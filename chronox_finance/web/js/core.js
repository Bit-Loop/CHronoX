/**
 * ChronoX Finance — Industrial Core Runtime (ES Module)
 * Shared DOM helpers, monochrome SVG icons, CSRF-protected API client,
 * Undo-capable toasts, 8-tab navigation, Command Palette & Electron Desktop Bridge.
 * Strict CSP compliant: zero raw HTML injection sinks, zero dynamic code eval, zero external CDNs, zero raw emojis.
 */

export const SVG_NS = "http://www.w3.org/2000/svg";
export const HERO_RING_CIRCUMFERENCE = 2 * Math.PI * 54;
export const CRED_RING_CIRCUMFERENCE = 2 * Math.PI * 35;

export const TAB_TITLES = {
  today: "Overview",
  analytics: "Cash Flow",
  activity: "Activity",
  portfolio: "Investments",
  credit: "Credit",
  goals: "Goals",
  coach: "Coach",
  settings: "Settings",
};

export const VALID_TABS = new Set(Object.keys(TAB_TITLES));

export const MODE_DESCRIPTIONS = {
  deterministic_only:
    "Built-in financial math only — works completely offline with instant, predictable calculations.",
  hybrid:
    "Combines exact built-in financial math with AI explanations verified against your real balances.",
  ai_standalone:
    "Direct AI financial coaching grounded in your live account balances and spending.",
};

export const BUCKET_LABELS = {
  autopay_protection: "Autopay Protection",
  promo_apr_cliff: "0% Promo Deadline",
  high_apr_debt: "High-APR Debt",
  azeo_fico_cliff: "Credit Utilization Tier",
  azeo_score_unlock: "Credit Utilization Tier",
  emergency_buffer: "Safety Cushion",
  tracked_goal: "Savings Goal",
  invest_vault: "Safe to Invest",
  low_apr_debt: "Low-APR Debt",
  safety_reserve: "Safety Reserve",
};

export const state = {
  csrfToken: "",
  vaultStatus: "locked", // "needs_setup" | "locked" | "unlocked"
  onboardingCompleted: true,
  dashboard: null,
  analytics: null,
  portfolio: null,
  creditReport: null,
  aiUsage: null,
  lastGoalPlan: null,
  settings: null,
  transactions: [],
  connections: [],
  emailQueue: [],
  opencodeModels: [],
  geminiModels: [],
  activeTab: "today",
  activeDrawer: null,
  editingJarId: null,
  reconcilingAcctId: null,
  editingTxId: null,
  txDetailId: null,
  paletteOpen: false,
  paletteIndex: 0,
  paletteItems: [],
  logsOpen: false,
  hydrated: false,
  desktopInfo: null,
  desktopPlatform: null,
  desktopStatementFile: null,
  lastLogsText: "",
  modalTimers: {},
  animatedValues: {},
};

const _tabHooks = new Map();
const _refreshHooks = new Set();
const _dashboardHooks = new Set();
const _commandProviders = new Set();
let _vaultGateHandler = null;
let _refreshAllImpl = null;
let _sayHandler = null;
let _oneTapHandler = null;

export const $ = (id) => document.getElementById(id);

export function registerSayHandler(fn) {
  _sayHandler = typeof fn === "function" ? fn : null;
}

export async function invokeSayHandler(text) {
  if (_sayHandler) {
    return _sayHandler(text);
  }
  const res = await api("/api/v1/quick-add", {
    method: "POST",
    body: JSON.stringify({ text }),
  });
  toast(res.message || "Saved.");
  await refreshAll();
  return res;
}

export function registerOneTapHandler(fn) {
  _oneTapHandler = typeof fn === "function" ? fn : null;
}

export async function invokeOneTapHandler(prefetchedPlan = null) {
  if (_oneTapHandler) {
    return _oneTapHandler(prefetchedPlan);
  }
  toast("One-Tap Optimize is loading — try again in a moment.", true);
  return null;
}

export function registerTabHook(tabName, fn) {
  if (typeof fn !== "function") return;
  if (!_tabHooks.has(tabName)) _tabHooks.set(tabName, new Set());
  _tabHooks.get(tabName).add(fn);
}

export function registerRefreshHook(fn) {
  if (typeof fn === "function") _refreshHooks.add(fn);
}

export function registerDashboardHook(fn) {
  if (typeof fn === "function") _dashboardHooks.add(fn);
}

export function registerCommandProvider(fn) {
  if (typeof fn === "function") _commandProviders.add(fn);
}

export function setVaultGateHandler(fn) {
  _vaultGateHandler = typeof fn === "function" ? fn : null;
}

export function triggerVaultGate(mode = "locked", reason = "") {
  if (_vaultGateHandler) {
    _vaultGateHandler(mode, reason);
  }
}

export function setRefreshAllImpl(fn) {
  _refreshAllImpl = typeof fn === "function" ? fn : null;
}

export function setCsrfToken(token) {
  if (typeof token === "string" && token.trim()) {
    state.csrfToken = token.trim();
  }
}

export function getCsrfToken() {
  return state.csrfToken;
}

export async function dispatchDashboardHooks(dash) {
  for (const fn of _dashboardHooks) {
    try {
      await fn(dash);
    } catch (_err) {
      // Keep independent module panels isolated
    }
  }
}

export async function dispatchRefreshHooks(opts = {}) {
  for (const fn of _refreshHooks) {
    try {
      await fn(opts);
    } catch (_err) {
      // Keep independent module refreshes isolated
    }
  }
}

export async function refreshAll(opts = {}) {
  const normalized = typeof opts === "boolean" ? { refreshAI: opts } : opts || {};
  if (_refreshAllImpl) {
    return _refreshAllImpl(normalized);
  }
  await dispatchRefreshHooks(normalized);
  return null;
}

/* --- Motion & Formatting Helpers --- */

export function prefersReducedMotion() {
  return Boolean(
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

export function fmtMoney(cents) {
  const num = Number(cents) || 0;
  const neg = num < 0;
  const val = (Math.abs(num) / 100).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  return `${neg ? "-" : ""}$${val}`;
}

export function fmtMoneyCompact(cents) {
  const num = (Number(cents) || 0) / 100;
  const abs = Math.abs(num);
  const sign = num < 0 ? "-" : "";
  if (abs >= 1000000) return `${sign}$${(abs / 1000000).toFixed(2)}M`;
  if (abs >= 10000) return `${sign}$${(abs / 1000).toFixed(1)}k`;
  if (abs >= 1000) return `${sign}$${(abs / 1000).toFixed(2)}k`;
  return `${sign}$${Math.round(abs)}`;
}

/**
 * Format fixed-point unit prices (handles both equities like $185.20 and sub-cent crypto like $0.00002415).
 */
export function fmtE8Price(priceE8) {
  const raw = Number(priceE8) || 0;
  const neg = raw < 0;
  const absVal = Math.abs(raw) / 1e8;
  if (absVal === 0) return "$0.00";
  if (absVal >= 1) {
    return `${neg ? "-" : ""}$${absVal.toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}`;
  }
  if (absVal >= 0.01) {
    return `${neg ? "-" : ""}$${absVal.toLocaleString(undefined, {
      minimumFractionDigits: 4,
      maximumFractionDigits: 4,
    })}`;
  }
  const fixed = absVal.toFixed(8).replace(/0+$/, "");
  return `${neg ? "-" : ""}$${fixed}`;
}

/**
 * Format fixed-point quantities (fractional shares or crypto units).
 */
export function fmtE8Qty(qtyE8) {
  const raw = Number(qtyE8) || 0;
  const val = raw / 1e8;
  if (val === 0) return "0";
  if (Math.abs(val) >= 1000) {
    return val.toLocaleString(undefined, { maximumFractionDigits: 2 });
  }
  if (Number.isInteger(val)) return String(val);
  return val.toFixed(8).replace(/\.?0+$/, "");
}

export function fmtShortDate(iso) {
  if (!iso) return "--";
  const parts = String(iso).slice(0, 10).split("-");
  if (parts.length !== 3) return String(iso);
  const dt = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]));
  if (Number.isNaN(dt.getTime())) return String(iso);
  return dt.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function fmtDayDate(iso) {
  if (!iso) return "--";
  const parts = String(iso).slice(0, 10).split("-");
  if (parts.length !== 3) return String(iso);
  const dt = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]));
  if (Number.isNaN(dt.getTime())) return String(iso);
  return dt.toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
  });
}

export function dollarsToCents(raw, { allowNegative = false, allowZero = false } = {}) {
  const cleaned = String(raw ?? "")
    .trim()
    .replace(/[$,\s]/g, "");
  if (!cleaned) throw new Error("Please enter a dollar amount.");
  const num = Number(cleaned);
  if (!Number.isFinite(num)) throw new Error("Please enter a valid number.");
  if (!allowNegative && num < 0) throw new Error("Amount cannot be negative.");
  if (!allowZero && num === 0) throw new Error("Amount must be greater than $0.00.");
  return Math.round(num * 100);
}

export function isoDateOffset(days = 0) {
  const dt = new Date();
  dt.setDate(dt.getDate() + days);
  const y = dt.getFullYear();
  const m = String(dt.getMonth() + 1).padStart(2, "0");
  const d = String(dt.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

export function animateMoney(node, targetCents, key) {
  if (!node) return;
  const target = Number(targetCents) || 0;
  const prev = state.animatedValues[key];
  state.animatedValues[key] = target;
  if (prev === undefined || prev === target || prefersReducedMotion()) {
    node.textContent = fmtMoney(target);
    return;
  }
  const start = performance.now();
  const duration = 260;
  const step = (now) => {
    const t = Math.min(1, (now - start) / duration);
    const eased = 1 - Math.pow(1 - t, 3);
    const cur = Math.round(prev + (target - prev) * eased);
    node.textContent = fmtMoney(cur);
    if (t < 1 && state.animatedValues[key] === target) {
      requestAnimationFrame(step);
    } else if (state.animatedValues[key] === target) {
      node.textContent = fmtMoney(target);
    }
  };
  requestAnimationFrame(step);
}

export function animatePercent(node, targetPct, key, decimals = 1) {
  if (!node) return;
  const target = Number(targetPct) || 0;
  const prev = state.animatedValues[key];
  state.animatedValues[key] = target;
  if (prev === undefined || Math.abs(prev - target) < 0.05 || prefersReducedMotion()) {
    node.textContent = `${target.toFixed(decimals)}%`;
    return;
  }
  const start = performance.now();
  const duration = 260;
  const step = (now) => {
    const t = Math.min(1, (now - start) / duration);
    const eased = 1 - Math.pow(1 - t, 3);
    const cur = prev + (target - prev) * eased;
    node.textContent = `${cur.toFixed(decimals)}%`;
    if (t < 1 && state.animatedValues[key] === target) {
      requestAnimationFrame(step);
    } else if (state.animatedValues[key] === target) {
      node.textContent = `${target.toFixed(decimals)}%`;
    }
  };
  requestAnimationFrame(step);
}

export function triggerCelebrate(node, withPulse = false) {
  if (prefersReducedMotion()) return;
  const targets = [node, withPulse ? $("xp-pill") : null].filter(Boolean);
  for (const elNode of targets) {
    elNode.classList.remove("celebrate-pulse");
    void elNode.offsetWidth;
    elNode.classList.add("celebrate-pulse");
  }
}

/* --- Strict Safe DOM & SVG Builders (DOM API Only) --- */

export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = String(v ?? "");
    else if (k === "checked") node.checked = Boolean(v);
    else if (k === "disabled") node.disabled = Boolean(v);
    else if (k === "selected") node.selected = Boolean(v);
    else if (k === "value") node.value = String(v ?? "");
    else if (k.startsWith("data-")) node.setAttribute(k, String(v ?? ""));
    else if (k === "style" && typeof v === "object" && v !== null) Object.assign(node.style, v);
    else if (v !== null && v !== undefined && v !== false) node.setAttribute(k, String(v));
  }
  const list = Array.isArray(children) ? children : [children];
  for (const child of list) {
    if (typeof child === "string" || typeof child === "number") {
      node.appendChild(document.createTextNode(String(child)));
    } else if (child instanceof Node) {
      node.appendChild(child);
    }
  }
  return node;
}

export function svgEl(tag, attrs = {}, children = []) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "text") node.textContent = String(v ?? "");
    else if (v !== null && v !== undefined) node.setAttribute(k, String(v));
  }
  const list = Array.isArray(children) ? children : [children];
  for (const child of list) {
    if (child instanceof Node) node.appendChild(child);
  }
  return node;
}

export const ICON_SHAPES = {
  sun: [
    ["circle", { cx: "12", cy: "12", r: "4" }],
    ["path", { d: "M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41" }],
  ],
  moon: [["path", { d: "M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" }]],
  card: [
    ["rect", { x: "2", y: "5", width: "20", height: "14" }],
    ["line", { x1: "2", y1: "10", x2: "22", y2: "10" }],
    ["line", { x1: "6", y1: "15", x2: "10", y2: "15" }],
  ],
  target: [
    ["circle", { cx: "12", cy: "12", r: "9" }],
    ["circle", { cx: "12", cy: "12", r: "5" }],
    ["circle", { cx: "12", cy: "12", r: "1.5" }],
  ],
  snowflake: [
    ["line", { x1: "12", y1: "2", x2: "12", y2: "22" }],
    ["line", { x1: "4.93", y1: "7", x2: "19.07", y2: "17" }],
    ["line", { x1: "19.07", y1: "7", x2: "4.93", y2: "17" }],
  ],
  zap: [["polygon", { points: "13 2 3 14 12 14 11 22 21 10 12 10 13 2" }]],
  bolt: [["polygon", { points: "13 2 3 14 12 14 11 22 21 10 12 10 13 2" }]],
  receipt: [
    ["path", { d: "M4 3h16v18l-2.5-1.5L15 21l-3-1.5L9 21l-2.5-1.5L4 21V3z" }],
    ["line", { x1: "8", y1: "8", x2: "16", y2: "8" }],
    ["line", { x1: "8", y1: "12", x2: "16", y2: "12" }],
  ],
  sliders: [
    ["line", { x1: "4", y1: "6", x2: "20", y2: "6" }],
    ["circle", { cx: "9", cy: "6", r: "2" }],
    ["line", { x1: "4", y1: "12", x2: "20", y2: "12" }],
    ["circle", { cx: "15", cy: "12", r: "2" }],
    ["line", { x1: "4", y1: "18", x2: "20", y2: "18" }],
    ["circle", { cx: "10", cy: "18", r: "2" }],
  ],
  home: [
    ["path", { d: "M3 10.5L12 3l9 7.5" }],
    ["path", { d: "M5 9.5V21h14V9.5" }],
    ["path", { d: "M9.5 21v-6h5v6" }],
  ],
  cart: [
    ["circle", { cx: "9", cy: "20", r: "1.4" }],
    ["circle", { cx: "18", cy: "20", r: "1.4" }],
    ["path", { d: "M2 3h3l2.4 12.2h10.7L22 7H6" }],
  ],
  bag: [
    ["path", { d: "M6 7h12l1 14H5L6 7z" }],
    ["path", { d: "M9 7V6a3 3 0 0 1 6 0v1" }],
  ],
  sparkles: [
    ["path", { d: "M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9L12 3z" }],
    ["path", { d: "M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8L19 15z" }],
  ],
  shield: [
    ["path", { d: "M12 2l7 4v6c0 5-3.5 8.5-7 10-3.5-1.5-7-5-7-10V6l7-4z" }],
    ["polyline", { points: "9 12 11 14 15 10" }],
  ],
  trend: [
    ["polyline", { points: "22 7 13.5 15.5 8.5 10.5 2 17" }],
    ["polyline", { points: "16 7 22 7 22 13" }],
  ],
  trending: [
    ["polyline", { points: "22 7 13.5 15.5 8.5 10.5 2 17" }],
    ["polyline", { points: "16 7 22 7 22 13" }],
  ],
  calculator: [
    ["rect", { x: "4", y: "2", width: "16", height: "20" }],
    ["line", { x1: "8", y1: "6", x2: "16", y2: "6" }],
    ["path", { d: "M8 11h.01M12 11h.01M16 11h.01M8 15h.01M12 15h.01M16 15h.01M8 19h4" }],
  ],
  refresh: [
    ["path", { d: "M21 12a9 9 0 1 1-2.64-6.36" }],
    ["polyline", { points: "21 3 21 9 15 9" }],
  ],
  message: [
    ["path", { d: "M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" }],
  ],
  check: [
    ["path", { d: "M22 11.08V12a10 10 0 1 1-5.93-9.14" }],
    ["polyline", { points: "22 4 12 14.01 9 11.01" }],
  ],
  alert: [
    ["path", { d: "M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" }],
    ["line", { x1: "12", y1: "9", x2: "12", y2: "13" }],
    ["line", { x1: "12", y1: "17", x2: "12.01", y2: "17" }],
  ],
  bulb: [
    ["path", { d: "M9 18h6M10 22h4" }],
    ["path", { d: "M15.09 14c.18-.98.65-1.74 1.41-2.5A4.65 4.65 0 0 0 18 8 6 6 0 0 0 6 8c0 1 .23 2.23 1.5 3.5A4.61 4.61 0 0 1 8.91 14" }],
  ],
  star: [
    ["polygon", { points: "12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2" }],
  ],
  trophy: [
    ["path", { d: "M6 9H4.5a2.5 2.5 0 0 1 0-5H6" }],
    ["path", { d: "M18 9h1.5a2.5 2.5 0 0 0 0-5H18" }],
    ["path", { d: "M4 22h16" }],
    ["path", { d: "M18 2H6v7a6 6 0 0 0 12 0V2z" }],
  ],
  edit: [
    ["path", { d: "M11 4H4v16h16v-7" }],
    ["path", { d: "M18.5 2.5l3 3L12 15l-4 1 1-4 9.5-9.5z" }],
  ],
  x: [
    ["line", { x1: "18", y1: "6", x2: "6", y2: "18" }],
    ["line", { x1: "6", y1: "6", x2: "18", y2: "18" }],
  ],
  plus: [
    ["line", { x1: "12", y1: "5", x2: "12", y2: "19" }],
    ["line", { x1: "5", y1: "12", x2: "19", y2: "12" }],
  ],
  lock: [
    ["rect", { x: "4", y: "11", width: "16", height: "10" }],
    ["path", { d: "M8 11V7a4 4 0 0 1 8 0v4" }],
  ],
  unlock: [
    ["rect", { x: "4", y: "11", width: "16", height: "10" }],
    ["path", { d: "M8 11V7a4 4 0 0 1 7.6-1.8" }],
  ],
  key: [
    ["circle", { cx: "7.5", cy: "15.5", r: "4.5" }],
    ["path", { d: "M21 2l-9.6 9.6" }],
    ["path", { d: "M15.5 7.5l3 3L22 7l-3-3" }],
  ],
  terminal: [
    ["polyline", { points: "4 17 10 11 4 5" }],
    ["line", { x1: "12", y1: "19", x2: "20", y2: "19" }],
  ],
  folder: [
    ["path", { d: "M22 19H2V5h7l2 3h11v11z" }],
  ],
  monitor: [
    ["rect", { x: "2", y: "3", width: "20", height: "14" }],
    ["line", { x1: "8", y1: "21", x2: "16", y2: "21" }],
    ["line", { x1: "12", y1: "17", x2: "12", y2: "21" }],
  ],
  chart: [
    ["line", { x1: "18", y1: "20", x2: "18", y2: "10" }],
    ["line", { x1: "12", y1: "20", x2: "12", y2: "4" }],
    ["line", { x1: "6", y1: "20", x2: "6", y2: "14" }],
  ],
  layers: [
    ["polygon", { points: "12 2 2 7 12 12 22 7 12 2" }],
    ["polyline", { points: "2 12 12 17 22 12" }],
    ["polyline", { points: "2 17 12 22 22 17" }],
  ],
  pie: [
    ["path", { d: "M21.21 15.89A10 10 0 1 1 8 2.83" }],
    ["path", { d: "M22 12A10 10 0 0 0 12 2v10z" }],
  ],
  activity: [
    ["polyline", { points: "22 12 18 12 15 21 9 3 6 12 2 12" }],
  ],
  briefcase: [
    ["rect", { x: "2", y: "7", width: "20", height: "14" }],
    ["path", { d: "M16 7V4H8v3" }],
  ],
  wallet: [
    ["rect", { x: "2", y: "5", width: "20", height: "14" }],
    ["path", { d: "M16 12h6" }],
  ],
  coins: [
    ["circle", { cx: "9", cy: "12", r: "6" }],
    ["path", { d: "M14.5 6.8a6 6 0 1 1 0 10.4" }],
  ],
  mail: [
    ["rect", { x: "2", y: "4", width: "20", height: "16" }],
    ["polyline", { points: "22 6 12 13 2 6" }],
  ],
  link: [
    ["path", { d: "M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" }],
    ["path", { d: "M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" }],
  ],
  upload: [
    ["path", { d: "M21 15v4H3v-4" }],
    ["polyline", { points: "17 8 12 3 7 8" }],
    ["line", { x1: "12", y1: "3", x2: "12", y2: "15" }],
  ],
  download: [
    ["path", { d: "M21 15v4H3v-4" }],
    ["polyline", { points: "7 10 12 15 17 10" }],
    ["line", { x1: "12", y1: "15", x2: "12", y2: "3" }],
  ],
  trash: [
    ["polyline", { points: "3 6 5 6 21 6" }],
    ["path", { d: "M19 6l-1 14H6L5 6" }],
    ["path", { d: "M10 11v6M14 11v6M9 6V3h6v3" }],
  ],
  grip: [
    ["circle", { cx: "9", cy: "6", r: "1.3" }],
    ["circle", { cx: "15", cy: "6", r: "1.3" }],
    ["circle", { cx: "9", cy: "12", r: "1.3" }],
    ["circle", { cx: "15", cy: "12", r: "1.3" }],
    ["circle", { cx: "9", cy: "18", r: "1.3" }],
    ["circle", { cx: "15", cy: "18", r: "1.3" }],
  ],
  tag: [
    ["path", { d: "M20.59 13.41l-7.17 7.17-10-10V2h8.59l8.58 8.59z" }],
    ["circle", { cx: "7.5", cy: "7.5", r: "1.5" }],
  ],
  split: [
    ["path", { d: "M16 3h5v5" }],
    ["path", { d: "M8 3H3v5" }],
    ["path", { d: "M12 22v-8.3a4 4 0 0 0-1.17-2.83L3 3" }],
    ["path", { d: "M15 9l6-6" }],
  ],
  undo: [
    ["polyline", { points: "9 14 4 9 9 4" }],
    ["path", { d: "M20 20v-7a4 4 0 0 0-4-4H4" }],
  ],
  cpu: [
    ["rect", { x: "4", y: "4", width: "16", height: "16" }],
    ["rect", { x: "9", y: "9", width: "6", height: "6" }],
    ["path", { d: "M9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 15h3M1 9h3M1 15h3" }],
  ],
  database: [
    ["ellipse", { cx: "12", cy: "5", rx: "9", ry: "3" }],
    ["path", { d: "M21 12c0 1.66-4 3-9 3s-9-1.34-9-3" }],
    ["path", { d: "M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" }],
  ],
  "arrow-up": [
    ["line", { x1: "12", y1: "19", x2: "12", y2: "5" }],
    ["polyline", { points: "5 12 12 5 19 12" }],
  ],
  "arrow-down": [
    ["line", { x1: "12", y1: "5", x2: "12", y2: "19" }],
    ["polyline", { points: "19 12 12 19 5 12" }],
  ],
  "arrow-right": [
    ["line", { x1: "5", y1: "12", x2: "19", y2: "12" }],
    ["polyline", { points: "12 5 19 12 12 19" }],
  ],
  eye: [
    ["path", { d: "M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z" }],
    ["circle", { cx: "12", cy: "12", r: "3" }],
  ],
  search: [
    ["circle", { cx: "11", cy: "11", r: "7" }],
    ["line", { x1: "21", y1: "21", x2: "16.65", y2: "16.65" }],
  ],
  clock: [
    ["circle", { cx: "12", cy: "12", r: "9" }],
    ["polyline", { points: "12 7 12 12 15.5 14" }],
  ],
  calendar: [
    ["rect", { x: "3", y: "4", width: "18", height: "18" }],
    ["line", { x1: "16", y1: "2", x2: "16", y2: "6" }],
    ["line", { x1: "8", y1: "2", x2: "8", y2: "6" }],
    ["line", { x1: "3", y1: "10", x2: "21", y2: "10" }],
  ],
  filter: [
    ["polygon", { points: "22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3" }],
  ],
  play: [
    ["polygon", { points: "5 3 19 12 5 21 5 3" }],
  ],
  copy: [
    ["rect", { x: "9", y: "9", width: "13", height: "13" }],
    ["path", { d: "M5 15H4V4h11v1" }],
  ],
  info: [
    ["circle", { cx: "12", cy: "12", r: "10" }],
    ["line", { x1: "12", y1: "16", x2: "12", y2: "12" }],
    ["line", { x1: "12", y1: "8", x2: "12.01", y2: "8" }],
  ],
};

export function makeIcon(name, extraClass = "") {
  const shapes = ICON_SHAPES[name] || ICON_SHAPES.zap;
  return svgEl(
    "svg",
    {
      class: extraClass ? `ui-icon ${extraClass}` : "ui-icon",
      viewBox: "0 0 24 24",
      fill: "none",
      stroke: "currentColor",
      "stroke-width": "1.85",
      "stroke-linecap": "square",
      "stroke-linejoin": "miter",
      "aria-hidden": "true",
    },
    shapes.map(([tag, attrs]) => svgEl(tag, attrs))
  );
}

export function jarIconKey(jar) {
  if (jar && jar.icon && ICON_SHAPES[jar.icon]) return jar.icon;
  const key = String((jar && (jar.slug || jar.kind || jar.name)) || "").toLowerCase();
  if (key.includes("bill") || key.includes("essential") || key.includes("fixed")) return "home";
  if (key.includes("daily") || key.includes("living") || key.includes("flex") || key.includes("cart")) return "cart";
  if (key.includes("fun") || key.includes("guilt") || key.includes("joy") || key.includes("bolt")) return "bolt";
  if (key.includes("safe") || key.includes("future") || key.includes("buffer") || key.includes("emergency")) return "shield";
  if (key.includes("invest") || key.includes("trade") || key.includes("chronox") || key.includes("vault")) return "trend";
  return "shield";
}

export function setNodeIconText(node, iconName, text) {
  if (!node) return;
  node.replaceChildren(makeIcon(iconName, "ui-icon-sm"), document.createTextNode(String(text ?? "")));
}

/**
 * Shared spacious empty-state card immune to chart SVG sizing rules.
 */
export function renderEmptyState({
  icon = "info",
  title = "Nothing here yet",
  subtitle = "",
  actionLabel = "",
  onAction = null,
  secondaryLabel = "",
  onSecondary = null,
  compact = false,
} = {}) {
  const actionButtons = [];
  if (actionLabel && typeof onAction === "function") {
    const primaryBtn = el("button", { type: "button", class: "btn btn-primary btn-sm", text: actionLabel });
    primaryBtn.addEventListener("click", (ev) => onAction(ev));
    actionButtons.push(primaryBtn);
  }
  if (secondaryLabel && typeof onSecondary === "function") {
    const secBtn = el("button", { type: "button", class: "btn btn-soft btn-sm", text: secondaryLabel });
    secBtn.addEventListener("click", (ev) => onSecondary(ev));
    actionButtons.push(secBtn);
  }

  return el("div", { class: `empty-state${compact ? " empty-state-compact" : ""}` }, [
    el("div", { class: "empty-state-icon", "aria-hidden": "true" }, [makeIcon(icon, "ui-icon-lg")]),
    el("p", { class: "empty-state-title", text: title }),
    subtitle ? el("p", { class: "empty-state-subtitle", text: subtitle }) : null,
    actionButtons.length ? el("div", { class: "empty-state-actions" }, actionButtons) : null,
  ]);
}

function arrayBufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  const chunkSize = 0x8000;
  const parts = [];
  for (let i = 0; i < bytes.length; i += chunkSize) {
    parts.push(String.fromCharCode.apply(null, bytes.subarray(i, i + chunkSize)));
  }
  return btoa(parts.join(""));
}

function parseFilenameFromHeader(disposition, fallback) {
  if (!disposition) return fallback;
  const utf8Match = disposition.match(/filename\*=UTF-8''([^;]+)/i);
  if (utf8Match && utf8Match[1]) {
    try {
      return decodeURIComponent(utf8Match[1].trim().replace(/^["']|["']$/g, ""));
    } catch (_e) {
      // fall through
    }
  }
  const plainMatch = disposition.match(/filename=([^;]+)/i);
  if (plainMatch && plainMatch[1]) {
    return plainMatch[1].trim().replace(/^["']|["']$/g, "") || fallback;
  }
  return fallback;
}

/**
 * Authenticated export download helper supporting both Browser Blob downloads
 * and native Electron Save dialogs (UTF-8 text & binary .xlsx/.zip files).
 */
export async function downloadExportFile({
  url,
  defaultFilename = "chronox-export.xlsx",
  filters = null,
  label = "Export",
} = {}) {
  if (!url) return false;
  const resp = await fetch(url, { credentials: "same-origin" });
  if (resp.status === 401) {
    state.vaultStatus = "locked";
    triggerVaultGate("locked", "Please unlock ChronoX before exporting.");
    throw new Error("ChronoX is locked — unlock first to export.");
  }
  if (!resp.ok) {
    let errMsg = `Export failed (HTTP ${resp.status})`;
    try {
      const errData = await resp.json();
      if (errData && (errData.detail || errData.message)) {
        errMsg = String(errData.detail || errData.message);
      }
    } catch (_e) {
      // ignore non-json error body
    }
    throw new Error(errMsg);
  }

  const filename = parseFilenameFromHeader(resp.headers.get("content-disposition"), defaultFilename);
  const lowerName = filename.toLowerCase();
  const isBinary =
    lowerName.endsWith(".xlsx") ||
    lowerName.endsWith(".zip") ||
    lowerName.endsWith(".numbers") ||
    lowerName.endsWith(".pdf");

  const desktop = getDesktopBridge();
  if (desktop && typeof desktop.saveFileDialog === "function") {
    const dialogFilters =
      filters ||
      (lowerName.endsWith(".xlsx")
        ? [{ name: "Apple Numbers / Excel Workbook", extensions: ["xlsx"] }]
        : lowerName.endsWith(".zip")
        ? [{ name: "CSV Export Pack (ZIP)", extensions: ["zip"] }]
        : lowerName.endsWith(".csv")
        ? [{ name: "CSV Spreadsheet", extensions: ["csv"] }]
        : [{ name: "All Files", extensions: ["*"] }]);

    let savePayload;
    if (isBinary) {
      const buf = await resp.arrayBuffer();
      savePayload = {
        defaultName: filename,
        isBinary: true,
        contentBase64: arrayBufferToBase64(buf),
        filters: dialogFilters,
      };
    } else {
      const txt = await resp.text();
      savePayload = {
        defaultName: filename,
        content: txt,
        filters: dialogFilters,
      };
    }
    const res = await desktop.saveFileDialog(savePayload);
    if (res && !res.canceled) {
      if (res.error) throw new Error(res.error);
      toast(`Saved ${label}${res.filePath ? ` to ${res.filePath}` : "."}`);
      return true;
    }
    return false;
  }

  const blob = await resp.blob();
  const objUrl = URL.createObjectURL(blob);
  try {
    const a = el("a", { href: objUrl, download: filename, style: { display: "none" } });
    document.body.appendChild(a);
    a.click();
    a.remove();
  } finally {
    setTimeout(() => URL.revokeObjectURL(objUrl), 2500);
  }
  toast(`Downloaded ${label} (${filename}).`);
  return true;
}

export function renderFormattedText(raw) {
  const frag = document.createDocumentFragment();
  const str = String(raw ?? "")
    .replace(/[\u{1F300}-\u{1FAFF}]/gu, "")
    .replace(/[\u2600-\u27BF]/g, "")
    .replace(/\uFE0F/g, "");
  const pattern = /\*\*([^*]+)\*\*/g;
  let lastIdx = 0;
  let match;
  while ((match = pattern.exec(str)) !== null) {
    if (match.index > lastIdx) {
      frag.appendChild(document.createTextNode(str.slice(lastIdx, match.index)));
    }
    frag.appendChild(el("strong", { text: match[1] }));
    lastIdx = pattern.lastIndex;
  }
  if (lastIdx < str.length) {
    frag.appendChild(document.createTextNode(str.slice(lastIdx)));
  }
  return frag;
}

/* --- Toasts with Optional Undo Action --- */

export function toast(message, optionsOrIsError = false) {
  const region = $("toast-region");
  if (!region) return;

  const opts =
    typeof optionsOrIsError === "object" && optionsOrIsError !== null
      ? optionsOrIsError
      : { isError: Boolean(optionsOrIsError) };

  const isError = Boolean(opts.isError);
  const hasUndo = typeof opts.onUndo === "function";
  const durationMs =
    Number(opts.durationMs) || (hasUndo ? 8000 : isError ? 5200 : 4000);

  const children = [
    el("span", { class: "inline-icon-row toast-msg" }, [
      makeIcon(isError ? "alert" : "check", "ui-icon-sm"),
      String(message ?? ""),
    ]),
  ];

  const item = el("div", { class: `toast${isError ? " toast-err" : ""}` }, children);

  let timer = null;
  const dismiss = () => {
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
    item.remove();
  };

  if (hasUndo) {
    const undoBtn = el(
      "button",
      {
        type: "button",
        class: "btn btn-soft btn-xs toast-undo-btn",
      },
      [makeIcon("undo", "ui-icon-sm"), opts.undoLabel || "Undo"]
    );
    undoBtn.addEventListener("click", async () => {
      undoBtn.disabled = true;
      try {
        await opts.onUndo();
      } catch (err) {
        toast(err && err.message ? err.message : "Undo failed", true);
      } finally {
        dismiss();
      }
    });
    item.appendChild(undoBtn);
  }

  region.appendChild(item);
  timer = setTimeout(dismiss, durationMs);
  return { dismiss };
}

/* --- Busy Button Wrapper --- */

export async function withBusy(btn, fn) {
  if (!btn) return fn();
  if (btn.disabled) return undefined;
  btn.disabled = true;
  btn.classList.add("is-busy");
  try {
    return await fn();
  } finally {
    btn.disabled = false;
    btn.classList.remove("is-busy");
  }
}

/* --- Overlays & Drawers --- */

export function openOverlay(overlay) {
  if (!overlay) return;
  const key = overlay.id || "overlay";
  if (state.modalTimers[key]) {
    clearTimeout(state.modalTimers[key]);
    delete state.modalTimers[key];
  }
  overlay.classList.remove("hidden", "is-closing");
  if (prefersReducedMotion()) {
    overlay.classList.add("is-open");
    return;
  }
  requestAnimationFrame(() => {
    overlay.classList.add("is-open");
  });
}

export function closeOverlay(overlay) {
  if (!overlay || overlay.classList.contains("hidden")) return;
  const key = overlay.id || "overlay";
  if (state.modalTimers[key]) {
    clearTimeout(state.modalTimers[key]);
    delete state.modalTimers[key];
  }
  if (prefersReducedMotion()) {
    overlay.classList.remove("is-open", "is-closing");
    overlay.classList.add("hidden");
    return;
  }
  overlay.classList.remove("is-open");
  overlay.classList.add("is-closing");
  state.modalTimers[key] = setTimeout(() => {
    overlay.classList.add("hidden");
    overlay.classList.remove("is-closing");
    delete state.modalTimers[key];
  }, 150);
}

export function openDrawer(drawerId) {
  const overlay = $(drawerId);
  if (!overlay) return;
  if (state.activeDrawer && state.activeDrawer !== drawerId) {
    closeOverlay($(state.activeDrawer));
  }
  state.activeDrawer = drawerId;
  openOverlay(overlay);
}

export function closeDrawer(drawerId) {
  const targetId = drawerId || state.activeDrawer;
  if (!targetId) return;
  closeOverlay($(targetId));
  if (state.activeDrawer === targetId) {
    state.activeDrawer = null;
  }
}

/* --- CSRF-Aware Fetch API Client --- */

const MUTATING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);
const AUTH_EXEMPT_PATHS = [
  "/api/v1/auth/status",
  "/api/v1/auth/setup",
  "/api/v1/auth/login",
  "/api/v1/auth/recover",
  "/api/auth/status",
  "/api/auth/setup",
  "/api/auth/login",
  "/api/auth/recover",
];

async function ensureCsrfToken() {
  if (state.csrfToken) return state.csrfToken;
  try {
    const resp = await fetch("/api/v1/auth/status", { credentials: "same-origin" });
    if (resp.ok) {
      const st = await resp.json();
      if (st && st.csrf_token) {
        state.csrfToken = st.csrf_token;
      }
      if (st && st.state) {
        state.vaultStatus = st.state;
      }
    }
  } catch (_err) {
    // Non-fatal; caller request will surface exact network/auth error
  }
  return state.csrfToken;
}

export async function api(path, options = {}) {
  const method = String(options.method || "GET").toUpperCase();
  const headers = Object.assign({}, options.headers || {});
  const isAuthExempt = AUTH_EXEMPT_PATHS.some((p) => path.startsWith(p));

  if (options.body && !(options.body instanceof FormData) && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }

  if (MUTATING_METHODS.has(method)) {
    if (!state.csrfToken && !isAuthExempt) {
      await ensureCsrfToken();
    }
    if (state.csrfToken && !headers["X-ChronoX-CSRF"]) {
      headers["X-ChronoX-CSRF"] = state.csrfToken;
    }
  }

  const isAiSlowEndpoint =
    path.startsWith("/api/v1/ai/say") || path.startsWith("/api/v1/optimize/one-tap");
  const timeoutMs =
    typeof options.timeoutMs === "number"
      ? options.timeoutMs
      : isAiSlowEndpoint
      ? 45000
      : 0;
  let controller = null;
  let timeoutId = null;
  if (timeoutMs > 0 && !options.signal && typeof AbortController === "function") {
    controller = new AbortController();
    timeoutId = setTimeout(() => controller.abort(), timeoutMs);
  }

  let resp;
  try {
    resp = await fetch(path, {
      credentials: "same-origin",
      ...options,
      method,
      headers,
      signal: options.signal || (controller ? controller.signal : undefined),
    });
  } catch (fetchErr) {
    if (fetchErr && fetchErr.name === "AbortError") {
      const timeoutErr = new Error(
        "Request timed out after 45s. Your local AI may be busy — try again or switch to Built-in Math."
      );
      timeoutErr.status = 504;
      throw timeoutErr;
    }
    throw fetchErr;
  } finally {
    if (timeoutId !== null) clearTimeout(timeoutId);
  }

  const respCsrf = resp.headers.get("X-ChronoX-CSRF");
  if (respCsrf) setCsrfToken(respCsrf);

  const ct = resp.headers.get("content-type") || "";
  const data = ct.includes("application/json")
    ? await resp.json().catch(() => null)
    : await resp.text();

  if (data && typeof data === "object" && data.csrf_token) {
    setCsrfToken(data.csrf_token);
  }

  const errCode = data && typeof data === "object" ? String(data.code || "") : "";
  const isVault401 =
    resp.status === 401 &&
    !isAuthExempt &&
    (!errCode ||
      errCode === "vault_auth_required" ||
      errCode === "vault_not_initialized" ||
      errCode === "invalid_credentials");

  if (isVault401) {
    const mode =
      data && typeof data === "object" && data.state === "needs_setup"
        ? "needs_setup"
        : "locked";
    state.vaultStatus = mode;
    triggerVaultGate(mode, (data && data.detail) || "Vault locked — enter master password.");
    const err = new Error((data && data.detail) || "Vault locked — please unlock ChronoX.");
    err.status = 401;
    err.data = data;
    throw err;
  }

  if (!resp.ok) {
    const msg =
      (data && typeof data === "object" && (data.detail || data.message)) ||
      (typeof data === "string" && data.trim()) ||
      `HTTP ${resp.status}`;
    const err = new Error(String(msg));
    err.status = resp.status;
    err.data = data;
    throw err;
  }

  return data;
}

/* --- Skeletons & Entrances --- */

export function buildSkeletonCard(lines = 3) {
  const children = [
    el("div", { class: "item-row" }, [
      el("div", { class: "skeleton-line w-md" }),
      el("div", { class: "skeleton-pill" }),
    ]),
    el("div", { class: "skeleton-line h-lg" }),
  ];
  for (let i = 0; i < lines - 1; i++) {
    children.push(el("div", { class: `skeleton-line ${i % 2 === 0 ? "w-lg" : "w-sm"}` }));
  }
  return el("div", { class: "skeleton-card", "aria-hidden": "true" }, children);
}

export function showBootSkeletons() {
  if (state.hydrated) return;
  document.body.classList.add("is-loading");
  const jarsGrid = $("jars-grid");
  if (jarsGrid && !jarsGrid.childElementCount) {
    jarsGrid.replaceChildren(
      buildSkeletonCard(3),
      buildSkeletonCard(3),
      buildSkeletonCard(3),
      buildSkeletonCard(3),
      buildSkeletonCard(3)
    );
  }
  const billList = $("bill-radar-list");
  if (billList && !billList.childElementCount) {
    billList.replaceChildren(buildSkeletonCard(2), buildSkeletonCard(2));
  }
}

export function clearBootSkeletons() {
  document.body.classList.remove("is-loading");
}

export function choreographEntrance(container) {
  if (!container || prefersReducedMotion()) return;
  const cards = Array.from(container.querySelectorAll(".card, .jar-card, .stat-tile")).filter(
    (node) => !node.classList.contains("hidden") && !node.closest(".hidden")
  );
  if (!cards.length) return;
  cards.forEach((node) => node.classList.remove("stagger-enter"));
  void container.offsetWidth;
  cards.forEach((node, idx) => {
    const delayMs = Math.min(idx, 8) * 20;
    node.style.setProperty("--stagger-delay", `${delayMs}ms`);
    node.classList.add("stagger-enter");
    const cleanup = () => {
      node.classList.remove("stagger-enter");
      node.style.removeProperty("--stagger-delay");
    };
    node.addEventListener("animationend", cleanup, { once: true });
    setTimeout(cleanup, delayMs + 260);
  });
}

export function initCardSheen() {
  // Industrial design uses crisp mechanical hairline top accents rather than soft radial blobs.
}

/* --- Industrial Theme (Dark Industrial default + Optional High-Contrast Light Steel) --- */

export function applyTheme(theme, persist = false, opts = {}) {
  const resolved = theme === "light" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", resolved);
  if (persist) {
    try {
      localStorage.setItem("chronox-theme", resolved);
    } catch (_err) {
      // ignore storage errors
    }
  }
  const isLight = resolved === "light";
  const sun = $("theme-icon-sun");
  const moon = $("theme-icon-moon");
  const btn = $("btn-theme-toggle");
  if (sun) sun.classList.toggle("hidden", !isLight);
  if (moon) moon.classList.toggle("hidden", isLight);
  if (btn) {
    btn.setAttribute("aria-pressed", String(isLight));
    btn.title = isLight ? "Switch to Dark Industrial theme" : "Switch to Light Steel theme";
  }

  const desktop = getDesktopBridge();
  if (desktop && persist && !opts.fromDesktop) {
    if (typeof desktop.setThemeOverlay === "function") {
      Promise.resolve(desktop.setThemeOverlay(resolved)).catch(() => {});
    }
    if (!opts.skipTraySync) {
      pushTraySnapshot();
    }
  }
}

export function initTheme() {
  let saved = null;
  try {
    saved = localStorage.getItem("chronox-theme");
  } catch (_err) {
    saved = null;
  }
  applyTheme(saved === "light" ? "light" : "dark", false);
}

export function toggleTheme() {
  const cur = document.documentElement.getAttribute("data-theme") || "dark";
  const next = cur === "dark" ? "light" : "dark";
  applyTheme(next, true);
  toast(next === "light" ? "Light Steel theme active" : "Dark Industrial theme active");
}

/* --- Intelligence Mode & Provider Sync --- */

export function syncModeControls(mode) {
  const normalized = ["deterministic_only", "hybrid", "ai_standalone"].includes(mode)
    ? mode
    : "hybrid";
  document.querySelectorAll("[data-set-mode]").forEach((btn) => {
    const active = btn.dataset.setMode === normalized;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", String(active));
  });
  const desc = $("settings-mode-desc");
  if (desc) desc.textContent = MODE_DESCRIPTIONS[normalized] || MODE_DESCRIPTIONS.hybrid;
}

const VALID_PROVIDERS = new Set([
  "auto",
  "claude_cli",
  "opencode_cli",
  "codex_cli",
  "agy_cli",
  "opencode_go",
  "gemini",
  "openai_codex",
  "chat_wrapper",
]);

export const PROVIDER_DISPLAY_NAMES = {
  auto: "Auto",
  claude_cli: "Claude (Local)",
  opencode_cli: "OpenCode (Local)",
  codex_cli: "Codex (Local)",
  agy_cli: "Antigravity (Local)",
  opencode_go: "OpenCode Cloud",
  gemini: "Google Gemini",
  openai_codex: "OpenAI",
  chat_wrapper: "Custom Gateway",
  deterministic_local: "Built-in Math",
  none: "Built-in Math",
};

export function syncProviderControls(provider, activeResolved) {
  const normalized = VALID_PROVIDERS.has(provider) ? provider : "auto";
  document.querySelectorAll("[data-set-provider]").forEach((btn) => {
    const active = btn.dataset.setProvider === normalized;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", String(active));
  });

  const badge = $("active-provider-badge");
  if (!badge) return;
  const map = {
    claude_cli: ["pill-calm", "Active: Claude (Local)"],
    opencode_cli: ["pill-calm", "Active: OpenCode (Local)"],
    codex_cli: ["pill-calm", "Active: Codex (Local)"],
    agy_cli: ["pill-blue", "Active: Antigravity (Local)"],
    opencode_go: ["pill-calm", "Active: OpenCode Cloud"],
    gemini: ["pill-blue", "Active: Google Gemini"],
    openai_codex: ["pill-calm", "Active: OpenAI"],
    chat_wrapper: ["pill-blue", "Active: Custom Gateway"],
  };
  const [cls, label] = map[activeResolved] || ["pill-soft", "Active: Built-in Math"];
  badge.className = `pill ${cls}`;
  badge.textContent = label;
}

export function formatProvenance(res) {
  const source = (res && res.source) || "deterministic_local";
  const mode = (res && res.intelligence_mode) || "hybrid";
  const provider = (res && res.provider_used) || null;
  const model = (res && res.model_used) || "";
  const cachedNote = res && res.cached ? " · cached" : "";

  const labelMap = {
    claude_cli: "Claude (Local)",
    opencode_cli: "OpenCode (Local)",
    codex_cli: "Codex (Local)",
    agy_cli: "Antigravity (Local)",
    gemini: "Google Gemini",
    opencode_go: "OpenCode Cloud",
    openai_codex: "OpenAI",
    chat_wrapper: "Custom Gateway",
  };
  const providerLabel = labelMap[provider] || labelMap[source] || null;

  if (mode === "deterministic_only" || !providerLabel) {
    return {
      icon: "calculator",
      text: `Built-in Math${cachedNote}`,
      cls: "pill-soft",
    };
  }
  const modeTag = mode === "ai_standalone" ? "AI Direct" : "Verified AI";
  const modelShort = model ? ` (${model})` : "";
  return {
    icon: mode === "ai_standalone" ? "sparkles" : "zap",
    text: `${modeTag} · ${providerLabel}${modelShort}${cachedNote}`,
    cls: provider === "gemini" || provider === "chat_wrapper" || provider === "agy_cli" ? "pill-blue" : "pill-calm",
  };
}

export async function setIntelligenceMode(mode) {
  try {
    const updated = await api("/api/v1/settings", {
      method: "PATCH",
      body: JSON.stringify({ intelligence_mode: mode }),
    });
    state.settings = updated;
    syncModeControls(updated.intelligence_mode);
    syncProviderControls(updated.ai_provider, updated.active_provider_resolved);
    const label =
      mode === "deterministic_only"
        ? "Built-in Math (Offline)"
        : mode === "ai_standalone"
        ? "AI Direct"
        : "Auto (Math + AI)";
    toast(`Smart Mode: ${label}`);
    pushTraySnapshot();
    await refreshAll({ refreshAI: true });
  } catch (err) {
    toast(err.message, true);
  }
}

export async function setPreferredProvider(provider) {
  try {
    const updated = await api("/api/v1/settings", {
      method: "PATCH",
      body: JSON.stringify({ ai_provider: provider }),
    });
    state.settings = updated;
    syncProviderControls(updated.ai_provider, updated.active_provider_resolved);
    const friendly = PROVIDER_DISPLAY_NAMES[provider] || provider.replace(/_/g, " ");
    toast(`AI Provider set to ${friendly}`);
    await refreshAll({ refreshAI: true });
  } catch (err) {
    toast(err.message, true);
  }
}

/* --- 8-Tab Navigation & Mechanical Indicator --- */

export function positionTabIndicator(immediate = false) {
  const bar = $("main-tabbar");
  const indicator = $("tab-indicator");
  if (!bar || !indicator) return;
  const activeBtn = bar.querySelector(".tab.active");
  if (!activeBtn) return;

  const left = activeBtn.offsetLeft;
  const width = activeBtn.offsetWidth;
  if (width <= 0) return;

  bar.classList.add("has-indicator");
  const snap = immediate || prefersReducedMotion();
  if (snap) indicator.classList.add("no-transition");
  indicator.style.transform = `translate3d(${left}px, 0, 0)`;
  indicator.style.width = `${width}px`;
  indicator.classList.add("ready");
  if (snap) {
    void indicator.offsetWidth;
    indicator.classList.remove("no-transition");
  }
}

export function initTabIndicatorObserver() {
  const bar = $("main-tabbar");
  if (!bar) return;
  positionTabIndicator(true);
  requestAnimationFrame(() => positionTabIndicator(true));
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(() => positionTabIndicator(true)).catch(() => {});
  }
  window.addEventListener("resize", () => positionTabIndicator(true));
  if (typeof ResizeObserver === "function") {
    const ro = new ResizeObserver(() => positionTabIndicator(true));
    ro.observe(bar);
  }
}

export function switchTab(tabName) {
  // Map legacy drawer tab requests cleanly
  if (tabName === "impulse") {
    openDrawer("impulse-drawer-overlay");
    return;
  }
  if (tabName === "reset") {
    openDrawer("reset-drawer-overlay");
    return;
  }
  const target = VALID_TABS.has(tabName) ? tabName : "today";
  state.activeTab = target;

  document.querySelectorAll(".tab[data-tab]").forEach((btn) => {
    const active = btn.dataset.tab === target;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", String(active));
  });

  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.classList.toggle("active", panel.id === `tab-${target}`);
  });

  const subtitle = $("titlebar-view-subtitle");
  if (subtitle) {
    subtitle.textContent = TAB_TITLES[target] || "Command";
  }

  positionTabIndicator(false);
  const activePanel = $(`tab-${target}`);
  if (activePanel && state.hydrated) {
    choreographEntrance(activePanel);
  }

  const hooks = _tabHooks.get(target);
  if (hooks) {
    for (const fn of hooks) {
      Promise.resolve(fn()).catch(() => {});
    }
  }
}

/* --- Electron Desktop Bridge --- */

export function getDesktopBridge() {
  return window.chronoxDesktop || null;
}

export function formatShortcutLabel(raw, platformKey) {
  if (!raw) return platformKey === "darwin" ? "Cmd+Shift+Space" : "Ctrl+Shift+Space";
  const str = String(raw).trim();
  if (platformKey === "darwin") {
    return str.replace(/CommandOrControl/gi, "Cmd");
  }
  return str.replace(/CommandOrControl/gi, "Ctrl");
}

export function setVaultTitlebarState(status, label) {
  const dot = $("titlebar-vault-dot");
  const text = $("titlebar-vault-label");
  if (!dot || !text) return;
  dot.classList.toggle("is-syncing", status === "syncing");
  dot.classList.toggle("is-locked", status === "locked");
  if (label) text.textContent = label;
}

function updateMaximizeButtonState(isMaximized) {
  const maxBtn = $("btn-win-maximize");
  const maxIco = $("win-icon-maximize");
  const restIco = $("win-icon-restore");
  if (!maxBtn || !maxIco || !restIco) return;
  maxIco.classList.toggle("hidden", isMaximized);
  restIco.classList.toggle("hidden", !isMaximized);
  maxBtn.title = isMaximized ? "Restore" : "Maximize";
  maxBtn.setAttribute("aria-label", isMaximized ? "Restore window" : "Maximize window");
}

export function pushTraySnapshot() {
  const desktop = getDesktopBridge();
  if (!desktop || typeof desktop.updateTraySnapshot !== "function") return;
  const dash = state.dashboard;
  const s = dash && dash.safe_to_spend;
  const payload = {
    theme: document.documentElement.getAttribute("data-theme") || "dark",
    mode:
      (state.settings && state.settings.intelligence_mode) ||
      (dash && dash.intelligence_mode) ||
      "hybrid",
    locked: state.vaultStatus !== "unlocked",
  };
  if (s && state.vaultStatus === "unlocked") {
    const dailySafeCents = Number(s.daily_safe_to_spend_cents) || 0;
    const safeToSpendCents = Number(s.safe_to_spend_cents) || 0;
    const daysUntilPayday = Number(s.days_until_payday) || 0;
    payload.dailySafe = fmtMoney(dailySafeCents);
    payload.dailySafeCents = dailySafeCents;
    payload.safeToSpendCents = safeToSpendCents;
    payload.daysUntilPayday = daysUntilPayday;
  }
  Promise.resolve(desktop.updateTraySnapshot(payload)).catch(() => {});
}

export async function loadBackendLogs() {
  const desktop = getDesktopBridge();
  const pre = $("desktop-logs-pre");
  const meta = $("desktop-logs-meta");
  const pathBar = $("desktop-logs-path");
  if (!pre || !meta || !pathBar) return;
  if (!desktop || typeof desktop.getBackendLogs !== "function") {
    pre.textContent = "Backend sidecar logs are available inside the ChronoX Desktop app.";
    return;
  }
  pre.textContent = "Loading local sidecar diagnostics...";
  try {
    const res = await desktop.getBackendLogs();
    const lines = Array.isArray(res && res.lines) ? res.lines : [];
    const mode =
      (res && res.backendMode) || (state.desktopInfo && state.desktopInfo.backendMode) || "local";
    const pid = res && res.pid ? `PID ${res.pid}` : "active";
    meta.textContent = `${mode} · ${pid} · ${lines.length} lines`;
    const urlStr =
      (res && res.backendUrl) || (state.desktopInfo && state.desktopInfo.backendUrl) || "";
    const dbStr = (state.desktopInfo && state.desktopInfo.dbPath) || "";
    pathBar.textContent =
      [urlStr ? `Sidecar: ${urlStr}` : null, dbStr ? `Vault DB: ${dbStr}` : null]
        .filter(Boolean)
        .join("  ·  ") || "Sanitized local Python sidecar output.";

    if (!lines.length) {
      state.lastLogsText = "(No sidecar log lines recorded yet.)";
      pre.textContent = state.lastLogsText;
    } else {
      const formatted = lines
        .map((entry) => {
          if (typeof entry === "string") return entry;
          const ts = entry.ts ? `[${entry.ts}] ` : "";
          const stream = entry.stream ? `[${entry.stream}] ` : "";
          return `${ts}${stream}${entry.text ?? ""}`;
        })
        .join("\n");
      state.lastLogsText = formatted;
      pre.textContent = formatted;
      pre.scrollTop = pre.scrollHeight;
    }
  } catch (err) {
    state.lastLogsText = `Unable to read backend logs: ${err.message}`;
    pre.textContent = state.lastLogsText;
  }
}

export function openBackendLogsModal() {
  state.logsOpen = true;
  openOverlay($("desktop-logs-overlay"));
  loadBackendLogs();
  const pre = $("desktop-logs-pre");
  if (pre) pre.focus();
}

export function closeBackendLogsModal() {
  state.logsOpen = false;
  closeOverlay($("desktop-logs-overlay"));
}

async function copyBackendLogs() {
  const info = state.desktopInfo;
  const header = info
    ? `ChronoX Finance Desktop v${info.appVersion || "1.0"} (${info.platform || "unknown"} ${info.arch || ""}) · Electron ${info.electronVersion || "--"}\n`
    : "ChronoX Finance Desktop Diagnostics\n";
  const pre = $("desktop-logs-pre");
  const text = `${header}${"-".repeat(64)}\n${state.lastLogsText || (pre && pre.textContent) || ""}`;
  try {
    if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = el("textarea", { value: text, style: { position: "fixed", opacity: "0" } });
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
    }
    toast("Copied backend diagnostics to clipboard.");
  } catch (err) {
    toast(`Could not copy logs: ${err.message}`, true);
  }
}

export async function initDesktopBridge() {
  const desktop = getDesktopBridge();
  if (!desktop) return;

  const normalizePlatform = (raw) => {
    const p = String(raw || "").toLowerCase();
    return p.startsWith("win") ? "win" : p === "darwin" ? "darwin" : "linux";
  };
  let platKey = normalizePlatform(desktop.platform);
  state.desktopPlatform = platKey;

  document.body.classList.add("is-desktop", `platform-${platKey}`);
  const titlebar = $("desktop-titlebar");
  const settingsCard = $("desktop-settings-card");
  const importBtn = $("btn-desktop-import-dialog");
  const winCtrls = $("desktop-win-controls");
  const hudKbd = $("desktop-hud-kbd");
  if (titlebar) titlebar.classList.remove("hidden");
  if (settingsCard) settingsCard.classList.remove("hidden");
  if (importBtn) importBtn.classList.remove("hidden");
  if (winCtrls) winCtrls.classList.toggle("hidden", platKey !== "linux");
  if (hudKbd) hudKbd.textContent = formatShortcutLabel("", platKey);

  try {
    if (typeof desktop.getInfo === "function") {
      const info = await desktop.getInfo();
      state.desktopInfo = info || {};
      if (info && info.platform) {
        const nextPlat = normalizePlatform(info.platform);
        if (nextPlat !== platKey) {
          document.body.classList.remove(`platform-${platKey}`);
          platKey = nextPlat;
          state.desktopPlatform = platKey;
          document.body.classList.add(`platform-${platKey}`);
          if (winCtrls) winCtrls.classList.toggle("hidden", platKey !== "linux");
        }
      }

      const shortcutStr = formatShortcutLabel(
        (info && (info.shortcut || (info.prefs && info.prefs.quickCaptureShortcut))) || "",
        platKey
      );
      if (hudKbd) hudKbd.textContent = shortcutStr;

      const platLabel = platKey === "win" ? "Windows" : platKey === "darwin" ? "macOS" : "Linux";
      const verStr = info && info.appVersion ? `v${info.appVersion}` : "Desktop";
      const archStr = info && info.arch ? ` · ${info.arch}` : "";
      const verPill = $("desktop-version-pill");
      if (verPill) verPill.textContent = `${verStr} · ${platLabel}${archStr}`;

      const prefs = (info && info.prefs) || {};
      const cbTray = $("desktop-pref-close-to-tray");
      const cbLogin = $("desktop-pref-launch-login");
      const cbWatch = $("desktop-pref-watch-enabled");
      const selWatch = $("desktop-watch-institution");
      if (cbTray) cbTray.checked = Boolean(prefs.closeToTray);
      if (cbLogin) cbLogin.checked = Boolean(prefs.launchAtLogin);
      if (cbWatch) cbWatch.checked = Boolean(prefs.watchFolderEnabled);
      if (selWatch) selWatch.value = prefs.watchFolderInstitution || "auto";

      if (prefs.theme === "light" || prefs.theme === "dark") {
        applyTheme(prefs.theme, true, { fromDesktop: true });
      }
    }
  } catch (_err) {
    // Platform classes already applied
  }

  const renderWatchFolderStatus = (st) => {
    const bar = $("desktop-watch-status-bar");
    if (!bar) return;
    if (!st || (!st.folderPath && !st.enabled && !st.lastError)) {
      bar.classList.add("hidden");
      return;
    }
    if (st.lastError) {
      setNodeIconText(bar, "alert", st.lastError);
      bar.classList.remove("hidden");
      return;
    }
    const stateTxt = st.watching ? "Watching" : st.enabled ? "Enabled (pick folder)" : "Paused";
    const lastTxt = st.lastImportSummary
      ? ` · Last: ${st.lastImportSummary.filename} (${st.lastImportSummary.importedCount} new)`
      : "";
    setNodeIconText(
      bar,
      st.watching ? "check" : "folder",
      `${stateTxt}: ${st.folderPath || "No folder chosen"}${lastTxt}`
    );
    bar.classList.remove("hidden");
  };

  if (typeof desktop.getWatchFolderStatus === "function") {
    Promise.resolve(desktop.getWatchFolderStatus())
      .then((st) => renderWatchFolderStatus(st))
      .catch(() => {});
  }

  if (typeof desktop.isMaximized === "function") {
    Promise.resolve(desktop.isMaximized())
      .then((max) => updateMaximizeButtonState(Boolean(max)))
      .catch(() => {});
  }

  const bindClick = (id, handler) => {
    const node = $(id);
    if (node) node.addEventListener("click", handler);
  };

  bindClick("btn-win-minimize", () => {
    if (typeof desktop.minimize === "function") desktop.minimize();
  });
  bindClick("btn-win-maximize", async () => {
    if (typeof desktop.maximizeToggle !== "function") return;
    try {
      const res = await desktop.maximizeToggle();
      if (res && typeof res.maximized === "boolean") updateMaximizeButtonState(res.maximized);
    } catch (_err) {
      // ignore
    }
  });
  bindClick("btn-win-close", () => {
    if (typeof desktop.close === "function") desktop.close();
  });

  if (titlebar) {
    titlebar.addEventListener("dblclick", async (e) => {
      if (e.target.closest("button, a, input, kbd")) return;
      if (typeof desktop.maximizeToggle === "function") {
        try {
          const res = await desktop.maximizeToggle();
          if (res && typeof res.maximized === "boolean") updateMaximizeButtonState(res.maximized);
        } catch (_err) {
          // ignore
        }
      }
    });
  }

  const openHud = () => {
    if (typeof desktop.toggleQuickCapture === "function") {
      Promise.resolve(desktop.toggleQuickCapture()).catch((err) => toast(err.message, true));
    }
  };
  bindClick("btn-desktop-hud", openHud);
  bindClick("btn-settings-open-hud", openHud);

  const cbTray = $("desktop-pref-close-to-tray");
  if (cbTray) {
    cbTray.addEventListener("change", async (e) => {
      const nextVal = e.target.checked;
      if (typeof desktop.setPrefs !== "function") return;
      try {
        const res = await desktop.setPrefs({ closeToTray: nextVal });
        if (res && res.prefs && typeof res.prefs.closeToTray === "boolean") {
          e.target.checked = res.prefs.closeToTray;
        }
        toast(nextVal ? "ChronoX will stay in the system tray on close." : "Closing will quit ChronoX.");
      } catch (err) {
        e.target.checked = !nextVal;
        toast(err.message, true);
      }
    });
  }

  const cbLogin = $("desktop-pref-launch-login");
  if (cbLogin) {
    cbLogin.addEventListener("change", async (e) => {
      const nextVal = e.target.checked;
      if (typeof desktop.setPrefs !== "function") return;
      try {
        const res = await desktop.setPrefs({ launchAtLogin: nextVal });
        if (res && res.prefs && typeof res.prefs.launchAtLogin === "boolean") {
          e.target.checked = res.prefs.launchAtLogin;
        }
        toast(nextVal ? "Launch at login enabled." : "Launch at login disabled.");
      } catch (err) {
        e.target.checked = !nextVal;
        toast(err.message, true);
      }
    });
  }

  const cbWatch = $("desktop-pref-watch-enabled");
  if (cbWatch) {
    cbWatch.addEventListener("change", async (e) => {
      const nextVal = e.target.checked;
      if (typeof desktop.setPrefs !== "function") return;
      try {
        const curStatus =
          typeof desktop.getWatchFolderStatus === "function"
            ? await desktop.getWatchFolderStatus()
            : null;
        if (nextVal && (!curStatus || !curStatus.folderPath) && typeof desktop.pickWatchFolder === "function") {
          const picked = await desktop.pickWatchFolder();
          if (!picked || picked.canceled) {
            e.target.checked = false;
            return;
          }
        }
        const res = await desktop.setPrefs({ watchFolderEnabled: nextVal });
        renderWatchFolderStatus(res && res.watchStatus);
        toast(nextVal ? "Auto-Import Drop Folder enabled." : "Auto-Import Drop Folder paused.");
      } catch (err) {
        e.target.checked = !nextVal;
        toast(err.message, true);
      }
    });
  }

  const selWatch = $("desktop-watch-institution");
  if (selWatch) {
    selWatch.addEventListener("change", async (e) => {
      if (typeof desktop.setPrefs !== "function") return;
      try {
        const res = await desktop.setPrefs({ watchFolderInstitution: e.target.value });
        renderWatchFolderStatus(res && res.watchStatus);
        toast("Updated Drop Folder profile.");
      } catch (err) {
        toast(err.message, true);
      }
    });
  }

  bindClick("btn-desktop-watch-pick", async () => {
    if (typeof desktop.pickWatchFolder !== "function") return;
    try {
      const res = await desktop.pickWatchFolder();
      if (!res || res.canceled) return;
      renderWatchFolderStatus(res.status);
      toast(`Drop folder set: ${res.folderPath}`);
    } catch (err) {
      toast(err.message, true);
    }
  });

  bindClick("btn-desktop-watch-scan", async () => {
    if (typeof desktop.scanWatchFolderNow !== "function") return;
    try {
      const res = await desktop.scanWatchFolderNow();
      if (res && res.status) renderWatchFolderStatus(res.status);
      if (res && res.locked) {
        toast("Unlock ChronoX Vault first to scan the drop folder.", true);
      } else if (res && res.ok) {
        toast(`Scanned drop folder: ${res.processed} file(s), ${res.importedCount} new tx.`);
        if (res.importedCount > 0) await refreshAll();
      } else {
        toast((res && res.error) || "Choose a drop folder first.", true);
      }
    } catch (err) {
      toast(err.message, true);
    }
  });

  bindClick("btn-desktop-watch-open", async () => {
    if (typeof desktop.openWatchFolder !== "function") return;
    try {
      const res = await desktop.openWatchFolder();
      if (res && !res.ok && res.error) toast(res.error, true);
    } catch (err) {
      toast(err.message, true);
    }
  });

  const openFolder = async () => {
    if (typeof desktop.openDataDir !== "function") return;
    try {
      await desktop.openDataDir();
    } catch (err) {
      toast(err.message, true);
    }
  };
  bindClick("btn-desktop-open-data", openFolder);
  bindClick("btn-desktop-logs-folder", openFolder);
  bindClick("btn-desktop-view-logs", openBackendLogsModal);
  bindClick("btn-desktop-logs-refresh", loadBackendLogs);
  bindClick("btn-desktop-logs-copy", copyBackendLogs);
  bindClick("btn-desktop-logs-close", closeBackendLogsModal);
  const logsOverlay = $("desktop-logs-overlay");
  if (logsOverlay) {
    logsOverlay.addEventListener("click", (e) => {
      if (e.target === logsOverlay) closeBackendLogsModal();
    });
  }

  bindClick("btn-desktop-import-dialog", async () => {
    if (typeof desktop.openStatementDialog !== "function") return;
    try {
      const res = await desktop.openStatementDialog();
      if (!res || res.canceled) return;
      if (res.error) {
        toast(res.error, true);
        return;
      }
      const fname = res.filename || "statement.csv";
      let blob;
      let mime = "text/plain;charset=utf-8";
      if (res.isBinary && res.contentBase64) {
        const binStr = atob(res.contentBase64);
        const bytes = new Uint8Array(binStr.length);
        for (let i = 0; i < binStr.length; i += 1) bytes[i] = binStr.charCodeAt(i);
        mime = fname.toLowerCase().endsWith(".pdf") ? "application/pdf" : "application/octet-stream";
        blob = new Blob([bytes], { type: mime });
      } else {
        blob = new Blob([res.content ?? ""], { type: mime });
      }
      state.desktopStatementFile = new File([blob], fname, { type: mime });
      const fileInput = $("csv-file-input");
      if (fileInput) fileInput.value = "";
      const kb = res.sizeBytes ? ` (${(res.sizeBytes / 1024).toFixed(1)} KB)` : "";
      const box = $("desktop-selected-statement");
      if (box) {
        setNodeIconText(
          box,
          "check",
          `Native file ready: ${fname}${kb} — click Preview First or Import & Auto-Update.`
        );
        box.classList.remove("hidden");
      }
    } catch (err) {
      toast(err.message, true);
    }
  });

  if (typeof desktop.saveFileDialog === "function") {
    const backupLink = $("btn-backup-export-link");
    if (backupLink) {
      backupLink.addEventListener("click", async (e) => {
        e.preventDefault();
        try {
          const data = await api("/api/v1/backup/export");
          const content = typeof data === "string" ? data : JSON.stringify(data, null, 2);
          const res = await desktop.saveFileDialog({
            defaultName: `chronox-finance-backup-${isoDateOffset(0)}.json`,
            content,
            filters: [{ name: "JSON Backup", extensions: ["json"] }],
          });
          if (res && !res.canceled) {
            if (res.error) toast(res.error, true);
            else toast(`Saved JSON backup${res.filePath ? ` to ${res.filePath}` : "."}`);
          }
        } catch (err) {
          toast(err.message, true);
        }
      });
    }
  }

  if (typeof desktop.onRefresh === "function") {
    desktop.onRefresh(async (payload) => {
      if (state.vaultStatus !== "unlocked") return;
      setVaultTitlebarState("syncing", "Syncing Vault...");
      try {
        await refreshAll();
        if (payload && payload.message) toast(payload.message);
      } catch (err) {
        toast(err.message, true);
      } finally {
        setVaultTitlebarState("active", "Local Vault Active");
      }
    });
  }
  if (typeof desktop.onNavigateTab === "function") {
    desktop.onNavigateTab((tab) => {
      if (tab) switchTab(String(tab));
    });
  }
  if (typeof desktop.onSetTheme === "function") {
    desktop.onSetTheme((theme) => {
      applyTheme(theme, true, { fromDesktop: true });
      toast(theme === "light" ? "Light Steel theme active" : "Dark Industrial theme active");
    });
  }
  if (typeof desktop.onSetMode === "function") {
    desktop.onSetMode((mode) => {
      if (mode && state.vaultStatus === "unlocked") setIntelligenceMode(String(mode));
    });
  }
  if (typeof desktop.onOpenCommandPalette === "function") {
    desktop.onOpenCommandPalette(() => {
      if (state.vaultStatus === "unlocked") openPalette();
    });
  }
  if (typeof desktop.onWindowStateChange === "function") {
    desktop.onWindowStateChange((payload) => {
      if (payload && typeof payload.maximized === "boolean") {
        updateMaximizeButtonState(payload.maximized);
      }
    });
  }
  if (typeof desktop.onTriggerSync === "function") {
    desktop.onTriggerSync(async () => {
      if (state.vaultStatus !== "unlocked") {
        triggerVaultGate(state.vaultStatus, "Unlock vault before syncing.");
        return;
      }
      setVaultTitlebarState("syncing", "Syncing Vault...");
      try {
        const res = await api("/api/v1/budget/sync", { method: "POST" });
        toast(`Auto-budget synced. Safe-to-Spend: ${fmtMoney(res.safe_to_spend_cents)}`);
        await refreshAll();
      } catch (err) {
        toast(err.message, true);
      } finally {
        setVaultTitlebarState("active", "Local Vault Active");
      }
    });
  }
  if (typeof desktop.onOpenBackendLogs === "function") {
    desktop.onOpenBackendLogs(() => {
      openBackendLogsModal();
    });
  }
}

/* --- Command Palette (Cmd+K / Ctrl+K) --- */

function getBaseCommands() {
  const curTheme = document.documentElement.getAttribute("data-theme") || "dark";
  const desktop = getDesktopBridge();
  const commands = [
    {
      id: "run-onetap",
      icon: "sparkles",
      title: "Run One-Tap Financial Optimization",
      sub: "AI + statistical cashflow check, tail-risk cushion, and 1-click plan",
      keywords: "one tap optimize plan cvar cushion ai quant",
      run: () => invokeOneTapHandler(),
    },
    {
      id: "export-numbers",
      icon: "download",
      title: "Export Apple Numbers Workbook (.xlsx)",
      sub: "Multi-sheet graphical financial workbook ready for Apple Numbers & Excel",
      keywords: "export apple numbers excel xlsx workbook report",
      run: () =>
        downloadExportFile({
          url: "/api/v1/export/numbers?days=365&native=false",
          defaultFilename: `chronox-finance-${isoDateOffset(0)}.xlsx`,
          label: "Apple Numbers Workbook",
        }).catch((err) => toast(err.message, true)),
    },
    {
      id: "export-csv-pack",
      icon: "download",
      title: "Export Full CSV Pack (.zip)",
      sub: "Complete financial tables in clean CSV format",
      keywords: "export csv zip spreadsheet backup",
      run: () =>
        downloadExportFile({
          url: "/api/v1/export/csv?pack=true",
          defaultFilename: `chronox-csv-pack-${isoDateOffset(0)}.zip`,
          label: "Full CSV Pack",
        }).catch((err) => toast(err.message, true)),
    },
    {
      id: "nav-today",
      icon: "activity",
      title: "Overview — Safe to Spend & Jars",
      sub: "Daily allowance, Safe to Invest, Money Jars & Upcoming Bills",
      keywords: "overview command today home dashboard safe spend jars",
      run: () => switchTab("today"),
    },
    {
      id: "nav-analytics",
      icon: "chart",
      title: "Cash Flow & Balance Forecast",
      sub: "Where your money goes, spending by category, monthly trends & forecast",
      keywords: "cash flow analytics sankey donut trend timeline forecast net worth",
      run: () => switchTab("analytics"),
    },
    {
      id: "nav-activity",
      icon: "receipt",
      title: "Activity — Transactions, Accounts & Bills",
      sub: "Searchable history, statement imports, splits, notes & recurring bills",
      keywords: "activity transactions bills splits tags comments import csv pdf email",
      run: () => switchTab("activity"),
    },
    {
      id: "nav-portfolio",
      icon: "briefcase",
      title: "Investments — Stocks & Crypto",
      sub: "Holdings, live prices & view-only brokerage connections",
      keywords: "investments portfolio stocks crypto holdings coinbase schwab robinhood",
      run: () => switchTab("portfolio"),
    },
    {
      id: "nav-credit",
      icon: "card",
      title: "Credit & Debt",
      sub: "Credit score factors, best time to pay cards & debt payoff plans",
      keywords: "credit debt fico vantage utilization bureau payoff",
      run: () => switchTab("credit"),
    },
    {
      id: "nav-goals",
      icon: "target",
      title: "Goals & Auto-Categorize Rules",
      sub: "Savings goals priority order, purchase planner & sorting rules",
      keywords: "goals rules automation drag swipe purchase planner",
      run: () => switchTab("goals"),
    },
    {
      id: "nav-coach",
      icon: "cpu",
      title: "AI Financial Coach",
      sub: "Ask questions, review proposed updates & personalized tips",
      keywords: "ai coach assistant plan execute undo chat",
      run: () => switchTab("coach"),
    },
    {
      id: "nav-settings",
      icon: "sliders",
      title: "Settings — Banks, AI & Exports",
      sub: "Connect Plaid or SimpleFIN, local AI assistants, passcode & exports",
      keywords: "settings banks plaid simplefin email ai local cli password export",
      run: () => switchTab("settings"),
    },
    {
      id: "open-impulse",
      icon: "snowflake",
      title: "Open Impulse Pause Vault",
      sub: "Park unplanned purchases for 48 hours before deciding",
      keywords: "impulse vault park cool urge drawer",
      run: () => openDrawer("impulse-drawer-overlay"),
    },
    {
      id: "open-reset",
      icon: "zap",
      title: "Open Weekly Money Check-In",
      sub: "5-minute categorization, subscription check & auto-rebalance",
      keywords: "weekly check-in reset triage streak drawer",
      run: () => openDrawer("reset-drawer-overlay"),
    },
    {
      id: "open-ai-usage",
      icon: "pie",
      title: "View AI Usage & Savings",
      sub: "Monthly requests, cache savings & daily limit",
      keywords: "ai usage tokens cache cost savings quota",
      run: () => {
        const btn = $("btn-ai-usage");
        if (btn) btn.click();
      },
    },
    {
      id: "lock-vault",
      icon: "lock",
      title: "Lock ChronoX Now",
      sub: "Require your passcode on next access",
      keywords: "lock logout security passcode password",
      run: () => {
        const btn = $("btn-lock-vault");
        if (btn) btn.click();
      },
    },
    {
      id: "mode-quant",
      icon: "calculator",
      title: "Smart Mode -> Built-in Math",
      sub: "100% offline calculations without calling external AI",
      keywords: "mode math offline local",
      run: () => setIntelligenceMode("deterministic_only"),
    },
    {
      id: "mode-hybrid",
      icon: "zap",
      title: "Smart Mode -> Auto (Recommended)",
      sub: "Exact built-in math paired with verified AI explanations",
      keywords: "mode auto hybrid verified ai",
      run: () => setIntelligenceMode("hybrid"),
    },
    {
      id: "mode-standalone",
      icon: "sparkles",
      title: "Smart Mode -> AI Direct",
      sub: "Direct AI coaching grounded in your balances",
      keywords: "mode standalone ai direct",
      run: () => setIntelligenceMode("ai_standalone"),
    },
    {
      id: "toggle-theme",
      icon: curTheme === "dark" ? "sun" : "moon",
      title: curTheme === "dark" ? "Switch to Light Theme" : "Switch to Dark Theme",
      sub: "Toggle high-contrast display theme",
      keywords: "theme dark light mode appearance",
      run: () => toggleTheme(),
    },
    {
      id: "sync-now",
      icon: "refresh",
      title: "Recalculate Budget Now",
      sub: "Refresh Money Jars, Upcoming Bills & Safe to Spend",
      keywords: "sync refresh recalculate budget",
      run: async () => {
        const res = await api("/api/v1/budget/sync", { method: "POST" });
        toast(`Budget synced. Safe to Spend: ${fmtMoney(res.safe_to_spend_cents)}`);
        await refreshAll();
      },
    },
  ];

  for (const provider of _commandProviders) {
    try {
      const extra = provider();
      if (Array.isArray(extra)) commands.push(...extra);
    } catch (_err) {
      // Ignore provider errors
    }
  }

  if (desktop) {
    commands.push(
      {
        id: "desktop-hud",
        icon: "zap",
        title: "Open Quick Capture",
        sub: "Summon floating quick capture bar",
        keywords: "hud quick capture desktop floating shortcut",
        run: () => {
          if (typeof desktop.toggleQuickCapture === "function") desktop.toggleQuickCapture();
        },
      },
      {
        id: "desktop-logs",
        icon: "terminal",
        title: "View App Diagnostics",
        sub: "Inspect sanitized local diagnostic output",
        keywords: "logs diagnostics debug desktop",
        run: () => openBackendLogsModal(),
      },
      {
        id: "desktop-data-dir",
        icon: "folder",
        title: "Open Local Data Folder",
        sub: "Reveal your private ChronoX data folder on this device",
        keywords: "folder data directory",
        run: () => {
          if (typeof desktop.openDataDir === "function") desktop.openDataDir();
        },
      }
    );
  }

  return commands;
}

function renderPaletteResults() {
  const input = $("cmd-palette-input");
  const list = $("cmd-palette-list");
  if (!input || !list) return;

  const query = input.value.trim();
  const qLower = query.toLowerCase();
  const base = getBaseCommands();

  const items = [];
  if (query && (/\d/.test(query) || query.split(/\s+/).length >= 3 || query.endsWith("?"))) {
    items.push({
      id: "quick-add-dynamic",
      icon: "sparkles",
      title: `Say or Log: "${query}"`,
      sub: "Log an expense, move savings, ask a question, or trigger an export",
      run: async () => {
        try {
          switchTab("today");
          await invokeSayHandler(query);
        } catch (err) {
          toast(err.message, true);
        }
      },
    });
  }

  for (const cmd of base) {
    if (
      !qLower ||
      cmd.title.toLowerCase().includes(qLower) ||
      cmd.sub.toLowerCase().includes(qLower) ||
      (cmd.keywords && cmd.keywords.includes(qLower))
    ) {
      items.push(cmd);
    }
  }

  state.paletteItems = items;
  if (state.paletteIndex >= items.length) state.paletteIndex = 0;

  if (!items.length) {
    list.replaceChildren(
      el("div", {
        class: "muted small",
        text: 'No matching command. Try typing a sentence like "I spent $12 on lunch" or "one-tap".',
        style: { padding: "14px" },
      })
    );
    return;
  }

  list.replaceChildren(
    ...items.map((item, idx) =>
      el(
        "button",
        {
          type: "button",
          class: `palette-item${idx === state.paletteIndex ? " selected" : ""}`,
          role: "option",
          "aria-selected": String(idx === state.paletteIndex),
          "data-palette-idx": String(idx),
        },
        [
          el("span", { class: "palette-item-left" }, [
            el("span", { class: "palette-icon-box" }, [makeIcon(item.icon, "ui-icon-sm")]),
            el("span", {}, [
              el("span", { class: "palette-item-title", text: item.title }),
              el("span", { class: "palette-item-sub", text: item.sub }),
            ]),
          ]),
          idx === state.paletteIndex ? el("kbd", { text: "ENTER" }) : null,
        ]
      )
    )
  );
}

export function openPalette() {
  if (state.vaultStatus !== "unlocked") return;
  state.paletteOpen = true;
  state.paletteIndex = 0;
  openOverlay($("cmd-palette-overlay"));
  const input = $("cmd-palette-input");
  if (input) {
    input.value = "";
    renderPaletteResults();
    input.focus();
  }
}

export function closePalette() {
  state.paletteOpen = false;
  closeOverlay($("cmd-palette-overlay"));
}

async function runPaletteItem(idx) {
  const item = state.paletteItems[idx];
  closePalette();
  if (item && typeof item.run === "function") {
    await item.run();
  }
}

export function initCommandPalette() {
  const openBtn = $("btn-open-palette");
  const closeBtn = $("cmd-palette-close");
  const overlay = $("cmd-palette-overlay");
  const input = $("cmd-palette-input");
  const list = $("cmd-palette-list");

  if (openBtn) openBtn.addEventListener("click", openPalette);
  if (closeBtn) closeBtn.addEventListener("click", closePalette);
  if (overlay) {
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) closePalette();
    });
  }

  if (input) {
    input.addEventListener("input", () => {
      state.paletteIndex = 0;
      renderPaletteResults();
    });
    input.addEventListener("keydown", async (e) => {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        if (state.paletteItems.length) {
          state.paletteIndex = (state.paletteIndex + 1) % state.paletteItems.length;
          renderPaletteResults();
          const activeBtn = list && list.querySelector(".palette-item.selected");
          if (activeBtn) activeBtn.scrollIntoView({ block: "nearest" });
        }
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        if (state.paletteItems.length) {
          state.paletteIndex =
            (state.paletteIndex - 1 + state.paletteItems.length) % state.paletteItems.length;
          renderPaletteResults();
          const activeBtn = list && list.querySelector(".palette-item.selected");
          if (activeBtn) activeBtn.scrollIntoView({ block: "nearest" });
        }
      } else if (e.key === "Enter") {
        e.preventDefault();
        await runPaletteItem(state.paletteIndex);
      } else if (e.key === "Escape") {
        e.preventDefault();
        closePalette();
      }
    });
  }

  if (list) {
    list.addEventListener("click", async (e) => {
      const itemBtn = e.target.closest("button[data-palette-idx]");
      if (!itemBtn) return;
      await runPaletteItem(Number(itemBtn.dataset.paletteIdx));
    });
  }

  document.addEventListener("keydown", (e) => {
    if (state.vaultStatus !== "unlocked") return;

    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      if (state.paletteOpen) closePalette();
      else openPalette();
      return;
    }

    const tag = (e.target && e.target.tagName) || "";
    const inInput = ["INPUT", "TEXTAREA", "SELECT"].includes(tag);

    if (e.key === "/" && !inInput && !state.paletteOpen && !state.logsOpen && !state.activeDrawer) {
      e.preventDefault();
      switchTab("today");
      const qa = $("quick-add-input");
      if (qa) qa.focus();
      return;
    }

    if (e.key === "Escape") {
      if (state.logsOpen) {
        closeBackendLogsModal();
        return;
      }
      if (state.paletteOpen) {
        closePalette();
        return;
      }
      const oneTapOverlay = $("onetap-overlay");
      if (oneTapOverlay && !oneTapOverlay.classList.contains("hidden")) {
        closeOverlay(oneTapOverlay);
        return;
      }
      const aiOverlay = $("ai-usage-overlay");
      if (aiOverlay && !aiOverlay.classList.contains("hidden")) {
        closeOverlay(aiOverlay);
        return;
      }
      if (state.activeDrawer) {
        closeDrawer(state.activeDrawer);
        return;
      }
      const onboard = $("onboarding-overlay");
      if (onboard && !onboard.classList.contains("hidden")) {
        closeOverlay(onboard);
      }
    }
  });
}
