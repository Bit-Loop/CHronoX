"""
Security & Envelope-Encrypted Vault utilities for ChronoX Finance.

Provides:
- Envelope encryption for local secrets (`secrets.enc`, mode 0600, AES-256-GCM):
  - Random 32-byte Data Encryption Key (DEK) seals the encrypted payload.
  - DEK is wrapped twice:
    1) By a password-derived KEK (PBKDF2-HMAC-SHA256, 600,000 iterations, random 16-byte salt)
    2) By a Recovery-Key-derived KEK (8-group Base32, 200 bits of entropy, shown once at setup)
  - Unlock succeeds iff AES-256-GCM DEK unwrap succeeds (no separate password hash stored).
  - Password change rewraps only the password-wrapped DEK without re-encrypting the payload.
  - Legacy `secrets.json` migration order: write `secrets.enc.tmp` -> fsync -> atomic rename
    -> decrypt-verify DEK + payload -> ONLY THEN unlink `secrets.json`.
  - NOTE: Only `secrets.enc` is encrypted at rest; the SQLite ledger DB is NOT encrypted at rest.
  - NOTE: On lock, DEK references are dropped best-effort. Python cannot guarantee deterministic
    RAM zeroization of immutable `bytes` objects prior to garbage collection.
- Ephemeral per-boot HMAC-SHA256 session signing key & per-boot CSRF token (rotated on lock)
- Strict loopback `Host` / `Origin` validators (`127.0.0.1:<port>` / `localhost:<port>`) for
  DNS-rebinding defense
- Sliding-window in-memory rate limiter
- CSV formula injection neutralization (`=`, `+`, `-`, `@`, tab, CR)
- PII redaction, prompt-injection sanitization, `<UNTRUSTED_DATA>` wrapping, & SSRF URL guard
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
from typing import Any, Deque, Dict, List, Literal, Optional, Tuple
from urllib.parse import urlparse

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PBKDF2_ITERATIONS = 600_000
DEK_BYTES = 32
SALT_BYTES = 16
NONCE_BYTES = 12
VAULT_ENVELOPE_VERSION = 2
MIN_VAULT_PASSWORD_LENGTH = 8

SESSION_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days
SESSION_COOKIE_NAME = "chronox_session"
CSRF_HEADER_NAME = "X-ChronoX-CSRF"

_AAD_DEK_PASSWORD = b"chronox:vault:v2:dek:password"
_AAD_DEK_RECOVERY = b"chronox:vault:v2:dek:recovery"
_AAD_PAYLOAD = b"chronox:vault:v2:payload"

_RECOVERY_ALPHABET = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")
_RECOVERY_GROUP_COUNT = 8
_RECOVERY_GROUP_LEN = 5
_RECOVERY_RAW_BYTES = 25  # 25 * 8 = 200 bits -> exactly 40 RFC-4648 base32 chars

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

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
    re.compile(r"</?UNTRUSTED_DATA[^>]*>", re.IGNORECASE),
]


class VaultError(Exception):
    """Base exception for ChronoX encrypted vault operations."""


class VaultLockedError(VaultError):
    """Raised when secret access is attempted while the vault is locked."""


class VaultNotInitializedError(VaultError):
    """Raised when an operation requires an initialized vault that has not been set up yet."""


class VaultAlreadyInitializedError(VaultError):
    """Raised when setup_vault is invoked on an already-initialized vault."""


class InvalidVaultCredentialsError(VaultError):
    """Raised when password or recovery key fails AES-256-GCM authentication."""


class VaultCorruptedError(VaultError):
    """Raised when secrets.enc fails structural or authenticated decryption checks."""


# --- Recovery Key Helpers (200-bit RFC 4648 Base32 in 8x5 groups) ---


def generate_recovery_key() -> str:
    """
    Generate a high-entropy 200-bit Recovery Key encoded as 8 dash-separated
    5-character RFC-4648 Base32 groups (`A-Z`, `2-7` — no `0/1/8/9` ambiguity).
    """
    raw = secrets.token_bytes(_RECOVERY_RAW_BYTES)
    encoded = base64.b32encode(raw).decode("ascii").rstrip("=")
    groups = [
        encoded[i : i + _RECOVERY_GROUP_LEN]
        for i in range(0, len(encoded), _RECOVERY_GROUP_LEN)
    ]
    return "-".join(groups)


def normalize_recovery_key(key: str) -> str:
    """Normalize and validate an 8-group Base32 recovery key into its canonical 40-char form."""
    cleaned = re.sub(r"[\s\-_]+", "", (key or "").strip().upper())
    expected_len = _RECOVERY_GROUP_COUNT * _RECOVERY_GROUP_LEN
    if len(cleaned) != expected_len or any(ch not in _RECOVERY_ALPHABET for ch in cleaned):
        raise ValueError("Recovery key must be 8 groups of 5 Base32 characters (A-Z, 2-7)")
    return cleaned


def split_recovery_key(key: str) -> List[str]:
    """Return the 8 canonical 5-char groups of a recovery key."""
    canonical = normalize_recovery_key(key)
    return [
        canonical[i : i + _RECOVERY_GROUP_LEN]
        for i in range(0, len(canonical), _RECOVERY_GROUP_LEN)
    ]


def build_recovery_challenge(count: int = 4) -> List[int]:
    """Select `count` sorted 1-based group indices (1..8) for the backup verification check."""
    bounded = max(1, min(_RECOVERY_GROUP_COUNT, int(count)))
    rng = secrets.SystemRandom()
    return sorted(rng.sample(range(1, _RECOVERY_GROUP_COUNT + 1), bounded))


def verify_recovery_challenge(recovery_key: str, responses: Dict[int, str]) -> bool:
    """Verify user-typed recovery key groups against expected 1-based indices in constant time."""
    if not responses:
        return False
    try:
        groups = split_recovery_key(recovery_key)
    except ValueError:
        return False
    for raw_idx, raw_val in responses.items():
        idx = int(raw_idx)
        if idx < 1 or idx > len(groups):
            return False
        provided = (raw_val or "").strip().upper()
        if not hmac.compare_digest(provided, groups[idx - 1]):
            return False
    return True


# --- Envelope Cryptography Helpers ---


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(encoded: str) -> bytes:
    return base64.b64decode(encoded.encode("ascii"))


def _derive_kek(secret_material: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256",
        secret_material.encode("utf-8"),
        salt,
        int(iterations),
        dklen=DEK_BYTES,
    )


def _aes_gcm_seal(key: bytes, plaintext: bytes, aad: bytes) -> Dict[str, str]:
    nonce = secrets.token_bytes(NONCE_BYTES)
    ct = AESGCM(key).encrypt(nonce, plaintext, aad)
    return {
        "nonce_b64": _b64e(nonce),
        "ct_b64": _b64e(ct),
    }


def _aes_gcm_open(key: bytes, box: Dict[str, str], aad: bytes) -> bytes:
    nonce = _b64d(box["nonce_b64"])
    ct = _b64d(box["ct_b64"])
    return AESGCM(key).decrypt(nonce, ct, aad)


def _atomic_write_fsync(path: Path, data: bytes) -> None:
    """Atomically write bytes with 0600 permissions, fsync file, rename, and best-effort fsync dir."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


# --- Legacy PBKDF2 Passcode Helpers (retained for backward compatibility) ---


def hash_passcode(passcode: str, salt: Optional[bytes] = None) -> str:
    """Hash a passcode with PBKDF2-HMAC-SHA256 and random salt."""
    if not passcode or len(passcode) < 6:
        raise ValueError("Passcode must be at least 6 characters")
    raw_salt = salt if salt is not None else secrets.token_bytes(SALT_BYTES)
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


# --- Session Tokens & Per-Boot CSRF ---


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


def verify_csrf_token(provided: Optional[str], expected: str) -> bool:
    """Constant-time verification of the per-boot CSRF header token."""
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided.strip(), expected)


# --- Localhost DNS-Rebinding & Origin Hardening ---


def _parse_host_header(raw_host: str) -> Tuple[Optional[str], Optional[int]]:
    """Parse an HTTP Host header into (lowercase_hostname, optional_port)."""
    host = (raw_host or "").strip()
    if not host or any(ch in host for ch in ("/", "\\", "@", " ", "\r", "\n", "\t")):
        return None, None
    parsed = urlparse(f"http://{host}")
    if parsed.username is not None or parsed.password is not None or parsed.path:
        return None, None
    try:
        port = parsed.port
    except ValueError:
        return None, None
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return None, None
    return hostname, port


def validate_loopback_host(
    host: Optional[str],
    *,
    allowed_port: Optional[int] = None,
    allow_testserver: bool = False,
) -> bool:
    """
    Strictly validate an HTTP `Host` header against loopback (`127.0.0.1`, `localhost`, `[::1]`)
    to defeat DNS-rebinding attacks against the local ChronoX API server.
    """
    hostname, port = _parse_host_header(host or "")
    if not hostname:
        return False
    if allow_testserver and hostname == "testserver":
        return True
    if hostname not in _LOOPBACK_HOSTS:
        return False
    if allowed_port is not None and port != allowed_port:
        return False
    return True


def validate_loopback_origin(
    origin: Optional[str],
    *,
    expected_host: Optional[str] = None,
    allowed_port: Optional[int] = None,
    allow_testserver: bool = False,
) -> bool:
    """
    Strictly validate an HTTP `Origin` (or `Referer`) header against loopback.
    Returns True if `origin` is omitted (empty/None); returns False for `null` or
    any non-loopback origin (defeating cross-site & DNS-rebinding attacks).
    """
    if origin is None or not origin.strip():
        return True
    raw = origin.strip()
    if raw.lower() == "null":
        return False
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    hostname = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        return False
    if allow_testserver and hostname == "testserver":
        return True
    if hostname not in _LOOPBACK_HOSTS:
        return False
    if allowed_port is not None and port != allowed_port:
        return False
    if expected_host:
        exp_host, exp_port = _parse_host_header(expected_host)
        if not exp_host:
            return False
        if not (allow_testserver and exp_host == "testserver"):
            if exp_port is not None and port != exp_port:
                return False
    return True


def mask_secret(value: str) -> str:
    """Return a non-reversible masked representation of an API key."""
    cleaned = (value or "").strip()
    if not cleaned:
        return ""
    if len(cleaned) <= 8:
        return "••••••••"
    return f"{cleaned[:3]}••••{cleaned[-4:]}"


# --- Envelope-Encrypted Local Secret Vault ---


class LocalSecretStore:
    """
    Envelope-encrypted local secret vault (`secrets.enc`, mode 0600, AES-256-GCM).

    - Random 32-byte DEK encrypts the secrets payload JSON.
    - DEK is wrapped twice: by password-derived KEK (PBKDF2-HMAC-SHA256, 600k iters)
      and by a high-entropy 8-group Base32 Recovery Key KEK.
    - Session signing key & CSRF token are strictly ephemeral per process boot
      (never persisted to disk) and rotated on `lock()`.
    - Legacy plaintext `secrets.json` is migrated safely: write `secrets.enc.tmp` ->
      `fsync` -> atomic rename -> decrypt-verify DEK + payload -> unlink `secrets.json`.
    - Note: On `lock()`, references to the in-memory DEK are dropped best-effort;
      Python does not guarantee deterministic RAM zeroization of immutable `bytes`.
    """

    def __init__(self, store_path: Path, *, kdf_iterations: int = PBKDF2_ITERATIONS):
        path = Path(store_path)
        if path.suffix == ".enc":
            self.enc_path = path
            self.legacy_path = path.with_suffix(".json")
        else:
            self.enc_path = path.with_suffix(".enc")
            self.legacy_path = path
        self.store_path = self.enc_path
        self.lease_path = self.enc_path.with_name(f"{self.enc_path.stem}.lease.json")
        self._kdf_iterations = max(10_000, int(kdf_iterations))
        self._lock = threading.RLock()
        self._dek: Optional[bytes] = None
        self._ephemeral_mode: bool = False
        self._ephemeral_data: Dict[str, str] = {}
        self._signing_key: str = secrets.token_urlsafe(48)
        self._csrf_token: str = secrets.token_urlsafe(32)
        self._lease_expires_at: Optional[int] = None
        self._last_activity: float = time.monotonic()
        self.enc_path.parent.mkdir(parents=True, exist_ok=True)
        self._restore_session_lease_if_valid()

    # --- Vault Lifecycle & State ---

    def _restore_session_lease_if_valid(self) -> None:
        """Restore an explicit opt-in local session lease (`0600`) if unexpired and valid."""
        with self._lock:
            if not self.lease_path.exists() or not self.enc_path.exists():
                return
            try:
                raw = json.loads(self.lease_path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict) or raw.get("version") != 1:
                    self.lease_path.unlink(missing_ok=True)
                    return
                exp = int(raw.get("expires_at", 0))
                if exp <= int(time.time()):
                    self.lease_path.unlink(missing_ok=True)
                    return
                dek = _b64d(str(raw["dek_b64"]))
                if len(dek) != DEK_BYTES:
                    self.lease_path.unlink(missing_ok=True)
                    return
                envelope = self._read_envelope()
                self._decrypt_payload_with_dek(envelope, dek)
                signing_key = str(raw.get("signing_key") or "").strip()
                csrf_token = str(raw.get("csrf_token") or "").strip()
                self._dek = dek
                self._ephemeral_mode = False
                if signing_key:
                    self._signing_key = signing_key
                if csrf_token:
                    self._csrf_token = csrf_token
                self._lease_expires_at = exp
                self._last_activity = time.monotonic()
            except Exception:
                self.lease_path.unlink(missing_ok=True)

    def grant_session_lease(self, ttl_seconds: int = SESSION_TTL_SECONDS) -> int:
        """
        Persist an explicit opt-in local machine session lease (`0600`) for `ttl_seconds`
        (up to 7 days) so local dev/TDD restarts remain unlocked until expiry or `lock()`.
        """
        bounded_ttl = max(60, min(int(ttl_seconds), SESSION_TTL_SECONDS))
        with self._lock:
            if self._dek is None or self._ephemeral_mode or not self.enc_path.exists():
                raise VaultLockedError("Vault must be unlocked to grant a session lease")
            exp = int(time.time()) + bounded_ttl
            lease_obj = {
                "version": 1,
                "dek_b64": _b64e(self._dek),
                "signing_key": self._signing_key,
                "csrf_token": self._csrf_token,
                "expires_at": exp,
            }
            _atomic_write_fsync(
                self.lease_path,
                json.dumps(lease_obj, indent=2, sort_keys=True).encode("utf-8"),
            )
            self._lease_expires_at = exp
            self._last_activity = time.monotonic()
            return exp

    def revoke_session_lease(self) -> None:
        """Delete any persisted local session lease from disk immediately."""
        with self._lock:
            self._lease_expires_at = None
            if self.lease_path.exists():
                self.lease_path.unlink(missing_ok=True)

    def get_session_lease_expires_at(self) -> Optional[int]:
        """Return UTC epoch timestamp when the active session lease expires, or None."""
        with self._lock:
            if self._lease_expires_at is None:
                return None
            if self._lease_expires_at <= int(time.time()):
                self.revoke_session_lease()
                return None
            return self._lease_expires_at

    def has_active_session_lease(self) -> bool:
        with self._lock:
            return self._dek is not None and self.get_session_lease_expires_at() is not None

    def is_initialized(self) -> bool:
        with self._lock:
            return self.enc_path.exists()

    def is_unlocked(self) -> bool:
        with self._lock:
            return self._dek is not None

    def has_legacy_plaintext(self) -> bool:
        with self._lock:
            return self.legacy_path.exists()

    def vault_state(self) -> Literal["needs_setup", "locked", "unlocked"]:
        with self._lock:
            if self._ephemeral_mode and self._dek is not None:
                return "unlocked"
            if not self.enc_path.exists():
                return "needs_setup"
            return "unlocked" if self._dek is not None else "locked"

    def touch(self) -> None:
        with self._lock:
            self._last_activity = time.monotonic()

    def check_idle_lock(self, auto_lock_minutes: int) -> bool:
        """Lock vault if idle beyond `auto_lock_minutes` (0 disables idle auto-lock)."""
        with self._lock:
            if self._dek is None:
                return False
            # If an explicit multi-day session lease was active and has now expired, enforce lock
            if self._lease_expires_at is not None and self._lease_expires_at <= int(time.time()):
                self.lock()
                return True
            if auto_lock_minutes <= 0:
                return False
            if (time.monotonic() - self._last_activity) >= float(auto_lock_minutes) * 60.0:
                self.lock()
                return True
            return False

    def unlock_ephemeral_for_testing(self) -> None:
        """Test-only helper: enable an in-memory DEK without writing plaintext to disk."""
        with self._lock:
            self._dek = secrets.token_bytes(DEK_BYTES)
            self._ephemeral_mode = True
            self._ephemeral_data = {}
            self._last_activity = time.monotonic()

    def _read_envelope(self) -> Dict[str, Any]:
        if not self.enc_path.exists():
            raise VaultNotInitializedError("Vault has not been initialized")
        try:
            env = json.loads(self.enc_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VaultCorruptedError(f"Failed to read vault envelope: {exc}") from exc
        if not isinstance(env, dict) or env.get("version") != VAULT_ENVELOPE_VERSION:
            raise VaultCorruptedError("Unsupported or corrupted vault envelope format")
        for req in ("kdf", "wrapped_dek_password", "wrapped_dek_recovery", "payload"):
            if req not in env or not isinstance(env[req], dict):
                raise VaultCorruptedError(f"Vault envelope missing section: {req}")
        return env

    def _read_legacy_plaintext(self) -> Tuple[Dict[str, str], bool]:
        if not self.legacy_path.exists():
            return {}, False
        try:
            raw = json.loads(self.legacy_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}, True
        if not isinstance(raw, dict):
            return {}, True
        migrated: Dict[str, str] = {}
        for k, v in raw.items():
            # Never migrate legacy persisted session signing key; signing keys are ephemeral per boot
            if k == "session_signing_key":
                continue
            if isinstance(k, str) and v is not None and str(v):
                migrated[k] = str(v)
        return migrated, True

    def _decrypt_payload_with_dek(self, envelope: Dict[str, Any], dek: bytes) -> Dict[str, str]:
        try:
            pt = _aes_gcm_open(dek, envelope["payload"], _AAD_PAYLOAD)
            data = json.loads(pt.decode("utf-8"))
        except (InvalidTag, KeyError, ValueError, json.JSONDecodeError) as exc:
            raise VaultCorruptedError("Failed to authenticate/decrypt vault secrets payload") from exc
        if not isinstance(data, dict):
            raise VaultCorruptedError("Vault payload is not a JSON object")
        return {str(k): str(v) for k, v in data.items() if v is not None}

    def _wrap_dek(self, dek: bytes, secret_material: str, aad: bytes, iterations: int) -> Dict[str, str]:
        salt = secrets.token_bytes(SALT_BYTES)
        kek = _derive_kek(secret_material, salt, iterations)
        box = _aes_gcm_seal(kek, dek, aad)
        return {
            "salt_b64": _b64e(salt),
            "nonce_b64": box["nonce_b64"],
            "ct_b64": box["ct_b64"],
        }

    def _unwrap_dek(self, wrapped: Dict[str, str], secret_material: str, aad: bytes, iterations: int) -> bytes:
        salt = _b64d(wrapped["salt_b64"])
        kek = _derive_kek(secret_material, salt, iterations)
        dek = _aes_gcm_open(kek, wrapped, aad)
        if len(dek) != DEK_BYTES:
            raise VaultCorruptedError("Unwrapped DEK has invalid length")
        return dek

    def _write_and_verify_envelope(
        self,
        envelope: Dict[str, Any],
        *,
        expected_dek: bytes,
        expected_payload: Optional[Dict[str, str]] = None,
        verify_password: Optional[str] = None,
        verify_recovery_canonical: Optional[str] = None,
    ) -> None:
        raw_bytes = json.dumps(envelope, indent=2, sort_keys=True).encode("utf-8")
        _atomic_write_fsync(self.enc_path, raw_bytes)

        # Read back from disk and decrypt-verify before returning (or unlinking legacy plaintext!)
        reloaded = self._read_envelope()
        iters = int(reloaded["kdf"]["iterations"])
        if verify_password is not None:
            pw_dek = self._unwrap_dek(
                reloaded["wrapped_dek_password"], verify_password, _AAD_DEK_PASSWORD, iters
            )
            if not hmac.compare_digest(pw_dek, expected_dek):
                raise VaultCorruptedError("Post-write password DEK verification mismatch")
        if verify_recovery_canonical is not None:
            rec_dek = self._unwrap_dek(
                reloaded["wrapped_dek_recovery"],
                verify_recovery_canonical,
                _AAD_DEK_RECOVERY,
                iters,
            )
            if not hmac.compare_digest(rec_dek, expected_dek):
                raise VaultCorruptedError("Post-write recovery DEK verification mismatch")
        decrypted = self._decrypt_payload_with_dek(reloaded, expected_dek)
        if expected_payload is not None and decrypted != expected_payload:
            raise VaultCorruptedError("Post-write payload decrypt-verify mismatch")

    def setup_vault(
        self,
        password: str,
        *,
        initial_secrets: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Initialize a new envelope-encrypted vault, migrate any legacy `secrets.json`,
        and return the one-time Recovery Key + challenge indices.
        """
        if not password or len(password) < MIN_VAULT_PASSWORD_LENGTH:
            raise ValueError(f"Vault password must be at least {MIN_VAULT_PASSWORD_LENGTH} characters")

        with self._lock:
            if self.enc_path.exists():
                raise VaultAlreadyInitializedError("Vault is already initialized")

            dek = secrets.token_bytes(DEK_BYTES)
            recovery_key = generate_recovery_key()
            recovery_canonical = normalize_recovery_key(recovery_key)

            legacy_data, had_legacy = self._read_legacy_plaintext()
            payload_dict: Dict[str, str] = dict(legacy_data)
            if initial_secrets:
                for k, v in initial_secrets.items():
                    if k and v:
                        payload_dict[str(k)] = str(v)

            iters = self._kdf_iterations
            wrapped_pw = self._wrap_dek(dek, password, _AAD_DEK_PASSWORD, iters)
            wrapped_rec = self._wrap_dek(dek, recovery_canonical, _AAD_DEK_RECOVERY, iters)
            sealed_payload = _aes_gcm_seal(
                dek,
                json.dumps(payload_dict, sort_keys=True).encode("utf-8"),
                _AAD_PAYLOAD,
            )

            envelope: Dict[str, Any] = {
                "version": VAULT_ENVELOPE_VERSION,
                "kdf": {
                    "algorithm": "pbkdf2_hmac_sha256",
                    "iterations": iters,
                },
                "wrapped_dek_password": wrapped_pw,
                "wrapped_dek_recovery": wrapped_rec,
                "payload": sealed_payload,
            }

            # Write -> fsync -> rename -> decrypt-verify DEK (both wrappers) + payload
            self._write_and_verify_envelope(
                envelope,
                expected_dek=dek,
                expected_payload=payload_dict,
                verify_password=password,
                verify_recovery_canonical=recovery_canonical,
            )

            # ONLY AFTER decrypt-verify succeeds, unlink legacy plaintext secrets.json
            migrated_legacy = False
            if had_legacy and self.legacy_path.exists():
                self.legacy_path.unlink(missing_ok=True)
                migrated_legacy = True

            self._dek = dek
            self._ephemeral_mode = False
            self._ephemeral_data = {}
            self._last_activity = time.monotonic()

            return {
                "recovery_key": recovery_key,
                "recovery_groups": split_recovery_key(recovery_key),
                "challenge_indices": build_recovery_challenge(4),
                "migrated_legacy_secrets": migrated_legacy,
            }

    def unlock(self, password: str) -> bool:
        """
        Unlock the vault by unwrapping the DEK with the user password.
        Returns True iff AES-256-GCM DEK unwrap and payload decryption succeed.
        """
        with self._lock:
            envelope = self._read_envelope()
            iters = int(envelope["kdf"]["iterations"])
            try:
                dek = self._unwrap_dek(
                    envelope["wrapped_dek_password"], password, _AAD_DEK_PASSWORD, iters
                )
            except (InvalidTag, ValueError, KeyError):
                return False

            payload_dict = self._decrypt_payload_with_dek(envelope, dek)

            # Complete any pending legacy secrets.json migration safely
            if self.legacy_path.exists():
                legacy_data, _ = self._read_legacy_plaintext()
                merged = dict(legacy_data)
                merged.update(payload_dict)
                envelope["payload"] = _aes_gcm_seal(
                    dek,
                    json.dumps(merged, sort_keys=True).encode("utf-8"),
                    _AAD_PAYLOAD,
                )
                self._write_and_verify_envelope(
                    envelope,
                    expected_dek=dek,
                    expected_payload=merged,
                    verify_password=password,
                )
                self.legacy_path.unlink(missing_ok=True)

            self._dek = dek
            self._ephemeral_mode = False
            self._last_activity = time.monotonic()
            return True

    def recover(
        self,
        recovery_key: str,
        new_password: str,
        *,
        rotate_recovery_key: bool = False,
    ) -> Optional[str]:
        """
        Recover vault access using the 8-group Base32 Recovery Key and set a new password.
        Rewraps the DEK without re-encrypting the secrets payload.
        """
        if not new_password or len(new_password) < MIN_VAULT_PASSWORD_LENGTH:
            raise ValueError(f"New vault password must be at least {MIN_VAULT_PASSWORD_LENGTH} characters")
        try:
            canonical_rec = normalize_recovery_key(recovery_key)
        except ValueError as exc:
            raise InvalidVaultCredentialsError(str(exc)) from exc

        with self._lock:
            envelope = self._read_envelope()
            iters = int(envelope["kdf"]["iterations"])
            try:
                dek = self._unwrap_dek(
                    envelope["wrapped_dek_recovery"], canonical_rec, _AAD_DEK_RECOVERY, iters
                )
            except (InvalidTag, ValueError, KeyError) as exc:
                raise InvalidVaultCredentialsError("Invalid recovery key") from exc

            # Verify payload authenticates under unwrapped DEK
            self._decrypt_payload_with_dek(envelope, dek)

            new_recovery_key: Optional[str] = None
            verify_rec = canonical_rec
            envelope["wrapped_dek_password"] = self._wrap_dek(
                dek, new_password, _AAD_DEK_PASSWORD, iters
            )
            if rotate_recovery_key:
                new_recovery_key = generate_recovery_key()
                verify_rec = normalize_recovery_key(new_recovery_key)
                envelope["wrapped_dek_recovery"] = self._wrap_dek(
                    dek, verify_rec, _AAD_DEK_RECOVERY, iters
                )

            self._write_and_verify_envelope(
                envelope,
                expected_dek=dek,
                verify_password=new_password,
                verify_recovery_canonical=verify_rec,
            )

            self.revoke_session_lease()
            self._dek = dek
            self._ephemeral_mode = False
            self._signing_key = secrets.token_urlsafe(48)
            self._csrf_token = secrets.token_urlsafe(32)
            self._last_activity = time.monotonic()
            return new_recovery_key

    def change_password(self, current_password: str, new_password: str) -> None:
        """
        Change the vault password by re-wrapping ONLY the password-wrapped DEK.
        The encrypted secrets payload is NEVER re-encrypted or modified.
        """
        if not new_password or len(new_password) < MIN_VAULT_PASSWORD_LENGTH:
            raise ValueError(f"New vault password must be at least {MIN_VAULT_PASSWORD_LENGTH} characters")

        with self._lock:
            envelope = self._read_envelope()
            iters = int(envelope["kdf"]["iterations"])
            try:
                dek = self._unwrap_dek(
                    envelope["wrapped_dek_password"], current_password, _AAD_DEK_PASSWORD, iters
                )
            except (InvalidTag, ValueError, KeyError) as exc:
                raise InvalidVaultCredentialsError("Current vault password is incorrect") from exc

            # Rewrap DEK only; envelope["payload"] stays bit-for-bit untouched
            envelope["wrapped_dek_password"] = self._wrap_dek(
                dek, new_password, _AAD_DEK_PASSWORD, iters
            )
            self._write_and_verify_envelope(
                envelope,
                expected_dek=dek,
                verify_password=new_password,
            )

            self.revoke_session_lease()
            self._dek = dek
            self._signing_key = secrets.token_urlsafe(48)
            self._csrf_token = secrets.token_urlsafe(32)
            self._last_activity = time.monotonic()

    def lock(self) -> None:
        """
        Lock the vault, revoke any persisted session lease, drop in-memory DEK reference
        (best-effort — Python cannot guarantee immediate physical RAM wipe), and rotate
        signing & CSRF keys.
        """
        with self._lock:
            self.revoke_session_lease()
            self._dek = None
            self._ephemeral_data.clear()
            self._signing_key = secrets.token_urlsafe(48)
            self._csrf_token = secrets.token_urlsafe(32)

    # --- Secret CRUD (Fail-Closed When Locked) ---

    def _read_secrets_unlocked(self) -> Dict[str, str]:
        if self._ephemeral_mode:
            if self._dek is None:
                raise VaultLockedError("Sync paused: vault locked")
            return dict(self._ephemeral_data)
        if not self.enc_path.exists():
            return {}
        if self._dek is None:
            raise VaultLockedError("Sync paused: vault locked")
        envelope = self._read_envelope()
        return self._decrypt_payload_with_dek(envelope, self._dek)

    def _write_secrets_unlocked(self, data: Dict[str, str]) -> None:
        if self._ephemeral_mode:
            if self._dek is None:
                raise VaultLockedError("Sync paused: vault locked")
            self._ephemeral_data = dict(data)
            self._last_activity = time.monotonic()
            return
        if not self.enc_path.exists():
            raise VaultNotInitializedError("Vault must be initialized before storing secrets")
        if self._dek is None:
            raise VaultLockedError("Sync paused: vault locked")
        envelope = self._read_envelope()
        envelope["payload"] = _aes_gcm_seal(
            self._dek,
            json.dumps(data, sort_keys=True).encode("utf-8"),
            _AAD_PAYLOAD,
        )
        self._write_and_verify_envelope(
            envelope,
            expected_dek=self._dek,
            expected_payload=data,
        )
        self._last_activity = time.monotonic()

    def get(self, key: str) -> str:
        with self._lock:
            return str(self._read_secrets_unlocked().get(key, ""))

    def set(self, key: str, value: str) -> None:
        with self._lock:
            data = self._read_secrets_unlocked()
            if value:
                data[key] = str(value)
            else:
                data.pop(key, None)
            self._write_secrets_unlocked(data)

    def delete(self, key: str) -> None:
        self.set(key, "")

    def get_json(self, key: str) -> Optional[Dict[str, Any]]:
        raw = self.get(key)
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None

    def set_json(self, key: str, value: Optional[Dict[str, Any]]) -> None:
        if value is None:
            self.delete(key)
        else:
            self.set(key, json.dumps(value, sort_keys=True))

    def list_keys(self) -> List[str]:
        with self._lock:
            return sorted(self._read_secrets_unlocked().keys())

    # --- Ephemeral Per-Boot Signing Key & CSRF Token ---

    def get_or_create_signing_key(self) -> str:
        """
        Return the ephemeral per-boot HMAC signing key (never persisted to disk,
        rotated on `lock()` so every restart/lock forces vault authentication).
        """
        with self._lock:
            return self._signing_key

    def get_csrf_token(self) -> str:
        """Return the ephemeral per-boot CSRF header token (rotated on `lock()`)."""
        with self._lock:
            return self._csrf_token

    def verify_csrf(self, provided: Optional[str]) -> bool:
        """Constant-time check against the current per-boot CSRF token."""
        with self._lock:
            return verify_csrf_token(provided, self._csrf_token)


class LocalVaultAuth:
    """
    Default local-desktop implementation of `AuthProvider` backed by `LocalSecretStore`.
    Swappable for a future SaaS/iOS `BearerJWTAuth` implementation.
    """

    def __init__(self, secret_store: LocalSecretStore):
        self.secrets = secret_store

    def vault_state(self) -> str:
        return self.secrets.vault_state()

    def is_authenticated(self, token: Optional[str]) -> bool:
        if not token or not self.secrets.is_unlocked():
            return False
        subject = verify_session_token(token, self.secrets.get_or_create_signing_key())
        return subject is not None

    def issue_session_token(self, subject: str = "owner", ttl: int = SESSION_TTL_SECONDS) -> str:
        return create_session_token(self.secrets.get_or_create_signing_key(), subject=subject, ttl=ttl)

    def get_csrf_token(self) -> str:
        return self.secrets.get_csrf_token()

    def verify_csrf(self, token: Optional[str]) -> bool:
        return self.secrets.verify_csrf(token)


# --- Rate Limiter & Input Sanitization ---


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
                    oldest_key = min(
                        self._buckets,
                        key=lambda k: self._buckets[k][-1] if self._buckets[k] else 0.0,
                    )
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
    """Redact PII and neutralize prompt-injection patterns in merchant/memo/email text."""
    cleaned = redact_pii(text).replace("\r", " ").replace("\n", " ").strip()
    for rx in _INJECTION_PATTERNS:
        cleaned = rx.sub("[filtered]", cleaned)
    return cleaned[:max_len]


def wrap_untrusted_for_llm(label: str, text: str, max_len: int = 300) -> str:
    """Sanitize and wrap untrusted text in explicit `<UNTRUSTED_DATA>` delimiters for LLMs."""
    safe_label = re.sub(r"[^a-zA-Z0-9_\-]", "", label or "data")[:32] or "data"
    cleaned = sanitize_untrusted_text(text, max_len=max_len)
    return f'<UNTRUSTED_DATA label="{safe_label}">{cleaned}</UNTRUSTED_DATA>'


def validate_external_api_url(url: str) -> str:
    """
    Validate an external API base URL to prevent SSRF attacks against
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
        if "Blocked" in str(exc):
            raise
    return url.rstrip("/")
