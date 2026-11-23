"""
FastAPI REST API & Web Application Server for ChronoX Finance.

Security features:
- Strict Content-Security-Policy (no external CDN scripts, no inline script execution)
- X-Content-Type-Options, X-Frame-Options, Referrer-Policy, Permissions-Policy
- CSRF protection via Origin/Host verification on state-changing requests
- Optional PBKDF2 passcode lock with signed HttpOnly SameSite=Strict session cookies
- Sliding-window rate limiting on auth and AI endpoints
- Safe 2 MB upload limit on CSV imports & JSON restores
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated, Any, Dict, Optional
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from chronox_finance.models import (
    AccountCreate,
    AISettingsUpdate,
    BalanceTransferRequest,
    CoachQuestionRequest,
    CreditLineCreate,
    CreditLineUpdate,
    GoalContributeRequest,
    GoalCreate,
    GoalPlanRequest,
    GoalUpdate,
    ImpulseCreate,
    ImpulseDecision,
    JarUpdate,
    PasscodeLoginRequest,
    PasscodeSetupRequest,
    ProfileSettingsUpdate,
    QuickAddRequest,
    RebalanceTransferRequest,
    RecurringBillCreate,
    TransactionCreate,
    TransactionUpdate,
)
from chronox_finance.security import (
    SESSION_COOKIE_NAME,
    SlidingWindowRateLimiter,
    create_session_token,
    verify_session_token,
)
from chronox_finance.service import FinanceService

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB
_VALID_PROVIDERS = {"auto", "opencode_go", "gemini"}


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


def create_app(
    db_path: str = ".chronox/finance.sqlite3",
    service: Optional[FinanceService] = None,
) -> FastAPI:
    """Create and configure the ChronoX Finance FastAPI application."""
    svc = service or FinanceService(db_path=db_path)
    signing_key = svc.secrets.get_or_create_signing_key()

    auth_limiter = SlidingWindowRateLimiter(max_requests=10, window_seconds=60)
    ai_limiter = SlidingWindowRateLimiter(max_requests=20, window_seconds=60)

    app = FastAPI(
        title="ChronoX Finance",
        version="1.1.0",
        description="ADHD-Friendly Self-Updating Auto-Budgeting, Quant Math Engine & Multi-Provider AI Coach",
        docs_url="/api/docs",
        redoc_url=None,
    )
    app.state.service = svc

    # --- Global Structured JSON Exception Handlers (never leak stack traces) ---

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
            clean_errors.append({"field": loc or "request", "message": msg, "type": str(err.get("type", "value_error"))})
            summary_parts.append(f"{loc}: {msg}" if loc else msg)
        detail = "; ".join(summary_parts) if summary_parts else "Request validation failed."
        return JSONResponse(
            status_code=422,
            content={"detail": detail, "code": "validation_error", "errors": clean_errors},
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

    # --- Security Headers & CSRF Origin Middleware ---

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        # CSRF check on mutating API calls when Origin header is present (browser requests)
        if request.url.path.startswith("/api/") and request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("origin")
            if origin:
                parsed_origin = urlparse(origin)
                origin_host = parsed_origin.netloc.lower()
                req_host = (request.headers.get("host") or "").lower()
                if origin_host and req_host and origin_host != req_host:
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "Cross-origin state-changing request blocked (CSRF protection)."},
                    )

        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else "public, max-age=60"
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

    # --- Auth Dependency ---

    def require_auth(request: Request) -> None:
        if not svc.is_auth_enabled():
            return
        token = request.cookies.get(SESSION_COOKIE_NAME, "")
        if not token or verify_session_token(token, signing_key) is None:
            raise HTTPException(status_code=401, detail="Locked — please unlock with your passcode.")

    def check_ai_rate_limit(request: Request) -> None:
        client_ip = request.client.host if request.client else "local"
        allowed, retry_after = ai_limiter.allow(client_ip)
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail=f"Slow down a moment — try again in {retry_after}s.",
            )

    # --- Health & Auth Routes ---

    @app.get("/api/health")
    def health():
        return svc.get_health()

    @app.get("/api/auth/status")
    def auth_status(request: Request) -> Dict[str, Any]:
        enabled = svc.is_auth_enabled()
        token = request.cookies.get(SESSION_COOKIE_NAME, "")
        authenticated = (not enabled) or (bool(token) and verify_session_token(token, signing_key) is not None)
        return {"auth_enabled": enabled, "authenticated": authenticated}

    @app.post("/api/auth/setup")
    def auth_setup(req: PasscodeSetupRequest, request: Request, response: Response) -> Dict[str, Any]:
        # If already enabled, require existing valid session before changing passcode
        if svc.is_auth_enabled():
            require_auth(request)
        svc.enable_passcode(req.passcode)
        token = create_session_token(signing_key)
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=token,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            max_age=60 * 60 * 24 * 7,
        )
        return {"ok": True, "auth_enabled": True, "message": "Local passcode lock enabled."}

    @app.post("/api/auth/login")
    def auth_login(req: PasscodeLoginRequest, request: Request, response: Response) -> Dict[str, Any]:
        client_ip = request.client.host if request.client else "local"
        allowed, retry_after = auth_limiter.allow(client_ip)
        if not allowed:
            raise HTTPException(status_code=429, detail=f"Too many unlock attempts. Try again in {retry_after}s.")
        if not svc.verify_owner_passcode(req.passcode):
            raise HTTPException(status_code=401, detail="Incorrect passcode.")
        token = create_session_token(signing_key)
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=token,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            max_age=60 * 60 * 24 * 7,
        )
        return {"ok": True, "authenticated": True}

    @app.post("/api/auth/logout")
    def auth_logout(response: Response) -> Dict[str, Any]:
        response.delete_cookie(SESSION_COOKIE_NAME)
        return {"ok": True}

    @app.post("/api/auth/disable", dependencies=[Depends(require_auth)])
    def auth_disable(req: PasscodeLoginRequest, response: Response) -> Dict[str, Any]:
        if not svc.disable_passcode(req.passcode):
            raise HTTPException(status_code=401, detail="Current passcode did not match.")
        response.delete_cookie(SESSION_COOKIE_NAME)
        return {"ok": True, "auth_enabled": False}

    # --- Dashboard, Quant Forecast & Self-Updating Auto-Budget ---

    @app.get("/api/dashboard", dependencies=[Depends(require_auth)])
    def get_dashboard():
        return svc.get_dashboard()

    @app.get("/api/quant/forecast", dependencies=[Depends(require_auth)])
    def get_quant_forecast(days: int = 30):
        return svc.get_quant_forecast(days=max(7, min(90, days)))

    @app.post("/api/budget/sync", dependencies=[Depends(require_auth)])
    def trigger_budget_sync():
        return svc.run_self_updating_sync(reason="Manual refresh")

    @app.post("/api/budget/rebalance", dependencies=[Depends(require_auth)])
    def rebalance_jars(req: RebalanceTransferRequest):
        try:
            return svc.rebalance_jars(req)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/chronox/safe-trading-capital", dependencies=[Depends(require_auth)])
    def safe_trading_capital():
        dash = svc.get_dashboard()
        return {
            "safe_to_invest_cents": dash.safe_to_invest_cents,
            "safe_to_invest_dollars": round(dash.safe_to_invest_cents / 100.0, 2),
            "guardrail_active": True,
            "note": "Only funds remaining AFTER Bills, Credit Cards, and Emergency Buffer are 100% shielded.",
        }

    # --- Accounts & Jars ---

    @app.post("/api/accounts", dependencies=[Depends(require_auth)])
    def create_account(req: AccountCreate):
        return svc.create_account(req)

    @app.patch("/api/accounts/{account_id}/balance", dependencies=[Depends(require_auth)])
    def patch_account_balance(account_id: int, req: AccountBalancePatch):
        updated = svc.update_account_balance(account_id, req.balance_cents)
        if not updated:
            raise HTTPException(status_code=404, detail="Account not found")
        return updated

    @app.patch("/api/jars/{jar_id}", dependencies=[Depends(require_auth)])
    def patch_jar(jar_id: int, req: JarUpdate):
        updated = svc.update_jar(jar_id, req)
        if not updated:
            raise HTTPException(status_code=404, detail="Jar not found")
        return updated

    # --- Transactions, Quick-Add & CSV Import/Export ---

    @app.post("/api/quick-add", dependencies=[Depends(require_auth)])
    def quick_add(req: QuickAddRequest):
        try:
            return svc.quick_add(req)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/transactions", dependencies=[Depends(require_auth)])
    def list_transactions(
        limit: int = 100,
        jar_id: Optional[int] = None,
        needs_review: bool = False,
        search: Optional[str] = None,
    ):
        return svc.list_transactions(
            limit=max(1, min(500, limit)),
            jar_id=jar_id,
            needs_review_only=needs_review,
            search=search,
        )

    @app.post("/api/transactions", dependencies=[Depends(require_auth)])
    def create_transaction(req: TransactionCreate):
        return svc.create_transaction(req)

    @app.patch("/api/transactions/{tx_id}", dependencies=[Depends(require_auth)])
    def update_transaction(tx_id: int, req: TransactionUpdate):
        try:
            updated = svc.update_transaction(tx_id, req)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not updated:
            raise HTTPException(status_code=404, detail="Transaction not found")
        return updated

    @app.delete("/api/transactions/{tx_id}", dependencies=[Depends(require_auth)])
    def delete_transaction(tx_id: int):
        if not svc.delete_transaction(tx_id):
            raise HTTPException(status_code=404, detail="Transaction not found")
        return {"deleted": True}

    def _decode_upload(raw: bytes) -> str:
        if len(raw) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Statement file exceeds 5 MB safety limit.")
        for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("latin-1", errors="replace")

    @app.post("/api/import/preview", dependencies=[Depends(require_auth)])
    async def preview_statement_file(
        file: Annotated[UploadFile, File(...)],
        account_id: Annotated[Optional[int], Form()] = None,
        flip_expense_sign: Annotated[bool, Form()] = False,
        detect_transfers: Annotated[bool, Form()] = True,
    ):
        raw = await file.read(MAX_UPLOAD_BYTES + 1)
        text = _decode_upload(raw)
        return svc.preview_statement(
            text,
            filename=file.filename or "",
            account_id=account_id,
            flip_expense_sign=flip_expense_sign,
            detect_transfers=detect_transfers,
        )

    @app.post("/api/import/statement", dependencies=[Depends(require_auth)])
    async def import_statement_file(
        file: Annotated[UploadFile, File(...)],
        account_id: Annotated[Optional[int], Form()] = None,
        flip_expense_sign: Annotated[bool, Form()] = False,
        detect_transfers: Annotated[bool, Form()] = True,
        sync_ofx_balance: Annotated[bool, Form()] = False,
    ):
        raw = await file.read(MAX_UPLOAD_BYTES + 1)
        text = _decode_upload(raw)
        return svc.import_statement(
            text,
            filename=file.filename or "",
            account_id=account_id,
            flip_expense_sign=flip_expense_sign,
            detect_transfers=detect_transfers,
            sync_ofx_balance=sync_ofx_balance,
        )

    @app.post("/api/import/csv", dependencies=[Depends(require_auth)])
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

    @app.post("/api/import/csv-text", dependencies=[Depends(require_auth)])
    def import_csv_text(req: CsvTextImportRequest):
        return svc.import_csv(req.csv_text, account_id=req.account_id, flip_expense_sign=req.flip_expense_sign)

    # --- Credit Score & Debt Optimizer ---

    @app.get("/api/credit/report", dependencies=[Depends(require_auth)])
    def get_credit_report(extra_monthly_cents: int = 25_000):
        return svc.get_credit_report(extra_monthly_budget_cents=max(0, min(50_000_000, extra_monthly_cents)))

    @app.post("/api/credit/lines", dependencies=[Depends(require_auth)])
    def create_credit_line(req: CreditLineCreate):
        return svc.create_credit_line(req)

    @app.patch("/api/credit/lines/{line_id}", dependencies=[Depends(require_auth)])
    def patch_credit_line(line_id: int, req: CreditLineUpdate):
        updated = svc.update_credit_line(line_id, req)
        if not updated:
            raise HTTPException(status_code=404, detail="Credit line not found")
        return updated

    @app.delete("/api/credit/lines/{line_id}", dependencies=[Depends(require_auth)])
    def delete_credit_line(line_id: int):
        if not svc.delete_credit_line(line_id):
            raise HTTPException(status_code=404, detail="Credit line not found")
        return {"deleted": True}

    @app.post("/api/credit/balance-transfer", dependencies=[Depends(require_auth)])
    def eval_balance_transfer(req: BalanceTransferRequest):
        return svc.evaluate_balance_transfer(req)

    # --- Goal Optimizer & Purchase Planner ---

    @app.get("/api/goals", dependencies=[Depends(require_auth)])
    def list_goals(include_completed: bool = False):
        return svc.list_goals(include_completed=include_completed)

    @app.post("/api/goals", dependencies=[Depends(require_auth)])
    def create_goal(req: GoalCreate):
        return svc.create_goal(req)

    @app.patch("/api/goals/{goal_id}", dependencies=[Depends(require_auth)])
    def patch_goal(goal_id: int, req: GoalUpdate):
        updated = svc.update_goal(goal_id, req)
        if not updated:
            raise HTTPException(status_code=404, detail="Goal not found")
        return updated

    @app.post("/api/goals/{goal_id}/contribute", dependencies=[Depends(require_auth)])
    def contribute_goal(goal_id: int, req: GoalContributeRequest):
        try:
            return svc.contribute_goal(goal_id, req)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.delete("/api/goals/{goal_id}", dependencies=[Depends(require_auth)])
    def delete_goal(goal_id: int):
        if not svc.delete_goal(goal_id):
            raise HTTPException(status_code=404, detail="Goal not found")
        return {"deleted": True}

    @app.post("/api/goals/plan", dependencies=[Depends(require_auth)])
    def optimize_goal_plan(req: GoalPlanRequest):
        return svc.optimize_goal_plan(req)

    @app.get("/api/export/csv", dependencies=[Depends(require_auth)])
    def export_csv():
        csv_body = svc.export_csv()
        return PlainTextResponse(
            content=csv_body,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="chronox-transactions.csv"'},
        )

    # --- Recurring Bills & Subscriptions ---

    @app.get("/api/bills", dependencies=[Depends(require_auth)])
    def list_bills():
        return svc.get_dashboard().upcoming_bills

    @app.post("/api/bills", dependencies=[Depends(require_auth)])
    def create_bill(req: RecurringBillCreate):
        try:
            return svc.create_recurring_bill(req)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/bills/{bill_id}", dependencies=[Depends(require_auth)])
    def delete_bill(bill_id: int):
        if not svc.deactivate_recurring_bill(bill_id):
            raise HTTPException(status_code=404, detail="Bill not found")
        return {"deactivated": True}

    @app.patch("/api/bills/{bill_id}/zombie", dependencies=[Depends(require_auth)])
    def flag_zombie(bill_id: int, req: ZombieFlagRequest):
        updated = svc.flag_zombie_bill(bill_id, flagged=req.flagged)
        if not updated:
            raise HTTPException(status_code=404, detail="Bill not found")
        return updated

    # --- ADHD Impulse Cooling-Off Vault ---

    @app.get("/api/impulses", dependencies=[Depends(require_auth)])
    def list_impulses(include_resolved: bool = False):
        return svc.list_impulses(include_resolved=include_resolved)

    @app.post("/api/impulses", dependencies=[Depends(require_auth)])
    def create_impulse(req: ImpulseCreate):
        return svc.create_impulse(req)

    @app.post("/api/impulses/{impulse_id}/decide", dependencies=[Depends(require_auth)])
    def decide_impulse(impulse_id: int, req: ImpulseDecision):
        try:
            return svc.decide_impulse(impulse_id, req)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # --- 5-Minute Weekly Money Reset ---

    @app.get("/api/weekly-reset", dependencies=[Depends(require_auth)])
    def get_weekly_reset():
        return svc.get_weekly_reset_summary()

    @app.post("/api/weekly-reset/complete", dependencies=[Depends(require_auth)])
    def complete_weekly_reset(req: WeeklyResetCompleteRequest):
        return svc.complete_weekly_reset(apply_rebalances=req.apply_rebalances)

    # --- Multi-Mode Intelligence & Multi-Provider AI Coach ---

    def _validate_provider_param(provider: str) -> str:
        cleaned = (provider or "auto").strip().lower()
        if cleaned not in _VALID_PROVIDERS:
            raise HTTPException(
                status_code=400,
                detail="Unsupported AI provider. Use 'auto', 'opencode_go', or 'gemini'.",
            )
        return cleaned

    @app.get("/api/ai/suggestions", dependencies=[Depends(require_auth), Depends(check_ai_rate_limit)])
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

    @app.post("/api/ai/suggestions/apply", dependencies=[Depends(require_auth)])
    def apply_suggestion(req: SuggestionApplyRequest):
        try:
            return svc.apply_suggestion_action(req.action_type, req.action_payload)
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/ai/ask", dependencies=[Depends(require_auth), Depends(check_ai_rate_limit)])
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

    @app.get("/api/ai/models", dependencies=[Depends(require_auth)])
    def list_ai_models(provider: str = "auto", refresh: bool = False):
        clean_provider = _validate_provider_param(provider)
        try:
            return svc.list_ai_models(provider=clean_provider, force_refresh=refresh)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/ai/test", dependencies=[Depends(require_auth), Depends(check_ai_rate_limit)])
    def test_ai_connection(provider: str = "auto"):
        clean_provider = _validate_provider_param(provider)
        return svc.test_ai_connection(provider=clean_provider)

    @app.post("/api/ai/test-connection", dependencies=[Depends(require_auth), Depends(check_ai_rate_limit)])
    def test_opencode_connection(provider: str = "auto"):
        clean_provider = _validate_provider_param(provider)
        return svc.test_ai_connection(provider=clean_provider)

    # --- Settings, Backup & Demo ---

    @app.get("/api/settings", dependencies=[Depends(require_auth)])
    def get_settings():
        return svc.get_settings()

    @app.patch("/api/settings", dependencies=[Depends(require_auth)])
    def patch_settings(req: ProfileSettingsUpdate):
        try:
            return svc.update_settings(req)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/settings/ai", dependencies=[Depends(require_auth)])
    def get_ai_settings():
        return svc.get_ai_settings()

    @app.patch("/api/settings/ai", dependencies=[Depends(require_auth)])
    def patch_ai_settings(req: AISettingsUpdate):
        try:
            return svc.update_ai_settings(req)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/backup/export", dependencies=[Depends(require_auth)])
    def export_backup():
        data = svc.storage.export_backup()
        return JSONResponse(
            content=data,
            headers={"Content-Disposition": 'attachment; filename="chronox-finance-backup.json"'},
        )

    @app.post("/api/backup/import", dependencies=[Depends(require_auth)])
    async def import_backup(file: Annotated[UploadFile, File(...)]):
        raw = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(raw) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Backup file exceeds 5 MB limit.")
        try:
            payload = json.loads(raw.decode("utf-8"))
            svc.storage.import_backup(payload)
            sync = svc.run_self_updating_sync(reason="Restored from JSON backup")
            return {"restored": True, "sync": sync}
        except (ValueError, json.JSONDecodeError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid backup file: {exc}") from exc

    @app.post("/api/demo/load", dependencies=[Depends(require_auth)])
    def load_demo():
        return svc.load_demo_workspace()

    # --- Static Frontend ---

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/")
    def index():
        index_file = WEB_DIR / "index.html"
        if not index_file.exists():
            return PlainTextResponse("ChronoX Finance API running. Web assets not found.")
        return FileResponse(index_file)

    return app
