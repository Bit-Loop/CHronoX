"""Unit tests for ChronoX Finance deterministic engines (zero floating-point bugs)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from chronox_finance.engines.adhd import compute_level, compute_weekly_reset_update, enrich_impulse
from chronox_finance.engines.autobudget import AutoBudgetEngine
from chronox_finance.engines.categorizer import TransactionCategorizer, normalize_merchant
from chronox_finance.engines.importer import export_transactions_csv, parse_bank_csv
from chronox_finance.engines.nlp_parser import parse_quick_add
from chronox_finance.engines.recurring import detect_payday_schedule, detect_recurring_bills
from chronox_finance.models import ImpulseStatus
from chronox_finance.service import FinanceService


class TestNLPQuickAddParser:
    def test_parses_expense_income_and_impulse_phrases(self):
        ref = date(2026, 11, 20)

        p1 = parse_quick_add("coffee 6.50", today=ref)
        assert p1.intent == "transaction"
        assert p1.tx_type == "expense"
        assert p1.amount_cents == 650
        assert p1.merchant == "Coffee"

        p2 = parse_quick_add("paycheck +2450.25", today=ref)
        assert p2.intent == "transaction"
        assert p2.tx_type == "income"
        assert p2.amount_cents == 245_025

        p3 = parse_quick_add("target 48.20 yesterday", today=ref)
        assert p3.tx_date == "2026-11-19"
        assert p3.amount_cents == 4820
        assert p3.merchant == "Target"

        p4 = parse_quick_add("paid electric bill 112.40", today=ref)
        assert p4.tx_type == "expense"
        assert p4.amount_cents == 11_240
        assert "Electric" in p4.merchant

        p5 = parse_quick_add("impulse $199 mechanical keyboard 72h", today=ref)
        assert p5.intent == "impulse"
        assert p5.amount_cents == 19_900
        assert p5.cooling_hours == 72
        assert "Mechanical Keyboard" in p5.merchant

    def test_rejects_missing_amount(self):
        with pytest.raises(ValueError, match="dollar amount"):
            parse_quick_add("bought some snacks")


class TestCategorizerAndMerchantLearning:
    def test_learned_rule_overrides_builtin(self):
        cat = TransactionCategorizer()
        jars = {
            "bills": {"id": 1, "slug": "bills"},
            "daily": {"id": 2, "slug": "daily"},
            "fun": {"id": 3, "slug": "fun"},
        }
        # Default maps Starbucks -> fun
        jid, sub, conf, review = cat.categorize("STARBUCKS #4421", "", jars, {})
        assert jid == 3
        assert conf > 0.85
        assert not review

        # User overrides Starbucks -> daily once; learned rule takes precedence forever
        norm = normalize_merchant("STARBUCKS #4421")
        learned = {norm: {"jar_id": 2, "subcategory": "Work Fuel"}}
        jid2, sub2, conf2, review2 = cat.categorize("Starbucks #9988", "", jars, learned)
        assert jid2 == 2
        assert sub2 == "Work Fuel"
        assert conf2 == 0.99
        assert not review2


class TestRecurringBillsAndPaydayDetector:
    def test_detects_monthly_bill_price_hike_and_biweekly_payday(self):
        ref = date(2026, 11, 20)
        txs = [
            # Bi-weekly paychecks
            {"tx_type": "income", "amount_cents": 240_000, "merchant": "Payroll", "normalized_merchant": "payroll", "tx_date": (ref - timedelta(days=34)).isoformat()},
            {"tx_type": "income", "amount_cents": 240_000, "merchant": "Payroll", "normalized_merchant": "payroll", "tx_date": (ref - timedelta(days=20)).isoformat()},
            {"tx_type": "income", "amount_cents": 240_000, "merchant": "Payroll", "normalized_merchant": "payroll", "tx_date": (ref - timedelta(days=6)).isoformat()},
            # Monthly subscription with price hike $15.49 -> $22.99
            {"tx_type": "expense", "amount_cents": 1549, "merchant": "Netflix", "normalized_merchant": "netflix", "tx_date": (ref - timedelta(days=65)).isoformat(), "jar_id": 1},
            {"tx_type": "expense", "amount_cents": 1549, "merchant": "Netflix", "normalized_merchant": "netflix", "tx_date": (ref - timedelta(days=35)).isoformat(), "jar_id": 1},
            {"tx_type": "expense", "amount_cents": 2299, "merchant": "Netflix", "normalized_merchant": "netflix", "tx_date": (ref - timedelta(days=5)).isoformat(), "jar_id": 1},
        ]

        bills = detect_recurring_bills(txs, today=ref)
        assert len(bills) == 1
        assert bills[0].name == "Netflix"
        assert bills[0].amount_cents == 2299
        assert bills[0].previous_amount_cents == 1549
        assert bills[0].is_subscription is True

        payday = detect_payday_schedule(txs, today=ref)
        assert payday.cadence_days == 14
        assert payday.next_payday_date == (ref + timedelta(days=8)).isoformat()
        assert payday.estimated_paycheck_cents == 240_000


class TestSelfUpdatingAutoBudgetMath:
    def test_safe_to_spend_and_trading_firewall_exact_cents(self):
        ref = date(2026, 11, 20)
        engine = AutoBudgetEngine()
        accounts = [
            {"id": 1, "account_type": "checking", "balance_cents": 300_000, "is_liquid": 1},
            {"id": 2, "account_type": "credit", "balance_cents": -25_000, "is_liquid": 0},
        ]
        jars = [
            {"id": 1, "slug": "bills", "name": "Bills", "kind": "bills", "icon": "H", "color": "#60a5fa", "priority": 1, "target_pct": 45.0, "monthly_target_cents": 180_000, "auto_allocated_cents": 180_000, "rollover_cents": 0, "is_locked": 1, "rollover_enabled": 1, "rollover_cap_cents": None},
            {"id": 2, "slug": "daily", "name": "Daily", "kind": "daily", "icon": "G", "color": "#34d399", "priority": 2, "target_pct": 25.0, "monthly_target_cents": 100_000, "auto_allocated_cents": 100_000, "rollover_cents": 0, "is_locked": 0, "rollover_enabled": 1, "rollover_cap_cents": None},
            {"id": 3, "slug": "fun", "name": "Fun", "kind": "fun", "icon": "F", "color": "#fbbf24", "priority": 3, "target_pct": 15.0, "monthly_target_cents": 60_000, "auto_allocated_cents": 60_000, "rollover_cents": 0, "is_locked": 0, "rollover_enabled": 1, "rollover_cap_cents": None},
            {"id": 4, "slug": "safety", "name": "Safety", "kind": "safety", "icon": "S", "color": "#a78bfa", "priority": 4, "target_pct": 10.0, "monthly_target_cents": 40_000, "auto_allocated_cents": 40_000, "rollover_cents": 0, "is_locked": 1, "rollover_enabled": 1, "rollover_cap_cents": None},
            {"id": 5, "slug": "invest", "name": "Invest", "kind": "invest", "icon": "I", "color": "#2dd4bf", "priority": 5, "target_pct": 5.0, "monthly_target_cents": 20_000, "auto_allocated_cents": 20_000, "rollover_cents": 0, "is_locked": 1, "rollover_enabled": 1, "rollover_cap_cents": None},
        ]
        bills = [
            {"id": 10, "name": "Rent", "normalized_merchant": "rent", "amount_cents": 120_000, "previous_amount_cents": None, "cadence": "monthly", "next_due_date": (ref + timedelta(days=5)).isoformat(), "jar_id": 1, "is_subscription": 0, "is_essential": 1, "auto_pay": 1, "active": 1, "confidence": 0.99, "flagged_zombie": 0},
        ]
        settings = {
            "emergency_buffer_cents": 50_000,
            "payday_cadence_days": 14,
            "next_payday_date": (ref + timedelta(days=10)).isoformat(),
            "expected_paycheck_cents": 200_000,
        }
        res = engine.compute(
            accounts=accounts,
            jars=jars,
            transactions=[],
            recurring_bills=bills,
            transfers=[],
            settings=settings,
            today=ref,
        )
        s = res.safe_to_spend
        # 3000 - 1200 (bill) - 250 (credit card) - 500 (emergency buffer) - locked(safety + invest)
        assert s.total_liquid_cash_cents == 300_000
        assert s.unpaid_bills_until_payday_cents == 120_000
        assert s.credit_card_reserved_cents == 25_000
        assert s.emergency_buffer_cents == 50_000
        expected_safe = max(
            0,
            300_000 - 120_000 - 25_000 - 50_000 - s.locked_jar_reserves_cents,
        )
        assert s.safe_to_spend_cents == expected_safe
        assert s.daily_safe_to_spend_cents == expected_safe // 10
        assert res.safe_to_invest_cents > 0

        # If liquid cash drops below bills + buffer, Safe-to-Invest MUST hard-clamp to $0!
        accounts[0]["balance_cents"] = 100_000
        res_tight = engine.compute(
            accounts=accounts,
            jars=jars,
            transactions=[],
            recurring_bills=bills,
            transfers=[],
            settings=settings,
            today=ref,
        )
        assert res_tight.safe_to_spend.safe_to_spend_cents == 0
        assert res_tight.safe_to_invest_cents == 0

    def test_rebalance_plan_never_raids_bills_jar(self):
        svc = FinanceService(db_path=":memory:")
        try:
            # Overspend Daily Living intentionally
            daily = svc.storage.get_jar_by_slug("daily")
            svc.add_transaction_raw(
                account_id=None,
                jar_id=daily["id"],
                tx_type="expense",
                amount_cents=120_000,
                merchant="Big Grocery Haul",
                tx_date=date.today().isoformat(),
            )
            dash = svc.get_dashboard()
            assert len(dash.rebalance_plans) >= 1
            bills = svc.storage.get_jar_by_slug("bills")
            for p in dash.rebalance_plans:
                assert p.from_jar_id != bills["id"]
        finally:
            svc.close()


class TestCSVImporterAndFormulaSanitization:
    def test_parses_bank_csv_and_neutralizes_formula_injection_on_export(self):
        csv_raw = """Date,Description,Amount,Memo
11/01/2026,TRADER JOES #123,-84.50,Groceries
11/05/2026,ACME PAYROLL,2400.00,Direct Deposit
11/08/2026,=CMD|' /C calc'!A0,-12.00,+SUM(A1:A2)
"""
        rows, warnings = parse_bank_csv(csv_raw)
        assert len(rows) == 3
        assert rows[0].tx_type == "expense" and rows[0].amount_cents == 8450
        assert rows[1].tx_type == "income" and rows[1].amount_cents == 240_000

        exported = export_transactions_csv(
            [
                {
                    "tx_date": "2026-11-08",
                    "tx_type": "expense",
                    "merchant": "=CMD|' /C calc'!A0",
                    "jar_name": "Fun",
                    "subcategory": "",
                    "amount_cents": 1200,
                    "memo": "+SUM(A1:A2)",
                    "source": "csv",
                }
            ]
        )
        assert "'=CMD" in exported
        assert "'+SUM" in exported


class TestADHDBehavioralMechanics:
    def test_grace_freeze_protects_streak_when_user_misses_a_week(self):
        ref = date(2026, 11, 20)
        prog = {
            "weekly_reset_streak": 4,
            "grace_days_available": 1,
            "last_reset_date": (ref - timedelta(days=14)).isoformat(),
            "dopamine_xp": 400,
            "recent_wins": [],
        }
        updated = compute_weekly_reset_update(prog, today=ref)
        # Grace freeze saved streak instead of zeroing it out!
        assert updated["weekly_reset_streak"] == 5
        assert updated["grace_days_available"] == 0
        assert "Grace Freeze" in updated["recent_wins"][0]

    def test_impulse_work_hours_and_level_progression(self):
        raw = {
            "id": 1,
            "item_name": "Fancy Espresso Machine",
            "price_cents": 60_000,
            "reason": "Hyperfixation",
            "url": "",
            "cooling_hours": 48,
            "created_at": "2026-11-17T00:00:00Z",
            "unlocks_at": "2026-11-19T00:00:00Z",
            "status": "cooling",
        }
        enriched = enrich_impulse(raw, hourly_wage_cents=3000, safe_to_spend_cents=50_000)
        assert enriched.status == ImpulseStatus.READY
        assert enriched.work_hours_cost == 20.0
        assert enriched.can_afford_safely is False
        assert compute_level(850)[0] == 4
