"use strict";

(() => {
  const DEFAULT_LABELS = {
    starting: "Starting local vault",
    probing: "Checking readiness",
    ready: "Opening ChronoX",
    error: "Startup interrupted",
  };

  const DEFAULT_DETAILS = {
    starting: "Launching local ChronoX Finance engine",
    probing: "Verifying local API health on 127.0.0.1",
    ready: "Local vault connected — opening dashboard",
    error: "Could not reach local ChronoX Finance engine.",
  };

  const DEFAULT_PROGRESS = {
    starting: 26,
    probing: 68,
    ready: 100,
    error: 100,
  };

  const elCard = document.getElementById("splash-card");
  const elVersion = document.getElementById("splash-version");
  const elBootPanel = document.getElementById("boot-panel");
  const elErrorPanel = document.getElementById("error-panel");
  const elStatusLabel = document.getElementById("status-label");
  const elStatusDetail = document.getElementById("status-detail");
  const elStatusDot = document.querySelector(".status-dot");
  const elStatusReadyIcon = document.querySelector(".status-icon-ready");
  const elProgressTrack = document.getElementById("progress-track");
  const elProgressFill = document.getElementById("progress-fill");
  const elErrorTitle = document.getElementById("error-title");
  const elErrorDetail = document.getElementById("error-detail");
  const btnRetry = document.getElementById("btn-retry");
  const btnViewLogs = document.getElementById("btn-view-logs");
  const btnQuit = document.getElementById("btn-quit");
  const elLogsSheet = document.getElementById("logs-sheet");
  const elLogsList = document.getElementById("logs-list");
  const elLogsMeta = document.getElementById("logs-meta");
  const btnCopyLogs = document.getElementById("btn-copy-logs");
  const btnCloseLogs = document.getElementById("btn-close-logs");

  const bridge = window.chronoxSplash || null;
  let lastLogText = "";
  let currentStage = "starting";

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

  function clampProgress(value, stage) {
    if (typeof value === "number" && Number.isFinite(value)) {
      return Math.max(6, Math.min(100, Math.round(value)));
    }
    return DEFAULT_PROGRESS[stage] || 20;
  }

  function applyStatus(payload) {
    if (!payload || typeof payload !== "object") {
      return;
    }

    if (payload.theme) {
      applyTheme(payload.theme);
    }

    if (payload.appVersion && typeof payload.appVersion === "string") {
      elVersion.textContent = payload.appVersion.startsWith("v")
        ? payload.appVersion
        : `v${payload.appVersion}`;
    }

    const stage =
      payload.stage === "probing" ||
      payload.stage === "ready" ||
      payload.stage === "error"
        ? payload.stage
        : "starting";

    currentStage = stage;
    elCard.setAttribute("data-stage", stage);

    const isError = stage === "error";
    const isReady = stage === "ready";

    elBootPanel.classList.toggle("hidden", isError);
    elErrorPanel.classList.toggle("hidden", !isError);

    if (isError) {
      elErrorTitle.textContent =
        (typeof payload.label === "string" && payload.label.trim()) ||
        "Local vault failed to start";
      elErrorDetail.textContent =
        (typeof payload.error === "string" && payload.error.trim()) ||
        (typeof payload.detail === "string" && payload.detail.trim()) ||
        DEFAULT_DETAILS.error;
      btnRetry.disabled = false;
      return;
    }

    elLogsSheet.classList.add("hidden");

    const label =
      (typeof payload.label === "string" && payload.label.trim()) ||
      DEFAULT_LABELS[stage];
    const detail =
      (typeof payload.detail === "string" && payload.detail.trim()) ||
      DEFAULT_DETAILS[stage];
    const pct = clampProgress(payload.progress, stage);

    elStatusLabel.textContent = label;
    elStatusDetail.textContent = detail;
    elProgressFill.style.width = `${pct}%`;
    elProgressTrack.setAttribute("aria-valuenow", String(pct));

    elStatusDot.classList.toggle("hidden", isReady);
    elStatusReadyIcon.classList.toggle("hidden", !isReady);
  }

  function formatTimestamp(ts) {
    if (!ts) return "";
    try {
      const date = new Date(ts);
      if (!Number.isNaN(date.getTime())) {
        return date.toTimeString().slice(0, 8);
      }
    } catch (_err) {
      // ignore malformed timestamps
    }
    return String(ts).slice(0, 8);
  }

  function renderLogs(data) {
    while (elLogsList.firstChild) {
      elLogsList.removeChild(elLogsList.firstChild);
    }

    const lines = data && Array.isArray(data.lines) ? data.lines : [];
    const textChunks = [];

    if (lines.length === 0) {
      const empty = document.createElement("div");
      empty.className = "log-row";
      empty.textContent = "No backend output captured yet.";
      elLogsList.appendChild(empty);
      lastLogText = "";
    } else {
      const recent = lines.slice(-120);
      for (const entry of recent) {
        const row = document.createElement("div");
        const isErr = entry && entry.stream === "stderr";
        row.className = isErr ? "log-row log-row-stderr" : "log-row";

        const tsStr = formatTimestamp(entry && entry.ts);
        if (tsStr) {
          const tsEl = document.createElement("span");
          tsEl.className = "log-ts";
          tsEl.textContent = tsStr;
          row.appendChild(tsEl);
        }

        const msgEl = document.createElement("span");
        const text =
          entry && typeof entry.text === "string" ? entry.text : String(entry || "");
        msgEl.textContent = text;
        row.appendChild(msgEl);

        elLogsList.appendChild(row);
        textChunks.push(tsStr ? `[${tsStr}] ${text}` : text);
      }
      lastLogText = textChunks.join("\n");
      elLogsList.scrollTop = elLogsList.scrollHeight;
    }

    const mode = data && data.backendMode ? data.backendMode : "local";
    const pid = data && data.pid ? `PID ${data.pid}` : "stopped";
    const url = data && data.backendUrl ? ` · ${data.backendUrl}` : "";
    elLogsMeta.textContent = `Mode: ${mode} · ${pid}${url}`;
  }

  async function openLogs() {
    elLogsSheet.classList.remove("hidden");
    if (!bridge || typeof bridge.getBackendLogs !== "function") {
      renderLogs({ lines: [], backendMode: "preview" });
      return;
    }
    try {
      const logs = await bridge.getBackendLogs();
      renderLogs(logs);
    } catch (err) {
      renderLogs({
        lines: [{ stream: "stderr", text: `Unable to read logs: ${err && err.message ? err.message : String(err)}` }],
      });
    }
  }

  async function handleRetry() {
    btnRetry.disabled = true;
    applyStatus({
      stage: "starting",
      label: "Restarting local vault",
      detail: "Retrying local backend connection",
      progress: 18,
    });
    if (bridge && typeof bridge.retry === "function") {
      try {
        await bridge.retry();
      } catch (err) {
        applyStatus({
          stage: "error",
          label: "Retry failed",
          error: err && err.message ? err.message : String(err),
        });
      }
    }
  }

  btnRetry.addEventListener("click", () => {
    void handleRetry();
  });

  btnViewLogs.addEventListener("click", () => {
    void openLogs();
  });

  btnCloseLogs.addEventListener("click", () => {
    elLogsSheet.classList.add("hidden");
  });

  btnCopyLogs.addEventListener("click", async () => {
    if (!lastLogText) return;
    try {
      if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
        await navigator.clipboard.writeText(lastLogText);
        btnCopyLogs.textContent = "Copied";
        window.setTimeout(() => {
          btnCopyLogs.textContent = "Copy";
        }, 1200);
      }
    } catch (_err) {
      btnCopyLogs.textContent = "Copy failed";
      window.setTimeout(() => {
        btnCopyLogs.textContent = "Copy";
      }, 1200);
    }
  });

  btnQuit.addEventListener("click", () => {
    if (bridge && typeof bridge.quit === "function") {
      void bridge.quit();
    }
  });

  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (!elLogsSheet.classList.contains("hidden")) {
        elLogsSheet.classList.add("hidden");
        return;
      }
      if (currentStage === "error" && bridge && typeof bridge.quit === "function") {
        void bridge.quit();
      }
    } else if (
      e.key === "Enter" &&
      currentStage === "error" &&
      elLogsSheet.classList.contains("hidden") &&
      document.activeElement === document.body
    ) {
      void handleRetry();
    }
  });

  // Subscribe to live status push events and pull initial snapshot
  let unsubscribe = null;
  if (bridge && typeof bridge.onStatus === "function") {
    unsubscribe = bridge.onStatus((payload) => {
      applyStatus(payload);
    });
  }

  if (bridge && typeof bridge.getInitialState === "function") {
    bridge
      .getInitialState()
      .then((initial) => {
        if (initial) {
          applyStatus(initial);
        }
      })
      .catch(() => {
        applyTheme();
      });
  } else {
    applyTheme();
  }

  window.addEventListener("beforeunload", () => {
    if (typeof unsubscribe === "function") {
      unsubscribe();
    }
  });
})();
