"""
Robust Recurring Bill, Subscription, Anomaly & Payday Cadence Detector.

Handles real-world messy bank statements:
1. Fuzzy merchant clustering (merges 'NETFLIX.COM*8821' and 'NETFLIX INC CA' cleanly).
2. 8 full cadences: weekly, bi-weekly, semi-monthly (1st & 15th), monthly,
   bi-monthly (water/sewer), quarterly, semi-annual (6-mo insurance), annual.
3. Calendar-day circular anchoring (survives February & weekend/holiday billing shifts).
4. Baseline-median price-hike detection + annualized cost tracking.
5. Anomaly detection: suspected double charges (same merchant & amount within 3 days)
   and missed/overdue bills.
6. Excludes internal credit-card payment transfers so debt minimums never double-count.
"""

from __future__ import annotations

import calendar
import math
import re
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import mean, median, stdev
from typing import Any, Dict, List, Optional, Set, Tuple

from chronox_finance.engines.categorizer import TransactionCategorizer
from chronox_finance.models import BillAnomaly, RecurrenceCadence

CADENCE_WINDOWS: List[Tuple[RecurrenceCadence, float, float, int]] = [
    (RecurrenceCadence.WEEKLY, 5.5, 8.5, 7),
    (RecurrenceCadence.BIWEEKLY, 12.0, 16.0, 14),
    (RecurrenceCadence.MONTHLY, 25.0, 35.0, 30),
    (RecurrenceCadence.BIMONTHLY, 55.0, 67.0, 61),
    (RecurrenceCadence.QUARTERLY, 83.0, 99.0, 91),
    (RecurrenceCadence.SEMIANNUAL, 170.0, 195.0, 182),
    (RecurrenceCadence.ANNUAL, 350.0, 380.0, 365),
]

_STOP_TOKENS: Set[str] = {
    "com", "net", "org", "www", "http", "https", "us", "usa", "ca", "ny", "tx",
    "bill", "pay", "payment", "autopay", "recurring", "subscription", "sub",
    "membership", "monthly", "online", "service", "services", "help", "direct",
}

_INTERNAL_TRANSFER_RE = re.compile(
    r"\b(credit\s*crd\s*(?:autopay|epay|pmt|payment)|payment\s+thank\s+you|"
    r"autopay\s+payment|online\s+transfer|internal\s+transfer|card\s+payment)\b",
    re.IGNORECASE,
)


@dataclass
class DetectedRecurring:
    name: str
    normalized_merchant: str
    amount_cents: int
    previous_amount_cents: Optional[int]
    cadence: RecurrenceCadence
    next_due_date: str
    jar_id: Optional[int]
    is_subscription: bool
    is_essential: bool
    confidence: float
    amount_min_cents: Optional[int] = None
    amount_max_cents: Optional[int] = None
    due_window_days: int = 4


@dataclass
class DetectedPayday:
    cadence_days: int
    next_payday_date: str
    estimated_paycheck_cents: int
    confidence: float


def canonical_brand_tokens(normalized_merchant: str) -> List[str]:
    """Extract informative brand tokens from a normalized merchant string."""
    raw_tokens = [t for t in (normalized_merchant or "").lower().split() if len(t) >= 2]
    filtered = [t for t in raw_tokens if t not in _STOP_TOKENS and not t.isdigit()]
    return filtered or raw_tokens[:2]


def _token_jaccard(a: List[str], b: List[str]) -> float:
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    inter = len(sa & sb)
    union = len(sa | sb)
    if union == 0:
        return 0.0
    # Boost when the primary leading brand token is identical (e.g., "netflix" in "netflix" vs "netflix inc")
    if a[0] == b[0] and len(a[0]) >= 4:
        return max(inter / union, 0.75)
    return inter / union


def cluster_merchants(transactions: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """
    Fuzzy-cluster expense transactions whose descriptors drift slightly across billing cycles.
    Returns mapping of canonical cluster key -> list of transactions.
    """
    clusters: List[Tuple[str, List[str], List[Dict[str, Any]]]] = []
    exact_index: Dict[str, int] = {}

    for tx in transactions:
        if tx.get("tx_type") != "expense":
            continue
        norm = (tx.get("normalized_merchant") or "").strip()
        if not norm or norm == "unknown":
            continue
        if _INTERNAL_TRANSFER_RE.search(f"{tx.get('merchant', '')} {tx.get('memo', '')}"):
            continue

        if norm in exact_index:
            clusters[exact_index[norm]][2].append(tx)
            continue

        tokens = canonical_brand_tokens(norm)
        matched_idx: Optional[int] = None
        best_sim = 0.0
        for idx, (_key, c_tokens, _items) in enumerate(clusters):
            sim = _token_jaccard(tokens, c_tokens)
            if sim >= 0.65 and sim > best_sim:
                best_sim = sim
                matched_idx = idx

        if matched_idx is not None:
            clusters[matched_idx][2].append(tx)
            exact_index[norm] = matched_idx
        else:
            key = " ".join(tokens[:3]) if tokens else norm
            # Ensure cluster key uniqueness
            existing_keys = {c[0] for c in clusters}
            if key in existing_keys:
                key = norm
            exact_index[norm] = len(clusters)
            clusters.append((key, tokens, [tx]))

    return {key: items for key, _tokens, items in clusters}


def _circular_dom_spread(dates: List[date]) -> float:
    """
    Circular standard deviation (in days) of day-of-month across billing dates.
    Handles month-end wraparound (e.g. Jan 31, Feb 28, Mar 31, Apr 30) cleanly.
    """
    if len(dates) < 2:
        return 0.0
    angles = [2.0 * math.pi * (d.day / 31.0) for d in dates]
    sin_mean = sum(math.sin(a) for a in angles) / len(angles)
    cos_mean = sum(math.cos(a) for a in angles) / len(angles)
    r = min(1.0, max(1e-9, math.hypot(sin_mean, cos_mean)))
    circ_std_rad = math.sqrt(-2.0 * math.log(r))
    return (circ_std_rad / (2.0 * math.pi)) * 31.0


def _is_semimonthly(dates: List[date], deltas: List[int]) -> bool:
    """Detect 1st/15th (or two-anchor-day) semi-monthly billing patterns."""
    if len(dates) < 4:
        return False
    avg_d = mean(deltas)
    if not (13.5 <= avg_d <= 17.0):
        return False
    days = sorted({d.day for d in dates})
    # Two tight clusters separated by ~13-16 days (e.g., {1, 2} and {15, 16})
    low_cluster = [d.day for d in dates if d.day <= 14]
    high_cluster = [d.day for d in dates if d.day >= 15]
    if len(low_cluster) >= 2 and len(high_cluster) >= 2:
        low_span = max(low_cluster) - min(low_cluster)
        high_span = max(high_cluster) - min(high_cluster)
        if low_span <= 3 and high_span <= 3 and (min(high_cluster) - max(low_cluster)) >= 10:
            return True
    return len(days) == 2 and abs(days[1] - days[0]) in range(13, 17)


def _classify_cadence(dates: List[date], deltas: List[int]) -> Optional[Tuple[RecurrenceCadence, int]]:
    if not deltas:
        return None
    if _is_semimonthly(dates, deltas):
        return RecurrenceCadence.SEMIMONTHLY, 15
    avg_delta = mean(deltas)
    for cadence, lo, hi, canonical_days in CADENCE_WINDOWS:
        if lo <= avg_delta <= hi:
            return cadence, canonical_days
    return None


def _add_months(dt: date, months: int, anchor_day: int) -> date:
    total = (dt.year * 12 + (dt.month - 1)) + months
    year = total // 12
    month = (total % 12) + 1
    max_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(anchor_day, max_day))


def _step_semimonthly_forward(dt: date, low_day: int = 1, high_day: int = 15) -> date:
    """Advance to the next semi-monthly calendar date (e.g. 1st -> 15th -> next month 1st, or 15th -> month-end -> next 15th)."""
    lo = max(1, min(16, int(low_day)))
    hi = max(lo + 10, min(31, int(high_day)))
    max_this = calendar.monthrange(dt.year, dt.month)[1]
    eff_hi = min(hi, max_this)
    if dt.day < lo:
        return date(dt.year, dt.month, lo)
    if dt.day < eff_hi:
        return date(dt.year, dt.month, eff_hi)
    nxt = _add_months(date(dt.year, dt.month, 1), 1, lo)
    return nxt


def _step_semimonthly_back(dt: date, low_day: int = 1, high_day: int = 15) -> date:
    lo = max(1, min(16, int(low_day)))
    hi = max(lo + 10, min(31, int(high_day)))
    max_this = calendar.monthrange(dt.year, dt.month)[1]
    eff_hi = min(hi, max_this)
    if dt.day > eff_hi:
        return date(dt.year, dt.month, eff_hi)
    if dt.day > lo:
        return date(dt.year, dt.month, lo)
    prev_first = _add_months(date(dt.year, dt.month, 1), -1, 1)
    max_prev = calendar.monthrange(prev_first.year, prev_first.month)[1]
    return date(prev_first.year, prev_first.month, min(hi, max_prev))


def advance_due_date(last_date: date, cadence: RecurrenceCadence, today: Optional[date] = None) -> date:
    """Project the next due date on or after `today`, preserving calendar day anchors cleanly."""
    ref = today or date.today()
    due = last_date
    guard = 0
    while due < ref and guard < 120:
        due = _step_cycle_forward(due, cadence, last_date.day)
        guard += 1
    return due


DEFAULT_RECURRING_LOOKBACK_DAYS = 730


def _step_cycle_back(dt: date, cadence: RecurrenceCadence, anchor_day: int) -> date:
    if cadence == RecurrenceCadence.SEMIMONTHLY:
        if anchor_day >= 25:
            return _step_semimonthly_back(dt, 15, 31)
        return _step_semimonthly_back(dt, 1, 15)
    month_steps = {
        RecurrenceCadence.MONTHLY: -1,
        RecurrenceCadence.BIMONTHLY: -2,
        RecurrenceCadence.QUARTERLY: -3,
        RecurrenceCadence.SEMIANNUAL: -6,
        RecurrenceCadence.ANNUAL: -12,
    }
    day_steps = {
        RecurrenceCadence.WEEKLY: -7,
        RecurrenceCadence.BIWEEKLY: -14,
    }
    if cadence in month_steps:
        return _add_months(dt, month_steps[cadence], anchor_day)
    return dt + timedelta(days=day_steps.get(cadence, -30))


def _step_cycle_forward(dt: date, cadence: RecurrenceCadence, anchor_day: int) -> date:
    if cadence == RecurrenceCadence.SEMIMONTHLY:
        if anchor_day >= 25:
            return _step_semimonthly_forward(dt, 15, 31)
        return _step_semimonthly_forward(dt, 1, 15)
    month_steps = {
        RecurrenceCadence.MONTHLY: 1,
        RecurrenceCadence.BIMONTHLY: 2,
        RecurrenceCadence.QUARTERLY: 3,
        RecurrenceCadence.SEMIANNUAL: 6,
        RecurrenceCadence.ANNUAL: 12,
    }
    day_steps = {
        RecurrenceCadence.WEEKLY: 7,
        RecurrenceCadence.BIWEEKLY: 14,
    }
    if cadence in month_steps:
        return _add_months(dt, month_steps[cadence], anchor_day)
    return dt + timedelta(days=day_steps.get(cadence, 30))


def step_bill_due_date(dt: date, cadence: RecurrenceCadence, anchor_day: int) -> date:
    """Public calendar-accurate cycle stepper for recurring bills & payday schedules."""
    return _step_cycle_forward(dt, cadence, anchor_day)


def step_bill_due_date_backward(dt: date, cadence: RecurrenceCadence, anchor_day: int) -> date:
    """Public calendar-accurate backward cycle stepper for missed-bill lookback."""
    return _step_cycle_back(dt, cadence, anchor_day)


_DEBT_MATCH_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "from",
        "with",
        "plan",
        "payment",
        "payments",
        "bill",
        "bills",
        "autopay",
        "auto",
        "pay",
        "monthly",
        "biweekly",
        "weekly",
        "annual",
        "credit",
        "card",
        "cards",
        "account",
        "bank",
        "mgmt",
        "management",
        "resource",
        "resources",
        "group",
        "service",
        "services",
        "inc",
        "llc",
        "corp",
        "company",
        "financial",
        "collection",
        "collections",
        "debt",
        "loan",
        "min",
        "minimum",
        "agency",
    }
)


def _significant_entity_tokens(text: str) -> set[str]:
    cleaned = re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()
    return {tok for tok in cleaned.split() if len(tok) >= 3 and tok not in _DEBT_MATCH_STOPWORDS}


def _bill_monthly_equivalent_cents(amount_cents: int, cadence: Any) -> int:
    c_str = str(cadence.value if hasattr(cadence, "value") else (cadence or "monthly")).strip().lower()
    factors = {
        "weekly": 52.0 / 12.0,
        "biweekly": 26.0 / 12.0,
        "semimonthly": 2.0,
        "monthly": 1.0,
        "bimonthly": 0.5,
        "quarterly": 1.0 / 3.0,
        "semiannual": 1.0 / 6.0,
        "annual": 1.0 / 12.0,
    }
    return int(round(max(0, int(amount_cents)) * factors.get(c_str, 1.0)))


def credit_line_covered_by_recurring_bill(
    credit_line: Any,
    recurring_bills: Optional[List[Dict[str, Any]]],
    *,
    effective_min_cents: Optional[int] = None,
) -> bool:
    """
    Return True when an active recurring_bill already tracks the scheduled payment
    for `credit_line` (e.g., a biweekly collection payment plan bill matching a
    collection debt line), preventing forecast and Safe-to-Spend double-counting.

    Requires BOTH semantic entity match AND meaningful payment coverage (>= 50%
    of the credit line's effective monthly minimum) so unrelated small subscriptions
    at the same brand/issuer never mask a real card minimum.
    """
    if not recurring_bills:
        return False

    def _g(obj: Any, key: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    cl_name = str(_g(credit_line, "name", "") or "")
    cl_issuer = str(_g(credit_line, "issuer", "") or "")
    cl_kind = str(
        _g(credit_line, "kind").value
        if hasattr(_g(credit_line, "kind"), "value")
        else (_g(credit_line, "kind", "") or "")
    ).strip().lower()
    cl_tokens = _significant_entity_tokens(f"{cl_name} {cl_issuer}")

    eff_min = int(effective_min_cents) if effective_min_cents is not None else int(_g(credit_line, "min_payment_cents", 0) or 0)
    min_floor = int(round(eff_min * 0.50)) if eff_min > 0 else 0

    for b in recurring_bills:
        if not _g(b, "active", 1):
            continue
        b_name = str(_g(b, "name", "") or "")
        b_norm = str(_g(b, "normalized_merchant", "") or "")
        b_amt = forecast_bill_amount_cents(b) if isinstance(b, dict) else int(_g(b, "amount_cents", 0) or 0)
        if b_amt <= 0:
            continue
        b_monthly = _bill_monthly_equivalent_cents(b_amt, _g(b, "cadence", "monthly"))
        if min_floor > 0 and b_monthly < min_floor:
            continue

        b_tokens = _significant_entity_tokens(f"{b_name} {b_norm}")
        if cl_tokens and b_tokens and (cl_tokens & b_tokens):
            return True

        # Generic collection-plan fallback when bill & debt both indicate collection and match monthly min within ±15%
        is_collection_line = cl_kind == "collection" or "collection" in f"{cl_name} {cl_issuer}".lower()
        if is_collection_line and "collection" in f"{b_name} {b_norm}".lower() and eff_min > 0:
            if abs(b_monthly - eff_min) <= max(500, int(round(eff_min * 0.15))):
                return True

    return False


def forecast_bill_amount_cents(
    bill: Dict[str, Any],
    recent_matched_amounts: Optional[List[int]] = None,
    *,
    prefer_midpoint: bool = False,
) -> int:
    """
    Forecast exact integer-cent amount for a recurring bill (supporting variable utilities).

    Priority:
    1. Rolling mean of up to last 6 matched transaction amounts when supplied.
    2. Midpoint `(amount_min_cents + amount_max_cents) // 2` when min/max range is set and
       either `prefer_midpoint=True` or `amount_cents` is absent/outside range.
    3. Explicit `amount_cents`.
    """
    if recent_matched_amounts:
        valid = [int(a) for a in recent_matched_amounts[-6:] if int(a) > 0]
        if valid:
            return int(round(mean(valid)))

    lo_raw = bill.get("amount_min_cents")
    hi_raw = bill.get("amount_max_cents")
    amt = int(bill.get("amount_cents") or 0)
    if lo_raw is not None and hi_raw is not None:
        lo, hi = int(lo_raw), int(hi_raw)
        if hi >= lo > 0:
            midpoint = (lo + hi) // 2
            if prefer_midpoint or amt <= 0 or amt < lo or amt > hi:
                return midpoint
    return max(0, amt)


def is_within_due_window(
    tx_date: date,
    bill_due_date: date,
    cadence: RecurrenceCadence,
    due_window_days: int = 4,
) -> bool:
    """Check if `tx_date` falls within `+/- due_window_days` of any cycle aligned to `bill_due_date`."""
    window = max(0, int(due_window_days))
    if abs((tx_date - bill_due_date).days) <= window:
        return True

    anchor_day = bill_due_date.day
    cursor = bill_due_date
    if tx_date < bill_due_date:
        for _ in range(110):  # covers 2+ years even at weekly cadence
            cursor = _step_cycle_back(cursor, cadence, anchor_day)
            if abs((tx_date - cursor).days) <= window:
                return True
            if cursor < tx_date - timedelta(days=window + 40):
                break
    else:
        for _ in range(110):
            cursor = _step_cycle_forward(cursor, cadence, anchor_day)
            if abs((tx_date - cursor).days) <= window:
                return True
            if cursor > tx_date + timedelta(days=window + 40):
                break
    return False


def match_transaction_to_bill(
    tx: Dict[str, Any],
    bill: Dict[str, Any],
    *,
    require_date_window: bool = True,
) -> bool:
    """
    Firefly III-inspired bill matcher supporting `[amount_min_cents, amount_max_cents]`
    and `due_window_days` for variable utilities and fixed subscriptions alike.
    """
    if tx.get("tx_type") != "expense":
        return False
    tx_amt = int(tx.get("amount_cents") or 0)
    if tx_amt <= 0:
        return False

    # 1. Merchant / brand similarity check
    tx_norm = (tx.get("normalized_merchant") or tx.get("merchant") or "").strip().lower()
    bill_norm = (bill.get("normalized_merchant") or bill.get("name") or "").strip().lower()
    if not tx_norm or not bill_norm:
        return False

    merchant_match = tx_norm == bill_norm or bill_norm in tx_norm or tx_norm in bill_norm
    if not merchant_match:
        sim = _token_jaccard(canonical_brand_tokens(tx_norm), canonical_brand_tokens(bill_norm))
        merchant_match = sim >= 0.65
    if not merchant_match:
        return False

    # 2. Exact integer-cent amount range check
    lo_raw = bill.get("amount_min_cents")
    hi_raw = bill.get("amount_max_cents")
    if lo_raw is not None or hi_raw is not None:
        lo = int(lo_raw) if lo_raw is not None else 0
        hi = int(hi_raw) if hi_raw is not None else 10**12
        if lo > hi:
            lo, hi = hi, lo
        if not (lo <= tx_amt <= hi):
            return False
    else:
        bill_amt = int(bill.get("amount_cents") or 0)
        if bill_amt <= 0:
            return False
        tol_ratio = 0.15 if bill.get("is_essential", True) else 0.05
        tol = max(100, int(round(bill_amt * tol_ratio)))
        if abs(tx_amt - bill_amt) > tol:
            return False

    # 3. Due-date window check (optional for retroactive / unanchored bills)
    if require_date_window and bill.get("next_due_date") and tx.get("tx_date"):
        try:
            tx_dt = date.fromisoformat(str(tx["tx_date"])[:10])
            due_dt = date.fromisoformat(str(bill["next_due_date"])[:10])
        except ValueError:
            return False
        cadence_raw = bill.get("cadence", RecurrenceCadence.MONTHLY.value)
        try:
            cadence = cadence_raw if isinstance(cadence_raw, RecurrenceCadence) else RecurrenceCadence(str(cadence_raw))
        except ValueError:
            cadence = RecurrenceCadence.MONTHLY
        window = int(bill.get("due_window_days")) if bill.get("due_window_days") is not None else 4
        if not is_within_due_window(tx_dt, due_dt, cadence, due_window_days=window):
            return False

    return True


def find_matching_bill(
    tx: Dict[str, Any],
    recurring_bills: List[Dict[str, Any]],
    *,
    require_date_window: bool = True,
) -> Optional[Dict[str, Any]]:
    """Return the closest matching active recurring bill for a transaction, if any."""
    candidates: List[Tuple[int, Dict[str, Any]]] = []
    tx_amt = int(tx.get("amount_cents") or 0)
    for bill in recurring_bills:
        if not bill.get("active", 1):
            continue
        if match_transaction_to_bill(tx, bill, require_date_window=require_date_window):
            target = forecast_bill_amount_cents(bill)
            candidates.append((abs(tx_amt - target), bill))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0])
    return candidates[0][1]


def detect_recurring_bills(
    transactions: List[Dict[str, Any]],
    today: Optional[date] = None,
    lookback_days: int = DEFAULT_RECURRING_LOOKBACK_DAYS,
) -> List[DetectedRecurring]:
    """
    Fuzzy-cluster expense transactions over up to 730 days (2 full annual cycles)
    and detect recurring bills, variable utilities (with `[amount_min_cents, amount_max_cents]`
    and `due_window_days`), and subscriptions.
    Survives descriptor drift, holiday/weekend date shifts, and step price hikes.
    """
    ref = today or date.today()
    categorizer = TransactionCategorizer()

    # Enforce lookback window (default 730 days -> captures 2 full annual billing cycles)
    if lookback_days and lookback_days > 0:
        cutoff_iso = (ref - timedelta(days=int(lookback_days))).isoformat()
        scoped_txs = [t for t in transactions if str(t.get("tx_date", "")) >= cutoff_iso]
    else:
        scoped_txs = list(transactions)

    merchant_clusters = cluster_merchants(scoped_txs)

    detected: List[DetectedRecurring] = []

    for cluster_key, txs in merchant_clusters.items():
        if len(txs) < 2:
            continue

        sorted_txs = sorted(txs, key=lambda t: t["tx_date"])
        # Collapse same-day or 1-day duplicate authorizations for cadence calculation
        deduped: List[Dict[str, Any]] = []
        for t in sorted_txs:
            if not deduped:
                deduped.append(t)
                continue
            prev_dt = date.fromisoformat(deduped[-1]["tx_date"])
            curr_dt = date.fromisoformat(t["tx_date"])
            if (curr_dt - prev_dt).days <= 2 and int(t["amount_cents"]) == int(deduped[-1]["amount_cents"]):
                continue
            deduped.append(t)
        if len(deduped) < 2:
            continue

        dates = [date.fromisoformat(t["tx_date"]) for t in deduped]
        deltas = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
        cadence_info = _classify_cadence(dates, deltas)
        if not cadence_info:
            continue
        cadence, canonical_days = cadence_info

        # Check timing consistency (either tight delta std OR tight circular day-of-month anchor!)
        delta_std = stdev(deltas) if len(deltas) >= 2 else 0.0
        dom_spread = _circular_dom_spread(dates)
        max_std = 7.5 if canonical_days >= 180 else (6.5 if canonical_days >= 60 else 4.8)
        if delta_std > max_std and not (canonical_days >= 28 and dom_spread <= 2.5):
            continue

        amounts = [int(t["amount_cents"]) for t in deduped]
        latest_amt = amounts[-1]
        prior_median = int(median(amounts[:-1])) if len(amounts) >= 2 else latest_amt
        med_amt = int(median(amounts))
        avg_amt = mean(amounts)
        if avg_amt <= 0:
            continue
        amt_cv = (stdev(amounts) / avg_amt) if len(amounts) >= 2 else 0.0

        latest_tx = deduped[-1]
        sub_hint, ess_hint = categorizer.detect_hints(latest_tx["merchant"], latest_tx.get("memo", ""))

        # Allow higher seasonal variance on utilities/essentials (up to 45% when cadence is tight),
        # tighter on discretionary subscriptions.
        tight_timing = delta_std <= 4.0 or (canonical_days >= 28 and dom_spread <= 2.5)
        max_cv = (0.45 if tight_timing else 0.30) if ess_hint else 0.14
        # Always allow a clean step-function price hike (e.g. 1549, 1549, 2299 or 1549, 2299)
        is_step_hike = (
            len(amounts) >= 2
            and latest_amt > prior_median
            and (len(amounts) == 2 or (stdev(amounts[:-1]) / max(1, mean(amounts[:-1])) <= 0.05))
            and latest_amt <= int(prior_median * 2.0)
        )
        if amt_cv > max_cv and not is_step_hike:
            continue

        # Ignore stale subscriptions that stopped > 2.25 cycles ago (capped cleanly for annual)
        max_stale_days = max(int(canonical_days * 1.35), 400) if canonical_days >= 350 else int(canonical_days * 2.25)
        if (ref - dates[-1]).days > max_stale_days:
            continue

        previous_amt: Optional[int] = None
        if len(amounts) >= 2 and abs(latest_amt - prior_median) >= 50:  # >= $0.50 meaningful change
            if is_step_hike or not ess_hint or amt_cv <= 0.10:
                previous_amt = prior_median

        base_conf = 0.78 if len(deduped) == 2 else min(0.98, 0.85 + 0.03 * len(deduped))
        if delta_std <= 1.8 or (canonical_days >= 28 and dom_spread <= 1.5):
            base_conf = min(0.99, base_conf + 0.04)

        next_due = advance_due_date(dates[-1], cadence, today=ref)
        # Preserve clean latest normalized_merchant if single-descriptor cluster, else canonical cluster key
        unique_norms = {t["normalized_merchant"] for t in deduped}
        norm_key = next(iter(unique_norms)) if len(unique_norms) == 1 else cluster_key

        # Compute Firefly-style [amount_min_cents, amount_max_cents] and due_window_days
        is_variable_utility = (not is_step_hike) and (min(amounts) != max(amounts)) and (amt_cv > 0.05)
        if is_variable_utility:
            amt_min = min(amounts)
            amt_max = max(amounts)
            # Rolling mean of last 6 cycles forecasts variable utilities smoothly
            forecast_amt = int(round(mean(amounts[-6:])))
        else:
            forecast_amt = latest_amt if previous_amt is not None else med_amt
            amt_min = forecast_amt if is_step_hike else min(amounts)
            amt_max = forecast_amt if is_step_hike else max(amounts)

        due_window = (
            max(4, min(10, int(math.ceil(dom_spread * 2.0)) + 2))
            if (len(dates) >= 2 and canonical_days >= 28)
            else 4
        )

        detected.append(
            DetectedRecurring(
                name=latest_tx["merchant"],
                normalized_merchant=norm_key,
                amount_cents=forecast_amt,
                previous_amount_cents=previous_amt,
                cadence=cadence,
                next_due_date=next_due.isoformat(),
                jar_id=latest_tx.get("jar_id"),
                is_subscription=sub_hint or (not ess_hint and amt_cv <= 0.03),
                is_essential=ess_hint,
                confidence=round(base_conf, 2),
                amount_min_cents=amt_min,
                amount_max_cents=amt_max,
                due_window_days=due_window,
            )
        )

    return sorted(detected, key=lambda b: b.next_due_date)


def detect_bill_anomalies(
    transactions: List[Dict[str, Any]],
    recurring_bills: List[Dict[str, Any]],
    today: Optional[date] = None,
) -> List[BillAnomaly]:
    """
    Detect real-world billing anomalies that ambush ADHD users:
    1. Suspected double charges (same merchant cluster & identical amount within 3 days).
    2. Missed / overdue active bills (due date passed beyond due window with no matching charge).
    3. Silent price hikes on active recurring bills.
    """
    ref = today or date.today()
    anomalies: List[BillAnomaly] = []

    # 1. Double-charge scan over last 45 days
    recent_cutoff = (ref - timedelta(days=45)).isoformat()
    all_expense_txs = [t for t in transactions if t.get("tx_type") == "expense"]
    recent_txs = [t for t in all_expense_txs if t["tx_date"] >= recent_cutoff]
    clusters = cluster_merchants(recent_txs)
    for _key, items in clusters.items():
        if len(items) < 2:
            continue
        ordered = sorted(items, key=lambda x: x["tx_date"])
        for i in range(len(ordered) - 1):
            t1, t2 = ordered[i], ordered[i + 1]
            d1 = date.fromisoformat(t1["tx_date"])
            d2 = date.fromisoformat(t2["tx_date"])
            if int(t1["amount_cents"]) == int(t2["amount_cents"]) and 0 <= (d2 - d1).days <= 3:
                amt = int(t1["amount_cents"])
                m_low = str(t2.get("merchant") or "").lower()
                if amt < 200 or any(tok in m_low for tok in ("intl transaction fee", "international transaction fee", "foreign transaction fee")):
                    continue
                anomalies.append(
                    BillAnomaly(
                        kind="double_charge",
                        merchant=t2["merchant"],
                        amount_cents=amt,
                        summary=(
                            f"Possible double charge at {t2['merchant']}: "
                            f"${amt / 100:,.2f} appeared twice ({t1['tx_date']} & {t2['tx_date']})."
                        ),
                        dates=[t1["tx_date"], t2["tx_date"]],
                        severity="medium",
                    )
                )
                break

    # 2. Missed / overdue bills (both current overdue due date AND stepped-past previous cycle) & price hikes
    for b in recurring_bills:
        if not b.get("active", 1):
            continue
        due_dt = date.fromisoformat(b["next_due_date"])
        overdue_days = (ref - due_dt).days
        amt = forecast_bill_amount_cents(b)
        due_window = max(3, int(b.get("due_window_days") or 3))
        is_autopay = bool(b.get("auto_pay", 0))
        bill_id = int(b["id"]) if b.get("id") is not None else None
        raw_cad = b.get("cadence") or RecurrenceCadence.MONTHLY.value
        try:
            cadence_enum = raw_cad if isinstance(raw_cad, RecurrenceCadence) else RecurrenceCadence(str(raw_cad))
        except ValueError:
            cadence_enum = RecurrenceCadence.MONTHLY

        missed_recorded = False
        if overdue_days >= due_window:
            # Skip false alarm if a transaction matching [min, max] & due window already posted
            already_paid = any(
                match_transaction_to_bill(t, b, require_date_window=True)
                for t in all_expense_txs
                if abs((date.fromisoformat(t["tx_date"]) - due_dt).days) <= max(due_window, 5)
            )
            if not already_paid:
                missed_recorded = True
                anomalies.append(
                    BillAnomaly(
                        kind="missed_bill",
                        merchant=b["name"],
                        amount_cents=amt,
                        summary=(
                            f"{b['name']} (${amt / 100:,.2f}) was expected {overdue_days} days ago "
                            f"({b['next_due_date']}) — "
                            + (
                                "autopay did not post as expected; verify payment or remove if cancelled."
                                if is_autopay
                                else "verify payment, mark paid, or remove if cancelled."
                            )
                        ),
                        dates=[b["next_due_date"]],
                        bill_id=bill_id,
                        severity="medium" if is_autopay else "high",
                        auto_pay=is_autopay,
                        days_overdue=overdue_days,
                    )
                )

        # Previous-cycle missed-bill lookback: catches bills whose next_due_date already stepped forward
        # (e.g., last paid Oct 2, next_due_date advanced to Dec 2, while Nov 2 cycle never posted).
        if not missed_recorded and overdue_days < due_window:
            prev_due_dt = step_bill_due_date_backward(due_dt, cadence_enum, due_dt.day)
            prev_overdue_days = (ref - prev_due_dt).days
            prev_window = max(due_window, 6)
            if max(due_window, 4) <= prev_overdue_days <= 45:
                # Only alert if historical payments exist BEFORE the missed cycle so brand-new bills never false-alarm
                prior_cutoff = prev_due_dt - timedelta(days=prev_window)
                has_prior_history = any(
                    date.fromisoformat(t["tx_date"]) < prior_cutoff
                    and match_transaction_to_bill(t, b, require_date_window=False)
                    for t in all_expense_txs
                )
                if has_prior_history:
                    paid_in_prev_cycle = any(
                        abs((date.fromisoformat(t["tx_date"]) - prev_due_dt).days) <= prev_window
                        and match_transaction_to_bill(t, b, require_date_window=False)
                        for t in all_expense_txs
                    )
                    if not paid_in_prev_cycle:
                        anomalies.append(
                            BillAnomaly(
                                kind="missed_bill",
                                merchant=b["name"],
                                amount_cents=amt,
                                summary=(
                                    f"Missed cycle for {b['name']} (${amt / 100:,.2f}): no payment posted around "
                                    f"{prev_due_dt.isoformat()} ({prev_overdue_days}d ago) — "
                                    + (
                                        "check autopay or remove if you switched providers."
                                        if is_autopay
                                        else "verify payment, mark paid, or remove if you switched providers."
                                    )
                                ),
                                dates=[prev_due_dt.isoformat()],
                                bill_id=bill_id,
                                severity="medium" if is_autopay else "high",
                                auto_pay=is_autopay,
                                days_overdue=prev_overdue_days,
                            )
                        )

        prev = b.get("previous_amount_cents")
        if prev is not None and amt > int(prev):
            hike = amt - int(prev)
            anomalies.append(
                BillAnomaly(
                    kind="price_hike",
                    merchant=b["name"],
                    amount_cents=hike,
                    summary=(
                        f"{b['name']} increased by +${hike / 100:,.2f}/cycle "
                        f"(${int(prev) / 100:,.2f} → ${amt / 100:,.2f}, +${(hike * 12) / 100:,.2f}/yr)."
                    ),
                    dates=[b["next_due_date"]],
                    bill_id=bill_id,
                    severity="low",
                    auto_pay=is_autopay,
                )
            )

    kind_rank = {"missed_bill": 0, "double_charge": 1, "price_hike": 2}
    sev_rank = {"high": 0, "medium": 1, "low": 2}
    anomalies.sort(key=lambda a: (kind_rank.get(a.kind, 9), sev_rank.get(a.severity, 9), -a.days_overdue, -a.amount_cents))
    return anomalies[:12]


def _is_semimonthly_payroll(dates: List[date], deltas: List[int]) -> Optional[Tuple[int, int]]:
    """
    Detect semi-monthly payroll (e.g. 1st & 15th, or 15th & last day of month)
    from >= 2 deposits and return `(low_day, high_day)` anchors when matched.
    """
    if len(dates) < 2 or not deltas:
        return None
    if not all(12 <= d <= 18 for d in deltas):
        return None
    days = [d.day for d in dates]
    # Pattern A: 1st & 15th (allowing weekend banking shift: 1..3 or 28..31 paired with 13..16)
    near_first_or_mid = all((1 <= dom <= 3) or (13 <= dom <= 16) for dom in days)
    has_first = any(1 <= dom <= 3 for dom in days)
    has_mid = any(13 <= dom <= 16 for dom in days)
    if near_first_or_mid and has_first and has_mid:
        return (1, 15)
    # Pattern B: 15th & end-of-month (13..16 paired with 28..31)
    near_mid_or_end = all((13 <= dom <= 16) or (28 <= dom <= 31) for dom in days)
    has_end = any(28 <= dom <= 31 for dom in days)
    if near_mid_or_end and has_mid and has_end:
        return (15, 31)
    if len(dates) >= 4 and _is_semimonthly(dates, deltas):
        lo = int(round(mean([dom for dom in days if dom <= 14]))) if any(dom <= 14 for dom in days) else 1
        hi = int(round(mean([dom for dom in days if dom >= 15]))) if any(dom >= 15 for dom in days) else 15
        return (lo, hi)
    return None


def detect_payday_schedule(
    transactions: List[Dict[str, Any]],
    fallback_cadence_days: int = 14,
    fallback_payday_date: Optional[str] = None,
    fallback_paycheck_cents: int = 0,
    today: Optional[date] = None,
    user_configured_payday: bool = False,
) -> DetectedPayday:
    """
    Infer next payday and expected paycheck amount from recent historical income inflows.

    Guarantees:
    - Never invents default paycheck income when `fallback_paycheck_cents == 0` and no recent payroll exists.
    - Respects `user_configured_payday=True` so explicit user payday settings are never overwritten.
    - Discounts stale deposits (>95 days old) so year-old deposits never get high confidence.
    - Preserves calendar anchors on semi-monthly (1st & 15th / 15th & month-end) payrolls.
    """
    ref = today or date.today()

    def _step_fallback_date(start_dt: date, cad_days: int) -> date:
        nxt = start_dt
        guard = 0
        while nxt <= ref and guard < 240:
            if cad_days == 15:
                lo_anchor, hi_anchor = (1, 15) if start_dt.day <= 20 else (15, 31)
                nxt = _step_semimonthly_forward(nxt, lo_anchor, hi_anchor)
            elif cad_days in (30, 31):
                nxt = _add_months(nxt, 1, start_dt.day)
            else:
                nxt += timedelta(days=max(7, cad_days))
            guard += 1
        return nxt

    all_incomes: List[Tuple[date, Dict[str, Any]]] = []
    for t in transactions:
        if t.get("tx_type") != "income" or int(t.get("amount_cents", 0)) < 20_000:
            continue
        try:
            dt = date.fromisoformat(str(t["tx_date"])[:10])
        except ValueError:
            continue
        if dt <= ref:
            all_incomes.append((dt, t))
    all_incomes.sort(key=lambda pair: pair[0])

    # Only use recent payroll history (within trailing 95 days) for active schedule inference
    recent_cutoff = ref - timedelta(days=95)
    recent_pairs = [(dt, t) for dt, t in all_incomes if dt >= recent_cutoff]

    if user_configured_payday and fallback_payday_date:
        try:
            cfg_dt = _step_fallback_date(date.fromisoformat(fallback_payday_date), fallback_cadence_days)
            cfg_paycheck = max(0, int(fallback_paycheck_cents))
            if cfg_paycheck == 0 and len(recent_pairs) >= 2:
                cfg_paycheck = int(median([int(t["amount_cents"]) for _, t in recent_pairs[-4:]]))
            return DetectedPayday(
                cadence_days=fallback_cadence_days,
                next_payday_date=cfg_dt.isoformat(),
                estimated_paycheck_cents=cfg_paycheck,
                confidence=0.95 if cfg_paycheck > 0 else 0.7,
            )
        except ValueError:
            pass

    if len(recent_pairs) >= 2:
        sample_pairs = recent_pairs[-6:]
        dates = [dt for dt, _ in sample_pairs]
        deltas = [
            (dates[i + 1] - dates[i]).days
            for i in range(len(dates) - 1)
            if (dates[i + 1] - dates[i]).days >= 5
        ]
        if deltas:
            days_since_last = (ref - dates[-1]).days
            semi_anchors = _is_semimonthly_payroll(dates, deltas)
            avg_d = int(round(mean(deltas)))
            spread = max(deltas) - min(deltas) if len(deltas) >= 2 else 0

            if semi_anchors is not None:
                cadence_days = 15
                lo_day, hi_day = semi_anchors
                next_dt = dates[-1]
                guard = 0
                while next_dt <= ref and guard < 48:
                    next_dt = _step_semimonthly_forward(next_dt, lo_day, hi_day)
                    guard += 1
            else:
                if 6 <= avg_d <= 8:
                    cadence_days = 7
                elif 12 <= avg_d <= 16:
                    cadence_days = 14
                elif 27 <= avg_d <= 33:
                    cadence_days = 30
                else:
                    cadence_days = max(7, min(31, avg_d))

                next_dt = dates[-1]
                guard = 0
                while next_dt <= ref and guard < 120:
                    if cadence_days == 30:
                        next_dt = _add_months(next_dt, 1, dates[-1].day)
                    else:
                        next_dt += timedelta(days=cadence_days)
                    guard += 1

            est_cents = int(median([int(t["amount_cents"]) for _, t in sample_pairs[-4:]]))

            # Calibrated confidence: requires >=3 recent, regular deposits and fresh last deposit
            if days_since_last > max(40, cadence_days * 2 + 7):
                conf = 0.35
            elif len(sample_pairs) >= 3 and (semi_anchors is not None or spread <= 4):
                conf = 0.90
            elif len(sample_pairs) >= 3 and spread <= 7:
                conf = 0.78
            else:
                # Only 2 deposits or wide interval spread -> moderate confidence (never overwrites settings)
                conf = 0.62

            return DetectedPayday(
                cadence_days=cadence_days,
                next_payday_date=next_dt.isoformat(),
                estimated_paycheck_cents=est_cents,
                confidence=conf,
            )

    # Stale historical deposits (>95 days old): never claim high confidence or project active payroll
    has_only_stale = len(all_incomes) >= 2 and len(recent_pairs) < 2

    if fallback_payday_date:
        try:
            next_dt = _step_fallback_date(date.fromisoformat(fallback_payday_date), fallback_cadence_days)
            return DetectedPayday(
                cadence_days=fallback_cadence_days,
                next_payday_date=next_dt.isoformat(),
                estimated_paycheck_cents=max(0, int(fallback_paycheck_cents)),
                confidence=0.20 if has_only_stale else (0.60 if fallback_paycheck_cents > 0 else 0.0),
            )
        except ValueError:
            pass

    next_dt = ref + timedelta(days=max(1, fallback_cadence_days // 2))
    return DetectedPayday(
        cadence_days=fallback_cadence_days,
        next_payday_date=next_dt.isoformat(),
        estimated_paycheck_cents=max(0, int(fallback_paycheck_cents)),
        confidence=0.15 if has_only_stale else (0.40 if fallback_paycheck_cents > 0 else 0.0),
    )
