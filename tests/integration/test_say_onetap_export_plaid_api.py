"""Comprehensive integration tests for Wave 2 platform wiring.

Covers:
1. Automagic Local AI CLI auto-discovery, priority resolution, live auth-failure failover, and /api/v1/ai/test
2. Unified natural-language AI Harness (POST /api/v1/ai/say) with causal reason preservation,
   savings deposits, inline splits, Q&A, export/one-tap routing, confirmation flow, and 1-step undo
3. AI + Statistical Quant One-Tap Optimizer (POST /api/v1/optimize/one-tap & atomic undo)
4. Apple Numbers Graphical Workbook (.xlsx) and Executive CSV / CSV-Pack exports
5. Bulletproof server-side Plaid Hosted Link poller, inline credential save, multi-item exchange,
   initial account seeding, /plaid/hosted-complete callback, /plaid/hosted-link/check,
   NOT_READY backfill retry thread, and zero hanging threads on close/lock
6. Apple-polished CLI (main.py & chronox_finance.cli) --say, bare utterances, --undo, --one-tap, --export
"""

from __future__ import annotations

import io
import json
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest
from fastapi.testclient import TestClient

import main as root_main
from chronox_finance.ai.local_cli_clients import LocalCLIRegistry, LocalCLIStatus
from chronox_finance.ai.opencode_client import CompletionResult, LLMProviderError
from chronox_finance.api import create_app
from chronox_finance.cli import run_cli
from chronox_finance.security import CSRF_HEADER_NAME
from chronox_finance.service import FinanceService
from tests.fixtures.seed_workspace import seed_workspace


def _clear_cloud_ai_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "OPENCODE_GO_API_KEY",
        "OPENCODE_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
        "CHRONOX_WRAPPER_API_KEY",
        "CHRONOX_WRAPPER_BASE_URL",
        "CHRONOX_INTELLIGENCE_MODE",
        "CHRONOX_AI_PROVIDER",
    ):
        monkeypatch.delenv(var, raising=False)


class _StubLocalCLIClient:
    """Deterministic local CLI LLMClient double wired with registry auth callbacks."""

    def __init__(
        self,
        provider_name: str,
        *,
        installed: bool = True,
        model: str = "stub-model",
        fail_auth: bool = False,
        fail_error: str = "401 Unauthorized: refresh token already used",
        response_text: str = '{"intent_kind":"question_advice","assistant_message":"You are in great shape.","actions":[]}',
        registry: Optional[LocalCLIRegistry] = None,
    ) -> None:
        self.provider_name = provider_name
        self.model = model
        self._installed = installed
        self._fail_auth = fail_auth
        self._fail_error = fail_error
        self._response_text = response_text
        self._registry = registry
        self.calls: int = 0
        self.test_calls: List[Dict[str, Any]] = []

    @property
    def is_configured(self) -> bool:
        return self._installed

    def chat_completion(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 900,
        json_mode: bool = False,
        use_cache: bool = True,
    ) -> CompletionResult:
        self.calls += 1
        if self._fail_auth:
            if self._registry is not None:
                self._registry.mark_auth_failure(self.provider_name, self._fail_error)
            raise LLMProviderError(self._fail_error)
        if self._registry is not None:
            self._registry.clear_auth_failure(self.provider_name, mark_ready=True)
        return CompletionResult(
            content=self._response_text,
            model=self.model,
            protocol_used="cli",
            provider=self.provider_name,
            cached=False,
            input_tokens=120,
            output_tokens=45,
            cached_tokens=0,
            latency_ms=42,
        )

    def test_connection(self, deep: bool = True, *, force_refresh: bool = False) -> Dict[str, Any]:
        self.test_calls.append({"deep": deep, "force_refresh": force_refresh})
        if not self._installed:
            return {"ok": False, "provider": self.provider_name, "model": self.model, "message": "Binary not installed"}
        if self._fail_auth:
            if self._registry is not None:
                self._registry.mark_auth_failure(self.provider_name, self._fail_error)
            return {
                "ok": False,
                "provider": self.provider_name,
                "model": self.model,
                "latency_ms": 35,
                "message": self._fail_error,
            }
        if self._registry is not None:
            self._registry.clear_auth_failure(self.provider_name, mark_ready=True)
        return {
            "ok": True,
            "provider": self.provider_name,
            "model": self.model,
            "latency_ms": 28,
            "message": f"{self.provider_name} authenticated and ready",
        }

    def list_models(self, force_refresh: bool = False) -> Dict[str, Any]:
        return {"provider": self.provider_name, "live": False, "models": [{"id": self.model}]}


class _FakeLocalCLIRegistry(LocalCLIRegistry):
    """In-memory deterministic `LocalCLIRegistry` for zero-subprocess testing."""

    def __init__(self, specs: Dict[str, Dict[str, Any]]) -> None:
        super().__init__()
        self._specs = specs
        self.clients: Dict[str, _StubLocalCLIClient] = {}
        for pid, cfg in specs.items():
            self.clients[pid] = _StubLocalCLIClient(
                pid,
                installed=bool(cfg.get("installed", False)),
                model=str(cfg.get("model", f"{pid}-default")),
                fail_auth=bool(cfg.get("fail_auth", False)),
                fail_error=str(cfg.get("fail_error", "401 Unauthorized: expired OAuth token")),
                response_text=str(
                    cfg.get(
                        "response_text",
                        '{"intent_kind":"question_advice","assistant_message":"Local CLI verified response.","actions":[]}',
                    )
                ),
                registry=self,
            )

    def build_client(
        self,
        provider_id: str,
        *,
        model: Optional[str] = None,
        enabled: bool = True,
        timeout_seconds: Optional[float] = None,
    ) -> Any:
        c = self.clients[provider_id]
        if not enabled:
            return _StubLocalCLIClient(provider_id, installed=False, model=c.model, registry=self)
        return c

    def list_detected_clis(
        self,
        run_auth_probe: bool = False,
        *,
        run_version_probe: bool = True,
        force_refresh: bool = False,
    ) -> List[LocalCLIStatus]:
        out: List[LocalCLIStatus] = []
        display_names = {
            "claude_cli": "Claude Code CLI",
            "opencode_cli": "OpenCode CLI",
            "codex_cli": "Codex CLI",
            "agy_cli": "Antigravity CLI",
        }
        bin_names = {
            "claude_cli": "claude",
            "opencode_cli": "opencode",
            "codex_cli": "codex",
            "agy_cli": "agy",
        }
        for prio, pid in enumerate(self.PRIORITY_ORDER, start=1):
            cfg = self._specs.get(pid, {})
            installed = bool(cfg.get("installed", False))
            if not installed:
                out.append(
                    LocalCLIStatus(
                        provider_id=pid,
                        display_name=display_names[pid],
                        binary_name=bin_names[pid],
                        installed=False,
                        auth_ready=False,
                        status_detail="Not installed",
                        priority=prio,
                    )
                )
                continue
            reason = self.get_auth_failure_reason(pid)
            in_cd = reason is not None
            if run_auth_probe and (force_refresh or not in_cd):
                res = self.clients[pid].test_connection(deep=True, force_refresh=force_refresh)
                auth_ready: Optional[bool] = bool(res["ok"])
                detail = str(res["message"])
                in_cd = not auth_ready
            elif in_cd:
                auth_ready = False
                detail = reason or "Auth failure cooldown"
            else:
                cached = self._get_cached_probe(pid)
                if cached is not None:
                    auth_ready, detail = cached
                else:
                    auth_ready = None
                    detail = f"Installed ({cfg.get('version', '1.0.0')})"
            out.append(
                LocalCLIStatus(
                    provider_id=pid,
                    display_name=display_names[pid],
                    binary_name=bin_names[pid],
                    binary_path=str(cfg.get("binary_path", f"/usr/local/bin/{bin_names[pid]}")),
                    installed=True,
                    auth_ready=auth_ready,
                    version=str(cfg.get("version", "1.0.0")),
                    status_detail=detail,
                    default_model=str(cfg.get("model", "")),
                    priority=prio,
                    in_cooldown=in_cd,
                )
            )
        return out

    def get_failover_clients(
        self,
        *,
        run_auth_probe: bool = False,
        include_cooldown: bool = False,
    ) -> List[Any]:
        res: List[Any] = []
        for pid in self.PRIORITY_ORDER:
            cfg = self._specs.get(pid, {})
            if not cfg.get("installed", False):
                continue
            if not include_cooldown and self.is_auth_failed(pid):
                continue
            res.append(self.clients[pid])
        return res


def _make_unlocked_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    local_cli_registry: Optional[LocalCLIRegistry] = None,
    plaid_transport: Optional[httpx.BaseTransport] = None,
    plaid_poll_interval_seconds: float = 0.04,
    plaid_retry_delays: Tuple[float, ...] = (0.05, 0.1, 0.2),
    enforce_vault_lock: bool = False,
) -> Tuple[TestClient, FinanceService, str]:
    _clear_cloud_ai_env(monkeypatch)
    db_file = str(tmp_path / "wave2.sqlite3")
    svc = FinanceService(
        db_path=db_file,
        secret_store_path=tmp_path / "secrets.enc",
        kdf_iterations=10_000,
        local_cli_registry=local_cli_registry,
        plaid_transport=plaid_transport,
        plaid_poll_interval_seconds=plaid_poll_interval_seconds,
        plaid_retry_delays=plaid_retry_delays,
    )
    app = create_app(db_path=db_file, service=svc, enforce_vault_lock=enforce_vault_lock)
    return TestClient(app), svc, db_file


def _plaid_bg_threads() -> List[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("chronox-plaid-") and t.is_alive()]


# =============================================================================
# 1. Automagic Local AI CLI Auto-Discovery & Failover
# =============================================================================


class TestAutomagicLocalAIAndFailover:
    def test_local_cli_auto_detect_default_and_priority_and_failover(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CHRONOX_LOCAL_CLI_AUTO_DETECT", "1")
        reg = _FakeLocalCLIRegistry(
            {
                "claude_cli": {"installed": False},
                "opencode_cli": {
                    "installed": True,
                    "version": "2.2.7",
                    "model": "opencode-go/glm-5.3-flash",
                    "fail_auth": True,
                    "fail_error": "401 Unauthorized: token expired",
                },
                "codex_cli": {
                    "installed": True,
                    "version": "0.167.2",
                    "model": "gpt-5.3-codex",
                    "fail_auth": True,
                    "fail_error": "401 Unauthorized: refresh token already used",
                },
                "agy_cli": {
                    "installed": True,
                    "version": "1.5.18",
                    "model": "gemini-3.8-flash-low",
                    "fail_auth": False,
                    "response_text": json.dumps(
                        {
                            "intent_kind": "question_advice",
                            "assistant_message": "Failover succeeded cleanly via Antigravity CLI.",
                            "actions": [],
                        }
                    ),
                },
            }
        )
        client, svc, _ = _make_unlocked_api(tmp_path, monkeypatch, local_cli_registry=reg)
        try:
            seed_workspace(svc)

            # Default setting out of the box is True
            settings = client.get("/api/v1/settings").json()
            assert settings["local_cli_auto_detect"] is True
            assert settings["ai_provider"] == "auto"
            assert settings["active_provider_resolved"] == "opencode_cli"

            # GET /api/v1/ai/local-clis contract check
            r_clis = client.get("/api/v1/ai/local-clis?probe=false")
            assert r_clis.status_code == 200
            body = r_clis.json()
            assert body["auto_detect_enabled"] is True
            assert body["active_provider"] == "opencode_cli"
            assert body["active_provider_label"] == "OpenCode CLI"
            assert [c["provider_id"] for c in body["clis"]] == [
                "claude_cli",
                "opencode_cli",
                "codex_cli",
                "agy_cli",
            ]
            by_id = {c["provider_id"]: c for c in body["clis"]}
            assert by_id["claude_cli"]["installed"] is False
            assert by_id["opencode_cli"]["installed"] is True
            assert by_id["opencode_cli"]["is_active"] is True
            assert by_id["opencode_cli"]["version"] == "2.2.7"
            for item in body["clis"]:
                for req_k in (
                    "provider_id",
                    "display_name",
                    "installed",
                    "auth_ready",
                    "binary_path",
                    "version",
                    "status_detail",
                    "is_active",
                ):
                    assert req_k in item

            # Live failover on /api/v1/ai/say:
            # opencode_cli hits 401 -> codex_cli hits 401 -> agy_cli succeeds seamlessly!
            r_say = client.post(
                "/api/v1/ai/say",
                json={"text": "How is my financial runway looking?", "auto_commit": True},
            )
            assert r_say.status_code == 200
            say_data = r_say.json()
            assert say_data["provider_used"] == "agy_cli"
            assert "Antigravity CLI" in say_data["assistant_message"]
            assert reg.is_auth_failed("opencode_cli") is True
            assert reg.is_auth_failed("codex_cli") is True
            assert reg.is_auth_failed("agy_cli") is False

            # Active provider automatically shifts to agy_cli now that broken CLIs entered cooldown!
            r_clis2 = client.get("/api/v1/ai/local-clis?probe=false")
            assert r_clis2.status_code == 200
            assert r_clis2.json()["active_provider"] == "agy_cli"

            # POST /api/v1/ai/test?provider=codex_cli & agy_cli
            r_test_bad = client.post("/api/v1/ai/test?provider=codex_cli")
            assert r_test_bad.status_code == 200
            t_bad = r_test_bad.json()
            assert t_bad["ok"] is False
            assert t_bad["provider"] == "codex_cli"
            assert "401" in t_bad["detail"]
            assert isinstance(t_bad["latency_ms"], int)
            assert reg.clients["codex_cli"].test_calls[-1] == {"deep": True, "force_refresh": True}

            r_test_ok = client.post("/api/v1/ai/test?provider=agy_cli")
            assert r_test_ok.status_code == 200
            t_ok = r_test_ok.json()
            assert t_ok["ok"] is True
            assert t_ok["provider"] == "agy_cli"
            assert t_ok["model"] == "gemini-3.8-flash-low"

            # Explicitly configured cloud key still takes priority over local CLIs in auto mode
            client.patch(
                "/api/v1/settings/ai",
                json={"gemini_api_key": "AIzaSyCloudWinsPriority1234", "ai_provider": "auto"},
            )
            assert client.get("/api/v1/settings/ai").json()["active_provider_resolved"] == "gemini"
        finally:
            svc.close()

    def test_unset_env_defaults_local_cli_auto_detect_to_true(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CHRONOX_LOCAL_CLI_AUTO_DETECT", raising=False)
        reg = _FakeLocalCLIRegistry(
            {"claude_cli": {"installed": True, "version": "1.2.3", "model": "claude-sonnet"}}
        )
        client, svc, _ = _make_unlocked_api(tmp_path, monkeypatch, local_cli_registry=reg)
        try:
            assert client.get("/api/v1/settings").json()["local_cli_auto_detect"] is True
            assert client.get("/api/v1/settings").json()["active_provider_resolved"] == "claude_cli"
            # Disabling in settings turns it off cleanly
            client.patch("/api/v1/settings/ai", json={"local_cli_auto_detect": False})
            assert client.get("/api/v1/settings/ai").json()["active_provider_resolved"] == "none"
        finally:
            svc.close()


# =============================================================================
# 2. Unified Natural-Language AI Harness (/api/v1/ai/say) & Undo
# =============================================================================


class TestUnifiedSayHarnessAPI:
    def test_say_expense_with_because_reason_savings_question_export_onetap_and_undo(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, svc, _ = _make_unlocked_api(tmp_path, monkeypatch)
        try:
            seed_workspace(svc)
            accts_before = {a["account_type"]: a["balance_cents"] for a in client.get("/api/v1/accounts").json()}
            chk_before = accts_before["checking"]

            # 1. Expense with causal "because ..." reason -> 1-step commit + memo preservation
            r_coffee = client.post(
                "/api/v1/ai/say",
                json={"text": "I spent $5.75 on coffee because early flight", "auto_commit": True},
            )
            assert r_coffee.status_code == 200
            coffee = r_coffee.json()
            assert coffee["intent_kind"] == "ledger_mutation"
            assert coffee["executed"] is True
            assert coffee["requires_confirmation"] is False
            assert coffee["undo_available"] is True
            assert isinstance(coffee["batch_id"], int)
            assert coffee["safe_to_spend_today_cents"] >= 0
            assert coffee["safe_to_spend_cents"] >= 0

            txs = client.get("/api/v1/transactions?search=coffee&limit=5").json()
            assert any(
                t["amount_cents"] == 575 and "early flight" in t["memo"].lower()
                for t in txs
            )
            accts_after_coffee = {a["account_type"]: a["balance_cents"] for a in client.get("/api/v1/accounts").json()}
            assert accts_after_coffee["checking"] == chk_before - 575

            # 2. 1-step Undo via /api/v1/ai/agent/undo-last restores exact balance & removes tx
            r_undo = client.post("/api/v1/ai/agent/undo-last")
            assert r_undo.status_code == 200
            assert r_undo.json()["status"] == "undone"
            assert r_undo.json()["batch_id"] == coffee["batch_id"]
            accts_restored = {a["account_type"]: a["balance_cents"] for a in client.get("/api/v1/accounts").json()}
            assert accts_restored["checking"] == chk_before

            # 3. Savings deposit
            r_save = client.post(
                "/api/v1/ai/say",
                json={"text": "put $100 in savings", "auto_commit": True},
            )
            assert r_save.status_code == 200
            save_data = r_save.json()
            assert save_data["batch_id"] is not None
            assert len(save_data["actions"]) >= 1
            if save_data["requires_confirmation"]:
                r_exec = client.post(f"/api/v1/ai/agent/execute/{save_data['batch_id']}")
                assert r_exec.status_code == 200
                assert r_exec.json()["status"] == "executed"
            else:
                assert save_data["executed"] is True

            # 4. Question / affordability check -> read-only answer
            r_q = client.post(
                "/api/v1/ai/say",
                json={"text": "Can I afford a $400 monitor?", "auto_commit": True},
            )
            assert r_q.status_code == 200
            q_data = r_q.json()
            assert q_data["intent_kind"] == "question_advice"
            assert q_data["executed"] is False
            assert len(q_data["assistant_message"]) > 10

            # 5. Export request routing via /api/v1/ai/say
            r_exp_num = client.post(
                "/api/v1/ai/say",
                json={"text": "export my Apple Numbers report", "auto_commit": True},
            )
            assert r_exp_num.status_code == 200
            exp_num = r_exp_num.json()
            assert exp_num["intent_kind"] == "export_request"
            assert exp_num["export_format"] == "numbers"
            assert exp_num["export_download_url"] == "/api/v1/export/numbers"

            r_exp_pack = client.post(
                "/api/v1/ai/say",
                json={"text": "export full csv pack zip", "auto_commit": True},
            )
            assert r_exp_pack.status_code == 200
            assert r_exp_pack.json()["export_format"] == "csv_pack"
            assert r_exp_pack.json()["export_download_url"] == "/api/v1/export/csv?pack=true"

            # 6. One-Tap trigger via /api/v1/ai/say
            r_ot = client.post(
                "/api/v1/ai/say",
                json={"text": "optimize my budget with one tap", "auto_commit": True},
            )
            assert r_ot.status_code == 200
            ot_say = r_ot.json()
            assert ot_say["intent_kind"] == "onetap_optimize"
            assert ot_say["onetap_plan"] is not None
            assert "runway" in ot_say["onetap_plan"]
            assert "executive_summary" in ot_say["onetap_plan"]

            # 7. Inline multi-category split capture + persistence + undo
            r_split = client.post(
                "/api/v1/ai/say",
                json={
                    "text": "Spent $60 at Target: $40 groceries and $20 household supplies because hosting",
                    "auto_commit": True,
                },
            )
            assert r_split.status_code == 200
            sp_data = r_split.json()
            assert sp_data["batch_id"] is not None
            if sp_data["requires_confirmation"]:
                assert client.post(f"/api/v1/ai/agent/execute/{sp_data['batch_id']}").status_code == 200
            target_txs = client.get("/api/v1/transactions?search=Target&limit=5").json()
            matched = [t for t in target_txs if t["amount_cents"] == 6000]
            assert len(matched) >= 1
            if matched[0]["splits"]:
                assert sum(s["amount_cents"] for s in matched[0]["splits"]) == 6000
        finally:
            svc.close()


# =============================================================================
# 3. AI + Statistical Quant One-Tap Optimizer & Atomic Undo
# =============================================================================


class TestOneTapOptimizeAndUndoAPI:
    def test_onetap_preview_apply_and_exact_atomic_undo(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, svc, _ = _make_unlocked_api(tmp_path, monkeypatch)
        try:
            seed_workspace(svc)
            # Capture pre-One-Tap ground truth
            settings_before = client.get("/api/v1/settings").json()
            goals_before = {g["id"]: g["saved_cents"] for g in client.get("/api/v1/goals").json()}
            jars_before = {j["slug"]: j["available_cents"] for j in client.get("/api/v1/dashboard").json()["jars"]}
            bills_before = jars_before["bills"]

            # 1. Preview (apply=False)
            r_prev = client.post("/api/v1/optimize/one-tap", json={"horizon_days": 30, "apply": False})
            assert r_prev.status_code == 200
            prev = r_prev.json()
            assert prev["applied"] is False
            assert prev["undo_available"] is False
            assert prev["needs_setup"] is False
            assert prev["executive_summary"]
            assert "cvar_95_buffer_cents" in prev["runway"]
            assert "ruin_probability_pct" in prev["runway"]
            assert "diagnostics" in prev
            assert isinstance(prev["actions"], list) and len(prev["actions"]) >= 1
            assert isinstance(prev["total_expected_annual_savings_cents"], int)

            # 2. Apply atomically (apply=True)
            r_apply = client.post("/api/v1/optimize/one-tap", json={"horizon_days": 30, "apply": True})
            assert r_apply.status_code == 200
            applied = r_apply.json()
            assert applied["applied"] is True
            assert applied["undo_available"] is True
            batch_id = applied["batch_id"]
            assert isinstance(batch_id, int)

            # Bills jar firewall: Bills available must never be drained
            jars_after = {j["slug"]: j["available_cents"] for j in client.get("/api/v1/dashboard").json()["jars"]}
            assert jars_after["bills"] >= bills_before

            # 3. Undo atomically -> exact restoration of settings, goals, and jars!
            r_undo = client.post(f"/api/v1/optimize/one-tap/undo/{batch_id}")
            assert r_undo.status_code == 200
            assert r_undo.json()["undone"] is True
            assert r_undo.json()["batch_id"] == batch_id

            settings_restored = client.get("/api/v1/settings").json()
            goals_restored = {g["id"]: g["saved_cents"] for g in client.get("/api/v1/goals").json()}
            jars_restored = {j["slug"]: j["available_cents"] for j in client.get("/api/v1/dashboard").json()["jars"]}
            assert settings_restored["emergency_buffer_cents"] == settings_before["emergency_buffer_cents"]
            assert goals_restored == goals_before
            assert jars_restored == jars_before
        finally:
            svc.close()

    def test_onetap_fresh_zero_profile_returns_needs_setup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, svc, _ = _make_unlocked_api(tmp_path, monkeypatch)
        try:
            r = client.post("/api/v1/optimize/one-tap", json={"horizon_days": 30, "apply": True})
            assert r.status_code == 200
            data = r.json()
            assert data["needs_setup"] is True
            assert data["applied"] is False
            assert data["batch_id"] is None
        finally:
            svc.close()


# =============================================================================
# 4. Apple Numbers Graphical Workbook & Executive CSV Exports
# =============================================================================


class TestAppleNumbersAndCSVExportsAPI:
    def test_export_numbers_xlsx_csv_and_csv_pack_endpoints(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, svc, _ = _make_unlocked_api(tmp_path, monkeypatch)
        try:
            seed_workspace(svc)

            # 1. Apple Numbers Graphical Workbook (.xlsx)
            r_num = client.get("/api/v1/export/numbers?days=365&native=false")
            assert r_num.status_code == 200
            assert "spreadsheetml.sheet" in r_num.headers["content-type"]
            assert "attachment;" in r_num.headers["content-disposition"]
            assert ".xlsx" in r_num.headers["content-disposition"]
            with zipfile.ZipFile(io.BytesIO(r_num.content), "r") as zf:
                names = set(zf.namelist())
                assert "xl/workbook.xml" in names
                assert "xl/worksheets/sheet1.xml" in names
                assert "xl/worksheets/sheet6.xml" in names
                chart_files = [n for n in names if n.startswith("xl/charts/chart") and n.endswith(".xml")]
                assert len(chart_files) == 5

            # 2. Transactions CSV (BOM + full rows) and alias /export/transactions.csv
            r_csv = client.get("/api/v1/export/csv?pack=false")
            assert r_csv.status_code == 200
            assert r_csv.content.startswith(b"\xef\xbb\xbf")
            assert "Starbucks" in r_csv.text

            r_csv_alias = client.get("/api/v1/export/transactions.csv")
            assert r_csv_alias.status_code == 200
            assert r_csv_alias.content == r_csv.content

            # 3. 7-table Executive CSV Pack (.zip) via both ?pack=true and /csv-pack
            for url in ("/api/v1/export/csv?pack=true", "/api/v1/export/csv-pack"):
                r_zip = client.get(url)
                assert r_zip.status_code == 200
                assert "zip" in r_zip.headers["content-type"]
                with zipfile.ZipFile(io.BytesIO(r_zip.content), "r") as zf:
                    csv_files = [n for n in zf.namelist() if n.endswith(".csv")]
                    assert len(csv_files) == 7
        finally:
            svc.close()


# =============================================================================
# 5. Bulletproof Server-Side Plaid Poller, Multi-Item, Callback & NOT_READY Retry
# =============================================================================


class TestPlaidHostedLinkPollerCallbackAndRetry:
    def test_plaid_inline_save_poller_multi_item_and_not_ready_backfill_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state: Dict[str, Any] = {
            "create_payloads": [],
            "poll_calls": 0,
            "exchanged": [],
            "sync_calls_by_token": {},
            "balance_fail_once": True,
        }

        def plaid_handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            body = json.loads(request.content.decode("utf-8"))

            if path == "/link/token/create":
                state["create_payloads"].append(body)
                return httpx.Response(
                    200,
                    json={
                        "link_token": "link-prod-multi-123",
                        "hosted_link_url": "https://hosted.plaid.com/link/multi-123",
                        "expiration": "2099-01-01T00:00:00Z",
                    },
                )

            if path == "/link/token/get":
                state["poll_calls"] += 1
                if state["poll_calls"] < 2:
                    return httpx.Response(
                        200,
                        json={"link_token": "link-prod-multi-123", "link_sessions": [{"finished_at": None}]},
                    )
                return httpx.Response(
                    200,
                    json={
                        "link_token": "link-prod-multi-123",
                        "link_sessions": [
                            {
                                "finished_at": "2026-11-26T10:00:00Z",
                                "results": {
                                    "item_add_results": [
                                        {
                                            "public_token": "public-prod-item-1",
                                            "institution": {"institution_id": "ins_1", "name": "Chase Bank"},
                                            "accounts": [
                                                {
                                                    "id": "acct_chase_1",
                                                    "name": "Chase Total Checking",
                                                    "mask": "4411",
                                                    "type": "depository",
                                                    "subtype": "checking",
                                                }
                                            ],
                                        },
                                        {
                                            "public_token": "public-prod-item-2",
                                            "institution": {"institution_id": "ins_2", "name": "Ally Bank"},
                                            "accounts": [
                                                {
                                                    "id": "acct_ally_1",
                                                    "name": "Ally High-Yield Savings",
                                                    "mask": "8899",
                                                    "type": "depository",
                                                    "subtype": "savings",
                                                }
                                            ],
                                        },
                                    ]
                                },
                            }
                        ],
                    },
                )

            if path == "/item/public_token/exchange":
                ptok = body["public_token"]
                state["exchanged"].append(ptok)
                idx = "1" if ptok.endswith("1") else "2"
                return httpx.Response(
                    200,
                    json={"access_token": f"access-prod-{idx}", "item_id": f"item-prod-{idx}"},
                )

            if path == "/transactions/sync":
                tok = body["access_token"]
                n = state["sync_calls_by_token"].get(tok, 0) + 1
                state["sync_calls_by_token"][tok] = n
                # First call for item 1 simulates Plaid NOT_READY async window!
                if tok == "access-prod-1" and n == 1:
                    return httpx.Response(
                        200,
                        json={
                            "added": [],
                            "modified": [],
                            "removed": [],
                            "next_cursor": "",
                            "has_more": False,
                            "transactions_update_status": "NOT_READY",
                        },
                    )
                acct_id = "acct_chase_1" if tok == "access-prod-1" else "acct_ally_1"
                return httpx.Response(
                    200,
                    json={
                        "added": [
                            {
                                "transaction_id": f"tx_{tok}_{n}",
                                "account_id": acct_id,
                                "amount": 24.50,
                                "date": "2026-11-20",
                                "merchant_name": f"Merchant {tok}",
                                "name": f"Merchant {tok}",
                                "pending": False,
                            }
                        ],
                        "modified": [],
                        "removed": [],
                        "next_cursor": f"cur_{tok}_{n}",
                        "has_more": False,
                        "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE",
                    },
                )

            if path == "/accounts/balance/get":
                tok = body["access_token"]
                if tok == "access-prod-1" and state["balance_fail_once"]:
                    state["balance_fail_once"] = False
                    return httpx.Response(
                        400,
                        json={"error_code": "PRODUCT_NOT_READY", "error_message": "Balances still hydrating"},
                    )
                acct_id = "acct_chase_1" if tok == "access-prod-1" else "acct_ally_1"
                return httpx.Response(
                    200,
                    json={
                        "accounts": [
                            {
                                "account_id": acct_id,
                                "name": "Chase Total Checking" if tok == "access-prod-1" else "Ally High-Yield Savings",
                                "mask": "4411" if tok == "access-prod-1" else "8899",
                                "type": "depository",
                                "subtype": "checking" if tok == "access-prod-1" else "savings",
                                "balances": {"current": 3200.00, "available": 3200.00, "iso_currency_code": "USD"},
                            }
                        ]
                    },
                )

            if path in ("/liabilities/get", "/investments/holdings/get"):
                return httpx.Response(400, json={"error_code": "PRODUCTS_NOT_SUPPORTED", "error_message": "Not enabled"})

            return httpx.Response(404, json={"error_message": f"Unhandled {path}"})

        client, svc, _ = _make_unlocked_api(
            tmp_path,
            monkeypatch,
            plaid_transport=httpx.MockTransport(plaid_handler),
            plaid_poll_interval_seconds=0.04,
            plaid_retry_delays=(0.06, 0.12),
            enforce_vault_lock=True,
        )
        try:
            r_setup = client.post("/api/v1/auth/setup", json={"password": "plaid-test-vault-2026"})
            assert r_setup.status_code == 200
            csrf = {CSRF_HEADER_NAME: r_setup.json()["csrf_token"]}

            # 1. GET /api/v1/plaid/config before setup
            cfg0 = client.get("/api/v1/plaid/config").json()
            assert cfg0["configured"] is False
            assert cfg0["env"] == "production"
            assert cfg0["pending_link_active"] is False

            # 2. POST /api/v1/plaid/hosted-link auto-saves inline credentials & launches background poller!
            r_hl = client.post(
                "/api/v1/plaid/hosted-link",
                json={"client_id": "cid_inline_9999", "secret": "sec_inline_8888"},
                headers=csrf,
            )
            assert r_hl.status_code == 200
            hl = r_hl.json()
            assert hl["link_token"] == "link-prod-multi-123"
            assert hl["hosted_link_url"] == "https://hosted.plaid.com/link/multi-123"
            assert state["create_payloads"][0]["hosted_link"]["url_lifetime_seconds"] == 3600

            # Wait up to 2.5s for background poller (exchanges BOTH items) + NOT_READY follow-up retry!
            deadline = time.monotonic() + 2.5
            while time.monotonic() < deadline:
                conns = client.get("/api/v1/connections").json()
                if len(conns) == 2 and all(c["backfill_status"] == "complete" for c in conns):
                    break
                time.sleep(0.05)

            conns = client.get("/api/v1/connections").json()
            assert len(conns) == 2, f"Expected 2 auto-exchanged Plaid items, got: {conns}"
            assert {c["institution"] for c in conns} == {"Chase Bank", "Ally Bank"}
            assert all(c["backfill_status"] == "complete" for c in conns)
            assert state["sync_calls_by_token"]["access-prod-1"] >= 2  # NOT_READY retried automatically!

            # Accounts & transactions from BOTH institutions were imported
            accts = client.get("/api/v1/accounts").json()
            assert any("Chase Total Checking" in a["name"] for a in accts)
            assert any("Ally High-Yield Savings" in a["name"] for a in accts)
            txs = client.get("/api/v1/transactions?limit=50").json()
            assert any("Merchant access-prod-1" in t["merchant"] for t in txs)
            assert any("Merchant access-prod-2" in t["merchant"] for t in txs)

            # Even if browser redirect hits /hosted-complete AFTER poller completed, it stays 200 & friendly!
            r_late_cb = client.get(f"/api/v1/plaid/hosted-complete?state={hl['state_nonce']}")
            assert r_late_cb.status_code == 200
            assert "Bank Connected — Return to ChronoX" in r_late_cb.text

            # Config never leaks plaintext credentials and reports 2 connected
            cfg1 = client.get("/api/v1/plaid/config").json()
            assert cfg1["configured"] is True
            assert cfg1["connected_count"] == 2
            assert "sec_inline_8888" not in json.dumps(cfg1)
        finally:
            svc.close()

        # Verify ZERO hanging Plaid daemon threads after svc.close()!
        assert _plaid_bg_threads() == []

    def test_plaid_hosted_complete_callback_and_manual_check_and_vault_lock_stops_threads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def plaid_handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/link/token/create":
                return httpx.Response(
                    200,
                    json={
                        "link_token": "link-cb-777",
                        "hosted_link_url": "https://hosted.plaid.com/link/cb-777",
                        "expiration": "2099-01-01T00:00:00Z",
                    },
                )
            if path == "/link/token/get":
                return httpx.Response(
                    200,
                    json={
                        "link_token": "link-cb-777",
                        "link_sessions": [
                            {
                                "finished_at": "2026-11-26T10:00:00Z",
                                "on_success": {
                                    "public_token": "public-cb-1",
                                    "metadata": {
                                        "institution": {"institution_id": "ins_cb", "name": "Navy Federal"},
                                        "accounts": [
                                            {
                                                "id": "acct_nfcu_1",
                                                "name": "NFCU Everyday Checking",
                                                "mask": "1122",
                                                "type": "depository",
                                                "subtype": "checking",
                                            }
                                        ],
                                    },
                                },
                            }
                        ],
                    },
                )
            if path == "/item/public_token/exchange":
                return httpx.Response(200, json={"access_token": "access-cb-1", "item_id": "item-cb-1"})
            if path == "/transactions/sync":
                return httpx.Response(
                    200,
                    json={
                        "added": [],
                        "modified": [],
                        "removed": [],
                        "next_cursor": "cur_cb_1",
                        "has_more": False,
                        "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE",
                    },
                )
            if path == "/accounts/balance/get":
                # Simulate transient balance failure -> link metadata still seeds NFCU account!
                return httpx.Response(400, json={"error_code": "PRODUCT_NOT_READY", "error_message": "Not ready"})
            return httpx.Response(400, json={"error_code": "PRODUCTS_NOT_SUPPORTED", "error_message": "No"})

        client, svc, _ = _make_unlocked_api(
            tmp_path,
            monkeypatch,
            plaid_transport=httpx.MockTransport(plaid_handler),
            plaid_poll_interval_seconds=60.0,  # Keep poller idle so callback triggers exchange deterministically
            enforce_vault_lock=True,
        )
        try:
            r_setup = client.post("/api/v1/auth/setup", json={"password": "cb-vault-password-2026"})
            csrf = {CSRF_HEADER_NAME: r_setup.json()["csrf_token"]}

            r_hl = client.post(
                "/api/v1/plaid/hosted-link",
                json={"client_id": "cid_cb_1234", "secret": "sec_cb_5678", "env": "sandbox"},
                headers=csrf,
            )
            assert r_hl.status_code == 200
            nonce = r_hl.json()["state_nonce"]
            assert nonce

            # Bad state nonce rejected cleanly
            r_bad = client.get("/api/v1/plaid/hosted-complete?state=wrong-nonce")
            assert r_bad.status_code == 502

            # Valid callback triggers immediate exchange + sync and returns clean HTML
            r_ok = client.get(f"/api/v1/plaid/hosted-complete?state={nonce}")
            assert r_ok.status_code == 200
            assert "Bank Connected — Return to ChronoX" in r_ok.text

            # Account seeded from link metadata even though balance endpoint returned 400!
            accts = client.get("/api/v1/accounts").json()
            assert any("NFCU Everyday Checking" in a["name"] for a in accts)

            # Subsequent /hosted-link/check with the completed link_token is idempotent (no duplicate exchange!)
            r_chk_lt = client.post(
                "/api/v1/plaid/hosted-link/check",
                json={"link_token": "link-cb-777"},
                headers=csrf,
            )
            assert r_chk_lt.status_code == 200
            assert r_chk_lt.json()["status"] == "completed"

            # Subsequent /hosted-link/check without link_token returns clean idle status
            r_chk = client.post("/api/v1/plaid/hosted-link/check", headers=csrf)
            assert r_chk.status_code == 200
            assert r_chk.json()["status"] == "idle"

            # Vault lock must stop all background Plaid tasks cleanly
            svc.lock_vault()
            assert _plaid_bg_threads() == []
        finally:
            svc.close()


# =============================================================================
# 6. Simplified Apple-Polished CLI (main.py & chronox_finance.cli)
# =============================================================================


class TestSimplifiedApplePolishedCLI:
    def test_cli_say_bare_utterance_onetap_export_and_undo(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _clear_cloud_ai_env(monkeypatch)
        db_file = str(tmp_path / "cli_wave2.sqlite3")
        svc = FinanceService(db_path=db_file, secret_store_path=tmp_path / "secrets.enc")
        try:
            seed_workspace(svc)
        finally:
            svc.close()

        # 1. chronox --say "..." with causal reason + --undo
        assert root_main.main(["--db", db_file, "--say", "I spent $5.75 on coffee because early flight"]) == 0
        out_say = capsys.readouterr().out
        assert "Undo anytime: chronox --undo" in out_say

        svc_chk = FinanceService(db_path=db_file, secret_store_path=tmp_path / "secrets.enc")
        try:
            txs = svc_chk.storage.list_transactions(limit=20)
            assert any(t["amount_cents"] == 575 and "early flight" in t["memo"].lower() for t in txs)
        finally:
            svc_chk.close()

        assert root_main.main(["--db", db_file, "--undo"]) == 0
        out_undo = capsys.readouterr().out
        assert "Undid" in out_undo

        # 2. Bare natural-language string on BOTH main.py and chronox-finance
        assert root_main.main(["--db", db_file, "I spent $14.25 on lunch because client call"]) == 0
        assert "Undo anytime" in capsys.readouterr().out
        assert run_cli(["--db", db_file, "I spent $7.50 on tea because late sprint"]) == 0
        assert "Undo anytime" in capsys.readouterr().out

        # 3. --one-tap --apply + --undo
        assert root_main.main(["--db", db_file, "--one-tap", "--apply", "--days", "30"]) == 0
        out_ot = capsys.readouterr().out
        assert "CVaR 95%" in out_ot
        assert "Survival Probability" in out_ot
        assert "Undo anytime: chronox --undo" in out_ot

        assert root_main.main(["--db", db_file, "--undo"]) == 0
        out_ot_undo = capsys.readouterr().out
        assert "Undid One-Tap" in out_ot_undo

        # 4. --export numbers, csv, csv-pack + --say export trigger
        xlsx_out = tmp_path / "out" / "report.xlsx"
        csv_out = tmp_path / "out" / "tx.csv"
        zip_out = tmp_path / "out" / "pack.zip"
        say_xlsx = tmp_path / "out" / "say-report.xlsx"

        assert root_main.main(["--db", db_file, "--export", "numbers", "--out", str(xlsx_out)]) == 0
        assert xlsx_out.is_file() and xlsx_out.stat().st_size > 2000
        with zipfile.ZipFile(xlsx_out, "r") as zf:
            assert "xl/workbook.xml" in zf.namelist()

        assert root_main.main(["--db", db_file, "--export", "csv", "--out", str(csv_out)]) == 0
        assert csv_out.is_file() and "Starbucks" in csv_out.read_text(encoding="utf-8-sig")

        assert root_main.main(["--db", db_file, "--export", "csv-pack", "--out", str(zip_out)]) == 0
        assert zip_out.is_file()
        with zipfile.ZipFile(zip_out, "r") as zf:
            assert len([n for n in zf.namelist() if n.endswith(".csv")]) == 7

        assert root_main.main(
            ["--db", db_file, "--say", "export my Apple Numbers report", "--out", str(say_xlsx)]
        ) == 0
        assert say_xlsx.is_file() and say_xlsx.stat().st_size > 2000

        # 5. Backward compatibility of legacy flags & clean --help
        ns_fin = root_main.parse_arguments(["--action", "finance", "--host", "0.0.0.0", "--port", "8742"])
        assert ns_fin.action == "finance" and ns_fin.host == "0.0.0.0" and ns_fin.port == 8742
        with pytest.raises(SystemExit) as exc:
            root_main.parse_arguments(["--help"])
        assert exc.value.code == 0
        help_txt = capsys.readouterr().out
        assert "--say" in help_txt
        assert "--one-tap" in help_txt
        assert "--export" in help_txt
        assert "Credit Factor Analysis" in help_txt
        assert "simulator" not in help_txt.lower()
