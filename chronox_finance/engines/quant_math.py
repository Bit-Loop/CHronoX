"""
Institutional-Grade Deterministic Quantitative Personal Finance Engine.

Provides exact integer-cent mathematical intelligence that powers both:
- `deterministic_only` mode (100% local, zero LLM calls, rich coaching & Q&A), and
- `hybrid` grounding (verified runway, volatility bands, and marginal-ROI waterfalls).

Core capabilities (zero heavy external dependencies — stdlib + Pydantic only):
1. 30/60/90-Day Day-by-Day Cashflow Runway & Overdraft Simulator (`simulate_cashflow_runway`)
2. Trailing 90-Day Spending Volatility & Statistical Buffer Sizing (`compute_spending_volatility`)
3. Optimal Marginal-ROI Multi-Objective Surplus Allocator (`optimize_surplus_allocation`)
4. Pure-Math Deterministic Coach & Free-Form Q&A Engine (`build_quant_suggestions`, `answer_quant_question`)
"""

from __future__ import annotations

import calendar
import math
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from statistics import mean, median, stdev
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from pydantic import BaseModel

from chronox_finance.engines.autobudget import AutoBudgetResult, iter_effective_tx_allocations
from chronox_finance.engines.credit_debt import (
    compute_effective_min_payment,
    next_occurrence_of_day,
)
from chronox_finance.engines.goal_planner import GoalOptimizerEngine
from chronox_finance.engines.recurring import detect_payday_schedule, forecast_bill_amount_cents
from chronox_finance.models import (
    ADHDStats,
    AutopayMode,
    AZEOAction,
    CoachAnswerResponse,
    CreditLineOut,
    CreditOptimizationReport,
    DailyCashflowPoint,
    DebtKind,
    GoalOut,
    GoalPlanRequest,
    ImpulseOut,
    ImpulseStatus,
    IntelligenceMode,
    JarKind,
    OptimalAllocationStep,
    QuantForecastReport,
    RecurrenceCadence,
    SpendingVolatilityMetrics,
    SuggestionActionType,
    ValidatedSuggestion,
    utc_now_iso,
)
from chronox_finance.security import sanitize_untrusted_text

__all__ = [
    "CashflowRunwayResult",
    "DailyCashflowPoint",
    "HYSA_BENCHMARK_BPS",
    "MannKendallTrendResult",
    "OptimalAllocationStep",
    "QuantForecastReport",
    "QuantMathEngine",
    "SeasonalBaseline",
    "SpendingVolatilityMetrics",
    "WEEKDAY_KEYS",
    "allocate_kkt_concave_cents",
    "compute_dow_multipliers",
    "compute_ewma_shrunk_rate",
    "compute_hhi_index",
    "compute_mad_stats",
    "compute_mann_kendall_slope",
    "compute_modified_z_score",
    "huber_mad_clip_series",
    "percentile_cents",
    "to_dict",
]

# --- Constants & Regex Patterns ---

MIN_EMERGENCY_BUFFER_CENTS = 50_000  # $500.00 baseline floor
LATE_FEE_SHIELD_CENTS = 3_500  # $35.00 conservative late-fee floor
INVEST_EXPECTED_ANNUAL_PCT = 7.5  # Long-run diversified expected return benchmark
SAFETY_HYSA_ANNUAL_PCT = 4.25  # High-yield cash reserve benchmark
HYSA_BENCHMARK_BPS = 425  # 4.25% exact basis points for integer/Decimal interest math
WEEKDAY_KEYS: Tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

_QUESTION_DOLLAR_RE = re.compile(
    r"(?<!\d)\$?\s*((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)"
    r"(?!\d|\s*%|\s*-?\s*(?:d|day|days|h|hr|hrs|hour|hours|mo|mos|month|months|wk|wks|week|weeks|yr|yrs|year|years)\b)",
    re.IGNORECASE,
)
_EXPLICIT_DOLLAR_SIGN_RE = re.compile(
    r"\$\s*((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)(?!\d)"
)
_AFFORD_INTENT_RE = re.compile(
    r"\b(afford\w*|buy(?:ing)?|spend(?:ing)?|purchas\w*|costs?|dinner|trips?|orders?|worth\s+it|"
    r"can\s+i\s+get|should\s+i\s+get|gadgets?|laptops?|phones?|monitors?|upgrades?|splurg\w*)\b",
    re.IGNORECASE,
)
_CREDIT_INTENT_RE = re.compile(
    r"\b(credit\s*scores?|fico|azeo|utilizations?|util|statement\s*clos\w*|closing\s*dates?|"
    r"grace\s*periods?|all[\s-]?zero|credit\s*cards?|credit\s*health|dti)\b",
    re.IGNORECASE,
)
_DEBT_INTENT_RE = re.compile(
    r"\b(debts?|pay\s*off|payoff|avalanche|snowball|aprs?|interest|promos?|loans?|"
    r"surplus|allocat\w*|extra\s+money|pay\s+first|order\s+of)\b",
    re.IGNORECASE,
)
_CASHFLOW_INTENT_RE = re.compile(
    r"\b(cash\s*flow|runway|overdraft\w*|negative|paydays?|forecasts?|nadir|dips?|"
    r"30[\s-]?day|60[\s-]?day|90[\s-]?day|make\s+it\s+to\s+payday|crunch)\b",
    re.IGNORECASE,
)
_SUBS_INTENT_RE = re.compile(
    r"\b(subscriptions?|subs|recurring|price\s*hikes?|price\s*jumps?|zombies?|cancel\w*|"
    r"bills?|memberships?|netflix|spotify)\b",
    re.IGNORECASE,
)
_BUFFER_INTENT_RE = re.compile(
    r"\b(emergency|buffers?|cushions?|volatil\w*|stability|stable|p90|p50|variance|rainy\s*day|safety\s*net)\b",
    re.IGNORECASE,
)
_GOAL_INVEST_INTENT_RE = re.compile(
    r"\b(goals?|savings?|save\s+for|invest\w*|trading|chronox|alpaca|vaults?)\b",
    re.IGNORECASE,
)


def _fmt(cents: int) -> str:
    """Format exact integer cents as human-friendly USD currency."""
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read attribute or mapping key seamlessly from dicts, Pydantic models, or dataclasses."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _enum_val(val: Any, default: str = "") -> str:
    if val is None:
        return default
    return str(val.value if hasattr(val, "value") else val)


def to_dict(obj: Any) -> Dict[str, Any]:
    """Convert Pydantic BaseModel, dataclass, or mapping cleanly into a JSON-ready dict."""
    if isinstance(obj, BaseModel):
        return obj.model_dump()
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if isinstance(obj, dict):
        return dict(obj)
    raise TypeError(f"Unsupported object type for to_dict(): {type(obj).__name__}")


def _add_months_clamped(dt: date, months: int, anchor_day: int) -> date:
    """Advance `dt` by `months` calendar months, clamping `anchor_day` to month length."""
    total = (dt.year * 12 + (dt.month - 1)) + months
    year = total // 12
    month = (total % 12) + 1
    max_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(max(1, anchor_day), max_day))


def _next_semimonthly_date(curr: date, anchor_day: int) -> date:
    """
    Advance a semi-monthly schedule cleanly (exact 2 occurrences per calendar month).

    Preserves standard two-anchor pairs: e.g. (1st, 15th), (5th, 20th), (15th, 30th/28th).
    """
    if anchor_day <= 15:
        low_day = max(1, anchor_day)
        high_day = 15 if anchor_day == 1 else min(31, anchor_day + 15)
    else:
        high_day = min(31, anchor_day)
        low_day = max(1, anchor_day - 15)

    this_month_len = calendar.monthrange(curr.year, curr.month)[1]
    high_this = date(curr.year, curr.month, min(high_day, this_month_len))
    if curr < high_this:
        return high_this

    next_y = curr.year + (1 if curr.month == 12 else 0)
    next_m = 1 if curr.month == 12 else curr.month + 1
    next_len = calendar.monthrange(next_y, next_m)[1]
    return date(next_y, next_m, min(low_day, next_len))


def _step_bill_date(curr: date, cadence: RecurrenceCadence, anchor_day: int) -> date:
    """Advance a recurring bill date by exactly one cycle for any of the 8 supported cadences."""
    if cadence == RecurrenceCadence.WEEKLY:
        return curr + timedelta(days=7)
    if cadence == RecurrenceCadence.BIWEEKLY:
        return curr + timedelta(days=14)
    if cadence == RecurrenceCadence.SEMIMONTHLY:
        return _next_semimonthly_date(curr, anchor_day)
    if cadence == RecurrenceCadence.MONTHLY:
        return _add_months_clamped(curr, 1, anchor_day)
    if cadence == RecurrenceCadence.BIMONTHLY:
        return _add_months_clamped(curr, 2, anchor_day)
    if cadence == RecurrenceCadence.QUARTERLY:
        return _add_months_clamped(curr, 3, anchor_day)
    if cadence == RecurrenceCadence.SEMIANNUAL:
        return _add_months_clamped(curr, 6, anchor_day)
    if cadence == RecurrenceCadence.ANNUAL:
        return _add_months_clamped(curr, 12, anchor_day)
    return _add_months_clamped(curr, 1, anchor_day)


def _step_payday_date(curr: date, cadence_days: int, anchor_day: int) -> date:
    """Advance a payday date preserving weekly/biweekly/semi-monthly/monthly rhythm."""
    if cadence_days in (30, 31):
        return _add_months_clamped(curr, 1, anchor_day)
    if cadence_days == 15:
        return _next_semimonthly_date(curr, anchor_day)
    return curr + timedelta(days=max(7, cadence_days))


def _percentile_cents(values: Sequence[int], q: float) -> int:
    """Compute exact linear-interpolated (Type-7) percentile in integer cents."""
    if not values:
        return 0
    ordered = sorted(int(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    q_clamped = min(1.0, max(0.0, float(q)))
    pos = (len(ordered) - 1) * q_clamped
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return ordered[lo]
    frac = pos - lo
    return int(round(ordered[lo] + frac * (ordered[hi] - ordered[lo])))


def _bill_monthly_equiv_cents(amount_cents: int, cadence_str: str) -> int:
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
    return int(round(amount_cents * multipliers.get(cadence_str, 1.0)))


def _allocate_proportional_exact_cents(
    total_cents: int,
    weights: Sequence[float],
    caps: Sequence[int],
) -> List[int]:
    """
    Distribute `total_cents` across buckets proportional to `weights`, bounded by `caps`,
    with exact largest-remainder cent conservation (zero floating-point penny drift).
    """
    n = len(weights)
    allocs = [0] * n
    remaining = max(0, int(total_cents))
    active = [i for i in range(n) if caps[i] > 0 and weights[i] > 0]

    # Iterative water-filling handles buckets hitting their individual caps cleanly
    while remaining > 0 and active:
        w_sum = sum(weights[i] for i in active)
        if w_sum <= 0:
            break
        raw = [(i, (remaining * weights[i]) / w_sum) for i in active]
        capped_any = False
        for i, val in raw:
            headroom = caps[i] - allocs[i]
            if val >= headroom:
                allocs[i] += headroom
                remaining -= headroom
                capped_any = True
        if capped_any:
            active = [i for i in active if allocs[i] < caps[i]]
            continue

        # Nobody hit a cap — use exact floor + largest remainder for remaining pennies
        floors: List[Tuple[int, int, float]] = []
        floor_sum = 0
        for i, val in raw:
            fl = int(math.floor(val))
            floors.append((i, fl, val - fl))
            floor_sum += fl
        for i, fl, _rem in floors:
            allocs[i] += fl
        leftover = remaining - floor_sum
        # Distribute leftover pennies deterministically by largest fractional part, then index order
        for i, _fl, _rem in sorted(floors, key=lambda t: (-t[2], t[0])):
            if leftover <= 0:
                break
            if allocs[i] < caps[i]:
                allocs[i] += 1
                leftover -= 1
        remaining = leftover
        break

    return allocs


def percentile_cents(values: Sequence[int], q: float) -> int:
    """Compute exact linear-interpolated (Type-7) percentile in integer cents."""
    return _percentile_cents(values, q)


def compute_mad_stats(values: Sequence[float]) -> Tuple[float, float]:
    """Return `(median, MAD)` where `MAD = median(|x_i - median|)` using pure Python stdlib."""
    if not values:
        return 0.0, 0.0
    nums = [float(v) for v in values]
    med = float(median(nums))
    abs_devs = [abs(v - med) for v in nums]
    mad = float(median(abs_devs))
    return med, mad


def compute_modified_z_score(
    value: float,
    median_val: float,
    mad_val: float,
    mean_ad: float = 0.0,
) -> float:
    """
    Compute the Iglewicz & Hoaglin (1993) Modified Z-Score.

    Uses `0.6745 * (x - median) / MAD` when `MAD > 0`, falling back to
    `(x - median) / (1.253314 * MeanAD)` when `MAD == 0` and `MeanAD > 0`.
    """
    diff = float(value) - float(median_val)
    if mad_val > 0.0:
        return 0.6745 * diff / float(mad_val)
    if mean_ad > 0.0:
        return diff / (1.253314 * float(mean_ad))
    return 0.0


def huber_mad_clip_series(values: Sequence[float], k: float = 2.5) -> List[float]:
    """
    Apply robust Huber/MAD winsorization to a non-negative spending series so one-off
    outliers cannot distort daily burn estimates.

    Handles zero-inflated daily spending cleanly: when the overall MAD is 0 (more than
    half of days have $0 spend), computes robust dispersion across positive spend days
    so normal spending days are preserved while extreme spikes are clipped.
    """
    if not values:
        return []
    nums = [max(0.0, float(v)) for v in values]
    k_val = max(0.5, float(k))
    med_all, mad_all = compute_mad_stats(nums)
    if mad_all > 0.0:
        sigma = 1.4826 * mad_all
        lo = max(0.0, med_all - k_val * sigma)
        hi = med_all + k_val * sigma
        return [min(hi, max(lo, v)) for v in nums]

    positives = [v for v in nums if v > 0.0]
    if not positives:
        return [0.0] * len(nums)

    med_pos, mad_pos = compute_mad_stats(positives)
    if mad_pos > 0.0:
        sigma_pos = 1.4826 * mad_pos
    else:
        ordered_pos = sorted(positives)
        # Trim top 10% extreme right tail before computing fallback scale so one massive spike
        # cannot inflate its own clip ceiling.
        trim_n = max(1, int(math.ceil(0.10 * len(ordered_pos)))) if len(ordered_pos) >= 5 else 0
        core = ordered_pos[:-trim_n] if trim_n > 0 else ordered_pos
        core_med = float(median(core))
        core_mean_ad = sum(abs(v - core_med) for v in core) / len(core) if core else 0.0
        sigma_pos = max(1.253314 * core_mean_ad, 0.25 * med_pos)

    hi = med_pos + k_val * sigma_pos
    return [min(hi, v) if v > 0.0 else 0.0 for v in nums]


def compute_ewma_shrunk_rate(
    daily_values: Sequence[float],
    *,
    alpha: float = 0.18,
    prior_days: float = 14.0,
    max_ewma_weight: float = 0.65,
    huber_k: float = 2.5,
) -> Tuple[float, float, float]:
    """
    Estimate daily discretionary burn rate via Huber/MAD-clipped EWMA (`alpha=0.18`)
    shrunk toward the trailing 90-day robust median anchor.

    Returns `(shrunk_daily_rate, ewma_daily_rate, robust_median_daily_rate)`.
    """
    if not daily_values:
        return 0.0, 0.0, 0.0

    clipped = huber_mad_clip_series(daily_values, k=huber_k)
    n = len(clipped)
    if n == 0 or sum(clipped) <= 0.0:
        return 0.0, 0.0, 0.0

    alpha_clamped = min(0.95, max(0.01, float(alpha)))
    daily_med = float(median(clipped))

    # Build complete trailing 7-day blocks anchored at the most recent day so sparse/weekend
    # spending patterns yield a realistic median daily rate rather than collapsing to $0.
    n_full_weeks = n // 7
    weekly_totals: List[float] = []
    for w in range(n_full_weeks):
        end_idx = n - w * 7
        start_idx = end_idx - 7
        weekly_totals.append(sum(clipped[start_idx:end_idx]))

    if daily_med > 0.0 and len(weekly_totals) >= 2:
        weekly_med_daily = float(median(weekly_totals)) / 7.0
        robust_anchor = 0.5 * daily_med + 0.5 * weekly_med_daily
    elif daily_med > 0.0:
        robust_anchor = daily_med
    elif weekly_totals:
        robust_anchor = float(median(weekly_totals)) / 7.0
    else:
        robust_anchor = sum(clipped) / float(n)

    ewma_val = robust_anchor
    for val in clipped:
        ewma_val = alpha_clamped * val + (1.0 - alpha_clamped) * ewma_val

    prior = max(1.0, float(prior_days))
    weight = min(float(max_ewma_weight), float(n) / (float(n) + prior))
    shrunk = weight * ewma_val + (1.0 - weight) * robust_anchor
    return max(0.0, shrunk), max(0.0, ewma_val), max(0.0, robust_anchor)


def compute_dow_multipliers(
    dates: Sequence[date],
    values: Sequence[float],
    *,
    min_days: int = 21,
    prior_weeks: float = 3.0,
) -> Tuple[Dict[str, float], bool]:
    """
    Compute Bayesian-shrunk day-of-week spending seasonality multipliers when at least
    `min_days` (default 21) of daily observations are present.

    Multipliers are clamped to `[0.40, 2.20]` and normalized so their 7-day average is 1.0.
    """
    flat = {k: 1.0 for k in WEEKDAY_KEYS}
    if len(dates) != len(values) or len(dates) < int(min_days):
        return flat, False

    overall_mean = sum(max(0.0, float(v)) for v in values) / float(len(values))
    if overall_mean <= 0.0:
        return flat, False

    buckets: Dict[int, List[float]] = {wd: [] for wd in range(7)}
    for dt, val in zip(dates, values, strict=True):
        buckets[dt.weekday()].append(max(0.0, float(val)))

    k_prior = max(0.5, float(prior_weeks))
    raw_mults: List[float] = []
    for wd in range(7):
        obs = buckets[wd]
        n_wd = len(obs)
        wd_mean = (sum(obs) / float(n_wd)) if n_wd > 0 else overall_mean
        shrunk_wd = (n_wd * wd_mean + k_prior * overall_mean) / (n_wd + k_prior)
        raw_mult = min(2.20, max(0.40, shrunk_wd / overall_mean))
        raw_mults.append(raw_mult)

    avg_mult = sum(raw_mults) / 7.0
    if avg_mult <= 0.0:
        return flat, False

    norm_mults = [m / avg_mult for m in raw_mults]
    result = {WEEKDAY_KEYS[wd]: round(norm_mults[wd], 4) for wd in range(7)}
    return result, True


@dataclass
class MannKendallTrendResult:
    """Non-parametric Mann-Kendall test + Theil-Sen / OLS weekly spend drift telemetry."""

    weeks_analyzed: int = 0
    drift_cents_per_week: int = 0
    linear_slope_cents_per_week: int = 0
    mann_kendall_s: int = 0
    mann_kendall_z: float = 0.0
    mann_kendall_tau: float = 0.0
    p_value: float = 1.0
    is_significant: bool = False
    trend_direction: str = "stable"


def compute_mann_kendall_slope(
    weekly_values: Sequence[float],
    *,
    significance_alpha: float = 0.10,
    min_material_cents: int = 500,
) -> MannKendallTrendResult:
    """
    Detect statistically significant upward or downward weekly spending drift using the
    exact tie-corrected Mann-Kendall S-test, Theil-Sen median pairwise slope, and OLS slope.
    """
    vals = [float(v) for v in weekly_values]
    n = len(vals)
    if n < 2:
        return MannKendallTrendResult(weeks_analyzed=n)

    # OLS linear slope (cents/week)
    x_bar = (n - 1) / 2.0
    y_bar = sum(vals) / float(n)
    denom_x = sum((i - x_bar) ** 2 for i in range(n))
    ols_slope = (
        sum((i - x_bar) * (vals[i] - y_bar) for i in range(n)) / denom_x
        if denom_x > 0.0
        else 0.0
    )

    # Exact pairwise Mann-Kendall S statistic + Theil-Sen median slope
    s_stat = 0
    pairwise_slopes: List[float] = []
    for i in range(n - 1):
        vi = vals[i]
        for j in range(i + 1, n):
            diff = vals[j] - vi
            if diff > 0.0:
                s_stat += 1
            elif diff < 0.0:
                s_stat -= 1
            pairwise_slopes.append(diff / float(j - i))

    theil_sen = float(median(pairwise_slopes)) if pairwise_slopes else ols_slope
    drift_cents = int(round(theil_sen))
    ols_cents = int(round(ols_slope))

    total_pairs = n * (n - 1) / 2.0
    tau = round(s_stat / total_pairs, 4) if total_pairs > 0 else 0.0

    if n < 4:
        return MannKendallTrendResult(
            weeks_analyzed=n,
            drift_cents_per_week=drift_cents,
            linear_slope_cents_per_week=ols_cents,
            mann_kendall_s=s_stat,
            mann_kendall_z=0.0,
            mann_kendall_tau=tau,
            p_value=1.0,
            is_significant=False,
            trend_direction="stable",
        )

    # Tie-corrected variance Var(S)
    counts = Counter(vals)
    tie_correction = sum(g * (g - 1) * (2 * g + 5) for g in counts.values() if g > 1)
    var_s = (n * (n - 1) * (2 * n + 5) - tie_correction) / 18.0
    if var_s <= 0.0 or s_stat == 0:
        z_stat = 0.0
        p_val = 1.0
    else:
        sigma_s = math.sqrt(var_s)
        z_stat = (s_stat - 1.0) / sigma_s if s_stat > 0 else (s_stat + 1.0) / sigma_s
        p_val = math.erfc(abs(z_stat) / math.sqrt(2.0))

    p_val_clamped = round(min(1.0, max(0.0, p_val)), 4)
    is_sig = p_val <= float(significance_alpha) and abs(drift_cents) >= int(min_material_cents)
    if is_sig and drift_cents > 0:
        direction = "accelerating"
    elif is_sig and drift_cents < 0:
        direction = "cooling"
    else:
        direction = "stable"

    return MannKendallTrendResult(
        weeks_analyzed=n,
        drift_cents_per_week=drift_cents,
        linear_slope_cents_per_week=ols_cents,
        mann_kendall_s=s_stat,
        mann_kendall_z=round(z_stat, 3),
        mann_kendall_tau=tau,
        p_value=p_val_clamped,
        is_significant=is_sig,
        trend_direction=direction,
    )


def compute_hhi_index(amounts: Sequence[int]) -> Tuple[int, str]:
    """
    Compute the Herfindahl-Hirschman Index (HHI) on the canonical `0..10000` scale.

    Returns `(hhi_0_to_10000, concentration_band)` where band is:
    - `"none"` when empty or zero total
    - `"diversified"` when `HHI < 1500`
    - `"moderate"` when `1500 <= HHI < 2500`
    - `"concentrated"` when `HHI >= 2500`
    """
    positives = [int(a) for a in amounts if int(a) > 0]
    total = sum(positives)
    if total <= 0:
        return 0, "none"

    total_dec = Decimal(total)
    hundred = Decimal(100)
    hhi_dec = sum(((Decimal(a) * hundred) / total_dec) ** 2 for a in positives)
    hhi_int = int(hhi_dec.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))
    hhi_clamped = max(0, min(10_000, hhi_int))
    if hhi_clamped < 1500:
        band = "diversified"
    elif hhi_clamped < 2500:
        band = "moderate"
    else:
        band = "concentrated"
    return hhi_clamped, band


def allocate_kkt_concave_cents(
    total_cents: int,
    weights: Sequence[float],
    scales: Sequence[float],
    caps: Sequence[int],
) -> Tuple[List[int], float]:
    """
    Solve the strictly concave separable KKT resource-allocation problem in exact integer cents:

        maximize   sum_i  alpha_i * beta_i * ln(1 + x_i / beta_i)
        subject to 0 <= x_i <= caps[i],   sum_i x_i <= total_cents

    Marginal utility is `MU_i(x_i) = alpha_i / (1 + x_i / beta_i)`.
    At optimality, active unconstrained buckets share an identical shadow price `lambda*`.
    Returns `(exact_integer_cent_allocations, shadow_price_lambda)`.
    """
    n = len(weights)
    if n == 0 or total_cents <= 0:
        return [0] * n, 0.0

    budget = int(total_cents)
    active = [i for i in range(n) if int(caps[i]) > 0 and float(weights[i]) > 0.0]
    if not active:
        return [0] * n, 0.0

    total_cap = sum(int(caps[i]) for i in active)
    if total_cap <= budget:
        out = [int(caps[i]) if i in active else 0 for i in range(n)]
        return out, 0.0

    alphas = [max(0.0, float(w)) for w in weights]
    betas = [max(1.0, float(s)) for s in scales]
    cap_ints = [max(0, int(c)) for c in caps]

    hi = max(alphas[i] for i in active)
    lo = min(alphas[i] / (1.0 + cap_ints[i] / betas[i]) for i in active) * 0.999
    lo = max(1e-12, lo)

    def _primal(lam: float) -> List[float]:
        res = [0.0] * n
        for i in active:
            raw = betas[i] * ((alphas[i] / lam) - 1.0)
            if raw <= 0.0:
                res[i] = 0.0
            elif raw >= cap_ints[i]:
                res[i] = float(cap_ints[i])
            else:
                res[i] = raw
        return res

    # Exact 70-step bisection on strictly decreasing primal sum(x_i(lambda))
    for _ in range(70):
        mid = 0.5 * (lo + hi)
        if sum(_primal(mid)) > float(budget):
            lo = mid
        else:
            hi = mid

    lam_star = 0.5 * (lo + hi)
    raw_alloc = _primal(lam_star)

    # Exact largest-remainder integer-cent discretization conserving sum(allocs) == budget
    allocs = [min(cap_ints[i], int(math.floor(raw_alloc[i]))) for i in range(n)]
    leftover = budget - sum(allocs)
    remainders = [
        (i, raw_alloc[i] - allocs[i], alphas[i])
        for i in active
        if allocs[i] < cap_ints[i]
    ]
    remainders.sort(key=lambda item: (-item[1], -item[2], item[0]))
    for i, _rem, _a in remainders:
        if leftover <= 0:
            break
        allocs[i] += 1
        leftover -= 1

    # Fallback pass if fractional remainders exhausted while headroom remains
    if leftover > 0:
        for i in sorted(active, key=lambda idx: (-alphas[idx], idx)):
            if leftover <= 0:
                break
            room = cap_ints[i] - allocs[i]
            if room > 0:
                take = min(leftover, room)
                allocs[i] += take
                leftover -= take

    return allocs, round(lam_star, 6)


@dataclass
class SeasonalBaseline:
    """730-day seasonal discretionary spending baseline layered beneath 90-day volatility."""

    has_seasonal_data: bool = False
    history_span_days: int = 0
    overall_730d_weekly_mean_cents: int = 0
    same_season_weekly_mean_cents: int = 0
    seasonal_multiplier: float = 1.0
    seasonal_buffer_uplift_cents: int = 0


@dataclass
class CashflowRunwayResult:
    """Exact day-by-day projected cashflow runway & overdraft simulation output."""

    horizon_days: int
    starting_balance_cents: int
    ending_balance_cents: int
    lowest_balance_cents: int
    lowest_balance_date: str
    overdraft_risk: bool
    safe_runway_days: int
    payday_crunch_warning: Optional[str]
    daily_points: List[DailyCashflowPoint] = field(default_factory=list)
    total_inflows_cents: int = 0
    total_outflows_cents: int = 0
    net_cashflow_cents: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "horizon_days": self.horizon_days,
            "starting_balance_cents": self.starting_balance_cents,
            "ending_balance_cents": self.ending_balance_cents,
            "lowest_balance_cents": self.lowest_balance_cents,
            "lowest_balance_date": self.lowest_balance_date,
            "overdraft_risk": self.overdraft_risk,
            "safe_runway_days": self.safe_runway_days,
            "payday_crunch_warning": self.payday_crunch_warning,
            "daily_points": [p.model_dump() for p in self.daily_points],
            "total_inflows_cents": self.total_inflows_cents,
            "total_outflows_cents": self.total_outflows_cents,
            "net_cashflow_cents": self.net_cashflow_cents,
        }


class QuantMathEngine:
    """
    Deterministic quantitative personal finance intelligence engine.

    All monetary quantities are computed strictly in exact integer cents (`int`).
    Zero external LLM calls; zero RSD-shaming phrasing.
    """

    # =========================================================================
    # 1. 30/60/90-Day Cashflow Runway & Overdraft Simulator
    # =========================================================================

    def simulate_cashflow_runway(
        self,
        *,
        starting_balance_cents: int,
        recurring_bills: Sequence[Any],
        transactions: Optional[Sequence[Any]] = None,
        credit_lines: Optional[Sequence[Any]] = None,
        azeo_plan: Optional[Sequence[Any]] = None,
        payday_cadence_days: int = 14,
        next_payday_date: Optional[str] = None,
        estimated_paycheck_cents: int = 0,
        emergency_buffer_cents: int = 0,
        days: int = 30,
        max_horizon_days: int = 90,
        include_azeo_paydowns: bool = True,
        azeo_respect_buffer: bool = True,
        daily_discretionary_cents: int = 0,
        today: Optional[date] = None,
    ) -> CashflowRunwayResult:
        """
        Project exact daily closing cash balance from `today` across `days` (1..max_horizon_days).

        Incorporates:
        - All 8 recurring bill cadences (weekly, biweekly, semimonthly, monthly,
          bimonthly, quarterly, semiannual, annual)
        - Recurring paychecks / income cadence
        - Credit card & debt minimum payments on their `payment_due_day`
        - AZEO pre-statement-close paydown recommendations (without double-counting
          same-cycle minimums and respecting emergency cash shields by default)
        """
        ref = today or date.today()
        cap_days = max(1, min(730, int(max_horizon_days)))
        horizon = max(1, min(cap_days, int(days)))
        end_dt = ref + timedelta(days=horizon - 1)
        start_bal = int(starting_balance_cents)
        buffer_floor = max(0, int(emergency_buffer_cents))
        daily_disc = max(0, int(daily_discretionary_cents))

        inflows_by_day: Dict[date, int] = defaultdict(int)
        outflows_by_day: Dict[date, int] = defaultdict(int)
        events_by_day: Dict[date, List[str]] = defaultdict(list)

        # --- A. Paychecks / Recurring Income ---
        pay_cadence = max(7, int(payday_cadence_days))
        pay_amt = max(0, int(estimated_paycheck_cents))
        pay_start_iso = next_payday_date

        # Infer payday from transaction history only if caller did not supply an explicit payday date
        if not pay_start_iso and transactions:
            tx_dicts = [to_dict(t) if not isinstance(t, dict) else t for t in transactions]
            has_income_tx = any(
                t.get("tx_type") == "income" and int(t.get("amount_cents", 0)) >= 20_000
                for t in tx_dicts
            )
            if has_income_tx:
                detected = detect_payday_schedule(
                    transactions=tx_dicts,
                    fallback_cadence_days=pay_cadence,
                    fallback_payday_date=None,
                    fallback_paycheck_cents=pay_amt,
                    today=ref,
                )
                pay_cadence = detected.cadence_days
                pay_start_iso = detected.next_payday_date
                if pay_amt == 0:
                    pay_amt = detected.estimated_paycheck_cents

        payday_dates: List[date] = []
        if pay_amt > 0:
            if pay_start_iso:
                try:
                    pay_dt = date.fromisoformat(str(pay_start_iso))
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
                events_by_day[pay_dt].append(f"Paycheck (+{_fmt(pay_amt)})")
                payday_dates.append(pay_dt)
                pay_dt = _step_payday_date(pay_dt, pay_cadence, anchor_pay_day)
                guard += 1

        # Track merchants paid within the last 5 days to prevent double-counting a bill due today/yesterday
        recent_paid_merchants: set[str] = set()
        if transactions:
            for tx in transactions:
                if _enum_val(_get(tx, "tx_type")) != "expense":
                    continue
                tx_date_str = str(_get(tx, "tx_date", ""))
                try:
                    tx_dt = date.fromisoformat(tx_date_str)
                except ValueError:
                    continue
                if 0 <= (ref - tx_dt).days <= 5:
                    norm = str(_get(tx, "normalized_merchant") or "").strip().lower()
                    if norm:
                        recent_paid_merchants.add(norm)

        # --- B. Recurring Bills (all 8 cadences) ---
        for bill in recurring_bills:
            if not _get(bill, "active", True):
                continue
            bill_map = bill if isinstance(bill, dict) else to_dict(bill)
            amt = forecast_bill_amount_cents(bill_map)
            if amt <= 0:
                continue
            due_str = str(_get(bill, "next_due_date", ""))
            try:
                due_dt = date.fromisoformat(due_str)
            except ValueError:
                continue
            cadence_raw = _enum_val(_get(bill, "cadence", RecurrenceCadence.MONTHLY.value))
            try:
                cadence = RecurrenceCadence(cadence_raw)
            except ValueError:
                cadence = RecurrenceCadence.MONTHLY

            name = str(_get(bill, "name", "Recurring Bill"))
            norm = str(_get(bill, "normalized_merchant", "")).strip().lower()
            anchor_day = due_dt.day

            overdue_days = (ref - due_dt).days
            if overdue_days > 0:
                if overdue_days <= 5 and norm not in recent_paid_merchants:
                    outflows_by_day[ref] += amt
                    events_by_day[ref].append(f"Overdue bill: {name} (-{_fmt(amt)})")
                # Advance to next future cycle strictly after `ref`
                guard = 0
                while due_dt <= ref and guard < 400:
                    due_dt = _step_bill_date(due_dt, cadence, anchor_day)
                    guard += 1
            elif overdue_days == 0 and norm in recent_paid_merchants:
                # Already paid today/recently; advance to next billing cycle
                due_dt = _step_bill_date(due_dt, cadence, anchor_day)

            guard = 0
            while due_dt <= end_dt and guard < 400:
                if due_dt >= ref:
                    outflows_by_day[due_dt] += amt
                    events_by_day[due_dt].append(f"Bill: {name} (-{_fmt(amt)})")
                due_dt = _step_bill_date(due_dt, cadence, anchor_day)
                guard += 1

        # --- C. Credit Line Minimum Payments on `payment_due_day` ---
        # Track scheduled minimums per credit line so AZEO pre-close paydowns never double-count them!
        # Also skip any credit/collection line already covered by an active recurring_bill (e.g. biweekly payment plan).
        from chronox_finance.engines.recurring import credit_line_covered_by_recurring_bill

        min_events_by_line: Dict[int, List[Tuple[date, int, str]]] = defaultdict(list)
        line_meta: Dict[int, Dict[str, Any]] = {}
        if credit_lines:
            for cl in credit_lines:
                if not _get(cl, "active", True):
                    continue
                bal = max(0, int(_get(cl, "balance_cents", 0)))
                if bal <= 0:
                    continue
                cl_id = int(_get(cl, "id", 0))
                name = str(_get(cl, "name", f"Credit Line #{cl_id}"))
                kind_str = _enum_val(_get(cl, "kind", DebtKind.CREDIT_CARD.value))
                try:
                    kind = DebtKind(kind_str)
                except ValueError:
                    kind = DebtKind.CREDIT_CARD
                eff_min = _get(cl, "effective_min_payment_cents")
                if eff_min is None:
                    apr_bps = int(_get(cl, "effective_apr_bps", _get(cl, "apr_bps", 0)))
                    eff_min = compute_effective_min_payment(
                        bal, int(_get(cl, "min_payment_cents", 0)), kind, apr_bps
                    )
                eff_min = max(0, int(eff_min))
                if eff_min <= 0:
                    continue
                if credit_line_covered_by_recurring_bill(cl, recurring_bills, effective_min_cents=eff_min):
                    continue

                due_day = int(_get(cl, "payment_due_day", 10))
                line_meta[cl_id] = {"name": name, "balance": bal, "eff_min": eff_min, "due_day": due_day}
                sim_rem = bal
                due_dt = next_occurrence_of_day(due_day, ref)
                step_idx = 0
                while due_dt <= end_dt and sim_rem > 0 and step_idx < 30:
                    pay = min(sim_rem, eff_min)
                    min_events_by_line[cl_id].append((due_dt, pay, name))
                    sim_rem -= pay
                    step_idx += 1
                    due_dt = _add_months_clamped(due_dt, 1, due_day)

        # Build mandatory-only daily baseline first so we know exact future headroom before scheduling optional AZEO
        mandatory_out = dict(outflows_by_day)
        for items in min_events_by_line.values():
            for due_dt, pay, _name in items:
                mandatory_out[due_dt] = mandatory_out.get(due_dt, 0) + pay

        baseline_balances: List[int] = []
        running_base = start_bal
        for i in range(horizon):
            d = ref + timedelta(days=i)
            running_base += inflows_by_day.get(d, 0) - mandatory_out.get(d, 0) - daily_disc
            baseline_balances.append(running_base)

        # Suffix minima of baseline balances: `suffix_min[i]` = min closing balance on days i..horizon-1
        suffix_min = [0] * horizon
        cur_min = baseline_balances[-1]
        for i in range(horizon - 1, -1, -1):
            cur_min = min(cur_min, baseline_balances[i])
            suffix_min[i] = cur_min

        # --- D. AZEO Pre-Close Paydowns (without double-counting same-cycle minimums) ---
        azeo_committed_outflows: Dict[date, int] = defaultdict(int)
        azeo_events: Dict[date, List[str]] = defaultdict(list)
        azeo_cumulative_incremental = 0
        reduced_min_by_line: Dict[int, int] = defaultdict(int)

        if include_azeo_paydowns and azeo_plan:
            sorted_azeo = sorted(
                azeo_plan,
                key=lambda a: (str(_get(a, "pay_by_date", "9999-12-31")), -int(_get(a, "pay_before_close_cents", 0))),
            )
            for action in sorted_azeo:
                pay_amt = max(0, int(_get(action, "pay_before_close_cents", 0)))
                if pay_amt <= 0:
                    continue
                pay_by_str = str(_get(action, "pay_by_date", ""))
                try:
                    pay_by_dt = date.fromisoformat(pay_by_str)
                except ValueError:
                    continue
                if not (ref <= pay_by_dt <= end_dt):
                    continue

                cl_id = int(_get(action, "credit_line_id", 0))
                card_name = str(_get(action, "card_name", "Card"))
                day_idx = (pay_by_dt - ref).days

                # Offset first upcoming minimum payment on this card within ~31 days so we never double-count
                first_min_offset = 0
                for m_dt, m_pay, _m_name in min_events_by_line.get(cl_id, []):
                    if (m_dt - pay_by_dt).days <= 31:
                        first_min_offset = min(m_pay, pay_amt)
                        break

                incremental_cost = max(0, pay_amt - first_min_offset)
                projected_future_min = suffix_min[day_idx] - azeo_cumulative_incremental - incremental_cost

                if not azeo_respect_buffer or projected_future_min >= buffer_floor:
                    azeo_committed_outflows[pay_by_dt] += pay_amt
                    azeo_events[pay_by_dt].append(f"AZEO pre-close: {card_name} (-{_fmt(pay_amt)})")
                    azeo_cumulative_incremental += incremental_cost
                    if first_min_offset > 0:
                        reduced_min_by_line[cl_id] += first_min_offset
                else:
                    azeo_events[pay_by_dt].append(
                        f"AZEO advisory ({card_name}): {_fmt(pay_amt)} pre-close paydown deferred to protect buffer"
                    )

        # Apply credit minimums (net of any AZEO offset in the first cycle) + AZEO events
        for cl_id, items in min_events_by_line.items():
            credit_offset = reduced_min_by_line.get(cl_id, 0)
            for due_dt, pay, name in items:
                if credit_offset > 0:
                    absorbed = min(pay, credit_offset)
                    pay -= absorbed
                    credit_offset -= absorbed
                if pay > 0:
                    outflows_by_day[due_dt] += pay
                    events_by_day[due_dt].append(f"Credit min: {name} (-{_fmt(pay)})")

        for d, amt in azeo_committed_outflows.items():
            outflows_by_day[d] += amt
        for d, evs in azeo_events.items():
            events_by_day[d].extend(evs)

        # --- E. Daily Simulation Loop (Exact Integer Cents) ---
        daily_points: List[DailyCashflowPoint] = []
        running_balance = start_bal
        lowest_bal = start_bal
        lowest_date = ref.isoformat()
        overdraft_risk = False
        first_overdraft_idx: Optional[int] = None
        total_inflows = 0
        total_outflows = 0

        for i in range(horizon):
            d = ref + timedelta(days=i)
            inflow = inflows_by_day.get(d, 0)
            outflow = outflows_by_day.get(d, 0) + daily_disc
            evs = list(events_by_day.get(d, []))
            if daily_disc > 0:
                evs.append(f"Estimated daily spend (-{_fmt(daily_disc)})")

            running_balance = running_balance + inflow - outflow
            total_inflows += inflow
            total_outflows += outflow

            if i == 0 or running_balance < lowest_bal:
                lowest_bal = running_balance
                lowest_date = d.isoformat()

            if running_balance < 0:
                overdraft_risk = True
                if first_overdraft_idx is None:
                    first_overdraft_idx = i

            daily_points.append(
                DailyCashflowPoint(
                    date=d.isoformat(),
                    balance_cents=running_balance,
                    inflow_cents=inflow,
                    outflow_cents=outflow,
                    events=evs,
                )
            )

        safe_runway_days = horizon if first_overdraft_idx is None else first_overdraft_idx

        # --- F. Calm, RSD-Safe Payday Crunch Warning ---
        warning: Optional[str] = None
        nadir_dt = date.fromisoformat(lowest_date)
        next_pay_after_nadir = next((p for p in payday_dates if p > nadir_dt), None)
        pay_hint = (
            f" before your paycheck on {next_pay_after_nadir.isoformat()}"
            if next_pay_after_nadir
            else ""
        )

        if overdraft_risk:
            shortfall = abs(lowest_bal)
            warning = (
                f"Cashflow crunch ahead: balance projects to {_fmt(lowest_bal)} on {lowest_date} "
                f"(short by {_fmt(shortfall)}{pay_hint}). Pausing non-essential spending or shifting a flexible "
                f"charge after payday keeps checking safely positive."
            )
        elif buffer_floor > 0 and lowest_bal < buffer_floor:
            dip = buffer_floor - lowest_bal
            warning = (
                f"Runway stays positive, with a temporary dip to {_fmt(lowest_bal)} on {lowest_date} "
                f"({_fmt(dip)} inside your {_fmt(buffer_floor)} emergency shield{pay_hint})."
            )

        return CashflowRunwayResult(
            horizon_days=horizon,
            starting_balance_cents=start_bal,
            ending_balance_cents=running_balance,
            lowest_balance_cents=lowest_bal,
            lowest_balance_date=lowest_date,
            overdraft_risk=overdraft_risk,
            safe_runway_days=safe_runway_days,
            payday_crunch_warning=warning,
            daily_points=daily_points,
            total_inflows_cents=total_inflows,
            total_outflows_cents=total_outflows,
            net_cashflow_cents=total_inflows - total_outflows,
        )

    # =========================================================================
    # 2. Spending Volatility, 730-Day Seasonal Baseline & Statistical Buffer
    # =========================================================================

    def _collect_split_aware_discretionary(
        self,
        *,
        transactions: Sequence[Any],
        window_start: date,
        ref: date,
        non_disc_jar_ids: set[int],
        essential_merchants: set[str],
        splits_by_tx_id: Optional[Dict[int, List[Any]]] = None,
    ) -> Tuple[Dict[date, int], Optional[date]]:
        """Collect split-aware discretionary expense totals by date with zero double-counting."""
        disc_by_day: Dict[date, int] = defaultdict(int)
        earliest_disc: Optional[date] = None

        for tx in transactions:
            if _enum_val(_get(tx, "tx_type")) != "expense":
                continue
            dt_str = str(_get(tx, "tx_date", ""))
            try:
                tx_dt = date.fromisoformat(dt_str[:10])
            except ValueError:
                continue
            if tx_dt < window_start or tx_dt > ref:
                continue

            norm = str(_get(tx, "normalized_merchant") or "").strip().lower()
            if norm and norm in essential_merchants:
                continue

            tx_disc_amt = 0
            for jid, _goal_id, alloc_amt, _memo in iter_effective_tx_allocations(tx, splits_by_tx_id):
                if jid is not None and jid in non_disc_jar_ids:
                    continue
                tx_disc_amt += alloc_amt

            if tx_disc_amt <= 0:
                continue
            disc_by_day[tx_dt] += tx_disc_amt
            if earliest_disc is None or tx_dt < earliest_disc:
                earliest_disc = tx_dt

        return disc_by_day, earliest_disc

    def compute_seasonal_baseline(
        self,
        *,
        transactions: Sequence[Any],
        jars: Optional[Sequence[Any]] = None,
        recurring_bills: Optional[Sequence[Any]] = None,
        splits_by_tx_id: Optional[Dict[int, List[Any]]] = None,
        recent_lookback_days: int = 90,
        seasonal_lookback_days: int = 730,
        today: Optional[date] = None,
    ) -> SeasonalBaseline:
        """
        Inspect up to 730 days (2 full annual cycles) of split-aware discretionary spending
        to quantify prior-year same-season spending pressure without diluting recent 90d volatility.
        """
        ref = today or date.today()
        span_cap = max(recent_lookback_days, min(730, int(seasonal_lookback_days)))
        seasonal_start = ref - timedelta(days=span_cap - 1)

        non_disc_jar_ids: set[int] = set()
        if jars:
            for j in jars:
                kind = _enum_val(_get(j, "kind"))
                if kind in (JarKind.BILLS.value, JarKind.SAFETY.value, JarKind.INVEST.value):
                    jid = _get(j, "id")
                    if jid is not None:
                        non_disc_jar_ids.add(int(jid))

        essential_merchants: set[str] = set()
        if recurring_bills:
            for b in recurring_bills:
                if _get(b, "active", True) and _get(b, "is_essential", True):
                    norm = str(_get(b, "normalized_merchant") or "").strip().lower()
                    if norm:
                        essential_merchants.add(norm)

        all_disc, earliest = self._collect_split_aware_discretionary(
            transactions=transactions,
            window_start=seasonal_start,
            ref=ref,
            non_disc_jar_ids=non_disc_jar_ids,
            essential_merchants=essential_merchants,
            splits_by_tx_id=splits_by_tx_id,
        )
        if earliest is None:
            return SeasonalBaseline()

        history_span_days = (ref - earliest).days + 1
        # Only activate seasonal baseline when history genuinely reaches back > 180 days (prior season/year)
        if history_span_days <= max(180, recent_lookback_days):
            return SeasonalBaseline(has_seasonal_data=False, history_span_days=history_span_days)

        # Overall 730d weekly mean across active history span
        num_weeks_all = max(1, (history_span_days - 1) // 7 + 1)
        overall_weekly_mean = int(round(sum(all_disc.values()) / num_weeks_all))

        # Same-calendar-season windows from 1 year ago (~365d +/- 21d) and 2 years ago (~730d +/- 21d)
        same_season_totals: List[int] = []
        for year_back in (1, 2):
            anchor = ref - timedelta(days=365 * year_back)
            win_lo = anchor - timedelta(days=21)
            win_hi = anchor + timedelta(days=21)
            if win_hi < earliest:
                continue
            window_spend = sum(amt for d, amt in all_disc.items() if win_lo <= d <= win_hi)
            if window_spend > 0:
                # 43-day window = 43 / 7 weeks
                same_season_totals.append(int(round(window_spend / (43.0 / 7.0))))

        if not same_season_totals or overall_weekly_mean <= 0:
            return SeasonalBaseline(
                has_seasonal_data=False,
                history_span_days=history_span_days,
                overall_730d_weekly_mean_cents=overall_weekly_mean,
            )

        same_season_weekly_mean = int(round(mean(same_season_totals)))
        raw_mult = same_season_weekly_mean / max(1, overall_weekly_mean)
        seasonal_mult = round(max(0.75, min(1.50, raw_mult)), 3)
        excess_weekly = max(0, same_season_weekly_mean - overall_weekly_mean)
        # Blend 30% of 2-week seasonal excess into emergency buffer sizing
        buffer_uplift = int(round(excess_weekly * 2.0 * 0.30))

        return SeasonalBaseline(
            has_seasonal_data=True,
            history_span_days=history_span_days,
            overall_730d_weekly_mean_cents=overall_weekly_mean,
            same_season_weekly_mean_cents=same_season_weekly_mean,
            seasonal_multiplier=seasonal_mult,
            seasonal_buffer_uplift_cents=buffer_uplift,
        )

    def compute_spending_volatility(
        self,
        *,
        transactions: Sequence[Any],
        safe_to_spend_cents: int = 0,
        days_until_payday: int = 14,
        jars: Optional[Sequence[Any]] = None,
        recurring_bills: Optional[Sequence[Any]] = None,
        current_emergency_buffer_cents: int = MIN_EMERGENCY_BUFFER_CENTS,
        monthly_essential_bills_cents: int = 0,
        smoothed_monthly_income_cents: int = 0,
        lookback_days: int = 90,
        seasonal_lookback_days: int = 730,
        splits_by_tx_id: Optional[Dict[int, List[Any]]] = None,
        today: Optional[date] = None,
    ) -> SpendingVolatilityMetrics:
        """
        Analyze trailing 90-day split-aware discretionary spending to keep short-term
        volatility sharp (mean, std dev, CV, P50/P75/P90 weekly spend, P50/P90 daily safe
        confidence bands) while blending a 730-day seasonal baseline into statistical
        emergency buffer sizing when multi-year history is available.
        """
        ref = today or date.today()
        window_days = max(7, min(730, int(lookback_days)))
        window_start = ref - timedelta(days=window_days - 1)
        safe_total = max(0, int(safe_to_spend_cents))
        pay_days = max(1, int(days_until_payday))
        base_daily_safe = safe_total // pay_days
        cur_buffer = max(0, int(current_emergency_buffer_cents))
        ess_bills = max(0, int(monthly_essential_bills_cents))
        monthly_inc = max(0, int(smoothed_monthly_income_cents))

        # Identify non-discretionary jars (Bills, Safety, Invest) and essential bill merchants
        non_disc_jar_ids: set[int] = set()
        if jars:
            for j in jars:
                kind = _enum_val(_get(j, "kind"))
                if kind in (JarKind.BILLS.value, JarKind.SAFETY.value, JarKind.INVEST.value):
                    jid = _get(j, "id")
                    if jid is not None:
                        non_disc_jar_ids.add(int(jid))

        essential_merchants: set[str] = set()
        if recurring_bills:
            for b in recurring_bills:
                if _get(b, "active", True) and _get(b, "is_essential", True):
                    norm = str(_get(b, "normalized_merchant") or "").strip().lower()
                    if norm:
                        essential_merchants.add(norm)

        # Collect trailing split-aware discretionary expense transactions
        disc_by_day, earliest_disc = self._collect_split_aware_discretionary(
            transactions=transactions,
            window_start=window_start,
            ref=ref,
            non_disc_jar_ids=non_disc_jar_ids,
            essential_merchants=essential_merchants,
            splits_by_tx_id=splits_by_tx_id,
        )

        # Empty discretionary history: calm defaults with prudent 15% P90 cushion
        if earliest_disc is None:
            fallback_buf = max(MIN_EMERGENCY_BUFFER_CENTS, cur_buffer, ess_bills // 2)
            p90_daily = int(round(base_daily_safe * 0.85))
            return SpendingVolatilityMetrics(
                sample_days=0,
                daily_mean_cents=0,
                daily_std_cents=0,
                weekly_mean_cents=0,
                weekly_std_cents=0,
                coefficient_of_variation=0.0,
                p50_weekly_spend_cents=0,
                p75_weekly_spend_cents=0,
                p90_weekly_spend_cents=0,
                recommended_buffer_cents=fallback_buf,
                p50_daily_safe_cents=base_daily_safe,
                p90_conservative_daily_safe_cents=min(base_daily_safe, p90_daily),
                stability_score_0_100=75,
            )

        sample_days = max(1, min(window_days, (ref - earliest_disc).days + 1))

        # Build exact daily series over active observation span
        daily_series: List[int] = [
            disc_by_day.get(earliest_disc + timedelta(days=offset), 0)
            for offset in range(sample_days)
        ]
        daily_mean_cents = int(round(mean(daily_series)))
        daily_std_cents = int(round(stdev(daily_series))) if len(daily_series) >= 2 else 0

        # Group into contiguous 7-day buckets anchored at `ref` so zero transactions are dropped
        num_weeks = (sample_days - 1) // 7 + 1
        weekly_totals: List[int] = [0] * num_weeks
        for tx_dt, amt in disc_by_day.items():
            age_days = (ref - tx_dt).days
            w_idx = min(num_weeks - 1, age_days // 7)
            weekly_totals[w_idx] += amt

        weekly_mean_float = mean(weekly_totals)
        weekly_mean_cents = int(round(weekly_mean_float))
        weekly_std_float = stdev(weekly_totals) if len(weekly_totals) >= 2 else 0.0
        weekly_std_cents = int(round(weekly_std_float))
        cv = round(weekly_std_float / weekly_mean_float, 3) if weekly_mean_float > 0 else 0.0

        p50_week = _percentile_cents(weekly_totals, 0.50)
        p75_week = _percentile_cents(weekly_totals, 0.75)
        p90_week = _percentile_cents(weekly_totals, 0.90)

        # Statistical stability score: smooth exponential decay in CV (0.0 -> 100, 0.5 -> 65, 1.2 -> 36)
        if len(weekly_totals) < 2:
            stability = 80
        else:
            stability = max(5, min(100, int(round(100.0 * math.exp(-0.85 * cv)))))

        # Incorporate 730-day seasonal baseline uplift if prior-year seasonal history exists
        seasonal = self.compute_seasonal_baseline(
            transactions=transactions,
            jars=jars,
            recurring_bills=recurring_bills,
            splits_by_tx_id=splits_by_tx_id,
            recent_lookback_days=window_days,
            seasonal_lookback_days=seasonal_lookback_days,
            today=ref,
        )

        # Statistically sized dynamic emergency buffer:
        # 50% of essential monthly obligations + 2 weeks of P90 discretionary spend
        # + 95% one-sided weekly shock (1.645 * sigma) + optional 730d seasonal uplift
        baseline_bills_need = max(ess_bills, MIN_EMERGENCY_BUFFER_CENTS)
        raw_buffer = (
            int(round(0.5 * baseline_bills_need + 2.0 * p90_week + 1.645 * weekly_std_float))
            + seasonal.seasonal_buffer_uplift_cents
        )
        upper_cap = max(500_000, monthly_inc * 3) if monthly_inc > 0 else max(500_000, raw_buffer)
        recommended_buffer_cents = max(MIN_EMERGENCY_BUFFER_CENTS, min(upper_cap, raw_buffer))

        # Volatility-adjusted Safe-to-Spend confidence band (square-root-of-time tail scaling)
        p50_daily_safe = base_daily_safe
        weekly_upside_tail = max(0, p90_week - p50_week)
        parametric_tail = int(round(1.2816 * weekly_std_float))
        tail_week = max(weekly_upside_tail, parametric_tail)
        p90_reserve = int(round(tail_week * math.sqrt(pay_days / 7.0)))
        p90_total_safe = max(0, safe_total - p90_reserve)
        p90_daily_safe = min(p50_daily_safe, p90_total_safe // pay_days)

        return SpendingVolatilityMetrics(
            sample_days=sample_days,
            daily_mean_cents=daily_mean_cents,
            daily_std_cents=daily_std_cents,
            weekly_mean_cents=weekly_mean_cents,
            weekly_std_cents=weekly_std_cents,
            coefficient_of_variation=cv,
            p50_weekly_spend_cents=p50_week,
            p75_weekly_spend_cents=p75_week,
            p90_weekly_spend_cents=p90_week,
            recommended_buffer_cents=recommended_buffer_cents,
            p50_daily_safe_cents=p50_daily_safe,
            p90_conservative_daily_safe_cents=p90_daily_safe,
            stability_score_0_100=stability,
        )

    # =========================================================================
    # 3. Optimal Marginal-ROI Multi-Objective Surplus Allocator
    # =========================================================================

    def optimize_surplus_allocation(
        self,
        *,
        monthly_surplus_cents: int,
        credit_report: Optional[CreditOptimizationReport] = None,
        emergency_buffer_gap_cents: int = 0,
        goals: Optional[Sequence[GoalOut]] = None,
        safe_to_invest_cap_cents: Optional[int] = None,
        unfunded_minimums_cents: int = 0,
        today: Optional[date] = None,
    ) -> List[OptimalAllocationStep]:
        """
        Allocate every marginal cent of `monthly_surplus_cents` across competing
        objectives ranked by true risk-adjusted annualized return & FICO cliff value:

        (a) Autopay / minimum protection (infinite risk priority -> 999% equiv)
        (b) 0% promo APR cliff amortization (prevents 25%+ post-promo jump)
        (c) High-APR revolving debt (>12% APR, exact guaranteed after-tax return = APR)
        (d) AZEO / FICO utilization cliff crossing (>28.9% -> <=8.9% score tier unlock)
        (e) Emergency buffer deficit up to statistical target (liquidity insurance yield)
        (f) Tracked goals weighted by priority & deadline urgency
        (g) Invest vault / ChronoX trading cap (+ residual low-APR debt / safety reserve)

        Guarantee: when `monthly_surplus_cents > 0`,
        `sum(step.amount_cents for step in steps) == monthly_surplus_cents` to the exact cent.
        """
        ref = today or date.today()
        remaining = max(0, int(monthly_surplus_cents))
        if remaining <= 0:
            return []

        steps: List[OptimalAllocationStep] = []
        credit_lines: List[CreditLineOut] = list(credit_report.credit_lines) if credit_report else []
        rem_debt: Dict[int, int] = {cl.id: max(0, cl.balance_cents) for cl in credit_lines}

        # --- (a) Autopay / Minimum Protection (Infinite Risk Priority) ---
        if unfunded_minimums_cents > 0 and remaining > 0:
            alloc = min(remaining, int(unfunded_minimums_cents))
            benefit = max(LATE_FEE_SHIELD_CENTS, alloc)
            steps.append(
                OptimalAllocationStep(
                    bucket="autopay_protection",
                    target_name="Unfunded Required Minimums",
                    amount_cents=alloc,
                    expected_annual_benefit_cents=benefit,
                    effective_apr_equivalent_pct=999.0,
                    rationale=(
                        f"Cover {_fmt(alloc)} of required minimums first to eliminate late fees "
                        f"and protect your 35% FICO Payment History pillar."
                    ),
                    formula_note=(
                        f"Risk priority = ∞ (999% equiv); shields {_fmt(alloc)} min obligation "
                        f"vs $35+ late fee & 7-year late-payment mark"
                    ),
                )
            )
            remaining -= alloc

        no_autopay_lines = [
            cl
            for cl in credit_lines
            if cl.autopay_mode == AutopayMode.NONE and rem_debt.get(cl.id, 0) > 0 and cl.effective_min_payment_cents > 0
        ]
        no_autopay_lines.sort(key=lambda c: (c.days_until_due, -c.effective_min_payment_cents))
        for cl in no_autopay_lines:
            if remaining <= 0:
                break
            alloc = min(remaining, cl.effective_min_payment_cents, rem_debt[cl.id])
            if alloc <= 0:
                continue
            interest_part = int(round(alloc * (cl.effective_apr_bps / 10000.0)))
            benefit = LATE_FEE_SHIELD_CENTS + interest_part
            steps.append(
                OptimalAllocationStep(
                    bucket="autopay_protection",
                    target_name=f"{cl.name} (Autopay Minimum Shield)",
                    amount_cents=alloc,
                    expected_annual_benefit_cents=benefit,
                    effective_apr_equivalent_pct=999.0,
                    rationale=(
                        f"Autopay is off on {cl.name}. Ring-fencing {_fmt(alloc)} and enabling autopay "
                        f"before {cl.next_due_date} removes 100% of ADHD late-payment risk."
                    ),
                    formula_note=(
                        f"Risk priority = ∞ (999% equiv); $35 late-fee shield + "
                        f"{cl.effective_apr_bps / 100:.2f}% APR interest avoided = {_fmt(benefit)}/yr"
                    ),
                )
            )
            remaining -= alloc
            rem_debt[cl.id] -= alloc

        # --- (b) 0% Promo APR Cliff Amortization ---
        promo_lines = [
            cl
            for cl in credit_lines
            if cl.promo_days_remaining is not None
            and 0 <= cl.promo_days_remaining <= 180
            and rem_debt.get(cl.id, 0) > 0
            and (cl.promo_apr_bps if cl.promo_apr_bps is not None else cl.effective_apr_bps) < cl.apr_bps
        ]
        promo_lines.sort(key=lambda c: (c.promo_days_remaining or 999, -c.apr_bps))
        for cl in promo_lines:
            if remaining <= 0:
                break
            days_left = max(1, cl.promo_days_remaining or 1)
            months_left = max(1, int(math.ceil(days_left / 30.0)))
            rem_bal = rem_debt[cl.id]
            req_monthly = int(math.ceil(rem_bal / months_left))
            alloc = min(remaining, req_monthly, rem_bal)
            if alloc <= 0:
                continue
            post_apr_pct = round(cl.apr_bps / 100.0, 2)
            urgency_mult = 1.25 if days_left <= 90 else 1.10
            eff_equiv = round(post_apr_pct * urgency_mult, 2)
            benefit = int(round(alloc * (cl.apr_bps / 10000.0)))
            steps.append(
                OptimalAllocationStep(
                    bucket="promo_apr_cliff",
                    target_name=f"{cl.name} (0% Promo Cliff)",
                    amount_cents=alloc,
                    expected_annual_benefit_cents=benefit,
                    effective_apr_equivalent_pct=eff_equiv,
                    rationale=(
                        f"{cl.name} jumps to {post_apr_pct:.2f}% APR on {cl.promo_ends_date} ({days_left}d left). "
                        f"Allocating {_fmt(alloc)}/mo amortizes the {_fmt(rem_bal)} balance cleanly before the cliff."
                    ),
                    formula_note=(
                        f"ceil({_fmt(rem_bal)} / {months_left}mo) = {_fmt(req_monthly)}/mo; "
                        f"avoids {post_apr_pct:.2f}% post-promo jump ({eff_equiv:.2f}% cliff-adjusted equiv)"
                    ),
                )
            )
            remaining -= alloc
            rem_debt[cl.id] -= alloc

        # --- (c) High-APR Revolving Debt (>12% APR = >1200 bps) ---
        high_apr_lines = [
            cl
            for cl in credit_lines
            if rem_debt.get(cl.id, 0) > 0 and cl.effective_apr_bps > 1200
        ]
        high_apr_lines.sort(
            key=lambda c: (-c.effective_apr_bps, -(c.utilization_pct or 0.0), rem_debt[c.id])
        )
        for cl in high_apr_lines:
            if remaining <= 0:
                break
            alloc = min(remaining, rem_debt[cl.id])
            if alloc <= 0:
                continue
            apr_pct = round(cl.effective_apr_bps / 100.0, 2)
            benefit = int(round(alloc * (cl.effective_apr_bps / 10000.0)))
            steps.append(
                OptimalAllocationStep(
                    bucket="high_apr_debt",
                    target_name=cl.name,
                    amount_cents=alloc,
                    expected_annual_benefit_cents=benefit,
                    effective_apr_equivalent_pct=apr_pct,
                    rationale=(
                        f"Guaranteed risk-free after-tax return of {apr_pct:.2f}% APR by paying down "
                        f"{cl.name} — saving {_fmt(benefit)}/yr in compounding interest."
                    ),
                    formula_note=(
                        f"{_fmt(alloc)} * {apr_pct:.2f}% guaranteed APR = {_fmt(benefit)}/yr interest saved"
                    ),
                )
            )
            remaining -= alloc
            rem_debt[cl.id] -= alloc

        # --- (d) AZEO / Utilization Threshold Crossing (>28.9% -> <=8.9%) ---
        # Pass 1: high per-card utilization thresholds (>28.9% down to <=28.9%)
        # Pass 2: low-utilization AZEO target band (>8.9% down to <=8.9%)
        for threshold_pct, threshold_ratio, tier_label in (
            (28.9, 0.289, "28.9% utilization threshold"),
            (8.9, 0.089, "8.9% AZEO target band"),
        ):
            if remaining <= 0:
                break
            cliff_cards = [
                cl
                for cl in credit_lines
                if cl.kind == DebtKind.CREDIT_CARD
                and cl.credit_limit_cents > 0
                and rem_debt.get(cl.id, 0) > int(math.floor(cl.credit_limit_cents * threshold_ratio))
            ]
            cliff_cards.sort(
                key=lambda c: (
                    -(rem_debt[c.id] / c.credit_limit_cents),
                    c.days_until_close,
                )
            )
            for cl in cliff_cards:
                if remaining <= 0:
                    break
                rem_bal = rem_debt[cl.id]
                target_bal = int(math.floor(cl.credit_limit_cents * threshold_ratio))
                needed = max(0, rem_bal - target_bal)
                alloc = min(remaining, needed)
                if alloc <= 0:
                    continue
                before_util = round((rem_bal / cl.credit_limit_cents) * 100.0, 1)
                after_util = round(((rem_bal - alloc) / cl.credit_limit_cents) * 100.0, 1)
                eff_equiv = round(cl.effective_apr_bps / 100.0, 2)
                benefit = int(round(alloc * (cl.effective_apr_bps / 10000.0)))
                steps.append(
                    OptimalAllocationStep(
                        bucket="azeo_fico_cliff",
                        target_name=f"{cl.name} ({tier_label})",
                        amount_cents=alloc,
                        expected_annual_benefit_cents=benefit,
                        effective_apr_equivalent_pct=eff_equiv,
                        rationale=(
                            f"Paying {_fmt(alloc)} on {cl.name} before {cl.next_statement_close_date} "
                            f"drops reported utilization from {before_util:.1f}% → {after_util:.1f}%."
                        ),
                        formula_note=(
                            f"Util {before_util:.1f}% -> {after_util:.1f}% (<= {threshold_pct:.1f}% cap); "
                            f"{eff_equiv:.2f}% direct APR = {_fmt(benefit)}/yr interest avoided"
                        ),
                    )
                )
                remaining -= alloc
                rem_debt[cl.id] -= alloc

        # --- (e) Emergency Buffer Deficit up to Statistical Target ---
        buf_gap = max(0, int(emergency_buffer_gap_cents))
        if remaining > 0 and buf_gap > 0:
            alloc = min(remaining, buf_gap)
            benefit = int(round(alloc * (SAFETY_HYSA_ANNUAL_PCT / 100.0)))
            steps.append(
                OptimalAllocationStep(
                    bucket="emergency_buffer",
                    target_name="Statistical Emergency Buffer",
                    amount_cents=alloc,
                    expected_annual_benefit_cents=benefit,
                    effective_apr_equivalent_pct=SAFETY_HYSA_ANNUAL_PCT,
                    rationale=(
                        f"Add {_fmt(alloc)} toward your volatility-sized emergency buffer "
                        f"to absorb P90 spending spikes without touching credit cards."
                    ),
                    formula_note=(
                        f"{_fmt(alloc)} * {SAFETY_HYSA_ANNUAL_PCT:.2f}% cash reserve yield = "
                        f"{_fmt(benefit)}/yr"
                    ),
                )
            )
            remaining -= alloc

        # --- (f) Tracked Goals Weighted by Priority & Deadline Urgency ---
        active_goals = [
            g for g in (goals or []) if str(g.status).lower() == "active" and g.remaining_cents > 0
        ]
        if remaining > 0 and active_goals:
            goal_meta: List[Tuple[GoalOut, float, int, Optional[int]]] = []
            for g in active_goals:
                prio_weight = float(max(1, 6 - max(1, min(5, int(g.priority)))))
                days_left: Optional[int] = None
                if g.target_date:
                    try:
                        td = date.fromisoformat(g.target_date)
                        days_left = max(1, (td - ref).days)
                        months_left = max(1.0, days_left / 30.4)
                        req_monthly = min(g.remaining_cents, int(math.ceil(g.remaining_cents / months_left)))
                        urgency_mult = 1.6 if days_left <= 90 else (1.3 if days_left <= 180 else 1.0)
                    except ValueError:
                        req_monthly = min(g.remaining_cents, max(10_000, int(round(g.remaining_cents / 6.0))))
                        urgency_mult = 1.0
                else:
                    req_monthly = min(g.remaining_cents, max(10_000, int(round(g.remaining_cents / 6.0))))
                    urgency_mult = 1.0
                goal_meta.append((g, prio_weight * urgency_mult, req_monthly, days_left))

            goal_meta.sort(
                key=lambda item: (
                    -item[1],
                    item[3] if item[3] is not None else 99_999,
                    item[0].remaining_cents,
                )
            )
            weights = [m[1] for m in goal_meta]
            caps = [m[2] for m in goal_meta]
            total_goal_demand = sum(caps)
            goal_pool = min(remaining, total_goal_demand)
            goal_allocs = _allocate_proportional_exact_cents(goal_pool, weights, caps)

            for (g, w, req_m, d_left), alloc in zip(goal_meta, goal_allocs, strict=True):
                if alloc <= 0:
                    continue
                equiv_pct = round(min(8.5, 6.0 + 0.5 * w), 2)
                benefit = int(round(alloc * (equiv_pct / 100.0)))
                deadline_note = f"{d_left}d to target ({g.target_date})" if d_left is not None else "flexible timeline"
                steps.append(
                    OptimalAllocationStep(
                        bucket="tracked_goal",
                        target_name=g.name,
                        amount_cents=alloc,
                        expected_annual_benefit_cents=benefit,
                        effective_apr_equivalent_pct=equiv_pct,
                        rationale=(
                            f"Fund {_fmt(alloc)} toward '{g.name}' (priority P{g.priority}, {deadline_note}) "
                            f"to stay debt-free and on pace."
                        ),
                        formula_note=(
                            f"Weight={w:.2f} (P{g.priority}), monthly pace={_fmt(req_m)}; "
                            f"cash-funding avoids financing drag ({equiv_pct:.2f}% equiv)"
                        ),
                    )
                )
                remaining -= alloc

        # --- (g) Invest Vault / ChronoX Trading Cap (+ residual conservation) ---
        if remaining > 0:
            if safe_to_invest_cap_cents is None:
                inv_alloc = remaining
            else:
                inv_alloc = min(remaining, max(0, int(safe_to_invest_cap_cents)))

            if inv_alloc > 0:
                benefit = int(round(inv_alloc * (INVEST_EXPECTED_ANNUAL_PCT / 100.0)))
                steps.append(
                    OptimalAllocationStep(
                        bucket="invest_vault",
                        target_name="ChronoX Invest & Trading Vault",
                        amount_cents=inv_alloc,
                        expected_annual_benefit_cents=benefit,
                        effective_apr_equivalent_pct=INVEST_EXPECTED_ANNUAL_PCT,
                        rationale=(
                            f"High-APR debt, FICO cliffs, buffer, and active goals are funded — "
                            f"route {_fmt(inv_alloc)} into your ChronoX Invest Vault."
                        ),
                        formula_note=(
                            f"{_fmt(inv_alloc)} * {INVEST_EXPECTED_ANNUAL_PCT:.2f}% diversified benchmark = "
                            f"{_fmt(benefit)}/yr expected compounding"
                        ),
                    )
                )
                remaining -= inv_alloc

        # Conserve any residual pennies if invest vault was capped: low-APR debt first, then HYSA reserve
        if remaining > 0:
            low_apr_lines = [
                cl
                for cl in credit_lines
                if rem_debt.get(cl.id, 0) > 0 and cl.effective_apr_bps <= 1200
            ]
            low_apr_lines.sort(key=lambda c: (-c.effective_apr_bps, rem_debt[c.id]))
            for cl in low_apr_lines:
                if remaining <= 0:
                    break
                alloc = min(remaining, rem_debt[cl.id])
                if alloc <= 0:
                    continue
                apr_pct = round(cl.effective_apr_bps / 100.0, 2)
                benefit = int(round(alloc * (cl.effective_apr_bps / 10000.0)))
                steps.append(
                    OptimalAllocationStep(
                        bucket="low_apr_debt",
                        target_name=cl.name,
                        amount_cents=alloc,
                        expected_annual_benefit_cents=benefit,
                        effective_apr_equivalent_pct=apr_pct,
                        rationale=(
                            f"Invest guardrail cap reached — accelerate {cl.name} by {_fmt(alloc)} "
                            f"for a guaranteed {apr_pct:.2f}% return."
                        ),
                        formula_note=f"{_fmt(alloc)} * {apr_pct:.2f}% APR = {_fmt(benefit)}/yr saved",
                    )
                )
                remaining -= alloc
                rem_debt[cl.id] -= alloc

        if remaining > 0:
            benefit = int(round(remaining * (SAFETY_HYSA_ANNUAL_PCT / 100.0)))
            steps.append(
                OptimalAllocationStep(
                    bucket="safety_reserve",
                    target_name="High-Yield Safety Reserve",
                    amount_cents=remaining,
                    expected_annual_benefit_cents=benefit,
                    effective_apr_equivalent_pct=SAFETY_HYSA_ANNUAL_PCT,
                    rationale=(
                        f"Park the remaining {_fmt(remaining)} in your High-Yield Safety Reserve "
                        f"until your ChronoX Invest Vault unlocks further capacity."
                    ),
                    formula_note=f"{_fmt(remaining)} * {SAFETY_HYSA_ANNUAL_PCT:.2f}% HYSA = {_fmt(benefit)}/yr",
                )
            )
            remaining = 0

        # Assign clean 1-based priority ranks
        for idx, step in enumerate(steps, start=1):
            step.priority_rank = idx

        return steps

    # =========================================================================
    # Full Composite Quant Forecast Report
    # =========================================================================

    def build_forecast_report(
        self,
        *,
        budget: AutoBudgetResult,
        transactions: Sequence[Any],
        recurring_bills: Sequence[Any],
        settings: Dict[str, Any],
        credit_report: Optional[CreditOptimizationReport] = None,
        goals: Optional[Sequence[GoalOut]] = None,
        splits_by_tx_id: Optional[Dict[int, List[Any]]] = None,
        days: int = 30,
        today: Optional[date] = None,
    ) -> QuantForecastReport:
        """
        Compute the full `QuantForecastReport` combining 30/60/90-day cashflow runway,
        trailing 90-day split-aware spending volatility (+ 730d seasonal baseline), and
        optimal marginal-ROI surplus allocation.
        """
        ref = today or date.today()
        horizon = max(7, min(90, int(days)))
        s = budget.safe_to_spend

        runway = self.simulate_cashflow_runway(
            starting_balance_cents=s.total_liquid_cash_cents,
            recurring_bills=recurring_bills,
            transactions=transactions,
            credit_lines=credit_report.credit_lines if credit_report else [],
            azeo_plan=credit_report.azeo_plan if credit_report else [],
            payday_cadence_days=int(settings.get("payday_cadence_days", 14)),
            next_payday_date=s.next_payday_date,
            estimated_paycheck_cents=s.estimated_paycheck_cents,
            emergency_buffer_cents=s.emergency_buffer_cents,
            days=horizon,
            include_azeo_paydowns=True,
            azeo_respect_buffer=True,
            today=ref,
        )

        bills_alloc = sum(j.auto_allocated_cents for j in budget.jars if j.kind == JarKind.BILLS)
        daily_alloc = sum(j.auto_allocated_cents for j in budget.jars if j.kind == JarKind.DAILY)
        fun_alloc = sum(j.auto_allocated_cents for j in budget.jars if j.kind == JarKind.FUN)
        safety_alloc = sum(j.auto_allocated_cents for j in budget.jars if j.kind == JarKind.SAFETY)
        invest_alloc = sum(j.auto_allocated_cents for j in budget.jars if j.kind == JarKind.INVEST)

        volatility = self.compute_spending_volatility(
            transactions=transactions,
            safe_to_spend_cents=s.safe_to_spend_cents,
            days_until_payday=s.days_until_payday,
            jars=budget.jars,
            recurring_bills=recurring_bills,
            current_emergency_buffer_cents=s.emergency_buffer_cents,
            monthly_essential_bills_cents=bills_alloc,
            smoothed_monthly_income_cents=budget.smoothed_monthly_income_cents,
            splits_by_tx_id=splits_by_tx_id,
            today=ref,
        )

        debt_mins = credit_report.total_monthly_minimums_cents if credit_report else 0
        structural_surplus = safety_alloc + invest_alloc + int(round(fun_alloc * 0.25))
        income_headroom = max(0, budget.smoothed_monthly_income_cents - bills_alloc - daily_alloc - debt_mins)
        if budget.smoothed_monthly_income_cents > 0 and income_headroom > 0:
            monthly_surplus_cents = max(0, min(structural_surplus, income_headroom))
        else:
            monthly_surplus_cents = max(0, min(s.safe_to_spend_cents, safety_alloc + invest_alloc))

        buffer_gap = max(0, volatility.recommended_buffer_cents - s.emergency_buffer_cents)
        allocations = self.optimize_surplus_allocation(
            monthly_surplus_cents=monthly_surplus_cents,
            credit_report=credit_report,
            emergency_buffer_gap_cents=buffer_gap,
            goals=goals,
            safe_to_invest_cap_cents=budget.safe_to_invest_cents,
            today=ref,
        )
        total_benefit = sum(step.expected_annual_benefit_cents for step in allocations)

        return QuantForecastReport(
            generated_at=utc_now_iso(),
            horizon_days=runway.horizon_days,
            starting_balance_cents=runway.starting_balance_cents,
            ending_balance_cents=runway.ending_balance_cents,
            lowest_balance_cents=runway.lowest_balance_cents,
            lowest_balance_date=runway.lowest_balance_date,
            overdraft_risk=runway.overdraft_risk,
            safe_runway_days=runway.safe_runway_days,
            payday_crunch_warning=runway.payday_crunch_warning,
            daily_points=runway.daily_points,
            volatility=volatility,
            monthly_surplus_cents=monthly_surplus_cents,
            optimal_allocations=allocations,
            total_expected_annual_benefit_cents=total_benefit,
        )

    # =========================================================================
    # 4. Pure-Math Deterministic Coach & Q&A Engine
    # =========================================================================

    def _coerce_forecast(
        self,
        *,
        quant_forecast: Optional[Union[QuantForecastReport, Dict[str, Any]]],
        budget: AutoBudgetResult,
        credit_report: Optional[CreditOptimizationReport],
        goals: Optional[Sequence[GoalOut]],
        today: Optional[date] = None,
    ) -> QuantForecastReport:
        if isinstance(quant_forecast, QuantForecastReport):
            return quant_forecast
        if isinstance(quant_forecast, dict):
            return QuantForecastReport.model_validate(quant_forecast)
        return self.build_forecast_report(
            budget=budget,
            transactions=[],
            recurring_bills=budget.upcoming_bills,
            settings={"emergency_buffer_cents": budget.safe_to_spend.emergency_buffer_cents},
            credit_report=credit_report,
            goals=goals,
            days=30,
            today=today,
        )

    def build_quant_suggestions(
        self,
        *,
        budget: AutoBudgetResult,
        quant_forecast: Optional[Union[QuantForecastReport, Dict[str, Any]]] = None,
        credit_report: Optional[CreditOptimizationReport] = None,
        impulses: Optional[Sequence[ImpulseOut]] = None,
        goals: Optional[Sequence[GoalOut]] = None,
        adhd_stats: Optional[ADHDStats] = None,
        needs_triage_count: int = 0,
        reset_done_this_week: bool = True,
        today: Optional[date] = None,
    ) -> List[ValidatedSuggestion]:
        """
        Produce up to 4 deeply mathematical, RSD-safe, actionable `ValidatedSuggestion`
        items with zero LLM calls (used in `deterministic_only` mode and offline fallback).
        """
        qf = self._coerce_forecast(
            quant_forecast=quant_forecast,
            budget=budget,
            credit_report=credit_report,
            goals=goals,
            today=today,
        )
        s = budget.safe_to_spend
        vol = qf.volatility
        suggestions: List[ValidatedSuggestion] = []
        used_keys: set[str] = set()

        def _add(item: ValidatedSuggestion, key: str) -> None:
            if len(suggestions) < 4 and key not in used_keys:
                used_keys.add(key)
                suggestions.append(item)

        # 1. Runway Overdraft / Buffer Dip Shield (highest urgency if cashflow dips)
        if qf.overdraft_risk or qf.payday_crunch_warning:
            rebal = budget.rebalance_plans[0] if budget.rebalance_plans else None
            if qf.overdraft_risk:
                title = f"Protect Cash Runway Before {qf.lowest_balance_date}"
                rationale = (
                    f"Projected checking dips to {_fmt(qf.lowest_balance_cents)} on {qf.lowest_balance_date} "
                    f"({qf.safe_runway_days} safe runway days). Keeping flexible spend light until "
                    f"{s.next_payday_date} prevents overdraft fees completely."
                )
                prio: str = "high"
                impact = abs(qf.lowest_balance_cents)
            else:
                title = f"Cash Nadir Watch: {qf.lowest_balance_date}"
                rationale = (
                    f"Runway stays positive, dipping to {_fmt(qf.lowest_balance_cents)} on {qf.lowest_balance_date} "
                    f"before climbing back to {_fmt(qf.ending_balance_cents)}. P90 conservative daily cap is "
                    f"{_fmt(vol.p90_conservative_daily_safe_cents)}/day."
                )
                prio = "medium"
                impact = max(0, s.emergency_buffer_cents - qf.lowest_balance_cents)

            if rebal:
                _add(
                    ValidatedSuggestion(
                        id="quant-runway-shield",
                        title=title,
                        rationale=rationale,
                        micro_step=(
                            f"Shift {_fmt(rebal.amount_cents)} from {rebal.from_jar_name} to {rebal.to_jar_name} "
                            f"and pace daily spend near {_fmt(vol.p90_conservative_daily_safe_cents)}/day."
                        ),
                        priority=prio,  # type: ignore[arg-type]
                        action_type=SuggestionActionType.REBALANCE_JARS,
                        action_payload={
                            "from_jar_id": rebal.from_jar_id,
                            "to_jar_id": rebal.to_jar_id,
                            "amount_cents": rebal.amount_cents,
                            "reason": "Quant cashflow nadir protection",
                        },
                        impact_cents=rebal.amount_cents,
                        verified=True,
                        verification_notes=[
                            f"30d runway nadir {_fmt(qf.lowest_balance_cents)} on {qf.lowest_balance_date}",
                            "Verified jar surplus without touching Bills shield",
                        ],
                        source="deterministic_quant",
                    ),
                    "runway",
                )
            else:
                _add(
                    ValidatedSuggestion(
                        id="quant-runway-shield",
                        title=title,
                        rationale=rationale,
                        micro_step=(
                            f"Follow the P90 conservative cap of {_fmt(vol.p90_conservative_daily_safe_cents)}/day "
                            f"until payday on {s.next_payday_date}."
                        ),
                        priority=prio,  # type: ignore[arg-type]
                        action_type=SuggestionActionType.GENERAL_NUDGE,
                        action_payload={"open_tab": "today", "nadir_date": qf.lowest_balance_date},
                        impact_cents=impact,
                        verified=True,
                        verification_notes=[
                            f"Day-by-day runway projection across {qf.horizon_days} days",
                            f"Lowest balance {_fmt(qf.lowest_balance_cents)} on {qf.lowest_balance_date}",
                        ],
                        source="deterministic_quant",
                    ),
                    "runway",
                )

        # 2. Autopay Late-Fee Shield (infinite FICO risk priority)
        if credit_report:
            for cl in credit_report.credit_lines:
                if cl.autopay_mode == AutopayMode.NONE and (cl.balance_cents > 0 or cl.kind == DebtKind.CREDIT_CARD):
                    _add(
                        ValidatedSuggestion(
                            id=f"quant-autopay-{cl.id}",
                            title=f"Enable Autopay Shield on {cl.name}",
                            rationale=(
                                f"Autopay is currently OFF on {cl.name} (min {_fmt(cl.effective_min_payment_cents)} "
                                f"due {cl.next_due_date}). Automating the minimum eliminates $35+ late fees and "
                                f"protects 35% of your FICO score from ADHD forgetfulness."
                            ),
                            micro_step=f"Tap Apply to enable Autopay Minimum on {cl.name} in 1 click.",
                            priority="high",
                            action_type=SuggestionActionType.ENABLE_AUTOPAY,
                            action_payload={"credit_line_id": cl.id, "autopay_mode": "minimum"},
                            impact_cents=LATE_FEE_SHIELD_CENTS,
                            verified=True,
                            verification_notes=["Risk-adjusted priority = ∞ (payment history shield)"],
                            source="deterministic_quant",
                        ),
                        f"autopay-{cl.id}",
                    )
                    break

        # 3. Top Marginal-ROI Surplus Allocation Move (Promo Cliff / High-APR / AZEO / Goal)
        lines_by_name = {cl.name: cl for cl in (credit_report.credit_lines if credit_report else [])}
        goals_by_name = {g.name: g for g in (goals or [])}
        for step in qf.optimal_allocations:
            if step.bucket == "autopay_protection":
                continue
            if step.bucket in ("promo_apr_cliff", "high_apr_debt", "azeo_fico_cliff"):
                matched_line = next(
                    (cl for name, cl in lines_by_name.items() if step.target_name.startswith(name)),
                    None,
                )
                payload = (
                    {"credit_line_id": matched_line.id, "amount_cents": step.amount_cents}
                    if matched_line
                    else {"open_tab": "credit"}
                )
                act_type = (
                    SuggestionActionType.CREDIT_PAYDOWN if matched_line else SuggestionActionType.GENERAL_NUDGE
                )
                _add(
                    ValidatedSuggestion(
                        id=f"quant-roi-{step.priority_rank}-{step.bucket}",
                        title=f"Optimal ROI ({step.effective_apr_equivalent_pct:.1f}%): {step.target_name}",
                        rationale=(
                            f"Routing {_fmt(step.amount_cents)}/mo here delivers an estimated "
                            f"{_fmt(step.expected_annual_benefit_cents)}/yr benefit ({step.formula_note})."
                        ),
                        micro_step=f"Direct {_fmt(step.amount_cents)} from your {_fmt(qf.monthly_surplus_cents)} monthly surplus to {step.target_name}.",
                        priority="high",
                        action_type=act_type,
                        action_payload=payload,
                        impact_cents=step.expected_annual_benefit_cents,
                        verified=True,
                        verification_notes=[
                            f"Marginal-ROI rank #{step.priority_rank} ({step.effective_apr_equivalent_pct:.2f}% equiv)",
                            step.formula_note,
                        ],
                        source="deterministic_quant",
                    ),
                    f"roi-{step.bucket}-{matched_line.id if matched_line else step.priority_rank}",
                )
                break
            if step.bucket == "tracked_goal":
                matched_goal = goals_by_name.get(step.target_name)
                if matched_goal:
                    _add(
                        ValidatedSuggestion(
                            id=f"quant-goal-{matched_goal.id}",
                            title=f"Optimal Goal Pace: {matched_goal.name}",
                            rationale=(
                                f"Debt & buffer priorities are covered — putting {_fmt(step.amount_cents)} toward "
                                f"'{matched_goal.name}' ({matched_goal.pct_funded:.0f}% funded) keeps it debt-free."
                            ),
                            micro_step=f"Tap Apply to contribute {_fmt(step.amount_cents)} toward {matched_goal.name}.",
                            priority="medium",
                            action_type=SuggestionActionType.FUND_GOAL,
                            action_payload={"goal_id": matched_goal.id, "amount_cents": step.amount_cents},
                            impact_cents=step.amount_cents,
                            verified=True,
                            verification_notes=[step.formula_note],
                            source="deterministic_quant",
                        ),
                        f"goal-{matched_goal.id}",
                    )
                    break

        # 4. AZEO Pre-Statement-Close Score Turbo (exact pay-by date 3d before close)
        if credit_report and credit_report.azeo_plan:
            top_azeo: Optional[AZEOAction] = next(
                (a for a in credit_report.azeo_plan if a.pay_before_close_cents > 0),
                None,
            )
            if top_azeo:
                _add(
                    ValidatedSuggestion(
                        id=f"quant-azeo-{top_azeo.credit_line_id}",
                        title=f"AZEO Score Boost: {top_azeo.card_name} by {top_azeo.pay_by_date}",
                        rationale=(
                            f"Paying {_fmt(top_azeo.pay_before_close_cents)} 3 days before statement close "
                            f"({top_azeo.statement_close_date}) drops {top_azeo.card_name}'s reported utilization "
                            f"to {top_azeo.resulting_util_pct:.1f}% (AZEO optimal tier)."
                        ),
                        micro_step=(
                            f"Schedule {_fmt(top_azeo.pay_before_close_cents)} on {top_azeo.card_name} by {top_azeo.pay_by_date}."
                        ),
                        priority="high" if (credit_report.aggregate_utilization_pct >= 29.0) else "medium",
                        action_type=SuggestionActionType.CREDIT_PAYDOWN,
                        action_payload={
                            "credit_line_id": top_azeo.credit_line_id,
                            "amount_cents": top_azeo.pay_before_close_cents,
                        },
                        impact_cents=top_azeo.pay_before_close_cents,
                        verified=True,
                        verification_notes=[
                            f"Statement close {top_azeo.statement_close_date} -> pay-by {top_azeo.pay_by_date}",
                            f"Resulting utilization: {top_azeo.resulting_util_pct:.1f}%",
                        ],
                        source="deterministic_quant",
                    ),
                    f"azeo-{top_azeo.credit_line_id}",
                )

        # 5. Zero-Guilt Rebalance if a jar dipped negative
        if budget.rebalance_plans:
            plan = budget.rebalance_plans[0]
            _add(
                ValidatedSuggestion(
                    id=f"quant-rebal-{plan.from_jar_id}-{plan.to_jar_id}",
                    title=f"1-Click Cushion for {plan.to_jar_name}",
                    rationale=(
                        f"{plan.to_jar_name} ran tight this cycle, and {plan.from_jar_name} has exact surplus "
                        f"to cover {_fmt(plan.amount_cents)} while keeping your Bills shield 100% locked."
                    ),
                    micro_step=f"Tap Apply to shift {_fmt(plan.amount_cents)} from {plan.from_jar_name} to {plan.to_jar_name}.",
                    priority="high",
                    action_type=SuggestionActionType.REBALANCE_JARS,
                    action_payload={
                        "from_jar_id": plan.from_jar_id,
                        "to_jar_id": plan.to_jar_id,
                        "amount_cents": plan.amount_cents,
                        "reason": plan.calm_message,
                    },
                    impact_cents=plan.amount_cents,
                    verified=True,
                    verification_notes=["Exact surplus verified; protected Bills jar untouched"],
                    source="deterministic_quant",
                ),
                "rebalance",
            )

        # 6. Statistical Volatility Buffer Sizing & P90 Safe-to-Spend Cushion
        buffer_gap = vol.recommended_buffer_cents - s.emergency_buffer_cents
        if buffer_gap >= 5_000:
            _add(
                ValidatedSuggestion(
                    id="quant-vol-buffer",
                    title=f"Resize Buffer to {_fmt(vol.recommended_buffer_cents)} (P90 Fit)",
                    rationale=(
                        f"Your spending stability is {vol.stability_score_0_100}/100 (P50 {_fmt(vol.p50_weekly_spend_cents)}/wk, "
                        f"P90 {_fmt(vol.p90_weekly_spend_cents)}/wk). Raising your emergency shield from "
                        f"{_fmt(s.emergency_buffer_cents)} → {_fmt(vol.recommended_buffer_cents)} absorbs spiky weeks smoothly."
                    ),
                    micro_step=(
                        f"Tap Apply to update your emergency buffer target to {_fmt(vol.recommended_buffer_cents)} "
                        f"(P90 daily safe: {_fmt(vol.p90_conservative_daily_safe_cents)}/day)."
                    ),
                    priority="medium",
                    action_type=SuggestionActionType.BOOST_EMERGENCY_BUFFER,
                    action_payload={"emergency_buffer_cents": vol.recommended_buffer_cents},
                    impact_cents=buffer_gap,
                    verified=True,
                    verification_notes=[
                        f"Trailing {vol.sample_days}d CV={vol.coefficient_of_variation:.2f}, P90={_fmt(vol.p90_weekly_spend_cents)}/wk",
                        f"Confidence band: P50 {_fmt(vol.p50_daily_safe_cents)}/day vs P90 {_fmt(vol.p90_conservative_daily_safe_cents)}/day",
                    ],
                    source="deterministic_quant",
                ),
                "vol-buffer",
            )

        # 7. Silent Subscription Price Hike (exact annualized math)
        for bill in budget.upcoming_bills:
            if bill.price_hike_cents > 0:
                annual_hike = bill.price_hike_cents * 12
                _add(
                    ValidatedSuggestion(
                        id=f"quant-hike-{bill.id}",
                        title=f"Price Hike Caught: {bill.name} (+{_fmt(annual_hike)}/yr)",
                        rationale=(
                            f"{bill.name} rose by +{_fmt(bill.price_hike_cents)}/cycle to {_fmt(bill.amount_cents)} "
                            f"(+{_fmt(annual_hike)}/yr). Auto-budget already reserved it so cashflow stays protected."
                        ),
                        micro_step=f"Flag {bill.name} to review or downgrade in your 5-minute Weekly Reset.",
                        priority="medium",
                        action_type=SuggestionActionType.REVIEW_SUBSCRIPTION,
                        action_payload={"bill_id": bill.id, "bill_name": bill.name},
                        impact_cents=annual_hike,
                        verified=True,
                        verification_notes=[f"Exact recurring delta +{_fmt(bill.price_hike_cents)}/cycle"],
                        source="deterministic_quant",
                    ),
                    f"hike-{bill.id}",
                )
                break

        # 8. Matured Impulse Win
        ready_imps = [i for i in (impulses or []) if i.status == ImpulseStatus.READY]
        if ready_imps:
            imp = ready_imps[0]
            _add(
                ValidatedSuggestion(
                    id=f"quant-imp-{imp.id}",
                    title=f"Cooldown Ready: {imp.item_name} ({_fmt(imp.price_cents)})",
                    rationale=(
                        f"Your {imp.cooling_hours}h cooldown finished ({imp.work_hours_cost:.1f}h of work). "
                        f"Skipping it adds {_fmt(imp.price_cents)} directly to your cash runway."
                    ),
                    micro_step="Open Impulse Vault and tap 'Skipped It' if you no longer want it.",
                    priority="medium",
                    action_type=SuggestionActionType.GENERAL_NUDGE,
                    action_payload={"open_tab": "impulse", "impulse_id": imp.id},
                    impact_cents=imp.price_cents,
                    verified=True,
                    verification_notes=["Cooldown elapsed; exact cash runway boost verified"],
                    source="deterministic_quant",
                ),
                f"imp-{imp.id}",
            )

        # 9. Always include at least one calm Quantitative Summary / Win if slots remain
        if len(suggestions) < 2:
            streak_note = (
                f" ({adhd_stats.weekly_reset_streak}-week reset streak active!)"
                if adhd_stats and adhd_stats.weekly_reset_streak > 0
                else ""
            )
            _add(
                ValidatedSuggestion(
                    id="quant-calm-summary",
                    title=f"{qf.horizon_days}-Day Runway Solid: {_fmt(qf.ending_balance_cents)} Projected",
                    rationale=(
                        f"Your cash runway stays positive all {qf.safe_runway_days} days (nadir {_fmt(qf.lowest_balance_cents)} "
                        f"on {qf.lowest_balance_date}), stability score {vol.stability_score_0_100}/100, and daily safe band "
                        f"{_fmt(vol.p90_conservative_daily_safe_cents)}–{_fmt(vol.p50_daily_safe_cents)}/day{streak_note}."
                    ),
                    micro_step=(
                        f"Deploy {_fmt(qf.monthly_surplus_cents)}/mo along your optimal allocation waterfall "
                        f"for +{_fmt(qf.total_expected_annual_benefit_cents)}/yr expected value."
                        if qf.monthly_surplus_cents > 0
                        else "Enjoy today guilt-free — all upcoming bills and shields are mathematically covered."
                    ),
                    priority="low",
                    action_type=SuggestionActionType.WEEKLY_RESET
                    if (not reset_done_this_week and needs_triage_count > 0)
                    else SuggestionActionType.GENERAL_NUDGE,
                    action_payload={"open_tab": "reset"}
                    if (not reset_done_this_week and needs_triage_count > 0)
                    else {"open_tab": "today"},
                    impact_cents=qf.total_expected_annual_benefit_cents or vol.p50_daily_safe_cents,
                    verified=True,
                    verification_notes=[
                        f"{qf.horizon_days}-day runway verified (0 overdraft days)",
                        f"Stability score {vol.stability_score_0_100}/100",
                    ],
                    source="deterministic_quant",
                ),
                "calm-summary",
            )

        return suggestions[:4]

    def _extract_dollar_cents(self, question: str) -> Optional[int]:
        """Extract an explicit `$amount` from text in exact integer cents."""
        match = _EXPLICIT_DOLLAR_SIGN_RE.search(question)
        if not match:
            return None
        try:
            cents = int(round(float(match.group(1).replace(",", "")) * 100))
        except ValueError:
            return None
        return cents if cents > 0 else None

    def _extract_purchase_cost_cents(self, question: str) -> Optional[int]:
        """Extract purchase dollar amount only when question asks about buying/affording an item."""
        explicit = _EXPLICIT_DOLLAR_SIGN_RE.search(question)
        has_afford_intent = bool(_AFFORD_INTENT_RE.search(question))
        # Do not hijack explicit surplus/debt allocation questions like "allocate $500 extra to debt"
        if not has_afford_intent:
            if not explicit:
                return None
            if _DEBT_INTENT_RE.search(question) or _BUFFER_INTENT_RE.search(question) or _SUBS_INTENT_RE.search(question):
                return None
        match = explicit or _QUESTION_DOLLAR_RE.search(question)
        if not match:
            return None
        try:
            cents = int(round(float(match.group(1).replace(",", "")) * 100))
        except ValueError:
            return None
        return cents if cents > 0 else None

    def answer_quant_question(
        self,
        *,
        question: str,
        budget: AutoBudgetResult,
        quant_forecast: Optional[Union[QuantForecastReport, Dict[str, Any]]] = None,
        credit_report: Optional[CreditOptimizationReport] = None,
        impulses: Optional[Sequence[ImpulseOut]] = None,
        goals: Optional[Sequence[GoalOut]] = None,
        hourly_wage_cents: int = 3_000,
        payday_cadence_days: int = 14,
        today: Optional[date] = None,
    ) -> CoachAnswerResponse:
        """
        Answer any free-form personal finance question using 100% pure deterministic math.

        Handles:
        - Affordability & big-purchase planning (e.g. "$1000 gadget" + 3 credit-safe paths + runway nadir check)
        - Credit score, FICO utilization tiers & AZEO pre-close schedule
        - Debt payoff order, 4-strategy comparison, promo cliffs & marginal-ROI allocation
        - 30/60/90-day cashflow runway, overdraft check & payday crunch timing
        - Subscriptions, recurring bills & silent price hikes
        - Emergency buffer sizing, spending volatility & P50/P90 daily safe band
        - Active goals & ChronoX Safe-to-Invest capital guardrail
        - General financial health executive summary
        """
        ref = today or date.today()
        safe_q = sanitize_untrusted_text(question, max_len=400)
        qf = self._coerce_forecast(
            quant_forecast=quant_forecast,
            budget=budget,
            credit_report=credit_report,
            goals=goals,
            today=ref,
        )
        s = budget.safe_to_spend
        vol = qf.volatility

        verified_numbers: Dict[str, Any] = {
            "safe_to_spend_dollars": round(s.safe_to_spend_cents / 100.0, 2),
            "daily_safe_to_spend_dollars": round(s.daily_safe_to_spend_cents / 100.0, 2),
            "p50_daily_safe_dollars": round(vol.p50_daily_safe_cents / 100.0, 2),
            "p90_conservative_daily_safe_dollars": round(vol.p90_conservative_daily_safe_cents / 100.0, 2),
            "days_until_payday": s.days_until_payday,
            "unpaid_bills_until_payday_dollars": round(s.unpaid_bills_until_payday_cents / 100.0, 2),
            "safe_to_invest_chronox_dollars": round(budget.safe_to_invest_cents / 100.0, 2),
            "runway_horizon_days": qf.horizon_days,
            "runway_lowest_balance_dollars": round(qf.lowest_balance_cents / 100.0, 2),
            "runway_lowest_balance_date": qf.lowest_balance_date,
            "runway_ending_balance_dollars": round(qf.ending_balance_cents / 100.0, 2),
            "overdraft_risk": qf.overdraft_risk,
            "safe_runway_days": qf.safe_runway_days,
            "stability_score_0_100": vol.stability_score_0_100,
            "recommended_buffer_dollars": round(vol.recommended_buffer_cents / 100.0, 2),
            "monthly_surplus_dollars": round(qf.monthly_surplus_cents / 100.0, 2),
            "total_expected_annual_benefit_dollars": round(qf.total_expected_annual_benefit_cents / 100.0, 2),
        }
        if credit_report:
            verified_numbers["aggregate_utilization_pct"] = credit_report.aggregate_utilization_pct
            verified_numbers["total_debt_dollars"] = round(credit_report.total_debt_cents / 100.0, 2)
            verified_numbers["monthly_interest_burn_dollars"] = round(
                credit_report.monthly_interest_burn_cents / 100.0, 2
            )
            verified_numbers["dti_pct"] = credit_report.dti_pct

        affordability_check: Optional[Dict[str, Any]] = None
        cost_cents = self._extract_purchase_cost_cents(safe_q)

        # --- Intent 1: Affordability / Purchase Plan (e.g. "$1000 gadget") ---
        if cost_cents is not None:
            after_safe = s.safe_to_spend_cents - cost_cents
            new_daily = max(0, after_safe) // max(1, s.days_until_payday)
            work_hours = round(cost_cents / max(100, int(hourly_wage_cents)), 1)
            fun_jar = next((j for j in budget.jars if j.kind == JarKind.FUN), None)
            fun_avail = fun_jar.available_cents if fun_jar else 0
            nadir_after = qf.lowest_balance_cents - cost_cents
            runway_safe = nadir_after >= s.emergency_buffer_cents

            if cost_cents <= s.safe_to_spend_cents and runway_safe and cost_cents <= max(fun_avail, s.daily_safe_to_spend_cents * 3):
                verdict = "safe_yes"
            elif cost_cents <= s.safe_to_spend_cents and nadir_after >= 0:
                verdict = "tight_yes_cooldown_recommended"
            else:
                verdict = "park_in_impulse_vault"

            affordability_check = {
                "purchase_cost_dollars": round(cost_cents / 100.0, 2),
                "current_safe_to_spend_dollars": round(s.safe_to_spend_cents / 100.0, 2),
                "safe_to_spend_after_dollars": round(after_safe / 100.0, 2),
                "current_daily_allowance_dollars": round(s.daily_safe_to_spend_cents / 100.0, 2),
                "daily_allowance_after_dollars": round(new_daily / 100.0, 2),
                "guilt_free_fun_available_dollars": round(fun_avail / 100.0, 2),
                "work_hours_cost": work_hours,
                "runway_nadir_after_dollars": round(nadir_after / 100.0, 2),
                "runway_safe": runway_safe,
                "verdict": verdict,
            }

            lines: List[str] = [
                f"**Affordability & Runway Math ({_fmt(cost_cents)} · {work_hours:.1f}h of work):**",
                (
                    f"- **Immediate Safe-to-Spend:** {_fmt(s.safe_to_spend_cents)} → {_fmt(after_safe)} after purchase "
                    f"(daily allowance {_fmt(s.daily_safe_to_spend_cents)} → {_fmt(new_daily)}/day for {s.days_until_payday}d; "
                    f"P90 conservative band is {_fmt(vol.p90_conservative_daily_safe_cents)}/day)."
                ),
                (
                    f"- **{qf.horizon_days}-Day Runway Impact:** Lowest cash nadir shifts from {_fmt(qf.lowest_balance_cents)} "
                    f"→ {_fmt(nadir_after)} on {qf.lowest_balance_date} "
                    f"({'stays above your ' + _fmt(s.emergency_buffer_cents) + ' emergency shield' if runway_safe else 'dips below your ' + _fmt(s.emergency_buffer_cents) + ' shield — cash sinking fund recommended'})."
                ),
            ]

            if credit_report is not None and cost_cents >= 20_000:
                plan = GoalOptimizerEngine().optimize(
                    req=GoalPlanRequest(item_name="Requested Purchase", target_cents=cost_cents),
                    budget=budget,
                    credit_report=credit_report,
                    impulses=list(impulses or []),
                    hourly_wage_cents=max(100, int(hourly_wage_cents)),
                    payday_cadence_days=max(7, int(payday_cadence_days)),
                    today=ref,
                )
                rec = next((o for o in plan.options if o.recommended), plan.options[0])
                credit_opt = next((o for o in plan.options if o.path_id == "credit_optimized_buy"), None)
                lines.append(f"- **Recommended Path ({rec.title}):** {rec.summary}")
                if credit_opt:
                    lines.append(f"- **Credit/AZEO Rule:** {credit_opt.credit_impact_note}")
                if plan.high_apr_debt_warning:
                    lines.append(f"- **Opportunity Cost:** {plan.high_apr_debt_warning}")
                if plan.accelerators:
                    top_acc = ", ".join(a["label"] for a in plan.accelerators[:2])
                    lines.append(f"- **Fast Accelerators:** {top_acc}")
            else:
                if verdict == "safe_yes":
                    lines.append(
                        "- **Verdict (Green Light):** Fits cleanly inside Safe-to-Spend and your 30-day runway — enjoy guilt-free!"
                    )
                elif verdict == "tight_yes_cooldown_recommended":
                    lines.append(
                        "- **Verdict (Tight Fit):** Fits inside Safe-to-Spend, though it narrows your daily cushion. "
                        "Try parking it in the 48h Impulse Vault first to confirm it's a true want."
                    )
                else:
                    lines.append(
                        f"- **Verdict (Hold Until Payday):** Exceeds current Safe-to-Spend ({_fmt(s.safe_to_spend_cents)}) "
                        f"with {_fmt(s.unpaid_bills_until_payday_cents)} shielded for upcoming bills. Park it in the "
                        f"48h Impulse Vault and revisit after payday on {s.next_payday_date}."
                    )

            return self._qa_response("\n".join(lines), verified_numbers, affordability_check)

        # --- Intent 2: Credit Score / AZEO / Utilization ---
        if _CREDIT_INTENT_RE.search(safe_q):
            if not credit_report or not credit_report.credit_lines:
                ans = (
                    "**Credit & AZEO Snapshot:** No credit cards or loans are tracked yet. "
                    "Add your cards (with limits, APRs, and statement closing days) in the Credit tab to unlock "
                    "exact pre-statement-close AZEO paydown dates and FICO utilization optimization."
                )
                return self._qa_response(ans, verified_numbers, None)

            lines = [
                f"**FICO & AZEO Credit Math ({credit_report.aggregate_utilization_pct:.1f}% Aggregate Util · Tier: {credit_report.aggregate_tier}):**",
                (
                    f"- **Revolving Totals:** {_fmt(credit_report.total_revolving_balance_cents)} used of "
                    f"{_fmt(credit_report.total_revolving_limit_cents)} limit across {credit_report.cards_with_balance_count}/"
                    f"{credit_report.total_cards_count} cards with balances · DTI {credit_report.dti_pct:.1f}% ({credit_report.dti_tier})."
                ),
            ]
            if credit_report.all_zero_penalty_risk:
                lines.append(
                    "- **All-Zero Penalty Active:** All cards are reporting $0. Let 1 primary card report 1%–4.9% then autopay in full."
                )
            drag_cards = [
                c
                for c in credit_report.credit_lines
                if c.kind == DebtKind.CREDIT_CARD and (c.utilization_pct or 0.0) >= 29.0
            ]
            for c in drag_cards[:3]:
                cap_289 = int(math.floor(c.credit_limit_cents * 0.289))
                need_clear = max(0, c.balance_cents - cap_289)
                lines.append(
                    f"- **Per-Card Cliff ({c.name}):** {c.utilization_pct:.1f}% util — paying {_fmt(need_clear)} "
                    f"before {c.next_statement_close_date} clears the 29% FICO drag threshold."
                )
            azeo_moves = [a for a in credit_report.azeo_plan if a.pay_before_close_cents > 0]
            if azeo_moves:
                for a in azeo_moves[:3]:
                    lines.append(
                        f"- **AZEO Pre-Close Move ({a.card_name}):** Pay {_fmt(a.pay_before_close_cents)} by "
                        f"{a.pay_by_date} (3d before close {a.statement_close_date}) → reports {a.resulting_util_pct:.1f}%."
                    )
            else:
                lines.append("- **AZEO Status:** Cards are already aligned with AZEO reporting targets.")
            if credit_report.autopay_missing_count > 0:
                lines.append(
                    f"- **Autopay Shield:** {credit_report.autopay_missing_count} line(s) have autopay OFF — enabling at least minimum autopay protects 35% of FICO."
                )
            if credit_report.oldest_account_name:
                lines.append(
                    f"- **History Anchor:** Keep **{credit_report.oldest_account_name}** open (avg age {credit_report.avg_account_age_months or 0:.0f} mo) to protect AAoA."
                )
            return self._qa_response("\n".join(lines), verified_numbers, None)

        # --- Intent 3: Debt Payoff Order / Strategy / Marginal-ROI Allocation ---
        if _DEBT_INTENT_RE.search(safe_q):
            lines = []
            custom_surplus_cents = self._extract_dollar_cents(safe_q)
            if custom_surplus_cents:
                buffer_gap = max(0, vol.recommended_buffer_cents - s.emergency_buffer_cents)
                active_steps = self.optimize_surplus_allocation(
                    monthly_surplus_cents=custom_surplus_cents,
                    credit_report=credit_report,
                    emergency_buffer_gap_cents=buffer_gap,
                    goals=goals,
                    safe_to_invest_cap_cents=budget.safe_to_invest_cents,
                    today=ref,
                )
                surplus_label_cents = custom_surplus_cents
                surplus_benefit_cents = sum(st.expected_annual_benefit_cents for st in active_steps)
            else:
                active_steps = qf.optimal_allocations
                surplus_label_cents = qf.monthly_surplus_cents
                surplus_benefit_cents = qf.total_expected_annual_benefit_cents

            if credit_report and credit_report.total_debt_cents > 0:
                annual_burn = credit_report.monthly_interest_burn_cents * 12
                lines.append(
                    f"**Debt Payoff & Marginal-ROI Order ({_fmt(credit_report.total_debt_cents)} Total Debt · {_fmt(credit_report.monthly_interest_burn_cents)}/mo Interest Burn = {_fmt(annual_burn)}/yr):**"
                )
                for st in credit_report.strategies:
                    badge = " ⭐ Recommended" if st.recommended_for_adhd else ""
                    lines.append(
                        f"- **{st.label}{badge}:** Debt-free in {st.months_to_debt_free} mo ({st.debt_free_date}) · "
                        f"Total interest {_fmt(st.total_interest_cents)} (saves {_fmt(st.interest_saved_vs_minimums_cents)} vs minimums) · "
                        f"First target: {st.first_target_name}."
                    )
            else:
                lines.append("**Debt & Surplus Allocation Math:** Zero active debt balances — 100% of surplus compounds into buffer, goals & investing!")

            if active_steps:
                lines.append(
                    f"- **Optimal Marginal-ROI Waterfall ({_fmt(surplus_label_cents)} surplus → +{_fmt(surplus_benefit_cents)}/yr value):**"
                )
                for step in active_steps[:5]:
                    lines.append(
                        f"  {step.priority_rank}. **{step.target_name}:** {_fmt(step.amount_cents)} "
                        f"({step.effective_apr_equivalent_pct:.1f}% equiv · {step.formula_note})"
                    )
            return self._qa_response("\n".join(lines), verified_numbers, None)

        # --- Intent 4: Cashflow Runway / Overdraft / Payday ---
        if _CASHFLOW_INTENT_RE.search(safe_q):
            net = qf.ending_balance_cents - qf.starting_balance_cents
            status_str = "Overdraft Risk Detected" if qf.overdraft_risk else "Positive Across Full Horizon"
            lines = [
                f"**{qf.horizon_days}-Day Cashflow Runway ({status_str}):**",
                (
                    f"- **Trajectory:** Starting {_fmt(qf.starting_balance_cents)} → Ending {_fmt(qf.ending_balance_cents)} "
                    f"(net {'+' if net >= 0 else ''}{_fmt(net)} over {qf.horizon_days} days)."
                ),
                (
                    f"- **Lowest Balance Nadir:** {_fmt(qf.lowest_balance_cents)} on **{qf.lowest_balance_date}** · "
                    f"Safe runway: **{qf.safe_runway_days}/{qf.horizon_days} days**."
                ),
                (
                    f"- **Next Payday:** {s.next_payday_date} ({s.days_until_payday}d away · est. {_fmt(s.estimated_paycheck_cents)}) "
                    f"with {_fmt(s.unpaid_bills_until_payday_cents)} in bills already shielded."
                ),
            ]
            if qf.payday_crunch_warning:
                lines.append(f"- **Heads-Up:** {qf.payday_crunch_warning}")
            key_days = [p for p in qf.daily_points if p.events][:5]
            if key_days:
                lines.append("- **Next Key Cashflow Events:**")
                for p in key_days:
                    lines.append(f"  · {p.date}: {', '.join(p.events[:2])} → closing {_fmt(p.balance_cents)}")
            return self._qa_response("\n".join(lines), verified_numbers, None)

        # --- Intent 5: Subscriptions / Recurring Bills / Price Hikes ---
        if _SUBS_INTENT_RE.search(safe_q):
            bills = budget.upcoming_bills
            if not bills:
                ans = (
                    "**Recurring Bills & Subscriptions:** No active recurring bills detected yet. "
                    "Import a bank CSV/OFX statement or add bills in the Bills tab to track all 8 cadences and price hikes."
                )
                return self._qa_response(ans, verified_numbers, None)

            monthly_total = sum(_bill_monthly_equiv_cents(b.amount_cents, b.cadence.value) for b in bills)
            non_ess = [b for b in bills if b.is_subscription and not b.is_essential]
            non_ess_mo = sum(_bill_monthly_equiv_cents(b.amount_cents, b.cadence.value) for b in non_ess)
            hikes = [b for b in bills if b.price_hike_cents > 0]
            zombies = [b for b in bills if b.flagged_zombie]

            lines = [
                f"**Recurring Bills & Subscription Audit ({len(bills)} Active · {_fmt(monthly_total)}/mo = {_fmt(monthly_total * 12)}/yr):**",
                f"- **Due Before Next Payday ({s.next_payday_date}):** {_fmt(s.unpaid_bills_until_payday_cents)} already reserved in Safe-to-Spend.",
                (
                    f"- **Non-Essential Subscriptions:** {len(non_ess)} active totaling {_fmt(non_ess_mo)}/mo "
                    f"({_fmt(non_ess_mo * 12)}/yr)."
                ),
            ]
            for h in hikes[:3]:
                lines.append(
                    f"- **Price Hike Spotted:** {h.name} rose +{_fmt(h.price_hike_cents)}/cycle to {_fmt(h.amount_cents)} "
                    f"(+{_fmt(h.price_hike_cents * 12)}/yr)."
                )
            if zombies:
                lines.append(
                    f"- **Flagged to Review:** {', '.join(z.name for z in zombies[:4])}."
                )
            for b in bills[:4]:
                due_lbl = f"in {b.days_until_due}d" if b.days_until_due >= 0 else f"{abs(b.days_until_due)}d overdue"
                lines.append(f"- **Upcoming:** {b.name} — {_fmt(b.amount_cents)} ({b.cadence.value}, {due_lbl} on {b.next_due_date}).")
            return self._qa_response("\n".join(lines), verified_numbers, None)

        # --- Intent 6: Emergency Buffer / Spending Volatility ---
        if _BUFFER_INTENT_RE.search(safe_q):
            gap = vol.recommended_buffer_cents - s.emergency_buffer_cents
            stability_lbl = (
                "Steady" if vol.stability_score_0_100 >= 75 else ("Moderate" if vol.stability_score_0_100 >= 50 else "Variable")
            )
            lines = [
                f"**Spending Volatility & Statistical Buffer Sizing ({stability_lbl} · {vol.stability_score_0_100}/100):**",
                (
                    f"- **Trailing {vol.sample_days}-Day Discretionary Pattern:** Mean {_fmt(vol.weekly_mean_cents)}/wk "
                    f"(±{_fmt(vol.weekly_std_cents)} std, CV={vol.coefficient_of_variation:.2f})."
                ),
                (
                    f"- **Weekly Spend Percentiles:** P50 {_fmt(vol.p50_weekly_spend_cents)} · "
                    f"P75 {_fmt(vol.p75_weekly_spend_cents)} · P90 {_fmt(vol.p90_weekly_spend_cents)}."
                ),
                (
                    f"- **Emergency Buffer Target:** Current {_fmt(s.emergency_buffer_cents)} vs statistical target "
                    f"**{_fmt(vol.recommended_buffer_cents)}** "
                    f"({'gap of ' + _fmt(gap) if gap > 0 else 'fully funded with ' + _fmt(abs(gap)) + ' cushion above target!'})."
                ),
                (
                    f"- **Safe-to-Spend Confidence Band:** P50 daily {_fmt(vol.p50_daily_safe_cents)}/day · "
                    f"P90 conservative {_fmt(vol.p90_conservative_daily_safe_cents)}/day over the next {s.days_until_payday} days."
                ),
            ]
            return self._qa_response("\n".join(lines), verified_numbers, None)

        # --- Intent 7: Goals & ChronoX Trading/Invest Vault ---
        if _GOAL_INVEST_INTENT_RE.search(safe_q):
            lines = [
                f"**Goals & ChronoX Safe-to-Invest Guardrail ({_fmt(budget.safe_to_invest_cents)} Safe to Deploy):**",
                (
                    f"- **ChronoX Firewall:** {_fmt(budget.safe_to_invest_cents)} unlocked after protecting "
                    f"{_fmt(s.unpaid_bills_until_payday_cents)} in bills, {_fmt(s.credit_card_reserved_cents)} in card reserves, "
                    f"and your {_fmt(s.emergency_buffer_cents)} emergency buffer."
                ),
            ]
            active_goals = [g for g in (goals or []) if str(g.status).lower() == "active"]
            if active_goals:
                for g in active_goals[:4]:
                    td = f" · target {g.target_date}" if g.target_date else ""
                    lines.append(
                        f"- **Goal '{g.name}' (P{g.priority}):** {_fmt(g.saved_cents)} / {_fmt(g.target_cents)} "
                        f"({g.pct_funded:.0f}% funded · {_fmt(g.remaining_cents)} remaining{td})."
                    )
            else:
                lines.append("- **Active Goals:** None created yet — add a target in the Goals tab for automatic surplus weighting.")
            goal_steps = [st for st in qf.optimal_allocations if st.bucket in ("tracked_goal", "invest_vault")]
            if goal_steps:
                for st in goal_steps[:3]:
                    lines.append(f"- **Optimal Monthly Route:** {_fmt(st.amount_cents)} → {st.target_name} ({st.formula_note}).")
            return self._qa_response("\n".join(lines), verified_numbers, None)

        # --- Intent 8: General Financial Health Executive Briefing ---
        lines = [
            "**ChronoX Pure-Math Financial Health Snapshot:**",
            (
                f"- **Safe-to-Spend Band:** {_fmt(s.safe_to_spend_cents)} total over {s.days_until_payday}d → "
                f"**{_fmt(vol.p90_conservative_daily_safe_cents)}–{_fmt(vol.p50_daily_safe_cents)}/day** "
                f"(P90 conservative to P50 baseline; {_fmt(s.unpaid_bills_until_payday_cents)} already shielded for bills)."
            ),
            (
                f"- **{qf.horizon_days}-Day Cashflow Runway:** {_fmt(qf.starting_balance_cents)} → {_fmt(qf.ending_balance_cents)} "
                f"(nadir {_fmt(qf.lowest_balance_cents)} on {qf.lowest_balance_date} · "
                f"{'overdraft risk — review crunch alert' if qf.overdraft_risk else f'{qf.safe_runway_days}/{qf.horizon_days} safe days'})."
            ),
            (
                f"- **Spending Stability & Buffer:** {vol.stability_score_0_100}/100 stability · "
                f"Buffer {_fmt(s.emergency_buffer_cents)} vs statistical target {_fmt(vol.recommended_buffer_cents)} · "
                f"ChronoX Safe-to-Invest: {_fmt(budget.safe_to_invest_cents)}."
            ),
        ]
        if credit_report and credit_report.credit_lines:
            lines.append(
                f"- **Credit & Debt:** {credit_report.aggregate_utilization_pct:.1f}% util ({credit_report.aggregate_tier}) · "
                f"{_fmt(credit_report.total_debt_cents)} debt ({_fmt(credit_report.monthly_interest_burn_cents)}/mo interest) · "
                f"DTI {credit_report.dti_pct:.1f}%."
            )
        if qf.optimal_allocations:
            top = qf.optimal_allocations[0]
            lines.append(
                f"- **#1 Marginal-ROI Move ({_fmt(qf.monthly_surplus_cents)}/mo surplus):** Route {_fmt(top.amount_cents)} → "
                f"**{top.target_name}** ({top.effective_apr_equivalent_pct:.1f}% equiv · +{_fmt(qf.total_expected_annual_benefit_cents)}/yr total plan value)."
            )
        return self._qa_response("\n".join(lines), verified_numbers, None)

    @staticmethod
    def _qa_response(
        answer: str,
        verified_numbers: Dict[str, Any],
        affordability_check: Optional[Dict[str, Any]],
    ) -> CoachAnswerResponse:
        return CoachAnswerResponse(
            answer=answer,
            source="deterministic_quant",
            model_used=None,
            intelligence_mode=IntelligenceMode.DETERMINISTIC_ONLY.value,
            provider_used=None,
            verified_numbers=verified_numbers,
            affordability_check=affordability_check,
        )

    # =========================================================================
    # 5. Statistical Tail-Risk Runway, Diagnostics & One-Tap Optimization Bridge
    # =========================================================================

    def compute_statistical_runway(self, **kwargs: Any) -> Any:
        """Delegate to `OneTapQuantEngine.compute_statistical_runway`."""
        from chronox_finance.engines.onetap_quant import OneTapQuantEngine

        return OneTapQuantEngine().compute_statistical_runway(**kwargs)

    def compute_statistical_diagnostics(self, **kwargs: Any) -> Any:
        """Delegate to `OneTapQuantEngine.compute_statistical_diagnostics`."""
        from chronox_finance.engines.onetap_quant import OneTapQuantEngine

        return OneTapQuantEngine().compute_statistical_diagnostics(**kwargs)

    def build_onetap_plan(self, **kwargs: Any) -> Any:
        """Delegate to `OneTapQuantEngine.build_onetap_plan`."""
        from chronox_finance.engines.onetap_quant import OneTapQuantEngine

        return OneTapQuantEngine().build_onetap_plan(**kwargs)
