"""
Research-Backed Credit Score & Debt Optimization Engine ("Niche Credit Advisor").

Grounded in empirical FICO scoring mechanics and ADHD behavioral finance:
1. Per-Card AND Aggregate Revolving Utilization Tracking (FICO 30% weight):
   - Tiers: All-Zero Inactivity Penalty (0%), Optimal 800+ Sweet Spot (1%–8.9%),
     Good (9%–28.9%), Score Drag (29%–48.9%), High Drag (49%–88.9%), Maxed (89%+).
2. AZEO (All Zero Except One) Pre-Statement-Close Planner:
   - Targets reporting dates (`statement_closing_day`), NOT due dates!
   - Computes exact dollar paydowns 3 days before statement close so exactly ONE
     card reports 1%–4.9% and all other cards report $0.00.
3. Grace-Period & 0% Promo APR Cliff Protection:
   - Warns when carrying a balance forfeits grace period on new charges.
   - Calculates exact monthly payoff needed to beat expiring 0% promo APR cliffs.
4. Autopay Safety Audit & Average Age of Accounts (AAoA) Oldest-Card Shield.
5. 4-Strategy Exact Integer-Cent Debt Payoff Simulator:
   - ADHD Hybrid (Snowball smallest 1–2 clutter accounts -> Avalanche high APR)
   - Credit-First (Fix >29% utilization credit killers first -> Avalanche)
   - Avalanche (Lowest total interest paid)
   - Snowball (Fastest account closures & fastest DTI drop)
6. Debt-to-Income (DTI) Lender Benchmark & Balance Transfer Breakeven Calculator.
"""

from __future__ import annotations

import calendar
import math
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from chronox_finance.models import (
    AutopayMode,
    AZEOAction,
    BalanceTransferRequest,
    BalanceTransferResult,
    CreditAdvisorAlert,
    CreditLineOut,
    CreditOptimizationReport,
    DebtKind,
    DebtStrategyResult,
    utc_now_iso,
)

MAX_SIM_MONTHS = 360  # 30-year safety cap


def next_occurrence_of_day(day_of_month: int, today: date) -> date:
    """Return the next date on or after `today` matching `day_of_month` (clamped to month length)."""
    dom = max(1, min(31, int(day_of_month)))
    max_this = calendar.monthrange(today.year, today.month)[1]
    candidate = date(today.year, today.month, min(dom, max_this))
    if candidate >= today:
        return candidate
    next_y = today.year + (1 if today.month == 12 else 0)
    next_m = 1 if today.month == 12 else today.month + 1
    max_next = calendar.monthrange(next_y, next_m)[1]
    return date(next_y, next_m, min(dom, max_next))


def classify_utilization_tier(util_pct: float, is_aggregate: bool = False, all_zero: bool = False) -> str:
    if is_aggregate and all_zero:
        return "zero_inactivity_penalty"
    if util_pct <= 0.0:
        return "zero"
    if util_pct < 9.0:
        return "optimal_800_tier"
    if util_pct < 29.0:
        return "good"
    if util_pct < 49.0:
        return "score_drag"
    if util_pct < 89.0:
        return "high_drag"
    return "maxed_severe"


def classify_dti_tier(dti_pct: float) -> str:
    if dti_pct <= 20.0:
        return "excellent"
    if dti_pct <= 35.0:
        return "healthy_lender_ready"
    if dti_pct <= 43.0:
        return "tight"
    return "elevated_high_risk"


def compute_effective_min_payment(balance_cents: int, configured_min_cents: int, kind: DebtKind, apr_bps: int) -> int:
    """Compute realistic minimum monthly payment in cents when not explicitly provided."""
    if balance_cents <= 0:
        return 0
    if configured_min_cents > 0:
        return min(balance_cents, configured_min_cents)
    monthly_interest = int(round(balance_cents * (apr_bps / 10000.0) / 12.0))
    if kind == DebtKind.CREDIT_CARD:
        # Typical issuer rule: max($25, 1% of balance + monthly interest, or 2% of balance)
        calc = max(2_500, int(round(balance_cents * 0.01)) + monthly_interest, int(round(balance_cents * 0.02)))
        return min(balance_cents, calc)
    calc = max(2_500, int(round(balance_cents * 0.025)) + monthly_interest)
    return min(balance_cents, calc)


def _add_months_date(ref: date, months: int) -> date:
    total = (ref.year * 12 + (ref.month - 1)) + months
    y = total // 12
    m = (total % 12) + 1
    d = min(ref.day, calendar.monthrange(y, m)[1])
    return date(y, m, d)


def _months_since(opened_iso: Optional[str], ref: date) -> Optional[int]:
    if not opened_iso:
        return None
    try:
        dt = date.fromisoformat(opened_iso)
    except ValueError:
        return None
    months = (ref.year - dt.year) * 12 + (ref.month - dt.month)
    return max(0, months)


class CreditDebtOptimizer:
    """Deterministic expert credit & debt optimization engine."""

    def analyze(
        self,
        *,
        raw_lines: List[Dict[str, Any]],
        smoothed_monthly_income_cents: int,
        extra_monthly_budget_cents: int = 25_000,
        today: Optional[date] = None,
    ) -> CreditOptimizationReport:
        ref = today or date.today()
        active_lines = [r for r in raw_lines if r.get("active", 1)]

        # Enrich all credit lines & debts
        enriched: List[CreditLineOut] = []
        ages: List[Tuple[int, str]] = []

        for r in active_lines:
            kind = DebtKind(r.get("kind", DebtKind.CREDIT_CARD.value))
            bal = max(0, int(r.get("balance_cents", 0)))
            limit = max(0, int(r.get("credit_limit_cents", 0)))
            apr_bps = max(0, int(r.get("apr_bps", 0)))

            promo_apr = r.get("promo_apr_bps")
            promo_end = r.get("promo_ends_date")
            promo_days: Optional[int] = None
            eff_apr = apr_bps
            if promo_apr is not None and promo_end:
                try:
                    pe_dt = date.fromisoformat(promo_end)
                    promo_days = (pe_dt - ref).days
                    if promo_days >= 0:
                        eff_apr = int(promo_apr)
                except ValueError:
                    promo_days = None

            eff_min = compute_effective_min_payment(
                bal, int(r.get("min_payment_cents", 0)), kind, eff_apr
            )
            close_day = int(r.get("statement_closing_day", 15))
            due_day = int(r.get("payment_due_day", 10))
            next_close = next_occurrence_of_day(close_day, ref)
            next_due = next_occurrence_of_day(due_day, ref)

            util_pct: Optional[float] = None
            util_tier = "installment"
            if kind == DebtKind.CREDIT_CARD and limit > 0:
                util_pct = round((bal / limit) * 100.0, 2)
                util_tier = classify_utilization_tier(util_pct)

            age_m = _months_since(r.get("opened_date"), ref)
            if age_m is not None:
                ages.append((age_m, r["name"]))

            monthly_int = int(round(bal * (eff_apr / 10000.0) / 12.0)) if bal > 0 else 0

            enriched.append(
                CreditLineOut(
                    id=int(r["id"]),
                    name=r["name"],
                    issuer=r.get("issuer", ""),
                    kind=kind,
                    balance_cents=bal,
                    credit_limit_cents=limit,
                    apr_bps=apr_bps,
                    effective_apr_bps=eff_apr,
                    min_payment_cents=int(r.get("min_payment_cents", 0)),
                    effective_min_payment_cents=eff_min,
                    statement_closing_day=close_day,
                    payment_due_day=due_day,
                    next_statement_close_date=next_close.isoformat(),
                    days_until_close=(next_close - ref).days,
                    next_due_date=next_due.isoformat(),
                    days_until_due=(next_due - ref).days,
                    utilization_pct=util_pct,
                    utilization_tier=util_tier,
                    promo_apr_bps=int(promo_apr) if promo_apr is not None else None,
                    promo_ends_date=promo_end,
                    promo_days_remaining=promo_days,
                    opened_date=r.get("opened_date"),
                    age_months=age_m,
                    is_oldest_account=False,
                    has_grace_period=bool(r.get("has_grace_period", 1)),
                    autopay_mode=AutopayMode(r.get("autopay_mode", AutopayMode.STATEMENT.value)),
                    rewards_note=r.get("rewards_note", ""),
                    account_id=r.get("account_id"),
                    monthly_interest_cents=monthly_int,
                )
            )

        # Mark oldest account (15% of FICO Length of Credit History protection)
        oldest_name: Optional[str] = None
        avg_age: Optional[float] = None
        if ages:
            avg_age = round(sum(a for a, _ in ages) / len(ages), 1)
            max_age, oldest_name = max(ages, key=lambda pair: pair[0])
            for line in enriched:
                if line.age_months == max_age and line.name == oldest_name:
                    line.is_oldest_account = True
                    break

        cards = [c for c in enriched if c.kind == DebtKind.CREDIT_CARD]
        total_rev_bal = sum(c.balance_cents for c in cards)
        total_rev_lim = sum(c.credit_limit_cents for c in cards)
        agg_util = round((total_rev_bal / total_rev_lim) * 100.0, 2) if total_rev_lim > 0 else 0.0
        cards_with_bal = sum(1 for c in cards if c.balance_cents > 0)
        all_zero_risk = len(cards) > 0 and total_rev_lim > 0 and total_rev_bal == 0
        agg_tier = classify_utilization_tier(agg_util, is_aggregate=True, all_zero=all_zero_risk)

        total_debt = sum(d.balance_cents for d in enriched)
        total_mins = sum(d.effective_min_payment_cents for d in enriched)
        monthly_burn = sum(d.monthly_interest_cents for d in enriched)

        income_base = max(100_000, int(smoothed_monthly_income_cents))
        dti_pct = round((total_mins / income_base) * 100.0, 1) if total_mins > 0 else 0.0
        dti_tier = classify_dti_tier(dti_pct)

        autopay_missing = sum(1 for d in enriched if d.autopay_mode == AutopayMode.NONE and (d.balance_cents > 0 or d.kind == DebtKind.CREDIT_CARD))

        azeo_plan = self._build_azeo_plan(cards, total_rev_lim, ref)
        strategies, rec_strategy = self._simulate_all_strategies(
            enriched, max(0, int(extra_monthly_budget_cents)), ref
        )
        alerts = self._build_expert_alerts(
            enriched=enriched,
            cards=cards,
            agg_util=agg_util,
            all_zero_risk=all_zero_risk,
            dti_pct=dti_pct,
            oldest_name=oldest_name,
        )

        return CreditOptimizationReport(
            generated_at=utc_now_iso(),
            total_revolving_balance_cents=total_rev_bal,
            total_revolving_limit_cents=total_rev_lim,
            aggregate_utilization_pct=agg_util,
            aggregate_tier=agg_tier,
            all_zero_penalty_risk=all_zero_risk,
            cards_with_balance_count=cards_with_bal,
            total_cards_count=len(cards),
            total_debt_cents=total_debt,
            total_monthly_minimums_cents=total_mins,
            monthly_interest_burn_cents=monthly_burn,
            dti_pct=dti_pct,
            dti_tier=dti_tier,
            avg_account_age_months=avg_age,
            oldest_account_name=oldest_name,
            autopay_missing_count=autopay_missing,
            extra_monthly_budget_cents=max(0, int(extra_monthly_budget_cents)),
            credit_lines=enriched,
            azeo_plan=azeo_plan,
            strategies=strategies,
            recommended_strategy=rec_strategy,
            alerts=alerts,
        )

    def _build_azeo_plan(
        self, cards: List[CreditLineOut], total_limit_cents: int, today: date
    ) -> List[AZEOAction]:
        """
        Compute exact All-Zero-Except-One (AZEO) pre-statement-close paydown schedule.
        Selects 1 primary card to report 1%–4.9% (avoiding the All-Zero Penalty)
        and brings every other card to $0.00 before its closing date.
        """
        eligible = [c for c in cards if c.credit_limit_cents > 0]
        if not eligible:
            return []

        # Pick best primary reporting card: prefers active grace period, highest limit, non-zero balance
        primary = sorted(
            eligible,
            key=lambda c: (
                0 if c.has_grace_period else 1,
                0 if c.balance_cents > 0 else 1,
                -c.credit_limit_cents,
            ),
        )[0]

        # Ideal primary statement balance: 2% of card limit, >= $10, strictly < 4.9% of card & aggregate limits
        ideal_primary = int(round(primary.credit_limit_cents * 0.02))
        max_card_cap = max(1_000, int(math.floor(primary.credit_limit_cents * 0.049)))
        max_agg_cap = max(1_000, int(math.floor(total_limit_cents * 0.049)))
        target_primary = max(1_000, min(ideal_primary, max_card_cap, max_agg_cap))

        actions: List[AZEOAction] = []
        for card in sorted(eligible, key=lambda c: c.days_until_close):
            close_dt = date.fromisoformat(card.next_statement_close_date)
            pay_by_dt = max(today, close_dt - timedelta(days=3))

            if card.id == primary.id:
                if card.balance_cents == 0:
                    actions.append(
                        AZEOAction(
                            credit_line_id=card.id,
                            card_name=card.name,
                            current_balance_cents=0,
                            target_statement_balance_cents=target_primary,
                            pay_before_close_cents=0,
                            pay_by_date=pay_by_dt.isoformat(),
                            statement_close_date=close_dt.isoformat(),
                            resulting_util_pct=round((target_primary / card.credit_limit_cents) * 100.0, 2),
                            role="primary_reporting_card",
                            explanation=(
                                f"Let a small ~${target_primary / 100:,.2f} recurring charge post on {card.name} "
                                f"before {close_dt.isoformat()} (then autopay in full on due date) to avoid the FICO All-Zero Penalty."
                            ),
                        )
                    )
                elif card.balance_cents <= max_card_cap and card.balance_cents <= max_agg_cap:
                    actions.append(
                        AZEOAction(
                            credit_line_id=card.id,
                            card_name=card.name,
                            current_balance_cents=card.balance_cents,
                            target_statement_balance_cents=card.balance_cents,
                            pay_before_close_cents=0,
                            pay_by_date=pay_by_dt.isoformat(),
                            statement_close_date=close_dt.isoformat(),
                            resulting_util_pct=round((card.balance_cents / card.credit_limit_cents) * 100.0, 2),
                            role="already_optimal",
                            explanation=(
                                f"Already in the 1%–4.9% AZEO sweet spot ({card.utilization_pct:.1f}%)! "
                                f"Just let statement close on {close_dt.isoformat()} and autopay statement balance."
                            ),
                        )
                    )
                else:
                    pay_amt = card.balance_cents - target_primary
                    actions.append(
                        AZEOAction(
                            credit_line_id=card.id,
                            card_name=card.name,
                            current_balance_cents=card.balance_cents,
                            target_statement_balance_cents=target_primary,
                            pay_before_close_cents=pay_amt,
                            pay_by_date=pay_by_dt.isoformat(),
                            statement_close_date=close_dt.isoformat(),
                            resulting_util_pct=round((target_primary / card.credit_limit_cents) * 100.0, 2),
                            role="primary_reporting_card",
                            explanation=(
                                f"Pay ${pay_amt / 100:,.2f} by {pay_by_dt.isoformat()} (3d before close on {close_dt.isoformat()}) "
                                f"to report ${target_primary / 100:,.2f} ({(target_primary / card.credit_limit_cents) * 100:.1f}% util — 800+ FICO tier)."
                            ),
                        )
                    )
            else:
                if card.balance_cents == 0:
                    actions.append(
                        AZEOAction(
                            credit_line_id=card.id,
                            card_name=card.name,
                            current_balance_cents=0,
                            target_statement_balance_cents=0,
                            pay_before_close_cents=0,
                            pay_by_date=pay_by_dt.isoformat(),
                            statement_close_date=close_dt.isoformat(),
                            resulting_util_pct=0.0,
                            role="already_optimal",
                            explanation="Already at $0.00 — keeps AWB (Accounts With Balances) minimal.",
                        )
                    )
                else:
                    actions.append(
                        AZEOAction(
                            credit_line_id=card.id,
                            card_name=card.name,
                            current_balance_cents=card.balance_cents,
                            target_statement_balance_cents=0,
                            pay_before_close_cents=card.balance_cents,
                            pay_by_date=pay_by_dt.isoformat(),
                            statement_close_date=close_dt.isoformat(),
                            resulting_util_pct=0.0,
                            role="zero_out_card",
                            explanation=(
                                f"Pay ${card.balance_cents / 100:,.2f} by {pay_by_dt.isoformat()} (3d before close) "
                                f"so {card.name} reports $0.00 to bureaus (AZEO)."
                            ),
                        )
                    )
        return actions

    def _simulate_strategy(
        self,
        debts: List[CreditLineOut],
        extra_monthly_cents: int,
        mode: str,
        today: date,
    ) -> Tuple[int, int, int, str, List[str]]:
        """
        Exact integer-cent month-by-month amortization simulator.
        Returns `(months, total_interest_cents, first_win_months, first_target_name, month_1_actions)`.
        """
        active = [
            {
                "id": d.id,
                "name": d.name,
                "kind": d.kind,
                "bal": d.balance_cents,
                "limit": d.credit_limit_cents,
                "apr": d.effective_apr_bps,
                "base_apr": d.apr_bps,
                "promo_end_month": (
                    max(0, int(round(d.promo_days_remaining / 30.4)))
                    if d.promo_days_remaining is not None and d.promo_days_remaining >= 0
                    else None
                ),
                "min": d.effective_min_payment_cents,
                "closed": False,
            }
            for d in debts
            if d.balance_cents > 0
        ]
        if not active:
            return 0, 0, 0, "Debt-Free!", ["Zero debt balances — keep autopay active on statement balances!"]

        # Precompute ADHD Hybrid small clutter accounts (up to 2 smallest balances <= $1,500 or smallest 1 if >=2 debts)
        small_ids: set[int] = set()
        if mode == "adhd_hybrid" and len(active) >= 2:
            by_small = sorted(active, key=lambda x: x["bal"])
            clutter = [x["id"] for x in by_small if x["bal"] <= 150_000][:2]
            if not clutter:
                clutter = [by_small[0]["id"]]
            small_ids = set(clutter)

        total_budget_pool = sum(d["min"] for d in active) + extra_monthly_cents
        total_interest = 0
        first_win_months = 0
        first_target_name = ""
        month_1_actions: List[str] = []

        def _pick_priority(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            alive = [x for x in items if x["bal"] > 0]
            if mode == "avalanche":
                return sorted(alive, key=lambda x: (-x["apr"], x["bal"]))
            if mode == "snowball":
                return sorted(alive, key=lambda x: (x["bal"], -x["apr"]))
            if mode == "adhd_hybrid":
                return sorted(
                    alive,
                    key=lambda x: (0 if x["id"] in small_ids else 1, x["bal"] if x["id"] in small_ids else -x["apr"]),
                )
            # credit_first: cards with util >= 29% first (highest util first), then Avalanche!
            def _cf_key(x: Dict[str, Any]) -> Tuple[int, float, int]:
                util = (x["bal"] / x["limit"] * 100.0) if (x["kind"] == DebtKind.CREDIT_CARD and x["limit"] > 0) else 0.0
                if util >= 29.0:
                    return (0, -util, -x["apr"])
                return (1, -float(x["apr"]), x["bal"])
            return sorted(alive, key=_cf_key)

        months = 0
        while any(d["bal"] > 0 for d in active) and months < MAX_SIM_MONTHS:
            months += 1
            # 1. Apply monthly interest (handle promo APR expiration cleanly!)
            for d in active:
                if d["bal"] <= 0:
                    continue
                if d["promo_end_month"] is not None and months > d["promo_end_month"]:
                    d["apr"] = d["base_apr"]
                interest = int(round(d["bal"] * (d["apr"] / 10000.0) / 12.0))
                d["bal"] += interest
                total_interest += interest

            # 2. Pay required minimums on all open accounts
            remaining_pool = total_budget_pool
            paid_m1: Dict[int, int] = {}
            for d in active:
                if d["bal"] <= 0:
                    continue
                req_min = min(d["bal"], d["min"])
                pay = min(req_min, remaining_pool)
                d["bal"] -= pay
                remaining_pool -= pay
                if months == 1:
                    paid_m1[d["id"]] = paid_m1.get(d["id"], 0) + pay

            # 3. Direct remaining snowball/avalanche surplus to priority target(s)
            ordered = _pick_priority(active)
            if months == 1 and ordered:
                first_target_name = ordered[0]["name"]

            for target in ordered:
                if remaining_pool <= 0:
                    break
                if target["bal"] <= 0:
                    continue
                # In credit_first mode, pay high-util card down to 28% threshold first if extra remains for another >29% card
                extra_pay = min(target["bal"], remaining_pool)
                target["bal"] -= extra_pay
                remaining_pool -= extra_pay
                if months == 1:
                    paid_m1[target["id"]] = paid_m1.get(target["id"], 0) + extra_pay

            # 4. Track first dopamine win (first debt reaching $0)
            for d in active:
                if d["bal"] <= 0 and not d["closed"]:
                    d["closed"] = True
                    if first_win_months == 0:
                        first_win_months = months

            if months == 1:
                for d in _pick_priority(active) + [x for x in active if x["bal"] <= 0]:
                    amt = paid_m1.get(d["id"], 0)
                    if amt > 0:
                        role = "FOCUS TARGET" if d["name"] == first_target_name else "Autopay Min"
                        month_1_actions.append(f"{d['name']}: pay ${amt / 100:,.2f} ({role})")

        if not first_target_name:
            first_target_name = active[0]["name"]
        return months, total_interest, first_win_months or months, first_target_name, month_1_actions[:5]

    def _simulate_all_strategies(
        self,
        debts: List[CreditLineOut],
        extra_monthly_cents: int,
        today: date,
    ) -> Tuple[List[DebtStrategyResult], str]:
        nonzero = [d for d in debts if d.balance_cents > 0]
        if not nonzero:
            return [], "debt_free"

        # Baseline: minimum payments only (0 extra)
        _m0, base_interest, _w0, _t0, _a0 = self._simulate_strategy(nonzero, 0, "avalanche", today)

        has_credit_killer = any(
            d.kind == DebtKind.CREDIT_CARD and (d.utilization_pct or 0.0) >= 29.0 for d in nonzero
        )
        recommended = "credit_first" if has_credit_killer else ("adhd_hybrid" if len(nonzero) >= 2 else "avalanche")

        specs = [
            ("adhd_hybrid", "ADHD Hybrid (Dopamine + Math)", "Clears 1–2 small clutter debts fast, then crushes highest APR."),
            ("credit_first", "Credit-First FICO Turbo", "Drops >29% cards below FICO cliff first to boost score, then Avalanche."),
            ("avalanche", "Debt Avalanche (Lowest Interest)", "Targets highest APR first for mathematical minimum interest."),
            ("snowball", "Debt Snowball (Fastest Wins)", "Targets smallest balance first for rapid closures & DTI drop."),
        ]

        results: List[DebtStrategyResult] = []
        for key, label, tagline in specs:
            months, interest, first_win, first_target, m1 = self._simulate_strategy(
                nonzero, extra_monthly_cents, key, today
            )
            results.append(
                DebtStrategyResult(
                    strategy=key,  # type: ignore[arg-type]
                    label=label,
                    tagline=tagline,
                    recommended_for_adhd=(key == recommended),
                    months_to_debt_free=months,
                    debt_free_date=_add_months_date(today, months).isoformat(),
                    total_interest_cents=interest,
                    interest_saved_vs_minimums_cents=max(0, base_interest - interest),
                    first_win_months=first_win,
                    first_target_name=first_target,
                    month_1_actions=m1,
                )
            )
        return results, recommended

    def _build_expert_alerts(
        self,
        *,
        enriched: List[CreditLineOut],
        cards: List[CreditLineOut],
        agg_util: float,
        all_zero_risk: bool,
        dti_pct: float,
        oldest_name: Optional[str],
    ) -> List[CreditAdvisorAlert]:
        alerts: List[CreditAdvisorAlert] = []

        # 1. Autopay missing (highest severity for ADHD!)
        for d in enriched:
            if d.autopay_mode == AutopayMode.NONE and (d.balance_cents > 0 or d.kind == DebtKind.CREDIT_CARD):
                alerts.append(
                    CreditAdvisorAlert(
                        severity="high",
                        category="autopay_shield",
                        title=f"Turn On Autopay for {d.name}",
                        detail=(
                            f"{d.name} has autopay OFF. A single forgotten 30-day late payment can drop FICO by 60–110 points "
                            f"for 7 years—automating at least the minimum payment eliminates ADHD forgetfulness risk."
                        ),
                        micro_step=f"Set autopay to Statement Balance (or Minimum) on {d.name} before {d.next_due_date}.",
                        credit_line_id=d.id,
                    )
                )

        # 2. 0% Promo APR Cliff Radar
        for d in enriched:
            if d.promo_days_remaining is not None and 0 <= d.promo_days_remaining <= 120 and d.balance_cents > 0:
                months_left = max(1, int(math.ceil(d.promo_days_remaining / 30.0)))
                req_monthly = int(math.ceil(d.balance_cents / months_left))
                alerts.append(
                    CreditAdvisorAlert(
                        severity="high",
                        category="promo_cliff",
                        title=f"0% Promo Cliff in {d.promo_days_remaining}d: {d.name}",
                        detail=(
                            f"{d.name} jumps to {d.apr_bps / 100:.2f}% APR on {d.promo_ends_date} "
                            f"(${d.balance_cents / 100:,.2f} remaining). Paying ${req_monthly / 100:,.2f}/mo clears it before interest hits."
                        ),
                        micro_step=f"Schedule ${req_monthly / 100:,.2f}/mo toward {d.name} until {d.promo_ends_date}.",
                        credit_line_id=d.id,
                    )
                )

        # 3. Per-card utilization cliffs (>29% / >49%) & Grace Period Lost
        for c in cards:
            util = c.utilization_pct or 0.0
            if util >= 29.0:
                target_bal = int(math.floor(c.credit_limit_cents * 0.289))
                pay_to_clear_cliff = max(0, c.balance_cents - target_bal)
                alerts.append(
                    CreditAdvisorAlert(
                        severity="high" if util >= 49.0 else "medium",
                        category="per_card_utilization",
                        title=f"Per-Card Utilization Drag on {c.name} ({util:.1f}%)",
                        detail=(
                            f"FICO penalizes individual cards over 29% even if your overall utilization is lower. "
                            f"Paying ${pay_to_clear_cliff / 100:,.2f} before {c.next_statement_close_date} drops {c.name} below the 29% cliff."
                        ),
                        micro_step=f"Prioritize ${pay_to_clear_cliff / 100:,.2f} toward {c.name} before statement close.",
                        credit_line_id=c.id,
                    )
                )
            if not c.has_grace_period and c.balance_cents > 0 and c.effective_apr_bps >= 1500:
                alerts.append(
                    CreditAdvisorAlert(
                        severity="high",
                        category="grace_period_lost",
                        title=f"Stop Swiping {c.name} (Grace Period Lost)",
                        detail=(
                            f"Because {c.name} is revolving at {c.effective_apr_bps / 100:.2f}% APR without a grace period, "
                            f"every new purchase accrues daily interest immediately from Day 1."
                        ),
                        micro_step=f"Put daily purchases on a debit card or a $0-balance card with an active grace period until {c.name} hits $0.",
                        credit_line_id=c.id,
                    )
                )

        # 4. All-Zero Inactivity Penalty (AZEO niche insight)
        if all_zero_risk:
            alerts.append(
                CreditAdvisorAlert(
                    severity="info",
                    category="azeo_inactivity",
                    title="FICO All-Zero Penalty Active (0% Aggregate Util)",
                    detail=(
                        "All your credit cards are reporting $0.00. FICO models apply a small 10–20 point inactivity dip "
                        "when every revolving card reports $0; letting 1 card report 1%–4.9% (AZEO) maximizes score."
                    ),
                    micro_step="Put one small recurring bill (e.g. $10–$25) on your primary card and autopay statement balance.",
                )
            )
        elif agg_util >= 9.0 and agg_util < 29.0:
            alerts.append(
                CreditAdvisorAlert(
                    severity="info",
                    category="azeo_optimization",
                    title=f"Unlock 800+ FICO Tier (Aggregate Util {agg_util:.1f}% → <8.9%)",
                    detail=(
                        "Under 30% prevents severe drag, but FICO's top tier rewards aggregate utilization between 1% and 8.9%. "
                        "Use the AZEO pre-statement paydown dates 30–60 days before any loan or apartment application."
                    ),
                    micro_step="Follow the AZEO pay-by dates below 3 days before each card's statement close.",
                )
            )

        # 5. DTI & Oldest Account Shield
        if dti_pct > 35.0:
            alerts.append(
                CreditAdvisorAlert(
                    severity="medium" if dti_pct <= 43.0 else "high",
                    category="dti",
                    title=f"Debt-to-Income Ratio at {dti_pct:.1f}%",
                    detail="Lenders prefer DTI under 36% (43% max for most qualified mortgages). Closing small monthly minimums lowers DTI fastest.",
                    micro_step="Check the Snowball or ADHD Hybrid payoff strategy to drop monthly minimum obligations quickly.",
                )
            )

        if oldest_name:
            alerts.append(
                CreditAdvisorAlert(
                    severity="info",
                    category="account_age",
                    title=f"Protect Your Oldest Credit Line: {oldest_name}",
                    detail=(
                        f"{oldest_name} anchors your Average Age of Accounts (15% of FICO) and total credit limit. "
                        f"Even if you simplify your wallet for ADHD focus, keep it open with a tiny recurring bill on autopay."
                    ),
                    micro_step=f"Never close {oldest_name} if it has no annual fee—sock-drawer it with autopay enabled.",
                )
            )

        return alerts[:8]

    def evaluate_balance_transfer(self, req: BalanceTransferRequest) -> BalanceTransferResult:
        """Exact amortization breakeven analysis for a 0% balance transfer offer."""
        fee_cents = int(round(req.balance_cents * (req.transfer_fee_bps / 10000.0)))

        # Scenario A: Keep current card at current_apr_bps
        bal_a = req.balance_cents
        int_a = 0
        m = 0
        while bal_a > 0 and m < MAX_SIM_MONTHS:
            m += 1
            interest = int(round(bal_a * (req.current_apr_bps / 10000.0) / 12.0))
            bal_a += interest
            int_a += interest
            if req.monthly_payment_cents <= interest:
                int_a += interest * 24  # Unaffordable / negative amortization penalty
                break
            bal_a = max(0, bal_a - req.monthly_payment_cents)

        # Scenario B: Transfer balance + fee at 0% for promo_months, then current_apr_bps thereafter
        bal_b = req.balance_cents + fee_cents
        int_b = 0
        m_b = 0
        while bal_b > 0 and m_b < MAX_SIM_MONTHS:
            m_b += 1
            if m_b > req.promo_months:
                interest = int(round(bal_b * (req.current_apr_bps / 10000.0) / 12.0))
                bal_b += interest
                int_b += interest
                if req.monthly_payment_cents <= interest:
                    int_b += interest * 24
                    break
            bal_b = max(0, bal_b - req.monthly_payment_cents)

        total_cost_b = fee_cents + int_b
        net_savings = int_a - total_cost_b
        req_monthly_clear = int(math.ceil((req.balance_cents + fee_cents) / req.promo_months))
        clears_promo = m_b <= req.promo_months

        if net_savings >= 15_000 and clears_promo:
            verdict = "strong_yes"
            expl = (
                f"Strong YES: saves ${net_savings / 100:,.2f} net after the ${fee_cents / 100:,.2f} transfer fee "
                f"and pays off cleanly inside the {req.promo_months}-month 0% window."
            )
        elif net_savings >= 5_000:
            verdict = "marginal"
            expl = (
                f"Marginal win: saves ${net_savings / 100:,.2f} net, "
                f"{'clearing within promo.' if clears_promo else f'but requires ${req_monthly_clear / 100:,.2f}/mo to beat the {req.promo_months}-mo promo cliff.'}"
            )
        else:
            verdict = "skip"
            expl = (
                f"Skip this transfer: the ${fee_cents / 100:,.2f} fee eats the interest savings "
                f"(net impact: {'+' if net_savings >= 0 else '-'}${abs(net_savings) / 100:,.2f}). Just pay down your current card directly."
            )

        return BalanceTransferResult(
            verdict=verdict,  # type: ignore[arg-type]
            transfer_fee_cents=fee_cents,
            interest_without_transfer_cents=int_a,
            interest_with_transfer_cents=int_b,
            net_savings_cents=net_savings,
            required_monthly_to_clear_promo_cents=req_monthly_clear,
            clears_before_promo_ends=clears_promo,
            explanation=expl,
        )
