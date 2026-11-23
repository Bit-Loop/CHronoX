"""
Smart Merchant Normalization & Auto-Categorization Engine.

Combines:
1. User-learned merchant rules (highest priority: fix once, remembered forever!)
2. Rich built-in pattern rules covering bills, subscriptions, groceries, gas,
   dining/coffee, hobbies/shopping, savings/debt, and investing/trading.
3. Explicit confidence scoring so low-confidence items surface cleanly in the
   ADHD 5-Minute Weekly Reset triage queue instead of nagging daily.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

_STRIP_SUFFIXES = re.compile(
    r"(\b(inc|llc|corp|co|ltd|store|pos|debit|credit|purchase|payment|autopay|ach|dbt|crd|chk)\b"
    r"|#\d+|\*\w+|\b\d{4,}\b)",
    re.IGNORECASE,
)
_NON_ALNUM = re.compile(r"[^a-z0-9\s]+")
_MULTI_SPACE = re.compile(r"\s+")


def normalize_merchant(raw: str) -> str:
    """Normalize noisy bank descriptors ('AMZN MKTP US*2K9A #1234' -> 'amzn mktp us')."""
    text = (raw or "").strip().lower()
    text = _STRIP_SUFFIXES.sub(" ", text)
    text = _NON_ALNUM.sub(" ", text)
    text = _MULTI_SPACE.sub(" ", text).strip()
    return text or "unknown"


@dataclass(frozen=True)
class CategoryMatch:
    jar_slug: str
    subcategory: str
    confidence: float
    is_subscription_hint: bool = False
    is_essential_hint: bool = False


# Ordered from most specific to broader keywords: (regex, jar_slug, subcategory, confidence, sub_hint, essential_hint)
_BUILTIN_RULES: List[Tuple[re.Pattern[str], str, str, float, bool, bool]] = [
    # --- Bills & Essentials (Protected) ---
    (re.compile(r"\b(rent|mortgage|apartment|property mgmt|landlord|hoa)\b", re.I), "bills", "Housing", 0.96, True, True),
    (re.compile(r"\b(electric|power|water|sewer|gas bill|coned|pge|duke energy|xcel|utility|utilities)\b", re.I), "bills", "Utilities", 0.95, True, True),
    (re.compile(r"\b(comcast|xfinity|verizon|at&t|att|t-mobile|tmobile|spectrum|google fiber|starlink|internet|phone bill)\b", re.I), "bills", "Internet & Phone", 0.94, True, True),
    (re.compile(r"\b(geico|progressive|state farm|allstate|usaa|tricare|lemonade|insurance|health ins|dental|vision)\b", re.I), "bills", "Insurance", 0.94, True, True),
    (re.compile(r"\b(student loan|navient|nelnet|mohela|auto loan|car payment|minimum payment|nfcu auto|navy federal loan)\b", re.I), "bills", "Debt Minimum", 0.93, True, True),
    (re.compile(r"\b(netflix|spotify|hulu|disney\+?|max|hbo|apple\.com/bill|icloud|youtube premium|amazon prime|audible|chatgpt|openai|claude|opencode|github|notion|dropbox|adobe|xbox|playstation|nintendo|gym|planet fitness|la fitness|equinox)\b", re.I), "bills", "Subscriptions", 0.90, True, False),

    # --- Daily Living (Flexible Necessities) ---
    (re.compile(r"\b(trader joe|whole foods|kroger|safeway|publix|aldi|lidl|wegmans|heb|costco|sam'?s club|sprouts|commissary|deca|navy exchange|nex\b|aafes|grocery|groceries|supermarket|market|walmart|target|meijer|winco)\b", re.I), "daily", "Groceries & Household", 0.92, False, True),
    (re.compile(r"\b(shell|chevron|exxon|mobil|bp|arco|sunoco|circle k|wawa|sheetz|7-eleven|gas station|fuel|supercharger|ev charging)\b", re.I), "daily", "Fuel & Transit", 0.92, False, True),
    (re.compile(r"\b(uber|lyft|metro|transit|subway|mta|bart|parking|toll|ezpass)\b", re.I), "daily", "Transit", 0.88, False, True),
    (re.compile(r"\b(cvs|walgreens|rite aid|pharmacy|doctor|clinic|hospital|therapy|therapist|psychiatry|adderall|vyvanse|rx|medical|urgent care)\b", re.I), "daily", "Health & Meds", 0.95, False, True),
    (re.compile(r"\b(petco|petsmart|chewy|vet|veterinary)\b", re.I), "daily", "Pet Care", 0.90, False, True),
    (re.compile(r"\b(laundry|dry clean|haircut|barber|home depot|lowe'?s|ikea|hardware)\b", re.I), "daily", "Personal & Home", 0.85, False, True),

    # --- Guilt-Free Fun (Dopamine Budget) ---
    (re.compile(r"\b(starbucks|dunkin|peet'?s|blue bottle|dutch bros|coffee|cafe|espresso|boba|tea)\b", re.I), "fun", "Coffee & Treats", 0.93, False, False),
    (re.compile(r"\b(doordash|ubereats|uber eats|grubhub|instacart|postmates|seamless|chipotle|chick-fil-a|mcdonald|wendy|taco bell|panera|sweetgreen|shake shack|five guys|pizza|sushi|thai|ramen|burger|restaurant|diner|grill|bar|pub|brewery|bakery)\b", re.I), "fun", "Dining & Delivery", 0.91, False, False),
    (re.compile(r"\b(steam|epic games|best buy|micro center|microcenter|apple store|amazon|amzn|etsy|ebay|nike|adidas|uniqlo|zara|sephora|ulta|rei|lego|books|barnes|cinema|movie|amc|regal|concert|ticketmaster|spotify live|hobby|craft|gaming)\b", re.I), "fun", "Shopping & Hobbies", 0.84, False, False),
    (re.compile(r"\b(airbnb|hotel|marriott|hilton|delta|united|american air|southwest|flight|vacation)\b", re.I), "fun", "Travel & Experiences", 0.88, False, False),

    # --- Safety & Future ---
    (re.compile(r"\b(emergency|savings|ally bank|marcus|sofi savings|hysa|sinking fund|debt payoff|extra payment)\b", re.I), "safety", "Savings & Buffer", 0.94, False, True),

    # --- ChronoX Invest Vault ---
    (re.compile(r"\b(vanguard|fidelity|schwab|robinhood|alpaca|ibkr|interactive brokers|coinbase|kraken|etrade|chronox|ira|401k|roth|brokerage|invest)\b", re.I), "invest", "Investing & Trading", 0.95, False, False),
]


class TransactionCategorizer:
    """Categorizes merchants into the 5-Jar ADHD budget model."""

    def categorize(
        self,
        merchant: str,
        memo: str,
        jars_by_slug: Dict[str, Dict[str, Any]],
        learned_rules: Dict[str, Dict[str, Any]],
    ) -> Tuple[Optional[int], str, float, bool]:
        """
        Return `(jar_id, subcategory, confidence, needs_review)`.

        Priority:
        1. User-learned merchant rules (`confidence = 0.99`, `needs_review = False`).
        2. Built-in merchant/keyword pattern matching.
        3. Fallback to `fun` jar with low confidence (`0.45`, `needs_review = True`)
           so it shows up cleanly in the 5-minute Weekly Reset triage step.
        """
        norm = normalize_merchant(merchant)
        if norm in learned_rules:
            rule = learned_rules[norm]
            return int(rule["jar_id"]), rule.get("subcategory") or "Learned", 0.99, False

        haystack = f"{merchant} {memo}".strip()
        for pattern, slug, subcat, conf, _sub_hint, _ess_hint in _BUILTIN_RULES:
            if pattern.search(haystack):
                jar = jars_by_slug.get(slug)
                if jar:
                    return int(jar["id"]), subcat, conf, conf < 0.75

        # Fallback: assign to Guilt-Free Fun or Daily and flag for quick 1-tap triage
        fallback_jar = jars_by_slug.get("fun") or jars_by_slug.get("daily") or next(iter(jars_by_slug.values()), None)
        jar_id = int(fallback_jar["id"]) if fallback_jar else None
        return jar_id, "Uncategorized", 0.45, True

    def detect_hints(self, merchant: str, memo: str = "") -> Tuple[bool, bool]:
        """Return `(is_subscription_hint, is_essential_hint)` for a merchant."""
        haystack = f"{merchant} {memo}".strip()
        for pattern, _slug, _subcat, _conf, sub_hint, ess_hint in _BUILTIN_RULES:
            if pattern.search(haystack):
                return sub_hint, ess_hint
        return False, False

    def classify_income_subcategory(self, merchant: str, memo: str = "") -> str:
        """Recognize Navy Federal dividends, military/VA/federal paychecks, and standard payroll."""
        haystack = f"{merchant} {memo}".strip()
        if re.search(r"\b(dfas|mil\s*pay|active\s+duty\s+pay|vaed\s+treas|vacp\s+treas|navy\s+pay|bah\b|bas\b)\b", haystack, re.I):
            return "Military / Federal Pay"
        if re.search(r"\b(dividend|share\s+dividend|nfcu\s+dividend|interest\s+paid|apy\s+interest)\b", haystack, re.I):
            return "Credit Union Dividend"
        if re.search(r"\b(payroll|direct\s+deposit|salary|paycheck|adp|gusto|workday)\b", haystack, re.I):
            return "Paycheck"
        return "Income"
