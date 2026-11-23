"""
Trustworthy Credit Data Architecture for ChronoX Finance (Proposal A §7 + Gate 1 #53).

Provides:
1. `CreditProvider` protocol & normalized credit dataclasses (`CreditImportBundle`,
   `CreditReportRecord`, `CreditInquiryRecord`, `CreditDerogatoryRecord`, `CreditTradelineRecord`).
2. Kind-dependent FCRA drop-off date calculator (`compute_drop_off_date`, `compute_inquiry_dates`):
   - Late payments (30/60/90/120), charge-offs, collections, Chapter 13 BK, foreclosure, repo: +7 years (+84m)
   - Chapter 7 bankruptcy: +10 years (+120m)
   - Hard inquiries: +12m FICO scoring impact (`counts_until`), +24m report drop-off (`drops_off`)
3. `PlaidLiabilitiesAdapter`: maps Plaid `/liabilities/get` directly into credit line payloads.
4. `OfficialCreditReportImporter`: primary structured JSON/manual parser + best-effort
   AnnualCreditReport.com Experian/Equifax/TransUnion pure-Python PDF extractor with
   mandatory `needs_review=True` preview payload.
5. `BureauAPIAdapter`: clean future SaaS interface for CRS / Array / Spinwheel returning
   honest `NotConfiguredForLocalDesktop` guidance on local desktop installs.
"""

from __future__ import annotations

import calendar
import io
import json
import logging
import re
import zlib
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple, Union

from chronox_finance.connectors.base import (
    ConnectorError,
    NormalizedLiability,
    NotConfiguredForLocalDesktop,
    apr_pct_to_bps,
    decimal_to_cents,
)
from chronox_finance.security import sanitize_untrusted_text

logger = logging.getLogger(__name__)

VALID_BUREAUS = frozenset({"experian", "equifax", "transunion", "other"})
VALID_MODELS = frozenset({"fico8", "vantage3", "fico9", "fico10t", "other"})

# FCRA retention periods in calendar months (per Gate 1 #53)
DEROGATORY_RETENTION_MONTHS: Dict[str, int] = {
    "late_30": 84,
    "late_60": 84,
    "late_90": 84,
    "late_120": 84,
    "late_payment": 84,
    "charge_off": 84,
    "collection": 84,
    "collections": 84,
    "bankruptcy_ch13": 84,
    "foreclosure": 84,
    "repossession": 84,
    "settled_less": 84,
    "tax_lien": 84,
    "bankruptcy_ch7": 120,  # Chapter 7 stays 10 years
    "hard_inquiry": 24,
    "inquiry": 24,
}

INQUIRY_SCORING_IMPACT_MONTHS = 12
INQUIRY_REPORT_DROP_OFF_MONTHS = 24


# ---------------------------------------------------------------------------
# Exact Calendar Date Arithmetic & Kind-Dependent Drop-Off Rules
# ---------------------------------------------------------------------------


def add_months(iso_date: str, months: int) -> str:
    """Add exact calendar months to an ISO `YYYY-MM-DD` date, clamping end-of-month cleanly."""
    dt = date.fromisoformat(iso_date[:10])
    total_months = (dt.year * 12 + (dt.month - 1)) + int(months)
    year, zero_month = divmod(total_months, 12)
    month = zero_month + 1
    last_day = calendar.monthrange(year, month)[1]
    day = min(dt.day, last_day)
    return date(year, month, day).isoformat()


def normalize_derogatory_kind(raw_kind: str) -> str:
    """Normalize diverse bureau / user derogatory labels into canonical ChronoX kind tokens."""
    val = re.sub(r"[^a-z0-9]+", "_", (raw_kind or "").strip().lower()).strip("_")
    aliases = {
        "ch7": "bankruptcy_ch7",
        "chapter_7": "bankruptcy_ch7",
        "chapter_7_bankruptcy": "bankruptcy_ch7",
        "bankruptcy_chapter_7": "bankruptcy_ch7",
        "bk_ch7": "bankruptcy_ch7",
        "ch13": "bankruptcy_ch13",
        "chapter_13": "bankruptcy_ch13",
        "chapter_13_bankruptcy": "bankruptcy_ch13",
        "bankruptcy_chapter_13": "bankruptcy_ch13",
        "bk_ch13": "bankruptcy_ch13",
        "chargeoff": "charge_off",
        "charged_off": "charge_off",
        "co": "charge_off",
        "collections": "collection",
        "collection_account": "collection",
        "30_day_late": "late_30",
        "60_day_late": "late_60",
        "90_day_late": "late_90",
        "120_day_late": "late_120",
        "late": "late_payment",
        "repo": "repossession",
    }
    canonical = aliases.get(val, val)
    return canonical if canonical in DEROGATORY_RETENTION_MONTHS else "late_payment"


def compute_drop_off_date(kind: str, event_date: str) -> str:
    """
    Compute exact FCRA report drop-off date based on derogatory/inquiry kind:
    - `bankruptcy_ch7`: +10 years (+120 months)
    - `late_*`, `charge_off`, `collection`, `bankruptcy_ch13`, `foreclosure`, `repossession`: +7 years (+84 months)
    - `hard_inquiry` / `inquiry`: +24 months
    """
    canonical = normalize_derogatory_kind(kind)
    months = DEROGATORY_RETENTION_MONTHS.get(canonical, 84)
    return add_months(event_date, months)


def compute_inquiry_dates(inquiry_date: str) -> Tuple[str, str]:
    """Return `(counts_until_12m, drops_off_24m)` ISO dates for a hard inquiry."""
    counts_until = add_months(inquiry_date, INQUIRY_SCORING_IMPACT_MONTHS)
    drops_off = add_months(inquiry_date, INQUIRY_REPORT_DROP_OFF_MONTHS)
    return counts_until, drops_off


# ---------------------------------------------------------------------------
# Normalized Credit Records & Protocol
# ---------------------------------------------------------------------------


@dataclass
class CreditReportRecord:
    bureau: str  # experian | equifax | transunion | other
    pulled_date: str
    model: str = "fico8"  # fico8 | vantage3 | fico9 | other
    score: Optional[int] = None
    source: str = "manual_import"
    needs_review: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CreditInquiryRecord:
    lender: str
    bureau: str
    inquiry_date: str
    counts_until: str = ""
    drops_off: str = ""
    inquiry_type: str = "hard"

    def __post_init__(self) -> None:
        self.lender = sanitize_untrusted_text(self.lender, max_len=80) or "Unknown Lender"
        self.bureau = _normalize_bureau(self.bureau)
        c_until, d_off = compute_inquiry_dates(self.inquiry_date)
        if not self.counts_until:
            self.counts_until = c_until
        if not self.drops_off:
            self.drops_off = d_off

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CreditDerogatoryRecord:
    kind: str
    opened_date: str  # Date of first delinquency (DOFD) or bankruptcy filing date
    drops_off: str = ""
    creditor: str = ""
    lender: str = ""
    bureau: str = "other"
    amount_cents: Optional[int] = None
    active: bool = True

    def __post_init__(self) -> None:
        self.kind = normalize_derogatory_kind(self.kind)
        primary_name = sanitize_untrusted_text(self.creditor or self.lender, max_len=80)
        self.creditor = primary_name
        self.lender = primary_name
        self.bureau = _normalize_bureau(self.bureau)
        if not self.drops_off:
            self.drops_off = compute_drop_off_date(self.kind, self.opened_date)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CreditTradelineRecord:
    name: str
    issuer: str = ""
    kind: str = "credit_card"  # credit_card | auto_loan | student_loan | mortgage | personal_loan | bnpl
    balance_cents: int = 0
    credit_limit_cents: int = 0
    apr_bps: int = 0
    cash_advance_apr_bps: Optional[int] = None
    balance_transfer_apr_bps: Optional[int] = None
    min_payment_cents: int = 0
    statement_balance_cents: Optional[int] = None
    statement_closing_day: int = 15
    payment_due_day: int = 10
    next_due_date: Optional[str] = None
    opened_date: Optional[str] = None
    is_overdue: bool = False
    is_authorized_user: bool = False
    status: str = "open"
    external_account_id: str = ""
    source: str = "manual_import"

    def to_credit_line_payload(self) -> Dict[str, Any]:
        """Convert to a payload compatible with ChronoX `CreditLineCreate` / `credit_lines`."""
        return {
            "name": self.name[:80],
            "issuer": self.issuer[:60],
            "kind": self.kind,
            "balance_cents": max(0, int(self.balance_cents)),
            "credit_limit_cents": max(0, int(self.credit_limit_cents)),
            "apr_bps": max(0, min(int(self.apr_bps), 10000)),
            "min_payment_cents": max(0, int(self.min_payment_cents)),
            "statement_closing_day": max(1, min(int(self.statement_closing_day), 31)),
            "payment_due_day": max(1, min(int(self.payment_due_day), 31)),
            "opened_date": self.opened_date,
            "statement_balance_cents": self.statement_balance_cents,
            "next_due_date": self.next_due_date,
            "is_overdue": self.is_overdue,
            "is_authorized_user": self.is_authorized_user,
            "external_account_id": self.external_account_id,
            "source": self.source,
        }

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CreditImportBundle:
    provider: str
    needs_review: bool = False
    reports: List[CreditReportRecord] = field(default_factory=list)
    tradelines: List[CreditTradelineRecord] = field(default_factory=list)
    inquiries: List[CreditInquiryRecord] = field(default_factory=list)
    derogatories: List[CreditDerogatoryRecord] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    raw_source_format: str = "structured"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "needs_review": self.needs_review,
            "raw_source_format": self.raw_source_format,
            "reports": [r.to_dict() for r in self.reports],
            "tradelines": [t.to_dict() for t in self.tradelines],
            "credit_line_payloads": [t.to_credit_line_payload() for t in self.tradelines],
            "inquiries": [i.to_dict() for i in self.inquiries],
            "derogatories": [d.to_dict() for d in self.derogatories],
            "warnings": list(self.warnings),
        }


class CreditProvider(Protocol):
    """Protocol implemented by all credit data sources."""

    provider_name: str

    def fetch_credit_snapshot(self, *args: Any, **kwargs: Any) -> CreditImportBundle: ...


def _normalize_bureau(raw: str) -> str:
    val = (raw or "").strip().lower()
    if "experian" in val or val == "exp":
        return "experian"
    if "equifax" in val or val == "efx":
        return "equifax"
    if "transunion" in val or "trans union" in val or val == "tu":
        return "transunion"
    return val if val in VALID_BUREAUS else "other"


def _normalize_model(raw: str) -> str:
    val = re.sub(r"[^a-z0-9]+", "", (raw or "").strip().lower())
    if val in ("fico", "fico8", "ficoscore8"):
        return "fico8"
    if val in ("vantage", "vantage3", "vantagescore3", "vantagescore30"):
        return "vantage3"
    if val in ("fico9", "ficoscore9"):
        return "fico9"
    if val in ("fico10t", "fico10"):
        return "fico10t"
    return "other"


def _normalize_debt_kind(raw: str) -> str:
    val = (raw or "").strip().lower()
    if any(k in val for k in ("credit", "revolving", "visa", "mastercard", "amex", "card")):
        return "credit_card"
    if "mortgage" in val or "real estate" in val or "home loan" in val:
        return "mortgage"
    if "student" in val or "education" in val:
        return "student_loan"
    if "auto" in val or "vehicle" in val or "car loan" in val:
        return "auto_loan"
    if "bnpl" in val or "installment" in val or "personal" in val:
        return "personal_loan"
    return "credit_card"


def _day_from_iso(iso_str: Optional[str], default_day: int) -> int:
    if not iso_str:
        return default_day
    try:
        return date.fromisoformat(iso_str[:10]).day
    except ValueError:
        return default_day


def _parse_flexible_date(raw: Any) -> Optional[str]:
    text = str(raw or "").strip()
    if not text:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        try:
            return date.fromisoformat(text[:10]).isoformat()
        except ValueError:
            return None
    cleaned = re.sub(r"\s+", " ", text.replace(".", "")).strip()
    cleaned = re.sub(r"\bSept\b", "Sep", cleaned, flags=re.IGNORECASE)
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%b %d, %Y", "%B %d, %Y", "%m-%d-%Y", "%Y/%m/%d", "%m/%Y"):
        try:
            dt = datetime.strptime(cleaned, fmt).date()
            return dt.isoformat()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# 1. Plaid Liabilities Adapter
# ---------------------------------------------------------------------------


class PlaidLiabilitiesAdapter:
    """Maps Plaid `/liabilities/get` records directly into ChronoX credit line payloads."""

    provider_name = "plaid_liabilities"

    def adapt_liabilities(
        self,
        liabilities: Sequence[NormalizedLiability],
    ) -> CreditImportBundle:
        tradelines: List[CreditTradelineRecord] = []
        warnings: List[str] = []

        for item in liabilities:
            closing_day = _day_from_iso(item.last_statement_issue_date, 15)
            due_day = _day_from_iso(item.next_payment_due_date, 10)
            tradelines.append(
                CreditTradelineRecord(
                    name=sanitize_untrusted_text(item.name, max_len=80) or "Credit Account",
                    issuer=sanitize_untrusted_text(item.issuer, max_len=60),
                    kind=_normalize_debt_kind(item.kind),
                    balance_cents=max(0, int(item.balance_cents)),
                    credit_limit_cents=max(0, int(item.credit_limit_cents)),
                    apr_bps=max(0, int(item.apr_bps)),
                    cash_advance_apr_bps=item.cash_advance_apr_bps,
                    balance_transfer_apr_bps=item.balance_transfer_apr_bps,
                    min_payment_cents=max(0, int(item.min_payment_cents)),
                    statement_balance_cents=item.statement_balance_cents,
                    statement_closing_day=closing_day,
                    payment_due_day=due_day,
                    next_due_date=item.next_payment_due_date,
                    opened_date=item.origination_date,
                    is_overdue=bool(item.is_overdue),
                    external_account_id=item.external_account_id,
                    source=self.provider_name,
                )
            )
            if item.is_overdue:
                warnings.append(f"Overdue balance detected on '{item.name}'.")

        return CreditImportBundle(
            provider=self.provider_name,
            needs_review=False,
            tradelines=tradelines,
            warnings=warnings,
            raw_source_format="plaid_liabilities",
        )

    def to_credit_line_payloads(
        self,
        liabilities: Sequence[NormalizedLiability],
    ) -> List[Dict[str, Any]]:
        """Convenience helper returning `CreditLineCreate`-compatible payload dicts."""
        bundle = self.adapt_liabilities(liabilities)
        return [t.to_credit_line_payload() for t in bundle.tradelines]

    def fetch_credit_snapshot(
        self,
        plaid_client: Any,
        access_token: str,
        *,
        institution: str = "",
    ) -> CreditImportBundle:
        """Fetch live liabilities via an injected `PlaidClient` and map into `CreditImportBundle`."""
        liabilities = plaid_client.get_liabilities(access_token, institution=institution)
        return self.adapt_liabilities(liabilities)


# ---------------------------------------------------------------------------
# 2. Official Credit Report Importer (Structured JSON/Manual + Best-Effort PDF)
# ---------------------------------------------------------------------------


def _unescape_pdf_literal(raw: bytes) -> str:
    out = bytearray()
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == 92 and i + 1 < n:
            nxt = raw[i + 1]
            escapes = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12}
            if nxt in escapes:
                out.append(escapes[nxt])
                i += 2
            elif nxt in (ord("("), ord(")"), ord("\\")):
                out.append(nxt)
                i += 2
            elif 48 <= nxt <= 55:
                m = re.match(rb"^[0-7]{1,3}", raw[i + 1 : i + 4])
                if m:
                    out.append(int(m.group(0), 8) & 0xFF)
                    i += 1 + len(m.group(0))
                else:
                    i += 2
            elif nxt in (10, 13):
                i += 2
            else:
                out.append(nxt)
                i += 2
        else:
            out.append(ch)
            i += 1
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        return out.decode("latin-1", errors="replace")


def _extract_pdf_text_pure_python(raw_bytes: bytes) -> str:
    """Zero-dependency pure-Python PDF text extractor supporting uncompressed & FlateDecode streams."""
    streams: List[bytes] = []
    for match in re.finditer(rb"stream\r?\n([\s\S]*?)\r?\nendstream", raw_bytes):
        chunk = match.group(1)
        if not chunk:
            continue
        try:
            streams.append(zlib.decompress(chunk))
        except zlib.error:
            streams.append(chunk)

    lines: List[str] = []
    for stream in streams:
        if b"BT" not in stream and b"Tj" not in stream and b"TJ" not in stream:
            continue
        blocks = re.findall(rb"BT([\s\S]*?)ET", stream) or [stream]
        for blk in blocks:
            parts: List[str] = []
            for tok in re.finditer(
                rb"\((?:\\.|[^\\()])*\)\s*Tj|\[([\s\S]*?)\]\s*TJ|\bT\*|\bTd\b|\bTD\b|\bTm\b",
                blk,
            ):
                full = tok.group(0)
                if full.endswith(b"Tj"):
                    inner = full[1 : full.rfind(b")")]
                    parts.append(_unescape_pdf_literal(inner))
                elif full.endswith(b"TJ"):
                    arr = tok.group(1) or b""
                    seg = "".join(
                        _unescape_pdf_literal(m.group(1))
                        for m in re.finditer(rb"\(((?:\\.|[^\\()])*)\)", arr)
                    )
                    if seg:
                        parts.append(seg)
                else:
                    if parts:
                        line = " ".join(p.strip() for p in parts if p.strip()).strip()
                        if line:
                            lines.append(line)
                        parts = []
            if parts:
                line = " ".join(p.strip() for p in parts if p.strip()).strip()
                if line:
                    lines.append(line)
    return "\n".join(lines)


def _extract_pdf_text(raw_bytes: bytes) -> str:
    try:
        import pypdf  # type: ignore[import-not-found]

        reader = pypdf.PdfReader(io.BytesIO(raw_bytes))
        combined = "\n".join((p.extract_text() or "") for p in reader.pages).strip()
        if combined:
            return combined
    except Exception as exc:
        logger.debug("Optional pypdf unavailable; using pure-Python stream parser: %s", exc)
    return _extract_pdf_text_pure_python(raw_bytes)


_PDF_PULL_DATE_RE = re.compile(
    r"(?:report\s+date|date\s+generated|prepared\s+on|pulled\s+on|as\s+of)\s*[:\-]?\s*"
    r"(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}|[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)
_PDF_SCORE_RE = re.compile(
    r"\b(FICO\s*(?:Score\s*)?8|FICO\s*(?:Score\s*)?9|VantageScore\s*3(?:\.0)?)\b[^\n\r]{0,30}?\b([3-8]\d{2})\b",
    re.IGNORECASE,
)
_PDF_INQUIRY_LINE_RE = re.compile(
    r"^(?:inquiry\s*[:\-]\s*)?(?P<lender>[A-Za-z0-9&'.,/\- ]{2,60}?)\s+"
    r"(?:date\s*[:\-]?\s*)?(?P<dt>\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}|[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4})"
    r"(?:\s+.*)?$",
    re.IGNORECASE,
)
_PDF_DEROG_RE = re.compile(
    r"\b(?P<kind>chapter\s*7\s*bankruptcy|bankruptcy\s*chapter\s*7|ch\.?\s*7\s*bankruptcy|"
    r"chapter\s*13\s*bankruptcy|bankruptcy\s*chapter\s*13|ch\.?\s*13\s*bankruptcy|"
    r"charge[\s-]?off|collection(?:\s+account)?|foreclosure|repossession|"
    r"(?:30|60|90|120)[\s-]?days?\s+late|late\s+(?:30|60|90|120))\b",
    re.IGNORECASE,
)
_PDF_DATE_ANY_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)
_PDF_MONEY_RE = re.compile(r"\$\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})?|[0-9]+(?:\.[0-9]{2})?)")


class OfficialCreditReportImporter:
    """
    Primary structured/JSON/manual credit report importer + best-effort
    AnnualCreditReport.com Experian/Equifax/TransUnion PDF extractor.

    Per Gate 1 #53:
    - Structured manual/JSON input is the primary high-trust path.
    - PDF extraction is best-effort and ALWAYS returns `needs_review=True` so the user
      verifies parsed entries in a review screen before writing to the ledger.
    """

    provider_name = "official_credit_report"

    def import_structured(
        self,
        payload: Union[Dict[str, Any], str],
        *,
        needs_review: bool = False,
    ) -> CreditImportBundle:
        """Parse a structured JSON string or manual-entry dict into a `CreditImportBundle`."""
        if isinstance(payload, str):
            try:
                data = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise ConnectorError(
                    f"Invalid credit report JSON payload: {exc}",
                    provider=self.provider_name,
                ) from exc
        else:
            data = payload

        if not isinstance(data, dict):
            raise ConnectorError("Credit report payload must be a JSON object", provider=self.provider_name)

        default_bureau = _normalize_bureau(str(data.get("bureau") or "other"))
        default_pulled = _parse_flexible_date(data.get("pulled_date")) or date.today().isoformat()
        warnings: List[str] = []

        reports: List[CreditReportRecord] = []
        raw_reports = data.get("reports")
        if isinstance(raw_reports, list) and raw_reports:
            for item in raw_reports:
                if not isinstance(item, dict):
                    continue
                rec = self._parse_report_obj(item, default_bureau, default_pulled, needs_review, warnings)
                if rec:
                    reports.append(rec)
        elif data.get("score") is not None or data.get("bureau"):
            rec = self._parse_report_obj(data, default_bureau, default_pulled, needs_review, warnings)
            if rec:
                reports.append(rec)

        tradelines: List[CreditTradelineRecord] = []
        raw_tradelines = data.get("tradelines") or data.get("accounts") or data.get("credit_lines") or []
        for item in raw_tradelines:
            if not isinstance(item, dict):
                continue
            name = sanitize_untrusted_text(str(item.get("name") or item.get("creditor") or ""), max_len=80)
            if not name:
                continue
            bal_cents = (
                int(item["balance_cents"])
                if item.get("balance_cents") is not None
                else abs(decimal_to_cents(item.get("balance")))
            )
            limit_cents = (
                int(item["credit_limit_cents"])
                if item.get("credit_limit_cents") is not None
                else abs(decimal_to_cents(item.get("credit_limit") or item.get("limit")))
            )
            apr_bps = (
                int(item["apr_bps"])
                if item.get("apr_bps") is not None
                else apr_pct_to_bps(item.get("apr") or item.get("apr_pct"))
            )
            min_pmt_cents = (
                int(item["min_payment_cents"])
                if item.get("min_payment_cents") is not None
                else abs(decimal_to_cents(item.get("min_payment")))
            )
            opened = _parse_flexible_date(item.get("opened_date"))
            tradelines.append(
                CreditTradelineRecord(
                    name=name,
                    issuer=sanitize_untrusted_text(str(item.get("issuer") or name), max_len=60),
                    kind=_normalize_debt_kind(str(item.get("kind") or item.get("account_type") or "credit_card")),
                    balance_cents=max(0, bal_cents),
                    credit_limit_cents=max(0, limit_cents),
                    apr_bps=max(0, apr_bps),
                    min_payment_cents=max(0, min_pmt_cents),
                    statement_closing_day=int(item.get("statement_closing_day") or 15),
                    payment_due_day=int(item.get("payment_due_day") or 10),
                    opened_date=opened,
                    is_overdue=bool(item.get("is_overdue", False)),
                    is_authorized_user=bool(item.get("is_authorized_user", False)),
                    status=str(item.get("status") or "open").lower(),
                    source="structured_credit_report",
                )
            )

        inquiries: List[CreditInquiryRecord] = []
        for item in data.get("inquiries") or []:
            if not isinstance(item, dict):
                continue
            inq_date = _parse_flexible_date(item.get("inquiry_date") or item.get("date"))
            if not inq_date:
                warnings.append(f"Skipped inquiry missing valid date: {item.get('lender')!r}")
                continue
            inquiries.append(
                CreditInquiryRecord(
                    lender=str(item.get("lender") or item.get("creditor") or "Unknown Lender"),
                    bureau=str(item.get("bureau") or default_bureau),
                    inquiry_date=inq_date,
                    counts_until=_parse_flexible_date(item.get("counts_until")) or "",
                    drops_off=_parse_flexible_date(item.get("drops_off")) or "",
                    inquiry_type=str(item.get("inquiry_type") or "hard").lower(),
                )
            )

        derogatories: List[CreditDerogatoryRecord] = []
        for item in data.get("derogatories") or []:
            if not isinstance(item, dict):
                continue
            opened = _parse_flexible_date(
                item.get("opened_date") or item.get("dofd") or item.get("filing_date") or item.get("date")
            )
            if not opened:
                warnings.append(f"Skipped derogatory item missing valid date: {item.get('kind')!r}")
                continue
            amt_cents: Optional[int] = None
            if item.get("amount_cents") is not None:
                amt_cents = abs(int(item["amount_cents"]))
            elif item.get("amount") is not None:
                amt_cents = abs(decimal_to_cents(item.get("amount")))
            derogatories.append(
                CreditDerogatoryRecord(
                    kind=str(item.get("kind") or "late_payment"),
                    opened_date=opened,
                    drops_off=_parse_flexible_date(item.get("drops_off")) or "",
                    creditor=str(item.get("creditor") or item.get("lender") or ""),
                    bureau=str(item.get("bureau") or default_bureau),
                    amount_cents=amt_cents,
                    active=bool(item.get("active", True)),
                )
            )

        return CreditImportBundle(
            provider=self.provider_name,
            needs_review=bool(needs_review),
            reports=reports,
            tradelines=tradelines,
            inquiries=inquiries,
            derogatories=derogatories,
            warnings=warnings,
            raw_source_format="structured_json",
        )

    def _parse_report_obj(
        self,
        item: Dict[str, Any],
        default_bureau: str,
        default_pulled: str,
        needs_review: bool,
        warnings: List[str],
    ) -> Optional[CreditReportRecord]:
        bureau = _normalize_bureau(str(item.get("bureau") or default_bureau))
        pulled = _parse_flexible_date(item.get("pulled_date") or item.get("date")) or default_pulled
        model = _normalize_model(str(item.get("model") or "fico8"))
        score_raw = item.get("score")
        score: Optional[int] = None
        if score_raw is not None and str(score_raw).strip() != "":
            try:
                parsed_score = int(score_raw)
                if 300 <= parsed_score <= 850:
                    score = parsed_score
                else:
                    warnings.append(f"Ignored out-of-range bureau score {parsed_score} (expected 300..850).")
            except (TypeError, ValueError):
                warnings.append(f"Ignored non-integer bureau score {score_raw!r}.")
        return CreditReportRecord(
            bureau=bureau,
            model=model,
            score=score,
            pulled_date=pulled,
            source=str(item.get("source") or "manual_structured"),
            needs_review=needs_review,
        )

    def import_pdf(
        self,
        raw_bytes: bytes,
        *,
        bureau_hint: str = "auto",
    ) -> CreditImportBundle:
        """
        Best-effort local extractor for AnnualCreditReport.com Experian / Equifax / TransUnion PDFs.

        ALWAYS returns `needs_review=True` with a review warning because bureau PDF layouts
        evolve frequently and users must verify extracted tradelines, inquiries, and derogatories.
        """
        text = _extract_pdf_text(raw_bytes)
        warnings: List[str] = [
            "Best-effort AnnualCreditReport PDF extraction: bureau PDF layouts drift over time. "
            "Review and confirm every extracted tradeline, inquiry, and derogatory before saving."
        ]

        if not text.strip():
            warnings.append(
                "No embedded text found in PDF (scanned image PDFs cannot be parsed without OCR). "
                "Use the primary Structured / Manual Entry form instead."
            )
            return CreditImportBundle(
                provider=self.provider_name,
                needs_review=True,
                warnings=warnings,
                raw_source_format="annual_credit_report_pdf",
            )

        detected_bureau = self._detect_bureau(text, bureau_hint)
        pull_match = _PDF_PULL_DATE_RE.search(text)
        pulled_date = (
            _parse_flexible_date(pull_match.group(1))
            if pull_match
            else None
        ) or date.today().isoformat()

        reports: List[CreditReportRecord] = []
        score_match = _PDF_SCORE_RE.search(text)
        if score_match:
            reports.append(
                CreditReportRecord(
                    bureau=detected_bureau,
                    model=_normalize_model(score_match.group(1)),
                    score=int(score_match.group(2)),
                    pulled_date=pulled_date,
                    source="annual_credit_report_pdf",
                    needs_review=True,
                )
            )
        else:
            reports.append(
                CreditReportRecord(
                    bureau=detected_bureau,
                    model="other",
                    score=None,
                    pulled_date=pulled_date,
                    source="annual_credit_report_pdf",
                    needs_review=True,
                )
            )
            warnings.append(
                "AnnualCreditReport.com statutory PDFs usually omit numeric FICO scores; "
                "add your actual bureau score manually if desired."
            )

        tradelines, inquiries, derogatories = self._parse_pdf_sections(text, detected_bureau)
        return CreditImportBundle(
            provider=self.provider_name,
            needs_review=True,  # Mandatory review screen per Gate 1 #53
            reports=reports,
            tradelines=tradelines,
            inquiries=inquiries,
            derogatories=derogatories,
            warnings=warnings,
            raw_source_format="annual_credit_report_pdf",
        )

    def _detect_bureau(self, text: str, bureau_hint: str) -> str:
        hint = _normalize_bureau(bureau_hint)
        if bureau_hint and bureau_hint.lower() != "auto" and hint != "other":
            return hint
        head = text[:3000].lower()
        scores = {
            "experian": head.count("experian"),
            "equifax": head.count("equifax"),
            "transunion": head.count("transunion") + head.count("trans union"),
        }
        best_bureau, best_hits = max(scores.items(), key=lambda kv: kv[1])
        return best_bureau if best_hits > 0 else "other"

    def _parse_pdf_sections(
        self,
        text: str,
        bureau: str,
    ) -> Tuple[List[CreditTradelineRecord], List[CreditInquiryRecord], List[CreditDerogatoryRecord]]:
        tradelines: List[CreditTradelineRecord] = []
        inquiries: List[CreditInquiryRecord] = []
        derogatories: List[CreditDerogatoryRecord] = []

        section = "general"
        current_tl: Optional[Dict[str, Any]] = None

        def flush_tradeline() -> None:
            nonlocal current_tl
            if not current_tl or not current_tl.get("name"):
                current_tl = None
                return
            tradelines.append(
                CreditTradelineRecord(
                    name=sanitize_untrusted_text(str(current_tl["name"]), max_len=80),
                    issuer=sanitize_untrusted_text(str(current_tl["name"]), max_len=60),
                    kind=_normalize_debt_kind(str(current_tl.get("kind") or "credit_card")),
                    balance_cents=int(current_tl.get("balance_cents") or 0),
                    credit_limit_cents=int(current_tl.get("credit_limit_cents") or 0),
                    opened_date=current_tl.get("opened_date"),
                    is_authorized_user=bool(current_tl.get("is_authorized_user", False)),
                    status=str(current_tl.get("status") or "open"),
                    source="annual_credit_report_pdf",
                )
            )
            current_tl = None

        for raw_line in text.splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip()
            if not line:
                continue
            low = line.lower()

            # Section transitions
            if any(
                hdr in low
                for hdr in (
                    "requests viewed by others",
                    "hard inquiries",
                    "regular inquiries",
                    "inquiries that impact",
                )
            ):
                flush_tradeline()
                section = "hard_inquiries"
                continue
            if any(hdr in low for hdr in ("requests viewed only by you", "soft inquiries", "promotional inquiries")):
                flush_tradeline()
                section = "soft_inquiries"
                continue
            if any(hdr in low for hdr in ("public records", "negative items", "adverse accounts", "collections")):
                flush_tradeline()
                section = "derogatories"
                continue
            if any(hdr in low for hdr in ("revolving accounts", "accounts", "credit accounts", "tradelines")):
                if len(line) <= 45:
                    flush_tradeline()
                    section = "tradelines"
                    continue

            # Check for explicit derogatory records anywhere except soft inquiries
            if section != "soft_inquiries":
                derog_m = _PDF_DEROG_RE.search(line)
                if derog_m:
                    dt_m = _PDF_DATE_ANY_RE.search(line)
                    parsed_dt = _parse_flexible_date(dt_m.group(1)) if dt_m else None
                    if parsed_dt:
                        money_m = _PDF_MONEY_RE.search(line)
                        amt_c = decimal_to_cents(money_m.group(1)) if money_m else None
                        creditor = ""
                        if current_tl and current_tl.get("name"):
                            creditor = str(current_tl["name"])
                        else:
                            # Text before the derogatory phrase
                            prefix = line[: derog_m.start()].strip(" :-|")
                            if 2 <= len(prefix) <= 60:
                                creditor = prefix
                        derogatories.append(
                            CreditDerogatoryRecord(
                                kind=derog_m.group("kind"),
                                opened_date=parsed_dt,
                                creditor=creditor,
                                bureau=bureau,
                                amount_cents=amt_c,
                                active=True,
                            )
                        )
                        continue

            if section == "hard_inquiries":
                inq_m = _PDF_INQUIRY_LINE_RE.match(line)
                if inq_m:
                    inq_dt = _parse_flexible_date(inq_m.group("dt"))
                    lender = inq_m.group("lender").strip(" :-|")
                    if inq_dt and lender and not lender.lower().startswith(("inquiry date", "subscriber")):
                        inquiries.append(
                            CreditInquiryRecord(
                                lender=lender,
                                bureau=bureau,
                                inquiry_date=inq_dt,
                                inquiry_type="hard",
                            )
                        )
                continue

            if section == "tradelines":
                acct_m = re.match(r"^(?:account\s+name|creditor)\s*[:\-]\s*(.+)$", line, re.IGNORECASE)
                if acct_m:
                    flush_tradeline()
                    current_tl = {"name": acct_m.group(1).strip()}
                    continue
                if current_tl is not None:
                    if re.search(r"\bauthorized\s+user\b", low):
                        current_tl["is_authorized_user"] = True
                    type_m = re.match(r"^(?:account\s+)?type\s*[:\-]\s*(.+)$", line, re.IGNORECASE)
                    if type_m:
                        current_tl["kind"] = type_m.group(1).strip()
                    bal_m = re.search(
                        r"(?:current\s+)?balance\s*[:\-]?\s*\$\s*([0-9,]+(?:\.[0-9]{2})?)",
                        line,
                        re.IGNORECASE,
                    )
                    if bal_m:
                        current_tl["balance_cents"] = decimal_to_cents(bal_m.group(1))
                    lim_m = re.search(
                        r"(?:credit\s+limit|high\s+balance)\s*[:\-]?\s*\$\s*([0-9,]+(?:\.[0-9]{2})?)",
                        line,
                        re.IGNORECASE,
                    )
                    if lim_m:
                        current_tl["credit_limit_cents"] = decimal_to_cents(lim_m.group(1))
                    open_m = re.search(
                        r"(?:date\s+opened|opened)\s*[:\-]?\s*(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}|[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4})",
                        line,
                        re.IGNORECASE,
                    )
                    if open_m:
                        current_tl["opened_date"] = _parse_flexible_date(open_m.group(1))
                    if re.search(r"\bstatus\s*[:\-]\s*closed\b", low):
                        current_tl["status"] = "closed"

        flush_tradeline()
        return tradelines, inquiries, derogatories

    def import_auto(
        self,
        content: Union[bytes, str, Dict[str, Any]],
        *,
        filename: str = "",
        bureau_hint: str = "auto",
    ) -> CreditImportBundle:
        """Auto-detect structured JSON/dict vs AnnualCreditReport PDF and parse cleanly."""
        if isinstance(content, dict):
            return self.import_structured(content)
        ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
        if isinstance(content, (bytes, bytearray)):
            raw = bytes(content)
            if ext == "pdf" or raw[:5] == b"%PDF-":
                return self.import_pdf(raw, bureau_hint=bureau_hint)
            return self.import_structured(raw.decode("utf-8-sig", errors="replace"))
        if ext == "pdf" or content.lstrip().startswith("%PDF-"):
            return self.import_pdf(content.encode("latin-1", errors="ignore"), bureau_hint=bureau_hint)
        return self.import_structured(content)

    def fetch_credit_snapshot(
        self,
        content: Union[bytes, str, Dict[str, Any]],
        *,
        filename: str = "",
        bureau_hint: str = "auto",
    ) -> CreditImportBundle:
        """Alias satisfying `CreditProvider` protocol."""
        return self.import_auto(content, filename=filename, bureau_hint=bureau_hint)


# ---------------------------------------------------------------------------
# 3. Bureau API Adapter (Clean Future SaaS Seam: CRS / Array / Spinwheel)
# ---------------------------------------------------------------------------


class BureauAPIAdapter:
    """
    Future SaaS credit-bureau adapter interface for CRS Credit API, Array, or Spinwheel.

    Direct tri-merge bureau APIs require B2B commercial credentialing, permissible-purpose
    audits, and FCRA compliance infrastructure — they cannot be embedded in a local-first
    desktop binary. On local desktop builds this adapter raises `NotConfiguredForLocalDesktop`
    with honest, actionable guidance.
    """

    SUPPORTED_VENDORS = ("crs", "array", "spinwheel")

    def __init__(
        self,
        vendor: str = "crs",
        *,
        api_base_url: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
    ) -> None:
        normalized = (vendor or "crs").strip().lower()
        if normalized not in self.SUPPORTED_VENDORS:
            raise ValueError(f"Unsupported bureau adapter '{vendor}'; choose from {self.SUPPORTED_VENDORS}")
        self.vendor = normalized
        self.provider_name = f"bureau_api_{normalized}"
        self._api_base_url = (api_base_url or "").strip()
        self._client_id = (client_id or "").strip()
        self._client_secret = (client_secret or "").strip()

    def is_configured(self) -> bool:
        return bool(self._api_base_url and self._client_id and self._client_secret)

    def capabilities(self) -> Dict[str, Any]:
        return {
            "vendor": self.vendor,
            "configured": self.is_configured(),
            "local_desktop_supported": False,
            "requires_fcra_saas_backend": True,
            "recommended_local_alternatives": [
                "Plaid Liabilities (/liabilities/get) for live credit cards, APRs, and loans",
                "Official AnnualCreditReport.com PDF or Structured Manual Form for bureau scores, inquiries, and derogatories",
            ],
        }

    def fetch_credit_snapshot(self, *args: Any, **kwargs: Any) -> CreditImportBundle:
        raise NotConfiguredForLocalDesktop(
            f"Direct bureau API '{self.vendor.upper()}' is a B2B/FCRA-gated SaaS integration and is "
            "not configured for local desktop installations. Use Plaid Liabilities for live card/loan "
            "balances & APRs and the Official Credit Report Structured Form / AnnualCreditReport.com "
            "importer for bureau scores, hard inquiries, and derogatory drop-off tracking.",
            provider=self.provider_name,
        )

    def pull_tri_merge(self, *args: Any, **kwargs: Any) -> CreditImportBundle:
        return self.fetch_credit_snapshot(*args, **kwargs)
