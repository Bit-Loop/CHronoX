"""
Comprehensive tests for Optional BYOK Credit Score APIs:
- Spinwheel (Equifax score + liabilities + optional debtProfile refresh)
- Array (1B/3B MISMO JSON scores, inquiries, derogatories, AU tradelines, HTTP 202 handling)
- CRS Credit API (JWT username/password login + direct Bearer, /system/health, bureau pulls, auth errors)
- Custom Credit Score API (public HTTPS + explicit loopback bridge + strict SSRF/host allowlists)
- Full FastAPI + FinanceService E2E lifecycle: Vault setup, connect, test, sync into /api/v1/credit/report,
  idempotent re-sync, VaultLockedError (423), zero secret leakage in GET/backup exports, and disconnect.
"""

from __future__ import annotations

import json
from typing import List

import httpx
import pytest
from fastapi.testclient import TestClient

from chronox_finance.api import create_app
from chronox_finance.connectors.base import (
    ConnectorAuthError,
    ConnectorError,
    HostNotAllowedError,
    NotConfiguredForLocalDesktop,
)
from chronox_finance.connectors.credit_providers import BureauAPIAdapter
from chronox_finance.security import CSRF_HEADER_NAME, VaultLockedError
from chronox_finance.service import FinanceService

# ---------------------------------------------------------------------------
# 1. Spinwheel Adapter Unit Tests
# ---------------------------------------------------------------------------


def test_spinwheel_adapter_test_and_sync_equifax_score_and_liabilities():
    calls: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        assert request.url.host == "sandbox-api.spinwheel.io"
        assert request.headers.get("authorization") == "Bearer sw_live_secret_999"

        if request.method == "POST" and request.url.path == "/v1/users/usr_123/debtProfile":
            return httpx.Response(200, json={"status": {"code": 200, "desc": "success"}})

        if request.method == "GET" and request.url.path == "/v1/users/usr_123":
            return httpx.Response(
                200,
                json={
                    "status": {"code": 200, "desc": "success"},
                    "data": {
                        "userId": "usr_123",
                        "creditProfile": {
                            "creditScore": 754,
                            "scoreModel": "VantageScore 3.0",
                            "bureau": "equifax",
                            "updatedAt": "2026-11-20T10:15:00Z",
                            "hardInquiries": [
                                {"lender": "Chase Bank", "inquiryDate": "2026-08-10", "bureau": "equifax"}
                            ],
                            "derogatories": [
                                {"kind": "late_30", "openedDate": "2022-03-01", "creditor": "Store Card"}
                            ],
                        },
                        "liabilities": {
                            "creditCards": [
                                {
                                    "creditCardId": "cc_sw_1",
                                    "creditCardProfile": {
                                        "accountName": "Chase Freedom Unlimited",
                                        "issuerName": "Chase",
                                    },
                                    "currentBalance": 1420.50,
                                    "creditLimit": 10000.00,
                                    "openedDate": "2025-02-15",
                                    "statementSummary": {
                                        "apr": 24.99,
                                        "minimumPaymentAmount": 35.00,
                                        "statementBalance": 1200.00,
                                        "statementDate": "2026-10-18",
                                        "dueDate": "2026-11-12",
                                    },
                                }
                            ],
                            "studentLoans": [
                                {
                                    "liabilityId": "sl_sw_1",
                                    "name": "Nelnet Direct Loan",
                                    "servicerName": "Nelnet",
                                    "currentBalance": 12500.00,
                                    "interestRate": 5.25,
                                    "minimumPaymentAmount": 165.00,
                                    "originationDate": "2020-09-01",
                                }
                            ],
                        },
                    },
                },
            )
        raise AssertionError(f"Unexpected Spinwheel request: {request.method} {request.url}")

    adapter = BureauAPIAdapter(
        "spinwheel",
        env="sandbox",
        api_key="sw_live_secret_999",
        user_id="usr_123",
        transport=httpx.MockTransport(handler),
        backoff_base=0.0,
    )
    assert adapter.is_configured() is True
    assert adapter.capabilities()["local_desktop_supported"] is True

    test_res = adapter.test_connection()
    assert test_res["ok"] is True
    assert test_res["has_credit_profile"] is True

    bundle = adapter.fetch_credit_snapshot(order_refresh=True)
    assert "POST /v1/users/usr_123/debtProfile" in calls
    assert len(bundle.reports) == 1
    assert bundle.reports[0].bureau == "equifax"
    assert bundle.reports[0].model == "vantage3"
    assert bundle.reports[0].score == 754
    assert bundle.reports[0].source == "spinwheel_api"

    assert len(bundle.inquiries) == 1
    assert bundle.inquiries[0].lender == "Chase Bank"
    assert bundle.inquiries[0].counts_until == "2027-08-10"
    assert bundle.inquiries[0].drops_off == "2028-08-10"

    assert len(bundle.derogatories) == 1
    assert bundle.derogatories[0].drops_off == "2029-03-01"

    assert len(bundle.tradelines) == 2
    by_name = {t.name: t for t in bundle.tradelines}
    assert by_name["Chase Freedom Unlimited"].balance_cents == 142050
    assert by_name["Chase Freedom Unlimited"].credit_limit_cents == 1000000
    assert by_name["Chase Freedom Unlimited"].apr_bps == 2499
    assert by_name["Chase Freedom Unlimited"].statement_closing_day == 18
    assert by_name["Chase Freedom Unlimited"].payment_due_day == 12
    assert by_name["Nelnet Direct Loan"].kind == "student_loan"
    assert by_name["Nelnet Direct Loan"].apr_bps == 525


# ---------------------------------------------------------------------------
# 2. Array Adapter Unit Tests (1B/3B MISMO JSON + HTTP 202 Handling)
# ---------------------------------------------------------------------------


def test_array_adapter_3b_mismo_scores_inquiries_derogatories_and_202():
    mode = {"status": 200}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "sandbox.array.io"
        assert request.headers.get("x-array-server-token") == "array_tok_secret_888"
        assert "reportKey=rk_001" in str(request.url)
        assert "displayToken=array_disp_secret_777" in str(request.url)

        if mode["status"] == 202:
            return httpx.Response(202, json={"status": "pending"})

        return httpx.Response(
            200,
            json={
                "CREDIT_RESPONSE": {
                    "@CreditReportFirstIssuedDate": "2026-11-22",
                    "CREDIT_SCORE": [
                        {
                            "@CreditRepositorySourceType": "Experian",
                            "@_ModelNameType": "FICO Score 8",
                            "@_Value": "772",
                            "@_Date": "2026-11-22",
                        },
                        {
                            "@CreditRepositorySourceType": "Equifax",
                            "@_ModelNameType": "VantageScore 3.0",
                            "@_Value": "764",
                            "@_Date": "2026-11-22",
                        },
                        {
                            "@CreditRepositorySourceType": "TransUnion",
                            "@_ModelNameType": "VantageScore 3.0",
                            "@_Value": "769",
                            "@_Date": "2026-11-22",
                        },
                    ],
                    "CREDIT_LIABILITY": [
                        {
                            "@CreditLiabilityID": "arr_liab_1",
                            "@_AccountType": "Revolving",
                            "@_AccountOwnershipType": "AuthorizedUser",
                            "@_UnpaidBalanceAmount": "520.00",
                            "@_CreditLimitAmount": "15000.00",
                            "@_MonthlyPaymentAmount": "25.00",
                            "@_AccountOpenedDate": "2025-06-01",
                            "@_AccountStatusType": "Open",
                            "_CREDITOR": {"@_Name": "Amex Platinum AU"},
                        }
                    ],
                    "CREDIT_INQUIRY": [
                        {
                            "@_Name": "CAPITAL ONE",
                            "@_Date": "2026-04-15",
                            "CREDIT_REPOSITORY": {"@_SourceType": "Experian"},
                        }
                    ],
                    "CREDIT_PUBLIC_RECORD": [
                        {
                            "@_Type": "Chapter 7 Bankruptcy",
                            "@_FiledDate": "2021-02-10",
                            "@_CourtName": "US Bankruptcy Court",
                            "@CreditRepositorySourceType": "Experian",
                        },
                        {
                            "@_Type": "Collection",
                            "@_FiledDate": "2023-05-20",
                            "@_CourtName": "Midland Credit",
                            "@_LegalObligationAmount": "480.00",
                            "@CreditRepositorySourceType": "Equifax",
                        },
                    ],
                }
            },
        )

    adapter = BureauAPIAdapter(
        "array",
        env="sandbox",
        api_key="array_tok_secret_888",
        report_key="rk_001",
        display_token="array_disp_secret_777",
        transport=httpx.MockTransport(handler),
        backoff_base=0.0,
    )
    assert adapter.test_connection()["ok"] is True

    bundle = adapter.fetch_credit_snapshot()
    assert len(bundle.reports) == 3
    scores_by_bureau = {r.bureau: (r.score, r.model) for r in bundle.reports}
    assert scores_by_bureau["experian"] == (772, "fico8")
    assert scores_by_bureau["equifax"] == (764, "vantage3")
    assert scores_by_bureau["transunion"] == (769, "vantage3")

    assert len(bundle.tradelines) == 1
    assert bundle.tradelines[0].name == "Amex Platinum AU"
    assert bundle.tradelines[0].is_authorized_user is True
    assert bundle.tradelines[0].credit_limit_cents == 1500000

    assert len(bundle.inquiries) == 1
    assert bundle.inquiries[0].bureau == "experian"
    assert bundle.inquiries[0].drops_off == "2028-04-15"

    assert len(bundle.derogatories) == 2
    by_kind = {d.kind: d for d in bundle.derogatories}
    assert by_kind["bankruptcy_ch7"].drops_off == "2031-02-10"  # +10y
    assert by_kind["collection"].drops_off == "2030-05-20"      # +7y
    assert by_kind["collection"].amount_cents == 48000

    # HTTP 202 (still generating) should raise informative retryable ConnectorError
    mode["status"] = 202
    assert adapter.test_connection()["ok"] is True
    with pytest.raises(ConnectorError, match="HTTP 202"):
        adapter.fetch_credit_snapshot()


# ---------------------------------------------------------------------------
# 3. CRS Credit API Adapter Unit Tests (JWT Login + Health + Auth Errors)
# ---------------------------------------------------------------------------


def test_crs_adapter_jwt_login_health_bureau_pull_and_auth_error():
    fail_auth = {"flag": False}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api-sandbox.stitchcredit.com"

        if request.method == "POST" and request.url.path == "/api/user/login":
            body = json.loads(request.content.decode("utf-8"))
            if fail_auth["flag"]:
                return httpx.Response(401, json={"message": "Invalid credentials"})
            assert body == {"username": "crs_user", "password": "crs_pwd_secret_666"}
            return httpx.Response(200, json={"token": "jwt_ephemeral_token_123"})

        if request.method == "GET" and request.url.path == "/api/system/health":
            assert request.headers.get("authorization") == "Bearer jwt_ephemeral_token_123"
            return httpx.Response(200, json={"status": "UP"})

        if request.method == "POST" and request.url.path == "/api/experian/credit-report/credit-report-experian-fico8":
            assert request.headers.get("authorization") == "Bearer jwt_ephemeral_token_123"
            return httpx.Response(
                200,
                json={
                    "reportDate": "2026-11-21",
                    "scores": [{"score": 785, "modelName": "FICO 8", "bureau": "experian"}],
                    "tradelines": [
                        {
                            "creditorName": "Navy Federal Visa",
                            "accountType": "Credit Card",
                            "currentBalance": 650.00,
                            "creditLimit": 20000.00,
                            "apr": 15.99,
                            "dateOpened": "2022-07-01",
                        }
                    ],
                    "inquiries": [{"subscriberName": "NFCU", "inquiryDate": "2026-01-15"}],
                },
            )
        raise AssertionError(f"Unexpected CRS call: {request.method} {request.url}")

    adapter = BureauAPIAdapter(
        "crs",
        env="sandbox",
        username="crs_user",
        password="crs_pwd_secret_666",
        bureau="experian",
        transport=httpx.MockTransport(handler),
        backoff_base=0.0,
    )
    health = adapter.test_connection()
    assert health["ok"] is True
    assert health["authenticated"] is True

    bundle = adapter.fetch_credit_snapshot()
    assert len(bundle.reports) == 1
    assert bundle.reports[0].bureau == "experian"
    assert bundle.reports[0].score == 785
    assert bundle.reports[0].model == "fico8"
    assert bundle.reports[0].source == "crs_api"
    assert bundle.tradelines[0].name == "Navy Federal Visa"
    assert bundle.tradelines[0].credit_limit_cents == 2000000
    assert bundle.inquiries[0].lender == "NFCU"

    # Path traversal option rejected
    with pytest.raises(ConnectorError, match="Invalid CRS"):
        adapter.fetch_credit_snapshot(option="../../etc/passwd")

    # 401 raises ConnectorAuthError and never leaks password
    fail_auth["flag"] = True
    with pytest.raises(ConnectorAuthError) as exc_info:
        adapter.test_connection()
    assert "crs_pwd_secret_666" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# 4. Custom Credit Score API + Strict SSRF / Host Allowlist Enforcement
# ---------------------------------------------------------------------------


def test_custom_credit_api_adapter_ssrf_and_allowlist_enforcement():
    # Reject SSRF & private network targets for Custom Credit API
    for bad_url in (
        "http://169.254.169.254/latest/meta-data",
        "https://10.0.0.5/credit",
        "https://192.168.1.25/credit",
        "http://example.com/credit",
        "https://user:pass@credit.example.com/v1",
        "ftp://credit.example.com/score",
    ):
        with pytest.raises(HostNotAllowedError):
            BureauAPIAdapter("custom", api_base_url=bad_url)

    # Reject non-allowlisted host overrides for Spinwheel / Array / CRS
    for vendor in ("spinwheel", "array", "crs"):
        with pytest.raises(HostNotAllowedError):
            BureauAPIAdapter(vendor, api_base_url="https://evil.example.com/steal", api_key="k")

    # Reject HTTP redirects (no secret leakage across hosts)
    redir_adapter = BureauAPIAdapter(
        "custom",
        api_base_url="https://credit-bridge.example.com/v1/snapshot",
        api_key="custom_bearer_secret_555",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(302, headers={"Location": "https://attacker.example.com/leak"})
        ),
        backoff_base=0.0,
    )
    with pytest.raises(HostNotAllowedError):
        redir_adapter.fetch_credit_snapshot()

    # Accept explicit loopback bridge AND public HTTPS bridge
    def custom_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer custom_bearer_secret_555"
        return httpx.Response(
            200,
            json={
                "reports": [
                    {"bureau": "transunion", "model": "fico8", "score": 802, "pulled_date": "2026-11-23"}
                ],
                "inquiries": [{"lender": "Apple Card", "bureau": "transunion", "inquiry_date": "2026-09-01"}],
            },
        )

    local_bridge = BureauAPIAdapter(
        "custom",
        api_base_url="http://127.0.0.1:9444/credit/snapshot",
        api_key="custom_bearer_secret_555",
        transport=httpx.MockTransport(custom_handler),
        backoff_base=0.0,
    )
    assert local_bridge.test_connection()["ok"] is True
    bundle = local_bridge.fetch_credit_snapshot()
    assert bundle.reports[0].score == 802
    assert bundle.reports[0].bureau == "transunion"
    assert bundle.reports[0].source == "custom_credit_api"
    assert len(bundle.inquiries) == 1


def test_unconfigured_bureau_adapter_preserves_not_configured_contract():
    for vendor in ("spinwheel", "array", "crs", "custom"):
        adapter = BureauAPIAdapter(vendor)
        assert adapter.is_configured() is False
        assert adapter.capabilities()["local_desktop_supported"] is False
        with pytest.raises(NotConfiguredForLocalDesktop):
            adapter.test_connection()
        with pytest.raises(NotConfiguredForLocalDesktop) as exc_info:
            adapter.fetch_credit_snapshot()
        assert "Plaid Liabilities" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 5. Full FastAPI + Vault E2E Lifecycle, Idempotency, 423 Lock & Secret Isolation
# ---------------------------------------------------------------------------


def test_credit_api_fastapi_e2e_vault_lifecycle_idempotency_and_zero_secret_leakage(tmp_path):
    def router_handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "sandbox-api.spinwheel.io":
            assert request.headers.get("authorization") == "Bearer sw_live_secret_999"
            return httpx.Response(
                200,
                json={
                    "data": {
                        "userId": "usr_e2e",
                        "creditProfile": {
                            "creditScore": 761,
                            "scoreModel": "VantageScore 3.0",
                            "bureau": "equifax",
                            "updatedAt": "2026-11-22",
                            "hardInquiries": [
                                {"lender": "Discover", "inquiryDate": "2026-07-12", "bureau": "equifax"}
                            ],
                            "derogatories": [
                                {"kind": "collection", "openedDate": "2022-05-01", "creditor": "Old Telecom"}
                            ],
                        },
                        "liabilities": {
                            "creditCards": [
                                {
                                    "creditCardId": "sw_cc_e2e",
                                    "name": "Discover It Cash Back",
                                    "currentBalance": 450.00,
                                    "creditLimit": 8000.00,
                                    "apr": 22.49,
                                    "minimumPaymentAmount": 30.00,
                                    "openedDate": "2025-08-01",
                                }
                            ]
                        },
                    }
                },
            )
        if host == "credit-bridge.example.com":
            assert request.headers.get("authorization") == "Bearer custom_bearer_secret_555"
            return httpx.Response(
                200,
                json={
                    "bureau": "experian",
                    "model": "fico8",
                    "score": 788,
                    "pulled_date": "2026-11-23",
                },
            )
        raise AssertionError(f"Unexpected host: {host}")

    db_file = str(tmp_path / "credit_api_e2e.sqlite3")
    svc = FinanceService(
        db_path=db_file,
        secret_store_path=tmp_path / "secrets.enc",
        kdf_iterations=10_000,
        credit_api_transport=httpx.MockTransport(router_handler),
    )
    app = create_app(db_path=db_file, service=svc, enforce_vault_lock=True)
    client = TestClient(app)

    try:
        # 1. Setup vault & capture CSRF token
        setup_resp = client.post("/api/v1/auth/setup", json={"password": "StrongVaultPassword!2026"})
        assert setup_resp.status_code == 200
        csrf = setup_resp.json()["csrf_token"]
        headers = {CSRF_HEADER_NAME: csrf}

        # 2. Catalog starts with 4 supported vendors & 0 connections
        cat = client.get("/api/v1/credit/apis").json()
        assert {v["vendor"] for v in cat["vendors"]} == {"spinwheel", "array", "crs", "custom"}
        assert cat["connections"] == []

        # 3. Connect Spinwheel with live test_on_connect=True
        r_sw = client.post(
            "/api/v1/credit/apis/connect",
            headers=headers,
            json={
                "vendor": "spinwheel",
                "env": "sandbox",
                "label": "My Equifax Live",
                "api_key": "sw_live_secret_999",
                "user_id": "usr_e2e",
                "test_on_connect": True,
            },
        )
        assert r_sw.status_code == 200, r_sw.text
        sw_body = r_sw.json()
        sw_conn_id = sw_body["connection"]["id"]
        assert sw_body["test_result"]["ok"] is True
        assert "sw_live_secret_999" not in r_sw.text

        # 4. Connect Custom Credit Score API
        r_cust = client.post(
            "/api/v1/credit/apis/connect",
            headers=headers,
            json={
                "vendor": "custom",
                "env": "production",
                "label": "Personal Experian Bridge",
                "api_key": "custom_bearer_secret_555",
                "api_base_url": "https://credit-bridge.example.com/v1/snapshot",
                "test_on_connect": True,
            },
        )
        assert r_cust.status_code == 200, r_cust.text
        cust_conn_id = r_cust.json()["connection"]["id"]
        assert "custom_bearer_secret_555" not in r_cust.text

        # 5. Test endpoint works & connections list validates cleanly with zero secret leaks
        assert client.post(f"/api/v1/credit/apis/{sw_conn_id}/test", headers=headers).json()["ok"] is True
        all_conns_resp = client.get("/api/v1/connections")
        assert all_conns_resp.status_code == 200
        assert len(all_conns_resp.json()) == 2
        assert "sw_live_secret_999" not in all_conns_resp.text
        assert "custom_bearer_secret_555" not in all_conns_resp.text

        apis_list_resp = client.get("/api/v1/credit/apis")
        assert apis_list_resp.status_code == 200
        assert "sw_live_secret_999" not in apis_list_resp.text
        assert "custom_bearer_secret_555" not in apis_list_resp.text

        # Backup export MUST NEVER contain sealed API secrets
        backup_resp = client.get("/api/v1/backup/export")
        assert backup_resp.status_code == 200
        assert "sw_live_secret_999" not in backup_resp.text
        assert "custom_bearer_secret_555" not in backup_resp.text

        # 6. Sync Spinwheel + Custom -> verify persisted into /api/v1/credit/report
        sync_sw = client.post(f"/api/v1/credit/apis/{sw_conn_id}/sync", headers=headers, json={})
        assert sync_sw.status_code == 200, sync_sw.text
        sc = sync_sw.json()["synced_counts"]
        assert sc["reports_added"] == 1
        assert sc["inquiries_added"] == 1
        assert sc["derogatories_added"] == 1
        assert sc["tradelines_added"] == 1

        sync_cust = client.post(f"/api/v1/credit/apis/{cust_conn_id}/sync", headers=headers, json={})
        assert sync_cust.status_code == 200
        assert sync_cust.json()["synced_counts"]["reports_added"] == 1

        # Re-syncing Spinwheel must be 100% idempotent (0 duplicate reports/inquiries/derogatories)
        resync_sw = client.post(f"/api/v1/credit/apis/{sw_conn_id}/sync", headers=headers, json={})
        assert resync_sw.status_code == 200
        sc2 = resync_sw.json()["synced_counts"]
        assert sc2["reports_added"] == 0
        assert sc2["reports_skipped_duplicate"] == 1
        assert sc2["inquiries_added"] == 0
        assert sc2["inquiries_skipped_duplicate"] == 1
        assert sc2["derogatories_added"] == 0
        assert sc2["derogatories_skipped_duplicate"] == 1
        assert sc2["tradelines_added"] == 0
        assert sc2["tradelines_updated"] == 1

        # Verify Credit Optimization Report & Simulator reflect live synced scores + tradeline + 5/24
        credit_rep = client.get("/api/v1/credit/report").json()
        assert len(credit_rep["credit_reports"]) == 2
        assert credit_rep["credit_reports"][0]["score"] == 788  # Most recent pulled_date (2026-11-23)
        assert credit_rep["credit_reports"][0]["bureau"] == "experian"
        assert credit_rep["credit_reports"][0]["source"] == "custom_credit_api"
        assert any(r["score"] == 761 and r["bureau"] == "equifax" for r in credit_rep["credit_reports"])
        assert len(credit_rep["credit_inquiries"]) == 1
        assert len(credit_rep["credit_derogatories"]) == 1
        assert any(cl["name"] == "Discover It Cash Back" for cl in credit_rep["credit_lines"])

        # 7. Vault lock must fail closed with VaultLockedError (and 423 via API handler)
        svc.secrets.lock()
        assert svc.list_credit_apis().connections[0].status == "paused_locked"
        with pytest.raises(VaultLockedError):
            svc.sync_credit_api(sw_conn_id)
        with pytest.raises(VaultLockedError):
            svc.test_credit_api(sw_conn_id)

        # Unlock via login & verify 423 mapping explicitly using non-auth-enforced app on locked vault
        app_423 = create_app(db_path=db_file, service=svc, enforce_vault_lock=False)
        client_423 = TestClient(app_423)
        r_locked = client_423.post(f"/api/v1/credit/apis/{sw_conn_id}/sync", json={})
        assert r_locked.status_code == 423
        assert r_locked.json()["code"] == "vault_locked"

        # 8. Re-login and disconnect cleanly (purges DB row AND secrets.enc entry)
        login_resp = client.post("/api/v1/auth/login", json={"password": "StrongVaultPassword!2026"})
        assert login_resp.status_code == 200
        headers2 = {CSRF_HEADER_NAME: login_resp.json()["csrf_token"]}

        del_resp = client.delete(f"/api/v1/credit/apis/{sw_conn_id}", headers=headers2)
        assert del_resp.status_code == 200
        assert del_resp.json()["deleted"] is True
        assert svc.secrets.get_json(f"conn:{sw_conn_id}") is None
        assert client.delete(f"/api/v1/credit/apis/{sw_conn_id}", headers=headers2).status_code == 404
    finally:
        svc.close()
