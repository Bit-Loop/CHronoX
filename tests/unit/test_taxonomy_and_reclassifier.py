"""
Strict TDD suite for Phase 2:
- Elimination of "Guilt-Free Fun" -> "Dining & Lifestyle" (including DB migration)
- Canonical 16-Category Taxonomy & Plaid `personal_finance_category` (`raw_category` / `subcategory`) mapping
- Zero discretionary pollution on unknown merchants (`Uncategorized` + `needs_review=True`)
- End-to-end Plaid ingestion + retroactive `POST /api/v1/transactions/reclassify` preserving learned rules
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chronox_finance.api import create_app
from chronox_finance.connectors.base import NormalizedTransaction
from chronox_finance.engines.categorizer import TransactionCategorizer, humanize_category_label
from chronox_finance.security import CSRF_HEADER_NAME
from chronox_finance.service import FinanceService
from chronox_finance.storage import DEFAULT_JARS, FinanceStorage


def test_guilt_free_fun_eliminated_and_migrated_in_storage(tmp_path: Path):
    jar_names = [j["name"] for j in DEFAULT_JARS]
    assert "Guilt-Free Fun" not in jar_names
    fun_jar = next(j for j in DEFAULT_JARS if j["slug"] == "fun")
    assert fun_jar["name"] == "Dining & Lifestyle"

    # Simulate a legacy database that still has "Guilt-Free Fun" stored in `jars`
    db_file = tmp_path / "legacy_jars.sqlite3"
    st1 = FinanceStorage(db_file)
    with st1.transaction(immediate=True) as conn:
        conn.execute("UPDATE jars SET name = 'Guilt-Free Fun' WHERE slug = 'fun'")
        conn.execute("PRAGMA user_version = 2")
    st1.close()

    # Re-opening FinanceStorage runs migrations and upgrades "Guilt-Free Fun" -> "Dining & Lifestyle"
    st2 = FinanceStorage(db_file)
    try:
        upgraded = {j["slug"]: j["name"] for j in st2.list_jars()}
        assert upgraded["fun"] == "Dining & Lifestyle"
        assert "Guilt-Free Fun" not in upgraded.values()
    finally:
        st2.close()


@pytest.mark.parametrize(
    ("merchant", "pfc_subcat", "expected_jar_slug", "expected_category"),
    [
        ("CarMax", "loan payments car payment", "bills", "Auto & Loan Payments"),
        ("U.S. Department of Education", "loan payments student loan payment", "bills", "Auto & Loan Payments"),
        ("Brigit", "loan payments cash advances", "bills", "Cash Advances & BNPL"),
        ("AfterPay", "loan payments bnpl", "bills", "Cash Advances & BNPL"),
        ("INTEREST CHARGE-CASH ADV", "bank fees interest charge", "bills", "Interest & Bank Fees"),
        ("INTL TRANSACTION FEE", "bank fees foreign transaction fees", "bills", "Interest & Bank Fees"),
        ("USAA P C AUTOPAY", "general services insurance", "bills", "Insurance"),
        ("AT&T", "rent and utilities telephone", "bills", "Phone & Internet"),
        ("Spectrum", "rent and utilities internet and cable", "bills", "Phone & Internet"),
        ("Extra Space Storage", "general services storage", "bills", "Storage & Subscriptions"),
        ("Anthropic", "general services other general services", "bills", "Software, AI & Services"),
        ("Aafes", "transportation gas", "daily", "Gas, Auto & Transit"),
        ("Valvoline Instant Oil Change", "general services automotive", "daily", "Gas, Auto & Transit"),
        ("5 Star Nutrition", "medical pharmacies and supplements", "daily", "Medical & Pharmacy"),
        ("Freedom Urgent Care", "medical primary care", "daily", "Medical & Pharmacy"),
        ("Brookshire's Food", "food and drink groceries", "daily", "Groceries & Superstores"),
        ("Target", "general merchandise superstores", "daily", "Groceries & Superstores"),
        ("Antojitos", "food and drink restaurant", "fun", "Dining & Restaurants"),
        ("Black Rifle Coffee", "food and drink coffee", "fun", "Fast Food & Coffee"),
        ("Chick-fil-A", "food and drink fast food", "fun", "Fast Food & Coffee"),
        ("Aafes", "food and drink beer wine and liquor", "fun", "Bars, Alcohol & Smoke"),
        ("24 Stop Smoke Shop", "general merchandise tobacco and vape", "fun", "Bars, Alcohol & Smoke"),
        ("7-Eleven", "general merchandise convenience stores", "fun", "Convenience & Snacks"),
        ("365 Retail Markets", "food and drink vending machines", "fun", "Convenience & Snacks"),
        ("Amazon", "general merchandise online marketplaces", "fun", "Shopping & Retail"),
        ("Aafes", "general merchandise department stores", "fun", "Shopping & Retail"),
        ("Aafes", "general merchandise clothing and accessories", "fun", "Clothing & Apparel"),
        ("Microsoft", "entertainment video games", "fun", "Entertainment & Gaming"),
        ("Bowlero", "entertainment sporting events amusement parks and museums", "fun", "Entertainment & Gaming"),
    ],
)
def test_plaid_pfc_and_merchant_taxonomy_mapping(
    merchant: str,
    pfc_subcat: str,
    expected_jar_slug: str,
    expected_category: str,
):
    jars_by_slug = {
        "bills": {"id": 1, "slug": "bills"},
        "daily": {"id": 2, "slug": "daily"},
        "fun": {"id": 3, "slug": "fun"},
        "safety": {"id": 4, "slug": "safety"},
        "invest": {"id": 5, "slug": "invest"},
    }
    cat = TransactionCategorizer()
    jar_id, clean_cat, conf, needs_rev = cat.categorize(
        merchant,
        "",
        jars_by_slug,
        {},
        raw_category=pfc_subcat,
    )
    assert jar_id == jars_by_slug[expected_jar_slug]["id"]
    assert clean_cat == expected_category
    assert conf >= 0.80
    assert needs_rev is False


def test_humanize_category_label_cleans_legacy_plaid_strings():
    assert humanize_category_label("loan payments car payment") == "Auto & Loan Payments"
    assert humanize_category_label("bank fees interest charge") == "Interest & Bank Fees"
    assert humanize_category_label("general merchandise convenience stores") == "Convenience & Snacks"
    assert humanize_category_label("Uncategorized") == "Uncategorized"


def test_unknown_merchant_does_not_pollute_fun_jar():
    jars_by_slug = {
        "bills": {"id": 1, "slug": "bills"},
        "daily": {"id": 2, "slug": "daily"},
        "fun": {"id": 3, "slug": "fun"},
    }
    cat = TransactionCategorizer()
    jar_id, clean_cat, conf, needs_rev = cat.categorize("OPAQUE CORP #8821", "", jars_by_slug, {})
    assert jar_id != jars_by_slug["fun"]["id"]
    assert clean_cat == "Uncategorized"
    assert conf == pytest.approx(0.45)
    assert needs_rev is True


def test_plaid_ingest_and_retroactive_reclassify_endpoint(tmp_path: Path):
    db_file = tmp_path / "reclassify.sqlite3"
    sec_file = tmp_path / "secrets.enc"
    svc = FinanceService(db_path=db_file, secret_store_path=sec_file, kdf_iterations=20_000)
    try:
        app = create_app(db_path=db_file, service=svc, enforce_vault_lock=True)
        client = TestClient(app)
        setup_r = client.post("/api/v1/auth/setup", json={"password": "TaxonomyTDD!2026"})
        csrf = setup_r.json()["csrf_token"]
        headers = {CSRF_HEADER_NAME: csrf}

        jars = {j["slug"]: j["id"] for j in svc.storage.list_jars()}
        acct_id = svc.storage.get_default_account_id()

        # 1. New Plaid-synced CarMax transaction must land directly in `bills` + "Auto & Loan Payments"
        norm_tx = NormalizedTransaction(
            external_id="plaid_carmax_1",
            external_account_id="ext_chk",
            tx_date="2026-11-16",
            merchant="CarMax",
            amount_cents=48_500,
            tx_type="expense",
            subcategory="loan payments car payment",
            raw_category="LOAN_PAYMENTS_CAR_PAYMENT",
            source="plaid",
        )
        svc._ingest_normalized_transactions(
            acct_map={"ext_chk": acct_id},
            transactions=[norm_tx],
            source_tag="plaid_sync",
        )
        ingested = [t for t in svc.storage.list_transactions() if t["merchant"] == "CarMax"][0]
        assert ingested["jar_id"] == jars["bills"]
        assert ingested["subcategory"] == "Auto & Loan Payments"

        # 2. Seed legacy misclassified rows (simulating old DB state where Brigit/Dept of Ed landed in `fun`)
        bad_ed = svc.storage.insert_transaction(
            account_id=acct_id,
            jar_id=jars["fun"],
            tx_type="expense",
            amount_cents=8_166,
            merchant="U.S. Department of Education",
            normalized_merchant="u s department of education",
            subcategory="loan payments student loan payment",
            memo="",
            tx_date="2026-11-20",
            confidence=0.45,
            needs_review=True,
            source="plaid_sync",
        )
        bad_brigit = svc.storage.insert_transaction(
            account_id=acct_id,
            jar_id=jars["fun"],
            tx_type="expense",
            amount_cents=10_000,
            merchant="Brigit",
            normalized_merchant="brigit",
            subcategory="loan payments cash advances",
            memo="",
            tx_date="2026-11-18",
            confidence=0.45,
            needs_review=True,
            source="plaid_sync",
        )
        # Seed a user-learned rule that MUST be preserved
        svc.storage.upsert_merchant_rule("custom local shop", jars["daily"], "Groceries & Superstores")
        learned_tx = svc.storage.insert_transaction(
            account_id=acct_id,
            jar_id=jars["daily"],
            tx_type="expense",
            amount_cents=2_500,
            merchant="Custom Local Shop",
            normalized_merchant="custom local shop",
            subcategory="Groceries & Superstores",
            memo="",
            tx_date="2026-11-19",
            confidence=0.99,
            needs_review=False,
            source="manual",
        )

        # 3. Call POST /api/v1/transactions/reclassify
        rec_r = client.post("/api/v1/transactions/reclassify", json={}, headers=headers)
        assert rec_r.status_code == 200
        body = rec_r.json()
        assert body["reclassified_count"] >= 2

        by_id = {t["id"]: t for t in svc.storage.list_transactions()}
        assert by_id[bad_ed["id"]]["jar_id"] == jars["bills"]
        assert by_id[bad_ed["id"]]["subcategory"] == "Auto & Loan Payments"
        assert by_id[bad_brigit["id"]]["jar_id"] == jars["bills"]
        assert by_id[bad_brigit["id"]]["subcategory"] == "Cash Advances & BNPL"
        assert by_id[learned_tx["id"]]["jar_id"] == jars["daily"]
        assert by_id[learned_tx["id"]]["subcategory"] == "Groceries & Superstores"

        # 4. Second call MUST be strictly idempotent (0 rows mutated)
        rec_r2 = client.post("/api/v1/transactions/reclassify", json={}, headers=headers)
        assert rec_r2.status_code == 200
        assert rec_r2.json()["reclassified_count"] == 0
    finally:
        svc.close()
