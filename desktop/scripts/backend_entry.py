#!/usr/bin/env python3
"""
Standalone backend sidecar entrypoint for the ChronoX Finance desktop app.

Supports both source execution (via `.venv/bin/python`) and frozen PyInstaller
builds (one-dir and one-file). Automatically locates `chronox_finance/web/`
inside PyInstaller's `sys._MEIPASS` bundle or via `CHRONOX_WEB_DIR`, ensures
the SQLite parent directory exists, and starts Uvicorn bound strictly to
localhost by default.
"""

from __future__ import annotations

import argparse
import multiprocessing
import os
import sys
from pathlib import Path
from typing import List, Optional

# Ensure repository root is importable when invoked directly from source
_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent.parent
if not getattr(sys, "frozen", False) and str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def resolve_web_dir(explicit_dir: Optional[str] = None) -> Optional[Path]:
    """Resolve the `chronox_finance/web` static directory in both source and frozen modes."""
    candidates: List[Path] = []

    if explicit_dir:
        candidates.append(Path(explicit_dir).expanduser())

    env_dir = os.getenv("CHRONOX_WEB_DIR")
    if env_dir:
        candidates.append(Path(env_dir).expanduser())

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        mp = Path(meipass)
        candidates.extend(
            [
                mp / "chronox_finance" / "web",
                mp / "web",
            ]
        )

    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.extend(
            [
                exe_dir / "chronox_finance" / "web",
                exe_dir / "_internal" / "chronox_finance" / "web",
                exe_dir / "web",
            ]
        )

    candidates.append(_REPO_ROOT / "chronox_finance" / "web")

    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if (resolved / "index.html").is_file():
            return resolved

    return None


def _env_truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    # Accept optional leading "serve" subcommand for CLI parity with `chronox_finance.cli`
    if raw_argv and raw_argv[0] == "serve":
        raw_argv = raw_argv[1:]

    default_host = os.getenv("CHRONOX_FINANCE_HOST", "127.0.0.1")
    default_port = int(os.getenv("CHRONOX_FINANCE_PORT", "8742"))
    default_db = os.getenv("CHRONOX_FINANCE_DB_PATH", ".chronox/finance.sqlite3")
    default_log_level = os.getenv("CHRONOX_LOG_LEVEL", "info").lower()

    parser = argparse.ArgumentParser(
        prog="chronox-backend",
        description="ChronoX Finance local desktop API sidecar",
    )
    parser.add_argument("--host", default=default_host, help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=default_port, help="Bind port (default: 8742)")
    parser.add_argument("--db", default=default_db, help="Path to SQLite database file")
    parser.add_argument(
        "--web-dir",
        default=None,
        help="Explicit path to chronox_finance/web static assets",
    )
    parser.add_argument(
        "--mode",
        choices=["deterministic_only", "hybrid", "ai_standalone"],
        default=None,
        help="Optional intelligence mode override",
    )
    parser.add_argument(
        "--provider",
        choices=["auto", "opencode_go", "gemini", "openai_codex", "chat_wrapper", "codex_cli"],
        default=None,
        help="Optional AI provider override",
    )
    parser.add_argument(
        "--log-level",
        choices=["critical", "error", "warning", "info", "debug"],
        default=default_log_level if default_log_level in {"critical", "error", "warning", "info", "debug"} else "info",
        help="Uvicorn log verbosity",
    )
    return parser.parse_args(raw_argv)


def main(argv: Optional[List[str]] = None) -> int:
    multiprocessing.freeze_support()

    from dotenv import load_dotenv

    load_dotenv()

    args = parse_args(argv)

    # Ensure database parent folder exists before SQLite opens it
    db_path = args.db
    if db_path and db_path != ":memory:":
        db_file = Path(db_path).expanduser()
        if not db_file.is_absolute():
            db_file = (Path.cwd() / db_file).resolve()
        db_file.parent.mkdir(parents=True, exist_ok=True)
        db_path = str(db_file)

    import uvicorn

    import chronox_finance.api as api_module
    from chronox_finance.models import AIProvider, IntelligenceMode, ProfileSettingsUpdate
    from chronox_finance.service import FinanceService

    web_dir = resolve_web_dir(args.web_dir)
    if web_dir is not None:
        api_module.WEB_DIR = web_dir
        os.environ["CHRONOX_WEB_DIR"] = str(web_dir)
    elif not api_module.WEB_DIR.exists():
        print(
            f"[chronox-backend] Warning: web assets directory not found (checked {api_module.WEB_DIR})",
            file=sys.stderr,
            flush=True,
        )

    svc = FinanceService(db_path=db_path)

    if args.mode or args.provider:
        svc.update_settings(
            ProfileSettingsUpdate(
                intelligence_mode=IntelligenceMode(args.mode) if args.mode else None,
                ai_provider=AIProvider(args.provider) if args.provider else None,
            )
        )

    app = api_module.create_app(db_path=db_path, service=svc)

    print(
        f"[chronox-backend] Listening on http://{args.host}:{args.port} (db={db_path})",
        flush=True,
    )

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        use_colors=sys.stderr.isatty(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
