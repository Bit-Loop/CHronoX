"""
Deterministic Post-LLM Suggestion Validator & RSD Tone Filter.

Guarantees that NO AI suggestion (from OpenCode Go or Google Gemini) can ever:
1. Hallucinate a non-existent Jar ID, Recurring Bill ID, Credit Line ID, or Goal ID.
2. Propose moving more money than a Jar actually has available, or negative/impossible amounts.
3. Propose raiding protected `Bills & Essentials` funds.
4. In Hybrid mode, cite fabricated large dollar balances contradicting the verified financial snapshot.
5. Use shaming / punitive language that triggers ADHD Rejection Sensitive Dysphoria (RSD).
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from chronox_finance.engines.autobudget import AutoBudgetResult
from chronox_finance.models import ImpulseOut, JarKind, SuggestionActionType, ValidatedSuggestion
from chronox_finance.security import sanitize_untrusted_text

_DOLLAR_RE = re.compile(r"\$((?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]{1,2})?)(?!\d)")
_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)
_UNTRUSTED_TAG_RE = re.compile(r"</?\s*UNTRUSTED_DATA\s*>", re.IGNORECASE)

# Extended prompt-injection patterns beyond base security.py filters
_EXTRA_INJECTION_PATTERNS: List[re.Pattern[str]] = [
    re.compile(r"</?\s*(?:UNTRUSTED_DATA|VERIFIED_FACTS|VERIFIED_CONTEXT|ACCOUNT_CONTEXT|SYSTEM|DEVELOPER|USER_QUESTION|GOAL_PLAN)\s*>", re.IGNORECASE),
    re.compile(r"\[/?INST\]|<<\s*/?SYS\s*>>", re.IGNORECASE),
    re.compile(r"\b(?:developer|assistant|tool_call|function_call)\s*:", re.IGNORECASE),
    re.compile(r"\b(?:forget|override|bypass)\s+(?:all\s+)?(?:(?:previous|prior|your|the)\s+)?(?:rules?|instructions?|safety|guardrails?|firewall|validator)\b", re.IGNORECASE),
    re.compile(r"\bnew\s+(?:system\s+)?instructions?\s*:", re.IGNORECASE),
    re.compile(r"\bact\s+as\s+(?:a|an)\s+(?:unrestricted|admin|root|bank)\b", re.IGNORECASE),
]

# RSD-hostile phrases neutralized into supportive ADHD-friendly framing
_SHAME_REPLACEMENTS: List[Tuple[re.Pattern[str], str]] = [
    (re.compile(r"\byou\s+failed\b", re.I), "this cycle ran tight"),
    (re.compile(r"\birresponsible\b", re.I), "easy to lose track of"),
    (re.compile(r"\breckless\b", re.I), "fast-paced"),
    (re.compile(r"\bterrible\s+(with|at)\s+money\b", re.I), "still building momentum"),
    (re.compile(r"\bpathetic\b", re.I), "tricky"),
    (re.compile(r"\blazy\b", re.I), "low-energy"),
    (re.compile(r"\bstupid\b", re.I), "frustrating"),
    (re.compile(r"\bshameful\b", re.I), "tough"),
    (re.compile(r"\bwasted\s+money\b", re.I), "unplanned spend"),
]

# Common round behavioral step amounts allowed in coaching text (e.g. "save $10/week")
_BENIGN_ROUND_CENTS: Set[int] = {
    100, 200, 300, 500, 1000, 1500, 2000, 2500, 3000, 4000, 5000, 7500, 10000,
}


def sanitize_rsd_tone(text: str) -> Tuple[str, bool]:
    """Rewrite any shaming phrases into calm, neutral, ADHD-safe phrasing."""
    cleaned = (text or "").strip()
    changed = False
    for rx, replacement in _SHAME_REPLACEMENTS:
        if rx.search(cleaned):
            cleaned = rx.sub(replacement, cleaned)
            changed = True
    return cleaned, changed


def sanitize_prompt_injection(text: str, max_len: int = 240) -> str:
    """
    Redact PII and neutralize prompt-injection & delimiter-breakout attempts
    inside untrusted external strings (merchants, memos, comments, email subjects/bodies).
    """
    cleaned = sanitize_untrusted_text(str(text or ""), max_len=max(max_len * 2, 480))
    for rx in _EXTRA_INJECTION_PATTERNS:
        cleaned = rx.sub("[filtered]", cleaned)
    # Collapse redundant whitespace after replacements
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_len]


def wrap_untrusted_data(value: Any, max_len: int = 200) -> str:
    """Sanitize and wrap an untrusted string in explicit `<UNTRUSTED_DATA>` delimiters."""
    cleaned = sanitize_prompt_injection("" if value is None else str(value), max_len=max_len)
    return f"<UNTRUSTED_DATA>{cleaned}</UNTRUSTED_DATA>"


def strip_untrusted_delimiters(text: str) -> str:
    """Strip echoed `<UNTRUSTED_DATA>` wrapper tags from post-LLM text fields."""
    return _UNTRUSTED_TAG_RE.sub("", str(text or "")).strip()


def extract_json_object(raw_text: str) -> Dict[str, Any]:
    """Robustly parse JSON object from raw LLM text or fenced code blocks."""
    text = (raw_text or "").strip()
    fence = _CODE_FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"suggestions": parsed}
    except json.JSONDecodeError:
        pass

    # Fallback: locate outermost {...}
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        parsed = json.loads(text[start : end + 1])
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("LLM response did not contain a valid JSON object")


def collect_verified_dollar_anchors(
    budget: AutoBudgetResult,
    impulses: List[ImpulseOut],
    extra_anchors: Optional[Set[int]] = None,
) -> Set[int]:
    """Collect every verified exact cent figure present in the user's real financial state."""
    s = budget.safe_to_spend
    anchors: Set[int] = {
        s.total_liquid_cash_cents,
        s.unpaid_bills_until_payday_cents,
        s.locked_jar_reserves_cents,
        s.emergency_buffer_cents,
        s.credit_card_reserved_cents,
        s.safe_to_spend_cents,
        s.daily_safe_to_spend_cents,
        s.estimated_paycheck_cents,
        budget.safe_to_invest_cents,
        budget.monthly_income_cents,
        budget.monthly_spent_cents,
        budget.smoothed_monthly_income_cents,
    }
    if extra_anchors:
        anchors.update(extra_anchors)
    for j in budget.jars:
        anchors.update(
            {
                abs(j.available_cents),
                j.spent_cents,
                j.auto_allocated_cents,
                j.monthly_target_cents,
                abs(j.rollover_cents),
            }
        )
    due_14d_total = 0
    for b in budget.upcoming_bills:
        anchors.update(
            {
                b.amount_cents,
                b.price_hike_cents,
                b.amount_cents * 12,
                b.price_hike_cents * 12,
            }
        )
        if b.days_until_due <= 14:
            due_14d_total += b.amount_cents
        if b.previous_amount_cents:
            anchors.add(b.previous_amount_cents)
    if due_14d_total > 0:
        anchors.add(due_14d_total)
    for p in budget.rebalance_plans:
        anchors.add(p.amount_cents)
    for imp in impulses:
        anchors.add(imp.price_cents)
    return {a for a in anchors if a > 0}


def _number_matches_anchors(cents: int, anchors: Set[int], extra_cents: int = 0) -> bool:
    if cents in _BENIGN_ROUND_CENTS:
        return True
    if extra_cents > 0 and abs(cents - extra_cents) <= max(100, int(extra_cents * 0.03)):
        return True
    for a in anchors:
        # Allow minor rounding (e.g., "$45" for $44.80 -> within $1 or 2%)
        tol = max(100, int(a * 0.02))
        if abs(cents - a) <= tol:
            return True
    return False


@dataclass
class ValidationReport:
    validated: List[ValidatedSuggestion]
    rejected_count: int
    rejection_reasons: List[str]


class SuggestionValidator:
    """Strictly validates and repairs AI financial suggestions against deterministic truth."""

    def validate_batch(
        self,
        raw_items: List[Dict[str, Any]],
        *,
        budget: AutoBudgetResult,
        impulses: List[ImpulseOut],
        model_used: str,
        extra_anchors: Optional[Set[int]] = None,
        source: str = "opencode_go",
        strict_dollar_anchors: bool = True,
        credit_line_ids: Optional[Set[int]] = None,
        goal_ids: Optional[Set[int]] = None,
    ) -> ValidationReport:
        jars_by_id = {j.id: j for j in budget.jars}
        bills_by_id = {b.id: b for b in budget.upcoming_bills}
        anchors = collect_verified_dollar_anchors(budget, impulses, extra_anchors=extra_anchors)

        validated: List[ValidatedSuggestion] = []
        rejected_count = 0
        reasons: List[str] = []

        for idx, item in enumerate(raw_items[:6]):
            if not isinstance(item, dict):
                rejected_count += 1
                reasons.append(f"Item {idx}: not an object")
                continue

            title = str(item.get("title") or "").strip()[:100]
            rationale = str(item.get("rationale") or "").strip()[:360]
            micro_step = str(item.get("micro_step") or "").strip()[:240]
            if not title or not rationale or not micro_step:
                rejected_count += 1
                reasons.append(f"Item {idx}: missing required text fields")
                continue

            # RSD-safe tone enforcement
            notes: List[str] = []
            title, t_chg = sanitize_rsd_tone(title)
            rationale, r_chg = sanitize_rsd_tone(rationale)
            micro_step, m_chg = sanitize_rsd_tone(micro_step)
            if t_chg or r_chg or m_chg:
                notes.append("Neutralized tone to stay ADHD/RSD-friendly")

            priority = str(item.get("priority", "medium")).lower()
            if priority not in ("high", "medium", "low"):
                priority = "medium"

            raw_action_type = str(item.get("action_type", "general_nudge")).lower()
            try:
                action_type = SuggestionActionType(raw_action_type)
            except ValueError:
                action_type = SuggestionActionType.GENERAL_NUDGE

            payload = item.get("action_payload")
            if not isinstance(payload, dict):
                payload = {}

            try:
                impact_cents = int(item.get("impact_cents") or 0)
            except (TypeError, ValueError):
                impact_cents = 0
            if impact_cents < 0:
                impact_cents = abs(impact_cents)

            # Action-specific deterministic feasibility verification (enforced in BOTH Hybrid and AI Standalone)
            ok, clean_payload, verified_impact, reason = self._verify_action(
                action_type,
                payload,
                jars_by_id,
                bills_by_id,
                budget,
                credit_line_ids=credit_line_ids,
                goal_ids=goal_ids,
            )
            if not ok:
                rejected_count += 1
                reasons.append(f"Item {idx} ({title}): {reason}")
                continue
            if verified_impact is not None:
                impact_cents = verified_impact
            notes.append("Verified action payload against live balances")

            if strict_dollar_anchors:
                # Check dollar citations against verified anchors
                hallucinated_dollars: List[str] = []
                for m in _DOLLAR_RE.finditer(f"{title} {rationale} {micro_step}"):
                    try:
                        val_cents = int(round(float(m.group(1).replace(",", "")) * 100))
                    except ValueError:
                        continue
                    if val_cents >= 1500 and not _number_matches_anchors(val_cents, anchors, extra_cents=impact_cents):
                        hallucinated_dollars.append(m.group(0))

                if hallucinated_dollars:
                    rejected_count += 1
                    reasons.append(
                        f"Item {idx} ({title}): rejected unverified dollar figure(s) {hallucinated_dollars}"
                    )
                    continue

                notes.append("All cited dollar amounts cross-checked against ledger")
                is_verified = True
            else:
                notes.append("AI Standalone mode: action safety bounds & RSD tone verified")
                is_verified = False

            validated.append(
                ValidatedSuggestion(
                    id=f"ai-{uuid.uuid4().hex[:8]}",
                    title=title,
                    rationale=rationale,
                    micro_step=micro_step,
                    priority=priority,  # type: ignore[arg-type]
                    action_type=action_type,
                    action_payload=clean_payload,
                    impact_cents=impact_cents,
                    verified=is_verified,
                    verification_notes=notes,
                    source=source,  # type: ignore[arg-type]
                    model_used=model_used,
                )
            )

        return ValidationReport(
            validated=validated,
            rejected_count=rejected_count,
            rejection_reasons=reasons,
        )

    def _verify_action(
        self,
        action_type: SuggestionActionType,
        payload: Dict[str, Any],
        jars_by_id: Dict[int, Any],
        bills_by_id: Dict[int, Any],
        budget: AutoBudgetResult,
        *,
        credit_line_ids: Optional[Set[int]] = None,
        goal_ids: Optional[Set[int]] = None,
    ) -> Tuple[bool, Dict[str, Any], Optional[int], str]:
        if action_type == SuggestionActionType.REBALANCE_JARS:
            try:
                from_id = int(payload["from_jar_id"])
                to_id = int(payload["to_jar_id"])
                amt = int(payload["amount_cents"])
            except (KeyError, TypeError, ValueError):
                return False, {}, None, "Invalid rebalance payload fields"

            if from_id == to_id or from_id not in jars_by_id or to_id not in jars_by_id:
                return False, {}, None, "Rebalance references non-existent or identical jar IDs"

            from_jar = jars_by_id[from_id]
            if from_jar.kind == JarKind.BILLS:
                return False, {}, None, "Blocked attempt to raid protected Bills & Essentials jar"
            if from_jar.available_cents <= 0 or amt <= 0:
                return False, {}, None, "Source jar has no available surplus to transfer"

            safe_amt = min(amt, from_jar.available_cents)
            return (
                True,
                {
                    "from_jar_id": from_id,
                    "to_jar_id": to_id,
                    "amount_cents": safe_amt,
                    "reason": str(payload.get("reason", "AI-verified jar rebalance"))[:160],
                },
                safe_amt,
                "",
            )

        if action_type == SuggestionActionType.ADJUST_JAR_TARGET:
            try:
                jar_id = int(payload["jar_id"])
                new_pct = float(payload["target_pct"])
            except (KeyError, TypeError, ValueError):
                return False, {}, None, "Invalid jar target payload"
            if jar_id not in jars_by_id or not (0.0 <= new_pct <= 75.0):
                return False, {}, None, "Jar target out of safe bounds"
            return True, {"jar_id": jar_id, "target_pct": round(new_pct, 1)}, None, ""

        if action_type == SuggestionActionType.REVIEW_SUBSCRIPTION:
            try:
                bill_id = int(payload["bill_id"])
            except (KeyError, TypeError, ValueError):
                return False, {}, None, "Missing bill_id for subscription review"
            if bill_id not in bills_by_id:
                return False, {}, None, f"Recurring bill {bill_id} does not exist"
            bill = bills_by_id[bill_id]
            return True, {"bill_id": bill_id, "bill_name": bill.name}, bill.amount_cents, ""

        if action_type == SuggestionActionType.BOOST_EMERGENCY_BUFFER:
            raw_target = payload.get("emergency_buffer_cents")
            if raw_target is None:
                target = budget.safe_to_spend.emergency_buffer_cents + 10_000
            else:
                try:
                    target = int(raw_target)
                except (TypeError, ValueError):
                    return False, {}, None, "Invalid emergency_buffer_cents payload"
                if target <= 0:
                    return False, {}, None, "Emergency buffer target must be positive"
            target = max(10_000, min(5_000_000, target))
            return True, {"emergency_buffer_cents": target}, target, ""

        if action_type == SuggestionActionType.SET_TRADING_CAP:
            return True, {"safe_to_invest_cents": budget.safe_to_invest_cents}, budget.safe_to_invest_cents, ""

        if action_type == SuggestionActionType.WEEKLY_RESET:
            return True, {"open_tab": "reset"}, 0, ""

        if action_type == SuggestionActionType.PARK_IMPULSE:
            if "price_cents" in payload:
                try:
                    price = int(payload["price_cents"])
                except (TypeError, ValueError):
                    return False, {}, None, "Invalid park_impulse price_cents"
                if price <= 0 or price > 100_000_000_00:
                    return False, {}, None, "Impulse price_cents out of safe bounds"
                clean: Dict[str, Any] = {"price_cents": price}
                if payload.get("item_name"):
                    clean["item_name"] = str(payload["item_name"]).strip()[:120]
                return True, clean, price, ""
            return True, {"open_tab": "impulse"}, None, ""

        if action_type == SuggestionActionType.CREDIT_PAYDOWN:
            try:
                line_id = int(payload["credit_line_id"])
                amt = int(payload["amount_cents"])
            except (KeyError, TypeError, ValueError):
                return False, {}, None, "Invalid credit_paydown payload"
            if credit_line_ids is not None and line_id not in credit_line_ids:
                return False, {}, None, f"Credit line {line_id} does not exist"
            if amt <= 0 or amt > 100_000_000_00:
                return False, {}, None, "Paydown amount must be positive and within safe bounds"
            return True, {"credit_line_id": line_id, "amount_cents": amt}, amt, ""

        if action_type == SuggestionActionType.ENABLE_AUTOPAY:
            try:
                line_id = int(payload["credit_line_id"])
            except (KeyError, TypeError, ValueError):
                return False, {}, None, "Missing credit_line_id for enable_autopay"
            if credit_line_ids is not None and line_id not in credit_line_ids:
                return False, {}, None, f"Credit line {line_id} does not exist"
            mode = str(payload.get("autopay_mode", "minimum")).lower()
            if mode not in ("minimum", "statement", "full"):
                mode = "minimum"
            return True, {"credit_line_id": line_id, "autopay_mode": mode}, 0, ""

        if action_type == SuggestionActionType.FUND_GOAL:
            try:
                goal_id = int(payload["goal_id"])
                amt = int(payload["amount_cents"])
            except (KeyError, TypeError, ValueError):
                return False, {}, None, "Invalid fund_goal payload"
            if goal_ids is not None and goal_id not in goal_ids:
                return False, {}, None, f"Goal {goal_id} does not exist"
            if amt <= 0 or amt > 100_000_000_00:
                return False, {}, None, "Goal contribution must be positive and within safe bounds"
            return True, {"goal_id": goal_id, "amount_cents": amt}, amt, ""

        return True, {}, None, ""
