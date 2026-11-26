"""
Strict TDD suite for Phase 3 (Cash Flow Tab Overhaul):
- `group_by="category"` (humanized canonical categories, up to 10 chart slices + "Others")
- `group_by="merchant"` (top merchant breakdown) and `group_by="jar"` (5-jar envelope view)
- `financial_drag_cents` (`Interest & Bank Fees` + `Cash Advances & BNPL`)
- Full `category_breakdown` drilldown payload (`tx_count`, `avg_monthly_cents`, `is_financial_drag`,
  `top_merchants`, `sample_transactions`) with exact integer cent conservation across KPIs, Sankey,
  Donut, and Breakdown table.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from chronox_finance.api import create_app
from chronox_finance.engines.analytics import AnalyticsEngine
from chronox_finance.security import CSRF_HEADER_NAME
from chronox_finance.service import FinanceService


def test_category_mode_groups_up_to_10_slices_and_tracks_financial_drag():
    ref = date(2026, 11, 26)
    jars = [
        {"id": 1, "slug": "bills", "name": "Bills & Essentials", "color": "#60a5fa"},
        {"id": 2, "slug": "daily", "name": "Daily Living", "color": "#34d399"},
        {"id": 3, "slug": "fun", "name": "Dining & Lifestyle", "color": "#fbbf24"},
    ]
    # 12 distinct categories (including raw Plaid strings to prove automatic humanization)
    txs = [
        {"id": 100, "tx_type": "income", "amount_cents": 600_000, "merchant": "DFAS", "subcategory": "Military / Federal Pay", "tx_date": "2026-11-01"},
        {"id": 1, "tx_type": "expense", "amount_cents": 97_000, "jar_id": 1, "merchant": "CarMax", "normalized_merchant": "carmax", "subcategory": "loan payments car payment", "tx_date": "2026-11-16"},
        {"id": 2, "tx_type": "expense", "amount_cents": 65_000, "jar_id": 1, "merchant": "USAA", "normalized_merchant": "usaa", "subcategory": "Insurance", "tx_date": "2026-11-05"},
        {"id": 3, "tx_type": "expense", "amount_cents": 45_000, "jar_id": 1, "merchant": "Brigit", "normalized_merchant": "brigit", "subcategory": "Cash Advances & BNPL", "tx_date": "2026-11-08"},
        {"id": 4, "tx_type": "expense", "amount_cents": 42_000, "jar_id": 3, "merchant": "Amazon", "normalized_merchant": "amazon", "subcategory": "Shopping & Retail", "tx_date": "2026-11-09"},
        {"id": 5, "tx_type": "expense", "amount_cents": 38_000, "jar_id": 2, "merchant": "Aafes Gas", "normalized_merchant": "aafes gas", "subcategory": "transportation gas", "tx_date": "2026-11-10"},
        {"id": 6, "tx_type": "expense", "amount_cents": 32_000, "jar_id": 2, "merchant": "Brookshire's", "normalized_merchant": "brookshires", "subcategory": "Groceries & Superstores", "tx_date": "2026-11-11"},
        {"id": 7, "tx_type": "expense", "amount_cents": 28_000, "jar_id": 3, "merchant": "Antojitos", "normalized_merchant": "antojitos", "subcategory": "Dining & Restaurants", "tx_date": "2026-11-12"},
        {"id": 8, "tx_type": "expense", "amount_cents": 24_000, "jar_id": 1, "merchant": "AT&T", "normalized_merchant": "at t", "subcategory": "Phone & Internet", "tx_date": "2026-11-13"},
        {"id": 9, "tx_type": "expense", "amount_cents": 19_000, "jar_id": 2, "merchant": "5 Star Nutrition", "normalized_merchant": "5 star nutrition", "subcategory": "Medical & Pharmacy", "tx_date": "2026-11-14"},
        {"id": 10, "tx_type": "expense", "amount_cents": 15_000, "jar_id": 3, "merchant": "7-Eleven", "normalized_merchant": "7 eleven", "subcategory": "Convenience & Snacks", "tx_date": "2026-11-15"},
        {"id": 11, "tx_type": "expense", "amount_cents": 10_000, "jar_id": 1, "merchant": "INTEREST CHARGE", "normalized_merchant": "interest charge", "subcategory": "bank fees interest charge", "tx_date": "2026-11-17"},
        {"id": 12, "tx_type": "expense", "amount_cents": 6_000, "jar_id": 3, "merchant": "Black Rifle Coffee", "normalized_merchant": "black rifle coffee", "subcategory": "Fast Food & Coffee", "tx_date": "2026-11-18"},
    ]

    overview = AnalyticsEngine().build_overview(
        transactions=txs,
        jars=jars,
        accounts=[{"id": 1, "account_type": "checking", "balance_cents": 300_000, "is_liquid": 1}],
        period="2026-11",
        group_by="category",
        today=ref,
    )

    assert overview.group_by == "category"
    assert overview.total_expense_cents == 421_000
    assert overview.financial_drag_cents == 55_000  # $450 Cash Advances & BNPL + $100 Interest & Bank Fees
    assert len(overview.top_5_expenses) == 5

    # Up to 10 distinct category slices + "Others" rollup (#11 + #12 = 10,000 + 6,000 = 16,000)
    assert len(overview.expense_distribution) == 11
    dist_by_label = {item.label: item for item in overview.expense_distribution}
    assert "Auto & Loan Payments" in dist_by_label
    assert "Gas, Auto & Transit" in dist_by_label
    assert dist_by_label["Others"].amount_cents == 16_000
    assert sum(item.amount_cents for item in overview.expense_distribution) == overview.total_expense_cents

    # Full category breakdown contains all 12 categories with zero truncation and exact cent conservation
    assert len(overview.category_breakdown) == 12
    assert sum(item.amount_cents for item in overview.category_breakdown) == overview.total_expense_cents

    drag_items = {item.label: item for item in overview.category_breakdown if item.is_financial_drag}
    assert set(drag_items.keys()) == {"Cash Advances & BNPL", "Interest & Bank Fees"}
    assert drag_items["Cash Advances & BNPL"].top_merchants[0].merchant == "Brigit"
    assert drag_items["Cash Advances & BNPL"].sample_transactions[0].id == 3

    # Sankey conservation & color parity across all 10 + Others nodes
    in_links = [lk for lk in overview.sankey_links if lk.target == "hub:total_income"]
    out_links = [lk for lk in overview.sankey_links if lk.source == "hub:total_income"]
    assert sum(lk.amount_cents for lk in in_links) == 600_000
    assert sum(lk.amount_cents for lk in out_links) == 600_000

    sankey_col2_colors = {n.label: n.color for n in overview.sankey_nodes if n.column == 2}
    for item in overview.expense_distribution:
        assert sankey_col2_colors[item.label] == item.color


def test_merchant_mode_and_api_drilldown_recategorization(tmp_path: Path):
    db_file = tmp_path / "cashflow_api.sqlite3"
    sec_file = tmp_path / "secrets.enc"
    svc = FinanceService(db_path=db_file, secret_store_path=sec_file, kdf_iterations=20_000)
    try:
        app = create_app(db_path=db_file, service=svc, enforce_vault_lock=True)
        client = TestClient(app)
        setup_r = client.post("/api/v1/auth/setup", json={"password": "CashFlowTDD!2026"})
        csrf = setup_r.json()["csrf_token"]
        headers = {CSRF_HEADER_NAME: csrf}

        today_iso = date.today().isoformat()
        client.post(
            "/api/v1/transactions",
            json={"tx_type": "income", "amount_cents": 400_000, "merchant": "DFAS Payroll", "tx_date": today_iso},
            headers=headers,
        )
        tx1 = client.post(
            "/api/v1/transactions",
            json={"tx_type": "expense", "amount_cents": 48_500, "merchant": "CarMax", "tx_date": today_iso},
            headers=headers,
        ).json()
        tx2 = client.post(
            "/api/v1/transactions",
            json={"tx_type": "expense", "amount_cents": 7_500, "merchant": "Mystery Bodega", "tx_date": today_iso},
            headers=headers,
        ).json()

        # 1. Category view
        r_cat = client.get("/api/v1/analytics/overview?period=30d&group_by=category")
        assert r_cat.status_code == 200
        d_cat = r_cat.json()
        assert d_cat["group_by"] == "category"
        cat_labels = {c["label"] for c in d_cat["category_breakdown"]}
        assert "Auto & Loan Payments" in cat_labels
        assert "Uncategorized" in cat_labels

        # 2. Merchant view
        r_merch = client.get("/api/v1/analytics/overview?period=30d&group_by=merchant")
        assert r_merch.status_code == 200
        d_merch = r_merch.json()
        assert d_merch["group_by"] == "merchant"
        merch_labels = {c["label"] for c in d_merch["category_breakdown"]}
        assert "CarMax" in merch_labels
        assert "Mystery Bodega" in merch_labels
        assert tx1["id"] in [t["id"] for c in d_merch["category_breakdown"] for t in c["sample_transactions"]]

        # 3. Drilldown 1-click recategorization moves "Mystery Bodega" out of Uncategorized
        jars = {j["slug"]: j["id"] for j in svc.storage.list_jars()}
        patch_r = client.patch(
            f"/api/v1/transactions/{tx2['id']}",
            json={
                "jar_id": jars["daily"],
                "subcategory": "Groceries & Superstores",
                "learn_merchant_rule": True,
            },
            headers=headers,
        )
        assert patch_r.status_code == 200

        r_cat_after = client.get("/api/v1/analytics/overview?period=30d&group_by=category").json()
        after_labels = {c["label"] for c in r_cat_after["category_breakdown"]}
        assert "Groceries & Superstores" in after_labels
        assert "Uncategorized" not in after_labels
    finally:
        svc.close()


def test_cashflow_frontend_category_switcher_and_drilldown_wiring():
    repo_root = Path(__file__).resolve().parents[2]
    index_html = (repo_root / "chronox_finance" / "web" / "index.html").read_text(encoding="utf-8")
    charts_js = (repo_root / "chronox_finance" / "web" / "js" / "charts.js").read_text(encoding="utf-8")

    # Toolbar exposes Categories | Envelopes | Merchants switcher + Reclassify Ledger action
    assert 'id="analytics-groupby-switcher"' in index_html
    assert 'data-analytics-groupby="category"' in index_html
    assert 'data-analytics-groupby="jar"' in index_html
    assert 'data-analytics-groupby="merchant"' in index_html
    assert 'id="btn-analytics-reclassify"' in index_html
    assert 'id="analytics-breakdown-card"' in index_html
    assert 'id="analytics-breakdown-body"' in index_html

    # charts.js defaults to group_by="category", passes group_by to API, renders breakdown & 1-click recategorize
    assert 'groupBy: "category"' in charts_js
    assert 'params.set("group_by"' in charts_js
    assert "/api/v1/transactions/reclassify" in charts_js
    assert "renderCategoryBreakdown" in charts_js
    assert "learn_merchant_rule: true" in charts_js

