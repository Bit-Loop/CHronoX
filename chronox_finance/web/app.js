/**
 * ChronoX Finance — Industrial ES-Module Application Bootstrapper
 * Coordinates Vault Gate, Core Runtime, and all 5 Visualization/Interaction Modules.
 */

import {
  $,
  api,
  dispatchRefreshHooks,
  initCardSheen,
  initCommandPalette,
  initDesktopBridge,
  initTabIndicatorObserver,
  initTheme,
  setIntelligenceMode,
  setPreferredProvider,
  setRefreshAllImpl,
  showBootSkeletons,
  state,
  switchTab,
  toast,
  toggleTheme,
} from "./js/core.js";
import {
  checkVaultBootStatus,
  initVaultAndShell,
  loadEmailQueue,
  loadLocalAiClis,
  loadSettings,
  loadTransactions,
  loadWeeklyReset,
  renderCommandDashboard,
} from "./js/vault.js";
import * as chartsModule from "./js/charts.js";
import * as timelineModule from "./js/timeline.js";
import * as goalsDndModule from "./js/goals-dnd.js";
import * as portfolioModule from "./js/portfolio.js";
import * as aiModule from "./js/ai.js";

function callModuleInit(mod, primaryName) {
  const fn = mod && (mod[primaryName] || mod.init || mod.default);
  if (typeof fn === "function") {
    try {
      fn();
    } catch (_err) {
      // Keep independent UI panels resilient during bootstrap
    }
  }
}

async function performRefreshAll(opts = {}) {
  if (state.vaultStatus !== "unlocked") return null;
  const dash = await api("/api/v1/dashboard");
  renderCommandDashboard(dash);

  const tasks = [dispatchRefreshHooks(opts)];
  if (state.activeTab === "activity") {
    tasks.push(loadTransactions(), loadEmailQueue());
  } else if (state.activeTab === "settings") {
    tasks.push(loadSettings());
  }
  if (state.activeDrawer === "reset-drawer-overlay") {
    tasks.push(loadWeeklyReset());
  }

  await Promise.allSettled(tasks);
  return dash;
}

function bindGlobalShellControls() {
  const themeBtn = $("btn-theme-toggle");
  if (themeBtn) themeBtn.addEventListener("click", toggleTheme);

  const tabBtns = Array.from(document.querySelectorAll(".tab[data-tab]"));
  tabBtns.forEach((btn, idx) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
    btn.addEventListener("keydown", (e) => {
      if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
        e.preventDefault();
        const dir = e.key === "ArrowRight" ? 1 : -1;
        const next = tabBtns[(idx + dir + tabBtns.length) % tabBtns.length];
        next.focus();
        switchTab(next.dataset.tab);
      }
    });
  });

  document.body.addEventListener("click", async (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    if (btn.dataset.gotoTab) {
      switchTab(btn.dataset.gotoTab);
      return;
    }
    if (btn.dataset.setMode) {
      await setIntelligenceMode(btn.dataset.setMode);
      return;
    }
    if (btn.dataset.setProvider) {
      await setPreferredProvider(btn.dataset.setProvider);
      loadLocalAiClis(false);
    }
  });
}

document.addEventListener("DOMContentLoaded", async () => {
  setRefreshAllImpl(performRefreshAll);

  initTheme();
  initTabIndicatorObserver();
  initCardSheen();
  initCommandPalette();
  bindGlobalShellControls();
  showBootSkeletons();

  await initDesktopBridge();

  initVaultAndShell();
  callModuleInit(chartsModule, "initCharts");
  callModuleInit(timelineModule, "initTimeline");
  callModuleInit(goalsDndModule, "initGoalsDnd");
  callModuleInit(portfolioModule, "initPortfolioAndCredit");
  callModuleInit(aiModule, "initAiCoach");

  await checkVaultBootStatus(async () => {
    try {
      await loadSettings();
      await performRefreshAll({ refreshAI: false });
      await loadEmailQueue();
    } catch (err) {
      if (err && err.status !== 401) {
        toast(err.message || "Failed to load dashboard.", true);
      }
    }
  });
});
