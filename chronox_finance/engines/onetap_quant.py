"""
Institutional-Grade Deterministic Statistical & Quantitative One-Tap Engine.

Pure Python standard library (`math`, `statistics`, `decimal`, `random.Random(seed)`)
plus Pydantic v2 — zero `numpy`/`scipy` runtime dependencies so Docker images and
PyInstaller desktop sidecars remain lightweight and deterministic.

Core capabilities:
1. Bayesian-EWMA cashflow & spend-rate estimator + seeded 500-path block-bootstrap
   and log-normal residual tail-risk runway (`compute_statistical_runway`).
2. Robust statistical diagnostics: MAD modified Z-score 14-day outlier detector,
   Mann-Kendall / Theil-Sen weekly spend drift, and category + portfolio HHI
   concentration with cost-basis coverage awareness (`compute_statistical_diagnostics`).
3. Constrained KKT / marginal-utility One-Tap capital optimizer (`build_onetap_plan`)
   producing atomic, reversible actions for `FinanceService` preview, execute, and undo
   while enforcing hard Bills/locked-jar firewalls and the 5% overdraft risk ceiling.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from statistics import mean, stdev
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple

from pydantic import BaseModel, Field

from chronox_finance.engines.autobudget import AutoBudgetResult, iter_effective_tx_allocations
from chronox_finance.engines.credit_debt import (
    compute_effective_min_payment,
    next_occurrence_of_day,
)
from chronox_finance.engines.quant_math import (
    HYSA_BENCHMARK_BPS,
    WEEKDAY_KEYS,
    QuantMathEngine,
    _add_months_clamped,
    _enum_val,
    _fmt,
    _get,
    _step_bill_date,
    _step_payday_date,
    allocate_kkt_concave_cents,
    compute_dow_multipliers,
    compute_ewma_shrunk_rate,
    compute_hhi_index,
    compute_mad_stats,
    compute_mann_kendall_slope,
    compute_modified_z_score,
    huber_mad_clip_series,
    percentile_cents,
    to_dict,
)
from chronox_finance.engines.recurring import detect_payday_schedule, forecast_bill_amount_cents
from chronox_finance.models import (
    CreditOptimizationReport,
    DebtKind,
    JarKind,
    RecurrenceCadence,
    compute_holding_value_cents,
    utc_now_iso,
)

__all__ = [
    "DailyFanPoint",
    "OneTapAction",
    "OneTapActionType",
    "OneTapPlan",
    "OneTapQuantEngine",
    "PortfolioConcentrationTelemetry",
    "SpendAnomaly",
    "SpendDriftTelemetry",
    "StatisticalDiagnostics",
    "StatisticalRunwayResult",
]

OneTapActionType = Literal[
    "rebalance_jar",
    "set_emergency_buffer",
    "set_safe_invest_cap",
    "fund_goal",
    "advisory_debt_azeo",
]

ConcentrationBand = Literal["none", "diversified", "moderate", "concentrated"]
TrendDirection = Literal["accelerating", "stable", "cooling"]

DEFAULT_BOOTSTRAP_PATHS = 500
DEFAULT_RANDOM_SEED = 42
DEFAULT_EWMA_ALPHA = 0.18
MIN_BUFFER_UPDATE_DELTA_CENTS = 2_500  # Avoid noisy sub-$25 buffer adjustments
OVERDRAFT_RISK_CEILING_PCT = 5.0


# --- Pydantic Schemas (ready for Wave 2 re-export in models.py & service.py) ---


class DailyFanPoint(BaseModel):
    """Per-day percentile envelope from the seeded bootstrap cashflow projection."""

    date: str
    p10_balance_cents: int
    p50_balance_cents: int
    p90_balance_cents: int
    scheduled_net_cents: int = 0
    expected_discretionary_cents: int = 0


class StatisticalRunwayResult(BaseModel):
    """Bayesian-EWMA burn estimate + 500-path tail-risk liquidity runway metrics."""

    generated_at: str = Field(default_factory=utc_now_iso)
    horizon_days: int = 30
    n_paths: int = DEFAULT_BOOTSTRAP_PATHS
    seed: int = DEFAULT_RANDOM_SEED
    needs_setup: bool = False
    starting_balance_cents: int = 0
    sample_days: int = 0
    ewma_alpha: float = DEFAULT_EWMA_ALPHA
    ewma_daily_burn_cents: int = 0
    robust_median_daily_burn_cents: int = 0
    shrunk_daily_burn_cents: int = 0
    seasonality_active: bool = False
    dow_multipliers: Dict[str, float] = Field(
        default_factory=lambda: {k: 1.0 for k in WEEKDAY_KEYS}
    )
    p10_ending_cents: int = 0
    p50_ending_cents: int = 0
    p90_ending_cents: int = 0
    p05_nadir_cents: int = 0
    p10_nadir_cents: int = 0
    p50_nadir_cents: int = 0
    p90_nadir_cents: int = 0
    var_95_shortfall_cents: int = 0
    cvar_95_buffer_cents: int = 0
    overdraft_var_95_cents: int = 0
    overdraft_safe_lump_sum_cents: int = 0
    ruin_probability_pct: float = 0.0
    buffer_breach_probability_pct: float = 0.0
    confidence_95_daily_safe_cents: int = 0
    scheduled_inflows_cents: int = 0
    scheduled_outflows_cents: int = 0
    daily_fan_points: List[DailyFanPoint] = Field(default_factory=list)


class SpendAnomaly(BaseModel):
    """Recent 14-day outlier expense flagged via Iglewicz-Hoaglin MAD modified Z-score."""

    tx_id: Optional[int] = None
    tx_date: str
    merchant: str
    category: str
    amount_cents: int
    baseline_median_cents: int
    excess_cents: int
    modified_z: float


class SpendDriftTelemetry(BaseModel):
    """Weekly discretionary spend drift from tie-corrected Mann-Kendall & Theil-Sen slope."""

    weeks_analyzed: int = 0
    drift_cents_per_week: int = 0
    linear_slope_cents_per_week: int = 0
    mann_kendall_s: int = 0
    mann_kendall_z: float = 0.0
    mann_kendall_tau: float = 0.0
    p_value: float = 1.0
    is_significant: bool = False
    trend_direction: TrendDirection = "stable"
    weekly_spend_series_cents: List[int] = Field(default_factory=list)


class PortfolioConcentrationTelemetry(BaseModel):
    """Portfolio HHI concentration (0..10000) and cost-basis coverage telemetry."""

    has_holdings: bool = False
    holdings_count: int = 0
    total_value_cents: int = 0
    symbol_hhi: int = 0
    symbol_concentration_band: ConcentrationBand = "none"
    asset_class_hhi: int = 0
    asset_class_concentration_band: ConcentrationBand = "none"
    top_symbol: Optional[str] = None
    top_symbol_share_pct: float = 0.0
    known_cost_basis_value_cents: int = 0
    unknown_cost_basis_value_cents: int = 0
    known_cost_basis_count: int = 0
    unknown_cost_basis_count: int = 0
    cost_basis_coverage_pct: float = 0.0
    all_cost_basis_known: bool = True
    unrealized_pnl_known_only_cents: Optional[int] = None
    cost_basis_warning: Optional[str] = None


class StatisticalDiagnostics(BaseModel):
    """Regime, drift, anomaly, and HHI concentration diagnostics."""

    generated_at: str = Field(default_factory=utc_now_iso)
    lookback_days: int = 90
    anomaly_window_days: int = 14
    anomalies: List[SpendAnomaly] = Field(default_factory=list)
    drift: SpendDriftTelemetry = Field(default_factory=SpendDriftTelemetry)
    drift_cents_per_week: int = 0
    trend_direction: TrendDirection = "stable"
    spend_hhi: int = 0
    spend_concentration_band: ConcentrationBand = "none"
    top_spend_category: Optional[str] = None
    top_spend_share_pct: float = 0.0
    category_spend_cents: Dict[str, int] = Field(default_factory=dict)
    portfolio: PortfolioConcentrationTelemetry = Field(
        default_factory=PortfolioConcentrationTelemetry
    )


class OneTapAction(BaseModel):
    """Atomic executable or advisory action ready for preview, execution, and undo."""

    action_id: str
    priority_rank: int = 1
    action_type: OneTapActionType
    title: str
    summary: str
    rationale: str
    amount_cents: int = 0
    expected_annual_savings_cents: int = 0
    executable: bool = True
    is_advisory: bool = False
    payload: Dict[str, Any] = Field(default_factory=dict)
    undo_payload: Dict[str, Any] = Field(default_factory=dict)
    safety_checks: List[str] = Field(default_factory=list)


class OneTapPlan(BaseModel):
    """Composite statistical telemetry and ordered One-Tap capital optimization plan."""

    generated_at: str = Field(default_factory=utc_now_iso)
    horizon_days: int = 30
    needs_setup: bool = False
    runway: StatisticalRunwayResult
    diagnostics: StatisticalDiagnostics
    available_unlocked_surplus_cents: int = 0
    allocated_surplus_cents: int = 0
    unallocated_surplus_cents: int = 0
    recommended_emergency_buffer_cents: int = 0
    statistical_safe_to_invest_cents: int = 0
    kkt_shadow_price_lambda: float = 0.0
    total_expected_annual_savings_cents: int = 0
    executable_action_count: int = 0
    advisory_action_count: int = 0
    actions: List[OneTapAction] = Field(default_factory=list)
    summary: str = ""


# --- Internal Pure Helpers ---


def _cents_times_bps_annual(amount_cents: int, bps: int) -> int:
    """Exact integer-cent annual dollar amount via banker's rounding (`amount * bps / 10000`)."""
    if amount_cents <= 0 or bps <= 0:
        return 0
    val = (Decimal(int(amount_cents)) * Decimal(int(bps))) / Decimal(10_000)
    return int(val.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))


def _strict_under_threshold_cap(limit_cents: int, threshold_ratio: Decimal) -> int:
    """
    Return the largest integer cent balance strictly below `limit_cents * threshold_ratio`
    so reported utilization stays strictly under the utilization boundary (`< 8.9%` / `< 28.9%`).
    """
    if limit_cents <= 0:
        return 0
    exact_boundary = Decimal(int(limit_cents)) * threshold_ratio
    cap = int(exact_boundary)
    if Decimal(cap) >= exact_boundary and cap > 0:
        cap -= 1
    return max(0, cap)


def _is_protected_jar(jar: Any) -> bool:
    """Hard firewall: Bills jar and any jar with `is_locked=True` can NEVER be raided."""
    kind = _enum_val(_get(jar, "kind", "")).strip().lower()
    slug = str(_get(jar, "slug", "") or "").strip().lower()
    is_locked = bool(_get(jar, "is_locked", False))
    return is_locked or kind == JarKind.BILLS.value or slug == "bills"


def _resolve_tx_category(tx: Any, jar_by_id: Dict[int, Any]) -> str:
    """Resolve human-readable spend category cleanly from transaction or jar metadata."""
    for field_name in ("category", "subcategory"):
        val = str(_get(tx, field_name, "") or "").strip()
        if val:
            return val.lower()
    jid = _get(tx, "jar_id", None)
    if jid is not None and int(jid) in jar_by_id:
        j = jar_by_id[int(jid)]
        slug = str(_get(j, "slug", "") or "").strip().lower()
        name = str(_get(j, "name", "") or "").strip().lower()
        if slug:
            return slug
        if name:
            return name
    return "general"


def _resolve_effective_apr_bps(credit_line: Any, ref: date) -> int:
    """
    Resolve a credit line's real current APR in basis points without ever fabricating
    a default rate when `apr_bps` is 0 or unset.
    """
    eff = _get(credit_line, "effective_apr_bps", None)
    if eff is not None:
        return max(0, int(eff))

    base_apr = max(0, int(_get(credit_line, "apr_bps", 0) or 0))
    promo_apr = _get(credit_line, "promo_apr_bps", None)
    promo_ends = _get(credit_line, "promo_ends_date", None)
    if promo_apr is not None and promo_ends:
        try:
            end_dt = date.fromisoformat(str(promo_ends)[:10])
            if end_dt >= ref:
                return max(0, int(promo_apr))
        except ValueError:
            pass
    return base_apr


def _extract_non_discretionary_filters(
    jars: Optional[Sequence[Any]],
    recurring_bills: Optional[Sequence[Any]],
) -> Tuple[set[int], set[str]]:
    """Identify non-discretionary jar IDs (Bills, Safety, Invest) and essential bill merchants."""
    non_disc_jar_ids: set[int] = set()
    if jars:
        for j in jars:
            kind = _enum_val(_get(j, "kind", "")).strip().lower()
            slug = str(_get(j, "slug", "") or "").strip().lower()
            if kind in (JarKind.BILLS.value, JarKind.SAFETY.value, JarKind.INVEST.value) or slug in (
                "bills",
                "safety",
                "invest",
            ):
                jid = _get(j, "id", None)
                if jid is not None:
                    non_disc_jar_ids.add(int(jid))

    essential_merchants: set[str] = set()
    if recurring_bills:
        for b in recurring_bills:
            if _get(b, "active", True) and _get(b, "is_essential", True):
                norm = str(_get(b, "normalized_merchant") or "").strip().lower()
                if norm:
                    essential_merchants.add(norm)

    return non_disc_jar_ids, essential_merchants


class OneTapQuantEngine:
    """
    Institutional-grade deterministic statistical & KKT capital optimization engine.

    All monetary quantities are computed strictly in exact integer cents (`int`).
    Uses local seeded `random.Random(seed)` instances so results are 100% reproducible
    and never mutate global RNG state.
    """

    def __init__(self) -> None:
        self._quant = QuantMathEngine()

    # =========================================================================
    # Internal Scheduled Cashflow Skeleton (Bills + Credit Mins + Verified Paydays)
    # =========================================================================

    def _build_scheduled_cashflow_grid(
        self,
        *,
        ref: date,
        horizon: int,
        recurring_bills: Sequence[Any],
        transactions: Sequence[Any],
        credit_lines: Optional[Sequence[Any]],
        payday_cadence_days: int,
        next_payday_date: Optional[str],
        estimated_paycheck_cents: int,
        user_configured_payday: bool,
    ) -> Tuple[List[int], List[int]]:
        """
        Build exact day-by-day `(inflows[0..H-1], outflows[0..H-1])` in integer cents
        covering verified paydays, all 8 recurring bill cadences, and active credit
        line minimum payments without double-counting recently paid bills.
        """
        end_dt = ref + timedelta(days=horizon - 1)
        inflows_by_day: Dict[date, int] = defaultdict(int)
        outflows_by_day: Dict[date, int] = defaultdict(int)

        # --- A. Verified Paychecks / Recurring Income (NEVER invented) ---
        pay_cadence = max(7, int(payday_cadence_days or 14))
        pay_amt = max(0, int(estimated_paycheck_cents or 0))
        pay_start_iso = next_payday_date

        if not user_configured_payday and (pay_amt == 0 or not pay_start_iso) and transactions:
            tx_dicts = [to_dict(t) if not isinstance(t, dict) else t for t in transactions]
            has_income_tx = any(
                t.get("tx_type") == "income" and int(t.get("amount_cents", 0)) >= 20_000
                for t in tx_dicts
            )
            if has_income_tx:
                detected = detect_payday_schedule(
                    transactions=tx_dicts,
                    fallback_cadence_days=pay_cadence,
                    fallback_payday_date=pay_start_iso,
                    fallback_paycheck_cents=pay_amt,
                    today=ref,
                    user_configured_payday=False,
                )
                pay_cadence = detected.cadence_days
                if not pay_start_iso:
                    pay_start_iso = detected.next_payday_date
                if pay_amt == 0:
                    pay_amt = max(0, int(detected.estimated_paycheck_cents))

        if pay_amt > 0:
            if pay_start_iso:
                try:
                    pay_dt = date.fromisoformat(str(pay_start_iso)[:10])
                except ValueError:
                    pay_dt = ref + timedelta(days=max(1, pay_cadence // 2))
            else:
                pay_dt = ref + timedelta(days=max(1, pay_cadence // 2))

            anchor_pay_day = pay_dt.day
            guard = 0
            while pay_dt < ref and guard < 400:
                pay_dt = _step_payday_date(pay_dt, pay_cadence, anchor_pay_day)
                guard += 1
            while pay_dt <= end_dt and guard < 600:
                inflows_by_day[pay_dt] += pay_amt
                pay_dt = _step_payday_date(pay_dt, pay_cadence, anchor_pay_day)
                guard += 1

        # Track merchants paid within the trailing 5 days so bills due today aren't double-counted
        recent_paid_merchants: set[str] = set()
        for tx in transactions:
            if _enum_val(_get(tx, "tx_type")) != "expense":
                continue
            tx_date_str = str(_get(tx, "tx_date", ""))[:10]
            try:
                tx_dt = date.fromisoformat(tx_date_str)
            except ValueError:
                continue
            if 0 <= (ref - tx_dt).days <= 5:
                norm = str(_get(tx, "normalized_merchant") or "").strip().lower()
                if norm:
                    recent_paid_merchants.add(norm)

        # --- B. Recurring Bills across all 8 cadences ---
        for bill in recurring_bills:
            if not _get(bill, "active", True):
                continue
            bill_map = bill if isinstance(bill, dict) else to_dict(bill)
            amt = forecast_bill_amount_cents(bill_map)
            if amt <= 0:
                continue
            due_str = str(_get(bill, "next_due_date", ""))[:10]
            try:
                due_dt = date.fromisoformat(due_str)
            except ValueError:
                continue
            cadence_raw = _enum_val(_get(bill, "cadence", RecurrenceCadence.MONTHLY.value))
            try:
                cadence = RecurrenceCadence(cadence_raw)
            except ValueError:
                cadence = RecurrenceCadence.MONTHLY

            norm = str(_get(bill, "normalized_merchant") or "").strip().lower()
            anchor_day = due_dt.day
            overdue_days = (ref - due_dt).days

            if overdue_days > 0:
                if overdue_days <= 5 and norm not in recent_paid_merchants:
                    outflows_by_day[ref] += amt
                guard = 0
                while due_dt <= ref and guard < 400:
                    due_dt = _step_bill_date(due_dt, cadence, anchor_day)
                    guard += 1
            elif overdue_days == 0 and norm in recent_paid_merchants:
                due_dt = _step_bill_date(due_dt, cadence, anchor_day)

            guard = 0
            while due_dt <= end_dt and guard < 400:
                if due_dt >= ref:
                    outflows_by_day[due_dt] += amt
                due_dt = _step_bill_date(due_dt, cadence, anchor_day)
                guard += 1

        # --- C. Credit Line Minimum Payments on `payment_due_day` ---
        if credit_lines:
            for cl in credit_lines:
                if not _get(cl, "active", True):
                    continue
                bal = max(0, int(_get(cl, "balance_cents", 0) or 0))
                if bal <= 0:
                    continue
                kind_str = _enum_val(_get(cl, "kind", DebtKind.CREDIT_CARD.value))
                try:
                    kind = DebtKind(kind_str)
                except ValueError:
                    kind = DebtKind.CREDIT_CARD

                eff_min = _get(cl, "effective_min_payment_cents", None)
                if eff_min is None:
                    apr_bps = _resolve_effective_apr_bps(cl, ref)
                    eff_min = compute_effective_min_payment(
                        bal,
                        int(_get(cl, "min_payment_cents", 0) or 0),
                        kind,
                        apr_bps,
                    )
                eff_min = max(0, int(eff_min))
                if eff_min <= 0:
                    continue

                due_day = int(_get(cl, "payment_due_day", 10) or 10)
                rem_bal = bal
                due_dt = next_occurrence_of_day(due_day, ref)
                step_idx = 0
                while due_dt <= end_dt and rem_bal > 0 and step_idx < 24:
                    pay = min(rem_bal, eff_min)
                    outflows_by_day[due_dt] += pay
                    rem_bal -= pay
                    step_idx += 1
                    due_dt = _add_months_clamped(due_dt, 1, due_day)

        inflows = [inflows_by_day.get(ref + timedelta(days=i), 0) for i in range(horizon)]
        outflows = [outflows_by_day.get(ref + timedelta(days=i), 0) for i in range(horizon)]
        return inflows, outflows

    # =========================================================================
    # 1. Bayesian-EWMA Burn Estimator + 500-Path Tail-Risk Runway
    # =========================================================================

    def compute_statistical_runway(
        self,
        *,
        starting_balance_cents: int,
        recurring_bills: Sequence[Any] = (),
        transactions: Sequence[Any] = (),
        jars: Optional[Sequence[Any]] = None,
        credit_lines: Optional[Sequence[Any]] = None,
        splits_by_tx_id: Optional[Dict[int, List[Any]]] = None,
        payday_cadence_days: int = 14,
        next_payday_date: Optional[str] = None,
        estimated_paycheck_cents: int = 0,
        user_configured_payday: bool = False,
        emergency_buffer_cents: int = 0,
        horizon_days: int = 30,
        days: Optional[int] = None,
        n_paths: int = DEFAULT_BOOTSTRAP_PATHS,
        seed: int = DEFAULT_RANDOM_SEED,
        ewma_alpha: float = DEFAULT_EWMA_ALPHA,
        lookback_days: int = 90,
        today: Optional[date] = None,
    ) -> StatisticalRunwayResult:
        """
        Estimate daily discretionary burn with Huber/MAD-clipped Bayesian-EWMA (`alpha=0.18`)
        and day-of-week seasonality (`>= 21` days), then project a deterministic seeded
        (`random.Random(seed)`) 500-path block-bootstrap / log-normal residual cashflow
        envelope layered over exact scheduled bills, credit minimums, and verified paydays.
        """
        ref = today or date.today()
        req_horizon = days if days is not None else horizon_days
        horizon = max(1, min(90, int(req_horizon)))
        paths_n = max(50, min(5000, int(n_paths)))
        seed_val = int(seed)
        alpha = min(0.95, max(0.01, float(ewma_alpha)))
        start_bal = int(starting_balance_cents)
        buf_floor = max(0, int(emergency_buffer_cents))

        # --- Fresh $0 Profile Invariant ---
        has_income_tx = any(
            _enum_val(_get(t, "tx_type")) == "income" and int(_get(t, "amount_cents", 0) or 0) > 0
            for t in transactions
        )
        has_configured_income = int(estimated_paycheck_cents or 0) > 0
        if start_bal == 0 and len(transactions) == 0 and not has_income_tx and not has_configured_income:
            zero_fan = [
                DailyFanPoint(
                    date=(ref + timedelta(days=i)).isoformat(),
                    p10_balance_cents=0,
                    p50_balance_cents=0,
                    p90_balance_cents=0,
                    scheduled_net_cents=0,
                    expected_discretionary_cents=0,
                )
                for i in range(horizon)
            ]
            return StatisticalRunwayResult(
                generated_at=utc_now_iso(),
                horizon_days=horizon,
                n_paths=paths_n,
                seed=seed_val,
                needs_setup=True,
                starting_balance_cents=0,
                sample_days=0,
                ewma_alpha=alpha,
                daily_fan_points=zero_fan,
            )

        # --- A. Exact Scheduled Cashflow Skeleton ---
        sched_in, sched_out = self._build_scheduled_cashflow_grid(
            ref=ref,
            horizon=horizon,
            recurring_bills=recurring_bills,
            transactions=transactions,
            credit_lines=credit_lines,
            payday_cadence_days=payday_cadence_days,
            next_payday_date=next_payday_date,
            estimated_paycheck_cents=estimated_paycheck_cents,
            user_configured_payday=user_configured_payday,
        )
        sched_net = [sched_in[i] - sched_out[i] for i in range(horizon)]
        sched_balances: List[int] = []
        run_sched = start_bal
        for i in range(horizon):
            run_sched += sched_net[i]
            sched_balances.append(run_sched)

        # --- B. Trailing 90-Day Split-Aware Discretionary Burn + Seasonality ---
        window_days = max(7, min(365, int(lookback_days)))
        window_start = ref - timedelta(days=window_days - 1)
        non_disc_jar_ids, essential_merchants = _extract_non_discretionary_filters(
            jars, recurring_bills
        )
        disc_by_day, earliest_disc = self._quant._collect_split_aware_discretionary(
            transactions=transactions,
            window_start=window_start,
            ref=ref,
            non_disc_jar_ids=non_disc_jar_ids,
            essential_merchants=essential_merchants,
            splits_by_tx_id=splits_by_tx_id,
        )

        hist_dates: List[date] = []
        hist_daily: List[float] = []
        clipped_daily: List[float] = []
        sample_days = 0
        shrunk_daily = 0.0
        ewma_daily = 0.0
        robust_med_daily = 0.0
        dow_mults: Dict[str, float] = {k: 1.0 for k in WEEKDAY_KEYS}
        seasonality_active = False

        if earliest_disc is not None:
            sample_days = max(1, min(window_days, (ref - earliest_disc).days + 1))
            hist_dates = [earliest_disc + timedelta(days=offset) for offset in range(sample_days)]
            hist_daily = [float(disc_by_day.get(d, 0)) for d in hist_dates]
            clipped_daily = huber_mad_clip_series(hist_daily, k=2.5)
            shrunk_daily, ewma_daily, robust_med_daily = compute_ewma_shrunk_rate(
                hist_daily,
                alpha=alpha,
                prior_days=14.0,
                max_ewma_weight=0.65,
                huber_k=2.5,
            )
            dow_mults, seasonality_active = compute_dow_multipliers(
                hist_dates,
                clipped_daily,
                min_days=21,
                prior_weeks=3.0,
            )

        dow_mult_list = [dow_mults[WEEKDAY_KEYS[wd]] for wd in range(7)]
        forecast_dates = [ref + timedelta(days=i) for i in range(horizon)]
        forecast_wds = [d.weekday() for d in forecast_dates]
        expected_disc_daily = [
            int(round(shrunk_daily * dow_mult_list[wd])) if shrunk_daily > 0.0 else 0
            for wd in forecast_wds
        ]

        # --- C. Seeded 500-Path Block-Bootstrap / Log-Normal Residual Paths ---
        rng = random.Random(seed_val)  # noqa: S311 - deterministic statistical bootstrap, not crypto
        ending_balances: List[int] = [0] * paths_n
        nadir_balances: List[int] = [0] * paths_n
        liquidity_dips: List[int] = [0] * paths_n
        overdraft_count = 0
        buffer_breach_count = 0
        day_balances_matrix: List[List[int]] = [[0] * paths_n for _ in range(horizon)]
        spend_paths: List[List[int]] = [[0] * horizon for _ in range(paths_n)]

        # Prepare block-bootstrap or parametric log-normal parameters
        use_dow_blocks = sample_days >= 21 and shrunk_daily > 0.0
        use_seq_blocks = (14 <= sample_days < 21) and shrunk_daily > 0.0
        use_parametric = (1 <= sample_days < 14) and shrunk_daily > 0.0

        week_profiles: List[Tuple[float, ...]] = []
        seq_blocks: List[Tuple[float, ...]] = []
        scale_ratio = 1.0
        sigma_ln = 0.0

        if use_dow_blocks:
            for s_idx in range(sample_days - 6):
                prof = [0.0] * 7
                for k in range(7):
                    prof[hist_dates[s_idx + k].weekday()] = clipped_daily[s_idx + k]
                week_profiles.append(tuple(prof))
            block_sums = [sum(p) for p in week_profiles]
            mean_block_daily = (mean(block_sums) / 7.0) if block_sums else 0.0
            if mean_block_daily > 0.0:
                scale_ratio = min(2.0, max(0.5, shrunk_daily / mean_block_daily))
            if len(block_sums) >= 2 and mean(block_sums) > 0.0:
                block_cv = stdev(block_sums) / mean(block_sums)
                sigma_ln = min(0.25, 0.35 * block_cv) if block_cv > 1e-9 else 0.0
        elif use_seq_blocks:
            for s_idx in range(sample_days - 6):
                seq_blocks.append(tuple(clipped_daily[s_idx : s_idx + 7]))
            block_sums = [sum(p) for p in seq_blocks]
            mean_block_daily = (mean(block_sums) / 7.0) if block_sums else 0.0
            if mean_block_daily > 0.0:
                scale_ratio = min(2.0, max(0.5, shrunk_daily / mean_block_daily))
            if len(block_sums) >= 2 and mean(block_sums) > 0.0:
                block_cv = stdev(block_sums) / mean(block_sums)
                sigma_ln = min(0.30, 0.40 * block_cv) if block_cv > 1e-9 else 0.0
        elif use_parametric:
            if len(clipped_daily) >= 4 and stdev(clipped_daily) == 0.0:
                sigma_ln = 0.0
            else:
                emp_mean = mean(clipped_daily) if clipped_daily else 0.0
                emp_cv = (
                    (stdev(clipped_daily) / emp_mean)
                    if len(clipped_daily) >= 2 and emp_mean > 0.0
                    else 0.35
                )
                cv_clamped = min(1.25, max(0.15, emp_cv))
                sigma_ln = math.sqrt(math.log(1.0 + cv_clamped * cv_clamped))

        half_sigma_sq = 0.5 * sigma_ln * sigma_ln
        n_dow_blocks = len(week_profiles)
        n_seq_blocks = len(seq_blocks)

        for p in range(paths_n):
            bal = start_bal
            running_peak = start_bal
            nadir = start_bal
            max_dip = 0
            had_overdraft = False
            had_buf_breach = False
            p_spends = spend_paths[p]

            if use_dow_blocks:
                cur_prof = week_profiles[rng.randrange(n_dow_blocks)]  # noqa: S311
                cur_mult = (
                    math.exp(sigma_ln * rng.gauss(0.0, 1.0) - half_sigma_sq)  # noqa: S311
                    if sigma_ln > 0.0
                    else 1.0
                )
                for t in range(horizon):
                    if t > 0 and (t % 7 == 0):
                        cur_prof = week_profiles[rng.randrange(n_dow_blocks)]  # noqa: S311
                        cur_mult = (
                            math.exp(sigma_ln * rng.gauss(0.0, 1.0) - half_sigma_sq)  # noqa: S311
                            if sigma_ln > 0.0
                            else 1.0
                        )
                    disc = int(round(cur_prof[forecast_wds[t]] * scale_ratio * cur_mult))
                    p_spends[t] = disc
                    bal = bal + sched_net[t] - disc
                    day_balances_matrix[t][p] = bal
                    if t == 0 or bal < nadir:
                        nadir = bal
                    if bal > running_peak:
                        running_peak = bal
                    dip = running_peak - bal
                    if dip > max_dip:
                        max_dip = dip
                    if bal < 0:
                        had_overdraft = True
                    if buf_floor > 0 and bal < buf_floor:
                        had_buf_breach = True
            elif use_seq_blocks:
                cur_blk = seq_blocks[rng.randrange(n_seq_blocks)]  # noqa: S311
                cur_mult = (
                    math.exp(sigma_ln * rng.gauss(0.0, 1.0) - half_sigma_sq)  # noqa: S311
                    if sigma_ln > 0.0
                    else 1.0
                )
                for t in range(horizon):
                    offset = t % 7
                    if t > 0 and offset == 0:
                        cur_blk = seq_blocks[rng.randrange(n_seq_blocks)]  # noqa: S311
                        cur_mult = (
                            math.exp(sigma_ln * rng.gauss(0.0, 1.0) - half_sigma_sq)  # noqa: S311
                            if sigma_ln > 0.0
                            else 1.0
                        )
                    disc = int(round(cur_blk[offset] * scale_ratio * cur_mult))
                    p_spends[t] = disc
                    bal = bal + sched_net[t] - disc
                    day_balances_matrix[t][p] = bal
                    if t == 0 or bal < nadir:
                        nadir = bal
                    if bal > running_peak:
                        running_peak = bal
                    dip = running_peak - bal
                    if dip > max_dip:
                        max_dip = dip
                    if bal < 0:
                        had_overdraft = True
                    if buf_floor > 0 and bal < buf_floor:
                        had_buf_breach = True
            elif use_parametric:
                for t in range(horizon):
                    base = shrunk_daily * dow_mult_list[forecast_wds[t]]
                    shock = (
                        math.exp(sigma_ln * rng.gauss(0.0, 1.0) - half_sigma_sq)  # noqa: S311
                        if sigma_ln > 0.0
                        else 1.0
                    )
                    disc = int(round(base * shock))
                    p_spends[t] = disc
                    bal = bal + sched_net[t] - disc
                    day_balances_matrix[t][p] = bal
                    if t == 0 or bal < nadir:
                        nadir = bal
                    if bal > running_peak:
                        running_peak = bal
                    dip = running_peak - bal
                    if dip > max_dip:
                        max_dip = dip
                    if bal < 0:
                        had_overdraft = True
                    if buf_floor > 0 and bal < buf_floor:
                        had_buf_breach = True
            else:
                # Zero discretionary history: exact scheduled cashflow trajectory
                for t in range(horizon):
                    bal = bal + sched_net[t]
                    day_balances_matrix[t][p] = bal
                    if t == 0 or bal < nadir:
                        nadir = bal
                    if bal > running_peak:
                        running_peak = bal
                    dip = running_peak - bal
                    if dip > max_dip:
                        max_dip = dip
                    if bal < 0:
                        had_overdraft = True
                    if buf_floor > 0 and bal < buf_floor:
                        had_buf_breach = True

            ending_balances[p] = bal
            nadir_balances[p] = nadir
            liquidity_dips[p] = max(0, max_dip)
            if had_overdraft:
                overdraft_count += 1
            if had_buf_breach:
                buffer_breach_count += 1

        # --- D. Exact Percentile Envelope, 95% VaR & 95% CVaR (Expected Shortfall) ---
        p10_ending = percentile_cents(ending_balances, 0.10)
        p50_ending = percentile_cents(ending_balances, 0.50)
        p90_ending = percentile_cents(ending_balances, 0.90)

        sorted_nadirs = sorted(nadir_balances)
        p05_nadir = percentile_cents(sorted_nadirs, 0.05)
        p10_nadir = percentile_cents(sorted_nadirs, 0.10)
        p50_nadir = percentile_cents(sorted_nadirs, 0.50)
        p90_nadir = percentile_cents(sorted_nadirs, 0.90)

        allowed_overdrafts = int(math.floor(0.05 * paths_n))
        overdraft_safe_lump_sum = max(0, sorted_nadirs[min(paths_n - 1, allowed_overdrafts)])

        sorted_dips = sorted(liquidity_dips)
        var_95_shortfall = percentile_cents(sorted_dips, 0.95)
        tail_count = max(1, int(math.ceil(0.05 * paths_n)))
        cvar_raw = int(round(mean(sorted_dips[-tail_count:])))
        cvar_95_buffer = max(var_95_shortfall, cvar_raw)
        overdraft_var_95 = max(0, -p05_nadir)

        ruin_prob_pct = round((overdraft_count / float(paths_n)) * 100.0, 2)
        buf_breach_pct = round((buffer_breach_count / float(paths_n)) * 100.0, 2)

        # --- E. Exact Max Daily Discretionary Spend Keeping P(overdraft) <= 5% ---
        confidence_95_daily_safe = self._solve_confidence_95_daily_safe_cents(
            sched_balances=sched_balances,
            spend_paths=spend_paths,
            shrunk_daily=shrunk_daily,
            paths_n=paths_n,
            horizon=horizon,
        )

        # Build per-day percentile fan points
        daily_fan_points = [
            DailyFanPoint(
                date=forecast_dates[t].isoformat(),
                p10_balance_cents=percentile_cents(day_balances_matrix[t], 0.10),
                p50_balance_cents=percentile_cents(day_balances_matrix[t], 0.50),
                p90_balance_cents=percentile_cents(day_balances_matrix[t], 0.90),
                scheduled_net_cents=sched_net[t],
                expected_discretionary_cents=expected_disc_daily[t],
            )
            for t in range(horizon)
        ]

        return StatisticalRunwayResult(
            generated_at=utc_now_iso(),
            horizon_days=horizon,
            n_paths=paths_n,
            seed=seed_val,
            needs_setup=False,
            starting_balance_cents=start_bal,
            sample_days=sample_days,
            ewma_alpha=alpha,
            ewma_daily_burn_cents=int(round(ewma_daily)),
            robust_median_daily_burn_cents=int(round(robust_med_daily)),
            shrunk_daily_burn_cents=int(round(shrunk_daily)),
            seasonality_active=seasonality_active,
            dow_multipliers=dow_mults,
            p10_ending_cents=p10_ending,
            p50_ending_cents=p50_ending,
            p90_ending_cents=p90_ending,
            p05_nadir_cents=p05_nadir,
            p10_nadir_cents=p10_nadir,
            p50_nadir_cents=p50_nadir,
            p90_nadir_cents=p90_nadir,
            var_95_shortfall_cents=var_95_shortfall,
            cvar_95_buffer_cents=cvar_95_buffer,
            overdraft_var_95_cents=overdraft_var_95,
            overdraft_safe_lump_sum_cents=overdraft_safe_lump_sum,
            ruin_probability_pct=ruin_prob_pct,
            buffer_breach_probability_pct=buf_breach_pct,
            confidence_95_daily_safe_cents=confidence_95_daily_safe,
            scheduled_inflows_cents=sum(sched_in),
            scheduled_outflows_cents=sum(sched_out),
            daily_fan_points=daily_fan_points,
        )

    @staticmethod
    def _solve_confidence_95_daily_safe_cents(
        *,
        sched_balances: Sequence[int],
        spend_paths: Sequence[Sequence[int]],
        shrunk_daily: float,
        paths_n: int,
        horizon: int,
    ) -> int:
        """
        Compute the exact maximum daily discretionary spend (`int` cents/day) that keeps
        the empirical overdraft probability across all bootstrap paths strictly `<= 5.0%`.
        """
        if not sched_balances or min(sched_balances) < 0:
            return 0

        allowed_overdrafts = int(math.floor(0.05 * paths_n))

        # No historical discretionary burn: exact deterministic unit-factor limit
        if shrunk_daily <= 0.0:
            cap_float = min(sched_balances[t] / float(t + 1) for t in range(horizon))
            return max(0, int(math.floor(cap_float)))

        inv_burn = 1.0 / shrunk_daily
        path_caps: List[float] = [0.0] * paths_n
        has_nonzero_factor = False

        for p in range(paths_n):
            p_spends = spend_paths[p]
            cum_factor = 0.0
            min_ratio = math.inf
            for t in range(horizon):
                cum_factor += p_spends[t] * inv_burn
                if cum_factor > 0.0:
                    has_nonzero_factor = True
                    ratio = sched_balances[t] / cum_factor
                    if ratio < min_ratio:
                        min_ratio = ratio
            if math.isinf(min_ratio):
                min_ratio = min(sched_balances[t] / float(t + 1) for t in range(horizon))
            path_caps[p] = max(0.0, min_ratio)

        if not has_nonzero_factor:
            cap_float = min(sched_balances[t] / float(t + 1) for t in range(horizon))
            return max(0, int(math.floor(cap_float)))

        path_caps.sort()
        idx = min(paths_n - 1, allowed_overdrafts)
        candidate = max(0, int(math.floor(path_caps[idx])))

        # Exact discrete integer-cent verification guard (handles rare 1-cent rounding edges)
        def _count_overdrafts(daily_cents: int) -> int:
            if daily_cents <= 0:
                return 0
            ratio_scale = daily_cents * inv_burn
            od = 0
            for p in range(paths_n):
                p_spends = spend_paths[p]
                cum_spend = 0
                for t in range(horizon):
                    cum_spend += int(round(p_spends[t] * ratio_scale))
                    if sched_balances[t] - cum_spend < 0:
                        od += 1
                        break
            return od

        while candidate > 0 and _count_overdrafts(candidate) > allowed_overdrafts:
            candidate -= 1

        return max(0, candidate)

    # =========================================================================
    # 2. Robust Statistical Regime, Drift & Concentration Diagnostics
    # =========================================================================

    def compute_statistical_diagnostics(
        self,
        *,
        transactions: Sequence[Any] = (),
        jars: Optional[Sequence[Any]] = None,
        recurring_bills: Optional[Sequence[Any]] = None,
        holdings: Optional[Sequence[Any]] = None,
        splits_by_tx_id: Optional[Dict[int, List[Any]]] = None,
        lookback_days: int = 90,
        anomaly_window_days: int = 14,
        modified_z_threshold: float = 3.5,
        today: Optional[date] = None,
    ) -> StatisticalDiagnostics:
        """
        Compute robust spending and portfolio statistical diagnostics:
        - 14-day MAD Modified Z-score outlier expense detector (`modified_z >= 3.5`)
        - Tie-corrected Mann-Kendall + Theil-Sen weekly spending drift
        - Category spend Herfindahl-Hirschman Index (`0..10000`)
        - Portfolio symbol & asset-class HHI concentration + `cost_basis_known` coverage
        """
        ref = today or date.today()
        lb_days = max(14, min(365, int(lookback_days)))
        anom_days = max(1, min(lb_days, int(anomaly_window_days)))
        window_start = ref - timedelta(days=lb_days - 1)
        anomaly_start = ref - timedelta(days=anom_days - 1)

        jar_by_id: Dict[int, Any] = {}
        if jars:
            for j in jars:
                jid = _get(j, "id", None)
                if jid is not None:
                    jar_by_id[int(jid)] = j

        # Map active recurring bills by normalized merchant so normal fixed bills aren't false anomalies
        bill_baseline_by_merchant: Dict[str, int] = {}
        if recurring_bills:
            for b in recurring_bills:
                if not _get(b, "active", True):
                    continue
                norm = str(_get(b, "normalized_merchant") or "").strip().lower()
                if not norm:
                    continue
                b_map = b if isinstance(b, dict) else to_dict(b)
                amt = forecast_bill_amount_cents(b_map)
                if amt > 0:
                    bill_baseline_by_merchant[norm] = max(
                        bill_baseline_by_merchant.get(norm, 0), amt
                    )

        # --- A. Collect Trailing Expense Records & Category Spend Totals ---
        expense_records: List[Tuple[date, Optional[int], str, str, str, int]] = []
        category_amounts: Dict[str, List[int]] = defaultdict(list)
        category_spend_totals: Dict[str, int] = defaultdict(int)
        all_non_bill_amounts: List[int] = []

        for tx in transactions:
            if _enum_val(_get(tx, "tx_type")) != "expense":
                continue
            dt_str = str(_get(tx, "tx_date", ""))[:10]
            try:
                tx_dt = date.fromisoformat(dt_str)
            except ValueError:
                continue
            if tx_dt < window_start or tx_dt > ref:
                continue

            tx_amt = int(_get(tx, "amount_cents", 0) or 0)
            if tx_amt <= 0:
                continue

            tx_id_raw = _get(tx, "id", None)
            tx_id = int(tx_id_raw) if tx_id_raw is not None else None
            merchant = str(_get(tx, "merchant") or "Unknown Merchant").strip()
            norm_merch = str(_get(tx, "normalized_merchant") or "").strip().lower()
            base_cat = _resolve_tx_category(tx, jar_by_id)

            # Split-aware category spend aggregation for HHI (zero double-counting)
            eff_allocs = iter_effective_tx_allocations(tx, splits_by_tx_id)
            if len(eff_allocs) > 1:
                for sp_jid, _sp_gid, sp_amt, _sp_memo in eff_allocs:
                    if sp_jid is not None and sp_jid in jar_by_id:
                        sp_j = jar_by_id[sp_jid]
                        sp_cat = (
                            str(_get(sp_j, "slug") or _get(sp_j, "name") or base_cat)
                            .strip()
                            .lower()
                        )
                    else:
                        sp_cat = base_cat
                    category_spend_totals[sp_cat] += sp_amt
            else:
                category_spend_totals[base_cat] += tx_amt

            # Skip normal expected recurring bill charges from outlier detection (still catch >1.25x bill spikes)
            expected_bill = bill_baseline_by_merchant.get(norm_merch, 0)
            is_expected_fixed_bill = expected_bill > 0 and tx_amt <= int(round(expected_bill * 1.15))

            expense_records.append((tx_dt, tx_id, merchant, norm_merch, base_cat, tx_amt))
            if not is_expected_fixed_bill:
                category_amounts[base_cat].append(tx_amt)
                all_non_bill_amounts.append(tx_amt)

        # --- B. MAD Modified Z-Score Anomaly Detector (Trailing 14 Days) ---
        anomalies: List[SpendAnomaly] = []
        z_thresh = float(modified_z_threshold)

        for tx_dt, tx_id, merchant, norm_merch, cat, tx_amt in expense_records:
            if tx_dt < anomaly_start:
                continue
            expected_bill = bill_baseline_by_merchant.get(norm_merch, 0)
            if expected_bill > 0 and tx_amt <= int(round(expected_bill * 1.15)):
                continue

            pool = category_amounts.get(cat, [])
            if len(pool) < 4:
                pool = all_non_bill_amounts
            if len(pool) < 4:
                continue

            med_val, mad_val = compute_mad_stats(pool)
            med_cents = int(round(med_val))
            excess = tx_amt - med_cents
            if excess < 500:
                continue

            if mad_val > 0.0:
                mod_z = compute_modified_z_score(tx_amt, med_val, mad_val)
            else:
                mean_ad = sum(abs(v - med_val) for v in pool) / float(len(pool))
                # Guard constant baselines against tiny-noise false positives
                if excess < max(1_500, int(round(0.50 * med_val))):
                    continue
                eff_sigma = max(1.253314 * mean_ad, 0.15 * med_val, 500.0)
                mod_z = (tx_amt - med_val) / eff_sigma

            if mod_z >= z_thresh:
                anomalies.append(
                    SpendAnomaly(
                        tx_id=tx_id,
                        tx_date=tx_dt.isoformat(),
                        merchant=merchant,
                        category=cat,
                        amount_cents=tx_amt,
                        baseline_median_cents=med_cents,
                        excess_cents=excess,
                        modified_z=round(mod_z, 2),
                    )
                )

        anomalies.sort(key=lambda a: (-a.modified_z, -a.amount_cents, a.tx_date))

        # --- C. Mann-Kendall / Theil-Sen Weekly Discretionary Spend Drift ---
        non_disc_jar_ids, essential_merchants = _extract_non_discretionary_filters(
            jars, recurring_bills
        )
        disc_by_day, earliest_disc = self._quant._collect_split_aware_discretionary(
            transactions=transactions,
            window_start=window_start,
            ref=ref,
            non_disc_jar_ids=non_disc_jar_ids,
            essential_merchants=essential_merchants,
            splits_by_tx_id=splits_by_tx_id,
        )

        weekly_series: List[int] = []
        if earliest_disc is not None:
            active_span = min(lb_days, (ref - earliest_disc).days + 1)
            complete_weeks = active_span // 7
            # Anchor complete 7-day blocks backward from `ref`, ordered oldest -> newest
            for w_idx in range(complete_weeks - 1, -1, -1):
                w_end = ref - timedelta(days=w_idx * 7)
                w_start = w_end - timedelta(days=6)
                w_total = sum(
                    amt for dt, amt in disc_by_day.items() if w_start <= dt <= w_end
                )
                weekly_series.append(int(w_total))

        mk = compute_mann_kendall_slope(weekly_series)
        drift_telemetry = SpendDriftTelemetry(
            weeks_analyzed=mk.weeks_analyzed,
            drift_cents_per_week=mk.drift_cents_per_week,
            linear_slope_cents_per_week=mk.linear_slope_cents_per_week,
            mann_kendall_s=mk.mann_kendall_s,
            mann_kendall_z=mk.mann_kendall_z,
            mann_kendall_tau=mk.mann_kendall_tau,
            p_value=mk.p_value,
            is_significant=mk.is_significant,
            trend_direction=mk.trend_direction,  # type: ignore[arg-type]
            weekly_spend_series_cents=weekly_series,
        )

        # --- D. Category Spend Herfindahl-Hirschman Index (0..10000) ---
        clean_cat_totals = {k: int(v) for k, v in category_spend_totals.items() if int(v) > 0}
        spend_hhi, spend_band = compute_hhi_index(list(clean_cat_totals.values()))
        total_cat_spend = sum(clean_cat_totals.values())
        top_cat: Optional[str] = None
        top_cat_share = 0.0
        if clean_cat_totals and total_cat_spend > 0:
            top_cat, top_amt = max(clean_cat_totals.items(), key=lambda kv: (kv[1], kv[0]))
            top_cat_share = round((top_amt / float(total_cat_spend)) * 100.0, 2)

        # --- E. Portfolio Asset/Symbol HHI + Cost-Basis Coverage Awareness ---
        portfolio_tel = self._compute_portfolio_concentration(holdings)

        return StatisticalDiagnostics(
            generated_at=utc_now_iso(),
            lookback_days=lb_days,
            anomaly_window_days=anom_days,
            anomalies=anomalies,
            drift=drift_telemetry,
            drift_cents_per_week=drift_telemetry.drift_cents_per_week,
            trend_direction=drift_telemetry.trend_direction,
            spend_hhi=spend_hhi,
            spend_concentration_band=spend_band,  # type: ignore[arg-type]
            top_spend_category=top_cat,
            top_spend_share_pct=top_cat_share,
            category_spend_cents=clean_cat_totals,
            portfolio=portfolio_tel,
        )

    @staticmethod
    def _compute_portfolio_concentration(
        holdings: Optional[Sequence[Any]],
    ) -> PortfolioConcentrationTelemetry:
        """Compute portfolio symbol/asset-class HHI and honest `cost_basis_known` coverage."""
        if not holdings:
            return PortfolioConcentrationTelemetry()

        by_symbol: Dict[str, int] = defaultdict(int)
        by_class: Dict[str, int] = defaultdict(int)
        total_val = 0
        known_val = 0
        unknown_val = 0
        known_cnt = 0
        unknown_cnt = 0
        known_cost_basis_sum = 0
        valid_count = 0

        for h in holdings:
            raw_mv = _get(h, "market_value_cents", None)
            if raw_mv is not None:
                mv = max(0, int(raw_mv))
            else:
                qty_e8 = int(_get(h, "quantity_e8", 0) or 0)
                px_e8 = int(_get(h, "price_e8", 0) or 0)
                mv = max(0, compute_holding_value_cents(qty_e8, px_e8))
            if mv <= 0:
                continue

            valid_count += 1
            total_val += mv
            sym = str(_get(h, "symbol", "UNKNOWN") or "UNKNOWN").strip().upper()
            aclass = str(_get(h, "asset_class", "equity") or "equity").strip().lower()
            by_symbol[sym] += mv
            by_class[aclass] += mv

            raw_cb = _get(h, "cost_basis_cents", None)
            raw_cb_known = _get(h, "cost_basis_known", None)
            cb_known = (raw_cb is not None) if raw_cb_known is None else bool(raw_cb_known)

            if cb_known and raw_cb is not None:
                known_cnt += 1
                known_val += mv
                known_cost_basis_sum += max(0, int(raw_cb))
            else:
                unknown_cnt += 1
                unknown_val += mv

        if valid_count == 0 or total_val <= 0:
            return PortfolioConcentrationTelemetry()

        sym_hhi, sym_band = compute_hhi_index(list(by_symbol.values()))
        cls_hhi, cls_band = compute_hhi_index(list(by_class.values()))
        top_sym, top_sym_val = max(by_symbol.items(), key=lambda kv: (kv[1], kv[0]))
        top_sym_share = round((top_sym_val / float(total_val)) * 100.0, 2)
        coverage_pct = round((known_val / float(total_val)) * 100.0, 2)
        all_known = unknown_cnt == 0
        known_pnl = (known_val - known_cost_basis_sum) if known_cnt > 0 else None

        cb_warning: Optional[str] = None
        if unknown_cnt > 0:
            unknown_pct = round((unknown_val / float(total_val)) * 100.0, 1)
            cb_warning = (
                f"{unknown_cnt} holding(s) ({_fmt(unknown_val)}, {unknown_pct:.1f}% of portfolio) "
                f"have unknown cost basis; unrealized P&L excludes those positions."
            )

        return PortfolioConcentrationTelemetry(
            has_holdings=True,
            holdings_count=valid_count,
            total_value_cents=total_val,
            symbol_hhi=sym_hhi,
            symbol_concentration_band=sym_band,  # type: ignore[arg-type]
            asset_class_hhi=cls_hhi,
            asset_class_concentration_band=cls_band,  # type: ignore[arg-type]
            top_symbol=top_sym,
            top_symbol_share_pct=top_sym_share,
            known_cost_basis_value_cents=known_val,
            unknown_cost_basis_value_cents=unknown_val,
            known_cost_basis_count=known_cnt,
            unknown_cost_basis_count=unknown_cnt,
            cost_basis_coverage_pct=coverage_pct,
            all_cost_basis_known=all_known,
            unrealized_pnl_known_only_cents=known_pnl,
            cost_basis_warning=cb_warning,
        )

    # =========================================================================
    # 3. Constrained KKT / Marginal-Utility One-Tap Capital Optimizer
    # =========================================================================

    def build_onetap_plan(
        self,
        *,
        budget: Optional[AutoBudgetResult] = None,
        transactions: Sequence[Any] = (),
        recurring_bills: Sequence[Any] = (),
        settings: Optional[Dict[str, Any]] = None,
        credit_report: Optional[CreditOptimizationReport] = None,
        credit_lines: Optional[Sequence[Any]] = None,
        azeo_plan: Optional[Sequence[Any]] = None,
        goals: Optional[Sequence[Any]] = None,
        holdings: Optional[Sequence[Any]] = None,
        jars: Optional[Sequence[Any]] = None,
        splits_by_tx_id: Optional[Dict[int, List[Any]]] = None,
        starting_balance_cents: Optional[int] = None,
        unpaid_bills_until_payday_cents: Optional[int] = None,
        credit_card_reserved_cents: Optional[int] = None,
        emergency_buffer_cents: Optional[int] = None,
        safe_to_invest_cents: Optional[int] = None,
        current_trading_cap_cents: Optional[int] = None,
        days_until_payday: Optional[int] = None,
        payday_cadence_days: Optional[int] = None,
        next_payday_date: Optional[str] = None,
        estimated_paycheck_cents: Optional[int] = None,
        user_configured_payday: Optional[bool] = None,
        horizon_days: int = 30,
        n_paths: int = DEFAULT_BOOTSTRAP_PATHS,
        seed: int = DEFAULT_RANDOM_SEED,
        today: Optional[date] = None,
    ) -> OneTapPlan:
        """
        Produce a complete `OneTapPlan` containing Bayesian-EWMA/CVaR runway telemetry,
        MAD/Mann-Kendall/HHI diagnostics, and ordered atomic `OneTapAction` items ready
        for `FinanceService` preview, one-tap execution, and deterministic undo.
        """
        ref = today or date.today()
        horizon = max(7, min(90, int(horizon_days)))
        cfg = settings or {}

        # Extract coherent baseline inputs from `budget` when provided, allowing explicit overrides
        s_bd = budget.safe_to_spend if budget is not None else None
        active_jars: List[Any] = (
            list(jars) if jars is not None else (list(budget.jars) if budget is not None else [])
        )
        active_bills: List[Any] = (
            list(recurring_bills)
            if recurring_bills
            else (list(budget.upcoming_bills) if budget is not None else [])
        )
        active_credit_lines: List[Any] = (
            list(credit_lines)
            if credit_lines is not None
            else (list(credit_report.credit_lines) if credit_report is not None else [])
        )
        active_azeo: List[Any] = (
            list(azeo_plan)
            if azeo_plan is not None
            else (list(credit_report.azeo_plan) if credit_report is not None else [])
        )

        liquid_cash = int(
            starting_balance_cents
            if starting_balance_cents is not None
            else (s_bd.total_liquid_cash_cents if s_bd is not None else 0)
        )
        unpaid_bills = max(
            0,
            int(
                unpaid_bills_until_payday_cents
                if unpaid_bills_until_payday_cents is not None
                else (s_bd.unpaid_bills_until_payday_cents if s_bd is not None else 0)
            ),
        )
        cc_reserved = max(
            0,
            int(
                credit_card_reserved_cents
                if credit_card_reserved_cents is not None
                else (s_bd.credit_card_reserved_cents if s_bd is not None else 0)
            ),
        )
        cur_buffer = max(
            0,
            int(
                emergency_buffer_cents
                if emergency_buffer_cents is not None
                else (
                    s_bd.emergency_buffer_cents
                    if s_bd is not None
                    else int(cfg.get("emergency_buffer_cents", 0) or 0)
                )
            ),
        )
        pay_days = max(
            1,
            int(
                days_until_payday
                if days_until_payday is not None
                else (s_bd.days_until_payday if s_bd is not None else 14)
            ),
        )
        pay_cadence = max(
            7,
            int(
                payday_cadence_days
                if payday_cadence_days is not None
                else int(cfg.get("payday_cadence_days", 14) or 14)
            ),
        )
        pay_next_iso = (
            next_payday_date
            if next_payday_date is not None
            else (s_bd.next_payday_date if s_bd is not None else cfg.get("next_payday_date"))
        )
        pay_user_cfg = bool(
            user_configured_payday
            if user_configured_payday is not None
            else bool(cfg.get("payday_user_configured", 0))
        )
        if estimated_paycheck_cents is not None:
            est_paycheck = max(0, int(estimated_paycheck_cents))
        elif s_bd is not None:
            est_paycheck = max(0, int(s_bd.estimated_paycheck_cents))
        else:
            est_paycheck = max(0, int(cfg.get("expected_paycheck_cents", 0) or 0))

        # --- Step 1: Statistical Runway & Diagnostics ---
        runway = self.compute_statistical_runway(
            starting_balance_cents=liquid_cash,
            recurring_bills=active_bills,
            transactions=transactions,
            jars=active_jars,
            credit_lines=active_credit_lines,
            splits_by_tx_id=splits_by_tx_id,
            payday_cadence_days=pay_cadence,
            next_payday_date=pay_next_iso,
            estimated_paycheck_cents=est_paycheck,
            user_configured_payday=pay_user_cfg,
            emergency_buffer_cents=cur_buffer,
            horizon_days=horizon,
            n_paths=n_paths,
            seed=seed,
            today=ref,
        )

        diagnostics = self.compute_statistical_diagnostics(
            transactions=transactions,
            jars=active_jars,
            recurring_bills=active_bills,
            holdings=holdings,
            splits_by_tx_id=splits_by_tx_id,
            today=ref,
        )

        # --- Fresh $0 Profile Invariant ---
        if runway.needs_setup:
            return OneTapPlan(
                generated_at=utc_now_iso(),
                horizon_days=horizon,
                needs_setup=True,
                runway=runway,
                diagnostics=diagnostics,
                available_unlocked_surplus_cents=0,
                allocated_surplus_cents=0,
                unallocated_surplus_cents=0,
                recommended_emergency_buffer_cents=0,
                statistical_safe_to_invest_cents=0,
                kkt_shadow_price_lambda=0.0,
                total_expected_annual_savings_cents=0,
                executable_action_count=0,
                advisory_action_count=0,
                actions=[],
                summary=(
                    "Setup state: add a liquid account balance, transaction history, or paycheck "
                    "schedule to generate a statistical optimization plan."
                ),
            )

        actions: List[OneTapAction] = []

        # --- Step 2: Action Type 1 — `rebalance_jar` (HARD FIREWALL: NEVER raid Bills or is_locked) ---
        rebal_actions, virtual_jar_avail = self._plan_rebalance_jar_actions(active_jars)
        actions.extend(rebal_actions)

        # Compute protected/locked jar reserves using post-rebalance balances
        locked_non_bills_reserves = 0
        locked_non_invest_reserves = 0
        bills_jar_avail_total = 0
        bills_in_deficit = False
        invest_jar_avail: Optional[int] = None

        for j in active_jars:
            jid = int(_get(j, "id", 0))
            kind = _enum_val(_get(j, "kind", "")).strip().lower()
            slug = str(_get(j, "slug", "") or "").strip().lower()
            avail = virtual_jar_avail.get(jid, int(_get(j, "available_cents", 0) or 0))
            is_locked = bool(_get(j, "is_locked", False))
            is_bills = kind == JarKind.BILLS.value or slug == "bills"
            is_invest = kind == JarKind.INVEST.value or slug == "invest"

            if is_bills:
                if avail < 0:
                    bills_in_deficit = True
                else:
                    bills_jar_avail_total += avail
            elif is_locked:
                if avail > 0:
                    locked_non_bills_reserves += avail
                    if not is_invest:
                        locked_non_invest_reserves += avail

            if is_invest:
                invest_jar_avail = max(0, avail)

        # --- Step 3: Action Type 2 — `set_emergency_buffer` ---
        cvar_target = runway.cvar_95_buffer_cents
        pre_payday_spend_need = runway.shrunk_daily_burn_cents * pay_days
        cash_headroom_for_buffer = max(
            0,
            liquid_cash
            - unpaid_bills
            - cc_reserved
            - locked_non_bills_reserves
            - pre_payday_spend_need,
        )
        proposed_buffer = cur_buffer
        if cvar_target > cur_buffer and cash_headroom_for_buffer > cur_buffer:
            candidate_buffer = min(cvar_target, cash_headroom_for_buffer)
            if candidate_buffer - cur_buffer >= MIN_BUFFER_UPDATE_DELTA_CENTS:
                proposed_buffer = candidate_buffer
                delta_buf = proposed_buffer - cur_buffer
                actions.append(
                    OneTapAction(
                        action_id=f"onetap-buffer-{proposed_buffer}",
                        action_type="set_emergency_buffer",
                        title=f"Align Emergency Buffer to {_fmt(proposed_buffer)} (95% CVaR)",
                        summary=(
                            f"Update emergency cash reserve target from {_fmt(cur_buffer)} "
                            f"to {_fmt(proposed_buffer)} (+{_fmt(delta_buf)})."
                        ),
                        rationale=(
                            f"95% Expected Shortfall across {runway.n_paths} bootstrap cashflow paths "
                            f"indicates a {_fmt(cvar_target)} tail liquidity cushion; current liquid "
                            f"cash safely supports {_fmt(proposed_buffer)} without cramping bills or daily spend."
                        ),
                        amount_cents=delta_buf,
                        expected_annual_savings_cents=_cents_times_bps_annual(
                            delta_buf, HYSA_BENCHMARK_BPS
                        ),
                        executable=True,
                        is_advisory=False,
                        payload={
                            "emergency_buffer_cents": proposed_buffer,
                            "previous_emergency_buffer_cents": cur_buffer,
                            "cvar_95_buffer_cents": cvar_target,
                        },
                        undo_payload={
                            "emergency_buffer_cents": cur_buffer,
                        },
                        safety_checks=[
                            f"Liquid cash {_fmt(liquid_cash)} covers unpaid bills, card reserves, locked jars, and buffer",
                            f"Target aligned toward 95% CVaR cushion ({_fmt(cvar_target)})",
                        ],
                    )
                )

        # --- Step 4: Action Type 3 — `set_safe_invest_cap` ---
        effective_buffer = max(proposed_buffer, cvar_target)
        stat_invest_ceiling = max(
            0,
            liquid_cash
            - unpaid_bills
            - cc_reserved
            - locked_non_invest_reserves
            - effective_buffer,
        )

        if safe_to_invest_cents is not None:
            baseline_invest_cap = max(0, int(safe_to_invest_cents))
        elif budget is not None:
            baseline_invest_cap = max(0, int(budget.safe_to_invest_cents))
        elif invest_jar_avail is not None:
            baseline_invest_cap = invest_jar_avail
        else:
            baseline_invest_cap = stat_invest_ceiling

        if bills_in_deficit or runway.ruin_probability_pct > OVERDRAFT_RISK_CEILING_PCT:
            stat_safe_to_invest = 0
        else:
            stat_safe_to_invest = max(0, min(baseline_invest_cap, stat_invest_ceiling))

        prev_cap = (
            max(0, int(current_trading_cap_cents))
            if current_trading_cap_cents is not None
            else baseline_invest_cap
        )
        should_emit_invest_cap = (
            stat_safe_to_invest != prev_cap
            or (current_trading_cap_cents is None and stat_safe_to_invest > 0)
        )
        if should_emit_invest_cap:
            actions.append(
                OneTapAction(
                    action_id=f"onetap-invest-cap-{stat_safe_to_invest}",
                    action_type="set_safe_invest_cap",
                    title=f"Set Safe-to-Invest Guardrail to {_fmt(stat_safe_to_invest)}",
                    summary=(
                        f"Cap deployable trading/investing capital at {_fmt(stat_safe_to_invest)} "
                        f"(after bills, card reserves, locked jars, and 95% CVaR cushion)."
                    ),
                    rationale=(
                        f"Strict statistical invest ceiling keeps {_fmt(unpaid_bills)} unpaid bills, "
                        f"{_fmt(cc_reserved)} card reserves, {_fmt(locked_non_invest_reserves)} locked "
                        f"jar reserves, and {_fmt(effective_buffer)} CVaR buffer fully shielded."
                    ),
                    amount_cents=stat_safe_to_invest,
                    expected_annual_savings_cents=0,
                    executable=True,
                    is_advisory=False,
                    payload={
                        "safe_to_invest_cents": stat_safe_to_invest,
                        "previous_safe_to_invest_cents": prev_cap,
                        "cvar_95_buffer_cents": cvar_target,
                        "unpaid_bills_cents": unpaid_bills,
                        "credit_card_reserved_cents": cc_reserved,
                        "locked_jar_reserves_cents": locked_non_invest_reserves,
                    },
                    undo_payload={
                        "safe_to_invest_cents": prev_cap,
                    },
                    safety_checks=[
                        f"Strictly <= budget safe_to_invest_cents ({_fmt(baseline_invest_cap)})",
                        f"Deducts unpaid bills, card reserves, locked jars, and 95% CVaR buffer ({_fmt(cvar_target)})",
                        f"Overdraft risk {runway.ruin_probability_pct:.2f}% <= {OVERDRAFT_RISK_CEILING_PCT:.1f}% ceiling"
                        if runway.ruin_probability_pct <= OVERDRAFT_RISK_CEILING_PCT
                        else f"Overdraft risk {runway.ruin_probability_pct:.2f}% > 5.0% — invest cap locked to $0.00",
                    ],
                )
            )

        # --- Step 5: Action Type 4 — `fund_goal` (Constrained KKT Surplus Allocation) ---
        protected_bills_reserve = max(unpaid_bills, bills_jar_avail_total)
        unlocked_liquid_headroom = max(
            0,
            liquid_cash
            - protected_bills_reserve
            - cc_reserved
            - locked_non_bills_reserves
            - effective_buffer,
        )
        # Exact 5% overdraft risk ceiling: lump-sum withdrawal X keeps P(nadir - X < 0) <= 5%
        # if and only if X <= overdraft_safe_lump_sum_cents (0 when ruin_probability_pct > 5%).
        overdraft_safe_surplus = (
            max(0, runway.overdraft_safe_lump_sum_cents)
            if runway.ruin_probability_pct <= OVERDRAFT_RISK_CEILING_PCT
            else 0
        )
        available_unlocked_surplus = min(unlocked_liquid_headroom, overdraft_safe_surplus)

        goal_actions, allocated_surplus, kkt_lambda = self._plan_kkt_goal_actions(
            goals=goals,
            available_surplus_cents=available_unlocked_surplus,
            ref=ref,
        )
        actions.extend(goal_actions)

        # --- Step 6: Action Type 5 — `advisory_debt_azeo` (Real APR vs 4.25% HYSA) ---
        debt_actions = self._plan_advisory_debt_azeo_actions(
            credit_lines=active_credit_lines,
            azeo_plan=active_azeo,
            available_surplus_cents=available_unlocked_surplus,
            ref=ref,
        )
        actions.extend(debt_actions)

        # Assign clean 1-based execution priority ranks
        for idx, act in enumerate(actions, start=1):
            act.priority_rank = idx

        exec_count = sum(1 for a in actions if a.executable)
        adv_count = sum(1 for a in actions if a.is_advisory)
        total_savings = sum(a.expected_annual_savings_cents for a in actions)
        unallocated = max(0, available_unlocked_surplus - allocated_surplus)

        summary_parts = [
            f"{horizon}d P50 ending {_fmt(runway.p50_ending_cents)} (P10 {_fmt(runway.p10_ending_cents)})",
            f"95% CVaR cushion {_fmt(runway.cvar_95_buffer_cents)}",
            f"overdraft risk {runway.ruin_probability_pct:.1f}%",
            f"{exec_count} executable action(s)",
        ]
        if adv_count > 0:
            summary_parts.append(f"{adv_count} debt/AZEO advisory item(s)")

        return OneTapPlan(
            generated_at=utc_now_iso(),
            horizon_days=horizon,
            needs_setup=False,
            runway=runway,
            diagnostics=diagnostics,
            available_unlocked_surplus_cents=available_unlocked_surplus,
            allocated_surplus_cents=allocated_surplus,
            unallocated_surplus_cents=unallocated,
            recommended_emergency_buffer_cents=cvar_target,
            statistical_safe_to_invest_cents=stat_safe_to_invest,
            kkt_shadow_price_lambda=kkt_lambda,
            total_expected_annual_savings_cents=total_savings,
            executable_action_count=exec_count,
            advisory_action_count=adv_count,
            actions=actions,
            summary=" · ".join(summary_parts),
        )

    def _plan_rebalance_jar_actions(
        self,
        jars: Sequence[Any],
    ) -> Tuple[List[OneTapAction], Dict[int, int]]:
        """
        Cover overspent flexible jars strictly from unlocked non-Bills surplus jars.

        HARD FIREWALL:
        - NEVER raid `bills` jar (by kind or slug).
        - NEVER raid any jar with `is_locked=True`.
        """
        virtual_avail: Dict[int, int] = {}
        for j in jars:
            jid = int(_get(j, "id", 0))
            virtual_avail[jid] = int(_get(j, "available_cents", 0) or 0)

        deficits = [
            j
            for j in jars
            if not _is_protected_jar(j) and virtual_avail[int(_get(j, "id", 0))] < 0
        ]
        if not deficits:
            return [], virtual_avail

        donor_priority = {
            "fun": 1,
            "invest": 2,
            "supplies": 3,
            "daily": 4,
            "goals": 5,
            "safety": 6,
        }

        def _donor_sort_key(j: Any) -> Tuple[int, int, int]:
            kind = _enum_val(_get(j, "kind", "")).strip().lower()
            slug = str(_get(j, "slug", "") or "").strip().lower()
            p_rank = donor_priority.get(slug, donor_priority.get(kind, 50))
            return (p_rank, -virtual_avail[int(_get(j, "id", 0))], int(_get(j, "id", 0)))

        donors = [
            j
            for j in sorted(jars, key=_donor_sort_key)
            if not _is_protected_jar(j) and virtual_avail[int(_get(j, "id", 0))] > 0
        ]
        if not donors:
            return [], virtual_avail

        actions: List[OneTapAction] = []
        # Cover largest flexible deficits first deterministically
        deficits.sort(key=lambda j: (virtual_avail[int(_get(j, "id", 0))], int(_get(j, "id", 0))))

        for target in deficits:
            to_id = int(_get(target, "id", 0))
            to_name = str(_get(target, "name", f"Jar #{to_id}"))
            to_slug = str(_get(target, "slug", "") or "").strip().lower()
            needed = abs(virtual_avail[to_id])

            for donor in donors:
                if needed <= 0:
                    break
                from_id = int(_get(donor, "id", 0))
                if from_id == to_id:
                    continue
                surplus = virtual_avail[from_id]
                if surplus <= 0:
                    continue

                move = min(needed, surplus)
                virtual_avail[from_id] -= move
                virtual_avail[to_id] += move
                needed -= move

                from_name = str(_get(donor, "name", f"Jar #{from_id}"))
                from_slug = str(_get(donor, "slug", "") or "").strip().lower()
                reason = (
                    f"One-tap flexible rebalance: cover {_fmt(move)} in {to_name} "
                    f"from unlocked {from_name} surplus"
                )
                actions.append(
                    OneTapAction(
                        action_id=f"onetap-rebalance-{from_id}-{to_id}-{move}",
                        action_type="rebalance_jar",
                        title=f"Rebalance {_fmt(move)}: {from_name} -> {to_name}",
                        summary=(
                            f"Shift {_fmt(move)} from unlocked {from_name} surplus to "
                            f"bring {to_name} back toward green."
                        ),
                        rationale=(
                            f"{to_name} dipped negative this cycle; {from_name} has unlocked "
                            f"surplus available while keeping Bills and all locked jars 100% untouched."
                        ),
                        amount_cents=move,
                        expected_annual_savings_cents=0,
                        executable=True,
                        is_advisory=False,
                        payload={
                            "from_jar_id": from_id,
                            "from_jar_slug": from_slug,
                            "from_jar_name": from_name,
                            "to_jar_id": to_id,
                            "to_jar_slug": to_slug,
                            "to_jar_name": to_name,
                            "amount_cents": move,
                            "reason": reason,
                        },
                        undo_payload={
                            "from_jar_id": to_id,
                            "to_jar_id": from_id,
                            "amount_cents": move,
                            "reason": f"Undo one-tap rebalance ({from_name} -> {to_name})",
                        },
                        safety_checks=[
                            f"Source jar '{from_name}' is unlocked (is_locked=False) and non-Bills",
                            f"Move {_fmt(move)} strictly <= source available surplus",
                        ],
                    )
                )

        return actions, virtual_avail

    def _plan_kkt_goal_actions(
        self,
        *,
        goals: Optional[Sequence[Any]],
        available_surplus_cents: int,
        ref: date,
    ) -> Tuple[List[OneTapAction], int, float]:
        """
        Allocate exact integer-cent KKT-optimal surplus across active goals bounded by
        `available_surplus_cents` (which already enforces the 5% overdraft risk ceiling).
        """
        if available_surplus_cents <= 0 or not goals:
            return [], 0, 0.0

        active_goals: List[Tuple[Any, int, int, int, Optional[int], float, float]] = []
        for g in goals:
            status = str(_get(g, "status", "active") or "active").strip().lower()
            deleted_at = _get(g, "deleted_at", None)
            if status != "active" or deleted_at:
                continue
            target_c = max(0, int(_get(g, "target_cents", 0) or 0))
            saved_c = max(0, int(_get(g, "saved_cents", 0) or 0))
            rem_raw = _get(g, "remaining_cents", None)
            rem_c = max(0, int(rem_raw)) if rem_raw is not None else max(0, target_c - saved_c)
            if rem_c <= 0:
                continue

            prio = max(1, min(5, int(_get(g, "priority", 2) or 2)))
            prio_weight = (6.0 - float(prio)) ** 1.5

            days_left: Optional[int] = None
            td_raw = _get(g, "target_date", None)
            if td_raw:
                try:
                    td = date.fromisoformat(str(td_raw)[:10])
                    days_left = max(1, (td - ref).days)
                    urgency_mult = 1.0 + (90.0 / max(15.0, float(days_left)))
                    months_left = max(1.0, days_left / 30.4)
                    monthly_pace = min(rem_c, int(math.ceil(rem_c / months_left)))
                except ValueError:
                    urgency_mult = 1.0
                    monthly_pace = min(rem_c, max(10_000, int(round(rem_c / 6.0))))
            else:
                urgency_mult = 1.0
                monthly_pace = min(rem_c, max(10_000, int(round(rem_c / 6.0))))

            alpha_i = prio_weight * urgency_mult
            beta_i = float(max(5_000, monthly_pace))
            gid = int(_get(g, "id", 0))
            active_goals.append((g, gid, saved_c, rem_c, days_left, alpha_i, beta_i))

        if not active_goals:
            return [], 0, 0.0

        active_goals.sort(
            key=lambda item: (
                -item[5],  # highest marginal utility weight first
                item[4] if item[4] is not None else 99_999,
                item[3],
                item[1],
            )
        )

        weights = [item[5] for item in active_goals]
        scales = [item[6] for item in active_goals]
        caps = [item[3] for item in active_goals]

        allocs, lam_star = allocate_kkt_concave_cents(
            total_cents=available_surplus_cents,
            weights=weights,
            scales=scales,
            caps=caps,
        )

        actions: List[OneTapAction] = []
        total_allocated = 0
        for (g, gid, saved_c, rem_c, days_left, alpha_i, beta_i), alloc in zip(
            active_goals, allocs, strict=True
        ):
            if alloc <= 0:
                continue
            total_allocated += alloc
            g_name = str(_get(g, "name", f"Goal #{gid}"))
            prio = max(1, min(5, int(_get(g, "priority", 2) or 2)))
            td_iso = _get(g, "target_date", None)
            mu_opt = alpha_i / (1.0 + (float(alloc) / beta_i))
            deadline_txt = (
                f"{days_left}d remaining to {td_iso}" if days_left is not None and td_iso else "flexible timeline"
            )
            actions.append(
                OneTapAction(
                    action_id=f"onetap-goal-{gid}-{alloc}",
                    action_type="fund_goal",
                    title=f"Fund Goal '{g_name}' (+{_fmt(alloc)})",
                    summary=(
                        f"Allocate {_fmt(alloc)} of unlocked surplus to '{g_name}' "
                        f"(P{prio}, {deadline_txt})."
                    ),
                    rationale=(
                        f"KKT concave utility optimization (weight={alpha_i:.2f}, shadow price "
                        f"lambda={lam_star:.3f}, marginal utility={mu_opt:.3f}) routes {_fmt(alloc)} "
                        f"toward {_fmt(rem_c)} remaining while keeping 30-day overdraft probability <= 5.0%."
                    ),
                    amount_cents=alloc,
                    expected_annual_savings_cents=0,
                    executable=True,
                    is_advisory=False,
                    payload={
                        "goal_id": gid,
                        "goal_name": g_name,
                        "amount_cents": alloc,
                        "previous_saved_cents": saved_c,
                        "remaining_after_cents": max(0, rem_c - alloc),
                        "priority": prio,
                        "target_date": str(td_iso)[:10] if td_iso else None,
                        "kkt_weight": round(alpha_i, 4),
                        "kkt_marginal_utility": round(mu_opt, 4),
                    },
                    undo_payload={
                        "goal_id": gid,
                        "restore_saved_cents": saved_c,
                    },
                    safety_checks=[
                        f"Allocation {_fmt(alloc)} <= remaining goal balance {_fmt(rem_c)}",
                        f"Total goal funding <= unlocked 5%-overdraft-safe surplus ({_fmt(available_surplus_cents)})",
                    ],
                )
            )

        return actions, total_allocated, lam_star

    def _plan_advisory_debt_azeo_actions(
        self,
        *,
        credit_lines: Sequence[Any],
        azeo_plan: Sequence[Any],
        available_surplus_cents: int,
        ref: date,
    ) -> List[OneTapAction]:
        """
        Generate exact-cent pre-statement-close AZEO paydown advisories (`< 8.9%` / `< 28.9%`
        utilization thresholds) and exact real-APR vs 4.25% HYSA annual dollar interest savings.

        STRICT GUARANTEES:
        - ZERO fabricated credit-score point deltas.
        - ZERO hardcoded APR assumptions (uses each card's real `apr_bps` / `effective_apr_bps`).
        - Strictly advisory (`executable=False`, `is_advisory=True`): never initiates bank transfers.
        """
        if not credit_lines:
            return []

        azeo_by_line: Dict[int, Any] = {}
        for act in azeo_plan:
            cid = int(_get(act, "credit_line_id", 0) or 0)
            if cid > 0:
                azeo_by_line[cid] = act

        cards: List[Any] = []
        for cl in credit_lines:
            if not _get(cl, "active", True):
                continue
            bal = max(0, int(_get(cl, "balance_cents", 0) or 0))
            if bal <= 0:
                continue
            kind = _enum_val(_get(cl, "kind", DebtKind.CREDIT_CARD.value)).strip().lower()
            if kind == DebtKind.CREDIT_CARD.value:
                cards.append(cl)

        if not cards:
            return []

        # If caller did not pass a precomputed AZEO plan, select 1 primary reporting card cleanly
        primary_card_id: Optional[int] = None
        if not azeo_by_line:
            eligible_primary = [
                c for c in cards if int(_get(c, "credit_limit_cents", 0) or 0) > 0
            ]
            if eligible_primary:
                eligible_primary.sort(
                    key=lambda c: (
                        _resolve_effective_apr_bps(c, ref),
                        -int(_get(c, "credit_limit_cents", 0) or 0),
                        int(_get(c, "id", 0) or 0),
                    )
                )
                primary_card_id = int(_get(eligible_primary[0], "id", 0) or 0)

        advisories: List[Tuple[int, int, str, OneTapAction]] = []

        for cl in cards:
            cl_id = int(_get(cl, "id", 0) or 0)
            name = str(_get(cl, "name", f"Card #{cl_id}"))
            bal = max(0, int(_get(cl, "balance_cents", 0) or 0))
            lim = max(0, int(_get(cl, "credit_limit_cents", 0) or 0))
            real_apr_bps = _resolve_effective_apr_bps(cl, ref)
            real_apr_pct = round(real_apr_bps / 100.0, 2)
            spread_bps = max(0, real_apr_bps - HYSA_BENCHMARK_BPS)

            cur_util = round((bal / float(lim)) * 100.0, 2) if lim > 0 else 0.0
            cap_289 = _strict_under_threshold_cap(lim, Decimal("0.289")) if lim > 0 else 0
            cap_089 = _strict_under_threshold_cap(lim, Decimal("0.089")) if lim > 0 else 0
            pay_to_289 = max(0, bal - cap_289) if lim > 0 else 0
            pay_to_089 = max(0, bal - cap_089) if lim > 0 else 0

            existing_azeo = azeo_by_line.get(cl_id)
            if existing_azeo is not None:
                pay_before_close = max(0, int(_get(existing_azeo, "pay_before_close_cents", 0) or 0))
                target_bal = max(0, int(_get(existing_azeo, "target_statement_balance_cents", bal - pay_before_close) or 0))
                pay_by_date = str(_get(existing_azeo, "pay_by_date", ""))[:10]
                close_date = str(_get(existing_azeo, "statement_close_date", ""))[:10]
                res_util = float(_get(existing_azeo, "resulting_util_pct", 0.0) or 0.0)
                role = str(_get(existing_azeo, "role", "zero_out_card"))
            else:
                close_day = int(_get(cl, "statement_closing_day", 15) or 15)
                close_dt = next_occurrence_of_day(close_day, ref)
                pay_by_dt = max(ref, close_dt - timedelta(days=3))
                close_date = close_dt.isoformat()
                pay_by_date = pay_by_dt.isoformat()

                if lim > 0:
                    if cl_id == primary_card_id:
                        # Primary AZEO card targets 1%..4.9% (strictly under 8.9% threshold)
                        target_49 = _strict_under_threshold_cap(lim, Decimal("0.049"))
                        target_bal = min(bal, max(100, min(cap_089, target_49)))
                        pay_before_close = max(0, bal - target_bal)
                        role = "primary_reporting_card" if pay_before_close > 0 else "already_optimal"
                    else:
                        target_bal = 0
                        pay_before_close = bal
                        role = "zero_out_card"
                    res_util = round((target_bal / float(lim)) * 100.0, 2)
                else:
                    target_bal = 0
                    pay_before_close = bal if spread_bps > 0 else 0
                    res_util = 0.0
                    role = "high_apr_paydown"

            # Also surface high-APR carry savings if balance carries above 4.25% HYSA without full autopay
            autopay = _enum_val(_get(cl, "autopay_mode", "statement")).strip().lower()
            has_grace = bool(_get(cl, "has_grace_period", True))
            if pay_before_close <= 0 and spread_bps > 0 and (not has_grace or autopay in ("none", "minimum")):
                pay_before_close = bal
                target_bal = 0
                res_util = 0.0
                role = "high_apr_carry_paydown"

            if pay_before_close <= 0:
                continue

            gross_annual_saved = _cents_times_bps_annual(pay_before_close, real_apr_bps)
            net_annual_saved = _cents_times_bps_annual(pay_before_close, spread_bps)
            hysa_opp_cost = _cents_times_bps_annual(pay_before_close, HYSA_BENCHMARK_BPS)

            if pay_to_289 > 0:
                threshold_note = (
                    f"clears both the 28.9% threshold ({_fmt(pay_to_289)}) and 8.9% AZEO band "
                    f"({_fmt(pay_to_089)})"
                )
            elif pay_to_089 > 0:
                threshold_note = f"brings utilization under the 8.9% AZEO band ({_fmt(pay_to_089)})"
            else:
                threshold_note = "keeps reported utilization in the low-utilization AZEO band"

            if spread_bps > 0:
                apr_note = (
                    f"At {real_apr_pct:.2f}% APR vs 4.25% HYSA (+{spread_bps / 100:.2f}% net spread), "
                    f"reducing principal saves {_fmt(net_annual_saved)}/yr net "
                    f"({_fmt(gross_annual_saved)}/yr gross interest avoided)."
                )
            else:
                apr_note = (
                    f"Card APR is {real_apr_pct:.2f}% (at or below 4.25% HYSA benchmark), "
                    f"so net APR-vs-HYSA interest savings are $0.00/yr; payment is purely for "
                    f"pre-close utilization hygiene."
                )

            act = OneTapAction(
                action_id=f"onetap-advisory-azeo-{cl_id}-{pay_before_close}",
                action_type="advisory_debt_azeo",
                title=f"AZEO Pre-Close Advisory: Pay {_fmt(pay_before_close)} on {name} by {pay_by_date}",
                summary=(
                    f"Pay {_fmt(pay_before_close)} before {close_date} statement close "
                    f"({cur_util:.1f}% -> {res_util:.1f}% utilization; "
                    f"{_fmt(net_annual_saved)}/yr net savings vs 4.25% HYSA)."
                ),
                rationale=(
                    f"External payment advisory (read-only): scheduling {_fmt(pay_before_close)} on "
                    f"{name} by {pay_by_date} {threshold_note}. {apr_note}"
                ),
                amount_cents=pay_before_close,
                expected_annual_savings_cents=net_annual_saved,
                executable=False,
                is_advisory=True,
                payload={
                    "credit_line_id": cl_id,
                    "card_name": name,
                    "role": role,
                    "current_balance_cents": bal,
                    "credit_limit_cents": lim,
                    "current_utilization_pct": cur_util,
                    "target_statement_balance_cents": target_bal,
                    "resulting_utilization_pct": res_util,
                    "pay_before_close_cents": pay_before_close,
                    "pay_to_under_28_9_cents": pay_to_289,
                    "pay_to_under_8_9_cents": pay_to_089,
                    "pay_by_date": pay_by_date,
                    "statement_close_date": close_date,
                    "apr_bps": real_apr_bps,
                    "hysa_benchmark_bps": HYSA_BENCHMARK_BPS,
                    "net_apr_spread_bps": spread_bps,
                    "gross_annual_interest_saved_cents": gross_annual_saved,
                    "hysa_opportunity_cost_cents": hysa_opp_cost,
                    "expected_annual_savings_cents": net_annual_saved,
                    "affordable_from_unlocked_surplus_cents": min(
                        pay_before_close, available_surplus_cents
                    ),
                },
                undo_payload={},
                safety_checks=[
                    "Read-only advisory: ChronoX never initiates external bank or card transfers",
                    f"Uses card's real APR ({real_apr_pct:.2f}%) vs 4.25% HYSA benchmark",
                    f"Exact-cent utilization target ({cur_util:.1f}% -> {res_util:.1f}%)",
                ],
            )
            advisories.append((-net_annual_saved, -pay_to_289, pay_by_date, act))

        advisories.sort(key=lambda item: (item[0], item[1], item[2]))
        return [item[3] for item in advisories]
