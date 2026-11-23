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

function detectSurface() {
  const argv = Array.isArray(process.argv) ? process.argv : [];
  const arg = argv.find((item) => typeof item === "string" && item.startsWith("--chronox-surface="));
  if (arg) {
    return arg.slice("--chronox-surface=".length);
  }
  const pathname = typeof window !== "undefined" && window.location ? String(window.location.pathname || "") : "";
  if (pathname.endsWith("splash.html")) return "splash";
  if (pathname.endsWith("quick-capture.html")) return "hud";
  return "";
}

const surface = detectSurface();

if (surface === "splash") {
  const chronoxSplash = Object.freeze({
    onStatus: (cb) => subscribe("splash:status", cb),
    getInitialState: () => ipcRenderer.invoke("splash:getState"),
    retry: () => ipcRenderer.invoke("splash:retry"),
    getBackendLogs: () => ipcRenderer.invoke("desktop:getBackendLogs"),
    quit: () => ipcRenderer.invoke("splash:quit"),
  });
  contextBridge.exposeInMainWorld("chronoxSplash", chronoxSplash);
} else if (surface === "hud") {
  const chronoxHud = Object.freeze({
    getState: () => ipcRenderer.invoke("hud:getState"),
    submitQuickAdd: (text) => ipcRenderer.invoke("hud:submitQuickAdd", text),
    hide: () => ipcRenderer.invoke("hud:hide"),
    openMainApp: (tab) => ipcRenderer.invoke("hud:openMainApp", tab),
    onShow: (cb) => subscribe("hud:shown", cb),
    onThemeChange: (cb) => subscribe("hud:theme-changed", cb),
  });
  contextBridge.exposeInMainWorld("chronoxHud", chronoxHud);
}
