"""
Opt-In Local Read-Only IMAP Email Scanner for ChronoX Finance.

Security & read-only guarantees (Proposal A §5 + Gate 1 #53):
- Uses Python stdlib `imaplib.IMAP4_SSL` + `email` ONLY (App Password auth for v1).
- Opens mailbox strictly read-only with `EXAMINE` (NEVER `SELECT`, `STORE`, or `EXPUNGE`).
- Fetches messages exclusively with `UID SEARCH SINCE` and
  `UID FETCH <uid> (BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)] BODY.PEEK[TEXT])`
  so emails are NEVER marked `\\Seen` on the mail server.
- Deterministic regex/Decimal bill & receipt extractor strips HTML, redacts PII, and
  neutralizes prompt-injection directives before producing review-queue candidates.
- Never modifies the ledger automatically — candidates go into `email_scan_queue` for
  explicit user Approve/Dismiss.
"""

from __future__ import annotations

import email
import html
import imaplib
import ipaddress
import logging
import re
import ssl
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Callable, Dict, List, Literal, Optional, Sequence, Tuple

from chronox_finance.connectors.base import (
    ConnectorAuthError,
    ConnectorError,
    HostNotAllowedError,
    decimal_to_cents,
    utc_now_iso,
)
from chronox_finance.security import sanitize_untrusted_text

logger = logging.getLogger(__name__)

# IMAP fetch spec strictly using BODY.PEEK so \Seen flag is NEVER modified
IMAP_PEEK_FETCH_SPEC = "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)] BODY.PEEK[TEXT])"

_BILL_KEYWORDS_RE = re.compile(
    r"\b("
    r"statement\s+(?:is\s+)?(?:ready|available|closing|balance)|"
    r"monthly\s+(?:bill|statement|invoice)|"
    r"bill\s+(?:is\s+)?(?:ready|due|available|reminder)|"
    r"payment\s+(?:due|reminder|scheduled|upcoming)|"
    r"amount\s+due|balance\s+due|total\s+due|minimum\s+payment|"
    r"auto[\s-]?pay\s+(?:scheduled|reminder|upcoming|will\s+be)|"
    r"invoice\s*(?:#|\d+|due|ready|attached)|"
    r"upcoming\s+(?:bill|charge|renewal|payment)|"
    r"subscription\s+renewal|past\s+due|due\s+date"
    r")\b",
    re.IGNORECASE,
)

_RECEIPT_KEYWORDS_RE = re.compile(
    r"\b("
    r"receipt|order\s+confirmation|payment\s+(?:received|confirmation|processed|thank\s+you)|"
    r"thank\s+you\s+for\s+your\s+(?:order|payment|purchase)|"
    r"your\s+(?:purchase|order|receipt)|"
    r"we\s+(?:received|processed)\s+your\s+payment|"
    r"card\s+was\s+charged|has\s+been\s+charged|you\s+paid|you\s+spent"
    r")\b",
    re.IGNORECASE,
)

_NOISE_FILTER_RE = re.compile(
    r"\b("
    r"verification\s+code|one[\s-]?time\s+passcode|otp\b|2fa\b|"
    r"reset\s+your\s+password|password\s+reset|sign[\s-]?in\s+attempt|"
    r"security\s+alert|confirm\s+your\s+email|unsubscribe\s+from\s+all\s+marketing"
    r")\b",
    re.IGNORECASE,
)

# Priority labeled amount extraction (matches e.g. "Amount Due: $124.50", "Total: USD 42.99")
_LABELED_AMOUNT_RE = re.compile(
    r"(?:amount\s+due|total\s+due|balance\s+due|new\s+balance|statement\s+balance|"
    r"minimum\s+payment(?:\s+due)?|payment\s+amount|total\s+charged|order\s+total|"
    r"amount\s+paid|grand\s+total|total\s+price|invoice\s+total|total)\s*"
    r"[:\-=]?\s*(?:USD\s*)?\$\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})|[0-9]+(?:\.[0-9]{2}))",
    re.IGNORECASE,
)

_GENERIC_DOLLAR_RE = re.compile(
    r"(?:USD\s*)?\$\s*([0-9]{1,3}(?:,[0-9]{3})*\.[0-9]{2}|[0-9]+\.[0-9]{2})"
)

_DUE_DATE_CONTEXT_RE = re.compile(
    r"(?:due\s+(?:date|on|by)|payment\s+due(?:\s+(?:date|on|by))?|"
    r"pay\s+by|scheduled\s+(?:for|on)|auto[\s-]?pay\s+(?:on|date))\s*[:\-]?\s*"
    r"("
    r"\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}/\d{1,2}/\d{2,4}"
    r"|(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\.?\s+\d{1,2},?\s+\d{4}"
    r")",
    re.IGNORECASE,
)

_EXTRA_INJECTION_RE = re.compile(
    r"(?:<\|[^|]{1,32}\|>|###\s*(?:system|instruction|assistant)\b|"
    r"\b(?:execute|run|invoke)\s+(?:tool|command|function|action)\b|"
    r"\bforget\s+(?:all\s+)?(?:previous|prior)\b|"
    r"\boverride\s+(?:rules?|safety|instructions?)\b)",
    re.IGNORECASE,
)

_HTML_SCRIPT_STYLE_RE = re.compile(r"<(?:script|style)[^>]*>[\s\S]*?</(?:script|style)>", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_VENDOR_NOISE_RE = re.compile(
    r"\b(billing|payments?|statements?|no[\s-]?reply|noreply|notifications?|"
    r"customer\s+(?:service|support|care)|auto[\s-]?pay|receipts?|orders?|team|support|info|alerts?)\b",
    re.IGNORECASE,
)

_DATE_PARSE_FMTS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%b %d %Y",
    "%B %d %Y",
)

_GENERIC_MAIL_DOMAINS = frozenset(
    {"gmail", "yahoo", "outlook", "hotmail", "icloud", "me", "aol", "proton", "protonmail", "fastmail"}
)


@dataclass
class IMAPConfig:
    host: str
    username: str
    app_password: str
    port: int = 993
    mailbox: str = "INBOX"
    lookback_days: int = 30
    max_messages: int = 100
    timeout_seconds: float = 20.0

    def __post_init__(self) -> None:
        self.host = (self.host or "").strip().lower()
        self.username = (self.username or "").strip()
        self.app_password = (self.app_password or "").strip()
        self.mailbox = (self.mailbox or "INBOX").strip()
        self.port = int(self.port)
        self.lookback_days = max(1, min(int(self.lookback_days), 365))
        self.max_messages = max(1, min(int(self.max_messages), 500))
        self.timeout_seconds = max(3.0, min(float(self.timeout_seconds), 60.0))

        if not self.host:
            raise HostNotAllowedError("IMAP host is required", provider="email_imap")
        if self.port < 1 or self.port > 65535:
            raise HostNotAllowedError("IMAP port must be between 1 and 65535", provider="email_imap")
        _validate_imap_host(self.host)
        if not self.username or not self.app_password:
            raise ConnectorAuthError(
                "IMAP username and App Password are required",
                provider="email_imap",
            )

    def __repr__(self) -> str:
        return (
            f"IMAPConfig(host={self.host!r}, port={self.port}, "
            f"username={self.username!r}, mailbox={self.mailbox!r}, app_password='***')"
        )


@dataclass
class EmailScanCandidate:
    uid_key: str
    sender: str
    subject: str
    received_at: str
    extracted_vendor: str
    amount_cents: Optional[int]
    due_date: Optional[str]
    kind: Literal["bill", "receipt"]
    confidence: float
    snippet: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class EmailScanResult:
    scanned_count: int = 0
    matched_count: int = 0
    candidates: List[EmailScanCandidate] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    read_only_verified: bool = True
    scanned_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _validate_imap_host(host: str) -> None:
    if any(ch.isspace() for ch in host) or "/" in host:
        raise HostNotAllowedError("Invalid IMAP hostname", provider="email_imap")
    if host in ("localhost", "localhost.localdomain") or host.endswith((".local", ".internal")):
        raise HostNotAllowedError(f"Blocked local/internal IMAP host '{host}'", provider="email_imap")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise HostNotAllowedError(f"Blocked non-public IMAP IP '{host}'", provider="email_imap")


def _decode_mime_header(raw_value: str) -> str:
    if not raw_value:
        return ""
    parts: List[str] = []
    for chunk, charset in decode_header(raw_value):
        if isinstance(chunk, bytes):
            enc = charset or "utf-8"
            try:
                parts.append(chunk.decode(enc, errors="replace"))
            except LookupError:
                parts.append(chunk.decode("utf-8", errors="replace"))
        else:
            parts.append(str(chunk))
    return "".join(parts).strip()


def sanitize_email_text(text: str, *, max_len: int = 240) -> str:
    """
    Strip HTML/scripts/control chars, neutralize prompt-injection directives, and redact PII
    from untrusted email headers and bodies.
    """
    raw = text or ""
    raw = _HTML_SCRIPT_STYLE_RE.sub(" ", raw)
    raw = _HTML_TAG_RE.sub(" ", raw)
    raw = html.unescape(raw)
    raw = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", raw)
    raw = _EXTRA_INJECTION_RE.sub("[filtered]", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return sanitize_untrusted_text(raw, max_len=max_len)


def _extract_vendor(sender_header: str, subject: str) -> str:
    display_name, addr = parseaddr(sender_header or "")
    cleaned_name = _VENDOR_NOISE_RE.sub(" ", display_name or "")
    cleaned_name = re.sub(r"[^A-Za-z0-9&'.\- ]+", " ", cleaned_name)
    cleaned_name = re.sub(r"\s+", " ", cleaned_name).strip(" -.")

    if len(cleaned_name) >= 2:
        return sanitize_untrusted_text(cleaned_name, max_len=80)

    if "@" in addr:
        domain = addr.split("@", 1)[1].lower().strip()
        labels = [lbl for lbl in domain.split(".") if lbl and lbl not in ("com", "net", "org", "co", "io", "us", "mail", "email", "billing", "notify", "alerts", "e", "m")]
        if labels:
            brand = labels[-1]
            if brand not in _GENERIC_MAIL_DOMAINS:
                return brand.replace("-", " ").title()[:80]

    # Fallback: first capitalized tokens in subject
    sub_clean = _VENDOR_NOISE_RE.sub(" ", subject or "")
    sub_clean = re.sub(r"\s+", " ", sub_clean).strip()
    return sanitize_untrusted_text(sub_clean[:40] or "Unknown Biller", max_len=80)


def _parse_date_candidate(raw: str) -> Optional[str]:
    text = re.sub(r"\s+", " ", (raw or "").replace(".", "")).strip()
    # Normalize "Sept" -> "Sep"
    text = re.sub(r"\bSept\b", "Sep", text, flags=re.IGNORECASE)
    for fmt in _DATE_PARSE_FMTS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _extract_amount_cents(subject: str, plain_body: str) -> Optional[int]:
    combined = f"{subject}\n{plain_body}"
    for rx in (_LABELED_AMOUNT_RE, _GENERIC_DOLLAR_RE):
        for match in rx.finditer(combined):
            amt_str = match.group(1)
            cents = decimal_to_cents(Decimal(amt_str.replace(",", "")))
            if 1 <= cents <= 100_000_000_00:
                return cents
    return None


def _extract_due_date(subject: str, plain_body: str) -> Optional[str]:
    combined = f"{subject}\n{plain_body}"
    for match in _DUE_DATE_CONTEXT_RE.finditer(combined):
        parsed = _parse_date_candidate(match.group(1))
        if parsed:
            return parsed
    return None


def _extract_plain_body(raw_body: bytes) -> str:
    """Decode MIME / text / HTML body payload cleanly without executing or loading remote content."""
    if not raw_body:
        return ""
    try:
        msg = email.message_from_bytes(raw_body)
        if msg.is_multipart():
            text_parts: List[str] = []
            html_parts: List[str] = []
            for part in msg.walk():
                ctype = (part.get_content_type() or "").lower()
                disp = str(part.get("Content-Disposition") or "").lower()
                if "attachment" in disp:
                    continue
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                charset = part.get_content_charset() or "utf-8"
                decoded = payload.decode(charset, errors="replace")
                if ctype == "text/plain":
                    text_parts.append(decoded)
                elif ctype == "text/html":
                    html_parts.append(decoded)
            if text_parts:
                return "\n".join(text_parts)
            if html_parts:
                raw_html = "\n".join(html_parts)
                raw_html = _HTML_SCRIPT_STYLE_RE.sub(" ", raw_html)
                return html.unescape(_HTML_TAG_RE.sub(" ", raw_html))
    except Exception as exc:
        logger.debug("Multipart decode fell back to raw body decode: %s", exc)

    decoded = raw_body.decode("utf-8", errors="replace")
    decoded = _HTML_SCRIPT_STYLE_RE.sub(" ", decoded)
    decoded = _HTML_TAG_RE.sub(" ", decoded)
    return html.unescape(decoded)


def extract_email_candidate(
    *,
    uid_key: str,
    raw_headers: bytes,
    raw_body: bytes,
) -> Optional[EmailScanCandidate]:
    """
    Deterministically inspect a single email's headers & text body and extract a normalized
    bill or receipt candidate for the review queue. Returns None if not a financial bill/receipt.
    """
    hdr_msg = email.message_from_bytes(raw_headers or b"")
    raw_from = _decode_mime_header(str(hdr_msg.get("From", "")))
    raw_subject = _decode_mime_header(str(hdr_msg.get("Subject", "")))
    raw_date = str(hdr_msg.get("Date", "")).strip()

    plain_body = _extract_plain_body(raw_body)
    search_space = f"{raw_subject}\n{plain_body[:4000]}"

    bill_hit = bool(_BILL_KEYWORDS_RE.search(search_space))
    receipt_hit = bool(_RECEIPT_KEYWORDS_RE.search(search_space))

    if not bill_hit and not receipt_hit:
        return None

    # Ignore security / OTP / password-reset emails unless a concrete dollar amount is present
    amount_cents = _extract_amount_cents(raw_subject, plain_body[:6000])
    if _NOISE_FILTER_RE.search(raw_subject) and amount_cents is None:
        return None

    due_date = _extract_due_date(raw_subject, plain_body[:6000])

    # Classify kind: explicit bill/due signals take priority over receipt wording
    if bill_hit and (due_date is not None or not receipt_hit):
        kind: Literal["bill", "receipt"] = "bill"
    else:
        kind = "receipt"

    received_at = utc_now_iso()
    if raw_date:
        try:
            dt = parsedate_to_datetime(raw_date)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            received_at = dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        except (TypeError, ValueError, OverflowError):
            pass

    vendor = _extract_vendor(raw_from, raw_subject)
    # Confidence scoring
    confidence = 0.45
    if amount_cents is not None:
        confidence += 0.30
    if due_date is not None and kind == "bill":
        confidence += 0.15
    if vendor and vendor != "Unknown Biller":
        confidence += 0.10
    confidence = round(min(confidence, 0.99), 2)

    # Strictly sanitize all text fields returned for UI / queue storage (zero prompt injection)
    safe_sender = sanitize_email_text(raw_from, max_len=120)
    safe_subject = sanitize_email_text(raw_subject, max_len=160)
    safe_vendor = sanitize_email_text(vendor, max_len=80) or "Unknown Biller"
    safe_snippet = sanitize_email_text(plain_body[:500], max_len=200)

    return EmailScanCandidate(
        uid_key=uid_key,
        sender=safe_sender,
        subject=safe_subject,
        received_at=received_at,
        extracted_vendor=safe_vendor,
        amount_cents=amount_cents,
        due_date=due_date,
        kind=kind,
        confidence=confidence,
        snippet=safe_snippet,
    )


def _parse_imap_fetch_parts(fetch_data: Sequence[Any]) -> Tuple[bytes, bytes]:
    """Extract header bytes and body bytes from an `imap.uid('FETCH', ...)` response."""
    header_chunks: List[bytes] = []
    body_chunks: List[bytes] = []
    for item in fetch_data:
        if not isinstance(item, tuple) or len(item) < 2:
            continue
        meta = item[0] if isinstance(item[0], bytes) else str(item[0]).encode("ascii", errors="ignore")
        payload = item[1] if isinstance(item[1], bytes) else str(item[1]).encode("utf-8", errors="replace")
        meta_upper = meta.upper()
        if b"HEADER" in meta_upper:
            header_chunks.append(payload)
        elif b"TEXT" in meta_upper or b"BODY" in meta_upper:
            body_chunks.append(payload)
        elif not header_chunks:
            header_chunks.append(payload)
        else:
            body_chunks.append(payload)
    return b"".join(header_chunks), b"".join(body_chunks)


def _default_imap_factory(host: str, port: int, timeout: float) -> imaplib.IMAP4_SSL:
    ctx = ssl.create_default_context()
    return imaplib.IMAP4_SSL(host=host, port=port, ssl_context=ctx, timeout=timeout)


class EmailScanner:
    """
    Strictly read-only local IMAP scanner (`EXAMINE` + `BODY.PEEK`).

    Never calls `SELECT`, `STORE`, `COPY`, `DELETE`, or `EXPUNGE`, and never
    modifies `\\Seen` flags on the remote mailbox.
    """

    provider_name = "email_imap"

    def __init__(
        self,
        config: IMAPConfig,
        *,
        imap_factory: Optional[Callable[[str, int, float], Any]] = None,
    ) -> None:
        self.config = config
        self._imap_factory = imap_factory or _default_imap_factory

    def _open_readonly_session(self) -> Any:
        try:
            conn = self._imap_factory(self.config.host, self.config.port, self.config.timeout_seconds)
        except Exception as exc:
            raise ConnectorError(
                f"Failed to connect to IMAP server {self.config.host}:{self.config.port}: {exc}",
                provider=self.provider_name,
            ) from exc

        try:
            status, _ = conn.login(self.config.username, self.config.app_password)
            if str(status).upper() != "OK":
                raise ConnectorAuthError("IMAP login rejected", provider=self.provider_name)
        except ConnectorAuthError:
            self._safe_logout(conn)
            raise
        except imaplib.IMAP4.error as exc:
            self._safe_logout(conn)
            raise ConnectorAuthError(
                f"IMAP authentication failed (use an App Password): {exc}",
                provider=self.provider_name,
            ) from exc

        # CRITICAL READ-ONLY INVARIANT: Issue IMAP EXAMINE (in Python stdlib imaplib,
        # `select(mailbox, readonly=True)` sends `EXAMINE` on the wire).
        try:
            if hasattr(conn, "examine"):
                status, data = conn.examine(self.config.mailbox)
            else:
                status, data = conn.select(self.config.mailbox, readonly=True)
            if str(status).upper() != "OK":
                raise ConnectorError(
                    f"Could not open IMAP mailbox '{self.config.mailbox}' in read-only mode",
                    provider=self.provider_name,
                )
        except ConnectorError:
            self._safe_logout(conn)
            raise
        except Exception as exc:
            self._safe_logout(conn)
            raise ConnectorError(
                f"IMAP EXAMINE failed on '{self.config.mailbox}': {exc}",
                provider=self.provider_name,
            ) from exc

        return conn, data

    @staticmethod
    def _safe_logout(conn: Any) -> None:
        try:
            conn.logout()
        except Exception as exc:
            logger.debug("Ignoring IMAP logout cleanup error: %s", exc)

    def test_connection(self) -> Dict[str, Any]:
        """Verify IMAP credentials and read-only `EXAMINE` access without fetching messages."""
        conn, data = self._open_readonly_session()
        try:
            msg_count = 0
            if data and isinstance(data, list) and data[0]:
                raw_cnt = data[0].decode("ascii", errors="ignore") if isinstance(data[0], bytes) else str(data[0])
                if raw_cnt.strip().isdigit():
                    msg_count = int(raw_cnt.strip())
            return {
                "ok": True,
                "host": self.config.host,
                "mailbox": self.config.mailbox,
                "message_count": msg_count,
                "read_only": True,
            }
        finally:
            self._safe_logout(conn)

    def scan(
        self,
        *,
        since_date: Optional[date] = None,
        existing_uid_keys: Optional[Sequence[str]] = None,
    ) -> EmailScanResult:
        """
        Scan recent messages using `EXAMINE`, `UID SEARCH SINCE`, and `BODY.PEEK`
        so messages are NEVER marked `\\Seen`.
        """
        cutoff = since_date or (datetime.now(timezone.utc).date() - timedelta(days=self.config.lookback_days))
        # IMAP SINCE date format: DD-Mon-YYYY (English month abbreviations per RFC 3501)
        months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
        imap_since = f"{cutoff.day:02d}-{months[cutoff.month - 1]}-{cutoff.year}"

        known_keys = set(existing_uid_keys or ())
        conn, _ = self._open_readonly_session()
        candidates: List[EmailScanCandidate] = []
        warnings: List[str] = []
        scanned = 0

        try:
            status, search_data = conn.uid("SEARCH", None, "SINCE", imap_since)
            if str(status).upper() != "OK":
                raise ConnectorError("IMAP UID SEARCH SINCE failed", provider=self.provider_name)

            raw_uid_list = search_data[0] if search_data and search_data[0] else b""
            if isinstance(raw_uid_list, bytes):
                uids = [tok.decode("ascii", errors="ignore") for tok in raw_uid_list.split() if tok]
            else:
                uids = [tok for tok in str(raw_uid_list).split() if tok]

            # Most recent messages first, bounded by max_messages
            uids = list(reversed(uids))[: self.config.max_messages]

            for uid in uids:
                uid_key = f"{self.config.host}:{self.config.mailbox}:{uid}"
                if uid_key in known_keys:
                    continue

                f_status, fetch_data = conn.uid("FETCH", uid, IMAP_PEEK_FETCH_SPEC)
                if str(f_status).upper() != "OK" or not fetch_data:
                    warnings.append(f"Skipped unreadable UID {uid}")
                    continue

                scanned += 1
                raw_headers, raw_body = _parse_imap_fetch_parts(fetch_data)
                cand = extract_email_candidate(
                    uid_key=uid_key,
                    raw_headers=raw_headers,
                    raw_body=raw_body,
                )
                if cand is not None:
                    candidates.append(cand)
        finally:
            self._safe_logout(conn)

        return EmailScanResult(
            scanned_count=scanned,
            matched_count=len(candidates),
            candidates=candidates,
            warnings=warnings,
            read_only_verified=True,
        )
