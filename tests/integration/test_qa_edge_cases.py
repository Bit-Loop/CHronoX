"""Comprehensive QA, edge-case, security, CLI, and frontend DOM integrity tests."""

from __future__ import annotations

import json
import os
import re
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chronox_finance import get_safe_trading_capital
from chronox_finance.api import create_app
from chronox_finance.cli import run_cli
from chronox_finance.models import ImpulseCreate, ImpulseDecision
from chronox_finance.security import SlidingWindowRateLimiter, validate_external_api_url
from chronox_finance.service import FinanceService


def _make_client(tmp_path, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.delenv("OPENCODE_GO_API_KEY", raising=False)
        monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    db_file = str(tmp_path / "qa_finance.sqlite3")
    svc = FinanceService(db_path=db_file, secret_store_path=tmp_path / "secrets.json")
    app = create_app(db_path=db_file, service=svc)
    return TestClient(app), svc, db_file


class TestFrontendDOMAndSecurityContract:
    def test_every_js_dom_id_exists_in_index_html_and_no_xss_sinks(self):
        web_dir = Path("chronox_finance/web")
        html = (web_dir / "index.html").read_text(encoding="utf-8")
        js = (web_dir / "app.js").read_text(encoding="utf-8")

        html_ids = set(re.findall(r'\bid="([^"]+)"', html))
        js_ids = set(re.findall(r'\$\("([^"]+)"\)', js))
        missing_ids = sorted(js_ids - html_ids)
        assert missing_ids == [], f"app.js references missing HTML IDs: {missing_ids}"

        # Strict CSP & XSS hygiene: zero inline event attributes, zero innerHTML assignments
        assert not re.search(r"\bon(?:click|submit|change|input|load)\s*=", html, re.I)
        assert "innerHTML" not in js
        assert "outerHTML" not in js
        assert "document.write" not in js


class TestMonthEndRolloverAndDemoRadar:
    def test_demo_workspace_populates_14_day_radar_and_unpaid_bills_shield(self, tmp_path, monkeypatch):
        client, svc, db_file = _make_client(tmp_path, monkeypatch)
        try:
            assert client.post("/api/demo/load").status_code == 200
            dash = client.get("/api/dashboard").json()
            # Unpaid bills due before next payday MUST be shielded (> 0)
            assert dash["safe_to_spend"]["unpaid_bills_until_payday_cents"] > 0
            # Multiple bills must appear in 14-day radar including a price hike
            bills_14d = [b for b in dash["upcoming_bills"] if b["days_until_due"] <= 14]
            assert len(bills_14d) >= 3
            assert any(b["price_hike_cents"] > 0 for b in bills_14d)

            # ChronoX trading capital bridge matches dashboard
            cap = get_safe_trading_capital(db_file)
            assert cap == round(dash["safe_to_invest_cents"] / 100.0, 2)
        finally:
            svc.close()

    def test_automatic_month_rollover_with_cap_overflow_to_safety(self, tmp_path, monkeypatch):
        _client, svc, _ = _make_client(tmp_path, monkeypatch)
        try:
            # Initialize cycle in October 2026
            oct_day = date(2026, 10, 28)
            svc.run_self_updating_sync(reason="Oct init", today=oct_day)
            assert svc.storage.get_settings()["last_rollover_ym"] == "2026-10"

            # Advance into November 2026 -> triggers automatic month rollover once
            nov_day = date(2026, 11, 2)
            svc.run_self_updating_sync(reason="Nov roll", today=nov_day)
            assert svc.storage.get_settings()["last_rollover_ym"] == "2026-11"

            jars = {j["slug"]: j for j in svc.storage.list_jars()}
            # Daily jar has rollover cap $400 (40_000 cents); unspent > cap sweeps overflow to Safety
            assert jars["daily"]["rollover_cents"] <= 40_000
            assert jars["safety"]["rollover_cents"] > 0

            # Second sync in November is strictly idempotent (does not double-roll!)
            safety_before = jars["safety"]["rollover_cents"]
            svc.run_self_updating_sync(reason="Nov repeat", today=nov_day)
            safety_after = svc.storage.get_jar_by_slug("safety")["rollover_cents"]
            assert safety_after == safety_before
        finally:
            svc.close()


class TestImpulseSweepAndAtomicBackupSecurity:
    def test_impulse_skip_sweeps_savings_and_buy_logs_transaction_once(self, tmp_path, monkeypatch):
        _client, svc, _ = _make_client(tmp_path, monkeypatch)
        try:
            svc.load_demo_workspace()
            imp1 = svc.create_impulse(ImpulseCreate(item_name="RGB Desk Mat", price_cents=6_000, cooling_hours=24))
            res_skip = svc.decide_impulse(imp1.id, ImpulseDecision(decision="skip", sweep_savings_pct=50))
            assert res_skip["status"] == "skipped"
            assert res_skip["swept_cents"] == 3_000

            # Double-deciding must raise ValueError cleanly
            with pytest.raises(ValueError, match="already been decided"):
                svc.decide_impulse(imp1.id, ImpulseDecision(decision="skip"))

            imp2 = svc.create_impulse(ImpulseCreate(item_name="Ergonomic Mouse", price_cents=8_000, cooling_hours=24))
            res_buy = svc.decide_impulse(imp2.id, ImpulseDecision(decision="buy"))
            assert res_buy["status"] == "bought"
            assert res_buy["transaction"]["amount_cents"] == 8_000
        finally:
            svc.close()

    def test_backup_restore_rejects_corrupt_or_ssrf_without_wiping_existing_data(self, tmp_path, monkeypatch):
        client, svc, _ = _make_client(tmp_path, monkeypatch)
        try:
            svc.load_demo_workspace()
            orig_tx_count = len(svc.storage.list_transactions(limit=500))
            assert orig_tx_count > 10

            # 1. Corrupt payload (missing jars) rejected cleanly
            bad_empty = json.dumps({"schema_version": 1, "jars": []}).encode("utf-8")
            r1 = client.post("/api/backup/import", files={"file": ("bad.json", bad_empty, "application/json")})
            assert r1.status_code == 400
            assert len(svc.storage.list_transactions(limit=500)) == orig_tx_count

            # 2. SSRF URL inside backup rejected cleanly
            backup = client.get("/api/backup/export").json()
            backup["settings"]["opencode_base_url"] = "http://169.254.169.254/latest/meta-data"
            r2 = client.post(
                "/api/backup/import",
                files={"file": ("ssrf.json", json.dumps(backup).encode("utf-8"), "application/json")},
            )
            assert r2.status_code == 400
            assert len(svc.storage.list_transactions(limit=500)) == orig_tx_count

            # 3. Mid-restore failure rolls back atomically (existing data stays intact!)
            backup2 = client.get("/api/backup/export").json()
            backup2["transactions"].append({"id": 999999})  # Missing required keys -> KeyError mid-transaction
            r3 = client.post(
                "/api/backup/import",
                files={"file": ("corrupt.json", json.dumps(backup2).encode("utf-8"), "application/json")},
            )
            assert r3.status_code == 400
            assert len(svc.storage.list_transactions(limit=500)) == orig_tx_count
        finally:
            svc.close()

    def test_db_file_permissions_restricted_and_ssrf_and_rate_limiter_bounded(self, tmp_path, monkeypatch):
        _client, svc, db_file = _make_client(tmp_path, monkeypatch)
        try:
            mode = os.stat(db_file).st_mode & 0o777
            assert mode == 0o600
        finally:
            svc.close()

        with pytest.raises(ValueError):
            validate_external_api_url("http://169.254.169.254/latest")
        with pytest.raises(ValueError):
            validate_external_api_url("ftp://opencode.ai/zen/go/v1")
        with pytest.raises(ValueError):
            validate_external_api_url("http://example.com/v1")  # Non-local HTTP blocked

        limiter = SlidingWindowRateLimiter(max_requests=2, window_seconds=60, max_keys=16)
        for i in range(40):
            ok, _ = limiter.allow(f"client-{i}")
            assert ok is True
        assert len(limiter._buckets) <= 16


class TestFullAPICRUDAndCLI:
    def test_crud_accounts_jars_transactions_bills_suggestions_and_auth_disable(self, tmp_path, monkeypatch):
        client, svc, _ = _make_client(tmp_path, monkeypatch)
        try:
            # Accounts create + reconcile
            r_acct = client.post(
                "/api/accounts",
                json={"name": "Reserve Checking", "account_type": "checking", "balance_cents": 200_000, "is_liquid": True},
            )
            assert r_acct.status_code == 200
            acct_id = r_acct.json()["id"]
            assert client.patch(f"/api/accounts/{acct_id}/balance", json={"balance_cents": 250_000}).status_code == 200

            # Jar update
            dash = client.get("/api/dashboard").json()
            fun_jar = next(j for j in dash["jars"] if j["slug"] == "fun")
            assert client.patch(f"/api/jars/{fun_jar['id']}", json={"target_pct": 18.0, "rollover_enabled": True}).status_code == 200

            # Transaction create, update (learns rule!), delete (reverses balance!)
            r_tx = client.post(
                "/api/transactions",
                json={
                    "account_id": acct_id,
                    "tx_type": "expense",
                    "amount_cents": 4_500,
                    "merchant": "Corner Bistro",
                    "tx_date": date.today().isoformat(),
                },
            )
            assert r_tx.status_code == 200
            tx_id = r_tx.json()["id"]
            assert client.patch(f"/api/transactions/{tx_id}", json={"jar_id": fun_jar["id"], "learn_merchant_rule": True}).status_code == 200
            assert client.get("/api/transactions?search=Corner").json()[0]["jar_id"] == fun_jar["id"]
            assert client.delete(f"/api/transactions/{tx_id}").status_code == 200
            assert svc.storage.get_account(acct_id)["balance_cents"] == 250_000

            # Bills CRUD + zombie toggle
            due = (date.today() + timedelta(days=5)).isoformat()
            r_bill = client.post(
                "/api/bills",
                json={"name": "Cloud Backup", "amount_cents": 999, "cadence": "monthly", "next_due_date": due, "is_subscription": True, "is_essential": False},
            )
            assert r_bill.status_code == 200
            bill_id = r_bill.json()["id"]
            assert len(client.get("/api/bills").json()) >= 1
            assert client.patch(f"/api/bills/{bill_id}/zombie", json={"flagged": True}).status_code == 200

            # Apply suggestion actions + invalid action -> 400
            assert client.post("/api/ai/suggestions/apply", json={"action_type": "review_subscription", "action_payload": {"bill_id": bill_id}}).status_code == 200
            assert client.post("/api/ai/suggestions/apply", json={"action_type": "boost_emergency_buffer", "action_payload": {"emergency_buffer_cents": 60_000}}).status_code == 200
            assert client.post("/api/ai/suggestions/apply", json={"action_type": "set_trading_cap", "action_payload": {"safe_to_invest_cents": 10_000}}).status_code == 200
            assert client.post("/api/ai/suggestions/apply", json={"action_type": "park_impulse", "action_payload": {"item_name": "Desk Chair", "price_cents": 25_000}}).status_code == 200
            assert client.post("/api/ai/suggestions/apply", json={"action_type": "invalid_action", "action_payload": {}}).status_code == 400
            assert client.delete(f"/api/bills/{bill_id}").status_code == 200

            # CSV file upload + export
            csv_bytes = b"Date,Description,Amount\n2026-11-15,TRADER JOES,-62.30\n"
            r_up = client.post("/api/import/csv", files={"file": ("bank.csv", csv_bytes, "text/csv")})
            assert r_up.status_code == 200 and r_up.json()["imported_count"] == 1
            r_exp = client.get("/api/export/csv")
            assert r_exp.status_code == 200 and "TRADER JOES" in r_exp.text

            # Settings key save + clear_opencode_key
            client.patch("/api/settings", json={"opencode_api_key": "sk-temp-key-1234"})
            assert client.get("/api/settings").json()["opencode_key_configured"] is True
            client.patch("/api/settings", json={"clear_opencode_key": True})
            assert client.get("/api/settings").json()["opencode_key_configured"] is False

            # Auth setup -> disable with wrong passcode fails -> right passcode disables
            assert client.post("/api/auth/setup", json={"passcode": "my-passcode-1"}).status_code == 200
            assert client.post("/api/auth/disable", json={"passcode": "wrong-pass"}).status_code == 401
            assert client.post("/api/auth/disable", json={"passcode": "my-passcode-1"}).status_code == 200
            assert client.get("/api/auth/status").json()["auth_enabled"] is False
        finally:
            svc.close()

    def test_cli_commands_end_to_end(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("OPENCODE_GO_API_KEY", raising=False)
        monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
        db_file = str(tmp_path / "cli_qa.sqlite3")

        assert run_cli(["--db", db_file, "demo"]) == 0
        assert run_cli(["--db", db_file, "status"]) == 0
        assert run_cli(["--db", db_file, "add", "coffee", "5.75"]) == 0
        assert run_cli(["--db", db_file, "ask", "Can I afford a $60 dinner?"]) == 0
        assert run_cli(["--db", db_file, "suggest"]) == 0

        out = capsys.readouterr().out
        assert "SAFE TO SPEND TODAY" in out
        assert "Logged $5.75" in out
        assert "Verified Local Engine" in out
