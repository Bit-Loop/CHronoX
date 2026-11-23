"""
Google Gemini API Client (Generative Language API v1beta).

Supports Google AI Studio / Gemini Developer API:
- Base URL: https://generativelanguage.googleapis.com/v1beta (configurable, SSRF-validated)
- Auth: explicit api_key -> GEMINI_API_KEY -> GOOGLE_API_KEY
  ALWAYS passed exclusively in the `x-goog-api-key` HTTP header (never in URL query params)
- Live model discovery: GET /models filtered to `generateContent` capability (15-min TTL cache)
- Structured JSON mode via `generationConfig.responseMimeType = "application/json"`
- Clean thought-part filtering (`thought: true` skipped) & MAX_TOKENS single retry
- Content-hash TTL response caching to protect free/paid Gemini quotas
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from chronox_finance.ai.opencode_client import (
    CompletionResult,
    LLMProviderError,
    _TTLCache,
    calculate_token_costs,
    is_local_url,
)
from chronox_finance.security import validate_external_api_url

logger = logging.getLogger(__name__)

DEFAULT_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

_SAFE_MODEL_RE = re.compile(r"^[A-Za-z0-9._:-]+$")

CURATED_GEMINI_MODELS: List[Dict[str, str]] = [
    {
        "id": "gemini-2.5-flash",
        "family": "gemini-2.5",
        "protocol": "gemini",
        "note": "Fast, balanced Gemini 2.5 reasoning (recommended for daily ADHD & quant coaching)",
    },
    {
        "id": "gemini-2.5-pro",
        "family": "gemini-2.5",
        "protocol": "gemini",
        "note": "Deep multi-step financial, credit & debt strategy analysis",
    },
    {
        "id": "gemini-2.5-flash-lite",
        "family": "gemini-2.5",
        "protocol": "gemini",
        "note": "Ultra-fast, lowest-latency Gemini 2.5 coaching",
    },
    {
        "id": "gemini-2.0-flash",
        "family": "gemini-2.0",
        "protocol": "gemini",
        "note": "Reliable low-latency Gemini 2.0 general coaching",
    },
]


class GeminiError(LLMProviderError):
    """Raised when a Google Gemini API call fails."""


def _normalize_model_id(model: str) -> str:
    """Strip any leading 'models/' prefix and verify safe URL path token."""
    cleaned = (model or DEFAULT_GEMINI_MODEL).strip()
    if cleaned.startswith("models/"):
        cleaned = cleaned[len("models/") :]
    if not cleaned or not _SAFE_MODEL_RE.match(cleaned) or ".." in cleaned:
        raise GeminiError("Invalid Gemini model identifier.")
    return cleaned


def _extract_candidate_text(data: Dict[str, Any]) -> Tuple[str, str]:
    """
    Extract non-thought text parts and finishReason from a Gemini generateContent response.
    Skips internal thinking parts marked with `thought: true`.
    """
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        feedback = data.get("promptFeedback")
        if isinstance(feedback, dict) and feedback.get("blockReason"):
            raise GeminiError(f"Google Gemini blocked the prompt ({feedback['blockReason']}).")
        raise GeminiError("Google Gemini returned empty candidates.")

    first = candidates[0] if isinstance(candidates[0], dict) else {}
    finish_reason = str(first.get("finishReason") or "")
    content = first.get("content") if isinstance(first.get("content"), dict) else {}
    parts = content.get("parts") if isinstance(content.get("parts"), list) else []

    texts: List[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("thought") is True:
            continue
        text_val = part.get("text")
        if isinstance(text_val, str) and text_val:
            texts.append(text_val)

    return "".join(texts).strip(), finish_reason


def _safe_int(value: Any) -> int:
    try:
        if isinstance(value, bool):
            return 0
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _extract_gemini_usage(data: Dict[str, Any]) -> Tuple[int, int, int]:
    """
    Extract `(input_tokens, output_tokens, cached_tokens)` from Gemini's `usageMetadata`.
    Parses `promptTokenCount`, `candidatesTokenCount` (+ `thoughtsTokenCount`),
    and `cachedContentTokenCount`.
    """
    meta = data.get("usageMetadata")
    if not isinstance(meta, dict):
        return (0, 0, 0)
    input_tokens = _safe_int(meta.get("promptTokenCount"))
    cand_tokens = _safe_int(meta.get("candidatesTokenCount"))
    thought_tokens = _safe_int(meta.get("thoughtsTokenCount"))
    if cand_tokens > 0 or thought_tokens > 0:
        output_tokens = cand_tokens + thought_tokens
    else:
        total_tokens = _safe_int(meta.get("totalTokenCount"))
        output_tokens = max(0, total_tokens - input_tokens) if total_tokens > 0 else 0
    cached_tokens = _safe_int(meta.get("cachedContentTokenCount"))
    if input_tokens > 0:
        cached_tokens = min(cached_tokens, input_tokens)
    else:
        cached_tokens = 0
    return (input_tokens, output_tokens, cached_tokens)


class GeminiClient:
    """Client for Google's Gemini Generative Language API."""

    provider_name: str = "gemini"

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = DEFAULT_GEMINI_BASE_URL,
        model: str = DEFAULT_GEMINI_MODEL,
        timeout_seconds: float = 45.0,
        cache_ttl_seconds: int = 900,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        resolved_key = (
            api_key
            if api_key is not None
            else (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "")
        ).strip()
        # Strip any accidental query params (never allow ?key= in stored base_url) or /models suffix
        raw_url = (base_url or DEFAULT_GEMINI_BASE_URL).strip().split("?", 1)[0].rstrip("/")
        if raw_url.endswith("/models"):
            raw_url = raw_url[: -len("/models")]
        self.api_key = resolved_key
        self.base_url = validate_external_api_url(raw_url or DEFAULT_GEMINI_BASE_URL)
        self.model = _normalize_model_id(model or DEFAULT_GEMINI_MODEL)
        self.timeout_seconds = float(timeout_seconds)
        self.cache_ttl_seconds = int(cache_ttl_seconds)
        self._transport = transport
        self._cache = _TTLCache(max_items=128)

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _scrub_key(self, text: str) -> str:
        """Ensure the Gemini API key can never appear in logs or exception messages."""
        if self.api_key and self.api_key in text:
            return text.replace(self.api_key, "[REDACTED]")
        return text

    def _http_client(self) -> httpx.Client:
        return httpx.Client(
            timeout=self.timeout_seconds,
            transport=self._transport,
            follow_redirects=False,
        )

    def _headers(self, *, json_body: bool = False) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "x-goog-api-key": self.api_key,
            "Accept": "application/json",
            "User-Agent": "ChronoX-Finance/1.0",
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def list_models(self, force_refresh: bool = False) -> Dict[str, Any]:
        """
        Query live Gemini models from `GET {base_url}/models` using `x-goog-api-key`.
        Filters to models supporting `generateContent`, strips `models/` prefix,
        and caches for 15 minutes. Falls back gracefully to curated roster when offline/unconfigured.
        """
        cache_key = f"gemini_models:{self.base_url}:{bool(self.api_key)}"
        if not force_refresh:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

        if not self.is_configured:
            return {
                "live": False,
                "source": "curated_fallback",
                "provider": self.provider_name,
                "notice": "Set GEMINI_API_KEY or GOOGLE_API_KEY (or save your key in Settings) to fetch live Gemini models.",
                "models": list(CURATED_GEMINI_MODELS),
            }

        url = f"{self.base_url}/models"
        try:
            with self._http_client() as client:
                resp = client.get(url, headers=self._headers())
            if resp.status_code in (401, 403):
                raise GeminiError(
                    f"Google Gemini authentication failed ({resp.status_code}). Please verify your Gemini API key."
                )
            self._check_status(resp)
            payload = resp.json()
            raw_items = payload.get("models", []) if isinstance(payload, dict) else []
            models: List[Dict[str, str]] = []
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                methods = item.get("supportedGenerationMethods") or []
                if "generateContent" not in methods:
                    continue
                raw_name = str(item.get("name") or "").strip()
                if not raw_name:
                    continue
                mid = raw_name[len("models/") :] if raw_name.startswith("models/") else raw_name
                if not mid or not _SAFE_MODEL_RE.match(mid):
                    continue
                family = "gemini-2.5" if "2.5" in mid else ("gemini-2.0" if "2.0" in mid else "gemini")
                note = str(item.get("displayName") or item.get("description") or "Available in Google Gemini API")
                models.append(
                    {
                        "id": mid,
                        "family": family,
                        "protocol": "gemini",
                        "note": note[:140],
                    }
                )
            if not models:
                models = list(CURATED_GEMINI_MODELS)
            result = {
                "live": True,
                "source": "gemini_api",
                "provider": self.provider_name,
                "models": models,
            }
            self._cache.set(cache_key, result, ttl_seconds=900)
            return result
        except GeminiError:
            raise
        except Exception as exc:
            logger.warning("Gemini /models fetch fell back to curated list: %s", self._scrub_key(str(exc)))
            return {
                "live": False,
                "source": "curated_fallback",
                "provider": self.provider_name,
                "notice": f"Could not reach {self.base_url}/models ({type(exc).__name__}); showing curated Gemini models.",
                "models": list(CURATED_GEMINI_MODELS),
            }

    def chat_completion(
        self,
        system_prompt: str,
        user_prompt: str,
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 1800,
        json_mode: bool = False,
        use_cache: bool = True,
    ) -> CompletionResult:
        """Execute a completion against Google Gemini's `:generateContent` endpoint."""
        if not self.is_configured:
            raise GeminiError("Google Gemini API key is not configured.")

        target_model = _normalize_model_id(model or self.model)
        cache_hash = hashlib.sha256(
            json.dumps(
                {
                    "provider": self.provider_name,
                    "url": self.base_url,
                    "model": target_model,
                    "sys": system_prompt,
                    "usr": user_prompt,
                    "json": json_mode,
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

        if use_cache:
            cached = self._cache.get(cache_hash)
            if cached is not None:
                return CompletionResult(
                    content=cached["content"],
                    model=cached["model"],
                    protocol_used="gemini",
                    cached=True,
                    provider=self.provider_name,
                    input_tokens=0,
                    output_tokens=0,
                    cached_tokens=0,
                    cost_microdollars=0,
                    saved_microdollars=int(cached.get("full_uncached_cost_microdollars", 0)),
                    latency_ms=0,
                )

        url = f"{self.base_url}/models/{target_model}:generateContent"
        headers = self._headers(json_body=True)
        generation_config: Dict[str, Any] = {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        }
        if json_mode:
            generation_config["responseMimeType"] = "application/json"

        body: Dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": generation_config,
        }

        t0 = time.monotonic()
        try:
            with self._http_client() as client:
                resp = client.post(url, headers=headers, json=body)
                # Some preview/legacy models reject responseMimeType; retry once without it
                if resp.status_code == 400 and json_mode:
                    body["generationConfig"].pop("responseMimeType", None)
                    resp = client.post(url, headers=headers, json=body)

                self._check_status(resp)
                data = resp.json()
                text, finish_reason = _extract_candidate_text(data)

                # Thinking models (e.g. gemini-2.5-flash/pro) can consume maxOutputTokens on thoughts
                if not text and finish_reason == "MAX_TOKENS" and max_tokens < 4096:
                    body["generationConfig"]["maxOutputTokens"] = min(8192, max(max_tokens * 2, 3200))
                    resp = client.post(url, headers=headers, json=body)
                    self._check_status(resp)
                    data = resp.json()
                    text, finish_reason = _extract_candidate_text(data)
        except GeminiError:
            raise
        except httpx.TimeoutException as exc:
            raise GeminiError(f"Google Gemini request timed out after {self.timeout_seconds:.0f}s.") from exc
        except httpx.HTTPError as exc:
            msg = self._scrub_key(f"Google Gemini network error ({type(exc).__name__}): {exc}")
            raise GeminiError(msg) from exc
        except ValueError as exc:
            raise GeminiError(f"Google Gemini returned invalid JSON payload: {exc}") from exc

        latency_ms = max(0, int(round((time.monotonic() - t0) * 1000)))

        if not text:
            raise GeminiError(
                f"Google Gemini returned empty message content (finishReason={finish_reason or 'unknown'})."
            )

        in_tok, out_tok, cached_tok = _extract_gemini_usage(data if isinstance(data, dict) else {})
        cost_ud, saved_ud, full_uncached_ud = calculate_token_costs(
            model=target_model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cached_tokens=cached_tok,
            provider=self.provider_name,
            is_local_endpoint=is_local_url(self.base_url),
        )

        if use_cache and self.cache_ttl_seconds > 0:
            self._cache.set(
                cache_hash,
                {
                    "content": text,
                    "model": target_model,
                    "full_uncached_cost_microdollars": full_uncached_ud,
                },
                ttl_seconds=self.cache_ttl_seconds,
            )

        return CompletionResult(
            content=text,
            model=target_model,
            protocol_used="gemini",
            cached=False,
            provider=self.provider_name,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cached_tokens=cached_tok,
            cost_microdollars=cost_ud,
            saved_microdollars=saved_ud,
            latency_ms=latency_ms,
        )

    def _check_status(self, resp: httpx.Response) -> None:
        if resp.status_code in (401, 403):
            raise GeminiError(
                f"Google Gemini authentication failed ({resp.status_code}). Check your Gemini API key."
            )
        if resp.status_code == 429:
            raise GeminiError("Google Gemini rate limit or quota reached (429). Falling back cleanly.")
        if resp.status_code >= 400:
            detail = ""
            try:
                err_json = resp.json()
                if isinstance(err_json, dict) and isinstance(err_json.get("error"), dict):
                    detail = str(err_json["error"].get("message") or "")
            except Exception:
                detail = ""
            if not detail:
                detail = resp.text[:220]
            snippet = self._scrub_key(detail[:220].replace("\n", " "))
            raise GeminiError(f"Google Gemini HTTP {resp.status_code}: {snippet}")

    def test_connection(self) -> Dict[str, Any]:
        """Verify Google Gemini key & live connectivity without burning heavy tokens."""
        if not self.is_configured:
            return {
                "ok": False,
                "provider": self.provider_name,
                "message": "No Google Gemini API key configured.",
            }
        try:
            models_info = self.list_models(force_refresh=True)
            if models_info.get("live"):
                return {
                    "ok": True,
                    "provider": self.provider_name,
                    "message": f"Connected to Google Gemini ({len(models_info['models'])} models discovered).",
                    "models_count": len(models_info["models"]),
                    "active_model": self.model,
                }
            res = self.chat_completion(
                system_prompt="Reply with only the word OK.",
                user_prompt="ping",
                max_tokens=16,
                temperature=0.0,
                use_cache=False,
            )
            return {
                "ok": True,
                "provider": self.provider_name,
                "message": f"Connected to Google Gemini ({res.model}).",
                "active_model": res.model,
            }
        except Exception as exc:
            return {
                "ok": False,
                "provider": self.provider_name,
                "message": self._scrub_key(str(exc)),
            }
