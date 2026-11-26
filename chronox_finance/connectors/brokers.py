"""
Direct Broker & Crypto Exchange Connectors with Enforced Read-Only Policies
and Exact Integer Math (`quantity_e8`, `price_e8`, `ROUND_HALF_EVEN`).

Includes:
1. `CoinbaseConnector`: Official CDP API key + 120s ES256/EdDSA JWT (`cryptography`).
   Strictly calls `GET /api/v3/brokerage/key_permissions` and REFUSES any key with
   `can_trade` or `can_transfer` (`ReadOnlyViolationError`).
2. `RobinhoodCryptoConnector`: Official `https://trading.robinhood.com` Ed25519 per-request
   signing (`x-api-key`, `x-timestamp`, `x-signature`), strictly allowlisting read-only GET
   endpoints (`/accounts/`, `/holdings/`, `/best_bid_ask/`) and refusing write scopes/methods.
3. `SchwabConnector` (Optional BYOK): Official Trader API 3-legged OAuth2 with exact fixed-port
   `https://127.0.0.1:<port>/api/v1/brokers/schwab/callback`, 30-min access token refresh,
   and 7-day refresh-token expiry countdown (`refresh_expires_at`).
4. `parse_holdings_csv`: Robinhood / Fidelity / Schwab / generic broker CSV holdings importer
   for equities & crypto alongside Plaid Investments.
"""

from __future__ import annotations

import base64
import binascii
import csv
import io
import json
import logging
import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlencode, urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, utils

from chronox_finance.connectors.base import (
    SCALE_E8,
    ConnectorAuthError,
    ConnectorError,
    HostNotAllowedError,
    NormalizedHolding,
    ReadOnlyViolationError,
    build_http_client,
    compute_market_value_cents,
    decimal_to_cents,
    decimal_to_e8,
    request_with_retry,
    to_decimal,
    utc_now_iso,
    validate_strict_external_url,
)
from chronox_finance.security import sanitize_untrusted_text

logger = logging.getLogger(__name__)

COINBASE_HOST = "api.coinbase.com"
COINBASE_BASE_URL = f"https://{COINBASE_HOST}"

ROBINHOOD_HOST = "trading.robinhood.com"
ROBINHOOD_BASE_URL = f"https://{ROBINHOOD_HOST}"
ROBINHOOD_READONLY_PATHS = (
    "/api/v1/crypto/trading/accounts/",
    "/api/v1/crypto/trading/holdings/",
    "/api/v1/crypto/marketdata/best_bid_ask/",
)
_WRITE_SCOPE_TOKENS = (
    "trade",
    "order",
    "write",
    "transfer",
    "withdraw",
    "send",
    "place",
    "cancel",
    "execute",
    "admin",
)

SCHWAB_HOST = "api.schwabapi.com"
SCHWAB_BASE_URL = f"https://{SCHWAB_HOST}"
SCHWAB_REFRESH_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days per Schwab Trader API policy

_STABLECOIN_SYMBOLS = frozenset({"USDC", "USDT", "DAI", "PYUSD", "GUSD", "TUSD", "USDP"})
_FIAT_CASH_SYMBOLS = frozenset({"USD", "USDOLLAR", "CASH", "SPAXX", "FDRXX", "VMFXX", "SWVXX"})


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _parse_iso_utc(iso_str: str) -> datetime:
    cleaned = (iso_str or "").strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    dt = datetime.fromisoformat(cleaned)
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso_utc(dt: datetime) -> str:
    utc_dt = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return utc_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_pem(raw_pem: str) -> bytes:
    """Normalize literal '\\n' escapes commonly found in downloaded CDP JSON key files."""
    cleaned = (raw_pem or "").strip().replace("\\n", "\n")
    return cleaned.encode("utf-8")


# ---------------------------------------------------------------------------
# 1. Coinbase Connector (CDP JWT + Enforced Read-Only key_permissions)
# ---------------------------------------------------------------------------


class CoinbaseConnector:
    """
    Strictly read-only Coinbase Advanced Trade (CDP) connector.

    Security invariant (#53):
    At connect time (and by default on `fetch_holdings`), calls
    `GET /api/v3/brokerage/key_permissions` and REFUSES any key with
    `can_trade=True` or `can_transfer=True` (`ReadOnlyViolationError`).
    """

    provider_name = "coinbase"

    def __init__(
        self,
        api_key_name: str,
        private_key_pem: str,
        *,
        timeout: float = 20.0,
        max_retries: int = 2,
        backoff_base: float = 0.25,
        transport: Optional[httpx.BaseTransport] = None,
        clock: Optional[Callable[[], int]] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
    ) -> None:
        cleaned_key = (api_key_name or "").strip()
        if not cleaned_key or not (private_key_pem or "").strip():
            raise ConnectorAuthError(
                "Coinbase CDP api_key_name and private_key_pem are required",
                provider=self.provider_name,
            )

        try:
            key_obj = serialization.load_pem_private_key(_normalize_pem(private_key_pem), password=None)
        except Exception as exc:
            raise ConnectorAuthError(
                f"Invalid Coinbase CDP PEM private key (expected EC P-256 or Ed25519 PEM): {exc}",
                provider=self.provider_name,
            ) from exc

        if not isinstance(key_obj, (ec.EllipticCurvePrivateKey, ed25519.Ed25519PrivateKey)):
            raise ConnectorAuthError(
                "Coinbase CDP private key must be EC (ES256) or Ed25519 (EdDSA)",
                provider=self.provider_name,
            )

        self._api_key_name = cleaned_key
        self._private_key = key_obj
        self._timeout = float(timeout)
        self._max_retries = max(0, int(max_retries))
        self._backoff_base = max(0.0, float(backoff_base))
        self._transport = transport
        self._clock = clock or (lambda: int(time.time()))
        self._sleep_fn = sleep_fn

    def __repr__(self) -> str:
        return f"CoinbaseConnector(key_prefix={self._api_key_name[:18]!r})"

    def build_cdp_jwt(
        self,
        method: str,
        path: str,
        *,
        now: Optional[int] = None,
    ) -> str:
        """
        Build a 120-second Coinbase CDP JWT (ES256 or EdDSA) scoped to a single HTTP method & path.
        Strictly blocks non-GET methods.
        """
        http_method = (method or "").strip().upper()
        if http_method != "GET":
            raise ReadOnlyViolationError(
                f"Coinbase connector is strictly read-only; method '{http_method}' is forbidden",
                provider=self.provider_name,
            )
        if not path.startswith("/"):
            raise ValueError("Coinbase API path must start with '/'")

        # Coinbase CDP JWT `uri` omits query parameters
        path_only = path.split("?", 1)[0]
        ts = int(now) if now is not None else int(self._clock())
        is_ed25519 = isinstance(self._private_key, ed25519.Ed25519PrivateKey)
        alg = "EdDSA" if is_ed25519 else "ES256"

        header = {
            "alg": alg,
            "kid": self._api_key_name,
            "nonce": secrets.token_hex(16),
            "typ": "JWT",
        }
        payload = {
            "sub": self._api_key_name,
            "iss": "cdp",
            "nbf": ts,
            "exp": ts + 120,
            "uri": f"{http_method} {COINBASE_HOST}{path_only}",
        }

        signing_input = (
            f"{_b64url_encode(json.dumps(header, separators=(',', ':')).encode('utf-8'))}."
            f"{_b64url_encode(json.dumps(payload, separators=(',', ':')).encode('utf-8'))}"
        ).encode("ascii")

        if isinstance(self._private_key, ed25519.Ed25519PrivateKey):
            raw_sig = self._private_key.sign(signing_input)
        else:
            der_sig = self._private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
            r_val, s_val = utils.decode_dss_signature(der_sig)
            coord_size = (self._private_key.curve.key_size + 7) // 8
            raw_sig = r_val.to_bytes(coord_size, "big") + s_val.to_bytes(coord_size, "big")

        return f"{signing_input.decode('ascii')}.{_b64url_encode(raw_sig)}"

    def _get(
        self,
        path: str,
        *,
        authenticated: bool = True,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"{COINBASE_BASE_URL}{path}"
        validate_strict_external_url(
            url,
            provider=self.provider_name,
            allowed_hosts={COINBASE_HOST},
            require_https=True,
        )
        headers: Dict[str, str] = {"Accept": "application/json"}
        if authenticated:
            headers["Authorization"] = f"Bearer {self.build_cdp_jwt('GET', path)}"

        retry_kwargs: Dict[str, Any] = {
            "provider": self.provider_name,
            "max_retries": self._max_retries,
            "backoff_base": self._backoff_base,
            "params": params,
        }
        if self._sleep_fn is not None:
            retry_kwargs["sleep_fn"] = self._sleep_fn

        with build_http_client(timeout=self._timeout, transport=self._transport, headers=headers) as client:
            resp = request_with_retry(client, "GET", url, **retry_kwargs)

        if resp.status_code in (401, 403):
            raise ConnectorAuthError(
                f"Coinbase authentication failed (HTTP {resp.status_code})",
                provider=self.provider_name,
                status_code=resp.status_code,
            )
        if resp.status_code >= 400:
            raise ConnectorError(
                f"Coinbase API error (HTTP {resp.status_code})",
                provider=self.provider_name,
                status_code=resp.status_code,
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise ConnectorError("Coinbase returned non-JSON response", provider=self.provider_name) from exc

        if not isinstance(data, dict):
            raise ConnectorError("Unexpected Coinbase response shape", provider=self.provider_name)
        return data

    def verify_read_only_permissions(self) -> Dict[str, Any]:
        """
        Call `GET /api/v3/brokerage/key_permissions` and strictly REFUSE any key that
        allows trading (`can_trade`) or funds movement (`can_transfer`).
        """
        data = self._get("/api/v3/brokerage/key_permissions", authenticated=True)
        can_view = bool(data.get("can_view", False))
        can_trade = bool(data.get("can_trade", False))
        can_transfer = bool(data.get("can_transfer", False))

        if can_trade or can_transfer:
            violations = []
            if can_trade:
                violations.append("can_trade=True")
            if can_transfer:
                violations.append("can_transfer=True")
            raise ReadOnlyViolationError(
                f"Refused Coinbase API key with write privileges ({', '.join(violations)}). "
                "ChronoX enforces strict read-only broker keys; create a View-Only CDP key.",
                provider=self.provider_name,
                error_code="KEY_NOT_READ_ONLY",
            )
        if not can_view:
            raise ConnectorAuthError(
                "Coinbase API key does not have 'can_view' permission",
                provider=self.provider_name,
                error_code="KEY_CANNOT_VIEW",
            )
        return {
            "can_view": True,
            "can_trade": False,
            "can_transfer": False,
            "portfolio_uuid": str(data.get("portfolio_uuid") or ""),
            "portfolio_type": str(data.get("portfolio_type") or ""),
        }

    def connect(self) -> Dict[str, Any]:
        """Verify credentials and enforce strict View-Only permissions before saving to vault."""
        perms = self.verify_read_only_permissions()
        return {
            "ok": True,
            "provider": self.provider_name,
            "read_only": True,
            "permissions": perms,
        }

    def fetch_spot_price_e8(self, symbol: str, quote_currency: str = "USD") -> Optional[int]:
        """Fetch public zero-auth spot price from `GET /v2/prices/{BASE}-{QUOTE}/spot`."""
        base = (symbol or "").strip().upper()
        quote = (quote_currency or "USD").strip().upper()
        if not base:
            return None
        if base == quote:
            return SCALE_E8
        try:
            data = self._get(f"/v2/prices/{base}-{quote}/spot", authenticated=False)
        except ConnectorError:
            return None
        amt = (data.get("data") or {}).get("amount")
        if amt is None:
            return None
        return decimal_to_e8(amt)

    def fetch_holdings(
        self,
        *,
        verify_permissions: bool = True,
        fetch_prices: bool = True,
        max_pages: int = 20,
    ) -> List[NormalizedHolding]:
        """Fetch non-zero Coinbase balances and value them in exact `price_e8` & cents."""
        if verify_permissions:
            self.verify_read_only_permissions()

        cursor: Optional[str] = None
        raw_accounts: List[Dict[str, Any]] = []
        for _ in range(max(1, int(max_pages))):
            params: Dict[str, Any] = {"limit": 250}
            if cursor:
                params["cursor"] = cursor
            data = self._get("/api/v3/brokerage/accounts", authenticated=True, params=params)
            for acct in data.get("accounts") or []:
                if isinstance(acct, dict):
                    raw_accounts.append(acct)
            if not bool(data.get("has_next")) or not data.get("cursor"):
                break
            cursor = str(data["cursor"])

        holdings: List[NormalizedHolding] = []
        for acct in raw_accounts:
            currency = str(acct.get("currency") or "").strip().upper()
            if not currency:
                continue
            avail_val = to_decimal((acct.get("available_balance") or {}).get("value"))
            hold_val = to_decimal((acct.get("hold") or {}).get("value"))
            total_qty = avail_val + hold_val
            qty_e8 = decimal_to_e8(total_qty)
            if qty_e8 <= 0:
                continue

            is_cash = currency in _FIAT_CASH_SYMBOLS
            asset_class = "cash" if is_cash else "crypto"

            if is_cash:
                price_e8 = SCALE_E8
            elif currency in _STABLECOIN_SYMBOLS:
                price_e8 = (self.fetch_spot_price_e8(currency) if fetch_prices else None) or SCALE_E8
            else:
                price_e8 = (self.fetch_spot_price_e8(currency) if fetch_prices else None) or 0

            mkt_cents = compute_market_value_cents(qty_e8, price_e8)
            holdings.append(
                NormalizedHolding(
                    symbol=currency,
                    name=sanitize_untrusted_text(str(acct.get("name") or currency), max_len=100),
                    asset_class=asset_class,
                    quantity_e8=qty_e8,
                    price_e8=price_e8,
                    cost_basis_cents=mkt_cents if is_cash else 0,
                    cost_basis_known=bool(is_cash),
                    market_value_cents=mkt_cents,
                    external_account_id=str(acct.get("uuid") or ""),
                    currency="USD",
                    source=self.provider_name,
                )
            )
        return holdings


# ---------------------------------------------------------------------------
# 2. Robinhood Crypto Connector (Official Ed25519 + Enforced Read-Only)
# ---------------------------------------------------------------------------


def _load_ed25519_private_key(raw_key: str) -> ed25519.Ed25519PrivateKey:
    text = (raw_key or "").strip()
    if not text:
        raise ConnectorAuthError("Robinhood Ed25519 private key is required", provider="robinhood_crypto")

    if "BEGIN " in text:
        key_obj = serialization.load_pem_private_key(_normalize_pem(text), password=None)
        if not isinstance(key_obj, ed25519.Ed25519PrivateKey):
            raise ConnectorAuthError("PEM key is not an Ed25519 private key", provider="robinhood_crypto")
        return key_obj

    padded = text + ("=" * ((4 - len(text) % 4) % 4))
    try:
        raw_bytes = base64.b64decode(padded.encode("ascii"), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ConnectorAuthError(
            "Robinhood private key must be Base64-encoded 32-byte Ed25519 seed or PEM",
            provider="robinhood_crypto",
        ) from exc

    if len(raw_bytes) == 64:
        raw_bytes = raw_bytes[:32]
    if len(raw_bytes) != 32:
        raise ConnectorAuthError(
            f"Expected 32-byte Ed25519 seed (got {len(raw_bytes)} bytes)",
            provider="robinhood_crypto",
        )
    return ed25519.Ed25519PrivateKey.from_private_bytes(raw_bytes)


def _assert_read_only_scopes(scopes: Sequence[str], *, provider: str) -> None:
    forbidden: List[str] = []
    for scope in scopes:
        low = (scope or "").strip().lower()
        if any(tok in low for tok in _WRITE_SCOPE_TOKENS):
            forbidden.append(scope)
    if forbidden:
        raise ReadOnlyViolationError(
            f"Refused {provider} credentials with write/trade scopes: {forbidden}. "
            "ChronoX enforces strictly read-only broker API keys.",
            provider=provider,
            error_code="SCOPE_NOT_READ_ONLY",
        )


class RobinhoodCryptoConnector:
    """
    Official Robinhood Crypto API connector (`https://trading.robinhood.com`).

    Security invariants:
    - Per-request Ed25519 signing (`x-api-key`, `x-timestamp`, `x-signature`).
    - Strictly allows ONLY GET requests on read-only endpoints:
      `/api/v1/crypto/trading/accounts/`, `/api/v1/crypto/trading/holdings/`,
      and `/api/v1/crypto/marketdata/best_bid_ask/`.
    - Refuses any write method, write endpoint, or write/trade scope (`ReadOnlyViolationError`).
    """

    provider_name = "robinhood_crypto"

    def __init__(
        self,
        api_key: str,
        private_key_b64: str,
        *,
        declared_scopes: Optional[Sequence[str]] = None,
        timeout: float = 20.0,
        max_retries: int = 2,
        backoff_base: float = 0.25,
        transport: Optional[httpx.BaseTransport] = None,
        clock: Optional[Callable[[], int]] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
    ) -> None:
        cleaned_key = (api_key or "").strip()
        if not cleaned_key:
            raise ConnectorAuthError("Robinhood Crypto api_key is required", provider=self.provider_name)
        if declared_scopes:
            _assert_read_only_scopes(declared_scopes, provider=self.provider_name)

        self._api_key = cleaned_key
        self._private_key = _load_ed25519_private_key(private_key_b64)
        self._timeout = float(timeout)
        self._max_retries = max(0, int(max_retries))
        self._backoff_base = max(0.0, float(backoff_base))
        self._transport = transport
        self._clock = clock or (lambda: int(time.time()))
        self._sleep_fn = sleep_fn

    def __repr__(self) -> str:
        return f"RobinhoodCryptoConnector(api_key_prefix={self._api_key[:10]!r})"

    def _assert_read_only_request(self, method: str, path: str) -> None:
        http_method = (method or "").strip().upper()
        if http_method != "GET":
            raise ReadOnlyViolationError(
                f"Robinhood Crypto connector is strictly read-only; HTTP '{http_method}' is forbidden",
                provider=self.provider_name,
                error_code="METHOD_NOT_READ_ONLY",
            )
        path_only = (path or "").split("?", 1)[0]
        if path_only not in ROBINHOOD_READONLY_PATHS:
            raise ReadOnlyViolationError(
                f"Endpoint '{path_only}' is not in the Robinhood Crypto read-only allowlist",
                provider=self.provider_name,
                error_code="ENDPOINT_NOT_READ_ONLY",
            )

    def sign_request(
        self,
        method: str,
        path: str,
        body: str = "",
        *,
        timestamp: Optional[int] = None,
    ) -> Dict[str, str]:
        """
        Compute official Robinhood Crypto Ed25519 headers:
        `message = f"{api_key}{timestamp}{path}{method}{body}"`
        """
        self._assert_read_only_request(method, path)
        ts = str(int(timestamp) if timestamp is not None else int(self._clock()))
        msg = f"{self._api_key}{ts}{path}{method.upper()}{body}".encode("utf-8")
        sig_b64 = base64.b64encode(self._private_key.sign(msg)).decode("ascii")
        return {
            "x-api-key": self._api_key,
            "x-timestamp": ts,
            "x-signature": sig_b64,
            "Accept": "application/json",
        }

    def _signed_get(self, path_with_query: str) -> Dict[str, Any]:
        self._assert_read_only_request("GET", path_with_query)
        url = f"{ROBINHOOD_BASE_URL}{path_with_query}"
        validate_strict_external_url(
            url,
            provider=self.provider_name,
            allowed_hosts={ROBINHOOD_HOST},
            require_https=True,
        )
        headers = self.sign_request("GET", path_with_query, "")
        retry_kwargs: Dict[str, Any] = {
            "provider": self.provider_name,
            "max_retries": self._max_retries,
            "backoff_base": self._backoff_base,
        }
        if self._sleep_fn is not None:
            retry_kwargs["sleep_fn"] = self._sleep_fn

        with build_http_client(timeout=self._timeout, transport=self._transport, headers=headers) as client:
            resp = request_with_retry(client, "GET", url, **retry_kwargs)

        if resp.status_code in (401, 403):
            raise ConnectorAuthError(
                f"Robinhood Crypto authentication failed (HTTP {resp.status_code})",
                provider=self.provider_name,
                status_code=resp.status_code,
            )
        if resp.status_code >= 400:
            raise ConnectorError(
                f"Robinhood Crypto API error (HTTP {resp.status_code})",
                provider=self.provider_name,
                status_code=resp.status_code,
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise ConnectorError("Robinhood Crypto returned non-JSON response", provider=self.provider_name) from exc

        if not isinstance(data, dict):
            raise ConnectorError("Unexpected Robinhood Crypto response shape", provider=self.provider_name)
        return data

    def get_account(self) -> Dict[str, Any]:
        """Fetch read-only account summary (`GET /api/v1/crypto/trading/accounts/`)."""
        data = self._signed_get("/api/v1/crypto/trading/accounts/")
        # Defensive check if response includes explicit scope/permission metadata
        if bool(data.get("can_trade")) or bool(data.get("can_transfer")):
            raise ReadOnlyViolationError(
                "Refused Robinhood Crypto key advertising write/trade privileges",
                provider=self.provider_name,
                error_code="KEY_NOT_READ_ONLY",
            )
        resp_scopes = data.get("scopes") or data.get("permissions")
        if isinstance(resp_scopes, list):
            _assert_read_only_scopes([str(s) for s in resp_scopes], provider=self.provider_name)
        return data

    def connect(self, *, declared_scopes: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        """Validate read-only scopes and confirm live signed authentication."""
        if declared_scopes:
            _assert_read_only_scopes(declared_scopes, provider=self.provider_name)
        acct = self.get_account()
        return {
            "ok": True,
            "provider": self.provider_name,
            "read_only": True,
            "account_number": str(acct.get("account_number") or ""),
            "status": str(acct.get("status") or "active"),
            "buying_power_cents": decimal_to_cents(acct.get("buying_power")),
        }

    def get_best_bid_ask(self, symbols: Sequence[str]) -> Dict[str, int]:
        """
        Fetch exact `price_e8` midpoints via `GET /api/v1/crypto/marketdata/best_bid_ask/`.
        Accepts base tickers (`BTC`, `SHIB`) or pairs (`BTC-USD`).
        """
        pairs: List[str] = []
        for sym in symbols:
            cleaned = (sym or "").strip().upper()
            if not cleaned or cleaned in _FIAT_CASH_SYMBOLS:
                continue
            pair = cleaned if "-" in cleaned else f"{cleaned}-USD"
            if pair not in pairs:
                pairs.append(pair)
        if not pairs:
            return {}

        qs = urlencode([("symbol", p) for p in pairs])
        data = self._signed_get(f"/api/v1/crypto/marketdata/best_bid_ask/?{qs}")
        prices: Dict[str, int] = {}
        for item in data.get("results") or []:
            if not isinstance(item, dict):
                continue
            pair_sym = str(item.get("symbol") or "").upper()
            base_sym = pair_sym.split("-", 1)[0]
            if not base_sym:
                continue
            bid = item.get("bid_inclusive_of_sell_spread")
            ask = item.get("ask_inclusive_of_buy_spread")
            direct = item.get("price")
            if direct is not None and str(direct).strip() != "":
                price_e8 = decimal_to_e8(direct)
            elif bid is not None and ask is not None:
                mid = (to_decimal(bid) + to_decimal(ask)) / Decimal(2)
                price_e8 = decimal_to_e8(mid)
            elif bid is not None:
                price_e8 = decimal_to_e8(bid)
            elif ask is not None:
                price_e8 = decimal_to_e8(ask)
            else:
                continue
            prices[base_sym] = price_e8
        return prices

    def fetch_holdings(
        self,
        *,
        fetch_prices: bool = True,
        max_pages: int = 20,
    ) -> List[NormalizedHolding]:
        """Fetch crypto holdings via `GET /api/v1/crypto/trading/holdings/` with exact `price_e8`."""
        next_path: Optional[str] = "/api/v1/crypto/trading/holdings/"
        raw_items: List[Dict[str, Any]] = []

        for _ in range(max(1, int(max_pages))):
            if not next_path:
                break
            data = self._signed_get(next_path)
            for row in data.get("results") or []:
                if isinstance(row, dict):
                    raw_items.append(row)
            nxt = data.get("next")
            if not nxt:
                break
            nxt_str = str(nxt).strip()
            if nxt_str.startswith("http"):
                validate_strict_external_url(
                    nxt_str,
                    provider=self.provider_name,
                    allowed_hosts={ROBINHOOD_HOST},
                    require_https=True,
                )
                parsed = urlparse(nxt_str)
                next_path = f"{parsed.path}?{parsed.query}" if parsed.query else parsed.path
            else:
                next_path = nxt_str

        symbols = [
            str(r.get("asset_code") or "").strip().upper()
            for r in raw_items
            if decimal_to_e8(r.get("total_quantity")) > 0
        ]
        prices = self.get_best_bid_ask(symbols) if (fetch_prices and symbols) else {}

        holdings: List[NormalizedHolding] = []
        for row in raw_items:
            sym = str(row.get("asset_code") or "").strip().upper()
            if not sym:
                continue
            qty_e8 = decimal_to_e8(row.get("total_quantity"))
            if qty_e8 <= 0:
                continue
            price_e8 = prices.get(sym, SCALE_E8 if sym in _STABLECOIN_SYMBOLS else 0)
            holdings.append(
                NormalizedHolding(
                    symbol=sym,
                    name=sym,
                    asset_class="crypto",
                    quantity_e8=qty_e8,
                    price_e8=price_e8,
                    cost_basis_cents=0,
                    cost_basis_known=False,
                    market_value_cents=compute_market_value_cents(qty_e8, price_e8),
                    external_account_id=str(row.get("account_number") or ""),
                    currency="USD",
                    source=self.provider_name,
                )
            )
        return holdings


# ---------------------------------------------------------------------------
# 3. Charles Schwab Connector (Optional BYOK OAuth2 + 7-Day Refresh Expiry)
# ---------------------------------------------------------------------------


@dataclass
class SchwabTokenBundle:
    access_token: str
    refresh_token: str
    obtained_at: str
    access_expires_at: str
    refresh_expires_at: str
    token_type: str = "Bearer"
    expires_in: int = 1800  # 30 minutes
    scope: str = "readonly"

    def refresh_seconds_remaining(self, now: Optional[datetime] = None) -> int:
        ref_now = now or datetime.now(timezone.utc)
        if ref_now.tzinfo is None:
            ref_now = ref_now.replace(tzinfo=timezone.utc)
        exp = _parse_iso_utc(self.refresh_expires_at)
        return max(0, int((exp - ref_now).total_seconds()))

    def refresh_days_remaining(self, now: Optional[datetime] = None) -> float:
        return round(self.refresh_seconds_remaining(now=now) / 86400.0, 2)

    def is_refresh_expired(self, now: Optional[datetime] = None) -> bool:
        return self.refresh_seconds_remaining(now=now) <= 0

    def needs_reauth(self, *, warn_within_hours: float = 24.0, now: Optional[datetime] = None) -> bool:
        return self.refresh_seconds_remaining(now=now) <= int(warn_within_hours * 3600)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _validate_schwab_callback_url(callback_url: str) -> str:
    """
    Schwab Trader API requires an exact registered HTTPS loopback redirect URI
    on a fixed port: `https://127.0.0.1:<fixed_port>/api/v1/brokers/schwab/callback`.
    """
    raw = (callback_url or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme != "https":
        raise HostNotAllowedError(
            "Schwab callback_url must use https:// (self-signed cert on fixed 127.0.0.1 port)",
            provider="schwab",
        )
    if (parsed.hostname or "").lower() != "127.0.0.1":
        raise HostNotAllowedError(
            "Schwab callback_url host must be 127.0.0.1",
            provider="schwab",
        )
    if parsed.port is None:
        raise HostNotAllowedError(
            "Schwab callback_url must specify a fixed port (e.g. https://127.0.0.1:8787/api/v1/brokers/schwab/callback)",
            provider="schwab",
        )
    return raw


def _map_schwab_asset_class(raw_asset_type: str) -> str:
    val = (raw_asset_type or "").strip().upper()
    if val in ("ETF",):
        return "etf"
    if val in ("MUTUAL_FUND",):
        return "mutual_fund"
    if val in ("FIXED_INCOME", "BOND"):
        return "bond"
    if val in ("CASH_EQUIVALENT", "CURRENCY"):
        return "cash"
    return "equity"


class SchwabConnector:
    """
    Optional BYOK Charles Schwab Trader API connector (`https://api.schwabapi.com`).

    Tracks 30-minute access token lifetime and mandatory 7-day refresh token expiration
    (`refresh_expires_at`) so ChronoX displays an accurate Re-Auth countdown banner.
    """

    provider_name = "schwab"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        callback_url: str = "https://127.0.0.1:8787/api/v1/brokers/schwab/callback",
        timeout: float = 20.0,
        max_retries: int = 2,
        backoff_base: float = 0.25,
        transport: Optional[httpx.BaseTransport] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
    ) -> None:
        cid = (client_id or "").strip()
        csec = (client_secret or "").strip()
        if not cid or not csec:
            raise ConnectorAuthError(
                "Schwab BYOK client_id and client_secret are required",
                provider=self.provider_name,
            )
        self._client_id = cid
        self._client_secret = csec
        self.callback_url = _validate_schwab_callback_url(callback_url)
        self._timeout = float(timeout)
        self._max_retries = max(0, int(max_retries))
        self._backoff_base = max(0.0, float(backoff_base))
        self._transport = transport
        self._sleep_fn = sleep_fn

    def __repr__(self) -> str:
        return f"SchwabConnector(callback_url={self.callback_url!r})"

    def build_authorization_url(self, state: str) -> str:
        """Build the official Schwab OAuth2 authorization URL for user login."""
        cleaned_state = (state or "").strip()
        if len(cleaned_state) < 8:
            raise ConnectorError("OAuth state parameter must be at least 8 characters", provider=self.provider_name)
        qs = urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": self.callback_url,
                "response_type": "code",
                "state": cleaned_state,
            }
        )
        return f"{SCHWAB_BASE_URL}/v1/oauth/authorize?{qs}"

    def _token_request(self, form_data: Dict[str, str]) -> Dict[str, Any]:
        url = f"{SCHWAB_BASE_URL}/v1/oauth/token"
        validate_strict_external_url(
            url,
            provider=self.provider_name,
            allowed_hosts={SCHWAB_HOST},
            require_https=True,
        )
        basic = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode("utf-8")).decode("ascii")
        headers = {
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        retry_kwargs: Dict[str, Any] = {
            "provider": self.provider_name,
            "max_retries": self._max_retries,
            "backoff_base": self._backoff_base,
            "data": form_data,
        }
        if self._sleep_fn is not None:
            retry_kwargs["sleep_fn"] = self._sleep_fn

        with build_http_client(timeout=self._timeout, transport=self._transport, headers=headers) as client:
            resp = request_with_retry(client, "POST", url, **retry_kwargs)

        if resp.status_code in (400, 401, 403):
            raise ConnectorAuthError(
                f"Schwab OAuth token request rejected (HTTP {resp.status_code}); re-authorization required",
                provider=self.provider_name,
                status_code=resp.status_code,
            )
        if resp.status_code >= 400:
            raise ConnectorError(
                f"Schwab OAuth token error (HTTP {resp.status_code})",
                provider=self.provider_name,
                status_code=resp.status_code,
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise ConnectorError("Schwab returned non-JSON token payload", provider=self.provider_name) from exc
        if not isinstance(data, dict) or not data.get("access_token"):
            raise ConnectorAuthError("Schwab token payload missing access_token", provider=self.provider_name)
        return data

    def exchange_code(
        self,
        authorization_code: str,
        *,
        now: Optional[datetime] = None,
    ) -> SchwabTokenBundle:
        """Exchange an OAuth2 authorization code for tokens and stamp 7-day `refresh_expires_at`."""
        code = (authorization_code or "").strip()
        if not code:
            raise ConnectorAuthError("Schwab authorization_code is required", provider=self.provider_name)
        data = self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.callback_url,
            }
        )
        now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        expires_in = int(data.get("expires_in") or 1800)
        return SchwabTokenBundle(
            access_token=str(data["access_token"]),
            refresh_token=str(data.get("refresh_token") or ""),
            token_type=str(data.get("token_type") or "Bearer"),
            expires_in=expires_in,
            scope=str(data.get("scope") or "readonly"),
            obtained_at=_iso_utc(now_utc),
            access_expires_at=_iso_utc(now_utc + timedelta(seconds=expires_in)),
            refresh_expires_at=_iso_utc(now_utc + timedelta(seconds=SCHWAB_REFRESH_TTL_SECONDS)),
        )

    def refresh_access_token(
        self,
        refresh_token: str,
        *,
        existing_refresh_expires_at: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> SchwabTokenBundle:
        """Refresh a 30-minute Schwab access token while preserving the 7-day refresh expiry clock."""
        rtok = (refresh_token or "").strip()
        if not rtok:
            raise ConnectorAuthError("Schwab refresh_token is required", provider=self.provider_name)

        now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if existing_refresh_expires_at and _parse_iso_utc(existing_refresh_expires_at) <= now_utc:
            raise ConnectorAuthError(
                "Schwab 7-day refresh token has expired; user must re-authenticate via OAuth2",
                provider=self.provider_name,
                error_code="REFRESH_TOKEN_EXPIRED",
            )

        data = self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": rtok,
            }
        )
        expires_in = int(data.get("expires_in") or 1800)
        # Schwab's 7-day refresh window is anchored to initial login unless a brand-new grant occurs
        refresh_exp = existing_refresh_expires_at or _iso_utc(
            now_utc + timedelta(seconds=SCHWAB_REFRESH_TTL_SECONDS)
        )
        return SchwabTokenBundle(
            access_token=str(data["access_token"]),
            refresh_token=str(data.get("refresh_token") or rtok),
            token_type=str(data.get("token_type") or "Bearer"),
            expires_in=expires_in,
            scope=str(data.get("scope") or "readonly"),
            obtained_at=_iso_utc(now_utc),
            access_expires_at=_iso_utc(now_utc + timedelta(seconds=expires_in)),
            refresh_expires_at=refresh_exp,
        )

    def _bearer_get(self, access_token: str, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        tok = (access_token or "").strip()
        if not tok:
            raise ConnectorAuthError("Schwab access_token is required", provider=self.provider_name)
        url = f"{SCHWAB_BASE_URL}{path}"
        validate_strict_external_url(
            url,
            provider=self.provider_name,
            allowed_hosts={SCHWAB_HOST},
            require_https=True,
        )
        headers = {"Authorization": f"Bearer {tok}", "Accept": "application/json"}
        retry_kwargs: Dict[str, Any] = {
            "provider": self.provider_name,
            "max_retries": self._max_retries,
            "backoff_base": self._backoff_base,
            "params": params,
        }
        if self._sleep_fn is not None:
            retry_kwargs["sleep_fn"] = self._sleep_fn

        with build_http_client(timeout=self._timeout, transport=self._transport, headers=headers) as client:
            resp = request_with_retry(client, "GET", url, **retry_kwargs)

        if resp.status_code in (401, 403):
            raise ConnectorAuthError(
                f"Schwab access token rejected or expired (HTTP {resp.status_code})",
                provider=self.provider_name,
                status_code=resp.status_code,
            )
        if resp.status_code >= 400:
            raise ConnectorError(
                f"Schwab API error (HTTP {resp.status_code})",
                provider=self.provider_name,
                status_code=resp.status_code,
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise ConnectorError("Schwab returned non-JSON payload", provider=self.provider_name) from exc

    def fetch_holdings(self, access_token: str) -> List[NormalizedHolding]:
        """Fetch read-only Schwab positions via `GET /trader/v1/accounts?fields=positions`."""
        data = self._bearer_get(access_token, "/trader/v1/accounts", params={"fields": "positions"})
        accounts = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
        holdings: List[NormalizedHolding] = []

        for entry in accounts:
            if not isinstance(entry, dict):
                continue
            sec_acct = entry.get("securitiesAccount") or entry
            acct_id = str(sec_acct.get("accountNumber") or sec_acct.get("hashValue") or "")

            for pos in sec_acct.get("positions") or []:
                if not isinstance(pos, dict):
                    continue
                inst = pos.get("instrument") or {}
                symbol = str(inst.get("symbol") or "").strip().upper()
                if not symbol:
                    continue
                long_qty = to_decimal(pos.get("longQuantity"))
                short_qty = to_decimal(pos.get("shortQuantity"))
                net_qty = long_qty - short_qty
                qty_e8 = decimal_to_e8(net_qty)
                if qty_e8 == 0:
                    continue

                mkt_val_dec = to_decimal(pos.get("marketValue"))
                avg_price_dec = to_decimal(pos.get("averagePrice"))
                if net_qty != 0 and mkt_val_dec != 0:
                    price_e8 = decimal_to_e8(abs(mkt_val_dec / net_qty))
                else:
                    price_e8 = decimal_to_e8(avg_price_dec)

                has_avg = pos.get("averagePrice") is not None and avg_price_dec > 0
                cost_basis_cents = decimal_to_cents(avg_price_dec * abs(net_qty)) if has_avg else 0
                holdings.append(
                    NormalizedHolding(
                        symbol=symbol,
                        name=sanitize_untrusted_text(str(inst.get("description") or symbol), max_len=100),
                        asset_class=_map_schwab_asset_class(str(inst.get("assetType") or "")),
                        quantity_e8=qty_e8,
                        price_e8=price_e8,
                        cost_basis_cents=cost_basis_cents,
                        cost_basis_known=bool(has_avg),
                        market_value_cents=compute_market_value_cents(qty_e8, price_e8),
                        external_account_id=acct_id,
                        currency="USD",
                        source=self.provider_name,
                    )
                )

            # Include cash balance if positive
            curr_bal = sec_acct.get("currentBalances") or {}
            cash_dec = to_decimal(curr_bal.get("cashBalance"))
            if cash_dec > 0:
                cash_e8 = decimal_to_e8(cash_dec)
                holdings.append(
                    NormalizedHolding(
                        symbol="USD",
                        name="Schwab Cash",
                        asset_class="cash",
                        quantity_e8=cash_e8,
                        price_e8=SCALE_E8,
                        cost_basis_cents=decimal_to_cents(cash_dec),
                        cost_basis_known=True,
                        market_value_cents=compute_market_value_cents(cash_e8, SCALE_E8),
                        external_account_id=acct_id,
                        currency="USD",
                        source=self.provider_name,
                    )
                )

        return holdings

    def fetch_quotes(self, access_token: str, symbols: Sequence[str]) -> Dict[str, int]:
        """Fetch live equity/ETF quotes via `GET /marketdata/v1/quotes` returning `{SYMBOL: price_e8}`."""
        cleaned = [s.strip().upper() for s in symbols if s and s.strip()]
        if not cleaned:
            return {}
        data = self._bearer_get(
            access_token,
            "/marketdata/v1/quotes",
            params={"symbols": ",".join(dict.fromkeys(cleaned))},
        )
        if not isinstance(data, dict):
            return {}
        result: Dict[str, int] = {}
        for sym, entry in data.items():
            if not isinstance(entry, dict):
                continue
            quote = entry.get("quote") or entry
            raw_price = (
                quote.get("lastPrice")
                if quote.get("lastPrice") is not None
                else quote.get("mark")
                if quote.get("mark") is not None
                else quote.get("closePrice")
            )
            if raw_price is not None:
                result[sym.strip().upper()] = decimal_to_e8(raw_price)
        return result


# ---------------------------------------------------------------------------
# 4. Robinhood / Broker CSV Holdings Importer (Equities & Crypto)
# ---------------------------------------------------------------------------


@dataclass
class HoldingsCSVResult:
    holdings: List[NormalizedHolding] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    rows_skipped: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "holdings": [h.to_dict() for h in self.holdings],
            "warnings": list(self.warnings),
            "rows_skipped": self.rows_skipped,
        }


_SYM_COLS = ("symbol", "ticker", "ticker symbol", "asset", "security", "instrument")
_NAME_COLS = ("name", "description", "security name", "asset name", "company")
_QTY_COLS = ("quantity", "shares", "qty", "units", "amount", "balance")
_PRICE_COLS = ("price", "last price", "current price", "market price", "close price", "unit price")
_AVG_COST_COLS = ("average cost", "avg cost", "average price", "avg price", "cost per share")
_TOTAL_COST_COLS = ("cost basis", "total cost", "cost basis total", "book cost")
_MKT_VAL_COLS = ("market value", "current value", "total value", "equity", "value")
_CLASS_COLS = ("asset class", "type", "security type", "category")
_ACCT_COLS = ("account number", "account name", "account", "account id")
_CRYPTO_HINTS = frozenset({"BTC", "ETH", "SOL", "DOGE", "SHIB", "AVAX", "LINK", "ADA", "XRP", "LTC", "MATIC", "UNI"})


def _pick_col(headers: Dict[str, str], candidates: Tuple[str, ...]) -> Optional[str]:
    for cand in candidates:
        if cand in headers:
            return headers[cand]
    for cand in candidates:
        for low, orig in headers.items():
            if cand in low:
                return orig
    return None


def _infer_csv_asset_class(symbol: str, raw_class: str, default_class: str) -> str:
    low = (raw_class or "").strip().lower()
    if "crypto" in low:
        return "crypto"
    if "etf" in low:
        return "etf"
    if "cash" in low or "money market" in low or symbol in _FIAT_CASH_SYMBOLS:
        return "cash"
    if "mutual" in low:
        return "mutual_fund"
    if "bond" in low or "fixed" in low:
        return "bond"
    if symbol in _CRYPTO_HINTS or symbol in _STABLECOIN_SYMBOLS:
        return "crypto"
    return default_class


def parse_holdings_csv(
    csv_text: str,
    *,
    source: str = "csv_import",
    default_asset_class: str = "equity",
) -> HoldingsCSVResult:
    """
    Parse Robinhood, Fidelity, Vanguard, Schwab, or generic broker portfolio CSV exports
    into normalized holdings with exact `quantity_e8`, `price_e8`, and `ROUND_HALF_EVEN` cents.
    """
    cleaned = (csv_text or "").lstrip("\ufeff").strip()
    if not cleaned:
        return HoldingsCSVResult(warnings=["Holdings CSV content is empty"])

    lines = [ln for ln in cleaned.splitlines() if ln.strip()]
    # Skip broker preamble lines above the real table header
    header_idx = 0
    for idx, line in enumerate(lines[:15]):
        low = line.lower()
        if any(k in low for k in ("symbol", "ticker")) and any(k in low for k in ("qty", "quantity", "shares", "value")):
            header_idx = idx
            break

    reader = csv.DictReader(io.StringIO("\n".join(lines[header_idx:])))
    if not reader.fieldnames:
        return HoldingsCSVResult(warnings=["Holdings CSV missing header row"])

    headers = {h.strip().lower(): h for h in reader.fieldnames if h}
    sym_col = _pick_col(headers, _SYM_COLS)
    name_col = _pick_col(headers, _NAME_COLS)
    qty_col = _pick_col(headers, _QTY_COLS)
    price_col = _pick_col(headers, _PRICE_COLS)
    avg_cost_col = _pick_col(headers, _AVG_COST_COLS)
    total_cost_col = _pick_col(headers, _TOTAL_COST_COLS)
    mkt_val_col = _pick_col(headers, _MKT_VAL_COLS)
    class_col = _pick_col(headers, _CLASS_COLS)
    acct_col = _pick_col(headers, _ACCT_COLS)

    if not sym_col or not qty_col:
        return HoldingsCSVResult(
            warnings=["CSV must include Symbol/Ticker and Quantity/Shares columns"]
        )

    holdings: List[NormalizedHolding] = []
    warnings: List[str] = []
    skipped = 0

    for row in reader:
        raw_sym = str(row.get(sym_col) or "").strip().upper()
        if not raw_sym or raw_sym in ("TOTAL", "ACCOUNT TOTAL", "PENDING ACTIVITY", "SUMMARY", "--"):
            skipped += 1
            continue
        # Normalize symbols like "BTC-USD" or "BRK.B"
        sym = raw_sym.split(" ", 1)[0].strip()
        if not sym or len(sym) > 24:
            skipped += 1
            continue

        try:
            qty_dec = to_decimal(row.get(qty_col))
        except ValueError:
            skipped += 1
            continue
        qty_e8 = decimal_to_e8(qty_dec)
        if qty_e8 == 0:
            skipped += 1
            continue

        asset_class = _infer_csv_asset_class(
            sym,
            str(row.get(class_col) or "") if class_col else "",
            default_asset_class,
        )

        price_e8 = 0
        if price_col and row.get(price_col):
            try:
                price_e8 = decimal_to_e8(row.get(price_col))
            except ValueError:
                price_e8 = 0

        if price_e8 == 0 and mkt_val_col and row.get(mkt_val_col):
            try:
                mkt_dec = to_decimal(row.get(mkt_val_col))
                if qty_dec != 0:
                    price_e8 = decimal_to_e8(abs(mkt_dec / qty_dec))
            except ValueError:
                price_e8 = 0

        if price_e8 == 0 and asset_class == "cash":
            price_e8 = SCALE_E8

        mkt_val_cents = compute_market_value_cents(qty_e8, price_e8)
        cost_basis_cents = 0
        cost_basis_known = False
        raw_total_cost = str(row.get(total_cost_col) or "").strip() if total_cost_col else ""
        raw_avg_cost = str(row.get(avg_cost_col) or "").strip() if avg_cost_col else ""
        if raw_total_cost and raw_total_cost.lower() not in ("--", "n/a", "na", "unknown"):
            try:
                cost_basis_cents = abs(decimal_to_cents(raw_total_cost))
                cost_basis_known = True
            except ValueError:
                cost_basis_cents = 0
                cost_basis_known = False
        elif raw_avg_cost and raw_avg_cost.lower() not in ("--", "n/a", "na", "unknown"):
            try:
                avg_dec = to_decimal(raw_avg_cost)
                cost_basis_cents = abs(decimal_to_cents(avg_dec * abs(qty_dec)))
                cost_basis_known = True
            except ValueError:
                cost_basis_cents = 0
                cost_basis_known = False
        elif asset_class == "cash":
            cost_basis_cents = mkt_val_cents
            cost_basis_known = True

        ext_acct = sanitize_untrusted_text(str(row.get(acct_col) or ""), max_len=60) if acct_col else ""
        name = sanitize_untrusted_text(
            str(row.get(name_col) or sym) if name_col else sym,
            max_len=100,
        )
        holdings.append(
            NormalizedHolding(
                symbol=sym,
                name=name or sym,
                asset_class=asset_class,
                quantity_e8=qty_e8,
                price_e8=price_e8,
                cost_basis_cents=cost_basis_cents,
                cost_basis_known=cost_basis_known,
                market_value_cents=mkt_val_cents,
                external_account_id=ext_acct,
                currency="USD",
                source=source,
                updated_at=utc_now_iso(),
            )
        )

    if not holdings:
        warnings.append("No valid holding rows were extracted from CSV.")

    return HoldingsCSVResult(holdings=holdings, warnings=warnings, rows_skipped=skipped)
