"""Tests for OpenCode Go & Google Gemini AI Clients, Validator & 3-Mode Financial Coach."""

from __future__ import annotations

import json

import httpx
import pytest

from chronox_finance.ai.coach import FinancialCoach
from chronox_finance.ai.gemini_client import (
    CURATED_GEMINI_MODELS,
    GeminiClient,
    GeminiError,
)
from chronox_finance.ai.opencode_client import LLMClient, OpenCodeGoClient
from chronox_finance.ai.validator import SuggestionValidator, sanitize_rsd_tone
from chronox_finance.models import (
    OptimalAllocationStep,
    QuantForecastReport,
    SpendingVolatilityMetrics,
)
from chronox_finance.service import FinanceService


class TestOpenCodeGoClient:
    def test_openai_compat_chat_completion_and_caching(self):
        calls = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            assert request.url.path.endswith("/chat/completions")
            assert request.headers["Authorization"] == "Bearer test-go-key"
            assert "x-opencode-session" in request.headers
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": '{"suggestions": []}'}}]},
            )

        client = OpenCodeGoClient(
            api_key="test-go-key",
            base_url="https://opencode.ai/zen/go/v1",
            model="glm-5.3-flash",
            transport=httpx.MockTransport(handler),
        )
        r1 = client.chat_completion(system_prompt="sys", user_prompt="usr", json_mode=True)
        r2 = client.chat_completion(system_prompt="sys", user_prompt="usr", json_mode=True)
        assert r1.cached is False
        assert r2.cached is True
        assert calls["count"] == 1  # Protected rolling Go allowance via cache!

    def test_anthropic_protocol_dispatch_for_minimax_models(self):
        seen_paths = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_paths.append(request.url.path)
            assert request.headers["x-api-key"] == "test-go-key"
            assert request.headers["anthropic-version"] == "2023-06-01"
            return httpx.Response(
                200,
                json={"content": [{"type": "text", "text": "Calm verified answer"}]},
            )

        client = OpenCodeGoClient(
            api_key="test-go-key",
            model="minimax-m2.7",
            protocol="auto",
            transport=httpx.MockTransport(handler),
        )
        res = client.chat_completion(system_prompt="sys", user_prompt="hello")
        assert res.protocol_used == "anthropic"
        assert res.content == "Calm verified answer"
        assert seen_paths[0].endswith("/messages")

    def test_live_model_discovery_endpoint(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/models")
            return httpx.Response(
                200,
                json={"data": [{"id": "glm-5.3-flash", "owned_by": "zai"}, {"id": "kimi-k3", "owned_by": "moonshot"}]},
            )

        client = OpenCodeGoClient(api_key="test-go-key", transport=httpx.MockTransport(handler))
        res = client.list_models(force_refresh=True)
        assert res["live"] is True
        ids = [m["id"] for m in res["models"]]
        assert "glm-5.3-flash" in ids and "kimi-k3" in ids


class TestSuggestionValidatorAndCoach:
    def test_validator_rejects_hallucinated_dollars_and_bills_raid_and_cleans_rsd_tone(self):
        svc = FinanceService(db_path=":memory:")
        try:
            svc.load_demo_workspace()
            budget = svc._compute_budget()
            impulses = svc.list_impulses()
            bills_jar = next(j for j in budget.jars if j.slug == "bills")
            fun_jar = next(j for j in budget.jars if j.slug == "fun")
            daily_jar = next(j for j in budget.jars if j.slug == "daily")

            raw_ai_output = [
                # 1. INVALID: Attempts to raid protected Bills jar -> MUST BE REJECTED
                {
                    "title": "Raid rent money",
                    "rationale": "Move money out of Bills.",
                    "micro_step": "Transfer now.",
                    "priority": "high",
                    "action_type": "rebalance_jars",
                    "action_payload": {
                        "from_jar_id": bills_jar.id,
                        "to_jar_id": fun_jar.id,
                        "amount_cents": 5000,
                    },
                },
                # 2. INVALID: Hallucinates $9,876.54 not present anywhere in ledger -> MUST BE REJECTED
                {
                    "title": "Move imaginary windfall",
                    "rationale": "Since you have $9,876.54 sitting in savings, splurge today.",
                    "micro_step": "Spend $9,876.54 right now.",
                    "priority": "medium",
                    "action_type": "general_nudge",
                    "action_payload": {},
                },
                # 3. VALID (with RSD-shaming phrase that must be gently neutralized!)
                {
                    "title": "Top up Daily Living",
                    "rationale": "You failed this week, so shift $25.00 to stay comfortable.",
                    "micro_step": "Tap Apply to shift $25.00 from Fun to Daily.",
                    "priority": "medium",
                    "action_type": "rebalance_jars",
                    "action_payload": {
                        "from_jar_id": fun_jar.id,
                        "to_jar_id": daily_jar.id,
                        "amount_cents": 2500,
                    },
                    "impact_cents": 2500,
                },
            ]

            validator = SuggestionValidator()
            report = validator.validate_batch(
                raw_ai_output, budget=budget, impulses=impulses, model_used="glm-5.3-flash"
            )
            assert report.rejected_count == 2
            assert len(report.validated) == 1
            kept = report.validated[0]
            assert "you failed" not in kept.rationale.lower()
            assert kept.action_payload["from_jar_id"] == fun_jar.id
            assert kept.verified is True
        finally:
            svc.close()

    def test_coach_end_to_end_with_mocked_opencode_go_and_affordability_qa(self):
        svc = FinanceService(db_path=":memory:")
        try:
            svc.load_demo_workspace()
            budget = svc._compute_budget()
            fun_jar = next(j for j in budget.jars if j.slug == "fun")
            daily_jar = next(j for j in budget.jars if j.slug == "daily")

            def mock_handler(request: httpx.Request) -> httpx.Response:
                body = json.loads(request.content.decode("utf-8"))
                # Distinguish suggestion batch vs Q&A
                if "VERIFIED_FACTS" in body["messages"][1]["content"]:
                    payload = {
                        "suggestions": [
                            {
                                "title": "Protect your calm groove",
                                "rationale": f"You can shift $20.00 from {fun_jar.name} to {daily_jar.name} for extra grocery breathing room.",
                                "micro_step": "Tap Apply to shift $20.00.",
                                "priority": "medium",
                                "action_type": "rebalance_jars",
                                "action_payload": {
                                    "from_jar_id": fun_jar.id,
                                    "to_jar_id": daily_jar.id,
                                    "amount_cents": 2000,
                                },
                                "impact_cents": 2000,
                            }
                        ]
                    }
                    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)}}]})
                return httpx.Response(
                    200,
                    json={"choices": [{"message": {"content": "Yes, $45 fits comfortably inside your Safe-to-Spend!"}}]},
                )

            client = OpenCodeGoClient(
                api_key="test-go-key",
                model="glm-5.3-flash",
                transport=httpx.MockTransport(mock_handler),
            )
            coach = FinancialCoach(client=client)
            impulses = svc.list_impulses()
            stats = svc.get_dashboard().adhd_stats

            batch = coach.generate_suggestions(
                budget=budget,
                impulses=impulses,
                adhd_stats=stats,
                needs_triage_count=1,
                reset_done_this_week=False,
            )
            assert batch.source == "opencode_go"
            assert batch.model_used == "glm-5.3-flash"
            assert len(batch.suggestions) >= 2
            assert all(s.verified for s in batch.suggestions)

            qa = coach.answer_question(
                question="Can I afford a $45 dinner tonight?",
                budget=budget,
                impulses=impulses,
                adhd_stats=stats,
                hourly_wage_cents=3400,
                needs_triage_count=1,
                reset_done_this_week=False,
            )
            assert qa.source == "opencode_go"
            assert qa.affordability_check is not None
            assert qa.affordability_check["purchase_cost_dollars"] == 45.0
        finally:
            svc.close()

    def test_rsd_tone_sanitizer(self):
        cleaned, changed = sanitize_rsd_tone("You failed and were reckless with your budget!")
        assert changed is True
        assert "failed" not in cleaned.lower()
        assert "reckless" not in cleaned.lower()

    def test_timeout_falls_back_cleanly_to_verified_local_suggestions(self, tmp_path):
        svc = FinanceService(db_path=str(tmp_path / "timeout_test.sqlite3"))
        try:
            svc.load_demo_workspace()
            budget = svc._compute_budget()
            impulses = svc.list_impulses()
            stats = svc.get_dashboard().adhd_stats

            def timeout_handler(request: httpx.Request) -> httpx.Response:
                raise httpx.ReadTimeout("simulated gateway timeout", request=request)

            client = OpenCodeGoClient(
                api_key="test-go-key",
                model="glm-5.3-flash",
                transport=httpx.MockTransport(timeout_handler),
            )
            coach = FinancialCoach(client=client)
            batch = coach.generate_suggestions(
                budget=budget,
                impulses=impulses,
                adhd_stats=stats,
                needs_triage_count=0,
                reset_done_this_week=True,
                credit_report=svc.get_credit_report(),
            )
            assert batch.source == "deterministic_local"
            assert batch.validation_passed is True
            assert len(batch.suggestions) >= 1
            assert "timed out" in (batch.notice or "").lower()
        finally:
            svc.close()


class TestGeminiClient:
    def test_protocol_conformance(self):
        assert isinstance(GeminiClient(api_key=""), LLMClient)
        assert isinstance(OpenCodeGoClient(api_key=""), LLMClient)
        assert GeminiClient(api_key="").provider_name == "gemini"
        assert OpenCodeGoClient(api_key="").provider_name == "opencode_go"

    def test_gemini_chat_completion_header_only_auth_and_caching(self):
        calls = {"count": 0}
        secret_key = "AIza-super-secret-gemini-key-999"

        def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            assert request.url.path.endswith("/models/gemini-2.5-flash:generateContent")
            # SECURITY CRITICAL: Key must ONLY be in x-goog-api-key header, NEVER in URL query!
            assert request.headers["x-goog-api-key"] == secret_key
            assert "key" not in request.url.params
            assert secret_key not in str(request.url)

            payload = json.loads(request.content.decode("utf-8"))
            assert payload["systemInstruction"]["parts"][0]["text"] == "Be calm"
            assert payload["contents"][0]["parts"][0]["text"] == "Analyze budget"
            assert payload["generationConfig"]["responseMimeType"] == "application/json"
            assert payload["generationConfig"]["maxOutputTokens"] == 1800

            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "finishReason": "STOP",
                            "content": {
                                "role": "model",
                                "parts": [{"text": '{"suggestions": []}'}],
                            },
                        }
                    ]
                },
            )

        client = GeminiClient(
            api_key=secret_key,
            model="gemini-2.5-flash",
            transport=httpx.MockTransport(handler),
        )
        r1 = client.chat_completion(system_prompt="Be calm", user_prompt="Analyze budget", json_mode=True)
        r2 = client.chat_completion(system_prompt="Be calm", user_prompt="Analyze budget", json_mode=True)
        assert r1.content == '{"suggestions": []}'
        assert r1.model == "gemini-2.5-flash"
        assert r1.protocol_used == "gemini"
        assert r1.provider == "gemini"
        assert r1.cached is False
        assert r2.cached is True
        assert calls["count"] == 1

    def test_gemini_env_resolution_and_url_security(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        assert GeminiClient().is_configured is False

        monkeypatch.setenv("GOOGLE_API_KEY", "google-fallback-key")
        assert GeminiClient().api_key == "google-fallback-key"

        monkeypatch.setenv("GEMINI_API_KEY", "gemini-primary-key")
        assert GeminiClient().api_key == "gemini-primary-key"
        assert GeminiClient(api_key="explicit-key").api_key == "explicit-key"

        # Accidental query param stripped from base_url
        c = GeminiClient(
            api_key="explicit-key",
            base_url="https://generativelanguage.googleapis.com/v1beta?key=leaked",
        )
        assert "?" not in c.base_url
        assert "leaked" not in c.base_url

        # SSRF protection via validate_external_api_url
        with pytest.raises(ValueError):
            GeminiClient(api_key="k", base_url="http://169.254.169.254/latest/meta-data")

    def test_gemini_skips_thought_parts_and_retries_on_max_tokens(self):
        attempts = {"count": 0}
        seen_max_tokens = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["count"] += 1
            body = json.loads(request.content.decode("utf-8"))
            seen_max_tokens.append(body["generationConfig"]["maxOutputTokens"])
            if attempts["count"] == 1:
                # Only thought part emitted before hitting MAX_TOKENS
                return httpx.Response(
                    200,
                    json={
                        "candidates": [
                            {
                                "finishReason": "MAX_TOKENS",
                                "content": {
                                    "parts": [
                                        {"thought": True, "text": "internal scratchpad reasoning..."}
                                    ]
                                },
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "finishReason": "STOP",
                            "content": {
                                "parts": [
                                    {"thought": True, "text": "brief internal thought"},
                                    {"text": "Final calm answer without thoughts."},
                                ]
                            },
                        }
                    ]
                },
            )

        client = GeminiClient(
            api_key="test-gemini-key",
            model="models/gemini-2.5-pro",
            transport=httpx.MockTransport(handler),
        )
        assert client.model == "gemini-2.5-pro"
        res = client.chat_completion(system_prompt="sys", user_prompt="usr", max_tokens=1000)
        assert attempts["count"] == 2
        assert seen_max_tokens[1] > seen_max_tokens[0]
        assert res.content == "Final calm answer without thoughts."
        assert "internal" not in res.content

    def test_gemini_live_model_discovery_filtering_prefix_strip_and_cache(self):
        calls = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            assert request.url.path.endswith("/models")
            assert request.headers["x-goog-api-key"] == "test-gemini-key"
            assert "key" not in request.url.params
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "models/gemini-2.5-flash",
                            "displayName": "Gemini 2.5 Flash",
                            "supportedGenerationMethods": ["generateContent", "countTokens"],
                        },
                        {
                            "name": "models/text-embedding-004",
                            "displayName": "Text Embedding",
                            "supportedGenerationMethods": ["embedContent"],
                        },
                        {
                            "name": "models/gemini-2.5-pro",
                            "displayName": "Gemini 2.5 Pro",
                            "supportedGenerationMethods": ["generateContent"],
                        },
                    ]
                },
            )

        unconfigured = GeminiClient(api_key="")
        fallback = unconfigured.list_models()
        assert fallback["live"] is False
        assert fallback["models"] == CURATED_GEMINI_MODELS

        client = GeminiClient(api_key="test-gemini-key", transport=httpx.MockTransport(handler))
        res1 = client.list_models(force_refresh=True)
        res2 = client.list_models(force_refresh=False)
        assert calls["count"] == 1
        assert res1["live"] is True
        assert res2["live"] is True
        ids = [m["id"] for m in res1["models"]]
        assert ids == ["gemini-2.5-flash", "gemini-2.5-pro"]
        assert "text-embedding-004" not in ids

        conn = client.test_connection()
        assert conn["ok"] is True
        assert conn["provider"] == "gemini"
        assert conn["models_count"] == 2

    def test_gemini_error_handling_401_403_429_timeout_and_key_scrubbing(self):
        secret = "AIza-SECRET-DO-NOT-LEAK-777"

        for status, expected_token in [(401, "401"), (403, "403"), (429, "429")]:
            def status_handler(request: httpx.Request, code: int = status) -> httpx.Response:
                return httpx.Response(code, json={"error": {"message": f"Denied for {secret}"}})

            c = GeminiClient(api_key=secret, transport=httpx.MockTransport(status_handler))
            with pytest.raises(GeminiError) as exc_info:
                c.chat_completion(system_prompt="s", user_prompt="u", use_cache=False)
            assert expected_token in str(exc_info.value)
            assert secret not in str(exc_info.value)

        def timeout_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout(f"timeout while sending {secret}", request=request)

        c_timeout = GeminiClient(api_key=secret, transport=httpx.MockTransport(timeout_handler))
        with pytest.raises(GeminiError) as exc_t:
            c_timeout.chat_completion(system_prompt="s", user_prompt="u", use_cache=False)
        assert "timed out" in str(exc_t.value).lower()
        assert secret not in str(exc_t.value)

        def network_err_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"failed with {secret}", request=request)

        c_net = GeminiClient(api_key=secret, transport=httpx.MockTransport(network_err_handler))
        with pytest.raises(GeminiError) as exc_n:
            c_net.chat_completion(system_prompt="s", user_prompt="u", use_cache=False)
        assert secret not in str(exc_n.value)
        assert "[REDACTED]" in str(exc_n.value)


def _sample_quant_forecast() -> QuantForecastReport:
    return QuantForecastReport(
        horizon_days=30,
        starting_balance_cents=310_000,
        ending_balance_cents=345_000,
        lowest_balance_cents=82_500,
        lowest_balance_date="2026-12-04",
        overdraft_risk=False,
        safe_runway_days=30,
        monthly_surplus_cents=65_000,
        total_expected_annual_benefit_cents=14_800,
        volatility=SpendingVolatilityMetrics(
            sample_days=60,
            weekly_mean_cents=28_000,
            p50_weekly_spend_cents=26_000,
            p90_weekly_spend_cents=39_000,
            recommended_buffer_cents=78_000,
            p50_daily_safe_cents=5_200,
            p90_conservative_daily_safe_cents=3_800,
            stability_score_0_100=84,
        ),
        optimal_allocations=[
            OptimalAllocationStep(
                priority_rank=1,
                bucket="high_apr_debt",
                target_name="Chase Sapphire 24.99%",
                amount_cents=40_000,
                expected_annual_benefit_cents=10_000,
                effective_apr_equivalent_pct=24.99,
                rationale="Guaranteed 24.99% risk-free annualized return by paying highest-APR balance first.",
                formula_note="400.00 * 24.99% = $99.96/yr interest avoided",
            )
        ],
    )


class TestMultiModeAndProviderRouting:
    def test_deterministic_only_makes_zero_external_calls(self, tmp_path):
        svc = FinanceService(db_path=str(tmp_path / "det_only.sqlite3"))
        try:
            svc.load_demo_workspace()
            budget = svc._compute_budget()
            impulses = svc.list_impulses()
            stats = svc.get_dashboard().adhd_stats
            qf = _sample_quant_forecast()

            def forbid_network(request: httpx.Request) -> httpx.Response:
                pytest.fail(f"External HTTP call must never happen in deterministic_only mode: {request.url}")

            oc = OpenCodeGoClient(api_key="go-key", transport=httpx.MockTransport(forbid_network))
            gm = GeminiClient(api_key="gm-key", transport=httpx.MockTransport(forbid_network))
            coach = FinancialCoach(client=oc, fallback_client=gm)

            batch = coach.generate_suggestions(
                budget=budget,
                impulses=impulses,
                adhd_stats=stats,
                needs_triage_count=0,
                reset_done_this_week=False,
                credit_report=svc.get_credit_report(),
                quant_forecast=qf,
                intelligence_mode="deterministic_only",
            )
            assert batch.source == "deterministic_quant"
            assert batch.intelligence_mode == "deterministic_only"
            assert batch.provider_used is None
            assert batch.model_used is None
            assert len(batch.suggestions) >= 1
            assert all(s.verified for s in batch.suggestions)

            qa = coach.answer_question(
                question="Can I afford a $120 keyboard this week?",
                budget=budget,
                impulses=impulses,
                adhd_stats=stats,
                hourly_wage_cents=3400,
                needs_triage_count=0,
                reset_done_this_week=False,
                credit_report=svc.get_credit_report(),
                quant_forecast=qf,
                intelligence_mode="deterministic_only",
            )
            assert qa.source == "deterministic_quant"
            assert qa.intelligence_mode == "deterministic_only"
            assert qa.provider_used is None
            assert qa.model_used is None
            assert "lowest_30d_balance_dollars" in qa.verified_numbers
            assert "runway_lowest_balance_dollars" in qa.verified_numbers

            # Also verify deterministic_only works cleanly via QuantMathEngine even when quant_forecast=None
            batch_auto = coach.generate_suggestions(
                budget=budget,
                impulses=impulses,
                adhd_stats=stats,
                needs_triage_count=0,
                reset_done_this_week=False,
                credit_report=svc.get_credit_report(),
                quant_forecast=None,
                intelligence_mode="deterministic_only",
            )
            assert batch_auto.source == "deterministic_quant"
            assert len(batch_auto.suggestions) >= 1
            assert all(s.source == "deterministic_quant" and s.verified for s in batch_auto.suggestions)

            qa_debt = coach.answer_question(
                question="How should I allocate $500 extra toward my debt and credit score?",
                budget=budget,
                impulses=impulses,
                adhd_stats=stats,
                hourly_wage_cents=3400,
                needs_triage_count=0,
                reset_done_this_week=False,
                credit_report=svc.get_credit_report(),
                quant_forecast=None,
                intelligence_mode="deterministic_only",
            )
            assert qa_debt.source == "deterministic_quant"
            assert qa_debt.intelligence_mode == "deterministic_only"
            assert qa_debt.provider_used is None
            assert len(qa_debt.answer) > 40
        finally:
            svc.close()

    def test_hybrid_mode_with_gemini_and_quant_anchors(self, tmp_path):
        svc = FinanceService(db_path=str(tmp_path / "hybrid_gemini.sqlite3"))
        try:
            svc.load_demo_workspace()
            budget = svc._compute_budget()
            impulses = svc.list_impulses()
            stats = svc.get_dashboard().adhd_stats
            qf = _sample_quant_forecast()

            def gemini_handler(request: httpx.Request) -> httpx.Response:
                body = json.loads(request.content.decode("utf-8"))
                prompt_text = body["contents"][0]["parts"][0]["text"]
                if "VERIFIED_FACTS" in prompt_text:
                    # Verify quant_forecast summary is embedded in prompt!
                    assert "quant_forecast" in prompt_text
                    suggestions = {
                        "suggestions": [
                            {
                                "title": "Direct $400.00 to Chase Sapphire",
                                "rationale": "Putting $400.00 toward your top marginal-ROI debt saves $100.00/yr while keeping your $825.00 runway floor safe.",
                                "micro_step": "Schedule a $400.00 extra payment after paycheck clears.",
                                "priority": "high",
                                "action_type": "general_nudge",
                                "action_payload": {},
                                "impact_cents": 40_000,
                            }
                        ]
                    }
                    return httpx.Response(
                        200,
                        json={"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(suggestions)}]}}]},
                    )
                return httpx.Response(
                    200,
                    json={
                        "candidates": [
                            {
                                "finishReason": "STOP",
                                "content": {"parts": [{"text": "Your lowest 30-day runway point stays calm at $825.00."}]},
                            }
                        ]
                    },
                )

            gm = GeminiClient(api_key="gm-key", model="gemini-2.5-flash", transport=httpx.MockTransport(gemini_handler))
            coach = FinancialCoach(client=gm)

            batch = coach.generate_suggestions(
                budget=budget,
                impulses=impulses,
                adhd_stats=stats,
                needs_triage_count=0,
                reset_done_this_week=True,
                credit_report=svc.get_credit_report(),
                quant_forecast=qf,
                intelligence_mode="hybrid",
            )
            assert batch.source == "gemini"
            assert batch.provider_used == "gemini"
            assert batch.intelligence_mode == "hybrid"
            assert batch.model_used == "gemini-2.5-flash"
            assert batch.rejected_count == 0
            assert any(s.source == "gemini" and s.verified for s in batch.suggestions)

            qa = coach.answer_question(
                question="How does my 30-day cashflow runway look?",
                budget=budget,
                impulses=impulses,
                adhd_stats=stats,
                hourly_wage_cents=3400,
                needs_triage_count=0,
                reset_done_this_week=True,
                credit_report=svc.get_credit_report(),
                quant_forecast=qf,
                intelligence_mode="hybrid",
            )
            assert qa.source == "gemini"
            assert qa.provider_used == "gemini"
            assert qa.intelligence_mode == "hybrid"
            assert "$825.00" in qa.answer
        finally:
            svc.close()

    def test_auto_provider_failover_from_opencode_to_gemini(self, tmp_path):
        svc = FinanceService(db_path=str(tmp_path / "failover.sqlite3"))
        try:
            svc.load_demo_workspace()
            budget = svc._compute_budget()
            impulses = svc.list_impulses()
            stats = svc.get_dashboard().adhd_stats

            def failing_opencode(request: httpx.Request) -> httpx.Response:
                return httpx.Response(429, text="quota exhausted")

            def healthy_gemini(request: httpx.Request) -> httpx.Response:
                body = json.loads(request.content.decode("utf-8"))
                prompt_text = body["contents"][0]["parts"][0]["text"]
                if "VERIFIED_FACTS" in prompt_text:
                    payload = {
                        "suggestions": [
                            {
                                "title": "Keep calm daily pacing",
                                "rationale": "Protect your momentum with a $25.00 weekly cushion sweep.",
                                "micro_step": "Review your Fun jar after dinner.",
                                "priority": "medium",
                                "action_type": "general_nudge",
                                "action_payload": {},
                                "impact_cents": 2500,
                            }
                        ]
                    }
                    return httpx.Response(
                        200,
                        json={"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(payload)}]}}]},
                    )
                return httpx.Response(
                    200,
                    json={"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "Gemini failover answer."}]}}]},
                )

            oc = OpenCodeGoClient(api_key="go-key", transport=httpx.MockTransport(failing_opencode))
            gm = GeminiClient(api_key="gm-key", model="gemini-2.5-flash", transport=httpx.MockTransport(healthy_gemini))
            coach = FinancialCoach(client=oc, fallback_client=gm)

            batch = coach.generate_suggestions(
                budget=budget,
                impulses=impulses,
                adhd_stats=stats,
                needs_triage_count=0,
                reset_done_this_week=True,
                intelligence_mode="hybrid",
            )
            assert batch.source == "gemini"
            assert batch.provider_used == "gemini"
            assert "gemini" in (batch.notice or "").lower()

            qa = coach.answer_question(
                question="Quick check-in?",
                budget=budget,
                impulses=impulses,
                adhd_stats=stats,
                hourly_wage_cents=3000,
                needs_triage_count=0,
                reset_done_this_week=True,
                intelligence_mode="hybrid",
            )
            assert qa.source == "gemini"
            assert qa.provider_used == "gemini"
            assert qa.answer == "Gemini failover answer."
        finally:
            svc.close()

    def test_ai_standalone_allows_creative_advice_sanitizes_rsd_and_blocks_bills_raid(self, tmp_path):
        svc = FinanceService(db_path=str(tmp_path / "standalone.sqlite3"))
        try:
            svc.load_demo_workspace()
            budget = svc._compute_budget()
            impulses = svc.list_impulses()
            stats = svc.get_dashboard().adhd_stats
            bills_jar = next(j for j in budget.jars if j.slug == "bills")
            fun_jar = next(j for j in budget.jars if j.slug == "fun")

            def gemini_standalone(request: httpx.Request) -> httpx.Response:
                body = json.loads(request.content.decode("utf-8"))
                prompt_text = body["contents"][0]["parts"][0]["text"]
                if "ACCOUNT_CONTEXT" in prompt_text and "USER_QUESTION" not in prompt_text:
                    payload = {
                        "suggestions": [
                            # Creative illustrative dollar target ($375.00 not in ledger) + RSD phrase -> ALLOWED & SANITIZED
                            {
                                "title": "Build a Joyful Automation Habit",
                                "rationale": "You failed before only because willpower runs dry; try automating a $375.00 quarterly treat fund.",
                                "micro_step": "Set a 2-minute calendar reminder on payday.",
                                "priority": "medium",
                                "action_type": "general_nudge",
                                "action_payload": {},
                                "impact_cents": 37_500,
                            },
                            # Unsafe Bills jar raid -> MUST STILL BE BLOCKED in AI Standalone!
                            {
                                "title": "Raid Bills Jar",
                                "rationale": "Pull $50.00 out of Bills.",
                                "micro_step": "Transfer from Bills.",
                                "priority": "high",
                                "action_type": "rebalance_jars",
                                "action_payload": {
                                    "from_jar_id": bills_jar.id,
                                    "to_jar_id": fun_jar.id,
                                    "amount_cents": 5000,
                                },
                            },
                            # Negative credit paydown -> MUST STILL BE BLOCKED!
                            {
                                "title": "Impossible Negative Paydown",
                                "rationale": "Pay -$100.",
                                "micro_step": "Negative pay.",
                                "priority": "low",
                                "action_type": "credit_paydown",
                                "action_payload": {"credit_line_id": 1, "amount_cents": -10000},
                            },
                        ]
                    }
                    return httpx.Response(
                        200,
                        json={"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(payload)}]}}]},
                    )
                return httpx.Response(
                    200,
                    json={
                        "candidates": [
                            {
                                "finishReason": "STOP",
                                "content": {
                                    "parts": [
                                        {"text": "You were not reckless — try a calm 72-hour rule for big upgrades!"}
                                    ]
                                },
                            }
                        ]
                    },
                )

            gm = GeminiClient(api_key="gm-key", model="gemini-2.5-flash", transport=httpx.MockTransport(gemini_standalone))
            coach = FinancialCoach(client=gm)

            batch = coach.generate_suggestions(
                budget=budget,
                impulses=impulses,
                adhd_stats=stats,
                needs_triage_count=0,
                reset_done_this_week=True,
                intelligence_mode="ai_standalone",
            )
            assert batch.source == "ai_standalone"
            assert batch.intelligence_mode == "ai_standalone"
            assert batch.provider_used == "gemini"
            assert batch.rejected_count == 2  # Blocked Bills raid + negative paydown!
            assert len(batch.suggestions) == 1
            kept = batch.suggestions[0]
            assert kept.source == "ai_standalone"
            assert kept.verified is False
            assert "you failed" not in kept.rationale.lower()
            assert "$375.00" in kept.rationale

            qa = coach.answer_question(
                question="How do I stop impulse upgrading tech?",
                budget=budget,
                impulses=impulses,
                adhd_stats=stats,
                hourly_wage_cents=3400,
                needs_triage_count=0,
                reset_done_this_week=True,
                intelligence_mode="ai_standalone",
            )
            assert qa.source == "ai_standalone"
            assert qa.intelligence_mode == "ai_standalone"
            assert qa.provider_used == "gemini"
            assert "reckless" not in qa.answer.lower()
        finally:
            svc.close()

    def test_ai_standalone_unconfigured_falls_back_gracefully_with_notice(self, tmp_path):
        svc = FinanceService(db_path=str(tmp_path / "standalone_offline.sqlite3"))
        try:
            svc.load_demo_workspace()
            budget = svc._compute_budget()
            impulses = svc.list_impulses()
            stats = svc.get_dashboard().adhd_stats

            coach = FinancialCoach(client=None)
            batch = coach.generate_suggestions(
                budget=budget,
                impulses=impulses,
                adhd_stats=stats,
                needs_triage_count=0,
                reset_done_this_week=True,
                intelligence_mode="ai_standalone",
            )
            assert batch.intelligence_mode == "ai_standalone"
            assert batch.source == "deterministic_local"
            assert "ai standalone" in (batch.notice or "").lower()
            assert len(batch.suggestions) >= 1

            qa = coach.answer_question(
                question="How am I doing?",
                budget=budget,
                impulses=impulses,
                adhd_stats=stats,
                hourly_wage_cents=3000,
                needs_triage_count=0,
                reset_done_this_week=True,
                intelligence_mode="ai_standalone",
            )
            assert qa.intelligence_mode == "ai_standalone"
            assert qa.source == "deterministic_local"
            assert "ai standalone" in qa.answer.lower()
        finally:
            svc.close()

    def test_unconfigured_primary_routes_directly_to_configured_fallback_gemini(self, tmp_path):
        svc = FinanceService(db_path=str(tmp_path / "fallback_direct.sqlite3"))
        try:
            svc.load_demo_workspace()
            budget = svc._compute_budget()
            impulses = svc.list_impulses()
            stats = svc.get_dashboard().adhd_stats

            def gemini_ok(request: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    200,
                    json={"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "Direct Gemini answer."}]}}]},
                )

            oc_unconfigured = OpenCodeGoClient(api_key="")
            gm_configured = GeminiClient(api_key="gm-key", transport=httpx.MockTransport(gemini_ok))
            coach = FinancialCoach(client=oc_unconfigured, fallback_client=gm_configured)

            qa = coach.answer_question(
                question="Check my status",
                budget=budget,
                impulses=impulses,
                adhd_stats=stats,
                hourly_wage_cents=3000,
                needs_triage_count=0,
                reset_done_this_week=True,
                intelligence_mode="hybrid",
            )
            assert qa.source == "gemini"
            assert qa.provider_used == "gemini"
            assert qa.answer == "Direct Gemini answer."
        finally:
            svc.close()

    def test_gemini_json_mode_400_retry_and_test_connection_auth_failure(self):
        calls = {"count": 0}

        def retry_handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            body = json.loads(request.content.decode("utf-8"))
            if calls["count"] == 1:
                assert body["generationConfig"].get("responseMimeType") == "application/json"
                return httpx.Response(400, json={"error": {"message": "responseMimeType unsupported"}})
            assert "responseMimeType" not in body["generationConfig"]
            return httpx.Response(
                200,
                json={"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": '{"suggestions": []}'}]}}]},
            )

        c = GeminiClient(api_key="gm-key", transport=httpx.MockTransport(retry_handler))
        res = c.chat_completion("sys", "usr", json_mode=True, use_cache=False)
        assert calls["count"] == 2
        assert res.content == '{"suggestions": []}'

        # test_connection on 401 returns ok=False and scrubs key
        secret = "AIza-VERY-SECRET-KEY-12345"

        def auth_fail(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": {"message": f"Invalid key {secret}"}})

        c_fail = GeminiClient(api_key=secret, transport=httpx.MockTransport(auth_fail))
        conn = c_fail.test_connection()
        assert conn["ok"] is False
        assert conn["provider"] == "gemini"
        assert "401" in conn["message"]
        assert secret not in conn["message"]
