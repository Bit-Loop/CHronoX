"use strict";

/**
 * Non-intrusive floating "Sync Page to ChronoX" pill on logged-in Credit Karma & Experian pages.
 * Never activates on login / OAuth / 2FA challenge URLs so 2FA completes undisturbed.
 */
(function initChronoXPill() {
  const host = (location.hostname || "").toLowerCase();
  const path = (location.pathname || "").toLowerCase();

  // Hands-off during login, Intuit SSO, OAuth, or 2FA verification steps
  if (
    host.includes("accounts.intuit.com") ||
    /\/(login|signin|sign-in|auth|oauth|mfa|verify|challenge|two-factor)\b/.test(path)
  ) {
    return;
  }
  if (document.getElementById("chronox-companion-pill-root")) return;

  const hostEl = document.createElement("div");
  hostEl.id = "chronox-companion-pill-root";
  const shadow = hostEl.attachShadow({ mode: "open" });

  const style = document.createElement("style");
  style.textContent = `
    .pill-wrap {
      position: fixed;
      right: 18px;
      bottom: 18px;
      z-index: 2147483646;
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      background: #0b0f14;
      color: #e6edf3;
      border: 1px solid #10b981;
      border-radius: 999px;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
      font: 600 12px/1.2 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }
    .sync-btn {
      all: unset;
      cursor: pointer;
      color: #10b981;
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }
    .sync-btn:hover { color: #34d399; }
    .sync-btn:disabled { opacity: 0.6; cursor: default; }
    .dot {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: #10b981;
      display: inline-block;
    }
    .close-btn {
      all: unset;
      cursor: pointer;
      color: #8b949e;
      font-size: 14px;
      padding: 0 2px;
    }
    .close-btn:hover { color: #e6edf3; }
  `;

  const wrap = document.createElement("div");
  wrap.className = "pill-wrap";

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "sync-btn";

  const dot = document.createElement("span");
  dot.className = "dot";
  const label = document.createElement("span");
  label.textContent = "Sync Credit Page \u2192 ChronoX";
  btn.append(dot, label);

  const closeBtn = document.createElement("button");
  closeBtn.type = "button";
  closeBtn.className = "close-btn";
  closeBtn.title = "Hide for this tab";
  closeBtn.textContent = "\u00d7";
  closeBtn.addEventListener("click", () => hostEl.remove());

  btn.addEventListener("click", () => {
    if (typeof window.__chronoxExtractPageSnapshot !== "function") return;
    btn.disabled = true;
    label.textContent = "Syncing to ChronoX...";
    const snapshot = window.__chronoxExtractPageSnapshot();
    chrome.runtime.sendMessage(
      { type: "CHRONOX_SYNC_CREDIT", payload: snapshot },
      (res) => {
        btn.disabled = false;
        if (chrome.runtime.lastError || !res || !res.ok) {
          const errMsg =
            (chrome.runtime.lastError && chrome.runtime.lastError.message) ||
            (res && res.error) ||
            "Sync failed";
          dot.style.background = "#ef4444";
          label.textContent = errMsg.slice(0, 42);
          return;
        }
        const s = res.synced || {};
        const totalItems =
          (s.scores_upserted || 0) +
          (s.tradelines_added || 0) +
          (s.tradelines_updated || 0) +
          (s.inquiries_added || 0) +
          (s.derogatories_added || 0);
        dot.style.background = "#10b981";
        label.textContent =
          totalItems > 0
            ? `Synced (${s.scores_upserted || 0} scores, ${(s.tradelines_added || 0) + (s.tradelines_updated || 0)} accts)`
            : "Up to date in ChronoX";
      }
    );
  });

  wrap.append(btn, closeBtn);
  shadow.append(style, wrap);
  document.documentElement.appendChild(hostEl);
})();
