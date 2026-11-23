"""
Atomic SQLite storage backend for ChronoX Finance.

Design guarantees:
- All money stored as exact integer cents (INTEGER).
- WAL mode + foreign keys enforced + thread-safe lock.
- Idempotent transaction deduplication via SHA-256 fingerprint.
- First-run bootstrapping of ADHD-optimized 5-Jar system and starter accounts.
- Full JSON backup export & restore.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from chronox_finance.models import AccountType, JarKind, utc_now_iso
from chronox_finance.security import validate_external_api_url

DEFAULT_JARS: List[Dict[str, Any]] = [
    {
        "slug": "bills",
        "name": "Bills & Essentials",
        "kind": JarKind.BILLS.value,
        "icon": "🏠",
        "color": "#60a5fa",
        "priority": 1,
        "target_pct": 45.0,
        "monthly_target_cents": 200_000,
        "is_locked": 1,
        "rollover_enabled": 1,
        "rollover_cap_cents": 150_000,
    },
    {
        "slug": "daily",
        "name": "Daily Living",
        "kind": JarKind.DAILY.value,
        "icon": "🛒",
        "color": "#34d399",
        "priority": 2,
        "target_pct": 20.0,
        "monthly_target_cents": 85_000,
        "is_locked": 0,
        "rollover_enabled": 1,
        "rollover_cap_cents": 40_000,
    },
    {
        "slug": "fun",
        "name": "Guilt-Free Fun",
        "kind": JarKind.FUN.value,
        "icon": "✨",
        "color": "#fbbf24",
        "priority": 3,
        "target_pct": 15.0,
        "monthly_target_cents": 60_000,
        "is_locked": 0,
        "rollover_enabled": 1,
        "rollover_cap_cents": 35_000,
    },
    {
        "slug": "safety",
        "name": "Safety & Future",
        "kind": JarKind.SAFETY.value,
        "icon": "🛡️",
        "color": "#a78bfa",
        "priority": 4,
        "target_pct": 15.0,
        "monthly_target_cents": 60_000,
        "is_locked": 1,
        "rollover_enabled": 1,
        "rollover_cap_cents": None,
    },
    {
        "slug": "invest",
        "name": "ChronoX Invest Vault",
        "kind": JarKind.INVEST.value,
        "icon": "📈",
        "color": "#2dd4bf",
        "priority": 5,
        "target_pct": 5.0,
        "monthly_target_cents": 25_000,
        "is_locked": 1,
        "rollover_enabled": 1,
        "rollover_cap_cents": None,
    },
]

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS profile_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    currency TEXT NOT NULL DEFAULT 'USD',
    hourly_wage_cents INTEGER NOT NULL DEFAULT 3000,
    emergency_buffer_cents INTEGER NOT NULL DEFAULT 50000,
    default_cooling_hours INTEGER NOT NULL DEFAULT 48,
    payday_cadence_days INTEGER NOT NULL DEFAULT 14,
    next_payday_date TEXT,
    expected_paycheck_cents INTEGER NOT NULL DEFAULT 220000,
    calm_mode INTEGER NOT NULL DEFAULT 1,
    auto_rebalance_enabled INTEGER NOT NULL DEFAULT 1,
    opencode_base_url TEXT NOT NULL DEFAULT 'https://opencode.ai/zen/go/v1',
    opencode_model TEXT NOT NULL DEFAULT 'glm-5.3-flash',
    opencode_protocol TEXT NOT NULL DEFAULT 'auto',
    opencode_max_daily_requests INTEGER NOT NULL DEFAULT 60,
    intelligence_mode TEXT NOT NULL DEFAULT 'hybrid',
    ai_provider TEXT NOT NULL DEFAULT 'auto',
    gemini_model TEXT NOT NULL DEFAULT 'gemini-2.5-flash',
    gemini_base_url TEXT NOT NULL DEFAULT 'https://generativelanguage.googleapis.com/v1beta',
    passcode_hash TEXT NOT NULL DEFAULT '',
    last_rollover_ym TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    account_type TEXT NOT NULL,
    balance_cents INTEGER NOT NULL DEFAULT 0,
    institution TEXT NOT NULL DEFAULT '',
    is_liquid INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jars (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    icon TEXT NOT NULL DEFAULT '💰',
    color TEXT NOT NULL DEFAULT '#60a5fa',
    priority INTEGER NOT NULL DEFAULT 3,
    target_pct REAL NOT NULL DEFAULT 20.0,
    monthly_target_cents INTEGER NOT NULL DEFAULT 0,
    auto_allocated_cents INTEGER NOT NULL DEFAULT 0,
    rollover_cents INTEGER NOT NULL DEFAULT 0,
    is_locked INTEGER NOT NULL DEFAULT 0,
    rollover_enabled INTEGER NOT NULL DEFAULT 1,
    rollover_cap_cents INTEGER,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    jar_id INTEGER REFERENCES jars(id) ON DELETE SET NULL,
    tx_type TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    merchant TEXT NOT NULL,
    normalized_merchant TEXT NOT NULL,
    subcategory TEXT NOT NULL DEFAULT '',
    memo TEXT NOT NULL DEFAULT '',
    tx_date TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    needs_review INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'manual',
    fingerprint TEXT UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(tx_date DESC);
CREATE INDEX IF NOT EXISTS idx_tx_jar ON transactions(jar_id, tx_date DESC);
CREATE INDEX IF NOT EXISTS idx_tx_norm_merchant ON transactions(normalized_merchant);

CREATE TABLE IF NOT EXISTS merchant_rules (
    normalized_merchant TEXT PRIMARY KEY,
    jar_id INTEGER NOT NULL REFERENCES jars(id) ON DELETE CASCADE,
    subcategory TEXT NOT NULL DEFAULT '',
    hit_count INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recurring_bills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    normalized_merchant TEXT NOT NULL UNIQUE,
    amount_cents INTEGER NOT NULL,
    previous_amount_cents INTEGER,
    cadence TEXT NOT NULL DEFAULT 'monthly',
    next_due_date TEXT NOT NULL,
    jar_id INTEGER REFERENCES jars(id) ON DELETE SET NULL,
    is_subscription INTEGER NOT NULL DEFAULT 0,
    is_essential INTEGER NOT NULL DEFAULT 1,
    auto_pay INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    confidence REAL NOT NULL DEFAULT 0.9,
    flagged_zombie INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS impulse_vault (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_name TEXT NOT NULL,
    price_cents INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    cooling_hours INTEGER NOT NULL DEFAULT 48,
    created_at TEXT NOT NULL,
    unlocks_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'cooling',
    decision_at TEXT
);

CREATE TABLE IF NOT EXISTS budget_transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_ym TEXT NOT NULL,
    from_jar_id INTEGER NOT NULL REFERENCES jars(id) ON DELETE CASCADE,
    to_jar_id INTEGER NOT NULL REFERENCES jars(id) ON DELETE CASCADE,
    amount_cents INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS budget_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS adhd_progress (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    weekly_reset_streak INTEGER NOT NULL DEFAULT 0,
    grace_days_available INTEGER NOT NULL DEFAULT 1,
    last_reset_date TEXT,
    impulses_skipped_count INTEGER NOT NULL DEFAULT 0,
    impulse_money_saved_cents INTEGER NOT NULL DEFAULT 0,
    dopamine_xp INTEGER NOT NULL DEFAULT 0,
    recent_wins_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_usage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    usage_date TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    cached INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ai_usage_date ON ai_usage_log(usage_date);

CREATE TABLE IF NOT EXISTS credit_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    issuer TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'credit_card',
    balance_cents INTEGER NOT NULL DEFAULT 0,
    credit_limit_cents INTEGER NOT NULL DEFAULT 0,
    apr_bps INTEGER NOT NULL DEFAULT 2499,
    min_payment_cents INTEGER NOT NULL DEFAULT 0,
    statement_closing_day INTEGER NOT NULL DEFAULT 15,
    payment_due_day INTEGER NOT NULL DEFAULT 10,
    promo_apr_bps INTEGER,
    promo_ends_date TEXT,
    opened_date TEXT,
    has_grace_period INTEGER NOT NULL DEFAULT 1,
    autopay_mode TEXT NOT NULL DEFAULT 'statement',
    rewards_note TEXT NOT NULL DEFAULT '',
    account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    target_cents INTEGER NOT NULL,
    saved_cents INTEGER NOT NULL DEFAULT 0,
    target_date TEXT,
    priority INTEGER NOT NULL DEFAULT 2,
    category TEXT NOT NULL DEFAULT 'gadget',
    funding_jar_slug TEXT NOT NULL DEFAULT 'fun',
    status TEXT NOT NULL DEFAULT 'active',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def compute_tx_fingerprint(
    account_id: int,
    tx_date: str,
    tx_type: str,
    amount_cents: int,
    normalized_merchant: str,
    memo: str = "",
    external_id: str = "",
) -> str:
    """Deterministic SHA-256 fingerprint for idempotent CSV/OFX/QIF/JSON bank imports."""
    if external_id and external_id.strip():
        raw = f"fitid|{account_id}|{external_id.strip()}"
    else:
        raw = f"{account_id}|{tx_date}|{tx_type}|{amount_cents}|{normalized_merchant}|{memo.strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class FinanceStorage:
    """Thread-safe SQLite storage layer for ChronoX Finance."""

    def __init__(self, db_path: str = ".chronox/finance.sqlite3"):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._tx_depth = 0
        if db_path != ":memory:":
            parent = Path(db_path).parent
            parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(parent, 0o700)
            except OSError:
                pass
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()
        if db_path != ":memory:":
            try:
                os.chmod(db_path, 0o600)
            except OSError:
                pass

    def _init_db(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON;")
            if self.db_path != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL;")
            self._conn.execute("PRAGMA busy_timeout = 5000;")
            self._conn.executescript(SCHEMA_SQL)
            # Lightweight forward schema migration for existing databases
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(profile_settings)").fetchall()}
            migrations = [
                ("last_rollover_ym", "TEXT NOT NULL DEFAULT ''"),
                ("intelligence_mode", "TEXT NOT NULL DEFAULT 'hybrid'"),
                ("ai_provider", "TEXT NOT NULL DEFAULT 'auto'"),
                ("gemini_model", "TEXT NOT NULL DEFAULT 'gemini-2.5-flash'"),
                ("gemini_base_url", "TEXT NOT NULL DEFAULT 'https://generativelanguage.googleapis.com/v1beta'"),
            ]
            for col_name, col_def in migrations:
                if col_name not in cols:
                    self._conn.execute(f"ALTER TABLE profile_settings ADD COLUMN {col_name} {col_def}")
            self._seed_defaults()
            self._conn.commit()

    def _seed_defaults(self) -> None:
        now = utc_now_iso()
        default_payday = (date.today() + timedelta(days=7)).isoformat()
        env_mode = (os.getenv("CHRONOX_INTELLIGENCE_MODE") or "hybrid").strip().lower()
        if env_mode not in ("deterministic_only", "hybrid", "ai_standalone"):
            env_mode = "hybrid"
        env_provider = (os.getenv("CHRONOX_AI_PROVIDER") or "auto").strip().lower()
        if env_provider not in ("auto", "opencode_go", "gemini"):
            env_provider = "auto"
        env_gemini_model = (os.getenv("GEMINI_MODEL") or "gemini-2.5-flash").strip() or "gemini-2.5-flash"
        env_gemini_url = (os.getenv("GEMINI_BASE_URL") or "https://generativelanguage.googleapis.com/v1beta").strip()
        try:
            env_gemini_url = validate_external_api_url(env_gemini_url)
        except ValueError:
            env_gemini_url = "https://generativelanguage.googleapis.com/v1beta"

        self._conn.execute(
            """
            INSERT OR IGNORE INTO profile_settings (
                id, next_payday_date, intelligence_mode, ai_provider, gemini_model, gemini_base_url, updated_at
            )
            VALUES (1, ?, ?, ?, ?, ?, ?)
            """,
            (default_payday, env_mode, env_provider, env_gemini_model, env_gemini_url, now),
        )
        self._conn.execute(
            """
            INSERT OR IGNORE INTO adhd_progress (id, recent_wins_json, updated_at)
            VALUES (1, '[]', ?)
            """,
            (now,),
        )

        # Seed ADHD 5-Jar system if no jars exist
        jar_count = self._conn.execute("SELECT COUNT(*) FROM jars").fetchone()[0]
        if jar_count == 0:
            for jar in DEFAULT_JARS:
                self._conn.execute(
                    """
                    INSERT INTO jars (
                        slug, name, kind, icon, color, priority, target_pct,
                        monthly_target_cents, auto_allocated_cents, rollover_cents,
                        is_locked, rollover_enabled, rollover_cap_cents, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                    """,
                    (
                        jar["slug"],
                        jar["name"],
                        jar["kind"],
                        jar["icon"],
                        jar["color"],
                        jar["priority"],
                        jar["target_pct"],
                        jar["monthly_target_cents"],
                        jar["monthly_target_cents"],
                        jar["is_locked"],
                        jar["rollover_enabled"],
                        jar["rollover_cap_cents"],
                        now,
                    ),
                )

        # Seed default primary checking account if no accounts exist
        acct_count = self._conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        if acct_count == 0:
            self._conn.execute(
                """
                INSERT INTO accounts (name, account_type, balance_cents, institution, is_liquid, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                ("Main Checking", AccountType.CHECKING.value, 0, "Local", now, now),
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Reentrant atomic transaction context manager (commits only on outermost scope)."""
        with self._lock:
            self._tx_depth += 1
            try:
                yield self._conn
                if self._tx_depth == 1:
                    self._conn.commit()
            except Exception:
                if self._tx_depth == 1:
                    self._conn.rollback()
                raise
            finally:
                self._tx_depth -= 1

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def ping(self) -> bool:
        """Lightweight DB connectivity check for /api/health."""
        try:
            with self._lock:
                row = self._conn.execute("SELECT 1").fetchone()
                return bool(row and row[0] == 1)
        except sqlite3.Error:
            return False

    # --- Settings ---

    def get_settings(self) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM profile_settings WHERE id = 1").fetchone()
            return dict(row) if row else {}

    def update_settings(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        allowed = {
            "currency",
            "hourly_wage_cents",
            "emergency_buffer_cents",
            "default_cooling_hours",
            "payday_cadence_days",
            "next_payday_date",
            "expected_paycheck_cents",
            "calm_mode",
            "auto_rebalance_enabled",
            "intelligence_mode",
            "ai_provider",
            "opencode_base_url",
            "opencode_model",
            "opencode_protocol",
            "opencode_max_daily_requests",
            "gemini_model",
            "gemini_base_url",
            "passcode_hash",
            "last_rollover_ym",
        }
        filtered = {k: v for k, v in updates.items() if k in allowed and v is not None}
        if not filtered:
            return self.get_settings()
        filtered["updated_at"] = utc_now_iso()
        cols = ", ".join(f"{k} = ?" for k in filtered)
        vals = list(filtered.values())
        with self.transaction() as conn:
            conn.execute(f"UPDATE profile_settings SET {cols} WHERE id = 1", vals)
        return self.get_settings()

    # --- Accounts ---

    def list_accounts(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM accounts ORDER BY id ASC").fetchall()
            return [dict(r) for r in rows]

    def get_account(self, account_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
            return dict(row) if row else None

    def get_default_account_id(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM accounts WHERE account_type = 'checking' ORDER BY id ASC LIMIT 1"
            ).fetchone()
            if row:
                return int(row["id"])
            row = self._conn.execute("SELECT id FROM accounts ORDER BY id ASC LIMIT 1").fetchone()
            if row:
                return int(row["id"])
            now = utc_now_iso()
            cur = self._conn.execute(
                "INSERT INTO accounts (name, account_type, balance_cents, institution, is_liquid, created_at, updated_at) "
                "VALUES ('Main Checking', 'checking', 0, 'Local', 1, ?, ?)",
                (now, now),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def create_account(
        self,
        name: str,
        account_type: str,
        balance_cents: int,
        institution: str = "",
        is_liquid: bool = True,
    ) -> Dict[str, Any]:
        now = utc_now_iso()
        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO accounts (name, account_type, balance_cents, institution, is_liquid, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (name, account_type, int(balance_cents), institution, int(is_liquid), now, now),
            )
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", (cur.lastrowid,)).fetchone()
            return dict(row)

    def adjust_account_balance(self, account_id: int, delta_cents: int) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE accounts SET balance_cents = balance_cents + ?, updated_at = ? WHERE id = ?",
                (int(delta_cents), utc_now_iso(), account_id),
            )

    def set_account_balance(self, account_id: int, balance_cents: int) -> Optional[Dict[str, Any]]:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE accounts SET balance_cents = ?, updated_at = ? WHERE id = ?",
                (int(balance_cents), utc_now_iso(), account_id),
            )
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
            return dict(row) if row else None

    # --- Jars ---

    def list_jars(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM jars ORDER BY priority ASC, id ASC").fetchall()
            return [dict(r) for r in rows]

    def get_jar(self, jar_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jars WHERE id = ?", (jar_id,)).fetchone()
            return dict(row) if row else None

    def get_jar_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jars WHERE slug = ?", (slug,)).fetchone()
            return dict(row) if row else None

    def update_jar(self, jar_id: int, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        allowed = {
            "name",
            "icon",
            "color",
            "target_pct",
            "monthly_target_cents",
            "auto_allocated_cents",
            "rollover_cents",
            "is_locked",
            "rollover_enabled",
            "rollover_cap_cents",
        }
        filtered = {k: v for k, v in updates.items() if k in allowed}
        if not filtered:
            return self.get_jar(jar_id)
        filtered["updated_at"] = utc_now_iso()
        cols = ", ".join(f"{k} = ?" for k in filtered)
        vals = list(filtered.values()) + [jar_id]
        with self.transaction() as conn:
            conn.execute(f"UPDATE jars SET {cols} WHERE id = ?", vals)
            row = conn.execute("SELECT * FROM jars WHERE id = ?", (jar_id,)).fetchone()
            return dict(row) if row else None

    # --- Merchant Rules ---

    def get_merchant_rules(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM merchant_rules").fetchall()
            return {r["normalized_merchant"]: dict(r) for r in rows}

    def upsert_merchant_rule(self, normalized_merchant: str, jar_id: int, subcategory: str = "") -> None:
        if not normalized_merchant:
            return
        now = utc_now_iso()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO merchant_rules (normalized_merchant, jar_id, subcategory, hit_count, updated_at)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(normalized_merchant) DO UPDATE SET
                    jar_id = excluded.jar_id,
                    subcategory = CASE WHEN excluded.subcategory != '' THEN excluded.subcategory ELSE merchant_rules.subcategory END,
                    hit_count = merchant_rules.hit_count + 1,
                    updated_at = excluded.updated_at
                """,
                (normalized_merchant, jar_id, subcategory, now),
            )

    # --- Transactions ---

    def insert_transaction(
        self,
        *,
        account_id: int,
        jar_id: Optional[int],
        tx_type: str,
        amount_cents: int,
        merchant: str,
        normalized_merchant: str,
        subcategory: str,
        memo: str,
        tx_date: str,
        confidence: float,
        needs_review: bool,
        source: str,
        fingerprint: Optional[str] = None,
        adjust_balance: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """Insert a transaction atomically and update account balance. Returns None if duplicate fingerprint."""
        now = utc_now_iso()
        with self.transaction() as conn:
            if fingerprint:
                existing = conn.execute(
                    "SELECT id FROM transactions WHERE fingerprint = ?", (fingerprint,)
                ).fetchone()
                if existing:
                    return None

            cur = conn.execute(
                """
                INSERT INTO transactions (
                    account_id, jar_id, tx_type, amount_cents, merchant, normalized_merchant,
                    subcategory, memo, tx_date, confidence, needs_review, source, fingerprint, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    jar_id,
                    tx_type,
                    int(amount_cents),
                    merchant,
                    normalized_merchant,
                    subcategory,
                    memo,
                    tx_date,
                    float(confidence),
                    int(needs_review),
                    source,
                    fingerprint,
                    now,
                ),
            )
            if adjust_balance:
                delta = int(amount_cents) if tx_type == "income" else -int(amount_cents)
                if tx_type != "transfer":
                    conn.execute(
                        "UPDATE accounts SET balance_cents = balance_cents + ?, updated_at = ? WHERE id = ?",
                        (delta, now, account_id),
                    )
            row = conn.execute(
                """
                SELECT t.*, COALESCE(j.name, '') AS jar_name, COALESCE(j.icon, '') AS jar_icon
                FROM transactions t
                LEFT JOIN jars j ON t.jar_id = j.id
                WHERE t.id = ?
                """,
                (cur.lastrowid,),
            ).fetchone()
            return dict(row)

    def list_transactions(
        self,
        *,
        limit: int = 200,
        since_date: Optional[str] = None,
        jar_id: Optional[int] = None,
        needs_review_only: bool = False,
        search: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        clauses = ["1=1"]
        params: List[Any] = []
        if since_date:
            clauses.append("t.tx_date >= ?")
            params.append(since_date)
        if jar_id is not None:
            clauses.append("t.jar_id = ?")
            params.append(jar_id)
        if needs_review_only:
            clauses.append("t.needs_review = 1")
        if search:
            clauses.append("(LOWER(t.merchant) LIKE ? OR LOWER(t.memo) LIKE ? OR LOWER(t.subcategory) LIKE ?)")
            q = f"%{search.strip().lower()}%"
            params.extend([q, q, q])
        params.append(int(limit))
        sql = f"""
            SELECT t.*, COALESCE(j.name, '') AS jar_name, COALESCE(j.icon, '') AS jar_icon
            FROM transactions t
            LEFT JOIN jars j ON t.jar_id = j.id
            WHERE {' AND '.join(clauses)}
            ORDER BY t.tx_date DESC, t.id DESC
            LIMIT ?
        """
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def get_transaction(self, tx_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT t.*, COALESCE(j.name, '') AS jar_name, COALESCE(j.icon, '') AS jar_icon
                FROM transactions t
                LEFT JOIN jars j ON t.jar_id = j.id
                WHERE t.id = ?
                """,
                (tx_id,),
            ).fetchone()
            return dict(row) if row else None

    def update_transaction(self, tx_id: int, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        allowed = {
            "jar_id",
            "amount_cents",
            "tx_type",
            "tx_date",
            "merchant",
            "normalized_merchant",
            "subcategory",
            "memo",
            "needs_review",
            "confidence",
        }
        filtered = {k: v for k, v in updates.items() if k in allowed}
        if not filtered:
            return self.get_transaction(tx_id)
        if "amount_cents" in filtered:
            filtered["amount_cents"] = int(filtered["amount_cents"])
        with self.transaction() as conn:
            existing = conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()
            if not existing:
                return None

            def _signed(ttype: str, amt: int) -> int:
                if ttype == "income":
                    return amt
                if ttype == "expense":
                    return -amt
                return 0

            old_signed = _signed(str(existing["tx_type"]), int(existing["amount_cents"]))
            new_type = str(filtered.get("tx_type", existing["tx_type"]))
            new_amt = int(filtered.get("amount_cents", existing["amount_cents"]))
            balance_delta = _signed(new_type, new_amt) - old_signed

            cols = ", ".join(f"{k} = ?" for k in filtered)
            vals = list(filtered.values()) + [tx_id]
            conn.execute(f"UPDATE transactions SET {cols} WHERE id = ?", vals)
            if balance_delta != 0:
                conn.execute(
                    "UPDATE accounts SET balance_cents = balance_cents + ?, updated_at = ? WHERE id = ?",
                    (balance_delta, utc_now_iso(), existing["account_id"]),
                )
        return self.get_transaction(tx_id)

    def delete_transaction(self, tx_id: int) -> bool:
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()
            if not row:
                return False
            # Reverse balance impact cleanly
            if row["tx_type"] == "income":
                delta = -int(row["amount_cents"])
            elif row["tx_type"] == "expense":
                delta = int(row["amount_cents"])
            else:
                delta = 0
            if delta:
                conn.execute(
                    "UPDATE accounts SET balance_cents = balance_cents + ?, updated_at = ? WHERE id = ?",
                    (delta, utc_now_iso(), row["account_id"]),
                )
            conn.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
            return True

    # --- Recurring Bills ---

    def list_recurring_bills(self, active_only: bool = True) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM recurring_bills"
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY next_due_date ASC, amount_cents DESC"
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql).fetchall()]

    def upsert_recurring_bill(
        self,
        *,
        name: str,
        normalized_merchant: str,
        amount_cents: int,
        cadence: str,
        next_due_date: str,
        jar_id: Optional[int],
        previous_amount_cents: Optional[int] = None,
        is_subscription: bool = False,
        is_essential: bool = True,
        auto_pay: bool = False,
        confidence: float = 0.9,
        flagged_zombie: bool = False,
    ) -> Dict[str, Any]:
        now = utc_now_iso()
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM recurring_bills WHERE normalized_merchant = ?",
                (normalized_merchant,),
            ).fetchone()
            prev_amt = previous_amount_cents
            if existing:
                old_amt = int(existing["amount_cents"])
                if prev_amt is None:
                    prev_amt = old_amt if old_amt != int(amount_cents) else existing["previous_amount_cents"]
                conn.execute(
                    """
                    UPDATE recurring_bills
                    SET name = ?, amount_cents = ?, previous_amount_cents = ?, cadence = ?,
                        next_due_date = ?, jar_id = COALESCE(?, jar_id),
                        is_subscription = ?, is_essential = ?, auto_pay = ?,
                        active = 1, confidence = ?, flagged_zombie = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        name,
                        int(amount_cents),
                        prev_amt,
                        cadence,
                        next_due_date,
                        jar_id,
                        int(is_subscription),
                        int(is_essential),
                        int(auto_pay),
                        float(confidence),
                        int(flagged_zombie),
                        now,
                        existing["id"],
                    ),
                )
                row = conn.execute("SELECT * FROM recurring_bills WHERE id = ?", (existing["id"],)).fetchone()
            else:
                cur = conn.execute(
                    """
                    INSERT INTO recurring_bills (
                        name, normalized_merchant, amount_cents, previous_amount_cents,
                        cadence, next_due_date, jar_id, is_subscription, is_essential,
                        auto_pay, active, confidence, flagged_zombie, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        name,
                        normalized_merchant,
                        int(amount_cents),
                        prev_amt,
                        cadence,
                        next_due_date,
                        jar_id,
                        int(is_subscription),
                        int(is_essential),
                        int(auto_pay),
                        float(confidence),
                        int(flagged_zombie),
                        now,
                    ),
                )
                row = conn.execute("SELECT * FROM recurring_bills WHERE id = ?", (cur.lastrowid,)).fetchone()
            return dict(row)

    def deactivate_recurring_bill(self, bill_id: int) -> bool:
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE recurring_bills SET active = 0, updated_at = ? WHERE id = ?",
                (utc_now_iso(), bill_id),
            )
            return cur.rowcount > 0

    def flag_zombie_bill(self, bill_id: int, flagged: bool = True) -> Optional[Dict[str, Any]]:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE recurring_bills SET flagged_zombie = ?, updated_at = ? WHERE id = ?",
                (int(flagged), utc_now_iso(), bill_id),
            )
            row = conn.execute("SELECT * FROM recurring_bills WHERE id = ?", (bill_id,)).fetchone()
            return dict(row) if row else None

    # --- Impulse Vault ---

    def create_impulse(
        self,
        *,
        item_name: str,
        price_cents: int,
        reason: str,
        url: str,
        cooling_hours: int,
        created_at: Optional[str] = None,
        unlocks_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        now_dt = datetime.now(timezone.utc).replace(microsecond=0)
        c_at = created_at or now_dt.isoformat().replace("+00:00", "Z")
        u_at = unlocks_at or (now_dt + timedelta(hours=cooling_hours)).isoformat().replace("+00:00", "Z")
        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO impulse_vault (item_name, price_cents, reason, url, cooling_hours, created_at, unlocks_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'cooling')
                """,
                (item_name, int(price_cents), reason, url, int(cooling_hours), c_at, u_at),
            )
            row = conn.execute("SELECT * FROM impulse_vault WHERE id = ?", (cur.lastrowid,)).fetchone()
            return dict(row)

    def list_impulses(self, include_resolved: bool = False) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM impulse_vault"
        if not include_resolved:
            sql += " WHERE status IN ('cooling', 'ready')"
        sql += " ORDER BY unlocks_at ASC, id DESC"
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql).fetchall()]

    def get_impulse(self, impulse_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM impulse_vault WHERE id = ?", (impulse_id,)).fetchone()
            return dict(row) if row else None

    def resolve_impulse(self, impulse_id: int, status: str) -> Optional[Dict[str, Any]]:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE impulse_vault SET status = ?, decision_at = ? WHERE id = ?",
                (status, utc_now_iso(), impulse_id),
            )
            row = conn.execute("SELECT * FROM impulse_vault WHERE id = ?", (impulse_id,)).fetchone()
            return dict(row) if row else None

    # --- Budget Transfers & Audit Log ---

    def record_budget_transfer(
        self, cycle_ym: str, from_jar_id: int, to_jar_id: int, amount_cents: int, reason: str
    ) -> Dict[str, Any]:
        now = utc_now_iso()
        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO budget_transfers (cycle_ym, from_jar_id, to_jar_id, amount_cents, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (cycle_ym, from_jar_id, to_jar_id, int(amount_cents), reason, now),
            )
            row = conn.execute("SELECT * FROM budget_transfers WHERE id = ?", (cur.lastrowid,)).fetchone()
            return dict(row)

    def list_budget_transfers(self, cycle_ym: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM budget_transfers WHERE cycle_ym = ? ORDER BY id ASC",
                (cycle_ym,),
            ).fetchall()
            return [dict(r) for r in rows]

    def log_budget_event(self, event_type: str, summary: str, details: Optional[Dict[str, Any]] = None) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO budget_events (event_type, summary, details_json, created_at) VALUES (?, ?, ?, ?)",
                (event_type, summary, json.dumps(details or {}), utc_now_iso()),
            )

    def list_budget_events(self, limit: int = 25) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM budget_events ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
            out = []
            for r in rows:
                item = dict(r)
                try:
                    item["details"] = json.loads(item.pop("details_json", "{}"))
                except json.JSONDecodeError:
                    item["details"] = {}
                out.append(item)
            return out

    # --- ADHD Progress ---

    def get_adhd_progress(self) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM adhd_progress WHERE id = 1").fetchone()
            data = dict(row) if row else {}
            try:
                data["recent_wins"] = json.loads(data.pop("recent_wins_json", "[]"))
            except json.JSONDecodeError:
                data["recent_wins"] = []
            return data

    def update_adhd_progress(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(updates)
        if "recent_wins" in data:
            data["recent_wins_json"] = json.dumps(data.pop("recent_wins")[:15])
        allowed = {
            "weekly_reset_streak",
            "grace_days_available",
            "last_reset_date",
            "impulses_skipped_count",
            "impulse_money_saved_cents",
            "dopamine_xp",
            "recent_wins_json",
        }
        filtered = {k: v for k, v in data.items() if k in allowed}
        if not filtered:
            return self.get_adhd_progress()
        filtered["updated_at"] = utc_now_iso()
        cols = ", ".join(f"{k} = ?" for k in filtered)
        with self.transaction() as conn:
            conn.execute(f"UPDATE adhd_progress SET {cols} WHERE id = 1", list(filtered.values()))
        return self.get_adhd_progress()

    # --- AI Usage Quota Tracking ---

    def count_ai_requests_today(self) -> int:
        today = date.today().isoformat()
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM ai_usage_log WHERE usage_date = ? AND cached = 0",
                (today,),
            ).fetchone()
            return int(row[0]) if row else 0

    def record_ai_usage(self, provider: str, model: str, cached: bool = False) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO ai_usage_log (usage_date, provider, model, cached, created_at) VALUES (?, ?, ?, ?, ?)",
                (date.today().isoformat(), provider, model, int(cached), utc_now_iso()),
            )

    # --- Credit Lines & Debts ---

    def list_credit_lines(self, active_only: bool = True) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM credit_lines"
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY CASE WHEN kind = 'credit_card' THEN 0 ELSE 1 END, apr_bps DESC, id ASC"
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql).fetchall()]

    def get_credit_line(self, line_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM credit_lines WHERE id = ?", (line_id,)).fetchone()
            return dict(row) if row else None

    def create_credit_line(self, data: Dict[str, Any]) -> Dict[str, Any]:
        now = utc_now_iso()
        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO credit_lines (
                    name, issuer, kind, balance_cents, credit_limit_cents, apr_bps,
                    min_payment_cents, statement_closing_day, payment_due_day,
                    promo_apr_bps, promo_ends_date, opened_date, has_grace_period,
                    autopay_mode, rewards_note, account_id, active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    data["name"],
                    data.get("issuer", ""),
                    data.get("kind", "credit_card"),
                    int(data.get("balance_cents", 0)),
                    int(data.get("credit_limit_cents", 0)),
                    int(data.get("apr_bps", 2499)),
                    int(data.get("min_payment_cents", 0)),
                    int(data.get("statement_closing_day", 15)),
                    int(data.get("payment_due_day", 10)),
                    data.get("promo_apr_bps"),
                    data.get("promo_ends_date"),
                    data.get("opened_date"),
                    int(bool(data.get("has_grace_period", True))),
                    data.get("autopay_mode", "statement"),
                    data.get("rewards_note", ""),
                    data.get("account_id"),
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM credit_lines WHERE id = ?", (cur.lastrowid,)).fetchone()
            return dict(row)

    def update_credit_line(self, line_id: int, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        allowed = {
            "name",
            "issuer",
            "balance_cents",
            "credit_limit_cents",
            "apr_bps",
            "min_payment_cents",
            "statement_closing_day",
            "payment_due_day",
            "promo_apr_bps",
            "promo_ends_date",
            "opened_date",
            "has_grace_period",
            "autopay_mode",
            "rewards_note",
            "active",
        }
        filtered = {k: v for k, v in updates.items() if k in allowed}
        if not filtered:
            return self.get_credit_line(line_id)
        if "has_grace_period" in filtered:
            filtered["has_grace_period"] = int(bool(filtered["has_grace_period"]))
        filtered["updated_at"] = utc_now_iso()
        cols = ", ".join(f"{k} = ?" for k in filtered)
        with self.transaction() as conn:
            conn.execute(f"UPDATE credit_lines SET {cols} WHERE id = ?", list(filtered.values()) + [line_id])
            row = conn.execute("SELECT * FROM credit_lines WHERE id = ?", (line_id,)).fetchone()
            return dict(row) if row else None

    def delete_credit_line(self, line_id: int) -> bool:
        with self.transaction() as conn:
            cur = conn.execute("DELETE FROM credit_lines WHERE id = ?", (line_id,))
            return cur.rowcount > 0

    # --- Savings & Purchase Goals ---

    def list_goals(self, include_completed: bool = False) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM goals"
        if not include_completed:
            sql += " WHERE status != 'completed'"
        sql += " ORDER BY priority ASC, id ASC"
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql).fetchall()]

    def get_goal(self, goal_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
            return dict(row) if row else None

    def create_goal(self, data: Dict[str, Any]) -> Dict[str, Any]:
        now = utc_now_iso()
        saved = int(data.get("saved_cents", 0))
        target = int(data["target_cents"])
        status = "funded" if saved >= target else "active"
        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO goals (
                    name, target_cents, saved_cents, target_date, priority,
                    category, funding_jar_slug, status, notes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data["name"],
                    target,
                    saved,
                    data.get("target_date"),
                    int(data.get("priority", 2)),
                    data.get("category", "gadget"),
                    data.get("funding_jar_slug", "fun"),
                    status,
                    data.get("notes", ""),
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM goals WHERE id = ?", (cur.lastrowid,)).fetchone()
            return dict(row)

    def update_goal(self, goal_id: int, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        allowed = {"name", "target_cents", "saved_cents", "target_date", "priority", "status", "notes"}
        filtered = {k: v for k, v in updates.items() if k in allowed}
        if not filtered:
            return self.get_goal(goal_id)
        filtered["updated_at"] = utc_now_iso()
        cols = ", ".join(f"{k} = ?" for k in filtered)
        with self.transaction() as conn:
            conn.execute(f"UPDATE goals SET {cols} WHERE id = ?", list(filtered.values()) + [goal_id])
            row = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
            return dict(row) if row else None

    def delete_goal(self, goal_id: int) -> bool:
        with self.transaction() as conn:
            cur = conn.execute("DELETE FROM goals WHERE id = ?", (goal_id,))
            return cur.rowcount > 0

    # --- Backup / Restore / Clear ---

    def clear_all_financial_data(self) -> None:
        """Reset financial tables cleanly (e.g., before restoring backup or loading demo data)."""
        with self.transaction() as conn:
            for tbl in (
                "transactions",
                "recurring_bills",
                "impulse_vault",
                "budget_transfers",
                "budget_events",
                "merchant_rules",
                "credit_lines",
                "goals",
                "accounts",
            ):
                conn.execute(f"DELETE FROM {tbl}")
            conn.execute(
                "UPDATE adhd_progress SET weekly_reset_streak=0, grace_days_available=1, "
                "last_reset_date=NULL, impulses_skipped_count=0, impulse_money_saved_cents=0, "
                "dopamine_xp=0, recent_wins_json='[]', updated_at=? WHERE id=1",
                (utc_now_iso(),),
            )
            conn.execute("UPDATE jars SET rollover_cents=0, updated_at=?", (utc_now_iso(),))

    def export_backup(self) -> Dict[str, Any]:
        """Export complete sanitized backup (never includes passcode_hash or API keys)."""
        settings = self.get_settings()
        for secret_field in ("passcode_hash", "opencode_api_key", "opencode_go_api_key", "gemini_api_key"):
            settings.pop(secret_field, None)
        with self._lock:
            return {
                "schema_version": 1,
                "exported_at": utc_now_iso(),
                "settings": settings,
                "accounts": self.list_accounts(),
                "jars": self.list_jars(),
                "transactions": [
                    dict(r) for r in self._conn.execute("SELECT * FROM transactions ORDER BY id ASC").fetchall()
                ],
                "merchant_rules": [
                    dict(r) for r in self._conn.execute("SELECT * FROM merchant_rules").fetchall()
                ],
                "recurring_bills": self.list_recurring_bills(active_only=False),
                "impulse_vault": self.list_impulses(include_resolved=True),
                "budget_transfers": [
                    dict(r) for r in self._conn.execute("SELECT * FROM budget_transfers ORDER BY id ASC").fetchall()
                ],
                "credit_lines": self.list_credit_lines(active_only=False),
                "goals": self.list_goals(include_completed=True),
                "adhd_progress": self.get_adhd_progress(),
            }

    def import_backup(self, payload: Dict[str, Any]) -> None:
        """Restore a full JSON backup atomically."""
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError("Unsupported or invalid ChronoX Finance backup format")
        if not payload.get("jars"):
            raise ValueError("Backup is missing required budget jars")
        if isinstance(payload.get("settings"), dict):
            if payload["settings"].get("opencode_base_url"):
                validate_external_api_url(str(payload["settings"]["opencode_base_url"]))
            if payload["settings"].get("gemini_base_url"):
                validate_external_api_url(str(payload["settings"]["gemini_base_url"]))

        now = utc_now_iso()
        with self.transaction() as conn:
            for tbl in (
                "transactions",
                "recurring_bills",
                "impulse_vault",
                "budget_transfers",
                "budget_events",
                "merchant_rules",
                "credit_lines",
                "goals",
                "accounts",
                "jars",
            ):
                conn.execute(f"DELETE FROM {tbl}")

            for jar in payload.get("jars", []):
                conn.execute(
                    """
                    INSERT INTO jars (
                        id, slug, name, kind, icon, color, priority, target_pct,
                        monthly_target_cents, auto_allocated_cents, rollover_cents,
                        is_locked, rollover_enabled, rollover_cap_cents, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        jar["id"], jar["slug"], jar["name"], jar["kind"], jar["icon"],
                        jar["color"], jar["priority"], jar["target_pct"],
                        jar["monthly_target_cents"], jar["auto_allocated_cents"],
                        jar["rollover_cents"], jar["is_locked"], jar["rollover_enabled"],
                        jar.get("rollover_cap_cents"), jar.get("updated_at", now),
                    ),
                )

            for acct in payload.get("accounts", []):
                conn.execute(
                    """
                    INSERT INTO accounts (id, name, account_type, balance_cents, institution, is_liquid, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        acct["id"], acct["name"], acct["account_type"], acct["balance_cents"],
                        acct.get("institution", ""), acct.get("is_liquid", 1),
                        acct.get("created_at", now), acct.get("updated_at", now),
                    ),
                )

            for rule in payload.get("merchant_rules", []):
                conn.execute(
                    """
                    INSERT INTO merchant_rules (normalized_merchant, jar_id, subcategory, hit_count, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        rule["normalized_merchant"], rule["jar_id"], rule.get("subcategory", ""),
                        rule.get("hit_count", 1), rule.get("updated_at", now),
                    ),
                )

            for tx in payload.get("transactions", []):
                conn.execute(
                    """
                    INSERT INTO transactions (
                        id, account_id, jar_id, tx_type, amount_cents, merchant, normalized_merchant,
                        subcategory, memo, tx_date, confidence, needs_review, source, fingerprint, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tx["id"], tx["account_id"], tx.get("jar_id"), tx["tx_type"], tx["amount_cents"],
                        tx["merchant"], tx["normalized_merchant"], tx.get("subcategory", ""),
                        tx.get("memo", ""), tx["tx_date"], tx.get("confidence", 1.0),
                        tx.get("needs_review", 0), tx.get("source", "backup"),
                        tx.get("fingerprint"), tx.get("created_at", now),
                    ),
                )

            for bill in payload.get("recurring_bills", []):
                conn.execute(
                    """
                    INSERT INTO recurring_bills (
                        id, name, normalized_merchant, amount_cents, previous_amount_cents,
                        cadence, next_due_date, jar_id, is_subscription, is_essential,
                        auto_pay, active, confidence, flagged_zombie, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        bill["id"], bill["name"], bill["normalized_merchant"], bill["amount_cents"],
                        bill.get("previous_amount_cents"), bill["cadence"], bill["next_due_date"],
                        bill.get("jar_id"), bill.get("is_subscription", 0), bill.get("is_essential", 1),
                        bill.get("auto_pay", 0), bill.get("active", 1), bill.get("confidence", 0.9),
                        bill.get("flagged_zombie", 0), bill.get("updated_at", now),
                    ),
                )

            for imp in payload.get("impulse_vault", []):
                conn.execute(
                    """
                    INSERT INTO impulse_vault (
                        id, item_name, price_cents, reason, url, cooling_hours,
                        created_at, unlocks_at, status, decision_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        imp["id"], imp["item_name"], imp["price_cents"], imp.get("reason", ""),
                        imp.get("url", ""), imp.get("cooling_hours", 48), imp["created_at"],
                        imp["unlocks_at"], imp.get("status", "cooling"), imp.get("decision_at"),
                    ),
                )

            for tr in payload.get("budget_transfers", []):
                conn.execute(
                    """
                    INSERT INTO budget_transfers (id, cycle_ym, from_jar_id, to_jar_id, amount_cents, reason, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tr["id"], tr["cycle_ym"], tr["from_jar_id"], tr["to_jar_id"],
                        tr["amount_cents"], tr.get("reason", ""), tr.get("created_at", now),
                    ),
                )

            for cl in payload.get("credit_lines", []):
                conn.execute(
                    """
                    INSERT INTO credit_lines (
                        id, name, issuer, kind, balance_cents, credit_limit_cents, apr_bps,
                        min_payment_cents, statement_closing_day, payment_due_day,
                        promo_apr_bps, promo_ends_date, opened_date, has_grace_period,
                        autopay_mode, rewards_note, account_id, active, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cl["id"], cl["name"], cl.get("issuer", ""), cl.get("kind", "credit_card"),
                        cl["balance_cents"], cl.get("credit_limit_cents", 0), cl.get("apr_bps", 2499),
                        cl.get("min_payment_cents", 0), cl.get("statement_closing_day", 15),
                        cl.get("payment_due_day", 10), cl.get("promo_apr_bps"), cl.get("promo_ends_date"),
                        cl.get("opened_date"), cl.get("has_grace_period", 1), cl.get("autopay_mode", "statement"),
                        cl.get("rewards_note", ""), cl.get("account_id"), cl.get("active", 1),
                        cl.get("created_at", now), cl.get("updated_at", now),
                    ),
                )

            for g in payload.get("goals", []):
                conn.execute(
                    """
                    INSERT INTO goals (
                        id, name, target_cents, saved_cents, target_date, priority,
                        category, funding_jar_slug, status, notes, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        g["id"], g["name"], g["target_cents"], g.get("saved_cents", 0),
                        g.get("target_date"), g.get("priority", 2), g.get("category", "gadget"),
                        g.get("funding_jar_slug", "fun"), g.get("status", "active"),
                        g.get("notes", ""), g.get("created_at", now), g.get("updated_at", now),
                    ),
                )

            if "settings" in payload and isinstance(payload["settings"], dict):
                s = dict(payload["settings"])
                for secret_field in ("passcode_hash", "opencode_api_key", "opencode_go_api_key", "gemini_api_key"):
                    s.pop(secret_field, None)
                self.update_settings(s)

            if "adhd_progress" in payload and isinstance(payload["adhd_progress"], dict):
                self.update_adhd_progress(payload["adhd_progress"])
