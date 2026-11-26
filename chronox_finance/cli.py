"""
Fast, Apple-Clean Terminal CLI for ChronoX Finance.

Primary workflow:
    chronox --say "I spent $5.75 on coffee because early flight"
    chronox "put $100 in savings"
    chronox --one-tap --apply
    chronox --export numbers --out report.xlsx
    chronox --undo
"""

from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from dotenv import load_dotenv

from chronox_finance.api import create_app
from chronox_finance.models import (
    E8_SCALE,
    AIProvider,
    GoalPlanRequest,
    IntelligenceMode,
    ProfileSettingsUpdate,
    QuickAddRequest,
)
from chronox_finance.service import FinanceService

_LEGACY_SUBCOMMANDS = frozenset(
    {"serve", "status", "quant", "portfolio", "usage", "add", "ask", "suggest", "credit", "goal"}
)
_VALUE_FLAGS = frozenset({"--db", "--mode", "--provider", "--say", "-s", "--export", "--out", "--days"})
_BOOL_FLAGS = frozenset({"--yes", "-y", "--one-tap", "--apply"})


def _money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


def _microdollars(micro: int) -> str:
    dollars = Decimal(int(micro)) / Decimal(1_000_000)
    if dollars == 0:
        return "$0.0000"
    if abs(dollars) >= Decimal("0.01"):
        return f"${dollars:,.4f}"
    return f"${dollars:.6f}"


def _fmt_e8_price(price_e8: int) -> str:
    val = Decimal(int(price_e8)) / Decimal(E8_SCALE)
    if val == 0:
        return "$0.00"
    if abs(val) >= Decimal("1"):
        return f"${val:,.2f}"
    if abs(val) >= Decimal("0.01"):
        return f"${val:,.4f}"
    return f"${val:.8f}".rstrip("0").rstrip(".")


def _fmt_e8_qty(qty_e8: int) -> str:
    val = Decimal(int(qty_e8)) / Decimal(E8_SCALE)
    if val == 0:
        return "0"
    txt = f"{val:.8f}".rstrip("0").rstrip(".")
    return txt or "0"


def _format_source(source: str, provider_used: Optional[str], model_used: Optional[str]) -> str:
    labels = {
        "opencode_go": "OpenCode Go",
        "gemini": "Google Gemini",
        "openai_codex": "OpenAI / Codex",
        "chat_wrapper": "Durable Chat-Wrapper",
        "codex_cli": "Codex CLI",
        "opencode_cli": "OpenCode CLI",
        "claude_cli": "Claude Code CLI",
        "agy_cli": "Antigravity CLI",
    }
    if source == "deterministic_quant":
        return "Verified Local Engine | Quant Math"
    if source == "deterministic_local":
        return "Verified Local Engine"
    if source in labels:
        return f"Hybrid | {labels[source]} ({model_used or source})"
    if source == "ai_standalone":
        prov_label = labels.get(provider_used or "", provider_used or "AI")
        return f"AI Standalone | {prov_label} ({model_used or prov_label})"
    return f"{source} ({model_used})" if model_used else source


def _normalize_cli_argv(argv: List[str]) -> List[str]:
    """
    Allow bare natural-language strings (`chronox-finance "I spent $5 on coffee"`)
    whenever the first positional token is not a known legacy subcommand or flag.
    """
    if not argv or any(tok in ("-h", "--help") for tok in argv):
        return argv
    if any(tok in ("--say", "-s") or tok.startswith("--say=") for tok in argv):
        return argv

    prefix_flags: List[str] = []
    utterance_parts: List[str] = []
    i = 0
    saw_positional = False
    while i < len(argv):
        tok = argv[i]
        if tok in _VALUE_FLAGS:
            prefix_flags.append(tok)
            if i + 1 < len(argv):
                prefix_flags.append(argv[i + 1])
                i += 2
            else:
                i += 1
            continue
        if any(tok.startswith(f + "=") for f in _VALUE_FLAGS) or tok.startswith("--undo="):
            prefix_flags.append(tok)
            i += 1
            continue
        if tok == "--undo":
            prefix_flags.append(tok)
            if i + 1 < len(argv) and not argv[i + 1].startswith("-") and argv[i + 1].isdigit():
                prefix_flags.append(argv[i + 1])
                i += 2
            else:
                i += 1
            continue
        if tok in _BOOL_FLAGS:
            prefix_flags.append(tok)
            i += 1
            continue
        if tok.startswith("-"):
            if not saw_positional:
                return argv
            prefix_flags.append(tok)
            i += 1
            continue

        if not saw_positional and tok in _LEGACY_SUBCOMMANDS:
            return argv
        saw_positional = True
        utterance_parts.append(tok)
        i += 1

    if utterance_parts:
        return [*prefix_flags, "--say", " ".join(utterance_parts)]
    return argv


def _print_status(svc: FinanceService) -> None:
    dash = svc.get_dashboard()
    s = dash.safe_to_spend
    print("\n" + "=" * 68)
    print("  CHRONOX FINANCE -- COMMAND SNAPSHOT")
    print("=" * 68)
    print(f"  SAFE TO SPEND TODAY : {_money(s.daily_safe_to_spend_cents)} / day")
    print(f"  SAFE UNTIL PAYDAY   : {_money(s.safe_to_spend_cents)} ({s.days_until_payday}d until {s.next_payday_date})")
    print(f"  UPCOMING BILLS      : {_money(s.unpaid_bills_until_payday_cents)} shielded")
    print(f"  CHRONOX INVEST CAP  : {_money(dash.safe_to_invest_cents)} safe to trade/invest")
    print(f"  INTELLIGENCE MODE   : {dash.intelligence_mode} (active AI: {dash.active_ai_provider})")
    print(f"  VAULT STATE         : {svc.secrets.vault_state().upper()}")
    print("-" * 68)
    print("  Money Jars:")
    for j in dash.jars:
        badge = {"calm": "OK", "watch": "WATCH", "rebalance": "REBALANCE"}[j.health.value]
        print(
            f"    [{j.icon:<6}] {j.name:<22} Avail: {_money(j.available_cents):>10} "
            f"(Spent {_money(j.spent_cents)} / {_money(j.auto_allocated_cents)}) [{badge}]"
        )
    if dash.rebalance_plans:
        print("-" * 68)
        for p in dash.rebalance_plans:
            print(f"  -> {p.calm_message}")
    print("=" * 68 + "\n")


def _print_quant(svc: FinanceService, days: int = 30) -> None:
    rep = svc.get_quant_forecast(days=days)
    vol = rep.volatility
    print("\n" + "=" * 72)
    print(f"  CHRONOX QUANT FORECAST -- {rep.horizon_days}-DAY RUNWAY & MARGINAL-ROI PLAN")
    print("=" * 72)
    print(f"  STARTING LIQUID CASH : {_money(rep.starting_balance_cents)}")
    print(f"  PROJECTED ENDING ({rep.horizon_days}d): {_money(rep.ending_balance_cents)}")
    print(f"  LOWEST BALANCE NADIR : {_money(rep.lowest_balance_cents)} on {rep.lowest_balance_date or 'N/A'}")
    print(f"  SAFE RUNWAY          : {rep.safe_runway_days} / {rep.horizon_days} days | Overdraft risk: {'YES' if rep.overdraft_risk else 'No'}")
    if rep.payday_crunch_warning:
        print(f"  RUNWAY NOTE          : {rep.payday_crunch_warning}")
    print("-" * 72)
    print(f"  SPENDING STABILITY   : {vol.stability_score_0_100}/100 (CV={vol.coefficient_of_variation:.2f}, {vol.sample_days}d sample)")
    print(f"  WEEKLY SPEND P50/P90 : {_money(vol.p50_weekly_spend_cents)} / {_money(vol.p90_weekly_spend_cents)}")
    print(f"  DYNAMIC BUFFER TARGET: {_money(vol.recommended_buffer_cents)} | P90 Daily Safe: {_money(vol.p90_conservative_daily_safe_cents)}/day")
    print("-" * 72)
    print(
        f"  OPTIMAL SURPLUS ALLOCATION ({_money(rep.monthly_surplus_cents)}/mo -> "
        f"Est. {_money(rep.total_expected_annual_benefit_cents)}/yr benefit):"
    )
    if not rep.optimal_allocations:
        print("    (No flexible surplus available after essentials this cycle.)")
    else:
        for step in rep.optimal_allocations:
            print(
                f"    {step.priority_rank}. [{step.bucket}] {step.target_name:<24} "
                f"{_money(step.amount_cents):>10}  (~{step.effective_apr_equivalent_pct:.1f}% equiv | +{_money(step.expected_annual_benefit_cents)}/yr)"
            )
            if step.formula_note:
                print(f"       {step.formula_note}")
    print("=" * 72 + "\n")


def _print_portfolio(svc: FinanceService, refresh: bool = False) -> None:
    port = svc.refresh_portfolio_quotes(force_refresh=True) if refresh else svc.get_portfolio()
    print("\n" + "=" * 76)
    print("  CHRONOX PORTFOLIO -- STOCKS & CRYPTO HOLDINGS (READ-ONLY)")
    print("=" * 76)
    print(f"  TOTAL MARKET VALUE : {_money(port.total_value_cents)}")
    print(f"  TOTAL COST BASIS   : {_money(port.total_cost_basis_cents)}")
    print(f"  UNREALIZED P&L     : {_money(port.unrealized_pnl_cents)} ({port.unrealized_pnl_pct:+.2f}%)")
    if port.allocations:
        alloc_str = " | ".join(
            f"{a.asset_class.upper()}: {_money(a.value_cents)} ({a.pct:.1f}%)" for a in port.allocations
        )
        print(f"  ALLOCATION         : {alloc_str}")
    print("-" * 76)
    if not port.holdings:
        print("  (No holdings tracked yet. Connect Coinbase/Robinhood/Schwab/Plaid or import broker CSV.)")
    else:
        print(f"  {'SYMBOL':<10} {'CLASS':<8} {'QUANTITY':>14} {'PRICE':>14} {'VALUE':>12} {'P&L':>12}")
        print("  " + "-" * 72)
        for h in port.holdings:
            print(
                f"  {h.symbol:<10} {h.asset_class:<8} {_fmt_e8_qty(h.quantity_e8):>14} "
                f"{_fmt_e8_price(h.price_e8):>14} {_money(h.market_value_cents):>12} {_money(h.unrealized_pnl_cents):>12}"
            )
    print("=" * 76 + "\n")


def _print_usage(svc: FinanceService, days: int = 30) -> None:
    rep = svc.get_ai_usage(days=days)
    print("\n" + "=" * 76)
    print(f"  CHRONOX AI USAGE & CACHE SAVINGS ({rep.days}-DAY WINDOW)")
    print("=" * 76)
    print(
        f"  REQUESTS TODAY     : {rep.requests_today} / {rep.daily_quota_limit} "
        f"({rep.quota_remaining_today} remaining)"
    )
    print(f"  TOTAL REQUESTS     : {rep.total_requests}")
    print(
        f"  TOKENS (IN / OUT)  : {rep.total_input_tokens:,} / {rep.total_output_tokens:,} "
        f"(cached input: {rep.total_cached_tokens:,})"
    )
    print(f"  CACHE HIT RATE     : {rep.cache_hit_rate_pct:.1f}%")
    print(f"  ESTIMATED COST     : {_microdollars(rep.total_cost_microdollars)}")
    print(f"  VERIFIED SAVED     : {_microdollars(rep.total_saved_microdollars)}")
    print("-" * 76)
    if not rep.by_provider:
        print("  (No external AI calls logged in this window.)")
    else:
        print(f"  {'PROVIDER':<15} {'MODEL':<22} {'REQS':>5} {'CACHE%':>7} {'COST':>11} {'SAVED':>11}")
        print("  " + "-" * 72)
        for row in rep.by_provider:
            print(
                f"  {row.provider:<15} {row.model[:22]:<22} {row.requests:>5} "
                f"{row.cache_hit_rate_pct:>6.1f}% {_microdollars(row.cost_microdollars):>11} "
                f"{_microdollars(row.saved_microdollars):>11}"
            )
    print("=" * 76 + "\n")


def _print_onetap_result(res: Dict[str, Any]) -> None:
    runway = res.get("runway") or {}
    ruin_pct = float(runway.get("ruin_probability_pct", 0.0) or 0.0)
    survival_pct = max(0.0, min(100.0, 100.0 - ruin_pct))
    cvar_cents = int(runway.get("cvar_95_buffer_cents", 0) or 0)
    daily_safe_95 = int(runway.get("confidence_95_daily_safe_cents", 0) or 0)
    savings_cents = int(res.get("total_expected_annual_savings_cents", 0) or 0)
    horizon = int(res.get("horizon_days", 30) or 30)

    print("\n" + "=" * 72)
    print(f"  ChronoX One-Tap Optimization ({horizon}-Day Statistical Plan)")
    print("=" * 72)
    print(f"  {res.get('executive_summary', '')}")
    print("-" * 72)
    print(f"  CVaR 95% Cushion     : {_money(cvar_cents)}")
    print(f"  Survival Probability : {survival_pct:.1f}% (overdraft risk {ruin_pct:.1f}%)")
    print(f"  95%-Safe Daily Spend : {_money(daily_safe_95)} / day")
    print(f"  Est. Annual Savings  : {_money(savings_cents)} / yr")
    actions = list(res.get("actions") or [])
    if actions:
        print("-" * 72)
        for idx, act in enumerate(actions, 1):
            if act.get("is_advisory"):
                tag = "ADVISORY"
            elif res.get("applied"):
                tag = "APPLIED"
            else:
                tag = "READY"
            print(f"  {idx}. [{tag}] {act.get('title', act.get('action_type', 'Action'))}")
            if act.get("summary"):
                print(f"     {act['summary']}")
    else:
        print("-" * 72)
        print("  No adjustments needed right now.")

    print("-" * 72)
    if res.get("applied") and res.get("batch_id") is not None:
        print(f"  Applied One-Tap batch #{res['batch_id']}.")
        print("  Undo anytime: chronox --undo")
    elif not res.get("needs_setup") and any(a.get("executable") for a in actions):
        print("  Preview only. Apply with: chronox --one-tap --apply")
    print("=" * 72 + "\n")


def _run_export(
    svc: FinanceService,
    *,
    fmt: str,
    out_path: Optional[str] = None,
    days: int = 365,
) -> Path:
    norm = (fmt or "numbers").strip().lower().replace("_", "-")
    if norm == "numbers":
        artifact = svc.export_apple_numbers_workbook(days=days, native=False)
        label = "Apple Numbers Graphical Workbook (.xlsx)"
    elif norm == "csv":
        artifact = svc.export_transactions_csv_artifact()
        label = "Transactions CSV"
    elif norm == "csv-pack":
        artifact = svc.export_executive_csv_pack_artifact(days=days)
        label = "Executive CSV Pack (.zip)"
    else:
        raise ValueError(f"Unsupported export format '{fmt}'. Choose numbers, csv, or csv-pack.")

    target = Path(out_path) if out_path else (Path.cwd() / artifact.filename)
    if target.parent and str(target.parent) not in ("", "."):
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(artifact.content)
    print(f"\nExported {label}")
    print(f"  Path : {target.resolve()}")
    print(f"  Size : {len(artifact.content):,} bytes\n")
    return target


def _run_say(
    svc: FinanceService,
    text: str,
    *,
    yes: bool = False,
    mode: Optional[str] = None,
    provider: Optional[str] = None,
    out_path: Optional[str] = None,
    days: int = 365,
) -> int:
    res = svc.say(
        text,
        auto_commit=True,
        mode_override=mode,
        provider_override=provider,
    )
    if res.get("requires_confirmation") and yes and res.get("batch_id") is not None:
        exec_res = svc.execute_agent_batch(int(res["batch_id"]))
        res["executed"] = True
        res["requires_confirmation"] = False
        res["undo_available"] = bool(exec_res.undo_available)
        dash = svc.get_dashboard()
        res["safe_to_spend_today_cents"] = dash.safe_to_spend.daily_safe_to_spend_cents
        res["safe_to_spend_cents"] = dash.safe_to_spend.safe_to_spend_cents

    src = _format_source(
        str(res.get("source") or "deterministic_local"),
        res.get("provider_used"),
        res.get("model_used"),
    )
    print(f"\nChronoX [{src}]")
    print(f"  {res.get('assistant_message', '')}")

    actions = list(res.get("actions") or [])
    if res.get("executed"):
        for act in actions:
            print(f"  + {act.get('summary', act.get('action_type', 'Action'))}")
        print(
            f"  Safe to spend today: {_money(int(res.get('safe_to_spend_today_cents', 0)))}/day "
            f"({_money(int(res.get('safe_to_spend_cents', 0)))} until payday)"
        )
        if res.get("undo_available") and res.get("batch_id") is not None:
            print(f"  Batch #{res['batch_id']} · Undo anytime: chronox --undo")
    elif res.get("requires_confirmation") and actions:
        print(f"  Proposed plan (batch #{res.get('batch_id')}):")
        for idx, act in enumerate(actions, 1):
            print(f"    {idx}. {act.get('summary', act.get('action_type', 'Action'))}")
        print('  Confirm and apply by re-running with --yes.')

    if res.get("export_format"):
        _run_export(svc, fmt=str(res["export_format"]), out_path=out_path, days=days)

    if res.get("onetap_plan"):
        _print_onetap_result(res["onetap_plan"])
    else:
        print()
    return 0


def _run_undo(svc: FinanceService, batch_spec: str) -> int:
    batch_id: Optional[int] = None
    cleaned = (batch_spec or "").strip().lower()
    if cleaned and cleaned != "latest":
        try:
            batch_id = int(cleaned)
        except ValueError:
            print(f"Error: invalid batch ID '{batch_spec}'. Pass an integer batch ID or omit for latest.", file=sys.stderr)
            return 1
    try:
        res = svc.undo_last_action(batch_id=batch_id)
    except ValueError as exc:
        print(f"\n[Undo] {exc}\n", file=sys.stderr)
        return 1
    if res.get("kind") == "onetap":
        print(f"\nUndid One-Tap batch #{res['batch_id']} cleanly ({res.get('restored_count', 0)} restored).\n")
    else:
        msg = res.get("message") or f"Undid batch #{res.get('batch_id')} cleanly."
        print(f"\n{msg}\n")
    return 0


def run_cli(argv: Optional[List[str]] = None) -> int:
    load_dotenv()
    default_db = os.getenv("CHRONOX_FINANCE_DB_PATH", ".chronox/finance.sqlite3")
    default_host = os.getenv("CHRONOX_FINANCE_HOST", "127.0.0.1")
    default_port = int(os.getenv("CHRONOX_FINANCE_PORT", "8742"))

    mode_choices = [m.value for m in IntelligenceMode]
    provider_choices = [p.value for p in AIProvider]

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    norm_argv = _normalize_cli_argv(raw_argv)

    parser = argparse.ArgumentParser(
        prog="chronox-finance",
        description=(
            "ChronoX Finance -- Local-First Personal Finance Command Center\n\n"
            "Quick Examples:\n"
            '  chronox --say "I spent $5.75 on coffee because early flight"\n'
            '  chronox "put $100 in savings"\n'
            "  chronox --one-tap --apply\n"
            "  chronox --export numbers --out report.xlsx\n"
            "  chronox --undo"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", default=default_db, help=f"SQLite DB path (default: {default_db})")
    parser.add_argument("--mode", choices=mode_choices, default=None, help="Intelligence mode override")
    parser.add_argument("--provider", choices=provider_choices, default=None, help="AI provider override")

    # Front-and-center ChronoX flags
    parser.add_argument(
        "-s",
        "--say",
        type=str,
        default=None,
        help="Say anything in natural language (log expenses with reason, save, split, ask, export, optimize)",
    )
    parser.add_argument("-y", "--yes", action="store_true", help="Auto-confirm multi-action plans for --say")
    parser.add_argument(
        "--undo",
        nargs="?",
        const="latest",
        default=None,
        metavar="BATCH_ID",
        help="Undo the latest (or specified) --say or --one-tap action",
    )
    parser.add_argument(
        "--one-tap",
        action="store_true",
        help="Run the AI + Statistical Quant One-Tap Optimizer",
    )
    parser.add_argument("--apply", action="store_true", help="Atomically apply the One-Tap optimization plan")
    parser.add_argument("--days", type=int, default=30, help="Horizon/lookback days (default: 30)")
    parser.add_argument(
        "--export",
        choices=["numbers", "csv", "csv-pack"],
        default=None,
        help="Export Apple Numbers workbook (.xlsx), Transactions CSV, or Executive CSV Pack (.zip)",
    )
    parser.add_argument("--out", type=str, default=None, help="Output file path for --export")

    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="Start the ChronoX Finance web app (fixed loopback port)")
    p_serve.add_argument("--host", default=default_host, help=f"Bind host (default: {default_host})")
    p_serve.add_argument("--port", type=int, default=default_port, help=f"Fixed bind port (default: {default_port})")
    p_serve.add_argument("--mode", choices=mode_choices, default=None, dest="sub_mode")
    p_serve.add_argument("--provider", choices=provider_choices, default=None, dest="sub_provider")

    sub.add_parser("status", help="Show Safe-to-Spend & 5-Jar overview")

    p_quant = sub.add_parser("quant", help="Show N-day cashflow runway, volatility buffer & optimal surplus allocation")
    p_quant.add_argument("--days", type=int, default=30, help="Forecast horizon in days (7-730, default: 30)")

    p_port = sub.add_parser("portfolio", help="Show Stocks & Crypto portfolio summary and holdings")
    p_port.add_argument("--refresh", action="store_true", help="Refresh live quotes from official sources before printing")

    p_usage = sub.add_parser("usage", help="Show AI token usage, cache hit rate & verified savings")
    p_usage.add_argument("--days", type=int, default=30, help="Lookback window in days (1-365, default: 30)")

    p_add = sub.add_parser("add", help="Natural-language quick-add (e.g. 'coffee 6.50' or 'impulse 180 keyboard')")
    p_add.add_argument("text", nargs="+", help="Quick-add phrase")

    p_ask = sub.add_parser("ask", help="Ask the ChronoX Financial Coach a question")
    p_ask.add_argument("question", nargs="+", help="Question to ask")
    p_ask.add_argument("--mode", choices=mode_choices, default=None, dest="sub_mode")
    p_ask.add_argument("--provider", choices=provider_choices, default=None, dest="sub_provider")

    p_sug = sub.add_parser("suggest", help="Show verified AI/quant auto-budget suggestions")
    p_sug.add_argument("--refresh", action="store_true", help="Bypass cache")
    p_sug.add_argument("--mode", choices=mode_choices, default=None, dest="sub_mode")
    p_sug.add_argument("--provider", choices=provider_choices, default=None, dest="sub_provider")

    p_cred = sub.add_parser("credit", help="Show Credit Factor Analysis, Chase 5/24 & AZEO debt plan")
    p_cred.add_argument("--extra", type=float, default=250.0, help="Extra monthly dollars toward debt (default: 250)")

    p_goal = sub.add_parser("goal", help="Optimize a credit-safe purchase plan (e.g. goal 'Mac Mini' 1000)")
    p_goal.add_argument("name", help="Item or goal name")
    p_goal.add_argument("price", type=float, help="Target dollar amount")

    args = parser.parse_args(norm_argv)
    effective_mode = getattr(args, "sub_mode", None) or args.mode
    effective_provider = getattr(args, "sub_provider", None) or args.provider

    # Direct front-and-center flags run before falling back to `serve`
    if args.say is not None or args.undo is not None or args.one_tap or args.export is not None:
        svc = FinanceService(db_path=args.db)
        try:
            if args.undo is not None:
                return _run_undo(svc, args.undo)
            if args.say is not None:
                return _run_say(
                    svc,
                    args.say,
                    yes=bool(args.yes),
                    mode=effective_mode,
                    provider=effective_provider,
                    out_path=args.out,
                    days=365 if args.days == 30 else int(args.days),
                )
            if args.one_tap:
                res = svc.run_onetap_optimization(
                    horizon_days=int(args.days),
                    apply=bool(args.apply),
                    mode_override=effective_mode,
                    provider_override=effective_provider,
                )
                _print_onetap_result(res)
                return 0
            if args.export is not None:
                exp_days = 365 if args.days == 30 else int(args.days)
                _run_export(svc, fmt=args.export, out_path=args.out, days=exp_days)
                return 0
        finally:
            svc.close()

    cmd = args.command or "serve"

    if cmd == "serve":
        host = getattr(args, "host", default_host)
        port = int(getattr(args, "port", default_port))
        svc = FinanceService(db_path=args.db)
        if effective_mode or effective_provider:
            svc.update_settings(
                ProfileSettingsUpdate(
                    intelligence_mode=IntelligenceMode(effective_mode) if effective_mode else None,
                    ai_provider=AIProvider(effective_provider) if effective_provider else None,
                )
            )
        app = create_app(db_path=args.db, service=svc, enforce_vault_lock=True, allowed_port=port)
        print(f"\n[CHRONOX] Finance Command Center running at http://{host}:{port} (vault={svc.secrets.vault_state()})\n")
        uvicorn.run(app, host=host, port=port, log_level="info")
        return 0

    svc = FinanceService(db_path=args.db)
    try:
        if cmd == "status":
            _print_status(svc)
        elif cmd == "quant":
            _print_quant(svc, days=args.days)
        elif cmd == "portfolio":
            _print_portfolio(svc, refresh=bool(args.refresh))
        elif cmd == "usage":
            _print_usage(svc, days=args.days)
        elif cmd == "add":
            res = svc.quick_add(QuickAddRequest(text=" ".join(args.text)))
            print(f"\n[OK] {res['message']}\n")
            _print_status(svc)
        elif cmd == "ask":
            ans = svc.ask_coach(
                " ".join(args.question),
                mode_override=effective_mode,
                provider_override=effective_provider,
            )
            src = _format_source(ans.source, ans.provider_used, ans.model_used)
            print(f"\n[{src}]\n{ans.answer}\n")
        elif cmd == "suggest":
            batch = svc.get_ai_suggestions(
                force_refresh=args.refresh,
                mode_override=effective_mode,
                provider_override=effective_provider,
            )
            src = _format_source(batch.source, batch.provider_used, batch.model_used)
            print(f"\nVerified Suggestions [{src}]:")
            if batch.notice:
                print(f"  Note: {batch.notice}")
            for i, s in enumerate(batch.suggestions, 1):
                print(f"\n  {i}. [{s.priority.upper()}] {s.title}")
                print(f"     {s.rationale}")
                print(f"     -> Next tiny step: {s.micro_step}")
            print()
        elif cmd == "credit":
            rep = svc.get_credit_report(extra_monthly_budget_cents=int(round(args.extra * 100)))
            print(
                f"\n[CREDIT ANALYSIS] Util: {rep.aggregate_utilization_pct:.1f}% ({rep.aggregate_tier}) "
                f"| DTI: {rep.dti_pct:.1f}%"
            )
            print(
                f"  Total Debt: {_money(rep.total_debt_cents)} | Interest Burn: "
                f"{_money(rep.monthly_interest_burn_cents)}/mo | Recommended: {rep.recommended_strategy}"
            )
            if rep.five_twenty_four:
                ftf = rep.five_twenty_four
                print(
                    f"  Chase 5/24 Velocity: {ftf.count_5_24}/24 "
                    f"({'UNDER' if ftf.under_5_24 else 'AT/OVER'}) | "
                    f"Inquiries 6m/12m: {ftf.inquiries_6m}/{ftf.inquiries_12m}"
                )
            if rep.azeo_plan:
                print("\n  AZEO Pre-Statement-Close Actions:")
                for a in rep.azeo_plan:
                    print(f"    - {a.card_name}: {a.explanation}")
            if rep.strategies:
                print("\n  Payoff Strategies:")
                for st in rep.strategies:
                    tag = " [RECOMMENDED]" if st.recommended_for_adhd else ""
                    print(
                        f"    - {st.label}{tag}: {st.months_to_debt_free} mo ({st.debt_free_date}) | "
                        f"Interest {_money(st.total_interest_cents)} | 1st Win: {st.first_win_months} mo"
                    )
            print()
        elif cmd == "goal":
            plan = svc.optimize_goal_plan(
                GoalPlanRequest(item_name=args.name, target_cents=int(round(args.price * 100)))
            )
            print(f"\n[GOAL PLAN] {plan.item_name} ({_money(plan.target_cents)} ~ {plan.work_hours_cost}h work)")
            if plan.high_apr_debt_warning:
                print(f"  [!] {plan.high_apr_debt_warning}")
            for opt in plan.options:
                tag = " [RECOMMENDED]" if opt.recommended else ""
                print(f"\n  [{opt.title}]{tag}")
                print(f"    {opt.summary}")
                print(f"    Credit note: {opt.credit_impact_note}")
            print()
        return 0
    finally:
        svc.close()


if __name__ == "__main__":
    sys.exit(run_cli())
