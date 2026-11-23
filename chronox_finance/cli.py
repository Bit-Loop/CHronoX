"""
Fast Terminal CLI for ChronoX Finance (ADHD zero-friction capture).

Examples:
    python main.py --action finance
    python -m chronox_finance.cli serve --port 8742
    python -m chronox_finance.cli status
    python -m chronox_finance.cli add "coffee 6.50"
    python -m chronox_finance.cli add "impulse 199 mech keyboard"
    python -m chronox_finance.cli ask "Can I afford an $80 dinner tonight?"
    python -m chronox_finance.cli suggest
    python -m chronox_finance.cli demo
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

import uvicorn
from dotenv import load_dotenv

from chronox_finance.api import create_app
from chronox_finance.models import (
    AIProvider,
    GoalPlanRequest,
    IntelligenceMode,
    ProfileSettingsUpdate,
    QuickAddRequest,
)
from chronox_finance.service import FinanceService


def _money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


def _format_source(source: str, provider_used: Optional[str], model_used: Optional[str]) -> str:
    if source == "deterministic_quant":
        return "Verified Local Engine · Quant Math"
    if source == "deterministic_local":
        return "Verified Local Engine"
    if source == "gemini":
        return f"Hybrid · Google Gemini ({model_used or 'gemini'})"
    if source == "opencode_go":
        return f"Hybrid · OpenCode Go ({model_used or 'opencode_go'})"
    if source == "ai_standalone":
        prov_label = "Google Gemini" if provider_used == "gemini" else "OpenCode Go"
        return f"AI Standalone · {prov_label} ({model_used or prov_label})"
    return f"{source} ({model_used})" if model_used else source


def _print_status(svc: FinanceService) -> None:
    dash = svc.get_dashboard()
    s = dash.safe_to_spend
    print("\n" + "=" * 64)
    print("  ChronoX Finance — Calm ADHD Money Snapshot")
    print("=" * 64)
    print(f"  SAFE TO SPEND TODAY : {_money(s.daily_safe_to_spend_cents)} / day")
    print(f"  SAFE UNTIL PAYDAY   : {_money(s.safe_to_spend_cents)} ({s.days_until_payday}d until {s.next_payday_date})")
    print(f"  UPCOMING BILLS      : {_money(s.unpaid_bills_until_payday_cents)} shielded")
    print(f"  CHRONOX INVEST CAP  : {_money(dash.safe_to_invest_cents)} safe to trade/invest")
    print(f"  INTELLIGENCE MODE   : {dash.intelligence_mode} (active AI: {dash.active_ai_provider})")
    print("-" * 64)
    print("  Money Jars:")
    for j in dash.jars:
        badge = {"calm": "OK", "watch": "WATCH", "rebalance": "REBALANCE"}[j.health.value]
        print(
            f"    {j.icon} {j.name:<22} Avail: {_money(j.available_cents):>10} "
            f"(Spent {_money(j.spent_cents)} / {_money(j.auto_allocated_cents)}) [{badge}]"
        )
    if dash.rebalance_plans:
        print("-" * 64)
        for p in dash.rebalance_plans:
            print(f"  -> {p.calm_message}")
    print("=" * 64 + "\n")


def _print_quant(svc: FinanceService, days: int = 30) -> None:
    rep = svc.get_quant_forecast(days=days)
    vol = rep.volatility
    print("\n" + "=" * 68)
    print(f"  ChronoX Quant Forecast — {rep.horizon_days}-Day Cashflow Runway & Marginal-ROI Plan")
    print("=" * 68)
    print(f"  STARTING LIQUID CASH : {_money(rep.starting_balance_cents)}")
    print(f"  PROJECTED ENDING ({rep.horizon_days}d): {_money(rep.ending_balance_cents)}")
    print(f"  LOWEST BALANCE NADIR : {_money(rep.lowest_balance_cents)} on {rep.lowest_balance_date or 'N/A'}")
    print(f"  SAFE RUNWAY          : {rep.safe_runway_days} / {rep.horizon_days} days | Overdraft risk: {'YES' if rep.overdraft_risk else 'No'}")
    if rep.payday_crunch_warning:
        print(f"  RUNWAY NOTE          : {rep.payday_crunch_warning}")
    print("-" * 68)
    print(f"  SPENDING STABILITY   : {vol.stability_score_0_100}/100 (CV={vol.coefficient_of_variation:.2f}, {vol.sample_days}d sample)")
    print(f"  WEEKLY SPEND P50/P90 : {_money(vol.p50_weekly_spend_cents)} / {_money(vol.p90_weekly_spend_cents)}")
    print(f"  DYNAMIC BUFFER TARGET: {_money(vol.recommended_buffer_cents)} | P90 Daily Safe: {_money(vol.p90_conservative_daily_safe_cents)}/day")
    print("-" * 68)
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
    print("=" * 68 + "\n")


def run_cli(argv: Optional[List[str]] = None) -> int:
    load_dotenv()
    default_db = os.getenv("CHRONOX_FINANCE_DB_PATH", ".chronox/finance.sqlite3")
    default_host = os.getenv("CHRONOX_FINANCE_HOST", "127.0.0.1")
    default_port = int(os.getenv("CHRONOX_FINANCE_PORT", "8742"))

    mode_choices = [m.value for m in IntelligenceMode]
    provider_choices = [p.value for p in AIProvider]

    parser = argparse.ArgumentParser(
        prog="chronox-finance",
        description="ChronoX Finance — ADHD-Friendly Auto-Budgeting, Quant Math Engine & Multi-Provider AI Coach",
    )
    parser.add_argument("--db", default=default_db, help=f"SQLite DB path (default: {default_db})")
    parser.add_argument("--mode", choices=mode_choices, default=None, help="Intelligence mode override (deterministic_only|hybrid|ai_standalone)")
    parser.add_argument("--provider", choices=provider_choices, default=None, help="AI provider override (auto|opencode_go|gemini)")

    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="Start the ChronoX Finance web app")
    p_serve.add_argument("--host", default=default_host)
    p_serve.add_argument("--port", type=int, default=default_port)
    p_serve.add_argument("--demo", action="store_true", help="Load realistic 90-day demo data if DB is empty")
    p_serve.add_argument("--mode", choices=mode_choices, default=None, dest="sub_mode")
    p_serve.add_argument("--provider", choices=provider_choices, default=None, dest="sub_provider")

    sub.add_parser("status", help="Show Safe-to-Spend & 5-Jar overview")

    p_quant = sub.add_parser("quant", help="Show N-day cashflow runway, volatility buffer & optimal surplus allocation")
    p_quant.add_argument("--days", type=int, default=30, help="Forecast horizon in days (7-90, default: 30)")

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

    p_cred = sub.add_parser("credit", help="Show FICO/AZEO credit & debt optimization plan")
    p_cred.add_argument("--extra", type=float, default=250.0, help="Extra monthly dollars toward debt (default: 250)")

    p_goal = sub.add_parser("goal", help="Optimize a credit-safe purchase plan (e.g. goal 'Mac Mini' 1000)")
    p_goal.add_argument("name", help="Item or goal name")
    p_goal.add_argument("price", type=float, help="Target dollar amount")

    sub.add_parser("demo", help="Load realistic 90-day ADHD demo workspace")

    args = parser.parse_args(argv)
    cmd = args.command or "serve"
    effective_mode = getattr(args, "sub_mode", None) or args.mode
    effective_provider = getattr(args, "sub_provider", None) or args.provider

    if cmd == "serve":
        host = getattr(args, "host", default_host)
        port = getattr(args, "port", default_port)
        svc = FinanceService(db_path=args.db)
        if effective_mode or effective_provider:
            svc.update_settings(
                ProfileSettingsUpdate(
                    intelligence_mode=IntelligenceMode(effective_mode) if effective_mode else None,
                    ai_provider=AIProvider(effective_provider) if effective_provider else None,
                )
            )
        if getattr(args, "demo", False) and not svc.storage.list_transactions(limit=1):
            svc.load_demo_workspace()
            print("✓ Loaded 90-day ADHD demo workspace.")
        app = create_app(db_path=args.db, service=svc)
        print(f"\n🌿 ChronoX Finance running at http://{host}:{port}\n")
        uvicorn.run(app, host=host, port=port, log_level="info")
        return 0

    svc = FinanceService(db_path=args.db)
    try:
        if cmd == "status":
            _print_status(svc)
        elif cmd == "quant":
            _print_quant(svc, days=args.days)
        elif cmd == "add":
            res = svc.quick_add(QuickAddRequest(text=" ".join(args.text)))
            print(f"\n✓ {res['message']}\n")
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
            print(f"\n💳 Credit & Debt Optimizer — Util: {rep.aggregate_utilization_pct:.1f}% ({rep.aggregate_tier}) | DTI: {rep.dti_pct:.1f}%")
            print(f"   Total Debt: {_money(rep.total_debt_cents)} | Interest Burn: {_money(rep.monthly_interest_burn_cents)}/mo | Recommended: {rep.recommended_strategy}")
            if rep.azeo_plan:
                print("\n  AZEO Pre-Statement-Close Actions:")
                for a in rep.azeo_plan:
                    print(f"    • {a.card_name}: {a.explanation}")
            if rep.strategies:
                print("\n  Payoff Strategies:")
                for st in rep.strategies:
                    star = " ★" if st.recommended_for_adhd else ""
                    print(f"    • {st.label}{star}: {st.months_to_debt_free} mo ({st.debt_free_date}) | Interest {_money(st.total_interest_cents)} | 1st Win: {st.first_win_months} mo")
            print()
        elif cmd == "goal":
            plan = svc.optimize_goal_plan(
                GoalPlanRequest(item_name=args.name, target_cents=int(round(args.price * 100)))
            )
            print(f"\n🎯 Credit-Safe Goal Plan: {plan.item_name} ({_money(plan.target_cents)} ≈ {plan.work_hours_cost}h work)")
            if plan.high_apr_debt_warning:
                print(f"   ⚠ {plan.high_apr_debt_warning}")
            for opt in plan.options:
                star = " ★ RECOMMENDED" if opt.recommended else ""
                print(f"\n  [{opt.title}]{star}")
                print(f"    {opt.summary}")
                print(f"    Credit note: {opt.credit_impact_note}")
            print()
        elif cmd == "demo":
            svc.load_demo_workspace()
            print("✓ Loaded realistic 90-day ADHD demo workspace.")
            _print_status(svc)
        return 0
    finally:
        svc.close()


if __name__ == "__main__":
    sys.exit(run_cli())
