"""
Strictly typed Pydantic v2 models for ChronoX Finance.

All monetary values are stored and computed in exact integer cents (`int`)
to completely eliminate IEEE-754 floating-point rounding bugs.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


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


SuggestionSource = Literal[
    "opencode_go",
    "gemini",
    "deterministic_local",
    "deterministic_quant",
    "ai_standalone",
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
    icon: Optional[str] = Field(None, max_length=12)
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


class RecurringBillCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    amount_cents: int = Field(..., gt=0, le=100_000_000_00)
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


class RecurringBillOut(BaseModel):
    id: int
    name: str
    normalized_merchant: str
    amount_cents: int
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


class AISettingsOut(BaseModel):
    intelligence_mode: IntelligenceMode = IntelligenceMode.HYBRID
    ai_provider: AIProvider = AIProvider.AUTO
    active_provider_resolved: Literal["opencode_go", "gemini", "none"] = "none"
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
    intelligence_mode: IntelligenceMode = IntelligenceMode.HYBRID
    ai_provider: AIProvider = AIProvider.AUTO
    active_provider_resolved: Literal["opencode_go", "gemini", "none"] = "none"
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
    intelligence_mode: Optional[IntelligenceMode] = None
    ai_provider: Optional[AIProvider] = None
    opencode_base_url: Optional[str] = None
    opencode_model: Optional[str] = None
    opencode_protocol: Optional[Literal["auto", "openai", "anthropic"]] = None
    opencode_max_daily_requests: Optional[int] = Field(None, ge=1, le=500)
    opencode_api_key: Optional[str] = None  # Write-only; never returned plaintext
    clear_opencode_key: bool = False  # Explicitly clear saved UI key and revert to env var
    gemini_api_key: Optional[str] = None  # Write-only; never returned plaintext
    clear_gemini_key: bool = False  # Explicitly clear saved UI key and revert to env var
    gemini_model: Optional[str] = None
    gemini_base_url: Optional[str] = None


class BillAnomaly(BaseModel):
    kind: Literal["double_charge", "missed_bill", "price_hike"]
    merchant: str
    amount_cents: int
    summary: str
    dates: List[str] = Field(default_factory=list)


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


# --- Goal Optimizer & Purchase Planner Models ---


class GoalCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    target_cents: int = Field(..., gt=0, le=100_000_000_00)
    saved_cents: int = Field(0, ge=0, le=100_000_000_00)
    target_date: Optional[str] = None
    priority: int = Field(2, ge=1, le=5)
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
    status: Optional[Literal["active", "funded", "completed"]] = None
    notes: Optional[str] = Field(None, max_length=240)


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
    category: GoalCategory
    funding_jar_slug: str
    status: str
    notes: str
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


# --- Quantitative Math Engine Models ---


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
    generated_at: str = Field(default_factory=lambda: utc_now_iso())
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
    version: str = "1.1.0"
    db_ok: bool = True
    auth_enabled: bool = False
    intelligence_mode: str = IntelligenceMode.HYBRID.value
    active_provider: str = "none"


class PasscodeSetupRequest(BaseModel):
    passcode: str = Field(..., min_length=6, max_length=128)


class PasscodeLoginRequest(BaseModel):
    passcode: str = Field(..., min_length=1, max_length=128)


def utc_now_iso() -> str:
    """Return ISO-8601 UTC timestamp string."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
