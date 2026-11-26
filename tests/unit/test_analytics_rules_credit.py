"""Comprehensive unit tests for MoneyStill Analytics, Scenario Projections,
Firefly Rule Engine & Split Validator, 5-Factor Credit Analysis + 5/24,
and 730-Day Variable Bill / Split-Aware Quant & AutoBudget upgrades."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from chronox_finance.engines.analytics import (
    AnalyticsEngine,
    reconstruct_net_worth_timeline,
    resolve_period,
    simulate_scenario,
)
from chronox_finance.engines.autobudget import AutoBudgetEngine
from chronox_finance.engines.credit_debt import (
    FACTOR_ANALYSIS_DISCLAIMER,
    CreditDebtOptimizer,
    compute_derogatory_drop_off,
    compute_inquiry_dates,
    evaluate_five_twenty_four,
)
from chronox_finance.engines.goal_planner import GoalOptimizerEngine
from chronox_finance.engines.quant_math import QuantMathEngine
from chronox_finance.engines.recurring import (
    detect_bill_anomalies,
    detect_recurring_bills,
    find_matching_bill,
    forecast_bill_amount_cents,
    match_transaction_to_bill,
)
from chronox_finance.engines.rules import (
    RuleEngine,
    SplitValidationError,
    UnsafeRegexError,
    compute_exact_pct_splits,
    dry_run_rule,
    validate_safe_regex,
    validate_splits,
)
from chronox_finance.models import (
    ActionSpec,
    GoalPlanRequest,
    RuleCreate,
    RuleOut,
    ScenarioOverride,
    TriggerSpec,
)


def _sample_jars():
    return [
        {"id": 1, "slug": "bills", "name": "Bills & Essentials", "kind": "bills", "icon": "home", "color": "#3b82f6", "priority": 1, "target_pct": 45.0, "monthly_target_cents": 180_000, "auto_allocated_cents": 180_000, "rollover_cents": 0, "is_locked": 1, "rollover_enabled": 1, "rollover_cap_cents": None},
        {"id": 2, "slug": "daily", "name": "Daily Living", "kind": "daily", "icon": "cart", "color": "#10b981", "priority": 2, "target_pct": 25.0, "monthly_target_cents": 100_000, "auto_allocated_cents": 100_000, "rollover_cents": 0, "is_locked": 0, "rollover_enabled": 1, "rollover_cap_cents": None},
        {"id": 3, "slug": "fun", "name": "Guilt-Free Fun", "kind": "fun", "icon": "bolt", "color": "#f59e0b", "priority": 3, "target_pct": 15.0, "monthly_target_cents": 60_000, "auto_allocated_cents": 60_000, "rollover_cents": 0, "is_locked": 0, "rollover_enabled": 1, "rollover_cap_cents": None},
        {"id": 4, "slug": "safety", "name": "Safety Net", "kind": "safety", "icon": "shield", "color": "#a855f7", "priority": 4, "target_pct": 10.0, "monthly_target_cents": 40_000, "auto_allocated_cents": 40_000, "rollover_cents": 0, "is_locked": 1, "rollover_enabled": 1, "rollover_cap_cents": None},
        {"id": 5, "slug": "invest", "name": "Invest Vault", "kind": "invest", "icon": "trend", "color": "#06b6d4", "priority": 5, "target_pct": 5.0, "monthly_target_cents": 20_000, "auto_allocated_cents": 20_000, "rollover_cents": 0, "is_locked": 1, "rollover_enabled": 1, "rollover_cap_cents": None},
    ]


class TestMoneyStillAnalyticsAndWhatIf:
    def test_period_resolution_30d_90d_1y_2y_and_single_month(self):
        ref = date(2026, 11, 20)
        p30, s30, e30, m30 = resolve_period("30d", today=ref)
        assert p30 == "30d" and (e30 - s30).days == 29 and m30 == 1

        p90, s90, e90, m90 = resolve_period("90d", today=ref)
        assert p90 == "90d" and (e90 - s90).days == 89 and m90 == 3

        p1y, s1y, e1y, m1y = resolve_period("1y", today=ref)
        assert p1y == "1y" and (e1y - s1y).days == 364 and m1y == 12

        p2y, s2y, e2y, m2y = resolve_period("2y", today=ref)
        assert p2y == "2y" and (e2y - s2y).days == 729 and m2y == 24

        pm, sm, em, mm = resolve_period("2026-02", today=ref)
        assert pm == "2026-02" and sm == date(2026, 2, 1) and em == date(2026, 2, 28) and mm == 1

        with pytest.raises(ValueError):
            resolve_period("invalid-period", today=ref)

    def test_split_aware_analytics_sankey_donut_top5_and_color_parity(self):
        ref = date(2026, 11, 20)
        jars = _sample_jars() + [
            {"id": 6, "slug": "health", "name": "Health", "kind": "daily", "icon": "shield", "color": "#ef4444"},
            {"id": 7, "slug": "travel", "name": "Travel", "kind": "fun", "icon": "bolt", "color": "#ec4899"},
        ]

        # Income = $5,000 ($4,200 Payroll + $800 Freelance)
        # Expenses = $2,600 across 7 jars, including a $300 Costco split ($180 Bills + $120 Daily)
        # NEVER double-counted!
        txs = [
            {"id": 1, "tx_type": "income", "amount_cents": 420_000, "merchant": "Acme Corp", "subcategory": "Payroll", "tx_date": "2026-11-05"},
            {"id": 2, "tx_type": "income", "amount_cents": 80_000, "merchant": "Client A", "subcategory": "Freelance", "tx_date": "2026-11-12"},
            # Split parent ($300, parent jar_id=2 should be ignored in favor of child splits!)
            {
                "id": 3,
                "tx_type": "expense",
                "amount_cents": 30_000,
                "jar_id": 2,
                "merchant": "Costco",
                "tx_date": "2026-11-06",
                "splits": [
                    {"jar_id": 1, "amount_cents": 18_000, "memo": "Household essentials"},
                    {"jar_id": 2, "amount_cents": 12_000, "memo": "Groceries"},
                ],
            },
            {"id": 4, "tx_type": "expense", "amount_cents": 120_000, "jar_id": 1, "merchant": "Rent", "tx_date": "2026-11-01"},
            {"id": 5, "tx_type": "expense", "amount_cents": 40_000, "jar_id": 3, "merchant": "Dining", "tx_date": "2026-11-08"},
            {"id": 6, "tx_type": "expense", "amount_cents": 30_000, "jar_id": 4, "merchant": "Insurance", "tx_date": "2026-11-09"},
            {"id": 7, "tx_type": "expense", "amount_cents": 20_000, "jar_id": 5, "merchant": "ETF Contribution", "tx_date": "2026-11-10"},
            {"id": 8, "tx_type": "expense", "amount_cents": 15_000, "jar_id": 6, "merchant": "Clinic", "tx_date": "2026-11-11"},
            {"id": 9, "tx_type": "expense", "amount_cents": 5_000, "jar_id": 7, "merchant": "Transit", "tx_date": "2026-11-14"},
        ]

        overview = AnalyticsEngine().build_overview(
            transactions=txs,
            jars=jars,
            accounts=[{"id": 1, "account_type": "checking", "balance_cents": 500_000, "is_liquid": 1}],
            period="2026-11",
            today=ref,
        )

        # Exact split-aware totals (zero double counting)
        assert overview.total_income_cents == 500_000
        assert overview.total_expense_cents == 260_000
        assert overview.net_surplus_cents == 240_000
        assert overview.savings_rate_pct == 48.0
        assert overview.avg_monthly_surplus_cents == 240_000

        # Bills = 120,000 + 18,000 (split) = 138,000; Daily = 12,000 (split ONLY!)
        top_by_label = {item.label: item for item in overview.top_5_expenses}
        assert len(overview.top_5_expenses) == 5
        assert top_by_label["Bills & Essentials"].amount_cents == 138_000
        assert top_by_label["Guilt-Free Fun"].amount_cents == 40_000
        # Daily (12,000) + Travel (5,000) = 17,000 rolled into "Others"!
        donut_by_label = {item.label: item for item in overview.expense_distribution}
        assert donut_by_label["Others"].amount_cents == 17_000
        assert sum(item.amount_cents for item in overview.expense_distribution) == 260_000

        # Sankey exact cent conservation: sum(col0->hub) == hub == sum(hub->col2)
        in_links = [lk for lk in overview.sankey_links if lk.target == "hub:total_income"]
        out_links = [lk for lk in overview.sankey_links if lk.source == "hub:total_income"]
        assert sum(lk.amount_cents for lk in in_links) == 500_000
        assert sum(lk.amount_cents for lk in out_links) == 500_000

        # Savings & Invest flow present with exact surplus
        sav_node = next(n for n in overview.sankey_nodes if n.id == "out:savings_invest")
        assert sav_node.amount_cents == 240_000
        assert sav_node.pct_of_income == 48.0

        # Exact color parity between Top 5, Sankey Column 2, and Expense Donut
        sankey_col2_colors = {n.label: n.color for n in overview.sankey_nodes if n.column == 2}
        for item in overview.expense_distribution:
            assert sankey_col2_colors[item.label] == item.color

        # Monthly trend contains 2026-11 with exact surplus
        nov = next(pt for pt in overview.monthly_trends if pt.month == "2026-11")
        assert nov.income_cents == 500_000
        assert nov.expense_cents == 260_000
        assert nov.surplus_cents == 240_000

    def test_net_worth_timeline_730d_and_subcent_crypto_exact_math(self):
        ref = date(2026, 11, 20)
        accounts = [
            {"id": 1, "account_type": "checking", "balance_cents": 400_000, "is_liquid": 1},
            {"id": 2, "account_type": "credit", "balance_cents": -50_000, "is_liquid": 0},
        ]
        # 10,000,000 SHIB at $0.00002415 -> $241.50 = 24,150 cents exact!
        holdings = [
            {"symbol": "SHIB", "asset_class": "crypto", "quantity_e8": 10_000_000 * 10**8, "price_e8": 2_415},
        ]
        # Credit line linked to acct #2 must NOT double-count; unlinked auto loan ($1,000) DOES count
        credit_lines = [
            {"id": 1, "account_id": 2, "balance_cents": 50_000, "active": 1},
            {"id": 2, "account_id": None, "balance_cents": 100_000, "active": 1},
        ]
        txs = [
            {"account_id": 1, "tx_type": "income", "amount_cents": 100_000, "tx_date": (ref - timedelta(days=5)).isoformat()},
            {"account_id": 1, "tx_type": "expense", "amount_cents": 20_000, "tx_date": (ref - timedelta(days=2)).isoformat()},
        ]

        series = reconstruct_net_worth_timeline(
            accounts=accounts,
            transactions=txs,
            holdings=holdings,
            credit_lines=credit_lines,
            days=730,
            today=ref,
        )
        assert len(series) == 730
        latest = series[-1]
        assert latest.date == ref.isoformat()
        assert latest.liquid_cents == 400_000
        assert latest.investment_cents == 24_150
        assert latest.debt_cents == 150_000  # 50k credit acct + 100k unlinked auto loan
        assert latest.net_worth_cents == 400_000 + 24_150 - 150_000

        # Every single day obeys liquid + investment - debt == net_worth
        for pt in series:
            assert pt.net_worth_cents == pt.liquid_cents + pt.investment_cents - pt.debt_cents

        # Before the $1,000 income & $200 expense (e.g. 10 days ago), liquid was 400k - 100k + 20k = 320k
        day_minus_10 = series[-11]
        assert day_minus_10.liquid_cents == 320_000

    def test_what_if_scenario_simulator_per_goal_series_and_overrides(self):
        ref = date(2026, 11, 20)
        bills = [
            {"id": 1, "name": "Rent", "normalized_merchant": "rent", "amount_cents": 100_000, "cadence": "monthly", "next_due_date": "2026-12-01", "active": 1},
        ]
        goals = [
            {"id": 10, "name": "GPU Upgrade", "target_cents": 100_000, "saved_cents": 20_000, "target_date": "2027-02-20", "priority": 1, "priority_order": 0, "status": "active", "deleted_at": None},
            {"id": 11, "name": "Deleted Goal", "target_cents": 50_000, "saved_cents": 0, "target_date": "2027-01-20", "priority": 2, "priority_order": 1, "status": "active", "deleted_at": "2026-11-15T00:00:00Z"},
        ]

        # 1. Baseline (no overrides): baseline_points == scenario_points, diff == 0
        sim_base = simulate_scenario(
            starting_balance_cents=300_000,
            recurring_bills=bills,
            goals=goals,
            payday_cadence_days=14,
            next_payday_date="2026-11-27",
            estimated_paycheck_cents=150_000,
            emergency_buffer_cents=75_000,
            horizon_days=180,
            today=ref,
        )
        assert sim_base.horizon_days == 180
        assert sim_base.safe_runway_floor_cents == 75_000
        assert len(sim_base.baseline_points) == 180
        assert len(sim_base.scenario_points) == 180
        assert sim_base.diff_ending_cents == 0
        assert sim_base.diff_pct == 0.0
        # Soft-deleted goal MUST be excluded; active goal's weekly series sums to exact remaining 80,000 cents!
        assert len(sim_base.per_goal_series) == 1
        g_series = sim_base.per_goal_series[0]
        assert g_series.goal_id == 10
        assert sum(p["contribution_cents"] for p in g_series.points) == 80_000
        assert g_series.points[-1]["cumulative_cents"] == 80_000
        assert g_series.points[-1]["remaining_cents"] == 0

        # 2. Scenario with goal date shift + monthly income boost + one-off expense
        sim_ov = simulate_scenario(
            starting_balance_cents=300_000,
            recurring_bills=bills,
            goals=goals,
            payday_cadence_days=14,
            next_payday_date="2026-11-27",
            estimated_paycheck_cents=150_000,
            emergency_buffer_cents=75_000,
            horizon_days=180,
            overrides=ScenarioOverride(
                goal_date_shifts={"10": "2027-04-20"},
                monthly_income_delta_cents=50_000,
                monthly_expense_delta_cents=0,
                one_off_events=[{"date": "2027-01-15", "amount_cents": -30_000, "label": "Car Tires"}],
            ),
            today=ref,
        )
        # 5 monthly income boosts inside 180d (+250k) - 30k one-off = +220k ending diff (goal still finishes before horizon!)
        assert sim_ov.per_goal_series[0].target_date == "2027-04-20"
        assert sum(p["contribution_cents"] for p in sim_ov.per_goal_series[0].points) == 80_000
        assert sim_ov.diff_ending_cents == 220_000
        assert sim_ov.diff_pct > 0


class TestFireflyRuleEngineAndSplits:
    def test_validate_splits_and_exact_pct_remainder_penny(self):
        # Remainder penny deterministically goes to last split
        s3 = compute_exact_pct_splits(
            1_000,
            [
                {"jar_id": 1, "pct": 33.33, "memo": "Part A"},
                {"jar_id": 2, "pct": 33.33, "memo": "Part B"},
                {"jar_id": 3, "pct": 33.34, "memo": "Part C"},
            ],
        )
        assert [s["amount_cents"] for s in s3] == [333, 333, 334]
        assert sum(s["amount_cents"] for s in s3) == 1_000

        s2 = compute_exact_pct_splits(101, [{"jar_id": 1, "pct": 50}, {"jar_id": 2, "pct": 50}])
        assert [s["amount_cents"] for s in s2] == [50, 51]

        # Strict split validation errors
        with pytest.raises(SplitValidationError):
            validate_splits(1_000, [])
        with pytest.raises(SplitValidationError):
            validate_splits(1_000, [{"jar_id": 1, "amount_cents": 600}, {"jar_id": 2, "amount_cents": 399}])
        with pytest.raises(SplitValidationError):
            validate_splits(1_000, [{"jar_id": 1, "amount_cents": 0}, {"jar_id": 2, "amount_cents": 1_000}])
        with pytest.raises(SplitValidationError):
            validate_splits(1_000, [{"jar_id": 1, "amount_cents": 500.0}, {"jar_id": 2, "amount_cents": 500}])

    def test_safe_regex_guard_rejects_redos_patterns(self):
        assert validate_safe_regex(r"^costco\s+#\d+$").search("COSTCO #1024")
        with pytest.raises(UnsafeRegexError):
            validate_safe_regex(r"(a+)+$")
        with pytest.raises(UnsafeRegexError):
            validate_safe_regex(r"(foo|foobar)+")
        with pytest.raises(UnsafeRegexError):
            validate_safe_regex("a" * 250)
        with pytest.raises(UnsafeRegexError):
            validate_safe_regex(r"(\w+)\1")

    def test_ordered_rule_groups_strict_and_or_negate_stop_processing_and_actions(self):
        groups = [
            {"id": 1, "name": "Merchants", "priority": 1, "active": True},
            {"id": 2, "name": "Fallbacks", "priority": 2, "active": True},
        ]
        # Rule 1 uses exact Pydantic RuleCreate schema (`TriggerSpec` + `ActionSpec`)
        rule1 = RuleCreate(
            group_id=1,
            name="Costco Auto-Split",
            priority=1,
            strict_and=True,
            stop_processing=True,
            triggers=[
                TriggerSpec(field="merchant", op="contains", value="costco"),
                TriggerSpec(field="amount_between", op="between", value=5_000, value_max=50_000),
                TriggerSpec(field="memo", op="contains", value="gas", **{"not": True}),
                TriggerSpec(field="tx_type", op="eq", value="expense"),
            ],
            actions=[
                ActionSpec(kind="set_jar", value=2),
                ActionSpec(kind="add_tag", value="bulk-shop"),
                ActionSpec(kind="append_memo", value="auto-split"),
                ActionSpec(kind="allocate_goal", value=7, params={"amount_cents": 1_000}),
                ActionSpec(
                    kind="split_pct",
                    params={
                        "splits": [
                            {"jar_id": 1, "pct": 60, "memo": "Household"},
                            {"jar_id": 2, "pct": 40, "memo": "Groceries"},
                        ]
                    },
                ),
            ],
        ).model_dump(by_alias=True)
        rule1["id"] = 101

        # Rule 2 in group 2 should NOT run when rule1 stops processing!
        rule2 = {
            "id": 102,
            "group_id": 2,
            "name": "Fallback Catch-All",
            "priority": 1,
            "strict_and": False,
            "stop_processing": False,
            "active": True,
            "triggers": [{"field": "missing_jar"}, {"field": "missing_tag", "value": "reviewed"}],
            "actions": [{"kind": "set_jar", "value": 3}, {"kind": "set_memo", "value": "fallback"}],
        }

        engine = RuleEngine()
        tx_costco = {
            "id": 50,
            "account_id": 1,
            "jar_id": None,
            "tx_type": "expense",
            "amount_cents": 20_001,
            "merchant": "COSTCO WHSE #123",
            "normalized_merchant": "costco whse",
            "memo": "Weekend run",
            "tx_date": "2026-11-18",
            "tags": [],
        }
        res = engine.evaluate_transaction(tx_costco, [rule2, rule1], rule_groups=groups)
        assert res.matched is True
        assert res.matched_rule_ids == [101]
        assert res.stopped_early is True
        assert res.changes["jar_id"] == 2
        assert res.changes["tags_to_add"] == ["bulk-shop"]
        assert res.changes["memo"] == "Weekend run | auto-split"
        assert res.changes["goal_allocations"] == [{"goal_id": 7, "amount_cents": 1_000}]
        # 60% of 20001 = floor(12000.6) = 12000; remainder goes to last split = 8001 (sum = 20001!)
        assert [s["amount_cents"] for s in res.changes["splits"]] == [12_000, 8_001]
        assert sum(s["amount_cents"] for s in res.changes["splits"]) == 20_001

        # Negated memo trigger ("gas") prevents rule1 from matching -> rule2 runs instead!
        tx_gas = dict(tx_costco, id=51, memo="Costco Gas Station")
        res_gas = engine.evaluate_transaction(tx_gas, [rule2, rule1], rule_groups=groups)
        assert res_gas.matched_rule_ids == [102]
        assert res_gas.changes["jar_id"] == 3
        assert res_gas.changes["memo"] == "fallback"

    def test_dry_run_rule_over_730_days_non_mutating(self):
        ref = date(2026, 11, 20)
        rule = {
            "id": 9,
            "name": "Tag Electric Bills",
            "active": False,  # Dry-run still tests draft/paused rules!
            "strict_and": True,
            "triggers": [
                {"field": "merchant", "op": "regex", "value": r"pacific\s+electric"},
                {"field": "amount_gt", "value": 5_000},
            ],
            "actions": [
                {"kind": "set_jar", "value": 1},
                {"kind": "link_bill", "value": 4},
                {"kind": "add_tag", "value": "utility"},
            ],
        }
        txs = [
            {"id": 1, "tx_type": "expense", "amount_cents": 12_500, "merchant": "Pacific Electric Co", "tx_date": (ref - timedelta(days=30)).isoformat(), "jar_id": None},
            {"id": 2, "tx_type": "expense", "amount_cents": 9_800, "merchant": "PACIFIC ELECTRIC", "tx_date": (ref - timedelta(days=650)).isoformat(), "jar_id": None},
            # Outside 730-day window -> ignored
            {"id": 3, "tx_type": "expense", "amount_cents": 11_000, "merchant": "Pacific Electric Co", "tx_date": (ref - timedelta(days=800)).isoformat(), "jar_id": None},
            # Below amount_gt threshold -> not matched
            {"id": 4, "tx_type": "expense", "amount_cents": 4_000, "merchant": "Pacific Electric Co", "tx_date": (ref - timedelta(days=10)).isoformat(), "jar_id": None},
        ]
        result = dry_run_rule(rule, txs, lookback_days=730, today=ref)
        assert result.rule_id == 9
        assert result.scanned_count == 3
        assert result.matched_count == 2
        assert len(result.samples) == 2
        assert result.samples[0].tx_id == 1
        assert any("link_bill -> #4" in ch for ch in result.samples[0].proposed_changes)
        # Ensure original transaction was NOT mutated
        assert txs[0]["jar_id"] is None


class TestCreditFactorAnalysisAnd524:
    def test_kind_dependent_drop_off_dates(self):
        assert compute_derogatory_drop_off("late_30", "2022-03-15") == "2029-03-15"
        assert compute_derogatory_drop_off("charge_off", "2021-06-01") == "2028-06-01"
        assert compute_derogatory_drop_off("collection", "2020-08-10") == "2027-08-10"
        assert compute_derogatory_drop_off("bankruptcy_ch13", "2023-01-20") == "2030-01-20"
        assert compute_derogatory_drop_off("bankruptcy_ch7", "2022-05-10") == "2032-05-10"

        counts_until, drops_off = compute_inquiry_dates("2026-02-15")
        assert counts_until == "2027-02-15"
        assert drops_off == "2028-02-15"

    def test_factor_analysis_524_au_toggle_and_goal_velocity_guard(self):
        ref = date(2026, 11, 20)
        # 5 cards opened across DIFFERENT issuers in trailing 24m (1 is Authorized User!) + 1 older anchor card
        raw_lines = [
            {"id": 1, "name": "Old Sapphire", "issuer": "Chase", "kind": "credit_card", "balance_cents": 20_000, "credit_limit_cents": 1_000_000, "apr_bps": 2149, "statement_closing_day": 15, "payment_due_day": 10, "opened_date": "2018-04-01", "has_grace_period": 1, "autopay_mode": "statement", "is_authorized_user": 0, "active": 1},
            {"id": 2, "name": "Amex Blue", "issuer": "Amex", "kind": "credit_card", "balance_cents": 180_000, "credit_limit_cents": 400_000, "apr_bps": 2699, "statement_closing_day": 10, "payment_due_day": 5, "opened_date": "2025-01-15", "has_grace_period": 0, "autopay_mode": "statement", "is_authorized_user": 0, "active": 1},
            {"id": 3, "name": "Citi Custom", "issuer": "Citi", "kind": "credit_card", "balance_cents": 0, "credit_limit_cents": 500_000, "apr_bps": 2299, "statement_closing_day": 12, "payment_due_day": 7, "opened_date": "2025-06-01", "has_grace_period": 1, "autopay_mode": "statement", "is_authorized_user": 0, "active": 1},
            {"id": 4, "name": "CapOne Venture", "issuer": "Capital One", "kind": "credit_card", "balance_cents": 0, "credit_limit_cents": 500_000, "apr_bps": 2399, "statement_closing_day": 18, "payment_due_day": 13, "opened_date": "2025-11-01", "has_grace_period": 1, "autopay_mode": "statement", "is_authorized_user": 0, "active": 1},
            {"id": 5, "name": "Discover Cash", "issuer": "Discover", "kind": "credit_card", "balance_cents": 0, "credit_limit_cents": 300_000, "apr_bps": 2199, "statement_closing_day": 22, "payment_due_day": 17, "opened_date": "2026-04-10", "has_grace_period": 1, "autopay_mode": "statement", "is_authorized_user": 0, "active": 1},
            # Authorized User card opened inside 24m!
            {"id": 6, "name": "Spouse Gold AU", "issuer": "Amex", "kind": "credit_card", "balance_cents": 0, "credit_limit_cents": 600_000, "apr_bps": 2099, "statement_closing_day": 25, "payment_due_day": 20, "opened_date": "2026-08-01", "has_grace_period": 1, "autopay_mode": "statement", "is_authorized_user": 1, "active": 1},
        ]
        reports = [
            {"id": 1, "bureau": "experian", "model": "fico8", "score": 742, "pulled_date": "2026-11-01", "source": "annualcreditreport"},
        ]
        inquiries = [
            {"id": 1, "lender": "Discover", "bureau": "experian", "inquiry_date": "2026-04-10"},
            {"id": 2, "lender": "Capital One", "bureau": "experian", "inquiry_date": "2026-07-15"},
            {"id": 3, "lender": "US Bank", "bureau": "transunion", "inquiry_date": "2026-09-20"},
            {"id": 4, "lender": "Old Lender", "bureau": "equifax", "inquiry_date": "2025-02-01"},  # >12m old, <24m
        ]
        derogs = [
            {"id": 1, "kind": "late_30", "lender": "Old Card", "opened_date": "2023-05-01", "active": True},
        ]

        opt = CreditDebtOptimizer()
        rep_with_au = opt.analyze(
            raw_lines=raw_lines,
            smoothed_monthly_income_cents=600_000,
            credit_reports=reports,
            credit_inquiries=inquiries,
            credit_derogatories=derogs,
            include_authorized_user=True,
            today=ref,
        )

        # 5/24 WITH Authorized User = 5/24 (over 5/24!)
        ftf = rep_with_au.five_twenty_four
        assert ftf is not None
        assert ftf.count_5_24 == 5
        assert ftf.under_5_24 is False
        assert ftf.include_authorized_user is True
        assert ftf.next_slot_drop_date == "2027-01-15"  # Amex Blue opened 2025-01-15
        assert ftf.inquiries_6m == 2  # Jul 15 + Sep 20
        assert ftf.inquiries_12m == 3  # Apr 10 + Jul 15 + Sep 20 (Feb 2025 aged >12m!)

        # Toggle AU OFF -> drops to 4/24 (under 5/24!)
        ftf_no_au = evaluate_five_twenty_four(
            credit_lines=rep_with_au.credit_lines,
            inquiries=rep_with_au.credit_inquiries,
            include_authorized_user=False,
            today=ref,
        )
        assert ftf_no_au.count_5_24 == 4
        assert ftf_no_au.under_5_24 is True

        # Deterministic 5-Factor Analysis checks (zero fabricated point deltas)
        fa = rep_with_au.factor_analysis
        assert fa is not None
        assert not hasattr(rep_with_au, "factor_simulator")
        assert not hasattr(fa, "simulated_actions")
        assert fa.disclaimer == FACTOR_ANALYSIS_DISCLAIMER
        assert len(fa.latest_actual_scores) == 1 and fa.latest_actual_scores[0].score == 742
        assert sum(f.weight_pct for f in fa.fico8_factors) == 100
        assert [f.weight_pct for f in fa.fico8_factors] == [35, 30, 15, 10, 10]
        assert sum(f.weight_pct for f in fa.vantage3_factors) == 100
        assert [f.weight_pct for f in fa.vantage3_factors] == [40, 21, 20, 11, 5, 3]
        for f in list(fa.fico8_factors) + list(fa.vantage3_factors):
            assert "pts" not in f.summary.lower()
            assert "pts" not in f.guidance.lower()
            assert "est." not in f.guidance.lower()

        # Derogatory drop-off auto-computed (+7y for late_30)
        assert rep_with_au.credit_derogatories[0].drops_off == "2030-05-01"

        # GoalOptimizerEngine MUST warn against new-card promo strategies when >=4/24!
        budget = AutoBudgetEngine().compute(
            accounts=[{"id": 1, "account_type": "checking", "balance_cents": 400_000, "is_liquid": 1}],
            jars=_sample_jars(),
            transactions=[],
            recurring_bills=[],
            transfers=[],
            settings={"emergency_buffer_cents": 50_000, "payday_cadence_days": 14, "expected_paycheck_cents": 250_000},
            today=ref,
        )
        plan = GoalOptimizerEngine().optimize(
            req=GoalPlanRequest(item_name="OLED Display", target_cents=90_000),
            budget=budget,
            credit_report=rep_with_au,
            impulses=[],
            hourly_wage_cents=4_000,
            today=ref,
        )
        credit_opt = next(o for o in plan.options if o.path_id == "credit_optimized_buy")
        assert "5/24" in credit_opt.credit_impact_note
        assert any("5/24" in st for st in credit_opt.steps)


class Test730DayBackportVariableBillsAndSplitAwareBudgetQuant:
    def test_730d_annual_cycles_and_variable_utility_min_max_matching(self):
        ref = date(2026, 11, 20)
        txs = [
            # 2 full annual cycles inside 730 days!
            {"tx_type": "expense", "amount_cents": 13_900, "merchant": "Amazon Prime Annual", "normalized_merchant": "amazon prime annual", "tx_date": "2024-11-25", "jar_id": 1},
            {"tx_type": "expense", "amount_cents": 13_900, "merchant": "Amazon Prime Annual", "normalized_merchant": "amazon prime annual", "tx_date": "2025-11-25", "jar_id": 1},
            {"tx_type": "expense", "amount_cents": 13_900, "merchant": "Amazon Prime Annual", "normalized_merchant": "amazon prime annual", "tx_date": "2026-11-18", "jar_id": 1},
            # Variable seasonal electric utility ($82 -> $158 -> $110 -> $94)
            {"tx_type": "expense", "amount_cents": 8_200, "merchant": "City Electric Utility", "normalized_merchant": "city electric utility", "tx_date": "2026-07-10", "jar_id": 1},
            {"tx_type": "expense", "amount_cents": 15_800, "merchant": "City Electric Utility", "normalized_merchant": "city electric utility", "tx_date": "2026-08-10", "jar_id": 1},
            {"tx_type": "expense", "amount_cents": 11_000, "merchant": "City Electric Utility", "normalized_merchant": "city electric utility", "tx_date": "2026-09-11", "jar_id": 1},
            {"tx_type": "expense", "amount_cents": 9_400, "merchant": "City Electric Utility", "normalized_merchant": "city electric utility", "tx_date": "2026-10-10", "jar_id": 1},
            {"tx_type": "expense", "amount_cents": 8_800, "merchant": "City Electric Utility", "normalized_merchant": "city electric utility", "tx_date": "2026-11-10", "jar_id": 1},
        ]

        bills = detect_recurring_bills(txs, today=ref, lookback_days=730)
        by_name = {b.name: b for b in bills}
        assert "Amazon Prime Annual" in by_name
        assert by_name["Amazon Prime Annual"].cadence.value == "annual"
        assert "City Electric Utility" in by_name
        elec = by_name["City Electric Utility"]
        assert elec.cadence.value == "monthly"
        assert elec.amount_min_cents == 8_200
        assert elec.amount_max_cents == 15_800
        assert elec.due_window_days >= 4
        # Rolling mean of [8200, 15800, 11000, 9400, 8800] = 10,640
        assert elec.amount_cents == 10_640

        # Variable bill matcher checks [amount_min_cents, amount_max_cents] AND due_window_days
        bill_dict = {
            "id": 7,
            "name": "City Electric Utility",
            "normalized_merchant": "city electric utility",
            "amount_cents": 0,
            "amount_min_cents": 8_000,
            "amount_max_cents": 16_000,
            "due_window_days": 4,
            "cadence": "monthly",
            "next_due_date": "2026-11-10",
            "active": 1,
        }
        assert forecast_bill_amount_cents(bill_dict) == 12_000  # exact midpoint
        assert forecast_bill_amount_cents(bill_dict, recent_matched_amounts=[9_000, 11_000, 13_000]) == 11_000

        # Inside range + inside +/- 4d window -> MATCH
        tx_ok = {"tx_type": "expense", "amount_cents": 14_250, "normalized_merchant": "city electric utility", "tx_date": "2026-11-12"}
        assert match_transaction_to_bill(tx_ok, bill_dict) is True
        assert find_matching_bill(tx_ok, [bill_dict]) == bill_dict

        # Outside range ($210 > $160) -> NO MATCH
        tx_too_high = dict(tx_ok, amount_cents=21_000)
        assert match_transaction_to_bill(tx_too_high, bill_dict) is False

        # Outside due window (Nov 22 is 12d after Nov 10 > 4d) -> NO MATCH
        tx_late = dict(tx_ok, tx_date="2026-11-22")
        assert match_transaction_to_bill(tx_late, bill_dict) is False

        # Paid within range & due window -> detect_bill_anomalies MUST NOT false-flag missed_bill!
        anomalies = detect_bill_anomalies([tx_ok], [bill_dict], today=date(2026, 11, 16))
        assert not any(a.kind == "missed_bill" for a in anomalies)

    def test_autobudget_and_quant_math_are_split_aware_with_730d_seasonal_baseline(self):
        ref = date(2026, 11, 20)
        jars = _sample_jars()
        accounts = [{"id": 1, "account_type": "checking", "balance_cents": 400_000, "is_liquid": 1}]
        settings = {"emergency_buffer_cents": 50_000, "payday_cadence_days": 14, "next_payday_date": "2026-11-27", "expected_paycheck_cents": 220_000}

        # Parent tx ($200 on Daily jar) split into $150 Bills + $50 Fun -> Daily MUST show $0, Bills $150, Fun $50!
        split_tx = {
            "id": 99,
            "tx_type": "expense",
            "amount_cents": 20_000,
            "jar_id": 2,
            "merchant": "Target",
            "normalized_merchant": "target",
            "tx_date": "2026-11-10",
            "splits": [
                {"jar_id": 1, "amount_cents": 15_000, "memo": "Household Bills"},
                {"jar_id": 3, "amount_cents": 5_000, "memo": "Board game"},
            ],
        }
        budget = AutoBudgetEngine().compute(
            accounts=accounts,
            jars=jars,
            transactions=[split_tx],
            recurring_bills=[],
            transfers=[],
            settings=settings,
            today=ref,
        )
        spent_by_slug = {j.slug: j.spent_cents for j in budget.jars}
        assert budget.monthly_spent_cents == 20_000  # ZERO double counting!
        assert spent_by_slug["bills"] == 15_000
        assert spent_by_slug["daily"] == 0
        assert spent_by_slug["fun"] == 5_000

        # QuantMathEngine split-aware volatility: Bills split ($150) excluded, ONLY Fun split ($50) counts!
        qengine = QuantMathEngine()
        vol_split = qengine.compute_spending_volatility(
            transactions=[split_tx],
            safe_to_spend_cents=100_000,
            days_until_payday=10,
            jars=jars,
            today=ref,
        )
        assert vol_split.weekly_mean_cents == 2_500  # 11 sample days -> 2 weeks -> (5000 + 0)/2 = 2500 (NOT 10000!)

        # 730-day seasonal baseline: steady $100/wk recent 90d, PLUS prior-year Nov holiday spike ($350/wk)!
        recent_90d = [
            {"id": i, "tx_type": "expense", "jar_id": 3, "amount_cents": 10_000, "normalized_merchant": "shop", "tx_date": (ref - timedelta(days=w * 7 + 1)).isoformat()}
            for i, w in enumerate(range(12), start=1)
        ]
        # Steady off-season prior year ($100/wk) + big holiday spike around ref - 365d ($350/wk)
        prior_year = [
            {
                "id": 100 + w,
                "tx_type": "expense",
                "jar_id": 3,
                "amount_cents": 35_000 if 49 <= w <= 55 else 10_000,
                "normalized_merchant": "shop",
                "tx_date": (ref - timedelta(days=w * 7 + 1)).isoformat(),
            }
            for w in range(13, 80)
        ]
        vol_90_only = qengine.compute_spending_volatility(
            transactions=recent_90d,
            safe_to_spend_cents=140_000,
            days_until_payday=14,
            jars=jars,
            monthly_essential_bills_cents=100_000,
            today=ref,
        )
        vol_with_730d = qengine.compute_spending_volatility(
            transactions=recent_90d + prior_year,
            safe_to_spend_cents=140_000,
            days_until_payday=14,
            jars=jars,
            monthly_essential_bills_cents=100_000,
            today=ref,
        )
        # Recent 90d volatility remains razor-sharp & identical!
        assert vol_with_730d.weekly_mean_cents == vol_90_only.weekly_mean_cents == 10_000
        assert vol_with_730d.weekly_std_cents == vol_90_only.weekly_std_cents == 0
        # 730d seasonal spike safely uplifts recommended_buffer_cents!
        seasonal = qengine.compute_seasonal_baseline(transactions=recent_90d + prior_year, jars=jars, today=ref)
        assert seasonal.has_seasonal_data is True
        assert seasonal.seasonal_multiplier > 1.2
        assert seasonal.seasonal_buffer_uplift_cents > 0
        assert vol_with_730d.recommended_buffer_cents > vol_90_only.recommended_buffer_cents

    def test_empty_and_deficit_analytics_plus_pydantic_rules_and_high_inquiry_velocity(self):
        ref = date(2026, 11, 20)
        jars = _sample_jars()

        # 1. Empty workspace analytics does not divide by zero
        empty_ov = AnalyticsEngine().build_overview(transactions=[], jars=jars, period="2y", today=ref)
        assert empty_ov.total_income_cents == 0
        assert empty_ov.total_expense_cents == 0
        assert empty_ov.savings_rate_pct == 0.0
        assert empty_ov.sankey_links == []
        assert len(empty_ov.monthly_trends) == 24

        # 2. Deficit month without reserve transfer labels gap honestly as "Unfunded Gap" (Finding #18)
        deficit_txs = [
            {"id": 1, "tx_type": "income", "amount_cents": 100_000, "merchant": "Gig", "tx_date": "2026-11-05"},
            {"id": 2, "tx_type": "expense", "amount_cents": 140_000, "jar_id": 1, "merchant": "Rent", "tx_date": "2026-11-01"},
        ]
        def_ov = AnalyticsEngine().build_overview(transactions=deficit_txs, jars=jars, period="30d", today=ref)
        assert def_ov.net_surplus_cents == -40_000
        in_sum = sum(lk.amount_cents for lk in def_ov.sankey_links if lk.target == "hub:total_income")
        out_sum = sum(lk.amount_cents for lk in def_ov.sankey_links if lk.source == "hub:total_income")
        assert in_sum == out_sum == 140_000
        assert any(n.label == "Unfunded Gap" and n.amount_cents == 40_000 for n in def_ov.sankey_nodes)
        assert any(n.id == "hub:total_income" and n.label == "Total Outflow" for n in def_ov.sankey_nodes)

        # 3. RuleOut Pydantic model + JSON-string SQLite rule row (starts_with/amount_exact/account_id)
        rule_out = RuleOut(
            id=201,
            group_id=None,
            name="Exact Subscription Rule",
            priority=5,
            strict_and=True,
            stop_processing=False,
            triggers=[
                TriggerSpec(field="merchant", op="starts_with", value="spotify"),
                TriggerSpec(field="amount_exact", op="eq", value=1_199),
                TriggerSpec(field="account_id", op="eq", value=2),
            ],
            actions=[ActionSpec(kind="set_jar", value=3), ActionSpec(kind="add_tag", value="music")],
            active=True,
        )
        tx_spot = {"id": 88, "account_id": 2, "jar_id": None, "tx_type": "expense", "amount_cents": 1_199, "merchant": "Spotify USA", "tx_date": "2026-11-18"}
        dry = dry_run_rule(rule_out, [tx_spot], today=ref)
        assert dry.matched_count == 1
        assert dry.samples[0].tx_id == 88

        sqlite_rule = {
            "id": 202,
            "name": "JSON Triggers Rule",
            "priority": 1,
            "strict_and": 1,
            "stop_processing": 0,
            "active": 1,
            "triggers_json": '[{"field":"merchant","op":"ends_with","value":"usa"},{"field":"amount_lt","value":2000}]',
            "actions_json": '[{"kind":"set_memo","value":"stream-ok"}]',
        }
        res_sql = RuleEngine().evaluate_transaction(tx_spot, [sqlite_rule])
        assert res_sql.matched_rule_ids == [202]
        assert res_sql.changes["memo"] == "stream-ok"

        # 4. Inquiry-only velocity (>=3 in 6m, even at 1/24) + Ch7 10-year drop-off + AutoBudget bill min/max
        rep_inq = CreditDebtOptimizer().analyze(
            raw_lines=[
                {"id": 1, "name": "Main Card", "issuer": "Chase", "kind": "credit_card", "balance_cents": 10_000, "credit_limit_cents": 500_000, "apr_bps": 2099, "statement_closing_day": 15, "payment_due_day": 10, "opened_date": "2020-01-01", "has_grace_period": 1, "autopay_mode": "statement", "active": 1},
            ],
            smoothed_monthly_income_cents=500_000,
            credit_inquiries=[
                {"id": 1, "lender": "A", "bureau": "experian", "inquiry_date": "2026-07-01"},
                {"id": 2, "lender": "B", "bureau": "experian", "inquiry_date": "2026-08-15"},
                {"id": 3, "lender": "C", "bureau": "transunion", "inquiry_date": "2026-10-05"},
            ],
            credit_derogatories=[
                {"id": 1, "kind": "bankruptcy_ch7", "lender": "Court", "opened_date": "2021-03-01", "active": True},
            ],
            today=ref,
        )
        assert rep_inq.five_twenty_four is not None
        assert rep_inq.five_twenty_four.count_5_24 == 0
        assert rep_inq.five_twenty_four.inquiries_6m == 3
        assert rep_inq.credit_derogatories[0].drops_off == "2031-03-01"
        assert any(a.category == "inquiry_velocity" for a in rep_inq.alerts)

        budget_bills = AutoBudgetEngine().compute(
            accounts=[{"id": 1, "account_type": "checking", "balance_cents": 400_000, "is_liquid": 1}],
            jars=jars,
            transactions=[],
            recurring_bills=[
                {"id": 5, "name": "Gas Utility", "normalized_merchant": "gas utility", "amount_cents": 0, "amount_min_cents": 6_000, "amount_max_cents": 14_000, "due_window_days": 5, "cadence": "monthly", "next_due_date": "2026-11-25", "jar_id": 1, "is_essential": 1, "active": 1},
            ],
            transfers=[],
            settings={"emergency_buffer_cents": 50_000, "payday_cadence_days": 14, "next_payday_date": "2026-11-27", "expected_paycheck_cents": 220_000},
            today=ref,
        )
        assert budget_bills.upcoming_bills[0].amount_cents == 10_000  # exact midpoint
        assert budget_bills.upcoming_bills[0].amount_min_cents == 6_000
        assert budget_bills.upcoming_bills[0].amount_max_cents == 14_000
        assert budget_bills.upcoming_bills[0].due_window_days == 5

        plan_inq = GoalOptimizerEngine().optimize(
            req=GoalPlanRequest(item_name="Monitor", target_cents=60_000),
            budget=budget_bills,
            credit_report=rep_inq,
            impulses=[],
            hourly_wage_cents=4_000,
            today=ref,
        )
        cred_opt = next(o for o in plan_inq.options if o.path_id == "credit_optimized_buy")
        assert "inquiry velocity" in cred_opt.credit_impact_note.lower()
