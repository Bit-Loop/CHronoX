# ChronoX — ADHD-Friendly Self-Updating Auto-Budget & Market Platform

**ChronoX** unifies two tightly coupled financial missions in one secure, local-first codebase:

1. **ChronoX Finance (`chronox_finance/`)** — A calm, first-party-quality **ADHD Personal Finance, Quantitative Cashflow & Self-Updating Auto-Budgeting App** with **3 Intelligence Modes** (`deterministic_only` pure optimal quant math, `hybrid` quant + verified AI, and `ai_standalone`) supporting both **OpenCode Go** and optional **Google Gemini** API keys.
2. **ChronoX Trading & Market Core (`data/`, `models/`, `backtesting/`, `training/`, `pipelines/`)** — Multi-asset market data ingestion (Polygon.io + TimescaleDB), feature engineering, transformer/RL models, and backtesting — now protected by a hard **"Don't Trade Your Rent" Safe-to-Invest Capital Guardrail** directly linked to your personal budget.

---

## Why ChronoX Finance Works for ADHD Brains

Traditional finance apps fail neurodivergent users because they demand manual spreadsheets, display 25 overwhelming categories, ignore time-blindness, and punish overspending with angry red warnings (triggering Rejection Sensitive Dysphoria and app abandonment). ChronoX Finance shifts the burden from willpower to automation:

| ADHD Challenge | ChronoX Behavioral Solution |
| --- | --- |
| **Cognitive overload / decision paralysis** | Single hero **Safe to Spend Today** + **Safe Until Payday** metric and **5 Calm Money Jars** (Bills, Daily Living, Guilt-Free Fun, Safety & Future, ChronoX Invest Vault). Full math sits in an optional 1-click drawer. |
| **Task initiation friction** | **2-second Natural-Language Quick-Add** (`"coffee 6.50"`, `"paycheck +2450"`, `"impulse 189 keyboard"`) + drag-and-drop **Universal Statement Ingestion** (**CSV/TSV** with bank preamble skipping & European decimals, **OFX/QFX** with `<FITID>` & ledger balance, **QIF**, and **JSON**) with dry-run preview, internal CC-transfer isolation, SHA-256 deduplication, and auto-learning merchant rules. |
| **Time-blindness & forgotten renewals** | **Fuzzy Bill & Anomaly Engine** clusters mutating bank descriptors (`NETFLIX.COM*8821` / `NETFLIX INC CA`), tracks 8 cadences (weekly through annual including semi-monthly, bi-monthly, semi-annual), catches **double charges**, **missed bills**, and **silent price hikes**, and shields upcoming bills & unlinked debt minimums automatically before payday. |
| **Credit score anxiety & hidden interest traps** | **Niche Credit Score & Debt Optimizer** computes exact **AZEO (All Zero Except One)** pre-statement-close paydowns (`1%–8.9%` FICO sweet spot), guards against the **All-Zero Inactivity Penalty**, **lost grace periods**, **0% promo APR cliffs**, **oldest-account AAoA closures**, and **unshielded autopay**, plus simulates **4 exact-cent payoff strategies** (ADHD Hybrid, Credit-First FICO Turbo, Avalanche, Snowball) and **balance-transfer breakeven**. |
| **Wanting big purchases without wrecking credit** | **Credit-Safe Goal Optimizer & Purchase Planner** turns any target (e.g., `$1,000` gadget) into **3 verified paths**: Cash Sinking Fund (with real subscription/impulse accelerators), Credit-Optimized Rewards Buy (post-statement-close golden float + pre-close paydown to stay `<= 8.9%` utilization), and ADHD Balanced Debt+Goal Split. |
| **Dopamine impulse spending** | **Impulse Cooling-Off Vault ("Park It, Don't Fight It")** lets you park temptations for 24–72h, translates prices into **hours of work** & **goal delay days**, and celebrates skipped items with **Dopamine XP** & running money-saved wins. |
| **Rejection Sensitive Dysphoria (RSD)** | Zero shaming copy, calm visual states, **1-click Zero-Guilt Auto-Rebalance**, and a **Streak Grace Freeze** so missing a week never wipes your progress. |
| **Boring monthly reviews** | **5-Minute Weekly Money Reset Wizard** (4 bite-sized steps: 1-tap triage, bill/sub check, matured impulse wins, 1-click auto-rebalance). |
| **Trading rent money impulsively** | **ChronoX Safe-to-Invest Firewall** clamps tradable capital to `$0.00` until upcoming bills, credit cards, and your emergency buffer are 100% covered. |

---

## Self-Updating Auto-Budgeting Engine

Every transaction, paycheck, CSV import, or bill update runs the deterministic exact-integer-cent loop (`chronox_finance/engines/autobudget.py`):

1. **Zero floating-point penny bugs**: All balances, allocations, and rollovers use exact integer cents (`int`).
2. **Safe-to-Spend Firewall**:
   $$\text{SafeToSpend} = \max\Big(0,\;\text{LiquidCash} - \text{UnpaidBillsUntilPayday} - \text{CreditCardReserve} - \text{LockedJars} - \text{EmergencyBuffer}\Big)$$
   $$\text{DailySafeToSpend} = \left\lfloor \frac{\text{SafeToSpend}}{\text{DaysUntilNextPayday}} \right\rfloor$$
3. **Adaptive 3-Month EMA Allocation**: Covers recurring bills and unlinked credit/debt minimum payments first, then blends your jar target percentages with 3-month trailing reality so your budget self-corrects without manual tweaking.
4. **Transparent Audit Trail**: Every self-update is logged in `budget_events` with exact reasoning.

---

## Niche Credit Score, Debt & Goal Optimizer

- **FICO & AZEO Engine (`chronox_finance/engines/credit_debt.py`)**: Evaluates aggregate and per-card utilization tiers (`1%–8.9%` optimal, `>= 29%`/`49%`/`89%` cliffs, and `0%` All-Zero penalty), schedules exact pre-statement-close paydowns 3 days ahead of each card's closing date, flags lost grace periods, 0% promo APR cliffs, disabled autopay, AAoA oldest-account risk, and DTI benchmarks, and runs an exact-cent 4-strategy debt simulator (`adhd_hybrid`, `credit_first`, `avalanche`, `snowball`) plus a balance-transfer breakeven calculator.
- **Credit-Safe Goal Planner (`chronox_finance/engines/goal_planner.py`)**: Models major purchases (e.g. a `$1,000` gadget) across cash sinking fund, credit-optimized rewards float (only unlocked when a card has an active grace period and enough headroom to stay `<= 8.9%` utilization), and balanced debt+goal splits—automatically factoring in real cancelable subscriptions and high-APR opportunity cost.

---

## Quantitative Math & 3-Mode Intelligence Architecture

ChronoX Finance gives you full control over how intelligence is generated — switchable live in the UI, via API (`/api/settings/ai`), CLI (`--mode`, `--provider`), or `.env`:

| Intelligence Mode | Behavior | External Calls |
| --- | --- | --- |
| **`deterministic_only`** (`🧮 Quant Math`) | Runs `chronox_finance/engines/quant_math.py` exclusively: 30–90 day exact-cent cashflow runway & overdraft simulator, trailing 90-day spending volatility & P50/P75/P90 dynamic emergency buffer sizing, marginal-ROI multi-objective surplus allocator (autopay → 0% promo cliff → high-APR debt → AZEO utilization cliff → statistical buffer → goals → Invest Vault), and pure-math Q&A / suggestions. | **Zero** |
| **`hybrid`** (`⚡ Hybrid`, default) | Feeds verified ledger facts + `QuantForecastReport` into your active AI provider (**OpenCode Go** or **Google Gemini**), verifies every jar/bill ID, transfer bound, dollar figure, and RSD-safe tone via `SuggestionValidator`, with automatic fallback to `QuantMathEngine`. | Cached & quota-capped |
| **`ai_standalone`** (`✨ AI Only`) | Sends PII-redacted live financial context directly to your active AI provider for flexible strategic coaching, with RSD-safe tone filtering, hard action safety bounds (never raids `Bills & Essentials`), and transparent `ai_standalone` provenance labeling. | Cached & quota-capped |

### Multi-Provider AI: OpenCode Go + Optional Google Gemini

- **Provider Selection (`ai_provider`)**: Choose `auto` (prefers OpenCode Go when configured, seamlessly falls back to Gemini), `opencode_go`, or `gemini`.
- **OpenCode Go (`https://opencode.ai/zen/go/v1`)**: Uses `OPENCODE_GO_API_KEY` / `OPENCODE_API_KEY` or a key saved in Settings (`0600` local store). Supports live model discovery (`GET /api/ai/models?provider=opencode_go`) and dual OpenAI/Anthropic protocol routing.
- **Google Gemini (`https://generativelanguage.googleapis.com/v1beta`)**: Optional `GEMINI_API_KEY` / `GOOGLE_API_KEY` env var or key saved in Settings (`0600` local store). API keys are sent **strictly via the `x-goog-api-key` HTTP header** (never in URL query parameters), never returned to the browser, and never written to JSON backups. Supports live model discovery (`gemini-2.5-flash`, `gemini-2.5-pro`, `gemini-2.5-flash-lite`, `gemini-2.0-flash`).
- **Hallucination & Safety Verification (`chronox_finance/ai/validator.py`)**:
  - All core financial & quant math is computed deterministically in exact integer cents in Python *before* any LLM call.
  - PII (SSNs, card numbers, emails, phone numbers, account numbers) and prompt-injection payloads are stripped before outbound requests.
  - Every hybrid suggestion is cross-checked against live jar IDs, bill IDs, available surpluses, protected `Bills & Essentials` lockouts, cited dollar figures, and an RSD-safe tone filter.

---

## Quick Start

### 1. Desktop App (Recommended — Windows, Linux & macOS)

ChronoX Finance ships as a hardened, local-first **Electron desktop app** with an embedded zero-Python backend sidecar, frameless Dark/Light window chrome, live System Tray **Safe-to-Spend** readout, and a global **ADHD Quick-Capture HUD** (`Ctrl+Shift+Space`).

| Platform | Architectures | Quick Setup / Build |
| --- | --- | --- |
| **Linux (Debian/Ubuntu `.deb`, Arch/CachyOS `.pacman`, `.AppImage`)** | `x86_64`, `aarch64` | `./scripts/install-linux.sh --dev --demo` (or `--deb` / `--pacman` / `--appimage` with `--with-backend`) |
| **Arch Linux `PKGBUILD`** | `x86_64`, `aarch64` | `cd desktop/packaging/arch && CHRONOX_LOCAL_REPO=1 makepkg -si` (or `sudo pacman -U chronox-finance-*.pacman`) |
| **Windows (NSIS Setup + Portable `.exe`)** | `x64`, `ARM64` | `.\scripts\install-windows.ps1 -Demo` (or `-BuildInstaller` for `.exe` packages) |
| **macOS (`.dmg`)** | `arm64`, `x64` | `make dev && make desktop-install && make desktop-demo` (`make desktop-mac` for `.dmg`) |

See [`desktop/README.md`](desktop/README.md) and [`desktop/packaging/arch/README.md`](desktop/packaging/arch/README.md) for full packaging, Wayland flags, and release details.

### 2. Install Python Core with `uv` (or `pip`)

```bash
cp .env.example .env
# Optional: set OPENCODE_GO_API_KEY in .env (or configure it inside Settings UI)

make dev
# Or manually:
# uv venv .venv --python 3.12
# uv pip install --python .venv/bin/python -e ".[dev]"
```

### 3. Launch the ADHD Auto-Budgeting Web App

```bash
# Start fresh on http://127.0.0.1:8742
.venv/bin/python main.py --action finance

# Or start preloaded with a realistic 90-day ADHD demo workspace
.venv/bin/python main.py --action finance --demo
```

### 4. Fast Terminal Quick-Capture CLI

```bash
.venv/bin/python main.py --action budget --quick "coffee 6.50"
.venv/bin/python main.py --action budget --quick "paycheck +2450"
.venv/bin/python main.py --action budget --quick "impulse 189 mechanical keyboard"
.venv/bin/python main.py --action budget --quant --days 30
.venv/bin/python main.py --action budget --mode deterministic_only --suggest
.venv/bin/python main.py --action budget --mode hybrid --provider gemini --ask "I want a $1000 gadget — how can I afford it while keeping my credit optimal?"
.venv/bin/python main.py --action budget --credit --extra 300
.venv/bin/python main.py --action budget --goal "Mac Mini M4" 1000
.venv/bin/python main.py --action health

# Or invoke the finance CLI directly:
.venv/bin/python -m chronox_finance.cli quant --days 30
.venv/bin/python -m chronox_finance.cli --mode deterministic_only suggest
```

### 5. Docker Deployment

```bash
# Run ChronoX Finance locally (bound strictly to 127.0.0.1:8742)
docker compose up -d --build chronox-finance

# Optional: also start TimescaleDB + Redis for market data / trading core
docker compose --profile trading up -d
```

---

## Security Architecture

- **Localhost-only default binding** (`127.0.0.1:8742`)
- **Optional Passcode Lock**: PBKDF2-HMAC-SHA256 (600,000 iterations, per-user random salt) + signed `HttpOnly`, `SameSite=Strict` session cookies
- **CSRF Protection**: Origin/Host verification on all state-changing `/api/*` requests
- **Strict Security Headers**: `Content-Security-Policy` (`script-src 'self'`, zero external CDNs), `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`
- **Secret Isolation**: Zero hardcoded passwords; environment variable interpolation (`${VAR:-default}`) with automatic secret redaction on config export; `0600` local secret storage
- **SQL & CSV Hardening**: Parameterized SQLite & `psycopg2.sql.Identifier` queries with explicit timeframe allowlists; spreadsheet formula injection neutralization (`=`, `+`, `-`, `@`) on CSV export; SSRF protection on external API URLs

---

## Testing & Code Quality

```bash
make test   # Runs unit + integration + security test suite
make lint   # Runs ruff security & style checks
```

## Project Layout

```text
CHronoX/
├── chronox_finance/          # ADHD Auto-Budget, Quant Math Engine & Multi-Provider AI Coach
│   ├── ai/                   # OpenCode Go + Google Gemini clients, validator & 3-mode coach
│   ├── engines/              # Auto-budget, quant_math (runway/volatility/ROI), NLP quick-add, categorizer, fuzzy bills, importer, credit/debt, goal planner, ADHD
│   ├── web/                  # Zero-CDN Apple/Google-craft Dark/Light UI, SVG runway chart, ⌘K command palette
│   ├── api.py                # FastAPI server + security middleware + structured JSON errors
│   ├── cli.py                # Fast terminal capture & quant CLI
│   ├── security.py           # PBKDF2 auth, signed cookies, rate limiter, PII/CSV sanitizers, 0600 secrets
│   ├── service.py            # Self-updating orchestrator facade
│   └── storage.py            # Atomic SQLite WAL database + additive migrations + JSON backup/restore
├── desktop/                  # Hardened Electron desktop shell (Windows x64/arm64, Linux .deb/.pacman/.AppImage, macOS)
│   ├── src/                  # Main process, sidecar manager, tray, splash bootloader & Quick-Capture HUD
│   ├── build/icons/          # Cross-platform multi-res PNG, Windows ICO, macOS ICNS & tray icon assets
│   ├── packaging/            # PyInstaller backend.spec & Arch Linux PKGBUILD / .desktop
│   ├── scripts/              # backend_entry.py, build-backend.mjs & generate-icons.py
│   └── electron-builder.yml  # Cross-platform installer & package configuration
├── scripts/                  # 1-command installers (install-linux.sh, install-windows.ps1) & utilities
├── config/                   # 12-factor ConfigManager with env expansion & secret redaction
├── data/                     # Market data ingestion (Polygon.io) & TimescaleDB storage
├── models/                   # Stockformer, TFT, FinBERT, RL trading agents
├── backtesting/              # Strategy backtesting & performance analysis
├── training/ & pipelines/    # Multi-scale training & Airflow DAGs
├── tests/                    # Unit & end-to-end API/security tests
├── docs/                     # Documentation & archived historical logs (docs/archive/)
├── main.py                   # Unified CLI entrypoint
├── pyproject.toml            # Packaging, pytest & ruff config
└── docker-compose.yml        # Finance + optional Trading/Monitoring stack
```
