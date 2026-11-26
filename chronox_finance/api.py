"""
FastAPI REST API & Web Application Server for ChronoX Finance.

Security features (Proposal A + Gate 1 Amendments #53):
- Mandatory fail-closed envelope-encrypted Vault Lock (`needs_setup` -> `locked` -> `unlocked`)
- Ephemeral per-boot HMAC-SHA256 HttpOnly SameSite=Strict session cookie + per-boot `X-ChronoX-CSRF` header
- Strict loopback `Host` & `Origin` validation (DNS-rebinding defense)
- Strict Content-Security-Policy (`script-src 'self'`, zero CDNs, zero inline scripts)
- Clean canonical `/api/v1/*` OpenAPI contract (`/api/openapi.json`) with `/api/*` aliases (`include_in_schema=False`)
- Sliding-window rate limiting on auth & AI endpoints + 25 MB upload safety guard
"""

from __future__ import annotations

import html
import json
import logging
from pathlib import Path
from typing import Annotated, Any, Dict, Optional

from fastapi import (
    APIRouter,
    Body,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from chronox_finance.connectors.base import (
    ConnectorAuthError,
    ConnectorError,
    HostNotAllowedError,
    ReadOnlyViolationError,
)
from chronox_finance.engines.rules import SplitValidationError, UnsafeRegexError
from chronox_finance.models import (
    AccountCreate,
    AgentPlanRequest,
    AIProvider,
    AISettingsUpdate,
    BalanceTransferRequest,
    BrokerCoinbaseConnectRequest,
    BrokerRobinhoodConnectRequest,
    BrokerSchwabCompleteRequest,
    BrokerSchwabConnectRequest,
    CoachQuestionRequest,
    CreditAPIConnectRequest,
    CreditAPISyncRequest,
    CreditDerogatoryCreate,
    CreditInquiryCreate,
    CreditLineCreate,
    CreditLineUpdate,
    CreditReportCreate,
    EmailConfigUpdate,
    GoalContributeRequest,
    GoalCreate,
    GoalPlanRequest,
    GoalReorderRequest,
    GoalUpdate,
    HoldingCreate,
    ImpulseCreate,
    ImpulseDecision,
    JarUpdate,
    OnboardingCompleteRequest,
    OneTapOptimizeRequest,
    PlaidConfigRequest,
    PlaidExchangeRequest,
    PlaidLinkTokenRequest,
    PlaidPollLinkRequest,
    ProfileSettingsUpdate,
    QuickAddRequest,
    RebalanceTransferRequest,
    RecurringBillCreate,
    RuleCreate,
    RuleGroupCreate,
    RuleUpdate,
    SayRequest,
    ScenarioCreate,
    ScenarioSimRequest,
    SimpleFINClaimRequest,
    TransactionCommentCreate,
    TransactionCreate,
    TransactionSplitsRequest,
    TransactionTagsRequest,
    TransactionUpdate,
    VaultChangePasswordRequest,
    VaultRecoverRequest,
    VaultSetupRequest,
    VaultUnlockRequest,
)
from chronox_finance.security import (
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    SESSION_TTL_SECONDS,
    InvalidVaultCredentialsError,
    SlidingWindowRateLimiter,
    VaultAlreadyInitializedError,
    VaultCorruptedError,
    VaultLockedError,
    VaultNotInitializedError,
    validate_loopback_host,
    validate_loopback_origin,
)
from chronox_finance.service import FinanceService, PlanConflictError

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB
_VALID_PROVIDERS = {p.value for p in AIProvider}
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_CSRF_EXEMPT_PATHS = {
    "/api/v1/auth/setup",
    "/api/v1/auth/login",
    "/api/v1/auth/recover",
    "/api/auth/setup",
    "/api/auth/login",
    "/api/auth/recover",
}


class _VaultAuthHTTPException(HTTPException):
    """401 HTTPException carrying explicit `vault_state` (`needs_setup` | `locked`)."""

    def __init__(self, detail: str, *, vault_state: str):
        super().__init__(status_code=401, detail=detail)
        self.vault_state = vault_state


class AccountBalancePatch(BaseModel):
    balance_cents: int = Field(..., ge=-100_000_000_00, le=100_000_000_00)


class SuggestionApplyRequest(BaseModel):
    action_type: str
    action_payload: Dict[str, Any] = Field(default_factory=dict)


class CsvTextImportRequest(BaseModel):
    csv_text: str = Field(..., min_length=1, max_length=MAX_UPLOAD_BYTES)
    account_id: Optional[int] = None
    flip_expense_sign: bool = False


class WeeklyResetCompleteRequest(BaseModel):
    apply_rebalances: bool = True


class ZombieFlagRequest(BaseModel):
    flagged: bool = True


class SyncTriggerRequest(BaseModel):
    connection_id: Optional[int] = None
    days: int = Field(730, ge=1, le=730)


class PlaidHostedCheckRequest(BaseModel):
    link_token: Optional[str] = Field(None, max_length=512)


def create_app(
    db_path: str = ".chronox/finance.sqlite3",
    service: Optional[FinanceService] = None,
    *,
    enforce_vault_lock: bool = True,
    allowed_port: Optional[int] = None,
) -> FastAPI:
    """Create and configure the ChronoX Finance FastAPI application."""
    svc = service or FinanceService(db_path=db_path)

    # Test-only DI bypass: if vault lock enforcement is explicitly disabled and vault is not
    # initialized on disk, unlock an ephemeral in-memory DEK so secret-writing tests work cleanly.
    if not enforce_vault_lock and not svc.secrets.is_initialized() and not svc.secrets.is_unlocked():
        svc.secrets.unlock_ephemeral_for_testing()
    if svc.secrets.is_unlocked():
        svc._resume_plaid_background_syncs_if_any()

    auth_limiter = SlidingWindowRateLimiter(max_requests=15, window_seconds=60)
    ai_limiter = SlidingWindowRateLimiter(max_requests=25, window_seconds=60)

    app = FastAPI(
        title="ChronoX Finance",
        version="2.0.0",
        description=(
            "Local-First Industrial Personal Finance Command Center — Envelope-Encrypted Vault, "
            "730-Day Connectors, MoneyStill Analytics, Firefly Automation Rules, Credit Factor Analysis, "
            "Stocks & Crypto Portfolio, and Guarded Multi-Provider Agentic Coach."
        ),
        openapi_url="/api/openapi.json",
        docs_url="/api/docs",
        redoc_url=None,
    )
    app.state.service = svc
    app.state.enforce_vault_lock = enforce_vault_lock

    # --- Global Structured JSON Exception Handlers (never leak stack traces) ---

    @app.exception_handler(_VaultAuthHTTPException)
    async def vault_auth_exception_handler(_request: Request, exc: _VaultAuthHTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "code": "vault_auth_required", "state": exc.vault_state},
        )

    @app.exception_handler(VaultLockedError)
    async def vault_locked_handler(_request: Request, exc: VaultLockedError):
        return JSONResponse(
            status_code=423,
            content={
                "detail": str(exc) or "Sync paused: vault locked",
                "code": "vault_locked",
                "state": "locked",
            },
        )

    @app.exception_handler(VaultNotInitializedError)
    async def vault_uninit_handler(_request: Request, exc: VaultNotInitializedError):
        return JSONResponse(
            status_code=401,
            content={
                "detail": str(exc) or "Vault must be initialized first.",
                "code": "vault_not_initialized",
                "state": "needs_setup",
            },
        )

    @app.exception_handler(VaultAlreadyInitializedError)
    async def vault_already_init_handler(_request: Request, exc: VaultAlreadyInitializedError):
        return JSONResponse(
            status_code=409,
            content={"detail": str(exc), "code": "vault_already_initialized"},
        )

    @app.exception_handler(InvalidVaultCredentialsError)
    async def vault_bad_creds_handler(_request: Request, exc: InvalidVaultCredentialsError):
        return JSONResponse(
            status_code=401,
            content={"detail": str(exc) or "Invalid vault credentials.", "code": "invalid_credentials"},
        )

    @app.exception_handler(VaultCorruptedError)
    async def vault_corrupted_handler(_request: Request, exc: VaultCorruptedError):
        return JSONResponse(
            status_code=500,
            content={"detail": str(exc), "code": "vault_corrupted"},
        )

    @app.exception_handler(ReadOnlyViolationError)
    async def read_only_violation_handler(_request: Request, exc: ReadOnlyViolationError):
        return JSONResponse(
            status_code=403,
            content={"detail": str(exc), "code": "read_only_violation"},
        )

    @app.exception_handler(HostNotAllowedError)
    async def host_not_allowed_handler(_request: Request, exc: HostNotAllowedError):
        return JSONResponse(
            status_code=403,
            content={"detail": str(exc), "code": "host_not_allowed"},
        )

    @app.exception_handler(ConnectorAuthError)
    async def connector_auth_handler(_request: Request, exc: ConnectorAuthError):
        return JSONResponse(
            status_code=502,
            content={
                "detail": str(exc),
                "code": "connector_auth_error",
                "provider": exc.provider,
                "reconnect_required": True,
            },
        )

    @app.exception_handler(ConnectorError)
    async def connector_error_handler(_request: Request, exc: ConnectorError):
        return JSONResponse(
            status_code=400,
            content={"detail": str(exc), "code": "connector_error"},
        )

    @app.exception_handler(SplitValidationError)
    async def split_validation_handler(_request: Request, exc: SplitValidationError):
        return JSONResponse(
            status_code=400,
            content={"detail": str(exc), "code": "split_validation_error"},
        )

    @app.exception_handler(UnsafeRegexError)
    async def unsafe_regex_handler(_request: Request, exc: UnsafeRegexError):
        return JSONResponse(
            status_code=400,
            content={"detail": str(exc), "code": "unsafe_regex"},
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(_request: Request, exc: StarletteHTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "code": f"http_{exc.status_code}"},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_request: Request, exc: RequestValidationError):
        clean_errors = []
        summary_parts = []
        for err in exc.errors()[:8]:
            loc = ".".join(str(x) for x in err.get("loc", []) if x != "body")
            msg = str(err.get("msg", "Invalid value"))
            clean_errors.append(
                {"field": loc or "request", "message": msg, "type": str(err.get("type", "value_error"))}
            )
            summary_parts.append(f"{loc}: {msg}" if loc else msg)
        detail = "; ".join(summary_parts) if summary_parts else "Request validation failed."
        return JSONResponse(
            status_code=422,
            content={"detail": detail, "code": "validation_error", "errors": clean_errors},
        )

    @app.exception_handler(PlanConflictError)
    async def plan_conflict_handler(_request: Request, exc: PlanConflictError):
        return JSONResponse(
            status_code=409,
            content={"detail": str(exc) or "Plan or state conflict.", "code": "conflict"},
        )

    @app.exception_handler(ValueError)
    async def value_error_handler(_request: Request, exc: ValueError):
        return JSONResponse(
            status_code=400,
            content={"detail": str(exc) or "Invalid request.", "code": "bad_request"},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger.exception("Unhandled error on %s %s: %s", request.method, request.url.path, exc)
        return JSONResponse(
            status_code=500,
            content={
                "detail": "An unexpected internal error occurred. Your local data is safe.",
                "code": "internal_error",
            },
            headers={
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )

    # --- Localhost DNS-Rebinding, Per-Boot CSRF & Security Headers Middleware ---

    def _apply_security_headers(response: Response, path: str) -> Response:
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        response.headers["Cache-Control"] = "no-store" if path.startswith("/api/") else "public, max-age=60"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "font-src 'self'; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        return response

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        path = request.url.path
        is_testclient = request.client is None or request.client.host == "testclient"

        # 1. Strict DNS-rebinding defense: reject any Host outside 127.0.0.1 / localhost / [::1]
        req_host = request.headers.get("host") or ""
        if not validate_loopback_host(req_host, allowed_port=allowed_port, allow_testserver=is_testclient):
            return _apply_security_headers(
                JSONResponse(
                    status_code=403,
                    content={
                        "detail": "Blocked non-loopback Host header (DNS-rebinding protection).",
                        "code": "host_not_allowed",
                    },
                ),
                path,
            )

        # 2. Strict Origin validation (reject non-loopback or mismatched-port origins)
        origin = request.headers.get("origin")
        if origin is not None and not validate_loopback_origin(
            origin,
            expected_host=req_host,
            allowed_port=allowed_port,
            allow_testserver=is_testclient,
        ):
            return _apply_security_headers(
                JSONResponse(
                    status_code=403,
                    content={
                        "detail": "Blocked cross-origin request (DNS-rebinding / CSRF protection).",
                        "code": "origin_not_allowed",
                    },
                ),
                path,
            )

        # 3. Per-boot X-ChronoX-CSRF header token on ALL state-changing authenticated API routes
        if (
            enforce_vault_lock
            and path.startswith("/api/")
            and request.method in _MUTATING_METHODS
            and path not in _CSRF_EXEMPT_PATHS
        ):
            provided_csrf = request.headers.get(CSRF_HEADER_NAME)
            if not svc.auth.verify_csrf(provided_csrf):
                return _apply_security_headers(
                    JSONResponse(
                        status_code=403,
                        content={
                            "detail": f"Missing or invalid {CSRF_HEADER_NAME} header token.",
                            "code": "csrf_invalid",
                        },
                    ),
                    path,
                )

        response = await call_next(request)
        return _apply_security_headers(response, path)

    # --- Auth & Rate Limit Dependencies ---

    def _set_auth_cookie_and_csrf(response: Response, request: Request) -> str:
        token = svc.auth.issue_session_token(subject="owner", ttl=SESSION_TTL_SECONDS)
        csrf_tok = svc.auth.get_csrf_token()
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=token,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            max_age=SESSION_TTL_SECONDS,
        )
        response.headers[CSRF_HEADER_NAME] = csrf_tok
        return csrf_tok

    def require_auth(request: Request) -> None:
        if not enforce_vault_lock:
            return
        svc._check_idle_lock()
        vstate = svc.secrets.vault_state()
        if vstate == "needs_setup":
            raise _VaultAuthHTTPException(
                "Vault not initialized — create a master password to begin.",
                vault_state="needs_setup",
            )
        if vstate != "unlocked":
            raise _VaultAuthHTTPException(
                "Vault locked — enter your master password to unlock.",
                vault_state="locked",
            )
        token = request.cookies.get(SESSION_COOKIE_NAME, "")
        if not svc.auth.is_authenticated(token):
            raise _VaultAuthHTTPException(
                "Session locked — please unlock with your master password.",
                vault_state="locked",
            )
        svc.secrets.touch()

    def check_auth_rate_limit(request: Request) -> None:
        client_ip = request.client.host if request.client else "local"
        allowed, retry_after = auth_limiter.allow(client_ip)
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail=f"Too many vault unlock attempts. Try again in {retry_after}s.",
            )

    def check_ai_rate_limit(request: Request) -> None:
        client_ip = request.client.host if request.client else "local"
        allowed, retry_after = ai_limiter.allow(client_ip)
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail=f"Slow down a moment — try again in {retry_after}s.",
            )

    def _decode_upload(raw: bytes) -> str:
        if len(raw) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Uploaded file exceeds 25 MB safety limit.")
        for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("latin-1", errors="replace")

    def _read_statement_payload(raw: bytes, filename: str) -> Any:
        if len(raw) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Statement file exceeds 25 MB safety limit.")
        if filename.lower().endswith(".pdf") or raw[:5] == b"%PDF-":
            return raw
        return _decode_upload(raw)

    def _validate_provider_param(provider: str) -> str:
        cleaned = (provider or "auto").strip().lower()
        if cleaned not in _VALID_PROVIDERS:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Unsupported AI provider. Use 'auto', 'opencode_go', 'gemini', "
                    "'openai_codex', 'chat_wrapper', 'codex_cli', 'opencode_cli', "
                    "'claude_cli', or 'agy_cli'."
                ),
            )
        return cleaned

    # =========================================================================
    # Canonical /api/v1 Router (mounted once in OpenAPI + aliased at /api/*)
    # =========================================================================

    v1 = APIRouter()

    # --- Health, Mandatory Vault Auth & Onboarding ---

    @v1.get("/health", tags=["Health"])
    def health():
        return svc.get_health()

    @v1.get("/auth/status", tags=["Vault & Auth"])
    def auth_status(request: Request, response: Response):
        token = request.cookies.get(SESSION_COOKIE_NAME, "")
        is_auth = (not enforce_vault_lock and svc.secrets.is_unlocked()) or svc.auth.is_authenticated(token)
        status = svc.get_vault_status(authenticated=is_auth)
        if status.csrf_token:
            response.headers[CSRF_HEADER_NAME] = status.csrf_token
        return status

    @v1.post("/auth/setup", tags=["Vault & Auth"], dependencies=[Depends(check_auth_rate_limit)])
    def auth_setup(req: VaultSetupRequest, request: Request, response: Response):
        res = svc.setup_vault(req)
        _set_auth_cookie_and_csrf(response, request)
        return res

    @v1.post("/auth/login", tags=["Vault & Auth"], dependencies=[Depends(check_auth_rate_limit)])
    def auth_login(req: VaultUnlockRequest, request: Request, response: Response) -> Dict[str, Any]:
        if svc.secrets.vault_state() == "needs_setup":
            raise _VaultAuthHTTPException(
                "Vault not initialized — create a master password first.",
                vault_state="needs_setup",
            )
        if not svc.unlock_vault(req.password):
            raise HTTPException(status_code=401, detail="Incorrect master password.")
        csrf_tok = _set_auth_cookie_and_csrf(response, request)
        status = svc.get_vault_status(authenticated=True)
        return {
            "ok": True,
            "authenticated": True,
            "onboarding_completed": status.onboarding_completed,
            "csrf_token": csrf_tok,
            "status": status.model_dump(),
        }

    @v1.post("/auth/recover", tags=["Vault & Auth"], dependencies=[Depends(check_auth_rate_limit)])
    def auth_recover(req: VaultRecoverRequest, request: Request, response: Response):
        res = svc.recover_vault(req)
        _set_auth_cookie_and_csrf(response, request)
        return res

    @v1.post("/auth/logout", tags=["Vault & Auth"])
    def auth_logout(response: Response) -> Dict[str, Any]:
        status = svc.lock_vault()
        response.delete_cookie(SESSION_COOKIE_NAME)
        return {"ok": True, "state": status.state, "status": status.model_dump()}

    @v1.post("/auth/change-password", tags=["Vault & Auth"], dependencies=[Depends(require_auth)])
    def auth_change_password(
        req: VaultChangePasswordRequest,
        request: Request,
        response: Response,
    ) -> Dict[str, Any]:
        status = svc.change_vault_password(req)
        csrf_tok = _set_auth_cookie_and_csrf(response, request)
        return {
            "ok": True,
            "csrf_token": csrf_tok,
            "status": status.model_dump(),
        }

    @v1.post("/onboarding/complete", tags=["Vault & Auth"], dependencies=[Depends(require_auth)])
    def complete_onboarding(req: Annotated[Optional[OnboardingCompleteRequest], Body()] = None):
        return svc.complete_onboarding(req or OnboardingCompleteRequest())

    # --- Dashboard, Quant Forecast & Self-Updating Auto-Budget ---

    @v1.get("/dashboard", tags=["Command"], dependencies=[Depends(require_auth)])
    def get_dashboard():
        return svc.get_dashboard()

    @v1.get("/quant/forecast", tags=["Analytics"], dependencies=[Depends(require_auth)])
    def get_quant_forecast(days: int = 30):
        return svc.get_quant_forecast(days=max(7, min(730, days)))

    @v1.post("/budget/sync", tags=["Command"], dependencies=[Depends(require_auth)])
    def trigger_budget_sync():
        return svc.run_self_updating_sync(reason="Manual refresh")

    @v1.post("/budget/rebalance", tags=["Command"], dependencies=[Depends(require_auth)])
    def rebalance_jars(req: RebalanceTransferRequest):
        return svc.rebalance_jars(req)

    @v1.get("/chronox/safe-trading-capital", tags=["Portfolio"], dependencies=[Depends(require_auth)])
    def safe_trading_capital():
        dash = svc.get_dashboard()
        return {
            "safe_to_invest_cents": dash.safe_to_invest_cents,
            "safe_to_invest_dollars": round(dash.safe_to_invest_cents / 100.0, 2),
            "guardrail_active": True,
            "note": "Only funds remaining AFTER Bills, Credit Cards, and Emergency Buffer are 100% shielded.",
        }

    # --- MoneyStill Analytics, Net Worth & Scenario Projections ---

    @v1.get("/analytics/overview", tags=["Analytics"], dependencies=[Depends(require_auth)])
    def analytics_overview(
        period: str = "1y",
        month: Optional[str] = None,
        group_by: str = "jar",
    ):
        effective_period = (month or period or "1y").strip()
        return svc.get_analytics_overview(period=effective_period, group_by=group_by)

    @v1.get("/analytics/net-worth", tags=["Analytics"], dependencies=[Depends(require_auth)])
    def analytics_net_worth(days: int = 730):
        return svc.get_net_worth_series(days=max(7, min(730, days)))

    @v1.get("/scenarios", tags=["Analytics"], dependencies=[Depends(require_auth)])
    def list_scenarios():
        return svc.list_scenarios()

    @v1.post("/scenarios", tags=["Analytics"], dependencies=[Depends(require_auth)])
    def create_scenario(req: ScenarioCreate):
        return svc.create_scenario(req)

    @v1.delete("/scenarios/{scenario_id}", tags=["Analytics"], dependencies=[Depends(require_auth)])
    def delete_scenario(scenario_id: int):
        if not svc.delete_scenario(scenario_id):
            raise HTTPException(status_code=404, detail="Scenario not found")
        return {"deleted": True}

    @v1.post("/analytics/scenario-sim", tags=["Analytics"], dependencies=[Depends(require_auth)])
    def scenario_sim(req: ScenarioSimRequest):
        return svc.simulate_scenario(req)

    # --- Accounts & Jars ---

    @v1.get("/accounts", tags=["Accounts"], dependencies=[Depends(require_auth)])
    def list_accounts():
        return svc.get_dashboard().accounts

    @v1.post("/accounts", tags=["Accounts"], dependencies=[Depends(require_auth)])
    def create_account(req: AccountCreate):
        return svc.create_account(req)

    @v1.patch("/accounts/{account_id}/balance", tags=["Accounts"], dependencies=[Depends(require_auth)])
    def patch_account_balance(account_id: int, req: AccountBalancePatch):
        updated = svc.update_account_balance(account_id, req.balance_cents)
        if not updated:
            raise HTTPException(status_code=404, detail="Account not found")
        return updated

    @v1.patch("/jars/{jar_id}", tags=["Accounts"], dependencies=[Depends(require_auth)])
    def patch_jar(jar_id: int, req: JarUpdate):
        updated = svc.update_jar(jar_id, req)
        if not updated:
            raise HTTPException(status_code=404, detail="Jar not found")
        return updated

    # --- Transactions (2-Year Backport), Splits, Tags, Comments & Statement Import ---

    @v1.post("/quick-add", tags=["Transactions"], dependencies=[Depends(require_auth)])
    def quick_add(req: QuickAddRequest):
        return svc.quick_add(req)

    @v1.get("/transactions", tags=["Transactions"], dependencies=[Depends(require_auth)])
    def list_transactions(
        limit: int = 100,
        offset: int = 0,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        account_id: Optional[int] = None,
        jar_id: Optional[int] = None,
        needs_review: bool = False,
        search: Optional[str] = None,
    ):
        return svc.list_transactions(
            limit=max(1, min(250_000, limit)),
            offset=max(0, offset),
            start_date=start_date,
            end_date=end_date,
            account_id=account_id,
            jar_id=jar_id,
            needs_review_only=needs_review,
            search=search,
        )

    @v1.post("/transactions", tags=["Transactions"], dependencies=[Depends(require_auth)])
    def create_transaction(req: TransactionCreate):
        return svc.create_transaction(req)

    @v1.patch("/transactions/{tx_id}", tags=["Transactions"], dependencies=[Depends(require_auth)])
    def update_transaction(tx_id: int, req: TransactionUpdate):
        updated = svc.update_transaction(tx_id, req)
        if not updated:
            raise HTTPException(status_code=404, detail="Transaction not found")
        return updated

    @v1.delete("/transactions/{tx_id}", tags=["Transactions"], dependencies=[Depends(require_auth)])
    def delete_transaction(tx_id: int):
        if not svc.delete_transaction(tx_id):
            raise HTTPException(status_code=404, detail="Transaction not found")
        return {"deleted": True}

    @v1.post("/transactions/{tx_id}/splits", tags=["Transactions"], dependencies=[Depends(require_auth)])
    def set_transaction_splits(tx_id: int, req: TransactionSplitsRequest):
        return svc.set_transaction_splits(tx_id, req)

    @v1.delete("/transactions/{tx_id}/splits", tags=["Transactions"], dependencies=[Depends(require_auth)])
    def clear_transaction_splits(tx_id: int):
        svc.clear_transaction_splits(tx_id)
        return {"cleared": True}

    @v1.put("/transactions/{tx_id}/tags", tags=["Transactions"], dependencies=[Depends(require_auth)])
    def set_transaction_tags(tx_id: int, req: TransactionTagsRequest):
        return svc.set_transaction_tags(tx_id, req)

    @v1.get("/transactions/{tx_id}/comments", tags=["Transactions"], dependencies=[Depends(require_auth)])
    def list_transaction_comments(tx_id: int):
        return svc.list_transaction_comments(tx_id)

    @v1.post("/transactions/{tx_id}/comments", tags=["Transactions"], dependencies=[Depends(require_auth)])
    def add_transaction_comment(tx_id: int, req: TransactionCommentCreate):
        return svc.add_transaction_comment(tx_id, req)

    @v1.post("/import/preview", tags=["Transactions"], dependencies=[Depends(require_auth)])
    async def preview_statement_file(
        file: Annotated[UploadFile, File(...)],
        account_id: Annotated[Optional[int], Form()] = None,
        flip_expense_sign: Annotated[bool, Form()] = False,
        detect_transfers: Annotated[bool, Form()] = True,
        institution: Annotated[str, Form()] = "auto",
        skip_pending: Annotated[bool, Form()] = True,
    ):
        raw = await file.read(MAX_UPLOAD_BYTES + 1)
        fname = file.filename or ""
        payload = _read_statement_payload(raw, fname)
        return svc.preview_statement(
            payload,
            filename=fname,
            account_id=account_id,
            flip_expense_sign=flip_expense_sign,
            detect_transfers=detect_transfers,
            institution=institution,
            skip_pending=skip_pending,
        )

    @v1.post("/import/statement", tags=["Transactions"], dependencies=[Depends(require_auth)])
    async def import_statement_file(
        file: Annotated[UploadFile, File(...)],
        account_id: Annotated[Optional[int], Form()] = None,
        flip_expense_sign: Annotated[bool, Form()] = False,
        detect_transfers: Annotated[bool, Form()] = True,
        sync_ofx_balance: Annotated[bool, Form()] = False,
        sync_balance: Annotated[Optional[bool], Form()] = None,
        institution: Annotated[str, Form()] = "auto",
        skip_pending: Annotated[bool, Form()] = True,
    ):
        raw = await file.read(MAX_UPLOAD_BYTES + 1)
        fname = file.filename or ""
        payload = _read_statement_payload(raw, fname)
        return svc.import_statement(
            payload,
            filename=fname,
            account_id=account_id,
            flip_expense_sign=flip_expense_sign,
            detect_transfers=detect_transfers,
            sync_ofx_balance=sync_ofx_balance,
            sync_balance=sync_balance,
            institution=institution,
            skip_pending=skip_pending,
        )

    @v1.post("/import/csv", tags=["Transactions"], dependencies=[Depends(require_auth)])
    async def import_csv_file(
        file: Annotated[UploadFile, File(...)],
        account_id: Annotated[Optional[int], Form()] = None,
        flip_expense_sign: Annotated[bool, Form()] = False,
    ):
        raw = await file.read(MAX_UPLOAD_BYTES + 1)
        text = _decode_upload(raw)
        return svc.import_statement(
            text,
            filename=file.filename or "upload.csv",
            account_id=account_id,
            flip_expense_sign=flip_expense_sign,
        )

    @v1.post("/import/csv-text", tags=["Transactions"], dependencies=[Depends(require_auth)])
    def import_csv_text(req: CsvTextImportRequest):
        return svc.import_csv(req.csv_text, account_id=req.account_id, flip_expense_sign=req.flip_expense_sign)

    @v1.get("/export/numbers", tags=["Exports"], dependencies=[Depends(require_auth)])
    def export_numbers_workbook(days: int = 365, native: bool = False):
        artifact = svc.export_apple_numbers_workbook(
            days=max(30, min(730, int(days))),
            native=bool(native),
        )
        return Response(
            content=artifact.content,
            media_type=artifact.media_type,
            headers={"Content-Disposition": f'attachment; filename="{artifact.filename}"'},
        )

    @v1.get("/export/csv", tags=["Exports"], dependencies=[Depends(require_auth)])
    def export_csv(pack: bool = False, days: int = 365):
        if pack:
            artifact = svc.export_executive_csv_pack_artifact(days=max(30, min(730, int(days))))
            return Response(
                content=artifact.content,
                media_type=artifact.media_type,
                headers={"Content-Disposition": f'attachment; filename="{artifact.filename}"'},
            )
        artifact = svc.export_transactions_csv_artifact()
        return Response(
            content=artifact.content,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="chronox-transactions.csv"'},
        )

    @v1.get("/export/csv-pack", tags=["Exports"], dependencies=[Depends(require_auth)])
    def export_csv_pack(days: int = 365):
        artifact = svc.export_executive_csv_pack_artifact(days=max(30, min(730, int(days))))
        return Response(
            content=artifact.content,
            media_type=artifact.media_type,
            headers={"Content-Disposition": f'attachment; filename="{artifact.filename}"'},
        )

    @v1.get(
        "/export/transactions.csv",
        tags=["Exports"],
        include_in_schema=False,
        dependencies=[Depends(require_auth)],
    )
    def export_transactions_csv_alias():
        artifact = svc.export_transactions_csv_artifact()
        return Response(
            content=artifact.content,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="chronox-transactions.csv"'},
        )

    # --- Firefly Automation Rules & Rule Groups ---

    @v1.get("/rule-groups", tags=["Rules"], dependencies=[Depends(require_auth)])
    def list_rule_groups():
        return svc.list_rule_groups()

    @v1.post("/rule-groups", tags=["Rules"], dependencies=[Depends(require_auth)])
    def create_rule_group(req: RuleGroupCreate):
        return svc.create_rule_group(req)

    @v1.get("/rules", tags=["Rules"], dependencies=[Depends(require_auth)])
    def list_rules(active_only: bool = False):
        return svc.list_rules(active_only=active_only)

    @v1.post("/rules", tags=["Rules"], dependencies=[Depends(require_auth)])
    def create_rule(req: RuleCreate):
        return svc.create_rule(req)

    @v1.patch("/rules/{rule_id}", tags=["Rules"], dependencies=[Depends(require_auth)])
    def update_rule(rule_id: int, req: RuleUpdate):
        updated = svc.update_rule(rule_id, req)
        if not updated:
            raise HTTPException(status_code=404, detail="Rule not found")
        return updated

    @v1.delete("/rules/{rule_id}", tags=["Rules"], dependencies=[Depends(require_auth)])
    def delete_rule(rule_id: int):
        if not svc.delete_rule(rule_id):
            raise HTTPException(status_code=404, detail="Rule not found")
        return {"deleted": True}

    @v1.post("/rules/{rule_id}/test", tags=["Rules"], dependencies=[Depends(require_auth)])
    def test_rule(rule_id: int, lookback_days: int = 730):
        return svc.test_rule(rule_id, lookback_days=max(7, min(730, lookback_days)))

    @v1.post("/rules/{rule_id}/apply", tags=["Rules"], dependencies=[Depends(require_auth)])
    def apply_rule(rule_id: int, lookback_days: int = 730):
        return svc.apply_rule(rule_id, lookback_days=max(7, min(730, lookback_days)))

    # --- Banking Connectors (Plaid Hosted Link BYOK, SimpleFIN, Connections) ---

    def _plaid_callback_base(request: Request) -> Optional[str]:
        netloc = (request.url.netloc or "").strip()
        if not netloc or netloc.lower().startswith("testserver"):
            return None
        return f"{request.url.scheme}://{netloc}"

    @v1.get("/plaid/config", tags=["Connectors"], dependencies=[Depends(require_auth)])
    def get_plaid_config():
        return svc.get_plaid_config()

    @v1.post("/plaid/config", tags=["Connectors"], dependencies=[Depends(require_auth)])
    def plaid_config(req: PlaidConfigRequest):
        return svc.configure_plaid(req)

    @v1.post("/plaid/hosted-link", tags=["Connectors"], dependencies=[Depends(require_auth)])
    def plaid_hosted_link(
        request: Request,
        req: Annotated[Optional[PlaidLinkTokenRequest], Body()] = None,
    ):
        return svc.create_plaid_link_token(
            req or PlaidLinkTokenRequest(),
            callback_base_url=_plaid_callback_base(request),
        )

    @v1.post("/plaid/link-token", tags=["Connectors"], dependencies=[Depends(require_auth)])
    def plaid_link_token(
        request: Request,
        req: Annotated[Optional[PlaidLinkTokenRequest], Body()] = None,
    ):
        return svc.create_plaid_link_token(
            req or PlaidLinkTokenRequest(),
            callback_base_url=_plaid_callback_base(request),
        )

    @v1.post("/plaid/hosted-link/check", tags=["Connectors"], dependencies=[Depends(require_auth)])
    def plaid_hosted_link_check(
        req: Annotated[Optional[PlaidHostedCheckRequest], Body()] = None,
    ):
        lt = req.link_token if req else None
        return svc.check_pending_plaid_link(link_token=lt)

    @v1.get("/plaid/hosted-complete", tags=["Connectors"])
    def plaid_hosted_complete(
        state: str,
        public_token: Optional[str] = None,
    ):
        # Validated via one-time cryptographic `state` nonce + unlocked vault check (top-level OAuth redirect)
        def _render_plaid_card(title: str, subtitle: str, accent: str = "#10b981") -> HTMLResponse:
            body = (
                "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
                f"<title>{html.escape(title)}</title>"
                "<style>body{background:#0b0e14;color:#e5e7eb;font-family:Inter,system-ui,sans-serif;"
                "display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}"
                ".card{background:#121722;border:1px solid #263042;border-radius:2px;padding:32px;max-width:460px}"
                f"h1{{margin:0 0 12px;font-size:20px;color:{accent}}}p{{margin:0;color:#9ca3af;line-height:1.5}}</style>"
                f"</head><body><div class='card'><h1>{html.escape(title)}</h1>"
                f"<p>{html.escape(subtitle)}</p></div></body></html>"
            )
            return HTMLResponse(content=body, status_code=200)

        try:
            res = svc.handle_plaid_hosted_complete(state=state, public_token=public_token)
        except (VaultLockedError, VaultNotInitializedError):
            return _render_plaid_card(
                "Unlock ChronoX to Finish Syncing",
                "Your bank sign-in completed while ChronoX was locked. Open ChronoX and enter your passcode — your pending bank connection will sync automatically.",
                accent="#f59e0b",
            )
        count = int(res.get("exchanged_count", 0))
        imported = int(res.get("imported_count", 0))
        status_label = str(res.get("status") or "completed")
        tx_word = "transaction" if imported == 1 else "transactions"
        inst_word = "institution" if count == 1 else "institutions"
        subtitle = (
            f"Linked {count} {inst_word} and imported {imported} {tx_word}. You can close this tab and return to ChronoX."
            if status_label == "completed"
            else "Your bank sign-in update was received. Return to ChronoX to finish syncing."
        )
        return _render_plaid_card("Bank Connected — Return to ChronoX", subtitle)

    @v1.post("/plaid/poll-link", tags=["Connectors"], dependencies=[Depends(require_auth)])
    def plaid_poll_link(req: PlaidPollLinkRequest):
        return svc.poll_plaid_link(req)

    @v1.post(
        "/plaid/hosted-link/status",
        tags=["Connectors"],
        include_in_schema=False,
        dependencies=[Depends(require_auth)],
    )
    def plaid_hosted_link_status(req: PlaidPollLinkRequest):
        return svc.poll_plaid_link(req)

    @v1.post("/plaid/exchange", tags=["Connectors"], dependencies=[Depends(require_auth)])
    def plaid_exchange(req: PlaidExchangeRequest):
        return svc.exchange_plaid_token(req)

    @v1.post(
        "/plaid/hosted-link/exchange",
        tags=["Connectors"],
        include_in_schema=False,
        dependencies=[Depends(require_auth)],
    )
    def plaid_hosted_link_exchange(req: PlaidExchangeRequest):
        return svc.exchange_plaid_token(req)

    @v1.post("/plaid/sync", tags=["Connectors"], dependencies=[Depends(require_auth)])
    def plaid_sync(req: Annotated[Optional[SyncTriggerRequest], Body()] = None):
        conn_id = req.connection_id if req else None
        return svc.sync_plaid(connection_id=conn_id)

    @v1.post("/simplefin/claim", tags=["Connectors"], dependencies=[Depends(require_auth)])
    def simplefin_claim(req: SimpleFINClaimRequest):
        return svc.claim_simplefin(req)

    @v1.post("/simplefin/sync", tags=["Connectors"], dependencies=[Depends(require_auth)])
    def simplefin_sync(req: Annotated[Optional[SyncTriggerRequest], Body()] = None):
        conn_id = req.connection_id if req else None
        days = req.days if req else 730
        return svc.sync_simplefin(connection_id=conn_id, days=days)

    @v1.get("/connections", tags=["Connectors"], dependencies=[Depends(require_auth)])
    def list_connections():
        return svc.list_connections()

    @v1.delete("/connections/{connection_id}", tags=["Connectors"], dependencies=[Depends(require_auth)])
    def delete_connection(connection_id: int):
        if not svc.delete_connection(connection_id):
            raise HTTPException(status_code=404, detail="Connection not found")
        return {"deleted": True}

    # --- Optional Read-Only IMAP Email Scanner ---

    @v1.get("/email/config", tags=["Email Scanner"], dependencies=[Depends(require_auth)])
    def get_email_config():
        return svc.get_email_config()

    @v1.post("/email/config", tags=["Email Scanner"], dependencies=[Depends(require_auth)])
    def update_email_config(req: EmailConfigUpdate):
        return svc.update_email_config(req)

    @v1.post("/email/scan", tags=["Email Scanner"], dependencies=[Depends(require_auth)])
    def scan_email():
        return svc.scan_email()

    @v1.get("/email/queue", tags=["Email Scanner"], dependencies=[Depends(require_auth)])
    def list_email_queue(status: Optional[str] = None, limit: int = 100):
        return svc.list_email_queue(status=status, limit=max(1, min(500, limit)))

    @v1.post("/email/queue/{item_id}/approve", tags=["Email Scanner"], dependencies=[Depends(require_auth)])
    def approve_email_queue_item(item_id: int):
        return svc.approve_email_queue_item(item_id)

    @v1.post("/email/queue/{item_id}/dismiss", tags=["Email Scanner"], dependencies=[Depends(require_auth)])
    def dismiss_email_queue_item(item_id: int):
        updated = svc.dismiss_email_queue_item(item_id)
        if not updated:
            raise HTTPException(status_code=404, detail="Email queue item not found")
        return updated

    # --- Credit Reports, Inquiries, Derogatories & Factor Analysis ---

    @v1.get("/credit/report", tags=["Credit"], dependencies=[Depends(require_auth)])
    def get_credit_report(
        extra_monthly_cents: int = 25_000,
        include_au: bool = True,
        include_authorized_user: Optional[bool] = None,
        include_sandbox: bool = False,
    ):
        inc_au = include_authorized_user if include_authorized_user is not None else include_au
        return svc.get_credit_report(
            extra_monthly_budget_cents=max(0, min(50_000_000, extra_monthly_cents)),
            include_authorized_user=inc_au,
            include_sandbox=include_sandbox,
        )

    @v1.get("/credit/reports", tags=["Credit"], dependencies=[Depends(require_auth)])
    def list_credit_reports(include_sandbox: bool = False):
        return svc.list_credit_reports(include_sandbox=include_sandbox)

    @v1.post("/credit/reports", tags=["Credit"], dependencies=[Depends(require_auth)])
    def create_credit_report(req: CreditReportCreate):
        return svc.create_credit_report(req)

    @v1.delete("/credit/reports/{report_id}", tags=["Credit"], dependencies=[Depends(require_auth)])
    def delete_credit_report(report_id: int):
        if not svc.delete_credit_report(report_id):
            raise HTTPException(status_code=404, detail="Credit report not found")
        return {"deleted": True}

    @v1.post("/credit/reports/import", tags=["Credit"], dependencies=[Depends(require_auth)])
    async def import_credit_report(
        request: Request,
        confirm: bool = False,
        bureau_hint: str = "auto",
    ):
        ctype = (request.headers.get("content-type") or "").lower()
        if "multipart/form-data" in ctype:
            form = await request.form()
            upload = form.get("file")
            if upload is None or not hasattr(upload, "read"):
                raise HTTPException(status_code=400, detail="Missing 'file' field in multipart upload.")
            raw = await upload.read(MAX_UPLOAD_BYTES + 1)
            if len(raw) > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="Credit report file exceeds 25 MB limit.")
            fname = getattr(upload, "filename", "") or ""
            raw_confirm = str(form.get("confirm", form.get("commit", str(confirm)))).strip().lower()
            do_confirm = raw_confirm in ("1", "true", "yes", "on")
            b_hint = str(form.get("bureau_hint", bureau_hint) or "auto")
            payload = raw if (fname.lower().endswith(".pdf") or raw[:5] == b"%PDF-") else _decode_upload(raw)
            return svc.import_credit_report(payload, filename=fname, bureau_hint=b_hint, confirm=do_confirm)

        raw_body = await request.body()
        if len(raw_body) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Credit report payload exceeds 25 MB limit.")
        if not raw_body.strip():
            raise HTTPException(status_code=400, detail="Empty credit report import payload.")
        try:
            parsed = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid JSON payload: {exc}") from exc
        do_confirm = bool(confirm or (isinstance(parsed, dict) and parsed.get("confirm", True)))
        b_hint = str(parsed.get("bureau_hint", bureau_hint)) if isinstance(parsed, dict) else bureau_hint
        return svc.import_credit_report(parsed, filename="structured.json", bureau_hint=b_hint, confirm=do_confirm)

    @v1.get("/credit/inquiries", tags=["Credit"], dependencies=[Depends(require_auth)])
    def list_credit_inquiries(include_sandbox: bool = False):
        return svc.list_credit_inquiries(include_sandbox=include_sandbox)

    @v1.post("/credit/inquiries", tags=["Credit"], dependencies=[Depends(require_auth)])
    def create_credit_inquiry(req: CreditInquiryCreate):
        return svc.create_credit_inquiry(req)

    @v1.delete("/credit/inquiries/{inquiry_id}", tags=["Credit"], dependencies=[Depends(require_auth)])
    def delete_credit_inquiry(inquiry_id: int):
        if not svc.delete_credit_inquiry(inquiry_id):
            raise HTTPException(status_code=404, detail="Credit inquiry not found")
        return {"deleted": True}

    @v1.get("/credit/derogatories", tags=["Credit"], dependencies=[Depends(require_auth)])
    def list_credit_derogatories(include_sandbox: bool = False):
        return svc.list_credit_derogatories(include_sandbox=include_sandbox)

    @v1.post("/credit/derogatories", tags=["Credit"], dependencies=[Depends(require_auth)])
    def create_credit_derogatory(req: CreditDerogatoryCreate):
        return svc.create_credit_derogatory(req)

    @v1.delete("/credit/derogatories/{derog_id}", tags=["Credit"], dependencies=[Depends(require_auth)])
    def delete_credit_derogatory(derog_id: int):
        if not svc.delete_credit_derogatory(derog_id):
            raise HTTPException(status_code=404, detail="Credit derogatory item not found")
        return {"deleted": True}

    @v1.post("/credit/lines", tags=["Credit"], dependencies=[Depends(require_auth)])
    def create_credit_line(req: CreditLineCreate):
        return svc.create_credit_line(req)

    @v1.patch("/credit/lines/{line_id}", tags=["Credit"], dependencies=[Depends(require_auth)])
    def patch_credit_line(line_id: int, req: CreditLineUpdate):
        updated = svc.update_credit_line(line_id, req)
        if not updated:
            raise HTTPException(status_code=404, detail="Credit line not found")
        return updated

    @v1.delete("/credit/lines/{line_id}", tags=["Credit"], dependencies=[Depends(require_auth)])
    def delete_credit_line(line_id: int):
        if not svc.delete_credit_line(line_id):
            raise HTTPException(status_code=404, detail="Credit line not found")
        return {"deleted": True}

    @v1.post("/credit/balance-transfer", tags=["Credit"], dependencies=[Depends(require_auth)])
    def eval_balance_transfer(req: BalanceTransferRequest):
        return svc.evaluate_balance_transfer(req)

    # --- Optional BYOK Credit Score APIs (Spinwheel / Array / CRS / Custom) ---

    @v1.get("/credit/apis", tags=["Credit"], dependencies=[Depends(require_auth)])
    def list_credit_apis():
        return svc.list_credit_apis()

    @v1.post("/credit/apis/connect", tags=["Credit"], dependencies=[Depends(require_auth)])
    def connect_credit_api(req: CreditAPIConnectRequest):
        return svc.connect_credit_api(req)

    @v1.post("/credit/apis/{connection_id}/test", tags=["Credit"], dependencies=[Depends(require_auth)])
    def test_credit_api(connection_id: int):
        try:
            return svc.test_credit_api(connection_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @v1.post("/credit/apis/{connection_id}/sync", tags=["Credit"], dependencies=[Depends(require_auth)])
    def sync_credit_api(
        connection_id: int,
        req: Annotated[Optional[CreditAPISyncRequest], Body()] = None,
    ):
        try:
            return svc.sync_credit_api(connection_id, req=req)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @v1.delete("/credit/apis/{connection_id}", tags=["Credit"], dependencies=[Depends(require_auth)])
    def disconnect_credit_api(connection_id: int):
        if not svc.disconnect_credit_api(connection_id):
            raise HTTPException(status_code=404, detail="Credit Score API connection not found")
        return {"deleted": True}

    # --- Stocks & Crypto Portfolio + Direct Brokers ---

    @v1.get("/portfolio", tags=["Portfolio"], dependencies=[Depends(require_auth)])
    def get_portfolio():
        return svc.get_portfolio()

    @v1.post("/portfolio/holdings", tags=["Portfolio"], dependencies=[Depends(require_auth)])
    def upsert_holding(req: HoldingCreate):
        return svc.upsert_holding(req)

    @v1.delete("/portfolio/holdings/{holding_id}", tags=["Portfolio"], dependencies=[Depends(require_auth)])
    def delete_holding(holding_id: int):
        if not svc.delete_holding(holding_id):
            raise HTTPException(status_code=404, detail="Holding not found")
        return {"deleted": True}

    @v1.post("/portfolio/refresh-quotes", tags=["Portfolio"], dependencies=[Depends(require_auth)])
    def refresh_portfolio_quotes():
        return svc.refresh_portfolio_quotes(force_refresh=True)

    @v1.post("/portfolio/import-csv", tags=["Portfolio"], dependencies=[Depends(require_auth)])
    async def import_portfolio_csv(request: Request):
        ctype = (request.headers.get("content-type") or "").lower()
        if "multipart/form-data" in ctype:
            form = await request.form()
            upload = form.get("file")
            if upload is None or not hasattr(upload, "read"):
                raise HTTPException(status_code=400, detail="Missing 'file' field in CSV upload.")
            raw = await upload.read(MAX_UPLOAD_BYTES + 1)
            csv_text = _decode_upload(raw)
            source = str(form.get("source") or "csv_import")
            return svc.import_portfolio_csv(csv_text, source=source)

        raw_body = await request.body()
        if len(raw_body) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="CSV import payload exceeds 25 MB limit.")
        if not raw_body.strip():
            raise HTTPException(status_code=400, detail="Empty CSV import payload.")
        try:
            parsed = json.loads(raw_body.decode("utf-8"))
            csv_text = str(parsed.get("csv_text") or "")
            source = str(parsed.get("source") or "csv_import")
        except (UnicodeDecodeError, json.JSONDecodeError):
            csv_text = _decode_upload(raw_body)
            source = "csv_import"
        if not csv_text.strip():
            raise HTTPException(status_code=400, detail="csv_text must be non-empty.")
        return svc.import_portfolio_csv(csv_text, source=source)

    @v1.post("/brokers/coinbase/connect", tags=["Portfolio"], dependencies=[Depends(require_auth)])
    def connect_coinbase(req: BrokerCoinbaseConnectRequest):
        return svc.connect_coinbase(req)

    @v1.post("/brokers/robinhood/connect", tags=["Portfolio"], dependencies=[Depends(require_auth)])
    def connect_robinhood(req: BrokerRobinhoodConnectRequest):
        return svc.connect_robinhood(req)

    @v1.post("/brokers/schwab/connect", tags=["Portfolio"], dependencies=[Depends(require_auth)])
    def connect_schwab(req: BrokerSchwabConnectRequest):
        return svc.connect_schwab(req)

    @v1.post("/brokers/schwab/complete", tags=["Portfolio"], dependencies=[Depends(require_auth)])
    def complete_schwab_oauth(req: BrokerSchwabCompleteRequest):
        return svc.complete_schwab_oauth(req)

    @v1.get("/brokers/schwab/callback", tags=["Portfolio"])
    def schwab_oauth_callback(code: str, state: str):
        # Validated via one-time cryptographic OAuth `state` + unlocked vault check (supports top-level OAuth redirect)
        return svc.handle_schwab_callback(code=code, state=state)

    @v1.post("/brokers/{connection_id}/sync", tags=["Portfolio"], dependencies=[Depends(require_auth)])
    def sync_broker(connection_id: int):
        return svc.sync_broker(connection_id)

    # --- Goal Optimizer & Direct Manipulation ---

    @v1.get("/goals", tags=["Goals"], dependencies=[Depends(require_auth)])
    def list_goals(include_completed: bool = False):
        return svc.list_goals(include_completed=include_completed)

    @v1.post("/goals", tags=["Goals"], dependencies=[Depends(require_auth)])
    def create_goal(req: GoalCreate):
        return svc.create_goal(req)

    @v1.post("/goals/reorder", tags=["Goals"], dependencies=[Depends(require_auth)])
    def reorder_goals(req: GoalReorderRequest):
        return svc.reorder_goals(req)

    @v1.post("/goals/plan", tags=["Goals"], dependencies=[Depends(require_auth)])
    def optimize_goal_plan(req: GoalPlanRequest):
        return svc.optimize_goal_plan(req)

    @v1.patch("/goals/{goal_id}", tags=["Goals"], dependencies=[Depends(require_auth)])
    def patch_goal(goal_id: int, req: GoalUpdate):
        updated = svc.update_goal(goal_id, req)
        if not updated:
            raise HTTPException(status_code=404, detail="Goal not found")
        return updated

    @v1.post("/goals/{goal_id}/contribute", tags=["Goals"], dependencies=[Depends(require_auth)])
    def contribute_goal(goal_id: int, req: GoalContributeRequest):
        try:
            return svc.contribute_goal(goal_id, req)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @v1.post("/goals/{goal_id}/restore", tags=["Goals"], dependencies=[Depends(require_auth)])
    def restore_goal(goal_id: int):
        restored = svc.restore_goal(goal_id)
        if not restored:
            raise HTTPException(status_code=404, detail="Deleted goal not found")
        return restored

    @v1.delete("/goals/{goal_id}", tags=["Goals"], dependencies=[Depends(require_auth)])
    def delete_goal(goal_id: int, hard: bool = False):
        if not svc.delete_goal(goal_id, hard=hard):
            raise HTTPException(status_code=404, detail="Goal not found")
        return {"deleted": True, "recoverable": not hard}

    # --- Recurring Bills & Subscriptions ---

    @v1.get("/bills", tags=["Bills"], dependencies=[Depends(require_auth)])
    def list_bills():
        return svc.get_dashboard().upcoming_bills

    @v1.post("/bills", tags=["Bills"], dependencies=[Depends(require_auth)])
    def create_bill(req: RecurringBillCreate):
        return svc.create_recurring_bill(req)

    @v1.delete("/bills/{bill_id}", tags=["Bills"], dependencies=[Depends(require_auth)])
    def delete_bill(bill_id: int):
        if not svc.deactivate_recurring_bill(bill_id):
            raise HTTPException(status_code=404, detail="Bill not found")
        return {"deactivated": True}

    @v1.patch("/bills/{bill_id}/zombie", tags=["Bills"], dependencies=[Depends(require_auth)])
    def flag_zombie(bill_id: int, req: ZombieFlagRequest):
        updated = svc.flag_zombie_bill(bill_id, flagged=req.flagged)
        if not updated:
            raise HTTPException(status_code=404, detail="Bill not found")
        return updated

    # --- ADHD Impulse Cooling-Off Vault ---

    @v1.get("/impulses", tags=["Impulse Vault"], dependencies=[Depends(require_auth)])
    def list_impulses(include_resolved: bool = False):
        return svc.list_impulses(include_resolved=include_resolved)

    @v1.post("/impulses", tags=["Impulse Vault"], dependencies=[Depends(require_auth)])
    def create_impulse(req: ImpulseCreate):
        return svc.create_impulse(req)

    @v1.post("/impulses/{impulse_id}/decide", tags=["Impulse Vault"], dependencies=[Depends(require_auth)])
    def decide_impulse(impulse_id: int, req: ImpulseDecision):
        return svc.decide_impulse(impulse_id, req)

    # --- 5-Minute Weekly Money Reset ---

    @v1.get("/weekly-reset", tags=["Weekly Reset"], dependencies=[Depends(require_auth)])
    def get_weekly_reset():
        return svc.get_weekly_reset_summary()

    @v1.post("/weekly-reset/complete", tags=["Weekly Reset"], dependencies=[Depends(require_auth)])
    def complete_weekly_reset(req: Annotated[Optional[WeeklyResetCompleteRequest], Body()] = None):
        effective = req or WeeklyResetCompleteRequest()
        return svc.complete_weekly_reset(apply_rebalances=effective.apply_rebalances)

    # --- AI + Statistical Quant One-Tap Optimizer ---

    @v1.post("/optimize/one-tap", tags=["One-Tap Optimization"], dependencies=[Depends(require_auth)])
    def optimize_one_tap(
        req: Annotated[Optional[OneTapOptimizeRequest], Body()] = None,
        mode: Optional[str] = None,
        provider: Optional[str] = None,
    ):
        effective = req or OneTapOptimizeRequest()
        clean_provider = _validate_provider_param(provider) if provider is not None else None
        return svc.run_onetap_optimization(
            horizon_days=effective.horizon_days,
            apply=effective.apply,
            expected_fingerprint=effective.plan_fingerprint,
            mode_override=mode,
            provider_override=clean_provider,
        )

    @v1.post("/optimize/one-tap/undo/{batch_id}", tags=["One-Tap Optimization"], dependencies=[Depends(require_auth)])
    def undo_one_tap(batch_id: int):
        return svc.undo_onetap_batch(batch_id)

    # --- Multi-Mode Intelligence, Local AI CLIs, Unified --say Harness & Agentic Coach ---

    @v1.get("/ai/local-clis", tags=["AI Coach"], dependencies=[Depends(require_auth)])
    def list_local_ai_clis(probe: bool = False):
        return svc.get_local_clis(probe=probe)

    @v1.post("/ai/say", tags=["AI Coach"], dependencies=[Depends(require_auth), Depends(check_ai_rate_limit)])
    def ai_say(req: SayRequest):
        clean_provider = _validate_provider_param(req.provider) if req.provider is not None else None
        return svc.say(
            req.text,
            auto_commit=req.auto_commit,
            mode_override=req.mode,
            provider_override=clean_provider,
        )

    @v1.get("/ai/usage", tags=["AI Coach"], dependencies=[Depends(require_auth)])
    def get_ai_usage(days: int = 30):
        return svc.get_ai_usage(days=max(1, min(365, days)))

    @v1.get("/ai/threads", tags=["AI Coach"], dependencies=[Depends(require_auth)])
    def list_ai_threads():
        return svc.list_ai_threads()

    @v1.delete("/ai/threads/{thread_id}", tags=["AI Coach"], dependencies=[Depends(require_auth)])
    def delete_ai_thread(thread_id: str):
        if not svc.delete_ai_thread(thread_id):
            raise HTTPException(status_code=404, detail="AI thread not found")
        return {"deleted": True}

    @v1.get("/ai/agent/batches", tags=["AI Coach"], dependencies=[Depends(require_auth)])
    def list_agent_batches(limit: int = 30):
        return svc.list_agent_batches(limit=max(1, min(100, limit)))

    @v1.post("/ai/agent/plan", tags=["AI Coach"], dependencies=[Depends(require_auth), Depends(check_ai_rate_limit)])
    def plan_agent_batch(req: AgentPlanRequest, mode: Optional[str] = None):
        return svc.plan_agent_batch(req, mode_override=mode)

    @v1.post("/ai/agent/execute/{batch_id}", tags=["AI Coach"], dependencies=[Depends(require_auth)])
    def execute_agent_batch(batch_id: int):
        return svc.execute_agent_batch(batch_id)

    @v1.post("/ai/agent/undo-last", tags=["AI Coach"], dependencies=[Depends(require_auth)])
    def undo_last_agent_batch(kind: Optional[str] = None):
        if kind:
            return svc.undo_last_action(kind=kind)
        return svc.undo_last_agent_batch()

    @v1.post("/ai/agent/undo/{batch_id}", tags=["AI Coach"], dependencies=[Depends(require_auth)])
    def undo_agent_batch(batch_id: int):
        return svc.undo_agent_batch(batch_id)

    @v1.get("/ai/suggestions", tags=["AI Coach"], dependencies=[Depends(require_auth), Depends(check_ai_rate_limit)])
    def get_ai_suggestions(
        refresh: bool = False,
        mode: Optional[str] = None,
        provider: Optional[str] = None,
    ):
        clean_provider = _validate_provider_param(provider) if provider is not None else None
        return svc.get_ai_suggestions(
            force_refresh=refresh,
            mode_override=mode,
            provider_override=clean_provider,
        )

    @v1.post("/ai/suggestions/apply", tags=["AI Coach"], dependencies=[Depends(require_auth)])
    def apply_suggestion(req: SuggestionApplyRequest):
        try:
            return svc.apply_suggestion_action(req.action_type, req.action_payload)
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @v1.post("/ai/ask", tags=["AI Coach"], dependencies=[Depends(require_auth), Depends(check_ai_rate_limit)])
    def ask_coach(
        req: CoachQuestionRequest,
        mode: Optional[str] = None,
        provider: Optional[str] = None,
    ):
        clean_provider = _validate_provider_param(provider) if provider is not None else None
        return svc.ask_coach(
            req.question,
            mode_override=mode,
            provider_override=clean_provider,
        )

    @v1.get("/ai/models", tags=["AI Coach"], dependencies=[Depends(require_auth)])
    def list_ai_models(provider: str = "auto", refresh: bool = False):
        clean_provider = _validate_provider_param(provider)
        try:
            return svc.list_ai_models(provider=clean_provider, force_refresh=refresh)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @v1.post("/ai/test", tags=["AI Coach"], dependencies=[Depends(require_auth), Depends(check_ai_rate_limit)])
    def test_ai_connection(provider: str = "auto"):
        clean_provider = _validate_provider_param(provider)
        return svc.test_ai_connection(provider=clean_provider)

    @v1.post(
        "/ai/test-connection",
        tags=["AI Coach"],
        include_in_schema=False,
        dependencies=[Depends(require_auth), Depends(check_ai_rate_limit)],
    )
    def test_opencode_connection(provider: str = "auto"):
        clean_provider = _validate_provider_param(provider)
        return svc.test_ai_connection(provider=clean_provider)

    # --- Settings & Sanitized Backup ---

    @v1.get("/settings", tags=["Settings"], dependencies=[Depends(require_auth)])
    def get_settings():
        return svc.get_settings()

    @v1.patch("/settings", tags=["Settings"], dependencies=[Depends(require_auth)])
    def patch_settings(req: ProfileSettingsUpdate):
        return svc.update_settings(req)

    @v1.get("/settings/ai", tags=["Settings"], dependencies=[Depends(require_auth)])
    def get_ai_settings():
        return svc.get_ai_settings()

    @v1.patch("/settings/ai", tags=["Settings"], dependencies=[Depends(require_auth)])
    def patch_ai_settings(req: AISettingsUpdate):
        return svc.update_ai_settings(req)

    @v1.get("/backup/export", tags=["Settings"], dependencies=[Depends(require_auth)])
    def export_backup():
        data = svc.storage.export_backup()
        return JSONResponse(
            content=data,
            headers={"Content-Disposition": 'attachment; filename="chronox-finance-backup.json"'},
        )

    @v1.post("/backup/preview", tags=["Settings"], dependencies=[Depends(require_auth)])
    async def preview_backup(file: Annotated[UploadFile, File(...)]):
        raw = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(raw) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Backup file exceeds 25 MB limit.")
        try:
            payload = json.loads(raw.decode("utf-8"))
            return svc.storage.preview_backup(payload)
        except (ValueError, json.JSONDecodeError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid backup file: {exc}") from exc

    @v1.post("/backup/import", tags=["Settings"], dependencies=[Depends(require_auth)])
    async def import_backup(file: Annotated[UploadFile, File(...)]):
        raw = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(raw) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Backup file exceeds 25 MB limit.")
        try:
            payload = json.loads(raw.decode("utf-8"))
            svc.storage.import_backup(payload)
            sync = svc.run_self_updating_sync(reason="Restored from JSON backup")
            return {"restored": True, "sync": sync}
        except (ValueError, json.JSONDecodeError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid backup file: {exc}") from exc

    # Mount canonical /api/v1/* in OpenAPI + legacy /api/* aliases hidden from OpenAPI (#53)
    app.include_router(v1, prefix="/api/v1")
    app.include_router(v1, prefix="/api", include_in_schema=False)

    # --- Static Frontend ---

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        index_file = WEB_DIR / "index.html"
        if not index_file.exists():
            return PlainTextResponse("ChronoX Finance API running. Web assets not found.")
        return FileResponse(index_file)

    return app
