"""
Clean-Room MoneyStill-Inspired Analytics, Net Worth & Scenario Forecast Engine.

All calculations run strictly in exact integer cents (`int`) with zero IEEE-754
floating-point penny drift and strict split-transaction awareness (reads child
`transaction_splits` when present, else parent — NEVER double-counting).

Outputs align 1:1 with `chronox_finance.models`:
1. Top 3 KPIs (`savings_rate_pct`, `avg_monthly_surplus_cents`, `top_5_expenses`)
2. 3-Column Cashflow Sankey (Income Sources -> Total Income Hub -> Top 5 + Others + Savings & Invest)
3. Expense Distribution Donut (exact same categorical hex color map as Sankey)
4. Monthly Trend Series (`month`, `income_cents`, `expense_cents`, `surplus_cents`, up to 24 months)
5. Net Worth Timeline reconstructed across up to 730 days (`liquid + investment - debt`)
6. Interactive Scenario Projection (`simulate_scenario`) returning `baseline_points`,
   `scenario_points`, `diff_ending_cents`, `diff_pct`, `safe_runway_floor_cents`, and
   `per_goal_series` for smooth client-side timeline drag + authoritative server commit.
"""

from __future__ import annotations

import calendar
import re
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple, Union

from chronox_finance.engines.autobudget import iter_effective_tx_allocations
from chronox_finance.engines.quant_math import QuantMathEngine
from chronox_finance.models import (
    AccountType,
    AnalyticsOverview,
    DailyCashflowPoint,
    GoalContributionSeries,
    MonthlyTrendPoint,
    NetWorthPoint,
    SankeyLink,
    SankeyNode,
    ScenarioOverride,
    ScenarioSimResult,
    TopExpenseItem,
    compute_holding_value_cents,
)

# Accessible industrial palette — expense hues intentionally exclude emerald (income) and cyan (surplus)
INCOME_PALETTE: Tuple[str, ...] = (
    "#047857",  # deep emerald (>= 5.1:1 on light, crisp on dark)
    "#0f766e",  # deep teal
    "#15803d",  # forest green
    "#059669",  # emerald
    "#0d9488",  # sea teal
)
EXPENSE_PALETTE: Tuple[str, ...] = (
    "#b45309",  # burnt amber (>= 4.7:1 on light)
    "#2563eb",  # royal blue
    "#7c3aed",  # violet
    "#dc2626",  # crimson
    "#db2777",  # magenta
    "#c2410c",  # rust orange
    "#4f46e5",  # indigo
    "#a16207",  # ochre
)
HUB_INCOME_COLOR = "#047857"
HUB_CASHFLOW_COLOR = "#475569"
OTHERS_EXPENSE_COLOR = "#64748b"
SAVINGS_INVEST_COLOR = "#0e7490"
UNALLOCATED_SURPLUS_COLOR = "#0369a1"
DEFICIT_DRAW_COLOR = "#b91c1c"
_RESERVED_SEMANTIC_COLORS = {
    "#10b981",
    "#047857",
    "#34d399",
    "#059669",
    "#06b6d4",
    "#0e7490",
    "#2dd4bf",
}

_YM_RE = re.compile(r"^(\d{4})-(\d{2})$")
_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _enum_val(val: Any, default: str = "") -> str:
    if val is None:
        return default
    return str(val.value if hasattr(val, "value") else val)


def _fmt_dollars(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


def resolve_period(
    period: Optional[str],
    today: Optional[date] = None,
) -> Tuple[str, date, date, int]:
    """
    Resolve unified period filter into `(normalized_period, start_date, end_date, canonical_months)`.

    Supports:
    - `30d` (30 days inclusive)
    - `90d` (90 days inclusive)
    - `1y` / `365d` (365 days inclusive)
    - `2y` / `730d` (730 days inclusive)
    - `YYYY-MM` (single calendar month)
    """
    ref = today or date.today()
    raw = (period or "1y").strip()
    low = raw.lower()

    if low == "30d":
        return "30d", ref - timedelta(days=29), ref, 1
    if low == "90d":
        return "90d", ref - timedelta(days=89), ref, 3
    if low in ("1y", "365d"):
        return "1y", ref - timedelta(days=364), ref, 12
    if low in ("2y", "730d"):
        return "2y", ref - timedelta(days=729), ref, 24

    match = _YM_RE.match(raw)
    if match:
        year = int(match.group(1))
        month = int(match.group(2))
        if not (1970 <= year <= 2100 and 1 <= month <= 12):
            raise ValueError(f"Invalid YYYY-MM period: {period!r}")
        last_day = calendar.monthrange(year, month)[1]
        return f"{year:04d}-{month:02d}", date(year, month, 1), date(year, month, last_day), 1

    raise ValueError(f"Unsupported analytics period {period!r}; expected 30d, 90d, 1y, 2y, or YYYY-MM.")


def _calendar_months_between(start_dt: date, end_dt: date, max_months: int = 24) -> List[str]:
    months: List[str] = []
    y, m = start_dt.year, start_dt.month
    while (y, m) <= (end_dt.year, end_dt.month):
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    if len(months) > max_months:
        months = months[-max_months:]
    return months


def _add_months(dt: date, months: int) -> date:
    total = (dt.year * 12 + (dt.month - 1)) + months
    y = total // 12
    m = (total % 12) + 1
    d = min(dt.day, calendar.monthrange(y, m)[1])
    return date(y, m, d)


def reconstruct_net_worth_timeline(
    *,
    accounts: Sequence[Any],
    transactions: Sequence[Any],
    holdings: Optional[Sequence[Any]] = None,
    credit_lines: Optional[Sequence[Any]] = None,
    existing_snapshots: Optional[Sequence[Any]] = None,
    portfolio_snapshots: Optional[Sequence[Any]] = None,
    days: int = 730,
    today: Optional[date] = None,
) -> List[NetWorthPoint]:
    """
    Reconstruct daily Net Worth (`liquid_cents + investment_cents - debt_cents`)
    across up to 730 days in exact integer cents.

    Uses persisted `net_worth_snapshots` where present and reconstructs missing
    historical days deterministically backward from current balances using ledger flows.
    """
    ref = today or date.today()
    span = max(1, min(730, int(days)))
    start_dt = ref - timedelta(days=span - 1)

    # 1. Anchor current balances on `today`
    liquid_acct_ids: set[int] = set()
    credit_acct_ids: set[int] = set()
    credit_acct_ext_ids: set[str] = set()
    current_liquid = 0
    current_trading_cash = 0
    current_debt = 0

    for acct in accounts:
        curr = str(_get(acct, "currency", "USD") or "USD").strip().upper()
        if curr != "USD":
            continue
        aid = _get(acct, "id", None)
        ext_id = str(_get(acct, "external_id", "") or "").strip()
        atype = _enum_val(_get(acct, "account_type", AccountType.CHECKING.value))
        bal = int(_get(acct, "balance_cents", 0) or 0)
        is_liq = bool(_get(acct, "is_liquid", True))

        if atype == AccountType.CREDIT.value:
            if aid is not None:
                credit_acct_ids.add(int(aid))
            if ext_id:
                credit_acct_ext_ids.add(ext_id)
            if bal < 0:
                current_debt += abs(bal)
            elif bal > 0:
                # Positive balance on a credit account is a credit overpayment / liquid credit
                current_liquid += bal
        elif atype == AccountType.TRADING.value:
            current_trading_cash += bal
        elif is_liq or atype in (AccountType.CHECKING.value, AccountType.SAVINGS.value, AccountType.CASH.value):
            if aid is not None:
                liquid_acct_ids.add(int(aid))
            current_liquid += bal

    # Add non-linked credit lines / loans so debt is never missed nor double-counted
    if credit_lines:
        for cl in credit_lines:
            if not _get(cl, "active", True):
                continue
            linked_id = _get(cl, "account_id", None)
            if linked_id is not None and int(linked_id) in credit_acct_ids:
                continue
            cl_ext = str(_get(cl, "external_account_id", "") or "").strip()
            if cl_ext and cl_ext in credit_acct_ext_ids:
                continue
            bal = max(0, int(_get(cl, "balance_cents", 0) or 0))
            current_debt += bal

    # Add exact holdings valuation (`quantity_e8 * price_e8` via ROUND_HALF_EVEN if needed)
    holdings_value = 0
    holding_anchor_dates: List[date] = []
    if holdings:
        for h in holdings:
            mv = _get(h, "market_value_cents", None)
            if mv is not None and int(mv) > 0:
                holdings_value += int(mv)
            else:
                q_e8 = int(_get(h, "quantity_e8", 0) or 0)
                p_e8 = int(_get(h, "price_e8", 0) or 0)
                holdings_value += compute_holding_value_cents(q_e8, p_e8)
            raw_anchor = str(_get(h, "created_at", "") or _get(h, "updated_at", "") or "")[:10]
            if len(raw_anchor) == 10:
                try:
                    holding_anchor_dates.append(date.fromisoformat(raw_anchor))
                except ValueError:
                    pass

    current_investments = holdings_value + current_trading_cash

    # 2. Index persisted net-worth and portfolio snapshots (if any)
    snap_by_date: Dict[str, Tuple[int, int, int]] = {}
    if existing_snapshots:
        for snap in existing_snapshots:
            s_date = str(_get(snap, "snap_date", _get(snap, "date", "")) or "")[:10]
            if not s_date:
                continue
            liq = int(_get(snap, "liquid_cents", 0) or 0)
            inv = int(_get(snap, "investment_cents", 0) or 0)
            dbt = int(_get(snap, "debt_cents", 0) or 0)
            snap_by_date[s_date] = (liq, inv, dbt)

    port_snaps_sorted: List[Tuple[date, int]] = []
    if portfolio_snapshots:
        for ps in portfolio_snapshots:
            p_date = str(_get(ps, "snap_date", "") or "")[:10]
            if len(p_date) == 10:
                try:
                    p_dt = date.fromisoformat(p_date)
                    port_snaps_sorted.append((p_dt, int(_get(ps, "total_value_cents", 0) or 0)))
                except ValueError:
                    continue
        port_snaps_sorted.sort(key=lambda pair: pair[0])

    earliest_inv_date: Optional[date] = None
    candidate_inv_dates: List[date] = []
    if port_snaps_sorted:
        candidate_inv_dates.append(port_snaps_sorted[0][0])
    if holding_anchor_dates:
        candidate_inv_dates.append(min(holding_anchor_dates))
    if candidate_inv_dates:
        earliest_inv_date = min(candidate_inv_dates)
    elif holdings_value > 0:
        # Holdings have no historical timestamp or snapshot -> anchor to `ref` so today's
        # valuation is NEVER projected backward into dates where no snapshot existed.
        earliest_inv_date = ref

    # 3. Bucket daily net ledger flows within [start_dt, ref]
    liquid_delta_by_day: Dict[date, int] = defaultdict(int)
    debt_delta_by_day: Dict[date, int] = defaultdict(int)
    trading_delta_by_day: Dict[date, int] = defaultdict(int)
    trading_acct_ids = {
        int(_get(a, "id"))
        for a in accounts
        if _get(a, "id", None) is not None
        and _enum_val(_get(a, "account_type", "")) == AccountType.TRADING.value
    }

    for tx in transactions:
        tx_date_str = str(_get(tx, "tx_date", "") or "")[:10]
        try:
            tx_dt = date.fromisoformat(tx_date_str)
        except ValueError:
            continue
        if tx_dt < start_dt or tx_dt > ref:
            continue
        amt = int(_get(tx, "amount_cents", 0) or 0)
        if amt <= 0:
            continue
        ttype = _enum_val(_get(tx, "tx_type", "expense"))
        xfer_dir = str(_get(tx, "transfer_direction", "") or "").strip().lower()
        acct_id = _get(tx, "account_id", None)
        is_credit_acct = acct_id is not None and int(acct_id) in credit_acct_ids
        is_trading_acct = acct_id is not None and int(acct_id) in trading_acct_ids

        if is_credit_acct:
            if ttype == "expense":
                debt_delta_by_day[tx_dt] += amt
            elif ttype in ("income", "transfer"):
                debt_delta_by_day[tx_dt] -= amt
        elif is_trading_acct:
            if ttype == "income" or (ttype == "transfer" and xfer_dir == "in"):
                trading_delta_by_day[tx_dt] += amt
            elif ttype == "expense" or (ttype == "transfer" and xfer_dir == "out"):
                trading_delta_by_day[tx_dt] -= amt
        else:
            if ttype == "income" or (ttype == "transfer" and xfer_dir == "in"):
                liquid_delta_by_day[tx_dt] += amt
            elif ttype == "expense" or (ttype == "transfer" and xfer_dir == "out"):
                liquid_delta_by_day[tx_dt] -= amt

    def _historical_holdings_value(day_dt: date) -> int:
        if day_dt >= ref:
            return holdings_value
        if earliest_inv_date is not None and day_dt < earliest_inv_date:
            return 0
        if port_snaps_sorted:
            # Latest recorded portfolio snapshot on or before `day_dt`
            chosen: Optional[int] = None
            for s_dt, s_val in port_snaps_sorted:
                if s_dt <= day_dt:
                    chosen = s_val
                else:
                    break
            if chosen is not None:
                return chosen
            return 0
        return holdings_value if (earliest_inv_date is not None and day_dt >= earliest_inv_date) else 0

    # 4. Walk backward from `today` -> `start_dt` so `today` matches exact live balances
    points_rev: List[NetWorthPoint] = []
    run_liq = current_liquid
    run_trading_cash = current_trading_cash
    run_debt = current_debt

    for offset in range(span):
        d = ref - timedelta(days=offset)
        iso = d.isoformat()
        if offset == 0:
            day_liq = current_liquid
            day_inv = current_investments
            day_debt = current_debt
        elif iso in snap_by_date:
            day_liq, day_inv, day_debt = snap_by_date[iso]
            run_liq = day_liq
            run_debt = day_debt
        else:
            day_liq = run_liq
            day_inv = max(0, run_trading_cash) + _historical_holdings_value(d)
            day_debt = run_debt

        nw = day_liq + day_inv - day_debt
        points_rev.append(
            NetWorthPoint(
                date=iso,
                liquid_cents=day_liq,
                investment_cents=day_inv,
                debt_cents=day_debt,
                net_worth_cents=nw,
            )
        )
        run_liq = run_liq - liquid_delta_by_day.get(d, 0)
        run_trading_cash = run_trading_cash - trading_delta_by_day.get(d, 0)
        run_debt = max(0, run_debt - debt_delta_by_day.get(d, 0))

    points_rev.reverse()
    return points_rev


def _build_goal_contribution_schedule(
    *,
    goal_id: int,
    goal_name: str,
    target_cents: int,
    saved_cents: int,
    target_date_str: Optional[str],
    ref: date,
    end_dt: date,
    step_days: int = 7,
) -> Tuple[GoalContributionSeries, Dict[date, int]]:
    """
    Build exact integer-cent periodic contribution schedule for a single active goal.

    Distributes `remaining_cents = max(0, target_cents - saved_cents)` evenly across
    weekly milestones from `ref + step_days` through `goal_end`, placing any remainder
    pennies on the final installment so `SUM(contribution_cents) == remaining_cents`.
    """
    remaining = max(0, int(target_cents) - int(saved_cents))
    if remaining <= 0:
        return (
            GoalContributionSeries(
                goal_id=goal_id,
                goal_name=goal_name,
                target_cents=target_cents,
                saved_cents=saved_cents,
                target_date=target_date_str,
                points=[],
            ),
            {},
        )

    goal_dt: Optional[date] = None
    if target_date_str:
        try:
            goal_dt = date.fromisoformat(str(target_date_str)[:10])
        except ValueError:
            goal_dt = None
    if goal_dt is None:
        goal_dt = ref + timedelta(days=180)
    if goal_dt <= ref:
        goal_dt = ref + timedelta(days=1)

    # Generate payment dates on weekly steps up to goal_dt
    pay_dates: List[date] = []
    cursor = ref + timedelta(days=step_days)
    while cursor < goal_dt:
        pay_dates.append(cursor)
        cursor += timedelta(days=step_days)
    pay_dates.append(goal_dt)

    n = len(pay_dates)
    base_installment = remaining // n
    remainder = remaining - (base_installment * n)

    daily_outflows: Dict[date, int] = defaultdict(int)
    points: List[Dict[str, Any]] = []
    cumulative = 0
    for idx, pdt in enumerate(pay_dates):
        amt = base_installment + (remainder if idx == n - 1 else 0)
        if amt <= 0:
            continue
        cumulative += amt
        if pdt <= end_dt:
            daily_outflows[pdt] += amt
            points.append(
                {
                    "date": pdt.isoformat(),
                    "contribution_cents": amt,
                    "cumulative_cents": cumulative,
                    "remaining_cents": max(0, remaining - cumulative),
                }
            )

    series = GoalContributionSeries(
        goal_id=goal_id,
        goal_name=goal_name,
        target_cents=target_cents,
        saved_cents=saved_cents,
        target_date=goal_dt.isoformat(),
        points=points,
    )
    return series, daily_outflows


class AnalyticsEngine:
    """Clean-room MoneyStill Analytics & Scenario Forecast Engine."""

    def build_overview(
        self,
        *,
        transactions: Sequence[Any],
        jars: Sequence[Any],
        accounts: Optional[Sequence[Any]] = None,
        holdings: Optional[Sequence[Any]] = None,
        credit_lines: Optional[Sequence[Any]] = None,
        net_worth_snapshots: Optional[Sequence[Any]] = None,
        portfolio_snapshots: Optional[Sequence[Any]] = None,
        splits_by_tx_id: Optional[Dict[int, List[Any]]] = None,
        period: str = "1y",
        group_by: Literal["jar", "subcategory"] = "jar",
        today: Optional[date] = None,
    ) -> AnalyticsOverview:
        """
        Compute unified MoneyStill Analytics dashboard payload in exact integer cents:
        - Top 3 KPIs (`savings_rate_pct`, `avg_monthly_surplus_cents`, `top_5_expenses`)
        - 3-Column Cashflow Sankey (honest source/destination labels: Unfunded Gap / Unallocated Surplus)
        - Expense Distribution Donut (exact shared color palette with Sankey)
        - Monthly Trend Series (clamped to exact date filter on rolling periods; never plots future months)
        - Net Worth Timeline reconstructed across up to 730 days
        """
        ref = today or date.today()
        norm_period, start_dt, end_dt, canonical_months = resolve_period(period, today=ref)
        start_iso = start_dt.isoformat()
        end_iso = end_dt.isoformat()
        is_single_month = bool(_YM_RE.match(norm_period))

        jars_by_id: Dict[int, Dict[str, Any]] = {}
        for j in jars:
            jid = _get(j, "id", None)
            if jid is not None:
                jars_by_id[int(jid)] = {
                    "id": int(jid),
                    "name": str(_get(j, "name", f"Jar #{jid}")),
                    "slug": str(_get(j, "slug", "")),
                    "color": str(_get(j, "color", "")),
                }

        acct_type_by_id: Dict[int, str] = {}
        if accounts:
            for a in accounts:
                aid = _get(a, "id", None)
                if aid is not None:
                    acct_type_by_id[int(aid)] = _enum_val(_get(a, "account_type", ""))

        # Determine trend window:
        # - Single YYYY-MM drilldown shows that calendar year's elapsed months up to `min(Dec 31, max(ref, end_dt))`
        #   so future unelapsed months are NEVER plotted as fake $0 inactivity.
        # - Rolling windows (`30d`, `90d`, `1y`, `2y`) strictly bound trend rows to `[start_iso, end_iso]`
        #   so chart totals ALWAYS equal KPI totals!
        if is_single_month:
            yr = start_dt.year
            trend_start = date(yr, 1, 1)
            year_cap = date(yr, 12, 31)
            trend_end = min(year_cap, max(ref, end_dt)) if yr >= ref.year else year_cap
        else:
            trend_start = start_dt
            trend_end = end_dt
        trend_start_iso = trend_start.isoformat()
        trend_end_iso = trend_end.isoformat()
        trend_months = _calendar_months_between(trend_start, trend_end, max_months=24)
        trend_month_set = set(trend_months)
        monthly_income: Dict[str, int] = defaultdict(int)
        monthly_expense: Dict[str, int] = defaultdict(int)

        # Period totals (split-aware — ZERO double-counting!)
        total_income_cents = 0
        total_expense_cents = 0
        verified_reserve_draw_cents = 0
        verified_savings_transfer_cents = 0
        income_by_source: Dict[str, int] = defaultdict(int)
        expense_by_cat: Dict[str, int] = defaultdict(int)
        cat_slug_map: Dict[str, str] = {}
        cat_preferred_color: Dict[str, str] = {}

        for tx in transactions:
            tx_date_str = str(_get(tx, "tx_date", "") or "")[:10]
            if len(tx_date_str) < 10:
                continue
            ym = tx_date_str[:7]
            ttype = _enum_val(_get(tx, "tx_type", "expense"))
            in_period = start_iso <= tx_date_str <= end_iso
            in_trend = (ym in trend_month_set) and (trend_start_iso <= tx_date_str <= trend_end_iso)

            if not in_period and not in_trend:
                continue

            if ttype == "transfer" and in_period:
                amt = int(_get(tx, "amount_cents", 0) or 0)
                if amt > 0:
                    merch_low = f"{_get(tx, 'merchant', '')} {_get(tx, 'memo', '')}".lower()
                    xfer_dir = str(_get(tx, "transfer_direction", "") or "").strip().lower()
                    acct_id = _get(tx, "account_id", None)
                    atype = acct_type_by_id.get(int(acct_id), "") if acct_id is not None else ""
                    if (
                        (atype in ("savings", "trading") and xfer_dir == "in")
                        or (
                            atype == "checking"
                            and xfer_dir == "out"
                            and any(k in merch_low for k in ("saving", "hysa", "invest", "broker", "schwab", "coinbase", "robinhood", "ira", "401k"))
                        )
                    ):
                        verified_savings_transfer_cents += amt
                    elif (
                        (atype == "savings" and xfer_dir == "out")
                        or (atype == "checking" and xfer_dir == "in" and any(k in merch_low for k in ("saving", "reserve", "hysa")))
                    ):
                        verified_reserve_draw_cents += amt
                continue

            if ttype == "income":
                amt = int(_get(tx, "amount_cents", 0) or 0)
                if amt <= 0:
                    continue
                if in_trend:
                    monthly_income[ym] += amt
                if in_period:
                    total_income_cents += amt
                    sub = str(_get(tx, "subcategory", "") or "").strip()
                    merch = str(_get(tx, "merchant", "") or "").strip()
                    src_label = sub if sub else (merch or "Income")
                    income_by_source[src_label] += amt

            elif ttype == "expense":
                allocs = iter_effective_tx_allocations(tx, splits_by_tx_id)
                has_splits = bool(_get(tx, "splits", None)) or (
                    splits_by_tx_id is not None
                    and _get(tx, "id", None) is not None
                    and bool(splits_by_tx_id.get(int(_get(tx, "id"))))
                )
                parent_sub = str(_get(tx, "subcategory", "") or "").strip()

                for jar_id, _goal_id, alloc_amt, alloc_memo in allocs:
                    if alloc_amt <= 0:
                        continue
                    if in_trend:
                        monthly_expense[ym] += alloc_amt
                    if not in_period:
                        continue

                    total_expense_cents += alloc_amt
                    jar_info = jars_by_id.get(jar_id) if jar_id is not None else None

                    if group_by == "subcategory":
                        if has_splits:
                            label = alloc_memo.strip()[:40] or (jar_info["name"] if jar_info else "Uncategorized")
                        else:
                            label = parent_sub or (jar_info["name"] if jar_info else "Uncategorized")
                    else:
                        if jar_info:
                            label = jar_info["name"]
                        elif not has_splits and parent_sub:
                            label = parent_sub
                        elif has_splits and alloc_memo.strip():
                            label = alloc_memo.strip()[:40]
                        else:
                            label = "Uncategorized"

                    expense_by_cat[label] += alloc_amt
                    if jar_info:
                        cat_slug_map.setdefault(label, jar_info["slug"])
                        if (
                            _HEX_COLOR_RE.match(jar_info["color"])
                            and jar_info["color"].lower() not in _RESERVED_SEMANTIC_COLORS
                        ):
                            cat_preferred_color.setdefault(label, jar_info["color"])

        # --- KPIs ---
        net_surplus_cents = total_income_cents - total_expense_cents
        savings_rate_pct = (
            round((net_surplus_cents / total_income_cents) * 100.0, 1)
            if total_income_cents > 0
            else 0.0
        )
        avg_monthly_surplus_cents = int(round(net_surplus_cents / max(1, canonical_months)))

        # --- Ranked Expense Categories & Shared Deterministic Color Map ---
        ranked_expenses = sorted(expense_by_cat.items(), key=lambda kv: (-kv[1], kv[0]))
        top_5_raw = ranked_expenses[:5]
        others_raw = ranked_expenses[5:]
        others_cents = sum(v for _, v in others_raw)

        used_colors: set[str] = set()
        cat_color_map: Dict[str, str] = {}
        for idx, (label, _amt) in enumerate(top_5_raw):
            pref = cat_preferred_color.get(label)
            if pref and pref.lower() not in used_colors:
                color = pref
            else:
                color = next(
                    (c for c in EXPENSE_PALETTE if c.lower() not in used_colors),
                    EXPENSE_PALETTE[idx % len(EXPENSE_PALETTE)],
                )
            used_colors.add(color.lower())
            cat_color_map[label] = color

        income_denom = total_income_cents if total_income_cents > 0 else max(1, total_expense_cents)
        expense_denom = max(1, total_expense_cents)

        top_5_expenses: List[TopExpenseItem] = []
        for label, amt in top_5_raw:
            top_5_expenses.append(
                TopExpenseItem(
                    label=label,
                    jar_slug=cat_slug_map.get(label, ""),
                    amount_cents=amt,
                    pct_of_expenses=round((amt / expense_denom) * 100.0, 1) if total_expense_cents > 0 else 0.0,
                    pct_of_income=round((amt / income_denom) * 100.0, 1) if (total_income_cents > 0 or total_expense_cents > 0) else 0.0,
                    color=cat_color_map[label],
                )
            )

        # Expense Distribution Donut shares exact color map + Others rollup
        expense_distribution: List[TopExpenseItem] = list(top_5_expenses)
        if others_cents > 0:
            expense_distribution.append(
                TopExpenseItem(
                    label="Others",
                    jar_slug="others",
                    amount_cents=others_cents,
                    pct_of_expenses=round((others_cents / expense_denom) * 100.0, 1),
                    pct_of_income=round((others_cents / income_denom) * 100.0, 1),
                    color=OTHERS_EXPENSE_COLOR,
                )
            )

        # --- 3-Column Cashflow Sankey (Honest Source & Destination Labels) ---
        sankey_nodes: List[SankeyNode] = []
        sankey_links: List[SankeyLink] = []
        hub_total_cents = max(total_income_cents, total_expense_cents)
        has_deficit = total_expense_cents > total_income_cents

        if hub_total_cents > 0:
            ranked_incomes = sorted(income_by_source.items(), key=lambda kv: (-kv[1], kv[0]))
            top_incomes = ranked_incomes[:5]
            other_inc_cents = sum(v for _, v in ranked_incomes[5:])
            col0_items: List[Tuple[str, int, str]] = [
                (lbl, amt, INCOME_PALETTE[idx % len(INCOME_PALETTE)])
                for idx, (lbl, amt) in enumerate(top_incomes)
            ]
            if other_inc_cents > 0:
                col0_items.append(("Other Income", other_inc_cents, "#047857"))
            if has_deficit:
                deficit_cents = total_expense_cents - total_income_cents
                verified_draw = min(deficit_cents, verified_reserve_draw_cents)
                unfunded_gap = deficit_cents - verified_draw
                if verified_draw > 0:
                    col0_items.append(("Cash Reserves Draw", verified_draw, "#b45309"))
                if unfunded_gap > 0:
                    col0_items.append(("Unfunded Gap", unfunded_gap, DEFICIT_DRAW_COLOR))

            for idx, (lbl, amt, color) in enumerate(col0_items):
                nid = f"inc:{idx}"
                pct = round((amt / hub_total_cents) * 100.0, 1) if has_deficit else round((amt / income_denom) * 100.0, 1)
                sankey_nodes.append(
                    SankeyNode(
                        id=nid,
                        label=lbl,
                        column=0,
                        amount_cents=amt,
                        pct_of_income=pct,
                        color=color,
                    )
                )
                sankey_links.append(
                    SankeyLink(
                        source=nid,
                        target="hub:total_income",
                        amount_cents=amt,
                        pct_of_income=pct,
                        color=color,
                    )
                )

            # Column 1: Center Hub — never call a deficit-inflated sum "Total Income"!
            hub_label = "Total Outflow" if has_deficit else "Total Income"
            hub_color = HUB_CASHFLOW_COLOR if has_deficit else HUB_INCOME_COLOR
            sankey_nodes.append(
                SankeyNode(
                    id="hub:total_income",
                    label=hub_label,
                    column=1,
                    amount_cents=hub_total_cents,
                    pct_of_income=100.0 if total_income_cents > 0 and not has_deficit else (
                        round((total_income_cents / hub_total_cents) * 100.0, 1) if hub_total_cents > 0 else 0.0
                    ),
                    color=hub_color,
                )
            )

            # Column 2: Top 5 Expense Categories + Others + Verified Savings / Unallocated Surplus
            for idx, item in enumerate(top_5_expenses):
                nid = f"exp:{idx}"
                sankey_nodes.append(
                    SankeyNode(
                        id=nid,
                        label=item.label,
                        column=2,
                        amount_cents=item.amount_cents,
                        pct_of_income=item.pct_of_income,
                        color=item.color,
                    )
                )
                sankey_links.append(
                    SankeyLink(
                        source="hub:total_income",
                        target=nid,
                        amount_cents=item.amount_cents,
                        pct_of_income=item.pct_of_income,
                        color=item.color,
                    )
                )

            if others_cents > 0:
                pct_others = round((others_cents / income_denom) * 100.0, 1)
                sankey_nodes.append(
                    SankeyNode(
                        id="exp:others",
                        label="Others",
                        column=2,
                        amount_cents=others_cents,
                        pct_of_income=pct_others,
                        color=OTHERS_EXPENSE_COLOR,
                    )
                )
                sankey_links.append(
                    SankeyLink(
                        source="hub:total_income",
                        target="exp:others",
                        amount_cents=others_cents,
                        pct_of_income=pct_others,
                        color=OTHERS_EXPENSE_COLOR,
                    )
                )

            if net_surplus_cents > 0:
                verified_sav = min(net_surplus_cents, verified_savings_transfer_cents)
                unallocated = net_surplus_cents - verified_sav
                if verified_sav > 0:
                    pct_sav = round((verified_sav / income_denom) * 100.0, 1)
                    sankey_nodes.append(
                        SankeyNode(
                            id="out:savings_invest",
                            label="Savings & Invest",
                            column=2,
                            amount_cents=verified_sav,
                            pct_of_income=pct_sav,
                            color=SAVINGS_INVEST_COLOR,
                        )
                    )
                    sankey_links.append(
                        SankeyLink(
                            source="hub:total_income",
                            target="out:savings_invest",
                            amount_cents=verified_sav,
                            pct_of_income=pct_sav,
                            color=SAVINGS_INVEST_COLOR,
                        )
                    )
                    if unallocated > 0:
                        pct_unalloc = round((unallocated / income_denom) * 100.0, 1)
                        sankey_nodes.append(
                            SankeyNode(
                                id="out:unallocated_surplus",
                                label="Unallocated Surplus",
                                column=2,
                                amount_cents=unallocated,
                                pct_of_income=pct_unalloc,
                                color=UNALLOCATED_SURPLUS_COLOR,
                            )
                        )
                        sankey_links.append(
                            SankeyLink(
                                source="hub:total_income",
                                target="out:unallocated_surplus",
                                amount_cents=unallocated,
                                pct_of_income=pct_unalloc,
                                color=UNALLOCATED_SURPLUS_COLOR,
                            )
                        )
                else:
                    pct_sur = round((net_surplus_cents / income_denom) * 100.0, 1)
                    sankey_nodes.append(
                        SankeyNode(
                            id="out:savings_invest",
                            label="Unallocated Surplus",
                            column=2,
                            amount_cents=net_surplus_cents,
                            pct_of_income=pct_sur,
                            color=UNALLOCATED_SURPLUS_COLOR,
                        )
                    )
                    sankey_links.append(
                        SankeyLink(
                            source="hub:total_income",
                            target="out:savings_invest",
                            amount_cents=net_surplus_cents,
                            pct_of_income=pct_sur,
                            color=UNALLOCATED_SURPLUS_COLOR,
                        )
                    )

        # --- Monthly Trend Series (up to 24 months) ---
        monthly_trends: List[MonthlyTrendPoint] = [
            MonthlyTrendPoint(
                month=ym,
                income_cents=monthly_income.get(ym, 0),
                expense_cents=monthly_expense.get(ym, 0),
                surplus_cents=monthly_income.get(ym, 0) - monthly_expense.get(ym, 0),
            )
            for ym in trend_months
        ]

        # --- Net Worth Timeline ---
        nw_days = 730 if norm_period == "2y" else (365 if norm_period == "1y" else (90 if norm_period == "90d" else 30))
        net_worth_series = reconstruct_net_worth_timeline(
            accounts=accounts or [],
            transactions=transactions,
            holdings=holdings,
            credit_lines=credit_lines,
            existing_snapshots=net_worth_snapshots,
            portfolio_snapshots=portfolio_snapshots,
            days=nw_days,
            today=ref,
        )

        return AnalyticsOverview(
            period=norm_period,
            start_date=start_iso,
            end_date=end_iso,
            total_income_cents=total_income_cents,
            total_expense_cents=total_expense_cents,
            net_surplus_cents=net_surplus_cents,
            savings_rate_pct=savings_rate_pct,
            avg_monthly_surplus_cents=avg_monthly_surplus_cents,
            top_5_expenses=top_5_expenses,
            sankey_nodes=sankey_nodes,
            sankey_links=sankey_links,
            expense_distribution=expense_distribution,
            monthly_trends=monthly_trends,
            net_worth_series=net_worth_series,
        )

    def simulate_scenario(
        self,
        *,
        starting_balance_cents: int,
        recurring_bills: Sequence[Any],
        goals: Sequence[Any] = (),
        transactions: Optional[Sequence[Any]] = None,
        credit_lines: Optional[Sequence[Any]] = None,
        payday_cadence_days: int = 14,
        next_payday_date: Optional[str] = None,
        estimated_paycheck_cents: int = 0,
        emergency_buffer_cents: int = 0,
        daily_discretionary_cents: int = 0,
        horizon_days: int = 365,
        overrides: Optional[Union[ScenarioOverride, Dict[str, Any]]] = None,
        today: Optional[date] = None,
    ) -> ScenarioSimResult:
        """
        Run baseline vs scenario projection across 30..730 days in exact integer cents.

        Returns `baseline_points`, `scenario_points`, `diff_ending_cents`, `diff_pct`,
        `safe_runway_floor_cents`, and `per_goal_series` (so UI timeline drag can smoothly
        re-project live without duplicating engine math, then commit via debounced `scenario-sim`).
        """
        ref = today or date.today()
        horizon = max(30, min(730, int(horizon_days)))
        end_dt = ref + timedelta(days=horizon - 1)
        floor_cents = max(0, int(emergency_buffer_cents))

        ov = (
            overrides
            if isinstance(overrides, ScenarioOverride)
            else ScenarioOverride.model_validate(overrides or {})
        )

        # 1. Compute base cashflow runway (without goals) over full horizon (up to 730d)
        quant = QuantMathEngine()
        base_runway = quant.simulate_cashflow_runway(
            starting_balance_cents=int(starting_balance_cents),
            recurring_bills=recurring_bills,
            transactions=transactions,
            credit_lines=credit_lines,
            azeo_plan=[],
            payday_cadence_days=payday_cadence_days,
            next_payday_date=next_payday_date,
            estimated_paycheck_cents=estimated_paycheck_cents,
            emergency_buffer_cents=floor_cents,
            days=horizon,
            max_horizon_days=730,
            include_azeo_paydowns=False,
            daily_discretionary_cents=daily_discretionary_cents,
            today=ref,
        )

        # 2. Active goals (respecting soft-delete `deleted_at` and priority ordering)
        active_goals = [
            g
            for g in goals
            if str(_get(g, "status", "active")).lower() == "active"
            and _get(g, "deleted_at", None) is None
            and max(0, int(_get(g, "target_cents", 0)) - int(_get(g, "saved_cents", 0))) > 0
        ]
        active_goals.sort(
            key=lambda g: (
                int(_get(g, "priority_order", 0) or 0),
                int(_get(g, "priority", 2) or 2),
                int(_get(g, "id", 0) or 0),
            )
        )

        baseline_goal_outflows: Dict[date, int] = defaultdict(int)
        baseline_goal_events: Dict[date, List[str]] = defaultdict(list)
        scenario_goal_outflows: Dict[date, int] = defaultdict(int)
        scenario_goal_events: Dict[date, List[str]] = defaultdict(list)
        per_goal_series: List[GoalContributionSeries] = []

        for g in active_goals:
            gid = int(_get(g, "id", 0))
            gname = str(_get(g, "name", f"Goal #{gid}"))
            target_c = int(_get(g, "target_cents", 0))
            saved_c = int(_get(g, "saved_cents", 0))
            base_td = _get(g, "target_date", None)

            _base_series, base_flow = _build_goal_contribution_schedule(
                goal_id=gid,
                goal_name=gname,
                target_cents=target_c,
                saved_cents=saved_c,
                target_date_str=base_td,
                ref=ref,
                end_dt=end_dt,
            )
            for dt, amt in base_flow.items():
                baseline_goal_outflows[dt] += amt
                baseline_goal_events[dt].append(f"Goal: {gname} (-{_fmt_dollars(amt)})")

            shifted_td = ov.goal_date_shifts.get(str(gid), base_td)
            scen_series, scen_flow = _build_goal_contribution_schedule(
                goal_id=gid,
                goal_name=gname,
                target_cents=target_c,
                saved_cents=saved_c,
                target_date_str=shifted_td,
                ref=ref,
                end_dt=end_dt,
            )
            per_goal_series.append(scen_series)
            for dt, amt in scen_flow.items():
                scenario_goal_outflows[dt] += amt
                scenario_goal_events[dt].append(f"Goal: {gname} (-{_fmt_dollars(amt)})")

        # 3. Monthly income/expense scenario deltas & one-off events
        scenario_extra_in: Dict[date, int] = defaultdict(int)
        scenario_extra_out: Dict[date, int] = defaultdict(int)
        scenario_extra_events: Dict[date, List[str]] = defaultdict(list)

        if ov.monthly_income_delta_cents != 0 or ov.monthly_expense_delta_cents != 0:
            m_step = 1
            while True:
                m_dt = _add_months(ref, m_step)
                if m_dt > end_dt:
                    break
                if ov.monthly_income_delta_cents > 0:
                    scenario_extra_in[m_dt] += ov.monthly_income_delta_cents
                    scenario_extra_events[m_dt].append(
                        f"Scenario income (+{_fmt_dollars(ov.monthly_income_delta_cents)})"
                    )
                elif ov.monthly_income_delta_cents < 0:
                    dec = abs(ov.monthly_income_delta_cents)
                    scenario_extra_out[m_dt] += dec
                    scenario_extra_events[m_dt].append(f"Scenario income change (-{_fmt_dollars(dec)})")

                if ov.monthly_expense_delta_cents > 0:
                    scenario_extra_out[m_dt] += ov.monthly_expense_delta_cents
                    scenario_extra_events[m_dt].append(
                        f"Scenario expense (-{_fmt_dollars(ov.monthly_expense_delta_cents)})"
                    )
                elif ov.monthly_expense_delta_cents < 0:
                    sav = abs(ov.monthly_expense_delta_cents)
                    scenario_extra_in[m_dt] += sav
                    scenario_extra_events[m_dt].append(f"Scenario savings (+{_fmt_dollars(sav)})")
                m_step += 1

        for ev in ov.one_off_events:
            ev_date_str = str(ev.get("date", "") or "")[:10]
            try:
                ev_dt = date.fromisoformat(ev_date_str)
            except ValueError:
                continue
            if not (ref <= ev_dt <= end_dt):
                continue
            raw_amt = int(ev.get("amount_cents", 0) or 0)
            if raw_amt == 0:
                continue
            label = str(ev.get("label") or ev.get("name") or "Scenario Event").strip()
            ev_type = str(ev.get("tx_type", "") or "").strip().lower()
            if ev_type == "expense" and raw_amt > 0:
                signed_amt = -raw_amt
            else:
                signed_amt = raw_amt

            if signed_amt > 0:
                scenario_extra_in[ev_dt] += signed_amt
                scenario_extra_events[ev_dt].append(f"{label} (+{_fmt_dollars(signed_amt)})")
            else:
                out_c = abs(signed_amt)
                scenario_extra_out[ev_dt] += out_c
                scenario_extra_events[ev_dt].append(f"{label} (-{_fmt_dollars(out_c)})")

        # 4. Assemble exact-cent daily baseline and scenario curves
        baseline_points: List[DailyCashflowPoint] = []
        scenario_points: List[DailyCashflowPoint] = []
        run_base = int(starting_balance_cents)
        run_scen = int(starting_balance_cents)

        for pt in base_runway.daily_points:
            d = date.fromisoformat(pt.date)
            base_in = pt.inflow_cents
            base_out = pt.outflow_cents + baseline_goal_outflows.get(d, 0)
            run_base = run_base + base_in - base_out
            baseline_points.append(
                DailyCashflowPoint(
                    date=pt.date,
                    balance_cents=run_base,
                    inflow_cents=base_in,
                    outflow_cents=base_out,
                    events=list(pt.events) + baseline_goal_events.get(d, []),
                )
            )

            scen_in = pt.inflow_cents + scenario_extra_in.get(d, 0)
            scen_out = pt.outflow_cents + scenario_goal_outflows.get(d, 0) + scenario_extra_out.get(d, 0)
            run_scen = run_scen + scen_in - scen_out
            scenario_points.append(
                DailyCashflowPoint(
                    date=pt.date,
                    balance_cents=run_scen,
                    inflow_cents=scen_in,
                    outflow_cents=scen_out,
                    events=list(pt.events) + scenario_goal_events.get(d, []) + scenario_extra_events.get(d, []),
                )
            )

        diff_ending_cents = run_scen - run_base
        if run_base != 0:
            diff_pct = round((diff_ending_cents / abs(run_base)) * 100.0, 2)
        elif diff_ending_cents > 0:
            diff_pct = 100.0
        elif diff_ending_cents < 0:
            diff_pct = -100.0
        else:
            diff_pct = 0.0

        return ScenarioSimResult(
            horizon_days=horizon,
            safe_runway_floor_cents=floor_cents,
            baseline_points=baseline_points,
            scenario_points=scenario_points,
            diff_ending_cents=diff_ending_cents,
            diff_pct=diff_pct,
            per_goal_series=per_goal_series,
        )


# Module-level convenience function matching brief signature
def simulate_scenario(
    *,
    starting_balance_cents: int,
    recurring_bills: Sequence[Any],
    goals: Sequence[Any] = (),
    transactions: Optional[Sequence[Any]] = None,
    credit_lines: Optional[Sequence[Any]] = None,
    payday_cadence_days: int = 14,
    next_payday_date: Optional[str] = None,
    estimated_paycheck_cents: int = 0,
    emergency_buffer_cents: int = 0,
    daily_discretionary_cents: int = 0,
    horizon_days: int = 365,
    overrides: Optional[Union[ScenarioOverride, Dict[str, Any]]] = None,
    today: Optional[date] = None,
) -> ScenarioSimResult:
    """Convenience wrapper delegating to `AnalyticsEngine().simulate_scenario(...)`."""
    return AnalyticsEngine().simulate_scenario(
        starting_balance_cents=starting_balance_cents,
        recurring_bills=recurring_bills,
        goals=goals,
        transactions=transactions,
        credit_lines=credit_lines,
        payday_cadence_days=payday_cadence_days,
        next_payday_date=next_payday_date,
        estimated_paycheck_cents=estimated_paycheck_cents,
        emergency_buffer_cents=emergency_buffer_cents,
        daily_discretionary_cents=daily_discretionary_cents,
        horizon_days=horizon_days,
        overrides=overrides,
        today=today,
    )
