/**
 * ChronoX Finance — AI Financial Coach, Unified --say Harness, One-Tap Optimizer & Usage Modal
 * Pure zero-CDN DOM implementation:
 * 1. Unified "Say or Log Anything" (`POST /api/v1/ai/say`) with inline advice, proposed changes, export & undo
 * 2. AI + Statistical Quant One-Tap Optimization Modal (`POST /api/v1/optimize/one-tap` + undo)
 * 3. AI Usage & Savings Modal (`GET /api/v1/ai/usage?days=30`) + daily limit setting
 * 4. Coach Assistant Planner (`Plan -> Proposed Changes -> Apply -> Undo`) & Personalized Recommendations
 */

import {
  $,
  el,
  makeIcon,
  renderEmptyState,
  buildSkeletonCard,
  downloadExportFile,
  isoDateOffset,
  api,
  state,
  toast,
  withBusy,
  fmtMoney,
  openOverlay,
  closeOverlay,
  switchTab,
  syncModeControls,
  formatProvenance,
  PROVIDER_DISPLAY_NAMES,
  renderFormattedText,
  setNodeIconText,
  refreshAll,
  registerTabHook,
  registerRefreshHook,
  registerDashboardHook,
  registerSayHandler,
  registerOneTapHandler,
} from "./core.js";

const aiState = {
  usage: null,
  activePlan: null,
  batchHistory: [],
  suggestions: null,
  loadingSuggestions: false,
  oneTapHorizon: 30,
  oneTapPlan: null,
  oneTapLastBatchId: null,
};

const ONETAP_ACTION_LABELS = {
  rebalance_jar: "Rebalance Budget",
  set_emergency_buffer: "Update Safety Cushion",
  set_safe_invest_cap: "Update Invest Cap",
  fund_goal: "Fund Savings Goal",
  advisory_debt_azeo: "Credit & Debt Tip",
};

function fmtMicrodollars(micro) {
  const val = (Number(micro) || 0) / 1e6;
  if (val === 0) return "$0.00";
  if (Math.abs(val) >= 0.01) return `$${val.toFixed(2)}`;
  return `$${val.toFixed(4)}`;
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
  badgeText.textContent = reqs > 0 ? `AI Usage (${reqs})` : "AI Usage";
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
        el("span", { class: "eyebrow", text: `Requests (${rep.days || 30}d)` }),
        el("div", { class: "metric-md tabular-nums", text: String(rep.total_requests || 0) }),
        el("span", {
          class: "muted small tabular-nums",
          text: `Today: ${rep.requests_today || 0} / ${rep.daily_quota_limit || 60} (${rep.quota_remaining_today ?? 0} left)`,
        }),
      ]),
      el("div", { class: "card stat-tile" }, [
        el("span", { class: "eyebrow", text: "Estimated Spend" }),
        el("div", {
          class: "metric-md tabular-nums",
          text: fmtMicrodollars(rep.total_cost_microdollars || 0),
        }),
        el("span", {
          class: "muted small tabular-nums",
          text: `${fmtTokenCount(totalIn)} in · ${fmtTokenCount(totalOut)} out`,
        }),
      ]),
      el("div", { class: "card stat-tile" }, [
        el("span", { class: "eyebrow", text: "Cache Efficiency" }),
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
      el("div", { class: "card stat-tile" }, [
        el("span", { class: "eyebrow", text: "Saved via Caching" }),
        el("div", {
          class: "metric-md tabular-nums",
          style: { color: "var(--primary)" },
          text: fmtMicrodollars(rep.total_saved_microdollars || 0),
        }),
        el("span", {
          class: "muted small tabular-nums",
          text: `${totalCached.toLocaleString()} cached tokens reused`,
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
            "No AI requests logged in the last 30 days. Built-in financial math always runs locally for free.",
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
              el("th", { class: "num", text: "Requests" }),
              el("th", { class: "num", text: "Cache %" }),
              el("th", { class: "num", text: "Avg Time" }),
              el("th", { class: "num", text: "Cost" }),
              el("th", { class: "num", text: "Saved" }),
            ]),
          ]),
          el(
            "tbody",
            {},
            byProv.map((row) =>
              el("tr", {}, [
                el("td", {}, [
                  el("strong", {
                    text: PROVIDER_DISPLAY_NAMES[row.provider] || String(row.provider || "").replace(/_/g, " "),
                  }),
                ]),
                el("td", { class: "tabular-nums small", text: row.model || "--" }),
                el("td", { class: "num tabular-nums", text: String(row.requests || 0) }),
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
      provHost.replaceChildren(el("h3", { text: "Usage by Assistant" }), table);
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
      "aria-label": "Daily AI request limit",
    });
    const saveBtn = el("button", { type: "submit", class: "btn btn-primary btn-sm", text: "Save Daily Limit" });

    quotaForm.replaceChildren(
      el("label", { class: "field-inline" }, [
        el("span", { class: "small muted", text: "Daily AI Request Limit:" }),
        quotaInput,
      ]),
      saveBtn,
      el("span", {
        class: "muted small",
        text: "Automatic response caching keeps repeat questions fast and low-cost.",
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
        el("strong", { text: "Proposed Changes" }),
        el("span", { class: `pill ${statusCls}`, text: plan.status === "executed" ? "Applied" : plan.status === "undone" ? "Undone" : "Ready to Apply" }),
        el("span", {
          class: "pill pill-soft",
          text: `${PROVIDER_DISPLAY_NAMES[plan.provider] || plan.provider || "Built-in Math"}${plan.model_used ? ` · ${plan.model_used}` : ""}`,
        }),
        el("span", { class: "pill pill-calm", text: "No external money movement" }),
      ]),
      el("span", { class: "muted small tabular-nums", text: `${validCount} ready${rejectedCount ? ` · ${rejectedCount} blocked` : ""}` }),
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
  const list = $("ai-suggestions-list");
  if (list && (!aiState.suggestions || forceRefresh)) {
    list.replaceChildren(buildSkeletonCard(2), buildSkeletonCard(2));
  }
  try {
    const res = await api(`/api/v1/ai/suggestions?refresh=${forceRefresh ? "true" : "false"}`);
    aiState.suggestions = res;
    renderAiSuggestions(res);
    loadAiUsage(30);
    return res;
  } catch (err) {
    if (list) {
      list.replaceChildren(
        renderEmptyState({
          icon: "alert",
          title: "Couldn't load recommendations",
          subtitle: err.message || "Try refreshing in a moment.",
          actionLabel: "Retry",
          onAction: () => loadAiSuggestions(true),
          compact: true,
        })
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
      renderEmptyState({
        icon: "sparkles",
        title: "Everything looks steady",
        subtitle: "No urgent adjustments needed right now. Run One-Tap Optimize anytime for a full cashflow check.",
        actionLabel: "One-Tap Optimize",
        onAction: () => openOneTapModal(),
        compact: true,
      })
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
   4. Unified "Say or Log Anything" Command Bar (`POST /api/v1/ai/say`)
   ============================================================================ */

function hideSayResponseCard() {
  const card = $("command-say-response");
  if (!card) return;
  card.classList.add("hidden");
  card.replaceChildren();
}

function renderSayResponseCard(res) {
  const card = $("command-say-response");
  if (!card || !res) return;

  const intent = String(res.intent_kind || "ledger_mutation");
  const isConfirm = Boolean(res.requires_confirmation && !res.executed);
  const isAdvice = intent === "question_advice" || (!res.executed && !isConfirm);
  const provLabel =
    PROVIDER_DISPLAY_NAMES[res.provider_used] ||
    (res.provider_used ? String(res.provider_used).replace(/_/g, " ") : "Built-in Math");

  card.className = `say-response-card${isConfirm ? " is-confirm" : isAdvice ? " is-advice" : ""}`;
  card.classList.remove("hidden");

  const dismissBtn = el(
    "button",
    { type: "button", class: "btn btn-ghost btn-xs", "aria-label": "Dismiss message" },
    [makeIcon("x", "ui-icon-sm")]
  );
  dismissBtn.addEventListener("click", hideSayResponseCard);

  const kickerText = isConfirm
    ? "Review Proposed Changes"
    : res.executed
    ? "Saved to ChronoX"
    : intent === "export_request"
    ? "Export Ready"
    : "AI Coach Answer";

  const headerRow = el("div", { class: "item-row" }, [
    el("div", { class: "row-gap wrap", style: { gap: "8px" } }, [
      el("span", {
        class: `pill ${isConfirm ? "pill-watch" : res.executed ? "pill-calm" : "pill-blue"}`,
        text: kickerText,
      }),
      el("span", {
        class: "muted small",
        text: `${provLabel}${res.model_used ? ` · ${res.model_used}` : ""}`,
      }),
    ]),
    dismissBtn,
  ]);

  const msgNode = el("div", { class: "small", style: { lineHeight: "1.5" } });
  msgNode.appendChild(renderFormattedText(res.assistant_message || res.message || "Done."));

  const footerActions = [];

  if (res.executed && res.undo_available !== false && res.batch_id) {
    const undoBtn = el("button", { type: "button", class: "btn btn-soft btn-xs" }, [
      makeIcon("undo", "ui-icon-sm"),
      "Undo",
    ]);
    undoBtn.addEventListener("click", async () => {
      await undoAgentBatch(res.batch_id, undoBtn);
      hideSayResponseCard();
    });
    footerActions.push(undoBtn);
  }

  let proposedListNode = null;
  if (isConfirm && res.batch_id) {
    const acts = Array.isArray(res.actions)
      ? res.actions
      : Array.isArray(res.plan && res.plan.actions)
      ? res.plan.actions
      : [];
    if (acts.length) {
      proposedListNode = el(
        "div",
        { class: "stack-xs" },
        acts.map((act, idx) =>
          el("div", { class: "item-box" }, [
            el("div", { class: "item-row" }, [
              el("strong", { class: "small", text: `${idx + 1}. ${act.summary || act.action_type || "Change"}` }),
              el("span", {
                class: "pill pill-soft",
                text: String(act.action_type || "action").replace(/_/g, " "),
              }),
            ]),
          ])
        )
      );
    }

    const applyBtn = el("button", { type: "button", class: "btn btn-primary btn-sm" }, [
      makeIcon("check", "ui-icon-sm"),
      "Apply Changes",
    ]);
    applyBtn.addEventListener("click", () =>
      withBusy(applyBtn, async () => {
        try {
          const execRes = await api(`/api/v1/ai/agent/execute/${res.batch_id}`, { method: "POST" });
          const applied = Number((execRes && execRes.applied_count) || 1);
          toast(`Applied ${applied} change${applied === 1 ? "" : "s"}.`, {
            durationMs: 8000,
            undoLabel: "Undo",
            onUndo: () => undoAgentBatch(res.batch_id),
          });
          hideSayResponseCard();
          await refreshAll();
        } catch (err) {
          toast(err.message || "Could not apply proposed changes.", true);
        }
      })
    );

    const cancelBtn = el("button", { type: "button", class: "btn btn-ghost btn-sm", text: "Cancel" });
    cancelBtn.addEventListener("click", hideSayResponseCard);
    footerActions.push(applyBtn, cancelBtn);
  } else if (isAdvice) {
    const coachBtn = el("button", { type: "button", class: "btn btn-ghost btn-xs", text: "Open Coach →" });
    coachBtn.addEventListener("click", () => switchTab("coach"));
    footerActions.push(coachBtn);
  }

  if (res.export_download_url) {
    const dlBtn = el("button", { type: "button", class: "btn btn-primary btn-xs" }, [
      makeIcon("download", "ui-icon-sm"),
      "Download Again",
    ]);
    dlBtn.addEventListener("click", () => triggerSayExport(res));
    footerActions.push(dlBtn);
  }

  const cardChildren = [headerRow, msgNode];
  if (proposedListNode) cardChildren.push(proposedListNode);
  if (footerActions.length) cardChildren.push(el("div", { class: "row-gap wrap" }, footerActions));
  card.replaceChildren(...cardChildren);
}

async function triggerSayExport(res) {
  const url = res.export_download_url;
  if (!url) return;
  const fmt = String(res.export_format || "numbers").toLowerCase();
  const ext = fmt === "csv_pack" || url.includes("pack=true") ? "zip" : fmt === "csv" ? "csv" : "xlsx";
  const label =
    ext === "xlsx"
      ? "Apple Numbers Workbook"
      : ext === "zip"
      ? "Full CSV Pack"
      : "Transactions CSV";
  try {
    await downloadExportFile({
      url,
      defaultFilename: `chronox-export-${isoDateOffset(0)}.${ext}`,
      label,
    });
  } catch (err) {
    toast(err.message || "Export download failed.", true);
  }
}

export async function submitSayUtterance(rawText) {
  const text = String(rawText || "").trim();
  if (!text) return null;

  let res = null;
  try {
    res = await api("/api/v1/ai/say", {
      method: "POST",
      body: JSON.stringify({ text, auto_commit: true }),
    });
  } catch (err) {
    if (err && err.status === 404) {
      // Graceful fallback for older backend instances without /api/v1/ai/say
      const legacy = await api("/api/v1/quick-add", {
        method: "POST",
        body: JSON.stringify({ text }),
      });
      toast((legacy && legacy.message) || "Saved.");
      await refreshAll();
      return legacy;
    }
    throw err;
  }

  if (!res) return null;

  if (res.onetap_plan) {
    openOneTapModal(res.onetap_plan);
  }

  if (res.export_download_url) {
    await triggerSayExport(res);
  }

  if (res.requires_confirmation && res.plan) {
    aiState.activePlan = res.plan;
    renderAgentPlanPreview(res.plan);
  }

  renderSayResponseCard(res);

  if (res.executed) {
    const msg = res.assistant_message || "Saved to ChronoX.";
    if (res.undo_available !== false && res.batch_id) {
      toast(msg, {
        durationMs: 8000,
        undoLabel: "Undo",
        onUndo: async () => {
          await undoAgentBatch(res.batch_id);
          hideSayResponseCard();
        },
      });
    } else {
      toast(msg);
    }
    await refreshAll();
  } else if (res.intent_kind === "question_advice") {
    loadAiUsage(30);
  }

  return res;
}

/* ============================================================================
   5. AI + Statistical Math "One-Tap Optimize" Modal (`POST /api/v1/optimize/one-tap`)
   ============================================================================ */

function syncOneTapHorizonButtons(horizonDays) {
  const sw = $("onetap-horizon-switcher");
  if (!sw) return;
  sw.querySelectorAll("[data-onetap-horizon]").forEach((btn) => {
    const active = Number(btn.dataset.onetapHorizon) === Number(horizonDays);
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", String(active));
  });
}

async function undoOneTapBatch(batchId, btn = null) {
  if (!batchId) return;
  await withBusy(btn, async () => {
    try {
      await api(`/api/v1/optimize/one-tap/undo/${batchId}`, { method: "POST" });
      aiState.oneTapLastBatchId = null;
      toast("One-Tap optimization undone — previous settings and jars restored.");
      await refreshAll();
      const overlay = $("onetap-overlay");
      if (overlay && !overlay.classList.contains("hidden")) {
        await loadOneTapPreview(aiState.oneTapHorizon);
      }
    } catch (err) {
      toast(err.message || "Could not undo One-Tap plan.", true);
    }
  });
}

function renderOneTapModal(plan) {
  const body = $("onetap-body");
  const footer = $("onetap-footer");
  if (!body || !footer) return;
  aiState.oneTapPlan = plan;

  const runway = (plan && plan.runway) || {};
  const diag = (plan && plan.diagnostics) || {};
  const actions = Array.isArray(plan && plan.actions) ? plan.actions : [];
  const rationales = {};
  if (Array.isArray(plan && plan.action_rationales)) {
    for (const item of plan.action_rationales) {
      if (item && item.action_id && item.rationale) {
        rationales[String(item.action_id)] = String(item.rationale);
      }
    }
  } else if (plan && plan.action_rationales && typeof plan.action_rationales === "object") {
    Object.assign(rationales, plan.action_rationales);
  }
  const horizon = Number(runway.horizon_days || aiState.oneTapHorizon || 30);
  const ruinPct = Number(runway.ruin_probability_pct || 0);
  const survivalPct = Math.max(0, Math.min(100, 100 - ruinPct));
  const cvarCents = Number(runway.cvar_95_buffer_cents || 0);
  const safeDailyCents = Number(runway.confidence_95_daily_safe_cents || 0);
  const annualSavingsCents = Number(plan.total_expected_annual_savings_cents || 0);
  const executableCount = actions.filter((a) => a.executable !== false && !a.is_advisory).length;

  const provName =
    PROVIDER_DISPLAY_NAMES[plan.provider_used] ||
    (plan.provider_used ? String(plan.provider_used).replace(/_/g, " ") : "Built-in Math");

  const summaryText = el("p", { class: "small", style: { margin: "0", lineHeight: "1.55" } });
  summaryText.appendChild(
    renderFormattedText(
      plan.executive_summary ||
        (plan.needs_setup
          ? "Connect a bank account or add transactions so ChronoX can build your statistical cashflow plan."
          : "Your finances are balanced across your safety cushion, bills, and savings goals.")
    )
  );

  const summaryCard = el("div", { class: "onetap-summary-card" }, [
    el("div", { class: "item-row" }, [
      el("strong", { text: "Executive Summary" }),
      el("span", {
        class: `pill ${plan.applied ? "pill-calm" : "pill-blue"}`,
        text: plan.applied
          ? "Plan Applied"
          : `${provName}${plan.model_used ? ` · ${plan.model_used}` : ""}`,
      }),
    ]),
    summaryText,
  ]);

  const kpiGrid = el("div", { class: "onetap-kpi-grid" }, [
    el("div", { class: "card stat-tile" }, [
      el("span", { class: "eyebrow", text: "95% Tail-Risk Cushion (CVaR)" }),
      el("div", { class: "metric-md tabular-nums", text: fmtMoney(cvarCents) }),
      el("span", { class: "muted small", text: "Buffer for worst 5% cashflow weeks" }),
    ]),
    el("div", { class: "card stat-tile" }, [
      el("span", { class: "eyebrow", text: `${horizon}-Day Survival Confidence` }),
      el("div", {
        class: "metric-md tabular-nums",
        style: { color: survivalPct >= 95 ? "var(--primary)" : survivalPct >= 85 ? "var(--amber)" : "var(--crimson)" },
        text: survivalPct >= 99.95 ? ">99.9%" : `${survivalPct.toFixed(1)}%`,
      }),
      el("span", { class: "muted small", text: "Probability of staying above $0" }),
    ]),
    el("div", { class: "card stat-tile" }, [
      el("span", { class: "eyebrow", text: "95%-Safe Daily Spend" }),
      el("div", { class: "metric-md tabular-nums", style: { color: "var(--primary)" }, text: fmtMoney(safeDailyCents) }),
      el("span", { class: "muted small", text: "Stress-tested daily budget" }),
    ]),
    el("div", { class: "card stat-tile" }, [
      el("span", { class: "eyebrow", text: "Projected Annual Savings" }),
      el("div", {
        class: "metric-md tabular-nums",
        style: { color: annualSavingsCents > 0 ? "var(--primary)" : "var(--text)" },
        text: `${annualSavingsCents > 0 ? "+" : ""}${fmtMoney(annualSavingsCents)}`,
      }),
      el("span", { class: "muted small", text: "Interest avoided & yield gained" }),
    ]),
  ]);

  const trendDir = String(diag.trend_direction || (diag.drift && diag.drift.trend_direction) || "stable");
  const driftCents = Number(diag.drift_cents_per_week || (diag.drift && diag.drift.drift_cents_per_week) || 0);
  const anomalies = Array.isArray(diag.anomalies) ? diag.anomalies : [];
  const diagStrip = el("div", { class: "onetap-diag-strip" }, [
    el("span", {
      class: `pill ${trendDir === "accelerating" ? "pill-watch" : trendDir === "cooling" ? "pill-calm" : "pill-soft"}`,
      text: `Spending trend: ${trendDir}${driftCents !== 0 ? ` (${driftCents > 0 ? "+" : ""}${fmtMoney(driftCents)}/wk)` : ""}`,
    }),
    el("span", {
      class: `pill ${anomalies.length > 0 ? "pill-watch" : "pill-soft"}`,
      text: anomalies.length > 0 ? `${anomalies.length} unusual charge${anomalies.length === 1 ? "" : "s"} (14d)` : "No unusual charges (14d)",
    }),
    diag.top_spend_category
      ? el("span", {
          class: "muted small",
          text: `Top category: ${diag.top_spend_category} (${Number(diag.top_spend_share_pct || 0).toFixed(0)}%)`,
        })
      : null,
  ]);

  const actionsSection = actions.length
    ? el("div", { class: "stack-sm" }, [
        el("h3", { text: `Recommended Actions (${actions.length})` }),
        ...actions.map((act, idx) => {
          const isAdv = Boolean(act.is_advisory || act.executable === false);
          const typeLabel = ONETAP_ACTION_LABELS[act.action_type] || String(act.action_type || "Action").replace(/_/g, " ");
          const rationaleText = rationales[act.action_id] || act.rationale || act.summary || "";
          return el("div", { class: `onetap-action-card${isAdv ? " is-advisory" : ""}` }, [
            el("div", { class: "item-row" }, [
              el("div", { class: "row-gap wrap", style: { gap: "6px" } }, [
                el("span", { class: "pill pill-soft", text: `#${idx + 1}` }),
                el("strong", { text: act.title || typeLabel }),
                el("span", { class: `pill ${isAdv ? "pill-blue" : "pill-calm"}`, text: isAdv ? "Advisory" : typeLabel }),
              ]),
              el("div", { class: "row-gap" }, [
                act.amount_cents > 0
                  ? el("strong", { class: "num tabular-nums", text: fmtMoney(act.amount_cents) })
                  : null,
                act.expected_annual_savings_cents > 0
                  ? el("span", {
                      class: "pill pill-calm",
                      text: `+${fmtMoney(act.expected_annual_savings_cents)}/yr`,
                    })
                  : null,
              ]),
            ]),
            rationaleText ? el("div", { class: "small muted", text: rationaleText }) : null,
          ]);
        }),
      ])
    : renderEmptyState({
        icon: "check",
        title: plan.needs_setup ? "Add account activity first" : "Your budget is already optimized",
        subtitle: plan.needs_setup
          ? "Connect your bank or import statements so ChronoX can tailor your safety cushion and goals."
          : "No jar rebalances or cushion updates are needed for this horizon.",
        actionLabel: plan.needs_setup ? "Connect Bank" : "",
        onAction: plan.needs_setup
          ? () => {
              closeOverlay($("onetap-overlay"));
              switchTab("settings");
            }
          : null,
        compact: true,
      });

  body.replaceChildren(summaryCard, kpiGrid, diagStrip, actionsSection);

  // Footer: Apply One-Tap Plan + Undo (if applied)
  const footerBtns = [];
  const appliedBatchId = plan.applied && plan.batch_id ? plan.batch_id : aiState.oneTapLastBatchId;

  if (plan.applied && appliedBatchId) {
    const undoBtn = el("button", { type: "button", class: "btn btn-soft btn-sm" }, [
      makeIcon("undo", "ui-icon-sm"),
      "Undo One-Tap",
    ]);
    undoBtn.addEventListener("click", () => undoOneTapBatch(appliedBatchId, undoBtn));
    footerBtns.push(undoBtn);
  } else {
    const advisoryCount = Math.max(0, actions.length - executableCount);
    const applyBtn = el(
      "button",
      {
        type: "button",
        class: "btn btn-primary btn-sm",
        disabled: Boolean(plan.needs_setup) || executableCount === 0,
      },
      [
        makeIcon("sparkles", "ui-icon-sm"),
        executableCount > 0 ? `Apply One-Tap Plan (${executableCount})` : "Nothing to Apply",
      ]
    );
    applyBtn.addEventListener("click", () =>
      withBusy(applyBtn, async () => {
        try {
          const applyBody = { horizon_days: horizon, apply: true };
          if (plan && plan.plan_fingerprint) {
            applyBody.plan_fingerprint = plan.plan_fingerprint;
          }
          const appliedRes = await api("/api/v1/optimize/one-tap", {
            method: "POST",
            body: JSON.stringify(applyBody),
          });
          aiState.oneTapLastBatchId = appliedRes.batch_id || null;
          renderOneTapModal(appliedRes);
          toast("One-Tap optimization applied.", {
            durationMs: 8000,
            undoLabel: "Undo One-Tap",
            onUndo: () => undoOneTapBatch(appliedRes.batch_id),
          });
          await refreshAll();
        } catch (err) {
          if (err && err.status === 409) {
            toast("Plan changed after recent activity — refreshing preview...", true);
            await loadOneTapPreview(horizon);
            return;
          }
          toast(err.message || "Could not apply One-Tap plan.", true);
        }
      })
    );
    footerBtns.push(applyBtn);
    footer.dataset.advisoryCount = String(advisoryCount);
  }

  const closeBtn = el("button", { type: "button", class: "btn btn-ghost btn-sm", text: "Close" });
  closeBtn.addEventListener("click", () => closeOverlay($("onetap-overlay")));
  footerBtns.push(closeBtn);

  const advisoryTotal = Math.max(0, actions.length - executableCount);
  const footCaption =
    executableCount > 0 && advisoryTotal > 0
      ? `${executableCount} of ${actions.length} actions apply · ${advisoryTotal} advisory · Bills & locked jars protected`
      : "Bills and locked jars are always protected. Every change is 1-click undoable.";

  footer.replaceChildren(
    el("span", {
      class: "muted small",
      text: footCaption,
    }),
    el("div", { class: "row-gap wrap" }, footerBtns)
  );
}

async function loadOneTapPreview(horizonDays = 30) {
  aiState.oneTapHorizon = horizonDays;
  syncOneTapHorizonButtons(horizonDays);
  const body = $("onetap-body");
  const footer = $("onetap-footer");
  if (body) {
    body.replaceChildren(buildSkeletonCard(3), buildSkeletonCard(2));
  }
  if (footer) {
    footer.replaceChildren(el("span", { class: "muted small", text: "Running 500-path statistical runway & optimization..." }));
  }
  try {
    const plan = await api("/api/v1/optimize/one-tap", {
      method: "POST",
      body: JSON.stringify({ horizon_days: horizonDays, apply: false }),
    });
    renderOneTapModal(plan);
    loadAiUsage(30);
  } catch (err) {
    if (body) {
      body.replaceChildren(
        renderEmptyState({
          icon: "alert",
          title: "One-Tap Optimizer unavailable",
          subtitle: err.message || "Could not compute optimization plan right now.",
          actionLabel: "Retry",
          onAction: () => loadOneTapPreview(horizonDays),
        })
      );
    }
    if (footer) {
      const cBtn = el("button", { type: "button", class: "btn btn-ghost btn-sm", text: "Close" });
      cBtn.addEventListener("click", () => closeOverlay($("onetap-overlay")));
      footer.replaceChildren(el("span", { class: "muted small", text: "" }), cBtn);
    }
  }
}

export async function openOneTapModal(prefetchedPlan = null) {
  const overlay = $("onetap-overlay");
  if (!overlay) return;
  openOverlay(overlay);
  if (prefetchedPlan && typeof prefetchedPlan === "object") {
    const hz = Number((prefetchedPlan.runway && prefetchedPlan.runway.horizon_days) || 30);
    aiState.oneTapHorizon = hz;
    syncOneTapHorizonButtons(hz);
    renderOneTapModal(prefetchedPlan);
    return;
  }
  await loadOneTapPreview(aiState.oneTapHorizon || 30);
}

/* ============================================================================
   Module Bootstrap
   ============================================================================ */

export function initAiCoach() {
  registerSayHandler(submitSayUtterance);
  registerOneTapHandler(openOneTapModal);

  // One-Tap Optimize Modal Triggers & Controls
  const overviewOneTapBtn = $("btn-overview-onetap");
  const coachOneTapBtn = $("btn-coach-onetap");
  const oneTapCloseBtn = $("btn-onetap-close");
  const oneTapOverlay = $("onetap-overlay");
  const oneTapHorizons = $("onetap-horizon-switcher");

  if (overviewOneTapBtn) overviewOneTapBtn.addEventListener("click", () => openOneTapModal());
  if (coachOneTapBtn) coachOneTapBtn.addEventListener("click", () => openOneTapModal());
  if (oneTapCloseBtn && oneTapOverlay) {
    oneTapCloseBtn.addEventListener("click", () => closeOverlay(oneTapOverlay));
  }
  if (oneTapOverlay) {
    oneTapOverlay.addEventListener("click", (ev) => {
      if (ev.target === oneTapOverlay) closeOverlay(oneTapOverlay);
    });
  }
  if (oneTapHorizons) {
    oneTapHorizons.addEventListener("click", (ev) => {
      const btn = ev.target.closest("button[data-onetap-horizon]");
      if (!btn) return;
      const days = Number(btn.dataset.onetapHorizon) || 30;
      loadOneTapPreview(days);
    });
  }

  // Settings & Coach AI Usage Modal Triggers
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
