"""
Storage & Auth Protocol Boundaries for ChronoX Finance.

Defines runtime-checkable typing Protocols (`FinanceStorageProtocol` & `AuthProvider`)
and `RequestContext(user_id="local")` so the local desktop SQLite + Cookie Vault
implementation can be swapped cleanly for Postgres + Bearer JWT in future SaaS / iOS deployments.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class RequestContext:
    """Tenant/request scope passed across service & storage boundaries."""

    user_id: str = "local"
    request_id: Optional[str] = None


LOCAL_CONTEXT = RequestContext(user_id="local")


@runtime_checkable
class AuthProvider(Protocol):
    """Authentication & CSRF boundary (LocalVaultAuth now -> BearerJWTAuth for SaaS/iOS)."""

    def vault_state(self) -> str: ...

    def is_authenticated(self, token: Optional[str]) -> bool: ...

    def issue_session_token(self, subject: str = "owner", ttl: int = ...) -> str: ...

    def get_csrf_token(self) -> str: ...

    def verify_csrf(self, token: Optional[str]) -> bool: ...


@runtime_checkable
class FinanceStorageProtocol(Protocol):
    """Persistence contract implemented by SQLite `FinanceStorage` (and future `PostgresStorage`)."""

    def close(self) -> None: ...

    def ping(self) -> bool: ...

    def get_schema_version(self) -> int: ...

    def transaction(self, immediate: bool = False) -> AbstractContextManager[Any]: ...

    # --- Settings ---
    def get_settings(self, ctx: RequestContext = LOCAL_CONTEXT) -> Dict[str, Any]: ...

    def update_settings(
        self, updates: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Dict[str, Any]: ...

    # --- Accounts ---
    def list_accounts(self, ctx: RequestContext = LOCAL_CONTEXT) -> List[Dict[str, Any]]: ...

    def get_account(
        self, account_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]: ...

    def get_default_account_id(self, ctx: RequestContext = LOCAL_CONTEXT) -> int: ...

    def create_account(
        self,
        name: str,
        account_type: str,
        balance_cents: int,
        institution: str = "",
        is_liquid: bool = True,
        ctx: RequestContext = LOCAL_CONTEXT,
    ) -> Dict[str, Any]: ...

    def adjust_account_balance(
        self, account_id: int, delta_cents: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> None: ...

    def set_account_balance(
        self, account_id: int, balance_cents: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]: ...

    # --- Jars ---
    def list_jars(self, ctx: RequestContext = LOCAL_CONTEXT) -> List[Dict[str, Any]]: ...

    def get_jar(
        self, jar_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]: ...

    def get_jar_by_slug(
        self, slug: str, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]: ...

    def update_jar(
        self, jar_id: int, updates: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]: ...

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
        adjust_balance: bool = True,
        ctx: RequestContext = LOCAL_CONTEXT,
    ) -> Optional[Dict[str, Any]]: ...

    def list_transactions(
        self,
        *,
        limit: int = 200,
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
    ) -> List[Dict[str, Any]]: ...

    def get_transaction(
        self, tx_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]: ...

    def update_transaction(
        self, tx_id: int, updates: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]: ...

    def delete_transaction(
        self, tx_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> bool: ...

    def set_transaction_splits(
        self,
        parent_tx_id: int,
        splits: List[Dict[str, Any]],
        ctx: RequestContext = LOCAL_CONTEXT,
    ) -> List[Dict[str, Any]]: ...

    def clear_transaction_splits(
        self, parent_tx_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> None: ...

    def get_transaction_splits(
        self, parent_tx_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> List[Dict[str, Any]]: ...

    def get_splits_for_transactions(
        self, tx_ids: List[int], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Dict[int, List[Dict[str, Any]]]: ...

    def list_tags(self, ctx: RequestContext = LOCAL_CONTEXT) -> List[Dict[str, Any]]: ...

    def set_transaction_tags(
        self, tx_id: int, tag_names: List[str], ctx: RequestContext = LOCAL_CONTEXT
    ) -> List[Dict[str, Any]]: ...

    def get_transaction_tags(
        self, tx_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> List[Dict[str, Any]]: ...

    def list_transaction_comments(
        self, tx_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> List[Dict[str, Any]]: ...

    def add_transaction_comment(
        self,
        tx_id: int,
        body: str,
        author: str = "user",
        ctx: RequestContext = LOCAL_CONTEXT,
    ) -> Dict[str, Any]: ...

    # --- Merchant & Firefly Automation Rules ---
    def get_merchant_rules(
        self, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Dict[str, Dict[str, Any]]: ...

    def upsert_merchant_rule(
        self,
        normalized_merchant: str,
        jar_id: int,
        subcategory: str = "",
        ctx: RequestContext = LOCAL_CONTEXT,
    ) -> None: ...

    def list_rule_groups(self, ctx: RequestContext = LOCAL_CONTEXT) -> List[Dict[str, Any]]: ...

    def create_rule_group(
        self, name: str, priority: int = 10, active: bool = True, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Dict[str, Any]: ...

    def list_rules(
        self, active_only: bool = False, ctx: RequestContext = LOCAL_CONTEXT
    ) -> List[Dict[str, Any]]: ...

    def get_rule(
        self, rule_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]: ...

    def create_rule(
        self, data: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Dict[str, Any]: ...

    def update_rule(
        self, rule_id: int, updates: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]: ...

    def delete_rule(self, rule_id: int, ctx: RequestContext = LOCAL_CONTEXT) -> bool: ...

    # --- Recurring Bills ---
    def list_recurring_bills(
        self, active_only: bool = True, ctx: RequestContext = LOCAL_CONTEXT
    ) -> List[Dict[str, Any]]: ...

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
    ) -> Dict[str, Any]: ...

    def deactivate_recurring_bill(
        self, bill_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> bool: ...

    def flag_zombie_bill(
        self, bill_id: int, flagged: bool = True, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]: ...

    # --- Connections & Email Scanner ---
    def list_connections(self, ctx: RequestContext = LOCAL_CONTEXT) -> List[Dict[str, Any]]: ...

    def get_connection(
        self, connection_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]: ...

    def upsert_connection(
        self, data: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Dict[str, Any]: ...

    def delete_connection(
        self, connection_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> bool: ...

    def list_email_queue(
        self, status: Optional[str] = None, limit: int = 100, ctx: RequestContext = LOCAL_CONTEXT
    ) -> List[Dict[str, Any]]: ...

    def enqueue_email_item(
        self, data: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]: ...

    def get_email_queue_item(
        self, item_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]: ...

    def update_email_queue_status(
        self,
        item_id: int,
        status: str,
        matched_bill_id: Optional[int] = None,
        ctx: RequestContext = LOCAL_CONTEXT,
    ) -> Optional[Dict[str, Any]]: ...

    # --- Scenarios & Net Worth Snapshots ---
    def list_scenarios(self, ctx: RequestContext = LOCAL_CONTEXT) -> List[Dict[str, Any]]: ...

    def get_scenario(
        self, scenario_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]: ...

    def create_scenario(
        self, data: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Dict[str, Any]: ...

    def update_scenario(
        self, scenario_id: int, updates: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]: ...

    def delete_scenario(
        self, scenario_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> bool: ...

    def upsert_net_worth_snapshot(
        self,
        snap_date: str,
        liquid_cents: int,
        investment_cents: int,
        debt_cents: int,
        net_worth_cents: int,
        ctx: RequestContext = LOCAL_CONTEXT,
    ) -> Dict[str, Any]: ...

    def list_net_worth_snapshots(
        self, start_date: Optional[str] = None, limit: int = 730, ctx: RequestContext = LOCAL_CONTEXT
    ) -> List[Dict[str, Any]]: ...

    # --- Credit Lines, Reports, Inquiries & Derogatories ---
    def list_credit_lines(
        self, active_only: bool = True, ctx: RequestContext = LOCAL_CONTEXT
    ) -> List[Dict[str, Any]]: ...

    def get_credit_line(
        self, line_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]: ...

    def create_credit_line(
        self, data: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Dict[str, Any]: ...

    def update_credit_line(
        self, line_id: int, updates: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]: ...

    def delete_credit_line(
        self, line_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> bool: ...

    def list_credit_reports(self, ctx: RequestContext = LOCAL_CONTEXT) -> List[Dict[str, Any]]: ...

    def create_credit_report(
        self, data: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Dict[str, Any]: ...

    def delete_credit_report(
        self, report_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> bool: ...

    def list_credit_inquiries(
        self, ctx: RequestContext = LOCAL_CONTEXT
    ) -> List[Dict[str, Any]]: ...

    def create_credit_inquiry(
        self, data: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Dict[str, Any]: ...

    def delete_credit_inquiry(
        self, inquiry_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> bool: ...

    def list_credit_derogatories(
        self, active_only: bool = False, ctx: RequestContext = LOCAL_CONTEXT
    ) -> List[Dict[str, Any]]: ...

    def create_credit_derogatory(
        self, data: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Dict[str, Any]: ...

    def delete_credit_derogatory(
        self, derog_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> bool: ...

    # --- Portfolio Holdings & Snapshots ---
    def list_holdings(self, ctx: RequestContext = LOCAL_CONTEXT) -> List[Dict[str, Any]]: ...

    def get_holding(
        self, holding_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]: ...

    def upsert_holding(
        self, data: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Dict[str, Any]: ...

    def delete_holding(
        self, holding_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> bool: ...

    def upsert_portfolio_snapshot(
        self,
        snap_date: str,
        total_value_cents: int,
        cost_basis_cents: int,
        ctx: RequestContext = LOCAL_CONTEXT,
    ) -> Dict[str, Any]: ...

    def list_portfolio_snapshots(
        self, limit: int = 730, ctx: RequestContext = LOCAL_CONTEXT
    ) -> List[Dict[str, Any]]: ...

    # --- Goals (via `goals_active` view + soft-delete/restore/purge) ---
    def list_goals(
        self, include_completed: bool = False, ctx: RequestContext = LOCAL_CONTEXT
    ) -> List[Dict[str, Any]]: ...

    def get_goal(
        self,
        goal_id: int,
        include_deleted: bool = False,
        ctx: RequestContext = LOCAL_CONTEXT,
    ) -> Optional[Dict[str, Any]]: ...

    def create_goal(
        self, data: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Dict[str, Any]: ...

    def update_goal(
        self, goal_id: int, updates: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]: ...

    def reorder_goals(
        self, ordered_ids: List[int], ctx: RequestContext = LOCAL_CONTEXT
    ) -> List[Dict[str, Any]]: ...

    def soft_delete_goal(
        self, goal_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]: ...

    def restore_goal(
        self, goal_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]: ...

    def purge_deleted_goals(
        self, older_than_hours: int = 24, ctx: RequestContext = LOCAL_CONTEXT
    ) -> int: ...

    def delete_goal(self, goal_id: int, ctx: RequestContext = LOCAL_CONTEXT) -> bool: ...

    # --- AI Usage, Threads & Agent Batches ---
    def count_ai_requests_today(self, ctx: RequestContext = LOCAL_CONTEXT) -> int: ...

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
    ) -> None: ...

    def get_ai_usage_summary(
        self, days: int = 30, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Dict[str, Any]: ...

    def list_ai_threads(self, ctx: RequestContext = LOCAL_CONTEXT) -> List[Dict[str, Any]]: ...

    def upsert_ai_thread(
        self, data: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Dict[str, Any]: ...

    def delete_ai_thread(self, thread_id: str, ctx: RequestContext = LOCAL_CONTEXT) -> bool: ...

    def create_agent_batch(
        self, data: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Dict[str, Any]: ...

    def get_agent_batch(
        self, batch_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]: ...

    def update_agent_batch(
        self, batch_id: int, updates: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]: ...

    def list_agent_batches(
        self, limit: int = 50, ctx: RequestContext = LOCAL_CONTEXT
    ) -> List[Dict[str, Any]]: ...

    # --- Impulse Vault, Transfers, Audit, ADHD Progress, Backup ---
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
    ) -> Dict[str, Any]: ...

    def list_impulses(
        self, include_resolved: bool = False, ctx: RequestContext = LOCAL_CONTEXT
    ) -> List[Dict[str, Any]]: ...

    def get_impulse(
        self, impulse_id: int, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]: ...

    def resolve_impulse(
        self, impulse_id: int, status: str, ctx: RequestContext = LOCAL_CONTEXT
    ) -> Optional[Dict[str, Any]]: ...

    def record_budget_transfer(
        self,
        cycle_ym: str,
        from_jar_id: int,
        to_jar_id: int,
        amount_cents: int,
        reason: str,
        ctx: RequestContext = LOCAL_CONTEXT,
    ) -> Dict[str, Any]: ...

    def list_budget_transfers(
        self, cycle_ym: str, ctx: RequestContext = LOCAL_CONTEXT
    ) -> List[Dict[str, Any]]: ...

    def log_budget_event(
        self,
        event_type: str,
        summary: str,
        details: Optional[Dict[str, Any]] = None,
        ctx: RequestContext = LOCAL_CONTEXT,
    ) -> None: ...

    def list_budget_events(
        self, limit: int = 25, ctx: RequestContext = LOCAL_CONTEXT
    ) -> List[Dict[str, Any]]: ...

    def get_adhd_progress(self, ctx: RequestContext = LOCAL_CONTEXT) -> Dict[str, Any]: ...

    def update_adhd_progress(
        self, updates: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> Dict[str, Any]: ...

    def clear_all_financial_data(self, ctx: RequestContext = LOCAL_CONTEXT) -> None: ...

    def export_backup(self, ctx: RequestContext = LOCAL_CONTEXT) -> Dict[str, Any]: ...

    def import_backup(
        self, payload: Dict[str, Any], ctx: RequestContext = LOCAL_CONTEXT
    ) -> None: ...
