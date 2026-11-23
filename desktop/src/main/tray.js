"use strict";

const fs = require("fs");
const path = require("path");
const { Menu, Tray, nativeImage, nativeTheme } = require("electron");

const MODE_LABELS = {
  deterministic_only: "Quant Math (Local)",
  hybrid: "Hybrid (Quant + AI)",
  ai_standalone: "AI Standalone",
};

function formatDailySafe(snapshot) {
  if (Number.isFinite(snapshot.dailySafeCents)) {
    const dollars = snapshot.dailySafeCents / 100;
    const sign = dollars < 0 ? "-" : "";
    const abs = Math.abs(dollars).toLocaleString("en-US", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
    return `${sign}$${abs}`;
  }
  if (typeof snapshot.dailySafe === "number" && Number.isFinite(snapshot.dailySafe)) {
    const sign = snapshot.dailySafe < 0 ? "-" : "";
    const abs = Math.abs(snapshot.dailySafe).toLocaleString("en-US", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
    return `${sign}$${abs}`;
  }
  if (typeof snapshot.dailySafe === "string" && snapshot.dailySafe.trim()) {
    return snapshot.dailySafe.trim();
  }
  return null;
}

class TrayController {
  constructor(options) {
    this.iconsDir = options.iconsDir;
    this.callbacks = options.callbacks || {};
    this.tray = null;
    this.snapshot = {
      dailySafe: null,
      dailySafeCents: null,
      safeToSpendCents: null,
      daysUntilPayday: null,
      mode: "hybrid",
      theme: options.initialTheme === "light" ? "light" : "dark",
      shortcut: options.shortcut || "CommandOrControl+Shift+Space",
      locked: true,
    };
  }

  get isAvailable() {
    return Boolean(this.tray && !this.tray.isDestroyed());
  }

  _resolveTrayImage() {
    const preferDarkTaskbar =
      process.platform === "darwin" ? true : nativeTheme.shouldUseDarkColors || this.snapshot.theme === "dark";

    const candidates =
      process.platform === "darwin"
        ? ["trayTemplate.png", "tray-dark.png", "32x32.png", "icon.png"]
        : preferDarkTaskbar
          ? ["tray-dark.png", "32x32.png", "icon.png"]
          : ["tray-light.png", "tray-dark.png", "32x32.png", "icon.png"];

    for (const filename of candidates) {
      const fullPath = path.join(this.iconsDir, filename);
      if (fs.existsSync(fullPath)) {
        let img = nativeImage.createFromPath(fullPath);
        if (!img.isEmpty()) {
          if (process.platform === "darwin") {
            img = img.resize({ width: 18, height: 18, quality: "best" });
            if (filename.includes("Template")) {
              img.setTemplateImage(true);
            }
          } else if (process.platform === "win32") {
            img = img.resize({ width: 16, height: 16, quality: "best" });
          } else {
            img = img.resize({ width: 22, height: 22, quality: "best" });
          }
          return img;
        }
      }
    }

    // Minimal valid 16x16 transparent fallback so Tray construction never crashes before icons exist.
    return nativeImage.createEmpty();
  }

  init() {
    if (this.tray && !this.tray.isDestroyed()) return true;
    try {
      const icon = this._resolveTrayImage();
      this.tray = new Tray(icon);
      this.tray.on("click", () => {
        if (this.callbacks.onOpenApp) this.callbacks.onOpenApp();
      });
      this.tray.on("double-click", () => {
        if (this.callbacks.onOpenApp) this.callbacks.onOpenApp();
      });
      this._refreshMenuAndTooltip();
      return true;
    } catch {
      this.tray = null;
      return false;
    }
  }

  updateSnapshot(partial) {
    if (!partial || typeof partial !== "object") return;
    const prevTheme = this.snapshot.theme;
    this.snapshot = {
      ...this.snapshot,
      ...(partial.dailySafe !== undefined ? { dailySafe: partial.dailySafe } : {}),
      ...(partial.dailySafeCents !== undefined ? { dailySafeCents: partial.dailySafeCents } : {}),
      ...(partial.safeToSpendCents !== undefined ? { safeToSpendCents: partial.safeToSpendCents } : {}),
      ...(partial.daysUntilPayday !== undefined ? { daysUntilPayday: partial.daysUntilPayday } : {}),
      ...(partial.mode ? { mode: partial.mode } : {}),
      ...(partial.theme === "dark" || partial.theme === "light" ? { theme: partial.theme } : {}),
      ...(partial.shortcut ? { shortcut: partial.shortcut } : {}),
      ...(typeof partial.locked === "boolean" ? { locked: partial.locked } : {}),
    };

    if (this.isAvailable) {
      if (this.snapshot.theme !== prevTheme) {
        try {
          this.tray.setImage(this._resolveTrayImage());
        } catch {
          // Ignore tray image update errors on unsupported desktops.
        }
      }
      this._refreshMenuAndTooltip();
    }
  }

  _formatShortcutHint() {
    const raw = this.snapshot.shortcut || "CommandOrControl+Shift+Space";
    return raw.replace("CommandOrControl", process.platform === "darwin" ? "Cmd" : "Ctrl");
  }

  _refreshMenuAndTooltip() {
    if (!this.isAvailable) return;

    const safeStr = formatDailySafe(this.snapshot);
    const days = Number.isFinite(this.snapshot.daysUntilPayday) ? this.snapshot.daysUntilPayday : null;
    const isLocked = Boolean(this.snapshot.locked);
    const statusLabel = isLocked
      ? "Vault Locked — Open ChronoX to Unlock"
      : safeStr
        ? `Safe Today: ${safeStr}${days !== null ? `  (${days}d to payday)` : ""}`
        : "Safe Today: Syncing...";

    const tooltip = isLocked
      ? "ChronoX Finance — Vault Locked"
      : safeStr
        ? `ChronoX Finance — Safe Today: ${safeStr}${days !== null ? ` (${days}d until payday)` : ""}`
        : "ChronoX Finance — Industrial Personal Finance Vault";

    try {
      this.tray.setToolTip(tooltip);
    } catch {
      // Ignore on environments without tooltip support.
    }

    const activeMode = this.snapshot.mode || "hybrid";
    const nextTheme = this.snapshot.theme === "dark" ? "light" : "dark";

    const menu = Menu.buildFromTemplate([
      {
        label: statusLabel,
        enabled: false,
      },
      {
        label: `Mode: ${MODE_LABELS[activeMode] || activeMode}`,
        enabled: false,
      },
      { type: "separator" },
      {
        label: `Quick Capture HUD  (${this._formatShortcutHint()})`,
        click: () => this.callbacks.onToggleQuickCapture && this.callbacks.onToggleQuickCapture(),
      },
      {
        label: "Open ChronoX Finance",
        click: () => this.callbacks.onOpenApp && this.callbacks.onOpenApp(),
      },
      {
        label: "Sync Auto-Budget Now",
        click: () => this.callbacks.onTriggerSync && this.callbacks.onTriggerSync(),
      },
      { type: "separator" },
      {
        label: "Intelligence Mode",
        submenu: [
          {
            label: "Quant Math (Local Deterministic)",
            type: "radio",
            checked: activeMode === "deterministic_only",
            click: () => this.callbacks.onSetMode && this.callbacks.onSetMode("deterministic_only"),
          },
          {
            label: "Hybrid (Quant Math + AI)",
            type: "radio",
            checked: activeMode === "hybrid",
            click: () => this.callbacks.onSetMode && this.callbacks.onSetMode("hybrid"),
          },
          {
            label: "AI Standalone",
            type: "radio",
            checked: activeMode === "ai_standalone",
            click: () => this.callbacks.onSetMode && this.callbacks.onSetMode("ai_standalone"),
          },
        ],
      },
      {
        label: nextTheme === "light" ? "Switch to Light Steel" : "Switch to Dark Industrial",
        click: () => this.callbacks.onSetTheme && this.callbacks.onSetTheme(nextTheme),
      },
      { type: "separator" },
      {
        label: "Quit ChronoX Finance",
        click: () => this.callbacks.onQuit && this.callbacks.onQuit(),
      },
    ]);

    try {
      this.tray.setContextMenu(menu);
    } catch {
      // Non-fatal on headless/minimal tray implementations.
    }
  }

  destroy() {
    if (this.tray && !this.tray.isDestroyed()) {
      this.tray.destroy();
    }
    this.tray = null;
  }
}

module.exports = {
  TrayController,
};
