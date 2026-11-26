"""End-to-end real Chromium/Electron browser rendering tests under Xvfb.

Verifies in an actual headless Xvfb + Electron BrowserWindow:
1. Zero console errors, zero uncaught page errors, and zero HTTP 5xx responses.
2. Full Vault Setup + 4-group Recovery Key challenge verification + Onboarding skip.
3. Fresh $0 profile rendering ($0.00 Safe-to-Spend, $0.00 across all 5 Jars, $0.00 90-day Forecast ending/nadir).
4. Non-destructive JSON Backup Preview (counts + transaction date range) -> Confirm & Replace Ledger restore flow.
5. Desktop (1440x900) SVG Sankey vs Mobile (375x812) `.sankey-mobile-stack` responsive visibility, layout bounds & >=12px font readability.
6. Zero `simulator`, `pts`/`pt`, `sandbox`, or `What-If` strings in rendered `innerText` across all 8 tabs (including populated Credit Factor Analysis, VantageScore switcher, Debt Payoff Strategy Comparison, and Credit Score APIs).
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from chronox_finance.api import create_app
from chronox_finance.models import (
    CreditInquiryCreate,
    CreditLineCreate,
    CreditReportCreate,
    GoalCreate,
    HoldingCreate,
)
from chronox_finance.service import FinanceService

REPO_ROOT = Path(__file__).resolve().parents[2]
ELECTRON_BIN = REPO_ROOT / "desktop" / "node_modules" / ".bin" / "electron"
XVFB_BIN = shutil.which("xvfb-run")


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _build_seed_backup_json(tmp_path: Path) -> Path:
    seed_dir = tmp_path / "seed_src"
    seed_dir.mkdir(parents=True, exist_ok=True)
    svc = FinanceService(db_path=str(seed_dir / "seed.sqlite3"), secret_store_path=seed_dir / "secrets.enc")
    try:
        svc.secrets.unlock_ephemeral_for_testing()
        chk = svc.storage.create_account(name="Primary Checking", account_type="checking", balance_cents=325_000)
        sav = svc.storage.create_account(name="High-Yield Savings", account_type="savings", balance_cents=500_000)
        jars = {j["slug"]: int(j["id"]) for j in svc.storage.list_jars()}

        svc.add_transaction_raw(
            account_id=int(chk["id"]),
            tx_type="income",
            amount_cents=280_000,
            merchant="ACME PAYROLL DIRECT DEP",
            tx_date="2026-10-15",
            source="manual",
            adjust_balance=False,
            run_auto_sync=False,
        )
        svc.add_transaction_raw(
            account_id=int(chk["id"]),
            tx_type="income",
            amount_cents=280_000,
            merchant="ACME PAYROLL DIRECT DEP",
            tx_date="2026-11-01",
            source="manual",
            adjust_balance=False,
            run_auto_sync=False,
        )
        svc.add_transaction_raw(
            account_id=int(chk["id"]),
            tx_type="expense",
            amount_cents=85_000,
            merchant="METRO APARTMENTS RENT",
            jar_id=jars["bills"],
            tx_date="2026-11-03",
            source="manual",
            adjust_balance=False,
            run_auto_sync=False,
        )
        svc.add_transaction_raw(
            account_id=int(chk["id"]),
            tx_type="expense",
            amount_cents=18_500,
            merchant="TRADER JOES GROCERY",
            jar_id=jars["daily"],
            tx_date="2026-11-18",
            source="manual",
            adjust_balance=False,
            run_auto_sync=False,
        )

        svc.storage.upsert_recurring_bill(
            name="Fiber Internet",
            normalized_merchant="fiber internet",
            amount_cents=7_500,
            cadence="monthly",
            next_due_date="2026-12-05",
            jar_id=jars["bills"],
            is_subscription=True,
            is_essential=True,
        )
        svc.create_goal(GoalCreate(name="OLED Monitor", target_cents=90_000, saved_cents=20_000, category="gadget"))
        svc.upsert_holding(
            HoldingCreate(
                symbol="AAPL",
                name="Apple Inc.",
                asset_class="equity",
                quantity_e8=1_000_000_000,
                cost_basis_cents=180_000,
                price_e8=22_500_000_000,
                account_id=int(sav["id"]),
            )
        )
        svc.create_credit_line(
            CreditLineCreate(
                name="Chase Sapphire Preferred",
                issuer="Chase",
                kind="credit_card",
                balance_cents=45_000,
                credit_limit_cents=1_000_000,
                apr_bps=2149,
                min_payment_cents=3_500,
                statement_closing_day=18,
                payment_due_day=13,
                opened_date="2020-04-01",
                has_grace_period=True,
                autopay_mode="statement",
            )
        )
        svc.create_credit_line(
            CreditLineCreate(
                name="Amex Everyday",
                issuer="American Express",
                kind="credit_card",
                balance_cents=140_000,
                credit_limit_cents=400_000,
                apr_bps=2599,
                min_payment_cents=4_500,
                statement_closing_day=10,
                payment_due_day=5,
                opened_date="2025-05-15",
                has_grace_period=False,
                autopay_mode="minimum",
            )
        )
        svc.create_credit_report(
            CreditReportCreate(
                bureau="experian",
                model="fico8",
                score=758,
                pulled_date="2026-11-15",
                source="manual",
            )
        )
        svc.create_credit_inquiry(
            CreditInquiryCreate(lender="American Express", bureau="experian", inquiry_date="2025-05-15")
        )
        backup_payload = svc.storage.export_backup()
        out_path = tmp_path / "chronox_seed_backup.json"
        out_path.write_text(json.dumps(backup_payload), encoding="utf-8")
        return out_path
    finally:
        svc.close()


ELECTRON_E2E_SCRIPT = r"""
const fs = require("fs");
const { app, BrowserWindow } = require("electron");

const [baseUrl, backupPath, reportPath] = process.argv.slice(-3);

app.commandLine.appendSwitch("disable-gpu");
app.commandLine.appendSwitch("no-sandbox");

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitFor(wc, expr, timeoutMs = 12000, label = "condition") {
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

app.whenReady().then(async () => {
  const consoleErrors = [];
  const server5xx = [];
  const results = {};

  const win = new BrowserWindow({
    width: 1440,
    height: 900,
    useContentSize: true,
    show: true,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      webSecurity: true,
    },
  });

  win.webContents.on("console-message", (_ev, level, message, line, sourceId) => {
    if (level >= 3) {
      consoleErrors.push({ level, message, line, sourceId });
    }
  });

  win.webContents.session.webRequest.onCompleted({ urls: [`${baseUrl}/*`] }, (details) => {
    if (details.statusCode >= 500) {
      server5xx.push({ url: details.url, statusCode: details.statusCode, method: details.method });
    }
  });

  try {
    await win.loadURL(baseUrl);

    // Install page error & unhandledrejection listeners inside renderer
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

    // 1. Verify desktop viewport & Vault Setup Gate
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

    results.desktopViewport = await win.webContents.executeJavaScript(
      `({ w: window.innerWidth, h: window.innerHeight })`
    );

    // Submit Master Password setup
    await win.webContents.executeJavaScript(`
      (() => {
        document.getElementById("vault-setup-pwd").value = "CorrectHorseBattery99!";
        document.getElementById("vault-setup-confirm").value = "CorrectHorseBattery99!";
        document.getElementById("vault-setup-form").requestSubmit();
        return true;
      })()
    `, true);

    // Wait for 4-word Recovery Key challenge pane and complete verification
    await waitFor(
      win.webContents,
      `(() => {
        const vp = document.getElementById("vault-pane-recovery-verify");
        const words = document.querySelectorAll("#vault-recovery-key-box .recovery-word strong");
        const inps = document.querySelectorAll("#vault-verify-inputs input[data-verify-idx]");
        return vp && !vp.classList.contains("hidden") && words.length >= 4 && inps.length === 4;
      })()`,
      12000,
      "recovery key verify pane"
    );

    await win.webContents.executeJavaScript(`
      (() => {
        const words = Array.from(document.querySelectorAll("#vault-recovery-key-box .recovery-word strong"))
          .map((el) => el.textContent.trim());
        const inps = Array.from(document.querySelectorAll("#vault-verify-inputs input[data-verify-idx]"));
        for (const inp of inps) {
          const idx = Number(inp.dataset.verifyIdx);
          inp.value = words[idx];
        }
        document.getElementById("vault-recovery-verify-form").requestSubmit();
        return true;
      })()
    `, true);

    // Dismiss onboarding modal
    await waitFor(
      win.webContents,
      `(() => {
        const ob = document.getElementById("onboarding-overlay");
        return ob && !ob.classList.contains("hidden");
      })()`,
      12000,
      "onboarding modal"
    );

    await win.webContents.executeJavaScript(`
      document.getElementById("btn-onboarding-skip").click();
      true;
    `, true);

    await waitFor(
      win.webContents,
      `(() => {
        const vo = document.getElementById("vault-overlay");
        const ob = document.getElementById("onboarding-overlay");
        const jars = document.querySelectorAll("#jars-grid .jar-card");
        return vo.classList.contains("hidden") && ob.classList.contains("hidden") && jars.length === 5;
      })()`,
      12000,
      "fresh command dashboard"
    );
    await sleep(450); // allow number animations to settle

    // 2. Verify $0 Fresh Profile on Command + Analytics Forecast
    results.freshCommand = await win.webContents.executeJavaScript(`
      (() => {
        const jarTexts = Array.from(document.querySelectorAll("#jars-grid .jar-card")).map((c) => c.innerText);
        const cardStyle = getComputedStyle(document.querySelector(".card"));
        return {
          dailySafe: document.getElementById("hero-daily-safe").textContent.trim(),
          totalSafe: document.getElementById("hero-total-safe").textContent.trim(),
          safeInvest: document.getElementById("hero-safe-invest").textContent.trim(),
          jarCount: jarTexts.length,
          jarTexts,
          cardBorderRadius: cardStyle.borderRadius,
        };
      })()
    `);

    await win.webContents.executeJavaScript(`document.getElementById("tab-btn-analytics").click(); true;`, true);
    await waitFor(
      win.webContents,
      `Boolean(document.querySelector("#timeline-svg-wrap svg"))`,
      12000,
      "fresh analytics timeline svg"
    );
    await sleep(350);

    results.freshForecast = await win.webContents.executeJavaScript(`
      ({
        ending: document.getElementById("timeline-ending-readout").textContent.trim(),
        nadir: document.getElementById("timeline-nadir-readout").textContent.trim(),
        delta: document.getElementById("timeline-delta-readout").textContent.trim(),
      })
    `);

    // Also verify fresh $0 Credit & Debt tab before backup restore
    await win.webContents.executeJavaScript(`document.getElementById("tab-btn-credit").click(); true;`, true);
    await waitFor(
      win.webContents,
      `document.querySelectorAll("#credit-factors-wrap .factor-row").length === 5`,
      10000,
      "fresh credit factor breakdown"
    );
    await sleep(300);
    results.freshCredit = await win.webContents.executeJavaScript(`
      ({
        score: document.getElementById("cred-bureau-score").textContent.trim(),
        totalDebt: document.getElementById("cred-total-debt").textContent.trim(),
        factorCount: document.querySelectorAll("#credit-factors-wrap .factor-row").length,
      })
    `);

    // Verify fresh $0 Goals Optimizer renders $0.00 monthly flexible surplus (never fabricated $150.00!)
    await win.webContents.executeJavaScript(`document.getElementById("tab-btn-goals").click(); true;`, true);
    await sleep(200);
    await win.webContents.executeJavaScript(`
      (() => {
        document.getElementById("gp-name").value = "Camera Lens";
        document.getElementById("gp-price").value = "600";
        document.getElementById("gp-saved").value = "0";
        document.getElementById("goal-plan-form").requestSubmit();
        return true;
      })()
    `, true);
    await waitFor(
      win.webContents,
      `!document.getElementById("goal-plan-output").classList.contains("hidden")`,
      10000,
      "fresh $0 goal plan output"
    );
    results.freshGoalPlanText = await win.webContents.executeJavaScript(
      `document.getElementById("goal-plan-output").innerText`
    );

    // 3. Backup Preview -> Confirm & Replace Ledger flow
    await win.webContents.executeJavaScript(`document.getElementById("tab-btn-settings").click(); true;`, true);
    await sleep(250);

    const backupJsonStr = fs.readFileSync(backupPath, "utf-8");
    await win.webContents.executeJavaScript(`
      (() => {
        const raw = ${JSON.stringify(backupJsonStr)};
        const file = new File([raw], "chronox_seed_backup.json", { type: "application/json" });
        const dt = new DataTransfer();
        dt.items.add(file);
        const inp = document.getElementById("backup-import-input");
        inp.files = dt.files;
        inp.dispatchEvent(new Event("change", { bubbles: true }));
        return true;
      })()
    `, true);

    await waitFor(
      win.webContents,
      `(() => {
        const box = document.getElementById("backup-preview-box");
        return box && !box.classList.contains("hidden") && box.innerText.includes("Backup Preview");
      })()`,
      12000,
      "backup preview box"
    );

    results.backupPreview = await win.webContents.executeJavaScript(`
      ({
        previewText: document.getElementById("backup-preview-box").innerText,
        heroSafeBeforeConfirm: document.getElementById("hero-total-safe").textContent.trim(),
      })
    `);

    // Click Confirm & Replace Ledger
    await win.webContents.executeJavaScript(`
      (() => {
        const btns = Array.from(document.querySelectorAll("#backup-preview-box button"));
        const confirmBtn = btns.find((b) => b.textContent.includes("Confirm"));
        if (!confirmBtn) throw new Error("Missing Confirm & Replace Ledger button");
        confirmBtn.click();
        return true;
      })()
    `, true);

    await waitFor(
      win.webContents,
      `document.getElementById("backup-preview-box").classList.contains("hidden")`,
      12000,
      "backup confirm completion"
    );
    await sleep(500);

    await win.webContents.executeJavaScript(`document.getElementById("tab-btn-today").click(); true;`, true);
    await sleep(450);
    results.restoredCommand = await win.webContents.executeJavaScript(`
      ({
        totalSafe: document.getElementById("hero-total-safe").textContent.trim(),
        dailySafe: document.getElementById("hero-daily-safe").textContent.trim(),
      })
    `);

    // 4. Desktop (1440x900) vs Mobile (375x812) Sankey Stack Visibility & Readability
    await win.webContents.executeJavaScript(`document.getElementById("tab-btn-analytics").click(); true;`, true);
    await waitFor(
      win.webContents,
      `Boolean(document.querySelector("#analytics-sankey-wrap .sankey-desktop-svg")) && Boolean(document.querySelector("#analytics-sankey-wrap .sankey-mobile-stack"))`,
      12000,
      "populated sankey render"
    );
    await sleep(300);

    results.sankeyDesktop = await win.webContents.executeJavaScript(`
      (() => {
        const svg = document.querySelector("#analytics-sankey-wrap .sankey-desktop-svg");
        const mob = document.querySelector("#analytics-sankey-wrap .sankey-mobile-stack");
        const rect = svg.getBoundingClientRect();
        return {
          innerWidth: window.innerWidth,
          svgDisplay: getComputedStyle(svg).display,
          svgWidth: rect.width,
          svgHeight: rect.height,
          mobileDisplay: getComputedStyle(mob).display,
        };
      })()
    `);

    // 4b. Pointer-event drag-and-drop on SVG diamond goal marker at Desktop (1440x900) + Undo Shift
    await waitFor(
      win.webContents,
      `Boolean(document.querySelector("#timeline-svg-wrap .timeline-goal-marker polygon"))`,
      10000,
      "desktop timeline goal marker"
    );
    await win.webContents.executeJavaScript(`
      document.querySelector("#timeline-svg-wrap .timeline-goal-marker").scrollIntoView({ block: "center", inline: "center" });
      true;
    `);
    await sleep(150);

    const deskDragGeom = await win.webContents.executeJavaScript(`
      (() => {
        const marker = document.querySelector("#timeline-svg-wrap .timeline-goal-marker");
        const poly = marker.querySelector("polygon");
        const svg = document.querySelector("#timeline-svg-wrap svg");
        const pr = poly.getBoundingClientRect();
        const sr = svg.getBoundingClientRect();
        return {
          beforeIso: marker.getAttribute("aria-valuetext"),
          startX: Math.round(pr.left + pr.width / 2),
          startY: Math.round(pr.top + pr.height / 2),
          targetX: Math.round(sr.left + sr.width * 0.35),
        };
      })()
    `);
    win.webContents.sendInputEvent({ type: "mouseDown", x: deskDragGeom.startX, y: deskDragGeom.startY, button: "left", clickCount: 1 });
    await sleep(60);
    win.webContents.sendInputEvent({ type: "mouseMove", x: deskDragGeom.targetX, y: deskDragGeom.startY, button: "left" });
    await sleep(60);
    win.webContents.sendInputEvent({ type: "mouseUp", x: deskDragGeom.targetX, y: deskDragGeom.startY, button: "left", clickCount: 1 });

    await waitFor(
      win.webContents,
      `(async () => {
        const res = await fetch("/api/v1/goals", { credentials: "same-origin" });
        const gs = await res.json();
        return gs.length > 0 && Boolean(gs[0].target_date);
      })()`,
      10000,
      "desktop goal drag server commit"
    );
    const deskGoalAfterDrag = await win.webContents.executeJavaScript(
      `(async () => (await (await fetch("/api/v1/goals", { credentials: "same-origin" })).json())[0].target_date)()`,
      true
    );

    // Click "Undo Shift" toast button and verify originally undated goal restores target_date === null
    await waitFor(
      win.webContents,
      `Array.from(document.querySelectorAll("#toast-region button")).some((b) => b.textContent.includes("Undo Shift"))`,
      6000,
      "Undo Shift toast button"
    );
    await win.webContents.executeJavaScript(`
      Array.from(document.querySelectorAll("#toast-region button")).find((b) => b.textContent.includes("Undo Shift")).click();
      true;
    `, true);
    await waitFor(
      win.webContents,
      `(async () => {
        const res = await fetch("/api/v1/goals", { credentials: "same-origin" });
        const gs = await res.json();
        return gs.length > 0 && gs[0].target_date === null;
      })()`,
      10000,
      "Undo Shift restores null target_date"
    );
    await sleep(450); // allow post-undo loadTimelineData() + refreshAll() re-render to settle
    results.desktopGoalDrag = {
      beforeDisplayIso: deskDragGeom.beforeIso,
      committedDate: deskGoalAfterDrag,
      restoredNullOnUndo: true,
    };

    win.setContentSize(375, 812);
    await waitFor(win.webContents, `window.innerWidth === 375`, 8000, "mobile 375x812 resize");
    await sleep(300);

    // 4c. Pointer-event drag-and-drop on SVG diamond goal marker at Mobile (375x812)
    await win.webContents.executeJavaScript(`
      (() => {
        const tr = document.getElementById("toast-region");
        if (tr) tr.replaceChildren();
        document.getElementById("timeline-svg-wrap").scrollIntoView({ block: "center" });
        return true;
      })()
    `);
    await sleep(200);
    const mobDragGeom = await win.webContents.executeJavaScript(`
      (() => {
        const marker = document.querySelector("#timeline-svg-wrap .timeline-goal-marker");
        const hit = marker.querySelector(".timeline-marker-hit") || marker.querySelector("polygon");
        const svg = document.querySelector("#timeline-svg-wrap svg");
        const pr = hit.getBoundingClientRect();
        const sr = svg.getBoundingClientRect();
        const startX = Math.min(window.innerWidth - 6, Math.max(6, Math.round(pr.left + pr.width / 2)));
        const startY = Math.round(pr.top + pr.height / 2);
        const topEl = document.elementFromPoint(startX, startY);
        return {
          startX,
          startY,
          targetX: Math.round(sr.left + sr.width * 0.45),
          hitTag: topEl ? topEl.tagName : null,
          hitClass: topEl ? String(topEl.getAttribute("class") || "") : null,
        };
      })()
    `);
    results.mobDragGeom = mobDragGeom;
    win.webContents.sendInputEvent({ type: "mouseDown", x: mobDragGeom.startX, y: mobDragGeom.startY, button: "left", clickCount: 1 });
    await sleep(60);
    win.webContents.sendInputEvent({ type: "mouseMove", x: mobDragGeom.targetX, y: mobDragGeom.startY, button: "left" });
    await sleep(60);
    win.webContents.sendInputEvent({ type: "mouseUp", x: mobDragGeom.targetX, y: mobDragGeom.startY, button: "left", clickCount: 1 });

    await waitFor(
      win.webContents,
      `(async () => {
        const res = await fetch("/api/v1/goals", { credentials: "same-origin" });
        const gs = await res.json();
        return gs.length > 0 && Boolean(gs[0].target_date);
      })()`,
      10000,
      "mobile goal drag server commit"
    );
    results.mobileGoalDrag = {
      committedDate: await win.webContents.executeJavaScript(
        `(async () => (await (await fetch("/api/v1/goals", { credentials: "same-origin" })).json())[0].target_date)()`,
        true
      ),
    };

    // Scroll back to Sankey on Mobile (375x812) for bounding-box measurement
    await win.webContents.executeJavaScript(`
      document.getElementById("analytics-sankey-wrap").scrollIntoView({ block: "start" });
      true;
    `);
    await sleep(150);

    results.sankeyMobile = await win.webContents.executeJavaScript(`
      (() => {
        const svg = document.querySelector("#analytics-sankey-wrap .sankey-desktop-svg");
        const mob = document.querySelector("#analytics-sankey-wrap .sankey-mobile-stack");
        const rows = Array.from(mob.querySelectorAll(".sankey-mobile-row"));
        return {
          innerWidth: window.innerWidth,
          innerHeight: window.innerHeight,
          svgDisplay: getComputedStyle(svg).display,
          mobileDisplay: getComputedStyle(mob).display,
          rowCount: rows.length,
          rows: rows.map((r) => {
            const b = r.getBoundingClientRect();
            return {
              text: r.innerText.replace(/\s+/g, " ").trim(),
              width: b.width,
              height: b.height,
              right: b.right,
              fontSizePx: parseFloat(getComputedStyle(r).fontSize),
            };
          }),
        };
      })()
    `);

    // Also verify mobile 375x812 Command & Credit rendering without horizontal card overflow
    await win.webContents.executeJavaScript(`document.getElementById("tab-btn-today").click(); true;`, true);
    await sleep(200);
    results.mobileCommand = await win.webContents.executeJavaScript(`
      (() => {
        const hero = document.querySelector(".hero-card").getBoundingClientRect();
        return { innerWidth: window.innerWidth, heroRight: hero.right, heroWidth: hero.width };
      })()
    `);

    // Restore 1440x900 desktop viewport
    win.setContentSize(1440, 900);
    await waitFor(win.webContents, `window.innerWidth === 1440`, 8000, "desktop 1440x900 restore");
    await win.webContents.executeJavaScript(`document.getElementById("tab-btn-analytics").click(); true;`, true);
    await sleep(200);

    // Exercise interactive Forecast Scenario creation in Analytics & Forecast tab
    await win.webContents.executeJavaScript(`
      (() => {
        document.getElementById("btn-timeline-new-scenario").click();
        document.getElementById("ts-name").value = "Side Project +$400/mo";
        document.getElementById("ts-monthly-delta").value = "400";
        document.getElementById("timeline-scenario-form").requestSubmit();
        return true;
      })()
    `, true);
    await waitFor(
      win.webContents,
      `document.getElementById("timeline-delta-readout").textContent.trim() !== "$0.00"`,
      10000,
      "scenario forecast delta update"
    );
    results.scenarioForecastDelta = await win.webContents.executeJavaScript(
      `document.getElementById("timeline-delta-readout").textContent.trim()`
    );

    // 5. Inspect rendered DOM across ALL 8 tabs for forbidden strings & Credit/Goals tab details
    const tabs = ["today", "analytics", "portfolio", "credit", "goals", "activity", "coach", "settings"];
    const tabSweep = {};
    for (const tab of tabs) {
      await win.webContents.executeJavaScript(`document.getElementById("tab-btn-${tab}").click(); true;`, true);
      await sleep(350);
      if (tab === "goals") {
        await win.webContents.executeJavaScript(`
          (() => {
            document.getElementById("gp-name").value = "Workstation Display";
            document.getElementById("gp-price").value = "950";
            document.getElementById("gp-saved").value = "50";
            document.getElementById("goal-plan-form").requestSubmit();
            return true;
          })()
        `, true);
        await waitFor(
          win.webContents,
          `!document.getElementById("goal-plan-output").classList.contains("hidden")`,
          10000,
          "goal planner output"
        );
      }
      if (tab === "credit") {
        await waitFor(
          win.webContents,
          `document.getElementById("cred-bureau-score") && document.getElementById("cred-bureau-score").textContent.trim() === "758"`,
          10000,
          "credit bureau score 758"
        );
        // Also test switching to VantageScore 3.0 weights and back
        const vantageCount = await win.webContents.executeJavaScript(`
          (() => {
            const btn = document.querySelector('button[data-factor-model="vantage3"]');
            if (btn) btn.click();
            return document.querySelectorAll("#credit-factors-wrap .factor-row").length;
          })()
        `, true);
        const ficoCount = await win.webContents.executeJavaScript(`
          (() => {
            const btn = document.querySelector('button[data-factor-model="fico8"]');
            if (btn) btn.click();
            return document.querySelectorAll("#credit-factors-wrap .factor-row").length;
          })()
        `, true);
        results.creditDetails = await win.webContents.executeJavaScript(`
          ({
            score: document.getElementById("cred-bureau-score").textContent.trim(),
            ficoFactorCount: ${ficoCount},
            vantageFactorCount: ${vantageCount},
            strategyCount: document.querySelectorAll("#debt-strategies-grid > *").length,
            hasSandboxOption: Boolean(document.querySelector('option[value="sandbox"]')),
          })
        `);
      }
      const text = await win.webContents.executeJavaScript(`document.body.innerText`);
      tabSweep[tab] = {
        length: text.length,
        hasSimulator: /simulat/i.test(text),
        hasPts: /\bpts?\b/i.test(text),
        hasSandbox: /sandbox/i.test(text),
        hasWhatIf: /what-if/i.test(text),
        matches: [
          ...(text.match(/[^\n]*simulat[^\n]*/gi) || []),
          ...(text.match(/[^\n]*\bpts?\b[^\n]*/gi) || []),
          ...(text.match(/[^\n]*sandbox[^\n]*/gi) || []),
          ...(text.match(/[^\n]*what-if[^\n]*/gi) || []),
        ],
      };
    }
    results.tabSweep = tabSweep;
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
def test_real_chromium_electron_rendering_desktop_mobile_backup_and_dom_hygiene(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Isolate test server from any ambient host LLM keys so coach tab uses deterministic local engine
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

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    db_path = tmp_path / "live_e2e.sqlite3"
    secrets_path = tmp_path / "secrets.enc"
    backup_path = _build_seed_backup_json(tmp_path)
    report_path = tmp_path / "e2e_report.json"
    runner_script = tmp_path / "electron_e2e_runner.cjs"
    runner_script.write_text(ELECTRON_E2E_SCRIPT, encoding="utf-8")

    svc = FinanceService(db_path=str(db_path), secret_store_path=secrets_path)
    app = create_app(db_path=str(db_path), service=svc, enforce_vault_lock=True, allowed_port=port)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    try:
        # Wait for live HTTP server readiness
        deadline = time.monotonic() + 10.0
        ready = False
        while time.monotonic() < deadline:
            try:
                r = httpx.get(f"{base_url}/api/health", timeout=1.0)
                if r.status_code == 200:
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
            ],
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        assert report_path.exists(), (
            f"Electron runner exited ({proc.returncode}) without writing report:\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
        rep = json.loads(report_path.read_text(encoding="utf-8"))
        assert rep.get("ok") is True, f"Browser E2E failed:\n{json.dumps(rep, indent=2)}\nSTDERR:\n{proc.stderr}"

        # 1. Zero console / page / 5xx errors
        assert rep["consoleErrors"] == []
        assert rep["pageErrors"] == []
        assert rep["server5xx"] == []

        # 2. Desktop viewport & Titanium iOS squircle card corners + $0 fresh-profile rendering
        assert rep["desktopViewport"] == {"w": 1440, "h": 900}
        assert rep["freshCommand"]["dailySafe"] == "$0.00"
        assert rep["freshCommand"]["totalSafe"] == "$0.00"
        assert rep["freshCommand"]["safeInvest"] == "$0.00"
        assert rep["freshCommand"]["jarCount"] == 5
        assert rep["freshCommand"]["cardBorderRadius"] == "16px"
        for jt in rep["freshCommand"]["jarTexts"]:
            assert "$4,777.14" not in jt
            assert "$0.00" in jt

        assert rep["freshForecast"]["ending"] == "$0.00"
        assert rep["freshForecast"]["nadir"] == "$0.00"
        assert rep["freshForecast"]["delta"] in ("$0.00", "+$0.00")
        assert rep["freshCredit"] == {"score": "--", "totalDebt": "$0.00", "factorCount": 5}
        assert "Monthly flexible surplus: $0.00" in rep["freshGoalPlanText"]
        assert "$150.00" not in rep["freshGoalPlanText"]
        assert rep["scenarioForecastDelta"].startswith("+$")
        assert rep["desktopGoalDrag"]["committedDate"]
        assert rep["desktopGoalDrag"]["committedDate"] != rep["desktopGoalDrag"]["beforeDisplayIso"]
        assert rep["desktopGoalDrag"]["restoredNullOnUndo"] is True
        assert rep["mobileGoalDrag"]["committedDate"]

        # 3. Backup preview -> confirm & restore flow
        prev_txt = rep["backupPreview"]["previewText"]
        assert "3 account(s)" in prev_txt
        assert "4 transaction(s) (2026-10-15 to 2026-11-18)" in prev_txt
        assert "1 bill(s)" in prev_txt
        assert "1 goal(s)" in prev_txt
        assert "1 holding(s)" in prev_txt
        assert rep["backupPreview"]["heroSafeBeforeConfirm"] == "$0.00"
        assert rep["restoredCommand"]["totalSafe"] != "$0.00"

        # 4. Desktop (1440x900) vs Mobile (375x812) Sankey visibility & readability
        assert rep["sankeyDesktop"]["innerWidth"] == 1440
        assert rep["sankeyDesktop"]["svgDisplay"] != "none"
        assert rep["sankeyDesktop"]["svgWidth"] >= 600
        assert rep["sankeyDesktop"]["svgHeight"] >= 200
        assert rep["sankeyDesktop"]["mobileDisplay"] == "none"

        assert rep["sankeyMobile"]["innerWidth"] == 375
        assert rep["sankeyMobile"]["innerHeight"] == 812
        assert rep["sankeyMobile"]["svgDisplay"] == "none"
        assert rep["sankeyMobile"]["mobileDisplay"] == "flex"
        assert rep["sankeyMobile"]["rowCount"] >= 4
        for row in rep["sankeyMobile"]["rows"]:
            assert row["width"] >= 250
            assert row["height"] >= 24
            assert row["right"] <= 375.5
            assert row["fontSizePx"] >= 12.0
        assert rep["mobileCommand"]["innerWidth"] == 375
        assert rep["mobileCommand"]["heroWidth"] >= 300
        assert rep["mobileCommand"]["heroRight"] <= 375.5

        # 5. Credit tab actual bureau score + 5-factor FICO / 6-factor Vantage + 4 strategies + zero sandbox option
        assert rep["creditDetails"]["score"] == "758"
        assert rep["creditDetails"]["ficoFactorCount"] == 5
        assert rep["creditDetails"]["vantageFactorCount"] == 6
        assert rep["creditDetails"]["strategyCount"] == 4
        assert rep["creditDetails"]["hasSandboxOption"] is False

        # 6. Zero simulator / pts / sandbox / What-If strings across ALL 8 rendered tabs
        for tab_name, sweep in rep["tabSweep"].items():
            assert sweep["length"] > 100
            assert sweep["hasSimulator"] is False, f"Simulator text rendered on tab '{tab_name}': {sweep['matches']}"
            assert sweep["hasPts"] is False, f"Fabricated pts text rendered on tab '{tab_name}': {sweep['matches']}"
            assert sweep["hasSandbox"] is False, f"Sandbox text rendered on tab '{tab_name}': {sweep['matches']}"
            assert sweep["hasWhatIf"] is False, f"What-If text rendered on tab '{tab_name}': {sweep['matches']}"
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)
        svc.close()
