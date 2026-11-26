/**
 * ChronoX Finance — Stocks & Crypto Portfolio + Credit & Debt Factor Analysis (ES Module)
 * Pure zero-CDN DOM/SVG implementation:
 * Part A — Portfolio:
 * - Total Investments, Cost Basis, Unrealized P&L (tabular-nums, cost-basis coverage aware)
 * - Sharp asset-class allocation bars (Equities, ETFs, Crypto, Cash)
 * - Holdings ledger with exact 10^-8 quantity & sub-cent price formatting (e.g. $0.00002415)
 * - Live Quote Refresh, Manual Add/Delete, Broker CSV Import
 * - Direct Read-Only Broker Adapters (Coinbase CDP, Robinhood Crypto, Charles Schwab OAuth2 + 7-day countdown, Plaid Investments)
 * Part B — Credit & Debt:
 * - Imported Actual Bureau Score card (never fabricated!)
 * - Credit Factor Analysis (FICO 8 + VantageScore 3.0 factor weights & utilization tiers)
 * - Chase 5/24 Velocity Gauge (all issuers + AU toggle + next slot drop-off date) + Inquiries (24m) & Derogatories (7y/10y)
 * - Official Credit Report Importer (structured form primary + AnnualCreditReport PDF mandatory review confirmation)
 * - Niche Advisor Alerts, AZEO Statement-Close Calendar, Debt Payoff Strategy Comparison, Credit Lines Manager & Balance Transfer Breakeven
 */

import {
  $,
  el,
  makeIcon,
  api,
  state,
  toast,
  withBusy,
  fmtMoney,
  fmtE8Price,
  fmtE8Qty,
  dollarsToCents,
  isoDateOffset,
  openOverlay,
  closeOverlay,
  switchTab,
  animateMoney,
  animatePercent,
  CRED_RING_CIRCUMFERENCE,
  refreshAll,
  registerTabHook,
  registerRefreshHook,
} from "./core.js";

const ASSET_CLASS_META = {
  equity: { label: "Equities", color: "#3b82f6" },
  etf: { label: "ETFs", color: "#06b6d4" },
  crypto: { label: "Crypto", color: "#f59e0b" },
  cash: { label: "Cash", color: "#10b981" },
};

const portState = {
  portfolio: null,
  connections: [],
  creditReport: null,
  creditApis: null,
  factorModel: "fico8", // "fico8" | "vantage3"
  pendingPdfBundle: null,
};

const CREDIT_API_HINTS = {
  spinwheel: "Equifax credit score & full liabilities profile via Bearer API Key + Spinwheel User ID.",
  array: "1B/3B Experian, Equifax & TransUnion JSON reports via Server Token + Report Key & Display Token.",
  crs: "Direct Experian, Equifax & TransUnion pulls via CRS Username/Password JWT login or Bearer Token.",
  custom: "Any ChronoX-compatible HTTPS or local loopback credit bridge returning structured score/report JSON.",
};

function normalizeDerogatoryKind(raw) {
  const k = String(raw || "").trim().toLowerCase();
  if (k === "late_payment" || k === "late") return "late_30";
  const valid = new Set([
    "late_30",
    "late_60",
    "late_90",
    "charge_off",
    "collection",
    "bankruptcy_ch7",
    "bankruptcy_ch13",
    "other",
  ]);
  return valid.has(k) ? k : "other";
}

function decimalStrToE8(raw) {
  const s = String(raw ?? "").trim().replace(/[$,\s]/g, "");
  if (!s) return 0;
  if (/^\d+(\.\d+)?$/.test(s)) {
    const [whole, frac = ""] = s.split(".");
    const padded = (frac + "00000000").slice(0, 8);
    return Number(whole) * 100000000 + Number(padded);
  }
  const num = Number(s);
  if (!Number.isFinite(num) || num < 0) {
    throw new Error("Enter a valid non-negative number.");
  }
  return Math.round(num * 1e8);
}

/* ============================================================================
   PART A: STOCKS & CRYPTO PORTFOLIO
   ============================================================================ */

export async function loadPortfolio() {
  if (state.vaultStatus !== "unlocked") return null;
  try {
    const [port, conns] = await Promise.all([
      api("/api/v1/portfolio"),
      api("/api/v1/connections").catch(() => []),
    ]);
    portState.portfolio = port;
    state.portfolio = port;
    portState.connections = Array.isArray(conns) ? conns : [];
    state.connections = portState.connections;
    renderPortfolio(port, portState.connections);
    return port;
  } catch (err) {
    const wrap = $("port-holdings-wrap");
    if (wrap) {
      wrap.replaceChildren(
        el("div", { class: "item-box muted small", text: err.message || "Could not load portfolio." })
      );
    }
    return null;
  }
}

function renderSchwabBanner(connections) {
  const banner = $("portfolio-schwab-banner");
  if (!banner) return;

  const schwab = (connections || []).find((c) => c.provider === "schwab");
  if (!schwab) {
    banner.classList.add("hidden");
    banner.replaceChildren();
    return;
  }

  let daysLeft = schwab.refresh_days_remaining;
  if (daysLeft == null && schwab.token_expires_at) {
    const expMs = Date.parse(schwab.token_expires_at);
    if (Number.isFinite(expMs)) {
      daysLeft = Math.max(0, Math.ceil((expMs - Date.now()) / 86400000));
    }
  }
  const needsReauth = schwab.status === "needs_reauth" || (daysLeft != null && daysLeft <= 2);
  const msg =
    daysLeft == null
      ? "Charles Schwab Trader API enforces a 7-day OAuth refresh token window. Re-authorize weekly to keep live positions synced."
      : daysLeft <= 0
      ? "Charles Schwab 7-day OAuth refresh token has EXPIRED — click Re-Authorize Schwab to resume position & quote sync."
      : `Charles Schwab OAuth refresh expires in ${daysLeft} day${daysLeft === 1 ? "" : "s"} (7-day Trader API policy).`;

  const reauthBtn = el(
    "button",
    { type: "button", class: `btn ${needsReauth ? "btn-primary" : "btn-soft"} btn-xs` },
    [makeIcon("key", "ui-icon-sm"), "Re-Authorize Schwab"]
  );
  reauthBtn.addEventListener("click", () => openBrokerModal("schwab"));

  banner.classList.remove("hidden");
  banner.style.borderLeftColor = needsReauth ? "var(--crimson)" : "var(--amber)";
  banner.style.borderTopColor = needsReauth ? "var(--crimson)" : "var(--amber)";
  banner.replaceChildren(
    el("div", { class: "rebal-row" }, [
      el("span", { class: "inline-icon-row small" }, [
        makeIcon("clock", "ui-icon-sm"),
        el("strong", { text: "SCHWAB 7-DAY OAUTH COUNTDOWN:" }),
        msg,
      ]),
      reauthBtn,
    ])
  );
}

function renderPortfolio(port, connections) {
  if (!port) return;
  renderSchwabBanner(connections);

  const totalVal = Number(port.total_value_cents || 0);
  const costBasis = Number(port.total_cost_basis_cents || 0);
  const pnl = Number(port.unrealized_pnl_cents || 0);
  const pnlPct = Number(port.unrealized_pnl_pct || 0);
  const holdings = Array.isArray(port.holdings) ? port.holdings : [];
  const unknownBasisVal = Number(port.unknown_cost_basis_value_cents || 0);
  const unknownBasisCnt = Number(port.unknown_cost_basis_holdings_count || 0);
  const knownBasisCnt = Number(port.known_cost_basis_holdings_count || 0);
  const knownBasisVal = Number(port.known_cost_basis_value_cents ?? (totalVal - unknownBasisVal));
  const coveragePct = Number(port.cost_basis_coverage_pct ?? 100);
  const hasAnyUnknownBasis = unknownBasisCnt > 0 || unknownBasisVal > 0;
  const allUnknownBasis = holdings.length > 0 && knownBasisCnt <= 0 && knownBasisVal <= 0 && hasAnyUnknownBasis;

  animateMoney($("port-total-value"), totalVal, "portTotal");
  const costBasisNode = $("port-cost-basis");
  if (allUnknownBasis && costBasisNode) {
    costBasisNode.textContent = "Unknown";
  } else {
    animateMoney(costBasisNode, costBasis, "portCost");
  }
  const cntNode = $("port-holdings-count");
  if (cntNode) {
    const covNote =
      hasAnyUnknownBasis ? ` · ${coveragePct.toFixed(0)}% basis known` : "";
    cntNode.textContent = `${holdings.length} holding${holdings.length === 1 ? "" : "s"}${covNote}`;
  }

  const pnlNode = $("port-unrealized-pnl");
  if (pnlNode) {
    if (allUnknownBasis) {
      pnlNode.textContent = "--";
      pnlNode.style.color = "var(--muted)";
    } else {
      pnlNode.textContent = `${pnl >= 0 ? "+" : ""}${fmtMoney(pnl)}`;
      pnlNode.style.color = pnl > 0 ? "var(--primary)" : pnl < 0 ? "var(--crimson)" : "var(--text)";
    }
  }
  const pctNode = $("port-unrealized-pct");
  if (pctNode) {
    if (allUnknownBasis) {
      pctNode.textContent = "BASIS UNKNOWN";
      pctNode.className = "pill pill-watch";
    } else {
      const suffix = hasAnyUnknownBasis ? " (known basis)" : "";
      pctNode.textContent = `${pnlPct >= 0 ? "+" : ""}${pnlPct.toFixed(2)}%${suffix}`;
      pctNode.className = `pill ${pnl > 0 ? "pill-calm" : pnl < 0 ? "pill-danger" : "pill-soft"}`;
    }
  }
  const syncNode = $("port-last-sync");
  if (syncNode) {
    const srcs = Array.isArray(port.quote_sources) && port.quote_sources.length
      ? port.quote_sources.join(" / ")
      : "Manual / Cached";
    syncNode.textContent = srcs;
  }

  renderPortfolioAllocation(port.allocations || [], totalVal);
  renderHoldingsTable(holdings);
  renderBrokerCards(connections);
}

function renderPortfolioAllocation(allocations, totalVal) {
  const wrap = $("port-allocation-wrap");
  if (!wrap) return;

  const byClass = new Map();
  for (const a of allocations || []) {
    byClass.set(a.asset_class, {
      asset_class: a.asset_class,
      value_cents: Number(a.value_cents || 0),
      pct: Number(a.pct || 0),
    });
  }

  const ordered = ["equity", "etf", "crypto", "cash"].map((cls) => {
    const found = byClass.get(cls);
    return found || { asset_class: cls, value_cents: 0, pct: 0 };
  });

  if (totalVal <= 0) {
    wrap.replaceChildren(
      el("div", { class: "muted small", text: "Add holdings manually, import a broker CSV, or connect a read-only adapter to inspect allocation." })
    );
    return;
  }

  const stackedBar = el(
    "div",
    { class: "alloc-stacked-bar", role: "img", "aria-label": "Asset class allocation bar" },
    ordered
      .filter((a) => a.pct > 0)
      .map((a) => {
        const meta = ASSET_CLASS_META[a.asset_class] || { label: a.asset_class, color: "#64748b" };
        return el("div", {
          class: "alloc-seg",
          style: { width: `${Math.max(1.5, a.pct).toFixed(2)}%`, backgroundColor: meta.color },
          title: `${meta.label}: ${fmtMoney(a.value_cents)} (${a.pct.toFixed(1)}%)`,
        });
      })
  );

  const breakdownRows = el(
    "div",
    { class: "four-col" },
    ordered.map((a) => {
      const meta = ASSET_CLASS_META[a.asset_class] || { label: a.asset_class, color: "#64748b" };
      return el("div", { class: "item-box", style: { borderLeft: `3px solid ${meta.color}` } }, [
        el("div", { class: "item-row" }, [
          el("span", { class: "eyebrow", style: { marginBottom: "0" }, text: meta.label }),
          el("span", { class: "pill pill-soft", text: `${a.pct.toFixed(1)}%` }),
        ]),
        el("strong", { class: "tabular-nums", text: fmtMoney(a.value_cents) }),
      ]);
    })
  );

  wrap.replaceChildren(stackedBar, breakdownRows);
}

function renderHoldingsTable(holdings) {
  const wrap = $("port-holdings-wrap");
  if (!wrap) return;

  if (!holdings.length) {
    wrap.replaceChildren(
      el("div", { class: "item-box muted small", style: { margin: "10px" } }, [
        el("span", { class: "inline-icon-row" }, [
          makeIcon("briefcase", "ui-icon-sm"),
          "No investment positions recorded yet. Click '+ Add Holding' or 'Import Broker CSV' above.",
        ]),
      ])
    );
    return;
  }

  const rows = holdings.map((h) => {
    const meta = ASSET_CLASS_META[h.asset_class] || { label: h.asset_class || "Other", color: "#64748b" };
    const basisKnown = h.cost_basis_known !== false;
    const pnl = Number(h.unrealized_pnl_cents || 0);
    const pnlPct = Number(h.unrealized_pnl_pct || 0);
    const acctSuffix = h.external_account_id ? ` · ${String(h.external_account_id).slice(-6)}` : "";
    const delBtn = el(
      "button",
      {
        type: "button",
        class: "btn btn-ghost btn-xs",
        title: `Delete ${h.symbol}`,
        "aria-label": `Delete holding ${h.symbol}`,
      },
      [makeIcon("trash", "ui-icon-sm")]
    );
    delBtn.addEventListener("click", () =>
      withBusy(delBtn, async () => {
        try {
          await api(`/api/v1/portfolio/holdings/${h.id}`, { method: "DELETE" });
          toast(`Removed ${h.symbol} from portfolio.`);
          await loadPortfolio();
          await refreshAll();
        } catch (err) {
          toast(err.message || "Could not delete holding.", true);
        }
      })
    );

    return el("tr", {}, [
      el("td", {}, [
        el("strong", { class: "tabular-nums", text: h.symbol }),
        h.name || acctSuffix ? el("span", { class: "tx-memo-sub", text: `${h.name || ""}${acctSuffix}`.trim() }) : null,
      ]),
      el("td", {}, [
        el("span", {
          class: "pill pill-soft",
          style: { borderLeft: `3px solid ${meta.color}` },
          text: meta.label,
        }),
      ]),
      el("td", { class: "num tabular-nums", text: fmtE8Qty(h.quantity_e8) }),
      el("td", { class: "num tabular-nums", text: fmtE8Price(h.price_e8) }),
      el("td", { class: "num tabular-nums", style: { fontWeight: "700" }, text: fmtMoney(h.market_value_cents) }),
      el("td", {
        class: "num tabular-nums muted",
        text: basisKnown ? fmtMoney(h.cost_basis_cents) : "Unknown",
      }),
      el("td", { class: "num tabular-nums" }, [
        basisKnown
          ? el("span", {
              style: { color: pnl > 0 ? "var(--primary)" : pnl < 0 ? "var(--crimson)" : "var(--muted)", fontWeight: "700" },
              text: `${pnl >= 0 ? "+" : ""}${fmtMoney(pnl)} (${pnlPct >= 0 ? "+" : ""}${pnlPct.toFixed(1)}%)`,
            })
          : el("span", { class: "muted small", text: "-- (No basis)" }),
      ]),
      el("td", {}, [el("span", { class: "pill pill-soft", text: h.source || "manual" })]),
      el("td", { class: "num" }, [delBtn]),
    ]);
  });

  const table = el("table", { class: "tx-table" }, [
    el("thead", {}, [
      el("tr", {}, [
        el("th", { text: "Symbol" }),
        el("th", { text: "Class" }),
        el("th", { class: "num", text: "Quantity" }),
        el("th", { class: "num", text: "Unit Price" }),
        el("th", { class: "num", text: "Market Value" }),
        el("th", { class: "num", text: "Cost Basis" }),
        el("th", { class: "num", text: "Unrealized P&L" }),
        el("th", { text: "Source" }),
        el("th", { class: "num", text: "" }),
      ]),
    ]),
    el("tbody", {}, rows),
  ]);

  wrap.replaceChildren(table);
}

function renderBrokerCards(connections) {
  const grid = $("port-brokers-grid");
  if (!grid) return;

  const findConn = (prov) => (connections || []).find((c) => c.provider === prov);
  const specs = [
    {
      key: "coinbase",
      title: "Coinbase CDP",
      tag: "READ-ONLY ENFORCED",
      desc: "Ed25519/ES256 120s JWT. Calls /key_permissions on connect and REFUSES keys with trade or transfer scopes.",
      conn: findConn("coinbase"),
    },
    {
      key: "robinhood",
      title: "Robinhood Crypto",
      tag: "OFFICIAL ED25519 API",
      desc: "Read-only signed requests to trading.robinhood.com for crypto holdings & quotes. Use Plaid/CSV for equities.",
      conn: findConn("robinhood"),
    },
    {
      key: "schwab",
      title: "Charles Schwab",
      tag: "BYOK OAUTH2 · 7D REFRESH",
      desc: "Official Trader API positions & live equity quotes. Requires weekly MFA re-consent per Schwab 7-day policy.",
      conn: findConn("schwab"),
    },
    {
      key: "plaid_csv",
      title: "Plaid Investments & CSV",
      tag: "EQUITIES / 401K / IRA",
      desc: "Sync brokerage equities via Plaid Hosted Link (/investments/holdings/get) or zero-3rd-party Broker CSV.",
      conn: findConn("plaid"),
    },
  ];

  grid.replaceChildren(
    ...specs.map((sp) => {
      const needsReauth = sp.conn && sp.conn.status === "needs_reauth";
      const statusBadge = sp.conn
        ? el("span", {
            class: `pill ${sp.conn.status === "active" ? "pill-calm" : needsReauth ? "pill-danger" : "pill-watch"}`,
            text:
              sp.conn.status === "active"
                ? "CONNECTED"
                : needsReauth
                ? "RECONNECT REQUIRED"
                : sp.conn.status.replace(/_/g, " ").toUpperCase(),
          })
        : el("span", { class: "pill pill-soft", text: sp.tag });

      const actions = [];
      if (sp.key === "plaid_csv") {
        const plaidBtn = el("button", { type: "button", class: "btn btn-soft btn-xs", text: "Open Plaid Link" });
        plaidBtn.addEventListener("click", () => switchTab("settings"));
        const csvBtn = el("button", { type: "button", class: "btn btn-primary btn-xs", text: "Import CSV" });
        csvBtn.addEventListener("click", () => {
          const f = $("port-csv-import-form");
          if (f) {
            f.classList.remove("hidden");
            f.scrollIntoView({ behavior: "smooth", block: "center" });
          }
        });
        actions.push(plaidBtn, csvBtn);
      } else {
        const connBtn = el("button", {
          type: "button",
          class: "btn btn-primary btn-xs",
          text: sp.conn ? "Configure / Re-Auth" : "Connect",
        });
        connBtn.addEventListener("click", () => openBrokerModal(sp.key));
        actions.push(connBtn);

        if (sp.conn) {
          const syncBtn = el(
            "button",
            { type: "button", class: "btn btn-soft btn-xs" },
            [makeIcon("refresh", "ui-icon-sm"), "Sync"]
          );
          syncBtn.addEventListener("click", () =>
            withBusy(syncBtn, async () => {
              try {
                await api(`/api/v1/brokers/${sp.conn.id}/sync`, { method: "POST" });
                toast(`Synced ${sp.title} holdings.`);
                await loadPortfolio();
                await refreshAll();
              } catch (err) {
                toast(err.message || `Could not sync ${sp.title}.`, true);
                await loadPortfolio();
              }
            })
          );
          actions.push(syncBtn);
        }
      }

      return el("article", { class: "track-card" }, [
        el("div", { class: "track-head" }, [statusBadge, el("h3", { text: sp.title })]),
        el("p", { class: "muted small", text: sp.desc }),
        sp.conn && sp.conn.last_error
          ? el("div", { class: "small error-text", text: sp.conn.last_error })
          : null,
        el("div", { class: "row-gap wrap" }, actions),
      ]);
    })
  );
}

function openBrokerModal(brokerKey) {
  const modal = $("portfolio-broker-modal");
  const title = $("port-broker-modal-title");
  const body = $("port-broker-modal-body");
  if (!modal || !title || !body) return;

  if (brokerKey === "coinbase") {
    title.textContent = "Connect Coinbase CDP (Read-Only Enforced)";
    const form = el("form", { class: "stack-sm" }, [
      el("div", { class: "notice-bar" }, [
        makeIcon("shield", "ui-icon-sm"),
        "ChronoX verifies /api/v3/brokerage/key_permissions and automatically REFUSES keys with trade or transfer access.",
      ]),
      el("label", { class: "field" }, [
        el("span", { text: "CDP API Key Name" }),
        el("input", { name: "api_key_name", type: "text", placeholder: "organizations/.../apiKeys/...", required: true }),
      ]),
      el("label", { class: "field" }, [
        el("span", { text: "Private Key PEM (sealed in secrets.enc)" }),
        el("textarea", { name: "private_key_pem", rows: "4", placeholder: "-----BEGIN EC PRIVATE KEY-----", required: true }),
      ]),
      el("label", { class: "field" }, [
        el("span", { text: "Connection Label" }),
        el("input", { name: "label", type: "text", value: "Coinbase" }),
      ]),
      el("button", { type: "submit", class: "btn btn-primary", text: "Verify Read-Only & Connect" }),
    ]);
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const btn = form.querySelector("button[type=submit]");
      await withBusy(btn, async () => {
        try {
          await api("/api/v1/brokers/coinbase/connect", {
            method: "POST",
            body: JSON.stringify({
              api_key_name: form.elements.api_key_name.value.trim(),
              private_key_pem: form.elements.private_key_pem.value.trim(),
              label: form.elements.label.value.trim() || "Coinbase",
            }),
          });
          closeOverlay(modal);
          toast("Coinbase read-only connection verified & sealed in vault.");
          await loadPortfolio();
        } catch (err) {
          toast(err.message || "Coinbase connection rejected.", true);
        }
      });
    });
    body.replaceChildren(form);
  } else if (brokerKey === "robinhood") {
    title.textContent = "Connect Robinhood Crypto (Read-Only Ed25519)";
    const form = el("form", { class: "stack-sm" }, [
      el("div", { class: "notice-bar" }, [
        makeIcon("shield", "ui-icon-sm"),
        "Uses official Robinhood Crypto API with Ed25519 request signing. Strictly read-only — zero order placement.",
      ]),
      el("label", { class: "field" }, [
        el("span", { text: "Robinhood Crypto API Key" }),
        el("input", { name: "api_key", type: "text", placeholder: "rh-api-...", required: true }),
      ]),
      el("label", { class: "field" }, [
        el("span", { text: "Ed25519 Private Key (Base64, sealed in secrets.enc)" }),
        el("input", { name: "private_key_b64", type: "password", autocomplete: "off", required: true }),
      ]),
      el("label", { class: "field" }, [
        el("span", { text: "Connection Label" }),
        el("input", { name: "label", type: "text", value: "Robinhood Crypto" }),
      ]),
      el("button", { type: "submit", class: "btn btn-primary", text: "Connect Read-Only Crypto" }),
    ]);
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const btn = form.querySelector("button[type=submit]");
      await withBusy(btn, async () => {
        try {
          await api("/api/v1/brokers/robinhood/connect", {
            method: "POST",
            body: JSON.stringify({
              api_key: form.elements.api_key.value.trim(),
              private_key_b64: form.elements.private_key_b64.value.trim(),
              label: form.elements.label.value.trim() || "Robinhood Crypto",
            }),
          });
          closeOverlay(modal);
          toast("Robinhood Crypto read-only adapter connected.");
          await loadPortfolio();
        } catch (err) {
          toast(err.message || "Robinhood Crypto connection failed.", true);
        }
      });
    });
    body.replaceChildren(form);
  } else if (brokerKey === "schwab") {
    title.textContent = "Connect Charles Schwab Trader API (BYOK OAuth2)";
    const statusOut = el("div", { class: "notice-bar hidden" });
    const form = el("form", { class: "stack-sm" }, [
      el("div", { class: "notice-bar notice-warn" }, [
        makeIcon("clock", "ui-icon-sm"),
        "Optional BYOK connector. ChronoX opens an ephemeral loopback HTTPS listener for your registered 127.0.0.1 callback, or you can paste the redirected URL below.",
      ]),
      el("label", { class: "field" }, [
        el("span", { text: "Schwab App Key (Client ID)" }),
        el("input", { name: "client_id", type: "text", required: true }),
      ]),
      el("label", { class: "field" }, [
        el("span", { text: "Schwab App Secret (sealed in secrets.enc)" }),
        el("input", { name: "client_secret", type: "password", autocomplete: "off", required: true }),
      ]),
      el("label", { class: "field" }, [
        el("span", { text: "Registered Callback URL" }),
        el("input", {
          name: "redirect_uri",
          type: "url",
          value: "https://127.0.0.1:8787/api/v1/brokers/schwab/callback",
          required: true,
        }),
      ]),
      el("label", { class: "field" }, [
        el("span", { text: "Connection Label" }),
        el("input", { name: "label", type: "text", value: "Charles Schwab" }),
      ]),
      el("button", { type: "submit", class: "btn btn-primary", text: "Generate OAuth2 Authorize URL" }),
    ]);

    const completeForm = el("form", { class: "subpanel stack-xs details-spacer" }, [
      el("span", { class: "eyebrow", text: "FALLBACK: PASTE REDIRECT URL OR CODE" }),
      el("p", {
        class: "muted small",
        text: "If your browser warns on the local self-signed HTTPS loopback certificate, copy the full redirected https://127.0.0.1:... URL from the address bar and paste it here:",
      }),
      el("input", {
        name: "callback_url_or_code",
        type: "text",
        placeholder: "https://127.0.0.1:8787/api/v1/brokers/schwab/callback?code=...&state=...",
        required: true,
      }),
      el("button", { type: "submit", class: "btn btn-soft btn-xs", text: "Complete Schwab Authorization" }),
    ]);

    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const btn = form.querySelector("button[type=submit]");
      await withBusy(btn, async () => {
        try {
          const res = await api("/api/v1/brokers/schwab/connect", {
            method: "POST",
            body: JSON.stringify({
              client_id: form.elements.client_id.value.trim(),
              client_secret: form.elements.client_secret.value.trim(),
              redirect_uri: form.elements.redirect_uri.value.trim(),
              label: form.elements.label.value.trim() || "Charles Schwab",
            }),
          });
          const authUrl = res && (res.authorize_url || res.authorization_url || res.auth_url);
          const listenerNote =
            res && (res.callback_listener_active || res.loopback_tls_listener)
              ? `Local HTTPS callback listener active at ${res.redirect_uri || "127.0.0.1"}.`
              : "Paste the redirected callback URL below after approving Schwab consent.";
          statusOut.classList.remove("hidden");
          if (authUrl) {
            statusOut.replaceChildren(
              el("div", { class: "stack-xs" }, [
                el("strong", { text: "Schwab OAuth2 Ready — Open in System Browser:" }),
                el("div", { class: "small muted", text: listenerNote }),
                el("a", {
                  href: authUrl,
                  target: "_blank",
                  rel: "noopener noreferrer",
                  class: "btn btn-soft btn-xs",
                  text: "Launch Schwab MFA Consent ->",
                }),
              ])
            );
          } else {
            statusOut.textContent = "Schwab credentials sealed. Complete OAuth callback to activate 7-day sync.";
          }
          toast("Schwab BYOK configuration saved.");
          await loadPortfolio();
        } catch (err) {
          toast(err.message || "Could not initialize Schwab OAuth.", true);
        }
      });
    });

    completeForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const btn = completeForm.querySelector("button[type=submit]");
      await withBusy(btn, async () => {
        try {
          await api("/api/v1/brokers/schwab/complete", {
            method: "POST",
            body: JSON.stringify({
              callback_url_or_code: completeForm.elements.callback_url_or_code.value.trim(),
              redirect_uri: form.elements.redirect_uri.value.trim() || null,
            }),
          });
          closeOverlay(modal);
          toast("Charles Schwab OAuth2 completed & positions ready to sync.");
          await loadPortfolio();
          await refreshAll();
        } catch (err) {
          toast(err.message || "Could not complete Schwab OAuth exchange.", true);
        }
      });
    });

    body.replaceChildren(form, statusOut, completeForm);
  }

  openOverlay(modal);
}

/* ============================================================================
   PART B: CREDIT & DEBT + FACTOR ANALYSIS + CHASE 5/24
   ============================================================================ */

export async function loadCreditReport() {
  if (state.vaultStatus !== "unlocked") return null;
  try {
    const extraInput = $("debt-extra-input");
    const extraDollars = Number(extraInput ? extraInput.value : 250) || 250;
    const extraCents = Math.max(0, Math.round(extraDollars * 100));
    const auToggle = $("credit-524-au-toggle");
    const includeAu = auToggle ? auToggle.checked : true;
    const rep = await api(
      `/api/v1/credit/report?extra_monthly_cents=${extraCents}&include_au=${includeAu ? "true" : "false"}`
    );
    portState.creditReport = rep;
    state.creditReport = rep;
    renderCreditReport(rep);
    await loadCreditApis();
    return rep;
  } catch (err) {
    toast(err.message || "Could not load credit report.", true);
    return null;
  }
}

function renderCreditReport(rep) {
  if (!rep) return;

  // 1. Top 4 KPIs
  const utilPct = Number(rep.aggregate_utilization_pct || 0);
  animatePercent($("cred-agg-util"), utilPct, "credAggUtil", 1);

  const clampedUtil = Math.max(0, Math.min(100, utilPct));
  const ringOffset = CRED_RING_CIRCUMFERENCE * (1 - clampedUtil / 100);
  const ringStroke =
    clampedUtil <= 8.9
      ? "var(--primary)"
      : clampedUtil <= 28.9
      ? "var(--cyan)"
      : clampedUtil <= 48.9
      ? "var(--amber)"
      : "var(--crimson)";
  const credRing = $("cred-ring-progress");
  if (credRing) {
    credRing.setAttribute("stroke", ringStroke);
    requestAnimationFrame(() => credRing.setAttribute("stroke-dashoffset", ringOffset.toFixed(2)));
  }

  const tierLabels = {
    zero_inactivity_penalty: ["0% All-Zero Penalty", "pill-watch"],
    zero: ["0% Util", "pill-soft"],
    optimal_800_tier: ["Optimal (<9%)", "pill-calm"],
    good: ["Good (<29%)", "pill-blue"],
    score_drag: ["Score Drag (29-48%)", "pill-watch"],
    high_drag: ["High Drag (49-88%)", "pill-danger"],
    maxed_severe: ["Maxed Cliff (89%+)", "pill-danger"],
  };
  const [tLabel, tCls] = tierLabels[rep.aggregate_tier] || [rep.aggregate_tier || "No cards yet", "pill-soft"];
  const tierEl = $("cred-agg-tier");
  if (tierEl) {
    tierEl.textContent = tLabel;
    tierEl.className = `pill ${tCls}`;
  }

  // Imported Actual Bureau Score (NEVER fabricated!)
  const reports = Array.isArray(rep.credit_reports) && rep.credit_reports.length
    ? rep.credit_reports
    : (rep.factor_analysis && rep.factor_analysis.latest_actual_scores) || [];
  const latestScore = reports.length ? reports[0] : null;
  const scoreEl = $("cred-bureau-score");
  const scoreMeta = $("cred-bureau-meta");
  if (scoreEl && scoreMeta) {
    if (latestScore && latestScore.score != null) {
      scoreEl.textContent = String(latestScore.score);
      scoreEl.style.color =
        latestScore.score >= 740 ? "var(--primary)" : latestScore.score >= 670 ? "var(--cyan)" : "var(--amber)";
      const modelLbl =
        latestScore.model === "vantage3"
          ? "VantageScore 3.0"
          : latestScore.model === "fico9"
          ? "FICO 9"
          : latestScore.model === "other"
          ? "Bureau Score"
          : "FICO 8";
      const srcTag =
        latestScore.source && String(latestScore.source).endsWith("_api")
          ? ` · ${String(latestScore.source).replace(/_/g, " ").toUpperCase()}`
          : "";
      scoreMeta.textContent = `${String(latestScore.bureau || "").toUpperCase()} · ${modelLbl} · ${latestScore.pulled_date}${srcTag}`;
    } else {
      scoreEl.textContent = "--";
      scoreEl.style.color = "var(--muted)";
      scoreMeta.textContent = "Import actual bureau score below (never fabricated)";
    }
  }

  animatePercent($("cred-dti"), Number(rep.dti_pct || 0), "credDti", 1);
  const dtiCls = rep.dti_pct <= 35 ? "pill-calm" : rep.dti_pct <= 43 ? "pill-watch" : "pill-danger";
  const dtiTierEl = $("cred-dti-tier");
  if (dtiTierEl) {
    dtiTierEl.textContent = String(rep.dti_tier || "healthy").replace(/_/g, " ");
    dtiTierEl.className = `pill ${dtiCls}`;
  }

  animateMoney($("cred-total-debt"), Number(rep.total_debt_cents || 0), "credTotalDebt");
  const burnEl = $("cred-interest-burn");
  if (burnEl) burnEl.textContent = `${fmtMoney(rep.monthly_interest_burn_cents || 0)}/mo interest burn`;

  // 2. Credit Factor Analysis
  renderFactorAnalysis(rep);

  // 3. Chase 5/24 Velocity + Inquiries + Derogatories
  renderVelocityAndDropOffs(rep);

  // 4. Alerts, AZEO, Debt Payoff Strategies, Credit Lines
  renderCreditAlerts(rep.alerts || []);
  renderAzeoCalendar(rep.azeo_plan || []);
  renderDebtStrategies(rep.strategies || []);
  renderCreditLines(rep.credit_lines || []);
}

function renderFactorAnalysis(rep) {
  const wrap = $("credit-factors-wrap");
  if (!wrap) return;

  const factorData = rep.factor_analysis || {};
  const factorSummaryNote =
    "Weighted factor breakdown from your tracked credit lines, inquiries, and history (not an official bureau score).";
  const factors =
    portState.factorModel === "vantage3"
      ? factorData.vantage3_factors || []
      : factorData.fico8_factors || [];

  const switcher = el("div", { class: "segmented segmented-sm", role: "group", "aria-label": "Scoring factor model" }, [
    el("button", {
      type: "button",
      class: `seg-btn${portState.factorModel === "fico8" ? " active" : ""}`,
      text: "FICO 8 Weights",
      "data-factor-model": "fico8",
    }),
    el("button", {
      type: "button",
      class: `seg-btn${portState.factorModel === "vantage3" ? " active" : ""}`,
      text: "VantageScore 3.0",
      "data-factor-model": "vantage3",
    }),
  ]);
  switcher.addEventListener("click", (ev) => {
    const btn = ev.target.closest("button[data-factor-model]");
    if (!btn) return;
    portState.factorModel = btn.dataset.factorModel;
    renderFactorAnalysis(rep);
  });

  const statusMeta = {
    optimal: { cls: "pill-calm", color: "var(--primary)", fill: 96 },
    good: { cls: "pill-blue", color: "var(--cyan)", fill: 76 },
    fair: { cls: "pill-watch", color: "var(--amber)", fill: 50 },
    drag: { cls: "pill-danger", color: "var(--crimson)", fill: 24 },
  };

  const factorRows = factors.length
    ? factors.map((f) => {
        const sm = statusMeta[f.status] || statusMeta.fair;
        return el("div", { class: "factor-row" }, [
          el("div", { class: "item-row" }, [
            el("span", { class: "row-gap", style: { gap: "6px" } }, [
              el("strong", { text: f.label }),
              el("span", { class: "pill pill-soft", text: `${f.weight_pct}% WEIGHT` }),
            ]),
            el("span", { class: "row-gap", style: { gap: "6px" } }, [
              f.guidance
                ? el("span", { class: "muted small tabular-nums", text: f.guidance })
                : null,
              el("span", { class: `pill ${sm.cls}`, text: String(f.status || "fair").toUpperCase() }),
            ]),
          ]),
          el("div", { class: "progress-track", style: { height: "5px" } }, [
            el("div", {
              class: "progress-fill",
              style: { width: `${sm.fill}%`, backgroundColor: sm.color },
            }),
          ]),
          el("div", { class: "small muted", text: f.summary }),
        ]);
      })
    : [el("p", { class: "small muted", text: "Add credit cards or bureau data to populate the 5-factor breakdown." })];

  wrap.replaceChildren(
    el("div", { class: "item-row" }, [
      el("span", { class: "muted small", text: factorSummaryNote }),
      switcher,
    ]),
    ...factorRows
  );
}

function renderVelocityAndDropOffs(rep) {
  const wrap524 = $("credit-524-wrap");
  const inqList = $("credit-inquiries-list");
  const derogList = $("credit-derogatories-list");

  const f24 = rep.five_twenty_four || {
    count_5_24: 0,
    under_5_24: true,
    include_authorized_user: true,
    counted_accounts: [],
    next_slot_drop_date: null,
    inquiries_6m: 0,
    inquiries_12m: 0,
    velocity_warnings: [],
  };

  if (wrap524) {
    const count = Number(f24.count_5_24 || 0);
    const under = count < 5;
    const slots = [];
    for (let i = 0; i < 5; i++) {
      const filled = i < count;
      const color = !filled
        ? "var(--surface-3)"
        : count >= 5
        ? "var(--crimson)"
        : count === 4
        ? "var(--amber)"
        : "var(--cyan)";
      slots.push(
        el("div", {
          style: {
            flex: "1",
            height: "12px",
            backgroundColor: color,
            border: "1px solid var(--border)",
          },
          title: `Slot ${i + 1}/5`,
        })
      );
    }

    const countedRows = (f24.counted_accounts || []).map((a) =>
      el("div", { class: "item-row small" }, [
        el("span", { text: `${a.name || "Card"}${a.is_authorized_user ? " (AU)" : ""}` }),
        el("span", {
          class: "muted tabular-nums",
          text: `Opened ${a.opened_date || "--"}${a.drops_off ? ` -> drops ${a.drops_off}` : ""}`,
        }),
      ])
    );

    const warnNodes = (f24.velocity_warnings || []).map((w) =>
      el("div", { class: "notice-bar notice-warn small" }, [makeIcon("alert", "ui-icon-sm"), w])
    );

    wrap524.replaceChildren(
      el("div", { class: "item-row" }, [
        el("span", {
          class: "metric-md tabular-nums",
          style: { color: under ? "var(--primary)" : "var(--crimson)" },
          text: `${count} / 24`,
        }),
        el("span", { class: "row-gap wrap", style: { gap: "6px" } }, [
          el("span", {
            class: `pill ${under ? "pill-calm" : "pill-danger"}`,
            text: under ? "UNDER 5/24" : "AT / OVER 5/24",
          }),
          el("span", { class: "pill pill-soft", text: `6m inq: ${f24.inquiries_6m || 0}` }),
          el("span", { class: "pill pill-soft", text: `12m inq: ${f24.inquiries_12m || 0}` }),
        ]),
      ]),
      el("div", { class: "row-gap", style: { gap: "4px" } }, slots),
      el("div", { class: "muted small tabular-nums" }, [
        f24.next_slot_drop_date
          ? `Next 5/24 slot drops off: ${f24.next_slot_drop_date}`
          : "No recent revolving accounts opened in the trailing 24 months.",
      ]),
      ...warnNodes,
      ...countedRows
    );
  }

  // Hard Inquiries
  if (inqList) {
    const inqs = Array.isArray(rep.credit_inquiries) ? rep.credit_inquiries : [];
    if (!inqs.length) {
      inqList.replaceChildren(el("p", { class: "small muted", text: "No hard inquiries logged." }));
    } else {
      inqList.replaceChildren(
        ...inqs.map((inq) => {
          const delBtn = el(
            "button",
            { type: "button", class: "btn btn-ghost btn-xs", "aria-label": `Delete inquiry ${inq.lender}` },
            [makeIcon("x", "ui-icon-sm")]
          );
          delBtn.addEventListener("click", () =>
            withBusy(delBtn, async () => {
              await api(`/api/v1/credit/inquiries/${inq.id}`, { method: "DELETE" });
              toast("Removed inquiry.");
              await loadCreditReport();
            })
          );
          return el("div", { class: "item-box" }, [
            el("div", { class: "item-row" }, [
              el("span", { class: "row-gap", style: { gap: "6px" } }, [
                el("strong", { text: inq.lender }),
                el("span", { class: "pill pill-soft", text: String(inq.bureau || "").toUpperCase() }),
                inq.is_scoring_active
                  ? el("span", { class: "pill pill-watch", text: "SCORING (<12M)" })
                  : el("span", { class: "pill pill-calm", text: "NON-SCORING (>12M)" }),
              ]),
              delBtn,
            ]),
            el("div", {
              class: "muted small tabular-nums",
              text: `Pulled ${inq.inquiry_date} · Unscored ${inq.counts_until} · Drops off ${inq.drops_off}`,
            }),
          ]);
        })
      );
    }
  }

  // Derogatories
  if (derogList) {
    const derogs = Array.isArray(rep.credit_derogatories) ? rep.credit_derogatories : [];
    if (!derogs.length) {
      derogList.replaceChildren(
        el("p", { class: "small muted inline-icon-row" }, [
          makeIcon("check", "ui-icon-sm"),
          "Zero derogatory marks logged.",
        ])
      );
    } else {
      derogList.replaceChildren(
        ...derogs.map((d) => {
          const delBtn = el(
            "button",
            { type: "button", class: "btn btn-ghost btn-xs", "aria-label": `Delete derogatory ${d.kind}` },
            [makeIcon("x", "ui-icon-sm")]
          );
          delBtn.addEventListener("click", () =>
            withBusy(delBtn, async () => {
              await api(`/api/v1/credit/derogatories/${d.id}`, { method: "DELETE" });
              toast("Removed derogatory item.");
              await loadCreditReport();
            })
          );
          const spanLabel = d.kind === "bankruptcy_ch7" ? "+10Y" : "+7Y";
          return el("div", { class: "item-box" }, [
            el("div", { class: "item-row" }, [
              el("span", { class: "row-gap", style: { gap: "6px" } }, [
                el("span", { class: "pill pill-danger", text: `${String(d.kind).replace(/_/g, " ").toUpperCase()} (${spanLabel})` }),
                d.lender ? el("strong", { text: d.lender }) : null,
              ]),
              delBtn,
            ]),
            el("div", {
              class: "muted small tabular-nums",
              text: `Opened ${d.opened_date} · Drops off ${d.drops_off}${d.notes ? ` · ${d.notes}` : ""}`,
            }),
          ]);
        })
      );
    }
  }
}

function renderPdfReviewBox(bundle) {
  const box = $("credit-pdf-review-box");
  if (!box) return;
  portState.pendingPdfBundle = bundle;
  box.classList.remove("hidden");

  const reports = Array.isArray(bundle.reports) ? bundle.reports : [];
  const inquiries = Array.isArray(bundle.inquiries) ? bundle.inquiries : [];
  const derogatories = Array.isArray(bundle.derogatories) ? bundle.derogatories : [];
  const warnings = Array.isArray(bundle.warnings) ? bundle.warnings : [];

  const checkRefs = { reports: [], inquiries: [], derogatories: [] };

  const confirmBtn = el(
    "button",
    { type: "button", class: "btn btn-primary btn-sm" },
    [makeIcon("check", "ui-icon-sm"), "Confirm & Import Checked Items"]
  );
  const cancelBtn = el("button", { type: "button", class: "btn btn-ghost btn-sm", text: "Discard Preview" });
  cancelBtn.addEventListener("click", () => {
    portState.pendingPdfBundle = null;
    box.classList.add("hidden");
    box.replaceChildren();
  });

  confirmBtn.addEventListener("click", () =>
    withBusy(confirmBtn, async () => {
      let savedCount = 0;
      try {
        for (const item of checkRefs.reports) {
          if (!item.cb.checked) continue;
          await api("/api/v1/credit/reports", {
            method: "POST",
            body: JSON.stringify({
              bureau: item.data.bureau || "experian",
              model: item.data.model || "fico8",
              score: Number(item.data.score),
              pulled_date: item.data.pulled_date || isoDateOffset(0),
              source: "annualcreditreport_pdf_reviewed",
            }),
          });
          savedCount += 1;
        }
        for (const item of checkRefs.inquiries) {
          if (!item.cb.checked) continue;
          await api("/api/v1/credit/inquiries", {
            method: "POST",
            body: JSON.stringify({
              lender: item.data.lender || "Unknown Lender",
              bureau: item.data.bureau || "experian",
              inquiry_date: item.data.inquiry_date || isoDateOffset(0),
            }),
          });
          savedCount += 1;
        }
        for (const item of checkRefs.derogatories) {
          if (!item.cb.checked) continue;
          await api("/api/v1/credit/derogatories", {
            method: "POST",
            body: JSON.stringify({
              kind: normalizeDerogatoryKind(item.data.kind),
              lender: item.data.lender || item.data.creditor || "",
              opened_date: item.data.opened_date || isoDateOffset(0),
              notes: item.data.notes || "Reviewed from PDF import",
            }),
          });
          savedCount += 1;
        }
        portState.pendingPdfBundle = null;
        box.classList.add("hidden");
        box.replaceChildren();
        toast(`Committed ${savedCount} reviewed credit item(s).`);
        await loadCreditReport();
      } catch (err) {
        toast(err.message || "Error committing reviewed credit items.", true);
      }
    })
  );

  const sectionNodes = [
    el("div", { class: "notice-bar notice-warn" }, [
      makeIcon("alert", "ui-icon-sm"),
      "MANDATORY PDF REVIEW: Bureau layouts vary — verify every extracted row below before committing to your ledger.",
    ]),
  ];

  for (const w of warnings) {
    sectionNodes.push(el("div", { class: "small muted", text: `Note: ${w}` }));
  }

  const makeRow = (bucket, data, labelText) => {
    const cb = el("input", { type: "checkbox", checked: true });
    checkRefs[bucket].push({ cb, data });
    return el("label", { class: "checkbox-row item-box", style: { flexDirection: "row" } }, [
      cb,
      el("span", { class: "small tabular-nums", text: labelText }),
    ]);
  };

  if (reports.length) {
    sectionNodes.push(el("h4", { text: `Extracted Scores (${reports.length})` }));
    reports.forEach((r) =>
      sectionNodes.push(makeRow("reports", r, `${String(r.bureau).toUpperCase()} ${r.model}: ${r.score} (${r.pulled_date})`))
    );
  }
  if (inquiries.length) {
    sectionNodes.push(el("h4", { text: `Extracted Hard Inquiries (${inquiries.length})` }));
    inquiries.forEach((i) =>
      sectionNodes.push(makeRow("inquiries", i, `${i.lender} (${String(i.bureau).toUpperCase()}) on ${i.inquiry_date}`))
    );
  }
  if (derogatories.length) {
    sectionNodes.push(el("h4", { text: `Extracted Derogatories (${derogatories.length})` }));
    derogatories.forEach((d) =>
      sectionNodes.push(makeRow("derogatories", d, `${d.kind} · ${d.lender || "Account"} · opened ${d.opened_date}`))
    );
  }
  if (!reports.length && !inquiries.length && !derogatories.length) {
    sectionNodes.push(
      el("p", {
        class: "small muted",
        text: "No structured scores, inquiries, or derogatories could be reliably parsed from this PDF. Use the structured forms above to log them cleanly.",
      })
    );
  }

  sectionNodes.push(el("div", { class: "row-gap details-spacer" }, [confirmBtn, cancelBtn]));
  box.replaceChildren(el("div", { class: "stack-xs" }, sectionNodes));
}

function renderCreditAlerts(alerts) {
  const alertBox = $("credit-alerts-list");
  if (!alertBox) return;
  if (!alerts.length) {
    alertBox.replaceChildren(
      el("p", { class: "small muted inline-icon-row" }, [
        makeIcon("check", "ui-icon-sm"),
        "Zero credit red flags — utilization, grace periods, and autopay look clean.",
      ])
    );
    return;
  }
  alertBox.replaceChildren(
    ...alerts.map((a) => {
      const sevCls = a.severity === "high" ? "pill-danger" : a.severity === "medium" ? "pill-watch" : "pill-blue";
      let quickBtn = null;
      if (a.category === "autopay_shield" && a.credit_line_id) {
        quickBtn = el(
          "button",
          { type: "button", class: "btn btn-primary btn-xs" },
          [makeIcon("check", "ui-icon-sm"), "Mark Autopay Enabled"]
        );
        quickBtn.addEventListener("click", () =>
          withBusy(quickBtn, async () => {
            await api(`/api/v1/credit/lines/${a.credit_line_id}`, {
              method: "PATCH",
              body: JSON.stringify({ autopay_mode: "minimum" }),
            });
            toast("Autopay shield recorded.");
            await loadCreditReport();
          })
        );
      }
      return el("div", { class: "item-box" }, [
        el("div", { class: "item-row" }, [
          el("strong", { text: a.title }),
          el("span", { class: `pill ${sevCls}`, text: a.severity }),
        ]),
        el("div", { class: "small", text: a.detail }),
        el("div", { class: "micro-step", text: `-> ${a.micro_step}` }),
        quickBtn ? el("div", { class: "row-gap" }, [quickBtn]) : null,
      ]);
    })
  );
}

function renderAzeoCalendar(azeoPlan) {
  const azeoBox = $("credit-azeo-list");
  if (!azeoBox) return;
  if (!azeoPlan.length) {
    azeoBox.replaceChildren(
      el("p", { class: "small muted", text: "Add a credit card with a credit limit below to compute exact AZEO statement-close dates." })
    );
    return;
  }
  azeoBox.replaceChildren(
    ...azeoPlan.map((a) => {
      const roleBadge =
        a.role === "already_optimal"
          ? el("span", { class: "pill pill-calm", text: "OPTIMAL" })
          : a.role === "primary_reporting_card"
          ? el("span", { class: "pill pill-blue", text: "PRIMARY 1-4.9%" })
          : el("span", { class: "pill pill-watch", text: "ZERO BEFORE CLOSE" });

      let payBtn = null;
      if (a.pay_before_close_cents > 0) {
        payBtn = el(
          "button",
          { type: "button", class: "btn btn-soft btn-xs" },
          [makeIcon("check", "ui-icon-sm"), `Record ${fmtMoney(a.pay_before_close_cents)} Paydown`]
        );
        payBtn.addEventListener("click", () =>
          withBusy(payBtn, async () => {
            const nextBal = Math.max(0, Number(a.current_balance_cents || 0) - Number(a.pay_before_close_cents || 0));
            await api(`/api/v1/credit/lines/${a.credit_line_id}`, {
              method: "PATCH",
              body: JSON.stringify({ balance_cents: nextBal }),
            });
            toast(`Recorded AZEO paydown on ${a.card_name}.`);
            await loadCreditReport();
            await refreshAll();
          })
        );
      }

      return el("div", { class: "item-box" }, [
        el("div", { class: "item-row" }, [el("strong", { text: a.card_name }), roleBadge]),
        el("div", { class: "small", text: a.explanation }),
        el("div", {
          class: "muted small tabular-nums",
          text: `Pay by ${a.pay_by_date} · Closes ${a.statement_close_date} -> reports ${Number(a.resulting_util_pct || 0).toFixed(1)}%`,
        }),
        payBtn ? el("div", { class: "row-gap" }, [payBtn]) : null,
      ]);
    })
  );
}

function renderDebtStrategies(strategies) {
  const stratGrid = $("debt-strategies-grid");
  if (!stratGrid) return;
  if (!strategies.length) {
    stratGrid.replaceChildren(
      el("div", { class: "item-box muted inline-icon-row" }, [
        makeIcon("trophy", "ui-icon-sm"),
        "Zero active debt balances.",
      ])
    );
    return;
  }
  stratGrid.replaceChildren(
    ...strategies.map((st) =>
      el(
        "article",
        {
          class: "jar-card",
          style: st.recommended_for_adhd ? { borderTopColor: "var(--primary)", borderColor: "var(--primary-border)" } : {},
        },
        [
          el("div", { class: "jar-top" }, [
            el("strong", { text: st.label }),
            st.recommended_for_adhd
              ? el("span", { class: "pill pill-calm" }, [makeIcon("star", "ui-icon-sm"), "RECOMMENDED"])
              : null,
          ]),
          el("div", { class: "small muted", text: st.tagline }),
          el("div", { class: "jar-avail", text: `${st.months_to_debt_free} mo` }),
          el("div", {
            class: "small tabular-nums",
            text: `Debt-free ${st.debt_free_date} · Interest ${fmtMoney(st.total_interest_cents)}`,
          }),
          el("div", {
            class: "micro-step",
            text: `Saves ${fmtMoney(st.interest_saved_vs_minimums_cents)} vs minimums · First $0 win in ${st.first_win_months} mo (${st.first_target_name})`,
          }),
          el(
            "ul",
            { class: "stack-xs small", style: { margin: "4px 0", paddingLeft: "18px" } },
            (st.month_1_actions || []).map((m) => el("li", { text: m }))
          ),
        ]
      )
    )
  );
}

function renderCreditLines(lines) {
  const clBox = $("credit-lines-list");
  if (!clBox) return;
  if (!lines.length) {
    clBox.replaceChildren(el("p", { class: "small muted", text: "No cards or loans tracked yet. Add one below." }));
    return;
  }

  clBox.replaceChildren(
    ...lines.map((cl) => {
      const utilStr =
        cl.utilization_pct != null
          ? `${cl.utilization_pct.toFixed(1)}% of ${fmtMoney(cl.credit_limit_cents)}`
          : String(cl.kind || "").replace(/_/g, " ");
      const pills = [
        el("span", { class: "pill pill-soft", text: `${(Number(cl.effective_apr_bps || 0) / 100).toFixed(2)}% APR` }),
        el("span", {
          class: `pill ${cl.autopay_mode === "none" ? "pill-danger" : "pill-calm"}`,
          text: `Autopay: ${cl.autopay_mode}`,
        }),
      ];
      if (cl.is_oldest_account) pills.push(el("span", { class: "pill pill-blue", text: "OLDEST ANCHOR" }));
      if (cl.is_authorized_user) pills.push(el("span", { class: "pill pill-watch", text: "AU CARD" }));
      if (cl.promo_days_remaining != null && cl.promo_days_remaining >= 0) {
        pills.push(el("span", { class: "pill pill-watch", text: `0% ends ${cl.promo_days_remaining}d` }));
      }

      let payBtn = null;
      if (cl.balance_cents > 0) {
        const payAmt = Math.min(10000, cl.balance_cents);
        payBtn = el("button", { type: "button", class: "btn btn-win btn-xs", text: `-${fmtMoney(payAmt)} Pay` });
        payBtn.addEventListener("click", () =>
          withBusy(payBtn, async () => {
            await api(`/api/v1/credit/lines/${cl.id}`, {
              method: "PATCH",
              body: JSON.stringify({ balance_cents: Math.max(0, cl.balance_cents - payAmt) }),
            });
            toast(`Recorded ${fmtMoney(payAmt)} paydown on ${cl.name}.`);
            await loadCreditReport();
            await refreshAll();
          })
        );
      }

      const delBtn = el(
        "button",
        { type: "button", class: "btn btn-ghost btn-xs", "aria-label": `Remove ${cl.name}` },
        [makeIcon("x", "ui-icon-sm")]
      );
      delBtn.addEventListener("click", () =>
        withBusy(delBtn, async () => {
          await api(`/api/v1/credit/lines/${cl.id}`, { method: "DELETE" });
          toast(`Removed ${cl.name}.`);
          await loadCreditReport();
          await refreshAll();
        })
      );

      return el("div", { class: "item-box" }, [
        el("div", { class: "item-row" }, [
          el("div", {}, [
            el("strong", { text: cl.name }),
            el("div", {
              class: "muted small tabular-nums",
              text: `${utilStr} · Closes ${cl.next_statement_close_date} · Due ${cl.next_due_date}`,
            }),
          ]),
          el("strong", { class: "num tabular-nums", text: fmtMoney(cl.balance_cents) }),
        ]),
        el("div", { class: "item-row" }, [
          el("div", { class: "row-gap wrap" }, pills),
          el("div", { class: "row-gap" }, [payBtn, delBtn]),
        ]),
      ]);
    })
  );
}

/* ============================================================================
   PART C: OPTIONAL BYOK CREDIT SCORE APIS (Spinwheel / Array / CRS / Custom)
   ============================================================================ */

function setCreditApiStatus(message, isError = false) {
  const box = $("credit-api-status-box");
  if (!box) return;
  if (!message) {
    box.classList.add("hidden");
    box.replaceChildren();
    return;
  }
  box.className = `notice-bar${isError ? " notice-warn" : ""}`;
  box.replaceChildren(
    makeIcon(isError ? "alert" : "check", "ui-icon-sm"),
    el("span", { text: message })
  );
}

function updateCreditApiFormFields() {
  const vendorSel = $("capi-vendor");
  if (!vendorSel) return;
  const v = vendorSel.value || "spinwheel";
  const hint = $("capi-vendor-hint");
  if (hint) {
    hint.textContent = CREDIT_API_HINTS[v] || "";
  }
  const fUser = $("capi-field-userid");
  const fArray = $("capi-field-array");
  const fCrs = $("capi-field-crs");
  const fUrl = $("capi-field-baseurl");
  const fKey = $("capi-field-apikey");
  if (fKey) fKey.classList.remove("hidden");
  if (fUser) fUser.classList.toggle("hidden", !(v === "spinwheel" || v === "array"));
  if (fArray) fArray.classList.toggle("hidden", v !== "array");
  if (fCrs) fCrs.classList.toggle("hidden", v !== "crs");
  if (fUrl) fUrl.classList.toggle("hidden", v !== "custom");
}

export async function loadCreditApis() {
  if (state.vaultStatus !== "unlocked") return null;
  try {
    const data = await api("/api/v1/credit/apis");
    portState.creditApis = data;
    renderCreditApis(data);
    return data;
  } catch (err) {
    const list = $("credit-apis-list");
    if (list && err.status !== 401) {
      list.replaceChildren(el("p", { class: "small muted", text: err.message || "Could not load Credit Score APIs." }));
    }
    return null;
  }
}

function renderCreditApis(data) {
  const list = $("credit-apis-list");
  const countPill = $("credit-apis-count");
  if (!list) return;
  const conns = Array.isArray(data && data.connections) ? data.connections : [];
  if (countPill) {
    countPill.textContent = `${conns.length} connected`;
    countPill.className = `pill ${conns.length > 0 ? "pill-calm" : "pill-soft"}`;
  }

  if (!conns.length) {
    const plaidNote =
      data && data.plaid_liabilities_connected
        ? "Plaid Liabilities is active for cards/loans. Add an optional Credit Score API below to pull live bureau scores automatically."
        : "No Credit Score API connected yet (optional). Connect Spinwheel, Array, CRS Credit API, or a Custom credit bridge below, or keep using manual / PDF bureau imports above.";
    list.replaceChildren(el("p", { class: "small muted", text: plaidNote }));
    return;
  }

  list.replaceChildren(
    ...conns.map((c) => {
      const needsReauth = c.status === "needs_reauth";
      const statusCls =
        c.status === "active"
          ? "pill-calm"
          : c.status === "paused_locked"
          ? "pill-watch"
          : "pill-danger";
      const statusLabel =
        c.status === "paused_locked"
          ? "VAULT LOCKED"
          : needsReauth
          ? "RECONNECT REQUIRED"
          : String(c.status || "active").replace(/_/g, " ").toUpperCase();

      const testBtn = el("button", { type: "button", class: "btn btn-ghost btn-xs", text: "Test" });
      testBtn.addEventListener("click", () =>
        withBusy(testBtn, async () => {
          try {
            const res = await api(`/api/v1/credit/apis/${c.id}/test`, { method: "POST" });
            setCreditApiStatus(res.message || `${c.institution} connection verified.`);
            toast(res.message || "Credit Score API verified.");
            await loadCreditApis();
          } catch (err) {
            setCreditApiStatus(err.message || "Connection test failed.", true);
            toast(err.message || "Credit Score API test failed.", true);
            await loadCreditApis();
          }
        })
      );

      const syncBtn = el("button", {
        type: "button",
        class: "btn btn-primary btn-xs",
        text: "Sync Score & Tradelines",
      });
      syncBtn.addEventListener("click", () =>
        withBusy(syncBtn, async () => {
          try {
            const res = await api(`/api/v1/credit/apis/${c.id}/sync`, {
              method: "POST",
              body: JSON.stringify({ sync_tradelines: true }),
            });
            const sc = (res && res.synced_counts) || {};
            const summary =
              `Synced ${c.institution}: ${sc.reports_added || 0} score(s), ` +
              `${(sc.tradelines_added || 0) + (sc.tradelines_updated || 0)} tradeline(s), ` +
              `${sc.inquiries_added || 0} inquiry(s), ${sc.derogatories_added || 0} derogatory(s).`;
            setCreditApiStatus(summary);
            toast(summary);
            await loadCreditReport();
            await refreshAll();
          } catch (err) {
            setCreditApiStatus(err.message || "Credit Score API sync failed.", true);
            toast(err.message || "Credit Score API sync failed.", true);
            await loadCreditApis();
          }
        })
      );

      const delBtn = el(
        "button",
        { type: "button", class: "btn btn-ghost btn-xs", "aria-label": `Disconnect ${c.institution}` },
        [makeIcon("x", "ui-icon-sm")]
      );
      delBtn.addEventListener("click", () =>
        withBusy(delBtn, async () => {
          try {
            await api(`/api/v1/credit/apis/${c.id}`, { method: "DELETE" });
            setCreditApiStatus(`Disconnected ${c.institution} and purged sealed credentials.`);
            toast(`Disconnected ${c.institution}.`);
            await loadCreditApis();
          } catch (err) {
            toast(err.message || "Could not disconnect Credit Score API.", true);
          }
        })
      );

      const metaParts = [`Vendor: ${String(c.vendor || "").toUpperCase()}`];
      if (c.env && c.env !== "production") metaParts.push("TEST ENV (ISOLATED)");
      if (c.api_key_masked) metaParts.push(`Key: ${c.api_key_masked}`);
      if (c.user_id) metaParts.push(`User: ${c.user_id}`);
      if (c.vendor === "crs" && c.bureau) metaParts.push(`Bureau: ${String(c.bureau).toUpperCase()}`);
      if (c.api_base_url) metaParts.push(c.api_base_url);
      metaParts.push(
        `Last sync: ${c.last_synced_at ? String(c.last_synced_at).replace("T", " ").slice(0, 16) : "Never"}`
      );

      return el("div", { class: "item-box" }, [
        el("div", { class: "item-row wrap" }, [
          el("div", {}, [
            el("div", { class: "row-gap wrap", style: { gap: "6px" } }, [
              el("strong", { text: c.institution }),
              el("span", { class: "pill pill-blue", text: String(c.vendor || "").toUpperCase() }),
              el("span", { class: `pill ${statusCls}`, text: statusLabel }),
            ]),
            el("div", { class: "muted small tabular-nums", text: metaParts.join(" · ") }),
            c.last_error ? el("div", { class: "small error-text", text: c.last_error }) : null,
          ]),
          el("div", { class: "row-gap wrap" }, [testBtn, syncBtn, delBtn]),
        ]),
      ]);
    })
  );
}

/* ============================================================================
   Module Bootstrap
   ============================================================================ */

export function initPortfolioAndCredit() {
  // --- Portfolio Handlers ---
  const refreshQuotesBtn = $("btn-port-refresh-quotes");
  if (refreshQuotesBtn) {
    refreshQuotesBtn.addEventListener("click", (e) =>
      withBusy(e.currentTarget, async () => {
        try {
          const res = await api("/api/v1/portfolio/refresh-quotes", { method: "POST" });
          const updatedCount = Number((res && (res.updated_count ?? res.refreshed_count)) || 0);
          toast(`Refreshed live market quotes (${updatedCount} updated).`);
          await loadPortfolio();
          await refreshAll();
        } catch (err) {
          toast(err.message || "Quote refresh failed.", true);
        }
      })
    );
  }

  const toggleAddBtn = $("btn-port-add-holding-toggle");
  const addForm = $("port-add-holding-form");
  if (toggleAddBtn && addForm) {
    toggleAddBtn.addEventListener("click", () => {
      addForm.classList.toggle("hidden");
      if (!addForm.classList.contains("hidden") && $("ph-symbol")) $("ph-symbol").focus();
    });
  }

  const toggleCsvBtn = $("btn-port-csv-toggle");
  const csvForm = $("port-csv-import-form");
  if (toggleCsvBtn && csvForm) {
    toggleCsvBtn.addEventListener("click", () => csvForm.classList.toggle("hidden"));
  }

  if (addForm) {
    addForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const btn = addForm.querySelector("button[type=submit]");
      await withBusy(btn, async () => {
        try {
          const symbol = $("ph-symbol").value.trim().toUpperCase();
          const assetClass = $("ph-asset-class").value || "equity";
          const qtyE8 = decimalStrToE8($("ph-qty").value);
          const priceE8 = decimalStrToE8($("ph-price").value);
          const costBasisCents = dollarsToCents($("ph-cost-basis").value || "0", { allowZero: true });
          await api("/api/v1/portfolio/holdings", {
            method: "POST",
            body: JSON.stringify({
              symbol,
              asset_class: assetClass,
              quantity_e8: qtyE8,
              price_e8: priceE8,
              cost_basis_cents: costBasisCents,
              source: "manual",
            }),
          });
          addForm.reset();
          addForm.classList.add("hidden");
          toast(`Saved holding ${symbol}.`);
          await loadPortfolio();
          await refreshAll();
        } catch (err) {
          toast(err.message || "Could not save holding.", true);
        }
      });
    });
  }

  if (csvForm) {
    csvForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const btn = csvForm.querySelector("button[type=submit]");
      await withBusy(btn, async () => {
        try {
          const fileInput = $("ph-csv-file");
          const file = fileInput && fileInput.files && fileInput.files[0];
          if (!file) throw new Error("Select a broker CSV holdings file first.");
          const fd = new FormData();
          fd.append("file", file, file.name);
          const res = await api("/api/v1/portfolio/import-csv", { method: "POST", body: fd });
          const imported = Number((res && (res.imported_count ?? res.count)) || 0);
          csvForm.reset();
          csvForm.classList.add("hidden");
          toast(`Imported ${imported} holding(s) from broker CSV.`);
          await loadPortfolio();
          await refreshAll();
        } catch (err) {
          toast(err.message || "CSV holdings import failed.", true);
        }
      });
    });
  }

  const brokerModal = $("portfolio-broker-modal");
  const brokerClose = $("btn-port-broker-modal-close");
  if (brokerClose && brokerModal) {
    brokerClose.addEventListener("click", () => closeOverlay(brokerModal));
  }
  if (brokerModal) {
    brokerModal.addEventListener("click", (ev) => {
      if (ev.target === brokerModal) closeOverlay(brokerModal);
    });
    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape" && !brokerModal.classList.contains("hidden")) {
        closeOverlay(brokerModal);
      }
    });
  }

  // --- Credit & Debt Handlers ---
  const auToggle = $("credit-524-au-toggle");
  if (auToggle) {
    auToggle.addEventListener("change", () => loadCreditReport());
  }

  const crDate = $("cr-pulled-date");
  if (crDate && !crDate.value) crDate.value = isoDateOffset(0);
  const ciDate = $("ci-date");
  if (ciDate && !ciDate.value) ciDate.value = isoDateOffset(0);
  const cdDate = $("cd-date");
  if (cdDate && !cdDate.value) cdDate.value = isoDateOffset(0);

  const reportForm = $("credit-report-form");
  if (reportForm) {
    reportForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const btn = reportForm.querySelector("button[type=submit]");
      await withBusy(btn, async () => {
        try {
          await api("/api/v1/credit/reports", {
            method: "POST",
            body: JSON.stringify({
              bureau: $("cr-bureau").value,
              model: $("cr-model").value,
              score: parseInt($("cr-score").value, 10),
              pulled_date: $("cr-pulled-date").value || isoDateOffset(0),
              source: "manual",
            }),
          });
          $("cr-score").value = "";
          toast("Actual bureau score recorded.");
          await loadCreditReport();
        } catch (err) {
          toast(err.message || "Could not record bureau score.", true);
        }
      });
    });
  }

  const pdfForm = $("credit-pdf-upload-form");
  if (pdfForm) {
    pdfForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const btn = pdfForm.querySelector("button[type=submit]");
      await withBusy(btn, async () => {
        try {
          const input = $("cr-pdf-file");
          const file = input && input.files && input.files[0];
          if (!file) throw new Error("Choose an AnnualCreditReport PDF first.");
          const fd = new FormData();
          fd.append("file", file, file.name);
          fd.append("confirm", "false");
          const bundle = await api("/api/v1/credit/reports/import", { method: "POST", body: fd });
          renderPdfReviewBox(bundle || {});
        } catch (err) {
          toast(err.message || "PDF parse failed — use the structured score form instead.", true);
        }
      });
    });
  }

  const inqForm = $("credit-inquiry-form");
  if (inqForm) {
    inqForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const btn = inqForm.querySelector("button[type=submit]");
      await withBusy(btn, async () => {
        try {
          await api("/api/v1/credit/inquiries", {
            method: "POST",
            body: JSON.stringify({
              lender: $("ci-lender").value.trim(),
              bureau: $("ci-bureau").value,
              inquiry_date: $("ci-date").value || isoDateOffset(0),
            }),
          });
          $("ci-lender").value = "";
          toast("Hard inquiry logged with 12m/24m drop-off dates.");
          await loadCreditReport();
        } catch (err) {
          toast(err.message || "Could not log inquiry.", true);
        }
      });
    });
  }

  const derogForm = $("credit-derogatory-form");
  if (derogForm) {
    derogForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const btn = derogForm.querySelector("button[type=submit]");
      await withBusy(btn, async () => {
        try {
          await api("/api/v1/credit/derogatories", {
            method: "POST",
            body: JSON.stringify({
              kind: normalizeDerogatoryKind($("cd-kind").value),
              opened_date: $("cd-date").value || isoDateOffset(0),
              notes: ($("cd-note") && $("cd-note").value.trim()) || "",
            }),
          });
          if ($("cd-note")) $("cd-note").value = "";
          toast("Derogatory mark logged with exact drop-off date.");
          await loadCreditReport();
        } catch (err) {
          toast(err.message || "Could not log derogatory mark.", true);
        }
      });
    });
  }

  // --- Optional BYOK Credit Score APIs ---
  const capiVendor = $("capi-vendor");
  if (capiVendor) {
    capiVendor.addEventListener("change", updateCreditApiFormFields);
    updateCreditApiFormFields();
  }

  const capiForm = $("credit-api-connect-form");
  if (capiForm) {
    capiForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const btn = capiForm.querySelector("button[type=submit]");
      await withBusy(btn, async () => {
        try {
          const vendor = $("capi-vendor").value;
          const payload = {
            vendor,
            env: "production",
            label: ($("capi-label") && $("capi-label").value.trim()) || "",
            api_key: ($("capi-api-key") && $("capi-api-key").value.trim()) || null,
            user_id: ($("capi-user-id") && $("capi-user-id").value.trim()) || null,
            report_key: ($("capi-report-key") && $("capi-report-key").value.trim()) || null,
            display_token: ($("capi-display-token") && $("capi-display-token").value.trim()) || null,
            username: ($("capi-username") && $("capi-username").value.trim()) || null,
            password: ($("capi-password") && $("capi-password").value.trim()) || null,
            bureau: ($("capi-bureau") && $("capi-bureau").value) || "equifax",
            option: ($("capi-option") && $("capi-option").value.trim()) || null,
            api_base_url: ($("capi-base-url") && $("capi-base-url").value.trim()) || null,
            test_on_connect: Boolean($("capi-test-on-connect") && $("capi-test-on-connect").checked),
          };
          const res = await api("/api/v1/credit/apis/connect", {
            method: "POST",
            body: JSON.stringify(payload),
          });
          capiForm.reset();
          if ($("capi-vendor")) $("capi-vendor").value = vendor;
          updateCreditApiFormFields();
          const label = (res && res.connection && res.connection.institution) || vendor.toUpperCase();
          setCreditApiStatus(`Sealed ${label} credentials in secrets.enc. Click Sync Score & Tradelines to pull live bureau data.`);
          toast(`Connected Credit Score API: ${label}.`);
          await loadCreditApis();
        } catch (err) {
          setCreditApiStatus(err.message || "Could not connect Credit Score API.", true);
          toast(err.message || "Could not connect Credit Score API.", true);
        }
      });
    });
  }

  const extraForm = $("debt-extra-form");
  if (extraForm) {
    extraForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      await loadCreditReport();
      toast("Recalculated all 4 debt payoff strategies.");
    });
  }

  const clForm = $("credit-line-form");
  if (clForm) {
    clForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const btn = clForm.querySelector("button[type=submit]");
      await withBusy(btn, async () => {
        try {
          const body = {
            name: $("cl-name").value.trim(),
            kind: $("cl-kind").value,
            balance_cents: dollarsToCents($("cl-balance").value, { allowZero: true }),
            credit_limit_cents: dollarsToCents($("cl-limit").value || "0", { allowZero: true }),
            apr_bps: Math.round((Number($("cl-apr").value) || 0) * 100),
            statement_closing_day: parseInt($("cl-close-day").value, 10) || 15,
            payment_due_day: parseInt($("cl-due-day").value, 10) || 10,
            autopay_mode: $("cl-autopay").value,
            has_grace_period: Boolean($("cl-grace") && $("cl-grace").checked),
            is_authorized_user: Boolean($("cl-is-au") && $("cl-is-au").checked),
          };
          if ($("cl-promo-ends") && $("cl-promo-ends").value) {
            body.promo_apr_bps = 0;
            body.promo_ends_date = $("cl-promo-ends").value;
          }
          if ($("cl-opened") && $("cl-opened").value) {
            body.opened_date = $("cl-opened").value;
          }
          await api("/api/v1/credit/lines", { method: "POST", body: JSON.stringify(body) });
          clForm.reset();
          toast("Credit line added — AZEO, 5/24 & strategies updated.");
          await loadCreditReport();
          await refreshAll();
        } catch (err) {
          toast(err.message || "Could not add credit line.", true);
        }
      });
    });
  }

  const btForm = $("bt-form");
  if (btForm) {
    btForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const btn = btForm.querySelector("button[type=submit]");
      await withBusy(btn, async () => {
        try {
          const postAprRaw = $("bt-post-apr") && $("bt-post-apr").value.trim();
          const payload = {
            balance_cents: dollarsToCents($("bt-balance").value),
            current_apr_bps: Math.round((Number($("bt-apr").value) || 0) * 100),
            transfer_fee_bps: Math.round((Number($("bt-fee").value) || 0) * 100),
            promo_months: parseInt($("bt-months").value, 10) || 15,
            monthly_payment_cents: dollarsToCents($("bt-pay").value),
            post_promo_apr_bps: postAprRaw ? Math.round((Number(postAprRaw) || 0) * 100) : null,
          };
          const res = await api("/api/v1/credit/balance-transfer", {
            method: "POST",
            body: JSON.stringify(payload),
          });
          const box = $("bt-result");
          if (box) {
            box.classList.remove("hidden");
            const vCls =
              res.verdict === "strong_yes" ? "pill-calm" : res.verdict === "marginal" ? "pill-watch" : "pill-danger";
            box.replaceChildren(
              el("span", { class: `pill ${vCls}`, text: String(res.verdict || "").replace(/_/g, " ").toUpperCase() }),
              el("p", { text: res.explanation, style: { margin: "8px 0 4px" } }),
              el("div", {
                class: "muted small tabular-nums",
                text:
                  `Fee ${fmtMoney(res.transfer_fee_cents)} · ` +
                  `Required to clear before promo ends: ${fmtMoney(res.required_monthly_to_clear_promo_cents)}/mo` +
                  (res.post_promo_apr_bps != null
                    ? ` · Post-promo APR: ${(Number(res.post_promo_apr_bps) / 100).toFixed(2)}%`
                    : ""),
              })
            );
          }
        } catch (err) {
          toast(err.message || "Balance transfer check failed.", true);
        }
      });
    });
  }

  registerTabHook("portfolio", () => loadPortfolio());
  registerTabHook("credit", () => loadCreditReport());
  registerRefreshHook(async () => {
    if (state.activeTab === "portfolio") await loadPortfolio();
    if (state.activeTab === "credit") await loadCreditReport();
  });
}
