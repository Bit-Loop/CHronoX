"""Comprehensive unit tests for OneTapQuantEngine and statistical primitives."""

from __future__ import annotations

import inspect
import math
import random
import re
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

from chronox_finance.engines.credit_debt import CreditDebtOptimizer
from chronox_finance.engines.onetap_quant import (
    OneTapPlan,
    OneTapQuantEngine,
    StatisticalDiagnostics,
    StatisticalRunwayResult,
)
from chronox_finance.engines.quant_math import (
    HYSA_BENCHMARK_BPS,
    QuantMathEngine,
    allocate_kkt_concave_cents,
    compute_dow_multipliers,
    compute_ewma_shrunk_rate,
    compute_hhi_index,
    compute_mad_stats,
    compute_mann_kendall_slope,
    compute_modified_z_score,
    huber_mad_clip_series,
)
from chronox_finance.models import (
    GoalCategory,
    GoalOut,
    HoldingOut,
    RecurrenceCadence,
)
from chronox_finance.service import FinanceService

_RSD_FORBIDDEN_RE = re.compile(
    r"\b(you\s+failed|irresponsible|reckless|terrible\s+(?:with|at)\s+money|"
    r"pathetic|lazy|stupid|shameful|wasted\s+money)\b",
    re.IGNORECASE,
)

# Constructed dynamically so the test asserts zero occurrences in production source/outputs
_BANNED_TERMS_RE = re.compile(
    r"("
    + "|".join(
        [
            "simu" + "lator",
            "what" + "-if",
            "what" + " if",
            "sand" + "box",
            "delta_min_" + "pts",
            "delta_max_" + "pts",
            r"\+\d+\s*" + "pts",
            r"24\.99%",
        ]
    )
    + r")",
    re.IGNORECASE,
)


def _assert_clean_copy(text: str) -> None:
    assert not _RSD_FORBIDDEN_RE.search(text), f"Found RSD-hostile phrasing: {text!r}"
    assert not _BANNED_TERMS_RE.search(text), f"Found banned wording: {text!r}"


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


class TestStatisticalMathPrimitives:
    def test_mad_and_modified_z_score_exact_formula(self):
        med, mad = compute_mad_stats([2000, 2200, 2500, 2700, 3000])
        assert med == 2500.0
        assert mad == 300.0
        z = compute_modified_z_score(10_000, med, mad)
        assert z == pytest.approx(0.6745 * (10_000 - 2500) / 300.0, rel=1e-6)
        assert z > 15.0

        # Zero-MAD fallback via MeanAD
        z_zero_mad = compute_modified_z_score(10_000, 2000.0, 0.0, mean_ad=1000.0)
        assert z_zero_mad == pytest.approx(8000.0 / 1253.314, rel=1e-4)
        assert compute_mad_stats([]) == (0.0, 0.0)
        assert compute_modified_z_score(100, 100, 0.0, 0.0) == 0.0

    def test_huber_mad_clip_preserves_normal_days_and_clips_oneoff_spike(self):
        # Sparse zero-inflated daily spend: 40 zero days, 20 normal $25 days, 1 massive $5,000 outlier
        series = [0.0] * 40 + [2500.0] * 20 + [500_000.0]
        clipped = huber_mad_clip_series(series, k=2.5)
        assert len(clipped) == len(series)
        assert all(v == 0.0 for v in clipped[:40])
        assert all(v == 2500.0 for v in clipped[40:60])
        # Outlier is sharply clipped instead of remaining $5,000
        assert clipped[-1] < 10_000.0
        assert clipped[-1] >= 2500.0

    def test_ewma_shrunk_rate_outlier_resistance_and_alpha(self):
        baseline = [3000.0] * 60
        shrunk_clean, ewma_clean, med_clean = compute_ewma_shrunk_rate(baseline, alpha=0.18)
        assert shrunk_clean == pytest.approx(3000.0, abs=1.0)
        assert ewma_clean == pytest.approx(3000.0, abs=1.0)
        assert med_clean == pytest.approx(3000.0, abs=1.0)

        # Inject single $4,500 one-off spike on the very last day
        spiked = [3000.0] * 59 + [450_000.0]
        shrunk_spiked, _ewma_spiked, med_spiked = compute_ewma_shrunk_rate(spiked, alpha=0.18)
        assert med_spiked == pytest.approx(3000.0, abs=1.0)
        # Naive EWMA would jump to 0.18*450000 + 0.82*3000 = $834.60/day;
        # Huber-clipped shrunk EWMA stays tightly near $30-$35/day!
        assert shrunk_spiked < 4_000.0

    def test_dow_multipliers_threshold_and_mean_conservation(self):
        ref = date(2026, 11, 20)
        # Fewer than 21 days -> seasonality inactive, all multipliers == 1.0
        short_dates = [ref - timedelta(days=i) for i in range(13, -1, -1)]
        short_vals = [10_000.0 if d.weekday() in (4, 5) else 1_000.0 for d in short_dates]
        m_short, active_short = compute_dow_multipliers(short_dates, short_vals, min_days=21)
        assert active_short is False
        assert all(v == 1.0 for v in m_short.values())

        # 35 days (>= 21) -> weekend spikes clearly reflected and 7-day mean normalized to 1.0
        long_dates = [ref - timedelta(days=i) for i in range(34, -1, -1)]
        long_vals = [12_000.0 if d.weekday() in (4, 5) else 1_500.0 for d in long_dates]
        m_long, active_long = compute_dow_multipliers(long_dates, long_vals, min_days=21)
        assert active_long is True
        assert m_long["fri"] > 1.4
        assert m_long["sat"] > 1.4
        assert m_long["tue"] < 0.8
        assert sum(m_long.values()) / 7.0 == pytest.approx(1.0, abs=1e-3)

    def test_mann_kendall_accelerating_cooling_and_stable(self):
        up = compute_mann_kendall_slope([10_000, 12_500, 15_000, 17_500, 20_000, 22_500])
        assert up.trend_direction == "accelerating"
        assert up.is_significant is True
        assert up.drift_cents_per_week == 2_500
        assert up.p_value < 0.05

        down = compute_mann_kendall_slope([25_000, 22_000, 19_000, 16_000, 13_000, 10_000])
        assert down.trend_direction == "cooling"
        assert down.is_significant is True
        assert down.drift_cents_per_week == -3_000
        assert down.p_value < 0.05

        flat = compute_mann_kendall_slope([15_000, 15_200, 14_800, 15_100, 14_900, 15_000])
        assert flat.trend_direction == "stable"
        assert flat.is_significant is False

        too_short = compute_mann_kendall_slope([10_000, 20_000, 30_000])
        assert too_short.trend_direction == "stable"
        assert too_short.is_significant is False

    def test_hhi_index_canonical_scale_and_bands(self):
        assert compute_hhi_index([]) == (0, "none")
        assert compute_hhi_index([0, 0]) == (0, "none")
        assert compute_hhi_index([50_000]) == (10_000, "concentrated")
        assert compute_hhi_index([50_000, 50_000]) == (5_000, "concentrated")
        assert compute_hhi_index([10_000] * 5) == (2_000, "moderate")
        assert compute_hhi_index([10_000] * 10) == (1_000, "diversified")

    @pytest.mark.parametrize("budget", [0, 1, 7, 33_333, 99_999, 250_001, 543_217])
    def test_kkt_allocator_exact_cent_conservation_and_optimality(self, budget):
        weights = [30.0, 20.0, 10.0]
        scales = [40_000.0, 40_000.0, 40_000.0]
        caps = [120_000, 150_000, 200_000]
        allocs, lam = allocate_kkt_concave_cents(budget, weights, scales, caps)

        expected_sum = min(budget, sum(caps))
        assert sum(allocs) == expected_sum
        assert all(0 <= a <= c for a, c in zip(allocs, caps, strict=True))
        if budget > 0:
            # Highest-weight bucket either hits its exact cap or receives >= lower-weight bucket
            assert allocs[0] == caps[0] or allocs[0] >= allocs[1]
            assert allocs[1] == caps[1] or allocs[1] >= allocs[2]
        if 0 < budget < sum(caps):
            assert lam > 0.0
            # Verify KKT stationarity: unconstrained interior allocations share identical MU ≈ lam
            for a, w, sc, c in zip(allocs, weights, scales, caps, strict=True):
                if 0 < a < c:
                    mu = w / (1.0 + a / sc)
                    assert mu == pytest.approx(lam, rel=1e-2)


class TestStatisticalRunwayEngine:
    def test_fresh_zero_profile_invariant_all_zeros_and_needs_setup(self):
        ref = date(2026, 11, 20)
        engine = OneTapQuantEngine()
        res = engine.compute_statistical_runway(
            starting_balance_cents=0,
            recurring_bills=[],
            transactions=[],
            estimated_paycheck_cents=0,
            user_configured_payday=False,
            horizon_days=30,
            today=ref,
        )
        assert isinstance(res, StatisticalRunwayResult)
        assert res.needs_setup is True
        assert res.starting_balance_cents == 0
        assert res.sample_days == 0
        assert res.ewma_daily_burn_cents == 0
        assert res.robust_median_daily_burn_cents == 0
        assert res.shrunk_daily_burn_cents == 0
        assert res.p10_ending_cents == 0
        assert res.p50_ending_cents == 0
        assert res.p90_ending_cents == 0
        assert res.p05_nadir_cents == 0
        assert res.p10_nadir_cents == 0
        assert res.p50_nadir_cents == 0
        assert res.var_95_shortfall_cents == 0
        assert res.cvar_95_buffer_cents == 0
        assert res.overdraft_safe_lump_sum_cents == 0
        assert res.confidence_95_daily_safe_cents == 0
        assert res.ruin_probability_pct == 0.0
        assert len(res.daily_fan_points) == 30
        assert all(
            pt.p10_balance_cents == 0 and pt.p50_balance_cents == 0 and pt.p90_balance_cents == 0
            for pt in res.daily_fan_points
        )

    def test_seeded_determinism_and_no_global_rng_mutation(self):
        ref = date(2026, 11, 20)
        engine = OneTapQuantEngine()
        txs = [
            {
                "tx_type": "expense",
                "jar_id": 2,
                "amount_cents": 2_000 + (i % 7) * 800,
                "normalized_merchant": "shop",
                "tx_date": (ref - timedelta(days=i)).isoformat(),
            }
            for i in range(45)
        ]
        bills = [
            {
                "id": 1,
                "name": "Rent",
                "normalized_merchant": "rent",
                "amount_cents": 140_000,
                "cadence": RecurrenceCadence.MONTHLY.value,
                "next_due_date": "2026-12-01",
                "active": 1,
            }
        ]

        random.seed(999)
        before_val = random.random()  # noqa: S311 - testing global RNG isolation
        random.seed(999)

        r1 = engine.compute_statistical_runway(
            starting_balance_cents=350_000,
            recurring_bills=bills,
            transactions=txs,
            estimated_paycheck_cents=220_000,
            next_payday_date="2026-11-27",
            horizon_days=60,
            seed=42,
            today=ref,
        )
        after_val = random.random()  # noqa: S311 - testing global RNG isolation
        assert after_val == before_val  # Global RNG state completely untouched

        r2 = engine.compute_statistical_runway(
            starting_balance_cents=350_000,
            recurring_bills=bills,
            transactions=txs,
            estimated_paycheck_cents=220_000,
            next_payday_date="2026-11-27",
            horizon_days=60,
            seed=42,
            today=ref,
        )
        assert r1.model_dump(exclude={"generated_at"}) == r2.model_dump(exclude={"generated_at"})

    @pytest.mark.parametrize("horizon", [30, 60, 90])
    def test_horizons_monotonicity_var_cvar_and_dow_seasonality(self, horizon):
        ref = date(2026, 11, 20)
        engine = OneTapQuantEngine()
        # 42 days of weekend-heavy discretionary spend + excluded monthly bills
        jars = [
            {"id": 1, "slug": "bills", "kind": "bills", "is_locked": True},
            {"id": 2, "slug": "fun", "kind": "fun", "is_locked": False},
        ]
        txs = []
        for offset in range(42):
            dt = ref - timedelta(days=offset)
            amt = 9_500 if dt.weekday() in (4, 5) else 1_800
            txs.append(
                {
                    "tx_type": "expense",
                    "jar_id": 2,
                    "amount_cents": amt,
                    "normalized_merchant": "dining",
                    "tx_date": dt.isoformat(),
                }
            )
        # Add split transaction (only Fun portion should enter discretionary burn)
        txs.append(
            {
                "id": 999,
                "tx_type": "expense",
                "jar_id": 1,
                "amount_cents": 150_000,
                "normalized_merchant": "landlord",
                "tx_date": (ref - timedelta(days=10)).isoformat(),
                "splits": [
                    {"jar_id": 1, "amount_cents": 148_000, "memo": "Rent"},
                    {"jar_id": 2, "amount_cents": 2_000, "memo": "Coffee"},
                ],
            }
        )
        bills = [
            {
                "id": 1,
                "name": "Rent",
                "normalized_merchant": "landlord",
                "amount_cents": 148_000,
                "cadence": "monthly",
                "next_due_date": "2026-12-01",
                "active": 1,
                "is_essential": 1,
            },
            {
                "id": 2,
                "name": "Weekly Box",
                "normalized_merchant": "mealbox",
                "amount_cents": 4_500,
                "cadence": "weekly",
                "next_due_date": "2026-11-23",
                "active": 1,
                "is_essential": 1,
            },
        ]

        res = engine.compute_statistical_runway(
            starting_balance_cents=320_000,
            recurring_bills=bills,
            transactions=txs,
            jars=jars,
            payday_cadence_days=14,
            next_payday_date="2026-11-27",
            estimated_paycheck_cents=240_000,
            emergency_buffer_cents=80_000,
            horizon_days=horizon,
            today=ref,
        )

        assert res.needs_setup is False
        assert res.horizon_days == horizon
        assert len(res.daily_fan_points) == horizon
        assert res.seasonality_active is True
        assert res.dow_multipliers["fri"] > res.dow_multipliers["tue"]
        assert res.dow_multipliers["sat"] > res.dow_multipliers["wed"]
        # Monotonic percentiles and coherent risk measure CVaR >= VaR >= 0
        assert res.p10_ending_cents <= res.p50_ending_cents <= res.p90_ending_cents
        assert res.p05_nadir_cents <= res.p10_nadir_cents <= res.p50_nadir_cents <= res.p90_nadir_cents
        assert res.cvar_95_buffer_cents >= res.var_95_shortfall_cents >= 0
        assert 0.0 <= res.ruin_probability_pct <= 100.0
        # Split Rent ($1,480) was excluded from discretionary burn
        assert res.shrunk_daily_burn_cents < 8_000

    def test_calm_vs_volatile_spender_cvar_and_daily_safe_cap(self):
        ref = date(2026, 11, 20)
        engine = OneTapQuantEngine()

        # Calm: steady $40/day for 42 days
        calm_txs = [
            {
                "tx_type": "expense",
                "jar_id": 2,
                "amount_cents": 4_000,
                "normalized_merchant": "market",
                "tx_date": (ref - timedelta(days=d)).isoformat(),
            }
            for d in range(42)
        ]

        # Spiky: same ~$40/day mean ($280/wk), oscillating wild weeks ($0-$700)
        spiky_week_totals = [0, 56_000, 4_000, 64_000, 2_000, 42_000]
        spiky_txs = []
        for w_idx, total in enumerate(spiky_week_totals):
            if total > 0:
                spiky_txs.append(
                    {
                        "tx_type": "expense",
                        "jar_id": 2,
                        "amount_cents": total,
                        "normalized_merchant": "shop",
                        "tx_date": (ref - timedelta(days=w_idx * 7 + 1)).isoformat(),
                    }
                )

        r_calm = engine.compute_statistical_runway(
            starting_balance_cents=250_000,
            transactions=calm_txs,
            horizon_days=30,
            today=ref,
        )
        r_spiky = engine.compute_statistical_runway(
            starting_balance_cents=250_000,
            transactions=spiky_txs,
            horizon_days=30,
            today=ref,
        )

        # Constant spender has tight fan; volatile spender has wider fan, higher CVaR, tighter safe daily cap
        assert (r_spiky.p90_ending_cents - r_spiky.p10_ending_cents) > (
            r_calm.p90_ending_cents - r_calm.p10_ending_cents
        )
        assert r_spiky.cvar_95_buffer_cents > r_calm.cvar_95_buffer_cents
        assert r_spiky.confidence_95_daily_safe_cents < r_calm.confidence_95_daily_safe_cents

    def test_guaranteed_overdraft_gives_100pct_ruin_and_zero_daily_safe(self):
        ref = date(2026, 11, 10)
        engine = OneTapQuantEngine()
        bills = [
            {
                "id": 1,
                "name": "Rent",
                "normalized_merchant": "rent",
                "amount_cents": 150_000,
                "cadence": "monthly",
                "next_due_date": "2026-11-12",
                "active": 1,
            }
        ]
        res = engine.compute_statistical_runway(
            starting_balance_cents=40_000,
            recurring_bills=bills,
            payday_cadence_days=14,
            next_payday_date="2026-11-20",
            estimated_paycheck_cents=200_000,
            horizon_days=30,
            today=ref,
        )
        assert res.ruin_probability_pct == 100.0
        assert res.p50_nadir_cents == -110_000
        assert res.overdraft_var_95_cents == 110_000
        assert res.confidence_95_daily_safe_cents == 0
        assert res.overdraft_safe_lump_sum_cents == 0


class TestStatisticalDiagnostics:
    def test_mad_modified_z_detector_flags_recent_outlier_and_ignores_old_or_fixed_bills(self):
        ref = date(2026, 11, 20)
        engine = OneTapQuantEngine()
        jars = [
            {"id": 1, "slug": "bills", "name": "Bills", "kind": "bills", "is_locked": True},
            {"id": 2, "slug": "dining", "name": "Dining", "kind": "fun", "is_locked": False},
        ]
        bills = [
            {
                "id": 1,
                "name": "Apartment Rent",
                "normalized_merchant": "oakridge rent",
                "amount_cents": 160_000,
                "cadence": "monthly",
                "next_due_date": "2026-12-01",
                "active": 1,
            }
        ]
        txs = [
            # Normal dining baseline ($20-$32)
            {"id": 1, "tx_type": "expense", "jar_id": 2, "subcategory": "dining", "amount_cents": 2_200, "merchant": "Chipotle", "normalized_merchant": "chipotle", "tx_date": (ref - timedelta(days=50)).isoformat()},
            {"id": 2, "tx_type": "expense", "jar_id": 2, "subcategory": "dining", "amount_cents": 2_500, "merchant": "Sweetgreen", "normalized_merchant": "sweetgreen", "tx_date": (ref - timedelta(days=42)).isoformat()},
            {"id": 3, "tx_type": "expense", "jar_id": 2, "subcategory": "dining", "amount_cents": 2_800, "merchant": "Ramen Shop", "normalized_merchant": "ramen", "tx_date": (ref - timedelta(days=34)).isoformat()},
            {"id": 4, "tx_type": "expense", "jar_id": 2, "subcategory": "dining", "amount_cents": 2_400, "merchant": "Thai Kitchen", "normalized_merchant": "thai", "tx_date": (ref - timedelta(days=26)).isoformat()},
            {"id": 5, "tx_type": "expense", "jar_id": 2, "subcategory": "dining", "amount_cents": 2_600, "merchant": "Taco Bar", "normalized_merchant": "taco", "tx_date": (ref - timedelta(days=18)).isoformat()},
            # Old outlier (20 days ago -> outside 14-day window, must NOT be flagged)
            {"id": 6, "tx_type": "expense", "jar_id": 2, "subcategory": "dining", "amount_cents": 38_000, "merchant": "Old Banquet", "normalized_merchant": "old banquet", "tx_date": (ref - timedelta(days=20)).isoformat()},
            # Recent 14-day outlier ($465 steakhouse -> MUST be flagged!)
            {"id": 7, "tx_type": "expense", "jar_id": 2, "subcategory": "dining", "amount_cents": 46_500, "merchant": "Prime Steakhouse", "normalized_merchant": "prime steakhouse", "tx_date": (ref - timedelta(days=3)).isoformat()},
            # Normal recurring Rent 2 days ago -> MUST NOT be flagged as anomaly!
            {"id": 8, "tx_type": "expense", "jar_id": 1, "subcategory": "rent", "amount_cents": 160_000, "merchant": "Oakridge Rent", "normalized_merchant": "oakridge rent", "tx_date": (ref - timedelta(days=2)).isoformat()},
        ]

        diag = engine.compute_statistical_diagnostics(
            transactions=txs,
            jars=jars,
            recurring_bills=bills,
            today=ref,
        )
        assert isinstance(diag, StatisticalDiagnostics)
        assert len(diag.anomalies) == 1
        anom = diag.anomalies[0]
        assert anom.merchant == "Prime Steakhouse"
        assert anom.amount_cents == 46_500
        assert anom.category == "dining"
        assert anom.modified_z >= 3.5
        assert 2_000 <= anom.baseline_median_cents <= 3_500

    def test_mann_kendall_weekly_spend_drift_accelerating_and_cooling(self):
        ref = date(2026, 11, 20)
        engine = OneTapQuantEngine()

        # 8 complete weeks of steadily rising weekly spend ($100 -> $240/wk)
        rising_txs = [
            {
                "tx_type": "expense",
                "jar_id": 2,
                "amount_cents": 10_000 + w * 2_000,
                "merchant": "Grocery",
                "normalized_merchant": "grocery",
                "tx_date": (ref - timedelta(days=(7 - w) * 7 + 2)).isoformat(),
            }
            for w in range(8)
        ]
        diag_up = engine.compute_statistical_diagnostics(transactions=rising_txs, today=ref)
        assert diag_up.trend_direction == "accelerating"
        assert diag_up.drift_cents_per_week == 2_000
        assert diag_up.drift.is_significant is True
        assert diag_up.drift.p_value < 0.05

        # 8 complete weeks of steadily cooling weekly spend ($240 -> $100/wk)
        cooling_txs = [
            {
                "tx_type": "expense",
                "jar_id": 2,
                "amount_cents": 24_000 - w * 2_000,
                "merchant": "Grocery",
                "normalized_merchant": "grocery",
                "tx_date": (ref - timedelta(days=(7 - w) * 7 + 2)).isoformat(),
            }
            for w in range(8)
        ]
        diag_down = engine.compute_statistical_diagnostics(transactions=cooling_txs, today=ref)
        assert diag_down.trend_direction == "cooling"
        assert diag_down.drift_cents_per_week == -2_000
        assert diag_down.drift.is_significant is True

    def test_category_hhi_and_portfolio_hhi_with_cost_basis_known_awareness(self):
        ref = date(2026, 11, 20)
        engine = OneTapQuantEngine()

        # 2 equal categories -> exact spend HHI == 5000
        txs = [
            {"tx_type": "expense", "subcategory": "groceries", "amount_cents": 40_000, "merchant": "TJ", "tx_date": (ref - timedelta(days=5)).isoformat()},
            {"tx_type": "expense", "subcategory": "dining", "amount_cents": 40_000, "merchant": "Cafe", "tx_date": (ref - timedelta(days=4)).isoformat()},
        ]

        # Portfolio: $6,000 VTI (known basis $5,000 -> +$1,000 PnL) + $4,000 SHIB (unknown cost basis, sub-cent e8!)
        holdings = [
            HoldingOut(
                id=1,
                symbol="VTI",
                name="Vanguard Total Stock ETF",
                asset_class="etf",
                quantity_e8=20_00000000,
                cost_basis_cents=500_000,
                cost_basis_known=True,
                price_e8=300_00000000,
                market_value_cents=600_000,
                unrealized_pnl_cents=100_000,
                unrealized_pnl_pct=20.0,
                source="manual",
                created_at="2026-11-01T00:00:00Z",
                updated_at="2026-11-20T00:00:00Z",
            ),
            {
                "symbol": "SHIB",
                "asset_class": "crypto",
                "quantity_e8": 200_000_000_00000000,  # 200M SHIB
                "price_e8": 2_000,  # $0.00002000 -> exact $4,000.00 (400_000 cents)
                "cost_basis_cents": 0,
                "cost_basis_known": False,
            },
        ]

        diag = engine.compute_statistical_diagnostics(
            transactions=txs,
            holdings=holdings,
            today=ref,
        )
        assert diag.spend_hhi == 5_000
        assert diag.spend_concentration_band == "concentrated"

        p = diag.portfolio
        assert p.has_holdings is True
        assert p.total_value_cents == 1_000_000  # $6,000 + $4,000
        # 60%^2 + 40%^2 = 3600 + 1600 = 5200
        assert p.symbol_hhi == 5_200
        assert p.asset_class_hhi == 5_200
        assert p.symbol_concentration_band == "concentrated"
        assert p.top_symbol == "VTI"
        assert p.top_symbol_share_pct == 60.0
        # Cost basis coverage honesty: only VTI ($6,000 / $10,000 = 60%) is known!
        assert p.known_cost_basis_count == 1
        assert p.unknown_cost_basis_count == 1
        assert p.known_cost_basis_value_cents == 600_000
        assert p.unknown_cost_basis_value_cents == 400_000
        assert p.cost_basis_coverage_pct == 60.0
        assert p.all_cost_basis_known is False
        # P&L strictly excludes unknown-basis SHIB (never fabricating +$4,000 profit!)
        assert p.unrealized_pnl_known_only_cents == 100_000
        assert p.cost_basis_warning is not None
        assert "unknown cost basis" in p.cost_basis_warning.lower()


class TestOneTapCapitalOptimizer:
    def test_fresh_zero_service_workspace_returns_zero_and_needs_setup(self):
        svc = FinanceService(db_path=":memory:")
        try:
            ref = date(2026, 11, 20)
            budget = svc._compute_budget(today=ref)
            settings = svc.storage.get_settings()
            txs = svc.storage.list_transactions(limit=500)
            bills = svc.storage.list_recurring_bills(active_only=True)

            plan = OneTapQuantEngine().build_onetap_plan(
                budget=budget,
                transactions=txs,
                recurring_bills=bills,
                settings=settings,
                today=ref,
            )
            assert isinstance(plan, OneTapPlan)
            assert plan.needs_setup is True
            assert plan.runway.needs_setup is True
            assert plan.actions == []
            assert plan.available_unlocked_surplus_cents == 0
            assert plan.allocated_surplus_cents == 0
            assert plan.recommended_emergency_buffer_cents == 0
            assert plan.statistical_safe_to_invest_cents == 0
            assert plan.total_expected_annual_savings_cents == 0
            _assert_clean_copy(plan.summary)
        finally:
            svc.close()

    def test_rebalance_jar_hard_firewall_never_raids_bills_or_locked_jars(self):
        ref = date(2026, 11, 20)
        engine = OneTapQuantEngine()

        # Case 1: Only Bills (even with is_locked=False!) and locked Safety have surplus -> ZERO rebalances!
        jars_only_protected = [
            {"id": 1, "slug": "bills", "name": "Bills", "kind": "bills", "available_cents": 200_000, "is_locked": False},
            {"id": 2, "slug": "safety", "name": "Safety", "kind": "safety", "available_cents": 150_000, "is_locked": True},
            {"id": 3, "slug": "supplies", "name": "Supplies", "kind": "daily", "available_cents": 80_000, "is_locked": True},
            {"id": 4, "slug": "fun", "name": "Fun", "kind": "fun", "available_cents": -12_000, "is_locked": False},
        ]
        plan_protected = engine.build_onetap_plan(
            starting_balance_cents=400_000,
            jars=jars_only_protected,
            today=ref,
        )
        rebal_protected = [a for a in plan_protected.actions if a.action_type == "rebalance_jar"]
        assert rebal_protected == []

        # Case 2: Unlocked supplies ($60) and goals ($80) cover $100 deficit in fun; Bills & locked Safety untouched!
        jars_mixed = [
            {"id": 1, "slug": "bills", "name": "Bills", "kind": "bills", "available_cents": 200_000, "is_locked": True},
            {"id": 2, "slug": "safety", "name": "Safety", "kind": "safety", "available_cents": 150_000, "is_locked": True},
            {"id": 3, "slug": "supplies", "name": "Supplies", "kind": "daily", "available_cents": 6_000, "is_locked": False},
            {"id": 4, "slug": "goals", "name": "Goals", "kind": "goals", "available_cents": 8_000, "is_locked": False},
            {"id": 5, "slug": "fun", "name": "Fun", "kind": "fun", "available_cents": -10_000, "is_locked": False},
        ]
        plan_mixed = engine.build_onetap_plan(
            starting_balance_cents=400_000,
            jars=jars_mixed,
            today=ref,
        )
        rebals = [a for a in plan_mixed.actions if a.action_type == "rebalance_jar"]
        assert len(rebals) == 2
        assert sum(a.amount_cents for a in rebals) == 10_000
        for act in rebals:
            assert act.executable is True
            assert act.payload["from_jar_id"] not in (1, 2)
            assert act.payload["to_jar_id"] == 5
            assert act.undo_payload["from_jar_id"] == 5
            assert act.undo_payload["to_jar_id"] == act.payload["from_jar_id"]
            assert act.undo_payload["amount_cents"] == act.amount_cents

    def test_kkt_goal_funding_respects_5pct_overdraft_ceiling_and_exact_cents(self):
        ref = date(2026, 11, 20)
        engine = OneTapQuantEngine()

        # Realistic 35-day spending history with volatility
        txs = [
            {
                "tx_type": "expense",
                "jar_id": 2,
                "amount_cents": 2_500 + ((d * 7) % 5) * 900,
                "normalized_merchant": "groceries",
                "tx_date": (ref - timedelta(days=d)).isoformat(),
            }
            for d in range(35)
        ]
        jars = [
            {"id": 1, "slug": "bills", "name": "Bills", "kind": "bills", "available_cents": 120_000, "is_locked": True},
            {"id": 2, "slug": "daily", "name": "Daily", "kind": "daily", "available_cents": 80_000, "is_locked": False},
            {"id": 3, "slug": "safety", "name": "Safety", "kind": "safety", "available_cents": 50_000, "is_locked": True},
            {"id": 4, "slug": "invest", "name": "Invest", "kind": "invest", "available_cents": 40_000, "is_locked": True},
        ]
        bills = [
            {
                "id": 1,
                "name": "Rent",
                "normalized_merchant": "rent",
                "amount_cents": 120_000,
                "cadence": "monthly",
                "next_due_date": "2026-12-01",
                "active": 1,
            }
        ]
        goals = [
            _make_goal(1, "Urgent Laptop", 150_000, saved_cents=30_000, priority=1, target_date=(ref + timedelta(days=45)).isoformat()),
            _make_goal(2, "Summer Trip", 200_000, saved_cents=20_000, priority=3, target_date=(ref + timedelta(days=180)).isoformat()),
            _make_goal(3, "Distant Car", 500_000, saved_cents=0, priority=5, target_date=(ref + timedelta(days=360)).isoformat()),
        ]

        plan = engine.build_onetap_plan(
            starting_balance_cents=650_000,
            recurring_bills=bills,
            transactions=txs,
            jars=jars,
            goals=goals,
            unpaid_bills_until_payday_cents=120_000,
            emergency_buffer_cents=50_000,
            payday_cadence_days=14,
            next_payday_date="2026-11-27",
            estimated_paycheck_cents=250_000,
            user_configured_payday=True,
            horizon_days=30,
            seed=42,
            today=ref,
        )

        goal_acts = [a for a in plan.actions if a.action_type == "fund_goal"]
        assert len(goal_acts) >= 1
        total_goal_alloc = sum(a.amount_cents for a in goal_acts)
        assert total_goal_alloc == plan.allocated_surplus_cents
        assert total_goal_alloc <= plan.available_unlocked_surplus_cents
        assert plan.unallocated_surplus_cents == plan.available_unlocked_surplus_cents - plan.allocated_surplus_cents
        # Urgent P1 goal is funded first: either 100% completed or strictly >= lower-priority goal
        assert goal_acts[0].payload["goal_id"] == 1
        if len(goal_acts) >= 2:
            assert (
                goal_acts[0].payload["remaining_after_cents"] == 0
                or goal_acts[0].amount_cents >= goal_acts[1].amount_cents
            )

        # Constrained surplus test (surplus smaller than P1 remaining -> P1 dominates P3/P5)
        plan_constrained = engine.build_onetap_plan(
            starting_balance_cents=460_000,
            recurring_bills=bills,
            transactions=txs,
            jars=jars,
            goals=[
                _make_goal(1, "Urgent Laptop", 250_000, saved_cents=0, priority=1, target_date=(ref + timedelta(days=45)).isoformat()),
                _make_goal(2, "Summer Trip", 250_000, saved_cents=0, priority=3, target_date=(ref + timedelta(days=180)).isoformat()),
            ],
            unpaid_bills_until_payday_cents=120_000,
            emergency_buffer_cents=50_000,
            payday_cadence_days=14,
            next_payday_date="2026-11-27",
            estimated_paycheck_cents=250_000,
            user_configured_payday=True,
            horizon_days=30,
            seed=42,
            today=ref,
        )
        c_goals = [a for a in plan_constrained.actions if a.action_type == "fund_goal"]
        assert len(c_goals) >= 1
        assert c_goals[0].payload["goal_id"] == 1
        if len(c_goals) >= 2:
            assert c_goals[0].amount_cents > c_goals[1].amount_cents

        # Hard verification: withdrawing `allocated_surplus_cents` on day 0 keeps P(overdraft) <= 5.0%!
        post_runway = engine.compute_statistical_runway(
            starting_balance_cents=650_000 - plan.allocated_surplus_cents,
            recurring_bills=bills,
            transactions=txs,
            jars=jars,
            payday_cadence_days=14,
            next_payday_date="2026-11-27",
            estimated_paycheck_cents=250_000,
            user_configured_payday=True,
            horizon_days=30,
            seed=42,
            today=ref,
        )
        assert post_runway.ruin_probability_pct <= 5.0

    def test_set_emergency_buffer_and_safe_invest_cap_respect_cvar_and_firewalls(self):
        ref = date(2026, 11, 20)
        engine = OneTapQuantEngine()
        txs = [
            {
                "tx_type": "expense",
                "jar_id": 2,
                "amount_cents": 3_500 + (d % 4) * 1_200,
                "normalized_merchant": "groceries",
                "tx_date": (ref - timedelta(days=d)).isoformat(),
            }
            for d in range(30)
        ]
        bills = [
            {
                "id": 1,
                "name": "Rent",
                "normalized_merchant": "rent",
                "amount_cents": 120_000,
                "cadence": "monthly",
                "next_due_date": "2026-11-25",
                "active": 1,
            }
        ]
        jars = [
            {"id": 1, "slug": "bills", "name": "Bills", "kind": "bills", "available_cents": 120_000, "is_locked": True},
            {"id": 2, "slug": "daily", "name": "Daily", "kind": "daily", "available_cents": 60_000, "is_locked": False},
            {"id": 3, "slug": "safety", "name": "Safety", "kind": "safety", "available_cents": 80_000, "is_locked": True},
            {"id": 4, "slug": "invest", "name": "Invest", "kind": "invest", "available_cents": 90_000, "is_locked": True},
        ]

        # Ample cash ($6,000) -> aligns buffer upward toward 95% CVaR & computes strict invest cap
        plan_ample = engine.build_onetap_plan(
            starting_balance_cents=600_000,
            recurring_bills=bills,
            transactions=txs,
            jars=jars,
            unpaid_bills_until_payday_cents=120_000,
            credit_card_reserved_cents=30_000,
            emergency_buffer_cents=40_000,
            safe_to_invest_cents=90_000,
            payday_cadence_days=14,
            next_payday_date="2026-11-28",
            estimated_paycheck_cents=250_000,
            user_configured_payday=True,
            today=ref,
        )
        buf_acts = [a for a in plan_ample.actions if a.action_type == "set_emergency_buffer"]
        inv_acts = [a for a in plan_ample.actions if a.action_type == "set_safe_invest_cap"]
        assert len(buf_acts) == 1
        assert buf_acts[0].payload["emergency_buffer_cents"] > 40_000
        assert buf_acts[0].payload["emergency_buffer_cents"] <= plan_ample.runway.cvar_95_buffer_cents
        assert buf_acts[0].undo_payload["emergency_buffer_cents"] == 40_000

        assert len(inv_acts) == 1
        cap = plan_ample.statistical_safe_to_invest_cents
        assert cap == inv_acts[0].payload["safe_to_invest_cents"]
        assert cap <= 90_000
        assert cap <= max(
            0,
            600_000 - 120_000 - 30_000 - 80_000 - max(buf_acts[0].payload["emergency_buffer_cents"], plan_ample.runway.cvar_95_buffer_cents),
        )

        # Tight cash ($1,000 with $1,200 rent due in 5d -> overdraft risk 100%) -> NO buffer hike, invest cap = 0, NO goal funding!
        plan_tight = engine.build_onetap_plan(
            starting_balance_cents=100_000,
            recurring_bills=bills,
            transactions=txs,
            jars=jars,
            goals=[_make_goal(1, "Laptop", 100_000, priority=1)],
            unpaid_bills_until_payday_cents=120_000,
            emergency_buffer_cents=50_000,
            safe_to_invest_cents=50_000,
            payday_cadence_days=14,
            next_payday_date="2026-11-28",
            estimated_paycheck_cents=200_000,
            user_configured_payday=True,
            today=ref,
        )
        assert plan_tight.runway.ruin_probability_pct > 5.0
        assert plan_tight.statistical_safe_to_invest_cents == 0
        assert [a for a in plan_tight.actions if a.action_type == "set_emergency_buffer"] == []
        assert [a for a in plan_tight.actions if a.action_type == "fund_goal"] == []

    def test_advisory_debt_azeo_uses_real_apr_vs_hysa_and_zero_fabricated_deltas(self):
        ref = date(2026, 11, 10)
        engine = OneTapQuantEngine()

        credit_lines = [
            # Card 1: 19.25% real APR (1925 bps -> exact +15.00% spread over 4.25% HYSA), 50% util (>28.9% and >8.9%)
            {
                "id": 10,
                "name": "Sapphire Real APR",
                "kind": "credit_card",
                "balance_cents": 100_000,  # $1,000
                "credit_limit_cents": 200_000,  # $2,000 -> 50.0%
                "apr_bps": 1925,
                "min_payment_cents": 3_500,
                "statement_closing_day": 20,
                "payment_due_day": 15,
                "autopay_mode": "minimum",
            },
            # Card 2: 0.00% APR (unset / 0% promo) -> MUST NOT fabricate 24.99% savings!
            {
                "id": 11,
                "name": "Zero APR Promo Card",
                "kind": "credit_card",
                "balance_cents": 80_000,  # $800
                "credit_limit_cents": 200_000,  # $2,000 -> 40.0%
                "apr_bps": 0,
                "min_payment_cents": 2_500,
                "statement_closing_day": 22,
                "payment_due_day": 18,
                "autopay_mode": "minimum",
            },
            # Card 3: 3.50% APR (< 4.25% HYSA) -> net savings vs HYSA MUST be 0!
            {
                "id": 12,
                "name": "Low Rate Credit Union",
                "kind": "credit_card",
                "balance_cents": 60_000,
                "credit_limit_cents": 200_000,  # 30.0%
                "apr_bps": 350,
                "min_payment_cents": 2_500,
                "statement_closing_day": 25,
                "payment_due_day": 20,
                "autopay_mode": "minimum",
            },
        ]

        plan = engine.build_onetap_plan(
            starting_balance_cents=400_000,
            credit_lines=credit_lines,
            today=ref,
        )
        debt_acts = [a for a in plan.actions if a.action_type == "advisory_debt_azeo"]
        assert len(debt_acts) == 3

        by_id = {a.payload["credit_line_id"]: a for a in debt_acts}

        # Sapphire (zero-out card, full $1,000 paydown at 19.25% vs 4.25% = exact 15.00% -> $150.00/yr)
        sapphire = by_id[10]
        assert sapphire.executable is False
        assert sapphire.is_advisory is True
        assert sapphire.payload["apr_bps"] == 1925
        assert sapphire.payload["hysa_benchmark_bps"] == HYSA_BENCHMARK_BPS == 425
        assert sapphire.payload["net_apr_spread_bps"] == 1500
        assert sapphire.payload["pay_before_close_cents"] == 100_000
        assert sapphire.expected_annual_savings_cents == 15_000  # Exact $150.00/yr!
        assert sapphire.payload["pay_by_date"] == "2026-11-17"  # 3 days before Nov 20 close
        # Verify exact <28.9% and <8.9% thresholds on $2,000 limit
        assert (100_000 - sapphire.payload["pay_to_under_28_9_cents"]) / 200_000 < 0.289
        assert (100_000 - sapphire.payload["pay_to_under_8_9_cents"]) / 200_000 < 0.089

        # Zero APR card: real APR 0 bps -> exact $0.00 savings (NEVER 24.99%!)
        zero_card = by_id[11]
        assert zero_card.payload["apr_bps"] == 0
        assert zero_card.expected_annual_savings_cents == 0
        assert zero_card.payload["resulting_utilization_pct"] < 8.9

        # Low rate card (3.50% < 4.25% HYSA) -> exact $0.00 net savings
        low_card = by_id[12]
        assert low_card.payload["apr_bps"] == 350
        assert low_card.expected_annual_savings_cents == 0

        for act in debt_acts:
            _assert_clean_copy(f"{act.title} {act.summary} {act.rationale} {' '.join(act.safety_checks)}")

    def test_end_to_end_seeded_workspace_and_quant_math_bridge(self):
        svc = FinanceService(db_path=":memory:")
        try:
            ref = date(2026, 11, 20)
            from tests.fixtures.seed_workspace import seed_workspace

            seed_workspace(svc, today=ref)
            budget = svc._compute_budget(today=ref)
            credit_report = svc.get_credit_report(today=ref)
            goals = svc.list_goals(include_completed=False)
            txs, splits_map = svc._load_recent_transactions_with_splits(days=730, today=ref)
            bills = svc.storage.list_recurring_bills(active_only=True)
            settings = svc.storage.get_settings()

            # Verify QuantMathEngine delegation bridge matches OneTapQuantEngine identically
            plan_direct = OneTapQuantEngine().build_onetap_plan(
                budget=budget,
                transactions=txs,
                recurring_bills=bills,
                settings=settings,
                credit_report=credit_report,
                goals=goals,
                splits_by_tx_id=splits_map,
                horizon_days=30,
                seed=42,
                today=ref,
            )
            plan_bridge = QuantMathEngine().build_onetap_plan(
                budget=budget,
                transactions=txs,
                recurring_bills=bills,
                settings=settings,
                credit_report=credit_report,
                goals=goals,
                splits_by_tx_id=splits_map,
                horizon_days=30,
                seed=42,
                today=ref,
            )
            assert plan_direct.model_dump(exclude={"generated_at", "runway", "diagnostics"}) == (
                plan_bridge.model_dump(exclude={"generated_at", "runway", "diagnostics"})
            )

            assert plan_direct.needs_setup is False
            assert plan_direct.allocated_surplus_cents <= plan_direct.available_unlocked_surplus_cents
            assert [a.priority_rank for a in plan_direct.actions] == list(range(1, len(plan_direct.actions) + 1))

            # Full Pydantic JSON round-trip
            serialized = plan_direct.model_dump()
            rehydrated = OneTapPlan.model_validate(serialized)
            assert rehydrated.horizon_days == 30
            assert len(rehydrated.actions) == len(plan_direct.actions)

            # Verify all human-facing strings across entire plan obey RSD-safe & banned-terms contracts
            _assert_clean_copy(plan_direct.summary)
            for act in plan_direct.actions:
                assert isinstance(act.amount_cents, int)
                assert isinstance(act.expected_annual_savings_cents, int)
                _assert_clean_copy(
                    f"{act.title} {act.summary} {act.rationale} {' '.join(act.safety_checks)}"
                )
        finally:
            svc.close()

    def test_source_contract_no_banned_terms_no_numpy_scipy(self):
        import chronox_finance.engines.onetap_quant as mod

        src_path = Path(inspect.getfile(mod))
        source = src_path.read_text(encoding="utf-8")
        assert not _BANNED_TERMS_RE.search(source), "Found banned term in onetap_quant.py source!"
        assert not re.search(r"^\s*(?:import|from)\s+(?:numpy|scipy)\b", source, re.MULTILINE)
        # Also verify CreditDebtOptimizer integration works cleanly with zero fabricated score deltas
        opt = CreditDebtOptimizer()
        cr = opt.analyze(
            raw_lines=[
                {
                    "id": 1,
                    "name": "Visa Test",
                    "kind": "credit_card",
                    "balance_cents": 75_000,
                    "credit_limit_cents": 200_000,
                    "apr_bps": 2099,
                    "min_payment_cents": 3_000,
                    "statement_closing_day": 15,
                    "payment_due_day": 10,
                }
            ],
            smoothed_monthly_income_cents=400_000,
            today=date(2026, 11, 5),
        )
        plan = OneTapQuantEngine().build_onetap_plan(
            starting_balance_cents=300_000,
            credit_report=cr,
            today=date(2026, 11, 5),
        )
        dumped_str = str(plan.model_dump())
        assert not _BANNED_TERMS_RE.search(dumped_str)
        assert not math.isnan(plan.runway.ruin_probability_pct)

    def test_thin_and_medium_history_regimes_all_eight_cadences_and_performance(self):
        ref = date(2026, 11, 1)
        engine = OneTapQuantEngine()

        # Thin history (7 days -> parametric log-normal branch, seasonality inactive)
        thin_txs = [
            {
                "tx_type": "expense",
                "jar_id": 2,
                "amount_cents": 2_000 + d * 400,
                "normalized_merchant": "cafe",
                "tx_date": (ref - timedelta(days=d)).isoformat(),
            }
            for d in range(7)
        ]
        r_thin = engine.compute_statistical_runway(
            starting_balance_cents=250_000,
            transactions=thin_txs,
            horizon_days=30,
            today=ref,
        )
        assert r_thin.sample_days == 7
        assert r_thin.seasonality_active is False
        assert r_thin.p10_ending_cents <= r_thin.p50_ending_cents <= r_thin.p90_ending_cents
        assert r_thin.cvar_95_buffer_cents >= r_thin.var_95_shortfall_cents >= 0

        # Medium history (16 days -> sequential block-bootstrap branch, seasonality inactive)
        med_txs = [
            {
                "tx_type": "expense",
                "jar_id": 2,
                "amount_cents": 1_800 + (d % 5) * 1_100,
                "normalized_merchant": "market",
                "tx_date": (ref - timedelta(days=d)).isoformat(),
            }
            for d in range(16)
        ]
        r_med = engine.compute_statistical_runway(
            starting_balance_cents=250_000,
            transactions=med_txs,
            horizon_days=30,
            n_paths=50,
            today=ref,
        )
        assert r_med.sample_days == 16
        assert r_med.seasonality_active is False
        assert r_med.p10_ending_cents <= r_med.p50_ending_cents <= r_med.p90_ending_cents

        # Verify n_paths=50 lump-sum safety strictly keeps post-withdrawal ruin <= 5.0%
        r_med_post = engine.compute_statistical_runway(
            starting_balance_cents=250_000 - r_med.overdraft_safe_lump_sum_cents,
            transactions=med_txs,
            horizon_days=30,
            n_paths=50,
            today=ref,
        )
        assert r_med_post.ruin_probability_pct <= 5.0

        # All 8 recurring bill cadences + credit minimum + 90-day 500-path speed check
        bills = [
            {"id": 1, "name": "Weekly", "normalized_merchant": "w", "amount_cents": 2_000, "cadence": RecurrenceCadence.WEEKLY.value, "next_due_date": "2026-11-02", "active": 1},
            {"id": 2, "name": "Biweekly", "normalized_merchant": "bw", "amount_cents": 3_000, "cadence": RecurrenceCadence.BIWEEKLY.value, "next_due_date": "2026-11-03", "active": 1},
            {"id": 3, "name": "SemiMonthly", "normalized_merchant": "sm", "amount_cents": 10_000, "cadence": RecurrenceCadence.SEMIMONTHLY.value, "next_due_date": "2026-11-01", "active": 1},
            {"id": 4, "name": "Monthly", "normalized_merchant": "m", "amount_cents": 100_000, "cadence": RecurrenceCadence.MONTHLY.value, "next_due_date": "2026-11-05", "active": 1},
            {"id": 5, "name": "Bimonthly", "normalized_merchant": "bm", "amount_cents": 5_000, "cadence": RecurrenceCadence.BIMONTHLY.value, "next_due_date": "2026-11-10", "active": 1},
            {"id": 6, "name": "Quarterly", "normalized_merchant": "q", "amount_cents": 8_000, "cadence": RecurrenceCadence.QUARTERLY.value, "next_due_date": "2026-11-15", "active": 1},
            {"id": 7, "name": "Semiannual", "normalized_merchant": "sa", "amount_cents": 20_000, "cadence": RecurrenceCadence.SEMIANNUAL.value, "next_due_date": "2026-11-20", "active": 1},
            {"id": 8, "name": "Annual", "normalized_merchant": "an", "amount_cents": 12_000, "cadence": RecurrenceCadence.ANNUAL.value, "next_due_date": "2026-12-01", "active": 1},
        ]
        credit_lines = [
            {
                "id": 1,
                "name": "Card Min",
                "kind": "credit_card",
                "balance_cents": 50_000,
                "credit_limit_cents": 200_000,
                "apr_bps": 1800,
                "min_payment_cents": 3_500,
                "payment_due_day": 12,
                "active": True,
            }
        ]
        long_txs = [
            {
                "tx_type": "expense",
                "jar_id": 2,
                "amount_cents": 2_200 + (d % 7) * 500,
                "normalized_merchant": "daily",
                "tx_date": (ref - timedelta(days=d)).isoformat(),
            }
            for d in range(45)
        ]

        t0 = time.perf_counter()
        r_90 = engine.compute_statistical_runway(
            starting_balance_cents=600_000,
            recurring_bills=bills,
            transactions=long_txs,
            credit_lines=credit_lines,
            payday_cadence_days=14,
            next_payday_date="2026-11-06",
            estimated_paycheck_cents=220_000,
            horizon_days=90,
            n_paths=500,
            today=ref,
        )
        elapsed = time.perf_counter() - t0
        assert elapsed < 1.0, f"500-path 90d runway took too long: {elapsed:.3f}s"
        assert r_90.horizon_days == 90
        assert r_90.scheduled_outflows_cents > 400_000
        assert r_90.scheduled_inflows_cents == 220_000 * 7

    def test_constant_baseline_zero_mad_anomaly_guard(self):
        ref = date(2026, 11, 20)
        engine = OneTapQuantEngine()
        # 5 identical $15.00 charges + 1 tiny $16.00 jitter (must NOT flag) + 1 huge $280 spike (MUST flag)
        txs = [
            {"id": i, "tx_type": "expense", "subcategory": "coffee", "amount_cents": 1_500, "merchant": "Cafe", "tx_date": (ref - timedelta(days=20 + i * 5)).isoformat()}
            for i in range(1, 6)
        ] + [
            {"id": 6, "tx_type": "expense", "subcategory": "coffee", "amount_cents": 1_600, "merchant": "Cafe Jitter", "tx_date": (ref - timedelta(days=5)).isoformat()},
            {"id": 7, "tx_type": "expense", "subcategory": "coffee", "amount_cents": 28_000, "merchant": "Espresso Machine", "tx_date": (ref - timedelta(days=2)).isoformat()},
        ]
        diag = engine.compute_statistical_diagnostics(transactions=txs, today=ref)
        merchants = [a.merchant for a in diag.anomalies]
        assert "Espresso Machine" in merchants
        assert "Cafe Jitter" not in merchants

