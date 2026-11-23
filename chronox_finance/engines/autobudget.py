"""
Self-Updating Auto-Budgeting Engine for ChronoX Finance.

Every financial event (new expense, paycheck deposit, CSV import, bill detection,
jar rebalance) triggers a deterministic recomputation in exact integer cents:

1. Smoothed Monthly Income (handles irregular/variable paychecks calmly)
2. Bill-First Adaptive Jar Allocations (3-month EMA reality blending)
3. Dynamic Rollover Accounting + Intra-Cycle Rebalance Transfers
4. Real-Time "Safe-to-Spend" & "Daily Safe-to-Spend" Firewall
5. Zero-Guilt 1-Click Auto-Rebalance Plans (never raids protected Bills)
6. ChronoX "Don't Trade Your Rent" Safe-to-Invest Capital Cap
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from chronox_finance.engines.recurring import DetectedPayday, detect_payday_schedule
from chronox_finance.models import (
    AccountType,
    JarHealth,
    JarKind,
    JarState,
    RebalancePlan,
    RecurrenceCadence,
    RecurringBillOut,
    SafeToSpendBreakdown,
)


def current_cycle_ym(today: Optional[date] = None) -> str:
    ref = today or date.today()
    return f"{ref.year:04d}-{ref.month:02d}"


def _month_bounds(ref: date) -> Tuple[date, date, float]:
    days_in_month = calendar.monthrange(ref.year, ref.month)[1]
    start = date(ref.year, ref.month, 1)
    end = date(ref.year, ref.month, days_in_month)
    progress_pct = round(min(100.0, max(1.0, (ref.day / days_in_month) * 100.0)), 1)
    return start, end, progress_pct


def _bill_monthly_equivalent_cents(amount_cents: int, cadence: str) -> int:
    multipliers = {
        RecurrenceCadence.WEEKLY.value: 52.0 / 12.0,
        RecurrenceCadence.BIWEEKLY.value: 26.0 / 12.0,
        RecurrenceCadence.SEMIMONTHLY.value: 2.0,
        RecurrenceCadence.MONTHLY.value: 1.0,
        RecurrenceCadence.BIMONTHLY.value: 0.5,
        RecurrenceCadence.QUARTERLY.value: 1.0 / 3.0,
        RecurrenceCadence.SEMIANNUAL.value: 1.0 / 6.0,
        RecurrenceCadence.ANNUAL.value: 1.0 / 12.0,
    }
    return int(round(amount_cents * multipliers.get(cadence, 1.0)))


def _estimate_smoothed_monthly_income(
    transactions: List[Dict[str, Any]],
    payday: DetectedPayday,
    today: date,
) -> Tuple[int, int]:
    """
    Return `(current_month_income_cents, smoothed_monthly_income_cents)`.

    Uses up to 3 trailing calendar months with exponential smoothing (0.5, 0.3, 0.2)
    or conservative paycheck projection so variable income never whipsaws the budget.
    """
    ym_now = current_cycle_ym(today)
    monthly_totals: Dict[str, int] = defaultdict(int)
    for tx in transactions:
        if tx.get("tx_type") == "income":
            ym = tx["tx_date"][:7]
            monthly_totals[ym] += int(tx["amount_cents"])

    current_month_income = monthly_totals.get(ym_now, 0)

    # Trailing completed months (up to 3)
    trailing_months: List[int] = []
    cursor = date(today.year, today.month, 1)
    for _ in range(3):
        prev_last = cursor - timedelta(days=1)
        ym = f"{prev_last.year:04d}-{prev_last.month:02d}"
        if monthly_totals.get(ym, 0) > 0:
            trailing_months.append(monthly_totals[ym])
        cursor = date(prev_last.year, prev_last.month, 1)

    if trailing_months:
        weights = [0.5, 0.3, 0.2][: len(trailing_months)]
        w_sum = sum(weights)
        ema_income = int(round(sum(v * w for v, w in zip(trailing_months, weights, strict=True)) / w_sum))
        # Blend in current month if it's already larger than 70% of EMA
        if current_month_income > int(ema_income * 0.7):
            smoothed = int(round(0.65 * ema_income + 0.35 * current_month_income))
        else:
            smoothed = ema_income
        return current_month_income, max(smoothed, current_month_income)

    # Fallback: project from paycheck cadence
    projected = int(round(payday.estimated_paycheck_cents * (30.4 / max(7, payday.cadence_days))))
    return current_month_income, max(current_month_income, projected)


def _compute_trailing_jar_spend_ema(
    transactions: List[Dict[str, Any]],
    today: date,
) -> Dict[int, int]:
    """3-month EMA of historical spending per jar_id for self-updating target adaptation."""
    by_jar_month: Dict[int, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    ym_now = current_cycle_ym(today)
    for tx in transactions:
        if tx.get("tx_type") == "expense" and tx.get("jar_id") is not None:
            ym = tx["tx_date"][:7]
            if ym != ym_now:
                by_jar_month[int(tx["jar_id"])][ym] += int(tx["amount_cents"])

    result: Dict[int, int] = {}
    for jar_id, months in by_jar_month.items():
        ordered = [months[k] for k in sorted(months.keys(), reverse=True)[:3]]
        if not ordered:
            continue
        weights = [0.5, 0.3, 0.2][: len(ordered)]
        w_sum = sum(weights)
        result[jar_id] = int(round(sum(v * w for v, w in zip(ordered, weights, strict=True)) / w_sum))
    return result


def _build_calm_jar_note(
    kind: JarKind,
    available_cents: int,
    allocated_cents: int,
    pace_delta_pct: float,
    health: JarHealth,
) -> str:
    """Generate RSD-safe, shame-free, plain-English jar guidance."""
    avail_str = f"${abs(available_cents) / 100:,.2f}"
    if available_cents < 0:
        return f"Dipped {avail_str} over this cycle — totally fixable with a 1-click rebalance."
    if health == JarHealth.CALM:
        if kind == JarKind.BILLS:
            return f"Bills shield solid — {avail_str} set aside and protected."
        if kind == JarKind.FUN:
            return f"Guilt-free zone! {avail_str} left to enjoy without stress."
        if kind == JarKind.INVEST:
            return f"Trading & investing guardrail active: up to {avail_str} safe to deploy."
        if kind == JarKind.SAFETY:
            return f"Safety net growing nicely — {avail_str} reserved for peace of mind."
        return f"Cruising smoothly with {avail_str} remaining this month."
    if health == JarHealth.WATCH:
        return f"Moving a touch faster than the month ({pace_delta_pct:+.0f}% pace). {avail_str} left — easy to pace out."
    return f"Running tight ({avail_str} left). Tap Rebalance if you want a quick cushion."


@dataclass
class AutoBudgetResult:
    safe_to_spend: SafeToSpendBreakdown
    safe_to_invest_cents: int
    monthly_income_cents: int
    monthly_spent_cents: int
    smoothed_monthly_income_cents: int
    jars: List[JarState]
    rebalance_plans: List[RebalancePlan]
    upcoming_bills: List[RecurringBillOut]
    adaptive_allocations_by_jar_id: Dict[int, int]
    adaptive_target_updates: List[Dict[str, Any]]


class AutoBudgetEngine:
    """Deterministic self-updating auto-budget engine."""

    def compute(
        self,
        *,
        accounts: List[Dict[str, Any]],
        jars: List[Dict[str, Any]],
        transactions: List[Dict[str, Any]],
        recurring_bills: List[Dict[str, Any]],
        transfers: List[Dict[str, Any]],
        settings: Dict[str, Any],
        credit_lines: Optional[List[Dict[str, Any]]] = None,
        today: Optional[date] = None,
    ) -> AutoBudgetResult:
        ref = today or date.today()
        ym_now = current_cycle_ym(ref)
        _m_start, _m_end, cycle_progress_pct = _month_bounds(ref)

        # 1. Infer payday schedule
        payday = detect_payday_schedule(
            transactions=transactions,
            fallback_cadence_days=int(settings.get("payday_cadence_days", 14)),
            fallback_payday_date=settings.get("next_payday_date"),
            fallback_paycheck_cents=int(settings.get("expected_paycheck_cents", 220_000)),
            today=ref,
        )
        next_payday_dt = date.fromisoformat(payday.next_payday_date)
        days_until_payday = max(1, (next_payday_dt - ref).days)

        # 2. Income smoothing
        monthly_income_cents, smoothed_income_cents = _estimate_smoothed_monthly_income(
            transactions, payday, ref
        )
        # Ensure sensible floor if user hasn't added income yet (sum of jar targets)
        configured_total_targets = sum(int(j.get("monthly_target_cents", 0)) for j in jars)
        planning_income_cents = smoothed_income_cents if smoothed_income_cents > 0 else max(configured_total_targets, 100_000)

        # 3. Annotate recurring bills & compute unpaid bills until next payday
        upcoming_bills: List[RecurringBillOut] = []
        unpaid_bills_until_payday_cents = 0
        monthly_bills_obligation_cents = 0

        # Track bills already paid in last 7 days so we don't double-count a bill due today
        recent_paid_merchants = {
            t["normalized_merchant"]
            for t in transactions
            if t.get("tx_type") == "expense"
            and (ref - date.fromisoformat(t["tx_date"])).days <= 5
        }

        for b in recurring_bills:
            if not b.get("active", 1):
                continue
            due_dt = date.fromisoformat(b["next_due_date"])
            days_until_due = (due_dt - ref).days
            amt = int(b["amount_cents"])
            prev_amt = b.get("previous_amount_cents")
            hike = max(0, amt - int(prev_amt)) if prev_amt is not None else 0

            monthly_bills_obligation_cents += _bill_monthly_equivalent_cents(amt, b["cadence"])

            if due_dt <= next_payday_dt:
                # If due date is today or past-due and NOT just paid, reserve it
                if not (days_until_due <= 0 and b["normalized_merchant"] in recent_paid_merchants):
                    unpaid_bills_until_payday_cents += amt

            upcoming_bills.append(
                RecurringBillOut(
                    id=int(b["id"]),
                    name=b["name"],
                    normalized_merchant=b["normalized_merchant"],
                    amount_cents=amt,
                    previous_amount_cents=int(prev_amt) if prev_amt is not None else None,
                    price_hike_cents=hike,
                    cadence=RecurrenceCadence(b["cadence"]),
                    next_due_date=b["next_due_date"],
                    days_until_due=days_until_due,
                    jar_id=b.get("jar_id"),
                    is_subscription=bool(b.get("is_subscription", 0)),
                    is_essential=bool(b.get("is_essential", 1)),
                    auto_pay=bool(b.get("auto_pay", 0)),
                    active=bool(b.get("active", 1)),
                    confidence=float(b.get("confidence", 0.9)),
                    flagged_zombie=bool(b.get("flagged_zombie", 0)),
                )
            )
        upcoming_bills.sort(key=lambda x: (x.days_until_due, -x.amount_cents))

        # 3b. Shield upcoming debt/credit-line minimum payments due before next payday
        # (Skip credit cards already linked to a tracked `credit` account to avoid double-counting!)
        if credit_lines:
            from chronox_finance.engines.credit_debt import (
                compute_effective_min_payment,
                next_occurrence_of_day,
            )
            from chronox_finance.models import DebtKind

            linked_credit_acct_ids = {
                int(a["id"]) for a in accounts if a.get("account_type") == AccountType.CREDIT.value
            }
            for cl in credit_lines:
                if not cl.get("active", 1) or int(cl.get("balance_cents", 0)) <= 0:
                    continue
                if cl.get("account_id") is not None and int(cl["account_id"]) in linked_credit_acct_ids:
                    continue
                kind = DebtKind(cl.get("kind", DebtKind.CREDIT_CARD.value))
                min_pay = compute_effective_min_payment(
                    int(cl["balance_cents"]),
                    int(cl.get("min_payment_cents", 0)),
                    kind,
                    int(cl.get("apr_bps", 0)),
                )
                monthly_bills_obligation_cents += min_pay
                due_dt = next_occurrence_of_day(int(cl.get("payment_due_day", 10)), ref)
                if due_dt <= next_payday_dt:
                    unpaid_bills_until_payday_cents += min_pay

        # 4. Self-Updating Adaptive Jar Allocations (Bills-First + 3-Month EMA Blending)
        trailing_ema = _compute_trailing_jar_spend_ema(transactions, ref)
        adaptive_allocations: Dict[int, int] = {}
        adaptive_updates: List[Dict[str, Any]] = []

        bills_jar = next((j for j in jars if j["kind"] == JarKind.BILLS.value), None)
        remaining_income = planning_income_cents

        # Step 4a: Protect Bills & Essentials first
        if bills_jar:
            target_by_pct = int(round(planning_income_cents * (float(bills_jar["target_pct"]) / 100.0)))
            ema_bills = trailing_ema.get(int(bills_jar["id"]), 0)
            bills_need = max(monthly_bills_obligation_cents, target_by_pct, int(round(0.7 * target_by_pct + 0.3 * ema_bills)))
            bills_alloc = min(planning_income_cents, bills_need)
            adaptive_allocations[int(bills_jar["id"])] = bills_alloc
            remaining_income = max(0, planning_income_cents - bills_alloc)

        # Step 4b: Distribute remaining income across non-bills jars using normalized target_pct + gentle EMA nudge
        other_jars = [j for j in jars if not bills_jar or j["id"] != bills_jar["id"]]
        pct_sum = sum(max(0.0, float(j["target_pct"])) for j in other_jars) or 100.0
        raw_other: Dict[int, int] = {}
        for j in other_jars:
            jid = int(j["id"])
            share = max(0.0, float(j["target_pct"])) / pct_sum
            base_alloc = int(round(remaining_income * share))
            ema_spend = trailing_ema.get(jid)
            if ema_spend is not None and ema_spend > 0 and j["kind"] in (JarKind.DAILY.value, JarKind.FUN.value):
                # Gentle 25% adaptation toward reality, capped within [0.75x, 1.25x] of base target
                blended = int(round(0.75 * base_alloc + 0.25 * ema_spend))
                lo, hi = int(base_alloc * 0.75), int(base_alloc * 1.25)
                raw_other[jid] = max(lo, min(hi, blended))
            else:
                raw_other[jid] = base_alloc

        # Re-normalize non-bills jars so total allocations NEVER exceed planning_income_cents
        raw_other_sum = sum(raw_other.values())
        if raw_other_sum > 0 and remaining_income > 0:
            scale = remaining_income / raw_other_sum
            allocated_so_far = 0
            for idx, j in enumerate(other_jars):
                jid = int(j["id"])
                if idx == len(other_jars) - 1:
                    val = max(0, remaining_income - allocated_so_far)
                else:
                    val = int(round(raw_other[jid] * scale))
                    allocated_so_far += val
                adaptive_allocations[jid] = val
        else:
            for j in other_jars:
                adaptive_allocations[int(j["id"])] = 0

        for j in jars:
            jid = int(j["id"])
            prev_alloc = int(j.get("auto_allocated_cents", 0))
            new_alloc = adaptive_allocations.get(jid, prev_alloc)
            if abs(new_alloc - prev_alloc) >= 500:  # >= $5.00 shift worth surfacing
                adaptive_updates.append(
                    {
                        "jar_id": jid,
                        "jar_name": j["name"],
                        "previous_cents": prev_alloc,
                        "recommended_cents": new_alloc,
                        "delta_cents": new_alloc - prev_alloc,
                    }
                )

        # 5. Compute current-month spending & intra-cycle rebalance transfers per jar
        spent_by_jar: Dict[int, int] = defaultdict(int)
        monthly_spent_cents = 0
        for tx in transactions:
            if tx.get("tx_type") == "expense" and tx["tx_date"][:7] == ym_now:
                amt = int(tx["amount_cents"])
                monthly_spent_cents += amt
                if tx.get("jar_id") is not None:
                    spent_by_jar[int(tx["jar_id"])] += amt

        transfer_delta_by_jar: Dict[int, int] = defaultdict(int)
        for tr in transfers:
            amt = int(tr["amount_cents"])
            transfer_delta_by_jar[int(tr["from_jar_id"])] -= amt
            transfer_delta_by_jar[int(tr["to_jar_id"])] += amt

        # 6. Build JarState objects
        jar_states: List[JarState] = []
        for j in jars:
            jid = int(j["id"])
            kind = JarKind(j["kind"])
            alloc = adaptive_allocations.get(jid, int(j.get("monthly_target_cents", 0)))
            rollover = int(j.get("rollover_cents", 0)) if j.get("rollover_enabled", 1) else 0
            cap = j.get("rollover_cap_cents")
            if cap is not None and rollover > int(cap):
                rollover = int(cap)
            t_delta = transfer_delta_by_jar.get(jid, 0)
            effective_budget = max(0, alloc + rollover + t_delta)
            spent = spent_by_jar.get(jid, 0)
            available = (alloc + rollover + t_delta) - spent

            pct_used = round((spent / effective_budget * 100.0), 1) if effective_budget > 0 else (100.0 if spent > 0 else 0.0)
            pace_delta = round(pct_used - cycle_progress_pct, 1)

            if available < 0 or pct_used >= 95.0:
                health = JarHealth.REBALANCE
            elif pace_delta > 15.0 and kind in (JarKind.DAILY, JarKind.FUN):
                health = JarHealth.WATCH
            else:
                health = JarHealth.CALM

            jar_states.append(
                JarState(
                    id=jid,
                    slug=j["slug"],
                    name=j["name"],
                    kind=kind,
                    icon=j["icon"],
                    color=j["color"],
                    priority=int(j["priority"]),
                    target_pct=float(j["target_pct"]),
                    monthly_target_cents=int(j["monthly_target_cents"]),
                    auto_allocated_cents=alloc,
                    rollover_cents=rollover,
                    spent_cents=spent,
                    available_cents=available,
                    pct_used=min(999.0, pct_used),
                    pace_delta_pct=pace_delta,
                    health=health,
                    is_locked=bool(j.get("is_locked", 0)),
                    rollover_enabled=bool(j.get("rollover_enabled", 1)),
                    rollover_cap_cents=int(cap) if cap is not None else None,
                    calm_note=_build_calm_jar_note(kind, available, effective_budget, pace_delta, health),
                )
            )

        # 7. Real-time Safe-to-Spend Firewall Calculation
        total_liquid_cash_cents = 0
        credit_card_reserved_cents = 0
        for acct in accounts:
            atype = acct["account_type"]
            bal = int(acct["balance_cents"])
            if atype == AccountType.CREDIT.value:
                # Negative balance on credit card represents balance owed (reserve cash to pay in full!)
                if bal < 0:
                    credit_card_reserved_cents += abs(bal)
            elif acct.get("is_liquid", 1) and atype in (
                AccountType.CHECKING.value,
                AccountType.SAVINGS.value,
                AccountType.CASH.value,
            ):
                total_liquid_cash_cents += bal

        emergency_buffer_cents = int(settings.get("emergency_buffer_cents", 50_000))

        # Locked jar reserves (Safety + Invest available funds that are intentionally ring-fenced;
        # Bills are already accounted for via unpaid_bills_until_payday_cents so we don't double-subtract)
        locked_jar_reserves_cents = sum(
            max(0, js.available_cents)
            for js in jar_states
            if js.is_locked and js.kind in (JarKind.SAFETY, JarKind.INVEST)
        )

        # Raw Safe-to-Spend before clamping
        raw_safe_to_spend = (
            total_liquid_cash_cents
            - unpaid_bills_until_payday_cents
            - locked_jar_reserves_cents
            - emergency_buffer_cents
            - credit_card_reserved_cents
        )
        safe_to_spend_cents = max(0, raw_safe_to_spend)
        daily_safe_to_spend_cents = safe_to_spend_cents // days_until_payday

        safe_breakdown = SafeToSpendBreakdown(
            total_liquid_cash_cents=total_liquid_cash_cents,
            unpaid_bills_until_payday_cents=unpaid_bills_until_payday_cents,
            locked_jar_reserves_cents=locked_jar_reserves_cents,
            emergency_buffer_cents=emergency_buffer_cents,
            credit_card_reserved_cents=credit_card_reserved_cents,
            safe_to_spend_cents=safe_to_spend_cents,
            days_until_payday=days_until_payday,
            daily_safe_to_spend_cents=daily_safe_to_spend_cents,
            next_payday_date=payday.next_payday_date,
            estimated_paycheck_cents=payday.estimated_paycheck_cents,
            cycle_progress_pct=cycle_progress_pct,
        )

        # 8. ChronoX "Don't Trade Your Rent" Safe-to-Invest Capital Cap
        cash_after_essentials = (
            total_liquid_cash_cents
            - unpaid_bills_until_payday_cents
            - emergency_buffer_cents
            - credit_card_reserved_cents
        )
        invest_jar = next((js for js in jar_states if js.kind == JarKind.INVEST), None)
        bills_healthy = all(js.available_cents >= 0 for js in jar_states if js.kind == JarKind.BILLS)
        if cash_after_essentials <= 0 or not bills_healthy or not invest_jar:
            safe_to_invest_cents = 0
        else:
            safe_to_invest_cents = max(0, min(invest_jar.available_cents, cash_after_essentials))

        # 9. Zero-Guilt 1-Click Auto-Rebalance Plans for any negative jar
        rebalance_plans = self._build_rebalance_plans(jar_states)

        return AutoBudgetResult(
            safe_to_spend=safe_breakdown,
            safe_to_invest_cents=safe_to_invest_cents,
            monthly_income_cents=monthly_income_cents,
            monthly_spent_cents=monthly_spent_cents,
            smoothed_monthly_income_cents=smoothed_income_cents,
            jars=jar_states,
            rebalance_plans=rebalance_plans,
            upcoming_bills=upcoming_bills,
            adaptive_allocations_by_jar_id=adaptive_allocations,
            adaptive_target_updates=adaptive_updates,
        )

    def _build_rebalance_plans(self, jars: List[JarState]) -> List[RebalancePlan]:
        """Compute mathematically valid, non-shaming transfers to cover any over-budget jar."""
        deficits = [j for j in jars if j.available_cents < 0]
        if not deficits:
            return []

        # Pull from flexible surplus jars in safest order: Fun -> Invest -> Daily -> Safety (never raid Bills!)
        donor_priority = {
            JarKind.FUN: 1,
            JarKind.INVEST: 2,
            JarKind.DAILY: 3,
            JarKind.SAFETY: 4,
            JarKind.BILLS: 99,
        }
        donors = [
            {"jar": j, "surplus": j.available_cents}
            for j in sorted(jars, key=lambda x: donor_priority.get(x.kind, 50))
            if j.kind != JarKind.BILLS and j.available_cents > 0
        ]

        plans: List[RebalancePlan] = []
        for deficit_jar in deficits:
            needed = abs(deficit_jar.available_cents)
            for donor in donors:
                if needed <= 0:
                    break
                if donor["jar"].id == deficit_jar.id or donor["surplus"] <= 0:
                    continue
                move = min(needed, donor["surplus"])
                donor["surplus"] -= move
                needed -= move
                plans.append(
                    RebalancePlan(
                        from_jar_id=donor["jar"].id,
                        from_jar_name=donor["jar"].name,
                        to_jar_id=deficit_jar.id,
                        to_jar_name=deficit_jar.name,
                        amount_cents=move,
                        calm_message=(
                            f"No stress — shift ${move / 100:,.2f} from {donor['jar'].name} "
                            f"to bring {deficit_jar.name} back to green."
                        ),
                    )
                )
        return plans
