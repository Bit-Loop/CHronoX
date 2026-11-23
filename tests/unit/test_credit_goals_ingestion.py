"""Comprehensive tests for Robust Bill Detection, Multi-Format Ingestion,
Credit/Debt Optimizer (FICO/AZEO), and Credit-Safe Goal Planner."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi.testclient import TestClient

from chronox_finance.api import create_app
from chronox_finance.cli import run_cli
from chronox_finance.engines.credit_debt import CreditDebtOptimizer
from chronox_finance.engines.importer import ingest_statement
from chronox_finance.engines.recurring import detect_bill_anomalies, detect_recurring_bills
from chronox_finance.models import BalanceTransferRequest, GoalPlanRequest
from chronox_finance.service import FinanceService


class TestRobustBillDetectionAndAnomalies:
    def test_fuzzy_clustering_mutating_descriptors_and_price_hike(self):
        ref = date(2026, 11, 20)
        txs = [
            {"tx_type": "expense", "amount_cents": 1549, "merchant": "NETFLIX.COM*8821", "normalized_merchant": "netflix com", "tx_date": (ref - timedelta(days=85)).isoformat(), "jar_id": 1},
            {"tx_type": "expense", "amount_cents": 1549, "merchant": "NETFLIX INC CA", "normalized_merchant": "netflix ca", "tx_date": (ref - timedelta(days=55)).isoformat(), "jar_id": 1},
            {"tx_type": "expense", "amount_cents": 2299, "merchant": "Netflix Subscription", "normalized_merchant": "netflix subscription", "tx_date": (ref - timedelta(days=25)).isoformat(), "jar_id": 1},
        ]
        bills = detect_recurring_bills(txs, today=ref)
        assert len(bills) == 1
        assert bills[0].amount_cents == 2299
        assert bills[0].previous_amount_cents == 1549

    def test_bimonthly_semimonthly_and_month_end_calendar_anchoring(self):
        ref = date(2026, 5, 5)
        txs = [
            # Month-end rent surviving February (Jan 31 -> Feb 28 -> Mar 31 -> Apr 30)
            {"tx_type": "expense", "amount_cents": 140_000, "merchant": "Oakridge Rent", "normalized_merchant": "oakridge rent", "tx_date": "2026-01-31", "jar_id": 1},
            {"tx_type": "expense", "amount_cents": 140_000, "merchant": "Oakridge Rent", "normalized_merchant": "oakridge rent", "tx_date": "2026-02-28", "jar_id": 1},
            {"tx_type": "expense", "amount_cents": 140_000, "merchant": "Oakridge Rent", "normalized_merchant": "oakridge rent", "tx_date": "2026-03-31", "jar_id": 1},
            {"tx_type": "expense", "amount_cents": 140_000, "merchant": "Oakridge Rent", "normalized_merchant": "oakridge rent", "tx_date": "2026-04-30", "jar_id": 1},
            # Bi-monthly water utility (every 2 months)
            {"tx_type": "expense", "amount_cents": 9_200, "merchant": "City Water Utility", "normalized_merchant": "city water utility", "tx_date": "2025-12-10", "jar_id": 1},
            {"tx_type": "expense", "amount_cents": 9_450, "merchant": "City Water Utility", "normalized_merchant": "city water utility", "tx_date": "2026-02-10", "jar_id": 1},
            {"tx_type": "expense", "amount_cents": 9_300, "merchant": "City Water Utility", "normalized_merchant": "city water utility", "tx_date": "2026-04-11", "jar_id": 1},
            # Semi-monthly (1st and 15th)
            {"tx_type": "expense", "amount_cents": 15_000, "merchant": "Therapy Copay", "normalized_merchant": "therapy copay", "tx_date": "2026-03-01", "jar_id": 2},
            {"tx_type": "expense", "amount_cents": 15_000, "merchant": "Therapy Copay", "normalized_merchant": "therapy copay", "tx_date": "2026-03-15", "jar_id": 2},
            {"tx_type": "expense", "amount_cents": 15_000, "merchant": "Therapy Copay", "normalized_merchant": "therapy copay", "tx_date": "2026-04-01", "jar_id": 2},
            {"tx_type": "expense", "amount_cents": 15_000, "merchant": "Therapy Copay", "normalized_merchant": "therapy copay", "tx_date": "2026-04-15", "jar_id": 2},
            # Internal CC autopay must be excluded from bills!
            {"tx_type": "expense", "amount_cents": 50_000, "merchant": "CHASE CREDIT CRD AUTOPAY", "normalized_merchant": "chase credit crd autopay", "tx_date": "2026-03-10", "jar_id": 1},
            {"tx_type": "expense", "amount_cents": 50_000, "merchant": "CHASE CREDIT CRD AUTOPAY", "normalized_merchant": "chase credit crd autopay", "tx_date": "2026-04-10", "jar_id": 1},
        ]
        bills = detect_recurring_bills(txs, today=ref)
        by_name = {b.name: b for b in bills}
        assert "Oakridge Rent" in by_name and by_name["Oakridge Rent"].cadence.value == "monthly"
        assert "City Water Utility" in by_name and by_name["City Water Utility"].cadence.value == "bimonthly"
        assert "Therapy Copay" in by_name and by_name["Therapy Copay"].cadence.value == "semimonthly"
        assert "CHASE CREDIT CRD AUTOPAY" not in by_name

    def test_detects_double_charge_and_missed_overdue_bill(self):
        ref = date(2026, 11, 20)
        txs = [
            {"tx_type": "expense", "amount_cents": 4_250, "merchant": "Cloud Hosting", "normalized_merchant": "cloud hosting", "tx_date": "2026-11-15"},
            {"tx_type": "expense", "amount_cents": 4_250, "merchant": "Cloud Hosting", "normalized_merchant": "cloud hosting", "tx_date": "2026-11-16"},
        ]
        bills = [
            {"name": "Internet Bill", "amount_cents": 7_500, "previous_amount_cents": None, "next_due_date": "2026-11-12", "active": 1},
        ]
        anomalies = detect_bill_anomalies(txs, bills, today=ref)
        kinds = {a.kind for a in anomalies}
        assert "double_charge" in kinds
        assert "missed_bill" in kinds


class TestMultiFormatStatementIngestion:
    def test_csv_with_bank_preamble_semicolon_euro_and_transfer_detection(self):
        raw = """Account Summary: Checking 1234
Beginning Balance: $3,200.00

Transaction Date;Description;Amount;Type;Memo
11/02/2026;TRADER JOES;84,50;Debit;Groceries
11/04/2026;CHASE CREDIT CRD AUTOPAY;450,00;Debit;Card Autopay
11/05/2026;ACME PAYROLL;2.450,00;Credit;Salary
"""
        res = ingest_statement(raw, filename="bofa_export.csv")
        assert res.format_detected == "csv"
        assert len(res.rows) == 3
        assert res.rows[0].amount_cents == 8450 and res.rows[0].tx_type == "expense"
        assert res.rows[1].tx_type == "transfer"
        assert res.rows[2].amount_cents == 245_000 and res.rows[2].tx_type == "income"
        assert res.transfers_detected == 1

    def test_ofx_qfx_parsing_with_fitid_and_ledger_balance(self):
        ofx = """OFXHEADER:100
DATA:OFXSGML
<OFX>
<BANKMSGSRSV1><STMTTRNRS><STMTRS>
<BANKTRANLIST>
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20261110120000[-5:EST]
<TRNAMT>-64.25
<FITID>TXN-20261110-001
<NAME>WHOLE FOODS MARKET
<MEMO>Organic groceries
</STMTTRN>
<STMTTRN>
<TRNTYPE>CREDIT
<DTPOSTED>20261114
<TRNAMT>2500.00
<FITID>TXN-20261114-002
<NAME>EMPLOYER PAYROLL
</STMTTRN>
</BANKTRANLIST>
<LEDGERBAL><BALAMT>4125.75<DTASOF>20261115</LEDGERBAL>
</STMTRS></STMTTRNRS></BANKMSGSRSV1>
</OFX>
"""
        res = ingest_statement(ofx, filename="chase.qfx")
        assert res.format_detected == "ofx"
        assert len(res.rows) == 2
        assert res.rows[0].external_id == "TXN-20261110-001"
        assert res.rows[0].amount_cents == 6425 and res.rows[0].tx_type == "expense"
        assert res.rows[1].amount_cents == 250_000 and res.rows[1].tx_type == "income"
        assert res.detected_balance_cents == 412_575

    def test_qif_and_json_statement_parsing(self):
        qif = """!Type:Bank
D11/12'26
T-19.99
PSpotify Premium
MMusic sub
^
D11/15'26
T1200.00
PFreelance Client
^
"""
        res_qif = ingest_statement(qif, filename="export.qif")
        assert res_qif.format_detected == "qif"
        assert len(res_qif.rows) == 2
        assert res_qif.rows[0].tx_date == "2026-11-12" and res_qif.rows[0].amount_cents == 1999

        js = '{"transactions": [{"date": "2026-11-16", "merchant": "Target", "amount": -42.10, "id": "plaid-1"}]}'
        res_json = ingest_statement(js, filename="plaid.json")
        assert res_json.format_detected == "json"
        assert len(res_json.rows) == 1
        assert res_json.rows[0].external_id == "plaid-1"


class TestCreditAndDebtOptimizer:
    def test_azeo_plan_cliffs_promo_grace_autopay_and_4_strategies(self):
        ref = date(2026, 11, 20)
        opt = CreditDebtOptimizer()
        raw_lines = [
            {
                "id": 1, "name": "Chase Sapphire", "issuer": "Chase", "kind": "credit_card",
                "balance_cents": 25_000, "credit_limit_cents": 600_000, "apr_bps": 2149,
                "min_payment_cents": 3_500, "statement_closing_day": 18, "payment_due_day": 13,
                "opened_date": "2019-03-01", "has_grace_period": 1, "autopay_mode": "statement", "active": 1,
            },
            {
                "id": 2, "name": "Amex Everyday", "issuer": "Amex", "kind": "credit_card",
                "balance_cents": 180_000, "credit_limit_cents": 400_000, "apr_bps": 2799,
                "min_payment_cents": 5_500, "statement_closing_day": 8, "payment_due_day": 3,
                "opened_date": "2022-05-01", "has_grace_period": 0, "autopay_mode": "none", "active": 1,
            },
            {
                "id": 3, "name": "Discover Promo", "issuer": "Discover", "kind": "credit_card",
                "balance_cents": 60_000, "credit_limit_cents": 300_000, "apr_bps": 2499,
                "promo_apr_bps": 0, "promo_ends_date": (ref + timedelta(days=60)).isoformat(),
                "min_payment_cents": 2_500, "statement_closing_day": 25, "payment_due_day": 20,
                "opened_date": "2023-01-15", "has_grace_period": 1, "autopay_mode": "minimum", "active": 1,
            },
        ]

        rep = opt.analyze(
            raw_lines=raw_lines,
            smoothed_monthly_income_cents=500_000,
            extra_monthly_budget_cents=35_000,
            today=ref,
        )
        assert rep.total_revolving_balance_cents == 265_000
        assert rep.total_revolving_limit_cents == 1_300_000
        assert rep.autopay_missing_count == 1
        assert rep.oldest_account_name == "Chase Sapphire"
        assert rep.recommended_strategy == "credit_first"  # Amex is at 45% > 29% cliff!

        # Alerts must flag autopay, promo cliff, per-card utilization, and grace period lost!
        alert_cats = {a.category for a in rep.alerts}
        assert {"autopay_shield", "promo_cliff", "per_card_utilization", "grace_period_lost"}.issubset(alert_cats)

        # AZEO: exactly 1 primary card (Chase Sapphire) keeps <= 4.9% balance, Amex & Discover zero out!
        azeo_by_card = {a.card_name: a for a in rep.azeo_plan}
        assert azeo_by_card["Amex Everyday"].target_statement_balance_cents == 0
        assert azeo_by_card["Amex Everyday"].pay_before_close_cents == 180_000
        assert azeo_by_card["Discover Promo"].target_statement_balance_cents == 0
        assert 0 < azeo_by_card["Chase Sapphire"].resulting_util_pct < 5.0

        # 4 strategies compared: Avalanche has <= Snowball interest; Snowball gets fastest first win
        by_strat = {s.strategy: s for s in rep.strategies}
        assert set(by_strat.keys()) == {"adhd_hybrid", "credit_first", "avalanche", "snowball"}
        assert by_strat["avalanche"].total_interest_cents <= by_strat["snowball"].total_interest_cents
        assert by_strat["credit_first"].first_target_name == "Amex Everyday"

    def test_all_zero_penalty_detected_and_balance_transfer_breakeven(self):
        opt = CreditDebtOptimizer()
        rep = opt.analyze(
            raw_lines=[
                {
                    "id": 1, "name": "Sapphire", "kind": "credit_card",
                    "balance_cents": 0, "credit_limit_cents": 500_000, "apr_bps": 2199,
                    "statement_closing_day": 15, "payment_due_day": 10, "autopay_mode": "statement", "active": 1,
                }
            ],
            smoothed_monthly_income_cents=400_000,
        )
        assert rep.all_zero_penalty_risk is True
        assert any(a.category == "azeo_inactivity" for a in rep.alerts)

        # Strong YES balance transfer (28% APR -> 0% 15mo w/ 3% fee)
        bt_yes = opt.evaluate_balance_transfer(
            BalanceTransferRequest(
                balance_cents=300_000,
                current_apr_bps=2800,
                transfer_fee_bps=300,
                promo_months=15,
                monthly_payment_cents=25_000,
            )
        )
        assert bt_yes.verdict == "strong_yes"
        assert bt_yes.net_savings_cents > 20_000

        # SKIP balance transfer (low balance paid off in 2 months -> fee > interest saved)
        bt_skip = opt.evaluate_balance_transfer(
            BalanceTransferRequest(
                balance_cents=50_000,
                current_apr_bps=1800,
                transfer_fee_bps=500,
                promo_months=12,
                monthly_payment_cents=30_000,
            )
        )
        assert bt_skip.verdict == "skip"


class TestGoalOptimizerAndEndToEndAPI:
    def test_1000_gadget_goal_plan_respects_credit_optimizer_and_api_crud(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OPENCODE_GO_API_KEY", raising=False)
        monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
        db_file = str(tmp_path / "credit_goals.sqlite3")
        svc = FinanceService(db_path=db_file, secret_store_path=tmp_path / "secrets.enc")
        app = create_app(db_path=db_file, service=svc, enforce_vault_lock=False)
        client = TestClient(app)

        try:
            from tests.fixtures.seed_workspace import seed_workspace

            seed_workspace(svc)
            assert client.post("/api/demo/load").status_code == 404

            # 1. Credit Report API
            rep = client.get("/api/credit/report?extra_monthly_cents=30000").json()
            assert len(rep["credit_lines"]) == 4
            assert len(rep["azeo_plan"]) == 3
            assert len(rep["strategies"]) == 4

            # 2. Optimize a $1,000 gadget respecting credit optimizers!
            plan_res = client.post(
                "/api/goals/plan",
                json={"item_name": "Mac Mini M4", "target_cents": 100_000, "saved_cents": 10_000},
            )
            assert plan_res.status_code == 200
            plan = plan_res.json()
            assert plan["remaining_cents"] == 90_000
            assert plan["high_apr_debt_warning"] is not None  # Amex is at 26.99% APR
            assert len(plan["accelerators"]) >= 1
            opts = {o["path_id"]: o for o in plan["options"]}
            assert set(opts.keys()) == {"cash_sinking_fund", "credit_optimized_buy", "balanced_debt_goal_split"}
            # Credit-optimized path MUST pick a grace-period card (Chase Sapphire, NOT Amex!)
            assert "Chase Sapphire" in opts["credit_optimized_buy"]["title"]
            assert "Amex" not in opts["credit_optimized_buy"]["title"]
            assert "statement close" in opts["credit_optimized_buy"]["credit_impact_note"].lower()

            # 3. Goals CRUD + contribute (+XP)
            g_create = client.post(
                "/api/goals",
                json={"name": "Mac Mini M4", "target_cents": 100_000, "saved_cents": 10_000, "category": "gadget"},
            )
            assert g_create.status_code == 200
            gid = g_create.json()["id"]
            g_contrib = client.post(f"/api/goals/{gid}/contribute", json={"amount_cents": 90_000})
            assert g_contrib.status_code == 200
            assert g_contrib.json()["status"] == "funded"

            # 4. Multi-format statement preview & OFX import
            ofx_bytes = b"""<OFX><BANKTRANLIST>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20261118<TRNAMT>-31.50<FITID>OFX-QA-1<NAME>TRADER JOES</STMTTRN>
</BANKTRANLIST><LEDGERBAL><BALAMT>3200.00</LEDGERBAL></OFX>"""
            r_prev = client.post("/api/import/preview", files={"file": ("bank.ofx", ofx_bytes, "application/octet-stream")})
            assert r_prev.status_code == 200
            assert r_prev.json()["format_detected"] == "ofx"
            assert r_prev.json()["new_count"] == 1
            assert r_prev.json()["detected_balance_cents"] == 320_000

            r_imp = client.post("/api/import/statement", files={"file": ("bank.ofx", ofx_bytes, "application/octet-stream")})
            assert r_imp.status_code == 200 and r_imp.json()["imported_count"] == 1
            # Re-importing exact same FITID deduplicates to 0!
            r_imp2 = client.post("/api/import/statement", files={"file": ("bank.ofx", ofx_bytes, "application/octet-stream")})
            assert r_imp2.status_code == 200 and r_imp2.json()["duplicate_count"] == 1

            # 5. Ask Coach about a $1000 gadget triggers deterministic Goal + Credit grounding!
            qa = client.post("/api/ai/ask", json={"question": "How can I afford a $1000 gadget without hurting my credit?"})
            assert qa.status_code == 200
            assert "credit" in qa.json()["answer"].lower() or "split" in qa.json()["answer"].lower()
            assert "aggregate_utilization_pct" in qa.json()["verified_numbers"]

            # 6. CLI `credit` and `goal` commands work cleanly
            assert run_cli(["--db", db_file, "credit", "--extra", "300"]) == 0
            assert run_cli(["--db", db_file, "goal", "OLED Monitor", "1000"]) == 0
        finally:
            svc.close()

    def test_credit_path_blocked_when_no_grace_period_card_available(self):
        svc = FinanceService(db_path=":memory:")
        try:
            svc.storage.create_credit_line(
                {
                    "name": "Revolving Card",
                    "kind": "credit_card",
                    "balance_cents": 200_000,
                    "credit_limit_cents": 250_000,
                    "apr_bps": 2899,
                    "has_grace_period": False,
                    "autopay_mode": "minimum",
                }
            )
            plan = svc.optimize_goal_plan(GoalPlanRequest(item_name="$1000 Gadget", target_cents=100_000))
            cred_opt = next(o for o in plan.options if o.path_id == "credit_optimized_buy")
            assert cred_opt.credit_safe is False
            assert "blocked" in cred_opt.title.lower()
        finally:
            svc.close()
