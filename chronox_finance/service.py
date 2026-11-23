"""
ChronoX Finance Service Facade.

Coordinates storage, self-updating auto-budgeting loop, recurring bill detection,
ADHD behavioral mechanics, universal CSV import/export, local passcode security,
and the grounded OpenCode Go AI Financial Coach.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

from chronox_finance.ai.coach import FinancialCoach
from chronox_finance.ai.gemini_client import (
    DEFAULT_GEMINI_BASE_URL,
    DEFAULT_GEMINI_MODEL,
    GeminiClient,
)
from chronox_finance.ai.opencode_client import LLMClient, OpenCodeGoClient
from chronox_finance.engines.adhd import (
    build_adhd_stats,
    compute_weekly_reset_update,
    enrich_impulse,
    is_reset_done_this_week,
)
from chronox_finance.engines.autobudget import AutoBudgetEngine, AutoBudgetResult, current_cycle_ym
from chronox_finance.engines.categorizer import TransactionCategorizer, normalize_merchant
from chronox_finance.engines.credit_debt import CreditDebtOptimizer
from chronox_finance.engines.goal_planner import GoalOptimizerEngine
from chronox_finance.engines.importer import export_transactions_csv, ingest_statement
from chronox_finance.engines.nlp_parser import parse_quick_add
from chronox_finance.engines.quant_math import QuantMathEngine
from chronox_finance.engines.recurring import (
    detect_bill_anomalies,
    detect_payday_schedule,
    detect_recurring_bills,
)
from chronox_finance.models import (
    AccountCreate,
    AccountOut,
    AIProvider,
    AISettingsOut,
    AISettingsUpdate,
    BalanceTransferRequest,
    BalanceTransferResult,
    BudgetAuditEvent,
    CoachAnswerResponse,
    CreditLineCreate,
    CreditLineUpdate,
    CreditOptimizationReport,
    DashboardResponse,
    GoalCategory,
    GoalContributeRequest,
    GoalCreate,
    GoalOut,
    GoalPlanRequest,
    GoalPlanResponse,
    GoalUpdate,
    HealthResponse,
    ImpulseCreate,
    ImpulseDecision,
    ImpulseOut,
    ImpulseStatus,
    IntelligenceMode,
    JarKind,
    JarUpdate,
    ProfileSettings,
    ProfileSettingsUpdate,
    QuantForecastReport,
    QuickAddRequest,
    RebalanceTransferRequest,
    RecurringBillCreate,
    SuggestionActionType,
    SuggestionBatchResponse,
    TransactionCreate,
    TransactionOut,
    TransactionType,
    TransactionUpdate,
    WeeklyResetSummary,
    utc_now_iso,
)
from chronox_finance.security import (
    LocalSecretStore,
    hash_passcode,
    mask_secret,
    validate_external_api_url,
    verify_passcode,
)
from chronox_finance.storage import FinanceStorage, compute_tx_fingerprint


class FinanceService:
    """Primary application service for ChronoX Finance."""

    def __init__(
        self,
        db_path: str = ".chronox/finance.sqlite3",
        secret_store_path: Optional[Path] = None,
        opencode_transport: Any = None,
        gemini_transport: Any = None,
    ):
        self.db_path = db_path
        self.storage = FinanceStorage(db_path=db_path)
        if secret_store_path is not None:
            store_file = secret_store_path
        elif db_path == ":memory:":
            store_file = Path(".chronox/test_secrets.json")
        else:
            store_file = Path(db_path).parent / "secrets.json"
        self.secrets = LocalSecretStore(store_file)
        self.categorizer = TransactionCategorizer()
        self.autobudget = AutoBudgetEngine()
        self.credit_optimizer = CreditDebtOptimizer()
        self.goal_optimizer = GoalOptimizerEngine()
        self.quant_engine = QuantMathEngine()
        self._opencode_transport = opencode_transport
        self._gemini_transport = gemini_transport

    def close(self) -> None:
        self.storage.close()

    # --- Multi-Provider AI Client & Key Resolution (OpenCode Go + Google Gemini) ---

    def _resolve_opencode_key_with_source(self) -> Tuple[str, Literal["saved", "env", "none"]]:
        saved = self.secrets.get("opencode_go_api_key").strip()
        if saved:
            return saved, "saved"
        env_key = (os.getenv("OPENCODE_GO_API_KEY") or os.getenv("OPENCODE_API_KEY") or "").strip()
        if env_key:
            return env_key, "env"
        return "", "none"

    def _resolve_opencode_api_key(self) -> str:
        return self._resolve_opencode_key_with_source()[0]

    def _resolve_gemini_key_with_source(self) -> Tuple[str, Literal["saved", "env", "none"]]:
        saved = self.secrets.get("gemini_api_key").strip()
        if saved:
            return saved, "saved"
        env_key = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
        if env_key:
            return env_key, "env"
        return "", "none"

    def _resolve_gemini_api_key(self) -> str:
        return self._resolve_gemini_key_with_source()[0]

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

    def resolve_active_provider(
        self,
        provider_override: Optional[str] = None,
    ) -> Literal["opencode_go", "gemini", "none"]:
        pref = self._resolve_ai_provider_setting(override=provider_override)
        has_oc = bool(self._resolve_opencode_api_key())
        has_gem = bool(self._resolve_gemini_api_key())
        if pref == AIProvider.OPENCODE_GO:
            return "opencode_go" if has_oc else "none"
        if pref == AIProvider.GEMINI:
            return "gemini" if has_gem else "none"
        # Auto mode: prefer OpenCode Go if configured, else Gemini if configured
        if has_oc:
            return "opencode_go"
        if has_gem:
            return "gemini"
        return "none"

    def build_opencode_client(self) -> OpenCodeGoClient:
        settings = self.storage.get_settings()
        return OpenCodeGoClient(
            api_key=self._resolve_opencode_api_key(),
            base_url=settings.get("opencode_base_url") or os.getenv("OPENCODE_GO_BASE_URL", "https://opencode.ai/zen/go/v1"),
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

    def _build_coach_clients(
        self,
        provider_override: Optional[str] = None,
    ) -> Tuple[Optional[LLMClient], Optional[LLMClient]]:
        """Resolve (primary_client, fallback_client) honoring ai_provider preference."""
        pref = self._resolve_ai_provider_setting(override=provider_override)
        oc = self.build_opencode_client()
        gem = self.build_gemini_client()
        if pref == AIProvider.OPENCODE_GO:
            return (oc, None)
        if pref == AIProvider.GEMINI:
            return (gem, None)
        # Auto: prefer OpenCode Go primary + Gemini fallback, or Gemini primary if only Gemini configured
        if oc.is_configured:
            return (oc, gem if gem.is_configured else None)
        if gem.is_configured:
            return (gem, None)
        return (None, None)

    def get_ai_status(self) -> Dict[str, Any]:
        settings = self.storage.get_settings()
        oc_key, oc_source = self._resolve_opencode_key_with_source()
        gem_key, gem_source = self._resolve_gemini_key_with_source()
        mode = self._resolve_intelligence_mode(settings)
        provider_pref = self._resolve_ai_provider_setting(settings)
        active = self.resolve_active_provider()
        max_daily = int(settings.get("opencode_max_daily_requests", 60))
        used_today = self.storage.count_ai_requests_today()

        oc_model = settings.get("opencode_model", "glm-5.3-flash")
        gem_model = settings.get("gemini_model", DEFAULT_GEMINI_MODEL)

        if active == "gemini":
            active_label = "Google Gemini"
            active_model = gem_model
            active_masked = mask_secret(gem_key)
            active_url = settings.get("gemini_base_url", DEFAULT_GEMINI_BASE_URL)
        elif active == "opencode_go":
            active_label = "OpenCode Go"
            active_model = oc_model
            active_masked = mask_secret(oc_key)
            active_url = settings.get("opencode_base_url", "https://opencode.ai/zen/go/v1")
        else:
            active_label = "Quant Math Local" if mode == IntelligenceMode.DETERMINISTIC_ONLY else "OpenCode Go"
            active_model = gem_model if provider_pref == AIProvider.GEMINI else oc_model
            active_masked = ""
            active_url = (
                settings.get("gemini_base_url", DEFAULT_GEMINI_BASE_URL)
                if provider_pref == AIProvider.GEMINI
                else settings.get("opencode_base_url", "https://opencode.ai/zen/go/v1")
            )

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
            "opencode_key_configured": bool(oc_key),
            "opencode_key_masked": mask_secret(oc_key),
            "opencode_key_source": oc_source,
            "opencode_model": oc_model,
            "gemini_api_key_set": bool(gem_key),
            "gemini_key_masked": mask_secret(gem_key),
            "gemini_key_source": gem_source,
            "gemini_model": gem_model,
            "requests_used_today": used_today,
            "max_daily_requests": max_daily,
            "quota_remaining_today": max(0, max_daily - used_today),
        }

    def get_health(self) -> HealthResponse:
        """Public readiness & health probe (never exposes any secret)."""
        db_ok = self.storage.ping()
        mode = IntelligenceMode.HYBRID.value
        active = "none"
        auth_enabled = False
        if db_ok:
            settings = self.storage.get_settings()
            mode = self._resolve_intelligence_mode(settings).value
            active = self.resolve_active_provider()
            auth_enabled = bool(settings.get("passcode_hash", ""))
        return HealthResponse(
            status="ok" if db_ok else "degraded",
            app="ChronoX Finance",
            version="1.1.0",
            db_ok=db_ok,
            auth_enabled=auth_enabled,
            intelligence_mode=mode,
            active_provider=active,
        )

    # --- Security / Passcode Lock ---

    def is_auth_enabled(self) -> bool:
        settings = self.storage.get_settings()
        return bool(settings.get("passcode_hash", ""))

    def enable_passcode(self, passcode: str) -> None:
        hashed = hash_passcode(passcode)
        self.storage.update_settings({"passcode_hash": hashed})
        self.storage.log_budget_event("security", "Local passcode lock enabled")

    def disable_passcode(self, current_passcode: str) -> bool:
        settings = self.storage.get_settings()
        stored = settings.get("passcode_hash", "")
        if stored and not verify_passcode(current_passcode, stored):
            return False
        self.storage.update_settings({"passcode_hash": ""})
        self.storage.log_budget_event("security", "Local passcode lock disabled")
        return True

    def verify_owner_passcode(self, passcode: str) -> bool:
        stored = self.storage.get_settings().get("passcode_hash", "")
        if not stored:
            return True
        return verify_passcode(passcode, stored)

    # --- Self-Updating Auto-Budget Core Loop ---

    def _compute_budget(self, today: Optional[date] = None) -> AutoBudgetResult:
        ref = today or date.today()
        ym = current_cycle_ym(ref)
        since_180d = (ref - timedelta(days=180)).isoformat()
        return self.autobudget.compute(
            accounts=self.storage.list_accounts(),
            jars=self.storage.list_jars(),
            transactions=self.storage.list_transactions(limit=2000, since_date=since_180d),
            recurring_bills=self.storage.list_recurring_bills(active_only=True),
            transfers=self.storage.list_budget_transfers(ym),
            settings=self.storage.get_settings(),
            credit_lines=self.storage.list_credit_lines(active_only=True),
            today=ref,
        )

    def _apply_month_rollover_if_needed(self, today: date) -> int:
        """
        Automatically carry over prior-month jar surpluses when a new calendar month begins.

        Surplus up to `rollover_cap_cents` rolls into each jar; overflow above the cap
        automatically sweeps into the `Safety & Future` jar. Idempotent via `last_rollover_ym`.
        """
        settings = self.storage.get_settings()
        ym_now = current_cycle_ym(today)
        last_ym = str(settings.get("last_rollover_ym") or "")
        if not last_ym:
            self.storage.update_settings({"last_rollover_ym": ym_now})
            return 0
        if last_ym >= ym_now:
            return 0

        # Compute prior-month spending per jar to determine rollover surplus
        prev_first = date(today.year, today.month, 1) - timedelta(days=1)
        prev_ym = current_cycle_ym(prev_first)
        since_prev = date(prev_first.year, prev_first.month, 1).isoformat()
        txs = self.storage.list_transactions(limit=2000, since_date=since_prev)
        spent_prev: Dict[int, int] = {}
        for t in txs:
            if t.get("tx_type") == "expense" and t["tx_date"][:7] == prev_ym and t.get("jar_id") is not None:
                jid = int(t["jar_id"])
                spent_prev[jid] = spent_prev.get(jid, 0) + int(t["amount_cents"])

        jars = self.storage.list_jars()
        safety_jar = next((j for j in jars if j["kind"] == JarKind.SAFETY.value), None)
        overflow_to_safety = 0
        rolled_jars = 0

        for j in jars:
            jid = int(j["id"])
            if not j.get("rollover_enabled", 1):
                self.storage.update_jar(jid, {"rollover_cents": 0})
                continue
            prev_avail = int(j.get("auto_allocated_cents", 0)) + int(j.get("rollover_cents", 0)) - spent_prev.get(jid, 0)
            # Never punish user with negative debt rollover; clamp at 0
            surplus = max(0, prev_avail)
            cap = j.get("rollover_cap_cents")
            if cap is not None and surplus > int(cap):
                overflow_to_safety += surplus - int(cap)
                surplus = int(cap)
            self.storage.update_jar(jid, {"rollover_cents": surplus})
            if surplus > 0:
                rolled_jars += 1

        if overflow_to_safety > 0 and safety_jar:
            sid = int(safety_jar["id"])
            curr_safety = self.storage.get_jar(sid)
            if curr_safety:
                self.storage.update_jar(sid, {"rollover_cents": int(curr_safety["rollover_cents"]) + overflow_to_safety})

        self.storage.update_settings({"last_rollover_ym": ym_now})
        msg = f"Month rollover ({prev_ym} → {ym_now}): rolled surplus across {rolled_jars} jar(s)"
        if overflow_to_safety > 0:
            msg += f" + swept ${overflow_to_safety / 100:,.2f} overflow into Safety & Future"
        self.storage.log_budget_event("month_rollover", msg, {"from_ym": prev_ym, "to_ym": ym_now, "overflow_cents": overflow_to_safety})
        return rolled_jars

    def run_self_updating_sync(self, reason: str = "Automatic refresh", today: Optional[date] = None) -> Dict[str, Any]:
        """
        Execute the full Self-Updating Auto-Budget Loop:
        0. Automatically roll over prior-month surplus if a new calendar month started.
        1. Scan 180-day transaction history for recurring bills & silent price hikes.
        2. Infer updated payday schedule & paycheck amount from recent deposits.
        3. Recompute adaptive jar allocations and persist updated auto_allocated_cents.
        4. Log concise audit event whenever material shifts occur.
        """
        ref = today or date.today()
        self._apply_month_rollover_if_needed(ref)
        since_180d = (ref - timedelta(days=180)).isoformat()
        txs = self.storage.list_transactions(limit=2000, since_date=since_180d)
        settings = self.storage.get_settings()

        # 1. Detect recurring bills & update table
        detected_bills = detect_recurring_bills(txs, today=ref)
        existing_by_norm = {b["normalized_merchant"]: b for b in self.storage.list_recurring_bills(active_only=False)}
        bills_upserted = 0
        price_hikes_found: List[str] = []

        for dbill in detected_bills:
            existing = existing_by_norm.get(dbill.normalized_merchant)
            # Respect user explicitly deactivating a cancelled subscription
            if existing and not existing.get("active", 1):
                continue
            saved = self.storage.upsert_recurring_bill(
                name=dbill.name,
                normalized_merchant=dbill.normalized_merchant,
                amount_cents=dbill.amount_cents,
                previous_amount_cents=dbill.previous_amount_cents,
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

        # 2. Update inferred payday schedule if high-confidence paychecks detected
        payday = detect_payday_schedule(
            transactions=txs,
            fallback_cadence_days=int(settings.get("payday_cadence_days", 14)),
            fallback_payday_date=settings.get("next_payday_date"),
            fallback_paycheck_cents=int(settings.get("expected_paycheck_cents", 220_000)),
            today=ref,
        )
        if payday.confidence >= 0.85:
            self.storage.update_settings(
                {
                    "payday_cadence_days": payday.cadence_days,
                    "next_payday_date": payday.next_payday_date,
                    "expected_paycheck_cents": payday.estimated_paycheck_cents,
                }
            )

        # 3. Recompute adaptive allocations & persist updated jar allocations
        budget = self._compute_budget(today=ref)
        jars_adjusted = 0
        for jar in self.storage.list_jars():
            jid = int(jar["id"])
            new_alloc = budget.adaptive_allocations_by_jar_id.get(jid)
            if new_alloc is not None and new_alloc != int(jar.get("auto_allocated_cents", 0)):
                self.storage.update_jar(jid, {"auto_allocated_cents": new_alloc})
                jars_adjusted += 1

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
        horizon = max(7, min(90, int(days)))
        settings = self.storage.get_settings()
        b = budget if budget is not None else self._compute_budget(today=ref)
        cr = credit_report if credit_report is not None else self.get_credit_report(today=ref)
        g = goals if goals is not None else self.list_goals(include_completed=False)
        since_90d = (ref - timedelta(days=90)).isoformat()
        txs = self.storage.list_transactions(limit=2000, since_date=since_90d)
        bills = self.storage.list_recurring_bills(active_only=True)
        return self.quant_engine.build_forecast_report(
            budget=b,
            transactions=txs,
            recurring_bills=bills,
            settings=settings,
            credit_report=cr,
            goals=g,
            days=horizon,
            today=ref,
        )

    # --- Dashboard ---

    def get_dashboard(self, today: Optional[date] = None) -> DashboardResponse:
        ref = today or date.today()
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
        recent_txs = [self._to_tx_out(t) for t in self.storage.list_transactions(limit=25)]
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
            },
            goals=goals,
            quant_forecast=quant_forecast,
            intelligence_mode=mode.value,
            active_ai_provider=active_provider,
        )

    # --- Transactions & Quick Add ---

    def _to_tx_out(self, row: Dict[str, Any]) -> TransactionOut:
        return TransactionOut(
            id=int(row["id"]),
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
        )

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
        adjust_balance: bool = True,
        run_auto_sync: bool = True,
    ) -> Optional[TransactionOut]:
        acct_id = account_id if account_id is not None else self.storage.get_default_account_id()
        norm = normalize_merchant(merchant)

        confidence = 1.0
        needs_review = False

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
                subcategory = "Income"
        elif tx_type == TransactionType.TRANSFER.value:
            jar_id = None
            if not subcategory:
                subcategory = "Internal Transfer"

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
            adjust_balance=adjust_balance,
        )
        if row is None:
            return None

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
            msg = f"Logged ${tx.amount_cents / 100:,.2f} at {tx.merchant} → {tx.jar_icon} {tx.jar_name}"
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

        # Auto-learn merchant rule so ADHD user NEVER has to categorize the same merchant twice!
        if req.learn_merchant_rule and req.jar_id is not None:
            self.storage.upsert_merchant_rule(
                updated["normalized_merchant"],
                req.jar_id,
                updated.get("subcategory", ""),
            )
            self.storage.log_budget_event(
                "learned_rule",
                f"Learned rule: '{updated['merchant']}' → {updated.get('jar_name', 'Jar')}",
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
        jar_id: Optional[int] = None,
        needs_review_only: bool = False,
        search: Optional[str] = None,
    ) -> List[TransactionOut]:
        rows = self.storage.list_transactions(
            limit=limit, jar_id=jar_id, needs_review_only=needs_review_only, search=search
        )
        return [self._to_tx_out(r) for r in rows]

    # --- Multi-Format Bank Statement Ingestion (CSV, TSV, OFX/QFX, QIF, JSON) ---

    def preview_statement(
        self,
        content: str,
        *,
        filename: str = "",
        account_id: Optional[int] = None,
        flip_expense_sign: bool = False,
        detect_transfers: bool = True,
    ) -> Dict[str, Any]:
        """Dry-run statement ingestion returning format, counts, duplicates, and sample rows."""
        res = ingest_statement(
            content,
            filename=filename,
            flip_expense_sign=flip_expense_sign,
            detect_transfers=detect_transfers,
        )
        acct_id = account_id if account_id is not None else self.storage.get_default_account_id()
        existing_fps = {
            r["fingerprint"]
            for r in self.storage._conn.execute("SELECT fingerprint FROM transactions WHERE fingerprint IS NOT NULL").fetchall()
        }
        dup_count = 0
        sample = []
        for r in res.rows:
            norm = normalize_merchant(r.merchant)
            fp = compute_tx_fingerprint(
                acct_id, r.tx_date, r.tx_type, r.amount_cents, norm, r.memo, external_id=r.external_id
            )
            is_dup = fp in existing_fps
            if is_dup:
                dup_count += 1
            if len(sample) < 8:
                sample.append(
                    {
                        "tx_date": r.tx_date,
                        "merchant": r.merchant,
                        "tx_type": r.tx_type,
                        "amount_cents": r.amount_cents,
                        "duplicate": is_dup,
                    }
                )
        return {
            "format_detected": res.format_detected,
            "total_parsed": len(res.rows),
            "new_count": max(0, len(res.rows) - dup_count),
            "duplicate_count": dup_count,
            "transfers_detected": res.transfers_detected,
            "detected_balance_cents": res.detected_balance_cents,
            "warnings": res.warnings[:20],
            "sample": sample,
        }

    def import_statement(
        self,
        content: str,
        *,
        filename: str = "",
        account_id: Optional[int] = None,
        flip_expense_sign: bool = False,
        detect_transfers: bool = True,
        sync_ofx_balance: bool = False,
    ) -> Dict[str, Any]:
        res = ingest_statement(
            content,
            filename=filename,
            flip_expense_sign=flip_expense_sign,
            detect_transfers=detect_transfers,
        )
        acct_id = account_id if account_id is not None else self.storage.get_default_account_id()
        imported = 0
        duplicates = 0
        for r in res.rows:
            tx = self.add_transaction_raw(
                account_id=acct_id,
                tx_type=r.tx_type,
                amount_cents=r.amount_cents,
                merchant=r.merchant,
                memo=r.memo,
                tx_date=r.tx_date,
                source=f"{res.format_detected}_import",
                deduplicate=True,
                external_id=r.external_id,
                adjust_balance=not (sync_ofx_balance and res.detected_balance_cents is not None),
                run_auto_sync=False,
            )
            if tx is None:
                duplicates += 1
            else:
                imported += 1

        if sync_ofx_balance and res.detected_balance_cents is not None:
            self.storage.set_account_balance(acct_id, res.detected_balance_cents)

        sync = self.run_self_updating_sync(
            reason=f"Imported {imported} {res.format_detected.upper()} statement transactions"
        )
        return {
            "format_detected": res.format_detected,
            "imported_count": imported,
            "duplicate_count": duplicates,
            "transfers_detected": res.transfers_detected,
            "detected_balance_cents": res.detected_balance_cents,
            "warnings": res.warnings[:20],
            "sync": sync,
        }

    def import_csv(self, csv_text: str, account_id: Optional[int] = None, flip_expense_sign: bool = False) -> Dict[str, Any]:
        return self.import_statement(
            csv_text, filename="upload.csv", account_id=account_id, flip_expense_sign=flip_expense_sign
        )

    def export_csv(self) -> str:
        txs = self.storage.list_transactions(limit=5000)
        return export_transactions_csv(txs)

    # --- Credit Score & Debt Optimization ---

    def get_credit_report(
        self,
        extra_monthly_budget_cents: int = 25_000,
        today: Optional[date] = None,
    ) -> CreditOptimizationReport:
        ref = today or date.today()
        budget = self._compute_budget(today=ref)
        return self.credit_optimizer.analyze(
            raw_lines=self.storage.list_credit_lines(active_only=True),
            smoothed_monthly_income_cents=budget.smoothed_monthly_income_cents,
            extra_monthly_budget_cents=extra_monthly_budget_cents,
            today=ref,
        )

    def create_credit_line(self, req: CreditLineCreate) -> Dict[str, Any]:
        saved = self.storage.create_credit_line(req.model_dump())
        self.run_self_updating_sync(reason=f"Added credit/debt line {saved['name']}")
        return saved

    def update_credit_line(self, line_id: int, req: CreditLineUpdate) -> Optional[Dict[str, Any]]:
        updates = req.model_dump(exclude_none=True)
        if "autopay_mode" in updates:
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

    # --- Goal Optimizer & Purchase Planner ---

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
            category=GoalCategory(row.get("category", GoalCategory.GADGET.value)),
            funding_jar_slug=row.get("funding_jar_slug", "fun"),
            status=row.get("status", "active"),
            notes=row.get("notes", ""),
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
        updated = self.storage.update_goal(goal_id, req.model_dump(exclude_none=True))
        return self._to_goal_out(updated) if updated else None

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

    def delete_goal(self, goal_id: int) -> bool:
        return self.storage.delete_goal(goal_id)

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
            f"Shifted ${req.amount_cents / 100:,.2f} from {src.name} → {dst.name}",
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
        # Reward the healthy habit of pausing immediately (+15 XP!)
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
            # Optionally sweep a share of avoided spend from Guilt-Free Fun -> Safety & Future
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

        # Intentional purchase after cooling off
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
        triage = [self._to_tx_out(r) for r in self.storage.list_transactions(limit=25, needs_review_only=True)]
        upcoming_14d = [b for b in budget.upcoming_bills if b.days_until_due <= 14]
        zombies = [b for b in budget.upcoming_bills if b.flagged_zombie or b.price_hike_cents > 0 or (b.is_subscription and not b.is_essential)]
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

    # --- Multi-Mode Intelligence & Multi-Provider AI Coach ---

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
            primary_client, fallback_client = self._build_coach_clients(provider_override=provider_override)
            coach = FinancialCoach(client=primary_client, fallback_client=fallback_client)

        result = coach.generate_suggestions(
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
        if result.provider_used in ("opencode_go", "gemini"):
            self.storage.record_ai_usage(
                result.provider_used,
                result.model_used or "",
                cached=result.cached,
            )
        return result

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
            primary_client, fallback_client = self._build_coach_clients(provider_override=provider_override)
            coach = FinancialCoach(client=primary_client, fallback_client=fallback_client)

        answer = coach.answer_question(
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
        if answer.provider_used in ("opencode_go", "gemini"):
            self.storage.record_ai_usage(answer.provider_used, answer.model_used or "", cached=False)
        return answer

    def _resolve_target_provider(self, provider: str = "auto") -> Literal["opencode_go", "gemini"]:
        cleaned = (provider or "auto").strip().lower()
        if cleaned == "gemini":
            return "gemini"
        if cleaned == "opencode_go":
            return "opencode_go"
        active = self.resolve_active_provider()
        if active == "gemini":
            return "gemini"
        if active == "opencode_go":
            return "opencode_go"
        pref = self._resolve_ai_provider_setting()
        return "gemini" if pref == AIProvider.GEMINI else "opencode_go"

    def list_ai_models(self, provider: str = "auto", force_refresh: bool = False) -> Dict[str, Any]:
        target = self._resolve_target_provider(provider)
        client: LLMClient = self.build_gemini_client() if target == "gemini" else self.build_opencode_client()
        res = dict(client.list_models(force_refresh=force_refresh))
        res.setdefault("provider", target)
        return res

    def test_ai_connection(self, provider: str = "auto") -> Dict[str, Any]:
        target = self._resolve_target_provider(provider)
        client: LLMClient = self.build_gemini_client() if target == "gemini" else self.build_opencode_client()
        res = dict(client.test_connection())
        res.setdefault("provider", target)
        return res

    def list_opencode_models(self, force_refresh: bool = False) -> Dict[str, Any]:
        return self.list_ai_models(provider="opencode_go", force_refresh=force_refresh)

    def test_opencode_connection(self) -> Dict[str, Any]:
        return self.test_ai_connection(provider="opencode_go")

    # --- Settings ---

    def get_ai_settings(self) -> AISettingsOut:
        raw = self.storage.get_settings()
        oc_key, oc_source = self._resolve_opencode_key_with_source()
        gem_key, gem_source = self._resolve_gemini_key_with_source()
        max_daily = int(raw.get("opencode_max_daily_requests", 60))
        used_today = self.storage.count_ai_requests_today()
        return AISettingsOut(
            intelligence_mode=self._resolve_intelligence_mode(raw),
            ai_provider=self._resolve_ai_provider_setting(raw),
            active_provider_resolved=self.resolve_active_provider(),
            opencode_base_url=raw.get("opencode_base_url", "https://opencode.ai/zen/go/v1"),
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
        return ProfileSettings(
            currency=raw.get("currency", "USD"),
            hourly_wage_cents=int(raw.get("hourly_wage_cents", 3000)),
            emergency_buffer_cents=int(raw.get("emergency_buffer_cents", 50_000)),
            default_cooling_hours=int(raw.get("default_cooling_hours", 48)),
            payday_cadence_days=int(raw.get("payday_cadence_days", 14)),
            next_payday_date=raw.get("next_payday_date"),
            expected_paycheck_cents=int(raw.get("expected_paycheck_cents", 220_000)),
            calm_mode=bool(raw.get("calm_mode", 1)),
            auto_rebalance_enabled=bool(raw.get("auto_rebalance_enabled", 1)),
            intelligence_mode=self._resolve_intelligence_mode(raw),
            ai_provider=self._resolve_ai_provider_setting(raw),
            active_provider_resolved=self.resolve_active_provider(),
            opencode_base_url=raw.get("opencode_base_url", "https://opencode.ai/zen/go/v1"),
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
            auth_enabled=bool(raw.get("passcode_hash", "")),
        )

    def update_settings(self, req: ProfileSettingsUpdate) -> ProfileSettings:
        data = req.model_dump(exclude_none=True)
        clear_oc_key = bool(data.pop("clear_opencode_key", False))
        oc_api_key = data.pop("opencode_api_key", None)
        if clear_oc_key:
            self.secrets.set("opencode_go_api_key", "")
        elif oc_api_key is not None and oc_api_key.strip():
            self.secrets.set("opencode_go_api_key", oc_api_key.strip())

        clear_gem_key = bool(data.pop("clear_gemini_key", False))
        gem_api_key = data.pop("gemini_api_key", None)
        if clear_gem_key:
            self.secrets.set("gemini_api_key", "")
        elif gem_api_key is not None and gem_api_key.strip():
            self.secrets.set("gemini_api_key", gem_api_key.strip())

        if "intelligence_mode" in data and isinstance(data["intelligence_mode"], IntelligenceMode):
            data["intelligence_mode"] = data["intelligence_mode"].value
        if "ai_provider" in data and isinstance(data["ai_provider"], AIProvider):
            data["ai_provider"] = data["ai_provider"].value

        if "opencode_base_url" in data:
            data["opencode_base_url"] = validate_external_api_url(data["opencode_base_url"])
        if "gemini_base_url" in data:
            data["gemini_base_url"] = validate_external_api_url(data["gemini_base_url"])

        for bool_k in ("calm_mode", "auto_rebalance_enabled"):
            if bool_k in data:
                data[bool_k] = int(data[bool_k])

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

    def load_demo_workspace(self) -> Dict[str, Any]:
        from chronox_finance.demo_data import seed_demo_data

        return seed_demo_data(self)
