"""
Comprehensive deterministic unit tests for:
1. Plaid Hosted Link creation retries (`INVALID_PRODUCT` / `PRODUCTS_NOT_SUPPORTED` and
   `INVALID_FIELD` / `INVALID_CONFIGURATION`), default Production env, and two-pass
   multi-session / OAuth-handoff / legacy `on_success` Link polling.
2. Automagic Local AI CLI discovery (`resolve_cli_binary`, `get_cli_search_dirs`,
   `build_augmented_cli_path`, `discover_local_ai_clis`), least-privilege safe subprocess
   environments, and `desktop/src/main/backend-manager.js` PATH augmentation.
3. All 4 hardened `LLMClient` CLI adapters (`OpenCodeCLIClient`, `ClaudeCLIClient`,
   `CodexCLIClient`, `AntigravityCLIClient`) and `LocalCLIRegistry` 5-minute auth failover.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest

from chronox_finance.ai.codex_client import (
    _SAFE_CLI_ENV_ALLOWLIST,
    DEFAULT_CODEX_CLI_MODEL,
    CodexCLIClient,
    CodexCLIError,
    _parse_codex_cli_json_output,
    build_safe_cli_env,
    resolve_cli_binary,
)
from chronox_finance.ai.local_cli_clients import (
    DEFAULT_AGY_CLI_MODEL,
    LOCAL_CLI_PRIORITY,
    AntigravityCLIClient,
    AntigravityCLIError,
    ClaudeCLIClient,
    ClaudeCLIError,
    LocalCLIRegistry,
    OpenCodeCLIClient,
    OpenCodeCLIError,
    discover_local_ai_clis,
)
from chronox_finance.ai.opencode_client import LLMClient, LLMProviderError
from chronox_finance.connectors.base import ConnectorAuthError, ConnectorError
from chronox_finance.connectors.plaid_client import PlaidClient

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


# ---------------------------------------------------------------------------
# Part A: Plaid create_link_token & poll_link_token
# ---------------------------------------------------------------------------


class TestPlaidLinkCreationAndRetries:
    def test_default_env_is_production_and_hosted_link_config(self):
        seen_bodies: List[Dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.host == "production.plaid.com"
            assert request.url.path == "/link/token/create"
            body = json.loads(request.content.decode("utf-8"))
            seen_bodies.append(body)
            return httpx.Response(
                200,
                json={
                    "link_token": "link-production-001",
                    "hosted_link_url": "https://hosted.plaid.com/link/prod001",
                    "expiration": "2026-11-26T12:00:00Z",
                    "request_id": "req-1",
                },
            )

        client = PlaidClient("cid", "sec", transport=httpx.MockTransport(handler))
        assert client.env == "production"
        assert client.base_url == "https://production.plaid.com"

        # 1. Bare call preserves hosted_link: {} for backward compatibility
        res0 = client.create_link_token()
        assert res0.link_token == "link-production-001"
        assert seen_bodies[0]["hosted_link"] == {}

        # 2. Explicit url_lifetime_seconds=3600 without redirect URI
        client.create_link_token(url_lifetime_seconds=3600)
        assert seen_bodies[1]["hosted_link"] == {"url_lifetime_seconds": 3600}

        # 3. With completion_redirect_uri automatically includes url_lifetime_seconds=3600
        client.create_link_token(completion_redirect_uri="https://chronox.local/plaid/done")
        assert seen_bodies[2]["hosted_link"] == {
            "url_lifetime_seconds": 3600,
            "completion_redirect_uri": "https://chronox.local/plaid/done",
        }

    @pytest.mark.parametrize("error_code", ["INVALID_PRODUCT", "PRODUCTS_NOT_SUPPORTED"])
    def test_auto_retries_once_without_optional_products(self, error_code: str):
        calls: List[Dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            calls.append(body)
            if len(calls) == 1:
                return httpx.Response(
                    400,
                    json={
                        "error_type": "INVALID_REQUEST",
                        "error_code": error_code,
                        "error_message": "investments is not enabled for this client",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "link_token": "link-production-core-tx",
                    "hosted_link_url": "https://hosted.plaid.com/link/core",
                    "expiration": "2026-11-26T12:00:00Z",
                    "request_id": "req-retry-prod",
                },
            )

        client = PlaidClient("cid", "sec", transport=httpx.MockTransport(handler))
        res = client.create_link_token(url_lifetime_seconds=3600)
        assert res.link_token == "link-production-core-tx"
        assert len(calls) == 2
        assert calls[0]["optional_products"] == ["liabilities", "investments"]
        assert "optional_products" not in calls[1]
        assert calls[1]["products"] == ["transactions"]
        assert calls[1]["transactions"] == {"days_requested": 730}
        assert calls[1]["hosted_link"] == {"url_lifetime_seconds": 3600}

    @pytest.mark.parametrize("error_code", ["INVALID_FIELD", "INVALID_CONFIGURATION"])
    def test_auto_retries_once_without_completion_redirect_uri(self, error_code: str):
        calls: List[Dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            calls.append(body)
            if len(calls) == 1:
                return httpx.Response(
                    400,
                    json={
                        "error_type": "INVALID_REQUEST",
                        "error_code": error_code,
                        "error_message": "completion_redirect_uri must be a registered HTTPS URI",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "link_token": "link-production-no-redirect",
                    "hosted_link_url": "https://hosted.plaid.com/link/noredir",
                    "expiration": "2026-11-26T12:00:00Z",
                    "request_id": "req-retry-redir",
                },
            )

        client = PlaidClient("cid", "sec", transport=httpx.MockTransport(handler))
        res = client.create_link_token(
            completion_redirect_uri="http://127.0.0.1:18742/api/v1/plaid/hosted-complete?state=abc"
        )
        assert res.link_token == "link-production-no-redirect"
        assert len(calls) == 2
        assert calls[0]["hosted_link"] == {
            "url_lifetime_seconds": 3600,
            "completion_redirect_uri": "http://127.0.0.1:18742/api/v1/plaid/hosted-complete?state=abc",
        }
        assert calls[1]["hosted_link"] == {"url_lifetime_seconds": 3600}

    def test_composes_both_fallbacks_sequentially_and_never_retries_auth_errors(self):
        calls: List[Dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            calls.append(body)
            if len(calls) == 1:
                return httpx.Response(
                    400,
                    json={"error_code": "INVALID_PRODUCT", "error_message": "liabilities not enabled"},
                )
            if len(calls) == 2:
                return httpx.Response(
                    400,
                    json={"error_code": "INVALID_FIELD", "error_message": "redirect URI rejected"},
                )
            return httpx.Response(
                200,
                json={
                    "link_token": "link-production-double-fallback",
                    "hosted_link_url": "https://hosted.plaid.com/link/ok",
                },
            )

        client = PlaidClient("cid", "sec", transport=httpx.MockTransport(handler))
        res = client.create_link_token(completion_redirect_uri="http://127.0.0.1:9999/cb")
        assert res.link_token == "link-production-double-fallback"
        assert len(calls) == 3
        assert "optional_products" not in calls[2]
        assert calls[2]["hosted_link"] == {"url_lifetime_seconds": 3600}

        # Auth errors MUST fail fast without retrying
        auth_attempts = 0

        def auth_handler(request: httpx.Request) -> httpx.Response:
            nonlocal auth_attempts
            auth_attempts += 1
            return httpx.Response(
                400,
                json={"error_code": "INVALID_API_KEYS", "error_message": "invalid client_id or secret"},
            )

        bad_client = PlaidClient("cid", "bad", transport=httpx.MockTransport(auth_handler))
        with pytest.raises(ConnectorAuthError):
            bad_client.create_link_token(completion_redirect_uri="http://127.0.0.1:9999/cb")
        assert auth_attempts == 1

        # Retry cap: if core product itself fails with INVALID_PRODUCT twice, do not loop forever
        always_fail_attempts = 0

        def always_invalid_product(request: httpx.Request) -> httpx.Response:
            nonlocal always_fail_attempts
            always_fail_attempts += 1
            return httpx.Response(
                400,
                json={"error_code": "INVALID_PRODUCT", "error_message": "still invalid"},
            )

        fail_client = PlaidClient("cid", "sec", transport=httpx.MockTransport(always_invalid_product))
        with pytest.raises(ConnectorError):
            fail_client.create_link_token()
        assert always_fail_attempts == 2


class TestPlaidMultiSessionPolling:
    def test_oauth_handoff_earlier_exit_does_not_kill_active_latest_session(self):
        """
        Regression test for the premature `status="exited"` bug during bank OAuth:
        session[0] had an earlier exit, while session[1] is actively in-progress
        (`finished_at: None`) with no error -> MUST return `pending`, NOT `exited`.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "link_token": "link-prod-oauth",
                    "request_id": "req-oauth-1",
                    "link_sessions": [
                        {
                            "link_session_id": "sess-1",
                            "started_at": "2026-11-26T08:00:00Z",
                            "finished_at": "2026-11-26T08:00:45Z",
                            "exit": {
                                "error": {
                                    "error_code": "INSTITUTION_NO_LONGER_SUPPORTED",
                                    "display_message": "Earlier attempt exited",
                                }
                            },
                        },
                        {
                            "link_session_id": "sess-2",
                            "started_at": "2026-11-26T08:01:00Z",
                            "finished_at": None,
                            "on_exit": {"error": None, "metadata": {"status": "requires_oauth"}},
                        },
                    ],
                },
            )

        client = PlaidClient("cid", "sec", transport=httpx.MockTransport(handler))
        polled = client.poll_link_token("link-prod-oauth")
        assert polled.status == "pending"
        assert polled.public_token is None
        assert polled.items == []

    def test_legacy_on_success_and_deduplication_with_item_add_results(self):
        responses = [
            # 1. Pure legacy `on_success` session
            {
                "link_token": "link-prod-legacy",
                "request_id": "req-leg-1",
                "link_sessions": [
                    {
                        "finished_at": "2026-11-26T08:05:00Z",
                        "on_success": {
                            "public_token": "public-production-nfcu-1",
                            "metadata": {
                                "institution": {
                                    "institution_id": "ins_nfcu",
                                    "name": "Navy Federal Credit Union",
                                },
                                "accounts": [{"id": "acc_1", "name": "Active Duty Checking", "mask": "1234"}],
                            },
                        },
                    }
                ],
            },
            # 2. Both `results.item_add_results` AND `on_success` with same public_token (dedup + merge)
            {
                "link_token": "link-prod-dedup",
                "request_id": "req-leg-2",
                "link_sessions": [
                    {
                        "finished_at": "2026-11-26T08:06:00Z",
                        "results": {
                            "item_add_results": [
                                {
                                    "public_token": "public-production-dup-1",
                                    "institution": {"institution_id": "ins_chase", "name": ""},
                                    "accounts": [],
                                }
                            ]
                        },
                        "on_success": {
                            "public_token": "public-production-dup-1",
                            "metadata": {
                                "institution": {"institution_id": "ins_chase", "name": "Chase"},
                                "accounts": [{"id": "acc_c1", "name": "Sapphire Checking"}],
                            },
                        },
                    }
                ],
            },
        ]
        idx = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            payload = responses[idx["n"]]
            idx["n"] += 1
            return httpx.Response(200, json=payload)

        client = PlaidClient("cid", "sec", transport=httpx.MockTransport(handler))

        r1 = client.poll_link_token("link-prod-legacy")
        assert r1.status == "completed"
        assert r1.public_token == "public-production-nfcu-1"
        assert r1.institution_id == "ins_nfcu"
        assert r1.institution_name == "Navy Federal Credit Union"
        assert len(r1.accounts) == 1
        assert len(r1.items) == 1

        r2 = client.poll_link_token("link-prod-dedup")
        assert r2.status == "completed"
        assert len(r2.items) == 1
        assert r2.public_token == "public-production-dup-1"
        assert r2.institution_name == "Chase"
        assert len(r2.accounts) == 1

    def test_multi_item_across_sessions_wins_even_when_later_session_exited(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "link_token": "link-prod-multi",
                    "request_id": "req-multi-1",
                    "link_sessions": [
                        {
                            "finished_at": "2026-11-26T08:10:00Z",
                            "results": {
                                "item_add_results": [
                                    {
                                        "public_token": "public-prod-chase",
                                        "institution": {"institution_id": "ins_56", "name": "Chase"},
                                        "accounts": [{"id": "c1", "name": "Checking"}],
                                    },
                                    {
                                        "public_token": "public-prod-amex",
                                        "institution": {"institution_id": "ins_10", "name": "American Express"},
                                        "accounts": [{"id": "a1", "name": "Gold Card"}],
                                    },
                                ]
                            },
                        },
                        {
                            "finished_at": "2026-11-26T08:11:00Z",
                            "on_success": {
                                "public_token": "public-prod-bofa",
                                "metadata": {
                                    "institution": {"institution_id": "ins_4", "name": "Bank of America"},
                                    "accounts": [{"id": "b1", "name": "Advantage Banking"}],
                                },
                            },
                        },
                        {
                            "finished_at": "2026-11-26T08:12:00Z",
                            "on_exit": {
                                "error": {"error_code": "USER_CANCELLED", "display_message": "Done adding banks"}
                            },
                        },
                    ],
                },
            )

        client = PlaidClient("cid", "sec", transport=httpx.MockTransport(handler))
        res = client.poll_link_token("link-prod-multi")
        assert res.status == "completed"
        # Backward-compatible primary item fields populated from item 0
        assert res.public_token == "public-prod-chase"
        assert res.institution_id == "ins_56"
        assert res.institution_name == "Chase"
        # ALL 3 linked items returned in `items`
        assert len(res.items) == 3
        assert [it["public_token"] for it in res.items] == [
            "public-prod-chase",
            "public-prod-amex",
            "public-prod-bofa",
        ]

    def test_terminal_exited_surfaces_error_code_and_message_across_exit_shapes(self):
        payloads = [
            # 1. Explicit on_exit with finished_at and error=None
            {
                "link_sessions": [
                    {
                        "finished_at": "2026-11-26T08:15:00Z",
                        "on_exit": {"error": None, "metadata": {"status": "choose_device"}},
                    }
                ]
            },
            # 2. results.exit with explicit error_code + display_message
            {
                "link_sessions": [
                    {
                        "finished_at": "2026-11-26T08:16:00Z",
                        "results": {
                            "exit": {
                                "error": {
                                    "error_code": "ITEM_LOGIN_REQUIRED",
                                    "display_message": "Bank login timed out during MFA",
                                }
                            }
                        },
                    }
                ]
            },
        ]
        idx = {"i": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            body = payloads[idx["i"]]
            idx["i"] += 1
            return httpx.Response(200, json=body)

        client = PlaidClient("cid", "sec", transport=httpx.MockTransport(handler))

        ex1 = client.poll_link_token("lt-1")
        assert ex1.status == "exited"
        assert "choose_device" in ex1.error_message

        ex2 = client.poll_link_token("lt-2")
        assert ex2.status == "exited"
        assert ex2.error_code == "ITEM_LOGIN_REQUIRED"
        assert ex2.error_message == "Bank login timed out during MFA"


# ---------------------------------------------------------------------------
# Part B1: Binary Discovery, Safe Env Allowlist, and backend-manager.js
# ---------------------------------------------------------------------------


class TestLocalCLIDiscoveryAndSafeEnv:
    def test_resolve_cli_binary_searches_user_dirs_and_sorts_nvm_fnm_newest_first(self, tmp_path: Path):
        fake_home = tmp_path / "home"
        opencode_bin = _make_executable(fake_home / ".opencode" / "bin" / "opencode")
        agy_bin = _make_executable(fake_home / ".local" / "bin" / "agy")
        claude_bin = _make_executable(fake_home / ".claude" / "local" / "claude")

        # Create out-of-order nvm & fnm versions to verify numeric semver sorting (22 > 18 > 8)
        _make_executable(fake_home / ".nvm" / "versions" / "node" / "v8.17.0" / "bin" / "codex")
        _make_executable(fake_home / ".nvm" / "versions" / "node" / "v18.19.0" / "bin" / "codex")
        nvm_newest = _make_executable(fake_home / ".nvm" / "versions" / "node" / "v22.14.0" / "bin" / "codex")

        _make_executable(
            fake_home / ".local" / "share" / "fnm" / "node-versions" / "v20.9.0" / "installation" / "bin" / "fnmcli"
        )
        fnm_newest = _make_executable(
            fake_home / ".local" / "share" / "fnm" / "node-versions" / "v23.2.0" / "installation" / "bin" / "fnmcli"
        )

        # Windows-style APPDATA / LOCALAPPDATA paths also searched when env vars present
        fake_appdata = tmp_path / "AppData" / "Roaming"
        fake_localappdata = tmp_path / "AppData" / "Local"
        win_npm_tool = _make_executable(fake_appdata / "npm" / "winnpmtool")
        win_prog_tool = _make_executable(fake_localappdata / "Programs" / "CustomAI" / "bin" / "winprogtool")

        fake_env = {
            "HOME": str(fake_home),
            "PATH": "",
            "APPDATA": str(fake_appdata),
            "LOCALAPPDATA": str(fake_localappdata),
        }

        assert resolve_cli_binary("opencode", home_dir=str(fake_home), env=fake_env) == str(opencode_bin)
        assert resolve_cli_binary("agy", home_dir=str(fake_home), env=fake_env) == str(agy_bin)
        assert resolve_cli_binary("claude", home_dir=str(fake_home), env=fake_env) == str(claude_bin)
        assert resolve_cli_binary("codex", home_dir=str(fake_home), env=fake_env) == str(nvm_newest)
        assert resolve_cli_binary("fnmcli", home_dir=str(fake_home), env=fake_env) == str(fnm_newest)
        assert resolve_cli_binary("winnpmtool", home_dir=str(fake_home), env=fake_env) == str(win_npm_tool)
        assert resolve_cli_binary("winprogtool", home_dir=str(fake_home), env=fake_env) == str(win_prog_tool)
        assert resolve_cli_binary("definitely-missing-cli-xyz", home_dir=str(fake_home), env=fake_env) is None

    def test_safe_cli_env_allowlist_and_least_privilege_secret_isolation(self):
        required_allowlist_vars = {
            "HOME",
            "USER",
            "LOGNAME",
            "PATH",
            "LANG",
            "LC_ALL",
            "SSL_CERT_FILE",
            "REQUESTS_CA_BUNDLE",
            "XDG_CONFIG_HOME",
            "XDG_CACHE_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "XDG_RUNTIME_DIR",
            "DBUS_SESSION_BUS_ADDRESS",
            "ANTHROPIC_API_KEY",
            "CLAUDE_CONFIG_DIR",
            "OPENCODE_API_KEY",
            "OPENCODE_GO_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "OPENAI_API_KEY",
            "CODEX_API_KEY",
            "CODEX_HOME",
            "APPDATA",
            "LOCALAPPDATA",
            "USERPROFILE",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
        }
        assert required_allowlist_vars <= _SAFE_CLI_ENV_ALLOWLIST

        dirty_env = {
            "HOME": "/home/tester",
            "PATH": "/usr/bin",
            "XDG_DATA_HOME": "/home/tester/.local/share",
            "XDG_STATE_HOME": "/home/tester/.local/state",
            "XDG_RUNTIME_DIR": "/run/user/1000",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
            "REQUESTS_CA_BUNDLE": "/etc/ssl/certs/ca-certificates.crt",
            "OPENAI_API_KEY": "sk-openai-ok",
            "CODEX_API_KEY": "sk-codex-ok",
            "CODEX_HOME": "/home/tester/.codex",
            "ANTHROPIC_API_KEY": "sk-ant-ok",
            "CLAUDE_CONFIG_DIR": "/home/tester/.claude",
            "OPENCODE_API_KEY": "oc-ok",
            "OPENCODE_GO_API_KEY": "ocg-ok",
            "GEMINI_API_KEY": "AIza-gemini-ok",
            "GOOGLE_API_KEY": "AIza-google-ok",
            # Sensitive financial / vault secrets that MUST NEVER leak
            "CHRONOX_FINANCE_SECRET_KEY": "vault-master-secret",
            "CHRONOX_VAULT_PASSPHRASE": "my-vault-passphrase",
            "PLAID_SECRET": "plaid-secret-123",
            "PLAID_CLIENT_ID": "plaid-cid-123",
            "SIMPLEFIN_ACCESS_URL": "https://u:p@bridge.simplefin.org",
            "SCHWAB_APP_SECRET": "schwab-secret",
            "COINBASE_API_SECRET": "coinbase-pem",
            "ROBINHOOD_PRIVATE_KEY": "rh-key",
            "IMAP_APP_PASSWORD": "imap-password",
        }

        secret_keys = {
            "CHRONOX_FINANCE_SECRET_KEY",
            "CHRONOX_VAULT_PASSPHRASE",
            "PLAID_SECRET",
            "PLAID_CLIENT_ID",
            "SIMPLEFIN_ACCESS_URL",
            "SCHWAB_APP_SECRET",
            "COINBASE_API_SECRET",
            "ROBINHOOD_PRIVATE_KEY",
            "IMAP_APP_PASSWORD",
        }

        for pid in ("codex_cli", "claude_cli", "opencode_cli", "agy_cli", None):
            cleaned = build_safe_cli_env(pid, env=dirty_env, home_dir="/home/tester")
            assert cleaned["XDG_DATA_HOME"] == "/home/tester/.local/share"
            assert cleaned["XDG_STATE_HOME"] == "/home/tester/.local/state"
            assert cleaned["XDG_RUNTIME_DIR"] == "/run/user/1000"
            assert cleaned["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/1000/bus"
            assert cleaned["REQUESTS_CA_BUNDLE"] == "/etc/ssl/certs/ca-certificates.crt"
            assert secret_keys.isdisjoint(cleaned.keys())

        # Least-privilege provider isolation
        codex_env = build_safe_cli_env("codex_cli", env=dirty_env, home_dir="/home/tester")
        assert "OPENAI_API_KEY" in codex_env and "CODEX_API_KEY" in codex_env and "CODEX_HOME" in codex_env
        assert "GEMINI_API_KEY" not in codex_env and "ANTHROPIC_API_KEY" not in codex_env

        claude_env = build_safe_cli_env("claude_cli", env=dirty_env, home_dir="/home/tester")
        assert "ANTHROPIC_API_KEY" in claude_env and "CLAUDE_CONFIG_DIR" in claude_env
        assert "OPENAI_API_KEY" not in claude_env and "GEMINI_API_KEY" not in claude_env

        agy_env = build_safe_cli_env("agy_cli", env=dirty_env, home_dir="/home/tester")
        assert "GEMINI_API_KEY" in agy_env and "GOOGLE_API_KEY" in agy_env
        assert "OPENAI_API_KEY" not in agy_env and "ANTHROPIC_API_KEY" not in agy_env

        oc_env = build_safe_cli_env("opencode_cli", env=dirty_env, home_dir="/home/tester")
        assert "OPENCODE_API_KEY" in oc_env and "OPENCODE_GO_API_KEY" in oc_env

    def test_backend_manager_js_augments_path_for_gui_launches(self, tmp_path: Path):
        fake_home = tmp_path / "gui_home"
        for rel in (
            ".opencode/bin",
            ".local/bin",
            ".claude/local",
            ".claude/bin",
            ".nvm/versions/node/v8.17.0/bin",
            ".nvm/versions/node/v22.14.0/bin",
            ".volta/bin",
            ".bun/bin",
        ):
            (fake_home / rel).mkdir(parents=True, exist_ok=True)

        bm_path = REPO_ROOT / "desktop" / "src" / "main" / "backend-manager.js"
        script = (
            f"const {{ buildAugmentedCliPath }} = require({json.dumps(str(bm_path))});"
            f"const out = buildAugmentedCliPath('/usr/bin', {{ homeDir: {json.dumps(str(fake_home))}, platform: 'linux' }});"
            "console.log(out);"
        )
        node_bin = shutil.which("node")
        if not node_bin:
            pytest.skip("node not installed")
        proc = subprocess.run(  # noqa: S603
            [node_bin, "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        entries = proc.stdout.strip().split(":")
        assert str(fake_home / ".opencode" / "bin") in entries
        assert str(fake_home / ".local" / "bin") in entries
        assert str(fake_home / ".claude" / "local") in entries
        assert str(fake_home / ".claude" / "bin") in entries
        assert str(fake_home / ".volta" / "bin") in entries
        assert str(fake_home / ".bun" / "bin") in entries
        # Verify v22.14.0 precedes v8.17.0
        idx_22 = entries.index(str(fake_home / ".nvm" / "versions" / "node" / "v22.14.0" / "bin"))
        idx_8 = entries.index(str(fake_home / ".nvm" / "versions" / "node" / "v8.17.0" / "bin"))
        assert idx_22 < idx_8


# ---------------------------------------------------------------------------
# Part B2: All 4 Hardened Local AI CLI Adapters
# ---------------------------------------------------------------------------


class TestLocalAICLIAdapters:
    def test_opencode_cli_client_execution_jsonl_parsing_cache_and_error(self, monkeypatch):
        observed_calls: List[Dict[str, Any]] = []

        def fake_run(cmd, *, cwd, stdin, capture_output, text, timeout, env, check):
            observed_calls.append(
                {
                    "cmd": list(cmd),
                    "cwd": cwd,
                    "cwd_contents": os.listdir(cwd),
                    "stdin": stdin,
                    "timeout": timeout,
                    "env": dict(env),
                }
            )
            jsonl = "\n".join(
                [
                    json.dumps({"type": "step_start", "part": {"type": "step-start"}}),
                    json.dumps({"type": "text", "part": {"type": "text", "text": "Calm "}}),
                    json.dumps({"type": "text", "part": {"type": "text", "text": "budget plan"}}),
                    json.dumps(
                        {
                            "type": "step_finish",
                            "part": {
                                "type": "step-finish",
                                "cost": 0.00125,
                                "model": "opencode-go/glm-5.3-flash",
                                "tokens": {
                                    "input": 1200,
                                    "output": 140,
                                    "reasoning": 0,
                                    "cache": {"read": 800, "write": 0},
                                },
                            },
                        }
                    ),
                ]
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=jsonl, stderr="")

        monkeypatch.setattr("chronox_finance.ai.local_cli_clients.resolve_cli_binary", lambda n: "/home/u/.opencode/bin/opencode")
        monkeypatch.setattr("subprocess.run", fake_run)
        monkeypatch.setenv("CHRONOX_FINANCE_SECRET_KEY", "super-vault-secret")

        client = OpenCodeCLIClient()
        assert isinstance(client, LLMClient)
        assert client.provider_name == "opencode_cli"
        assert client.is_configured is True

        res1 = client.chat_completion("System coach", "Check my SSN 123-45-6789 balance")
        res2 = client.chat_completion("System coach", "Check my SSN 123-45-6789 balance")

        assert len(observed_calls) == 1  # 2nd call hit TTL cache!
        call = observed_calls[0]
        assert call["cmd"][:5] == ["/home/u/.opencode/bin/opencode", "run", "--format", "json", "--archived"]
        assert "--model" not in call["cmd"]  # Default model omitted
        assert call["stdin"] is subprocess.DEVNULL
        assert call["cwd_contents"] == []
        assert "CHRONOX_FINANCE_SECRET_KEY" not in call["env"]
        assert "123-45-6789" not in call["cmd"][-1]

        assert res1.content == "Calm budget plan"
        assert res1.provider == "opencode_cli"
        assert res1.model == "opencode-go/glm-5.3-flash"
        assert res1.input_tokens == 1200
        assert res1.output_tokens == 140
        assert res1.cached_tokens == 800
        assert res1.cost_microdollars == 1250
        assert res1.cached is False
        assert res2.cached is True
        assert res2.content == "Calm budget plan"

        # Explicit model override + type=="error" rejection
        def fake_error_run(cmd, *, cwd, stdin, capture_output, text, timeout, env, check):
            assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "opencode-go/glm-5.3-flash"
            err_jsonl = json.dumps({"type": "error", "error": {"data": {"message": "401 Unauthorized API key"}}})
            return subprocess.CompletedProcess(cmd, 0, stdout=err_jsonl, stderr="")

        monkeypatch.setattr("subprocess.run", fake_error_run)
        with pytest.raises(OpenCodeCLIError) as exc_info:
            client.chat_completion("sys", "usr", model="opencode-go/glm-5.3-flash", use_cache=False)
        assert isinstance(exc_info.value, LLMProviderError)
        assert "401 Unauthorized" in str(exc_info.value)

    def test_claude_cli_client_stdin_piping_json_parsing_and_error_handling(self, monkeypatch):
        observed: Dict[str, Any] = {}

        def fake_run(cmd, *, cwd, input, capture_output, text, timeout, env, check):
            observed["cmd"] = list(cmd)
            observed["cwd_contents"] = os.listdir(cwd)
            observed["input"] = input
            observed["env"] = dict(env)
            payload = {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": '{"action": "keep_calm"}',
                "total_cost_usd": 0.0021,
                "model": "claude-sonnet-4-5",
                "usage": {
                    "input_tokens": 400,
                    "cache_read_input_tokens": 1100,
                    "output_tokens": 65,
                },
            }
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

        monkeypatch.setattr("chronox_finance.ai.local_cli_clients.resolve_cli_binary", lambda n: "/home/u/.claude/local/claude")
        monkeypatch.setattr("subprocess.run", fake_run)
        monkeypatch.setenv("PLAID_SECRET", "plaid-never-leak")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-safe-key")

        client = ClaudeCLIClient(model="sonnet")
        assert isinstance(client, LLMClient)
        assert client.provider_name == "claude_cli"
        assert client.is_configured is True

        res = client.chat_completion("System", "Pay bob@example.com with sk-proj-secret12345678901234", json_mode=True)
        assert observed["cmd"] == [
            "/home/u/.claude/local/claude",
            "-p",
            "--output-format",
            "json",
            "--permission-mode",
            "dontAsk",
            "--tools",
            "",
            "--no-session-persistence",
            "--model",
            "sonnet",
        ]
        assert observed["cwd_contents"] == []
        assert "PLAID_SECRET" not in observed["env"]
        assert observed["env"]["ANTHROPIC_API_KEY"] == "sk-ant-safe-key"
        # Prompt piped via stdin (redacted!)
        assert "USER REQUEST:" in observed["input"]
        assert "bob@example.com" not in observed["input"]
        assert "sk-proj-secret" not in observed["input"]

        assert res.content == '{"action": "keep_calm"}'
        assert res.provider == "claude_cli"
        assert res.input_tokens == 1500
        assert res.cached_tokens == 1100
        assert res.output_tokens == 65
        assert res.cost_microdollars == 2100

        # is_error: true MUST raise ClaudeCLIError
        def fake_is_error(cmd, **kwargs):
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps({"is_error": True, "result": "Not logged in. Please run `claude login`."}),
                stderr="",
            )

        monkeypatch.setattr("subprocess.run", fake_is_error)
        with pytest.raises(ClaudeCLIError) as exc_info:
            client.chat_completion("s", "u", use_cache=False)
        assert "Not logged in" in str(exc_info.value)

    def test_codex_cli_default_model_empty_flags_stdin_error_rejection_and_deep_probe(self, monkeypatch):
        assert DEFAULT_CODEX_CLI_MODEL == ""

        # 1. `_parse_codex_cli_json_output` MUST reject error / turn.failed / item.type==error
        err_jsonl = "\n".join(
            [
                json.dumps({"type": "item.completed", "item": {"type": "error", "message": "Model metadata not found"}}),
                json.dumps({"type": "error", "message": "401 Unauthorized: refresh token already used"}),
                json.dumps({"type": "turn.failed", "error": {"message": "401 Unauthorized: refresh token already used"}}),
            ]
        )
        with pytest.raises(CodexCLIError) as err_exc:
            _parse_codex_cli_json_output(err_jsonl)
        assert isinstance(err_exc.value, LLMProviderError)
        assert "401 Unauthorized" in str(err_exc.value)

        # If a non-fatal error item precedes a valid agent_message, only the agent_message is returned
        mixed_jsonl = "\n".join(
            [
                json.dumps({"type": "item.completed", "item": {"type": "error", "message": "non-fatal warning"}}),
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Verified answer"}}),
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 200, "output_tokens": 25}}),
            ]
        )
        content, in_t, out_t, _ = _parse_codex_cli_json_output(mixed_jsonl)
        assert content == "Verified answer"
        assert in_t == 200 and out_t == 25

        # 2. Default model="" omits --model, includes --ephemeral / low reasoning / mcp_servers={},
        #    and pipes >8000 char prompts via stdin + "-"
        observed: List[Dict[str, Any]] = []

        def fake_run(cmd, **kwargs):
            observed.append({"cmd": list(cmd), "kwargs": dict(kwargs)})
            if "--version" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="codex-cli 0.167.2\n", stderr="")
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps(
                    {"type": "item.completed", "item": {"type": "agent_message", "text": "OK"}}
                ),
                stderr="",
            )

        monkeypatch.setattr("chronox_finance.ai.codex_client.resolve_cli_binary", lambda n: "/usr/bin/codex")
        monkeypatch.setattr("subprocess.run", fake_run)

        cli = CodexCLIClient(enabled=True)
        assert cli.model == ""
        big_prompt = "budget item " * 900  # > 8000 chars -> triggers stdin + "-"
        res = cli.chat_completion("sys", big_prompt, use_cache=False)
        assert res.content == "OK"
        cmd = observed[-1]["cmd"]
        assert "--ephemeral" in cmd
        assert 'model_reasoning_effort="low"' in cmd
        assert "mcp_servers={}" in cmd
        assert "--model" not in cmd
        assert cmd[-1] == "-"
        assert "input" in observed[-1]["kwargs"]
        assert len(observed[-1]["kwargs"]["input"]) > 8000

        # 3. `test_connection(deep=True)` catches expired OAuth 401 and caches for 5 minutes!
        probe_calls = {"exec": 0, "ver": 0}

        def fake_expired_auth_run(cmd, **kwargs):
            if "--version" in cmd:
                probe_calls["ver"] += 1
                return subprocess.CompletedProcess(cmd, 0, stdout="codex-cli 0.167.2\n", stderr="")
            probe_calls["exec"] += 1
            return subprocess.CompletedProcess(
                cmd,
                1,
                stdout=json.dumps({"type": "error", "message": "401 Unauthorized: refresh token already used"}),
                stderr="",
            )

        monkeypatch.setattr("subprocess.run", fake_expired_auth_run)
        cli2 = CodexCLIClient(enabled=True, probe_cache_ttl_seconds=300)
        p1 = cli2.test_connection(deep=True)
        p2 = cli2.test_connection(deep=True)
        assert p1["ok"] is False
        assert p1["auth_ready"] is False
        assert "401 Unauthorized" in p1["message"]
        assert p2["ok"] is False
        assert probe_calls["exec"] == 1  # Cached for 5 minutes!

    def test_agy_cli_client_flags_no_mode_plan_json_parsing_and_error(self, monkeypatch):
        observed: Dict[str, Any] = {}

        def fake_run(cmd, *, cwd, stdin, capture_output, text, timeout, env, check):
            observed["cmd"] = list(cmd)
            observed["cwd_contents"] = os.listdir(cwd)
            observed["stdin"] = stdin
            observed["env"] = dict(env)
            payload = {
                "status": "SUCCESS",
                "response": "OK\n",
                "usage": {
                    "input_tokens": 900,
                    "output_tokens": 12,
                    "thinking_tokens": 5,
                    "cache_read_tokens": 350,
                },
            }
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

        monkeypatch.setattr("chronox_finance.ai.local_cli_clients.resolve_cli_binary", lambda n: "/home/u/.local/bin/agy")
        monkeypatch.setattr("subprocess.run", fake_run)

        client = AntigravityCLIClient()
        assert isinstance(client, LLMClient)
        assert client.provider_name == "agy_cli"
        assert client.model == DEFAULT_AGY_CLI_MODEL == "gemini-3.8-flash-low"

        res = client.chat_completion("System", "Reply OK")
        cmd = observed["cmd"]
        assert cmd[0] == "/home/u/.local/bin/agy"
        assert cmd[1] == "-p"
        assert "--output-format" in cmd and cmd[cmd.index("--output-format") + 1] == "json"
        assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "gemini-3.8-flash-low"
        assert "--disable-slash-commands" in cmd
        assert "--sandbox" in cmd
        assert "--print-timeout" in cmd and cmd[cmd.index("--print-timeout") + 1] == "55s"
        # Critical rule: NEVER pass --mode plan alongside --disable-slash-commands
        assert "--mode" not in cmd
        assert observed["stdin"] is subprocess.DEVNULL
        assert observed["cwd_contents"] == []

        assert res.content == "OK"
        assert res.provider == "agy_cli"
        assert res.input_tokens == 900
        assert res.output_tokens == 12
        assert res.cached_tokens == 350

        # Non-SUCCESS status raises AntigravityCLIError
        def fake_failed_agy(cmd, **kwargs):
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps({"status": "ERROR", "error": "Quota exceeded"}),
                stderr="",
            )

        monkeypatch.setattr("subprocess.run", fake_failed_agy)
        with pytest.raises(AntigravityCLIError) as exc_info:
            client.chat_completion("s", "u", use_cache=False)
        assert "Quota exceeded" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Part B3: LocalCLIRegistry Priority Order & 5-Min Auth Failover
# ---------------------------------------------------------------------------


class TestLocalCLIRegistryAndFailover:
    def test_priority_order_discovery_and_5min_auth_failover_skips_broken_codex(self, monkeypatch):
        assert LOCAL_CLI_PRIORITY == ("claude_cli", "opencode_cli", "codex_cli", "agy_cli")

        fake_now = {"t": 1000.0}
        installed_bins = {
            "claude": None,
            "opencode": None,
            "codex": "/usr/bin/codex",
            "agy": "/home/u/.local/bin/agy",
        }
        exec_counts = {"codex": 0, "agy": 0}

        def fake_resolve(name: str, **kwargs):
            return installed_bins.get(name)

        def fake_run(cmd, **kwargs):
            bin_path = cmd[0]
            if "--version" in cmd:
                if "codex" in bin_path:
                    return subprocess.CompletedProcess(cmd, 0, stdout="codex-cli 0.167.2\n", stderr="")
                if "agy" in bin_path:
                    return subprocess.CompletedProcess(cmd, 0, stdout="agy 1.5.15\n", stderr="")
                if "opencode" in bin_path:
                    return subprocess.CompletedProcess(cmd, 0, stdout="opencode 2.2.7\n", stderr="")
            if "codex" in bin_path:
                exec_counts["codex"] += 1
                return subprocess.CompletedProcess(
                    cmd,
                    1,
                    stdout=json.dumps({"type": "error", "message": "401 Unauthorized: refresh token already used"}),
                    stderr="",
                )
            if "agy" in bin_path:
                exec_counts["agy"] += 1
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout=json.dumps({"status": "SUCCESS", "response": "OK", "usage": {"input_tokens": 10, "output_tokens": 2}}),
                    stderr="",
                )
            raise AssertionError(f"Unexpected cmd: {cmd}")

        monkeypatch.setattr("chronox_finance.ai.codex_client.resolve_cli_binary", fake_resolve)
        monkeypatch.setattr("chronox_finance.ai.local_cli_clients.resolve_cli_binary", fake_resolve)
        monkeypatch.setattr("subprocess.run", fake_run)

        registry = LocalCLIRegistry(
            auth_failure_ttl_seconds=300.0,
            clock=lambda: fake_now["t"],
        )

        # 1. Fast discovery (no auth probe) returns all 4 in strict priority order
        detected = discover_local_ai_clis(run_auth_probe=False, registry=registry)
        assert [d["provider_id"] for d in detected] == ["claude_cli", "opencode_cli", "codex_cli", "agy_cli"]
        by_id = {d["provider_id"]: d for d in detected}
        assert by_id["claude_cli"]["installed"] is False
        assert by_id["opencode_cli"]["installed"] is False
        assert by_id["codex_cli"]["installed"] is True and by_id["codex_cli"]["version"] == "codex-cli 0.167.2"
        assert by_id["agy_cli"]["installed"] is True and by_id["agy_cli"]["version"] == "agy 1.5.15"

        # 2. Deep selection probes codex (fails 401), records 5m cooldown, and fails over cleanly to agy!
        selected = registry.select_best_available_cli(run_auth_probe=True)
        assert selected is not None
        assert selected.provider_name == "agy_cli"
        assert registry.is_auth_failed("codex_cli") is True
        assert exec_counts["codex"] == 1
        assert exec_counts["agy"] == 1

        # 3. Subsequent selection within 5 minutes skips codex WITHOUT re-running codex subprocess!
        fake_now["t"] += 120.0
        selected_fast = registry.select_best_available_cli(run_auth_probe=False)
        assert selected_fast is not None
        assert selected_fast.provider_name == "agy_cli"
        failover_chain = registry.get_failover_clients()
        assert [c.provider_name for c in failover_chain] == ["agy_cli"]
        assert exec_counts["codex"] == 1

        # 4. After 5-minute TTL expires, codex cooldown clears automatically
        fake_now["t"] += 200.0
        assert registry.is_auth_failed("codex_cli") is False

        # 5. Simulate this Linux host's installed CLIs (`opencode` + `codex` + `agy`):
        #    priority selects `opencode_cli` ahead of `codex_cli` and `agy_cli`!
        installed_bins["opencode"] = "/home/u/.opencode/bin/opencode"
        best_host = registry.select_best_available_cli(run_auth_probe=False)
        assert best_host is not None
        assert best_host.provider_name == "opencode_cli"

    def test_runtime_auth_failure_during_chat_completion_triggers_registry_cooldown(self, monkeypatch):
        installed_bins = {
            "claude": "/home/u/.claude/local/claude",
            "opencode": "/home/u/.opencode/bin/opencode",
            "codex": None,
            "agy": None,
        }

        monkeypatch.setattr("chronox_finance.ai.codex_client.resolve_cli_binary", lambda n, **kw: installed_bins.get(n))
        monkeypatch.setattr(
            "chronox_finance.ai.local_cli_clients.resolve_cli_binary", lambda n, **kw: installed_bins.get(n)
        )

        def fake_claude_401(cmd, **kwargs):
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps({"is_error": True, "result": "401 Unauthorized: please log in"}),
                stderr="",
            )

        monkeypatch.setattr("subprocess.run", fake_claude_401)
        reg = LocalCLIRegistry()
        # Initially claude_cli is #1 priority
        first = reg.select_best_available_cli(run_auth_probe=False)
        assert first is not None and first.provider_name == "claude_cli"

        # Runtime auth failure triggers automatic 5-min cooldown via callback
        with pytest.raises(ClaudeCLIError):
            first.chat_completion("sys", "usr", use_cache=False)

        assert reg.is_auth_failed("claude_cli") is True
        # Next call seamlessly picks opencode_cli!
        nxt = reg.select_best_available_cli(run_auth_probe=False)
        assert nxt is not None and nxt.provider_name == "opencode_cli"
