"""
Research-Backed Credit Score & Debt Optimization Engine ("Niche Credit Advisor").

Grounded in empirical FICO scoring mechanics and ADHD behavioral finance:
1. Per-Card AND Aggregate Revolving Utilization Tracking (FICO 30% weight):
   - Tiers: All-Zero Inactivity Penalty (0%), Optimal Low-Utilization Band (1%–8.9%),
     Good (9%–28.9%), Score Drag (29%–48.9%), High Drag (49%–88.9%), Maxed (89%+).
2. AZEO (All Zero Except One) Pre-Statement-Close Planner:
   - Targets reporting dates (`statement_closing_day`), NOT due dates!
   - Computes exact dollar paydowns 3 days before statement close so exactly ONE
     card reports 1%–4.9% and all other cards report $0.00.
3. Grace-Period & 0% Promo APR Cliff Protection:
   - Warns when carrying a balance forfeits grace period on new charges.
   - Calculates exact monthly payoff needed to beat expiring 0% promo APR cliffs.
4. Autopay Safety Audit & Average Age of Accounts (AAoA) Oldest-Card Shield.
5. 4-Strategy Exact Integer-Cent Debt Payoff Comparison:
   - ADHD Hybrid (Snowball smallest 1–2 clutter accounts -> Avalanche high APR)
   - Credit-First (Fix >29% utilization credit killers first -> Avalanche)
   - Avalanche (Lowest total interest paid)
   - Snowball (Fastest account closures & fastest DTI drop)
6. Debt-to-Income (DTI) Lender Benchmark & Balance Transfer Breakeven Calculator.
"""

from __future__ import annotations

import calendar
import math
import re
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from chronox_finance.models import (
    AutopayMode,
    AZEOAction,
    BalanceTransferRequest,
    BalanceTransferResult,
    BridgeActionStepOut,
    CardPaymentTierOut,
    CollectionPlanOut,
    CreditAdvisorAlert,
    CreditDerogatoryOut,
    CreditFactorBreakdown,
    CreditFactorItem,
    CreditInquiryOut,
    CreditLineOut,
    CreditMissionRoadmap,
    CreditOptimizationReport,
    CreditReportOut,
    DebtKind,
    DebtStrategyResult,
    DerogatoryResolutionOut,
    DragUtilizationBridge,
    EwaAppSummaryOut,
    FiveTwentyFourStatus,
    OptimalCardPaymentPlan,
    RoadmapBranchOut,
    RoadmapStageOut,
    UtilizationMilestoneOut,
    WishlistUnlockOut,
    utc_now_iso,
)

MAX_AMORTIZATION_MONTHS = 360  # 30-year safety cap
FACTOR_ANALYSIS_DISCLAIMER = (
    "Weighted factor analysis from tracked accounts, inquiries, and history — not an official bureau score."
)
FICO_UTILIZATION_CLIFFS: Tuple[float, ...] = (1.0, 9.0, 29.0, 49.0, 69.0, 89.0)


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


def _get_field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def compute_derogatory_drop_off(kind: str, opened_date_iso: str) -> str:
    """
    Compute statutory FCRA credit report drop-off date based on derogatory kind:
    - Chapter 7 Bankruptcy (`bankruptcy_ch7`): +10 years (+120 months)
    - Late payments (`late_30/60/90`), Charge-Offs (`charge_off`), Collections (`collection`),
      Chapter 13 Bankruptcy (`bankruptcy_ch13`), and other derogatories: +7 years (+84 months)
    """
    dt = date.fromisoformat(str(opened_date_iso)[:10])
    months = 120 if str(kind).strip().lower() == "bankruptcy_ch7" else 84
    return _add_months_date(dt, months).isoformat()


def compute_inquiry_dates(inquiry_date_iso: str) -> Tuple[str, str]:
    """Return `(counts_until (+12m scoring), drops_off (+24m report))` for a hard inquiry."""
    dt = date.fromisoformat(str(inquiry_date_iso)[:10])
    counts_until = _add_months_date(dt, 12).isoformat()
    drops_off = _add_months_date(dt, 24).isoformat()
    return counts_until, drops_off


def _normalize_credit_reports(raw_reports: Optional[Sequence[Any]]) -> List[CreditReportOut]:
    out: List[CreditReportOut] = []
    for idx, r in enumerate(raw_reports or (), start=1):
        if isinstance(r, CreditReportOut):
            out.append(r)
            continue
        out.append(
            CreditReportOut(
                id=int(_get_field(r, "id", idx) or idx),
                bureau=str(_get_field(r, "bureau", "other")),  # type: ignore[arg-type]
                model=str(_get_field(r, "model", "fico8")),  # type: ignore[arg-type]
                score=int(_get_field(r, "score", 300)),
                pulled_date=str(_get_field(r, "pulled_date", date.today().isoformat()))[:10],
                source=str(_get_field(r, "source", "manual")),
                notes=str(_get_field(r, "notes", "") or ""),
                created_at=str(_get_field(r, "created_at", "") or ""),
            )
        )
    out.sort(key=lambda x: (x.pulled_date, x.id), reverse=True)
    return out


def _normalize_credit_inquiries(
    raw_inquiries: Optional[Sequence[Any]],
    ref: date,
) -> List[CreditInquiryOut]:
    out: List[CreditInquiryOut] = []
    for idx, q in enumerate(raw_inquiries or (), start=1):
        inq_date = str(_get_field(q, "inquiry_date", ""))[:10]
        if not inq_date:
            continue
        try:
            default_counts, default_drops = compute_inquiry_dates(inq_date)
        except ValueError:
            continue
        counts_until = str(_get_field(q, "counts_until", None) or default_counts)[:10]
        drops_off = str(_get_field(q, "drops_off", None) or default_drops)[:10]
        try:
            scoring_active = ref <= date.fromisoformat(counts_until)
        except ValueError:
            scoring_active = False
        out.append(
            CreditInquiryOut(
                id=int(_get_field(q, "id", idx) or idx),
                lender=str(_get_field(q, "lender", "Unknown Lender")),
                bureau=str(_get_field(q, "bureau", "experian")),  # type: ignore[arg-type]
                inquiry_date=inq_date,
                counts_until=counts_until,
                drops_off=drops_off,
                is_scoring_active=scoring_active,
                created_at=str(_get_field(q, "created_at", "") or ""),
            )
        )
    out.sort(key=lambda x: x.inquiry_date, reverse=True)
    return out


def _normalize_credit_derogatories(
    raw_derogs: Optional[Sequence[Any]],
) -> List[CreditDerogatoryOut]:
    out: List[CreditDerogatoryOut] = []
    for idx, d in enumerate(raw_derogs or (), start=1):
        kind = str(_get_field(d, "kind", "other"))
        opened = str(_get_field(d, "opened_date", ""))[:10]
        if not opened:
            continue
        try:
            default_drop = compute_derogatory_drop_off(kind, opened)
        except ValueError:
            continue
        drops_off = str(_get_field(d, "drops_off", None) or default_drop)[:10]
        out.append(
            CreditDerogatoryOut(
                id=int(_get_field(d, "id", idx) or idx),
                kind=kind,  # type: ignore[arg-type]
                lender=str(_get_field(d, "lender", "") or ""),
                opened_date=opened,
                drops_off=drops_off,
                active=bool(_get_field(d, "active", True)),
                notes=str(_get_field(d, "notes", "") or ""),
                created_at=str(_get_field(d, "created_at", "") or ""),
            )
        )
    out.sort(key=lambda x: x.drops_off)
    return out


def evaluate_five_twenty_four(
    *,
    credit_lines: Sequence[CreditLineOut],
    inquiries: Sequence[CreditInquiryOut] = (),
    include_authorized_user: bool = True,
    today: Optional[date] = None,
) -> FiveTwentyFourStatus:
    """
    Compute Chase 5/24 status & issuer inquiry velocity across ALL issuers.

    - Counts personal revolving accounts (`DebtKind.CREDIT_CARD`) opened in the
      trailing 24 months across all issuers.
    - Respects `include_authorized_user` toggle (`True` by default, matching Chase's
      automated underwriting behavior; toggle `False` to model AU recon exclusion).
    - Tracks trailing 6-month and 12-month hard inquiry velocity + Chase 2/30 guard.
    """
    ref = today or date.today()
    counted: List[Dict[str, Any]] = []
    opened_last_30d = 0
    au_counted = 0

    for line in credit_lines:
        if line.kind != DebtKind.CREDIT_CARD or not line.opened_date:
            continue
        if line.is_authorized_user and not include_authorized_user:
            continue
        try:
            open_dt = date.fromisoformat(line.opened_date[:10])
        except ValueError:
            continue
        if open_dt > ref:
            continue
        drop_dt = _add_months_date(open_dt, 24)
        if drop_dt > ref:
            if line.is_authorized_user:
                au_counted += 1
            if (ref - open_dt).days <= 30:
                opened_last_30d += 1
            counted.append(
                {
                    "credit_line_id": line.id,
                    "name": line.name,
                    "issuer": line.issuer,
                    "opened_date": line.opened_date[:10],
                    "is_authorized_user": line.is_authorized_user,
                    "drops_from_5_24_on": drop_dt.isoformat(),
                }
            )

    counted.sort(key=lambda x: (x["opened_date"], x["credit_line_id"]))
    count_5_24 = len(counted)
    under_5_24 = count_5_24 < 5
    next_slot_drop = counted[0]["drops_from_5_24_on"] if counted else None

    inquiries_6m = 0
    inquiries_12m = 0
    for inq in inquiries:
        try:
            inq_dt = date.fromisoformat(inq.inquiry_date[:10])
        except ValueError:
            continue
        age_days = (ref - inq_dt).days
        if 0 <= age_days <= 183:
            inquiries_6m += 1
        if inq.is_scoring_active and age_days >= 0:
            inquiries_12m += 1

    warnings: List[str] = []
    if count_5_24 >= 5:
        warnings.append(
            f"At {count_5_24}/24 — Chase 5/24 velocity cap active (next slot drops on {next_slot_drop})."
        )
    elif count_5_24 == 4:
        warnings.append(
            f"At 4/24 — only 1 Chase 5/24 slot remaining before lock-out (oldest slot drops {next_slot_drop})."
        )
    if au_counted > 0 and include_authorized_user:
        warnings.append(
            f"Includes {au_counted} Authorized User account(s) in 5/24 count — toggle AU off to preview Chase reconsideration."
        )
    if opened_last_30d >= 2:
        warnings.append(
            f"2/30 issuer velocity warning: {opened_last_30d} revolving cards opened within the last 30 days."
        )
    if inquiries_6m >= 3:
        warnings.append(
            f"High recent inquiry velocity ({inquiries_6m} hard inquiries in 6m / {inquiries_12m} in 12m) — pause new applications."
        )

    return FiveTwentyFourStatus(
        count_5_24=count_5_24,
        under_5_24=under_5_24,
        include_authorized_user=include_authorized_user,
        counted_accounts=counted,
        next_slot_drop_date=next_slot_drop,
        inquiries_6m=inquiries_6m,
        inquiries_12m=inquiries_12m,
        velocity_warnings=warnings,
    )


def _util_cliff_band(util_pct: float) -> int:
    """Map utilization percentage to FICO cliff band index (0=0%, 1=<9%, 2=<29%, 3=<49%, 4=<69%, 5=<89%, 6=89%+)."""
    if util_pct <= 0.0:
        return 0
    if util_pct < 9.0:
        return 1
    if util_pct < 29.0:
        return 2
    if util_pct < 49.0:
        return 3
    if util_pct < 69.0:
        return 4
    if util_pct < 89.0:
        return 5
    return 6


class CreditFactorAnalyzer:
    """
    Deterministic 5-Factor Credit Analysis (FICO 8 + VantageScore 3.0 weights).

    NEVER computes or displays a fabricated bureau score or score-point delta estimate.
    Pairs imported actual bureau scores (`credit_reports`) with factual factor health
    ratings, utilization threshold positions, and statutory FCRA drop-off timelines.
    """

    def analyze(
        self,
        *,
        credit_lines: Sequence[CreditLineOut],
        aggregate_utilization_pct: float,
        all_zero_penalty_risk: bool,
        avg_account_age_months: Optional[float],
        oldest_account_name: Optional[str],
        autopay_missing_count: int,
        five_twenty_four: FiveTwentyFourStatus,
        credit_reports: Sequence[CreditReportOut] = (),
        credit_inquiries: Sequence[CreditInquiryOut] = (),
        credit_derogatories: Sequence[CreditDerogatoryOut] = (),
        today: Optional[date] = None,
    ) -> CreditFactorBreakdown:
        ref = today or date.today()

        # Keep latest actual imported score per (bureau, model)
        latest_by_key: Dict[Tuple[str, str], CreditReportOut] = {}
        for rep in sorted(credit_reports, key=lambda r: (r.pulled_date, r.id), reverse=True):
            key = (rep.bureau, rep.model)
            if key not in latest_by_key:
                latest_by_key[key] = rep
        latest_actual_scores = list(latest_by_key.values())

        # Active derogatories (not yet past FCRA drops_off date)
        active_derogs = [
            d
            for d in credit_derogatories
            if d.active and d.drops_off > ref.isoformat()
        ]
        severe_derogs = [
            d
            for d in active_derogs
            if d.kind in ("late_90", "charge_off", "collection", "bankruptcy_ch7", "bankruptcy_ch13")
        ]

        cards = [c for c in credit_lines if c.kind == DebtKind.CREDIT_CARD]
        installments = [c for c in credit_lines if c.kind != DebtKind.CREDIT_CARD]
        max_card_util = max(((c.utilization_pct or 0.0) for c in cards), default=0.0)
        cards_with_bal = sum(1 for c in cards if c.balance_cents > 0)
        total_limit = sum(c.credit_limit_cents for c in cards)
        total_bal = sum(c.balance_cents for c in cards)
        available_credit = max(0, total_limit - total_bal)
        oldest_months = max(((c.age_months or 0) for c in credit_lines), default=0)

        # --- 1. Payment History (FICO 35% / Vantage 40%) ---
        if severe_derogs:
            pay_status = "drag"
            earliest_drop = min(d.drops_off for d in severe_derogs)
            pay_summary = (
                f"{len(active_derogs)} active derogatory mark(s) including severe item(s); "
                f"next statutory drop-off on {earliest_drop}."
            )
            pay_guidance = "FCRA statutory drop-off applies (+7y / +10y Ch. 7)"
        elif active_derogs:
            pay_status = "fair"
            earliest_drop = min(d.drops_off for d in active_derogs)
            pay_summary = (
                f"{len(active_derogs)} active late-payment mark(s); drops off {earliest_drop} (+7y FCRA rule)."
            )
            pay_guidance = "FCRA 7-year reporting window"
        elif autopay_missing_count > 0:
            pay_status = "good"
            pay_summary = (
                f"0 active derogatories, but {autopay_missing_count} account(s) lack autopay protection."
            )
            pay_guidance = "Enable autopay on all open accounts"
        else:
            pay_status = "optimal"
            pay_summary = "0 active derogatories and autopay shields active across tracked accounts."
            pay_guidance = "All tracked accounts protected by autopay"

        # --- 2. Utilization / AZEO Thresholds (FICO 30% / Vantage 20%) ---
        agg_band = _util_cliff_band(aggregate_utilization_pct)
        max_band = _util_cliff_band(max_card_util)
        worst_band = max(agg_band, max_band)

        if all_zero_penalty_risk:
            util_status = "good"
            util_summary = (
                "0.0% aggregate utilization across all cards triggers the All-Zero reporting penalty."
            )
            util_guidance = "Report 1%–4.9% on 1 primary card (AZEO)"
        elif worst_band <= 1:
            util_status = "optimal"
            util_summary = (
                f"{aggregate_utilization_pct:.1f}% aggregate util (max card {max_card_util:.1f}%) — "
                f"inside the 1%–8.9% AZEO target band ({cards_with_bal}/{len(cards)} cards reporting)."
            )
            util_guidance = "Target band maintained (1%–8.9%)"
        elif worst_band == 2:
            util_status = "good"
            util_summary = (
                f"{aggregate_utilization_pct:.1f}% aggregate util (max card {max_card_util:.1f}%) — "
                f"below 29% threshold, room to optimize under 8.9% via AZEO."
            )
            util_guidance = "Pay down before statement close to reach <8.9%"
        elif worst_band == 3:
            util_status = "fair"
            util_summary = (
                f"{aggregate_utilization_pct:.1f}% aggregate util (max card {max_card_util:.1f}%) — "
                f"above the 29% utilization threshold."
            )
            util_guidance = "Pay below 28.9% before statement close"
        else:
            util_status = "drag"
            util_summary = (
                f"{aggregate_utilization_pct:.1f}% aggregate util (max card {max_card_util:.1f}%) — "
                f"elevated utilization band (49%/69%/89% thresholds)."
            )
            util_guidance = "Prioritize pre-close paydown below 28.9% / 8.9%"

        # --- 3. Length of Credit History: AAoA / AoOA (FICO 15%) ---
        aaoa = avg_account_age_months or 0.0
        if aaoa >= 84.0:
            age_status = "optimal"
            age_guidance = "7+ yr average account age"
        elif aaoa >= 48.0:
            age_status = "good"
            age_guidance = "4+ yr average account age — keep anchor open"
        elif aaoa >= 24.0:
            age_status = "fair"
            age_guidance = "Avoid closing oldest fee-free account"
        else:
            age_status = "drag"
            age_guidance = "Thin history — preserve oldest line and limit new openings"
        age_summary = (
            f"AAoA {aaoa:.1f} mo · AoOA {oldest_months} mo"
            + (f" (anchor: {oldest_account_name})." if oldest_account_name else ".")
        )

        # --- 4. New Credit & Hard Inquiries (FICO 10% / Vantage 5%) ---
        inq_12m = five_twenty_four.inquiries_12m
        inq_24m = sum(1 for q in credit_inquiries if q.drops_off > ref.isoformat())
        if inq_12m <= 1 and five_twenty_four.count_5_24 <= 2:
            new_status = "optimal"
            new_guidance = "Low inquiry & new-account velocity"
        elif inq_12m <= 3 and five_twenty_four.count_5_24 <= 4:
            new_status = "good"
            new_guidance = "Inquiries stop scoring at 12m, drop off at 24m"
        elif inq_12m <= 5 or five_twenty_four.count_5_24 >= 5:
            new_status = "fair"
            new_guidance = "Elevated velocity — pause new applications"
        else:
            new_status = "drag"
            new_guidance = "High velocity — pause new applications until inquiries age past 12m"
        new_summary = (
            f"{inq_12m} scoring inquiries (<12m), {inq_24m} on report (<24m), "
            f"Chase velocity {five_twenty_four.count_5_24}/24."
        )

        # --- 5. Credit Mix (FICO 10%) ---
        has_rev = len(cards) > 0
        has_inst = len(installments) > 0
        if has_rev and has_inst:
            mix_status = "optimal"
            mix_summary = f"Balanced mix: {len(cards)} revolving card(s) + {len(installments)} installment account(s)."
            mix_guidance = "Revolving + installment tradelines present"
        elif len(credit_lines) >= 2:
            mix_status = "good"
            mix_summary = (
                f"{len(cards)} revolving card(s), {len(installments)} installment account(s) — "
                f"solid profile (never take on interest just for mix)."
            )
            mix_guidance = "Never borrow at interest solely for credit mix"
        else:
            mix_status = "fair"
            mix_summary = f"{len(credit_lines)} tracked tradeline(s); thin file mix."
            mix_guidance = "Thin tradeline count"

        fico8_factors = [
            CreditFactorItem(
                factor="payment_history",
                label="Payment History",
                weight_pct=35,
                status=pay_status,  # type: ignore[arg-type]
                summary=pay_summary,
                guidance=pay_guidance,
            ),
            CreditFactorItem(
                factor="utilization",
                label="Amounts Owed / Utilization (AZEO)",
                weight_pct=30,
                status=util_status,  # type: ignore[arg-type]
                summary=util_summary,
                guidance=util_guidance,
            ),
            CreditFactorItem(
                factor="credit_age",
                label="Length of Credit History (AAoA / AoOA)",
                weight_pct=15,
                status=age_status,  # type: ignore[arg-type]
                summary=age_summary,
                guidance=age_guidance,
            ),
            CreditFactorItem(
                factor="new_credit",
                label="New Credit & Hard Inquiries",
                weight_pct=10,
                status=new_status,  # type: ignore[arg-type]
                summary=new_summary,
                guidance=new_guidance,
            ),
            CreditFactorItem(
                factor="credit_mix",
                label="Credit Mix",
                weight_pct=10,
                status=mix_status,  # type: ignore[arg-type]
                summary=mix_summary,
                guidance=mix_guidance,
            ),
        ]

        # VantageScore 3.0 published factor weights (40 + 21 + 20 + 11 + 5 + 3 = 100%)
        depth_status = "optimal" if (age_status == "optimal" and mix_status == "optimal") else (
            "good" if age_status in ("optimal", "good") else age_status
        )
        total_debt_all = sum(c.balance_cents for c in credit_lines)
        bal_status = "optimal" if total_debt_all == 0 else ("good" if total_bal < 250_000 else ("fair" if total_bal < 1_000_000 else "drag"))
        avail_status = "optimal" if available_credit >= 1_000_000 else ("good" if available_credit >= 300_000 else "fair")

        vantage3_factors = [
            CreditFactorItem(
                factor="payment_history",
                label="Payment History",
                weight_pct=40,
                status=pay_status,  # type: ignore[arg-type]
                summary=pay_summary,
                guidance=pay_guidance,
            ),
            CreditFactorItem(
                factor="depth_of_credit",
                label="Age & Type of Credit (Depth)",
                weight_pct=21,
                status=depth_status,  # type: ignore[arg-type]
                summary=f"{age_summary} {mix_summary}",
                guidance=age_guidance,
            ),
            CreditFactorItem(
                factor="utilization",
                label="Credit Utilization",
                weight_pct=20,
                status=util_status,  # type: ignore[arg-type]
                summary=util_summary,
                guidance=util_guidance,
            ),
            CreditFactorItem(
                factor="balances",
                label="Total Balances",
                weight_pct=11,
                status=bal_status,  # type: ignore[arg-type]
                summary=f"${total_debt_all / 100:,.2f} total debt (${total_bal / 100:,.2f} revolving).",
                guidance="Lower revolving balances reduce utilization & balance load",
            ),
            CreditFactorItem(
                factor="recent_credit",
                label="Recent Credit & Inquiries",
                weight_pct=5,
                status=new_status,  # type: ignore[arg-type]
                summary=new_summary,
                guidance=new_guidance,
            ),
            CreditFactorItem(
                factor="available_credit",
                label="Available Credit",
                weight_pct=3,
                status=avail_status,  # type: ignore[arg-type]
                summary=f"${available_credit / 100:,.2f} unused revolving credit across {len(cards)} card(s).",
                guidance="Unused revolving credit headroom",
            ),
        ]

        return CreditFactorBreakdown(
            disclaimer=FACTOR_ANALYSIS_DISCLAIMER,
            latest_actual_scores=latest_actual_scores,
            fico8_factors=fico8_factors,
            vantage3_factors=vantage3_factors,
        )


class CreditDebtOptimizer:
    """Deterministic expert credit & debt optimization engine."""

    def analyze(
        self,
        *,
        raw_lines: List[Dict[str, Any]],
        smoothed_monthly_income_cents: int,
        extra_monthly_budget_cents: int = 25_000,
        credit_reports: Optional[Sequence[Any]] = None,
        credit_inquiries: Optional[Sequence[Any]] = None,
        credit_derogatories: Optional[Sequence[Any]] = None,
        transactions: Optional[Sequence[Any]] = None,
        recurring_bills: Optional[Sequence[Any]] = None,
        goals: Optional[Sequence[Any]] = None,
        payday_cadence_days: int = 14,
        selected_branch: str = "balanced_mission",
        lookback_days: int = 365,
        include_authorized_user: bool = True,
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
                    is_authorized_user=bool(r.get("is_authorized_user", 0)),
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

        autopay_missing = sum(
            1
            for d in enriched
            if d.autopay_mode == AutopayMode.NONE and (d.balance_cents > 0 or d.kind == DebtKind.CREDIT_CARD)
        )

        # Normalize bureau reports, inquiries, derogatories & compute 5-factor analysis + 5/24
        norm_reports = _normalize_credit_reports(credit_reports)
        norm_inquiries = _normalize_credit_inquiries(credit_inquiries, ref)
        norm_derogs = _normalize_credit_derogatories(credit_derogatories)

        five_twenty_four = evaluate_five_twenty_four(
            credit_lines=enriched,
            inquiries=norm_inquiries,
            include_authorized_user=include_authorized_user,
            today=ref,
        )
        factor_analysis = CreditFactorAnalyzer().analyze(
            credit_lines=enriched,
            aggregate_utilization_pct=agg_util,
            all_zero_penalty_risk=all_zero_risk,
            avg_account_age_months=avg_age,
            oldest_account_name=oldest_name,
            autopay_missing_count=autopay_missing,
            five_twenty_four=five_twenty_four,
            credit_reports=norm_reports,
            credit_inquiries=norm_inquiries,
            credit_derogatories=norm_derogs,
            today=ref,
        )

        azeo_plan = self._build_azeo_plan(cards, total_rev_lim, ref)
        strategies, rec_strategy = self._compare_all_strategies(
            enriched, max(0, int(extra_monthly_budget_cents)), ref
        )
        alerts = self._build_expert_alerts(
            enriched=enriched,
            cards=cards,
            agg_util=agg_util,
            all_zero_risk=all_zero_risk,
            dti_pct=dti_pct,
            oldest_name=oldest_name,
            five_twenty_four=five_twenty_four,
        )

        drag_bridge = analyze_financial_drag_bridge(
            credit_lines=enriched,
            transactions=transactions or (),
            derogatories=norm_derogs,
            extra_monthly_budget_cents=max(0, int(extra_monthly_budget_cents)),
            lookback_days=lookback_days,
            today=ref,
        )
        mission_roadmap = build_credit_and_wishlist_roadmap(
            credit_lines=enriched,
            transactions=transactions or (),
            recurring_bills=recurring_bills or (),
            goals=goals or (),
            credit_reports=norm_reports,
            derogatories=norm_derogs,
            smoothed_monthly_income_cents=smoothed_monthly_income_cents,
            payday_cadence_days=payday_cadence_days,
            extra_monthly_budget_cents=max(0, int(extra_monthly_budget_cents)),
            selected_branch=selected_branch,
            lookback_days=lookback_days,
            drag_bridge=drag_bridge,
            today=ref,
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
            factor_analysis=factor_analysis,
            five_twenty_four=five_twenty_four,
            drag_bridge=drag_bridge,
            mission_roadmap=mission_roadmap,
            credit_reports=norm_reports,
            credit_inquiries=norm_inquiries,
            credit_derogatories=norm_derogs,
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
                                f"to report ${target_primary / 100:,.2f} ({(target_primary / card.credit_limit_cents) * 100:.1f}% util — <8.9% AZEO target band)."
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

    def _amortize_strategy(
        self,
        debts: List[CreditLineOut],
        extra_monthly_cents: int,
        mode: str,
        today: date,
    ) -> Tuple[int, int, int, str, List[str]]:
        """
        Exact integer-cent month-by-month amortization schedule.
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
        while any(d["bal"] > 0 for d in active) and months < MAX_AMORTIZATION_MONTHS:
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

    def _compare_all_strategies(
        self,
        debts: List[CreditLineOut],
        extra_monthly_cents: int,
        today: date,
    ) -> Tuple[List[DebtStrategyResult], str]:
        nonzero = [d for d in debts if d.balance_cents > 0]
        if not nonzero:
            return [], "debt_free"

        # Baseline: minimum payments only (0 extra)
        _m0, base_interest, _w0, _t0, _a0 = self._amortize_strategy(nonzero, 0, "avalanche", today)

        has_credit_killer = any(
            d.kind == DebtKind.CREDIT_CARD and (d.utilization_pct or 0.0) >= 29.0 for d in nonzero
        )
        recommended = "credit_first" if has_credit_killer else ("adhd_hybrid" if len(nonzero) >= 2 else "avalanche")

        specs = [
            ("adhd_hybrid", "ADHD Hybrid (Dopamine + Math)", "Clears 1–2 small clutter debts fast, then crushes highest APR."),
            ("credit_first", "Credit-First Utilization Priority", "Drops >29% cards below utilization threshold first, then Avalanche."),
            ("avalanche", "Debt Avalanche (Lowest Interest)", "Targets highest APR first for mathematical minimum interest."),
            ("snowball", "Debt Snowball (Fastest Wins)", "Targets smallest balance first for rapid closures & DTI drop."),
        ]

        results: List[DebtStrategyResult] = []
        for key, label, tagline in specs:
            months, interest, first_win, first_target, m1 = self._amortize_strategy(
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
        five_twenty_four: Optional[FiveTwentyFourStatus] = None,
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
                            f"{d.name} has autopay OFF. A missed 30-day payment remains on bureau credit reports "
                            f"for 7 years—automating at least the minimum payment eliminates missed-due-date risk."
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
                        "All your credit cards are reporting $0.00. Scoring models penalize all-zero revolving reporting "
                        "when every card reports $0; letting 1 primary card report 1%–4.9% (AZEO) avoids the all-zero penalty."
                    ),
                    micro_step="Put one small recurring bill (e.g. $10–$25) on your primary card and autopay statement balance.",
                )
            )
        elif agg_util >= 9.0 and agg_util < 29.0:
            alerts.append(
                CreditAdvisorAlert(
                    severity="info",
                    category="azeo_optimization",
                    title=f"Optimize Aggregate Utilization ({agg_util:.1f}% → <8.9%)",
                    detail=(
                        "Under 30% prevents severe drag, while the lowest utilization band is between 1% and 8.9%. "
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

        # 5b. Chase 5/24 & Inquiry Velocity Guard
        if five_twenty_four is not None:
            if five_twenty_four.count_5_24 >= 4:
                drop_hint = (
                    f" Next slot drops on {five_twenty_four.next_slot_drop_date}."
                    if five_twenty_four.next_slot_drop_date
                    else ""
                )
                alerts.append(
                    CreditAdvisorAlert(
                        severity="high" if five_twenty_four.count_5_24 >= 5 else "medium",
                        category="five_twenty_four",
                        title=f"Chase 5/24 Velocity at {five_twenty_four.count_5_24}/24",
                        detail=(
                            f"You have opened {five_twenty_four.count_5_24} personal revolving card(s) across all issuers "
                            f"in the trailing 24 months.{drop_hint} Avoid opening new store/promo cards right now."
                        ),
                        micro_step=(
                            f"Pause new card applications until {five_twenty_four.next_slot_drop_date or 'slots age out'} "
                            f"to preserve Chase/issuer eligibility."
                        ),
                    )
                )
            if five_twenty_four.inquiries_6m >= 3:
                alerts.append(
                    CreditAdvisorAlert(
                        severity="medium",
                        category="inquiry_velocity",
                        title=f"Elevated Hard Inquiry Velocity ({five_twenty_four.inquiries_6m} in 6m)",
                        detail=(
                            f"{five_twenty_four.inquiries_6m} hard inquiries in 6 months "
                            f"({five_twenty_four.inquiries_12m} scoring active in 12m). "
                            f"High velocity triggers issuer denials and short-term score drag."
                        ),
                        micro_step="Garden existing cards and wait 6 months before applying for new credit.",
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

        return alerts[:10]

    def evaluate_balance_transfer(self, req: BalanceTransferRequest) -> BalanceTransferResult:
        """
        Exact amortization breakeven analysis for a 0% balance transfer offer.

        Explicitly detects zero/negative amortization (`monthly_payment_cents <= monthly_interest`)
        instead of inventing arbitrary penalties, never recommends continuing non-amortizing
        payments, and models an independent `post_promo_apr_bps` after the 0% window expires.
        """
        fee_cents = int(round(req.balance_cents * (req.transfer_fee_bps / 10000.0)))
        post_promo_apr_bps = (
            int(req.post_promo_apr_bps)
            if req.post_promo_apr_bps is not None
            else int(req.current_apr_bps)
        )
        req_monthly_clear = int(math.ceil((req.balance_cents + fee_cents) / req.promo_months))

        first_mo_interest_a = int(round(req.balance_cents * (req.current_apr_bps / 10000.0) / 12.0))
        min_amortizing_cents = (
            first_mo_interest_a + max(100, int(math.ceil(req.balance_cents * 0.01)))
            if req.current_apr_bps > 0
            else max(100, int(math.ceil(req.balance_cents / max(1, MAX_AMORTIZATION_MONTHS))))
        )

        # Scenario A: Keep current card at current_apr_bps
        bal_a = req.balance_cents
        int_a = 0
        m_a = 0
        current_payoff_possible = True
        if req.current_apr_bps > 0 and req.monthly_payment_cents <= first_mo_interest_a:
            current_payoff_possible = False
            # Bounded 60-month interest burn purely for factual display (never used as a fake finite payoff)
            sim_bal = req.balance_cents
            for _ in range(60):
                mo_int = int(round(sim_bal * (req.current_apr_bps / 10000.0) / 12.0))
                int_a += mo_int
                sim_bal = max(0, sim_bal + mo_int - req.monthly_payment_cents)
        else:
            while bal_a > 0 and m_a < MAX_AMORTIZATION_MONTHS:
                m_a += 1
                interest = int(round(bal_a * (req.current_apr_bps / 10000.0) / 12.0))
                if req.monthly_payment_cents <= interest:
                    current_payoff_possible = False
                    break
                bal_a += interest
                int_a += interest
                bal_a = max(0, bal_a - req.monthly_payment_cents)
            if bal_a > 0:
                current_payoff_possible = False

        # Scenario B: Transfer balance + fee at 0% for promo_months, then post_promo_apr_bps thereafter
        bal_b = req.balance_cents + fee_cents
        int_b = 0
        m_b = 0
        transfer_payoff_possible = True
        while bal_b > 0 and m_b < MAX_AMORTIZATION_MONTHS:
            m_b += 1
            if m_b > req.promo_months and post_promo_apr_bps > 0:
                interest = int(round(bal_b * (post_promo_apr_bps / 10000.0) / 12.0))
                if req.monthly_payment_cents <= interest:
                    transfer_payoff_possible = False
                    # Record remaining post-promo interest up to 60 total months for factual reporting
                    rem_months = max(0, 60 - m_b + 1)
                    sim_b = bal_b
                    for _ in range(rem_months):
                        mo_int_b = int(round(sim_b * (post_promo_apr_bps / 10000.0) / 12.0))
                        int_b += mo_int_b
                        sim_b = max(0, sim_b + mo_int_b - req.monthly_payment_cents)
                    break
                bal_b += interest
                int_b += interest
            bal_b = max(0, bal_b - req.monthly_payment_cents)
        if bal_b > 0 and transfer_payoff_possible:
            transfer_payoff_possible = False

        clears_promo = transfer_payoff_possible and m_b <= req.promo_months
        total_cost_b = fee_cents + int_b

        if not current_payoff_possible and not transfer_payoff_possible:
            verdict = "skip"
            net_savings = 0
            expl = (
                f"Payment warning: ${req.monthly_payment_cents / 100:,.2f}/mo does not amortize principal "
                f"(current monthly interest is ${first_mo_interest_a / 100:,.2f}/mo, and post-promo interest at "
                f"{post_promo_apr_bps / 100:.2f}% APR also exceeds ${req.monthly_payment_cents / 100:,.2f}/mo). "
                f"Increase your monthly payment to at least ${min_amortizing_cents / 100:,.2f}/mo "
                f"(or ${req_monthly_clear / 100:,.2f}/mo to clear inside the {req.promo_months}-month promo) before transferring."
            )
        elif not current_payoff_possible and transfer_payoff_possible:
            # Current card never pays down principal at this payment, whereas 0% promo reduces principal enough to amortize!
            if m_b > 60 and req.current_apr_bps > 0:
                int_a = 0
                sim_bal = req.balance_cents
                for _ in range(m_b):
                    mo_int = int(round(sim_bal * (req.current_apr_bps / 10000.0) / 12.0))
                    int_a += mo_int
                    sim_bal = max(0, sim_bal + mo_int - req.monthly_payment_cents)
            net_savings = max(0, int_a - total_cost_b)
            verdict = "strong_yes" if clears_promo else "marginal"
            expl = (
                f"Non-amortizing current payment alert: at ${req.monthly_payment_cents / 100:,.2f}/mo, your current "
                f"{req.current_apr_bps / 100:.2f}% APR card charges ${first_mo_interest_a / 100:,.2f}/mo in interest "
                f"and NEVER pays down principal. Moving to 0% for {req.promo_months} months applies every dollar to principal "
                f"after the ${fee_cents / 100:,.2f} fee and pays off in {m_b} months (post-promo {post_promo_apr_bps / 100:.2f}% APR) — "
                f"or raise payments to ${req_monthly_clear / 100:,.2f}/mo to finish before promo ends."
            )
        elif current_payoff_possible and not transfer_payoff_possible:
            verdict = "skip"
            net_savings = int_a - total_cost_b
            expl = (
                f"Skip this transfer: after {req.promo_months} months, the remaining balance hits a higher "
                f"{post_promo_apr_bps / 100:.2f}% post-promo APR where ${req.monthly_payment_cents / 100:,.2f}/mo "
                f"no longer covers monthly interest. Raise payments to ${req_monthly_clear / 100:,.2f}/mo or stay on your current card."
            )
        else:
            net_savings = int_a - total_cost_b
            if net_savings >= 15_000 and clears_promo:
                verdict = "strong_yes"
                expl = (
                    f"Strong YES: saves ${net_savings / 100:,.2f} net after the ${fee_cents / 100:,.2f} transfer fee "
                    f"and pays off cleanly inside the {req.promo_months}-month 0% window."
                )
            elif net_savings >= 5_000:
                verdict = "marginal"
                expl = (
                    f"Marginal win: saves ${net_savings / 100:,.2f} net (post-promo {post_promo_apr_bps / 100:.2f}% APR), "
                    f"{'clearing within promo.' if clears_promo else f'but requires ${req_monthly_clear / 100:,.2f}/mo to beat the {req.promo_months}-mo promo cliff.'}"
                )
            else:
                verdict = "skip"
                expl = (
                    f"Skip this transfer: the ${fee_cents / 100:,.2f} fee outweighs the interest savings "
                    f"(net impact: {'+' if net_savings >= 0 else '-'}${abs(net_savings) / 100:,.2f}). "
                    f"Apply ${req.monthly_payment_cents / 100:,.2f}/mo directly to principal ({m_a} months to payoff)."
                )

        return BalanceTransferResult(
            verdict=verdict,  # type: ignore[arg-type]
            transfer_fee_cents=fee_cents,
            interest_without_transfer_cents=int_a,
            interest_with_transfer_cents=int_b,
            net_savings_cents=net_savings,
            required_monthly_to_clear_promo_cents=req_monthly_clear,
            clears_before_promo_ends=clears_promo,
            post_promo_apr_bps=post_promo_apr_bps,
            current_payoff_possible=current_payoff_possible,
            transfer_payoff_possible=transfer_payoff_possible,
            min_amortizing_payment_cents=min_amortizing_cents,
            explanation=expl,
        )


# ============================================================================
# Financial Drag & Utilization Paydown Bridge
# ============================================================================

_EWA_APP_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    ("Dave", re.compile(r"\bdave\b(?!\s*&\s*buster)", re.I)),
    ("Brigit", re.compile(r"\bbrigit\b", re.I)),
    ("EarnIn", re.compile(r"\bearnin\b", re.I)),
    ("Klover", re.compile(r"\bklover\b", re.I)),
    ("Albert Instant", re.compile(r"\balbert\s+instant\b", re.I)),
    ("MoneyLion", re.compile(r"\bmoneylion\b", re.I)),
)
_BNPL_RE = re.compile(r"\b(afterpay|klarna|affirm|sezzle|zip\s*pay)\b", re.I)
_INTEREST_RE = re.compile(r"\b(interest\s*charge|finance\s*charge)\b", re.I)
_CASH_ADV_INT_RE = re.compile(r"(cash\s*adv|protected\s*cash\s*adv)", re.I)
_CARD_CASH_ADV_RE = re.compile(r"(credit\s*card\s*cash\s*advance|online\s*cash\s*advance)", re.I)
_FX_FEE_RE = re.compile(r"(intl\s+transaction\s+fee|international\s+transaction\s+fee|foreign\s+transaction\s+fee)", re.I)
_ATM_FEE_RE = re.compile(r"\batm\s+fee\b", re.I)

_UTIL_MILESTONE_SPEC: Tuple[Tuple[float, str], ...] = (
    (88.9, "Exit Maxed-Out Band (<88.9%)"),
    (68.9, "Clear High-Drag Cliff (<68.9%)"),
    (48.9, "Mid-Utilization Cliff (<48.9%)"),
    (28.9, "Sub-30% Standard Cliff (<28.9%)"),
    (8.9, "Peak Low-Utilization Band (<8.9%)"),
    (1.0, "AZEO Optimal Target (1%–4.9%)"),
    (0.0, "Full Grace-Period Reset ($0.00)"),
)


def _match_ewa_app(merchant: str) -> Optional[str]:
    for name, pattern in _EWA_APP_PATTERNS:
        if pattern.search(merchant):
            return name
    return None


def detect_open_ewa_advances(
    transactions: Sequence[Any],
    *,
    today: Optional[date] = None,
    lookback_days: int = 90,
    open_recency_days: int = 45,
) -> Tuple[int, Dict[str, int]]:
    """
    Detect unsettled open EWA paycheck advances (Dave, Brigit, EarnIn, Klover, etc.)
    using the trailing-tail matched-cycle rule:
    - Any inbound advance (`tx_type == "income"`, `amount_cents >= 500`) posted within
      `open_recency_days` (default 45d) AND strictly after that app's most recent outbound
      repayment (`last_out_date`) is an open unsettled advance due on an upcoming payday.
    - Micro-deposit pennies (`< $5.00`) and stale >45d deposits are ignored.
    """
    ref = today or date.today()
    ref_iso = ref.isoformat()
    start_iso = (ref - timedelta(days=max(open_recency_days, int(lookback_days)) - 1)).isoformat()
    recency_iso = (ref - timedelta(days=max(1, int(open_recency_days)) - 1)).isoformat()

    events_by_app: Dict[str, List[Tuple[str, str, int]]] = defaultdict(list)
    for tx in transactions or ():
        tx_date_str = str(_get_field(tx, "tx_date", "") or "")[:10]
        if len(tx_date_str) < 10 or not (start_iso <= tx_date_str <= ref_iso):
            continue
        amt = max(0, int(_get_field(tx, "amount_cents", 0) or 0))
        if amt <= 0:
            continue
        ttype_raw = _get_field(tx, "tx_type", "expense")
        ttype = str(ttype_raw.value if hasattr(ttype_raw, "value") else (ttype_raw or "expense")).strip().lower()
        if ttype not in ("income", "expense"):
            continue
        merchant = str(_get_field(tx, "merchant", "") or "").strip()
        app_name = _match_ewa_app(merchant)
        if app_name is not None:
            events_by_app[app_name].append((tx_date_str, ttype, amt))

    per_app: Dict[str, int] = {}
    for app_name, evs in sorted(events_by_app.items()):
        outs = [d for d, t, _ in evs if t == "expense"]
        last_out = max(outs) if outs else ""
        open_cents = sum(
            a
            for d, t, a in evs
            if t == "income" and a >= 500 and d >= recency_iso and (not last_out or d > last_out)
        )
        if open_cents > 0:
            per_app[app_name] = open_cents
    return sum(per_app.values()), per_app


def _months_to_reach_balance(
    start_balance_cents: int,
    target_balance_cents: int,
    apr_bps: int,
    monthly_payment_cents: int,
) -> Optional[int]:
    """Exact integer-cent amortization until balance <= target_balance_cents (None if non-amortizing)."""
    bal = max(0, int(start_balance_cents))
    tgt = max(0, int(target_balance_cents))
    pmt = max(0, int(monthly_payment_cents))
    if bal <= tgt:
        return 0
    if pmt <= 0:
        return None
    rate_m = max(0, int(apr_bps)) / 10000.0 / 12.0
    for month in range(1, MAX_AMORTIZATION_MONTHS + 1):
        interest = int(round(bal * rate_m)) if rate_m > 0 else 0
        if pmt <= interest:
            return None
        bal = max(0, bal + interest - pmt)
        if bal <= tgt:
            return month
    return None


def _months_between(ref: date, future_iso: str) -> Optional[int]:
    try:
        dt = date.fromisoformat(str(future_iso)[:10])
    except ValueError:
        return None
    return max(0, (dt.year - ref.year) * 12 + (dt.month - ref.month))


def _match_line_balance_for_lender(lender: str, lines: Sequence[Any]) -> int:
    tokens = [
        t
        for t in re.findall(r"[a-z0-9]+", (lender or "").lower())
        if len(t) >= 4 and t not in ("orig", "comm", "mgmt", "inc", "llc", "corp", "wireless", "resource")
    ]
    if not tokens:
        return 0
    for ln in lines:
        ln_name = str(_get_field(ln, "name", "") or "").lower()
        if any(tok in ln_name for tok in tokens[:3]):
            return max(0, int(_get_field(ln, "balance_cents", 0) or 0))
    return 0


def analyze_financial_drag_bridge(
    *,
    credit_lines: Sequence[Any],
    transactions: Sequence[Any] = (),
    derogatories: Sequence[Any] = (),
    extra_monthly_budget_cents: int = 25_000,
    lookback_days: int = 365,
    today: Optional[date] = None,
) -> DragUtilizationBridge:
    """
    Forensic Financial Drag & Utilization Paydown Bridge.

    Connects ledger-level interest burn, FX/ATM fees, card cash advances, and EWA/BNPL
    reborrowing churn directly to FICO 8 / 10T / VantageScore 3.0 & 4.0 revolving
    utilization cliffs and CARD Act (15 U.S.C. § 1666c(b)) payment allocation mechanics.
    """
    ref = today or date.today()
    span_days = max(30, min(730, int(lookback_days)))
    start_iso = (ref - timedelta(days=span_days - 1)).isoformat()
    recent90_iso = (ref - timedelta(days=89)).isoformat()
    ref_iso = ref.isoformat()

    # 1. Forensic Ledger Scan (separating unrecoverable fees/interest from EWA principal churn)
    card_interest_burn = 0
    cash_adv_interest = 0
    cash_adv_int_90d = 0
    bank_and_fx_fees = 0
    bank_fx_90d = 0
    fx_fee_count = 0
    atm_fee_count = 0
    bnpl_outflow = 0
    bnpl_90d = 0
    recent_card_cash_adv = 0
    seen_card_adv_legs: set[Tuple[str, int]] = set()

    ewa_events: Dict[str, List[Tuple[str, str, int]]] = defaultdict(list)
    ewa_monthly_out_90d: Dict[str, int] = defaultdict(int)

    for tx in transactions or ():
        tx_date_str = str(_get_field(tx, "tx_date", "") or "")[:10]
        if len(tx_date_str) < 10 or not (start_iso <= tx_date_str <= ref_iso):
            continue
        amt = max(0, int(_get_field(tx, "amount_cents", 0) or 0))
        if amt <= 0:
            continue
        ttype = str(_get_field(tx, "tx_type", "expense") or "expense").strip().lower()
        if hasattr(ttype, "value"):
            ttype = str(ttype.value)
        merchant = str(_get_field(tx, "merchant", "") or "").strip()
        subcat = str(_get_field(tx, "subcategory", "") or "").strip()
        in_90d = tx_date_str >= recent90_iso

        # Card cash advances (deduplicate paired checking-in / card-transfer legs on same date & amount)
        if _CARD_CASH_ADV_RE.search(merchant):
            if in_90d:
                leg_key = (tx_date_str, amt)
                if leg_key not in seen_card_adv_legs:
                    seen_card_adv_legs.add(leg_key)
                    recent_card_cash_adv += amt
            continue

        # EWA apps (track BOTH outbound repayments and inbound advances so principal churn is never double-counted)
        ewa_app = _match_ewa_app(merchant)
        if ewa_app is not None:
            if ttype in ("expense", "income"):
                ewa_events[ewa_app].append((tx_date_str, ttype, amt))
                if ttype == "expense" and in_90d:
                    ewa_monthly_out_90d[tx_date_str[:7]] += amt
            continue

        if ttype != "expense":
            continue

        if _BNPL_RE.search(merchant):
            bnpl_outflow += amt
            if in_90d:
                bnpl_90d += amt
            continue

        if _INTEREST_RE.search(merchant):
            card_interest_burn += amt
            if _CASH_ADV_INT_RE.search(merchant):
                cash_adv_interest += amt
                if in_90d:
                    cash_adv_int_90d += amt
            continue

        if _FX_FEE_RE.search(merchant):
            bank_and_fx_fees += amt
            fx_fee_count += 1
            if in_90d:
                bank_fx_90d += amt
            continue

        if _ATM_FEE_RE.search(merchant):
            bank_and_fx_fees += amt
            atm_fee_count += 1
            if in_90d:
                bank_fx_90d += amt
            continue

        if subcat == "Interest & Bank Fees":
            bank_and_fx_fees += amt
            if in_90d:
                bank_fx_90d += amt
        elif subcat == "Cash Advances & BNPL":
            bnpl_outflow += amt
            if in_90d:
                bnpl_90d += amt

    # Summarize EWA apps using matched-cycle boundaries ([first_in .. last_out]) so:
    # - Repayments crossing the left window boundary never inflate fees
    # - Inbound advances strictly after the last repayment are surfaced penny-exact as open unsettled advances
    def _matched_ewa_fees_and_open(events: List[Tuple[str, str, int]]) -> Tuple[int, int]:
        ins = [(d, a) for d, t, a in events if t == "income"]
        outs = [(d, a) for d, t, a in events if t == "expense"]
        if not ins:
            return sum(a for _, a in outs), 0
        if not outs:
            return 0, sum(a for _, a in ins if a >= 500)
        first_in = min(d for d, _ in ins)
        last_out = max(d for d, _ in outs)
        trailing_open = sum(a for d, a in ins if d > last_out and a >= 500)
        settled_in = sum(a for d, a in ins if d <= last_out)
        settled_out = sum(a for d, a in outs if d >= first_in)
        matched_fees = max(0, settled_out - settled_in)
        open_adv = max(trailing_open, max(0, sum(a for _, a in ins) - sum(a for _, a in outs)))
        return matched_fees, open_adv

    ewa_apps: List[EwaAppSummaryOut] = []
    ewa_gross_out = 0
    ewa_gross_in = 0
    ewa_net_fee = 0
    ewa_net_fee_90d = 0
    ewa_open_adv = 0
    ewa_cycles = 0
    ewa_active_90d = 0
    for app_name, evs in sorted(
        ewa_events.items(),
        key=lambda kv: (-sum(a for _, t, a in kv[1] if t == "expense"), kv[0]),
    ):
        out_c = sum(a for _, t, a in evs if t == "expense")
        in_c = sum(a for _, t, a in evs if t == "income")
        cycles = sum(1 for _, t, _ in evs if t == "expense")
        cycles_90d = sum(1 for d, t, _ in evs if t == "expense" and d >= recent90_iso)
        last_dt = max((d for d, _, _ in evs), default="")
        net_f, open_a = _matched_ewa_fees_and_open(evs)
        evs_90d = [e for e in evs if e[0] >= recent90_iso]
        net_f_90d, _ = _matched_ewa_fees_and_open(evs_90d)

        ewa_gross_out += out_c
        ewa_gross_in += in_c
        ewa_net_fee += net_f
        ewa_net_fee_90d += net_f_90d
        ewa_open_adv += open_a
        ewa_cycles += cycles
        ewa_active_90d += cycles_90d
        ewa_apps.append(
            EwaAppSummaryOut(
                app_name=app_name,
                outflow_cents=out_c,
                inflow_cents=in_c,
                cycle_count=cycles,
                net_fee_or_subscription_cents=net_f,
                estimated_open_advance_cents=open_a,
                last_activity_date=last_dt,
            )
        )

    peak_monthly_ewa_90d = max(ewa_monthly_out_90d.values(), default=0)
    ewa_exit_buffer = (
        max(ewa_open_adv, peak_monthly_ewa_90d)
        if (ewa_active_90d > 0 or ewa_open_adv > 0)
        else 0
    )

    # Strictly reclaimable monthly cash flow (NEVER counts recycled EWA principal as free surplus!)
    reclaimable_monthly_cents = (
        int(round(bank_fx_90d / 3.0))
        + int(round(bnpl_90d / 3.0))
        + int(round(ewa_net_fee_90d / 3.0))
        + int(round(cash_adv_int_90d / 3.0))
    )
    total_recorded_drag = card_interest_burn + bank_and_fx_fees + ewa_gross_out + bnpl_outflow

    # 2. Revolving Portfolio & Exact Utilization Cliff Ladder
    rev_cards: List[Dict[str, Any]] = []
    for ln in credit_lines or ():
        active = bool(_get_field(ln, "active", True))
        if not active:
            continue
        kind_raw = _get_field(ln, "kind", "")
        kind_str = str(kind_raw.value if hasattr(kind_raw, "value") else kind_raw).lower()
        if kind_str != DebtKind.CREDIT_CARD.value:
            continue
        bal = max(0, int(_get_field(ln, "balance_cents", 0) or 0))
        lim = max(0, int(_get_field(ln, "credit_limit_cents", 0) or 0))
        apr = max(0, int(_get_field(ln, "effective_apr_bps", None) or _get_field(ln, "apr_bps", 0) or 0))
        cfg_min = max(
            0,
            int(_get_field(ln, "effective_min_payment_cents", None) or _get_field(ln, "min_payment_cents", 0) or 0),
        )
        eff_min = compute_effective_min_payment(bal, cfg_min, DebtKind.CREDIT_CARD, apr)
        close_day = int(_get_field(ln, "statement_closing_day", 15) or 15)
        next_close = str(
            _get_field(ln, "next_statement_close_date", None)
            or next_occurrence_of_day(close_day, ref).isoformat()
        )
        rev_cards.append(
            {
                "name": str(_get_field(ln, "name", "Credit Card")),
                "balance_cents": bal,
                "limit_cents": lim,
                "apr_bps": apr,
                "min_cents": eff_min,
                "has_grace": bool(_get_field(ln, "has_grace_period", True)),
                "next_close": next_close,
                "util_pct": (bal / lim * 100.0) if lim > 0 else 0.0,
            }
        )

    rev_bal = sum(c["balance_cents"] for c in rev_cards)
    rev_lim = sum(c["limit_cents"] for c in rev_cards)
    curr_util = round((rev_bal / rev_lim) * 100.0, 2) if rev_lim > 0 else 0.0
    weighted_apr = (
        int(round(sum(c["balance_cents"] * c["apr_bps"] for c in rev_cards) / rev_bal))
        if rev_bal > 0
        else 0
    )
    live_monthly_interest = (
        int(round(sum(c["balance_cents"] * (c["apr_bps"] / 10000.0) / 12.0 for c in rev_cards)))
        if rev_bal > 0
        else 0
    )
    rev_mins = sum(c["min_cents"] for c in rev_cards)
    baseline_pmt = rev_mins + max(0, int(extra_monthly_budget_cents)) if rev_bal > 0 else 0
    accelerated_pmt = baseline_pmt + reclaimable_monthly_cents if rev_bal > 0 else 0

    min_amort_pmt = (
        live_monthly_interest + max(100, int(math.ceil(rev_bal * 0.01)))
        if rev_bal > 0 and weighted_apr > 0
        else (min(rev_bal, 2_500) if rev_bal > 0 else 0)
    )
    non_amort_warn = rev_bal > 0 and weighted_apr > 0 and baseline_pmt <= live_monthly_interest

    trailing_interest_detected = cash_adv_interest > 0
    any_grace_lost = any((not c["has_grace"] and c["balance_cents"] > 0) for c in rev_cards)
    cash_adv_grace_lost = trailing_interest_detected or recent_card_cash_adv > 0 or any_grace_lost

    primary_card = (
        max(rev_cards, key=lambda c: (c["util_pct"], c["balance_cents"]))
        if rev_cards
        else None
    )
    primary_card_name = primary_card["name"] if primary_card else None
    primary_close_date = primary_card["next_close"] if primary_card else None

    milestones: List[UtilizationMilestoneOut] = []
    if rev_lim > 0 and rev_bal > 0:
        for target_pct, tier_label in _UTIL_MILESTONE_SPEC:
            if target_pct > 0.0 and curr_util <= target_pct:
                continue
            target_bal = (rev_lim * int(round(target_pct * 10))) // 1000 if target_pct > 0.0 else 0
            paydown = max(0, rev_bal - target_bal)
            if paydown <= 0:
                continue
            ann_saved = int(round(paydown * (weighted_apr / 10000.0)))
            mo_saved = int(round(ann_saved / 12.0))
            b_mo = _months_to_reach_balance(rev_bal, target_bal, weighted_apr, baseline_pmt)
            a_mo = _months_to_reach_balance(rev_bal, target_bal, weighted_apr, accelerated_pmt)
            b_dt = _add_months_date(ref, b_mo).isoformat() if b_mo is not None else None
            a_dt = _add_months_date(ref, a_mo).isoformat() if a_mo is not None else None
            m_saved = (b_mo - a_mo) if (b_mo is not None and a_mo is not None and b_mo > a_mo) else 0

            if target_pct == 0.0:
                tip = (
                    "Clears all revolving principal and residual trailing interest to restore the 25-day purchase grace period."
                )
            elif primary_close_date:
                tip = (
                    f"Post paydown 3 days before {primary_close_date} statement close so bureaus record "
                    f"<= {target_pct:.1f}% utilization and downward 24-month FICO 10T / VantageScore 4.0 trended balance."
                )
            else:
                tip = f"Target <= {target_pct:.1f}% before statement closing date."

            milestones.append(
                UtilizationMilestoneOut(
                    target_util_pct=target_pct,
                    tier_label=tier_label,
                    target_balance_cents=target_bal,
                    paydown_needed_cents=paydown,
                    monthly_interest_saved_cents=mo_saved,
                    annual_interest_saved_cents=ann_saved,
                    baseline_months=b_mo,
                    baseline_target_date=b_dt,
                    accelerated_months=a_mo,
                    accelerated_target_date=a_dt,
                    months_saved=m_saved,
                    statement_close_tip=tip,
                )
            )

    # 3. Derogatory & Collection Model-Specific Guidance (FICO 8 vs. FICO 9/10T & VantageScore 3.0/4.0)
    derog_guidance: List[DerogatoryResolutionOut] = []
    for dg in derogatories or ():
        if not bool(_get_field(dg, "active", True)):
            continue
        dkind = str(_get_field(dg, "kind", "other") or "other").lower()
        dlender = str(_get_field(dg, "lender", "") or "Reported Account").strip()
        ddrops = str(_get_field(dg, "drops_off", "") or "")[:10]
        m_left = _months_between(ref, ddrops) if ddrops else None
        bal_match = _match_line_balance_for_lender(dlender, credit_lines)

        if dkind == "collection":
            fico8_r = (
                "FICO 8 counts both unpaid and paid non-medical collections >= $100; only written Pay-for-Delete "
                "or 7-year FCRA removal clears FICO 8 impact."
            )
            fico9_r = (
                "FICO 9 / 10T and VantageScore 3.0 / 4.0 completely exclude paid or settled collections once reported at $0."
            )
            move = (
                f"Request written Pay-for-Delete before paying ${bal_match / 100:,.2f} to {dlender} (maximizes FICO 8 while "
                f"$0 status automatically clears FICO 9/10T and VantageScore)."
                if bal_match > 0
                else f"Negotiate written Pay-for-Delete with {dlender} before settling."
            )
        elif dkind in ("late_30", "late_60", "late_90"):
            fico8_r = (
                "Active past-due tradelines risk escalating into third-party collections and recurring 30/60/90-day delinquency marks."
            )
            fico9_r = (
                "Both FICO 9/10T and VantageScore 3.0/4.0 weigh recent delinquencies heavily; bringing the account current stops compounding recency damage."
            )
            move = (
                f"Clear the ${bal_match / 100:,.2f} past-due balance on {dlender} first to stop third-party collection referral, then request goodwill adjustment."
                if bal_match > 0
                else f"Bring {dlender} current immediately to prevent collection escalation."
            )
        elif dkind == "bankruptcy_ch7":
            fico8_r = (
                f"Chapter 7 remains on FICO 8 reports for 10 years (statutory FCRA removal {ddrops or 'in 10y'}). "
                f"Revolving utilization (30% weight) is your primary controllable lever right now."
            )
            fico9_r = (
                "FICO 10T and VantageScore 4.0 reward 24-month downward revolving balance trajectories even while older public records age out."
            )
            move = (
                f"Wait out the remaining {m_left} months to {ddrops} statutory removal while keeping zero new late marks and driving revolving utilization under 8.9%."
                if m_left is not None and ddrops
                else "Keep all open tradelines on autopay while driving revolving utilization under 8.9%."
            )
        else:
            fico8_r = "Remains reported up to 7 years under FCRA; verify Date of First Delinquency (DOFD) accuracy."
            fico9_r = "Paid/settled status reduces lender manual-underwriting friction across newer scoring models."
            move = f"Verify DOFD and balance accuracy for {dlender}."

        derog_guidance.append(
            DerogatoryResolutionOut(
                kind=dkind,
                lender=dlender,
                balance_cents=bal_match,
                drops_off=ddrops,
                months_until_drop_off=m_left,
                fico8_rule=fico8_r,
                fico9_vantage_rule=fico9_r,
                recommended_move=move,
            )
        )

    # 4. Sequenced Action Playbook (strictly driven by real ledger & credit metrics)
    steps: List[BridgeActionStepOut] = []
    step_idx = 1

    if cash_adv_grace_lost or ewa_active_90d > 0 or ewa_open_adv > 0:
        parts: List[str] = []
        if trailing_interest_detected or recent_card_cash_adv > 0:
            parts.append(
                f"Stop credit-card cash advances immediately (${recent_card_cash_adv / 100:,.2f} in 90d; "
                f"${cash_adv_interest / 100:,.2f} cash-advance interest recorded). Cash advances accrue daily interest "
                f"with zero grace period; under 15 U.S.C. § 1666c(b), payments above the minimum clear the highest-APR "
                f"balance first, and paying to $0 restores the 25-day purchase grace period."
            )
        if ewa_active_90d > 0 or ewa_open_adv > 0:
            open_txt = (
                f" Account for ~${ewa_open_adv / 100:,.2f} in estimated unsettled EWA advance debits before your next payday."
                if ewa_open_adv > 0
                else ""
            )
            buf_txt = (
                f" Build a one-time ${ewa_exit_buffer / 100:,.2f} checking cushion to break the "
                f"{ewa_cycles}-cycle EWA reborrowing loop permanently."
                if ewa_exit_buffer > 0
                else ""
            )
            parts.append(f"Active EWA advance cycling detected ({ewa_active_90d} outflows in 90d).{open_txt}{buf_txt}")
        steps.append(
            BridgeActionStepOut(
                step_number=step_idx,
                priority="critical",
                title="Stop Cash-Advance Trailing Interest & Exit EWA Reborrowing",
                detail=" ".join(parts),
                dollar_impact_label=(
                    f"${ewa_exit_buffer / 100:,.2f} exit cushion"
                    if ewa_exit_buffer > 0
                    else f"${live_monthly_interest / 100:,.2f}/mo interest burn"
                ),
            )
        )
        step_idx += 1

    if milestones:
        first_m = milestones[0]
        card_label = primary_card_name or "revolving cards"
        close_txt = f" (3 days before {primary_close_date} statement close)" if primary_close_date else ""
        curr_util_disp = f"{curr_util:.2f}%" if 99.9 < curr_util < 100.0 else f"{curr_util:.1f}%"
        steps.append(
            BridgeActionStepOut(
                step_number=step_idx,
                priority="high",
                title=f"Clear First Utilization Cliff: {curr_util_disp} → <{first_m.target_util_pct:.1f}%",
                detail=(
                    f"Pay ${first_m.paydown_needed_cents / 100:,.2f} toward {card_label}{close_txt} to drop balance to "
                    f"${first_m.target_balance_cents / 100:,.2f}. Cuts ${first_m.annual_interest_saved_cents / 100:,.2f}/yr "
                    f"in revolving interest immediately and begins a downward 24-month FICO 10T / VantageScore 4.0 trend."
                ),
                dollar_impact_label=f"-${first_m.monthly_interest_saved_cents / 100:,.2f}/mo interest",
            )
        )
        step_idx += 1

    if fx_fee_count > 0 or atm_fee_count > 0:
        fx_txt = (
            f"{fx_fee_count} foreign transaction fee hits detected on debit/checking swipes. "
            f"Once a $0-FTF credit card regains its 25-day grace period, routing overseas purchases through it eliminates 1% FX fees."
            if fx_fee_count > 0
            else ""
        )
        atm_txt = f" {atm_fee_count} out-of-network ATM fees recorded." if atm_fee_count > 0 else ""
        steps.append(
            BridgeActionStepOut(
                step_number=step_idx,
                priority="medium",
                title="Plug Recurring Foreign Transaction & ATM Fee Leaks",
                detail=f"Recorded ${bank_and_fx_fees / 100:,.2f} in avoidable bank/FX/ATM fees.{(' ' + fx_txt) if fx_txt else ''}{atm_txt}".strip(),
                dollar_impact_label=f"${bank_and_fx_fees / 100:,.2f} recorded fees",
            )
        )
        step_idx += 1

    actionable_derogs = [d for d in derog_guidance if d.kind in ("late_30", "late_60", "late_90", "collection")]
    if actionable_derogs:
        total_derog_bal = sum(d.balance_cents for d in actionable_derogs)
        names = ", ".join(d.lender for d in actionable_derogs[:2])
        steps.append(
            BridgeActionStepOut(
                step_number=step_idx,
                priority="strategic",
                title="Resolve Past-Due & Collection Tradelines Strategically",
                detail=(
                    f"Clear active past-due balances before third-party collection escalation, and request written "
                    f"Pay-for-Delete on collections ({names}) so both FICO 8 and FICO 9/10T / VantageScore reflect clean status."
                ),
                dollar_impact_label=(
                    f"${total_derog_bal / 100:,.2f} total open" if total_derog_bal > 0 else "Model-specific cleanup"
                ),
            )
        )

    return DragUtilizationBridge(
        lookback_days=span_days,
        card_interest_burn_cents=card_interest_burn,
        cash_advance_interest_cents=cash_adv_interest,
        recent_card_cash_advance_cents=recent_card_cash_adv,
        cash_advance_grace_period_lost=cash_adv_grace_lost,
        trailing_interest_detected=trailing_interest_detected,
        bank_and_fx_fee_cents=bank_and_fx_fees,
        fx_fee_count=fx_fee_count,
        atm_fee_count=atm_fee_count,
        ewa_gross_outflow_cents=ewa_gross_out,
        ewa_gross_inflow_cents=ewa_gross_in,
        ewa_net_fee_bleed_cents=ewa_net_fee,
        ewa_estimated_open_advance_cents=ewa_open_adv,
        ewa_cycle_count=ewa_cycles,
        ewa_active_90d_cycles=ewa_active_90d,
        ewa_exit_buffer_target_cents=ewa_exit_buffer,
        bnpl_outflow_cents=bnpl_outflow,
        total_recorded_drag_cents=total_recorded_drag,
        reclaimable_monthly_cents=reclaimable_monthly_cents,
        revolving_balance_cents=rev_bal,
        revolving_limit_cents=rev_lim,
        current_utilization_pct=curr_util,
        weighted_revolving_apr_bps=weighted_apr,
        live_monthly_revolving_interest_cents=live_monthly_interest,
        baseline_monthly_payment_cents=baseline_pmt,
        accelerated_monthly_payment_cents=accelerated_pmt,
        min_amortizing_revolving_payment_cents=min_amort_pmt,
        non_amortizing_warning=non_amort_warn,
        primary_target_card_name=primary_card_name,
        primary_target_close_date=primary_close_date,
        ewa_apps=ewa_apps,
        utilization_milestones=milestones,
        derogatory_guidance=derog_guidance,
        ordered_action_steps=steps,
    )


# ============================================================================
# Optimal Bi-Weekly Card Paydown, Collection Cascade & Branching 700+ Roadmap
# ============================================================================

_SPLURGE_TAGS = frozenset({"one-time", "onetime", "birthday", "splurge", "anomaly", "holiday"})
_COLLECTION_BILL_RE = re.compile(r"\b(collection|recovery|collector|settlement|agency|charge[- ]?off)\b", re.I)


def _extract_tx_tags(tx: Any) -> set[str]:
    raw_tags = _get_field(tx, "tags", None) or ()
    out: set[str] = set()
    for t in raw_tags:
        if isinstance(t, str):
            out.add(t.strip().lower())
        elif isinstance(t, dict):
            name = str(t.get("name", "") or "").strip().lower()
            if name:
                out.add(name)
        else:
            name = str(getattr(t, "name", "") or "").strip().lower()
            if name:
                out.add(name)
    return out


def _simulate_adb_biweekly_trajectory(
    *,
    balance_cents: int,
    limit_cents: int,
    apr_bps: int,
    biweekly_payment_cents: int,
    today: date,
    delay_days: int = 0,
    cascade_after_days: Optional[int] = None,
    cascade_biweekly_bonus_cents: int = 0,
) -> Dict[str, Any]:
    """
    Day-by-day Average Daily Balance (ADB) bi-weekly amortization engine (`DPR = APR / 365`).
    Posts payments every 14 days (26 payments/yr = 13 monthly equivalents/yr), optionally
    starting after `delay_days` and cascading `+cascade_biweekly_bonus_cents` once an
    installment/collection clears at `cascade_after_days`.
    """
    bal = max(0, int(balance_cents))
    lim = max(0, int(limit_cents))
    pmt_base = max(0, int(biweekly_payment_cents))
    dpr = max(0, int(apr_bps)) / 10000.0 / 365.0
    max_days = MAX_AMORTIZATION_MONTHS * 31

    if bal <= 0:
        iso0 = today.isoformat()
        return {
            "amortizes": True,
            "payoff_days": 0,
            "payoff_months": 0,
            "payoff_date": iso0,
            "payments_count": 0,
            "total_interest_cents": 0,
            "cliff_dates": {88.9: iso0, 68.9: iso0, 48.9: iso0, 28.9: iso0, 8.9: iso0, 1.0: iso0},
        }
    if pmt_base <= 0:
        return {
            "amortizes": False,
            "payoff_days": None,
            "payoff_months": None,
            "payoff_date": None,
            "payments_count": 0,
            "total_interest_cents": 0,
            "cliff_dates": {},
        }

    cliff_targets: Dict[float, int] = (
        {pct: (lim * int(round(pct * 10))) // 1000 for pct in (88.9, 68.9, 48.9, 28.9, 8.9, 1.0)}
        if lim > 0
        else {}
    )
    cliff_dates: Dict[float, str] = {}
    for pct, tgt in cliff_targets.items():
        if bal <= tgt:
            cliff_dates[pct] = today.isoformat()

    total_interest = 0
    payments_count = 0
    cycle_bal_sum = 0
    cycle_start_bal = bal
    stalled_cycles = 0

    for day in range(1, max_days + 1):
        # Post bi-weekly payment every 14 days once any upfront delay passes
        if day > delay_days and (day - delay_days) % 14 == 0:
            bonus = (
                max(0, int(cascade_biweekly_bonus_cents))
                if (cascade_after_days is not None and day >= cascade_after_days)
                else 0
            )
            pmt_now = pmt_base + bonus
            if pmt_now > 0:
                bal = max(0, bal - pmt_now)
                payments_count += 1
                day_iso = (today + timedelta(days=day)).isoformat()
                for pct, tgt in cliff_targets.items():
                    if pct not in cliff_dates and bal <= tgt:
                        cliff_dates[pct] = day_iso

        cycle_bal_sum += bal

        # Post accrued cycle interest every 30 days or immediately upon reaching $0
        if day % 30 == 0 or bal == 0:
            cycle_int = int(round(cycle_bal_sum * dpr)) if dpr > 0 else 0
            total_interest += cycle_int
            bal += cycle_int
            cycle_bal_sum = 0

            # If payment cleared principal + residual interest on this cycle, finish cleanly
            if bal == 0:
                payoff_dt = (today + timedelta(days=day)).isoformat()
                for pct in cliff_targets:
                    cliff_dates.setdefault(pct, payoff_dt)
                return {
                    "amortizes": True,
                    "payoff_days": day,
                    "payoff_months": max(1, int(math.ceil(day / 30.4375))),
                    "payoff_date": payoff_dt,
                    "payments_count": payments_count,
                    "total_interest_cents": total_interest,
                    "cliff_dates": cliff_dates,
                }

            # Detect non-amortizing stall (only after any initial delay AND any pending cascade have started)
            post_delay_and_cascade = day > delay_days + 30 and (
                cascade_after_days is None or day >= cascade_after_days + 30
            )
            if post_delay_and_cascade:
                if bal >= cycle_start_bal:
                    stalled_cycles += 1
                    if stalled_cycles >= 2:
                        return {
                            "amortizes": False,
                            "payoff_days": None,
                            "payoff_months": None,
                            "payoff_date": None,
                            "payments_count": payments_count,
                            "total_interest_cents": total_interest,
                            "cliff_dates": cliff_dates,
                        }
                else:
                    stalled_cycles = 0
            cycle_start_bal = bal

    return {
        "amortizes": False,
        "payoff_days": None,
        "payoff_months": None,
        "payoff_date": None,
        "payments_count": payments_count,
        "total_interest_cents": total_interest,
        "cliff_dates": cliff_dates,
    }


def simulate_biweekly_vs_monthly_payoff(
    *,
    balance_cents: int,
    apr_bps: int,
    monthly_payment_cents: int,
    biweekly_payment_cents: Optional[int] = None,
    today: Optional[date] = None,
) -> Dict[str, Any]:
    """
    Exact side-by-side comparison of Monthly (12x/yr) vs. Bi-Weekly (26x/yr, day-by-day ADB)
    revolving payoff in integer cents.
    """
    ref = today or date.today()
    bal0 = max(0, int(balance_cents))
    pmt_mo = max(0, int(monthly_payment_cents))
    pmt_bw = max(0, int(biweekly_payment_cents)) if biweekly_payment_cents is not None else (pmt_mo // 2)

    if bal0 <= 0:
        iso0 = ref.isoformat()
        return {
            "amortizes": True,
            "monthly_payment_cents": pmt_mo,
            "biweekly_payment_cents": pmt_bw,
            "monthly_payoff_months": 0,
            "monthly_payoff_date": iso0,
            "monthly_total_interest_cents": 0,
            "biweekly_payoff_months": 0,
            "biweekly_payoff_date": iso0,
            "biweekly_total_interest_cents": 0,
            "biweekly_interest_saved_cents": 0,
            "biweekly_months_saved": 0,
        }

    # 1. Standard Monthly (12 payments/yr on cycle end)
    bal_m = bal0
    int_m = 0
    months_m: Optional[int] = 0
    rate_m = max(0, int(apr_bps)) / 10000.0 / 12.0
    if pmt_mo <= 0:
        months_m = None
    else:
        for m in range(1, MAX_AMORTIZATION_MONTHS + 1):
            mo_int = int(round(bal_m * rate_m)) if rate_m > 0 else 0
            if pmt_mo <= mo_int:
                months_m = None
                break
            int_m += mo_int
            bal_m = max(0, bal_m + mo_int - pmt_mo)
            if bal_m == 0:
                months_m = m
                break
        if bal_m > 0:
            months_m = None

    # 2. Bi-Weekly ADB (26 half-payments/yr = 13 monthly eq. + lower Average Daily Balance)
    bw_res = _simulate_adb_biweekly_trajectory(
        balance_cents=bal0,
        limit_cents=0,
        apr_bps=apr_bps,
        biweekly_payment_cents=pmt_bw,
        today=ref,
    )

    amortizes = months_m is not None and bw_res["amortizes"]
    int_bw = int(bw_res["total_interest_cents"])
    months_bw = bw_res["payoff_months"]
    saved_int = max(0, int_m - int_bw) if amortizes else 0
    saved_mo = (
        max(0, int(months_m) - int(months_bw))
        if (months_m is not None and months_bw is not None)
        else 0
    )

    return {
        "amortizes": amortizes,
        "monthly_payment_cents": pmt_mo,
        "biweekly_payment_cents": pmt_bw,
        "monthly_payoff_months": months_m,
        "monthly_payoff_date": _add_months_date(ref, months_m).isoformat() if months_m is not None else None,
        "monthly_total_interest_cents": int_m if months_m is not None else 0,
        "biweekly_payoff_months": months_bw,
        "biweekly_payoff_date": bw_res["payoff_date"],
        "biweekly_total_interest_cents": int_bw if bw_res["amortizes"] else 0,
        "biweekly_interest_saved_cents": saved_int,
        "biweekly_months_saved": saved_mo,
    }


def _detect_biweekly_collection_plan(
    *,
    credit_lines: Sequence[Any],
    recurring_bills: Sequence[Any],
    derogatories: Sequence[Any],
    today: date,
) -> Optional[CollectionPlanOut]:
    """Detect active collection balance + bi-weekly payment plan (e.g. $75 bi-weekly collection settlement)."""
    coll_bill: Optional[Any] = None
    for b in recurring_bills or ():
        if not bool(_get_field(b, "active", True)):
            continue
        bname = str(_get_field(b, "name", "") or "")
        cad = str(_get_field(b, "cadence", "monthly") or "monthly").lower()
        if _COLLECTION_BILL_RE.search(bname) and cad == "biweekly":
            coll_bill = b
            break

    coll_line: Optional[Any] = None
    for ln in credit_lines or ():
        if not bool(_get_field(ln, "active", True)):
            continue
        lname = str(_get_field(ln, "name", "") or "")
        bal = max(0, int(_get_field(ln, "balance_cents", 0) or 0))
        if bal <= 0:
            continue
        if _COLLECTION_BILL_RE.search(lname):
            coll_line = ln
            break

    if coll_line is None:
        for dg in derogatories or ():
            if not bool(_get_field(dg, "active", True)):
                continue
            if str(_get_field(dg, "kind", "")).lower() == "collection":
                lender = str(_get_field(dg, "lender", "") or "Collection Account")
                bal_m = _match_line_balance_for_lender(lender, credit_lines)
                if bal_m > 0:
                    coll_line = {"name": lender, "balance_cents": bal_m, "min_payment_cents": 0}
                    break

    if coll_bill is None and coll_line is None:
        return None

    lender_name = (
        str(_get_field(coll_line, "name", ""))
        if coll_line is not None
        else str(_get_field(coll_bill, "name", "Collection Account"))
    )
    balance_cents = max(0, int(_get_field(coll_line, "balance_cents", 0) or 0)) if coll_line is not None else 0
    if coll_bill is not None:
        bw_pmt = max(100, int(_get_field(coll_bill, "amount_cents", 7_500) or 7_500))
    else:
        cfg_min = max(0, int(_get_field(coll_line, "min_payment_cents", 0) or 0))
        bw_pmt = max(2_500, int(round(cfg_min * 12.0 / 26.0))) if cfg_min > 0 else 7_500

    if balance_cents <= 0:
        return None

    payments_rem = max(1, int(math.ceil(balance_cents / bw_pmt)))
    payoff_date = (today + timedelta(days=payments_rem * 14)).isoformat()
    mo_eq = int(round(bw_pmt * 26.0 / 12.0))

    return CollectionPlanOut(
        lender_name=lender_name,
        balance_cents=balance_cents,
        biweekly_payment_cents=bw_pmt,
        monthly_equivalent_cents=mo_eq,
        payments_remaining=payments_rem,
        payoff_date=payoff_date,
        freed_biweekly_cents=bw_pmt,
        freed_monthly_cents=mo_eq,
        pay_for_delete_note=(
            f"At ${bw_pmt / 100:,.2f} bi-weekly, {lender_name} reaches $0 in {payments_rem} payments ({payoff_date}). "
            f"Request written Pay-for-Delete before the final payment so FICO 8 drops it too; FICO 9/10T & "
            f"VantageScore 3.0/4.0 ignore paid collections automatically at $0, freeing ${mo_eq / 100:,.2f}/mo to cascade into card principal."
        ),
    )


def build_credit_and_wishlist_roadmap(
    *,
    credit_lines: Sequence[Any],
    transactions: Sequence[Any] = (),
    recurring_bills: Sequence[Any] = (),
    goals: Sequence[Any] = (),
    credit_reports: Sequence[Any] = (),
    derogatories: Sequence[Any] = (),
    smoothed_monthly_income_cents: int = 0,
    payday_cadence_days: int = 14,
    extra_monthly_budget_cents: int = 25_000,
    selected_branch: str = "balanced_mission",
    lookback_days: int = 365,
    drag_bridge: Optional[DragUtilizationBridge] = None,
    today: Optional[date] = None,
) -> CreditMissionRoadmap:
    """
    End-to-End Credit 700+ Branching Roadmap, Optimal Bi-Weekly Card Plan & Staged Wishlist Engine.

    Combines:
    - Exact day-by-day Average Daily Balance bi-weekly vs. monthly revolving card amortization
    - Automatic collection payment payoff & freed-cashflow cascade (e.g. $75 bi-weekly -> card)
    - One-time splurge / birthday anomaly filtering (`one-time` / `birthday` / `splurge` tags)
    - 3 branching trajectories (`balanced_mission`, `credit_sprint`, `gear_upfront`)
    - 6 empirical credit stages toward 700+ eligibility and Clean Scorecard graduation
    - Exact paycheck-aligned wishlist unlock dates (EWA Stabilization Pool -> Priority Tier 1 -> Tier 2 -> Tier 3)
    """
    ref = today or date.today()
    eff_branch: str = (selected_branch or "balanced_mission").strip().lower()
    if eff_branch not in ("balanced_mission", "credit_sprint", "gear_upfront"):
        eff_branch = "balanced_mission"

    bridge = drag_bridge or analyze_financial_drag_bridge(
        credit_lines=credit_lines,
        transactions=transactions,
        derogatories=derogatories,
        extra_monthly_budget_cents=extra_monthly_budget_cents,
        lookback_days=lookback_days,
        today=ref,
    )

    # 1. Latest actual bureau scores (FICO 8 & VantageScore 3.0)
    curr_fico8: Optional[int] = None
    curr_vantage3: Optional[int] = None
    for r in credit_reports or ():
        model = str(_get_field(r, "model", "fico8") or "fico8").lower()
        sc = int(_get_field(r, "score", 0) or 0)
        if sc < 300:
            continue
        if model == "fico8" and curr_fico8 is None:
            curr_fico8 = sc
        elif model == "vantage3" and curr_vantage3 is None:
            curr_vantage3 = sc

    # 2. Steady-state income & splurge-adjusted 90d monthly spend baseline
    recent90_iso = (ref - timedelta(days=89)).isoformat()
    ref_iso = ref.isoformat()
    splurge_excluded = 0
    splurge_count = 0
    steady_90d_expense = 0
    income_90d = 0
    for tx in transactions or ():
        tx_date_str = str(_get_field(tx, "tx_date", "") or "")[:10]
        if len(tx_date_str) < 10 or not (recent90_iso <= tx_date_str <= ref_iso):
            continue
        amt = max(0, int(_get_field(tx, "amount_cents", 0) or 0))
        if amt <= 0:
            continue
        ttype_raw = _get_field(tx, "tx_type", "expense")
        ttype = str(ttype_raw.value if hasattr(ttype_raw, "value") else (ttype_raw or "expense")).strip().lower()
        merchant = str(_get_field(tx, "merchant", "") or "")
        if _match_ewa_app(merchant) is not None:
            continue  # Exclude recycled EWA churn from steady-state income/spend baseline
        if ttype == "income":
            income_90d += amt
        elif ttype == "expense":
            tags = _extract_tx_tags(tx)
            if tags & _SPLURGE_TAGS:
                splurge_excluded += amt
                splurge_count += 1
            else:
                steady_90d_expense += amt

    est_monthly_income = max(int(smoothed_monthly_income_cents or 0), int(round(income_90d / 3.0)))
    est_steady_spend = int(round(steady_90d_expense / 3.0))

    # 3. Collection bi-weekly plan & cascade schedule
    coll_plan = _detect_biweekly_collection_plan(
        credit_lines=credit_lines,
        recurring_bills=recurring_bills,
        derogatories=derogatories,
        today=ref,
    )
    cascade_days = (coll_plan.payments_remaining * 14) if coll_plan is not None else None
    cascade_bw_bonus = coll_plan.freed_biweekly_cents if coll_plan is not None else 0

    # 4. Optimal 3-tier Credit Card Payment Plan (Safe Floor | Optimal Bi-Weekly | Aggressive Sprint)
    rev_bal = bridge.revolving_balance_cents
    rev_lim = bridge.revolving_limit_cents
    weighted_apr = bridge.weighted_revolving_apr_bps
    min_amort_mo = max(bridge.min_amortizing_revolving_payment_cents, 5_000) if rev_bal > 0 else 0
    rev_mins_mo = max(0, bridge.baseline_monthly_payment_cents - max(0, int(extra_monthly_budget_cents)))

    # Round cleanly to nearest $5 (500 cents) so real-world bank autopay amounts feel natural
    def _round5(cents: int) -> int:
        return max(500, int(round(cents / 500.0)) * 500) if cents > 0 else 0

    if rev_bal > 0:
        safe_floor_bw = _round5(max(int(math.ceil(min_amort_mo / 2.0)), int(math.ceil(rev_mins_mo / 2.0)) + 2_500))
        # Optimal bi-weekly balances strong principal velocity (~14-18% of monthly paycheck) with EWA/wishlist room
        income_target_bw = _round5(int(round(est_monthly_income * 0.085))) if est_monthly_income > 0 else 20_000
        optimal_bw = max(
            safe_floor_bw + 5_000,
            min(45_000, max(income_target_bw, _round5((rev_mins_mo + max(25_000, extra_monthly_budget_cents)) // 2))),
        )
        # Reclaimed fees boost optimal/sprint velocity naturally
        reclaim_bw = int(round(bridge.reclaimable_monthly_cents * 12.0 / 26.0))
        aggressive_bw = max(optimal_bw + 7_500, _round5(optimal_bw + max(7_500, reclaim_bw + 5_000)))
    else:
        safe_floor_bw = 0
        optimal_bw = 0
        aggressive_bw = 0

    tier_specs = [
        (
            "safe_floor",
            "Safe Floor",
            False,
            safe_floor_bw,
            "Clears monthly interest plus steady principal while preserving maximum checking liquidity during stabilization.",
        ),
        (
            "optimal_biweekly",
            "Optimal Bi-Weekly",
            True,
            optimal_bw,
            "Syncs every payday (26x/yr = 13 monthly payments/yr), cuts Average Daily Balance interest, and leaves room for your Stabilization Pool & Tier 1 gear.",
        ),
        (
            "aggressive_sprint",
            "Aggressive Sprint",
            False,
            aggressive_bw,
            "Deploys maximum bi-weekly surplus toward revolving principal to clear utilization cliffs and restore your 25-day grace period earliest.",
        ),
    ]

    tiers_out: List[CardPaymentTierOut] = []
    for tid, tlabel, is_rec, bw_c, rationale in tier_specs:
        mo_c = bw_c * 2
        cmp = simulate_biweekly_vs_monthly_payoff(
            balance_cents=rev_bal,
            apr_bps=weighted_apr,
            monthly_payment_cents=mo_c,
            biweekly_payment_cents=bw_c,
            today=ref,
        )
        casc_sim = _simulate_adb_biweekly_trajectory(
            balance_cents=rev_bal,
            limit_cents=rev_lim,
            apr_bps=weighted_apr,
            biweekly_payment_cents=bw_c,
            today=ref,
            cascade_after_days=cascade_days,
            cascade_biweekly_bonus_cents=cascade_bw_bonus,
        )
        tiers_out.append(
            CardPaymentTierOut(
                tier_id=tid,  # type: ignore[arg-type]
                label=tlabel,
                recommended=is_rec,
                monthly_payment_cents=mo_c,
                biweekly_payment_cents=bw_c,
                monthly_payoff_months=cmp["monthly_payoff_months"],
                monthly_payoff_date=cmp["monthly_payoff_date"],
                monthly_total_interest_cents=cmp["monthly_total_interest_cents"],
                biweekly_payoff_months=cmp["biweekly_payoff_months"],
                biweekly_payoff_date=cmp["biweekly_payoff_date"],
                biweekly_total_interest_cents=cmp["biweekly_total_interest_cents"],
                biweekly_interest_saved_cents=cmp["biweekly_interest_saved_cents"],
                biweekly_months_saved=cmp["biweekly_months_saved"],
                with_cascade_payoff_date=casc_sim["payoff_date"],
                rationale=rationale,
            )
        )

    close_tip = (
        f"Schedule your second bi-weekly payment at least 3 days before {bridge.primary_target_close_date} "
        f"statement close so bureaus record the lowest utilization snapshot."
        if bridge.primary_target_close_date
        else "Align bi-weekly payments 3 days before statement close to lock in lower reported utilization."
    )
    optimal_card_plan = OptimalCardPaymentPlan(
        primary_card_name=bridge.primary_target_card_name,
        revolving_balance_cents=rev_bal,
        weighted_apr_bps=weighted_apr,
        statement_close_date=bridge.primary_target_close_date,
        recommended_tier_id="optimal_biweekly",
        recommended_biweekly_cents=optimal_bw,
        recommended_monthly_cents=optimal_bw * 2,
        statement_close_alignment_tip=close_tip,
        tiers=tiers_out,
    )

    # 5. Staged Active Goals / Wishlist Inspection (Stabilization Pool + Gear Tiers)
    active_goals = [
        g
        for g in (goals or ())
        if str(_get_field(g, "status", "active") or "active").lower() == "active"
        and _get_field(g, "deleted_at", None) is None
    ]
    active_goals.sort(
        key=lambda g: (
            0
            if (
                str(_get_field(g, "category", "")).lower() == "emergency"
                or "stabiliz" in str(_get_field(g, "name", "")).lower()
                or "ewa" in str(_get_field(g, "name", "")).lower()
            )
            else 1,
            int(_get_field(g, "priority", 2) or 2),
            int(_get_field(g, "priority_order", 0) or 0),
            int(_get_field(g, "id", 0) or 0),
        )
    )
    gear_total_remaining = sum(
        max(0, int(_get_field(g, "target_cents", 0) or 0) - int(_get_field(g, "saved_cents", 0) or 0))
        for g in active_goals
        if not (
            str(_get_field(g, "category", "")).lower() == "emergency"
            or "stabiliz" in str(_get_field(g, "name", "")).lower()
            or "ewa" in str(_get_field(g, "name", "")).lower()
        )
    )
    if gear_total_remaining <= 0:
        gear_total_remaining = 50_000  # Reference $500 kit comparison when no gadget goals exist yet

    ewa_pool_target = max(bridge.ewa_exit_buffer_target_cents, 50_000 if bridge.ewa_cycle_count > 0 else 0)
    for g in active_goals:
        if (
            str(_get_field(g, "category", "")).lower() == "emergency"
            or "stabiliz" in str(_get_field(g, "name", "")).lower()
        ):
            ewa_pool_target = max(
                ewa_pool_target,
                max(0, int(_get_field(g, "target_cents", 0) or 0) - int(_get_field(g, "saved_cents", 0) or 0)),
            )
            break

    # 6. Compute All 3 Branching Trajectories Over Time (Exact ADB + Collection Cascade)
    wishlist_slice_balanced = 6_000  # $60/paycheck (~$130/mo) controlled sinking slice
    stab_slice_balanced = 12_500  # $125/paycheck toward Stabilization Pool
    stab_paychecks_bal = max(1, int(math.ceil(ewa_pool_target / stab_slice_balanced))) if ewa_pool_target > 0 else 0
    stab_date_bal = (ref + timedelta(days=stab_paychecks_bal * 14)).isoformat()

    stab_paychecks_sprint = max(1, int(math.ceil(ewa_pool_target / 17_500))) if ewa_pool_target > 0 else 0
    stab_date_sprint = (ref + timedelta(days=stab_paychecks_sprint * 14)).isoformat()

    # Upfront gear delays stabilization + first card paydown cycles by exact time to absorb full gadget cost
    upfront_delay_cycles = max(2, int(math.ceil(gear_total_remaining / max(10_000, optimal_bw))))
    upfront_delay_days = upfront_delay_cycles * 14
    stab_date_upfront = (ref + timedelta(days=upfront_delay_days + stab_paychecks_bal * 14)).isoformat()

    sim_sprint = _simulate_adb_biweekly_trajectory(
        balance_cents=rev_bal,
        limit_cents=rev_lim,
        apr_bps=weighted_apr,
        biweekly_payment_cents=aggressive_bw,
        today=ref,
        cascade_after_days=cascade_days,
        cascade_biweekly_bonus_cents=cascade_bw_bonus,
    )
    sim_balanced = _simulate_adb_biweekly_trajectory(
        balance_cents=rev_bal,
        limit_cents=rev_lim,
        apr_bps=weighted_apr,
        biweekly_payment_cents=optimal_bw,
        today=ref,
        cascade_after_days=cascade_days,
        cascade_biweekly_bonus_cents=cascade_bw_bonus,
    )
    sim_upfront = _simulate_adb_biweekly_trajectory(
        balance_cents=rev_bal,
        limit_cents=rev_lim,
        apr_bps=weighted_apr,
        biweekly_payment_cents=optimal_bw,
        today=ref,
        delay_days=upfront_delay_days,
        cascade_after_days=cascade_days,
        cascade_biweekly_bonus_cents=cascade_bw_bonus,
    )

    sprint_int = int(sim_sprint["total_interest_cents"])
    bal_int = int(sim_balanced["total_interest_cents"])
    up_int = int(sim_upfront["total_interest_cents"])

    def _target_700_date(sim_d: Dict[str, Any]) -> Optional[str]:
        sub9 = sim_d["cliff_dates"].get(8.9)
        if coll_plan is not None and sub9:
            return max(sub9, coll_plan.payoff_date)
        return sub9 or sim_d["payoff_date"]

    balanced_tagline = (
        f"Stabilize first, fund Priority 1 wishlist safely, cascade ${coll_plan.biweekly_payment_cents / 100:,.0f}/2wk collection into card"
        if coll_plan is not None
        else "Stabilize first, fund Priority 1 wishlist safely, and hold optimal bi-weekly card payments"
    )
    branches_out: List[RoadmapBranchOut] = [
        RoadmapBranchOut(
            branch_id="balanced_mission",
            label="Balanced Mission Plan",
            tagline=balanced_tagline,
            recommended=True,
            biweekly_card_payment_cents=optimal_bw,
            biweekly_wishlist_slice_cents=wishlist_slice_balanced,
            stabilization_date=stab_date_bal,
            sub_30_date=sim_balanced["cliff_dates"].get(28.9),
            sub_9_azeo_date=sim_balanced["cliff_dates"].get(8.9),
            zero_balance_date=sim_balanced["payoff_date"],
            target_700_band_date=_target_700_date(sim_balanced),
            total_revolving_interest_cents=bal_int,
            interest_delta_vs_sprint_cents=max(0, bal_int - sprint_int),
            tradeoff_summary=(
                f"Keeps ${optimal_bw / 100:,.2f} bi-weekly attacking principal while carving ${wishlist_slice_balanced / 100:,.2f}/paycheck "
                f"to unlock your top-priority gear without re-entering EWA payday apps."
            ),
        ),
        RoadmapBranchOut(
            branch_id="credit_sprint",
            label="Fastest 700+ Credit Sprint",
            tagline="Pause gear purchases until <8.9% AZEO — minimum interest & earliest 700+ band",
            recommended=False,
            biweekly_card_payment_cents=aggressive_bw,
            biweekly_wishlist_slice_cents=0,
            stabilization_date=stab_date_sprint,
            sub_30_date=sim_sprint["cliff_dates"].get(28.9),
            sub_9_azeo_date=sim_sprint["cliff_dates"].get(8.9),
            zero_balance_date=sim_sprint["payoff_date"],
            target_700_band_date=_target_700_date(sim_sprint),
            total_revolving_interest_cents=sprint_int,
            interest_delta_vs_sprint_cents=0,
            tradeoff_summary=(
                f"Sends ${aggressive_bw / 100:,.2f} bi-weekly straight to principal, saving "
                f"${max(0, bal_int - sprint_int) / 100:,.2f} vs. Balanced and hitting <8.9% AZEO fastest; buy full gear kit cash afterward."
            ),
        ),
        RoadmapBranchOut(
            branch_id="gear_upfront",
            label="Full Kit Upfront (Tradeoff)",
            tagline="Buy entire wishlist immediately — exposes exact interest & timeline delay",
            recommended=False,
            biweekly_card_payment_cents=optimal_bw,
            biweekly_wishlist_slice_cents=gear_total_remaining,
            stabilization_date=stab_date_upfront,
            sub_30_date=sim_upfront["cliff_dates"].get(28.9),
            sub_9_azeo_date=sim_upfront["cliff_dates"].get(8.9),
            zero_balance_date=sim_upfront["payoff_date"],
            target_700_band_date=_target_700_date(sim_upfront),
            total_revolving_interest_cents=up_int,
            interest_delta_vs_sprint_cents=max(0, up_int - sprint_int),
            tradeoff_summary=(
                f"Diverting ${gear_total_remaining / 100:,.2f} upfront delays stabilization by {upfront_delay_cycles} paychecks "
                f"and adds ${max(0, up_int - bal_int) / 100:,.2f} in extra revolving interest vs. Balanced Mission."
            ),
        ),
    ]

    active_sim = (
        sim_sprint
        if eff_branch == "credit_sprint"
        else (sim_upfront if eff_branch == "gear_upfront" else sim_balanced)
    )
    active_branch = next(b for b in branches_out if b.branch_id == eff_branch)

    # 7. Build 6 Ordered Stages for the Selected Branch (Zero Fabricated Score-Delta Claims!)
    ch7_drop: Optional[str] = None
    for dg in derogatories or ():
        if bool(_get_field(dg, "active", True)) and str(_get_field(dg, "kind", "")).lower() == "bankruptcy_ch7":
            ch7_drop = str(_get_field(dg, "drops_off", "") or "")[:10] or None
            break

    past_due_bal = sum(
        max(0, int(_get_field(ln, "balance_cents", 0) or 0))
        for ln in (credit_lines or ())
        if "past due" in str(_get_field(ln, "name", "") or "").lower()
    )

    score_now_str = (
        f"EX FICO 8: {curr_fico8} · TU Vantage 3.0: {curr_vantage3}"
        if (curr_fico8 and curr_vantage3)
        else (f"Current Score: {curr_fico8 or curr_vantage3}" if (curr_fico8 or curr_vantage3) else "Current Baseline")
    )

    stages_out: List[RoadmapStageOut] = [
        RoadmapStageOut(
            stage_number=0,
            stage_id="stage_0_baseline",
            title="Current Baseline",
            status="current",
            target_date=ref.isoformat(),
            target_util_pct=bridge.current_utilization_pct,
            target_revolving_balance_cents=rev_bal,
            score_band_note=f"{score_now_str} · Utilization {bridge.current_utilization_pct:.1f}%",
            unlock_criteria=[
                f"Revolving balance ${rev_bal / 100:,.2f} / ${rev_lim / 100:,.2f}",
                f"Open EWA advances shielded: ${bridge.ewa_estimated_open_advance_cents / 100:,.2f}",
            ],
            cashflow_shift=f"Active interest burn: ${bridge.live_monthly_revolving_interest_cents / 100:,.2f}/mo",
        ),
        RoadmapStageOut(
            stage_number=1,
            stage_id="stage_1_stabilize",
            title="Stabilize Cash Flow & Delete EWA Apps",
            status="next",
            target_date=active_branch.stabilization_date,
            target_util_pct=88.9 if bridge.current_utilization_pct > 88.9 else bridge.current_utilization_pct,
            target_revolving_balance_cents=(rev_lim * 889) // 1000 if rev_lim > 0 else rev_bal,
            score_band_note="Stabilized Cash Flow · Exit Maxed-Out Band (<88.9%)",
            unlock_criteria=[
                f"Cover ${bridge.ewa_estimated_open_advance_cents / 100:,.2f} unsettled EWA debits & fund ${ewa_pool_target / 100:,.2f} Stabilization Pool",
                "Revoke bank access on data-harvesting EWA apps (Dave, Brigit, Klover) & stop card cash advances",
                (
                    f"Clear ${past_due_bal / 100:,.2f} past-due telecom balance before collection referral"
                    if past_due_bal > 0
                    else "Confirm 100% autopay coverage across all active accounts"
                ),
            ],
            cashflow_shift=f"Start ${active_branch.biweekly_card_payment_cents / 100:,.2f} bi-weekly card autopay",
        ),
        RoadmapStageOut(
            stage_number=2,
            stage_id="stage_2_collection_cascade",
            title="Collection $0 + Freed-Cashflow Cascade",
            status="upcoming",
            target_date=coll_plan.payoff_date if coll_plan is not None else (active_sim["cliff_dates"].get(68.9) or _add_months_date(ref, 5).isoformat()),
            target_util_pct=68.9 if bridge.current_utilization_pct > 68.9 else bridge.current_utilization_pct,
            target_revolving_balance_cents=(rev_lim * 689) // 1000 if rev_lim > 0 else 0,
            score_band_note="FICO 9 / 10T & VantageScore 3.0/4.0 Ignore Paid Collection at $0",
            unlock_criteria=[
                (
                    f"Finish {coll_plan.payments_remaining} bi-weekly ${coll_plan.biweekly_payment_cents / 100:,.2f} payments on {coll_plan.lender_name} + request written Pay-for-Delete"
                    if coll_plan is not None
                    else "Clear High-Drag 68.9% utilization cliff before statement close"
                ),
                "Roll freed collection payment directly into bi-weekly revolving principal",
            ],
            cashflow_shift=(
                f"+${coll_plan.freed_biweekly_cents / 100:,.2f} bi-weekly (+${coll_plan.freed_monthly_cents / 100:,.2f}/mo) cascaded into card"
                if coll_plan is not None
                else "Compound monthly interest reduction accelerates principal"
            ),
        ),
        RoadmapStageOut(
            stage_number=3,
            stage_id="stage_3_sub30",
            title="Sub-30% Standard Cliff (<28.9%) & Trended Deleveraging",
            status="upcoming",
            target_date=active_branch.sub_30_date or _add_months_date(ref, 12).isoformat(),
            target_util_pct=28.9,
            target_revolving_balance_cents=(rev_lim * 289) // 1000 if rev_lim > 0 else 0,
            score_band_note="Good Utilization Band · VantageScore 3.0/4.0 & FICO 9 Approaching 700+",
            unlock_criteria=[
                f"Drive revolving balance <= ${((rev_lim * 289) // 1000) / 100:,.2f} (3 days before statement close)",
                "Maintain 100% on-time streak + zero new hard inquiries for clean 24-month FICO 10T downward slope",
            ],
            cashflow_shift="Monthly revolving interest cut by >70% vs. baseline",
        ),
        RoadmapStageOut(
            stage_number=4,
            stage_id="stage_4_700_azeo",
            title="Peak <8.9% AZEO + $0 Grace Period Restored (700+ Target)",
            status="milestone_700",
            target_date=active_branch.target_700_band_date or _add_months_date(ref, 18).isoformat(),
            target_util_pct=8.9,
            target_revolving_balance_cents=(rev_lim * 89) // 1000 if rev_lim > 0 else 0,
            score_band_note="700+ Profile Eligibility Band (VantageScore 3.0/4.0, FICO 9/10T & Upper FICO 8 Public-Record Tier)",
            unlock_criteria=[
                f"Pay card to $0.00 ({active_branch.zero_balance_date or 'target date'}) to restore 25-day purchase grace period and stop 1% overseas debit FX fees",
                f"Run AZEO: report 1%–4.9% (${((rev_lim * 25) // 1000) / 100:,.2f}) on 1 card at statement close, autopay in full on due date",
            ],
            cashflow_shift=f"+${bridge.live_monthly_revolving_interest_cents / 100:,.2f}/mo interest burn permanently eliminated",
        ),
    ]

    if ch7_drop:
        stages_out.append(
            RoadmapStageOut(
                stage_number=5,
                stage_id="stage_5_clean_scorecard",
                title="Statutory Ch. 7 Drop-Off & Clean Scorecard Graduation",
                status="clean_scorecard",
                target_date=ch7_drop,
                target_util_pct=4.9,
                target_revolving_balance_cents=(rev_lim * 49) // 1000 if rev_lim > 0 else 0,
                score_band_note="Clean Thick-File FICO 8 Scorecard (Prime 740+ Band Eligible with <8.9% AZEO)",
                unlock_criteria=[
                    f"Chapter 7 public record ages off under FCRA (+10y) on {ch7_drop}",
                    "Keep oldest revolving account open with tiny recurring autopay + <8.9% AZEO",
                ],
                cashflow_shift="Eligible for prime auto refinancing & zero-annual-fee credit limit increases",
            )
        )

    # 8. Staged Wishlist Unlock Schedule (Sequential Bi-Weekly Paycheck Funding)
    wishlist_unlocks: List[WishlistUnlockOut] = []
    cursor_date = ref
    if eff_branch == "credit_sprint":
        # Sprint defers non-stabilization gear until <8.9% AZEO milestone
        gear_start_date = (
            date.fromisoformat(active_branch.sub_9_azeo_date)
            if active_branch.sub_9_azeo_date
            else _add_months_date(ref, 12)
        )
    else:
        gear_start_date = ref

    for g in active_goals:
        gid = _get_field(g, "id", None)
        gname = str(_get_field(g, "name", "Goal") or "Goal")
        gcat = str(_get_field(g, "category", "general") or "general").lower()
        gprio = int(_get_field(g, "priority", 2) or 2)
        gtgt = max(0, int(_get_field(g, "target_cents", 0) or 0))
        gsav = max(0, int(_get_field(g, "saved_cents", 0) or 0))
        grem = max(0, gtgt - gsav)
        is_stab = gcat == "emergency" or "stabiliz" in gname.lower() or "ewa" in gname.lower()

        if grem == 0:
            wishlist_unlocks.append(
                WishlistUnlockOut(
                    goal_id=int(gid) if gid is not None else None,
                    goal_name=gname,
                    category=gcat,
                    priority=gprio,
                    target_cents=gtgt,
                    saved_cents=gsav,
                    remaining_cents=0,
                    per_paycheck_cents=0,
                    paychecks_needed=0,
                    unlock_date=ref.isoformat(),
                    status="funded",
                    note="Fully funded and ready.",
                )
            )
            continue

        if is_stab:
            per_chk = 17_500 if eff_branch == "credit_sprint" else stab_slice_balanced
            start_dt = (ref + timedelta(days=upfront_delay_days)) if eff_branch == "gear_upfront" else ref
            p_needed = max(1, int(math.ceil(grem / per_chk)))
            unlock_dt = start_dt + timedelta(days=p_needed * 14)
            cursor_date = max(cursor_date, unlock_dt)
            wishlist_unlocks.append(
                WishlistUnlockOut(
                    goal_id=int(gid) if gid is not None else None,
                    goal_name=gname,
                    category=gcat,
                    priority=gprio,
                    target_cents=gtgt,
                    saved_cents=gsav,
                    remaining_cents=grem,
                    per_paycheck_cents=per_chk,
                    paychecks_needed=p_needed,
                    unlock_date=unlock_dt.isoformat(),
                    status="active_priority",
                    note="Priority #1 foundation: eliminates reliance on data-harvesting EWA paycheck apps permanently.",
                )
            )
        else:
            if eff_branch == "gear_upfront":
                per_chk = grem
                p_needed = 1
                unlock_dt = ref
                status_str = "active_priority"
                note = "Funded immediately upfront (adds interest & delays stabilization — see branch comparison)."
            elif eff_branch == "credit_sprint":
                per_chk = max(15_000, optimal_bw)
                start_dt = max(cursor_date, gear_start_date)
                p_needed = max(1, int(math.ceil(grem / per_chk)))
                unlock_dt = start_dt + timedelta(days=p_needed * 14)
                cursor_date = unlock_dt
                status_str = "queued_after_cliff"
                note = "Queued in cash after <8.9% AZEO sprint clears revolving interest."
            else:
                # Balanced Mission: fund sequentially right after Stabilization Pool using controlled paycheck slice
                per_chk = wishlist_slice_balanced
                p_needed = max(1, int(math.ceil(grem / per_chk)))
                unlock_dt = cursor_date + timedelta(days=p_needed * 14)
                cursor_date = unlock_dt
                status_str = "queued_after_stabilization" if gprio <= 1 else "queued_after_cliff"
                note = (
                    f"Tier 1 core gear: ${per_chk / 100:,.2f}/paycheck right after Stabilization Pool ({p_needed} paychecks, zero credit-card debt added)."
                    if gprio <= 1
                    else f"Tier {gprio} upgrade: unlocks sequentially via ${per_chk / 100:,.2f}/paycheck sinking slice once prior tier completes."
                )

            wishlist_unlocks.append(
                WishlistUnlockOut(
                    goal_id=int(gid) if gid is not None else None,
                    goal_name=gname,
                    category=gcat,
                    priority=gprio,
                    target_cents=gtgt,
                    saved_cents=gsav,
                    remaining_cents=grem,
                    per_paycheck_cents=per_chk,
                    paychecks_needed=p_needed,
                    unlock_date=unlock_dt.isoformat(),
                    status=status_str,  # type: ignore[arg-type]
                    note=note,
                )
            )

    return CreditMissionRoadmap(
        generated_date=ref.isoformat(),
        selected_branch=eff_branch,  # type: ignore[arg-type]
        current_fico8_score=curr_fico8,
        current_vantage3_score=curr_vantage3,
        target_score_goal=700,
        estimated_monthly_income_cents=est_monthly_income,
        estimated_steady_monthly_spend_cents=est_steady_spend,
        splurge_excluded_cents=splurge_excluded,
        splurge_tx_count=splurge_count,
        ewa_exit_pool_target_cents=ewa_pool_target,
        ewa_open_advance_cents=bridge.ewa_estimated_open_advance_cents,
        collection_plan=coll_plan,
        optimal_card_plan=optimal_card_plan,
        branches=branches_out,
        stages=stages_out,
        wishlist_unlocks=wishlist_unlocks,
    )


