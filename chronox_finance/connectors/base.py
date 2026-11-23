"""
Base protocols, normalized financial dataclasses, exact integer math, and hardened HTTP helpers
for all ChronoX Finance external connectors.

Design invariants:
- Monetary values are represented in exact integer cents (`int`).
- Asset quantities are represented in exact `quantity_e8` (`10^-8` units — satoshis / fractional shares).
- Asset unit prices are represented in exact `price_e8` (`10^-8` USD — supports sub-cent crypto like SHIB).
- Market values in cents are computed via ONE canonical function `compute_market_value_cents()`
  using `Decimal` + `ROUND_HALF_EVEN`.
- All HTTP clients disable automatic redirects (`follow_redirects=False`) and accept an injectable
  `httpx.BaseTransport` for 100% deterministic offline unit tests.
"""

from __future__ import annotations

import ipaddress
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol, Union
from urllib.parse import urlparse

import httpx

from chronox_finance.models import compute_holding_value_cents
from chronox_finance.security import validate_external_api_url

logger = logging.getLogger(__name__)

SCALE_E8 = 100_000_000  # 10^8
_DECIMAL_E8 = Decimal(SCALE_E8)
_DECIMAL_CENTS = Decimal(100)
_ONE = Decimal("1")

_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ConnectorError(Exception):
    """Base error raised by any external connector."""

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        status_code: Optional[int] = None,
        error_code: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.error_code = error_code


class ConnectorAuthError(ConnectorError):
    """Authentication, token expiration, or item re-link error."""


class ReadOnlyViolationError(ConnectorError):
    """Raised whenever an API key, token, scope, or request violates strict read-only policy."""


class HostNotAllowedError(ConnectorError):
    """Raised whenever a URL or host fails strict allowlist or SSRF validation."""


class NotConfiguredForLocalDesktop(ConnectorError):
    """Raised by enterprise/B2B SaaS adapters (e.g. FCRA bureau APIs) on local desktop builds."""


# ---------------------------------------------------------------------------
# Exact Decimal / Integer Math Helpers (Zero IEEE-754 Drift)
# ---------------------------------------------------------------------------


def utc_now_iso() -> str:
    """Return an ISO-8601 UTC timestamp string (second precision, trailing Z)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def to_decimal(value: Union[Decimal, int, float, str, None]) -> Decimal:
    """Convert numeric or string input to an exact Decimal (returns Decimal(0) for None/empty)."""
    if value is None:
        return Decimal(0)
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError("Boolean is not a valid numeric value")
    if isinstance(value, int):
        return Decimal(value)
    text = str(value).strip().replace(",", "").replace("$", "")
    if not text or text.lower() in ("none", "null", "n/a", "-", "--"):
        return Decimal(0)
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid decimal value: {value!r}") from exc


def decimal_to_e8(value: Union[Decimal, int, float, str, None]) -> int:
    """Convert a decimal quantity or USD price into exact integer 10^-8 units (ROUND_HALF_EVEN)."""
    dec = to_decimal(value)
    return int((dec * _DECIMAL_E8).quantize(_ONE, rounding=ROUND_HALF_EVEN))


def decimal_to_cents(value: Union[Decimal, int, float, str, None]) -> int:
    """Convert a USD decimal value into exact integer cents (ROUND_HALF_EVEN)."""
    dec = to_decimal(value)
    return int((dec * _DECIMAL_CENTS).quantize(_ONE, rounding=ROUND_HALF_EVEN))


def apr_pct_to_bps(apr_pct: Union[Decimal, int, float, str, None]) -> int:
    """Convert an APR percentage (e.g. 24.99) into integer basis points (2499)."""
    dec = to_decimal(apr_pct)
    if dec < 0:
        return 0
    return int((dec * _DECIMAL_CENTS).quantize(_ONE, rounding=ROUND_HALF_EVEN))


def compute_market_value_cents(quantity_e8: int, price_e8: int) -> int:
    """
    Canonical market value calculation in exact integer cents.

    Delegates directly to `chronox_finance.models.compute_holding_value_cents`
    so the entire codebase shares ONE single `Decimal` + `ROUND_HALF_EVEN` formula.
    """
    return compute_holding_value_cents(quantity_e8, price_e8)


def e8_to_str(value_e8: int, *, strip_trailing_zeros: bool = True) -> str:
    """Format an integer 10^-8 quantity or price as a clean decimal string."""
    dec = Decimal(int(value_e8)) / _DECIMAL_E8
    formatted = f"{dec:.8f}"
    if strip_trailing_zeros and "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")
    return formatted


# ---------------------------------------------------------------------------
# Strict Host Allowlist & SSRF-Safe HTTP Helpers
# ---------------------------------------------------------------------------


def validate_strict_external_url(
    url: str,
    *,
    provider: str = "",
    allowed_hosts: Optional[Iterable[str]] = None,
    require_https: bool = True,
    allow_credentials: bool = False,
) -> str:
    """
    Validate an external connector URL against SSRF and host allowlists.

    Always delegates base validation to `chronox_finance.security.validate_external_api_url`
    and then enforces HTTPS, blocks loopback/private/reserved IPs, and checks `allowed_hosts`.
    """
    raw = (url or "").strip()
    if not raw:
        raise HostNotAllowedError("URL must not be empty", provider=provider)
    try:
        validated = validate_external_api_url(raw)
    except ValueError as exc:
        raise HostNotAllowedError(str(exc), provider=provider) from exc

    parsed = urlparse(validated)
    host = (parsed.hostname or "").lower()
    if not host:
        raise HostNotAllowedError("URL must include a hostname", provider=provider)

    if require_https and parsed.scheme != "https":
        raise HostNotAllowedError(f"HTTPS is required for {provider or 'external'} URLs", provider=provider)

    if not allow_credentials and (parsed.username is not None or parsed.password is not None):
        raise HostNotAllowedError("Embedded URL credentials are not permitted", provider=provider)

    if allowed_hosts is not None:
        normalized_allowed = {h.strip().lower() for h in allowed_hosts if h and h.strip()}
        if host not in normalized_allowed:
            raise HostNotAllowedError(
                f"Host '{host}' is not in the allowlist for {provider or 'connector'}: "
                f"{sorted(normalized_allowed)}",
                provider=provider,
            )
        return validated

    # Without an explicit allowlist (e.g. SimpleFIN claim/access URLs), strictly reject
    # loopback, private RFC1918/ULA, link-local, multicast, reserved, and local hostnames.
    if host in ("localhost", "localhost.localdomain") or host.endswith((".local", ".internal", ".localhost")):
        raise HostNotAllowedError(f"Local/internal host '{host}' is blocked", provider=provider)

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None

    if ip is not None and (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise HostNotAllowedError(f"Blocked non-public IP address '{host}'", provider=provider)

    return validated


def build_http_client(
    *,
    timeout: float = 20.0,
    transport: Optional[httpx.BaseTransport] = None,
    headers: Optional[Dict[str, str]] = None,
) -> httpx.Client:
    """Create a hardened `httpx.Client` with redirects disabled and optional mock transport."""
    return httpx.Client(
        timeout=timeout,
        transport=transport,
        headers=headers,
        follow_redirects=False,
    )


def request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    provider: str = "",
    max_retries: int = 2,
    backoff_base: float = 0.25,
    sleep_fn: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> httpx.Response:
    """
    Execute an HTTP request with bounded retry/backoff on transient transport errors and 429/5xx.
    Never follows HTTP redirects (prevents cross-host token leakage).
    """
    attempts = max(1, int(max_retries) + 1)
    last_exc: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            response = client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            last_exc = exc
            if attempt + 1 >= attempts:
                raise ConnectorError(
                    f"{provider or 'HTTP'} transport error after {attempts} attempts: {exc}",
                    provider=provider,
                ) from exc
            if backoff_base > 0:
                sleep_fn(min(backoff_base * (2**attempt), 4.0))
            continue

        if response.status_code in _RETRYABLE_STATUS_CODES and attempt + 1 < attempts:
            retry_after_raw = response.headers.get("Retry-After", "")
            delay = min(backoff_base * (2**attempt), 4.0)
            if retry_after_raw.isdigit():
                delay = min(float(int(retry_after_raw)), 5.0)
            if delay > 0:
                sleep_fn(delay)
            continue

        if 300 <= response.status_code < 400:
            raise HostNotAllowedError(
                f"Unexpected HTTP redirect ({response.status_code}) blocked for security",
                provider=provider,
                status_code=response.status_code,
            )

        return response

    raise ConnectorError(
        f"{provider or 'HTTP'} request failed: {last_exc}",
        provider=provider,
    )


# ---------------------------------------------------------------------------
# Normalized Financial Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class NormalizedAccount:
    external_account_id: str
    name: str
    account_type: str = "checking"  # checking | savings | credit | trading | cash | loan
    balance_cents: int = 0
    available_cents: Optional[int] = None
    limit_cents: Optional[int] = None
    institution: str = ""
    official_name: str = ""
    mask: str = ""
    subtype: str = ""
    currency: str = "USD"
    is_liquid: bool = True
    provider: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class NormalizedTransaction:
    external_id: str
    external_account_id: str
    tx_date: str  # ISO YYYY-MM-DD
    merchant: str
    amount_cents: int  # Always >= 0 integer cents; direction encoded in tx_type
    tx_type: str = "expense"  # expense | income | transfer
    memo: str = ""
    subcategory: str = ""
    pending: bool = False
    currency: str = "USD"
    source: str = ""
    raw_category: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class NormalizedLiability:
    external_account_id: str
    name: str
    issuer: str = ""
    kind: str = "credit_card"  # credit_card | student_loan | mortgage | auto_loan | personal_loan
    balance_cents: int = 0
    statement_balance_cents: Optional[int] = None
    credit_limit_cents: int = 0
    apr_bps: int = 0
    cash_advance_apr_bps: Optional[int] = None
    balance_transfer_apr_bps: Optional[int] = None
    min_payment_cents: int = 0
    last_payment_amount_cents: Optional[int] = None
    last_payment_date: Optional[str] = None
    next_payment_due_date: Optional[str] = None
    last_statement_issue_date: Optional[str] = None
    is_overdue: bool = False
    origination_date: Optional[str] = None
    origination_principal_cents: Optional[int] = None
    source: str = "plaid_liabilities"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class NormalizedHolding:
    symbol: str
    name: str = ""
    asset_class: str = "equity"  # equity | etf | crypto | cash | mutual_fund | bond
    quantity_e8: int = 0
    price_e8: int = 0
    cost_basis_cents: int = 0
    market_value_cents: int = 0
    external_account_id: str = ""
    currency: str = "USD"
    source: str = ""
    updated_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        self.symbol = (self.symbol or "").strip().upper()
        if self.market_value_cents == 0 and self.quantity_e8 != 0 and self.price_e8 != 0:
            self.market_value_cents = compute_market_value_cents(self.quantity_e8, self.price_e8)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class NormalizedSyncResult:
    provider: str
    accounts: List[NormalizedAccount] = field(default_factory=list)
    added: List[NormalizedTransaction] = field(default_factory=list)
    modified: List[NormalizedTransaction] = field(default_factory=list)
    removed_external_ids: List[str] = field(default_factory=list)
    next_cursor: Optional[str] = None
    has_more: bool = False
    warnings: List[str] = field(default_factory=list)
    synced_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


class ConnectorProtocol(Protocol):
    """Common interface for bank aggregation connectors (Plaid, SimpleFIN)."""

    provider_name: str

    def sync(self, *args: Any, **kwargs: Any) -> NormalizedSyncResult: ...


class BrokerConnectorProtocol(Protocol):
    """Common interface for strictly read-only broker & crypto exchange connectors."""

    provider_name: str

    def fetch_holdings(self, *args: Any, **kwargs: Any) -> List[NormalizedHolding]: ...
