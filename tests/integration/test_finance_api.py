"""End-to-end integration & security tests for ChronoX Finance FastAPI server."""

from __future__ import annotations

import json

import httpx
from fastapi.testclient import TestClient

from chronox_finance.api import create_app
from chronox_finance.service import FinanceService


def _make_client(tmp_path, mock_transport=None):
    db_file = str(tmp_path / "finance.sqlite3")
    svc = FinanceService(
        db_path=db_file,
        secret_store_path=tmp_path / "secrets.json",
        opencode_transport=mock_transport,
    )
    app = create_app(db_path=db_file, service=svc)
    return TestClient(app), svc


class TestFinanceEndToEndAPI:
    def test_security_headers_and_csrf_origin_block(self, tmp_path):
        client, svc = _make_client(tmp_path)
        try:
            r = client.get("/api/health")
            assert r.status_code == 200
            assert r.headers["X-Content-Type-Options"] == "nosniff"
            assert r.headers["X-Frame-Options"] == "DENY"
            assert "script-src 'self'" in r.headers["Content-Security-Policy"]

            # Cross-origin POST must be blocked (CSRF protection)
            evil = client.post(
                "/api/quick-add",
                json={"text": "coffee 5"},
                headers={"Origin": "https://evil.example.com", "Host": "127.0.0.1:8742"},
            )
            assert evil.status_code == 403
        finally:
            svc.close()

    def test_full_adhd_workflow_demo_quickadd_impulse_reset_csv_and_backup(self, tmp_path):
        client, svc = _make_client(tmp_path)
        try:
            # 1. Load Web UI
            home = client.get("/")
            assert home.status_code == 200
            assert "ChronoX Finance" in home.text

            # 2. Seed realistic demo workspace
            r_demo = client.post("/api/demo/load")
            assert r_demo.status_code == 200
            dash = client.get("/api/dashboard").json()
            assert dash["safe_to_spend"]["total_liquid_cash_cents"] > 0
            assert len(dash["jars"]) == 5
            assert len(dash["upcoming_bills"]) >= 5
            assert any(b["price_hike_cents"] > 0 for b in dash["upcoming_bills"])

            # 3. Zero-friction Quick-Add expense & impulse
            r_tx = client.post("/api/quick-add", json={"text": "starbucks coffee 7.25"})
            assert r_tx.status_code == 200
            assert r_tx.json()["kind"] == "transaction"

            r_imp = client.post("/api/quick-add", json={"text": "impulse $150 desk lamp 48h"})
            assert r_imp.status_code == 200
            assert r_imp.json()["kind"] == "impulse"
            imp_id = r_imp.json()["impulse"]["id"]

            # 4. Skip the impulse -> earns XP & records saved dollars
            r_skip = client.post(f"/api/impulses/{imp_id}/decide", json={"decision": "skip"})
            assert r_skip.status_code == 200
            assert r_skip.json()["saved_cents"] == 15_000

            # 5. CSV Import deduplication (import twice -> 2nd import skips all duplicates!)
            csv_payload = "Date,Description,Amount\n2026-11-18,TRADER JOES,-55.40\n2026-11-19,SHELL OIL,-42.10\n"
            r_csv1 = client.post("/api/import/csv-text", json={"csv_text": csv_payload})
            assert r_csv1.status_code == 200
            assert r_csv1.json()["imported_count"] == 2
            assert r_csv1.json()["duplicate_count"] == 0

            r_csv2 = client.post("/api/import/csv-text", json={"csv_text": csv_payload})
            assert r_csv2.json()["imported_count"] == 0
            assert r_csv2.json()["duplicate_count"] == 2

            # 6. Weekly Reset completion
            r_reset = client.post("/api/weekly-reset/complete", json={"apply_rebalances": True})
            assert r_reset.status_code == 200
            assert r_reset.json()["adhd_stats"]["weekly_reset_streak"] >= 3

            # 7. ChronoX Trading Capital Guardrail endpoint
            r_cap = client.get("/api/chronox/safe-trading-capital")
            assert r_cap.status_code == 200
            assert r_cap.json()["guardrail_active"] is True

            # 8. Export & restore backup cleanly
            backup = client.get("/api/backup/export").json()
            assert backup["schema_version"] == 1
            assert "passcode_hash" not in backup["settings"]

            r_restore = client.post(
                "/api/backup/import",
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

        client, svc = _make_client(tmp_path, mock_transport=httpx.MockTransport(handler))
        try:
            r_set = client.patch(
                "/api/settings",
                json={"opencode_api_key": "sk-opencode-live-secret-9876", "opencode_model": "glm-5.3-flash"},
            )
            assert r_set.status_code == 200
            body = r_set.json()
            assert body["opencode_key_configured"] is True
            assert "sk-opencode-live-secret-9876" not in json.dumps(body)
            assert body["opencode_key_masked"].endswith("9876")

            r_test = client.post("/api/ai/test-connection")
            assert r_test.status_code == 200
            assert r_test.json()["ok"] is True
        finally:
            svc.close()

    def test_local_passcode_lock_protects_all_private_routes(self, tmp_path):
        client, svc = _make_client(tmp_path)
        try:
            # Enable passcode lock
            r_setup = client.post("/api/auth/setup", json={"passcode": "calm-adhd-2026"})
            assert r_setup.status_code == 200

            # Logout clears cookie -> dashboard must return 401
            client.post("/api/auth/logout")
            assert client.get("/api/dashboard").status_code == 401

            # Wrong passcode -> 401
            assert client.post("/api/auth/login", json={"passcode": "wrong-pass"}).status_code == 401

            # Right passcode -> sets HttpOnly cookie -> dashboard succeeds!
            r_login = client.post("/api/auth/login", json={"passcode": "calm-adhd-2026"})
            assert r_login.status_code == 200
            assert client.get("/api/dashboard").status_code == 200
        finally:
            svc.close()
