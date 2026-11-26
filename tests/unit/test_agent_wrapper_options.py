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
