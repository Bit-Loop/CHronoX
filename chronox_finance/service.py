"""
ChronoX Finance Service Facade.

Coordinates envelope-encrypted secret vault (`LocalSecretStore` + `LocalVaultAuth`),
SQLite WAL storage, 730-day self-updating auto-budgeting loop, Firefly III-inspired
automation rules & exact-sum splits, Plaid Hosted Link & SimpleFIN banking connectors,
opt-in read-only IMAP email scanner, MoneyStill analytics & scenario projections,
5-factor Credit Factor Analysis + Chase 5/24, Stocks & Crypto portfolio + official
broker adapters (Coinbase read-only, Robinhood Crypto read-only, Charles Schwab OAuth2),
and Multi-Provider AI Coaching + guarded Agentic Tool Coach (Preview -> Execute -> Undo).
"""

from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import logging
import os
import secrets
import sqlite3
import ssl
import tempfile
import threading
import time
import weakref
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from chronox_finance.ai.agent_tools import (
    AgentActionPlanner,
    AgentLedgerContext,
    compute_portfolio_analysis,
    validate_agent_batch,
)
from chronox_finance.ai.coach import FinancialCoach
from chronox_finance.ai.codex_client import (
    DEFAULT_CODEX_CLI_MODEL,
    DEFAULT_CODEX_MODEL,
    DEFAULT_OPENAI_BASE_URL,
    DEFAULT_WRAPPER_BASE_URL,
    DEFAULT_WRAPPER_MODEL,
    CodexCLIClient,
    CodexResponsesClient,
)
from chronox_finance.ai.gemini_client import (
    DEFAULT_GEMINI_BASE_URL,
    DEFAULT_GEMINI_MODEL,
    GeminiClient,
)
from chronox_finance.ai.harness import FinancialAIHarness
from chronox_finance.ai.local_cli_clients import (
    LOCAL_CLI_PRIORITY,
    LOCAL_CLI_SPECS,
    LocalCLIRegistry,
    LocalHTTPGatewayInfo,
    detect_local_http_gateways,
    get_default_local_cli_registry,
)
from chronox_finance.ai.opencode_client import (
    DEFAULT_OPENCODE_GO_BASE_URL,
    CompletionResult,
    LLMClient,
    OpenCodeGoClient,
)
from chronox_finance.connectors.base import (
    ConnectorAuthError,
    ConnectorError,
    NormalizedAccount,
    NormalizedHolding,
    NormalizedTransaction,
)
from chronox_finance.connectors.brokers import (
    CoinbaseConnector,
    RobinhoodCryptoConnector,
    SchwabConnector,
    parse_holdings_csv,
)
from chronox_finance.connectors.credit_providers import (
    CREDIT_API_VENDOR_SPECS,
    BureauAPIAdapter,
    OfficialCreditReportImporter,
    PlaidLiabilitiesAdapter,
)
from chronox_finance.connectors.email_scanner import EmailScanner, IMAPConfig
from chronox_finance.connectors.plaid_client import PlaidClient, _map_plaid_account_type
from chronox_finance.connectors.quotes import QuoteService
from chronox_finance.connectors.simplefin_client import SimpleFINClient
from chronox_finance.engines.adhd import (
    build_adhd_stats,
    compute_weekly_reset_update,
    enrich_impulse,
    is_reset_done_this_week,
)
from chronox_finance.engines.analytics import (
    AnalyticsEngine,
    reconstruct_net_worth_timeline,
)
from chronox_finance.engines.analytics import (
    simulate_scenario as run_scenario_simulation,
)
from chronox_finance.engines.autobudget import AutoBudgetEngine, AutoBudgetResult, current_cycle_ym
from chronox_finance.engines.categorizer import TransactionCategorizer, normalize_merchant
from chronox_finance.engines.credit_debt import CreditDebtOptimizer
from chronox_finance.engines.exporter import (
    ExportArtifact,
    ExportSnapshot,
    FinanceExporter,
)
from chronox_finance.engines.goal_planner import GoalOptimizerEngine
from chronox_finance.engines.importer import export_transactions_csv, ingest_statement
from chronox_finance.engines.nlp_parser import parse_quick_add
from chronox_finance.engines.onetap_quant import OneTapQuantEngine
from chronox_finance.engines.quant_math import QuantMathEngine
from chronox_finance.engines.recurring import (
    detect_bill_anomalies,
    detect_payday_schedule,
    detect_recurring_bills,
)
from chronox_finance.engines.rules import (
    RuleEngine,
    dry_run_rule,
    validate_safe_regex,
    validate_splits,
)
from chronox_finance.models import (
    AccountCreate,
    AccountOut,
    AccountType,
    ActiveProviderResolved,
    AgentExecuteResponse,
    AgentPlanRequest,
    AgentPlanResponse,
    AgentUndoResponse,
    AIProvider,
    AISettingsOut,
    AISettingsUpdate,
    AIUsageReport,
    AnalyticsOverview,
    BalanceTransferRequest,
    BalanceTransferResult,
    BrokerCoinbaseConnectRequest,
    BrokerRobinhoodConnectRequest,
    BrokerSchwabCompleteRequest,
    BrokerSchwabConnectRequest,
    BudgetAuditEvent,
    CoachAnswerResponse,
    ConnectionOut,
    CreditAPIConnectionOut,
    CreditAPIConnectRequest,
    CreditAPIListResponse,
    CreditAPISyncRequest,
    CreditAPIVendorInfo,
    CreditDerogatoryCreate,
    CreditDerogatoryOut,
    CreditInquiryCreate,
    CreditInquiryOut,
    CreditLineCreate,
    CreditLineUpdate,
    CreditOptimizationReport,
    CreditReportCreate,
    CreditReportOut,
    DashboardResponse,
    EmailConfigOut,
    EmailConfigUpdate,
    EmailQueueItem,
    GoalCategory,
    GoalContributeRequest,
    GoalCreate,
    GoalOut,
    GoalPlanRequest,
    GoalPlanResponse,
    GoalReorderRequest,
    GoalUpdate,
    HealthResponse,
    HoldingCreate,
    HoldingOut,
    ImpulseCreate,
    ImpulseDecision,
    ImpulseOut,
    ImpulseStatus,
    IntelligenceMode,
    JarKind,
    JarUpdate,
    NetWorthPoint,
    OnboardingCompleteRequest,
    OnboardingStatus,
    PlaidConfigRequest,
    PlaidExchangeRequest,
    PlaidLinkTokenRequest,
    PlaidLinkTokenResponse,
    PlaidPollLinkRequest,
    PortfolioAllocationItem,
    PortfolioSnapshotOut,
    PortfolioSummary,
    ProfileSettings,
    ProfileSettingsUpdate,
    QuantForecastReport,
    QuickAddRequest,
    RebalanceTransferRequest,
    RecurringBillCreate,
    RuleCreate,
    RuleGroupCreate,
    RuleGroupOut,
    RuleOut,
    RuleTestResult,
    RuleUpdate,
    ScenarioCreate,
    ScenarioOut,
    ScenarioOverride,
    ScenarioSimRequest,
    ScenarioSimResult,
    SimpleFINClaimRequest,
    SuggestionActionType,
    SuggestionBatchResponse,
    TagOut,
    TransactionCommentCreate,
    TransactionCommentOut,
    TransactionCreate,
    TransactionOut,
    TransactionSplitOut,
    TransactionSplitsRequest,
    TransactionTagsRequest,
    TransactionType,
    TransactionUpdate,
    VaultChangePasswordRequest,
    VaultRecoverRequest,
    VaultRecoverResponse,
    VaultSetupRequest,
    VaultSetupResponse,
    VaultStatus,
    WeeklyResetSummary,
    compute_holding_value_cents,
    utc_now_iso,
)
from chronox_finance.security import (
    LocalSecretStore,
    LocalVaultAuth,
    VaultLockedError,
    VaultNotInitializedError,
    mask_secret,
    validate_external_api_url,
)
from chronox_finance.storage import FinanceStorage, compute_tx_fingerprint

logger = logging.getLogger(__name__)


class PlanConflictError(ValueError):
    """Raised when an optimistic plan fingerprint or post-apply undo snapshot conflicts with later edits (HTTP 409)."""


_LOOKBACK_730D = 730
_MAX_TX_LIMIT = 250_000


def _row_fingerprint(row: Optional[Dict[str, Any]], fields: Sequence[str]) -> str:
    """Deterministic SHA-256 fingerprint of selected row columns for row-versioned Undo."""
    if not row:
        return ""
    payload = {k: row.get(k) for k in fields}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _splits_fingerprint(splits: Sequence[Dict[str, Any]]) -> str:
    canonical = [
        {
            "jar_id": s.get("jar_id"),
            "goal_id": s.get("goal_id"),
            "amount_cents": int(s.get("amount_cents", 0)),
            "memo": str(s.get("memo", "")),
        }
        for s in splits
    ]
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode("utf-8")).hexdigest()


def _days_until_iso_timestamp(iso_ts: Optional[str]) -> Optional[int]:
    if not iso_ts:
        return None
    raw = str(iso_ts).strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = dt - datetime.now(timezone.utc)
        return max(0, int(delta.total_seconds() // 86400))
    except ValueError:
        try:
            d = date.fromisoformat(raw[:10])
            return max(0, (d - date.today()).days)
        except ValueError:
            return None


class FinanceService:
    """Primary application service for ChronoX Finance."""

    _live_instances: "weakref.WeakSet[FinanceService]" = weakref.WeakSet()

    def __init__(
        self,
        db_path: str = ".chronox/finance.sqlite3",
        secret_store_path: Optional[Path] = None,
        *,
        kdf_iterations: Optional[int] = None,
        opencode_transport: Any = None,
        gemini_transport: Any = None,
        codex_transport: Any = None,
        wrapper_transport: Any = None,
        plaid_transport: Any = None,
        simplefin_transport: Any = None,
        broker_transport: Any = None,
        quote_transport: Any = None,
        credit_api_transport: Any = None,
        imap_factory: Any = None,
        local_cli_registry: Optional[LocalCLIRegistry] = None,
        plaid_poll_interval_seconds: float = 3.0,
        plaid_poll_max_seconds: float = 900.0,
        plaid_retry_delays: Sequence[float] = (15.0, 45.0, 120.0, 300.0) + (900.0,) * 92,
        plaid_periodic_sync_interval_seconds: float = 14400.0,
        plaid_callback_base_url: Optional[str] = None,
    ):
        self.db_path = db_path
        self.storage = FinanceStorage(db_path=db_path)
        self._temp_secret_dir: Optional[tempfile.TemporaryDirectory[str]] = None
        if secret_store_path is not None:
            store_file = Path(secret_store_path)
        elif db_path == ":memory:":
            self._temp_secret_dir = tempfile.TemporaryDirectory(prefix="chronox_vault_")
            store_file = Path(self._temp_secret_dir.name) / "secrets.enc"
        else:
            store_file = Path(db_path).parent / "secrets.enc"

        store_kwargs: Dict[str, Any] = {}
        if kdf_iterations is not None:
            store_kwargs["kdf_iterations"] = int(kdf_iterations)
        self.secrets = LocalSecretStore(store_file, **store_kwargs)
        self.auth = LocalVaultAuth(self.secrets)

        self.categorizer = TransactionCategorizer()
        self.autobudget = AutoBudgetEngine()
        self.credit_optimizer = CreditDebtOptimizer()
        self.goal_optimizer = GoalOptimizerEngine()
        self.quant_engine = QuantMathEngine()
        self.onetap_engine = OneTapQuantEngine()
        self.exporter = FinanceExporter()
        self.analytics_engine = AnalyticsEngine()
        self.rule_engine = RuleEngine()
        self.plaid_liabilities_adapter = PlaidLiabilitiesAdapter()
        self.credit_report_importer = OfficialCreditReportImporter()

        self._opencode_transport = opencode_transport
        self._gemini_transport = gemini_transport
        self._codex_transport = codex_transport
        self._wrapper_transport = wrapper_transport
        self._plaid_transport = plaid_transport
        self._simplefin_transport = simplefin_transport
        self._broker_transport = broker_transport
        self._quote_transport = quote_transport
        self._credit_api_transport = credit_api_transport
        self._imap_factory = imap_factory
        self._local_cli_registry: LocalCLIRegistry = (
            local_cli_registry if local_cli_registry is not None else get_default_local_cli_registry()
        )

        self._plaid_poll_interval_seconds = max(0.02, float(plaid_poll_interval_seconds))
        self._plaid_poll_max_seconds = max(1.0, float(plaid_poll_max_seconds))
        self._plaid_retry_delays: Tuple[float, ...] = tuple(max(0.01, float(d)) for d in plaid_retry_delays)
        self._plaid_periodic_sync_interval_seconds = max(1.0, float(plaid_periodic_sync_interval_seconds))
        self._plaid_callback_base_url = plaid_callback_base_url
        self._plaid_bg_lock = threading.RLock()
        self._plaid_stop_event = threading.Event()
        self._plaid_poller_thread: Optional[threading.Thread] = None
        self._plaid_poller_stop_event: Optional[threading.Event] = None
        self._plaid_retry_timers: Dict[int, threading.Timer] = {}
        self._plaid_periodic_holder: List[Optional[threading.Timer]] = [None]
        self._plaid_check_in_flight: bool = False
        self._plaid_pending_state: Optional[Dict[str, Any]] = None
        self._plaid_completed_nonces: Dict[str, Dict[str, Any]] = {}
        self._plaid_completed_link_tokens: Dict[str, Dict[str, Any]] = {}
        self._plaid_last_status: str = "idle"
        self._plaid_last_error: str = ""

        self._pending_schwab_states: Dict[str, Dict[str, Any]] = {}
        self._schwab_callback_server: Optional[HTTPServer] = None
        self._schwab_callback_port: Optional[int] = None
        self._schwab_tls_dir: Optional[tempfile.TemporaryDirectory[str]] = None
        FinanceService._live_instances.add(self)
        weakref.finalize(
            self,
            FinanceService._cleanup_plaid_resources,
            self._plaid_bg_lock,
            self._plaid_stop_event,
            self._plaid_retry_timers,
            self._plaid_periodic_holder,
        )

    @staticmethod
    def _cleanup_plaid_resources(
        bg_lock: threading.RLock,
        stop_event: threading.Event,
        retry_timers: Dict[int, threading.Timer],
        periodic_holder: Optional[List[Optional[threading.Timer]]] = None,
    ) -> None:
        from contextlib import suppress

        with bg_lock:
            stop_event.set()
            timers = list(retry_timers.values())
            retry_timers.clear()
            if periodic_holder and periodic_holder[0] is not None:
                timers.append(periodic_holder[0])
                periodic_holder[0] = None
        for timer in timers:
            with suppress(Exception):
                timer.cancel()

    def _stop_plaid_background_tasks(self) -> None:
        """Stop all active Plaid Hosted Link poller threads, backfill retry timers, and periodic sync timers."""
        with self._plaid_bg_lock:
            self._plaid_stop_event.set()
            poller_stop = self._plaid_poller_stop_event
            self._plaid_poller_stop_event = None
            if poller_stop is not None:
                poller_stop.set()
            poller_thread = self._plaid_poller_thread
            self._plaid_poller_thread = None
            timers = list(self._plaid_retry_timers.values())
            self._plaid_retry_timers.clear()
            if self._plaid_periodic_holder[0] is not None:
                timers.append(self._plaid_periodic_holder[0])
                self._plaid_periodic_holder[0] = None
            self._plaid_pending_state = None
            self._plaid_completed_nonces.clear()
            self._plaid_completed_link_tokens.clear()
        from contextlib import suppress

        for timer in timers:
            with suppress(Exception):
                timer.cancel()
        if (
            poller_thread is not None
            and poller_thread.is_alive()
            and poller_thread is not threading.current_thread()
        ):
            poller_thread.join(timeout=1.5)

    def _stop_schwab_callback_server(self) -> None:
        from contextlib import suppress

        srv = self._schwab_callback_server
        self._schwab_callback_server = None
        self._schwab_callback_port = None
        if srv is not None:
            with suppress(Exception):
                srv.shutdown()
            with suppress(Exception):
                srv.server_close()
        if self._schwab_tls_dir is not None:
            with suppress(OSError):
                self._schwab_tls_dir.cleanup()
            self._schwab_tls_dir = None

    def close(self) -> None:
        self._stop_plaid_background_tasks()
        self._stop_schwab_callback_server()
        self.storage.close()
        if self._temp_secret_dir is not None:
            try:
                self._temp_secret_dir.cleanup()
            except OSError:
                pass
            self._temp_secret_dir = None

    # --- Vault Lifecycle & Fail-Closed Secret Helpers ---

    def _check_idle_lock(self) -> bool:
        settings = self.storage.get_settings()
        minutes = int(settings.get("vault_auto_lock_minutes", 30))
        return self.secrets.check_idle_lock(minutes)

    def _require_vault_unlocked(self) -> None:
        """Fail closed whenever vault is not unlocked (never caches secrets across lock)."""
        self._check_idle_lock()
        state = self.secrets.vault_state()
        if state == "needs_setup":
            raise VaultNotInitializedError("Vault must be initialized before accessing encrypted credentials.")
        if state != "unlocked":
            raise VaultLockedError("Sync paused: vault locked")

    def _safe_get_secret(self, key: str) -> str:
        """Read a secret only when the vault is currently unlocked; never raises on locked status probes."""
        if not self.secrets.is_unlocked():
            return ""
        try:
            return self.secrets.get(key).strip()
        except (VaultLockedError, VaultNotInitializedError):
            return ""

    def _safe_get_secret_json(self, key: str) -> Optional[Dict[str, Any]]:
        if not self.secrets.is_unlocked():
            return None
        try:
            return self.secrets.get_json(key)
        except (VaultLockedError, VaultNotInitializedError):
            return None

    def is_auth_enabled(self) -> bool:
        """Mandatory local vault lock is always enabled."""
        return True

    def get_vault_status(self, *, authenticated: bool = False) -> VaultStatus:
        self._check_idle_lock()
        settings = self.storage.get_settings()
        vstate = self.secrets.vault_state()
        unlocked = vstate == "unlocked"
        return VaultStatus(
            state=vstate,
            initialized=self.secrets.is_initialized(),
            unlocked=unlocked,
            auth_enabled=True,
            authenticated=bool(authenticated and unlocked),
            onboarding_completed=bool(settings.get("onboarding_completed", 0)),
            auto_lock_minutes=int(settings.get("vault_auto_lock_minutes", 30)),
            csrf_token=self.secrets.get_csrf_token(),
            recovery_configured=self.secrets.is_initialized(),
            db_encrypted_at_rest=False,
        )

    def setup_vault(self, req: VaultSetupRequest) -> VaultSetupResponse:
        res = self.secrets.setup_vault(req.password)
        self.storage.log_budget_event("security", "Envelope-encrypted local vault initialized")
        status = self.get_vault_status(authenticated=True)
        return VaultSetupResponse(
            status=status,
            recovery_key=res["recovery_key"],
            recovery_groups=list(res["recovery_groups"]),
            challenge_indices=list(res["challenge_indices"]),
            csrf_token=self.secrets.get_csrf_token(),
            migrated_legacy_secrets=bool(res["migrated_legacy_secrets"]),
        )

    def unlock_vault(self, password: str) -> bool:
        ok = self.secrets.unlock(password)
        if ok:
            self._plaid_stop_event.clear()
            self.storage.log_budget_event("security", "Local vault unlocked")
            self._resume_plaid_background_syncs_if_any()
        return ok

    def recover_vault(self, req: VaultRecoverRequest) -> VaultRecoverResponse:
        new_rec = self.secrets.recover(
            req.recovery_key,
            req.new_password,
            rotate_recovery_key=req.rotate_recovery_key,
        )
        self._plaid_stop_event.clear()
        self.storage.log_budget_event("security", "Local vault recovered via Recovery Key (DEK rewrapped)")
        self._resume_plaid_background_syncs_if_any()
        status = self.get_vault_status(authenticated=True)
        return VaultRecoverResponse(
            status=status,
            csrf_token=self.secrets.get_csrf_token(),
            new_recovery_key=new_rec,
        )

    def change_vault_password(self, req: VaultChangePasswordRequest) -> VaultStatus:
        self.secrets.change_password(req.current_password, req.new_password)
        self.storage.log_budget_event("security", "Vault master password updated (DEK rewrapped)")
        return self.get_vault_status(authenticated=True)

    def lock_vault(self) -> VaultStatus:
        self._stop_plaid_background_tasks()
        self.secrets.lock()
        return self.get_vault_status(authenticated=False)

    def complete_onboarding(self, req: OnboardingCompleteRequest) -> OnboardingStatus:
        self.storage.update_settings({"onboarding_completed": 1})
        self.storage.log_budget_event("onboarding", f"First-run onboarding completed ({req.track})")
        return OnboardingStatus(onboarding_completed=True, track=req.track)

    # --- Multi-Provider AI Client & Key Resolution ---

    def _resolve_opencode_key_with_source(self) -> Tuple[str, Literal["saved", "env", "none"]]:
        saved = self._safe_get_secret("opencode_go_api_key")
        if saved:
            return saved, "saved"
        env_key = (os.getenv("OPENCODE_GO_API_KEY") or os.getenv("OPENCODE_API_KEY") or "").strip()
        if env_key:
            return env_key, "env"
        return "", "none"

    def _resolve_opencode_api_key(self) -> str:
        return self._resolve_opencode_key_with_source()[0]

    def _resolve_gemini_key_with_source(self) -> Tuple[str, Literal["saved", "env", "none"]]:
        saved = self._safe_get_secret("gemini_api_key")
        if saved:
            return saved, "saved"
        env_key = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
        if env_key:
            return env_key, "env"
        return "", "none"

    def _resolve_gemini_api_key(self) -> str:
        return self._resolve_gemini_key_with_source()[0]

    def _resolve_codex_key_with_source(self) -> Tuple[str, Literal["saved", "env", "none"]]:
        saved = self._safe_get_secret("codex_api_key")
        if saved:
            return saved, "saved"
        env_key = (os.getenv("OPENAI_API_KEY") or "").strip()
        if env_key:
            return env_key, "env"
        return "", "none"

    def _resolve_codex_api_key(self) -> str:
        return self._resolve_codex_key_with_source()[0]

    def _resolve_wrapper_key_with_source(self) -> Tuple[str, Literal["saved", "env", "none"]]:
        saved = self._safe_get_secret("wrapper_api_key")
        if saved:
            return saved, "saved"
        env_key = (os.getenv("CHRONOX_WRAPPER_API_KEY") or "").strip()
        if env_key:
            return env_key, "env"
        return "", "none"

    def _resolve_wrapper_api_key(self) -> str:
        return self._resolve_wrapper_key_with_source()[0]

    def _resolve_intelligence_mode(
        self,
        settings: Optional[Dict[str, Any]] = None,
        override: Optional[str] = None,
    ) -> IntelligenceMode:
        raw = override
        if not raw:
            s = settings if settings is not None else self.storage.get_settings()
            raw = str(s.get("intelligence_mode") or os.getenv("CHRONOX_INTELLIGENCE_MODE") or "hybrid")
        try:
            return IntelligenceMode(str(raw).strip().lower())
        except ValueError:
            return IntelligenceMode.HYBRID

    def _resolve_ai_provider_setting(
        self,
        settings: Optional[Dict[str, Any]] = None,
        override: Optional[str] = None,
    ) -> AIProvider:
        raw = override
        if not raw:
            s = settings if settings is not None else self.storage.get_settings()
            raw = str(s.get("ai_provider") or os.getenv("CHRONOX_AI_PROVIDER") or "auto")
        try:
            return AIProvider(str(raw).strip().lower())
        except ValueError:
            return AIProvider.AUTO

    def build_opencode_client(self) -> OpenCodeGoClient:
        settings = self.storage.get_settings()
        return OpenCodeGoClient(
            api_key=self._resolve_opencode_api_key(),
            base_url=settings.get("opencode_base_url")
            or os.getenv("OPENCODE_GO_BASE_URL", DEFAULT_OPENCODE_GO_BASE_URL),
            model=settings.get("opencode_model") or os.getenv("OPENCODE_GO_MODEL", "glm-5.3-flash"),
            protocol=settings.get("opencode_protocol", "auto"),
            transport=self._opencode_transport,
        )

    def build_gemini_client(self) -> GeminiClient:
        settings = self.storage.get_settings()
        return GeminiClient(
            api_key=self._resolve_gemini_api_key(),
            base_url=settings.get("gemini_base_url") or os.getenv("GEMINI_BASE_URL", DEFAULT_GEMINI_BASE_URL),
            model=settings.get("gemini_model") or os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL),
            transport=self._gemini_transport,
        )

    def build_codex_client(self) -> CodexResponsesClient:
        settings = self.storage.get_settings()
        return CodexResponsesClient(
            api_key=self._resolve_codex_api_key(),
            base_url=settings.get("codex_base_url") or os.getenv("OPENAI_BASE_URL", DEFAULT_OPENAI_BASE_URL),
            model=settings.get("codex_model") or os.getenv("OPENAI_CODEX_MODEL", DEFAULT_CODEX_MODEL),
            provider_name="openai_codex",
            wire_api="responses",
            transport=self._codex_transport,
        )

    def _detect_local_http_gateway(
        self,
        *,
        settings: Optional[Dict[str, Any]] = None,
        force_refresh: bool = False,
    ) -> Optional[LocalHTTPGatewayInfo]:
        """Auto-detect a running local OpenAI-compatible server (Ollama/LM Studio/LiteLLM/vLLM)."""
        if not self._is_local_cli_auto_detect_enabled(settings):
            return None
        hits = detect_local_http_gateways(
            force_refresh=force_refresh,
            transport=self._wrapper_transport,
        )
        return hits[0] if hits else None

    def _build_auto_local_gateway_client(
        self,
        *,
        settings: Optional[Dict[str, Any]] = None,
        force_refresh: bool = False,
    ) -> Optional[CodexResponsesClient]:
        hit = self._detect_local_http_gateway(settings=settings, force_refresh=force_refresh)
        if hit is None:
            return None
        return CodexResponsesClient(
            api_key="",
            base_url=hit.base_url,
            model=hit.model,
            provider_name="chat_wrapper",
            wire_api="chat_completions",
            transport=self._wrapper_transport,
        )

    def build_wrapper_client(self, *, auto_detect_local: bool = False) -> CodexResponsesClient:
        settings = self.storage.get_settings()
        wire = str(settings.get("wrapper_wire_api") or "chat_completions").strip().lower()
        if wire not in ("responses", "chat_completions"):
            wire = "chat_completions"
        raw_base = (settings.get("wrapper_base_url") or os.getenv("CHRONOX_WRAPPER_BASE_URL") or "").strip()
        if not raw_base and auto_detect_local:
            detected = self._build_auto_local_gateway_client(settings=settings)
            if detected is not None:
                return detected
        return CodexResponsesClient(
            api_key=self._resolve_wrapper_api_key(),
            base_url=raw_base,
            model=settings.get("wrapper_model") or os.getenv("CHRONOX_WRAPPER_MODEL", DEFAULT_WRAPPER_MODEL),
            provider_name="chat_wrapper",
            wire_api=wire,  # type: ignore[arg-type]
            transport=self._wrapper_transport,
        )

    def _is_local_cli_auto_detect_enabled(
        self, settings: Optional[Dict[str, Any]] = None
    ) -> bool:
        env_raw = os.getenv("CHRONOX_LOCAL_CLI_AUTO_DETECT")
        if env_raw is not None and env_raw.strip() != "":
            return env_raw.strip().lower() not in ("0", "false", "no", "off")
        s = settings if settings is not None else self.storage.get_settings()
        return bool(s.get("local_cli_auto_detect", 1))

    def _build_local_cli_client(
        self,
        provider_id: str,
        *,
        enabled: bool = True,
        settings: Optional[Dict[str, Any]] = None,
    ) -> LLMClient:
        s = settings if settings is not None else self.storage.get_settings()
        model_override: Optional[str] = None
        if provider_id == "codex_cli":
            raw_m = str(s.get("codex_cli_model") or "").strip()
            if raw_m:
                model_override = raw_m
        return self._local_cli_registry.build_client(
            provider_id,
            model=model_override,
            enabled=enabled,
        )

    def build_codex_cli_client(self, *, force_enable: bool = False) -> CodexCLIClient:
        settings = self.storage.get_settings()
        enabled = bool(
            force_enable
            or settings.get("codex_cli_enabled", 0)
            or self._is_local_cli_auto_detect_enabled(settings)
        )
        client = self._build_local_cli_client("codex_cli", enabled=enabled, settings=settings)
        return client  # type: ignore[return-value]

    def _build_cloud_ai_clients(self) -> Dict[str, LLMClient]:
        return {
            "opencode_go": self.build_opencode_client(),
            "gemini": self.build_gemini_client(),
            "openai_codex": self.build_codex_client(),
            "chat_wrapper": self.build_wrapper_client(),
        }

    def _build_all_ai_clients(self, *, force_enable_local_clis: bool = False) -> Dict[str, LLMClient]:
        settings = self.storage.get_settings()
        auto_detect = self._is_local_cli_auto_detect_enabled(settings)
        clients: Dict[str, LLMClient] = self._build_cloud_ai_clients()
        for pid in LOCAL_CLI_PRIORITY:
            if pid == "codex_cli":
                is_en = bool(force_enable_local_clis or auto_detect or settings.get("codex_cli_enabled", 0))
            else:
                is_en = bool(force_enable_local_clis or auto_detect)
            clients[pid] = self._build_local_cli_client(pid, enabled=is_en, settings=settings)
        return clients

    def _build_auto_clients_chain(self, settings: Dict[str, Any]) -> List[LLMClient]:
        ordered: List[LLMClient] = []
        cloud = self._build_cloud_ai_clients()
        for pid in ("opencode_go", "gemini", "openai_codex", "chat_wrapper"):
            if cloud[pid].is_configured:
                ordered.append(cloud[pid])

        # Auto-detected local OpenAI-compatible HTTP gateway (Ollama/LM Studio/LiteLLM/vLLM)
        # sits right after explicit cloud keys and before local CLIs
        if not cloud["chat_wrapper"].is_configured:
            auto_gw = self._build_auto_local_gateway_client(settings=settings)
            if auto_gw is not None and auto_gw.is_configured:
                ordered.append(auto_gw)

        if self._is_local_cli_auto_detect_enabled(settings):
            custom_codex_model = str(settings.get("codex_cli_model") or "").strip()
            for cli_client in self._local_cli_registry.get_failover_clients(
                run_auth_probe=False, include_cooldown=False
            ):
                if getattr(cli_client, "provider_name", "") == "codex_cli" and custom_codex_model:
                    cli_client = self._build_local_cli_client("codex_cli", enabled=True, settings=settings)
                if cli_client.is_configured:
                    ordered.append(cli_client)
        elif bool(settings.get("codex_cli_enabled", 0)):
            if not self._local_cli_registry.is_auth_failed("codex_cli"):
                c = self._build_local_cli_client("codex_cli", enabled=True, settings=settings)
                if c.is_configured:
                    ordered.append(c)
        return ordered

    def resolve_active_provider(
        self,
        provider_override: Optional[str] = None,
    ) -> ActiveProviderResolved:
        settings = self.storage.get_settings()
        pref = self._resolve_ai_provider_setting(settings, override=provider_override)
        if pref != AIProvider.AUTO:
            if pref.value in LOCAL_CLI_SPECS:
                c = self._build_local_cli_client(pref.value, enabled=True, settings=settings)
                if c.is_configured:
                    return pref.value  # type: ignore[return-value]
                return "none"
            cloud = self._build_cloud_ai_clients()
            c = cloud.get(pref.value)
            if c and c.is_configured:
                return pref.value  # type: ignore[return-value]
            if pref.value == "chat_wrapper":
                auto_gw = self._build_auto_local_gateway_client(settings=settings)
                if auto_gw is not None and auto_gw.is_configured:
                    return "chat_wrapper"
            return "none"

        chain = self._build_auto_clients_chain(settings)
        if chain:
            return str(getattr(chain[0], "provider_name", "none") or "none")  # type: ignore[return-value]
        return "none"

    def _build_coach_clients_list(
        self,
        provider_override: Optional[str] = None,
    ) -> List[LLMClient]:
        settings = self.storage.get_settings()
        pref = self._resolve_ai_provider_setting(settings, override=provider_override)
        auto_chain = self._build_auto_clients_chain(settings)
        if pref != AIProvider.AUTO:
            chosen: Optional[LLMClient] = None
            if pref.value in LOCAL_CLI_SPECS:
                chosen = self._build_local_cli_client(pref.value, enabled=True, settings=settings)
            else:
                cloud = self._build_cloud_ai_clients()
                chosen = cloud.get(pref.value)
                if (chosen is None or not chosen.is_configured) and pref.value == "chat_wrapper":
                    chosen = self._build_auto_local_gateway_client(settings=settings) or chosen
            ordered: List[LLMClient] = []
            seen_pids: set[str] = set()
            if chosen is not None and chosen.is_configured:
                ordered.append(chosen)
                seen_pids.add(str(getattr(chosen, "provider_name", pref.value) or pref.value))
            # Always append auto failover chain behind a pinned provider so signed-out/uninstalled pins never break AI
            for candidate in auto_chain:
                cpid = str(getattr(candidate, "provider_name", "") or "")
                if cpid and cpid not in seen_pids:
                    ordered.append(candidate)
                    seen_pids.add(cpid)
            return ordered

        return auto_chain

    def _provider_display_label(self, provider_id: str) -> str:
        labels = {
            "opencode_go": "OpenCode Go",
            "gemini": "Google Gemini",
            "openai_codex": "OpenAI / Codex",
            "chat_wrapper": "Durable Chat-Wrapper",
            "claude_cli": "Claude Code CLI",
            "opencode_cli": "OpenCode CLI",
            "codex_cli": "Codex CLI",
            "agy_cli": "Antigravity CLI",
            "none": "Local Quant Engine",
        }
        return labels.get((provider_id or "none").strip().lower(), provider_id or "Local Quant Engine")

    def get_local_clis(self, *, probe: bool = False) -> Dict[str, Any]:
        """Discover installed local AI CLIs and report active auto-detection state."""
        settings = self.storage.get_settings()
        auto_detect = self._is_local_cli_auto_detect_enabled(settings)
        statuses = self._local_cli_registry.list_detected_clis(
            run_auth_probe=bool(probe),
            run_version_probe=True,
            force_refresh=bool(probe),
        )
        active = self.resolve_active_provider()
        active_label = self._provider_display_label(active)
        clis: List[Dict[str, Any]] = []
        for st in statuses:
            d = st.to_dict()
            clis.append(
                {
                    "provider_id": d["provider_id"],
                    "display_name": d["display_name"],
                    "installed": bool(d["installed"]),
                    "auth_ready": d["auth_ready"],
                    "binary_path": d["binary_path"],
                    "version": d["version"],
                    "status_detail": d["status_detail"],
                    "is_active": bool(d["provider_id"] == active),
                    "default_model": d.get("default_model", ""),
                    "in_cooldown": bool(d.get("in_cooldown", False)),
                }
            )
        if auto_detect:
            gw_hit = self._detect_local_http_gateway(settings=settings, force_refresh=bool(probe))
            if gw_hit is not None:
                clis.insert(
                    0,
                    {
                        "provider_id": "chat_wrapper",
                        "display_name": f"{gw_hit.name} (Local)",
                        "installed": True,
                        "auth_ready": True,
                        "binary_path": gw_hit.base_url,
                        "version": f"{gw_hit.model_count} model{'s' if gw_hit.model_count != 1 else ''}",
                        "status_detail": f"Auto-detected at {gw_hit.base_url} · {gw_hit.model}",
                        "is_active": bool(active == "chat_wrapper"),
                        "default_model": gw_hit.model,
                        "in_cooldown": False,
                    },
                )
        return {
            "auto_detect_enabled": auto_detect,
            "active_provider": active,
            "active_provider_label": active_label,
            "clis": clis,
        }

    def _record_completion_usage(self, res: CompletionResult) -> None:
        if getattr(self.storage, "_closed", False):
            return
        provider = (res.provider or "unknown").strip().lower()
        try:
            self.storage.record_ai_usage(
                provider=provider,
                model=res.model or "",
                cached=bool(res.cached),
                input_tokens=int(res.input_tokens),
                output_tokens=int(res.output_tokens),
                cached_tokens=int(res.cached_tokens),
                cost_microdollars=int(res.cost_microdollars),
                saved_microdollars=int(res.saved_microdollars),
                latency_ms=int(res.latency_ms),
            )
        except sqlite3.ProgrammingError:
            # Graceful no-op if storage closed concurrently during shutdown
            return

    def get_ai_status(self) -> Dict[str, Any]:
        settings = self.storage.get_settings()
        oc_key, oc_source = self._resolve_opencode_key_with_source()
        gem_key, gem_source = self._resolve_gemini_key_with_source()
        codex_key, codex_source = self._resolve_codex_key_with_source()
        wrap_key, _wrap_source = self._resolve_wrapper_key_with_source()
        mode = self._resolve_intelligence_mode(settings)
        provider_pref = self._resolve_ai_provider_setting(settings)
        active = self.resolve_active_provider()
        auto_detect = self._is_local_cli_auto_detect_enabled(settings)
        max_daily = int(settings.get("opencode_max_daily_requests", 60))
        used_today = self.storage.count_ai_requests_today()

        oc_model = settings.get("opencode_model", "glm-5.3-flash")
        gem_model = settings.get("gemini_model", DEFAULT_GEMINI_MODEL)
        codex_model = settings.get("codex_model", DEFAULT_CODEX_MODEL)
        wrap_model = settings.get("wrapper_model", "") or DEFAULT_WRAPPER_MODEL
        cli_model = settings.get("codex_cli_model", "") or DEFAULT_CODEX_CLI_MODEL

        label_map: Dict[str, Tuple[str, str, str, str]] = {
            "opencode_go": (
                "OpenCode Go",
                oc_model,
                mask_secret(oc_key),
                settings.get("opencode_base_url", DEFAULT_OPENCODE_GO_BASE_URL),
            ),
            "gemini": (
                "Google Gemini",
                gem_model,
                mask_secret(gem_key),
                settings.get("gemini_base_url", DEFAULT_GEMINI_BASE_URL),
            ),
            "openai_codex": (
                "OpenAI / Codex",
                codex_model,
                mask_secret(codex_key),
                settings.get("codex_base_url", DEFAULT_OPENAI_BASE_URL),
            ),
            "chat_wrapper": (
                "Durable Chat-Wrapper",
                wrap_model,
                mask_secret(wrap_key),
                settings.get("wrapper_base_url", "") or DEFAULT_WRAPPER_BASE_URL,
            ),
            "codex_cli": (
                "Codex CLI",
                cli_model,
                "",
                "local://codex-cli",
            ),
            "opencode_cli": (
                "OpenCode CLI",
                str(LOCAL_CLI_SPECS["opencode_cli"]["default_model"] or "default"),
                "",
                "local://opencode-cli",
            ),
            "claude_cli": (
                "Claude Code CLI",
                str(LOCAL_CLI_SPECS["claude_cli"]["default_model"] or "default"),
                "",
                "local://claude-cli",
            ),
            "agy_cli": (
                "Antigravity CLI",
                str(LOCAL_CLI_SPECS["agy_cli"]["default_model"] or "gemini-3.8-flash-low"),
                "",
                "local://agy-cli",
            ),
        }
        if active in label_map:
            active_label, active_model, active_masked, active_url = label_map[active]
        else:
            active_label = "Quant Math Local" if mode == IntelligenceMode.DETERMINISTIC_ONLY else "Unconfigured"
            active_model = label_map.get(provider_pref.value, label_map["opencode_go"])[1]
            active_masked = ""
            active_url = label_map.get(provider_pref.value, label_map["opencode_go"])[3]

        return {
            "provider": active_label,
            "configured": active != "none",
            "key_masked": active_masked,
            "base_url": active_url,
            "model": active_model,
            "protocol": settings.get("opencode_protocol", "auto"),
            "intelligence_mode": mode.value,
            "ai_provider": provider_pref.value,
            "active_provider_resolved": active,
            "local_cli_auto_detect": auto_detect,
            "opencode_key_configured": bool(oc_key),
            "opencode_key_masked": mask_secret(oc_key),
            "opencode_key_source": oc_source,
            "opencode_model": oc_model,
            "gemini_api_key_set": bool(gem_key),
            "gemini_key_masked": mask_secret(gem_key),
            "gemini_key_source": gem_source,
            "gemini_model": gem_model,
            "codex_key_configured": bool(codex_key),
            "codex_key_masked": mask_secret(codex_key),
            "codex_key_source": codex_source,
            "codex_model": codex_model,
            "wrapper_key_configured": bool(wrap_key),
            "wrapper_key_masked": mask_secret(wrap_key),
            "wrapper_base_url": settings.get("wrapper_base_url", ""),
            "wrapper_model": settings.get("wrapper_model", ""),
            "wrapper_wire_api": settings.get("wrapper_wire_api", "chat_completions"),
            "codex_cli_enabled": bool(settings.get("codex_cli_enabled", 0)),
            "codex_cli_model": settings.get("codex_cli_model", ""),
            "requests_used_today": used_today,
            "max_daily_requests": max_daily,
            "quota_remaining_today": max(0, max_daily - used_today),
        }

    def get_ai_usage(self, days: int = 30) -> AIUsageReport:
        summary = self.storage.get_ai_usage_summary(days=days)
        return AIUsageReport(**summary)

    def list_ai_threads(self) -> List[Dict[str, Any]]:
        return self.storage.list_ai_threads()

    def delete_ai_thread(self, thread_id: str) -> bool:
        return self.storage.delete_ai_thread(thread_id)

    def get_health(self) -> HealthResponse:
        """Public readiness & health probe (never exposes any secret)."""
        db_ok = self.storage.ping()
        mode = IntelligenceMode.HYBRID.value
        active: ActiveProviderResolved = "none"
        vstate = self.secrets.vault_state()
        if db_ok:
            settings = self.storage.get_settings()
            mode = self._resolve_intelligence_mode(settings).value
            active = self.resolve_active_provider()
        return HealthResponse(
            status="ok" if db_ok else "degraded",
            app="ChronoX Finance",
            version="2.0.0",
            db_ok=db_ok,
            auth_enabled=True,
            vault_state=vstate,
            intelligence_mode=mode,
            active_provider=active,
        )

    # --- Self-Updating Auto-Budget Core Loop (730-Day + Split-Aware) ---

    def _load_recent_transactions_with_splits(
        self,
        *,
        days: int = _LOOKBACK_730D,
        today: Optional[date] = None,
        limit: int = _MAX_TX_LIMIT,
    ) -> Tuple[List[Dict[str, Any]], Dict[int, List[Dict[str, Any]]]]:
        ref = today or date.today()
        since_str = (ref - timedelta(days=max(1, int(days)))).isoformat()
        txs = self.storage.list_transactions(
            limit=limit,
            since_date=since_str,
            include_splits=True,
            include_tags=True,
        )
        splits_map = {int(t["id"]): list(t.get("splits") or []) for t in txs if t.get("splits")}
        return txs, splits_map

    def _compute_budget(self, today: Optional[date] = None) -> AutoBudgetResult:
        ref = today or date.today()
        ym = current_cycle_ym(ref)
        txs, splits_map = self._load_recent_transactions_with_splits(days=_LOOKBACK_730D, today=ref)
        return self.autobudget.compute(
            accounts=self.storage.list_accounts(),
            jars=self.storage.list_jars(),
            transactions=txs,
            recurring_bills=self.storage.list_recurring_bills(active_only=True),
            transfers=self.storage.list_budget_transfers(ym),
            settings=self.storage.get_settings(),
            credit_lines=self.storage.list_credit_lines(active_only=True),
            splits_by_tx_id=splits_map,
            today=ref,
        )

    def _apply_month_rollover_if_needed(self, today: date) -> int:
        """
        Automatically carry over prior-month cash-backed jar surpluses when a new calendar month begins.
        Never rolls forward unfunded target/projected allocations when no cash or income arrived.
        Uses child splits when present so split transactions are never double-counted.
        """
        settings = self.storage.get_settings()
        ym_now = current_cycle_ym(today)
        last_ym = str(settings.get("last_rollover_ym") or "")
        if not last_ym:
            self.storage.update_settings({"last_rollover_ym": ym_now})
            return 0
        if last_ym >= ym_now:
            return 0

        prev_first = date(today.year, today.month, 1) - timedelta(days=1)
        prev_ym = current_cycle_ym(prev_first)
        since_prev = date(prev_first.year, prev_first.month, 1).isoformat()
        txs = self.storage.list_transactions(limit=None, since_date=since_prev, include_splits=True)
        spent_prev: Dict[int, int] = {}
        prev_income_cents = 0
        for t in txs:
            if str(t["tx_date"])[:7] != prev_ym:
                continue
            if t.get("tx_type") == "income":
                prev_income_cents += int(t["amount_cents"])
                continue
            if t.get("tx_type") != "expense":
                continue
            child_splits = t.get("splits") or []
            if child_splits:
                for sp in child_splits:
                    if sp.get("jar_id") is not None:
                        jid = int(sp["jar_id"])
                        spent_prev[jid] = spent_prev.get(jid, 0) + int(sp["amount_cents"])
            elif t.get("jar_id") is not None:
                jid = int(t["jar_id"])
                spent_prev[jid] = spent_prev.get(jid, 0) + int(t["amount_cents"])

        profile_currency = str(settings.get("currency") or "USD").strip().upper() or "USD"
        liquid_cash_cents = sum(
            max(0, int(a["balance_cents"]))
            for a in self.storage.list_accounts()
            if a.get("is_liquid", 1)
            and a.get("account_type") != AccountType.CREDIT.value
            and (str(a.get("currency") or profile_currency).strip().upper() or profile_currency) == profile_currency
        )

        jars = self.storage.list_jars()
        if liquid_cash_cents <= 0:
            for j in jars:
                if int(j.get("rollover_cents", 0)) != 0:
                    self.storage.update_jar(int(j["id"]), {"rollover_cents": 0})
            self.storage.update_settings({"last_rollover_ym": ym_now})
            return 0

        total_auto_alloc = sum(max(0, int(j.get("auto_allocated_cents", 0))) for j in jars)
        funding_basis = min(liquid_cash_cents, prev_income_cents) if prev_income_cents > 0 else liquid_cash_cents
        alloc_scale = min(1.0, funding_basis / total_auto_alloc) if total_auto_alloc > 0 else 0.0

        safety_jar = next((j for j in jars if j["kind"] == JarKind.SAFETY.value), None)
        overflow_to_safety = 0
        proposed_rollovers: Dict[int, int] = {}

        for j in jars:
            jid = int(j["id"])
            if not j.get("rollover_enabled", 1):
                proposed_rollovers[jid] = 0
                continue
            funded_prev = int(round(max(0, int(j.get("auto_allocated_cents", 0))) * alloc_scale))
            prev_avail = funded_prev + int(j.get("rollover_cents", 0)) - spent_prev.get(jid, 0)
            surplus = max(0, prev_avail)
            cap = j.get("rollover_cap_cents")
            if cap is not None and surplus > int(cap):
                overflow_to_safety += surplus - int(cap)
                surplus = int(cap)
            proposed_rollovers[jid] = surplus

        if overflow_to_safety > 0 and safety_jar:
            sid = int(safety_jar["id"])
            proposed_rollovers[sid] = proposed_rollovers.get(sid, 0) + overflow_to_safety

        total_proposed = sum(proposed_rollovers.values())
        if total_proposed > liquid_cash_cents > 0:
            cap_ratio = liquid_cash_cents / total_proposed
            for jid in list(proposed_rollovers.keys()):
                proposed_rollovers[jid] = int(proposed_rollovers[jid] * cap_ratio)
            overflow_to_safety = int(overflow_to_safety * cap_ratio)

        rolled_jars = 0
        for jid, roll_val in proposed_rollovers.items():
            self.storage.update_jar(jid, {"rollover_cents": roll_val})
            if roll_val > 0:
                rolled_jars += 1

        self.storage.update_settings({"last_rollover_ym": ym_now})
        msg = f"Month rollover ({prev_ym} -> {ym_now}): rolled surplus across {rolled_jars} jar(s)"
        if overflow_to_safety > 0:
            msg += f" + swept ${overflow_to_safety / 100:,.2f} overflow into Safety & Future"
        self.storage.log_budget_event(
            "month_rollover",
            msg,
            {"from_ym": prev_ym, "to_ym": ym_now, "overflow_cents": overflow_to_safety},
        )
        return rolled_jars

    def _refresh_net_worth_snapshot_today(self, today: Optional[date] = None) -> None:
        ref = today or date.today()
        pts = reconstruct_net_worth_timeline(
            accounts=self.storage.list_accounts(),
            transactions=[],
            holdings=self.storage.list_holdings(),
            credit_lines=self.storage.list_credit_lines(active_only=True),
            existing_snapshots=None,
            portfolio_snapshots=self.storage.list_portfolio_snapshots(limit=_LOOKBACK_730D),
            days=1,
            today=ref,
        )
        pt = pts[-1]
        self.storage.upsert_net_worth_snapshot(
            snap_date=ref.isoformat(),
            liquid_cents=pt.liquid_cents,
            investment_cents=pt.investment_cents,
            debt_cents=pt.debt_cents,
            net_worth_cents=pt.net_worth_cents,
        )

    def _backfill_net_worth_snapshots(self, days: int = _LOOKBACK_730D, today: Optional[date] = None) -> int:
        ref = today or date.today()
        txs, _ = self._load_recent_transactions_with_splits(days=days, today=ref)
        points = reconstruct_net_worth_timeline(
            accounts=self.storage.list_accounts(),
            transactions=txs,
            holdings=self.storage.list_holdings(),
            credit_lines=self.storage.list_credit_lines(active_only=True),
            existing_snapshots=self.storage.list_net_worth_snapshots(limit=days),
            portfolio_snapshots=self.storage.list_portfolio_snapshots(limit=days),
            days=days,
            today=ref,
        )
        # Store periodic/weekly + recent daily points cleanly without churning thousands of rows
        saved = 0
        if points:
            step = max(1, len(points) // 180)
            chosen = points[::step]
            if chosen[-1].date != points[-1].date:
                chosen.append(points[-1])
            for pt in chosen:
                self.storage.upsert_net_worth_snapshot(
                    snap_date=pt.date,
                    liquid_cents=pt.liquid_cents,
                    investment_cents=pt.investment_cents,
                    debt_cents=pt.debt_cents,
                    net_worth_cents=pt.net_worth_cents,
                )
                saved += 1
        return saved

    def run_self_updating_sync(self, reason: str = "Automatic refresh", today: Optional[date] = None) -> Dict[str, Any]:
        """
        Execute the full 730-day Self-Updating Auto-Budget Loop:
        0. Purge soft-deleted goals past undo window & roll over prior-month surplus.
        1. Scan 730-day transaction history for recurring bills (including annual + min/max ranges).
        2. Infer updated payday schedule & paycheck amount from recent deposits.
        3. Recompute adaptive split-aware jar allocations and refresh net-worth snapshot.
        """
        ref = today or date.today()
        self.storage.purge_deleted_goals(older_than_hours=24)
        self._apply_month_rollover_if_needed(ref)
        txs, _ = self._load_recent_transactions_with_splits(days=_LOOKBACK_730D, today=ref)
        settings = self.storage.get_settings()

        detected_bills = detect_recurring_bills(txs, today=ref, lookback_days=_LOOKBACK_730D)
        existing_by_norm = {b["normalized_merchant"]: b for b in self.storage.list_recurring_bills(active_only=False)}
        bills_upserted = 0
        price_hikes_found: List[str] = []

        for dbill in detected_bills:
            existing = existing_by_norm.get(dbill.normalized_merchant)
            if existing and not existing.get("active", 1):
                continue
            saved = self.storage.upsert_recurring_bill(
                name=dbill.name,
                normalized_merchant=dbill.normalized_merchant,
                amount_cents=dbill.amount_cents,
                previous_amount_cents=dbill.previous_amount_cents,
                amount_min_cents=dbill.amount_min_cents,
                amount_max_cents=dbill.amount_max_cents,
                due_window_days=dbill.due_window_days,
                cadence=dbill.cadence.value,
                next_due_date=dbill.next_due_date,
                jar_id=dbill.jar_id,
                is_subscription=dbill.is_subscription,
                is_essential=dbill.is_essential,
                confidence=dbill.confidence,
                flagged_zombie=bool(existing["flagged_zombie"]) if existing else False,
            )
            bills_upserted += 1
            if dbill.previous_amount_cents is not None and dbill.amount_cents > dbill.previous_amount_cents:
                hike = dbill.amount_cents - dbill.previous_amount_cents
                price_hikes_found.append(f"{saved['name']} (+${hike / 100:,.2f})")

        user_cfg_payday = bool(settings.get("payday_user_configured", 0))
        payday = detect_payday_schedule(
            transactions=txs,
            fallback_cadence_days=int(settings.get("payday_cadence_days", 14)),
            fallback_payday_date=settings.get("next_payday_date"),
            fallback_paycheck_cents=int(settings.get("expected_paycheck_cents", 0)),
            today=ref,
            user_configured_payday=user_cfg_payday,
        )
        if payday.confidence >= 0.85 and not user_cfg_payday:
            self.storage.update_settings(
                {
                    "payday_cadence_days": payday.cadence_days,
                    "next_payday_date": payday.next_payday_date,
                    "expected_paycheck_cents": payday.estimated_paycheck_cents,
                }
            )

        budget = self._compute_budget(today=ref)
        jars_adjusted = 0
        for jar in self.storage.list_jars():
            jid = int(jar["id"])
            new_alloc = budget.adaptive_allocations_by_jar_id.get(jid)
            if new_alloc is not None and new_alloc != int(jar.get("auto_allocated_cents", 0)):
                self.storage.update_jar(jid, {"auto_allocated_cents": new_alloc})
                jars_adjusted += 1

        self._refresh_net_worth_snapshot_today(today=ref)

        if jars_adjusted > 0 or price_hikes_found or reason != "Automatic refresh":
            summary = f"Auto-budget updated ({reason}): Safe-to-Spend ${budget.safe_to_spend.safe_to_spend_cents / 100:,.2f}"
            if price_hikes_found:
                summary += f" · Price jump spotted: {', '.join(price_hikes_found)}"
            self.storage.log_budget_event(
                "auto_sync",
                summary,
                {
                    "bills_detected": bills_upserted,
                    "jars_adjusted": jars_adjusted,
                    "safe_to_spend_cents": budget.safe_to_spend.safe_to_spend_cents,
                    "safe_to_invest_cents": budget.safe_to_invest_cents,
                    "price_hikes": price_hikes_found,
                },
            )

        return {
            "bills_detected": bills_upserted,
            "jars_adjusted": jars_adjusted,
            "price_hikes": price_hikes_found,
            "safe_to_spend_cents": budget.safe_to_spend.safe_to_spend_cents,
            "daily_safe_to_spend_cents": budget.safe_to_spend.daily_safe_to_spend_cents,
            "safe_to_invest_cents": budget.safe_to_invest_cents,
        }

    # --- Quantitative Cashflow Runway, Volatility & Optimal Allocation ---

    def get_quant_forecast(
        self,
        days: int = 30,
        today: Optional[date] = None,
        *,
        budget: Optional[AutoBudgetResult] = None,
        credit_report: Optional[CreditOptimizationReport] = None,
        goals: Optional[List[GoalOut]] = None,
    ) -> QuantForecastReport:
        ref = today or date.today()
        horizon = max(7, min(730, int(days)))
        settings = self.storage.get_settings()
        b = budget if budget is not None else self._compute_budget(today=ref)
        cr = credit_report if credit_report is not None else self.get_credit_report(today=ref)
        g = goals if goals is not None else self.list_goals(include_completed=False)
        txs, splits_map = self._load_recent_transactions_with_splits(days=_LOOKBACK_730D, today=ref)
        bills = self.storage.list_recurring_bills(active_only=True)
        return self.quant_engine.build_forecast_report(
            budget=b,
            transactions=txs,
            recurring_bills=bills,
            settings=settings,
            credit_report=cr,
            goals=g,
            splits_by_tx_id=splits_map,
            days=horizon,
            today=ref,
        )

    # --- MoneyStill Analytics, Net Worth & Scenario Projections ---

    def get_analytics_overview(
        self,
        period: str = "1y",
        group_by: str = "jar",
        today: Optional[date] = None,
    ) -> AnalyticsOverview:
        ref = today or date.today()
        # Load full ledger history when 2y or YYYY-MM outside 730d is requested, with no 5,000-row cap
        txs = self.storage.list_transactions(
            limit=None,
            include_splits=True,
            include_tags=True,
        )
        splits_map = {int(t["id"]): list(t.get("splits") or []) for t in txs if t.get("splits")}
        return self.analytics_engine.build_overview(
            transactions=txs,
            jars=self.storage.list_jars(),
            accounts=self.storage.list_accounts(),
            holdings=self.storage.list_holdings(),
            credit_lines=self.storage.list_credit_lines(active_only=True),
            net_worth_snapshots=self.storage.list_net_worth_snapshots(limit=_LOOKBACK_730D),
            portfolio_snapshots=self.storage.list_portfolio_snapshots(limit=_LOOKBACK_730D),
            splits_by_tx_id=splits_map,
            period=period,
            group_by=group_by,
            today=ref,
        )

    def get_net_worth_series(
        self,
        days: int = _LOOKBACK_730D,
        today: Optional[date] = None,
    ) -> List[NetWorthPoint]:
        ref = today or date.today()
        bounded = max(7, min(_LOOKBACK_730D, int(days)))
        txs, _ = self._load_recent_transactions_with_splits(days=_LOOKBACK_730D, today=ref)
        return reconstruct_net_worth_timeline(
            accounts=self.storage.list_accounts(),
            transactions=txs,
            holdings=self.storage.list_holdings(),
            credit_lines=self.storage.list_credit_lines(active_only=True),
            existing_snapshots=self.storage.list_net_worth_snapshots(limit=_LOOKBACK_730D),
            portfolio_snapshots=self.storage.list_portfolio_snapshots(limit=_LOOKBACK_730D),
            days=bounded,
            today=ref,
        )

    def _to_scenario_out(self, row: Dict[str, Any]) -> ScenarioOut:
        raw_ov = row.get("overrides") or {}
        if isinstance(raw_ov, str):
            try:
                raw_ov = json.loads(raw_ov)
            except json.JSONDecodeError:
                raw_ov = {}
        return ScenarioOut(
            id=int(row["id"]),
            name=str(row["name"]),
            overrides=ScenarioOverride(**raw_ov),
            active=bool(row.get("active", True)),
            updated_at=str(row.get("updated_at", "")),
        )

    def list_scenarios(self) -> List[ScenarioOut]:
        return [self._to_scenario_out(r) for r in self.storage.list_scenarios()]

    def create_scenario(self, req: ScenarioCreate) -> ScenarioOut:
        row = self.storage.create_scenario(
            {
                "name": req.name.strip(),
                "overrides": req.overrides.model_dump(),
                "active": req.active,
            }
        )
        return self._to_scenario_out(row)

    def delete_scenario(self, scenario_id: int) -> bool:
        return self.storage.delete_scenario(scenario_id)

    def simulate_scenario(
        self,
        req: ScenarioSimRequest,
        today: Optional[date] = None,
    ) -> ScenarioSimResult:
        ref = today or date.today()
        settings = self.storage.get_settings()
        budget = self._compute_budget(today=ref)
        txs, _ = self._load_recent_transactions_with_splits(days=_LOOKBACK_730D, today=ref)
        goals = self.list_goals(include_completed=False)
        bills = self.storage.list_recurring_bills(active_only=True)
        credit_lines = self.storage.list_credit_lines(active_only=True)

        merged_ov = req.overrides
        if req.scenario_id is not None:
            saved = self.storage.get_scenario(req.scenario_id)
            if saved and isinstance(saved.get("overrides"), dict):
                base_ov = ScenarioOverride(**saved["overrides"])
                # Layer explicit request overrides on top of saved scenario overrides
                combined_shifts = dict(base_ov.goal_date_shifts)
                combined_shifts.update(req.overrides.goal_date_shifts)
                combined_one_offs = list(base_ov.one_off_events) + list(req.overrides.one_off_events)
                merged_ov = ScenarioOverride(
                    goal_date_shifts=combined_shifts,
                    monthly_income_delta_cents=(
                        req.overrides.monthly_income_delta_cents
                        if req.overrides.monthly_income_delta_cents != 0
                        else base_ov.monthly_income_delta_cents
                    ),
                    monthly_expense_delta_cents=(
                        req.overrides.monthly_expense_delta_cents
                        if req.overrides.monthly_expense_delta_cents != 0
                        else base_ov.monthly_expense_delta_cents
                    ),
                    one_off_events=combined_one_offs,
                )

        return run_scenario_simulation(
            starting_balance_cents=budget.safe_to_spend.total_liquid_cash_cents,
            recurring_bills=bills,
            goals=goals,
            transactions=txs,
            credit_lines=credit_lines,
            payday_cadence_days=int(settings.get("payday_cadence_days", 14)),
            next_payday_date=budget.safe_to_spend.next_payday_date,
            estimated_paycheck_cents=budget.safe_to_spend.estimated_paycheck_cents,
            emergency_buffer_cents=int(settings.get("emergency_buffer_cents", 50_000)),
            horizon_days=req.horizon_days,
            overrides=merged_ov,
            today=ref,
        )

    # --- Dashboard ---

    def get_dashboard(self, today: Optional[date] = None) -> DashboardResponse:
        ref = today or date.today()
        self._apply_month_rollover_if_needed(ref)
        settings = self.storage.get_settings()
        budget = self._compute_budget(today=ref)

        raw_impulses = self.storage.list_impulses(include_resolved=False)
        impulses = [
            enrich_impulse(
                imp,
                hourly_wage_cents=int(settings.get("hourly_wage_cents", 3000)),
                safe_to_spend_cents=budget.safe_to_spend.safe_to_spend_cents,
            )
            for imp in raw_impulses
        ]
        recent_txs = self.list_transactions(limit=25)
        triage_txs = self.storage.list_transactions(limit=50, needs_review_only=True)
        adhd_stats = build_adhd_stats(self.storage.get_adhd_progress())
        accounts = [AccountOut(**a) for a in self.storage.list_accounts()]
        events = [BudgetAuditEvent(**e) for e in self.storage.list_budget_events(limit=15)]
        since_60d = (ref - timedelta(days=60)).isoformat()
        anomalies = detect_bill_anomalies(
            self.storage.list_transactions(limit=500, since_date=since_60d),
            self.storage.list_recurring_bills(active_only=True),
            today=ref,
        )
        credit_report = self.get_credit_report(today=ref)
        goals = self.list_goals(include_completed=False)
        quant_forecast = self.get_quant_forecast(
            days=30,
            today=ref,
            budget=budget,
            credit_report=credit_report,
            goals=goals,
        )
        mode = self._resolve_intelligence_mode(settings)
        active_provider = self.resolve_active_provider()

        return DashboardResponse(
            generated_at=utc_now_iso(),
            safe_to_spend=budget.safe_to_spend,
            safe_to_invest_cents=budget.safe_to_invest_cents,
            monthly_income_cents=budget.monthly_income_cents,
            monthly_spent_cents=budget.monthly_spent_cents,
            smoothed_monthly_income_cents=budget.smoothed_monthly_income_cents,
            accounts=accounts,
            jars=budget.jars,
            rebalance_plans=budget.rebalance_plans,
            upcoming_bills=budget.upcoming_bills,
            bill_anomalies=anomalies,
            cooling_impulses=impulses,
            recent_transactions=recent_txs,
            adhd_stats=adhd_stats,
            needs_triage_count=len(triage_txs),
            ai_status=self.get_ai_status(),
            recent_budget_events=events,
            credit_summary={
                "aggregate_utilization_pct": credit_report.aggregate_utilization_pct,
                "aggregate_tier": credit_report.aggregate_tier,
                "total_debt_cents": credit_report.total_debt_cents,
                "monthly_interest_burn_cents": credit_report.monthly_interest_burn_cents,
                "dti_pct": credit_report.dti_pct,
                "dti_tier": credit_report.dti_tier,
                "autopay_missing_count": credit_report.autopay_missing_count,
                "alerts_count": len(credit_report.alerts),
                "recommended_strategy": credit_report.recommended_strategy,
                "five_twenty_four_count": (
                    credit_report.five_twenty_four.count_5_24 if credit_report.five_twenty_four else 0
                ),
            },
            goals=goals,
            quant_forecast=quant_forecast,
            intelligence_mode=mode.value,
            active_ai_provider=active_provider,
        )

    # --- Transactions, Splits, Tags, Comments & Quick Add ---

    def _get_comment_counts(self, tx_ids: Sequence[int]) -> Dict[int, int]:
        if not tx_ids:
            return {}
        counts: Dict[int, int] = {}
        with self.storage._lock:
            for i in range(0, len(tx_ids), 500):
                chunk = [int(x) for x in tx_ids[i : i + 500]]
                placeholders = ",".join("?" for _ in chunk)
                rows = self.storage._conn.execute(
                    f"SELECT tx_id, COUNT(*) AS cnt FROM transaction_comments WHERE tx_id IN ({placeholders}) GROUP BY tx_id",
                    chunk,
                ).fetchall()
                for r in rows:
                    counts[int(r["tx_id"])] = int(r["cnt"])
        return counts

    def _to_tx_out(
        self,
        row: Dict[str, Any],
        *,
        comment_counts: Optional[Dict[int, int]] = None,
    ) -> TransactionOut:
        tx_id = int(row["id"])
        raw_splits = row.get("splits")
        if raw_splits is None:
            raw_splits = self.storage.get_transaction_splits(tx_id)
        splits = [
            TransactionSplitOut(
                id=int(s["id"]),
                parent_tx_id=int(s["parent_tx_id"]),
                jar_id=int(s["jar_id"]) if s.get("jar_id") is not None else None,
                goal_id=int(s["goal_id"]) if s.get("goal_id") is not None else None,
                amount_cents=int(s["amount_cents"]),
                memo=str(s.get("memo", "")),
                jar_name=str(s.get("jar_name", "")),
            )
            for s in raw_splits
        ]
        raw_tags = row.get("tags")
        if raw_tags is None:
            raw_tags = self.storage.get_transaction_tags(tx_id)
        tags = [
            TagOut(id=int(tg["id"]), name=str(tg["name"]), color=str(tg.get("color", "#06b6d4")))
            for tg in raw_tags
        ]
        c_count = (
            comment_counts.get(tx_id, 0)
            if comment_counts is not None
            else len(self.storage.list_transaction_comments(tx_id))
        )
        return TransactionOut(
            id=tx_id,
            account_id=int(row["account_id"]),
            jar_id=int(row["jar_id"]) if row.get("jar_id") is not None else None,
            jar_name=row.get("jar_name", ""),
            jar_icon=row.get("jar_icon", ""),
            tx_type=TransactionType(row["tx_type"]),
            amount_cents=int(row["amount_cents"]),
            merchant=row["merchant"],
            normalized_merchant=row["normalized_merchant"],
            subcategory=row.get("subcategory", ""),
            memo=row.get("memo", ""),
            tx_date=row["tx_date"],
            confidence=float(row.get("confidence", 1.0)),
            needs_review=bool(row.get("needs_review", 0)),
            source=row.get("source", "manual"),
            created_at=row["created_at"],
            splits=splits,
            tags=tags,
            comment_count=c_count,
        )

    def _persist_rule_changes_for_tx(self, tx_row: Dict[str, Any], changes: Dict[str, Any]) -> bool:
        tx_id = int(tx_row["id"])
        mutated = False
        tx_updates: Dict[str, Any] = {}
        if "jar_id" in changes and changes["jar_id"] is not None:
            if self.storage.get_jar(int(changes["jar_id"])):
                tx_updates["jar_id"] = int(changes["jar_id"])
                tx_updates["needs_review"] = 0
                tx_updates["confidence"] = 1.0
        if "memo" in changes and isinstance(changes["memo"], str):
            tx_updates["memo"] = changes["memo"][:300]
        if tx_updates:
            self.storage.update_transaction(tx_id, tx_updates)
            mutated = True

        tags_to_add = changes.get("tags_to_add") or []
        if tags_to_add:
            existing = [t["name"] for t in self.storage.get_transaction_tags(tx_id)]
            merged = list(existing)
            lower_set = {x.lower() for x in merged}
            for tag in tags_to_add:
                cleaned = str(tag).strip()
                if cleaned and cleaned.lower() not in lower_set:
                    merged.append(cleaned)
                    lower_set.add(cleaned.lower())
            self.storage.set_transaction_tags(tx_id, merged)
            mutated = True

        computed_splits = changes.get("splits") or []
        if computed_splits:
            parent_amt = int(tx_row["amount_cents"])
            valid_splits = validate_splits(parent_amt, computed_splits)
            self.storage.set_transaction_splits(tx_id, valid_splits)
            mutated = True

        for alloc in changes.get("goal_allocations") or []:
            gid = int(alloc["goal_id"])
            amt = int(alloc["amount_cents"])
            existing_goal = self.storage.get_goal(gid)
            if existing_goal and amt > 0:
                new_saved = int(existing_goal["saved_cents"]) + amt
                new_status = "funded" if new_saved >= int(existing_goal["target_cents"]) else existing_goal["status"]
                self.storage.update_goal(gid, {"saved_cents": new_saved, "status": new_status})
                mutated = True

        return mutated

    def _apply_active_rules_to_tx(self, tx_row: Dict[str, Any]) -> Dict[str, Any]:
        active_rules = self.storage.list_rules(active_only=True)
        if not active_rules:
            return tx_row
        rule_groups = self.storage.list_rule_groups()
        tx_id = int(tx_row["id"])
        tx_enriched = dict(tx_row)
        tx_enriched["tags"] = [t["name"] for t in self.storage.get_transaction_tags(tx_id)]
        res = self.rule_engine.evaluate_transaction(tx_enriched, active_rules, rule_groups=rule_groups)
        if res.matched and res.changes:
            self._persist_rule_changes_for_tx(tx_row, res.changes)
            refreshed = self.storage.get_transaction(tx_id)
            if refreshed:
                return refreshed
        return tx_row

    def add_transaction_raw(
        self,
        *,
        account_id: Optional[int],
        tx_type: str,
        amount_cents: int,
        merchant: str,
        tx_date: str,
        jar_id: Optional[int] = None,
        subcategory: str = "",
        memo: str = "",
        source: str = "manual",
        deduplicate: bool = False,
        external_id: str = "",
        transfer_direction: str = "",
        adjust_balance: bool = True,
        run_auto_sync: bool = True,
        apply_rules: bool = True,
    ) -> Optional[TransactionOut]:
        acct_id = account_id if account_id is not None else self.storage.get_default_account_id()
        norm = normalize_merchant(merchant)

        confidence = 1.0
        needs_review = False
        eff_transfer_dir = (transfer_direction or "").strip().lower()

        if tx_type == TransactionType.EXPENSE.value and jar_id is None:
            jars_by_slug = {j["slug"]: j for j in self.storage.list_jars()}
            learned = self.storage.get_merchant_rules()
            jar_id, auto_sub, confidence, needs_review = self.categorizer.categorize(
                merchant, memo, jars_by_slug, learned
            )
            if not subcategory:
                subcategory = auto_sub
        elif tx_type == TransactionType.INCOME.value:
            jar_id = None
            if not subcategory:
                subcategory = self.categorizer.classify_income_subcategory(merchant, memo)
        elif tx_type == TransactionType.TRANSFER.value:
            jar_id = None
            if not subcategory:
                subcategory = "Internal Transfer"
            if not eff_transfer_dir and adjust_balance:
                combined_low = f"{merchant} {memo}".lower()
                if any(tok in combined_low for tok in ("transfer from", "xfer from", "from savings", "from checking", "inbound", "deposit from")):
                    eff_transfer_dir = "in"
                else:
                    eff_transfer_dir = "out"

        fp = (
            compute_tx_fingerprint(acct_id, tx_date, tx_type, amount_cents, norm, memo, external_id=external_id)
            if deduplicate
            else None
        )
        row = self.storage.insert_transaction(
            account_id=acct_id,
            jar_id=jar_id,
            tx_type=tx_type,
            amount_cents=amount_cents,
            merchant=merchant.strip(),
            normalized_merchant=norm,
            subcategory=subcategory,
            memo=memo.strip(),
            tx_date=tx_date,
            confidence=confidence,
            needs_review=needs_review,
            source=source,
            fingerprint=fp,
            external_id=external_id,
            transfer_direction=eff_transfer_dir,
            adjust_balance=adjust_balance,
        )
        if row is None:
            return None

        if apply_rules:
            row = self._apply_active_rules_to_tx(row)

        if run_auto_sync:
            reason = (
                f"Income +${amount_cents / 100:,.2f} ({merchant})"
                if tx_type == "income"
                else f"Logged ${amount_cents / 100:,.2f} at {merchant}"
            )
            self.run_self_updating_sync(reason=reason)

        return self._to_tx_out(row)

    def create_transaction(self, req: TransactionCreate) -> TransactionOut:
        tx = self.add_transaction_raw(
            account_id=req.account_id,
            jar_id=req.jar_id,
            tx_type=req.tx_type.value,
            amount_cents=req.amount_cents,
            merchant=req.merchant,
            subcategory=req.subcategory,
            memo=req.memo,
            tx_date=req.tx_date,
            source=req.source,
            transfer_direction=req.transfer_direction or "",
        )
        assert tx is not None
        return tx

    def quick_add(self, req: QuickAddRequest) -> Dict[str, Any]:
        """Parse ADHD natural-language quick-add and route to Transaction or Impulse Vault."""
        settings = self.storage.get_settings()
        parsed = parse_quick_add(
            req.text,
            default_cooling_hours=int(settings.get("default_cooling_hours", 48)),
        )
        if parsed.intent == "impulse":
            imp = self.create_impulse(
                ImpulseCreate(
                    item_name=parsed.merchant,
                    price_cents=parsed.amount_cents,
                    reason=parsed.memo,
                    cooling_hours=parsed.cooling_hours,
                )
            )
            return {
                "kind": "impulse",
                "message": f"Parked '{imp.item_name}' (${imp.price_cents / 100:,.2f}) in your {imp.cooling_hours}h Cooling-Off Vault!",
                "impulse": imp.model_dump(),
            }

        tx = self.add_transaction_raw(
            account_id=req.account_id,
            tx_type=parsed.tx_type,
            amount_cents=parsed.amount_cents,
            merchant=parsed.merchant,
            memo=parsed.memo,
            tx_date=parsed.tx_date,
            source="quick_add",
        )
        assert tx is not None
        if parsed.tx_type == "income":
            msg = f"Logged +${tx.amount_cents / 100:,.2f} ({tx.merchant}) — auto-budget & Safe-to-Spend updated!"
        else:
            msg = f"Logged ${tx.amount_cents / 100:,.2f} at {tx.merchant} -> {tx.jar_name}"
        return {"kind": "transaction", "message": msg, "transaction": tx.model_dump()}

    def update_transaction(self, tx_id: int, req: TransactionUpdate) -> Optional[TransactionOut]:
        existing = self.storage.get_transaction(tx_id)
        if not existing:
            return None
        updates: Dict[str, Any] = {}
        if req.jar_id is not None:
            if not self.storage.get_jar(req.jar_id):
                raise ValueError("Unknown jar ID")
            updates["jar_id"] = req.jar_id
            updates["needs_review"] = 0
            updates["confidence"] = 1.0
        if req.amount_cents is not None:
            # If parent amount changes and existing splits no longer sum to parent, require explicit split update
            existing_splits = self.storage.get_transaction_splits(tx_id)
            if existing_splits and req.amount_cents != int(existing["amount_cents"]):
                raise ValueError(
                    "Transaction has child splits; update or clear splits before changing parent amount."
                )
            updates["amount_cents"] = req.amount_cents
        if req.tx_type is not None:
            updates["tx_type"] = req.tx_type.value
        if req.tx_date is not None:
            updates["tx_date"] = req.tx_date
        if req.merchant is not None:
            updates["merchant"] = req.merchant.strip()
            updates["normalized_merchant"] = normalize_merchant(req.merchant)
        if req.subcategory is not None:
            updates["subcategory"] = req.subcategory.strip()
        if req.memo is not None:
            updates["memo"] = req.memo.strip()

        updated = self.storage.update_transaction(tx_id, updates)
        if not updated:
            return None

        if req.learn_merchant_rule and req.jar_id is not None:
            self.storage.upsert_merchant_rule(
                updated["normalized_merchant"],
                req.jar_id,
                updated.get("subcategory", ""),
            )
            self.storage.log_budget_event(
                "learned_rule",
                f"Learned rule: '{updated['merchant']}' -> {updated.get('jar_name', 'Jar')}",
            )

        self.run_self_updating_sync(reason=f"Updated transaction #{tx_id}")
        return self._to_tx_out(updated)

    def delete_transaction(self, tx_id: int) -> bool:
        deleted = self.storage.delete_transaction(tx_id)
        if deleted:
            self.run_self_updating_sync(reason=f"Removed transaction #{tx_id}")
        return deleted

    def list_transactions(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        account_id: Optional[int] = None,
        jar_id: Optional[int] = None,
        needs_review_only: bool = False,
        search: Optional[str] = None,
    ) -> List[TransactionOut]:
        rows = self.storage.list_transactions(
            limit=max(1, min(_MAX_TX_LIMIT, int(limit))),
            offset=max(0, int(offset)),
            start_date=start_date,
            end_date=end_date,
            account_id=account_id,
            jar_id=jar_id,
            needs_review_only=needs_review_only,
            search=search,
            include_splits=True,
            include_tags=True,
        )
        comment_counts = self._get_comment_counts([int(r["id"]) for r in rows])
        return [self._to_tx_out(r, comment_counts=comment_counts) for r in rows]

    def set_transaction_splits(
        self,
        tx_id: int,
        req: TransactionSplitsRequest,
    ) -> List[TransactionSplitOut]:
        parent = self.storage.get_transaction(tx_id)
        if not parent:
            raise ValueError(f"Transaction {tx_id} not found")
        raw_splits = [sp.model_dump() for sp in req.splits]
        validated = validate_splits(int(parent["amount_cents"]), raw_splits)
        for sp in validated:
            if sp.get("jar_id") is not None and not self.storage.get_jar(int(sp["jar_id"])):
                raise ValueError(f"Unknown jar ID: {sp['jar_id']}")
            if sp.get("goal_id") is not None and not self.storage.get_goal(int(sp["goal_id"])):
                raise ValueError(f"Unknown goal ID: {sp['goal_id']}")
        saved = self.storage.set_transaction_splits(tx_id, validated)
        self.run_self_updating_sync(reason=f"Split transaction #{tx_id} into {len(saved)} lines")
        return [TransactionSplitOut(**s) for s in saved]

    def clear_transaction_splits(self, tx_id: int) -> None:
        if not self.storage.get_transaction(tx_id):
            raise ValueError(f"Transaction {tx_id} not found")
        self.storage.clear_transaction_splits(tx_id)
        self.run_self_updating_sync(reason=f"Cleared splits on transaction #{tx_id}")

    def set_transaction_tags(self, tx_id: int, req: TransactionTagsRequest) -> List[TagOut]:
        rows = self.storage.set_transaction_tags(tx_id, req.tags)
        return [TagOut(**r) for r in rows]

    def list_transaction_comments(self, tx_id: int) -> List[TransactionCommentOut]:
        if not self.storage.get_transaction(tx_id):
            raise ValueError(f"Transaction {tx_id} not found")
        return [TransactionCommentOut(**c) for c in self.storage.list_transaction_comments(tx_id)]

    def add_transaction_comment(
        self,
        tx_id: int,
        req: TransactionCommentCreate,
    ) -> TransactionCommentOut:
        saved = self.storage.add_transaction_comment(tx_id, req.body, author=req.author)
        return TransactionCommentOut(**saved)

    # --- Firefly III-Inspired Rules & Rule Groups ---

    def _validate_rule_triggers(self, triggers: Sequence[Any]) -> None:
        for trig in triggers:
            op = trig.op if hasattr(trig, "op") else trig.get("op")
            val = trig.value if hasattr(trig, "value") else trig.get("value")
            if op == "regex":
                validate_safe_regex(str(val or ""))

    def list_rule_groups(self) -> List[RuleGroupOut]:
        return [RuleGroupOut(**g) for g in self.storage.list_rule_groups()]

    def create_rule_group(self, req: RuleGroupCreate) -> RuleGroupOut:
        saved = self.storage.create_rule_group(req.name.strip(), priority=req.priority, active=req.active)
        return RuleGroupOut(**saved)

    def list_rules(self, active_only: bool = False) -> List[RuleOut]:
        return [RuleOut(**r) for r in self.storage.list_rules(active_only=active_only)]

    def create_rule(self, req: RuleCreate) -> RuleOut:
        self._validate_rule_triggers(req.triggers)
        saved = self.storage.create_rule(
            {
                "group_id": req.group_id,
                "name": req.name.strip(),
                "priority": req.priority,
                "strict_and": req.strict_and,
                "stop_processing": req.stop_processing,
                "triggers": [t.model_dump() for t in req.triggers],
                "actions": [a.model_dump() for a in req.actions],
                "active": req.active,
            }
        )
        self.storage.log_budget_event("rule_created", f"Created automation rule '{saved['name']}'")
        return RuleOut(**saved)

    def update_rule(self, rule_id: int, req: RuleUpdate) -> Optional[RuleOut]:
        if req.triggers is not None:
            self._validate_rule_triggers(req.triggers)
        updates = req.model_dump(exclude_none=True)
        if "triggers" in updates and req.triggers is not None:
            updates["triggers"] = [t.model_dump() for t in req.triggers]
        if "actions" in updates and req.actions is not None:
            updates["actions"] = [a.model_dump() for a in req.actions]
        updated = self.storage.update_rule(rule_id, updates)
        return RuleOut(**updated) if updated else None

    def delete_rule(self, rule_id: int) -> bool:
        return self.storage.delete_rule(rule_id)

    def test_rule(self, rule_id: int, lookback_days: int = _LOOKBACK_730D) -> RuleTestResult:
        rule = self.storage.get_rule(rule_id)
        if not rule:
            raise ValueError(f"Rule {rule_id} not found")
        txs, _ = self._load_recent_transactions_with_splits(days=lookback_days)
        for t in txs:
            if t.get("tags") and isinstance(t["tags"][0], dict):
                t["tags"] = [tg["name"] for tg in t["tags"]]
        return dry_run_rule(rule, txs, lookback_days=lookback_days)

    def apply_rule(self, rule_id: int, lookback_days: int = _LOOKBACK_730D) -> Dict[str, Any]:
        rule = self.storage.get_rule(rule_id)
        if not rule:
            raise ValueError(f"Rule {rule_id} not found")
        txs, _ = self._load_recent_transactions_with_splits(days=lookback_days)
        for t in txs:
            if t.get("tags") and isinstance(t["tags"][0], dict):
                t["tags"] = [tg["name"] for tg in t["tags"]]
        matches = self.rule_engine.apply_rules_batch(txs, [rule])
        updated_count = 0
        with self.storage.transaction(immediate=True):
            for tx_row, res in matches:
                if res.changes and self._persist_rule_changes_for_tx(tx_row, res.changes):
                    updated_count += 1
        if updated_count > 0:
            self.run_self_updating_sync(reason=f"Applied rule '{rule['name']}' to {updated_count} tx")
        return {
            "rule_id": rule_id,
            "rule_name": rule["name"],
            "scanned_count": len(txs),
            "matched_count": len(matches),
            "updated_count": updated_count,
            "lookback_days": lookback_days,
        }

    # --- Banking Connectors (Plaid Hosted Link BYOK, SimpleFIN, Connections) ---

    def _to_connection_out(self, row: Dict[str, Any]) -> ConnectionOut:
        provider = str(row["provider"])
        hist_days = int(row.get("history_days_requested", 730))
        status = str(row.get("status", "active"))
        refresh_rem = _days_until_iso_timestamp(row.get("token_expires_at"))
        if provider == "schwab" and refresh_rem is not None and refresh_rem <= 0:
            status = "needs_reauth"
        if not self.secrets.is_unlocked() and status == "active":
            status = "paused_locked"
        raw_bf = str(row.get("backfill_status") or "complete").strip().lower()
        bf_status = raw_bf if raw_bf in ("complete", "backfill_pending", "partial") else "complete"
        return ConnectionOut(
            id=int(row["id"]),
            provider=provider,  # type: ignore[arg-type]
            item_id=str(row.get("item_id", "")),
            institution=str(row.get("institution", "")),
            env=str(row.get("env", "production")),
            status=status,  # type: ignore[arg-type]
            token_expires_at=row.get("token_expires_at"),
            refresh_days_remaining=refresh_rem,
            last_synced_at=row.get("last_synced_at"),
            history_days_requested=hist_days,
            needs_relink_for_730d=(provider == "plaid" and hist_days < 730),
            backfill_status=bf_status,  # type: ignore[arg-type]
            imported_start_date=row.get("imported_start_date"),
            imported_end_date=row.get("imported_end_date"),
            last_error=str(row.get("last_error") or ""),
            created_at=str(row.get("created_at", "")),
            updated_at=str(row.get("updated_at", "")),
        )

    def list_connections(self) -> List[ConnectionOut]:
        return [self._to_connection_out(r) for r in self.storage.list_connections()]

    def delete_connection(self, connection_id: int) -> bool:
        existing = self.storage.get_connection(connection_id)
        if not existing:
            return False
        provider = str(existing.get("provider") or "")
        if provider == "plaid":
            # Confirm provider revocation via Plaid /item/remove BEFORE discarding local credentials
            self._require_vault_unlocked()
            creds = self.secrets.get_json(f"conn:{connection_id}")
            if creds and creds.get("access_token"):
                conn_env = str(existing.get("env") or creds.get("env") or "production").strip().lower()
                plaid_client, _ = self._build_plaid_client(env_override=conn_env, creds_override=creds)
                plaid_client.remove_item(str(creds["access_token"]))
        if self.secrets.is_unlocked():
            self.secrets.delete(f"conn:{connection_id}")
        self._cancel_plaid_backfill_retry(connection_id)
        ok = self.storage.delete_connection(connection_id)
        if ok:
            self.storage.log_budget_event(
                "connector_removed",
                f"Disconnected {existing['provider']} ({existing.get('institution', '')})",
            )
        return ok

    def _build_plaid_client(
        self,
        *,
        env_override: Optional[str] = None,
        creds_override: Optional[Dict[str, Any]] = None,
    ) -> Tuple[PlaidClient, str]:
        self._require_vault_unlocked()
        cfg = self.secrets.get_json("plaid:config") or {}
        cid = str((creds_override or {}).get("client_id") or cfg.get("client_id") or "").strip()
        sec = str((creds_override or {}).get("secret") or cfg.get("secret") or "").strip()
        if not cid or not sec:
            raise ConnectorError(
                "Plaid BYOK credentials are not configured. Save your Plaid client_id and secret in Vault & Connectors first."
            )
        raw_env = env_override or (creds_override or {}).get("env") or cfg.get("env") or "production"
        env = str(raw_env).strip().lower()
        return (
            PlaidClient(
                client_id=cid,
                secret=sec,
                env=env,
                transport=self._plaid_transport,
            ),
            env,
        )

    def configure_plaid(self, req: PlaidConfigRequest) -> Dict[str, Any]:
        self._require_vault_unlocked()
        self.secrets.set_json(
            "plaid:config",
            {"client_id": req.client_id.strip(), "secret": req.secret.strip(), "env": req.env},
        )
        self.storage.log_budget_event("connector_config", f"Plaid BYOK credentials sealed ({req.env})")
        return {
            "configured": True,
            "env": req.env,
            "client_id_masked": mask_secret(req.client_id),
        }

    def _is_pending_link_expired(self, pending: Optional[Dict[str, Any]]) -> bool:
        if not pending or not pending.get("link_token"):
            return True
        exp_raw = str(pending.get("expires_at") or "").strip()
        if not exp_raw:
            return False
        try:
            exp_dt = datetime.fromisoformat(exp_raw.replace("Z", "+00:00"))
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) >= exp_dt
        except ValueError:
            return False

    def _load_pending_plaid_link(self) -> Optional[Dict[str, Any]]:
        if not self.secrets.is_unlocked():
            return None
        with self._plaid_bg_lock:
            pending = self._safe_get_secret_json("plaid:pending_link")
            if not pending or not pending.get("link_token"):
                self._plaid_pending_state = None
                return None
            if self._is_pending_link_expired(pending):
                try:
                    self.secrets.delete("plaid:pending_link")
                except (VaultLockedError, VaultNotInitializedError):
                    pass
                self._plaid_pending_state = None
                return None
            self._plaid_pending_state = dict(pending)
            return dict(pending)

    def _clear_pending_plaid_link(self) -> None:
        with self._plaid_bg_lock:
            self._plaid_pending_state = None
            poller_stop = self._plaid_poller_stop_event
            self._plaid_poller_stop_event = None
            if poller_stop is not None:
                poller_stop.set()
            if self.secrets.is_unlocked():
                try:
                    self.secrets.delete("plaid:pending_link")
                except (VaultLockedError, VaultNotInitializedError):
                    pass

    def _resume_pending_plaid_poller_if_any(self) -> None:
        pending = self._load_pending_plaid_link()
        if pending and str(pending.get("status") or "pending") == "pending":
            self._start_plaid_poller(
                link_token=str(pending["link_token"]),
                env=str(pending.get("env") or "production"),
            )

    def _is_connection_stale_for_sync(self, conn: Dict[str, Any], *, stale_seconds: float = 14400.0) -> bool:
        if str(conn.get("backfill_status") or "").lower() == "backfill_pending":
            return True
        last_str = str(conn.get("last_synced_at") or "").strip()
        if not last_str:
            return True
        try:
            dt = datetime.fromisoformat(last_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).total_seconds() >= stale_seconds
        except ValueError:
            return True

    def _schedule_plaid_periodic_sync(self) -> None:
        """Schedule a daemon timer to refresh linked Plaid institutions every 4h while unlocked."""
        if self._plaid_stop_event.is_set() or getattr(self.storage, "_closed", False):
            return
        if not self.secrets.is_unlocked():
            return
        plaid_conns = [c for c in self.storage.list_connections() if str(c.get("provider") or "") == "plaid"]
        if not plaid_conns:
            return
        delay = float(self._plaid_periodic_sync_interval_seconds)
        master_stop = self._plaid_stop_event
        bg_lock = self._plaid_bg_lock
        svc_ref = weakref.ref(self)

        def _run_periodic() -> None:
            with bg_lock:
                svc = svc_ref()
                if svc is not None:
                    svc._plaid_periodic_holder[0] = None
            if master_stop.is_set() or svc is None:
                return
            if getattr(svc.storage, "_closed", False) or not svc.secrets.is_unlocked():
                return
            try:
                svc.sync_plaid()
            except (VaultLockedError, VaultNotInitializedError, sqlite3.ProgrammingError):
                return
            except Exception as exc:
                logger.debug("Periodic Plaid sync deferred: %s", exc)
            finally:
                if not master_stop.is_set() and not getattr(svc.storage, "_closed", False) and svc.secrets.is_unlocked():
                    svc._schedule_plaid_periodic_sync()

        with self._plaid_bg_lock:
            existing = self._plaid_periodic_holder[0]
            if existing is not None:
                existing.cancel()
            timer = threading.Timer(delay, _run_periodic)
            timer.daemon = True
            timer.name = "chronox-plaid-periodic-sync"
            self._plaid_periodic_holder[0] = timer
            timer.start()

    def _resume_plaid_background_syncs_if_any(self) -> None:
        """Resume pending Hosted Link polling, stale/backfill_pending syncs, and periodic 4h sync."""
        if self._plaid_stop_event.is_set() or getattr(self.storage, "_closed", False) or not self.secrets.is_unlocked():
            return
        self._resume_pending_plaid_poller_if_any()
        plaid_conns = [c for c in self.storage.list_connections() if str(c.get("provider") or "") == "plaid"]
        if not plaid_conns:
            return
        for conn in plaid_conns:
            cid = int(conn["id"])
            if self._is_connection_stale_for_sync(conn):
                self._schedule_plaid_backfill_retry(cid, attempt=0, delay_override=0.15)
        self._schedule_plaid_periodic_sync()

    def _start_plaid_poller(self, *, link_token: str, env: str) -> None:
        with self._plaid_bg_lock:
            old_stop = self._plaid_poller_stop_event
            if old_stop is not None:
                old_stop.set()
            self._plaid_stop_event.clear()
            stop_ev = threading.Event()
            self._plaid_poller_stop_event = stop_ev
            deadline = time.monotonic() + self._plaid_poll_max_seconds
            poll_interval = self._plaid_poll_interval_seconds
            master_stop = self._plaid_stop_event
            svc_ref = weakref.ref(self)

            def _poll_loop() -> None:
                while not stop_ev.is_set() and not master_stop.is_set():
                    if stop_ev.wait(poll_interval):
                        return
                    if master_stop.is_set():
                        return
                    svc = svc_ref()
                    if svc is None:
                        return
                    if getattr(svc.storage, "_closed", False) or not svc.secrets.is_unlocked():
                        return
                    if time.monotonic() >= deadline:
                        with svc._plaid_bg_lock:
                            svc._plaid_last_status = "timeout"
                            svc._plaid_last_error = (
                                "Bank sign-in session timed out waiting for completion. "
                                "Click Check & Sync Bank if you finished signing in."
                            )
                        return
                    try:
                        res = svc.check_pending_plaid_link(link_token=link_token)
                        st = str(res.get("status") or "pending")
                        if st in ("completed", "exited", "expired", "idle"):
                            return
                    except (VaultLockedError, VaultNotInitializedError):
                        return
                    except sqlite3.ProgrammingError:
                        return
                    except Exception as exc:
                        logger.debug("Plaid background poll retry after error (%s): %s", env, exc)
                        with svc._plaid_bg_lock:
                            svc._plaid_last_error = str(exc)[:240]
                    del svc

            t = threading.Thread(
                target=_poll_loop,
                name="chronox-plaid-link-poller",
                daemon=True,
            )
            self._plaid_poller_thread = t
            t.start()

    def _schedule_plaid_backfill_retry(
        self,
        connection_id: int,
        *,
        attempt: int = 0,
        delay_override: Optional[float] = None,
    ) -> None:
        """Schedule non-blocking follow-up Plaid sync retry for `NOT_READY` / `backfill_pending`."""
        if delay_override is None and attempt >= len(self._plaid_retry_delays):
            return
        if self._plaid_stop_event.is_set() or getattr(self.storage, "_closed", False):
            return
        if not self.secrets.is_unlocked():
            return
        delay = (
            max(0.01, float(delay_override))
            if delay_override is not None
            else float(self._plaid_retry_delays[attempt])
        )
        master_stop = self._plaid_stop_event
        bg_lock = self._plaid_bg_lock
        retry_timers = self._plaid_retry_timers
        svc_ref = weakref.ref(self)

        def _run_retry() -> None:
            with bg_lock:
                retry_timers.pop(connection_id, None)
            if master_stop.is_set():
                return
            svc = svc_ref()
            if svc is None:
                return
            if getattr(svc.storage, "_closed", False) or not svc.secrets.is_unlocked():
                return
            try:
                svc.sync_plaid(connection_id=connection_id, _from_retry_attempt=attempt)
            except (VaultLockedError, VaultNotInitializedError, sqlite3.ProgrammingError):
                return
            except Exception as exc:
                logger.debug(
                    "Plaid backfill retry #%d for connection %s deferred: %s",
                    attempt + 1,
                    connection_id,
                    exc,
                )
                if attempt + 1 < len(svc._plaid_retry_delays):
                    svc._schedule_plaid_backfill_retry(connection_id, attempt=attempt + 1)

        with self._plaid_bg_lock:
            existing_timer = self._plaid_retry_timers.pop(connection_id, None)
            if existing_timer is not None:
                existing_timer.cancel()
            timer = threading.Timer(delay, _run_retry)
            timer.daemon = True
            timer.name = f"chronox-plaid-retry-{connection_id}-{attempt}"
            self._plaid_retry_timers[connection_id] = timer
            timer.start()

    def _cancel_plaid_backfill_retry(self, connection_id: int) -> None:
        with self._plaid_bg_lock:
            timer = self._plaid_retry_timers.pop(connection_id, None)
        if timer is not None:
            timer.cancel()

    def get_plaid_config(self) -> Dict[str, Any]:
        """Return safe Plaid configuration & pending Hosted Link status (never returns secrets)."""
        cfg = self._safe_get_secret_json("plaid:config") or {}
        cid = str(cfg.get("client_id") or "").strip()
        sec = str(cfg.get("secret") or "").strip()
        env = str(cfg.get("env") or "production").strip().lower()
        pending = self._load_pending_plaid_link()
        pending_active = bool(pending and str(pending.get("status") or "pending") == "pending")
        pending_url = str(pending.get("hosted_link_url") or "") if pending_active else ""
        with self._plaid_bg_lock:
            pending_status = "pending" if pending_active else self._plaid_last_status
            last_error = str((pending or {}).get("last_error") or self._plaid_last_error or "")
        connected_count = sum(
            1
            for c in self.storage.list_connections()
            if str(c.get("provider") or "") == "plaid"
        )
        return {
            "configured": bool(cid and sec),
            "env": env,
            "client_id_masked": mask_secret(cid) if cid else "",
            "pending_link_active": pending_active,
            "pending_hosted_link_url": pending_url,
            "pending_status": pending_status,
            "last_error": last_error,
            "connected_count": connected_count,
        }

    def create_plaid_link_token(
        self,
        req: PlaidLinkTokenRequest,
        *,
        callback_base_url: Optional[str] = None,
    ) -> PlaidLinkTokenResponse:
        self._require_vault_unlocked()
        if req.client_id and req.secret:
            existing_cfg = self._safe_get_secret_json("plaid:config") or {}
            target_env = str(req.env or existing_cfg.get("env") or "production").strip().lower()
            if target_env not in ("sandbox", "production"):
                target_env = "production"
            self.configure_plaid(
                PlaidConfigRequest(
                    client_id=req.client_id,
                    secret=req.secret,
                    env=target_env,  # type: ignore[arg-type]
                )
            )
        elif req.env:
            existing_cfg = self._safe_get_secret_json("plaid:config") or {}
            if existing_cfg.get("client_id") and existing_cfg.get("secret"):
                self.configure_plaid(
                    PlaidConfigRequest(
                        client_id=str(existing_cfg["client_id"]),
                        secret=str(existing_cfg["secret"]),
                        env=req.env,
                    )
                )

        client, env = self._build_plaid_client()
        state_nonce = secrets.token_urlsafe(24)
        base_cb = (callback_base_url or self._plaid_callback_base_url or "").strip().rstrip("/")

        def _compute_redirect(target_env_val: str) -> Optional[str]:
            explicit = (req.completion_redirect_uri or "").strip() or None
            if explicit:
                return explicit
            if base_cb and (
                target_env_val == "sandbox"
                or base_cb.lower().startswith("https://")
                or base_cb.lower().startswith("chronox://")
            ):
                return f"{base_cb}/api/v1/plaid/hosted-complete?state={state_nonce}"
            return None

        effective_redirect = _compute_redirect(env)
        try:
            res = client.create_link_token(
                days_requested=req.days_requested,
                products=tuple(req.products or ["transactions"]),
                optional_products=tuple(req.optional_products or ["liabilities", "investments"]),
                completion_redirect_uri=effective_redirect,
                url_lifetime_seconds=3600,
            )
        except (ConnectorAuthError, ConnectorError) as exc:
            if "INVALID_API_KEYS" not in str(exc).upper():
                raise
            alt_env: Literal["sandbox", "production"] = "sandbox" if env == "production" else "production"
            alt_client, _ = self._build_plaid_client(env_override=alt_env)
            res = alt_client.create_link_token(
                days_requested=req.days_requested,
                products=tuple(req.products or ["transactions"]),
                optional_products=tuple(req.optional_products or ["liabilities", "investments"]),
                completion_redirect_uri=_compute_redirect(alt_env),
                url_lifetime_seconds=3600,
            )
            env = alt_env
            saved_cfg = self._safe_get_secret_json("plaid:config") or {}
            if saved_cfg.get("client_id") and saved_cfg.get("secret"):
                self.configure_plaid(
                    PlaidConfigRequest(
                        client_id=str(saved_cfg["client_id"]),
                        secret=str(saved_cfg["secret"]),
                        env=alt_env,
                    )
                )
        default_exp = (
            (datetime.now(timezone.utc) + timedelta(hours=6))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        pending_record = {
            "link_token": res.link_token,
            "hosted_link_url": res.hosted_link_url or "",
            "state_nonce": state_nonce,
            "env": env,
            "status": "pending",
            "created_at": utc_now_iso(),
            "expires_at": res.expiration or default_exp,
            "exchanged_tokens": [],
            "last_error": "",
        }
        self.secrets.set_json("plaid:pending_link", pending_record)
        with self._plaid_bg_lock:
            self._plaid_pending_state = dict(pending_record)
            self._plaid_last_status = "pending"
            self._plaid_last_error = ""
        self._start_plaid_poller(link_token=res.link_token, env=env)
        return PlaidLinkTokenResponse(
            link_token=res.link_token,
            hosted_link_url=res.hosted_link_url,
            expiration=res.expiration,
            days_requested=res.days_requested,
            state_nonce=state_nonce,
            pending_status="pending",
            env=env,
        )

    def poll_plaid_link(self, req: PlaidPollLinkRequest) -> Dict[str, Any]:
        client, _env = self._build_plaid_client()
        poll = client.poll_link_token(req.link_token)
        return {
            "status": poll.status,
            "public_token": poll.public_token,
            "institution_id": poll.institution_id,
            "institution_name": poll.institution_name,
            "institution": poll.institution_name or "Plaid Institution",
            "accounts": poll.accounts,
            "items": poll.items,
            "error_code": poll.error_code,
            "error_message": poll.error_message,
        }

    def _seed_accounts_from_plaid_link_metadata(
        self,
        *,
        connection_id: int,
        institution: str,
        raw_accounts: Sequence[Dict[str, Any]],
    ) -> int:
        """Seed initial local accounts from Plaid Link metadata so accounts appear even if balance fetch is NOT_READY."""
        seeded = 0
        for raw in raw_accounts or ():
            if not isinstance(raw, dict):
                continue
            ext_id = str(raw.get("account_id") or raw.get("id") or "").strip()
            name = str(raw.get("name") or raw.get("official_name") or "").strip()
            if not name and not ext_id:
                continue
            raw_type = str(raw.get("type") or "").strip().lower()
            raw_sub = str(raw.get("subtype") or "").strip().lower()
            acct_type, is_liquid = _map_plaid_account_type(raw_type, raw_sub)
            norm = NormalizedAccount(
                external_account_id=ext_id,
                name=(name or "Linked Account")[:80],
                mask=str(raw.get("mask") or "")[:8],
                account_type=acct_type,
                subtype=raw_sub,
                institution=(institution or "Plaid Institution")[:80],
                balance_cents=0,
                available_cents=None,
                limit_cents=None,
                currency="USD",
                is_liquid=is_liquid,
                provider="plaid",
            )
            self._ensure_local_account_for_normalized(norm, connection_id=connection_id)
            seeded += 1
        return seeded

    def exchange_plaid_token(
        self,
        req: PlaidExchangeRequest,
        *,
        auto_sync: bool = False,
        link_accounts: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> ConnectionOut:
        client, env = self._build_plaid_client()
        cfg = self.secrets.get_json("plaid:config") or {}
        ex = client.exchange_public_token(req.public_token)
        inst_name = req.institution.strip() or "Plaid Institution"
        row = self.storage.upsert_connection(
            {
                "provider": "plaid",
                "item_id": ex.item_id,
                "institution": inst_name,
                "env": env,
                "sync_cursor": "",
                "status": "active",
                "history_days_requested": 730,
                "backfill_status": "backfill_pending",
                "last_error": "",
            }
        )
        cid = int(row["id"])
        self.secrets.set_json(
            f"conn:{cid}",
            {
                "access_token": ex.access_token,
                "item_id": ex.item_id,
                "env": env,
                "client_id": str(cfg.get("client_id") or ""),
                "secret": str(cfg.get("secret") or ""),
            },
        )
        if link_accounts:
            self._seed_accounts_from_plaid_link_metadata(
                connection_id=cid,
                institution=inst_name,
                raw_accounts=link_accounts,
            )
        self.storage.log_budget_event("connector_added", f"Connected Plaid institution '{row['institution']}' (730d requested)")
        if auto_sync:
            try:
                self.sync_plaid(connection_id=cid)
            except ConnectorError as exc:
                logger.warning("Initial post-exchange Plaid sync deferred for connection %s: %s", cid, exc)
                self._schedule_plaid_backfill_retry(cid, attempt=0)
            refreshed = self.storage.get_connection(cid)
            if refreshed:
                row = refreshed
        return self._to_connection_out(row)

    @staticmethod
    def _is_plaid_terminal_token_error(exc: Exception) -> bool:
        if isinstance(exc, ConnectorAuthError):
            return True
        upper_msg = str(exc).upper()
        return any(
            code in upper_msg
            for code in (
                "PUBLIC_TOKEN_EXPIRED",
                "INVALID_PUBLIC_TOKEN",
                "LINK_TOKEN_EXPIRED",
                "INVALID_LINK_TOKEN",
            )
        )

    def check_pending_plaid_link(
        self,
        link_token: Optional[str] = None,
        *,
        public_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Poll the persisted pending Plaid Hosted Link session, exchange ALL newly linked
        items idempotently, seed accounts from link metadata if balances haven't settled,
        and trigger immediate sync + non-blocking backfill retries.

        `_plaid_bg_lock` is held ONLY for short in-memory state reads/commits — never
        across network I/O — so `get_plaid_config()` and `lock_vault()` never block.
        """
        self._require_vault_unlocked()
        with self._plaid_bg_lock:
            pending = self._load_pending_plaid_link()
            target_lt = (link_token or "").strip() or str((pending or {}).get("link_token") or "").strip()
            if target_lt and target_lt in self._plaid_completed_link_tokens:
                return dict(self._plaid_completed_link_tokens[target_lt])
            if self._plaid_check_in_flight:
                return {
                    "status": "pending",
                    "pending_link_active": True,
                    "in_flight": True,
                    "link_token": target_lt,
                    "hosted_link_url": str((pending or {}).get("hosted_link_url") or ""),
                    "exchanged_count": 0,
                }
            if not (public_token and public_token.strip()) and not target_lt:
                last_status = self._plaid_last_status
                last_error = self._plaid_last_error
                plaid_conns = [
                    c.model_dump()
                    for c in self.list_connections()
                    if c.provider == "plaid"
                ]
                return {
                    "status": "idle",
                    "pending_link_active": False,
                    "exchanged_count": 0,
                    "connections": plaid_conns,
                    "last_status": last_status,
                    "last_error": last_error,
                }
            already_exchanged = set((pending or {}).get("exchanged_tokens") or [])
            pending_env = str((pending or {}).get("env") or "").strip().lower() or None
            hosted_url_snap = str((pending or {}).get("hosted_link_url") or "")
            nonce_snap = str((pending or {}).get("state_nonce") or "").strip()
            self._plaid_check_in_flight = True

        try:
            items_to_exchange: List[Dict[str, Any]] = []
            poll_status = "idle"
            poll_error_code = ""
            poll_error_msg = ""

            if public_token and public_token.strip():
                poll_status = "completed"
                items_to_exchange.append(
                    {
                        "public_token": public_token.strip(),
                        "institution_id": "",
                        "institution_name": "Plaid Institution",
                        "accounts": [],
                    }
                )
            else:
                client, _env = self._build_plaid_client(env_override=pending_env)
                try:
                    poll = client.poll_link_token(target_lt)
                except (ConnectorAuthError, ConnectorError) as exc:
                    if self._is_plaid_terminal_token_error(exc):
                        expired_msg = "Bank sign-in expired — click Connect Bank to try again."
                        with self._plaid_bg_lock:
                            self._plaid_last_status = "expired"
                            self._plaid_last_error = expired_msg
                        self._clear_pending_plaid_link()
                        return {
                            "status": "exited",
                            "pending_link_active": False,
                            "error_code": "TOKEN_EXPIRED",
                            "error_message": expired_msg,
                            "exchanged_count": 0,
                        }
                    raise
                poll_status = poll.status
                poll_error_code = poll.error_code
                poll_error_msg = poll.error_message
                if poll.status == "completed":
                    if poll.items:
                        items_to_exchange = list(poll.items)
                    elif poll.public_token:
                        items_to_exchange = [
                            {
                                "public_token": poll.public_token,
                                "institution_id": poll.institution_id,
                                "institution_name": poll.institution_name,
                                "accounts": list(poll.accounts),
                            }
                        ]

            if poll_status == "pending":
                return {
                    "status": "pending",
                    "pending_link_active": True,
                    "link_token": target_lt,
                    "hosted_link_url": hosted_url_snap,
                    "exchanged_count": 0,
                }

            if poll_status == "exited":
                exit_msg = poll_error_msg or "Bank sign-in was closed before completion."
                with self._plaid_bg_lock:
                    self._plaid_last_status = "exited"
                    self._plaid_last_error = exit_msg
                self._clear_pending_plaid_link()
                return {
                    "status": "exited",
                    "pending_link_active": False,
                    "error_code": poll_error_code,
                    "error_message": exit_msg,
                    "exchanged_count": 0,
                }

            connections_out: List[Dict[str, Any]] = []
            sync_imported = 0
            sync_modified = 0
            sync_dups = 0
            any_backfill_pending = False
            sync_errors: List[str] = []

            for item in items_to_exchange:
                ptok = str(item.get("public_token") or "").strip()
                if not ptok or ptok in already_exchanged:
                    continue
                inst = str(item.get("institution_name") or "").strip() or "Plaid Institution"
                link_accts = item.get("accounts") if isinstance(item.get("accounts"), list) else []
                try:
                    conn_out = self.exchange_plaid_token(
                        PlaidExchangeRequest(public_token=ptok, institution=inst),
                        auto_sync=False,
                        link_accounts=link_accts,
                    )
                except (ConnectorAuthError, ConnectorError) as exc:
                    if self._is_plaid_terminal_token_error(exc):
                        expired_msg = "Bank sign-in expired — click Connect Bank to try again."
                        with self._plaid_bg_lock:
                            self._plaid_last_status = "expired"
                            self._plaid_last_error = expired_msg
                        self._clear_pending_plaid_link()
                        return {
                            "status": "exited",
                            "pending_link_active": False,
                            "error_code": "TOKEN_EXPIRED",
                            "error_message": expired_msg,
                            "exchanged_count": len(connections_out),
                        }
                    raise
                already_exchanged.add(ptok)
                with self._plaid_bg_lock:
                    if self._plaid_pending_state is not None and self.secrets.is_unlocked():
                        self._plaid_pending_state["exchanged_tokens"] = sorted(already_exchanged)
                        self.secrets.set_json("plaid:pending_link", dict(self._plaid_pending_state))

                try:
                    sync_res = self.sync_plaid(connection_id=conn_out.id)
                    sync_imported += int(sync_res.get("imported_count", 0))
                    sync_modified += int(sync_res.get("modified_count", 0))
                    sync_dups += int(sync_res.get("duplicate_count", 0))
                    if sync_res.get("backfill_status") == "backfill_pending":
                        any_backfill_pending = True
                    refreshed = self.storage.get_connection(conn_out.id)
                    if refreshed:
                        conn_out = self._to_connection_out(refreshed)
                except ConnectorError as exc:
                    sync_errors.append(str(exc))
                    any_backfill_pending = True
                    self._schedule_plaid_backfill_retry(conn_out.id, attempt=0)

                connections_out.append(conn_out.model_dump())

            summary = {
                "status": "completed",
                "pending_link_active": False,
                "exchanged_count": len(connections_out),
                "connections": connections_out,
                "imported_count": sync_imported,
                "modified_count": sync_modified,
                "duplicate_count": sync_dups,
                "backfill_status": "backfill_pending" if any_backfill_pending else "complete",
                "sync_errors": sync_errors,
            }
            with self._plaid_bg_lock:
                if nonce_snap:
                    if len(self._plaid_completed_nonces) >= 16:
                        self._plaid_completed_nonces.clear()
                    self._plaid_completed_nonces[nonce_snap] = dict(summary)
                if target_lt:
                    if len(self._plaid_completed_link_tokens) >= 16:
                        self._plaid_completed_link_tokens.clear()
                    self._plaid_completed_link_tokens[target_lt] = dict(summary)
                self._plaid_last_status = "completed"
                self._plaid_last_error = "; ".join(sync_errors)[:240]
            self._clear_pending_plaid_link()
            self._schedule_plaid_periodic_sync()

            return summary
        finally:
            with self._plaid_bg_lock:
                self._plaid_check_in_flight = False

    def handle_plaid_hosted_complete(
        self,
        *,
        state: str,
        public_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Validate one-time Hosted Link `state_nonce` and trigger immediate exchange & sync."""
        self._require_vault_unlocked()
        cleaned_state = (state or "").strip()
        if not cleaned_state:
            raise ConnectorAuthError(
                "Invalid or expired Plaid Hosted Link callback state.",
                provider="plaid",
            )
        pending = self._load_pending_plaid_link()
        expected = str((pending or {}).get("state_nonce") or "").strip()
        if expected and secrets.compare_digest(cleaned_state, expected):
            return self.check_pending_plaid_link(
                link_token=str(pending["link_token"]),
                public_token=public_token,
            )
        # If background poller already completed this Hosted Link session moments before browser redirect
        with self._plaid_bg_lock:
            for done_nonce, done_summary in list(self._plaid_completed_nonces.items()):
                if secrets.compare_digest(cleaned_state, done_nonce):
                    return dict(done_summary)
        raise ConnectorAuthError(
            "Invalid or expired Plaid Hosted Link callback state.",
            provider="plaid",
        )

    def _ensure_local_account_for_normalized(
        self,
        acct: NormalizedAccount,
        *,
        connection_id: Optional[int] = None,
    ) -> int:
        existing_accounts = self.storage.list_accounts()
        ext_id = (acct.external_account_id or "").strip()
        inst_low = (acct.institution or "").strip().lower()
        name_low = (acct.name or "").strip().lower()
        acct_type = (
            acct.account_type
            if acct.account_type in ("checking", "savings", "credit", "cash", "trading")
            else "checking"
        )
        curr = (acct.currency or "USD").strip().upper()[:8] or "USD"

        matched: Optional[Dict[str, Any]] = None
        # 1. Exact provider external_account_id match (scoped to connection_id or institution)
        if ext_id:
            for local in existing_accounts:
                loc_ext = str(local.get("external_id") or "").strip()
                if not loc_ext or loc_ext != ext_id:
                    continue
                loc_cid = local.get("connection_id")
                if connection_id is not None and loc_cid is not None and int(loc_cid) == int(connection_id):
                    matched = local
                    break
                if loc_cid is None and local.get("institution", "").strip().lower() == inst_low:
                    matched = local
                    break

        # 2. Fallback match ONLY for legacy/unlinked rows that do not yet have an external_id
        if matched is None:
            for local in existing_accounts:
                if str(local.get("external_id") or "").strip():
                    # Never collapse two distinct provider accounts that happen to share a display name!
                    continue
                if (
                    local["name"].strip().lower() == name_low
                    and local.get("institution", "").strip().lower() == inst_low
                    and local.get("account_type") == acct_type
                ):
                    matched = local
                    break

        if matched is not None:
            self.storage.set_account_balance(
                int(matched["id"]),
                acct.balance_cents,
                available_cents=acct.available_cents,
                credit_limit_cents=acct.credit_limit_cents,
                currency=curr,
                mask=acct.mask,
                connection_id=connection_id,
                external_id=ext_id or None,
            )
            return int(matched["id"])

        created = self.storage.create_account(
            name=acct.name[:80],
            account_type=acct_type,
            balance_cents=acct.balance_cents,
            institution=acct.institution[:80],
            is_liquid=acct.is_liquid,
            currency=curr,
            available_cents=acct.available_cents,
            credit_limit_cents=acct.credit_limit_cents,
            mask=acct.mask,
            connection_id=connection_id,
            external_id=ext_id,
        )
        return int(created["id"])

    def _ingest_normalized_transactions(
        self,
        *,
        acct_map: Dict[str, int],
        transactions: Sequence[NormalizedTransaction],
        source_tag: str,
    ) -> Tuple[int, int]:
        default_acct_id = self.storage.get_default_account_id()
        tx_indexes: Dict[int, List[Tuple[date, str, int, str, str]]] = {}
        imported = 0
        duplicates = 0
        for ntx in transactions:
            if ntx.pending:
                continue
            acct_id = acct_map.get(ntx.external_account_id, default_acct_id)
            if acct_id not in tx_indexes:
                tx_indexes[acct_id] = self._load_account_tx_index(acct_id)
            norm = normalize_merchant(ntx.merchant)
            if self._matches_cross_source_duplicate(
                tx_indexes[acct_id],
                ntx.tx_date,
                ntx.tx_type,
                ntx.amount_cents,
                norm,
                source_tag=source_tag,
            ):
                duplicates += 1
                continue
            created = self.add_transaction_raw(
                account_id=acct_id,
                tx_type=ntx.tx_type,
                amount_cents=ntx.amount_cents,
                merchant=ntx.merchant,
                memo=ntx.memo,
                subcategory=ntx.subcategory,
                tx_date=ntx.tx_date,
                source=source_tag,
                deduplicate=True,
                external_id=ntx.external_id,
                transfer_direction=ntx.transfer_direction,
                adjust_balance=False,
                run_auto_sync=False,
                apply_rules=True,
            )
            if created is None:
                duplicates += 1
            else:
                imported += 1
                try:
                    tx_indexes[acct_id].append(
                        (date.fromisoformat(ntx.tx_date), ntx.tx_type, ntx.amount_cents, norm, source_tag)
                    )
                except ValueError:
                    pass
        return imported, duplicates

    def _apply_modified_transactions(
        self,
        *,
        acct_map: Dict[str, int],
        modified: Sequence[NormalizedTransaction],
        source_tag: str,
    ) -> Tuple[int, int]:
        """Apply provider `modified` deltas in place when `external_id` exists, or insert if unseen."""
        updated_count = 0
        inserted_count = 0
        unseen: List[NormalizedTransaction] = []
        for ntx in modified:
            if ntx.pending:
                if ntx.external_id:
                    self.storage.delete_provider_transactions(
                        external_ids=[ntx.external_id],
                        account_ids=list(acct_map.values()) or None,
                    )
                continue
            existing = (
                self.storage.find_provider_transaction(
                    external_id=ntx.external_id,
                    account_id=acct_map.get(ntx.external_account_id),
                )
                if ntx.external_id
                else None
            )
            if existing is None:
                unseen.append(ntx)
                continue
            tx_id = int(existing["id"])
            acct_id = acct_map.get(ntx.external_account_id, int(existing["account_id"]))
            norm = normalize_merchant(ntx.merchant)
            fp = compute_tx_fingerprint(
                acct_id,
                ntx.tx_date,
                ntx.tx_type,
                ntx.amount_cents,
                norm,
                ntx.memo,
                external_id=ntx.external_id,
            )
            upd: Dict[str, Any] = {
                "account_id": acct_id,
                "tx_type": ntx.tx_type,
                "amount_cents": ntx.amount_cents,
                "merchant": ntx.merchant.strip(),
                "normalized_merchant": norm,
                "memo": ntx.memo.strip(),
                "tx_date": ntx.tx_date,
                "fingerprint": fp,
                "transfer_direction": ntx.transfer_direction,
            }
            if ntx.subcategory:
                upd["subcategory"] = ntx.subcategory
            after = self.storage.update_transaction(tx_id, upd)
            if after:
                self._apply_active_rules_to_tx(after)
                updated_count += 1
        if unseen:
            ins, _ = self._ingest_normalized_transactions(
                acct_map=acct_map,
                transactions=unseen,
                source_tag=source_tag,
            )
            inserted_count += ins
        return updated_count, inserted_count

    @staticmethod
    def _merge_tradeline_payload(
        existing: Dict[str, Any],
        incoming: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Safely merge an incoming synced tradeline into an existing `credit_lines` row without
        clobbering user-configured APRs, promo settings, autopay, or custom closing/due days
        when the provider omitted those fields.
        """
        merged: Dict[str, Any] = {
            "balance_cents": max(0, int(incoming.get("balance_cents", existing.get("balance_cents", 0)))),
            "is_overdue": bool(incoming.get("is_overdue", existing.get("is_overdue", False))),
        }
        inc_limit = int(incoming.get("credit_limit_cents") or 0)
        if inc_limit > 0 or int(existing.get("credit_limit_cents") or 0) == 0:
            merged["credit_limit_cents"] = inc_limit
        inc_min = int(incoming.get("min_payment_cents") or 0)
        if inc_min > 0 or int(existing.get("min_payment_cents") or 0) == 0:
            merged["min_payment_cents"] = inc_min

        inc_apr = int(incoming.get("apr_bps") or 0)
        if inc_apr > 0:
            merged["apr_bps"] = inc_apr
        elif int(existing.get("apr_bps") or 0) == 0 and incoming.get("apr_provided"):
            merged["apr_bps"] = 0

        if incoming.get("closing_day_provided") is True:
            merged["statement_closing_day"] = int(incoming["statement_closing_day"])
        if incoming.get("due_day_provided") is True:
            merged["payment_due_day"] = int(incoming["payment_due_day"])

        for opt_k in ("issuer", "opened_date", "statement_balance_cents", "next_due_date", "external_account_id", "source"):
            val = incoming.get(opt_k)
            if val is not None and str(val).strip() != "":
                merged[opt_k] = val
        if incoming.get("is_authorized_user") is not None:
            merged["is_authorized_user"] = bool(incoming["is_authorized_user"])
        return merged

    def sync_plaid(
        self,
        connection_id: Optional[int] = None,
        *,
        _from_retry_attempt: Optional[int] = None,
    ) -> Dict[str, Any]:
        self._require_vault_unlocked()
        all_conns = self.storage.list_connections()
        targets = [
            c
            for c in all_conns
            if c["provider"] == "plaid" and (connection_id is None or int(c["id"]) == int(connection_id))
        ]
        if not targets:
            raise ConnectorError("No linked Plaid connections found.")

        total_imported = 0
        total_modified = 0
        total_dups = 0
        total_removed = 0
        liabilities_synced = 0
        holdings_synced = 0
        backfill_statuses: List[str] = []
        pending_retry_cids: List[int] = []
        complete_cids: List[int] = []
        overall_min_date: Optional[str] = None
        overall_max_date: Optional[str] = None

        for conn in targets:
            cid = int(conn["id"])
            creds = self.secrets.get_json(f"conn:{cid}")
            if not creds or not creds.get("access_token"):
                self.storage.upsert_connection(
                    {
                        "id": cid,
                        "provider": "plaid",
                        "status": "needs_reauth",
                        "last_error": "Missing sealed Plaid access_token; reconnect required.",
                    }
                )
                continue
            conn_env = str(conn.get("env") or creds.get("env") or "production").strip().lower()
            client, _ = self._build_plaid_client(env_override=conn_env, creds_override=creds)
            access_token = str(creds["access_token"])
            cursor = str(conn.get("sync_cursor") or "") or None
            institution = str(conn.get("institution") or "Plaid")

            try:
                sync_res = client.sync_transactions(access_token, cursor=cursor, institution=institution)
            except ConnectorAuthError as exc:
                self.storage.upsert_connection(
                    {
                        "id": cid,
                        "provider": "plaid",
                        "status": "needs_reauth",
                        "last_error": str(exc)[:240],
                    }
                )
                raise

            try:
                bal_accounts = client.get_accounts_balances(access_token, institution=institution)
            except ConnectorAuthError as exc:
                self.storage.upsert_connection(
                    {
                        "id": cid,
                        "provider": "plaid",
                        "status": "needs_reauth",
                        "last_error": str(exc)[:240],
                    }
                )
                raise
            except ConnectorError:
                bal_accounts = sync_res.accounts

            # Optional Plaid Liabilities & Investments fetched BEFORE committing cursor atomically
            liabs_payloads: Optional[List[Dict[str, Any]]] = None
            try:
                liabs = client.get_liabilities(access_token, institution=institution)
                liabs_payloads = self.plaid_liabilities_adapter.to_credit_line_payloads(liabs)
            except ConnectorError:
                liabs_payloads = None

            inv_holdings: Optional[List[NormalizedHolding]] = None
            try:
                inv_holdings = client.get_investments_holdings(access_token)
            except ConnectorError:
                inv_holdings = None

            # Compute imported date window & honest backfill status
            seen_dates = [
                ntx.tx_date
                for ntx in list(sync_res.added) + list(sync_res.modified)
                if not ntx.pending and ntx.tx_date
            ]
            prev_min = conn.get("imported_start_date")
            prev_max = conn.get("imported_end_date")
            if prev_min:
                seen_dates.append(str(prev_min))
            if prev_max:
                seen_dates.append(str(prev_max))
            imp_start = min(seen_dates) if seen_dates else None
            imp_end = max(seen_dates) if seen_dates else None
            if imp_start and (overall_min_date is None or imp_start < overall_min_date):
                overall_min_date = imp_start
            if imp_end and (overall_max_date is None or imp_end > overall_max_date):
                overall_max_date = imp_end

            up_status = (sync_res.transactions_update_status or "").strip().upper()
            hist_req = int(conn.get("history_days_requested", 730))
            if up_status == "HISTORICAL_UPDATE_COMPLETE":
                bf_status = "complete"
            elif up_status in ("INITIAL_UPDATE_COMPLETE", "NOT_READY"):
                bf_status = "backfill_pending"
            elif cursor is None and hist_req >= 730:
                if imp_start:
                    try:
                        span_days = (date.today() - date.fromisoformat(imp_start[:10])).days
                    except ValueError:
                        span_days = 0
                    bf_status = "complete" if span_days >= 60 else "backfill_pending"
                else:
                    bf_status = "backfill_pending"
            else:
                bf_status = "complete" if str(conn.get("backfill_status") or "") == "complete" or cursor is not None else "backfill_pending"
            backfill_statuses.append(bf_status)

            # Commit accounts, added/modified/removed transactions, liabilities, holdings, and cursor atomically
            with self.storage.transaction(immediate=True):
                acct_map: Dict[str, int] = {}
                for na in bal_accounts or sync_res.accounts:
                    acct_map[na.external_account_id] = self._ensure_local_account_for_normalized(
                        na, connection_id=cid
                    )

                imp, dups = self._ingest_normalized_transactions(
                    acct_map=acct_map,
                    transactions=list(sync_res.added),
                    source_tag="plaid_sync",
                )
                mod_upd, mod_ins = self._apply_modified_transactions(
                    acct_map=acct_map,
                    modified=list(sync_res.modified),
                    source_tag="plaid_sync",
                )
                rem_deleted = self.storage.delete_provider_transactions(
                    external_ids=sync_res.removed_external_ids,
                    account_ids=list(acct_map.values()) or None,
                )
                total_imported += imp + mod_ins
                total_modified += mod_upd
                total_dups += dups
                total_removed += rem_deleted

                if liabs_payloads is not None:
                    is_sbx = conn_env == "sandbox"
                    existing_lines_list = self.storage.list_credit_lines(
                        active_only=False, include_sandbox=is_sbx
                    )
                    if is_sbx:
                        existing_lines_list = [
                            cl
                            for cl in existing_lines_list
                            if str(cl.get("source_env") or "production").lower() == "sandbox"
                        ]
                    by_ext = {
                        str(cl.get("external_account_id") or "").strip(): cl
                        for cl in existing_lines_list
                        if str(cl.get("external_account_id") or "").strip()
                    }
                    by_name = {cl["name"].strip().lower(): cl for cl in existing_lines_list}
                    for pl in liabs_payloads:
                        clean_pl = {
                            k: v
                            for k, v in pl.items()
                            if k not in ("apr_provided", "closing_day_provided", "due_day_provided")
                        }
                        clean_pl["is_sandbox"] = is_sbx
                        clean_pl["source_env"] = conn_env
                        ext_key = str(pl.get("external_account_id") or "").strip()
                        name_key = str(pl.get("name") or "").strip().lower()
                        target_cl = by_ext.get(ext_key) if ext_key else None
                        if target_cl is None and name_key:
                            target_cl = by_name.get(name_key)
                        if target_cl is not None:
                            merged_pl = self._merge_tradeline_payload(target_cl, pl)
                            self.storage.update_credit_line(int(target_cl["id"]), merged_pl)
                        else:
                            created_cl = self.storage.create_credit_line(clean_pl)
                            if ext_key:
                                by_ext[ext_key] = created_cl
                            if name_key:
                                by_name[name_key] = created_cl
                        liabilities_synced += 1

                if inv_holdings is not None:
                    keep_ids: List[int] = []
                    for nh in inv_holdings:
                        saved_h = self._upsert_normalized_holding(
                            nh,
                            connection_id=cid,
                            account_id=acct_map.get(nh.external_account_id),
                        )
                        keep_ids.append(saved_h.id)
                        holdings_synced += 1
                    self.storage.prune_connection_holdings(cid, keep_ids)

                self.storage.upsert_connection(
                    {
                        "id": cid,
                        "provider": "plaid",
                        "sync_cursor": sync_res.next_cursor or "",
                        "status": "active",
                        "backfill_status": bf_status,
                        "imported_start_date": imp_start,
                        "imported_end_date": imp_end,
                        "last_error": "",
                        "last_synced_at": utc_now_iso(),
                    }
                )
            if bf_status == "backfill_pending":
                pending_retry_cids.append(cid)
            else:
                complete_cids.append(cid)

        for done_cid in complete_cids:
            self._cancel_plaid_backfill_retry(done_cid)
        next_attempt = 0 if _from_retry_attempt is None else (_from_retry_attempt + 1)
        for pending_cid in pending_retry_cids:
            self._schedule_plaid_backfill_retry(pending_cid, attempt=next_attempt)
        self._schedule_plaid_periodic_sync()

        self._backfill_net_worth_snapshots(days=_LOOKBACK_730D)
        sync_summary = self.run_self_updating_sync(reason=f"Plaid synced ({total_imported} new tx)")
        any_pending = "backfill_pending" in backfill_statuses
        range_str = f"{overall_min_date} to {overall_max_date}" if (overall_min_date and overall_max_date) else "no posted rows yet"
        hist_note = (
            f"Initial sync imported ({range_str}); 730-day historical backfill still pending from institution."
            if any_pending
            else f"Plaid sync complete ({range_str})."
        )
        return {
            "synced_connections": len(targets),
            "imported_count": total_imported,
            "modified_count": total_modified,
            "duplicate_count": total_dups,
            "removed_count": total_removed,
            "liabilities_synced": liabilities_synced,
            "holdings_synced": holdings_synced,
            "backfill_status": "backfill_pending" if any_pending else "complete",
            "imported_start_date": overall_min_date,
            "imported_end_date": overall_max_date,
            "history_note": hist_note,
            "sync": sync_summary,
        }

    def claim_simplefin(self, req: SimpleFINClaimRequest) -> ConnectionOut:
        self._require_vault_unlocked()
        client = SimpleFINClient(transport=self._simplefin_transport)
        access_url = client.claim_setup_token(req.setup_token)
        item_key = hashlib.sha256(access_url.encode("utf-8")).hexdigest()[:16]
        row = self.storage.upsert_connection(
            {
                "provider": "simplefin",
                "item_id": item_key,
                "institution": req.institution.strip() or "SimpleFIN Bridge",
                "env": "production",
                "status": "active",
                "history_days_requested": 730,
                "last_error": "",
            }
        )
        self.secrets.set_json(f"conn:{row['id']}", {"access_url": access_url})
        self.storage.log_budget_event("connector_added", f"Claimed SimpleFIN Bridge '{row['institution']}'")
        return self._to_connection_out(row)

    def sync_simplefin(self, connection_id: Optional[int] = None, days: int = _LOOKBACK_730D) -> Dict[str, Any]:
        self._require_vault_unlocked()
        client = SimpleFINClient(transport=self._simplefin_transport)
        targets = [
            c
            for c in self.storage.list_connections()
            if c["provider"] == "simplefin" and (connection_id is None or int(c["id"]) == int(connection_id))
        ]
        if not targets:
            raise ConnectorError("No linked SimpleFIN connections found.")

        total_imported = 0
        total_dups = 0
        accounts_synced = 0
        for conn in targets:
            cid = int(conn["id"])
            creds = self.secrets.get_json(f"conn:{cid}")
            if not creds or not creds.get("access_url"):
                self.storage.upsert_connection(
                    {
                        "id": cid,
                        "provider": "simplefin",
                        "status": "needs_reauth",
                        "last_error": "Missing sealed SimpleFIN access URL; reconnect required.",
                    }
                )
                continue
            try:
                res = client.fetch_accounts(str(creds["access_url"]), days=max(1, min(730, int(days))))
            except ConnectorAuthError as exc:
                self.storage.upsert_connection(
                    {
                        "id": cid,
                        "provider": "simplefin",
                        "status": "needs_reauth",
                        "last_error": str(exc)[:240],
                    }
                )
                raise

            seen_dates = [ntx.tx_date for ntx in res.added if not ntx.pending and ntx.tx_date]
            if conn.get("imported_start_date"):
                seen_dates.append(str(conn["imported_start_date"]))
            if conn.get("imported_end_date"):
                seen_dates.append(str(conn["imported_end_date"]))
            imp_start = min(seen_dates) if seen_dates else None
            imp_end = max(seen_dates) if seen_dates else None

            with self.storage.transaction(immediate=True):
                acct_map: Dict[str, int] = {}
                for na in res.accounts:
                    acct_map[na.external_account_id] = self._ensure_local_account_for_normalized(
                        na, connection_id=cid
                    )
                    accounts_synced += 1
                imp, dups = self._ingest_normalized_transactions(
                    acct_map=acct_map,
                    transactions=res.added,
                    source_tag="simplefin_sync",
                )
                total_imported += imp
                total_dups += dups
                self.storage.upsert_connection(
                    {
                        "id": cid,
                        "provider": "simplefin",
                        "status": "active",
                        "backfill_status": "complete",
                        "imported_start_date": imp_start,
                        "imported_end_date": imp_end,
                        "last_error": "",
                        "last_synced_at": utc_now_iso(),
                    }
                )

        self._backfill_net_worth_snapshots(days=_LOOKBACK_730D)
        sync_summary = self.run_self_updating_sync(reason=f"SimpleFIN synced ({total_imported} new tx)")
        return {
            "synced_connections": len(targets),
            "accounts_synced": accounts_synced,
            "imported_count": total_imported,
            "duplicate_count": total_dups,
            "sync": sync_summary,
        }

    # --- Optional Read-Only IMAP Email Scanner ---

    def get_email_config(self) -> EmailConfigOut:
        s = self.storage.get_settings()
        return EmailConfigOut(
            enabled=bool(s.get("email_scanner_enabled", 0)),
            imap_host=str(s.get("email_imap_host", "")),
            imap_port=int(s.get("email_imap_port", 993)),
            username=str(s.get("email_username", "")),
            password_configured=bool(self._safe_get_secret("email:app_password")),
            folder=str(s.get("email_folder", "INBOX")),
            lookback_days=int(s.get("email_lookback_days", 30)),
            read_only_mode=True,
        )

    def update_email_config(self, req: EmailConfigUpdate) -> EmailConfigOut:
        updates: Dict[str, Any] = {}
        if req.enabled is not None:
            updates["email_scanner_enabled"] = int(bool(req.enabled))
        if req.imap_host is not None:
            updates["email_imap_host"] = req.imap_host.strip()
        if req.imap_port is not None:
            updates["email_imap_port"] = int(req.imap_port)
        if req.username is not None:
            updates["email_username"] = req.username.strip()
        if req.folder is not None:
            updates["email_folder"] = req.folder.strip() or "INBOX"
        if req.lookback_days is not None:
            updates["email_lookback_days"] = int(req.lookback_days)
        if updates:
            self.storage.update_settings(updates)
        if req.clear_password:
            self._require_vault_unlocked()
            self.secrets.delete("email:app_password")
        elif req.app_password is not None and req.app_password.strip():
            self._require_vault_unlocked()
            self.secrets.set("email:app_password", req.app_password.strip())
        return self.get_email_config()

    def scan_email(self) -> Dict[str, Any]:
        self._require_vault_unlocked()
        cfg = self.get_email_config()
        if not cfg.enabled:
            raise ConnectorError("Email scanner is disabled. Enable it in Vault & Connectors first.")
        app_password = self.secrets.get("email:app_password").strip()
        if not cfg.imap_host or not cfg.username or not app_password:
            raise ConnectorError("IMAP host, username, and App Password are required before scanning.")

        existing_uids = {item["uid_key"] for item in self.storage.list_email_queue(limit=500)}
        scanner = EmailScanner(
            IMAPConfig(
                host=cfg.imap_host,
                port=cfg.imap_port,
                username=cfg.username,
                app_password=app_password,
                mailbox=cfg.folder or "INBOX",
                lookback_days=cfg.lookback_days,
            ),
            imap_factory=self._imap_factory,
        )
        res = scanner.scan(existing_uid_keys=existing_uids)
        queued_items: List[EmailQueueItem] = []
        for cand in res.candidates:
            saved = self.storage.enqueue_email_item(
                {
                    "uid_key": cand.uid_key,
                    "sender": cand.sender,
                    "subject": cand.subject,
                    "received_at": cand.received_at,
                    "extracted_vendor": cand.extracted_vendor,
                    "amount_cents": cand.amount_cents,
                    "due_date": cand.due_date,
                    "kind": cand.kind,
                }
            )
            if saved:
                queued_items.append(EmailQueueItem(**saved))
        skipped_duplicates = max(0, len(res.candidates) - len(queued_items))
        return {
            "scanned_count": res.scanned_count,
            "scanned_messages": res.scanned_count,
            "matched_count": res.matched_count,
            "queued_count": len(queued_items),
            "skipped_duplicates": skipped_duplicates,
            "read_only_verified": res.read_only_verified,
            "scanned_at": res.scanned_at,
            "warnings": list(res.warnings),
            "items": [i.model_dump() for i in queued_items],
        }

    def list_email_queue(self, status: Optional[str] = None, limit: int = 100) -> List[EmailQueueItem]:
        return [EmailQueueItem(**r) for r in self.storage.list_email_queue(status=status, limit=limit)]

    def approve_email_queue_item(self, item_id: int) -> Dict[str, Any]:
        item = self.storage.get_email_queue_item(item_id)
        if not item:
            raise ValueError(f"Email queue item {item_id} not found")
        if item["status"] != "pending":
            raise ValueError(f"Email queue item {item_id} is already {item['status']}")

        vendor = (item.get("extracted_vendor") or item.get("subject") or "Email Bill").strip()[:100]
        amount_cents = int(item["amount_cents"]) if item.get("amount_cents") else 0
        matched_bill_id: Optional[int] = None
        created_tx: Optional[Dict[str, Any]] = None

        if item.get("kind") == "bill" and amount_cents > 0:
            due = item.get("due_date") or (date.today() + timedelta(days=7)).isoformat()
            bill = self.create_recurring_bill(
                RecurringBillCreate(
                    name=vendor,
                    amount_cents=amount_cents,
                    next_due_date=due,
                    is_subscription=False,
                    is_essential=True,
                )
            )
            matched_bill_id = int(bill["id"])
        elif amount_cents > 0:
            tx_date = (item.get("received_at") or date.today().isoformat())[:10]
            try:
                date.fromisoformat(tx_date)
            except ValueError:
                tx_date = date.today().isoformat()
            tx = self.add_transaction_raw(
                account_id=None,
                tx_type=TransactionType.EXPENSE.value,
                amount_cents=amount_cents,
                merchant=vendor,
                memo=f"Approved email receipt: {item.get('subject', '')[:180]}",
                tx_date=tx_date,
                source="email_receipt",
                deduplicate=True,
                external_id=str(item["uid_key"]),
            )
            if tx:
                created_tx = tx.model_dump()

        updated = self.storage.update_email_queue_status(item_id, "approved", matched_bill_id=matched_bill_id)
        return {
            "approved": True,
            "item": EmailQueueItem(**updated).model_dump() if updated else None,
            "matched_bill_id": matched_bill_id,
            "transaction": created_tx,
        }

    def dismiss_email_queue_item(self, item_id: int) -> Optional[EmailQueueItem]:
        updated = self.storage.update_email_queue_status(item_id, "dismissed")
        return EmailQueueItem(**updated) if updated else None

    # --- Multi-Format Bank Statement Ingestion (CSV, TSV, PDF, OFX/QFX, QIF, JSON/Plaid) ---

    @staticmethod
    def _merchants_semantically_match(norm_a: str, norm_b: str) -> bool:
        if not norm_a or not norm_b:
            return False
        if norm_a == norm_b:
            return True
        if len(norm_a) >= 4 and len(norm_b) >= 4 and (norm_a in norm_b or norm_b in norm_a):
            return True
        toks_a = {t for t in norm_a.split() if len(t) >= 2}
        toks_b = {t for t in norm_b.split() if len(t) >= 2}
        if not toks_a or not toks_b:
            return False
        overlap = len(toks_a & toks_b) / len(toks_a | toks_b)
        return overlap >= 0.5

    def _load_account_tx_index(self, acct_id: int) -> List[Tuple[date, str, int, str, str]]:
        """Snapshot existing account transactions for cross-source (Plaid <-> NFCU CSV/PDF) dedup."""
        rows = self.storage._conn.execute(
            """
            SELECT tx_date, tx_type, amount_cents, normalized_merchant, source
            FROM transactions
            WHERE account_id = ?
            """,
            (acct_id,),
        ).fetchall()
        index: List[Tuple[date, str, int, str, str]] = []
        for r in rows:
            try:
                index.append(
                    (
                        date.fromisoformat(str(r["tx_date"])),
                        str(r["tx_type"]),
                        int(r["amount_cents"]),
                        str(r["normalized_merchant"] or ""),
                        str(r["source"] or ""),
                    )
                )
            except ValueError:
                continue
        return index

    def _matches_cross_source_duplicate(
        self,
        existing_index: List[Tuple[date, str, int, str, str]],
        tx_date_str: str,
        tx_type: str,
        amount_cents: int,
        norm_merchant: str,
        *,
        source_tag: str = "",
        consume: bool = True,
    ) -> bool:
        """
        Detect same transaction across DIFFERENT ingestion sources (e.g., Plaid sync vs CSV/PDF/OFX
        with ±2d posting lag). Never collapses two distinct transactions from the SAME source!
        """
        try:
            cand_dt = date.fromisoformat(tx_date_str)
        except ValueError:
            return False
        for idx, (ex_dt, ex_type, ex_amt, ex_norm, ex_src) in enumerate(existing_index):
            if source_tag and ex_src == source_tag:
                continue
            if ex_amt != amount_cents or ex_type != tx_type:
                continue
            if abs((cand_dt - ex_dt).days) <= 2 and self._merchants_semantically_match(norm_merchant, ex_norm):
                if consume:
                    existing_index.pop(idx)
                return True
        return False

    def _resolve_statement_target_account_id(
        self,
        *,
        explicit_account_id: Optional[int],
        row_hint: str = "",
        doc_hint: str = "",
        doc_type: str = "",
        institution: str = "",
        allow_create: bool = True,
    ) -> int:
        if explicit_account_id is not None:
            return int(explicit_account_id)

        hint = (row_hint or doc_hint or "").strip()
        hint_low = hint.lower()
        inferred_type = (doc_type or "").strip().lower()
        if "saving" in hint_low or "money market" in hint_low or "share" in hint_low:
            inferred_type = "savings"
        elif any(k in hint_low for k in ("credit card", "visa", "mastercard", "amex", "revolving")):
            inferred_type = "credit"
        elif "checking" in hint_low:
            inferred_type = "checking"

        if not hint and not inferred_type:
            return self.storage.get_default_account_id()

        accounts = self.storage.list_accounts()
        # 1. Exact or substring name / mask match
        if hint:
            digits_tail = "".join(ch for ch in hint if ch.isdigit())[-4:]
            for acct in accounts:
                aname = str(acct.get("name") or "").strip().lower()
                amask = str(acct.get("mask") or "").strip()
                if aname == hint_low or (len(hint_low) >= 5 and (hint_low in aname or aname in hint_low)):
                    if not inferred_type or acct.get("account_type") == inferred_type:
                        return int(acct["id"])
                if len(digits_tail) == 4 and amask == digits_tail:
                    return int(acct["id"])

        # 2. Match existing account of the same inferred account_type
        if inferred_type:
            typed_matches = [a for a in accounts if a.get("account_type") == inferred_type]
            if typed_matches:
                return int(typed_matches[0]["id"])
            if allow_create and inferred_type in ("savings", "credit", "cash", "trading"):
                clean_inst = (
                    "Navy Federal"
                    if institution == "navy_federal"
                    else (institution.replace("_", " ").title() if institution and institution not in ("auto", "generic") else "")
                )
                acct_label = (
                    hint[:60]
                    if hint and not hint.isdigit()
                    else (f"{clean_inst} {inferred_type.title()}".strip() or f"{inferred_type.title()} Account")
                )
                created = self.storage.create_account(
                    name=acct_label,
                    account_type=inferred_type,
                    balance_cents=0,
                    institution=clean_inst[:60],
                    is_liquid=(inferred_type != "trading"),
                )
                return int(created["id"])

        return self.storage.get_default_account_id()

    def preview_statement(
        self,
        content: Any,
        *,
        filename: str = "",
        account_id: Optional[int] = None,
        flip_expense_sign: bool = False,
        detect_transfers: bool = True,
        institution: str = "auto",
        skip_pending: bool = True,
    ) -> Dict[str, Any]:
        res = ingest_statement(
            content,
            filename=filename,
            flip_expense_sign=flip_expense_sign,
            detect_transfers=detect_transfers,
            institution=institution,
            skip_pending=skip_pending,
        )
        source_tag = (
            f"{res.detected_institution}_{res.format_detected}_import"
            if res.detected_institution in ("navy_federal", "plaid")
            else f"{res.format_detected}_import"
        )
        default_target_id = self._resolve_statement_target_account_id(
            explicit_account_id=account_id,
            doc_hint=res.detected_account_hint,
            doc_type=res.detected_account_type,
            institution=res.detected_institution,
            allow_create=False,
        )
        existing_fps = {
            r["fingerprint"]
            for r in self.storage._conn.execute("SELECT fingerprint FROM transactions WHERE fingerprint IS NOT NULL").fetchall()
        }
        index_by_acct: Dict[int, List[Tuple[date, str, int, str, str]]] = {}
        dup_count = 0
        cross_source_dups = 0
        sample = []
        for r in res.rows:
            row_acct_id = (
                self._resolve_statement_target_account_id(
                    explicit_account_id=account_id,
                    row_hint=r.account_hint,
                    doc_hint=res.detected_account_hint,
                    doc_type=res.detected_account_type,
                    institution=res.detected_institution,
                    allow_create=False,
                )
                if account_id is None and r.account_hint
                else default_target_id
            )
            if row_acct_id not in index_by_acct:
                index_by_acct[row_acct_id] = self._load_account_tx_index(row_acct_id)
            norm = normalize_merchant(r.merchant)
            fp = compute_tx_fingerprint(
                row_acct_id, r.tx_date, r.tx_type, r.amount_cents, norm, r.memo, external_id=r.external_id
            )
            is_exact_dup = fp in existing_fps
            is_cross_dup = (not is_exact_dup) and self._matches_cross_source_duplicate(
                index_by_acct[row_acct_id],
                r.tx_date,
                r.tx_type,
                r.amount_cents,
                norm,
                source_tag=source_tag,
                consume=True,
            )
            is_dup = is_exact_dup or is_cross_dup
            if is_dup:
                dup_count += 1
            if is_cross_dup:
                cross_source_dups += 1
            if len(sample) < 8:
                sample.append(
                    {
                        "tx_date": r.tx_date,
                        "merchant": r.merchant,
                        "tx_type": r.tx_type,
                        "amount_cents": r.amount_cents,
                        "account_hint": r.account_hint or res.detected_account_hint,
                        "duplicate": is_dup,
                        "cross_source_duplicate": is_cross_dup,
                    }
                )
        target_acct = self.storage.get_account(default_target_id)
        return {
            "format_detected": res.format_detected,
            "detected_institution": res.detected_institution,
            "detected_account_hint": res.detected_account_hint,
            "detected_account_type": res.detected_account_type,
            "target_account_id": default_target_id,
            "target_account_name": str(target_acct["name"]) if target_acct else "",
            "parse_failed": bool(res.parse_failed),
            "total_parsed": len(res.rows),
            "new_count": max(0, len(res.rows) - dup_count),
            "duplicate_count": dup_count,
            "cross_source_duplicates": cross_source_dups,
            "pending_skipped": res.pending_skipped,
            "transfers_detected": res.transfers_detected,
            "detected_balance_cents": res.detected_balance_cents,
            "warnings": res.warnings[:20],
            "sample": sample,
        }

    def import_statement(
        self,
        content: Any,
        *,
        filename: str = "",
        account_id: Optional[int] = None,
        flip_expense_sign: bool = False,
        detect_transfers: bool = True,
        sync_ofx_balance: bool = False,
        sync_balance: Optional[bool] = None,
        institution: str = "auto",
        skip_pending: bool = True,
    ) -> Dict[str, Any]:
        res = ingest_statement(
            content,
            filename=filename,
            flip_expense_sign=flip_expense_sign,
            detect_transfers=detect_transfers,
            institution=institution,
            skip_pending=skip_pending,
        )
        default_acct_id = self._resolve_statement_target_account_id(
            explicit_account_id=account_id,
            doc_hint=res.detected_account_hint,
            doc_type=res.detected_account_type,
            institution=res.detected_institution,
            allow_create=(account_id is None and len(res.rows) > 0),
        )
        should_sync_balance = sync_balance if sync_balance is not None else sync_ofx_balance
        index_by_acct: Dict[int, List[Tuple[date, str, int, str, str]]] = {}
        source_tag = (
            f"{res.detected_institution}_{res.format_detected}_import"
            if res.detected_institution in ("navy_federal", "plaid")
            else f"{res.format_detected}_import"
        )
        imported = 0
        duplicates = 0
        cross_source_dups = 0
        touched_accts: set[int] = set()
        for r in res.rows:
            row_acct_id = (
                self._resolve_statement_target_account_id(
                    explicit_account_id=account_id,
                    row_hint=r.account_hint,
                    doc_hint=res.detected_account_hint,
                    doc_type=res.detected_account_type,
                    institution=res.detected_institution,
                    allow_create=True,
                )
                if account_id is None and r.account_hint
                else default_acct_id
            )
            touched_accts.add(row_acct_id)
            if row_acct_id not in index_by_acct:
                index_by_acct[row_acct_id] = self._load_account_tx_index(row_acct_id)
            norm = normalize_merchant(r.merchant)
            if self._matches_cross_source_duplicate(
                index_by_acct[row_acct_id],
                r.tx_date,
                r.tx_type,
                r.amount_cents,
                norm,
                source_tag=source_tag,
                consume=True,
            ):
                duplicates += 1
                cross_source_dups += 1
                continue
            row_syncs_doc_balance = (
                should_sync_balance
                and res.detected_balance_cents is not None
                and row_acct_id == default_acct_id
                and len(touched_accts) <= 1
            )
            tx = self.add_transaction_raw(
                account_id=row_acct_id,
                tx_type=r.tx_type,
                amount_cents=r.amount_cents,
                merchant=r.merchant,
                memo=r.memo,
                tx_date=r.tx_date,
                source=source_tag,
                deduplicate=True,
                external_id=r.external_id,
                transfer_direction=r.transfer_direction,
                adjust_balance=not row_syncs_doc_balance,
                run_auto_sync=False,
                apply_rules=True,
            )
            if tx is None:
                duplicates += 1
            else:
                imported += 1
                try:
                    index_by_acct[row_acct_id].append(
                        (date.fromisoformat(r.tx_date), r.tx_type, r.amount_cents, norm, source_tag)
                    )
                except ValueError:
                    pass

        if should_sync_balance and res.detected_balance_cents is not None and len(touched_accts) <= 1:
            self.storage.set_account_balance(default_acct_id, res.detected_balance_cents)

        self._backfill_net_worth_snapshots(days=_LOOKBACK_730D)
        inst_label = (
            "Navy Federal"
            if res.detected_institution == "navy_federal"
            else ("Plaid" if res.detected_institution == "plaid" else res.format_detected.upper())
        )
        sync = (
            self.run_self_updating_sync(
                reason=f"Imported {imported} {inst_label} ({res.format_detected.upper()}) transactions"
            )
            if imported > 0 or (should_sync_balance and res.detected_balance_cents is not None)
            else self._compute_budget()
        )
        sync_dict = (
            sync
            if isinstance(sync, dict)
            else {
                "bills_detected": 0,
                "jars_adjusted": 0,
                "price_hikes": [],
                "safe_to_spend_cents": sync.safe_to_spend.safe_to_spend_cents,
                "daily_safe_to_spend_cents": sync.safe_to_spend.daily_safe_to_spend_cents,
                "safe_to_invest_cents": sync.safe_to_invest_cents,
            }
        )
        target_acct = self.storage.get_account(default_acct_id)
        return {
            "format_detected": res.format_detected,
            "detected_institution": res.detected_institution,
            "detected_account_hint": res.detected_account_hint,
            "detected_account_type": res.detected_account_type,
            "target_account_id": default_acct_id,
            "target_account_name": str(target_acct["name"]) if target_acct else "",
            "parse_failed": bool(res.parse_failed),
            "total_parsed": len(res.rows),
            "imported_count": imported,
            "duplicate_count": duplicates,
            "cross_source_duplicates": cross_source_dups,
            "pending_skipped": res.pending_skipped,
            "transfers_detected": res.transfers_detected,
            "detected_balance_cents": res.detected_balance_cents,
            "warnings": res.warnings[:20],
            "sync": sync_dict,
        }

    def import_csv(self, csv_text: str, account_id: Optional[int] = None, flip_expense_sign: bool = False) -> Dict[str, Any]:
        return self.import_statement(
            csv_text, filename="upload.csv", account_id=account_id, flip_expense_sign=flip_expense_sign
        )

    def export_csv(self) -> str:
        txs = self.storage.list_transactions(limit=None)
        return export_transactions_csv(txs)

    def build_export_snapshot(self, *, days: int = 365) -> ExportSnapshot:
        """Assemble a complete live `ExportSnapshot` for Apple Numbers and Executive CSV Pack exports."""
        lookback = max(30, min(730, int(days or 365)))
        settings = self.storage.get_settings()
        budget = self._compute_budget()
        dashboard = self.get_dashboard()
        if lookback <= 31:
            period = "30d"
        elif lookback <= 95:
            period = "90d"
        elif lookback <= 370:
            period = "1y"
        else:
            period = "2y"
        analytics = self.get_analytics_overview(period=period)
        portfolio = self.get_portfolio()
        credit_report = self.get_credit_report()
        goals = self.list_goals(include_completed=True)
        quant_forecast = self.get_quant_forecast(
            days=min(90, lookback),
            budget=budget,
            credit_report=credit_report,
            goals=[g for g in goals if g.status == "active"],
        )
        recent_txs, splits_map = self._load_recent_transactions_with_splits(days=min(180, lookback))
        onetap_plan = self.onetap_engine.build_onetap_plan(
            budget=budget,
            transactions=recent_txs,
            recurring_bills=self.storage.list_recurring_bills(active_only=True),
            settings=settings,
            credit_report=credit_report,
            goals=[g for g in goals if g.status == "active"],
            holdings=portfolio.holdings,
            splits_by_tx_id=splits_map,
            horizon_days=30,
        )
        since_str = (date.today() - timedelta(days=lookback)).isoformat() if lookback < 730 else None
        export_txs = self.storage.list_transactions(
            limit=None,
            since_date=since_str,
            include_splits=True,
            include_tags=True,
        )
        return self.exporter.build_snapshot(
            dashboard=dashboard,
            analytics=analytics,
            portfolio=portfolio,
            credit_report=credit_report,
            quant_forecast=quant_forecast,
            onetap_plan=onetap_plan,
            accounts=dashboard.accounts,
            jars=dashboard.jars,
            transactions=export_txs,
            goals=goals,
            bills=dashboard.upcoming_bills,
            currency=str(settings.get("currency") or "USD"),
        )

    def export_apple_numbers_workbook(
        self,
        *,
        days: int = 365,
        native: bool = False,
    ) -> ExportArtifact:
        """Build the 6-sheet Apple Numbers Graphical Workbook (`.xlsx` or native `.numbers` on macOS)."""
        snap = self.build_export_snapshot(days=days)
        return self.exporter.export_apple_numbers_workbook(
            snap,
            convert_to_native_numbers=bool(native),
        )

    def export_transactions_csv_artifact(
        self,
        *,
        days: Optional[int] = None,
    ) -> ExportArtifact:
        """Export the full transaction ledger as UTF-8 BOM CSV with formula-injection neutralization."""
        settings = self.storage.get_settings()
        since_str = (
            (date.today() - timedelta(days=max(1, min(730, int(days))))).isoformat()
            if days is not None
            else None
        )
        txs = self.storage.list_transactions(
            limit=None,
            since_date=since_str,
            include_splits=True,
            include_tags=True,
        )
        accounts = self.storage.list_accounts()
        return self.exporter.export_transactions_csv_artifact(
            txs,
            accounts=accounts,
            include_bom=True,
            default_currency=str(settings.get("currency") or "USD"),
        )

    def export_executive_csv_pack_artifact(self, *, days: int = 365) -> ExportArtifact:
        """Export the 7-table Executive CSV Pack `.zip` archive."""
        snap = self.build_export_snapshot(days=days)
        return self.exporter.export_executive_csv_pack_artifact(snap, include_bom=True)

    # --- Credit Reports, Inquiries, Derogatories & Factor Analysis ---

    def get_credit_report(
        self,
        extra_monthly_budget_cents: int = 25_000,
        *,
        include_authorized_user: bool = True,
        include_sandbox: bool = False,
        today: Optional[date] = None,
    ) -> CreditOptimizationReport:
        ref = today or date.today()
        budget = self._compute_budget(today=ref)
        return self.credit_optimizer.analyze(
            raw_lines=self.storage.list_credit_lines(active_only=True, include_sandbox=include_sandbox),
            smoothed_monthly_income_cents=budget.smoothed_monthly_income_cents,
            extra_monthly_budget_cents=extra_monthly_budget_cents,
            credit_reports=self.storage.list_credit_reports(include_sandbox=include_sandbox),
            credit_inquiries=self.storage.list_credit_inquiries(include_sandbox=include_sandbox),
            credit_derogatories=self.storage.list_credit_derogatories(active_only=False, include_sandbox=include_sandbox),
            include_authorized_user=include_authorized_user,
            today=ref,
        )

    def create_credit_line(self, req: CreditLineCreate) -> Dict[str, Any]:
        data = req.model_dump()
        data["kind"] = req.kind.value
        data["autopay_mode"] = req.autopay_mode.value
        saved = self.storage.create_credit_line(data)
        self.run_self_updating_sync(reason=f"Added credit/debt line {saved['name']}")
        return saved

    def update_credit_line(self, line_id: int, req: CreditLineUpdate) -> Optional[Dict[str, Any]]:
        updates = req.model_dump(exclude_none=True)
        if "autopay_mode" in updates and hasattr(updates["autopay_mode"], "value"):
            updates["autopay_mode"] = updates["autopay_mode"].value
        updated = self.storage.update_credit_line(line_id, updates)
        if not updated:
            return None
        self.run_self_updating_sync(reason=f"Updated credit/debt line {updated['name']}")
        return updated

    def delete_credit_line(self, line_id: int) -> bool:
        deleted = self.storage.delete_credit_line(line_id)
        if deleted:
            self.run_self_updating_sync(reason=f"Removed credit/debt line #{line_id}")
        return deleted

    def evaluate_balance_transfer(self, req: BalanceTransferRequest) -> BalanceTransferResult:
        return self.credit_optimizer.evaluate_balance_transfer(req)

    def list_credit_reports(self, *, include_sandbox: bool = False) -> List[CreditReportOut]:
        return [CreditReportOut(**r) for r in self.storage.list_credit_reports(include_sandbox=include_sandbox)]

    def create_credit_report(self, req: CreditReportCreate) -> CreditReportOut:
        saved = self.storage.create_credit_report(req.model_dump())
        return CreditReportOut(**saved)

    def delete_credit_report(self, report_id: int) -> bool:
        return self.storage.delete_credit_report(report_id)

    def list_credit_inquiries(self, *, include_sandbox: bool = False) -> List[CreditInquiryOut]:
        today_str = date.today().isoformat()
        out = []
        for r in self.storage.list_credit_inquiries(include_sandbox=include_sandbox):
            out.append(
                CreditInquiryOut(
                    id=int(r["id"]),
                    lender=r["lender"],
                    bureau=r["bureau"],
                    inquiry_date=r["inquiry_date"],
                    counts_until=r["counts_until"],
                    drops_off=r["drops_off"],
                    is_scoring_active=str(r["counts_until"]) >= today_str,
                    created_at=str(r.get("created_at", "")),
                )
            )
        return out

    def create_credit_inquiry(self, req: CreditInquiryCreate) -> CreditInquiryOut:
        saved = self.storage.create_credit_inquiry(req.model_dump())
        today_str = date.today().isoformat()
        return CreditInquiryOut(
            id=int(saved["id"]),
            lender=saved["lender"],
            bureau=saved["bureau"],
            inquiry_date=saved["inquiry_date"],
            counts_until=saved["counts_until"],
            drops_off=saved["drops_off"],
            is_scoring_active=str(saved["counts_until"]) >= today_str,
            created_at=str(saved.get("created_at", "")),
        )

    def delete_credit_inquiry(self, inquiry_id: int) -> bool:
        return self.storage.delete_credit_inquiry(inquiry_id)

    def list_credit_derogatories(self, *, include_sandbox: bool = False) -> List[CreditDerogatoryOut]:
        return [
            CreditDerogatoryOut(**r)
            for r in self.storage.list_credit_derogatories(active_only=False, include_sandbox=include_sandbox)
        ]

    def create_credit_derogatory(self, req: CreditDerogatoryCreate) -> CreditDerogatoryOut:
        saved = self.storage.create_credit_derogatory(req.model_dump())
        return CreditDerogatoryOut(**saved)

    def delete_credit_derogatory(self, derog_id: int) -> bool:
        return self.storage.delete_credit_derogatory(derog_id)

    def import_credit_report(
        self,
        content: Any,
        *,
        filename: str = "",
        bureau_hint: str = "auto",
        confirm: bool = False,
    ) -> Dict[str, Any]:
        """
        Parse an official AnnualCreditReport PDF or structured JSON report.
        PDF imports ALWAYS default to `needs_review=True` preview until explicitly confirmed (`confirm=True`).
        """
        bundle = self.credit_report_importer.import_auto(content, filename=filename, bureau_hint=bureau_hint)
        out = bundle.to_dict()
        should_commit = bool(confirm) or (not bundle.needs_review)
        committed_counts = {"reports": 0, "inquiries": 0, "derogatories": 0, "credit_lines": 0}
        if should_commit:
            for rep in bundle.reports:
                if rep.score is not None and 300 <= int(rep.score) <= 850:
                    model_val = rep.model if rep.model in ("fico8", "vantage3", "fico9", "other") else "other"
                    self.storage.create_credit_report(
                        {
                            "bureau": rep.bureau,
                            "model": model_val,
                            "score": int(rep.score),
                            "pulled_date": rep.pulled_date,
                            "source": rep.source,
                            "is_sandbox": False,
                            "source_env": "production",
                        }
                    )
                    committed_counts["reports"] += 1
            for inq in bundle.inquiries:
                self.storage.create_credit_inquiry(
                    {
                        "lender": inq.lender,
                        "bureau": inq.bureau,
                        "inquiry_date": inq.inquiry_date,
                        "counts_until": inq.counts_until,
                        "drops_off": inq.drops_off,
                        "is_sandbox": False,
                        "source_env": "production",
                    }
                )
                committed_counts["inquiries"] += 1
            valid_derog = {
                "late_30",
                "late_60",
                "late_90",
                "charge_off",
                "collection",
                "bankruptcy_ch7",
                "bankruptcy_ch13",
                "other",
            }
            for der in bundle.derogatories:
                dkind = der.kind if der.kind in valid_derog else "other"
                self.storage.create_credit_derogatory(
                    {
                        "kind": dkind,
                        "lender": der.lender or der.creditor,
                        "opened_date": der.opened_date,
                        "drops_off": der.drops_off,
                        "active": der.active,
                        "is_sandbox": False,
                        "source_env": "production",
                    }
                )
                committed_counts["derogatories"] += 1
            existing_lines = {
                str(cl["name"]).strip().lower(): cl
                for cl in self.storage.list_credit_lines(active_only=False, include_sandbox=False)
            }
            for tl in bundle.tradelines:
                pl = tl.to_credit_line_payload()
                name_key = str(pl.get("name") or "").strip().lower()
                if name_key and name_key in existing_lines:
                    merged_pl = self._merge_tradeline_payload(existing_lines[name_key], pl)
                    self.storage.update_credit_line(int(existing_lines[name_key]["id"]), merged_pl)
                else:
                    clean_pl = {
                        k: v
                        for k, v in pl.items()
                        if k not in ("apr_provided", "closing_day_provided", "due_day_provided")
                    }
                    clean_pl["is_sandbox"] = False
                    clean_pl["source_env"] = "production"
                    created_cl = self.storage.create_credit_line(clean_pl)
                    if name_key:
                        existing_lines[name_key] = created_cl
                committed_counts["credit_lines"] += 1
            if sum(committed_counts.values()) > 0:
                self.run_self_updating_sync(reason="Imported official credit report")

        out["committed"] = should_commit
        out["committed_counts"] = committed_counts
        return out

    # --- Optional BYOK Credit Score APIs (Spinwheel / Array / CRS / Custom) ---

    @staticmethod
    def _credit_api_vendor_from_provider(provider: str) -> Optional[str]:
        if not provider.startswith("credit_api_"):
            return None
        vendor = provider[len("credit_api_") :]
        return vendor if vendor in CREDIT_API_VENDOR_SPECS else None

    def _build_credit_api_adapter(
        self, vendor: str, env: str, creds: Dict[str, Any]
    ) -> BureauAPIAdapter:
        return BureauAPIAdapter(
            vendor=vendor,
            env=env or "production",
            api_base_url=creds.get("api_base_url"),
            api_key=creds.get("api_key"),
            api_secret=creds.get("api_secret"),
            username=creds.get("username"),
            password=creds.get("password"),
            user_id=creds.get("user_id"),
            report_key=creds.get("report_key"),
            display_token=creds.get("display_token"),
            bureau=str(creds.get("bureau") or "equifax"),
            option=creds.get("option"),
            transport=self._credit_api_transport,
        )

    def _to_credit_api_connection_out(self, row: Dict[str, Any]) -> CreditAPIConnectionOut:
        provider = str(row["provider"])
        vendor = self._credit_api_vendor_from_provider(provider) or "custom"
        status = str(row.get("status", "active"))
        unlocked = self.secrets.is_unlocked()
        if not unlocked and status == "active":
            status = "paused_locked"
        creds = self._safe_get_secret_json(f"conn:{row['id']}") or {}
        key_for_mask = str(creds.get("api_key") or creds.get("username") or "")
        return CreditAPIConnectionOut(
            id=int(row["id"]),
            vendor=vendor,  # type: ignore[arg-type]
            provider=provider,
            institution=str(row.get("institution") or CREDIT_API_VENDOR_SPECS[vendor]["display_name"]),
            env=str(row.get("env", "production")),
            status=status,  # type: ignore[arg-type]
            api_key_masked=mask_secret(key_for_mask) if key_for_mask else "",
            has_credentials=bool(creds) if unlocked else True,
            user_id=str(creds.get("user_id") or "") if unlocked else "",
            has_report_key=bool(creds.get("report_key")) if unlocked else False,
            bureau=str(creds.get("bureau") or "equifax") if unlocked else "equifax",
            api_base_url=str(creds.get("api_base_url") or "") if unlocked else "",
            last_error=str(row.get("last_error") or ""),
            last_synced_at=row.get("last_synced_at"),
            created_at=str(row.get("created_at", "")),
            updated_at=str(row.get("updated_at", "")),
        )

    def list_credit_apis(self) -> CreditAPIListResponse:
        vendors = [
            CreditAPIVendorInfo(
                vendor=spec["vendor"],
                display_name=spec["display_name"],
                auth_type=spec["auth_type"],
                bureaus=list(spec["bureaus"]),
                models=list(spec["models"]),
                sandbox_base_url=spec["sandbox_base_url"],
                production_base_url=spec["production_base_url"],
                notes=spec["notes"],
            )
            for spec in CREDIT_API_VENDOR_SPECS.values()
        ]
        all_conns = self.storage.list_connections()
        credit_conns = [
            self._to_credit_api_connection_out(c)
            for c in all_conns
            if self._credit_api_vendor_from_provider(str(c["provider"])) is not None
        ]
        plaid_connected = any(c["provider"] == "plaid" and c.get("status") == "active" for c in all_conns)
        return CreditAPIListResponse(
            vendors=vendors,
            connections=credit_conns,
            plaid_liabilities_connected=plaid_connected,
        )

    def connect_credit_api(self, req: CreditAPIConnectRequest) -> Dict[str, Any]:
        self._require_vault_unlocked()
        spec = CREDIT_API_VENDOR_SPECS[req.vendor]
        creds: Dict[str, Any] = {
            "vendor": req.vendor,
            "env": req.env,
            "api_key": (req.api_key or "").strip(),
            "api_secret": (req.api_secret or "").strip(),
            "username": (req.username or "").strip(),
            "password": (req.password or "").strip(),
            "user_id": (req.user_id or "").strip(),
            "report_key": (req.report_key or "").strip(),
            "display_token": (req.display_token or "").strip(),
            "bureau": req.bureau,
            "option": (req.option or "").strip(),
            "api_base_url": (req.api_base_url or "").strip(),
        }
        # Validate host allowlist / SSRF guard and required credentials up front
        adapter = self._build_credit_api_adapter(req.vendor, req.env, creds)
        if not adapter.is_configured():
            raise ConnectorError(adapter.missing_config_message(), provider=adapter.provider_name)

        test_result: Optional[Dict[str, Any]] = None
        if req.test_on_connect:
            test_result = adapter.test_connection()

        identity_seed = "|".join(
            [
                req.vendor,
                req.env,
                creds["user_id"],
                creds["username"],
                creds["report_key"],
                creds["api_base_url"],
                creds["api_key"][:12],
                req.label.strip(),
            ]
        )
        item_key = hashlib.sha256(identity_seed.encode("utf-8")).hexdigest()[:16]
        institution = req.label.strip() or spec["display_name"]
        row = self.storage.upsert_connection(
            {
                "provider": f"credit_api_{req.vendor}",
                "item_id": item_key,
                "institution": institution,
                "env": req.env,
                "status": "active",
                "last_error": "",
            }
        )
        # Seal sensitive credentials strictly in secrets.enc (NEVER in SQLite)
        self.secrets.set_json(f"conn:{row['id']}", creds)
        self.storage.log_budget_event(
            "connector_added", f"Connected Credit Score API: {institution} ({req.vendor})"
        )
        return {
            "connection": self._to_credit_api_connection_out(row).model_dump(),
            "capabilities": adapter.capabilities(),
            "test_result": test_result,
        }

    def _load_credit_api_connection(self, connection_id: int) -> Tuple[Dict[str, Any], str, BureauAPIAdapter]:
        self._require_vault_unlocked()
        conn = self.storage.get_connection(connection_id)
        if not conn:
            raise ValueError(f"Credit Score API connection #{connection_id} not found.")
        vendor = self._credit_api_vendor_from_provider(str(conn["provider"]))
        if not vendor:
            raise ValueError(f"Connection #{connection_id} is not a Credit Score API connection.")
        creds = self.secrets.get_json(f"conn:{connection_id}")
        if not creds:
            self.storage.upsert_connection(
                {"id": connection_id, "provider": conn["provider"], "status": "needs_reauth"}
            )
            raise ConnectorAuthError(
                "Encrypted credentials missing for this Credit Score API connection. Re-connect to seal new keys.",
                provider=f"bureau_api_{vendor}",
            )
        adapter = self._build_credit_api_adapter(vendor, str(conn.get("env") or "production"), creds)
        return conn, vendor, adapter

    def test_credit_api(self, connection_id: int) -> Dict[str, Any]:
        conn, _vendor, adapter = self._load_credit_api_connection(connection_id)
        try:
            res = adapter.test_connection()
            self.storage.upsert_connection(
                {"id": connection_id, "provider": conn["provider"], "status": "active", "last_error": ""}
            )
            return res
        except ConnectorAuthError as exc:
            self.storage.upsert_connection(
                {
                    "id": connection_id,
                    "provider": conn["provider"],
                    "status": "needs_reauth",
                    "last_error": str(exc)[:240],
                }
            )
            raise
        except ConnectorError as exc:
            self.storage.upsert_connection(
                {
                    "id": connection_id,
                    "provider": conn["provider"],
                    "status": "error",
                    "last_error": str(exc)[:240],
                }
            )
            raise

    def sync_credit_api(
        self,
        connection_id: int,
        req: Optional[CreditAPISyncRequest] = None,
    ) -> Dict[str, Any]:
        conn, vendor, adapter = self._load_credit_api_connection(connection_id)
        conn_env = str(conn.get("env") or "production").strip().lower()
        is_sbx = conn_env == "sandbox"
        effective = req or CreditAPISyncRequest()
        try:
            bundle = adapter.fetch_credit_snapshot(
                order_refresh=effective.order_refresh,
                report_key=effective.report_key,
                display_token=effective.display_token,
                bureau=effective.bureau,
                option=effective.option,
                consumer_profile=effective.consumer_profile,
            )
        except ConnectorAuthError as exc:
            self.storage.upsert_connection(
                {
                    "id": connection_id,
                    "provider": conn["provider"],
                    "status": "needs_reauth",
                    "last_error": str(exc)[:240],
                }
            )
            raise
        except ConnectorError as exc:
            self.storage.upsert_connection(
                {
                    "id": connection_id,
                    "provider": conn["provider"],
                    "status": "error",
                    "last_error": str(exc)[:240],
                }
            )
            raise

        counts = {
            "reports_added": 0,
            "reports_skipped_duplicate": 0,
            "inquiries_added": 0,
            "inquiries_skipped_duplicate": 0,
            "derogatories_added": 0,
            "derogatories_skipped_duplicate": 0,
            "tradelines_added": 0,
            "tradelines_updated": 0,
        }

        partition_reports = [
            r
            for r in self.storage.list_credit_reports(include_sandbox=is_sbx)
            if bool(r.get("is_sandbox", 0)) == is_sbx
        ]
        existing_reports = {
            (str(r["bureau"]), str(r["model"]), int(r["score"]), str(r["pulled_date"]))
            for r in partition_reports
        }
        for rep in bundle.reports:
            if rep.score is None or not (300 <= int(rep.score) <= 850):
                continue
            model_val = rep.model if rep.model in ("fico8", "vantage3", "fico9", "other") else "other"
            rkey = (rep.bureau, model_val, int(rep.score), rep.pulled_date)
            if rkey in existing_reports:
                counts["reports_skipped_duplicate"] += 1
                continue
            self.storage.create_credit_report(
                {
                    "bureau": rep.bureau,
                    "model": model_val,
                    "score": int(rep.score),
                    "pulled_date": rep.pulled_date,
                    "source": rep.source or f"{vendor}_api",
                    "is_sandbox": is_sbx,
                    "source_env": conn_env,
                }
            )
            existing_reports.add(rkey)
            counts["reports_added"] += 1

        partition_inqs = [
            i
            for i in self.storage.list_credit_inquiries(include_sandbox=is_sbx)
            if bool(i.get("is_sandbox", 0)) == is_sbx
        ]
        existing_inqs = {
            (str(i["lender"]).strip().lower(), str(i["bureau"]), str(i["inquiry_date"]))
            for i in partition_inqs
        }
        for inq in bundle.inquiries:
            ikey = (inq.lender.strip().lower(), inq.bureau, inq.inquiry_date)
            if ikey in existing_inqs:
                counts["inquiries_skipped_duplicate"] += 1
                continue
            self.storage.create_credit_inquiry(
                {
                    "lender": inq.lender,
                    "bureau": inq.bureau,
                    "inquiry_date": inq.inquiry_date,
                    "counts_until": inq.counts_until,
                    "drops_off": inq.drops_off,
                    "is_sandbox": is_sbx,
                    "source_env": conn_env,
                }
            )
            existing_inqs.add(ikey)
            counts["inquiries_added"] += 1

        valid_derog = {
            "late_30",
            "late_60",
            "late_90",
            "charge_off",
            "collection",
            "bankruptcy_ch7",
            "bankruptcy_ch13",
            "other",
        }
        partition_derogs = [
            d
            for d in self.storage.list_credit_derogatories(active_only=False, include_sandbox=is_sbx)
            if bool(d.get("is_sandbox", 0)) == is_sbx
        ]
        existing_derogs = {
            (str(d["kind"]), str(d.get("lender", "")).strip().lower(), str(d["opened_date"]))
            for d in partition_derogs
        }
        for der in bundle.derogatories:
            dkind = der.kind if der.kind in valid_derog else "other"
            lender_str = der.lender or der.creditor
            dkey = (dkind, lender_str.strip().lower(), der.opened_date)
            if dkey in existing_derogs:
                counts["derogatories_skipped_duplicate"] += 1
                continue
            self.storage.create_credit_derogatory(
                {
                    "kind": dkind,
                    "lender": lender_str,
                    "opened_date": der.opened_date,
                    "drops_off": der.drops_off,
                    "active": der.active,
                    "is_sandbox": is_sbx,
                    "source_env": conn_env,
                }
            )
            existing_derogs.add(dkey)
            counts["derogatories_added"] += 1

        if effective.sync_tradelines and bundle.tradelines:
            partition_lines = [
                cl
                for cl in self.storage.list_credit_lines(active_only=False, include_sandbox=is_sbx)
                if (str(cl.get("source_env") or "production").lower() == "sandbox") == is_sbx
            ]
            by_ext = {
                str(cl.get("external_account_id") or "").strip(): cl
                for cl in partition_lines
                if str(cl.get("external_account_id") or "").strip()
            }
            existing_lines = {
                str(cl["name"]).strip().lower(): cl
                for cl in partition_lines
            }
            for tl in bundle.tradelines:
                payload = tl.to_credit_line_payload()
                name_key = str(payload.get("name", "")).strip().lower()
                ext_key = str(payload.get("external_account_id") or "").strip()
                if not name_key:
                    continue
                target_cl = by_ext.get(ext_key) if ext_key else None
                if target_cl is None:
                    target_cl = existing_lines.get(name_key)
                if target_cl is not None:
                    merged_pl = self._merge_tradeline_payload(target_cl, payload)
                    self.storage.update_credit_line(int(target_cl["id"]), merged_pl)
                    counts["tradelines_updated"] += 1
                else:
                    clean_pl = {
                        k: v
                        for k, v in payload.items()
                        if k not in ("apr_provided", "closing_day_provided", "due_day_provided")
                    }
                    clean_pl["is_sandbox"] = is_sbx
                    clean_pl["source_env"] = conn_env
                    created = self.storage.create_credit_line(clean_pl)
                    existing_lines[name_key] = created
                    if ext_key:
                        by_ext[ext_key] = created
                    counts["tradelines_added"] += 1

        updated_conn = self.storage.upsert_connection(
            {
                "id": connection_id,
                "provider": conn["provider"],
                "status": "active",
                "last_error": "",
                "last_synced_at": utc_now_iso(),
            }
        )
        total_changes = (
            counts["reports_added"]
            + counts["inquiries_added"]
            + counts["derogatories_added"]
            + counts["tradelines_added"]
            + counts["tradelines_updated"]
        )
        if total_changes > 0 and not is_sbx:
            self.run_self_updating_sync(
                reason=f"Synced Credit Score API ({conn.get('institution') or vendor})"
            )

        return {
            "connection": self._to_credit_api_connection_out(updated_conn).model_dump(),
            "synced_counts": counts,
            "sandbox_isolated": is_sbx,
            "warnings": list(bundle.warnings),
            "credit_report": self.get_credit_report(include_sandbox=is_sbx).model_dump(),
        }

    def disconnect_credit_api(self, connection_id: int) -> bool:
        self._require_vault_unlocked()
        conn = self.storage.get_connection(connection_id)
        if not conn or self._credit_api_vendor_from_provider(str(conn["provider"])) is None:
            return False
        return self.delete_connection(connection_id)

    # --- Stocks & Crypto Portfolio + Direct Official Brokers ---

    def _to_holding_out(self, row: Dict[str, Any]) -> HoldingOut:
        qty = int(row["quantity_e8"])
        price = int(row["price_e8"])
        mval = compute_holding_value_cents(qty, price)
        cb = int(row.get("cost_basis_cents", 0))
        asset_cls = str(row.get("asset_class", "equity"))
        raw_known = row.get("cost_basis_known")
        cb_known = bool(raw_known) if raw_known is not None else (cb > 0 or asset_cls == "cash")
        eff_cb = cb if cb_known else 0
        pnl = (mval - eff_cb) if cb_known else 0
        pnl_pct = round((pnl / eff_cb) * 100.0, 2) if (cb_known and eff_cb > 0) else 0.0
        return HoldingOut(
            id=int(row["id"]),
            account_id=int(row["account_id"]) if row.get("account_id") is not None else None,
            connection_id=int(row["connection_id"]) if row.get("connection_id") is not None else None,
            external_account_id=str(row.get("external_account_id") or ""),
            symbol=str(row["symbol"]).upper(),
            name=str(row.get("name", "")),
            asset_class=asset_cls,  # type: ignore[arg-type]
            quantity_e8=qty,
            cost_basis_cents=eff_cb,
            cost_basis_known=cb_known,
            price_e8=price,
            market_value_cents=mval,
            unrealized_pnl_cents=pnl,
            unrealized_pnl_pct=pnl_pct,
            source=str(row.get("source", "manual")),
            created_at=str(row.get("created_at", "")),
            updated_at=str(row.get("updated_at", "")),
        )

    def _upsert_normalized_holding(
        self,
        nh: NormalizedHolding,
        *,
        connection_id: Optional[int] = None,
        account_id: Optional[int] = None,
    ) -> HoldingOut:
        saved = self.storage.upsert_holding(
            {
                "account_id": account_id,
                "connection_id": connection_id,
                "external_account_id": nh.external_account_id,
                "symbol": nh.symbol,
                "name": nh.name,
                "asset_class": nh.asset_class if nh.asset_class in ("equity", "etf", "crypto", "cash") else "equity",
                "quantity_e8": nh.quantity_e8,
                "cost_basis_cents": nh.cost_basis_cents,
                "cost_basis_known": nh.cost_basis_known,
                "price_e8": nh.price_e8,
                "source": nh.source,
            }
        )
        return self._to_holding_out(saved)

    def _record_portfolio_snapshot(self, today: Optional[date] = None) -> None:
        ref = today or date.today()
        holdings = [self._to_holding_out(r) for r in self.storage.list_holdings()]
        tot_val = sum(h.market_value_cents for h in holdings)
        tot_cb = sum(h.cost_basis_cents for h in holdings if h.cost_basis_known)
        self.storage.upsert_portfolio_snapshot(ref.isoformat(), tot_val, tot_cb)
        self._refresh_net_worth_snapshot_today(today=ref)

    def get_portfolio(self) -> PortfolioSummary:
        holdings = [self._to_holding_out(r) for r in self.storage.list_holdings()]
        tot_val = sum(h.market_value_cents for h in holdings)
        known_cnt = sum(1 for h in holdings if h.cost_basis_known)
        unknown_cnt = len(holdings) - known_cnt
        known_val = sum(h.market_value_cents for h in holdings if h.cost_basis_known)
        unknown_val = max(0, tot_val - known_val)
        tot_cb = sum(h.cost_basis_cents for h in holdings if h.cost_basis_known)
        pnl = (known_val - tot_cb) if known_cnt > 0 else 0
        pnl_pct = round((pnl / tot_cb) * 100.0, 2) if tot_cb > 0 else 0.0
        cov_pct = round((known_val / tot_val) * 100.0, 1) if tot_val > 0 else 100.0

        by_class: Dict[str, int] = {}
        sources = set()
        for h in holdings:
            by_class[h.asset_class] = by_class.get(h.asset_class, 0) + h.market_value_cents
            if h.source:
                sources.add(h.source)

        allocations = [
            PortfolioAllocationItem(
                asset_class=cls_name,  # type: ignore[arg-type]
                value_cents=val,
                pct=round((val / tot_val) * 100.0, 2) if tot_val > 0 else 0.0,
            )
            for cls_name, val in sorted(by_class.items(), key=lambda kv: -kv[1])
        ]
        snaps = [
            PortfolioSnapshotOut(
                snap_date=s["snap_date"],
                total_value_cents=int(s["total_value_cents"]),
                cost_basis_cents=int(s["cost_basis_cents"]),
            )
            for s in self.storage.list_portfolio_snapshots(limit=_LOOKBACK_730D)
        ]
        return PortfolioSummary(
            generated_at=utc_now_iso(),
            total_value_cents=tot_val,
            total_cost_basis_cents=tot_cb,
            known_cost_basis_value_cents=known_val,
            unknown_cost_basis_value_cents=unknown_val,
            known_cost_basis_holdings_count=known_cnt,
            unknown_cost_basis_holdings_count=unknown_cnt,
            cost_basis_coverage_pct=cov_pct,
            unrealized_pnl_cents=pnl,
            unrealized_pnl_pct=pnl_pct,
            allocations=allocations,
            holdings=holdings,
            snapshots=snaps,
            quote_sources=sorted(sources),
        )

    def upsert_holding(self, req: HoldingCreate) -> HoldingOut:
        saved = self.storage.upsert_holding(req.model_dump())
        self._record_portfolio_snapshot()
        return self._to_holding_out(saved)

    def delete_holding(self, holding_id: int) -> bool:
        ok = self.storage.delete_holding(holding_id)
        if ok:
            self._record_portfolio_snapshot()
        return ok

    def _build_quote_service(self) -> QuoteService:
        schwab_connector: Optional[SchwabConnector] = None
        schwab_token: Optional[str] = None
        if self.secrets.is_unlocked():
            for c in self.storage.list_connections():
                if c["provider"] == "schwab" and c.get("status") == "active":
                    creds = self.secrets.get_json(f"conn:{c['id']}")
                    if creds and creds.get("access_token") and creds.get("client_id") and creds.get("client_secret"):
                        schwab_connector = SchwabConnector(
                            client_id=str(creds["client_id"]),
                            client_secret=str(creds["client_secret"]),
                            callback_url=str(
                                creds.get("redirect_uri") or "https://127.0.0.1:8787/api/v1/brokers/schwab/callback"
                            ),
                            transport=self._broker_transport,
                        )
                        schwab_token = str(creds["access_token"])
                        break
        polygon_key = self._safe_get_secret("polygon_api_key") or (os.getenv("POLYGON_API_KEY") or "").strip() or None
        return QuoteService(
            schwab_connector=schwab_connector,
            schwab_access_token=schwab_token,
            polygon_api_key=polygon_key,
            transport=self._quote_transport,
        )

    def refresh_portfolio_quotes(self, force_refresh: bool = True) -> PortfolioSummary:
        raw_rows = self.storage.list_holdings()
        if not raw_rows:
            return self.get_portfolio()
        qsvc = self._build_quote_service()
        items = [(str(r["symbol"]), str(r.get("asset_class", "equity"))) for r in raw_rows]
        quotes = qsvc.get_quotes(items, force_refresh=force_refresh)
        for r in raw_rows:
            sym = str(r["symbol"]).upper()
            q = quotes.get(sym)
            if q and q.price_e8 > 0:
                self.storage.upsert_holding(
                    {
                        "id": int(r["id"]),
                        "account_id": r.get("account_id"),
                        "connection_id": r.get("connection_id"),
                        "external_account_id": r.get("external_account_id", ""),
                        "symbol": sym,
                        "name": r.get("name", ""),
                        "asset_class": r.get("asset_class", "equity"),
                        "quantity_e8": int(r["quantity_e8"]),
                        "cost_basis_cents": int(r["cost_basis_cents"]),
                        "cost_basis_known": bool(r.get("cost_basis_known", 1)),
                        "price_e8": int(q.price_e8),
                        "source": str(r.get("source", "manual")),
                    }
                )
        self._record_portfolio_snapshot()
        return self.get_portfolio()

    def import_portfolio_csv(self, csv_text: str, source: str = "csv_import") -> Dict[str, Any]:
        res = parse_holdings_csv(csv_text, source=source)
        for nh in res.holdings:
            self._upsert_normalized_holding(nh)
        self._record_portfolio_snapshot()
        return {
            "imported_count": len(res.holdings),
            "warnings": list(res.warnings),
            "portfolio": self.get_portfolio().model_dump(),
        }

    def connect_coinbase(self, req: BrokerCoinbaseConnectRequest) -> Dict[str, Any]:
        self._require_vault_unlocked()
        connector = CoinbaseConnector(
            api_key_name=req.api_key_name.strip(),
            private_key_pem=req.private_key_pem.strip(),
            transport=self._broker_transport,
        )
        # Strictly enforces read-only key_permissions (refuses can_trade / can_transfer)
        info = connector.connect()
        item_key = hashlib.sha256(req.api_key_name.strip().encode("utf-8")).hexdigest()[:16]
        row = self.storage.upsert_connection(
            {
                "provider": "coinbase",
                "item_id": item_key,
                "institution": req.label.strip() or "Coinbase",
                "env": "production",
                "status": "active",
            }
        )
        self.secrets.set_json(
            f"conn:{row['id']}",
            {
                "api_key_name": req.api_key_name.strip(),
                "private_key_pem": req.private_key_pem.strip(),
            },
        )
        self.storage.log_budget_event("connector_added", f"Connected read-only Coinbase ({row['institution']})")
        return {"connection": self._to_connection_out(row).model_dump(), "permissions": info}

    def connect_robinhood(self, req: BrokerRobinhoodConnectRequest) -> Dict[str, Any]:
        self._require_vault_unlocked()
        connector = RobinhoodCryptoConnector(
            api_key=req.api_key.strip(),
            private_key_b64=req.private_key_b64.strip(),
            declared_scopes=req.declared_scopes,
            transport=self._broker_transport,
        )
        # Enforces read-only scope check
        info = connector.connect(declared_scopes=req.declared_scopes)
        item_key = hashlib.sha256(req.api_key.strip().encode("utf-8")).hexdigest()[:16]
        row = self.storage.upsert_connection(
            {
                "provider": "robinhood",
                "item_id": item_key,
                "institution": req.label.strip() or "Robinhood Crypto",
                "env": "production",
                "status": "active",
            }
        )
        self.secrets.set_json(
            f"conn:{row['id']}",
            {
                "api_key": req.api_key.strip(),
                "private_key_b64": req.private_key_b64.strip(),
                "declared_scopes": req.declared_scopes,
            },
        )
        self.storage.log_budget_event("connector_added", f"Connected read-only Robinhood Crypto ({row['institution']})")
        return {"connection": self._to_connection_out(row).model_dump(), "account": info}

    def _start_ephemeral_schwab_tls_server(self, redirect_uri: str) -> bool:
        """
        Start an ephemeral loopback HTTPS listener on `127.0.0.1:<port>` for Schwab's
        fixed-port OAuth2 redirect URI, automatically shutting down once the callback completes.
        """
        parsed = urlparse(redirect_uri)
        if parsed.scheme != "https" or (parsed.hostname or "") != "127.0.0.1" or parsed.port is None:
            return False
        port = int(parsed.port)
        expected_path = parsed.path or "/api/v1/brokers/schwab/callback"

        if self._schwab_callback_server is not None and self._schwab_callback_port == port:
            return True
        self._stop_schwab_callback_server()

        try:
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            now_utc = datetime.now(timezone.utc)
            subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
            cert = (
                x509.CertificateBuilder()
                .subject_name(subject)
                .issuer_name(issuer)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now_utc - timedelta(minutes=5))
                .not_valid_after(now_utc + timedelta(days=2))
                .add_extension(
                    x509.SubjectAlternativeName(
                        [
                            x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                            x509.DNSName("localhost"),
                        ]
                    ),
                    critical=False,
                )
                .sign(key, hashes.SHA256())
            )
            tls_dir = tempfile.TemporaryDirectory(prefix="chronox_schwab_tls_")
            cert_path = Path(tls_dir.name) / "cert.pem"
            key_path = Path(tls_dir.name) / "key.pem"
            cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            key_path.write_bytes(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.TraditionalOpenSSL,
                    serialization.NoEncryption(),
                )
            )
            os.chmod(key_path, 0o600)

            svc_ref = self

            class _SchwabCallbackHandler(BaseHTTPRequestHandler):
                def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                    return

                def do_GET(self) -> None:  # noqa: N802
                    req_parsed = urlparse(self.path)
                    if req_parsed.path.rstrip("/") != expected_path.rstrip("/"):
                        self.send_response(404)
                        self.end_headers()
                        return
                    qs = parse_qs(req_parsed.query)
                    code_val = unquote((qs.get("code") or [""])[0]).strip()
                    state_val = (qs.get("state") or [""])[0].strip()
                    if not code_val or not state_val:
                        self.send_response(400)
                        self.send_header("Content-Type", "text/plain; charset=utf-8")
                        self.end_headers()
                        self.wfile.write(b"Missing OAuth2 code or state parameter.")
                        return
                    try:
                        conn_out = svc_ref.handle_schwab_callback(code_val, state_val)
                        safe_inst = html.escape(str(conn_out.institution or "Charles Schwab"))
                        body = (
                            f"<!doctype html><html><head><meta charset='utf-8'><title>Schwab Connected</title></head>"
                            f"<body style='background:#0b0e14;color:#e2e8f0;font-family:monospace;padding:32px;'>"
                            f"<h2>Charles Schwab Connected ({safe_inst})</h2>"
                            f"<p>Authorization complete. You may close this browser tab and return to ChronoX.</p>"
                            f"</body></html>"
                        ).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.end_headers()
                        self.wfile.write(body)
                    except Exception as cb_exc:
                        err_body = f"Schwab OAuth callback failed: {cb_exc}".encode("utf-8", errors="replace")
                        self.send_response(400)
                        self.send_header("Content-Type", "text/plain; charset=utf-8")
                        self.end_headers()
                        self.wfile.write(err_body)

            httpd = HTTPServer(("127.0.0.1", port), _SchwabCallbackHandler)
            ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ssl_ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
            httpd.socket = ssl_ctx.wrap_socket(httpd.socket, server_side=True)

            self._schwab_tls_dir = tls_dir
            self._schwab_callback_server = httpd
            self._schwab_callback_port = port
            t = threading.Thread(target=httpd.serve_forever, name="chronox-schwab-oauth-tls", daemon=True)
            t.start()
            return True
        except Exception as exc:
            logger.warning("Could not bind ephemeral Schwab HTTPS listener on %s: %s", redirect_uri, exc)
            self._stop_schwab_callback_server()
            return False

    def connect_schwab(self, req: BrokerSchwabConnectRequest) -> Dict[str, Any]:
        self._require_vault_unlocked()
        redirect_clean = req.redirect_uri.strip()
        connector = SchwabConnector(
            client_id=req.client_id.strip(),
            client_secret=req.client_secret.strip(),
            callback_url=redirect_clean,
            transport=self._broker_transport,
        )
        oauth_state = secrets.token_urlsafe(24)
        auth_url = connector.build_authorization_url(oauth_state)
        self._pending_schwab_states[oauth_state] = {
            "client_id": req.client_id.strip(),
            "client_secret": req.client_secret.strip(),
            "redirect_uri": redirect_clean,
            "label": req.label.strip() or "Charles Schwab",
            "created_at": utc_now_iso(),
        }
        listener_active = (
            self._start_ephemeral_schwab_tls_server(redirect_clean)
            if req.start_callback_listener
            else False
        )
        return {
            "authorize_url": auth_url,
            "authorization_url": auth_url,
            "state": oauth_state,
            "redirect_uri": redirect_clean,
            "callback_listener_active": listener_active,
            "manual_completion_supported": True,
            "refresh_note": (
                "Schwab refresh tokens expire every 7 days. Complete login in your browser (or paste the redirect URL below if self-signed local TLS is blocked)."
            ),
        }

    def handle_schwab_callback(self, code: str, state: str) -> ConnectionOut:
        self._require_vault_unlocked()
        pending = self._pending_schwab_states.pop(state, None)
        if not pending:
            raise ConnectorAuthError("Invalid or expired Charles Schwab OAuth2 state parameter.")
        connector = SchwabConnector(
            client_id=pending["client_id"],
            client_secret=pending["client_secret"],
            callback_url=pending["redirect_uri"],
            transport=self._broker_transport,
        )
        bundle = connector.exchange_code(unquote(code.strip()))
        item_key = hashlib.sha256(pending["client_id"].encode("utf-8")).hexdigest()[:16]
        row = self.storage.upsert_connection(
            {
                "provider": "schwab",
                "item_id": item_key,
                "institution": pending["label"],
                "env": "production",
                "status": "active",
                "token_expires_at": bundle.refresh_expires_at,
                "last_error": "",
            }
        )
        self.secrets.set_json(
            f"conn:{row['id']}",
            {
                "client_id": pending["client_id"],
                "client_secret": pending["client_secret"],
                "redirect_uri": pending["redirect_uri"],
                "access_token": bundle.access_token,
                "refresh_token": bundle.refresh_token,
                "access_expires_at": bundle.access_expires_at,
                "refresh_expires_at": bundle.refresh_expires_at,
            },
        )
        self.storage.log_budget_event("connector_added", f"Connected Charles Schwab OAuth2 ({row['institution']})")
        if not self._pending_schwab_states:
            threading.Thread(target=self._stop_schwab_callback_server, daemon=True).start()
        return self._to_connection_out(row)

    def complete_schwab_oauth(self, req: BrokerSchwabCompleteRequest) -> ConnectionOut:
        """
        Complete Charles Schwab OAuth2 either from a pasted browser redirect URL
        (`https://127.0.0.1:8787/api/v1/brokers/schwab/callback?code=...&state=...`)
        or from explicit `code` + `state` fields.
        """
        self._require_vault_unlocked()
        code = (req.code or "").strip()
        state = (req.state or "").strip()
        raw_input = (req.redirect_url or req.callback_url_or_code or "").strip()
        if raw_input:
            if "://" in raw_input or "code=" in raw_input or raw_input.startswith("?"):
                q_source = raw_input if "://" in raw_input else f"https://127.0.0.1/?{raw_input.lstrip('?')}"
                parsed = urlparse(q_source)
                qs = parse_qs(parsed.query)
                if not code and qs.get("code"):
                    code = unquote(qs["code"][0]).strip()
                if not state and qs.get("state"):
                    state = qs["state"][0].strip()
            elif not code:
                code = unquote(raw_input)
        if not state and len(self._pending_schwab_states) == 1:
            state = next(iter(self._pending_schwab_states.keys()))
        if not code or not state:
            raise ConnectorAuthError(
                "Paste the full Schwab redirect URL (containing ?code=...&state=...) or provide both code and state.",
                provider="schwab",
            )
        return self.handle_schwab_callback(code, state)

    def sync_broker(self, connection_id: int) -> Dict[str, Any]:
        self._require_vault_unlocked()
        conn = self.storage.get_connection(connection_id)
        if not conn:
            raise ValueError(f"Connection {connection_id} not found")
        provider = str(conn["provider"])
        creds = self.secrets.get_json(f"conn:{connection_id}")
        if not creds:
            self.storage.upsert_connection(
                {
                    "id": connection_id,
                    "provider": provider,
                    "status": "needs_reauth",
                    "last_error": "Missing sealed credentials; reconnect required.",
                }
            )
            raise ConnectorAuthError(f"Missing sealed credentials for {provider} connection #{connection_id}")

        holdings: List[NormalizedHolding] = []
        try:
            if provider == "coinbase":
                cb = CoinbaseConnector(
                    api_key_name=str(creds["api_key_name"]),
                    private_key_pem=str(creds["private_key_pem"]),
                    transport=self._broker_transport,
                )
                holdings = cb.fetch_holdings(verify_permissions=True, fetch_prices=True)
            elif provider == "robinhood":
                rh = RobinhoodCryptoConnector(
                    api_key=str(creds["api_key"]),
                    private_key_b64=str(creds["private_key_b64"]),
                    declared_scopes=creds.get("declared_scopes"),
                    transport=self._broker_transport,
                )
                holdings = rh.fetch_holdings(fetch_prices=True)
            elif provider == "schwab":
                sc = SchwabConnector(
                    client_id=str(creds["client_id"]),
                    client_secret=str(creds["client_secret"]),
                    callback_url=str(creds.get("redirect_uri") or "https://127.0.0.1:8787/api/v1/brokers/schwab/callback"),
                    transport=self._broker_transport,
                )
                access_token = str(creds.get("access_token") or "")
                access_exp = creds.get("access_expires_at")
                refresh_exp = creds.get("refresh_expires_at")
                rem_days = _days_until_iso_timestamp(refresh_exp)
                if rem_days is not None and rem_days <= 0:
                    raise ConnectorAuthError(
                        "Schwab 7-day refresh token expired — please re-authenticate.",
                        provider="schwab",
                    )
                # Refresh access token if expired or close to expiry
                needs_refresh = not access_token
                if access_exp and not needs_refresh:
                    try:
                        exp_dt = datetime.fromisoformat(str(access_exp).replace("Z", "+00:00"))
                        if exp_dt <= datetime.now(timezone.utc) + timedelta(minutes=2):
                            needs_refresh = True
                    except ValueError:
                        needs_refresh = True
                if needs_refresh and creds.get("refresh_token"):
                    bundle = sc.refresh_access_token(
                        str(creds["refresh_token"]),
                        existing_refresh_expires_at=refresh_exp,
                    )
                    access_token = bundle.access_token
                    creds["access_token"] = bundle.access_token
                    creds["refresh_token"] = bundle.refresh_token
                    creds["access_expires_at"] = bundle.access_expires_at
                    creds["refresh_expires_at"] = bundle.refresh_expires_at
                    self.secrets.set_json(f"conn:{connection_id}", creds)
                    self.storage.upsert_connection(
                        {
                            "id": connection_id,
                            "provider": "schwab",
                            "token_expires_at": bundle.refresh_expires_at,
                        }
                    )
                holdings = sc.fetch_holdings(access_token)
            elif provider == "plaid":
                conn_env = str(conn.get("env") or creds.get("env") or "production").strip().lower()
                client, _env = self._build_plaid_client(env_override=conn_env, creds_override=creds)
                holdings = client.get_investments_holdings(str(creds["access_token"]))
            else:
                raise ConnectorError(f"Provider '{provider}' does not support brokerage holdings sync.")
        except ConnectorAuthError as exc:
            self.storage.upsert_connection(
                {
                    "id": connection_id,
                    "provider": provider,
                    "status": "needs_reauth",
                    "last_error": str(exc)[:240],
                }
            )
            raise

        with self.storage.transaction(immediate=True):
            acct_by_ext = {
                str(a.get("external_id") or "").strip(): int(a["id"])
                for a in self.storage.list_accounts()
                if str(a.get("external_id") or "").strip()
            }
            keep_ids: List[int] = []
            for nh in holdings:
                saved_h = self._upsert_normalized_holding(
                    nh,
                    connection_id=connection_id,
                    account_id=acct_by_ext.get(nh.external_account_id),
                )
                keep_ids.append(saved_h.id)
            pruned_count = self.storage.prune_connection_holdings(connection_id, keep_ids)

            self.storage.upsert_connection(
                {
                    "id": connection_id,
                    "provider": provider,
                    "status": "active",
                    "last_error": "",
                    "last_synced_at": utc_now_iso(),
                }
            )
        self._record_portfolio_snapshot()
        summary = self.get_portfolio()
        return {
            "connection_id": connection_id,
            "provider": provider,
            "holdings_synced": len(holdings),
            "holdings_pruned": pruned_count,
            "total_value_cents": summary.total_value_cents,
        }

    # --- Goal Optimizer & Direct Manipulation ---

    def _to_goal_out(self, row: Dict[str, Any]) -> GoalOut:
        target = max(1, int(row["target_cents"]))
        saved = max(0, int(row["saved_cents"]))
        rem = max(0, target - saved)
        pct = round(min(100.0, (saved / target) * 100.0), 1)
        return GoalOut(
            id=int(row["id"]),
            name=row["name"],
            target_cents=target,
            saved_cents=saved,
            remaining_cents=rem,
            pct_funded=pct,
            target_date=row.get("target_date"),
            priority=int(row.get("priority", 2)),
            priority_order=int(row.get("priority_order", 0)),
            category=GoalCategory(row.get("category", GoalCategory.GADGET.value)),
            funding_jar_slug=row.get("funding_jar_slug", "fun"),
            status=row.get("status", "active"),
            notes=row.get("notes", ""),
            deleted_at=row.get("deleted_at"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_goals(self, include_completed: bool = False) -> List[GoalOut]:
        return [self._to_goal_out(r) for r in self.storage.list_goals(include_completed=include_completed)]

    def create_goal(self, req: GoalCreate) -> GoalOut:
        data = req.model_dump()
        data["category"] = req.category.value
        saved = self.storage.create_goal(data)
        self.storage.log_budget_event(
            "goal_created",
            f"Created goal '{saved['name']}' (${saved['target_cents'] / 100:,.2f})",
        )
        return self._to_goal_out(saved)

    def update_goal(self, goal_id: int, req: GoalUpdate) -> Optional[GoalOut]:
        updates = req.model_dump(exclude_none=True)
        if "target_date" in req.model_fields_set and req.target_date is None:
            updates["target_date"] = None
        updated = self.storage.update_goal(goal_id, updates)
        return self._to_goal_out(updated) if updated else None

    def reorder_goals(self, req: GoalReorderRequest) -> List[GoalOut]:
        rows = self.storage.reorder_goals(req.ordered_ids)
        self.storage.log_budget_event("goals_reordered", f"Reordered {len(req.ordered_ids)} goal(s)")
        return [self._to_goal_out(r) for r in rows]

    def contribute_goal(self, goal_id: int, req: GoalContributeRequest) -> GoalOut:
        existing = self.storage.get_goal(goal_id)
        if not existing:
            raise ValueError("Goal not found")
        new_saved = int(existing["saved_cents"]) + req.amount_cents
        new_status = "funded" if new_saved >= int(existing["target_cents"]) else existing["status"]
        updated = self.storage.update_goal(goal_id, {"saved_cents": new_saved, "status": new_status})
        assert updated is not None
        prog = self.storage.get_adhd_progress()
        xp_gain = 40 if new_status == "funded" and existing["status"] != "funded" else 15
        win_text = (
            f"Goal funded! '{existing['name']}' hit ${int(existing['target_cents']) / 100:,.2f}! +{xp_gain} XP"
            if new_status == "funded" and existing["status"] != "funded"
            else f"Added ${req.amount_cents / 100:,.2f} toward '{existing['name']}' +{xp_gain} XP"
        )
        self.storage.update_adhd_progress(
            {"dopamine_xp": int(prog["dopamine_xp"]) + xp_gain, "recent_wins": [win_text] + prog["recent_wins"]}
        )
        self.storage.log_budget_event("goal_contribution", win_text, {"goal_id": goal_id, "amount_cents": req.amount_cents})
        return self._to_goal_out(updated)

    def delete_goal(self, goal_id: int, *, hard: bool = False) -> bool:
        if hard:
            return self.storage.delete_goal(goal_id)
        return self.storage.soft_delete_goal(goal_id) is not None

    def restore_goal(self, goal_id: int) -> Optional[GoalOut]:
        restored = self.storage.restore_goal(goal_id)
        return self._to_goal_out(restored) if restored else None

    def optimize_goal_plan(self, req: GoalPlanRequest, today: Optional[date] = None) -> GoalPlanResponse:
        ref = today or date.today()
        settings = self.storage.get_settings()
        budget = self._compute_budget(today=ref)
        credit_report = self.get_credit_report(today=ref)
        impulses = self.list_impulses(include_resolved=False)
        return self.goal_optimizer.optimize(
            req=req,
            budget=budget,
            credit_report=credit_report,
            impulses=impulses,
            hourly_wage_cents=int(settings.get("hourly_wage_cents", 3000)),
            payday_cadence_days=int(settings.get("payday_cadence_days", 14)),
            today=ref,
        )

    # --- Accounts & Jars ---

    def create_account(self, req: AccountCreate) -> AccountOut:
        acct = self.storage.create_account(
            name=req.name.strip(),
            account_type=req.account_type.value,
            balance_cents=req.balance_cents,
            institution=req.institution.strip(),
            is_liquid=req.is_liquid,
            currency=req.currency.strip().upper() or "USD",
            available_cents=req.available_cents,
            credit_limit_cents=req.credit_limit_cents,
            mask=req.mask.strip(),
        )
        self.run_self_updating_sync(reason=f"Added account {acct['name']}")
        return AccountOut(**acct)

    def update_account_balance(self, account_id: int, balance_cents: int) -> Optional[AccountOut]:
        acct = self.storage.set_account_balance(account_id, balance_cents)
        if not acct:
            return None
        self.run_self_updating_sync(reason=f"Reconciled {acct['name']} balance")
        return AccountOut(**acct)

    def update_jar(self, jar_id: int, req: JarUpdate) -> Optional[Dict[str, Any]]:
        updates = req.model_dump(exclude_none=True)
        for bool_k in ("rollover_enabled", "is_locked"):
            if bool_k in updates:
                updates[bool_k] = int(updates[bool_k])
        updated = self.storage.update_jar(jar_id, updates)
        if not updated:
            return None
        self.run_self_updating_sync(reason=f"Updated jar {updated['name']}")
        return updated

    def rebalance_jars(self, req: RebalanceTransferRequest) -> Dict[str, Any]:
        if req.from_jar_id == req.to_jar_id:
            raise ValueError("Source and destination jars must be different")
        budget = self._compute_budget()
        by_id = {j.id: j for j in budget.jars}
        if req.from_jar_id not in by_id or req.to_jar_id not in by_id:
            raise ValueError("Unknown jar ID")
        src = by_id[req.from_jar_id]
        dst = by_id[req.to_jar_id]
        if src.kind == JarKind.BILLS:
            raise ValueError("Protected Bills & Essentials jar cannot be drained via quick rebalance")
        if src.available_cents < req.amount_cents:
            raise ValueError(
                f"{src.name} only has ${max(0, src.available_cents) / 100:,.2f} available to move"
            )

        ym = current_cycle_ym()
        tr = self.storage.record_budget_transfer(
            cycle_ym=ym,
            from_jar_id=req.from_jar_id,
            to_jar_id=req.to_jar_id,
            amount_cents=req.amount_cents,
            reason=req.reason,
        )
        self.storage.log_budget_event(
            "rebalance",
            f"Shifted ${req.amount_cents / 100:,.2f} from {src.name} -> {dst.name}",
            {"from_jar_id": src.id, "to_jar_id": dst.id, "amount_cents": req.amount_cents},
        )
        return tr

    # --- Recurring Bills ---

    def create_recurring_bill(self, req: RecurringBillCreate) -> Dict[str, Any]:
        norm = normalize_merchant(req.name)
        jar_id = req.jar_id
        if jar_id is None:
            bills_jar = self.storage.get_jar_by_slug("bills")
            jar_id = int(bills_jar["id"]) if bills_jar else None
        saved = self.storage.upsert_recurring_bill(
            name=req.name.strip(),
            normalized_merchant=norm,
            amount_cents=req.amount_cents,
            amount_min_cents=req.amount_min_cents,
            amount_max_cents=req.amount_max_cents,
            due_window_days=req.due_window_days,
            cadence=req.cadence.value,
            next_due_date=req.next_due_date,
            jar_id=jar_id,
            is_subscription=req.is_subscription,
            is_essential=req.is_essential,
            auto_pay=req.auto_pay,
            confidence=1.0,
        )
        self.run_self_updating_sync(reason=f"Added recurring bill {saved['name']}")
        return saved

    def deactivate_recurring_bill(self, bill_id: int) -> bool:
        ok = self.storage.deactivate_recurring_bill(bill_id)
        if ok:
            self.run_self_updating_sync(reason=f"Cancelled/archived recurring bill #{bill_id}")
        return ok

    def flag_zombie_bill(self, bill_id: int, flagged: bool = True) -> Optional[Dict[str, Any]]:
        updated = self.storage.flag_zombie_bill(bill_id, flagged=flagged)
        if updated:
            self.storage.log_budget_event(
                "subscription_review",
                f"Flagged '{updated['name']}' (${updated['amount_cents'] / 100:,.2f}) for review/cancellation",
            )
        return updated

    # --- Impulse Cooling-Off Vault ---

    def create_impulse(self, req: ImpulseCreate) -> ImpulseOut:
        settings = self.storage.get_settings()
        budget = self._compute_budget()
        raw = self.storage.create_impulse(
            item_name=req.item_name.strip(),
            price_cents=req.price_cents,
            reason=req.reason.strip(),
            url=req.url.strip(),
            cooling_hours=req.cooling_hours,
        )
        prog = self.storage.get_adhd_progress()
        wins = [f"Parked '{req.item_name.strip()}' (${req.price_cents / 100:,.2f}) for {req.cooling_hours}h cooldown +15 XP"] + prog["recent_wins"]
        self.storage.update_adhd_progress(
            {"dopamine_xp": int(prog["dopamine_xp"]) + 15, "recent_wins": wins}
        )
        return enrich_impulse(
            raw,
            hourly_wage_cents=int(settings.get("hourly_wage_cents", 3000)),
            safe_to_spend_cents=budget.safe_to_spend.safe_to_spend_cents,
        )

    def list_impulses(self, include_resolved: bool = False) -> List[ImpulseOut]:
        settings = self.storage.get_settings()
        budget = self._compute_budget()
        return [
            enrich_impulse(
                raw,
                hourly_wage_cents=int(settings.get("hourly_wage_cents", 3000)),
                safe_to_spend_cents=budget.safe_to_spend.safe_to_spend_cents,
            )
            for raw in self.storage.list_impulses(include_resolved=include_resolved)
        ]

    def decide_impulse(self, impulse_id: int, req: ImpulseDecision) -> Dict[str, Any]:
        raw = self.storage.get_impulse(impulse_id)
        if not raw:
            raise ValueError("Impulse item not found")
        if raw["status"] in (ImpulseStatus.SKIPPED.value, ImpulseStatus.BOUGHT.value):
            raise ValueError("Impulse item has already been decided")

        price_cents = int(raw["price_cents"])
        if req.decision == "skip":
            resolved = self.storage.resolve_impulse(impulse_id, ImpulseStatus.SKIPPED.value)
            swept_cents = 0
            if req.sweep_savings_pct > 0:
                budget = self._compute_budget()
                fun_jar = next((j for j in budget.jars if j.kind == JarKind.FUN), None)
                safety_jar = next((j for j in budget.jars if j.kind == JarKind.SAFETY), None)
                if fun_jar and safety_jar and fun_jar.available_cents > 0:
                    desired = int(round(price_cents * (req.sweep_savings_pct / 100.0)))
                    swept_cents = min(desired, fun_jar.available_cents)
                    if swept_cents > 0:
                        self.storage.record_budget_transfer(
                            cycle_ym=current_cycle_ym(),
                            from_jar_id=fun_jar.id,
                            to_jar_id=safety_jar.id,
                            amount_cents=swept_cents,
                            reason=f"Impulse skip sweep ({raw['item_name']})",
                        )

            prog = self.storage.get_adhd_progress()
            xp_gain = 75
            sweep_note = f" (swept ${swept_cents / 100:,.2f} to Safety & Future!)" if swept_cents > 0 else ""
            wins = [
                f"Skipped '{raw['item_name']}' and kept ${price_cents / 100:,.2f}{sweep_note} +{xp_gain} XP"
            ] + prog["recent_wins"]
            self.storage.update_adhd_progress(
                {
                    "impulses_skipped_count": int(prog["impulses_skipped_count"]) + 1,
                    "impulse_money_saved_cents": int(prog["impulse_money_saved_cents"]) + price_cents,
                    "dopamine_xp": int(prog["dopamine_xp"]) + xp_gain,
                    "recent_wins": wins,
                }
            )
            self.storage.log_budget_event(
                "impulse_skipped",
                f"Dopamine win: skipped '{raw['item_name']}' (${price_cents / 100:,.2f} saved{sweep_note})",
            )
            return {
                "status": "skipped",
                "saved_cents": price_cents,
                "swept_cents": swept_cents,
                "xp_earned": xp_gain,
                "message": f"Huge win! You kept ${price_cents / 100:,.2f}{sweep_note} +{xp_gain} XP!",
                "impulse": resolved,
            }

        fun_jar = self.storage.get_jar_by_slug("fun")
        tx = self.add_transaction_raw(
            account_id=req.account_id,
            jar_id=int(fun_jar["id"]) if fun_jar else None,
            tx_type=TransactionType.EXPENSE.value,
            amount_cents=price_cents,
            merchant=raw["item_name"],
            subcategory="Planned Purchase",
            memo="Intentional buy after Impulse Vault cooldown",
            tx_date=date.today().isoformat(),
            source="impulse_vault",
        )
        resolved = self.storage.resolve_impulse(impulse_id, ImpulseStatus.BOUGHT.value)
        return {
            "status": "bought",
            "message": f"Logged intentional purchase of '{raw['item_name']}' (${price_cents / 100:,.2f}) and updated your budget.",
            "impulse": resolved,
            "transaction": tx.model_dump() if tx else None,
        }

    # --- 5-Minute Weekly Money Reset ---

    def get_weekly_reset_summary(self) -> WeeklyResetSummary:
        budget = self._compute_budget()
        triage = self.list_transactions(limit=25, needs_review_only=True)
        upcoming_14d = [b for b in budget.upcoming_bills if b.days_until_due <= 14]
        zombies = [
            b
            for b in budget.upcoming_bills
            if b.flagged_zombie or b.price_hike_cents > 0 or (b.is_subscription and not b.is_essential)
        ]
        matured = [i for i in self.list_impulses(include_resolved=False) if i.status == ImpulseStatus.READY]
        prog = self.storage.get_adhd_progress()
        return WeeklyResetSummary(
            needs_triage_transactions=triage,
            upcoming_bills_14d=upcoming_14d,
            zombie_subscriptions=zombies,
            matured_impulses=matured,
            rebalance_plans=budget.rebalance_plans,
            adaptive_target_updates=budget.adaptive_target_updates,
            already_completed_this_week=is_reset_done_this_week(prog.get("last_reset_date")),
        )

    def complete_weekly_reset(self, apply_rebalances: bool = True) -> Dict[str, Any]:
        applied_transfers = 0
        if apply_rebalances:
            budget = self._compute_budget()
            for plan in budget.rebalance_plans:
                try:
                    self.rebalance_jars(
                        RebalanceTransferRequest(
                            from_jar_id=plan.from_jar_id,
                            to_jar_id=plan.to_jar_id,
                            amount_cents=plan.amount_cents,
                            reason="Weekly Money Reset auto-rebalance",
                        )
                    )
                    applied_transfers += 1
                except ValueError:
                    continue

        prog = self.storage.get_adhd_progress()
        updated_prog = self.storage.update_adhd_progress(compute_weekly_reset_update(prog))
        sync = self.run_self_updating_sync(reason="Weekly Money Reset completed")
        return {
            "applied_rebalances": applied_transfers,
            "adhd_stats": build_adhd_stats(updated_prog).model_dump(),
            "sync": sync,
        }

    # --- Multi-Mode Intelligence, Multi-Provider AI Coach & Guarded Agentic Tool Coach ---

    def _build_agent_ledger_context(
        self,
        *,
        budget: Optional[AutoBudgetResult] = None,
        quant_forecast: Optional[QuantForecastReport] = None,
    ) -> AgentLedgerContext:
        settings = self.storage.get_settings()
        b = budget if budget is not None else self._compute_budget()
        qf = quant_forecast if quant_forecast is not None else self.get_quant_forecast(days=30, budget=b)
        return AgentLedgerContext.from_objects(
            accounts=self.storage.list_accounts(),
            jars=self.storage.list_jars(),
            transactions=self.storage.list_transactions(limit=250, include_splits=True, include_tags=True),
            goals=self.storage.list_goals(include_completed=True),
            rules=self.storage.list_rules(active_only=False),
            credit_lines=self.storage.list_credit_lines(active_only=True),
            holdings=self.storage.list_holdings(),
            email_queue=self.storage.list_email_queue(status="pending", limit=25),
            bills=self.storage.list_recurring_bills(active_only=True),
            safe_to_spend_cents=b.safe_to_spend.safe_to_spend_cents,
            safe_to_invest_cents=b.safe_to_invest_cents,
            monthly_surplus_cents=qf.monthly_surplus_cents,
            daily_safe_to_spend_cents=b.safe_to_spend.daily_safe_to_spend_cents,
            days_until_payday=b.safe_to_spend.days_until_payday,
            hourly_wage_cents=int(settings.get("hourly_wage_cents", 3000)),
        )

    def plan_agent_batch(
        self,
        req: AgentPlanRequest,
        *,
        mode_override: Optional[str] = None,
    ) -> AgentPlanResponse:
        settings = self.storage.get_settings()
        mode = self._resolve_intelligence_mode(settings, override=mode_override)
        provider_override = req.provider.value if req.provider is not None else None
        max_daily = int(settings.get("opencode_max_daily_requests", 60))
        used = self.storage.count_ai_requests_today()
        remaining = max(0, max_daily - used)

        ctx = self._build_agent_ledger_context()
        clients = [] if mode == IntelligenceMode.DETERMINISTIC_ONLY else self._build_coach_clients_list(provider_override)
        planner = AgentActionPlanner(clients=clients, on_completion=self._record_completion_usage)
        plan = planner.plan(
            prompt=req.prompt,
            context=ctx,
            intelligence_mode=mode,
            quota_remaining=remaining,
        )
        saved = self.storage.create_agent_batch(
            {
                "prompt": plan.prompt,
                "provider": plan.provider_used or plan.source,
                "model": plan.model_used or "",
                "actions": [a.to_dict() for a in plan.actions],
                "undo_snapshot": {},
                "status": "preview",
            }
        )
        return plan.to_plan_response(batch_id=int(saved["id"]))

    def list_agent_batches(self, limit: int = 30) -> List[Dict[str, Any]]:
        return self.storage.list_agent_batches(limit=limit)

    def execute_agent_batch(self, batch_id: int) -> AgentExecuteResponse:
        """
        Execute a validated Preview batch inside ONE atomic `BEGIN IMMEDIATE` SQLite transaction,
        capturing exact row pre-images and post-execution row versions for safe Undo.
        """
        batch = self.storage.get_agent_batch(batch_id)
        if not batch:
            raise ValueError(f"Agent batch #{batch_id} not found")
        if batch["status"] != "preview":
            raise ValueError(f"Agent batch #{batch_id} is already '{batch['status']}' and cannot be re-executed.")

        raw_actions = list(batch.get("actions") or [])
        if not raw_actions:
            raise ValueError(f"Agent batch #{batch_id} contains no executable actions.")

        ctx = self._build_agent_ledger_context()
        reval = validate_agent_batch(raw_actions, ctx)
        if not reval.actions:
            raise ValueError(
                "No valid actions remained after re-validation: " + "; ".join(reval.rejection_reasons[:3])
            )

        tx_fp_fields = (
            "account_id",
            "jar_id",
            "tx_type",
            "amount_cents",
            "merchant",
            "normalized_merchant",
            "subcategory",
            "memo",
            "tx_date",
        )
        goal_fp_fields = (
            "name",
            "target_cents",
            "saved_cents",
            "target_date",
            "priority",
            "priority_order",
            "status",
            "notes",
            "deleted_at",
        )
        rule_fp_fields = (
            "group_id",
            "name",
            "priority",
            "strict_and",
            "stop_processing",
            "triggers",
            "actions",
            "active",
        )
        cl_fp_fields = (
            "name",
            "issuer",
            "balance_cents",
            "credit_limit_cents",
            "apr_bps",
            "min_payment_cents",
            "statement_closing_day",
            "payment_due_day",
            "promo_apr_bps",
            "promo_ends_date",
            "opened_date",
            "has_grace_period",
            "autopay_mode",
            "rewards_note",
            "is_authorized_user",
        )
        results: List[Dict[str, Any]] = []
        undo_entries: List[Dict[str, Any]] = []

        with self.storage.transaction(immediate=True) as conn:
            for act in reval.actions:
                atype = act.action_type
                pl = dict(act.payload)

                if atype == "create_transaction":
                    acct_id = int(pl["account_id"]) if pl.get("account_id") is not None else self.storage.get_default_account_id()
                    merchant = str(pl["merchant"]).strip()
                    tx_type = str(pl.get("tx_type", "expense"))
                    amt = int(pl["amount_cents"])
                    jar_id = int(pl["jar_id"]) if pl.get("jar_id") is not None else None
                    if tx_type == "expense" and jar_id is None:
                        jars_by_slug = {j["slug"]: j for j in self.storage.list_jars()}
                        learned = self.storage.get_merchant_rules()
                        jar_id, auto_sub, _, _ = self.categorizer.categorize(
                            merchant, str(pl.get("memo", "")), jars_by_slug, learned
                        )
                        if not pl.get("subcategory"):
                            pl["subcategory"] = auto_sub
                    row = self.storage.insert_transaction(
                        account_id=acct_id,
                        jar_id=jar_id,
                        tx_type=tx_type,
                        amount_cents=amt,
                        merchant=merchant,
                        normalized_merchant=normalize_merchant(merchant),
                        subcategory=str(pl.get("subcategory", "")),
                        memo=str(pl.get("memo", "")),
                        tx_date=str(pl.get("tx_date") or date.today().isoformat()),
                        confidence=1.0,
                        needs_review=False,
                        source="ai_agent",
                        transfer_direction=str(pl.get("transfer_direction") or ""),
                        adjust_balance=bool(pl.get("affects_balance", True)),
                    )
                    assert row is not None
                    inline_splits = pl.get("splits")
                    if isinstance(inline_splits, list) and len(inline_splits) >= 2:
                        valid_inline = validate_splits(amt, inline_splits)
                        self.storage.set_transaction_splits(int(row["id"]), valid_inline)
                    undo_entries.append(
                        {
                            "op": "delete_tx",
                            "table": "transactions",
                            "id": int(row["id"]),
                            "after_hash": _row_fingerprint(row, tx_fp_fields),
                        }
                    )
                    results.append({"action_type": atype, "transaction_id": int(row["id"]), "summary": act.summary})

                elif atype == "update_transaction":
                    tx_id = int(pl["transaction_id"])
                    before = self.storage.get_transaction(tx_id)
                    if not before:
                        raise ValueError(f"Transaction #{tx_id} no longer exists")
                    upd: Dict[str, Any] = {}
                    for k in ("jar_id", "amount_cents", "tx_type", "tx_date", "subcategory", "memo"):
                        if k in pl and pl[k] is not None:
                            upd[k] = pl[k]
                    if "merchant" in pl and pl["merchant"]:
                        upd["merchant"] = str(pl["merchant"]).strip()
                        upd["normalized_merchant"] = normalize_merchant(upd["merchant"])
                    after = self.storage.update_transaction(tx_id, upd)
                    assert after is not None
                    undo_entries.append(
                        {
                            "op": "restore_tx",
                            "table": "transactions",
                            "id": tx_id,
                            "before_row": {k: before.get(k) for k in tx_fp_fields},
                            "after_hash": _row_fingerprint(after, tx_fp_fields),
                        }
                    )
                    results.append({"action_type": atype, "transaction_id": tx_id, "summary": act.summary})

                elif atype == "add_tx_comment":
                    tx_id = int(pl["transaction_id"])
                    saved_c = self.storage.add_transaction_comment(
                        tx_id,
                        str(pl["body"]),
                        author=str(pl.get("author") or "ai_coach"),
                    )
                    undo_entries.append(
                        {
                            "op": "delete_comment",
                            "table": "transaction_comments",
                            "id": int(saved_c["id"]),
                            "tx_id": tx_id,
                            "after_hash": _row_fingerprint(saved_c, ("tx_id", "author", "body")),
                        }
                    )
                    results.append(
                        {"action_type": atype, "comment_id": int(saved_c["id"]), "transaction_id": tx_id, "summary": act.summary}
                    )

                elif atype == "split_transaction":
                    tx_id = int(pl["transaction_id"])
                    parent = self.storage.get_transaction(tx_id)
                    if not parent:
                        raise ValueError(f"Transaction #{tx_id} not found")
                    before_splits = [
                        {
                            "jar_id": s.get("jar_id"),
                            "goal_id": s.get("goal_id"),
                            "amount_cents": int(s["amount_cents"]),
                            "memo": str(s.get("memo", "")),
                        }
                        for s in self.storage.get_transaction_splits(tx_id)
                    ]
                    valid_splits = validate_splits(int(parent["amount_cents"]), pl["splits"])
                    after_splits = self.storage.set_transaction_splits(tx_id, valid_splits)
                    undo_entries.append(
                        {
                            "op": "restore_splits",
                            "table": "transaction_splits",
                            "id": tx_id,
                            "before_splits": before_splits,
                            "after_splits_hash": _splits_fingerprint(after_splits),
                        }
                    )
                    results.append(
                        {"action_type": atype, "transaction_id": tx_id, "split_count": len(after_splits), "summary": act.summary}
                    )

                elif atype == "create_goal":
                    created_g = self.storage.create_goal(pl)
                    undo_entries.append(
                        {
                            "op": "delete_goal",
                            "table": "goals",
                            "id": int(created_g["id"]),
                            "after_updated_at": str(created_g["updated_at"]),
                            "after_hash": _row_fingerprint(created_g, goal_fp_fields),
                        }
                    )
                    results.append({"action_type": atype, "goal_id": int(created_g["id"]), "summary": act.summary})

                elif atype == "update_goal":
                    gid = int(pl["goal_id"])
                    before_g = self.storage.get_goal(gid)
                    if not before_g:
                        raise ValueError(f"Goal #{gid} not found")
                    upd_g = {k: v for k, v in pl.items() if k != "goal_id" and v is not None}
                    after_g = self.storage.update_goal(gid, upd_g)
                    assert after_g is not None
                    undo_entries.append(
                        {
                            "op": "restore_goal",
                            "table": "goals",
                            "id": gid,
                            "before_row": {
                                k: before_g.get(k)
                                for k in ("name", "target_cents", "saved_cents", "target_date", "priority", "priority_order", "status", "notes")
                            },
                            "after_updated_at": str(after_g["updated_at"]),
                            "after_hash": _row_fingerprint(after_g, goal_fp_fields),
                        }
                    )
                    results.append({"action_type": atype, "goal_id": gid, "summary": act.summary})

                elif atype == "delete_goal":
                    gid = int(pl["goal_id"])
                    deleted_g = self.storage.soft_delete_goal(gid)
                    if not deleted_g:
                        raise ValueError(f"Goal #{gid} not found or already deleted")
                    undo_entries.append(
                        {
                            "op": "restore_soft_deleted_goal",
                            "table": "goals",
                            "id": gid,
                            "after_updated_at": str(deleted_g["updated_at"]),
                            "after_hash": _row_fingerprint(deleted_g, goal_fp_fields),
                        }
                    )
                    results.append({"action_type": atype, "goal_id": gid, "summary": act.summary})

                elif atype == "create_rule":
                    self._validate_rule_triggers(pl.get("triggers") or [])
                    created_r = self.storage.create_rule(pl)
                    undo_entries.append(
                        {
                            "op": "delete_rule",
                            "table": "automation_rules",
                            "id": int(created_r["id"]),
                            "after_updated_at": str(created_r["updated_at"]),
                            "after_hash": _row_fingerprint(created_r, rule_fp_fields),
                        }
                    )
                    results.append({"action_type": atype, "rule_id": int(created_r["id"]), "summary": act.summary})

                elif atype == "update_rule":
                    rid = int(pl["rule_id"])
                    before_r = self.storage.get_rule(rid)
                    if not before_r:
                        raise ValueError(f"Rule #{rid} not found")
                    if pl.get("triggers") is not None:
                        self._validate_rule_triggers(pl["triggers"])
                    upd_r = {k: v for k, v in pl.items() if k != "rule_id" and v is not None}
                    after_r = self.storage.update_rule(rid, upd_r)
                    assert after_r is not None
                    undo_entries.append(
                        {
                            "op": "restore_rule",
                            "table": "automation_rules",
                            "id": rid,
                            "before_row": {k: before_r.get(k) for k in rule_fp_fields},
                            "after_updated_at": str(after_r["updated_at"]),
                            "after_hash": _row_fingerprint(after_r, rule_fp_fields),
                        }
                    )
                    results.append({"action_type": atype, "rule_id": rid, "summary": act.summary})

                elif atype == "create_credit_line":
                    created_cl = self.storage.create_credit_line(pl)
                    undo_entries.append(
                        {
                            "op": "delete_credit_line",
                            "table": "credit_lines",
                            "id": int(created_cl["id"]),
                            "after_updated_at": str(created_cl["updated_at"]),
                            "after_hash": _row_fingerprint(created_cl, cl_fp_fields),
                        }
                    )
                    results.append({"action_type": atype, "credit_line_id": int(created_cl["id"]), "summary": act.summary})

                elif atype == "update_credit_line":
                    clid = int(pl["credit_line_id"])
                    before_cl = self.storage.get_credit_line(clid)
                    if not before_cl:
                        raise ValueError(f"Credit line #{clid} not found")
                    upd_cl = {k: v for k, v in pl.items() if k != "credit_line_id" and v is not None}
                    after_cl = self.storage.update_credit_line(clid, upd_cl)
                    assert after_cl is not None
                    undo_entries.append(
                        {
                            "op": "restore_credit_line",
                            "table": "credit_lines",
                            "id": clid,
                            "before_row": {k: before_cl.get(k) for k in cl_fp_fields},
                            "after_updated_at": str(after_cl["updated_at"]),
                            "after_hash": _row_fingerprint(after_cl, cl_fp_fields),
                        }
                    )
                    results.append({"action_type": atype, "credit_line_id": clid, "summary": act.summary})

                elif atype == "analyze_portfolio":
                    analysis = pl.get("analysis") or compute_portfolio_analysis(
                        ctx, focus=str(pl.get("focus") or "full"), symbols=pl.get("symbols")
                    )
                    results.append({"action_type": atype, "analysis": analysis, "summary": act.summary})

            # Persist executed status + undo pre-images inside the same atomic transaction
            conn.execute(
                "UPDATE agent_action_batches SET status = 'executed', undo_snapshot_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps({"version": 1, "entries": undo_entries}), utc_now_iso(), batch_id),
            )

        self.storage.log_budget_event(
            "agent_batch_executed",
            f"Executed agent batch #{batch_id} ({len(results)} actions)",
            {"batch_id": batch_id},
        )
        self.run_self_updating_sync(reason=f"Executed agent batch #{batch_id} ({len(results)} actions)")
        return AgentExecuteResponse(
            batch_id=batch_id,
            status="executed",
            applied_count=len(results),
            results=results,
            undo_available=bool(undo_entries),
        )

    def undo_agent_batch(self, batch_id: int) -> AgentUndoResponse:
        """
        Row-versioned atomic Undo for an executed agent batch (Gate 1 Amendment #53).
        Verifies EVERY affected row's post-execute version/fingerprint BEFORE mutating anything;
        refuses cleanly with a structured conflict diff if any row changed since execute.
        """
        batch = self.storage.get_agent_batch(batch_id)
        if not batch:
            raise ValueError(f"Agent batch #{batch_id} not found")
        if batch["status"] != "executed":
            raise ValueError(f"Agent batch #{batch_id} has status '{batch['status']}' and cannot be undone.")

        snapshot = batch.get("undo_snapshot") or {}
        entries: List[Dict[str, Any]] = list(snapshot.get("entries") or [])
        tx_fp_fields = (
            "account_id",
            "jar_id",
            "tx_type",
            "amount_cents",
            "merchant",
            "normalized_merchant",
            "subcategory",
            "memo",
            "tx_date",
        )
        goal_fp_fields = (
            "name",
            "target_cents",
            "saved_cents",
            "target_date",
            "priority",
            "priority_order",
            "status",
            "notes",
            "deleted_at",
        )
        rule_fp_fields = (
            "group_id",
            "name",
            "priority",
            "strict_and",
            "stop_processing",
            "triggers",
            "actions",
            "active",
        )
        cl_fp_fields = (
            "name",
            "issuer",
            "balance_cents",
            "credit_limit_cents",
            "apr_bps",
            "min_payment_cents",
            "statement_closing_day",
            "payment_due_day",
            "promo_apr_bps",
            "promo_ends_date",
            "opened_date",
            "has_grace_period",
            "autopay_mode",
            "rewards_note",
            "is_authorized_user",
        )

        with self.storage.transaction(immediate=True) as conn:
            conflicts: List[Dict[str, Any]] = []

            # Phase 1: Check ALL row versions & content hashes before touching a single row
            for entry in reversed(entries):
                op = entry["op"]
                row_id = int(entry["id"])

                if op in ("delete_tx", "restore_tx"):
                    curr = self.storage.get_transaction(row_id)
                    curr_hash = _row_fingerprint(curr, tx_fp_fields)
                    if not curr:
                        conflicts.append(
                            {
                                "table": "transactions",
                                "id": row_id,
                                "reason": f"Transaction #{row_id} was deleted after batch execution.",
                            }
                        )
                    elif curr_hash != entry["after_hash"]:
                        conflicts.append(
                            {
                                "table": "transactions",
                                "id": row_id,
                                "reason": f"Transaction #{row_id} was modified after batch execution.",
                                "expected_hash": entry["after_hash"][:12],
                                "actual_hash": curr_hash[:12],
                                "current": {k: curr.get(k) for k in ("merchant", "amount_cents", "jar_id", "memo")},
                            }
                        )

                elif op == "delete_comment":
                    row = conn.execute("SELECT * FROM transaction_comments WHERE id = ?", (row_id,)).fetchone()
                    curr = dict(row) if row else None
                    curr_hash = _row_fingerprint(curr, ("tx_id", "author", "body"))
                    if not curr or curr_hash != entry["after_hash"]:
                        conflicts.append(
                            {
                                "table": "transaction_comments",
                                "id": row_id,
                                "reason": f"Comment #{row_id} changed or was removed after execution.",
                            }
                        )

                elif op == "restore_splits":
                    curr_splits = self.storage.get_transaction_splits(row_id)
                    curr_hash = _splits_fingerprint(curr_splits)
                    if curr_hash != entry["after_splits_hash"]:
                        conflicts.append(
                            {
                                "table": "transaction_splits",
                                "id": row_id,
                                "reason": f"Splits on transaction #{row_id} were modified after execution.",
                                "current_splits_count": len(curr_splits),
                            }
                        )

                elif op in ("delete_goal", "restore_goal", "restore_soft_deleted_goal"):
                    curr = self.storage.get_goal(row_id, include_deleted=True)
                    curr_hash = _row_fingerprint(curr, goal_fp_fields)
                    expected_hash = entry.get("after_hash")
                    if not curr:
                        conflicts.append(
                            {
                                "table": "goals",
                                "id": row_id,
                                "reason": f"Goal #{row_id} was permanently removed after execution.",
                            }
                        )
                    elif (
                        str(curr.get("updated_at", "")) != str(entry["after_updated_at"])
                        or (expected_hash and curr_hash != expected_hash)
                    ):
                        conflicts.append(
                            {
                                "table": "goals",
                                "id": row_id,
                                "reason": f"Goal #{row_id} ('{curr.get('name', '')}') was modified after execution.",
                                "expected_updated_at": entry["after_updated_at"],
                                "actual_updated_at": curr.get("updated_at"),
                                "current": {
                                    "name": curr.get("name"),
                                    "target_cents": curr.get("target_cents"),
                                    "saved_cents": curr.get("saved_cents"),
                                    "notes": curr.get("notes"),
                                    "deleted_at": curr.get("deleted_at"),
                                },
                            }
                        )

                elif op in ("delete_rule", "restore_rule"):
                    curr = self.storage.get_rule(row_id)
                    curr_hash = _row_fingerprint(curr, rule_fp_fields)
                    expected_hash = entry.get("after_hash")
                    if (
                        not curr
                        or str(curr.get("updated_at", "")) != str(entry["after_updated_at"])
                        or (expected_hash and curr_hash != expected_hash)
                    ):
                        conflicts.append(
                            {
                                "table": "automation_rules",
                                "id": row_id,
                                "reason": f"Rule #{row_id} was modified or deleted after execution.",
                                "expected_updated_at": entry["after_updated_at"],
                                "actual_updated_at": curr.get("updated_at") if curr else None,
                            }
                        )

                elif op in ("delete_credit_line", "restore_credit_line"):
                    curr = self.storage.get_credit_line(row_id)
                    curr_hash = _row_fingerprint(curr, cl_fp_fields)
                    expected_hash = entry.get("after_hash")
                    if (
                        not curr
                        or str(curr.get("updated_at", "")) != str(entry["after_updated_at"])
                        or (expected_hash and curr_hash != expected_hash)
                    ):
                        conflicts.append(
                            {
                                "table": "credit_lines",
                                "id": row_id,
                                "reason": f"Credit line #{row_id} was modified or deleted after execution.",
                                "expected_updated_at": entry["after_updated_at"],
                                "actual_updated_at": curr.get("updated_at") if curr else None,
                            }
                        )

            if conflicts:
                return AgentUndoResponse(
                    batch_id=batch_id,
                    status="conflict",
                    restored_count=0,
                    conflicts=conflicts,
                    message=f"Undo refused: {len(conflicts)} row(s) changed since batch execution.",
                )

            # Phase 2: All versions verified — restore in reverse order atomically
            restored_count = 0
            for entry in reversed(entries):
                op = entry["op"]
                row_id = int(entry["id"])

                if op == "delete_tx":
                    self.storage.delete_transaction(row_id)
                    restored_count += 1
                elif op == "restore_tx":
                    self.storage.update_transaction(row_id, dict(entry["before_row"]))
                    restored_count += 1
                elif op == "delete_comment":
                    conn.execute("DELETE FROM transaction_comments WHERE id = ?", (row_id,))
                    restored_count += 1
                elif op == "restore_splits":
                    before_splits = entry.get("before_splits") or []
                    if before_splits:
                        self.storage.set_transaction_splits(row_id, before_splits)
                    else:
                        self.storage.clear_transaction_splits(row_id)
                    restored_count += 1
                elif op == "delete_goal":
                    self.storage.delete_goal(row_id)
                    restored_count += 1
                elif op == "restore_goal":
                    self.storage.update_goal(row_id, dict(entry["before_row"]))
                    restored_count += 1
                elif op == "restore_soft_deleted_goal":
                    self.storage.restore_goal(row_id)
                    restored_count += 1
                elif op == "delete_rule":
                    self.storage.delete_rule(row_id)
                    restored_count += 1
                elif op == "restore_rule":
                    self.storage.update_rule(row_id, dict(entry["before_row"]))
                    restored_count += 1
                elif op == "delete_credit_line":
                    self.storage.delete_credit_line(row_id)
                    restored_count += 1
                elif op == "restore_credit_line":
                    self.storage.update_credit_line(row_id, dict(entry["before_row"]))
                    restored_count += 1

            conn.execute(
                "UPDATE agent_action_batches SET status = 'undone', updated_at = ? WHERE id = ?",
                (utc_now_iso(), batch_id),
            )

        self.run_self_updating_sync(reason=f"Undid agent batch #{batch_id}")
        return AgentUndoResponse(
            batch_id=batch_id,
            status="undone",
            restored_count=restored_count,
            conflicts=[],
            message=f"Undid {restored_count} action(s) from batch #{batch_id} cleanly.",
        )

    def undo_last_agent_batch(self) -> AgentUndoResponse:
        """Undo the most recently executed agent action batch in 1 step."""
        batches = self.storage.list_agent_batches(limit=50)
        latest = next((b for b in batches if b.get("status") == "executed"), None)
        if not latest:
            raise ValueError("No executed agent action batch is available to undo.")
        return self.undo_agent_batch(int(latest["id"]))

    def say(
        self,
        text: str,
        *,
        auto_commit: bool = True,
        mode_override: Optional[str] = None,
        provider_override: Optional[str] = None,
        completion_fn: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Unified natural-language AI Harness (`POST /api/v1/ai/say` & `chronox --say`).
        Handles single-step capture with causal reason preservation, savings transfers,
        exact-sum splits, Q&A, report export requests, and One-Tap optimization triggers.
        """
        settings = self.storage.get_settings()
        mode = self._resolve_intelligence_mode(settings, override=mode_override)
        max_daily = int(settings.get("opencode_max_daily_requests", 60))
        used = self.storage.count_ai_requests_today()
        remaining = max(0, max_daily - used)

        budget = self._compute_budget()
        credit_report = self.get_credit_report()
        goals = self.list_goals(include_completed=False)
        qf = self.get_quant_forecast(
            days=30,
            budget=budget,
            credit_report=credit_report,
            goals=goals,
        )
        ctx = self._build_agent_ledger_context(budget=budget, quant_forecast=qf)
        dashboard = self.get_dashboard()

        clients = (
            []
            if mode == IntelligenceMode.DETERMINISTIC_ONLY
            else self._build_coach_clients_list(provider_override=provider_override)
        )
        harness = FinancialAIHarness(
            clients=clients,
            completion_fn=completion_fn,
            on_completion=self._record_completion_usage,
        )
        result = harness.process_utterance(
            text,
            context=ctx,
            quant_snapshot=qf,
            dashboard_snapshot=dashboard,
            intelligence_mode=mode,
            auto_commit=bool(auto_commit),
            quota_remaining=remaining,
        )

        onetap_plan: Optional[Dict[str, Any]] = None
        if result.intent_kind == "onetap_optimize" or result.metadata.trigger_onetap:
            onetap_plan = self.run_onetap_optimization(
                horizon_days=30,
                apply=False,
                mode_override=mode_override,
                provider_override=provider_override,
                completion_fn=completion_fn,
            )

        export_format: Optional[str] = None
        export_download_url: Optional[str] = None
        if result.intent_kind == "export_request" or result.metadata.export_format:
            low_utterance = (text or "").lower()
            raw_fmt = result.metadata.export_format
            if raw_fmt == "csv" and any(
                kw in low_utterance for kw in ("pack", "zip", "all tables", "full csv", "executive csv")
            ):
                export_format = "csv_pack"
            elif raw_fmt in ("numbers", "csv"):
                export_format = raw_fmt
            else:
                export_format = "numbers"

            if export_format == "numbers":
                export_download_url = "/api/v1/export/numbers"
            elif export_format == "csv_pack":
                export_download_url = "/api/v1/export/csv?pack=true"
            else:
                export_download_url = "/api/v1/export/csv"

        batch_id: Optional[int] = None
        plan_dict: Optional[Dict[str, Any]] = None
        execution_dict: Optional[Dict[str, Any]] = None
        executed = False
        requires_confirmation = False
        undo_available = False

        if result.actions:
            saved = self.storage.create_agent_batch(
                {
                    "prompt": result.utterance,
                    "provider": result.provider_used or result.source,
                    "model": result.model_used or "",
                    "actions": [a.model_dump() for a in result.actions],
                    "undo_snapshot": {},
                    "status": "preview",
                }
            )
            batch_id = int(saved["id"])
            plan_resp = result.to_agent_plan_response(batch_id=batch_id)
            policy = result.execution_policy
            if auto_commit and policy.auto_execute_eligible and not policy.requires_confirmation:
                exec_res = self.execute_agent_batch(batch_id)
                executed = True
                requires_confirmation = False
                undo_available = bool(exec_res.undo_available)
                execution_dict = exec_res.model_dump()
                plan_resp.status = "executed"
            else:
                executed = False
                requires_confirmation = True
                undo_available = False
            plan_dict = plan_resp.model_dump()

        post_budget = self._compute_budget() if executed else budget
        return {
            "intent_kind": result.intent_kind,
            "assistant_message": result.assistant_message,
            "executed": executed,
            "requires_confirmation": requires_confirmation,
            "undo_available": undo_available,
            "batch_id": batch_id,
            "actions": [a.model_dump() for a in result.actions],
            "plan": plan_dict,
            "execution": execution_dict,
            "onetap_plan": onetap_plan,
            "export_format": export_format,
            "export_download_url": export_download_url,
            "provider_used": result.provider_used,
            "model_used": result.model_used,
            "source": result.source,
            "safe_to_spend_today_cents": post_budget.safe_to_spend.daily_safe_to_spend_cents,
            "safe_to_spend_cents": post_budget.safe_to_spend.safe_to_spend_cents,
        }

    # --- AI + Statistical Quant One-Tap Optimizer & Atomic Undo ---

    def run_onetap_optimization(
        self,
        *,
        horizon_days: int = 30,
        apply: bool = False,
        expected_fingerprint: Optional[str] = None,
        mode_override: Optional[str] = None,
        provider_override: Optional[str] = None,
        completion_fn: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Build a statistical CVaR/EWMA + KKT One-Tap optimization plan paired with
        a grounded executive narrative. When `apply=True`, validates optional
        `expected_fingerprint` (409 on stale plan), atomically commits all
        executable actions, and records a touched-rows-only, conflict-checked undo batch.
        """
        horizon = max(7, min(90, int(horizon_days or 30)))
        settings = self.storage.get_settings()
        budget = self._compute_budget()
        txs, splits_map = self._load_recent_transactions_with_splits(days=180)
        bills = self.storage.list_recurring_bills(active_only=True)
        credit_report = self.get_credit_report()
        goals = self.list_goals(include_completed=False)
        portfolio = self.get_portfolio()

        current_cap_raw = settings.get("safe_invest_cap_cents")
        current_cap = int(current_cap_raw) if current_cap_raw is not None else None

        plan = self.onetap_engine.build_onetap_plan(
            budget=budget,
            transactions=txs,
            recurring_bills=bills,
            settings=settings,
            credit_report=credit_report,
            goals=goals,
            holdings=portfolio.holdings,
            splits_by_tx_id=splits_map,
            current_trading_cap_cents=current_cap,
            horizon_days=horizon,
        )
        plan_fingerprint = hashlib.sha256(
            json.dumps(
                [a.model_dump() for a in plan.actions],
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:32]

        if apply and expected_fingerprint and expected_fingerprint.strip() != plan_fingerprint:
            raise PlanConflictError(
                "One-Tap plan changed since preview (e.g. a bank sync or edit occurred). "
                "Refresh the preview before applying."
            )

        mode = self._resolve_intelligence_mode(settings, override=mode_override)
        max_daily = int(settings.get("opencode_max_daily_requests", 60))
        used = self.storage.count_ai_requests_today()
        remaining = max(0, max_daily - used)
        clients = (
            []
            if mode == IntelligenceMode.DETERMINISTIC_ONLY
            else self._build_coach_clients_list(provider_override=provider_override)
        )
        harness = FinancialAIHarness(
            clients=clients,
            completion_fn=completion_fn,
            on_completion=self._record_completion_usage,
        )
        narrative = harness.synthesize_onetap_narrative(
            plan,
            intelligence_mode=mode,
            quota_remaining=remaining,
        )

        batch_id: Optional[int] = None
        applied = False
        undo_available = False

        if apply and not plan.needs_setup:
            executable_actions = [a for a in plan.actions if a.executable and not a.is_advisory]
            if executable_actions:
                with self.storage.transaction(immediate=True) as conn:
                    raw_jars_before = self.storage.list_jars()
                    jars_by_id = {int(j["id"]): j for j in raw_jars_before}
                    touched_settings_before: Dict[str, Any] = {}
                    touched_settings_after: Dict[str, Any] = {}
                    touched_jar_ids: set[int] = set()
                    created_transfer_ids: List[int] = []
                    goals_changes: Dict[int, Dict[str, Any]] = {}
                    applied_any = False

                    for act in executable_actions:
                        pl = dict(act.payload)
                        if act.action_type == "rebalance_jar":
                            from_id = int(pl["from_jar_id"])
                            to_id = int(pl["to_jar_id"])
                            amt = int(pl["amount_cents"])
                            src_jar = jars_by_id.get(from_id)
                            if (
                                src_jar is None
                                or str(src_jar.get("kind") or "").lower() == JarKind.BILLS.value
                                or str(src_jar.get("slug") or "").lower() == "bills"
                                or bool(src_jar.get("is_locked"))
                            ):
                                continue
                            if amt > 0 and from_id != to_id and to_id in jars_by_id:
                                tr = self.storage.record_budget_transfer(
                                    cycle_ym=current_cycle_ym(),
                                    from_jar_id=from_id,
                                    to_jar_id=to_id,
                                    amount_cents=amt,
                                    reason=str(pl.get("reason") or "One-Tap statistical rebalance"),
                                )
                                created_transfer_ids.append(int(tr["id"]))
                                touched_jar_ids.add(from_id)
                                touched_jar_ids.add(to_id)
                                applied_any = True

                        elif act.action_type == "set_emergency_buffer":
                            new_buf = max(0, int(pl["emergency_buffer_cents"]))
                            if "emergency_buffer_cents" not in touched_settings_before:
                                touched_settings_before["emergency_buffer_cents"] = int(
                                    settings.get("emergency_buffer_cents", 50_000)
                                )
                            conn.execute(
                                "UPDATE profile_settings SET emergency_buffer_cents = ?, updated_at = ? WHERE id = 1",
                                (new_buf, utc_now_iso()),
                            )
                            touched_settings_after["emergency_buffer_cents"] = new_buf
                            applied_any = True

                        elif act.action_type == "set_safe_invest_cap":
                            new_cap = max(0, int(pl["safe_to_invest_cents"]))
                            if "safe_invest_cap_cents" not in touched_settings_before:
                                touched_settings_before["safe_invest_cap_cents"] = settings.get(
                                    "safe_invest_cap_cents"
                                )
                            conn.execute(
                                "UPDATE profile_settings SET safe_invest_cap_cents = ?, updated_at = ? WHERE id = 1",
                                (new_cap, utc_now_iso()),
                            )
                            touched_settings_after["safe_invest_cap_cents"] = new_cap
                            self.storage.log_budget_event(
                                "trading_guardrail",
                                f"One-Tap set safe-to-invest cap at ${new_cap / 100:,.2f}",
                                {"safe_to_invest_cents": new_cap},
                            )
                            applied_any = True

                        elif act.action_type == "fund_goal":
                            gid = int(pl["goal_id"])
                            amt = max(0, int(pl["amount_cents"]))
                            if amt <= 0:
                                continue
                            g_before = self.storage.get_goal(gid)
                            if not g_before:
                                continue
                            if gid not in goals_changes:
                                goals_changes[gid] = {
                                    "goal_id": gid,
                                    "before_saved_cents": int(g_before["saved_cents"]),
                                    "before_status": str(g_before["status"]),
                                    "funded_amount_cents": 0,
                                    "after_saved_cents": int(g_before["saved_cents"]),
                                    "after_status": str(g_before["status"]),
                                }
                            new_saved = int(g_before["saved_cents"]) + amt
                            new_status = (
                                "funded"
                                if new_saved >= int(g_before["target_cents"])
                                else str(g_before["status"])
                            )
                            self.storage.update_goal(
                                gid,
                                {"saved_cents": new_saved, "status": new_status},
                            )
                            goals_changes[gid]["funded_amount_cents"] += amt
                            goals_changes[gid]["after_saved_cents"] = new_saved
                            goals_changes[gid]["after_status"] = new_status
                            applied_any = True

                    if applied_any:
                        touched_jars_before = [
                            {
                                "id": int(j["id"]),
                                "auto_allocated_cents": int(j["auto_allocated_cents"]),
                                "rollover_cents": int(j["rollover_cents"]),
                                "target_pct": float(j["target_pct"]),
                                "monthly_target_cents": int(j["monthly_target_cents"]),
                                "is_locked": int(j["is_locked"]),
                            }
                            for j in raw_jars_before
                            if int(j["id"]) in touched_jar_ids
                        ]
                        undo_snap = {
                            "version": 2,
                            "plan_fingerprint": plan_fingerprint,
                            "settings_before": touched_settings_before,
                            "settings_after": touched_settings_after,
                            "jars_before": touched_jars_before,
                            "created_transfer_ids": created_transfer_ids,
                            "goals_changes": list(goals_changes.values()),
                        }
                        saved_batch = self.storage.create_onetap_batch(
                            {
                                "horizon_days": horizon,
                                "provider": narrative.provider_used or narrative.source,
                                "model": narrative.model_used or "",
                                "plan": plan.model_dump(),
                                "undo_snapshot": undo_snap,
                                "status": "applied",
                            }
                        )
                        batch_id = int(saved_batch["id"])
                        applied = True
                        undo_available = True

                if applied and batch_id is not None:
                    self.storage.log_budget_event(
                        "onetap_batch_applied",
                        f"Applied One-Tap optimization batch #{batch_id}",
                        {"batch_id": batch_id},
                    )
                    self.run_self_updating_sync(reason=f"Applied One-Tap optimization batch #{batch_id}")

        return {
            "batch_id": batch_id,
            "plan_fingerprint": plan_fingerprint,
            "applied": applied,
            "undo_available": undo_available,
            "needs_setup": plan.needs_setup,
            "horizon_days": plan.horizon_days,
            "executive_summary": narrative.executive_summary,
            "action_rationales": [r.model_dump() for r in narrative.action_rationales],
            "provider_used": narrative.provider_used,
            "model_used": narrative.model_used,
            "source": narrative.source,
            "grounding_verified": narrative.grounding_verified,
            "runway": plan.runway.model_dump(),
            "diagnostics": plan.diagnostics.model_dump(),
            "actions": [a.model_dump() for a in plan.actions],
            "total_expected_annual_savings_cents": plan.total_expected_annual_savings_cents,
            "recommended_emergency_buffer_cents": plan.recommended_emergency_buffer_cents,
            "statistical_safe_to_invest_cents": plan.statistical_safe_to_invest_cents,
            "available_unlocked_surplus_cents": plan.available_unlocked_surplus_cents,
            "allocated_surplus_cents": plan.allocated_surplus_cents,
        }

    def undo_onetap_batch(self, batch_id: int) -> Dict[str, Any]:
        """
        Atomically restore ONLY rows touched by this One-Tap batch, verifying post-apply
        fingerprints so later manual edits are never silently overwritten.
        """
        batch = self.storage.get_onetap_batch(batch_id)
        if not batch:
            raise ValueError(f"One-Tap batch #{batch_id} not found")
        if batch["status"] != "applied":
            raise ValueError(f"One-Tap batch #{batch_id} has status '{batch['status']}' and cannot be undone.")

        snap = batch.get("undo_snapshot") or {}
        snap_version = int(snap.get("version") or 1)
        now = utc_now_iso()
        restored_count = 0

        with self.storage.transaction(immediate=True) as conn:
            # 1. Conflict verification before mutating anything (v2 snapshots)
            if snap_version >= 2:
                cur_settings = self.storage.get_settings()
                settings_after = snap.get("settings_after") or {}
                for skey, expected_val in settings_after.items():
                    cur_val = cur_settings.get(skey)
                    if expected_val is None:
                        if cur_val is not None:
                            raise PlanConflictError(
                                f"Undo conflict on One-Tap batch #{batch_id}: setting '{skey}' was modified after apply."
                            )
                    elif cur_val is None or int(cur_val) != int(expected_val):
                        raise PlanConflictError(
                            f"Undo conflict on One-Tap batch #{batch_id}: setting '{skey}' was modified after apply."
                        )

                for g_chg in snap.get("goals_changes") or []:
                    gid = int(g_chg["goal_id"])
                    cur_g = self.storage.get_goal(gid)
                    if not cur_g:
                        raise PlanConflictError(
                            f"Undo conflict on One-Tap batch #{batch_id}: goal #{gid} no longer exists."
                        )
                    if int(cur_g["saved_cents"]) != int(g_chg["after_saved_cents"]):
                        raise PlanConflictError(
                            f"Undo conflict on One-Tap batch #{batch_id}: goal #{gid} was modified after apply."
                        )

            # 2. Reverse created jar rebalance transfers (and only touched jars)
            for tid in snap.get("created_transfer_ids") or []:
                conn.execute("DELETE FROM budget_transfers WHERE id = ?", (int(tid),))
                restored_count += 1

            for jar_row in snap.get("jars_before") or []:
                conn.execute(
                    """
                    UPDATE jars
                    SET auto_allocated_cents = ?, rollover_cents = ?, target_pct = ?,
                        monthly_target_cents = ?, is_locked = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        int(jar_row["auto_allocated_cents"]),
                        int(jar_row["rollover_cents"]),
                        float(jar_row["target_pct"]),
                        int(jar_row["monthly_target_cents"]),
                        int(jar_row["is_locked"]),
                        now,
                        int(jar_row["id"]),
                    ),
                )
                restored_count += 1

            # 3. Restore ONLY touched settings columns
            settings_before = snap.get("settings_before") or {}
            if settings_before:
                set_clauses: List[str] = []
                set_params: List[Any] = []
                if "emergency_buffer_cents" in settings_before:
                    set_clauses.append("emergency_buffer_cents = ?")
                    set_params.append(int(settings_before["emergency_buffer_cents"]))
                if "safe_invest_cap_cents" in settings_before:
                    set_clauses.append("safe_invest_cap_cents = ?")
                    set_params.append(settings_before["safe_invest_cap_cents"])
                if set_clauses:
                    set_clauses.append("updated_at = ?")
                    set_params.append(now)
                    conn.execute(
                        f"UPDATE profile_settings SET {', '.join(set_clauses)} WHERE id = 1",
                        tuple(set_params),
                    )
                    restored_count += 1

            # 4. Reverse goal funding cleanly via delta subtraction (`saved_cents -= funded_amount_cents`)
            if snap_version >= 2:
                for g_chg in snap.get("goals_changes") or []:
                    gid = int(g_chg["goal_id"])
                    cur_g = self.storage.get_goal(gid)
                    if not cur_g:
                        continue
                    funded_amt = max(0, int(g_chg.get("funded_amount_cents", 0)))
                    new_saved = max(0, int(cur_g["saved_cents"]) - funded_amt)
                    before_status = str(g_chg.get("before_status") or "active")
                    restored_status = (
                        before_status
                        if new_saved < int(cur_g["target_cents"])
                        else str(cur_g["status"])
                    )
                    conn.execute(
                        "UPDATE goals SET saved_cents = ?, status = ?, updated_at = ? WHERE id = ?",
                        (new_saved, restored_status, now, gid),
                    )
                    restored_count += 1
            else:
                for g_row in snap.get("goals_before") or []:
                    conn.execute(
                        "UPDATE goals SET saved_cents = ?, status = ?, updated_at = ? WHERE id = ?",
                        (
                            int(g_row["saved_cents"]),
                            str(g_row["status"]),
                            now,
                            int(g_row["goal_id"]),
                        ),
                    )
                    restored_count += 1

            conn.execute(
                "UPDATE onetap_batches SET status = 'undone', updated_at = ? WHERE id = ?",
                (now, batch_id),
            )

        self.run_self_updating_sync(reason=f"Undid One-Tap optimization batch #{batch_id}")
        return {
            "undone": True,
            "batch_id": batch_id,
            "restored_count": restored_count,
        }

    def undo_last_action(
        self,
        batch_id: Optional[int] = None,
        *,
        kind: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Undo a specific `agent:N` / `onetap:N` batch or the most recent `--say` or `--one-tap` batch."""
        norm_kind = (kind or "").strip().lower() or None
        if norm_kind not in (None, "agent", "onetap"):
            raise ValueError(f"Unsupported undo kind '{kind}'. Use 'agent' or 'onetap'.")

        if batch_id is not None:
            if norm_kind == "agent":
                res = self.undo_agent_batch(batch_id)
                return {"kind": "agent", **res.model_dump()}
            if norm_kind == "onetap":
                res_ot = self.undo_onetap_batch(batch_id)
                return {"kind": "onetap", **res_ot}

            agent_b = self.storage.get_agent_batch(batch_id)
            ot_b = self.storage.get_onetap_batch(batch_id)
            agent_ready = bool(agent_b and agent_b.get("status") == "executed")
            ot_ready = bool(ot_b and ot_b.get("status") == "applied")
            if agent_ready and ot_ready:
                raise ValueError(
                    f"Batch #{batch_id} is ambiguous (matches both agent:{batch_id} and onetap:{batch_id}). "
                    f"Specify 'agent:{batch_id}' or 'onetap:{batch_id}'."
                )
            if agent_ready:
                res = self.undo_agent_batch(batch_id)
                return {"kind": "agent", **res.model_dump()}
            if ot_ready:
                res_ot = self.undo_onetap_batch(batch_id)
                return {"kind": "onetap", **res_ot}
            if agent_b:
                raise ValueError(f"Agent batch #{batch_id} has status '{agent_b['status']}' and cannot be undone.")
            if ot_b:
                raise ValueError(f"One-Tap batch #{batch_id} has status '{ot_b['status']}' and cannot be undone.")
            raise ValueError(f"Batch #{batch_id} not found.")

        if norm_kind == "agent":
            res = self.undo_last_agent_batch()
            return {"kind": "agent", **res.model_dump()}
        if norm_kind == "onetap":
            ot_batches_only = self.storage.list_onetap_batches(limit=25)
            latest_ot_only = next((b for b in ot_batches_only if b.get("status") == "applied"), None)
            if latest_ot_only is None:
                raise ValueError("No applied One-Tap plan is available to undo.")
            res_ot = self.undo_onetap_batch(int(latest_ot_only["id"]))
            return {"kind": "onetap", **res_ot}

        agent_batches = self.storage.list_agent_batches(limit=25)
        exec_agent_by_id = {int(b["id"]): b for b in agent_batches if b.get("status") == "executed"}
        latest_agent = next((b for b in agent_batches if b.get("status") == "executed"), None)

        ot_batches = self.storage.list_onetap_batches(limit=25)
        applied_ot_by_id = {int(b["id"]): b for b in ot_batches if b.get("status") == "applied"}
        latest_ot = next((b for b in ot_batches if b.get("status") == "applied"), None)

        if latest_agent is None and latest_ot is None:
            raise ValueError("No recent action or One-Tap plan is available to undo.")

        # Prefer strict monotonic event order (handles sub-second consecutive CLI calls cleanly)
        for ev in self.storage.list_budget_events(limit=50):
            ev_type = str(ev.get("event_type") or "")
            det = ev.get("details") if isinstance(ev.get("details"), dict) else {}
            bid = det.get("batch_id")
            if ev_type == "onetap_batch_applied" and bid is not None and int(bid) in applied_ot_by_id:
                res_ot = self.undo_onetap_batch(int(bid))
                return {"kind": "onetap", **res_ot}
            if ev_type == "agent_batch_executed" and bid is not None and int(bid) in exec_agent_by_id:
                res_ag = self.undo_agent_batch(int(bid))
                return {"kind": "agent", **res_ag.model_dump()}

        if latest_ot is not None and (
            latest_agent is None
            or str(latest_ot.get("updated_at") or "") >= str(latest_agent.get("updated_at") or "")
        ):
            res_ot = self.undo_onetap_batch(int(latest_ot["id"]))
            return {"kind": "onetap", **res_ot}
        assert latest_agent is not None
        res_ag = self.undo_agent_batch(int(latest_agent["id"]))
        return {"kind": "agent", **res_ag.model_dump()}

    def get_ai_suggestions(
        self,
        force_refresh: bool = False,
        *,
        mode_override: Optional[str] = None,
        provider_override: Optional[str] = None,
    ) -> SuggestionBatchResponse:
        settings = self.storage.get_settings()
        budget = self._compute_budget()
        impulses = self.list_impulses(include_resolved=False)
        prog = self.storage.get_adhd_progress()
        adhd_stats = build_adhd_stats(prog)
        triage_count = len(self.storage.list_transactions(limit=50, needs_review_only=True))
        reset_done = is_reset_done_this_week(prog.get("last_reset_date"))

        max_daily = int(settings.get("opencode_max_daily_requests", 60))
        used = self.storage.count_ai_requests_today()
        remaining = max(0, max_daily - used)

        credit_report = self.get_credit_report()
        goals = self.list_goals(include_completed=False)
        quant_forecast = self.get_quant_forecast(
            days=30,
            budget=budget,
            credit_report=credit_report,
            goals=goals,
        )
        mode = self._resolve_intelligence_mode(settings, override=mode_override)

        if mode == IntelligenceMode.DETERMINISTIC_ONLY:
            coach = FinancialCoach(client=None, fallback_client=None)
        else:
            clients = self._build_coach_clients_list(provider_override=provider_override)
            coach = FinancialCoach(clients=clients, on_completion=self._record_completion_usage)

        return coach.generate_suggestions(
            budget=budget,
            impulses=impulses,
            adhd_stats=adhd_stats,
            needs_triage_count=triage_count,
            reset_done_this_week=reset_done,
            credit_report=credit_report,
            goals=goals,
            quant_forecast=quant_forecast,
            intelligence_mode=mode,
            quota_remaining=remaining,
            force_refresh=force_refresh,
        )

    def apply_suggestion_action(self, action_type: str, action_payload: Dict[str, Any]) -> Dict[str, Any]:
        """Deterministically execute a verified 1-click AI suggestion action."""
        atype = SuggestionActionType(action_type)
        if atype == SuggestionActionType.REBALANCE_JARS:
            tr = self.rebalance_jars(
                RebalanceTransferRequest(
                    from_jar_id=int(action_payload["from_jar_id"]),
                    to_jar_id=int(action_payload["to_jar_id"]),
                    amount_cents=int(action_payload["amount_cents"]),
                    reason=str(action_payload.get("reason", "Applied AI-verified rebalance")),
                )
            )
            return {"applied": True, "message": "Jar rebalance applied cleanly!", "result": tr}

        if atype == SuggestionActionType.REVIEW_SUBSCRIPTION:
            bill_id = int(action_payload["bill_id"])
            bill = self.flag_zombie_bill(bill_id, flagged=True)
            return {
                "applied": True,
                "message": f"Flagged {bill['name'] if bill else 'subscription'} for your Weekly Reset review.",
                "result": bill,
            }

        if atype == SuggestionActionType.ADJUST_JAR_TARGET:
            jar_id = int(action_payload["jar_id"])
            pct = float(action_payload["target_pct"])
            jar = self.update_jar(jar_id, JarUpdate(target_pct=pct))
            return {
                "applied": True,
                "message": f"Updated {jar['name'] if jar else 'jar'} target to {pct:.1f}%.",
                "result": jar,
            }

        if atype == SuggestionActionType.BOOST_EMERGENCY_BUFFER:
            new_buf = int(action_payload["emergency_buffer_cents"])
            self.update_settings(ProfileSettingsUpdate(emergency_buffer_cents=new_buf))
            return {
                "applied": True,
                "message": f"Emergency buffer shield updated to ${new_buf / 100:,.2f}.",
            }

        if atype == SuggestionActionType.SET_TRADING_CAP:
            cap = int(action_payload.get("safe_to_invest_cents", 0))
            self.storage.log_budget_event(
                "trading_guardrail",
                f"Confirmed ChronoX trading capital cap at ${cap / 100:,.2f}",
                {"safe_to_invest_cents": cap},
            )
            return {
                "applied": True,
                "message": f"ChronoX Invest guardrail confirmed at ${cap / 100:,.2f}.",
            }

        if atype == SuggestionActionType.PARK_IMPULSE and action_payload.get("item_name") and action_payload.get("price_cents"):
            imp = self.create_impulse(
                ImpulseCreate(
                    item_name=str(action_payload["item_name"]),
                    price_cents=int(action_payload["price_cents"]),
                    reason=str(action_payload.get("reason", "Coach recommendation")),
                )
            )
            return {"applied": True, "message": f"Parked '{imp.item_name}' in Impulse Vault!", "result": imp.model_dump()}

        if atype == SuggestionActionType.ENABLE_AUTOPAY:
            line_id = int(action_payload["credit_line_id"])
            mode = str(action_payload.get("autopay_mode", "minimum"))
            updated = self.storage.update_credit_line(line_id, {"autopay_mode": mode})
            if not updated:
                raise ValueError("Credit line not found")
            self.storage.log_budget_event("credit_autopay", f"Marked autopay={mode} on {updated['name']}")
            return {"applied": True, "message": f"Recorded autopay ({mode}) on {updated['name']}!", "result": updated}

        if atype == SuggestionActionType.CREDIT_PAYDOWN:
            line_id = int(action_payload["credit_line_id"])
            amt = int(action_payload["amount_cents"])
            existing = self.storage.get_credit_line(line_id)
            if not existing:
                raise ValueError("Credit line not found")
            new_bal = max(0, int(existing["balance_cents"]) - amt)
            updated = self.storage.update_credit_line(line_id, {"balance_cents": new_bal})
            self.run_self_updating_sync(reason=f"Applied ${amt / 100:,.2f} paydown to {existing['name']}")
            return {
                "applied": True,
                "message": f"Recorded ${amt / 100:,.2f} paydown on {existing['name']} (new balance ${new_bal / 100:,.2f})!",
                "result": updated,
            }

        if atype == SuggestionActionType.FUND_GOAL:
            goal_id = int(action_payload["goal_id"])
            amt = int(action_payload["amount_cents"])
            goal = self.contribute_goal(goal_id, GoalContributeRequest(amount_cents=amt))
            return {"applied": True, "message": f"Added ${amt / 100:,.2f} to '{goal.name}'!", "result": goal.model_dump()}

        return {"applied": True, "message": "Suggestion acknowledged."}

    def ask_coach(
        self,
        question: str,
        *,
        mode_override: Optional[str] = None,
        provider_override: Optional[str] = None,
    ) -> CoachAnswerResponse:
        settings = self.storage.get_settings()
        budget = self._compute_budget()
        impulses = self.list_impulses(include_resolved=False)
        prog = self.storage.get_adhd_progress()
        adhd_stats = build_adhd_stats(prog)
        triage_count = len(self.storage.list_transactions(limit=50, needs_review_only=True))
        reset_done = is_reset_done_this_week(prog.get("last_reset_date"))

        max_daily = int(settings.get("opencode_max_daily_requests", 60))
        used = self.storage.count_ai_requests_today()
        remaining = max(0, max_daily - used)

        credit_report = self.get_credit_report()
        goals = self.list_goals(include_completed=False)
        quant_forecast = self.get_quant_forecast(
            days=30,
            budget=budget,
            credit_report=credit_report,
            goals=goals,
        )
        mode = self._resolve_intelligence_mode(settings, override=mode_override)

        if mode == IntelligenceMode.DETERMINISTIC_ONLY:
            coach = FinancialCoach(client=None, fallback_client=None)
        else:
            clients = self._build_coach_clients_list(provider_override=provider_override)
            coach = FinancialCoach(clients=clients, on_completion=self._record_completion_usage)

        return coach.answer_question(
            question=question,
            budget=budget,
            impulses=impulses,
            adhd_stats=adhd_stats,
            hourly_wage_cents=int(settings.get("hourly_wage_cents", 3000)),
            needs_triage_count=triage_count,
            reset_done_this_week=reset_done,
            credit_report=credit_report,
            goals=goals,
            quant_forecast=quant_forecast,
            intelligence_mode=mode,
            quota_remaining=remaining,
        )

    def _resolve_target_provider(self, provider: str = "auto") -> str:
        cleaned = (provider or "auto").strip().lower()
        all_supported = (
            "opencode_go",
            "gemini",
            "openai_codex",
            "chat_wrapper",
            "codex_cli",
            "opencode_cli",
            "claude_cli",
            "agy_cli",
        )
        if cleaned in all_supported:
            return cleaned
        active = self.resolve_active_provider()
        if active != "none":
            return active
        pref = self._resolve_ai_provider_setting()
        return pref.value if pref != AIProvider.AUTO else "opencode_go"

    def _get_client_by_provider(
        self,
        provider: str,
        *,
        force_enable_local_clis: bool = False,
    ) -> LLMClient:
        target = self._resolve_target_provider(provider)
        clients = self._build_all_ai_clients(force_enable_local_clis=force_enable_local_clis)
        return clients.get(target, clients["opencode_go"])

    def list_ai_models(self, provider: str = "auto", force_refresh: bool = False) -> Dict[str, Any]:
        target = self._resolve_target_provider(provider)
        client = self._get_client_by_provider(target, force_enable_local_clis=(target in LOCAL_CLI_SPECS))
        res = dict(client.list_models(force_refresh=force_refresh))
        res.setdefault("provider", target)
        return res

    def test_ai_connection(self, provider: str = "auto") -> Dict[str, Any]:
        target = self._resolve_target_provider(provider)
        is_local_cli = target in LOCAL_CLI_SPECS
        client = self._get_client_by_provider(target, force_enable_local_clis=is_local_cli)
        t0 = time.perf_counter()
        if is_local_cli:
            probe_fn: Any = client.test_connection
            try:
                raw_res = dict(probe_fn(deep=True, force_refresh=True))
            except TypeError:
                raw_res = dict(probe_fn())
        else:
            raw_res = dict(client.test_connection())
        elapsed_ms = max(0, int(round((time.perf_counter() - t0) * 1000.0)))
        ok = bool(raw_res.get("ok"))
        detail = str(
            raw_res.get("detail")
            or raw_res.get("message")
            or ("Connected and ready" if ok else "Connection check failed")
        )
        if is_local_cli:
            if ok:
                self._local_cli_registry.clear_auth_failure(target, mark_ready=True)
            elif getattr(client, "is_configured", False):
                self._local_cli_registry.mark_auth_failure(target, detail)

        model_val = str(raw_res.get("model") or getattr(client, "model", "") or "")
        latency_val = int(raw_res.get("latency_ms") if raw_res.get("latency_ms") is not None else elapsed_ms)
        out = dict(raw_res)
        out.update(
            {
                "ok": ok,
                "provider": target,
                "model": model_val,
                "latency_ms": latency_val,
                "detail": detail,
                "message": detail,
            }
        )
        return out

    # --- Settings ---

    def get_ai_settings(self) -> AISettingsOut:
        raw = self.storage.get_settings()
        oc_key, oc_source = self._resolve_opencode_key_with_source()
        gem_key, gem_source = self._resolve_gemini_key_with_source()
        codex_key, codex_source = self._resolve_codex_key_with_source()
        wrap_key, _wrap_source = self._resolve_wrapper_key_with_source()
        max_daily = int(raw.get("opencode_max_daily_requests", 60))
        used_today = self.storage.count_ai_requests_today()
        return AISettingsOut(
            intelligence_mode=self._resolve_intelligence_mode(raw),
            ai_provider=self._resolve_ai_provider_setting(raw),
            active_provider_resolved=self.resolve_active_provider(),
            opencode_base_url=raw.get("opencode_base_url", DEFAULT_OPENCODE_GO_BASE_URL),
            opencode_model=raw.get("opencode_model", "glm-5.3-flash"),
            opencode_protocol=raw.get("opencode_protocol", "auto"),
            opencode_max_daily_requests=max_daily,
            opencode_key_configured=bool(oc_key),
            opencode_key_masked=mask_secret(oc_key),
            opencode_key_source=oc_source,
            gemini_api_key_set=bool(gem_key),
            gemini_key_masked=mask_secret(gem_key),
            gemini_key_source=gem_source,
            gemini_model=raw.get("gemini_model", DEFAULT_GEMINI_MODEL),
            gemini_base_url=raw.get("gemini_base_url", DEFAULT_GEMINI_BASE_URL),
            codex_base_url=raw.get("codex_base_url", DEFAULT_OPENAI_BASE_URL),
            codex_model=raw.get("codex_model", DEFAULT_CODEX_MODEL),
            codex_key_configured=bool(codex_key),
            codex_key_masked=mask_secret(codex_key),
            codex_key_source=codex_source,
            wrapper_base_url=raw.get("wrapper_base_url", ""),
            wrapper_model=raw.get("wrapper_model", ""),
            wrapper_wire_api=raw.get("wrapper_wire_api", "chat_completions"),
            wrapper_key_configured=bool(wrap_key),
            wrapper_key_masked=mask_secret(wrap_key),
            codex_cli_enabled=bool(raw.get("codex_cli_enabled", 0)),
            codex_cli_model=raw.get("codex_cli_model", ""),
            local_cli_auto_detect=self._is_local_cli_auto_detect_enabled(raw),
            requests_used_today=used_today,
            quota_remaining_today=max(0, max_daily - used_today),
        )

    def update_ai_settings(self, req: AISettingsUpdate) -> AISettingsOut:
        self.update_settings(ProfileSettingsUpdate(**req.model_dump()))
        return self.get_ai_settings()

    def get_settings(self) -> ProfileSettings:
        raw = self.storage.get_settings()
        oc_key, oc_source = self._resolve_opencode_key_with_source()
        gem_key, gem_source = self._resolve_gemini_key_with_source()
        codex_key, codex_source = self._resolve_codex_key_with_source()
        wrap_key, _wrap_source = self._resolve_wrapper_key_with_source()
        return ProfileSettings(
            currency=raw.get("currency", "USD"),
            hourly_wage_cents=int(raw.get("hourly_wage_cents", 3000)),
            emergency_buffer_cents=int(raw.get("emergency_buffer_cents", 50_000)),
            default_cooling_hours=int(raw.get("default_cooling_hours", 48)),
            payday_cadence_days=int(raw.get("payday_cadence_days", 14)),
            next_payday_date=raw.get("next_payday_date"),
            expected_paycheck_cents=int(raw.get("expected_paycheck_cents", 0)),
            calm_mode=bool(raw.get("calm_mode", 1)),
            auto_rebalance_enabled=bool(raw.get("auto_rebalance_enabled", 1)),
            onboarding_completed=bool(raw.get("onboarding_completed", 0)),
            vault_auto_lock_minutes=int(raw.get("vault_auto_lock_minutes", 30)),
            email_scanner_enabled=bool(raw.get("email_scanner_enabled", 0)),
            intelligence_mode=self._resolve_intelligence_mode(raw),
            ai_provider=self._resolve_ai_provider_setting(raw),
            active_provider_resolved=self.resolve_active_provider(),
            opencode_base_url=raw.get("opencode_base_url", DEFAULT_OPENCODE_GO_BASE_URL),
            opencode_model=raw.get("opencode_model", "glm-5.3-flash"),
            opencode_protocol=raw.get("opencode_protocol", "auto"),
            opencode_max_daily_requests=int(raw.get("opencode_max_daily_requests", 60)),
            opencode_key_configured=bool(oc_key),
            opencode_key_masked=mask_secret(oc_key),
            opencode_key_source=oc_source,
            gemini_api_key_set=bool(gem_key),
            gemini_key_masked=mask_secret(gem_key),
            gemini_key_source=gem_source,
            gemini_model=raw.get("gemini_model", DEFAULT_GEMINI_MODEL),
            gemini_base_url=raw.get("gemini_base_url", DEFAULT_GEMINI_BASE_URL),
            codex_base_url=raw.get("codex_base_url", DEFAULT_OPENAI_BASE_URL),
            codex_model=raw.get("codex_model", DEFAULT_CODEX_MODEL),
            codex_key_configured=bool(codex_key),
            codex_key_masked=mask_secret(codex_key),
            codex_key_source=codex_source,
            wrapper_base_url=raw.get("wrapper_base_url", ""),
            wrapper_model=raw.get("wrapper_model", ""),
            wrapper_wire_api=raw.get("wrapper_wire_api", "chat_completions"),
            wrapper_key_configured=bool(wrap_key),
            wrapper_key_masked=mask_secret(wrap_key),
            codex_cli_enabled=bool(raw.get("codex_cli_enabled", 0)),
            codex_cli_model=raw.get("codex_cli_model", ""),
            local_cli_auto_detect=self._is_local_cli_auto_detect_enabled(raw),
            auth_enabled=True,
        )

    def update_settings(self, req: ProfileSettingsUpdate) -> ProfileSettings:
        data = req.model_dump(exclude_none=True)

        secret_ops = [
            ("opencode_api_key", "clear_opencode_key", "opencode_go_api_key"),
            ("gemini_api_key", "clear_gemini_key", "gemini_api_key"),
            ("codex_api_key", "clear_codex_key", "codex_api_key"),
            ("wrapper_api_key", "clear_wrapper_key", "wrapper_api_key"),
        ]
        for key_field, clear_field, store_key in secret_ops:
            should_clear = bool(data.pop(clear_field, False))
            raw_val = data.pop(key_field, None)
            if should_clear:
                self._require_vault_unlocked()
                self.secrets.delete(store_key)
            elif raw_val is not None and str(raw_val).strip():
                self._require_vault_unlocked()
                self.secrets.set(store_key, str(raw_val).strip())

        if "intelligence_mode" in data and isinstance(data["intelligence_mode"], IntelligenceMode):
            data["intelligence_mode"] = data["intelligence_mode"].value
        if "ai_provider" in data and isinstance(data["ai_provider"], AIProvider):
            data["ai_provider"] = data["ai_provider"].value

        for url_field in ("opencode_base_url", "gemini_base_url", "codex_base_url"):
            if url_field in data and data[url_field]:
                data[url_field] = validate_external_api_url(str(data[url_field]))
        if "wrapper_base_url" in data:
            val = str(data["wrapper_base_url"]).strip()
            data["wrapper_base_url"] = validate_external_api_url(val) if val else ""

        for bool_k in (
            "calm_mode",
            "auto_rebalance_enabled",
            "onboarding_completed",
            "codex_cli_enabled",
            "local_cli_auto_detect",
            "email_scanner_enabled",
        ):
            if bool_k in data:
                data[bool_k] = int(bool(data[bool_k]))

        payday_keys = {"payday_cadence_days", "next_payday_date", "expected_paycheck_cents"}
        if payday_keys & set(data.keys()):
            curr_settings = self.storage.get_settings()
            non_payday_keys = set(data.keys()) - payday_keys
            payday_changed = (
                ("payday_cadence_days" in data and int(data["payday_cadence_days"]) != int(curr_settings.get("payday_cadence_days", 14)))
                or ("next_payday_date" in data and str(data["next_payday_date"] or "") != str(curr_settings.get("next_payday_date") or ""))
                or ("expected_paycheck_cents" in data and int(data["expected_paycheck_cents"]) != int(curr_settings.get("expected_paycheck_cents", 0)))
                or ("expected_paycheck_cents" in data and int(data["expected_paycheck_cents"]) > 0)
            )
            if payday_changed or not non_payday_keys:
                data["payday_user_configured"] = 1
        if "expected_paycheck_cents" in data:
            data["income_configured"] = int(int(data["expected_paycheck_cents"]) > 0)

        budget_fields = {
            "currency",
            "hourly_wage_cents",
            "emergency_buffer_cents",
            "default_cooling_hours",
            "payday_cadence_days",
            "next_payday_date",
            "expected_paycheck_cents",
            "calm_mode",
            "auto_rebalance_enabled",
        }
        if data:
            self.storage.update_settings(data)
            if any(k in budget_fields for k in data):
                self.run_self_updating_sync(reason="Profile settings updated")
        return self.get_settings()
