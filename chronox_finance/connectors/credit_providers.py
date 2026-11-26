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
5. `BureauAPIAdapter`: optional BYOK Credit Score API client supporting:
   - **Spinwheel** (Equifax credit score + full liabilities profile, Sandbox & Production)
   - **Array** (1B/3B Experian/Equifax/TransUnion JSON reports, Sandbox & Production)
   - **CRS Credit API** (Experian/Equifax/TransUnion JWT login or Bearer token, Sandbox & Production)
   - **Custom Credit Score API** (ChronoX-compatible HTTPS or local loopback JSON bridge)
   When unconfigured, cleanly raises `NotConfiguredForLocalDesktop` with actionable local guidance.
"""

from __future__ import annotations

import calendar
import io
import ipaddress
import json
import logging
import re
import zlib
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Tuple, Union
from urllib.parse import quote, urlencode, urlparse

import httpx

from chronox_finance.connectors.base import (
    ConnectorAuthError,
    ConnectorError,
    HostNotAllowedError,
    NormalizedLiability,
    NotConfiguredForLocalDesktop,
    apr_pct_to_bps,
    build_http_client,
    decimal_to_cents,
    request_with_retry,
    validate_strict_external_url,
)
from chronox_finance.security import sanitize_untrusted_text, validate_external_api_url

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
    if canonical in DEROGATORY_RETENTION_MONTHS:
        return canonical
    if "chapter_7" in val or "ch_7" in val or "ch7" in val:
        return "bankruptcy_ch7"
    if "chapter_13" in val or "ch_13" in val or "ch13" in val:
        return "bankruptcy_ch13"
    if "charge_off" in val or "chargeoff" in val:
        return "charge_off"
    if "collection" in val:
        return "collection"
    if "foreclosure" in val:
        return "foreclosure"
    if "repossess" in val:
        return "repossession"
    return "late_payment"


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
    apr_provided: Optional[bool] = None
    closing_day_provided: Optional[bool] = None
    due_day_provided: Optional[bool] = None

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
            "apr_provided": self.apr_provided,
            "closing_day_provided": self.closing_day_provided,
            "due_day_provided": self.due_day_provided,
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
                    apr_provided=int(item.apr_bps) > 0,
                    closing_day_provided=bool(item.last_statement_issue_date),
                    due_day_provided=bool(item.next_payment_due_date),
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
            has_apr = (
                item.get("apr_bps") is not None
                or item.get("apr") is not None
                or item.get("apr_pct") is not None
            )
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
                    apr_provided=has_apr,
                    closing_day_provided=item.get("statement_closing_day") is not None,
                    due_day_provided=item.get("payment_due_day") is not None,
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

        bundle = self.import_portal_text(
            text,
            bureau_hint=bureau_hint,
            source_tag="annual_credit_report_pdf",
            needs_review=True,
        )
        bundle.warnings = warnings + [w for w in bundle.warnings if w not in warnings]
        bundle.raw_source_format = "annual_credit_report_pdf"
        return bundle

    def import_portal_text(
        self,
        raw_text: str,
        *,
        bureau_hint: str = "auto",
        source_tag: str = "credit_portal_capture",
        needs_review: bool = True,
    ) -> CreditImportBundle:
        """
        Parse plain-text or PDF-extracted text from Experian.com, Credit Karma
        (TransUnion + Equifax VantageScore 3.0), or AnnualCreditReport.com into a
        normalized `CreditImportBundle`.
        """
        # Strip HTML tags if raw HTML was passed, preserving line breaks
        if "<html" in raw_text.lower() or "<div" in raw_text.lower():
            cleaned_html = re.sub(r"<(script|style)[^>]*>[\s\S]*?</\1>", " ", raw_text, flags=re.IGNORECASE)
            text = re.sub(r"<[^>]+>", "\n", cleaned_html)
        else:
            text = raw_text

        warnings: List[str] = []
        detected_bureau = self._detect_bureau(text, bureau_hint)
        pull_match = _PDF_PULL_DATE_RE.search(text)
        pulled_date = (
            _parse_flexible_date(pull_match.group(1))
            if pull_match
            else None
        ) or date.today().isoformat()

        reports = self._extract_portal_scores(text, detected_bureau, pulled_date, source_tag, needs_review)
        if not reports:
            reports.append(
                CreditReportRecord(
                    bureau=detected_bureau,
                    model="other",
                    score=None,
                    pulled_date=pulled_date,
                    source=source_tag,
                    needs_review=needs_review,
                )
            )
            warnings.append(
                "AnnualCreditReport.com statutory PDFs usually omit numeric FICO scores; "
                "add your actual bureau score manually if desired."
            )

        tradelines, inquiries, derogatories = self._parse_pdf_sections(text, detected_bureau)
        p_tl, p_inq, p_der = self._parse_modern_portal_blocks(text, detected_bureau, source_tag)

        # Merge modern Experian / Credit Karma blocks, disambiguating multi-loan servicers
        seen_tl_pairs = {(t.name.strip().lower(), t.balance_cents) for t in tradelines}
        name_counts: Dict[str, int] = {}
        for t in tradelines:
            k = t.name.strip().lower()
            name_counts[k] = name_counts.get(k, 0) + 1
        for t in p_tl:
            base_low = t.name.strip().lower()
            if (base_low, t.balance_cents) in seen_tl_pairs and name_counts.get(base_low, 0) == 1:
                continue
            cnt = name_counts.get(base_low, 0) + 1
            name_counts[base_low] = cnt
            if cnt > 1:
                t.name = f"{t.name} #{cnt}"[:80]
            tradelines.append(t)
            seen_tl_pairs.add((base_low, t.balance_cents))

        if p_inq and not inquiries:
            inquiries.extend(p_inq)
        else:
            seen_inq = {(i.lender.strip().lower(), i.bureau, i.inquiry_date) for i in inquiries}
            for i in p_inq:
                key = (i.lender.strip().lower(), i.bureau, i.inquiry_date)
                if key not in seen_inq:
                    inquiries.append(i)
                    seen_inq.add(key)

        seen_der = {(d.kind, (d.creditor or d.lender).strip().lower(), d.opened_date) for d in derogatories}
        for d in p_der:
            key = (d.kind, (d.creditor or d.lender).strip().lower(), d.opened_date)
            if key not in seen_der:
                derogatories.append(d)
                seen_der.add(key)

        return CreditImportBundle(
            provider=self.provider_name,
            needs_review=needs_review,
            reports=reports,
            tradelines=tradelines,
            inquiries=inquiries,
            derogatories=derogatories,
            warnings=warnings,
            raw_source_format=source_tag,
        )

    def _extract_portal_scores(
        self,
        text: str,
        default_bureau: str,
        pulled_date: str,
        source_tag: str,
        needs_review: bool,
    ) -> List[CreditReportRecord]:
        reports: List[CreditReportRecord] = []
        seen: set[Tuple[str, str, int]] = set()

        def add_score(bureau: str, model: str, val: int) -> None:
            if not (300 <= val <= 850):
                return
            b_norm = _normalize_bureau(bureau) or default_bureau
            m_norm = _normalize_model(model)
            if (b_norm, m_norm, val) in seen:
                return
            seen.add((b_norm, m_norm, val))
            reports.append(
                CreditReportRecord(
                    bureau=b_norm,
                    model=m_norm,
                    score=val,
                    pulled_date=pulled_date,
                    source=source_tag,
                    needs_review=needs_review,
                )
            )

        # 1. Credit Karma dual-bureau blocks (neutralize "3.0" / "+5 pts" / dates first so digits don't block)
        scrubbed_head = re.sub(
            r"VantageScore\s*®?\s*3(?:\.0)?", "VantageScore", text[:25000], flags=re.IGNORECASE
        )
        scrubbed_head = re.sub(
            r"FICO\s*®?\s*(?:Score\s*)?(?:8|9|10T?)", "FICOScore", scrubbed_head, flags=re.IGNORECASE
        )
        scrubbed_head = re.sub(r"[+-]\s*\d+\s*(?:pts?|points?)", " ", scrubbed_head, flags=re.IGNORECASE)

        ck_fw_re = re.compile(
            r"\b(TransUnion|Trans\s+Union|Equifax|Experian)\b[\s\S]{0,140}?"
            r"(?<![\$0-9.,])\b([3-8]\d{2})\b(?![0-9%])",
            re.IGNORECASE,
        )
        ck_rv_re = re.compile(
            r"(?<![\$0-9.,])\b([3-8]\d{2})\b(?![0-9%])[\s\S]{0,120}?"
            r"\b(TransUnion|Trans\s+Union|Equifax)\b",
            re.IGNORECASE,
        )
        is_ck = "credit karma" in text.lower() or "vantagescore" in text.lower() or (
            "transunion" in text[:8000].lower() and "equifax" in text[:8000].lower()
        )
        if is_ck:
            for m in ck_fw_re.finditer(scrubbed_head):
                val = int(m.group(2))
                if val in (300, 579, 669, 739, 799, 850):
                    continue
                add_score(m.group(1), "vantagescore3", val)
            if len(reports) < 2:
                for m in ck_rv_re.finditer(scrubbed_head):
                    val = int(m.group(1))
                    if val in (300, 579, 669, 739, 799, 850):
                        continue
                    add_score(m.group(2), "vantagescore3", val)

        # 2. Inline model + score (classic AnnualCreditReport / statements)
        if not reports:
            for m in _PDF_SCORE_RE.finditer(text[:5000]):
                val = int(m.group(2))
                add_score(default_bureau, m.group(1), val)

        # 3. Multi-line Experian web print ("FICO SCORE 8" ... "As of today" ... "645" ... "Fair")
        if not reports:
            exp_ml = re.search(
                r"(FICO\s*(?:SCORE\s*)?[89]|VantageScore\s*3(?:\.0)?)[\s\S]{0,120}?"
                r"\n\s*([3-8]\d{2})\s*\n\s*(?:Exceptional|Very\s+Good|Good|Fair|Poor)\b",
                text[:4000],
                re.IGNORECASE,
            )
            if exp_ml:
                add_score(default_bureau, exp_ml.group(1), int(exp_ml.group(2)))
            elif "experian credit report" in text[:1500].lower():
                exp_num = re.search(
                    r"\n\s*([3-8]\d{2})\s*\n\s*(?:Exceptional|Very\s+Good|Good|Fair|Poor)\b",
                    text[:2500],
                    re.IGNORECASE,
                )
                if exp_num:
                    add_score("experian", "fico8", int(exp_num.group(1)))

        return reports

    def _parse_modern_portal_blocks(
        self,
        text: str,
        default_bureau: str,
        source_tag: str,
    ) -> Tuple[List[CreditTradelineRecord], List[CreditInquiryRecord], List[CreditDerogatoryRecord]]:
        """
        Parse modern Experian web-print PDFs and Credit Karma account/inquiry/public-record
        cards where labels and values span adjacent lines without `Account Name:` colons.
        """
        tradelines: List[CreditTradelineRecord] = []
        inquiries: List[CreditInquiryRecord] = []
        derogatories: List[CreditDerogatoryRecord] = []

        raw_lines = [
            re.sub(r"[\ue000-\uf8ff®™]+", " ", raw).strip()
            for raw in text.splitlines()
        ]
        lines = [re.sub(r"\s+", " ", ln).strip() for ln in raw_lines if re.sub(r"\s+", " ", ln).strip()]

        noise_prefixes = (
            "isaiah, you've been pre-approved",
            "you've been pre-approved",
            "view offers",
            "see your offers",
            "learn more",
            "link card name",
            "what's this",
            "build your credit",
            "adding another account",
            "help show lenders",
            "want to lower your car payment",
            "autopay helps you compare",
            "a rate that's right",
            "on autopay.com",
            "get started",
            "late payments stay on your report",
            "collections can stay on your credit report",
            "keep in mind, not all inquiries",
            "your experian credit file",
            "locking your credit file",
            "more on creditlock",
            "chapter 7 bankruptcies remain",
            "you don't have any tradelines",
            "connect account",
            "these lenders have accessed",
            "these are financial accounts",
        )

        section = ""
        current_kind = "credit_card"
        section_limit_cents = 0
        pending_balance_cents: Optional[int] = None
        pending_limit_cents: Optional[int] = None
        pending_updated_date: Optional[str] = None
        pending_acct_name: str = ""
        pending_coll_agency: str = ""
        pending_orig_creditor: str = ""
        pending_inq_date: Optional[str] = None
        pending_inq_lender: str = ""
        pending_pub_kind: str = ""
        pending_pub_filed: Optional[str] = None
        last_added_tl_from_ck: bool = False

        def is_noise(low: str) -> bool:
            if any(low.startswith(p) for p in noise_prefixes):
                return True
            if re.match(r"^(?:\d+\s+closed\s+accounts?|total\s+balance\b|\d+%\s+credit\s+usage)", low):
                return True
            if low.startswith("credit used") and "credit limit" not in low:
                return True
            if low in ("|", "6%", "30%", "2", "faqs", "services", "tools", "support"):
                return True
            return False

        for line in lines:
            low = line.lower()
            if is_noise(low):
                continue

            # Major section transitions
            if low in ("credit cards or credit lines", "credit cards"):
                section = "accounts"
                current_kind = "credit_card"
                section_limit_cents = 0
                pending_balance_cents = None
                continue
            if low in ("auto loans or leases", "auto loans"):
                section = "accounts"
                current_kind = "auto_loan"
                section_limit_cents = 0
                pending_balance_cents = None
                continue
            if low == "student loans":
                section = "accounts"
                current_kind = "student_loan"
                section_limit_cents = 0
                pending_balance_cents = None
                continue
            if low in ("personal loans", "other loans"):
                section = "accounts"
                current_kind = "personal_loan"
                section_limit_cents = 0
                pending_balance_cents = None
                pending_acct_name = ""
                continue
            if low in ("mortgages", "home loans", "real estate loans"):
                section = "accounts"
                current_kind = "mortgage"
                section_limit_cents = 0
                pending_balance_cents = None
                pending_acct_name = ""
                continue
            if low in ("other accounts", "rent and utilities"):
                section = "other_accounts"
                current_kind = "personal_loan"
                pending_balance_cents = None
                pending_acct_name = ""
                continue
            if low in ("self-reported accounts", "personal information"):
                section = "ignore"
                pending_balance_cents = None
                pending_acct_name = ""
                continue
            if re.match(r"^(?:\d+\s+)?collections?$", low):
                section = "collections"
                pending_balance_cents = None
                pending_coll_agency = ""
                pending_orig_creditor = ""
                continue
            if re.match(r"^(?:\d+\s+)?(?:hard\s+)?inquiries$", low):
                section = "inquiries"
                pending_inq_date = None
                pending_inq_lender = ""
                continue
            if re.match(r"^(?:\d+\s+)?public\s+records?$", low):
                section = "public_records"
                pending_pub_kind = ""
                pending_pub_filed = None
                continue
            if low in ("faqs", "suggested for your credit", "suggested cards", "advertiser disclosure", "company info"):
                section = "done"
                break
            if line == "+":
                continue

            if section in ("accounts", "other_accounts"):
                if low == "closed" and last_added_tl_from_ck and tradelines:
                    tradelines.pop()
                    last_added_tl_from_ck = False
                    continue
                if low in ("in good standing", "needs attention") or low.startswith("show closed"):
                    last_added_tl_from_ck = False
                    continue

                lim_m = re.search(r"credit\s+limit\s*[:\-]?\s*\$\s*([0-9,]+(?:\.[0-9]{2})?)", line, re.IGNORECASE)
                if lim_m:
                    parsed_lim = decimal_to_cents(lim_m.group(1))
                    section_limit_cents = parsed_lim
                    pending_limit_cents = parsed_lim
                    if "credit used" in low or low.startswith("credit limit"):
                        continue

                bal_m = re.match(
                    r"^(?:(?:current|reported|account)\s+)?(?:balance\s*[:\-]?\s*)?\$\s*([0-9,]+(?:\.[0-9]{2})?)$",
                    line,
                    re.IGNORECASE,
                )
                if bal_m:
                    val_cents = decimal_to_cents(bal_m.group(1))
                    if pending_acct_name:
                        pot_neg = "potentially negative" in pending_acct_name.lower()
                        clean_name = re.sub(
                            r"\s*POTENTIALLY\s+NEGATIVE\s*$", "", pending_acct_name, flags=re.IGNORECASE
                        ).strip()
                        lim_val = pending_limit_cents if pending_limit_cents is not None else (
                            section_limit_cents if current_kind == "credit_card" else 0
                        )
                        tradelines.append(
                            CreditTradelineRecord(
                                name=sanitize_untrusted_text(clean_name, max_len=80),
                                issuer=sanitize_untrusted_text(clean_name, max_len=60),
                                kind=current_kind,
                                balance_cents=val_cents,
                                credit_limit_cents=lim_val,
                                statement_closing_day=_day_from_iso(pending_updated_date, 15),
                                source=source_tag,
                                apr_provided=False,
                                closing_day_provided=bool(pending_updated_date),
                                due_day_provided=False,
                            )
                        )
                        if pot_neg:
                            derogatories.append(
                                CreditDerogatoryRecord(
                                    kind="late_30",
                                    opened_date=pending_updated_date or date.today().isoformat(),
                                    creditor=clean_name,
                                    bureau=default_bureau,
                                    amount_cents=val_cents,
                                    active=True,
                                )
                            )
                        pending_acct_name = ""
                        pending_limit_cents = None
                        pending_updated_date = None
                        section_limit_cents = 0
                        last_added_tl_from_ck = True
                    elif line.lower().startswith("balance") or current_kind != "":
                        pending_balance_cents = val_cents
                        last_added_tl_from_ck = False
                    continue

                upd_m = re.match(r"^(?:balance\s+updated|last\s+reported|reported)\s*[:\-]?\s*(.+)$", line, re.IGNORECASE)
                if upd_m:
                    pending_updated_date = _parse_flexible_date(upd_m.group(1))
                    continue

                # Creditor name line (supports both ALL-CAPS Experian and Title-Case Credit Karma names)
                is_creditor_candidate = (
                    re.match(r"^[A-Za-z0-9][A-Za-z0-9&'.,/\- ]{2,65}$", line)
                    and not re.search(r"\b(balance|limit|usage|utilization|payment|opened|updated|reported|closed|open|on\s+time|remarks|details|overview|factors|score|offers|recommended|apply|credit\s+karma|transunion|equifax|experian)\b", low)
                    and not re.match(r"^\d", line)
                )
                if is_creditor_candidate:
                    if pending_balance_cents is not None:
                        pot_neg = "potentially negative" in low
                        clean_name = re.sub(r"\s*POTENTIALLY\s+NEGATIVE\s*$", "", line, flags=re.IGNORECASE).strip()
                        lim_val = pending_limit_cents if pending_limit_cents is not None else (
                            section_limit_cents if current_kind == "credit_card" else 0
                        )
                        tradelines.append(
                            CreditTradelineRecord(
                                name=sanitize_untrusted_text(clean_name, max_len=80),
                                issuer=sanitize_untrusted_text(clean_name, max_len=60),
                                kind=current_kind,
                                balance_cents=pending_balance_cents,
                                credit_limit_cents=lim_val,
                                statement_closing_day=_day_from_iso(pending_updated_date, 15),
                                source=source_tag,
                                apr_provided=False,
                                closing_day_provided=bool(pending_updated_date),
                                due_day_provided=False,
                            )
                        )
                        if pot_neg and pending_updated_date:
                            derogatories.append(
                                CreditDerogatoryRecord(
                                    kind="late_30",
                                    opened_date=pending_updated_date,
                                    creditor=clean_name,
                                    bureau=default_bureau,
                                    amount_cents=pending_balance_cents,
                                    active=True,
                                )
                            )
                        pending_balance_cents = None
                        pending_limit_cents = None
                        pending_updated_date = None
                        section_limit_cents = 0
                    else:
                        pending_acct_name = line.strip()

            elif section == "collections":
                if low.startswith(("if you've fallen behind", "needs attention")):
                    continue
                orig_m = re.match(r"^original\s+creditor\s*[:\-]?\s*(.+)$", line, re.IGNORECASE)
                if orig_m:
                    pending_orig_creditor = orig_m.group(1).strip()
                    continue
                bal_m = re.match(r"^(?:balance\s*[:\-]?\s*)?\$\s*([0-9,]+(?:\.[0-9]{2})?)$", line, re.IGNORECASE)
                if bal_m:
                    val_c = decimal_to_cents(bal_m.group(1))
                    if pending_coll_agency:
                        label = f"{pending_coll_agency} (Orig: {pending_orig_creditor})" if pending_orig_creditor else pending_coll_agency
                        derogatories.append(
                            CreditDerogatoryRecord(
                                kind="collection",
                                opened_date=pending_updated_date or date.today().isoformat(),
                                creditor=label[:80],
                                bureau=default_bureau,
                                amount_cents=val_c,
                                active=True,
                            )
                        )
                        pending_coll_agency = ""
                        pending_orig_creditor = ""
                        pending_updated_date = None
                    else:
                        pending_balance_cents = val_c
                    continue
                upd_m = re.match(r"^(?:balance\s+updated|reported|opened(?:\s+date)?|date\s+opened)\s*[:\-]?\s*(.+)$", line, re.IGNORECASE)
                if upd_m:
                    pending_updated_date = _parse_flexible_date(upd_m.group(1))
                    continue
                if re.match(r"^[A-Z0-9][A-Z0-9&'.,/\- ]{2,70}$", line):
                    agency = line.strip()
                    if pending_balance_cents is not None:
                        label = f"{agency} (Orig: {pending_orig_creditor})" if pending_orig_creditor else agency
                        ev_dt = pending_updated_date or date.today().isoformat()
                        derogatories.append(
                            CreditDerogatoryRecord(
                                kind="collection",
                                opened_date=ev_dt,
                                creditor=label[:80],
                                bureau=default_bureau,
                                amount_cents=pending_balance_cents,
                                active=True,
                            )
                        )
                        pending_balance_cents = None
                        pending_orig_creditor = ""
                        pending_updated_date = None
                    else:
                        pending_coll_agency = agency

            elif section == "inquiries":
                if low.startswith(("when you apply", "removal date", "credit unions and finance", "banks", "auto finance")):
                    continue
                inq_dt_m = re.match(r"^(?:inquiry(?:\s+date)?|date)\s*[:\-]?\s*(.+)$", line, re.IGNORECASE)
                if inq_dt_m:
                    parsed_dt = _parse_flexible_date(inq_dt_m.group(1))
                    if parsed_dt and pending_inq_lender:
                        inquiries.append(
                            CreditInquiryRecord(
                                lender=pending_inq_lender,
                                bureau=default_bureau,
                                inquiry_date=parsed_dt,
                                inquiry_type="hard",
                            )
                        )
                        pending_inq_lender = ""
                    else:
                        pending_inq_date = parsed_dt
                    continue
                if re.match(r"^[A-Z0-9][A-Z0-9&'.,/\- ]{2,70}$", line):
                    if pending_inq_date:
                        inquiries.append(
                            CreditInquiryRecord(
                                lender=line.strip(),
                                bureau=default_bureau,
                                inquiry_date=pending_inq_date,
                                inquiry_type="hard",
                            )
                        )
                        pending_inq_date = None
                    else:
                        pending_inq_lender = line.strip()

            elif section == "public_records":
                if low.startswith(("things like bankruptcies", "latest status date")) or line.startswith("$"):
                    continue
                rec_m = re.match(r"^(?:record\s+type\s*[:\-]?\s*)?(chapter\s*(?:7|13)\s*bankruptcy.*)$", line, re.IGNORECASE)
                if not rec_m:
                    rec_m = re.match(r"^record\s+type\s*[:\-]?\s*(.+)$", line, re.IGNORECASE)
                if rec_m:
                    if pending_pub_filed and pending_pub_kind:
                        # trailing "Chapter 7 bankruptcy discharged" description line on CK
                        continue
                    pending_pub_kind = rec_m.group(1).strip()
                    continue
                fil_m = re.match(r"^(?:original\s+filing\s+date|filing\s+date|date\s+filed)\s*[:\-]?\s*(.+)$", line, re.IGNORECASE)
                direct_dt = _parse_flexible_date(fil_m.group(1) if fil_m else line)
                if direct_dt:
                    pending_pub_filed = direct_dt
                    # Credit Karma often omits court name on summary; emit immediately if kind already known
                    if pending_pub_kind and "chapter" in pending_pub_kind.lower() and ":" in pending_pub_kind:
                        derogatories.append(
                            CreditDerogatoryRecord(
                                kind=pending_pub_kind,
                                opened_date=pending_pub_filed,
                                creditor="US Bankruptcy Court",
                                bureau=default_bureau,
                                active=True,
                            )
                        )
                        pending_pub_kind = ""
                        pending_pub_filed = None
                    continue
                if pending_pub_filed and re.match(r"^[A-Z0-9][A-Z0-9&'.,/\- ]{2,70}$", line):
                    derogatories.append(
                        CreditDerogatoryRecord(
                            kind=pending_pub_kind or "bankruptcy_ch7",
                            opened_date=pending_pub_filed,
                            creditor=line.strip(),
                            bureau=default_bureau,
                            active=True,
                        )
                    )
                    pending_pub_kind = ""
                    pending_pub_filed = None

        return tradelines, inquiries, derogatories

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
                    apr_provided=False,
                    closing_day_provided=False,
                    due_day_provided=False,
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
# 3. Optional BYOK Credit Score API Adapter (Spinwheel / Array / CRS / Custom)
# ---------------------------------------------------------------------------

CREDIT_API_VENDOR_SPECS: Dict[str, Dict[str, Any]] = {
    "spinwheel": {
        "vendor": "spinwheel",
        "display_name": "Spinwheel",
        "auth_type": "bearer_api_key",
        "bureaus": ["equifax"],
        "models": ["vantage3", "fico8"],
        "sandbox_base_url": "https://sandbox-api.spinwheel.io",
        "production_base_url": "https://api.spinwheel.io",
        "allowed_hosts": ("sandbox-api.spinwheel.io", "api.spinwheel.io"),
        "notes": "Equifax credit score & full liabilities profile via Bearer API Key + Spinwheel User ID.",
    },
    "array": {
        "vendor": "array",
        "display_name": "Array",
        "auth_type": "server_token",
        "bureaus": ["experian", "equifax", "transunion"],
        "models": ["vantage3", "fico8"],
        "sandbox_base_url": "https://sandbox.array.io/api",
        "production_base_url": "https://array.io/api",
        "allowed_hosts": ("sandbox.array.io", "array.io"),
        "notes": "1B/3B Experian, Equifax & TransUnion JSON reports via Server Token + Report Key & Display Token.",
    },
    "crs": {
        "vendor": "crs",
        "display_name": "CRS Credit API",
        "auth_type": "jwt_login_or_bearer",
        "bureaus": ["experian", "equifax", "transunion"],
        "models": ["fico8", "vantage3", "fico9"],
        "sandbox_base_url": "https://api-sandbox.stitchcredit.com/api",
        "production_base_url": "https://api.stitchcredit.com/api",
        "allowed_hosts": ("api-sandbox.stitchcredit.com", "api.stitchcredit.com"),
        "notes": "Direct Experian, Equifax & TransUnion pulls via CRS Username/Password JWT login or Bearer Token.",
    },
    "custom": {
        "vendor": "custom",
        "display_name": "Custom Credit Score API",
        "auth_type": "bearer_api_key",
        "bureaus": ["experian", "equifax", "transunion", "other"],
        "models": ["fico8", "vantage3", "fico9", "other"],
        "sandbox_base_url": "",
        "production_base_url": "",
        "allowed_hosts": None,
        "notes": "Any ChronoX-compatible HTTPS or local loopback credit bridge returning structured score/report JSON.",
    },
}

_CRS_DEFAULT_OPTIONS: Dict[str, str] = {
    "equifax": "credit-report-equifax-vantage3",
    "experian": "credit-report-experian-fico8",
    "transunion": "credit-report-transunion-vantage3",
}


def _validate_custom_credit_url(url: str, *, provider: str) -> str:
    """
    Validate a Custom Credit Score API URL.

    Allows:
    - Public `https://` endpoints
    - Explicit loopback `http(s)://127.0.0.1`, `localhost`, or `[::1]` for self-hosted/local bridges

    Blocks:
    - Non-HTTP schemes, non-local plaintext `http://`, embedded URL credentials
    - Cloud metadata / link-local IPs (`169.254.x.x`, `fe80::/10`), multicast, reserved
    - Private RFC1918 / ULA networks (`10.x`, `172.16.x`, `192.168.x`, `fc00::/7`)
    """
    raw = (url or "").strip()
    if not raw:
        raise HostNotAllowedError("Custom Credit API URL must not be empty", provider=provider)
    try:
        validated = validate_external_api_url(raw)
    except ValueError as exc:
        raise HostNotAllowedError(str(exc), provider=provider) from exc

    parsed = urlparse(validated)
    host = (parsed.hostname or "").lower()
    if parsed.username is not None or parsed.password is not None:
        raise HostNotAllowedError("Embedded URL credentials are not permitted", provider=provider)
    if host.endswith((".local", ".internal")):
        raise HostNotAllowedError(f"Internal host '{host}' is blocked", provider=provider)

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None

    if ip is not None and not ip.is_loopback:
        if (
            ip.is_private
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise HostNotAllowedError(f"Blocked non-public IP address '{host}'", provider=provider)

    return validated


def _money_field_to_cents(obj: Dict[str, Any], cents_keys: Sequence[str], dollar_keys: Sequence[str]) -> int:
    for k in cents_keys:
        if obj.get(k) is not None and str(obj.get(k)).strip() != "":
            return max(0, int(obj[k]))
    for k in dollar_keys:
        val = obj.get(k)
        if val is not None and str(val).strip() != "":
            try:
                return max(0, decimal_to_cents(val))
            except ValueError:
                continue
    return 0


def _opt_money_field_to_cents(
    obj: Dict[str, Any], cents_keys: Sequence[str], dollar_keys: Sequence[str]
) -> Optional[int]:
    for k in cents_keys:
        if obj.get(k) is not None and str(obj.get(k)).strip() != "":
            return max(0, int(obj[k]))
    for k in dollar_keys:
        val = obj.get(k)
        if val is not None and str(val).strip() != "":
            try:
                return max(0, decimal_to_cents(val))
            except ValueError:
                continue
    return None


def _parse_score_int(raw: Any) -> Optional[int]:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        val = int(round(float(str(raw).strip().lstrip("+"))))
    except (ValueError, TypeError):
        return None
    return val if 300 <= val <= 850 else None


def _safe_error_snippet(response: httpx.Response, *, max_len: int = 180) -> str:
    try:
        body = response.json()
        if isinstance(body, dict):
            msg = (
                body.get("message")
                or body.get("error_description")
                or body.get("error")
                or body.get("detail")
                or body.get("statusMessage")
            )
            if isinstance(msg, dict):
                msg = msg.get("message") or msg.get("desc") or json.dumps(msg)
            if msg:
                return sanitize_untrusted_text(str(msg), max_len=max_len)
    except (ValueError, TypeError):
        logger.debug("Non-JSON credit API error body (HTTP %s)", response.status_code)
    return sanitize_untrusted_text(response.text or f"HTTP {response.status_code}", max_len=max_len)


class BureauAPIAdapter:
    """
    Optional BYOK Credit Score API client for Spinwheel, Array, CRS Credit API, and
    Custom Credit Score REST endpoints.

    - When instantiated WITHOUT credentials (default local desktop state), `is_configured()`
      is `False` and `fetch_credit_snapshot()` raises `NotConfiguredForLocalDesktop` with
      clear guidance pointing to Plaid Liabilities & Official Credit Report imports.
    - When instantiated WITH user-supplied BYOK credentials (sealed in `secrets.enc`),
      performs strictly allowlisted, redirect-blocking HTTPS requests and normalizes
      bureau scores, hard inquiries, derogatories, and tradelines into `CreditImportBundle`.
    """

    SUPPORTED_VENDORS = ("spinwheel", "array", "crs", "custom")

    def __init__(
        self,
        vendor: str = "crs",
        *,
        env: str = "production",
        api_base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        user_id: Optional[str] = None,
        report_key: Optional[str] = None,
        display_token: Optional[str] = None,
        bureau: str = "equifax",
        option: Optional[str] = None,
        transport: Optional[httpx.BaseTransport] = None,
        timeout: float = 20.0,
        max_retries: int = 2,
        backoff_base: float = 0.25,
        sleep_fn: Optional[Callable[[float], None]] = None,
    ) -> None:
        normalized = (vendor or "crs").strip().lower()
        if normalized not in self.SUPPORTED_VENDORS:
            raise ValueError(f"Unsupported bureau adapter '{vendor}'; choose from {self.SUPPORTED_VENDORS}")
        norm_env = (env or "production").strip().lower()
        if norm_env not in ("sandbox", "production"):
            raise ValueError("Environment must be 'sandbox' or 'production'")

        self.vendor = normalized
        self.env = norm_env
        self.provider_name = f"bureau_api_{normalized}"
        self._spec = CREDIT_API_VENDOR_SPECS[normalized]

        raw_base = (api_base_url or "").strip()
        if normalized == "custom":
            self._api_base_url = (
                _validate_custom_credit_url(raw_base, provider=self.provider_name) if raw_base else ""
            )
        else:
            default_url = self._spec[f"{norm_env}_base_url"]
            target_url = raw_base or default_url
            self._api_base_url = validate_strict_external_url(
                target_url,
                provider=self.provider_name,
                allowed_hosts=self._spec["allowed_hosts"],
                require_https=True,
            ).rstrip("/")

        self._api_key = (api_key or "").strip()
        self._api_secret = (api_secret or "").strip()
        self._client_id = (client_id or "").strip()
        self._client_secret = (client_secret or "").strip()
        self._username = (username or "").strip()
        self._password = (password or "").strip()
        self._user_id = (user_id or "").strip()
        self._report_key = (report_key or "").strip()
        self._display_token = (display_token or "").strip()
        self._bureau = _normalize_bureau(bureau or "equifax")
        if self._bureau == "other":
            self._bureau = "equifax"
        self._option = (option or "").strip()
        self._transport = transport
        self._timeout = float(timeout)
        self._max_retries = int(max_retries)
        self._backoff_base = float(backoff_base)
        self._sleep_fn = sleep_fn

    def is_configured(self) -> bool:
        if self.vendor == "spinwheel":
            return bool(self._api_key and self._user_id)
        if self.vendor == "array":
            return bool(self._api_key or self._client_secret)
        if self.vendor == "crs":
            return bool(
                self._api_key
                or (self._username and self._password)
                or (self._client_id and self._client_secret)
            )
        if self.vendor == "custom":
            return bool(self._api_base_url)
        return False

    def missing_config_message(self) -> str:
        if self.vendor == "spinwheel":
            return "Spinwheel requires both an API key and a Spinwheel User ID."
        if self.vendor == "array":
            return "Array requires an Array Server Token (api_key)."
        if self.vendor == "crs":
            return "CRS Credit API requires either a Bearer token (api_key) or a username and password."
        return "Custom Credit Score API requires a valid HTTPS or localhost API URL."

    def capabilities(self) -> Dict[str, Any]:
        configured = self.is_configured()
        return {
            "vendor": self.vendor,
            "display_name": self._spec["display_name"],
            "env": self.env,
            "base_url": self._api_base_url,
            "configured": configured,
            "byok_supported": True,
            "local_desktop_supported": configured,
            "requires_fcra_saas_backend": self.vendor in ("crs", "array", "spinwheel"),
            "bureaus": list(self._spec["bureaus"]),
            "models": list(self._spec["models"]),
            "recommended_local_alternatives": [
                "Plaid Liabilities (/liabilities/get) for live credit cards, APRs, and loans",
                "Official AnnualCreditReport.com PDF or Structured Manual Form for bureau scores, inquiries, and derogatories",
            ],
        }

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, Any]:
        req_headers = {"Accept": "application/json", "User-Agent": "ChronoX-Finance/2.0"}
        if headers:
            req_headers.update(headers)
        kwargs: Dict[str, Any] = {}
        if json_body is not None:
            kwargs["json"] = json_body
            req_headers.setdefault("Content-Type", "application/json")

        retry_kwargs: Dict[str, Any] = {
            "provider": self.provider_name,
            "max_retries": self._max_retries,
            "backoff_base": self._backoff_base,
        }
        if self._sleep_fn is not None:
            retry_kwargs["sleep_fn"] = self._sleep_fn

        with build_http_client(timeout=self._timeout, transport=self._transport, headers=req_headers) as client:
            resp = request_with_retry(client, method, url, **retry_kwargs, **kwargs)

        if resp.status_code in (401, 403):
            raise ConnectorAuthError(
                f"{self._spec['display_name']} authentication failed (HTTP {resp.status_code}): "
                f"{_safe_error_snippet(resp)}",
                provider=self.provider_name,
                status_code=resp.status_code,
            )
        if resp.status_code >= 400:
            raise ConnectorError(
                f"{self._spec['display_name']} request failed (HTTP {resp.status_code}): "
                f"{_safe_error_snippet(resp)}",
                provider=self.provider_name,
                status_code=resp.status_code,
            )
        try:
            data = resp.json() if resp.content else {}
        except ValueError as exc:
            raise ConnectorError(
                f"{self._spec['display_name']} returned non-JSON response (HTTP {resp.status_code}).",
                provider=self.provider_name,
                status_code=resp.status_code,
            ) from exc
        return resp.status_code, data

    def test_connection(
        self,
        *,
        report_key: Optional[str] = None,
        display_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Verify live API credentials without mutating local ledger state."""
        if not self.is_configured():
            raise NotConfiguredForLocalDesktop(
                f"{self._spec['display_name']} is not configured. {self.missing_config_message()}",
                provider=self.provider_name,
            )

        if self.vendor == "spinwheel":
            safe_uid = quote(self._user_id, safe="")
            url = f"{self._api_base_url}/v1/users/{safe_uid}"
            status, payload = self._request_json(
                "GET", url, headers={"Authorization": f"Bearer {self._api_key}"}
            )
            data = payload.get("data", payload) if isinstance(payload, dict) else {}
            has_profile = bool(isinstance(data, dict) and (data.get("creditProfile") or data.get("credit_profile")))
            return {
                "ok": True,
                "vendor": "spinwheel",
                "env": self.env,
                "status_code": status,
                "user_id": self._user_id,
                "has_credit_profile": has_profile,
                "message": f"Spinwheel {self.env} connection verified for user {self._user_id}.",
            }

        if self.vendor == "array":
            token = self._api_key or self._client_secret
            rk = (report_key or self._report_key).strip()
            dt = (display_token or self._display_token).strip()
            headers = {
                "x-array-server-token": token,
                "Authorization": f"Bearer {token}",
            }
            if rk and dt:
                qs = urlencode({"reportKey": rk, "displayToken": dt})
                url = f"{self._api_base_url}/report/v2?{qs}"
            elif self._user_id:
                qs = urlencode({"userId": self._user_id})
                url = f"{self._api_base_url}/user/v2?{qs}"
            else:
                url = f"{self._api_base_url}/report/v2"
            status, _payload = self._request_json("GET", url, headers=headers)
            return {
                "ok": True,
                "vendor": "array",
                "env": self.env,
                "status_code": status,
                "message": (
                    "Array report still generating (HTTP 202); credentials valid."
                    if status == 202
                    else f"Array {self.env} connection verified."
                ),
            }

        if self.vendor == "crs":
            bearer = self._crs_get_bearer_token()
            status, payload = self._request_json(
                "GET",
                f"{self._api_base_url}/system/health",
                headers={"Authorization": f"Bearer {bearer}"},
            )
            return {
                "ok": True,
                "vendor": "crs",
                "env": self.env,
                "status_code": status,
                "authenticated": True,
                "health": payload if isinstance(payload, dict) else {"status": "ok"},
                "message": f"CRS Credit API {self.env} authenticated & healthy.",
            }

        # custom
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        status, payload = self._request_json("GET", self._api_base_url, headers=headers)
        if not isinstance(payload, dict):
            raise ConnectorError(
                "Custom Credit Score API must return a JSON object.",
                provider=self.provider_name,
                status_code=status,
            )
        return {
            "ok": True,
            "vendor": "custom",
            "env": self.env,
            "status_code": status,
            "message": f"Custom Credit Score API reachable ({self._api_base_url}).",
        }

    def fetch_credit_snapshot(
        self,
        *,
        order_refresh: bool = False,
        report_key: Optional[str] = None,
        display_token: Optional[str] = None,
        bureau: Optional[str] = None,
        option: Optional[str] = None,
        consumer_profile: Optional[Dict[str, Any]] = None,
    ) -> CreditImportBundle:
        """
        Pull live bureau credit scores, inquiries, derogatories, and tradelines from the
        configured Credit Score API vendor.

        When unconfigured, raises `NotConfiguredForLocalDesktop` with guidance for local
        Plaid Liabilities & Official Credit Report imports.
        """
        if not self.is_configured():
            raise NotConfiguredForLocalDesktop(
                f"Direct bureau API '{self.vendor.upper()}' is not configured yet. Configure BYOK "
                "credentials in Credit & Debt -> Optional Credit Score APIs, or use Plaid Liabilities "
                "for live card/loan balances & APRs and the Official Credit Report Structured Form / "
                "AnnualCreditReport.com importer for bureau scores, hard inquiries, and derogatory drop-offs.",
                provider=self.provider_name,
            )

        if self.vendor == "spinwheel":
            return self._fetch_spinwheel(order_refresh=order_refresh)
        if self.vendor == "array":
            return self._fetch_array(report_key=report_key, display_token=display_token)
        if self.vendor == "crs":
            return self._fetch_crs(bureau=bureau, option=option, consumer_profile=consumer_profile)
        return self._fetch_custom()

    def pull_tri_merge(self, *args: Any, **kwargs: Any) -> CreditImportBundle:
        return self.fetch_credit_snapshot(*args, **kwargs)

    # --- Vendor 1: Spinwheel (Equifax Credit Profile + Liabilities) ---

    def _fetch_spinwheel(self, *, order_refresh: bool = False) -> CreditImportBundle:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        safe_uid = quote(self._user_id, safe="")
        if order_refresh:
            self._request_json(
                "POST",
                f"{self._api_base_url}/v1/users/{safe_uid}/debtProfile",
                headers=headers,
                json_body={"creditReport": {"source": "equifax"}},
            )
        _status, payload = self._request_json(
            "GET",
            f"{self._api_base_url}/v1/users/{safe_uid}",
            headers=headers,
        )
        return self._parse_spinwheel_payload(payload if isinstance(payload, dict) else {})

    def _parse_spinwheel_payload(self, payload: Dict[str, Any]) -> CreditImportBundle:
        data = payload.get("data", payload) if isinstance(payload.get("data"), dict) else payload
        today_iso = date.today().isoformat()
        reports: List[CreditReportRecord] = []
        tradelines: List[CreditTradelineRecord] = []
        inquiries: List[CreditInquiryRecord] = []
        derogatories: List[CreditDerogatoryRecord] = []
        warnings: List[str] = []

        profile = data.get("creditProfile") or data.get("credit_profile") or {}
        if isinstance(profile, dict):
            raw_scores: List[Dict[str, Any]] = []
            if isinstance(profile.get("creditScores"), list):
                raw_scores.extend(s for s in profile["creditScores"] if isinstance(s, dict))
            elif isinstance(profile.get("creditScore"), dict):
                raw_scores.append(profile["creditScore"])
            elif profile.get("creditScore") is not None:
                raw_scores.append(
                    {
                        "score": profile.get("creditScore"),
                        "model": profile.get("scoreModel") or profile.get("creditScoreModel") or "vantage3",
                        "bureau": profile.get("bureau") or "equifax",
                        "pulled_date": profile.get("updatedAt") or profile.get("pulledDate") or today_iso,
                    }
                )
            elif profile.get("score") is not None:
                raw_scores.append(profile)

            for s_item in raw_scores:
                score_val = _parse_score_int(
                    s_item.get("score") or s_item.get("creditScore") or s_item.get("value")
                )
                if score_val is None:
                    continue
                pulled = (
                    _parse_flexible_date(
                        s_item.get("pulled_date")
                        or s_item.get("pulledDate")
                        or s_item.get("updatedAt")
                        or s_item.get("reportedDate")
                    )
                    or today_iso
                )
                reports.append(
                    CreditReportRecord(
                        bureau=_normalize_bureau(str(s_item.get("bureau") or "equifax")),
                        pulled_date=pulled,
                        model=_normalize_model(
                            str(s_item.get("model") or s_item.get("scoreModel") or "vantage3")
                        ),
                        score=score_val,
                        source="spinwheel_api",
                        needs_review=False,
                    )
                )

            for raw_inq in profile.get("inquiries") or profile.get("hardInquiries") or []:
                if not isinstance(raw_inq, dict):
                    continue
                inq_date = _parse_flexible_date(
                    raw_inq.get("inquiryDate") or raw_inq.get("inquiry_date") or raw_inq.get("date")
                )
                if not inq_date:
                    continue
                inquiries.append(
                    CreditInquiryRecord(
                        lender=str(raw_inq.get("lender") or raw_inq.get("subscriberName") or "Unknown Lender"),
                        bureau=str(raw_inq.get("bureau") or "equifax"),
                        inquiry_date=inq_date,
                    )
                )

            for raw_der in profile.get("derogatories") or profile.get("publicRecords") or []:
                if not isinstance(raw_der, dict):
                    continue
                ev_date = _parse_flexible_date(
                    raw_der.get("openedDate") or raw_der.get("opened_date") or raw_der.get("date")
                )
                if not ev_date:
                    continue
                derogatories.append(
                    CreditDerogatoryRecord(
                        kind=str(raw_der.get("kind") or raw_der.get("type") or "late_payment"),
                        opened_date=ev_date,
                        creditor=str(raw_der.get("creditor") or raw_der.get("lender") or ""),
                        bureau=str(raw_der.get("bureau") or "equifax"),
                        amount_cents=_opt_money_field_to_cents(raw_der, ("amount_cents",), ("amount", "balance")),
                    )
                )

        # Parse Spinwheel liabilities buckets
        liab_root = data.get("liabilities") if isinstance(data.get("liabilities"), dict) else data
        bucket_kind_map = (
            ("creditCards", "credit_card"),
            ("credit_cards", "credit_card"),
            ("studentLoans", "student_loan"),
            ("student_loans", "student_loan"),
            ("autoLoans", "auto_loan"),
            ("auto_loans", "auto_loan"),
            ("personalLoans", "personal_loan"),
            ("personal_loans", "personal_loan"),
            ("homeLoans", "mortgage"),
            ("home_loans", "mortgage"),
            ("mortgages", "mortgage"),
            ("miscellaneousLiabilities", "personal_loan"),
        )
        for bucket_key, default_kind in bucket_kind_map:
            items = liab_root.get(bucket_key)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                stmt = item.get("statementSummary") if isinstance(item.get("statementSummary"), dict) else {}
                card_prof = item.get("creditCardProfile") if isinstance(item.get("creditCardProfile"), dict) else {}
                bal_det = item.get("balanceDetails") if isinstance(item.get("balanceDetails"), dict) else {}
                liab_prof = item.get("liabilityProfile") if isinstance(item.get("liabilityProfile"), dict) else {}

                name = (
                    item.get("name")
                    or item.get("accountName")
                    or card_prof.get("accountName")
                    or liab_prof.get("creditorName")
                    or item.get("creditorName")
                    or f"{default_kind.replace('_', ' ').title()}"
                )
                issuer = (
                    item.get("issuer")
                    or card_prof.get("issuerName")
                    or liab_prof.get("servicerName")
                    or item.get("servicerName")
                    or ""
                )
                bal_cents = _money_field_to_cents(
                    item,
                    ("balance_cents",),
                    ("currentBalance", "balance", "outstandingBalance"),
                ) or _money_field_to_cents(bal_det, ("balance_cents",), ("outstandingBalance", "currentBalance"))

                limit_cents = (
                    _money_field_to_cents(
                        item, ("credit_limit_cents",), ("creditLimit", "limit", "highCredit")
                    )
                    or _money_field_to_cents(stmt, ("credit_limit_cents",), ("creditLimit",))
                    or _money_field_to_cents(card_prof, ("credit_limit_cents",), ("creditLimit",))
                )
                stmt_bal = _opt_money_field_to_cents(
                    stmt, ("statement_balance_cents",), ("statementBalance", "lastStatementBalance")
                ) or _opt_money_field_to_cents(
                    item, ("statement_balance_cents",), ("statementBalance", "lastStatementBalance")
                )
                min_pay = _money_field_to_cents(
                    stmt, ("min_payment_cents",), ("minimumPaymentAmount", "minimumPayment")
                ) or _money_field_to_cents(
                    item, ("min_payment_cents",), ("minimumPaymentAmount", "minimumPayment")
                )

                raw_apr = (
                    item.get("apr")
                    if item.get("apr") is not None
                    else (
                        item.get("interestRate")
                        if item.get("interestRate") is not None
                        else (stmt.get("apr") if stmt.get("apr") is not None else liab_prof.get("interestRatePercentage"))
                    )
                )
                apr_bps = int(item["apr_bps"]) if item.get("apr_bps") is not None else apr_pct_to_bps(raw_apr)

                due_date = _parse_flexible_date(
                    stmt.get("dueDate") or item.get("dueDate") or item.get("nextPaymentDueDate")
                )
                stmt_date = _parse_flexible_date(
                    stmt.get("statementDate") or item.get("lastStatementDate")
                )
                opened_date = _parse_flexible_date(
                    item.get("openedDate") or item.get("originationDate") or liab_prof.get("originationDate")
                )
                status_raw = str(item.get("status") or liab_prof.get("status") or "open").strip().lower()
                is_overdue = bool(
                    item.get("isOverdue")
                    or stmt.get("pastDueAmount")
                    or "delinquent" in status_raw
                    or "past_due" in status_raw
                )
                ecoa = str(item.get("accountOwnershipType") or card_prof.get("ecoaCode") or "").lower()
                is_au = bool(item.get("isAuthorizedUser") or "authorized" in ecoa)

                tradelines.append(
                    CreditTradelineRecord(
                        name=sanitize_untrusted_text(str(name), max_len=80) or "Credit Account",
                        issuer=sanitize_untrusted_text(str(issuer), max_len=60),
                        kind=default_kind,
                        balance_cents=bal_cents,
                        credit_limit_cents=limit_cents,
                        apr_bps=apr_bps,
                        min_payment_cents=min_pay,
                        statement_balance_cents=stmt_bal,
                        statement_closing_day=_day_from_iso(stmt_date, 15),
                        payment_due_day=_day_from_iso(due_date, 10),
                        next_due_date=due_date,
                        opened_date=opened_date,
                        is_overdue=is_overdue,
                        is_authorized_user=is_au,
                        status="closed" if "closed" in status_raw else "open",
                        external_account_id=str(
                            item.get("creditCardId") or item.get("liabilityId") or item.get("id") or ""
                        ),
                        source="spinwheel_api",
                        apr_provided=(item.get("apr_bps") is not None or raw_apr is not None),
                        closing_day_provided=(stmt_date is not None),
                        due_day_provided=(due_date is not None),
                    )
                )

        if not reports and not tradelines:
            warnings.append("Spinwheel response contained no credit score or liability records yet.")

        return CreditImportBundle(
            provider=self.provider_name,
            needs_review=False,
            reports=reports,
            tradelines=tradelines,
            inquiries=inquiries,
            derogatories=derogatories,
            warnings=warnings,
            raw_source_format="spinwheel_json",
        )

    # --- Vendor 2: Array (1B/3B Experian / Equifax / TransUnion JSON) ---

    def _fetch_array(
        self,
        *,
        report_key: Optional[str] = None,
        display_token: Optional[str] = None,
    ) -> CreditImportBundle:
        token = self._api_key or self._client_secret
        rk = (report_key or self._report_key).strip()
        dt = (display_token or self._display_token).strip()
        if not rk or not dt:
            raise ConnectorError(
                "Array requires both 'report_key' and 'display_token' to retrieve a credit report JSON.",
                provider=self.provider_name,
            )
        qs = urlencode({"reportKey": rk, "displayToken": dt})
        url = f"{self._api_base_url}/report/v2?{qs}"
        status, payload = self._request_json(
            "GET",
            url,
            headers={
                "x-array-server-token": token,
                "Authorization": f"Bearer {token}",
            },
        )
        if status == 202:
            raise ConnectorError(
                "Array credit report is still being generated (HTTP 202). Wait a few seconds and sync again.",
                provider=self.provider_name,
                status_code=202,
            )
        return self._parse_array_payload(payload if isinstance(payload, dict) else {})

    def _parse_array_payload(self, payload: Dict[str, Any]) -> CreditImportBundle:
        today_iso = date.today().isoformat()
        reports: List[CreditReportRecord] = []
        tradelines: List[CreditTradelineRecord] = []
        inquiries: List[CreditInquiryRecord] = []
        derogatories: List[CreditDerogatoryRecord] = []
        warnings: List[str] = []

        # Support Array MISMO JSON (`CREDIT_RESPONSE`) and flattened JSON formats
        cr = payload.get("CREDIT_RESPONSE") or payload.get("creditResponse") or payload
        if not isinstance(cr, dict):
            cr = {}

        # 1. Scores
        raw_scores = (
            cr.get("CREDIT_SCORE")
            or cr.get("creditScores")
            or cr.get("scores")
            or cr.get("reports")
            or []
        )
        if isinstance(raw_scores, dict):
            raw_scores = [raw_scores]
        for s_item in raw_scores:
            if not isinstance(s_item, dict):
                continue
            score_val = _parse_score_int(
                s_item.get("@_Value")
                or s_item.get("value")
                or s_item.get("score")
                or s_item.get("creditScore")
            )
            if score_val is None:
                continue
            bureau_raw = (
                s_item.get("@CreditRepositorySourceType")
                or s_item.get("bureau")
                or s_item.get("source")
                or "other"
            )
            model_raw = (
                s_item.get("@_ModelNameType")
                or s_item.get("model")
                or s_item.get("scoreModel")
                or "vantage3"
            )
            pulled = (
                _parse_flexible_date(
                    s_item.get("@_Date")
                    or s_item.get("pulled_date")
                    or s_item.get("date")
                    or cr.get("@CreditReportFirstIssuedDate")
                )
                or today_iso
            )
            reports.append(
                CreditReportRecord(
                    bureau=_normalize_bureau(str(bureau_raw)),
                    pulled_date=pulled,
                    model=_normalize_model(str(model_raw)),
                    score=score_val,
                    source="array_api",
                    needs_review=False,
                )
            )

        # 2. Tradelines / Liabilities
        raw_liabs = (
            cr.get("CREDIT_LIABILITY")
            or cr.get("tradelines")
            or cr.get("accounts")
            or cr.get("liabilities")
            or []
        )
        if isinstance(raw_liabs, dict):
            raw_liabs = [raw_liabs]
        for item in raw_liabs:
            if not isinstance(item, dict):
                continue
            creditor_obj = item.get("_CREDITOR") if isinstance(item.get("_CREDITOR"), dict) else {}
            name = (
                creditor_obj.get("@_Name")
                or item.get("name")
                or item.get("creditorName")
                or item.get("accountName")
                or "Credit Account"
            )
            kind_raw = (
                item.get("@_AccountType")
                or item.get("@_AccountTypeDescription")
                or item.get("kind")
                or item.get("accountType")
                or "credit_card"
            )
            ecoa = str(
                item.get("@_AccountOwnershipType")
                or item.get("ownershipType")
                or item.get("ecoa")
                or ""
            ).lower()
            is_au = bool(item.get("is_authorized_user") or "authorized" in ecoa or ecoa == "a")
            bal_cents = _money_field_to_cents(
                item,
                ("balance_cents",),
                ("@_UnpaidBalanceAmount", "balance", "currentBalance"),
            )
            limit_cents = _money_field_to_cents(
                item,
                ("credit_limit_cents",),
                ("@_CreditLimitAmount", "@_HighCreditAmount", "creditLimit", "limit"),
            )
            min_pay = _money_field_to_cents(
                item,
                ("min_payment_cents",),
                ("@_MonthlyPaymentAmount", "monthlyPayment", "minimumPayment"),
            )
            opened = _parse_flexible_date(
                item.get("@_AccountOpenedDate") or item.get("opened_date") or item.get("openedDate")
            )
            status_raw = str(
                item.get("@_AccountStatusType") or item.get("status") or "open"
            ).strip().lower()
            past_due = _money_field_to_cents(item, (), ("@_PastDueAmount", "pastDueAmount"))
            tradelines.append(
                CreditTradelineRecord(
                    name=sanitize_untrusted_text(str(name), max_len=80) or "Credit Account",
                    issuer=sanitize_untrusted_text(str(creditor_obj.get("@_Name") or item.get("issuer") or ""), max_len=60),
                    kind=_normalize_debt_kind(str(kind_raw)),
                    balance_cents=bal_cents,
                    credit_limit_cents=limit_cents,
                    apr_bps=int(item["apr_bps"]) if item.get("apr_bps") is not None else apr_pct_to_bps(item.get("apr")),
                    min_payment_cents=min_pay,
                    opened_date=opened,
                    is_overdue=bool(item.get("is_overdue") or past_due > 0),
                    is_authorized_user=is_au,
                    status="closed" if "closed" in status_raw else "open",
                    external_account_id=str(item.get("@CreditLiabilityID") or item.get("id") or ""),
                    source="array_api",
                    apr_provided=(item.get("apr_bps") is not None or item.get("apr") is not None),
                    closing_day_provided=False,
                    due_day_provided=False,
                )
            )

        # 3. Hard Inquiries
        raw_inqs = cr.get("CREDIT_INQUIRY") or cr.get("inquiries") or []
        if isinstance(raw_inqs, dict):
            raw_inqs = [raw_inqs]
        for inq in raw_inqs:
            if not isinstance(inq, dict):
                continue
            inq_date = _parse_flexible_date(
                inq.get("@_Date") or inq.get("inquiry_date") or inq.get("inquiryDate") or inq.get("date")
            )
            if not inq_date:
                continue
            repo = inq.get("CREDIT_REPOSITORY") if isinstance(inq.get("CREDIT_REPOSITORY"), dict) else {}
            bureau_raw = (
                repo.get("@_SourceType")
                or inq.get("@CreditRepositorySourceType")
                or inq.get("bureau")
                or "other"
            )
            inquiries.append(
                CreditInquiryRecord(
                    lender=str(inq.get("@_Name") or inq.get("lender") or inq.get("subscriberName") or "Unknown Lender"),
                    bureau=str(bureau_raw),
                    inquiry_date=inq_date,
                )
            )

        # 4. Public Records / Derogatories
        raw_derogs = (
            cr.get("CREDIT_PUBLIC_RECORD")
            or cr.get("publicRecords")
            or cr.get("derogatories")
            or []
        )
        if isinstance(raw_derogs, dict):
            raw_derogs = [raw_derogs]
        for der in raw_derogs:
            if not isinstance(der, dict):
                continue
            ev_date = _parse_flexible_date(
                der.get("@_FiledDate")
                or der.get("opened_date")
                or der.get("filedDate")
                or der.get("date")
            )
            if not ev_date:
                continue
            derogatories.append(
                CreditDerogatoryRecord(
                    kind=str(der.get("@_Type") or der.get("kind") or der.get("type") or "collection"),
                    opened_date=ev_date,
                    creditor=str(der.get("@_CourtName") or der.get("creditor") or der.get("lender") or ""),
                    bureau=str(der.get("@CreditRepositorySourceType") or der.get("bureau") or "other"),
                    amount_cents=_opt_money_field_to_cents(
                        der, ("amount_cents",), ("@_LegalObligationAmount", "amount")
                    ),
                )
            )

        if not reports and not tradelines and not inquiries and not derogatories:
            warnings.append("Array report payload contained no recognized credit records.")

        return CreditImportBundle(
            provider=self.provider_name,
            needs_review=False,
            reports=reports,
            tradelines=tradelines,
            inquiries=inquiries,
            derogatories=derogatories,
            warnings=warnings,
            raw_source_format="array_json",
        )

    # --- Vendor 3: CRS Credit API (Experian / Equifax / TransUnion) ---

    def _crs_get_bearer_token(self) -> str:
        if self._api_key:
            return self._api_key
        user = self._username or self._client_id
        pwd = self._password or self._client_secret
        if not user or not pwd:
            raise ConnectorAuthError(
                "CRS Credit API requires either a Bearer token or username + password.",
                provider=self.provider_name,
            )
        _status, data = self._request_json(
            "POST",
            f"{self._api_base_url}/user/login",
            json_body={"username": user, "password": pwd},
        )
        token = ""
        if isinstance(data, dict):
            token = str(data.get("token") or data.get("access_token") or data.get("jwt") or "").strip()
        if not token:
            raise ConnectorAuthError(
                "CRS Credit API login succeeded without returning a JWT token.",
                provider=self.provider_name,
            )
        return token

    def _fetch_crs(
        self,
        *,
        bureau: Optional[str] = None,
        option: Optional[str] = None,
        consumer_profile: Optional[Dict[str, Any]] = None,
    ) -> CreditImportBundle:
        bearer = self._crs_get_bearer_token()
        target_bureau = _normalize_bureau(bureau or self._bureau or "equifax")
        if target_bureau not in ("equifax", "experian", "transunion"):
            target_bureau = "equifax"
        target_option = (option or self._option or _CRS_DEFAULT_OPTIONS[target_bureau]).strip()
        # Prevent path traversal in option segment
        if not re.match(r"^[a-zA-Z0-9._-]+$", target_option):
            raise ConnectorError(f"Invalid CRS credit-report option '{target_option}'.", provider=self.provider_name)

        url = f"{self._api_base_url}/{target_bureau}/credit-report/{target_option}"
        _status, payload = self._request_json(
            "POST",
            url,
            headers={"Authorization": f"Bearer {bearer}"},
            json_body=consumer_profile or {},
        )
        return self._parse_crs_payload(
            payload if isinstance(payload, dict) else {},
            default_bureau=target_bureau,
        )

    def _parse_crs_payload(
        self, payload: Dict[str, Any], *, default_bureau: str = "equifax"
    ) -> CreditImportBundle:
        today_iso = date.today().isoformat()
        reports: List[CreditReportRecord] = []
        tradelines: List[CreditTradelineRecord] = []
        inquiries: List[CreditInquiryRecord] = []
        derogatories: List[CreditDerogatoryRecord] = []
        warnings: List[str] = []

        root = payload.get("data", payload) if isinstance(payload.get("data"), dict) else payload

        raw_scores: List[Dict[str, Any]] = []
        for key in ("scores", "creditScores", "models", "reports"):
            if isinstance(root.get(key), list):
                raw_scores.extend(s for s in root[key] if isinstance(s, dict))
        if not raw_scores and root.get("score") is not None:
            raw_scores.append(root)
        elif not raw_scores and isinstance(root.get("creditScore"), dict):
            raw_scores.append(root["creditScore"])

        for s_item in raw_scores:
            score_val = _parse_score_int(
                s_item.get("score") or s_item.get("scoreValue") or s_item.get("value")
            )
            if score_val is None:
                continue
            pulled = (
                _parse_flexible_date(
                    s_item.get("pulled_date")
                    or s_item.get("date")
                    or root.get("reportDate")
                    or root.get("createdAt")
                )
                or today_iso
            )
            reports.append(
                CreditReportRecord(
                    bureau=_normalize_bureau(str(s_item.get("bureau") or default_bureau)),
                    pulled_date=pulled,
                    model=_normalize_model(
                        str(s_item.get("model") or s_item.get("modelName") or s_item.get("scoreModel") or "fico8")
                    ),
                    score=score_val,
                    source="crs_api",
                    needs_review=False,
                )
            )

        for tl in root.get("tradelines") or root.get("trades") or root.get("accounts") or []:
            if not isinstance(tl, dict):
                continue
            name = tl.get("accountName") or tl.get("creditorName") or tl.get("name") or "Credit Account"
            ecoa = str(tl.get("ecoa") or tl.get("ownership") or "").lower()
            tradelines.append(
                CreditTradelineRecord(
                    name=sanitize_untrusted_text(str(name), max_len=80) or "Credit Account",
                    issuer=sanitize_untrusted_text(str(tl.get("creditorName") or tl.get("issuer") or ""), max_len=60),
                    kind=_normalize_debt_kind(str(tl.get("accountType") or tl.get("kind") or "credit_card")),
                    balance_cents=_money_field_to_cents(tl, ("balance_cents",), ("currentBalance", "balance")),
                    credit_limit_cents=_money_field_to_cents(
                        tl, ("credit_limit_cents",), ("creditLimit", "highCredit", "limit")
                    ),
                    apr_bps=int(tl["apr_bps"]) if tl.get("apr_bps") is not None else apr_pct_to_bps(tl.get("apr")),
                    min_payment_cents=_money_field_to_cents(
                        tl, ("min_payment_cents",), ("monthlyPayment", "minimumPayment")
                    ),
                    opened_date=_parse_flexible_date(tl.get("dateOpened") or tl.get("opened_date") or tl.get("openedDate")),
                    is_overdue=bool(tl.get("isOverdue") or tl.get("pastDueAmount")),
                    is_authorized_user=bool(tl.get("is_authorized_user") or "authorized" in ecoa or ecoa == "a"),
                    status="closed" if "closed" in str(tl.get("status") or "open").lower() else "open",
                    external_account_id=str(tl.get("accountNumber") or tl.get("id") or ""),
                    source="crs_api",
                    apr_provided=(tl.get("apr_bps") is not None or tl.get("apr") is not None),
                    closing_day_provided=False,
                    due_day_provided=False,
                )
            )

        for inq in root.get("inquiries") or []:
            if not isinstance(inq, dict):
                continue
            inq_date = _parse_flexible_date(inq.get("inquiryDate") or inq.get("inquiry_date") or inq.get("date"))
            if not inq_date:
                continue
            inquiries.append(
                CreditInquiryRecord(
                    lender=str(inq.get("subscriberName") or inq.get("lender") or inq.get("name") or "Unknown Lender"),
                    bureau=str(inq.get("bureau") or default_bureau),
                    inquiry_date=inq_date,
                )
            )

        raw_derogs = list(root.get("derogatories") or []) + list(root.get("publicRecords") or []) + list(root.get("collections") or [])
        for der in raw_derogs:
            if not isinstance(der, dict):
                continue
            ev_date = _parse_flexible_date(
                der.get("openedDate") or der.get("opened_date") or der.get("filedDate") or der.get("date")
            )
            if not ev_date:
                continue
            derogatories.append(
                CreditDerogatoryRecord(
                    kind=str(der.get("kind") or der.get("type") or "collection"),
                    opened_date=ev_date,
                    creditor=str(der.get("creditor") or der.get("lender") or der.get("agency") or ""),
                    bureau=str(der.get("bureau") or default_bureau),
                    amount_cents=_opt_money_field_to_cents(der, ("amount_cents",), ("amount", "balance")),
                )
            )

        if not reports and not tradelines and not inquiries and not derogatories:
            warnings.append("CRS Credit API response contained no recognized credit records.")

        return CreditImportBundle(
            provider=self.provider_name,
            needs_review=False,
            reports=reports,
            tradelines=tradelines,
            inquiries=inquiries,
            derogatories=derogatories,
            warnings=warnings,
            raw_source_format="crs_json",
        )

    # --- Vendor 4: Custom Credit Score API (ChronoX Structured JSON Bridge) ---

    def _fetch_custom(self) -> CreditImportBundle:
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        _status, payload = self._request_json("GET", self._api_base_url, headers=headers)
        if not isinstance(payload, dict):
            raise ConnectorError(
                "Custom Credit Score API must return a JSON object.",
                provider=self.provider_name,
            )
        importer = OfficialCreditReportImporter()
        bundle = importer.import_structured(payload)
        bundle.provider = self.provider_name
        bundle.needs_review = False
        bundle.raw_source_format = "custom_credit_json"
        for rep in bundle.reports:
            rep.source = "custom_credit_api"
            rep.needs_review = False
        for tl in bundle.tradelines:
            tl.source = "custom_credit_api"
        return bundle
