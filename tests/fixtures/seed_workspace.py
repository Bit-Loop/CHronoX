"""
Test-only realistic 90-day workspace seeder for ChronoX Finance unit & integration tests.

Strictly isolated under `tests/fixtures/` — NEVER exposed to production UI, API, or CLI.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chronox_finance.service import FinanceService


def seed_workspace(service: "FinanceService", today: date | None = None) -> dict:
    """Populate realistic 90-day test financial data and trigger self-updating sync."""
    ref = today or date.today()
    service.storage.clear_all_financial_data()

    # Create realistic test accounts
    checking = service.storage.create_account(
        name="Main Checking",
        account_type="checking",
        balance_cents=318_500,  # $3,185.00
        institution="SoFi",
        is_liquid=True,
    )
    savings = service.storage.create_account(
        name="Emergency HYSA",
        account_type="savings",
        balance_cents=125_000,  # $1,250.00
        institution="Ally",
        is_liquid=True,
    )
    credit = service.storage.create_account(
        name="Everyday Rewards Card",
        account_type="credit",
        balance_cents=-24_800,  # $248.00 current balance (auto-reserved in Safe-to-Spend)
        institution="Chase",
        is_liquid=False,
    )
    service.storage.create_account(
        name="ChronoX Paper/Trading Vault",
        account_type="trading",
        balance_cents=85_000,  # $850.00 ring-fenced
        institution="Alpaca",
        is_liquid=False,
    )

    cid = int(checking["id"])
    ccid = int(credit["id"])

    def _hist(
        days_ago: int,
        tx_type: str,
        cents: int,
        merchant: str,
        memo: str = "",
        acct_id: int = cid,
    ):
        tx_dt = (ref - timedelta(days=days_ago)).isoformat()
        service.add_transaction_raw(
            account_id=acct_id,
            tx_type=tx_type,
            amount_cents=cents,
            merchant=merchant,
            memo=memo,
            tx_date=tx_dt,
            source="test_fixture",
            adjust_balance=False,
            run_auto_sync=False,
        )

    # Bi-weekly paychecks over the past 84 days
    for k in range(6):
        _hist(
            days_ago=6 + k * 14,
            tx_type="income",
            cents=245_000,
            merchant="Acme Tech Payroll Direct Deposit",
        )

    # Recurring monthly bills over 3 months
    for m_offset in (62, 32, 2):
        _hist(days_ago=m_offset, tx_type="expense", cents=145_000, merchant="Oakridge Apartments Rent")
    _hist(days_ago=87, tx_type="expense", cents=11_400, merchant="CITY ELECTRIC UTIL #101")
    _hist(days_ago=57, tx_type="expense", cents=11_850, merchant="CITY ELECTRIC UTILITY PAY")
    _hist(days_ago=27, tx_type="expense", cents=11_400, merchant="City Electric Utility")
    for m_offset in (83, 53, 23):
        _hist(days_ago=m_offset, tx_type="expense", cents=7_500, merchant="Google Fiber Internet")
    for m_offset in (79, 49, 19):
        _hist(days_ago=m_offset, tx_type="expense", cents=11_900, merchant="Progressive Auto Insurance")

    # Subscriptions — including a silent price hike on Netflix ($15.49 -> $22.99)
    _hist(days_ago=85, tx_type="expense", cents=1_549, merchant="NETFLIX.COM*8821", acct_id=ccid)
    _hist(days_ago=55, tx_type="expense", cents=1_549, merchant="NETFLIX INC CA", acct_id=ccid)
    _hist(days_ago=25, tx_type="expense", cents=2_299, merchant="Netflix Subscription", acct_id=ccid)

    for m_offset in (64, 34, 4):
        _hist(days_ago=m_offset, tx_type="expense", cents=1_199, merchant="Spotify Premium", acct_id=ccid)
    for m_offset in (77, 47, 17):
        _hist(days_ago=m_offset, tx_type="expense", cents=4_999, merchant="Equinox Gym Membership", acct_id=ccid)
    for m_offset in (61, 31, 1):
        _hist(days_ago=m_offset, tx_type="expense", cents=1_000, merchant="OpenCode Go Subscription", acct_id=ccid)

    # Daily living
    for d, amt, merch in [
        (55, 13_420, "Trader Joe's"),
        (42, 11_850, "Trader Joe's"),
        (28, 14_190, "Costco Wholesale"),
        (19, 4_850, "Shell Gas Station"),
        (12, 10_640, "Trader Joe's"),
        (7, 3_200, "CVS Pharmacy"),
        (4, 9_480, "Whole Foods Market"),
        (2, 4_620, "Chevron Fuel"),
    ]:
        _hist(days_ago=d, tx_type="expense", cents=amt, merchant=merch, acct_id=ccid)

    # Guilt-Free Fun
    for d, amt, merch in [
        (48, 3_650, "DoorDash Thai Kitchen"),
        (33, 6_499, "Steam Games"),
        (21, 5_200, "Local Ramen Bar"),
        (9, 4_180, "DoorDash Chipotle"),
        (6, 725, "Starbucks Coffee"),
        (3, 8_900, "Micro Center Electronics"),
        (1, 685, "Blue Bottle Coffee"),
    ]:
        _hist(days_ago=d, tx_type="expense", cents=amt, merchant=merch, acct_id=ccid)

    # Low-confidence merchant for Weekly Reset triage test
    _hist(
        days_ago=2,
        tx_type="expense",
        cents=3_450,
        merchant="SQ *BRAINWAVE STUDIO #882",
        memo="Card tap",
        acct_id=ccid,
    )

    # Impulse Cooling-Off Vault items
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    service.storage.create_impulse(
        item_name="Custom Mechanical Keyboard Kit",
        price_cents=18_900,
        reason="Late-night YouTube hyperfixation",
        url="",
        cooling_hours=48,
        created_at=(now_utc - timedelta(hours=53)).isoformat().replace("+00:00", "Z"),
        unlocks_at=(now_utc - timedelta(hours=5)).isoformat().replace("+00:00", "Z"),
    )
    service.storage.create_impulse(
        item_name="Noise-Canceling Headphones Upgrade",
        price_cents=32_900,
        reason="Saw a flash sale banner",
        url="",
        cooling_hours=48,
        created_at=(now_utc - timedelta(hours=10)).isoformat().replace("+00:00", "Z"),
        unlocks_at=(now_utc + timedelta(hours=38)).isoformat().replace("+00:00", "Z"),
    )

    # Profile settings + ADHD progress
    service.storage.update_settings(
        {
            "hourly_wage_cents": 3_400,
            "emergency_buffer_cents": 75_000,
            "default_cooling_hours": 48,
            "payday_cadence_days": 14,
            "next_payday_date": (ref + timedelta(days=8)).isoformat(),
            "expected_paycheck_cents": 245_000,
        }
    )
    service.storage.update_adhd_progress(
        {
            "weekly_reset_streak": 2,
            "grace_days_available": 1,
            "last_reset_date": (ref - timedelta(days=8)).isoformat(),
            "impulses_skipped_count": 3,
            "impulse_money_saved_cents": 27_400,
            "dopamine_xp": 310,
            "recent_wins": [
                "Skipped $129.00 impulse smart mug after 48h cooldown! +75 XP",
                "Auto-budget caught Netflix price jump (+$7.50/mo) automatically",
                "Completed Weekly Money Reset — 2-week calm streak!",
            ],
        }
    )

    # Credit Lines & Debts
    service.storage.create_credit_line(
        {
            "name": "Chase Sapphire Preferred",
            "issuer": "Chase",
            "kind": "credit_card",
            "balance_cents": 24_800,
            "credit_limit_cents": 600_000,
            "apr_bps": 2149,
            "min_payment_cents": 3_500,
            "statement_closing_day": 18,
            "payment_due_day": 13,
            "opened_date": "2020-04-15",
            "has_grace_period": True,
            "autopay_mode": "statement",
            "rewards_note": "2x dining/travel + purchase protection",
            "account_id": ccid,
        }
    )
    service.storage.create_credit_line(
        {
            "name": "Amex Blue Cash Everyday",
            "issuer": "American Express",
            "kind": "credit_card",
            "balance_cents": 168_000,
            "credit_limit_cents": 400_000,
            "apr_bps": 2699,
            "min_payment_cents": 5_200,
            "statement_closing_day": 8,
            "payment_due_day": 3,
            "opened_date": "2022-09-01",
            "has_grace_period": False,
            "autopay_mode": "none",
            "rewards_note": "3% groceries/gas",
        }
    )
    service.storage.create_credit_line(
        {
            "name": "Discover It 0% Promo",
            "issuer": "Discover",
            "kind": "credit_card",
            "balance_cents": 62_000,
            "credit_limit_cents": 350_000,
            "apr_bps": 2499,
            "promo_apr_bps": 0,
            "promo_ends_date": (ref + timedelta(days=75)).isoformat(),
            "min_payment_cents": 2_500,
            "statement_closing_day": 24,
            "payment_due_day": 19,
            "opened_date": "2023-06-10",
            "has_grace_period": True,
            "autopay_mode": "minimum",
            "rewards_note": "5% rotating categories",
        }
    )
    service.storage.create_credit_line(
        {
            "name": "Federal Student Loan B",
            "issuer": "Nelnet",
            "kind": "student_loan",
            "balance_cents": 420_000,
            "credit_limit_cents": 0,
            "apr_bps": 550,
            "min_payment_cents": 9_500,
            "statement_closing_day": 1,
            "payment_due_day": 22,
            "opened_date": "2021-08-20",
            "has_grace_period": True,
            "autopay_mode": "minimum",
            "rewards_note": "0.25% autopay rate discount",
        }
    )

    # Starter Goals
    service.storage.create_goal(
        {
            "name": "$1,000 Workstation Gadget Upgrade",
            "target_cents": 100_000,
            "saved_cents": 25_000,
            "target_date": (ref + timedelta(days=90)).isoformat(),
            "priority": 2,
            "category": "gadget",
            "funding_jar_slug": "fun",
            "notes": "OLED monitor / Mac Mini upgrade — credit-safe plan",
        }
    )
    service.storage.create_goal(
        {
            "name": "3-Month Calm Emergency Buffer",
            "target_cents": 450_000,
            "saved_cents": 125_000,
            "priority": 1,
            "category": "emergency",
            "funding_jar_slug": "safety",
            "notes": "Peace-of-mind runway",
        }
    )

    sync_summary = service.run_self_updating_sync(reason="Test workspace seeded")
    return {
        "accounts_created": 4,
        "savings_id": savings["id"],
        "sync": sync_summary,
    }


# Test-only backward-compatible alias
seed_demo_data = seed_workspace
