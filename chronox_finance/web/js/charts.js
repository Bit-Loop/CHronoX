/**
 * ChronoX Finance — Clean-Room MoneyStill Analytics Dashboard (ES Module)
 * Pure zero-CDN SVG visualizations & interactive Pointer Events:
 * 1. Unified Period Selector (30D | 90D | 1Y | 2Y | Single-Month Drilldown)
 * 2. Top 3 KPI Strip (Savings Rate %, Avg Monthly Surplus, Top 5 Expenses with inline bars)
 * 3. Full-Width 3-Column Cashflow Sankey with dynamic non-clipping right gutter & single-flow hover isolation
 * 4. Segmented Expense Distribution Donut (shared Sankey palette + 2-col interactive legend)
 * 5. Annual/2Y Income / Expenses / Surplus Trend Chart (crosshair scrub, floating readout, month filter)
 */

import {
  $,
  el,
  svgEl,
  makeIcon,
  api,
  state,
  toast,
  withBusy,
  fmtMoney,
  fmtMoneyCompact,
  registerTabHook,
  registerRefreshHook,
  prefersReducedMotion,
} from "./core.js";

const EXPENSE_PALETTE = [
  "#f59e0b", // Amber
  "#3b82f6", // Cobalt Blue
  "#a855f7", // Violet
  "#ec4899", // Magenta
  "#14b8a6", // Teal
  "#64748b", // Steel (Others)
];
const COLOR_INCOME = "#10b981";
const COLOR_SAVINGS = "#06b6d4";
const COLOR_EXPENSE = "#f59e0b";
const COLOR_DEFICIT = "#ef4444";

const chartState = {
  period: "1y",
  month: null,
  data: null,
  loading: false,
  hiddenSeries: new Set(),
  trendScrubIdx: null,
  donutHoveredIdx: null,
};

function truncateLabel(text, maxChars = 22) {
  const s = String(text ?? "").trim();
  if (s.length <= maxChars) return s;
  return `${s.slice(0, Math.max(1, maxChars - 1))}...`;
}

function fmtMonthShort(ym) {
  if (!ym || !/^\d{4}-\d{2}$/.test(String(ym))) return String(ym || "--");
  const [y, m] = String(ym).split("-").map(Number);
  const dt = new Date(y, m - 1, 1);
  if (Number.isNaN(dt.getTime())) return String(ym);
  return dt.toLocaleDateString(undefined, { month: "short", year: "2-digit" });
}

function fmtMonthAxis(ym, showYear = false) {
  if (!ym || !/^\d{4}-\d{2}$/.test(String(ym))) return String(ym || "");
  const [y, m] = String(ym).split("-").map(Number);
  const dt = new Date(y, m - 1, 1);
  if (Number.isNaN(dt.getTime())) return String(ym);
  if (showYear || m === 1) {
    return `${dt.toLocaleDateString(undefined, { month: "short" })} '${String(y).slice(-2)}`;
  }
  return dt.toLocaleDateString(undefined, { month: "short" });
}

function syncPeriodUI() {
  const switcher = $("analytics-period-switcher");
  if (switcher) {
    switcher.querySelectorAll("[data-analytics-period]").forEach((btn) => {
      const active = !chartState.month && btn.dataset.analyticsPeriod === chartState.period;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-pressed", String(active));
    });
  }
  const chip = $("analytics-month-chip");
  const clearBtn = $("btn-analytics-clear-month");
  if (chip) {
    if (chartState.month) {
      chip.textContent = `MONTH: ${fmtMonthShort(chartState.month)}`;
      chip.classList.remove("hidden");
    } else {
      chip.classList.add("hidden");
    }
  }
  if (clearBtn) {
    clearBtn.classList.toggle("hidden", !chartState.month);
  }
}

export async function loadAnalyticsOverview(opts = {}) {
  if (state.vaultStatus !== "unlocked") return null;
  if (opts.period) {
    chartState.period = opts.period;
    if (!opts.keepMonth) chartState.month = null;
  }
  if ("month" in opts) {
    chartState.month = opts.month || null;
  }
  syncPeriodUI();

  if (chartState.loading) return chartState.data;
  chartState.loading = true;
  try {
    const queryPeriod = chartState.month || chartState.period || "1y";
    const params = new URLSearchParams({ period: queryPeriod });
    if (chartState.month) {
      params.set("month", chartState.month);
    }
    const data = await api(`/api/v1/analytics/overview?${params.toString()}`);
    chartState.data = data;
    state.analytics = data;
    renderAnalyticsOverview(data);
    return data;
  } catch (err) {
    renderAnalyticsError(err && err.message ? err.message : "Failed to load analytics.");
    return null;
  } finally {
    chartState.loading = false;
  }
}

function renderAnalyticsError(message) {
  const strip = $("analytics-kpi-strip");
  if (strip) {
    strip.replaceChildren(
      el("div", { class: "card", style: { gridColumn: "1 / -1" } }, [
        el("div", { class: "inline-icon-row small muted" }, [
          makeIcon("alert", "ui-icon-sm"),
          String(message),
        ]),
      ])
    );
  }
}

function renderAnalyticsOverview(data) {
  if (!data) return;
  renderKpiStrip(data);
  renderCashflowSankey(data);
  renderExpenseDonut(data);
  renderTrendChart(data);
}

/* ============================================================================
   1. Top 3 KPI Strip (Savings Rate %, Avg Monthly Surplus, Top 5 Expenses)
   ============================================================================ */

function renderKpiStrip(data) {
  const host = $("analytics-kpi-strip");
  if (!host) return;

  const savingsRate = Number(data.savings_rate_pct || 0);
  const avgSurplus = Number(data.avg_monthly_surplus_cents || 0);
  const totalIncome = Number(data.total_income_cents || 0);
  const totalExpense = Number(data.total_expense_cents || 0);
  const netSurplus = Number(data.net_surplus_cents || 0);
  const top5 = Array.isArray(data.top_5_expenses) ? data.top_5_expenses.slice(0, 5) : [];

  const rateColor =
    savingsRate >= 20 ? "var(--primary)" : savingsRate >= 5 ? "var(--cyan)" : savingsRate >= 0 ? "var(--amber)" : "var(--crimson)";
  const rateBadgeCls =
    savingsRate >= 20 ? "pill-calm" : savingsRate >= 5 ? "pill-blue" : savingsRate >= 0 ? "pill-watch" : "pill-danger";
  const rateBadgeTxt =
    savingsRate >= 30 ? "STRONG SURPLUS" : savingsRate >= 15 ? "HEALTHY RATE" : savingsRate >= 0 ? "TIGHT MARGIN" : "DEFICIT";

  const clampedRate = Math.max(0, Math.min(100, savingsRate));

  const card1 = el("article", { class: "card stat-tile", style: { borderTopColor: rateColor } }, [
    el("div", { class: "item-row" }, [
      el("span", { class: "eyebrow", text: "SAVINGS RATE" }),
      el("span", { class: `pill ${rateBadgeCls}`, text: rateBadgeTxt }),
    ]),
    el("div", {
      class: "metric-md tabular-nums",
      style: { color: rateColor, fontSize: "1.95rem" },
      text: `${savingsRate.toFixed(1)}%`,
    }),
    el("div", { class: "progress-track", style: { height: "5px" } }, [
      el("div", {
        class: "progress-fill",
        style: { width: `${clampedRate}%`, backgroundColor: rateColor },
      }),
    ]),
    el("div", {
      class: "muted small tabular-nums",
      text: `Net ${netSurplus >= 0 ? "+" : ""}${fmtMoney(netSurplus)} of ${fmtMoney(totalIncome)} income`,
    }),
  ]);

  const surplusColor = avgSurplus >= 0 ? "var(--primary)" : "var(--crimson)";
  const periodCaption = chartState.month
    ? `Selected month (${fmtMonthShort(chartState.month)})`
    : `${data.start_date || "--"} to ${data.end_date || "--"}`;

  const card2 = el("article", { class: "card stat-tile", style: { borderTopColor: surplusColor } }, [
    el("div", { class: "item-row" }, [
      el("span", { class: "eyebrow", text: "AVG MONTHLY SURPLUS" }),
      el("span", { class: "pill pill-soft", text: String(data.period || chartState.period).toUpperCase() }),
    ]),
    el("div", {
      class: "metric-md tabular-nums",
      style: { color: surplusColor, fontSize: "1.95rem" },
      text: `${avgSurplus >= 0 ? "+" : ""}${fmtMoney(avgSurplus)}`,
    }),
    el("div", { class: "item-row small tabular-nums" }, [
      el("span", { style: { color: "var(--primary)" }, text: `IN ${fmtMoneyCompact(totalIncome)}` }),
      el("span", { style: { color: "var(--amber)" }, text: `OUT ${fmtMoneyCompact(totalExpense)}` }),
    ]),
    el("div", { class: "muted small", text: periodCaption }),
  ]);

  // Card 3: Top 5 Expenses with crisp inline proportional bars
  const maxExp = top5.reduce((m, item) => Math.max(m, Number(item.amount_cents || 0)), 1);
  const top5Rows = top5.length
    ? top5.map((item, idx) => {
        const amt = Number(item.amount_cents || 0);
        const pctExp = Number(item.pct_of_expenses || 0);
        const barPct = Math.max(2, Math.min(100, (amt / maxExp) * 100));
        const barColor = item.color || EXPENSE_PALETTE[idx % EXPENSE_PALETTE.length];
        return el("div", { class: "stack-xs", style: { gap: "3px" } }, [
          el("div", { class: "item-row small", style: { gap: "6px" } }, [
            el("span", { class: "row-gap", style: { gap: "6px", minWidth: "0" } }, [
              el("span", {
                class: "tabular-nums muted",
                style: { fontSize: "0.7rem", fontWeight: "700" },
                text: `0${idx + 1}`,
              }),
              el("span", {
                style: {
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                  fontWeight: "600",
                },
                title: item.label,
                text: truncateLabel(item.label, 20),
              }),
            ]),
            el("span", {
              class: "tabular-nums",
              style: { fontWeight: "700", whiteSpace: "nowrap" },
              text: `${fmtMoney(amt)} (${pctExp.toFixed(0)}%)`,
            }),
          ]),
          el("div", { class: "progress-track", style: { height: "4px" } }, [
            el("div", {
              class: "progress-fill",
              style: { width: `${barPct.toFixed(1)}%`, backgroundColor: barColor },
            }),
          ]),
        ]);
      })
    : [
        el("p", {
          class: "muted small",
          text: "No expenses recorded in this period yet.",
        }),
      ];

  const card3 = el("article", { class: "card stat-tile", style: { borderTopColor: "var(--amber)" } }, [
    el("div", { class: "item-row" }, [
      el("span", { class: "eyebrow", text: "TOP 5 EXPENSES" }),
      el("span", { class: "pill pill-watch", text: `${top5.length} CATEGORIES` }),
    ]),
    el("div", { class: "stack-xs", style: { gap: "7px", marginTop: "2px" } }, top5Rows),
  ]);

  host.replaceChildren(card1, card2, card3);
}

/* ============================================================================
   2. Full-Width 3-Column Cashflow Sankey (Pure Zero-CDN SVG)
      Fixes right-edge label clipping via dynamic right gutter & hover isolation
   ============================================================================ */

function renderCashflowSankey(data) {
  const wrap = $("analytics-sankey-wrap");
  const meta = $("analytics-sankey-meta");
  const legendHost = $("analytics-sankey-legend");
  if (!wrap) return;

  const rawNodes = Array.isArray(data.sankey_nodes) ? data.sankey_nodes : [];
  const rawLinks = Array.isArray(data.sankey_links) ? data.sankey_links : [];

  if (legendHost) {
    legendHost.replaceChildren(
      el("span", { class: "pill pill-calm", text: "INCOME" }),
      el("span", { class: "pill pill-watch", text: "TOP EXPENSES" }),
      el("span", { class: "pill pill-blue", text: "SAVINGS / INVEST" })
    );
  }

  const defaultMetaText =
    "Hover or focus any ribbon or node to isolate a single dollar flow (<1s trace).";
  if (meta) meta.textContent = defaultMetaText;

  if (!rawNodes.length || !rawLinks.length) {
    wrap.replaceChildren(
      el("div", { class: "item-box muted small", style: { margin: "12px" } }, [
        el("span", { class: "inline-icon-row" }, [
          makeIcon("chart", "ui-icon-sm"),
          "Log or import income and expense transactions to render the 3-column Cashflow Sankey.",
        ]),
      ])
    );
    return;
  }

  const col0 = rawNodes.filter((n) => Number(n.column) === 0 && Number(n.amount_cents) > 0);
  const col1 = rawNodes.filter((n) => Number(n.column) === 1);
  const col2 = rawNodes.filter((n) => Number(n.column) === 2 && Number(n.amount_cents) > 0);

  if (!col1.length || (!col0.length && !col2.length)) {
    wrap.replaceChildren(
      el("div", { class: "item-box muted small", style: { margin: "12px" } }, [
        "Insufficient flow data in this period for Sankey rendering.",
      ])
    );
    return;
  }

  // Dynamic dimensions & generous gutters so right-side labels NEVER clip
  const maxNodesInSide = Math.max(col0.length, col2.length, 3);
  const W = 1140;
  const H = Math.max(360, 110 + maxNodesInSide * 44);
  const padL = 225; // Left gutter for income source names + amounts
  const padR = 290; // Dynamic right gutter for category + "$1,850 (31.2%)"
  const padT = 40;
  const padB = 28;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  const nodeW = 14;
  const hubW = 18;
  const nodeGap = 14;

  const colX = {
    0: padL,
    1: padL + Math.round((plotW - hubW) / 2),
    2: W - padR - nodeW,
  };

  // Compute global vertical scale so total column value maps cleanly into plotH
  const sumCol = (arr) => arr.reduce((acc, n) => acc + Math.max(0, Number(n.amount_cents) || 0), 0);
  const total0 = sumCol(col0);
  const total1 = sumCol(col1);
  const total2 = sumCol(col2);
  const maxColVal = Math.max(total0, total1, total2, 1);

  const usableHForCol = (arr) => Math.max(60, plotH - Math.max(0, arr.length - 1) * nodeGap);
  const pxPerCent = Math.min(
    usableHForCol(col0) / Math.max(total0, 1),
    usableHForCol(col1) / Math.max(total1, 1),
    usableHForCol(col2) / Math.max(total2, 1),
    usableHForCol(col2) / maxColVal
  );

  // Position nodes vertically centered in plotH
  const nodeMap = new Map();
  const layoutColumn = (items, colIdx) => {
    const minNodeH = 12;
    const rawHeights = items.map((n) => Math.max(minNodeH, (Number(n.amount_cents) || 0) * pxPerCent));
    let totalH = rawHeights.reduce((a, b) => a + b, 0) + Math.max(0, items.length - 1) * nodeGap;
    let scale = 1;
    if (totalH > plotH) {
      const avail = Math.max(40, plotH - Math.max(0, items.length - 1) * nodeGap);
      scale = avail / Math.max(1, rawHeights.reduce((a, b) => a + b, 0));
      totalH = plotH;
    }
    let y = padT + Math.max(0, (plotH - totalH) / 2);
    items.forEach((n, idx) => {
      const h = Math.max(6, rawHeights[idx] * scale);
      const w = colIdx === 1 ? hubW : nodeW;
      const defaultColor =
        colIdx === 0
          ? COLOR_INCOME
          : colIdx === 1
          ? COLOR_INCOME
          : String(n.id || "").includes("saving")
          ? COLOR_SAVINGS
          : EXPENSE_PALETTE[idx % EXPENSE_PALETTE.length];
      const placed = {
        ...n,
        col: colIdx,
        x: colX[colIdx],
        y,
        w,
        h,
        color: n.color || defaultColor,
        outOffset: 0,
        inOffset: 0,
      };
      nodeMap.set(n.id, placed);
      y += h + nodeGap;
    });
  };

  layoutColumn(col0, 0);
  layoutColumn(col1, 1);
  layoutColumn(col2, 2);

  // Build links with exact stacked offsets on source & target nodes
  const defs = svgEl("defs");
  const validLinks = rawLinks
    .filter((lnk) => nodeMap.has(lnk.source) && nodeMap.has(lnk.target) && Number(lnk.amount_cents) > 0)
    .sort((a, b) => {
      const sa = nodeMap.get(a.source);
      const sb = nodeMap.get(b.source);
      const ta = nodeMap.get(a.target);
      const tb = nodeMap.get(b.target);
      if (sa.col !== sb.col) return sa.col - sb.col;
      if (sa.y !== sb.y) return sa.y - sb.y;
      return ta.y - tb.y;
    });

  // Compute total outgoing/incoming flow per node so ribbon thicknesses fill node height cleanly
  const outTotals = new Map();
  const inTotals = new Map();
  for (const lnk of validLinks) {
    const v = Number(lnk.amount_cents) || 0;
    outTotals.set(lnk.source, (outTotals.get(lnk.source) || 0) + v);
    inTotals.set(lnk.target, (inTotals.get(lnk.target) || 0) + v);
  }

  const linkEls = [];
  const nodeElMap = new Map();
  const labelElMap = new Map();

  validLinks.forEach((lnk, idx) => {
    const src = nodeMap.get(lnk.source);
    const tgt = nodeMap.get(lnk.target);
    const val = Number(lnk.amount_cents) || 0;
    const srcFrac = val / Math.max(1, outTotals.get(lnk.source) || src.amount_cents || 1);
    const tgtFrac = val / Math.max(1, inTotals.get(lnk.target) || tgt.amount_cents || 1);
    const hSrc = Math.max(2, src.h * srcFrac);
    const hTgt = Math.max(2, tgt.h * tgtFrac);

    const y0Top = src.y + src.outOffset;
    const y0Bot = Math.min(src.y + src.h, y0Top + hSrc);
    src.outOffset += hSrc;

    const y1Top = tgt.y + tgt.inOffset;
    const y1Bot = Math.min(tgt.y + tgt.h, y1Top + hTgt);
    tgt.inOffset += hTgt;

    const x0 = src.x + src.w;
    const x1 = tgt.x;
    const cx = (x0 + x1) / 2;

    const gradId = `sankey-grad-${idx}`;
    const startColor = src.color;
    const endColor = lnk.color || tgt.color;
    defs.appendChild(
      svgEl("linearGradient", { id: gradId, gradientUnits: "userSpaceOnUse", x1: x0, y1: 0, x2: x1, y2: 0 }, [
        svgEl("stop", { offset: "0%", "stop-color": startColor, "stop-opacity": "0.85" }),
        svgEl("stop", { offset: "100%", "stop-color": endColor, "stop-opacity": "0.85" }),
      ])
    );

    const d = [
      `M ${x0.toFixed(1)},${y0Top.toFixed(1)}`,
      `C ${cx.toFixed(1)},${y0Top.toFixed(1)} ${cx.toFixed(1)},${y1Top.toFixed(1)} ${x1.toFixed(1)},${y1Top.toFixed(1)}`,
      `L ${x1.toFixed(1)},${y1Bot.toFixed(1)}`,
      `C ${cx.toFixed(1)},${y1Bot.toFixed(1)} ${cx.toFixed(1)},${y0Bot.toFixed(1)} ${x0.toFixed(1)},${y0Bot.toFixed(1)}`,
      "Z",
    ].join(" ");

    const pctTxt = Number(lnk.pct_of_income || 0).toFixed(1);
    const titleStr = `${src.label} -> ${tgt.label}: ${fmtMoney(val)} (${pctTxt}% of income)`;

    const path = svgEl(
      "path",
      {
        d,
        fill: `url(#${gradId})`,
        "fill-opacity": "0.58",
        stroke: endColor,
        "stroke-opacity": "0.18",
        "stroke-width": "0.8",
        tabindex: "0",
        role: "graphics-symbol",
        "aria-label": titleStr,
        style: "cursor: pointer; transition: fill-opacity 120ms ease, stroke-opacity 120ms ease;",
      },
      [svgEl("title", { text: titleStr })]
    );
    path._sankeyMeta = {
      source: lnk.source,
      target: lnk.target,
      title: titleStr,
    };
    linkEls.push(path);
  });

  // Render Node Bars & Crisp Non-Clipping Labels
  const nodeGroup = svgEl("g");
  const labelGroup = svgEl("g");

  // Column headers
  labelGroup.appendChild(
    svgEl("text", {
      x: colX[0],
      y: 18,
      fill: "var(--muted)",
      "font-family": "var(--font-mono)",
      "font-size": "10",
      "font-weight": "700",
      "letter-spacing": "0.08em",
      "text-anchor": "start",
      text: "INCOME SOURCES",
    })
  );
  labelGroup.appendChild(
    svgEl("text", {
      x: colX[1] + hubW / 2,
      y: 18,
      fill: "var(--muted)",
      "font-family": "var(--font-mono)",
      "font-size": "10",
      "font-weight": "700",
      "letter-spacing": "0.08em",
      "text-anchor": "middle",
      text: "TOTAL INCOME HUB",
    })
  );
  labelGroup.appendChild(
    svgEl("text", {
      x: colX[2] + nodeW,
      y: 18,
      fill: "var(--muted)",
      "font-family": "var(--font-mono)",
      "font-size": "10",
      "font-weight": "700",
      "letter-spacing": "0.08em",
      "text-anchor": "end",
      text: "ALLOCATION & SURPLUS",
    })
  );

  for (const n of nodeMap.values()) {
    const pctStr = `${Number(n.pct_of_income || 0).toFixed(1)}%`;
    const rect = svgEl(
      "rect",
      {
        x: n.x.toFixed(1),
        y: n.y.toFixed(1),
        width: n.w,
        height: n.h.toFixed(1),
        rx: "0",
        fill: n.color,
        stroke: "var(--bg)",
        "stroke-width": "1",
        tabindex: "0",
        role: "graphics-symbol",
        "aria-label": `${n.label}: ${fmtMoney(n.amount_cents)} (${pctStr})`,
        style: "cursor: pointer; transition: opacity 120ms ease;",
      },
      [svgEl("title", { text: `${n.label}: ${fmtMoney(n.amount_cents)} (${pctStr})` })]
    );
    nodeGroup.appendChild(rect);
    nodeElMap.set(n.id, rect);

    const midY = n.y + n.h / 2;
    const lblG = svgEl("g", { style: "pointer-events: none; transition: opacity 120ms ease;" });

    if (n.col === 0) {
      // Left-column label placed inside left gutter (right-aligned toward bar)
      lblG.appendChild(
        svgEl("text", {
          x: (n.x - 10).toFixed(1),
          y: (midY - 2).toFixed(1),
          fill: "var(--text)",
          "font-family": "var(--font-sans)",
          "font-size": "12",
          "font-weight": "600",
          "text-anchor": "end",
          text: truncateLabel(n.label, 24),
        })
      );
      lblG.appendChild(
        svgEl("text", {
          x: (n.x - 10).toFixed(1),
          y: (midY + 12).toFixed(1),
          fill: "var(--primary)",
          "font-family": "var(--font-mono)",
          "font-size": "11",
          "font-weight": "700",
          "text-anchor": "end",
          text: `${fmtMoneyCompact(n.amount_cents)} (${pctStr})`,
        })
      );
    } else if (n.col === 1) {
      // Center hub badge anchored cleanly above the hub bar
      lblG.appendChild(
        svgEl("text", {
          x: (n.x + n.w / 2).toFixed(1),
          y: Math.max(32, n.y - 8).toFixed(1),
          fill: "var(--text)",
          "font-family": "var(--font-mono)",
          "font-size": "12",
          "font-weight": "800",
          "text-anchor": "middle",
          text: `${fmtMoney(n.amount_cents)}`,
        })
      );
    } else {
      // Right-column label inside generous 290px gutter: left-aligned name + right-aligned "$amt (pct%)"
      const labelX = n.x + n.w + 10;
      const rightEdgeX = W - 10;
      lblG.appendChild(
        svgEl("text", {
          x: labelX.toFixed(1),
          y: (midY + 4).toFixed(1),
          fill: "var(--text)",
          "font-family": "var(--font-sans)",
          "font-size": "12",
          "font-weight": "600",
          "text-anchor": "start",
          text: truncateLabel(n.label, 19),
        })
      );
      lblG.appendChild(
        svgEl("text", {
          x: rightEdgeX.toFixed(1),
          y: (midY + 4).toFixed(1),
          fill: n.color === COLOR_SAVINGS ? "var(--cyan)" : "var(--text-soft)",
          "font-family": "var(--font-mono)",
          "font-size": "11.5",
          "font-weight": "700",
          "text-anchor": "end",
          text: `${fmtMoneyCompact(n.amount_cents)} (${pctStr})`,
        })
      );
    }

    labelGroup.appendChild(lblG);
    labelElMap.set(n.id, lblG);
  }

  // Single-Flow Hover Isolation Controller
  const resetIsolation = () => {
    for (const p of linkEls) {
      p.setAttribute("fill-opacity", "0.58");
      p.setAttribute("stroke-opacity", "0.18");
    }
    for (const r of nodeElMap.values()) r.setAttribute("opacity", "1");
    for (const lg of labelElMap.values()) lg.setAttribute("opacity", "1");
    if (meta) meta.textContent = defaultMetaText;
  };

  const isolateLink = (activePath) => {
    const activeNodes = new Set([activePath._sankeyMeta.source, activePath._sankeyMeta.target]);
    for (const p of linkEls) {
      const match = p === activePath;
      p.setAttribute("fill-opacity", match ? "0.94" : "0.14");
      p.setAttribute("stroke-opacity", match ? "0.85" : "0.05");
    }
    for (const [id, r] of nodeElMap.entries()) {
      r.setAttribute("opacity", activeNodes.has(id) ? "1" : "0.3");
    }
    for (const [id, lg] of labelElMap.entries()) {
      lg.setAttribute("opacity", activeNodes.has(id) ? "1" : "0.32");
    }
    if (meta) meta.textContent = activePath._sankeyMeta.title;
  };

  const isolateNode = (nodeId) => {
    const connectedNodes = new Set([nodeId]);
    let matchedCount = 0;
    for (const p of linkEls) {
      const m = p._sankeyMeta;
      const connected = m.source === nodeId || m.target === nodeId;
      if (connected) {
        connectedNodes.add(m.source);
        connectedNodes.add(m.target);
        matchedCount += 1;
      }
      p.setAttribute("fill-opacity", connected ? "0.9" : "0.14");
      p.setAttribute("stroke-opacity", connected ? "0.7" : "0.05");
    }
    for (const [id, r] of nodeElMap.entries()) {
      r.setAttribute("opacity", connectedNodes.has(id) ? "1" : "0.3");
    }
    for (const [id, lg] of labelElMap.entries()) {
      lg.setAttribute("opacity", connectedNodes.has(id) ? "1" : "0.32");
    }
    const n = nodeMap.get(nodeId);
    if (meta && n) {
      meta.textContent = `${n.label}: ${fmtMoney(n.amount_cents)} (${Number(n.pct_of_income || 0).toFixed(1)}% of income · ${matchedCount} flow${matchedCount === 1 ? "" : "s"})`;
    }
  };

  for (const p of linkEls) {
    p.addEventListener("pointerenter", () => isolateLink(p));
    p.addEventListener("focus", () => isolateLink(p));
    p.addEventListener("pointerleave", resetIsolation);
    p.addEventListener("blur", resetIsolation);
  }

  for (const [id, r] of nodeElMap.entries()) {
    r.addEventListener("pointerenter", () => isolateNode(id));
    r.addEventListener("focus", () => isolateNode(id));
    r.addEventListener("pointerleave", resetIsolation);
    r.addEventListener("blur", resetIsolation);
  }

  const svg = svgEl(
    "svg",
    {
      viewBox: `0 0 ${W} ${H}`,
      role: "img",
      "aria-label": "3-Column Cashflow Sankey diagram",
    },
    [defs, svgEl("g", {}, linkEls), nodeGroup, labelGroup]
  );

  wrap.replaceChildren(svg);
}

/* ============================================================================
   3. Bottom-Left Segmented Expense Distribution Donut + 2-Column Legend
   ============================================================================ */

function polarToCartesian(cx, cy, r, angleDeg) {
  const rad = ((angleDeg - 90) * Math.PI) / 180;
  return { x: cx + r * Math.cos(rad), y: cy + r * Math.sin(rad) };
}

function buildRingSegmentPath(cx, cy, rInner, rOuter, startDeg, endDeg) {
  const sweep = endDeg - startDeg;
  if (sweep >= 359.5) {
    // Full circle split into two 180-degree arcs
    const p1 = polarToCartesian(cx, cy, rOuter, 0);
    const p2 = polarToCartesian(cx, cy, rOuter, 180);
    const i1 = polarToCartesian(cx, cy, rInner, 180);
    const i2 = polarToCartesian(cx, cy, rInner, 0);
    return [
      `M ${p1.x.toFixed(2)},${p1.y.toFixed(2)}`,
      `A ${rOuter},${rOuter} 0 1 1 ${p2.x.toFixed(2)},${p2.y.toFixed(2)}`,
      `A ${rOuter},${rOuter} 0 1 1 ${p1.x.toFixed(2)},${p1.y.toFixed(2)}`,
      `M ${i2.x.toFixed(2)},${i2.y.toFixed(2)}`,
      `A ${rInner},${rInner} 0 1 0 ${i1.x.toFixed(2)},${i1.y.toFixed(2)}`,
      `A ${rInner},${rInner} 0 1 0 ${i2.x.toFixed(2)},${i2.y.toFixed(2)}`,
      "Z",
    ].join(" ");
  }
  const oStart = polarToCartesian(cx, cy, rOuter, startDeg);
  const oEnd = polarToCartesian(cx, cy, rOuter, endDeg);
  const iEnd = polarToCartesian(cx, cy, rInner, endDeg);
  const iStart = polarToCartesian(cx, cy, rInner, startDeg);
  const largeArc = sweep > 180 ? 1 : 0;

  return [
    `M ${oStart.x.toFixed(2)},${oStart.y.toFixed(2)}`,
    `A ${rOuter},${rOuter} 0 ${largeArc} 1 ${oEnd.x.toFixed(2)},${oEnd.y.toFixed(2)}`,
    `L ${iEnd.x.toFixed(2)},${iEnd.y.toFixed(2)}`,
    `A ${rInner},${rInner} 0 ${largeArc} 0 ${iStart.x.toFixed(2)},${iStart.y.toFixed(2)}`,
    "Z",
  ].join(" ");
}

function renderExpenseDonut(data) {
  const wrap = $("analytics-donut-wrap");
  const legend = $("analytics-donut-legend");
  if (!wrap || !legend) return;

  const items = (Array.isArray(data.expense_distribution) && data.expense_distribution.length
    ? data.expense_distribution
    : Array.isArray(data.top_5_expenses)
    ? data.top_5_expenses
    : []
  ).filter((it) => Number(it.amount_cents || 0) > 0);

  if (!items.length) {
    wrap.replaceChildren(
      el("div", { class: "muted small", text: "No categorized spending in this period." })
    );
    legend.replaceChildren();
    return;
  }

  const totalSpend = items.reduce((s, it) => s + Number(it.amount_cents || 0), 0);
  const cx = 130;
  const cy = 130;
  const rInner = 68;
  const rOuter = 102;
  const rOuterHover = 108;
  const gapDeg = items.length > 1 ? 2.4 : 0;

  const centerTitle = svgEl("text", {
    x: cx,
    y: cy - 10,
    fill: "var(--muted)",
    "font-family": "var(--font-mono)",
    "font-size": "10",
    "font-weight": "700",
    "letter-spacing": "0.08em",
    "text-anchor": "middle",
    text: "TOTAL SPEND",
  });
  const centerValue = svgEl("text", {
    x: cx,
    y: cy + 12,
    fill: "var(--text)",
    "font-family": "var(--font-mono)",
    "font-size": "17",
    "font-weight": "800",
    "text-anchor": "middle",
    text: fmtMoneyCompact(totalSpend),
  });
  const centerSub = svgEl("text", {
    x: cx,
    y: cy + 28,
    fill: "var(--muted)",
    "font-family": "var(--font-mono)",
    "font-size": "10",
    "font-weight": "600",
    "text-anchor": "middle",
    text: `${items.length} categories`,
  });

  let cursorDeg = 0;
  const segPaths = [];
  const legendNodes = [];

  const highlightIdx = (activeIdx) => {
    chartState.donutHoveredIdx = activeIdx;
    segPaths.forEach(({ path, startDeg, endDeg, idx }) => {
      const isAct = activeIdx === idx;
      const dimmed = activeIdx !== null && !isAct;
      path.setAttribute("d", buildRingSegmentPath(cx, cy, rInner, isAct ? rOuterHover : rOuter, startDeg, endDeg));
      path.setAttribute("fill-opacity", dimmed ? "0.28" : "1");
    });
    legendNodes.forEach((node, idx) => {
      node.style.borderColor = activeIdx === idx ? "var(--cyan)" : "";
      node.style.opacity = activeIdx !== null && activeIdx !== idx ? "0.5" : "1";
    });
    if (activeIdx !== null && items[activeIdx]) {
      const it = items[activeIdx];
      centerTitle.textContent = truncateLabel(it.label.toUpperCase(), 15);
      centerValue.textContent = fmtMoney(it.amount_cents);
      centerSub.textContent = `${Number(it.pct_of_expenses || 0).toFixed(1)}% of expenses`;
    } else {
      centerTitle.textContent = "TOTAL SPEND";
      centerValue.textContent = fmtMoneyCompact(totalSpend);
      centerSub.textContent = `${items.length} categories`;
    }
  };

  items.forEach((it, idx) => {
    const val = Number(it.amount_cents || 0);
    const sweep = Math.max(2.6, (val / Math.max(1, totalSpend)) * 360);
    const startDeg = cursorDeg + gapDeg / 2;
    const endDeg = Math.min(360 - gapDeg / 2, cursorDeg + sweep - gapDeg / 2);
    cursorDeg += sweep;

    const color = it.color || EXPENSE_PALETTE[idx % EXPENSE_PALETTE.length];
    const pctVal = Number(it.pct_of_expenses || (val / Math.max(1, totalSpend)) * 100);
    const path = svgEl(
      "path",
      {
        d: buildRingSegmentPath(cx, cy, rInner, rOuter, startDeg, Math.max(startDeg + 0.8, endDeg)),
        fill: color,
        tabindex: "0",
        role: "graphics-symbol",
        "aria-label": `${it.label}: ${fmtMoney(val)} (${pctVal.toFixed(1)}%)`,
        style: "cursor: pointer; transition: fill-opacity 120ms ease;",
      },
      [svgEl("title", { text: `${it.label}: ${fmtMoney(val)} (${pctVal.toFixed(1)}%)` })]
    );
    path.addEventListener("pointerenter", () => highlightIdx(idx));
    path.addEventListener("focus", () => highlightIdx(idx));
    path.addEventListener("pointerleave", () => highlightIdx(null));
    path.addEventListener("blur", () => highlightIdx(null));

    segPaths.push({ path, startDeg, endDeg: Math.max(startDeg + 0.8, endDeg), idx });

    const legRow = el(
      "button",
      {
        type: "button",
        class: "donut-legend-item",
        style: { cursor: "pointer", textAlign: "left", color: "var(--text)", width: "100%" },
      },
      [
        el("span", { class: "row-gap", style: { gap: "7px", minWidth: "0" } }, [
          el("span", { class: "legend-swatch", style: { backgroundColor: color } }),
          el("span", {
            style: { overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontWeight: "600" },
            title: it.label,
            text: truncateLabel(it.label, 16),
          }),
        ]),
        el("span", {
          class: "tabular-nums muted",
          style: { whiteSpace: "nowrap", fontSize: "0.75rem" },
          text: `${fmtMoneyCompact(val)} · ${pctVal.toFixed(0)}%`,
        }),
      ]
    );
    legRow.addEventListener("pointerenter", () => highlightIdx(idx));
    legRow.addEventListener("focus", () => highlightIdx(idx));
    legRow.addEventListener("pointerleave", () => highlightIdx(null));
    legRow.addEventListener("blur", () => highlightIdx(null));
    legendNodes.push(legRow);
  });

  const svg = svgEl(
    "svg",
    {
      viewBox: "0 0 260 260",
      style: "max-width: 248px; width: 100%; height: auto;",
      role: "img",
      "aria-label": "Expense distribution segmented donut chart",
    },
    [
      svgEl("circle", { cx, cy, r: (rInner + rOuter) / 2, fill: "none", stroke: "var(--ring-track)", "stroke-width": String(rOuter - rInner), opacity: "0.3" }),
      ...segPaths.map((s) => s.path),
      centerTitle,
      centerValue,
      centerSub,
    ]
  );

  wrap.replaceChildren(svg);
  legend.replaceChildren(...legendNodes);
}

/* ============================================================================
   4. Bottom-Right Annual / 2Y Trend Chart (Crosshair Scrub + Click Month)
   ============================================================================ */

function renderTrendChart(data) {
  const wrap = $("analytics-trend-wrap");
  const scrubBar = $("analytics-trend-scrub");
  if (!wrap || !scrubBar) return;

  const pts = Array.isArray(data.monthly_trends) ? data.monthly_trends : [];
  if (!pts.length) {
    wrap.replaceChildren(
      el("div", { class: "muted small", style: { padding: "14px" }, text: "No monthly trend points in this range yet." })
    );
    scrubBar.replaceChildren(el("span", { class: "muted small", text: "Waiting for monthly ledger data." }));
    return;
  }

  const showInc = !chartState.hiddenSeries.has("income");
  const showExp = !chartState.hiddenSeries.has("expense");
  const showSur = !chartState.hiddenSeries.has("surplus");

  const W = 640;
  const H = 264;
  const padL = 56;
  const padR = 18;
  const padT = 22;
  const padB = 34;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;

  const allVals = [0];
  pts.forEach((p) => {
    if (showInc) allVals.push(Number(p.income_cents || 0));
    if (showExp) allVals.push(Number(p.expense_cents || 0));
    if (showSur) allVals.push(Number(p.surplus_cents || 0));
  });
  let minVal = Math.min(...allVals);
  let maxVal = Math.max(...allVals);
  if (!Number.isFinite(minVal)) minVal = 0;
  if (!Number.isFinite(maxVal) || maxVal <= minVal) maxVal = minVal + 10000;
  const span = maxVal - minVal;
  const yMin = minVal < 0 ? minVal - span * 0.1 : 0;
  const yMax = maxVal + span * 0.12;

  const xFor = (i) => padL + (pts.length === 1 ? plotW / 2 : (i / (pts.length - 1)) * plotW);
  const yFor = (val) => padT + (1 - (val - yMin) / Math.max(1, yMax - yMin)) * plotH;
  const zeroY = yFor(0);

  const defs = svgEl("defs", {}, [
    svgEl("linearGradient", { id: "trendSurplusGrad", x1: "0", y1: "0", x2: "0", y2: "1" }, [
      svgEl("stop", { offset: "0%", "stop-color": COLOR_INCOME, "stop-opacity": "0.32" }),
      svgEl("stop", { offset: "100%", "stop-color": COLOR_INCOME, "stop-opacity": "0.02" }),
    ]),
  ]);

  // Y-axis gridlines & labels
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
        x: padL - 7,
        y: (y + 3.5).toFixed(1),
        fill: "var(--muted)",
        "font-family": "var(--font-mono)",
        "font-size": "10",
        "text-anchor": "end",
        text: fmtMoneyCompact(val),
      })
    );
  }

  if (yMin < 0 && yMax > 0) {
    gridNodes.push(
      svgEl("line", {
        x1: padL,
        y1: zeroY.toFixed(1),
        x2: W - padR,
        y2: zeroY.toFixed(1),
        stroke: "var(--border-strong)",
        "stroke-width": "1",
      })
    );
  }

  // X-axis month labels (thinned cleanly for 24-month views)
  const labelStep = pts.length > 14 ? 3 : pts.length > 8 ? 2 : 1;
  pts.forEach((p, i) => {
    if (i % labelStep !== 0 && i !== pts.length - 1) return;
    const anchor = i === 0 ? "start" : i === pts.length - 1 ? "end" : "middle";
    gridNodes.push(
      svgEl("text", {
        x: xFor(i).toFixed(1),
        y: H - 10,
        fill: chartState.month === p.month ? "var(--cyan)" : "var(--muted)",
        "font-family": "var(--font-mono)",
        "font-size": "10",
        "font-weight": chartState.month === p.month ? "800" : "500",
        "text-anchor": anchor,
        text: fmtMonthAxis(p.month, pts.length > 12),
      })
    );
  });

  // Selected Month Vertical Highlight Band
  const bandNodes = [];
  const selectedIdx = chartState.month ? pts.findIndex((p) => p.month === chartState.month) : -1;
  const colW = plotW / Math.max(1, pts.length);
  if (selectedIdx >= 0) {
    const sx = xFor(selectedIdx);
    bandNodes.push(
      svgEl("rect", {
        x: Math.max(padL, sx - colW / 2).toFixed(1),
        y: padT,
        width: Math.max(18, colW).toFixed(1),
        height: plotH,
        fill: "var(--cyan)",
        "fill-opacity": "0.11",
        stroke: "var(--cyan)",
        "stroke-opacity": "0.45",
        "stroke-width": "1",
      })
    );
  }

  // Build series paths
  const seriesNodes = [];
  const makeLinePath = (key) =>
    pts.map((p, i) => `${i === 0 ? "M" : "L"}${xFor(i).toFixed(1)},${yFor(Number(p[key] || 0)).toFixed(1)}`).join(" ");

  if (showSur && pts.length > 0) {
    const surLine = makeLinePath("surplus_cents");
    const areaD = `${surLine} L${xFor(pts.length - 1).toFixed(1)},${zeroY.toFixed(1)} L${xFor(0).toFixed(1)},${zeroY.toFixed(1)} Z`;
    seriesNodes.push(svgEl("path", { d: areaD, fill: "url(#trendSurplusGrad)" }));
    seriesNodes.push(
      svgEl("path", {
        d: surLine,
        fill: "none",
        stroke: COLOR_SAVINGS,
        "stroke-width": "1.6",
        "stroke-dasharray": "4 2",
      })
    );
  }
  if (showInc) {
    seriesNodes.push(
      svgEl("path", { d: makeLinePath("income_cents"), fill: "none", stroke: COLOR_INCOME, "stroke-width": "2.2" })
    );
    pts.forEach((p, i) => {
      seriesNodes.push(
        svgEl("circle", {
          cx: xFor(i).toFixed(1),
          cy: yFor(Number(p.income_cents || 0)).toFixed(1),
          r: selectedIdx === i ? "4.5" : "2.8",
          fill: COLOR_INCOME,
          stroke: "var(--bg)",
          "stroke-width": "1",
        })
      );
    });
  }
  if (showExp) {
    seriesNodes.push(
      svgEl("path", { d: makeLinePath("expense_cents"), fill: "none", stroke: COLOR_EXPENSE, "stroke-width": "2.2" })
    );
    pts.forEach((p, i) => {
      seriesNodes.push(
        svgEl("circle", {
          cx: xFor(i).toFixed(1),
          cy: yFor(Number(p.expense_cents || 0)).toFixed(1),
          r: selectedIdx === i ? "4.5" : "2.8",
          fill: COLOR_EXPENSE,
          stroke: "var(--bg)",
          "stroke-width": "1",
        })
      );
    });
  }

  // Interactive Crosshair, Enlarged Active Dots & Floating Readout Tooltip
  const initIdx = selectedIdx >= 0 ? selectedIdx : pts.length - 1;
  const crosshair = svgEl("line", {
    x1: xFor(initIdx).toFixed(1),
    y1: padT,
    x2: xFor(initIdx).toFixed(1),
    y2: padT + plotH,
    stroke: "var(--chart-crosshair)",
    "stroke-width": "1",
    "stroke-dasharray": "3 2",
  });
  const dotInc = svgEl("circle", {
    cx: xFor(initIdx).toFixed(1),
    cy: yFor(Number(pts[initIdx].income_cents || 0)).toFixed(1),
    r: "5.2",
    fill: COLOR_INCOME,
    stroke: "var(--bg)",
    "stroke-width": "1.6",
    opacity: showInc ? "1" : "0",
  });
  const dotExp = svgEl("circle", {
    cx: xFor(initIdx).toFixed(1),
    cy: yFor(Number(pts[initIdx].expense_cents || 0)).toFixed(1),
    r: "5.2",
    fill: COLOR_EXPENSE,
    stroke: "var(--bg)",
    "stroke-width": "1.6",
    opacity: showExp ? "1" : "0",
  });

  // Floating tooltip inside SVG
  const tipW = 166;
  const tipH = 66;
  const tipRect = svgEl("rect", {
    x: "0",
    y: "0",
    width: tipW,
    height: tipH,
    fill: "var(--surface)",
    stroke: "var(--border-strong)",
    "stroke-width": "1",
  });
  const tipHead = svgEl("text", {
    x: "8",
    y: "15",
    fill: "var(--text)",
    "font-family": "var(--font-mono)",
    "font-size": "10",
    "font-weight": "800",
  });
  const tipInc = svgEl("text", {
    x: "8",
    y: "31",
    fill: COLOR_INCOME,
    "font-family": "var(--font-mono)",
    "font-size": "10",
    "font-weight": "700",
  });
  const tipExp = svgEl("text", {
    x: "8",
    y: "45",
    fill: COLOR_EXPENSE,
    "font-family": "var(--font-mono)",
    "font-size": "10",
    "font-weight": "700",
  });
  const tipSur = svgEl("text", {
    x: "8",
    y: "59",
    fill: COLOR_SAVINGS,
    "font-family": "var(--font-mono)",
    "font-size": "10",
    "font-weight": "700",
  });
  const tipGroup = svgEl("g", { style: "pointer-events: none;" }, [tipRect, tipHead, tipInc, tipExp, tipSur]);

  // Scrub bar readout + toggleable legend buttons
  const readoutSpan = el("span", { class: "tabular-nums" });
  const makeLegendToggle = (key, label, color) => {
    const active = !chartState.hiddenSeries.has(key);
    const btn = el(
      "button",
      {
        type: "button",
        class: "chip",
        style: {
          height: "22px",
          opacity: active ? "1" : "0.45",
          borderColor: active ? color : "var(--border)",
        },
        "aria-pressed": String(active),
      },
      [
        el("span", { class: "legend-swatch", style: { backgroundColor: color, width: "8px", height: "8px" } }),
        label,
      ]
    );
    btn.addEventListener("click", () => {
      if (chartState.hiddenSeries.has(key)) chartState.hiddenSeries.delete(key);
      else chartState.hiddenSeries.add(key);
      renderTrendChart(data);
    });
    return btn;
  };

  scrubBar.replaceChildren(
    readoutSpan,
    el("div", { class: "row-gap wrap", style: { gap: "5px" } }, [
      makeLegendToggle("income", "Income", COLOR_INCOME),
      makeLegendToggle("expense", "Expenses", COLOR_EXPENSE),
      makeLegendToggle("surplus", "Surplus", COLOR_SAVINGS),
    ])
  );

  const updateScrub = (idx) => {
    const safeIdx = Math.max(0, Math.min(pts.length - 1, idx));
    chartState.trendScrubIdx = safeIdx;
    const p = pts[safeIdx];
    const x = xFor(safeIdx);
    const yI = yFor(Number(p.income_cents || 0));
    const yE = yFor(Number(p.expense_cents || 0));

    crosshair.setAttribute("x1", x.toFixed(1));
    crosshair.setAttribute("x2", x.toFixed(1));
    dotInc.setAttribute("cx", x.toFixed(1));
    dotInc.setAttribute("cy", yI.toFixed(1));
    dotExp.setAttribute("cx", x.toFixed(1));
    dotExp.setAttribute("cy", yE.toFixed(1));

    const tx = x + 12 + tipW > W - padR ? x - tipW - 12 : x + 12;
    const ty = padT + 4;
    tipGroup.setAttribute("transform", `translate(${Math.max(padL + 2, tx).toFixed(1)}, ${ty.toFixed(1)})`);
    tipHead.textContent = `${fmtMonthShort(p.month)}${chartState.month === p.month ? " [FILTERED]" : ""}`;
    tipInc.textContent = `INC  ${fmtMoney(p.income_cents)}`;
    tipExp.textContent = `EXP  ${fmtMoney(p.expense_cents)}`;
    const surVal = Number(p.surplus_cents || 0);
    tipSur.textContent = `SUR  ${surVal >= 0 ? "+" : ""}${fmtMoney(surVal)}`;
    tipSur.setAttribute("fill", surVal >= 0 ? COLOR_SAVINGS : COLOR_DEFICIT);

    readoutSpan.replaceChildren(
      el("strong", { text: `${fmtMonthShort(p.month)}  ` }),
      el("span", { style: { color: COLOR_INCOME }, text: `In ${fmtMoneyCompact(p.income_cents)}` }),
      " · ",
      el("span", { style: { color: COLOR_EXPENSE }, text: `Out ${fmtMoneyCompact(p.expense_cents)}` }),
      " · ",
      el("span", {
        style: { color: surVal >= 0 ? COLOR_SAVINGS : COLOR_DEFICIT },
        text: `Surplus ${surVal >= 0 ? "+" : ""}${fmtMoneyCompact(surVal)}`,
      })
    );
  };

  // Click / keyboard-focusable month columns
  const hitCols = pts.map((p, i) => {
    const rect = svgEl("rect", {
      x: Math.max(padL, xFor(i) - colW / 2).toFixed(1),
      y: padT,
      width: Math.max(14, colW).toFixed(1),
      height: plotH,
      fill: "transparent",
      tabindex: "0",
      role: "button",
      style: "cursor: pointer;",
      "aria-label": `Filter analytics to ${fmtMonthShort(p.month)}: income ${fmtMoney(p.income_cents)}, expenses ${fmtMoney(p.expense_cents)}`,
    });
    const toggleMonth = () => {
      const nextMonth = chartState.month === p.month ? null : p.month;
      loadAnalyticsOverview({ month: nextMonth });
    };
    rect.addEventListener("pointerenter", () => updateScrub(i));
    rect.addEventListener("focus", () => updateScrub(i));
    rect.addEventListener("click", toggleMonth);
    rect.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        toggleMonth();
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        hitCols[Math.min(pts.length - 1, i + 1)].focus();
      } else if (e.key === "ArrowLeft") {
        e.preventDefault();
        hitCols[Math.max(0, i - 1)].focus();
      }
    });
    return rect;
  });

  const svg = svgEl(
    "svg",
    {
      viewBox: `0 0 ${W} ${H}`,
      role: "img",
      "aria-label": "Income, Expenses, and Surplus monthly trend chart",
    },
    [defs, ...gridNodes, ...bandNodes, ...seriesNodes, crosshair, dotInc, dotExp, tipGroup, ...hitCols]
  );

  svg.addEventListener("pointermove", (ev) => {
    const rect = svg.getBoundingClientRect();
    if (!rect.width) return;
    const relX = ((ev.clientX - rect.left) / rect.width) * W;
    const frac = Math.max(0, Math.min(1, (relX - padL) / Math.max(1, plotW)));
    const idx = Math.round(frac * (pts.length - 1));
    if (idx !== chartState.trendScrubIdx) updateScrub(idx);
  });

  wrap.replaceChildren(svg);
  updateScrub(initIdx);
}

/* ============================================================================
   Module Bootstrap
   ============================================================================ */

export function initCharts() {
  const periodSwitcher = $("analytics-period-switcher");
  if (periodSwitcher) {
    periodSwitcher.addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-analytics-period]");
      if (!btn) return;
      loadAnalyticsOverview({ period: btn.dataset.analyticsPeriod, keepMonth: false });
    });
  }

  const clearMonthBtn = $("btn-analytics-clear-month");
  if (clearMonthBtn) {
    clearMonthBtn.addEventListener("click", () => {
      loadAnalyticsOverview({ month: null });
    });
  }

  const refreshBtn = $("btn-analytics-refresh");
  if (refreshBtn) {
    refreshBtn.addEventListener("click", (e) =>
      withBusy(e.currentTarget, async () => {
        await loadAnalyticsOverview();
        if (!prefersReducedMotion()) {
          toast("Analytics refreshed.");
        }
      })
    );
  }

  registerTabHook("analytics", () => loadAnalyticsOverview());
  registerRefreshHook(() => {
    if (state.activeTab === "analytics") {
      return loadAnalyticsOverview();
    }
    return null;
  });
}
