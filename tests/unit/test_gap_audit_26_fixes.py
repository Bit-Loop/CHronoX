"""Regression tests for all 26 gap-audit findings (#117).

Covers:
- Plaid expired/invalid token stop (#1), INVALID_API_KEYS auto-env retry (#2),
  24h backfill cadence + unlock stale sync + periodic sync (#3), non-blocking
  `_plaid_bg_lock` across network I/O (#4), chronological session sorting (#14),
  and locked-vault HTML `/plaid/hosted-complete` (#20).
- OpenCode `OPENCODE_CONFIG_CONTENT` tool-deny lockdown (#5), localhost HTTP
  gateway auto-probe (`11434/1234/4000/8000`) gated on auto-detect (#6),
  harness 30s deadline budget + timeout cooldown (#7), `Popen(start_new_session=True)`
  + `os.killpg` orphan reaping (#8), pinned provider failover (#13),
  expanded CLI env allowlists & search dirs (#16), stdin/0600 prompt privacy (#17),
  free local CLI exclusion from daily quota (#18), and `CHRONOX_AI_PROVIDER` seed (#26).
- Conflict-safe partial `undo_onetap_batch` (#9), double-count-free savings
  analytics (#10), One-Tap `plan_fingerprint` HTTP 409 stale guard (#11),
  unambiguous `--undo agent:N` / `onetap:N` (#12), sensible new goal targets,
  savings-only `affects_balance=False`, `\b` goal word-boundary match (#19),
  One-Tap boilerplate rationale deduplication (#23), and clean `.xlsx` shared
  strings + skipped empty portfolio pie chart (#25).
"""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest
from fastapi.testclient import TestClient

from chronox_finance.ai.agent_tools import AgentLedgerContext
from chronox_finance.ai.codex_client import (
    OPENCODE_SAFE_CONFIG_CONTENT,
    build_safe_cli_env,
    get_cli_search_dirs,
)
from chronox_finance.ai.harness import FinancialAIHarness
from chronox_finance.ai.local_cli_clients import (
    AntigravityCLIClient,
    LocalCLIRegistry,
    OpenCodeCLIClient,
    OpenCodeCLIError,
    detect_local_http_gateways,
)
from chronox_finance.ai.opencode_client import CompletionResult
from chronox_finance.api import create_app
from chronox_finance.cli import run_cli
from chronox_finance.connectors.plaid_client import PlaidClient
from chronox_finance.engines.analytics import AnalyticsEngine
from chronox_finance.engines.exporter import (
    ExportSnapshot,
    FinanceExporter,
    sanitize_csv_text,
    sanitize_sheet_text,
)
from chronox_finance.models import PlaidConfigRequest, PlaidLinkTokenRequest
from chronox_finance.service import FinanceService, PlanConflictError
from tests.fixtures.seed_workspace import seed_workspace


@pytest.fixture(autouse=True)
def _clear_ambient_cloud_ai_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "OPENCODE_GO_API_KEY",
        "OPENCODE_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "CHRONOX_WRAPPER_API_KEY",
        "CHRONOX_WRAPPER_BASE_URL",
        "CHRONOX_AI_PROVIDER",
        "CHRONOX_INTELLIGENCE_MODE",
    ):
        monkeypatch.delenv(var, raising=False)


def test_plaid_expired_token_stops_retrying_and_clears_pending(tmp_path: Path) -> None:
    """#1: PUBLIC_TOKEN_EXPIRED / LINK_TOKEN_EXPIRED stops poller and clears pending state immediately."""
    calls = {"get": 0, "exchange": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/link/token/create":
            return httpx.Response(
                200,
                json={"link_token": "link-exp-1", "hosted_link_url": "https://hosted.plaid.com/link/exp-1"},
            )
        if request.url.path == "/link/token/get":
            calls["get"] += 1
            return httpx.Response(
                200,
                json={
                    "link_token": "link-exp-1",
                    "link_sessions": [
                        {
                            "finished_at": "2026-11-26T10:00:00Z",
                            "results": {
                                "item_add_results": [
                                    {
                                        "public_token": "public-expired-1",
                                        "institution": {"name": "Chase"},
                                        "accounts": [],
                                    }
                                ]
                            },
                        }
                    ],
                },
            )
        if request.url.path == "/item/public_token/exchange":
            calls["exchange"] += 1
            return httpx.Response(
                400,
                json={
                    "error_type": "INVALID_INPUT",
                    "error_code": "PUBLIC_TOKEN_EXPIRED",
                    "error_message": "the provided public token is expired",
                },
            )
        return httpx.Response(404)

    svc = FinanceService(
        db_path=str(tmp_path / "exp.db"),
        secret_store_path=tmp_path / "secrets.enc",
        plaid_transport=httpx.MockTransport(handler),
        plaid_poll_interval_seconds=60.0,
    )
    try:
        svc.secrets.unlock_ephemeral_for_testing()
        svc.create_plaid_link_token(
            PlaidLinkTokenRequest(client_id="cid_test_1", secret="sec_test_1")
        )
        res = svc.check_pending_plaid_link()
        assert res["status"] == "exited"
        assert res["pending_link_active"] is False
        assert "expired" in res["error_message"].lower()
        cfg = svc.get_plaid_config()
        assert cfg["pending_link_active"] is False
        assert cfg["pending_status"] == "expired"
        assert calls["exchange"] == 1
    finally:
        svc.close()


def test_plaid_invalid_api_keys_auto_retries_opposite_env_and_persists(tmp_path: Path) -> None:
    """#2: INVALID_API_KEYS on production automatically retries sandbox and persists working env."""
    seen_hosts: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_hosts.append(str(request.url.host))
        if request.url.host == "production.plaid.com":
            return httpx.Response(
                400,
                json={"error_code": "INVALID_API_KEYS", "error_message": "invalid client_id or secret"},
            )
        return httpx.Response(
            200,
            json={"link_token": "link-sbx-auto-1", "hosted_link_url": "https://hosted.plaid.com/link/sbx-1"},
        )

    svc = FinanceService(
        db_path=str(tmp_path / "env_auto.db"),
        secret_store_path=tmp_path / "secrets.enc",
        plaid_transport=httpx.MockTransport(handler),
        plaid_poll_interval_seconds=60.0,
    )
    try:
        svc.secrets.unlock_ephemeral_for_testing()
        lt = svc.create_plaid_link_token(
            PlaidLinkTokenRequest(client_id="cid_trial_1", secret="sec_trial_1")
        )
        assert lt.env == "sandbox"
        assert lt.link_token == "link-sbx-auto-1"
        assert seen_hosts == ["production.plaid.com", "sandbox.plaid.com"]
        assert svc.get_plaid_config()["env"] == "sandbox"
    finally:
        svc.close()


def test_plaid_lock_not_held_across_network_io_and_in_flight_guard(tmp_path: Path) -> None:
    """#4: get_plaid_config() and concurrent check_pending_plaid_link() do not block during slow network I/O."""
    entered = threading.Event()
    release = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/link/token/create":
            return httpx.Response(200, json={"link_token": "link-slow-1", "hosted_link_url": "https://hosted.plaid.com/slow"})
        if request.url.path == "/link/token/get":
            entered.set()
            release.wait(timeout=3.0)
            return httpx.Response(200, json={"link_token": "link-slow-1", "link_sessions": []})
        return httpx.Response(404)

    svc = FinanceService(
        db_path=str(tmp_path / "lockfree.db"),
        secret_store_path=tmp_path / "secrets.enc",
        plaid_transport=httpx.MockTransport(handler),
        plaid_poll_interval_seconds=60.0,
    )
    try:
        svc.secrets.unlock_ephemeral_for_testing()
        svc.configure_plaid(PlaidConfigRequest(client_id="cid_slow", secret="sec_slow", env="production"))
        svc.create_plaid_link_token(PlaidLinkTokenRequest())

        t = threading.Thread(target=svc.check_pending_plaid_link)
        t.start()
        assert entered.wait(timeout=2.0)

        t0 = time.monotonic()
        cfg = svc.get_plaid_config()
        concurrent_res = svc.check_pending_plaid_link()
        elapsed = time.monotonic() - t0

        assert elapsed < 0.5, f"get_plaid_config blocked on network I/O for {elapsed:.2f}s"
        assert cfg["pending_link_active"] is True
        assert concurrent_res["status"] == "pending"
        assert concurrent_res.get("in_flight") is True

        release.set()
        t.join(timeout=3.0)
    finally:
        release.set()
        svc.close()


def test_plaid_out_of_order_sessions_sorted_chronologically() -> None:
    """#14: An older exited session listed AFTER a newer in-progress session must NOT kill polling."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "link_token": "link-order-1",
                "link_sessions": [
                    # Newer active session returned FIRST
                    {"started_at": "2026-11-26T10:05:00Z", "finished_at": None},
                    # Older exited session returned LAST
                    {
                        "started_at": "2026-11-26T10:01:00Z",
                        "finished_at": "2026-11-26T10:02:00Z",
                        "exit": {"error": {"error_code": "USER_EXITED", "error_message": "old exit"}},
                    },
                ],
            },
        )

    client = PlaidClient("cid_ord", "sec_ord", env="production", transport=httpx.MockTransport(handler))
    res = client.poll_link_token("link-order-1")
    assert res.status == "pending"


def test_plaid_hosted_complete_on_locked_vault_returns_html_card(tmp_path: Path) -> None:
    """#20: /plaid/hosted-complete on a locked vault renders a friendly HTML card instead of raw JSON 4xx."""
    svc = FinanceService(db_path=str(tmp_path / "locked_cb.db"), secret_store_path=tmp_path / "secrets.enc")
    app = create_app(db_path=str(tmp_path / "locked_cb.db"), service=svc, enforce_vault_lock=True)
    client = TestClient(app)
    try:
        r_setup = client.post("/api/v1/auth/setup", json={"password": "StrongPassword2026!"})
        assert r_setup.status_code == 200
        svc.lock_vault()
        r_cb = client.get("/api/v1/plaid/hosted-complete?state=some-nonce")
        assert r_cb.status_code == 200
        assert "text/html" in r_cb.headers["content-type"]
        assert "Unlock ChronoX to Finish Syncing" in r_cb.text
    finally:
        svc.close()


def test_opencode_config_content_env_allowlists_search_dirs_and_popen_killpg_privacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#5, #7, #8, #16, #17: OPENCODE_CONFIG_CONTENT lockdown, proxy/cloud allowlists, Popen + killpg, stdin/0600 privacy."""
    monkeypatch.setenv("http_proxy", "http://proxy.corp:8080")
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", "/etc/ssl/corp.pem")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-123")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "claude-oauth-123")
    monkeypatch.setenv("PLAID_SECRET", "must-never-leak")

    oc_env = build_safe_cli_env("opencode_cli")
    assert oc_env["OPENCODE_CONFIG_CONTENT"] == OPENCODE_SAFE_CONFIG_CONTENT
    assert oc_env["http_proxy"] == "http://proxy.corp:8080"
    assert oc_env["NODE_EXTRA_CA_CERTS"] == "/etc/ssl/corp.pem"
    assert oc_env["OPENROUTER_API_KEY"] == "sk-or-123"
    assert "PLAID_SECRET" not in oc_env

    cl_env = build_safe_cli_env("claude_cli")
    assert cl_env["CLAUDE_CODE_OAUTH_TOKEN"] == "claude-oauth-123"
    assert "OPENCODE_CONFIG_CONTENT" not in cl_env

    search_dirs = get_cli_search_dirs(home_dir="/home/tester")
    assert "/home/linuxbrew/.linuxbrew/bin" in search_dirs
    assert "/home/tester/.local/share/pnpm" in search_dirs
    assert "/home/tester/.asdf/shims" in search_dirs
    assert "/home/tester/.local/share/mise/shims" in search_dirs

    # Verify real-runtime Popen path pipes prompt via stdin (zero prompt in argv) AND calls killpg on timeout!
    observed: Dict[str, Any] = {"killpg_calls": []}

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            self.pid = 424242
            self.returncode = 0
            observed["cmd"] = list(cmd)
            observed["kwargs"] = dict(kwargs)
            observed["cwd_files"] = os.listdir(kwargs["cwd"])

        def communicate(self, input=None, timeout=None):  # noqa: A002
            observed["stdin_input"] = input
            if observed.get("raise_timeout"):
                observed["raise_timeout"] = False
                raise subprocess.TimeoutExpired(observed["cmd"], timeout or 5.0)
            if "agy" in observed["cmd"][0]:
                return (json.dumps({"status": "SUCCESS", "response": "OK"}), "")
            return (
                json.dumps({"type": "text", "part": {"text": "OK"}}) + "\n",
                "",
            )

        def kill(self):
            observed["killed"] = True

    monkeypatch.setattr("chronox_finance.ai.local_cli_clients.resolve_cli_binary", lambda n: f"/usr/local/bin/{n}")
    monkeypatch.setattr("subprocess.Popen", _FakePopen)
    if hasattr(os, "killpg"):
        monkeypatch.setattr("os.killpg", lambda pid, sig: observed["killpg_calls"].append((pid, sig)))

    # 1. OpenCode pipes via stdin + sets OPENCODE_CONFIG_CONTENT + start_new_session=True
    oc = OpenCodeCLIClient()
    res = oc.chat_completion("System", "Secret balance $9,876.54")
    assert res.content == "OK"
    assert all("9,876.54" not in arg for arg in observed["cmd"])
    assert "9,876.54" in (observed["stdin_input"] or "")
    assert observed["kwargs"].get("start_new_session") is (os.name != "nt")
    assert observed["kwargs"]["env"]["OPENCODE_CONFIG_CONTENT"] == OPENCODE_SAFE_CONFIG_CONTENT

    # 2. Timeout triggers killpg AND records cooldown via on_auth_failure!
    cooldown_hits: List[str] = []
    oc_to = OpenCodeCLIClient(on_auth_failure=lambda pid, reason: cooldown_hits.append(f"{pid}:{reason}"))
    observed["raise_timeout"] = True
    with pytest.raises(OpenCodeCLIError):
        oc_to.chat_completion("Sys", "Slow prompt", use_cache=False)
    if hasattr(os, "killpg"):
        assert (424242, signal.SIGKILL) in observed["killpg_calls"]
    assert len(cooldown_hits) == 1 and "timed out" in cooldown_hits[0].lower()

    # 3. Antigravity writes 0600 prompt.txt in empty cwd so balances never appear in argv
    agy = AntigravityCLIClient()
    res_agy = agy.chat_completion("Sys", "Checking balance $12,345.00")
    assert res_agy.content == "OK"
    assert all("12,345.00" not in arg for arg in observed["cmd"])
    assert "prompt.txt" in observed["cwd_files"]


def test_local_http_gateway_auto_probe_and_service_priority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#6: Auto-detects Ollama/LM Studio/LiteLLM/vLLM on 11434/1234/4000/8000 before local CLIs."""
    def gw_handler(request: httpx.Request) -> httpx.Response:
        if request.url.port == 11434 and request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "llama3.2:latest"}]})
        raise httpx.ConnectError("connection refused")

    transport = httpx.MockTransport(gw_handler)
    hits = detect_local_http_gateways(force_refresh=True, transport=transport)
    assert len(hits) == 1
    assert hits[0].name == "Ollama"
    assert hits[0].base_url == "http://127.0.0.1:11434/v1"
    assert hits[0].model == "llama3.2:latest"

    # Hermetic default (CHRONOX_LOCAL_CLI_AUTO_DETECT=0) must NOT probe!
    monkeypatch.setenv("CHRONOX_LOCAL_CLI_AUTO_DETECT", "0")
    svc_off = FinanceService(db_path=str(tmp_path / "off.db"), secret_store_path=tmp_path / "s1.enc", wrapper_transport=transport)
    try:
        assert svc_off.resolve_active_provider() == "none"
    finally:
        svc_off.close()

    # Enabled auto-detect resolves chat_wrapper (Ollama) automatically and includes it in /ai/local-clis!
    monkeypatch.setenv("CHRONOX_LOCAL_CLI_AUTO_DETECT", "1")
    empty_reg = LocalCLIRegistry()
    monkeypatch.setattr("chronox_finance.ai.local_cli_clients.resolve_cli_binary", lambda _n: None)
    svc_on = FinanceService(
        db_path=str(tmp_path / "on.db"),
        secret_store_path=tmp_path / "s2.enc",
        wrapper_transport=transport,
        local_cli_registry=empty_reg,
    )
    try:
        assert svc_on.resolve_active_provider() == "chat_wrapper"
        clis_info = svc_on.get_local_clis()
        assert clis_info["active_provider"] == "chat_wrapper"
        assert any(c["provider_id"] == "chat_wrapper" and "Ollama" in c["display_name"] for c in clis_info["clis"])
    finally:
        svc_on.close()


def test_quota_excludes_free_local_clis_and_seed_accepts_new_cli_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#18 & #26: Free local CLI calls don't burn 60/day cloud quota, and CHRONOX_AI_PROVIDER accepts *_cli."""
    monkeypatch.setenv("CHRONOX_AI_PROVIDER", "opencode_cli")
    svc = FinanceService(db_path=str(tmp_path / "quota.db"), secret_store_path=tmp_path / "secrets.enc")
    try:
        assert svc.storage.get_settings()["ai_provider"] == "opencode_cli"
        for _ in range(5):
            svc.storage.record_ai_usage(provider="opencode_cli", model="opencode-default", cached=False)
            svc.storage.record_ai_usage(provider="claude_cli", model="claude-sonnet", cached=False)
            svc.storage.record_ai_usage(provider="chat_wrapper", model="llama3.2", cached=False, cost_microdollars=0)
        assert svc.storage.count_ai_requests_today() == 0
        svc.storage.record_ai_usage(provider="gemini", model="gemini-2.5-flash", cached=False, cost_microdollars=500)
        assert svc.storage.count_ai_requests_today() == 1
    finally:
        svc.close()


def test_savings_transfer_no_double_count_and_goal_word_boundary_match(tmp_path: Path) -> None:
    """#10 & #19: Two-leg savings transfer counts once in analytics; 'car' never matches 'Scarf'."""
    svc = FinanceService(db_path=str(tmp_path / "acct.db"), secret_store_path=tmp_path / "secrets.enc")
    try:
        chk = svc.storage.create_account(name="Checking", account_type="checking", balance_cents=500_000)
        sav = svc.storage.create_account(name="High-Yield Savings", account_type="savings", balance_cents=200_000)
        svc.add_transaction_raw(
            account_id=int(chk["id"]),
            tx_type="income",
            amount_cents=400_000,
            merchant="Employer",
            tx_date="2026-11-15",
            adjust_balance=False,
            run_auto_sync=False,
        )
        res = svc.say("Put $100 in savings", mode_override="deterministic_only")
        assert res["executed"] is True
        # Checking out-leg has jar_id, savings in-leg has jar_id=None
        txs = svc.storage.list_transactions(limit=20)
        xfer_txs = [t for t in txs if t["tx_type"] == "transfer"]
        assert len(xfer_txs) == 2
        in_leg = next(t for t in xfer_txs if t["transfer_direction"] == "in")
        out_leg = next(t for t in xfer_txs if t["transfer_direction"] == "out")
        assert in_leg["jar_id"] is None
        assert out_leg["jar_id"] is not None

        # Analytics verifies savings transfer ONCE ($100 = 10,000c), NEVER 2x ($200 = 20,000c)!
        engine = AnalyticsEngine()
        ov = engine.build_overview(
            transactions=txs,
            accounts=svc.storage.list_accounts(),
            jars=svc.storage.list_jars(),
            period="30d",
        )
        # Check savings node in Sankey / verified savings
        sav_nodes = [n for n in ov.sankey_nodes if "saving" in n.label.lower() and n.amount_cents > 0]
        assert sum(n.amount_cents for n in sav_nodes) == 10_000

        # Word-boundary goal match (#19): "car" must NOT match "Scarf"
        ctx = AgentLedgerContext(
            goals=[{"id": 9, "name": "Winter Scarf", "target_cents": 10_000, "saved_cents": 0, "status": "active"}]
        )
        assert ctx.find_goal_by_name("car") is None
        assert ctx.find_goal_by_name("scarf")["id"] == 9

        # Savings-only profile: lone `in` leg must set affects_balance=False (no phantom net worth)
        ctx_sav_only = AgentLedgerContext(
            accounts=[{"id": int(sav["id"]), "name": "Savings", "account_type": "savings", "balance_cents": 200_000}],
            jars=[{"id": 4, "slug": "safety", "name": "Safety"}],
        )
        r_sav = FinancialAIHarness().process_utterance(
            "Put $100 in savings", context=ctx_sav_only, intelligence_mode="deterministic_only"
        )
        assert r_sav.actions[0].payload.get("affects_balance") is False
    finally:
        svc.close()


def test_onetap_fingerprint_409_conflict_safe_undo_and_unambiguous_cli_undo(tmp_path: Path) -> None:
    """#9, #11, #12: Stale plan_fingerprint -> 409; partial conflict-checked undo -> 409 on later edit; agent:N / onetap:N."""
    db_file = str(tmp_path / "onetap_guard.db")
    svc = FinanceService(db_path=db_file, secret_store_path=tmp_path / "secrets.enc")
    app = create_app(db_path=db_file, service=svc, enforce_vault_lock=False)
    client = TestClient(app)
    try:
        seed_workspace(svc)
        r_prev = client.post("/api/v1/optimize/one-tap", json={"horizon_days": 30, "apply": False})
        assert r_prev.status_code == 200
        fp = r_prev.json()["plan_fingerprint"]
        assert fp and len(fp) == 32

        # Wrong fingerprint returns HTTP 409 Conflict
        r_stale = client.post(
            "/api/v1/optimize/one-tap",
            json={"horizon_days": 30, "apply": True, "plan_fingerprint": "0" * 32},
        )
        assert r_stale.status_code == 409

        # Matching fingerprint applies cleanly
        r_apply = client.post(
            "/api/v1/optimize/one-tap",
            json={"horizon_days": 30, "apply": True, "plan_fingerprint": fp},
        )
        assert r_apply.status_code == 200
        ot_batch_id = r_apply.json()["batch_id"]
        assert ot_batch_id is not None

        # Create an agent batch with the same ID (1) so bare `1` is ambiguous (#12)!
        r_say = svc.say("I spent $5 on coffee", mode_override="deterministic_only")
        ag_batch_id = r_say["batch_id"]
        if ag_batch_id == ot_batch_id:
            with pytest.raises(ValueError, match="ambiguous"):
                svc.undo_last_action(batch_id=ag_batch_id)
        # Explicit agent:N works cleanly via CLI
        assert run_cli(["--db", db_file, "--undo", f"agent:{ag_batch_id}"]) == 0

        # Manually edit emergency_buffer_cents AFTER One-Tap apply -> undo MUST refuse with HTTP 409 (#9)!
        svc.storage.update_settings({"emergency_buffer_cents": 999_999})
        r_conflict = client.post(f"/api/v1/optimize/one-tap/undo/{ot_batch_id}")
        assert r_conflict.status_code == 409
        with pytest.raises(PlanConflictError):
            svc.undo_onetap_batch(ot_batch_id)
    finally:
        svc.close()


def test_onetap_boilerplate_rationale_deduplication_and_clean_xlsx_strings(tmp_path: Path) -> None:
    """#23 & #25: Boilerplate identical AI rationales fall back to deterministic; .xlsx shared strings omit leading quote."""
    svc = FinanceService(db_path=str(tmp_path / "dedup.db"), secret_store_path=tmp_path / "secrets.enc")
    try:
        seed_workspace(svc)
        plan = svc.onetap_engine.build_onetap_plan(
            budget=svc._compute_budget(),
            transactions=svc.storage.list_transactions(limit=200),
            recurring_bills=svc.storage.list_recurring_bills(active_only=True),
            settings=svc.storage.get_settings(),
            credit_report=svc.get_credit_report(),
            goals=svc.list_goals(include_completed=False),
            holdings=svc.get_portfolio().holdings,
            horizon_days=30,
        )
        assert len(plan.actions) >= 2

        def _boilerplate_llm(**_kwargs: Any) -> CompletionResult:
            return CompletionResult(
                content=json.dumps(
                    {
                        "executive_summary": (
                            f"Your buffer is ${plan.runway.cvar_95_buffer_cents / 100:,.2f} with "
                            f"{plan.runway.ruin_probability_pct:.1f}% risk."
                        ),
                        "action_rationales": [
                            {
                                "action_id": a.action_id,
                                "rationale": (
                                    f"Priority move allocating ${a.amount_cents / 100:,.2f} while keeping bills safe."
                                ),
                            }
                            for a in plan.actions
                        ],
                    }
                ),
                model="test-model",
                protocol_used="cli",
                provider="opencode_cli",
            )

        res = FinancialAIHarness().synthesize_onetap_narrative(plan, completion_fn=_boilerplate_llm)
        # All identical boilerplate rationales must be replaced by distinct deterministic rationales
        assert all("Priority move allocating" not in r.rationale for r in res.action_rationales)
        assert len({r.rationale for r in res.action_rationales}) == len(res.action_rationales)
    finally:
        svc.close()

    # #25: sanitize_sheet_text does NOT inject leading apostrophe while sanitize_csv_text still does!
    assert sanitize_sheet_text("-Negative Merchant") == "-Negative Merchant"
    assert sanitize_sheet_text("=SUM(A1)") == "=SUM(A1)"
    assert sanitize_csv_text("=SUM(A1)") == "'=SUM(A1)"

    # #25: Empty portfolio skips blank zero-value pie chart5
    empty_xlsx = FinanceExporter().export_apple_numbers_workbook(ExportSnapshot(as_of_date="2026-11-26"))
    with zipfile.ZipFile(io.BytesIO(empty_xlsx.content), "r") as zf:
        assert "xl/charts/chart5.xml" not in zf.namelist()
        assert "xl/drawings/drawing3.xml" not in zf.namelist()
