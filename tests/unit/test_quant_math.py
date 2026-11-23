"""Comprehensive unit tests for ChronoX Finance Quantitative Math Engine."""

from __future__ import annotations

import re
from datetime import date, timedelta

import pytest

from chronox_finance.engines.autobudget import AutoBudgetEngine
from chronox_finance.engines.credit_debt import CreditDebtOptimizer
from chronox_finance.engines.quant_math import (
    CashflowRunwayResult,
    DailyCashflowPoint,
    OptimalAllocationStep,
    QuantForecastReport,
    QuantMathEngine,
    SpendingVolatilityMetrics,
    to_dict,
)
from chronox_finance.models import (
    GoalCategory,
    GoalOut,
    IntelligenceMode,
    RecurrenceCadence,
    SuggestionActionType,
)
from chronox_finance.service import FinanceService

_RSD_FORBIDDEN_RE = re.compile(
    r"\b(you\s+failed|irresponsible|reckless|terrible\s+(?:with|at)\s+money|"
    r"pathetic|lazy|stupid|shameful|wasted\s+money)\b",
    re.IGNORECASE,
)


def _assert_rsd_safe(text: str) -> None:
    assert not _RSD_FORBIDDEN_RE.search(text), f"Found RSD-hostile phrasing in: {text!r}"


def _make_goal(
    gid: int,
    name: str,
    target_cents: int,
    saved_cents: int = 0,
    priority: int = 2,
    target_date: str | None = None,
    status: str = "active",
) -> GoalOut:
    rem = max(0, target_cents - saved_cents)
    pct = round(min(100.0, (saved_cents / max(1, target_cents)) * 100.0), 1)
    return GoalOut(
        id=gid,
        name=name,
        target_cents=target_cents,
        saved_cents=saved_cents,
        remaining_cents=rem,
        pct_funded=pct,
        target_date=target_date,
        priority=priority,
        category=GoalCategory.GADGET,
        funding_jar_slug="fun",
        status=status,
        notes="",
        created_at="2026-11-01T00:00:00Z",
        updated_at="2026-11-01T00:00:00Z",
    )


class TestCashflowRunwaySimulator:
    def test_all_eight_cadences_exact_cent_accounting_over_90_days(self):
        ref = date(2026, 11, 1)
        engine = QuantMathEngine()
        bills = [
            {"id": 1, "name": "Weekly Meal Box", "normalized_merchant": "mealbox", "amount_cents": 5_000, "cadence": RecurrenceCadence.WEEKLY.value, "next_due_date": "2026-11-02", "active": 1},
            {"id": 2, "name": "Biweekly Lawn", "normalized_merchant": "lawn", "amount_cents": 4_000, "cadence": RecurrenceCadence.BIWEEKLY.value, "next_due_date": "2026-11-03", "active": 1},
            {"id": 3, "name": "SemiMonthly Childcare", "normalized_merchant": "childcare", "amount_cents": 20_000, "cadence": RecurrenceCadence.SEMIMONTHLY.value, "next_due_date": "2026-11-01", "active": 1},
            {"id": 4, "name": "Monthly Rent", "normalized_merchant": "rent", "amount_cents": 100_000, "cadence": RecurrenceCadence.MONTHLY.value, "next_due_date": "2026-11-05", "active": 1},
            {"id": 5, "name": "Bimonthly Water", "normalized_merchant": "water", "amount_cents": 9_000, "cadence": RecurrenceCadence.BIMONTHLY.value, "next_due_date": "2026-11-10", "active": 1},
            {"id": 6, "name": "Quarterly Trash", "normalized_merchant": "trash", "amount_cents": 12_000, "cadence": RecurrenceCadence.QUARTERLY.value, "next_due_date": "2026-11-15", "active": 1},
            {"id": 7, "name": "Semiannual Auto", "normalized_merchant": "autoins", "amount_cents": 45_000, "cadence": RecurrenceCadence.SEMIANNUAL.value, "next_due_date": "2026-11-20", "active": 1},
            {"id": 8, "name": "Annual Prime", "normalized_merchant": "prime", "amount_cents": 13_900, "cadence": RecurrenceCadence.ANNUAL.value, "next_due_date": "2026-12-01", "active": 1},
        ]

        res = engine.simulate_cashflow_runway(
            starting_balance_cents=500_000,
            recurring_bills=bills,
            payday_cadence_days=14,
            next_payday_date="2026-11-06",
            estimated_paycheck_cents=200_000,
            emergency_buffer_cents=50_000,
            days=90,
            today=ref,
        )

        assert isinstance(res, CashflowRunwayResult)
        assert res.horizon_days == 90
        assert len(res.daily_points) == 90
        assert all(isinstance(p, DailyCashflowPoint) for p in res.daily_points)

        # Exact cent conservation across all 90 simulated days
        sum_in = sum(p.inflow_cents for p in res.daily_points)
        sum_out = sum(p.outflow_cents for p in res.daily_points)
        assert sum_in == res.total_inflows_cents
        assert sum_out == res.total_outflows_cents
        assert res.ending_balance_cents == res.starting_balance_cents + sum_in - sum_out

        # Count occurrences of every cadence inside [2026-11-01, 2027-01-29] (90 days)
        all_events = [ev for p in res.daily_points for ev in p.events]
        assert sum("Weekly Meal Box" in e for e in all_events) == 13
        assert sum("Biweekly Lawn" in e for e in all_events) == 7
        assert sum("SemiMonthly Childcare" in e for e in all_events) == 6  # Nov 1/15, Dec 1/15, Jan 1/15
        assert sum("Monthly Rent" in e for e in all_events) == 3  # Nov 5, Dec 5, Jan 5
        assert sum("Bimonthly Water" in e for e in all_events) == 2  # Nov 10, Jan 10
        assert sum("Quarterly Trash" in e for e in all_events) == 1  # Nov 15
        assert sum("Semiannual Auto" in e for e in all_events) == 1  # Nov 20
        assert sum("Annual Prime" in e for e in all_events) == 1  # Dec 1

        # Verify to_dict() compatibility
        as_map = to_dict(res)
        assert as_map["horizon_days"] == 90
        assert len(as_map["daily_points"]) == 90

    def test_negative_runway_overdraft_risk_and_payday_crunch_warning(self):
        ref = date(2026, 11, 10)
        engine = QuantMathEngine()
        bills = [
            {
                "id": 1,
                "name": "Rent",
                "normalized_merchant": "rent",
                "amount_cents": 120_000,
                "cadence": "monthly",
                "next_due_date": "2026-11-15",
                "active": 1,
            }
        ]
        res = engine.simulate_cashflow_runway(
            starting_balance_cents=50_000,
            recurring_bills=bills,
            payday_cadence_days=14,
            next_payday_date="2026-11-20",
            estimated_paycheck_cents=180_000,
            emergency_buffer_cents=30_000,
            days=30,
            today=ref,
        )

        assert res.overdraft_risk is True
        assert res.lowest_balance_cents == -70_000  # $500 - $1,200 = -$700.00
        assert res.lowest_balance_date == "2026-11-15"
        assert res.safe_runway_days == 5  # Days 0..4 (Nov 10..14) are >= 0; Day 5 (Nov 15) goes negative
        assert res.payday_crunch_warning is not None
        assert "-$700.00" in res.payday_crunch_warning
        assert "2026-11-15" in res.payday_crunch_warning
        assert "2026-11-20" in res.payday_crunch_warning
        _assert_rsd_safe(res.payday_crunch_warning)
        # Recovers positive after Nov 20 paycheck (+180k) and Dec 4 paycheck (+180k)
        assert res.ending_balance_cents > 0

    def test_credit_minimums_and_azeo_preclose_deduplication_and_buffer_guard(self):
        ref = date(2026, 11, 1)
        credit_opt = CreditDebtOptimizer()
        report = credit_opt.analyze(
            raw_lines=[
                {
                    "id": 1,
                    "name": "Primary Chase",
                    "kind": "credit_card",
                    "balance_cents": 10_000,
                    "credit_limit_cents": 500_000,
                    "apr_bps": 2000,
                    "min_payment_cents": 3_500,
                    "statement_closing_day": 20,
                    "payment_due_day": 15,
                    "has_grace_period": True,
                    "autopay_mode": "statement",
                },
                {
                    "id": 2,
                    "name": "ZeroOut Amex",
                    "kind": "credit_card",
                    "balance_cents": 60_000,
                    "credit_limit_cents": 200_000,
                    "apr_bps": 2699,
                    "min_payment_cents": 4_000,
                    "statement_closing_day": 12,
                    "payment_due_day": 18,
                    "has_grace_period": True,
                    "autopay_mode": "minimum",
                },
            ],
            smoothed_monthly_income_cents=400_000,
            today=ref,
        )

        engine = QuantMathEngine()
        # Case 1: Ample cash ($2,000, buffer $500) -> AZEO pays $600 on Nov 9 AND offsets Nov 18 $40 min!
        res_ample = engine.simulate_cashflow_runway(
            starting_balance_cents=200_000,
            recurring_bills=[],
            credit_lines=report.credit_lines,
            azeo_plan=report.azeo_plan,
            estimated_paycheck_cents=0,
            emergency_buffer_cents=50_000,
            days=30,
            today=ref,
        )
        amex_azeo_events = [
            (p.date, p.outflow_cents, ev)
            for p in res_ample.daily_points
            for ev in p.events
            if "AZEO pre-close: ZeroOut Amex" in ev
        ]
        amex_min_events = [
            ev for p in res_ample.daily_points for ev in p.events if "Credit min: ZeroOut Amex" in ev
        ]
        assert len(amex_azeo_events) == 1
        assert amex_azeo_events[0][0] == "2026-11-09"  # 3 days before closing day 12
        assert amex_min_events == []  # Never double-counted in the same cycle!
        # Total outflow = $600 AZEO (Amex) + $35 min (Primary Chase) = $635
        assert res_ample.total_outflows_cents == 63_500
        assert res_ample.ending_balance_cents == 200_000 - 63_500

        # Case 2: Tight cash ($800, buffer $500) -> AZEO ($600) would breach buffer, so it defers gracefully!
        res_tight = engine.simulate_cashflow_runway(
            starting_balance_cents=80_000,
            recurring_bills=[],
            credit_lines=report.credit_lines,
            azeo_plan=report.azeo_plan,
            estimated_paycheck_cents=0,
            emergency_buffer_cents=50_000,
            days=30,
            today=ref,
        )
        assert res_tight.overdraft_risk is False  # Optional AZEO must not cause false overdraft!
        advisory_events = [
            ev for p in res_tight.daily_points for ev in p.events if "AZEO advisory (ZeroOut Amex)" in ev
        ]
        assert len(advisory_events) == 1
        # Mandatory minimums still run: $35 (Chase) + $40 (Amex) = $75
        assert res_tight.total_outflows_cents == 7_500

    def test_horizon_clamping_and_recently_paid_bill_not_double_counted(self):
        ref = date(2026, 11, 15)
        engine = QuantMathEngine()
        bills = [
            {
                "id": 1,
                "name": "Netflix",
                "normalized_merchant": "netflix",
                "amount_cents": 2_299,
                "cadence": "monthly",
                "next_due_date": "2026-11-15",
                "active": 1,
            }
        ]
        txs = [
            {
                "tx_type": "expense",
                "amount_cents": 2_299,
                "normalized_merchant": "netflix",
                "tx_date": "2026-11-14",
            }
        ]
        res = engine.simulate_cashflow_runway(
            starting_balance_cents=100_000,
            recurring_bills=bills,
            transactions=txs,
            days=250,  # Clamped to 90
            today=ref,
        )
        assert res.horizon_days == 90
        # Nov 15 occurrence skipped because paid Nov 14; Dec 15 and Jan 15 count -> 2 occurrences
        assert res.total_outflows_cents == 2_299 * 2


class TestSpendingVolatilityAndBufferSizing:
    def test_empty_history_returns_calm_safe_defaults(self):
        engine = QuantMathEngine()
        vol = engine.compute_spending_volatility(
            transactions=[],
            safe_to_spend_cents=140_000,
            days_until_payday=14,
            current_emergency_buffer_cents=60_000,
            monthly_essential_bills_cents=150_000,
            today=date(2026, 11, 20),
        )
        assert isinstance(vol, SpendingVolatilityMetrics)
        assert vol.sample_days == 0
        assert vol.daily_mean_cents == 0
        assert vol.weekly_mean_cents == 0
        assert vol.coefficient_of_variation == 0.0
        assert vol.p50_weekly_spend_cents == 0
        assert vol.p90_weekly_spend_cents == 0
        assert vol.recommended_buffer_cents == 75_000  # max($500, $600 cur, $1500/2 = $750)
        assert vol.p50_daily_safe_cents == 10_000
        assert vol.p90_conservative_daily_safe_cents == 8_500
        assert vol.stability_score_0_100 == 75

    def test_calm_vs_high_volatility_spending_bands_and_buffer(self):
        ref = date(2026, 11, 20)
        engine = QuantMathEngine()
        jars = [
            {"id": 1, "kind": "bills"},
            {"id": 2, "kind": "daily"},
            {"id": 3, "kind": "fun"},
        ]

        # Calm spender: exact $120/week for 12 weeks (+ excluded $1,500 monthly rent in bills jar!)
        calm_txs = [
            {"tx_type": "expense", "jar_id": 2, "amount_cents": 12_000, "normalized_merchant": "groceries", "tx_date": (ref - timedelta(days=w * 7 + 1)).isoformat()}
            for w in range(12)
        ] + [
            {"tx_type": "expense", "jar_id": 1, "amount_cents": 150_000, "normalized_merchant": "rent", "tx_date": (ref - timedelta(days=m * 30 + 2)).isoformat()}
            for m in range(3)
        ]

        # Spiky spender: same ~$120/week mean, wild swings ($0 to $520 weeks)
        spiky_amounts = [0, 48_000, 1_500, 0, 52_000, 2_000, 0, 38_000, 1_000, 0, 500, 1_000]
        spiky_txs = [
            {"tx_type": "expense", "jar_id": 3, "amount_cents": amt, "normalized_merchant": "shop", "tx_date": (ref - timedelta(days=w * 7 + 1)).isoformat()}
            for w, amt in enumerate(spiky_amounts)
            if amt > 0
        ]

        vol_calm = engine.compute_spending_volatility(
            transactions=calm_txs,
            safe_to_spend_cents=140_000,
            days_until_payday=14,
            jars=jars,
            monthly_essential_bills_cents=150_000,
            today=ref,
        )
        vol_spiky = engine.compute_spending_volatility(
            transactions=spiky_txs,
            safe_to_spend_cents=140_000,
            days_until_payday=14,
            jars=jars,
            monthly_essential_bills_cents=150_000,
            today=ref,
        )

        # Calm checks: Bills rent excluded -> exact $120/wk mean, 0 std, 100 stability, tight band
        assert vol_calm.weekly_mean_cents == 12_000
        assert vol_calm.weekly_std_cents == 0
        assert vol_calm.coefficient_of_variation == 0.0
        assert vol_calm.p50_weekly_spend_cents == 12_000
        assert vol_calm.p75_weekly_spend_cents == 12_000
        assert vol_calm.p90_weekly_spend_cents == 12_000
        assert vol_calm.stability_score_0_100 == 100
        assert vol_calm.p90_conservative_daily_safe_cents == vol_calm.p50_daily_safe_cents == 10_000

        # Volatile checks: monotonicity + larger buffer + lower stability + conservative P90 haircut
        assert vol_spiky.p50_weekly_spend_cents <= vol_spiky.p75_weekly_spend_cents <= vol_spiky.p90_weekly_spend_cents
        assert vol_spiky.coefficient_of_variation > 0.8
        assert vol_spiky.stability_score_0_100 < vol_calm.stability_score_0_100
        assert vol_spiky.recommended_buffer_cents > vol_calm.recommended_buffer_cents
        assert vol_spiky.p90_conservative_daily_safe_cents < vol_spiky.p50_daily_safe_cents
        assert vol_spiky.p90_conservative_daily_safe_cents >= 0


class TestOptimalMarginalROISurplusAllocator:
    def _build_complex_credit_report(self, ref: date):
        opt = CreditDebtOptimizer()
        return opt.analyze(
            raw_lines=[
                # Card 1: Autopay OFF + 26.99% high APR
                {
                    "id": 1,
                    "name": "Amex High APR",
                    "kind": "credit_card",
                    "balance_cents": 150_000,
                    "credit_limit_cents": 400_000,
                    "apr_bps": 2699,
                    "min_payment_cents": 5_000,
                    "statement_closing_day": 10,
                    "payment_due_day": 5,
                    "autopay_mode": "none",
                },
                # Card 2: Expiring 0% Promo APR cliff in 60 days ($600 -> needs $300/mo to beat 24.99% jump!)
                {
                    "id": 2,
                    "name": "Discover 0% Promo",
                    "kind": "credit_card",
                    "balance_cents": 60_000,
                    "credit_limit_cents": 150_000,  # 40% util -> also triggers >28.9% AZEO cliff!
                    "apr_bps": 2499,
                    "promo_apr_bps": 0,
                    "promo_ends_date": (ref + timedelta(days=60)).isoformat(),
                    "min_payment_cents": 2_500,
                    "statement_closing_day": 22,
                    "payment_due_day": 18,
                    "autopay_mode": "minimum",
                },
                # Card 3: Moderate High APR (18.99%)
                {
                    "id": 3,
                    "name": "Citi Custom",
                    "kind": "credit_card",
                    "balance_cents": 40_000,
                    "credit_limit_cents": 500_000,
                    "apr_bps": 1899,
                    "min_payment_cents": 2_500,
                    "statement_closing_day": 25,
                    "payment_due_day": 20,
                    "autopay_mode": "statement",
                },
                # Loan 4: Low APR Student Loan (5.50%)
                {
                    "id": 4,
                    "name": "Student Loan",
                    "kind": "student_loan",
                    "balance_cents": 300_000,
                    "credit_limit_cents": 0,
                    "apr_bps": 550,
                    "min_payment_cents": 8_000,
                    "statement_closing_day": 1,
                    "payment_due_day": 22,
                    "autopay_mode": "minimum",
                },
            ],
            smoothed_monthly_income_cents=500_000,
            today=ref,
        )

    def test_strict_marginal_roi_priority_order_promo_cliff_before_high_apr(self):
        ref = date(2026, 11, 20)
        report = self._build_complex_credit_report(ref)
        goals = [
            _make_goal(1, "Urgent Laptop", 100_000, saved_cents=40_000, priority=1, target_date=(ref + timedelta(days=60)).isoformat()),
            _make_goal(2, "Later Vacation", 200_000, saved_cents=0, priority=4, target_date=(ref + timedelta(days=300)).isoformat()),
        ]
        engine = QuantMathEngine()

        # Large enough surplus ($4,000) to walk through ALL 7 tiers (a) -> (g)!
        steps = engine.optimize_surplus_allocation(
            monthly_surplus_cents=400_000,
            credit_report=report,
            emergency_buffer_gap_cents=50_000,
            goals=goals,
            safe_to_invest_cap_cents=None,
            today=ref,
        )

        assert all(isinstance(s, OptimalAllocationStep) for s in steps)
        assert sum(s.amount_cents for s in steps) == 400_000
        assert [s.priority_rank for s in steps] == list(range(1, len(steps) + 1))

        buckets = [s.bucket for s in steps]
        # Verify strict waterfall precedence:
        # (a) autopay_protection -> (b) promo_apr_cliff -> (c) high_apr_debt -> (d) azeo_fico_cliff -> (e) emergency_buffer -> (f) tracked_goal -> (g) invest_vault
        assert buckets.index("autopay_protection") < buckets.index("promo_apr_cliff")
        assert buckets.index("promo_apr_cliff") < buckets.index("high_apr_debt")
        assert buckets.index("high_apr_debt") < buckets.index("azeo_fico_cliff")
        assert buckets.index("azeo_fico_cliff") < buckets.index("emergency_buffer")
        assert buckets.index("emergency_buffer") < buckets.index("tracked_goal")
        assert buckets.index("tracked_goal") < buckets.index("invest_vault")

        # Promo cliff step took exact ceil($600 / 2mo) = $300.00!
        promo_step = next(s for s in steps if s.bucket == "promo_apr_cliff")
        assert promo_step.amount_cents == 30_000
        assert "Discover 0% Promo" in promo_step.target_name

        # Within high_apr_debt: Amex 26.99% (remaining $1,450 after $50 autopay shield) strictly precedes Citi 18.99% ($400)!
        high_apr_steps = [s for s in steps if s.bucket == "high_apr_debt"]
        assert [s.target_name for s in high_apr_steps] == ["Amex High APR", "Citi Custom"]
        assert high_apr_steps[0].amount_cents == 145_000
        assert high_apr_steps[1].amount_cents == 40_000

        # Within tracked_goal: Urgent P1 Laptop precedes P4 Vacation
        goal_steps = [s for s in steps if s.bucket == "tracked_goal"]
        assert goal_steps[0].target_name == "Urgent Laptop"

        # Every step has transparent formula_note and RSD-safe rationale
        for s in steps:
            assert s.amount_cents > 0
            assert s.formula_note.strip() != ""
            _assert_rsd_safe(s.rationale)

    @pytest.mark.parametrize(
        "surplus_cents,invest_cap",
        [
            (0, None),
            (1, None),
            (7, 0),
            (33_333, None),
            (99_999, 10_000),
            (250_001, 0),
            (543_217, 25_000),
            (1_234_567, 50_000),
        ],
    )
    def test_exact_cent_conservation_for_arbitrary_and_odd_surpluses(self, surplus_cents, invest_cap):
        ref = date(2026, 11, 20)
        report = self._build_complex_credit_report(ref)
        goals = [
            _make_goal(1, "Goal Alpha", 90_000, priority=1, target_date=(ref + timedelta(days=75)).isoformat()),
            _make_goal(2, "Goal Beta", 90_000, priority=2, target_date=(ref + timedelta(days=120)).isoformat()),
            _make_goal(3, "Goal Gamma", 90_000, priority=3, target_date=(ref + timedelta(days=200)).isoformat()),
        ]
        engine = QuantMathEngine()
        steps = engine.optimize_surplus_allocation(
            monthly_surplus_cents=surplus_cents,
            credit_report=report,
            emergency_buffer_gap_cents=45_678,
            goals=goals,
            safe_to_invest_cap_cents=invest_cap,
            today=ref,
        )

        if surplus_cents == 0:
            assert steps == []
        else:
            assert sum(s.amount_cents for s in steps) == surplus_cents
            assert all(s.amount_cents > 0 for s in steps)
            if invest_cap is not None:
                invest_total = sum(s.amount_cents for s in steps if s.bucket == "invest_vault")
                assert invest_total <= invest_cap

    def test_negative_surplus_returns_empty(self):
        engine = QuantMathEngine()
        assert engine.optimize_surplus_allocation(monthly_surplus_cents=-5_000) == []


class TestDeterministicQuantCoachAndQA:
    def _build_demo_state(self):
        svc = FinanceService(db_path=":memory:")
        ref = date(2026, 11, 20)
        from chronox_finance.demo_data import seed_demo_data

        seed_demo_data(svc, today=ref)
        budget = svc._compute_budget(today=ref)
        credit_report = svc.get_credit_report(today=ref)
        goals = svc.list_goals(include_completed=False)
        impulses = svc.list_impulses()
        txs = svc.storage.list_transactions(limit=2000)
        bills = svc.storage.list_recurring_bills(active_only=True)
        settings = svc.storage.get_settings()
        adhd_stats = svc.get_dashboard(today=ref).adhd_stats
        return svc, ref, budget, credit_report, goals, impulses, txs, bills, settings, adhd_stats

    def test_build_forecast_report_and_quant_suggestions_on_demo_workspace(self):
        svc, ref, budget, credit_report, goals, impulses, txs, bills, settings, adhd_stats = self._build_demo_state()
        try:
            engine = QuantMathEngine()
            report = engine.build_forecast_report(
                budget=budget,
                transactions=txs,
                recurring_bills=bills,
                settings=settings,
                credit_report=credit_report,
                goals=goals,
                days=30,
                today=ref,
            )
            assert isinstance(report, QuantForecastReport)
            assert report.horizon_days == 30
            assert len(report.daily_points) == 30
            assert report.volatility.sample_days > 0
            assert report.monthly_surplus_cents > 0
            assert sum(s.amount_cents for s in report.optimal_allocations) == report.monthly_surplus_cents
            assert report.total_expected_annual_benefit_cents > 0

            # Verify dict serialization round-trip
            dumped = to_dict(report)
            assert dumped["horizon_days"] == 30
            assert len(dumped["daily_points"]) == 30

            # Build up to 4 verified deterministic quant suggestions
            suggestions = engine.build_quant_suggestions(
                budget=budget,
                quant_forecast=report,
                credit_report=credit_report,
                impulses=impulses,
                goals=goals,
                adhd_stats=adhd_stats,
                needs_triage_count=1,
                reset_done_this_week=False,
                today=ref,
            )
            assert 1 <= len(suggestions) <= 4
            for sug in suggestions:
                assert sug.verified is True
                assert sug.source == "deterministic_quant"
                assert len(sug.verification_notes) >= 1
                _assert_rsd_safe(f"{sug.title} {sug.rationale} {sug.micro_step}")

            # Demo has Amex autopay OFF -> should surface ENABLE_AUTOPAY with real credit_line_id!
            autopay_sugs = [s for s in suggestions if s.action_type == SuggestionActionType.ENABLE_AUTOPAY]
            assert len(autopay_sugs) == 1
            assert isinstance(autopay_sugs[0].action_payload["credit_line_id"], int)

            # Also verify works cleanly when quant_forecast is passed as a dict OR omitted (None)
            sugs_from_dict = engine.build_quant_suggestions(
                budget=budget, quant_forecast=dumped, credit_report=credit_report, today=ref
            )
            sugs_auto = engine.build_quant_suggestions(
                budget=budget, quant_forecast=None, credit_report=credit_report, today=ref
            )
            assert len(sugs_from_dict) >= 1
            assert len(sugs_auto) >= 1
        finally:
            svc.close()

    @pytest.mark.parametrize(
        "question,expected_tokens,expect_afford",
        [
            (
                "How can I buy a $1,000 gadget without hurting my credit?",
                ["$1,000.00", "Runway", "Credit/AZEO"],
                True,
            ),
            (
                "Can I afford a $45 dinner tonight?",
                ["$45.00", "Safe-to-Spend", "Verdict"],
                True,
            ),
            (
                "How can I boost my FICO credit score using AZEO?",
                ["FICO & AZEO", "AZEO", "Autopay"],
                False,
            ),
            (
                "What order should I pay off my debt and allocate surplus?",
                ["Debt Payoff", "Marginal-ROI", "Discover"],
                False,
            ),
            (
                "Will I overdraft or hit a payday crunch in my 30-day cashflow runway?",
                ["30-Day Cashflow Runway", "Lowest Balance Nadir", "Next Payday"],
                False,
            ),
            (
                "What subscriptions and price hikes should I review?",
                ["Recurring Bills", "Price Hike", "Netflix"],
                False,
            ),
            (
                "How big should my emergency buffer be given my spending volatility?",
                ["Spending Volatility", "P90", "Emergency Buffer Target"],
                False,
            ),
            (
                "How are my goals looking and how much can I invest in ChronoX?",
                ["ChronoX Safe-to-Invest", "Goal"],
                False,
            ),
            (
                "How is my overall financial health right now?",
                ["Pure-Math Financial Health", "Safe-to-Spend Band", "Cashflow Runway"],
                False,
            ),
        ],
    )
    def test_answer_quant_question_all_intents(self, question, expected_tokens, expect_afford):
        svc, ref, budget, credit_report, goals, impulses, txs, bills, settings, _stats = self._build_demo_state()
        try:
            engine = QuantMathEngine()
            report = engine.build_forecast_report(
                budget=budget,
                transactions=txs,
                recurring_bills=bills,
                settings=settings,
                credit_report=credit_report,
                goals=goals,
                days=30,
                today=ref,
            )
            resp = engine.answer_quant_question(
                question=question,
                budget=budget,
                quant_forecast=report,
                credit_report=credit_report,
                impulses=impulses,
                goals=goals,
                hourly_wage_cents=int(settings["hourly_wage_cents"]),
                payday_cadence_days=int(settings["payday_cadence_days"]),
                today=ref,
            )
            assert resp.source == "deterministic_quant"
            assert resp.intelligence_mode == IntelligenceMode.DETERMINISTIC_ONLY.value
            assert resp.provider_used is None
            assert resp.model_used is None
            assert "safe_to_spend_dollars" in resp.verified_numbers
            assert "runway_lowest_balance_dollars" in resp.verified_numbers
            assert "stability_score_0_100" in resp.verified_numbers
            _assert_rsd_safe(resp.answer)

            for token in expected_tokens:
                assert token in resp.answer, f"Expected {token!r} in answer for {question!r}:\n{resp.answer}"

            if expect_afford:
                assert resp.affordability_check is not None
                assert "runway_nadir_after_dollars" in resp.affordability_check
                assert "verdict" in resp.affordability_check
            else:
                assert resp.affordability_check is None
        finally:
            svc.close()

    def test_quant_suggestions_surface_overdraft_runway_shield_calmly(self):
        ref = date(2026, 11, 10)
        auto = AutoBudgetEngine()
        accounts = [{"id": 1, "account_type": "checking", "balance_cents": 40_000, "is_liquid": 1}]
        jars = [
            {"id": 1, "slug": "bills", "name": "Bills", "kind": "bills", "icon": "H", "color": "#60a5fa", "priority": 1, "target_pct": 50.0, "monthly_target_cents": 150_000, "auto_allocated_cents": 150_000, "rollover_cents": 0, "is_locked": 1, "rollover_enabled": 1, "rollover_cap_cents": None},
            {"id": 2, "slug": "fun", "name": "Fun", "kind": "fun", "icon": "F", "color": "#fbbf24", "priority": 2, "target_pct": 50.0, "monthly_target_cents": 150_000, "auto_allocated_cents": 150_000, "rollover_cents": 0, "is_locked": 0, "rollover_enabled": 1, "rollover_cap_cents": None},
        ]
        bills = [
            {"id": 9, "name": "Rent", "normalized_merchant": "rent", "amount_cents": 120_000, "previous_amount_cents": None, "cadence": "monthly", "next_due_date": "2026-11-14", "jar_id": 1, "is_subscription": 0, "is_essential": 1, "auto_pay": 1, "active": 1, "confidence": 0.99, "flagged_zombie": 0},
        ]
        settings = {
            "emergency_buffer_cents": 50_000,
            "payday_cadence_days": 14,
            "next_payday_date": "2026-11-20",
            "expected_paycheck_cents": 200_000,
        }
        budget = auto.compute(
            accounts=accounts,
            jars=jars,
            transactions=[],
            recurring_bills=bills,
            transfers=[],
            settings=settings,
            today=ref,
        )
        engine = QuantMathEngine()
        qf = engine.build_forecast_report(
            budget=budget,
            transactions=[],
            recurring_bills=bills,
            settings=settings,
            days=30,
            today=ref,
        )
        assert qf.overdraft_risk is True
        assert qf.lowest_balance_cents == -80_000
        assert qf.lowest_balance_date == "2026-11-14"

        sugs = engine.build_quant_suggestions(budget=budget, quant_forecast=qf, today=ref)
        assert len(sugs) >= 1
        assert sugs[0].id == "quant-runway-shield"
        assert sugs[0].priority == "high"
        assert "2026-11-14" in sugs[0].title
        _assert_rsd_safe(f"{sugs[0].title} {sugs[0].rationale} {sugs[0].micro_step}")

    def test_qa_disambiguates_30_day_and_custom_500_surplus_allocation(self):
        svc, ref, budget, credit_report, goals, impulses, txs, bills, settings, _stats = self._build_demo_state()
        try:
            engine = QuantMathEngine()
            report = engine.build_forecast_report(
                budget=budget,
                transactions=txs,
                recurring_bills=bills,
                settings=settings,
                credit_report=credit_report,
                goals=goals,
                days=30,
                today=ref,
            )
            # "30-day" + "spend" must NOT misparse 30 as a $30 purchase
            r_runway = engine.answer_quant_question(
                question="How much do I spend in my 30-day cashflow runway?",
                budget=budget,
                quant_forecast=report,
                credit_report=credit_report,
                today=ref,
            )
            assert r_runway.affordability_check is None
            assert "30-Day Cashflow Runway" in r_runway.answer

            # Explicit "$500" with debt/surplus allocation intent runs live $500 waterfall!
            r_surplus = engine.answer_quant_question(
                question="How should I allocate $500 extra surplus toward my debt?",
                budget=budget,
                quant_forecast=report,
                credit_report=credit_report,
                goals=goals,
                today=ref,
            )
            assert r_surplus.affordability_check is None
            assert "$500.00 surplus" in r_surplus.answer
            assert "Marginal-ROI Waterfall" in r_surplus.answer
        finally:
            svc.close()

    def test_buffer_dip_warning_overdue_bill_and_empty_workspace_qa(self):
        ref = date(2026, 11, 10)
        engine = QuantMathEngine()

        # Overdue bill (2 days overdue, unpaid) hits day 0; dips below $500 buffer without going negative
        runway = engine.simulate_cashflow_runway(
            starting_balance_cents=60_000,
            recurring_bills=[
                {
                    "id": 1,
                    "name": "Electric",
                    "normalized_merchant": "electric",
                    "amount_cents": 25_000,
                    "cadence": "monthly",
                    "next_due_date": "2026-11-08",
                    "active": 1,
                }
            ],
            payday_cadence_days=30,
            next_payday_date="2026-11-25",
            estimated_paycheck_cents=150_000,
            emergency_buffer_cents=50_000,
            days=30,
            today=ref,
        )
        assert runway.overdraft_risk is False
        assert runway.lowest_balance_cents == 35_000
        assert runway.lowest_balance_date == "2026-11-10"
        assert runway.safe_runway_days == 30
        assert runway.payday_crunch_warning is not None
        assert "emergency shield" in runway.payday_crunch_warning
        assert any("Overdue bill: Electric" in e for e in runway.daily_points[0].events)

        # Unfunded minimums & proportional weighted goals under tight surplus
        steps_tight = engine.optimize_surplus_allocation(
            monthly_surplus_cents=25_001,
            unfunded_minimums_cents=5_000,
            goals=[
                _make_goal(1, "Priority One", 50_000, priority=1, target_date="2026-12-20"),
                _make_goal(2, "Priority Three", 50_000, priority=3, target_date="2027-06-01"),
            ],
            today=ref,
        )
        assert sum(s.amount_cents for s in steps_tight) == 25_001
        assert steps_tight[0].bucket == "autopay_protection"
        assert steps_tight[0].amount_cents == 5_000
        goal_steps = [s for s in steps_tight if s.bucket == "tracked_goal"]
        assert len(goal_steps) == 2
        assert goal_steps[0].target_name == "Priority One"
        assert goal_steps[0].amount_cents > goal_steps[1].amount_cents

        # Clean empty workspace Q&A for credit, bills, and goals
        auto = AutoBudgetEngine()
        budget = auto.compute(
            accounts=[{"id": 1, "account_type": "checking", "balance_cents": 200_000, "is_liquid": 1}],
            jars=[
                {"id": 1, "slug": "bills", "name": "Bills", "kind": "bills", "icon": "H", "color": "#60a5fa", "priority": 1, "target_pct": 50.0, "monthly_target_cents": 100_000, "auto_allocated_cents": 100_000, "rollover_cents": 0, "is_locked": 1, "rollover_enabled": 1, "rollover_cap_cents": None},
                {"id": 2, "slug": "invest", "name": "Invest", "kind": "invest", "icon": "I", "color": "#2dd4bf", "priority": 2, "target_pct": 50.0, "monthly_target_cents": 100_000, "auto_allocated_cents": 100_000, "rollover_cents": 0, "is_locked": 1, "rollover_enabled": 1, "rollover_cap_cents": None},
            ],
            transactions=[],
            recurring_bills=[],
            transfers=[],
            settings={"emergency_buffer_cents": 50_000},
            today=ref,
        )
        for q in (
            "What is my credit score utilization?",
            "What subscriptions do I have?",
            "How are my goals and investing looking?",
        ):
            ans = engine.answer_quant_question(question=q, budget=budget, today=ref)
            assert ans.source == "deterministic_quant"
            _assert_rsd_safe(ans.answer)

