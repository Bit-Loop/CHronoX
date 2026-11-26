"""Full-stack live Chromium/Electron E2E tests for the ChronoX consumer overhaul.

Boots a real live `uvicorn` FastAPI backend + `xvfb-run` + local Electron Chromium
and verifies end-to-end:
1. Zero-overflow pixel geometry across BOTH empty ($0) AND populated profiles at
   1380x880, 1024x768, and 375x812 across all 8 tabs:
   - Cash Flow empty state has 0px overflow (no giant 1,174px icon, no "Cashflow Sanke" clipping)
   - All 5 `.jar-card` elements (and `.jar-top` rows) have `scrollWidth <= clientWidth + 1`
   - `.tabbar` at 1024x768 and 1380x880 fits without clipping Coach or Settings
   - Zero developer-jargon strings (`PBKDF2`, `DEK`, `Base32`, `BYOK`, `Coffee $6.50`,
     `Paycheck +$2400`, `Simulator`, `What-If`, `Sandbox`) rendered anywhere
2. Live "Say or Log Anything" Bar (`POST /api/v1/ai/say`): submits natural language with
   causal reason -> verifies ledger row created with `$14.50` & `"client pitch ran late"` ->
   clicks inline Undo -> verifies row undone.
3. Live "One-Tap Optimize" Modal (`POST /api/v1/optimize/one-tap`): verifies CVaR cushion,
   survival %, grounded AI rationales, and actions render -> Apply One-Tap Plan -> Undo!
4. Live Export Studio (`GET /api/v1/export/numbers` & `/api/v1/export/csv?pack=true`):
   clicks UI export buttons + unzips `.xlsx` (6 sheets, 5 `<c:chartSpace>` charts) and
   `.zip` (7 CSV files).
5. Live Automagic Local AI CLI status (`GET /api/v1/ai/local-clis` + per-CLI Test button)
   and Plaid 1-step Hosted Link connect -> check -> sync flow in Settings.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import pytest
import uvicorn

from chronox_finance.ai.local_cli_clients import LocalCLIRegistry, LocalCLIStatus
from chronox_finance.ai.opencode_client import CompletionResult, LLMProviderError
from chronox_finance.api import create_app
from chronox_finance.service import FinanceService
from tests.fixtures.seed_workspace import seed_workspace

REPO_ROOT = Path(__file__).resolve().parents[2]
ELECTRON_BIN = REPO_ROOT / "desktop" / "node_modules" / ".bin" / "electron"
XVFB_BIN = shutil.which("xvfb-run")


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _build_populated_backup_json(tmp_path: Path) -> Path:
    seed_dir = tmp_path / "seed_populated"
    seed_dir.mkdir(parents=True, exist_ok=True)
    svc = FinanceService(db_path=str(seed_dir / "seed.sqlite3"), secret_store_path=seed_dir / "secrets.enc")
    try:
        svc.secrets.unlock_ephemeral_for_testing()
        seed_workspace(svc)
        svc.storage.upsert_holding(
            {
                "symbol": "VTI",
                "name": "Vanguard Total Stock Market ETF",
                "asset_class": "etf",
                "quantity_e8": 10 * 10**8,
                "price_e8": 265 * 10**8,
                "cost_basis_cents": 240_000,
                "cost_basis_known": True,
            }
        )
        svc.storage.upsert_holding(
            {
                "symbol": "AAPL",
                "name": "Apple Inc.",
                "asset_class": "equity",
                "quantity_e8": 8 * 10**8,
                "price_e8": 225 * 10**8,
                "cost_basis_cents": 150_000,
                "cost_basis_known": True,
            }
        )
        svc.storage.update_settings({"onboarding_completed": 1})
        backup_payload = svc.storage.export_backup()
        out_path = tmp_path / "chronox_populated_backup.json"
        out_path.write_text(json.dumps(backup_payload), encoding="utf-8")
        return out_path
    finally:
        svc.close()


class _E2ELocalCLIClient:
    """Deterministic local CLI double returning grounded JSON for --say and One-Tap."""

    def __init__(
        self,
        provider_name: str,
        *,
        installed: bool = True,
        model: str = "opencode-go/glm-5.3-flash",
        auth_ready: Optional[bool] = True,
        registry: Optional[LocalCLIRegistry] = None,
    ) -> None:
        self.provider_name = provider_name
        self.model = model
        self._installed = installed
        self._auth_ready = auth_ready
        self._registry = registry

    @property
    def is_configured(self) -> bool:
        return self._installed

    def chat_completion(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 900,
        json_mode: bool = False,
        use_cache: bool = True,
    ) -> CompletionResult:
        if not self._installed or self._auth_ready is False:
            if self._registry is not None:
                self._registry.mark_auth_failure(self.provider_name, "Authentication required")
            raise LLMProviderError(f"{self.provider_name} is not authenticated")

        # 1. One-Tap Narrative synthesis (extract exact grounded action IDs & figures from prompt)
        if "ONETAP_PLAN" in system_prompt or "ONETAP_PLAN" in user_prompt:
            payload = self._build_grounded_onetap_json(user_prompt)
            content = json.dumps(payload)
        elif "artisanal coffee" in user_prompt.lower():
            content = json.dumps(
                {
                    "intent_kind": "ledger_mutation",
                    "assistant_message": "Logged $14.50 at Artisanal Coffee (note: client pitch ran late).",
                    "extracted_reason": "client pitch ran late",
                    "actions": [
                        {
                            "action_type": "create_transaction",
                            "payload": {
                                "tx_type": "expense",
                                "amount_cents": 1450,
                                "merchant": "Artisanal Coffee",
                                "subcategory": "Coffee & Treats",
                                "memo": "Client pitch ran late",
                            },
                            "summary": 'Log expense $14.50 at "Artisanal Coffee"',
                        }
                    ],
                }
            )
        else:
            content = json.dumps(
                {
                    "intent_kind": "question_advice",
                    "assistant_message": "Your cashflow is tracking calmly against your target safety cushion.",
                    "actions": [],
                }
            )

        return CompletionResult(
            content=content,
            model=self.model,
            protocol_used="cli",
            provider=self.provider_name,
            cached=False,
            input_tokens=140,
            output_tokens=62,
            cached_tokens=0,
            latency_ms=34,
        )

    @staticmethod
    def _build_grounded_onetap_json(user_prompt: str) -> Dict[str, Any]:
        plan_obj: Dict[str, Any] = {}
        m = re.search(r"\{.*\}", user_prompt, re.DOTALL)
        if m:
            try:
                plan_obj = json.loads(m.group(0))
            except Exception:
                plan_obj = {}
        actions = plan_obj.get("actions") or []
        cvar_str = str(plan_obj.get("cvar_95_buffer_dollars") or "$0.00")
        daily_str = str(plan_obj.get("confidence_95_daily_safe_dollars") or "$0.00")
        savings_str = str(plan_obj.get("total_annual_savings_dollars") or "$0.00")
        ruin = float(plan_obj.get("ruin_probability_pct") or 0.0)
        rationales = []
        for idx, act in enumerate(actions):
            aid = str(act.get("action_id") or "")
            if not aid:
                continue
            atype = str(act.get("action_type") or "")
            amt = int(act.get("amount_cents") or 0)
            amt_str = f"${amt / 100:,.2f}"
            raw_target = str(act.get("target_name") or "")
            clean_target = re.sub(r"</?UNTRUSTED_DATA>", "", raw_target)
            clean_target = re.sub(r"\$?\d[\d,]*(?:\.\d+)?%?", "", clean_target).strip(" -:")
            if atype == "set_emergency_buffer":
                txt = (
                    f"AI-verified cushion step: reserve {amt_str} to align directly with your {cvar_str} tail-risk buffer."
                )
            elif atype == "set_safe_invest_cap":
                txt = f"AI-verified guardrail: cap discretionary brokerage transfers at {amt_str} above protected bills."
            elif atype == "fund_goal":
                goal_hint = f" ({clean_target})" if clean_target else ""
                verb = "accelerate milestone progress" if idx % 2 == 0 else "close the remaining target gap"
                txt = f"AI-verified goal boost{goal_hint}: direct {amt_str} of unlocked surplus to {verb}."
            else:
                card_hint = f" on {clean_target}" if clean_target else ""
                reason = (
                    "curb reported statement balance" if idx % 2 == 0 else "trim revolving interest drag ahead of close"
                )
                txt = f"AI-verified card move{card_hint}: schedule {amt_str} early to {reason}."
            rationales.append({"action_id": aid, "rationale": txt})
        return {
            "executive_summary": (
                f"Your 95%-safe daily spend is {daily_str} with a {cvar_str} tail-risk cushion, "
                f"{ruin:.1f}% overdraft probability, and {savings_str} in projected annual savings. "
                "Every recommended move protects essential bills and stays one-click undoable."
            ),
            "action_rationales": rationales,
        }

    def test_connection(self, deep: bool = True, *, force_refresh: bool = False) -> Dict[str, Any]:
        if not self._installed:
            return {"ok": False, "provider": self.provider_name, "model": self.model, "message": "Not installed"}
        if self._auth_ready is False:
            return {
                "ok": False,
                "provider": self.provider_name,
                "model": self.model,
                "latency_ms": 25,
                "message": "Sign-in required for Codex CLI",
            }
        return {
            "ok": True,
            "provider": self.provider_name,
            "model": self.model,
            "latency_ms": 22,
            "message": f"{self.provider_name} authenticated and ready",
        }

    def list_models(self, force_refresh: bool = False) -> Dict[str, Any]:
        return {"provider": self.provider_name, "live": False, "models": [{"id": self.model}]}


class _E2ELocalCLIRegistry(LocalCLIRegistry):
    """Deterministic registry exposing realistic detected local AI CLIs for Browser E2E."""

    def __init__(self) -> None:
        super().__init__()
        self._meta = {
            "claude_cli": {
                "display_name": "Claude Code CLI",
                "binary_name": "claude",
                "priority": 10,
                "installed": False,
                "auth_ready": None,
                "binary_path": "",
                "version": "",
                "detail": "Not installed on this device",
                "model": "claude-sonnet",
            },
            "opencode_cli": {
                "display_name": "OpenCode CLI",
                "binary_name": "opencode",
                "priority": 20,
                "installed": True,
                "auth_ready": True,
                "binary_path": "/home/user/.opencode/bin/opencode",
                "version": "2.2.7",
                "detail": "Ready (v2.2.7)",
                "model": "opencode-go/glm-5.3-flash",
            },
            "codex_cli": {
                "display_name": "OpenAI Codex CLI",
                "binary_name": "codex",
                "priority": 30,
                "installed": True,
                "auth_ready": False,
                "binary_path": "/usr/bin/codex",
                "version": "0.167.2",
                "detail": "Sign-in expired — run codex login",
                "model": "codex-default",
            },
            "agy_cli": {
                "display_name": "Antigravity CLI (agy)",
                "binary_name": "agy",
                "priority": 40,
                "installed": True,
                "auth_ready": None,
                "binary_path": "/home/user/.local/bin/agy",
                "version": "1.5.15",
                "detail": "Installed (v1.5.15)",
                "model": "gemini-3.8-flash-low",
            },
        }
        self._clients = {
            pid: _E2ELocalCLIClient(
                pid,
                installed=bool(m["installed"]),
                model=str(m["model"]),
                auth_ready=m["auth_ready"],
                registry=self,
            )
            for pid, m in self._meta.items()
        }

    def build_client(
        self,
        provider_id: str,
        *,
        model: Optional[str] = None,
        enabled: bool = True,
        timeout_seconds: Optional[float] = None,
    ) -> Any:
        if not enabled:
            return _E2ELocalCLIClient(provider_id, installed=False, registry=self)
        return self._clients[provider_id]

    def list_detected_clis(
        self,
        run_auth_probe: bool = False,
        *,
        run_version_probe: bool = True,
        force_refresh: bool = False,
    ) -> List[LocalCLIStatus]:
        out: List[LocalCLIStatus] = []
        for pid, m in self._meta.items():
            out.append(
                LocalCLIStatus(
                    provider_id=pid,
                    display_name=str(m["display_name"]),
                    binary_name=str(m["binary_name"]),
                    installed=bool(m["installed"]),
                    auth_ready=m["auth_ready"],
                    binary_path=str(m["binary_path"]),
                    version=str(m["version"]),
                    status_detail=str(m["detail"]),
                    default_model=str(m["model"]),
                    priority=int(m["priority"]),
                    in_cooldown=bool(m["auth_ready"] is False),
                )
            )
        return out


def _make_mock_plaid_transport() -> httpx.MockTransport:
    """Deterministic Plaid Hosted Link MockTransport supporting pending -> check -> sync."""
    state = {"get_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/link/token/create":
            return httpx.Response(
                200,
                json={
                    "link_token": "link-e2e-hosted-900",
                    "hosted_link_url": "https://hosted.plaid.com/link/e2e-hosted-900",
                    "expiration": "2099-01-01T00:00:00Z",
                },
            )
        if path == "/link/token/get":
            state["get_calls"] += 1
            return httpx.Response(
                200,
                json={
                    "link_token": "link-e2e-hosted-900",
                    "link_sessions": [
                        {
                            "finished_at": "2026-11-26T10:15:00Z",
                            "results": {
                                "item_add_results": [
                                    {
                                        "public_token": "public-e2e-chase-1",
                                        "institution": {
                                            "institution_id": "ins_chase",
                                            "name": "Chase Bank",
                                        },
                                        "accounts": [
                                            {
                                                "id": "acct_chase_e2e_1",
                                                "name": "Chase Total Checking",
                                                "mask": "4242",
                                                "type": "depository",
                                                "subtype": "checking",
                                            }
                                        ],
                                    }
                                ]
                            },
                        }
                    ],
                },
            )
        if path == "/item/public_token/exchange":
            return httpx.Response(
                200,
                json={"access_token": "access-e2e-chase-1", "item_id": "item-e2e-chase-1"},
            )
        if path == "/accounts/balance/get":
            return httpx.Response(
                200,
                json={
                    "accounts": [
                        {
                            "account_id": "acct_chase_e2e_1",
                            "name": "Chase Total Checking",
                            "mask": "4242",
                            "type": "depository",
                            "subtype": "checking",
                            "balances": {"current": 4120.50, "available": 4120.50, "iso_currency_code": "USD"},
                        }
                    ]
                },
            )
        if path == "/transactions/sync":
            return httpx.Response(
                200,
                json={
                    "added": [
                        {
                            "transaction_id": "tx_plaid_e2e_1",
                            "account_id": "acct_chase_e2e_1",
                            "amount": 24.75,
                            "date": "2026-11-25",
                            "name": "BLUE BOTTLE COFFEE",
                            "merchant_name": "Blue Bottle Coffee",
                            "pending": False,
                            "personal_finance_category": {
                                "primary": "FOOD_AND_DRINK",
                                "detailed": "FOOD_AND_DRINK_COFFEE",
                            },
                        }
                    ],
                    "modified": [],
                    "removed": [],
                    "next_cursor": "cursor-e2e-done-1",
                    "has_more": False,
                    "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE",
                },
            )
        return httpx.Response(400, json={"error_code": "PRODUCTS_NOT_SUPPORTED", "error_message": "Unsupported"})

    return httpx.MockTransport(handler)


ELECTRON_CONSUMER_OVERHAUL_SCRIPT = r"""
const fs = require("fs");
const path = require("path");
const { app, BrowserWindow } = require("electron");

const [baseUrl, backupPath, reportPath, outDir, screenshotDir] = process.argv.slice(-5);

app.commandLine.appendSwitch("disable-gpu");
app.commandLine.appendSwitch("no-sandbox");

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitFor(wc, expr, timeoutMs = 15000, label = "condition") {
  const deadline = Date.now() + timeoutMs;
  let lastErr = null;
  while (Date.now() < deadline) {
    try {
      const val = await wc.executeJavaScript(expr, true);
      if (val) return val;
    } catch (err) {
      lastErr = err;
    }
    await sleep(100);
  }
  throw new Error(`Timed out waiting for ${label}${lastErr ? ": " + lastErr.message : ""}`);
}

async function captureShot(win, name) {
  if (!screenshotDir || screenshotDir === "NONE") return null;
  fs.mkdirSync(screenshotDir, { recursive: true });
  await win.webContents.executeJavaScript(`
    (() => {
      const tr = document.getElementById("toast-region");
      if (tr) tr.replaceChildren();
      return true;
    })()
  `);
  await sleep(220);
  const img = await win.webContents.capturePage();
  const dest = path.join(screenshotDir, name);
  fs.writeFileSync(dest, img.toPNG());
  return dest;
}

const BANNED_JS = `
  function scanBannedJargon(text) {
    const patterns = [
      ["PBKDF2", /PBKDF2/i],
      ["DEK", /\\bDEK\\b/],
      ["Base32", /Base32/i],
      ["BYOK", /\\bBYOK\\b/i],
      ["Coffee $6.50", /Coffee\\s*\\$6\\.50/i],
      ["Paycheck +$2400", /Paycheck\\s*\\+\\$2400/i],
      ["Simulator", /simulat/i],
      ["What-If", /what-if/i],
      ["Sandbox", /sandbox/i],
    ];
    const hits = [];
    for (const [label, rx] of patterns) {
      if (rx.test(text)) {
        const m = text.match(new RegExp("[^\\n]*" + rx.source + "[^\\n]*", rx.flags.includes("i") ? "gi" : "g")) || [];
        hits.push({ label, samples: m.slice(0, 3) });
      }
    }
    return hits;
  }
`;

async function measureGeometry(win, tabName, vpWidth, vpHeight) {
  return await win.webContents.executeJavaScript(`
    (() => {
      ${BANNED_JS}
      const iw = window.innerWidth;
      const docOverflowX = Math.max(0, document.documentElement.scrollWidth - iw);
      const bodyText = document.body.innerText || "";
      const bannedHits = scanBannedJargon(bodyText);

      // Measure every visible .jar-card and .jar-top
      const jarOverflows = [];
      for (const card of document.querySelectorAll(".jar-card")) {
        const rect = card.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) continue;
        const ox = card.scrollWidth - card.clientWidth;
        if (ox > 1) {
          jarOverflows.push({ sel: ".jar-card", ox, sw: card.scrollWidth, cw: card.clientWidth, text: (card.innerText || "").slice(0, 60) });
        }
        const top = card.querySelector(".jar-top");
        if (top) {
          const topOx = top.scrollWidth - top.clientWidth;
          if (topOx > 1) {
            jarOverflows.push({ sel: ".jar-top", ox: topOx, sw: top.scrollWidth, cw: top.clientWidth, text: (top.innerText || "").slice(0, 60) });
          }
        }
      }

      // Measure .tabbar & Coach/Settings visibility on desktop viewports
      const tabbar = document.getElementById("main-tabbar");
      let tabbarAudit = null;
      if (tabbar) {
        const tbRect = tabbar.getBoundingClientRect();
        const coachBtn = document.getElementById("tab-btn-coach");
        const settingsBtn = document.getElementById("tab-btn-settings");
        const cRect = coachBtn ? coachBtn.getBoundingClientRect() : null;
        const sRect = settingsBtn ? settingsBtn.getBoundingClientRect() : null;
        tabbarAudit = {
          scrollWidth: tabbar.scrollWidth,
          clientWidth: tabbar.clientWidth,
          overflowX: Math.max(0, tabbar.scrollWidth - tabbar.clientWidth),
          coachRightClip: cRect ? Math.max(0, cRect.right - tbRect.right) : 0,
          settingsRightClip: sRect ? Math.max(0, sRect.right - tbRect.right) : 0,
        };
      }

      // Measure Cash Flow empty-state SVG icons & Sankey container overflow
      let cashFlowAudit = null;
      if ("${tabName}" === "analytics") {
        const sankeyWrap = document.getElementById("analytics-sankey-wrap");
        const panel = document.getElementById("tab-analytics");
        const maxIconDim = Array.from(panel ? panel.querySelectorAll("svg.ui-icon") : []).reduce((mx, svg) => {
          const r = svg.getBoundingClientRect();
          return Math.max(mx, r.width, r.height);
        }, 0);
        const sankeyOx = sankeyWrap ? Math.max(0, sankeyWrap.scrollWidth - sankeyWrap.clientWidth) : 0;
        cashFlowAudit = {
          maxIconDim: Math.round(maxIconDim * 10) / 10,
          sankeyOverflowX: sankeyOx,
          hasTruncatedSanke: /Cashflow Sanke(?!y)/i.test(bodyText),
        };
      }

      return {
        tab: "${tabName}",
        viewport: "${vpWidth}x${vpHeight}",
        innerWidth: iw,
        docOverflowX,
        jarOverflows,
        tabbarAudit,
        cashFlowAudit,
        bannedHits,
      };
    })()
  `, true);
}

app.whenReady().then(async () => {
  const consoleErrors = [];
  const server5xx = [];
  const exportResponses = [];
  const screenshots = {};
  const results = {
    emptySweep: [],
    populatedSweep: [],
    screenshots,
  };

  const win = new BrowserWindow({
    width: 1380,
    height: 880,
    useContentSize: true,
    show: true,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      webSecurity: true,
    },
  });

  // Prevent test run from popping open real system browser tabs on Plaid Connect
  win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));

  win.webContents.on("console-message", (_ev, level, message, line, sourceId) => {
    if (level >= 3) {
      consoleErrors.push({ level, message, line, sourceId });
    }
  });

  win.webContents.session.webRequest.onCompleted({ urls: [`${baseUrl}/*`] }, (details) => {
    if (details.statusCode >= 500) {
      server5xx.push({ url: details.url, statusCode: details.statusCode, method: details.method });
    }
    if (details.url.includes("/api/v1/export/")) {
      exportResponses.push({ url: details.url, statusCode: details.statusCode, method: details.method });
    }
  });

  try {
    await win.loadURL(baseUrl);

    await win.webContents.executeJavaScript(`
      window.__e2eErrors = [];
      window.addEventListener("error", (e) => {
        window.__e2eErrors.push("error: " + (e.message || String(e)));
      });
      window.addEventListener("unhandledrejection", (e) => {
        window.__e2eErrors.push("unhandledrejection: " + (e.reason && e.reason.message ? e.reason.message : String(e.reason)));
      });
      true;
    `);

    // ------------------------------------------------------------------
    // 1. Passcode / Vault Setup Gate (1380x880) + Jargon Check + Shot 1
    // ------------------------------------------------------------------
    await waitFor(
      win.webContents,
      `(() => {
        const ov = document.getElementById("vault-overlay");
        const pane = document.getElementById("vault-pane-setup");
        return ov && !ov.classList.contains("hidden") && pane && !pane.classList.contains("hidden");
      })()`,
      12000,
      "vault setup gate"
    );
    await sleep(300);

    results.vaultGateJargon = await win.webContents.executeJavaScript(`
      (() => { ${BANNED_JS} return scanBannedJargon(document.body.innerText || ""); })()
    `);
    const s1 = await captureShot(win, "after_01_passcode_vault_1380x880.png");
    if (s1) screenshots.vault = s1;

    await win.webContents.executeJavaScript(`
      (() => {
        document.getElementById("vault-setup-pwd").value = "CalmIndustrial2026!";
        document.getElementById("vault-setup-confirm").value = "CalmIndustrial2026!";
        document.getElementById("vault-setup-form").requestSubmit();
        return true;
      })()
    `, true);

    await waitFor(
      win.webContents,
      `(() => {
        const vp = document.getElementById("vault-pane-recovery-verify");
        const words = document.querySelectorAll("#vault-recovery-key-box .recovery-word strong");
        const inps = document.querySelectorAll("#vault-verify-inputs input[data-verify-idx]");
        return vp && !vp.classList.contains("hidden") && words.length >= 4 && inps.length === 4;
      })()`,
      12000,
      "recovery verify pane"
    );

    results.recoveryGateJargon = await win.webContents.executeJavaScript(`
      (() => { ${BANNED_JS} return scanBannedJargon(document.body.innerText || ""); })()
    `);

    await win.webContents.executeJavaScript(`
      (() => {
        const words = Array.from(document.querySelectorAll("#vault-recovery-key-box .recovery-word strong"))
          .map((n) => n.textContent.trim());
        for (const inp of document.querySelectorAll("#vault-verify-inputs input[data-verify-idx]")) {
          inp.value = words[Number(inp.dataset.verifyIdx)];
        }
        document.getElementById("vault-recovery-verify-form").requestSubmit();
        return true;
      })()
    `, true);

    await waitFor(
      win.webContents,
      `(() => {
        const ob = document.getElementById("onboarding-overlay");
        return ob && !ob.classList.contains("hidden");
      })()`,
      12000,
      "onboarding modal"
    );

    results.onboardingJargon = await win.webContents.executeJavaScript(`
      (() => { ${BANNED_JS} return scanBannedJargon(document.body.innerText || ""); })()
    `);

    await win.webContents.executeJavaScript(`document.getElementById("btn-onboarding-skip").click(); true;`, true);
    await waitFor(
      win.webContents,
      `(() => {
        const vo = document.getElementById("vault-overlay");
        const ob = document.getElementById("onboarding-overlay");
        return vo.classList.contains("hidden") && ob.classList.contains("hidden") &&
          document.querySelectorAll("#jars-grid .jar-card").length === 5;
      })()`,
      12000,
      "empty $0 dashboard"
    );
    await sleep(350);

    // ------------------------------------------------------------------
    // 2. Empty $0 Profile: Zero-Overflow Geometry (1380x880, 1024x768, 375x812 x 8 tabs)
    // ------------------------------------------------------------------
    const tabs = ["today", "analytics", "activity", "portfolio", "credit", "goals", "coach", "settings"];
    const viewports = [
      [1380, 880],
      [1024, 768],
      [375, 812],
    ];

    for (const [vw, vh] of viewports) {
      win.setContentSize(vw, vh);
      await waitFor(win.webContents, `window.innerWidth === ${vw}`, 6000, `resize ${vw}x${vh}`);
      await sleep(160);
      for (const tab of tabs) {
        await win.webContents.executeJavaScript(`
          window.scrollTo(0, 0);
          document.getElementById("tab-btn-${tab}").click();
          true;
        `, true);
        await sleep(180);
        if (tab === "analytics" && vw === 1380) {
          await sleep(200);
          results.emptyCashFlowKpis = await win.webContents.executeJavaScript(`
            (() => {
              const strip = document.getElementById("analytics-kpi-strip");
              const txt = strip ? strip.innerText : "";
              return {
                text: txt,
                hasNoDataYet: txt.includes("NO DATA YET"),
                hasTightMargin: txt.includes("TIGHT MARGIN"),
                hasZeroCategories: txt.includes("0 CATEGORIES"),
                hasPlusZero: txt.includes("+$0.00"),
                tabbarTop: getComputedStyle(document.getElementById("main-tabbar")).top,
              };
            })()
          `);
          const s2 = await captureShot(win, "after_02_cashflow_empty_1380x880.png");
          if (s2) screenshots.cashflowEmpty = s2;
        }
        const m = await measureGeometry(win, tab, vw, vh);
        results.emptySweep.push(m);
      }
    }

    // ------------------------------------------------------------------
    // 3. Restore Populated Workspace Backup at 1380x880
    // ------------------------------------------------------------------
    win.setContentSize(1380, 880);
    await waitFor(win.webContents, `window.innerWidth === 1380`, 6000, "resize back 1380x880");

    const backupContent = fs.readFileSync(backupPath, "utf-8");
    const importOk = await win.webContents.executeJavaScript(`
      (async () => {
        const auth = await fetch("/api/v1/auth/status", { credentials: "same-origin" }).then(r => r.json());
        const fd = new FormData();
        const blob = new Blob([${JSON.stringify(backupContent)}], { type: "application/json" });
        fd.append("file", blob, "chronox_populated_backup.json");
        const resp = await fetch("/api/v1/backup/import", {
          method: "POST",
          credentials: "same-origin",
          headers: auth.csrf_token ? { "X-ChronoX-CSRF": auth.csrf_token } : {},
          body: fd,
        });
        return { ok: resp.ok, status: resp.status };
      })()
    `, true);
    results.backupImport = importOk;
    if (!importOk || !importOk.ok) {
      throw new Error("Populated backup import failed: " + JSON.stringify(importOk));
    }

    // Reload clean so full UI hydrates with populated profile
    await win.loadURL(baseUrl);
    await win.webContents.executeJavaScript(`
      window.__e2eErrors = [];
      window.addEventListener("error", (e) => window.__e2eErrors.push("error: " + (e.message || String(e))));
      window.addEventListener("unhandledrejection", (e) => window.__e2eErrors.push("unhandledrejection: " + (e.reason && e.reason.message ? e.reason.message : String(e.reason))));
      true;
    `);
    await waitFor(
      win.webContents,
      `(() => {
        const hero = document.getElementById("hero-total-safe");
        return hero && hero.textContent.trim() !== "$0.00" &&
          document.querySelectorAll("#jars-grid .jar-card").length === 5;
      })()`,
      15000,
      "populated overview hydration"
    );
    await sleep(450);

    // ------------------------------------------------------------------
    // 4. Live "Say or Log Anything" Bar (POST /api/v1/ai/say) + Undo
    // ------------------------------------------------------------------
    const sayUtterance = "I spent $14.50 on artisanal coffee because client pitch ran late";
    await win.webContents.executeJavaScript(`
      (() => {
        const inp = document.getElementById("quick-add-input");
        inp.value = ${JSON.stringify(sayUtterance)};
        document.getElementById("quick-add-form").requestSubmit();
        return true;
      })()
    `, true);

    await waitFor(
      win.webContents,
      `(() => {
        const card = document.getElementById("command-say-response");
        return card && !card.classList.contains("hidden") && card.innerText.includes("Undo");
      })()`,
      12000,
      "say response card with Undo"
    );
    await sleep(300);

    // Capture populated Overview with Say-Anything bar response & all 5 zero-overflow Jars!
    // Scroll cleanly to #command-say (honoring scroll-margin-top below top:0 sticky .tabbar!)
    await win.webContents.executeJavaScript(`
      (() => {
        const sec = document.getElementById("command-say");
        if (sec) sec.scrollIntoView({ behavior: "instant", block: "start" });
        return true;
      })()
    `);
    const s3 = await captureShot(win, "after_03_overview_say_jars_1380x880.png");
    if (s3) screenshots.overviewPopulated = s3;
    await win.webContents.executeJavaScript(`window.scrollTo(0, 0); true;`);

    const sayAfter = await win.webContents.executeJavaScript(`
      (async () => {
        const cardText = document.getElementById("command-say-response").innerText;
        const txs = await fetch("/api/v1/transactions?limit=25", { credentials: "same-origin" }).then(r => r.json());
        const match = txs.find(
          (t) => Number(t.amount_cents) === 1450 &&
            String(t.memo || "").toLowerCase().includes("client pitch ran late")
        );
        return { cardText, matchedTx: match || null };
      })()
    `, true);
    results.sayAfter = sayAfter;

    // Click inline Undo button inside #command-say-response
    await win.webContents.executeJavaScript(`
      (() => {
        const btns = Array.from(document.querySelectorAll("#command-say-response button"));
        const undoBtn = btns.find((b) => b.textContent.trim().toLowerCase().includes("undo"));
        if (!undoBtn) throw new Error("Undo button not found in #command-say-response");
        undoBtn.click();
        return true;
      })()
    `, true);

    await waitFor(
      win.webContents,
      `document.getElementById("command-say-response").classList.contains("hidden")`,
      10000,
      "say response card hidden after undo"
    );
    await sleep(250);

    const sayAfterUndo = await win.webContents.executeJavaScript(`
      (async () => {
        const txs = await fetch("/api/v1/transactions?limit=25", { credentials: "same-origin" }).then(r => r.json());
        const match = txs.find(
          (t) => Number(t.amount_cents) === 1450 &&
            String(t.memo || "").toLowerCase().includes("client pitch ran late")
        );
        return { remainingMatch: match || null };
      })()
    `, true);
    results.sayAfterUndo = sayAfterUndo;

    // ------------------------------------------------------------------
    // 5. Live "One-Tap Optimize" Modal (POST /api/v1/optimize/one-tap) + Apply + Undo
    // ------------------------------------------------------------------
    await win.webContents.executeJavaScript(`document.getElementById("btn-overview-onetap").click(); true;`, true);
    await waitFor(
      win.webContents,
      `(() => {
        const ov = document.getElementById("onetap-overlay");
        const body = document.getElementById("onetap-body");
        return ov && !ov.classList.contains("hidden") &&
          body && body.querySelectorAll(".onetap-action-card").length >= 1 &&
          body.innerText.includes("CVaR");
      })()`,
      15000,
      "One-Tap Optimize modal preview"
    );
    await sleep(300);

    const s4 = await captureShot(win, "after_04_onetap_modal_1380x880.png");
    if (s4) screenshots.oneTapModal = s4;

    results.oneTapPreview = await win.webContents.executeJavaScript(`
      (() => {
        const bodyText = document.getElementById("onetap-body").innerText;
        const footText = document.getElementById("onetap-footer").innerText;
        const actionCount = document.querySelectorAll("#onetap-body .onetap-action-card").length;
        return {
          hasCvar: bodyText.includes("CVaR"),
          hasSurvivalPct: /(?:>99\.9%|\d+\.\d+%)/.test(bodyText),
          hasCappedSurvival: bodyText.includes(">99.9%"),
          hasAiRationale: bodyText.includes("AI-verified cushion step") && bodyText.includes("AI-verified guardrail"),
          actionCount,
          footText,
        };
      })()
    `);

    // Click "Apply One-Tap Plan"
    await win.webContents.executeJavaScript(`
      (() => {
        const btn = Array.from(document.querySelectorAll("#onetap-footer button"))
          .find((b) => b.textContent.includes("Apply One-Tap Plan"));
        if (!btn) throw new Error("Apply One-Tap Plan button not found");
        btn.click();
        return true;
      })()
    `, true);

    await waitFor(
      win.webContents,
      `(() => {
        const body = document.getElementById("onetap-body");
        const foot = document.getElementById("onetap-footer");
        return body && body.innerText.includes("Plan Applied") && foot && foot.innerText.includes("Undo One-Tap");
      })()`,
      12000,
      "One-Tap applied state with Undo button"
    );
    results.oneTapApplied = true;

    // Click "Undo One-Tap"
    await win.webContents.executeJavaScript(`
      (() => {
        const btn = Array.from(document.querySelectorAll("#onetap-footer button"))
          .find((b) => b.textContent.includes("Undo One-Tap"));
        if (!btn) throw new Error("Undo One-Tap button not found");
        btn.click();
        return true;
      })()
    `, true);

    await waitFor(
      win.webContents,
      `(() => {
        const foot = document.getElementById("onetap-footer");
        return foot && foot.innerText.includes("Apply One-Tap Plan");
      })()`,
      12000,
      "One-Tap undone back to Apply state"
    );
    results.oneTapUndone = true;

    await win.webContents.executeJavaScript(`document.getElementById("btn-onetap-close").click(); true;`, true);
    await waitFor(
      win.webContents,
      `document.getElementById("onetap-overlay").classList.contains("hidden")`,
      5000,
      "One-Tap modal closed"
    );

    // ------------------------------------------------------------------
    // 6. Populated Profile: Zero-Overflow Geometry (1380x880, 1024x768, 375x812 x 8 tabs)
    // ------------------------------------------------------------------
    for (const [vw, vh] of viewports) {
      win.setContentSize(vw, vh);
      await waitFor(win.webContents, `window.innerWidth === ${vw}`, 6000, `populated resize ${vw}x${vh}`);
      await sleep(160);
      for (const tab of tabs) {
        await win.webContents.executeJavaScript(`
          window.scrollTo(0, 0);
          document.getElementById("tab-btn-${tab}").click();
          true;
        `, true);
        await sleep(220);
        if (tab === "analytics" && vw === 1380) {
          await waitFor(
            win.webContents,
            `Boolean(document.querySelector("#analytics-sankey-wrap svg"))`,
            10000,
            "populated cashflow sankey svg"
          );
          await sleep(220);
          const s5 = await captureShot(win, "after_05_cashflow_populated_1380x880.png");
          if (s5) screenshots.cashflowPopulated = s5;
        }
        const m = await measureGeometry(win, tab, vw, vh);
        results.populatedSweep.push(m);
      }
    }

    // ------------------------------------------------------------------
    // 7. Live Settings: Local AI CLI Status + Plaid 1-Step Connect + Exports (1380x880)
    // ------------------------------------------------------------------
    win.setContentSize(1380, 880);
    await waitFor(win.webContents, `window.innerWidth === 1380`, 6000, "settings resize 1380x880");
    await win.webContents.executeJavaScript(`
      window.scrollTo(0, 0);
      document.getElementById("tab-btn-settings").click();
      true;
    `, true);
    await sleep(400);

    await waitFor(
      win.webContents,
      `(() => {
        const cards = document.querySelectorAll("#local-cli-cards-grid .local-cli-card");
        const st = document.getElementById("local-cli-status-text");
        return cards.length === 4 && st && st.textContent.includes("OpenCode");
      })()`,
      12000,
      "Settings detected local AI CLI cards"
    );
    await sleep(200);

    // Click Test button on OpenCode CLI card
    await win.webContents.executeJavaScript(`
      (() => {
        const cards = Array.from(document.querySelectorAll("#local-cli-cards-grid .local-cli-card"));
        const ocCard = cards.find((c) => c.innerText.includes("OpenCode"));
        if (!ocCard) throw new Error("OpenCode local CLI card missing");
        const testBtn = Array.from(ocCard.querySelectorAll("button")).find((b) => b.textContent.trim() === "Test");
        if (!testBtn) throw new Error("Test button missing on OpenCode card");
        testBtn.click();
        return true;
      })()
    `, true);

    await waitFor(
      win.webContents,
      `(() => {
        const cards = Array.from(document.querySelectorAll("#local-cli-cards-grid .local-cli-card"));
        const ocCard = cards.find((c) => c.innerText.includes("OpenCode"));
        return ocCard && ocCard.innerText.toLowerCase().includes("authenticated and ready");
      })()`,
      10000,
      "OpenCode CLI Test result"
    );

    results.localCliUi = await win.webContents.executeJavaScript(`
      (() => {
        const cards = Array.from(document.querySelectorAll("#local-cli-cards-grid .local-cli-card"));
        const claudeCard = cards.find((c) => c.innerText.includes("Claude"));
        const claudeUse = claudeCard ? claudeCard.querySelector("button") : null;
        const codexCard = cards.find((c) => c.innerText.includes("Codex"));
        const codexUse = codexCard ? codexCard.querySelector("button") : null;
        const agyCard = cards.find((c) => c.innerText.includes("Antigravity"));
        const agyBadge = agyCard ? agyCard.querySelector(".pill") : null;
        return {
          statusText: document.getElementById("local-cli-status-text").textContent.trim(),
          cardCount: cards.length,
          autoDetectChecked: Boolean(document.getElementById("set-local-cli-auto-detect").checked),
          opencodeActive: cards.some((c) => c.classList.contains("is-active") && c.innerText.includes("OpenCode")),
          claudeUseMuted: Boolean(claudeUse && claudeUse.disabled && !claudeUse.classList.contains("btn-primary")),
          codexUseDisabled: Boolean(codexUse && codexUse.disabled && codexUse.textContent.includes("Sign in")),
          agyBadgeText: agyBadge ? agyBadge.textContent.trim() : "",
        };
      })()
    `);

    // Exercise 1-step Plaid Connect flow in Settings
    const plaidPillBefore = await win.webContents.executeJavaScript(
      `document.getElementById("plaid-status-pill").textContent.trim()`
    );
    await win.webContents.executeJavaScript(`
      (() => {
        document.getElementById("set-plaid-client-id").value = "cid_e2e_1234";
        document.getElementById("set-plaid-secret").value = "sec_e2e_5678";
        document.getElementById("btn-plaid-connect").click();
        return true;
      })()
    `, true);

    await waitFor(
      win.webContents,
      `(() => {
        const st = document.getElementById("plaid-link-status");
        return st && !st.classList.contains("hidden") && st.innerText.includes("Check & Sync Now");
      })()`,
      12000,
      "Plaid Hosted Link pending status with Check & Sync Now"
    );

    // Click "Check & Sync Now" -> triggers POST /api/v1/plaid/hosted-link/check -> exchanges & syncs
    await win.webContents.executeJavaScript(`
      (() => {
        const btn = Array.from(document.querySelectorAll("#plaid-link-status button"))
          .find((b) => b.textContent.includes("Check & Sync Now"));
        if (!btn) throw new Error("Check & Sync Now button missing");
        btn.click();
        return true;
      })()
    `, true);

    await waitFor(
      win.webContents,
      `(() => {
        const pill = document.getElementById("plaid-status-pill");
        const list = document.getElementById("connections-list");
        return pill && pill.textContent.trim() === "Connected (1)" &&
          list && list.innerText.includes("Chase Bank");
      })()`,
      12000,
      "Plaid status pill Connected (1) and Chase Bank connection row"
    );

    results.plaidUi = await win.webContents.executeJavaScript(`
      ({
        pillBefore: ${JSON.stringify(plaidPillBefore)},
        pillAfter: document.getElementById("plaid-status-pill").textContent.trim(),
        progressText: document.getElementById("plaid-link-status").innerText.trim(),
        connectionsText: document.getElementById("connections-list").innerText.trim(),
      })
    `);

    // Capture Settings screenshots: top (Connect Bank + Auto-Detected Local AI) & Export Studio
    await win.webContents.executeJavaScript(`
      (() => {
        const card = document.getElementById("connectors-card");
        if (card) card.scrollIntoView({ behavior: "instant", block: "start" });
        return true;
      })()
    `);
    await sleep(220);
    const s6 = await captureShot(win, "after_06_settings_plaid_ai_1380x880.png");
    if (s6) screenshots.settingsPlaidAi = s6;

    await win.webContents.executeJavaScript(`
      (() => {
        const btn = document.getElementById("btn-settings-export-numbers");
        if (btn) btn.scrollIntoView({ behavior: "instant", block: "center" });
        return true;
      })()
    `);
    await sleep(220);
    const s7 = await captureShot(win, "after_07_settings_exports_1380x880.png");
    if (s7) screenshots.settingsExports = s7;

    // Trigger live UI export buttons (Apple Numbers .xlsx & Full CSV Pack .zip)
    await win.webContents.executeJavaScript(`
      document.getElementById("btn-settings-export-numbers").click();
      true;
    `, true);
    await waitFor(
      win.webContents,
      `true`,
      1000,
      "numbers click"
    );
    await sleep(500);

    await win.webContents.executeJavaScript(`
      document.getElementById("btn-settings-export-csv-pack").click();
      true;
    `, true);
    await sleep(500);

    // Fetch exact binary artifacts using live authenticated browser session cookies
    const exportArtifacts = await win.webContents.executeJavaScript(`
      (async () => {
        async function fetchB64(url) {
          const r = await fetch(url, { credentials: "same-origin" });
          const buf = await r.arrayBuffer();
          const bytes = new Uint8Array(buf);
          let bin = "";
          for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
          return {
            status: r.status,
            contentType: r.headers.get("content-type") || "",
            disposition: r.headers.get("content-disposition") || "",
            size: bytes.length,
            b64: btoa(bin),
          };
        }
        const numbers = await fetchB64("/api/v1/export/numbers?days=365&native=false");
        const csvPack = await fetchB64("/api/v1/export/csv?pack=true");
        return { numbers, csvPack };
      })()
    `, true);

    const xlsxPath = path.join(outDir, "live_export.xlsx");
    const zipPath = path.join(outDir, "live_export_pack.zip");
    fs.writeFileSync(xlsxPath, Buffer.from(exportArtifacts.numbers.b64, "base64"));
    fs.writeFileSync(zipPath, Buffer.from(exportArtifacts.csvPack.b64, "base64"));

    results.exports = {
      uiResponses: exportResponses,
      numbersMeta: {
        status: exportArtifacts.numbers.status,
        contentType: exportArtifacts.numbers.contentType,
        disposition: exportArtifacts.numbers.disposition,
        size: exportArtifacts.numbers.size,
        path: xlsxPath,
      },
      csvPackMeta: {
        status: exportArtifacts.csvPack.status,
        contentType: exportArtifacts.csvPack.contentType,
        disposition: exportArtifacts.csvPack.disposition,
        size: exportArtifacts.csvPack.size,
        path: zipPath,
      },
    };

    results.pageErrors = await win.webContents.executeJavaScript(`window.__e2eErrors || []`);
    results.consoleErrors = consoleErrors;
    results.server5xx = server5xx;
    results.ok = true;
  } catch (err) {
    results.ok = false;
    results.fatalError = err && err.stack ? err.stack : String(err);
    results.consoleErrors = consoleErrors;
    results.server5xx = server5xx;
  } finally {
    fs.writeFileSync(reportPath, JSON.stringify(results, null, 2), "utf-8");
    app.quit();
  }
});
"""


@pytest.mark.skipif(
    not XVFB_BIN or not ELECTRON_BIN.exists(),
    reason="xvfb-run or local Electron binary not installed",
)
def test_consumer_overhaul_full_stack_zero_overflow_say_onetap_exports_and_ai_plaid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for env_key in (
        "OPENCODE_GO_API_KEY",
        "OPENCODE_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "CHRONOX_WRAPPER_API_KEY",
    ):
        monkeypatch.delenv(env_key, raising=False)
    monkeypatch.setenv("CHRONOX_LOCAL_CLI_AUTO_DETECT", "1")

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    db_path = tmp_path / "consumer_e2e.sqlite3"
    secrets_path = tmp_path / "secrets.enc"
    backup_path = _build_populated_backup_json(tmp_path)
    report_path = tmp_path / "consumer_e2e_report.json"
    out_dir = tmp_path / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    runner_script = tmp_path / "electron_consumer_e2e.cjs"
    runner_script.write_text(ELECTRON_CONSUMER_OVERHAUL_SCRIPT, encoding="utf-8")

    screenshot_dir = os.environ.get("CHRONOX_E2E_SCREENSHOT_DIR", "").strip() or "NONE"

    svc = FinanceService(
        db_path=str(db_path),
        secret_store_path=secrets_path,
        plaid_transport=_make_mock_plaid_transport(),
        local_cli_registry=_E2ELocalCLIRegistry(),
        plaid_poll_interval_seconds=60.0,
    )
    app = create_app(db_path=str(db_path), service=svc, enforce_vault_lock=True, allowed_port=port)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    try:
        deadline = time.monotonic() + 10.0
        ready = False
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{base_url}/api/health", timeout=1.0).status_code == 200:
                    ready = True
                    break
            except httpx.HTTPError:
                ready = False
            time.sleep(0.1)
        assert ready, "Live uvicorn server failed to start"

        proc = subprocess.run(  # noqa: S603
            [
                XVFB_BIN,
                "-a",
                "-s",
                "-screen 0 1600x1000x24",
                str(ELECTRON_BIN),
                "--no-sandbox",
                "--disable-gpu",
                str(runner_script),
                base_url,
                str(backup_path),
                str(report_path),
                str(out_dir),
                screenshot_dir,
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert report_path.exists(), (
            f"Electron runner exited ({proc.returncode}) without report:\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
        rep = json.loads(report_path.read_text(encoding="utf-8"))
        assert rep.get("ok") is True, (
            f"Consumer overhaul browser E2E failed:\n{json.dumps(rep, indent=2)}\nSTDERR:\n{proc.stderr}"
        )

        # 1. Zero runtime errors
        assert rep["consoleErrors"] == [], f"Console errors: {rep['consoleErrors']}"
        assert rep["pageErrors"] == [], f"Page errors: {rep['pageErrors']}"
        assert rep["server5xx"] == [], f"HTTP 5xx errors: {rep['server5xx']}"

        # 2. Zero developer-jargon strings on Vault Setup, Recovery Verify, and Onboarding
        assert rep["vaultGateJargon"] == [], f"Vault setup jargon: {rep['vaultGateJargon']}"
        assert rep["recoveryGateJargon"] == [], f"Recovery verify jargon: {rep['recoveryGateJargon']}"
        assert rep["onboardingJargon"] == [], f"Onboarding jargon: {rep['onboardingJargon']}"

        # 3. Zero-overflow pixel geometry & zero banned jargon across BOTH empty ($0) and populated
        #    profiles across all 3 viewports (1380x880, 1024x768, 375x812) and all 8 tabs (48 views!)
        for label, sweep in (("empty", rep["emptySweep"]), ("populated", rep["populatedSweep"])):
            assert len(sweep) == 24, f"Expected 24 viewport x tab checks for {label}, got {len(sweep)}"
            for item in sweep:
                ctx = f"{label}:{item['viewport']}:{item['tab']}"
                assert item["docOverflowX"] <= 1, f"Horizontal document overflow on {ctx}: {item['docOverflowX']}px"
                assert item["jarOverflows"] == [], f"Jar card overflow on {ctx}: {item['jarOverflows']}"
                assert item["bannedHits"] == [], f"Banned jargon rendered on {ctx}: {item['bannedHits']}"
                if item["viewport"] in ("1380x880", "1024x768") and item["tabbarAudit"]:
                    tb = item["tabbarAudit"]
                    assert tb["overflowX"] <= 1, f"Tabbar overflow on {ctx}: {tb}"
                    assert tb["coachRightClip"] <= 1, f"Coach tab clipped on {ctx}: {tb}"
                    assert tb["settingsRightClip"] <= 1, f"Settings tab clipped on {ctx}: {tb}"
                if item["tab"] == "analytics" and item["cashFlowAudit"]:
                    cf = item["cashFlowAudit"]
                    assert cf["maxIconDim"] <= 48, f"Oversized icon in Cash Flow on {ctx}: {cf}"
                    assert cf["sankeyOverflowX"] <= 1, f"Sankey container overflow on {ctx}: {cf}"
                    assert cf["hasTruncatedSanke"] is False, f"Clipped 'Cashflow Sanke' text on {ctx}"

        # 4. Live "Say or Log Anything" Bar -> $14.50 + causal reason -> Undo
        assert "\nnull\n" not in rep["sayAfter"]["cardText"], f"Literal null rendered in say card: {rep['sayAfter']}"
        matched_tx = rep["sayAfter"]["matchedTx"]
        assert matched_tx is not None, f"Say-bar did not create $14.50 tx with causal reason: {rep['sayAfter']}"
        assert int(matched_tx["amount_cents"]) == 1450
        assert "client pitch ran late" in str(matched_tx["memo"]).lower()
        assert rep["sayAfterUndo"]["remainingMatch"] is None, "Undo did not remove the $14.50 transaction!"

        # 5. Live "One-Tap Optimize" Modal -> CVaR, >99.9% survival, distinct AI rationales -> Apply -> Undo
        ot = rep["oneTapPreview"]
        assert ot["hasCvar"] is True, f"CVaR cushion missing from One-Tap modal: {ot}"
        assert ot["hasSurvivalPct"] is True, f"Survival % missing from One-Tap modal: {ot}"
        assert ot["hasCappedSurvival"] is True, f"100.0% survival should render as >99.9%: {ot}"
        assert ot["hasAiRationale"] is True, f"AI action rationales did not render in One-Tap modal: {ot}"
        assert "of 6 actions apply" in ot["footText"], f"Expected advisory breakdown in footer: {ot}"
        assert ot["actionCount"] >= 2
        assert rep["oneTapApplied"] is True
        assert rep["oneTapUndone"] is True

        # 5b. Empty Cash Flow KPI polish (#21) & solid top:0 sticky tabbar (#24)
        cf_kpis = rep["emptyCashFlowKpis"]
        assert cf_kpis["hasNoDataYet"] is True, f"Expected NO DATA YET on $0 Cash Flow: {cf_kpis}"
        assert cf_kpis["hasTightMargin"] is False, f"TIGHT MARGIN should not show on $0 Cash Flow: {cf_kpis}"
        assert cf_kpis["hasZeroCategories"] is False, f"0 CATEGORIES should be hidden on $0 Cash Flow: {cf_kpis}"
        assert cf_kpis["hasPlusZero"] is False, f"Empty surplus should render $0.00 without +: {cf_kpis}"
        assert cf_kpis["tabbarTop"] == "0px", f"Sticky .tabbar should sit at top:0px: {cf_kpis}"

        # 6. Live Automagic Local AI CLI Status & Plaid 1-Step Connect UI
        cli_ui = rep["localCliUi"]
        assert cli_ui["cardCount"] == 4
        assert cli_ui["autoDetectChecked"] is True
        assert cli_ui["opencodeActive"] is True
        assert cli_ui["claudeUseMuted"] is True, f"Uninstalled CLI Use button should be muted non-primary: {cli_ui}"
        assert cli_ui["codexUseDisabled"] is True, f"Sign-in-needed CLI should disable Use button: {cli_ui}"
        assert cli_ui["agyBadgeText"] == "Installed", f"Unprobed agy should show Installed until auth_ready: {cli_ui}"
        assert "OpenCode" in cli_ui["statusText"]

        plaid_ui = rep["plaidUi"]
        assert plaid_ui["pillBefore"] == "Not Connected"
        assert plaid_ui["pillAfter"] == "Connected (1)"
        assert "1 transaction imported" in plaid_ui["progressText"], f"Singular pluralization expected: {plaid_ui}"
        assert "Chase Bank" in plaid_ui["connectionsText"]

        # 7. Live Export Endpoints: UI requests + .xlsx (6 sheets, 5 charts) + .zip (7 CSVs)
        ui_urls = [r["url"] for r in rep["exports"]["uiResponses"] if r["statusCode"] == 200]
        assert any("/api/v1/export/numbers" in u for u in ui_urls), f"UI Numbers export missed: {rep['exports']}"
        assert any("/api/v1/export/csv?pack=true" in u for u in ui_urls), f"UI CSV pack export missed: {rep['exports']}"

        xlsx_meta = rep["exports"]["numbersMeta"]
        assert xlsx_meta["status"] == 200
        assert xlsx_meta["size"] > 8000
        with zipfile.ZipFile(xlsx_meta["path"], "r") as zf:
            names = zf.namelist()
            sheet_xmls = [n for n in names if re.match(r"^xl/worksheets/sheet\d+\.xml$", n)]
            chart_xmls = [n for n in names if re.match(r"^xl/charts/chart\d+\.xml$", n)]
            assert len(sheet_xmls) == 6, f"Expected 6 sheets in .xlsx, found: {sheet_xmls}"
            assert len(chart_xmls) == 5, f"Expected 5 charts in .xlsx, found: {chart_xmls}"
            for cn in chart_xmls:
                xml_body = zf.read(cn).decode("utf-8", errors="ignore")
                assert "<c:chartSpace" in xml_body, f"Chart {cn} missing <c:chartSpace>"

        zip_meta = rep["exports"]["csvPackMeta"]
        assert zip_meta["status"] == 200
        assert zip_meta["size"] > 1000
        with zipfile.ZipFile(zip_meta["path"], "r") as zf:
            csv_files = [n for n in zf.namelist() if n.endswith(".csv")]
            assert len(csv_files) == 7, f"Expected 7 CSVs in pack .zip, found: {csv_files}"
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)
        svc.close()
