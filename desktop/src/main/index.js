"use strict";

const fs = require("fs");
const http = require("http");
const os = require("os");
const path = require("path");
const {
  app,
  BrowserWindow,
  Notification,
  dialog,
  globalShortcut,
  ipcMain,
  nativeImage,
  screen,
  session,
  shell,
} = require("electron");

const { BackendManager } = require("./backend-manager");
const { buildApplicationMenu } = require("./menu");
const { TrayController } = require("./tray");
const { WatchFolderController } = require("./watch-folder");
const { loadWindowState, trackWindowState } = require("./window-state");

// ---------------------------------------------------------------------------
// Constants & Theme Palette (aligned with chronox_finance/web/styles.css)
// ---------------------------------------------------------------------------

const APP_NAME = "ChronoX Finance";
const DOCS_URL = "https://github.com/Bit-Loop/CHronoX";
const MAX_STATEMENT_BYTES = 25 * 1024 * 1024; // 25 MB strict safety cap
const MAX_SAVE_BYTES = 25 * 1024 * 1024;
const PRIMARY_SHORTCUT = "CommandOrControl+Shift+Space";
const FALLBACK_SHORTCUT = "Alt+Shift+Space";
const VALID_TABS = new Set([
  "today",
  "analytics",
  "portfolio",
  "credit",
  "goals",
  "activity",
  "coach",
  "settings",
]);
const VALID_MODES = new Set(["deterministic_only", "hybrid", "ai_standalone"]);

const THEME_PALETTE = {
  dark: {
    backgroundColor: "#080a0d",
    overlayColor: "#080a0d",
    symbolColor: "#eef2f6",
  },
  light: {
    backgroundColor: "#e9edf2",
    overlayColor: "#e2e7ee",
    symbolColor: "#0b1118",
  },
};

const DEFAULT_PREFS = {
  closeToTray: true,
  launchAtLogin: false,
  theme: "dark",
  quickCaptureShortcut: PRIMARY_SHORTCUT,
  watchFolderEnabled: false,
  watchFolderPath: "",
  watchFolderInstitution: "auto",
  watchFolderSkipPending: true,
  watchFolderSyncBalance: false,
};

const DESKTOP_ROOT = path.resolve(__dirname, "..", "..");
const REPO_ROOT = path.resolve(DESKTOP_ROOT, "..");
const PRELOAD_MAIN_PATH = path.join(DESKTOP_ROOT, "src", "preload", "preload.js");
const PRELOAD_HUD_PATH = path.join(DESKTOP_ROOT, "src", "preload", "hud-preload.js");
const SPLASH_HTML_PATH = path.join(DESKTOP_ROOT, "src", "splash", "splash.html");
const HUD_HTML_PATH = path.join(DESKTOP_ROOT, "src", "quick-capture", "quick-capture.html");

function resolveIconsDir() {
  const candidates = [];
  if (process.resourcesPath) {
    candidates.push(path.join(process.resourcesPath, "icons"));
    candidates.push(path.join(process.resourcesPath, "build", "icons"));
  }
  candidates.push(path.join(DESKTOP_ROOT, "build", "icons"));
  for (const dir of candidates) {
    if (fs.existsSync(dir)) return dir;
  }
  return path.join(DESKTOP_ROOT, "build", "icons");
}

// ---------------------------------------------------------------------------
// CLI Argument Parsing
// ---------------------------------------------------------------------------

function parseCliFlags(argv) {
  const args = Array.isArray(argv) ? argv : [];
  let dbPath = null;
  let dev = false;
  let quickCapture = false;

  for (let i = 0; i < args.length; i += 1) {
    const arg = String(args[i] || "");
    if (arg === "--dev") {
      dev = true;
    } else if (arg === "--quick-capture") {
      quickCapture = true;
    } else if (arg === "--db" && i + 1 < args.length) {
      dbPath = path.resolve(String(args[i + 1]));
      i += 1;
    } else if (arg.startsWith("--db=")) {
      const val = arg.slice("--db=".length).trim();
      if (val) dbPath = path.resolve(val);
    }
  }

  return { dbPath, dev, quickCapture };
}

const cliFlags = parseCliFlags(process.argv.slice(1));

// Set consistent application name & AppUserModelID for Windows notifications/taskbar
app.setName(APP_NAME);
if (process.platform === "win32") {
  app.setAppUserModelId("com.chronox.finance");
}

// ---------------------------------------------------------------------------
// Single-Instance Lock
// ---------------------------------------------------------------------------

const gotSingleInstanceLock = app.requestSingleInstanceLock();
if (!gotSingleInstanceLock) {
  app.quit();
}

// ---------------------------------------------------------------------------
// Runtime State
// ---------------------------------------------------------------------------

let mainWindow = null;
let splashWindow = null;
let hudWindow = null;
let backendManager = null;
let trayController = null;
let watchFolderController = null;
let desktopPrefs = { ...DEFAULT_PREFS };
let activeShortcut = PRIMARY_SHORTCUT;
let shortcutRegistered = false;
let isQuitting = false;
let isCleaningUpBeforeQuit = false;
let hudLastShownAt = 0;
let liveSnapshot = {
  dailySafe: null,
  dailySafeCents: null,
  safeToSpendCents: null,
  daysUntilPayday: null,
  mode: "hybrid",
  theme: "dark",
  locked: true,
};
let cachedCsrfToken = "";

// ---------------------------------------------------------------------------
// Preferences & Login Autostart Persistence
// ---------------------------------------------------------------------------

function getPrefsPath() {
  return path.join(app.getPath("userData"), "desktop-prefs.json");
}

function sanitizePrefs(raw) {
  const input = raw && typeof raw === "object" ? raw : {};
  const theme = input.theme === "light" ? "light" : "dark";
  const shortcut =
    typeof input.quickCaptureShortcut === "string" &&
    input.quickCaptureShortcut.trim().length > 0 &&
    input.quickCaptureShortcut.trim().length <= 64
      ? input.quickCaptureShortcut.trim()
      : PRIMARY_SHORTCUT;

  const watchInstitution = ["auto", "generic", "navy_federal", "plaid"].includes(input.watchFolderInstitution)
    ? input.watchFolderInstitution
    : "auto";
  const watchPath =
    typeof input.watchFolderPath === "string" && input.watchFolderPath.trim().length <= 512
      ? input.watchFolderPath.trim()
      : "";

  return {
    closeToTray: typeof input.closeToTray === "boolean" ? input.closeToTray : DEFAULT_PREFS.closeToTray,
    launchAtLogin: typeof input.launchAtLogin === "boolean" ? input.launchAtLogin : DEFAULT_PREFS.launchAtLogin,
    theme,
    quickCaptureShortcut: shortcut,
    watchFolderEnabled: typeof input.watchFolderEnabled === "boolean" ? input.watchFolderEnabled : false,
    watchFolderPath: watchPath,
    watchFolderInstitution: watchInstitution,
    watchFolderSkipPending: typeof input.watchFolderSkipPending === "boolean" ? input.watchFolderSkipPending : true,
    watchFolderSyncBalance: typeof input.watchFolderSyncBalance === "boolean" ? input.watchFolderSyncBalance : false,
  };
}

function loadPrefs() {
  const filePath = getPrefsPath();
  try {
    if (fs.existsSync(filePath)) {
      const parsed = JSON.parse(fs.readFileSync(filePath, "utf8"));
      desktopPrefs = sanitizePrefs(parsed);
      liveSnapshot.theme = desktopPrefs.theme;
      return desktopPrefs;
    }
  } catch {
    // Fall back to defaults cleanly if corrupted.
  }
  desktopPrefs = { ...DEFAULT_PREFS };
  liveSnapshot.theme = desktopPrefs.theme;
  return desktopPrefs;
}

function savePrefs(nextPrefs) {
  desktopPrefs = sanitizePrefs(nextPrefs);
  liveSnapshot.theme = desktopPrefs.theme;
  const filePath = getPrefsPath();
  try {
    fs.mkdirSync(path.dirname(filePath), { recursive: true });
    const tmpPath = `${filePath}.tmp`;
    fs.writeFileSync(tmpPath, JSON.stringify(desktopPrefs, null, 2), "utf8");
    fs.renameSync(tmpPath, filePath);
  } catch {
    // Non-fatal persistence error.
  }
  return desktopPrefs;
}

function applyLaunchAtLogin(enabled) {
  try {
    if (process.platform === "win32" || process.platform === "darwin") {
      app.setLoginItemSettings({
        openAtLogin: Boolean(enabled),
        ...(process.platform === "win32" ? { path: process.execPath } : {}),
      });
      return true;
    }

    if (process.platform === "linux") {
      const configHome = process.env.XDG_CONFIG_HOME || path.join(os.homedir(), ".config");
      const autostartDir = path.join(configHome, "autostart");
      const desktopEntryPath = path.join(autostartDir, "chronox-finance.desktop");

      if (!enabled) {
        if (fs.existsSync(desktopEntryPath)) {
          fs.unlinkSync(desktopEntryPath);
        }
        return true;
      }

      fs.mkdirSync(autostartDir, { recursive: true });
      const execCmd = app.isPackaged
        ? `"${process.execPath}"`
        : `"${process.execPath}" "${DESKTOP_ROOT}"`;
      const iconPath = path.join(resolveIconsDir(), "icon.png");
      const useThemeIconName = app.isPackaged || iconPath.includes(".asar") || !fs.existsSync(iconPath);
      const entry = [
        "[Desktop Entry]",
        "Type=Application",
        "Version=1.0",
        "Name=ChronoX Finance",
        "Comment=Calm ADHD Personal Finance & Self-Updating Auto-Budget",
        `Exec=${execCmd}`,
        useThemeIconName ? "Icon=chronox-finance" : `Icon=${iconPath}`,
        "Terminal=false",
        "Categories=Finance;Office;",
        "StartupWMClass=chronox-finance",
        "X-GNOME-Autostart-enabled=true",
        "",
      ].join("\n");
      fs.writeFileSync(desktopEntryPath, entry, { encoding: "utf8", mode: 0o644 });
      return true;
    }
  } catch {
    return false;
  }
  return false;
}

// ---------------------------------------------------------------------------
// Security Helpers (URL Allow-List, External HTTPS Guard, IPC Sender Check)
// ---------------------------------------------------------------------------

function isAllowedBackendUrl(targetUrl) {
  if (!backendManager || !backendManager.baseUrl || typeof targetUrl !== "string") {
    return false;
  }
  try {
    const parsed = new URL(targetUrl);
    const expected = new URL(backendManager.baseUrl);
    return parsed.protocol === "http:" && parsed.hostname === "127.0.0.1" && parsed.port === expected.port;
  } catch {
    return false;
  }
}

async function openExternalHttps(rawUrl) {
  if (typeof rawUrl !== "string") {
    return { ok: false, error: "Invalid external URL." };
  }
  try {
    const parsed = new URL(rawUrl);
    if (parsed.protocol !== "https:") {
      return { ok: false, error: "Only HTTPS external links are permitted." };
    }
    await shell.openExternal(parsed.toString());
    return { ok: true };
  } catch (err) {
    return {
      ok: false,
      error: err && err.message ? String(err.message) : String(err || "Could not open browser."),
    };
  }
}

function getAppIcon() {
  const iconsDir = resolveIconsDir();
  const candidates =
    process.platform === "win32"
      ? ["icon.ico", "256x256.png", "icon.png"]
      : ["256x256.png", "icon.png", "512x512.png"];
  for (const name of candidates) {
    const p = path.join(iconsDir, name);
    if (fs.existsSync(p)) {
      const img = nativeImage.createFromPath(p);
      if (!img.isEmpty()) return img;
    }
  }
  return undefined;
}

function isFromWindow(event, win) {
  return Boolean(win && !win.isDestroyed() && event && event.sender === win.webContents);
}

function assertMainSender(event) {
  if (!isFromWindow(event, mainWindow)) {
    throw new Error("Unauthorized IPC sender.");
  }
  const frameUrl = event.senderFrame ? event.senderFrame.url : "";
  if (frameUrl && !isAllowedBackendUrl(frameUrl)) {
    throw new Error("Unauthorized IPC origin.");
  }
}

function assertSplashSender(event) {
  if (!isFromWindow(event, splashWindow)) {
    throw new Error("Unauthorized splash IPC sender.");
  }
}

function assertHudSender(event) {
  if (!isFromWindow(event, hudWindow)) {
    throw new Error("Unauthorized HUD IPC sender.");
  }
}

function assertKnownChronoxSender(event) {
  if (
    isFromWindow(event, mainWindow) ||
    isFromWindow(event, splashWindow) ||
    isFromWindow(event, hudWindow)
  ) {
    return;
  }
  throw new Error("Unauthorized IPC sender.");
}

// ---------------------------------------------------------------------------
// Authenticated Loopback Backend HTTP Client (for HUD & Tray Prefetch)
// ---------------------------------------------------------------------------

async function backendRequest(method, apiPath, bodyObj = null) {
  if (!backendManager || !backendManager.baseUrl || !backendManager.port) {
    throw new Error("ChronoX local engine is not ready yet.");
  }

  let cookieHeader = "";
  try {
    const cookies = await session.defaultSession.cookies.get({ url: backendManager.baseUrl });
    if (Array.isArray(cookies) && cookies.length > 0) {
      cookieHeader = cookies.map((c) => `${c.name}=${c.value}`).join("; ");
    }
  } catch {
    cookieHeader = "";
  }

  const payload = bodyObj !== null ? JSON.stringify(bodyObj) : null;
  const upperMethod = String(method || "GET").toUpperCase();
  const headers = {
    Accept: "application/json",
    Origin: backendManager.baseUrl,
  };
  if (cookieHeader) {
    headers.Cookie = cookieHeader;
  }
  if (["POST", "PUT", "PATCH", "DELETE"].includes(upperMethod)) {
    if (!cachedCsrfToken && !apiPath.startsWith("/api/v1/auth/status")) {
      await fetchVaultStatus();
    }
    if (cachedCsrfToken) {
      headers["X-ChronoX-CSRF"] = cachedCsrfToken;
    }
  }
  if (payload !== null) {
    headers["Content-Type"] = "application/json";
    headers["Content-Length"] = Buffer.byteLength(payload, "utf8");
  }

  const reqTimeoutMs = apiPath.startsWith("/api/v1/ai/") ? 28000 : 8000;

  return new Promise((resolve, reject) => {
    const req = http.request(
      {
        hostname: "127.0.0.1",
        port: backendManager.port,
        path: apiPath,
        method,
        headers,
        timeout: reqTimeoutMs,
      },
      (res) => {
        let raw = "";
        res.setEncoding("utf8");
        res.on("data", (chunk) => {
          if (raw.length < 512 * 1024) raw += chunk;
        });
        res.on("end", () => {
          let data = null;
          try {
            data = raw ? JSON.parse(raw) : null;
          } catch {
            data = { detail: raw };
          }
          resolve({ status: res.statusCode || 500, data });
        });
      }
    );

    req.on("timeout", () => {
      req.destroy(new Error("Local backend request timed out."));
    });
    req.on("error", reject);
    if (payload !== null) req.write(payload);
    req.end();
  });
}

async function fetchVaultStatus() {
  try {
    const res = await backendRequest("GET", "/api/v1/auth/status");
    if (res.status === 200 && res.data && typeof res.data === "object") {
      if (typeof res.data.csrf_token === "string" && res.data.csrf_token.trim()) {
        cachedCsrfToken = res.data.csrf_token.trim();
      }
      const unlocked = Boolean(res.data.unlocked && res.data.authenticated);
      return {
        unlocked,
        authenticated: Boolean(res.data.authenticated),
        state: res.data.state || (unlocked ? "unlocked" : "locked"),
        csrfToken: cachedCsrfToken,
      };
    }
  } catch {
    // Backend unavailable or locked
  }
  return { unlocked: false, authenticated: false, state: "locked", csrfToken: cachedCsrfToken };
}

async function fetchLiveDashboardSnapshot() {
  try {
    const vault = await fetchVaultStatus();
    if (!vault.unlocked) {
      applySnapshotUpdate({ locked: true, dailySafe: null, dailySafeCents: null });
      return null;
    }
    const res = await backendRequest("GET", "/api/v1/dashboard");
    if (res.status === 401) {
      applySnapshotUpdate({ locked: true, dailySafe: null, dailySafeCents: null });
      return null;
    }
    if (res.status !== 200 || !res.data || typeof res.data !== "object") {
      return null;
    }
    const safe = res.data.safe_to_spend || {};
    const next = {
      locked: false,
      dailySafeCents: Number.isFinite(safe.daily_safe_to_spend_cents)
        ? safe.daily_safe_to_spend_cents
        : liveSnapshot.dailySafeCents,
      safeToSpendCents: Number.isFinite(safe.safe_to_spend_cents)
        ? safe.safe_to_spend_cents
        : liveSnapshot.safeToSpendCents,
      daysUntilPayday: Number.isFinite(safe.days_until_payday)
        ? safe.days_until_payday
        : liveSnapshot.daysUntilPayday,
      mode: VALID_MODES.has(res.data.intelligence_mode) ? res.data.intelligence_mode : liveSnapshot.mode,
    };
    applySnapshotUpdate(next);
    return next;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Theme, Snapshot & Menu Synchronization
// ---------------------------------------------------------------------------

function applyWindowThemeOverlay(theme) {
  const normalized = theme === "light" ? "light" : "dark";
  const palette = THEME_PALETTE[normalized];
  if (mainWindow && !mainWindow.isDestroyed()) {
    try {
      mainWindow.setBackgroundColor(palette.backgroundColor);
    } catch {
      // Ignore if unsupported.
    }
    if (process.platform === "win32" && typeof mainWindow.setTitleBarOverlay === "function") {
      try {
        mainWindow.setTitleBarOverlay({
          color: palette.overlayColor,
          symbolColor: palette.symbolColor,
          height: 38,
        });
      } catch {
        // Ignore on older Windows builds without titleBarOverlay support.
      }
    }
  }
  if (hudWindow && !hudWindow.isDestroyed()) {
    hudWindow.webContents.send("hud:theme-changed", normalized);
  }
}

function refreshApplicationMenu() {
  buildApplicationMenu({
    isDev: cliFlags.dev || !app.isPackaged,
    mode: liveSnapshot.mode,
    theme: desktopPrefs.theme,
    callbacks: {
      onToggleQuickCapture: () => toggleQuickCaptureHud(),
      onOpenCommandPalette: () => {
        showMainWindow();
        sendToMain("chronox:open-command-palette");
      },
      onTriggerSync: () => {
        showMainWindow();
        sendToMain("chronox:trigger-sync");
      },
      onOpenDataDir: () => {
        shell.openPath(app.getPath("userData")).catch(() => {});
      },
      onOpenBackendLogs: () => {
        showMainWindow();
        sendToMain("chronox:open-backend-logs");
      },
      onNavigateTab: (tab) => {
        if (!VALID_TABS.has(tab)) return;
        showMainWindow();
        sendToMain("chronox:navigate-tab", tab);
      },
      onSetTheme: (theme) => {
        const clean = theme === "light" ? "light" : "dark";
        savePrefs({ ...desktopPrefs, theme: clean });
        applyWindowThemeOverlay(clean);
        if (trayController) trayController.updateSnapshot({ theme: clean });
        sendToMain("chronox:set-theme", clean);
        refreshApplicationMenu();
      },
      onSetMode: (mode) => {
        if (!VALID_MODES.has(mode)) return;
        liveSnapshot.mode = mode;
        if (trayController) trayController.updateSnapshot({ mode });
        sendToMain("chronox:set-mode", mode);
        refreshApplicationMenu();
      },
      onReload: () => {
        if (mainWindow && !mainWindow.isDestroyed()) {
          mainWindow.webContents.reload();
        }
      },
      onOpenDocs: () => openExternalHttps(DOCS_URL),
      onShowAbout: () => showAboutDialog(),
      onQuit: () => requestFullQuit(),
    },
  });
}

function applySnapshotUpdate(partial) {
  if (!partial || typeof partial !== "object") return;
  const prevMode = liveSnapshot.mode;
  const prevTheme = desktopPrefs.theme;

  if (partial.dailySafe !== undefined) liveSnapshot.dailySafe = partial.dailySafe;
  if (Number.isFinite(partial.dailySafeCents) || partial.dailySafeCents === null) {
    liveSnapshot.dailySafeCents = partial.dailySafeCents;
  }
  if (Number.isFinite(partial.safeToSpendCents)) liveSnapshot.safeToSpendCents = partial.safeToSpendCents;
  if (Number.isFinite(partial.daysUntilPayday)) liveSnapshot.daysUntilPayday = partial.daysUntilPayday;
  if (VALID_MODES.has(partial.mode)) liveSnapshot.mode = partial.mode;
  if (typeof partial.locked === "boolean") liveSnapshot.locked = partial.locked;

  if (partial.theme === "dark" || partial.theme === "light") {
    liveSnapshot.theme = partial.theme;
    if (desktopPrefs.theme !== partial.theme) {
      savePrefs({ ...desktopPrefs, theme: partial.theme });
      applyWindowThemeOverlay(partial.theme);
    }
  }

  if (trayController) {
    trayController.updateSnapshot({
      ...liveSnapshot,
      shortcut: activeShortcut,
    });
  }

  if (liveSnapshot.mode !== prevMode || desktopPrefs.theme !== prevTheme) {
    refreshApplicationMenu();
  }
}

function sendToMain(channel, payload) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send(channel, payload);
  }
}

function showMainWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  if (mainWindow.isMinimized()) {
    mainWindow.restore();
  }
  if (!mainWindow.isVisible()) {
    mainWindow.show();
  }
  mainWindow.focus();
}

function showAboutDialog() {
  const detail = [
    `Version: ${app.getVersion()}`,
    `Electron: ${process.versions.electron}  ·  Chromium: ${process.versions.chrome}`,
    `Platform: ${process.platform} (${process.arch})`,
    `Local Vault: ${backendManager ? backendManager.dbPath : app.getPath("userData")}`,
  ].join("\n");

  dialog
    .showMessageBox(mainWindow && !mainWindow.isDestroyed() ? mainWindow : undefined, {
      type: "info",
      title: `About ${APP_NAME}`,
      message: APP_NAME,
      detail,
      buttons: ["OK"],
    })
    .catch(() => {});
}

// ---------------------------------------------------------------------------
// Global Hardened WebContents & Permission Policy
// ---------------------------------------------------------------------------

app.on("web-contents-created", (_event, contents) => {
  contents.on("will-attach-webview", (event) => {
    event.preventDefault();
  });

  contents.setWindowOpenHandler(({ url }) => {
    openExternalHttps(url);
    return { action: "deny" };
  });

  contents.on("will-navigate", (event, targetUrl) => {
    if (mainWindow && !mainWindow.isDestroyed() && contents === mainWindow.webContents) {
      if (isAllowedBackendUrl(targetUrl)) {
        return;
      }
      event.preventDefault();
      openExternalHttps(targetUrl);
      return;
    }
    // Splash and HUD windows must never navigate away from their local HTML surface.
    event.preventDefault();
  });
});

function configureSessionSecurity() {
  const ses = session.defaultSession;
  const ALLOWED_PERMISSIONS = new Set(["notifications", "clipboard-sanitized-write"]);

  const isTrustedChronoxContents = (webContents, originUrl) => {
    if (mainWindow && !mainWindow.isDestroyed() && webContents === mainWindow.webContents) {
      return isAllowedBackendUrl(originUrl || webContents.getURL());
    }
    if (splashWindow && !splashWindow.isDestroyed() && webContents === splashWindow.webContents) {
      return true;
    }
    return false;
  };

  ses.setPermissionRequestHandler((webContents, permission, callback) => {
    const url = webContents ? webContents.getURL() : "";
    const allowed =
      ALLOWED_PERMISSIONS.has(permission) &&
      (isTrustedChronoxContents(webContents, url) &&
        (permission !== "notifications" || isAllowedBackendUrl(url)));
    callback(allowed);
  });

  ses.setPermissionCheckHandler((webContents, permission, requestingOrigin) => {
    return (
      ALLOWED_PERMISSIONS.has(permission) &&
      (isAllowedBackendUrl(requestingOrigin) ||
        (permission === "clipboard-sanitized-write" &&
          splashWindow &&
          !splashWindow.isDestroyed() &&
          webContents === splashWindow.webContents))
    );
  });
}

// ---------------------------------------------------------------------------
// Splash Bootloader Window
// ---------------------------------------------------------------------------

function createSplashWindow() {
  if (splashWindow && !splashWindow.isDestroyed()) {
    return splashWindow;
  }

  splashWindow = new BrowserWindow({
    width: 420,
    height: 260,
    frame: false,
    transparent: true,
    resizable: false,
    movable: true,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    center: true,
    show: false,
    backgroundColor: "#00000000",
    icon: getAppIcon(),
    webPreferences: {
      preload: PRELOAD_HUD_PATH,
      additionalArguments: ["--chronox-surface=splash"],
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
      webviewTag: false,
      navigateOnDragDrop: false,
      spellcheck: false,
    },
  });

  splashWindow.once("ready-to-show", () => {
    if (splashWindow && !splashWindow.isDestroyed()) {
      splashWindow.show();
    }
  });

  splashWindow.on("closed", () => {
    splashWindow = null;
  });

  if (fs.existsSync(SPLASH_HTML_PATH)) {
    splashWindow.loadFile(SPLASH_HTML_PATH).catch(() => {});
  } else {
    // Graceful minimal fallback if splash surface is not yet generated.
    splashWindow
      .loadURL(
        `data:text/html;charset=utf-8,${encodeURIComponent(
          "<!doctype html><html><body style='margin:0;background:#070b12;color:#e6edf7;font-family:system-ui,sans-serif;display:grid;place-items:center;height:100vh'><div>ChronoX Finance — Starting local vault...</div></body></html>"
        )}`
      )
      .catch(() => {});
  }

  return splashWindow;
}

function sendSplashStatus(status) {
  if (splashWindow && !splashWindow.isDestroyed()) {
    splashWindow.webContents.send("splash:status", {
      ...status,
      theme: desktopPrefs.theme,
    });
  }
}

function closeSplashWindow() {
  if (splashWindow && !splashWindow.isDestroyed()) {
    splashWindow.close();
  }
  splashWindow = null;
}

// ---------------------------------------------------------------------------
// Main Application Window (First-Party Frameless Chrome)
// ---------------------------------------------------------------------------

function createMainWindow() {
  const { state: bounds, filePath: stateFilePath, minWidth, minHeight } = loadWindowState(
    app.getPath("userData")
  );
  const palette = THEME_PALETTE[desktopPrefs.theme] || THEME_PALETTE.dark;

  const platformChromeOptions =
    process.platform === "win32"
      ? {
          titleBarStyle: "hidden",
          titleBarOverlay: {
            color: palette.overlayColor,
            symbolColor: palette.symbolColor,
            height: 38,
          },
          autoHideMenuBar: true,
        }
      : process.platform === "darwin"
        ? {
            titleBarStyle: "hiddenInset",
            trafficLightPosition: { x: 14, y: 12 },
          }
        : {
            frame: false,
            autoHideMenuBar: true,
          };

  mainWindow = new BrowserWindow({
    x: bounds.x,
    y: bounds.y,
    width: bounds.width,
    height: bounds.height,
    minWidth,
    minHeight,
    title: APP_NAME,
    show: false,
    backgroundColor: palette.backgroundColor,
    icon: getAppIcon(),
    ...platformChromeOptions,
    webPreferences: {
      preload: PRELOAD_MAIN_PATH,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
      webviewTag: false,
      navigateOnDragDrop: false,
      spellcheck: true,
    },
  });

  trackWindowState(mainWindow, stateFilePath, bounds);

  const emitWindowState = () => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send("window:state-changed", {
        maximized: mainWindow.isMaximized(),
      });
    }
  };
  mainWindow.on("maximize", emitWindowState);
  mainWindow.on("unmaximize", emitWindowState);

  mainWindow.on("close", (event) => {
    if (!isQuitting && desktopPrefs.closeToTray && trayController && trayController.isAvailable) {
      event.preventDefault();
      mainWindow.hide();
    }
  });

  mainWindow.on("closed", () => {
    mainWindow = null;
  });

  let revealed = false;
  const revealMain = () => {
    if (revealed || !mainWindow || mainWindow.isDestroyed()) return;
    revealed = true;
    if (bounds.isMaximized) {
      mainWindow.maximize();
    }
    mainWindow.show();
    mainWindow.focus();
    setTimeout(closeSplashWindow, 120);
  };

  mainWindow.once("ready-to-show", revealMain);
  mainWindow.webContents.once("did-finish-load", () => {
    // Safety net for headless Linux environments where ready-to-show may lag.
    setTimeout(revealMain, 450);
  });

  mainWindow.loadURL(`${backendManager.baseUrl}/`).catch(() => {});
  return mainWindow;
}

// ---------------------------------------------------------------------------
// ADHD Global Quick-Capture Floating HUD
// ---------------------------------------------------------------------------

function positionHudWindow(win) {
  const cursor = screen.getCursorScreenPoint();
  const display = screen.getDisplayNearestPoint(cursor) || screen.getPrimaryDisplay();
  const area = display ? display.workArea : { x: 0, y: 0, width: 1440, height: 900 };
  const [width, height] = win.getSize();
  const x = Math.round(area.x + Math.max(0, (area.width - width) / 2));
  const y = Math.round(area.y + Math.max(48, Math.min(area.height - height - 24, area.height * 0.17)));
  win.setPosition(x, y, false);
}

function ensureHudWindow() {
  if (hudWindow && !hudWindow.isDestroyed()) {
    return hudWindow;
  }

  hudWindow = new BrowserWindow({
    width: 580,
    height: 236,
    frame: false,
    transparent: true,
    resizable: false,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    show: false,
    backgroundColor: "#00000000",
    icon: getAppIcon(),
    webPreferences: {
      preload: PRELOAD_HUD_PATH,
      additionalArguments: ["--chronox-surface=hud"],
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
      webviewTag: false,
      navigateOnDragDrop: false,
      spellcheck: true,
    },
  });

  if (process.platform === "darwin") {
    hudWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  }

  hudWindow.on("blur", () => {
    // Guard against immediate blur flicker right after showing on Linux WMs.
    if (Date.now() - hudLastShownAt < 220) return;
    if (hudWindow && !hudWindow.isDestroyed() && hudWindow.isVisible()) {
      hudWindow.hide();
    }
  });

  hudWindow.on("closed", () => {
    hudWindow = null;
  });

  if (fs.existsSync(HUD_HTML_PATH)) {
    hudWindow.loadFile(HUD_HTML_PATH).catch(() => {});
  } else {
    hudWindow
      .loadURL(
        `data:text/html;charset=utf-8,${encodeURIComponent(
          "<!doctype html><html><body style='margin:0;background:#0d1422;color:#e6edf7;font-family:system-ui,sans-serif;display:grid;place-items:center;height:100vh'><div>Quick Capture HUD</div></body></html>"
        )}`
      )
      .catch(() => {});
  }

  return hudWindow;
}

async function toggleQuickCaptureHud() {
  if (!backendManager || !backendManager.baseUrl) {
    return { visible: false };
  }
  const win = ensureHudWindow();
  if (win.isVisible()) {
    win.hide();
    return { visible: false };
  }

  // Enforce mandatory vault lock: if locked/uninitialized, focus main unlock window instead of opening HUD
  const vault = await fetchVaultStatus();
  if (!vault.unlocked) {
    applySnapshotUpdate({ locked: true, dailySafe: null, dailySafeCents: null });
    showMainWindow();
    return { visible: false, locked: true };
  }

  positionHudWindow(win);
  hudLastShownAt = Date.now();

  const notifyHudShown = () => {
    if (!win.isDestroyed()) {
      win.webContents.send("hud:shown", {
        theme: desktopPrefs.theme,
        shortcut: activeShortcut,
        snapshot: {
          dailySafeCents: liveSnapshot.dailySafeCents,
          safeToSpendCents: liveSnapshot.safeToSpendCents,
          daysUntilPayday: liveSnapshot.daysUntilPayday,
          mode: liveSnapshot.mode,
        },
      });
    }
  };

  if (win.webContents.isLoading()) {
    win.webContents.once("did-finish-load", () => {
      notifyHudShown();
      if (!win.isDestroyed()) {
        win.show();
        win.focus();
      }
    });
  } else {
    notifyHudShown();
    win.show();
    win.focus();
  }

  // Silently keep Tray & future HUD opens fresh without resetting active HUD input
  fetchLiveDashboardSnapshot();

  return { visible: true };
}

function registerQuickCaptureShortcut(preferred) {
  globalShortcut.unregisterAll();
  const candidates = [];
  if (preferred && typeof preferred === "string" && preferred.trim()) {
    candidates.push(preferred.trim());
  }
  if (!candidates.includes(PRIMARY_SHORTCUT)) candidates.push(PRIMARY_SHORTCUT);
  if (!candidates.includes(FALLBACK_SHORTCUT)) candidates.push(FALLBACK_SHORTCUT);

  for (const accel of candidates) {
    try {
      const ok = globalShortcut.register(accel, () => {
        toggleQuickCaptureHud();
      });
      if (ok) {
        activeShortcut = accel;
        shortcutRegistered = true;
        if (trayController) trayController.updateSnapshot({ shortcut: activeShortcut });
        return { registered: true, shortcut: activeShortcut };
      }
    } catch {
      // Invalid accelerator syntax or OS conflict — try fallback.
    }
  }

  activeShortcut = preferred || PRIMARY_SHORTCUT;
  shortcutRegistered = false;
  return { registered: false, shortcut: activeShortcut };
}

// ---------------------------------------------------------------------------
// IPC Handlers (Strictly Validated)
// ---------------------------------------------------------------------------

function registerIpcHandlers() {
  // --- Window Controls ---
  ipcMain.handle("window:minimize", (event) => {
    assertMainSender(event);
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.minimize();
  });

  ipcMain.handle("window:maximizeToggle", (event) => {
    assertMainSender(event);
    if (!mainWindow || mainWindow.isDestroyed()) return { maximized: false };
    if (mainWindow.isMaximized()) {
      mainWindow.unmaximize();
    } else {
      mainWindow.maximize();
    }
    return { maximized: mainWindow.isMaximized() };
  });

  ipcMain.handle("window:close", (event) => {
    assertMainSender(event);
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.close();
  });

  ipcMain.handle("window:isMaximized", (event) => {
    assertMainSender(event);
    return Boolean(mainWindow && !mainWindow.isDestroyed() && mainWindow.isMaximized());
  });

  ipcMain.handle("window:setThemeOverlay", (event, theme) => {
    assertMainSender(event);
    const clean = theme === "light" ? "light" : "dark";
    savePrefs({ ...desktopPrefs, theme: clean });
    applyWindowThemeOverlay(clean);
    if (trayController) trayController.updateSnapshot({ theme: clean });
    refreshApplicationMenu();
    return { ok: true, theme: clean };
  });

  // --- Desktop Capabilities, Prefs & Diagnostics ---
  ipcMain.handle("desktop:getInfo", (event) => {
    assertMainSender(event);
    return {
      isDesktop: true,
      appVersion: app.getVersion(),
      platform: process.platform,
      arch: process.arch,
      electronVersion: process.versions.electron,
      chromeVersion: process.versions.chrome,
      nodeVersion: process.versions.node,
      backendUrl: backendManager ? backendManager.baseUrl || "" : "",
      backendMode: backendManager ? backendManager.backendMode : "uninitialized",
      dataDir: app.getPath("userData"),
      dbPath: backendManager ? backendManager.dbPath : "",
      shortcut: activeShortcut,
      shortcutRegistered,
      prefs: { ...desktopPrefs },
    };
  });

  ipcMain.handle("desktop:setPrefs", (event, partial) => {
    assertMainSender(event);
    const input = partial && typeof partial === "object" ? partial : {};
    const prevLaunch = desktopPrefs.launchAtLogin;
    const prevTheme = desktopPrefs.theme;
    const prevShortcut = desktopPrefs.quickCaptureShortcut;

    const merged = savePrefs({ ...desktopPrefs, ...input });

    if (merged.launchAtLogin !== prevLaunch) {
      applyLaunchAtLogin(merged.launchAtLogin);
    }
    if (merged.theme !== prevTheme) {
      applyWindowThemeOverlay(merged.theme);
      if (trayController) trayController.updateSnapshot({ theme: merged.theme });
      refreshApplicationMenu();
    }
    if (merged.quickCaptureShortcut !== prevShortcut) {
      registerQuickCaptureShortcut(merged.quickCaptureShortcut);
    }
    const watchStatus = syncWatchFolder();

    return {
      ok: true,
      prefs: { ...merged },
      shortcutRegistered,
      shortcut: activeShortcut,
      watchStatus,
    };
  });

  ipcMain.handle("desktop:openDataDir", async (event) => {
    assertMainSender(event);
    const dir = app.getPath("userData");
    const errStr = await shell.openPath(dir);
    return { ok: !errStr, path: dir, ...(errStr ? { error: errStr } : {}) };
  });

  ipcMain.handle("desktop:getBackendLogs", (event) => {
    assertKnownChronoxSender(event);
    if (!backendManager) {
      return { lines: [], backendMode: "uninitialized", backendUrl: "", pid: null };
    }
    return backendManager.getLogs();
  });

  ipcMain.handle("desktop:toggleQuickCapture", (event) => {
    assertMainSender(event);
    return toggleQuickCaptureHud();
  });

  ipcMain.handle("desktop:notify", (event, payload) => {
    assertMainSender(event);
    if (!Notification.isSupported()) {
      return { ok: false };
    }
    const title = String((payload && payload.title) || APP_NAME).slice(0, 120);
    const body = String((payload && payload.body) || "").slice(0, 320);
    try {
      const n = new Notification({
        title,
        body,
        icon: getAppIcon(),
        silent: false,
      });
      n.on("click", () => showMainWindow());
      n.show();
      return { ok: true };
    } catch {
      return { ok: false };
    }
  });

  ipcMain.handle("desktop:openExternal", async (event, url) => {
    assertMainSender(event);
    return openExternalHttps(url);
  });

  // --- Native OS File Dialogs ---
  ipcMain.handle("dialog:openStatement", async (event) => {
    assertMainSender(event);
    if (!mainWindow || mainWindow.isDestroyed()) return { canceled: true };
    const result = await dialog.showOpenDialog(mainWindow, {
      title: "Import Bank or Card Statement",
      properties: ["openFile"],
      filters: [
        {
          name: "Financial Statements (CSV, PDF, OFX, QIF, Plaid JSON)",
          extensions: ["csv", "tsv", "pdf", "ofx", "qfx", "qif", "json"],
        },
        { name: "All Files", extensions: ["*"] },
      ],
    });

    if (result.canceled || !result.filePaths || result.filePaths.length === 0) {
      return { canceled: true };
    }

    const filePath = result.filePaths[0];
    try {
      const stat = fs.statSync(filePath);
      if (stat.size > MAX_STATEMENT_BYTES) {
        return {
          canceled: false,
          error: `File exceeds the 5 MB safety limit (${(stat.size / (1024 * 1024)).toFixed(1)} MB).`,
        };
      }
      const rawBuffer = fs.readFileSync(filePath);
      const isPdf = filePath.toLowerCase().endsWith(".pdf") || rawBuffer.subarray(0, 5).toString("latin1") === "%PDF-";
      if (isPdf) {
        return {
          canceled: false,
          filename: path.basename(filePath),
          contentBase64: rawBuffer.toString("base64"),
          isBinary: true,
          sizeBytes: stat.size,
        };
      }
      const content = rawBuffer.toString("utf8").replace(/^\uFEFF/, "");
      return {
        canceled: false,
        filename: path.basename(filePath),
        content,
        isBinary: false,
        sizeBytes: stat.size,
      };
    } catch (err) {
      return {
        canceled: false,
        error: `Could not read selected file: ${err.message}`,
      };
    }
  });

  ipcMain.handle("dialog:saveFile", async (event, options) => {
    assertMainSender(event);
    if (!mainWindow || mainWindow.isDestroyed()) return { canceled: true };
    const opts = options && typeof options === "object" ? options : {};
    const isBinary = Boolean(opts.isBinary && typeof opts.contentBase64 === "string");
    let rawBuffer = null;
    const content = typeof opts.content === "string" ? opts.content : "";
    if (isBinary) {
      rawBuffer = Buffer.from(opts.contentBase64, "base64");
      if (rawBuffer.length > MAX_SAVE_BYTES) {
        return { canceled: false, error: "Export payload exceeds maximum size limit." };
      }
    } else if (Buffer.byteLength(content, "utf8") > MAX_SAVE_BYTES) {
      return { canceled: false, error: "Export payload exceeds maximum size limit." };
    }

    const defaultName =
      typeof opts.defaultName === "string" && opts.defaultName.trim()
        ? path.basename(opts.defaultName.trim())
        : "chronox-finance-backup.json";

    const filters =
      Array.isArray(opts.filters) && opts.filters.length > 0
        ? opts.filters
            .filter((f) => f && typeof f.name === "string" && Array.isArray(f.extensions))
            .map((f) => ({
              name: f.name.slice(0, 64),
              extensions: f.extensions.filter((e) => typeof e === "string").slice(0, 12),
            }))
        : [
            { name: "Apple Numbers / Excel Workbook", extensions: ["xlsx"] },
            { name: "CSV Spreadsheet", extensions: ["csv"] },
            { name: "ZIP Archive", extensions: ["zip"] },
            { name: "JSON Backup", extensions: ["json"] },
            { name: "All Files", extensions: ["*"] },
          ];

    const result = await dialog.showSaveDialog(mainWindow, {
      title: "Save ChronoX Export",
      defaultPath: path.join(app.getPath("documents"), defaultName),
      filters,
    });

    if (result.canceled || !result.filePath) {
      return { canceled: true };
    }

    try {
      if (isBinary && rawBuffer) {
        fs.writeFileSync(result.filePath, rawBuffer);
      } else {
        fs.writeFileSync(result.filePath, content, "utf8");
      }
      return { canceled: false, filePath: result.filePath };
    } catch (err) {
      return { canceled: false, error: `Could not save file: ${err.message}` };
    }
  });

  // --- Optional Local Auto-Import Watch Folder (Zero-3rd-Party Sync) ---
  ipcMain.handle("watch:getStatus", (event) => {
    assertMainSender(event);
    return watchFolderController
      ? watchFolderController.getStatus()
      : {
          enabled: desktopPrefs.watchFolderEnabled,
          watching: false,
          folderPath: desktopPrefs.watchFolderPath,
          institution: desktopPrefs.watchFolderInstitution,
          skipPending: desktopPrefs.watchFolderSkipPending,
          syncBalance: desktopPrefs.watchFolderSyncBalance,
          importedFilesCount: 0,
          lastImportSummary: null,
          lastError: null,
        };
  });

  ipcMain.handle("watch:pickFolder", async (event) => {
    assertMainSender(event);
    if (!mainWindow || mainWindow.isDestroyed()) return { canceled: true };
    const result = await dialog.showOpenDialog(mainWindow, {
      title: "Choose Local Statement Drop Folder (e.g., Navy Federal / Plaid Exports)",
      defaultPath: desktopPrefs.watchFolderPath || app.getPath("downloads"),
      properties: ["openDirectory", "createDirectory"],
    });
    if (result.canceled || !result.filePaths || result.filePaths.length === 0) {
      return { canceled: true };
    }
    const chosen = result.filePaths[0];
    const merged = savePrefs({ ...desktopPrefs, watchFolderPath: chosen });
    const status = syncWatchFolder();
    return { canceled: false, folderPath: chosen, prefs: { ...merged }, status };
  });

  ipcMain.handle("watch:scanNow", async (event) => {
    assertMainSender(event);
    if (!watchFolderController) {
      syncWatchFolder();
    }
    if (!watchFolderController) {
      return { ok: false, processed: 0, error: "Watch folder is unavailable." };
    }
    return watchFolderController.scanNow();
  });

  ipcMain.handle("watch:openFolder", async (event) => {
    assertMainSender(event);
    if (!desktopPrefs.watchFolderPath) {
      return { ok: false, error: "No watch folder configured yet." };
    }
    const errStr = await shell.openPath(desktopPrefs.watchFolderPath);
    return { ok: !errStr, path: desktopPrefs.watchFolderPath, ...(errStr ? { error: errStr } : {}) };
  });

  // --- Live Tray Snapshot Sync ---
  ipcMain.handle("tray:updateSnapshot", (event, snapshot) => {
    assertMainSender(event);
    applySnapshotUpdate(snapshot);
    return { ok: true };
  });

  // --- Splash Surface Handlers ---
  ipcMain.handle("splash:getState", (event) => {
    assertSplashSender(event);
    const s = backendManager
      ? backendManager.getState()
      : { stage: "starting", label: "Starting local vault", detail: "", progress: 10, error: null };
    return {
      ...s,
      theme: desktopPrefs.theme,
      appVersion: app.getVersion(),
    };
  });

  ipcMain.handle("splash:retry", async (event) => {
    assertSplashSender(event);
    bootBackendAndMainWindow().catch(() => {});
    return { ok: true };
  });

  ipcMain.handle("splash:quit", (event) => {
    assertSplashSender(event);
    requestFullQuit();
  });

  // --- Global Quick-Capture HUD Handlers ---
  ipcMain.handle("hud:getState", (event) => {
    assertHudSender(event);
    return {
      backendUrl: backendManager ? backendManager.baseUrl || "" : "",
      theme: desktopPrefs.theme,
      shortcut: activeShortcut,
      snapshot: {
        dailySafeCents: liveSnapshot.dailySafeCents,
        safeToSpendCents: liveSnapshot.safeToSpendCents,
        daysUntilPayday: liveSnapshot.daysUntilPayday,
        mode: liveSnapshot.mode,
      },
    };
  });

  ipcMain.handle("hud:submitQuickAdd", async (event, rawText) => {
    assertHudSender(event);
    const text = String(rawText || "").trim();
    if (!text) {
      return { ok: false, error: "Type a quick expense, income, question, or command first." };
    }
    if (text.length > 500) {
      return { ok: false, error: "Entry is too long (max 500 characters)." };
    }

    try {
      let res = await backendRequest("POST", "/api/v1/ai/say", { text, auto_commit: true });
      if (res.status === 404) {
        res = await backendRequest("POST", "/api/v1/quick-add", { text });
      }
      if (res.status === 401) {
        applySnapshotUpdate({ locked: true, dailySafe: null, dailySafeCents: null });
        showMainWindow();
        return {
          ok: false,
          locked: true,
          error: "ChronoX is locked — unlock the main window first.",
        };
      }
      if (res.status < 200 || res.status >= 300) {
        const detail = (res.data && res.data.detail) || "Could not process entry.";
        return { ok: false, error: String(detail) };
      }

      const data = res.data || {};
      const intentKind = String(data.intent_kind || data.kind || "transaction");
      const displayMsg = String(data.assistant_message || data.message || "Saved to ChronoX");
      const updatedSnap = await fetchLiveDashboardSnapshot();

      sendToMain("chronox:refresh", {
        source: "quick-capture-hud",
        message: displayMsg,
        intentKind,
      });

      return {
        ok: true,
        kind: intentKind,
        intentKind,
        executed: Boolean(data.executed !== undefined ? data.executed : true),
        requiresConfirmation: Boolean(data.requires_confirmation),
        batchId: data.batch_id || null,
        message: displayMsg,
        dailySafeCents: updatedSnap ? updatedSnap.dailySafeCents : liveSnapshot.dailySafeCents,
        safeToSpendCents: updatedSnap ? updatedSnap.safeToSpendCents : liveSnapshot.safeToSpendCents,
        daysUntilPayday: updatedSnap ? updatedSnap.daysUntilPayday : liveSnapshot.daysUntilPayday,
      };
    } catch (err) {
      return {
        ok: false,
        error: err && err.message ? err.message : "Failed to connect to local ChronoX engine.",
      };
    }
  });

  ipcMain.handle("hud:hide", (event) => {
    assertHudSender(event);
    if (hudWindow && !hudWindow.isDestroyed()) {
      hudWindow.hide();
    }
  });

  ipcMain.handle("hud:openMainApp", (event, tab) => {
    assertHudSender(event);
    if (hudWindow && !hudWindow.isDestroyed()) {
      hudWindow.hide();
    }
    showMainWindow();
    if (typeof tab === "string" && VALID_TABS.has(tab)) {
      sendToMain("chronox:navigate-tab", tab);
    }
  });
}

// ---------------------------------------------------------------------------
// Boot & Teardown Choreography
// ---------------------------------------------------------------------------

function resolveDbPath() {
  if (cliFlags.dbPath) return cliFlags.dbPath;
  if (process.env.CHRONOX_FINANCE_DB_PATH && process.env.CHRONOX_FINANCE_DB_PATH.trim()) {
    return path.resolve(process.env.CHRONOX_FINANCE_DB_PATH.trim());
  }
  return path.join(app.getPath("userData"), "finance.sqlite3");
}

function ensureWatchFolderController() {
  if (!watchFolderController) {
    watchFolderController = new WatchFolderController({
      getBackendUrl: () => (backendManager && backendManager.baseUrl ? backendManager.baseUrl : ""),
      getCookieHeader: async (url) => {
        try {
          const cookies = await session.defaultSession.cookies.get({ url });
          return cookies.map((c) => `${c.name}=${c.value}`).join("; ");
        } catch {
          return "";
        }
      },
      getCsrfToken: async () => {
        const st = await fetchVaultStatus();
        return st.csrfToken || "";
      },
      onImported: async (summary) => {
        const snap = await fetchLiveDashboardSnapshot();
        const instLabel =
          summary.detectedInstitution === "navy_federal"
            ? "Navy Federal"
            : summary.detectedInstitution === "plaid"
              ? "Plaid"
              : summary.formatDetected;
        const msg =
          summary.importedCount > 0
            ? `Auto-imported ${summary.importedCount} ${instLabel} tx from ${summary.filename}`
            : `Checked ${summary.filename} (${summary.duplicateCount} already imported)`;
        sendToMain("chronox:refresh", { source: "watch-folder", message: msg });
        if (Notification.isSupported() && summary.importedCount > 0) {
          const safeStr =
            snap && Number.isFinite(snap.dailySafeCents)
              ? ` · Safe Today: $${(snap.dailySafeCents / 100).toFixed(2)}`
              : "";
          try {
            const n = new Notification({
              title: `${instLabel} Statement Imported`,
              body: `Imported ${summary.importedCount} transaction(s) from ${summary.filename}${safeStr}`,
              icon: getAppIcon(),
            });
            n.on("click", () => showMainWindow());
            n.show();
          } catch {
            // Ignore desktop notification daemon absence on headless Linux.
          }
        }
      },
      onLog: (_level, text) => {
        if (backendManager && typeof backendManager._pushLog === "function") {
          backendManager._pushLog("stdout", `[watch-folder] ${text}`);
        }
      },
    });
  }
  return watchFolderController;
}

function syncWatchFolder() {
  const ctrl = ensureWatchFolderController();
  return ctrl.configure({
    enabled: desktopPrefs.watchFolderEnabled,
    folderPath: desktopPrefs.watchFolderPath,
    institution: desktopPrefs.watchFolderInstitution,
    skipPending: desktopPrefs.watchFolderSkipPending,
    syncBalance: desktopPrefs.watchFolderSyncBalance,
  });
}

function initTrayAndShortcuts() {
  if (!trayController) {
    trayController = new TrayController({
      iconsDir: resolveIconsDir(),
      initialTheme: desktopPrefs.theme,
      shortcut: desktopPrefs.quickCaptureShortcut,
      callbacks: {
        onOpenApp: () => showMainWindow(),
        onToggleQuickCapture: () => toggleQuickCaptureHud(),
        onTriggerSync: async () => {
          showMainWindow();
          const vault = await fetchVaultStatus();
          if (vault.unlocked) {
            sendToMain("chronox:trigger-sync");
          }
        },
        onSetMode: (mode) => {
          if (!VALID_MODES.has(mode)) return;
          liveSnapshot.mode = mode;
          trayController.updateSnapshot({ mode });
          sendToMain("chronox:set-mode", mode);
          refreshApplicationMenu();
        },
        onSetTheme: (theme) => {
          const clean = theme === "light" ? "light" : "dark";
          savePrefs({ ...desktopPrefs, theme: clean });
          applyWindowThemeOverlay(clean);
          trayController.updateSnapshot({ theme: clean });
          sendToMain("chronox:set-theme", clean);
          refreshApplicationMenu();
        },
        onQuit: () => requestFullQuit(),
      },
    });
    trayController.init();
  }

  registerQuickCaptureShortcut(desktopPrefs.quickCaptureShortcut);
  refreshApplicationMenu();
  syncWatchFolder();
}

async function bootBackendAndMainWindow() {
  if (!backendManager) {
    backendManager = new BackendManager({
      app,
      dbPath: resolveDbPath(),
      repoRoot: REPO_ROOT,
      desktopRoot: DESKTOP_ROOT,
    });
    backendManager.on("status", (status) => {
      sendSplashStatus(status);
    });
  }

  try {
    if (backendManager.pid) {
      await backendManager.restart();
    } else {
      await backendManager.start();
    }

    initTrayAndShortcuts();
    if (!mainWindow || mainWindow.isDestroyed()) {
      createMainWindow();
    } else {
      mainWindow.loadURL(`${backendManager.baseUrl}/`).catch(() => {});
      showMainWindow();
      closeSplashWindow();
    }

    // Hydrate tray & HUD snapshot asynchronously once backend is healthy
    fetchLiveDashboardSnapshot();
  } catch {
    // Error status is already emitted to splashWindow so user can Retry or View Logs.
  }
}

function requestFullQuit() {
  isQuitting = true;
  app.quit();
}

// ---------------------------------------------------------------------------
// Electron Application Lifecycle
// ---------------------------------------------------------------------------

if (gotSingleInstanceLock) {
  app.on("second-instance", (_event, argv) => {
    const secondFlags = parseCliFlags(argv.slice(1));
    if (secondFlags.quickCapture) {
      toggleQuickCaptureHud();
      return;
    }
    if (mainWindow && !mainWindow.isDestroyed()) {
      showMainWindow();
    } else if (splashWindow && !splashWindow.isDestroyed()) {
      splashWindow.show();
      splashWindow.focus();
    }
  });

  app.whenReady().then(() => {
    loadPrefs();
    configureSessionSecurity();
    registerIpcHandlers();
    createSplashWindow();
    bootBackendAndMainWindow();
  });

  app.on("activate", () => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      showMainWindow();
    } else if (backendManager && backendManager.baseUrl) {
      createMainWindow();
    }
  });

  app.on("window-all-closed", () => {
    if (process.platform !== "darwin" && (!desktopPrefs.closeToTray || !trayController || !trayController.isAvailable || isQuitting)) {
      requestFullQuit();
    }
  });

  app.on("before-quit", (event) => {
    isQuitting = true;
    globalShortcut.unregisterAll();

    if (hudWindow && !hudWindow.isDestroyed()) {
      hudWindow.destroy();
      hudWindow = null;
    }
    if (splashWindow && !splashWindow.isDestroyed()) {
      splashWindow.destroy();
      splashWindow = null;
    }
    if (trayController) {
      trayController.destroy();
      trayController = null;
    }
    if (watchFolderController) {
      watchFolderController.stop();
      watchFolderController = null;
    }

    if (backendManager && backendManager.pid && !isCleaningUpBeforeQuit) {
      isCleaningUpBeforeQuit = true;
      event.preventDefault();
      backendManager
        .stop()
        .catch(() => {})
        .finally(() => {
          app.exit(0);
        });
    }
  });

  app.on("will-quit", () => {
    globalShortcut.unregisterAll();
  });

  const handleTerminationSignal = () => {
    if (isCleaningUpBeforeQuit) return;
    isQuitting = true;
    isCleaningUpBeforeQuit = true;
    globalShortcut.unregisterAll();
    const hardTimer = setTimeout(() => {
      if (backendManager) backendManager.forceKillSync();
      app.exit(0);
    }, 2000);
    hardTimer.unref();
    Promise.resolve(backendManager ? backendManager.stop() : null)
      .catch(() => {})
      .finally(() => {
        clearTimeout(hardTimer);
        app.exit(0);
      });
  };

  process.on("SIGINT", handleTerminationSignal);
  process.on("SIGTERM", handleTerminationSignal);
  process.on("exit", () => {
    if (backendManager) backendManager.forceKillSync();
  });
}
