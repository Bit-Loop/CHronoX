"""
First-Class Reliable BYOK Plaid Connector for ChronoX Finance.

Security & reliability invariants:
- User brings their own `client_id` and `secret` (BYOK); zero secrets are ever hardcoded.
- Strict Plaid host allowlist: ONLY `sandbox.plaid.com` and `production.plaid.com`.
- Uses Plaid Hosted Link (`hosted_link: {}`) opened in the user's system browser +
  `POST /link/token/get` polling — zero local HTTP redirect URI breakage for Chase/BofA OAuth.
- Uses `POST /transactions/sync` ONLY (cursor-based incremental sync, paginated until
  `has_more == False`; no `/transactions/get` fallback).
- Always requests `transactions: {"days_requested": 730}` at Link time (note: Plaid applies
  `days_requested` ONLY when creating a new Link Item; existing Items require re-Link).
- Exact integer cents, `quantity_e8`, `price_e8`, and `ROUND_HALF_EVEN` market value math.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Sequence
from urllib.parse import urlparse

import httpx

from chronox_finance.connectors.base import (
    ConnectorAuthError,
    ConnectorError,
    HostNotAllowedError,
    NormalizedAccount,
    NormalizedHolding,
    NormalizedLiability,
    NormalizedSyncResult,
    NormalizedTransaction,
    apr_pct_to_bps,
    build_http_client,
    compute_market_value_cents,
    decimal_to_cents,
    decimal_to_e8,
    request_with_retry,
    to_decimal,
    validate_strict_external_url,
)
from chronox_finance.security import sanitize_untrusted_text

logger = logging.getLogger(__name__)

PLAID_ALLOWED_HOSTS = frozenset({"sandbox.plaid.com", "production.plaid.com"})
PLAID_ENV_URLS: Dict[str, str] = {
    "sandbox": "https://sandbox.plaid.com",
    "production": "https://production.plaid.com",
}

DEFAULT_DAYS_REQUESTED = 730
PLAID_HISTORY_NOTE = (
    "Plaid applies transactions.days_requested=730 only when a bank Item is linked. "
    "Existing Items connected with a shorter history window must be re-Linked to fetch 2 years."
)

_AUTH_ERROR_CODES = frozenset(
    {
        "ITEM_LOGIN_REQUIRED",
        "INVALID_ACCESS_TOKEN",
        "INVALID_API_KEYS",
        "ACCESS_NOT_GRANTED",
        "INVALID_PUBLIC_TOKEN",
        "PUBLIC_TOKEN_EXPIRED",
        "LINK_TOKEN_EXPIRED",
    }
)


@dataclass
class PlaidLinkTokenResult:
    link_token: str
    hosted_link_url: str = ""
    expiration: str = ""
    request_id: str = ""
    days_requested: int = DEFAULT_DAYS_REQUESTED
    history_note: str = PLAID_HISTORY_NOTE

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PlaidLinkPollResult:
    status: str  # "pending" | "completed" | "exited"
    link_token: str
    public_token: Optional[str] = None
    institution_id: str = ""
    institution_name: str = ""
    accounts: List[Dict[str, Any]] = field(default_factory=list)
    error_message: str = ""
    request_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PlaidTokenExchangeResult:
    access_token: str
    item_id: str
    request_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _map_plaid_account_type(plaid_type: str, plaid_subtype: str) -> tuple[str, bool]:
    """Map Plaid account type/subtype -> (ChronoX account_type, is_liquid)."""
    t = (plaid_type or "").strip().lower()
    st = (plaid_subtype or "").strip().lower()
    if t == "depository":
        if st in ("savings", "money market", "cd", "hsa", "cash management"):
            return "savings", True
        return "checking", True
    if t == "credit":
        return "credit", False
    if t in ("investment", "brokerage"):
        return "trading", False
    if t == "loan":
        return "loan", False
    return "checking", True


def _map_plaid_security_class(sec_type: str, sec_subtype: str = "") -> str:
    st = (sec_type or "").strip().lower()
    sub = (sec_subtype or "").strip().lower()
    if "crypto" in st or "crypto" in sub:
        return "crypto"
    if st == "etf" or sub == "etf":
        return "etf"
    if st in ("cash", "cash equivalent"):
        return "cash"
    if st in ("mutual fund", "mutual_fund"):
        return "mutual_fund"
    if st in ("fixed income", "bond"):
        return "bond"
    return "equity"


def _normalize_plaid_account(raw: Dict[str, Any], institution: str = "") -> NormalizedAccount:
    balances = raw.get("balances") or {}
    raw_type = str(raw.get("type") or "").strip().lower()
    acct_type, is_liquid = _map_plaid_account_type(
        raw_type,
        str(raw.get("subtype") or ""),
    )
    current_val = balances.get("current")
    avail_val = balances.get("available")
    limit_val = balances.get("limit")
    currency = (
        balances.get("iso_currency_code")
        or balances.get("unofficial_currency_code")
        or "USD"
    )
    raw_bal_cents = decimal_to_cents(current_val)
    # Plaid reports debt owed on credit/loan accounts as a positive `current` balance,
    # whereas ChronoX represents debt on `credit` accounts as negative `balance_cents`.
    norm_bal_cents = -raw_bal_cents if (acct_type == "credit" or raw_type in ("credit", "loan")) else raw_bal_cents
    name = sanitize_untrusted_text(str(raw.get("name") or raw.get("official_name") or "Account"), max_len=80)
    official_name = sanitize_untrusted_text(str(raw.get("official_name") or ""), max_len=120)
    return NormalizedAccount(
        external_account_id=str(raw.get("account_id") or ""),
        name=name or "Account",
        official_name=official_name,
        mask=str(raw.get("mask") or "")[:8],
        account_type=acct_type,
        subtype=str(raw.get("subtype") or "").lower(),
        institution=sanitize_untrusted_text(institution, max_len=80),
        balance_cents=norm_bal_cents,
        available_cents=decimal_to_cents(avail_val) if avail_val is not None else None,
        limit_cents=decimal_to_cents(limit_val) if limit_val is not None else None,
        currency=str(currency).upper(),
        is_liquid=is_liquid,
        provider="plaid",
    )


def _normalize_plaid_transaction(raw: Dict[str, Any]) -> NormalizedTransaction:
    """
    Normalize a Plaid transaction from `/transactions/sync`.

    Plaid sign convention:
    - Positive `amount` (> 0): money leaving the account (expense / outflow).
    - Negative `amount` (< 0): money entering the account (income / deposit / refund).
    """
    amt_dec = to_decimal(raw.get("amount"))
    amount_cents = abs(decimal_to_cents(amt_dec))

    pfc = raw.get("personal_finance_category") or {}
    primary_cat = str(pfc.get("primary") or "").strip().upper()
    detailed_cat = str(pfc.get("detailed") or "").strip().upper()

    xfer_dir = ""
    if primary_cat in ("TRANSFER_IN", "TRANSFER_OUT") or detailed_cat == "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT":
        tx_type = "transfer"
        xfer_dir = "in" if (primary_cat == "TRANSFER_IN" or amt_dec < Decimal(0)) else "out"
    elif amt_dec < Decimal(0):
        tx_type = "income"
    else:
        tx_type = "expense"

    raw_merchant = raw.get("merchant_name") or raw.get("name") or raw.get("original_description") or "Unknown"
    merchant = sanitize_untrusted_text(str(raw_merchant), max_len=120) or "Unknown"
    raw_memo = raw.get("name") if raw.get("merchant_name") and raw.get("name") != raw.get("merchant_name") else ""
    memo = sanitize_untrusted_text(str(raw_memo or ""), max_len=240)

    subcategory = ""
    if detailed_cat:
        subcategory = detailed_cat.lower().replace("_", " ")[:60]
    elif primary_cat:
        subcategory = primary_cat.lower().replace("_", " ")[:60]

    tx_date = str(raw.get("date") or raw.get("authorized_date") or "")[:10]
    currency = (
        raw.get("iso_currency_code")
        or raw.get("unofficial_currency_code")
        or "USD"
    )
    return NormalizedTransaction(
        external_id=str(raw.get("transaction_id") or ""),
        external_account_id=str(raw.get("account_id") or ""),
        tx_date=tx_date,
        merchant=merchant,
        amount_cents=amount_cents,
        tx_type=tx_type,
        transfer_direction=xfer_dir,
        memo=memo,
        subcategory=subcategory,
        pending=bool(raw.get("pending", False)),
        currency=str(currency).upper(),
        source="plaid",
        raw_category=detailed_cat or primary_cat,
    )


class PlaidClient:
    """
    BYOK Plaid REST API client.

    Credentials (`client_id`, `secret`) are supplied per-instance from the user's encrypted
    local vault — never hardcoded or shipped with the application.
    """

    provider_name = "plaid"

    def __init__(
        self,
        client_id: str,
        secret: str,
        *,
        env: str = "production",
        base_url: Optional[str] = None,
        timeout: float = 25.0,
        max_retries: int = 2,
        backoff_base: float = 0.25,
        transport: Optional[httpx.BaseTransport] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
    ) -> None:
        cleaned_id = (client_id or "").strip()
        cleaned_secret = (secret or "").strip()
        if not cleaned_id or not cleaned_secret:
            raise ConnectorAuthError(
                "Plaid client_id and secret are required (BYOK vault credentials)",
                provider=self.provider_name,
            )

        normalized_env = (env or "production").strip().lower()
        if base_url is not None:
            resolved_url = validate_strict_external_url(
                base_url,
                provider=self.provider_name,
                allowed_hosts=PLAID_ALLOWED_HOSTS,
                require_https=True,
            )
        else:
            if normalized_env not in PLAID_ENV_URLS:
                raise HostNotAllowedError(
                    f"Unsupported Plaid environment '{env}'. Allowed environments: sandbox, production.",
                    provider=self.provider_name,
                )
            resolved_url = validate_strict_external_url(
                PLAID_ENV_URLS[normalized_env],
                provider=self.provider_name,
                allowed_hosts=PLAID_ALLOWED_HOSTS,
                require_https=True,
            )

        self._client_id = cleaned_id
        self._secret = cleaned_secret
        self.env = normalized_env if normalized_env in PLAID_ENV_URLS else (
            "production" if urlparse(resolved_url).hostname == "production.plaid.com" else "sandbox"
        )
        self.base_url = resolved_url
        self._timeout = float(timeout)
        self._max_retries = max(0, int(max_retries))
        self._backoff_base = max(0.0, float(backoff_base))
        self._transport = transport
        self._sleep_fn = sleep_fn

    def __repr__(self) -> str:
        return f"PlaidClient(env={self.env!r}, base_url={self.base_url!r})"

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not path.startswith("/"):
            raise ValueError("Plaid endpoint path must start with '/'")
        url = f"{self.base_url}{path}"
        # Re-validate full URL host allowlist before every request
        validate_strict_external_url(
            url,
            provider=self.provider_name,
            allowed_hosts=PLAID_ALLOWED_HOSTS,
            require_https=True,
        )
        body = {
            "client_id": self._client_id,
            "secret": self._secret,
            **payload,
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Plaid-Version": "2020-09-14",
        }
        retry_kwargs: Dict[str, Any] = {
            "provider": self.provider_name,
            "max_retries": self._max_retries,
            "backoff_base": self._backoff_base,
            "json": body,
        }
        if self._sleep_fn is not None:
            retry_kwargs["sleep_fn"] = self._sleep_fn

        with build_http_client(timeout=self._timeout, transport=self._transport, headers=headers) as client:
            resp = request_with_retry(client, "POST", url, **retry_kwargs)

        try:
            data = resp.json()
        except ValueError as exc:
            raise ConnectorError(
                f"Plaid returned non-JSON response (HTTP {resp.status_code})",
                provider=self.provider_name,
                status_code=resp.status_code,
            ) from exc

        if resp.status_code >= 400 or (isinstance(data, dict) and data.get("error_code")):
            err_code = str(data.get("error_code") or "") if isinstance(data, dict) else ""
            err_msg = (
                str(data.get("display_message") or data.get("error_message") or f"HTTP {resp.status_code}")
                if isinstance(data, dict)
                else f"HTTP {resp.status_code}"
            )
            exc_cls = (
                ConnectorAuthError
                if (resp.status_code in (401, 403) or err_code in _AUTH_ERROR_CODES)
                else ConnectorError
            )
            raise exc_cls(
                f"Plaid API error ({err_code or resp.status_code}): {err_msg}",
                provider=self.provider_name,
                status_code=resp.status_code,
                error_code=err_code or None,
            )

        if not isinstance(data, dict):
            raise ConnectorError("Unexpected Plaid response format", provider=self.provider_name)
        return data

    def create_link_token(
        self,
        *,
        user_client_id: str = "chronox-local-user",
        client_name: str = "ChronoX Finance",
        country_codes: Sequence[str] = ("US",),
        language: str = "en",
        days_requested: int = DEFAULT_DAYS_REQUESTED,
        products: Sequence[str] = ("transactions",),
        optional_products: Sequence[str] = ("liabilities", "investments"),
        access_token: Optional[str] = None,
        hosted_link: Optional[Dict[str, Any]] = None,
    ) -> PlaidLinkTokenResult:
        """
        Create a Plaid Hosted Link token for authentication in the user's system browser.

        Always configures `transactions: {"days_requested": 730}` (clamped 1..730),
        `products: ["transactions"]`, `optional_products: ["liabilities", "investments"]`,
        and `hosted_link: {}` so OAuth institutions (Chase, BofA, etc.) complete cleanly
        without requiring a local HTTPS redirect_uri.

        Note: `days_requested` applies at Link creation time; existing Items need re-Link
        to expand their historical window to 730 days.
        """
        clamped_days = max(1, min(int(days_requested), 730))
        payload: Dict[str, Any] = {
            "user": {"client_user_id": (user_client_id or "chronox-local-user")[:128]},
            "client_name": (client_name or "ChronoX Finance")[:30],
            "country_codes": [str(c).upper() for c in country_codes] or ["US"],
            "language": (language or "en").lower(),
            "transactions": {"days_requested": clamped_days},
            "hosted_link": {} if hosted_link is None else dict(hosted_link),
        }
        if access_token:
            payload["access_token"] = access_token
        else:
            payload["products"] = list(products) if products else ["transactions"]
            if optional_products:
                payload["optional_products"] = list(optional_products)

        data = self._post("/link/token/create", payload)
        link_token = str(data.get("link_token") or "")
        if not link_token:
            raise ConnectorError("Plaid did not return a link_token", provider=self.provider_name)
        return PlaidLinkTokenResult(
            link_token=link_token,
            hosted_link_url=str(data.get("hosted_link_url") or ""),
            expiration=str(data.get("expiration") or ""),
            request_id=str(data.get("request_id") or ""),
            days_requested=clamped_days,
            history_note=PLAID_HISTORY_NOTE,
        )

    def poll_link_token(self, link_token: str) -> PlaidLinkPollResult:
        """
        Poll `POST /link/token/get` to retrieve `public_token` after Hosted Link completes
        in the user's system browser.
        """
        cleaned_token = (link_token or "").strip()
        if not cleaned_token:
            raise ConnectorError("link_token is required", provider=self.provider_name)

        data = self._post("/link/token/get", {"link_token": cleaned_token})
        sessions = data.get("link_sessions") or []
        req_id = str(data.get("request_id") or "")

        # Scan sessions from most recent to oldest for a completed item_add_result
        for session in reversed(sessions):
            if not isinstance(session, dict):
                continue
            results = session.get("results") or {}
            item_results = results.get("item_add_results") or []
            for item_res in item_results:
                if not isinstance(item_res, dict):
                    continue
                pub = str(item_res.get("public_token") or "").strip()
                if pub:
                    inst = item_res.get("institution") or {}
                    return PlaidLinkPollResult(
                        status="completed",
                        link_token=cleaned_token,
                        public_token=pub,
                        institution_id=str(inst.get("institution_id") or ""),
                        institution_name=sanitize_untrusted_text(str(inst.get("name") or ""), max_len=80),
                        accounts=list(item_res.get("accounts") or []),
                        request_id=req_id,
                    )

            exit_info = session.get("exit") or results.get("exit")
            if exit_info and isinstance(exit_info, dict):
                err = exit_info.get("error") or {}
                err_msg = str(err.get("display_message") or err.get("error_message") or "Link exited")
                return PlaidLinkPollResult(
                    status="exited",
                    link_token=cleaned_token,
                    error_message=err_msg,
                    request_id=req_id,
                )

        return PlaidLinkPollResult(
            status="pending",
            link_token=cleaned_token,
            request_id=req_id,
        )

    def exchange_public_token(self, public_token: str) -> PlaidTokenExchangeResult:
        """Exchange a Hosted Link `public_token` for a durable `{access_token, item_id}`."""
        cleaned = (public_token or "").strip()
        if not cleaned:
            raise ConnectorAuthError("public_token is required", provider=self.provider_name)

        data = self._post("/item/public_token/exchange", {"public_token": cleaned})
        access_token = str(data.get("access_token") or "")
        item_id = str(data.get("item_id") or "")
        if not access_token or not item_id:
            raise ConnectorError(
                "Plaid token exchange response missing access_token or item_id",
                provider=self.provider_name,
            )
        return PlaidTokenExchangeResult(
            access_token=access_token,
            item_id=item_id,
            request_id=str(data.get("request_id") or ""),
        )

    def remove_item(self, access_token: str) -> Dict[str, Any]:
        """
        Revoke an active Plaid Item via `POST /item/remove` so recurring Plaid
        product subscriptions stop billing before local credentials are deleted.
        """
        cleaned_token = (access_token or "").strip()
        if not cleaned_token:
            raise ConnectorAuthError("access_token is required", provider=self.provider_name)
        try:
            data = self._post("/item/remove", {"access_token": cleaned_token})
            return {"removed": True, "request_id": str(data.get("request_id") or "")}
        except ConnectorError as exc:
            if exc.error_code in ("ITEM_NOT_FOUND", "INVALID_ACCESS_TOKEN"):
                return {"removed": True, "already_revoked": True}
            raise

    def sync_transactions(
        self,
        access_token: str,
        cursor: Optional[str] = None,
        *,
        count: int = 500,
        max_pages: int = 50,
        institution: str = "",
    ) -> NormalizedSyncResult:
        """
        Synchronize transactions using `POST /transactions/sync` ONLY.

        Paginates until `has_more == False` (or `max_pages` safety cap), handling Plaid's
        `TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION` by restarting from the initial cursor.
        Returns normalized `added`, `modified`, `removed_external_ids`, `accounts`, and `next_cursor`.
        """
        cleaned_token = (access_token or "").strip()
        if not cleaned_token:
            raise ConnectorAuthError("access_token is required", provider=self.provider_name)

        page_size = max(1, min(int(count), 500))
        page_limit = max(1, int(max_pages))
        initial_cursor = cursor or None

        for mutation_retry in range(2):
            current_cursor = initial_cursor
            added: List[NormalizedTransaction] = []
            modified: List[NormalizedTransaction] = []
            removed: List[str] = []
            accounts_by_id: Dict[str, NormalizedAccount] = {}
            warnings: List[str] = []
            tx_update_status = ""
            has_more = True
            pages_fetched = 0
            restart_needed = False

            while has_more and pages_fetched < page_limit:
                payload: Dict[str, Any] = {
                    "access_token": cleaned_token,
                    "count": page_size,
                    "options": {"include_personal_finance_category": True},
                }
                if current_cursor:
                    payload["cursor"] = current_cursor

                try:
                    data = self._post("/transactions/sync", payload)
                except ConnectorError as exc:
                    if exc.error_code == "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION" and mutation_retry == 0:
                        logger.info("Plaid sync mutated mid-pagination; restarting from initial cursor")
                        restart_needed = True
                        break
                    raise

                pages_fetched += 1
                if data.get("transactions_update_status"):
                    tx_update_status = str(data["transactions_update_status"])
                for raw_acct in data.get("accounts") or []:
                    if isinstance(raw_acct, dict) and raw_acct.get("account_id"):
                        norm_acct = _normalize_plaid_account(raw_acct, institution=institution)
                        accounts_by_id[norm_acct.external_account_id] = norm_acct

                for raw_tx in data.get("added") or []:
                    if isinstance(raw_tx, dict):
                        added.append(_normalize_plaid_transaction(raw_tx))

                for raw_tx in data.get("modified") or []:
                    if isinstance(raw_tx, dict):
                        modified.append(_normalize_plaid_transaction(raw_tx))

                for raw_rem in data.get("removed") or []:
                    if isinstance(raw_rem, dict) and raw_rem.get("transaction_id"):
                        removed.append(str(raw_rem["transaction_id"]))

                next_cursor = data.get("next_cursor")
                if next_cursor is not None:
                    current_cursor = str(next_cursor)
                has_more = bool(data.get("has_more", False))

            if restart_needed:
                continue

            if has_more:
                warnings.append(
                    f"Reached max_pages={page_limit}; call sync_transactions again with returned next_cursor."
                )

            return NormalizedSyncResult(
                provider=self.provider_name,
                accounts=list(accounts_by_id.values()),
                added=added,
                modified=modified,
                removed_external_ids=removed,
                next_cursor=current_cursor,
                has_more=has_more,
                transactions_update_status=tx_update_status,
                warnings=warnings,
            )

        raise ConnectorError(
            "Plaid transactions/sync failed repeatedly due to pagination mutation",
            provider=self.provider_name,
            error_code="TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION",
        )

    def sync(
        self,
        access_token: str,
        cursor: Optional[str] = None,
        *,
        count: int = 500,
        institution: str = "",
    ) -> NormalizedSyncResult:
        """Alias satisfying `ConnectorProtocol`."""
        return self.sync_transactions(access_token, cursor=cursor, count=count, institution=institution)

    def get_accounts_balances(
        self,
        access_token: str,
        *,
        institution: str = "",
    ) -> List[NormalizedAccount]:
        """Fetch live account balances via `POST /accounts/balance/get`."""
        cleaned_token = (access_token or "").strip()
        if not cleaned_token:
            raise ConnectorAuthError("access_token is required", provider=self.provider_name)

        data = self._post("/accounts/balance/get", {"access_token": cleaned_token})
        accounts: List[NormalizedAccount] = []
        for raw_acct in data.get("accounts") or []:
            if isinstance(raw_acct, dict):
                accounts.append(_normalize_plaid_account(raw_acct, institution=institution))
        return accounts

    def get_liabilities(
        self,
        access_token: str,
        *,
        institution: str = "",
    ) -> List[NormalizedLiability]:
        """
        Fetch credit card, student loan, and mortgage liabilities via `POST /liabilities/get`.

        Extracts purchase/cash/balance-transfer APRs (bps), statement balances, credit limits,
        minimum payments, payment due dates, overdue flags, and loan origination metadata.
        """
        cleaned_token = (access_token or "").strip()
        if not cleaned_token:
            raise ConnectorAuthError("access_token is required", provider=self.provider_name)

        data = self._post("/liabilities/get", {"access_token": cleaned_token})
        accounts_map: Dict[str, Dict[str, Any]] = {}
        for raw_acct in data.get("accounts") or []:
            if isinstance(raw_acct, dict) and raw_acct.get("account_id"):
                accounts_map[str(raw_acct["account_id"])] = raw_acct

        liabilities_obj = data.get("liabilities") or {}
        results: List[NormalizedLiability] = []

        # 1. Credit Cards
        for item in liabilities_obj.get("credit") or []:
            if not isinstance(item, dict):
                continue
            acct_id = str(item.get("account_id") or "")
            acct = accounts_map.get(acct_id, {})
            balances = acct.get("balances") or {}

            purchase_apr_bps = 0
            cash_apr_bps: Optional[int] = None
            bt_apr_bps: Optional[int] = None
            for apr_entry in item.get("aprs") or []:
                if not isinstance(apr_entry, dict):
                    continue
                apr_type = str(apr_entry.get("apr_type") or "").lower()
                bps = apr_pct_to_bps(apr_entry.get("apr_percentage"))
                if apr_type == "purchase_apr":
                    purchase_apr_bps = bps
                elif apr_type == "cash_advance_apr":
                    cash_apr_bps = bps
                elif apr_type == "balance_transfer_apr":
                    bt_apr_bps = bps
                elif purchase_apr_bps == 0 and bps > 0:
                    purchase_apr_bps = bps

            stmt_bal = item.get("last_statement_balance")
            last_pmt = item.get("last_payment_amount")
            results.append(
                NormalizedLiability(
                    external_account_id=acct_id,
                    name=sanitize_untrusted_text(
                        str(acct.get("name") or acct.get("official_name") or "Credit Card"), max_len=80
                    ),
                    issuer=sanitize_untrusted_text(institution, max_len=60),
                    kind="credit_card",
                    balance_cents=abs(decimal_to_cents(balances.get("current"))),
                    statement_balance_cents=abs(decimal_to_cents(stmt_bal)) if stmt_bal is not None else None,
                    credit_limit_cents=abs(decimal_to_cents(balances.get("limit"))),
                    apr_bps=purchase_apr_bps,
                    cash_advance_apr_bps=cash_apr_bps,
                    balance_transfer_apr_bps=bt_apr_bps,
                    min_payment_cents=abs(decimal_to_cents(item.get("minimum_payment_amount"))),
                    last_payment_amount_cents=abs(decimal_to_cents(last_pmt)) if last_pmt is not None else None,
                    last_payment_date=item.get("last_payment_date"),
                    next_payment_due_date=item.get("next_payment_due_date"),
                    last_statement_issue_date=item.get("last_statement_issue_date"),
                    is_overdue=bool(item.get("is_overdue", False)),
                    source="plaid_liabilities",
                )
            )

        # 2. Student Loans
        for item in liabilities_obj.get("student") or []:
            if not isinstance(item, dict):
                continue
            acct_id = str(item.get("account_id") or "")
            acct = accounts_map.get(acct_id, {})
            balances = acct.get("balances") or {}
            orig_amt = item.get("origination_principal_amount")
            last_pmt = item.get("last_payment_amount")
            stmt_bal = item.get("last_statement_balance")
            results.append(
                NormalizedLiability(
                    external_account_id=acct_id,
                    name=sanitize_untrusted_text(
                        str(item.get("loan_name") or acct.get("name") or "Student Loan"), max_len=80
                    ),
                    issuer=sanitize_untrusted_text(institution, max_len=60),
                    kind="student_loan",
                    balance_cents=abs(decimal_to_cents(balances.get("current"))),
                    statement_balance_cents=abs(decimal_to_cents(stmt_bal)) if stmt_bal is not None else None,
                    credit_limit_cents=0,
                    apr_bps=apr_pct_to_bps(item.get("interest_rate_percentage")),
                    min_payment_cents=abs(decimal_to_cents(item.get("minimum_payment_amount"))),
                    last_payment_amount_cents=abs(decimal_to_cents(last_pmt)) if last_pmt is not None else None,
                    last_payment_date=item.get("last_payment_date"),
                    next_payment_due_date=item.get("next_payment_due_date"),
                    last_statement_issue_date=item.get("last_statement_issue_date"),
                    is_overdue=bool(item.get("is_overdue", False)),
                    origination_date=item.get("origination_date"),
                    origination_principal_cents=(
                        abs(decimal_to_cents(orig_amt)) if orig_amt is not None else None
                    ),
                    source="plaid_liabilities",
                )
            )

        # 3. Mortgages
        for item in liabilities_obj.get("mortgage") or []:
            if not isinstance(item, dict):
                continue
            acct_id = str(item.get("account_id") or "")
            acct = accounts_map.get(acct_id, {})
            balances = acct.get("balances") or {}
            rate_obj = item.get("interest_rate") or {}
            orig_amt = item.get("origination_principal_amount")
            last_pmt = item.get("last_payment_amount")
            results.append(
                NormalizedLiability(
                    external_account_id=acct_id,
                    name=sanitize_untrusted_text(
                        str(acct.get("name") or acct.get("official_name") or "Mortgage"), max_len=80
                    ),
                    issuer=sanitize_untrusted_text(institution, max_len=60),
                    kind="mortgage",
                    balance_cents=abs(decimal_to_cents(balances.get("current"))),
                    credit_limit_cents=0,
                    apr_bps=apr_pct_to_bps(rate_obj.get("percentage")),
                    min_payment_cents=abs(decimal_to_cents(item.get("next_monthly_payment"))),
                    last_payment_amount_cents=abs(decimal_to_cents(last_pmt)) if last_pmt is not None else None,
                    last_payment_date=item.get("last_payment_date"),
                    next_payment_due_date=item.get("next_payment_due_date"),
                    is_overdue=bool(item.get("past_due_amount") and to_decimal(item.get("past_due_amount")) > 0),
                    origination_date=item.get("origination_date"),
                    origination_principal_cents=(
                        abs(decimal_to_cents(orig_amt)) if orig_amt is not None else None
                    ),
                    source="plaid_liabilities",
                )
            )

        return results

    def get_investments_holdings(self, access_token: str) -> List[NormalizedHolding]:
        """
        Fetch investment holdings via `POST /investments/holdings/get` using exact
        `quantity_e8`, `price_e8`, and `compute_market_value_cents()`.
        """
        cleaned_token = (access_token or "").strip()
        if not cleaned_token:
            raise ConnectorAuthError("access_token is required", provider=self.provider_name)

        data = self._post("/investments/holdings/get", {"access_token": cleaned_token})
        securities_map: Dict[str, Dict[str, Any]] = {}
        for sec in data.get("securities") or []:
            if isinstance(sec, dict) and sec.get("security_id"):
                securities_map[str(sec["security_id"])] = sec

        holdings: List[NormalizedHolding] = []
        for raw_h in data.get("holdings") or []:
            if not isinstance(raw_h, dict):
                continue
            sec = securities_map.get(str(raw_h.get("security_id") or ""), {})
            symbol = str(sec.get("ticker_symbol") or sec.get("name") or raw_h.get("security_id") or "").strip().upper()
            if not symbol:
                continue

            qty_e8 = decimal_to_e8(raw_h.get("quantity"))
            price_raw = raw_h.get("institution_price")
            if price_raw is None:
                price_raw = sec.get("close_price")
            price_e8 = decimal_to_e8(price_raw)
            cb_raw = raw_h.get("cost_basis")
            cost_basis_cents = decimal_to_cents(cb_raw)
            cb_known = cb_raw is not None and (cost_basis_cents > 0 or bool(sec.get("is_cash_equivalent")))
            market_value_cents = compute_market_value_cents(qty_e8, price_e8)

            asset_class = _map_plaid_security_class(
                str(sec.get("type") or ""),
                str(sec.get("subtype") or ""),
            )
            if bool(sec.get("is_cash_equivalent")):
                asset_class = "cash"
                cb_known = True
                if symbol.startswith("CUR:"):
                    symbol = symbol.split(":", 1)[1]

            currency = (
                raw_h.get("iso_currency_code")
                or sec.get("iso_currency_code")
                or "USD"
            )
            holdings.append(
                NormalizedHolding(
                    symbol=symbol[:24],
                    name=sanitize_untrusted_text(str(sec.get("name") or symbol), max_len=100),
                    asset_class=asset_class,
                    quantity_e8=qty_e8,
                    price_e8=price_e8,
                    cost_basis_cents=cost_basis_cents,
                    cost_basis_known=cb_known,
                    market_value_cents=market_value_cents,
                    external_account_id=str(raw_h.get("account_id") or ""),
                    currency=str(currency).upper(),
                    source="plaid_investments",
                )
            )

        return holdings
