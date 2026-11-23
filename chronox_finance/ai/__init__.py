"""Grounded Multi-Provider AI Financial Coaching (OpenCode Go, Google Gemini, & Optimal Quant Math)."""

from chronox_finance.ai.coach import FinancialCoach
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
    CompletionResult,
    LLMClient,
    LLMProviderError,
    OpenCodeGoClient,
    OpenCodeGoError,
)
from chronox_finance.ai.validator import (
    SuggestionValidator,
    ValidationReport,
    extract_json_object,
    sanitize_rsd_tone,
)

__all__ = [
    "CURATED_GEMINI_MODELS",
    "CURATED_OPENCODE_GO_MODELS",
    "DEFAULT_GEMINI_BASE_URL",
    "DEFAULT_GEMINI_MODEL",
    "DEFAULT_OPENCODE_GO_BASE_URL",
    "CompletionResult",
    "FinancialCoach",
    "GeminiClient",
    "GeminiError",
    "LLMClient",
    "LLMProviderError",
    "OpenCodeGoClient",
    "OpenCodeGoError",
    "SuggestionValidator",
    "ValidationReport",
    "extract_json_object",
    "sanitize_rsd_tone",
]
