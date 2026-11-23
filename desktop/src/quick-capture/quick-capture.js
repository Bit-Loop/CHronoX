"use strict";

(() => {
  const AUTO_CLOSE_MS = 950;
  const EMOJI_RE = /[\p{Extended_Pictographic}\uFE0E\uFE0F]/gu;

  const elCard = document.getElementById("hud-card");
  const elCaptureView = document.getElementById("capture-view");
  const elConfirmView = document.getElementById("confirm-view");
  const elForm = document.getElementById("hud-form");
  const elInput = document.getElementById("hud-input");
  const elIntentBadge = document.getElementById("intent-badge");
  const elIntentTag = document.getElementById("intent-tag");
  const elIconNeutral = document.querySelector(".intent-icon-neutral");
  const elIconIncome = document.querySelector(".intent-icon-income");
  const elIconImpulse = document.querySelector(".intent-icon-impulse");
  const btnSubmit = document.getElementById("btn-submit");
  const elSubmitLabel = document.getElementById("btn-submit-label");
  const btnOpenApp = document.getElementById("btn-open-app");
  const btnCloseHud = document.getElementById("btn-close-hud");
  const elError = document.getElementById("hud-error");
  const elErrorText = document.getElementById("hud-error-text");
  const btnUnlockApp = document.getElementById("btn-unlock-app");
  const elFooter = document.getElementById("hud-footer");
  const elShortcutHint = document.getElementById("hud-shortcut-hint");
  const elLiveSafePill = document.getElementById("live-safe-pill");
  const elLiveSafeValue = document.getElementById("live-safe-value");
  const elConfirmKicker = document.getElementById("confirm-kicker");
  const elConfirmMessage = document.getElementById("confirm-message");
  const elConfirmSafeCard = document.getElementById("confirm-safe-card");
  const elConfirmSafeAmount = document.getElementById("confirm-safe-amount");
  const elConfirmSafeMeta = document.getElementById("confirm-safe-meta");
  const elConfirmProgress = document.getElementById("confirm-progress-fill");

  const bridge = window.chronoxHud || null;
  let backendUrl = "";
  let isSubmitting = false;
  let closeTimer = null;
  let lastResultKind = "transaction";

  function applyTheme(theme) {
    if (theme === "light" || theme === "dark") {
      document.documentElement.setAttribute("data-theme", theme);
      return;
    }
    const prefersLight =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-color-scheme: light)").matches;
    document.documentElement.setAttribute("data-theme", prefersLight ? "light" : "dark");
  }

  function sanitizeText(raw) {
    return String(raw || "")
      .replace(EMOJI_RE, "")
      .replace(/\s{2,}/g, " ")
      .trim();
  }

  function formatMoneyFromCents(cents) {
    if (typeof cents !== "number" || !Number.isFinite(cents)) {
      return null;
    }
    const dollars = cents / 100;
    const sign = dollars < 0 ? "-" : "";
    const formatted = Math.abs(dollars).toLocaleString("en-US", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
    return `${sign}$${formatted}`;
  }

  function formatShortcutLabel(shortcut) {
    if (!shortcut || typeof shortcut !== "string") {
      return "Ctrl+Shift+Space";
    }
    const isMac =
      typeof navigator !== "undefined" &&
      /Mac|Darwin/i.test(navigator.platform || navigator.userAgent || "");
    if (isMac) {
      return shortcut
        .replace(/CommandOrControl/gi, "Cmd")
        .replace(/Control/gi, "Ctrl");
    }
    return shortcut.replace(/CommandOrControl/gi, "Ctrl");
  }

  function updateSnapshotDisplay(snapshot) {
    if (!snapshot || typeof snapshot !== "object") {
      return;
    }
    const formatted = formatMoneyFromCents(snapshot.dailySafeCents);
    if (formatted) {
      elLiveSafeValue.textContent = formatted;
      elLiveSafePill.classList.remove("hidden");
    }
  }

  function clearCloseTimer() {
    if (closeTimer !== null) {
      window.clearTimeout(closeTimer);
      closeTimer = null;
    }
  }

  function showError(message, options) {
    const locked = Boolean(options && options.locked);
    elErrorText.textContent = sanitizeText(message) || "Could not log entry. Try again.";
    btnUnlockApp.classList.toggle("hidden", !locked);
    elError.classList.remove("hidden");
    elFooter.classList.add("hidden");
  }

  function clearError() {
    elError.classList.add("hidden");
    btnUnlockApp.classList.add("hidden");
    elFooter.classList.remove("hidden");
  }

  function detectIntent(raw) {
    const text = String(raw || "").trim().toLowerCase();
    if (!text) return "neutral";
    if (/^(impulse|park|cool|wish|want)\b/.test(text)) {
      return "impulse";
    }
    if (
      /(^|\s)\+\s*\$?\d/.test(text) ||
      /\b(paycheck|income|salary|bonus|deposit|refund|dividend)\b/.test(text)
    ) {
      return "income";
    }
    if (/\d/.test(text)) {
      return "expense";
    }
    return "neutral";
  }

  function updateIntentVisuals(raw) {
    const intent = detectIntent(raw);
    elForm.setAttribute("data-intent", intent);
    elIntentBadge.setAttribute("data-intent", intent);
    elIntentTag.setAttribute("data-intent", intent);

    elIconNeutral.classList.toggle("hidden", intent === "income" || intent === "impulse");
    elIconIncome.classList.toggle("hidden", intent !== "income");
    elIconImpulse.classList.toggle("hidden", intent !== "impulse");

    if (intent === "impulse") {
      elIntentTag.textContent = "Vault";
      elIntentTag.classList.remove("hidden");
    } else if (intent === "income") {
      elIntentTag.textContent = "Income";
      elIntentTag.classList.remove("hidden");
    } else if (intent === "expense") {
      elIntentTag.textContent = "Expense";
      elIntentTag.classList.remove("hidden");
    } else {
      elIntentTag.textContent = "";
      elIntentTag.classList.add("hidden");
    }
  }

  function focusInputAtEnd() {
    elInput.focus();
    const len = elInput.value.length;
    try {
      elInput.setSelectionRange(len, len);
    } catch (_err) {
      // ignore if input not selectable yet
    }
  }

  function resetToCapture(statePayload) {
    clearCloseTimer();
    isSubmitting = false;
    btnSubmit.disabled = false;
    elSubmitLabel.textContent = "Log";

    if (statePayload && typeof statePayload === "object") {
      if (statePayload.theme) {
        applyTheme(statePayload.theme);
      }
      if (statePayload.backendUrl) {
        backendUrl = String(statePayload.backendUrl);
      }
      if (statePayload.shortcut) {
        elShortcutHint.textContent = formatShortcutLabel(statePayload.shortcut);
      }
      if (statePayload.snapshot) {
        updateSnapshotDisplay(statePayload.snapshot);
      }
    }

    elCard.setAttribute("data-state", "capture");
    elConfirmView.classList.add("hidden");
    elCaptureView.classList.remove("hidden");
    elConfirmProgress.classList.remove("running");

    elInput.value = "";
    clearError();
    updateIntentVisuals("");

    window.requestAnimationFrame(() => {
      focusInputAtEnd();
    });
  }

  function hideHud() {
    clearCloseTimer();
    if (bridge && typeof bridge.hide === "function") {
      void bridge.hide();
    }
  }

  function openMainApp(tab) {
    clearCloseTimer();
    if (bridge && typeof bridge.openMainApp === "function") {
      void bridge.openMainApp(tab);
    }
  }

  function showConfirmation(result) {
    clearCloseTimer();
    lastResultKind = result && result.kind === "impulse" ? "impulse" : "transaction";

    elConfirmKicker.textContent =
      lastResultKind === "impulse" ? "Parked in Cooling-Off Vault" : "Logged to ChronoX";

    const cleanMsg = sanitizeText(result && result.message);
    elConfirmMessage.textContent = cleanMsg || "Saved and synced with your budget.";

    const dailyFormatted = formatMoneyFromCents(result && result.dailySafeCents);
    const totalFormatted = formatMoneyFromCents(result && result.safeToSpendCents);

    if (dailyFormatted) {
      elConfirmSafeAmount.textContent = dailyFormatted;
      elLiveSafeValue.textContent = dailyFormatted;
      elLiveSafePill.classList.remove("hidden");
      elConfirmSafeCard.classList.remove("hidden");

      const metaParts = [];
      if (totalFormatted) {
        metaParts.push(`${totalFormatted} total`);
      }
      if (typeof result.daysUntilPayday === "number" && Number.isFinite(result.daysUntilPayday)) {
        metaParts.push(`${result.daysUntilPayday}d to payday`);
      }
      elConfirmSafeMeta.textContent = metaParts.join(" · ");
    } else if (totalFormatted) {
      elConfirmSafeAmount.textContent = totalFormatted;
      elConfirmSafeMeta.textContent = "Updated balance";
      elConfirmSafeCard.classList.remove("hidden");
    } else {
      elConfirmSafeCard.classList.add("hidden");
    }

    elCard.setAttribute("data-state", "confirm");
    elCaptureView.classList.add("hidden");
    elConfirmView.classList.remove("hidden");

    // Restart 950ms linear countdown bar smoothly
    elConfirmProgress.classList.remove("running");
    void elConfirmProgress.offsetWidth;
    elConfirmProgress.classList.add("running");

    closeTimer = window.setTimeout(() => {
      closeTimer = null;
      hideHud();
    }, AUTO_CLOSE_MS);
  }

  async function submitViaFallbackApi(text) {
    if (!backendUrl) {
      return { ok: false, error: "Desktop bridge unavailable in standalone preview." };
    }
    const resp = await fetch(`${backendUrl}/api/quick-add`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (resp.status === 401) {
      return { ok: false, locked: true, error: "ChronoX is locked — unlock the main window first." };
    }
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      return { ok: false, error: data.detail || "Could not parse quick-add entry." };
    }

    let dailySafeCents = null;
    let safeToSpendCents = null;
    let daysUntilPayday = null;
    try {
      const dashResp = await fetch(`${backendUrl}/api/dashboard`);
      if (dashResp.ok) {
        const dash = await dashResp.json();
        if (dash && dash.safe_to_spend) {
          dailySafeCents = dash.safe_to_spend.daily_safe_to_spend_cents;
          safeToSpendCents = dash.safe_to_spend.safe_to_spend_cents;
          daysUntilPayday = dash.safe_to_spend.days_until_payday;
        }
      }
    } catch (_err) {
      // ignore non-critical snapshot fetch error
    }

    return {
      ok: true,
      kind: data.kind || "transaction",
      message: data.message || "Logged to ChronoX Finance",
      dailySafeCents,
      safeToSpendCents,
      daysUntilPayday,
    };
  }

  async function handleSubmit(rawText) {
    if (isSubmitting) return;
    const text = String(rawText !== undefined ? rawText : elInput.value).trim();
    if (!text) {
      showError('Type an entry like "coffee 6.50" or "impulse 189 keyboard".');
      elInput.focus();
      return;
    }

    clearError();
    isSubmitting = true;
    btnSubmit.disabled = true;
    elSubmitLabel.textContent = "Saving";

    try {
      const result =
        bridge && typeof bridge.submitQuickAdd === "function"
          ? await bridge.submitQuickAdd(text)
          : await submitViaFallbackApi(text);

      if (result && result.ok) {
        showConfirmation(result);
      } else {
        showError(
          (result && result.error) || "Could not parse quick-add entry.",
          { locked: Boolean(result && result.locked) }
        );
        focusInputAtEnd();
      }
    } catch (err) {
      showError(err && err.message ? err.message : "Could not reach ChronoX engine.");
      focusInputAtEnd();
    } finally {
      isSubmitting = false;
      btnSubmit.disabled = false;
      elSubmitLabel.textContent = "Log";
    }
  }

  elForm.addEventListener("submit", (e) => {
    e.preventDefault();
    void handleSubmit(elInput.value);
  });

  elInput.addEventListener("input", () => {
    clearError();
    updateIntentVisuals(elInput.value);
  });

  document.querySelectorAll("[data-quick]").forEach((chip) => {
    chip.addEventListener("click", () => {
      const value = chip.getAttribute("data-quick") || "";
      elInput.value = value;
      clearError();
      updateIntentVisuals(value);
      focusInputAtEnd();
      if (!value.endsWith(" ")) {
        void handleSubmit(value);
      }
    });
  });

  btnOpenApp.addEventListener("click", () => {
    const targetTab =
      elCard.getAttribute("data-state") === "confirm" && lastResultKind === "impulse"
        ? "impulse"
        : "today";
    openMainApp(targetTab);
  });

  btnCloseHud.addEventListener("click", () => {
    hideHud();
  });

  btnUnlockApp.addEventListener("click", () => {
    openMainApp("today");
  });

  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      hideHud();
    } else if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      openMainApp("today");
    }
  });

  // Wire live Electron IPC subscriptions & initial state hydration
  let unsubShow = null;
  let unsubTheme = null;

  if (bridge && typeof bridge.onShow === "function") {
    unsubShow = bridge.onShow((payload) => {
      resetToCapture(payload);
    });
  }

  if (bridge && typeof bridge.onThemeChange === "function") {
    unsubTheme = bridge.onThemeChange((theme) => {
      applyTheme(theme);
    });
  }

  if (bridge && typeof bridge.getState === "function") {
    bridge
      .getState()
      .then((state) => {
        resetToCapture(state);
      })
      .catch(() => {
        applyTheme();
        focusInputAtEnd();
      });
  } else {
    applyTheme();
    focusInputAtEnd();
  }

  window.addEventListener("beforeunload", () => {
    clearCloseTimer();
    if (typeof unsubShow === "function") unsubShow();
    if (typeof unsubTheme === "function") unsubTheme();
  });
})();
