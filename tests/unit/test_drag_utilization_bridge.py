"""
Strict TDD suite for the Financial Drag & Utilization Paydown Bridge:
- Forensic drag decomposition: unrecoverable interest/FX/ATM fees vs. EWA reborrowing churn & open advances
- CARD Act 15 U.S.C. § 1666c(b) cash-advance trailing interest & grace-period-loss detection
- Exact FICO 8 / 10T / VantageScore 3.0 & 4.0 utilization cliff ladder (88.9% -> 68.9% -> 48.9% -> 28.9% -> 8.9% -> 1.0% -> $0)
- Exact integer-cent baseline vs. drag-redirected amortization with non-amortizing payment safety
- FICO 8 vs. FICO 9/10T vs. VantageScore 3.0/4.0 collection/derogatory resolution rules
- Zero fabricated point-delta / simulator / what-if hygiene compliance
- End-to-end API (`/api/v1/credit/report` & `/api/v1/credit/drag-bridge`) + Credit & Cash Flow UI wiring
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from chronox_finance.api import create_app
from chronox_finance.engines.categorizer import TransactionCategorizer
from chronox_finance.engines.credit_debt import CreditDebtOptimizer, analyze_financial_drag_bridge
from chronox_finance.security import CSRF_HEADER_NAME
from chronox_finance.service import FinanceService


def test_dave_and_busters_not_misclassified_as_cash_advance():
    jars = {
        "bills": {"id": 1, "slug": "bills"},
        "daily": {"id": 2, "slug": "daily"},
        "fun": {"id": 3, "slug": "fun"},
    }
    cat = TransactionCategorizer()
    jar_id, subcat, _conf, _rev = cat.categorize("Dave & Buster's", "", jars, {})
    assert subcat != "Cash Advances & BNPL"
    assert jar_id != jars["bills"]["id"]


def test_drag_bridge_separates_ewa_churn_from_fee_bleed_and_computes_exact_cliffs():
    ref = date(2026, 11, 26)
    lines = [
        {
            "id": 1,
            "name": "Sample Rewards Card",
            "issuer": "Sample Federal",
            "kind": "credit_card",
            "balance_cents": 709_000,
            "credit_limit_cents": 710_000,
            "apr_bps": 1800,
            "min_payment_cents": 10_300,
            "statement_closing_day": 11,
            "payment_due_day": 8,
            "has_grace_period": False,
            "autopay_mode": "minimum",
            "active": 1,
        },
        {
            "id": 5,
            "name": "Metro Wireless (Past Due)",
            "kind": "personal_loan",
            "balance_cents": 15_000,
            "credit_limit_cents": 0,
            "apr_bps": 0,
            "min_payment_cents": 2_500,
            "autopay_mode": "none",
            "active": 1,
        },
        {
            "id": 6,
            "name": "Summit Recovery Agency (Cable)",
            "kind": "personal_loan",
            "balance_cents": 62_000,
            "credit_limit_cents": 0,
            "apr_bps": 0,
            "min_payment_cents": 2_500,
            "autopay_mode": "none",
            "active": 1,
        },
    ]
    derogs = [
        {
            "id": 1,
            "kind": "bankruptcy_ch7",
            "lender": "US BKPT CT SAMPLE DIST",
            "opened_date": "2020-04-15",
            "drops_off": "2030-04-15",
            "active": True,
        },
        {
            "id": 2,
            "kind": "collection",
            "lender": "Summit Recovery Agency (Orig: Cable Co.)",
            "opened_date": "2026-11-21",
            "drops_off": "2033-11-21",
            "active": True,
            "notes": "$620 collection",
        },
        {
            "id": 3,
            "kind": "late_30",
            "lender": "Metro Wireless",
            "opened_date": "2026-11-02",
            "drops_off": "2033-11-02",
            "active": True,
            "notes": "$150 past due",
        },
    ]
    txs = [
        # Card purchase interest + cash-advance trailing interest (last 90d)
        {"id": 1, "tx_type": "expense", "amount_cents": 10_500, "merchant": "INTEREST CHARGE-PURCHASES", "subcategory": "Interest & Bank Fees", "tx_date": "2026-11-11"},
        {"id": 2, "tx_type": "expense", "amount_cents": 10_400, "merchant": "INTEREST CHARGE-PURCHASES", "subcategory": "Interest & Bank Fees", "tx_date": "2026-10-11"},
        {"id": 3, "tx_type": "expense", "amount_cents": 446, "merchant": "INTEREST CHARGE-CIT PROTECTED CASH ADV", "subcategory": "Interest & Bank Fees", "tx_date": "2026-11-11"},
        {"id": 4, "tx_type": "expense", "amount_cents": 265, "merchant": "INTEREST CHARGE-CASH ADVANCES", "subcategory": "Interest & Bank Fees", "tx_date": "2026-10-09"},
        # Recent credit card cash advance
        {"id": 5, "tx_type": "transfer", "amount_cents": 6_000, "merchant": "ONLINE CASH ADVANCE", "subcategory": "Internal Transfer", "tx_date": "2026-11-20"},
        # Overseas debit card FX fees + ATM fees
        {"id": 6, "tx_type": "expense", "amount_cents": 217, "merchant": "Intl Transaction Fee Visa Overseas Merchant", "subcategory": "Interest & Bank Fees", "tx_date": "2026-11-23"},
        {"id": 7, "tx_type": "expense", "amount_cents": 500, "merchant": "ATM Fee - Withdrawal", "subcategory": "Interest & Bank Fees", "tx_date": "2026-10-20"},
        # EWA Reborrowing Treadmill (Dave: $1,000 in vs $930 out -> $70 open advance, NOT $930 fake free surplus!)
        {"id": 8, "tx_type": "income", "amount_cents": 50_000, "merchant": "Dave", "subcategory": "Income", "tx_date": "2026-10-05"},
        {"id": 9, "tx_type": "expense", "amount_cents": 51_500, "merchant": "Dave", "subcategory": "Cash Advances & BNPL", "tx_date": "2026-10-19"},
        {"id": 10, "tx_type": "income", "amount_cents": 50_000, "merchant": "Dave", "subcategory": "Income", "tx_date": "2026-11-20"},
        {"id": 11, "tx_type": "expense", "amount_cents": 41_500, "merchant": "Dave", "subcategory": "Cash Advances & BNPL", "tx_date": "2026-10-30"},
        # Brigit fee/subscription + BNPL
        {"id": 12, "tx_type": "expense", "amount_cents": 999, "merchant": "Brigit", "subcategory": "Cash Advances & BNPL", "tx_date": "2026-10-29"},
        {"id": 13, "tx_type": "expense", "amount_cents": 15_000, "merchant": "AfterPay", "subcategory": "Cash Advances & BNPL", "tx_date": "2026-10-15"},
    ]

    report = CreditDebtOptimizer().analyze(
        raw_lines=lines,
        smoothed_monthly_income_cents=400_000,
        extra_monthly_budget_cents=25_000,
        credit_derogatories=derogs,
        transactions=txs,
        today=ref,
    )
    bridge = report.drag_bridge
    assert bridge is not None

    # 1. Forensic drag decomposition
    assert bridge.card_interest_burn_cents == 21_611  # 10500 + 10400 + 446 + 265
    assert bridge.cash_advance_interest_cents == 711  # 446 + 265
    assert bridge.cash_advance_grace_period_lost is True
    assert bridge.trailing_interest_detected is True
    assert bridge.recent_card_cash_advance_cents == 6_000
    assert bridge.bank_and_fx_fee_cents == 717
    assert bridge.fx_fee_count == 1
    assert bridge.atm_fee_count == 1

    # EWA honesty: separates principal churn ($930 + $9.99) from open advance ($500 Nov 20 after Oct 30 last out!)
    assert bridge.ewa_gross_outflow_cents == 93_999
    assert bridge.ewa_gross_inflow_cents == 100_000
    assert bridge.ewa_estimated_open_advance_cents == 50_000  # Exact Nov 20 unsettled Dave advance after Oct 30 repayment!
    assert bridge.ewa_cycle_count == 3  # 2 Dave + 1 Brigit outflows
    assert bridge.bnpl_outflow_cents == 15_000
    # Reclaimable monthly MUST NOT falsely include recycled Dave principal!
    assert 5_000 <= bridge.reclaimable_monthly_cents <= 25_000

    # 2. Exact FICO utilization cliff ladder for $7,090.00 / $7,100.00 (99.86%) — zero IEEE-754 penny drift!
    milestones = {m.target_util_pct: m for m in bridge.utilization_milestones}
    assert set(milestones.keys()) == {88.9, 68.9, 48.9, 28.9, 8.9, 1.0, 0.0}

    m889 = milestones[88.9]
    assert m889.target_balance_cents == 631_190  # exact integer (710_000 * 889) // 1000
    assert m889.paydown_needed_cents == 77_810  # 709_000 - 631_190
    assert milestones[28.9].target_balance_cents == 205_190  # exact integer (NOT 205_189 float truncation!)
    assert milestones[28.9].paydown_needed_cents == 503_810
    assert m889.baseline_months is not None and m889.accelerated_months is not None
    assert m889.accelerated_months <= m889.baseline_months
    assert m889.annual_interest_saved_cents > 0

    m0 = milestones[0.0]
    assert m0.target_balance_cents == 0
    assert m0.paydown_needed_cents == 709_000
    assert m0.accelerated_months is not None and m0.baseline_months is not None
    assert m0.accelerated_months < m0.baseline_months
    assert m0.months_saved > 0

    # 3. Collection / derogatory scoring-model matrix
    assert len(bridge.derogatory_guidance) >= 2
    coll = next(d for d in bridge.derogatory_guidance if d.kind == "collection")
    assert "fico 8" in coll.fico8_rule.lower()
    assert "paid" in coll.fico9_vantage_rule.lower() or "$0" in coll.fico9_vantage_rule.lower()

    # 4. Sequenced action steps + strict hygiene (ZERO fabricated score points / simulator / what-if!)
    assert len(bridge.ordered_action_steps) >= 3
    blob = json.dumps(bridge.model_dump())
    assert not re.search(r"\bpts?\b", blob, re.I), f"Fabricated pts string leaked: {blob}"
    assert not re.search(r"\b\d+[–-]\d+\s*points?\b", blob, re.I)
    assert "WHAT-IF" not in blob.upper()
    assert "SIMULATOR" not in blob.upper()


def test_drag_bridge_matched_cycle_window_ignores_left_boundary_repayment():
    # Simulates a real 90-day boundary crossing:
    # - 2026-08-31 -$426.00 repays a pre-window advance (BEFORE first 90d inbound on 2026-09-09)
    # - 2026-09-09 +$400.00 -> 2026-10-15 -$426.00 (exact $26.00 fee)
    # - 2026-11-18 +$225.00 + 2026-11-20 +$75.00 -> exact $300.00 open unsettled advance!
    bridge = analyze_financial_drag_bridge(
        credit_lines=[],
        transactions=[
            {"tx_type": "expense", "amount_cents": 42_600, "merchant": "Dave", "tx_date": "2026-08-31"},
            {"tx_type": "income", "amount_cents": 40_000, "merchant": "Dave", "tx_date": "2026-09-09"},
            {"tx_type": "expense", "amount_cents": 42_600, "merchant": "Dave", "tx_date": "2026-10-15"},
            {"tx_type": "income", "amount_cents": 22_500, "merchant": "Dave", "tx_date": "2026-11-18"},
            {"tx_type": "income", "amount_cents": 7_500, "merchant": "Dave", "tx_date": "2026-11-20"},
        ],
        derogatories=[],
        extra_monthly_budget_cents=0,
        lookback_days=90,
        today=date(2026, 11, 26),
    )
    assert bridge.ewa_net_fee_bleed_cents == 2_600  # Exact $26.00 fee (NOT $452!)
    assert bridge.ewa_estimated_open_advance_cents == 30_000  # Exact $300.00 open advance!
    assert bridge.reclaimable_monthly_cents == 867  # round(2600 / 3) = $8.67/mo


def test_drag_bridge_handles_non_amortizing_minimum_payment_safely():
    # Card at 28% APR ($10,000 -> ~$233.33/mo interest) with $150 payment -> MUST NOT infinite loop!
    bridge = analyze_financial_drag_bridge(
        credit_lines=[
            {
                "id": 1,
                "name": "High APR Card",
                "kind": "credit_card",
                "balance_cents": 1_000_000,
                "credit_limit_cents": 1_000_000,
                "apr_bps": 2800,
                "min_payment_cents": 15_000,
                "statement_closing_day": 15,
                "payment_due_day": 10,
                "has_grace_period": False,
                "active": 1,
            }
        ],
        transactions=[],
        derogatories=[],
        extra_monthly_budget_cents=0,
        today=date(2026, 11, 26),
    )
    assert bridge.non_amortizing_warning is True
    assert bridge.min_amortizing_revolving_payment_cents > 23_333
    assert all(m.baseline_months is None for m in bridge.utilization_milestones)


def test_drag_bridge_api_endpoints_and_ui_wiring(tmp_path: Path):
    db_file = tmp_path / "bridge_api.sqlite3"
    sec_file = tmp_path / "secrets.enc"
    svc = FinanceService(db_path=db_file, secret_store_path=sec_file, kdf_iterations=20_000)
    try:
        app = create_app(db_path=db_file, service=svc, enforce_vault_lock=True)
        client = TestClient(app)
        setup_r = client.post("/api/v1/auth/setup", json={"password": "BridgeTDD!2026"})
        csrf = setup_r.json()["csrf_token"]
        headers = {CSRF_HEADER_NAME: csrf}

        client.post(
            "/api/v1/credit/lines",
            json={
                "name": "Sample Rewards Card",
                "kind": "credit_card",
                "balance_cents": 709_000,
                "credit_limit_cents": 710_000,
                "apr_bps": 1800,
                "min_payment_cents": 10_300,
                "statement_closing_day": 11,
                "payment_due_day": 8,
                "has_grace_period": False,
                "autopay_mode": "minimum",
            },
            headers=headers,
        )
        client.post(
            "/api/v1/transactions",
            json={
                "tx_type": "expense",
                "amount_cents": 10_500,
                "merchant": "INTEREST CHARGE-PURCHASES",
                "tx_date": date.today().isoformat(),
            },
            headers=headers,
        )

        # 1. Included in /api/v1/credit/report
        rep_r = client.get("/api/v1/credit/report?extra_monthly_cents=25000")
        assert rep_r.status_code == 200
        rep = rep_r.json()
        assert rep["drag_bridge"] is not None
        assert rep["drag_bridge"]["card_interest_burn_cents"] == 10_500
        assert len(rep["drag_bridge"]["utilization_milestones"]) == 7

        # 2. Dedicated /api/v1/credit/drag-bridge endpoint
        br_r = client.get("/api/v1/credit/drag-bridge?extra_monthly_cents=35000&lookback_days=365")
        assert br_r.status_code == 200
        br = br_r.json()
        assert br["baseline_monthly_payment_cents"] == 10_300 + 35_000
        assert br["utilization_milestones"][0]["target_util_pct"] == 88.9
    finally:
        svc.close()

    # 3. Frontend HTML & JS contract
    repo_root = Path(__file__).resolve().parents[2]
    index_html = (repo_root / "chronox_finance" / "web" / "index.html").read_text(encoding="utf-8")
    portfolio_js = (repo_root / "chronox_finance" / "web" / "js" / "portfolio.js").read_text(encoding="utf-8")
    charts_js = (repo_root / "chronox_finance" / "web" / "js" / "charts.js").read_text(encoding="utf-8")

    assert 'id="credit-drag-bridge-card"' in index_html
    assert 'id="credit-drag-bridge-wrap"' in index_html
    assert 'id="btn-analytics-open-bridge"' in index_html
    assert "renderDragUtilizationBridge" in portfolio_js
    assert "btn-analytics-open-bridge" in charts_js
