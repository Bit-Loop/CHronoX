"""
Executive CSV Pack & Apple Numbers Multi-Sheet Graphical Workbook Exporter.

Generates 100% pure Python stdlib exports with zero heavy third-party spreadsheet
dependencies so wheels, Docker images, and PyInstaller sidecars work out of the box:

1. `export_transactions_csv(...) -> bytes`:
   RFC-4180 UTF-8 CSV (optional UTF-8 BOM for Apple Numbers / Excel double-click)
   with spreadsheet formula-injection neutralization on untrusted text columns while
   preserving exact numeric values for `amount_dollars` and `amount_cents`.
2. `export_executive_csv_pack_zip(...) -> bytes`:
   Structured 7-table `.zip` archive (`01_executive_summary.csv` through
   `07_goals_and_bills.csv`).
3. `export_apple_numbers_workbook(...) -> ExportArtifact`:
   Standards-compliant ECMA-376 OOXML `.xlsx` multi-sheet graphical workbook engineered
   for Apple Numbers, Microsoft Excel, and LibreOffice Calc with dark-industrial
   slate/emerald executive styling, live spreadsheet formulas (`SUM`, `IF`, `MAX`),
   exact currency/percent/date number formats, and 5 live native editable DrawingML
   `<c:chartSpace>` vector charts (`<c:barChart>`, `<c:doughnutChart>`, `<c:lineChart>`,
   `<c:pieChart>`).
4. `convert_to_native_numbers_if_macos(xlsx_bytes) -> ExportArtifact`:
   Optional macOS helper that converts `.xlsx` -> native `.numbers` ONLY when
   explicitly requested on `sys.platform == "darwin"` with `/Applications/Numbers.app`
   and `osascript` available; otherwise returns the `.xlsx` workbook honestly without
   ever disguising `.xlsx` bytes under a `.numbers` extension.
"""

from __future__ import annotations

import csv
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union
from xml.sax.saxutils import escape as _xml_escape
from xml.sax.saxutils import quoteattr as _xml_quoteattr

# --- Public Constants & Media Types ---

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
NUMBERS_MEDIA_TYPE = "application/vnd.apple.numbers"
CSV_MEDIA_TYPE = "text/csv; charset=utf-8"
ZIP_MEDIA_TYPE = "application/zip"

SHEET_EXECUTIVE = "Executive Overview"
SHEET_CASHFLOW = "Cashflow & Runway"
SHEET_PORTFOLIO = "Portfolio & Wealth"
SHEET_CREDIT = "Credit & Debt Analysis"
SHEET_GOALS_BILLS = "Goals & Bills"
SHEET_TRANSACTIONS = "Transactions Ledger"

SHEET_NAMES: Tuple[str, ...] = (
    SHEET_EXECUTIVE,
    SHEET_CASHFLOW,
    SHEET_PORTFOLIO,
    SHEET_CREDIT,
    SHEET_GOALS_BILLS,
    SHEET_TRANSACTIONS,
)

CSV_PACK_FILENAMES: Tuple[str, ...] = (
    "01_executive_summary.csv",
    "02_accounts.csv",
    "03_budget_jars.csv",
    "04_transactions.csv",
    "05_credit_and_debt.csv",
    "06_portfolio_holdings.csv",
    "07_goals_and_bills.csv",
)

TRANSACTIONS_CSV_HEADERS: Tuple[str, ...] = (
    "id",
    "tx_date",
    "account_name",
    "currency",
    "direction",
    "transfer_direction",
    "amount_dollars",
    "amount_cents",
    "merchant",
    "category",
    "jar",
    "memo_reason",
    "tags",
    "source",
    "affects_balance",
    "split_summary",
)

CREDIT_FACTOR_DISCLAIMER = (
    "Weighted factor analysis from tracked accounts, inquiries, and history — not an official bureau score."
)

_E8_SCALE = Decimal(100_000_000)
_CENTS_SCALE = Decimal(100)
_EXCEL_EPOCH = date(1899, 12, 30)
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_ILLEGAL_XML_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]")

_CHART_PALETTE: Tuple[str, ...] = (
    "10B981",  # Emerald
    "06B6D4",  # Cyan
    "6366F1",  # Indigo
    "F59E0B",  # Amber
    "F43F5E",  # Rose
    "475569",  # Slate
    "14B8A6",  # Teal
    "8B5CF6",  # Violet
    "F97316",  # Orange
    "0EA5E9",  # Sky
)

_DEFAULT_FICO8_FACTORS: Tuple[Tuple[str, int, str], ...] = (
    ("Payment History", 35, "On-time payment consistency and autopay coverage"),
    ("Amounts Owed / Utilization (AZEO)", 30, "Revolving credit utilization ratio across cards"),
    ("Length of Credit History (AAoA / AoOA)", 15, "Average and oldest tradeline age"),
    ("New Credit & Hard Inquiries", 10, "Recent hard pulls and newly opened tradelines"),
    ("Credit Mix", 10, "Diversity of revolving and installment tradelines"),
)

_DEFAULT_VANTAGE3_FACTORS: Tuple[Tuple[str, int, str], ...] = (
    ("Payment History", 40, "On-time payment consistency and autopay coverage"),
    ("Age & Type of Credit (Depth)", 21, "Account age and revolving/installment mix"),
    ("Credit Utilization", 20, "Revolving balance-to-limit ratio"),
    ("Total Balances", 11, "Aggregate debt load across tracked accounts"),
    ("Recent Credit & Inquiries", 5, "Recently opened credit lines and inquiries"),
    ("Available Credit", 3, "Unused revolving credit headroom"),
)


# --- Low-Level Precision, Date & Sanitization Helpers ---


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sanitize_xml_text(value: object) -> str:
    """Strip XML 1.0 illegal control characters while preserving valid Unicode."""
    if isinstance(value, Enum):
        value = value.value
    text = "" if value is None else str(value)
    return _ILLEGAL_XML_RE.sub("", text)


def sanitize_csv_text(value: object) -> str:
    """
    Neutralize spreadsheet formula injection on untrusted text strings.

    Prefixes cells starting with `=`, `+`, `-`, `@`, `\\t`, or `\\r` (or leading
    ASCII spaces followed by one of those trigger characters) with a single
    apostrophe so Excel, Apple Numbers, and LibreOffice never evaluate user-supplied
    strings as formulas. Never call this on true numeric cells like `amount_dollars`.
    """
    if isinstance(value, Enum):
        value = value.value
    text = "" if value is None else str(value)
    if not text:
        return ""
    if text[0] in _CSV_FORMULA_PREFIXES:
        return "'" + text
    stripped_spaces = text.lstrip(" ")
    if stripped_spaces and stripped_spaces[0] in _CSV_FORMULA_PREFIXES:
        return "'" + text
    return text


def sanitize_sheet_text(value: object) -> str:
    """Apply XML 1.0 character safety for OOXML shared strings (CSV formula quoting stays CSV-only)."""
    return sanitize_xml_text(value)


def cents_to_decimal_str(cents: int) -> str:
    """Convert exact integer cents to fixed 2-decimal string without float drift."""
    d = (Decimal(int(cents)) / _CENTS_SCALE).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    return f"{d:.2f}"


def e8_to_decimal_str(value_e8: int, *, min_decimals: int = 0) -> str:
    """Convert exact 10^-8 integer units to canonical decimal string (up to 8 decimals)."""
    d = (Decimal(int(value_e8)) / _E8_SCALE).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_EVEN)
    text = format(d, "f")
    if "." in text:
        int_part, frac_part = text.split(".", 1)
        frac_trimmed = frac_part.rstrip("0")
        if len(frac_trimmed) < min_decimals:
            frac_trimmed = frac_trimmed.ljust(min_decimals, "0")
        return f"{int_part}.{frac_trimmed}" if frac_trimmed else int_part
    return text


def ratio_to_decimal_str(pct_value: float) -> str:
    """Convert a percentage number (e.g., 24.5 for 24.5%) into spreadsheet decimal (0.245000)."""
    try:
        d = (Decimal(str(pct_value)) / _CENTS_SCALE).quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN)
    except (InvalidOperation, ValueError):
        d = Decimal("0.000000")
    return format(d, "f")


def bps_to_decimal_str(apr_bps: int) -> str:
    """Convert basis points (e.g., 2199 for 21.99%) into spreadsheet decimal (0.219900)."""
    d = (Decimal(int(apr_bps)) / Decimal(10_000)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN)
    return format(d, "f")


def iso_date_to_excel_serial(date_str: str) -> Optional[int]:
    """Convert ISO YYYY-MM-DD (>= 1900-03-01) into Excel/OOXML 1900 serial date integer."""
    raw = (date_str or "").strip()
    if len(raw) < 10:
        return None
    try:
        parsed = date.fromisoformat(raw[:10])
    except ValueError:
        return None
    if parsed < date(1900, 3, 1):
        return None
    return (parsed - _EXCEL_EPOCH).days


def col_index_to_letter(col_idx: int) -> str:
    """Convert 1-based column index to Excel column letters (1->A, 26->Z, 27->AA)."""
    if col_idx < 1:
        raise ValueError(f"Column index must be >= 1, got {col_idx}")
    chars: List[str] = []
    n = col_idx
    while n > 0:
        n, rem = divmod(n - 1, 26)
        chars.append(chr(65 + rem))
    return "".join(reversed(chars))


def cell_ref(row_idx: int, col_idx: int, *, absolute: bool = False) -> str:
    """Format 1-based (row, col) into A1 or $A$1 reference."""
    col_ltr = col_index_to_letter(col_idx)
    if absolute:
        return f"${col_ltr}${row_idx}"
    return f"{col_ltr}{row_idx}"


def quote_sheet_name(sheet_name: str) -> str:
    """Quote a worksheet title for OOXML formulas and chart references."""
    escaped = sheet_name.replace("'", "''")
    return f"'{escaped}'"


def signed_tx_cents(direction: str, amount_cents: int, transfer_direction: str = "") -> int:
    """
    Compute signed integer cents for cashflow/ledger exports:
    - expense: negative
    - income: positive
    - transfer with transfer_direction == "out": negative
    - transfer with transfer_direction == "in" or unspecified: positive
    """
    raw = abs(int(amount_cents))
    tx_dir = (direction or "expense").strip().lower()
    if tx_dir == "income":
        return raw
    if tx_dir == "transfer":
        xfer = (transfer_direction or "").strip().lower()
        return -raw if xfer == "out" else raw
    return -raw


def _get_field(obj: Any, key: str, default: Any = None) -> Any:
    """Read an attribute or mapping key uniformly from Pydantic models, dataclasses, or dicts."""
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        val = obj.get(key, default)
    else:
        val = getattr(obj, key, default)
    if isinstance(val, Enum):
        return val.value
    return val


def _as_int(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(Decimal(str(value)))
        except (InvalidOperation, ValueError):
            return default


def _as_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y", "on"):
        return True
    if text in ("0", "false", "no", "n", "off", ""):
        return False
    return default


def _format_tags(raw_tags: Any) -> str:
    if not raw_tags:
        return ""
    if isinstance(raw_tags, str):
        return raw_tags.strip()
    names: List[str] = []
    if isinstance(raw_tags, Iterable):
        for item in raw_tags:
            if isinstance(item, str):
                name = item.strip()
            else:
                name = str(_get_field(item, "name", "") or "").strip()
            if name:
                names.append(name)
    return "; ".join(names)


def _format_splits(raw_splits: Any) -> str:
    if not raw_splits:
        return ""
    if isinstance(raw_splits, str):
        return raw_splits.strip()
    parts: List[str] = []
    if isinstance(raw_splits, Iterable):
        for item in raw_splits:
            jar_name = str(_get_field(item, "jar_name", "") or "").strip()
            memo = str(_get_field(item, "memo", "") or "").strip()
            amt_cents = _as_int(_get_field(item, "amount_cents", 0))
            label = jar_name or memo or "Split"
            parts.append(f"{label} (${cents_to_decimal_str(amt_cents)})")
    return "; ".join(parts)


# --- Typed Export Data Contracts ---


@dataclass(frozen=True)
class ExportArtifact:
    """Binary export artifact with honest filename, media type, and metadata."""

    filename: str
    media_type: str
    content: bytes
    format: str
    sheet_names: Tuple[str, ...] = ()
    row_counts: Dict[str, int] = field(default_factory=dict)
    generated_at: str = ""
    native_numbers_converted: bool = False
    notes: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutiveKPIs:
    """Top-level executive financial metrics for Sheet 1 and 01_executive_summary.csv."""

    liquid_cash_cents: int = 0
    safe_to_spend_today_cents: int = 0
    safe_until_payday_cents: int = 0
    safe_to_invest_cap_cents: int = 0
    net_worth_cents: int = 0
    tail_cushion_95_cents: int = 0
    inflow_30d_cents: int = 0
    outflow_30d_cents: int = 0
    net_flow_30d_cents: Optional[int] = None
    total_investments_cents: int = 0
    total_debt_cents: int = 0
    days_until_payday: int = 0
    next_payday_date: str = ""
    ruin_probability_pct: Optional[float] = None

    @property
    def resolved_net_flow_30d_cents(self) -> int:
        if self.net_flow_30d_cents is not None:
            return int(self.net_flow_30d_cents)
        return int(self.inflow_30d_cents) - int(self.outflow_30d_cents)


@dataclass(frozen=True)
class AccountExportRow:
    id: int = 0
    name: str = ""
    account_type: str = "checking"
    institution: str = ""
    currency: str = "USD"
    balance_cents: int = 0
    available_cents: Optional[int] = None
    credit_limit_cents: Optional[int] = None
    is_liquid: bool = True
    mask: str = ""


@dataclass(frozen=True)
class JarExportRow:
    id: int = 0
    slug: str = ""
    name: str = ""
    kind: str = "daily"
    target_pct: float = 0.0
    monthly_target_cents: int = 0
    allocated_cents: int = 0
    available_cents: int = 0
    spent_cents: int = 0
    rollover_cents: int = 0
    pct_used: float = 0.0
    health: str = "calm"
    is_locked: bool = False


@dataclass(frozen=True)
class CategoryExpenseRow:
    label: str
    amount_cents: int
    pct_of_expenses: float = 0.0
    pct_of_income: float = 0.0
    jar_slug: str = ""


@dataclass(frozen=True)
class MonthlyCashflowRow:
    month: str
    income_cents: int = 0
    expense_cents: int = 0
    net_cents: Optional[int] = None
    savings_rate_pct: Optional[float] = None

    @property
    def resolved_net_cents(self) -> int:
        if self.net_cents is not None:
            return int(self.net_cents)
        return int(self.income_cents) - int(self.expense_cents)

    @property
    def resolved_savings_rate_pct(self) -> float:
        if self.savings_rate_pct is not None:
            return float(self.savings_rate_pct)
        if self.income_cents <= 0:
            return 0.0
        return round((self.resolved_net_cents / self.income_cents) * 100.0, 2)


@dataclass(frozen=True)
class RunwayPointRow:
    date: str
    p50_balance_cents: int = 0
    inflow_cents: int = 0
    outflow_cents: int = 0
    p10_balance_cents: Optional[int] = None
    p90_balance_cents: Optional[int] = None
    events: str = ""


@dataclass(frozen=True)
class HoldingExportRow:
    symbol: str
    name: str = ""
    asset_class: str = "equity"
    quantity_e8: int = 0
    price_e8: int = 0
    market_value_cents: int = 0
    cost_basis_cents: Optional[int] = None
    cost_basis_known: bool = True
    unrealized_pnl_cents: Optional[int] = None
    unrealized_pnl_pct: Optional[float] = None
    source: str = "manual"


@dataclass(frozen=True)
class AllocationExportRow:
    asset_class: str
    value_cents: int = 0
    pct: float = 0.0
    holdings_count: int = 0


@dataclass(frozen=True)
class CreditScoreExportRow:
    bureau: str
    model: str = "fico8"
    score: int = 0
    pulled_date: str = ""
    source: str = "manual"
    notes: str = ""


@dataclass(frozen=True)
class CreditFactorExportRow:
    model: str  # "FICO 8" or "VantageScore 3.0"
    label: str
    weight_pct: int
    status: str = "untracked"
    summary: str = ""
    guidance: str = ""


@dataclass(frozen=True)
class CreditLineExportRow:
    id: int = 0
    name: str = ""
    issuer: str = ""
    kind: str = "credit_card"
    balance_cents: int = 0
    credit_limit_cents: int = 0
    utilization_pct: Optional[float] = None
    apr_bps: int = 0
    min_payment_cents: int = 0
    monthly_interest_cents: int = 0
    azeo_paydown_cents: int = 0
    statement_close_date: str = ""
    next_due_date: str = ""
    autopay_mode: str = "statement"


@dataclass(frozen=True)
class DebtStrategyExportRow:
    strategy: str
    label: str
    tagline: str = ""
    months_to_debt_free: int = 0
    debt_free_date: str = ""
    total_interest_cents: int = 0
    interest_saved_vs_minimums_cents: int = 0
    first_win_months: int = 0
    first_target_name: str = ""
    recommended: bool = False


@dataclass(frozen=True)
class GoalExportRow:
    id: int = 0
    name: str = ""
    category: str = "custom"
    funding_jar_slug: str = "fun"
    target_cents: int = 0
    saved_cents: int = 0
    remaining_cents: Optional[int] = None
    pct_funded: Optional[float] = None
    target_date: str = ""
    priority: int = 2
    status: str = "active"
    notes: str = ""

    @property
    def resolved_remaining_cents(self) -> int:
        if self.remaining_cents is not None:
            return max(0, int(self.remaining_cents))
        return max(0, int(self.target_cents) - int(self.saved_cents))

    @property
    def resolved_pct_funded(self) -> float:
        if self.pct_funded is not None:
            return float(self.pct_funded)
        if self.target_cents <= 0:
            return 0.0
        return round((self.saved_cents / self.target_cents) * 100.0, 2)


@dataclass(frozen=True)
class BillExportRow:
    id: int = 0
    name: str = ""
    amount_cents: int = 0
    cadence: str = "monthly"
    next_due_date: str = ""
    days_until_due: int = 0
    is_essential: bool = True
    is_subscription: bool = False
    auto_pay: bool = False
    flagged_zombie: bool = False


@dataclass(frozen=True)
class TransactionExportRow:
    id: int = 0
    tx_date: str = ""
    account_id: Optional[int] = None
    account_name: str = ""
    currency: str = "USD"
    direction: str = "expense"
    transfer_direction: str = ""
    amount_cents: int = 0
    merchant: str = ""
    category: str = ""
    jar: str = ""
    memo_reason: str = ""
    tags: str = ""
    source: str = "manual"
    affects_balance: bool = True
    split_summary: str = ""


@dataclass(frozen=True)
class ExportSnapshot:
    """Complete, immutable ledger snapshot consumed by CSV Pack and Workbook exporters."""

    as_of_date: str = field(default_factory=lambda: date.today().isoformat())
    generated_at: str = field(default_factory=_utc_now_iso)
    profile_currency: str = "USD"
    profile_name: str = "ChronoX Executive Financial Report"
    kpis: ExecutiveKPIs = field(default_factory=ExecutiveKPIs)
    accounts: Tuple[AccountExportRow, ...] = ()
    jars: Tuple[JarExportRow, ...] = ()
    top_expenses: Tuple[CategoryExpenseRow, ...] = ()
    monthly_cashflow: Tuple[MonthlyCashflowRow, ...] = ()
    runway_points: Tuple[RunwayPointRow, ...] = ()
    holdings: Tuple[HoldingExportRow, ...] = ()
    allocations: Tuple[AllocationExportRow, ...] = ()
    credit_scores: Tuple[CreditScoreExportRow, ...] = ()
    credit_factors: Tuple[CreditFactorExportRow, ...] = ()
    credit_lines: Tuple[CreditLineExportRow, ...] = ()
    debt_strategies: Tuple[DebtStrategyExportRow, ...] = ()
    credit_disclaimer: str = CREDIT_FACTOR_DISCLAIMER
    goals: Tuple[GoalExportRow, ...] = ()
    bills: Tuple[BillExportRow, ...] = ()
    transactions: Tuple[TransactionExportRow, ...] = ()


# --- Pure-Stdlib OOXML Styles, Shared Strings, Worksheets & Charts ---


class _SharedStrings:
    """Deduplicated OOXML shared string table (`xl/sharedStrings.xml`)."""

    def __init__(self) -> None:
        self._index: Dict[str, int] = {}
        self._items: List[str] = []
        self._total_refs: int = 0

    def add(self, raw: object) -> int:
        text = sanitize_xml_text(raw)
        self._total_refs += 1
        idx = self._index.get(text)
        if idx is None:
            idx = len(self._items)
            self._index[text] = idx
            self._items.append(text)
        return idx

    def to_xml(self) -> str:
        parts = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            (
                '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                f'count="{self._total_refs}" uniqueCount="{len(self._items)}">'
            ),
        ]
        for text in self._items:
            needs_preserve = bool(text) and (text[0].isspace() or text[-1].isspace() or "\n" in text)
            space_attr = ' xml:space="preserve"' if needs_preserve else ""
            parts.append(f"<si><t{space_attr}>{_xml_escape(text)}</t></si>")
        parts.append("</sst>")
        return "".join(parts)


# Style index constants aligned with `_build_styles_xml()`
STYLE_DEFAULT = 0
STYLE_BANNER_TITLE = 1
STYLE_BANNER_SUBTITLE = 2
STYLE_SECTION_HEADER = 3
STYLE_TABLE_HEADER = 4
STYLE_TEXT = 5
STYLE_TEXT_ZEBRA = 6
STYLE_TEXT_BOLD = 7
STYLE_KPI_LABEL = 8
STYLE_KPI_MONEY = 9
STYLE_KPI_MONEY_EMERALD = 10
STYLE_MONEY = 11
STYLE_MONEY_ZEBRA = 12
STYLE_MONEY_TOTAL = 13
STYLE_PERCENT = 14
STYLE_PERCENT_ZEBRA = 15
STYLE_PERCENT_TOTAL = 16
STYLE_DATE = 17
STYLE_DATE_ZEBRA = 18
STYLE_QTY_E8 = 19
STYLE_QTY_E8_ZEBRA = 20
STYLE_PRICE_E8 = 21
STYLE_PRICE_E8_ZEBRA = 22
STYLE_INT = 23
STYLE_INT_ZEBRA = 24
STYLE_INT_TOTAL = 25
STYLE_BADGE_GOOD = 26
STYLE_BADGE_WARN = 27
STYLE_BADGE_ALERT = 28
STYLE_MONEY_NEG = 29
STYLE_MONEY_POS = 30
STYLE_MONEY_NONUSD = 31
STYLE_MONEY_NONUSD_ZEBRA = 32
STYLE_KPI_TEXT = 33
STYLE_TOTAL_LABEL = 34


def _build_styles_xml() -> str:
    """Build ECMA-376 compliant `xl/styles.xml` with Dark-Industrial Slate & Emerald theme."""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<numFmts count="7">'
        '<numFmt numFmtId="164" formatCode="$#,##0.00;-$#,##0.00;$0.00"/>'
        '<numFmt numFmtId="165" formatCode="0.0%"/>'
        '<numFmt numFmtId="166" formatCode="yyyy-mm-dd"/>'
        '<numFmt numFmtId="167" formatCode="#,##0.########"/>'
        '<numFmt numFmtId="168" formatCode="$#,##0.00######"/>'
        '<numFmt numFmtId="169" formatCode="#,##0"/>'
        '<numFmt numFmtId="170" formatCode="#,##0.00;-#,##0.00;0.00"/>'
        "</numFmts>"
        '<fonts count="10">'
        # 0: Default body (Slate 900)
        '<font><sz val="10"/><color rgb="FF0F172A"/><name val="Helvetica Neue"/><family val="2"/></font>'
        # 1: Banner title (15pt bold white)
        '<font><b/><sz val="15"/><color rgb="FFFFFFFF"/><name val="Helvetica Neue"/><family val="2"/></font>'
        # 2: Banner subtitle (9.5pt italic emerald accent)
        '<font><i/><sz val="9.5"/><color rgb="FF34D399"/><name val="Helvetica Neue"/><family val="2"/></font>'
        # 3: Section & table header (10pt bold white)
        '<font><b/><sz val="10"/><color rgb="FFFFFFFF"/><name val="Helvetica Neue"/><family val="2"/></font>'
        # 4: Body bold (Slate 900)
        '<font><b/><sz val="10"/><color rgb="FF0F172A"/><name val="Helvetica Neue"/><family val="2"/></font>'
        # 5: KPI label (9pt bold Slate 600)
        '<font><b/><sz val="9"/><color rgb="FF475569"/><name val="Helvetica Neue"/><family val="2"/></font>'
        # 6: KPI value (12pt bold Slate 900)
        '<font><b/><sz val="12"/><color rgb="FF0F172A"/><name val="Helvetica Neue"/><family val="2"/></font>'
        # 7: KPI / Positive Emerald (11pt bold Emerald 700)
        '<font><b/><sz val="11"/><color rgb="FF047857"/><name val="Helvetica Neue"/><family val="2"/></font>'
        # 8: Amber warning (10pt bold Amber 800)
        '<font><b/><sz val="10"/><color rgb="FF92400E"/><name val="Helvetica Neue"/><family val="2"/></font>'
        # 9: Rose negative / alert (10pt bold Rose 700)
        '<font><b/><sz val="10"/><color rgb="FFBE123C"/><name val="Helvetica Neue"/><family val="2"/></font>'
        "</fonts>"
        '<fills count="10">'
        # 0 & 1: Mandatory OOXML built-in fills
        '<fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        # 2: Deep Slate 900 (Title banner)
        '<fill><patternFill patternType="solid"><fgColor rgb="FF0F172A"/><bgColor indexed="64"/></patternFill></fill>'
        # 3: Slate 800 (Subtitle / Section header)
        '<fill><patternFill patternType="solid"><fgColor rgb="FF1E293B"/><bgColor indexed="64"/></patternFill></fill>'
        # 4: Emerald 800 (Table header accent)
        '<fill><patternFill patternType="solid"><fgColor rgb="FF065F46"/><bgColor indexed="64"/></patternFill></fill>'
        # 5: Slate 100 (Zebra / KPI label)
        '<fill><patternFill patternType="solid"><fgColor rgb="FFF1F5F9"/><bgColor indexed="64"/></patternFill></fill>'
        # 6: Emerald 50 (KPI emerald highlight / good badge)
        '<fill><patternFill patternType="solid"><fgColor rgb="FFECFDF5"/><bgColor indexed="64"/></patternFill></fill>'
        # 7: Amber 100 (Watch badge)
        '<fill><patternFill patternType="solid"><fgColor rgb="FFFEF3C7"/><bgColor indexed="64"/></patternFill></fill>'
        # 8: Rose 100 (Alert badge)
        '<fill><patternFill patternType="solid"><fgColor rgb="FFFFE4E6"/><bgColor indexed="64"/></patternFill></fill>'
        # 9: Slate 200 (Totals row)
        '<fill><patternFill patternType="solid"><fgColor rgb="FFE2E8F0"/><bgColor indexed="64"/></patternFill></fill>'
        "</fills>"
        '<borders count="4">'
        # 0: None
        "<border><left/><right/><top/><bottom/><diagonal/></border>"
        # 1: Subtle grid cell border (Slate 300)
        '<border>'
        '<left style="thin"><color rgb="FFCBD5E1"/></left>'
        '<right style="thin"><color rgb="FFCBD5E1"/></right>'
        '<top style="thin"><color rgb="FFCBD5E1"/></top>'
        '<bottom style="thin"><color rgb="FFCBD5E1"/></bottom>'
        "<diagonal/></border>"
        # 2: Totals row border (top thin Slate 700, bottom double Slate 900)
        '<border>'
        '<left style="thin"><color rgb="FFCBD5E1"/></left>'
        '<right style="thin"><color rgb="FFCBD5E1"/></right>'
        '<top style="thin"><color rgb="FF334155"/></top>'
        '<bottom style="double"><color rgb="FF0F172A"/></bottom>'
        "<diagonal/></border>"
        # 3: Header bottom accent (Emerald 500)
        '<border>'
        '<left style="thin"><color rgb="FF1E293B"/></left>'
        '<right style="thin"><color rgb="FF1E293B"/></right>'
        '<top style="thin"><color rgb="FF1E293B"/></top>'
        '<bottom style="medium"><color rgb="FF10B981"/></bottom>'
        "<diagonal/></border>"
        "</borders>"
        '<cellStyleXfs count="1">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>'
        "</cellStyleXfs>"
        '<cellXfs count="35">'
        # 0: DEFAULT
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        # 1: BANNER_TITLE
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1">'
        '<alignment horizontal="left" vertical="center" indent="1"/></xf>'
        # 2: BANNER_SUBTITLE
        '<xf numFmtId="0" fontId="2" fillId="3" borderId="3" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="left" vertical="center" indent="1"/></xf>'
        # 3: SECTION_HEADER
        '<xf numFmtId="0" fontId="3" fillId="3" borderId="3" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="left" vertical="center" indent="1"/></xf>'
        # 4: TABLE_HEADER
        '<xf numFmtId="0" fontId="3" fillId="4" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="center" vertical="center" wrapText="1"/></xf>'
        # 5: TEXT
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="left" vertical="center"/></xf>'
        # 6: TEXT_ZEBRA
        '<xf numFmtId="0" fontId="0" fillId="5" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="left" vertical="center"/></xf>'
        # 7: TEXT_BOLD
        '<xf numFmtId="0" fontId="4" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="left" vertical="center"/></xf>'
        # 8: KPI_LABEL
        '<xf numFmtId="0" fontId="5" fillId="5" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="left" vertical="center" indent="1"/></xf>'
        # 9: KPI_MONEY
        '<xf numFmtId="164" fontId="6" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="right" vertical="center"/></xf>'
        # 10: KPI_MONEY_EMERALD
        '<xf numFmtId="164" fontId="7" fillId="6" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="right" vertical="center"/></xf>'
        # 11: MONEY
        '<xf numFmtId="164" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="right" vertical="center"/></xf>'
        # 12: MONEY_ZEBRA
        '<xf numFmtId="164" fontId="0" fillId="5" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="right" vertical="center"/></xf>'
        # 13: MONEY_TOTAL
        '<xf numFmtId="164" fontId="4" fillId="9" borderId="2" xfId="0" applyNumberFormat="1" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="right" vertical="center"/></xf>'
        # 14: PERCENT
        '<xf numFmtId="165" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="right" vertical="center"/></xf>'
        # 15: PERCENT_ZEBRA
        '<xf numFmtId="165" fontId="0" fillId="5" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="right" vertical="center"/></xf>'
        # 16: PERCENT_TOTAL
        '<xf numFmtId="165" fontId="4" fillId="9" borderId="2" xfId="0" applyNumberFormat="1" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="right" vertical="center"/></xf>'
        # 17: DATE
        '<xf numFmtId="166" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="center" vertical="center"/></xf>'
        # 18: DATE_ZEBRA
        '<xf numFmtId="166" fontId="0" fillId="5" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="center" vertical="center"/></xf>'
        # 19: QTY_E8
        '<xf numFmtId="167" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="right" vertical="center"/></xf>'
        # 20: QTY_E8_ZEBRA
        '<xf numFmtId="167" fontId="0" fillId="5" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="right" vertical="center"/></xf>'
        # 21: PRICE_E8
        '<xf numFmtId="168" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="right" vertical="center"/></xf>'
        # 22: PRICE_E8_ZEBRA
        '<xf numFmtId="168" fontId="0" fillId="5" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="right" vertical="center"/></xf>'
        # 23: INT
        '<xf numFmtId="169" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="right" vertical="center"/></xf>'
        # 24: INT_ZEBRA
        '<xf numFmtId="169" fontId="0" fillId="5" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="right" vertical="center"/></xf>'
        # 25: INT_TOTAL
        '<xf numFmtId="169" fontId="4" fillId="9" borderId="2" xfId="0" applyNumberFormat="1" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="right" vertical="center"/></xf>'
        # 26: BADGE_GOOD
        '<xf numFmtId="0" fontId="7" fillId="6" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="center" vertical="center"/></xf>'
        # 27: BADGE_WARN
        '<xf numFmtId="0" fontId="8" fillId="7" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="center" vertical="center"/></xf>'
        # 28: BADGE_ALERT
        '<xf numFmtId="0" fontId="9" fillId="8" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="center" vertical="center"/></xf>'
        # 29: MONEY_NEG
        '<xf numFmtId="164" fontId="9" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="right" vertical="center"/></xf>'
        # 30: MONEY_POS
        '<xf numFmtId="164" fontId="7" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="right" vertical="center"/></xf>'
        # 31: MONEY_NONUSD
        '<xf numFmtId="170" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="right" vertical="center"/></xf>'
        # 32: MONEY_NONUSD_ZEBRA
        '<xf numFmtId="170" fontId="0" fillId="5" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="right" vertical="center"/></xf>'
        # 33: KPI_TEXT
        '<xf numFmtId="0" fontId="4" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="right" vertical="center"/></xf>'
        # 34: TOTAL_LABEL
        '<xf numFmtId="0" fontId="4" fillId="9" borderId="2" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="left" vertical="center"/></xf>'
        "</cellXfs>"
        '<cellStyles count="1">'
        '<cellStyle name="Normal" xfId="0" builtinId="0"/>'
        "</cellStyles>"
        "</styleSheet>"
    )


@dataclass(frozen=True)
class _CellEntry:
    kind: str  # "str" | "num" | "formula"
    value: str
    style: int
    formula: str = ""


class _WorksheetBuilder:
    """Strict ECMA-376 worksheet XML builder preserving row/column order."""

    def __init__(
        self,
        name: str,
        shared_strings: _SharedStrings,
        *,
        tab_selected: bool = False,
        freeze_row: int = 0,
    ) -> None:
        self.name = name
        self._sst = shared_strings
        self._tab_selected = tab_selected
        self._freeze_row = freeze_row
        self._rows: Dict[int, Dict[int, _CellEntry]] = {}
        self._row_heights: Dict[int, float] = {}
        self._col_widths: Dict[int, float] = {}
        self._merges: List[str] = []
        self._drawing_rid: Optional[str] = None
        self._max_row = 1
        self._max_col = 1

    def set_col_widths(self, widths: Sequence[float]) -> None:
        for idx, width in enumerate(widths, start=1):
            self._col_widths[idx] = float(width)

    def set_row_height(self, row_idx: int, height: float) -> None:
        self._row_heights[row_idx] = float(height)

    def set_drawing_rid(self, rid: str) -> None:
        self._drawing_rid = rid

    def _put(self, row_idx: int, col_idx: int, entry: _CellEntry) -> None:
        self._rows.setdefault(row_idx, {})[col_idx] = entry
        if row_idx > self._max_row:
            self._max_row = row_idx
        if col_idx > self._max_col:
            self._max_col = col_idx

    def write_text(self, row_idx: int, col_idx: int, text: object, style: int = STYLE_TEXT) -> None:
        sst_idx = self._sst.add(sanitize_xml_text(text))
        self._put(row_idx, col_idx, _CellEntry(kind="str", value=str(sst_idx), style=style))

    def write_sanitized_text(self, row_idx: int, col_idx: int, text: object, style: int = STYLE_TEXT) -> None:
        sst_idx = self._sst.add(sanitize_sheet_text(text))
        self._put(row_idx, col_idx, _CellEntry(kind="str", value=str(sst_idx), style=style))

    def write_num(self, row_idx: int, col_idx: int, numeric_str: str, style: int) -> None:
        self._put(row_idx, col_idx, _CellEntry(kind="num", value=numeric_str, style=style))

    def write_money(self, row_idx: int, col_idx: int, cents: int, style: int = STYLE_MONEY) -> None:
        self.write_num(row_idx, col_idx, cents_to_decimal_str(cents), style=style)

    def write_percent(self, row_idx: int, col_idx: int, pct_value: float, style: int = STYLE_PERCENT) -> None:
        self.write_num(row_idx, col_idx, ratio_to_decimal_str(pct_value), style=style)

    def write_date(
        self,
        row_idx: int,
        col_idx: int,
        iso_date: str,
        style: int = STYLE_DATE,
        *,
        fallback_style: Optional[int] = None,
    ) -> None:
        serial = iso_date_to_excel_serial(iso_date)
        if serial is not None:
            self.write_num(row_idx, col_idx, str(serial), style=style)
        else:
            fallback = sanitize_sheet_text(iso_date) if iso_date else "—"
            fb_style = fallback_style if fallback_style is not None else (
                STYLE_TEXT_ZEBRA if style == STYLE_DATE_ZEBRA else STYLE_TEXT
            )
            self.write_text(row_idx, col_idx, fallback, style=fb_style)

    def write_formula(
        self,
        row_idx: int,
        col_idx: int,
        formula: str,
        cached_value: str,
        style: int,
    ) -> None:
        clean_formula = formula.lstrip("=")
        self._put(
            row_idx,
            col_idx,
            _CellEntry(kind="formula", value=cached_value, style=style, formula=clean_formula),
        )

    def write_banner(self, title: str, subtitle: str, col_span: int) -> None:
        self.write_text(1, 1, title, style=STYLE_BANNER_TITLE)
        for c in range(2, col_span + 1):
            self.write_text(1, c, "", style=STYLE_BANNER_TITLE)
        self.merge_cells(1, 1, 1, col_span)
        self.set_row_height(1, 30.0)

        self.write_text(2, 1, subtitle, style=STYLE_BANNER_SUBTITLE)
        for c in range(2, col_span + 1):
            self.write_text(2, c, "", style=STYLE_BANNER_SUBTITLE)
        self.merge_cells(2, 1, 2, col_span)
        self.set_row_height(2, 20.0)
        self.set_row_height(3, 10.0)

    def write_section_header(self, row_idx: int, title: str, start_col: int, end_col: int) -> None:
        self.write_text(row_idx, start_col, title, style=STYLE_SECTION_HEADER)
        for c in range(start_col + 1, end_col + 1):
            self.write_text(row_idx, c, "", style=STYLE_SECTION_HEADER)
        if end_col > start_col:
            self.merge_cells(row_idx, start_col, row_idx, end_col)
        self.set_row_height(row_idx, 22.0)

    def write_table_headers(self, row_idx: int, headers: Sequence[str], start_col: int = 1) -> None:
        for offset, label in enumerate(headers):
            self.write_text(row_idx, start_col + offset, label, style=STYLE_TABLE_HEADER)
        self.set_row_height(row_idx, 22.0)

    def merge_cells(self, r1: int, c1: int, r2: int, c2: int) -> None:
        ref = f"{cell_ref(r1, c1)}:{cell_ref(r2, c2)}"
        self._merges.append(ref)
        if r2 > self._max_row:
            self._max_row = r2
        if c2 > self._max_col:
            self._max_col = c2

    def to_xml(self) -> str:
        dim = f"A1:{cell_ref(self._max_row, self._max_col)}"
        tab_attr = ' tabSelected="1"' if self._tab_selected else ""
        parts: List[str] = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            (
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            ),
            f'<dimension ref="{dim}"/>',
            "<sheetViews>",
            f'<sheetView{tab_attr} workbookViewId="0" showGridLines="1">',
        ]
        if self._freeze_row > 0:
            parts.append(
                f'<pane ySplit="{self._freeze_row}" topLeftCell="A{self._freeze_row + 1}" '
                'activePane="bottomLeft" state="frozen"/>'
            )
        parts.append("</sheetView></sheetViews>")
        parts.append('<sheetFormatPr defaultRowHeight="19"/>')

        if self._col_widths:
            parts.append("<cols>")
            for col_idx in sorted(self._col_widths):
                w = self._col_widths[col_idx]
                parts.append(f'<col min="{col_idx}" max="{col_idx}" width="{w:.2f}" customWidth="1"/>')
            parts.append("</cols>")

        parts.append("<sheetData>")
        for row_idx in sorted(self._rows):
            ht = self._row_heights.get(row_idx)
            ht_attr = f' ht="{ht:.1f}" customHeight="1"' if ht is not None else ""
            parts.append(f'<row r="{row_idx}"{ht_attr}>')
            row_cells = self._rows[row_idx]
            for col_idx in sorted(row_cells):
                entry = row_cells[col_idx]
                ref = cell_ref(row_idx, col_idx)
                if entry.kind == "str":
                    parts.append(f'<c r="{ref}" s="{entry.style}" t="s"><v>{entry.value}</v></c>')
                elif entry.kind == "num":
                    parts.append(f'<c r="{ref}" s="{entry.style}"><v>{entry.value}</v></c>')
                elif entry.kind == "formula":
                    parts.append(
                        f'<c r="{ref}" s="{entry.style}">'
                        f"<f>{_xml_escape(entry.formula)}</f>"
                        f"<v>{_xml_escape(entry.value)}</v></c>"
                    )
            parts.append("</row>")
        parts.append("</sheetData>")

        if self._merges:
            parts.append(f'<mergeCells count="{len(self._merges)}">')
            for ref in self._merges:
                parts.append(f'<mergeCell ref="{ref}"/>')
            parts.append("</mergeCells>")

        parts.append('<pageMargins left="0.5" right="0.5" top="0.6" bottom="0.6" header="0.3" footer="0.3"/>')

        if self._drawing_rid:
            parts.append(f'<drawing r:id="{self._drawing_rid}"/>')

        parts.append("</worksheet>")
        return "".join(parts)


# --- Native OOXML DrawingML Chart Builders ---


@dataclass(frozen=True)
class _ChartSeriesSpec:
    name: str
    name_cell_ref: str
    val_range_ref: str
    values: Tuple[str, ...]
    color_hex: str
    num_format_code: str = "$#,##0.00;-$#,##0.00;$0.00"


@dataclass(frozen=True)
class _ChartAnchorSpec:
    chart_index: int  # 1-based global chart file index (chart1.xml .. chart5.xml)
    rid: str  # drawing-local rId1, rId2
    shape_id: int  # >= 2 unique per drawing
    name: str
    from_col: int  # 0-indexed
    from_row: int  # 0-indexed
    to_col: int  # 0-indexed (> from_col)
    to_row: int  # 0-indexed (> from_row)


def _chart_title_xml(title: str) -> str:
    safe_title = _xml_escape(sanitize_xml_text(title))
    return (
        "<c:title>"
        "<c:tx><c:rich>"
        "<a:bodyPr/>"
        "<a:lstStyle/>"
        "<a:p>"
        '<a:pPr><a:defRPr sz="1200" b="1"><a:solidFill><a:srgbClr val="0F172A"/></a:solidFill>'
        '<a:latin typeface="Helvetica Neue"/></a:defRPr></a:pPr>'
        f'<a:r><a:rPr lang="en-US" sz="1200" b="1"><a:solidFill><a:srgbClr val="0F172A"/></a:solidFill>'
        f'<a:latin typeface="Helvetica Neue"/></a:rPr><a:t>{safe_title}</a:t></a:r>'
        "</a:p>"
        "</c:rich></c:tx>"
        "<c:layout/>"
        '<c:overlay val="0"/>'
        "</c:title>"
    )


def _chart_str_cache_xml(values: Sequence[str]) -> str:
    pts = [
        f'<c:pt idx="{idx}"><c:v>{_xml_escape(sanitize_xml_text(val))}</c:v></c:pt>'
        for idx, val in enumerate(values)
    ]
    return f'<c:strCache><c:ptCount val="{len(values)}"/>{"".join(pts)}</c:strCache>'


def _chart_num_cache_xml(values: Sequence[str], format_code: str) -> str:
    pts = [f'<c:pt idx="{idx}"><c:v>{_xml_escape(val)}</c:v></c:pt>' for idx, val in enumerate(values)]
    return (
        "<c:numCache>"
        f"<c:formatCode>{_xml_escape(format_code)}</c:formatCode>"
        f'<c:ptCount val="{len(values)}"/>'
        f'{"".join(pts)}'
        "</c:numCache>"
    )


def _chart_cat_xml(
    cat_range_ref: str,
    categories: Sequence[str],
    *,
    is_numeric_date: bool = False,
) -> str:
    if is_numeric_date:
        return (
            "<c:cat><c:numRef>"
            f"<c:f>{_xml_escape(cat_range_ref)}</c:f>"
            f'{_chart_num_cache_xml(categories, "yyyy-mm-dd")}'
            "</c:numRef></c:cat>"
        )
    return (
        "<c:cat><c:strRef>"
        f"<c:f>{_xml_escape(cat_range_ref)}</c:f>"
        f"{_chart_str_cache_xml(categories)}"
        "</c:strRef></c:cat>"
    )


def _chart_series_tx_xml(ser: _ChartSeriesSpec) -> str:
    return (
        "<c:tx><c:strRef>"
        f"<c:f>{_xml_escape(ser.name_cell_ref)}</c:f>"
        f"{_chart_str_cache_xml([ser.name])}"
        "</c:strRef></c:tx>"
    )


def _chart_series_val_xml(ser: _ChartSeriesSpec) -> str:
    return (
        "<c:val><c:numRef>"
        f"<c:f>{_xml_escape(ser.val_range_ref)}</c:f>"
        f"{_chart_num_cache_xml(ser.values, ser.num_format_code)}"
        "</c:numRef></c:val>"
    )


def _chart_axes_xml(cat_ax_id: int, val_ax_id: int, *, cat_num_fmt: Optional[str] = None) -> str:
    cat_fmt_xml = (
        f'<c:numFmt formatCode="{_xml_escape(cat_num_fmt)}" sourceLinked="0"/>'
        if cat_num_fmt
        else ""
    )
    return (
        "<c:catAx>"
        f'<c:axId val="{cat_ax_id}"/>'
        '<c:scaling><c:orientation val="minMax"/></c:scaling>'
        '<c:delete val="0"/>'
        '<c:axPos val="b"/>'
        f"{cat_fmt_xml}"
        '<c:majorTickMark val="none"/>'
        '<c:minorTickMark val="none"/>'
        '<c:tickLblPos val="nextTo"/>'
        '<c:spPr><a:ln w="9525"><a:solidFill><a:srgbClr val="CBD5E1"/></a:solidFill></a:ln></c:spPr>'
        f'<c:crossAx val="{val_ax_id}"/>'
        '<c:crosses val="autoZero"/>'
        '<c:auto val="1"/>'
        '<c:lblAlgn val="ctr"/>'
        '<c:lblOffset val="100"/>'
        "</c:catAx>"
        "<c:valAx>"
        f'<c:axId val="{val_ax_id}"/>'
        '<c:scaling><c:orientation val="minMax"/></c:scaling>'
        '<c:delete val="0"/>'
        '<c:axPos val="l"/>'
        '<c:majorGridlines><c:spPr><a:ln w="6350"><a:solidFill><a:srgbClr val="E2E8F0"/></a:solidFill></a:ln></c:spPr></c:majorGridlines>'
        '<c:numFmt formatCode="$#,##0;-$#,##0;$0" sourceLinked="0"/>'
        '<c:majorTickMark val="none"/>'
        '<c:minorTickMark val="none"/>'
        '<c:tickLblPos val="nextTo"/>'
        '<c:spPr><a:ln><a:noFill/></a:ln></c:spPr>'
        f'<c:crossAx val="{cat_ax_id}"/>'
        '<c:crosses val="autoZero"/>'
        '<c:crossBetween val="between"/>'
        "</c:valAx>"
    )


def _wrap_chart_space(title: str, plot_body_xml: str, *, legend_pos: str = "b") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<c:chartSpace '
        'xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<c:date1904 val="0"/>'
        '<c:lang val="en-US"/>'
        '<c:roundedCorners val="0"/>'
        "<c:chart>"
        f"{_chart_title_xml(title)}"
        '<c:autoTitleDeleted val="0"/>'
        "<c:plotArea>"
        "<c:layout/>"
        f"{plot_body_xml}"
        "<c:spPr>"
        '<a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill>'
        "<a:ln><a:noFill/></a:ln>"
        "</c:spPr>"
        "</c:plotArea>"
        "<c:legend>"
        f'<c:legendPos val="{legend_pos}"/>'
        '<c:overlay val="0"/>'
        "</c:legend>"
        '<c:plotVisOnly val="1"/>'
        '<c:dispBlanksAs val="gap"/>'
        "</c:chart>"
        "<c:spPr>"
        '<a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill>'
        '<a:ln w="9525"><a:solidFill><a:srgbClr val="CBD5E1"/></a:solidFill></a:ln>'
        "</c:spPr>"
        "</c:chartSpace>"
    )


def _build_bar_chart_xml(
    *,
    title: str,
    cat_range_ref: str,
    categories: Sequence[str],
    series: Sequence[_ChartSeriesSpec],
    cat_ax_id: int,
    val_ax_id: int,
) -> str:
    ser_parts: List[str] = []
    for idx, ser in enumerate(series):
        ser_parts.append(
            "<c:ser>"
            f'<c:idx val="{idx}"/>'
            f'<c:order val="{idx}"/>'
            f"{_chart_series_tx_xml(ser)}"
            "<c:spPr>"
            f'<a:solidFill><a:srgbClr val="{ser.color_hex}"/></a:solidFill>'
            "<a:ln><a:noFill/></a:ln>"
            "</c:spPr>"
            '<c:invertIfNegative val="0"/>'
            f"{_chart_cat_xml(cat_range_ref, categories)}"
            f"{_chart_series_val_xml(ser)}"
            "</c:ser>"
        )
    plot_body = (
        "<c:barChart>"
        '<c:barDir val="col"/>'
        '<c:grouping val="clustered"/>'
        '<c:varyColors val="0"/>'
        f'{"".join(ser_parts)}'
        '<c:gapWidth val="120"/>'
        '<c:overlap val="-15"/>'
        f'<c:axId val="{cat_ax_id}"/>'
        f'<c:axId val="{val_ax_id}"/>'
        "</c:barChart>"
        f"{_chart_axes_xml(cat_ax_id, val_ax_id)}"
    )
    return _wrap_chart_space(title, plot_body, legend_pos="b")


def _build_line_chart_xml(
    *,
    title: str,
    cat_range_ref: str,
    categories: Sequence[str],
    series: Sequence[_ChartSeriesSpec],
    cat_ax_id: int,
    val_ax_id: int,
    is_numeric_date: bool = False,
) -> str:
    ser_parts: List[str] = []
    for idx, ser in enumerate(series):
        ser_parts.append(
            "<c:ser>"
            f'<c:idx val="{idx}"/>'
            f'<c:order val="{idx}"/>'
            f"{_chart_series_tx_xml(ser)}"
            "<c:spPr>"
            f'<a:ln w="25400" cap="rnd"><a:solidFill><a:srgbClr val="{ser.color_hex}"/></a:solidFill></a:ln>'
            "</c:spPr>"
            '<c:marker><c:symbol val="circle"/><c:size val="4"/>'
            f'<c:spPr><a:solidFill><a:srgbClr val="{ser.color_hex}"/></a:solidFill></c:spPr>'
            "</c:marker>"
            f"{_chart_cat_xml(cat_range_ref, categories, is_numeric_date=is_numeric_date)}"
            f"{_chart_series_val_xml(ser)}"
            '<c:smooth val="0"/>'
            "</c:ser>"
        )
    plot_body = (
        "<c:lineChart>"
        '<c:grouping val="standard"/>'
        '<c:varyColors val="0"/>'
        f'{"".join(ser_parts)}'
        '<c:marker val="1"/>'
        '<c:smooth val="0"/>'
        f'<c:axId val="{cat_ax_id}"/>'
        f'<c:axId val="{val_ax_id}"/>'
        "</c:lineChart>"
        f'{_chart_axes_xml(cat_ax_id, val_ax_id, cat_num_fmt="yyyy-mm-dd" if is_numeric_date else None)}'
    )
    return _wrap_chart_space(title, plot_body, legend_pos="b")


def _build_radial_chart_xml(
    *,
    chart_tag: str,  # "doughnutChart" or "pieChart"
    title: str,
    cat_range_ref: str,
    categories: Sequence[str],
    series: _ChartSeriesSpec,
) -> str:
    dpt_parts: List[str] = []
    for idx in range(len(categories)):
        color = _CHART_PALETTE[idx % len(_CHART_PALETTE)]
        dpt_parts.append(
            "<c:dPt>"
            f'<c:idx val="{idx}"/>'
            '<c:bubble3D val="0"/>'
            "<c:spPr>"
            f'<a:solidFill><a:srgbClr val="{color}"/></a:solidFill>'
            '<a:ln w="12700"><a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill></a:ln>'
            "</c:spPr>"
            "</c:dPt>"
        )
    hole_xml = '<c:holeSize val="55"/>' if chart_tag == "doughnutChart" else ""
    plot_body = (
        f"<c:{chart_tag}>"
        '<c:varyColors val="1"/>'
        "<c:ser>"
        '<c:idx val="0"/>'
        '<c:order val="0"/>'
        f"{_chart_series_tx_xml(series)}"
        f'{"".join(dpt_parts)}'
        f"{_chart_cat_xml(cat_range_ref, categories)}"
        f"{_chart_series_val_xml(series)}"
        "</c:ser>"
        "<c:dLbls>"
        '<c:showLegendKey val="0"/>'
        '<c:showVal val="0"/>'
        '<c:showCatName val="0"/>'
        '<c:showSerName val="0"/>'
        '<c:showPercent val="1"/>'
        '<c:showBubbleSize val="0"/>'
        "</c:dLbls>"
        '<c:firstSliceAng val="0"/>'
        f"{hole_xml}"
        f"</c:{chart_tag}>"
    )
    return _wrap_chart_space(title, plot_body, legend_pos="r")


def _build_drawing_xml(anchors: Sequence[_ChartAnchorSpec]) -> str:
    anchor_parts: List[str] = []
    for anc in anchors:
        if anc.to_col <= anc.from_col or anc.to_row <= anc.from_row:
            raise ValueError(f"Invalid twoCellAnchor geometry for {anc.name}")
        anchor_parts.append(
            "<xdr:twoCellAnchor>"
            "<xdr:from>"
            f"<xdr:col>{anc.from_col}</xdr:col><xdr:colOff>0</xdr:colOff>"
            f"<xdr:row>{anc.from_row}</xdr:row><xdr:rowOff>0</xdr:rowOff>"
            "</xdr:from>"
            "<xdr:to>"
            f"<xdr:col>{anc.to_col}</xdr:col><xdr:colOff>0</xdr:colOff>"
            f"<xdr:row>{anc.to_row}</xdr:row><xdr:rowOff>0</xdr:rowOff>"
            "</xdr:to>"
            '<xdr:graphicFrame macro="">'
            "<xdr:nvGraphicFramePr>"
            f'<xdr:cNvPr id="{anc.shape_id}" name={_xml_quoteattr(anc.name)}/>'
            "<xdr:cNvGraphicFramePr/>"
            "</xdr:nvGraphicFramePr>"
            "<xdr:xfrm>"
            '<a:off x="0" y="0"/>'
            '<a:ext cx="0" cy="0"/>'
            "</xdr:xfrm>"
            "<a:graphic>"
            '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/chart">'
            f'<c:chart r:id="{anc.rid}"/>'
            "</a:graphicData>"
            "</a:graphic>"
            "</xdr:graphicFrame>"
            "<xdr:clientData/>"
            "</xdr:twoCellAnchor>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<xdr:wsDr '
        'xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart">'
        f'{"".join(anchor_parts)}'
        "</xdr:wsDr>"
    )


def _build_rels_xml(entries: Sequence[Tuple[str, str, str]]) -> str:
    """Build a `.rels` file from `(rid, rel_type, target)` tuples."""
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">',
    ]
    for rid, rel_type, target in entries:
        parts.append(
            f'<Relationship Id={_xml_quoteattr(rid)} '
            f"Type={_xml_quoteattr(rel_type)} "
            f"Target={_xml_quoteattr(target)}/>"
        )
    parts.append("</Relationships>")
    return "".join(parts)


# --- Apple Numbers Optional macOS Native Conversion Helper ---


def convert_to_native_numbers_if_macos(
    xlsx_bytes: bytes,
    *,
    filename: Optional[str] = None,
    timeout_seconds: int = 45,
) -> ExportArtifact:
    """
    Convert an OOXML `.xlsx` graphical workbook to a native `.numbers` package
    ONLY on macOS (`sys.platform == "darwin"`) when `/Applications/Numbers.app`
    and `osascript` are both available.

    Honesty guarantee:
    - Never renames `.xlsx` bytes to `.numbers`.
    - On Linux, Windows, macOS without Numbers.app, or if AppleScript conversion fails,
      returns the `.xlsx` workbook transparently with `format="xlsx"` and
      `native_numbers_converted=False`.
    """
    base_xlsx = filename or f"chronox-executive-report-{date.today().isoformat()}.xlsx"
    if not base_xlsx.lower().endswith(".xlsx"):
        base_xlsx = f"{Path(base_xlsx).stem}.xlsx"
    stem = base_xlsx[:-5]

    fallback_notes: List[str] = []
    if sys.platform != "darwin":
        fallback_notes.append("Native .numbers conversion requires macOS; returning Apple Numbers-ready .xlsx workbook.")
        return ExportArtifact(
            filename=base_xlsx,
            media_type=XLSX_MEDIA_TYPE,
            content=xlsx_bytes,
            format="xlsx",
            generated_at=_utc_now_iso(),
            native_numbers_converted=False,
            notes=tuple(fallback_notes),
        )

    numbers_app = Path("/Applications/Numbers.app")
    osascript_bin = shutil.which("osascript")
    if not numbers_app.exists() or not osascript_bin:
        fallback_notes.append(
            "Apple Numbers.app or osascript not available on this macOS host; returning .xlsx workbook."
        )
        return ExportArtifact(
            filename=base_xlsx,
            media_type=XLSX_MEDIA_TYPE,
            content=xlsx_bytes,
            format="xlsx",
            generated_at=_utc_now_iso(),
            native_numbers_converted=False,
            notes=tuple(fallback_notes),
        )

    applescript = (
        "on run argv\n"
        "  set inFile to POSIX file (item 1 of argv)\n"
        "  set outFile to POSIX file (item 2 of argv)\n"
        '  tell application "Numbers"\n'
        "    set doc to open inFile\n"
        "    save doc in outFile\n"
        "    close doc saving no\n"
        "  end tell\n"
        "end run\n"
    )

    try:
        with tempfile.TemporaryDirectory(prefix="chronox_numbers_") as tmpdir:
            in_path = Path(tmpdir) / base_xlsx
            out_path = Path(tmpdir) / f"{stem}.numbers"
            in_path.write_bytes(xlsx_bytes)

            proc = subprocess.run(  # noqa: S603
                [osascript_bin, "-e", applescript, str(in_path), str(out_path)],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            if proc.returncode != 0 or not out_path.exists():
                err_msg = (proc.stderr or proc.stdout or "AppleScript conversion failed").strip()
                fallback_notes.append(f"macOS Numbers conversion fallback ({err_msg[:180]}); returning .xlsx workbook.")
                return ExportArtifact(
                    filename=base_xlsx,
                    media_type=XLSX_MEDIA_TYPE,
                    content=xlsx_bytes,
                    format="xlsx",
                    generated_at=_utc_now_iso(),
                    native_numbers_converted=False,
                    notes=tuple(fallback_notes),
                )

            if out_path.is_file():
                numbers_bytes = out_path.read_bytes()
            else:
                # Numbers package directory: archive into a single portable ZIP stream
                pkg_buf = io.BytesIO()
                with zipfile.ZipFile(pkg_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                    for root, _, files in os.walk(out_path):
                        for fname in sorted(files):
                            full = Path(root) / fname
                            arcname = str(full.relative_to(out_path)).replace(os.sep, "/")
                            zf.write(full, arcname)
                numbers_bytes = pkg_buf.getvalue()

            if numbers_bytes and numbers_bytes != xlsx_bytes:
                return ExportArtifact(
                    filename=f"{stem}.numbers",
                    media_type=NUMBERS_MEDIA_TYPE,
                    content=numbers_bytes,
                    format="numbers",
                    generated_at=_utc_now_iso(),
                    native_numbers_converted=True,
                    notes=("Converted via macOS Apple Numbers.app.",),
                )
    except (OSError, subprocess.SubprocessError) as exc:
        fallback_notes.append(f"macOS Numbers conversion skipped ({exc}); returning .xlsx workbook.")

    return ExportArtifact(
        filename=base_xlsx,
        media_type=XLSX_MEDIA_TYPE,
        content=xlsx_bytes,
        format="xlsx",
        generated_at=_utc_now_iso(),
        native_numbers_converted=False,
        notes=tuple(fallback_notes or ["Returning Apple Numbers-ready .xlsx workbook."]),
    )


# --- Main Exporter Facade ---


class FinanceExporter:
    """
    Pure-stdlib Executive CSV, CSV Pack, and Apple Numbers Graphical Workbook exporter.

    All methods accept clean typed `ExportSnapshot` objects or raw ChronoX domain
    models/mappings normalized via `FinanceExporter.build_snapshot(...)`.
    """

    # ---- Normalization / Snapshot Factory ----

    @classmethod
    def build_snapshot(
        cls,
        *,
        dashboard: Any = None,
        analytics: Any = None,
        portfolio: Any = None,
        credit_report: Any = None,
        quant_forecast: Any = None,
        onetap_plan: Any = None,
        accounts: Optional[Sequence[Any]] = None,
        jars: Optional[Sequence[Any]] = None,
        transactions: Optional[Sequence[Any]] = None,
        goals: Optional[Sequence[Any]] = None,
        bills: Optional[Sequence[Any]] = None,
        as_of_date: Optional[str] = None,
        currency: str = "USD",
        profile_name: str = "ChronoX Executive Financial Report",
    ) -> ExportSnapshot:
        """Normalize live ChronoX service/model objects or dicts into an immutable `ExportSnapshot`."""
        report_date = (as_of_date or date.today().isoformat())[:10]
        now_iso = _utc_now_iso()

        raw_accounts = accounts if accounts is not None else (_get_field(dashboard, "accounts", []) or [])
        norm_accounts = tuple(cls._normalize_account(a) for a in raw_accounts)
        acct_by_id = {a.id: a for a in norm_accounts if a.id}

        raw_jars = jars if jars is not None else (_get_field(dashboard, "jars", []) or [])
        norm_jars = tuple(cls._normalize_jar(j) for j in raw_jars)

        raw_txs = (
            transactions
            if transactions is not None
            else (_get_field(dashboard, "recent_transactions", []) or [])
        )
        norm_txs = tuple(cls._normalize_tx(tx, acct_by_id, currency) for tx in raw_txs)

        raw_goals = goals if goals is not None else (_get_field(dashboard, "goals", []) or [])
        norm_goals = tuple(cls._normalize_goal(g) for g in raw_goals)

        raw_bills = bills if bills is not None else (_get_field(dashboard, "upcoming_bills", []) or [])
        norm_bills = tuple(cls._normalize_bill(b) for b in raw_bills)

        # Portfolio holdings & allocations
        raw_holdings = _get_field(portfolio, "holdings", []) or []
        norm_holdings = tuple(cls._normalize_holding(h) for h in raw_holdings)
        raw_allocs = _get_field(portfolio, "allocations", []) or []
        if raw_allocs:
            counts_by_cls: Dict[str, int] = {}
            for h in norm_holdings:
                counts_by_cls[h.asset_class] = counts_by_cls.get(h.asset_class, 0) + 1
            norm_allocs = tuple(
                AllocationExportRow(
                    asset_class=str(_get_field(a, "asset_class", "equity") or "equity"),
                    value_cents=_as_int(_get_field(a, "value_cents", 0)),
                    pct=_as_float(_get_field(a, "pct", 0.0)),
                    holdings_count=_as_int(
                        _get_field(a, "holdings_count", counts_by_cls.get(str(_get_field(a, "asset_class", "")), 0))
                    ),
                )
                for a in raw_allocs
            )
        else:
            norm_allocs = cls._derive_allocations(norm_holdings)

        # Top expense categories
        raw_top_exp = (
            _get_field(analytics, "expense_distribution", None)
            or _get_field(analytics, "top_5_expenses", None)
            or []
        )
        if raw_top_exp:
            norm_top_exp = tuple(
                CategoryExpenseRow(
                    label=str(_get_field(item, "label", "Uncategorized") or "Uncategorized"),
                    amount_cents=abs(_as_int(_get_field(item, "amount_cents", 0))),
                    pct_of_expenses=_as_float(_get_field(item, "pct_of_expenses", 0.0)),
                    pct_of_income=_as_float(_get_field(item, "pct_of_income", 0.0)),
                    jar_slug=str(_get_field(item, "jar_slug", "") or ""),
                )
                for item in raw_top_exp
            )
        else:
            norm_top_exp = cls._derive_top_expenses(norm_txs)

        # Monthly cashflow trends
        raw_trends = _get_field(analytics, "monthly_trends", []) or []
        if raw_trends:
            norm_monthly = tuple(
                MonthlyCashflowRow(
                    month=str(_get_field(pt, "month", "") or ""),
                    income_cents=_as_int(_get_field(pt, "income_cents", 0)),
                    expense_cents=abs(_as_int(_get_field(pt, "expense_cents", 0))),
                    net_cents=(
                        _as_int(_get_field(pt, "surplus_cents"))
                        if _get_field(pt, "surplus_cents") is not None
                        else None
                    ),
                )
                for pt in raw_trends
                if _get_field(pt, "month")
            )
        else:
            norm_monthly = cls._derive_monthly_cashflow(norm_txs)

        # Cashflow forecast runway trajectory
        qf = quant_forecast if quant_forecast is not None else _get_field(dashboard, "quant_forecast", None)
        raw_points = (
            _get_field(onetap_plan, "runway_points", None)
            or _get_field(qf, "daily_points", None)
            or []
        )
        norm_runway = tuple(cls._normalize_runway_point(pt) for pt in raw_points)

        # Credit & Debt
        credit_scores, credit_factors, credit_lines, debt_strategies, disclaimer = cls._normalize_credit(
            credit_report
        )

        # KPIs
        sts = _get_field(dashboard, "safe_to_spend", None)
        liquid_cash = (
            _as_int(_get_field(sts, "total_liquid_cash_cents"))
            if sts is not None
            else sum(a.balance_cents for a in norm_accounts if a.is_liquid and a.account_type != "credit")
        )
        total_investments = (
            _as_int(_get_field(portfolio, "total_value_cents"))
            if portfolio is not None
            else sum(h.market_value_cents for h in norm_holdings)
        )
        total_debt = (
            _as_int(_get_field(credit_report, "total_debt_cents"))
            if credit_report is not None
            else sum(cl.balance_cents for cl in credit_lines)
        )
        if total_debt == 0:
            total_debt = sum(abs(a.balance_cents) for a in norm_accounts if a.account_type == "credit")

        net_worth = liquid_cash + total_investments - total_debt
        net_worth_series = _get_field(analytics, "net_worth_series", None) or []
        if net_worth_series:
            last_nw = _get_field(net_worth_series[-1], "net_worth_cents", None)
            if last_nw is not None:
                net_worth = _as_int(last_nw)

        # Statistical 95% Tail Cushion: prefer OneTap CVaR 95 buffer, else quant volatility buffer, else emergency buffer
        runway_obj = _get_field(onetap_plan, "runway", None)
        tail_cushion = _as_int(
            _get_field(onetap_plan, "cvar_95_buffer_cents", None)
            or _get_field(runway_obj, "cvar_95_buffer_cents", None)
            or _get_field(_get_field(qf, "volatility", None), "recommended_buffer_cents", None)
            or _get_field(sts, "emergency_buffer_cents", 0)
        )
        ruin_prob = (
            _get_field(onetap_plan, "ruin_probability_pct", None)
            if _get_field(onetap_plan, "ruin_probability_pct", None) is not None
            else _get_field(runway_obj, "ruin_probability_pct", None)
        )

        # 30D Inflow / Outflow
        inflow_30d, outflow_30d = cls._compute_30d_flows(
            norm_txs, report_date, dashboard=dashboard, analytics=analytics
        )

        kpis = ExecutiveKPIs(
            liquid_cash_cents=liquid_cash,
            safe_to_spend_today_cents=_as_int(_get_field(sts, "daily_safe_to_spend_cents", 0)),
            safe_until_payday_cents=_as_int(_get_field(sts, "safe_to_spend_cents", 0)),
            safe_to_invest_cap_cents=_as_int(_get_field(dashboard, "safe_to_invest_cents", 0)),
            net_worth_cents=net_worth,
            tail_cushion_95_cents=tail_cushion,
            inflow_30d_cents=inflow_30d,
            outflow_30d_cents=outflow_30d,
            net_flow_30d_cents=inflow_30d - outflow_30d,
            total_investments_cents=total_investments,
            total_debt_cents=total_debt,
            days_until_payday=_as_int(_get_field(sts, "days_until_payday", 0)),
            next_payday_date=str(_get_field(sts, "next_payday_date", "") or ""),
            ruin_probability_pct=float(ruin_prob) if ruin_prob is not None else None,
        )

        return ExportSnapshot(
            as_of_date=report_date,
            generated_at=now_iso,
            profile_currency=(currency or "USD").upper(),
            profile_name=profile_name,
            kpis=kpis,
            accounts=norm_accounts,
            jars=norm_jars,
            top_expenses=norm_top_exp,
            monthly_cashflow=norm_monthly,
            runway_points=norm_runway,
            holdings=norm_holdings,
            allocations=norm_allocs,
            credit_scores=credit_scores,
            credit_factors=credit_factors,
            credit_lines=credit_lines,
            debt_strategies=debt_strategies,
            credit_disclaimer=disclaimer,
            goals=norm_goals,
            bills=norm_bills,
            transactions=norm_txs,
        )

    @staticmethod
    def _normalize_account(obj: Any) -> AccountExportRow:
        if isinstance(obj, AccountExportRow):
            return obj
        avail = _get_field(obj, "available_cents", None)
        limit = _get_field(obj, "credit_limit_cents", None)
        return AccountExportRow(
            id=_as_int(_get_field(obj, "id", 0)),
            name=str(_get_field(obj, "name", "") or ""),
            account_type=str(_get_field(obj, "account_type", "checking") or "checking"),
            institution=str(_get_field(obj, "institution", "") or ""),
            currency=str(_get_field(obj, "currency", "USD") or "USD").upper(),
            balance_cents=_as_int(_get_field(obj, "balance_cents", 0)),
            available_cents=_as_int(avail) if avail is not None else None,
            credit_limit_cents=_as_int(limit) if limit is not None else None,
            is_liquid=_as_bool(_get_field(obj, "is_liquid", True), default=True),
            mask=str(_get_field(obj, "mask", "") or ""),
        )

    @staticmethod
    def _normalize_jar(obj: Any) -> JarExportRow:
        if isinstance(obj, JarExportRow):
            return obj
        allocated = _get_field(obj, "allocated_cents", None)
        if allocated is None:
            allocated = _get_field(obj, "auto_allocated_cents", None)
        if allocated is None:
            allocated = _get_field(obj, "funded_cents", 0)
        return JarExportRow(
            id=_as_int(_get_field(obj, "id", 0)),
            slug=str(_get_field(obj, "slug", "") or ""),
            name=str(_get_field(obj, "name", "") or ""),
            kind=str(_get_field(obj, "kind", "daily") or "daily"),
            target_pct=_as_float(_get_field(obj, "target_pct", 0.0)),
            monthly_target_cents=_as_int(_get_field(obj, "monthly_target_cents", 0)),
            allocated_cents=_as_int(allocated),
            available_cents=_as_int(_get_field(obj, "available_cents", 0)),
            spent_cents=_as_int(_get_field(obj, "spent_cents", 0)),
            rollover_cents=_as_int(_get_field(obj, "rollover_cents", 0)),
            pct_used=_as_float(_get_field(obj, "pct_used", 0.0)),
            health=str(_get_field(obj, "health", "calm") or "calm"),
            is_locked=_as_bool(_get_field(obj, "is_locked", False)),
        )

    @staticmethod
    def _normalize_tx(
        obj: Any,
        acct_by_id: Mapping[int, AccountExportRow],
        default_currency: str,
    ) -> TransactionExportRow:
        if isinstance(obj, TransactionExportRow):
            acct_name = obj.account_name
            curr = obj.currency
            if (not acct_name or not curr) and obj.account_id and obj.account_id in acct_by_id:
                acct = acct_by_id[obj.account_id]
                acct_name = acct_name or acct.name
                curr = curr or acct.currency
            return TransactionExportRow(
                id=obj.id,
                tx_date=obj.tx_date,
                account_id=obj.account_id,
                account_name=acct_name,
                currency=(curr or default_currency or "USD").upper(),
                direction=obj.direction,
                transfer_direction=obj.transfer_direction,
                amount_cents=obj.amount_cents,
                merchant=obj.merchant,
                category=obj.category,
                jar=obj.jar,
                memo_reason=obj.memo_reason,
                tags=obj.tags,
                source=obj.source,
                affects_balance=obj.affects_balance,
                split_summary=obj.split_summary,
            )

        acct_id_raw = _get_field(obj, "account_id", None)
        acct_id = _as_int(acct_id_raw) if acct_id_raw is not None else None
        acct = acct_by_id.get(acct_id) if acct_id is not None else None

        acct_name = str(_get_field(obj, "account_name", "") or "") or (acct.name if acct else "")
        curr = str(_get_field(obj, "currency", "") or "") or (acct.currency if acct else default_currency)
        direction = str(_get_field(obj, "direction", None) or _get_field(obj, "tx_type", "expense") or "expense")
        transfer_dir = str(_get_field(obj, "transfer_direction", "") or "")
        category = str(_get_field(obj, "category", None) or _get_field(obj, "subcategory", "") or "")
        jar = str(
            _get_field(obj, "jar", None)
            or _get_field(obj, "jar_name", None)
            or _get_field(obj, "jar_slug", "")
            or ""
        )
        memo_reason = str(_get_field(obj, "memo_reason", None) or _get_field(obj, "memo", "") or "")
        tags_str = _format_tags(_get_field(obj, "tags", None))
        split_str = str(_get_field(obj, "split_summary", "") or "") or _format_splits(_get_field(obj, "splits", None))
        affects_bal = _as_bool(_get_field(obj, "affects_balance", True), default=True)

        return TransactionExportRow(
            id=_as_int(_get_field(obj, "id", 0)),
            tx_date=str(_get_field(obj, "tx_date", "") or ""),
            account_id=acct_id,
            account_name=acct_name,
            currency=(curr or "USD").upper(),
            direction=direction.strip().lower(),
            transfer_direction=transfer_dir.strip().lower(),
            amount_cents=abs(_as_int(_get_field(obj, "amount_cents", 0))),
            merchant=str(_get_field(obj, "merchant", "") or ""),
            category=category,
            jar=jar,
            memo_reason=memo_reason,
            tags=tags_str,
            source=str(_get_field(obj, "source", "manual") or "manual"),
            affects_balance=affects_bal,
            split_summary=split_str,
        )

    @staticmethod
    def _normalize_holding(obj: Any) -> HoldingExportRow:
        if isinstance(obj, HoldingExportRow):
            return obj
        qty_e8 = _as_int(_get_field(obj, "quantity_e8", 0))
        price_e8 = _as_int(_get_field(obj, "price_e8", 0))
        mkt_val = _get_field(obj, "market_value_cents", None)
        if mkt_val is None:
            exact = (Decimal(qty_e8) * Decimal(price_e8)) / Decimal(10**14)
            mkt_val_cents = int(exact.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))
        else:
            mkt_val_cents = _as_int(mkt_val)
        cb_known = _as_bool(_get_field(obj, "cost_basis_known", True), default=True)
        cb_raw = _get_field(obj, "cost_basis_cents", None)
        if cb_raw is None:
            cb_known = False
            cb_cents: Optional[int] = None
        else:
            cb_cents = _as_int(cb_raw)
        pnl_raw = _get_field(obj, "unrealized_pnl_cents", None)
        pnl_pct_raw = _get_field(obj, "unrealized_pnl_pct", None)
        if cb_known and cb_cents is not None:
            pnl_cents = _as_int(pnl_raw) if pnl_raw is not None else (mkt_val_cents - cb_cents)
            if pnl_pct_raw is not None:
                pnl_pct = _as_float(pnl_pct_raw)
            elif cb_cents > 0:
                pnl_pct = round((pnl_cents / cb_cents) * 100.0, 2)
            else:
                pnl_pct = 0.0
        else:
            pnl_cents = None
            pnl_pct = None

        return HoldingExportRow(
            symbol=str(_get_field(obj, "symbol", "") or "").strip().upper(),
            name=str(_get_field(obj, "name", "") or ""),
            asset_class=str(_get_field(obj, "asset_class", "equity") or "equity").lower(),
            quantity_e8=qty_e8,
            price_e8=price_e8,
            market_value_cents=mkt_val_cents,
            cost_basis_cents=cb_cents,
            cost_basis_known=cb_known,
            unrealized_pnl_cents=pnl_cents,
            unrealized_pnl_pct=pnl_pct,
            source=str(_get_field(obj, "source", "manual") or "manual"),
        )

    @staticmethod
    def _normalize_runway_point(obj: Any) -> RunwayPointRow:
        if isinstance(obj, RunwayPointRow):
            return obj
        p50 = _get_field(obj, "p50_balance_cents", None)
        if p50 is None:
            p50 = _get_field(obj, "balance_cents", 0)
        p10 = _get_field(obj, "p10_balance_cents", None)
        p90 = _get_field(obj, "p90_balance_cents", None)
        ev = _get_field(obj, "events", "")
        if isinstance(ev, (list, tuple)):
            ev_str = "; ".join(str(x) for x in ev if x)
        else:
            ev_str = str(ev or "")
        return RunwayPointRow(
            date=str(_get_field(obj, "date", "") or ""),
            p50_balance_cents=_as_int(p50),
            inflow_cents=_as_int(_get_field(obj, "inflow_cents", 0)),
            outflow_cents=abs(_as_int(_get_field(obj, "outflow_cents", 0))),
            p10_balance_cents=_as_int(p10) if p10 is not None else None,
            p90_balance_cents=_as_int(p90) if p90 is not None else None,
            events=ev_str,
        )

    @staticmethod
    def _normalize_goal(obj: Any) -> GoalExportRow:
        if isinstance(obj, GoalExportRow):
            return obj
        rem = _get_field(obj, "remaining_cents", None)
        pct = _get_field(obj, "pct_funded", None)
        return GoalExportRow(
            id=_as_int(_get_field(obj, "id", 0)),
            name=str(_get_field(obj, "name", "") or ""),
            category=str(_get_field(obj, "category", "custom") or "custom"),
            funding_jar_slug=str(_get_field(obj, "funding_jar_slug", "fun") or "fun"),
            target_cents=_as_int(_get_field(obj, "target_cents", 0)),
            saved_cents=_as_int(_get_field(obj, "saved_cents", 0)),
            remaining_cents=_as_int(rem) if rem is not None else None,
            pct_funded=_as_float(pct) if pct is not None else None,
            target_date=str(_get_field(obj, "target_date", "") or ""),
            priority=_as_int(_get_field(obj, "priority", 2)),
            status=str(_get_field(obj, "status", "active") or "active"),
            notes=str(_get_field(obj, "notes", "") or ""),
        )

    @staticmethod
    def _normalize_bill(obj: Any) -> BillExportRow:
        if isinstance(obj, BillExportRow):
            return obj
        return BillExportRow(
            id=_as_int(_get_field(obj, "id", 0)),
            name=str(_get_field(obj, "name", "") or ""),
            amount_cents=_as_int(_get_field(obj, "amount_cents", 0)),
            cadence=str(_get_field(obj, "cadence", "monthly") or "monthly"),
            next_due_date=str(_get_field(obj, "next_due_date", "") or ""),
            days_until_due=_as_int(_get_field(obj, "days_until_due", 0)),
            is_essential=_as_bool(_get_field(obj, "is_essential", True), default=True),
            is_subscription=_as_bool(_get_field(obj, "is_subscription", False)),
            auto_pay=_as_bool(_get_field(obj, "auto_pay", False)),
            flagged_zombie=_as_bool(_get_field(obj, "flagged_zombie", False)),
        )

    @staticmethod
    def _normalize_credit(
        credit_report: Any,
    ) -> Tuple[
        Tuple[CreditScoreExportRow, ...],
        Tuple[CreditFactorExportRow, ...],
        Tuple[CreditLineExportRow, ...],
        Tuple[DebtStrategyExportRow, ...],
        str,
    ]:
        if credit_report is None:
            return (), (), (), (), CREDIT_FACTOR_DISCLAIMER

        fa = _get_field(credit_report, "factor_analysis", None)
        disclaimer = str(_get_field(fa, "disclaimer", CREDIT_FACTOR_DISCLAIMER) or CREDIT_FACTOR_DISCLAIMER)

        raw_scores = _get_field(credit_report, "credit_reports", None) or _get_field(fa, "latest_actual_scores", None) or []
        scores = tuple(
            CreditScoreExportRow(
                bureau=str(_get_field(s, "bureau", "other") or "other"),
                model=str(_get_field(s, "model", "fico8") or "fico8"),
                score=_as_int(_get_field(s, "score", 0)),
                pulled_date=str(_get_field(s, "pulled_date", "") or ""),
                source=str(_get_field(s, "source", "manual") or "manual"),
                notes=str(_get_field(s, "notes", "") or ""),
            )
            for s in raw_scores
        )

        factors: List[CreditFactorExportRow] = []
        for f in _get_field(fa, "fico8_factors", []) or []:
            factors.append(
                CreditFactorExportRow(
                    model="FICO 8",
                    label=str(_get_field(f, "label", "") or ""),
                    weight_pct=_as_int(_get_field(f, "weight_pct", 0)),
                    status=str(_get_field(f, "status", "fair") or "fair"),
                    summary=str(_get_field(f, "summary", "") or ""),
                    guidance=str(_get_field(f, "guidance", "") or ""),
                )
            )
        for f in _get_field(fa, "vantage3_factors", []) or []:
            factors.append(
                CreditFactorExportRow(
                    model="VantageScore 3.0",
                    label=str(_get_field(f, "label", "") or ""),
                    weight_pct=_as_int(_get_field(f, "weight_pct", 0)),
                    status=str(_get_field(f, "status", "fair") or "fair"),
                    summary=str(_get_field(f, "summary", "") or ""),
                    guidance=str(_get_field(f, "guidance", "") or ""),
                )
            )

        azeo_map: Dict[int, int] = {}
        for act in _get_field(credit_report, "azeo_plan", []) or []:
            cid = _as_int(_get_field(act, "credit_line_id", 0))
            if cid:
                azeo_map[cid] = _as_int(_get_field(act, "pay_before_close_cents", 0))

        lines: List[CreditLineExportRow] = []
        for cl in _get_field(credit_report, "credit_lines", []) or []:
            cid = _as_int(_get_field(cl, "id", 0))
            bal = _as_int(_get_field(cl, "balance_cents", 0))
            lim = _as_int(_get_field(cl, "credit_limit_cents", 0))
            util_raw = _get_field(cl, "utilization_pct", None)
            if util_raw is not None:
                util_pct: Optional[float] = _as_float(util_raw)
            elif lim > 0:
                util_pct = round((bal / lim) * 100.0, 2)
            else:
                util_pct = None
            apr_bps = _as_int(_get_field(cl, "effective_apr_bps", None) or _get_field(cl, "apr_bps", 0))
            min_pay = _as_int(
                _get_field(cl, "effective_min_payment_cents", None) or _get_field(cl, "min_payment_cents", 0)
            )
            lines.append(
                CreditLineExportRow(
                    id=cid,
                    name=str(_get_field(cl, "name", "") or ""),
                    issuer=str(_get_field(cl, "issuer", "") or ""),
                    kind=str(_get_field(cl, "kind", "credit_card") or "credit_card"),
                    balance_cents=bal,
                    credit_limit_cents=lim,
                    utilization_pct=util_pct,
                    apr_bps=apr_bps,
                    min_payment_cents=min_pay,
                    monthly_interest_cents=_as_int(_get_field(cl, "monthly_interest_cents", 0)),
                    azeo_paydown_cents=azeo_map.get(cid, _as_int(_get_field(cl, "azeo_paydown_cents", 0))),
                    statement_close_date=str(_get_field(cl, "next_statement_close_date", "") or ""),
                    next_due_date=str(_get_field(cl, "next_due_date", "") or ""),
                    autopay_mode=str(_get_field(cl, "autopay_mode", "statement") or "statement"),
                )
            )

        rec_key = str(_get_field(credit_report, "recommended_strategy", "") or "")
        strategies: List[DebtStrategyExportRow] = []
        for st in _get_field(credit_report, "strategies", []) or []:
            strat_id = str(_get_field(st, "strategy", "") or "")
            strategies.append(
                DebtStrategyExportRow(
                    strategy=strat_id,
                    label=str(_get_field(st, "label", strat_id) or strat_id),
                    tagline=str(_get_field(st, "tagline", "") or ""),
                    months_to_debt_free=_as_int(_get_field(st, "months_to_debt_free", 0)),
                    debt_free_date=str(_get_field(st, "debt_free_date", "") or ""),
                    total_interest_cents=_as_int(_get_field(st, "total_interest_cents", 0)),
                    interest_saved_vs_minimums_cents=_as_int(_get_field(st, "interest_saved_vs_minimums_cents", 0)),
                    first_win_months=_as_int(_get_field(st, "first_win_months", 0)),
                    first_target_name=str(_get_field(st, "first_target_name", "") or ""),
                    recommended=(
                        _as_bool(_get_field(st, "recommended_for_adhd", False))
                        or (bool(rec_key) and strat_id == rec_key)
                    ),
                )
            )

        return scores, tuple(factors), tuple(lines), tuple(strategies), disclaimer

    @staticmethod
    def _derive_allocations(holdings: Sequence[HoldingExportRow]) -> Tuple[AllocationExportRow, ...]:
        if not holdings:
            return ()
        totals: Dict[str, int] = {}
        counts: Dict[str, int] = {}
        for h in holdings:
            cls_key = (h.asset_class or "equity").lower()
            totals[cls_key] = totals.get(cls_key, 0) + h.market_value_cents
            counts[cls_key] = counts.get(cls_key, 0) + 1
        grand = sum(totals.values())
        rows = [
            AllocationExportRow(
                asset_class=cls_key,
                value_cents=val,
                pct=round((val / grand) * 100.0, 2) if grand > 0 else 0.0,
                holdings_count=counts[cls_key],
            )
            for cls_key, val in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        return tuple(rows)

    @staticmethod
    def _derive_top_expenses(transactions: Sequence[TransactionExportRow]) -> Tuple[CategoryExpenseRow, ...]:
        totals: Dict[str, int] = {}
        jars: Dict[str, str] = {}
        total_income = 0
        for tx in transactions:
            if tx.direction == "income":
                total_income += abs(tx.amount_cents)
            elif tx.direction == "expense":
                cat = (tx.category or tx.jar or "Uncategorized").strip() or "Uncategorized"
                totals[cat] = totals.get(cat, 0) + abs(tx.amount_cents)
                if cat not in jars and tx.jar:
                    jars[cat] = tx.jar
        total_exp = sum(totals.values())
        if not totals:
            return ()
        ordered = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
        return tuple(
            CategoryExpenseRow(
                label=cat,
                amount_cents=val,
                pct_of_expenses=round((val / total_exp) * 100.0, 2) if total_exp > 0 else 0.0,
                pct_of_income=round((val / total_income) * 100.0, 2) if total_income > 0 else 0.0,
                jar_slug=jars.get(cat, ""),
            )
            for cat, val in ordered
        )

    @staticmethod
    def _derive_monthly_cashflow(transactions: Sequence[TransactionExportRow]) -> Tuple[MonthlyCashflowRow, ...]:
        by_month: Dict[str, List[int]] = {}
        for tx in transactions:
            ym = (tx.tx_date or "")[:7]
            if len(ym) != 7 or ym[4] != "-":
                continue
            bucket = by_month.setdefault(ym, [0, 0])
            if tx.direction == "income":
                bucket[0] += abs(tx.amount_cents)
            elif tx.direction == "expense":
                bucket[1] += abs(tx.amount_cents)
        return tuple(
            MonthlyCashflowRow(month=ym, income_cents=vals[0], expense_cents=vals[1])
            for ym, vals in sorted(by_month.items())
        )

    @staticmethod
    def _compute_30d_flows(
        transactions: Sequence[TransactionExportRow],
        as_of_date: str,
        *,
        dashboard: Any = None,
        analytics: Any = None,
    ) -> Tuple[int, int]:
        if transactions:
            try:
                end_d = date.fromisoformat(as_of_date[:10])
            except ValueError:
                end_d = date.today()
            start_d = end_d - timedelta(days=30)
            inflow = 0
            outflow = 0
            for tx in transactions:
                try:
                    tx_d = date.fromisoformat((tx.tx_date or "")[:10])
                except ValueError:
                    continue
                if start_d <= tx_d <= end_d:
                    if tx.direction == "income":
                        inflow += abs(tx.amount_cents)
                    elif tx.direction == "expense":
                        outflow += abs(tx.amount_cents)
            if inflow > 0 or outflow > 0:
                return inflow, outflow

        if dashboard is not None:
            inc = _as_int(_get_field(dashboard, "monthly_income_cents", 0))
            exp = _as_int(_get_field(dashboard, "monthly_spent_cents", 0))
            if inc > 0 or exp > 0:
                return inc, exp

        if analytics is not None:
            return (
                _as_int(_get_field(analytics, "total_income_cents", 0)),
                abs(_as_int(_get_field(analytics, "total_expense_cents", 0))),
            )

        return 0, 0

    # ---- 1. Polished CSV Exports ----

    def export_transactions_csv(
        self,
        transactions: Union[ExportSnapshot, Sequence[Any]],
        *,
        accounts: Optional[Sequence[Any]] = None,
        include_bom: bool = True,
        default_currency: str = "USD",
    ) -> bytes:
        """
        Export transactions as UTF-8 RFC-4180 CSV bytes (optional UTF-8 BOM).

        Columns:
          id, tx_date, account_name, currency, direction, transfer_direction,
          amount_dollars, amount_cents, merchant, category, jar, memo_reason,
          tags, source, affects_balance, split_summary

        All untrusted text columns are formula-injection neutralized; numeric
        `amount_dollars` and `amount_cents` remain true signed numeric values.
        """
        if isinstance(transactions, ExportSnapshot):
            snap = transactions
            tx_seq: Sequence[Any] = snap.transactions
            accounts = accounts if accounts is not None else snap.accounts
            default_currency = snap.profile_currency or default_currency
        else:
            tx_seq = transactions

        acct_by_id: Dict[int, AccountExportRow] = {}
        if accounts:
            for a in accounts:
                norm_a = self._normalize_account(a)
                if norm_a.id:
                    acct_by_id[norm_a.id] = norm_a

        buf = io.StringIO(newline="")
        writer = csv.writer(buf, lineterminator="\r\n")
        writer.writerow(TRANSACTIONS_CSV_HEADERS)

        for raw_tx in tx_seq:
            tx = self._normalize_tx(raw_tx, acct_by_id, default_currency)
            signed_cents = signed_tx_cents(tx.direction, tx.amount_cents, tx.transfer_direction)
            writer.writerow(
                [
                    tx.id,
                    sanitize_csv_text(tx.tx_date),
                    sanitize_csv_text(tx.account_name),
                    sanitize_csv_text(tx.currency),
                    sanitize_csv_text(tx.direction),
                    sanitize_csv_text(tx.transfer_direction),
                    cents_to_decimal_str(signed_cents),
                    signed_cents,
                    sanitize_csv_text(tx.merchant),
                    sanitize_csv_text(tx.category),
                    sanitize_csv_text(tx.jar),
                    sanitize_csv_text(tx.memo_reason),
                    sanitize_csv_text(tx.tags),
                    sanitize_csv_text(tx.source),
                    "true" if tx.affects_balance else "false",
                    sanitize_csv_text(tx.split_summary),
                ]
            )

        encoding = "utf-8-sig" if include_bom else "utf-8"
        return buf.getvalue().encode(encoding)

    def export_transactions_csv_artifact(
        self,
        transactions: Union[ExportSnapshot, Sequence[Any]],
        *,
        accounts: Optional[Sequence[Any]] = None,
        include_bom: bool = True,
        default_currency: str = "USD",
        as_of_date: Optional[str] = None,
    ) -> ExportArtifact:
        """Return `export_transactions_csv` wrapped in an `ExportArtifact`."""
        if isinstance(transactions, ExportSnapshot):
            report_date = (as_of_date or transactions.as_of_date or date.today().isoformat())[:10]
            tx_count = len(transactions.transactions)
        else:
            report_date = (as_of_date or date.today().isoformat())[:10]
            tx_count = len(transactions)
        payload = self.export_transactions_csv(
            transactions,
            accounts=accounts,
            include_bom=include_bom,
            default_currency=default_currency,
        )
        return ExportArtifact(
            filename=f"chronox-transactions-{report_date}.csv",
            media_type=CSV_MEDIA_TYPE,
            content=payload,
            format="csv",
            row_counts={"transactions": tx_count},
            generated_at=_utc_now_iso(),
        )

    def export_executive_csv_pack_zip(
        self,
        snapshot: ExportSnapshot,
        *,
        include_bom: bool = True,
    ) -> bytes:
        """
        Export a 7-CSV `.zip` archive containing:
          - 01_executive_summary.csv
          - 02_accounts.csv
          - 03_budget_jars.csv
          - 04_transactions.csv
          - 05_credit_and_debt.csv
          - 06_portfolio_holdings.csv
          - 07_goals_and_bills.csv
        """
        encoding = "utf-8-sig" if include_bom else "utf-8"

        def _csv_bytes(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> bytes:
            buf = io.StringIO(newline="")
            w = csv.writer(buf, lineterminator="\r\n")
            w.writerow(headers)
            for row in rows:
                w.writerow(row)
            return buf.getvalue().encode(encoding)

        # 01_executive_summary.csv
        k = snapshot.kpis
        summary_rows: List[List[Any]] = [
            ["as_of_date", sanitize_csv_text(snapshot.as_of_date), "date"],
            ["profile_currency", sanitize_csv_text(snapshot.profile_currency), "currency_code"],
            ["liquid_cash_dollars", cents_to_decimal_str(k.liquid_cash_cents), snapshot.profile_currency],
            ["safe_to_spend_today_dollars", cents_to_decimal_str(k.safe_to_spend_today_cents), snapshot.profile_currency],
            ["safe_until_payday_dollars", cents_to_decimal_str(k.safe_until_payday_cents), snapshot.profile_currency],
            ["safe_to_invest_cap_dollars", cents_to_decimal_str(k.safe_to_invest_cap_cents), snapshot.profile_currency],
            ["net_worth_dollars", cents_to_decimal_str(k.net_worth_cents), snapshot.profile_currency],
            ["statistical_95_tail_cushion_dollars", cents_to_decimal_str(k.tail_cushion_95_cents), snapshot.profile_currency],
            ["inflow_30d_dollars", cents_to_decimal_str(k.inflow_30d_cents), snapshot.profile_currency],
            ["outflow_30d_dollars", cents_to_decimal_str(k.outflow_30d_cents), snapshot.profile_currency],
            ["net_flow_30d_dollars", cents_to_decimal_str(k.resolved_net_flow_30d_cents), snapshot.profile_currency],
            ["total_investments_dollars", cents_to_decimal_str(k.total_investments_cents), snapshot.profile_currency],
            ["total_debt_dollars", cents_to_decimal_str(k.total_debt_cents), snapshot.profile_currency],
            ["days_until_payday", k.days_until_payday, "days"],
            ["next_payday_date", sanitize_csv_text(k.next_payday_date or ""), "date"],
        ]
        if k.ruin_probability_pct is not None:
            summary_rows.append(["overdraft_risk_pct", f"{k.ruin_probability_pct:.2f}", "percent"])

        # 02_accounts.csv
        account_rows = [
            [
                a.id,
                sanitize_csv_text(a.name),
                sanitize_csv_text(a.account_type),
                sanitize_csv_text(a.institution),
                sanitize_csv_text(a.currency),
                cents_to_decimal_str(a.balance_cents),
                a.balance_cents,
                cents_to_decimal_str(a.available_cents) if a.available_cents is not None else "",
                cents_to_decimal_str(a.credit_limit_cents) if a.credit_limit_cents is not None else "",
                "true" if a.is_liquid else "false",
                sanitize_csv_text(a.mask),
            ]
            for a in snapshot.accounts
        ]

        # 03_budget_jars.csv
        jar_rows = [
            [
                j.id,
                sanitize_csv_text(j.slug),
                sanitize_csv_text(j.name),
                sanitize_csv_text(j.kind),
                f"{j.target_pct:.2f}",
                cents_to_decimal_str(j.monthly_target_cents),
                cents_to_decimal_str(j.allocated_cents),
                cents_to_decimal_str(j.available_cents),
                cents_to_decimal_str(j.spent_cents),
                cents_to_decimal_str(j.rollover_cents),
                f"{j.pct_used:.2f}",
                sanitize_csv_text(j.health),
                "true" if j.is_locked else "false",
            ]
            for j in snapshot.jars
        ]

        # 04_transactions.csv
        tx_csv = self.export_transactions_csv(
            snapshot.transactions,
            accounts=snapshot.accounts,
            include_bom=include_bom,
            default_currency=snapshot.profile_currency,
        )

        # 05_credit_and_debt.csv
        credit_rows: List[List[Any]] = []
        for sc in snapshot.credit_scores:
            credit_rows.append(
                [
                    "bureau_score",
                    sanitize_csv_text(sc.bureau),
                    sanitize_csv_text(sc.model),
                    sanitize_csv_text(sc.source),
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    sc.score,
                    sanitize_csv_text(sc.pulled_date),
                ]
            )
        for cl in snapshot.credit_lines:
            util = (
                f"{cl.utilization_pct:.2f}"
                if cl.utilization_pct is not None
                else (f"{(cl.balance_cents / cl.credit_limit_cents) * 100.0:.2f}" if cl.credit_limit_cents > 0 else "")
            )
            credit_rows.append(
                [
                    "credit_line",
                    sanitize_csv_text(cl.name),
                    sanitize_csv_text(cl.issuer),
                    sanitize_csv_text(cl.kind),
                    cents_to_decimal_str(cl.balance_cents),
                    cents_to_decimal_str(cl.credit_limit_cents),
                    util,
                    f"{cl.apr_bps / 100.0:.2f}",
                    cents_to_decimal_str(cl.azeo_paydown_cents),
                    cents_to_decimal_str(cl.min_payment_cents),
                    cents_to_decimal_str(cl.monthly_interest_cents),
                    sanitize_csv_text(cl.autopay_mode),
                ]
            )
        for st in snapshot.debt_strategies:
            credit_rows.append(
                [
                    "payoff_strategy",
                    sanitize_csv_text(st.label),
                    sanitize_csv_text(st.strategy),
                    "recommended" if st.recommended else "option",
                    "",
                    "",
                    "",
                    "",
                    cents_to_decimal_str(st.interest_saved_vs_minimums_cents),
                    cents_to_decimal_str(st.total_interest_cents),
                    st.months_to_debt_free,
                    sanitize_csv_text(st.debt_free_date),
                ]
            )

        # 06_portfolio_holdings.csv
        holding_rows = [
            [
                sanitize_csv_text(h.symbol),
                sanitize_csv_text(h.name),
                sanitize_csv_text(h.asset_class),
                e8_to_decimal_str(h.quantity_e8),
                e8_to_decimal_str(h.price_e8, min_decimals=2),
                cents_to_decimal_str(h.market_value_cents),
                cents_to_decimal_str(h.cost_basis_cents)
                if (h.cost_basis_known and h.cost_basis_cents is not None)
                else "Unknown",
                "true" if h.cost_basis_known else "false",
                cents_to_decimal_str(h.unrealized_pnl_cents)
                if (h.cost_basis_known and h.unrealized_pnl_cents is not None)
                else "Unknown",
                f"{h.unrealized_pnl_pct:.2f}"
                if (h.cost_basis_known and h.unrealized_pnl_pct is not None)
                else "Unknown",
                sanitize_csv_text(h.source),
            ]
            for h in snapshot.holdings
        ]

        # 07_goals_and_bills.csv
        gb_rows: List[List[Any]] = []
        for g in snapshot.goals:
            gb_rows.append(
                [
                    "goal",
                    g.id,
                    sanitize_csv_text(g.name),
                    sanitize_csv_text(g.category),
                    cents_to_decimal_str(g.target_cents),
                    cents_to_decimal_str(g.saved_cents),
                    cents_to_decimal_str(g.resolved_remaining_cents),
                    f"{g.resolved_pct_funded:.2f}",
                    sanitize_csv_text(g.target_date),
                    sanitize_csv_text(g.status),
                    sanitize_csv_text(g.notes),
                ]
            )
        for b in snapshot.bills:
            gb_rows.append(
                [
                    "bill",
                    b.id,
                    sanitize_csv_text(b.name),
                    sanitize_csv_text(b.cadence),
                    cents_to_decimal_str(b.amount_cents),
                    "",
                    "",
                    "",
                    sanitize_csv_text(b.next_due_date),
                    "auto_pay" if b.auto_pay else "manual_pay",
                    "zombie_review" if b.flagged_zombie else ("subscription" if b.is_subscription else "essential"),
                ]
            )

        files: Dict[str, bytes] = {
            "01_executive_summary.csv": _csv_bytes(["metric", "value", "unit"], summary_rows),
            "02_accounts.csv": _csv_bytes(
                [
                    "id",
                    "name",
                    "account_type",
                    "institution",
                    "currency",
                    "balance_dollars",
                    "balance_cents",
                    "available_dollars",
                    "credit_limit_dollars",
                    "is_liquid",
                    "mask",
                ],
                account_rows,
            ),
            "03_budget_jars.csv": _csv_bytes(
                [
                    "id",
                    "slug",
                    "name",
                    "kind",
                    "target_pct",
                    "monthly_target_dollars",
                    "allocated_dollars",
                    "available_dollars",
                    "spent_dollars",
                    "rollover_dollars",
                    "pct_used",
                    "health",
                    "is_locked",
                ],
                jar_rows,
            ),
            "04_transactions.csv": tx_csv,
            "05_credit_and_debt.csv": _csv_bytes(
                [
                    "record_type",
                    "name",
                    "issuer_or_key",
                    "kind_or_status",
                    "balance_dollars",
                    "limit_dollars",
                    "utilization_pct",
                    "apr_pct",
                    "azeo_paydown_or_interest_saved_dollars",
                    "min_payment_or_total_interest_dollars",
                    "monthly_interest_or_months",
                    "autopay_or_debt_free_date",
                ],
                credit_rows,
            ),
            "06_portfolio_holdings.csv": _csv_bytes(
                [
                    "symbol",
                    "name",
                    "asset_class",
                    "quantity",
                    "price_dollars",
                    "market_value_dollars",
                    "cost_basis_dollars",
                    "cost_basis_known",
                    "unrealized_pnl_dollars",
                    "unrealized_pnl_pct",
                    "source",
                ],
                holding_rows,
            ),
            "07_goals_and_bills.csv": _csv_bytes(
                [
                    "record_type",
                    "id",
                    "name",
                    "category_or_cadence",
                    "target_or_amount_dollars",
                    "saved_dollars",
                    "remaining_dollars",
                    "pct_funded",
                    "due_or_target_date",
                    "status",
                    "notes",
                ],
                gb_rows,
            ),
        }

        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for arcname in CSV_PACK_FILENAMES:
                zf.writestr(arcname, files[arcname])
        return zip_buf.getvalue()

    def export_executive_csv_pack_artifact(
        self,
        snapshot: ExportSnapshot,
        *,
        include_bom: bool = True,
    ) -> ExportArtifact:
        """Return `export_executive_csv_pack_zip` wrapped in an `ExportArtifact`."""
        payload = self.export_executive_csv_pack_zip(snapshot, include_bom=include_bom)
        return ExportArtifact(
            filename=f"chronox-executive-csv-pack-{snapshot.as_of_date[:10]}.zip",
            media_type=ZIP_MEDIA_TYPE,
            content=payload,
            format="zip",
            row_counts={
                "accounts": len(snapshot.accounts),
                "jars": len(snapshot.jars),
                "transactions": len(snapshot.transactions),
                "credit_lines": len(snapshot.credit_lines),
                "holdings": len(snapshot.holdings),
                "goals": len(snapshot.goals),
                "bills": len(snapshot.bills),
            },
            generated_at=snapshot.generated_at,
        )

    # ---- 2. Apple Numbers Multi-Sheet Graphical Workbook (.xlsx) ----

    def export_apple_numbers_workbook(
        self,
        snapshot: ExportSnapshot,
        *,
        convert_to_native_numbers: bool = False,
        as_of_date: Optional[str] = None,
    ) -> ExportArtifact:
        """
        Generate a 6-sheet Apple Numbers / Excel / LibreOffice OOXML `.xlsx`
        Executive Graphical Workbook with 5 live native DrawingML vector charts.

        Honesty guarantee: returns `.xlsx` with `XLSX_MEDIA_TYPE` by default.
        Only converts to native `.numbers` if `convert_to_native_numbers=True`
        on macOS with `/Applications/Numbers.app` + `osascript` present.
        """
        report_date = (as_of_date or snapshot.as_of_date or date.today().isoformat())[:10]
        sst = _SharedStrings()

        charts_xml: Dict[int, str] = {}
        drawings_xml: Dict[int, str] = {}
        drawing_rels: Dict[int, List[Tuple[str, str, str]]] = {}

        # Build all 6 sheets
        sheet1, d1_anchors, d1_charts = self._build_sheet1_executive(snapshot, sst)
        sheet1.set_drawing_rid("rId1")
        drawings_xml[1] = _build_drawing_xml(d1_anchors)
        drawing_rels[1] = [
            (
                a.rid,
                "http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart",
                f"../charts/chart{a.chart_index}.xml",
            )
            for a in d1_anchors
        ]
        charts_xml.update(d1_charts)

        sheet2, d2_anchors, d2_charts = self._build_sheet2_cashflow(snapshot, sst)
        sheet2.set_drawing_rid("rId1")
        drawings_xml[2] = _build_drawing_xml(d2_anchors)
        drawing_rels[2] = [
            (
                a.rid,
                "http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart",
                f"../charts/chart{a.chart_index}.xml",
            )
            for a in d2_anchors
        ]
        charts_xml.update(d2_charts)

        sheet3, d3_anchors, d3_charts = self._build_sheet3_portfolio(snapshot, sst)
        if d3_anchors:
            sheet3.set_drawing_rid("rId1")
            drawings_xml[3] = _build_drawing_xml(d3_anchors)
            drawing_rels[3] = [
                (
                    a.rid,
                    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart",
                    f"../charts/chart{a.chart_index}.xml",
                )
                for a in d3_anchors
            ]
            charts_xml.update(d3_charts)

        sheet4 = self._build_sheet4_credit(snapshot, sst)
        sheet5 = self._build_sheet5_goals_bills(snapshot, sst)
        sheet6 = self._build_sheet6_transactions(snapshot, sst)

        sheets = [sheet1, sheet2, sheet3, sheet4, sheet5, sheet6]
        xlsx_bytes = self._assemble_xlsx_zip(
            snapshot=snapshot,
            sheets=sheets,
            sst=sst,
            drawings_xml=drawings_xml,
            drawing_rels=drawing_rels,
            charts_xml=charts_xml,
        )

        filename = f"chronox-executive-report-{report_date}.xlsx"
        row_counts = {
            SHEET_EXECUTIVE: len(snapshot.jars),
            SHEET_CASHFLOW: len(snapshot.monthly_cashflow) + len(snapshot.runway_points),
            SHEET_PORTFOLIO: len(snapshot.holdings),
            SHEET_CREDIT: len(snapshot.credit_lines),
            SHEET_GOALS_BILLS: len(snapshot.goals) + len(snapshot.bills),
            SHEET_TRANSACTIONS: len(snapshot.transactions),
        }

        if convert_to_native_numbers:
            converted = convert_to_native_numbers_if_macos(xlsx_bytes, filename=filename)
            return ExportArtifact(
                filename=converted.filename,
                media_type=converted.media_type,
                content=converted.content,
                format=converted.format,
                sheet_names=SHEET_NAMES,
                row_counts=row_counts,
                generated_at=snapshot.generated_at,
                native_numbers_converted=converted.native_numbers_converted,
                notes=converted.notes,
            )

        return ExportArtifact(
            filename=filename,
            media_type=XLSX_MEDIA_TYPE,
            content=xlsx_bytes,
            format="xlsx",
            sheet_names=SHEET_NAMES,
            row_counts=row_counts,
            generated_at=snapshot.generated_at,
            native_numbers_converted=False,
            notes=("Apple Numbers, Microsoft Excel, and LibreOffice Calc compatible OOXML graphical workbook.",),
        )

    @staticmethod
    def convert_to_native_numbers_if_macos(
        xlsx_bytes: bytes,
        *,
        filename: Optional[str] = None,
        timeout_seconds: int = 45,
    ) -> ExportArtifact:
        """Convenience wrapper around module-level `convert_to_native_numbers_if_macos`."""
        return convert_to_native_numbers_if_macos(
            xlsx_bytes, filename=filename, timeout_seconds=timeout_seconds
        )

    # ---- Sheet 1: Executive Overview ----

    def _build_sheet1_executive(
        self,
        snapshot: ExportSnapshot,
        sst: _SharedStrings,
    ) -> Tuple[_WorksheetBuilder, List[_ChartAnchorSpec], Dict[int, str]]:
        ws = _WorksheetBuilder(SHEET_EXECUTIVE, sst, tab_selected=True)
        ws.set_col_widths([26, 17, 24, 17, 22, 17, 20, 18])
        qname = quote_sheet_name(SHEET_EXECUTIVE)
        k = snapshot.kpis

        ws.write_banner(
            f"{sanitize_sheet_text(snapshot.profile_name).upper()} — EXECUTIVE OVERVIEW",
            (
                f"As of {snapshot.as_of_date}  |  Currency: {snapshot.profile_currency}  |  "
                f"Generated: {snapshot.generated_at}  |  Deterministic Audited Ledger"
            ),
            col_span=8,
        )

        # KPI Cards Block
        ws.write_section_header(4, "EXECUTIVE KPI SNAPSHOT", 1, 8)
        ws.set_row_height(5, 24.0)
        ws.set_row_height(6, 24.0)
        ws.set_row_height(7, 24.0)

        # Row 5
        ws.write_text(5, 1, "Liquid Cash", STYLE_KPI_LABEL)
        ws.write_money(5, 2, k.liquid_cash_cents, STYLE_KPI_MONEY_EMERALD)
        ws.write_text(5, 3, "Safe-to-Spend Today", STYLE_KPI_LABEL)
        ws.write_money(5, 4, k.safe_to_spend_today_cents, STYLE_KPI_MONEY_EMERALD)
        ws.write_text(5, 5, "Safe Until Payday", STYLE_KPI_LABEL)
        ws.write_money(5, 6, k.safe_until_payday_cents, STYLE_KPI_MONEY_EMERALD)
        ws.write_text(5, 7, "Safe-to-Invest Cap", STYLE_KPI_LABEL)
        ws.write_money(5, 8, k.safe_to_invest_cap_cents, STYLE_KPI_MONEY_EMERALD)

        # Row 6
        ws.write_text(6, 1, "Net Worth", STYLE_KPI_LABEL)
        ws.write_money(6, 2, k.net_worth_cents, STYLE_KPI_MONEY)
        ws.write_text(6, 3, "Statistical 95% Tail Cushion", STYLE_KPI_LABEL)
        ws.write_money(6, 4, k.tail_cushion_95_cents, STYLE_KPI_MONEY)
        ws.write_text(6, 5, "30D Inflow", STYLE_KPI_LABEL)
        ws.write_money(6, 6, k.inflow_30d_cents, STYLE_KPI_MONEY)
        ws.write_text(6, 7, "30D Outflow", STYLE_KPI_LABEL)
        ws.write_money(6, 8, k.outflow_30d_cents, STYLE_KPI_MONEY)

        # Row 7
        ws.write_text(7, 1, "30D Net Flow", STYLE_KPI_LABEL)
        ws.write_formula(
            7,
            2,
            "F6-H6",
            cents_to_decimal_str(k.resolved_net_flow_30d_cents),
            STYLE_KPI_MONEY_EMERALD if k.resolved_net_flow_30d_cents >= 0 else STYLE_MONEY_NEG,
        )
        ws.write_text(7, 3, "Total Investments", STYLE_KPI_LABEL)
        ws.write_money(7, 4, k.total_investments_cents, STYLE_KPI_MONEY)
        ws.write_text(7, 5, "Total Debt", STYLE_KPI_LABEL)
        ws.write_money(7, 6, k.total_debt_cents, STYLE_KPI_MONEY)
        ws.write_text(7, 7, "Next Payday", STYLE_KPI_LABEL)
        if k.next_payday_date:
            ws.write_date(7, 8, k.next_payday_date, STYLE_DATE)
        else:
            ws.write_text(7, 8, "Not configured", STYLE_KPI_TEXT)

        # 5-Jar Budget Summary Table
        ws.write_section_header(9, "5-JAR BUDGET SUMMARY", 1, 8)
        jar_header_row = 10
        ws.write_table_headers(
            jar_header_row,
            ["Budget Jar", "Allocated", "Available", "Spent", "Monthly Target", "Used %", "Health", "Firewall"],
        )

        jars = list(snapshot.jars)
        if not jars:
            jars = [
                JarExportRow(
                    name="No Jars Configured",
                    allocated_cents=0,
                    available_cents=0,
                    spent_cents=0,
                    monthly_target_cents=0,
                    pct_used=0.0,
                    health="calm",
                    is_locked=False,
                )
            ]

        jar_start = jar_header_row + 1
        jar_labels: List[str] = []
        jar_alloc_vals: List[str] = []
        jar_avail_vals: List[str] = []
        jar_spent_vals: List[str] = []
        sum_alloc = 0
        sum_avail = 0
        sum_spent = 0
        sum_target = 0

        for idx, jar in enumerate(jars):
            r = jar_start + idx
            zebra = (idx % 2) == 1
            t_style = STYLE_TEXT_ZEBRA if zebra else STYLE_TEXT
            m_style = STYLE_MONEY_ZEBRA if zebra else STYLE_MONEY
            p_style = STYLE_PERCENT_ZEBRA if zebra else STYLE_PERCENT

            label = sanitize_sheet_text(jar.name or jar.slug or "Jar")
            ws.write_text(r, 1, label, t_style)
            ws.write_money(r, 2, jar.allocated_cents, m_style)
            ws.write_money(r, 3, jar.available_cents, m_style)
            ws.write_money(r, 4, jar.spent_cents, m_style)
            ws.write_money(r, 5, jar.monthly_target_cents, m_style)
            used_pct = (jar.spent_cents / jar.allocated_cents) * 100.0 if jar.allocated_cents > 0 else jar.pct_used
            ws.write_formula(r, 6, f"IF(B{r}>0,D{r}/B{r},0)", ratio_to_decimal_str(used_pct), p_style)

            health_low = (jar.health or "calm").lower()
            h_style = (
                STYLE_BADGE_GOOD
                if health_low == "calm"
                else (STYLE_BADGE_WARN if health_low == "watch" else STYLE_BADGE_ALERT)
            )
            ws.write_text(r, 7, health_low.title(), h_style)
            ws.write_text(r, 8, "Locked" if jar.is_locked else "Flexible", t_style)

            jar_labels.append(label)
            jar_alloc_vals.append(cents_to_decimal_str(jar.allocated_cents))
            jar_avail_vals.append(cents_to_decimal_str(jar.available_cents))
            jar_spent_vals.append(cents_to_decimal_str(jar.spent_cents))
            sum_alloc += jar.allocated_cents
            sum_avail += jar.available_cents
            sum_spent += jar.spent_cents
            sum_target += jar.monthly_target_cents

        jar_end = jar_start + len(jars) - 1
        jar_tot_row = jar_end + 1
        ws.write_text(jar_tot_row, 1, "Total", STYLE_TOTAL_LABEL)
        ws.write_formula(jar_tot_row, 2, f"SUM(B{jar_start}:B{jar_end})", cents_to_decimal_str(sum_alloc), STYLE_MONEY_TOTAL)
        ws.write_formula(jar_tot_row, 3, f"SUM(C{jar_start}:C{jar_end})", cents_to_decimal_str(sum_avail), STYLE_MONEY_TOTAL)
        ws.write_formula(jar_tot_row, 4, f"SUM(D{jar_start}:D{jar_end})", cents_to_decimal_str(sum_spent), STYLE_MONEY_TOTAL)
        ws.write_formula(jar_tot_row, 5, f"SUM(E{jar_start}:E{jar_end})", cents_to_decimal_str(sum_target), STYLE_MONEY_TOTAL)
        tot_used_pct = (sum_spent / sum_alloc) * 100.0 if sum_alloc > 0 else 0.0
        ws.write_formula(
            jar_tot_row,
            6,
            f"IF(B{jar_tot_row}>0,D{jar_tot_row}/B{jar_tot_row},0)",
            ratio_to_decimal_str(tot_used_pct),
            STYLE_PERCENT_TOTAL,
        )
        ws.write_text(jar_tot_row, 7, "—", STYLE_TOTAL_LABEL)
        ws.write_text(jar_tot_row, 8, "—", STYLE_TOTAL_LABEL)

        # Top Expense Categories Table
        cat_sec_row = jar_tot_row + 2
        ws.write_section_header(cat_sec_row, "TOP EXPENSE CATEGORIES", 1, 4)
        cat_header_row = cat_sec_row + 1
        ws.write_table_headers(cat_header_row, ["Category", "Expense Amount", "% of Expenses", "Jar"])

        categories = list(snapshot.top_expenses) or list(self._derive_top_expenses(snapshot.transactions))
        if not categories:
            categories = [CategoryExpenseRow(label="No Expenses Recorded", amount_cents=0, pct_of_expenses=0.0, jar_slug="—")]

        cat_start = cat_header_row + 1
        cat_labels: List[str] = []
        cat_vals: List[str] = []
        cat_total_cents = sum(c.amount_cents for c in categories)

        for idx, cat in enumerate(categories):
            r = cat_start + idx
            zebra = (idx % 2) == 1
            t_style = STYLE_TEXT_ZEBRA if zebra else STYLE_TEXT
            m_style = STYLE_MONEY_ZEBRA if zebra else STYLE_MONEY
            p_style = STYLE_PERCENT_ZEBRA if zebra else STYLE_PERCENT

            c_label = sanitize_sheet_text(cat.label or "Uncategorized")
            ws.write_text(r, 1, c_label, t_style)
            ws.write_money(r, 2, cat.amount_cents, m_style)
            pct = (cat.amount_cents / cat_total_cents) * 100.0 if cat_total_cents > 0 else 0.0
            cat_tot_row_ref = cat_start + len(categories)
            ws.write_formula(r, 3, f"IF($B${cat_tot_row_ref}>0,B{r}/$B${cat_tot_row_ref},0)", ratio_to_decimal_str(pct), p_style)
            ws.write_sanitized_text(r, 4, cat.jar_slug or "—", t_style)

            cat_labels.append(c_label)
            cat_vals.append(cents_to_decimal_str(cat.amount_cents))

        cat_end = cat_start + len(categories) - 1
        cat_tot_row = cat_end + 1
        ws.write_text(cat_tot_row, 1, "Total Expenses", STYLE_TOTAL_LABEL)
        ws.write_formula(cat_tot_row, 2, f"SUM(B{cat_start}:B{cat_end})", cents_to_decimal_str(cat_total_cents), STYLE_MONEY_TOTAL)
        ws.write_formula(
            cat_tot_row,
            3,
            f"SUM(C{cat_start}:C{cat_end})",
            ratio_to_decimal_str(100.0 if cat_total_cents > 0 else 0.0),
            STYLE_PERCENT_TOTAL,
        )
        ws.write_text(cat_tot_row, 4, "—", STYLE_TOTAL_LABEL)

        # Chart 1: 5-Jar Budget Allocated vs Available vs Spent (Clustered Column)
        chart1_xml = _build_bar_chart_xml(
            title="5-Jar Budget: Allocated vs Available vs Spent",
            cat_range_ref=f"{qname}!$A${jar_start}:$A${jar_end}",
            categories=jar_labels,
            series=[
                _ChartSeriesSpec(
                    name="Allocated",
                    name_cell_ref=f"{qname}!$B${jar_header_row}",
                    val_range_ref=f"{qname}!$B${jar_start}:$B${jar_end}",
                    values=tuple(jar_alloc_vals),
                    color_hex="0F172A",
                ),
                _ChartSeriesSpec(
                    name="Available",
                    name_cell_ref=f"{qname}!$C${jar_header_row}",
                    val_range_ref=f"{qname}!$C${jar_start}:$C${jar_end}",
                    values=tuple(jar_avail_vals),
                    color_hex="10B981",
                ),
                _ChartSeriesSpec(
                    name="Spent",
                    name_cell_ref=f"{qname}!$D${jar_header_row}",
                    val_range_ref=f"{qname}!$D${jar_start}:$D${jar_end}",
                    values=tuple(jar_spent_vals),
                    color_hex="F59E0B",
                ),
            ],
            cat_ax_id=1001,
            val_ax_id=1002,
        )

        # Chart 2: Expense Distribution by Category (Doughnut)
        chart2_xml = _build_radial_chart_xml(
            chart_tag="doughnutChart",
            title="Expense Distribution by Category",
            cat_range_ref=f"{qname}!$A${cat_start}:$A${cat_end}",
            categories=cat_labels,
            series=_ChartSeriesSpec(
                name="Expense Amount",
                name_cell_ref=f"{qname}!$B${cat_header_row}",
                val_range_ref=f"{qname}!$B${cat_start}:$B${cat_end}",
                values=tuple(cat_vals),
                color_hex="10B981",
            ),
        )

        # Anchor both charts cleanly side-by-side below all data tables
        chart_top_0idx = cat_tot_row + 1
        anchors = [
            _ChartAnchorSpec(
                chart_index=1,
                rid="rId1",
                shape_id=2,
                name="5-Jar Budget Comparison",
                from_col=0,
                from_row=chart_top_0idx,
                to_col=4,
                to_row=chart_top_0idx + 16,
            ),
            _ChartAnchorSpec(
                chart_index=2,
                rid="rId2",
                shape_id=3,
                name="Expense Distribution by Category",
                from_col=4,
                from_row=chart_top_0idx,
                to_col=8,
                to_row=chart_top_0idx + 16,
            ),
        ]
        return ws, anchors, {1: chart1_xml, 2: chart2_xml}

    # ---- Sheet 2: Cashflow & Runway ----

    def _build_sheet2_cashflow(
        self,
        snapshot: ExportSnapshot,
        sst: _SharedStrings,
    ) -> Tuple[_WorksheetBuilder, List[_ChartAnchorSpec], Dict[int, str]]:
        ws = _WorksheetBuilder(SHEET_CASHFLOW, sst)
        ws.set_col_widths([15, 18, 18, 18, 18, 28, 3, 14, 14, 14, 14, 14, 14, 14])
        qname = quote_sheet_name(SHEET_CASHFLOW)

        ws.write_banner(
            "CASHFLOW HISTORY & RUNWAY FORECAST",
            f"Monthly Income, Expenses, Net Cashflow, Savings Rate & 30/90-Day Liquidity Trajectory ({snapshot.as_of_date})",
            col_span=6,
        )

        # Monthly Cashflow Table
        ws.write_section_header(4, "MONTHLY CASHFLOW HISTORY", 1, 6)
        m_header_row = 5
        ws.write_table_headers(
            m_header_row,
            ["Month", "Income", "Expenses", "Net Cashflow", "Savings Rate %", "Cashflow Status"],
        )

        months = list(snapshot.monthly_cashflow) or list(self._derive_monthly_cashflow(snapshot.transactions))
        if not months:
            months = [MonthlyCashflowRow(month=snapshot.as_of_date[:7], income_cents=0, expense_cents=0)]

        m_start = m_header_row + 1
        m_labels: List[str] = []
        m_inc_vals: List[str] = []
        m_exp_vals: List[str] = []
        tot_inc = 0
        tot_exp = 0

        for idx, pt in enumerate(months):
            r = m_start + idx
            zebra = (idx % 2) == 1
            t_style = STYLE_TEXT_ZEBRA if zebra else STYLE_TEXT
            m_style = STYLE_MONEY_ZEBRA if zebra else STYLE_MONEY
            p_style = STYLE_PERCENT_ZEBRA if zebra else STYLE_PERCENT

            net_c = pt.resolved_net_cents
            sr = pt.resolved_savings_rate_pct
            label = sanitize_sheet_text(pt.month or snapshot.as_of_date[:7])
            ws.write_text(r, 1, label, t_style)
            ws.write_money(r, 2, pt.income_cents, m_style)
            ws.write_money(r, 3, pt.expense_cents, m_style)
            ws.write_formula(r, 4, f"B{r}-C{r}", cents_to_decimal_str(net_c), m_style)
            ws.write_formula(r, 5, f"IF(B{r}>0,(B{r}-C{r})/B{r},0)", ratio_to_decimal_str(sr), p_style)
            status_txt = "Surplus" if net_c > 0 else ("Balanced" if net_c == 0 else "Deficit")
            st_style = STYLE_BADGE_GOOD if net_c > 0 else (t_style if net_c == 0 else STYLE_BADGE_ALERT)
            ws.write_text(r, 6, status_txt, st_style)

            m_labels.append(label)
            m_inc_vals.append(cents_to_decimal_str(pt.income_cents))
            m_exp_vals.append(cents_to_decimal_str(pt.expense_cents))
            tot_inc += pt.income_cents
            tot_exp += pt.expense_cents

        m_end = m_start + len(months) - 1
        m_tot_row = m_end + 1
        tot_net = tot_inc - tot_exp
        tot_sr = (tot_net / tot_inc) * 100.0 if tot_inc > 0 else 0.0
        ws.write_text(m_tot_row, 1, "Total", STYLE_TOTAL_LABEL)
        ws.write_formula(m_tot_row, 2, f"SUM(B{m_start}:B{m_end})", cents_to_decimal_str(tot_inc), STYLE_MONEY_TOTAL)
        ws.write_formula(m_tot_row, 3, f"SUM(C{m_start}:C{m_end})", cents_to_decimal_str(tot_exp), STYLE_MONEY_TOTAL)
        ws.write_formula(m_tot_row, 4, f"SUM(D{m_start}:D{m_end})", cents_to_decimal_str(tot_net), STYLE_MONEY_TOTAL)
        ws.write_formula(
            m_tot_row,
            5,
            f"IF(B{m_tot_row}>0,D{m_tot_row}/B{m_tot_row},0)",
            ratio_to_decimal_str(tot_sr),
            STYLE_PERCENT_TOTAL,
        )
        ws.write_text(m_tot_row, 6, "—", STYLE_TOTAL_LABEL)

        # 30/90-Day Forecast Trajectory Table
        r_sec_row = m_tot_row + 2
        ws.write_section_header(r_sec_row, "30/90-DAY FORECAST RUNWAY TRAJECTORY", 1, 6)
        r_header_row = r_sec_row + 1
        ws.write_table_headers(
            r_header_row,
            ["Date", "Projected Balance (p50)", "Scheduled Inflow", "Scheduled Outflow", "Conservative Floor (p10)", "Events"],
        )

        runway = list(snapshot.runway_points)
        if not runway:
            try:
                base_d = date.fromisoformat(snapshot.as_of_date[:10])
            except ValueError:
                base_d = date.today()
            base_bal = snapshot.kpis.liquid_cash_cents
            runway = [
                RunwayPointRow(date=base_d.isoformat(), p50_balance_cents=base_bal, p10_balance_cents=base_bal, events="Starting cash"),
                RunwayPointRow(
                    date=(base_d + timedelta(days=30)).isoformat(),
                    p50_balance_cents=base_bal,
                    p10_balance_cents=base_bal,
                    events="No scheduled cashflow events",
                ),
            ]

        r_start = r_header_row + 1
        r_cat_values: List[str] = []
        r_p50_vals: List[str] = []
        r_p10_vals: List[str] = []
        all_serial_dates = all(iso_date_to_excel_serial(pt.date) is not None for pt in runway)
        has_distinct_p10 = False

        for idx, pt in enumerate(runway):
            r = r_start + idx
            zebra = (idx % 2) == 1
            d_style = STYLE_DATE_ZEBRA if zebra else STYLE_DATE
            m_style = STYLE_MONEY_ZEBRA if zebra else STYLE_MONEY
            t_style = STYLE_TEXT_ZEBRA if zebra else STYLE_TEXT

            if all_serial_dates:
                serial = iso_date_to_excel_serial(pt.date)
                ws.write_num(r, 1, str(serial), d_style)
                r_cat_values.append(str(serial))
            else:
                clean_d = sanitize_sheet_text(pt.date or snapshot.as_of_date)
                ws.write_text(r, 1, clean_d, t_style)
                r_cat_values.append(clean_d)

            p10_cents = pt.p10_balance_cents if pt.p10_balance_cents is not None else pt.p50_balance_cents
            if pt.p10_balance_cents is not None and pt.p10_balance_cents != pt.p50_balance_cents:
                has_distinct_p10 = True

            ws.write_money(r, 2, pt.p50_balance_cents, m_style)
            ws.write_money(r, 3, pt.inflow_cents, m_style)
            ws.write_money(r, 4, pt.outflow_cents, m_style)
            ws.write_money(r, 5, p10_cents, m_style)
            ws.write_sanitized_text(r, 6, pt.events or "—", t_style)

            r_p50_vals.append(cents_to_decimal_str(pt.p50_balance_cents))
            r_p10_vals.append(cents_to_decimal_str(p10_cents))

        r_end = r_start + len(runway) - 1

        # Chart 3: Monthly Income vs Outflow (Clustered Column)
        chart3_xml = _build_bar_chart_xml(
            title="Monthly Income vs Outflow",
            cat_range_ref=f"{qname}!$A${m_start}:$A${m_end}",
            categories=m_labels,
            series=[
                _ChartSeriesSpec(
                    name="Income",
                    name_cell_ref=f"{qname}!$B${m_header_row}",
                    val_range_ref=f"{qname}!$B${m_start}:$B${m_end}",
                    values=tuple(m_inc_vals),
                    color_hex="10B981",
                ),
                _ChartSeriesSpec(
                    name="Expenses",
                    name_cell_ref=f"{qname}!$C${m_header_row}",
                    val_range_ref=f"{qname}!$C${m_start}:$C${m_end}",
                    values=tuple(m_exp_vals),
                    color_hex="F43F5E",
                ),
            ],
            cat_ax_id=2001,
            val_ax_id=2002,
        )

        # Chart 4: Projected Cashflow Runway Trajectory (Line)
        runway_series = [
            _ChartSeriesSpec(
                name="Projected Balance (p50)",
                name_cell_ref=f"{qname}!$B${r_header_row}",
                val_range_ref=f"{qname}!$B${r_start}:$B${r_end}",
                values=tuple(r_p50_vals),
                color_hex="0EA5E9",
            )
        ]
        if has_distinct_p10:
            runway_series.append(
                _ChartSeriesSpec(
                    name="Conservative Floor (p10)",
                    name_cell_ref=f"{qname}!$E${r_header_row}",
                    val_range_ref=f"{qname}!$E${r_start}:$E${r_end}",
                    values=tuple(r_p10_vals),
                    color_hex="F59E0B",
                )
            )

        chart4_xml = _build_line_chart_xml(
            title="Projected Cashflow Runway Trajectory",
            cat_range_ref=f"{qname}!$A${r_start}:$A${r_end}",
            categories=r_cat_values,
            series=runway_series,
            cat_ax_id=2003,
            val_ax_id=2004,
            is_numeric_date=all_serial_dates,
        )

        # Place both charts in the right-hand canvas (Cols H..N, zero table overlap)
        anchors = [
            _ChartAnchorSpec(
                chart_index=3,
                rid="rId1",
                shape_id=2,
                name="Monthly Income vs Outflow",
                from_col=7,
                from_row=3,
                to_col=14,
                to_row=19,
            ),
            _ChartAnchorSpec(
                chart_index=4,
                rid="rId2",
                shape_id=3,
                name="Projected Cashflow Runway Trajectory",
                from_col=7,
                from_row=20,
                to_col=14,
                to_row=36,
            ),
        ]
        return ws, anchors, {3: chart3_xml, 4: chart4_xml}

    # ---- Sheet 3: Portfolio & Wealth ----

    def _build_sheet3_portfolio(
        self,
        snapshot: ExportSnapshot,
        sst: _SharedStrings,
    ) -> Tuple[_WorksheetBuilder, List[_ChartAnchorSpec], Dict[int, str]]:
        ws = _WorksheetBuilder(SHEET_PORTFOLIO, sst)
        ws.set_col_widths([14, 24, 14, 18, 16, 17, 17, 17, 14])
        qname = quote_sheet_name(SHEET_PORTFOLIO)

        ws.write_banner(
            "PORTFOLIO & WEALTH ALLOCATION",
            f"Exact 10^-8 Unit Valuation, Cost Basis Coverage & Asset Class Distribution ({snapshot.as_of_date})",
            col_span=9,
        )

        # Holdings Table
        ws.write_section_header(4, "INVESTMENT HOLDINGS", 1, 9)
        h_header_row = 5
        ws.write_table_headers(
            h_header_row,
            ["Symbol", "Name", "Class", "Quantity", "Price", "Market Value", "Cost Basis", "Unrealized P&L", "Return %"],
        )

        holdings = list(snapshot.holdings)
        has_real_holdings = bool(holdings)
        if not holdings:
            holdings = [
                HoldingExportRow(
                    symbol="—",
                    name="No holdings tracked",
                    asset_class="cash",
                    quantity_e8=0,
                    price_e8=0,
                    market_value_cents=0,
                    cost_basis_cents=None,
                    cost_basis_known=False,
                    unrealized_pnl_cents=None,
                    unrealized_pnl_pct=None,
                )
            ]

        h_start = h_header_row + 1
        sum_mkt = 0
        sum_known_cb = 0
        sum_known_pnl = 0

        for idx, h in enumerate(holdings):
            r = h_start + idx
            zebra = (idx % 2) == 1
            t_style = STYLE_TEXT_ZEBRA if zebra else STYLE_TEXT
            m_style = STYLE_MONEY_ZEBRA if zebra else STYLE_MONEY
            q_style = STYLE_QTY_E8_ZEBRA if zebra else STYLE_QTY_E8
            pr_style = STYLE_PRICE_E8_ZEBRA if zebra else STYLE_PRICE_E8
            p_style = STYLE_PERCENT_ZEBRA if zebra else STYLE_PERCENT

            ws.write_sanitized_text(r, 1, h.symbol or "—", STYLE_TEXT_BOLD if has_real_holdings else t_style)
            ws.write_sanitized_text(r, 2, h.name or h.symbol or "—", t_style)
            ws.write_sanitized_text(r, 3, (h.asset_class or "equity").upper(), t_style)
            ws.write_num(r, 4, e8_to_decimal_str(h.quantity_e8), q_style)
            ws.write_num(r, 5, e8_to_decimal_str(h.price_e8, min_decimals=2), pr_style)
            ws.write_money(r, 6, h.market_value_cents, m_style)

            sum_mkt += h.market_value_cents

            if h.cost_basis_known and h.cost_basis_cents is not None:
                cb_c = h.cost_basis_cents
                pnl_c = h.unrealized_pnl_cents if h.unrealized_pnl_cents is not None else (h.market_value_cents - cb_c)
                pnl_pct = (
                    h.unrealized_pnl_pct
                    if h.unrealized_pnl_pct is not None
                    else ((pnl_c / cb_c) * 100.0 if cb_c > 0 else 0.0)
                )
                ws.write_money(r, 7, cb_c, m_style)
                ws.write_formula(
                    r,
                    8,
                    f"F{r}-G{r}",
                    cents_to_decimal_str(pnl_c),
                    STYLE_MONEY_POS if pnl_c > 0 else (STYLE_MONEY_NEG if pnl_c < 0 else m_style),
                )
                ws.write_formula(r, 9, f"IF(G{r}>0,H{r}/G{r},0)", ratio_to_decimal_str(pnl_pct), p_style)
                sum_known_cb += cb_c
                sum_known_pnl += pnl_c
            else:
                ws.write_text(r, 7, "Unknown", STYLE_BADGE_WARN if has_real_holdings else t_style)
                ws.write_text(r, 8, "Unknown", STYLE_BADGE_WARN if has_real_holdings else t_style)
                ws.write_text(r, 9, "—", t_style)

        h_end = h_start + len(holdings) - 1
        h_tot_row = h_end + 1
        ws.write_text(h_tot_row, 1, "Total", STYLE_TOTAL_LABEL)
        ws.write_text(h_tot_row, 2, f"{len(snapshot.holdings)} holding(s)", STYLE_TOTAL_LABEL)
        ws.write_text(h_tot_row, 3, "—", STYLE_TOTAL_LABEL)
        ws.write_text(h_tot_row, 4, "—", STYLE_TOTAL_LABEL)
        ws.write_text(h_tot_row, 5, "—", STYLE_TOTAL_LABEL)
        ws.write_formula(h_tot_row, 6, f"SUM(F{h_start}:F{h_end})", cents_to_decimal_str(sum_mkt), STYLE_MONEY_TOTAL)
        ws.write_formula(h_tot_row, 7, f"SUM(G{h_start}:G{h_end})", cents_to_decimal_str(sum_known_cb), STYLE_MONEY_TOTAL)
        ws.write_formula(h_tot_row, 8, f"SUM(H{h_start}:H{h_end})", cents_to_decimal_str(sum_known_pnl), STYLE_MONEY_TOTAL)
        tot_ret_pct = (sum_known_pnl / sum_known_cb) * 100.0 if sum_known_cb > 0 else 0.0
        ws.write_formula(
            h_tot_row,
            9,
            f"IF(G{h_tot_row}>0,H{h_tot_row}/G{h_tot_row},0)",
            ratio_to_decimal_str(tot_ret_pct),
            STYLE_PERCENT_TOTAL,
        )

        # Asset Allocation Summary Table
        a_sec_row = h_tot_row + 2
        ws.write_section_header(a_sec_row, "ASSET ALLOCATION SUMMARY", 1, 4)
        a_header_row = a_sec_row + 1
        ws.write_table_headers(a_header_row, ["Asset Class", "Market Value", "Allocation %", "Holdings"])

        allocs = list(snapshot.allocations) or list(self._derive_allocations(snapshot.holdings))
        if not allocs:
            allocs = [AllocationExportRow(asset_class="No Holdings", value_cents=0, pct=0.0, holdings_count=0)]

        a_start = a_header_row + 1
        a_labels: List[str] = []
        a_vals: List[str] = []
        a_total_val = sum(a.value_cents for a in allocs)
        a_total_cnt = sum(a.holdings_count for a in allocs)
        a_tot_row = a_start + len(allocs)

        for idx, alloc in enumerate(allocs):
            r = a_start + idx
            zebra = (idx % 2) == 1
            t_style = STYLE_TEXT_ZEBRA if zebra else STYLE_TEXT
            m_style = STYLE_MONEY_ZEBRA if zebra else STYLE_MONEY
            p_style = STYLE_PERCENT_ZEBRA if zebra else STYLE_PERCENT
            i_style = STYLE_INT_ZEBRA if zebra else STYLE_INT

            cls_label = sanitize_sheet_text(alloc.asset_class.upper() if alloc.asset_class != "No Holdings" else alloc.asset_class)
            pct = (alloc.value_cents / a_total_val) * 100.0 if a_total_val > 0 else 0.0
            ws.write_text(r, 1, cls_label, t_style)
            ws.write_money(r, 2, alloc.value_cents, m_style)
            ws.write_formula(r, 3, f"IF($B${a_tot_row}>0,B{r}/$B${a_tot_row},0)", ratio_to_decimal_str(pct), p_style)
            ws.write_num(r, 4, str(alloc.holdings_count), i_style)

            a_labels.append(cls_label)
            a_vals.append(cents_to_decimal_str(alloc.value_cents))

        a_end = a_start + len(allocs) - 1
        ws.write_text(a_tot_row, 1, "Total Portfolio", STYLE_TOTAL_LABEL)
        ws.write_formula(a_tot_row, 2, f"SUM(B{a_start}:B{a_end})", cents_to_decimal_str(a_total_val), STYLE_MONEY_TOTAL)
        ws.write_formula(
            a_tot_row,
            3,
            f"SUM(C{a_start}:C{a_end})",
            ratio_to_decimal_str(100.0 if a_total_val > 0 else 0.0),
            STYLE_PERCENT_TOTAL,
        )
        ws.write_formula(a_tot_row, 4, f"SUM(D{a_start}:D{a_end})", str(a_total_cnt), STYLE_INT_TOTAL)

        # Chart 5: Portfolio Allocation by Asset Class (Pie) — skip when total is $0 so Numbers never renders a blank pie
        if a_total_val <= 0:
            ws.write_text(
                a_sec_row,
                5,
                "Add holdings or connect a brokerage to render your Portfolio Allocation chart.",
                STYLE_TEXT,
            )
            return ws, [], {}

        chart5_xml = _build_radial_chart_xml(
            chart_tag="pieChart",
            title="Portfolio Allocation by Asset Class",
            cat_range_ref=f"{qname}!$A${a_start}:$A${a_end}",
            categories=a_labels,
            series=_ChartSeriesSpec(
                name="Market Value",
                name_cell_ref=f"{qname}!$B${a_header_row}",
                val_range_ref=f"{qname}!$B${a_start}:$B${a_end}",
                values=tuple(a_vals),
                color_hex="10B981",
            ),
        )

        # Anchor Chart 5 neatly to the right of the Asset Allocation table (zero overlap)
        anchors = [
            _ChartAnchorSpec(
                chart_index=5,
                rid="rId1",
                shape_id=2,
                name="Portfolio Allocation by Asset Class",
                from_col=4,
                from_row=a_sec_row - 1,
                to_col=9,
                to_row=a_sec_row + 15,
            )
        ]
        return ws, anchors, {5: chart5_xml}

    # ---- Sheet 4: Credit & Debt Analysis ----

    def _build_sheet4_credit(
        self,
        snapshot: ExportSnapshot,
        sst: _SharedStrings,
    ) -> _WorksheetBuilder:
        ws = _WorksheetBuilder(SHEET_CREDIT, sst)
        ws.set_col_widths([24, 18, 16, 16, 15, 13, 18, 16, 28])

        disclaimer = sanitize_xml_text(snapshot.credit_disclaimer or CREDIT_FACTOR_DISCLAIMER)
        ws.write_banner("CREDIT & DEBT ANALYSIS", disclaimer, col_span=9)

        # Section 1: Bureau Scores Logged
        r = 4
        ws.write_section_header(r, "OFFICIAL BUREAU SCORES LOGGED", 1, 6)
        r += 1
        ws.write_table_headers(r, ["Bureau", "Scoring Model", "Reported Score", "Pulled Date", "Source", "Notes"])
        r += 1

        if snapshot.credit_scores:
            for idx, sc in enumerate(snapshot.credit_scores):
                zebra = (idx % 2) == 1
                t_style = STYLE_TEXT_ZEBRA if zebra else STYLE_TEXT
                i_style = STYLE_INT_ZEBRA if zebra else STYLE_INT
                d_style = STYLE_DATE_ZEBRA if zebra else STYLE_DATE
                ws.write_sanitized_text(r, 1, sc.bureau.title(), t_style)
                ws.write_sanitized_text(r, 2, sc.model.upper(), t_style)
                ws.write_num(r, 3, str(sc.score), i_style)
                ws.write_date(r, 4, sc.pulled_date, d_style)
                ws.write_sanitized_text(r, 5, sc.source, t_style)
                ws.write_sanitized_text(r, 6, sc.notes or "—", t_style)
                r += 1
        else:
            ws.write_text(r, 1, "No bureau scores logged", STYLE_TEXT)
            ws.write_text(r, 2, "—", STYLE_TEXT)
            ws.write_text(r, 3, "—", STYLE_TEXT)
            ws.write_text(r, 4, "—", STYLE_TEXT)
            ws.write_text(r, 5, "Manual / Official PDF", STYLE_TEXT)
            ws.write_text(r, 6, "Record an official bureau score to track longitudinal history.", STYLE_TEXT)
            r += 1

        # Section 2: Published FICO 8 & VantageScore 3.0 Factor Weights
        r += 1
        ws.write_section_header(r, "PUBLISHED FICO 8 & VANTAGESCORE 3.0 FACTOR WEIGHTS", 1, 6)
        r += 1
        ws.write_table_headers(
            r,
            ["Scoring Model", "Factor", "Published Weight", "Status", "Tracked Assessment", "Factual Guidance"],
        )
        r += 1

        factors = list(snapshot.credit_factors)
        if not factors:
            for lbl, wt, guide in _DEFAULT_FICO8_FACTORS:
                factors.append(
                    CreditFactorExportRow(
                        model="FICO 8",
                        label=lbl,
                        weight_pct=wt,
                        status="untracked",
                        summary="No credit tradelines tracked yet.",
                        guidance=guide,
                    )
                )
            for lbl, wt, guide in _DEFAULT_VANTAGE3_FACTORS:
                factors.append(
                    CreditFactorExportRow(
                        model="VantageScore 3.0",
                        label=lbl,
                        weight_pct=wt,
                        status="untracked",
                        summary="No credit tradelines tracked yet.",
                        guidance=guide,
                    )
                )

        for idx, fac in enumerate(factors):
            zebra = (idx % 2) == 1
            t_style = STYLE_TEXT_ZEBRA if zebra else STYLE_TEXT
            p_style = STYLE_PERCENT_ZEBRA if zebra else STYLE_PERCENT
            st_low = (fac.status or "untracked").lower()
            st_style = (
                STYLE_BADGE_GOOD
                if st_low in ("optimal", "good")
                else (STYLE_BADGE_WARN if st_low == "fair" else (STYLE_BADGE_ALERT if st_low == "drag" else t_style))
            )
            ws.write_sanitized_text(r, 1, fac.model, t_style)
            ws.write_sanitized_text(r, 2, fac.label, t_style)
            ws.write_percent(r, 3, float(fac.weight_pct), p_style)
            ws.write_text(r, 4, st_low.title(), st_style)
            ws.write_sanitized_text(r, 5, fac.summary or "—", t_style)
            ws.write_sanitized_text(r, 6, fac.guidance or "—", t_style)
            r += 1

        # Section 3: Credit Lines & AZEO Paydown Table
        r += 1
        ws.write_section_header(r, "CREDIT LINES & AZEO UTILIZATION", 1, 9)
        r += 1
        ws.write_table_headers(
            r,
            [
                "Card / Line",
                "Issuer",
                "Balance",
                "Limit",
                "Utilization %",
                "APR %",
                "AZEO Paydown Needed",
                "Min Payment",
                "Monthly Interest",
            ],
        )
        r += 1

        lines = list(snapshot.credit_lines)
        if not lines:
            lines = [
                CreditLineExportRow(
                    name="No credit lines tracked",
                    issuer="—",
                    balance_cents=0,
                    credit_limit_cents=0,
                    utilization_pct=0.0,
                    apr_bps=0,
                    azeo_paydown_cents=0,
                    min_payment_cents=0,
                    monthly_interest_cents=0,
                )
            ]

        cl_start = r
        sum_bal = 0
        sum_lim = 0
        sum_azeo = 0
        sum_min = 0
        sum_int = 0

        for idx, cl in enumerate(lines):
            zebra = (idx % 2) == 1
            t_style = STYLE_TEXT_ZEBRA if zebra else STYLE_TEXT
            m_style = STYLE_MONEY_ZEBRA if zebra else STYLE_MONEY
            p_style = STYLE_PERCENT_ZEBRA if zebra else STYLE_PERCENT

            util_pct = (
                cl.utilization_pct
                if cl.utilization_pct is not None
                else ((cl.balance_cents / cl.credit_limit_cents) * 100.0 if cl.credit_limit_cents > 0 else 0.0)
            )
            ws.write_sanitized_text(r, 1, cl.name, t_style)
            ws.write_sanitized_text(r, 2, cl.issuer or "—", t_style)
            ws.write_money(r, 3, cl.balance_cents, m_style)
            ws.write_money(r, 4, cl.credit_limit_cents, m_style)
            ws.write_formula(r, 5, f"IF(D{r}>0,C{r}/D{r},0)", ratio_to_decimal_str(util_pct), p_style)
            ws.write_num(r, 6, bps_to_decimal_str(cl.apr_bps), p_style)
            ws.write_money(r, 7, cl.azeo_paydown_cents, m_style)
            ws.write_money(r, 8, cl.min_payment_cents, m_style)
            ws.write_money(r, 9, cl.monthly_interest_cents, m_style)

            sum_bal += cl.balance_cents
            sum_lim += cl.credit_limit_cents
            sum_azeo += cl.azeo_paydown_cents
            sum_min += cl.min_payment_cents
            sum_int += cl.monthly_interest_cents
            r += 1

        cl_end = r - 1
        agg_util = (sum_bal / sum_lim) * 100.0 if sum_lim > 0 else 0.0
        ws.write_text(r, 1, "Total", STYLE_TOTAL_LABEL)
        ws.write_text(r, 2, f"{len(snapshot.credit_lines)} line(s)", STYLE_TOTAL_LABEL)
        ws.write_formula(r, 3, f"SUM(C{cl_start}:C{cl_end})", cents_to_decimal_str(sum_bal), STYLE_MONEY_TOTAL)
        ws.write_formula(r, 4, f"SUM(D{cl_start}:D{cl_end})", cents_to_decimal_str(sum_lim), STYLE_MONEY_TOTAL)
        ws.write_formula(r, 5, f"IF(D{r}>0,C{r}/D{r},0)", ratio_to_decimal_str(agg_util), STYLE_PERCENT_TOTAL)
        ws.write_text(r, 6, "—", STYLE_TOTAL_LABEL)
        ws.write_formula(r, 7, f"SUM(G{cl_start}:G{cl_end})", cents_to_decimal_str(sum_azeo), STYLE_MONEY_TOTAL)
        ws.write_formula(r, 8, f"SUM(H{cl_start}:H{cl_end})", cents_to_decimal_str(sum_min), STYLE_MONEY_TOTAL)
        ws.write_formula(r, 9, f"SUM(I{cl_start}:I{cl_end})", cents_to_decimal_str(sum_int), STYLE_MONEY_TOTAL)

        # Section 4: Payoff Strategy Comparison
        r += 2
        ws.write_section_header(r, "PAYOFF STRATEGY COMPARISON (DETERMINISTIC AMORTIZATION)", 1, 9)
        r += 1
        ws.write_table_headers(
            r,
            [
                "Strategy",
                "Months to Debt-Free",
                "Debt-Free Date",
                "Total Interest",
                "Interest Saved vs Minimums",
                "First Win (Mo)",
                "First Target",
                "Recommended",
                "Summary",
            ],
        )
        r += 1

        if snapshot.debt_strategies:
            for idx, st in enumerate(snapshot.debt_strategies):
                zebra = (idx % 2) == 1
                t_style = STYLE_TEXT_ZEBRA if zebra else STYLE_TEXT
                m_style = STYLE_MONEY_ZEBRA if zebra else STYLE_MONEY
                i_style = STYLE_INT_ZEBRA if zebra else STYLE_INT
                d_style = STYLE_DATE_ZEBRA if zebra else STYLE_DATE

                ws.write_sanitized_text(r, 1, st.label, STYLE_TEXT_BOLD if st.recommended else t_style)
                ws.write_num(r, 2, str(st.months_to_debt_free), i_style)
                ws.write_date(r, 3, st.debt_free_date, d_style)
                ws.write_money(r, 4, st.total_interest_cents, m_style)
                ws.write_money(r, 5, st.interest_saved_vs_minimums_cents, m_style)
                ws.write_num(r, 6, str(st.first_win_months), i_style)
                ws.write_sanitized_text(r, 7, st.first_target_name or "—", t_style)
                ws.write_text(r, 8, "Yes" if st.recommended else "No", STYLE_BADGE_GOOD if st.recommended else t_style)
                ws.write_sanitized_text(r, 9, st.tagline or "—", t_style)
                r += 1
        else:
            ws.write_text(r, 1, "No active debt payoff schedule", STYLE_TEXT)
            ws.write_num(r, 2, "0", STYLE_INT)
            ws.write_text(r, 3, "—", STYLE_TEXT)
            ws.write_money(r, 4, 0, STYLE_MONEY)
            ws.write_money(r, 5, 0, STYLE_MONEY)
            ws.write_num(r, 6, "0", STYLE_INT)
            ws.write_text(r, 7, "—", STYLE_TEXT)
            ws.write_text(r, 8, "—", STYLE_TEXT)
            ws.write_text(r, 9, "Track revolving cards or installment loans to compute payoff math.", STYLE_TEXT)

        return ws

    # ---- Sheet 5: Goals & Bills ----

    def _build_sheet5_goals_bills(
        self,
        snapshot: ExportSnapshot,
        sst: _SharedStrings,
    ) -> _WorksheetBuilder:
        ws = _WorksheetBuilder(SHEET_GOALS_BILLS, sst)
        ws.set_col_widths([26, 16, 16, 16, 16, 14, 15, 14, 26])

        ws.write_banner(
            "SAVINGS GOALS & RECURRING BILLS SCHEDULE",
            f"Goal Funding Progress & Upcoming Fixed Obligations ({snapshot.as_of_date})",
            col_span=9,
        )

        # Section 1: Savings Goals
        r = 4
        ws.write_section_header(r, "SAVINGS GOALS PROGRESS", 1, 9)
        r += 1
        ws.write_table_headers(
            r,
            ["Goal", "Category", "Target", "Saved", "Remaining", "Funded %", "Target Date", "Status", "Notes"],
        )
        r += 1

        goals = list(snapshot.goals)
        if not goals:
            goals = [
                GoalExportRow(
                    name="No savings goals configured",
                    category="—",
                    target_cents=0,
                    saved_cents=0,
                    remaining_cents=0,
                    pct_funded=0.0,
                    target_date="",
                    status="—",
                    notes="Add a goal to track target dates and progress.",
                )
            ]

        g_start = r
        sum_tgt = 0
        sum_saved = 0
        sum_rem = 0

        for idx, g in enumerate(goals):
            zebra = (idx % 2) == 1
            t_style = STYLE_TEXT_ZEBRA if zebra else STYLE_TEXT
            m_style = STYLE_MONEY_ZEBRA if zebra else STYLE_MONEY
            p_style = STYLE_PERCENT_ZEBRA if zebra else STYLE_PERCENT
            d_style = STYLE_DATE_ZEBRA if zebra else STYLE_DATE

            rem_c = g.resolved_remaining_cents
            pct = g.resolved_pct_funded
            ws.write_sanitized_text(r, 1, g.name, t_style)
            ws.write_sanitized_text(r, 2, g.category, t_style)
            ws.write_money(r, 3, g.target_cents, m_style)
            ws.write_money(r, 4, g.saved_cents, m_style)
            ws.write_formula(r, 5, f"MAX(0,C{r}-D{r})", cents_to_decimal_str(rem_c), m_style)
            ws.write_formula(r, 6, f"IF(C{r}>0,D{r}/C{r},0)", ratio_to_decimal_str(pct), p_style)
            ws.write_date(r, 7, g.target_date, d_style)
            st_low = (g.status or "active").lower()
            st_style = STYLE_BADGE_GOOD if st_low in ("funded", "completed") else t_style
            ws.write_sanitized_text(r, 8, g.status.title() if g.status != "—" else "—", st_style)
            ws.write_sanitized_text(r, 9, g.notes or "—", t_style)

            sum_tgt += g.target_cents
            sum_saved += g.saved_cents
            sum_rem += rem_c
            r += 1

        g_end = r - 1
        agg_funded = (sum_saved / sum_tgt) * 100.0 if sum_tgt > 0 else 0.0
        ws.write_text(r, 1, "Total Goals", STYLE_TOTAL_LABEL)
        ws.write_text(r, 2, f"{len(snapshot.goals)} goal(s)", STYLE_TOTAL_LABEL)
        ws.write_formula(r, 3, f"SUM(C{g_start}:C{g_end})", cents_to_decimal_str(sum_tgt), STYLE_MONEY_TOTAL)
        ws.write_formula(r, 4, f"SUM(D{g_start}:D{g_end})", cents_to_decimal_str(sum_saved), STYLE_MONEY_TOTAL)
        ws.write_formula(r, 5, f"SUM(E{g_start}:E{g_end})", cents_to_decimal_str(sum_rem), STYLE_MONEY_TOTAL)
        ws.write_formula(r, 6, f"IF(C{r}>0,D{r}/C{r},0)", ratio_to_decimal_str(agg_funded), STYLE_PERCENT_TOTAL)
        ws.write_text(r, 7, "—", STYLE_TOTAL_LABEL)
        ws.write_text(r, 8, "—", STYLE_TOTAL_LABEL)
        ws.write_text(r, 9, "—", STYLE_TOTAL_LABEL)

        # Section 2: Recurring Bills Schedule
        r += 2
        ws.write_section_header(r, "RECURRING BILLS & SUBSCRIPTIONS SCHEDULE", 1, 9)
        r += 1
        ws.write_table_headers(
            r,
            [
                "Bill / Merchant",
                "Amount",
                "Cadence",
                "Next Due Date",
                "Days Until Due",
                "Essential",
                "Subscription",
                "Auto-Pay",
                "Review Status",
            ],
        )
        r += 1

        bills = list(snapshot.bills)
        if not bills:
            bills = [
                BillExportRow(
                    name="No recurring bills scheduled",
                    amount_cents=0,
                    cadence="—",
                    next_due_date="",
                    days_until_due=0,
                    is_essential=False,
                    is_subscription=False,
                    auto_pay=False,
                    flagged_zombie=False,
                )
            ]

        b_start = r
        sum_bills = 0
        for idx, b in enumerate(bills):
            zebra = (idx % 2) == 1
            t_style = STYLE_TEXT_ZEBRA if zebra else STYLE_TEXT
            m_style = STYLE_MONEY_ZEBRA if zebra else STYLE_MONEY
            d_style = STYLE_DATE_ZEBRA if zebra else STYLE_DATE
            i_style = STYLE_INT_ZEBRA if zebra else STYLE_INT

            ws.write_sanitized_text(r, 1, b.name, t_style)
            ws.write_money(r, 2, b.amount_cents, m_style)
            ws.write_sanitized_text(r, 3, b.cadence.title() if b.cadence != "—" else "—", t_style)
            ws.write_date(r, 4, b.next_due_date, d_style)
            ws.write_num(r, 5, str(b.days_until_due), i_style)
            ws.write_text(r, 6, "Yes" if b.is_essential else "No", t_style)
            ws.write_text(r, 7, "Yes" if b.is_subscription else "No", t_style)
            ws.write_text(r, 8, "On" if b.auto_pay else "Off", STYLE_BADGE_GOOD if b.auto_pay else t_style)
            if b.flagged_zombie:
                ws.write_text(r, 9, "Review Subscription", STYLE_BADGE_WARN)
            else:
                ws.write_text(r, 9, "Active" if snapshot.bills else "—", t_style)

            sum_bills += b.amount_cents
            r += 1

        b_end = r - 1
        ws.write_text(r, 1, "Total Scheduled Bills", STYLE_TOTAL_LABEL)
        ws.write_formula(r, 2, f"SUM(B{b_start}:B{b_end})", cents_to_decimal_str(sum_bills), STYLE_MONEY_TOTAL)
        ws.write_text(r, 3, f"{len(snapshot.bills)} bill(s)", STYLE_TOTAL_LABEL)
        for c in range(4, 10):
            ws.write_text(r, c, "—", STYLE_TOTAL_LABEL)

        return ws

    # ---- Sheet 6: Transactions Ledger ----

    def _build_sheet6_transactions(
        self,
        snapshot: ExportSnapshot,
        sst: _SharedStrings,
    ) -> _WorksheetBuilder:
        ws = _WorksheetBuilder(SHEET_TRANSACTIONS, sst, freeze_row=4)
        ws.set_col_widths([14, 22, 11, 15, 28, 18, 18, 16, 36, 15])

        ws.write_banner(
            "TRANSACTIONS LEDGER",
            f"Complete Unfiltered Ledger ({len(snapshot.transactions)} transaction(s)) — Formula-Injection Neutralized",
            col_span=10,
        )

        header_row = 4
        ws.write_table_headers(
            header_row,
            ["Date", "Account", "Currency", "Direction", "Merchant", "Category", "Jar", "Amount", "Reason / Memo", "Source"],
        )

        tx_start = header_row + 1
        txs = list(snapshot.transactions)
        has_real_txs = bool(txs)
        if not txs:
            txs = [
                TransactionExportRow(
                    id=0,
                    tx_date=snapshot.as_of_date,
                    account_name="No transactions recorded",
                    currency=snapshot.profile_currency,
                    direction="—",
                    amount_cents=0,
                    merchant="—",
                    category="—",
                    jar="—",
                    memo_reason="Fresh ledger",
                    source="system",
                )
            ]

        r = tx_start
        sum_signed_cents = 0
        for idx, tx in enumerate(txs):
            zebra = (idx % 2) == 1
            t_style = STYLE_TEXT_ZEBRA if zebra else STYLE_TEXT
            d_style = STYLE_DATE_ZEBRA if zebra else STYLE_DATE

            if has_real_txs:
                signed_c = signed_tx_cents(tx.direction, tx.amount_cents, tx.transfer_direction)
            else:
                signed_c = 0
            sum_signed_cents += signed_c

            is_usd = (tx.currency or "USD").upper() == "USD"
            if is_usd:
                amt_style = STYLE_MONEY_ZEBRA if zebra else STYLE_MONEY
            else:
                amt_style = STYLE_MONEY_NONUSD_ZEBRA if zebra else STYLE_MONEY_NONUSD

            dir_label = tx.direction
            if tx.direction == "transfer" and tx.transfer_direction:
                dir_label = f"transfer ({tx.transfer_direction})"

            ws.write_date(r, 1, tx.tx_date, d_style)
            ws.write_sanitized_text(r, 2, tx.account_name or "Primary Account", t_style)
            ws.write_sanitized_text(r, 3, (tx.currency or snapshot.profile_currency).upper(), t_style)
            ws.write_sanitized_text(r, 4, dir_label, t_style)
            ws.write_sanitized_text(r, 5, tx.merchant or "—", t_style)
            ws.write_sanitized_text(r, 6, tx.category or "—", t_style)
            ws.write_sanitized_text(r, 7, tx.jar or "—", t_style)
            # True numeric cell for amount (NEVER stringified or apostrophe-prefixed!)
            ws.write_money(r, 8, signed_c, amt_style)
            ws.write_sanitized_text(r, 9, tx.memo_reason or "", t_style)
            ws.write_sanitized_text(r, 10, tx.source or "manual", t_style)
            r += 1

        tx_end = r - 1
        currencies_seen = {(tx.currency or snapshot.profile_currency).upper() for tx in txs}
        ws.write_text(r, 1, "Total", STYLE_TOTAL_LABEL)
        ws.write_text(r, 2, f"{len(snapshot.transactions)} transaction(s)", STYLE_TOTAL_LABEL)
        ws.write_text(r, 3, next(iter(currencies_seen)) if len(currencies_seen) == 1 else "MIXED", STYLE_TOTAL_LABEL)
        for c in (4, 5, 6, 7):
            ws.write_text(r, c, "—", STYLE_TOTAL_LABEL)
        ws.write_formula(
            r,
            8,
            f"SUM(H{tx_start}:H{tx_end})",
            cents_to_decimal_str(sum_signed_cents),
            STYLE_MONEY_TOTAL,
        )
        sum_note = (
            "Live Net Cashflow SUM"
            if len(currencies_seen) <= 1
            else "Live Numeric SUM (multi-currency — review Currency column)"
        )
        ws.write_text(r, 9, sum_note, STYLE_TOTAL_LABEL)
        ws.write_text(r, 10, "—", STYLE_TOTAL_LABEL)

        return ws

    # ---- OOXML Package Assembler ----

    def _assemble_xlsx_zip(
        self,
        *,
        snapshot: ExportSnapshot,
        sheets: Sequence[_WorksheetBuilder],
        sst: _SharedStrings,
        drawings_xml: Mapping[int, str],
        drawing_rels: Mapping[int, Sequence[Tuple[str, str, str]]],
        charts_xml: Mapping[int, str],
    ) -> bytes:
        # [Content_Types].xml
        ct_overrides: List[str] = [
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>',
            '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>',
            '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>',
            '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>',
        ]
        for idx in range(1, len(sheets) + 1):
            ct_overrides.append(
                f'<Override PartName="/xl/worksheets/sheet{idx}.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            )
        for d_idx in sorted(drawings_xml):
            ct_overrides.append(
                f'<Override PartName="/xl/drawings/drawing{d_idx}.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.drawing+xml"/>'
            )
        for c_idx in sorted(charts_xml):
            ct_overrides.append(
                f'<Override PartName="/xl/charts/chart{c_idx}.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.drawingml.chart+xml"/>'
            )

        content_types_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            f'{"".join(ct_overrides)}'
            "</Types>"
        )

        # _rels/.rels
        root_rels_xml = _build_rels_xml(
            [
                ("rId1", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument", "xl/workbook.xml"),
                ("rId2", "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties", "docProps/core.xml"),
                ("rId3", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties", "docProps/app.xml"),
            ]
        )

        # docProps/core.xml & docProps/app.xml
        created_ts = sanitize_xml_text(snapshot.generated_at or _utc_now_iso())
        title_escaped = _xml_escape(sanitize_xml_text(snapshot.profile_name))
        core_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties '
            'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:dcmitype="http://purl.org/dc/dcmitype/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f"<dc:title>{title_escaped}</dc:title>"
            "<dc:creator>ChronoX Finance</dc:creator>"
            "<cp:lastModifiedBy>ChronoX Finance</cp:lastModifiedBy>"
            f'<dcterms:created xsi:type="dcterms:W3CDTF">{_xml_escape(created_ts)}</dcterms:created>'
            f'<dcterms:modified xsi:type="dcterms:W3CDTF">{_xml_escape(created_ts)}</dcterms:modified>'
            "</cp:coreProperties>"
        )

        sheet_title_tags = "".join(f"<vt:lpstr>{_xml_escape(ws.name)}</vt:lpstr>" for ws in sheets)
        app_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
            'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
            "<Application>ChronoX Finance Exporter</Application>"
            "<DocSecurity>0</DocSecurity>"
            "<ScaleCrop>false</ScaleCrop>"
            "<HeadingPairs>"
            '<vt:vector size="2" baseType="variant">'
            "<vt:variant><vt:lpstr>Worksheets</vt:lpstr></vt:variant>"
            f"<vt:variant><vt:i4>{len(sheets)}</vt:i4></vt:variant>"
            "</vt:vector>"
            "</HeadingPairs>"
            "<TitlesOfParts>"
            f'<vt:vector size="{len(sheets)}" baseType="lpstr">{sheet_title_tags}</vt:vector>'
            "</TitlesOfParts>"
            "</Properties>"
        )

        # xl/workbook.xml & xl/_rels/workbook.xml.rels
        sheet_tags: List[str] = []
        wb_rels_entries: List[Tuple[str, str, str]] = []
        for idx, ws in enumerate(sheets, start=1):
            rid = f"rId{idx}"
            sheet_tags.append(
                f'<sheet name={_xml_quoteattr(ws.name)} sheetId="{idx}" r:id="{rid}"/>'
            )
            wb_rels_entries.append(
                (
                    rid,
                    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet",
                    f"worksheets/sheet{idx}.xml",
                )
            )

        styles_rid = f"rId{len(sheets) + 1}"
        sst_rid = f"rId{len(sheets) + 2}"
        wb_rels_entries.append(
            (styles_rid, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles", "styles.xml")
        )
        wb_rels_entries.append(
            (sst_rid, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings", "sharedStrings.xml")
        )

        workbook_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<workbookPr date1904="0"/>'
            '<bookViews><workbookView xWindow="0" yWindow="0" windowWidth="24000" windowHeight="15000"/></bookViews>'
            f'<sheets>{"".join(sheet_tags)}</sheets>'
            '<calcPr calcId="191029" fullCalcOnLoad="1"/>'
            "</workbook>"
        )
        wb_rels_xml = _build_rels_xml(wb_rels_entries)

        # Assemble final ZIP package
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", content_types_xml.encode("utf-8"))
            zf.writestr("_rels/.rels", root_rels_xml.encode("utf-8"))
            zf.writestr("docProps/core.xml", core_xml.encode("utf-8"))
            zf.writestr("docProps/app.xml", app_xml.encode("utf-8"))
            zf.writestr("xl/workbook.xml", workbook_xml.encode("utf-8"))
            zf.writestr("xl/_rels/workbook.xml.rels", wb_rels_xml.encode("utf-8"))
            zf.writestr("xl/styles.xml", _build_styles_xml().encode("utf-8"))
            zf.writestr("xl/sharedStrings.xml", sst.to_xml().encode("utf-8"))

            for idx, ws in enumerate(sheets, start=1):
                zf.writestr(f"xl/worksheets/sheet{idx}.xml", ws.to_xml().encode("utf-8"))
                if idx in drawings_xml:
                    sheet_rel = _build_rels_xml(
                        [
                            (
                                "rId1",
                                "http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing",
                                f"../drawings/drawing{idx}.xml",
                            )
                        ]
                    )
                    zf.writestr(f"xl/worksheets/_rels/sheet{idx}.xml.rels", sheet_rel.encode("utf-8"))

            for d_idx in sorted(drawings_xml):
                zf.writestr(f"xl/drawings/drawing{d_idx}.xml", drawings_xml[d_idx].encode("utf-8"))
                zf.writestr(
                    f"xl/drawings/_rels/drawing{d_idx}.xml.rels",
                    _build_rels_xml(drawing_rels[d_idx]).encode("utf-8"),
                )

            for c_idx in sorted(charts_xml):
                zf.writestr(f"xl/charts/chart{c_idx}.xml", charts_xml[c_idx].encode("utf-8"))

        return zip_buf.getvalue()
