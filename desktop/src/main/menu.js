"use strict";

const { Menu, app } = require("electron");

/**
 * Build and apply the native first-party ChronoX Finance Application Menu.
 * Accelerators remain active even when the menu bar is hidden on frameless Linux/Windows.
 */
function buildApplicationMenu(options) {
  const isMac = process.platform === "darwin";
  const isDev = Boolean(options.isDev);
  const currentMode = options.mode || "hybrid";
  const currentTheme = options.theme === "light" ? "light" : "dark";
  const cb = options.callbacks || {};

  const template = [
    ...(isMac
      ? [
          {
            label: app.name,
            submenu: [
              { label: "About ChronoX Finance", click: () => cb.onShowAbout && cb.onShowAbout() },
              { type: "separator" },
              {
                label: "Preferences...",
                accelerator: "Command+,",
                click: () => cb.onNavigateTab && cb.onNavigateTab("settings"),
              },
              { type: "separator" },
              { role: "services" },
              { type: "separator" },
              { role: "hide" },
              { role: "hideOthers" },
              { role: "unhide" },
              { type: "separator" },
              {
                label: "Quit ChronoX Finance",
                accelerator: "Command+Q",
                click: () => cb.onQuit && cb.onQuit(),
              },
            ],
          },
        ]
      : []),

    {
      label: "File",
      submenu: [
        {
          label: "Quick Capture HUD",
          accelerator: "CommandOrControl+Shift+Space",
          click: () => cb.onToggleQuickCapture && cb.onToggleQuickCapture(),
        },
        {
          label: "Command Palette",
          accelerator: "CommandOrControl+K",
          click: () => cb.onOpenCommandPalette && cb.onOpenCommandPalette(),
        },
        {
          label: "Sync Auto-Budget Now",
          accelerator: "CommandOrControl+R",
          click: () => cb.onTriggerSync && cb.onTriggerSync(),
        },
        { type: "separator" },
        {
          label: "Open Local Data Folder",
          click: () => cb.onOpenDataDir && cb.onOpenDataDir(),
        },
        {
          label: "Backend Diagnostics Logs",
          click: () => cb.onOpenBackendLogs && cb.onOpenBackendLogs(),
        },
        { type: "separator" },
        isMac
          ? { role: "close" }
          : {
              label: "Quit ChronoX Finance",
              accelerator: "CommandOrControl+Q",
              click: () => cb.onQuit && cb.onQuit(),
            },
      ],
    },

    {
      label: "Edit",
      submenu: [
        { role: "undo" },
        { role: "redo" },
        { type: "separator" },
        { role: "cut" },
        { role: "copy" },
        { role: "paste" },
        ...(isMac
          ? [{ role: "pasteAndMatchStyle" }, { role: "delete" }, { role: "selectAll" }]
          : [{ role: "delete" }, { type: "separator" }, { role: "selectAll" }]),
      ],
    },

    {
      label: "View",
      submenu: [
        {
          label: currentTheme === "dark" ? "Switch to Light Steel" : "Switch to Dark Industrial",
          accelerator: "CommandOrControl+Shift+T",
          click: () => cb.onSetTheme && cb.onSetTheme(currentTheme === "dark" ? "light" : "dark"),
        },
        { type: "separator" },
        {
          label: "Reload Interface",
          accelerator: "CommandOrControl+Shift+R",
          click: () => cb.onReload && cb.onReload(),
        },
        { type: "separator" },
        { role: "resetZoom" },
        { role: "zoomIn" },
        { role: "zoomOut" },
        { type: "separator" },
        { role: "togglefullscreen" },
        ...(isDev
          ? [{ type: "separator" }, { role: "toggleDevTools" }]
          : []),
      ],
    },

    {
      label: "Navigate",
      submenu: [
        {
          label: "Command",
          accelerator: "CommandOrControl+1",
          click: () => cb.onNavigateTab && cb.onNavigateTab("today"),
        },
        {
          label: "Analytics & Forecast",
          accelerator: "CommandOrControl+2",
          click: () => cb.onNavigateTab && cb.onNavigateTab("analytics"),
        },
        {
          label: "Portfolio",
          accelerator: "CommandOrControl+3",
          click: () => cb.onNavigateTab && cb.onNavigateTab("portfolio"),
        },
        {
          label: "Credit & Debt",
          accelerator: "CommandOrControl+4",
          click: () => cb.onNavigateTab && cb.onNavigateTab("credit"),
        },
        {
          label: "Goals & Rules",
          accelerator: "CommandOrControl+5",
          click: () => cb.onNavigateTab && cb.onNavigateTab("goals"),
        },
        {
          label: "Activity & Bills",
          accelerator: "CommandOrControl+6",
          click: () => cb.onNavigateTab && cb.onNavigateTab("activity"),
        },
        {
          label: "AI Coach",
          accelerator: "CommandOrControl+7",
          click: () => cb.onNavigateTab && cb.onNavigateTab("coach"),
        },
        {
          label: "Vault & Connectors",
          accelerator: "CommandOrControl+8",
          click: () => cb.onNavigateTab && cb.onNavigateTab("settings"),
        },
      ],
    },

    {
      label: "Intelligence",
      submenu: [
        {
          label: "Quant Math (Local Deterministic)",
          type: "radio",
          checked: currentMode === "deterministic_only",
          click: () => cb.onSetMode && cb.onSetMode("deterministic_only"),
        },
        {
          label: "Hybrid (Quant Math + AI)",
          type: "radio",
          checked: currentMode === "hybrid",
          click: () => cb.onSetMode && cb.onSetMode("hybrid"),
        },
        {
          label: "AI Standalone",
          type: "radio",
          checked: currentMode === "ai_standalone",
          click: () => cb.onSetMode && cb.onSetMode("ai_standalone"),
        },
      ],
    },

    {
      label: "Window",
      submenu: [
        { role: "minimize" },
        ...(isMac ? [{ role: "zoom" }, { type: "separator" }, { role: "front" }] : [{ role: "close" }]),
      ],
    },

    {
      label: "Help",
      submenu: [
        {
          label: "ChronoX Repository & Docs",
          click: () => cb.onOpenDocs && cb.onOpenDocs(),
        },
        {
          label: "Backend Diagnostics Logs",
          click: () => cb.onOpenBackendLogs && cb.onOpenBackendLogs(),
        },
        { type: "separator" },
        {
          label: "About ChronoX Finance",
          click: () => cb.onShowAbout && cb.onShowAbout(),
        },
      ],
    },
  ];

  const menu = Menu.buildFromTemplate(template);
  Menu.setApplicationMenu(menu);
  return menu;
}

module.exports = {
  buildApplicationMenu,
};
