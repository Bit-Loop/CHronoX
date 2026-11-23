"""
Grounded Multi-Provider AI Financial Coaching & Agentic Tool-Calling Planner.

Supports:
- OpenCode Go (`OpenCodeGoClient`)
- Google Gemini (`GeminiClient`)
- OpenAI / Codex Responses API & Durable Chat-Wrapper (`CodexResponsesClient`)
- Opt-in Sandboxed Local Codex CLI (`CodexCLIClient`)
- Pure Optimal Quantitative Math & Deterministic Agent Fallback (`QuantMathEngine` + `plan_deterministic_actions`)
"""

from chronox_finance.ai.agent_tools import (
    ALLOWED_AGENT_ACTION_TYPES,
    FORBIDDEN_AGENT_ACTION_TYPES,
    MAX_AGENT_BATCH_ACTIONS,
    AgentActionPlanner,
    AgentLedgerContext,
    AgentPlanResult,
    AgentValidationReport,
    ValidatedAgentAction,
    compute_holding_value_cents,
    compute_portfolio_analysis,
    plan_deterministic_actions,
    validate_agent_batch,
)
from chronox_finance.ai.coach import FinancialCoach
from chronox_finance.ai.codex_client import (
    CHRONOX_STATIC_KNOWLEDGE_PREFIX,
    CURATED_CHAT_WRAPPER_MODELS,
    CURATED_OPENAI_CODEX_MODELS,
    DEFAULT_CODEX_CLI_MODEL,
    DEFAULT_CODEX_MODEL,
    DEFAULT_OPENAI_BASE_URL,
    DEFAULT_WRAPPER_BASE_URL,
    DEFAULT_WRAPPER_MODEL,
    CodexCLIClient,
    CodexCLIError,
    CodexError,
    CodexResponsesClient,
    build_cache_optimized_system_prompt,
    estimate_token_count,
    sanitize_cli_prompt,
)
from chronox_finance.ai.gemini_client import (
    CURATED_GEMINI_MODELS,
    DEFAULT_GEMINI_BASE_URL,
    DEFAULT_GEMINI_MODEL,
    GeminiClient,
    GeminiError,
)
from chronox_finance.ai.opencode_client import (
    CURATED_OPENCODE_GO_MODELS,
    DEFAULT_OPENCODE_GO_BASE_URL,
    MODEL_PRICING_MICRODOLLARS_PER_1M,
    CompletionResult,
    LLMClient,
    LLMProviderError,
    OpenCodeGoClient,
    OpenCodeGoError,
    calculate_token_costs,
    extract_anthropic_usage,
    extract_openai_usage,
    lookup_model_pricing,
)
from chronox_finance.ai.validator import (
    SuggestionValidator,
    ValidationReport,
    extract_json_object,
    sanitize_prompt_injection,
    sanitize_rsd_tone,
    strip_untrusted_delimiters,
    wrap_untrusted_data,
)

__all__ = [
    "ALLOWED_AGENT_ACTION_TYPES",
    "CHRONOX_STATIC_KNOWLEDGE_PREFIX",
    "CURATED_CHAT_WRAPPER_MODELS",
    "CURATED_GEMINI_MODELS",
    "CURATED_OPENAI_CODEX_MODELS",
    "CURATED_OPENCODE_GO_MODELS",
    "DEFAULT_CODEX_CLI_MODEL",
    "DEFAULT_CODEX_MODEL",
    "DEFAULT_GEMINI_BASE_URL",
    "DEFAULT_GEMINI_MODEL",
    "DEFAULT_OPENAI_BASE_URL",
    "DEFAULT_OPENCODE_GO_BASE_URL",
    "DEFAULT_WRAPPER_BASE_URL",
    "DEFAULT_WRAPPER_MODEL",
    "FORBIDDEN_AGENT_ACTION_TYPES",
    "MAX_AGENT_BATCH_ACTIONS",
    "MODEL_PRICING_MICRODOLLARS_PER_1M",
    "AgentActionPlanner",
    "AgentLedgerContext",
    "AgentPlanResult",
    "AgentValidationReport",
    "CodexCLIClient",
    "CodexCLIError",
    "CodexError",
    "CodexResponsesClient",
    "CompletionResult",
    "FinancialCoach",
    "GeminiClient",
    "GeminiError",
    "LLMClient",
    "LLMProviderError",
    "OpenCodeGoClient",
    "OpenCodeGoError",
    "SuggestionValidator",
    "ValidatedAgentAction",
    "ValidationReport",
    "build_cache_optimized_system_prompt",
    "calculate_token_costs",
    "compute_holding_value_cents",
    "compute_portfolio_analysis",
    "estimate_token_count",
    "extract_anthropic_usage",
    "extract_json_object",
    "extract_openai_usage",
    "lookup_model_pricing",
    "plan_deterministic_actions",
    "sanitize_cli_prompt",
    "sanitize_prompt_injection",
    "sanitize_rsd_tone",
    "strip_untrusted_delimiters",
    "validate_agent_batch",
    "wrap_untrusted_data",
]
