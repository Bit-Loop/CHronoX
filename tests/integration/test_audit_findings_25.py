"""Comprehensive integration and regression tests for all 25 audit findings (#1-#25)
and user product/UI directives (Credit Score APIs, zero UI simulators, zero UI sandbox controls).
"""

from __future__ import annotations

import io
import json
import re
import shutil
import socket
import subprocess
import sys
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chronox_finance.api import create_app
from chronox_finance.connectors.base import (
    ConnectorAuthError,
    ConnectorError,
    NormalizedAccount,
    NormalizedHolding,
    NormalizedSyncResult,
    NormalizedTransaction,
)
from chronox_finance.connectors.brokers import SchwabConnector, SchwabTokenBundle
from chronox_finance.connectors.credit_providers import (
    BureauAPIAdapter,
    CreditImportBundle,
    CreditReportRecord,
    CreditTradelineRecord,
)
from chronox_finance.connectors.plaid_client import PlaidClient
from chronox_finance.connectors.simplefin_client import SimpleFINClient
from chronox_finance.engines.credit_debt import CreditDebtOptimizer
from chronox_finance.engines.recurring import detect_payday_schedule
from chronox_finance.models import BalanceTransferRequest
from chronox_finance.security import CSRF_HEADER_NAME
from chronox_finance.service import FinanceService


def _make_unlocked_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OPENCODE_GO_API_KEY", raising=False)
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_file = str(tmp_path / "audit25.sqlite3")
    svc = FinanceService(db_path=db_file, secret_store_path=tmp_path / "secrets.enc", kdf_iterations=10_000)
    app = create_app(db_path=db_file, service=svc, enforce_vault_lock=True)
    client = TestClient(app)
    r = client.post("/api/v1/auth/setup", json={"password": "StrongAuditPassword!99"})
    assert r.status_code == 200, r.text
    csrf = r.json()["csrf_token"]
    client.headers.update({CSRF_HEADER_NAME: csrf})
    return client, svc


def _srgb_channel_lum(c: int) -> float:
    v = c / 255.0
    return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4


def _hex_luminance(hex_color: str) -> float:
    h = hex_color.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return 0.2126 * _srgb_channel_lum(r) + 0.7152 * _srgb_channel_lum(g) + 0.0722 * _srgb_channel_lum(b)


def _contrast_ratio(fg_hex: str, bg_hex: str) -> float:
    l1 = _hex_luminance(fg_hex)
    l2 = _hex_luminance(bg_hex)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


class TestAuditFindings1To5:
    def test_finding_01_and_02_plaid_simplefin_sync_dedup_delta_and_removal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, svc = _make_unlocked_client(tmp_path, monkeypatch)
        today = date.today().isoformat()

        assert (
            client.post(
                "/api/v1/plaid/config",
                json={"client_id": "cid_live", "secret": "sec_live"},
            ).status_code
            == 200
        )
        conn_row = svc.storage.upsert_connection(
            {
                "provider": "plaid",
                "item_id": "item_chase_1",
                "institution": "Chase",
                "env": "production",
                "status": "active",
            }
        )
        conn_id = int(conn_row["id"])
        svc.secrets.set_json(f"conn:{conn_id}", {"access_token": "access-prod-123", "env": "production"})

        acct_norm = NormalizedAccount(
            external_account_id="plaid_chk_1",
            name="Total Checking",
            account_type="checking",
            balance_cents=100_000,
            available_cents=98_500,
            currency="USD",
            institution="Chase",
            mask="4411",
        )

        # Initial Plaid sync: two legitimate $5.00 coffee purchases on the same day with distinct provider IDs
        def mock_plaid_sync_1(self, access_token: str, cursor=None, institution: str = "Plaid"):
            assert access_token == "access-prod-123"
            assert cursor is None
            return NormalizedSyncResult(
                provider="plaid",
                accounts=[acct_norm],
                added=[
                    NormalizedTransaction(
                        external_id="ptx_coffee_1",
                        external_account_id="plaid_chk_1",
                        tx_date=today,
                        merchant="Blue Bottle Coffee",
                        amount_cents=500,
                        tx_type="expense",
                    ),
                    NormalizedTransaction(
                        external_id="ptx_coffee_2",
                        external_account_id="plaid_chk_1",
                        tx_date=today,
                        merchant="Blue Bottle Coffee",
                        amount_cents=500,
                        tx_type="expense",
                    ),
                    NormalizedTransaction(
                        external_id="ptx_temp_hold",
                        external_account_id="plaid_chk_1",
                        tx_date=today,
                        merchant="Gas Station Hold",
                        amount_cents=7500,
                        tx_type="expense",
                    ),
                ],
                modified=[],
                removed_external_ids=[],
                next_cursor="cursor_v1",
            )

        monkeypatch.setattr(PlaidClient, "sync_transactions", mock_plaid_sync_1)
        monkeypatch.setattr(PlaidClient, "get_accounts_balances", lambda self, tok, institution="Plaid": [acct_norm])
        monkeypatch.setattr(PlaidClient, "get_liabilities", lambda self, tok, institution="Plaid": [])
        monkeypatch.setattr(PlaidClient, "get_investments_holdings", lambda self, tok: [])

        r1 = client.post("/api/v1/plaid/sync", json={"connection_id": conn_id})
        assert r1.status_code == 200, r1.text
        assert r1.json()["imported_count"] == 3

        txs_after_1 = client.get("/api/v1/transactions").json()
        coffees = [t for t in txs_after_1 if t["merchant"] == "Blue Bottle Coffee"]
        assert len(coffees) == 2, "Two legitimate $5 purchases at same merchant with distinct provider IDs must not collapse"

        # Cross-source CSV import with one $5.00 Blue Bottle Coffee should deduplicate against ONE Plaid row, not create a 3rd
        local_chk_id = [a["id"] for a in client.get("/api/v1/accounts").json() if a["name"] == "Total Checking"][0]
        csv_bytes = f"Date,Description,Amount\n{today},Blue Bottle Coffee,-5.00\n".encode("utf-8")
        imp_res = client.post(
            "/api/v1/import/statement",
            files={"file": ("chase.csv", io.BytesIO(csv_bytes), "text/csv")},
            data={"account_id": str(local_chk_id), "institution": "auto", "sync_balance": "false"},
        )
        assert imp_res.status_code == 200, imp_res.text
        assert imp_res.json()["duplicate_count"] == 1
        assert imp_res.json()["imported_count"] == 0

        # Second Plaid delta sync: modify ptx_coffee_1 ($5.00 -> $6.25 tip settled) and remove ptx_temp_hold
        acct_norm_2 = NormalizedAccount(
            external_account_id="plaid_chk_1",
            name="Total Checking",
            account_type="checking",
            balance_cents=98_875,
            currency="USD",
            institution="Chase",
        )

        def mock_plaid_sync_2(self, access_token: str, cursor=None, institution: str = "Plaid"):
            assert cursor == "cursor_v1"
            return NormalizedSyncResult(
                provider="plaid",
                accounts=[acct_norm_2],
                added=[],
                modified=[
                    NormalizedTransaction(
                        external_id="ptx_coffee_1",
                        external_account_id="plaid_chk_1",
                        tx_date=today,
                        merchant="Blue Bottle Coffee",
                        amount_cents=625,
                        tx_type="expense",
                    )
                ],
                removed_external_ids=["ptx_temp_hold"],
                next_cursor="cursor_v2",
            )

        monkeypatch.setattr(PlaidClient, "sync_transactions", mock_plaid_sync_2)
        monkeypatch.setattr(PlaidClient, "get_accounts_balances", lambda self, tok, institution="Plaid": [acct_norm_2])
        r2 = client.post("/api/v1/plaid/sync", json={"connection_id": conn_id})
        assert r2.status_code == 200, r2.text
        body2 = r2.json()
        assert body2["modified_count"] == 1
        assert body2["removed_count"] == 1

        txs_after_2 = client.get("/api/v1/transactions").json()
        assert not any(t["merchant"] == "Gas Station Hold" for t in txs_after_2)
        coffee_amts = sorted(t["amount_cents"] for t in txs_after_2 if t["merchant"] == "Blue Bottle Coffee")
        assert coffee_amts == [500, 625]
        assert svc.storage.get_connection(conn_id)["sync_cursor"] == "cursor_v2"

        # Verify SimpleFIN sync also reads external_account_id cleanly without HTTP 500
        sfin_row = svc.storage.upsert_connection(
            {
                "provider": "simplefin",
                "item_id": "sfin_item_1",
                "institution": "SimpleFIN Credit Union",
                "env": "production",
                "status": "active",
            }
        )
        sfin_id = int(sfin_row["id"])
        svc.secrets.set_json(f"conn:{sfin_id}", {"access_url": "https://user:pass@bridge.simplefin.org/simplefin"})
        monkeypatch.setattr(
            SimpleFINClient,
            "fetch_accounts",
            lambda self, access_url, days=730: NormalizedSyncResult(
                provider="simplefin",
                accounts=[
                    NormalizedAccount(
                        external_account_id="sfin_chk_99",
                        name="CU Checking",
                        account_type="checking",
                        balance_cents=250_000,
                        currency="USD",
                        institution="SimpleFIN Credit Union",
                    )
                ],
                added=[
                    NormalizedTransaction(
                        external_id="sfin_tx_1",
                        external_account_id="sfin_chk_99",
                        tx_date=today,
                        merchant="Trader Joes",
                        amount_cents=4200,
                        tx_type="expense",
                    )
                ],
            ),
        )
        rs = client.post("/api/v1/simplefin/sync", json={"connection_id": sfin_id})
        assert rs.status_code == 200, rs.text
        assert rs.json()["imported_count"] == 1

    def test_finding_03_edit_and_delete_synced_history_preserves_bank_balance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, svc = _make_unlocked_client(tmp_path, monkeypatch)
        today = date.today().isoformat()

        acct = svc.storage.create_account(
            name="Synced Checking",
            account_type="checking",
            balance_cents=100_000,
        )
        # Provider-synced historical expense (adjust_balance=False -> affects_balance=0)
        synced_tx = svc.add_transaction_raw(
            account_id=int(acct["id"]),
            tx_type="expense",
            amount_cents=500,
            merchant="Corner Deli",
            tx_date=today,
            source="plaid_sync",
            external_id="ptx_deli_5",
            adjust_balance=False,
        )
        assert synced_tx is not None
        assert svc.storage.get_transaction(synced_tx.id)["affects_balance"] == 0
        assert svc.storage.get_account(acct["id"])["balance_cents"] == 100_000

        # Editing $5.00 -> $4.00 MUST NOT raise synced $1,000.00 balance to $1,001.00
        rp = client.patch(f"/api/v1/transactions/{synced_tx.id}", json={"amount_cents": 400})
        assert rp.status_code == 200
        assert svc.storage.get_account(acct["id"])["balance_cents"] == 100_000

        # Deleting historical expense MUST NOT raise synced $1,000.00 balance to $1,004.00 / $1,005.00
        rd = client.delete(f"/api/v1/transactions/{synced_tx.id}")
        assert rd.status_code == 200
        assert svc.storage.get_account(acct["id"])["balance_cents"] == 100_000

        # Manual transaction (affects_balance=1) DOES adjust balance cleanly on create/edit/delete
        rm = client.post(
            "/api/v1/transactions",
            json={
                "tx_type": "expense",
                "merchant": "Cash Lunch",
                "amount_cents": 1000,
                "tx_date": today,
                "account_id": acct["id"],
            },
        )
        assert rm.status_code == 200
        man_id = rm.json()["id"]
        assert svc.storage.get_account(acct["id"])["balance_cents"] == 99_000
        client.patch(f"/api/v1/transactions/{man_id}", json={"amount_cents": 600})
        assert svc.storage.get_account(acct["id"])["balance_cents"] == 99_400
        client.delete(f"/api/v1/transactions/{man_id}")
        assert svc.storage.get_account(acct["id"])["balance_cents"] == 100_000

    def test_finding_04_statement_account_routing_and_manual_transfer_balances(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, svc = _make_unlocked_client(tmp_path, monkeypatch)
        today = date.today().isoformat()

        chk = client.post(
            "/api/v1/accounts",
            json={"name": "Main Checking", "account_type": "checking", "balance_cents": 100_000, "is_liquid": True},
        ).json()
        sav = client.post(
            "/api/v1/accounts",
            json={"name": "Share Savings", "account_type": "savings", "balance_cents": 200_000, "is_liquid": True},
        ).json()

        # 1. Statement import targeting Savings explicitly debits Share Savings, leaving Main Checking at $1,000
        sav_csv = f"Date,Description,Amount\n{today},Emergency Repair,-50.00\n".encode("utf-8")
        r_imp = client.post(
            "/api/v1/import/statement",
            files={"file": ("savings_stmt.csv", io.BytesIO(sav_csv), "text/csv")},
            data={"account_id": str(sav["id"]), "sync_balance": "false"},
        )
        assert r_imp.status_code == 200, r_imp.text
        assert svc.storage.get_account(chk["id"])["balance_cents"] == 100_000
        assert svc.storage.get_account(sav["id"])["balance_cents"] == 195_000

        # 2. Manual $200 outbound transfer from Main Checking reduces Main Checking from $1,000 to $800
        r_tr = client.post(
            "/api/v1/transactions",
            json={
                "tx_type": "transfer",
                "transfer_direction": "out",
                "merchant": "Transfer to External Brokerage",
                "amount_cents": 20_000,
                "tx_date": today,
                "account_id": chk["id"],
            },
        )
        assert r_tr.status_code == 200, r_tr.text
        assert svc.storage.get_account(chk["id"])["balance_cents"] == 80_000

    def test_finding_05_safe_to_spend_multi_occurrence_bill_reserve_and_safe_to_invest_cap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, _svc = _make_unlocked_client(tmp_path, monkeypatch)
        today = date.today()
        payday = (today + timedelta(days=14)).isoformat()

        chk = client.post(
            "/api/v1/accounts",
            json={"name": "Checking", "account_type": "checking", "balance_cents": 50_000, "is_liquid": True},
        ).json()
        client.patch(
            "/api/v1/settings",
            json={
                "next_payday_date": payday,
                "payday_cadence_days": 14,
                "emergency_buffer_cents": 0,
                "expected_paycheck_cents": 0,
            },
        )

        # Weekly $100 bill due in 2 days -> TWO occurrences (day +2 and day +9) fall before day +14 payday ($200 reserved)
        r_bill = client.post(
            "/api/v1/bills",
            json={
                "name": "Childcare Co-op",
                "amount_cents": 10_000,
                "cadence": "weekly",
                "next_due_date": (today + timedelta(days=2)).isoformat(),
            },
        )
        assert r_bill.status_code == 200
        dash1 = client.get("/api/v1/dashboard").json()
        assert dash1["safe_to_spend"]["unpaid_bills_until_payday_cents"] == 20_000

        # A $1.00 purchase at the same merchant MUST NOT clear the $100 bill reserve!
        client.post(
            "/api/v1/transactions",
            json={
                "tx_type": "expense",
                "merchant": "Childcare Co-op Snack",
                "amount_cents": 100,
                "tx_date": today.isoformat(),
                "account_id": chk["id"],
            },
        )
        dash2 = client.get("/api/v1/dashboard").json()
        assert dash2["safe_to_spend"]["unpaid_bills_until_payday_cents"] == 20_000

        # Locked savings profile: $0 Checking, $150 Savings -> $0 safe to spend AND $0 safe to invest
        client2, _svc2 = _make_unlocked_client(tmp_path / "sub", monkeypatch)
        client2.post(
            "/api/v1/accounts",
            json={"name": "Locked Savings", "account_type": "savings", "balance_cents": 15_000, "is_liquid": True},
        )
        client2.patch("/api/v1/settings", json={"emergency_buffer_cents": 15_000})
        dash_locked = client2.get("/api/v1/dashboard").json()
        assert dash_locked["safe_to_spend"]["safe_to_spend_cents"] == 0
        assert dash_locked["safe_to_invest_cents"] == 0


class TestAuditFindings6To10:
    def test_finding_06_payday_detection_stale_guard_semimonthly_anchor_and_user_override_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        today = date(2026, 11, 24)
        # 1. Stale deposits (> 1 year old) must NOT produce high-confidence payday forecasts
        stale_txs = [
            {"tx_type": "income", "tx_date": "2025-05-01", "merchant": "ACME PAYROLL", "amount_cents": 220_000},
            {"tx_type": "income", "tx_date": "2025-05-15", "merchant": "ACME PAYROLL", "amount_cents": 220_000},
        ]
        stale_sched = detect_payday_schedule(stale_txs, today=today)
        assert stale_sched.confidence <= 0.25, "Stale >1y deposits must never receive high confidence"
        assert stale_sched.estimated_paycheck_cents == 0

        # 2. Semi-monthly 1st & 15th payroll lands on Dec 1 (NOT Nov 29 / Nov 30 drift)
        sm_txs = [
            {"tx_type": "income", "tx_date": "2026-10-01", "merchant": "ACME PAYROLL", "amount_cents": 250_000},
            {"tx_type": "income", "tx_date": "2026-10-15", "merchant": "ACME PAYROLL", "amount_cents": 250_000},
            {"tx_type": "income", "tx_date": "2026-11-01", "merchant": "ACME PAYROLL", "amount_cents": 250_000},
            {"tx_type": "income", "tx_date": "2026-11-15", "merchant": "ACME PAYROLL", "amount_cents": 250_000},
        ]
        sm_sched = detect_payday_schedule(sm_txs, today=today)
        assert sm_sched.cadence_days == 15
        assert sm_sched.next_payday_date == "2026-12-01"

        # 3. User-configured payday setting is NEVER overwritten by automatic detection
        client, svc = _make_unlocked_client(tmp_path, monkeypatch)
        user_payday = (date.today() + timedelta(days=21)).isoformat()
        client.patch(
            "/api/v1/settings",
            json={"next_payday_date": user_payday, "payday_cadence_days": 15, "expected_paycheck_cents": 300_000},
        )
        acct = client.post(
            "/api/v1/accounts",
            json={"name": "Checking", "account_type": "checking", "balance_cents": 200_000},
        ).json()
        for offset in (28, 14):
            client.post(
                "/api/v1/transactions",
                json={
                    "tx_type": "income",
                    "merchant": "DIRECT DEP PAYROLL",
                    "amount_cents": 200_000,
                    "tx_date": (date.today() - timedelta(days=offset)).isoformat(),
                    "account_id": acct["id"],
                },
            )
        svc.run_self_updating_sync(reason="test payday lock")
        s_after = client.get("/api/v1/settings").json()
        assert s_after["next_payday_date"] == user_payday
        assert s_after["expected_paycheck_cents"] == 300_000

    def test_finding_07_account_identity_credit_card_sign_and_currency_isolation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, svc = _make_unlocked_client(tmp_path, monkeypatch)
        client.post("/api/v1/plaid/config", json={"client_id": "cid_nfcu", "secret": "sec_nfcu"})
        conn_row = svc.storage.upsert_connection(
            {
                "provider": "plaid",
                "item_id": "nfcu_item",
                "institution": "Navy Federal",
                "env": "production",
                "status": "active",
            }
        )
        conn_id = int(conn_row["id"])
        svc.secrets.set_json(f"conn:{conn_id}", {"access_token": "tok_nfcu", "env": "production"})

        accts_list = [
            NormalizedAccount(
                external_account_id="nfcu_chk_a",
                name="Everyday Checking",
                account_type="checking",
                balance_cents=120_000,
                available_cents=115_000,
                currency="USD",
                institution="Navy Federal",
                mask="1111",
            ),
            NormalizedAccount(
                external_account_id="nfcu_chk_b",
                name="Everyday Checking",
                account_type="checking",
                balance_cents=80_000,
                available_cents=80_000,
                currency="USD",
                institution="Navy Federal",
                mask="2222",
            ),
            NormalizedAccount(
                external_account_id="nfcu_cc_1",
                name="cashRewards Visa",
                account_type="credit",
                balance_cents=-50_000,
                limit_cents=500_000,
                is_liquid=False,
                currency="USD",
                institution="Navy Federal",
                mask="9999",
            ),
            NormalizedAccount(
                external_account_id="nfcu_cad_1",
                name="Borderless Checking",
                account_type="checking",
                balance_cents=900_000,
                currency="CAD",
                institution="Navy Federal",
            ),
        ]

        monkeypatch.setattr(
            PlaidClient,
            "sync_transactions",
            lambda self, access_token, cursor=None, institution="Plaid": NormalizedSyncResult(
                provider="plaid", accounts=accts_list, added=[], modified=[], removed_external_ids=[], next_cursor="cur_nfcu"
            ),
        )
        monkeypatch.setattr(PlaidClient, "get_accounts_balances", lambda self, tok, institution="Plaid": accts_list)
        monkeypatch.setattr(PlaidClient, "get_liabilities", lambda self, tok, institution="Plaid": [])
        monkeypatch.setattr(PlaidClient, "get_investments_holdings", lambda self, tok: [])

        r = client.post("/api/v1/plaid/sync", json={"connection_id": conn_id})
        assert r.status_code == 200, r.text
        accts = client.get("/api/v1/accounts").json()
        same_name = [a for a in accts if a["name"] == "Everyday Checking"]
        assert len(same_name) == 2, "Same-name accounts with distinct external_account_id must not collapse"
        assert {a["mask"] for a in same_name} == {"1111", "2222"}

        dash = client.get("/api/v1/dashboard").json()
        sts = dash["safe_to_spend"]
        # USD liquid cash = $1,200 + $800 = $2,000 (CAD $9,000 excluded!), credit card reserve = $500
        assert sts["total_liquid_cash_cents"] == 200_000
        assert sts["credit_card_reserved_cents"] == 50_000

    def test_finding_08_net_worth_includes_unlinked_card_debt_and_historical_investments(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, svc = _make_unlocked_client(tmp_path, monkeypatch)

        # $1,000 cash + $500 credit account debt (without separate credit_line) => $500 net worth
        chk = client.post(
            "/api/v1/accounts",
            json={"name": "Checking", "account_type": "checking", "balance_cents": 100_000, "is_liquid": True},
        ).json()
        cc = client.post(
            "/api/v1/accounts",
            json={"name": "Sapphire Card", "account_type": "credit", "balance_cents": -50_000, "is_liquid": False},
        ).json()
        nw_res = client.get("/api/v1/analytics/net-worth?days=90").json()
        pts = nw_res if isinstance(nw_res, list) else nw_res.get("points", [])
        assert pts[-1]["net_worth_cents"] == 50_000
        assert pts[-1]["debt_cents"] == 50_000

        # Adding a linked credit_line for the same account_id must NOT double-count the $500 debt
        svc.storage.create_credit_line(
            {
                "account_id": cc["id"],
                "name": "Sapphire Card",
                "kind": "revolving",
                "balance_cents": 50_000,
                "credit_limit_cents": 500_000,
                "apr_bps": 2499,
            }
        )
        nw_res2 = client.get("/api/v1/analytics/net-worth?days=90").json()
        pts2 = nw_res2 if isinstance(nw_res2, list) else nw_res2.get("points", [])
        assert pts2[-1]["debt_cents"] == 50_000
        assert pts2[-1]["net_worth_cents"] == 50_000

        # Adding a $1,000 holding today must NOT back-project $1,000 investment valuation 60 days into the past
        client.post(
            "/api/v1/portfolio/holdings",
            json={
                "symbol": "VTI",
                "asset_class": "etf",
                "quantity_e8": 4 * 100_000_000,
                "price_e8": 250 * 100_000_000,
                "cost_basis_cents": 90_000,
                "account_id": chk["id"],
            },
        )
        nw_res3 = client.get("/api/v1/analytics/net-worth?days=90").json()
        pts3 = nw_res3 if isinstance(nw_res3, list) else nw_res3.get("points", [])
        assert pts3[-1]["investment_cents"] == 100_000
        assert pts3[0]["investment_cents"] == 0, "Historical dates prior to holding creation must not inherit today's valuation"

    def test_finding_09_broker_sync_prunes_sold_positions_and_preserves_multi_account_holdings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, svc = _make_unlocked_client(tmp_path, monkeypatch)
        conn_row = svc.storage.upsert_connection(
            {
                "provider": "schwab",
                "item_id": "schwab_item_1",
                "institution": "Charles Schwab",
                "env": "production",
                "status": "active",
            }
        )
        conn_id = int(conn_row["id"])
        future_iso = (date.today() + timedelta(days=5)).isoformat() + "T00:00:00Z"
        svc.secrets.set_json(
            f"conn:{conn_id}",
            {
                "client_id": "schwab_cid",
                "client_secret": "schwab_sec",
                "access_token": "schwab_acc_tok",
                "refresh_token": "schwab_ref_tok",
                "access_expires_at": future_iso,
                "refresh_expires_at": future_iso,
            },
        )

        # Sync 1: 1 share AAPL in IRA + 2 shares AAPL in Taxable = 3 shares total ($600 at $200/sh)
        monkeypatch.setattr(
            SchwabConnector,
            "fetch_holdings",
            lambda self, access_token: [
                NormalizedHolding(
                    symbol="AAPL",
                    name="Apple Inc.",
                    asset_class="equity",
                    quantity_e8=1 * 100_000_000,
                    price_e8=200 * 100_000_000,
                    market_value_cents=20_000,
                    cost_basis_cents=15_000,
                    cost_basis_known=True,
                    external_account_id="schwab_ira_01",
                    source="schwab",
                ),
                NormalizedHolding(
                    symbol="AAPL",
                    name="Apple Inc.",
                    asset_class="equity",
                    quantity_e8=2 * 100_000_000,
                    price_e8=200 * 100_000_000,
                    market_value_cents=40_000,
                    cost_basis_cents=32_000,
                    cost_basis_known=True,
                    external_account_id="schwab_tax_02",
                    source="schwab",
                ),
            ],
        )
        r1 = client.post(f"/api/v1/brokers/{conn_id}/sync")
        assert r1.status_code == 200, r1.text
        port1 = client.get("/api/v1/portfolio").json()
        assert len(port1["holdings"]) == 2
        assert port1["total_value_cents"] == 60_000

        # Sync 2: User sold all AAPL (broker returns 0 positions) -> portfolio must prune sold holdings!
        monkeypatch.setattr(SchwabConnector, "fetch_holdings", lambda self, access_token: [])
        r2 = client.post(f"/api/v1/brokers/{conn_id}/sync")
        assert r2.status_code == 200, r2.text
        port2 = client.get("/api/v1/portfolio").json()
        assert port2["holdings"] == []
        assert port2["total_value_cents"] == 0

    def test_finding_10_sandbox_credit_isolation_and_tradeline_merge_preserves_user_apr_and_due_day(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, _svc = _make_unlocked_client(tmp_path, monkeypatch)

        # User creates a real credit card with 29% APR and due day 26
        r_card = client.post(
            "/api/v1/credit/lines",
            json={
                "name": "Amex Gold",
                "kind": "credit_card",
                "balance_cents": 120_000,
                "credit_limit_cents": 1_000_000,
                "apr_bps": 2900,
                "statement_closing_day": 18,
                "payment_due_day": 26,
                "autopay_mode": "statement",
            },
        )
        assert r_card.status_code == 200, r_card.text
        real_card = r_card.json()
        assert real_card["apr_bps"] == 2900
        assert real_card["payment_due_day"] == 26

        # Connect a sandbox Credit API and sync an 820 score + 0% APR / due day 10 tradeline
        r_conn = client.post(
            "/api/v1/credit/apis/connect",
            json={
                "vendor": "spinwheel",
                "env": "sandbox",
                "api_key": "sbx_spin_key",
                "user_id": "sbx_usr_1",
                "test_on_connect": False,
            },
        )
        assert r_conn.status_code == 200, r_conn.text
        sbx_conn_id = r_conn.json()["connection"]["id"]

        monkeypatch.setattr(
            BureauAPIAdapter,
            "fetch_credit_snapshot",
            lambda self, **kwargs: CreditImportBundle(
                provider="spinwheel",
                reports=[
                    CreditReportRecord(
                        bureau="equifax",
                        model="fico8",
                        score=820,
                        pulled_date=date.today().isoformat(),
                        source="spinwheel_api",
                    )
                ],
                tradelines=[
                    CreditTradelineRecord(
                        name="Amex Gold",
                        kind="credit_card",
                        balance_cents=50_000,
                        credit_limit_cents=1_000_000,
                        apr_bps=0,
                        statement_closing_day=15,
                        payment_due_day=10,
                        apr_provided=False,
                        closing_day_provided=False,
                        due_day_provided=False,
                    )
                ],
                inquiries=[],
                derogatories=[],
                needs_review=False,
            ),
        )
        r_sync = client.post(f"/api/v1/credit/apis/{sbx_conn_id}/sync", json={"sync_tradelines": True})
        assert r_sync.status_code == 200, r_sync.text

        # Normal credit report MUST NOT include sandbox 820 score, and real Amex Gold MUST keep 29% APR & due day 26!
        rep = client.get("/api/v1/credit/report").json()
        assert all(s["score"] != 820 for s in rep["credit_reports"])
        real_lines = [cl for cl in rep["credit_lines"] if cl["id"] == real_card["id"]]
        assert len(real_lines) == 1
        assert real_lines[0]["apr_bps"] == 2900
        assert real_lines[0]["payment_due_day"] == 26
        assert real_lines[0]["balance_cents"] == 120_000

        # Production tradeline sync where provider omits APR/due_day updates balance without wiping 29% APR or due day 26
        prod_conn = client.post(
            "/api/v1/credit/apis/connect",
            json={
                "vendor": "spinwheel",
                "env": "production",
                "api_key": "prod_spin_key",
                "user_id": "prod_usr_1",
                "test_on_connect": False,
            },
        ).json()["connection"]["id"]
        client.post(f"/api/v1/credit/apis/{prod_conn}/sync", json={"sync_tradelines": True})
        rep2 = client.get("/api/v1/credit/report").json()
        assert any(s["score"] == 820 for s in rep2["credit_reports"])
        updated_card = [cl for cl in rep2["credit_lines"] if cl["id"] == real_card["id"]][0]
        assert updated_card["balance_cents"] == 50_000
        assert updated_card["apr_bps"] == 2900
        assert updated_card["payment_due_day"] == 26


class TestAuditFindings11To15:
    def test_finding_11_large_6mb_backup_export_preview_and_restore(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, svc = _make_unlocked_client(tmp_path, monkeypatch)
        acct = client.post(
            "/api/v1/accounts",
            json={"name": "Primary Checking", "account_type": "checking", "balance_cents": 321_000},
        ).json()
        # Pad memo rows so exported backup exceeds 6.2 MB (previously rejected with HTTP 413 at 5 MB!)
        big_memo = "X" * 4096
        today = date.today().isoformat()
        with svc.storage.transaction(immediate=True) as conn:
            conn.executemany(
                """
                INSERT INTO transactions (
                    tx_date, merchant, normalized_merchant, amount_cents, tx_type, jar_id, account_id, memo, source, created_at
                ) VALUES (?, ?, ?, ?, 'expense', NULL, ?, ?, 'manual', ?)
                """,
                [(today, f"Vendor {i}", f"vendor {i}", 100, acct["id"], big_memo, f"{today}T00:00:00Z") for i in range(1600)],
            )

        exp = client.get("/api/v1/backup/export")
        assert exp.status_code == 200
        raw_bytes = exp.content
        assert len(raw_bytes) > 6_200_000, f"Expected >6.2 MB backup, got {len(raw_bytes)} bytes"

        # Mutate current DB, then preview backup: preview MUST NOT overwrite current DB yet!
        client.post(
            "/api/v1/accounts",
            json={"name": "Temporary Account", "account_type": "savings", "balance_cents": 99_900},
        )
        prev = client.post(
            "/api/v1/backup/preview",
            files={"file": ("chronox_6mb.json", io.BytesIO(raw_bytes), "application/json")},
        )
        assert prev.status_code == 200, prev.text
        assert prev.json()["counts"]["transactions"] == 1600
        assert any(a["name"] == "Temporary Account" for a in client.get("/api/v1/accounts").json())

        # Confirmed import restores the 6.2 MB backup without HTTP 413
        imp = client.post(
            "/api/v1/backup/import",
            files={"file": ("chronox_6mb.json", io.BytesIO(raw_bytes), "application/json")},
        )
        assert imp.status_code == 200, imp.text
        assert not any(a["name"] == "Temporary Account" for a in client.get("/api/v1/accounts").json())

    def test_finding_12_unbounded_6000_row_analytics_and_csv_export(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, svc = _make_unlocked_client(tmp_path, monkeypatch)
        acct = client.post(
            "/api/v1/accounts",
            json={"name": "Ledger Checking", "account_type": "checking", "balance_cents": 5_000_000},
        ).json()
        today = date.today()
        rows = []
        for i in range(6000):
            d_iso = (today - timedelta(days=i % 700)).isoformat()
            rows.append((d_iso, f"Expense #{i}", f"expense {i}", 500, acct["id"], f"{d_iso}T12:00:00Z"))
        with svc.storage.transaction(immediate=True) as conn:
            conn.executemany(
                """
                INSERT INTO transactions (
                    tx_date, merchant, normalized_merchant, amount_cents, tx_type, jar_id, account_id, memo, source, created_at
                ) VALUES (?, ?, ?, ?, 'expense', NULL, ?, '', 'manual', ?)
                """,
                rows,
            )

        # 6,000 * $5.00 = $30,000.00 (3,000,000 cents) — must NOT truncate at 5,000 rows ($25,000)!
        ov = client.get("/api/v1/analytics/overview?period=2y").json()
        assert ov["total_expense_cents"] == 3_000_000

        csv_resp = client.get("/api/v1/export/csv")
        assert csv_resp.status_code == 200
        csv_lines = [ln for ln in csv_resp.text.strip().splitlines() if ln]
        assert len(csv_lines) == 6001  # 1 header + 6,000 rows

    def test_finding_13_balance_transfer_non_amortizing_guard_and_post_promo_apr(self):
        opt = CreditDebtOptimizer()
        # $10,000 at 24% APR = $200.00/mo interest -> $200/mo payment never pays down principal!
        res = opt.evaluate_balance_transfer(
            BalanceTransferRequest(
                balance_cents=1_000_000,
                current_apr_bps=2400,
                transfer_fee_bps=300,
                promo_months=15,
                monthly_payment_cents=20_000,
                post_promo_apr_bps=2999,
            )
        )
        assert res.current_payoff_possible is False
        assert res.post_promo_apr_bps == 2999
        assert "never" in res.explanation.lower() or "interest" in res.explanation.lower()

        # If transfer ALSO fails to amortize post-promo (e.g. $50/mo payment + 29.99% post-promo APR), verdict MUST be skip!
        res_bad = opt.evaluate_balance_transfer(
            BalanceTransferRequest(
                balance_cents=1_000_000,
                current_apr_bps=2400,
                transfer_fee_bps=500,
                promo_months=6,
                monthly_payment_cents=5_000,
                post_promo_apr_bps=2999,
            )
        )
        assert res_bad.transfer_payoff_possible is False
        assert res_bad.verdict == "skip"

    def test_finding_14_failed_pdf_returns_parse_failed_and_watch_folder_refuses_archive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, _svc = _make_unlocked_client(tmp_path, monkeypatch)
        bad_pdf = b"%PDF-1.4\n%corrupt unreadable binary stream without text\n%%EOF"
        r = client.post(
            "/api/v1/import/statement",
            files={"file": ("unreadable.pdf", io.BytesIO(bad_pdf), "application/pdf")},
            data={"institution": "auto"},
        )
        assert r.status_code == 200
        payload = r.json()
        assert payload["imported_count"] == 0
        assert payload["parse_failed"] is True
        assert len(payload["warnings"]) >= 1

        node_bin = shutil.which("node")
        if not node_bin:
            pytest.skip("node binary not available")
        watch_dir = tmp_path / "DropWatch"
        watch_dir.mkdir()
        pdf_file = watch_dir / "scan_unreadable.pdf"
        pdf_file.write_bytes(bad_pdf)

        node_script = """
        const http = require("http");
        const { WatchFolderController } = require("./desktop/src/main/watch-folder.js");
        const watchDir = process.argv[1];
        const responseBody = process.argv[2];
        const srv = http.createServer((req, res) => {
          req.on("data", () => {});
          req.on("end", () => {
            res.writeHead(200, { "Content-Type": "application/json" });
            res.end(responseBody);
          });
        });
        srv.listen(0, "127.0.0.1", async () => {
          const port = srv.address().port;
          const wf = new WatchFolderController({
            getBackendUrl: () => `http://127.0.0.1:${port}`,
            getCookieHeader: () => "chronox_session=1",
            getCsrfToken: () => "c1",
            onImported: () => {},
            onLog: () => {},
          });
          wf.configure({ enabled: true, folderPath: watchDir });
          await wf.scanNow();
          const st = wf.getStatus();
          wf.stop();
          srv.close();
          console.log(JSON.stringify(st));
        });
        """
        proc = subprocess.run(  # noqa: S603
            [node_bin, "-e", node_script, str(watch_dir), json.dumps(payload)],
            cwd=str(Path(__file__).resolve().parents[2]),
            capture_output=True,
            text=True,
            check=True,
        )
        wf_state = json.loads(proc.stdout.strip().splitlines()[-1])
        assert pdf_file.exists(), "Failed PDF must remain in watch folder for inspection, NOT moved to Imported/"
        assert not (watch_dir / "Imported" / "scan_unreadable.pdf").exists()
        assert wf_state["lastError"], "WatchFolderController must record lastError on 0-row failed PDF parse"

    def test_finding_15_wheel_includes_main_py_and_web_assets(self, tmp_path: Path):
        repo_root = Path(__file__).resolve().parents[2]
        uv_bin = shutil.which("uv")
        wheel_dir = tmp_path / "dist"
        wheel_dir.mkdir()
        had_build = (repo_root / "build").exists()
        had_egg_info = (repo_root / "chronox.egg-info").exists()
        try:
            built = False
            uv_err = ""
            pip_err = ""
            if uv_bin:
                uv_proc = subprocess.run(  # noqa: S603
                    [uv_bin, "build", "--wheel", "--offline", "--out-dir", str(wheel_dir)],
                    cwd=str(repo_root),
                    check=False,
                    capture_output=True,
                    text=True,
                )
                built = uv_proc.returncode == 0
                uv_err = uv_proc.stderr
            if not built:
                pip_proc = subprocess.run(  # noqa: S603
                    [
                        sys.executable,
                        "-m",
                        "pip",
                        "wheel",
                        ".",
                        "--no-deps",
                        "--no-build-isolation",
                        "--wheel-dir",
                        str(wheel_dir),
                    ],
                    cwd=str(repo_root),
                    check=False,
                    capture_output=True,
                    text=True,
                )
                built = pip_proc.returncode == 0
                pip_err = pip_proc.stderr
            if not built:
                pep517_proc = subprocess.run(  # noqa: S603
                    [
                        sys.executable,
                        "-c",
                        "import sys, setuptools.build_meta as bm; bm.build_wheel(sys.argv[1])",
                        str(wheel_dir),
                    ],
                    cwd=str(repo_root),
                    check=False,
                    capture_output=True,
                    text=True,
                )
                assert pep517_proc.returncode == 0, (
                    f"Wheel build failed.\nuv stderr:\n{uv_err}\n"
                    f"pip stderr:\n{pip_err}\npep517 stderr:\n{pep517_proc.stderr}"
                )
        finally:
            if not had_build:
                shutil.rmtree(repo_root / "build", ignore_errors=True)
            if not had_egg_info:
                shutil.rmtree(repo_root / "chronox.egg-info", ignore_errors=True)
        wheels = list(wheel_dir.glob("chronox-*.whl"))
        assert len(wheels) == 1
        with zipfile.ZipFile(wheels[0], "r") as zf:
            names = set(zf.namelist())
        assert "main.py" in names, "Built wheel must include main.py for chronox entry point"
        assert "chronox_finance/web/index.html" in names
        assert "chronox_finance/web/styles.css" in names
        assert "chronox_finance/web/js/vault.js" in names
        assert "chronox_finance/web/js/charts.js" in names
        assert "chronox_finance/web/js/portfolio.js" in names


class TestAuditFindings16To25:
    def test_finding_16_fresh_zero_profile_jars_forecast_and_month_rollover(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, svc = _make_unlocked_client(tmp_path, monkeypatch)

        dash = client.get("/api/v1/dashboard").json()
        assert dash["safe_to_spend"]["total_liquid_cash_cents"] == 0
        assert dash["safe_to_spend"]["safe_to_spend_cents"] == 0
        for jar in dash["jars"]:
            assert jar["available_cents"] == 0, f"Fresh $0 profile jar {jar['key']} must show $0 available"
            assert jar["funded_cents"] == 0
            assert jar["needs_setup"] is True

        # 90-day What-If forecast on fresh $0 profile ends at $0 (NOT $13,200!)
        sim = client.post("/api/v1/analytics/scenario-sim", json={"horizon_days": 90}).json()
        assert sim["baseline_points"][-1]["balance_cents"] == 0
        assert sim["scenario_points"][-1]["balance_cents"] == 0

        # Crossing a month boundary with $0 cash and 0 transactions rolls $0 forward!
        prev_month = (date.today().replace(day=1) - timedelta(days=10)).strftime("%Y-%m")
        svc.storage.update_settings({"last_rollover_ym": prev_month})
        dash_rolled = client.get("/api/v1/dashboard").json()
        assert svc.storage.get_settings()["last_rollover_ym"] == date.today().strftime("%Y-%m")
        assert all(j["rollover_cents"] == 0 for j in dash_rolled["jars"])
        assert all(j["available_cents"] == 0 for j in dash_rolled["jars"])

    def test_finding_17_rolling_period_chart_matches_kpi_and_single_month_omits_future_months(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, _svc = _make_unlocked_client(tmp_path, monkeypatch)
        acct = client.post(
            "/api/v1/accounts",
            json={"name": "Checking", "account_type": "checking", "balance_cents": 200_000},
        ).json()
        today = date.today()
        d_outside = (today - timedelta(days=32)).isoformat()
        d_inside = (today - timedelta(days=3)).isoformat()
        client.post(
            "/api/v1/transactions",
            json={"tx_type": "expense", "merchant": "Prior Spend", "amount_cents": 10_000, "tx_date": d_outside, "account_id": acct["id"]},
        )
        client.post(
            "/api/v1/transactions",
            json={"tx_type": "expense", "merchant": "Recent Spend", "amount_cents": 5_000, "tx_date": d_inside, "account_id": acct["id"]},
        )
        ov30 = client.get("/api/v1/analytics/overview?period=30d").json()
        assert ov30["total_expense_cents"] == 5_000
        plotted_sum = sum(m["expense_cents"] for m in ov30["monthly_trends"])
        assert plotted_sum == ov30["total_expense_cents"] == 5_000

        cur_ym = today.strftime("%Y-%m")
        ov_m = client.get(f"/api/v1/analytics/overview?period={cur_ym}&month={cur_ym}").json()
        assert all(pt["month"] <= cur_ym for pt in ov_m["monthly_trends"])

    def test_finding_18_sankey_labels_unfunded_gap_and_unallocated_surplus_honestly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, _svc = _make_unlocked_client(tmp_path, monkeypatch)
        acct = client.post(
            "/api/v1/accounts",
            json={"name": "Checking", "account_type": "checking", "balance_cents": 200_000},
        ).json()
        today = date.today().isoformat()

        # Case A: $1,000 income and $400 spending without savings transfer -> Unallocated Surplus ($600)
        client.post(
            "/api/v1/transactions",
            json={"tx_type": "income", "merchant": "Payroll", "amount_cents": 100_000, "tx_date": today, "account_id": acct["id"]},
        )
        client.post(
            "/api/v1/transactions",
            json={"tx_type": "expense", "merchant": "Groceries", "amount_cents": 40_000, "tx_date": today, "account_id": acct["id"]},
        )
        ov_surplus = client.get("/api/v1/analytics/overview?period=30d").json()
        labels_surplus = {n["label"]: n["amount_cents"] for n in ov_surplus["sankey_nodes"]}
        assert "Unallocated Surplus" in labels_surplus
        assert labels_surplus["Unallocated Surplus"] == 60_000
        assert "Savings & Invest" not in labels_surplus

        # Case B: Add $1,100 expense -> $1,000 income vs $1,500 spending -> Unfunded Gap ($500) + Total Outflow ($1,500)
        client.post(
            "/api/v1/transactions",
            json={"tx_type": "expense", "merchant": "Rent", "amount_cents": 110_000, "tx_date": today, "account_id": acct["id"]},
        )
        ov_def = client.get("/api/v1/analytics/overview?period=30d").json()
        labels_def = {n["label"]: n["amount_cents"] for n in ov_def["sankey_nodes"]}
        assert labels_def.get("Unfunded Gap") == 50_000
        assert labels_def.get("Total Outflow") == 150_000
        assert "Cash Reserves Draw" not in labels_def
        assert "Total Income" not in labels_def

    def test_finding_19_wcag_aa_contrast_ratios_and_ui_simulator_sandbox_hygiene(self):
        web_dir = Path("chronox_finance/web")
        css = (web_dir / "styles.css").read_text(encoding="utf-8")
        html = (web_dir / "index.html").read_text(encoding="utf-8")

        dark_block = css.split('[data-theme="light"]')[0]
        light_block = css.split('[data-theme="light"]')[1].split("*, *::before")[0]

        def extract_vars(block: str) -> dict[str, str]:
            return dict(re.findall(r"(--[a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{6})", block))

        dark_vars = extract_vars(dark_block)
        light_vars = extract_vars(light_block)

        for theme_name, tv in [("dark", dark_vars), ("light", light_vars)]:
            for bg_key in ("--surface", "--bg"):
                bg = tv[bg_key]
                for fg_key in (
                    "--chart-inc-text",
                    "--chart-exp-text",
                    "--chart-sur-text",
                    "--chart-def-text",
                    "--chart-inv-text",
                ):
                    fg = tv[fg_key]
                    ratio = _contrast_ratio(fg, bg)
                    assert ratio >= 4.5, f"{theme_name} {fg_key} ({fg}) vs {bg_key} ({bg}) = {ratio:.2f}:1 < 4.5:1 WCAG AA"

        # Verify timeline.js & charts.js do not hardcode low-contrast hex text colors and match Sankey labels
        tl_js = (web_dir / "js" / "timeline.js").read_text(encoding="utf-8")
        ch_js = (web_dir / "js" / "charts.js").read_text(encoding="utf-8")
        vault_js = (web_dir / "js" / "vault.js").read_text(encoding="utf-8")
        assert 'color: "#06b6d4"' not in tl_js
        assert 'color: "#10b981"' not in tl_js
        assert 'color: "#3b82f6"' not in tl_js
        assert "Unfunded Gap" in ch_js
        assert "Unallocated Surplus" in ch_js
        assert "exRes.id" in vault_js
        assert "prev.date_range" in vault_js

        assert ".sankey-mobile-stack" in css
        assert "@media (max-width: 680px)" in css

        # Verify simulator & sandbox controls are purged from end-user HTML & JS
        assert "5-FACTOR CREDIT SIMULATOR" not in html.upper()
        assert "LIVE UTILIZATION CLIFF SIMULATOR" not in html.upper()
        assert "4-STRATEGY DEBT PAYOFF SIMULATOR" not in html.upper()
        assert 'value="sandbox"' not in html.lower()
        for js_path in (web_dir / "js").glob("*.js"):
            js_txt = js_path.read_text(encoding="utf-8")
            assert "LIVE UTILIZATION CLIFF SIMULATOR" not in js_txt.upper()
            assert "5-FACTOR CREDIT SIMULATOR" not in js_txt.upper()

    def test_finding_20_21_22_23_connector_auth_502_plaid_remove_env_routing_and_backfill_status(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, svc = _make_unlocked_client(tmp_path, monkeypatch)

        # Finding #20: SimpleFIN 401 auth failure returns 502 connector_auth_error (NOT 401 vault lock) & sets needs_reauth
        sfin_row = svc.storage.upsert_connection(
            {
                "provider": "simplefin",
                "item_id": "bad_sfin",
                "institution": "Bad SimpleFIN",
                "env": "production",
                "status": "active",
            }
        )
        sfin_id = int(sfin_row["id"])
        svc.secrets.set_json(f"conn:{sfin_id}", {"access_url": "https://bad:creds@bridge.simplefin.org/simplefin"})
        monkeypatch.setattr(
            SimpleFINClient,
            "fetch_accounts",
            lambda self, url, days=730: (_ for _ in ()).throw(
                ConnectorAuthError("SimpleFIN credentials rejected (401)", provider="simplefin")
            ),
        )
        r_auth = client.post("/api/v1/simplefin/sync", json={"connection_id": sfin_id})
        assert r_auth.status_code == 502, f"Expected 502 for provider auth error, got {r_auth.status_code}"
        assert r_auth.json()["code"] == "connector_auth_error"
        assert client.get("/api/v1/auth/status").json()["unlocked"] is True
        conns = {c["id"]: c for c in client.get("/api/v1/connections").json()}
        assert conns[sfin_id]["status"] == "needs_reauth"
        assert "rejected" in (conns[sfin_id]["last_error"] or "")

        # Finding #22 & #23: Sandbox Plaid connection keeps routing to sandbox even after global config changes to production
        sbx_row = svc.storage.upsert_connection(
            {
                "provider": "plaid",
                "item_id": "sbx_item",
                "institution": "Sandbox Bank",
                "env": "sandbox",
                "status": "active",
            }
        )
        sbx_id = int(sbx_row["id"])
        svc.secrets.set_json(
            f"conn:{sbx_id}",
            {"access_token": "access-sandbox-111", "env": "sandbox", "client_id": "sbx_cid", "secret": "sbx_sec"},
        )

        prod_row = svc.storage.upsert_connection(
            {
                "provider": "plaid",
                "item_id": "prod_item",
                "institution": "Prod Bank",
                "env": "production",
                "status": "active",
            }
        )
        prod_id = int(prod_row["id"])
        svc.secrets.set_json(f"conn:{prod_id}", {"access_token": "access-prod-222", "env": "production"})
        client.post("/api/v1/plaid/config", json={"client_id": "prod_cid", "secret": "prod_sec"})

        called_envs: list[tuple[str, str]] = []
        today = date.today().isoformat()
        sbx_acct = NormalizedAccount(
            external_account_id="sbx_acct",
            name="Sandbox Checking",
            account_type="checking",
            balance_cents=50_000,
        )

        def mock_env_sync(self, access_token: str, cursor=None, institution: str = "Plaid"):
            called_envs.append((self.env, access_token))
            return NormalizedSyncResult(
                provider="plaid",
                accounts=[sbx_acct],
                added=[
                    NormalizedTransaction(
                        external_id="sbx_tx_recent",
                        external_account_id="sbx_acct",
                        tx_date=today,
                        merchant="Recent Shop",
                        amount_cents=1200,
                        tx_type="expense",
                    )
                ],
                modified=[],
                removed_external_ids=[],
                next_cursor="sbx_cur_1",
            )

        monkeypatch.setattr(PlaidClient, "sync_transactions", mock_env_sync)
        monkeypatch.setattr(PlaidClient, "get_accounts_balances", lambda self, tok, institution="Plaid": [sbx_acct])
        monkeypatch.setattr(PlaidClient, "get_liabilities", lambda self, tok, institution="Plaid": [])
        monkeypatch.setattr(PlaidClient, "get_investments_holdings", lambda self, tok: [])

        r_sbx_sync = client.post("/api/v1/plaid/sync", json={"connection_id": sbx_id})
        assert r_sbx_sync.status_code == 200, r_sbx_sync.text
        assert called_envs == [("sandbox", "access-sandbox-111")], "Per-row sync must use connection's stored env & sync ONLY that connection"
        sync_body = r_sbx_sync.json()
        assert sync_body["backfill_status"] == "backfill_pending"
        assert "730-day transaction sync complete" not in sync_body["history_note"]

        # Finding #21: Removing Plaid connection confirms provider /item/remove BEFORE deleting local token
        remove_calls: list[tuple[str, str]] = []

        def mock_remove_fail(self, access_token: str):
            raise ConnectorError("Plaid upstream timeout during /item/remove", provider="plaid")

        monkeypatch.setattr(PlaidClient, "remove_item", mock_remove_fail)
        r_del_fail = client.delete(f"/api/v1/connections/{sbx_id}")
        assert r_del_fail.status_code in (400, 502)
        assert svc.secrets.get_json(f"conn:{sbx_id}") is not None

        def mock_remove_ok(self, access_token: str):
            remove_calls.append((self.env, access_token))
            return {"removed": True}

        monkeypatch.setattr(PlaidClient, "remove_item", mock_remove_ok)
        r_del_ok = client.delete(f"/api/v1/connections/{sbx_id}")
        assert r_del_ok.status_code == 200
        assert remove_calls == [("sandbox", "access-sandbox-111")]
        assert svc.secrets.get_json(f"conn:{sbx_id}") is None

    def test_finding_24_and_25_schwab_loopback_tls_and_manual_complete_plus_unknown_cost_basis_pnl(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, svc = _make_unlocked_client(tmp_path, monkeypatch)

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]

        cb_url = f"https://127.0.0.1:{free_port}/api/v1/brokers/schwab/callback"
        r_conn = client.post(
            "/api/v1/brokers/schwab/connect",
            json={
                "client_id": "schwab_key_1",
                "client_secret": "schwab_secret_1",
                "redirect_uri": cb_url,
                "label": "Schwab Main",
            },
        )
        assert r_conn.status_code == 200, r_conn.text
        conn_body = r_conn.json()
        state_tok = conn_body["state"]
        assert conn_body["callback_listener_active"] is True
        assert str(free_port) in conn_body["authorize_url"]

        future_iso = (date.today() + timedelta(days=7)).isoformat() + "T00:00:00Z"
        monkeypatch.setattr(
            SchwabConnector,
            "exchange_code",
            lambda self, code: SchwabTokenBundle(
                access_token=f"schwab_tok_{code}",
                refresh_token="schwab_ref_1",
                obtained_at=date.today().isoformat() + "T00:00:00Z",
                access_expires_at=future_iso,
                refresh_expires_at=future_iso,
            ),
        )
        r_comp = client.post(
            "/api/v1/brokers/schwab/complete",
            json={
                "callback_url_or_code": f"{cb_url}?code=AUTH_CODE_777&state={state_tok}",
                "redirect_uri": cb_url,
            },
        )
        assert r_comp.status_code == 200, r_comp.text
        assert r_comp.json()["status"] == "active"
        svc._stop_schwab_callback_server()

        # Finding #25: Unknown cost basis holding must NEVER fabricate +100% Unrealized P&L
        csv_no_basis = b"Symbol,Quantity,Price\nNVDA,10,120.00\n"
        r_csv = client.post(
            "/api/v1/portfolio/import-csv",
            files={"file": ("positions.csv", io.BytesIO(csv_no_basis), "text/csv")},
        )
        assert r_csv.status_code == 200, r_csv.text
        port = client.get("/api/v1/portfolio").json()
        assert port["total_value_cents"] == 120_000
        assert port["unknown_cost_basis_value_cents"] == 120_000
        assert port["known_cost_basis_value_cents"] == 0
        assert port["cost_basis_coverage_pct"] == 0.0
        assert port["unrealized_pnl_cents"] == 0, "Uncosted holdings must not report full market value as P&L"
        assert port["unrealized_pnl_pct"] == 0.0
        assert port["holdings"][0]["cost_basis_known"] is False

        # Add a second holding WITH known cost basis ($500 basis -> $600 market value => +$100 / +20% P&L, 33.3% coverage)
        client.post(
            "/api/v1/portfolio/holdings",
            json={
                "symbol": "MSFT",
                "asset_class": "equity",
                "quantity_e8": 2 * 100_000_000,
                "price_e8": 300 * 100_000_000,
                "cost_basis_cents": 50_000,
                "cost_basis_known": True,
            },
        )
        port_mixed = client.get("/api/v1/portfolio").json()
        assert port_mixed["total_value_cents"] == 180_000
        assert port_mixed["known_cost_basis_value_cents"] == 60_000
        assert port_mixed["unknown_cost_basis_value_cents"] == 120_000
        assert port_mixed["known_cost_basis_holdings_count"] == 1
        assert port_mixed["unknown_cost_basis_holdings_count"] == 1
        assert port_mixed["unrealized_pnl_cents"] == 10_000
        assert port_mixed["unrealized_pnl_pct"] == 20.0
        assert port_mixed["cost_basis_coverage_pct"] == pytest.approx(33.3, abs=0.1)

    def test_review_round_edge_cases_payday_rollover_plaid_holdings_and_zero_basis(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, svc = _make_unlocked_client(tmp_path, monkeypatch)

        # 1. Saving unrelated ADHD settings (hourly_wage_cents) while sending unchanged payday defaults
        #    MUST NOT lock payday_user_configured=1 and block future payroll auto-detection!
        s0 = client.get("/api/v1/settings").json()
        client.patch(
            "/api/v1/settings",
            json={
                "hourly_wage_cents": 4500,
                "payday_cadence_days": s0["payday_cadence_days"],
                "next_payday_date": s0["next_payday_date"],
                "expected_paycheck_cents": 0,
            },
        )
        assert svc.storage.get_settings()["payday_user_configured"] == 0

        # 2. 15th & 30th fallback payday stepping from Nov 30 across Dec 1 lands on Dec 15 (not Dec 31!)
        sm_30 = detect_payday_schedule(
            transactions=[
                {"tx_type": "income", "tx_date": "2026-11-15", "amount_cents": 240_000},
                {"tx_type": "income", "tx_date": "2026-11-30", "amount_cents": 240_000},
            ],
            fallback_cadence_days=15,
            fallback_payday_date="2026-11-30",
            fallback_paycheck_cents=0,
            today=date(2026, 12, 1),
            user_configured_payday=True,
        )
        assert sm_30.next_payday_date == "2026-12-15"
        assert sm_30.estimated_paycheck_cents == 240_000

        # 3. Known $0 cost-basis grant/RSU holding preserves cost_basis_known=True
        r_rsu = client.post(
            "/api/v1/portfolio/holdings",
            json={
                "symbol": "GOOG",
                "asset_class": "equity",
                "quantity_e8": 1 * 100_000_000,
                "price_e8": 175 * 100_000_000,
                "cost_basis_cents": 0,
                "cost_basis_known": True,
            },
        )
        assert r_rsu.status_code == 200
        assert r_rsu.json()["cost_basis_known"] is True
        assert r_rsu.json()["unrealized_pnl_cents"] == 17_500

        # 4. Plaid JSON credit-card statement import negates positive debt & marks payment inflow as 'in'
        plaid_cc_json = json.dumps(
            {
                "accounts": [
                    {
                        "account_id": "cc_ext_1",
                        "name": "Rewards Credit Card",
                        "type": "credit",
                        "subtype": "credit card",
                        "balances": {"current": 500.00},
                    }
                ],
                "transactions": [
                    {
                        "transaction_id": "cc_pay_1",
                        "account_id": "cc_ext_1",
                        "date": date.today().isoformat(),
                        "name": "ONLINE PAYMENT THANK YOU",
                        "amount": -200.00,
                        "personal_finance_category": {
                            "primary": "LOAN_PAYMENTS",
                            "detailed": "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
                        },
                    }
                ],
            }
        ).encode("utf-8")
        r_cc_imp = client.post(
            "/api/v1/import/statement",
            files={"file": ("plaid_cc.json", io.BytesIO(plaid_cc_json), "application/json")},
            data={"sync_balance": "true"},
        )
        assert r_cc_imp.status_code == 200, r_cc_imp.text
        cc_body = r_cc_imp.json()
        assert cc_body["target_account_name"]
        cc_acct = svc.storage.get_account(cc_body["target_account_id"])
        assert cc_acct["balance_cents"] == -50_000
        cc_txs = svc.storage.list_transactions(account_id=cc_body["target_account_id"])
        assert cc_txs[0]["transfer_direction"] == "in"

        # 5. Plaid holdings identity stays stable across sync_plaid -> sync_broker (preserving created_at & account_id)
        client.post("/api/v1/plaid/config", json={"client_id": "cid_hold", "secret": "sec_hold"})
        p_conn = svc.storage.upsert_connection(
            {"provider": "plaid", "item_id": "it_hold", "institution": "Fidelity", "env": "production", "status": "active"}
        )
        pid = int(p_conn["id"])
        svc.secrets.set_json(f"conn:{pid}", {"access_token": "tok_hold", "env": "production"})
        inv_acct = NormalizedAccount(
            external_account_id="fid_ira_1",
            name="Roth IRA",
            account_type="trading",
            is_liquid=False,
            balance_cents=0,
            institution="Fidelity",
        )
        inv_h = NormalizedHolding(
            symbol="SPY",
            name="SPDR S&P 500",
            asset_class="etf",
            quantity_e8=2 * 100_000_000,
            price_e8=500 * 100_000_000,
            cost_basis_cents=80_000,
            cost_basis_known=True,
            external_account_id="fid_ira_1",
            source="plaid",
        )
        monkeypatch.setattr(
            PlaidClient,
            "sync_transactions",
            lambda self, tok, cursor=None, institution="Plaid": NormalizedSyncResult(
                provider="plaid", accounts=[inv_acct], added=[], modified=[], removed_external_ids=[], next_cursor="c1"
            ),
        )
        monkeypatch.setattr(PlaidClient, "get_accounts_balances", lambda self, tok, institution="Plaid": [inv_acct])
        monkeypatch.setattr(PlaidClient, "get_liabilities", lambda self, tok, institution="Plaid": [])
        monkeypatch.setattr(PlaidClient, "get_investments_holdings", lambda self, tok: [inv_h])

        assert client.post("/api/v1/plaid/sync", json={"connection_id": pid}).status_code == 200
        spy_before = [h for h in svc.storage.list_holdings() if h["symbol"] == "SPY"][0]
        assert spy_before["account_id"] is not None

        assert client.post(f"/api/v1/brokers/{pid}/sync").status_code == 200
        spy_after = [h for h in svc.storage.list_holdings() if h["symbol"] == "SPY"][0]
        assert spy_after["id"] == spy_before["id"]
        assert spy_after["account_id"] == spy_before["account_id"]
        assert spy_after["created_at"] == spy_before["created_at"]

        # 6. storage.update_transaction persists fingerprint & account_id updates cleanly
        chk_a = svc.storage.create_account(name="Chk A", account_type="checking", balance_cents=10_000)
        chk_b = svc.storage.create_account(name="Chk B", account_type="checking", balance_cents=10_000)
        tx_m = svc.add_transaction_raw(
            account_id=int(chk_a["id"]),
            tx_type="expense",
            amount_cents=1_000,
            merchant="Lunch",
            tx_date=date.today().isoformat(),
            source="manual",
            adjust_balance=True,
            run_auto_sync=False,
        )
        assert tx_m is not None
        assert svc.storage.get_account(chk_a["id"])["balance_cents"] == 9_000
        svc.storage.update_transaction(tx_m.id, {"account_id": int(chk_b["id"]), "fingerprint": "fp_updated_123"})
        updated_tx_row = svc.storage.get_transaction(tx_m.id)
        assert updated_tx_row["fingerprint"] == "fp_updated_123"
        assert updated_tx_row["account_id"] == int(chk_b["id"])
        assert svc.storage.get_account(chk_a["id"])["balance_cents"] == 10_000
        assert svc.storage.get_account(chk_b["id"])["balance_cents"] == 9_000

        # 7. Month-end rollover never carries phantom surplus when actual liquid cash is $0
        client2, svc2 = _make_unlocked_client(tmp_path / "zero_liq_roll", monkeypatch)
        z_acct = svc2.storage.create_account(name="Zero Checking", account_type="checking", balance_cents=0)
        svc2.add_transaction_raw(
            account_id=int(z_acct["id"]),
            tx_type="income",
            amount_cents=300_000,
            merchant="Oct Payroll",
            tx_date="2026-10-15",
            source="manual",
            adjust_balance=False,
            run_auto_sync=False,
        )
        svc2.run_self_updating_sync(reason="Oct sync", today=date(2026, 10, 28))
        svc2.run_self_updating_sync(reason="Nov roll", today=date(2026, 11, 2))
        assert all(int(j["rollover_cents"]) == 0 for j in svc2.storage.list_jars())
        del client2


class TestRound2SimulatorAndInventedConstantPurge:
    """Verify complete removal of CreditFactorSimulator, fabricated point deltas, invented constants, and What-If framing."""

    def test_zero_credit_simulator_models_fabricated_pts_and_what_if_copy(self):
        import chronox_finance.engines.credit_debt as credit_debt_mod
        import chronox_finance.engines.quant_math as quant_math_mod
        import chronox_finance.models as models_mod
        from chronox_finance.ai.codex_client import CHRONOX_STATIC_KNOWLEDGE_PREFIX

        # Removed simulator symbols & invented constants must not exist
        assert not hasattr(credit_debt_mod, "CreditFactorSimulator")
        assert not hasattr(credit_debt_mod, "FACTOR_SIMULATOR_DISCLAIMER")
        assert hasattr(credit_debt_mod, "CreditFactorAnalyzer")
        assert not hasattr(models_mod, "CreditSimulatedAction")
        assert not hasattr(quant_math_mod, "LIQUIDITY_INSURANCE_EQUIV_PCT")

        # AI prompt prefix must not describe a 5-Factor Credit Simulator
        assert "CREDIT SIMULATOR" not in CHRONOX_STATIC_KNOWLEDGE_PREFIX.upper()
        assert "CREDIT FACTOR ANALYSIS" in CHRONOX_STATIC_KNOWLEDGE_PREFIX.upper()

        # Web HTML, JS, Desktop menu, and README must be free of What-If / Simulator / UI sandbox strings
        web_dir = Path("chronox_finance/web")
        html = (web_dir / "index.html").read_text(encoding="utf-8")
        readme = Path("README.md").read_text(encoding="utf-8")
        menu_js = Path("desktop/src/main/menu.js").read_text(encoding="utf-8")

        for label, txt in [("index.html", html), ("README.md", readme), ("menu.js", menu_js)]:
            assert "WHAT-IF" not in txt.upper(), f"Residual What-If copy in {label}"
            assert "CREDIT SIMULATOR" not in txt.upper(), f"Residual Credit Simulator copy in {label}"

        assert "sandbox" not in re.sub(r"<!--[\s\S]*?-->", "", html).lower()
        assert 'value="24.99"' not in html

        for js_path in sorted((web_dir / "js").glob("*.js")):
            js_txt = js_path.read_text(encoding="utf-8")
            assert "WHAT-IF" not in js_txt.upper(), f"Residual What-If copy in {js_path.name}"
            assert "factor_simulator" not in js_txt, f"Residual factor_simulator in {js_path.name}"
            assert "estimated_delta_range" not in js_txt, f"Residual estimated_delta_range in {js_path.name}"
            assert '"SANDBOX"' not in js_txt and '"SANDBOX (ISOLATED)"' not in js_txt

    def test_goal_planner_zero_invented_2499_apr_and_quant_emergency_buffer_cash_yield(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from chronox_finance.engines.quant_math import SAFETY_HYSA_ANNUAL_PCT, QuantMathEngine
        from chronox_finance.models import GoalPlanRequest

        client, svc = _make_unlocked_client(tmp_path, monkeypatch)
        try:
            # 1. Fresh profile with 0 credit cards -> blocked credit path MUST have interest_cost_cents == 0 (never 24.99%!)
            empty_plan = svc.optimize_goal_plan(GoalPlanRequest(item_name="Camera Lens", target_cents=100_000))
            cred_opt_empty = next(o for o in empty_plan.options if o.path_id == "credit_optimized_buy")
            assert cred_opt_empty.credit_safe is False
            assert cred_opt_empty.interest_cost_cents == 0

            # 2. Profile with a real non-grace card at 18.00% APR -> uses actual 18.00% APR amortization, not 24.99%
            svc.storage.create_credit_line(
                {
                    "name": "Actual 18% Card",
                    "kind": "credit_card",
                    "balance_cents": 150_000,
                    "credit_limit_cents": 200_000,
                    "apr_bps": 1800,
                    "has_grace_period": False,
                    "autopay_mode": "minimum",
                }
            )
            card_plan = svc.optimize_goal_plan(GoalPlanRequest(item_name="Camera Lens", target_cents=100_000))
            cred_opt_card = next(o for o in card_plan.options if o.path_id == "credit_optimized_buy")
            assert cred_opt_card.credit_safe is False
            assert cred_opt_card.interest_cost_cents > 0
            assert cred_opt_card.interest_cost_cents != int(round(100_000 * 0.2499 / 2.0))

            # 3. Credit report factor_analysis has zero fabricated point-delta strings
            rep = client.get("/api/v1/credit/report").json()
            assert "factor_simulator" not in rep
            fa = rep["factor_analysis"]
            assert "simulated_actions" not in fa
            for item in fa["fico8_factors"] + fa["vantage3_factors"]:
                assert not re.search(r"\bpts?\b", item["summary"], re.I)
                assert not re.search(r"\bpts?\b", item["guidance"], re.I)
            for alert in rep["alerts"]:
                assert not re.search(r"\b\d+[–-]\d+\s*points?\b", alert["detail"], re.I)

            # 4. Quant emergency buffer uses SAFETY_HYSA_ANNUAL_PCT (4.25%), never invented 9.0% shock-avoidance
            steps = QuantMathEngine().optimize_surplus_allocation(
                monthly_surplus_cents=50_000,
                emergency_buffer_gap_cents=50_000,
            )
            buf_step = next(s for s in steps if s.bucket == "emergency_buffer")
            assert buf_step.effective_apr_equivalent_pct == SAFETY_HYSA_ANNUAL_PCT
            assert "shock-avoidance" not in buf_step.formula_note.lower()
        finally:
            del client
            svc.close()

    def test_credit_factor_analyzer_all_branches_emit_zero_fabricated_point_deltas(self):
        from chronox_finance.engines.credit_debt import CreditDebtOptimizer

        opt = CreditDebtOptimizer()
        ref = date(2026, 11, 20)

        scenarios = [
            # Branch 1: Empty profile
            {"lines": [], "inquiries": [], "derogs": []},
            # Branch 2: All-zero penalty (2 cards, $0 balance, mature age >=84m, optimal inquiries)
            {
                "lines": [
                    {"id": 1, "name": "Anchor Card", "kind": "credit_card", "balance_cents": 0, "credit_limit_cents": 1_000_000, "apr_bps": 1999, "opened_date": "2015-01-01", "autopay_mode": "statement", "active": 1},
                    {"id": 2, "name": "Second Card", "kind": "credit_card", "balance_cents": 0, "credit_limit_cents": 500_000, "apr_bps": 2199, "opened_date": "2018-01-01", "autopay_mode": "statement", "active": 1},
                ],
                "inquiries": [],
                "derogs": [],
            },
            # Branch 3: Mild late_30 derog + 15% good util + 50m age + 2 inquiries + installment mix
            {
                "lines": [
                    {"id": 1, "name": "Card A", "kind": "credit_card", "balance_cents": 75_000, "credit_limit_cents": 500_000, "apr_bps": 2199, "opened_date": "2022-09-01", "autopay_mode": "none", "active": 1},
                    {"id": 2, "name": "Auto Loan", "kind": "auto_loan", "balance_cents": 800_000, "credit_limit_cents": 0, "apr_bps": 599, "opened_date": "2022-09-01", "autopay_mode": "minimum", "active": 1},
                ],
                "inquiries": [
                    {"id": 1, "lender": "L1", "bureau": "experian", "inquiry_date": "2026-06-01"},
                    {"id": 2, "lender": "L2", "bureau": "equifax", "inquiry_date": "2026-07-01"},
                ],
                "derogs": [{"id": 1, "kind": "late_30", "lender": "Card A", "opened_date": "2024-01-10", "active": True}],
            },
            # Branch 4: Severe derog + maxed 92% util + young <24m age + >=6 inquiries
            {
                "lines": [
                    {"id": 1, "name": "Maxed Card", "kind": "credit_card", "balance_cents": 460_000, "credit_limit_cents": 500_000, "apr_bps": 2999, "opened_date": "2026-03-01", "autopay_mode": "none", "active": 1},
                ],
                "inquiries": [
                    {"id": i, "lender": f"Bank {i}", "bureau": "experian", "inquiry_date": f"2026-0{i}-10"}
                    for i in range(1, 7)
                ],
                "derogs": [{"id": 1, "kind": "charge_off", "lender": "Old Bank", "opened_date": "2023-02-01", "active": True}],
            },
        ]

        for sc in scenarios:
            rep = opt.analyze(
                raw_lines=sc["lines"],
                smoothed_monthly_income_cents=500_000,
                credit_inquiries=sc["inquiries"],
                credit_derogatories=sc["derogs"],
                today=ref,
            )
            assert rep.factor_analysis is not None
            for item in rep.factor_analysis.fico8_factors + rep.factor_analysis.vantage3_factors:
                combined = f"{item.summary} {item.guidance}"
                assert not re.search(r"\bpts?\b", combined, re.I), f"Fabricated pts in: {combined}"
                assert "est." not in combined.lower()
                assert "directional" not in combined.lower()
            for alert in rep.alerts:
                assert not re.search(r"\b\d+[–-]\d+\s*points?\b", alert.detail, re.I)

    def test_cli_help_linux_installer_zero_invented_apr_and_fresh_zero_goal_surplus(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        import main as root_main
        from chronox_finance.ai.agent_tools import (
            AgentLedgerContext,
            plan_deterministic_actions,
            validate_agent_batch,
        )
        from chronox_finance.ai.opencode_client import CompletionResult
        from chronox_finance.models import CreditLineCreate

        # 1. main.py --help and scripts/install-linux.sh must contain zero simulator strings
        with pytest.raises(SystemExit) as exc_info:
            root_main.parse_arguments(["--help"])
        assert exc_info.value.code == 0
        help_out = capsys.readouterr().out
        assert "simulator" not in help_out.lower()
        assert "Credit Factor Analysis" in help_out

        install_sh = Path("scripts/install-linux.sh").read_text(encoding="utf-8")
        assert "simulator" not in install_sh.lower()

        # 2. CreditLineCreate, agent_tools, and storage default apr_bps to 0 (never 2499 / 24.99%)
        assert CreditLineCreate(name="Zero Default Card", credit_limit_cents=500_000).apr_bps == 0

        client, svc = _make_unlocked_client(tmp_path, monkeypatch)
        try:
            r_cl = client.post(
                "/api/v1/credit/lines",
                json={"name": "Unset APR Card", "kind": "credit_card", "balance_cents": 10_000, "credit_limit_cents": 500_000},
            )
            assert r_cl.status_code == 200
            assert r_cl.json()["apr_bps"] == 0

            ctx = AgentLedgerContext(
                accounts=svc.storage.list_accounts(),
                jars=svc.storage.list_jars(),
                transactions=[],
                goals=[],
                rules=[],
                credit_lines=[],
                holdings=[],
            )
            det = plan_deterministic_actions("add credit card Freedom Unlimited limit $5000", ctx)
            assert det.actions[0].payload["apr_bps"] == 0
            val_batch = validate_agent_batch(
                [{"action_type": "create_credit_line", "payload": {"name": "Card No APR", "credit_limit_cents": 300_000}}],
                ctx,
            )
            assert val_batch.actions[0].payload["apr_bps"] == 0

            # 3. Fresh $0 profile GoalOptimizerEngine reports $0 monthly flexible surplus & $0 weekly set-aside
            fresh_plan = client.post(
                "/api/v1/goals/plan",
                json={"item_name": "Desk Chair", "target_cents": 60_000},
            ).json()
            assert fresh_plan["monthly_flexible_surplus_cents"] == 0
            for opt in fresh_plan["options"]:
                assert opt["weekly_set_aside_cents"] == 0
                assert opt["weeks_to_goal"] == 0

            html = Path("chronox_finance/web/index.html").read_text(encoding="utf-8")
            assert 'value="1680"' not in html
            assert 'value="26.99"' not in html

            # 4. CreditAPIConnectionOut preserves last_error on failed test/sync
            c_res = client.post(
                "/api/v1/credit/apis/connect",
                json={"vendor": "spinwheel", "api_key": "bad_key_123", "user_id": "usr_bad", "test_on_connect": False},
            )
            assert c_res.status_code == 200
            cid = c_res.json()["connection"]["id"]
            monkeypatch.setattr(
                BureauAPIAdapter,
                "test_connection",
                lambda self: (_ for _ in ()).throw(
                    ConnectorError("Spinwheel upstream gateway timeout (504)", provider="bureau_api_spinwheel")
                ),
            )
            r_test = client.post(f"/api/v1/credit/apis/{cid}/test")
            assert r_test.status_code == 400
            apis_after = client.get("/api/v1/credit/apis").json()
            conn_after = next(c for c in apis_after["connections"] if c["id"] == cid)
            assert conn_after["status"] == "error"
            assert "504" in conn_after["last_error"]

            monkeypatch.setattr(
                BureauAPIAdapter,
                "test_connection",
                lambda self: (_ for _ in ()).throw(
                    ConnectorAuthError("Spinwheel token expired (401)", provider="bureau_api_spinwheel")
                ),
            )
            r_auth_err = client.post(f"/api/v1/credit/apis/{cid}/test")
            assert r_auth_err.status_code == 502
            conn_reauth = next(c for c in client.get("/api/v1/credit/apis").json()["connections"] if c["id"] == cid)
            assert conn_reauth["status"] == "needs_reauth"
            assert "401" in conn_reauth["last_error"]
        finally:
            del client
            svc.close()

        # 5. Post-shutdown FinanceService._record_completion_usage & storage.record_ai_usage are safe no-ops
        svc._record_completion_usage(
            CompletionResult(
                content="{}",
                model="gpt-5-codex",
                protocol_used="responses",
                provider="openai_codex",
                input_tokens=10,
                output_tokens=5,
            )
        )
        svc.storage.record_ai_usage("openai_codex", "gpt-5-codex", input_tokens=10, output_tokens=5)

    def test_legacy_savings_goals_and_10000_row_backup_preview_and_restore(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        client, svc = _make_unlocked_client(tmp_path, monkeypatch)
        try:
            base_backup = svc.storage.export_backup()
            acct_id = base_backup["accounts"][0]["id"]
            today = date(2026, 11, 20)

            # Construct a legacy v1 backup with `savings_goals`, 10,050 multi-year transactions
            # omitting `normalized_merchant`, credit line omitting `apr_bps`, and inquiry/derog omitting drop-offs
            legacy_payload = {
                "schema_version": 1,
                "exported_at": "2026-11-20T00:00:00Z",
                "jars": base_backup["jars"],
                "accounts": base_backup["accounts"],
                "savings_goals": [
                    {"id": 1, "name": "Emergency Roof Fund", "target_cents": 400_000, "saved_cents": 125_000},
                    {"id": 2, "name": "Laptop Upgrade", "target_cents": 150_000, "saved_cents": 30_000},
                ],
                "transactions": [
                    {
                        "id": i + 1,
                        "account_id": acct_id,
                        "tx_type": "expense",
                        "amount_cents": 250,
                        "merchant": f"Coffee Roaster #{i % 50}",
                        "tx_date": (today - timedelta(days=i % 720)).isoformat(),
                    }
                    for i in range(10_050)
                ],
                "credit_lines": [
                    {"id": 1, "name": "Legacy Card", "balance_cents": 25_000, "credit_limit_cents": 300_000}
                ],
                "credit_inquiries": [
                    {"id": 1, "lender": "Chase", "bureau": "experian", "inquiry_date": "2026-04-10"}
                ],
                "credit_derogatories": [
                    {"id": 1, "kind": "late_30", "lender": "Old Utility", "opened_date": "2023-06-15"}
                ],
            }
            raw = json.dumps(legacy_payload).encode("utf-8")

            prev = client.post(
                "/api/v1/backup/preview",
                files={"file": ("legacy_10k.json", io.BytesIO(raw), "application/json")},
            )
            assert prev.status_code == 200, prev.text
            p_body = prev.json()
            assert p_body["counts"]["goals"] == 2
            assert p_body["counts"]["transactions"] == 10_050

            imp = client.post(
                "/api/v1/backup/import",
                files={"file": ("legacy_10k.json", io.BytesIO(raw), "application/json")},
            )
            assert imp.status_code == 200, imp.text

            restored_goals = client.get("/api/v1/goals").json()
            assert len(restored_goals) == 2
            assert {g["name"] for g in restored_goals} == {"Emergency Roof Fund", "Laptop Upgrade"}

            rep = client.get("/api/v1/credit/report").json()
            assert rep["credit_lines"][0]["apr_bps"] == 0
            assert rep["credit_inquiries"][0]["counts_until"] == "2027-04-10"
            assert rep["credit_inquiries"][0]["drops_off"] == "2028-04-10"
            assert rep["credit_derogatories"][0]["drops_off"] == "2030-06-15"
        finally:
            del client
            svc.close()
