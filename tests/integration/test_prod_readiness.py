"""Production-readiness end-to-end verification tests for ChronoX Finance.

Covers:
- All 3 Intelligence Modes (`deterministic_only`, `hybrid`, `ai_standalone`) via HTTP API
- Google Gemini optional provider lifecycle, header-only `x-goog-api-key` auth, SSRF, and zero secret leaks
- `/api/quant/forecast`, `PATCH /api/transactions/{id}`, public `/api/health`, 5 MB upload caps (413)
- Global structured JSON error handling (400 / 404 / 422 / 500 with zero stack traces)
- Safe additive SQLite migration on legacy pre-v1.1 databases
- CLI production smoke suite (`demo`, `quant`, `credit`, `goal`, `--mode`, `--provider`)
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date
from typing import Any, List, Optional, Tuple

import httpx
import pytest
from fastapi.testclient import TestClient

from chronox_finance.api import MAX_UPLOAD_BYTES, create_app
from chronox_finance.cli import run_cli
from chronox_finance.models import utc_now_iso
from chronox_finance.security import CSRF_HEADER_NAME
from chronox_finance.service import FinanceService
from tests.fixtures.seed_workspace import seed_workspace


def _clear_ai_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "OPENCODE_GO_API_KEY",
        "OPENCODE_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
        "CHRONOX_INTELLIGENCE_MODE",
        "CHRONOX_AI_PROVIDER",
    ):
        monkeypatch.delenv(var, raising=False)


def _make_client(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    opencode_transport: Optional[httpx.BaseTransport] = None,
    gemini_transport: Optional[httpx.BaseTransport] = None,
    raise_server_exceptions: bool = True,
    enforce_vault_lock: bool = False,
) -> Tuple[TestClient, FinanceService, str]:
    _clear_ai_env(monkeypatch)
    db_file = str(tmp_path / "prod_finance.sqlite3")
    svc = FinanceService(
        db_path=db_file,
        secret_store_path=tmp_path / "secrets.enc",
        opencode_transport=opencode_transport,
        gemini_transport=gemini_transport,
    )
    app = create_app(db_path=db_file, service=svc, enforce_vault_lock=enforce_vault_lock)
    return TestClient(app, raise_server_exceptions=raise_server_exceptions), svc, db_file


class TestIntelligenceModesEndToEnd:
    def test_deterministic_only_makes_zero_http_calls_and_returns_rich_quant(self, tmp_path, monkeypatch):
        http_calls: List[str] = []

        def forbid_http(request: httpx.Request) -> httpx.Response:
            http_calls.append(f"{request.method} {request.url}")
            raise AssertionError(f"Outbound HTTP forbidden in deterministic_only mode: {request.url}")

        transport = httpx.MockTransport(forbid_http)
        client, svc, _ = _make_client(
            tmp_path,
            monkeypatch,
            opencode_transport=transport,
            gemini_transport=transport,
        )
        try:
            seed_workspace(svc)
            # Even with BOTH cloud API keys configured, deterministic_only MUST make 0 outbound calls
            r_cfg = client.patch(
                "/api/settings/ai",
                json={
                    "intelligence_mode": "deterministic_only",
                    "ai_provider": "auto",
                    "opencode_api_key": "sk-opencode-should-not-call",
                    "gemini_api_key": "AIzaSyShouldNotCall1234",
                },
            )
            assert r_cfg.status_code == 200
            assert r_cfg.json()["intelligence_mode"] == "deterministic_only"

            r_sug = client.get("/api/ai/suggestions?refresh=true")
            assert r_sug.status_code == 200
            sug_body = r_sug.json()
            assert sug_body["source"] == "deterministic_quant"
            assert sug_body["intelligence_mode"] == "deterministic_only"
            assert sug_body["provider_used"] is None
            assert len(sug_body["suggestions"]) >= 1
            assert all(s["verified"] is True for s in sug_body["suggestions"])
            assert all(s["source"] == "deterministic_quant" for s in sug_body["suggestions"])

            r_ask = client.post(
                "/api/ai/ask",
                json={"question": "Can I afford a $1,000 laptop without hurting my credit score?"},
            )
            assert r_ask.status_code == 200
            ask_body = r_ask.json()
            assert ask_body["source"] == "deterministic_quant"
            assert ask_body["intelligence_mode"] == "deterministic_only"
            assert ask_body["provider_used"] is None
            assert ask_body["affordability_check"] is not None
            assert len(ask_body["answer"]) > 80

            assert http_calls == [], f"Unexpected outbound HTTP calls: {http_calls}"
        finally:
            svc.close()

    def test_hybrid_mode_combines_quant_and_llm_with_validation_and_auto_failover(self, tmp_path, monkeypatch):
        oc_calls: List[str] = []
        gem_calls: List[httpx.Request] = []

        # Primary OpenCode Go simulates temporary 503 -> Auto mode MUST seamlessly fail over to Gemini!
        def oc_fail(request: httpx.Request) -> httpx.Response:
            oc_calls.append(str(request.url))
            return httpx.Response(503, json={"error": "upstream unavailable"})

        def gem_ok(request: httpx.Request) -> httpx.Response:
            gem_calls.append(request)
            if request.url.path.endswith("/models"):
                return httpx.Response(
                    200,
                    json={
                        "models": [
                            {
                                "name": "models/gemini-2.5-flash",
                                "displayName": "Gemini 2.5 Flash",
                                "supportedGenerationMethods": ["generateContent"],
                            }
                        ]
                    },
                )
            body = json.loads(request.content.decode("utf-8"))
            gen_cfg = body.get("generationConfig", {})
            if gen_cfg.get("responseMimeType") == "application/json":
                # Mix 1 valid suggestion + 1 illegal 'bills' raid that SuggestionValidator MUST reject!
                payload = {
                    "suggestions": [
                        {
                            "title": "Confirm ChronoX Invest Cap",
                            "rationale": "Keep your trading allocation protected behind bills and buffer.",
                            "micro_step": "Tap Apply to confirm your current safe trading cap.",
                            "priority": "medium",
                            "action_type": "set_trading_cap",
                            "action_payload": {"safe_to_invest_cents": 15000},
                        },
                        {
                            "title": "Raid Bills Jar (Must Be Blocked)",
                            "rationale": "You are reckless and terrible with money.",
                            "micro_step": "Drain bills.",
                            "priority": "high",
                            "action_type": "rebalance_jars",
                            "action_payload": {"from_jar_id": 1, "to_jar_id": 3, "amount_cents": 50000},
                        },
                    ]
                }
                return httpx.Response(
                    200,
                    json={
                        "candidates": [
                            {
                                "content": {"parts": [{"text": json.dumps(payload)}]},
                                "finishReason": "STOP",
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {"text": "Internal thought scratchpad", "thought": True},
                                    {"text": "Based on your exact runway, you are in a strong position to pace this purchase calmly."},
                                ]
                            },
                            "finishReason": "STOP",
                        }
                    ]
                },
            )

        client, svc, _ = _make_client(
            tmp_path,
            monkeypatch,
            opencode_transport=httpx.MockTransport(oc_fail),
            gemini_transport=httpx.MockTransport(gem_ok),
        )
        try:
            seed_workspace(svc)
            r_cfg = client.patch(
                "/api/settings/ai",
                json={
                    "intelligence_mode": "hybrid",
                    "ai_provider": "auto",
                    "opencode_api_key": "sk-oc-primary-failover-1111",
                    "gemini_api_key": "AIzaSyGeminiFailover2222",
                    "gemini_model": "gemini-2.5-flash",
                },
            )
            assert r_cfg.status_code == 200
            assert r_cfg.json()["active_provider_resolved"] == "opencode_go"

            r_sug = client.get("/api/ai/suggestions?refresh=true")
            assert r_sug.status_code == 200
            sug = r_sug.json()
            assert sug["intelligence_mode"] == "hybrid"
            assert sug["source"] == "gemini"
            assert sug["provider_used"] == "gemini"
            assert len(oc_calls) >= 1
            assert len(gem_calls) >= 1
            # Validator MUST block the illegal 'bills' jar raid!
            titles = [s["title"] for s in sug["suggestions"]]
            assert "Raid Bills Jar (Must Be Blocked)" not in titles
            assert all(s["verified"] is True for s in sug["suggestions"])

            # Ask Coach in hybrid mode skips thought parts and uses Gemini cleanly
            r_ask = client.post("/api/ai/ask", json={"question": "How is my cashflow looking this month?"})
            assert r_ask.status_code == 200
            ask = r_ask.json()
            assert ask["source"] == "gemini"
            assert ask["provider_used"] == "gemini"
            assert "Internal thought scratchpad" not in ask["answer"]
            assert "strong position" in ask["answer"]
        finally:
            svc.close()

    def test_ai_standalone_mode_rsd_filtering_safety_bounds_and_offline_fallback(self, tmp_path, monkeypatch):
        def gem_shaming(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            if body.get("generationConfig", {}).get("responseMimeType") == "application/json":
                payload = {
                    "suggestions": [
                        {
                            "title": "Build calm momentum",
                            "rationale": "Stop being irresponsible and careless; park big temptations for 48h.",
                            "micro_step": "Park one item in your Impulse Vault.",
                            "priority": "medium",
                            "action_type": "park_impulse",
                            "action_payload": {"item_name": "Desk Lamp", "price_cents": 8500},
                        },
                        {
                            "title": "Negative amount action must be blocked",
                            "rationale": "Bad payload",
                            "micro_step": "Noop",
                            "priority": "low",
                            "action_type": "boost_emergency_buffer",
                            "action_payload": {"emergency_buffer_cents": -5000},
                        },
                    ]
                }
                return httpx.Response(
                    200,
                    json={"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}, "finishReason": "STOP"}]},
                )
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "text": (
                                            "Don't be reckless or irresponsible! You failed your budget, "
                                            "so set aside $100 per paycheck instead."
                                        )
                                    }
                                ]
                            },
                            "finishReason": "STOP",
                        }
                    ]
                },
            )

        client, svc, _ = _make_client(
            tmp_path,
            monkeypatch,
            gemini_transport=httpx.MockTransport(gem_shaming),
        )
        try:
            seed_workspace(svc)

            # 1. Unconfigured in ai_standalone falls back gracefully with transparent notice
            client.patch("/api/settings/ai", json={"intelligence_mode": "ai_standalone"})
            r_unconf = client.post("/api/ai/ask", json={"question": "How do I save for a trip?"})
            assert r_unconf.status_code == 200
            unconf = r_unconf.json()
            assert unconf["intelligence_mode"] == "ai_standalone"
            assert unconf["source"] in ("deterministic_quant", "deterministic_local")
            assert unconf["provider_used"] is None
            assert len(unconf["answer"]) > 20

            # 2. Configured Gemini in ai_standalone -> transparent provenance + RSD shame filter
            client.patch(
                "/api/settings/ai",
                json={
                    "intelligence_mode": "ai_standalone",
                    "ai_provider": "gemini",
                    "gemini_api_key": "AIzaSyStandaloneTest9999",
                },
            )
            r_ask = client.post("/api/ai/ask", json={"question": "Any creative savings ideas?"})
            assert r_ask.status_code == 200
            ask = r_ask.json()
            assert ask["source"] == "ai_standalone"
            assert ask["intelligence_mode"] == "ai_standalone"
            assert ask["provider_used"] == "gemini"
            low = ask["answer"].lower()
            assert "reckless" not in low
            assert "irresponsible" not in low
            assert "you failed" not in low

            r_sug = client.get("/api/ai/suggestions?refresh=true")
            assert r_sug.status_code == 200
            sug = r_sug.json()
            assert sug["source"] == "ai_standalone"
            assert sug["provider_used"] == "gemini"
            assert all("irresponsible" not in s["rationale"].lower() for s in sug["suggestions"])
            assert all(s["title"] != "Negative amount action must be blocked" for s in sug["suggestions"])
        finally:
            svc.close()


class TestGeminiProviderConfigAndSecurity:
    def test_gemini_key_lifecycle_header_only_no_leaks_and_provider_switching(self, tmp_path, monkeypatch):
        captured_requests: List[httpx.Request] = []
        secret_gem = "AIzaSySuperSecretGeminiKey8844"
        secret_oc = "sk-opencode-super-secret-7733"

        def gem_handler(request: httpx.Request) -> httpx.Response:
            captured_requests.append(request)
            if request.url.path.endswith("/models"):
                return httpx.Response(
                    200,
                    json={
                        "models": [
                            {
                                "name": "models/gemini-2.5-flash",
                                "displayName": "Gemini 2.5 Flash",
                                "supportedGenerationMethods": ["generateContent"],
                            },
                            {
                                "name": "models/text-embedding-004",
                                "displayName": "Embedding Only",
                                "supportedGenerationMethods": ["embedContent"],
                            },
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={"candidates": [{"content": {"parts": [{"text": "OK"}]}, "finishReason": "STOP"}]},
            )

        def oc_handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/models"):
                return httpx.Response(200, json={"data": [{"id": "glm-5.3-flash", "owned_by": "zai"}]})
            return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

        client, svc, _ = _make_client(
            tmp_path,
            monkeypatch,
            opencode_transport=httpx.MockTransport(oc_handler),
            gemini_transport=httpx.MockTransport(gem_handler),
            enforce_vault_lock=True,
        )
        try:
            # Initialize mandatory envelope-encrypted vault & capture per-boot CSRF token
            r_setup = client.post("/api/v1/auth/setup", json={"password": "gemini-vault-pass-99"})
            assert r_setup.status_code == 200
            csrf_headers = {CSRF_HEADER_NAME: r_setup.json()["csrf_token"]}

            # 1. Save Gemini key via /api/v1/settings/ai
            r_save = client.patch(
                "/api/v1/settings/ai",
                json={
                    "gemini_api_key": secret_gem,
                    "gemini_model": "gemini-2.5-flash",
                    "ai_provider": "gemini",
                },
                headers=csrf_headers,
            )
            assert r_save.status_code == 200
            saved = r_save.json()
            assert saved["gemini_api_key_set"] is True
            assert saved["gemini_key_source"] == "saved"
            assert saved["gemini_key_masked"].endswith("8844")
            assert saved["active_provider_resolved"] == "gemini"
            assert secret_gem not in json.dumps(saved)

            # Verify 0600 permissions & AES-256-GCM encryption on local secrets.enc store (no plaintext!)
            secrets_file = tmp_path / "secrets.enc"
            assert secrets_file.is_file()
            assert (os.stat(secrets_file).st_mode & 0o777) == 0o600
            assert secret_gem not in secrets_file.read_text(encoding="utf-8")

            # 2. Verify plaintext key NEVER leaks in ANY GET endpoint or backup export!
            for ep in ("/api/v1/settings/ai", "/api/v1/settings", "/api/v1/dashboard", "/api/v1/health", "/api/v1/backup/export"):
                resp = client.get(ep)
                assert resp.status_code == 200, f"GET {ep} failed"
                raw_text = resp.text
                assert secret_gem not in raw_text, f"Gemini key leaked in {ep}!"

            # 3. Verify live Gemini model discovery & connection test use x-goog-api-key header ONLY (never query params!)
            r_models = client.get("/api/v1/ai/models?provider=gemini&refresh=true")
            assert r_models.status_code == 200
            models_body = r_models.json()
            assert models_body["live"] is True
            model_ids = [m["id"] for m in models_body["models"]]
            assert "gemini-2.5-flash" in model_ids
            assert "text-embedding-004" not in model_ids  # embed-only filtered out

            r_test = client.post("/api/v1/ai/test?provider=gemini", headers=csrf_headers)
            assert r_test.status_code == 200
            assert r_test.json()["ok"] is True

            assert len(captured_requests) >= 2
            for req in captured_requests:
                assert req.headers.get("x-goog-api-key") == secret_gem
                assert "key=" not in str(req.url)
                assert secret_gem not in str(req.url)

            # 4. Provider switching between opencode_go, gemini, and auto
            client.patch(
                "/api/v1/settings/ai",
                json={"opencode_api_key": secret_oc, "ai_provider": "opencode_go"},
                headers=csrf_headers,
            )
            assert client.get("/api/v1/settings/ai").json()["active_provider_resolved"] == "opencode_go"
            assert secret_oc not in client.get("/api/v1/settings/ai").text

            client.patch("/api/v1/settings/ai", json={"ai_provider": "gemini"}, headers=csrf_headers)
            assert client.get("/api/v1/settings/ai").json()["active_provider_resolved"] == "gemini"

            client.patch("/api/v1/settings/ai", json={"ai_provider": "auto"}, headers=csrf_headers)
            assert client.get("/api/v1/settings/ai").json()["active_provider_resolved"] == "opencode_go"

            # 5. Clear Gemini key -> gemini_api_key_set=False
            r_clr = client.patch(
                "/api/v1/settings/ai",
                json={"clear_gemini_key": True, "ai_provider": "gemini"},
                headers=csrf_headers,
            )
            assert r_clr.status_code == 200
            cleared = r_clr.json()
            assert cleared["gemini_api_key_set"] is False
            assert cleared["gemini_key_source"] == "none"
            assert cleared["active_provider_resolved"] == "none"

            # 6. SSRF rejection on gemini_base_url (both PATCH and backup restore)
            r_ssrf = client.patch(
                "/api/v1/settings/ai",
                json={"gemini_base_url": "http://169.254.169.254/v1beta"},
                headers=csrf_headers,
            )
            assert r_ssrf.status_code == 400
            backup = client.get("/api/v1/backup/export").json()
            backup["settings"]["gemini_base_url"] = "http://evil.example.com/v1beta"
            r_bkp = client.post(
                "/api/v1/backup/import",
                files={"file": ("bad_gem.json", json.dumps(backup).encode("utf-8"), "application/json")},
                headers=csrf_headers,
            )
            assert r_bkp.status_code == 400
        finally:
            svc.close()


class TestQuantForecastTransactionPatchHealthAndHardening:
    def test_quant_forecast_endpoint_clamping_dashboard_parity_and_cent_conservation(self, tmp_path, monkeypatch):
        client, svc, _ = _make_client(tmp_path, monkeypatch)
        try:
            seed_workspace(svc)
            dash = client.get("/api/v1/dashboard").json()
            assert dash["quant_forecast"] is not None
            assert dash["intelligence_mode"] == "hybrid"
            assert dash["active_ai_provider"] == "none"

            f30 = client.get("/api/v1/quant/forecast?days=30").json()
            assert f30["horizon_days"] == 30
            assert len(f30["daily_points"]) == 30
            assert f30["lowest_balance_cents"] == dash["quant_forecast"]["lowest_balance_cents"]
            assert f30["ending_balance_cents"] == dash["quant_forecast"]["ending_balance_cents"]
            assert f30["volatility"]["recommended_buffer_cents"] >= 0
            assert 0 <= f30["volatility"]["stability_score_0_100"] <= 100

            if f30["monthly_surplus_cents"] > 0:
                alloc_sum = sum(step["amount_cents"] for step in f30["optimal_allocations"])
                assert alloc_sum == f30["monthly_surplus_cents"]

            # Horizon clamping [7..90] in QuantMathEngine.build_forecast_report
            assert client.get("/api/v1/quant/forecast?days=60").json()["horizon_days"] == 60
            assert client.get("/api/v1/quant/forecast?days=2").json()["horizon_days"] == 7
            assert client.get("/api/v1/quant/forecast?days=365").json()["horizon_days"] == 90
        finally:
            svc.close()

    def test_patch_transaction_updates_atomically_and_adjusts_account_balance(self, tmp_path, monkeypatch):
        client, svc, _ = _make_client(tmp_path, monkeypatch)
        try:
            acct = client.post(
                "/api/accounts",
                json={"name": "Main Checking", "account_type": "checking", "balance_cents": 100_000, "is_liquid": True},
            ).json()
            acct_id = acct["id"]
            jars = {j["slug"]: j["id"] for j in client.get("/api/dashboard").json()["jars"]}

            tx = client.post(
                "/api/transactions",
                json={
                    "account_id": acct_id,
                    "jar_id": jars["daily"],
                    "tx_type": "expense",
                    "amount_cents": 2_500,
                    "merchant": "Corner Cafe",
                    "memo": "morning latte",
                    "tx_date": date.today().isoformat(),
                },
            ).json()
            tx_id = tx["id"]
            # Balance dropped $1000 -> $975.00
            assert svc.storage.get_account(acct_id)["balance_cents"] == 97_500

            # Edit amount ($25 -> $40), jar (daily -> fun), merchant, memo
            r_patch = client.patch(
                f"/api/transactions/{tx_id}",
                json={
                    "merchant": "Corner Cafe Roasters",
                    "amount_cents": 4_000,
                    "jar_id": jars["fun"],
                    "memo": "latte + beans",
                    "learn_merchant_rule": True,
                },
            )
            assert r_patch.status_code == 200
            updated = r_patch.json()
            assert updated["merchant"] == "Corner Cafe Roasters"
            assert updated["amount_cents"] == 4_000
            assert updated["jar_id"] == jars["fun"]
            assert updated["memo"] == "latte + beans"
            # Account balance delta: -1500 cents -> $960.00
            assert svc.storage.get_account(acct_id)["balance_cents"] == 96_000

            # Flip expense -> income (+$40 instead of -$40 => +$80 delta => $1040.00)
            r_flip = client.patch(f"/api/transactions/{tx_id}", json={"tx_type": "income"})
            assert r_flip.status_code == 200
            assert svc.storage.get_account(acct_id)["balance_cents"] == 104_000

            # Invalid jar_id -> clean 400; missing tx -> clean 404
            assert client.patch(f"/api/transactions/{tx_id}", json={"jar_id": 999999}).status_code == 400
            assert client.patch("/api/transactions/999999", json={"merchant": "Ghost"}).status_code == 404
        finally:
            svc.close()

    def test_health_endpoint_public_under_lock_and_zero_secrets(self, tmp_path, monkeypatch):
        client, svc, _ = _make_client(tmp_path, monkeypatch, enforce_vault_lock=True)
        try:
            r_setup = client.post("/api/v1/auth/setup", json={"password": "vault-lock-password-99"})
            assert r_setup.status_code == 200
            csrf_headers = {CSRF_HEADER_NAME: r_setup.json()["csrf_token"]}

            r_ai = client.patch(
                "/api/v1/settings/ai",
                json={"gemini_api_key": "AIzaSyHealthSecret1234", "ai_provider": "gemini"},
                headers=csrf_headers,
            )
            assert r_ai.status_code == 200
            assert client.get("/api/v1/health").json()["active_provider"] == "gemini"

            r_out = client.post("/api/v1/auth/logout", headers=csrf_headers)
            assert r_out.status_code == 200

            # Private routes locked (401), but /api/v1/health MUST stay public (200) and secret-free
            assert client.get("/api/v1/dashboard").status_code == 401
            r_health = client.get("/api/v1/health")
            assert r_health.status_code == 200
            h = r_health.json()
            assert h["status"] == "ok"
            assert h["db_ok"] is True
            assert h["auth_enabled"] is True
            assert h["vault_state"] == "locked"
            assert h["intelligence_mode"] == "hybrid"
            assert "AIzaSyHealthSecret1234" not in r_health.text
            assert "vault-lock-password-99" not in r_health.text
            assert "passcode_hash" not in r_health.text
        finally:
            svc.close()

    def test_upload_size_limit_returns_413_and_global_error_handlers_hide_tracebacks(self, tmp_path, monkeypatch):
        client, svc, _ = _make_client(tmp_path, monkeypatch, raise_server_exceptions=False)
        try:
            oversized = b"x" * (MAX_UPLOAD_BYTES + 128)
            for ep, fname, ctype in (
                ("/api/import/preview", "big.csv", "text/csv"),
                ("/api/import/statement", "big.csv", "text/csv"),
                ("/api/import/csv", "big.csv", "text/csv"),
                ("/api/backup/import", "big.json", "application/json"),
            ):
                r = client.post(ep, files={"file": (fname, oversized, ctype)})
                assert r.status_code == 413, f"Expected 413 on {ep}, got {r.status_code}"
                body = r.json()
                assert body["code"] == "http_413"
                assert "Traceback" not in r.text

            # 422 structured validation JSON
            r_422 = client.post("/api/transactions", json={"amount_cents": -50, "merchant": ""})
            assert r_422.status_code == 422
            b422 = r_422.json()
            assert b422["code"] == "validation_error"
            assert isinstance(b422["errors"], list) and len(b422["errors"]) >= 1
            assert "Traceback" not in r_422.text

            # 400 structured bad_request JSON for invalid provider
            r_400 = client.get("/api/ai/models?provider=not_a_provider")
            assert r_400.status_code == 400
            assert r_400.json()["code"] == "http_400"

            # Simulated unhandled 500 -> clean JSON, ZERO stack trace, security headers intact
            def boom(*_args: Any, **_kwargs: Any):
                raise RuntimeError("secret internal pointer 0xDEADBEEF")

            monkeypatch.setattr(svc, "get_quant_forecast", boom)
            r_500 = client.get("/api/quant/forecast")
            assert r_500.status_code == 500
            b500 = r_500.json()
            assert b500["code"] == "internal_error"
            assert "0xDEADBEEF" not in r_500.text
            assert "Traceback" not in r_500.text
            assert ".py" not in r_500.text
            assert r_500.headers.get("X-Content-Type-Options") == "nosniff"
            assert r_500.headers.get("Cache-Control") == "no-store"
        finally:
            svc.close()

    def test_additive_migration_upgrades_legacy_sqlite_db_without_data_loss(self, tmp_path, monkeypatch):
        _clear_ai_env(monkeypatch)
        legacy_db = str(tmp_path / "legacy_v1.sqlite3")
        now = utc_now_iso()

        # Construct a pre-v1.1 SQLite DB missing intelligence_mode, ai_provider, gemini_*, last_rollover_ym
        conn = sqlite3.connect(legacy_db)
        conn.executescript(
            f"""
            CREATE TABLE profile_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                currency TEXT NOT NULL DEFAULT 'USD',
                hourly_wage_cents INTEGER NOT NULL DEFAULT 3000,
                emergency_buffer_cents INTEGER NOT NULL DEFAULT 50000,
                default_cooling_hours INTEGER NOT NULL DEFAULT 48,
                payday_cadence_days INTEGER NOT NULL DEFAULT 14,
                next_payday_date TEXT,
                expected_paycheck_cents INTEGER NOT NULL DEFAULT 220000,
                calm_mode INTEGER NOT NULL DEFAULT 1,
                auto_rebalance_enabled INTEGER NOT NULL DEFAULT 1,
                opencode_base_url TEXT NOT NULL DEFAULT 'https://opencode.ai/zen/go/v1',
                opencode_model TEXT NOT NULL DEFAULT 'glm-5.3-flash',
                opencode_protocol TEXT NOT NULL DEFAULT 'auto',
                opencode_max_daily_requests INTEGER NOT NULL DEFAULT 60,
                passcode_hash TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            INSERT INTO profile_settings (id, hourly_wage_cents, emergency_buffer_cents, updated_at)
            VALUES (1, 4500, 75000, '{now}');

            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                account_type TEXT NOT NULL,
                balance_cents INTEGER NOT NULL DEFAULT 0,
                institution TEXT NOT NULL DEFAULT '',
                is_liquid INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO accounts (name, account_type, balance_cents, institution, is_liquid, created_at, updated_at)
            VALUES ('Legacy Checking', 'checking', 312500, 'OldBank', 1, '{now}', '{now}');
            """
        )
        conn.commit()
        conn.close()

        # Open with upgraded FinanceService -> additive migration runs cleanly with zero data loss!
        svc = FinanceService(db_path=legacy_db, secret_store_path=tmp_path / "secrets.json")
        try:
            settings = svc.get_settings()
            assert settings.hourly_wage_cents == 4500
            assert settings.emergency_buffer_cents == 75_000
            assert settings.intelligence_mode.value == "hybrid"
            assert settings.ai_provider.value == "auto"
            assert settings.gemini_model == "gemini-2.5-flash"

            accts = svc.storage.list_accounts()
            assert any(a["name"] == "Legacy Checking" and a["balance_cents"] == 312_500 for a in accts)

            dash = svc.get_dashboard()
            assert dash.quant_forecast is not None
            assert dash.safe_to_spend.total_liquid_cash_cents == 312_500
        finally:
            svc.close()


class TestCLIProductionSmoke:
    def test_cli_seeded_quant_credit_goal_portfolio_usage_and_deterministic_mode(self, tmp_path, monkeypatch, capsys):
        _clear_ai_env(monkeypatch)
        db_file = str(tmp_path / "cli_prod.sqlite3")

        svc = FinanceService(db_path=db_file, secret_store_path=tmp_path / "secrets.enc")
        try:
            seed_workspace(svc)
        finally:
            svc.close()

        assert run_cli(["--db", db_file, "quant", "--days", "30"]) == 0
        assert run_cli(["--db", db_file, "credit", "--extra", "300"]) == 0
        assert run_cli(["--db", db_file, "goal", "Studio Display", "1000"]) == 0
        assert run_cli(["--db", db_file, "portfolio"]) == 0
        assert run_cli(["--db", db_file, "usage", "--days", "30"]) == 0
        assert run_cli(["--db", db_file, "--mode", "deterministic_only", "suggest"]) == 0
        assert run_cli(["--db", db_file, "--mode", "deterministic_only", "ask", "How is my 30-day cashflow runway?"]) == 0

        out = capsys.readouterr().out
        assert "CHRONOX QUANT FORECAST" in out
        assert "OPTIMAL SURPLUS ALLOCATION" in out
        assert "[CREDIT SIMULATOR]" in out
        assert "[GOAL PLAN] Studio Display" in out
        assert "CHRONOX PORTFOLIO" in out
        assert "CHRONOX AI USAGE" in out
        assert "Verified Local Engine | Quant Math" in out
