"""
TDD verification suite for:
1. Forecast & Safe-to-Spend deduplication when a recurring_bill covers a credit/collection line.
2. Recurring bills PATCH / mark-paid CRUD + AI Agent create_bill/update_bill/delete_bill Preview->Execute->Undo.
3. Safe unused account removal (DELETE /api/v1/accounts/{id}) with bank-link & transaction guards.
4. Upgraded Missed Bill Detector (current overdue + stepped-past previous-cycle lookback, zero false alarms).
5. Plaid multi-account (checking + savings + credit card) sync & airtight deduplication.
6. UI contract verification for editable Upcoming Bills, removable accounts, and clickable iOS Cash Flow charts.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from chronox_finance.ai.agent_tools import (
    ALLOWED_AGENT_ACTION_TYPES,
    AgentLedgerContext,
    plan_deterministic_actions,
)
from chronox_finance.api import create_app
from chronox_finance.connectors.base import (
    NormalizedAccount,
    NormalizedSyncResult,
    NormalizedTransaction,
)
from chronox_finance.connectors.plaid_client import _normalize_plaid_transaction
from chronox_finance.engines.autobudget import AutoBudgetEngine
from chronox_finance.engines.quant_math import QuantMathEngine
from chronox_finance.engines.recurring import (
    credit_line_covered_by_recurring_bill,
    detect_bill_anomalies,
)
from chronox_finance.models import (
    AccountCreate,
    AccountType,
    AgentPlanRequest,
    RecurrenceCadence,
    RecurringBillCreate,
    RecurringBillUpdate,
    VaultSetupRequest,
)
from chronox_finance.service import FinanceService

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_unlocked_service(tmp_path: Path) -> FinanceService:
    svc = FinanceService(db_path=tmp_path / "vault.sqlite3")
    svc.setup_vault(VaultSetupRequest(password="ChronoX-TDD-Passphrase-2026!"))
    return svc


def _make_unlocked_client(tmp_path: Path) -> tuple[TestClient, FinanceService, dict[str, str]]:
    svc = FinanceService(db_path=tmp_path / "vault.sqlite3", secret_store_path=tmp_path / "secrets.enc", kdf_iterations=20_000)
    app = create_app(db_path=tmp_path / "vault.sqlite3", service=svc, enforce_vault_lock=True)
    client = TestClient(app)
    res = client.post("/api/v1/auth/setup", json={"password": "ChronoX-TDD-Passphrase-2026!"})
    assert res.status_code == 200
    csrf = res.json()["csrf_token"]
    return client, svc, {"X-ChronoX-CSRF": csrf}


# ---------------------------------------------------------------------------
# 1. Eliminate Double-Count When a Recurring Bill Covers a Credit/Collection Line
# ---------------------------------------------------------------------------


def test_credit_line_covered_by_recurring_bill_eliminates_forecast_and_budget_double_count():
    collection_line = {
        "id": 10,
        "name": "Apex Collection Mgmt (Cable)",
        "issuer": "Apex Collection",
        "kind": "collection",
        "balance_cents": 120_000,
        "credit_limit_cents": 0,
        "apr_bps": 0,
        "min_payment_cents": 16_250,  # $162.50/mo equivalent
        "payment_due_day": 5,
        "active": 1,
    }
    card_line = {
        "id": 11,
        "name": "Acme Rewards Card",
        "issuer": "Acme Bank",
        "kind": "credit_card",
        "balance_cents": 400_000,
        "credit_limit_cents": 700_000,
        "apr_bps": 1800,
        "min_payment_cents": 20_000,  # $200/mo
        "payment_due_day": 12,
        "active": 1,
    }
    bills = [
        {
            "id": 1,
            "name": "Apex / Cable Collection Plan",
            "normalized_merchant": "apex cable collection plan",
            "amount_cents": 7_500,  # $75 biweekly = $162.50/mo
            "cadence": "biweekly",
            "next_due_date": "2026-11-27",
            "jar_id": 1,
            "auto_pay": 1,
            "is_essential": 1,
            "active": 1,
        },
        {
            # Small streaming sub sharing token "acme" MUST NOT mask a $200/mo credit card minimum!
            "id": 2,
            "name": "Acme Cloud Music",
            "normalized_merchant": "acme cloud music",
            "amount_cents": 999,
            "cadence": "monthly",
            "next_due_date": "2026-12-03",
            "jar_id": 1,
            "auto_pay": 1,
            "is_essential": 0,
            "active": 1,
        },
    ]

    assert credit_line_covered_by_recurring_bill(collection_line, bills, effective_min_cents=16_250) is True
    assert credit_line_covered_by_recurring_bill(card_line, bills, effective_min_cents=20_000) is False

    # Verify AutoBudgetEngine does NOT double-count collection_line's $162.50/mo on top of $75/2wk bill
    today = date(2026, 11, 26)
    accounts = [{"id": 1, "name": "Checking", "account_type": "checking", "balance_cents": 200_000, "is_liquid": 1}]
    jars = [
        {"id": 1, "slug": "bills", "name": "Bills", "kind": "bills", "icon": "bills", "target_pct": 50, "priority": 1, "color": "#3b82f6"},
        {"id": 2, "slug": "daily", "name": "Daily", "kind": "daily", "icon": "daily", "target_pct": 25, "priority": 2, "color": "#10b981"},
        {"id": 3, "slug": "fun", "name": "Fun", "kind": "fun", "icon": "fun", "target_pct": 15, "priority": 3, "color": "#f59e0b"},
        {"id": 4, "slug": "safety", "name": "Safety", "kind": "safety", "icon": "safety", "target_pct": 10, "priority": 4, "color": "#06b6d4"},
    ]
    txs = [
        {"id": 1, "account_id": 1, "jar_id": None, "tx_type": "income", "amount_cents": 220_000, "merchant": "Payroll", "normalized_merchant": "payroll", "tx_date": "2026-11-15"},
        {"id": 2, "account_id": 1, "jar_id": None, "tx_type": "income", "amount_cents": 220_000, "merchant": "Payroll", "normalized_merchant": "payroll", "tx_date": "2026-11-01"},
    ]
    settings = {
        "payday_cadence": "semimonthly",
        "payday_anchor_date": "2026-11-15",
        "next_payday_date": "2026-12-01",
        "safety_buffer_cents": 50_000,
    }
    engine = AutoBudgetEngine()
    res_with_cl = engine.compute(
        accounts=accounts,
        jars=jars,
        transactions=txs,
        recurring_bills=bills,
        transfers=[],
        settings=settings,
        credit_lines=[collection_line],
        today=today,
    )
    res_without_cl = engine.compute(
        accounts=accounts,
        jars=jars,
        transactions=txs,
        recurring_bills=bills,
        transfers=[],
        settings=settings,
        credit_lines=[],
        today=today,
    )
    assert res_with_cl.safe_to_spend.safe_to_spend_cents == res_without_cl.safe_to_spend.safe_to_spend_cents
    assert res_with_cl.safe_to_spend.unpaid_bills_until_payday_cents == res_without_cl.safe_to_spend.unpaid_bills_until_payday_cents

    # Verify QuantMathEngine 90-day forecast does NOT emit duplicate $162.50 events for collection_line
    q_engine = QuantMathEngine()
    forecast = q_engine.simulate_cashflow_runway(
        starting_balance_cents=200_000,
        transactions=txs,
        recurring_bills=bills,
        credit_lines=[collection_line, card_line],
        payday_cadence_days=15,
        next_payday_date="2026-12-01",
        estimated_paycheck_cents=220_000,
        emergency_buffer_cents=50_000,
        days=45,
        today=today,
    )
    all_events = [ev for pt in forecast.daily_points for ev in pt.events]
    assert any("Apex / Cable Collection Plan" in ev for ev in all_events)
    assert not any("Apex Collection Mgmt" in ev for ev in all_events)
    assert any("Acme Rewards Card" in ev for ev in all_events)


# ---------------------------------------------------------------------------
# 2. Bills CRUD API (PATCH / mark-paid) + AI Agent Bill Tools (Preview -> Execute -> Undo)
# ---------------------------------------------------------------------------


def test_bills_patch_autopay_edit_and_mark_paid_api(tmp_path: Path):
    client, _svc, headers = _make_unlocked_client(tmp_path)

    create_res = client.post(
        "/api/v1/bills",
        headers=headers,
        json={
            "name": "Apex Collection Plan",
            "amount_cents": 7500,
            "cadence": "biweekly",
            "next_due_date": "2026-11-27",
            "auto_pay": False,
            "is_essential": True,
        },
    )
    assert create_res.status_code == 200
    bill = create_res.json()
    bill_id = bill["id"]
    assert bill["auto_pay"] == 0

    patch_res = client.patch(
        f"/api/v1/bills/{bill_id}",
        headers=headers,
        json={"auto_pay": True, "amount_cents": 8000, "name": "Apex Payment Plan"},
    )
    assert patch_res.status_code == 200
    patched = patch_res.json()
    assert patched["auto_pay"] == 1
    assert patched["amount_cents"] == 8000
    assert patched["previous_amount_cents"] == 7500
    assert patched["name"] == "Apex Payment Plan"

    paid_res = client.post(
        f"/api/v1/bills/{bill_id}/mark-paid?paid_date=2026-11-27",
        headers=headers,
    )
    assert paid_res.status_code == 200
    assert paid_res.json()["next_due_date"] == "2026-12-11"

    del_res = client.delete(f"/api/v1/bills/{bill_id}", headers=headers)
    assert del_res.status_code == 200
    assert del_res.json()["deactivated"] is True


def test_ai_agent_bill_actions_preview_execute_and_atomic_undo(tmp_path: Path):
    assert {"create_bill", "update_bill", "delete_bill"}.issubset(ALLOWED_AGENT_ACTION_TYPES)
    svc = _make_unlocked_service(tmp_path)

    b_col = svc.create_recurring_bill(
        RecurringBillCreate(
            name="Apex Collection Plan",
            amount_cents=7500,
            cadence=RecurrenceCadence.BIWEEKLY,
            next_due_date="2026-11-27",
            auto_pay=False,
        )
    )
    b_old = svc.create_recurring_bill(
        RecurringBillCreate(
            name="Legacy Telecom",
            amount_cents=14_695,
            cadence=RecurrenceCadence.MONTHLY,
            next_due_date="2026-12-02",
            auto_pay=False,
        )
    )

    # 1. Deterministic multi-clause NL plan: flip autopay ON + archive old telecom + add new telecom
    plan = svc.plan_agent_batch(
        AgentPlanRequest(
            prompt=(
                "Turn on autopay for Apex Collection Plan; "
                "remove Legacy Telecom bill; "
                "add Metro Fiber $45.40 monthly bill due 2026-12-16 with autopay"
            )
        ),
        mode_override="deterministic_only",
    )
    action_types = [a.action_type for a in plan.actions]
    assert action_types == ["update_bill", "delete_bill", "create_bill"]
    assert plan.actions[0].payload["bill_id"] == b_col["id"]
    assert plan.actions[0].payload["auto_pay"] is True
    assert plan.actions[1].payload["bill_id"] == b_old["id"]
    assert plan.actions[2].payload["amount_cents"] == 4540
    assert plan.actions[2].payload["auto_pay"] is True

    # 2. Execute atomically
    exec_res = svc.execute_agent_batch(plan.batch_id)
    assert exec_res.status == "executed"
    assert exec_res.applied_count == 3
    assert exec_res.undo_available is True

    after_col = svc.storage.get_recurring_bill(b_col["id"])
    after_old = svc.storage.get_recurring_bill(b_old["id"])
    active_names = {b["name"] for b in svc.storage.list_recurring_bills(active_only=True)}
    assert after_col is not None and after_col["auto_pay"] == 1
    assert after_old is not None and after_old["active"] == 0
    assert "Metro Fiber" in active_names

    # 3. Atomic Undo restores exact pre-images
    undo_res = svc.undo_agent_batch(plan.batch_id)
    assert undo_res.status == "undone"
    assert undo_res.restored_count == 3

    restored_col = svc.storage.get_recurring_bill(b_col["id"])
    restored_old = svc.storage.get_recurring_bill(b_old["id"])
    restored_names = {b["name"] for b in svc.storage.list_recurring_bills(active_only=True)}
    assert restored_col is not None and restored_col["auto_pay"] == 0
    assert restored_old is not None and restored_old["active"] == 1
    assert "Metro Fiber" not in restored_names

    # Verify standalone deterministic parser handles natural phrasing too
    ctx = AgentLedgerContext.from_objects(
        bills=[b_col],
        jars=[{"id": 1, "slug": "bills", "name": "Bills", "kind": "bills"}],
    )
    res_nl = plan_deterministic_actions("Enable autopay on Apex", ctx)
    assert len(res_nl.actions) == 1
    assert res_nl.actions[0].action_type == "update_bill"
    assert res_nl.actions[0].payload["auto_pay"] is True


# ---------------------------------------------------------------------------
# 3. Safe Account Removal (DELETE /api/v1/accounts/{id})
# ---------------------------------------------------------------------------


def test_delete_account_removes_unused_local_and_protects_connected_or_populated_accounts(tmp_path: Path):
    client, svc, headers = _make_unlocked_client(tmp_path)

    # Vault boots with #1 Main Checking (0 tx, local). Add a bank-linked checking & a populated cash account.
    conn = svc.storage.upsert_connection({"provider": "plaid", "institution": "Sample Bank", "status": "active"})
    synced_acct = svc.storage.create_account(
        name="Active Checking",
        account_type="checking",
        balance_cents=150_000,
        institution="Sample Bank",
        connection_id=conn["id"],
        external_id="ext_chk_1",
    )
    cash_acct = svc.create_account(
        AccountCreate(name="Pocket Cash", account_type=AccountType.CASH, balance_cents=5000, institution="Local")
    )
    svc.add_transaction_raw(
        account_id=cash_acct.id,
        tx_type="expense",
        amount_cents=1200,
        merchant="Coffee Shop",
        tx_date="2026-11-20",
        run_auto_sync=False,
    )

    accounts_before = svc.storage.list_accounts()
    main_checking = next(a for a in accounts_before if a["name"] == "Main Checking")

    # 1. Empty unused local "Main Checking" removes cleanly
    r_del = client.delete(f"/api/v1/accounts/{main_checking['id']}", headers=headers)
    assert r_del.status_code == 200
    assert r_del.json()["deleted"] is True
    assert all(a["id"] != main_checking["id"] for a in svc.storage.list_accounts())

    # 2. Connected Plaid account is blocked from direct deletion
    r_sync = client.delete(f"/api/v1/accounts/{synced_acct['id']}", headers=headers)
    assert r_sync.status_code == 400
    assert "live bank connection" in r_sync.json()["detail"].lower()

    # 3. Populated local account is protected unless force=true
    r_pop = client.delete(f"/api/v1/accounts/{cash_acct.id}", headers=headers)
    assert r_pop.status_code == 400
    assert "transaction" in r_pop.json()["detail"].lower()

    r_force = client.delete(f"/api/v1/accounts/{cash_acct.id}?force=true", headers=headers)
    assert r_force.status_code == 200
    assert r_force.json()["deleted"] is True
    assert r_force.json()["removed_transactions"] == 1


# ---------------------------------------------------------------------------
# 4. Upgraded Missed Bill Detector
# ---------------------------------------------------------------------------


def test_missed_bill_detector_catches_overdue_and_stepped_past_previous_cycle_without_false_alarms():
    today = date(2026, 11, 26)
    bills = [
        {
            # Case A: Overdue next_due_date (8 days past due, manual pay)
            "id": 101,
            "name": "City Water",
            "normalized_merchant": "city water",
            "amount_cents": 6_500,
            "cadence": "monthly",
            "next_due_date": "2026-11-18",
            "due_window_days": 4,
            "auto_pay": 0,
            "active": 1,
        },
        {
            # Case B: Stepped-past previous cycle (paid Oct 2, next_due_date advanced to Dec 2, Nov 2 MISSED!)
            "id": 102,
            "name": "Legacy Telecom",
            "normalized_merchant": "legacy telecom",
            "amount_cents": 14_695,
            "cadence": "monthly",
            "next_due_date": "2026-12-02",
            "due_window_days": 4,
            "auto_pay": 0,
            "active": 1,
        },
        {
            # Case C: Paid on time in Nov -> ZERO false alarm
            "id": 103,
            "name": "Metro Fiber",
            "normalized_merchant": "metro fiber",
            "amount_cents": 4_540,
            "cadence": "monthly",
            "next_due_date": "2026-12-16",
            "due_window_days": 4,
            "auto_pay": 1,
            "active": 1,
        },
        {
            # Case D: Brand-new manual bill (due tomorrow, no prior history) -> ZERO false alarm
            "id": 104,
            "name": "Apex Collection Plan",
            "normalized_merchant": "apex collection plan",
            "amount_cents": 7_500,
            "cadence": "biweekly",
            "next_due_date": "2026-11-27",
            "due_window_days": 4,
            "auto_pay": 1,
            "active": 1,
        },
    ]
    txs = [
        # Legacy Telecom paid in Sep & Oct, then stopped in Nov!
        {"tx_type": "expense", "tx_date": "2026-09-02", "merchant": "Legacy Telecom", "normalized_merchant": "legacy telecom", "amount_cents": 14_695},
        {"tx_type": "expense", "tx_date": "2026-10-02", "merchant": "Legacy Telecom", "normalized_merchant": "legacy telecom", "amount_cents": 14_695},
        # Metro Fiber paid in Oct & Nov
        {"tx_type": "expense", "tx_date": "2026-10-16", "merchant": "Metro Fiber", "normalized_merchant": "metro fiber", "amount_cents": 4_540},
        {"tx_type": "expense", "tx_date": "2026-11-16", "merchant": "Metro Fiber", "normalized_merchant": "metro fiber", "amount_cents": 4_540},
    ]

    anomalies = detect_bill_anomalies(txs, bills, today=today)
    missed = [a for a in anomalies if a.kind == "missed_bill"]
    by_id = {a.bill_id: a for a in missed}

    assert 101 in by_id
    assert by_id[101].severity == "high"
    assert by_id[101].days_overdue == 8

    assert 102 in by_id
    assert by_id[102].dates == ["2026-11-02"]
    assert by_id[102].days_overdue == 24
    assert by_id[102].severity == "high"

    assert 103 not in by_id
    assert 104 not in by_id


# ---------------------------------------------------------------------------
# 5. Plaid Multi-Account (Checking + Savings + Credit Card) Sync & Airtight Dedup
# ---------------------------------------------------------------------------


def test_plaid_sync_all_accounts_including_credit_cards_and_airtight_dedup(tmp_path: Path):
    svc = _make_unlocked_service(tmp_path)
    conn = svc.storage.upsert_connection(
        {"provider": "plaid", "institution": "Sample Credit Union", "env": "production", "status": "active"}
    )
    svc.secrets.set_json(f"conn:{conn['id']}", {"access_token": "access-prod-mock-token", "env": "production"})

    # Verify credit-card LOAN_PAYMENTS negative amount classifies as transfer (not income!)
    cc_pay = _normalize_plaid_transaction(
        {
            "transaction_id": "tx_cc_pay",
            "account_id": "ext_cc_1",
            "date": "2026-11-20",
            "amount": -500.00,
            "name": "ONLINE PAYMENT THANK YOU",
            "personal_finance_category": {"primary": "LOAN_PAYMENTS", "detailed": "LOAN_PAYMENTS_OTHER"},
            "pending": False,
        }
    )
    assert cc_pay.tx_type == "transfer"
    assert cc_pay.transfer_direction == "in"

    chk_acct = NormalizedAccount(
        external_account_id="ext_chk_1",
        name="Active Checking",
        account_type="checking",
        institution="Sample Credit Union",
        balance_cents=120_000,
        is_liquid=True,
    )
    cc_acct = NormalizedAccount(
        external_account_id="ext_cc_1",
        name="Sample Rewards Card",
        account_type="credit",
        institution="Sample Credit Union",
        balance_cents=-150_000,
        limit_cents=710_000,
        is_liquid=False,
    )

    # Pre-seed an existing CSV import on the credit card (3-day posting lag) AND a stale pending tx
    cc_local_id = svc._ensure_local_account_for_normalized(cc_acct, connection_id=conn["id"])
    svc.add_transaction_raw(
        account_id=cc_local_id,
        tx_type="expense",
        amount_cents=4_250,
        merchant="Corner Market",
        tx_date="2026-11-17",
        source="csv_import",
        run_auto_sync=False,
    )
    svc.add_transaction_raw(
        account_id=cc_local_id,
        tx_type="expense",
        amount_cents=1_899,
        merchant="Coffee Roasters",
        tx_date="2026-11-21",
        source="plaid_sync",
        external_id="pend_tx_99",
        run_auto_sync=False,
    )

    # Sync #1: bal_accounts ONLY returns checking (simulating partial balance endpoint),
    # while sync_res.accounts includes BOTH checking AND credit card!
    sync_1 = NormalizedSyncResult(
        provider="plaid",
        accounts=[chk_acct, cc_acct],
        added=[
            NormalizedTransaction(
                external_id="tx_chk_1",
                external_account_id="ext_chk_1",
                tx_date="2026-11-22",
                merchant="Direct Deposit",
                amount_cents=220_000,
                tx_type="income",
            ),
            # Posted settlement replacing pend_tx_99
            NormalizedTransaction(
                external_id="tx_cc_posted_99",
                external_account_id="ext_cc_1",
                tx_date="2026-11-22",
                merchant="Coffee Roasters",
                amount_cents=1_899,
                tx_type="expense",
                pending_transaction_id="pend_tx_99",
            ),
            # Cross-source overlap with CSV import 3 days earlier -> MUST dedup!
            NormalizedTransaction(
                external_id="tx_cc_csv_dup",
                external_account_id="ext_cc_1",
                tx_date="2026-11-20",
                merchant="CORNER MARKET #412",
                amount_cents=4_250,
                tx_type="expense",
            ),
            # Pending transaction -> skipped until posted
            NormalizedTransaction(
                external_id="tx_cc_pending_skip",
                external_account_id="ext_cc_1",
                tx_date="2026-11-25",
                merchant="Pending Hold",
                amount_cents=9_900,
                tx_type="expense",
                pending=True,
            ),
            # Credit card payment
            cc_pay,
        ],
        next_cursor="cursor_1",
        transactions_update_status="HISTORICAL_UPDATE_COMPLETE",
    )

    # Sync #2: replays exact same external_ids + removes tx_cc_pay -> 0 duplicates, 1 removed!
    sync_2 = NormalizedSyncResult(
        provider="plaid",
        accounts=[chk_acct, cc_acct],
        added=[
            NormalizedTransaction(
                external_id="tx_chk_1",
                external_account_id="ext_chk_1",
                tx_date="2026-11-22",
                merchant="Direct Deposit",
                amount_cents=220_000,
                tx_type="income",
            ),
            NormalizedTransaction(
                external_id="tx_cc_posted_99",
                external_account_id="ext_cc_1",
                tx_date="2026-11-22",
                merchant="Coffee Roasters",
                amount_cents=1_899,
                tx_type="expense",
            ),
        ],
        removed_external_ids=["tx_cc_pay"],
        next_cursor="cursor_2",
        transactions_update_status="HISTORICAL_UPDATE_COMPLETE",
    )

    calls = {"n": 0}

    def _fake_sync(_token, cursor=None, institution=""):
        calls["n"] += 1
        return sync_1 if calls["n"] == 1 else sync_2

    fake_client = SimpleNamespace(
        sync_transactions=_fake_sync,
        get_accounts_balances=lambda _t, institution="": [chk_acct],
        get_liabilities=lambda _t, institution="": [],
        get_investments_holdings=lambda _t: [],
    )

    with patch.object(svc, "_build_plaid_client", return_value=(fake_client, "production")):
        r1 = svc.sync_plaid(connection_id=conn["id"])
        assert r1["imported_count"] == 3  # tx_chk_1, tx_cc_posted_99, tx_cc_pay
        assert r1["duplicate_count"] == 1  # tx_cc_csv_dup matched CSV within 3d

        cc_txs_1 = svc.storage.list_transactions(limit=50, account_id=cc_local_id)
        cc_ext_ids_1 = {t["external_id"] for t in cc_txs_1}
        assert "tx_cc_posted_99" in cc_ext_ids_1
        assert "pend_tx_99" not in cc_ext_ids_1  # stale pending cleaned up!
        assert "tx_cc_pending_skip" not in cc_ext_ids_1
        assert len([t for t in cc_txs_1 if t["amount_cents"] == 4_250]) == 1

        r2 = svc.sync_plaid(connection_id=conn["id"])
        assert r2["imported_count"] == 0
        assert r2["duplicate_count"] == 2
        assert r2["removed_count"] == 1

        cc_txs_2 = svc.storage.list_transactions(limit=50, account_id=cc_local_id)
        assert all(t["external_id"] != "tx_cc_pay" for t in cc_txs_2)


# ---------------------------------------------------------------------------
# 6. UI Contracts (Editable Bills, Removable Accounts, Clickable iOS Graphs)
# ---------------------------------------------------------------------------


def test_ui_contracts_bills_accounts_and_interactive_charts():
    html_text = (REPO_ROOT / "chronox_finance/web/index.html").read_text(encoding="utf-8")
    vault_js = (REPO_ROOT / "chronox_finance/web/js/vault.js").read_text(encoding="utf-8")
    charts_js = (REPO_ROOT / "chronox_finance/web/js/charts.js").read_text(encoding="utf-8")
    timeline_js = (REPO_ROOT / "chronox_finance/web/js/timeline.js").read_text(encoding="utf-8")

    for dom_id in (
        "btn-bills-scope-14d",
        "btn-bills-scope-all",
        "btn-overview-add-bill",
        "overview-bill-quick-form",
        "bill-anomalies-overview",
        "analytics-sankey-drilldown",
        "analytics-trend-filter-bar",
        "timeline-day-breakdown",
    ):
        assert f'id="{dom_id}"' in html_text, f"Missing DOM id {dom_id} in index.html"

    # Editable bills + autopay toggle + mark paid + account remove
    for marker in (
        "buildBillInlineEditor",
        "data-toggle-bill-autopay",
        "data-mark-bill-paid",
        "data-edit-bill-toggle",
        "data-remove-account",
        "/api/v1/bills/${billId}",
        "/api/v1/accounts/${btn.dataset.removeAccount}",
    ):
        assert marker in vault_js

    # 1st-party iOS charts + clickable breakdowns across Sankey, Donut, Trend, Forecast
    for marker in (
        "buildMonotoneSplinePath",
        "openSankeyNodeDrilldown",
        'rx: "5"',
        'rx: "10"',
        "analytics-trend-filter-bar",
    ):
        assert marker in charts_js

    for marker in (
        "timeline-day-breakdown",
        "renderDayBreakdown",
        "eventDotGroup",
        "pinnedDayIdx",
    ):
        assert marker in timeline_js

    # Ensure zero RecurringBillUpdate validation regression on bad range
    try:
        RecurringBillUpdate(amount_min_cents=5000, amount_max_cents=1000)
        raise AssertionError("Expected ValidationError for min > max")
    except ValueError:
        pass
