"""
Smart Merchant Normalization, Plaid PFC Taxonomy & Auto-Categorization Engine.

Combines:
1. User-learned merchant rules (highest priority: fix once, remembered forever).
2. Plaid / bank-feed `personal_finance_category` (`raw_category`) resolution into a
   clean human-readable 19-category taxonomy (separating terminal-level MCCs such as
   AAFES Gas vs. AAFES Class Six vs. AAFES Exchange, and isolating financial drag).
3. Rich built-in merchant pattern rules when no provider category is present.
4. Explicit low-confidence `"Uncategorized"` fallback (`needs_review = True`) so
   unknown merchants never silently pollute discretionary lifestyle spend.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

_STRIP_SUFFIXES = re.compile(
    r"(\b(inc|llc|corp|co|ltd|store|pos|debit|credit|purchase|payment|autopay|ach|dbt|crd|chk)\b"
    r"|#\d+|\*\w+|\b\d{4,}\b)",
    re.IGNORECASE,
)
_NON_ALNUM = re.compile(r"[^a-z0-9\s]+")
_MULTI_SPACE = re.compile(r"\s+")
_PFC_SEP = re.compile(r"[_\-/]+")

FINANCIAL_DRAG_CATEGORIES: FrozenSet[str] = frozenset(
    {
        "Interest & Bank Fees",
        "Cash Advances & BNPL",
    }
)

CANONICAL_CATEGORY_TO_JAR: Dict[str, str] = {
    # Bills & Essentials (`bills`)
    "Housing & Utilities": "bills",
    "Phone & Internet": "bills",
    "Insurance": "bills",
    "Auto & Loan Payments": "bills",
    "Cash Advances & BNPL": "bills",
    "Interest & Bank Fees": "bills",
    "Storage & Subscriptions": "bills",
    "Software, AI & Services": "bills",
    # Daily Living (`daily`)
    "Groceries & Superstores": "daily",
    "Gas, Auto & Transit": "daily",
    "Medical & Pharmacy": "daily",
    "Personal, Home & Travel": "daily",
    # Dining & Lifestyle (`fun`)
    "Dining & Restaurants": "fun",
    "Fast Food & Coffee": "fun",
    "Bars, Alcohol & Smoke": "fun",
    "Convenience & Snacks": "fun",
    "Clothing & Apparel": "fun",
    "Entertainment & Gaming": "fun",
    "Shopping & Retail": "fun",
    # Safety & Invest
    "Savings & Buffer": "safety",
    "Investing & Trading": "invest",
}

_CANONICAL_LOWER_MAP: Dict[str, Tuple[str, str]] = {
    label.lower(): (slug, label) for label, slug in CANONICAL_CATEGORY_TO_JAR.items()
}

# Legacy built-in subcategory aliases mapped cleanly onto the canonical taxonomy for analytics/humanization
_LEGACY_SUBCATEGORY_ALIASES: Dict[str, str] = {
    "housing": "Housing & Utilities",
    "utilities": "Housing & Utilities",
    "internet & phone": "Phone & Internet",
    "debt minimum": "Auto & Loan Payments",
    "subscriptions": "Software, AI & Services",
    "groceries & household": "Groceries & Superstores",
    "fuel & transit": "Gas, Auto & Transit",
    "transit": "Gas, Auto & Transit",
    "transport": "Gas, Auto & Transit",
    "health & meds": "Medical & Pharmacy",
    "pet care": "Personal, Home & Travel",
    "personal & home": "Personal, Home & Travel",
    "household supplies": "Groceries & Superstores",
    "coffee & treats": "Fast Food & Coffee",
    "dining & treats": "Fast Food & Coffee",
    "dining & delivery": "Dining & Restaurants",
    "shopping & hobbies": "Shopping & Retail",
    "travel & experiences": "Personal, Home & Travel",
}

# Exact normalized Plaid `personal_finance_category` strings -> (jar_slug, canonical_label, confidence)
_PFC_EXACT_MAP: Dict[str, Tuple[str, str, float]] = {
    # Loan payments & debt service
    "loan payments car payment": ("bills", "Auto & Loan Payments", 0.96),
    "loan payments student loan payment": ("bills", "Auto & Loan Payments", 0.96),
    "loan payments personal loan payment": ("bills", "Auto & Loan Payments", 0.94),
    "loan payments credit card payment": ("bills", "Auto & Loan Payments", 0.92),
    "loan payments other payment": ("bills", "Auto & Loan Payments", 0.91),
    "loan payments mortgage payment": ("bills", "Housing & Utilities", 0.96),
    # Financial drag: Cash advances, BNPL, interest & bank fees
    "loan payments cash advances": ("bills", "Cash Advances & BNPL", 0.95),
    "loan payments cash advances and loans": ("bills", "Cash Advances & BNPL", 0.95),
    "loan payments bnpl": ("bills", "Cash Advances & BNPL", 0.94),
    "loan payments buy now pay later": ("bills", "Cash Advances & BNPL", 0.94),
    "bank fees interest charge": ("bills", "Interest & Bank Fees", 0.97),
    "bank fees atm fees": ("bills", "Interest & Bank Fees", 0.96),
    "bank fees foreign transaction fees": ("bills", "Interest & Bank Fees", 0.96),
    "bank fees insufficient funds": ("bills", "Interest & Bank Fees", 0.96),
    "bank fees overdraft fees": ("bills", "Interest & Bank Fees", 0.96),
    "bank fees late fees": ("bills", "Interest & Bank Fees", 0.96),
    "bank fees other bank fees": ("bills", "Interest & Bank Fees", 0.95),
    # Insurance, telecom, utilities, storage & services
    "general services insurance": ("bills", "Insurance", 0.95),
    "rent and utilities telephone": ("bills", "Phone & Internet", 0.95),
    "rent and utilities internet and cable": ("bills", "Phone & Internet", 0.95),
    "rent and utilities rent": ("bills", "Housing & Utilities", 0.96),
    "rent and utilities gas and electricity": ("bills", "Housing & Utilities", 0.95),
    "rent and utilities water": ("bills", "Housing & Utilities", 0.95),
    "rent and utilities sewage and waste management": ("bills", "Housing & Utilities", 0.95),
    "rent and utilities other utilities": ("bills", "Housing & Utilities", 0.94),
    "general services storage": ("bills", "Storage & Subscriptions", 0.93),
    "general services other general services": ("bills", "Software, AI & Services", 0.89),
    "general services accounting and financial planning": ("bills", "Software, AI & Services", 0.89),
    "general services postage and shipping": ("bills", "Software, AI & Services", 0.88),
    "general services consulting and legal": ("bills", "Software, AI & Services", 0.88),
    "general services education": ("bills", "Software, AI & Services", 0.88),
    # Daily necessities: Gas/auto, medical, groceries, personal/home
    "transportation gas": ("daily", "Gas, Auto & Transit", 0.94),
    "general services automotive": ("daily", "Gas, Auto & Transit", 0.93),
    "transportation taxis and ride shares": ("daily", "Gas, Auto & Transit", 0.92),
    "transportation parking": ("daily", "Gas, Auto & Transit", 0.92),
    "transportation public transit": ("daily", "Gas, Auto & Transit", 0.92),
    "transportation tolls": ("daily", "Gas, Auto & Transit", 0.92),
    "transportation bikes and scooters": ("daily", "Gas, Auto & Transit", 0.90),
    "transportation other transportation": ("daily", "Gas, Auto & Transit", 0.90),
    "medical pharmacies and supplements": ("daily", "Medical & Pharmacy", 0.94),
    "medical primary care": ("daily", "Medical & Pharmacy", 0.95),
    "medical dental care": ("daily", "Medical & Pharmacy", 0.95),
    "medical eye care": ("daily", "Medical & Pharmacy", 0.95),
    "medical nursing care": ("daily", "Medical & Pharmacy", 0.95),
    "medical veterinary services": ("daily", "Medical & Pharmacy", 0.93),
    "medical other medical": ("daily", "Medical & Pharmacy", 0.93),
    "food and drink groceries": ("daily", "Groceries & Superstores", 0.94),
    "general merchandise superstores": ("daily", "Groceries & Superstores", 0.91),
    "general merchandise discount stores": ("daily", "Groceries & Superstores", 0.90),
    # Dining & Lifestyle (`fun`)
    "food and drink restaurant": ("fun", "Dining & Restaurants", 0.93),
    "food and drink coffee": ("fun", "Fast Food & Coffee", 0.92),
    "food and drink fast food": ("fun", "Fast Food & Coffee", 0.92),
    "food and drink other food and drink": ("fun", "Fast Food & Coffee", 0.89),
    "food and drink beer wine and liquor": ("fun", "Bars, Alcohol & Smoke", 0.92),
    "general merchandise tobacco and vape": ("fun", "Bars, Alcohol & Smoke", 0.92),
    "general merchandise convenience stores": ("fun", "Convenience & Snacks", 0.90),
    "food and drink vending machines": ("fun", "Convenience & Snacks", 0.91),
    "general merchandise clothing and accessories": ("fun", "Clothing & Apparel", 0.91),
    "entertainment video games": ("fun", "Entertainment & Gaming", 0.92),
    "entertainment sporting events amusement parks and museums": ("fun", "Entertainment & Gaming", 0.91),
    "entertainment music and audio": ("fun", "Entertainment & Gaming", 0.91),
    "entertainment tv and movies": ("fun", "Entertainment & Gaming", 0.91),
    "entertainment casinos and gambling": ("fun", "Entertainment & Gaming", 0.90),
    "entertainment other entertainment": ("fun", "Entertainment & Gaming", 0.89),
    "general merchandise online marketplaces": ("fun", "Shopping & Retail", 0.89),
    "general merchandise department stores": ("fun", "Shopping & Retail", 0.89),
    "general merchandise electronics": ("fun", "Shopping & Retail", 0.89),
    "general merchandise sporting goods": ("fun", "Shopping & Retail", 0.89),
    "general merchandise office supplies": ("fun", "Shopping & Retail", 0.88),
    "general merchandise gifts and novelties": ("fun", "Shopping & Retail", 0.88),
    "general merchandise bookstores and newsstands": ("fun", "Shopping & Retail", 0.88),
    "general merchandise pet supplies": ("daily", "Personal, Home & Travel", 0.88),
    "general merchandise other general merchandise": ("fun", "Shopping & Retail", 0.87),
}

_PFC_PREFIX_RULES: List[Tuple[str, str, str, float]] = [
    ("bank fees", "bills", "Interest & Bank Fees", 0.95),
    ("loan payments", "bills", "Auto & Loan Payments", 0.91),
    ("rent and utilities", "bills", "Housing & Utilities", 0.93),
    ("medical", "daily", "Medical & Pharmacy", 0.93),
    ("transportation", "daily", "Gas, Auto & Transit", 0.91),
    ("personal care", "daily", "Personal, Home & Travel", 0.87),
    ("home improvement", "daily", "Personal, Home & Travel", 0.87),
    ("travel", "daily", "Personal, Home & Travel", 0.87),
    ("government and non profit", "daily", "Personal, Home & Travel", 0.86),
    ("entertainment", "fun", "Entertainment & Gaming", 0.89),
    ("general merchandise", "fun", "Shopping & Retail", 0.86),
    ("food and drink", "fun", "Fast Food & Coffee", 0.87),
    ("general services", "bills", "Software, AI & Services", 0.86),
]


def _normalize_pfc_key(raw: str) -> str:
    cleaned = _PFC_SEP.sub(" ", (raw or "").strip().lower())
    return _MULTI_SPACE.sub(" ", cleaned).strip()


def resolve_provider_category(raw_category: str) -> Optional[Tuple[str, str, float]]:
    """Resolve a Plaid/provider category string to `(jar_slug, canonical_label, confidence)`."""
    key = _normalize_pfc_key(raw_category)
    if not key:
        return None
    if key in _PFC_EXACT_MAP:
        return _PFC_EXACT_MAP[key]
    for prefix, slug, label, conf in _PFC_PREFIX_RULES:
        if key == prefix or key.startswith(prefix + " "):
            return slug, label, conf
    return None


def is_raw_provider_category(subcategory: str) -> bool:
    """Return True when `subcategory` is an un-humanized Plaid/provider token."""
    raw = (subcategory or "").strip()
    if not raw or raw in CANONICAL_CATEGORY_TO_JAR:
        return False
    return resolve_provider_category(raw) is not None


def humanize_category_label(raw_subcat: str) -> str:
    """
    Convert any raw Plaid PFC token or legacy ChronoX subcategory into a clean,
    canonical human-readable category label.
    """
    cleaned = (raw_subcat or "").strip()
    if not cleaned:
        return "Uncategorized"
    if cleaned in CANONICAL_CATEGORY_TO_JAR or cleaned == "Uncategorized":
        return cleaned
    low = cleaned.lower()
    if low in _CANONICAL_LOWER_MAP:
        return _CANONICAL_LOWER_MAP[low][1]
    if low in _LEGACY_SUBCATEGORY_ALIASES:
        return _LEGACY_SUBCATEGORY_ALIASES[low]
    pfc = resolve_provider_category(cleaned)
    if pfc is not None:
        return pfc[1]
    if "_" in cleaned or cleaned.islower() or cleaned.isupper():
        return _MULTI_SPACE.sub(" ", cleaned.replace("_", " ")).strip().title()
    return cleaned


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
    (re.compile(r"\b(interest charge|cash adv|transaction fee|intl transaction|overdraft fee|atm fee|late fee|finance charge)\b", re.I), "bills", "Interest & Bank Fees", 0.96, False, True),
    (re.compile(r"\b(brigit|earnin|dave\b(?!\s*&\s*buster)|klover|albert instant|afterpay|klarna|affirm|sezzle|zip pay)\b", re.I), "bills", "Cash Advances & BNPL", 0.94, False, True),
    (re.compile(r"\b(rent|mortgage|apartment|property mgmt|landlord|hoa)\b", re.I), "bills", "Housing & Utilities", 0.96, True, True),
    (re.compile(r"\b(electric|power|water|sewer|gas bill|coned|pge|duke energy|xcel|utility|utilities)\b", re.I), "bills", "Housing & Utilities", 0.95, True, True),
    (re.compile(r"\b(comcast|xfinity|verizon|at&t|att|t-mobile|tmobile|spectrum|google fiber|starlink|internet|phone bill)\b", re.I), "bills", "Phone & Internet", 0.94, True, True),
    (re.compile(r"\b(geico|progressive|state farm|allstate|usaa|tricare|lemonade|insurance|health ins|dental|vision)\b", re.I), "bills", "Insurance", 0.94, True, True),
    (re.compile(r"\b(carmax|student loan|department of education|dept of ed|navient|nelnet|mohela|aidvantage|edfinancial|auto loan|car payment|minimum payment|nfcu auto|navy federal loan|ally auto|exeter|santander)\b", re.I), "bills", "Auto & Loan Payments", 0.95, True, True),
    (re.compile(r"\b(extra space|public storage|cubesmart|life storage|u-haul storage)\b", re.I), "bills", "Storage & Subscriptions", 0.93, True, True),
    (re.compile(r"\b(anthropic|chatgpt|openai|claude|opencode|cursor|github|notion|dropbox|adobe|netflix|spotify|hulu|disney\+?|max|hbo|apple\.com/bill|icloud|youtube premium|amazon prime|audible|xbox|playstation|nintendo|gym|planet fitness|la fitness|equinox)\b", re.I), "bills", "Software, AI & Services", 0.90, True, False),

    # --- Daily Living (Flexible Necessities) ---
    (re.compile(r"\b(trader joe|whole foods|kroger|safeway|publix|aldi|lidl|wegmans|heb|brookshire|costco|sam'?s club|sprouts|commissary|deca|navy exchange|nex\b|aafes|grocery|groceries|supermarket|market|walmart|target|meijer|winco)\b", re.I), "daily", "Groceries & Superstores", 0.92, False, True),
    (re.compile(r"\b(shell|chevron|exxon|mobil|bp|arco|sunoco|circle k|wawa|sheetz|bucee|buc-ee|gas station|fuel|supercharger|ev charging|valvoline|jiffy lube|autozone|o'?reilly auto|uber|lyft|metro|transit|subway|mta|bart|parking|toll|ezpass)\b", re.I), "daily", "Gas, Auto & Transit", 0.92, False, True),
    (re.compile(r"\b(cvs|walgreens|rite aid|5 star nutrition|gnc\b|vitamin shoppe|pharmacy|doctor|clinic|hospital|therapy|therapist|psychiatry|adderall|vyvanse|rx|medical|urgent care)\b", re.I), "daily", "Medical & Pharmacy", 0.95, False, True),
    (re.compile(r"\b(petco|petsmart|chewy|vet|veterinary|laundry|dry clean|haircut|barber|home depot|lowe'?s|ikea|hardware|airbnb|hotel|marriott|hilton|delta|united|american air|southwest|flight|vacation)\b", re.I), "daily", "Personal, Home & Travel", 0.88, False, True),

    # --- Dining & Lifestyle (`fun`) ---
    (re.compile(r"\b(starbucks|dunkin|peet'?s|blue bottle|dutch bros|black rifle|coffee|cafe|espresso|boba|tea|chick-fil-a|mcdonald|wendy|taco bell|whataburger|sonic drive|popeyes|kfc|subway|panera)\b", re.I), "fun", "Fast Food & Coffee", 0.92, False, False),
    (re.compile(r"\b(doordash|ubereats|uber eats|grubhub|instacart|postmates|seamless|chipotle|sweetgreen|shake shack|five guys|antojitos|pizza|sushi|thai|ramen|burger|restaurant|diner|grill|bistro|steakhouse|bakery)\b", re.I), "fun", "Dining & Restaurants", 0.91, False, False),
    (re.compile(r"\b(liquor|wine|beer|bar\b|pub\b|brewery|taproom|smoke shop|vape|tobacco|cigar)\b", re.I), "fun", "Bars, Alcohol & Smoke", 0.91, False, False),
    (re.compile(r"\b(7-eleven|7 eleven|365 retail|vending|convenience|snack)\b", re.I), "fun", "Convenience & Snacks", 0.90, False, False),
    (re.compile(r"\b(nike|adidas|uniqlo|zara|lululemon|old navy|gap\b|tj maxx|marshalls|ross\b|foot locker|apparel|clothing)\b", re.I), "fun", "Clothing & Apparel", 0.90, False, False),
    (re.compile(r"\b(steam|epic games|bowlero|dave\s*&\s*buster|cinema|movie|amc|regal|concert|ticketmaster|spotify live|gaming|arcade|bowling)\b", re.I), "fun", "Entertainment & Gaming", 0.90, False, False),
    (re.compile(r"\b(best buy|micro center|microcenter|apple store|amazon|amzn|etsy|ebay|sephora|ulta|rei|dick'?s sporting|academy sports|lego|books|barnes|hobby|craft)\b", re.I), "fun", "Shopping & Retail", 0.86, False, False),

    # --- Safety & Future ---
    (re.compile(r"\b(emergency|savings|ally bank|marcus|sofi savings|hysa|sinking fund|debt payoff|extra payment)\b", re.I), "safety", "Savings & Buffer", 0.94, False, True),

    # --- ChronoX Invest Vault ---
    (re.compile(r"\b(vanguard|fidelity|schwab|robinhood|alpaca|ibkr|interactive brokers|coinbase|kraken|etrade|chronox|ira|401k|roth|brokerage|invest)\b", re.I), "invest", "Investing & Trading", 0.95, False, False),
]


class TransactionCategorizer:
    """Categorizes transactions into the 5-Jar envelope model and canonical category taxonomy."""

    def categorize(
        self,
        merchant: str,
        memo: str,
        jars_by_slug: Dict[str, Dict[str, Any]],
        learned_rules: Dict[str, Dict[str, Any]],
        *,
        raw_category: str = "",
    ) -> Tuple[Optional[int], str, float, bool]:
        """
        Return `(jar_id, subcategory, confidence, needs_review)`.

        Priority:
        1. User-learned merchant rules (`confidence = 0.99`, `needs_review = False`).
        2. Plaid / bank-feed `personal_finance_category` (`raw_category`) or existing
           canonical category label (preserving idempotency on reclassification).
        3. Built-in merchant/keyword pattern matching.
        4. Low-confidence `"Uncategorized"` fallback (`confidence = 0.45`, `needs_review = True`)
           without labeling unknown spend as discretionary fun.
        """
        norm = normalize_merchant(merchant)
        if norm in learned_rules:
            rule = learned_rules[norm]
            learned_sub = rule.get("subcategory") or ""
            clean_learned = (
                humanize_category_label(learned_sub)
                if is_raw_provider_category(learned_sub)
                else (learned_sub or "Learned")
            )
            return int(rule["jar_id"]), clean_learned, 0.99, False

        if raw_category:
            pfc = resolve_provider_category(raw_category)
            if pfc is not None:
                slug, clean_label, conf = pfc
                jar = jars_by_slug.get(slug)
                if jar:
                    return int(jar["id"]), clean_label, conf, conf < 0.75

            canon_key = raw_category.strip().lower()
            if canon_key in _CANONICAL_LOWER_MAP:
                slug, clean_label = _CANONICAL_LOWER_MAP[canon_key]
                jar = jars_by_slug.get(slug)
                if jar:
                    return int(jar["id"]), clean_label, 0.92, False

        # Match merchant name first so causal memos (e.g. "Coffee" + memo "Red-eye flight")
        # never hijack a clear merchant match; only fall back to memo when merchant is generic.
        for candidate in (merchant.strip(), memo.strip()):
            if not candidate:
                continue
            for pattern, slug, subcat, conf, _sub_hint, _ess_hint in _BUILTIN_RULES:
                if pattern.search(candidate):
                    jar = jars_by_slug.get(slug)
                    if jar:
                        return int(jar["id"]), subcat, conf, conf < 0.75

        fallback_jar = (
            jars_by_slug.get("daily")
            or jars_by_slug.get("fun")
            or next(iter(jars_by_slug.values()), None)
        )
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
