"use strict";

/**
 * ChronoX Finance — Calm ADHD Auto-Budget & Quant Intelligence Frontend
 * Zero external CDN dependencies, strict CSP compliant, XSS-safe DOM rendering.
 */

const SVG_NS = "http://www.w3.org/2000/svg";
const HERO_RING_CIRCUMFERENCE = 2 * Math.PI * 54;
const CRED_RING_CIRCUMFERENCE = 2 * Math.PI * 35;

const MODE_DESCRIPTIONS = {
  deterministic_only: "Pure institutional-grade quant math — zero external AI calls, instant 100% deterministic guidance.",
  hybrid: "Combines exact optimal quant math with AI synthesis and strict mathematical validation.",
  ai_standalone: "Direct creative AI financial advisor with PII-redacted live balances and calm RSD-safe tone filtering.",
};

const BUCKET_LABELS = {
  autopay_protection: "Autopay Shield",
  promo_apr_cliff: "0% Promo Cliff",
  high_apr_debt: "High-APR Debt",
  azeo_fico_cliff: "AZEO Score Tier",
  azeo_score_unlock: "AZEO Score Tier",
  emergency_buffer: "Liquidity Buffer",
  tracked_goal: "Tracked Goal",
  invest_vault: "Invest Guardrail",
  low_apr_debt: "Low-APR Debt",
  safety_reserve: "Safety Reserve",
};

const TAB_TITLES = {
  today: "Today",
  credit: "Credit & Debt",
  goals: "Goals & Planner",
  impulse: "Impulse Vault",
  reset: "5-Min Reset",
  activity: "Activity & Bills",
  settings: "AI & Settings",
};

const state = {
  dashboard: null,
  quantForecast: null,
  quantDays: 30,
  quantScrubIndex: null,
  creditReport: null,
  lastGoalPlan: null,
  settings: null,
  transactions: [],
  opencodeModels: [],
  geminiModels: [],
  activeTab: "today",
  editingJarId: null,
  reconcilingAcctId: null,
  editingTxId: null,
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

const $ = (id) => document.getElementById(id);

function prefersReducedMotion() {
  return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function fmtMoney(cents) {
  const neg = cents < 0;
  const val = (Math.abs(Number(cents) || 0) / 100).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  return `${neg ? "-" : ""}$${val}`;
}

function fmtMoneyCompact(cents) {
  const num = (Number(cents) || 0) / 100;
  const abs = Math.abs(num);
  const sign = num < 0 ? "-" : "";
  if (abs >= 10000) return `${sign}$${(abs / 1000).toFixed(1)}k`;
  if (abs >= 1000) return `${sign}$${(abs / 1000).toFixed(2)}k`;
  return `${sign}$${Math.round(abs)}`;
}

function fmtShortDate(iso) {
  if (!iso) return "--";
  const parts = String(iso).split("-");
  if (parts.length !== 3) return iso;
  const dt = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]));
  if (Number.isNaN(dt.getTime())) return iso;
  return dt.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function fmtDayDate(iso) {
  if (!iso) return "--";
  const parts = String(iso).split("-");
  if (parts.length !== 3) return iso;
  const dt = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]));
  if (Number.isNaN(dt.getTime())) return iso;
  return dt.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

function dollarsToCents(raw, { allowNegative = false, allowZero = false } = {}) {
  const cleaned = String(raw ?? "").trim().replace(/[$,\s]/g, "");
  if (!cleaned) throw new Error("Please enter a dollar amount.");
  const num = Number(cleaned);
  if (!Number.isFinite(num)) throw new Error("Please enter a valid number.");
  if (!allowNegative && num < 0) throw new Error("Amount cannot be negative.");
  if (!allowZero && num === 0) throw new Error("Amount must be greater than $0.00.");
  return Math.round(num * 100);
}

function isoDateOffset(days = 0) {
  const dt = new Date();
  dt.setDate(dt.getDate() + days);
  const y = dt.getFullYear();
  const m = String(dt.getMonth() + 1).padStart(2, "0");
  const d = String(dt.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

function animateMoney(node, targetCents, key) {
  if (!node) return;
  const target = Number(targetCents) || 0;
  const prev = state.animatedValues[key];
  state.animatedValues[key] = target;
  if (prev === undefined || prev === target || prefersReducedMotion()) {
    node.textContent = fmtMoney(target);
    return;
  }
  const start = performance.now();
  const duration = 420;
  const step = (now) => {
    const t = Math.min(1, (now - start) / duration);
    const eased = t === 1 ? 1 : 1 - Math.pow(2, -10 * t);
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

function animatePercent(node, targetPct, key, decimals = 1) {
  if (!node) return;
  const target = Number(targetPct) || 0;
  const prev = state.animatedValues[key];
  state.animatedValues[key] = target;
  if (prev === undefined || Math.abs(prev - target) < 0.05 || prefersReducedMotion()) {
    node.textContent = `${target.toFixed(decimals)}%`;
    return;
  }
  const start = performance.now();
  const duration = 460;
  const step = (now) => {
    const t = Math.min(1, (now - start) / duration);
    const eased = t === 1 ? 1 : 1 - Math.pow(2, -10 * t);
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

function spawnCelebrationBurst(anchorNode) {
  if (prefersReducedMotion()) return;
  const target = anchorNode && typeof anchorNode.getBoundingClientRect === "function" ? anchorNode : $("xp-pill");
  if (!target) return;
  const rect = target.getBoundingClientRect();
  if (!rect.width && !rect.height) return;

  const vw = window.innerWidth || 1200;
  const vh = window.innerHeight || 800;
  const cx = Math.max(66, Math.min(vw - 66, rect.left + rect.width / 2));
  const cy = Math.max(66, Math.min(vh - 66, rect.top + rect.height / 2));

  const sparks = [];
  for (let i = 0; i < 8; i++) {
    const angle = (i * Math.PI) / 4;
    const x1 = 62 + Math.cos(angle) * 26;
    const y1 = 62 + Math.sin(angle) * 26;
    const x2 = 62 + Math.cos(angle) * 43;
    const y2 = 62 + Math.sin(angle) * 43;
    sparks.push(
      svgEl("line", {
        class: "burst-spark",
        x1: x1.toFixed(1),
        y1: y1.toFixed(1),
        x2: x2.toFixed(1),
        y2: y2.toFixed(1),
        stroke: i % 2 === 0 ? "var(--primary)" : "var(--amber)",
      })
    );
  }

  const host = svgEl(
    "svg",
    {
      class: "celebrate-burst-host",
      viewBox: "0 0 124 124",
      "aria-hidden": "true",
    },
    [
      svgEl("circle", { class: "burst-ring-primary", cx: "62", cy: "62", r: "42" }),
      svgEl("circle", { class: "burst-ring-gold", cx: "62", cy: "62", r: "31" }),
      ...sparks,
    ]
  );
  host.style.left = `${cx.toFixed(1)}px`;
  host.style.top = `${cy.toFixed(1)}px`;
  document.body.appendChild(host);
  setTimeout(() => host.remove(), 760);
}

function triggerCelebrate(node, withBurst = false) {
  if (prefersReducedMotion()) return;
  const targets = [node, $("xp-pill")].filter(Boolean);
  for (const elNode of targets) {
    elNode.classList.remove("celebrate-pulse");
    void elNode.offsetWidth;
    elNode.classList.add("celebrate-pulse");
  }
  if (withBurst) {
    spawnCelebrationBurst(node || $("xp-pill"));
  }
}

/* --- Spring-Physics Overlay Choreography --- */

function openOverlay(overlay) {
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

function closeOverlay(overlay) {
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
  }, 180);
}

function setVaultTitlebarState(status, label) {
  const dot = $("titlebar-vault-dot");
  const text = $("titlebar-vault-label");
  if (!dot || !text) return;
  dot.classList.toggle("is-syncing", status === "syncing");
  dot.classList.toggle("is-locked", status === "locked");
  if (label) text.textContent = label;
}

function openLockOverlay() {
  clearBootSkeletons();
  setVaultTitlebarState("locked", "Vault Locked");
  openOverlay($("lock-overlay"));
}

function closeLockOverlay() {
  setVaultTitlebarState("active", "Local Vault Active");
  closeOverlay($("lock-overlay"));
}

/* --- Gliding Spring Tab Indicator Pill, Staggered Entrances, Pointer Sheen & Skeletons --- */

function positionTabIndicator(immediate = false) {
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

function initTabIndicatorObserver() {
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

function choreographEntrance(container) {
  if (!container || prefersReducedMotion()) return;
  const cards = Array.from(container.querySelectorAll(".card, .jar-card, .stat-tile")).filter(
    (node) => !node.classList.contains("hidden") && !node.closest(".hidden")
  );
  if (!cards.length) return;
  cards.forEach((node) => node.classList.remove("stagger-enter"));
  void container.offsetWidth;
  cards.forEach((node, idx) => {
    const delayMs = Math.min(idx, 10) * 30;
    node.style.setProperty("--stagger-delay", `${delayMs}ms`);
    node.classList.add("stagger-enter");
    const cleanup = () => {
      node.classList.remove("stagger-enter");
      node.style.removeProperty("--stagger-delay");
    };
    node.addEventListener("animationend", cleanup, { once: true });
    setTimeout(cleanup, delayMs + 480);
  });
}

function initCardSheen() {
  let rafId = null;
  let pendingCard = null;
  let pendingX = 0;
  let pendingY = 0;

  document.addEventListener(
    "pointermove",
    (e) => {
      if (prefersReducedMotion()) return;
      const card = e.target && e.target.closest ? e.target.closest(".hero-card, .jar-card") : null;
      if (!card) return;
      pendingCard = card;
      pendingX = e.clientX;
      pendingY = e.clientY;
      if (rafId !== null) return;
      rafId = requestAnimationFrame(() => {
        rafId = null;
        if (!pendingCard) return;
        const rect = pendingCard.getBoundingClientRect();
        pendingCard.style.setProperty("--mx", `${(pendingX - rect.left).toFixed(1)}px`);
        pendingCard.style.setProperty("--my", `${(pendingY - rect.top).toFixed(1)}px`);
      });
    },
    { passive: true }
  );
}

function buildSkeletonCard(lines = 3) {
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

function showBootSkeletons() {
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
  const chartWrap = $("quant-chart-wrap");
  if (chartWrap && !chartWrap.childElementCount) {
    chartWrap.replaceChildren(el("div", { class: "skeleton-chart", "aria-hidden": "true" }));
  }
  const aiList = $("ai-suggestions-list");
  if (aiList && !aiList.childElementCount) {
    aiList.replaceChildren(buildSkeletonCard(2), buildSkeletonCard(2));
  }
  const billList = $("bill-radar-list");
  if (billList && !billList.childElementCount) {
    billList.replaceChildren(buildSkeletonCard(2), buildSkeletonCard(2));
  }
}

function clearBootSkeletons() {
  document.body.classList.remove("is-loading");
}

async function withBusy(btn, fn) {
  if (!btn) return fn();
  if (btn.disabled) return;
  const prevText = btn.textContent;
  btn.disabled = true;
  try {
    return await fn();
  } finally {
    btn.disabled = false;
    if (btn.textContent !== prevText && btn.dataset.restoreText !== "false") {
      btn.textContent = prevText;
    }
  }
}

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "checked") node.checked = Boolean(v);
    else if (k === "value") node.value = String(v);
    else if (k.startsWith("data-")) node.setAttribute(k, v);
    else if (k === "style" && typeof v === "object") Object.assign(node.style, v);
    else if (v !== null && v !== undefined) node.setAttribute(k, String(v));
  }
  for (const child of children) {
    if (typeof child === "string") node.appendChild(document.createTextNode(child));
    else if (child) node.appendChild(child);
  }
  return node;
}

function svgEl(tag, attrs = {}, children = []) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "text") node.textContent = v;
    else if (v !== null && v !== undefined) node.setAttribute(k, String(v));
  }
  for (const child of children) {
    if (child) node.appendChild(child);
  }
  return node;
}

const ICON_SHAPES = {
  sun: [
    ["circle", { cx: "12", cy: "12", r: "4" }],
    ["path", { d: "M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41" }],
  ],
  moon: [["path", { d: "M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" }]],
  card: [
    ["rect", { x: "2", y: "5", width: "20", height: "14", rx: "2.5" }],
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
    ["path", { d: "M5 9.5V20a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V9.5" }],
    ["path", { d: "M9.5 21v-6h5v6" }],
  ],
  bag: [
    ["path", { d: "M6 7h12l1 13a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1L6 7z" }],
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
  trending: [
    ["polyline", { points: "22 7 13.5 15.5 8.5 10.5 2 17" }],
    ["polyline", { points: "16 7 22 7 22 13" }],
  ],
  calculator: [
    ["rect", { x: "4", y: "2", width: "16", height: "20", rx: "2.5" }],
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
    ["path", { d: "M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" }],
    ["path", { d: "M18.5 2.5a2.12 2.12 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" }],
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
    ["rect", { x: "4", y: "11", width: "16", height: "10", rx: "2.5" }],
    ["path", { d: "M8 11V7a4 4 0 0 1 8 0v4" }],
  ],
  terminal: [
    ["polyline", { points: "4 17 10 11 4 5" }],
    ["line", { x1: "12", y1: "19", x2: "20", y2: "19" }],
  ],
  folder: [
    ["path", { d: "M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" }],
  ],
  monitor: [
    ["rect", { x: "2", y: "3", width: "20", height: "14", rx: "2.5" }],
    ["line", { x1: "8", y1: "21", x2: "16", y2: "21" }],
    ["line", { x1: "12", y1: "17", x2: "12", y2: "21" }],
  ],
};

function makeIcon(name, extraClass = "") {
  const shapes = ICON_SHAPES[name] || ICON_SHAPES.sparkles;
  return svgEl(
    "svg",
    {
      class: extraClass ? `ui-icon ${extraClass}` : "ui-icon",
      viewBox: "0 0 24 24",
      fill: "none",
      stroke: "currentColor",
      "stroke-width": "1.8",
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
      "aria-hidden": "true",
    },
    shapes.map(([tag, attrs]) => svgEl(tag, attrs))
  );
}

function jarIconKey(jar) {
  const key = String((jar && (jar.slug || jar.kind || jar.name)) || "").toLowerCase();
  if (key.includes("bill") || key.includes("essential") || key.includes("fixed")) return "home";
  if (key.includes("daily") || key.includes("living") || key.includes("flex")) return "bag";
  if (key.includes("fun") || key.includes("guilt") || key.includes("joy")) return "sparkles";
  if (key.includes("safe") || key.includes("future") || key.includes("buffer") || key.includes("emergency")) return "shield";
  if (key.includes("invest") || key.includes("trade") || key.includes("chronox") || key.includes("vault")) return "trending";
  return "sparkles";
}

function setNodeIconText(node, iconName, text) {
  node.replaceChildren(makeIcon(iconName, "ui-icon-sm"), document.createTextNode(text));
}

function renderFormattedText(raw) {
  const frag = document.createDocumentFragment();
  const str = String(raw ?? "")
    .replace(/\u2B50/g, "\u2605")
    .replace(/[\u{1F300}-\u{1FAFF}\uFE0F]/gu, "");
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

function toast(message, isError = false) {
  const region = $("toast-region");
  const item = el("div", { class: `toast${isError ? " toast-err" : ""}` }, [
    el("span", { class: "inline-icon-row" }, [
      makeIcon(isError ? "alert" : "check", "ui-icon-sm"),
      String(message ?? ""),
    ]),
  ]);
  region.appendChild(item);
  setTimeout(() => item.remove(), 4200);
}

async function api(path, options = {}) {
  const headers = Object.assign({}, options.headers || {});
  if (options.body && !(options.body instanceof FormData) && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const resp = await fetch(path, {
    credentials: "same-origin",
    ...options,
    headers,
  });
  if (resp.status === 401 && path !== "/api/auth/login") {
    openLockOverlay();
    throw new Error("Locked — please unlock with your passcode.");
  }
  const ct = resp.headers.get("content-type") || "";
  const data = ct.includes("application/json") ? await resp.json() : await resp.text();
  if (!resp.ok) {
    const msg = (data && data.detail) || (typeof data === "string" ? data : `HTTP ${resp.status}`);
    throw new Error(msg);
  }
  return data;
}

/* --- Adaptive Theme (Obsidian Dark default + Apple Porcelain Light) --- */

function applyTheme(theme, persist = false, opts = {}) {
  const resolved = theme === "light" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", resolved);
  if (persist) {
    try {
      localStorage.setItem("chronox-theme", resolved);
    } catch (_err) {
      // Ignore private-mode storage restrictions
    }
  }
  const isLight = resolved === "light";
  $("theme-icon-sun").classList.toggle("hidden", !isLight);
  $("theme-icon-moon").classList.toggle("hidden", isLight);
  $("btn-theme-toggle").setAttribute("aria-pressed", String(isLight));
  $("btn-theme-toggle").title = isLight ? "Switch to Obsidian Dark theme" : "Switch to Apple Porcelain Light theme";

  // Sync native Electron window titleBarOverlay, prefs, Tray & HUD when explicitly changed by the user
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

function initTheme() {
  let saved = null;
  try {
    saved = localStorage.getItem("chronox-theme");
  } catch (_err) {
    saved = null;
  }
  // Obsidian Dark is default unless explicitly toggled to light
  applyTheme(saved === "light" ? "light" : "dark", false);

  if (window.matchMedia) {
    const mq = window.matchMedia("(prefers-color-scheme: light)");
    if (typeof mq.addEventListener === "function") {
      mq.addEventListener("change", (ev) => {
        try {
          if (!localStorage.getItem("chronox-theme")) {
            applyTheme(ev.matches ? "light" : "dark", false);
          }
        } catch (_err) {
          // ignore
        }
      });
    }
  }
}

function toggleTheme() {
  const cur = document.documentElement.getAttribute("data-theme") || "dark";
  const next = cur === "dark" ? "light" : "dark";
  applyTheme(next, true);
  toast(next === "light" ? "Porcelain Light mode active" : "Obsidian Dark mode active");
}

/* --- Intelligence Mode & Provider UI Helpers --- */

function syncModeControls(mode) {
  const normalized = ["deterministic_only", "hybrid", "ai_standalone"].includes(mode) ? mode : "hybrid";
  document.querySelectorAll("[data-set-mode]").forEach((btn) => {
    const active = btn.dataset.setMode === normalized;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", String(active));
  });
  const desc = $("settings-mode-desc");
  if (desc) desc.textContent = MODE_DESCRIPTIONS[normalized] || MODE_DESCRIPTIONS.hybrid;
}

function syncProviderControls(provider, activeResolved) {
  const normalized = ["auto", "opencode_go", "gemini"].includes(provider) ? provider : "auto";
  document.querySelectorAll("[data-set-provider]").forEach((btn) => {
    const active = btn.dataset.setProvider === normalized;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", String(active));
  });

  const badge = $("active-provider-badge");
  if (!badge) return;
  if (activeResolved === "opencode_go") {
    badge.className = "pill pill-calm";
    badge.textContent = "Active: OpenCode Go";
  } else if (activeResolved === "gemini") {
    badge.className = "pill pill-blue";
    badge.textContent = "Active: Google Gemini";
  } else {
    badge.className = "pill pill-soft";
    badge.textContent = "Active: Local Quant Math";
  }
}

function formatProvenance(res) {
  const source = (res && res.source) || "deterministic_local";
  const mode = (res && res.intelligence_mode) || "hybrid";
  const provider = (res && res.provider_used) || null;
  const model = (res && res.model_used) || "";
  const cachedNote = res && res.cached ? " · cached" : "";

  const providerLabel =
    provider === "gemini" || source === "gemini"
      ? "Google Gemini"
      : provider === "opencode_go" || source === "opencode_go"
      ? "OpenCode Go"
      : null;

  if (mode === "deterministic_only") {
    return { icon: "calculator", text: "Optimal Quant Math · Zero AI", cls: "pill-calm" };
  }
  if (source === "ai_standalone" || (mode === "ai_standalone" && providerLabel)) {
    const prov = providerLabel || "AI";
    const mod = model ? ` (${model})` : "";
    return { icon: "sparkles", text: `AI Standalone · ${prov}${mod}${cachedNote}`, cls: "pill-blue" };
  }
  if (providerLabel) {
    const mod = model ? ` · ${model}` : "";
    return { icon: "zap", text: `Hybrid · ${providerLabel}${mod}${cachedNote}`, cls: "pill-calm" };
  }
  if (source === "deterministic_quant") {
    return { icon: "calculator", text: "Optimal Quant Fallback", cls: "pill-soft" };
  }
  return { icon: "calculator", text: "Verified Quant Local Engine", cls: "pill-soft" };
}

async function setIntelligenceMode(mode) {
  try {
    syncModeControls(mode);
    const updated = await api("/api/settings", {
      method: "PATCH",
      body: JSON.stringify({ intelligence_mode: mode }),
    });
    state.settings = updated;
    syncModeControls(updated.intelligence_mode || mode);
    syncProviderControls(updated.ai_provider || "auto", updated.active_provider_resolved || "none");
    const labels = {
      deterministic_only: "Quant Math Only (zero AI calls)",
      hybrid: "Hybrid (Quant Math + Verified AI)",
      ai_standalone: "AI Standalone Advisor",
    };
    toast(`Switched Intelligence Mode → ${labels[mode] || mode}`);
    pushTraySnapshot();
    await loadAISuggestions(true);
  } catch (err) {
    toast(err.message, true);
  }
}

async function setPreferredProvider(provider) {
  try {
    const updated = await api("/api/settings", {
      method: "PATCH",
      body: JSON.stringify({ ai_provider: provider }),
    });
    state.settings = updated;
    syncProviderControls(updated.ai_provider || provider, updated.active_provider_resolved || "none");
    toast(`Preferred provider set to ${provider === "opencode_go" ? "OpenCode Go" : provider === "gemini" ? "Google Gemini" : "Auto"}`);
    await loadAISuggestions(true);
  } catch (err) {
    toast(err.message, true);
  }
}

/* --- Tabs --- */

function switchTab(tabName) {
  const validTab = TAB_TITLES[tabName] ? tabName : "today";
  const changed = state.activeTab !== validTab;
  state.activeTab = validTab;

  document.querySelectorAll(".tab").forEach((btn) => {
    const active = btn.dataset.tab === validTab;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", String(active));
  });

  let activePanel = null;
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    const isActive = panel.id === `tab-${validTab}`;
    panel.classList.toggle("active", isActive);
    if (isActive) activePanel = panel;
  });

  const subtitle = $("titlebar-view-subtitle");
  if (subtitle) subtitle.textContent = TAB_TITLES[validTab] || "Today";

  positionTabIndicator(false);
  if (changed && activePanel) {
    choreographEntrance(activePanel);
  }

  if (validTab === "credit") loadCreditReport();
  if (validTab === "goals" && state.dashboard) renderGoals(state.dashboard.goals || []);
  if (validTab === "reset") loadWeeklyReset();
  if (validTab === "activity") loadTransactions();
  if (validTab === "settings") loadSettings();
}

/* --- First-Party Electron Desktop Integration (Progressive Enhancement) --- */

function getDesktopBridge() {
  return window.chronoxDesktop || null;
}

function formatShortcutLabel(raw, platformKey) {
  if (!raw) return platformKey === "darwin" ? "⌘⇧Space" : "Ctrl+Shift+Space";
  let str = String(raw).trim();
  if (platformKey === "darwin") {
    str = str
      .replace(/CommandOrControl/gi, "⌘")
      .replace(/Command|Cmd/gi, "⌘")
      .replace(/Shift/gi, "⇧")
      .replace(/Alt|Option/gi, "⌥")
      .replace(/Control|Ctrl/gi, "⌃")
      .replace(/\+/g, "");
    return str;
  }
  return str.replace(/CommandOrControl/gi, "Ctrl");
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

function pushTraySnapshot() {
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
  };
  if (s) {
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

async function loadBackendLogs() {
  const desktop = getDesktopBridge();
  const pre = $("desktop-logs-pre");
  const meta = $("desktop-logs-meta");
  const pathBar = $("desktop-logs-path");
  if (!desktop || typeof desktop.getBackendLogs !== "function") {
    pre.textContent = "Backend sidecar logs are available inside the ChronoX Desktop app.";
    return;
  }
  pre.textContent = "Loading local sidecar diagnostics…";
  try {
    const res = await desktop.getBackendLogs();
    const lines = Array.isArray(res && res.lines) ? res.lines : [];
    const mode = (res && res.backendMode) || (state.desktopInfo && state.desktopInfo.backendMode) || "local";
    const pid = res && res.pid ? `PID ${res.pid}` : "active";
    meta.textContent = `${mode} · ${pid} · ${lines.length} lines`;
    const urlStr = (res && res.backendUrl) || (state.desktopInfo && state.desktopInfo.backendUrl) || "";
    const dbStr = (state.desktopInfo && state.desktopInfo.dbPath) || "";
    pathBar.textContent = [urlStr ? `Sidecar: ${urlStr}` : null, dbStr ? `Vault DB: ${dbStr}` : null]
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

function openBackendLogsModal() {
  state.logsOpen = true;
  openOverlay($("desktop-logs-overlay"));
  loadBackendLogs();
  $("desktop-logs-pre").focus();
}

function closeBackendLogsModal() {
  state.logsOpen = false;
  closeOverlay($("desktop-logs-overlay"));
}

async function copyBackendLogs() {
  const info = state.desktopInfo;
  const header = info
    ? `ChronoX Finance Desktop v${info.appVersion || "1.0"} (${info.platform || "unknown"} ${info.arch || ""}) · Electron ${info.electronVersion || "--"}\n`
    : "ChronoX Finance Desktop Diagnostics\n";
  const text = `${header}${"-".repeat(64)}\n${state.lastLogsText || $("desktop-logs-pre").textContent || ""}`;
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
    toast("Copied backend diagnostics to clipboard!");
  } catch (err) {
    toast(`Could not copy logs: ${err.message}`, true);
  }
}

async function initDesktopBridge() {
  const desktop = getDesktopBridge();
  if (!desktop) return;

  // Resolve platform synchronously from preload bridge for zero-shift initial frame
  const normalizePlatform = (raw) => {
    const p = String(raw || "").toLowerCase();
    return p.startsWith("win") ? "win" : p === "darwin" ? "darwin" : "linux";
  };
  let platKey = normalizePlatform(desktop.platform);
  state.desktopPlatform = platKey;

  // Reveal desktop chrome & controls progressively
  document.body.classList.add("is-desktop", `platform-${platKey}`);
  $("desktop-titlebar").classList.remove("hidden");
  $("desktop-settings-card").classList.remove("hidden");
  $("btn-desktop-import-dialog").classList.remove("hidden");
  $("desktop-win-controls").classList.toggle("hidden", platKey !== "linux");
  $("desktop-hud-kbd").textContent = formatShortcutLabel("", platKey);

  // Fetch host metadata & saved desktop preferences
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
          $("desktop-win-controls").classList.toggle("hidden", platKey !== "linux");
        }
      }

      const shortcutStr = formatShortcutLabel(
        (info && (info.shortcut || (info.prefs && info.prefs.quickCaptureShortcut))) || "",
        platKey
      );
      $("desktop-hud-kbd").textContent = shortcutStr;

      const platLabel = platKey === "win" ? "Windows" : platKey === "darwin" ? "macOS" : "Linux";
      const verStr = info && info.appVersion ? `v${info.appVersion}` : "Desktop";
      const archStr = info && info.arch ? ` · ${info.arch}` : "";
      $("desktop-version-pill").textContent = `${verStr} · ${platLabel}${archStr}`;

      const prefs = (info && info.prefs) || {};
      $("desktop-pref-close-to-tray").checked = Boolean(prefs.closeToTray);
      $("desktop-pref-launch-login").checked = Boolean(prefs.launchAtLogin);

      // Honor Electron-persisted theme across ephemeral localhost ports
      if (prefs.theme === "light" || prefs.theme === "dark") {
        applyTheme(prefs.theme, true, { fromDesktop: true });
      }
    }
  } catch (_err) {
    // Platform classes & controls were already applied synchronously above
  }

  // Initial maximized icon state
  if (typeof desktop.isMaximized === "function") {
    Promise.resolve(desktop.isMaximized())
      .then((max) => updateMaximizeButtonState(Boolean(max)))
      .catch(() => {});
  }

  // Titlebar window buttons & double-click maximize
  $("btn-win-minimize").addEventListener("click", () => {
    if (typeof desktop.minimize === "function") desktop.minimize();
  });
  $("btn-win-maximize").addEventListener("click", async () => {
    if (typeof desktop.maximizeToggle !== "function") return;
    try {
      const res = await desktop.maximizeToggle();
      if (res && typeof res.maximized === "boolean") {
        updateMaximizeButtonState(res.maximized);
      }
    } catch (_err) {
      // ignore
    }
  });
  $("btn-win-close").addEventListener("click", () => {
    if (typeof desktop.close === "function") desktop.close();
  });
  $("desktop-titlebar").addEventListener("dblclick", async (e) => {
    if (e.target.closest("button, a, input, kbd")) return;
    if (typeof desktop.maximizeToggle === "function") {
      try {
        const res = await desktop.maximizeToggle();
        if (res && typeof res.maximized === "boolean") {
          updateMaximizeButtonState(res.maximized);
        }
      } catch (_err) {
        // ignore
      }
    }
  });

  // Quick Capture HUD triggers
  const openHud = () => {
    if (typeof desktop.toggleQuickCapture === "function") {
      Promise.resolve(desktop.toggleQuickCapture()).catch((err) => toast(err.message, true));
    }
  };
  $("btn-desktop-hud").addEventListener("click", openHud);
  $("btn-settings-open-hud").addEventListener("click", openHud);

  // Desktop Preferences switches
  $("desktop-pref-close-to-tray").addEventListener("change", async (e) => {
    const nextVal = e.target.checked;
    if (typeof desktop.setPrefs !== "function") return;
    try {
      const res = await desktop.setPrefs({ closeToTray: nextVal });
      if (res && res.prefs && typeof res.prefs.closeToTray === "boolean") {
        e.target.checked = res.prefs.closeToTray;
      }
      toast(nextVal ? "ChronoX will stay ready in the system tray when closed." : "Closing the window will quit ChronoX cleanly.");
    } catch (err) {
      e.target.checked = !nextVal;
      toast(err.message, true);
    }
  });

  $("desktop-pref-launch-login").addEventListener("change", async (e) => {
    const nextVal = e.target.checked;
    if (typeof desktop.setPrefs !== "function") return;
    try {
      const res = await desktop.setPrefs({ launchAtLogin: nextVal });
      if (res && res.prefs && typeof res.prefs.launchAtLogin === "boolean") {
        e.target.checked = res.prefs.launchAtLogin;
      }
      toast(nextVal ? "ChronoX will launch automatically at login." : "Launch at login disabled.");
    } catch (err) {
      e.target.checked = !nextVal;
      toast(err.message, true);
    }
  });

  // Open Data Folder & Backend Logs modal triggers
  const openFolder = async () => {
    if (typeof desktop.openDataDir !== "function") return;
    try {
      await desktop.openDataDir();
    } catch (err) {
      toast(err.message, true);
    }
  };
  $("btn-desktop-open-data").addEventListener("click", openFolder);
  $("btn-desktop-logs-folder").addEventListener("click", openFolder);
  $("btn-desktop-view-logs").addEventListener("click", openBackendLogsModal);
  $("btn-desktop-logs-refresh").addEventListener("click", loadBackendLogs);
  $("btn-desktop-logs-copy").addEventListener("click", copyBackendLogs);
  $("btn-desktop-logs-close").addEventListener("click", closeBackendLogsModal);
  $("desktop-logs-overlay").addEventListener("click", (e) => {
    if (e.target === $("desktop-logs-overlay")) closeBackendLogsModal();
  });

  // Native Statement Import Picker
  $("btn-desktop-import-dialog").addEventListener("click", async () => {
    if (typeof desktop.openStatementDialog !== "function") return;
    try {
      const res = await desktop.openStatementDialog();
      if (!res || res.canceled) return;
      if (res.error) {
        toast(res.error, true);
        return;
      }
      const fname = res.filename || "statement.csv";
      const blob = new Blob([res.content ?? ""], { type: "text/plain;charset=utf-8" });
      state.desktopStatementFile = new File([blob], fname, { type: "text/plain" });
      $("csv-file-input").value = "";
      const kb = res.sizeBytes ? ` (${(res.sizeBytes / 1024).toFixed(1)} KB)` : "";
      const box = $("desktop-selected-statement");
      setNodeIconText(box, "check", `Native file ready: ${fname}${kb} — click Preview First or Import & Auto-Update.`);
      box.classList.remove("hidden");
    } catch (err) {
      toast(err.message, true);
    }
  });

  // Native Save Dialog enhancement for JSON Backup & CSV Export
  if (typeof desktop.saveFileDialog === "function") {
    $("btn-backup-export-link").addEventListener("click", async (e) => {
      e.preventDefault();
      try {
        const data = await api("/api/backup/export");
        const content = typeof data === "string" ? data : JSON.stringify(data, null, 2);
        const res = await desktop.saveFileDialog({
          defaultName: `chronox-finance-backup-${isoDateOffset(0)}.json`,
          content,
          filters: [{ name: "JSON Backup", extensions: ["json"] }],
        });
        if (res && !res.canceled) {
          if (res.error) toast(res.error, true);
          else toast(`Saved JSON backup${res.filePath ? ` to ${res.filePath}` : "!"}`);
        }
      } catch (err) {
        toast(err.message, true);
      }
    });

    $("btn-export-csv-link").addEventListener("click", async (e) => {
      e.preventDefault();
      try {
        const csvText = await api("/api/export/csv");
        const res = await desktop.saveFileDialog({
          defaultName: `chronox-transactions-${isoDateOffset(0)}.csv`,
          content: String(csvText ?? ""),
          filters: [{ name: "CSV Spreadsheet", extensions: ["csv"] }],
        });
        if (res && !res.canceled) {
          if (res.error) toast(res.error, true);
          else toast(`Exported CSV${res.filePath ? ` to ${res.filePath}` : "!"}`);
        }
      } catch (err) {
        toast(err.message, true);
      }
    });
  }

  // Subscribe to Main-Process / System Tray / Global HUD push events
  if (typeof desktop.onRefresh === "function") {
    desktop.onRefresh(async (payload) => {
      setVaultTitlebarState("syncing", "Syncing Vault…");
      try {
        await refreshAll();
        if (payload && payload.message) {
          toast(payload.message);
        }
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
      toast(theme === "light" ? "Porcelain Light mode active" : "Obsidian Dark mode active");
    });
  }
  if (typeof desktop.onSetMode === "function") {
    desktop.onSetMode((mode) => {
      if (mode) setIntelligenceMode(String(mode));
    });
  }
  if (typeof desktop.onOpenCommandPalette === "function") {
    desktop.onOpenCommandPalette(() => {
      openPalette();
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
      setVaultTitlebarState("syncing", "Syncing Vault…");
      try {
        const res = await api("/api/budget/sync", { method: "POST" });
        toast(`Auto-budget synced! Safe-to-Spend: ${fmtMoney(res.safe_to_spend_cents)}`);
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

/* --- Spotlight Command Palette (⌘K / Ctrl+K) --- */

function getBaseCommands() {
  const curTheme = document.documentElement.getAttribute("data-theme") || "dark";
  const desktop = getDesktopBridge();
  const commands = [
    {
      id: "nav-today",
      icon: "sun",
      title: "Go to Today Focus View",
      sub: "Safe-to-Spend ring, 5 Jars, 30-Day Runway & Coach",
      keywords: "today home dashboard safe spend runway",
      run: () => switchTab("today"),
    },
    {
      id: "nav-credit",
      icon: "card",
      title: "Go to Credit & Debt Optimizer",
      sub: "FICO utilization ring, AZEO calendar & 4-strategy payoff",
      keywords: "credit debt fico azeo cards utilization",
      run: () => switchTab("credit"),
    },
    {
      id: "nav-goals",
      icon: "target",
      title: "Go to Goals & Purchase Planner",
      sub: "Credit-safe purchase paths & tracked savings goals",
      keywords: "goals planner purchase savings",
      run: () => switchTab("goals"),
    },
    {
      id: "goal-1000",
      icon: "sparkles",
      title: "Plan a $1,000 Purchase Goal",
      sub: "Compute 3 credit-safe paths for a $1,000 gadget or target",
      keywords: "1000 gadget laptop plan buy afford",
      run: () => {
        switchTab("goals");
        $("gp-name").value = "$1,000 Purchase Goal";
        $("gp-price").value = "1000";
        $("gp-saved").value = "0";
        $("goal-plan-form").requestSubmit();
      },
    },
    {
      id: "nav-impulse",
      icon: "snowflake",
      title: "Open Impulse Cooling-Off Vault",
      sub: "Park a craving for 48h and claim Dopamine Wins",
      keywords: "impulse vault park cool urge",
      run: () => switchTab("impulse"),
    },
    {
      id: "nav-reset",
      icon: "zap",
      title: "Start 5-Minute Weekly Money Reset",
      sub: "1-tap triage, subscription check & streak protection",
      keywords: "weekly reset triage streak",
      run: () => switchTab("reset"),
    },
    {
      id: "nav-activity",
      icon: "receipt",
      title: "Open Activity, Accounts & Bills",
      sub: "Search/edit transactions, import CSV/OFX, manage bills",
      keywords: "activity transactions bills accounts import csv",
      run: () => switchTab("activity"),
    },
    {
      id: "nav-settings",
      icon: "sliders",
      title: "Open AI Providers & Settings",
      sub: "Configure OpenCode Go, Google Gemini, ADHD profile & lock",
      keywords: "settings ai gemini opencode key passcode backup",
      run: () => switchTab("settings"),
    },
    {
      id: "mode-quant",
      icon: "calculator",
      title: "Switch Intelligence Mode → Quant Math Only",
      sub: "100% deterministic optimal math, zero external AI calls",
      keywords: "mode quant math deterministic offline local",
      run: () => setIntelligenceMode("deterministic_only"),
    },
    {
      id: "mode-hybrid",
      icon: "zap",
      title: "Switch Intelligence Mode → Hybrid Verified AI",
      sub: "Optimal quant math grounded + verified AI coaching",
      keywords: "mode hybrid verified ai",
      run: () => setIntelligenceMode("hybrid"),
    },
    {
      id: "mode-standalone",
      icon: "sparkles",
      title: "Switch Intelligence Mode → AI Standalone",
      sub: "Direct AI advisor with calm RSD-safe tone filtering",
      keywords: "mode standalone ai only llm",
      run: () => setIntelligenceMode("ai_standalone"),
    },
    {
      id: "toggle-theme",
      icon: curTheme === "dark" ? "sun" : "moon",
      title: curTheme === "dark" ? "Switch to Apple Porcelain Light Mode" : "Switch to Obsidian Dark Mode",
      sub: "Toggle adaptive color theme (saved automatically)",
      keywords: "theme dark light mode appearance color",
      run: () => toggleTheme(),
    },
    {
      id: "sync-now",
      icon: "refresh",
      title: "Run Auto-Sync Budget Now",
      sub: "Recalculate 5 jars, bills shield & 30-day cashflow runway",
      keywords: "sync refresh recalculate budget",
      run: async () => {
        const res = await api("/api/budget/sync", { method: "POST" });
        toast(`Auto-budget synced! Safe-to-Spend: ${fmtMoney(res.safe_to_spend_cents)}`);
        await refreshAll();
      },
    },
    {
      id: "ask-coach",
      icon: "message",
      title: "Ask Intelligence Coach a Question",
      sub: "Focus the Coach Q&A box on Today",
      keywords: "ask coach question chat help",
      run: () => {
        switchTab("today");
        $("ask-coach-input").focus();
      },
    },
  ];

  if (desktop) {
    commands.push(
      {
        id: "desktop-hud",
        icon: "zap",
        title: "Open Quick Capture HUD",
        sub: "Summon the floating global expense & impulse capture bar",
        keywords: "hud quick capture desktop floating shortcut",
        run: () => {
          if (typeof desktop.toggleQuickCapture === "function") desktop.toggleQuickCapture();
        },
      },
      {
        id: "desktop-logs",
        icon: "terminal",
        title: "View Backend Sidecar Logs",
        sub: "Inspect live local Python diagnostics & copy troubleshooting info",
        keywords: "logs diagnostics debug backend sidecar desktop",
        run: () => openBackendLogsModal(),
      },
      {
        id: "desktop-data-dir",
        icon: "folder",
        title: "Open Local Vault Data Folder",
        sub: "Reveal your local SQLite database and settings directory",
        keywords: "folder data vault sqlite directory finder explorer",
        run: () => {
          if (typeof desktop.openDataDir === "function") desktop.openDataDir();
        },
      }
    );
  }

  return commands;
}

function renderPaletteResults() {
  const query = $("cmd-palette-input").value.trim();
  const qLower = query.toLowerCase();
  const base = getBaseCommands();

  const items = [];
  // Smart quick-add detection when user types an amount (e.g. "coffee 6.50" or "paycheck +2400")
  if (query && /\d/.test(query)) {
    items.push({
      id: "quick-add-dynamic",
      icon: "zap",
      title: `Quick-Log: "${query}"`,
      sub: "Instant natural-language expense, income, or impulse entry",
      run: async () => {
        try {
          const res = await api("/api/quick-add", {
            method: "POST",
            body: JSON.stringify({ text: query }),
          });
          toast(res.message);
          await refreshAll();
        } catch (err) {
          toast(err.message, true);
        }
      },
    });
  }

  for (const cmd of base) {
    if (!qLower || cmd.title.toLowerCase().includes(qLower) || cmd.sub.toLowerCase().includes(qLower) || cmd.keywords.includes(qLower)) {
      items.push(cmd);
    }
  }

  state.paletteItems = items;
  if (state.paletteIndex >= items.length) state.paletteIndex = 0;

  const list = $("cmd-palette-list");
  if (!items.length) {
    list.replaceChildren(
      el("div", { class: "muted small", text: "No matching command. Try typing 'coffee 6.50' or 'light' or 'credit'.", style: { padding: "14px" } })
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
          idx === state.paletteIndex ? el("kbd", { text: "↵" }) : null,
        ]
      )
    )
  );
}

function openPalette() {
  state.paletteOpen = true;
  state.paletteIndex = 0;
  openOverlay($("cmd-palette-overlay"));
  $("cmd-palette-input").value = "";
  renderPaletteResults();
  $("cmd-palette-input").focus();
}

function closePalette() {
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

/* --- Dashboard Render --- */

function renderDashboard(dash) {
  state.dashboard = dash;
  const s = dash.safe_to_spend;

  // Header pills
  $("xp-level").textContent = `Lv ${dash.adhd_stats.level} · ${dash.adhd_stats.dopamine_xp} XP`;
  $("xp-streak").textContent = `${dash.adhd_stats.weekly_reset_streak}w streak`;

  const hasData = dash.recent_transactions.length > 0 || s.total_liquid_cash_cents > 0;
  $("btn-demo-quick").classList.toggle("hidden", hasData);

  // Sync Intelligence Mode & Provider indicators from dashboard payload
  if (dash.intelligence_mode) syncModeControls(dash.intelligence_mode);
  if (dash.active_ai_provider) {
    const pref = (state.settings && state.settings.ai_provider) || "auto";
    syncProviderControls(pref, dash.active_ai_provider);
  }

  // Badges
  const impCount = dash.cooling_impulses.length;
  $("badge-impulse").textContent = String(impCount);
  $("badge-impulse").classList.toggle("hidden", impCount === 0);

  const triageCount = dash.needs_triage_count;
  $("badge-triage").textContent = String(triageCount);
  $("badge-triage").classList.toggle("hidden", triageCount === 0);

  const creditAlerts = (dash.credit_summary && dash.credit_summary.alerts_count) || 0;
  $("badge-credit").textContent = String(creditAlerts);
  $("badge-credit").classList.toggle("hidden", creditAlerts === 0);

  // Hero Safe-to-Spend Gauge Ring + Smooth Tweened Numbers
  animateMoney($("hero-daily-safe"), s.daily_safe_to_spend_cents, "heroDailySafe");
  animateMoney($("hero-total-safe"), s.safe_to_spend_cents, "heroTotalSafe");
  $("hero-payday-date").textContent = fmtShortDate(s.next_payday_date);
  $("payday-chip").textContent = `${s.days_until_payday}d until payday`;

  const liquid = Math.max(0, Number(s.total_liquid_cash_cents) || 0);
  const safeTotal = Math.max(0, Number(s.safe_to_spend_cents) || 0);
  const ratio = liquid > 0 ? Math.max(0, Math.min(1, safeTotal / liquid)) : 0;
  const ringOffset = HERO_RING_CIRCUMFERENCE * (1 - ratio);
  requestAnimationFrame(() => {
    $("hero-ring-progress").setAttribute("stroke-dashoffset", ringOffset.toFixed(2));
  });
  animatePercent($("hero-ring-pct"), ratio * 100, "heroRingPct", 0);
  $("hero-ring-label").textContent = ratio >= 0.2 ? "free cash" : ratio > 0 ? "cozy" : "shielded";

  const mathBox = $("safe-math-breakdown");
  mathBox.replaceChildren(
    mathLine("Liquid cash (checking + savings + cash)", fmtMoney(s.total_liquid_cash_cents)),
    mathLine("− Unpaid bills due before payday", `−${fmtMoney(s.unpaid_bills_until_payday_cents)}`),
    mathLine("− Credit card payoff reserve", `−${fmtMoney(s.credit_card_reserved_cents)}`),
    mathLine("− Ring-fenced Safety & Invest jars", `−${fmtMoney(s.locked_jar_reserves_cents)}`),
    mathLine("− Emergency overdraft buffer", `−${fmtMoney(s.emergency_buffer_cents)}`),
    mathLine("= Safe to Spend Until Payday", fmtMoney(s.safe_to_spend_cents), true),
    mathLine(`÷ ${s.days_until_payday} days = Daily Safe Allowance`, fmtMoney(s.daily_safe_to_spend_cents), true)
  );

  // Side Hero
  animateMoney($("hero-safe-invest"), dash.safe_to_invest_cents, "heroSafeInvest");
  animateMoney($("hero-impulse-saved"), dash.adhd_stats.impulse_money_saved_cents, "heroImpulseSaved");
  $("hero-level-title").textContent = dash.adhd_stats.level_title;
  $("hero-grace-note").textContent = `${dash.adhd_stats.grace_days_available} streak grace freeze ready`;

  const cs = dash.credit_summary || {};
  const gCount = (dash.goals || []).length;
  if (cs.total_debt_cents > 0 || cs.aggregate_utilization_pct > 0) {
    $("hero-credit-pulse").textContent =
      `Util: ${Number(cs.aggregate_utilization_pct || 0).toFixed(1)}% · DTI: ${Number(cs.dti_pct || 0).toFixed(1)}% · ` +
      `Debt: ${fmtMoney(cs.total_debt_cents || 0)} · ${gCount} active goal(s)`;
  } else {
    $("hero-credit-pulse").textContent = `Zero tracked debt · ${gCount} active goal(s) — open Credit or Goals anytime.`;
  }

  // Rebalance Banner
  const rebalBanner = $("rebalance-banner");
  if (dash.rebalance_plans.length > 0) {
    rebalBanner.classList.remove("hidden");
    rebalBanner.replaceChildren(
      el("strong", { class: "inline-icon-row" }, [makeIcon("shield", "ui-icon-sm"), "Zero-Guilt Auto-Rebalance Ready"]),
      ...dash.rebalance_plans.map((p) =>
        el("div", { class: "rebal-row" }, [
          el("span", { text: p.calm_message }),
          el("button", {
            type: "button",
            class: "btn btn-primary btn-sm",
            text: `Shift ${fmtMoney(p.amount_cents)}`,
            "data-rebalance-from": String(p.from_jar_id),
            "data-rebalance-to": String(p.to_jar_id),
            "data-rebalance-amt": String(p.amount_cents),
          }),
        ])
      )
    );
  } else {
    rebalBanner.classList.add("hidden");
  }

  // 5 Calm Money Jars
  renderJars(dash.jars);

  // 30-Day Cashflow Runway & Quant Intelligence Panel
  if (dash.quant_forecast && state.quantDays === 30) {
    state.quantForecast = dash.quant_forecast;
    renderQuantForecast(dash.quant_forecast);
  } else if (state.quantForecast) {
    renderQuantForecast(state.quantForecast);
  } else {
    loadQuantForecast(state.quantDays);
  }

  // 14-Day Bill Radar
  const bills14 = dash.upcoming_bills.filter((b) => b.days_until_due <= 14);
  const dueSum = bills14.reduce((acc, b) => acc + b.amount_cents, 0);
  $("bills-total-pill").textContent = `${fmtMoney(dueSum)} due in 14d`;

  const radar = $("bill-radar-list");
  if (bills14.length === 0) {
    radar.replaceChildren(el("p", { class: "muted small", text: "No bills due in the next 14 days. You're clear!" }));
  } else {
    radar.replaceChildren(
      ...bills14.map((b) => {
        const dueLabel = b.days_until_due <= 0 ? "Due today" : `In ${b.days_until_due}d (${fmtShortDate(b.next_due_date)})`;
        const pills = [el("span", { class: "pill pill-soft", text: dueLabel })];
        if (b.price_hike_cents > 0) {
          pills.push(el("span", { class: "pill pill-watch", text: `+${fmtMoney(b.price_hike_cents)} price jump` }));
        }
        return el("div", { class: "item-box" }, [
          el("div", { class: "item-row" }, [
            el("strong", { text: b.name }),
            el("strong", { class: "num", text: fmtMoney(b.amount_cents) }),
          ]),
          el("div", { class: "item-row" }, pills),
        ]);
      })
    );
  }

  // Impulse Vault + Dopamine Wins
  renderImpulses(dash.cooling_impulses, dash.adhd_stats);
  renderDopamineWins(dash.adhd_stats.recent_wins);

  // Activity tab sub-panels
  renderAccounts(dash.accounts);
  renderBillAnomalies(dash.bill_anomalies || []);
  renderAllBills(dash.upcoming_bills);
  renderAuditLog(dash.recent_budget_events);
  populateSelectors(dash.jars, dash.accounts);
  renderGoals(dash.goals || []);

  // Hydration finish, staggered card entrance, tab indicator badge re-measure & Tray/HUD sync
  clearBootSkeletons();
  setVaultTitlebarState("active", "Local Vault Active");
  if (!state.hydrated) {
    state.hydrated = true;
    choreographEntrance($("tab-today"));
  }
  requestAnimationFrame(() => positionTabIndicator(true));
  pushTraySnapshot();
}

/* --- Interactive Pure-SVG 30/60-Day Cashflow Runway & Quant Intelligence --- */

async function loadQuantForecast(days = 30) {
  state.quantDays = days;
  $("quant-horizon-30").classList.toggle("active", days === 30);
  $("quant-horizon-30").setAttribute("aria-pressed", String(days === 30));
  $("quant-horizon-60").classList.toggle("active", days === 60);
  $("quant-horizon-60").setAttribute("aria-pressed", String(days === 60));

  try {
    const q = await api(`/api/quant/forecast?days=${days}`);
    state.quantForecast = q;
    renderQuantForecast(q);
  } catch (_err) {
    // Graceful silent fallback if endpoint is unavailable during startup
    renderQuantEmpty();
  }
}

function renderQuantEmpty() {
  $("quant-status-pill").className = "pill pill-soft";
  $("quant-status-pill").textContent = "Awaiting data";
  $("quant-chart-wrap").replaceChildren(
    el("p", { class: "muted small", text: "Add an account or load Demo Data to project your 30-day cashflow runway.", style: { padding: "18px 8px", margin: "0" } })
  );
  $("quant-alloc-grid").replaceChildren();
}

function renderQuantForecast(q) {
  if (!q || !Array.isArray(q.daily_points) || q.daily_points.length === 0) {
    renderQuantEmpty();
    return;
  }

  const pts = q.daily_points;
  const vol = q.volatility || {};
  const bufferCents =
    (state.dashboard && state.dashboard.safe_to_spend && state.dashboard.safe_to_spend.emergency_buffer_cents) ||
    vol.recommended_buffer_cents ||
    0;

  // Status pill
  const statusPill = $("quant-status-pill");
  if (q.overdraft_risk) {
    statusPill.className = "pill pill-rebalance";
    setNodeIconText(statusPill, "shield", `Overdraft Watch · ${q.safe_runway_days}d runway`);
  } else if (q.lowest_balance_cents < bufferCents) {
    statusPill.className = "pill pill-watch";
    setNodeIconText(statusPill, "alert", `Buffer Dip on ${fmtShortDate(q.lowest_balance_date)}`);
  } else {
    statusPill.className = "pill pill-calm";
    setNodeIconText(statusPill, "check", `Clear · ${q.safe_runway_days}d safe runway`);
  }

  // 4 Quant KPIs
  animateMoney($("quant-nadir-val"), q.lowest_balance_cents, "quantNadir");
  $("quant-nadir-date").textContent = `Lowest on ${fmtShortDate(q.lowest_balance_date)}`;

  animateMoney($("quant-ending-val"), q.ending_balance_cents, "quantEnding");
  const delta = (q.ending_balance_cents || 0) - (q.starting_balance_cents || 0);
  $("quant-ending-delta").textContent = `${delta >= 0 ? "+" : ""}${fmtMoney(delta)} over ${q.horizon_days || pts.length}d`;

  const stab = Math.round(Number(vol.stability_score_0_100) || 0);
  $("quant-stability-val").textContent = `${stab}/100`;
  $("quant-confidence-band").textContent =
    `P50 ${fmtMoney(vol.p50_daily_safe_cents || 0)}/d · P90 ${fmtMoney(vol.p90_conservative_daily_safe_cents || 0)}/d`;

  animateMoney($("quant-rec-buffer-val"), vol.recommended_buffer_cents || 0, "quantRecBuf");
  $("quant-rec-buffer-note").textContent =
    vol.p90_weekly_spend_cents > 0
      ? `P90 weekly spend: ${fmtMoney(vol.p90_weekly_spend_cents)}`
      : "Dynamic statistical cushion";

  // Payday crunch / overdraft notice
  const crunch = $("quant-crunch-notice");
  if (q.payday_crunch_warning) {
    crunch.replaceChildren(
      el("span", { class: "inline-icon-row" }, [makeIcon("shield", "ui-icon-sm"), q.payday_crunch_warning])
    );
    crunch.classList.remove("hidden");
  } else if (q.overdraft_risk) {
    crunch.replaceChildren(
      el("span", { class: "inline-icon-row" }, [
        makeIcon("shield", "ui-icon-sm"),
        `Projected balance dips to ${fmtMoney(q.lowest_balance_cents)} on ${fmtDayDate(q.lowest_balance_date)}. Shift a flexible jar or park non-essentials to stay smoothly above $0.`,
      ])
    );
    crunch.classList.remove("hidden");
  } else {
    crunch.classList.add("hidden");
  }

  // Render interactive pure-SVG Area + Line chart
  renderRunwaySvgChart(q, bufferCents);

  // Render Optimal Marginal-ROI Surplus Allocation cards
  renderOptimalAllocations(q.optimal_allocations || [], q.total_expected_annual_benefit_cents || 0);
}

function renderRunwaySvgChart(q, bufferCents) {
  const pts = q.daily_points;
  const wrap = $("quant-chart-wrap");

  const W = 820;
  const H = 225;
  const padL = 62;
  const padR = 18;
  const padT = 18;
  const padB = 30;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;

  const balances = pts.map((p) => p.balance_cents);
  const dataMin = Math.min(...balances);
  const dataMax = Math.max(...balances);
  let minVal = Math.min(dataMin, bufferCents > 0 ? bufferCents : dataMin);
  let maxVal = Math.max(dataMax, bufferCents > 0 ? bufferCents * 1.15 : dataMax);
  if (!Number.isFinite(minVal)) minVal = 0;
  if (!Number.isFinite(maxVal) || maxVal <= minVal) maxVal = minVal + 10000;
  const span = maxVal - minVal;
  const yMin = minVal >= 0 ? Math.max(0, minVal - span * 0.1) : minVal - span * 0.08;
  const yMax = maxVal + span * 0.1;

  const xFor = (i) => padL + (pts.length === 1 ? plotW / 2 : (i / (pts.length - 1)) * plotW);
  const yFor = (val) => padT + (1 - (val - yMin) / (yMax - yMin)) * plotH;

  const coords = pts.map((p, i) => ({ x: xFor(i), y: yFor(p.balance_cents), p, i }));
  const lineD = coords.map((c, i) => `${i === 0 ? "M" : "L"}${c.x.toFixed(1)},${c.y.toFixed(1)}`).join(" ");
  const baselineY = padT + plotH;
  const areaD = `${lineD} L${coords[coords.length - 1].x.toFixed(1)},${baselineY.toFixed(1)} L${coords[0].x.toFixed(1)},${baselineY.toFixed(1)} Z`;

  const strokeColor = q.overdraft_risk ? "var(--coral)" : q.lowest_balance_cents < bufferCents ? "var(--amber)" : "var(--primary)";
  const gradStopTop = q.overdraft_risk ? "#fb7185" : q.lowest_balance_cents < bufferCents ? "#fbbf24" : "#34d399";

  // SVG Defs
  const defs = svgEl("defs", {}, [
    svgEl("linearGradient", { id: "runwayAreaGrad", x1: "0", y1: "0", x2: "0", y2: "1" }, [
      svgEl("stop", { offset: "0%", "stop-color": gradStopTop, "stop-opacity": "0.32" }),
      svgEl("stop", { offset: "95%", "stop-color": gradStopTop, "stop-opacity": "0.01" }),
    ]),
  ]);

  // Y-axis horizontal gridlines (4 ticks)
  const gridNodes = [];
  for (let t = 0; t <= 3; t++) {
    const val = yMin + ((yMax - yMin) * t) / 3;
    const y = yFor(val);
    gridNodes.push(svgEl("line", { class: "runway-grid-line", x1: padL, y1: y.toFixed(1), x2: W - padR, y2: y.toFixed(1) }));
    gridNodes.push(
      svgEl("text", { class: "runway-axis-text", x: padL - 8, y: (y + 4).toFixed(1), "text-anchor": "end", text: fmtMoneyCompact(val) })
    );
  }

  // X-axis date ticks (5 evenly spaced)
  const tickCount = Math.min(6, pts.length);
  for (let k = 0; k < tickCount; k++) {
    const idx = tickCount === 1 ? 0 : Math.round((k * (pts.length - 1)) / (tickCount - 1));
    const c = coords[idx];
    const anchor = k === 0 ? "start" : k === tickCount - 1 ? "end" : "middle";
    gridNodes.push(
      svgEl("text", { class: "runway-axis-text", x: c.x.toFixed(1), y: H - 8, "text-anchor": anchor, text: fmtShortDate(c.p.date) })
    );
  }

  // Buffer & Zero threshold lines
  const refNodes = [];
  if (bufferCents > 0 && bufferCents >= yMin && bufferCents <= yMax) {
    const by = yFor(bufferCents);
    refNodes.push(svgEl("line", { class: "runway-buffer-line", x1: padL, y1: by.toFixed(1), x2: W - padR, y2: by.toFixed(1) }));
    refNodes.push(
      svgEl("text", {
        class: "runway-axis-text",
        x: W - padR - 4,
        y: (by - 5).toFixed(1),
        "text-anchor": "end",
        text: `Shield ${fmtMoneyCompact(bufferCents)}`,
      })
    );
  }
  if (0 >= yMin && 0 <= yMax && q.lowest_balance_cents <= bufferCents) {
    const zy = yFor(0);
    refNodes.push(svgEl("line", { class: "runway-zero-line", x1: padL, y1: zy.toFixed(1), x2: W - padR, y2: zy.toFixed(1) }));
  }

  // Nadir index & Payday markers
  let nadirIdx = pts.findIndex((p) => p.date === q.lowest_balance_date);
  if (nadirIdx < 0) nadirIdx = 0;

  const markerNodes = [];
  for (const c of coords) {
    if (c.p.inflow_cents > 0) {
      markerNodes.push(svgEl("circle", { class: "runway-payday-dot", cx: c.x.toFixed(1), cy: c.y.toFixed(1), r: "4.2" }));
    }
  }
  const nadirCoord = coords[nadirIdx];
  markerNodes.push(svgEl("circle", { class: "runway-nadir-dot", cx: nadirCoord.x.toFixed(1), cy: nadirCoord.y.toFixed(1), r: "5.2" }));

  // Interactive Crosshair + Active Dot
  const defaultIdx = nadirIdx;
  const activeCoord = coords[defaultIdx];
  const crosshair = svgEl("line", {
    class: "runway-crosshair",
    x1: activeCoord.x.toFixed(1),
    y1: padT,
    x2: activeCoord.x.toFixed(1),
    y2: baselineY,
  });
  const activeDot = svgEl("circle", {
    class: "runway-active-dot",
    cx: activeCoord.x.toFixed(1),
    cy: activeCoord.y.toFixed(1),
    r: "6",
  });

  const updateScrub = (idx) => {
    const safeIdx = Math.max(0, Math.min(coords.length - 1, idx));
    state.quantScrubIndex = safeIdx;
    const c = coords[safeIdx];
    crosshair.setAttribute("x1", c.x.toFixed(1));
    crosshair.setAttribute("x2", c.x.toFixed(1));
    activeDot.setAttribute("cx", c.x.toFixed(1));
    activeDot.setAttribute("cy", c.y.toFixed(1));

    const isNadir = safeIdx === nadirIdx;
    $("quant-scrub-date").textContent = `${fmtDayDate(c.p.date)}${isNadir ? " · Lowest Nadir" : ""}`;
    $("quant-scrub-balance").textContent = fmtMoney(c.p.balance_cents);
    $("quant-scrub-balance").style.color =
      c.p.balance_cents < 0 ? "var(--coral)" : c.p.balance_cents < bufferCents ? "var(--amber)" : "var(--primary)";

    const flowParts = [];
    if (c.p.inflow_cents > 0) flowParts.push(`+${fmtMoney(c.p.inflow_cents)} in`);
    if (c.p.outflow_cents > 0) flowParts.push(`−${fmtMoney(c.p.outflow_cents)} out`);
    $("quant-scrub-flow").textContent = flowParts.length ? flowParts.join(" · ") : "Steady day (no scheduled bills)";

    const evBox = $("quant-scrub-events");
    const evs = Array.isArray(c.p.events) ? c.p.events : [];
    evBox.replaceChildren(
      ...evs.slice(0, 4).map((ev) =>
        el("span", {
          class: `pill ${c.p.inflow_cents > 0 && /pay|income/i.test(ev) ? "pill-calm" : "pill-soft"}`,
          text: ev,
        })
      )
    );
  };

  // Hit columns + focusable points for mouse/touch/keyboard scrubbing
  const colW = plotW / Math.max(1, pts.length);
  const hitNodes = coords.map((c) => {
    const rect = svgEl("rect", {
      class: "runway-hit-point",
      x: (c.x - colW / 2).toFixed(1),
      y: padT,
      width: Math.max(10, colW).toFixed(1),
      height: plotH,
      tabindex: "0",
      role: "button",
      "aria-label": `${fmtDayDate(c.p.date)}: balance ${fmtMoney(c.p.balance_cents)}`,
    });
    rect.addEventListener("mouseenter", () => updateScrub(c.i));
    rect.addEventListener("focus", () => updateScrub(c.i));
    rect.addEventListener("click", () => updateScrub(c.i));
    rect.addEventListener("keydown", (e) => {
      if (e.key === "ArrowRight") {
        e.preventDefault();
        const next = Math.min(coords.length - 1, c.i + 1);
        hitNodes[next].focus();
      } else if (e.key === "ArrowLeft") {
        e.preventDefault();
        const prev = Math.max(0, c.i - 1);
        hitNodes[prev].focus();
      }
    });
    return rect;
  });

  const svg = svgEl(
    "svg",
    { class: "runway-svg", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `${q.horizon_days || pts.length}-day projected cashflow chart` },
    [
      defs,
      ...gridNodes,
      ...refNodes,
      svgEl("path", { d: areaD, fill: "url(#runwayAreaGrad)" }),
      svgEl("path", { class: "runway-line", d: lineD, stroke: strokeColor }),
      crosshair,
      ...markerNodes,
      activeDot,
      ...hitNodes,
    ]
  );

  // Smooth continuous pointer scrub (Apple Stocks feel on mouse & touch)
  svg.addEventListener("pointermove", (ev) => {
    const rect = svg.getBoundingClientRect();
    if (!rect.width) return;
    const relX = ((ev.clientX - rect.left) / rect.width) * W;
    const frac = Math.max(0, Math.min(1, (relX - padL) / plotW));
    const idx = Math.round(frac * (coords.length - 1));
    if (idx !== state.quantScrubIndex) updateScrub(idx);
  });

  wrap.replaceChildren(svg);
  updateScrub(defaultIdx);
}

function renderOptimalAllocations(steps, totalAnnualBenefitCents) {
  const grid = $("quant-alloc-grid");
  if (!steps || !steps.length) {
    grid.replaceChildren(
      el("div", {
        class: "item-box muted small",
        text: "No extra flexible surplus to allocate right now — bills and essentials are prioritized first.",
      })
    );
    return;
  }

  grid.replaceChildren(
    ...steps.map((st) => {
      const bucketName = BUCKET_LABELS[st.bucket] || String(st.bucket || "").replace(/_/g, " ");
      const aprVal = Number(st.effective_apr_equivalent_pct || 0);
      const aprLabel =
        aprVal >= 50
          ? "Protection Priority"
          : aprVal > 0
          ? `${aprVal.toFixed(1)}% eff. APR`
          : "Surplus Allocation";
      const benefitNote =
        st.expected_annual_benefit_cents > 0
          ? `≈ +${fmtMoney(st.expected_annual_benefit_cents)}/yr value`
          : null;

      return el("div", { class: "alloc-card" }, [
        el("div", { class: "item-row" }, [
          el("span", { class: "pill pill-soft", text: `#${st.priority_rank} · ${bucketName}` }),
          el("span", { class: `pill ${aprVal >= 12 ? "pill-calm" : "pill-blue"}`, text: aprLabel }),
        ]),
        el("strong", { text: st.target_name }),
        el("div", { class: "item-row" }, [
          el("span", { class: "alloc-amt", text: fmtMoney(st.amount_cents) }),
          benefitNote ? el("span", { class: "muted small", text: benefitNote }) : null,
        ]),
        el("div", { class: "small muted", text: st.rationale }),
        st.formula_note
          ? el("details", { class: "math-drawer" }, [
              el("summary", { text: "Formula & math proof" }),
              el("div", { class: "alloc-formula", text: st.formula_note }),
            ])
          : null,
      ]);
    })
  );

  if (totalAnnualBenefitCents > 0) {
    grid.appendChild(
      el("div", { class: "alloc-card plan-card-recommended" }, [
        el("span", { class: "pill pill-calm", text: "Total Quant Impact" }),
        el("strong", { text: "Combined Annualized Value" }),
        el("div", { class: "alloc-amt", text: `+${fmtMoney(totalAnnualBenefitCents)}/yr` }),
        el("div", {
          class: "small muted",
          text: "Estimated combined interest saved, promo cliff protection & liquidity yield across this allocation plan.",
        }),
      ])
    );
  }
}

function renderBillAnomalies(anomalies) {
  const box = $("bill-anomalies-list");
  if (!anomalies || !anomalies.length) {
    box.replaceChildren();
    return;
  }
  box.replaceChildren(
    ...anomalies.map((a) =>
      el("div", { class: "anomaly-box" }, [
        el("span", { class: "inline-icon-row" }, [makeIcon("alert", "ui-icon-sm"), a.summary]),
      ])
    )
  );
}

/* --- Credit Score & Debt Optimizer --- */

async function loadCreditReport() {
  try {
    const extraDollars = Number($("debt-extra-input") ? $("debt-extra-input").value : 250) || 250;
    const extraCents = Math.max(0, Math.round(extraDollars * 100));
    const rep = await api(`/api/credit/report?extra_monthly_cents=${extraCents}`);
    state.creditReport = rep;
    renderCreditReport(rep);
  } catch (err) {
    toast(err.message, true);
  }
}

function renderCreditReport(rep) {
  const utilPct = Number(rep.aggregate_utilization_pct || 0);
  animatePercent($("cred-agg-util"), utilPct, "credAggUtil", 1);

  // Apple Card-style Credit Utilization Arc Ring
  const clampedUtil = Math.max(0, Math.min(100, utilPct));
  const ringOffset = CRED_RING_CIRCUMFERENCE * (1 - clampedUtil / 100);
  const ringStroke =
    clampedUtil <= 8.9
      ? "var(--primary)"
      : clampedUtil <= 28.9
      ? "var(--sky)"
      : clampedUtil <= 48.9
      ? "var(--amber)"
      : "var(--coral)";
  const credRing = $("cred-ring-progress");
  credRing.setAttribute("stroke", ringStroke);
  requestAnimationFrame(() => {
    credRing.setAttribute("stroke-dashoffset", ringOffset.toFixed(2));
  });

  const tierLabels = {
    zero_inactivity_penalty: ["0% All-Zero Penalty", "pill-watch"],
    zero: ["0% Util", "pill-soft"],
    optimal_800_tier: ["800+ Optimal (<9%)", "pill-calm"],
    good: ["Good (<29%)", "pill-blue"],
    score_drag: ["Score Drag (29-48%)", "pill-watch"],
    high_drag: ["High Drag (49-88%)", "pill-rebalance"],
    maxed_severe: ["Maxed Cliff (89%+)", "pill-rebalance"],
  };
  const [tLabel, tCls] = tierLabels[rep.aggregate_tier] || [rep.aggregate_tier, "pill-soft"];
  $("cred-agg-tier").textContent = tLabel;
  $("cred-agg-tier").className = `pill ${tCls}`;

  animatePercent($("cred-dti"), rep.dti_pct, "credDti", 1);
  const dtiCls = rep.dti_pct <= 35 ? "pill-calm" : rep.dti_pct <= 43 ? "pill-watch" : "pill-rebalance";
  $("cred-dti-tier").textContent = rep.dti_tier.replace(/_/g, " ");
  $("cred-dti-tier").className = `pill ${dtiCls}`;

  animateMoney($("cred-total-debt"), rep.total_debt_cents, "credTotalDebt");
  $("cred-interest-burn").textContent = `${fmtMoney(rep.monthly_interest_burn_cents)}/mo interest burn`;

  $("cred-autopay-stat").textContent = rep.autopay_missing_count === 0 ? "All Protected" : `${rep.autopay_missing_count} Missing`;
  $("cred-age-stat").textContent =
    rep.avg_account_age_months != null
      ? `Avg age: ${(rep.avg_account_age_months / 12).toFixed(1)} yrs${rep.oldest_account_name ? ` (anchor: ${rep.oldest_account_name})` : ""}`
      : "Add open dates to track AAoA";

  // Niche Advisor Alerts
  const alertBox = $("credit-alerts-list");
  if (!rep.alerts.length) {
    alertBox.replaceChildren(
      el("p", { class: "small muted inline-icon-row" }, [
        makeIcon("check", "ui-icon-sm"),
        "Zero credit red flags — your credit setup looks calm and clean!",
      ])
    );
  } else {
    alertBox.replaceChildren(
      ...rep.alerts.map((a) => {
        const sevCls = a.severity === "high" ? "pill-rebalance" : a.severity === "medium" ? "pill-watch" : "pill-blue";
        const quickBtn =
          a.category === "autopay_shield" && a.credit_line_id
            ? el(
                "button",
                {
                  type: "button",
                  class: "btn btn-primary btn-xs",
                  "data-cl-autopay": String(a.credit_line_id),
                  "data-autopay-val": "minimum",
                },
                [makeIcon("check", "ui-icon-sm"), "Mark Autopay Enabled"]
              )
            : null;
        return el("div", { class: "item-box" }, [
          el("div", { class: "item-row" }, [
            el("strong", { text: a.title }),
            el("span", { class: `pill ${sevCls}`, text: a.severity }),
          ]),
          el("div", { class: "small", text: a.detail }),
          el("div", { class: "micro-step", text: `→ ${a.micro_step}` }),
          quickBtn ? el("div", { class: "row-gap" }, [quickBtn]) : null,
        ]);
      })
    );
  }

  // AZEO Statement-Close Calendar
  const azeoBox = $("credit-azeo-list");
  if (!rep.azeo_plan.length) {
    azeoBox.replaceChildren(
      el("p", { class: "small muted", text: "Add a credit card with a limit below to generate your exact AZEO statement-close dates." })
    );
  } else {
    azeoBox.replaceChildren(
      ...rep.azeo_plan.map((a) => {
        const roleBadge =
          a.role === "already_optimal"
            ? el("span", { class: "pill pill-calm", text: "Optimal" })
            : a.role === "primary_reporting_card"
            ? el("span", { class: "pill pill-blue", text: "Primary 1–4.9% Card" })
            : el("span", { class: "pill pill-watch", text: "Zero Out Before Close" });
        const payBtn =
          a.pay_before_close_cents > 0
            ? el(
                "button",
                {
                  type: "button",
                  class: "btn btn-soft btn-xs",
                  "data-cl-paydown": String(a.credit_line_id),
                  "data-paydown-amt": String(a.pay_before_close_cents),
                },
                [makeIcon("check", "ui-icon-sm"), `Record ${fmtMoney(a.pay_before_close_cents)} Paydown`]
              )
            : null;
        return el("div", { class: "item-box" }, [
          el("div", { class: "item-row" }, [el("strong", { text: a.card_name }), roleBadge]),
          el("div", { class: "small", text: a.explanation }),
          el("div", {
            class: "muted small",
            text: `Pay by ${a.pay_by_date} · Closes ${a.statement_close_date} → reports ${a.resulting_util_pct.toFixed(1)}%`,
          }),
          payBtn ? el("div", { class: "row-gap" }, [payBtn]) : null,
        ]);
      })
    );
  }

  // 4-Strategy Payoff Arena
  const stratGrid = $("debt-strategies-grid");
  if (!rep.strategies.length) {
    stratGrid.replaceChildren(
      el("div", { class: "card muted inline-icon-row" }, [
        makeIcon("trophy", "ui-icon-sm"),
        "Zero active debt balances — you're debt-free!",
      ])
    );
  } else {
    stratGrid.replaceChildren(
      ...rep.strategies.map((st) =>
        el("div", { class: `jar-card${st.recommended_for_adhd ? " plan-card-recommended" : ""}` }, [
          el("div", { class: "jar-top" }, [
            el("strong", { text: st.label }),
            st.recommended_for_adhd
              ? el("span", { class: "pill pill-calm" }, [makeIcon("star", "ui-icon-sm"), "Recommended"])
              : null,
          ]),
          el("div", { class: "small muted", text: st.tagline }),
          el("div", { class: "jar-avail", text: `${st.months_to_debt_free} mo` }),
          el("div", {
            class: "small",
            text: `Debt-free: ${st.debt_free_date} · Total interest: ${fmtMoney(st.total_interest_cents)}`,
          }),
          el("div", {
            class: "micro-step",
            text: `Saves ${fmtMoney(st.interest_saved_vs_minimums_cents)} vs minimums · First $0 win in ${st.first_win_months} mo (${st.first_target_name})`,
          }),
          el(
            "ul",
            { class: "step-list" },
            st.month_1_actions.map((m) => el("li", { text: m }))
          ),
        ])
      )
    );
  }

  // Credit Lines & Loans Manager
  const clBox = $("credit-lines-list");
  if (!rep.credit_lines.length) {
    clBox.replaceChildren(el("p", { class: "small muted", text: "No cards or loans tracked yet. Add one below!" }));
  } else {
    clBox.replaceChildren(
      ...rep.credit_lines.map((cl) => {
        const utilStr =
          cl.utilization_pct != null
            ? `${cl.utilization_pct.toFixed(1)}% of ${fmtMoney(cl.credit_limit_cents)}`
            : cl.kind.replace(/_/g, " ");
        const pills = [
          el("span", { class: "pill pill-soft", text: `${(cl.effective_apr_bps / 100).toFixed(2)}% APR` }),
          el("span", {
            class: `pill ${cl.autopay_mode === "none" ? "pill-rebalance" : "pill-calm"}`,
            text: `Autopay: ${cl.autopay_mode}`,
          }),
        ];
        if (cl.is_oldest_account) pills.push(el("span", { class: "pill pill-blue", text: "Oldest Anchor" }));
        if (cl.promo_days_remaining != null && cl.promo_days_remaining >= 0) {
          pills.push(el("span", { class: "pill pill-watch", text: `0% ends in ${cl.promo_days_remaining}d` }));
        }
        return el("div", { class: "item-box" }, [
          el("div", { class: "item-row" }, [
            el("div", {}, [
              el("strong", { text: cl.name }),
              el("div", {
                class: "muted small",
                text: `${utilStr} · Closes ${cl.next_statement_close_date} · Due ${cl.next_due_date}`,
              }),
            ]),
            el("strong", { class: "num", text: fmtMoney(cl.balance_cents) }),
          ]),
          el("div", { class: "item-row" }, [
            el("div", { class: "row-gap wrap" }, pills),
            el("div", { class: "row-gap" }, [
              cl.balance_cents > 0
                ? el("button", {
                    type: "button",
                    class: "btn btn-win btn-xs",
                    text: "−$100 Pay",
                    "data-cl-paydown": String(cl.id),
                    "data-paydown-amt": String(Math.min(10000, cl.balance_cents)),
                  })
                : null,
              el(
                "button",
                {
                  type: "button",
                  class: "btn btn-ghost btn-xs",
                  title: `Remove ${cl.name}`,
                  "aria-label": `Remove ${cl.name}`,
                  "data-cl-delete": String(cl.id),
                },
                [makeIcon("x", "ui-icon-sm")]
              ),
            ]),
          ]),
        ]);
      })
    );
  }
}

/* --- Goals & Credit-Safe Purchase Planner --- */

function renderGoals(goals) {
  const grid = $("goals-grid");
  if (!goals || !goals.length) {
    grid.replaceChildren(
      el("div", { class: "card muted", text: "No tracked goals yet. Optimize a gadget or savings target above and save it in 1 click!" })
    );
    return;
  }
  grid.replaceChildren(
    ...goals.map((g) =>
      el("div", { class: "jar-card" }, [
        el("div", { class: "jar-top" }, [
          el("strong", { text: g.name }),
          el("span", { class: `pill ${g.status === "funded" ? "pill-calm" : "pill-blue"}`, text: `${g.pct_funded.toFixed(0)}%` }),
        ]),
        el("div", { class: "jar-avail", text: `${fmtMoney(g.saved_cents)} / ${fmtMoney(g.target_cents)}` }),
        el("div", { class: "progress-track" }, [
          el("div", { class: "progress-fill", style: { width: `${Math.min(100, g.pct_funded)}%`, backgroundColor: "var(--primary)" } }),
        ]),
        el("div", { class: "muted small", text: `${fmtMoney(g.remaining_cents)} remaining${g.target_date ? ` · target ${g.target_date}` : ""}` }),
        g.notes ? el("div", { class: "small muted", text: g.notes }) : null,
        el("div", { class: "row-gap wrap" }, [
          el("button", {
            type: "button",
            class: "btn btn-win btn-sm",
            text: "+$50 Fund (+15 XP)",
            "data-goal-contrib": String(g.id),
            "data-contrib-amt": "5000",
          }),
          el(
            "button",
            {
              type: "button",
              class: "btn btn-soft btn-sm",
              "data-goal-plan-name": g.name,
              "data-goal-plan-target": String(g.target_cents),
              "data-goal-plan-saved": String(g.saved_cents),
            },
            [makeIcon("sparkles", "ui-icon-sm"), "Re-Optimize"]
          ),
          el(
            "button",
            {
              type: "button",
              class: "btn btn-ghost btn-sm",
              title: `Remove ${g.name}`,
              "aria-label": `Remove ${g.name}`,
              "data-goal-delete": String(g.id),
            },
            [makeIcon("x", "ui-icon-sm")]
          ),
        ]),
      ])
    )
  );
}

function renderGoalPlan(plan) {
  state.lastGoalPlan = plan;
  const out = $("goal-plan-output");
  out.classList.remove("hidden");

  const headerChildren = [
    el("div", { class: "item-row" }, [
      el("div", {}, [
        el("h3", { text: `Plan for ${plan.item_name} (${fmtMoney(plan.target_cents)} · ≈ ${plan.work_hours_cost}h of work)` }),
        el("p", {
          class: "muted small",
          text: `${fmtMoney(plan.remaining_cents)} remaining · Monthly flexible surplus: ${fmtMoney(plan.monthly_flexible_surplus_cents)}`,
        }),
      ]),
      el(
        "button",
        {
          type: "button",
          class: "btn btn-primary btn-sm",
          "data-save-planned-goal": "true",
        },
        [makeIcon("plus", "ui-icon-sm"), "Save as Tracked Goal"]
      ),
    ]),
  ];

  if (plan.high_apr_debt_warning) {
    headerChildren.push(
      el("div", { class: "anomaly-box" }, [
        el("span", { class: "inline-icon-row" }, [makeIcon("bulb", "ui-icon-sm"), plan.high_apr_debt_warning]),
      ])
    );
  }
  if (plan.accelerators && plan.accelerators.length) {
    headerChildren.push(
      el("div", { class: "chip-row" }, [
        el("span", { class: "chip-label", text: "Real accelerators from your data:" }),
        ...plan.accelerators.map((a) => el("span", { class: "pill pill-soft", text: a.label })),
      ])
    );
  }

  const cards = el(
    "div",
    { class: "jars-grid section-block" },
    plan.options.map((opt, idx) =>
      el("div", { class: `jar-card${opt.recommended ? " plan-card-recommended" : ""}` }, [
        el("div", { class: "jar-top" }, [
          el("strong", { text: opt.title }),
          opt.recommended
            ? el("span", { class: "pill pill-calm" }, [makeIcon("star", "ui-icon-sm"), "Recommended"])
            : el("span", {
                class: `pill ${opt.credit_safe ? "pill-blue" : "pill-rebalance"}`,
                text: opt.credit_safe ? "Credit-Safe" : "Credit Warning",
              }),
        ]),
        el("div", { class: "small", text: opt.summary }),
        el("div", {
          class: "muted small",
          text: `Ready: ${opt.projected_ready_date} (${opt.weeks_to_goal}w) · ${fmtMoney(opt.per_paycheck_cents)}/paycheck · Interest: ${fmtMoney(opt.interest_cost_cents)}`,
        }),
        el(
          "ul",
          { class: "step-list" },
          opt.steps.map((st) => el("li", { text: st }))
        ),
        el("div", { class: "micro-step", text: `Credit impact: ${opt.credit_impact_note}` }),
        el("div", { class: "row-gap", style: { marginTop: "auto", paddingTop: "6px" } }, [
          el(
            "button",
            {
              type: "button",
              class: `btn ${opt.recommended ? "btn-primary" : "btn-soft"} btn-sm`,
              "data-save-plan-option": String(idx),
            },
            [makeIcon("plus", "ui-icon-sm"), "Save Plan as Tracked Goal"]
          ),
        ]),
      ])
    )
  );

  out.replaceChildren(...headerChildren, cards);
}

/* --- 5 Calm Money Jars --- */

function renderJars(jars) {
  const jarsGrid = $("jars-grid");
  jarsGrid.replaceChildren(
    ...jars.map((j) => {
      const pct = Math.min(100, Math.max(0, j.pct_used));
      const healthClass = `pill pill-${j.health}`;
      const healthLabel = { calm: "Calm", watch: "Watch pace", rebalance: "Needs cushion" }[j.health] || j.health;
      const isEditing = state.editingJarId === j.id;

      const editor = isEditing
        ? el("form", { class: "inline-editor", "data-jar-edit-form": String(j.id) }, [
            el("label", { class: "field" }, [
              el("span", { text: "Target Share (%)" }),
              el("input", {
                type: "number",
                name: "target_pct",
                min: "0",
                max: "100",
                step: "1",
                value: String(j.target_pct),
              }),
            ]),
            el("label", { class: "checkbox-row" }, [
              el("input", { type: "checkbox", name: "rollover_enabled", checked: j.rollover_enabled }),
              el("span", { text: "Roll over unused surplus" }),
            ]),
            el("label", { class: "checkbox-row" }, [
              el("input", { type: "checkbox", name: "is_locked", checked: j.is_locked }),
              el("span", { text: "Ring-fence (shield from Safe-to-Spend)" }),
            ]),
            el("div", { class: "row-gap" }, [
              el("button", { type: "submit", class: "btn btn-primary btn-xs", text: "Save Jar" }),
              el("button", {
                type: "button",
                class: "btn btn-ghost btn-xs",
                text: "Cancel",
                "data-edit-jar-toggle": "",
              }),
            ]),
          ])
        : null;

      return el("div", { class: "jar-card" }, [
        el("div", { class: "jar-top" }, [
          el("span", { class: "jar-name" }, [
            el("span", { class: "jar-icon-badge", style: { color: j.color } }, [makeIcon(jarIconKey(j))]),
            j.name,
          ]),
          el("div", { class: "row-gap" }, [
            el("span", { class: healthClass, text: healthLabel }),
            el(
              "button",
              {
                type: "button",
                class: "btn btn-ghost btn-xs",
                title: `Edit ${j.name} settings`,
                "aria-label": `Edit ${j.name}`,
                "data-edit-jar-toggle": String(j.id),
              },
              [makeIcon("sliders", "ui-icon-sm")]
            ),
          ]),
        ]),
        el("div", {
          class: "jar-avail",
          text: fmtMoney(j.available_cents),
          style: { color: j.available_cents < 0 ? "var(--coral)" : j.color },
        }),
        el("div", { class: "progress-track" }, [
          el("div", {
            class: "progress-fill",
            style: { width: `${pct}%`, backgroundColor: j.available_cents < 0 ? "var(--coral)" : j.color },
          }),
        ]),
        el("div", {
          class: "muted small",
          text: `Spent ${fmtMoney(j.spent_cents)} of ${fmtMoney(j.auto_allocated_cents)} (${j.target_pct.toFixed(0)}% target)`,
        }),
        j.rollover_cents > 0
          ? el("div", { class: "muted small", text: `Includes +${fmtMoney(j.rollover_cents)} rolled over` })
          : null,
        el("div", { class: "small muted", text: j.calm_note }),
        editor,
      ]);
    })
  );
}

function mathLine(label, value, bold = false) {
  const l = el(bold ? "strong" : "span", { text: label });
  const r = el(bold ? "strong" : "span", { class: "num", text: value });
  return el("div", { class: "math-line" }, [l, r]);
}

function renderImpulses(impulses, stats) {
  $("impulse-vault-stats").textContent = `${stats.impulses_skipped_count} skipped · ${fmtMoney(stats.impulse_money_saved_cents)} kept`;
  const list = $("impulse-list");
  if (!impulses.length) {
    list.replaceChildren(
      el("div", { class: "card muted", text: "Nothing cooling right now. Next time an urge hits, park it here!" })
    );
    return;
  }
  list.replaceChildren(
    ...impulses.map((imp) => {
      const ready = imp.status === "ready";
      const badge = ready
        ? el("span", { class: "pill pill-calm", text: "Cooldown Done — Decide Clearly!" })
        : el("span", { class: "pill pill-blue" }, [makeIcon("snowflake", "ui-icon-sm"), `${imp.hours_remaining}h left`]);
      return el("div", { class: "jar-card" }, [
        el("div", { class: "jar-top" }, [
          el("strong", { text: imp.item_name }),
          badge,
        ]),
        el("div", { class: "jar-avail", text: fmtMoney(imp.price_cents) }),
        el("div", {
          class: "muted small",
          text: `≈ ${imp.work_hours_cost} hrs of work · delays savings goal ~${imp.goal_delay_days} days`,
        }),
        imp.reason ? el("div", { class: "small muted", text: `Trigger: ${imp.reason}` }) : null,
        el("div", { class: "row-gap wrap" }, [
          el(
            "button",
            {
              type: "button",
              class: "btn btn-win btn-sm",
              "data-impulse-decide": String(imp.id),
              "data-decision": "skip",
            },
            [makeIcon("trophy", "ui-icon-sm"), "Skipped It! (+75 XP & Sweep 50%)"]
          ),
          el("button", {
            type: "button",
            class: "btn btn-ghost btn-sm",
            text: imp.can_afford_safely ? "Still Want It (Fits Safe)" : "Buy Anyway",
            "data-impulse-decide": String(imp.id),
            "data-decision": "buy",
          }),
        ]),
      ]);
    })
  );
}

function renderDopamineWins(wins) {
  const box = $("dopamine-wins-list");
  if (!wins || !wins.length) {
    box.replaceChildren(el("p", { class: "muted small", text: "Park an impulse or finish a Weekly Reset to start stacking wins!" }));
    return;
  }
  box.replaceChildren(
    ...wins.map((w) =>
      el("div", { class: "win-item" }, [
        el("span", { class: "inline-icon-row" }, [makeIcon("sparkles", "ui-icon-sm"), w]),
      ])
    )
  );
}

function renderAccounts(accounts) {
  const box = $("accounts-list");
  box.replaceChildren(
    ...accounts.map((a) => {
      const isReconciling = state.reconcilingAcctId === a.id;
      const reconcileForm = isReconciling
        ? el("form", { class: "inline-editor", "data-reconcile-form": String(a.id) }, [
            el("div", { class: "row-gap" }, [
              el("input", {
                type: "number",
                name: "balance",
                step: "0.01",
                value: (a.balance_cents / 100).toFixed(2),
                placeholder: "Exact bank balance ($)",
                required: "true",
              }),
              el("button", { type: "submit", class: "btn btn-primary btn-xs", text: "Save" }),
              el("button", {
                type: "button",
                class: "btn btn-ghost btn-xs",
                text: "Cancel",
                "data-reconcile-toggle": "",
              }),
            ]),
          ])
        : null;

      return el("div", { class: "item-box" }, [
        el("div", { class: "item-row" }, [
          el("div", {}, [
            el("strong", { text: a.name }),
            el("div", { class: "muted small", text: `${a.account_type} · ${a.institution || "Local"}` }),
          ]),
          el("div", { class: "row-gap" }, [
            el("strong", { class: "num", text: fmtMoney(a.balance_cents) }),
            el("button", {
              type: "button",
              class: "btn btn-ghost btn-xs",
              text: "Reconcile",
              "data-reconcile-toggle": String(a.id),
            }),
          ]),
        ]),
        reconcileForm,
      ]);
    })
  );
}

function renderAllBills(bills) {
  $("all-bills-count").textContent = `${bills.length} active`;
  const box = $("all-bills-list");
  if (!bills.length) {
    box.replaceChildren(el("p", { class: "muted small", text: "No recurring bills yet. Add one below or import a bank CSV." }));
    return;
  }
  box.replaceChildren(
    ...bills.map((b) => {
      const dueStr = b.days_until_due <= 0 ? "Due today" : `Due in ${b.days_until_due}d (${b.next_due_date})`;
      return el("div", { class: "item-box" }, [
        el("div", { class: "item-row" }, [
          el("div", {}, [
            el("strong", { text: b.name }),
            el("div", { class: "muted small", text: `${dueStr} · ${b.cadence}${b.is_subscription ? " · sub" : ""}` }),
          ]),
          el("strong", { class: "num", text: fmtMoney(b.amount_cents) }),
        ]),
        el("div", { class: "item-row" }, [
          el("div", { class: "row-gap wrap" }, [
            b.price_hike_cents > 0 ? el("span", { class: "pill pill-watch", text: `+${fmtMoney(b.price_hike_cents)} hike` }) : null,
            b.flagged_zombie ? el("span", { class: "pill pill-rebalance", text: "Flagged to review" }) : null,
          ]),
          el("div", { class: "row-gap" }, [
            el("button", {
              type: "button",
              class: "btn btn-ghost btn-xs",
              text: b.flagged_zombie ? "Keep" : "Flag Review",
              "data-toggle-zombie": String(b.id),
              "data-zombie-next": String(!b.flagged_zombie),
            }),
            el("button", {
              type: "button",
              class: "btn btn-ghost btn-xs",
              text: "Archive",
              "data-archive-bill": String(b.id),
            }),
          ]),
        ]),
      ]);
    })
  );
}

function renderAuditLog(events) {
  const box = $("audit-log-list");
  if (!events.length) {
    box.replaceChildren(el("p", { class: "muted small", text: "Auto-budget events will appear here." }));
    return;
  }
  box.replaceChildren(
    ...events.map((e) =>
      el("div", { class: "item-box" }, [
        el("div", { class: "small", text: e.summary }),
        el("span", { class: "muted small", text: `${e.event_type} · ${e.created_at.replace("T", " ").replace("Z", " UTC")}` }),
      ])
    )
  );
}

function populateSelectors(jars, accounts) {
  const filterSel = $("tx-jar-filter");
  const prevFilter = filterSel.value;
  filterSel.replaceChildren(
    el("option", { value: "", text: "All Jars" }),
    ...jars.map((j) => el("option", { value: String(j.id), text: j.name }))
  );
  filterSel.value = prevFilter;

  const manualJar = $("manual-tx-jar");
  if (manualJar) {
    const prevManualJar = manualJar.value;
    manualJar.replaceChildren(
      el("option", { value: "", text: "Auto-Categorize Jar" }),
      ...jars.map((j) => el("option", { value: String(j.id), text: j.name }))
    );
    manualJar.value = prevManualJar;
  }

  const manualAcct = $("manual-tx-account");
  if (manualAcct) {
    const prevManualAcct = manualAcct.value;
    manualAcct.replaceChildren(
      ...accounts.map((a) => el("option", { value: String(a.id), text: `${a.name} (${a.account_type})` }))
    );
    if (prevManualAcct) manualAcct.value = prevManualAcct;
  }
}

/* --- Verified Intelligence Coach --- */

async function loadAISuggestions(refresh = false) {
  try {
    const batch = await api(`/api/ai/suggestions?refresh=${refresh ? "true" : "false"}`);
    if (batch.intelligence_mode) syncModeControls(batch.intelligence_mode);

    const prov = formatProvenance(batch);
    const badge = $("ai-source-badge");
    badge.className = `pill ${prov.cls}`;
    setNodeIconText(badge, prov.icon, prov.text);

    const notice = $("ai-notice");
    if (batch.notice) {
      notice.textContent = batch.notice;
      notice.classList.remove("hidden");
    } else {
      notice.classList.add("hidden");
    }

    const list = $("ai-suggestions-list");
    list.replaceChildren(
      ...batch.suggestions.map((s) => {
        const actionable = [
          "rebalance_jars",
          "review_subscription",
          "adjust_jar_target",
          "boost_emergency_buffer",
          "set_trading_cap",
          "park_impulse",
          "credit_paydown",
          "enable_autopay",
          "fund_goal",
        ].includes(s.action_type);
        const btn = actionable
          ? el(
              "button",
              {
                type: "button",
                class: "btn btn-primary btn-sm",
                "data-apply-action": s.action_type,
                "data-apply-payload": JSON.stringify(s.action_payload || {}),
              },
              [makeIcon("check", "ui-icon-sm"), "Apply in 1 Click"]
            )
          : s.action_type === "weekly_reset"
          ? el("button", { type: "button", class: "btn btn-soft btn-sm", text: "Open Weekly Reset →", "data-goto-tab": "reset" })
          : null;

        const rationaleDiv = el("div", { class: "small" });
        rationaleDiv.appendChild(renderFormattedText(s.rationale));
        return el("div", { class: "item-box" }, [
          el("div", { class: "item-row" }, [
            el("strong", { text: s.title }),
            el("span", { class: `pill pill-${s.priority === "high" ? "watch" : "soft"}`, text: s.priority }),
          ]),
          rationaleDiv,
          el("div", { class: "micro-step", text: `→ ${s.micro_step}` }),
          btn ? el("div", { class: "row-gap" }, [btn]) : null,
        ]);
      })
    );
  } catch (err) {
    toast(err.message, true);
  }
}

/* --- Weekly Reset --- */

async function loadWeeklyReset() {
  try {
    const data = await api("/api/weekly-reset");
    const pill = $("reset-status-pill");
    pill.className = data.already_completed_this_week ? "pill pill-calm" : "pill pill-watch";
    if (data.already_completed_this_week) {
      setNodeIconText(pill, "check", "Done this week!");
    } else {
      pill.textContent = "Ready (~3 min)";
    }

    const triage = $("reset-triage-list");
    const jars = (state.dashboard && state.dashboard.jars) || [];
    if (!data.needs_triage_transactions.length) {
      triage.replaceChildren(
        el("p", { class: "small inline-icon-row" }, [
          makeIcon("check", "ui-icon-sm"),
          "Zero uncategorized charges — all clean!",
        ])
      );
    } else {
      triage.replaceChildren(
        ...data.needs_triage_transactions.map((tx) =>
          el("div", { class: "item-box" }, [
            el("div", { class: "item-row" }, [
              el("strong", { text: `${tx.merchant} (${tx.tx_date})` }),
              el("strong", { class: "num", text: fmtMoney(tx.amount_cents) }),
            ]),
            el(
              "div",
              { class: "row-gap wrap" },
              jars.map((j) =>
                el(
                  "button",
                  {
                    type: "button",
                    class: "chip",
                    "data-triage-tx": String(tx.id),
                    "data-triage-jar": String(j.id),
                  },
                  [makeIcon(jarIconKey(j), "ui-icon-sm"), j.name]
                )
              )
            ),
          ])
        )
      );
    }

    const subs = $("reset-subs-list");
    if (!data.zombie_subscriptions.length) {
      subs.replaceChildren(el("p", { class: "small", text: "No risky subscriptions spotted." }));
    } else {
      subs.replaceChildren(
        ...data.zombie_subscriptions.map((b) =>
          el("div", { class: "item-box" }, [
            el("div", { class: "item-row" }, [
              el("span", { text: `${b.name} — ${fmtMoney(b.amount_cents)}/${b.cadence}` }),
              el("div", { class: "row-gap" }, [
                b.price_hike_cents > 0 ? el("span", { class: "pill pill-watch", text: `+${fmtMoney(b.price_hike_cents)} hike` }) : null,
                el("button", {
                  type: "button",
                  class: "btn btn-ghost btn-sm",
                  text: "Cancel / Archive",
                  "data-archive-bill": String(b.id),
                }),
              ]),
            ]),
          ])
        )
      );
    }

    const imps = $("reset-impulses-list");
    if (!data.matured_impulses.length) {
      imps.replaceChildren(el("p", { class: "small muted", text: "No matured impulse items waiting." }));
    } else {
      imps.replaceChildren(
        ...data.matured_impulses.map((imp) =>
          el("div", { class: "item-box" }, [
            el("div", { class: "item-row" }, [
              el("span", { text: `${imp.item_name} (${fmtMoney(imp.price_cents)})` }),
              el(
                "button",
                {
                  type: "button",
                  class: "btn btn-win btn-sm",
                  "data-impulse-decide": String(imp.id),
                  "data-decision": "skip",
                },
                [makeIcon("trophy", "ui-icon-sm"), "Claim Skipped Win (+75 XP)"]
              ),
            ]),
          ])
        )
      );
    }

    const rebal = $("reset-rebalance-list");
    if (!data.rebalance_plans.length) {
      rebal.replaceChildren(
        el("p", { class: "small inline-icon-row" }, [makeIcon("check", "ui-icon-sm"), "All 5 jars are balanced!"])
      );
    } else {
      rebal.replaceChildren(...data.rebalance_plans.map((p) => el("div", { class: "item-box small", text: p.calm_message })));
    }
  } catch (err) {
    toast(err.message, true);
  }
}

/* --- Activity Transactions (Search, Filter & Inline Edit/Delete) --- */

async function loadTransactions() {
  try {
    const q = $("tx-search").value.trim();
    const jarId = $("tx-jar-filter").value;
    const params = new URLSearchParams({ limit: "100" });
    if (q) params.set("search", q);
    if (jarId) params.set("jar_id", jarId);
    const txs = await api(`/api/transactions?${params.toString()}`);
    state.transactions = txs;
    renderTransactionsTable(txs);
  } catch (err) {
    toast(err.message, true);
  }
}

function renderTransactionsTable(txs) {
  const jars = (state.dashboard && state.dashboard.jars) || [];
  const tbody = $("tx-table-body");
  $("tx-count-pill").textContent = `${txs.length} ${txs.length === 1 ? "item" : "items"}`;

  if (!txs.length) {
    tbody.replaceChildren(
      el("tr", {}, [el("td", { colspan: "5", class: "muted small", text: "No matching transactions yet." })])
    );
    return;
  }

  const rows = [];
  for (const tx of txs) {
    const isEditing = state.editingTxId === tx.id;

    if (isEditing) {
      const jarSelect = el("select", { name: "jar_id", "aria-label": "Edit jar" }, [
        el("option", { value: "", text: tx.tx_type === "income" ? "Income" : "Unassigned" }),
        ...jars.map((j) => {
          const opt = el("option", { value: String(j.id), text: j.name });
          if (tx.jar_id === j.id) opt.selected = true;
          return opt;
        }),
      ]);
      if (tx.tx_type === "income") jarSelect.disabled = true;

      rows.push(
        el("tr", { class: "tx-edit-row" }, [
          el("td", { colspan: "5" }, [
            el("form", { class: "tx-edit-form", "data-tx-edit-form": String(tx.id) }, [
              el("input", { type: "date", name: "tx_date", value: tx.tx_date, required: "true", "aria-label": "Edit date" }),
              el("input", { type: "text", name: "merchant", value: tx.merchant, placeholder: "Merchant", required: "true", "aria-label": "Edit merchant" }),
              el("input", {
                type: "number",
                name: "amount",
                step: "0.01",
                min: "0.01",
                value: (tx.amount_cents / 100).toFixed(2),
                required: "true",
                "aria-label": "Edit amount",
              }),
              jarSelect,
              el("input", { type: "text", name: "memo", value: tx.memo || "", placeholder: "Memo (optional)", "aria-label": "Edit memo" }),
              el("div", { class: "row-gap" }, [
                el("button", { type: "submit", class: "btn btn-primary btn-xs", text: "Save" }),
                el("button", { type: "button", class: "btn btn-ghost btn-xs", text: "Cancel", "data-tx-edit-cancel": "true" }),
              ]),
            ]),
          ]),
        ])
      );
      continue;
    }

    const select = el("select", { "data-tx-jar-select": String(tx.id), "aria-label": `Jar for ${tx.merchant}` }, [
      el("option", { value: "", text: tx.tx_type === "income" ? "Income" : "Unassigned" }),
      ...jars.map((j) => {
        const opt = el("option", { value: String(j.id), text: j.name });
        if (tx.jar_id === j.id) opt.selected = true;
        return opt;
      }),
    ]);
    if (tx.tx_type === "income") select.disabled = true;

    rows.push(
      el("tr", {}, [
        el("td", { class: "num-inline", text: tx.tx_date }),
        el("td", {}, [
          el("strong", { text: tx.merchant }),
          tx.needs_review ? el("span", { class: "pill pill-watch", text: "Review", style: { marginLeft: "6px" } }) : null,
          tx.memo ? el("span", { class: "tx-memo-sub", text: tx.memo }) : null,
        ]),
        el("td", {}, [select]),
        el("td", {
          class: `num ${tx.tx_type === "income" ? "tx-income" : "tx-expense"}`,
          text: `${tx.tx_type === "income" ? "+" : "-"}${fmtMoney(tx.amount_cents)}`,
        }),
        el("td", { class: "num" }, [
          el("div", { class: "tx-actions" }, [
            el(
              "button",
              {
                type: "button",
                class: "btn btn-ghost btn-xs",
                title: `Edit ${tx.merchant}`,
                "aria-label": `Edit ${tx.merchant}`,
                "data-tx-edit": String(tx.id),
              },
              [makeIcon("edit", "ui-icon-sm")]
            ),
            el(
              "button",
              {
                type: "button",
                class: "btn btn-ghost btn-xs",
                title: `Delete ${tx.merchant}`,
                "aria-label": `Delete ${tx.merchant}`,
                "data-delete-tx": String(tx.id),
              },
              [makeIcon("x", "ui-icon-sm")]
            ),
          ]),
        ]),
      ])
    );
  }

  tbody.replaceChildren(...rows);
}

/* --- Settings, OpenCode Go & Google Gemini Models --- */

async function loadSettings() {
  try {
    const s = await api("/api/settings");
    state.settings = s;

    // Intelligence Mode & Provider Routing
    syncModeControls(s.intelligence_mode || "hybrid");
    syncProviderControls(s.ai_provider || "auto", s.active_provider_resolved || "none");

    // OpenCode Go
    $("set-opencode-url").value = s.opencode_base_url || "https://opencode.ai/zen/go/v1";
    $("set-opencode-proto").value = s.opencode_protocol || "auto";
    $("set-opencode-quota").value = String(s.opencode_max_daily_requests || 60);
    const ocBadge = $("key-status-badge");
    if (s.opencode_key_configured) {
      const srcTag = s.opencode_key_source === "env" ? "Env" : "Saved";
      ocBadge.className = "pill pill-calm";
      ocBadge.textContent = `${srcTag} · ${s.opencode_key_masked || "Configured"}`;
    } else {
      ocBadge.className = "pill pill-soft";
      ocBadge.textContent = "Not configured";
    }

    // Google Gemini
    $("set-gemini-url").value = s.gemini_base_url || "https://generativelanguage.googleapis.com/v1beta";
    const gemBadge = $("gemini-key-status-badge");
    if (s.gemini_api_key_set) {
      const srcTag = s.gemini_key_source === "env" ? "Env" : "Saved";
      gemBadge.className = "pill pill-blue";
      gemBadge.textContent = `${srcTag} · ${s.gemini_key_masked || "Configured"}`;
    } else {
      gemBadge.className = "pill pill-soft";
      gemBadge.textContent = "Optional · Not set";
    }

    // ADHD Profile
    $("set-hourly-wage").value = String(Math.round((s.hourly_wage_cents || 3000) / 100));
    $("set-emergency-buffer").value = String(Math.round((s.emergency_buffer_cents || 50000) / 100));
    $("set-cooling-hours").value = String(s.default_cooling_hours || 48);
    $("set-payday-cadence").value = String(s.payday_cadence_days || 14);
    $("set-next-payday").value = s.next_payday_date || "";
    $("set-expected-paycheck").value = String(Math.round((s.expected_paycheck_cents || 220000) / 100));

    $("btn-lock-now").classList.toggle("hidden", !s.auth_enabled);
    $("passcode-setup-form").classList.toggle("hidden", s.auth_enabled);
    $("passcode-disable-form").classList.toggle("hidden", !s.auth_enabled);

    await Promise.all([
      fetchProviderModels("opencode_go", false, s.opencode_model),
      fetchProviderModels("gemini", false, s.gemini_model),
    ]);
  } catch (err) {
    toast(err.message, true);
  }
}

async function fetchProviderModels(provider, forceRefresh, selectedModel) {
  const isGemini = provider === "gemini";
  const sel = isGemini ? $("set-gemini-model") : $("set-opencode-model");
  const fallbackModel = isGemini ? "gemini-2.5-flash" : "glm-5.3-flash";
  try {
    const res = await api(`/api/ai/models?provider=${encodeURIComponent(provider)}&refresh=${forceRefresh ? "true" : "false"}`);
    const models = res.models || [];
    if (isGemini) state.geminiModels = models;
    else state.opencodeModels = models;

    const target =
      selectedModel ||
      sel.value ||
      (state.settings && (isGemini ? state.settings.gemini_model : state.settings.opencode_model)) ||
      fallbackModel;
    const hasTarget = models.some((m) => m.id === target);
    const items = hasTarget ? models : [{ id: target, family: "Custom", note: "Current model" }, ...models];

    sel.replaceChildren(
      ...items.map((m) => {
        const opt = el("option", { value: m.id, text: `${m.id}${m.note || m.family ? ` — ${m.note || m.family}` : ""}` });
        if (m.id === target) opt.selected = true;
        return opt;
      })
    );
    if (forceRefresh) {
      const label = isGemini ? "Google Gemini" : "OpenCode Go";
      toast(res.live ? `Loaded ${models.length} live ${label} models!` : res.notice || `Loaded curated ${label} models.`);
    }
  } catch (err) {
    if (forceRefresh) toast(err.message, true);
    if (!sel.options.length) {
      sel.replaceChildren(el("option", { value: fallbackModel, text: fallbackModel }));
    }
  }
}

async function refreshAll(refreshAI = false) {
  const dash = await api("/api/dashboard");
  renderDashboard(dash);
  loadAISuggestions(refreshAI);
  if (state.quantDays !== 30) loadQuantForecast(state.quantDays);
  if (state.activeTab === "credit") loadCreditReport();
  if (state.activeTab === "reset") loadWeeklyReset();
  if (state.activeTab === "activity") loadTransactions();
}

/* --- Event Bindings --- */

document.addEventListener("DOMContentLoaded", async () => {
  initTheme();
  initTabIndicatorObserver();
  initCardSheen();
  showBootSkeletons();
  await initDesktopBridge();

  // Sensible date defaults for ADHD friction reduction
  if ($("bill-due")) $("bill-due").value = isoDateOffset(7);
  if ($("manual-tx-date")) $("manual-tx-date").value = isoDateOffset(0);

  // Theme & Command Palette controls
  $("btn-theme-toggle").addEventListener("click", toggleTheme);
  $("btn-open-palette").addEventListener("click", openPalette);
  $("cmd-palette-close").addEventListener("click", closePalette);
  $("cmd-palette-overlay").addEventListener("click", (e) => {
    if (e.target === $("cmd-palette-overlay")) closePalette();
  });

  $("cmd-palette-input").addEventListener("input", () => {
    state.paletteIndex = 0;
    renderPaletteResults();
  });

  $("cmd-palette-input").addEventListener("keydown", async (e) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (state.paletteItems.length) {
        state.paletteIndex = (state.paletteIndex + 1) % state.paletteItems.length;
        renderPaletteResults();
        const activeBtn = $("cmd-palette-list").querySelector(".palette-item.selected");
        if (activeBtn) activeBtn.scrollIntoView({ block: "nearest" });
      }
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (state.paletteItems.length) {
        state.paletteIndex = (state.paletteIndex - 1 + state.paletteItems.length) % state.paletteItems.length;
        renderPaletteResults();
        const activeBtn = $("cmd-palette-list").querySelector(".palette-item.selected");
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

  $("cmd-palette-list").addEventListener("click", async (e) => {
    const itemBtn = e.target.closest("button[data-palette-idx]");
    if (!itemBtn) return;
    await runPaletteItem(Number(itemBtn.dataset.paletteIdx));
  });

  // Global keyboard shortcuts: ⌘K/Ctrl+K Command Palette, '/' Quick-Add, 'Escape' closes editors/modals
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      if (state.paletteOpen) closePalette();
      else openPalette();
      return;
    }

    const tag = (e.target && e.target.tagName) || "";
    const inInput = ["INPUT", "TEXTAREA", "SELECT"].includes(tag);
    if (e.key === "/" && !inInput && !state.paletteOpen && !state.logsOpen) {
      e.preventDefault();
      switchTab("today");
      $("quick-add-input").focus();
    } else if (e.key === "Escape") {
      if (state.logsOpen) {
        closeBackendLogsModal();
        return;
      }
      if (state.paletteOpen) {
        closePalette();
        return;
      }
      if (state.editingJarId !== null || state.reconcilingAcctId !== null || state.editingTxId !== null) {
        state.editingJarId = null;
        state.reconcilingAcctId = null;
        state.editingTxId = null;
        if (state.dashboard) renderDashboard(state.dashboard);
        if (state.activeTab === "activity") renderTransactionsTable(state.transactions);
      }
    }
  });

  // Check auth lock state first
  try {
    const auth = await api("/api/auth/status");
    $("btn-lock-now").classList.toggle("hidden", !auth.auth_enabled);
    if (auth.auth_enabled && !auth.authenticated) {
      openLockOverlay();
    } else {
      await refreshAll();
    }
  } catch (err) {
    clearBootSkeletons();
    toast(err.message, true);
  }

  // Tabs (click + WAI-ARIA Left/Right arrow navigation)
  const tabBtns = Array.from(document.querySelectorAll(".tab"));
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

  // Unlock form
  $("unlock-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector("button[type=submit]");
    await withBusy(btn, async () => {
      try {
        await api("/api/auth/login", {
          method: "POST",
          body: JSON.stringify({ passcode: $("unlock-passcode").value }),
        });
        $("unlock-passcode").value = "";
        $("unlock-error").classList.add("hidden");
        closeLockOverlay();
        await refreshAll();
      } catch (err) {
        $("unlock-error").textContent = err.message;
        $("unlock-error").classList.remove("hidden");
      }
    });
  });

  $("btn-lock-now").addEventListener("click", async () => {
    await api("/api/auth/logout", { method: "POST" });
    openLockOverlay();
  });

  // Quick-add form & chips
  $("quick-add-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = $("quick-add-input");
    const text = input.value.trim();
    if (!text) return;
    const btn = e.target.querySelector("button[type=submit]");
    await withBusy(btn, async () => {
      try {
        const res = await api("/api/quick-add", {
          method: "POST",
          body: JSON.stringify({ text }),
        });
        input.value = "";
        toast(res.message);
        await refreshAll();
      } catch (err) {
        toast(err.message, true);
      }
    });
  });

  document.querySelectorAll("[data-quick]").forEach((chip) => {
    chip.addEventListener("click", () => {
      const val = chip.dataset.quick;
      $("quick-add-input").value = val;
      $("quick-add-input").focus();
      if (!val.endsWith(" ")) {
        $("quick-add-form").requestSubmit();
      }
    });
  });

  // Auto-Sync & Demo buttons
  $("btn-sync-now").addEventListener("click", (e) =>
    withBusy(e.currentTarget, async () => {
      const res = await api("/api/budget/sync", { method: "POST" });
      toast(`Auto-budget synced! Safe-to-Spend: ${fmtMoney(res.safe_to_spend_cents)}`);
      await refreshAll();
    })
  );

  const handleLoadDemo = (e) =>
    withBusy(e.currentTarget, async () => {
      try {
        await api("/api/demo/load", { method: "POST" });
        toast("Loaded realistic 90-day ADHD finance demo!");
        await refreshAll(true);
      } catch (err) {
        toast(err.message, true);
      }
    });
  $("btn-demo-quick").addEventListener("click", handleLoadDemo);
  $("btn-load-demo").addEventListener("click", handleLoadDemo);

  $("btn-refresh-ai").addEventListener("click", (e) => withBusy(e.currentTarget, () => loadAISuggestions(true)));

  // Quant Runway horizon toggles (30D / 60D)
  $("quant-horizon-30").addEventListener("click", () => loadQuantForecast(30));
  $("quant-horizon-60").addEventListener("click", () => loadQuantForecast(60));

  // Ask Coach
  $("ask-coach-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const q = $("ask-coach-input").value.trim();
    if (!q) return;
    const btn = e.target.querySelector("button[type=submit]");
    const ansBox = $("ask-coach-answer");
    ansBox.textContent = "Checking exact balances & thinking calmly…";
    ansBox.classList.remove("hidden");
    await withBusy(btn, async () => {
      try {
        const res = await api("/api/ai/ask", { method: "POST", body: JSON.stringify({ question: q }) });
        const prov = formatProvenance(res);
        const answerP = el("p", { style: { margin: "8px 0 0" } });
        answerP.appendChild(renderFormattedText(res.answer));
        ansBox.replaceChildren(
          el("div", { class: `pill ${prov.cls}` }, [makeIcon(prov.icon, "ui-icon-sm"), prov.text]),
          answerP
        );
      } catch (err) {
        ansBox.textContent = err.message;
      }
    });
  });

  // Impulse form
  $("impulse-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector("button[type=submit]");
    await withBusy(btn, async () => {
      try {
        const priceCents = dollarsToCents($("imp-price").value);
        await api("/api/impulses", {
          method: "POST",
          body: JSON.stringify({
            item_name: $("imp-name").value.trim(),
            price_cents: priceCents,
            cooling_hours: parseInt($("imp-hours").value, 10) || 48,
            reason: $("imp-reason").value.trim(),
          }),
        });
        $("impulse-form").reset();
        triggerCelebrate(btn, true);
        toast("Parked in Impulse Vault! (+15 XP for pausing!)");
        await refreshAll();
      } catch (err) {
        toast(err.message, true);
      }
    });
  });

  // Complete Weekly Reset
  $("btn-complete-reset").addEventListener("click", (e) =>
    withBusy(e.currentTarget, async () => {
      try {
        await api("/api/weekly-reset/complete", {
          method: "POST",
          body: JSON.stringify({ apply_rebalances: true }),
        });
        triggerCelebrate(e.currentTarget, true);
        toast("Weekly Money Reset complete! +100 XP & streak protected!");
        await refreshAll();
      } catch (err) {
        toast(err.message, true);
      }
    })
  );

  // Detailed Manual Transaction form
  $("manual-tx-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector("button[type=submit]");
    await withBusy(btn, async () => {
      try {
        const amountCents = dollarsToCents($("manual-tx-amount").value);
        const jarVal = $("manual-tx-jar").value;
        const acctVal = $("manual-tx-account").value;
        await api("/api/transactions", {
          method: "POST",
          body: JSON.stringify({
            tx_type: $("manual-tx-type").value,
            merchant: $("manual-tx-merchant").value.trim(),
            amount_cents: amountCents,
            tx_date: $("manual-tx-date").value || isoDateOffset(0),
            account_id: acctVal ? Number(acctVal) : null,
            jar_id: jarVal ? Number(jarVal) : null,
            memo: $("manual-tx-memo").value.trim(),
            source: "manual_detailed",
          }),
        });
        $("manual-tx-form").reset();
        $("manual-tx-date").value = isoDateOffset(0);
        toast("Transaction saved & auto-budget updated!");
        await refreshAll();
      } catch (err) {
        toast(err.message, true);
      }
    });
  });

  // Account form
  $("account-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector("button[type=submit]");
    await withBusy(btn, async () => {
      try {
        const balanceCents = dollarsToCents($("acct-balance").value, { allowNegative: true, allowZero: true });
        await api("/api/accounts", {
          method: "POST",
          body: JSON.stringify({
            name: $("acct-name").value.trim(),
            account_type: $("acct-type").value,
            balance_cents: balanceCents,
            is_liquid: ["checking", "savings", "cash"].includes($("acct-type").value),
          }),
        });
        $("account-form").reset();
        toast("Account added & Safe-to-Spend updated!");
        await refreshAll();
      } catch (err) {
        toast(err.message, true);
      }
    });
  });

  // Multi-Format Statement Preview & Import (supports browser picker + native Electron picker)
  $("csv-file-input").addEventListener("change", () => {
    if ($("csv-file-input").files.length > 0) {
      state.desktopStatementFile = null;
      $("desktop-selected-statement").classList.add("hidden");
    }
  });

  const buildStatementFormData = () => {
    const file = $("csv-file-input").files[0] || state.desktopStatementFile;
    if (!file) throw new Error("Choose a statement file (.csv, .ofx, .qfx, .qif, or .json) first.");
    const fd = new FormData();
    fd.append("file", file, file.name || "statement.csv");
    fd.append("flip_expense_sign", $("csv-flip-sign").checked ? "true" : "false");
    fd.append("detect_transfers", $("csv-detect-transfers").checked ? "true" : "false");
    fd.append("sync_ofx_balance", $("csv-sync-balance").checked ? "true" : "false");
    return fd;
  };

  $("btn-preview-statement").addEventListener("click", (e) =>
    withBusy(e.currentTarget, async () => {
      try {
        const fd = buildStatementFormData();
        const res = await api("/api/import/preview", { method: "POST", body: fd });
        const box = $("csv-import-result");
        const balNote = res.detected_balance_cents != null ? ` · OFX Balance: ${fmtMoney(res.detected_balance_cents)}` : "";
        box.textContent =
          `Preview (${res.format_detected.toUpperCase()}): ${res.new_count} new row(s), ${res.duplicate_count} duplicate(s), ` +
          `${res.transfers_detected} internal transfer(s) detected${balNote}. Click Import to apply!`;
        box.classList.remove("hidden");
      } catch (err) {
        toast(err.message, true);
      }
    })
  );

  $("csv-upload-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector("button[type=submit]");
    await withBusy(btn, async () => {
      try {
        const fd = buildStatementFormData();
        const res = await api("/api/import/statement", { method: "POST", body: fd });
        const box = $("csv-import-result");
        box.textContent =
          `Imported ${res.imported_count} ${res.format_detected.toUpperCase()} transaction(s) ` +
          `(${res.duplicate_count} duplicate(s) skipped, ${res.transfers_detected} transfer(s) isolated). ` +
          `Detected ${res.sync.bills_detected} recurring bill(s).`;
        box.classList.remove("hidden");
        state.desktopStatementFile = null;
        $("desktop-selected-statement").classList.add("hidden");
        toast(`Imported ${res.imported_count} ${res.format_detected.toUpperCase()} transactions!`);
        await refreshAll();
      } catch (err) {
        toast(err.message, true);
      }
    });
  });

  // Credit & Debt Forms
  $("debt-extra-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    await loadCreditReport();
    toast("Recalculated all 4 debt strategies!");
  });

  $("credit-line-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector("button[type=submit]");
    await withBusy(btn, async () => {
      try {
        const balCents = dollarsToCents($("cl-balance").value, { allowZero: true });
        const limCents = dollarsToCents($("cl-limit").value || "0", { allowZero: true });
        const aprBps = Math.round((Number($("cl-apr").value) || 0) * 100);
        const body = {
          name: $("cl-name").value.trim(),
          kind: $("cl-kind").value,
          balance_cents: balCents,
          credit_limit_cents: limCents,
          apr_bps: aprBps,
          statement_closing_day: parseInt($("cl-close-day").value, 10) || 15,
          payment_due_day: parseInt($("cl-due-day").value, 10) || 10,
          autopay_mode: $("cl-autopay").value,
          has_grace_period: $("cl-grace").checked,
        };
        if ($("cl-promo-ends").value) {
          body.promo_apr_bps = 0;
          body.promo_ends_date = $("cl-promo-ends").value;
        }
        if ($("cl-opened").value) body.opened_date = $("cl-opened").value;
        await api("/api/credit/lines", { method: "POST", body: JSON.stringify(body) });
        $("credit-line-form").reset();
        toast("Credit line added — AZEO & payoff strategies updated!");
        await refreshAll();
        await loadCreditReport();
      } catch (err) {
        toast(err.message, true);
      }
    });
  });

  $("bt-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector("button[type=submit]");
    await withBusy(btn, async () => {
      try {
        const res = await api("/api/credit/balance-transfer", {
          method: "POST",
          body: JSON.stringify({
            balance_cents: dollarsToCents($("bt-balance").value),
            current_apr_bps: Math.round((Number($("bt-apr").value) || 0) * 100),
            transfer_fee_bps: Math.round((Number($("bt-fee").value) || 0) * 100),
            promo_months: parseInt($("bt-months").value, 10) || 15,
            monthly_payment_cents: dollarsToCents($("bt-pay").value),
          }),
        });
        const box = $("bt-result");
        box.classList.remove("hidden");
        box.replaceChildren(
          el("span", {
            class: `pill ${res.verdict === "strong_yes" ? "pill-calm" : res.verdict === "marginal" ? "pill-watch" : "pill-rebalance"}`,
            text: res.verdict.replace(/_/g, " ").toUpperCase(),
          }),
          el("p", { text: res.explanation, style: { margin: "8px 0 4px" } }),
          el("div", {
            class: "muted small",
            text: `Fee: ${fmtMoney(res.transfer_fee_cents)} · Required to beat promo cliff: ${fmtMoney(res.required_monthly_to_clear_promo_cents)}/mo`,
          })
        );
      } catch (err) {
        toast(err.message, true);
      }
    });
  });

  // Goal & Purchase Optimizer Form
  $("goal-plan-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector("button[type=submit]");
    await withBusy(btn, async () => {
      try {
        const body = {
          item_name: $("gp-name").value.trim(),
          target_cents: dollarsToCents($("gp-price").value),
          saved_cents: dollarsToCents($("gp-saved").value || "0", { allowZero: true }),
          prefer_credit_rewards: true,
        };
        if ($("gp-date").value) body.target_date = $("gp-date").value;
        const plan = await api("/api/goals/plan", { method: "POST", body: JSON.stringify(body) });
        renderGoalPlan(plan);
      } catch (err) {
        toast(err.message, true);
      }
    });
  });

  // Bill Form
  $("bill-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector("button[type=submit]");
    await withBusy(btn, async () => {
      try {
        const amountCents = dollarsToCents($("bill-amount").value);
        await api("/api/bills", {
          method: "POST",
          body: JSON.stringify({
            name: $("bill-name").value.trim(),
            amount_cents: amountCents,
            cadence: $("bill-cadence").value,
            next_due_date: $("bill-due").value,
            is_subscription: $("bill-is-sub").checked,
            is_essential: !$("bill-is-sub").checked,
          }),
        });
        $("bill-form").reset();
        $("bill-due").value = isoDateOffset(7);
        toast("Bill added to your 14-Day Radar & Safe-to-Spend shield!");
        await refreshAll();
      } catch (err) {
        toast(err.message, true);
      }
    });
  });

  // Transaction search & filter
  $("tx-search").addEventListener("input", () => loadTransactions());
  $("tx-jar-filter").addEventListener("change", () => loadTransactions());

  // Settings: OpenCode Go
  $("opencode-settings-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector("button[type=submit]");
    await withBusy(btn, async () => {
      const body = {
        opencode_base_url: $("set-opencode-url").value.trim(),
        opencode_model: $("set-opencode-model").value,
        opencode_protocol: $("set-opencode-proto").value,
        opencode_max_daily_requests: parseInt($("set-opencode-quota").value, 10) || 60,
      };
      const keyVal = $("set-opencode-key").value.trim();
      if (keyVal) body.opencode_api_key = keyVal;
      try {
        await api("/api/settings", { method: "PATCH", body: JSON.stringify(body) });
        $("set-opencode-key").value = "";
        toast("OpenCode Go settings saved securely!");
        await loadSettings();
        await loadAISuggestions(true);
      } catch (err) {
        toast(err.message, true);
      }
    });
  });

  $("btn-clear-opencode-key").addEventListener("click", (e) =>
    withBusy(e.currentTarget, async () => {
      try {
        await api("/api/settings", { method: "PATCH", body: JSON.stringify({ clear_opencode_key: true }) });
        toast("Cleared saved OpenCode Go key (will use OPENCODE_GO_API_KEY env var if set).");
        await loadSettings();
      } catch (err) {
        toast(err.message, true);
      }
    })
  );

  $("btn-fetch-models").addEventListener("click", (e) => withBusy(e.currentTarget, () => fetchProviderModels("opencode_go", true)));

  $("btn-test-opencode").addEventListener("click", (e) =>
    withBusy(e.currentTarget, async () => {
      const out = $("opencode-test-result");
      out.textContent = "Testing OpenCode Go connection…";
      out.classList.remove("hidden");
      try {
        const res = await api("/api/ai/test?provider=opencode_go", { method: "POST" });
        setNodeIconText(out, res.ok ? "check" : "alert", res.message);
      } catch (err) {
        setNodeIconText(out, "alert", err.message);
      }
    })
  );

  // Settings: Google Gemini (Optional)
  $("gemini-settings-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector("button[type=submit]");
    await withBusy(btn, async () => {
      const body = {
        gemini_base_url: $("set-gemini-url").value.trim(),
        gemini_model: $("set-gemini-model").value,
      };
      const keyVal = $("set-gemini-key").value.trim();
      if (keyVal) body.gemini_api_key = keyVal;
      try {
        await api("/api/settings", { method: "PATCH", body: JSON.stringify(body) });
        $("set-gemini-key").value = "";
        toast("Google Gemini settings saved securely (key never echoed back)!");
        await loadSettings();
        await loadAISuggestions(true);
      } catch (err) {
        toast(err.message, true);
      }
    });
  });

  $("btn-clear-gemini-key").addEventListener("click", (e) =>
    withBusy(e.currentTarget, async () => {
      try {
        await api("/api/settings", { method: "PATCH", body: JSON.stringify({ clear_gemini_key: true }) });
        toast("Cleared saved Gemini key (will use GEMINI_API_KEY env var if set).");
        await loadSettings();
      } catch (err) {
        toast(err.message, true);
      }
    })
  );

  $("btn-fetch-gemini-models").addEventListener("click", (e) => withBusy(e.currentTarget, () => fetchProviderModels("gemini", true)));

  $("btn-test-gemini").addEventListener("click", (e) =>
    withBusy(e.currentTarget, async () => {
      const out = $("gemini-test-result");
      out.textContent = "Testing Google Gemini connection…";
      out.classList.remove("hidden");
      try {
        const res = await api("/api/ai/test?provider=gemini", { method: "POST" });
        setNodeIconText(out, res.ok ? "check" : "alert", res.message);
      } catch (err) {
        setNodeIconText(out, "alert", err.message);
      }
    })
  );

  // Settings: ADHD Profile
  $("profile-settings-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector("button[type=submit]");
    await withBusy(btn, async () => {
      try {
        const body = {
          hourly_wage_cents: dollarsToCents($("set-hourly-wage").value),
          emergency_buffer_cents: dollarsToCents($("set-emergency-buffer").value, { allowZero: true }),
          default_cooling_hours: parseInt($("set-cooling-hours").value, 10) || 48,
          payday_cadence_days: parseInt($("set-payday-cadence").value, 10) || 14,
          expected_paycheck_cents: dollarsToCents($("set-expected-paycheck").value, { allowZero: true }),
        };
        if ($("set-next-payday").value) body.next_payday_date = $("set-next-payday").value;
        await api("/api/settings", { method: "PATCH", body: JSON.stringify(body) });
        toast("ADHD profile updated & Safe-to-Spend recalculated!");
        await refreshAll();
      } catch (err) {
        toast(err.message, true);
      }
    });
  });

  // Restore JSON Backup
  $("backup-import-input").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    try {
      await api("/api/backup/import", { method: "POST", body: fd });
      e.target.value = "";
      toast("Backup restored cleanly!");
      await refreshAll(true);
    } catch (err) {
      toast(err.message, true);
    }
  });

  // Security Passcode Enable / Disable
  $("passcode-setup-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector("button[type=submit]");
    await withBusy(btn, async () => {
      try {
        await api("/api/auth/setup", {
          method: "POST",
          body: JSON.stringify({ passcode: $("new-passcode").value }),
        });
        $("new-passcode").value = "";
        toast("Local passcode lock enabled!");
        await loadSettings();
      } catch (err) {
        toast(err.message, true);
      }
    });
  });

  $("passcode-disable-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector("button[type=submit]");
    await withBusy(btn, async () => {
      try {
        await api("/api/auth/disable", {
          method: "POST",
          body: JSON.stringify({ passcode: $("disable-passcode").value }),
        });
        $("disable-passcode").value = "";
        toast("Local passcode lock disabled.");
        await loadSettings();
      } catch (err) {
        toast(err.message, true);
      }
    });
  });

  // Delegated inline form submits (Jar editor, Account reconcile, Inline Transaction editor)
  document.body.addEventListener("submit", async (e) => {
    const jarForm = e.target.closest("form[data-jar-edit-form]");
    if (jarForm) {
      e.preventDefault();
      const jarId = Number(jarForm.dataset.jarEditForm);
      const targetPct = Number(jarForm.elements.target_pct.value);
      if (!Number.isFinite(targetPct) || targetPct < 0 || targetPct > 100) {
        return toast("Target share must be between 0% and 100%.", true);
      }
      try {
        await api(`/api/jars/${jarId}`, {
          method: "PATCH",
          body: JSON.stringify({
            target_pct: targetPct,
            rollover_enabled: jarForm.elements.rollover_enabled.checked,
            is_locked: jarForm.elements.is_locked.checked,
          }),
        });
        state.editingJarId = null;
        toast("Jar updated & auto-budget recalculated!");
        await refreshAll();
      } catch (err) {
        toast(err.message, true);
      }
      return;
    }

    const recForm = e.target.closest("form[data-reconcile-form]");
    if (recForm) {
      e.preventDefault();
      const acctId = Number(recForm.dataset.reconcileForm);
      try {
        const balanceCents = dollarsToCents(recForm.elements.balance.value, { allowNegative: true, allowZero: true });
        await api(`/api/accounts/${acctId}/balance`, {
          method: "PATCH",
          body: JSON.stringify({ balance_cents: balanceCents }),
        });
        state.reconcilingAcctId = null;
        toast("Account balance reconciled!");
        await refreshAll();
      } catch (err) {
        toast(err.message, true);
      }
      return;
    }

    const txEditForm = e.target.closest("form[data-tx-edit-form]");
    if (txEditForm) {
      e.preventDefault();
      const txId = Number(txEditForm.dataset.txEditForm);
      try {
        const amountCents = dollarsToCents(txEditForm.elements.amount.value);
        const payload = {
          tx_date: txEditForm.elements.tx_date.value,
          merchant: txEditForm.elements.merchant.value.trim(),
          amount_cents: amountCents,
          memo: txEditForm.elements.memo.value.trim(),
          learn_merchant_rule: true,
        };
        const jarVal = txEditForm.elements.jar_id && !txEditForm.elements.jar_id.disabled ? txEditForm.elements.jar_id.value : "";
        if (jarVal) payload.jar_id = Number(jarVal);
        await api(`/api/transactions/${txId}`, {
          method: "PATCH",
          body: JSON.stringify(payload),
        });
        state.editingTxId = null;
        toast("Transaction updated & auto-budget recalculated!");
        await refreshAll();
      } catch (err) {
        toast(err.message, true);
      }
    }
  });

  // Delegated click handlers (strict CSP compliant — zero inline handlers)
  document.body.addEventListener("click", async (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;

    if (btn.dataset.setMode) {
      await setIntelligenceMode(btn.dataset.setMode);
      return;
    }

    if (btn.dataset.setProvider) {
      await setPreferredProvider(btn.dataset.setProvider);
      return;
    }

    if (btn.dataset.gotoTab) {
      switchTab(btn.dataset.gotoTab);
      return;
    }

    if (btn.dataset.editJarToggle !== undefined) {
      const nextId = btn.dataset.editJarToggle ? Number(btn.dataset.editJarToggle) : null;
      state.editingJarId = state.editingJarId === nextId ? null : nextId;
      if (state.dashboard) renderJars(state.dashboard.jars);
      return;
    }

    if (btn.dataset.reconcileToggle !== undefined) {
      const nextId = btn.dataset.reconcileToggle ? Number(btn.dataset.reconcileToggle) : null;
      state.reconcilingAcctId = state.reconcilingAcctId === nextId ? null : nextId;
      if (state.dashboard) renderAccounts(state.dashboard.accounts);
      return;
    }

    if (btn.dataset.txEdit) {
      state.editingTxId = Number(btn.dataset.txEdit);
      renderTransactionsTable(state.transactions);
      return;
    }

    if (btn.dataset.txEditCancel) {
      state.editingTxId = null;
      renderTransactionsTable(state.transactions);
      return;
    }

    if (btn.dataset.rebalanceFrom) {
      await withBusy(btn, async () => {
        try {
          await api("/api/budget/rebalance", {
            method: "POST",
            body: JSON.stringify({
              from_jar_id: Number(btn.dataset.rebalanceFrom),
              to_jar_id: Number(btn.dataset.rebalanceTo),
              amount_cents: Number(btn.dataset.rebalanceAmt),
              reason: "One-click calm rebalance",
            }),
          });
          triggerCelebrate(null);
          toast("Jars rebalanced smoothly!");
          await refreshAll();
        } catch (err) {
          toast(err.message, true);
        }
      });
      return;
    }

    if (btn.dataset.applyAction) {
      await withBusy(btn, async () => {
        try {
          const res = await api("/api/ai/suggestions/apply", {
            method: "POST",
            body: JSON.stringify({
              action_type: btn.dataset.applyAction,
              action_payload: JSON.parse(btn.dataset.applyPayload || "{}"),
            }),
          });
          triggerCelebrate(btn.closest(".item-box"));
          toast(res.message || "Applied!");
          await refreshAll(true);
        } catch (err) {
          toast(err.message, true);
        }
      });
      return;
    }

    if (btn.dataset.impulseDecide) {
      await withBusy(btn, async () => {
        try {
          const res = await api(`/api/impulses/${btn.dataset.impulseDecide}/decide`, {
            method: "POST",
            body: JSON.stringify({ decision: btn.dataset.decision, sweep_savings_pct: 50 }),
          });
          if (btn.dataset.decision === "skip") triggerCelebrate(btn.closest(".jar-card") || btn, true);
          toast(res.message);
          await refreshAll();
        } catch (err) {
          toast(err.message, true);
        }
      });
      return;
    }

    if (btn.dataset.triageTx) {
      await withBusy(btn, async () => {
        try {
          await api(`/api/transactions/${btn.dataset.triageTx}`, {
            method: "PATCH",
            body: JSON.stringify({ jar_id: Number(btn.dataset.triageJar), learn_merchant_rule: true }),
          });
          toast("Categorized & remembered for next time!");
          await refreshAll();
        } catch (err) {
          toast(err.message, true);
        }
      });
      return;
    }

    if (btn.dataset.toggleZombie) {
      await withBusy(btn, async () => {
        try {
          await api(`/api/bills/${btn.dataset.toggleZombie}/zombie`, {
            method: "PATCH",
            body: JSON.stringify({ flagged: btn.dataset.zombieNext === "true" }),
          });
          toast("Subscription review flag updated.");
          await refreshAll();
        } catch (err) {
          toast(err.message, true);
        }
      });
      return;
    }

    if (btn.dataset.archiveBill) {
      await withBusy(btn, async () => {
        try {
          await api(`/api/bills/${btn.dataset.archiveBill}`, { method: "DELETE" });
          toast("Archived bill & freed up Safe-to-Spend!");
          await refreshAll();
        } catch (err) {
          toast(err.message, true);
        }
      });
      return;
    }

    if (btn.dataset.clAutopay) {
      await withBusy(btn, async () => {
        try {
          await api(`/api/credit/lines/${btn.dataset.clAutopay}`, {
            method: "PATCH",
            body: JSON.stringify({ autopay_mode: btn.dataset.autopayVal || "minimum" }),
          });
          toast("Autopay shield updated!");
          await refreshAll();
          await loadCreditReport();
        } catch (err) {
          toast(err.message, true);
        }
      });
      return;
    }

    if (btn.dataset.clPaydown) {
      await withBusy(btn, async () => {
        try {
          const res = await api("/api/ai/suggestions/apply", {
            method: "POST",
            body: JSON.stringify({
              action_type: "credit_paydown",
              action_payload: {
                credit_line_id: Number(btn.dataset.clPaydown),
                amount_cents: Number(btn.dataset.paydownAmt),
              },
            }),
          });
          triggerCelebrate(null);
          toast(res.message);
          await refreshAll();
          await loadCreditReport();
        } catch (err) {
          toast(err.message, true);
        }
      });
      return;
    }

    if (btn.dataset.clDelete) {
      await withBusy(btn, async () => {
        try {
          await api(`/api/credit/lines/${btn.dataset.clDelete}`, { method: "DELETE" });
          toast("Removed credit line.");
          await refreshAll();
          await loadCreditReport();
        } catch (err) {
          toast(err.message, true);
        }
      });
      return;
    }

    if (btn.dataset.savePlanOption !== undefined && state.lastGoalPlan) {
      const optIdx = Number(btn.dataset.savePlanOption);
      const opt = (state.lastGoalPlan.options || [])[optIdx];
      await withBusy(btn, async () => {
        try {
          const payload = {
            name: String(state.lastGoalPlan.item_name || "Saved Goal").slice(0, 100),
            target_cents: state.lastGoalPlan.target_cents,
            saved_cents: state.lastGoalPlan.already_saved_cents,
            category: "gadget",
          };
          if (opt && opt.projected_ready_date && /^\d{4}-\d{2}-\d{2}$/.test(opt.projected_ready_date)) {
            payload.target_date = opt.projected_ready_date;
          }
          if (opt && opt.title) {
            payload.notes = `${opt.title}: ${opt.summary || ""}`.trim().slice(0, 238);
          }
          await api("/api/goals", { method: "POST", body: JSON.stringify(payload) });
          triggerCelebrate(btn.closest(".jar-card"));
          toast(`Saved '${state.lastGoalPlan.item_name}' (${opt ? opt.title : "Plan"}) as a tracked goal!`);
          await refreshAll();
        } catch (err) {
          toast(err.message, true);
        }
      });
      return;
    }

    if (btn.dataset.savePlannedGoal && state.lastGoalPlan) {
      await withBusy(btn, async () => {
        try {
          await api("/api/goals", {
            method: "POST",
            body: JSON.stringify({
              name: state.lastGoalPlan.item_name,
              target_cents: state.lastGoalPlan.target_cents,
              saved_cents: state.lastGoalPlan.already_saved_cents,
              category: "gadget",
            }),
          });
          triggerCelebrate(null);
          toast(`Saved '${state.lastGoalPlan.item_name}' to your tracked goals!`);
          await refreshAll();
        } catch (err) {
          toast(err.message, true);
        }
      });
      return;
    }

    if (btn.dataset.goalContrib) {
      await withBusy(btn, async () => {
        try {
          await api(`/api/goals/${btn.dataset.goalContrib}/contribute`, {
            method: "POST",
            body: JSON.stringify({ amount_cents: Number(btn.dataset.contribAmt || 5000) }),
          });
          triggerCelebrate(btn.closest(".jar-card") || btn, true);
          toast("Goal boosted! +15 XP!");
          await refreshAll();
        } catch (err) {
          toast(err.message, true);
        }
      });
      return;
    }

    if (btn.dataset.goalPlanName) {
      $("gp-name").value = btn.dataset.goalPlanName;
      $("gp-price").value = (Number(btn.dataset.goalPlanTarget) / 100).toFixed(2);
      $("gp-saved").value = (Number(btn.dataset.goalPlanSaved) / 100).toFixed(2);
      $("goal-plan-form").requestSubmit();
      return;
    }

    if (btn.dataset.goalDelete) {
      await withBusy(btn, async () => {
        try {
          await api(`/api/goals/${btn.dataset.goalDelete}`, { method: "DELETE" });
          toast("Goal removed.");
          await refreshAll();
        } catch (err) {
          toast(err.message, true);
        }
      });
      return;
    }

    if (btn.dataset.deleteTx) {
      await withBusy(btn, async () => {
        try {
          await api(`/api/transactions/${btn.dataset.deleteTx}`, { method: "DELETE" });
          toast("Transaction deleted & budget recalculated.");
          await refreshAll();
        } catch (err) {
          toast(err.message, true);
        }
      });
    }
  });

  document.body.addEventListener("change", async (e) => {
    const sel = e.target.closest("select[data-tx-jar-select]");
    if (!sel || !sel.value) return;
    try {
      await api(`/api/transactions/${sel.dataset.txJarSelect}`, {
        method: "PATCH",
        body: JSON.stringify({ jar_id: Number(sel.value), learn_merchant_rule: true }),
      });
      toast("Jar updated & merchant rule learned automatically!");
      await refreshAll();
    } catch (err) {
      toast(err.message, true);
    }
  });
});
