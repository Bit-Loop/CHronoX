"""
Automagic Local AI CLI Discovery, Registry & Sandboxed Adapters.

Provides zero-config discovery and hardened `LLMClient`-conforming adapters for
installed local AI CLIs without requiring pasted API keys or localhost URLs:
1. `ClaudeCLIClient` (`provider_name = "claude_cli"`) — `claude -p --output-format json` (stdin piped)
2. `OpenCodeCLIClient` (`provider_name = "opencode_cli"`) — `opencode run --format json --archived`
3. `CodexCLIClient` (`provider_name = "codex_cli"`) — imported/re-exported from `codex_client.py`
4. `AntigravityCLIClient` (`provider_name = "agy_cli"`) — `agy -p ... --output-format json`

Security guarantees shared across every adapter:
- Runs inside an isolated empty `tempfile.TemporaryDirectory` (`cwd`) so no local
  workspace files or SQLite databases can be inspected.
- Uses least-privilege sanitized subprocess environments (`build_safe_cli_env`) that
  never leak `CHRONOX_*` vault, Plaid, SimpleFIN, broker, or IMAP secrets.
- Scrubs PII, private keys, API keys, access tokens, and email headers via
  `sanitize_cli_prompt()` prior to every invocation.
- Caches responses in `_TTLCache` and remembers transient auth failures (e.g. expired
  OAuth `401`) for 5 minutes so `auto` failover seamlessly lands on the first working CLI.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import httpx

from chronox_finance.ai.codex_client import (
    _BASE_SAFE_CLI_ENV_ALLOWLIST,
    _PROVIDER_CLI_ENV_ALLOWLISTS,
    _REAL_SUBPROCESS_RUN,
    _SAFE_CLI_ENV_ALLOWLIST,
    _SAFE_CLI_MODEL_RE,
    DEFAULT_CLI_PROBE_CACHE_TTL_SECONDS,
    DEFAULT_CODEX_CLI_MODEL,
    OPENCODE_SAFE_CONFIG_CONTENT,
    CodexCLIClient,
    CodexCLIError,
    build_augmented_cli_path,
    build_safe_cli_env,
    get_cli_search_dirs,
    is_cli_auth_error,
    resolve_cli_binary,
    run_isolated_cli_subprocess,
    sanitize_cli_prompt,
)
from chronox_finance.ai.opencode_client import (
    CompletionResult,
    LLMClient,
    LLMProviderError,
    _safe_int,
    _TTLCache,
    calculate_token_costs,
    extract_anthropic_usage,
)

logger = logging.getLogger(__name__)

DEFAULT_OPENCODE_CLI_MODEL = ""
DEFAULT_CLAUDE_CLI_MODEL = ""
DEFAULT_AGY_CLI_MODEL = "gemini-3.8-flash-low"
DEFAULT_LOCAL_CLI_TIMEOUT_SECONDS = 60.0
DEFAULT_AGY_CLI_TIMEOUT_SECONDS = 65.0
DEFAULT_AUTH_FAILURE_COOLDOWN_SECONDS = 300.0
_SAFE_CLI_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,160}$")


def _sanitize_cli_session_id(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    cleaned = str(raw).strip()
    if cleaned and _SAFE_CLI_SESSION_ID_RE.match(cleaned):
        return cleaned
    return None


def _extract_opencode_session_id(stdout: str) -> Optional[str]:
    for line in (stdout or "").splitlines():
        stripped = line.strip()
        if not stripped or not stripped.startswith("{"):
            continue
        try:
            ev = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        cand = ev.get("sessionID") or ev.get("session_id")
        if not cand and isinstance(ev.get("part"), dict):
            cand = ev["part"].get("sessionID") or ev["part"].get("session_id")
        valid = _sanitize_cli_session_id(cand)
        if valid:
            return valid
    return None


def _extract_agy_conversation_id(stdout: str) -> Optional[str]:
    data = _extract_json_object(stdout or "")
    if not isinstance(data, dict):
        return None
    for key in ("conversation_id", "conversationId", "session_id", "sessionId"):
        valid = _sanitize_cli_session_id(data.get(key))
        if valid:
            return valid
    return None

LOCAL_CLI_PRIORITY: Tuple[str, ...] = (
    "claude_cli",
    "opencode_cli",
    "codex_cli",
    "agy_cli",
)

LOCAL_CLI_SPECS: Dict[str, Dict[str, Any]] = {
    "claude_cli": {
        "display_name": "Claude Code CLI",
        "binary_name": "claude",
        "default_model": DEFAULT_CLAUDE_CLI_MODEL,
        "priority": 1,
    },
    "opencode_cli": {
        "display_name": "OpenCode CLI",
        "binary_name": "opencode",
        "default_model": DEFAULT_OPENCODE_CLI_MODEL,
        "priority": 2,
    },
    "codex_cli": {
        "display_name": "Codex CLI",
        "binary_name": "codex",
        "default_model": DEFAULT_CODEX_CLI_MODEL,
        "priority": 3,
    },
    "agy_cli": {
        "display_name": "Antigravity CLI",
        "binary_name": "agy",
        "default_model": DEFAULT_AGY_CLI_MODEL,
        "priority": 4,
    },
}


class LocalCLIError(LLMProviderError):
    """Base error raised when a sandboxed local AI CLI adapter fails."""


class OpenCodeCLIError(LocalCLIError):
    """Raised when the sandboxed local `opencode` CLI adapter fails."""


class ClaudeCLIError(LocalCLIError):
    """Raised when the sandboxed local `claude` CLI adapter fails."""


class AntigravityCLIError(LocalCLIError):
    """Raised when the sandboxed local `agy` (Antigravity) CLI adapter fails."""


def _usd_to_microdollars(value: Any) -> Optional[int]:
    """Convert a provider-reported USD cost float/string to exact integer microdollars."""
    if value is None or isinstance(value, bool):
        return None
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if dec < 0:
        return 0
    return int((dec * Decimal(1_000_000)).quantize(Decimal("1")))


def _extract_json_object(stdout: str) -> Optional[Dict[str, Any]]:
    """Parse a JSON object from CLI stdout, tolerating leading/trailing non-JSON log lines."""
    raw = (stdout or "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    for line in reversed(raw.splitlines()):
        stripped = line.strip()
        if not stripped or not stripped.startswith("{"):
            continue
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue

    first_brace = raw.find("{")
    last_brace = raw.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        try:
            parsed = json.loads(raw[first_brace : last_brace + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return None


def _run_cli_version_probe(
    binary: str,
    *,
    provider_id: str,
    timeout_seconds: float = 8.0,
) -> Tuple[bool, str]:
    """Run `[binary, "--version"]` inside an empty temp dir with least-privilege env."""
    extra = [os.path.dirname(binary)] if os.path.dirname(binary) else []
    env = build_safe_cli_env(provider_id, extra_path_dirs=extra)
    try:
        with tempfile.TemporaryDirectory(prefix=f"chronox_{provider_id}_ver_") as empty_cwd:
            proc = subprocess.run(  # noqa: S603
                [binary, "--version"],
                cwd=empty_cwd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env=env,
                check=False,
            )
        out = (proc.stdout or proc.stderr or "").strip()
        first_line = out.splitlines()[0].strip() if out else ""
        return (proc.returncode == 0, first_line[:120])
    except Exception as exc:
        return (False, str(exc)[:120])


# ---------------------------------------------------------------------------
# Pure Output Parsers (OpenCode JSONL, Claude JSON, Antigravity JSON)
# ---------------------------------------------------------------------------


def _parse_opencode_cli_jsonl(stdout: str) -> Tuple[str, int, int, int, Optional[int], str]:
    """
    Parse JSONL events from `opencode run --format json --archived`.

    - Joins `ev["part"]["text"]` for `ev.get("type") == "text"`.
    - Extracts `input_tokens`, `output_tokens`, `cached_tokens` (`cache.read`),
      and `cost` from `ev.get("type") == "step_finish"`.
    - Rejects `ev.get("type") == "error"` by raising `OpenCodeCLIError`.

    Returns `(content, input_tokens, output_tokens, cached_tokens, reported_cost_ud, model)`.
    """
    raw = (stdout or "").strip()
    if not raw:
        return ("", 0, 0, 0, None, "")

    text_chunks: List[str] = []
    in_tok = 0
    out_tok = 0
    cached_tok = 0
    total_cost_ud: Optional[int] = None
    resolved_model = ""

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            ev = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue

        ev_type = str(ev.get("type") or "").strip().lower()
        if ev_type == "error":
            err_msg = ""
            err_obj = ev.get("error")
            if isinstance(err_obj, dict):
                data_obj = err_obj.get("data") if isinstance(err_obj.get("data"), dict) else {}
                err_msg = str(
                    data_obj.get("message")
                    or err_obj.get("message")
                    or err_obj.get("name")
                    or ""
                ).strip()
            elif isinstance(err_obj, str):
                err_msg = err_obj.strip()
            if not err_msg:
                part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
                err_msg = str(ev.get("message") or part.get("error") or part.get("text") or "Unknown error").strip()
            raise OpenCodeCLIError(f"OpenCode CLI error: {sanitize_cli_prompt(err_msg, max_len=240)}")

        if ev_type == "text":
            part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
            chunk = part.get("text")
            if not isinstance(chunk, str):
                chunk = ev.get("text")
            if isinstance(chunk, str) and chunk:
                text_chunks.append(chunk)
            if not resolved_model and isinstance(part.get("model"), str) and part["model"].strip():
                resolved_model = part["model"].strip()
            continue

        if ev_type == "step_finish":
            part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
            tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
            in_tok += _safe_int(tokens.get("input"))
            out_tok += _safe_int(tokens.get("output"))
            cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
            cached_tok += _safe_int(cache.get("read"))
            step_cost = _usd_to_microdollars(part.get("cost"))
            if step_cost is not None:
                total_cost_ud = (total_cost_ud or 0) + step_cost
            if not resolved_model:
                cand_model = part.get("model") or ev.get("model")
                if isinstance(cand_model, str) and cand_model.strip():
                    resolved_model = cand_model.strip()

    if in_tok > 0:
        cached_tok = min(cached_tok, in_tok)
    content = "".join(text_chunks).strip()
    return (content, in_tok, out_tok, cached_tok, total_cost_ud, resolved_model)


def _parse_claude_cli_json(stdout: str) -> Tuple[str, int, int, int, Optional[int], str]:
    """
    Parse JSON result from `claude -p --output-format json ...`.

    Checks `not data.get("is_error")`, extracts `data["result"]`,
    `usage.input_tokens`, `usage.output_tokens`, `usage.cache_read_input_tokens`,
    and optional `total_cost_usd`.
    """
    data = _extract_json_object(stdout)
    if data is None:
        raise ClaudeCLIError("Claude CLI returned invalid or non-JSON output.")

    if data.get("is_error"):
        err_msg = str(data.get("result") or data.get("error") or data.get("message") or "Unknown error").strip()
        raise ClaudeCLIError(f"Claude CLI error: {sanitize_cli_prompt(err_msg, max_len=240)}")

    result_text = str(data.get("result") or "").strip()
    if not result_text:
        raise ClaudeCLIError("Claude CLI returned empty result.")

    in_tok, out_tok, cached_tok = extract_anthropic_usage(data)
    reported_cost_ud = _usd_to_microdollars(
        data.get("total_cost_usd") if "total_cost_usd" in data else data.get("cost_usd")
    )

    resolved_model = ""
    if isinstance(data.get("model"), str) and data["model"].strip():
        resolved_model = data["model"].strip()
    elif isinstance(data.get("modelUsage"), dict) and data["modelUsage"]:
        first_key = next(iter(data["modelUsage"].keys()), "")
        if isinstance(first_key, str) and first_key.strip():
            resolved_model = first_key.strip()

    return (result_text, in_tok, out_tok, cached_tok, reported_cost_ud, resolved_model)


def _parse_agy_cli_json(stdout: str) -> Tuple[str, int, int, int, str]:
    """
    Parse JSON result from `agy -p ... --output-format json ...`.

    Verifies `status == "SUCCESS"`, extracts `response` and
    `usage.{input_tokens, output_tokens, cache_read_tokens}`.
    """
    data = _extract_json_object(stdout)
    if data is None:
        raise AntigravityCLIError("Antigravity CLI returned invalid or non-JSON output.")

    status = str(data.get("status") or "").strip().upper()
    if status != "SUCCESS":
        err_msg = str(data.get("error") or data.get("message") or data.get("response") or "Request failed").strip()
        raise AntigravityCLIError(
            f"Antigravity CLI error ({status or 'FAILED'}): {sanitize_cli_prompt(err_msg, max_len=240)}"
        )

    response_text = str(data.get("response") or "").strip()
    if not response_text:
        raise AntigravityCLIError("Antigravity CLI returned empty response.")

    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    in_tok = _safe_int(usage.get("input_tokens"))
    out_tok = _safe_int(usage.get("output_tokens"))
    cached_tok = _safe_int(
        usage.get("cache_read_tokens")
        if usage.get("cache_read_tokens") is not None
        else usage.get("cached_tokens")
    )
    if in_tok > 0:
        cached_tok = min(cached_tok, in_tok)
    else:
        cached_tok = 0

    resolved_model = str(data.get("model") or "").strip()
    return (response_text, in_tok, out_tok, cached_tok, resolved_model)


# ---------------------------------------------------------------------------
# Shared Base Class for New Sandboxed Local CLI Adapters
# ---------------------------------------------------------------------------


class _BaseLocalCLIClient:
    """Shared caching, env sanitization, binary resolution, and auth-probe foundation."""

    provider_name: str = ""
    default_binary_name: str = ""
    default_model_value: str = ""
    error_cls: type[LocalCLIError] = LocalCLIError
    display_label: str = "Local AI CLI"

    def __init__(
        self,
        *,
        enabled: bool = True,
        model: Optional[str] = None,
        binary: Optional[str] = None,
        timeout_seconds: float = DEFAULT_LOCAL_CLI_TIMEOUT_SECONDS,
        cache_ttl_seconds: int = 900,
        probe_cache_ttl_seconds: int = DEFAULT_CLI_PROBE_CACHE_TTL_SECONDS,
        on_auth_failure: Optional[Callable[[str, str], None]] = None,
        on_auth_success: Optional[Callable[[str], None]] = None,
        session_id: Optional[str] = None,
        on_session_id: Optional[Callable[[str, Optional[str]], None]] = None,
    ) -> None:
        self.enabled = bool(enabled)
        cleaned_model = (self.default_model_value if model is None else model).strip()
        if cleaned_model and not _SAFE_CLI_MODEL_RE.match(cleaned_model):
            raise ValueError(f"Invalid {self.display_label} model identifier.")
        self.model = cleaned_model
        self.binary = (binary or self.default_binary_name).strip()
        self.timeout_seconds = max(5.0, min(180.0, float(timeout_seconds)))
        self.cache_ttl_seconds = int(cache_ttl_seconds)
        self.probe_cache_ttl_seconds = max(10, int(probe_cache_ttl_seconds))
        self._on_auth_failure = on_auth_failure
        self._on_auth_success = on_auth_success
        self._session_id: Optional[str] = _sanitize_cli_session_id(session_id)
        self._on_session_id = on_session_id
        self._cache = _TTLCache(max_items=64)
        self._probe_cache = _TTLCache(max_items=16)

    def _record_session_id(self, sid: Optional[str]) -> None:
        cleaned = _sanitize_cli_session_id(sid)
        self._session_id = cleaned
        if self._on_session_id is not None:
            try:
                self._on_session_id(self.provider_name, cleaned)
            except Exception as exc:
                logger.debug("Session ID callback ignored for %s: %s", self.provider_name, exc)

    def _resolve_binary(self) -> Optional[str]:
        if not self.binary:
            return None
        return resolve_cli_binary(self.binary)

    @property
    def is_configured(self) -> bool:
        return bool(self.enabled and self._resolve_binary())

    def _sanitized_env(self, binary_path: Optional[str] = None) -> Dict[str, str]:
        extra = [os.path.dirname(binary_path)] if binary_path and os.path.dirname(binary_path) else []
        return build_safe_cli_env(self.provider_name, extra_path_dirs=extra)

    def _notify_auth_failure(self, reason: str) -> None:
        if self._on_auth_failure is not None:
            try:
                self._on_auth_failure(self.provider_name, reason)
            except Exception as exc:
                logger.debug("Auth failure callback ignored for %s: %s", self.provider_name, exc)

    def _notify_auth_success(self) -> None:
        if self._on_auth_success is not None:
            try:
                self._on_auth_success(self.provider_name)
            except Exception as exc:
                logger.debug("Auth success callback ignored for %s: %s", self.provider_name, exc)

    def _prepare_prompt(self, system_prompt: str, user_prompt: str, *, json_mode: bool) -> str:
        json_hint = "\nReturn STRICT valid JSON only with no markdown fences." if json_mode else ""
        sanitized_sys = sanitize_cli_prompt(system_prompt, max_len=10_000)
        sanitized_usr = sanitize_cli_prompt(user_prompt, max_len=14_000)
        return f"{sanitized_sys}{json_hint}\n\nUSER REQUEST:\n{sanitized_usr}".strip()

    def _make_cache_key(self, target_model: str, combined_prompt: str, json_mode: bool) -> str:
        return hashlib.sha256(
            json.dumps(
                {
                    "provider": self.provider_name,
                    "model": target_model,
                    "prompt": combined_prompt,
                    "json": json_mode,
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

    def chat_completion(
        self,
        system_prompt: str,
        user_prompt: str,
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 1800,
        json_mode: bool = False,
        use_cache: bool = True,
        timeout_override: Optional[float] = None,
    ) -> CompletionResult:
        raise NotImplementedError

    def test_connection(self, deep: bool = True, *, force_refresh: bool = False) -> Dict[str, Any]:
        """Verify CLI binary availability, version, and (when `deep=True`) live auth/inference."""
        if not self.enabled:
            return {
                "ok": False,
                "provider": self.provider_name,
                "auth_ready": False,
                "message": f"{self.display_label} is disabled.",
            }
        binary = self._resolve_binary()
        if not binary:
            return {
                "ok": False,
                "provider": self.provider_name,
                "auth_ready": False,
                "message": f"{self.display_label} binary `{self.binary}` was not found.",
            }

        cache_key = f"probe:{binary}:{self.model}:{bool(deep)}"
        if not force_refresh:
            cached = self._probe_cache.get(cache_key)
            if isinstance(cached, dict):
                return dict(cached)

        ver_ok, version_out = _run_cli_version_probe(binary, provider_id=self.provider_name)
        if not ver_ok:
            res = {
                "ok": False,
                "provider": self.provider_name,
                "auth_ready": False,
                "binary_path": binary,
                "version": version_out,
                "message": f"{self.display_label} version check failed: {version_out}",
            }
            self._probe_cache.set(cache_key, res, ttl_seconds=self.probe_cache_ttl_seconds)
            return dict(res)

        if not deep:
            res = {
                "ok": True,
                "provider": self.provider_name,
                "auth_ready": None,
                "binary_path": binary,
                "version": version_out,
                "active_model": self.model or "default",
                "message": f"{self.display_label} binary found ({version_out or binary}).",
            }
            self._probe_cache.set(cache_key, res, ttl_seconds=self.probe_cache_ttl_seconds)
            return dict(res)

        probe_timeout = min(self.timeout_seconds, 35.0)
        try:
            probe = self.chat_completion(
                system_prompt="Reply with ONLY the word OK.",
                user_prompt="OK",
                max_tokens=16,
                temperature=0.0,
                use_cache=False,
                timeout_override=probe_timeout,
            )
            res = {
                "ok": True,
                "provider": self.provider_name,
                "auth_ready": True,
                "binary_path": binary,
                "version": version_out,
                "active_model": probe.model or self.model or "default",
                "latency_ms": probe.latency_ms,
                "message": f"{self.display_label} ready ({version_out or binary}).",
            }
        except Exception as exc:
            err_msg = str(exc)
            if is_cli_auth_error(err_msg):
                self._notify_auth_failure(err_msg)
            res = {
                "ok": False,
                "provider": self.provider_name,
                "auth_ready": False,
                "binary_path": binary,
                "version": version_out,
                "message": err_msg,
            }

        self._probe_cache.set(cache_key, res, ttl_seconds=self.probe_cache_ttl_seconds)
        return dict(res)


# ---------------------------------------------------------------------------
# Concrete Local AI CLI Adapters
# ---------------------------------------------------------------------------


class OpenCodeCLIClient(_BaseLocalCLIClient):
    """
    Sandboxed Local OpenCode CLI Adapter (`provider_name = "opencode_cli"`).

    Invokes:
      `[bin, "run", "--format", "json", "--archived", *(["--model", model] if model else []), prompt]`
    inside an isolated empty `TemporaryDirectory` (`stdin=subprocess.DEVNULL`).
    """

    provider_name: str = "opencode_cli"
    default_binary_name: str = "opencode"
    default_model_value: str = DEFAULT_OPENCODE_CLI_MODEL
    error_cls = OpenCodeCLIError
    display_label = "OpenCode CLI"

    def list_models(self, force_refresh: bool = False) -> Dict[str, Any]:
        binary = self._resolve_binary()
        return {
            "live": bool(self.enabled and binary),
            "source": "opencode_cli_local" if (self.enabled and binary) else "curated_fallback",
            "provider": self.provider_name,
            "binary_found": bool(binary),
            "binary_path": binary or "",
            "enabled": self.enabled,
            "notice": None if (self.enabled and binary) else "Install `opencode` CLI for zero-config local AI.",
            "models": [
                {
                    "id": "",
                    "family": "opencode",
                    "protocol": "opencode_cli",
                    "note": "Default configured OpenCode model (recommended)",
                },
                {
                    "id": "opencode-go/glm-5.3-flash",
                    "family": "glm",
                    "protocol": "opencode_cli",
                    "note": "Fast OpenCode Go model (~2s response)",
                },
            ],
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
        timeout_override: Optional[float] = None,
    ) -> CompletionResult:
        if not self.enabled:
            raise OpenCodeCLIError("OpenCode CLI provider is disabled.")
        binary = self._resolve_binary()
        if not binary:
            raise OpenCodeCLIError(f"OpenCode CLI binary `{self.binary}` was not found.")

        target_model = (self.model if model is None else model).strip()
        if target_model and not _SAFE_CLI_MODEL_RE.match(target_model):
            raise OpenCodeCLIError("Invalid OpenCode CLI model identifier.")

        combined_prompt = self._prepare_prompt(system_prompt, user_prompt, json_mode=json_mode)
        cache_hash = self._make_cache_key(target_model, combined_prompt, json_mode)
        if use_cache:
            cached = self._cache.get(cache_hash)
            if cached is not None:
                return CompletionResult(
                    content=cached["content"],
                    model=cached["model"],
                    protocol_used="opencode_cli",
                    cached=True,
                    provider=self.provider_name,
                    input_tokens=0,
                    output_tokens=0,
                    cached_tokens=0,
                    cost_microdollars=0,
                    saved_microdollars=int(cached.get("full_uncached_cost_microdollars", 0)),
                    latency_ms=0,
                )

        def _build_oc_cmd(use_archived: bool, sid: Optional[str]) -> List[str]:
            if use_archived:
                out_cmd: List[str] = [binary, "run", "--format", "json", "--archived"]
            else:
                out_cmd = [binary, "run", "--standalone", "--format", "json"]
            if sid:
                out_cmd.extend(["--session", sid])
            if target_model:
                out_cmd.extend(["--model", target_model])
            if not is_real_runtime:
                out_cmd.append(combined_prompt)
            return out_cmd

        is_real_runtime = subprocess.run is _REAL_SUBPROCESS_RUN
        active_sid = self._session_id
        cmd = _build_oc_cmd(use_archived=True, sid=active_sid)

        effective_timeout = (
            max(5.0, min(180.0, float(timeout_override)))
            if timeout_override is not None
            else self.timeout_seconds
        )
        t0 = time.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix="chronox_opencode_") as empty_cwd:
                proc = run_isolated_cli_subprocess(
                    cmd,
                    cwd=empty_cwd,
                    env=self._sanitized_env(binary),
                    timeout=effective_timeout,
                    stdin_text=combined_prompt if is_real_runtime else None,
                )
                used_archived = True
                if (
                    proc.returncode != 0
                    and "--archived" in cmd
                    and "unrecognized flag" in ((proc.stderr or "") + (proc.stdout or "")).lower()
                    and "--archived" in ((proc.stderr or "") + (proc.stdout or "")).lower()
                ):
                    used_archived = False
                    fallback_cmd = _build_oc_cmd(use_archived=False, sid=active_sid)
                    proc = run_isolated_cli_subprocess(
                        fallback_cmd,
                        cwd=empty_cwd,
                        env=self._sanitized_env(binary),
                        timeout=effective_timeout,
                        stdin_text=combined_prompt if is_real_runtime else None,
                    )
                if proc.returncode != 0 and active_sid and not is_cli_auth_error((proc.stderr or "") + (proc.stdout or "")):
                    # Self-heal if the previously persisted OpenCode session was pruned/deleted
                    self._record_session_id(None)
                    fresh_cmd = _build_oc_cmd(use_archived=used_archived, sid=None)
                    proc = run_isolated_cli_subprocess(
                        fresh_cmd,
                        cwd=empty_cwd,
                        env=self._sanitized_env(binary),
                        timeout=effective_timeout,
                        stdin_text=combined_prompt if is_real_runtime else None,
                    )
        except subprocess.TimeoutExpired as exc:
            timeout_msg = f"OpenCode CLI timed out after {effective_timeout:.0f}s."
            self._notify_auth_failure(timeout_msg)
            raise OpenCodeCLIError(timeout_msg) from exc
        except OSError as exc:
            raise OpenCodeCLIError(f"Failed to launch OpenCode CLI: {exc}") from exc

        latency_ms = max(0, int(round((time.monotonic() - t0) * 1000)))
        if proc.returncode != 0:
            # Check if stdout contains a structured JSONL error event first
            try:
                _parse_opencode_cli_jsonl(proc.stdout or "")
            except OpenCodeCLIError as parsed_err:
                if is_cli_auth_error(str(parsed_err)):
                    self._notify_auth_failure(str(parsed_err))
                raise
            err_snippet = sanitize_cli_prompt((proc.stderr or proc.stdout or "").strip(), max_len=240)
            full_err = f"OpenCode CLI exited with status {proc.returncode}: {err_snippet}"
            if is_cli_auth_error(full_err):
                self._notify_auth_failure(full_err)
            raise OpenCodeCLIError(full_err)

        try:
            content, in_tok, out_tok, cached_tok, reported_cost_ud, detected_model = _parse_opencode_cli_jsonl(
                proc.stdout or ""
            )
        except OpenCodeCLIError as exc:
            if is_cli_auth_error(str(exc)):
                self._notify_auth_failure(str(exc))
            raise

        if not content:
            raise OpenCodeCLIError("OpenCode CLI returned empty JSONL output.")

        detected_sid = _extract_opencode_session_id(proc.stdout or "")
        if detected_sid:
            self._record_session_id(detected_sid)
        self._notify_auth_success()
        resolved_model = target_model or detected_model or "opencode-default"
        calc_cost_ud, saved_ud, full_uncached_ud = calculate_token_costs(
            model=resolved_model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cached_tokens=cached_tok,
            provider=self.provider_name,
            is_local_endpoint=False,
        )
        cost_ud = reported_cost_ud if reported_cost_ud is not None else calc_cost_ud
        if reported_cost_ud is not None:
            full_uncached_ud = max(full_uncached_ud, reported_cost_ud + saved_ud)

        if use_cache and self.cache_ttl_seconds > 0:
            self._cache.set(
                cache_hash,
                {
                    "content": content,
                    "model": resolved_model,
                    "full_uncached_cost_microdollars": full_uncached_ud,
                },
                ttl_seconds=self.cache_ttl_seconds,
            )

        return CompletionResult(
            content=content,
            model=resolved_model,
            protocol_used="opencode_cli",
            cached=False,
            provider=self.provider_name,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cached_tokens=cached_tok,
            cost_microdollars=cost_ud,
            saved_microdollars=saved_ud,
            latency_ms=latency_ms,
        )


class ClaudeCLIClient(_BaseLocalCLIClient):
    """
    Sandboxed Local Claude Code CLI Adapter (`provider_name = "claude_cli"`).

    Invokes:
      `[bin, "-p", "--output-format", "json", "--permission-mode", "dontAsk", "--tools", "", "--no-session-persistence", *(["--model", model] if model else [])]`
    with the redacted prompt piped via `stdin` (`input=combined_prompt`) inside an isolated empty `TemporaryDirectory`.
    """

    provider_name: str = "claude_cli"
    default_binary_name: str = "claude"
    default_model_value: str = DEFAULT_CLAUDE_CLI_MODEL
    error_cls = ClaudeCLIError
    display_label = "Claude Code CLI"

    def list_models(self, force_refresh: bool = False) -> Dict[str, Any]:
        binary = self._resolve_binary()
        return {
            "live": bool(self.enabled and binary),
            "source": "claude_cli_local" if (self.enabled and binary) else "curated_fallback",
            "provider": self.provider_name,
            "binary_found": bool(binary),
            "binary_path": binary or "",
            "enabled": self.enabled,
            "notice": None if (self.enabled and binary) else "Install Claude Code (`claude`) CLI for zero-config local AI.",
            "models": [
                {
                    "id": "",
                    "family": "claude",
                    "protocol": "claude_cli",
                    "note": "Default configured Claude CLI model (recommended)",
                },
                {
                    "id": "haiku",
                    "family": "claude",
                    "protocol": "claude_cli",
                    "note": "Fast low-latency Claude Haiku coaching",
                },
                {
                    "id": "sonnet",
                    "family": "claude",
                    "protocol": "claude_cli",
                    "note": "Balanced Claude Sonnet financial reasoning",
                },
                {
                    "id": "opus",
                    "family": "claude",
                    "protocol": "claude_cli",
                    "note": "Highest-capability Claude Opus analysis",
                },
            ],
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
        timeout_override: Optional[float] = None,
    ) -> CompletionResult:
        if not self.enabled:
            raise ClaudeCLIError("Claude CLI provider is disabled.")
        binary = self._resolve_binary()
        if not binary:
            raise ClaudeCLIError(f"Claude CLI binary `{self.binary}` was not found.")

        target_model = (self.model if model is None else model).strip()
        if target_model and not _SAFE_CLI_MODEL_RE.match(target_model):
            raise ClaudeCLIError("Invalid Claude CLI model identifier.")

        combined_prompt = self._prepare_prompt(system_prompt, user_prompt, json_mode=json_mode)
        cache_hash = self._make_cache_key(target_model, combined_prompt, json_mode)
        if use_cache:
            cached = self._cache.get(cache_hash)
            if cached is not None:
                return CompletionResult(
                    content=cached["content"],
                    model=cached["model"],
                    protocol_used="claude_cli",
                    cached=True,
                    provider=self.provider_name,
                    input_tokens=0,
                    output_tokens=0,
                    cached_tokens=0,
                    cost_microdollars=0,
                    saved_microdollars=int(cached.get("full_uncached_cost_microdollars", 0)),
                    latency_ms=0,
                )

        cmd: List[str] = [
            binary,
            "-p",
            "--output-format",
            "json",
            "--permission-mode",
            "dontAsk",
            "--tools",
            "",
            "--no-session-persistence",
        ]
        if target_model:
            cmd.extend(["--model", target_model])

        effective_timeout = (
            max(5.0, min(180.0, float(timeout_override)))
            if timeout_override is not None
            else self.timeout_seconds
        )
        t0 = time.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix="chronox_claude_") as empty_cwd:
                proc = run_isolated_cli_subprocess(
                    cmd,
                    cwd=empty_cwd,
                    env=self._sanitized_env(binary),
                    timeout=effective_timeout,
                    stdin_text=combined_prompt,
                )
        except subprocess.TimeoutExpired as exc:
            timeout_msg = f"Claude CLI timed out after {effective_timeout:.0f}s."
            self._notify_auth_failure(timeout_msg)
            raise ClaudeCLIError(timeout_msg) from exc
        except OSError as exc:
            raise ClaudeCLIError(f"Failed to launch Claude CLI: {exc}") from exc

        latency_ms = max(0, int(round((time.monotonic() - t0) * 1000)))
        if proc.returncode != 0:
            # Surface structured JSON error if Claude emitted one on stdout
            data_err = _extract_json_object(proc.stdout or "")
            raw_err = ""
            if isinstance(data_err, dict) and (data_err.get("is_error") or data_err.get("error")):
                raw_err = str(data_err.get("result") or data_err.get("error") or "")
            if not raw_err:
                raw_err = (proc.stderr or proc.stdout or "").strip()
            err_snippet = sanitize_cli_prompt(raw_err, max_len=240)
            full_err = f"Claude CLI exited with status {proc.returncode}: {err_snippet}"
            if is_cli_auth_error(full_err):
                self._notify_auth_failure(full_err)
            raise ClaudeCLIError(full_err)

        try:
            content, in_tok, out_tok, cached_tok, reported_cost_ud, detected_model = _parse_claude_cli_json(
                proc.stdout or ""
            )
        except ClaudeCLIError as exc:
            if is_cli_auth_error(str(exc)):
                self._notify_auth_failure(str(exc))
            raise

        self._notify_auth_success()
        resolved_model = target_model or detected_model or "claude-default"
        calc_cost_ud, saved_ud, full_uncached_ud = calculate_token_costs(
            model=resolved_model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cached_tokens=cached_tok,
            provider=self.provider_name,
            is_local_endpoint=False,
        )
        cost_ud = reported_cost_ud if reported_cost_ud is not None else calc_cost_ud
        if reported_cost_ud is not None:
            full_uncached_ud = max(full_uncached_ud, reported_cost_ud + saved_ud)

        if use_cache and self.cache_ttl_seconds > 0:
            self._cache.set(
                cache_hash,
                {
                    "content": content,
                    "model": resolved_model,
                    "full_uncached_cost_microdollars": full_uncached_ud,
                },
                ttl_seconds=self.cache_ttl_seconds,
            )

        return CompletionResult(
            content=content,
            model=resolved_model,
            protocol_used="claude_cli",
            cached=False,
            provider=self.provider_name,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cached_tokens=cached_tok,
            cost_microdollars=cost_ud,
            saved_microdollars=saved_ud,
            latency_ms=latency_ms,
        )


class AntigravityCLIClient(_BaseLocalCLIClient):
    """
    Sandboxed Local Antigravity CLI (`agy`) Adapter (`provider_name = "agy_cli"`).

    Invokes:
      `[bin, "-p", prompt, "--output-format", "json", "--model", model or "gemini-3.8-flash-low", "--disable-slash-commands", "--sandbox", "--print-timeout", "55s"]`
    inside an isolated empty `TemporaryDirectory` (`stdin=subprocess.DEVNULL`).
    Never passes `--mode plan` alongside `--disable-slash-commands`.
    """

    provider_name: str = "agy_cli"
    default_binary_name: str = "agy"
    default_model_value: str = DEFAULT_AGY_CLI_MODEL
    error_cls = AntigravityCLIError
    display_label = "Antigravity CLI"

    def __init__(
        self,
        *,
        enabled: bool = True,
        model: Optional[str] = None,
        binary: Optional[str] = None,
        timeout_seconds: float = DEFAULT_AGY_CLI_TIMEOUT_SECONDS,
        cache_ttl_seconds: int = 900,
        probe_cache_ttl_seconds: int = DEFAULT_CLI_PROBE_CACHE_TTL_SECONDS,
        on_auth_failure: Optional[Callable[[str, str], None]] = None,
        on_auth_success: Optional[Callable[[str], None]] = None,
        session_id: Optional[str] = None,
        on_session_id: Optional[Callable[[str, Optional[str]], None]] = None,
    ) -> None:
        super().__init__(
            enabled=enabled,
            model=model,
            binary=binary,
            timeout_seconds=timeout_seconds,
            cache_ttl_seconds=cache_ttl_seconds,
            probe_cache_ttl_seconds=probe_cache_ttl_seconds,
            on_auth_failure=on_auth_failure,
            on_auth_success=on_auth_success,
            session_id=session_id,
            on_session_id=on_session_id,
        )
        if not self.model:
            self.model = DEFAULT_AGY_CLI_MODEL

    def list_models(self, force_refresh: bool = False) -> Dict[str, Any]:
        binary = self._resolve_binary()
        curated: List[Dict[str, str]] = [
            {
                "id": DEFAULT_AGY_CLI_MODEL,
                "family": "gemini",
                "protocol": "agy_cli",
                "note": "Fast low-effort Gemini Flash route (recommended for interactive coaching)",
            },
            {
                "id": "gemini-3.8-flash-medium",
                "family": "gemini",
                "protocol": "agy_cli",
                "note": "Gemini 3.8 Flash (Medium)",
            },
            {
                "id": "gemini-4-neon-medium",
                "family": "gemini",
                "protocol": "agy_cli",
                "note": "Gemini 4 Neon (Medium)",
            },
            {
                "id": "gemini-4-neon-high",
                "family": "gemini",
                "protocol": "agy_cli",
                "note": "Gemini 4 Neon (High)",
            },
            {
                "id": "claude-sonnet-5-5-medium",
                "family": "claude",
                "protocol": "agy_cli",
                "note": "Claude Sonnet 5.5 via Antigravity",
            },
            {
                "id": "claude-opus-5-5-medium",
                "family": "claude",
                "protocol": "agy_cli",
                "note": "Claude Opus 5.5 via Antigravity",
            },
        ]
        models: List[Dict[str, str]] = list(curated)
        source = "agy_cli_local" if (self.enabled and binary) else "curated_fallback"
        if self.enabled and binary and subprocess.run is _REAL_SUBPROCESS_RUN:
            cache_key = f"agy_models:{binary}"
            cached_models = None if force_refresh else self._probe_cache.get(cache_key)
            if isinstance(cached_models, list) and cached_models:
                models = list(cached_models)
            else:
                try:
                    with tempfile.TemporaryDirectory(prefix="chronox_agy_models_") as empty_cwd:
                        proc = subprocess.run(  # noqa: S603
                            [binary, "models"],
                            cwd=empty_cwd,
                            stdin=subprocess.DEVNULL,
                            capture_output=True,
                            text=True,
                            timeout=6.0,
                            env=self._sanitized_env(binary),
                            check=False,
                        )
                    if proc.returncode == 0 and proc.stdout:
                        parsed: List[Dict[str, str]] = []
                        seen: set[str] = set()
                        for line in proc.stdout.splitlines():
                            stripped = line.strip()
                            if not stripped or stripped.lower().startswith("fetching"):
                                continue
                            parts = stripped.split("\t", 1)
                            mid = parts[0].strip()
                            if not mid or mid in seen or not _SAFE_CLI_MODEL_RE.match(mid):
                                continue
                            seen.add(mid)
                            label = parts[1].strip() if len(parts) > 1 else mid
                            fam = "claude" if mid.startswith("claude") else ("gpt" if mid.startswith("gpt") else "gemini")
                            parsed.append({"id": mid, "family": fam, "protocol": "agy_cli", "note": label})
                        if parsed:
                            models = parsed
                            self._probe_cache.set(cache_key, list(parsed), ttl_seconds=300)
                except Exception as exc:
                    logger.debug("Antigravity live model listing skipped: %s", exc)
        return {
            "live": bool(self.enabled and binary),
            "source": source,
            "provider": self.provider_name,
            "binary_found": bool(binary),
            "binary_path": binary or "",
            "enabled": self.enabled,
            "notice": None if (self.enabled and binary) else "Install Antigravity CLI (`agy`) for zero-config local AI.",
            "models": models,
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
        timeout_override: Optional[float] = None,
    ) -> CompletionResult:
        if not self.enabled:
            raise AntigravityCLIError("Antigravity CLI provider is disabled.")
        binary = self._resolve_binary()
        if not binary:
            raise AntigravityCLIError(f"Antigravity CLI binary `{self.binary}` was not found.")

        target_model = (model if model is not None else self.model).strip() or DEFAULT_AGY_CLI_MODEL
        if not _SAFE_CLI_MODEL_RE.match(target_model):
            raise AntigravityCLIError("Invalid Antigravity CLI model identifier.")

        combined_prompt = self._prepare_prompt(system_prompt, user_prompt, json_mode=json_mode)
        cache_hash = self._make_cache_key(target_model, combined_prompt, json_mode)
        if use_cache:
            cached = self._cache.get(cache_hash)
            if cached is not None:
                return CompletionResult(
                    content=cached["content"],
                    model=cached["model"],
                    protocol_used="agy_cli",
                    cached=True,
                    provider=self.provider_name,
                    input_tokens=0,
                    output_tokens=0,
                    cached_tokens=0,
                    cost_microdollars=0,
                    saved_microdollars=int(cached.get("full_uncached_cost_microdollars", 0)),
                    latency_ms=0,
                )

        effective_timeout = (
            max(5.0, min(180.0, float(timeout_override)))
            if timeout_override is not None
            else self.timeout_seconds
        )
        print_timeout_s = 55 if effective_timeout >= 60.0 else max(5, int(effective_timeout - 5))
        is_real_runtime = subprocess.run is _REAL_SUBPROCESS_RUN
        t0 = time.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix="chronox_agy_") as empty_cwd:
                if is_real_runtime:
                    # Keep sensitive prompt balances/merchants out of argv (`ps` / `/proc/cmdline`)
                    prompt_file = os.path.join(empty_cwd, "prompt.txt")
                    fd = os.open(prompt_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    try:
                        os.write(fd, combined_prompt.encode("utf-8"))
                    finally:
                        os.close(fd)
                    prompt_arg = (
                        "Read and follow all instructions in prompt.txt inside the current workspace "
                        "directory, then reply directly with the requested output only."
                    )
                else:
                    prompt_arg = combined_prompt
                def _build_agy_cmd(conv_id: Optional[str]) -> List[str]:
                    out_cmd: List[str] = [
                        binary,
                        "-p",
                        prompt_arg,
                        "--output-format",
                        "json",
                        "--model",
                        target_model,
                        "--disable-slash-commands",
                        "--sandbox",
                        "--print-timeout",
                        f"{print_timeout_s}s",
                    ]
                    if conv_id:
                        out_cmd.extend(["--conversation", conv_id])
                    return out_cmd

                active_conv = self._session_id
                cmd = _build_agy_cmd(active_conv)
                proc = run_isolated_cli_subprocess(
                    cmd,
                    cwd=empty_cwd,
                    env=self._sanitized_env(binary),
                    timeout=effective_timeout,
                    stdin_text=None,
                )
                if proc.returncode != 0 and active_conv and not is_cli_auth_error((proc.stderr or "") + (proc.stdout or "")):
                    # Self-heal if the previously persisted Antigravity conversation was deleted/expired
                    self._record_session_id(None)
                    proc = run_isolated_cli_subprocess(
                        _build_agy_cmd(None),
                        cwd=empty_cwd,
                        env=self._sanitized_env(binary),
                        timeout=effective_timeout,
                        stdin_text=None,
                    )
        except subprocess.TimeoutExpired as exc:
            timeout_msg = f"Antigravity CLI timed out after {effective_timeout:.0f}s."
            self._notify_auth_failure(timeout_msg)
            raise AntigravityCLIError(timeout_msg) from exc
        except OSError as exc:
            raise AntigravityCLIError(f"Failed to launch Antigravity CLI: {exc}") from exc

        latency_ms = max(0, int(round((time.monotonic() - t0) * 1000)))
        if proc.returncode != 0:
            data_err = _extract_json_object(proc.stdout or "")
            raw_err = ""
            if isinstance(data_err, dict) and str(data_err.get("status") or "").upper() != "SUCCESS":
                raw_err = str(data_err.get("error") or data_err.get("message") or "")
            if not raw_err:
                raw_err = (proc.stderr or proc.stdout or "").strip()
            err_snippet = sanitize_cli_prompt(raw_err, max_len=240)
            full_err = f"Antigravity CLI exited with status {proc.returncode}: {err_snippet}"
            if is_cli_auth_error(full_err):
                self._notify_auth_failure(full_err)
            raise AntigravityCLIError(full_err)

        try:
            content, in_tok, out_tok, cached_tok, detected_model = _parse_agy_cli_json(proc.stdout or "")
        except AntigravityCLIError as exc:
            if is_cli_auth_error(str(exc)):
                self._notify_auth_failure(str(exc))
            raise

        detected_conv = _extract_agy_conversation_id(proc.stdout or "")
        if detected_conv:
            self._record_session_id(detected_conv)
        self._notify_auth_success()
        resolved_model = detected_model or target_model
        cost_ud, saved_ud, full_uncached_ud = calculate_token_costs(
            model=resolved_model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cached_tokens=cached_tok,
            provider=self.provider_name,
            is_local_endpoint=False,
        )

        if use_cache and self.cache_ttl_seconds > 0:
            self._cache.set(
                cache_hash,
                {
                    "content": content,
                    "model": resolved_model,
                    "full_uncached_cost_microdollars": full_uncached_ud,
                },
                ttl_seconds=self.cache_ttl_seconds,
            )

        return CompletionResult(
            content=content,
            model=resolved_model,
            protocol_used="agy_cli",
            cached=False,
            provider=self.provider_name,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cached_tokens=cached_tok,
            cost_microdollars=cost_ud,
            saved_microdollars=saved_ud,
            latency_ms=latency_ms,
        )


# ---------------------------------------------------------------------------
# Auto-Discovery Registry (`LocalCLIRegistry`)
# ---------------------------------------------------------------------------


@dataclass
class LocalCLIStatus:
    provider_id: str
    display_name: str
    binary_name: str
    binary_path: str = ""
    installed: bool = False
    auth_ready: Optional[bool] = None
    version: str = ""
    status_detail: str = ""
    default_model: str = ""
    priority: int = 0
    in_cooldown: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "display_name": self.display_name,
            "binary_name": self.binary_name,
            "binary_path": self.binary_path,
            "installed": self.installed,
            "auth_ready": self.auth_ready,
            "version": self.version,
            "status_detail": self.status_detail,
            "default_model": self.default_model,
            "priority": self.priority,
            "in_cooldown": self.in_cooldown,
        }


class LocalCLIRegistry:
    """
    Zero-config auto-discovery and failover registry for local AI CLIs.

    Priority order: `claude_cli -> opencode_cli -> codex_cli -> agy_cli`.
    Remembers transient auth failures (e.g., Codex OAuth `401`) for 5 minutes so
    `auto` mode immediately skips broken auth and lands on the first working local CLI.
    """

    PRIORITY_ORDER: Tuple[str, ...] = LOCAL_CLI_PRIORITY

    def __init__(
        self,
        *,
        model_overrides: Optional[Dict[str, str]] = None,
        binary_overrides: Optional[Dict[str, str]] = None,
        timeout_seconds: float = DEFAULT_LOCAL_CLI_TIMEOUT_SECONDS,
        auth_failure_ttl_seconds: float = DEFAULT_AUTH_FAILURE_COOLDOWN_SECONDS,
        version_cache_ttl_seconds: int = DEFAULT_CLI_PROBE_CACHE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._model_overrides: Dict[str, str] = dict(model_overrides or {})
        self._binary_overrides: Dict[str, str] = dict(binary_overrides or {})
        self._timeout_seconds = float(timeout_seconds)
        self._auth_failure_ttl_seconds = max(5.0, float(auth_failure_ttl_seconds))
        self._version_cache_ttl_seconds = max(10, int(version_cache_ttl_seconds))
        self._clock = clock
        self._lock = threading.RLock()
        self._auth_failures: Dict[str, Tuple[float, str]] = {}
        self._auth_probe_results: Dict[str, Tuple[float, bool, str]] = {}
        self._version_cache = _TTLCache(max_items=32)

    def _binary_name_for(self, provider_id: str) -> str:
        spec = LOCAL_CLI_SPECS[provider_id]
        return (self._binary_overrides.get(provider_id) or str(spec["binary_name"])).strip()

    def _model_for(self, provider_id: str, override: Optional[str] = None) -> str:
        if override is not None:
            return override.strip()
        if provider_id in self._model_overrides:
            return (self._model_overrides[provider_id] or "").strip()
        return str(LOCAL_CLI_SPECS[provider_id]["default_model"])

    def mark_auth_failure(
        self,
        provider_id: str,
        reason: str,
        *,
        ttl_seconds: Optional[float] = None,
    ) -> None:
        """Record a transient auth failure for `provider_id` (default 5-minute cooldown)."""
        ttl = self._auth_failure_ttl_seconds if ttl_seconds is None else max(1.0, float(ttl_seconds))
        expires_at = self._clock() + ttl
        cleaned_reason = sanitize_cli_prompt(reason or "Authentication failed", max_len=240)
        with self._lock:
            self._auth_failures[provider_id] = (expires_at, cleaned_reason)
            self._auth_probe_results[provider_id] = (expires_at, False, cleaned_reason)

    def clear_auth_failure(self, provider_id: str, *, mark_ready: bool = True) -> None:
        """Clear any recorded auth failure cooldown for `provider_id`."""
        with self._lock:
            self._auth_failures.pop(provider_id, None)
            if mark_ready:
                expires_at = self._clock() + self._auth_failure_ttl_seconds
                self._auth_probe_results[provider_id] = (expires_at, True, "Authenticated and ready")

    def get_auth_failure_reason(self, provider_id: str) -> Optional[str]:
        """Return active failure reason if `provider_id` is in auth-failure cooldown, else None."""
        now = self._clock()
        with self._lock:
            entry = self._auth_failures.get(provider_id)
            if entry is None:
                return None
            expires_at, reason = entry
            if now >= expires_at:
                self._auth_failures.pop(provider_id, None)
                return None
            return reason

    def is_auth_failed(self, provider_id: str) -> bool:
        return self.get_auth_failure_reason(provider_id) is not None

    def _get_cached_probe(self, provider_id: str) -> Optional[Tuple[bool, str]]:
        now = self._clock()
        with self._lock:
            entry = self._auth_probe_results.get(provider_id)
            if entry is None:
                return None
            expires_at, ok, detail = entry
            if now >= expires_at:
                self._auth_probe_results.pop(provider_id, None)
                return None
            return (ok, detail)

    def build_client(
        self,
        provider_id: str,
        *,
        model: Optional[str] = None,
        enabled: bool = True,
        timeout_seconds: Optional[float] = None,
        session_id: Optional[str] = None,
        on_session_id: Optional[Callable[[str, Optional[str]], None]] = None,
    ) -> LLMClient:
        """Construct a hardened `LLMClient` wired with registry auth-failure tracking."""
        pid = (provider_id or "").strip().lower()
        if pid not in LOCAL_CLI_SPECS:
            raise ValueError(f"Unsupported local CLI provider_id: {provider_id!r}")

        bin_name = self._binary_name_for(pid)
        chosen_model = self._model_for(pid, override=model)
        timeout = (
            float(timeout_seconds)
            if timeout_seconds is not None
            else (DEFAULT_AGY_CLI_TIMEOUT_SECONDS if pid == "agy_cli" else self._timeout_seconds)
        )
        ttl_int = int(self._auth_failure_ttl_seconds)

        def _on_fail(p: str, reason: str) -> None:
            self.mark_auth_failure(p, reason)

        def _on_ok(p: str) -> None:
            self.clear_auth_failure(p, mark_ready=True)

        if pid == "claude_cli":
            return ClaudeCLIClient(
                enabled=enabled,
                model=chosen_model,
                binary=bin_name,
                timeout_seconds=timeout,
                probe_cache_ttl_seconds=ttl_int,
                on_auth_failure=_on_fail,
                on_auth_success=_on_ok,
            )
        if pid == "opencode_cli":
            return OpenCodeCLIClient(
                enabled=enabled,
                model=chosen_model,
                binary=bin_name,
                timeout_seconds=timeout,
                probe_cache_ttl_seconds=ttl_int,
                on_auth_failure=_on_fail,
                on_auth_success=_on_ok,
                session_id=session_id,
                on_session_id=on_session_id,
            )
        if pid == "codex_cli":
            return CodexCLIClient(
                enabled=enabled,
                model=chosen_model,
                codex_bin=bin_name,
                timeout_seconds=timeout,
                probe_cache_ttl_seconds=ttl_int,
                on_auth_failure=_on_fail,
                on_auth_success=_on_ok,
            )
        return AntigravityCLIClient(
            enabled=enabled,
            model=chosen_model or DEFAULT_AGY_CLI_MODEL,
            binary=bin_name,
            timeout_seconds=timeout,
            probe_cache_ttl_seconds=ttl_int,
            on_auth_failure=_on_fail,
            on_auth_success=_on_ok,
            session_id=session_id,
            on_session_id=on_session_id,
        )

    def _get_version(self, provider_id: str, binary_path: str, *, force_refresh: bool = False) -> str:
        cache_key = f"ver:{provider_id}:{binary_path}"
        if not force_refresh:
            cached = self._version_cache.get(cache_key)
            if isinstance(cached, str):
                return cached
        ok, version = _run_cli_version_probe(binary_path, provider_id=provider_id)
        val = version if ok else ""
        self._version_cache.set(cache_key, val, ttl_seconds=self._version_cache_ttl_seconds)
        return val

    def list_detected_clis(
        self,
        run_auth_probe: bool = False,
        *,
        run_version_probe: bool = True,
        force_refresh: bool = False,
    ) -> List[LocalCLIStatus]:
        """
        Discover all supported local AI CLIs in priority order:
        `claude_cli -> opencode_cli -> codex_cli -> agy_cli`.

        Fast by default (`run_auth_probe=False`): resolves binary paths + cached version
        and honors any 5-min auth-failure cooldowns without stalling on slow probes.
        """
        statuses: List[LocalCLIStatus] = []
        for pid in self.PRIORITY_ORDER:
            spec = LOCAL_CLI_SPECS[pid]
            bin_name = self._binary_name_for(pid)
            binary_path = resolve_cli_binary(bin_name) or ""
            installed = bool(binary_path)
            default_model = self._model_for(pid)

            if not installed:
                statuses.append(
                    LocalCLIStatus(
                        provider_id=pid,
                        display_name=str(spec["display_name"]),
                        binary_name=bin_name,
                        binary_path="",
                        installed=False,
                        auth_ready=False,
                        version="",
                        status_detail="Not installed",
                        default_model=default_model,
                        priority=int(spec["priority"]),
                        in_cooldown=False,
                    )
                )
                continue

            version = (
                self._get_version(pid, binary_path, force_refresh=force_refresh)
                if run_version_probe
                else ""
            )
            failure_reason = self.get_auth_failure_reason(pid)
            in_cooldown = failure_reason is not None

            if run_auth_probe and (force_refresh or not in_cooldown):
                client = self.build_client(pid, enabled=True)
                probe_fn: Any = client.test_connection
                try:
                    probe_res = probe_fn(deep=True, force_refresh=force_refresh)
                except TypeError:
                    probe_res = probe_fn()
                ok = bool(probe_res.get("ok"))
                detail = str(probe_res.get("message") or ("Ready" if ok else "Auth probe failed"))
                if not version and probe_res.get("version"):
                    version = str(probe_res["version"])
                if ok:
                    self.clear_auth_failure(pid, mark_ready=True)
                    auth_ready: Optional[bool] = True
                    in_cooldown = False
                else:
                    self.mark_auth_failure(pid, detail)
                    auth_ready = False
                    in_cooldown = True
                status_detail = detail
            elif in_cooldown:
                auth_ready = False
                status_detail = failure_reason or "Temporarily skipped after auth failure"
            else:
                cached_probe = self._get_cached_probe(pid)
                if cached_probe is not None:
                    auth_ready, status_detail = cached_probe
                else:
                    auth_ready = None
                    status_detail = f"Installed ({version})" if version else "Installed"

            statuses.append(
                LocalCLIStatus(
                    provider_id=pid,
                    display_name=str(spec["display_name"]),
                    binary_name=bin_name,
                    binary_path=binary_path,
                    installed=True,
                    auth_ready=auth_ready,
                    version=version,
                    status_detail=status_detail,
                    default_model=default_model,
                    priority=int(spec["priority"]),
                    in_cooldown=in_cooldown,
                )
            )

        return statuses

    def select_best_available_cli(
        self,
        *,
        run_auth_probe: bool = False,
        force_refresh: bool = False,
    ) -> Optional[LLMClient]:
        """
        Select the highest-priority working local AI CLI in order:
        `claude_cli -> opencode_cli -> codex_cli -> agy_cli`.

        Automatically skips uninstalled CLIs and any CLI currently in 5-min auth-failure
        cooldown (e.g., Codex `401 Unauthorized`). When `run_auth_probe=True`, tests
        candidates in priority order until the first authenticated CLI succeeds.
        """
        for pid in self.PRIORITY_ORDER:
            bin_name = self._binary_name_for(pid)
            if not resolve_cli_binary(bin_name):
                continue
            if not force_refresh and self.is_auth_failed(pid):
                continue

            client = self.build_client(pid, enabled=True)
            if not run_auth_probe:
                return client

            probe_fn: Any = client.test_connection
            try:
                probe_res = probe_fn(deep=True, force_refresh=force_refresh)
            except TypeError:
                probe_res = probe_fn()

            if probe_res.get("ok"):
                self.clear_auth_failure(pid, mark_ready=True)
                return client

            self.mark_auth_failure(pid, str(probe_res.get("message") or "Auth check failed"))

        return None

    def get_failover_clients(
        self,
        *,
        run_auth_probe: bool = False,
        include_cooldown: bool = False,
    ) -> List[LLMClient]:
        """
        Return all installed local CLI clients in priority order for runtime failover chains,
        skipping any CLI currently in 5-minute auth-failure cooldown unless `include_cooldown=True`.
        """
        clients: List[LLMClient] = []
        for pid in self.PRIORITY_ORDER:
            bin_name = self._binary_name_for(pid)
            if not resolve_cli_binary(bin_name):
                continue
            if not include_cooldown and self.is_auth_failed(pid):
                continue
            client = self.build_client(pid, enabled=True)
            if run_auth_probe:
                probe_fn: Any = client.test_connection
                try:
                    probe_res = probe_fn(deep=True)
                except TypeError:
                    probe_res = probe_fn()
                if not probe_res.get("ok"):
                    self.mark_auth_failure(pid, str(probe_res.get("message") or "Auth check failed"))
                    continue
                self.clear_auth_failure(pid, mark_ready=True)
            clients.append(client)
        return clients


LOCAL_HTTP_GATEWAY_PROBES: Tuple[Tuple[int, str], ...] = (
    (11434, "Ollama"),
    (1234, "LM Studio"),
    (4000, "LiteLLM"),
    (8000, "vLLM / Local Server"),
    (8788, "OMC Provider Wrapper"),
)
DEFAULT_LOCAL_HTTP_GATEWAY_CACHE_TTL_SECONDS = 60
_WRAPPER_HEALTH_CACHE = _TTLCache(max_items=16)


def probe_wrapper_provider_health(
    base_url: str,
    *,
    force_refresh: bool = False,
    timeout_seconds: float = 0.45,
    transport: Optional[httpx.BaseTransport] = None,
) -> Dict[str, Dict[str, Any]]:
    """
    Query `<origin>/health` on a local chat wrapper (e.g. OMC Provider Wrapper on :8788)
    to discover per-agent readiness (`codex`, `claude`, `opencode`, `agy`).
    Returns `{}` if the endpoint is unreachable or not an OMC-style wrapper.
    """
    raw = (base_url or "").strip().rstrip("/")
    if not raw:
        return {}
    origin = raw[:-3] if raw.endswith("/v1") else raw
    cache_key = f"wrap_health:{origin}"
    if transport is None and not force_refresh:
        cached = _WRAPPER_HEALTH_CACHE.get(cache_key)
        if isinstance(cached, dict):
            return dict(cached)

    probe_timeout = max(0.05, min(3.0, float(timeout_seconds)))
    client_kwargs: Dict[str, Any] = {"timeout": probe_timeout, "follow_redirects": False}
    if transport is not None:
        client_kwargs["transport"] = transport

    result: Dict[str, Dict[str, Any]] = {}
    try:
        with httpx.Client(**client_kwargs) as client:
            resp = client.get(f"{origin}/health")
            if resp.status_code == 200:
                payload = resp.json()
                providers = payload.get("providers") if isinstance(payload, dict) else None
                if isinstance(providers, dict):
                    for k, v in providers.items():
                        if isinstance(v, dict):
                            result[str(k).strip().lower()] = {
                                "available": bool(v.get("available")),
                                "reason_code": str(v.get("reason_code") or ""),
                                "version": str(v.get("version") or ""),
                            }
    except Exception as exc:
        logger.debug("Wrapper health probe skipped for %s: %s", origin, exc)

    if transport is None:
        _WRAPPER_HEALTH_CACHE.set(cache_key, dict(result), ttl_seconds=30)
    return result


@dataclass(frozen=True)
class LocalHTTPGatewayInfo:
    """Auto-detected OpenAI-compatible local HTTP gateway (Ollama, LM Studio, LiteLLM, vLLM)."""

    name: str
    port: int
    base_url: str
    model: str
    model_count: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "port": self.port,
            "base_url": self.base_url,
            "model": self.model,
            "model_count": self.model_count,
        }


_LOCAL_HTTP_GATEWAY_CACHE = _TTLCache()


def detect_local_http_gateways(
    *,
    force_refresh: bool = False,
    timeout_seconds: float = 0.30,
    transport: Optional[httpx.BaseTransport] = None,
    ports: Sequence[Tuple[int, str]] = LOCAL_HTTP_GATEWAY_PROBES,
) -> List[LocalHTTPGatewayInfo]:
    """
    Probe standard localhost OpenAI-compatible gateways (`11434`, `1234`, `4000`, `8000`)
    via `GET http://127.0.0.1:<port>/v1/models` with a tight 300ms timeout (cached 60s).
    """
    if transport is None and os.getenv("CHRONOX_DISABLE_LOCAL_HTTP_PROBE", "").strip().lower() in ("1", "true", "yes"):
        return []
    cache_key = "local_http_gateways"
    if transport is None and not force_refresh:
        cached = _LOCAL_HTTP_GATEWAY_CACHE.get(cache_key)
        if isinstance(cached, list):
            return list(cached)

    found: List[LocalHTTPGatewayInfo] = []
    probe_timeout = max(0.05, min(2.0, float(timeout_seconds)))
    client_kwargs: Dict[str, Any] = {"timeout": probe_timeout, "follow_redirects": False}
    if transport is not None:
        client_kwargs["transport"] = transport

    with httpx.Client(**client_kwargs) as client:
        for port, label in ports:
            base_url = f"http://127.0.0.1:{int(port)}/v1"
            try:
                resp = client.get(f"{base_url}/models")
                if resp.status_code != 200:
                    continue
                payload = resp.json()
            except Exception as exc:
                logger.debug("Local gateway probe skipped on 127.0.0.1:%d: %s", int(port), exc)
                continue

            raw_models: List[Any] = []
            if isinstance(payload, dict):
                if isinstance(payload.get("data"), list):
                    raw_models = payload["data"]
                elif isinstance(payload.get("models"), list):
                    raw_models = payload["models"]
            elif isinstance(payload, list):
                raw_models = payload

            model_ids: List[str] = []
            for item in raw_models:
                if isinstance(item, dict):
                    mid = str(item.get("id") or item.get("name") or item.get("model") or "").strip()
                    if mid:
                        model_ids.append(mid)
                elif isinstance(item, str) and item.strip():
                    model_ids.append(item.strip())

            if not model_ids:
                continue
            found.append(
                LocalHTTPGatewayInfo(
                    name=label,
                    port=int(port),
                    base_url=base_url,
                    model=model_ids[0],
                    model_count=len(model_ids),
                )
            )

    if transport is None:
        _LOCAL_HTTP_GATEWAY_CACHE.set(cache_key, list(found), ttl_seconds=DEFAULT_LOCAL_HTTP_GATEWAY_CACHE_TTL_SECONDS)
    return found


_DEFAULT_REGISTRY_LOCK = threading.Lock()
_DEFAULT_REGISTRY: Optional[LocalCLIRegistry] = None


def get_default_local_cli_registry() -> LocalCLIRegistry:
    """Return the process-wide default `LocalCLIRegistry` preserving 5-min auth cooldowns."""
    global _DEFAULT_REGISTRY
    with _DEFAULT_REGISTRY_LOCK:
        if _DEFAULT_REGISTRY is None:
            _DEFAULT_REGISTRY = LocalCLIRegistry()
        return _DEFAULT_REGISTRY


def discover_local_ai_clis(
    *,
    run_auth_probe: bool = False,
    run_version_probe: bool = True,
    force_refresh: bool = False,
    registry: Optional[LocalCLIRegistry] = None,
) -> List[Dict[str, Any]]:
    """Discover installed local AI CLIs (`claude`, `opencode`, `codex`, `agy`) as JSON-ready dicts."""
    reg = registry if registry is not None else get_default_local_cli_registry()
    return [
        status.to_dict()
        for status in reg.list_detected_clis(
            run_auth_probe=run_auth_probe,
            run_version_probe=run_version_probe,
            force_refresh=force_refresh,
        )
    ]


__all__ = [
    "_BASE_SAFE_CLI_ENV_ALLOWLIST",
    "_PROVIDER_CLI_ENV_ALLOWLISTS",
    "_SAFE_CLI_ENV_ALLOWLIST",
    "DEFAULT_AGY_CLI_MODEL",
    "DEFAULT_AGY_CLI_TIMEOUT_SECONDS",
    "DEFAULT_AUTH_FAILURE_COOLDOWN_SECONDS",
    "DEFAULT_CLAUDE_CLI_MODEL",
    "DEFAULT_CODEX_CLI_MODEL",
    "DEFAULT_LOCAL_CLI_TIMEOUT_SECONDS",
    "DEFAULT_OPENCODE_CLI_MODEL",
    "LOCAL_CLI_PRIORITY",
    "LOCAL_CLI_SPECS",
    "LOCAL_HTTP_GATEWAY_PROBES",
    "OPENCODE_SAFE_CONFIG_CONTENT",
    "AntigravityCLIClient",
    "AntigravityCLIError",
    "ClaudeCLIClient",
    "ClaudeCLIError",
    "CodexCLIClient",
    "CodexCLIError",
    "LocalCLIError",
    "LocalCLIRegistry",
    "LocalCLIStatus",
    "LocalHTTPGatewayInfo",
    "OpenCodeCLIClient",
    "OpenCodeCLIError",
    "build_augmented_cli_path",
    "build_safe_cli_env",
    "detect_local_http_gateways",
    "discover_local_ai_clis",
    "get_cli_search_dirs",
    "get_default_local_cli_registry",
    "is_cli_auth_error",
    "probe_wrapper_provider_health",
    "resolve_cli_binary",
    "sanitize_cli_prompt",
]
