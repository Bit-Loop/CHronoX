![ChronoX Finance](docs/assets/chronox-hero.jpg)

# ChronoX Finance

**A local-first, dark-industrial personal finance command center unifying natural-language `--say` capture, statistical CVaR/KKT One-Tap optimization, Apple Numbers graphical workbooks, encrypted secret management, 2-year bank sync, portfolio tracking, credit factor analysis, and automagic local AI.**

[![CI](https://github.com/Bit-Loop/CHronoX/actions/workflows/ci.yml/badge.svg)](https://github.com/Bit-Loop/CHronoX/actions/workflows/ci.yml)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776ab?style=flat-square)
![Desktop Linux · Windows · macOS](https://img.shields.io/badge/desktop-Linux%20%7C%20Windows%20%7C%20macOS-1f2937?style=flat-square)
![Local-First](https://img.shields.io/badge/storage-local--first%20%7C%20127.0.0.1-10b981?style=flat-square)
[![License: MIT](https://img.shields.io/badge/license-MIT-64748b?style=flat-square)](LICENSE)

---

## Everyday CLI Workflow (`--say`, `--one-tap`, `--export`)

Speak to your ledger in plain English, optimize cashflow in one command, or export presentation-ready spreadsheets directly from your terminal:

```bash
# Say anything — logs expenses (preserving "because ..." reasons), moves savings, splits, or answers questions
.venv/bin/python main.py --say "I spent $5.75 on coffee because early flight"
.venv/bin/python main.py "put $100 in savings"
.venv/bin/python main.py --say "Can I afford a $400 monitor?"
.venv/bin/python main.py --undo                              # 1-step atomic undo of latest --say or --one-tap

# AI + Statistical Quant One-Tap Optimizer (Huber/MAD EWMA, 95% CVaR cushion, KKT goal allocation)
.venv/bin/python main.py --one-tap                          # Preview plan, CVaR 95% cushion & survival probability
.venv/bin/python main.py --one-tap --apply --days 30        # Apply atomically (undoable via --undo)

# Apple Numbers Graphical Workbook (.xlsx with 6 sheets & 5 live vector charts) & Executive CSVs
.venv/bin/python main.py --export numbers --out chronox-report.xlsx
.venv/bin/python main.py --export csv --out transactions.csv
.venv/bin/python main.py --export csv-pack --out chronox-pack.zip
```

---

## Core Capabilities

| Pillar | What Ships Today |
| :--- | :--- |
| **Unified `--say` Harness & Automagic Local AI** | Single natural-language command bar and CLI (`--say`) preserving causal reasons, exact-sum splits, savings transfers, and 1-step Undo. Out-of-the-box local AI CLI auto-discovery & automatic failover across **Claude Code CLI**, **OpenCode CLI**, **Codex CLI**, and **Antigravity CLI** — plus optional **OpenCode Go**, **Google Gemini**, **OpenAI / Codex**, and **Chat-Wrapper** endpoints, or 100% offline **Quant Math** (`deterministic_only`). |
| **Statistical One-Tap Optimizer** | Seeded 500-path bootstrap cashflow runway with Huber/MAD-clipped EWMA burn rates, **95% Expected Shortfall (`CVaR`)** tail cushion, 5% overdraft-risk guardrail, Mann-Kendall spend drift, HHI concentration telemetry, KKT-optimal goal funding, and atomic **Apply → Undo**. |
| **Apple Numbers & Executive CSV Export Studio** | Zero-dependency 6-sheet **Apple Numbers / Excel Graphical Workbook (`.xlsx`)** with 5 live DrawingML vector charts (`Executive Overview`, `Cashflow & Runway`, `Portfolio & Wealth`, `Credit & Debt Analysis`, `Goals & Bills`, `Transactions Ledger`), UTF-8 BOM **Transactions CSV**, and 7-table **Executive CSV Pack (`.zip`)**. |
| **Bulletproof Banking, Brokers & Credit** | 1-step BYOK **Plaid Hosted Link** (`730-day` history, persistent server-side poller, loopback completion callback, multi-item exchange, automatic `NOT_READY` historical backfill retries), **SimpleFIN Bridge**, **Navy Federal CSV/PDF** + Desktop Auto-Import Drop Folder, read-only **Coinbase**, **Robinhood Crypto**, **Schwab OAuth2**, and **Credit Factor Analysis** (AZEO, Chase 5/24, optional BYOK Credit Score APIs). |
| **Envelope Vault Security** | Mandatory launch password (`PBKDF2-HMAC-SHA256`, 600,000 iterations), random 32-byte DEK sealing `secrets.enc` (`AES-256-GCM`, `0600`), one-time Recovery Key (200-bit Base32), strict loopback `Host`/`Origin` DNS-rebinding defense, `SameSite=Strict` `HttpOnly` session cookies, per-boot CSRF header tokens, and zero-CDN CSP. |

### Security & Storage Transparency

- **Encrypted at rest (`secrets.enc`, `0600`)**: API keys, Plaid/SimpleFIN/broker credentials, pending Hosted Link sessions, and IMAP App Passwords are sealed with AES-256-GCM under a random 32-byte DEK wrapped by your vault password and one-time Recovery Key.
- **Unencrypted local ledger (`finance.sqlite3`, `0600`)**: Your SQLite ledger database is stored **unencrypted on disk** with owner-only `0600` file permissions (inside a `0700` directory) for fast local querying and safe pre-migration backups (`backups/pre-vN.db`). Use OS full-disk encryption (LUKS / BitLocker / FileVault) for disk-level ledger protection.
- **Auto-lock behavior**: Locking drops active DEK references and halts background Plaid pollers/retry timers cleanly until unlocked. Because Python cannot guarantee deterministic RAM zeroization of immutable buffers, ChronoX does not claim hardware-level memory wiping.

---

## Quick Start

### 1. Desktop App (Recommended)

Hardened Electron shell with an embedded PyInstaller backend sidecar, live System Tray Safe-to-Spend readout, and global Quick-Capture HUD (`Ctrl+Shift+Space` / `Cmd+Shift+Space`).

| Platform | Architectures | Setup / Build |
| :--- | :--- | :--- |
| **Linux (`.deb`, `.pacman`, `.AppImage`)** | `x86_64`, `aarch64` | `./scripts/install-linux.sh --dev --launch` (or `--deb` / `--pacman` / `--appimage` with `--with-backend`) |
| **Arch Linux (`PKGBUILD`)** | `x86_64`, `aarch64` | `cd desktop/packaging/arch && CHRONOX_LOCAL_REPO=1 makepkg -si` |
| **Windows (NSIS + Portable `.exe`)** | `x64`, `ARM64` | `.\scripts\install-windows.ps1 -Launch` (or `-BuildInstaller` for `.exe` packages) |
| **Makefile (Linux / macOS)** | `x64`, `arm64` | `make dev && make desktop-install && make desktop` |

See [`desktop/README.md`](desktop/README.md) and [`desktop/packaging/arch/README.md`](desktop/packaging/arch/README.md) for packaging, Wayland flags, and release details.

### 2. Web App & Terminal CLI

```bash
cp .env.example .env
make dev                                       # Create .venv (Python 3.12) & install deps
make finance                                   # Launch on http://127.0.0.1:8742

# Legacy subcommands remain 100% supported
.venv/bin/python main.py --action finance --host 127.0.0.1 --port 8742
.venv/bin/python main.py --action budget --portfolio
.venv/bin/python main.py --action budget --mode deterministic_only --suggest
```

### 3. Docker & Quality Checks

```bash
docker compose up -d --build chronox-finance   # Bound strictly to 127.0.0.1:8742
make test && make lint                         # Full pytest suite + ruff lint & Bandit checks
```

---

## Architecture & Roadmap

ChronoX separates a deterministic exact-integer-cent Python engine (`chronox_finance/`) from a zero-CDN ES-module UI (`chronox_finance/web/`) and Electron shell (`desktop/`), alongside an optional algorithmic market core (`data/`, `models/`, `backtesting/`) gated by a hard **Safe-to-Invest** liquidity firewall.

- **Today**: Local-first desktop & loopback web app (`127.0.0.1`) powered by versioned **`/api/v1/*`** JSON contracts, idempotent SQLite migrations, and zero third-party telemetry.
- **Next**: Self-hosted web deployment and a native **SwiftUI iOS client** built directly against the same `/api/v1/*` API surface.

## License

MIT — see [`LICENSE`](LICENSE). Educational and personal finance tooling; not financial advice.
