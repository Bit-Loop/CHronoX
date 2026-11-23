/**
 * ChronoX Finance — AI Usage Telemetry Modal, Multi-Provider Mode Sync & Agentic Command Console (ES Module)
 * Pure zero-CDN DOM implementation:
 * 1. Topbar `#btn-ai-usage` live badge (`reqs · cache%`) + Usage/Cache/Exact-$ Saved Modal + Daily Quota Guard
 * 2. Agentic Natural-Language Command Console: Plan (`POST /api/v1/ai/agent/plan`) -> Action Diff Preview ->
 *    1-Click Atomic Execute (`POST /api/v1/ai/agent/execute/{batch_id}`) -> 8s Undo Toast (`POST /api/v1/ai/agent/undo/{batch_id}`)
 *    with row-version conflict handling
 * 3. Synced 3-Mode Intelligence Switcher (Quant Math | Hybrid | AI Only), Verified Suggestions Feed & Ask Coach
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
  openOverlay,
  closeOverlay,
  syncModeControls,
  formatProvenance,
  renderFormattedText,
  setNodeIconText,
  refreshAll,
  registerTabHook,
  registerRefreshHook,
  registerDashboardHook,
} from "./core.js";

const aiState = {
  usage: null,
  activePlan: null,
  batchHistory: [],
  suggestions: null,
  loadingSuggestions: false,
};

function fmtMicrodollars(micro) {
  const val = (Number(micro) || 0) / 1e6;
  if (val === 0) return "$0.0000";
  if (Math.abs(val) >= 1) return `$${val.toFixed(2)}`;
  if (Math.abs(val) >= 0.01) return `$${val.toFixed(4)}`;
  return `$${val.toFixed(6)}`;
}

function fmtTokenCount(n) {
  const num = Number(n) || 0;
  if (num >= 1e6) return `${(num / 1e6).toFixed(2)}M`;
  if (num >= 1e4) return `${(num / 1e3).toFixed(1)}k`;
  return num.toLocaleString();
}

/* ============================================================================
   1. Topbar AI Usage Badge & Modal (GET /api/v1/ai/usage?days=30)
   ============================================================================ */

export async function loadAiUsage(days = 30) {
  if (state.vaultStatus !== "unlocked") return null;
  try {
    const rep = await api(`/api/v1/ai/usage?days=${days}`);
    aiState.usage = rep;
    state.aiUsage = rep;
    updateUsageBadge(rep);
    const overlay = $("ai-usage-overlay");
    if (overlay && !overlay.classList.contains("hidden")) {
      renderAiUsageModal(rep);
    }
    return rep;
  } catch (_err) {
    return null;
  }
}

function updateUsageBadge(rep) {
  const badgeText = $("ai-usage-badge-text");
  if (!badgeText || !rep) return;
  const reqs = Number(rep.total_requests || 0);
  const hit = Number(rep.cache_hit_rate_pct || 0);
  badgeText.textContent = `AI: ${reqs} req · ${hit.toFixed(0)}% cache`;
}

function renderAiUsageModal(rep) {
  const summaryHost = $("ai-usage-summary");
  const provHost = $("ai-usage-providers");
  const quotaForm = $("ai-usage-quota-form");
  if (!rep) return;

  const hitPct = Math.max(0, Math.min(100, Number(rep.cache_hit_rate_pct || 0)));
  const totalIn = Number(rep.total_input_tokens || 0);
  const totalOut = Number(rep.total_output_tokens || 0);
  const totalCached = Number(rep.total_cached_tokens || 0);

  if (summaryHost) {
    summaryHost.replaceChildren(
      el("div", { class: "card stat-tile" }, [
        el("span", { class: "eyebrow", text: `REQUESTS (${rep.days || 30}D)` }),
        el("div", { class: "metric-md tabular-nums", text: String(rep.total_requests || 0) }),
        el("span", {
          class: "muted small tabular-nums",
          text: `Today: ${rep.requests_today || 0} / ${rep.daily_quota_limit || 60} (${rep.quota_remaining_today ?? 0} left)`,
        }),
      ]),
      el("div", { class: "card stat-tile" }, [
        el("span", { class: "eyebrow", text: "TOKENS (IN / OUT)" }),
        el("div", {
          class: "metric-md tabular-nums",
          text: `${fmtTokenCount(totalIn)} / ${fmtTokenCount(totalOut)}`,
        }),
        el("span", {
          class: "muted small tabular-nums",
          text: `Cached tokens: ${totalCached.toLocaleString()}`,
        }),
      ]),
      el("div", { class: "card stat-tile" }, [
        el("span", { class: "eyebrow", text: "CACHE HIT RATE" }),
        el("div", {
          class: "metric-md tabular-nums",
          style: { color: hitPct >= 25 ? "var(--primary)" : "var(--cyan)" },
          text: `${hitPct.toFixed(1)}%`,
        }),
        el("div", { class: "progress-track", style: { height: "5px" } }, [
          el("div", {
            class: "progress-fill",
            style: { width: `${hitPct.toFixed(1)}%`, backgroundColor: "var(--primary)" },
          }),
        ]),
      ]),
      el("div", { class: "card stat-tile", style: { borderTopColor: "var(--primary)" } }, [
        el("span", { class: "eyebrow", text: "EXACT $ SAVED (CACHE)" }),
        el("div", {
          class: "metric-md tabular-nums",
          style: { color: "var(--primary)" },
          text: fmtMicrodollars(rep.total_saved_microdollars || 0),
        }),
        el("span", {
          class: "muted small tabular-nums",
          text: `Spend: ${fmtMicrodollars(rep.total_cost_microdollars || 0)} · computed from real cached tokens`,
        }),
      ])
    );
  }

  if (provHost) {
    const byProv = Array.isArray(rep.by_provider) ? rep.by_provider : [];
    if (!byProv.length) {
      provHost.replaceChildren(
        el("div", { class: "item-box muted small" }, [
          el("span", { class: "inline-icon-row" }, [
            makeIcon("cpu", "ui-icon-sm"),
            "Zero AI provider requests logged in the last 30 days. Local Quant Math runs at $0.00.",
          ]),
        ])
      );
    } else {
      const table = el("div", { class: "table-wrap" }, [
        el("table", { class: "tx-table" }, [
          el("thead", {}, [
            el("tr", {}, [
              el("th", { text: "Provider" }),
              el("th", { text: "Model" }),
              el("th", { class: "num", text: "Reqs" }),
              el("th", { class: "num", text: "In / Out" }),
              el("th", { class: "num", text: "Cached" }),
              el("th", { class: "num", text: "Cache %" }),
              el("th", { class: "num", text: "Latency" }),
              el("th", { class: "num", text: "Cost" }),
              el("th", { class: "num", text: "Exact Saved" }),
            ]),
          ]),
          el(
            "tbody",
            {},
            byProv.map((row) =>
              el("tr", {}, [
                el("td", {}, [el("strong", { text: String(row.provider || "").replace(/_/g, " ").toUpperCase() })]),
                el("td", { class: "tabular-nums small", text: row.model || "--" }),
                el("td", { class: "num tabular-nums", text: String(row.requests || 0) }),
                el("td", {
                  class: "num tabular-nums",
                  text: `${fmtTokenCount(row.input_tokens)} / ${fmtTokenCount(row.output_tokens)}`,
                }),
                el("td", { class: "num tabular-nums", text: fmtTokenCount(row.cached_tokens) }),
                el("td", { class: "num tabular-nums", text: `${Number(row.cache_hit_rate_pct || 0).toFixed(1)}%` }),
                el("td", { class: "num tabular-nums muted", text: `${row.avg_latency_ms || 0}ms` }),
                el("td", { class: "num tabular-nums", text: fmtMicrodollars(row.cost_microdollars) }),
                el("td", {
                  class: "num tabular-nums",
                  style: { color: "var(--primary)", fontWeight: "700" },
                  text: fmtMicrodollars(row.saved_microdollars),
                }),
              ])
            )
          ),
        ]),
      ]);
      provHost.replaceChildren(el("h3", { text: "PROVIDER & MODEL BREAKDOWN" }), table);
    }
  }

  if (quotaForm) {
    const quotaInput = el("input", {
      id: "ai-modal-quota-input",
      type: "number",
      min: "1",
      max: "500",
      value: String(rep.daily_quota_limit || (state.settings && state.settings.opencode_max_daily_requests) || 60),
      class: "input-xs",
      "aria-label": "Daily AI request quota limit",
    });
    const saveBtn = el("button", { type: "submit", class: "btn btn-primary btn-sm", text: "Update Daily Quota Guard" });

    quotaForm.replaceChildren(
      el("label", { class: "field-inline" }, [
        el("span", { class: "eyebrow", style: { marginBottom: "0" }, text: "DAILY QUOTA GUARD (REQS/DAY):" }),
        quotaInput,
      ]),
      saveBtn,
      el("span", {
        class: "muted small",
        text: "Stateless store=false prompt caching active (>=1024-token static prefix).",
      })
    );
  }
}

async function openAiUsageModal() {
  const overlay = $("ai-usage-overlay");
  if (!overlay) return;
  openOverlay(overlay);
  if (aiState.usage) {
    renderAiUsageModal(aiState.usage);
  }
  const fresh = await loadAiUsage(30);
  if (fresh) renderAiUsageModal(fresh);
}

/* ============================================================================
   2. Agentic Natural-Language Command Console (Plan -> Diff -> Execute -> Undo)
   ============================================================================ */

function formatPayloadDiffChips(payload) {
  if (!payload || typeof payload !== "object") return [];
  const chips = [];
  for (const [k, v] of Object.entries(payload)) {
    if (v === null || v === undefined || v === "") continue;
    let displayVal = "";
    if (k.endsWith("_cents") && Number.isFinite(Number(v))) {
      displayVal = fmtMoney(Number(v));
    } else if (Array.isArray(v)) {
      displayVal = `${v.length} item(s)`;
    } else if (typeof v === "object") {
      displayVal = JSON.stringify(v);
    } else {
      displayVal = String(v);
    }
    const cleanKey = k.replace(/_cents$/, "").replace(/_/g, " ");
    chips.push(el("span", { class: "pill pill-soft", text: `${cleanKey}: ${displayVal}` }));
  }
  return chips.slice(0, 6);
}

async function undoAgentBatch(batchId, btn = null) {
  await withBusy(btn, async () => {
    try {
      const res = await api(`/api/v1/ai/agent/undo/${batchId}`, { method: "POST" });
      if (res && res.status === "conflict") {
        const conflicts = Array.isArray(res.conflicts) ? res.conflicts : [];
        const detail = conflicts.length
          ? ` Conflicts: ${conflicts.map((c) => c.reason || c.table || JSON.stringify(c)).join("; ")}`
          : "";
        toast(`Undo refused (rows modified after execute).${detail}`, { isError: true, durationMs: 8000 });
        renderAgentConflictBanner(batchId, res);
        return;
      }
      if (aiState.activePlan && Number(aiState.activePlan.batch_id) === Number(batchId)) {
        aiState.activePlan = { ...aiState.activePlan, status: "undone" };
        renderAgentPlanPreview(aiState.activePlan);
      }
      aiState.batchHistory = aiState.batchHistory.map((b) =>
        Number(b.batch_id) === Number(batchId) ? { ...b, status: "undone" } : b
      );
      renderAgentHistory();
      toast(`Undone Batch #${batchId} (${(res && res.restored_count) || 0} row(s) restored).`);
      await refreshAll();
    } catch (err) {
      toast(err.message || "Could not undo agent batch.", true);
    }
  });
}

function renderAgentConflictBanner(batchId, res) {
  const wrap = $("agent-preview-wrap");
  if (!wrap) return;
  wrap.classList.remove("hidden");
  const conflicts = Array.isArray(res && res.conflicts) ? res.conflicts : [];
  wrap.prepend(
    el("div", { class: "notice-bar notice-warn details-spacer" }, [
      makeIcon("alert", "ui-icon-sm"),
      el("div", { class: "stack-xs" }, [
        el("strong", { text: `Undo Conflict on Batch #${batchId}: ${(res && res.message) || "Target rows changed since execution."}` }),
        ...conflicts.map((c) =>
          el("div", { class: "small tabular-nums", text: typeof c === "string" ? c : JSON.stringify(c) })
        ),
      ]),
    ])
  );
}

async function executeAgentBatch(plan, btn = null) {
  await withBusy(btn, async () => {
    try {
      const res = await api(`/api/v1/ai/agent/execute/${plan.batch_id}`, { method: "POST" });
      const applied = Number((res && res.applied_count) || 0);
      aiState.activePlan = { ...plan, status: "executed", executeResults: (res && res.results) || [] };
      aiState.batchHistory = [
        {
          batch_id: plan.batch_id,
          prompt: plan.prompt,
          applied_count: applied,
          status: "executed",
          undo_available: Boolean(res && res.undo_available !== false),
        },
        ...aiState.batchHistory.filter((b) => Number(b.batch_id) !== Number(plan.batch_id)),
      ].slice(0, 6);

      renderAgentPlanPreview(aiState.activePlan);
      renderAgentHistory();

      toast(`Executed Batch #${plan.batch_id} (${applied} action${applied === 1 ? "" : "s"} applied).`, {
        durationMs: 8000,
        undoLabel: "Undo Batch",
        onUndo: () => undoAgentBatch(plan.batch_id),
      });
      await refreshAll();
      await loadAiUsage(30);
    } catch (err) {
      toast(err.message || "Agent execution failed.", true);
    }
  });
}

function renderAgentPlanPreview(plan) {
  const wrap = $("agent-preview-wrap");
  if (!wrap) return;
  if (!plan) {
    wrap.classList.add("hidden");
    wrap.replaceChildren();
    return;
  }

  wrap.classList.remove("hidden");
  const actions = Array.isArray(plan.actions) ? plan.actions : [];
  const validCount = Number(plan.valid_count ?? actions.filter((a) => a.valid !== false).length);
  const rejectedCount = Number(plan.rejected_count ?? actions.filter((a) => a.valid === false).length);
  const statusCls =
    plan.status === "executed" ? "pill-calm" : plan.status === "undone" ? "pill-watch" : "pill-blue";

  const execBtn = el(
    "button",
    {
      type: "button",
      class: "btn btn-primary btn-sm",
      disabled: plan.status !== "preview" || validCount === 0,
    },
    [
      makeIcon("play", "ui-icon-sm"),
      plan.status === "executed" ? "Executed" : `Execute ${validCount} Valid Action${validCount === 1 ? "" : "s"}`,
    ]
  );
  execBtn.addEventListener("click", () => executeAgentBatch(plan, execBtn));

  const undoBtn =
    plan.status === "executed"
      ? el("button", { type: "button", class: "btn btn-soft btn-sm" }, [makeIcon("undo", "ui-icon-sm"), "Undo Batch"])
      : null;
  if (undoBtn) {
    undoBtn.addEventListener("click", () => undoAgentBatch(plan.batch_id, undoBtn));
  }

  const dismissBtn = el("button", { type: "button", class: "btn btn-ghost btn-sm", text: "Dismiss" });
  dismissBtn.addEventListener("click", () => {
    aiState.activePlan = null;
    wrap.classList.add("hidden");
    wrap.replaceChildren();
  });

  const actionRows = actions.length
    ? actions.map((act, idx) => {
        const isValid = act.valid !== false;
        const typeLabel = String(act.action_type || "action").replace(/_/g, " ").toUpperCase();
        const warns = Array.isArray(act.warnings) ? act.warnings : [];
        return el(
          "div",
          {
            class: "agent-action-row",
            style: {
              flexDirection: "column",
              alignItems: "stretch",
              borderLeft: `3px solid ${isValid ? "var(--primary)" : "var(--crimson)"}`,
            },
          },
          [
            el("div", { class: "item-row" }, [
              el("span", { class: "row-gap wrap", style: { gap: "6px" } }, [
                el("span", { class: "muted", text: `#${String(idx + 1).padStart(2, "0")}` }),
                el("span", { class: `pill ${isValid ? "pill-blue" : "pill-danger"}`, text: typeLabel }),
                el("strong", { style: { fontFamily: "var(--font-sans)" }, text: act.summary }),
              ]),
              el("span", {
                class: `pill ${isValid ? "pill-calm" : "pill-danger"}`,
                text: isValid ? "VALIDATED" : "BLOCKED",
              }),
            ]),
            el("div", { class: "chip-row", style: { marginTop: "2px" } }, formatPayloadDiffChips(act.payload)),
            !isValid && act.rejected_reason
              ? el("div", { class: "error-text small", text: `Blocked: ${act.rejected_reason}` })
              : null,
            ...warns.map((w) => el("div", { class: "small", style: { color: "var(--amber)" }, text: `Warning: ${w}` })),
          ]
        );
      })
    : [
        el("div", {
          class: "item-box muted small",
          text: "No ledger mutations proposed for this prompt (read-only advice only).",
        }),
      ];

  const expP = el("p", { class: "small", style: { margin: "0" } });
  if (plan.explanation) expP.appendChild(renderFormattedText(plan.explanation));

  const card = el("div", { class: "agent-diff-card" }, [
    el("div", { class: "item-row" }, [
      el("div", { class: "row-gap wrap", style: { gap: "6px" } }, [
        el("strong", { text: `ACTION DIFF PREVIEW · BATCH #${plan.batch_id}` }),
        el("span", { class: `pill ${statusCls}`, text: String(plan.status || "preview").toUpperCase() }),
        el("span", { class: "pill pill-soft", text: `${plan.provider || "local"}${plan.model_used ? ` · ${plan.model_used}` : ""}` }),
        el("span", { class: "pill pill-calm", text: "ZERO MONEY MOVEMENT" }),
      ]),
      el("span", { class: "muted small tabular-nums", text: `${validCount} valid · ${rejectedCount} blocked` }),
    ]),
    plan.explanation ? expP : null,
    el("div", { class: "stack-xs" }, actionRows),
    el("div", { class: "row-gap wrap" }, [execBtn, undoBtn, dismissBtn]),
  ]);

  wrap.replaceChildren(card);
}

function renderAgentHistory() {
  const host = $("agent-history-list");
  if (!host) return;
  if (!aiState.batchHistory.length) {
    host.replaceChildren();
    return;
  }

  host.replaceChildren(
    el("span", { class: "eyebrow", text: "RECENT AGENT BATCHES" }),
    ...aiState.batchHistory.map((b) => {
      const undoBtn =
        b.status === "executed" && b.undo_available
          ? el("button", { type: "button", class: "btn btn-ghost btn-xs" }, [makeIcon("undo", "ui-icon-sm"), "Undo"])
          : null;
      if (undoBtn) {
        undoBtn.addEventListener("click", () => undoAgentBatch(b.batch_id, undoBtn));
      }
      return el("div", { class: "item-box", style: { flexDirection: "row", alignItems: "center", justifyContent: "space-between" } }, [
        el("span", { class: "row-gap wrap small", style: { gap: "7px" } }, [
          el("span", { class: `pill ${b.status === "executed" ? "pill-calm" : "pill-watch"}`, text: `#${b.batch_id} ${b.status}` }),
          el("span", { text: `"${b.prompt}" (${b.applied_count} applied)` }),
        ]),
        undoBtn,
      ]);
    })
  );
}

/* ============================================================================
   3. Verified Suggestions Feed & Ask Coach
   ============================================================================ */

export async function loadAgentBatches() {
  if (state.vaultStatus !== "unlocked") return;
  try {
    const list = await api("/api/v1/ai/agent/batches?limit=8");
    if (Array.isArray(list)) {
      aiState.batchHistory = list
        .filter((b) => b.status === "executed" || b.status === "undone")
        .slice(0, 6)
        .map((b) => ({
          batch_id: b.id ?? b.batch_id,
          prompt: b.prompt || "",
          applied_count: Array.isArray(b.actions) ? b.actions.length : Number(b.applied_count || 0),
          status: b.status,
          undo_available: b.status === "executed",
        }));
      renderAgentHistory();
    }
  } catch (_err) {
    // Non-fatal
  }
}

export async function loadAiSuggestions(forceRefresh = false) {
  if (state.vaultStatus !== "unlocked" || aiState.loadingSuggestions) return null;
  aiState.loadingSuggestions = true;
  try {
    const res = await api(`/api/v1/ai/suggestions?refresh=${forceRefresh ? "true" : "false"}`);
    aiState.suggestions = res;
    renderAiSuggestions(res);
    loadAiUsage(30);
    return res;
  } catch (err) {
    const list = $("ai-suggestions-list");
    if (list) {
      list.replaceChildren(
        el("div", { class: "item-box muted small", text: err.message || "Could not load suggestions." })
      );
    }
    return null;
  } finally {
    aiState.loadingSuggestions = false;
  }
}

function renderAiSuggestions(res) {
  const badge = $("ai-source-badge");
  const notice = $("ai-notice");
  const list = $("ai-suggestions-list");
  if (!res) return;

  if (badge) {
    const prov = formatProvenance(res);
    badge.className = `pill ${prov.cls}`;
    setNodeIconText(badge, prov.icon, prov.text);
  }
  if (res.intelligence_mode) {
    syncModeControls(res.intelligence_mode);
  }

  if (notice) {
    if (res.notice) {
      notice.textContent = res.notice;
      notice.classList.remove("hidden");
    } else {
      notice.classList.add("hidden");
    }
  }

  if (!list) return;
  const items = Array.isArray(res.suggestions) ? res.suggestions : [];
  if (!items.length) {
    list.replaceChildren(
      el("div", { class: "item-box muted small" }, [
        el("span", { class: "inline-icon-row" }, [
          makeIcon("check", "ui-icon-sm"),
          "All systems steady — no urgent adjustments suggested right now.",
        ]),
      ])
    );
    return;
  }

  list.replaceChildren(
    ...items.map((s) => {
      const cat = String(s.category || "tip").toUpperCase();
      const bodyP = el("div", { class: "small" });
      bodyP.appendChild(renderFormattedText(s.body || s.detail || ""));

      let actionBtn = null;
      if (s.action_kind === "agent_prompt" && s.action_payload && s.action_payload.prompt) {
        actionBtn = el("button", { type: "button", class: "btn btn-soft btn-xs", text: "Plan in Console ->" });
        actionBtn.addEventListener("click", () => {
          const inp = $("agent-prompt-input");
          const frm = $("agent-prompt-form");
          if (inp && frm) {
            inp.value = s.action_payload.prompt;
            frm.requestSubmit();
          }
        });
      } else if (s.action_type && s.action_type !== "none") {
        actionBtn = el(
          "button",
          { type: "button", class: "btn btn-primary btn-xs" },
          [makeIcon("check", "ui-icon-sm"), s.action_label || "Apply Suggestion"]
        );
        actionBtn.addEventListener("click", () =>
          withBusy(actionBtn, async () => {
            try {
              const applied = await api("/api/v1/ai/suggestions/apply", {
                method: "POST",
                body: JSON.stringify({
                  action_type: s.action_type,
                  action_payload: s.action_payload || {},
                }),
              });
              toast((applied && applied.message) || "Applied recommendation.");
              await refreshAll();
              await loadAiSuggestions(true);
            } catch (err) {
              toast(err.message || "Could not apply suggestion.", true);
            }
          })
        );
      }

      return el("div", { class: "item-box" }, [
        el("div", { class: "item-row" }, [
          el("strong", { text: s.title || "Recommendation" }),
          el("span", { class: "pill pill-soft", text: cat }),
        ]),
        bodyP,
        s.micro_step ? el("div", { class: "micro-step", text: `-> ${s.micro_step}` }) : null,
        actionBtn ? el("div", { class: "row-gap" }, [actionBtn]) : null,
      ]);
    })
  );
}

/* ============================================================================
   Module Bootstrap
   ============================================================================ */

export function initAiCoach() {
  // Topbar & Coach AI Usage Modal Triggers
  const topUsageBtn = $("btn-ai-usage");
  const coachUsageBtn = $("btn-coach-open-usage");
  const usageCloseBtn = $("btn-ai-usage-close");
  const usageOverlay = $("ai-usage-overlay");

  if (topUsageBtn) topUsageBtn.addEventListener("click", openAiUsageModal);
  if (coachUsageBtn) coachUsageBtn.addEventListener("click", openAiUsageModal);
  if (usageCloseBtn && usageOverlay) {
    usageCloseBtn.addEventListener("click", () => closeOverlay(usageOverlay));
  }
  if (usageOverlay) {
    usageOverlay.addEventListener("click", (ev) => {
      if (ev.target === usageOverlay) closeOverlay(usageOverlay);
    });
  }

  // Quota Guard Save inside AI Usage Modal
  const quotaForm = $("ai-usage-quota-form");
  if (quotaForm) {
    quotaForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const input = $("ai-modal-quota-input");
      const btn = quotaForm.querySelector("button[type=submit]");
      const limit = parseInt((input && input.value) || "60", 10);
      if (!Number.isFinite(limit) || limit < 1 || limit > 500) {
        toast("Daily quota must be between 1 and 500 requests.", true);
        return;
      }
      await withBusy(btn, async () => {
        try {
          const updated = await api("/api/v1/settings", {
            method: "PATCH",
            body: JSON.stringify({ opencode_max_daily_requests: limit }),
          });
          state.settings = updated;
          toast(`Daily AI quota guard updated to ${limit} req/day.`);
          const fresh = await loadAiUsage(30);
          if (fresh) renderAiUsageModal(fresh);
        } catch (err) {
          toast(err.message || "Could not update quota guard.", true);
        }
      });
    });
  }

  // Note: [data-set-mode] inside #coach-mode-switcher is delegated globally in app.js
  // to avoid duplicate setIntelligenceMode() calls; syncModeControls() keeps it visually synced.

  // Refresh AI Suggestions button
  const refreshAiBtn = $("btn-refresh-ai");
  if (refreshAiBtn) {
    refreshAiBtn.addEventListener("click", (ev) =>
      withBusy(ev.currentTarget, () => loadAiSuggestions(true))
    );
  }

  // Agentic Command Console Form & Example Chips
  const agentForm = $("agent-prompt-form");
  const agentInput = $("agent-prompt-input");
  document.querySelectorAll("[data-agent-example]").forEach((chip) => {
    chip.addEventListener("click", () => {
      if (!agentInput || !agentForm) return;
      agentInput.value = chip.dataset.agentExample || "";
      agentInput.focus();
      agentForm.requestSubmit();
    });
  });

  if (agentForm && agentInput) {
    agentForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const prompt = agentInput.value.trim();
      if (!prompt) return;
      const btn = agentForm.querySelector("button[type=submit]");
      await withBusy(btn, async () => {
        try {
          const plan = await api("/api/v1/ai/agent/plan", {
            method: "POST",
            body: JSON.stringify({ prompt }),
          });
          aiState.activePlan = plan;
          renderAgentPlanPreview(plan);
          await loadAiUsage(30);
        } catch (err) {
          toast(err.message || "Could not generate agent action plan.", true);
        }
      });
    });
  }

  // Direct Ask Coach Form
  const askForm = $("ask-coach-form");
  const askInput = $("ask-coach-input");
  const askOut = $("ask-coach-answer");
  if (askForm && askInput && askOut) {
    askForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const question = askInput.value.trim();
      if (!question) return;
      const btn = askForm.querySelector("button[type=submit]");
      askOut.classList.remove("hidden");
      askOut.textContent = "Checking exact balances and synthesizing guidance...";
      await withBusy(btn, async () => {
        try {
          const res = await api("/api/v1/ai/ask", {
            method: "POST",
            body: JSON.stringify({ question }),
          });
          const prov = formatProvenance(res);
          const p = el("p", { style: { margin: "8px 0 0" } });
          p.appendChild(renderFormattedText(res.answer || ""));
          askOut.replaceChildren(
            el("div", { class: `pill ${prov.cls}` }, [makeIcon(prov.icon, "ui-icon-sm"), prov.text]),
            p
          );
          await loadAiUsage(30);
        } catch (err) {
          askOut.textContent = err.message || "Could not answer question right now.";
        }
      });
    });
  }

  registerDashboardHook((dash) => {
    if (dash && dash.intelligence_mode) {
      syncModeControls(dash.intelligence_mode);
    }
    loadAiUsage(30);
  });

  registerTabHook("coach", async () => {
    await Promise.all([loadAiUsage(30), loadAiSuggestions(false), loadAgentBatches()]);
  });

  registerRefreshHook(async (opts = {}) => {
    await loadAiUsage(30);
    if (state.activeTab === "coach" || opts.refreshAI) {
      await loadAiSuggestions(Boolean(opts.refreshAI));
    }
  });
}
