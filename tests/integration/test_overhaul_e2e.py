"""Comprehensive End-to-End Integration Suite for the ChronoX Finance Platform Overhaul.

Verifies all 9 production overhaul subsystems through the real FastAPI `TestClient`:
1. Mandatory fail-closed Envelope Vault Lock, Recovery Key, DEK rewrap, DNS-rebinding, CSRF & 423 locked sync
2. Clean `/api/openapi.json` schema (canonical `/api/v1/*` routes only, zero `/api/*` duplicates, zero demo/disable)
3. MoneyStill Analytics (`1y` overview, single-month drilldown), 730-day Net Worth, What-If Scenarios w/ `per_goal_series`
4. Firefly Rules (`create`, 2Y `test` dry-run, `apply`), Exact-Sum Splits (`SUM==parent` vs `400`), Tags & Comments
5. Honest Credit Factor Simulator + Chase 5/24 + Inquiries/Derogatories + Official Credit Report Import (`needs_review=True`)
6. Portfolio Holdings (`quantity_e8`/`price_e8` sub-cent crypto + `ROUND_HALF_EVEN`), CSV Import, Coinbase RO refusal & Quote Refresh
7. AI Usage telemetry & Agentic Coach (`plan` -> atomic `execute` -> `undo`, plus row-version conflict refusal on Undo)
8. Goals drag-reorder (`POST /api/v1/goals/reorder`) + soft-delete & restore (`DELETE` -> `POST /restore`)
9. Optional Read-Only IMAP Email Queue (`BODY.PEEK` / `EXAMINE`, scan -> approve / dismiss)
"""

from __future__ import annotations

import json
import os
import zlib
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from chronox_finance.api import create_app
from chronox_finance.models import compute_holding_value_cents
from chronox_finance.security import CSRF_HEADER_NAME
from chronox_finance.service import FinanceService
from tests.fixtures.seed_workspace import seed_workspace


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "OPENCODE_GO_API_KEY",
        "OPENCODE_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
        "POLYGON_API_KEY",
        "CHRONOX_INTELLIGENCE_MODE",
        "CHRONOX_AI_PROVIDER",
    ):
        monkeypatch.delenv(var, raising=False)


def _gen_ec_p256_pem() -> str:
    priv = ec.generate_private_key(ec.SECP256R1())
    return priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def _build_synthetic_pdf(lines: List[str]) -> bytes:
    cmds = ["BT", "/F1 10 Tf", "72 720 Td"]
    for idx, line in enumerate(lines):
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        if idx > 0:
            cmds.append("0 -14 Td")
        cmds.append(f"({escaped}) Tj")
    cmds.append("ET")
    payload = zlib.compress("\n".join(cmds).encode("utf-8"))
    return (
        b"%PDF-1.4\n1 0 obj\n<< /Filter /FlateDecode /Length "
        + str(len(payload)).encode("ascii")
        + b" >>\nstream\n"
        + payload
        + b"\nendstream\nendobj\n%%EOF\n"
    )


class _ReadOnlyMockIMAP:
    """Strict read-only IMAP double that enforces EXAMINE/SELECT(readonly=True) & BODY.PEEK."""

    def __init__(self, messages: Dict[str, Tuple[bytes, bytes]]):
        self.messages = messages
        self.seen_flags: Dict[str, bool] = {uid: False for uid in messages}
        self.commands: List[Tuple[str, Any]] = []
        self.logged_out = False

    def login(self, user: str, password: str):
        self.commands.append(("LOGIN", user))
        return "OK", [b"Logged in"]

    def select(self, mailbox: str = "INBOX", readonly: bool = False):
        if not readonly:
            raise AssertionError("IMAP select MUST be readonly=True!")
        self.commands.append(("SELECT_RO", mailbox))
        return "OK", [str(len(self.messages)).encode("ascii")]

    def examine(self, mailbox: str = "INBOX"):
        self.commands.append(("EXAMINE", mailbox))
        return "OK", [str(len(self.messages)).encode("ascii")]

    def uid(self, command: str, *args):
        cmd = command.upper()
        self.commands.append((f"UID_{cmd}", args))
        if cmd == "SEARCH":
            return "OK", [" ".join(self.messages.keys()).encode("ascii")]
        if cmd == "FETCH":
            uid_str = str(args[0])
            spec = str(args[1]).upper()
            if "BODY.PEEK" not in spec:
                raise AssertionError(f"IMAP fetch MUST use BODY.PEEK to stay read-only! Got: {spec}")
            hdr, body = self.messages[uid_str]
            return "OK", [
                (b"1 (BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {100}", hdr),
                (b" BODY[TEXT] {100}", body),
                b")",
            ]
        raise AssertionError(f"Forbidden mutating IMAP UID command: {cmd}")

    def store(self, *_args, **_kwargs):
        raise AssertionError("IMAP STORE is forbidden (read-only contract)")

    def logout(self):
        self.logged_out = True
        return "BYE", [b"Logged out"]


def _make_locked_app(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    kdf_iterations: int = 10_000,
    allowed_port: Optional[int] = None,
    opencode_transport: Any = None,
    gemini_transport: Any = None,
    codex_transport: Any = None,
    broker_transport: Any = None,
    quote_transport: Any = None,
    imap_factory: Any = None,
) -> Tuple[TestClient, FinanceService]:
    _clear_env(monkeypatch)
    db_file = str(tmp_path / "overhaul_e2e.sqlite3")
    svc = FinanceService(
        db_path=db_file,
        secret_store_path=tmp_path / "secrets.enc",
        kdf_iterations=kdf_iterations,
        opencode_transport=opencode_transport,
        gemini_transport=gemini_transport,
        codex_transport=codex_transport,
        broker_transport=broker_transport,
        quote_transport=quote_transport,
        imap_factory=imap_factory,
    )
    app = create_app(
        db_path=db_file,
        service=svc,
        enforce_vault_lock=True,
        allowed_port=allowed_port,
    )
    return TestClient(app), svc


def _setup_vault(client: TestClient, secret: Optional[str] = None) -> Tuple[Dict[str, str], Dict[str, Any]]:
    pw = secret or "OverhaulVault!2026"
    resp = client.post("/api/v1/auth/setup", json={"password": pw})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return {CSRF_HEADER_NAME: body["csrf_token"]}, body


# =============================================================================
# 1. Mandatory Envelope Vault Lock, Recovery, DNS-Rebinding, CSRF & Locked Sync
# =============================================================================


class TestMandatoryVaultAndSecurityE2E:
    def test_full_vault_lifecycle_dns_rebinding_csrf_locked_sync_and_purged_routes(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ):
        client, svc = _make_locked_app(tmp_path, monkeypatch, kdf_iterations=10_000)
        try:
            # Purged user-facing demo & auth/disable routes MUST return 404
            for dead_path in ("/api/demo/load", "/api/v1/demo/load", "/api/auth/disable", "/api/v1/auth/disable"):
                assert client.post(dead_path, headers={CSRF_HEADER_NAME: "x"}).status_code in (403, 404)

            # Fail-closed before vault setup -> 401 with state=needs_setup
            r_init = client.get("/api/v1/dashboard")
            assert r_init.status_code == 401
            assert r_init.json()["state"] == "needs_setup"

            st0 = client.get("/api/v1/auth/status").json()
            assert st0["state"] == "needs_setup"
            assert st0["initialized"] is False
            assert st0["unlocked"] is False
            assert st0["db_encrypted_at_rest"] is False

            # DNS-rebinding: non-loopback Host -> 403 host_not_allowed
            r_bad_host = client.get("/api/v1/health", headers={"Host": "evil-rebind.example:8742"})
            assert r_bad_host.status_code == 403
            assert r_bad_host.json()["code"] == "host_not_allowed"

            # DNS-rebinding / cross-origin: bad Origin -> 403 origin_not_allowed
            r_bad_origin = client.post(
                "/api/v1/auth/setup",
                json={"password": "OverhaulVault!2026"},
                headers={"Host": "127.0.0.1:8742", "Origin": "https://attacker.example"},
            )
            assert r_bad_origin.status_code == 403
            assert r_bad_origin.json()["code"] == "origin_not_allowed"

            # Setup Vault -> returns 8-group Base32 Recovery Key, 4 challenge indices, CSRF token
            csrf, setup_body = _setup_vault(client, "OverhaulVault!2026")
            recovery_key = setup_body["recovery_key"]
            assert len(setup_body["recovery_groups"]) == 8
            assert all(len(g) == 5 for g in setup_body["recovery_groups"])
            assert len(setup_body["challenge_indices"]) == 4
            assert all(1 <= idx <= 8 for idx in setup_body["challenge_indices"])
            assert setup_body["status"]["state"] == "unlocked"
            assert setup_body["status"]["recovery_configured"] is True

            # Verify removed routes return exact 404 even with valid CSRF & auth
            assert client.post("/api/demo/load", headers=csrf).status_code == 404
            assert client.post("/api/v1/demo/load", headers=csrf).status_code == 404
            assert client.post("/api/auth/disable", headers=csrf).status_code == 404
            assert client.post("/api/v1/auth/disable", headers=csrf).status_code == 404

            # Missing X-ChronoX-CSRF header on mutating endpoint -> 403 csrf_invalid
            r_no_csrf = client.post("/api/v1/onboarding/complete", json={"track": "local"})
            assert r_no_csrf.status_code == 403
            assert r_no_csrf.json()["code"] == "csrf_invalid"

            # Valid CSRF succeeds & store a secret to verify DEK rewrap preserves payload
            assert client.post("/api/v1/onboarding/complete", json={"track": "local"}, headers=csrf).status_code == 200
            r_save_secret = client.patch(
                "/api/v1/settings/ai",
                json={"gemini_api_key": "AIzaSyVaultEnvelopeSecret9999"},
                headers=csrf,
            )
            assert r_save_secret.status_code == 200

            enc_path = tmp_path / "secrets.enc"
            assert enc_path.is_file()
            assert (os.stat(enc_path).st_mode & 0o777) == 0o600
            env_before = json.loads(enc_path.read_text(encoding="utf-8"))
            assert "AIzaSyVaultEnvelopeSecret9999" not in enc_path.read_text(encoding="utf-8")

            # Logout -> locked 401
            r_out = client.post("/api/v1/auth/logout", headers=csrf)
            assert r_out.status_code == 200
            assert r_out.json()["state"] == "locked"
            r_locked = client.get("/api/v1/dashboard")
            assert r_locked.status_code == 401
            assert r_locked.json()["state"] == "locked"

            # Login with wrong password -> 401; right password -> 200 + fresh CSRF
            assert client.post("/api/v1/auth/login", json={"password": "WrongPassword!"}).status_code == 401
            r_in = client.post("/api/v1/auth/login", json={"password": "OverhaulVault!2026"})
            assert r_in.status_code == 200
            csrf = {CSRF_HEADER_NAME: r_in.json()["csrf_token"]}
            assert client.get("/api/v1/settings/ai").json()["gemini_api_key_set"] is True

            # Change password -> rewraps password DEK ONLY; payload ciphertext untouched!
            r_chpw = client.post(
                "/api/v1/auth/change-password",
                json={"current_password": "OverhaulVault!2026", "new_password": "RotatedPassword!2027"},
                headers=csrf,
            )
            assert r_chpw.status_code == 200
            csrf = {CSRF_HEADER_NAME: r_chpw.json()["csrf_token"]}
            env_after = json.loads(enc_path.read_text(encoding="utf-8"))
            assert env_after["payload"] == env_before["payload"]

            # Logout -> old password rejected -> new password unlocks -> secret intact
            client.post("/api/v1/auth/logout", headers=csrf)
            assert client.post("/api/v1/auth/login", json={"password": "OverhaulVault!2026"}).status_code == 401
            r_in2 = client.post("/api/v1/auth/login", json={"password": "RotatedPassword!2027"})
            assert r_in2.status_code == 200
            csrf = {CSRF_HEADER_NAME: r_in2.json()["csrf_token"]}
            assert client.get("/api/v1/settings/ai").json()["gemini_key_masked"].endswith("9999")

            # Recover using 8-group Recovery Key -> sets 3rd password & preserves secret!
            client.post("/api/v1/auth/logout", headers=csrf)
            r_rec = client.post(
                "/api/v1/auth/recover",
                json={"recovery_key": recovery_key, "new_password": "RecoveredVault!2028"},
            )
            assert r_rec.status_code == 200
            csrf = {CSRF_HEADER_NAME: r_rec.json()["csrf_token"]}
            assert client.get("/api/v1/settings/ai").json()["gemini_key_masked"].endswith("9999")
        finally:
            svc.close()

    def test_locked_connector_sync_returns_423(self, tmp_path, monkeypatch: pytest.MonkeyPatch):
        """When vault DEK is locked, connector sync endpoints fail closed with HTTP 423."""
        _clear_env(monkeypatch)
        db_file = str(tmp_path / "locked_sync.sqlite3")
        svc = FinanceService(db_path=db_file, secret_store_path=tmp_path / "secrets.enc", kdf_iterations=10_000)
        svc.secrets.setup_vault("LockedSyncPass!2026")
        svc.secrets.lock()
        # Bypass HTTP session gate to exercise the service-level VaultLockedError -> HTTP 423 handler directly
        app = create_app(db_path=db_file, service=svc, enforce_vault_lock=False)
        client = TestClient(app)
        try:
            for ep in ("/api/v1/plaid/sync", "/api/v1/simplefin/sync", "/api/v1/email/scan"):
                resp = client.post(ep)
                assert resp.status_code == 423, f"Expected 423 on locked {ep}, got {resp.status_code}: {resp.text}"
                assert resp.json()["code"] == "vault_locked"
                assert "vault locked" in resp.json()["detail"].lower()
        finally:
            svc.close()


# =============================================================================
# 2. Clean OpenAPI Schema (/api/openapi.json)
# =============================================================================


class TestOpenAPISchemaContract:
    def test_openapi_exposes_only_canonical_v1_routes_without_duplicates(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ):
        client, svc = _make_locked_app(tmp_path, monkeypatch)
        try:
            r = client.get("/api/openapi.json")
            assert r.status_code == 200
            schema = r.json()
            paths = sorted(schema["paths"].keys())
            assert len(paths) >= 50
            assert all(p.startswith("/api/v1/") for p in paths), (
                f"Non-canonical paths leaked into OpenAPI: {[p for p in paths if not p.startswith('/api/v1/')]}"
            )
            assert "/api/v1/demo/load" not in paths
            assert "/api/v1/auth/disable" not in paths
            assert "/api/v1/ai/test-connection" not in paths
        finally:
            svc.close()


# =============================================================================
# 3. MoneyStill Analytics, 730d Net Worth & What-If Scenarios
# =============================================================================


class TestAnalyticsAndWhatIfE2E:
    def test_moneystill_analytics_net_worth_and_what_if_scenario_sim(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ):
        client, svc = _make_locked_app(tmp_path, monkeypatch)
        try:
            csrf, _ = _setup_vault(client)
            seed_workspace(svc)

            # MoneyStill 1Y Overview
            r_ov = client.get("/api/v1/analytics/overview?period=1y")
            assert r_ov.status_code == 200
            ov = r_ov.json()
            assert ov["total_income_cents"] > 0
            assert ov["total_expense_cents"] > 0
            assert len(ov["top_5_expenses"]) >= 1
            assert len(ov["sankey_nodes"]) >= 3
            assert len(ov["sankey_links"]) >= 2
            assert len(ov["expense_distribution"]) >= 1
            assert len(ov["monthly_trends"]) >= 1
            assert len(ov["net_worth_series"]) >= 1

            # Single-month drilldown
            ym = ov["monthly_trends"][-1]["month"]
            r_month = client.get(f"/api/v1/analytics/overview?period=1y&month={ym}")
            assert r_month.status_code == 200
            assert r_month.json()["period"] == ym

            # 730-day Net Worth series
            r_nw = client.get("/api/v1/analytics/net-worth?days=730")
            assert r_nw.status_code == 200
            nw = r_nw.json()
            assert isinstance(nw, list) and len(nw) >= 30
            assert {"date", "liquid_cents", "investment_cents", "debt_cents", "net_worth_cents"} <= set(nw[-1].keys())

            # Saved What-If Scenario CRUD + scenario-sim with per_goal_series
            goals = client.get("/api/v1/goals").json()
            assert len(goals) >= 2
            g0 = goals[0]
            shifted_date = (date.today() + timedelta(days=150)).isoformat()

            r_sc = client.post(
                "/api/v1/scenarios",
                json={
                    "name": "Aggressive Paydown + Gadget Shift",
                    "overrides": {
                        "goal_date_shifts": {str(g0["id"]): shifted_date},
                        "monthly_income_delta_cents": 20_000,
                        "monthly_expense_delta_cents": -10_000,
                    },
                },
                headers=csrf,
            )
            assert r_sc.status_code == 200
            sc_id = r_sc.json()["id"]
            assert any(s["id"] == sc_id for s in client.get("/api/v1/scenarios").json())

            r_sim = client.post(
                "/api/v1/analytics/scenario-sim",
                json={"horizon_days": 365, "scenario_id": sc_id},
                headers=csrf,
            )
            assert r_sim.status_code == 200
            sim = r_sim.json()
            assert len(sim["baseline_points"]) == 365
            assert len(sim["scenario_points"]) == 365
            assert sim["diff_ending_cents"] > 0
            assert len(sim["per_goal_series"]) >= 2
            for gs in sim["per_goal_series"]:
                assert len(gs["points"]) >= 1
                assert sum(p["contribution_cents"] for p in gs["points"]) == gs["target_cents"] - gs["saved_cents"]

            assert client.delete(f"/api/v1/scenarios/{sc_id}", headers=csrf).status_code == 200
        finally:
            svc.close()


# =============================================================================
# 4. Firefly Rules, Exact-Sum Splits, Tags & Comments
# =============================================================================


class TestFireflyRulesSplitsTagsCommentsE2E:
    def test_rules_dry_run_apply_splits_invariant_tags_and_comments(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ):
        client, svc = _make_locked_app(tmp_path, monkeypatch)
        try:
            csrf, _ = _setup_vault(client)
            seed_workspace(svc)

            jars = {j["slug"]: j["id"] for j in client.get("/api/v1/dashboard").json()["jars"]}

            # Create rule group + Firefly rule targeting Trader Joe's
            r_grp = client.post("/api/v1/rule-groups", json={"name": "Groceries Automation", "priority": 1}, headers=csrf)
            assert r_grp.status_code == 200
            grp_id = r_grp.json()["id"]

            r_rule = client.post(
                "/api/v1/rules",
                json={
                    "group_id": grp_id,
                    "name": "Tag Trader Joe's Runs",
                    "priority": 1,
                    "strict_and": True,
                    "triggers": [{"field": "merchant", "op": "contains", "value": "trader joe"}],
                    "actions": [
                        {"kind": "set_jar", "value": jars["daily"]},
                        {"kind": "add_tag", "value": "groceries-auto"},
                    ],
                },
                headers=csrf,
            )
            assert r_rule.status_code == 200
            rule_id = r_rule.json()["id"]

            # 2-year dry-run test
            r_test = client.post(f"/api/v1/rules/{rule_id}/test?lookback_days=730", headers=csrf)
            assert r_test.status_code == 200
            t_res = r_test.json()
            assert t_res["matched_count"] >= 3
            assert len(t_res["samples"]) >= 1

            # Apply rule across ledger
            r_apply = client.post(f"/api/v1/rules/{rule_id}/apply?lookback_days=730", headers=csrf)
            assert r_apply.status_code == 200
            assert r_apply.json()["updated_count"] >= 3

            txs = client.get("/api/v1/transactions?search=Trader&limit=50").json()
            assert len(txs) >= 1
            tx0 = txs[0]
            assert any(t["name"] == "groceries-auto" for t in tx0["tags"])
            tx_id = tx0["id"]
            parent_amt = tx0["amount_cents"]

            # Exact-sum Splits: mismatch MUST return 400 split_validation_error
            r_bad_split = client.post(
                f"/api/v1/transactions/{tx_id}/splits",
                json={
                    "splits": [
                        {"jar_id": jars["daily"], "amount_cents": parent_amt - 500, "memo": "Part 1"},
                        {"jar_id": jars["fun"], "amount_cents": 400, "memo": "Off by $1"},
                    ]
                },
                headers=csrf,
            )
            assert r_bad_split.status_code == 400
            assert r_bad_split.json()["code"] == "split_validation_error"

            # Exact-sum Splits: SUM == parent accepted 200
            part_a = parent_amt // 2
            part_b = parent_amt - part_a
            r_ok_split = client.post(
                f"/api/v1/transactions/{tx_id}/splits",
                json={
                    "splits": [
                        {"jar_id": jars["daily"], "amount_cents": part_a, "memo": "Essentials"},
                        {"jar_id": jars["fun"], "amount_cents": part_b, "memo": "Treats"},
                    ]
                },
                headers=csrf,
            )
            assert r_ok_split.status_code == 200
            splits_out = r_ok_split.json()
            assert len(splits_out) == 2
            assert sum(s["amount_cents"] for s in splits_out) == parent_amt

            # Tags PUT & Comments POST/GET
            r_tags = client.put(
                f"/api/v1/transactions/{tx_id}/tags",
                json={"tags": ["groceries-auto", "reconciled", "household"]},
                headers=csrf,
            )
            assert r_tags.status_code == 200
            assert {t["name"] for t in r_tags.json()} == {"groceries-auto", "reconciled", "household"}

            r_cmt = client.post(
                f"/api/v1/transactions/{tx_id}/comments",
                json={"body": "Split snacks into Fun jar for clean analytics.", "author": "user"},
                headers=csrf,
            )
            assert r_cmt.status_code == 200
            cmts = client.get(f"/api/v1/transactions/{tx_id}/comments").json()
            assert len(cmts) == 1
            assert "Split snacks" in cmts[0]["body"]

            # Clear splits cleanly
            assert client.delete(f"/api/v1/transactions/{tx_id}/splits", headers=csrf).status_code == 200
        finally:
            svc.close()


# =============================================================================
# 5. Honest Credit Factor Simulator + 5/24 + Official Report Import
# =============================================================================


class TestCreditSimulatorAndImportE2E:
    def test_credit_factor_simulator_524_derogatories_and_pdf_review_gate(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ):
        client, svc = _make_locked_app(tmp_path, monkeypatch)
        try:
            csrf, _ = _setup_vault(client)
            seed_workspace(svc)

            # Record imported actual bureau score
            r_rep = client.post(
                "/api/v1/credit/reports",
                json={
                    "bureau": "experian",
                    "model": "fico8",
                    "score": 742,
                    "pulled_date": date.today().isoformat(),
                    "source": "manual",
                },
                headers=csrf,
            )
            assert r_rep.status_code == 200

            # Add an Authorized User card opened within last 24m to test include_au toggle
            recent_open = (date.today() - timedelta(days=90)).isoformat()
            r_au = client.post(
                "/api/v1/credit/lines",
                json={
                    "name": "Partner Sapphire Reserve AU",
                    "issuer": "Chase",
                    "kind": "credit_card",
                    "balance_cents": 10_000,
                    "credit_limit_cents": 1_000_000,
                    "apr_bps": 2299,
                    "min_payment_cents": 3_500,
                    "opened_date": recent_open,
                    "is_authorized_user": True,
                },
                headers=csrf,
            )
            assert r_au.status_code == 200

            # Hard Inquiry (+12m counts_until / +24m drops_off) & Derogatory (+7y / +10y Ch7)
            inq_date = (date.today() - timedelta(days=30)).isoformat()
            r_inq = client.post(
                "/api/v1/credit/inquiries",
                json={"lender": "Chase Bank", "bureau": "experian", "inquiry_date": inq_date},
                headers=csrf,
            )
            assert r_inq.status_code == 200
            inq = r_inq.json()
            assert inq["counts_until"] > inq_date
            assert inq["drops_off"] > inq["counts_until"]

            r_der = client.post(
                "/api/v1/credit/derogatories",
                json={"kind": "bankruptcy_ch7", "lender": "Federal Court", "opened_date": "2020-05-01"},
                headers=csrf,
            )
            assert r_der.status_code == 200
            assert r_der.json()["drops_off"].startswith("2030-")

            # Full credit report with Honest Factor Simulator & Chase 5/24
            rep_with_au = client.get("/api/v1/credit/report?extra_monthly_cents=35000&include_au=true").json()
            rep_no_au = client.get("/api/v1/credit/report?extra_monthly_cents=35000&include_au=false").json()

            fs = rep_with_au["factor_simulator"]
            assert fs is not None
            assert "cannot reproduce" in fs["disclaimer"].lower() or "directional" in fs["disclaimer"].lower()
            assert any(s["score"] == 742 and s["bureau"] == "experian" for s in fs["latest_actual_scores"])
            assert len(fs["fico8_factors"]) == 5
            assert len(fs["vantage3_factors"]) >= 5
            assert len(fs["simulated_actions"]) >= 1

            assert rep_with_au["five_twenty_four"]["count_5_24"] == rep_no_au["five_twenty_four"]["count_5_24"] + 1
            assert rep_with_au["five_twenty_four"]["inquiries_6m"] >= 1
            assert len(rep_with_au["credit_inquiries"]) >= 1
            assert len(rep_with_au["credit_derogatories"]) >= 1

            # Official AnnualCreditReport PDF import MUST return needs_review=True & committed=False by default!
            pdf_bytes = _build_synthetic_pdf(
                [
                    "Experian Personal Credit Report",
                    "Date Generated: 2026-11-18",
                    "FICO Score 8: 758",
                    "Hard Inquiries",
                    "CAPITAL ONE 10/05/2026",
                ]
            )
            r_pdf = client.post(
                "/api/v1/credit/reports/import",
                files={"file": ("experian_report.pdf", pdf_bytes, "application/pdf")},
                headers=csrf,
            )
            assert r_pdf.status_code == 200
            pdf_res = r_pdf.json()
            assert pdf_res["needs_review"] is True
            assert pdf_res["committed"] is False
        finally:
            svc.close()


# =============================================================================
# 6. Portfolio Holdings (10^-8), CSV Import, Coinbase RO Refusal & Quote Refresh
# =============================================================================


class TestPortfolioAndBrokersE2E:
    def test_holdings_subcent_crypto_csv_coinbase_ro_refusal_and_quote_refresh(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ):
        ec_pem = _gen_ec_p256_pem()
        cb_mode = {"can_trade": True}

        def broker_handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v3/brokerage/key_permissions":
                return httpx.Response(
                    200,
                    json={
                        "can_view": True,
                        "can_trade": cb_mode["can_trade"],
                        "can_transfer": False,
                        "portfolio_uuid": "pf-uuid-1",
                        "portfolio_type": "DEFAULT",
                    },
                )
            return httpx.Response(200, json={"accounts": [], "has_next": False})

        def quote_handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "api.polygon.io" and "/v2/aggs/ticker/MSFT/prev" in request.url.path:
                return httpx.Response(200, json={"results": [{"T": "MSFT", "c": 430.50}]})
            return httpx.Response(404, json={"status": "NOT_FOUND"})

        client, svc = _make_locked_app(
            tmp_path,
            monkeypatch,
            broker_transport=httpx.MockTransport(broker_handler),
            quote_transport=httpx.MockTransport(quote_handler),
        )
        try:
            csrf, _ = _setup_vault(client)

            # 1. Sub-cent crypto holding (1,000,000 SHIB @ $0.00002345 = $23.45 exact via ROUND_HALF_EVEN)
            qty_e8 = 1_000_000 * 100_000_000
            price_e8 = 2_345  # $0.00002345
            assert compute_holding_value_cents(qty_e8, price_e8) == 2_345

            r_shib = client.post(
                "/api/v1/portfolio/holdings",
                json={
                    "symbol": "SHIB",
                    "name": "Shiba Inu",
                    "asset_class": "crypto",
                    "quantity_e8": qty_e8,
                    "price_e8": price_e8,
                    "cost_basis_cents": 2_000,
                    "source": "manual",
                },
                headers=csrf,
            )
            assert r_shib.status_code == 200
            shib = r_shib.json()
            assert shib["price_e8"] == 2_345
            assert shib["market_value_cents"] == 2_345
            assert shib["unrealized_pnl_cents"] == 345

            # 2. Broker CSV import (JSON body)
            csv_text = "Symbol,Name,Asset Class,Quantity,Price,Cost Basis\nMSFT,Microsoft Corp,equity,10,400.00,3500.00\n"
            r_csv = client.post("/api/v1/portfolio/import-csv", json={"csv_text": csv_text}, headers=csrf)
            assert r_csv.status_code == 200
            assert r_csv.json()["imported_count"] == 1

            # 3. Coinbase read-only enforcement: can_trade=True MUST be REFUSED with 403!
            r_cb_bad = client.post(
                "/api/v1/brokers/coinbase/connect",
                json={
                    "api_key_name": "organizations/org1/apiKeys/trade-key",
                    "private_key_pem": ec_pem,
                    "label": "Unsafe Trade Key",
                },
                headers=csrf,
            )
            assert r_cb_bad.status_code == 403
            assert r_cb_bad.json()["code"] == "read_only_violation"

            # Strict view-only key succeeds
            cb_mode["can_trade"] = False
            r_cb_ok = client.post(
                "/api/v1/brokers/coinbase/connect",
                json={
                    "api_key_name": "organizations/org1/apiKeys/view-only-key",
                    "private_key_pem": ec_pem,
                    "label": "Coinbase Read-Only",
                },
                headers=csrf,
            )
            assert r_cb_ok.status_code == 200
            assert r_cb_ok.json()["connection"]["provider"] == "coinbase"

            # 4. Quote refresh via BYOK Polygon updates MSFT from $400.00 -> $430.50 ($4,305.00 market value)
            monkeypatch.setenv("POLYGON_API_KEY", "poly-test-byok-key")
            r_ref = client.post("/api/v1/portfolio/refresh-quotes", headers=csrf)
            assert r_ref.status_code == 200
            port = r_ref.json()
            msft = next(h for h in port["holdings"] if h["symbol"] == "MSFT")
            assert msft["price_e8"] == 430_50000000
            assert msft["market_value_cents"] == 430_500
        finally:
            svc.close()


# =============================================================================
# 7. AI Usage Telemetry & Agentic Coach (Plan -> Execute -> Undo + Conflict)
# =============================================================================


class TestAIUsageAndAgenticCoachE2E:
    def test_ai_usage_and_agent_plan_execute_undo_and_conflict_refusal(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ):
        plan_payload = {
            "summary": "Update Starbucks coffee memo via validated Agentic Coach action.",
            "actions": [
                {
                    "action_type": "update_transaction",
                    "payload": {"transaction_id": None, "memo": "Agent verified coffee purchase"},
                    "reason": "Keep ledger memo crisp",
                }
            ],
        }

        def codex_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            # Verify stateless store=false contract
            assert body.get("store") is False
            return httpx.Response(
                200,
                json={
                    "id": "resp_e2e_1",
                    "model": "gpt-5.1-codex-mini",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": json.dumps(plan_payload)}],
                        }
                    ],
                    "usage": {
                        "input_tokens": 1800,
                        "output_tokens": 120,
                        "input_tokens_details": {"cached_tokens": 1200},
                    },
                },
            )

        client, svc = _make_locked_app(tmp_path, monkeypatch, codex_transport=httpx.MockTransport(codex_handler))
        try:
            csrf, _ = _setup_vault(client)
            seed_workspace(svc)

            txs = client.get("/api/v1/transactions?search=Starbucks&limit=5").json()
            assert len(txs) >= 1
            tx_id = txs[0]["id"]
            orig_memo = txs[0]["memo"]
            plan_payload["actions"][0]["payload"]["transaction_id"] = tx_id

            # Configure OpenAI/Codex provider
            r_ai_cfg = client.patch(
                "/api/v1/settings/ai",
                json={
                    "intelligence_mode": "hybrid",
                    "ai_provider": "openai_codex",
                    "codex_api_key": "sk-openai-test-key-e2e",
                    "codex_model": "gpt-5.1-codex-mini",
                },
                headers=csrf,
            )
            assert r_ai_cfg.status_code == 200

            # 1. Plan -> Execute -> Clean Undo
            r_plan = client.post(
                "/api/v1/ai/agent/plan",
                json={"prompt": "Add a clear coffee memo to my Starbucks purchase", "provider": "openai_codex"},
                headers=csrf,
            )
            assert r_plan.status_code == 200
            plan = r_plan.json()
            batch_id = plan["batch_id"]
            assert plan["status"] == "preview"
            assert len(plan["actions"]) == 1

            # Verify AI Usage logged exact cached tokens & verified savings!
            r_usage = client.get("/api/v1/ai/usage?days=30")
            assert r_usage.status_code == 200
            usage = r_usage.json()
            assert usage["total_requests"] >= 1
            assert usage["total_cached_tokens"] >= 1200
            assert usage["cache_hit_rate_pct"] > 0
            assert usage["total_saved_microdollars"] > 0

            r_exec = client.post(f"/api/v1/ai/agent/execute/{batch_id}", headers=csrf)
            assert r_exec.status_code == 200
            assert r_exec.json()["status"] == "executed"
            assert r_exec.json()["applied_count"] == 1

            tx_after = next(t for t in client.get("/api/v1/transactions?search=Starbucks&limit=5").json() if t["id"] == tx_id)
            assert tx_after["memo"] == "Agent verified coffee purchase"

            r_undo = client.post(f"/api/v1/ai/agent/undo/{batch_id}", headers=csrf)
            assert r_undo.status_code == 200
            assert r_undo.json()["status"] == "undone"
            assert r_undo.json()["restored_count"] >= 1

            tx_undone = next(t for t in client.get("/api/v1/transactions?search=Starbucks&limit=5").json() if t["id"] == tx_id)
            assert tx_undone["memo"] == orig_memo

            # 2. Row-Version Conflict Detection: Plan -> Execute -> External Edit -> Undo MUST refuse with conflict!
            r_plan2 = client.post(
                "/api/v1/ai/agent/plan",
                json={"prompt": "Update Starbucks memo again", "provider": "openai_codex"},
                headers=csrf,
            )
            batch2 = r_plan2.json()["batch_id"]
            assert client.post(f"/api/v1/ai/agent/execute/{batch2}", headers=csrf).json()["status"] == "executed"

            # External user mutation changes the row after execute (sub-second edit!)
            assert client.patch(
                f"/api/v1/transactions/{tx_id}",
                json={"memo": "Manual user edit after execute"},
                headers=csrf,
            ).status_code == 200

            r_conflict = client.post(f"/api/v1/ai/agent/undo/{batch2}", headers=csrf)
            assert r_conflict.status_code == 200
            conf = r_conflict.json()
            assert conf["status"] == "conflict"
            assert len(conf["conflicts"]) >= 1
            # External manual edit MUST remain preserved (not clobbered!)
            tx_preserved = next(t for t in client.get("/api/v1/transactions?search=Starbucks&limit=5").json() if t["id"] == tx_id)
            assert tx_preserved["memo"] == "Manual user edit after execute"
        finally:
            svc.close()


# =============================================================================
# 8. Goals Drag-Reorder + Soft-Delete & Restore
# =============================================================================


class TestGoalsReorderSoftDeleteRestoreE2E:
    def test_goals_reorder_soft_delete_and_restore(self, tmp_path, monkeypatch: pytest.MonkeyPatch):
        client, svc = _make_locked_app(tmp_path, monkeypatch)
        try:
            csrf, _ = _setup_vault(client)
            seed_workspace(svc)

            goals = client.get("/api/v1/goals").json()
            assert len(goals) >= 2
            reversed_ids = [g["id"] for g in reversed(goals)]

            # Drag-reorder goals
            r_re = client.post("/api/v1/goals/reorder", json={"ordered_ids": reversed_ids}, headers=csrf)
            assert r_re.status_code == 200
            reordered = r_re.json()
            assert [g["id"] for g in reordered] == reversed_ids
            assert reordered[0]["priority"] == 1

            # Soft-delete top goal
            victim_id = reversed_ids[0]
            r_del = client.delete(f"/api/v1/goals/{victim_id}", headers=csrf)
            assert r_del.status_code == 200
            assert r_del.json()["deleted"] is True
            assert r_del.json()["recoverable"] is True
            assert all(g["id"] != victim_id for g in client.get("/api/v1/goals").json())

            # Restore soft-deleted goal
            r_rest = client.post(f"/api/v1/goals/{victim_id}/restore", headers=csrf)
            assert r_rest.status_code == 200
            assert r_rest.json()["id"] == victim_id
            assert any(g["id"] == victim_id for g in client.get("/api/v1/goals").json())
        finally:
            svc.close()


# =============================================================================
# 9. Optional Read-Only IMAP Email Queue Approve / Dismiss
# =============================================================================


class TestReadOnlyEmailQueueE2E:
    def test_imap_scan_approve_and_dismiss(self, tmp_path, monkeypatch: pytest.MonkeyPatch):
        mock_imap = _ReadOnlyMockIMAP(
            {
                "501": (
                    (
                        b"From: Comcast Xfinity Billing <billing@xfinity.com>\r\n"
                        b"Subject: Your monthly statement is ready\r\n"
                        b"Date: Mon, 16 Nov 2026 14:22:00 +0000\r\n\r\n"
                    ),
                    b"Your Xfinity bill is ready. Amount Due: $124.50. Payment Due Date: Dec 15, 2026.",
                ),
                "502": (
                    (
                        b"From: Apple <no_reply@email.apple.com>\r\n"
                        b"Subject: Your receipt from Apple\r\n"
                        b"Date: Tue, 17 Nov 2026 09:05:00 +0000\r\n\r\n"
                    ),
                    b"Thank you for your purchase. Order Total: $19.99 charged to Visa.",
                ),
                "503": (
                    (
                        b"From: GitHub <noreply@github.com>\r\n"
                        b"Subject: Your verification code is 849201\r\n"
                        b"Date: Wed, 18 Nov 2026 10:00:00 +0000\r\n\r\n"
                    ),
                    b"Enter OTP 849201 to sign in.",
                ),
            }
        )

        client, svc = _make_locked_app(
            tmp_path,
            monkeypatch,
            imap_factory=lambda _h, _p, _t: mock_imap,
        )
        try:
            csrf, _ = _setup_vault(client)

            r_cfg = client.post(
                "/api/v1/email/config",
                json={
                    "enabled": True,
                    "imap_host": "imap.gmail.com",
                    "imap_port": 993,
                    "username": "owner@gmail.com",
                    "app_password": "abcd-efgh-ijkl-mnop",
                    "folder": "INBOX",
                    "lookback_days": 30,
                },
                headers=csrf,
            )
            assert r_cfg.status_code == 200
            assert r_cfg.json()["enabled"] is True
            assert r_cfg.json()["password_configured"] is True
            assert "abcd-efgh" not in r_cfg.text

            r_scan = client.post("/api/v1/email/scan", headers=csrf)
            assert r_scan.status_code == 200
            scan = r_scan.json()
            assert scan["queued_count"] == 2
            assert mock_imap.logged_out is True
            assert all(seen is False for seen in mock_imap.seen_flags.values())

            queue = client.get("/api/v1/email/queue?status=pending").json()
            assert len(queue) == 2
            bill_item = next(i for i in queue if i["kind"] == "bill")
            receipt_item = next(i for i in queue if i["kind"] != "bill")

            # Approve bill -> creates recurring bill & marks item approved
            r_app = client.post(f"/api/v1/email/queue/{bill_item['id']}/approve", headers=csrf)
            assert r_app.status_code == 200
            app_body = r_app.json()
            assert app_body["approved"] is True
            assert app_body["matched_bill_id"] is not None
            assert app_body["item"]["status"] == "approved"

            # Dismiss receipt -> marks dismissed
            r_dis = client.post(f"/api/v1/email/queue/{receipt_item['id']}/dismiss", headers=csrf)
            assert r_dis.status_code == 200
            assert r_dis.json()["status"] == "dismissed"

            # Zero pending items remain
            assert client.get("/api/v1/email/queue?status=pending").json() == []
        finally:
            svc.close()
