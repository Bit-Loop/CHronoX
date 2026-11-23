"""
ADHD Behavioral Finance Engine.

Translates neurodivergent finance research into concrete mechanics:
1. Impulse Cooling-Off Vault ("Park It, Don't Fight It"):
   - Satisfies the urge to act immediately without spending money.
   - Translates abstract price tags into tangible work-hours & goal-delay days.
   - Celebrates skipped impulses with Dopamine XP & optional auto-savings sweep.
2. Shame-Free Streak & Dopamine Level System:
   - Includes a free "Grace Freeze" so missing a week NEVER wipes a streak and triggers RSD.
3. 5-Minute Weekly Money Reset Wizard:
   - Bundles all housekeeping into 4 bite-sized, high-reward steps.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from chronox_finance.models import ADHDStats, ImpulseOut, ImpulseStatus

LEVELS: List[Tuple[int, int, str]] = [
    (1, 0, "Calm Starter"),
    (2, 150, "Impulse Tamer"),
    (3, 400, "Bill Radar Pilot"),
    (4, 800, "Jar Architect"),
    (5, 1400, "Dopamine Strategist"),
    (6, 2200, "Autopilot Master"),
    (7, 3500, "ChronoX Money Zen"),
]


def compute_level(xp: int) -> Tuple[int, str]:
    level, title = 1, "Calm Starter"
    for lvl, threshold, name in LEVELS:
        if xp >= threshold:
            level, title = lvl, name
    return level, title


def enrich_impulse(
    raw: Dict[str, Any],
    *,
    hourly_wage_cents: int,
    safe_to_spend_cents: int,
    daily_savings_rate_cents: int = 2_000,
    now_utc: Optional[datetime] = None,
) -> ImpulseOut:
    """Compute live countdown, work-hours cost, goal delay, and affordability for an impulse item."""
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    unlocks_dt = datetime.fromisoformat(raw["unlocks_at"].replace("Z", "+00:00"))
    if unlocks_dt.tzinfo is None:
        unlocks_dt = unlocks_dt.replace(tzinfo=timezone.utc)
    delta_hours = (unlocks_dt - now).total_seconds() / 3600.0
    hours_remaining = round(max(0.0, delta_hours), 1)

    stored_status = raw["status"]
    if stored_status == ImpulseStatus.COOLING.value and hours_remaining <= 0.0:
        status = ImpulseStatus.READY
    else:
        status = ImpulseStatus(stored_status)

    price_cents = int(raw["price_cents"])
    wage = max(100, int(hourly_wage_cents))
    work_hours_cost = round(price_cents / wage, 1)

    daily_save = max(500, int(daily_savings_rate_cents))
    goal_delay_days = max(1, int(round(price_cents / daily_save)))

    return ImpulseOut(
        id=int(raw["id"]),
        item_name=raw["item_name"],
        price_cents=price_cents,
        reason=raw.get("reason", ""),
        url=raw.get("url", ""),
        cooling_hours=int(raw["cooling_hours"]),
        created_at=raw["created_at"],
        unlocks_at=raw["unlocks_at"],
        hours_remaining=hours_remaining,
        status=status,
        work_hours_cost=work_hours_cost,
        goal_delay_days=goal_delay_days,
        can_afford_safely=price_cents <= safe_to_spend_cents,
    )


def build_adhd_stats(raw: Dict[str, Any]) -> ADHDStats:
    xp = int(raw.get("dopamine_xp", 0))
    level, title = compute_level(xp)
    return ADHDStats(
        weekly_reset_streak=int(raw.get("weekly_reset_streak", 0)),
        grace_days_available=int(raw.get("grace_days_available", 1)),
        last_reset_date=raw.get("last_reset_date"),
        impulses_skipped_count=int(raw.get("impulses_skipped_count", 0)),
        impulse_money_saved_cents=int(raw.get("impulse_money_saved_cents", 0)),
        dopamine_xp=xp,
        level=level,
        level_title=title,
        recent_wins=list(raw.get("recent_wins", [])),
    )


def compute_weekly_reset_update(
    current_progress: Dict[str, Any],
    today: Optional[date] = None,
) -> Dict[str, Any]:
    """
    Update weekly reset streak with ADHD-friendly Grace Freeze protection.

    - Within 10 days of last reset: increments streak cleanly (+100 XP).
    - 11–18 days since last reset AND grace freeze available: uses grace freeze
      to PROTECT the streak instead of punishing the user with a reset to 0!
    - Same ISO week: idempotent, awards gentle +25 XP check-in bonus without double-counting streak.
    """
    ref = today or date.today()
    streak = int(current_progress.get("weekly_reset_streak", 0))
    grace = int(current_progress.get("grace_days_available", 1))
    xp = int(current_progress.get("dopamine_xp", 0))
    wins = list(current_progress.get("recent_wins", []))
    last_str = current_progress.get("last_reset_date")

    win_msg = f"Completed Weekly Money Reset ({ref.isoformat()}) — +100 XP!"

    if last_str:
        last_dt = date.fromisoformat(last_str)
        delta_days = (ref - last_dt).days
        if last_dt.isocalendar()[:2] == ref.isocalendar()[:2]:
            # Already reset this week — celebrate extra check-in without double-counting streak
            xp += 25
            win_msg = f"Bonus mid-week check-in ({ref.isoformat()}) — +25 XP!"
        elif delta_days <= 10:
            streak += 1
            xp += 100
            if streak % 4 == 0 and grace < 2:
                grace += 1
                win_msg = f"4-week Reset Streak ({streak}w)! Earned an extra Grace Freeze +100 XP!"
        elif delta_days <= 18 and grace > 0:
            grace -= 1
            streak += 1
            xp += 100
            win_msg = f"Grace Freeze saved your {streak}-week streak! Fresh start +100 XP!"
        else:
            streak = 1
            grace = max(grace, 1)
            xp += 100
            win_msg = "Fresh Weekly Reset started — zero shame, full momentum! +100 XP!"
    else:
        streak = 1
        xp += 100
        win_msg = "First Weekly Money Reset complete! +100 XP!"

    wins = [win_msg] + wins[:11]
    return {
        "weekly_reset_streak": streak,
        "grace_days_available": grace,
        "last_reset_date": ref.isoformat(),
        "dopamine_xp": xp,
        "recent_wins": wins,
    }


def is_reset_done_this_week(last_reset_date: Optional[str], today: Optional[date] = None) -> bool:
    if not last_reset_date:
        return False
    ref = today or date.today()
    try:
        return date.fromisoformat(last_reset_date).isocalendar()[:2] == ref.isocalendar()[:2]
    except ValueError:
        return False
