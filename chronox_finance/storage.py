"""
Atomic SQLite storage backend for ChronoX Finance.

Design guarantees:
- All money stored as exact integer cents (INTEGER).
- Investment quantities & unit prices stored as exact integer 10^-8 units (`quantity_e8`, `price_e8`)
  with `ROUND_HALF_EVEN` valuation (`compute_holding_value_cents`).
- WAL mode + foreign keys enforced + thread-safe RLock + explicit `BEGIN [IMMEDIATE]` transactions.
- Ordered idempotent migrations tracked in `schema_version` with automatic point-in-time
  `backups/pre-vN.db` snapshot before migrating existing on-disk databases.
- Active goals read through the `goals_active` SQL VIEW (`WHERE deleted_at IS NULL`) with
  soft-delete/restore undo and hard purge.
- Idempotent transaction deduplication via SHA-256 fingerprint + 730-day indexed queries.
- Strict sanitized JSON backup export & restore (NEVER exports vault salts, wrapped DEKs,
  `secrets.enc`, Plaid/broker/IMAP credentials, or AI keys).
"""

from __future__ import annotations

import calendar
import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from chronox_finance.models import (
    AccountType,
    JarKind,
    compute_holding_value_cents,
    utc_now_iso,
)
from chronox_finance.security import validate_external_api_url
from chronox_finance.storage_protocol import LOCAL_CONTEXT, RequestContext

LATEST_SCHEMA_VERSION = 2

_SECRET_SETTINGS_FIELDS = frozenset(
    {
        "passcode_hash",
        "vault_salt_kek",
        "wrapped_dek_password",
        "wrapped_dek_recovery",
        "opencode_api_key",
        "opencode_go_api_key",
        "gemini_api_key",
        "codex_api_key",
        "wrapper_api_key",
        "plaid_client_id",
        "plaid_secret",
        "plaid_access_token",
        "simplefin_access_url",
        "imap_password",
        "email_app_password",
        "broker_secret",
        "private_key",
        "session_signing_key",
    }
)

_EMOJI_ICON_MIGRATION = {
    "🏠": "home",
    "🛒": "cart",
    "✨": "bolt",
    "🛡️": "shield",
    "🛡": "shield",
    "📈": "trend",
    "💰": "wallet",
}

DEFAULT_JARS: List[Dict[str, Any]] = [
    {
        "slug": "bills",
        "name": "Bills & Essentials",
        "kind": JarKind.BILLS.value,
        "icon": "home",
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
        "icon": "cart",
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
        "name": "Dining & Lifestyle",
        "kind": JarKind.FUN.value,
        "icon": "bolt",
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
        "icon": "shield",
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
        "icon": "trend",
        "color": "#2dd4bf",
        "priority": 5,
        "target_pct": 5.0,
        "monthly_target_cents": 25_000,
        "is_locked": 1,
        "rollover_enabled": 1,
        "rollover_cap_cents": None,
    },
]

SCHEMA_V1_SQL = """
CREATE TABLE IF NOT EXISTS profile_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    currency TEXT NOT NULL DEFAULT 'USD',
    hourly_wage_cents INTEGER NOT NULL DEFAULT 3000,
    emergency_buffer_cents INTEGER NOT NULL DEFAULT 50000,
    default_cooling_hours INTEGER NOT NULL DEFAULT 48,
    payday_cadence_days INTEGER NOT NULL DEFAULT 14,
    next_payday_date TEXT,
    expected_paycheck_cents INTEGER NOT NULL DEFAULT 0,
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
    icon TEXT NOT NULL DEFAULT 'wallet',
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
    apr_bps INTEGER NOT NULL DEFAULT 0,
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

SCHEMA_V2_TABLES_SQL = """
CREATE INDEX IF NOT EXISTS idx_tx_account_date ON transactions(account_id, tx_date DESC);

CREATE TABLE IF NOT EXISTS bank_connections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    item_id TEXT NOT NULL DEFAULT '',
    institution TEXT NOT NULL DEFAULT '',
    env TEXT NOT NULL DEFAULT 'production',
    sync_cursor TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    token_expires_at TEXT,
    history_days_requested INTEGER NOT NULL DEFAULT 730,
    last_synced_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bank_conn_provider ON bank_connections(provider, item_id);

CREATE TABLE IF NOT EXISTS rule_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 10,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS automation_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER REFERENCES rule_groups(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 10,
    strict_and INTEGER NOT NULL DEFAULT 1,
    stop_processing INTEGER NOT NULL DEFAULT 0,
    triggers_json TEXT NOT NULL DEFAULT '[]',
    actions_json TEXT NOT NULL DEFAULT '[]',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rules_priority ON automation_rules(active, priority ASC, id ASC);

CREATE TABLE IF NOT EXISTS transaction_splits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_tx_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    jar_id INTEGER REFERENCES jars(id) ON DELETE SET NULL,
    goal_id INTEGER REFERENCES goals(id) ON DELETE SET NULL,
    amount_cents INTEGER NOT NULL,
    memo TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_splits_parent ON transaction_splits(parent_tx_id);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    color TEXT NOT NULL DEFAULT '#06b6d4',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transaction_tags (
    tx_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (tx_id, tag_id)
);

CREATE TABLE IF NOT EXISTS transaction_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tx_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    author TEXT NOT NULL DEFAULT 'user',
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tx_comments_tx ON transaction_comments(tx_id, created_at DESC);

CREATE TABLE IF NOT EXISTS email_scan_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid_key TEXT NOT NULL UNIQUE,
    sender TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL,
    extracted_vendor TEXT NOT NULL DEFAULT '',
    amount_cents INTEGER,
    due_date TEXT,
    kind TEXT NOT NULL DEFAULT 'bill',
    status TEXT NOT NULL DEFAULT 'pending',
    matched_bill_id INTEGER REFERENCES recurring_bills(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_email_queue_status ON email_scan_queue(status, received_at DESC);

CREATE TABLE IF NOT EXISTS scenarios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    overrides_json TEXT NOT NULL DEFAULT '{}',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS net_worth_snapshots (
    snap_date TEXT PRIMARY KEY,
    liquid_cents INTEGER NOT NULL DEFAULT 0,
    investment_cents INTEGER NOT NULL DEFAULT 0,
    debt_cents INTEGER NOT NULL DEFAULT 0,
    net_worth_cents INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credit_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bureau TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT 'fico8',
    score INTEGER NOT NULL,
    pulled_date TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_credit_reports_date ON credit_reports(pulled_date DESC);

CREATE TABLE IF NOT EXISTS credit_inquiries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lender TEXT NOT NULL,
    bureau TEXT NOT NULL DEFAULT 'experian',
    inquiry_date TEXT NOT NULL,
    counts_until TEXT NOT NULL,
    drops_off TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_credit_inquiries_date ON credit_inquiries(inquiry_date DESC);

CREATE TABLE IF NOT EXISTS credit_derogatories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    lender TEXT NOT NULL DEFAULT '',
    opened_date TEXT NOT NULL,
    drops_off TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS holdings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    connection_id INTEGER REFERENCES bank_connections(id) ON DELETE SET NULL,
    symbol TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    asset_class TEXT NOT NULL DEFAULT 'equity',
    quantity_e8 INTEGER NOT NULL DEFAULT 0,
    cost_basis_cents INTEGER NOT NULL DEFAULT 0,
    price_e8 INTEGER NOT NULL DEFAULT 0,
    market_value_cents INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'manual',
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_holdings_symbol ON holdings(symbol);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    snap_date TEXT PRIMARY KEY,
    total_value_cents INTEGER NOT NULL DEFAULT 0,
    cost_basis_cents INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_threads (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    remote_response_id TEXT NOT NULL DEFAULT '',
    last_state_hash TEXT NOT NULL DEFAULT '',
    last_snapshot_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_action_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'deterministic_local',
    model TEXT NOT NULL DEFAULT '',
    actions_json TEXT NOT NULL DEFAULT '[]',
    undo_snapshot_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'preview',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

SCHEMA_SQL = SCHEMA_V1_SQL


def _add_months_iso(iso_date: str, months: int) -> str:
    """Add exact calendar months to an ISO YYYY-MM-DD date string (clamping day cleanly)."""
    d = date.fromisoformat(iso_date)
    total_m = (d.year * 12 + (d.month - 1)) + int(months)
    year, month_idx = divmod(total_m, 12)
    month = month_idx + 1
    max_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(d.day, max_day)).isoformat()


def compute_derogatory_drop_off(kind: str, opened_date: str) -> str:
    """Compute statutory credit report drop-off date (+10y Ch7 BK, +7y late/CO/collections/Ch13)."""
    months = 120 if kind == "bankruptcy_ch7" else 84
    return _add_months_iso(opened_date, months)


def compute_tx_fingerprint(
    account_id: int,
    tx_date: str,
    tx_type: str,
    amount_cents: int,
    normalized_merchant: str,
    memo: str = "",
    external_id: str = "",
) -> str:
    """Deterministic SHA-256 fingerprint for idempotent CSV/OFX/QIF/JSON/Plaid/SimpleFIN imports."""
    if external_id and external_id.strip():
        raw = f"fitid|{account_id}|{external_id.strip()}"
    else:
        raw = f"{account_id}|{tx_date}|{tx_type}|{amount_cents}|{normalized_merchant}|{memo.strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _ensure_columns(conn: sqlite3.Connection, table: str, cols_to_add: List[Tuple[str, str]]) -> None:
    existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for col_name, col_def in cols_to_add:
        if col_name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}")


def _migrate_v1(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_V1_SQL)
    _ensure_columns(
        conn,
        "profile_settings",
        [
            ("last_rollover_ym", "TEXT NOT NULL DEFAULT ''"),
            ("intelligence_mode", "TEXT NOT NULL DEFAULT 'hybrid'"),
            ("ai_provider", "TEXT NOT NULL DEFAULT 'auto'"),
            ("gemini_model", "TEXT NOT NULL DEFAULT 'gemini-2.5-flash'"),
            ("gemini_base_url", "TEXT NOT NULL DEFAULT 'https://generativelanguage.googleapis.com/v1beta'"),
        ],
    )


def _migrate_v2(conn: sqlite3.Connection) -> None:
    _ensure_columns(
        conn,
        "profile_settings",
        [
            ("onboarding_completed", "INTEGER NOT NULL DEFAULT 0"),
            ("vault_salt_kek", "TEXT NOT NULL DEFAULT ''"),
            ("vault_auto_lock_minutes", "INTEGER NOT NULL DEFAULT 30"),
            ("codex_base_url", "TEXT NOT NULL DEFAULT 'https://api.openai.com/v1'"),
            ("codex_model", "TEXT NOT NULL DEFAULT 'gpt-4.1-mini'"),
            ("wrapper_base_url", "TEXT NOT NULL DEFAULT ''"),
            ("wrapper_model", "TEXT NOT NULL DEFAULT ''"),
            ("wrapper_wire_api", "TEXT NOT NULL DEFAULT 'chat_completions'"),
            ("codex_cli_enabled", "INTEGER NOT NULL DEFAULT 0"),
            ("codex_cli_model", "TEXT NOT NULL DEFAULT ''"),
            ("email_scanner_enabled", "INTEGER NOT NULL DEFAULT 0"),
            ("email_imap_host", "TEXT NOT NULL DEFAULT ''"),
            ("email_imap_port", "INTEGER NOT NULL DEFAULT 993"),
            ("email_username", "TEXT NOT NULL DEFAULT ''"),
            ("email_folder", "TEXT NOT NULL DEFAULT 'INBOX'"),
            ("email_lookback_days", "INTEGER NOT NULL DEFAULT 30"),
        ],
    )
    _ensure_columns(
        conn,
        "recurring_bills",
        [
            ("amount_min_cents", "INTEGER"),
            ("amount_max_cents", "INTEGER"),
            ("due_window_days", "INTEGER NOT NULL DEFAULT 4"),
        ],
    )
    _ensure_columns(
        conn,
        "credit_lines",
        [
            ("is_authorized_user", "INTEGER NOT NULL DEFAULT 0"),
        ],
    )
    _ensure_columns(
        conn,
        "ai_usage_log",
        [
            ("input_tokens", "INTEGER NOT NULL DEFAULT 0"),
            ("output_tokens", "INTEGER NOT NULL DEFAULT 0"),
            ("cached_tokens", "INTEGER NOT NULL DEFAULT 0"),
            ("cost_microdollars", "INTEGER NOT NULL DEFAULT 0"),
            ("saved_microdollars", "INTEGER NOT NULL DEFAULT 0"),
            ("latency_ms", "INTEGER NOT NULL DEFAULT 0"),
            ("thread_id", "TEXT"),
        ],
    )
    _ensure_columns(
        conn,
        "goals",
        [
            ("priority_order", "INTEGER NOT NULL DEFAULT 0"),
            ("deleted_at", "TEXT"),
        ],
    )

    conn.executescript(SCHEMA_V2_TABLES_SQL)

    # Create or refresh goals_active VIEW filtering out soft-deleted goals
    conn.execute("DROP VIEW IF EXISTS goals_active")
    conn.execute("CREATE VIEW goals_active AS SELECT * FROM goals WHERE deleted_at IS NULL")

    # Migrate legacy raw Unicode emojis in jars to monochrome icon keys
    for emoji, key in _EMOJI_ICON_MIGRATION.items():
        conn.execute("UPDATE jars SET icon = ? WHERE icon = ?", (key, emoji))

    _ensure_additive_columns(conn)


def _ensure_additive_columns(conn: sqlite3.Connection) -> None:
    """Idempotently ensure all additive columns & indexes exist on v2 databases."""
    _ensure_columns(
        conn,
        "profile_settings",
        [
            ("income_configured", "INTEGER NOT NULL DEFAULT 0"),
            ("payday_user_configured", "INTEGER NOT NULL DEFAULT 0"),
            ("local_cli_auto_detect", "INTEGER NOT NULL DEFAULT 1"),
            ("safe_invest_cap_cents", "INTEGER"),
            ("claude_cli_model", "TEXT NOT NULL DEFAULT ''"),
            ("opencode_cli_model", "TEXT NOT NULL DEFAULT ''"),
            ("agy_cli_model", "TEXT NOT NULL DEFAULT ''"),
            ("claude_cli_use_wrapper", "INTEGER NOT NULL DEFAULT 0"),
            ("opencode_cli_use_wrapper", "INTEGER NOT NULL DEFAULT 0"),
            ("codex_cli_use_wrapper", "INTEGER NOT NULL DEFAULT 0"),
            ("agy_cli_use_wrapper", "INTEGER NOT NULL DEFAULT 0"),
            ("reserve_open_ewa_advances", "INTEGER NOT NULL DEFAULT 1"),
            ("wrapper_single_session", "INTEGER NOT NULL DEFAULT 1"),
            ("wrapper_session_key", "TEXT NOT NULL DEFAULT 'chronox-finance'"),
            ("ai_cli_sessions_json", "TEXT NOT NULL DEFAULT '{}'"),
        ],
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS onetap_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            horizon_days INTEGER NOT NULL DEFAULT 30,
            provider TEXT NOT NULL DEFAULT 'deterministic_local',
            model TEXT NOT NULL DEFAULT '',
            plan_json TEXT NOT NULL DEFAULT '{}',
            undo_snapshot_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'applied',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    _ensure_columns(
        conn,
        "accounts",
        [
            ("connection_id", "INTEGER"),
            ("external_id", "TEXT NOT NULL DEFAULT ''"),
            ("mask", "TEXT NOT NULL DEFAULT ''"),
            ("currency", "TEXT NOT NULL DEFAULT 'USD'"),
            ("available_cents", "INTEGER"),
            ("credit_limit_cents", "INTEGER"),
        ],
    )
    _ensure_columns(
        conn,
        "transactions",
        [
            ("external_id", "TEXT NOT NULL DEFAULT ''"),
            ("affects_balance", "INTEGER NOT NULL DEFAULT 1"),
            ("transfer_direction", "TEXT NOT NULL DEFAULT ''"),
        ],
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tx_acct_extid ON transactions(account_id, external_id)")
    _ensure_columns(
        conn,
        "bank_connections",
        [
            ("backfill_status", "TEXT NOT NULL DEFAULT 'complete'"),
            ("imported_start_date", "TEXT"),
            ("imported_end_date", "TEXT"),
            ("last_error", "TEXT NOT NULL DEFAULT ''"),
        ],
    )
    _ensure_columns(
        conn,
        "credit_lines",
        [
            ("source_env", "TEXT NOT NULL DEFAULT 'production'"),
            ("external_account_id", "TEXT NOT NULL DEFAULT ''"),
        ],
    )
    _ensure_columns(
        conn,
        "credit_reports",
        [
            ("is_sandbox", "INTEGER NOT NULL DEFAULT 0"),
        ],
    )
    _ensure_columns(
        conn,
        "credit_inquiries",
        [
            ("is_sandbox", "INTEGER NOT NULL DEFAULT 0"),
        ],
    )
    _ensure_columns(
        conn,
        "credit_derogatories",
        [
            ("is_sandbox", "INTEGER NOT NULL DEFAULT 0"),
        ],
    )
    existing_holding_cols = {
        str(r[1]) for r in conn.execute("PRAGMA table_info(holdings)").fetchall()
    }
    had_cb_known = "cost_basis_known" in existing_holding_cols
    _ensure_columns(
        conn,
        "holdings",
        [
            ("external_account_id", "TEXT NOT NULL DEFAULT ''"),
            ("cost_basis_known", "INTEGER NOT NULL DEFAULT 1"),
            ("created_at", "TEXT NOT NULL DEFAULT ''"),
        ],
    )
    if not had_cb_known and existing_holding_cols:
        conn.execute(
            "UPDATE holdings SET cost_basis_known = 0 "
            "WHERE cost_basis_cents <= 0 AND LOWER(COALESCE(asset_class, '')) != 'cash'"
        )


def _tx_balance_effect(tx_type: str, amount_cents: int, transfer_direction: str = "") -> int:
    """Signed balance delta for a transaction when `affects_balance == 1`."""
    amt = int(amount_cents)
    if tx_type == "income":
        return amt
    if tx_type == "expense":
        return -amt
    if tx_type == "transfer":
        direction = (transfer_direction or "").strip().lower()
        if direction == "out":
            return -amt
        if direction == "in":
            return amt
    return 0


MIGRATIONS: List[Tuple[int, str, Callable[[sqlite3.Connection], None]]] = [
    (1, "Base ChronoX Finance ledger schema", _migrate_v1),
    (2, "Industrial Finance overhaul: vault/onboarding, connectors, rules, splits, analytics, credit, portfolio, AI/agent, goals_active view", _migrate_v2),
]


class FinanceStorage:
    """Thread-safe SQLite storage layer implementing `FinanceStorageProtocol`."""

    def __init__(self, db_path: str = ".chronox/finance.sqlite3"):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._tx_depth = 0
        self._closed = False
        if db_path != ":memory:":
            parent = Path(db_path).parent
            parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(parent, 0o700)
            except OSError:
                pass
        # Explicit transaction management (`isolation_level=None`) allows deterministic
        # `BEGIN` and `BEGIN IMMEDIATE` semantics across threads.
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._init_db()
        if db_path != ":memory:":
            try:
                os.chmod(db_path, 0o600)
            except OSError:
                pass

    def _backup_before_migration(self, target_version: int) -> Optional[Path]:
        """Copy live on-disk SQLite DB to `backups/pre-vN.db` before running schema migration."""
        if self.db_path == ":memory:":
            return None
        db_file = Path(self.db_path)
        if not db_file.exists():
            return None
        backups_dir = db_file.parent / "backups"
        backups_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(backups_dir, 0o700)
        except OSError:
            pass
        backup_path = backups_dir / f"pre-v{target_version}.db"
        backup_conn = sqlite3.connect(str(backup_path))
        try:
            self._conn.backup(backup_conn)
        finally:
            backup_conn.close()
        try:
            os.chmod(backup_path, 0o600)
        except OSError:
            pass
        return backup_path

    def _init_db(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON;")
            if self.db_path != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL;")
            self._conn.execute("PRAGMA busy_timeout = 5000;")

            # Check whether this on-disk DB already contained user tables prior to migration
            existing_tables = {
                r[0]
                for r in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            }
            had_existing_data = bool(existing_tables - {"schema_version"})

            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY,
                    description TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
            )
            row = self._conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()
            current_version = int(row[0]) if row else 0

            for version, desc, fn in MIGRATIONS:
                if version <= current_version:
                    continue
                if had_existing_data and self.db_path != ":memory:":
                    self._backup_before_migration(version)
                fn(self._conn)
                self._conn.execute(
                    "INSERT OR REPLACE INTO schema_version (version, description, applied_at) VALUES (?, ?, ?)",
                    (version, desc, utc_now_iso()),
                )
                had_existing_data = True

            _ensure_additive_columns(self._conn)
            self._seed_defaults()

    def _seed_defaults(self) -> None:
        now = utc_now_iso()
        default_payday = (date.today() + timedelta(days=7)).isoformat()
        env_mode = (os.getenv("CHRONOX_INTELLIGENCE_MODE") or "hybrid").strip().lower()
        if env_mode not in ("deterministic_only", "hybrid", "ai_standalone"):
            env_mode = "hybrid"
        env_provider = (os.getenv("CHRONOX_AI_PROVIDER") or "auto").strip().lower()
        if env_provider not in (
            "auto",
            "opencode_go",
            "gemini",
            "openai_codex",
            "chat_wrapper",
            "codex_cli",
            "opencode_cli",
            "claude_cli",
            "agy_cli",
        ):
            env_provider = "auto"
        env_gemini_model = (os.getenv("GEMINI_MODEL") or "gemini-2.5-flash").strip() or "gemini-2.5-flash"
        env_gemini_url = (
            os.getenv("GEMINI_BASE_URL") or "https://generativelanguage.googleapis.com/v1beta"
        ).strip()
        try:
            env_gemini_url = validate_external_api_url(env_gemini_url)
        except ValueError:
            env_gemini_url = "https://generativelanguage.googleapis.com/v1beta"

        with self.transaction() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO profile_settings (
                    id, next_payday_date, expected_paycheck_cents, income_configured, payday_user_configured,
                    intelligence_mode, ai_provider, gemini_model, gemini_base_url, updated_at
                )
                VALUES (1, ?, 0, 0, 0, ?, ?, ?, ?, ?)
                """,
                (default_payday, env_mode, env_provider, env_gemini_model, env_gemini_url, now),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO adhd_progress (id, recent_wins_json, updated_at)
                VALUES (1, '[]', ?)
                """,
                (now,),
            )

            jar_count = conn.execute("SELECT COUNT(*) FROM jars").fetchone()[0]
            if jar_count == 0:
                for jar in DEFAULT_JARS:
                    conn.execute(
                        """
                        INSERT INTO jars (
                            slug, name, kind, icon, color, priority, target_pct,
                            monthly_target_cents, auto_allocated_cents, rollover_cents,
                            is_locked, rollover_enabled, rollover_cap_cents, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?)
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
                            jar["is_locked"],
                            jar["rollover_enabled"],
                            jar["rollover_cap_cents"],
                            now,
                        ),
                    )
            else:
                conn.execute(
                    "UPDATE jars SET name = 'Dining & Lifestyle', updated_at = ? WHERE slug = 'fun' AND name = 'Guilt-Free Fun'",
                    (now,),
                )

            acct_count = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
            if acct_count == 0:
                conn.execute(
                    """
                    INSERT INTO accounts (name, account_type, balance_cents, institution, is_liquid, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 1, ?, ?)
                    """,
                    ("Main Checking", AccountType.CHECKING.value, 0, "Local", now, now),
                )

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """Reentrant atomic transaction context manager (`BEGIN` or `BEGIN IMMEDIATE`)."""
        with self._lock:
            self._tx_depth += 1
            started = False
            if self._tx_depth == 1 and not self._conn.in_transaction:
                self._conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                started = True
            try:
                yield self._conn
                if started:
                    self._conn.execute("COMMIT")
            except Exception:
                if started and self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
            finally:
                self._tx_depth -= 1

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._conn.close()

    def ping(self) -> bool:
        """Lightweight DB connectivity check for /api/health."""
        try:
            with self._lock:
                row = self._conn.execute("SELECT 1").fetchone()
                return bool(row and row[0] == 1)
        except sqlite3.Error:
            return False

    def get_schema_version(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()
            return int(row[0]) if row else 0

    # --- Settings ---

    def get_settings(self, ctx: RequestContext = LOCAL_CONTEXT) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM profile_settings WHERE id = 1").fetchone()
            return dict(row) if row else {}

    def update_settings(
        self, updates: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Dict[str, Any]:
        allowed = {
            "currency",
            "hourly_wage_cents",
            "emergency_buffer_cents",
            "default_cooling_hours",
            "payday_cadence_days",
            "next_payday_date",
            "expected_paycheck_cents",
            "income_configured",
            "payday_user_configured",
            "calm_mode",
            "auto_rebalance_enabled",
            "reserve_open_ewa_advances",
            "onboarding_completed",
            "vault_salt_kek",
            "vault_auto_lock_minutes",
            "intelligence_mode",
            "ai_provider",
            "opencode_base_url",
            "opencode_model",
            "opencode_protocol",
            "opencode_max_daily_requests",
            "gemini_model",
            "gemini_base_url",
            "codex_base_url",
            "codex_model",
            "wrapper_base_url",
            "wrapper_model",
            "wrapper_wire_api",
            "wrapper_single_session",
            "wrapper_session_key",
            "ai_cli_sessions_json",
            "codex_cli_enabled",
            "codex_cli_model",
            "claude_cli_model",
            "opencode_cli_model",
            "agy_cli_model",
            "claude_cli_use_wrapper",
            "opencode_cli_use_wrapper",
            "codex_cli_use_wrapper",
            "agy_cli_use_wrapper",
            "local_cli_auto_detect",
            "safe_invest_cap_cents",
            "email_scanner_enabled",
            "email_imap_host",
            "email_imap_port",
            "email_username",
            "email_folder",
            "email_lookback_days",
            "passcode_hash",
            "last_rollover_ym",
        }
        filtered = {k: v for k, v in updates.items() if k in allowed and v is not None}
        if not filtered:
            return self.get_settings(ctx=ctx)
        if "expected_paycheck_cents" in filtered and "income_configured" not in filtered:
            filtered["income_configured"] = int(int(filtered["expected_paycheck_cents"]) > 0)
        for bool_col in (
            "calm_mode",
            "auto_rebalance_enabled",
            "reserve_open_ewa_advances",
            "wrapper_single_session",
            "onboarding_completed",
            "codex_cli_enabled",
            "claude_cli_use_wrapper",
            "opencode_cli_use_wrapper",
            "codex_cli_use_wrapper",
            "agy_cli_use_wrapper",
            "local_cli_auto_detect",
            "email_scanner_enabled",
            "income_configured",
            "payday_user_configured",
        ):
            if bool_col in filtered:
                filtered[bool_col] = int(bool(filtered[bool_col]))
        filtered["updated_at"] = utc_now_iso()
        cols = ", ".join(f"{k} = ?" for k in filtered)
        vals = list(filtered.values())
        with self.transaction() as conn:
            conn.execute(f"UPDATE profile_settings SET {cols} WHERE id = 1", vals)
        return self.get_settings(ctx=ctx)

    # --- Accounts ---

    def list_accounts(self, ctx: RequestContext = LOCAL_CONTEXT) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM accounts ORDER BY id ASC").fetchall()
            return [dict(r) for r in rows]

    def get_account(
        self, account_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
            return dict(row) if row else None

    def get_default_account_id(self, ctx: RequestContext = LOCAL_CONTEXT) -> int:
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
            with self.transaction() as conn:
                cur = conn.execute(
                    "INSERT INTO accounts (name, account_type, balance_cents, institution, is_liquid, created_at, updated_at) "
                    "VALUES ('Main Checking', 'checking', 0, 'Local', 1, ?, ?)",
                    (now, now),
                )
                return int(cur.lastrowid)

    def create_account(
        self,
        name: str,
        account_type: str,
        balance_cents: int,
        institution: str = "",
        is_liquid: bool = True,
        *,
        connection_id: Optional[int] = None,
        external_id: str = "",
        mask: str = "",
        currency: str = "USD",
        available_cents: Optional[int] = None,
        credit_limit_cents: Optional[int] = None,
        ctx: RequestContext = LOCAL_CONTEXT,
    ) -> Dict[str, Any]:
        now = utc_now_iso()
        curr = (currency or "USD").strip().upper()[:12] or "USD"
        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO accounts (
                    name, account_type, balance_cents, institution, is_liquid,
                    connection_id, external_id, mask, currency, available_cents, credit_limit_cents,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    account_type,
                    int(balance_cents),
                    institution,
                    int(is_liquid),
                    connection_id,
                    (external_id or "").strip(),
                    (mask or "").strip(),
                    curr,
                    int(available_cents) if available_cents is not None else None,
                    int(credit_limit_cents) if credit_limit_cents is not None else None,
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", (cur.lastrowid,)).fetchone()
            return dict(row)

    def adjust_account_balance(
        self, account_id: int, delta_cents: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE accounts SET balance_cents = balance_cents + ?, updated_at = ? WHERE id = ?",
                (int(delta_cents), utc_now_iso(), account_id),
            )

    def set_account_balance(
        self,
        account_id: int,
        balance_cents: int,
        *,
        available_cents: Optional[int] = None,
        credit_limit_cents: Optional[int] = None,
        currency: Optional[str] = None,
        mask: Optional[str] = None,
        connection_id: Optional[int] = None,
        external_id: Optional[str] = None,
        mark_history_reconciled: bool = True,
        ctx: RequestContext = LOCAL_CONTEXT,
    ) -> Optional[Dict[str, Any]]:
        now = utc_now_iso()
        with self.transaction() as conn:
            sets = ["balance_cents = ?", "updated_at = ?"]
            vals: List[Any] = [int(balance_cents), now]
            if available_cents is not None:
                sets.append("available_cents = ?")
                vals.append(int(available_cents))
            if credit_limit_cents is not None:
                sets.append("credit_limit_cents = ?")
                vals.append(int(credit_limit_cents))
            if currency:
                sets.append("currency = ?")
                vals.append(currency.strip().upper()[:12])
            if mask is not None and mask != "":
                sets.append("mask = ?")
                vals.append(mask.strip()[:16])
            if connection_id is not None:
                sets.append("connection_id = ?")
                vals.append(int(connection_id))
            if external_id is not None and external_id != "":
                sets.append("external_id = ?")
                vals.append(external_id.strip())
            vals.append(account_id)
            conn.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE id = ?", vals)
            if mark_history_reconciled:
                conn.execute(
                    "UPDATE transactions SET affects_balance = 0 WHERE account_id = ?",
                    (account_id,),
                )
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
            return dict(row) if row else None

    def delete_account(
        self,
        account_id: int,
        *,
        force: bool = False,
        ctx: RequestContext = LOCAL_CONTEXT,
    ) -> Dict[str, Any]:
        """Safely remove an unused/unlinked account (blocks connected bank accounts and last account)."""
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
            if not row:
                return {"deleted": False, "reason": "not_found"}
            if row["connection_id"] is not None:
                raise ValueError(
                    f"Account '{row['name']}' is synced to a live bank connection. "
                    "Disconnect the bank link in Settings before removing it."
                )
            total_accts = int(conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0])
            if total_accts <= 1:
                raise ValueError("Cannot remove your last remaining account.")
            tx_count = int(
                conn.execute("SELECT COUNT(*) FROM transactions WHERE account_id = ?", (account_id,)).fetchone()[0]
            )
            if tx_count > 0 and not force:
                raise ValueError(
                    f"Account '{row['name']}' has {tx_count} transaction(s). "
                    "Reassign or pass force=true to permanently remove it with its transactions."
                )
            conn.execute("UPDATE credit_lines SET account_id = NULL WHERE account_id = ?", (account_id,))
            conn.execute("UPDATE holdings SET account_id = NULL WHERE account_id = ?", (account_id,))
            conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
            return {
                "deleted": True,
                "id": int(account_id),
                "name": str(row["name"]),
                "removed_transactions": tx_count,
            }

    # --- Jars ---

    def list_jars(self, ctx: RequestContext = LOCAL_CONTEXT) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM jars ORDER BY priority ASC, id ASC").fetchall()
            return [dict(r) for r in rows]

    def get_jar(self, jar_id: int, ctx: RequestContext = LOCAL_CONTEXT) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jars WHERE id = ?", (jar_id,)).fetchone()
            return dict(row) if row else None

    def get_jar_by_slug(
        self, slug: str, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jars WHERE slug = ?", (slug,)).fetchone()
            return dict(row) if row else None

    def update_jar(
        self, jar_id: int, updates: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]:
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
            return self.get_jar(jar_id, ctx=ctx)
        filtered["updated_at"] = utc_now_iso()
        cols = ", ".join(f"{k} = ?" for k in filtered)
        vals = list(filtered.values()) + [jar_id]
        with self.transaction() as conn:
            conn.execute(f"UPDATE jars SET {cols} WHERE id = ?", vals)
            row = conn.execute("SELECT * FROM jars WHERE id = ?", (jar_id,)).fetchone()
            return dict(row) if row else None

    # --- Merchant Rules ---

    def get_merchant_rules(self, ctx: RequestContext = LOCAL_CONTEXT) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM merchant_rules").fetchall()
            return {r["normalized_merchant"]: dict(r) for r in rows}

    def upsert_merchant_rule(
        self,
        normalized_merchant: str,
        jar_id: int,
        subcategory: str = "",
        ctx: RequestContext = LOCAL_CONTEXT,
    ) -> None:
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

    # --- Firefly III-Inspired Rule Groups & Automation Rules ---

    def list_rule_groups(self, ctx: RequestContext = LOCAL_CONTEXT) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM rule_groups ORDER BY priority ASC, id ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def create_rule_group(
        self,
        name: str,
        priority: int = 10,
        active: bool = True,
        ctx: RequestContext = LOCAL_CONTEXT,
    ) -> Dict[str, Any]:
        now = utc_now_iso()
        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO rule_groups (name, priority, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (name, int(priority), int(bool(active)), now, now),
            )
            row = conn.execute("SELECT * FROM rule_groups WHERE id = ?", (cur.lastrowid,)).fetchone()
            return dict(row)

    @staticmethod
    def _decode_rule_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        try:
            item["triggers"] = json.loads(item.pop("triggers_json", "[]"))
        except json.JSONDecodeError:
            item["triggers"] = []
        try:
            item["actions"] = json.loads(item.pop("actions_json", "[]"))
        except json.JSONDecodeError:
            item["actions"] = []
        item["strict_and"] = bool(item.get("strict_and", 1))
        item["stop_processing"] = bool(item.get("stop_processing", 0))
        item["active"] = bool(item.get("active", 1))
        return item

    def list_rules(
        self, active_only: bool = False, ctx: RequestContext = LOCAL_CONTEXT
    ) -> List[Dict[str, Any]]:
        sql = """
            SELECT r.*
            FROM automation_rules r
            LEFT JOIN rule_groups g ON r.group_id = g.id
        """
        if active_only:
            sql += " WHERE r.active = 1 AND (g.id IS NULL OR g.active = 1)"
        sql += " ORDER BY COALESCE(g.priority, 1000) ASC, r.priority ASC, r.id ASC"
        with self._lock:
            return [self._decode_rule_row(r) for r in self._conn.execute(sql).fetchall()]

    def get_rule(
        self, rule_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM automation_rules WHERE id = ?", (rule_id,)
            ).fetchone()
            return self._decode_rule_row(row) if row else None

    def create_rule(
        self, data: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Dict[str, Any]:
        now = utc_now_iso()
        triggers_json = json.dumps(data.get("triggers", []))
        actions_json = json.dumps(data.get("actions", []))
        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO automation_rules (
                    group_id, name, priority, strict_and, stop_processing,
                    triggers_json, actions_json, active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data.get("group_id"),
                    data["name"],
                    int(data.get("priority", 10)),
                    int(bool(data.get("strict_and", True))),
                    int(bool(data.get("stop_processing", False))),
                    triggers_json,
                    actions_json,
                    int(bool(data.get("active", True))),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM automation_rules WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return self._decode_rule_row(row)

    def update_rule(
        self, rule_id: int, updates: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]:
        data = dict(updates)
        if "triggers" in data and data["triggers"] is not None:
            data["triggers_json"] = json.dumps(data.pop("triggers"))
        if "actions" in data and data["actions"] is not None:
            data["actions_json"] = json.dumps(data.pop("actions"))
        allowed = {
            "group_id",
            "name",
            "priority",
            "strict_and",
            "stop_processing",
            "triggers_json",
            "actions_json",
            "active",
        }
        filtered = {k: v for k, v in data.items() if k in allowed}
        if not filtered:
            return self.get_rule(rule_id, ctx=ctx)
        for bcol in ("strict_and", "stop_processing", "active"):
            if bcol in filtered:
                filtered[bcol] = int(bool(filtered[bcol]))
        filtered["updated_at"] = utc_now_iso()
        cols = ", ".join(f"{k} = ?" for k in filtered)
        with self.transaction() as conn:
            conn.execute(
                f"UPDATE automation_rules SET {cols} WHERE id = ?",
                list(filtered.values()) + [rule_id],
            )
            row = conn.execute(
                "SELECT * FROM automation_rules WHERE id = ?", (rule_id,)
            ).fetchone()
            return self._decode_rule_row(row) if row else None

    def delete_rule(self, rule_id: int, ctx: RequestContext = LOCAL_CONTEXT) -> bool:
        with self.transaction() as conn:
            cur = conn.execute("DELETE FROM automation_rules WHERE id = ?", (rule_id,))
            return cur.rowcount > 0

    # --- Transactions, Splits, Tags & Comments ---

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
        external_id: str = "",
        transfer_direction: str = "",
        adjust_balance: bool = True,
        ctx: RequestContext = LOCAL_CONTEXT,
    ) -> Optional[Dict[str, Any]]:
        """Insert a transaction atomically and update account balance. Returns None if duplicate fingerprint."""
        now = utc_now_iso()
        ext_id = (external_id or "").strip()
        xfer_dir = (transfer_direction or "").strip().lower()
        affects_bal = int(bool(adjust_balance))
        with self.transaction() as conn:
            if fingerprint:
                existing = conn.execute(
                    "SELECT id FROM transactions WHERE fingerprint = ?", (fingerprint,)
                ).fetchone()
                if existing:
                    return None
            if ext_id:
                existing_ext = conn.execute(
                    "SELECT id FROM transactions WHERE account_id = ? AND external_id = ? LIMIT 1",
                    (account_id, ext_id),
                ).fetchone()
                if existing_ext:
                    return None

            cur = conn.execute(
                """
                INSERT INTO transactions (
                    account_id, jar_id, tx_type, amount_cents, merchant, normalized_merchant,
                    subcategory, memo, tx_date, confidence, needs_review, source, fingerprint,
                    external_id, affects_balance, transfer_direction, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    ext_id,
                    affects_bal,
                    xfer_dir,
                    now,
                ),
            )
            if adjust_balance:
                delta = _tx_balance_effect(tx_type, int(amount_cents), xfer_dir)
                if delta != 0:
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

    def find_provider_transaction(
        self,
        *,
        external_id: str,
        account_id: Optional[int] = None,
        ctx: RequestContext = LOCAL_CONTEXT,
    ) -> Optional[Dict[str, Any]]:
        ext_id = (external_id or "").strip()
        if not ext_id:
            return None
        with self._lock:
            if account_id is not None:
                fp = compute_tx_fingerprint(account_id, "", "", 0, "", "", ext_id)
                row = self._conn.execute(
                    "SELECT * FROM transactions WHERE (account_id = ? AND external_id = ?) OR fingerprint = ? LIMIT 1",
                    (int(account_id), ext_id, fp),
                ).fetchone()
                if row:
                    return dict(row)
            row = self._conn.execute(
                "SELECT * FROM transactions WHERE external_id = ? LIMIT 1",
                (ext_id,),
            ).fetchone()
            return dict(row) if row else None

    def delete_provider_transactions(
        self,
        *,
        external_ids: Sequence[str],
        account_ids: Optional[Sequence[int]] = None,
        ctx: RequestContext = LOCAL_CONTEXT,
    ) -> int:
        clean_ids = [str(eid).strip() for eid in external_ids if str(eid).strip()]
        if not clean_ids:
            return 0
        deleted = 0
        with self.transaction() as conn:
            for eid in clean_ids:
                if account_ids:
                    fps = [compute_tx_fingerprint(int(aid), "", "", 0, "", "", eid) for aid in account_ids]
                    placeholders_aid = ",".join("?" for _ in account_ids)
                    placeholders_fp = ",".join("?" for _ in fps)
                    rows = conn.execute(
                        f"SELECT id, account_id, tx_type, amount_cents, affects_balance, transfer_direction "
                        f"FROM transactions WHERE (account_id IN ({placeholders_aid}) AND external_id = ?) "
                        f"OR fingerprint IN ({placeholders_fp})",
                        [*[int(a) for a in account_ids], eid, *fps],
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT id, account_id, tx_type, amount_cents, affects_balance, transfer_direction "
                        "FROM transactions WHERE external_id = ?",
                        (eid,),
                    ).fetchall()
                for row in rows:
                    if int(row["affects_balance"] or 0) == 1:
                        delta = -_tx_balance_effect(
                            str(row["tx_type"]),
                            int(row["amount_cents"]),
                            str(row["transfer_direction"] or ""),
                        )
                        if delta != 0:
                            conn.execute(
                                "UPDATE accounts SET balance_cents = balance_cents + ?, updated_at = ? WHERE id = ?",
                                (delta, utc_now_iso(), row["account_id"]),
                            )
                    conn.execute("DELETE FROM transactions WHERE id = ?", (row["id"],))
                    deleted += 1
        return deleted

    def count_transactions(
        self,
        *,
        since_date: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        account_id: Optional[int] = None,
        jar_id: Optional[int] = None,
        needs_review_only: bool = False,
        search: Optional[str] = None,
        ctx: RequestContext = LOCAL_CONTEXT,
    ) -> int:
        effective_start = start_date or since_date
        clauses = ["1=1"]
        params: List[Any] = []
        if effective_start:
            clauses.append("t.tx_date >= ?")
            params.append(effective_start)
        if end_date:
            clauses.append("t.tx_date <= ?")
            params.append(end_date)
        if account_id is not None:
            clauses.append("t.account_id = ?")
            params.append(int(account_id))
        if jar_id is not None:
            clauses.append("t.jar_id = ?")
            params.append(int(jar_id))
        if needs_review_only:
            clauses.append("t.needs_review = 1")
        if search:
            clauses.append(
                "(LOWER(t.merchant) LIKE ? OR LOWER(t.memo) LIKE ? OR LOWER(t.subcategory) LIKE ?)"
            )
            q = f"%{search.strip().lower()}%"
            params.extend([q, q, q])
        with self._lock:
            row = self._conn.execute(
                f"SELECT COUNT(*) FROM transactions t WHERE {' AND '.join(clauses)}",
                params,
            ).fetchone()
            return int(row[0]) if row else 0

    def list_transactions(
        self,
        *,
        limit: Optional[int] = 200,
        offset: int = 0,
        since_date: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        account_id: Optional[int] = None,
        jar_id: Optional[int] = None,
        needs_review_only: bool = False,
        search: Optional[str] = None,
        include_splits: bool = False,
        include_tags: bool = False,
        ctx: RequestContext = LOCAL_CONTEXT,
    ) -> List[Dict[str, Any]]:
        """Query transactions with 730-day+ range filtering and optional offset pagination."""
        effective_start = start_date or since_date
        clauses = ["1=1"]
        params: List[Any] = []
        if effective_start:
            clauses.append("t.tx_date >= ?")
            params.append(effective_start)
        if end_date:
            clauses.append("t.tx_date <= ?")
            params.append(end_date)
        if account_id is not None:
            clauses.append("t.account_id = ?")
            params.append(int(account_id))
        if jar_id is not None:
            clauses.append("t.jar_id = ?")
            params.append(int(jar_id))
        if needs_review_only:
            clauses.append("t.needs_review = 1")
        if search:
            clauses.append(
                "(LOWER(t.merchant) LIKE ? OR LOWER(t.memo) LIKE ? OR LOWER(t.subcategory) LIKE ?)"
            )
            q = f"%{search.strip().lower()}%"
            params.extend([q, q, q])

        limit_clause = ""
        if limit is not None and int(limit) > 0:
            bounded_limit = max(1, min(250_000, int(limit)))
            limit_clause = "LIMIT ? OFFSET ?"
            params.extend([bounded_limit, max(0, int(offset))])

        sql = f"""
            SELECT t.*, COALESCE(j.name, '') AS jar_name, COALESCE(j.icon, '') AS jar_icon
            FROM transactions t
            LEFT JOIN jars j ON t.jar_id = j.id
            WHERE {' AND '.join(clauses)}
            ORDER BY t.tx_date DESC, t.id DESC
            {limit_clause}
        """
        with self._lock:
            rows = [dict(r) for r in self._conn.execute(sql, params).fetchall()]
            if not rows:
                return rows
            if include_splits:
                splits_map = self.get_splits_for_transactions([int(r["id"]) for r in rows], ctx=ctx)
                for r in rows:
                    r["splits"] = splits_map.get(int(r["id"]), [])
            if include_tags:
                tags_map = self.get_tags_for_transactions([int(r["id"]) for r in rows], ctx=ctx)
                for r in rows:
                    r["tags"] = tags_map.get(int(r["id"]), [])
            return rows

    def get_transaction(
        self, tx_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]:
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

    def update_transaction(
        self, tx_id: int, updates: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]:
        allowed = {
            "account_id",
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
            "fingerprint",
            "transfer_direction",
        }
        filtered = {k: v for k, v in updates.items() if k in allowed}
        if not filtered:
            return self.get_transaction(tx_id, ctx=ctx)
        if "amount_cents" in filtered:
            filtered["amount_cents"] = int(filtered["amount_cents"])
        if "account_id" in filtered and filtered["account_id"] is not None:
            filtered["account_id"] = int(filtered["account_id"])
        with self.transaction() as conn:
            existing = conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()
            if not existing:
                return None

            affects_bal = int(existing["affects_balance"] if "affects_balance" in existing.keys() else 1)
            old_acct_id = int(existing["account_id"])
            new_acct_id = int(filtered.get("account_id") or old_acct_id)
            old_dir = str(existing["transfer_direction"] or "") if "transfer_direction" in existing.keys() else ""
            new_dir = str(filtered.get("transfer_direction", old_dir) or "")
            old_signed = _tx_balance_effect(str(existing["tx_type"]), int(existing["amount_cents"]), old_dir)
            new_type = str(filtered.get("tx_type", existing["tx_type"]))
            new_amt = int(filtered.get("amount_cents", existing["amount_cents"]))
            new_signed = _tx_balance_effect(new_type, new_amt, new_dir)

            cols = ", ".join(f"{k} = ?" for k in filtered)
            vals = list(filtered.values()) + [tx_id]
            conn.execute(f"UPDATE transactions SET {cols} WHERE id = ?", vals)
            if affects_bal == 1:
                now = utc_now_iso()
                if new_acct_id != old_acct_id:
                    if old_signed != 0:
                        conn.execute(
                            "UPDATE accounts SET balance_cents = balance_cents + ?, updated_at = ? WHERE id = ?",
                            (-old_signed, now, old_acct_id),
                        )
                    if new_signed != 0:
                        conn.execute(
                            "UPDATE accounts SET balance_cents = balance_cents + ?, updated_at = ? WHERE id = ?",
                            (new_signed, now, new_acct_id),
                        )
                else:
                    balance_delta = new_signed - old_signed
                    if balance_delta != 0:
                        conn.execute(
                            "UPDATE accounts SET balance_cents = balance_cents + ?, updated_at = ? WHERE id = ?",
                            (balance_delta, now, old_acct_id),
                        )
        return self.get_transaction(tx_id, ctx=ctx)

    def delete_transaction(self, tx_id: int, ctx: RequestContext = LOCAL_CONTEXT) -> bool:
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()
            if not row:
                return False
            affects_bal = int(row["affects_balance"] if "affects_balance" in row.keys() else 1)
            if affects_bal == 1:
                old_dir = str(row["transfer_direction"] or "") if "transfer_direction" in row.keys() else ""
                delta = -_tx_balance_effect(str(row["tx_type"]), int(row["amount_cents"]), old_dir)
                if delta:
                    conn.execute(
                        "UPDATE accounts SET balance_cents = balance_cents + ?, updated_at = ? WHERE id = ?",
                        (delta, utc_now_iso(), row["account_id"]),
                    )
            conn.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
            return True

    def set_transaction_splits(
        self,
        parent_tx_id: int,
        splits: List[Dict[str, Any]],
        ctx: RequestContext = LOCAL_CONTEXT,
    ) -> List[Dict[str, Any]]:
        """
        Atomically replace child splits for `parent_tx_id`, strictly enforcing
        the exact split-sum invariant (`SUM(splits.amount_cents) == parent.amount_cents`).
        """
        if not splits:
            raise ValueError("Splits list must be non-empty")
        total = 0
        for item in splits:
            amt = int(item.get("amount_cents", 0))
            if amt <= 0:
                raise ValueError("Each split amount_cents must be strictly positive")
            total += amt

        now = utc_now_iso()
        with self.transaction(immediate=True) as conn:
            parent = conn.execute(
                "SELECT id, amount_cents FROM transactions WHERE id = ?", (parent_tx_id,)
            ).fetchone()
            if not parent:
                raise ValueError(f"Transaction {parent_tx_id} not found")
            parent_amt = int(parent["amount_cents"])
            if total != parent_amt:
                raise ValueError(
                    f"Split sum ({total} cents) must exactly equal parent transaction amount ({parent_amt} cents)"
                )

            conn.execute("DELETE FROM transaction_splits WHERE parent_tx_id = ?", (parent_tx_id,))
            for item in splits:
                conn.execute(
                    """
                    INSERT INTO transaction_splits (parent_tx_id, jar_id, goal_id, amount_cents, memo, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        parent_tx_id,
                        item.get("jar_id"),
                        item.get("goal_id"),
                        int(item["amount_cents"]),
                        str(item.get("memo", "")),
                        now,
                    ),
                )
        return self.get_transaction_splits(parent_tx_id, ctx=ctx)

    def clear_transaction_splits(
        self, parent_tx_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM transaction_splits WHERE parent_tx_id = ?", (parent_tx_id,))

    def get_transaction_splits(
        self, parent_tx_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT s.*, COALESCE(j.name, '') AS jar_name
                FROM transaction_splits s
                LEFT JOIN jars j ON s.jar_id = j.id
                WHERE s.parent_tx_id = ?
                ORDER BY s.id ASC
                """,
                (parent_tx_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_splits_for_transactions(
        self, tx_ids: List[int], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Dict[int, List[Dict[str, Any]]]:
        if not tx_ids:
            return {}
        out: Dict[int, List[Dict[str, Any]]] = {}
        with self._lock:
            # Chunk IN-clauses cleanly for up to 5000 transactions (SQLite variable limit safety)
            for i in range(0, len(tx_ids), 500):
                chunk = [int(x) for x in tx_ids[i : i + 500]]
                placeholders = ",".join("?" for _ in chunk)
                rows = self._conn.execute(
                    f"""
                    SELECT s.*, COALESCE(j.name, '') AS jar_name
                    FROM transaction_splits s
                    LEFT JOIN jars j ON s.jar_id = j.id
                    WHERE s.parent_tx_id IN ({placeholders})
                    ORDER BY s.parent_tx_id ASC, s.id ASC
                    """,
                    chunk,
                ).fetchall()
                for r in rows:
                    d = dict(r)
                    out.setdefault(int(d["parent_tx_id"]), []).append(d)
        return out

    def list_tags(self, ctx: RequestContext = LOCAL_CONTEXT) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM tags ORDER BY LOWER(name) ASC").fetchall()
            return [dict(r) for r in rows]

    def set_transaction_tags(
        self, tx_id: int, tag_names: List[str], ctx: RequestContext = LOCAL_CONTEXT
    ) -> List[Dict[str, Any]]:
        cleaned: List[str] = []
        seen = set()
        for raw in tag_names:
            name = (raw or "").strip()[:40]
            if name and name.lower() not in seen:
                seen.add(name.lower())
                cleaned.append(name)

        now = utc_now_iso()
        with self.transaction() as conn:
            tx = conn.execute("SELECT id FROM transactions WHERE id = ?", (tx_id,)).fetchone()
            if not tx:
                raise ValueError(f"Transaction {tx_id} not found")
            conn.execute("DELETE FROM transaction_tags WHERE tx_id = ?", (tx_id,))
            for name in cleaned:
                row = conn.execute(
                    "SELECT id FROM tags WHERE LOWER(name) = LOWER(?)", (name,)
                ).fetchone()
                if row:
                    tag_id = int(row["id"])
                else:
                    cur = conn.execute(
                        "INSERT INTO tags (name, color, created_at) VALUES (?, '#06b6d4', ?)",
                        (name, now),
                    )
                    tag_id = int(cur.lastrowid)
                conn.execute(
                    "INSERT OR IGNORE INTO transaction_tags (tx_id, tag_id) VALUES (?, ?)",
                    (tx_id, tag_id),
                )
        return self.get_transaction_tags(tx_id, ctx=ctx)

    def get_transaction_tags(
        self, tx_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT tg.id, tg.name, tg.color
                FROM transaction_tags tt
                JOIN tags tg ON tt.tag_id = tg.id
                WHERE tt.tx_id = ?
                ORDER BY LOWER(tg.name) ASC
                """,
                (tx_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_tags_for_transactions(
        self, tx_ids: List[int], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Dict[int, List[Dict[str, Any]]]:
        if not tx_ids:
            return {}
        out: Dict[int, List[Dict[str, Any]]] = {}
        with self._lock:
            for i in range(0, len(tx_ids), 500):
                chunk = [int(x) for x in tx_ids[i : i + 500]]
                placeholders = ",".join("?" for _ in chunk)
                rows = self._conn.execute(
                    f"""
                    SELECT tt.tx_id, tg.id, tg.name, tg.color
                    FROM transaction_tags tt
                    JOIN tags tg ON tt.tag_id = tg.id
                    WHERE tt.tx_id IN ({placeholders})
                    ORDER BY tt.tx_id ASC, LOWER(tg.name) ASC
                    """,
                    chunk,
                ).fetchall()
                for r in rows:
                    out.setdefault(int(r["tx_id"]), []).append(
                        {"id": int(r["id"]), "name": r["name"], "color": r["color"]}
                    )
        return out

    def list_transaction_comments(
        self, tx_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM transaction_comments WHERE tx_id = ? ORDER BY created_at ASC, id ASC",
                (tx_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def add_transaction_comment(
        self,
        tx_id: int,
        body: str,
        author: str = "user",
        ctx: RequestContext = LOCAL_CONTEXT,
    ) -> Dict[str, Any]:
        if author not in ("user", "ai_coach"):
            author = "user"
        now = utc_now_iso()
        with self.transaction() as conn:
            tx = conn.execute("SELECT id FROM transactions WHERE id = ?", (tx_id,)).fetchone()
            if not tx:
                raise ValueError(f"Transaction {tx_id} not found")
            cur = conn.execute(
                """
                INSERT INTO transaction_comments (tx_id, author, body, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (tx_id, author, body.strip(), now),
            )
            row = conn.execute(
                "SELECT * FROM transaction_comments WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return dict(row)

    # --- Recurring Bills ---

    def list_recurring_bills(
        self, active_only: bool = True, ctx: RequestContext = LOCAL_CONTEXT
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM recurring_bills"
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY next_due_date ASC, amount_cents DESC"
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql).fetchall()]

    def get_recurring_bill(
        self, bill_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM recurring_bills WHERE id = ?", (int(bill_id),)
            ).fetchone()
            return dict(row) if row else None

    def update_recurring_bill(
        self,
        bill_id: int,
        updates: Dict[str, Any],
        ctx: RequestContext = LOCAL_CONTEXT,
    ) -> Optional[Dict[str, Any]]:
        allowed = {
            "name",
            "normalized_merchant",
            "amount_cents",
            "previous_amount_cents",
            "amount_min_cents",
            "amount_max_cents",
            "due_window_days",
            "cadence",
            "next_due_date",
            "jar_id",
            "is_subscription",
            "is_essential",
            "auto_pay",
            "active",
            "confidence",
            "flagged_zombie",
        }
        bool_cols = {"is_subscription", "is_essential", "auto_pay", "active", "flagged_zombie"}
        int_cols = {"amount_cents", "previous_amount_cents", "amount_min_cents", "amount_max_cents", "due_window_days", "jar_id"}
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM recurring_bills WHERE id = ?", (int(bill_id),)
            ).fetchone()
            if not existing:
                return None
            clean: Dict[str, Any] = {}
            for k, v in updates.items():
                if k not in allowed:
                    continue
                if k in bool_cols and v is not None:
                    clean[k] = int(bool(v))
                elif k in int_cols:
                    clean[k] = int(v) if v is not None else None
                else:
                    clean[k] = v
            if (
                "amount_cents" in clean
                and clean["amount_cents"] is not None
                and "previous_amount_cents" not in clean
                and int(clean["amount_cents"]) != int(existing["amount_cents"])
            ):
                clean["previous_amount_cents"] = int(existing["amount_cents"])
            if not clean:
                return dict(existing)
            clean["updated_at"] = utc_now_iso()
            cols = ", ".join(f"{k} = ?" for k in clean)
            conn.execute(
                f"UPDATE recurring_bills SET {cols} WHERE id = ?",
                [*clean.values(), int(bill_id)],
            )
            row = conn.execute(
                "SELECT * FROM recurring_bills WHERE id = ?", (int(bill_id),)
            ).fetchone()
            return dict(row) if row else None

    def delete_recurring_bill_hard(
        self, bill_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> bool:
        """Permanently delete a bill row (used exclusively for atomic agent Undo of create_bill)."""
        with self.transaction() as conn:
            cur = conn.execute("DELETE FROM recurring_bills WHERE id = ?", (int(bill_id),))
            return cur.rowcount > 0

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
        amount_min_cents: Optional[int] = None,
        amount_max_cents: Optional[int] = None,
        due_window_days: int = 4,
        is_subscription: bool = False,
        is_essential: bool = True,
        auto_pay: bool = False,
        confidence: float = 0.9,
        flagged_zombie: bool = False,
        ctx: RequestContext = LOCAL_CONTEXT,
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
                    SET name = ?, amount_cents = ?, previous_amount_cents = ?,
                        amount_min_cents = COALESCE(?, amount_min_cents),
                        amount_max_cents = COALESCE(?, amount_max_cents),
                        due_window_days = ?, cadence = ?,
                        next_due_date = ?, jar_id = COALESCE(?, jar_id),
                        is_subscription = ?, is_essential = ?, auto_pay = ?,
                        active = 1, confidence = ?, flagged_zombie = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        name,
                        int(amount_cents),
                        prev_amt,
                        int(amount_min_cents) if amount_min_cents is not None else None,
                        int(amount_max_cents) if amount_max_cents is not None else None,
                        int(due_window_days),
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
                row = conn.execute(
                    "SELECT * FROM recurring_bills WHERE id = ?", (existing["id"],)
                ).fetchone()
            else:
                cur = conn.execute(
                    """
                    INSERT INTO recurring_bills (
                        name, normalized_merchant, amount_cents, previous_amount_cents,
                        amount_min_cents, amount_max_cents, due_window_days,
                        cadence, next_due_date, jar_id, is_subscription, is_essential,
                        auto_pay, active, confidence, flagged_zombie, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        name,
                        normalized_merchant,
                        int(amount_cents),
                        prev_amt,
                        int(amount_min_cents) if amount_min_cents is not None else None,
                        int(amount_max_cents) if amount_max_cents is not None else None,
                        int(due_window_days),
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
                row = conn.execute(
                    "SELECT * FROM recurring_bills WHERE id = ?", (cur.lastrowid,)
                ).fetchone()
            return dict(row)

    def deactivate_recurring_bill(
        self, bill_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> bool:
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE recurring_bills SET active = 0, updated_at = ? WHERE id = ?",
                (utc_now_iso(), bill_id),
            )
            return cur.rowcount > 0

    def flag_zombie_bill(
        self, bill_id: int, flagged: bool = True, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE recurring_bills SET flagged_zombie = ?, updated_at = ? WHERE id = ?",
                (int(flagged), utc_now_iso(), bill_id),
            )
            row = conn.execute("SELECT * FROM recurring_bills WHERE id = ?", (bill_id,)).fetchone()
            return dict(row) if row else None

    # --- Bank & Broker Connections (tokens stored strictly in secrets.enc under conn:{id}) ---

    def list_connections(self, ctx: RequestContext = LOCAL_CONTEXT) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM bank_connections ORDER BY id ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_connection(
        self, connection_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM bank_connections WHERE id = ?", (connection_id,)
            ).fetchone()
            return dict(row) if row else None

    def upsert_connection(
        self, data: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Dict[str, Any]:
        now = utc_now_iso()
        conn_id = data.get("id")
        with self.transaction() as conn:
            existing = None
            if conn_id is not None:
                existing = conn.execute(
                    "SELECT * FROM bank_connections WHERE id = ?", (int(conn_id),)
                ).fetchone()
            elif data.get("item_id"):
                existing = conn.execute(
                    "SELECT * FROM bank_connections WHERE provider = ? AND item_id = ?",
                    (data["provider"], str(data["item_id"])),
                ).fetchone()

            if existing:
                conn.execute(
                    """
                    UPDATE bank_connections
                    SET institution = ?, env = ?, sync_cursor = ?, status = ?,
                        token_expires_at = ?, history_days_requested = ?,
                        backfill_status = ?, imported_start_date = ?, imported_end_date = ?,
                        last_error = ?, last_synced_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        data.get("institution", existing["institution"]),
                        data.get("env", existing["env"]),
                        data.get("sync_cursor", existing["sync_cursor"]),
                        data.get("status", existing["status"]),
                        data.get("token_expires_at", existing["token_expires_at"]),
                        int(data.get("history_days_requested", existing["history_days_requested"])),
                        str(data.get("backfill_status", existing["backfill_status"] or "complete")),
                        data.get("imported_start_date", existing["imported_start_date"]),
                        data.get("imported_end_date", existing["imported_end_date"]),
                        str(data.get("last_error", existing["last_error"] or "")),
                        data.get("last_synced_at", existing["last_synced_at"]),
                        now,
                        existing["id"],
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM bank_connections WHERE id = ?", (existing["id"],)
                ).fetchone()
                return dict(row)

            cur = conn.execute(
                """
                INSERT INTO bank_connections (
                    provider, item_id, institution, env, sync_cursor, status,
                    token_expires_at, history_days_requested,
                    backfill_status, imported_start_date, imported_end_date, last_error,
                    last_synced_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data["provider"],
                    str(data.get("item_id", "")),
                    str(data.get("institution", "")),
                    str(data.get("env", "production")),
                    str(data.get("sync_cursor", "")),
                    str(data.get("status", "active")),
                    data.get("token_expires_at"),
                    int(data.get("history_days_requested", 730)),
                    str(data.get("backfill_status", "complete")),
                    data.get("imported_start_date"),
                    data.get("imported_end_date"),
                    str(data.get("last_error", "")),
                    data.get("last_synced_at"),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM bank_connections WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return dict(row)

    def delete_connection(
        self, connection_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> bool:
        with self.transaction() as conn:
            cur = conn.execute("DELETE FROM bank_connections WHERE id = ?", (connection_id,))
            return cur.rowcount > 0

    # --- Email Scan Queue ---

    def list_email_queue(
        self,
        status: Optional[str] = None,
        limit: int = 100,
        ctx: RequestContext = LOCAL_CONTEXT,
    ) -> List[Dict[str, Any]]:
        bounded = max(1, min(500, int(limit)))
        with self._lock:
            if status:
                rows = self._conn.execute(
                    "SELECT * FROM email_scan_queue WHERE status = ? ORDER BY received_at DESC, id DESC LIMIT ?",
                    (status, bounded),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM email_scan_queue ORDER BY received_at DESC, id DESC LIMIT ?",
                    (bounded,),
                ).fetchall()
            return [dict(r) for r in rows]

    def get_email_queue_item(
        self, item_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM email_scan_queue WHERE id = ?", (item_id,)
            ).fetchone()
            return dict(row) if row else None

    def enqueue_email_item(
        self, data: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]:
        now = utc_now_iso()
        uid_key = str(data["uid_key"])
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT id FROM email_scan_queue WHERE uid_key = ?", (uid_key,)
            ).fetchone()
            if existing:
                return None
            cur = conn.execute(
                """
                INSERT INTO email_scan_queue (
                    uid_key, sender, subject, received_at, extracted_vendor,
                    amount_cents, due_date, kind, status, matched_bill_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    uid_key,
                    str(data.get("sender", "")),
                    str(data.get("subject", "")),
                    str(data.get("received_at", now)),
                    str(data.get("extracted_vendor", "")),
                    int(data["amount_cents"]) if data.get("amount_cents") is not None else None,
                    data.get("due_date"),
                    str(data.get("kind", "bill")),
                    data.get("matched_bill_id"),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM email_scan_queue WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return dict(row)

    def update_email_queue_status(
        self,
        item_id: int,
        status: str,
        matched_bill_id: Optional[int] = None,
        ctx: RequestContext = LOCAL_CONTEXT,
    ) -> Optional[Dict[str, Any]]:
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE email_scan_queue
                SET status = ?, matched_bill_id = COALESCE(?, matched_bill_id), updated_at = ?
                WHERE id = ?
                """,
                (status, matched_bill_id, utc_now_iso(), item_id),
            )
            row = conn.execute(
                "SELECT * FROM email_scan_queue WHERE id = ?", (item_id,)
            ).fetchone()
            return dict(row) if row else None

    # --- Scenarios & Net Worth Snapshots ---

    @staticmethod
    def _decode_scenario(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        try:
            item["overrides"] = json.loads(item.pop("overrides_json", "{}"))
        except json.JSONDecodeError:
            item["overrides"] = {}
        item["active"] = bool(item.get("active", 1))
        return item

    def list_scenarios(self, ctx: RequestContext = LOCAL_CONTEXT) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM scenarios ORDER BY updated_at DESC, id DESC"
            ).fetchall()
            return [self._decode_scenario(r) for r in rows]

    def get_scenario(
        self, scenario_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM scenarios WHERE id = ?", (scenario_id,)
            ).fetchone()
            return self._decode_scenario(row) if row else None

    def create_scenario(
        self, data: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Dict[str, Any]:
        now = utc_now_iso()
        overrides_json = json.dumps(data.get("overrides", {}))
        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO scenarios (name, overrides_json, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (data["name"], overrides_json, int(bool(data.get("active", True))), now, now),
            )
            row = conn.execute("SELECT * FROM scenarios WHERE id = ?", (cur.lastrowid,)).fetchone()
            return self._decode_scenario(row)

    def update_scenario(
        self, scenario_id: int, updates: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]:
        data = dict(updates)
        if "overrides" in data and data["overrides"] is not None:
            data["overrides_json"] = json.dumps(data.pop("overrides"))
        allowed = {"name", "overrides_json", "active"}
        filtered = {k: v for k, v in data.items() if k in allowed}
        if not filtered:
            return self.get_scenario(scenario_id, ctx=ctx)
        if "active" in filtered:
            filtered["active"] = int(bool(filtered["active"]))
        filtered["updated_at"] = utc_now_iso()
        cols = ", ".join(f"{k} = ?" for k in filtered)
        with self.transaction() as conn:
            conn.execute(
                f"UPDATE scenarios SET {cols} WHERE id = ?",
                list(filtered.values()) + [scenario_id],
            )
            row = conn.execute("SELECT * FROM scenarios WHERE id = ?", (scenario_id,)).fetchone()
            return self._decode_scenario(row) if row else None

    def delete_scenario(
        self, scenario_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> bool:
        with self.transaction() as conn:
            cur = conn.execute("DELETE FROM scenarios WHERE id = ?", (scenario_id,))
            return cur.rowcount > 0

    def upsert_net_worth_snapshot(
        self,
        snap_date: str,
        liquid_cents: int,
        investment_cents: int,
        debt_cents: int,
        net_worth_cents: int,
        ctx: RequestContext = LOCAL_CONTEXT,
    ) -> Dict[str, Any]:
        now = utc_now_iso()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO net_worth_snapshots (
                    snap_date, liquid_cents, investment_cents, debt_cents, net_worth_cents, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(snap_date) DO UPDATE SET
                    liquid_cents = excluded.liquid_cents,
                    investment_cents = excluded.investment_cents,
                    debt_cents = excluded.debt_cents,
                    net_worth_cents = excluded.net_worth_cents,
                    updated_at = excluded.updated_at
                """,
                (
                    snap_date,
                    int(liquid_cents),
                    int(investment_cents),
                    int(debt_cents),
                    int(net_worth_cents),
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM net_worth_snapshots WHERE snap_date = ?", (snap_date,)
            ).fetchone()
            return dict(row)

    def list_net_worth_snapshots(
        self,
        start_date: Optional[str] = None,
        limit: int = 730,
        ctx: RequestContext = LOCAL_CONTEXT,
    ) -> List[Dict[str, Any]]:
        bounded = max(1, min(2000, int(limit)))
        with self._lock:
            if start_date:
                rows = self._conn.execute(
                    "SELECT * FROM net_worth_snapshots WHERE snap_date >= ? ORDER BY snap_date ASC LIMIT ?",
                    (start_date, bounded),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM net_worth_snapshots ORDER BY snap_date ASC LIMIT ?",
                    (bounded,),
                ).fetchall()
            return [dict(r) for r in rows]

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
        ctx: RequestContext = LOCAL_CONTEXT,
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

    def list_impulses(
        self, include_resolved: bool = False, ctx: RequestContext = LOCAL_CONTEXT
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM impulse_vault"
        if not include_resolved:
            sql += " WHERE status IN ('cooling', 'ready')"
        sql += " ORDER BY unlocks_at ASC, id DESC"
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql).fetchall()]

    def get_impulse(
        self, impulse_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM impulse_vault WHERE id = ?", (impulse_id,)).fetchone()
            return dict(row) if row else None

    def resolve_impulse(
        self, impulse_id: int, status: str, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE impulse_vault SET status = ?, decision_at = ? WHERE id = ?",
                (status, utc_now_iso(), impulse_id),
            )
            row = conn.execute("SELECT * FROM impulse_vault WHERE id = ?", (impulse_id,)).fetchone()
            return dict(row) if row else None

    # --- Budget Transfers & Audit Log ---

    def record_budget_transfer(
        self,
        cycle_ym: str,
        from_jar_id: int,
        to_jar_id: int,
        amount_cents: int,
        reason: str,
        ctx: RequestContext = LOCAL_CONTEXT,
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

    def list_budget_transfers(
        self, cycle_ym: str, ctx: RequestContext = LOCAL_CONTEXT
    ) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM budget_transfers WHERE cycle_ym = ? ORDER BY id ASC",
                (cycle_ym,),
            ).fetchall()
            return [dict(r) for r in rows]

    def log_budget_event(
        self,
        event_type: str,
        summary: str,
        details: Optional[Dict[str, Any]] = None,
        ctx: RequestContext = LOCAL_CONTEXT,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO budget_events (event_type, summary, details_json, created_at) VALUES (?, ?, ?, ?)",
                (event_type, summary, json.dumps(details or {}), utc_now_iso()),
            )

    def list_budget_events(
        self, limit: int = 25, ctx: RequestContext = LOCAL_CONTEXT
    ) -> List[Dict[str, Any]]:
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

    def get_adhd_progress(self, ctx: RequestContext = LOCAL_CONTEXT) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM adhd_progress WHERE id = 1").fetchone()
            data = dict(row) if row else {}
            try:
                data["recent_wins"] = json.loads(data.pop("recent_wins_json", "[]"))
            except json.JSONDecodeError:
                data["recent_wins"] = []
            return data

    def update_adhd_progress(
        self, updates: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Dict[str, Any]:
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
            return self.get_adhd_progress(ctx=ctx)
        filtered["updated_at"] = utc_now_iso()
        cols = ", ".join(f"{k} = ?" for k in filtered)
        with self.transaction() as conn:
            conn.execute(f"UPDATE adhd_progress SET {cols} WHERE id = 1", list(filtered.values()))
        return self.get_adhd_progress(ctx=ctx)

    # --- AI Usage Telemetry, Threads & Agent Action Batches ---

    def count_ai_requests_today(self, ctx: RequestContext = LOCAL_CONTEXT) -> int:
        """Count metered cloud AI requests today (excludes free/subscription local CLIs and zero-cost local gateways)."""
        today = date.today().isoformat()
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*)
                FROM ai_usage_log
                WHERE usage_date = ?
                  AND cached = 0
                  AND provider NOT IN ('claude_cli', 'opencode_cli', 'codex_cli', 'agy_cli')
                  AND NOT (provider = 'chat_wrapper' AND cost_microdollars = 0)
                """,
                (today,),
            ).fetchone()
            return int(row[0]) if row else 0

    def record_ai_usage(
        self,
        provider: str,
        model: str,
        cached: bool = False,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_tokens: int = 0,
        cost_microdollars: int = 0,
        saved_microdollars: int = 0,
        latency_ms: int = 0,
        thread_id: Optional[str] = None,
        ctx: RequestContext = LOCAL_CONTEXT,
    ) -> None:
        with self._lock:
            if self._closed:
                return
            with self.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO ai_usage_log (
                        usage_date, provider, model, cached, input_tokens, output_tokens,
                        cached_tokens, cost_microdollars, saved_microdollars, latency_ms, thread_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        date.today().isoformat(),
                        provider,
                        model,
                        int(bool(cached)),
                        int(input_tokens),
                        int(output_tokens),
                        int(cached_tokens),
                        int(cost_microdollars),
                        int(saved_microdollars),
                        int(latency_ms),
                        thread_id,
                        utc_now_iso(),
                    ),
                )

    def get_ai_usage_summary(
        self, days: int = 30, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Dict[str, Any]:
        bounded_days = max(1, min(365, int(days)))
        cutoff = (date.today() - timedelta(days=bounded_days - 1)).isoformat()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    provider,
                    model,
                    COUNT(*) AS requests,
                    SUM(CASE WHEN cached = 1 THEN 1 ELSE 0 END) AS cached_responses,
                    COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                    COALESCE(SUM(cost_microdollars), 0) AS cost_microdollars,
                    COALESCE(SUM(saved_microdollars), 0) AS saved_microdollars,
                    COALESCE(CAST(AVG(latency_ms) AS INTEGER), 0) AS avg_latency_ms
                FROM ai_usage_log
                WHERE usage_date >= ?
                GROUP BY provider, model
                ORDER BY requests DESC, provider ASC
                """,
                (cutoff,),
            ).fetchall()

            by_provider = []
            tot_req = 0
            tot_in = 0
            tot_out = 0
            tot_cached_tok = 0
            tot_cost = 0
            tot_saved = 0
            tot_cached_resp = 0

            for r in rows:
                req = int(r["requests"])
                c_resp = int(r["cached_responses"] or 0)
                in_tok = int(r["input_tokens"])
                out_tok = int(r["output_tokens"])
                c_tok = int(r["cached_tokens"])
                hit_pct = (
                    round((c_tok / in_tok) * 100.0, 2)
                    if in_tok > 0
                    else (round((c_resp / req) * 100.0, 2) if req > 0 else 0.0)
                )
                by_provider.append(
                    {
                        "provider": r["provider"],
                        "model": r["model"],
                        "requests": req,
                        "cached_responses": c_resp,
                        "input_tokens": in_tok,
                        "output_tokens": out_tok,
                        "cached_tokens": c_tok,
                        "cache_hit_rate_pct": hit_pct,
                        "cost_microdollars": int(r["cost_microdollars"]),
                        "saved_microdollars": int(r["saved_microdollars"]),
                        "avg_latency_ms": int(r["avg_latency_ms"]),
                    }
                )
                tot_req += req
                tot_cached_resp += c_resp
                tot_in += in_tok
                tot_out += out_tok
                tot_cached_tok += c_tok
                tot_cost += int(r["cost_microdollars"])
                tot_saved += int(r["saved_microdollars"])

            overall_hit = (
                round((tot_cached_tok / tot_in) * 100.0, 2)
                if tot_in > 0
                else (round((tot_cached_resp / tot_req) * 100.0, 2) if tot_req > 0 else 0.0)
            )
            req_today = self.count_ai_requests_today(ctx=ctx)
            settings = self.get_settings(ctx=ctx)
            quota_limit = int(settings.get("opencode_max_daily_requests", 60))
            return {
                "days": bounded_days,
                "requests_today": req_today,
                "daily_quota_limit": quota_limit,
                "quota_remaining_today": max(0, quota_limit - req_today),
                "total_requests": tot_req,
                "total_input_tokens": tot_in,
                "total_output_tokens": tot_out,
                "total_cached_tokens": tot_cached_tok,
                "cache_hit_rate_pct": overall_hit,
                "total_cost_microdollars": tot_cost,
                "total_saved_microdollars": tot_saved,
                "by_provider": by_provider,
            }

    def list_ai_threads(self, ctx: RequestContext = LOCAL_CONTEXT) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM ai_threads ORDER BY updated_at DESC"
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                try:
                    d["last_snapshot"] = json.loads(d.pop("last_snapshot_json", "{}"))
                except json.JSONDecodeError:
                    d["last_snapshot"] = {}
                out.append(d)
            return out

    def upsert_ai_thread(
        self, data: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Dict[str, Any]:
        now = utc_now_iso()
        snap_json = json.dumps(data.get("last_snapshot", {}))
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO ai_threads (
                    id, provider, model, remote_response_id, last_state_hash, last_snapshot_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    provider = excluded.provider,
                    model = excluded.model,
                    remote_response_id = excluded.remote_response_id,
                    last_state_hash = excluded.last_state_hash,
                    last_snapshot_json = excluded.last_snapshot_json,
                    updated_at = excluded.updated_at
                """,
                (
                    str(data["id"]),
                    str(data.get("provider", "")),
                    str(data.get("model", "")),
                    str(data.get("remote_response_id", "")),
                    str(data.get("last_state_hash", "")),
                    snap_json,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM ai_threads WHERE id = ?", (str(data["id"]),)
            ).fetchone()
            item = dict(row)
            item["last_snapshot"] = json.loads(item.pop("last_snapshot_json", "{}"))
            return item

    def delete_ai_thread(self, thread_id: str, ctx: RequestContext = LOCAL_CONTEXT) -> bool:
        with self.transaction() as conn:
            cur = conn.execute("DELETE FROM ai_threads WHERE id = ?", (thread_id,))
            return cur.rowcount > 0

    @staticmethod
    def _decode_agent_batch(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        try:
            item["actions"] = json.loads(item.pop("actions_json", "[]"))
        except json.JSONDecodeError:
            item["actions"] = []
        try:
            item["undo_snapshot"] = json.loads(item.pop("undo_snapshot_json", "{}"))
        except json.JSONDecodeError:
            item["undo_snapshot"] = {}
        return item

    def create_agent_batch(
        self, data: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Dict[str, Any]:
        now = utc_now_iso()
        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO agent_action_batches (
                    prompt, provider, model, actions_json, undo_snapshot_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(data["prompt"]),
                    str(data.get("provider", "deterministic_local")),
                    str(data.get("model", "")),
                    json.dumps(data.get("actions", [])),
                    json.dumps(data.get("undo_snapshot", {})),
                    str(data.get("status", "preview")),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM agent_action_batches WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return self._decode_agent_batch(row)

    def get_agent_batch(
        self, batch_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM agent_action_batches WHERE id = ?", (batch_id,)
            ).fetchone()
            return self._decode_agent_batch(row) if row else None

    def update_agent_batch(
        self, batch_id: int, updates: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]:
        data = dict(updates)
        if "actions" in data and data["actions"] is not None:
            data["actions_json"] = json.dumps(data.pop("actions"))
        if "undo_snapshot" in data and data["undo_snapshot"] is not None:
            data["undo_snapshot_json"] = json.dumps(data.pop("undo_snapshot"))
        allowed = {"actions_json", "undo_snapshot_json", "status"}
        filtered = {k: v for k, v in data.items() if k in allowed}
        if not filtered:
            return self.get_agent_batch(batch_id, ctx=ctx)
        filtered["updated_at"] = utc_now_iso()
        cols = ", ".join(f"{k} = ?" for k in filtered)
        with self.transaction() as conn:
            conn.execute(
                f"UPDATE agent_action_batches SET {cols} WHERE id = ?",
                list(filtered.values()) + [batch_id],
            )
            row = conn.execute(
                "SELECT * FROM agent_action_batches WHERE id = ?", (batch_id,)
            ).fetchone()
            return self._decode_agent_batch(row) if row else None

    def list_agent_batches(
        self, limit: int = 50, ctx: RequestContext = LOCAL_CONTEXT
    ) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM agent_action_batches ORDER BY id DESC LIMIT ?",
                (max(1, min(200, int(limit))),),
            ).fetchall()
            return [self._decode_agent_batch(r) for r in rows]

    @staticmethod
    def _decode_onetap_batch(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        try:
            item["plan"] = json.loads(item.pop("plan_json", "{}"))
        except json.JSONDecodeError:
            item["plan"] = {}
        try:
            item["undo_snapshot"] = json.loads(item.pop("undo_snapshot_json", "{}"))
        except json.JSONDecodeError:
            item["undo_snapshot"] = {}
        return item

    def create_onetap_batch(
        self, data: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Dict[str, Any]:
        now = utc_now_iso()
        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO onetap_batches (
                    horizon_days, provider, model, plan_json, undo_snapshot_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(data.get("horizon_days", 30)),
                    str(data.get("provider", "deterministic_local")),
                    str(data.get("model", "")),
                    json.dumps(data.get("plan", {})),
                    json.dumps(data.get("undo_snapshot", {})),
                    str(data.get("status", "applied")),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM onetap_batches WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return self._decode_onetap_batch(row)

    def get_onetap_batch(
        self, batch_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM onetap_batches WHERE id = ?", (batch_id,)
            ).fetchone()
            return self._decode_onetap_batch(row) if row else None

    def list_onetap_batches(
        self, limit: int = 50, ctx: RequestContext = LOCAL_CONTEXT
    ) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM onetap_batches ORDER BY id DESC LIMIT ?",
                (max(1, min(200, int(limit))),),
            ).fetchall()
            return [self._decode_onetap_batch(r) for r in rows]

    # --- Credit Lines, Reports, Inquiries & Derogatories ---

    def list_credit_lines(
        self,
        active_only: bool = True,
        *,
        include_sandbox: bool = False,
        ctx: RequestContext = LOCAL_CONTEXT,
    ) -> List[Dict[str, Any]]:
        clauses = []
        if active_only:
            clauses.append("active = 1")
        if not include_sandbox:
            clauses.append("COALESCE(source_env, 'production') != 'sandbox'")
        sql = "SELECT * FROM credit_lines"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY CASE WHEN kind = 'credit_card' THEN 0 ELSE 1 END, apr_bps DESC, id ASC"
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql).fetchall()]

    def get_credit_line(
        self, line_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM credit_lines WHERE id = ?", (line_id,)).fetchone()
            return dict(row) if row else None

    def create_credit_line(
        self, data: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Dict[str, Any]:
        now = utc_now_iso()
        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO credit_lines (
                    name, issuer, kind, balance_cents, credit_limit_cents, apr_bps,
                    min_payment_cents, statement_closing_day, payment_due_day,
                    promo_apr_bps, promo_ends_date, opened_date, has_grace_period,
                    autopay_mode, rewards_note, account_id, is_authorized_user,
                    source_env, external_account_id, active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    data["name"],
                    data.get("issuer", ""),
                    data.get("kind", "credit_card"),
                    int(data.get("balance_cents", 0)),
                    int(data.get("credit_limit_cents", 0)),
                    int(data.get("apr_bps", 0)),
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
                    int(bool(data.get("is_authorized_user", False))),
                    str(data.get("source_env") or "production"),
                    str(data.get("external_account_id") or ""),
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM credit_lines WHERE id = ?", (cur.lastrowid,)).fetchone()
            return dict(row)

    def update_credit_line(
        self, line_id: int, updates: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]:
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
            "is_authorized_user",
            "source_env",
            "external_account_id",
            "active",
        }
        filtered = {k: v for k, v in updates.items() if k in allowed}
        if not filtered:
            return self.get_credit_line(line_id, ctx=ctx)
        for bcol in ("has_grace_period", "is_authorized_user", "active"):
            if bcol in filtered:
                filtered[bcol] = int(bool(filtered[bcol]))
        filtered["updated_at"] = utc_now_iso()
        cols = ", ".join(f"{k} = ?" for k in filtered)
        with self.transaction() as conn:
            conn.execute(
                f"UPDATE credit_lines SET {cols} WHERE id = ?",
                list(filtered.values()) + [line_id],
            )
            row = conn.execute("SELECT * FROM credit_lines WHERE id = ?", (line_id,)).fetchone()
            return dict(row) if row else None

    def delete_credit_line(self, line_id: int, ctx: RequestContext = LOCAL_CONTEXT) -> bool:
        with self.transaction() as conn:
            cur = conn.execute("DELETE FROM credit_lines WHERE id = ?", (line_id,))
            return cur.rowcount > 0

    def list_credit_reports(
        self,
        *,
        include_sandbox: bool = False,
        ctx: RequestContext = LOCAL_CONTEXT,
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM credit_reports"
        if not include_sandbox:
            sql += " WHERE COALESCE(is_sandbox, 0) = 0"
        sql += " ORDER BY pulled_date DESC, id DESC"
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql).fetchall()]

    def create_credit_report(
        self, data: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Dict[str, Any]:
        now = utc_now_iso()
        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO credit_reports (bureau, model, score, pulled_date, source, notes, is_sandbox, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(data["bureau"]),
                    str(data.get("model", "fico8")),
                    int(data["score"]),
                    str(data.get("pulled_date") or date.today().isoformat()),
                    str(data.get("source", "manual")),
                    str(data.get("notes", "")),
                    int(bool(data.get("is_sandbox", False))),
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM credit_reports WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return dict(row)

    def delete_credit_report(
        self, report_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> bool:
        with self.transaction() as conn:
            cur = conn.execute("DELETE FROM credit_reports WHERE id = ?", (report_id,))
            return cur.rowcount > 0

    def list_credit_inquiries(
        self,
        *,
        include_sandbox: bool = False,
        ctx: RequestContext = LOCAL_CONTEXT,
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM credit_inquiries"
        if not include_sandbox:
            sql += " WHERE COALESCE(is_sandbox, 0) = 0"
        sql += " ORDER BY inquiry_date DESC, id DESC"
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql).fetchall()]

    def create_credit_inquiry(
        self, data: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Dict[str, Any]:
        now = utc_now_iso()
        inq_date = str(data["inquiry_date"])
        counts_until = data.get("counts_until") or _add_months_iso(inq_date, 12)
        drops_off = data.get("drops_off") or _add_months_iso(inq_date, 24)
        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO credit_inquiries (lender, bureau, inquiry_date, counts_until, drops_off, is_sandbox, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(data["lender"]),
                    str(data.get("bureau", "experian")),
                    inq_date,
                    counts_until,
                    drops_off,
                    int(bool(data.get("is_sandbox", False))),
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM credit_inquiries WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return dict(row)

    def delete_credit_inquiry(
        self, inquiry_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> bool:
        with self.transaction() as conn:
            cur = conn.execute("DELETE FROM credit_inquiries WHERE id = ?", (inquiry_id,))
            return cur.rowcount > 0

    def list_credit_derogatories(
        self,
        active_only: bool = False,
        *,
        include_sandbox: bool = False,
        ctx: RequestContext = LOCAL_CONTEXT,
    ) -> List[Dict[str, Any]]:
        clauses = []
        if active_only:
            clauses.append("active = 1")
        if not include_sandbox:
            clauses.append("COALESCE(is_sandbox, 0) = 0")
        sql = "SELECT * FROM credit_derogatories"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY drops_off ASC, id DESC"
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql).fetchall()]

    def create_credit_derogatory(
        self, data: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Dict[str, Any]:
        now = utc_now_iso()
        kind = str(data["kind"])
        opened_date = str(data["opened_date"])
        drops_off = data.get("drops_off") or compute_derogatory_drop_off(kind, opened_date)
        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO credit_derogatories (kind, lender, opened_date, drops_off, active, notes, is_sandbox, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    kind,
                    str(data.get("lender", "")),
                    opened_date,
                    drops_off,
                    int(bool(data.get("active", True))),
                    str(data.get("notes", "")),
                    int(bool(data.get("is_sandbox", False))),
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM credit_derogatories WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return dict(row)

    def delete_credit_derogatory(
        self, derog_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> bool:
        with self.transaction() as conn:
            cur = conn.execute("DELETE FROM credit_derogatories WHERE id = ?", (derog_id,))
            return cur.rowcount > 0

    # --- Portfolio Holdings & Snapshots ---

    def list_holdings(self, ctx: RequestContext = LOCAL_CONTEXT) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM holdings ORDER BY market_value_cents DESC, symbol ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_holding(
        self, holding_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM holdings WHERE id = ?", (holding_id,)
            ).fetchone()
            return dict(row) if row else None

    def upsert_holding(
        self, data: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Dict[str, Any]:
        now = utc_now_iso()
        symbol = str(data["symbol"]).strip().upper()
        qty_e8 = int(data.get("quantity_e8", 0))
        price_e8 = int(data.get("price_e8", 0))
        market_val = compute_holding_value_cents(qty_e8, price_e8)
        raw_cb = data.get("cost_basis_cents")
        cb_known_in = data.get("cost_basis_known")
        if cb_known_in is not None:
            cb_known = bool(cb_known_in)
        elif raw_cb is None:
            cb_known = False
        else:
            cb_known = int(raw_cb) > 0 or str(data.get("asset_class", "")).lower() == "cash"
        cost_basis = int(raw_cb or 0)
        holding_id = data.get("id")
        conn_id = data.get("connection_id")
        acct_id = data.get("account_id")
        ext_acct_id = str(data.get("external_account_id") or "").strip()

        with self.transaction() as conn:
            existing = None
            if holding_id is not None:
                existing = conn.execute(
                    "SELECT * FROM holdings WHERE id = ?", (int(holding_id),)
                ).fetchone()
            elif conn_id is not None:
                if ext_acct_id:
                    existing = conn.execute(
                        """
                        SELECT * FROM holdings
                        WHERE connection_id = ?
                          AND COALESCE(external_account_id, '') = ?
                          AND symbol = ?
                        """,
                        (int(conn_id), ext_acct_id, symbol),
                    ).fetchone()
                else:
                    existing = conn.execute(
                        """
                        SELECT * FROM holdings
                        WHERE connection_id = ?
                          AND COALESCE(external_account_id, '') = ''
                          AND COALESCE(account_id, -1) = COALESCE(?, -1)
                          AND symbol = ?
                        """,
                        (int(conn_id), acct_id, symbol),
                    ).fetchone()

            if existing:
                # Preserve existing known cost basis if a quote refresh / broker sync didn't supply one
                if cb_known_in is None and raw_cb is None:
                    cost_basis = int(existing["cost_basis_cents"] or 0)
                    cb_known = bool(existing["cost_basis_known"] if "cost_basis_known" in existing.keys() else (cost_basis > 0))
                conn.execute(
                    """
                    UPDATE holdings
                    SET account_id = COALESCE(?, account_id),
                        external_account_id = CASE WHEN ? != '' THEN ? ELSE external_account_id END,
                        name = COALESCE(NULLIF(?, ''), name),
                        asset_class = ?,
                        quantity_e8 = ?,
                        cost_basis_cents = ?,
                        cost_basis_known = ?,
                        price_e8 = ?,
                        market_value_cents = ?,
                        source = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        acct_id,
                        ext_acct_id,
                        ext_acct_id,
                        str(data.get("name", "")),
                        str(data.get("asset_class", existing["asset_class"])),
                        qty_e8,
                        cost_basis,
                        int(cb_known),
                        price_e8,
                        market_val,
                        str(data.get("source", existing["source"])),
                        now,
                        existing["id"],
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM holdings WHERE id = ?", (existing["id"],)
                ).fetchone()
                return dict(row)

            created_ts = str(data.get("created_at") or now)
            cur = conn.execute(
                """
                INSERT INTO holdings (
                    account_id, connection_id, external_account_id, symbol, name, asset_class,
                    quantity_e8, cost_basis_cents, cost_basis_known, price_e8, market_value_cents,
                    source, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    acct_id,
                    conn_id,
                    ext_acct_id,
                    symbol,
                    str(data.get("name", "")),
                    str(data.get("asset_class", "equity")),
                    qty_e8,
                    cost_basis,
                    int(cb_known),
                    price_e8,
                    market_val,
                    str(data.get("source", "manual")),
                    created_ts,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM holdings WHERE id = ?", (cur.lastrowid,)).fetchone()
            return dict(row)

    def prune_connection_holdings(
        self,
        connection_id: int,
        keep_ids: Sequence[int],
        ctx: RequestContext = LOCAL_CONTEXT,
    ) -> int:
        """Delete stale holdings for `connection_id` that were no longer returned by the broker."""
        with self.transaction() as conn:
            if not keep_ids:
                cur = conn.execute("DELETE FROM holdings WHERE connection_id = ?", (int(connection_id),))
                return int(cur.rowcount)
            placeholders = ",".join("?" for _ in keep_ids)
            cur = conn.execute(
                f"DELETE FROM holdings WHERE connection_id = ? AND id NOT IN ({placeholders})",
                [int(connection_id), *[int(i) for i in keep_ids]],
            )
            return int(cur.rowcount)

    def delete_holding(self, holding_id: int, ctx: RequestContext = LOCAL_CONTEXT) -> bool:
        with self.transaction() as conn:
            cur = conn.execute("DELETE FROM holdings WHERE id = ?", (holding_id,))
            return cur.rowcount > 0

    def upsert_portfolio_snapshot(
        self,
        snap_date: str,
        total_value_cents: int,
        cost_basis_cents: int,
        ctx: RequestContext = LOCAL_CONTEXT,
    ) -> Dict[str, Any]:
        now = utc_now_iso()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO portfolio_snapshots (snap_date, total_value_cents, cost_basis_cents, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(snap_date) DO UPDATE SET
                    total_value_cents = excluded.total_value_cents,
                    cost_basis_cents = excluded.cost_basis_cents,
                    updated_at = excluded.updated_at
                """,
                (snap_date, int(total_value_cents), int(cost_basis_cents), now),
            )
            row = conn.execute(
                "SELECT * FROM portfolio_snapshots WHERE snap_date = ?", (snap_date,)
            ).fetchone()
            return dict(row)

    def list_portfolio_snapshots(
        self, limit: int = 730, ctx: RequestContext = LOCAL_CONTEXT
    ) -> List[Dict[str, Any]]:
        bounded = max(1, min(2000, int(limit)))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM portfolio_snapshots ORDER BY snap_date ASC LIMIT ?", (bounded,)
            ).fetchall()
            return [dict(r) for r in rows]

    # --- Savings & Purchase Goals (via `goals_active` VIEW) ---

    def list_goals(
        self, include_completed: bool = False, ctx: RequestContext = LOCAL_CONTEXT
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM goals_active"
        if not include_completed:
            sql += " WHERE status != 'completed'"
        sql += " ORDER BY priority_order ASC, priority ASC, id ASC"
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql).fetchall()]

    def get_goal(
        self,
        goal_id: int,
        include_deleted: bool = False,
        ctx: RequestContext = LOCAL_CONTEXT,
    ) -> Optional[Dict[str, Any]]:
        table = "goals" if include_deleted else "goals_active"
        with self._lock:
            row = self._conn.execute(f"SELECT * FROM {table} WHERE id = ?", (goal_id,)).fetchone()
            return dict(row) if row else None

    def create_goal(
        self, data: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Dict[str, Any]:
        now = utc_now_iso()
        saved = int(data.get("saved_cents", 0))
        target = int(data["target_cents"])
        status = "funded" if saved >= target else "active"
        with self.transaction() as conn:
            prio_order = data.get("priority_order")
            if prio_order is None:
                mx = conn.execute(
                    "SELECT COALESCE(MAX(priority_order), 0) FROM goals_active"
                ).fetchone()
                prio_order = int(mx[0]) + 1 if mx else 1
            cur = conn.execute(
                """
                INSERT INTO goals (
                    name, target_cents, saved_cents, target_date, priority, priority_order,
                    category, funding_jar_slug, status, notes, deleted_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    data["name"],
                    target,
                    saved,
                    data.get("target_date"),
                    int(data.get("priority", 2)),
                    int(prio_order),
                    data.get("category", "gadget"),
                    data.get("funding_jar_slug", "fun"),
                    status,
                    data.get("notes", ""),
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM goals_active WHERE id = ?", (cur.lastrowid,)).fetchone()
            return dict(row)

    def update_goal(
        self, goal_id: int, updates: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]:
        allowed = {
            "name",
            "target_cents",
            "saved_cents",
            "target_date",
            "priority",
            "priority_order",
            "status",
            "notes",
        }
        filtered = {k: v for k, v in updates.items() if k in allowed}
        if not filtered:
            return self.get_goal(goal_id, ctx=ctx)
        filtered["updated_at"] = utc_now_iso()
        cols = ", ".join(f"{k} = ?" for k in filtered)
        with self.transaction() as conn:
            conn.execute(
                f"UPDATE goals SET {cols} WHERE id = ? AND deleted_at IS NULL",
                list(filtered.values()) + [goal_id],
            )
            row = conn.execute("SELECT * FROM goals_active WHERE id = ?", (goal_id,)).fetchone()
            return dict(row) if row else None

    def reorder_goals(
        self, ordered_ids: List[int], ctx: RequestContext = LOCAL_CONTEXT
    ) -> List[Dict[str, Any]]:
        now = utc_now_iso()
        with self.transaction(immediate=True) as conn:
            for idx, gid in enumerate(ordered_ids, start=1):
                conn.execute(
                    "UPDATE goals SET priority_order = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
                    (idx, now, int(gid)),
                )
        return self.list_goals(include_completed=True, ctx=ctx)

    def soft_delete_goal(
        self, goal_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]:
        now = utc_now_iso()
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE goals SET deleted_at = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
                (now, now, goal_id),
            )
            if cur.rowcount == 0:
                return None
            row = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
            return dict(row) if row else None

    def restore_goal(
        self, goal_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]:
        now = utc_now_iso()
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE goals SET deleted_at = NULL, updated_at = ? WHERE id = ? AND deleted_at IS NOT NULL",
                (now, goal_id),
            )
            if cur.rowcount == 0:
                return None
            row = conn.execute("SELECT * FROM goals_active WHERE id = ?", (goal_id,)).fetchone()
            return dict(row) if row else None

    def purge_deleted_goals(
        self, older_than_hours: int = 24, ctx: RequestContext = LOCAL_CONTEXT
    ) -> int:
        cutoff = (
            datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=max(0, int(older_than_hours)))
        ).isoformat().replace("+00:00", "Z")
        with self.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM goals WHERE deleted_at IS NOT NULL AND deleted_at <= ?",
                (cutoff,),
            )
            return int(cur.rowcount)

    def delete_goal(self, goal_id: int, ctx: RequestContext = LOCAL_CONTEXT) -> bool:
        with self.transaction() as conn:
            cur = conn.execute("DELETE FROM goals WHERE id = ?", (goal_id,))
            return cur.rowcount > 0

    # --- Backup / Restore / Clear ---

    def clear_all_financial_data(self, ctx: RequestContext = LOCAL_CONTEXT) -> None:
        """Reset financial tables cleanly (e.g., before restoring backup or running test fixtures)."""
        with self.transaction() as conn:
            for tbl in (
                "transaction_splits",
                "transaction_tags",
                "transaction_comments",
                "tags",
                "automation_rules",
                "rule_groups",
                "email_scan_queue",
                "transactions",
                "recurring_bills",
                "impulse_vault",
                "budget_transfers",
                "budget_events",
                "merchant_rules",
                "credit_reports",
                "credit_inquiries",
                "credit_derogatories",
                "credit_lines",
                "holdings",
                "portfolio_snapshots",
                "net_worth_snapshots",
                "scenarios",
                "agent_action_batches",
                "ai_threads",
                "bank_connections",
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
            conn.execute("UPDATE jars SET auto_allocated_cents=0, rollover_cents=0, updated_at=?", (utc_now_iso(),))
            conn.execute(
                "UPDATE profile_settings SET expected_paycheck_cents=0, income_configured=0, "
                "payday_user_configured=0, last_rollover_ym='', updated_at=? WHERE id=1",
                (utc_now_iso(),),
            )

    def preview_backup(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Validate a ChronoX backup payload without mutating ledger data and return a summary."""
        if not isinstance(payload, dict) or payload.get("schema_version") not in (1, 2):
            raise ValueError("Unsupported or invalid ChronoX Finance backup format")
        if not payload.get("jars"):
            raise ValueError("Backup is missing required budget jars")
        if isinstance(payload.get("settings"), dict):
            for url_key in ("opencode_base_url", "gemini_base_url", "codex_base_url", "wrapper_base_url"):
                if payload["settings"].get(url_key):
                    validate_external_api_url(str(payload["settings"][url_key]))
        txs = payload.get("transactions") or []
        if not isinstance(txs, list):
            raise ValueError("Invalid transactions array in backup")
        for tx in txs:
            if not isinstance(tx, dict) or "account_id" not in tx or "tx_type" not in tx or "amount_cents" not in tx or "tx_date" not in tx:
                raise ValueError("Backup contains malformed transaction records")
        accts = payload.get("accounts") or []
        goals_list = payload.get("goals") or payload.get("savings_goals") or []
        dates = [str(t.get("tx_date", "")) for t in txs if t.get("tx_date")]
        date_window = {
            "start": min(dates) if dates else None,
            "end": max(dates) if dates else None,
        }
        return {
            "valid": True,
            "schema_version": int(payload["schema_version"]),
            "exported_at": str(payload.get("exported_at") or ""),
            "counts": {
                "accounts": len(accts),
                "jars": len(payload.get("jars") or []),
                "transactions": len(txs),
                "recurring_bills": len(payload.get("recurring_bills") or []),
                "goals": len(goals_list),
                "savings_goals": len(goals_list),
                "credit_lines": len(payload.get("credit_lines") or []),
                "holdings": len(payload.get("holdings") or []),
                "automation_rules": len(payload.get("automation_rules") or []),
            },
            "date_range": date_window,
            "tx_date_range": date_window,
        }

    def export_backup(self, ctx: RequestContext = LOCAL_CONTEXT) -> Dict[str, Any]:
        """
        Export complete sanitized backup.
        NEVER includes vault salts/wrapped DEKs, `secrets.enc`, Plaid/broker/IMAP creds, or AI keys.
        """
        settings = self.get_settings(ctx=ctx)
        for secret_field in _SECRET_SETTINGS_FIELDS:
            settings.pop(secret_field, None)
        with self._lock:
            return {
                "schema_version": LATEST_SCHEMA_VERSION,
                "exported_at": utc_now_iso(),
                "settings": settings,
                "accounts": self.list_accounts(ctx=ctx),
                "jars": self.list_jars(ctx=ctx),
                "transactions": [
                    dict(r) for r in self._conn.execute("SELECT * FROM transactions ORDER BY id ASC").fetchall()
                ],
                "transaction_splits": [
                    dict(r)
                    for r in self._conn.execute("SELECT * FROM transaction_splits ORDER BY id ASC").fetchall()
                ],
                "tags": self.list_tags(ctx=ctx),
                "transaction_tags": [
                    dict(r)
                    for r in self._conn.execute(
                        "SELECT * FROM transaction_tags ORDER BY tx_id ASC, tag_id ASC"
                    ).fetchall()
                ],
                "transaction_comments": [
                    dict(r)
                    for r in self._conn.execute("SELECT * FROM transaction_comments ORDER BY id ASC").fetchall()
                ],
                "merchant_rules": [
                    dict(r) for r in self._conn.execute("SELECT * FROM merchant_rules").fetchall()
                ],
                "rule_groups": self.list_rule_groups(ctx=ctx),
                "automation_rules": self.list_rules(active_only=False, ctx=ctx),
                "recurring_bills": self.list_recurring_bills(active_only=False, ctx=ctx),
                "impulse_vault": self.list_impulses(include_resolved=True, ctx=ctx),
                "budget_transfers": [
                    dict(r) for r in self._conn.execute("SELECT * FROM budget_transfers ORDER BY id ASC").fetchall()
                ],
                "credit_lines": self.list_credit_lines(active_only=False, include_sandbox=False, ctx=ctx),
                "credit_reports": self.list_credit_reports(include_sandbox=False, ctx=ctx),
                "credit_inquiries": self.list_credit_inquiries(include_sandbox=False, ctx=ctx),
                "credit_derogatories": self.list_credit_derogatories(active_only=False, include_sandbox=False, ctx=ctx),
                "holdings": self.list_holdings(ctx=ctx),
                "portfolio_snapshots": self.list_portfolio_snapshots(ctx=ctx),
                "net_worth_snapshots": self.list_net_worth_snapshots(ctx=ctx),
                "scenarios": self.list_scenarios(ctx=ctx),
                "goals": self.list_goals(include_completed=True, ctx=ctx),
                "adhd_progress": self.get_adhd_progress(ctx=ctx),
            }

    def import_backup(
        self, payload: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> None:
        """Restore a sanitized JSON backup (v1 or v2) atomically."""
        self.preview_backup(payload)

        now = utc_now_iso()
        with self.transaction(immediate=True) as conn:
            for tbl in (
                "transaction_splits",
                "transaction_tags",
                "transaction_comments",
                "tags",
                "automation_rules",
                "rule_groups",
                "email_scan_queue",
                "transactions",
                "recurring_bills",
                "impulse_vault",
                "budget_transfers",
                "budget_events",
                "merchant_rules",
                "credit_reports",
                "credit_inquiries",
                "credit_derogatories",
                "credit_lines",
                "holdings",
                "portfolio_snapshots",
                "net_worth_snapshots",
                "scenarios",
                "goals",
                "accounts",
                "jars",
            ):
                conn.execute(f"DELETE FROM {tbl}")

            for jar in payload.get("jars", []):
                icon = _EMOJI_ICON_MIGRATION.get(jar.get("icon", ""), jar.get("icon", "wallet"))
                conn.execute(
                    """
                    INSERT INTO jars (
                        id, slug, name, kind, icon, color, priority, target_pct,
                        monthly_target_cents, auto_allocated_cents, rollover_cents,
                        is_locked, rollover_enabled, rollover_cap_cents, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        jar["id"],
                        jar["slug"],
                        jar["name"],
                        jar["kind"],
                        icon,
                        jar["color"],
                        jar["priority"],
                        jar["target_pct"],
                        jar["monthly_target_cents"],
                        jar["auto_allocated_cents"],
                        jar["rollover_cents"],
                        jar["is_locked"],
                        jar["rollover_enabled"],
                        jar.get("rollover_cap_cents"),
                        jar.get("updated_at", now),
                    ),
                )

            for acct in payload.get("accounts", []):
                conn.execute(
                    """
                    INSERT INTO accounts (
                        id, name, account_type, balance_cents, institution, is_liquid,
                        connection_id, external_id, mask, currency, available_cents, credit_limit_cents,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        acct["id"],
                        acct["name"],
                        acct["account_type"],
                        acct["balance_cents"],
                        acct.get("institution", ""),
                        acct.get("is_liquid", 1),
                        acct.get("external_id", ""),
                        acct.get("mask", ""),
                        acct.get("currency", "USD"),
                        acct.get("available_cents"),
                        acct.get("credit_limit_cents"),
                        acct.get("created_at", now),
                        acct.get("updated_at", now),
                    ),
                )

            for rule in payload.get("merchant_rules", []):
                conn.execute(
                    """
                    INSERT INTO merchant_rules (normalized_merchant, jar_id, subcategory, hit_count, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        rule["normalized_merchant"],
                        rule["jar_id"],
                        rule.get("subcategory", ""),
                        rule.get("hit_count", 1),
                        rule.get("updated_at", now),
                    ),
                )

            from chronox_finance.engines.categorizer import normalize_merchant

            raw_goals = payload.get("goals") or payload.get("savings_goals") or []
            for idx_g, g in enumerate(raw_goals, start=1):
                conn.execute(
                    """
                    INSERT INTO goals (
                        id, name, target_cents, saved_cents, target_date, priority, priority_order,
                        category, funding_jar_slug, status, notes, deleted_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        g.get("id", idx_g),
                        g["name"],
                        g["target_cents"],
                        g.get("saved_cents", 0),
                        g.get("target_date"),
                        g.get("priority", 2),
                        g.get("priority_order", idx_g - 1),
                        g.get("category", "gadget"),
                        g.get("funding_jar_slug", "fun"),
                        g.get("status", "active"),
                        g.get("notes", ""),
                        g.get("deleted_at"),
                        g.get("created_at", now),
                        g.get("updated_at", now),
                    ),
                )

            for idx_tx, tx in enumerate(payload.get("transactions", []), start=1):
                merch = str(tx.get("merchant", ""))
                norm_merch = str(tx.get("normalized_merchant") or normalize_merchant(merch))
                conn.execute(
                    """
                    INSERT INTO transactions (
                        id, account_id, jar_id, tx_type, amount_cents, merchant, normalized_merchant,
                        subcategory, memo, tx_date, confidence, needs_review, source, fingerprint,
                        external_id, affects_balance, transfer_direction, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tx.get("id", idx_tx),
                        tx["account_id"],
                        tx.get("jar_id"),
                        tx["tx_type"],
                        tx["amount_cents"],
                        merch,
                        norm_merch,
                        tx.get("subcategory", ""),
                        tx.get("memo", ""),
                        tx["tx_date"],
                        tx.get("confidence", 1.0),
                        tx.get("needs_review", 0),
                        tx.get("source", "backup"),
                        tx.get("fingerprint"),
                        tx.get("external_id", ""),
                        tx.get("affects_balance", 1),
                        tx.get("transfer_direction", ""),
                        tx.get("created_at", now),
                    ),
                )

            for sp in payload.get("transaction_splits", []):
                conn.execute(
                    """
                    INSERT INTO transaction_splits (id, parent_tx_id, jar_id, goal_id, amount_cents, memo, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sp["id"],
                        sp["parent_tx_id"],
                        sp.get("jar_id"),
                        sp.get("goal_id"),
                        sp["amount_cents"],
                        sp.get("memo", ""),
                        sp.get("created_at", now),
                    ),
                )

            for tg in payload.get("tags", []):
                conn.execute(
                    "INSERT INTO tags (id, name, color, created_at) VALUES (?, ?, ?, ?)",
                    (tg["id"], tg["name"], tg.get("color", "#06b6d4"), tg.get("created_at", now)),
                )

            for tt in payload.get("transaction_tags", []):
                conn.execute(
                    "INSERT OR IGNORE INTO transaction_tags (tx_id, tag_id) VALUES (?, ?)",
                    (tt["tx_id"], tt["tag_id"]),
                )

            for cm in payload.get("transaction_comments", []):
                conn.execute(
                    "INSERT INTO transaction_comments (id, tx_id, author, body, created_at) VALUES (?, ?, ?, ?, ?)",
                    (cm["id"], cm["tx_id"], cm.get("author", "user"), cm["body"], cm.get("created_at", now)),
                )

            for rg in payload.get("rule_groups", []):
                conn.execute(
                    "INSERT INTO rule_groups (id, name, priority, active, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        rg["id"],
                        rg["name"],
                        rg.get("priority", 10),
                        int(bool(rg.get("active", True))),
                        rg.get("created_at", now),
                        rg.get("updated_at", now),
                    ),
                )

            for ar in payload.get("automation_rules", []):
                conn.execute(
                    """
                    INSERT INTO automation_rules (
                        id, group_id, name, priority, strict_and, stop_processing,
                        triggers_json, actions_json, active, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ar["id"],
                        ar.get("group_id"),
                        ar["name"],
                        ar.get("priority", 10),
                        int(bool(ar.get("strict_and", True))),
                        int(bool(ar.get("stop_processing", False))),
                        json.dumps(ar.get("triggers", [])),
                        json.dumps(ar.get("actions", [])),
                        int(bool(ar.get("active", True))),
                        ar.get("created_at", now),
                        ar.get("updated_at", now),
                    ),
                )

            for bill in payload.get("recurring_bills", []):
                conn.execute(
                    """
                    INSERT INTO recurring_bills (
                        id, name, normalized_merchant, amount_cents, previous_amount_cents,
                        amount_min_cents, amount_max_cents, due_window_days,
                        cadence, next_due_date, jar_id, is_subscription, is_essential,
                        auto_pay, active, confidence, flagged_zombie, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        bill["id"],
                        bill["name"],
                        bill["normalized_merchant"],
                        bill["amount_cents"],
                        bill.get("previous_amount_cents"),
                        bill.get("amount_min_cents"),
                        bill.get("amount_max_cents"),
                        bill.get("due_window_days", 4),
                        bill["cadence"],
                        bill["next_due_date"],
                        bill.get("jar_id"),
                        bill.get("is_subscription", 0),
                        bill.get("is_essential", 1),
                        bill.get("auto_pay", 0),
                        bill.get("active", 1),
                        bill.get("confidence", 0.9),
                        bill.get("flagged_zombie", 0),
                        bill.get("updated_at", now),
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
                        imp["id"],
                        imp["item_name"],
                        imp["price_cents"],
                        imp.get("reason", ""),
                        imp.get("url", ""),
                        imp.get("cooling_hours", 48),
                        imp["created_at"],
                        imp["unlocks_at"],
                        imp.get("status", "cooling"),
                        imp.get("decision_at"),
                    ),
                )

            for tr in payload.get("budget_transfers", []):
                conn.execute(
                    """
                    INSERT INTO budget_transfers (id, cycle_ym, from_jar_id, to_jar_id, amount_cents, reason, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tr["id"],
                        tr["cycle_ym"],
                        tr["from_jar_id"],
                        tr["to_jar_id"],
                        tr["amount_cents"],
                        tr.get("reason", ""),
                        tr.get("created_at", now),
                    ),
                )

            for cl in payload.get("credit_lines", []):
                conn.execute(
                    """
                    INSERT INTO credit_lines (
                        id, name, issuer, kind, balance_cents, credit_limit_cents, apr_bps,
                        min_payment_cents, statement_closing_day, payment_due_day,
                        promo_apr_bps, promo_ends_date, opened_date, has_grace_period,
                        autopay_mode, rewards_note, account_id, is_authorized_user,
                        source_env, external_account_id, active, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cl["id"],
                        cl["name"],
                        cl.get("issuer", ""),
                        cl.get("kind", "credit_card"),
                        cl["balance_cents"],
                        cl.get("credit_limit_cents", 0),
                        cl.get("apr_bps", 0),
                        cl.get("min_payment_cents", 0),
                        cl.get("statement_closing_day", 15),
                        cl.get("payment_due_day", 10),
                        cl.get("promo_apr_bps"),
                        cl.get("promo_ends_date"),
                        cl.get("opened_date"),
                        cl.get("has_grace_period", 1),
                        cl.get("autopay_mode", "statement"),
                        cl.get("rewards_note", ""),
                        cl.get("account_id"),
                        cl.get("is_authorized_user", 0),
                        str(cl.get("source_env") or "production"),
                        str(cl.get("external_account_id") or ""),
                        cl.get("active", 1),
                        cl.get("created_at", now),
                        cl.get("updated_at", now),
                    ),
                )

            for cr in payload.get("credit_reports", []):
                conn.execute(
                    """
                    INSERT INTO credit_reports (id, bureau, model, score, pulled_date, source, notes, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cr["id"],
                        cr["bureau"],
                        cr.get("model", "fico8"),
                        cr["score"],
                        cr["pulled_date"],
                        cr.get("source", "backup"),
                        cr.get("notes", ""),
                        cr.get("created_at", now),
                    ),
                )

            for idx_ci, ci in enumerate(payload.get("credit_inquiries", []), start=1):
                inq_date = str(ci["inquiry_date"])
                conn.execute(
                    """
                    INSERT INTO credit_inquiries (id, lender, bureau, inquiry_date, counts_until, drops_off, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ci.get("id", idx_ci),
                        ci["lender"],
                        ci.get("bureau", "experian"),
                        inq_date,
                        ci.get("counts_until") or _add_months_iso(inq_date, 12),
                        ci.get("drops_off") or _add_months_iso(inq_date, 24),
                        ci.get("created_at", now),
                    ),
                )

            for idx_cd, cd in enumerate(payload.get("credit_derogatories", []), start=1):
                cd_kind = str(cd["kind"])
                cd_opened = str(cd["opened_date"])
                conn.execute(
                    """
                    INSERT INTO credit_derogatories (id, kind, lender, opened_date, drops_off, active, notes, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cd.get("id", idx_cd),
                        cd_kind,
                        cd.get("lender", ""),
                        cd_opened,
                        cd.get("drops_off") or compute_derogatory_drop_off(cd_kind, cd_opened),
                        cd.get("active", 1),
                        cd.get("notes", ""),
                        cd.get("created_at", now),
                    ),
                )

            for h in payload.get("holdings", []):
                qty_e8 = int(h["quantity_e8"])
                price_e8 = int(h["price_e8"])
                cb_val = int(h.get("cost_basis_cents", 0) or 0)
                cb_known = int(bool(h.get("cost_basis_known", cb_val > 0)))
                conn.execute(
                    """
                    INSERT INTO holdings (
                        id, account_id, connection_id, external_account_id, symbol, name, asset_class,
                        quantity_e8, cost_basis_cents, cost_basis_known, price_e8, market_value_cents,
                        source, created_at, updated_at
                    ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        h["id"],
                        h.get("account_id"),
                        h.get("external_account_id", ""),
                        h["symbol"],
                        h.get("name", ""),
                        h.get("asset_class", "equity"),
                        qty_e8,
                        cb_val,
                        cb_known,
                        price_e8,
                        compute_holding_value_cents(qty_e8, price_e8),
                        h.get("source", "backup"),
                        h.get("created_at") or h.get("updated_at", now),
                        h.get("updated_at", now),
                    ),
                )

            for ps in payload.get("portfolio_snapshots", []):
                conn.execute(
                    """
                    INSERT INTO portfolio_snapshots (snap_date, total_value_cents, cost_basis_cents, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (ps["snap_date"], ps["total_value_cents"], ps["cost_basis_cents"], ps.get("updated_at", now)),
                )

            for nw in payload.get("net_worth_snapshots", []):
                conn.execute(
                    """
                    INSERT INTO net_worth_snapshots (
                        snap_date, liquid_cents, investment_cents, debt_cents, net_worth_cents, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        nw["snap_date"],
                        nw["liquid_cents"],
                        nw["investment_cents"],
                        nw["debt_cents"],
                        nw["net_worth_cents"],
                        nw.get("updated_at", now),
                    ),
                )

            for sc in payload.get("scenarios", []):
                conn.execute(
                    """
                    INSERT INTO scenarios (id, name, overrides_json, active, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sc["id"],
                        sc["name"],
                        json.dumps(sc.get("overrides", {})),
                        int(bool(sc.get("active", True))),
                        sc.get("created_at", now),
                        sc.get("updated_at", now),
                    ),
                )

            if "settings" in payload and isinstance(payload["settings"], dict):
                s = dict(payload["settings"])
                for secret_field in _SECRET_SETTINGS_FIELDS:
                    s.pop(secret_field, None)
                self.update_settings(s, ctx=ctx)

            if "adhd_progress" in payload and isinstance(payload["adhd_progress"], dict):
                self.update_adhd_progress(payload["adhd_progress"], ctx=ctx)
