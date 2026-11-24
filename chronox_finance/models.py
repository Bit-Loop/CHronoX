"""
Strictly typed Pydantic v2 models for ChronoX Finance.

All monetary values are stored and computed in exact integer cents (`int`),
and investment quantities/prices use exact integer `10^-8` units (`quantity_e8`,
`price_e8`) with `ROUND_HALF_EVEN` valuation to eliminate IEEE-754 floating-point drift.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --- Precision & Timestamp Helpers ---

E8_SCALE = 100_000_000  # 10^8 units (satoshis / fractional shares / sub-cent USD prices)
_E14_DIVISOR = Decimal(10**14)  # (quantity_e8 * price_e8) / 10^14 == exact USD cents
_E8_DECIMAL = Decimal(E8_SCALE)


def utc_now_iso() -> str:
    """Return ISO-8601 UTC timestamp string."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def decimal_to_e8(value: Union[str, int, Decimal, float]) -> int:
    """Convert a decimal quantity or USD price into exact integer 10^-8 units (ROUND_HALF_EVEN)."""
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return int((d * _E8_DECIMAL).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))


def price_cents_to_e8(price_cents: int) -> int:
    """Convert integer USD cents into `price_e8` (1 cent == 1,000,000 e8 units)."""
    return int(price_cents) * 1_000_000


def compute_holding_value_cents(quantity_e8: int, price_e8: int) -> int:
    """
    Compute exact USD cents value for a holding given `quantity_e8` (10^-8 units)
    and `price_e8` (10^-8 USD per unit) using banker's rounding (`ROUND_HALF_EVEN`).

    Handles both high-value assets (BTC) and sub-cent crypto (e.g. SHIB @ $0.00002415)
    with zero IEEE-754 float drift.
    """
    if quantity_e8 == 0 or price_e8 == 0:
        return 0
    exact_cents = (Decimal(int(quantity_e8)) * Decimal(int(price_e8))) / _E14_DIVISOR
    return int(exact_cents.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))


# --- Core Enums & Type Literals ---


class AccountType(str, Enum):
    CHECKING = "checking"
    SAVINGS = "savings"
    CREDIT = "credit"
    CASH = "cash"
    TRADING = "trading"


class TransactionType(str, Enum):
    EXPENSE = "expense"
    INCOME = "income"
    TRANSFER = "transfer"


class JarKind(str, Enum):
    BILLS = "bills"           # Protected necessities (rent, utilities, insurance, minimums)
    DAILY = "daily"           # Flexible necessities (groceries, gas, transit, pharmacy)
    FUN = "fun"               # Guilt-free dopamine budget (coffee, dining, hobbies)
    SAFETY = "safety"         # Emergency buffer, sinking funds, debt payoff
    INVEST = "invest"         # ChronoX Trading / Investing Vault (strict firewall)


class JarHealth(str, Enum):
    CALM = "calm"             # Healthy pacing
    WATCH = "watch"           # Spending slightly faster than cycle elapsed
    REBALANCE = "rebalance"   # Needs gentle 1-click rebalance (never punitive!)


class RecurrenceCadence(str, Enum):
    WEEKLY = "weekly"
    BIWEEKLY = "biweekly"
    SEMIMONTHLY = "semimonthly"
    MONTHLY = "monthly"
    BIMONTHLY = "bimonthly"
    QUARTERLY = "quarterly"
    SEMIANNUAL = "semiannual"
    ANNUAL = "annual"


class ImpulseStatus(str, Enum):
    COOLING = "cooling"       # Inside cooling-off timer
    READY = "ready"           # Timer elapsed — ready for clear-headed decision
    SKIPPED = "skipped"       # User resisted! Dopamine win + money saved
    BOUGHT = "bought"         # User intentionally decided to buy after cooling off


class DebtKind(str, Enum):
    CREDIT_CARD = "credit_card"
    AUTO_LOAN = "auto_loan"
    STUDENT_LOAN = "student_loan"
    PERSONAL_LOAN = "personal_loan"
    MEDICAL = "medical"
    BNPL = "bnpl"
    MORTGAGE = "mortgage"


class AutopayMode(str, Enum):
    NONE = "none"
    MINIMUM = "minimum"
    STATEMENT = "statement"
    FULL = "full"


class GoalCategory(str, Enum):
    GADGET = "gadget"
    EMERGENCY = "emergency"
    TRAVEL = "travel"
    DEBT_FREEDOM = "debt_freedom"
    INVEST = "invest"
    CUSTOM = "custom"


class IntelligenceMode(str, Enum):
    DETERMINISTIC_ONLY = "deterministic_only"
    HYBRID = "hybrid"
    AI_STANDALONE = "ai_standalone"


class AIProvider(str, Enum):
    AUTO = "auto"
    OPENCODE_GO = "opencode_go"
    GEMINI = "gemini"
    OPENAI_CODEX = "openai_codex"
    CHAT_WRAPPER = "chat_wrapper"
    CODEX_CLI = "codex_cli"


ActiveProviderResolved = Literal[
    "opencode_go",
    "gemini",
    "openai_codex",
    "chat_wrapper",
    "codex_cli",
    "none",
]

SuggestionSource = Literal[
    "opencode_go",
    "gemini",
    "openai_codex",
    "chat_wrapper",
    "codex_cli",
    "deterministic_local",
    "deterministic_quant",
    "ai_standalone",
]

VaultState = Literal["needs_setup", "locked", "unlocked"]
CreditAPIVendor = Literal["spinwheel", "array", "crs", "custom"]
ConnectorProvider = Literal[
    "plaid",
    "simplefin",
    "coinbase",
    "schwab",
    "robinhood",
    "credit_api_spinwheel",
    "credit_api_array",
    "credit_api_crs",
    "credit_api_custom",
]
AssetClass = Literal["equity", "etf", "crypto", "cash"]
CreditBureau = Literal["experian", "equifax", "transunion", "other"]
CreditScoreModel = Literal["fico8", "vantage3", "fico9", "other"]
DerogatoryKind = Literal[
    "late_30",
    "late_60",
    "late_90",
    "charge_off",
    "collection",
    "bankruptcy_ch7",
    "bankruptcy_ch13",
    "other",
]

# Strictly allowlisted Agentic Coach actions (12 safe types; NO delete_credit_line,
# NO delete_rule, and NO money-movement actions per Gate 1 security review).
AgentActionType = Literal[
    "create_transaction",
    "update_transaction",
    "add_tx_comment",
    "split_transaction",
    "create_goal",
    "update_goal",
    "delete_goal",
    "create_rule",
    "update_rule",
    "create_credit_line",
    "update_credit_line",
    "analyze_portfolio",
]


class SuggestionActionType(str, Enum):
    REBALANCE_JARS = "rebalance_jars"
    ADJUST_JAR_TARGET = "adjust_jar_target"
    REVIEW_SUBSCRIPTION = "review_subscription"
    BOOST_EMERGENCY_BUFFER = "boost_emergency_buffer"
    SET_TRADING_CAP = "set_trading_cap"
    WEEKLY_RESET = "weekly_reset"
    PARK_IMPULSE = "park_impulse"
    CREDIT_PAYDOWN = "credit_paydown"
    ENABLE_AUTOPAY = "enable_autopay"
    FUND_GOAL = "fund_goal"
    GENERAL_NUDGE = "general_nudge"


# --- Vault & First-Run Onboarding Models ---


class VaultStatus(BaseModel):
    state: VaultState
    initialized: bool
    unlocked: bool
    auth_enabled: bool = True
    authenticated: bool = False
    onboarding_completed: bool = False
    auto_lock_minutes: int = 30
    csrf_token: Optional[str] = None
    recovery_configured: bool = False
    db_encrypted_at_rest: bool = False


class VaultSetupRequest(BaseModel):
    password: str = Field(..., min_length=8, max_length=128)


class VaultSetupResponse(BaseModel):
    status: VaultStatus
    recovery_key: str
    recovery_groups: List[str] = Field(default_factory=list)
    challenge_indices: List[int] = Field(default_factory=list)
    csrf_token: str
    migrated_legacy_secrets: bool = False


class VaultUnlockRequest(BaseModel):
    password: str = Field(..., min_length=1, max_length=128)


class VaultRecoverRequest(BaseModel):
    recovery_key: str = Field(..., min_length=16, max_length=128)
    new_password: str = Field(..., min_length=8, max_length=128)
    rotate_recovery_key: bool = False


class VaultRecoverResponse(BaseModel):
    status: VaultStatus
    csrf_token: str
    new_recovery_key: Optional[str] = None


class VaultChangePasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=8, max_length=128)


class OnboardingCompleteRequest(BaseModel):
    track: Literal["plaid", "simplefin", "local", "skipped"] = "skipped"


class OnboardingStatus(BaseModel):
    onboarding_completed: bool
    track: str = ""


# --- Core Domain Entities ---


class AccountCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    account_type: AccountType = AccountType.CHECKING
    balance_cents: int = Field(0, ge=-100_000_000_00, le=100_000_000_00)
    institution: str = Field("", max_length=80)
    is_liquid: bool = True


class AccountOut(AccountCreate):
    id: int
    created_at: str
    updated_at: str


class JarUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=60)
    icon: Optional[str] = Field(None, max_length=24)
    color: Optional[str] = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")
    target_pct: Optional[float] = Field(None, ge=0.0, le=100.0)
    monthly_target_cents: Optional[int] = Field(None, ge=0, le=100_000_000_00)
    rollover_enabled: Optional[bool] = None
    rollover_cap_cents: Optional[int] = Field(None, ge=0, le=100_000_000_00)
    is_locked: Optional[bool] = None


class JarState(BaseModel):
    id: int
    slug: str
    name: str
    kind: JarKind
    icon: str
    color: str
    priority: int
    target_pct: float
    monthly_target_cents: int
    auto_allocated_cents: int
    rollover_cents: int
    spent_cents: int
    available_cents: int
    pct_used: float
    pace_delta_pct: float
    health: JarHealth
    is_locked: bool
    rollover_enabled: bool
    rollover_cap_cents: Optional[int] = None
    calm_note: str


# --- Splits, Tags & Comments Models ---


class TransactionSplitItem(BaseModel):
    jar_id: Optional[int] = None
    goal_id: Optional[int] = None
    amount_cents: int = Field(..., gt=0, le=100_000_000_00)
    memo: str = Field("", max_length=240)


class TransactionSplitOut(TransactionSplitItem):
    id: int
    parent_tx_id: int
    jar_name: str = ""


class TransactionSplitsRequest(BaseModel):
    splits: List[TransactionSplitItem] = Field(..., min_length=1, max_length=20)


class TagCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=40)
    color: str = Field("#06b6d4", pattern=r"^#[0-9a-fA-F]{6}$")


class TagOut(TagCreate):
    id: int


class TransactionTagsRequest(BaseModel):
    tags: List[str] = Field(default_factory=list, max_length=20)


class TransactionCommentCreate(BaseModel):
    body: str = Field(..., min_length=1, max_length=1000)
    author: Literal["user", "ai_coach"] = "user"


class TransactionCommentOut(BaseModel):
    id: int
    tx_id: int
    author: Literal["user", "ai_coach"]
    body: str
    created_at: str


class TransactionCreate(BaseModel):
    account_id: Optional[int] = None
    jar_id: Optional[int] = None
    tx_type: TransactionType = TransactionType.EXPENSE
    amount_cents: int = Field(..., gt=0, le=100_000_000_00)
    merchant: str = Field(..., min_length=1, max_length=120)
    subcategory: str = Field("", max_length=60)
    memo: str = Field("", max_length=300)
    tx_date: str = Field(default_factory=lambda: date.today().isoformat())
    source: str = Field("manual", max_length=40)

    @field_validator("tx_date")
    @classmethod
    def validate_iso_date(cls, v: str) -> str:
        date.fromisoformat(v)
        return v


class QuickAddRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=240)
    account_id: Optional[int] = None


class TransactionUpdate(BaseModel):
    jar_id: Optional[int] = None
    amount_cents: Optional[int] = Field(None, gt=0, le=100_000_000_00)
    tx_type: Optional[TransactionType] = None
    tx_date: Optional[str] = None
    merchant: Optional[str] = Field(None, min_length=1, max_length=120)
    subcategory: Optional[str] = Field(None, max_length=60)
    memo: Optional[str] = Field(None, max_length=300)
    learn_merchant_rule: bool = True

    @field_validator("tx_date")
    @classmethod
    def validate_opt_tx_date(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            date.fromisoformat(v)
        return v


class TransactionOut(BaseModel):
    id: int
    account_id: int
    jar_id: Optional[int]
    jar_name: str = ""
    jar_icon: str = ""
    tx_type: TransactionType
    amount_cents: int
    merchant: str
    normalized_merchant: str
    subcategory: str
    memo: str
    tx_date: str
    confidence: float
    needs_review: bool
    source: str
    created_at: str
    splits: List[TransactionSplitOut] = Field(default_factory=list)
    tags: List[TagOut] = Field(default_factory=list)
    comment_count: int = 0


class RecurringBillCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    amount_cents: int = Field(..., gt=0, le=100_000_000_00)
    amount_min_cents: Optional[int] = Field(None, ge=0, le=100_000_000_00)
    amount_max_cents: Optional[int] = Field(None, ge=0, le=100_000_000_00)
    due_window_days: int = Field(4, ge=0, le=30)
    cadence: RecurrenceCadence = RecurrenceCadence.MONTHLY
    next_due_date: str
    jar_id: Optional[int] = None
    is_subscription: bool = False
    is_essential: bool = True
    auto_pay: bool = False

    @field_validator("next_due_date")
    @classmethod
    def validate_due_date(cls, v: str) -> str:
        date.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def validate_amount_range(self) -> "RecurringBillCreate":
        if (
            self.amount_min_cents is not None
            and self.amount_max_cents is not None
            and self.amount_min_cents > self.amount_max_cents
        ):
            raise ValueError("amount_min_cents cannot exceed amount_max_cents")
        return self


class RecurringBillOut(BaseModel):
    id: int
    name: str
    normalized_merchant: str
    amount_cents: int
    amount_min_cents: Optional[int] = None
    amount_max_cents: Optional[int] = None
    due_window_days: int = 4
    previous_amount_cents: Optional[int] = None
    price_hike_cents: int = 0
    cadence: RecurrenceCadence
    next_due_date: str
    days_until_due: int
    jar_id: Optional[int]
    is_subscription: bool
    is_essential: bool
    auto_pay: bool
    active: bool
    confidence: float
    flagged_zombie: bool = False


class ImpulseCreate(BaseModel):
    item_name: str = Field(..., min_length=1, max_length=120)
    price_cents: int = Field(..., gt=0, le=100_000_000_00)
    reason: str = Field("", max_length=240)
    url: str = Field("", max_length=400)
    cooling_hours: int = Field(48, ge=1, le=720)


class ImpulseOut(BaseModel):
    id: int
    item_name: str
    price_cents: int
    reason: str
    url: str
    cooling_hours: int
    created_at: str
    unlocks_at: str
    hours_remaining: float
    status: ImpulseStatus
    work_hours_cost: float
    goal_delay_days: int
    can_afford_safely: bool


class ImpulseDecision(BaseModel):
    decision: Literal["skip", "buy"]
    sweep_savings_pct: int = Field(50, ge=0, le=100)
    account_id: Optional[int] = None


class RebalanceTransferRequest(BaseModel):
    from_jar_id: int
    to_jar_id: int
    amount_cents: int = Field(..., gt=0, le=100_000_000_00)
    reason: str = Field("Manual jar rebalance", max_length=200)


class RebalancePlan(BaseModel):
    from_jar_id: int
    from_jar_name: str
    to_jar_id: int
    to_jar_name: str
    amount_cents: int
    calm_message: str


class SafeToSpendBreakdown(BaseModel):
    total_liquid_cash_cents: int
    unpaid_bills_until_payday_cents: int
    locked_jar_reserves_cents: int
    emergency_buffer_cents: int
    credit_card_reserved_cents: int
    safe_to_spend_cents: int
    days_until_payday: int
    daily_safe_to_spend_cents: int
    next_payday_date: str
    estimated_paycheck_cents: int
    cycle_progress_pct: float


class BudgetAuditEvent(BaseModel):
    id: int
    event_type: str
    summary: str
    details: Dict[str, Any] = Field(default_factory=dict)
    created_at: str


class ADHDStats(BaseModel):
    weekly_reset_streak: int
    grace_days_available: int
    last_reset_date: Optional[str]
    impulses_skipped_count: int
    impulse_money_saved_cents: int
    dopamine_xp: int
    level: int
    level_title: str
    recent_wins: List[str]


class ValidatedSuggestion(BaseModel):
    id: str
    title: str
    rationale: str
    micro_step: str
    priority: Literal["high", "medium", "low"] = "medium"
    action_type: SuggestionActionType = SuggestionActionType.GENERAL_NUDGE
    action_payload: Dict[str, Any] = Field(default_factory=dict)
    impact_cents: int = 0
    verified: bool = True
    verification_notes: List[str] = Field(default_factory=list)
    source: SuggestionSource = "deterministic_local"
    model_used: Optional[str] = None


class SuggestionBatchResponse(BaseModel):
    generated_at: str
    source: SuggestionSource
    model_used: Optional[str] = None
    intelligence_mode: str = IntelligenceMode.HYBRID.value
    provider_used: Optional[str] = None
    cached: bool = False
    validation_passed: bool = True
    rejected_count: int = 0
    suggestions: List[ValidatedSuggestion]
    notice: Optional[str] = None


class CoachQuestionRequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=500)


class CoachAnswerResponse(BaseModel):
    answer: str
    source: SuggestionSource
    model_used: Optional[str] = None
    intelligence_mode: str = IntelligenceMode.HYBRID.value
    provider_used: Optional[str] = None
    verified_numbers: Dict[str, Any] = Field(default_factory=dict)
    affordability_check: Optional[Dict[str, Any]] = None


class WeeklyResetSummary(BaseModel):
    needs_triage_transactions: List[TransactionOut]
    upcoming_bills_14d: List[RecurringBillOut]
    zombie_subscriptions: List[RecurringBillOut]
    matured_impulses: List[ImpulseOut]
    rebalance_plans: List[RebalancePlan]
    adaptive_target_updates: List[Dict[str, Any]]
    already_completed_this_week: bool


# --- AI Settings, Usage Telemetry & Agentic Coach Models ---


class AISettingsOut(BaseModel):
    intelligence_mode: IntelligenceMode = IntelligenceMode.HYBRID
    ai_provider: AIProvider = AIProvider.AUTO
    active_provider_resolved: ActiveProviderResolved = "none"
    opencode_base_url: str = "https://opencode.ai/zen/go/v1"
    opencode_model: str = "glm-5.3-flash"
    opencode_protocol: Literal["auto", "openai", "anthropic"] = "auto"
    opencode_max_daily_requests: int = Field(60, ge=1, le=500)
    opencode_key_configured: bool = False
    opencode_key_masked: str = ""
    opencode_key_source: Literal["saved", "env", "none"] = "none"
    gemini_api_key_set: bool = False
    gemini_key_masked: str = ""
    gemini_key_source: Literal["saved", "env", "none"] = "none"
    gemini_model: str = "gemini-2.5-flash"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    codex_base_url: str = "https://api.openai.com/v1"
    codex_model: str = "gpt-4.1-mini"
    codex_key_configured: bool = False
    codex_key_masked: str = ""
    codex_key_source: Literal["saved", "env", "none"] = "none"
    wrapper_base_url: str = ""
    wrapper_model: str = ""
    wrapper_wire_api: Literal["responses", "chat_completions"] = "chat_completions"
    wrapper_key_configured: bool = False
    wrapper_key_masked: str = ""
    codex_cli_enabled: bool = False
    codex_cli_model: str = ""
    requests_used_today: int = 0
    quota_remaining_today: int = 60


class AISettingsUpdate(BaseModel):
    intelligence_mode: Optional[IntelligenceMode] = None
    ai_provider: Optional[AIProvider] = None
    opencode_base_url: Optional[str] = None
    opencode_model: Optional[str] = None
    opencode_protocol: Optional[Literal["auto", "openai", "anthropic"]] = None
    opencode_max_daily_requests: Optional[int] = Field(None, ge=1, le=500)
    opencode_api_key: Optional[str] = None  # Write-only; never returned plaintext
    clear_opencode_key: bool = False
    gemini_api_key: Optional[str] = None  # Write-only; never returned plaintext
    clear_gemini_key: bool = False
    gemini_model: Optional[str] = None
    gemini_base_url: Optional[str] = None
    codex_base_url: Optional[str] = None
    codex_model: Optional[str] = None
    codex_api_key: Optional[str] = None  # Write-only; never returned plaintext
    clear_codex_key: bool = False
    wrapper_base_url: Optional[str] = None
    wrapper_model: Optional[str] = None
    wrapper_wire_api: Optional[Literal["responses", "chat_completions"]] = None
    wrapper_api_key: Optional[str] = None  # Write-only; never returned plaintext
    clear_wrapper_key: bool = False
    codex_cli_enabled: Optional[bool] = None
    codex_cli_model: Optional[str] = None


class AIUsageProviderBreakdown(BaseModel):
    provider: str
    model: str
    requests: int = 0
    cached_responses: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_hit_rate_pct: float = 0.0
    cost_microdollars: int = 0
    saved_microdollars: int = 0
    avg_latency_ms: int = 0


class AIUsageReport(BaseModel):
    days: int = 30
    requests_today: int = 0
    daily_quota_limit: int = 60
    quota_remaining_today: int = 60
    total_requests: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cached_tokens: int = 0
    cache_hit_rate_pct: float = 0.0
    total_cost_microdollars: int = 0
    total_saved_microdollars: int = 0
    by_provider: List[AIUsageProviderBreakdown] = Field(default_factory=list)


class AgentPlanRequest(BaseModel):
    prompt: str = Field(..., min_length=2, max_length=1500)
    provider: Optional[AIProvider] = None


class AgentActionItem(BaseModel):
    index: int = 0
    action_type: AgentActionType
    summary: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    valid: bool = True
    warnings: List[str] = Field(default_factory=list)
    rejected_reason: Optional[str] = None


class AgentPlanResponse(BaseModel):
    batch_id: int
    prompt: str
    provider: str
    model_used: Optional[str] = None
    status: Literal["preview", "executed", "undone"] = "preview"
    explanation: str = ""
    actions: List[AgentActionItem] = Field(default_factory=list)
    valid_count: int = 0
    rejected_count: int = 0
    created_at: str = ""


class AgentExecuteResponse(BaseModel):
    batch_id: int
    status: Literal["executed"] = "executed"
    applied_count: int = 0
    results: List[Dict[str, Any]] = Field(default_factory=list)
    undo_available: bool = True


class AgentUndoResponse(BaseModel):
    batch_id: int
    status: Literal["undone", "conflict"]
    restored_count: int = 0
    conflicts: List[Dict[str, Any]] = Field(default_factory=list)
    message: str = ""


class ProfileSettings(BaseModel):
    currency: str = "USD"
    hourly_wage_cents: int = Field(3000, ge=100, le=500_000)
    emergency_buffer_cents: int = Field(50_000, ge=0, le=50_000_000)
    default_cooling_hours: int = Field(48, ge=1, le=720)
    payday_cadence_days: int = Field(14, ge=7, le=31)
    next_payday_date: Optional[str] = None
    expected_paycheck_cents: int = Field(220_000, ge=0, le=50_000_000)
    calm_mode: bool = True
    auto_rebalance_enabled: bool = True
    onboarding_completed: bool = False
    vault_auto_lock_minutes: int = Field(30, ge=0, le=1440)
    email_scanner_enabled: bool = False
    intelligence_mode: IntelligenceMode = IntelligenceMode.HYBRID
    ai_provider: AIProvider = AIProvider.AUTO
    active_provider_resolved: ActiveProviderResolved = "none"
    opencode_base_url: str = "https://opencode.ai/zen/go/v1"
    opencode_model: str = "glm-5.3-flash"
    opencode_protocol: Literal["auto", "openai", "anthropic"] = "auto"
    opencode_max_daily_requests: int = Field(60, ge=1, le=500)
    opencode_key_configured: bool = False
    opencode_key_masked: str = ""
    opencode_key_source: Literal["saved", "env", "none"] = "none"
    gemini_api_key_set: bool = False
    gemini_key_masked: str = ""
    gemini_key_source: Literal["saved", "env", "none"] = "none"
    gemini_model: str = "gemini-2.5-flash"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    codex_base_url: str = "https://api.openai.com/v1"
    codex_model: str = "gpt-4.1-mini"
    codex_key_configured: bool = False
    codex_key_masked: str = ""
    codex_key_source: Literal["saved", "env", "none"] = "none"
    wrapper_base_url: str = ""
    wrapper_model: str = ""
    wrapper_wire_api: Literal["responses", "chat_completions"] = "chat_completions"
    wrapper_key_configured: bool = False
    wrapper_key_masked: str = ""
    codex_cli_enabled: bool = False
    codex_cli_model: str = ""
    auth_enabled: bool = False


class ProfileSettingsUpdate(BaseModel):
    currency: Optional[str] = Field(None, min_length=3, max_length=5)
    hourly_wage_cents: Optional[int] = Field(None, ge=100, le=500_000)
    emergency_buffer_cents: Optional[int] = Field(None, ge=0, le=50_000_000)
    default_cooling_hours: Optional[int] = Field(None, ge=1, le=720)
    payday_cadence_days: Optional[int] = Field(None, ge=7, le=31)
    next_payday_date: Optional[str] = None
    expected_paycheck_cents: Optional[int] = Field(None, ge=0, le=50_000_000)
    calm_mode: Optional[bool] = None
    auto_rebalance_enabled: Optional[bool] = None
    onboarding_completed: Optional[bool] = None
    vault_auto_lock_minutes: Optional[int] = Field(None, ge=0, le=1440)
    email_scanner_enabled: Optional[bool] = None
    intelligence_mode: Optional[IntelligenceMode] = None
    ai_provider: Optional[AIProvider] = None
    opencode_base_url: Optional[str] = None
    opencode_model: Optional[str] = None
    opencode_protocol: Optional[Literal["auto", "openai", "anthropic"]] = None
    opencode_max_daily_requests: Optional[int] = Field(None, ge=1, le=500)
    opencode_api_key: Optional[str] = None  # Write-only; never returned plaintext
    clear_opencode_key: bool = False
    gemini_api_key: Optional[str] = None  # Write-only; never returned plaintext
    clear_gemini_key: bool = False
    gemini_model: Optional[str] = None
    gemini_base_url: Optional[str] = None
    codex_base_url: Optional[str] = None
    codex_model: Optional[str] = None
    codex_api_key: Optional[str] = None  # Write-only; never returned plaintext
    clear_codex_key: bool = False
    wrapper_base_url: Optional[str] = None
    wrapper_model: Optional[str] = None
    wrapper_wire_api: Optional[Literal["responses", "chat_completions"]] = None
    wrapper_api_key: Optional[str] = None  # Write-only; never returned plaintext
    clear_wrapper_key: bool = False
    codex_cli_enabled: Optional[bool] = None
    codex_cli_model: Optional[str] = None


class BillAnomaly(BaseModel):
    kind: Literal["double_charge", "missed_bill", "price_hike"]
    merchant: str
    amount_cents: int
    summary: str
    dates: List[str] = Field(default_factory=list)


# --- Banking, Broker & Email Connector Models ---


class ConnectionOut(BaseModel):
    id: int
    provider: ConnectorProvider
    item_id: str = ""
    institution: str = ""
    env: str = "production"
    status: Literal["active", "needs_reauth", "relink_required", "error", "paused_locked"] = "active"
    token_expires_at: Optional[str] = None
    refresh_days_remaining: Optional[int] = None
    last_synced_at: Optional[str] = None
    history_days_requested: int = 730
    needs_relink_for_730d: bool = False
    created_at: str = ""
    updated_at: str = ""


class PlaidConfigRequest(BaseModel):
    client_id: str = Field(..., min_length=4, max_length=128)
    secret: str = Field(..., min_length=4, max_length=256)
    env: Literal["sandbox", "production"] = "sandbox"


class PlaidLinkTokenRequest(BaseModel):
    products: List[str] = Field(default_factory=lambda: ["transactions"])
    optional_products: List[str] = Field(default_factory=lambda: ["liabilities", "investments"])
    days_requested: int = Field(730, ge=30, le=730)


class PlaidLinkTokenResponse(BaseModel):
    link_token: str
    hosted_link_url: Optional[str] = None
    expiration: Optional[str] = None
    days_requested: int = 730


class PlaidPollLinkRequest(BaseModel):
    link_token: str = Field(..., min_length=4, max_length=512)


class PlaidExchangeRequest(BaseModel):
    public_token: str = Field(..., min_length=4, max_length=512)
    institution: str = Field("", max_length=120)


class SimpleFINClaimRequest(BaseModel):
    setup_token: str = Field(..., min_length=8, max_length=2048)
    institution: str = Field("SimpleFIN Bridge", max_length=120)


class BrokerCoinbaseConnectRequest(BaseModel):
    api_key_name: str = Field(..., min_length=4, max_length=256)
    private_key_pem: str = Field(..., min_length=32, max_length=4096)
    label: str = Field("Coinbase", max_length=100)


class BrokerRobinhoodConnectRequest(BaseModel):
    api_key: str = Field(..., min_length=8, max_length=256)
    private_key_b64: str = Field(..., min_length=32, max_length=1024)
    declared_scopes: Optional[List[str]] = None
    label: str = Field("Robinhood Crypto", max_length=100)


class BrokerSchwabConnectRequest(BaseModel):
    client_id: str = Field(..., min_length=4, max_length=256)
    client_secret: str = Field(..., min_length=4, max_length=256)
    redirect_uri: str = Field("https://127.0.0.1:8787/api/v1/brokers/schwab/callback", max_length=256)
    label: str = Field("Charles Schwab", max_length=100)


class EmailConfigUpdate(BaseModel):
    enabled: Optional[bool] = None
    imap_host: Optional[str] = Field(None, max_length=120)
    imap_port: Optional[int] = Field(None, ge=1, le=65535)
    username: Optional[str] = Field(None, max_length=160)
    app_password: Optional[str] = Field(None, max_length=256)  # Write-only; stored in secrets.enc
    clear_password: bool = False
    folder: Optional[str] = Field(None, max_length=80)
    lookback_days: Optional[int] = Field(None, ge=1, le=90)


class EmailConfigOut(BaseModel):
    enabled: bool = False
    imap_host: str = ""
    imap_port: int = 993
    username: str = ""
    password_configured: bool = False
    folder: str = "INBOX"
    lookback_days: int = 30
    read_only_mode: bool = True


class EmailQueueItem(BaseModel):
    id: int
    uid_key: str
    sender: str
    subject: str
    received_at: str
    extracted_vendor: str
    amount_cents: Optional[int] = None
    due_date: Optional[str] = None
    kind: Literal["bill", "receipt"] = "bill"
    status: Literal["pending", "approved", "dismissed"] = "pending"
    matched_bill_id: Optional[int] = None
    created_at: str = ""


# --- Firefly III-Inspired Automation Rules Models ---


class TriggerSpec(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    field: Literal[
        "merchant",
        "memo",
        "amount_gt",
        "amount_lt",
        "amount_between",
        "amount_exact",
        "account_id",
        "tx_type",
        "missing_jar",
        "missing_tag",
    ]
    op: Literal[
        "contains",
        "starts_with",
        "ends_with",
        "exact",
        "regex",
        "eq",
        "gt",
        "lt",
        "between",
        "is_true",
    ] = "contains"
    value: Any = None
    value_max: Optional[int] = None
    negate: bool = Field(False, alias="not")


class ActionSpec(BaseModel):
    kind: Literal[
        "set_jar",
        "add_tag",
        "link_bill",
        "set_memo",
        "append_memo",
        "allocate_goal",
        "split_pct",
    ]
    value: Any = None
    params: Dict[str, Any] = Field(default_factory=dict)


class RuleGroupCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    priority: int = Field(10, ge=0, le=1000)
    active: bool = True


class RuleGroupOut(RuleGroupCreate):
    id: int
    created_at: str = ""
    updated_at: str = ""


class RuleCreate(BaseModel):
    group_id: Optional[int] = None
    name: str = Field(..., min_length=1, max_length=100)
    priority: int = Field(10, ge=0, le=1000)
    strict_and: bool = True
    stop_processing: bool = False
    triggers: List[TriggerSpec] = Field(..., min_length=1)
    actions: List[ActionSpec] = Field(..., min_length=1)
    active: bool = True


class RuleUpdate(BaseModel):
    group_id: Optional[int] = None
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    priority: Optional[int] = Field(None, ge=0, le=1000)
    strict_and: Optional[bool] = None
    stop_processing: Optional[bool] = None
    triggers: Optional[List[TriggerSpec]] = None
    actions: Optional[List[ActionSpec]] = None
    active: Optional[bool] = None


class RuleOut(BaseModel):
    id: int
    group_id: Optional[int] = None
    name: str
    priority: int
    strict_and: bool
    stop_processing: bool
    triggers: List[TriggerSpec]
    actions: List[ActionSpec]
    active: bool
    created_at: str = ""
    updated_at: str = ""


class RuleTestMatchSample(BaseModel):
    tx_id: int
    tx_date: str
    merchant: str
    amount_cents: int
    proposed_changes: List[str] = Field(default_factory=list)


class RuleTestResult(BaseModel):
    rule_id: Optional[int] = None
    scanned_count: int = 0
    matched_count: int = 0
    lookback_days: int = 730
    samples: List[RuleTestMatchSample] = Field(default_factory=list)


# --- Credit Reports, Inquiries, Derogatories & Factor Simulator Models ---


class CreditReportCreate(BaseModel):
    bureau: CreditBureau
    model: CreditScoreModel = "fico8"
    score: int = Field(..., ge=300, le=850)
    pulled_date: str = Field(default_factory=lambda: date.today().isoformat())
    source: str = Field("manual", max_length=60)
    notes: str = Field("", max_length=240)

    @field_validator("pulled_date")
    @classmethod
    def validate_pulled_date(cls, v: str) -> str:
        date.fromisoformat(v)
        return v


class CreditReportOut(CreditReportCreate):
    id: int
    created_at: str = ""


class CreditInquiryCreate(BaseModel):
    lender: str = Field(..., min_length=1, max_length=100)
    bureau: CreditBureau = "experian"
    inquiry_date: str
    counts_until: Optional[str] = None
    drops_off: Optional[str] = None

    @field_validator("inquiry_date", "counts_until", "drops_off")
    @classmethod
    def validate_inq_dates(cls, v: Optional[str]) -> Optional[str]:
        if v:
            date.fromisoformat(v)
        return v


class CreditInquiryOut(BaseModel):
    id: int
    lender: str
    bureau: CreditBureau
    inquiry_date: str
    counts_until: str
    drops_off: str
    is_scoring_active: bool = True
    created_at: str = ""


class CreditDerogatoryCreate(BaseModel):
    kind: DerogatoryKind
    lender: str = Field("", max_length=100)
    opened_date: str
    drops_off: Optional[str] = None
    active: bool = True
    notes: str = Field("", max_length=240)

    @field_validator("opened_date", "drops_off")
    @classmethod
    def validate_derog_dates(cls, v: Optional[str]) -> Optional[str]:
        if v:
            date.fromisoformat(v)
        return v


class CreditDerogatoryOut(BaseModel):
    id: int
    kind: DerogatoryKind
    lender: str = ""
    opened_date: str
    drops_off: str
    active: bool = True
    notes: str = ""
    created_at: str = ""


class CreditAPIConnectRequest(BaseModel):
    vendor: CreditAPIVendor
    env: Literal["sandbox", "production"] = "sandbox"
    label: str = Field("", max_length=100)
    api_key: Optional[str] = Field(None, max_length=2048)       # Write-only; sealed in secrets.enc
    api_secret: Optional[str] = Field(None, max_length=2048)    # Write-only; sealed in secrets.enc
    username: Optional[str] = Field(None, max_length=200)
    password: Optional[str] = Field(None, max_length=512)       # Write-only; sealed in secrets.enc
    user_id: Optional[str] = Field(None, max_length=200)
    report_key: Optional[str] = Field(None, max_length=256)
    display_token: Optional[str] = Field(None, max_length=512)  # Write-only; sealed in secrets.enc
    bureau: CreditBureau = "equifax"
    option: Optional[str] = Field(None, max_length=120)
    api_base_url: Optional[str] = Field(None, max_length=512)
    test_on_connect: bool = False


class CreditAPISyncRequest(BaseModel):
    order_refresh: bool = False
    sync_tradelines: bool = True
    report_key: Optional[str] = Field(None, max_length=256)
    display_token: Optional[str] = Field(None, max_length=512)
    bureau: Optional[CreditBureau] = None
    option: Optional[str] = Field(None, max_length=120)
    consumer_profile: Optional[Dict[str, Any]] = None


class CreditAPIVendorInfo(BaseModel):
    vendor: CreditAPIVendor
    display_name: str
    auth_type: str
    bureaus: List[str] = Field(default_factory=list)
    models: List[str] = Field(default_factory=list)
    sandbox_base_url: str = ""
    production_base_url: str = ""
    notes: str = ""


class CreditAPIConnectionOut(BaseModel):
    id: int
    vendor: CreditAPIVendor
    provider: str
    institution: str
    env: str = "sandbox"
    status: Literal["active", "needs_reauth", "relink_required", "error", "paused_locked"] = "active"
    api_key_masked: str = ""
    has_credentials: bool = False
    user_id: str = ""
    has_report_key: bool = False
    bureau: str = "equifax"
    api_base_url: str = ""
    last_synced_at: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""


class CreditAPIListResponse(BaseModel):
    vendors: List[CreditAPIVendorInfo] = Field(default_factory=list)
    connections: List[CreditAPIConnectionOut] = Field(default_factory=list)
    plaid_liabilities_connected: bool = False


class CreditFactorItem(BaseModel):
    factor: str
    label: str
    weight_pct: int
    status: Literal["optimal", "good", "fair", "drag"]
    summary: str
    estimated_delta_range: str = ""


class CreditSimulatedAction(BaseModel):
    action: str
    delta_min_pts: int
    delta_max_pts: int
    explanation: str


class CreditFactorBreakdown(BaseModel):
    disclaimer: str = (
        "Directional factor simulator only — not an official FICO or VantageScore calculation."
    )
    latest_actual_scores: List[CreditReportOut] = Field(default_factory=list)
    fico8_factors: List[CreditFactorItem] = Field(default_factory=list)
    vantage3_factors: List[CreditFactorItem] = Field(default_factory=list)
    simulated_actions: List[CreditSimulatedAction] = Field(default_factory=list)


class FiveTwentyFourStatus(BaseModel):
    count_5_24: int = 0
    under_5_24: bool = True
    include_authorized_user: bool = True
    counted_accounts: List[Dict[str, Any]] = Field(default_factory=list)
    next_slot_drop_date: Optional[str] = None
    inquiries_6m: int = 0
    inquiries_12m: int = 0
    velocity_warnings: List[str] = Field(default_factory=list)


# --- Credit & Debt Optimization Models ---


class CreditLineCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    issuer: str = Field("", max_length=60)
    kind: DebtKind = DebtKind.CREDIT_CARD
    balance_cents: int = Field(0, ge=0, le=100_000_000_00)
    credit_limit_cents: int = Field(0, ge=0, le=100_000_000_00)
    apr_bps: int = Field(2499, ge=0, le=10000)  # basis points: 2499 = 24.99% APR
    min_payment_cents: int = Field(0, ge=0, le=50_000_000)
    statement_closing_day: int = Field(15, ge=1, le=31)
    payment_due_day: int = Field(10, ge=1, le=31)
    promo_apr_bps: Optional[int] = Field(None, ge=0, le=10000)
    promo_ends_date: Optional[str] = None
    opened_date: Optional[str] = None
    has_grace_period: bool = True
    autopay_mode: AutopayMode = AutopayMode.STATEMENT
    rewards_note: str = Field("", max_length=120)
    account_id: Optional[int] = None
    is_authorized_user: bool = False

    @field_validator("promo_ends_date", "opened_date")
    @classmethod
    def validate_opt_iso_date(cls, v: Optional[str]) -> Optional[str]:
        if v:
            date.fromisoformat(v)
        return v


class CreditLineUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    issuer: Optional[str] = Field(None, max_length=60)
    balance_cents: Optional[int] = Field(None, ge=0, le=100_000_000_00)
    credit_limit_cents: Optional[int] = Field(None, ge=0, le=100_000_000_00)
    apr_bps: Optional[int] = Field(None, ge=0, le=10000)
    min_payment_cents: Optional[int] = Field(None, ge=0, le=50_000_000)
    statement_closing_day: Optional[int] = Field(None, ge=1, le=31)
    payment_due_day: Optional[int] = Field(None, ge=1, le=31)
    promo_apr_bps: Optional[int] = Field(None, ge=0, le=10000)
    promo_ends_date: Optional[str] = None
    opened_date: Optional[str] = None
    has_grace_period: Optional[bool] = None
    autopay_mode: Optional[AutopayMode] = None
    rewards_note: Optional[str] = Field(None, max_length=120)
    is_authorized_user: Optional[bool] = None


class CreditLineOut(BaseModel):
    id: int
    name: str
    issuer: str
    kind: DebtKind
    balance_cents: int
    credit_limit_cents: int
    apr_bps: int
    effective_apr_bps: int
    min_payment_cents: int
    effective_min_payment_cents: int
    statement_closing_day: int
    payment_due_day: int
    next_statement_close_date: str
    days_until_close: int
    next_due_date: str
    days_until_due: int
    utilization_pct: Optional[float] = None
    utilization_tier: str = "installment"
    promo_apr_bps: Optional[int] = None
    promo_ends_date: Optional[str] = None
    promo_days_remaining: Optional[int] = None
    opened_date: Optional[str] = None
    age_months: Optional[int] = None
    is_oldest_account: bool = False
    has_grace_period: bool = True
    autopay_mode: AutopayMode = AutopayMode.STATEMENT
    rewards_note: str = ""
    account_id: Optional[int] = None
    is_authorized_user: bool = False
    monthly_interest_cents: int = 0


class AZEOAction(BaseModel):
    credit_line_id: int
    card_name: str
    current_balance_cents: int
    target_statement_balance_cents: int
    pay_before_close_cents: int
    pay_by_date: str
    statement_close_date: str
    resulting_util_pct: float
    role: Literal["primary_reporting_card", "zero_out_card", "already_optimal"]
    explanation: str


class DebtStrategyResult(BaseModel):
    strategy: Literal["adhd_hybrid", "credit_first", "avalanche", "snowball"]
    label: str
    tagline: str
    recommended_for_adhd: bool = False
    months_to_debt_free: int
    debt_free_date: str
    total_interest_cents: int
    interest_saved_vs_minimums_cents: int
    first_win_months: int
    first_target_name: str
    month_1_actions: List[str] = Field(default_factory=list)


class CreditAdvisorAlert(BaseModel):
    severity: Literal["high", "medium", "info"]
    category: str
    title: str
    detail: str
    micro_step: str
    credit_line_id: Optional[int] = None


class CreditOptimizationReport(BaseModel):
    generated_at: str
    total_revolving_balance_cents: int
    total_revolving_limit_cents: int
    aggregate_utilization_pct: float
    aggregate_tier: str
    all_zero_penalty_risk: bool
    cards_with_balance_count: int
    total_cards_count: int
    total_debt_cents: int
    total_monthly_minimums_cents: int
    monthly_interest_burn_cents: int
    dti_pct: float
    dti_tier: str
    avg_account_age_months: Optional[float] = None
    oldest_account_name: Optional[str] = None
    autopay_missing_count: int
    extra_monthly_budget_cents: int
    credit_lines: List[CreditLineOut]
    azeo_plan: List[AZEOAction]
    strategies: List[DebtStrategyResult]
    recommended_strategy: str
    alerts: List[CreditAdvisorAlert]
    factor_simulator: Optional[CreditFactorBreakdown] = None
    five_twenty_four: Optional[FiveTwentyFourStatus] = None
    credit_reports: List[CreditReportOut] = Field(default_factory=list)
    credit_inquiries: List[CreditInquiryOut] = Field(default_factory=list)
    credit_derogatories: List[CreditDerogatoryOut] = Field(default_factory=list)


class BalanceTransferRequest(BaseModel):
    balance_cents: int = Field(..., gt=0, le=100_000_000_00)
    current_apr_bps: int = Field(..., ge=0, le=10000)
    transfer_fee_bps: int = Field(300, ge=0, le=1500)  # 300 = 3.00%
    promo_months: int = Field(15, ge=3, le=36)
    monthly_payment_cents: int = Field(..., gt=0, le=50_000_000)


class BalanceTransferResult(BaseModel):
    verdict: Literal["strong_yes", "marginal", "skip"]
    transfer_fee_cents: int
    interest_without_transfer_cents: int
    interest_with_transfer_cents: int
    net_savings_cents: int
    required_monthly_to_clear_promo_cents: int
    clears_before_promo_ends: bool
    explanation: str


# --- Stocks & Crypto Portfolio Models ---


class HoldingCreate(BaseModel):
    account_id: Optional[int] = None
    connection_id: Optional[int] = None
    symbol: str = Field(..., min_length=1, max_length=24)
    name: str = Field("", max_length=100)
    asset_class: AssetClass = "equity"
    quantity_e8: int = Field(..., ge=0)
    cost_basis_cents: int = Field(0, ge=0)
    price_e8: int = Field(0, ge=0)
    source: str = Field("manual", max_length=40)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, v: str) -> str:
        return v.strip().upper()


class HoldingUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=100)
    asset_class: Optional[AssetClass] = None
    quantity_e8: Optional[int] = Field(None, ge=0)
    cost_basis_cents: Optional[int] = Field(None, ge=0)
    price_e8: Optional[int] = Field(None, ge=0)
    source: Optional[str] = Field(None, max_length=40)


class HoldingOut(BaseModel):
    id: int
    account_id: Optional[int] = None
    connection_id: Optional[int] = None
    symbol: str
    name: str = ""
    asset_class: AssetClass
    quantity_e8: int
    cost_basis_cents: int
    price_e8: int
    market_value_cents: int
    unrealized_pnl_cents: int = 0
    unrealized_pnl_pct: float = 0.0
    source: str
    updated_at: str


class PortfolioAllocationItem(BaseModel):
    asset_class: AssetClass
    value_cents: int
    pct: float


class PortfolioSnapshotOut(BaseModel):
    snap_date: str
    total_value_cents: int
    cost_basis_cents: int


class PortfolioSummary(BaseModel):
    generated_at: str = Field(default_factory=utc_now_iso)
    total_value_cents: int = 0
    total_cost_basis_cents: int = 0
    unrealized_pnl_cents: int = 0
    unrealized_pnl_pct: float = 0.0
    allocations: List[PortfolioAllocationItem] = Field(default_factory=list)
    holdings: List[HoldingOut] = Field(default_factory=list)
    snapshots: List[PortfolioSnapshotOut] = Field(default_factory=list)
    quote_sources: List[str] = Field(default_factory=list)


# --- Goal Optimizer & Direct Manipulation Models ---


class GoalCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    target_cents: int = Field(..., gt=0, le=100_000_000_00)
    saved_cents: int = Field(0, ge=0, le=100_000_000_00)
    target_date: Optional[str] = None
    priority: int = Field(2, ge=1, le=5)
    priority_order: Optional[int] = Field(None, ge=0)
    category: GoalCategory = GoalCategory.GADGET
    funding_jar_slug: str = Field("fun", max_length=32)
    notes: str = Field("", max_length=240)

    @field_validator("target_date")
    @classmethod
    def validate_goal_date(cls, v: Optional[str]) -> Optional[str]:
        if v:
            date.fromisoformat(v)
        return v


class GoalUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    target_cents: Optional[int] = Field(None, gt=0, le=100_000_000_00)
    saved_cents: Optional[int] = Field(None, ge=0, le=100_000_000_00)
    target_date: Optional[str] = None
    priority: Optional[int] = Field(None, ge=1, le=5)
    priority_order: Optional[int] = Field(None, ge=0)
    status: Optional[Literal["active", "funded", "completed"]] = None
    notes: Optional[str] = Field(None, max_length=240)

    @field_validator("target_date")
    @classmethod
    def validate_opt_goal_date(cls, v: Optional[str]) -> Optional[str]:
        if v:
            date.fromisoformat(v)
        return v


class GoalReorderRequest(BaseModel):
    ordered_ids: List[int] = Field(..., min_length=1)


class GoalContributeRequest(BaseModel):
    amount_cents: int = Field(..., gt=0, le=100_000_000_00)


class GoalOut(BaseModel):
    id: int
    name: str
    target_cents: int
    saved_cents: int
    remaining_cents: int
    pct_funded: float
    target_date: Optional[str]
    priority: int
    priority_order: int = 0
    category: GoalCategory
    funding_jar_slug: str
    status: str
    notes: str
    deleted_at: Optional[str] = None
    created_at: str
    updated_at: str


class GoalPlanRequest(BaseModel):
    item_name: str = Field(..., min_length=1, max_length=100)
    target_cents: int = Field(..., gt=0, le=100_000_000_00)
    saved_cents: int = Field(0, ge=0, le=100_000_000_00)
    target_date: Optional[str] = None
    prefer_credit_rewards: bool = True


class GoalPlanOption(BaseModel):
    path_id: Literal["cash_sinking_fund", "credit_optimized_buy", "balanced_debt_goal_split"]
    title: str
    recommended: bool
    credit_safe: bool
    weekly_set_aside_cents: int
    per_paycheck_cents: int
    weeks_to_goal: int
    projected_ready_date: str
    interest_cost_cents: int
    summary: str
    steps: List[str]
    credit_impact_note: str


class GoalPlanResponse(BaseModel):
    item_name: str
    target_cents: int
    already_saved_cents: int
    remaining_cents: int
    work_hours_cost: float
    can_buy_cash_today: bool
    monthly_flexible_surplus_cents: int
    high_apr_debt_warning: Optional[str] = None
    accelerators: List[Dict[str, Any]] = Field(default_factory=list)
    options: List[GoalPlanOption]


# --- Quantitative Math Engine & MoneyStill Analytics Models ---


class DailyCashflowPoint(BaseModel):
    date: str
    balance_cents: int
    inflow_cents: int = 0
    outflow_cents: int = 0
    events: List[str] = Field(default_factory=list)


class SpendingVolatilityMetrics(BaseModel):
    sample_days: int = 0
    daily_mean_cents: int = 0
    daily_std_cents: int = 0
    weekly_mean_cents: int = 0
    weekly_std_cents: int = 0
    coefficient_of_variation: float = 0.0
    p50_weekly_spend_cents: int = 0
    p75_weekly_spend_cents: int = 0
    p90_weekly_spend_cents: int = 0
    recommended_buffer_cents: int = 0
    p50_daily_safe_cents: int = 0
    p90_conservative_daily_safe_cents: int = 0
    stability_score_0_100: int = 100


class OptimalAllocationStep(BaseModel):
    priority_rank: int = 1
    bucket: str
    target_name: str
    amount_cents: int
    expected_annual_benefit_cents: int = 0
    effective_apr_equivalent_pct: float = 0.0
    rationale: str = ""
    formula_note: str = ""


class QuantForecastReport(BaseModel):
    generated_at: str = Field(default_factory=utc_now_iso)
    horizon_days: int = 30
    starting_balance_cents: int = 0
    ending_balance_cents: int = 0
    lowest_balance_cents: int = 0
    lowest_balance_date: str = ""
    overdraft_risk: bool = False
    safe_runway_days: int = 30
    payday_crunch_warning: Optional[str] = None
    daily_points: List[DailyCashflowPoint] = Field(default_factory=list)
    volatility: SpendingVolatilityMetrics = Field(default_factory=SpendingVolatilityMetrics)
    monthly_surplus_cents: int = 0
    optimal_allocations: List[OptimalAllocationStep] = Field(default_factory=list)
    total_expected_annual_benefit_cents: int = 0


class TopExpenseItem(BaseModel):
    label: str
    jar_slug: str = ""
    amount_cents: int
    pct_of_expenses: float
    pct_of_income: float = 0.0
    color: str


class SankeyNode(BaseModel):
    id: str
    label: str
    column: Literal[0, 1, 2]
    amount_cents: int
    pct_of_income: float = 0.0
    color: str


class SankeyLink(BaseModel):
    source: str
    target: str
    amount_cents: int
    pct_of_income: float = 0.0
    color: str


class MonthlyTrendPoint(BaseModel):
    month: str
    income_cents: int
    expense_cents: int
    surplus_cents: int


class NetWorthPoint(BaseModel):
    date: str
    liquid_cents: int
    investment_cents: int
    debt_cents: int
    net_worth_cents: int


class AnalyticsOverview(BaseModel):
    period: str
    start_date: str
    end_date: str
    total_income_cents: int
    total_expense_cents: int
    net_surplus_cents: int
    savings_rate_pct: float
    avg_monthly_surplus_cents: int
    top_5_expenses: List[TopExpenseItem] = Field(default_factory=list)
    sankey_nodes: List[SankeyNode] = Field(default_factory=list)
    sankey_links: List[SankeyLink] = Field(default_factory=list)
    expense_distribution: List[TopExpenseItem] = Field(default_factory=list)
    monthly_trends: List[MonthlyTrendPoint] = Field(default_factory=list)
    net_worth_series: List[NetWorthPoint] = Field(default_factory=list)


class ScenarioOverride(BaseModel):
    goal_date_shifts: Dict[str, str] = Field(default_factory=dict)
    monthly_income_delta_cents: int = 0
    monthly_expense_delta_cents: int = 0
    one_off_events: List[Dict[str, Any]] = Field(default_factory=list)


class ScenarioCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    overrides: ScenarioOverride = Field(default_factory=ScenarioOverride)
    active: bool = True


class ScenarioOut(BaseModel):
    id: int
    name: str
    overrides: ScenarioOverride
    active: bool
    updated_at: str


class GoalContributionSeries(BaseModel):
    goal_id: int
    goal_name: str
    target_cents: int
    saved_cents: int
    target_date: Optional[str] = None
    points: List[Dict[str, Any]] = Field(default_factory=list)


class ScenarioSimRequest(BaseModel):
    horizon_days: int = Field(365, ge=30, le=730)
    scenario_id: Optional[int] = None
    overrides: ScenarioOverride = Field(default_factory=ScenarioOverride)


class ScenarioSimResult(BaseModel):
    horizon_days: int
    safe_runway_floor_cents: int
    baseline_points: List[DailyCashflowPoint] = Field(default_factory=list)
    scenario_points: List[DailyCashflowPoint] = Field(default_factory=list)
    diff_ending_cents: int = 0
    diff_pct: float = 0.0
    per_goal_series: List[GoalContributionSeries] = Field(default_factory=list)


class DashboardResponse(BaseModel):
    generated_at: str
    safe_to_spend: SafeToSpendBreakdown
    safe_to_invest_cents: int
    monthly_income_cents: int
    monthly_spent_cents: int
    smoothed_monthly_income_cents: int
    accounts: List[AccountOut]
    jars: List[JarState]
    rebalance_plans: List[RebalancePlan]
    upcoming_bills: List[RecurringBillOut]
    bill_anomalies: List[BillAnomaly] = Field(default_factory=list)
    cooling_impulses: List[ImpulseOut]
    recent_transactions: List[TransactionOut]
    adhd_stats: ADHDStats
    needs_triage_count: int
    ai_status: Dict[str, Any]
    recent_budget_events: List[BudgetAuditEvent]
    credit_summary: Dict[str, Any] = Field(default_factory=dict)
    goals: List[GoalOut] = Field(default_factory=list)
    quant_forecast: Optional[QuantForecastReport] = None
    intelligence_mode: str = IntelligenceMode.HYBRID.value
    active_ai_provider: str = "none"


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"] = "ok"
    app: str = "ChronoX Finance"
    version: str = "2.0.0"
    db_ok: bool = True
    auth_enabled: bool = True
    vault_state: VaultState = "needs_setup"
    intelligence_mode: str = IntelligenceMode.HYBRID.value
    active_provider: str = "none"


class PasscodeSetupRequest(BaseModel):
    passcode: str = Field(..., min_length=6, max_length=128)


class PasscodeLoginRequest(BaseModel):
    passcode: str = Field(..., min_length=1, max_length=128)
