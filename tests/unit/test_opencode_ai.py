"""Tests for OpenCode Go & Google Gemini AI Clients, Validator & 3-Mode Financial Coach."""

from __future__ import annotations

import json

import httpx
import pytest

from chronox_finance.ai.coach import FinancialCoach
from chronox_finance.ai.codex_client import (
    CHRONOX_STATIC_KNOWLEDGE_PREFIX,
    CodexCLIClient,
    CodexCLIError,
    CodexError,
    CodexResponsesClient,
    estimate_token_count,
)
from chronox_finance.ai.gemini_client import (
    CURATED_GEMINI_MODELS,
    GeminiClient,
    GeminiError,
)
from chronox_finance.ai.opencode_client import (
    LLMClient,
    OpenCodeGoClient,
    calculate_token_costs,
)
from chronox_finance.ai.validator import SuggestionValidator, sanitize_rsd_tone
from chronox_finance.models import (
    OptimalAllocationStep,
    QuantForecastReport,
    SpendingVolatilityMetrics,
)
from chronox_finance.service import FinanceService
from tests.fixtures.seed_workspace import seed_workspace


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
            seed_workspace(svc)
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
            seed_workspace(svc)
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
            seed_workspace(svc)
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
            seed_workspace(svc)
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
            seed_workspace(svc)
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
            seed_workspace(svc)
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
            seed_workspace(svc)
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
            seed_workspace(svc)
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
            seed_workspace(svc)
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


class TestTokenTelemetryAndHonestSavings:
    def test_opencode_openai_and_anthropic_usage_parsing_and_local_cache_savings(self):
        # OpenAI-compat route with real cached tokens (prompt_tokens_details.cached_tokens)
        def openai_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": '{"suggestions": []}'}}],
                    "usage": {
                        "prompt_tokens": 2000,
                        "completion_tokens": 400,
                        "prompt_tokens_details": {"cached_tokens": 1500},
                    },
                },
            )

        oc = OpenCodeGoClient(
            api_key="go-key",
            model="glm-5.3-flash",
            transport=httpx.MockTransport(openai_handler),
        )
        r1 = oc.chat_completion("sys", "usr", json_mode=True)
        assert r1.input_tokens == 2000
        assert r1.output_tokens == 400
        assert r1.cached_tokens == 1500
        # glm-5.3-flash: input $0.10/1M (100k), cached $0.02/1M (20k), output $0.30/1M (300k)
        # uncached_in: 500 * 100k / 1M = 50; cached_in: 1500 * 20k / 1M = 30; out: 400 * 300k / 1M = 120 -> cost = 200
        # saved = 1500 * (100k - 20k) / 1M = 120
        assert r1.cost_microdollars == 200
        assert r1.saved_microdollars == 120
        assert r1.latency_ms >= 0

        # Local TTL cache hit saves full uncached input+output cost (200 + 120 = 320) and spends $0!
        r2 = oc.chat_completion("sys", "usr", json_mode=True)
        assert r2.cached is True
        assert r2.cost_microdollars == 0
        assert r2.input_tokens == 0
        assert r2.output_tokens == 0
        assert r2.cached_tokens == 0
        assert r2.saved_microdollars == 320

        # Anthropic-compat route with cache_read_input_tokens
        def anthropic_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "content": [{"type": "text", "text": "Anthropic calm answer"}],
                    "usage": {
                        "input_tokens": 500,
                        "cache_read_input_tokens": 1500,
                        "output_tokens": 250,
                    },
                },
            )

        oc_anth = OpenCodeGoClient(
            api_key="go-key",
            model="claude-haiku-5-5",
            protocol="anthropic",
            transport=httpx.MockTransport(anthropic_handler),
        )
        ra = oc_anth.chat_completion("sys", "usr", use_cache=False)
        assert ra.input_tokens == 2000
        assert ra.cached_tokens == 1500
        assert ra.output_tokens == 250
        # claude-haiku-5-5: 800k / 80k / 4_000k -> saved = 1500 * 720k / 1M = 1080
        assert ra.saved_microdollars == 1080
        assert ra.cost_microdollars == (500 * 800_000 + 1500 * 80_000 + 250 * 4_000_000) // 1_000_000

    def test_gemini_usage_metadata_cached_tokens_and_zero_fabricated_savings(self):
        calls = {"n": 0}

        def gem_handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            cached_count = 0 if calls["n"] == 1 else 2000
            return httpx.Response(
                200,
                json={
                    "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "OK"}]}}],
                    "usageMetadata": {
                        "promptTokenCount": 3000,
                        "candidatesTokenCount": 300,
                        "thoughtsTokenCount": 100,
                        "cachedContentTokenCount": cached_count,
                    },
                },
            )

        gm = GeminiClient(
            api_key="gm-key",
            model="gemini-2.5-flash",
            transport=httpx.MockTransport(gem_handler),
        )
        # First call: cachedContentTokenCount == 0 -> saved_microdollars MUST be strictly 0!
        r_no_cache = gm.chat_completion("sys", "q1", use_cache=False)
        assert r_no_cache.input_tokens == 3000
        assert r_no_cache.output_tokens == 400
        assert r_no_cache.cached_tokens == 0
        assert r_no_cache.saved_microdollars == 0
        # gemini-2.5-flash: 300k / 75k / 2_500k -> 3000*0.3 + 400*2.5 = 900 + 1000 = 1900
        assert r_no_cache.cost_microdollars == 1900

        # Second call: cachedContentTokenCount == 2000 -> saved = 2000 * (300k - 75k) / 1M = 450
        r_with_cache = gm.chat_completion("sys", "q2", use_cache=True)
        assert r_with_cache.cached_tokens == 2000
        assert r_with_cache.saved_microdollars == 450
        assert r_with_cache.cost_microdollars == 1450

        # Third call (local TTL hit on q2) -> saved = full uncached cost (1900), cost = 0
        r_local_hit = gm.chat_completion("sys", "q2", use_cache=True)
        assert r_local_hit.cached is True
        assert r_local_hit.cost_microdollars == 0
        assert r_local_hit.saved_microdollars == 1900

    def test_zero_tokens_or_local_endpoint_never_fabricate_cost_or_savings(self):
        assert calculate_token_costs(model="gpt-4.1-mini", input_tokens=0, output_tokens=0, cached_tokens=0) == (0, 0, 0)
        assert calculate_token_costs(
            model="qwen2.5:14b",
            input_tokens=4000,
            output_tokens=500,
            cached_tokens=3000,
            provider="chat_wrapper",
            is_local_endpoint=True,
        ) == (0, 0, 0)


class TestCodexResponsesAndChatWrapperClient:
    def test_static_prefix_is_over_1024_tokens_and_timestamp_uuid_free(self):
        import re

        assert estimate_token_count(CHRONOX_STATIC_KNOWLEDGE_PREFIX) >= 1024
        # Must NEVER contain dynamic ISO dates or UUIDs
        assert not re.search(r"\b20\d{2}-\d{2}-\d{2}\b", CHRONOX_STATIC_KNOWLEDGE_PREFIX)
        assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", CHRONOX_STATIC_KNOWLEDGE_PREFIX, re.I)

    def test_codex_responses_api_enforces_store_false_stable_prefix_and_usage(self):
        seen_bodies = []

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/responses")
            assert request.headers["Authorization"] == "Bearer sk-test-codex-123"
            body = json.loads(request.content.decode("utf-8"))
            seen_bodies.append(body)
            # BINDING PRIVACY RULE (#53): store MUST be False and NO previous_response_id!
            assert body["store"] is False
            assert "previous_response_id" not in body
            assert body["input"][0]["role"] == "developer"
            assert body["input"][0]["content"].startswith(CHRONOX_STATIC_KNOWLEDGE_PREFIX)
            assert estimate_token_count(body["input"][0]["content"]) >= 1024
            return httpx.Response(
                200,
                json={
                    "id": "resp_test_001",
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": '{"ok": true}'}],
                        }
                    ],
                    "usage": {
                        "input_tokens": 2500,
                        "output_tokens": 200,
                        "input_tokens_details": {"cached_tokens": 1800},
                    },
                },
            )

        client = CodexResponsesClient(
            api_key="sk-test-codex-123",
            model="gpt-4.1-mini",
            provider_name="openai_codex",
            wire_api="responses",
            transport=httpx.MockTransport(handler),
        )
        assert isinstance(client, LLMClient)
        assert client.provider_name == "openai_codex"
        assert client.is_configured is True

        r1 = client.chat_completion("Be calm.", "Snapshot A", json_mode=True)
        r2 = client.chat_completion("Be calm.", "Snapshot B", json_mode=True)
        r1_cached = client.chat_completion("Be calm.", "Snapshot A", json_mode=True)

        assert len(seen_bodies) == 2
        # Developer prefix is byte-for-byte identical across distinct snapshots for automatic prompt caching!
        assert seen_bodies[0]["input"][0]["content"] == seen_bodies[1]["input"][0]["content"]
        assert r1.protocol_used == "responses"
        assert r1.provider == "openai_codex"
        assert r1.input_tokens == 2500
        assert r1.output_tokens == 200
        assert r1.cached_tokens == 1800
        # gpt-4.1-mini: 400k / 100k / 1_600k -> saved = 1800 * 300k / 1M = 540
        assert r1.saved_microdollars == 540
        assert r1.cost_microdollars == (700 * 400_000 + 1800 * 100_000 + 200 * 1_600_000) // 1_000_000
        assert r1_cached.cached is True
        assert r1_cached.cost_microdollars == 0
        assert r1_cached.saved_microdollars == (2500 * 400_000 + 200 * 1_600_000) // 1_000_000
        assert r2.cached is False

    def test_chat_wrapper_completions_local_ollama_and_remote_gateway(self):
        # Empty base_url + empty key must NOT look configured!
        unconfigured = CodexResponsesClient(api_key="", base_url="", provider_name="chat_wrapper", wire_api="chat_completions")
        assert unconfigured.is_configured is False

        # Local keyless Ollama / LiteLLM gateway at 127.0.0.1 is allowed without API key and has honest $0 cost
        def ollama_handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/chat/completions")
            assert "Authorization" not in request.headers
            body = json.loads(request.content.decode("utf-8"))
            assert body["messages"][0]["role"] == "system"
            assert body["messages"][0]["content"].startswith(CHRONOX_STATIC_KNOWLEDGE_PREFIX)
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "Local wrapper calm reply"}}],
                    "usage": {"prompt_tokens": 1800, "completion_tokens": 120, "prompt_tokens_details": {"cached_tokens": 1400}},
                },
            )

        local_wrapper = CodexResponsesClient(
            api_key="",
            base_url="http://127.0.0.1:11434/v1",
            model="qwen2.5:14b",
            provider_name="chat_wrapper",
            wire_api="chat_completions",
            transport=httpx.MockTransport(ollama_handler),
        )
        assert local_wrapper.is_configured is True
        res = local_wrapper.chat_completion("Be calm.", "Check runway")
        assert res.provider == "chat_wrapper"
        assert res.protocol_used == "chat_completions"
        assert res.content == "Local wrapper calm reply"
        assert res.input_tokens == 1800
        assert res.cached_tokens == 1400
        # Local hardware costs $0 -> never fabricate savings!
        assert res.cost_microdollars == 0
        assert res.saved_microdollars == 0

        # Remote wrapper requires API key and scrubs key on errors
        remote_no_key = CodexResponsesClient(
            api_key="",
            base_url="https://openrouter.ai/api/v1",
            provider_name="chat_wrapper",
            wire_api="chat_completions",
        )
        assert remote_no_key.is_configured is False

        secret = "sk-or-secret-wrapper-999"

        def err_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": {"message": f"Bad key {secret}"}})

        remote_err = CodexResponsesClient(
            api_key=secret,
            base_url="https://openrouter.ai/api/v1",
            provider_name="chat_wrapper",
            wire_api="chat_completions",
            transport=httpx.MockTransport(err_handler),
        )
        with pytest.raises(CodexError) as exc_info:
            remote_err.chat_completion("s", "u", use_cache=False)
        assert secret not in str(exc_info.value)

        # SSRF protection
        with pytest.raises(ValueError):
            CodexResponsesClient(api_key="k", base_url="http://169.254.169.254/v1")

    def test_coach_failover_with_codex_and_telemetry_hook(self, tmp_path):
        svc = FinanceService(db_path=str(tmp_path / "codex_coach.sqlite3"))
        try:
            seed_workspace(svc)
            budget = svc._compute_budget()
            impulses = svc.list_impulses()
            stats = svc.get_dashboard().adhd_stats
            captured = []

            def fail_primary(request: httpx.Request) -> httpx.Response:
                return httpx.Response(429, text="rate limited")

            def codex_ok(request: httpx.Request) -> httpx.Response:
                body = json.loads(request.content.decode("utf-8"))
                user_msg = body["input"][1]["content"]
                # Verify untrusted data tags are present in LLM context!
                assert "<UNTRUSTED_DATA>" in user_msg
                return httpx.Response(
                    200,
                    json={
                        "output_text": "Your Safe-to-Spend stays calm and protected.",
                        "usage": {
                            "input_tokens": 2200,
                            "output_tokens": 110,
                            "input_tokens_details": {"cached_tokens": 1500},
                        },
                    },
                )

            oc_bad = OpenCodeGoClient(api_key="go-key", transport=httpx.MockTransport(fail_primary))
            cx_ok = CodexResponsesClient(api_key="sk-test", model="gpt-4.1-mini", transport=httpx.MockTransport(codex_ok))
            coach = FinancialCoach(clients=[oc_bad, cx_ok], on_completion=captured.append)

            qa = coach.answer_question(
                question="How does my buffer look?",
                budget=budget,
                impulses=impulses,
                adhd_stats=stats,
                hourly_wage_cents=3400,
                needs_triage_count=0,
                reset_done_this_week=True,
                intelligence_mode="hybrid",
            )
            assert qa.source == "openai_codex"
            assert qa.provider_used == "openai_codex"
            assert coach.last_completion is not None
            assert coach.last_completion.cached_tokens == 1500
            assert coach.last_completion.saved_microdollars == 450
            assert len(captured) == 1
        finally:
            svc.close()


class TestCodexCLIClient:
    def test_codex_cli_disabled_by_default_and_protocol_conformant(self):
        cli = CodexCLIClient()
        assert isinstance(cli, LLMClient)
        assert cli.provider_name == "codex_cli"
        assert cli.is_configured is False
        with pytest.raises(CodexCLIError):
            cli.chat_completion("sys", "usr")

    def test_codex_cli_sandboxed_empty_cwd_devnull_timeout_and_pii_secret_redaction(self, monkeypatch, tmp_path):
        import os
        import subprocess
        from pathlib import Path

        observed = {}
        monkeypatch.setenv("PLAID_SECRET", "plaid-super-secret-xyz")
        monkeypatch.setenv("GEMINI_API_KEY", "AIzaSySecretGemini123456789012345")
        monkeypatch.setenv("CHRONOX_FINANCE_SECRET_KEY", "vault-secret-signing-key")

        def fake_which(name: str):
            return "/usr/local/bin/codex"

        def fake_run(cmd, *, cwd, stdin, capture_output, text, timeout, env, check):
            observed["cmd"] = list(cmd)
            observed["cwd"] = cwd
            observed["cwd_contents"] = os.listdir(cwd)
            observed["stdin"] = stdin
            observed["timeout"] = timeout
            observed["env"] = dict(env)
            stdout_jsonl = "\n".join(
                [
                    json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": '{"ok": true}'}}),
                    json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1600, "cached_input_tokens": 1000, "output_tokens": 90}}),
                ]
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout_jsonl, stderr="")

        monkeypatch.setattr("shutil.which", fake_which)
        monkeypatch.setattr("subprocess.run", fake_run)

        cli = CodexCLIClient(enabled=True, model="gpt-5.3-codex", timeout_seconds=60.0)
        assert cli.is_configured is True

        sensitive_prompt = (
            "User email alice@example.com SSN 123-45-6789 acct 9876543210 "
            "key sk-proj-supersecretkey1234567890 access-production-12345678-1234-1234-1234-1234567890ab\n"
            "From: bank@chase.com\nSubject: Statement"
        )
        res = cli.chat_completion("System rules", sensitive_prompt, json_mode=True)

        # Verify exact sandbox flags, empty temp cwd, DEVNULL stdin, and 60s timeout
        assert observed["cmd"][0] == "/usr/local/bin/codex"
        assert observed["cmd"][1] == "exec"
        assert "--sandbox" in observed["cmd"]
        assert observed["cmd"][observed["cmd"].index("--sandbox") + 1] == "read-only"
        assert "--json" in observed["cmd"]
        assert observed["stdin"] is subprocess.DEVNULL
        assert observed["timeout"] == 60.0
        assert observed["cwd_contents"] == []
        assert str(Path.cwd()) != observed["cwd"]

        # Verify ChronoX / Plaid / Gemini secrets stripped from subprocess env
        assert "PLAID_SECRET" not in observed["env"]
        assert "GEMINI_API_KEY" not in observed["env"]
        assert "CHRONOX_FINANCE_SECRET_KEY" not in observed["env"]

        # Verify ALL PII, keys, tokens, and email headers redacted from CLI prompt!
        sent_prompt = observed["cmd"][-1]
        for leaked in ("alice@example.com", "123-45-6789", "9876543210", "sk-proj-supersecretkey", "access-production-", "bank@chase.com"):
            assert leaked not in sent_prompt

        # Verify JSONL output & real usage parsed
        assert res.content == '{"ok": true}'
        assert res.provider == "codex_cli"
        assert res.input_tokens == 1600
        assert res.cached_tokens == 1000
        assert res.output_tokens == 90
        assert res.saved_microdollars > 0

