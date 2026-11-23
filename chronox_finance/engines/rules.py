"""
Firefly III-Inspired Deterministic Automation Rule Engine & Exact-Cent Split Validator.

Capabilities (100% deterministic, exact integer cents, zero float drift):
1. Ordered `rule_groups` -> `automation_rules` evaluation supporting:
   - `strict_and` (ALL triggers match) vs `any` (OR — at least one trigger matches)
   - Per-trigger logical negation (`negate` / `not`)
   - `stop_processing` short-circuiting at both rule and group level
2. Rich trigger library:
   - `merchant` / `memo`: `contains`, `starts_with`, `ends_with`, `exact`, `regex` (ReDoS-guarded)
   - `amount_gt`, `amount_lt`, `amount_between`, `amount_exact` (exact integer cents)
   - `account_id`, `tx_type`, `missing_jar`, `missing_tag`
3. Deterministic action pipeline:
   - `set_jar`, `add_tag`, `link_bill`, `set_memo`, `append_memo`, `allocate_goal`, `split_pct`
   - `split_pct` computes exact integer-cent child splits with remainder penny assigned to last split
     so `SUM(splits.amount_cents) == parent.amount_cents` ALWAYS holds.
4. `dry_run_rule(rule, transactions)` over up to 730-day (2-year) history.
5. `validate_splits(parent_amount_cents, splits)` enforcing non-empty, strictly positive integer
   cents summing exactly to `parent_amount_cents`.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from chronox_finance.models import RuleTestMatchSample, RuleTestResult

MAX_REGEX_PATTERN_LEN = 200
MAX_REGEX_TARGET_LEN = 500
DEFAULT_DRY_RUN_LOOKBACK_DAYS = 730

# Static ReDoS detector: rejects nested quantifiers like (a+)+, (a*)*, (\w+){2,}, (a|aa)+
_NESTED_QUANTIFIER_RE = re.compile(
    r"\([^()]*[+*][^()]*\)\s*(?:[+*?]|\{\d+,?\d*\})"
    r"|\([^()]*\{\d+,\d*\}[^()]*\)\s*(?:[+*?]|\{\d+,?\d*\})"
)
_OVERLAPPING_ALT_QUANT_RE = re.compile(r"\([^()]*\|[^()]*\)\s*(?:[+*]|\{\d+,\d*\})")
_BACKREF_RE = re.compile(r"\\[1-9]")


class SplitValidationError(ValueError):
    """Raised when transaction splits violate exact-sum or positivity invariants."""


class UnsafeRegexError(ValueError):
    """Raised when a rule regex pattern is invalid or potentially catastrophic (ReDoS)."""


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _as_list(val: Any) -> List[Any]:
    if val is None:
        return []
    if isinstance(val, str):
        raw = val.strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    if isinstance(val, (list, tuple)):
        return list(val)
    return []


def _ensure_exact_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SplitValidationError(f"{field_name} must be an exact integer (cents), got {type(value).__name__}")
    return value


def validate_safe_regex(pattern: str) -> re.Pattern[str]:
    """Compile a user/rule regex with strict length and static ReDoS safety guards."""
    if not isinstance(pattern, str) or not pattern:
        raise UnsafeRegexError("Regex pattern must be a non-empty string.")
    if len(pattern) > MAX_REGEX_PATTERN_LEN:
        raise UnsafeRegexError(f"Regex pattern exceeds {MAX_REGEX_PATTERN_LEN} characters.")
    if _NESTED_QUANTIFIER_RE.search(pattern):
        raise UnsafeRegexError("Regex rejected: nested quantifiers risk catastrophic backtracking.")
    if _OVERLAPPING_ALT_QUANT_RE.search(pattern):
        # Allow simple literal word alternations like (uber|lyft) without trailing +, reject quantified alternations
        raise UnsafeRegexError("Regex rejected: quantified alternation risks catastrophic backtracking.")
    if _BACKREF_RE.search(pattern):
        raise UnsafeRegexError("Regex rejected: backreferences are not permitted in automation rules.")
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise UnsafeRegexError(f"Invalid regex pattern: {exc}") from exc


def validate_splits(
    parent_amount_cents: int,
    splits: Sequence[Any],
) -> List[Dict[str, Any]]:
    """
    Validate child splits against parent transaction amount in exact integer cents.

    Enforces:
    - `parent_amount_cents` is a strictly positive integer
    - `splits` is non-empty
    - Every split has strictly positive exact-integer `amount_cents`
    - `SUM(split.amount_cents) == parent_amount_cents` (zero penny drift)
    """
    parent_amt = _ensure_exact_int(parent_amount_cents, "parent_amount_cents")
    if parent_amt <= 0:
        raise SplitValidationError("parent_amount_cents must be strictly positive.")

    split_list = list(splits)
    if not split_list:
        raise SplitValidationError("splits must be non-empty.")

    normalized: List[Dict[str, Any]] = []
    running_sum = 0
    for idx, sp in enumerate(split_list):
        raw_amt = _get(sp, "amount_cents", None)
        amt = _ensure_exact_int(raw_amt, f"splits[{idx}].amount_cents")
        if amt <= 0:
            raise SplitValidationError(f"splits[{idx}].amount_cents must be > 0, got {amt}.")
        running_sum += amt

        jar_id = _get(sp, "jar_id", None)
        goal_id = _get(sp, "goal_id", None)
        memo = str(_get(sp, "memo", "") or "")
        normalized.append(
            {
                "jar_id": int(jar_id) if jar_id is not None else None,
                "goal_id": int(goal_id) if goal_id is not None else None,
                "amount_cents": amt,
                "memo": memo,
            }
        )

    if running_sum != parent_amt:
        diff = running_sum - parent_amt
        raise SplitValidationError(
            f"Split sum ({running_sum} cents) must equal parent_amount_cents ({parent_amt} cents); delta={diff:+d} cents."
        )

    return normalized


def compute_exact_pct_splits(
    parent_amount_cents: int,
    split_specs: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Compute exact integer-cent child splits from percentage specs (`pct` summing to ~100%).

    Any remainder pennies go deterministically to the LAST split so:
    - every split is strictly positive (`>= 1` cent)
    - `SUM(amount_cents) == parent_amount_cents` to the exact cent.
    """
    parent_amt = _ensure_exact_int(parent_amount_cents, "parent_amount_cents")
    if parent_amt <= 0:
        raise SplitValidationError("parent_amount_cents must be strictly positive.")
    specs = list(split_specs)
    if not specs:
        raise SplitValidationError("split_pct requires at least one split definition.")
    if parent_amt < len(specs):
        raise SplitValidationError(
            f"Cannot split {parent_amt} cents into {len(specs)} strictly positive child splits."
        )

    pcts: List[float] = []
    for idx, spec in enumerate(specs):
        raw_pct = _get(spec, "pct", _get(spec, "percent", _get(spec, "percentage", None)))
        if raw_pct is None or isinstance(raw_pct, bool):
            raise SplitValidationError(f"split_pct[{idx}] missing numeric 'pct'.")
        pct_val = float(raw_pct)
        if not math.isfinite(pct_val) or pct_val <= 0.0:
            raise SplitValidationError(f"split_pct[{idx}].pct must be > 0, got {raw_pct}.")
        pcts.append(pct_val)

    total_pct = sum(pcts)
    if abs(total_pct - 100.0) > 0.5:
        raise SplitValidationError(f"split_pct percentages must sum to 100% (got {total_pct:.2f}%).")

    computed: List[Dict[str, Any]] = []
    allocated = 0
    count = len(specs)
    for idx, (spec, pct_val) in enumerate(zip(specs, pcts, strict=True)):
        if idx == count - 1:
            amt = parent_amt - allocated
        else:
            remaining_slots = count - 1 - idx
            raw_floor = int(math.floor((parent_amt * pct_val) / total_pct))
            # Guarantee every split gets >= 1 cent while preserving >= 1 cent per remaining split
            max_for_this = parent_amt - allocated - remaining_slots
            amt = max(1, min(max_for_this, raw_floor))
            allocated += amt

        jar_id = _get(spec, "jar_id", None)
        goal_id = _get(spec, "goal_id", None)
        memo = str(_get(spec, "memo", "") or "")
        computed.append(
            {
                "jar_id": int(jar_id) if jar_id is not None else None,
                "goal_id": int(goal_id) if goal_id is not None else None,
                "amount_cents": amt,
                "memo": memo,
            }
        )

    return validate_splits(parent_amt, computed)


# --- Trigger & Action Evaluation ---


def _tx_tags_lower(tx: Any) -> List[str]:
    raw_tags = _get(tx, "tags", None)
    if raw_tags is None:
        return []
    out: List[str] = []
    for t in raw_tags:
        if isinstance(t, str):
            val = t.strip().lower()
            if val:
                out.append(val)
        elif isinstance(t, dict):
            name = str(t.get("name", "")).strip().lower()
            if name:
                out.append(name)
        else:
            name = str(getattr(t, "name", "")).strip().lower()
            if name:
                out.append(name)
    return out


def _match_text(haystack: str, operator: str, needle: str) -> bool:
    op = operator.strip().lower()
    if op == "regex":
        compiled = validate_safe_regex(needle)
        return bool(compiled.search(haystack[:MAX_REGEX_TARGET_LEN]))
    h_low = haystack.lower()
    n_low = needle.lower()
    if op == "contains":
        return n_low in h_low
    if op == "starts_with":
        return h_low.startswith(n_low)
    if op == "ends_with":
        return h_low.endswith(n_low)
    if op == "exact":
        return h_low == n_low
    return False


def evaluate_trigger(trigger: Any, tx: Any) -> bool:
    """
    Evaluate a single Firefly-style trigger against a transaction.

    Supports both canonical schemas:
    - `{"type": "merchant", "op": "contains", "value": "costco", "not": false}`
    - `{"field": "merchant", "operator": "contains", "value": "costco", "negate": false}`
    - Shorthand triggers like `{"type": "amount_gt", "value": 5000}`,
      `{"type": "amount_between", "min_cents": 1000, "max_cents": 5000}` (or `value: [1000, 5000]`),
      `{"type": "missing_jar"}`, `{"type": "missing_tag", "value": "business"}`.
    """
    t_type = str(
        _get(trigger, "type", None)
        or _get(trigger, "field", None)
        or _get(trigger, "kind", "")
    ).strip().lower()
    op = str(
        _get(trigger, "op", None)
        or _get(trigger, "operator", None)
        or _get(trigger, "match_mode", "contains")
    ).strip().lower()
    val = _get(trigger, "value", None)
    negate = bool(
        _get(trigger, "negate", False)
        or _get(trigger, "not", False)
        or _get(trigger, "is_not", False)
        or _get(trigger, "invert", False)
    )

    matched = False
    if t_type in ("merchant", "description"):
        merchant = str(_get(tx, "merchant", "") or "")
        norm = str(_get(tx, "normalized_merchant", "") or "")
        needle = str(val or "")
        if op == "regex":
            try:
                matched = _match_text(merchant, "regex", needle) or (bool(norm) and _match_text(norm, "regex", needle))
            except UnsafeRegexError:
                matched = False
        else:
            matched = _match_text(merchant, op, needle) or (bool(norm) and _match_text(norm, op, needle))

    elif t_type in ("memo", "notes"):
        memo = str(_get(tx, "memo", "") or "")
        needle = str(val or "")
        try:
            matched = _match_text(memo, op, needle)
        except UnsafeRegexError:
            matched = False

    elif t_type in ("amount_gt", "amount_lt", "amount_exact", "amount_between", "amount"):
        amt = int(_get(tx, "amount_cents", 0) or 0)
        eff_op = t_type if t_type != "amount" else op
        if eff_op in ("amount_gt", "gt", ">"):
            thresh = int(_get(trigger, "value_cents", val if val is not None else 0))
            matched = amt > thresh
        elif eff_op in ("amount_lt", "lt", "<"):
            thresh = int(_get(trigger, "value_cents", val if val is not None else 0))
            matched = amt < thresh
        elif eff_op in ("amount_exact", "exact", "eq", "=="):
            thresh = int(_get(trigger, "value_cents", val if val is not None else 0))
            matched = amt == thresh
        elif eff_op in ("amount_between", "between"):
            lo = _get(trigger, "min_cents", None)
            hi = _get(trigger, "max_cents", None)
            if hi is None:
                hi = _get(trigger, "value_max", None)
            if lo is None:
                if isinstance(val, (list, tuple)) and len(val) == 2:
                    lo, hi = val[0], val[1]
                elif isinstance(val, dict):
                    lo = val.get("min_cents", val.get("min"))
                    hi = val.get("max_cents", val.get("max"))
                elif val is not None and hi is not None:
                    lo = val
            if lo is not None and hi is not None:
                lo_i, hi_i = int(lo), int(hi)
                if lo_i > hi_i:
                    lo_i, hi_i = hi_i, lo_i
                matched = lo_i <= amt <= hi_i

    elif t_type == "account_id":
        tx_acct = _get(tx, "account_id", None)
        if tx_acct is not None and val is not None:
            matched = int(tx_acct) == int(val)

    elif t_type == "tx_type":
        raw_tt = _get(tx, "tx_type", "")
        tt = str(raw_tt.value if hasattr(raw_tt, "value") else raw_tt).strip().lower()
        matched = tt == str(val or "").strip().lower()

    elif t_type == "missing_jar":
        tx_jar = _get(tx, "jar_id", None)
        matched = tx_jar is None

    elif t_type == "missing_tag":
        tags = _tx_tags_lower(tx)
        if val is None or str(val).strip() == "":
            matched = len(tags) == 0
        else:
            matched = str(val).strip().lower() not in tags

    return (not matched) if negate else matched


def evaluate_rule_matches(rule: Any, tx: Any) -> bool:
    """Return True if an active rule's triggers match `tx` under `strict_and` (AND) or `any` (OR)."""
    if not _get(rule, "active", True):
        return False
    triggers = _as_list(_get(rule, "triggers", None))
    if not triggers:
        triggers = _as_list(_get(rule, "triggers_json", None))
    if not triggers:
        return False

    strict_and = bool(_get(rule, "strict_and", True))
    if strict_and:
        return all(evaluate_trigger(tr, tx) for tr in triggers)
    return any(evaluate_trigger(tr, tx) for tr in triggers)


@dataclass
class RuleExecutionResult:
    """Result of evaluating ordered rule groups & rules against a single transaction."""

    matched_rule_ids: List[int] = field(default_factory=list)
    matched_rule_names: List[str] = field(default_factory=list)
    stopped_early: bool = False
    changes: Dict[str, Any] = field(default_factory=dict)
    updated_tx: Dict[str, Any] = field(default_factory=dict)

    @property
    def matched(self) -> bool:
        return len(self.matched_rule_ids) > 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "matched": self.matched,
            "matched_rule_ids": list(self.matched_rule_ids),
            "matched_rule_names": list(self.matched_rule_names),
            "stopped_early": self.stopped_early,
            "changes": dict(self.changes),
            "updated_tx": dict(self.updated_tx),
        }


def _apply_actions_to_state(
    actions: Sequence[Any],
    tx_state: Dict[str, Any],
    changes: Dict[str, Any],
) -> None:
    """Apply a rule's action list onto `tx_state` and accumulate structured `changes`."""
    for action in actions:
        a_type = str(
            _get(action, "kind", None)
            or _get(action, "type", None)
            or _get(action, "action", "")
        ).strip().lower()
        val = _get(action, "value", None)
        params = _get(action, "params", None)
        if not isinstance(params, dict):
            params = {}

        if a_type == "set_jar":
            jar_val = _get(action, "jar_id", params.get("jar_id", val))
            if jar_val is not None:
                jid = int(jar_val)
                tx_state["jar_id"] = jid
                changes["jar_id"] = jid

        elif a_type == "add_tag":
            tag_name = str(_get(action, "tag", params.get("tag", val)) or "").strip()
            if tag_name:
                existing_tags = list(tx_state.get("tags") or [])
                existing_lower = {
                    (t.get("name", "") if isinstance(t, dict) else str(t)).strip().lower()
                    for t in existing_tags
                }
                if tag_name.lower() not in existing_lower:
                    existing_tags.append(tag_name)
                    tx_state["tags"] = existing_tags
                tags_add = changes.setdefault("tags_to_add", [])
                if tag_name.lower() not in {t.lower() for t in tags_add}:
                    tags_add.append(tag_name)

        elif a_type == "link_bill":
            bill_val = _get(action, "bill_id", params.get("bill_id", val))
            if bill_val is not None:
                bid = int(bill_val)
                tx_state["bill_id"] = bid
                changes["linked_bill_id"] = bid

        elif a_type == "set_memo":
            memo_str = str(_get(action, "memo", params.get("memo", val)) or "")
            tx_state["memo"] = memo_str
            changes["memo"] = memo_str

        elif a_type == "append_memo":
            suffix = str(_get(action, "memo", params.get("memo", val)) or "").strip()
            if suffix:
                cur = str(tx_state.get("memo") or "").strip()
                new_memo = f"{cur} | {suffix}" if cur else suffix
                tx_state["memo"] = new_memo
                changes["memo"] = new_memo

        elif a_type == "allocate_goal":
            goal_val = _get(
                action,
                "goal_id",
                params.get("goal_id", val if not isinstance(val, dict) else val.get("goal_id")),
            )
            if goal_val is not None:
                raw_amt = _get(action, "amount_cents", params.get("amount_cents", None))
                if raw_amt is None and isinstance(val, dict):
                    raw_amt = val.get("amount_cents")
                alloc_cents = int(raw_amt) if raw_amt is not None else int(tx_state.get("amount_cents", 0))
                if alloc_cents > 0:
                    allocs = changes.setdefault("goal_allocations", [])
                    allocs.append({"goal_id": int(goal_val), "amount_cents": alloc_cents})

        elif a_type == "split_pct":
            raw_specs = _get(action, "splits", params.get("splits", None))
            if raw_specs is None:
                raw_specs = val
            if isinstance(raw_specs, (list, tuple)) and raw_specs:
                parent_amt = int(tx_state.get("amount_cents", 0))
                if parent_amt >= len(raw_specs):
                    computed_splits = compute_exact_pct_splits(parent_amt, raw_specs)
                    tx_state["splits"] = computed_splits
                    changes["splits"] = computed_splits


class RuleEngine:
    """Ordered Firefly III-inspired deterministic rule evaluator."""

    def _ordered_rules(
        self,
        rules: Sequence[Any],
        rule_groups: Optional[Sequence[Any]] = None,
    ) -> List[Tuple[Optional[Any], List[Any]]]:
        """Group and order active rules by `(group.priority, group.id)` then `(rule.priority, rule.id)`."""
        active_rules = [r for r in rules if _get(r, "active", True)]
        if not rule_groups:
            ordered = sorted(
                active_rules,
                key=lambda r: (int(_get(r, "priority", 0)), int(_get(r, "id", 0))),
            )
            return [(None, ordered)]

        active_groups = sorted(
            [g for g in rule_groups if _get(g, "active", True)],
            key=lambda g: (int(_get(g, "priority", 0)), int(_get(g, "id", 0))),
        )
        group_ids = {int(_get(g, "id", 0)) for g in active_groups}
        by_group: Dict[int, List[Any]] = {gid: [] for gid in group_ids}
        ungrouped: List[Any] = []

        for r in active_rules:
            gid = _get(r, "group_id", None)
            if gid is not None and int(gid) in by_group:
                by_group[int(gid)].append(r)
            elif gid is None:
                ungrouped.append(r)

        plan: List[Tuple[Optional[Any], List[Any]]] = []
        for g in active_groups:
            gid = int(_get(g, "id", 0))
            g_rules = sorted(
                by_group[gid],
                key=lambda r: (int(_get(r, "priority", 0)), int(_get(r, "id", 0))),
            )
            if g_rules:
                plan.append((g, g_rules))
        if ungrouped:
            plan.append(
                (
                    None,
                    sorted(ungrouped, key=lambda r: (int(_get(r, "priority", 0)), int(_get(r, "id", 0)))),
                )
            )
        return plan

    def evaluate_transaction(
        self,
        tx: Any,
        rules: Sequence[Any],
        rule_groups: Optional[Sequence[Any]] = None,
    ) -> RuleExecutionResult:
        """
        Evaluate ordered `rule_groups` -> `rules` against a single transaction without mutating input.

        Supports chaining (later rules can observe mutations made by earlier rules, e.g. `missing_jar`
        becomes False once an earlier rule runs `set_jar`), plus `stop_processing` at rule and group scope.
        """
        tx_state: Dict[str, Any] = dict(tx) if isinstance(tx, dict) else dict(tx.model_dump())
        if "tags" in tx_state and tx_state["tags"] is not None:
            tx_state["tags"] = list(tx_state["tags"])

        matched_ids: List[int] = []
        matched_names: List[str] = []
        changes: Dict[str, Any] = {}
        stopped_early = False

        for group, group_rules in self._ordered_rules(rules, rule_groups):
            group_matched = False
            for rule in group_rules:
                if not evaluate_rule_matches(rule, tx_state):
                    continue
                group_matched = True
                rid = _get(rule, "id", 0)
                matched_ids.append(int(rid) if rid is not None else 0)
                matched_names.append(str(_get(rule, "name", f"Rule #{rid}")))

                actions = _as_list(_get(rule, "actions", None))
                if not actions:
                    actions = _as_list(_get(rule, "actions_json", None))
                _apply_actions_to_state(actions, tx_state, changes)

                if bool(_get(rule, "stop_processing", False)):
                    stopped_early = True
                    break

            if stopped_early:
                break
            if group is not None and group_matched and bool(_get(group, "stop_processing", False)):
                stopped_early = True
                break

        return RuleExecutionResult(
            matched_rule_ids=matched_ids,
            matched_rule_names=matched_names,
            stopped_early=stopped_early,
            changes=changes,
            updated_tx=tx_state,
        )

    def apply_rules_batch(
        self,
        transactions: Sequence[Any],
        rules: Sequence[Any],
        rule_groups: Optional[Sequence[Any]] = None,
    ) -> List[Tuple[Any, RuleExecutionResult]]:
        """Evaluate ordered rules over a batch of transactions, returning `(tx, result)` pairs for matches."""
        results: List[Tuple[Any, RuleExecutionResult]] = []
        for tx in transactions:
            res = self.evaluate_transaction(tx, rules, rule_groups=rule_groups)
            if res.matched:
                results.append((tx, res))
        return results


def _format_proposed_changes(changes: Dict[str, Any]) -> List[str]:
    notes: List[str] = []
    if "jar_id" in changes:
        notes.append(f"set_jar -> #{changes['jar_id']}")
    for tag in changes.get("tags_to_add", []):
        notes.append(f"add_tag -> {tag}")
    if "linked_bill_id" in changes:
        notes.append(f"link_bill -> #{changes['linked_bill_id']}")
    if "memo" in changes:
        notes.append(f"memo -> {changes['memo']}")
    for alloc in changes.get("goal_allocations", []):
        notes.append(f"allocate_goal -> #{alloc['goal_id']} (${alloc['amount_cents'] / 100:,.2f})")
    if changes.get("splits"):
        parts = ", ".join(
            f"jar#{s['jar_id']}:${s['amount_cents'] / 100:,.2f}"
            if s.get("jar_id") is not None
            else f"${s['amount_cents'] / 100:,.2f}"
            for s in changes["splits"]
        )
        notes.append(f"split -> [{parts}]")
    return notes


def dry_run_rule(
    rule: Any,
    transactions: Sequence[Any],
    *,
    max_preview: int = 25,
    lookback_days: int = DEFAULT_DRY_RUN_LOOKBACK_DAYS,
    today: Optional[date] = None,
) -> RuleTestResult:
    """
    Dry-run a single automation rule against up to 2 years (730 days) of transaction history.

    Never mutates input transactions. Returns exact `RuleTestResult` with scanned count,
    matched count, lookback window, and sample previews.
    """
    ref = today or date.today()
    effective_lookback = max(1, min(730, int(lookback_days))) if lookback_days > 0 else 730
    cutoff_iso = (ref - timedelta(days=effective_lookback)).isoformat() if lookback_days > 0 else ""

    # Temporarily force active=True for dry-run testing of draft/paused rules
    rule_copy: Dict[str, Any] = (
        dict(rule) if isinstance(rule, dict) else dict(rule.model_dump(by_alias=True))
    )
    rule_copy["active"] = True

    engine = RuleEngine()
    scanned = 0
    matched_count = 0
    samples: List[RuleTestMatchSample] = []

    for tx in transactions:
        tx_date_str = str(_get(tx, "tx_date", ""))
        if cutoff_iso and tx_date_str and tx_date_str < cutoff_iso:
            continue
        scanned += 1
        res = engine.evaluate_transaction(tx, [rule_copy])
        if not res.matched:
            continue
        matched_count += 1
        if len(samples) < max(1, int(max_preview)):
            tx_id = _get(tx, "id", 0)
            samples.append(
                RuleTestMatchSample(
                    tx_id=int(tx_id) if tx_id is not None else 0,
                    tx_date=tx_date_str,
                    merchant=str(_get(tx, "merchant", "")),
                    amount_cents=int(_get(tx, "amount_cents", 0)),
                    proposed_changes=_format_proposed_changes(res.changes),
                )
            )

    rid = _get(rule_copy, "id", None)
    return RuleTestResult(
        rule_id=int(rid) if rid is not None else None,
        scanned_count=scanned,
        matched_count=matched_count,
        lookback_days=effective_lookback,
        samples=samples,
    )
