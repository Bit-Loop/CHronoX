#!/usr/bin/env python3
"""
ChronoX -- Unified Entry Point (Local-First Industrial Personal Finance & Algorithmic Trading Platform).

Primary modes:
    # Start the ChronoX Finance Command Center web app (default, fixed port 8742)
    python main.py
    python main.py --action finance --port 8742

    # Terminal quick-capture, portfolio, AI usage & coach
    python main.py --action budget --quick "coffee 6.50"
    python main.py --action budget --ask "Can I afford a $90 dinner tonight?"
    python main.py --action budget --suggest
    python main.py --action budget --portfolio
    python main.py --action budget --usage

    # ChronoX Market Data & Trading Core (TimescaleDB + Polygon.io)
    python main.py --action backfill --tickers AAPL,MSFT,NVDA --start 2022-01-01
    python main.py --action health
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.config_manager import ConfigManager  # noqa: E402
from utils.logger import setup_logging  # noqa: E402


class ChronoXOrchestrator:
    """
    Central orchestrator for ChronoX.

    Uses lazy subsystem initialization so lightweight personal finance and CLI
    commands start instantly without requiring TimescaleDB, Redis, or CUDA.
    """

    def __init__(self, config_path: Optional[str] = None, init_trading_db: bool = False):
        default_log_dir = PROJECT_ROOT / "logs"
        try:
            default_log_dir.mkdir(parents=True, exist_ok=True)
            test_probe = default_log_dir / ".write_probe"
            test_probe.write_text("", encoding="utf-8")
            test_probe.unlink(missing_ok=True)
        except OSError:
            default_log_dir = Path.home() / ".chronox" / "logs"
        self.logger = setup_logging(
            name="ChronoX",
            log_dir=default_log_dir,
            level=logging.INFO,
        )
        self.config = ConfigManager(config_path)
        self.polygon_client: Any = None
        self.db_writer: Any = None
        if init_trading_db:
            self._init_trading_infrastructure()

    def _init_trading_infrastructure(self) -> None:
        """Lazily initialize Polygon.io client and TimescaleDB writer when trading ops run."""
        if self.polygon_client is None and self.config.polygon.api_key:
            from data.ingestion.polygon.client import PolygonClient

            self.polygon_client = PolygonClient(
                api_key=self.config.polygon.api_key,
                rate_limit_calls=self.config.polygon.rate_limit_calls,
                rate_limit_period=self.config.polygon.rate_limit_period,
                timeout=self.config.polygon.timeout,
            )

        if self.db_writer is None and self.config.timescale.password:
            try:
                from data.storage.timescale_writer import TimescaleWriter

                self.db_writer = TimescaleWriter(
                    host=self.config.timescale.host,
                    port=self.config.timescale.port,
                    database=self.config.timescale.database,
                    user=self.config.timescale.user,
                    password=self.config.timescale.password,
                    min_connections=self.config.timescale.min_connections,
                    max_connections=self.config.timescale.max_connections,
                )
            except Exception as exc:
                self.logger.error("TimescaleDB initialization failed: %s", exc)
                self.db_writer = None

    def run_finance_app(
        self,
        *,
        host: Optional[str] = None,
        port: Optional[int] = None,
        db_path: Optional[str] = None,
        intelligence_mode: Optional[str] = None,
        ai_provider: Optional[str] = None,
    ) -> None:
        """Launch the ChronoX Industrial Finance Web App on a fixed loopback port."""
        import uvicorn

        from chronox_finance.api import create_app
        from chronox_finance.models import AIProvider, IntelligenceMode, ProfileSettingsUpdate
        from chronox_finance.service import FinanceService

        resolved_host = host or self.config.finance.host
        resolved_port = int(port if port is not None else self.config.finance.port)
        resolved_db = db_path or self.config.finance.db_path

        svc = FinanceService(db_path=resolved_db)
        if intelligence_mode or ai_provider:
            svc.update_settings(
                ProfileSettingsUpdate(
                    intelligence_mode=IntelligenceMode(intelligence_mode) if intelligence_mode else None,
                    ai_provider=AIProvider(ai_provider) if ai_provider else None,
                )
            )

        app = create_app(
            db_path=resolved_db,
            service=svc,
            enforce_vault_lock=True,
            allowed_port=resolved_port,
        )
        self.logger.info("Starting ChronoX Finance at http://%s:%s", resolved_host, resolved_port)
        print("\n[CHRONOX] ChronoX Industrial Finance Command Center")
        print(f"   Open in browser: http://{resolved_host}:{resolved_port} (vault={svc.secrets.vault_state()})\n")
        uvicorn.run(app, host=resolved_host, port=resolved_port, log_level="info")

    def run_backfill(
        self,
        tickers: List[str],
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        skip_minute: bool = True,
        use_flatfiles: bool = False,
    ) -> Dict[str, Any]:
        """Run historical market data backfill honoring date range and timeframe flags."""
        self._init_trading_infrastructure()
        api_key = self.config.polygon.api_key
        if not api_key:
            return {"status": "failed", "error": "Missing POLYGON_API_KEY in environment"}
        if not self.db_writer:
            return {"status": "failed", "error": "TimescaleDB not connected (check TIMESCALE_PASSWORD)"}

        ref_end = date.fromisoformat(end_date) if end_date else date.today()
        ref_start = date.fromisoformat(start_date) if start_date else date(ref_end.year - 2, ref_end.month, ref_end.day)
        years_back = max(1, int(round((ref_end - ref_start).days / 365.25)))

        from scripts.backfill_historical_data import BackfillOrchestrator

        backfill = BackfillOrchestrator(
            polygon_api_key=api_key,
            db_writer=self.db_writer,
            years_back=years_back,
            max_workers=4,
            use_flatfiles=use_flatfiles,
        )
        self.logger.info(
            "Running backfill for %s (%s -> %s, skip_minute=%s, flatfiles=%s)",
            tickers,
            ref_start.isoformat(),
            ref_end.isoformat(),
            skip_minute,
            use_flatfiles,
        )
        return backfill.run_full_backfill(tickers)

    def check_safe_trading_capital(self) -> float:
        """Query the ChronoX Finance 'Don't Trade Your Rent' capital guardrail."""
        from chronox_finance import get_safe_trading_capital

        return get_safe_trading_capital(self.config.finance.db_path)

    def run_health_check(self) -> Dict[str, Any]:
        """Verify configuration, Finance DB, vault state, AI providers, and optional trading stack."""
        from chronox_finance.service import FinanceService

        svc = FinanceService(db_path=self.config.finance.db_path)
        try:
            health = svc.get_health()
            dash = svc.get_dashboard()
            ai_status = svc.get_ai_status()
            report = {
                "status": health.status,
                "db_ok": health.db_ok,
                "vault_state": health.vault_state,
                "finance_db": self.config.finance.db_path,
                "intelligence_mode": health.intelligence_mode,
                "active_provider": health.active_provider,
                "safe_to_spend_dollars": round(dash.safe_to_spend.safe_to_spend_cents / 100.0, 2),
                "safe_to_invest_dollars": round(dash.safe_to_invest_cents / 100.0, 2),
                "opencode_go_configured": ai_status["opencode_key_configured"],
                "gemini_configured": ai_status["gemini_api_key_set"],
                "codex_configured": ai_status["codex_key_configured"],
                "chat_wrapper_configured": ai_status["wrapper_key_configured"] or bool(ai_status["wrapper_base_url"]),
                "codex_cli_enabled": ai_status["codex_cli_enabled"],
                "polygon_configured": bool(self.config.polygon.api_key),
                "timescale_configured": bool(self.config.timescale.password),
            }
            return report
        finally:
            svc.close()

    def shutdown(self) -> None:
        if self.db_writer:
            self.db_writer.close()
        if self.polygon_client:
            self.polygon_client.close()


def parse_arguments(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ChronoX -- Local-First Industrial Personal Finance, Multi-Provider AI Coach & Market Data Platform",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--action",
        type=str,
        choices=["finance", "budget", "gui", "backfill", "health"],
        default="finance",
        help="Action to perform (default: finance web app)",
    )
    parser.add_argument("--config", type=str, help="Path to YAML/JSON configuration file")
    parser.add_argument("--host", type=str, help="Finance server host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, help="Fixed finance server port (default: 8742)")
    parser.add_argument("--db", type=str, help="Path to ChronoX Finance SQLite database")

    # Quick CLI budget & intelligence options
    parser.add_argument(
        "--mode",
        choices=["deterministic_only", "hybrid", "ai_standalone"],
        default=None,
        help="Intelligence mode (deterministic_only | hybrid | ai_standalone)",
    )
    parser.add_argument(
        "--provider",
        choices=["auto", "opencode_go", "gemini", "openai_codex", "chat_wrapper", "codex_cli"],
        default=None,
        help="AI provider (auto | opencode_go | gemini | openai_codex | chat_wrapper | codex_cli)",
    )
    parser.add_argument("--quick", type=str, help="Quick-add phrase, e.g. 'coffee 6.50' or 'impulse 180 keyboard'")
    parser.add_argument("--ask", type=str, help="Ask the ChronoX Financial Coach a question")
    parser.add_argument("--suggest", action="store_true", help="Print verified AI/quant auto-budget suggestions")
    parser.add_argument("--quant", action="store_true", help="Show cashflow runway & optimal marginal-ROI allocation")
    parser.add_argument("--portfolio", action="store_true", help="Show Stocks & Crypto portfolio holdings")
    parser.add_argument("--usage", action="store_true", help="Show AI token usage, cache hit rate & verified savings")
    parser.add_argument("--days", type=int, default=30, help="Lookback/forecast horizon in days (default: 30)")
    parser.add_argument("--credit", action="store_true", help="Show Credit Factor Analysis, Chase 5/24 & AZEO debt plan")
    parser.add_argument("--extra", type=float, default=250.0, help="Extra monthly dollars toward debt for --credit (default: 250)")
    parser.add_argument("--goal", nargs=2, metavar=("NAME", "PRICE"), help="Plan a credit-safe goal purchase")

    # Market data backfill options
    parser.add_argument("--tickers", type=str, help="Comma-separated tickers for backfill (e.g., AAPL,MSFT)")
    parser.add_argument("--start", type=str, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, help="End date (YYYY-MM-DD)")
    parser.add_argument("--skip-minute", action="store_true", help="Skip 1-minute bars during backfill")
    parser.add_argument("--flatfiles", action="store_true", help="Use Polygon S3 flat files")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_arguments(argv)
    orchestrator = ChronoXOrchestrator(config_path=args.config)
    try:
        if args.action in ("finance", "gui"):
            orchestrator.run_finance_app(
                host=args.host,
                port=args.port,
                db_path=args.db,
                intelligence_mode=args.mode,
                ai_provider=args.provider,
            )
            return 0

        if args.action == "budget":
            from chronox_finance.cli import run_cli

            cli_args: List[str] = []
            if args.db:
                cli_args.extend(["--db", args.db])
            if args.mode:
                cli_args.extend(["--mode", args.mode])
            if args.provider:
                cli_args.extend(["--provider", args.provider])
            if args.quick:
                cli_args.extend(["add", args.quick])
            elif args.ask:
                cli_args.extend(["ask", args.ask])
            elif args.suggest:
                cli_args.append("suggest")
            elif args.quant:
                cli_args.extend(["quant", "--days", str(args.days)])
            elif args.portfolio:
                cli_args.append("portfolio")
            elif args.usage:
                cli_args.extend(["usage", "--days", str(args.days)])
            elif args.credit:
                cli_args.extend(["credit", "--extra", str(args.extra)])
            elif args.goal:
                cli_args.extend(["goal", args.goal[0], args.goal[1]])
            else:
                cli_args.append("status")
            return run_cli(cli_args)

        if args.action == "health":
            report = orchestrator.run_health_check()
            print("\nChronoX Health Summary")
            print("-" * 40)
            for k, v in report.items():
                print(f"  {k:<26}: {v}")
            print()
            return 0

        if args.action == "backfill":
            if not args.tickers:
                print("Error: --tickers is required for backfill", file=sys.stderr)
                return 1
            tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
            res = orchestrator.run_backfill(
                tickers=tickers,
                start_date=args.start,
                end_date=args.end or datetime.now().strftime("%Y-%m-%d"),
                skip_minute=args.skip_minute,
                use_flatfiles=args.flatfiles,
            )
            print(res)
            return 0 if res.get("status") != "failed" else 1

        return 0
    except KeyboardInterrupt:
        print("\nStopped cleanly.")
        return 0
    finally:
        orchestrator.shutdown()


if __name__ == "__main__":
    sys.exit(main())
