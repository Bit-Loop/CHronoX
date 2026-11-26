"""
Strict TDD suite for Overnight /goal End-to-End Mission Roadmap & iOS Titanium UI:
- Exact day-by-day Average Daily Balance (ADB) Bi-Weekly (26x/yr) vs. Monthly (12x/yr) credit card math
- Bi-weekly collection payment plan ($75/2wk -> $0 -> automatic cashflow cascade into card paydown)
- Optimal 3-tier credit card payment recommendations (Safe Floor | Optimal Bi-Weekly | Aggressive Sprint)
- One-time splurge / birthday anomaly exclusion from steady-state monthly spend baseline
- 6-Stage Branching 700+ Credit & Staged Wishlist Roadmap (`balanced_mission`, `credit_sprint`, `gear_upfront`)
- Strict zero-hallucination audit hygiene (zero fabricated `pts`, `What-If`, or `Simulator` strings)
- End-to-end API (`/api/v1/credit/roadmap` & `/api/v1/credit/report`) + iOS Titanium masculine UI contracts
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from chronox_finance.api import create_app
from chronox_finance.engines.credit_debt import (
    build_credit_and_wishlist_roadmap,
    simulate_biweekly_vs_monthly_payoff,
)
from chronox_finance.security import CSRF_HEADER_NAME
from chronox_finance.service import FinanceService


def test_biweekly_vs_monthly_adb_math_saves_interest_and_months():
    ref = date(2026, 11, 26)
    res = simulate_biweekly_vs_monthly_payoff(
        balance_cents=700_000,
        apr_bps=1800,
        monthly_payment_cents=36_000,
        today=ref,
    )
    assert res["amortizes"] is True
    assert res["monthly_payoff_months"] is not None
    assert res["biweekly_payoff_months"] is not None
    # 26 half-payments/yr (= 13 monthly eq.) + lower Average Daily Balance MUST beat monthly on BOTH interest and time
    assert res["biweekly_total_interest_cents"] < res["monthly_total_interest_cents"]
    assert res["biweekly_interest_saved_cents"] == (
        res["monthly_total_interest_cents"] - res["biweekly_total_interest_cents"]
    )
    assert res["biweekly_interest_saved_cents"] > 10_000  # > $100 real interest saved on $7k @ 18%
    assert res["biweekly_payoff_months"] < res["monthly_payoff_months"]
    assert res["biweekly_months_saved"] >= 2

    # Non-amortizing guard ($50/mo < $105/mo interest on $7k @ 18%) MUST never infinite loop
    non_am = simulate_biweekly_vs_monthly_payoff(
        balance_cents=700_000,
        apr_bps=1800,
        monthly_payment_cents=5_000,
        today=ref,
    )
    assert non_am["amortizes"] is False
    assert non_am["monthly_payoff_months"] is None
    assert non_am["biweekly_payoff_months"] is None


def test_branching_700_roadmap_collection_cascade_splurge_filter_and_wishlist_unlocks():
    ref = date(2026, 11, 26)
    credit_lines = [
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
            "id": 2,
            "name": "Apex Auto Finance",
            "kind": "auto_loan",
            "balance_cents": 1_250_000,
            "credit_limit_cents": 0,
            "apr_bps": 790,
            "min_payment_cents": 48_500,
            "autopay_mode": "minimum",
            "active": 1,
        },
        {
            "id": 5,
            "name": "Metro Wireless (Past Due)",
            "kind": "personal_loan",
            "balance_cents": 18_700,
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
            "balance_cents": 75_500,
            "credit_limit_cents": 0,
            "apr_bps": 0,
            "min_payment_cents": 16_250,
            "autopay_mode": "minimum",
            "active": 1,
        },
    ]
    recurring_bills = [
        {"id": 1, "name": "Summit Cable Collection Plan", "amount_cents": 7_500, "cadence": "biweekly", "next_due_date": "2026-11-27", "active": 1, "is_essential": 1},
        {"id": 2, "name": "Phone Service", "amount_cents": 14_500, "cadence": "monthly", "next_due_date": "2026-12-02", "active": 1, "is_essential": 1},
        {"id": 3, "name": "Storage Unit", "amount_cents": 8_600, "cadence": "monthly", "next_due_date": "2026-12-02", "active": 1, "is_essential": 1},
    ]
    goals = [
        {"id": 1, "name": "EWA Exit Stabilization Pool", "target_cents": 64_114, "saved_cents": 0, "category": "emergency", "priority": 1, "priority_order": 1, "status": "active", "deleted_at": None},
        {"id": 2, "name": "FPV Drone Controller & Sim", "target_cents": 30_000, "saved_cents": 0, "category": "gadget", "priority": 1, "priority_order": 2, "status": "active", "deleted_at": None},
        {"id": 3, "name": "FPV Starter Drone", "target_cents": 10_000, "saved_cents": 0, "category": "gadget", "priority": 2, "priority_order": 3, "status": "active", "deleted_at": None},
        {"id": 4, "name": "FPV Drone #2 & Visor", "target_cents": 10_000, "saved_cents": 0, "category": "gadget", "priority": 3, "priority_order": 4, "status": "active", "deleted_at": None},
    ]
    credit_reports = [
        {"id": 1, "bureau": "experian", "model": "fico8", "score": 615, "pulled_date": "2026-11-26"},
        {"id": 2, "bureau": "transunion", "model": "vantage3", "score": 575, "pulled_date": "2026-11-26"},
    ]
    derogs = [
        {"id": 1, "kind": "bankruptcy_ch7", "lender": "US BKPT CT SAMPLE DIST", "opened_date": "2020-04-15", "drops_off": "2030-04-15", "active": True},
        {"id": 2, "kind": "collection", "lender": "Summit Recovery Agency (Orig: Cable Co.)", "opened_date": "2026-11-21", "drops_off": "2033-11-21", "active": True},
        {"id": 3, "kind": "late_30", "lender": "Metro Wireless", "opened_date": "2026-11-02", "drops_off": "2033-11-02", "active": True},
    ]
    # Include a $450 Nov 19 birthday splurge tagged "one-time" + real EWA open advance ($300)
    txs = [
        {"id": 1, "tx_type": "income", "amount_cents": 147_000, "merchant": "DFAS PAYROLL", "tx_date": "2026-11-10"},
        {"id": 2, "tx_type": "income", "amount_cents": 147_000, "merchant": "DFAS PAYROLL", "tx_date": "2026-10-28"},
        {"id": 3, "tx_type": "expense", "amount_cents": 45_000, "merchant": "Birthday Celebration", "subcategory": "Dining & Restaurants", "tx_date": "2026-11-19", "tags": [{"name": "one-time"}, {"name": "birthday"}]},
        {"id": 4, "tx_type": "expense", "amount_cents": 40_000, "merchant": "Dave", "subcategory": "Cash Advances & BNPL", "tx_date": "2026-10-30"},
        {"id": 5, "tx_type": "income", "amount_cents": 30_000, "merchant": "Dave", "subcategory": "Income", "tx_date": "2026-11-20"},
        {"id": 6, "tx_type": "expense", "amount_cents": 10_500, "merchant": "INTEREST CHARGE-PURCHASES", "subcategory": "Interest & Bank Fees", "tx_date": "2026-11-11"},
    ]

    roadmap = build_credit_and_wishlist_roadmap(
        credit_lines=credit_lines,
        transactions=txs,
        recurring_bills=recurring_bills,
        goals=goals,
        credit_reports=credit_reports,
        derogatories=derogs,
        smoothed_monthly_income_cents=294_000,
        payday_cadence_days=14,
        selected_branch="balanced_mission",
        today=ref,
    )

    # 1. One-time birthday splurge excluded from baseline burn
    assert roadmap.splurge_excluded_cents == 45_000
    assert roadmap.splurge_tx_count == 1

    # 2. Bi-weekly collection plan ($75 bi-weekly on $755 -> 11 payments -> frees $162.50/mo)
    assert roadmap.collection_plan is not None
    assert roadmap.collection_plan.biweekly_payment_cents == 7_500
    assert roadmap.collection_plan.payments_remaining == 11
    assert roadmap.collection_plan.freed_monthly_cents == 16_250

    # 3. 3 ordered optimal credit card payment tiers with exact bi-weekly vs monthly savings
    tiers = {t.tier_id: t for t in roadmap.optimal_card_plan.tiers}
    assert set(tiers.keys()) == {"safe_floor", "optimal_biweekly", "aggressive_sprint"}
    assert tiers["safe_floor"].biweekly_payment_cents < tiers["optimal_biweekly"].biweekly_payment_cents
    assert tiers["optimal_biweekly"].biweekly_payment_cents <= tiers["aggressive_sprint"].biweekly_payment_cents
    assert tiers["optimal_biweekly"].biweekly_interest_saved_cents > 0
    assert tiers["optimal_biweekly"].recommended is True

    # 4. 3 branching paths over time with exact economic ordering
    branches = {b.branch_id: b for b in roadmap.branches}
    assert set(branches.keys()) == {"balanced_mission", "credit_sprint", "gear_upfront"}
    assert branches["credit_sprint"].total_revolving_interest_cents <= branches["balanced_mission"].total_revolving_interest_cents
    assert branches["balanced_mission"].total_revolving_interest_cents <= branches["gear_upfront"].total_revolving_interest_cents
    assert len(roadmap.stages) >= 5
    assert any("700+" in st.score_band_note for st in roadmap.stages)

    # 5. Staged wishlist unlocks: Stabilization Pool -> Controller & Sim ($300) -> Drone ($100) -> Visor ($100)
    unlocks = {u.goal_name: u for u in roadmap.wishlist_unlocks}
    assert "EWA Exit Stabilization Pool" in unlocks
    assert "FPV Drone Controller & Sim" in unlocks
    assert "FPV Starter Drone" in unlocks
    assert "FPV Drone #2 & Visor" in unlocks
    assert unlocks["EWA Exit Stabilization Pool"].unlock_date <= unlocks["FPV Drone Controller & Sim"].unlock_date
    assert unlocks["FPV Drone Controller & Sim"].unlock_date <= unlocks["FPV Starter Drone"].unlock_date
    assert unlocks["FPV Starter Drone"].unlock_date <= unlocks["FPV Drone #2 & Visor"].unlock_date

    # 6. Zero fabricated score points / simulator / what-if hygiene compliance
    blob = json.dumps(roadmap.model_dump())
    assert not re.search(r"\bpts?\b", blob, re.I), f"Fabricated pts string leaked: {blob}"
    assert not re.search(r"\b\d+[–-]\d+\s*points?\b", blob, re.I)
    assert "WHAT-IF" not in blob.upper()
    assert "SIMULATOR" not in blob.upper()


def test_roadmap_api_endpoints_and_ios_titanium_ui_contracts(tmp_path: Path):
    db_file = tmp_path / "roadmap_api.sqlite3"
    sec_file = tmp_path / "secrets.enc"
    svc = FinanceService(db_path=db_file, secret_store_path=sec_file, kdf_iterations=20_000)
    try:
        app = create_app(db_path=db_file, service=svc, enforce_vault_lock=True)
        client = TestClient(app)
        setup_r = client.post("/api/v1/auth/setup", json={"password": "RoadmapTDD!2026"})
        csrf = setup_r.json()["csrf_token"]
        headers = {CSRF_HEADER_NAME: csrf}

        client.post(
            "/api/v1/credit/lines",
            json={
                "name": "Sample Rewards Card",
                "kind": "credit_card",
                "balance_cents": 500_000,
                "credit_limit_cents": 520_000,
                "apr_bps": 1800,
                "min_payment_cents": 10_000,
                "statement_closing_day": 11,
                "payment_due_day": 8,
                "has_grace_period": False,
                "autopay_mode": "minimum",
            },
            headers=headers,
        )
        client.post(
            "/api/v1/goals",
            json={"name": "FPV Drone Controller & Sim", "target_cents": 30_000, "saved_cents": 0, "category": "gadget", "priority": 1},
            headers=headers,
        )
        client.post(
            "/api/v1/bills",
            json={
                "name": "Cable Collection Plan",
                "amount_cents": 7_500,
                "cadence": "biweekly",
                "next_due_date": (date.today() + timedelta(days=3)).isoformat(),
                "is_essential": True,
            },
            headers=headers,
        )

        # 1. Embedded in /api/v1/credit/report
        rep_r = client.get("/api/v1/credit/report")
        assert rep_r.status_code == 200
        rep = rep_r.json()
        assert rep["mission_roadmap"] is not None
        assert len(rep["mission_roadmap"]["branches"]) == 3

        # 2. Dedicated /api/v1/credit/roadmap supports branch switching
        rm_sprint = client.get("/api/v1/credit/roadmap?branch=credit_sprint")
        assert rm_sprint.status_code == 200
        d_sprint = rm_sprint.json()
        assert d_sprint["selected_branch"] == "credit_sprint"
        assert len(d_sprint["optimal_card_plan"]["tiers"]) == 3
        assert len(d_sprint["wishlist_unlocks"]) >= 1

        # 3. 1-click One-Time splurge tagging via PUT /api/v1/transactions/{id}/tags excludes splurge from steady baseline
        tx_r = client.post(
            "/api/v1/transactions",
            json={
                "tx_type": "expense",
                "amount_cents": 42_000,
                "merchant": "Birthday Celebration Dinner",
                "tx_date": date.today().isoformat(),
            },
            headers=headers,
        )
        assert tx_r.status_code == 200
        tx_id = tx_r.json()["id"]
        tag_r = client.put(
            f"/api/v1/transactions/{tx_id}/tags",
            json={"tags": ["one-time", "birthday"]},
            headers=headers,
        )
        assert tag_r.status_code == 200
        rm_after_tag = client.get("/api/v1/credit/roadmap?branch=balanced_mission").json()
        assert rm_after_tag["splurge_excluded_cents"] == 42_000
        assert rm_after_tag["splurge_tx_count"] == 1
    finally:
        svc.close()

    # 4. UI & iOS Titanium masculine design system contracts
    repo_root = Path(__file__).resolve().parents[2]
    index_html = (repo_root / "chronox_finance" / "web" / "index.html").read_text(encoding="utf-8")
    portfolio_js = (repo_root / "chronox_finance" / "web" / "js" / "portfolio.js").read_text(encoding="utf-8")
    charts_js = (repo_root / "chronox_finance" / "web" / "js" / "charts.js").read_text(encoding="utf-8")
    styles_css = (repo_root / "chronox_finance" / "web" / "styles.css").read_text(encoding="utf-8")

    assert 'id="credit-roadmap-card"' in index_html
    assert 'id="credit-roadmap-wrap"' in index_html
    assert 'id="credit-branch-switcher"' in index_html
    assert "renderMissionRoadmap" in portfolio_js
    assert "/api/v1/credit/roadmap" in portfolio_js
    assert "buildBranchingTrajectorySvg" in portfolio_js
    assert "/tags" in charts_js
    # iOS Titanium masculine radii (upgraded from 2px boxy terminal corners!)
    assert "--radius: 16px;" in styles_css
    assert "backdrop-filter: blur(" in styles_css
