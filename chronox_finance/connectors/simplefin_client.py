"""
SimpleFIN Bridge Connector for ChronoX Finance.

Protocol flow:
1. User pastes a Base64-encoded SimpleFIN Setup Token.
2. `decode_setup_token()` decodes and SSRF-validates the claim URL (HTTPS + public host only).
3. `claim_setup_token()` issues `POST <claim_url>` with `Content-Length: 0` and empty body,
   receiving an `access_url` (stored encrypted in `secrets.enc` by `FinanceService`).
4. `fetch_accounts()` splits up to 730 days into 90-day windows and issues
   `GET <access_url>/accounts?start-date=<ts>&end-date=<ts>&version=2` with credentials
   passed via HTTP Basic auth headers (never logged in URLs).
"""

from __future__ import annotations

import base64
import binascii
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse, urlunparse

import httpx

from chronox_finance.connectors.base import (
    ConnectorAuthError,
    ConnectorError,
    HostNotAllowedError,
    NormalizedAccount,
    NormalizedSyncResult,
    NormalizedTransaction,
    build_http_client,
    decimal_to_cents,
    request_with_retry,
    to_decimal,
    validate_strict_external_url,
)
from chronox_finance.security import sanitize_untrusted_text

logger = logging.getLogger(__name__)

MAX_LOOKBACK_DAYS = 730
DEFAULT_CHUNK_DAYS = 90


def _infer_account_type(name: str, org_name: str, balance_cents: int) -> Tuple[str, bool]:
    text = f"{name} {org_name}".lower()
    if any(k in text for k in ("credit", "visa", "mastercard", "amex", "discover", "card")):
        return "credit", False
    if any(k in text for k in ("401k", "ira", "roth", "brokerage", "invest", "trading", "stock")):
        return "trading", False
    if any(k in text for k in ("mortgage", "loan", "auto loan", "student")):
        return "loan", False
    if any(k in text for k in ("saving", "money market", "cd ", "share savings", "hysa")):
        return "savings", True
    if balance_cents < 0 and "checking" not in text:
        return "credit", False
    return "checking", True


def _unix_ts_to_iso_date(ts: Any) -> str:
    try:
        val = int(ts)
        if val <= 0:
            return datetime.now(timezone.utc).date().isoformat()
        return datetime.fromtimestamp(val, tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return datetime.now(timezone.utc).date().isoformat()


def _split_access_url(access_url: str) -> Tuple[str, Optional[Tuple[str, str]]]:
    """
    Validate a SimpleFIN `access_url` and separate embedded Basic Auth credentials
    from the clean HTTPS base URL.
    """
    validated = validate_strict_external_url(
        access_url,
        provider="simplefin",
        require_https=True,
        allow_credentials=True,
    )
    parsed = urlparse(validated)
    auth: Optional[Tuple[str, str]] = None
    if parsed.username is not None or parsed.password is not None:
        auth = (unquote(parsed.username or ""), unquote(parsed.password or ""))

    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    clean_path = (parsed.path or "").rstrip("/")
    clean_base = urlunparse((parsed.scheme, netloc, clean_path, "", "", ""))
    return clean_base, auth


class SimpleFINClient:
    """Official SimpleFIN Bridge protocol client with 730-day 90d auto-chunking."""

    provider_name = "simplefin"

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        max_retries: int = 2,
        backoff_base: float = 0.25,
        transport: Optional[httpx.BaseTransport] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
    ) -> None:
        self._timeout = float(timeout)
        self._max_retries = max(0, int(max_retries))
        self._backoff_base = max(0.0, float(backoff_base))
        self._transport = transport
        self._sleep_fn = sleep_fn

    def decode_setup_token(self, setup_token: str) -> str:
        """
        Decode a Base64 SimpleFIN Setup Token into an SSRF-validated HTTPS claim URL.
        """
        raw = "".join((setup_token or "").split())
        if not raw:
            raise ConnectorError("SimpleFIN setup token is empty", provider=self.provider_name)

        # Pad Base64 cleanly if trailing '=' were trimmed during copy/paste
        padded = raw + ("=" * ((4 - len(raw) % 4) % 4))
        try:
            decoded_bytes = base64.b64decode(padded.encode("ascii"), validate=True)
            claim_url = decoded_bytes.decode("utf-8").strip()
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            raise ConnectorError(
                "Invalid SimpleFIN Base64 setup token",
                provider=self.provider_name,
            ) from exc

        return validate_strict_external_url(
            claim_url,
            provider=self.provider_name,
            require_https=True,
            allow_credentials=False,
        )

    def claim_setup_token(self, setup_token: str) -> str:
        """
        Claim a SimpleFIN Setup Token by sending `POST <claim_url>` with `Content-Length: 0`.
        Returns the SSRF-validated SimpleFIN `access_url` (store only in `secrets.enc`).
        """
        claim_url = self.decode_setup_token(setup_token)
        retry_kwargs: Dict[str, Any] = {
            "provider": self.provider_name,
            "max_retries": self._max_retries,
            "backoff_base": self._backoff_base,
            "content": b"",
            "headers": {"Content-Length": "0"},
        }
        if self._sleep_fn is not None:
            retry_kwargs["sleep_fn"] = self._sleep_fn

        with build_http_client(timeout=self._timeout, transport=self._transport) as client:
            resp = request_with_retry(client, "POST", claim_url, **retry_kwargs)

        if resp.status_code in (401, 403):
            raise ConnectorAuthError(
                "SimpleFIN setup token was already claimed, expired, or unauthorized",
                provider=self.provider_name,
                status_code=resp.status_code,
            )
        if resp.status_code >= 400:
            raise ConnectorError(
                f"SimpleFIN claim failed (HTTP {resp.status_code})",
                provider=self.provider_name,
                status_code=resp.status_code,
            )

        access_url = resp.text.strip()
        if not access_url:
            raise ConnectorError("SimpleFIN claim returned an empty access URL", provider=self.provider_name)

        # Validate returned access_url before handing it back to vault storage
        try:
            _split_access_url(access_url)
        except HostNotAllowedError:
            raise
        except Exception as exc:
            raise ConnectorError("SimpleFIN returned an invalid access URL", provider=self.provider_name) from exc
        return access_url

    def _build_windows(
        self,
        *,
        days: int,
        chunk_days: int,
        end_time: datetime,
    ) -> List[Tuple[int, int]]:
        clamped_days = max(1, min(int(days), MAX_LOOKBACK_DAYS))
        step = max(1, min(int(chunk_days), 90))
        end_utc = end_time.astimezone(timezone.utc) if end_time.tzinfo else end_time.replace(tzinfo=timezone.utc)
        start_utc = end_utc - timedelta(days=clamped_days)

        windows: List[Tuple[int, int]] = []
        cursor = start_utc
        while cursor < end_utc:
            nxt = min(cursor + timedelta(days=step), end_utc)
            windows.append((int(cursor.timestamp()), int(nxt.timestamp())))
            cursor = nxt
        return windows

    def fetch_accounts(
        self,
        access_url: str,
        *,
        days: int = MAX_LOOKBACK_DAYS,
        chunk_days: int = DEFAULT_CHUNK_DAYS,
        end_time: Optional[datetime] = None,
        balances_only: bool = False,
    ) -> NormalizedSyncResult:
        """
        Fetch accounts and transactions from SimpleFIN Bridge (`version=2`), automatically
        chunking up to 730 days into 90-day windows and deduplicating across boundaries.
        """
        clean_base, auth = _split_access_url(access_url)
        endpoint = f"{clean_base}/accounts"
        now_utc = end_time or datetime.now(timezone.utc)

        windows: List[Optional[Tuple[int, int]]]
        if balances_only:
            windows = [None]
        else:
            windows = list(self._build_windows(days=days, chunk_days=chunk_days, end_time=now_utc))

        accounts_by_id: Dict[str, NormalizedAccount] = {}
        tx_by_key: Dict[Tuple[str, str], NormalizedTransaction] = {}
        warnings: List[str] = []
        seen_warnings: set[str] = set()

        with build_http_client(
            timeout=self._timeout,
            transport=self._transport,
            headers={"Accept": "application/json"},
        ) as client:
            for window in windows:
                params: Dict[str, Any] = {"version": "2"}
                if window is None:
                    params["balances-only"] = "1"
                else:
                    params["start-date"] = str(window[0])
                    params["end-date"] = str(window[1])

                retry_kwargs: Dict[str, Any] = {
                    "provider": self.provider_name,
                    "max_retries": self._max_retries,
                    "backoff_base": self._backoff_base,
                    "params": params,
                }
                if auth is not None:
                    retry_kwargs["auth"] = auth
                if self._sleep_fn is not None:
                    retry_kwargs["sleep_fn"] = self._sleep_fn

                resp = request_with_retry(client, "GET", endpoint, **retry_kwargs)
                if resp.status_code in (401, 403):
                    raise ConnectorAuthError(
                        "SimpleFIN access credentials rejected or revoked",
                        provider=self.provider_name,
                        status_code=resp.status_code,
                    )
                if resp.status_code >= 400:
                    raise ConnectorError(
                        f"SimpleFIN accounts request failed (HTTP {resp.status_code})",
                        provider=self.provider_name,
                        status_code=resp.status_code,
                    )

                try:
                    data = resp.json()
                except ValueError as exc:
                    raise ConnectorError(
                        "SimpleFIN returned non-JSON response",
                        provider=self.provider_name,
                        status_code=resp.status_code,
                    ) from exc

                if not isinstance(data, dict):
                    raise ConnectorError("Unexpected SimpleFIN response payload", provider=self.provider_name)

                for err_item in data.get("errors") or []:
                    msg = sanitize_untrusted_text(str(err_item), max_len=200)
                    if msg and msg not in seen_warnings:
                        seen_warnings.add(msg)
                        warnings.append(msg)

                for raw_acct in data.get("accounts") or []:
                    if not isinstance(raw_acct, dict):
                        continue
                    acct_id = str(raw_acct.get("id") or "").strip()
                    if not acct_id:
                        continue

                    org = raw_acct.get("org") or {}
                    org_name = sanitize_untrusted_text(
                        str(org.get("name") or org.get("domain") or ""), max_len=80
                    )
                    acct_name = sanitize_untrusted_text(str(raw_acct.get("name") or "Account"), max_len=80)
                    bal_cents = decimal_to_cents(raw_acct.get("balance"))
                    avail_raw = raw_acct.get("available-balance")
                    avail_cents = decimal_to_cents(avail_raw) if avail_raw not in (None, "") else None
                    acct_type, is_liquid = _infer_account_type(acct_name, org_name, bal_cents)
                    currency = str(raw_acct.get("currency") or "USD").upper()

                    accounts_by_id[acct_id] = NormalizedAccount(
                        external_account_id=acct_id,
                        name=acct_name,
                        account_type=acct_type,
                        balance_cents=bal_cents,
                        available_cents=avail_cents,
                        institution=org_name,
                        currency=currency,
                        is_liquid=is_liquid,
                        provider=self.provider_name,
                    )

                    for raw_tx in raw_acct.get("transactions") or []:
                        if not isinstance(raw_tx, dict):
                            continue
                        tx_id = str(raw_tx.get("id") or "").strip()
                        amt_dec = to_decimal(raw_tx.get("amount"))
                        amt_cents = abs(decimal_to_cents(amt_dec))
                        if amt_cents == 0:
                            continue

                        # SimpleFIN sign convention: negative = outflow/expense, positive = inflow/income
                        tx_type = "income" if amt_dec > Decimal(0) else "expense"
                        posted_ts = raw_tx.get("posted")
                        transacted_ts = raw_tx.get("transacted_at")
                        tx_date = _unix_ts_to_iso_date(posted_ts or transacted_ts)
                        pending = bool(raw_tx.get("pending", False)) or (
                            posted_ts is not None and str(posted_ts) == "0"
                        )

                        payee = str(raw_tx.get("payee") or raw_tx.get("description") or "Unknown")
                        desc = str(raw_tx.get("description") or "")
                        memo_raw = str(raw_tx.get("memo") or (desc if desc != payee else ""))
                        merchant = sanitize_untrusted_text(payee, max_len=120) or "Unknown"
                        memo = sanitize_untrusted_text(memo_raw, max_len=240)

                        dedup_id = tx_id or f"{tx_date}:{amt_cents}:{tx_type}:{merchant[:40]}"
                        key = (acct_id, dedup_id)
                        tx_by_key[key] = NormalizedTransaction(
                            external_id=dedup_id,
                            external_account_id=acct_id,
                            tx_date=tx_date,
                            merchant=merchant,
                            amount_cents=amt_cents,
                            tx_type=tx_type,
                            memo=memo,
                            pending=pending,
                            currency=currency,
                            source=self.provider_name,
                        )

        ordered_txs = sorted(tx_by_key.values(), key=lambda t: (t.tx_date, t.external_id))
        return NormalizedSyncResult(
            provider=self.provider_name,
            accounts=list(accounts_by_id.values()),
            added=ordered_txs,
            modified=[],
            removed_external_ids=[],
            next_cursor=None,
            has_more=False,
            warnings=warnings,
        )

    def sync(
        self,
        access_url: str,
        *,
        days: int = MAX_LOOKBACK_DAYS,
        chunk_days: int = DEFAULT_CHUNK_DAYS,
        end_time: Optional[datetime] = None,
    ) -> NormalizedSyncResult:
        """Alias satisfying `ConnectorProtocol`."""
        return self.fetch_accounts(
            access_url,
            days=days,
            chunk_days=chunk_days,
            end_time=end_time,
        )
