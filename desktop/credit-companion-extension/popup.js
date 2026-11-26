"use strict";

const connPill = document.getElementById("conn-pill");
const scExp = document.getElementById("sc-experian");
const scTu = document.getElementById("sc-transunion");
const scEq = document.getElementById("sc-equifax");
const ledgerMeta = document.getElementById("ledger-meta");
const btnCapture = document.getElementById("btn-capture");
const statusBox = document.getElementById("status");

function fmtUsd(cents) {
  const val = Number(cents || 0) / 100;
  return val.toLocaleString("en-US", { style: "currency", currency: "USD" });
}

function renderSummary(summary) {
  if (!summary) return;
  const scores = Array.isArray(summary.latest_scores) ? summary.latest_scores : [];
  const byBureau = {};
  for (const s of scores) {
    if (s && s.bureau && byBureau[s.bureau] == null) {
      byBureau[s.bureau] = s.score;
    }
  }
  scExp.textContent = byBureau.experian != null ? String(byBureau.experian) : "--";
  scTu.textContent = byBureau.transunion != null ? String(byBureau.transunion) : "--";
  scEq.textContent = byBureau.equifax != null ? String(byBureau.equifax) : "--";

  if (summary.total_debt_cents != null) {
    ledgerMeta.textContent =
      `Debt ${fmtUsd(summary.total_debt_cents)} · ` +
      `Util ${Number(summary.aggregate_utilization_pct || 0).toFixed(1)}% · ` +
      `${summary.credit_lines_count || 0} accts · ${summary.inquiries_count || 0} inq`;
  }
}

function refreshStatus() {
  chrome.runtime.sendMessage({ type: "CHRONOX_PING" }, (res) => {
    if (chrome.runtime.lastError || !res || !res.ok) {
      connPill.textContent = "Host Offline";
      connPill.className = "pill warn";
      ledgerMeta.textContent =
        (res && res.error) || "Run ./scripts/install-edge-extension.sh to link ChronoX.";
      return;
    }
    connPill.textContent = res.mode === "native_messaging" ? "Direct Local Link" : "HTTP Bridge";
    connPill.className = "pill";
    renderSummary(res.summary);
  });
}

document.getElementById("btn-open-ck").addEventListener("click", () => {
  chrome.tabs.create({ url: "https://www.creditkarma.com/dashboard" });
});

document.getElementById("btn-open-exp").addEventListener("click", () => {
  chrome.tabs.create({ url: "https://usa.experian.com/mfe/member/credit-report" });
});

btnCapture.addEventListener("click", async () => {
  btnCapture.disabled = true;
  statusBox.textContent = "Extracting credit snapshot from active tab...";
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !tab.id) throw new Error("No active browser tab found.");
    if (/^(edge|chrome|about):/i.test(tab.url || "")) {
      throw new Error("Switch to your open Credit Karma or Experian tab first.");
    }

    await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      files: ["extractor.js"],
    });
    const [{ result: snapshot }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => window.__chronoxExtractPageSnapshot(),
    });
    if (!snapshot) throw new Error("Could not capture page content.");

    statusBox.textContent = "Writing directly to local ChronoX ledger...";
    chrome.runtime.sendMessage(
      { type: "CHRONOX_SYNC_CREDIT", payload: snapshot },
      (res) => {
        btnCapture.disabled = false;
        if (chrome.runtime.lastError || !res || !res.ok) {
          const msg =
            (chrome.runtime.lastError && chrome.runtime.lastError.message) ||
            (res && res.error) ||
            "Sync failed.";
          statusBox.textContent = `Error: ${msg}`;
          return;
        }
        renderSummary(res.summary);
        const s = res.synced || {};
        statusBox.textContent =
          `Synced directly into ChronoX!\n` +
          `Scores upserted: ${s.scores_upserted || 0} · ` +
          `Accounts: +${s.tradelines_added || 0} new / ${s.tradelines_updated || 0} updated · ` +
          `Inquiries: +${s.inquiries_added || 0} · ` +
          `Derogatories: +${s.derogatories_added || 0}.\n` +
          `Click "Sync" in ChronoX Desktop to refresh open charts.`;
      }
    );
  } catch (err) {
    btnCapture.disabled = false;
    statusBox.textContent = `Error: ${err.message || err}`;
  }
});

refreshStatus();
