"use strict";

const fs = require("fs");
const path = require("path");
const { screen } = require("electron");

const DEFAULT_WIDTH = 1380;
const DEFAULT_HEIGHT = 880;
const MIN_WIDTH = 960;
const MIN_HEIGHT = 660;
const MIN_VISIBLE_PX = 120;

/**
 * Clamp and validate persisted window bounds against all currently connected
 * displays so the window never opens off-screen after monitor layout changes.
 */
function ensureVisibleBounds(bounds) {
  const displays = screen.getAllDisplays();
  const primary = screen.getPrimaryDisplay();
  const primaryWork = primary ? primary.workArea : { x: 0, y: 0, width: 1440, height: 900 };

  const width = Math.max(
    MIN_WIDTH,
    Math.min(Number(bounds.width) || DEFAULT_WIDTH, Math.max(MIN_WIDTH, primaryWork.width))
  );
  const height = Math.max(
    MIN_HEIGHT,
    Math.min(Number(bounds.height) || DEFAULT_HEIGHT, Math.max(MIN_HEIGHT, primaryWork.height))
  );

  const hasCoords = Number.isFinite(bounds.x) && Number.isFinite(bounds.y);
  if (hasCoords && displays.length > 0) {
    const x = Math.round(bounds.x);
    const y = Math.round(bounds.y);
    const isVisibleOnAnyDisplay = displays.some((display) => {
      const area = display.workArea;
      const overlapX = Math.min(x + width, area.x + area.width) - Math.max(x, area.x);
      const overlapY = Math.min(y + height, area.y + area.height) - Math.max(y, area.y);
      return overlapX >= MIN_VISIBLE_PX && overlapY >= MIN_VISIBLE_PX;
    });

    if (isVisibleOnAnyDisplay) {
      return {
        x,
        y,
        width: Math.round(width),
        height: Math.round(height),
        isMaximized: Boolean(bounds.isMaximized),
      };
    }
  }

  // Center gracefully on the primary display work area.
  const centeredX = Math.round(primaryWork.x + Math.max(0, (primaryWork.width - width) / 2));
  const centeredY = Math.round(primaryWork.y + Math.max(0, (primaryWork.height - height) / 2));
  return {
    x: centeredX,
    y: centeredY,
    width: Math.round(width),
    height: Math.round(height),
    isMaximized: Boolean(bounds.isMaximized),
  };
}

function loadWindowState(userDataDir) {
  const filePath = path.join(userDataDir, "window-state.json");
  let saved = {};
  try {
    if (fs.existsSync(filePath)) {
      const raw = fs.readFileSync(filePath, "utf8");
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === "object") {
        saved = parsed;
      }
    }
  } catch {
    saved = {};
  }

  const state = ensureVisibleBounds({
    x: saved.x,
    y: saved.y,
    width: saved.width || DEFAULT_WIDTH,
    height: saved.height || DEFAULT_HEIGHT,
    isMaximized: Boolean(saved.isMaximized),
  });

  return {
    state,
    filePath,
    minWidth: MIN_WIDTH,
    minHeight: MIN_HEIGHT,
  };
}

function writeStateFile(filePath, payload) {
  try {
    fs.mkdirSync(path.dirname(filePath), { recursive: true });
    const tmpPath = `${filePath}.tmp`;
    fs.writeFileSync(tmpPath, JSON.stringify(payload, null, 2), "utf8");
    fs.renameSync(tmpPath, filePath);
  } catch {
    // Non-fatal: failing to persist window geometry should never disrupt app UX.
  }
}

/**
 * Attach lifecycle listeners to a BrowserWindow so position, size, and
 * maximized state are persisted safely and debounced.
 */
function trackWindowState(win, filePath, initialState) {
  let lastNormalBounds = {
    x: initialState.x,
    y: initialState.y,
    width: initialState.width,
    height: initialState.height,
  };
  let saveTimer = null;

  const snapshot = () => {
    if (!win || win.isDestroyed()) return null;
    const isMaximized = win.isMaximized();
    if (!isMaximized && !win.isMinimized() && !win.isFullScreen()) {
      const b = win.getBounds();
      if (b.width >= MIN_WIDTH && b.height >= MIN_HEIGHT) {
        lastNormalBounds = { x: b.x, y: b.y, width: b.width, height: b.height };
      }
    }
    return {
      ...lastNormalBounds,
      isMaximized,
    };
  };

  const saveNow = () => {
    if (saveTimer) {
      clearTimeout(saveTimer);
      saveTimer = null;
    }
    const data = snapshot();
    if (data) writeStateFile(filePath, data);
  };

  const scheduleSave = () => {
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(saveNow, 300);
  };

  win.on("resize", scheduleSave);
  win.on("move", scheduleSave);
  win.on("maximize", scheduleSave);
  win.on("unmaximize", scheduleSave);
  win.on("close", saveNow);

  return { saveNow };
}

module.exports = {
  DEFAULT_WIDTH,
  DEFAULT_HEIGHT,
  MIN_WIDTH,
  MIN_HEIGHT,
  ensureVisibleBounds,
  loadWindowState,
  trackWindowState,
};
