"""
Goal Optimizer & Credit-Aware Purchase Planner Engine.

Answers questions like:
  "I want a $1,000 gadget — how can I afford it while keeping my auto-budget
   protected AND abiding by optimal FICO / AZEO credit rules?"

Generates 3 concrete, mathematically verified paths:
1. Path A — Zero-Interest Cash Sinking Fund (with real subscription/impulse accelerators)
2. Path B — Credit-Optimized Rewards Purchase (picks best grace-period card, enforces
   <= 8.9% / <= 28.9% utilization cliffs, calculates exact pre-statement-close paydown
   and post-close golden float timing!)
3. Path C — Balanced ADHD Anti-Deprivation Split (simultaneously crushes high-APR debt
   and builds your goal so you stay motivated without burnout!)
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from chronox_finance.engines.autobudget import AutoBudgetResult
from chronox_finance.models import (
    CreditLineOut,
    CreditOptimizationReport,
    DebtKind,
    GoalPlanOption,
    GoalPlanRequest,
    GoalPlanResponse,
    ImpulseOut,
    ImpulseStatus,
    JarKind,
)


def _fmt(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _build_velocity_warning(credit_report: CreditOptimizationReport) -> Optional[str]:
    """Return a calm warning against opening new promo/store cards when >=4/24 or inquiry velocity is high."""
    ftf = credit_report.five_twenty_four
    if ftf is None:
        return None
    parts: List[str] = []
    if ftf.count_5_24 >= 4:
        drop = f" (next slot drops {ftf.next_slot_drop_date})" if ftf.next_slot_drop_date else ""
        parts.append(f"you are at {ftf.count_5_24}/24 on Chase 5/24{drop}")
    if ftf.inquiries_6m >= 3 or ftf.inquiries_12m >= 5:
        parts.append(f"recent inquiry velocity is elevated ({ftf.inquiries_6m} in 6m / {ftf.inquiries_12m} in 12m)")
    if not parts:
        return None
    joined = " and ".join(parts)
    return (
        f"New-card promo guard: {joined}. Skip opening a new 0% promo or store card for this purchase — "
        f"use an existing grace-period card or the Cash Sinking Fund instead."
    )


class GoalOptimizerEngine:
    """Deterministic multi-constraint goal & purchase planner."""

    def optimize(
        self,
        *,
        req: GoalPlanRequest,
        budget: AutoBudgetResult,
        credit_report: CreditOptimizationReport,
        impulses: List[ImpulseOut],
        hourly_wage_cents: int,
        payday_cadence_days: int = 14,
        today: Optional[date] = None,
    ) -> GoalPlanResponse:
        ref = today or date.today()
        remaining = max(0, req.target_cents - req.saved_cents)
        work_hours = round(req.target_cents / max(100, hourly_wage_cents), 1)
        s = budget.safe_to_spend

        # Can user buy in cash right now without breaking bills, cards, or emergency buffer?
        can_buy_today = remaining == 0 or remaining <= s.safe_to_spend_cents

        # Determine realistic monthly flexible funding capacity (never raids Bills or Emergency Shield,
        # and never fabricates surplus when the profile has $0 income and $0 Safe-to-Spend).
        fun_jar = next((j for j in budget.jars if j.kind == JarKind.FUN), None)
        daily_jar = next((j for j in budget.jars if j.kind == JarKind.DAILY), None)
        fun_alloc = fun_jar.auto_allocated_cents if fun_jar else 0
        daily_alloc = daily_jar.auto_allocated_cents if daily_jar else 0
        jar_flex = int(round(fun_alloc * 0.50)) + int(round(daily_alloc * 0.10))

        monthly_bills_and_mins = sum(
            j.auto_allocated_cents for j in budget.jars if j.kind == JarKind.BILLS
        ) + credit_report.total_monthly_minimums_cents

        if budget.smoothed_monthly_income_cents > 0:
            if budget.smoothed_monthly_income_cents > monthly_bills_and_mins:
                income_headroom = int(
                    round((budget.smoothed_monthly_income_cents - monthly_bills_and_mins) * 0.45)
                )
                monthly_flex = min(max(jar_flex, min(15_000, income_headroom)), income_headroom)
            else:
                monthly_flex = min(jar_flex, max(0, s.safe_to_spend_cents))
        else:
            monthly_flex = min(jar_flex, max(0, s.safe_to_spend_cents))

        # Collect concrete data-backed accelerators from user's real subscriptions & matured impulses
        accelerators: List[Dict[str, Any]] = []
        extra_monthly_from_subs = 0
        for b in budget.upcoming_bills:
            if b.flagged_zombie or b.price_hike_cents > 0 or (b.is_subscription and not b.is_essential):
                monthly_save = b.amount_cents if b.cadence.value == "monthly" else int(round(b.amount_cents / 12.0))
                if monthly_save >= 500:
                    extra_monthly_from_subs += monthly_save
                    accelerators.append(
                        {
                            "type": "trim_subscription",
                            "label": f"Pause/cancel {b.name} (+{_fmt(monthly_save)}/mo)",
                            "monthly_gain_cents": monthly_save,
                            "bill_id": b.id,
                        }
                    )
        ready_imps = [i for i in impulses if i.status == ImpulseStatus.READY]
        for imp in ready_imps[:2]:
            accelerators.append(
                {
                    "type": "skip_matured_impulse",
                    "label": f"Skip matured '{imp.item_name}' (+{_fmt(imp.price_cents)} instant boost)",
                    "one_time_gain_cents": imp.price_cents,
                    "impulse_id": imp.id,
                }
            )
        accelerators = accelerators[:4]

        # Check high-APR revolving debt warning (>= 15% APR)
        high_apr_debts = [
            d for d in credit_report.credit_lines
            if d.balance_cents > 0 and d.effective_apr_bps >= 1500
        ]
        high_apr_warning: Optional[str] = None
        if high_apr_debts:
            worst = max(high_apr_debts, key=lambda d: d.effective_apr_bps)
            annual_opp_cost = int(round(remaining * (worst.effective_apr_bps / 10000.0)))
            high_apr_warning = (
                f"Heads-up (zero shame): {worst.name} is carrying {_fmt(worst.balance_cents)} at "
                f"{worst.effective_apr_bps / 100:.2f}% APR. Every {_fmt(remaining)} directed there first saves "
                f"~{_fmt(annual_opp_cost)}/yr in interest—check Path C for a guilt-free split!"
            )

        # --- Path A: Cash-Only Sinking Fund ---
        weekly_cash = int(round(monthly_flex / 4.33)) if monthly_flex > 0 else 0
        if monthly_flex > 0:
            weekly_cash = min(monthly_flex, max(100, weekly_cash))
        per_paycheck_cash = int(round(weekly_cash * (max(7, payday_cadence_days) / 7.0)))
        if req.target_date:
            try:
                td = date.fromisoformat(req.target_date)
                weeks_avail = max(1, int(math.ceil((td - ref).days / 7.0)))
                needed_weekly = int(math.ceil(remaining / weeks_avail))
                # Use target-date pace if it fits within 1.35x flexible surplus (or if surplus is $0 and user gave a deadline)
                if weekly_cash == 0 or needed_weekly <= int(weekly_cash * 1.35):
                    weekly_cash = needed_weekly
                    per_paycheck_cash = int(round(weekly_cash * (max(7, payday_cadence_days) / 7.0)))
            except ValueError:
                pass

        if remaining == 0:
            weeks_a = 0
        elif weekly_cash > 0:
            weeks_a = max(1, int(math.ceil(remaining / weekly_cash)))
        else:
            weeks_a = 0
        ready_a = (ref + timedelta(days=weeks_a * 7)).isoformat()
        accel_note = ""
        if extra_monthly_from_subs > 0 and weeks_a > 1:
            faster_weekly = weekly_cash + int(round(extra_monthly_from_subs / 4.33))
            faster_weeks = max(1, int(math.ceil(remaining / max(1, faster_weekly))))
            if faster_weeks < weeks_a:
                accel_note = f"Trimming unused subs cuts wait time from {weeks_a}w → {faster_weeks}w!"

        if weekly_cash > 0 or remaining == 0:
            steps_a = [
                f"Auto-set aside {_fmt(per_paycheck_cash)} per paycheck ({_fmt(weekly_cash)}/week) into a '{req.item_name}' goal.",
                f"Keep Bills ({_fmt(s.unpaid_bills_until_payday_cents)}) & Emergency Shield ({_fmt(s.emergency_buffer_cents)}) 100% untouched.",
            ]
            summary_a = (
                f"Save {_fmt(weekly_cash)}/week ({_fmt(per_paycheck_cash)}/paycheck) and own {req.item_name} "
                f"debt-free by {ready_a} ({weeks_a} weeks) with $0 interest."
            )
        else:
            steps_a = [
                f"$0.00 monthly flexible surplus currently tracked — add an account balance or income deposit to size a weekly contribution for '{req.item_name}'.",
                f"Keep Bills ({_fmt(s.unpaid_bills_until_payday_cents)}) & Emergency Shield ({_fmt(s.emergency_buffer_cents)}) 100% untouched.",
            ]
            summary_a = (
                f"$0.00 monthly flexible surplus currently tracked — add income or liquid cash (or set a target date) "
                f"to calculate a debt-free savings schedule for {req.item_name}."
            )
        if accel_note:
            steps_a.append(accel_note)
        if can_buy_today and not high_apr_debts:
            steps_a.insert(0, f"You actually have {_fmt(s.safe_to_spend_cents)} Safe-to-Spend right now — you could buy cash today if you want!")

        path_a = GoalPlanOption(
            path_id="cash_sinking_fund",
            title="Cash Sinking Fund (Zero Interest, Zero Credit Drag)",
            recommended=(not req.prefer_credit_rewards or bool(high_apr_debts) or not credit_report.credit_lines),
            credit_safe=True,
            weekly_set_aside_cents=weekly_cash,
            per_paycheck_cents=per_paycheck_cash,
            weeks_to_goal=weeks_a,
            projected_ready_date=ready_a,
            interest_cost_cents=0,
            summary=summary_a,
            steps=steps_a,
            credit_impact_note="0% impact on credit utilization or DTI — keeps FICO & AZEO spotless.",
        )

        # --- Path B: Credit-Optimized Rewards Purchase ---
        path_b = self._build_credit_optimized_path(
            req=req,
            remaining=remaining,
            weeks_cash=weeks_a,
            weekly_cash=weekly_cash,
            per_paycheck_cash=per_paycheck_cash,
            ready_cash=ready_a,
            can_buy_today=can_buy_today,
            credit_report=credit_report,
            ref=ref,
        )

        # --- Path C: Balanced ADHD Anti-Deprivation Split (Debt + Goal Together) ---
        if weekly_cash > 0:
            split_goal_weekly = max(100, int(round(weekly_cash * 0.45)))
            split_debt_weekly = max(0, weekly_cash - split_goal_weekly)
            split_paycheck = int(round(split_goal_weekly * (max(7, payday_cadence_days) / 7.0)))
            weeks_c = 0 if remaining == 0 else max(1, int(math.ceil(remaining / max(1, split_goal_weekly))))
        else:
            split_goal_weekly = 0
            split_debt_weekly = 0
            split_paycheck = 0
            weeks_c = 0
        ready_c = (ref + timedelta(days=weeks_c * 7)).isoformat()
        top_debt = high_apr_debts[0] if high_apr_debts else (credit_report.credit_lines[0] if credit_report.credit_lines else None)
        split_target_label = top_debt.name if top_debt else "Safety & Future Buffer"

        if weekly_cash > 0 or remaining == 0:
            summary_c = (
                f"Send {_fmt(split_debt_weekly)}/wk to {split_target_label} AND {_fmt(split_goal_weekly)}/wk to "
                f"{req.item_name} (ready {ready_c}) — avoids ADHD deprivation burnout while lowering utilization & interest."
            )
            steps_c = [
                f"Route {_fmt(split_debt_weekly)}/wk automatically to {split_target_label}.",
                f"Park {_fmt(split_goal_weekly)}/wk ({_fmt(split_paycheck)}/paycheck) into your {req.item_name} goal.",
                "Never feel 'banned' from fun goals while still cutting debt & boosting credit every week!",
            ]
        else:
            summary_c = (
                f"$0.00 flexible weekly surplus currently tracked to split between {split_target_label} and {req.item_name} — "
                f"add income or cash headroom first."
            )
            steps_c = [
                f"Once positive monthly surplus is tracked, split contributions between {split_target_label} and {req.item_name}.",
                "Protect essential bills and minimum debt payments before funding discretionary purchases.",
            ]

        path_c = GoalPlanOption(
            path_id="balanced_debt_goal_split",
            title=f"ADHD Balanced Split ({split_target_label} + Goal Together)",
            recommended=bool(high_apr_debts),
            credit_safe=True,
            weekly_set_aside_cents=split_goal_weekly,
            per_paycheck_cents=split_paycheck,
            weeks_to_goal=weeks_c,
            projected_ready_date=ready_c,
            interest_cost_cents=0,
            summary=summary_c,
            steps=steps_c,
            credit_impact_note=(
                f"Actively improves credit score by reducing {split_target_label} balance every week before buying."
            ),
        )

        # Ensure exactly ONE path carries the primary recommended badge
        options = [path_a, path_b, path_c]
        if bool(high_apr_debts):
            for opt in options:
                opt.recommended = opt.path_id == "balanced_debt_goal_split"
        elif path_b.credit_safe and req.prefer_credit_rewards:
            for opt in options:
                opt.recommended = opt.path_id == "credit_optimized_buy"
        else:
            for opt in options:
                opt.recommended = opt.path_id == "cash_sinking_fund"

        return GoalPlanResponse(
            item_name=req.item_name,
            target_cents=req.target_cents,
            already_saved_cents=req.saved_cents,
            remaining_cents=remaining,
            work_hours_cost=work_hours,
            can_buy_cash_today=can_buy_today,
            monthly_flexible_surplus_cents=monthly_flex,
            high_apr_debt_warning=high_apr_warning,
            accelerators=accelerators,
            options=options,
        )

    def _build_credit_optimized_path(
        self,
        *,
        req: GoalPlanRequest,
        remaining: int,
        weeks_cash: int,
        weekly_cash: int,
        per_paycheck_cash: int,
        ready_cash: str,
        can_buy_today: bool,
        credit_report: CreditOptimizationReport,
        ref: date,
    ) -> GoalPlanOption:
        velocity_warning = _build_velocity_warning(credit_report)
        cards = [
            c for c in credit_report.credit_lines
            if c.kind == DebtKind.CREDIT_CARD and c.credit_limit_cents > 0
        ]
        grace_cards = [
            c for c in cards
            if c.has_grace_period and (c.credit_limit_cents - c.balance_cents) >= req.target_cents
        ]

        if not grace_cards:
            # Compute actual Revolving APR cost ONLY if the user has tracked credit cards;
            # never fabricate an invented APR when no cards exist.
            actual_interest_cents = 0
            if remaining > 0 and cards:
                lowest_apr_bps = min(c.effective_apr_bps for c in cards)
                if lowest_apr_bps > 0:
                    months_est = max(1, int(math.ceil(weeks_cash / 4.33))) if weeks_cash > 0 else 12
                    monthly_pay = max(1, int(math.ceil(remaining / months_est)))
                    bal = remaining
                    for _ in range(months_est):
                        if bal <= 0:
                            break
                        mo_int = int(round(bal * (lowest_apr_bps / 10000.0) / 12.0))
                        actual_interest_cents += mo_int
                        bal = max(0, bal + mo_int - monthly_pay)
            blocked_steps = [
                "Do NOT put this purchase on a card without an active grace period (interest starts Day 1!).",
                f"Build the {_fmt(remaining)} cash sinking fund first, or reduce card balances below 29% utilization.",
            ]
            blocked_note = "Warning: putting this on credit now would spike utilization or trigger immediate APR charges."
            if velocity_warning:
                blocked_steps.append(velocity_warning)
                blocked_note = f"{blocked_note} {velocity_warning}"
            return GoalPlanOption(
                path_id="credit_optimized_buy",
                title="Credit Card Purchase (Blocked by Credit Optimizer)",
                recommended=False,
                credit_safe=False,
                weekly_set_aside_cents=weekly_cash,
                per_paycheck_cents=per_paycheck_cash,
                weeks_to_goal=weeks_cash,
                projected_ready_date=ready_cash,
                interest_cost_cents=actual_interest_cents,
                summary=(
                    "No credit card currently has both an active grace period AND enough safe credit limit headroom "
                    "for this purchase. Use Cash Sinking Fund (Path A) or Balanced Split (Path C) first."
                ),
                steps=blocked_steps,
                credit_impact_note=blocked_note,
            )

        # Pick card yielding lowest post-purchase per-card utilization
        best: CreditLineOut = min(
            grace_cards,
            key=lambda c: (c.balance_cents + req.target_cents) / c.credit_limit_cents,
        )
        post_card_bal = best.balance_cents + req.target_cents
        post_card_util = round((post_card_bal / best.credit_limit_cents) * 100.0, 1)
        total_lim = max(1, credit_report.total_revolving_limit_cents)
        post_agg_util = round(((credit_report.total_revolving_balance_cents + req.target_cents) / total_lim) * 100.0, 1)

        # How much must be paid BEFORE statement close so card reports <= 8.9% (AZEO target band)?
        safe_89_cap = int(math.floor(best.credit_limit_cents * 0.089))
        pre_close_paydown = max(0, post_card_bal - safe_89_cap)
        close_dt = date.fromisoformat(best.next_statement_close_date)
        golden_buy_dt = close_dt + timedelta(days=1)
        pay_by_dt = max(ref, close_dt - timedelta(days=3))

        is_safe = can_buy_today or (0 < weeks_cash <= 12)
        steps = [
            f"Best card in wallet: {best.name} (active grace period{', ' + best.rewards_note if best.rewards_note else ''}).",
            (
                f"Cash-backed rule: accumulate {_fmt(remaining)} in your sinking fund first ({weeks_cash}w → {ready_cash}) "
                f"so you NEVER carry a revolving balance or pay $0.01 in APR."
                if not can_buy_today
                else f"You already have {_fmt(remaining)} cash backing available in Safe-to-Spend."
            ),
            f"Golden float window: buy right after statement close ({golden_buy_dt.isoformat()}) for maximum ~50-day interest-free float.",
        ]
        if pre_close_paydown > 0:
            steps.append(
                f"FICO Shield: pre-pay {_fmt(pre_close_paydown)} from your cash fund 3 days before statement close "
                f"(e.g. by {pay_by_dt.isoformat()}) so {best.name} reports <= 8.9% instead of {post_card_util:.1f}%!"
            )
        else:
            steps.append(
                f"Utilization stays at {post_card_util:.1f}% on {best.name} ({post_agg_util:.1f}% aggregate) — inside the <8.9% AZEO target band."
            )
        if velocity_warning:
            steps.append(velocity_warning)

        impact_note = (
            f"Unbuffered util would hit {post_card_util:.1f}% card / {post_agg_util:.1f}% agg; "
            f"pre-paying {_fmt(pre_close_paydown)} before statement close locks reported util <= 8.9% (AZEO-compliant)."
        )
        if velocity_warning:
            impact_note = f"{impact_note} {velocity_warning}"

        return GoalPlanOption(
            path_id="credit_optimized_buy",
            title=f"Credit-Optimized Rewards Buy via {best.name}",
            recommended=False,
            credit_safe=is_safe,
            weekly_set_aside_cents=weekly_cash if not can_buy_today else 0,
            per_paycheck_cents=per_paycheck_cash if not can_buy_today else 0,
            weeks_to_goal=0 if can_buy_today else weeks_cash,
            projected_ready_date=ref.isoformat() if can_buy_today else ready_cash,
            interest_cost_cents=0,
            summary=(
                f"Earn card rewards & buyer protection on {best.name} with $0 interest: "
                f"back it 100% with cash, buy after statement close, and pre-pay {_fmt(pre_close_paydown)} before closing day."
            ),
            steps=steps,
            credit_impact_note=impact_note,
        )
