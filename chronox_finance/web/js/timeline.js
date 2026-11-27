/**
 * ChronoX Finance — Interactive Draggable Cashflow, Net Worth & Scenario Timeline (ES Module)
 * Pure zero-CDN SVG + Pointer Events direct manipulation:
 * - Zoom controls (30D | 90D | 1Y | 2Y) & Cashflow Runway / Total Net Worth toggle
 * - Non-destructive named Forecast Scenarios comparing Live Baseline (dashed steel) vs Scenario Plan
 * - Crimson Safe-Runway Floor Band & live delta/nadir/ending KPIs
 * - Draggable diamond/chevron Goal Markers bending the projected curve in real time using
 *   server-provided `per_goal_series` (zero duplicated backend math!), committing via debounced
 *   `POST /api/v1/analytics/scenario-sim` and optional `PATCH /api/v1/goals/{id}` with 8s Undo toast.
 * - Marginal-ROI Surplus Allocation breakdown cards.
 */

import {
  $,
  el,
  svgEl,
  makeIcon,
  renderEmptyState,
  switchTab,
  api,
  state,
  toast,
  withBusy,
  fmtMoney,
  fmtMoneyCompact,
  fmtShortDate,
  fmtDayDate,
  dollarsToCents,
  isoDateOffset,
  refreshAll,
  registerTabHook,
  registerRefreshHook,
  registerDashboardHook,
  BUCKET_LABELS,
} from "./core.js";

const ZOOM_DAYS = {
  "30d": 30,
  "90d": 90,
  "1y": 365,
  "2y": 730,
};

const tlState = {
  mode: "cashflow", // "cashflow" | "net_worth"
  zoom: "90d",
  reqSeq: 0,
  scenarios: [],
  activeScenarioId: null,
  adHocOverrides: {
    goal_date_shifts: {},
    monthly_income_delta_cents: 0,
    monthly_expense_delta_cents: 0,
    one_off_events: [],
  },
  simResult: null,
  netWorthSeries: [],
  quantForecast: null,
  scrubIdx: null,
  drag: null,
  commitTimer: null,
  pendingShiftOrigins: {},
};

function parseIsoDay(iso) {
  if (!iso) return null;
  const parts = String(iso).slice(0, 10).split("-").map(Number);
  if (parts.length !== 3 || parts.some((n) => !Number.isFinite(n))) return null;
  return new Date(parts[0], parts[1] - 1, parts[2]);
}

function toIsoDay(dt) {
  const y = dt.getFullYear();
  const m = String(dt.getMonth() + 1).padStart(2, "0");
  const d = String(dt.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

function daysBetween(isoA, isoB) {
  const a = parseIsoDay(isoA);
  const b = parseIsoDay(isoB);
  if (!a || !b) return 0;
  return Math.round((b.getTime() - a.getTime()) / 86400000);
}

function addDaysIso(iso, deltaDays) {
  const dt = parseIsoDay(iso) || new Date();
  dt.setDate(dt.getDate() + Math.round(deltaDays));
  return toIsoDay(dt);
}

function getActiveScenario() {
  if (tlState.activeScenarioId == null) return null;
  return tlState.scenarios.find((s) => Number(s.id) === Number(tlState.activeScenarioId)) || null;
}

function getMergedOverrides() {
  const scen = getActiveScenario();
  const baseOv = (scen && scen.overrides) || {};
  return {
    goal_date_shifts: Object.assign(
      {},
      baseOv.goal_date_shifts || {},
      tlState.adHocOverrides.goal_date_shifts || {}
    ),
    monthly_income_delta_cents:
      Number(baseOv.monthly_income_delta_cents || 0) +
      Number(tlState.adHocOverrides.monthly_income_delta_cents || 0),
    monthly_expense_delta_cents:
      Number(baseOv.monthly_expense_delta_cents || 0) +
      Number(tlState.adHocOverrides.monthly_expense_delta_cents || 0),
    one_off_events: [
      ...(Array.isArray(baseOv.one_off_events) ? baseOv.one_off_events : []),
      ...(Array.isArray(tlState.adHocOverrides.one_off_events) ? tlState.adHocOverrides.one_off_events : []),
    ],
  };
}

function syncModeAndZoomUI() {
  const modeSw = $("timeline-mode-switcher");
  if (modeSw) {
    modeSw.querySelectorAll("[data-timeline-mode]").forEach((btn) => {
      const active = btn.dataset.timelineMode === tlState.mode;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-pressed", String(active));
    });
  }
  const zoomSw = $("timeline-zoom-switcher");
  if (zoomSw) {
    zoomSw.querySelectorAll("[data-timeline-zoom]").forEach((btn) => {
      const active = btn.dataset.timelineZoom === tlState.zoom;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-pressed", String(active));
    });
  }
  const delBtn = $("btn-timeline-delete-scenario");
  if (delBtn) {
    delBtn.classList.toggle("hidden", tlState.activeScenarioId == null);
  }
}

function populateScenarioSelect() {
  const sel = $("timeline-scenario-select");
  if (!sel) return;
  const opts = [el("option", { value: "", text: "Live Baseline (No Overrides)" })];
  for (const sc of tlState.scenarios) {
    opts.push(
      el("option", {
        value: String(sc.id),
        text: sc.name,
        selected: Number(sc.id) === Number(tlState.activeScenarioId),
      })
    );
  }
  sel.replaceChildren(...opts);
  sel.value = tlState.activeScenarioId != null ? String(tlState.activeScenarioId) : "";
  syncModeAndZoomUI();
}

export async function loadScenarios() {
  if (state.vaultStatus !== "unlocked") return;
  try {
    const list = await api("/api/v1/scenarios");
    tlState.scenarios = Array.isArray(list) ? list : [];
    if (
      tlState.activeScenarioId != null &&
      !tlState.scenarios.some((s) => Number(s.id) === Number(tlState.activeScenarioId))
    ) {
      tlState.activeScenarioId = null;
    }
    populateScenarioSelect();
  } catch (_err) {
    // Keep existing scenarios list if unavailable
  }
}

export async function loadTimelineData() {
  if (state.vaultStatus !== "unlocked") return;
  syncModeAndZoomUI();
  const reqSeq = ++tlState.reqSeq;
  const days = ZOOM_DAYS[tlState.zoom] || 90;

  if (tlState.mode === "net_worth") {
    try {
      const res = await api(`/api/v1/analytics/net-worth?days=${days}`);
      if (reqSeq !== tlState.reqSeq) return;
      const pts = Array.isArray(res)
        ? res
        : Array.isArray(res && res.points)
        ? res.points
        : Array.isArray(res && res.series)
        ? res.series
        : Array.isArray(res && res.net_worth_series)
        ? res.net_worth_series
        : [];
      tlState.netWorthSeries = pts;
      renderNetWorthTimeline(pts);
    } catch (err) {
      if (reqSeq !== tlState.reqSeq) return;
      const fallback = (state.analytics && state.analytics.net_worth_series) || [];
      if (fallback.length) {
        tlState.netWorthSeries = fallback;
        renderNetWorthTimeline(fallback);
      } else {
        renderTimelineEmpty(err && err.message ? err.message : "Could not load net worth history.");
      }
    }
    return;
  }

  // Cashflow scenario projection + Marginal-ROI forecast
  try {
    const payload = {
      horizon_days: days,
      scenario_id: tlState.activeScenarioId != null ? Number(tlState.activeScenarioId) : null,
      overrides: getMergedOverrides(),
    };
    const [simRes, quantRes] = await Promise.all([
      api("/api/v1/analytics/scenario-sim", {
        method: "POST",
        body: JSON.stringify(payload),
      }),
      api(`/api/v1/quant/forecast?days=${Math.min(60, Math.max(30, days))}`).catch(() => null),
    ]);
    if (reqSeq !== tlState.reqSeq) return;
    tlState.simResult = simRes;
    if (quantRes) {
      tlState.quantForecast = quantRes;
      renderOptimalAllocations(
        quantRes.optimal_allocations || [],
        quantRes.total_expected_annual_benefit_cents || 0
      );
    } else if (state.dashboard && state.dashboard.quant_forecast) {
      renderOptimalAllocations(
        state.dashboard.quant_forecast.optimal_allocations || [],
        state.dashboard.quant_forecast.total_expected_annual_benefit_cents || 0
      );
    }
    renderCashflowTimeline(simRes);
  } catch (err) {
    if (reqSeq !== tlState.reqSeq) return;
    renderTimelineEmpty(err && err.message ? err.message : "Could not project cashflow timeline.");
  }
}

function truncateWordBoundary(raw, maxLen = 18) {
  const s = String(raw || "").trim();
  if (s.length <= maxLen) return s;
  const cut = s.slice(0, Math.max(1, maxLen - 1));
  const lastSpace = cut.lastIndexOf(" ");
  if (lastSpace >= Math.floor(maxLen * 0.45)) {
    return `${cut.slice(0, lastSpace)}...`;
  }
  return `${cut}...`;
}

function renderTimelineEmpty(msg) {
  const wrap = $("timeline-svg-wrap");
  if (wrap) {
    wrap.replaceChildren(
      renderEmptyState({
        icon: "activity",
        title: "No balance forecast yet",
        subtitle: String(msg || "Connect a bank account or import transactions to project your upcoming balance."),
        actionLabel: "Connect Bank",
        onAction: () => switchTab("settings"),
        secondaryLabel: "Import Statement",
        onSecondary: () => switchTab("activity"),
        compact: true,
      })
    );
  }
}

/* ============================================================================
   Client-Side Real-Time Curve Re-Projection from Server `per_goal_series`
   (Zero duplicated payday/bill engine math!)
   ============================================================================ */

function lookupGoalCumulative(goalSeries, refDateIso, queryDateIso) {
  const pts = Array.isArray(goalSeries.points) ? goalSeries.points : [];
  const remaining = Math.max(0, Number(goalSeries.target_cents || 0) - Number(goalSeries.saved_cents || 0));
  if (remaining <= 0) return 0;
  if (!pts.length) {
    const td = goalSeries.target_date;
    if (!td) return 0;
    const span = Math.max(1, daysBetween(refDateIso, td));
    const elapsed = Math.max(0, Math.min(span, daysBetween(refDateIso, queryDateIso)));
    return Math.round((remaining * elapsed) / span);
  }
  let cum = 0;
  for (let i = 0; i < pts.length; i++) {
    if (pts[i].date <= queryDateIso) {
      cum = Number(pts[i].cumulative_cents || 0);
    } else {
      break;
    }
  }
  const lastPt = pts[pts.length - 1];
  if (queryDateIso > lastPt.date && cum < remaining && goalSeries.target_date && goalSeries.target_date > lastPt.date) {
    const tailSpan = Math.max(1, daysBetween(lastPt.date, goalSeries.target_date));
    const tailElapsed = Math.max(0, Math.min(tailSpan, daysBetween(lastPt.date, queryDateIso)));
    cum += Math.round(((remaining - cum) * tailElapsed) / tailSpan);
  }
  return Math.min(remaining, cum);
}

function computeLiveShiftedBalances(sim, draggedGoalId, newTargetIso) {
  const scenPts = sim.scenario_points || [];
  if (!scenPts.length || draggedGoalId == null || !newTargetIso) {
    return scenPts.map((p) => Number(p.balance_cents || 0));
  }
  const gs = (sim.per_goal_series || []).find((g) => Number(g.goal_id) === Number(draggedGoalId));
  if (!gs) return scenPts.map((p) => Number(p.balance_cents || 0));

  const refDate = scenPts[0].date;
  const origTarget = gs.target_date || addDaysIso(refDate, 180);
  const origSpan = Math.max(1, daysBetween(refDate, origTarget));
  const newSpan = Math.max(1, daysBetween(refDate, newTargetIso));
  const remaining = Math.max(0, Number(gs.target_cents || 0) - Number(gs.saved_cents || 0));

  return scenPts.map((p) => {
    const origCum = lookupGoalCumulative(gs, refDate, p.date);
    let shiftedCum = 0;
    if (p.date >= newTargetIso) {
      shiftedCum = remaining;
    } else {
      const elapsed = Math.max(0, daysBetween(refDate, p.date));
      const mappedDays = Math.round((elapsed * origSpan) / newSpan);
      const mappedIso = addDaysIso(refDate, mappedDays);
      shiftedCum = lookupGoalCumulative(gs, refDate, mappedIso);
    }
    // Lower cumulative contribution by date d frees cash (+delta); faster contribution reduces cash (-delta)
    return Number(p.balance_cents || 0) + (origCum - shiftedCum);
  });
}

function updateTimelineKpis(basePts, liveBalances, floorCents, isNetWorth = false, nwPts = null) {
  const deltaEl = $("timeline-delta-readout");
  const deltaPctEl = $("timeline-delta-pct");
  const nadirEl = $("timeline-nadir-readout");
  const nadirDateEl = $("timeline-nadir-date");
  const floorEl = $("timeline-floor-readout");
  const endEl = $("timeline-ending-readout");
  const endMetaEl = $("timeline-ending-meta");

  if (isNetWorth && Array.isArray(nwPts) && nwPts.length) {
    const first = nwPts[0];
    const last = nwPts[nwPts.length - 1];
    const diff = Number(last.net_worth_cents || 0) - Number(first.net_worth_cents || 0);
    const pct = first.net_worth_cents ? (diff / Math.abs(first.net_worth_cents)) * 100 : 0;
    let minPt = nwPts[0];
    for (const p of nwPts) {
      if (Number(p.net_worth_cents) < Number(minPt.net_worth_cents)) minPt = p;
    }
    if (deltaEl) {
      deltaEl.textContent = `${diff >= 0 ? "+" : ""}${fmtMoney(diff)}`;
      deltaEl.style.color = diff >= 0 ? "var(--primary)" : "var(--crimson)";
    }
    if (deltaPctEl) deltaPctEl.textContent = `${pct >= 0 ? "+" : ""}${pct.toFixed(1)}% over window`;
    if (nadirEl) {
      nadirEl.textContent = fmtMoney(minPt.net_worth_cents);
      nadirEl.style.color = Number(minPt.net_worth_cents) < 0 ? "var(--crimson)" : "var(--text)";
    }
    if (nadirDateEl) nadirDateEl.textContent = fmtShortDate(minPt.date);
    if (floorEl) floorEl.textContent = fmtMoney(last.liquid_cents);
    if (endEl) {
      endEl.textContent = fmtMoney(last.net_worth_cents);
      endEl.style.color = "var(--cyan)";
    }
    if (endMetaEl) {
      endMetaEl.textContent = `Inv ${fmtMoneyCompact(last.investment_cents)} · Debt ${fmtMoneyCompact(last.debt_cents)}`;
    }
    return;
  }

  if (!basePts.length || !liveBalances.length) return;
  const baseEnd = Number(basePts[basePts.length - 1].balance_cents || 0);
  const scenEnd = Number(liveBalances[liveBalances.length - 1] || 0);
  const diff = scenEnd - baseEnd;
  const pct = baseEnd !== 0 ? (diff / Math.abs(baseEnd)) * 100 : 0;

  let nadirVal = liveBalances[0];
  let nadirIdx = 0;
  for (let i = 1; i < liveBalances.length; i++) {
    if (liveBalances[i] < nadirVal) {
      nadirVal = liveBalances[i];
      nadirIdx = i;
    }
  }

  if (deltaEl) {
    deltaEl.textContent = `${diff >= 0 ? "+" : ""}${fmtMoney(diff)}`;
    deltaEl.style.color = diff > 0 ? "var(--primary)" : diff < 0 ? "var(--crimson)" : "var(--text)";
  }
  if (deltaPctEl) {
    const scen = getActiveScenario();
    const tag = scen ? scen.name : "Live Baseline";
    deltaPctEl.textContent = `${pct >= 0 ? "+" : ""}${pct.toFixed(1)}% · ${tag}`;
  }
  if (nadirEl) {
    nadirEl.textContent = fmtMoney(nadirVal);
    nadirEl.style.color =
      nadirVal < 0 ? "var(--crimson)" : nadirVal < floorCents ? "var(--amber)" : "var(--primary)";
  }
  if (nadirDateEl) {
    nadirDateEl.textContent = `${fmtShortDate(basePts[nadirIdx].date)}${nadirVal < floorCents ? " (Below Floor)" : ""}`;
  }
  if (floorEl) {
    floorEl.textContent = fmtMoney(floorCents);
    floorEl.style.color = "var(--crimson)";
  }
  if (endEl) {
    endEl.textContent = fmtMoney(scenEnd);
    endEl.style.color = scenEnd < floorCents ? "var(--amber)" : "var(--primary)";
  }
  if (endMetaEl) {
    endMetaEl.textContent = `Horizon end ${fmtShortDate(basePts[basePts.length - 1].date)}`;
  }
}

/* ============================================================================
   Cashflow Runway & Draggable Goal Marker SVG Renderer
   ============================================================================ */

function renderCashflowTimeline(sim) {
  const wrap = $("timeline-svg-wrap");
  const scrubBar = $("timeline-scrub-bar");
  if (!wrap || !sim) return;

  const basePts = Array.isArray(sim.baseline_points) ? sim.baseline_points : [];
  const scenPts = Array.isArray(sim.scenario_points) && sim.scenario_points.length ? sim.scenario_points : basePts;
  if (!basePts.length) {
    renderTimelineEmpty("Add accounts, bills, or goals to project your forward cashflow timeline.");
    return;
  }

  const floorCents = Math.max(0, Number(sim.safe_runway_floor_cents || 0));
  const goals = Array.isArray(sim.per_goal_series) ? sim.per_goal_series : [];

  let liveBalances = scenPts.map((p) => Number(p.balance_cents || 0));
  updateTimelineKpis(basePts, liveBalances, floorCents);

  const W = 1040;
  const H = 352;
  const padL = 64;
  const padR = 24;
  const padT = 26;
  const padB = 82; // Dedicated bottom rail + two staggered goal label rows clear of X-axis ticks
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;

  const allVals = [
    ...basePts.map((p) => Number(p.balance_cents || 0)),
    ...liveBalances,
    floorCents,
    0,
  ];
  let minVal = Math.min(...allVals);
  let maxVal = Math.max(...allVals);
  if (!Number.isFinite(minVal)) minVal = 0;
  if (!Number.isFinite(maxVal) || maxVal <= minVal) maxVal = minVal + 50000;
  const span = maxVal - minVal;
  const yMin = minVal < 0 ? minVal - span * 0.1 : Math.max(0, minVal - span * 0.08);
  const yMax = maxVal + span * 0.12;

  const startIso = basePts[0].date;
  const endIso = basePts[basePts.length - 1].date;
  const totalDays = Math.max(1, basePts.length - 1);

  const xForIdx = (i) => padL + (basePts.length === 1 ? plotW / 2 : (i / totalDays) * plotW);
  const xForDate = (iso) => {
    const d = daysBetween(startIso, iso);
    const clamped = Math.max(0, Math.min(totalDays, d));
    return padL + (clamped / totalDays) * plotW;
  };
  const yFor = (val) => padT + (1 - (val - yMin) / Math.max(1, yMax - yMin)) * plotH;
  const baselineY = padT + plotH;

  const buildPath = (vals) => {
    if (!vals.length) return "";
    const coords = vals.map((v, i) => [xForIdx(i), yFor(v)]);
    if (coords.length <= 2) {
      return coords.map((c, i) => `${i === 0 ? "M" : "L"}${c[0].toFixed(1)},${c[1].toFixed(1)}`).join(" ");
    }
    const n = coords.length;
    const dx = new Array(n - 1);
    const slope = new Array(n - 1);
    for (let i = 0; i < n - 1; i++) {
      dx[i] = Math.max(0.0001, coords[i + 1][0] - coords[i][0]);
      slope[i] = (coords[i + 1][1] - coords[i][1]) / dx[i];
    }
    const tangents = new Array(n);
    tangents[0] = slope[0];
    tangents[n - 1] = slope[n - 2];
    for (let i = 1; i < n - 1; i++) {
      if (slope[i - 1] * slope[i] <= 0) {
        tangents[i] = 0;
      } else {
        const common = dx[i - 1] + dx[i];
        tangents[i] = (3 * common) / ((common + dx[i]) / slope[i - 1] + (common + dx[i - 1]) / slope[i]);
      }
    }
    let d = `M${coords[0][0].toFixed(1)},${coords[0][1].toFixed(1)}`;
    for (let i = 0; i < n - 1; i++) {
      const h = dx[i] / 3;
      const cp1x = coords[i][0] + h;
      const cp1y = coords[i][1] + tangents[i] * h;
      const cp2x = coords[i + 1][0] - h;
      const cp2y = coords[i + 1][1] - tangents[i + 1] * h;
      d += ` C${cp1x.toFixed(1)},${cp1y.toFixed(1)} ${cp2x.toFixed(1)},${cp2y.toFixed(1)} ${coords[i + 1][0].toFixed(1)},${coords[i + 1][1].toFixed(1)}`;
    }
    return d;
  };

  const baseVals = basePts.map((p) => Number(p.balance_cents || 0));
  const basePathD = buildPath(baseVals);
  const scenPathD = buildPath(liveBalances);
  const scenAreaD = `${scenPathD} L${xForIdx(liveBalances.length - 1).toFixed(1)},${baselineY.toFixed(1)} L${xForIdx(0).toFixed(1)},${baselineY.toFixed(1)} Z`;

  const liveNadir = Math.min(...liveBalances);
  const scenStrokeColor =
    liveNadir < 0
      ? "var(--chart-def-stroke)"
      : liveNadir < floorCents
      ? "var(--chart-exp-stroke)"
      : "var(--chart-inc-stroke)";

  const defs = svgEl("defs", {}, [
    svgEl("linearGradient", { id: "tlAreaGrad", x1: "0", y1: "0", x2: "0", y2: "1" }, [
      svgEl("stop", { offset: "0%", "stop-color": "var(--chart-inc-stroke)", "stop-opacity": "0.26" }),
      svgEl("stop", { offset: "100%", "stop-color": "var(--chart-inc-stroke)", "stop-opacity": "0.01" }),
    ]),
    svgEl("linearGradient", { id: "tlFloorGrad", x1: "0", y1: "0", x2: "0", y2: "1" }, [
      svgEl("stop", { offset: "0%", "stop-color": "var(--chart-def-stroke)", "stop-opacity": "0.18" }),
      svgEl("stop", { offset: "100%", "stop-color": "var(--chart-def-stroke)", "stop-opacity": "0.05" }),
    ]),
  ]);

  // Gridlines & Axes
  const gridNodes = [];
  for (let t = 0; t <= 4; t++) {
    const val = yMin + ((yMax - yMin) * t) / 4;
    const y = yFor(val);
    gridNodes.push(
      svgEl("line", {
        x1: padL,
        y1: y.toFixed(1),
        x2: W - padR,
        y2: y.toFixed(1),
        stroke: "var(--chart-grid)",
        "stroke-dasharray": t === 0 ? "none" : "3 3",
        "stroke-width": "1",
      })
    );
    gridNodes.push(
      svgEl("text", {
        x: padL - 8,
        y: (y + 3.5).toFixed(1),
        fill: "var(--muted)",
        "font-family": "var(--font-mono)",
        "font-size": "10",
        "text-anchor": "end",
        text: fmtMoneyCompact(val),
      })
    );
  }

  // X-axis date ticks
  const tickCount = Math.min(7, basePts.length);
  for (let k = 0; k < tickCount; k++) {
    const idx = tickCount === 1 ? 0 : Math.round((k * (basePts.length - 1)) / (tickCount - 1));
    const anchor = k === 0 ? "start" : k === tickCount - 1 ? "end" : "middle";
    gridNodes.push(
      svgEl("text", {
        x: xForIdx(idx).toFixed(1),
        y: (baselineY + 16).toFixed(1),
        fill: "var(--muted)",
        "font-family": "var(--font-mono)",
        "font-size": "10",
        "text-anchor": anchor,
        text: fmtShortDate(basePts[idx].date),
      })
    );
  }

  // Crimson Safe-Runway Floor Band
  const floorNodes = [];
  const floorY = Math.max(padT, Math.min(baselineY, yFor(floorCents)));
  if (floorCents > 0 && floorY < baselineY) {
    floorNodes.push(
      svgEl("rect", {
        x: padL,
        y: floorY.toFixed(1),
        width: plotW,
        height: Math.max(0, baselineY - floorY).toFixed(1),
        fill: "url(#tlFloorGrad)",
      })
    );
    floorNodes.push(
      svgEl("line", {
        x1: padL,
        y1: floorY.toFixed(1),
        x2: W - padR,
        y2: floorY.toFixed(1),
        stroke: "var(--chart-def-stroke)",
        "stroke-width": "1.2",
        "stroke-dasharray": "5 3",
      })
    );
    floorNodes.push(
      svgEl("text", {
        x: W - padR - 4,
        y: (floorY - 5).toFixed(1),
        fill: "var(--chart-def-text)",
        "font-family": "var(--font-mono)",
        "font-size": "9.5",
        "font-weight": "700",
        "text-anchor": "end",
        text: `SAFETY CUSHION ${fmtMoneyCompact(floorCents)}`,
      })
    );
  }

  // Baseline (dashed steel) vs Scenario Plan (solid emerald/cyan/amber/crimson)
  const scenAreaPath = svgEl("path", { d: scenAreaD, fill: "url(#tlAreaGrad)" });
  const baseLinePath = svgEl("path", {
    d: basePathD,
    fill: "none",
    stroke: "#64748b",
    "stroke-width": "1.7",
    "stroke-linecap": "round",
    "stroke-linejoin": "round",
    "stroke-dasharray": "6 4",
  });
  const scenLinePath = svgEl("path", {
    d: scenPathD,
    fill: "none",
    stroke: scenStrokeColor,
    "stroke-width": "2.6",
    "stroke-linecap": "round",
    "stroke-linejoin": "round",
  });

  // Clickable Event Markers on days with scheduled bills, paydays, or debt payments
  const dayBreakdownBox = $("timeline-day-breakdown");
  const renderDayBreakdown = (idx) => {
    if (!dayBreakdownBox) return;
    const safeIdx = Math.max(0, Math.min(basePts.length - 1, idx));
    const p = scenPts[safeIdx] || basePts[safeIdx];
    const val = liveBalances[safeIdx];
    const baseVal = baseVals[safeIdx];
    const cushionMargin = val - floorCents;
    const evs = Array.isArray(p.events) ? p.events : [];

    const closeBtn = el("button", { type: "button", class: "btn btn-ghost btn-xs", text: "Close" });
    closeBtn.addEventListener("click", () => {
      tlState.pinnedDayIdx = null;
      dayBreakdownBox.classList.add("hidden");
      dayBreakdownBox.replaceChildren();
    });

    const eventRows = evs.length
      ? evs.map((evText) => {
          const isInflow = /payday|income|deposit|\+/i.test(String(evText));
          const isBill = /bill|debt|credit|autopay|minimum/i.test(String(evText));
          const badgeClass = isInflow ? "pill pill-calm" : isBill ? "pill pill-watch" : "pill pill-soft";
          const badgeText = isInflow ? "Inflow" : isBill ? "Scheduled" : "Event";
          return el("div", { class: "timeline-event-row" }, [
            el("span", { class: "row-gap", style: { gap: "8px" } }, [
              el("span", { class: badgeClass, text: badgeText }),
              el("span", { text: String(evText) }),
            ]),
          ]);
        })
      : [
          el("p", {
            class: "muted small",
            text: "No scheduled bills or paydays on this date — only steady daily living pace.",
          }),
        ];

    dayBreakdownBox.classList.remove("hidden");
    dayBreakdownBox.replaceChildren(
      el("div", { class: "item-row wrap", style: { gap: "8px" } }, [
        el("div", { class: "row-gap wrap", style: { gap: "8px" } }, [
          el("strong", { text: fmtDayDate(p.date) }),
          el("span", {
            class: `pill ${val < floorCents ? "pill-watch" : "pill-calm"}`,
            text: `Projected: ${fmtMoney(val)}`,
          }),
          el("span", {
            class: "pill pill-soft",
            text: `Baseline: ${fmtMoney(baseVal)}`,
          }),
          floorCents > 0
            ? el("span", {
                class: `pill ${cushionMargin >= 0 ? "pill-soft" : "pill-danger"}`,
                text: `${cushionMargin >= 0 ? "+" : ""}${fmtMoney(cushionMargin)} vs Cushion`,
              })
            : null,
        ]),
        closeBtn,
      ]),
      el("div", { class: "timeline-day-events" }, eventRows)
    );
  };

  const eventDotGroup = svgEl("g");
  scenPts.forEach((pt, idx) => {
    const evs = Array.isArray(pt.events) ? pt.events : [];
    if (!evs.length) return;
    const hasInflow = evs.some((s) => /payday|income|deposit|\+/i.test(String(s)));
    const dotColor = hasInflow ? "var(--chart-inc-stroke)" : "var(--chart-exp-stroke)";
    const dot = svgEl(
      "circle",
      {
        cx: xForIdx(idx).toFixed(1),
        cy: yFor(liveBalances[idx]).toFixed(1),
        r: "4.2",
        fill: dotColor,
        stroke: "var(--bg)",
        "stroke-width": "1.4",
        tabindex: "0",
        role: "button",
        "aria-label": `${fmtDayDate(pt.date)}: ${evs.join("; ")} — click for breakdown`,
        style: "cursor: pointer;",
      },
      [svgEl("title", { text: `${fmtDayDate(pt.date)} · ${evs.join(" · ")} (Click for breakdown)` })]
    );
    dot.addEventListener("click", (ev) => {
      ev.stopPropagation();
      tlState.pinnedDayIdx = idx;
      updateScrub(idx);
      renderDayBreakdown(idx);
    });
    dot.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        tlState.pinnedDayIdx = idx;
        updateScrub(idx);
        renderDayBreakdown(idx);
      }
    });
    eventDotGroup.appendChild(dot);
  });

  // Crosshair Scrubber
  const initIdx = Math.min(basePts.length - 1, Math.floor(basePts.length / 3));
  const crosshair = svgEl("line", {
    x1: xForIdx(initIdx).toFixed(1),
    y1: padT,
    x2: xForIdx(initIdx).toFixed(1),
    y2: baselineY,
    stroke: "var(--chart-crosshair)",
    "stroke-width": "1",
    "stroke-dasharray": "2 2",
  });
  const activeDot = svgEl("circle", {
    cx: xForIdx(initIdx).toFixed(1),
    cy: yFor(liveBalances[initIdx]).toFixed(1),
    r: "5",
    fill: scenStrokeColor,
    stroke: "var(--bg)",
    "stroke-width": "1.6",
  });

  const updateScrub = (idx, dragBanner = null) => {
    const safeIdx = Math.max(0, Math.min(basePts.length - 1, idx));
    tlState.scrubIdx = safeIdx;
    const x = xForIdx(safeIdx);
    const val = liveBalances[safeIdx];
    const baseVal = baseVals[safeIdx];
    const delta = val - baseVal;

    crosshair.setAttribute("x1", x.toFixed(1));
    crosshair.setAttribute("x2", x.toFixed(1));
    activeDot.setAttribute("cx", x.toFixed(1));
    activeDot.setAttribute("cy", yFor(val).toFixed(1));

    if (!scrubBar) return;
    if (dragBanner) {
      scrubBar.replaceChildren(
        el("span", { class: "inline-icon-row", style: { color: "var(--cyan)", fontWeight: "700" } }, [
          makeIcon("target", "ui-icon-sm"),
          dragBanner,
        ]),
        el("span", { class: "muted small", text: "Release pointer to commit authoritative server projection" })
      );
      return;
    }

    const p = scenPts[safeIdx] || basePts[safeIdx];
    const evs = Array.isArray(p.events) ? p.events.slice(0, 3) : [];
    scrubBar.replaceChildren(
      el("span", { class: "row-gap wrap", style: { gap: "10px" } }, [
        el("strong", { text: fmtDayDate(p.date) }),
        el("span", {
          style: { color: val < floorCents ? "var(--amber)" : "var(--primary)", fontWeight: "800" },
          text: `Plan: ${fmtMoney(val)}`,
        }),
        delta !== 0
          ? el("span", {
              class: `pill ${delta >= 0 ? "pill-calm" : "pill-danger"}`,
              text: `${delta >= 0 ? "+" : ""}${fmtMoney(delta)} vs Live`,
            })
          : el("span", { class: "muted small", text: `Baseline: ${fmtMoney(baseVal)}` }),
      ]),
      el(
        "span",
        { class: "row-gap wrap", style: { gap: "5px" } },
        evs.length
          ? evs.map((ev) => el("span", { class: "pill pill-soft", text: ev }))
          : [el("span", { class: "muted small", text: "Drag any cyan diamond marker below to shift goal date" })]
      )
    );
  };

  // Draggable Diamond / Chevron Goal Markers along X-axis rail
  const markerGroup = svgEl("g");
  const snapStepDays = tlState.zoom === "30d" ? 1 : tlState.zoom === "2y" ? 30 : 7;

  const commitGoalShift = (goalSeries, prevTargetIso, newTargetIso) => {
    const gKey = String(goalSeries.goal_id);
    if (!(gKey in tlState.pendingShiftOrigins)) {
      tlState.pendingShiftOrigins[gKey] = prevTargetIso;
    }
    if (tlState.commitTimer) clearTimeout(tlState.commitTimer);
    tlState.commitTimer = setTimeout(async () => {
      const originTargetIso = tlState.pendingShiftOrigins[gKey];
      delete tlState.pendingShiftOrigins[gKey];
      const scen = getActiveScenario();
      tlState.adHocOverrides.goal_date_shifts[gKey] = newTargetIso;

      if (!scen) {
        // Live baseline drag: persist target_date to live goal with 8s Undo toast + authoritative sim
        try {
          await api(`/api/v1/goals/${goalSeries.goal_id}`, {
            method: "PATCH",
            body: JSON.stringify({ target_date: newTargetIso }),
          });
          if (Array.isArray(state.dashboard && state.dashboard.goals)) {
            const gObj = state.dashboard.goals.find((g) => Number(g.id) === Number(goalSeries.goal_id));
            if (gObj) gObj.target_date = newTargetIso;
          }
          delete tlState.adHocOverrides.goal_date_shifts[gKey];
          toast(`Moved "${goalSeries.goal_name}" target date to ${newTargetIso}`, {
            durationMs: 8000,
            undoLabel: "Undo Shift",
            onUndo: async () => {
              await api(`/api/v1/goals/${goalSeries.goal_id}`, {
                method: "PATCH",
                body: JSON.stringify({ target_date: originTargetIso }),
              });
              if (Array.isArray(state.dashboard && state.dashboard.goals)) {
                const gObj = state.dashboard.goals.find((g) => Number(g.id) === Number(goalSeries.goal_id));
                if (gObj) gObj.target_date = originTargetIso;
              }
              toast(`Restored "${goalSeries.goal_name}" to ${originTargetIso || "flexible timeline"}`);
              await loadTimelineData();
              await refreshAll();
            },
          });
        } catch (err) {
          toast(err.message || "Could not update goal target date.", true);
        }
      } else {
        toast(`Scenario "${scen.name}": "${goalSeries.goal_name}" shifted to ${newTargetIso}`, {
          durationMs: 8000,
          undoLabel: "Reset Shift",
          onUndo: async () => {
            delete tlState.adHocOverrides.goal_date_shifts[gKey];
            await loadTimelineData();
          },
        });
      }
      await loadTimelineData();
    }, 180);
  };

  const railY = baselineY + 38;
  const goalLabelAnchor = (x) => {
    if (x > W - padR - 105) return "end";
    if (x < padL + 105) return "start";
    return "middle";
  };
  // Horizontal rail track for markers
  markerGroup.appendChild(
    svgEl("line", {
      x1: padL,
      y1: railY.toFixed(1),
      x2: W - padR,
      y2: railY.toFixed(1),
      stroke: "var(--border)",
      "stroke-width": "1",
    })
  );

  goals.forEach((gs, gIdx) => {
    const liveGoal = Array.isArray(state.dashboard && state.dashboard.goals)
      ? state.dashboard.goals.find((g) => Number(g.id) === Number(gs.goal_id))
      : null;
    const origServerTarget = liveGoal ? (liveGoal.target_date ?? null) : (gs.target_date ?? null);
    const origTarget = gs.target_date || addDaysIso(startIso, Math.min(totalDays, 90));
    const initialX = xForDate(origTarget);
    const labelY = gIdx % 2 === 1 ? railY + 33 : railY + 19;
    const guideLine = svgEl("line", {
      x1: initialX.toFixed(1),
      y1: padT,
      x2: initialX.toFixed(1),
      y2: railY.toFixed(1),
      stroke: "var(--chart-sur-stroke)",
      "stroke-width": "1",
      "stroke-dasharray": "3 3",
      opacity: "0.55",
    });

    const diamondPts = (cx, cy, r = 8) =>
      `${cx.toFixed(1)},${(cy - r).toFixed(1)} ${(cx + r).toFixed(1)},${cy.toFixed(1)} ${cx.toFixed(1)},${(cy + r).toFixed(1)} ${(cx - r).toFixed(1)},${cy.toFixed(1)}`;

    const hitCircle = svgEl("circle", {
      class: "timeline-marker-hit",
      cx: initialX.toFixed(1),
      cy: railY.toFixed(1),
      r: "28",
      fill: "transparent",
      "pointer-events": "all",
    });

    const diamond = svgEl("polygon", {
      points: diamondPts(initialX, railY, 8),
      fill: "var(--chart-sur-stroke)",
      stroke: "var(--bg)",
      "stroke-width": "1.6",
    });

    const shortName = truncateWordBoundary(gs.goal_name || `Goal ${gs.goal_id}`, 18);
    const lblText = svgEl("text", {
      x: initialX.toFixed(1),
      y: labelY.toFixed(1),
      fill: "var(--chart-sur-text)",
      "font-family": "var(--font-mono)",
      "font-size": "9.5",
      "font-weight": "700",
      "text-anchor": goalLabelAnchor(initialX),
      text: `${shortName} (${fmtShortDate(origTarget)})`,
    });

    const handleG = svgEl(
      "g",
      {
        class: "timeline-goal-marker",
        tabindex: "0",
        role: "slider",
        "aria-label": `Drag ${gs.goal_name} target date (currently ${origTarget}). Use Left/Right arrows to shift.`,
        "aria-valuetext": origTarget,
      },
      [guideLine, hitCircle, diamond, lblText]
    );

    let currentTargetIso = origTarget;

    const applyLiveDragDate = (nextIso, liveMode = true) => {
      currentTargetIso = nextIso;
      const nx = xForDate(nextIso);
      guideLine.setAttribute("x1", nx.toFixed(1));
      guideLine.setAttribute("x2", nx.toFixed(1));
      hitCircle.setAttribute("cx", nx.toFixed(1));
      diamond.setAttribute("points", diamondPts(nx, railY, liveMode ? 10 : 8));
      lblText.setAttribute("x", nx.toFixed(1));
      lblText.setAttribute("text-anchor", goalLabelAnchor(nx));
      lblText.textContent = `${shortName} (${fmtShortDate(nextIso)})`;
      handleG.setAttribute("aria-valuetext", nextIso);

      // Re-project curve in real time using server's per_goal_series!
      liveBalances = computeLiveShiftedBalances(sim, gs.goal_id, nextIso);
      const nextD = buildPath(liveBalances);
      const nextAreaD = `${nextD} L${xForIdx(liveBalances.length - 1).toFixed(1)},${baselineY.toFixed(1)} L${xForIdx(0).toFixed(1)},${baselineY.toFixed(1)} Z`;
      const nextNadir = Math.min(...liveBalances);
      const nextColor =
        nextNadir < 0
          ? "var(--chart-def-stroke)"
          : nextNadir < floorCents
          ? "var(--chart-exp-stroke)"
          : "var(--chart-sur-stroke)";
      scenLinePath.setAttribute("d", nextD);
      scenLinePath.setAttribute("stroke", nextColor);
      scenAreaPath.setAttribute("d", nextAreaD);
      updateTimelineKpis(basePts, liveBalances, floorCents);
      if (liveMode) {
        const diffDays = daysBetween(origTarget, nextIso);
        const dir = diffDays === 0 ? "unchanged" : diffDays > 0 ? `+${diffDays}d later` : `${diffDays}d earlier`;
        updateScrub(tlState.scrubIdx ?? initIdx, `Shifting "${gs.goal_name}" -> ${nextIso} (${dir})`);
      }
    };

    const onDragMove = (ev) => {
      if (!tlState.drag || tlState.drag.goalId !== gs.goal_id) return;
      const svgRect = svg.getBoundingClientRect();
      if (!svgRect.width) return;
      const relX = ((ev.clientX - svgRect.left) / svgRect.width) * W;
      const frac = Math.max(0.02, Math.min(1, (relX - padL) / Math.max(1, plotW)));
      const rawDays = Math.round(frac * totalDays);
      const snappedDays = Math.max(1, Math.min(totalDays, Math.round(rawDays / snapStepDays) * snapStepDays));
      const nextIso = addDaysIso(startIso, snappedDays);
      if (nextIso !== currentTargetIso) {
        applyLiveDragDate(nextIso, true);
      }
    };

    const endDrag = (ev) => {
      if (!tlState.drag || tlState.drag.goalId !== gs.goal_id) return;
      window.removeEventListener("pointermove", onDragMove);
      window.removeEventListener("pointerup", endDrag);
      window.removeEventListener("pointercancel", endDrag);
      const capEl = tlState.drag.captureEl || handleG;
      try {
        capEl.releasePointerCapture(ev.pointerId);
      } catch (_e) {
        // ignore
      }
      handleG.classList.remove("is-dragging");
      const prevDisplayTarget = tlState.drag.origTarget;
      const prevServerTarget = tlState.drag.origServerTarget;
      tlState.drag = null;
      diamond.setAttribute("points", diamondPts(xForDate(currentTargetIso), railY, 8));
      if (currentTargetIso !== prevDisplayTarget) {
        commitGoalShift(gs, prevServerTarget, currentTargetIso);
      } else {
        updateScrub(tlState.scrubIdx ?? initIdx);
      }
    };

    handleG.addEventListener("pointerdown", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const capEl = ev.target && typeof ev.target.setPointerCapture === "function" ? ev.target : handleG;
      try {
        capEl.setPointerCapture(ev.pointerId);
      } catch (_e) {
        // Synthetic pointer events without an active OS pointer id skip capture safely
      }
      handleG.classList.add("is-dragging");
      tlState.drag = {
        goalId: gs.goal_id,
        origTarget,
        origServerTarget,
        pointerId: ev.pointerId,
        captureEl: capEl,
      };
      window.addEventListener("pointermove", onDragMove);
      window.addEventListener("pointerup", endDrag);
      window.addEventListener("pointercancel", endDrag);
    });

    // Keyboard A11y: ArrowLeft / ArrowRight shifts target date by +/- 7d (or 30d with Shift)
    handleG.addEventListener("keydown", (ev) => {
      if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
      ev.preventDefault();
      const step = (ev.shiftKey ? 30 : 7) * (ev.key === "ArrowRight" ? 1 : -1);
      const prev = currentTargetIso;
      const nextDayOffset = Math.max(1, Math.min(totalDays, daysBetween(startIso, currentTargetIso) + step));
      const nextIso = addDaysIso(startIso, nextDayOffset);
      if (nextIso !== prev) {
        applyLiveDragDate(nextIso, true);
        commitGoalShift(gs, origServerTarget, nextIso);
      }
    });

    markerGroup.appendChild(handleG);
  });

  const svg = svgEl(
    "svg",
    {
      viewBox: `0 0 ${W} ${H}`,
      role: "img",
      style: "cursor: crosshair;",
      "aria-label": "Interactive cashflow and scenario forecast chart with clickable day breakdown and draggable goal markers",
    },
    [defs, ...gridNodes, ...floorNodes, scenAreaPath, baseLinePath, scenLinePath, eventDotGroup, crosshair, activeDot, markerGroup]
  );

  // Smooth continuous hover scrub (paused while dragging a goal handle)
  svg.addEventListener("pointermove", (ev) => {
    if (tlState.drag) return;
    const rect = svg.getBoundingClientRect();
    if (!rect.width) return;
    const relX = ((ev.clientX - rect.left) / rect.width) * W;
    const frac = Math.max(0, Math.min(1, (relX - padL) / Math.max(1, plotW)));
    const idx = Math.round(frac * (basePts.length - 1));
    if (idx !== tlState.scrubIdx) updateScrub(idx);
  });

  // Click anywhere in plot area to inspect exact day-by-day bill/payday breakdown
  svg.addEventListener("click", (ev) => {
    if (tlState.drag) return;
    if (ev.target && ev.target.closest && ev.target.closest(".timeline-goal-marker")) return;
    const rect = svg.getBoundingClientRect();
    if (!rect.width) return;
    const relX = ((ev.clientX - rect.left) / rect.width) * W;
    const frac = Math.max(0, Math.min(1, (relX - padL) / Math.max(1, plotW)));
    const idx = Math.round(frac * (basePts.length - 1));
    tlState.pinnedDayIdx = idx;
    updateScrub(idx);
    renderDayBreakdown(idx);
  });

  wrap.replaceChildren(svg);
  updateScrub(initIdx);
  if (tlState.pinnedDayIdx != null && tlState.pinnedDayIdx < basePts.length) {
    renderDayBreakdown(tlState.pinnedDayIdx);
  } else if (dayBreakdownBox) {
    dayBreakdownBox.classList.add("hidden");
    dayBreakdownBox.replaceChildren();
  }
}

/* ============================================================================
   Total Net Worth (Liquid + Investments - Debt) Timeline Renderer
   ============================================================================ */

function renderNetWorthTimeline(pts) {
  const wrap = $("timeline-svg-wrap");
  const scrubBar = $("timeline-scrub-bar");
  if (!wrap) return;

  if (!Array.isArray(pts) || !pts.length) {
    renderTimelineEmpty("No net worth snapshots recorded yet. Sync accounts or holdings to reconstruct 730d Net Worth.");
    return;
  }

  updateTimelineKpis([], [], 0, true, pts);

  const W = 1040;
  const H = 310;
  const padL = 66;
  const padR = 24;
  const padT = 26;
  const padB = 34;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  const baselineY = padT + plotH;

  const vals = [];
  pts.forEach((p) => {
    vals.push(
      Number(p.net_worth_cents || 0),
      Number(p.liquid_cents || 0),
      Number(p.investment_cents || 0),
      Number(p.debt_cents || 0)
    );
  });
  let minVal = Math.min(...vals, 0);
  let maxVal = Math.max(...vals, 10000);
  const span = Math.max(1000, maxVal - minVal);
  const yMin = minVal < 0 ? minVal - span * 0.08 : 0;
  const yMax = maxVal + span * 0.12;

  const xFor = (i) => padL + (pts.length === 1 ? plotW / 2 : (i / (pts.length - 1)) * plotW);
  const yFor = (v) => padT + (1 - (v - yMin) / Math.max(1, yMax - yMin)) * plotH;

  const defs = svgEl("defs", {}, [
    svgEl("linearGradient", { id: "nwAreaGrad", x1: "0", y1: "0", x2: "0", y2: "1" }, [
      svgEl("stop", { offset: "0%", "stop-color": "var(--chart-sur-stroke)", "stop-opacity": "0.28" }),
      svgEl("stop", { offset: "100%", "stop-color": "var(--chart-sur-stroke)", "stop-opacity": "0.02" }),
    ]),
  ]);

  const gridNodes = [];
  for (let t = 0; t <= 4; t++) {
    const val = yMin + ((yMax - yMin) * t) / 4;
    const y = yFor(val);
    gridNodes.push(
      svgEl("line", {
        x1: padL,
        y1: y.toFixed(1),
        x2: W - padR,
        y2: y.toFixed(1),
        stroke: "var(--chart-grid)",
        "stroke-dasharray": t === 0 ? "none" : "3 3",
      })
    );
    gridNodes.push(
      svgEl("text", {
        x: padL - 8,
        y: (y + 3.5).toFixed(1),
        fill: "var(--muted)",
        "font-family": "var(--font-mono)",
        "font-size": "10",
        "text-anchor": "end",
        text: fmtMoneyCompact(val),
      })
    );
  }

  const tickCount = Math.min(7, pts.length);
  for (let k = 0; k < tickCount; k++) {
    const idx = tickCount === 1 ? 0 : Math.round((k * (pts.length - 1)) / (tickCount - 1));
    gridNodes.push(
      svgEl("text", {
        x: xFor(idx).toFixed(1),
        y: H - 10,
        fill: "var(--muted)",
        "font-family": "var(--font-mono)",
        "font-size": "10",
        "text-anchor": k === 0 ? "start" : k === tickCount - 1 ? "end" : "middle",
        text: fmtShortDate(pts[idx].date),
      })
    );
  }

  const pathFor = (key) =>
    pts.map((p, i) => `${i === 0 ? "M" : "L"}${xFor(i).toFixed(1)},${yFor(Number(p[key] || 0)).toFixed(1)}`).join(" ");

  const nwPath = pathFor("net_worth_cents");
  const nwArea = `${nwPath} L${xFor(pts.length - 1).toFixed(1)},${yFor(0).toFixed(1)} L${xFor(0).toFixed(1)},${yFor(0).toFixed(1)} Z`;

  const initIdx = pts.length - 1;
  const crosshair = svgEl("line", {
    x1: xFor(initIdx).toFixed(1),
    y1: padT,
    x2: xFor(initIdx).toFixed(1),
    y2: baselineY,
    stroke: "var(--chart-crosshair)",
    "stroke-width": "1",
    "stroke-dasharray": "2 2",
  });
  const dot = svgEl("circle", {
    cx: xFor(initIdx).toFixed(1),
    cy: yFor(Number(pts[initIdx].net_worth_cents || 0)).toFixed(1),
    r: "5",
    fill: "var(--chart-sur-stroke)",
    stroke: "var(--bg)",
    "stroke-width": "1.6",
  });

  const updateNwScrub = (idx) => {
    const safeIdx = Math.max(0, Math.min(pts.length - 1, idx));
    tlState.scrubIdx = safeIdx;
    const p = pts[safeIdx];
    crosshair.setAttribute("x1", xFor(safeIdx).toFixed(1));
    crosshair.setAttribute("x2", xFor(safeIdx).toFixed(1));
    dot.setAttribute("cx", xFor(safeIdx).toFixed(1));
    dot.setAttribute("cy", yFor(Number(p.net_worth_cents || 0)).toFixed(1));
    if (scrubBar) {
      scrubBar.replaceChildren(
        el("span", { class: "row-gap wrap", style: { gap: "12px" } }, [
          el("strong", { text: fmtDayDate(p.date) }),
          el("span", { style: { color: "var(--chart-sur-text)", fontWeight: "800" }, text: `Net Worth ${fmtMoney(p.net_worth_cents)}` }),
          el("span", { style: { color: "var(--chart-inc-text)", fontWeight: "700" }, text: `Liquid ${fmtMoneyCompact(p.liquid_cents)}` }),
          el("span", { style: { color: "var(--chart-inv-text)", fontWeight: "700" }, text: `Invest ${fmtMoneyCompact(p.investment_cents)}` }),
          el("span", { style: { color: "var(--chart-def-text)", fontWeight: "700" }, text: `Debt ${fmtMoneyCompact(p.debt_cents)}` }),
        ]),
        el("span", { class: "muted small", text: "Assets + Investments - Debt" })
      );
    }
  };

  const svg = svgEl(
    "svg",
    { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Total Net Worth timeline chart" },
    [
      defs,
      ...gridNodes,
      svgEl("path", { d: nwArea, fill: "url(#nwAreaGrad)" }),
      svgEl("path", { d: pathFor("liquid_cents"), fill: "none", stroke: "var(--chart-inc-stroke)", "stroke-width": "1.6" }),
      svgEl("path", { d: pathFor("investment_cents"), fill: "none", stroke: "var(--chart-inv-stroke)", "stroke-width": "1.6", "stroke-dasharray": "2.5 2.5" }),
      svgEl("path", { d: pathFor("debt_cents"), fill: "none", stroke: "var(--chart-def-stroke)", "stroke-width": "1.5", "stroke-dasharray": "5 3" }),
      svgEl("path", { d: nwPath, fill: "none", stroke: "var(--chart-sur-stroke)", "stroke-width": "2.6" }),
      crosshair,
      dot,
    ]
  );

  svg.addEventListener("pointermove", (ev) => {
    const rect = svg.getBoundingClientRect();
    if (!rect.width) return;
    const relX = ((ev.clientX - rect.left) / rect.width) * W;
    const frac = Math.max(0, Math.min(1, (relX - padL) / Math.max(1, plotW)));
    const idx = Math.round(frac * (pts.length - 1));
    if (idx !== tlState.scrubIdx) updateNwScrub(idx);
  });

  wrap.replaceChildren(svg);
  updateNwScrub(initIdx);
}

/* ============================================================================
   Marginal-ROI Surplus Allocation Cards
   ============================================================================ */

function renderOptimalAllocations(steps, totalAnnualBenefitCents) {
  const grid = $("timeline-alloc-grid");
  if (!grid) return;
  if (!steps || !steps.length) {
    grid.replaceChildren(
      el("div", {
        class: "item-box muted small",
        text: "No extra flexible surplus to allocate right now — bills and liquidity shield come first.",
      })
    );
    return;
  }

  const cards = steps.map((st) => {
    const bucketName = BUCKET_LABELS[st.bucket] || String(st.bucket || "").replace(/_/g, " ");
    const aprVal = Number(st.effective_apr_equivalent_pct || 0);
    const aprLabel =
      aprVal >= 50
        ? "Protection Priority"
        : aprVal > 0
        ? `${aprVal.toFixed(1)}% eff. APR`
        : "Surplus Allocation";
    const benefitNote =
      st.expected_annual_benefit_cents > 0 ? `+${fmtMoney(st.expected_annual_benefit_cents)}/yr value` : null;

    return el("div", { class: "alloc-card" }, [
      el("div", { class: "item-row" }, [
        el("span", { class: "pill pill-soft", text: `#${st.priority_rank} · ${bucketName}` }),
        el("span", { class: `pill ${aprVal >= 12 ? "pill-calm" : "pill-blue"}`, text: aprLabel }),
      ]),
      el("strong", { text: st.target_name }),
      el("div", { class: "item-row" }, [
        el("span", { class: "alloc-amt", text: fmtMoney(st.amount_cents) }),
        benefitNote ? el("span", { class: "muted small tabular-nums", text: benefitNote }) : null,
      ]),
      el("div", { class: "small muted", text: st.rationale }),
      st.formula_note
        ? el("details", { class: "math-drawer" }, [
            el("summary", { text: "Formula & math proof" }),
            el("div", { class: "small muted tabular-nums", style: { marginTop: "6px" }, text: st.formula_note }),
          ])
        : null,
    ]);
  });

  if (totalAnnualBenefitCents > 0) {
    cards.push(
      el("div", { class: "alloc-card", style: { borderLeftColor: "var(--cyan)" } }, [
        el("span", { class: "pill pill-blue", text: "Estimated Annual Value" }),
        el("strong", { text: "Combined Annual Savings" }),
        el("div", { class: "alloc-amt", style: { color: "var(--cyan)" }, text: `+${fmtMoney(totalAnnualBenefitCents)}/yr` }),
        el("div", {
          class: "small muted",
          text: "Combined interest avoided, promo deadline protection, and savings yield.",
        }),
      ])
    );
  }

  grid.replaceChildren(...cards);
}

/* ============================================================================
   Module Bootstrap
   ============================================================================ */

export function initTimeline() {
  const modeSw = $("timeline-mode-switcher");
  if (modeSw) {
    modeSw.addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-timeline-mode]");
      if (!btn) return;
      tlState.mode = btn.dataset.timelineMode;
      loadTimelineData();
    });
  }

  const zoomSw = $("timeline-zoom-switcher");
  if (zoomSw) {
    zoomSw.addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-timeline-zoom]");
      if (!btn) return;
      tlState.zoom = btn.dataset.timelineZoom;
      loadTimelineData();
    });
  }

  const scenSel = $("timeline-scenario-select");
  if (scenSel) {
    scenSel.addEventListener("change", () => {
      tlState.activeScenarioId = scenSel.value ? Number(scenSel.value) : null;
      tlState.adHocOverrides.goal_date_shifts = {};
      syncModeAndZoomUI();
      loadTimelineData();
    });
  }

  const editor = $("timeline-scenario-editor");
  const newBtn = $("btn-timeline-new-scenario");
  const cancelBtn = $("btn-timeline-cancel-scenario");
  const delBtn = $("btn-timeline-delete-scenario");
  const form = $("timeline-scenario-form");

  if (newBtn && editor) {
    newBtn.addEventListener("click", () => {
      editor.classList.toggle("hidden");
      const dtInput = $("ts-oneoff-date");
      if (dtInput && !dtInput.value) dtInput.value = isoDateOffset(30);
    });
  }
  if (cancelBtn && editor) {
    cancelBtn.addEventListener("click", () => editor.classList.add("hidden"));
  }

  if (form) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const submitBtn = form.querySelector("button[type=submit]");
      await withBusy(submitBtn, async () => {
        try {
          const name = ($("ts-name") && $("ts-name").value.trim()) || "";
          if (!name) throw new Error("Enter a scenario name.");
          const monthlyVal = Number(($("ts-monthly-delta") && $("ts-monthly-delta").value) || 0);
          const monthlyCents = Number.isFinite(monthlyVal) ? Math.round(monthlyVal * 100) : 0;
          const oneOffVal = Number(($("ts-oneoff-delta") && $("ts-oneoff-delta").value) || 0);
          const oneOffCents = Number.isFinite(oneOffVal) ? Math.round(oneOffVal * 100) : 0;
          const oneOffDate = ($("ts-oneoff-date") && $("ts-oneoff-date").value) || isoDateOffset(30);

          const overrides = {
            goal_date_shifts: Object.assign({}, tlState.adHocOverrides.goal_date_shifts),
            monthly_income_delta_cents: monthlyCents > 0 ? monthlyCents : 0,
            monthly_expense_delta_cents: monthlyCents < 0 ? Math.abs(monthlyCents) : 0,
            one_off_events:
              oneOffCents !== 0
                ? [{ date: oneOffDate, amount_cents: oneOffCents, label: name }]
                : [],
          };

          const created = await api("/api/v1/scenarios", {
            method: "POST",
            body: JSON.stringify({ name, overrides, active: true }),
          });
          form.reset();
          if (editor) editor.classList.add("hidden");
          tlState.activeScenarioId = created && created.id != null ? Number(created.id) : null;
          tlState.adHocOverrides.goal_date_shifts = {};
          await loadScenarios();
          await loadTimelineData();
          toast(`Created scenario "${name}".`);
        } catch (err) {
          toast(err.message || "Could not create scenario.", true);
        }
      });
    });
  }

  if (delBtn) {
    delBtn.addEventListener("click", (e) =>
      withBusy(e.currentTarget, async () => {
        if (tlState.activeScenarioId == null) return;
        const targetId = tlState.activeScenarioId;
        try {
          await api(`/api/v1/scenarios/${targetId}`, { method: "DELETE" });
          tlState.activeScenarioId = null;
          tlState.adHocOverrides.goal_date_shifts = {};
          await loadScenarios();
          await loadTimelineData();
          toast("Deleted scenario.");
        } catch (err) {
          toast(err.message || "Could not delete scenario.", true);
        }
      })
    );
  }

  registerDashboardHook((dash) => {
    if (dash && dash.quant_forecast) {
      tlState.quantForecast = dash.quant_forecast;
      renderOptimalAllocations(
        dash.quant_forecast.optimal_allocations || [],
        dash.quant_forecast.total_expected_annual_benefit_cents || 0
      );
    }
  });

  registerTabHook("analytics", async () => {
    await loadScenarios();
    await loadTimelineData();
  });

  registerRefreshHook(async () => {
    if (state.activeTab === "analytics") {
      await loadScenarios();
      await loadTimelineData();
    }
  });
}
