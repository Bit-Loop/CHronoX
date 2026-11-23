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


def advance_due_date(last_date: date, cadence: RecurrenceCadence, today: Optional[date] = None) -> date:
    """Project the next due date on or after `today`, preserving calendar day anchors cleanly."""
    ref = today or date.today()
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
        RecurrenceCadence.SEMIMONTHLY: 15,
    }

    due = last_date
    guard = 0
    while due < ref and guard < 120:
        if cadence in month_steps:
            due = _add_months(due, month_steps[cadence], last_date.day)
        else:
            due = due + timedelta(days=day_steps[cadence])
        guard += 1
    return due


def detect_recurring_bills(
    transactions: List[Dict[str, Any]],
    today: Optional[date] = None,
) -> List[DetectedRecurring]:
    """
    Fuzzy-cluster expense transactions and detect recurring bills & subscriptions.
    Survives descriptor drift, holiday/weekend date shifts, and step price hikes.
    """
    ref = today or date.today()
    categorizer = TransactionCategorizer()
    merchant_clusters = cluster_merchants(transactions)

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
        max_std = 6.5 if canonical_days >= 60 else 4.8
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

        # Allow higher seasonal variance on utilities/essentials (up to 30%), tighter on discretionary subs
        max_cv = 0.30 if ess_hint else 0.14
        # Always allow a clean step-function price hike (e.g. 1549, 1549, 2299 or 1549, 2299)
        is_step_hike = (
            len(amounts) >= 2
            and latest_amt > prior_median
            and (len(amounts) == 2 or (stdev(amounts[:-1]) / max(1, mean(amounts[:-1])) <= 0.05))
            and latest_amt <= int(prior_median * 2.0)
        )
        if amt_cv > max_cv and not is_step_hike:
            continue

        # Ignore stale subscriptions that stopped > 2.2 cycles ago
        if (ref - dates[-1]).days > int(canonical_days * 2.25):
            continue

        previous_amt: Optional[int] = None
        if len(amounts) >= 2 and abs(latest_amt - prior_median) >= 50:  # >= $0.50 meaningful change
            previous_amt = prior_median

        base_conf = 0.78 if len(deduped) == 2 else min(0.98, 0.85 + 0.03 * len(deduped))
        if delta_std <= 1.8 or (canonical_days >= 28 and dom_spread <= 1.5):
            base_conf = min(0.99, base_conf + 0.04)

        next_due = advance_due_date(dates[-1], cadence, today=ref)
        # Preserve clean latest normalized_merchant if single-descriptor cluster, else canonical cluster key
        unique_norms = {t["normalized_merchant"] for t in deduped}
        norm_key = next(iter(unique_norms)) if len(unique_norms) == 1 else cluster_key

        detected.append(
            DetectedRecurring(
                name=latest_tx["merchant"],
                normalized_merchant=norm_key,
                amount_cents=latest_amt if previous_amt is not None else med_amt,
                previous_amount_cents=previous_amt,
                cadence=cadence,
                next_due_date=next_due.isoformat(),
                jar_id=latest_tx.get("jar_id"),
                is_subscription=sub_hint or (not ess_hint and amt_cv <= 0.03),
                is_essential=ess_hint,
                confidence=round(base_conf, 2),
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
    2. Missed / overdue active bills (due date passed by >= 3 days with no recent charge).
    3. Silent price hikes on active recurring bills.
    """
    ref = today or date.today()
    anomalies: List[BillAnomaly] = []

    # 1. Double-charge scan over last 45 days
    recent_cutoff = (ref - timedelta(days=45)).isoformat()
    recent_txs = [t for t in transactions if t.get("tx_type") == "expense" and t["tx_date"] >= recent_cutoff]
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
                    )
                )
                break

    # 2. Missed / overdue bills & silent price hikes
    for b in recurring_bills:
        if not b.get("active", 1):
            continue
        due_dt = date.fromisoformat(b["next_due_date"])
        overdue_days = (ref - due_dt).days
        amt = int(b["amount_cents"])
        if overdue_days >= 3:
            anomalies.append(
                BillAnomaly(
                    kind="missed_bill",
                    merchant=b["name"],
                    amount_cents=amt,
                    summary=(
                        f"{b['name']} (${amt / 100:,.2f}) was expected {overdue_days} days ago "
                        f"({b['next_due_date']}) — verify autopay or mark paid so you don't get a late fee."
                    ),
                    dates=[b["next_due_date"]],
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
                )
            )

    return anomalies[:12]


def detect_payday_schedule(
    transactions: List[Dict[str, Any]],
    fallback_cadence_days: int = 14,
    fallback_payday_date: Optional[str] = None,
    fallback_paycheck_cents: int = 220_000,
    today: Optional[date] = None,
) -> DetectedPayday:
    """Infer next payday and expected paycheck amount from historical income inflows."""
    ref = today or date.today()
    incomes = [
        t for t in transactions
        if t.get("tx_type") == "income" and int(t.get("amount_cents", 0)) >= 20_000
    ]
    incomes.sort(key=lambda t: t["tx_date"])

    if len(incomes) >= 2:
        dates = [date.fromisoformat(t["tx_date"]) for t in incomes[-6:]]
        deltas = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1) if (dates[i + 1] - dates[i]).days >= 5]
        if deltas:
            avg_d = int(round(mean(deltas)))
            if 6 <= avg_d <= 8:
                cadence_days = 7
            elif 12 <= avg_d <= 16:
                cadence_days = 14
            elif 14 <= avg_d <= 17:
                cadence_days = 15
            elif 27 <= avg_d <= 33:
                cadence_days = 30
            else:
                cadence_days = max(7, min(31, avg_d))

            next_dt = dates[-1]
            while next_dt <= ref:
                next_dt += timedelta(days=cadence_days)
            est_cents = int(median([int(t["amount_cents"]) for t in incomes[-4:]]))
            return DetectedPayday(
                cadence_days=cadence_days,
                next_payday_date=next_dt.isoformat(),
                estimated_paycheck_cents=est_cents,
                confidence=0.92,
            )

    if fallback_payday_date:
        try:
            next_dt = date.fromisoformat(fallback_payday_date)
            while next_dt <= ref:
                next_dt += timedelta(days=max(7, fallback_cadence_days))
            return DetectedPayday(
                cadence_days=fallback_cadence_days,
                next_payday_date=next_dt.isoformat(),
                estimated_paycheck_cents=fallback_paycheck_cents,
                confidence=0.6,
            )
        except ValueError:
            pass

    next_dt = ref + timedelta(days=max(1, fallback_cadence_days // 2))
    return DetectedPayday(
        cadence_days=fallback_cadence_days,
        next_payday_date=next_dt.isoformat(),
        estimated_paycheck_cents=fallback_paycheck_cents,
        confidence=0.4,
    )
