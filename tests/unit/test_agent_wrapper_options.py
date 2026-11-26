"""Unit tests for per-agent wrapper routing, per-agent model selection, and OpenCode fallback."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest

from chronox_finance.ai.codex_client import CodexResponsesClient
from chronox_finance.ai.local_cli_clients import OpenCodeCLIClient
from chronox_finance.models import ProfileSettingsUpdate
from chronox_finance.service import FinanceService


def test_per_agent_wrapper_routing_and_model_prefixing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each of the 4 local agents can independently route through a chat wrapper with custom models."""
    monkeypatch.setenv("CHRONOX_LOCAL_CLI_AUTO_DETECT", "0")
    captured_requests: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "providers": {
                        "codex": {"available": True, "reason_code": "ready", "version": "0.171"},
                        "claude": {"available": False, "reason_code": "provider_authentication_failed", "version": "2.1"},
                        "opencode": {"available": True, "reason_code": "ready", "version": "2.0.24"},
                        "agy": {"available": True, "reason_code": "ready", "version": "1.5.18"},
                    },
                },
            )
        if request.url.path == "/v1/chat/completions":
            body = json.loads(request.content.decode("utf-8"))
            captured_requests.append(
                {
                    "auth": request.headers.get("authorization"),
                    "model": body.get("model"),
                }
            )
            if str(body.get("model", "")).startswith("claude:"):
                return httpx.Response(502, json={"error": {"message": "claude auth required"}})
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "model": body.get("model"),
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
                },
            )
        return httpx.Response(404)

    monkeypatch.setenv("CHRONOX_WRAPPER_API_KEY", "test-wrapper-token")
    svc = FinanceService(
        db_path=str(tmp_path / "fin.sqlite3"),
        secret_store_path=tmp_path / "secrets.enc",
        wrapper_transport=httpx.MockTransport(handler),
    )
    try:
        updated = svc.update_settings(
            ProfileSettingsUpdate(
                wrapper_base_url="http://127.0.0.1:8788/v1",
                wrapper_wire_api="chat_completions",
                codex_cli_model="gpt-5.3-codex",
                claude_cli_model="opus",
                opencode_cli_model="opencode-go/glm-5.3-flash",
                agy_cli_model="gemini-4-neon-high",
                codex_cli_use_wrapper=True,
                claude_cli_use_wrapper=True,
                opencode_cli_use_wrapper=True,
                agy_cli_use_wrapper=True,
            )
        )
        assert updated.codex_cli_use_wrapper is True
        assert updated.agy_cli_model == "gemini-4-neon-high"

        # Verify each agent builds a wrapper-routed client with provider-prefixed model
        for pid, expected_model in (
            ("codex_cli", "codex:gpt-5.3-codex"),
            ("claude_cli", "claude:opus"),
            ("opencode_cli", "opencode:opencode-go/glm-5.3-flash"),
            ("agy_cli", "agy:gemini-4-neon-high"),
        ):
            client = svc._build_local_cli_client(pid, enabled=True)
            assert isinstance(client, CodexResponsesClient)
            assert client.provider_name == pid
            assert client.model == expected_model
            assert client.base_url == "http://127.0.0.1:8788/v1"

        # Verify get_local_clis reports transport, wrapper readiness, and selected model
        local_info = svc.get_local_clis(probe=False)
        by_id = {item["provider_id"]: item for item in local_info["clis"]}
        assert by_id["agy_cli"]["transport"] == "wrapper"
        assert by_id["agy_cli"]["use_wrapper"] is True
        assert by_id["agy_cli"]["selected_model"] == "gemini-4-neon-high"
        assert by_id["agy_cli"]["auth_ready"] is True
        assert by_id["claude_cli"]["auth_ready"] is False
        assert "provider_authentication_failed" in by_id["claude_cli"]["status_detail"]

        # Verify live connection test uses wrapper and truthfully distinguishes OK vs auth failure
        res_agy = svc.test_ai_connection(provider="agy_cli")
        assert res_agy["ok"] is True
        assert res_agy["model"] == "agy:gemini-4-neon-high"

        res_claude = svc.test_ai_connection(provider="claude_cli")
        assert res_claude["ok"] is False

        assert any(r["model"] == "agy:gemini-4-neon-high" and r["auth"] == "Bearer test-wrapper-token" for r in captured_requests)

        # Invalid model characters must be rejected
        with pytest.raises(ValueError, match="Invalid model identifier"):
            svc.update_settings(ProfileSettingsUpdate(agy_cli_model="bad model; rm -rf /"))
    finally:
        svc.close()


def test_opencode_cli_retries_standalone_when_archived_flag_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenCode v2.0.24 rejects `--archived`; adapter must transparently retry with `--standalone`."""
    calls: List[List[str]] = []

    def fake_run(cmd: List[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        if "--archived" in cmd:
            return subprocess.CompletedProcess(
                cmd,
                returncode=1,
                stdout="",
                stderr="ERROR\n  Unrecognized flag: --archived in command opencode run",
            )
        return subprocess.CompletedProcess(
            cmd,
            returncode=0,
            stdout='{"type":"text","part":{"text":"OK"}}\n{"type":"step_finish","part":{"tokens":{"input":5,"output":1}}}\n',
            stderr="",
        )

    monkeypatch.setattr("chronox_finance.ai.local_cli_clients.resolve_cli_binary", lambda _b: "/usr/local/bin/opencode")
    monkeypatch.setattr("chronox_finance.ai.local_cli_clients.run_isolated_cli_subprocess", fake_run)

    client = OpenCodeCLIClient(enabled=True)
    res = client.chat_completion("Sys", "Ping", use_cache=False)
    assert res.content == "OK"
    assert len(calls) == 2
    assert "--archived" in calls[0]
    assert "--standalone" in calls[1]
    assert "--archived" not in calls[1]


def test_wrapper_single_session_conversation_id_dedup_reset_and_non_omc_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    HTTP chat wrappers reuse ONE persistent `conversation_id` per backend provider
    so ChronoX never spams session history, skips repeating the 10KB static prefix
    on resumed turns, never leaks `conversation_id` to cloud OpenAI, gracefully
    retries if a non-OMC gateway rejects unknown keys, and rotates cleanly on reset.
    """
    from fastapi.testclient import TestClient

    from chronox_finance.ai.codex_client import (
        CHRONOX_STATIC_KNOWLEDGE_PREFIX,
        clear_primed_wrapper_conversations,
    )
    from chronox_finance.api import create_app
    from chronox_finance.security import CSRF_HEADER_NAME

    clear_primed_wrapper_conversations()

    monkeypatch.setenv("CHRONOX_LOCAL_CLI_AUTO_DETECT", "0")
    monkeypatch.setenv("CHRONOX_DISABLE_LOCAL_HTTP_PROBE", "1")
    monkeypatch.setenv("CHRONOX_WRAPPER_API_KEY", "test-wrapper-token")

    captured: List[Dict[str, Any]] = []
    reject_conv_id = {"flag": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True, "providers": {"agy": {"available": True}, "codex": {"available": True}}})
        if request.url.path == "/v1/chat/completions":
            body = json.loads(request.content.decode("utf-8"))
            captured.append(body)
            if reject_conv_id["flag"] and "conversation_id" in body:
                return httpx.Response(400, json={"error": {"message": "Unknown field: conversation_id"}})
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-sess",
                    "object": "chat.completion",
                    "model": body.get("model"),
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14},
                },
            )
        return httpx.Response(404)

    db_path = tmp_path / "sess.sqlite3"
    sec_path = tmp_path / "secrets.enc"
    svc = FinanceService(db_path=str(db_path), secret_store_path=sec_path, wrapper_transport=httpx.MockTransport(handler))
    try:
        # Default ON
        s0 = svc.get_settings()
        assert s0.wrapper_single_session is True
        assert s0.wrapper_session_key == "chronox-finance"

        svc.update_settings(
            ProfileSettingsUpdate(
                wrapper_base_url="http://127.0.0.1:8788/v1",
                wrapper_wire_api="chat_completions",
                wrapper_model="agy:gemini-4-neon-high",
                codex_cli_use_wrapper=True,
                agy_cli_use_wrapper=True,
                claude_cli_use_wrapper=True,
                opencode_cli_use_wrapper=True,
            )
        )

        # 1. Per-provider stable conversation_id prevents cross-provider collision in OMC SessionStore
        c_agy = svc._build_local_cli_client("agy_cli", enabled=True)
        c_codex = svc._build_local_cli_client("codex_cli", enabled=True)
        c_claude = svc._build_local_cli_client("claude_cli", enabled=True)
        c_oc = svc._build_local_cli_client("opencode_cli", enabled=True)
        c_gw_agy = svc.build_wrapper_client()
        assert isinstance(c_agy, CodexResponsesClient)
        assert c_agy.conversation_id == "chronox-finance-agy"
        assert c_codex.conversation_id == "chronox-finance-codex"
        assert c_claude.conversation_id == "chronox-finance-claude"
        assert c_oc.conversation_id == "chronox-finance-opencode"
        # chat_wrapper targeting agy:* shares the SAME single Antigravity session!
        assert c_gw_agy.conversation_id == "chronox-finance-agy"

        # 2. Turn 1 sends conversation_id + persist=True + full static prefix; Turn 2 reuses SAME id & skips 10KB duplicate prefix
        c_agy.chat_completion("Task instructions", "First turn", use_cache=False)
        c_agy.chat_completion("Task instructions", "Second turn", use_cache=False)
        assert captured[0]["conversation_id"] == "chronox-finance-agy"
        assert captured[0]["persist"] is True
        assert CHRONOX_STATIC_KNOWLEDGE_PREFIX[:60] in captured[0]["messages"][0]["content"]

        assert captured[1]["conversation_id"] == "chronox-finance-agy"
        assert captured[1]["persist"] is True
        assert CHRONOX_STATIC_KNOWLEDGE_PREFIX[:60] not in captured[1]["messages"][0]["content"]
        assert "Task instructions" in captured[1]["messages"][0]["content"]

        # 3. Non-OMC local gateway rejecting conversation_id with HTTP 400 transparently retries without it
        reject_conv_id["flag"] = True
        c_codex.chat_completion("Sys", "Ping non-OMC", use_cache=False)
        assert "conversation_id" in captured[-2]
        assert "conversation_id" not in captured[-1]
        assert "persist" not in captured[-1]
        reject_conv_id["flag"] = False

        # 4. API reset rotates wrapper_session_key so next call starts a fresh single session
        app = create_app(db_path=db_path, service=svc, enforce_vault_lock=True)
        client = TestClient(app)
        csrf = client.post("/api/v1/auth/setup", json={"password": "SessionTest!2026"}).json()["csrf_token"]
        rst = client.post("/api/v1/ai/sessions/reset", headers={CSRF_HEADER_NAME: csrf})
        assert rst.status_code == 200
        new_key = rst.json()["wrapper_session_key"]
        assert new_key.startswith("chronox-finance-") and new_key != "chronox-finance"
        c_agy_fresh = svc._build_local_cli_client("agy_cli", enabled=True)
        assert c_agy_fresh.conversation_id == f"{new_key}-agy"

        # 5. Disabling wrapper_single_session omits conversation_id completely
        svc.update_settings(ProfileSettingsUpdate(wrapper_single_session=False))
        c_ephemeral = svc._build_local_cli_client("agy_cli", enabled=True)
        assert c_ephemeral.conversation_id is None
        c_ephemeral.chat_completion("Sys", "Ephemeral ping", use_cache=False)
        assert "conversation_id" not in captured[-1]

        # 6. Cloud OpenAI client NEVER sends conversation_id even if set on non-local URL
        cloud_seen: List[Dict[str, Any]] = []

        def cloud_handler(req: httpx.Request) -> httpx.Response:
            cloud_seen.append(json.loads(req.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "CLOUD_OK"}}], "usage": {"prompt_tokens": 5, "completion_tokens": 2}},
            )

        cloud_c = CodexResponsesClient(
            api_key="sk-test-cloud",
            base_url="https://api.openai.com/v1",
            model="gpt-4.1-mini",
            provider_name="openai_codex",
            wire_api="chat_completions",
            conversation_id="should-never-send",
            transport=httpx.MockTransport(cloud_handler),
        )
        cloud_c.chat_completion("Sys", "Hello cloud", use_cache=False)
        assert "conversation_id" not in cloud_seen[0]
        assert "persist" not in cloud_seen[0]
    finally:
        svc.close()


def test_direct_cli_agy_and_opencode_persist_single_session_and_self_heal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct local `agy` and `opencode` CLIs capture & reuse ONE persisted session ID and self-heal if deleted."""
    monkeypatch.setenv("CHRONOX_LOCAL_CLI_AUTO_DETECT", "1")
    monkeypatch.setenv("CHRONOX_DISABLE_LOCAL_HTTP_PROBE", "1")
    monkeypatch.setattr("chronox_finance.ai.local_cli_clients.resolve_cli_binary", lambda name: f"/usr/local/bin/{name}")

    agy_cmds: List[List[str]] = []
    agy_mode = {"fail_stale": False}

    def fake_run(cmd: List[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        agy_cmds.append(list(cmd))
        if cmd[0].endswith("/agy"):
            if "--conversation" in cmd and agy_mode["fail_stale"]:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Conversation not found")
            cid = cmd[cmd.index("--conversation") + 1] if "--conversation" in cmd else (
                "agy-conv-100" if agy_mode["fail_stale"] else "agy-conv-99"
            )
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps({"status": "SUCCESS", "response": "AGY_OK", "conversation_id": cid}),
                stderr="",
            )
        if cmd[0].endswith("/opencode"):
            if "--archived" in cmd:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Unrecognized flag: --archived")
            sid = cmd[cmd.index("--session") + 1] if "--session" in cmd else "ses_oc_77"
            out = (
                json.dumps({"type": "text", "sessionID": sid, "part": {"text": "OC_OK"}})
                + "\n"
                + json.dumps({"type": "step_finish", "sessionID": sid, "part": {"tokens": {"input": 5, "output": 2}}})
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="1.0.0\n", stderr="")

    monkeypatch.setattr("chronox_finance.ai.local_cli_clients.run_isolated_cli_subprocess", fake_run)

    db_path = tmp_path / "cli_sess.sqlite3"
    sec_path = tmp_path / "secrets.enc"
    svc = FinanceService(db_path=str(db_path), secret_store_path=sec_path)
    try:
        # 1. Antigravity direct CLI: Turn 1 mints & persists agy-conv-99; Turn 2 (new client!) passes --conversation agy-conv-99
        c1 = svc._build_local_cli_client("agy_cli", enabled=True, force_direct_cli=True)
        assert c1.chat_completion("Sys", "Turn 1", use_cache=False).content == "AGY_OK"
        assert "--conversation" not in agy_cmds[-1]

        c2 = svc._build_local_cli_client("agy_cli", enabled=True, force_direct_cli=True)
        assert c2.chat_completion("Sys", "Turn 2", use_cache=False).content == "AGY_OK"
        assert "--conversation" in agy_cmds[-1]
        assert agy_cmds[-1][agy_cmds[-1].index("--conversation") + 1] == "agy-conv-99"

        # 2. Stale conversation self-heal: retries once fresh and updates persisted ID to agy-conv-100
        agy_mode["fail_stale"] = True
        c3 = svc._build_local_cli_client("agy_cli", enabled=True, force_direct_cli=True)
        assert c3.chat_completion("Sys", "Turn 3", use_cache=False).content == "AGY_OK"
        assert "--conversation" in agy_cmds[-2]
        assert "--conversation" not in agy_cmds[-1]
        c4 = svc._build_local_cli_client("agy_cli", enabled=True, force_direct_cli=True)
        assert getattr(c4, "_session_id", None) == "agy-conv-100"

        # 3. OpenCode direct CLI: Turn 1 captures ses_oc_77; Turn 2 passes --session ses_oc_77
        oc1 = svc._build_local_cli_client("opencode_cli", enabled=True, force_direct_cli=True)
        assert oc1.chat_completion("Sys", "OC Turn 1", use_cache=False).content == "OC_OK"
        oc2 = svc._build_local_cli_client("opencode_cli", enabled=True, force_direct_cli=True)
        assert oc2.chat_completion("Sys", "OC Turn 2", use_cache=False).content == "OC_OK"
        assert "--session" in agy_cmds[-1]
        assert agy_cmds[-1][agy_cmds[-1].index("--session") + 1] == "ses_oc_77"

        # 4. Reset clears all persisted direct CLI session IDs
        svc.reset_ai_sessions()
        assert getattr(svc._build_local_cli_client("agy_cli", enabled=True, force_direct_cli=True), "_session_id", None) is None
        assert getattr(svc._build_local_cli_client("opencode_cli", enabled=True, force_direct_cli=True), "_session_id", None) is None
    finally:
        svc.close()

    # 5. Settings UI contract assertions
    repo_root = Path(__file__).resolve().parents[2]
    index_html = (repo_root / "chronox_finance" / "web" / "index.html").read_text(encoding="utf-8")
    vault_js = (repo_root / "chronox_finance" / "web" / "js" / "vault.js").read_text(encoding="utf-8")
    assert 'id="set-wrapper-single-session"' in index_html
    assert 'id="btn-reset-ai-sessions"' in index_html
    assert "/api/v1/ai/sessions/reset" in vault_js
