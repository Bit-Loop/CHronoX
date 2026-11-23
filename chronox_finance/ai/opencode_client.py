"""
OpenCode Go Subscription API Client.

Supports the official OpenCode Go subscription gateway:
- Base URL: https://opencode.ai/zen/go/v1 (configurable)
- Auth: OPENCODE_GO_API_KEY (or fallback OPENCODE_API_KEY) via Bearer token
- Live model discovery: GET /models (with 15-minute cache)
- Dual protocol dispatch:
  * OpenAI-compatible POST /chat/completions
  * Anthropic-compatible POST /messages (for Go models routed via Anthropic protocol)
- Session tracking via `x-opencode-session` header
- Content-hash response caching to protect rolling 5-hour / weekly Go allowances
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable

import httpx

from chronox_finance.security import validate_external_api_url

logger = logging.getLogger(__name__)

DEFAULT_OPENCODE_GO_BASE_URL = "https://opencode.ai/zen/go/v1"

# Curated fallback roster matching active OpenCode Go model catalog
CURATED_OPENCODE_GO_MODELS: List[Dict[str, str]] = [
    {"id": "glm-5.3-flash", "family": "glm", "protocol": "openai", "note": "Fast & efficient (recommended for daily ADHD coaching)"},
    {"id": "deepseek-v4.1-flash", "family": "deepseek-flash", "protocol": "openai", "note": "High-speed structured JSON reasoning"},
    {"id": "glm-5.3", "family": "glm", "protocol": "openai", "note": "Strong reasoning for budget deep-dives"},
    {"id": "kimi-k3", "family": "kimi", "protocol": "openai", "note": "Warm, nuanced conversational coaching"},
    {"id": "qwen3.8-flash", "family": "qwen", "protocol": "openai", "note": "Fast balanced planning & analytical clarity"},
    {"id": "qwen3.8-max", "family": "qwen", "protocol": "openai", "note": "Deep multi-step financial planning"},
    {"id": "minimax-m3.1-flash", "family": "minimax", "protocol": "anthropic", "note": "Fast conversational Go model"},
    {"id": "minimax-m3", "family": "minimax", "protocol": "anthropic", "note": "Latest MiniMax model on OpenCode Go"},
    {"id": "claude-haiku-5-5", "family": "claude", "protocol": "anthropic", "note": "Crisp empathetic coaching"},
]


class LLMProviderError(Exception):
    """Base error raised when an external LLM provider call fails."""


class OpenCodeGoError(LLMProviderError):
    """Raised when an OpenCode Go API call fails."""


@dataclass
class CompletionResult:
    content: str
    model: str
    protocol_used: str
    cached: bool = False
    provider: str = ""


@runtime_checkable
class LLMClient(Protocol):
    """Common protocol shared by OpenCodeGoClient and GeminiClient."""

    provider_name: str
    model: str

    @property
    def is_configured(self) -> bool: ...

    def list_models(self, force_refresh: bool = False) -> Dict[str, Any]: ...

    def chat_completion(
        self,
        system_prompt: str,
        user_prompt: str,
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 1800,
        json_mode: bool = False,
        use_cache: bool = True,
    ) -> CompletionResult: ...

    def test_connection(self) -> Dict[str, Any]: ...


class _TTLCache:
    """Thread-safe bounded in-memory TTL cache."""

    def __init__(self, max_items: int = 128):
        self.max_items = max_items
        self._data: Dict[str, Tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            if not entry:
                return None
            expires_at, value = entry
            if now >= expires_at:
                self._data.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        now = time.monotonic()
        with self._lock:
            if len(self._data) >= self.max_items:
                # Evict oldest expiry
                oldest_key = min(self._data, key=lambda k: self._data[k][0])
                self._data.pop(oldest_key, None)
            self._data[key] = (now + ttl_seconds, value)


class OpenCodeGoClient:
    """Client for the user's OpenCode Go subscription API."""

    provider_name: str = "opencode_go"

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = DEFAULT_OPENCODE_GO_BASE_URL,
        model: str = "glm-5.3-flash",
        protocol: str = "auto",
        timeout_seconds: float = 45.0,
        cache_ttl_seconds: int = 900,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        resolved_key = (
            api_key
            if api_key is not None
            else (os.getenv("OPENCODE_GO_API_KEY") or os.getenv("OPENCODE_API_KEY") or "")
        ).strip()
        raw_url = (base_url or DEFAULT_OPENCODE_GO_BASE_URL).strip().rstrip("/")
        for suffix in ("/chat/completions", "/messages", "/models"):
            if raw_url.endswith(suffix):
                raw_url = raw_url[: -len(suffix)]
                break
        self.api_key = resolved_key
        self.base_url = validate_external_api_url(raw_url or DEFAULT_OPENCODE_GO_BASE_URL)
        self.model = (model or "glm-5.3-flash").strip()
        self.protocol = (protocol or "auto").strip().lower()
        self.timeout_seconds = float(timeout_seconds)
        self.cache_ttl_seconds = int(cache_ttl_seconds)
        self.session_id = f"chronox-finance-{uuid.uuid4().hex[:12]}"
        self._transport = transport
        self._cache = _TTLCache(max_items=128)

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _scrub_key(self, text: str) -> str:
        if self.api_key and self.api_key in text:
            return text.replace(self.api_key, "[REDACTED]")
        return text

    def _http_client(self) -> httpx.Client:
        return httpx.Client(
            timeout=self.timeout_seconds,
            transport=self._transport,
            follow_redirects=False,
        )

    def _choose_protocols(self, model: str) -> List[str]:
        if self.protocol in ("openai", "anthropic"):
            return [self.protocol]
        lower = model.lower()
        if lower.startswith(("minimax", "claude")):
            return ["anthropic", "openai"]
        return ["openai", "anthropic"]

    def list_models(self, force_refresh: bool = False) -> Dict[str, Any]:
        """
        Query live model catalog from `GET {base_url}/models`.
        Falls back gracefully to the curated OpenCode Go roster if unconfigured or offline.
        """
        cache_key = f"models:{self.base_url}:{bool(self.api_key)}"
        if not force_refresh:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

        if not self.is_configured:
            return {
                "live": False,
                "source": "curated_fallback",
                "provider": self.provider_name,
                "notice": "Set OPENCODE_GO_API_KEY (or save your key in Settings) to fetch your live OpenCode Go model catalog.",
                "models": list(CURATED_OPENCODE_GO_MODELS),
            }

        url = f"{self.base_url}/models"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "x-opencode-session": self.session_id,
            "Accept": "application/json",
            "User-Agent": "ChronoX-Finance/1.0",
        }
        try:
            with self._http_client() as client:
                resp = client.get(url, headers=headers)
            if resp.status_code == 401:
                raise OpenCodeGoError("Authentication failed (401). Please verify your OpenCode Go API key.")
            resp.raise_for_status()
            payload = resp.json()
            raw_items = payload.get("data", []) if isinstance(payload, dict) else []
            models = []
            for item in raw_items:
                if isinstance(item, dict) and item.get("id"):
                    mid = str(item["id"])
                    proto = "anthropic" if mid.lower().startswith(("minimax", "claude")) else "openai"
                    models.append(
                        {
                            "id": mid,
                            "family": str(item.get("family") or item.get("owned_by") or "opencode-go"),
                            "protocol": proto,
                            "note": str(item.get("name") or "Available in your OpenCode Go subscription"),
                        }
                    )
            if not models:
                models = list(CURATED_OPENCODE_GO_MODELS)
            result = {"live": True, "source": "opencode_go_api", "provider": self.provider_name, "models": models}
            self._cache.set(cache_key, result, ttl_seconds=900)
            return result
        except OpenCodeGoError:
            raise
        except Exception as exc:
            logger.warning("OpenCode Go /models fetch fell back to curated list: %s", self._scrub_key(str(exc)))
            return {
                "live": False,
                "source": "curated_fallback",
                "provider": self.provider_name,
                "notice": f"Could not reach {self.base_url}/models ({type(exc).__name__}); showing curated Go models.",
                "models": list(CURATED_OPENCODE_GO_MODELS),
            }

    def chat_completion(
        self,
        system_prompt: str,
        user_prompt: str,
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 900,
        json_mode: bool = False,
        use_cache: bool = True,
    ) -> CompletionResult:
        """Execute a grounded completion against the user's OpenCode Go subscription."""
        if not self.is_configured:
            raise OpenCodeGoError("OpenCode Go API key is not configured.")

        target_model = (model or self.model).strip()
        cache_hash = hashlib.sha256(
            json.dumps(
                {
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
                    protocol_used=cached["protocol_used"],
                    cached=True,
                    provider=self.provider_name,
                )

        protocols = self._choose_protocols(target_model)
        last_error: Optional[Exception] = None

        for proto in protocols:
            try:
                if proto == "openai":
                    text = self._call_openai_compat(
                        model=target_model,
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        json_mode=json_mode,
                    )
                else:
                    text = self._call_anthropic_compat(
                        model=target_model,
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        temperature=temperature,
                        max_tokens=max_tokens,
                    )
                if use_cache and self.cache_ttl_seconds > 0:
                    self._cache.set(
                        cache_hash,
                        {"content": text, "model": target_model, "protocol_used": proto},
                        ttl_seconds=self.cache_ttl_seconds,
                    )
                return CompletionResult(
                    content=text,
                    model=target_model,
                    protocol_used=proto,
                    cached=False,
                    provider=self.provider_name,
                )
            except httpx.TimeoutException as exc:
                raise OpenCodeGoError(f"OpenCode Go request timed out after {self.timeout_seconds:.0f}s.") from exc
            except httpx.HTTPError as exc:
                msg = self._scrub_key(f"OpenCode Go network error ({type(exc).__name__}): {exc}")
                last_error = OpenCodeGoError(msg)
                logger.info("Protocol %s network error for %s (%s); trying fallback if available.", proto, target_model, msg)
            except OpenCodeGoError as exc:
                last_error = exc
                # Don't retry alternate protocol on auth errors or quota exhaustion
                msg = str(exc).lower()
                if "401" in msg or "unauthorized" in msg or "quota" in msg or "429" in msg:
                    raise
                logger.info("Protocol %s failed for %s (%s); trying fallback if available.", proto, target_model, exc)

        raise last_error or OpenCodeGoError("OpenCode Go request failed")

    def _call_openai_compat(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> str:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "x-opencode-session": self.session_id,
            "User-Agent": "ChronoX-Finance/1.0",
        }
        body: Dict[str, Any] = {
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        with self._http_client() as client:
            resp = client.post(url, headers=headers, json=body)
            # Some open-weight routes reject response_format; retry once without it
            if resp.status_code == 400 and json_mode:
                body.pop("response_format", None)
                resp = client.post(url, headers=headers, json=body)

            self._check_status(resp)
            data = resp.json()
            choices = data.get("choices") or []
            if not choices:
                raise OpenCodeGoError("OpenCode Go returned empty choices")

            msg = choices[0].get("message", {}) or {}
            content = (msg.get("content") or "").strip()

            # Reasoning models (e.g. glm-5.3-flash) may exhaust max_tokens inside reasoning_content
            if not content and choices[0].get("finish_reason") == "length" and max_tokens < 4000:
                body["max_tokens"] = min(4096, max(max_tokens * 2, 3200))
                resp = client.post(url, headers=headers, json=body)
                self._check_status(resp)
                data = resp.json()
                choices = data.get("choices") or []
                msg = (choices[0].get("message", {}) or {}) if choices else {}
                content = (msg.get("content") or "").strip()

        if not content:
            raise OpenCodeGoError("OpenCode Go returned empty message content")
        return str(content)

    def _call_anthropic_compat(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        # Handle both .../zen/go/v1 and .../zen/go base URLs cleanly
        base = self.base_url.rstrip("/")
        url = f"{base}/messages" if base.endswith("/v1") else f"{base}/v1/messages"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "x-opencode-session": self.session_id,
            "User-Agent": "ChronoX-Finance/1.0",
        }
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        with self._http_client() as client:
            resp = client.post(url, headers=headers, json=body)

        self._check_status(resp)
        data = resp.json()
        blocks = data.get("content") or []
        texts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
        combined = "\n".join(t for t in texts if t).strip()
        if not combined:
            raise OpenCodeGoError("OpenCode Go Anthropic route returned empty content")
        return combined

    def _check_status(self, resp: httpx.Response) -> None:
        if resp.status_code == 401:
            raise OpenCodeGoError("OpenCode Go authentication failed (401). Check your API key.")
        if resp.status_code == 429:
            raise OpenCodeGoError("OpenCode Go rolling quota or rate limit reached (429). Using local verified engine.")
        if resp.status_code >= 400:
            snippet = self._scrub_key(resp.text[:220].replace("\n", " "))
            raise OpenCodeGoError(f"OpenCode Go HTTP {resp.status_code}: {snippet}")

    def test_connection(self) -> Dict[str, Any]:
        """Verify OpenCode Go key & live connectivity without burning heavy tokens."""
        if not self.is_configured:
            return {"ok": False, "provider": self.provider_name, "message": "No OpenCode Go API key configured."}
        try:
            models_info = self.list_models(force_refresh=True)
            if models_info.get("live"):
                return {
                    "ok": True,
                    "provider": self.provider_name,
                    "message": f"Connected to OpenCode Go ({len(models_info['models'])} models discovered).",
                    "models_count": len(models_info["models"]),
                    "active_model": self.model,
                }
            # Try tiny completion fallback if /models isn't exposed on a custom gateway
            res = self.chat_completion(
                system_prompt="Reply with only the word OK.",
                user_prompt="ping",
                max_tokens=8,
                temperature=0.0,
                use_cache=False,
            )
            return {
                "ok": True,
                "provider": self.provider_name,
                "message": f"Connected via {res.protocol_used} protocol ({res.model}).",
                "active_model": res.model,
            }
        except Exception as exc:
            return {"ok": False, "provider": self.provider_name, "message": self._scrub_key(str(exc))}
