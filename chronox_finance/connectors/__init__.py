"""
ChronoX Finance External Connectors Package.

Provides strictly read-only, deterministic, BYOK-first financial data connectors:
- `PlaidClient` (Hosted Link + 730d `/transactions/sync` + balances + liabilities + investments)
- `SimpleFINClient` (Base64 setup token claim + 730d 90-day chunked `/accounts`)
- `EmailScanner` (Opt-in local read-only IMAP `EXAMINE` + `BODY.PEEK` bill/receipt extractor)
- `PlaidLiabilitiesAdapter`, `OfficialCreditReportImporter`, `BureauAPIAdapter`
- `CoinbaseConnector` (CDP JWT + enforced `key_permissions` read-only check)
- `RobinhoodCryptoConnector` (Ed25519 per-request signing + enforced read-only allowlist)
- `SchwabConnector` (Optional BYOK OAuth2 + 7-day refresh expiry countdown)
- `parse_holdings_csv` (Broker CSV equities/crypto holdings importer)
- `QuoteService` (60s TTL official-only quotes: Coinbase spot, Schwab, Polygon BYOK, manual)
"""

from chronox_finance.connectors.base import (
    SCALE_E8,
    BrokerConnectorProtocol,
    ConnectorAuthError,
    ConnectorError,
    ConnectorProtocol,
    HostNotAllowedError,
    NormalizedAccount,
    NormalizedHolding,
    NormalizedLiability,
    NormalizedSyncResult,
    NormalizedTransaction,
    NotConfiguredForLocalDesktop,
    ReadOnlyViolationError,
    apr_pct_to_bps,
    compute_market_value_cents,
    decimal_to_cents,
    decimal_to_e8,
    e8_to_str,
    validate_strict_external_url,
)
from chronox_finance.connectors.brokers import (
    CoinbaseConnector,
    HoldingsCSVResult,
    RobinhoodCryptoConnector,
    SchwabConnector,
    SchwabTokenBundle,
    parse_holdings_csv,
)
from chronox_finance.connectors.credit_providers import (
    BureauAPIAdapter,
    CreditDerogatoryRecord,
    CreditImportBundle,
    CreditInquiryRecord,
    CreditProvider,
    CreditReportRecord,
    CreditTradelineRecord,
    OfficialCreditReportImporter,
    PlaidLiabilitiesAdapter,
    add_months,
    compute_drop_off_date,
    compute_inquiry_dates,
)
from chronox_finance.connectors.email_scanner import (
    IMAP_PEEK_FETCH_SPEC,
    EmailScanCandidate,
    EmailScanner,
    EmailScanResult,
    IMAPConfig,
    extract_email_candidate,
    sanitize_email_text,
)
from chronox_finance.connectors.plaid_client import (
    DEFAULT_DAYS_REQUESTED,
    PLAID_ALLOWED_HOSTS,
    PLAID_HISTORY_NOTE,
    PlaidClient,
    PlaidLinkPollResult,
    PlaidLinkTokenResult,
    PlaidTokenExchangeResult,
)
from chronox_finance.connectors.quotes import (
    DEFAULT_QUOTE_TTL_SECONDS,
    QuoteResult,
    QuoteService,
)
from chronox_finance.connectors.simplefin_client import SimpleFINClient

__all__ = [
    "BrokerConnectorProtocol",
    "BureauAPIAdapter",
    "CoinbaseConnector",
    "ConnectorAuthError",
    "ConnectorError",
    "ConnectorProtocol",
    "CreditDerogatoryRecord",
    "CreditImportBundle",
    "CreditInquiryRecord",
    "CreditProvider",
    "CreditReportRecord",
    "CreditTradelineRecord",
    "DEFAULT_DAYS_REQUESTED",
    "DEFAULT_QUOTE_TTL_SECONDS",
    "EmailScanCandidate",
    "EmailScanResult",
    "EmailScanner",
    "HoldingsCSVResult",
    "HostNotAllowedError",
    "IMAPConfig",
    "IMAP_PEEK_FETCH_SPEC",
    "NormalizedAccount",
    "NormalizedHolding",
    "NormalizedLiability",
    "NormalizedSyncResult",
    "NormalizedTransaction",
    "NotConfiguredForLocalDesktop",
    "OfficialCreditReportImporter",
    "PLAID_ALLOWED_HOSTS",
    "PLAID_HISTORY_NOTE",
    "PlaidClient",
    "PlaidLiabilitiesAdapter",
    "PlaidLinkPollResult",
    "PlaidLinkTokenResult",
    "PlaidTokenExchangeResult",
    "QuoteResult",
    "QuoteService",
    "ReadOnlyViolationError",
    "RobinhoodCryptoConnector",
    "SCALE_E8",
    "SchwabConnector",
    "SchwabTokenBundle",
    "SimpleFINClient",
    "add_months",
    "apr_pct_to_bps",
    "compute_drop_off_date",
    "compute_inquiry_dates",
    "compute_market_value_cents",
    "decimal_to_cents",
    "decimal_to_e8",
    "e8_to_str",
    "extract_email_candidate",
    "parse_holdings_csv",
    "sanitize_email_text",
    "validate_strict_external_url",
]
