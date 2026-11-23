"""
Tests for Optional Navy Federal Credit Union (Zero-3rd-Party CSV & Local PDF)
and Official Plaid JSON Interoperability + Cross-Source Deduplication.
"""

from __future__ import annotations

import json
import zlib

from fastapi.testclient import TestClient

from chronox_finance.api import create_app
from chronox_finance.engines.importer import ingest_statement
from chronox_finance.service import FinanceService


def _build_minimal_pdf(lines: list[str], *, compress: bool = True) -> bytes:
    """Build a valid minimal PDF containing text lines (FlateDecode or raw)."""
    ops = ["BT", "/F1 10 Tf", "72 720 Td"]
    for idx, line in enumerate(lines):
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        if idx > 0:
            ops.append("0 -14 Td")
        ops.append(f"({escaped}) Tj")
    ops.append("ET")
    stream_bytes = "\n".join(ops).encode("latin-1")
    if compress:
        encoded = zlib.compress(stream_bytes)
        stream_dict = f"<< /Length {len(encoded)} /Filter /FlateDecode >>"
    else:
        encoded = stream_bytes
        stream_dict = f"<< /Length {len(encoded)} >>"

    objs = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>\nendobj\n",
        b"4 0 obj\n" + stream_dict.encode("ascii") + b"\nstream\n" + encoded + b"\nendstream\nendobj\n",
    ]
    out = bytearray(b"%PDF-1.4\n")
    for obj in objs:
        out.extend(obj)
    out.extend(b"%%EOF\n")
    return bytes(out)


class TestNavyFederalZeroThirdPartyIngestion:
    def test_nfcu_csv_indicator_trailing_minus_pending_transfers_and_running_balance(self):
        csv_text = """Posting Date,Transaction Date,Amount,Credit Debit Indicator,Type,Status,Description,Balance
11/18/2026,11/18/2026,19.50,Debit,POS,Pending,STARBUCKS STORE 9912,4210.50
11/17/2026,11/17/2026,64.25-,Debit,POS,Posted,DECA COMMISSARY NORFOLK,4230.00
11/16/2026,11/16/2026,350.00,Debit,Transfer,Posted,TRANSFER TO SHARE SAVINGS,4294.25
11/15/2026,11/15/2026,2450.00,Credit,ACH,Posted,DFAS MIL PAY ACTIVE DUTY,4644.25
11/14/2026,11/14/2026,4.12,Credit,Dividend,Posted,NFCU SHARE DIVIDEND PAID,2194.25
11/13/2026,11/13/2026,200.00,Debit,Payment,Posted,NFCU CASHREWARDS PAYMENT,2190.13
"""
        res = ingest_statement(csv_text, filename="nfcu_checking_nov.csv", institution="auto", skip_pending=True)
        assert res.format_detected == "csv"
        assert res.detected_institution == "navy_federal"
        assert res.pending_skipped == 1
        assert len(res.rows) == 5
        assert res.transfers_detected == 2
        # Newest posted row is 11/17/2026 -> balance $4,230.00 (ignores skipped 11/18 pending!)
        assert res.detected_balance_cents == 423_000

        by_desc = {r.merchant: r for r in res.rows}
        assert by_desc["DECA COMMISSARY NORFOLK"].tx_type == "expense"
        assert by_desc["DECA COMMISSARY NORFOLK"].amount_cents == 6425
        assert by_desc["TRANSFER TO SHARE SAVINGS"].tx_type == "transfer"
        assert by_desc["NFCU CASHREWARDS PAYMENT"].tx_type == "transfer"
        assert by_desc["DFAS MIL PAY ACTIVE DUTY"].tx_type == "income"
        assert by_desc["DFAS MIL PAY ACTIVE DUTY"].amount_cents == 245_000
        assert by_desc["NFCU SHARE DIVIDEND PAID"].tx_type == "income"
        assert by_desc["NFCU SHARE DIVIDEND PAID"].amount_cents == 412

    def test_nfcu_csv_oldest_first_picks_final_row_balance(self):
        csv_text = """Date,Description,Amount,Status,Balance
11/01/2026,DFAS ACTIVE DUTY PAY,2400.00,Posted,3400.00
11/05/2026,NAVY EXCHANGE NEX,85.00-,Posted,3315.00
11/10/2026,TRICARE PRIME,45.00-,Posted,3270.00
"""
        res = ingest_statement(csv_text, filename="navy_federal_export.csv", institution="navy_federal")
        assert res.detected_institution == "navy_federal"
        assert len(res.rows) == 3
        assert res.detected_balance_cents == 327_000
        assert res.rows[1].tx_type == "expense" and res.rows[1].amount_cents == 8500
        assert res.rows[2].tx_type == "expense" and res.rows[2].amount_cents == 4500

    def test_nfcu_combined_monthly_pdf_local_parser(self):
        pdf_bytes = _build_minimal_pdf(
            [
                "Navy Federal Credit Union",
                "Statement Period: 10/15/2026 through 11/14/2026",
                "Active Duty Checking",
                "Beginning Balance 1,500.00",
                "11/01 DFAS MIL PAY DIRECT DEPOSIT 2,450.00 3,950.00",
                "11/03 DECA COMMISSARY 82.40- 3,867.60",
                "11/06 TRANSFER TO SHARE SAVINGS 300.00- 3,567.60",
                "Ending Balance: $3,567.60",
                "Share Savings",
                "11/14 NFCU SHARE DIVIDEND 2.15 1,202.15",
            ],
            compress=True,
        )
        res = ingest_statement(pdf_bytes, filename="nfcu_statement_2026_11.pdf", institution="auto")
        assert res.format_detected == "pdf"
        assert res.detected_institution == "navy_federal"
        assert len(res.rows) == 4
        assert res.rows[0].tx_date == "2026-11-01"
        assert res.rows[0].tx_type == "income" and res.rows[0].amount_cents == 245_000
        assert res.rows[1].tx_date == "2026-11-03"
        assert res.rows[1].tx_type == "expense" and res.rows[1].amount_cents == 8240
        assert res.rows[2].tx_type == "transfer" and res.rows[2].amount_cents == 30_000
        assert res.rows[3].tx_type == "income" and res.rows[3].amount_cents == 215
        assert "Share Savings" in res.rows[3].memo
        assert res.detected_balance_cents == 120_215


class TestPlaidInteroperabilityAndCrossSourceDedup:
    def test_official_plaid_get_and_sync_json_normalization(self):
        plaid_sync_payload = {
            "accounts": [
                {
                    "account_id": "nfcu_chk_1",
                    "name": "Everyday Checking",
                    "balances": {"current": 4120.50, "available": 4050.00},
                }
            ],
            "added": [
                {
                    "transaction_id": "plaid_tx_001",
                    "account_id": "nfcu_chk_1",
                    "date": "2026-11-15",
                    "merchant_name": "Trader Joe's",
                    "name": "TRADER JOES #104 VA",
                    "amount": 84.50,  # Plaid positive = outflow/expense
                    "iso_currency_code": "USD",
                    "pending": False,
                    "personal_finance_category": {
                        "primary": "FOOD_AND_DRINK",
                        "detailed": "FOOD_AND_DRINK_GROCERIES",
                    },
                },
                {
                    "transaction_id": "plaid_tx_002",
                    "account_id": "nfcu_chk_1",
                    "date": "2026-11-15",
                    "name": "DFAS ACTIVE DUTY PAY",
                    "amount": -2450.00,  # Plaid negative = inflow/income
                    "iso_currency_code": "USD",
                    "pending": False,
                    "personal_finance_category": {
                        "primary": "INCOME",
                        "detailed": "INCOME_WAGES",
                    },
                },
                {
                    "transaction_id": "plaid_tx_003",
                    "account_id": "nfcu_chk_1",
                    "date": "2026-11-16",
                    "name": "NFCU CARD PAYMENT",
                    "amount": 250.00,
                    "iso_currency_code": "USD",
                    "pending": False,
                    "personal_finance_category": {
                        "primary": "LOAN_PAYMENTS",
                        "detailed": "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
                    },
                },
                {
                    "transaction_id": "plaid_tx_004",
                    "account_id": "nfcu_chk_1",
                    "date": "2026-11-18",
                    "name": "COFFEE SHOP PENDING",
                    "amount": 6.50,
                    "iso_currency_code": "USD",
                    "pending": True,
                },
            ],
            "modified": [],
            "removed": [],
        }

        res = ingest_statement(json.dumps(plaid_sync_payload), filename="plaid_sync.json", skip_pending=True)
        assert res.format_detected == "json"
        assert res.detected_institution == "plaid"
        assert res.pending_skipped == 1
        assert res.detected_balance_cents == 412_050
        assert len(res.rows) == 3

        assert res.rows[0].merchant == "Trader Joe's"
        assert res.rows[0].tx_type == "expense" and res.rows[0].amount_cents == 8450
        assert res.rows[1].tx_type == "income" and res.rows[1].amount_cents == 245_000
        assert res.rows[2].tx_type == "transfer" and res.rows[2].amount_cents == 25_000

    def test_cross_source_plaid_and_navy_federal_csv_dedup_and_api_upload(self, tmp_path):
        db_path = str(tmp_path / "nfcu_plaid_e2e.sqlite3")
        svc = FinanceService(db_path=db_path)
        app = create_app(db_path=db_path, service=svc, enforce_vault_lock=False)
        try:
            with TestClient(app) as client:
                # 1. Import official Plaid JSON first
                plaid_json = json.dumps(
                    {
                        "accounts": [{"account_id": "acc1", "balances": {"current": 3500.00}}],
                        "transactions": [
                            {
                                "transaction_id": "plaid-tx-881",
                                "account_id": "acc1",
                                "date": "2026-11-15",
                                "merchant_name": "Trader Joes",
                                "name": "TRADER JOES #104",
                                "amount": 84.50,
                                "iso_currency_code": "USD",
                                "pending": False,
                            },
                            {
                                "transaction_id": "plaid-tx-882",
                                "account_id": "acc1",
                                "date": "2026-11-15",
                                "name": "DFAS MIL PAY",
                                "amount": -2450.00,
                                "iso_currency_code": "USD",
                                "pending": False,
                            },
                        ],
                    }
                )
                r1 = client.post(
                    "/api/import/statement",
                    files={"file": ("plaid_export.json", plaid_json.encode("utf-8"), "application/json")},
                    data={"institution": "auto", "skip_pending": "true", "sync_balance": "true"},
                )
                assert r1.status_code == 200
                d1 = r1.json()
                assert d1["detected_institution"] == "plaid"
                assert d1["imported_count"] == 2

                # Verify DFAS military pay subcategory auto-classification
                txs_after_plaid = svc.list_transactions(limit=20)
                dfas_tx = next(t for t in txs_after_plaid if "DFAS" in t.merchant)
                assert dfas_tx.subcategory == "Military / Federal Pay"

                # 2. Now preview overlapping Navy Federal CSV (+1 day posting lag on Trader Joes + 2 identical coffees + 1 pending)
                nfcu_csv = """Posting Date,Description,Amount,Credit Debit Indicator,Status,Balance
11/18/2026,PENDING AUTH COFFEE,7.00,Debit,Pending,3400.00
11/17/2026,STARBUCKS,5.50,Debit,Posted,3407.00
11/17/2026,STARBUCKS,5.50,Debit,Posted,3412.50
11/16/2026,TRADER JOES #104 VA,84.50,Debit,Posted,3418.00
11/16/2026,DECA COMMISSARY,42.00,Debit,Posted,3502.50
"""
                prev = client.post(
                    "/api/import/preview",
                    files={"file": ("nfcu_activity.csv", nfcu_csv.encode("utf-8"), "text/csv")},
                    data={"institution": "navy_federal", "skip_pending": "true"},
                )
                assert prev.status_code == 200
                p = prev.json()
                assert p["detected_institution"] == "navy_federal"
                assert p["pending_skipped"] == 1
                assert p["cross_source_duplicates"] == 1  # Trader Joes matched Plaid tx despite +1d lag & no FITID!
                assert p["new_count"] == 3  # 2 Starbucks + 1 Commissary (same-day coffees NOT collapsed!)
                assert p["detected_balance_cents"] == 340_700

                # 3. Commit Navy Federal CSV with balance sync
                imp = client.post(
                    "/api/import/statement",
                    files={"file": ("nfcu_activity.csv", nfcu_csv.encode("utf-8"), "text/csv")},
                    data={"institution": "navy_federal", "skip_pending": "true", "sync_balance": "true"},
                )
                assert imp.status_code == 200
                d2 = imp.json()
                assert d2["cross_source_duplicates"] == 1
                assert d2["imported_count"] == 2 or d2["imported_count"] == 3
                # Verify Trader Joes appears ONLY ONCE across both Plaid & NFCU imports
                all_txs = svc.list_transactions(limit=50)
                tj_txs = [t for t in all_txs if "trader" in t.merchant.lower()]
                assert len(tj_txs) == 1

                # Verify account balance synced to Navy Federal latest posted balance ($3,407.00)
                accts = svc.storage.list_accounts()
                assert int(accts[0]["balance_cents"]) == 340_700

                # 4. Upload a real Navy Federal PDF statement via multipart endpoint
                pdf_bytes = _build_minimal_pdf(
                    [
                        "Navy Federal Credit Union",
                        "Statement Period: 11/01/2026 through 11/20/2026",
                        "Everyday Checking",
                        "11/19 NAVY EXCHANGE NEX 31.25- 3,375.75",
                    ],
                    compress=True,
                )
                r_pdf = client.post(
                    "/api/import/statement",
                    files={"file": ("nfcu_nov.pdf", pdf_bytes, "application/pdf")},
                    data={"institution": "auto"},
                )
                assert r_pdf.status_code == 200
                dp = r_pdf.json()
                assert dp["format_detected"] == "pdf"
                assert dp["detected_institution"] == "navy_federal"
                assert dp["imported_count"] == 1
        finally:
            svc.close()
