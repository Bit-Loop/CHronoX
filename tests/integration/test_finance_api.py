"""End-to-end integration & security tests for ChronoX Finance FastAPI server."""

from __future__ import annotations

import json

import httpx
from fastapi.testclient import TestClient

from chronox_finance.api import create_app
from chronox_finance.security import CSRF_HEADER_NAME
from chronox_finance.service import FinanceService
from tests.fixtures.seed_workspace import seed_workspace


def _make_client(tmp_path, mock_transport=None, *, enforce_vault_lock: bool = False):
    db_file = str(tmp_path / "finance.sqlite3")
    svc = FinanceService(
        db_path=db_file,
        secret_store_path=tmp_path / "secrets.enc",
        opencode_transport=mock_transport,
    )
    app = create_app(db_path=db_file, service=svc, enforce_vault_lock=enforce_vault_lock)
    return TestClient(app), svc


class TestFinanceEndToEndAPI:
    def test_security_headers_and_csrf_origin_block(self, tmp_path):
        client, svc = _make_client(tmp_path, enforce_vault_lock=True)
        try:
            r = client.get("/api/v1/health")
            assert r.status_code == 200
            assert r.headers["X-Content-Type-Options"] == "nosniff"
            assert r.headers["X-Frame-Options"] == "DENY"
            assert "script-src 'self'" in r.headers["Content-Security-Policy"]

            # Cross-origin POST must be blocked (DNS-rebinding / Origin protection)
            evil = client.post(
                "/api/v1/quick-add",
                json={"text": "coffee 5"},
                headers={"Origin": "https://evil.example.com", "Host": "127.0.0.1:8742"},
            )
            assert evil.status_code == 403

            # Non-loopback Host header must be blocked (DNS-rebinding protection)
            bad_host = client.get("/api/v1/health", headers={"Host": "attacker.example.com:8742"})
            assert bad_host.status_code == 403

            # Missing X-ChronoX-CSRF header on mutating route must be blocked
            no_csrf = client.post("/api/v1/quick-add", json={"text": "coffee 5"})
            assert no_csrf.status_code == 403
        finally:
            svc.close()

    def test_full_adhd_workflow_seed_quickadd_impulse_reset_csv_and_backup(self, tmp_path):
        client, svc = _make_client(tmp_path, enforce_vault_lock=False)
        try:
            # 1. Load Web UI
            home = client.get("/")
            assert home.status_code == 200
            assert "ChronoX Finance" in home.text

            # 2. Demo endpoint is purged; seed workspace via test fixture
            assert client.post("/api/demo/load").status_code == 404
            assert client.post("/api/v1/demo/load").status_code == 404
            seed_workspace(svc)
            dash = client.get("/api/v1/dashboard").json()
            assert dash["safe_to_spend"]["total_liquid_cash_cents"] > 0
            assert len(dash["jars"]) == 5
            assert len(dash["upcoming_bills"]) >= 5
            assert any(b["price_hike_cents"] > 0 for b in dash["upcoming_bills"])

            # 3. Zero-friction Quick-Add expense & impulse
            r_tx = client.post("/api/v1/quick-add", json={"text": "starbucks coffee 7.25"})
            assert r_tx.status_code == 200
            assert r_tx.json()["kind"] == "transaction"

            r_imp = client.post("/api/v1/quick-add", json={"text": "impulse $150 desk lamp 48h"})
            assert r_imp.status_code == 200
            assert r_imp.json()["kind"] == "impulse"
            imp_id = r_imp.json()["impulse"]["id"]

            # 4. Skip the impulse -> earns XP & records saved dollars
            r_skip = client.post(f"/api/v1/impulses/{imp_id}/decide", json={"decision": "skip"})
            assert r_skip.status_code == 200
            assert r_skip.json()["saved_cents"] == 15_000

            # 5. CSV Import deduplication (import twice -> 2nd import skips all duplicates!)
            csv_payload = "Date,Description,Amount\n2026-11-18,TRADER JOES,-55.40\n2026-11-19,SHELL OIL,-42.10\n"
            r_csv1 = client.post("/api/v1/import/csv-text", json={"csv_text": csv_payload})
            assert r_csv1.status_code == 200
            assert r_csv1.json()["imported_count"] == 2
            assert r_csv1.json()["duplicate_count"] == 0

            r_csv2 = client.post("/api/v1/import/csv-text", json={"csv_text": csv_payload})
            assert r_csv2.json()["imported_count"] == 0
            assert r_csv2.json()["duplicate_count"] == 2

            # 6. Weekly Reset completion
            r_reset = client.post("/api/v1/weekly-reset/complete", json={"apply_rebalances": True})
            assert r_reset.status_code == 200
            assert r_reset.json()["adhd_stats"]["weekly_reset_streak"] >= 3

            # 7. ChronoX Trading Capital Guardrail endpoint
            r_cap = client.get("/api/v1/chronox/safe-trading-capital")
            assert r_cap.status_code == 200
            assert r_cap.json()["guardrail_active"] is True

            # 8. Export & restore backup cleanly
            backup = client.get("/api/v1/backup/export").json()
            assert backup["schema_version"] >= 2
            assert "passcode_hash" not in backup["settings"]

            r_restore = client.post(
                "/api/v1/backup/import",
                files={"file": ("backup.json", json.dumps(backup).encode("utf-8"), "application/json")},
            )
            assert r_restore.status_code == 200
            assert r_restore.json()["restored"] is True
        finally:
            svc.close()

    def test_opencode_go_key_never_leaks_and_connects(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/models"):
                return httpx.Response(200, json={"data": [{"id": "glm-5.3-flash", "owned_by": "zai"}]})
            return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

        client, svc = _make_client(tmp_path, mock_transport=httpx.MockTransport(handler), enforce_vault_lock=False)
        try:
            r_set = client.patch(
                "/api/v1/settings",
                json={"opencode_api_key": "sk-opencode-live-secret-9876", "opencode_model": "glm-5.3-flash"},
            )
            assert r_set.status_code == 200
            body = r_set.json()
            assert body["opencode_key_configured"] is True
            assert "sk-opencode-live-secret-9876" not in json.dumps(body)
            assert body["opencode_key_masked"].endswith("9876")

            r_test = client.post("/api/v1/ai/test-connection")
            assert r_test.status_code == 200
            assert r_test.json()["ok"] is True
        finally:
            svc.close()

    def test_mandatory_vault_lock_protects_all_private_routes(self, tmp_path):
        client, svc = _make_client(tmp_path, enforce_vault_lock=True)
        try:
            # Fail-closed before setup -> 401 with state=needs_setup
            r_uninit = client.get("/api/v1/dashboard")
            assert r_uninit.status_code == 401
            assert r_uninit.json()["state"] == "needs_setup"

            # Setup envelope-encrypted vault -> returns 8-group Recovery Key & CSRF token
            r_setup = client.post("/api/v1/auth/setup", json={"password": "calm-adhd-vault-2026"})
            assert r_setup.status_code == 200
            setup_data = r_setup.json()
            csrf = setup_data["csrf_token"]
            assert len(setup_data["recovery_groups"]) == 8
            assert client.get("/api/v1/dashboard").status_code == 200

            # Mutating route without CSRF -> 403
            assert client.post("/api/v1/auth/logout").status_code == 403

            # Logout clears cookie & locks vault -> dashboard must return 401 locked
            r_out = client.post("/api/v1/auth/logout", headers={CSRF_HEADER_NAME: csrf})
            assert r_out.status_code == 200
            r_locked = client.get("/api/v1/dashboard")
            assert r_locked.status_code == 401
            assert r_locked.json()["state"] == "locked"

            # Wrong password -> 401
            assert client.post("/api/v1/auth/login", json={"password": "wrong-pass"}).status_code == 401

            # Right password -> sets HttpOnly cookie & fresh CSRF -> dashboard succeeds!
            r_login = client.post("/api/v1/auth/login", json={"password": "calm-adhd-vault-2026"})
            assert r_login.status_code == 200
            assert r_login.json()["csrf_token"]
            assert client.get("/api/v1/dashboard").status_code == 200
        finally:
            svc.close()
