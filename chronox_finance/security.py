"""
Security hardening utilities for ChronoX Finance.

Provides:
- PBKDF2-HMAC-SHA256 passcode hashing (600,000 iterations, random 16-byte salt)
- Constant-time HMAC-SHA256 signed session tokens with expiration
- Local restricted-permission (0600) secret store for server signing key & OpenCode Go key
- Sliding-window in-memory rate limiter (protects auth & AI quota endpoints)
- CSV formula injection neutralization (`=`, `+`, `-`, `@`, tab, CR)
- Prompt-injection & PII redaction helpers for outbound AI payloads
- Safe URL validation for external API endpoints (blocks SSRF to link-local/metadata IPs)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from typing import Deque, Dict, Optional, Tuple
from urllib.parse import urlparse

PBKDF2_ITERATIONS = 600_000
SESSION_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days
SESSION_COOKIE_NAME = "chronox_session"

# PII patterns scrubbed before sending financial summaries to external LLMs
_PII_PATTERNS = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED-SSN]"),
    (re.compile(r"\b(?:\d[ -]*?){13,19}\b"), "[REDACTED-CARD]"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[REDACTED-EMAIL]"),
    (re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"), "[REDACTED-PHONE]"),
    (re.compile(r"\b(?:acct|account|routing)[\s#:]*\d{4,17}\b", re.IGNORECASE), "[REDACTED-ACCT]"),
]

_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions?", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?(previous|system)\s+prompts?", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+(a|an)\s+", re.IGNORECASE),
    re.compile(r"system\s*:\s*", re.IGNORECASE),
]


def hash_passcode(passcode: str, salt: Optional[bytes] = None) -> str:
    """Hash a passcode with PBKDF2-HMAC-SHA256 and random salt."""
    if not passcode or len(passcode) < 6:
        raise ValueError("Passcode must be at least 6 characters")
    raw_salt = salt if salt is not None else secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", passcode.encode("utf-8"), raw_salt, PBKDF2_ITERATIONS)
    return (
        f"pbkdf2_sha256${PBKDF2_ITERATIONS}$"
        f"{base64.urlsafe_b64encode(raw_salt).decode('ascii')}$"
        f"{base64.urlsafe_b64encode(dk).decode('ascii')}"
    )


def verify_passcode(passcode: str, stored_hash: str) -> bool:
    """Constant-time verification of a PBKDF2-HMAC-SHA256 passcode hash."""
    try:
        scheme, iter_str, salt_b64, dk_b64 = stored_hash.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        iterations = int(iter_str)
        raw_salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
        expected_dk = base64.urlsafe_b64decode(dk_b64.encode("ascii"))
        actual_dk = hashlib.pbkdf2_hmac("sha256", passcode.encode("utf-8"), raw_salt, iterations)
        return hmac.compare_digest(actual_dk, expected_dk)
    except (ValueError, TypeError):
        return False


def create_session_token(secret_key: str, subject: str = "owner", ttl: int = SESSION_TTL_SECONDS) -> str:
    """Create an HMAC-SHA256 signed session token with expiry timestamp."""
    exp = int(time.time()) + ttl
    nonce = secrets.token_urlsafe(12)
    payload = f"{subject}.{exp}.{nonce}".encode("utf-8")
    sig = hmac.new(secret_key.encode("utf-8"), payload, hashlib.sha256).digest()
    return (
        f"{base64.urlsafe_b64encode(payload).decode('ascii')}."
        f"{base64.urlsafe_b64encode(sig).decode('ascii')}"
    )


def verify_session_token(token: str, secret_key: str) -> Optional[str]:
    """Verify signature and expiration of a session token; returns subject or None."""
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        payload = base64.urlsafe_b64decode(payload_b64.encode("ascii"))
        provided_sig = base64.urlsafe_b64decode(sig_b64.encode("ascii"))
        expected_sig = hmac.new(secret_key.encode("utf-8"), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(provided_sig, expected_sig):
            return None
        subject, exp_str, _nonce = payload.decode("utf-8").split(".", 2)
        if int(exp_str) < int(time.time()):
            return None
        return subject
    except (ValueError, TypeError):
        return None


def mask_secret(value: str) -> str:
    """Return a non-reversible masked representation of an API key."""
    cleaned = (value or "").strip()
    if not cleaned:
        return ""
    if len(cleaned) <= 8:
        return "••••••••"
    return f"{cleaned[:3]}••••{cleaned[-4:]}"


class LocalSecretStore:
    """
    Stores local runtime secrets (session signing key, optional user-entered
    OpenCode Go key) in a 0600-permission JSON file alongside the SQLite DB.
    Environment variables always take precedence when explicitly provided.
    """

    def __init__(self, store_path: Path):
        self.store_path = store_path
        self._lock = threading.Lock()
        self.store_path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> Dict[str, str]:
        if not self.store_path.exists():
            return {}
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, data: Dict[str, str]) -> None:
        tmp = self.store_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, self.store_path)
        try:
            os.chmod(self.store_path, 0o600)
        except OSError:
            pass

    def get(self, key: str) -> str:
        with self._lock:
            return str(self._read().get(key, ""))

    def set(self, key: str, value: str) -> None:
        with self._lock:
            data = self._read()
            if value:
                data[key] = value
            else:
                data.pop(key, None)
            self._write(data)

    def get_or_create_signing_key(self) -> str:
        env_key = os.getenv("CHRONOX_FINANCE_SECRET_KEY", "").strip()
        if env_key:
            return env_key
        with self._lock:
            data = self._read()
            existing = data.get("session_signing_key", "")
            if existing and len(existing) >= 32:
                return existing
            generated = secrets.token_urlsafe(48)
            data["session_signing_key"] = generated
            self._write(data)
            return generated


class SlidingWindowRateLimiter:
    """Thread-safe per-key sliding-window rate limiter with bounded key cardinality."""

    def __init__(self, max_requests: int, window_seconds: float, max_keys: int = 512):
        self.max_requests = max(1, max_requests)
        self.window_seconds = max(1.0, float(window_seconds))
        self.max_keys = max(16, int(max_keys))
        self._buckets: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> Tuple[bool, int]:
        """Return (allowed, retry_after_seconds)."""
        now = time.monotonic()
        with self._lock:
            if len(self._buckets) >= self.max_keys and key not in self._buckets:
                stale = [
                    k for k, dq in self._buckets.items()
                    if not dq or now - dq[-1] >= self.window_seconds
                ]
                for k in stale:
                    self._buckets.pop(k, None)
                if len(self._buckets) >= self.max_keys:
                    oldest_key = min(self._buckets, key=lambda k: self._buckets[k][-1] if self._buckets[k] else 0.0)
                    self._buckets.pop(oldest_key, None)

            bucket = self._buckets.setdefault(key, deque())
            while bucket and now - bucket[0] >= self.window_seconds:
                bucket.popleft()
            if len(bucket) >= self.max_requests:
                retry_after = max(1, int(self.window_seconds - (now - bucket[0])) + 1)
                return False, retry_after
            bucket.append(now)
            return True, 0


def sanitize_csv_cell(value: object) -> str:
    """Neutralize spreadsheet formula injection (=, +, -, @, tab, CR) on CSV export."""
    text = "" if value is None else str(value)
    if text and text[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def redact_pii(text: str) -> str:
    """Strip SSNs, card numbers, emails, phone numbers, and account numbers."""
    result = text or ""
    for rx, replacement in _PII_PATTERNS:
        result = rx.sub(replacement, result)
    return result


def sanitize_untrusted_text(text: str, max_len: int = 120) -> str:
    """Redact PII and neutralize prompt-injection patterns in merchant/memo text."""
    cleaned = redact_pii(text).replace("\r", " ").replace("\n", " ").strip()
    for rx in _INJECTION_PATTERNS:
        cleaned = rx.sub("[filtered]", cleaned)
    return cleaned[:max_len]


def validate_external_api_url(url: str) -> str:
    """
    Validate an external LLM API base URL to prevent SSRF attacks against
    cloud metadata services (169.254.169.254) or non-HTTP schemes.
    """
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("https", "http"):
        raise ValueError("API base URL must use http:// or https://")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("API base URL must include a hostname")
    if parsed.scheme == "http" and host not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("HTTPS is required for non-local API base URLs")
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            raise ValueError("Blocked link-local/metadata IP address in API URL")
    except ValueError as exc:
        # Hostname (not an IP literal) raises ValueError from ip_address; re-raise our block error
        if "Blocked" in str(exc):
            raise
    return url.rstrip("/")
