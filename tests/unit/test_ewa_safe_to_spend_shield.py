"""
Strict TDD suite for Phase 5:
- Shared `detect_open_ewa_advances()` matched-cycle trailing-tail detection (45d recency + >=$5 micro-deposit filter)
- Safe-to-Spend AND Safe-to-Invest firewall deduction when `reserve_open_ewa_advances=True`
- Live toggle via `PATCH /api/v1/settings` (`reserve_open_ewa_advances: false | true`) reflected in `GET /api/v1/dashboard`
- UI Settings checkbox (`set-reserve-open-ewa`) + Overview Safe-to-Spend math breakdown wiring
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from chronox_finance.api import create_app
from chronox_finance.engines.autobudget import AutoBudgetEngine
from chronox_finance.engines.credit_debt import detect_open_ewa_advances
from chronox_finance.security import CSRF_HEADER_NAME
from chronox_finance.service import FinanceService


def test_detect_open_ewa_advances_exact_trailing_tail_and_guards():
    ref = date(2026, 11, 26)
    txs = [
        # Settled Dave cycle
        {"tx_type": "income", "amount_cents": 40_000, "merchant": "Dave", "tx_date": "2026-10-05"},
        {"tx_type": "expense", "amount_cents": 42_600, "merchant": "Dave", "tx_date": "2026-10-30"},
        # Unsettled Dave advances posted AFTER last repayment (2026-10-30)
        {"tx_type": "income", "amount_cents": 22_500, "merchant": "Dave", "tx_date": "2026-11-18"},
        {"tx_type": "income", "amount_cents": 7_500, "merchant": "Dave", "tx_date": "2026-11-20"},
        # Micro-deposit verification penny must be ignored
        {"tx_type": "income", "amount_cents": 1, "merchant": "Brigit", "tx_date": "2026-11-22"},
        # Unsettled Brigit advance
        {"tx_type": "expense", "amount_cents": 5_799, "merchant": "Brigit", "tx_date": "2026-10-29"},
        {"tx_type": "income", "amount_cents": 7_500, "merchant": "Brigit", "tx_date": "2026-11-23"},
        # Stale >45d inbound advance with no repayment should NOT trap Safe-to-Spend forever
        {"tx_type": "income", "amount_cents": 10_000, "merchant": "EarnIn", "tx_date": "2026-09-20"},
        # Restaurant with "Dave" in name must NEVER trigger EWA shield
        {"tx_type": "income", "amount_cents": 5_000, "merchant": "Dave & Buster's Refund", "tx_date": "2026-11-24"},
    ]

    total_open, per_app = detect_open_ewa_advances(txs, today=ref)
    assert total_open == 37_500
    assert per_app == {"Dave": 30_000, "Brigit": 7_500}


def test_autobudget_shields_open_ewa_advances_in_safe_to_spend_and_safe_to_invest():
    ref = date(2026, 11, 26)
    accounts = [
        {"id": 1, "name": "Checking", "account_type": "checking", "balance_cents": 200_000, "is_liquid": 1, "currency": "USD"},
    ]
    jars = [
        {"id": 1, "slug": "bills", "name": "Bills & Essentials", "kind": "bills", "icon": "home", "color": "#60a5fa", "priority": 1, "target_pct": 45.0, "monthly_target_cents": 100_000, "auto_allocated_cents": 0, "rollover_cents": 0, "is_locked": 1, "rollover_enabled": 1, "rollover_cap_cents": None},
        {"id": 2, "slug": "daily", "name": "Daily Living", "kind": "daily", "icon": "cart", "color": "#34d399", "priority": 2, "target_pct": 25.0, "monthly_target_cents": 50_000, "auto_allocated_cents": 0, "rollover_cents": 0, "is_locked": 0, "rollover_enabled": 1, "rollover_cap_cents": None},
        {"id": 3, "slug": "invest", "name": "ChronoX Invest Vault", "kind": "invest", "icon": "trend", "color": "#2dd4bf", "priority": 5, "target_pct": 10.0, "monthly_target_cents": 20_000, "auto_allocated_cents": 0, "rollover_cents": 100_000, "is_locked": 1, "rollover_enabled": 1, "rollover_cap_cents": None},
    ]
    txs = [
        {"id": 1, "tx_type": "income", "amount_cents": 250_000, "merchant": "DFAS PAYROLL", "tx_date": "2026-11-15", "jar_id": None},
        {"id": 2, "tx_type": "expense", "amount_cents": 40_000, "merchant": "Dave", "tx_date": "2026-10-30", "jar_id": 1},
        {"id": 3, "tx_type": "income", "amount_cents": 30_000, "merchant": "Dave", "tx_date": "2026-11-20", "jar_id": None},
        {"id": 4, "tx_type": "income", "amount_cents": 7_500, "merchant": "Brigit", "tx_date": "2026-11-23", "jar_id": None},
    ]
    base_settings = {
        "emergency_buffer_cents": 50_000,
        "next_payday_date": (ref + timedelta(days=5)).isoformat(),
        "payday_cadence_days": 14,
        "expected_paycheck_cents": 250_000,
        "payday_user_configured": 1,
        "income_configured": 1,
    }

    engine = AutoBudgetEngine()
    res_on = engine.compute(
        settings={**base_settings, "reserve_open_ewa_advances": 1},
        accounts=accounts,
        jars=jars,
        transactions=txs,
        recurring_bills=[],
        transfers=[],
        today=ref,
    )
    res_off = engine.compute(
        settings={**base_settings, "reserve_open_ewa_advances": 0},
        accounts=accounts,
        jars=jars,
        transactions=txs,
        recurring_bills=[],
        transfers=[],
        today=ref,
    )

    assert res_on.safe_to_spend.detected_open_ewa_advance_cents == 37_500
    assert res_on.safe_to_spend.ewa_advance_reserved_cents == 37_500
    assert res_on.safe_to_spend.reserve_open_ewa_advances is True

    assert res_off.safe_to_spend.detected_open_ewa_advance_cents == 37_500
    assert res_off.safe_to_spend.ewa_advance_reserved_cents == 0
    assert res_off.safe_to_spend.reserve_open_ewa_advances is False

    # Turning shield ON lowers Safe-to-Spend by exact $375.00 (bounded at 0) and protects Safe-to-Invest!
    assert res_off.safe_to_spend.safe_to_spend_cents - res_on.safe_to_spend.safe_to_spend_cents == min(
        37_500, res_off.safe_to_spend.safe_to_spend_cents
    )
    assert res_on.safe_to_invest_cents <= res_off.safe_to_invest_cents


def test_ewa_shield_settings_toggle_api_and_ui_wiring(tmp_path: Path):
    db_file = tmp_path / "ewa_shield.sqlite3"
    sec_file = tmp_path / "secrets.enc"
    svc = FinanceService(db_path=db_file, secret_store_path=sec_file, kdf_iterations=20_000)
    try:
        app = create_app(db_path=db_file, service=svc, enforce_vault_lock=True)
        client = TestClient(app)
        setup_r = client.post("/api/v1/auth/setup", json={"password": "EwaShieldTDD!2026"})
        csrf = setup_r.json()["csrf_token"]
        headers = {CSRF_HEADER_NAME: csrf}

        # Seed $2,000 checking + $0 emergency buffer + $300 unsettled Dave advance
        acct = client.post(
            "/api/v1/accounts",
            json={"name": "Checking", "account_type": "checking", "balance_cents": 200_000},
            headers=headers,
        ).json()
        client.patch(
            "/api/v1/settings",
            json={
                "emergency_buffer_cents": 0,
                "next_payday_date": (date.today() + timedelta(days=6)).isoformat(),
                "expected_paycheck_cents": 200_000,
            },
            headers=headers,
        )
        client.post(
            "/api/v1/transactions",
            json={
                "account_id": acct["id"],
                "tx_type": "income",
                "amount_cents": 30_000,
                "merchant": "Dave",
                "tx_date": date.today().isoformat(),
            },
            headers=headers,
        )

        # Default ON: $300.00 shielded
        dash_on = client.get("/api/v1/dashboard").json()
        sts_on = dash_on["safe_to_spend"]
        assert sts_on["reserve_open_ewa_advances"] is True
        assert sts_on["detected_open_ewa_advance_cents"] == 30_000
        assert sts_on["ewa_advance_reserved_cents"] == 30_000

        # Toggle OFF via PATCH /api/v1/settings -> Safe-to-Spend rises by $300.00 immediately
        patch_r = client.patch(
            "/api/v1/settings",
            json={"reserve_open_ewa_advances": False},
            headers=headers,
        )
        assert patch_r.status_code == 200
        assert patch_r.json()["reserve_open_ewa_advances"] is False

        dash_off = client.get("/api/v1/dashboard").json()
        sts_off = dash_off["safe_to_spend"]
        assert sts_off["reserve_open_ewa_advances"] is False
        assert sts_off["detected_open_ewa_advance_cents"] == 30_000
        assert sts_off["ewa_advance_reserved_cents"] == 0
        assert sts_off["safe_to_spend_cents"] - sts_on["safe_to_spend_cents"] == 30_000
    finally:
        svc.close()

    # Frontend HTML + JS contract
    repo_root = Path(__file__).resolve().parents[2]
    index_html = (repo_root / "chronox_finance" / "web" / "index.html").read_text(encoding="utf-8")
    vault_js = (repo_root / "chronox_finance" / "web" / "js" / "vault.js").read_text(encoding="utf-8")
    assert 'id="set-reserve-open-ewa"' in index_html
    assert "ewa_advance_reserved_cents" in vault_js
    assert "reserve_open_ewa_advances" in vault_js
