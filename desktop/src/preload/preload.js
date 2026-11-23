"use strict";

const { contextBridge, ipcRenderer } = require("electron");

function subscribe(channel, callback) {
  if (typeof callback !== "function") {
    return () => {};
  }
  const listener = (_event, payload) => {
    callback(payload);
  };
  ipcRenderer.on(channel, listener);
  return () => {
    ipcRenderer.removeListener(channel, listener);
  };
}

/**
 * Strictly scoped preload bridge for the main ChronoX Finance application window.
 * Zero Node or raw Electron primitives are exposed to the renderer.
 */
const chronoxDesktop = Object.freeze({
  isDesktop: true,
  platform: process.platform,

  // Window chrome controls
  minimize: () => ipcRenderer.invoke("window:minimize"),
  maximizeToggle: () => ipcRenderer.invoke("window:maximizeToggle"),
  close: () => ipcRenderer.invoke("window:close"),
  isMaximized: () => ipcRenderer.invoke("window:isMaximized"),
  setThemeOverlay: (theme) => ipcRenderer.invoke("window:setThemeOverlay", theme),

  // Desktop info, preferences & diagnostics
  getInfo: () => ipcRenderer.invoke("desktop:getInfo"),
  setPrefs: (partialPrefs) => ipcRenderer.invoke("desktop:setPrefs", partialPrefs),
  openDataDir: () => ipcRenderer.invoke("desktop:openDataDir"),
  getBackendLogs: () => ipcRenderer.invoke("desktop:getBackendLogs"),
  toggleQuickCapture: () => ipcRenderer.invoke("desktop:toggleQuickCapture"),
  notify: (payload) => ipcRenderer.invoke("desktop:notify", payload),
  openExternal: (url) => ipcRenderer.invoke("desktop:openExternal", url),

  // Native OS file dialogs
  openStatementDialog: () => ipcRenderer.invoke("dialog:openStatement"),
  saveFileDialog: (options) => ipcRenderer.invoke("dialog:saveFile", options),

  // System Tray live snapshot sync
  updateTraySnapshot: (snapshot) => ipcRenderer.invoke("tray:updateSnapshot", snapshot),

  // Main -> Renderer push events (each returns an unsubscribe disposer)
  onRefresh: (cb) => subscribe("chronox:refresh", cb),
  onNavigateTab: (cb) => subscribe("chronox:navigate-tab", cb),
  onSetTheme: (cb) => subscribe("chronox:set-theme", cb),
  onSetMode: (cb) => subscribe("chronox:set-mode", cb),
  onOpenCommandPalette: (cb) => subscribe("chronox:open-command-palette", cb),
  onWindowStateChange: (cb) => subscribe("window:state-changed", cb),
  onTriggerSync: (cb) => subscribe("chronox:trigger-sync", cb),
  onOpenBackendLogs: (cb) => subscribe("chronox:open-backend-logs", cb),
});

contextBridge.exposeInMainWorld("chronoxDesktop", chronoxDesktop);
