"""
Zero-Friction ADHD Natural-Language Quick-Add Parser.

Turns 2-second brain-dumps into structured transactions or impulse-vault entries:
- "coffee 6.50"                  -> Expense $6.50, Coffee
- "target 48.20 yesterday"       -> Expense $48.20, Target, date=yesterday
- "paycheck +2450"               -> Income $2,450.00, Paycheck
- "got paid 1850 freelance"      -> Income $1,850.00, Freelance
- "paid electric bill 112.40"    -> Expense $112.40, Electric Bill
- "impulse 199 mech keyboard"    -> Impulse Cooling-Off Vault entry ($199.00)
- "want $85 hoodie 72h"          -> Impulse Cooling-Off Vault entry ($85.00, 72h)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal, Optional

_AMOUNT_RE = re.compile(
    r"(?<!\d)(?P<sign>[+-])?\s*\$?\s*(?P<num>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)(?!\d)"
)
_HOURS_RE = re.compile(r"\b(\d{1,3})\s*(?:h|hr|hrs|hour|hours)\b", re.IGNORECASE)
_DAYS_COOL_RE = re.compile(r"\b(\d{1,2})\s*(?:d|day|days)\b", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")

_IMPULSE_PREFIX_RE = re.compile(
    r"^\s*(impulse|park|cool|cooldown|wishlist|want|tempted|later)\b[:\s-]*",
    re.IGNORECASE,
)
_INCOME_HINT_RE = re.compile(
    r"\b(paycheck|paid|salary|income|deposit|refund|bonus|dividend|freelance|stipend|gig|venmo in|received)\b",
    re.IGNORECASE,
)
_LEADING_VERBS_RE = re.compile(
    r"^\s*(spent|paid|bought|got|received|earned|logged|add)\b\s*(on|for|at)?\s*",
    re.IGNORECASE,
)


@dataclass
class ParsedQuickAdd:
    intent: Literal["transaction", "impulse"]
    tx_type: Literal["expense", "income"]
    amount_cents: int
    merchant: str
    tx_date: str
    memo: str = ""
    cooling_hours: int = 48


def _to_cents(num_str: str) -> int:
    try:
        dec = Decimal(num_str.replace(",", ""))
        cents = int((dec * 100).quantize(Decimal("1")))
        return cents
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Could not parse dollar amount: {num_str!r}") from exc


def parse_quick_add(text: str, default_cooling_hours: int = 48, today: Optional[date] = None) -> ParsedQuickAdd:
    """Parse a free-form ADHD quick-add string into a structured action."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("Quick-add text cannot be empty")

    ref_today = today or date.today()
    working = raw

    # 1. Detect impulse intent first
    is_impulse = False
    if _IMPULSE_PREFIX_RE.match(working):
        is_impulse = True
        working = _IMPULSE_PREFIX_RE.sub("", working).strip()

    # 2. Extract optional cooling duration if impulse
    cooling_hours = default_cooling_hours
    if is_impulse:
        hr_match = _HOURS_RE.search(working)
        day_match = _DAYS_COOL_RE.search(working)
        if hr_match:
            cooling_hours = max(1, min(720, int(hr_match.group(1))))
            working = _HOURS_RE.sub(" ", working)
        elif day_match:
            cooling_hours = max(1, min(720, int(day_match.group(1)) * 24))
            working = _DAYS_COOL_RE.sub(" ", working)

    # 3. Extract optional date token ('yesterday', 'today', or YYYY-MM-DD)
    tx_date = ref_today
    iso_match = _ISO_DATE_RE.search(working)
    if iso_match:
        tx_date = date.fromisoformat(iso_match.group(1))
        working = _ISO_DATE_RE.sub(" ", working)
    elif re.search(r"\byesterday\b", working, re.IGNORECASE):
        tx_date = ref_today - timedelta(days=1)
        working = re.sub(r"\byesterday\b", " ", working, flags=re.IGNORECASE)
    elif re.search(r"\btoday\b", working, re.IGNORECASE):
        working = re.sub(r"\btoday\b", " ", working, flags=re.IGNORECASE)

    # 4. Extract amount (prefer token with '$' or '+' or decimal; else last numeric token)
    matches = list(_AMOUNT_RE.finditer(working))
    if not matches:
        raise ValueError("Please include a dollar amount, e.g. 'coffee 6.50' or 'paycheck +2400'")

    chosen = None
    for m in matches:
        token = m.group(0)
        if "$" in token or m.group("sign") in ("+", "-") or "." in m.group("num"):
            chosen = m
            break
    if chosen is None:
        chosen = matches[-1]

    sign = chosen.group("sign")
    amount_cents = _to_cents(chosen.group("num"))
    if amount_cents <= 0:
        raise ValueError("Amount must be greater than $0.00")

    # Remove matched amount from descriptor
    merchant_raw = (working[: chosen.start()] + " " + working[chosen.end() :]).strip()

    # 5. Determine income vs expense
    tx_type: Literal["expense", "income"] = "expense"
    if not is_impulse:
        # "paid <something> <amount>" is an expense unless it's "got paid" / "paycheck"
        if sign == "+":
            tx_type = "income"
        elif re.search(r"\bgot\s+paid\b", raw, re.IGNORECASE):
            tx_type = "income"
        elif not re.match(r"^\s*paid\b", raw, re.IGNORECASE) and _INCOME_HINT_RE.search(raw):
            tx_type = "income"

    # Clean leading filler words
    merchant_clean = _LEADING_VERBS_RE.sub("", merchant_raw)
    merchant_clean = re.sub(r"\b(on|for|at|to|from|in)\s*$", "", merchant_clean, flags=re.IGNORECASE)
    merchant_clean = re.sub(r"^\s*(on|for|at|to|from)\b\s*", "", merchant_clean, flags=re.IGNORECASE)
    merchant_clean = re.sub(r"\s+", " ", merchant_clean).strip(" -,:;")

    if not merchant_clean:
        merchant_clean = "Paycheck" if tx_type == "income" else ("Impulse Item" if is_impulse else "Quick Expense")
    else:
        merchant_clean = merchant_clean[:100]
        if merchant_clean.islower():
            merchant_clean = merchant_clean.title()

    return ParsedQuickAdd(
        intent="impulse" if is_impulse else "transaction",
        tx_type=tx_type,
        amount_cents=amount_cents,
        merchant=merchant_clean,
        tx_date=tx_date.isoformat(),
        memo=f"Quick-added: {raw[:140]}",
        cooling_hours=cooling_hours,
    )
