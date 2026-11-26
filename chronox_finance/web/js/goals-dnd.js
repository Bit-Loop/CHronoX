/**
 * ChronoX Finance — Savings Goals, Purchase Planner & Auto-Categorize Rules (ES Module)
 * Pure local Pointer Events & DOM API:
 * 1. Drag-to-Reprioritize Savings Goals (grip handle + emerald insertion bar + Alt+Up/Down A11y)
 * 2. Swipe Detents (Swipe Right >=80px -> Add $50 with Undo; Swipe Left <=-80px -> Remove with 8s Undo)
 *    paired with always-visible desktop buttons (+$50 Fund, Edit, Plan, Delete)
 * 3. Credit-Safe Purchase Planner (3 paths + 1-click Save as Savings Goal)
 * 4. Auto-Categorize Rule Builder with Match Preview & Retroactive Apply
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
  dollarsToCents,
  openDrawer,
  refreshAll,
  registerTabHook,
  registerRefreshHook,
  registerDashboardHook,
  renderEmptyState,
  triggerCelebrate,
} from "./core.js";

const SWIPE_THRESHOLD_PX = 80;
const SWIPE_MAX_PX = 128;

const dndState = {
  goals: [],
  rules: [],
  editingGoalId: null,
  editingRuleId: null,
  lastPlan: null,
  dragReorder: null,
};

function sortGoalsByPriority(list) {
  return [...(Array.isArray(list) ? list : [])]
    .filter((g) => !g.deleted_at)
    .sort((a, b) => {
      const poA = Number(a.priority_order ?? 9999);
      const poB = Number(b.priority_order ?? 9999);
      if (poA !== poB) return poA - poB;
      return Number(a.id || 0) - Number(b.id || 0);
    });
}

async function fetchGoals() {
  if (state.vaultStatus !== "unlocked") return;
  try {
    const res = await api("/api/v1/goals");
    dndState.goals = sortGoalsByPriority(Array.isArray(res) ? res : []);
    renderGoalsList();
  } catch (err) {
    toast(err.message || "Could not load goals.", true);
  }
}

async function fetchRules() {
  if (state.vaultStatus !== "unlocked") return;
  try {
    const res = await api("/api/v1/rules");
    dndState.rules = Array.isArray(res) ? res : [];
    renderRulesList();
  } catch (err) {
    const list = $("rules-list");
    if (list) {
      list.replaceChildren(
        el("div", { class: "item-box muted small", text: err.message || "Could not load automation rules." })
      );
    }
  }
}

/* ============================================================================
   Goal Mutations (Contribute, Soft-Delete + Restore Undo, Reorder, Edit)
   ============================================================================ */

async function contributeGoal(goal, amountCents = 5000, anchorEl = null) {
  const prevSaved = Number(goal.saved_cents || 0);
  try {
    await api(`/api/v1/goals/${goal.id}/contribute`, {
      method: "POST",
      body: JSON.stringify({ amount_cents: amountCents }),
    });
    if (anchorEl) triggerCelebrate(anchorEl, true);
    toast(`Funded "${goal.name}" +${fmtMoney(amountCents)}`, {
      durationMs: 8000,
      undoLabel: "Undo Fund",
      onUndo: async () => {
        await api(`/api/v1/goals/${goal.id}`, {
          method: "PATCH",
          body: JSON.stringify({ saved_cents: prevSaved }),
        });
        toast(`Reverted +${fmtMoney(amountCents)} contribution on "${goal.name}".`);
        await fetchGoals();
        await refreshAll();
      },
    });
    await fetchGoals();
    await refreshAll();
  } catch (err) {
    toast(err.message || "Could not fund goal.", true);
  }
}

async function destroyGoalWithUndo(goal) {
  try {
    await api(`/api/v1/goals/${goal.id}`, { method: "DELETE" });
    dndState.goals = dndState.goals.filter((g) => Number(g.id) !== Number(goal.id));
    renderGoalsList();
    toast(`Removed goal "${goal.name}"`, {
      durationMs: 8000,
      undoLabel: "Restore Goal",
      onUndo: async () => {
        await api(`/api/v1/goals/${goal.id}/restore`, { method: "POST" });
        toast(`Restored "${goal.name}".`);
        await fetchGoals();
        await refreshAll();
      },
    });
    await refreshAll();
  } catch (err) {
    toast(err.message || "Could not remove goal.", true);
  }
}

async function persistGoalOrder(orderedIds) {
  try {
    const updated = await api("/api/v1/goals/reorder", {
      method: "POST",
      body: JSON.stringify({ ordered_ids: orderedIds }),
    });
    if (Array.isArray(updated) && updated.length) {
      dndState.goals = sortGoalsByPriority(updated);
    } else {
      const byId = new Map(dndState.goals.map((g) => [Number(g.id), g]));
      dndState.goals = orderedIds
        .map((id, idx) => {
          const item = byId.get(Number(id));
          return item ? { ...item, priority_order: idx } : null;
        })
        .filter(Boolean);
    }
    renderGoalsList();
    toast("Goal priority order updated — surplus allocation recalculated.");
    await refreshAll();
  } catch (err) {
    toast(err.message || "Could not save goal order.", true);
    await fetchGoals();
  }
}

async function moveGoalByStep(goalId, delta) {
  const idx = dndState.goals.findIndex((g) => Number(g.id) === Number(goalId));
  if (idx < 0) return;
  const nextIdx = idx + delta;
  if (nextIdx < 0 || nextIdx >= dndState.goals.length) return;
  const copy = [...dndState.goals];
  const [moved] = copy.splice(idx, 1);
  copy.splice(nextIdx, 0, moved);
  const orderedIds = copy.map((g) => Number(g.id));
  dndState.goals = copy.map((g, i) => ({ ...g, priority_order: i }));
  renderGoalsList();
  await persistGoalOrder(orderedIds);
  const focusCard = document.querySelector(`.goal-swipe-shell[data-goal-id="${goalId}"] .goal-drag-handle`);
  if (focusCard) focusCard.focus();
}

/* ============================================================================
   Render Tracked Goals with Pointer Events Drag-Reorder + Swipe Detents
   ============================================================================ */

function bindSwipeGestures(surface, shell, goal) {
  let startX = 0;
  let startY = 0;
  let locked = false;
  let canceled = false;
  let currentDx = 0;
  let activePointerId = null;

  const backdrop = shell.querySelector(".goal-swipe-backdrop");

  const resetVisual = () => {
    surface.classList.remove("is-swiping");
    surface.style.transform = "translate3d(0, 0, 0)";
    if (backdrop) backdrop.style.background = "";
  };

  surface.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0 && ev.pointerType === "mouse") return;
    if (ev.target.closest("button, input, select, textarea, form, .goal-drag-handle")) return;
    activePointerId = ev.pointerId;
    startX = ev.clientX;
    startY = ev.clientY;
    locked = false;
    canceled = false;
    currentDx = 0;
  });

  surface.addEventListener("pointermove", (ev) => {
    if (activePointerId !== ev.pointerId || canceled) return;
    const dx = ev.clientX - startX;
    const dy = ev.clientY - startY;
    if (!locked) {
      if (Math.abs(dy) > 14 && Math.abs(dy) >= Math.abs(dx)) {
        canceled = true;
        activePointerId = null;
        return;
      }
      if (Math.abs(dx) > 16 && Math.abs(dx) > Math.abs(dy) * 1.4) {
        locked = true;
        surface.classList.add("is-swiping");
        try {
          surface.setPointerCapture(ev.pointerId);
        } catch (_err) {
          // ignore
        }
      } else {
        return;
      }
    }
    currentDx = Math.max(-SWIPE_MAX_PX, Math.min(SWIPE_MAX_PX, dx));
    surface.style.transform = `translate3d(${currentDx.toFixed(1)}px, 0, 0)`;
    if (backdrop) {
      if (currentDx >= SWIPE_THRESHOLD_PX) {
        backdrop.style.background = "var(--primary-soft)";
      } else if (currentDx <= -SWIPE_THRESHOLD_PX) {
        backdrop.style.background = "var(--coral-soft)";
      } else {
        backdrop.style.background = "";
      }
    }
  });

  const finishSwipe = (ev) => {
    if (activePointerId !== ev.pointerId) return;
    activePointerId = null;
    if (locked) {
      try {
        surface.releasePointerCapture(ev.pointerId);
      } catch (_err) {
        // ignore
      }
    }
    const finalDx = currentDx;
    const wasLocked = locked;
    locked = false;
    currentDx = 0;
    resetVisual();

    if (!wasLocked) return;
    if (finalDx >= SWIPE_THRESHOLD_PX) {
      contributeGoal(goal, 5000, shell);
    } else if (finalDx <= -SWIPE_THRESHOLD_PX) {
      destroyGoalWithUndo(goal);
    }
  };

  surface.addEventListener("pointerup", finishSwipe);
  surface.addEventListener("pointercancel", finishSwipe);
}

function bindGripReorder(handle, shell, goalId) {
  handle.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0 && ev.pointerType === "mouse") return;
    ev.preventDefault();
    ev.stopPropagation();
    const list = $("goals-grid");
    if (!list) return;

    handle.setPointerCapture(ev.pointerId);
    shell.style.opacity = "0.62";
    shell.style.outline = "1px solid var(--primary)";
    dndState.dragReorder = {
      sourceId: Number(goalId),
      targetId: null,
      insertAfter: false,
      pointerId: ev.pointerId,
    };

    const clearMarkers = () => {
      list.querySelectorAll(".goal-swipe-shell").forEach((node) => {
        node.classList.remove("drop-above", "drop-below");
      });
    };

    const onMove = (moveEv) => {
      if (!dndState.dragReorder || dndState.dragReorder.pointerId !== moveEv.pointerId) return;
      clearMarkers();
      const shells = Array.from(list.querySelectorAll(".goal-swipe-shell"));
      let bestShell = null;
      let bestAfter = false;
      for (const s of shells) {
        const rect = s.getBoundingClientRect();
        const midY = rect.top + rect.height / 2;
        if (moveEv.clientY < midY) {
          bestShell = s;
          bestAfter = false;
          break;
        }
        bestShell = s;
        bestAfter = true;
      }
      if (bestShell) {
        const tid = Number(bestShell.dataset.goalId);
        dndState.dragReorder.targetId = tid;
        dndState.dragReorder.insertAfter = bestAfter;
        if (tid !== Number(goalId)) {
          bestShell.classList.add(bestAfter ? "drop-below" : "drop-above");
        }
      }
    };

    const onUp = async (upEv) => {
      if (!dndState.dragReorder || dndState.dragReorder.pointerId !== upEv.pointerId) return;
      handle.removeEventListener("pointermove", onMove);
      handle.removeEventListener("pointerup", onUp);
      handle.removeEventListener("pointercancel", onUp);
      try {
        handle.releasePointerCapture(upEv.pointerId);
      } catch (_err) {
        // ignore
      }
      clearMarkers();
      shell.style.opacity = "";
      shell.style.outline = "";
      const { sourceId, targetId, insertAfter } = dndState.dragReorder;
      dndState.dragReorder = null;

      if (targetId == null || targetId === sourceId) return;
      const ids = dndState.goals.map((g) => Number(g.id));
      const fromIdx = ids.indexOf(sourceId);
      if (fromIdx < 0) return;
      ids.splice(fromIdx, 1);
      const toIdx = ids.indexOf(targetId);
      if (toIdx < 0) return;
      ids.splice(insertAfter ? toIdx + 1 : toIdx, 0, sourceId);
      await persistGoalOrder(ids);
    };

    handle.addEventListener("pointermove", onMove);
    handle.addEventListener("pointerup", onUp);
    handle.addEventListener("pointercancel", onUp);
  });

  // Keyboard Accessibility: Alt+ArrowUp / Alt+ArrowDown
  const onKeyReorder = (ev) => {
    if (ev.altKey && ev.key === "ArrowUp") {
      ev.preventDefault();
      moveGoalByStep(goalId, -1);
    } else if (ev.altKey && ev.key === "ArrowDown") {
      ev.preventDefault();
      moveGoalByStep(goalId, 1);
    }
  };
  handle.addEventListener("keydown", onKeyReorder);
  shell.addEventListener("keydown", onKeyReorder);
}

function buildGoalInlineEditForm(goal) {
  const form = el("form", { class: "inline-editor form-grid-compact", style: { width: "100%" } }, [
    el("input", {
      name: "name",
      type: "text",
      value: goal.name || "",
      placeholder: "Goal name",
      required: true,
    }),
    el("input", {
      name: "target",
      type: "number",
      step: "0.01",
      min: "1",
      value: (Number(goal.target_cents || 0) / 100).toFixed(2),
      placeholder: "Target ($)",
      required: true,
    }),
    el("input", {
      name: "saved",
      type: "number",
      step: "0.01",
      min: "0",
      value: (Number(goal.saved_cents || 0) / 100).toFixed(2),
      placeholder: "Saved ($)",
    }),
    el("input", {
      name: "target_date",
      type: "date",
      value: goal.target_date || "",
      title: "Target date",
    }),
    el("input", {
      name: "notes",
      type: "text",
      value: goal.notes || "",
      placeholder: "Notes (optional)",
    }),
    el("div", { class: "row-gap" }, [
      el("button", { type: "submit", class: "btn btn-primary btn-xs", text: "Save" }),
      el("button", { type: "button", class: "btn btn-ghost btn-xs", text: "Cancel", "data-cancel-edit": "1" }),
    ]),
  ]);

  form.querySelector("[data-cancel-edit]").addEventListener("click", () => {
    dndState.editingGoalId = null;
    renderGoalsList();
  });

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const btn = form.querySelector("button[type=submit]");
    await withBusy(btn, async () => {
      try {
        const body = {
          name: form.elements.name.value.trim(),
          target_cents: dollarsToCents(form.elements.target.value),
          saved_cents: dollarsToCents(form.elements.saved.value || "0", { allowZero: true }),
          notes: form.elements.notes.value.trim(),
        };
        if (form.elements.target_date.value) body.target_date = form.elements.target_date.value;
        await api(`/api/v1/goals/${goal.id}`, {
          method: "PATCH",
          body: JSON.stringify(body),
        });
        dndState.editingGoalId = null;
        toast(`Updated goal "${body.name}".`);
        await fetchGoals();
        await refreshAll();
      } catch (err) {
        toast(err.message || "Could not save goal changes.", true);
      }
    });
  });

  return form;
}

function renderGoalsList() {
  const grid = $("goals-grid");
  if (!grid) return;

  if (!dndState.goals.length) {
    grid.replaceChildren(
      renderEmptyState({
        icon: "target",
        title: "No savings goals yet",
        subtitle: "Create your first goal or run the Purchase Planner below to compare credit-safe saving paths.",
        actionLabel: "+ New Goal",
        onAction: () => {
          const f = $("goal-quick-create-form");
          if (f) {
            f.classList.remove("hidden");
            if ($("gq-name")) $("gq-name").focus();
          }
        },
      })
    );
    return;
  }

  const nodes = dndState.goals.map((g, idx) => {
    const pct = Math.min(100, Math.max(0, Number(g.pct_funded || 0)));
    const isFunded = g.status === "funded" || pct >= 100;
    const rankStr = `#${String(idx + 1).padStart(2, "0")}`;

    const handle = el(
      "button",
      {
        type: "button",
        class: "goal-drag-handle",
        title: "Drag to reprioritize (or press Alt+Up / Alt+Down)",
        "aria-label": `Reorder ${g.name} (currently priority ${idx + 1}). Press Alt+Up or Alt+Down.`,
      },
      [makeIcon("grip", "ui-icon-sm")]
    );

    const fundBtn = el(
      "button",
      {
        type: "button",
        class: "btn btn-win btn-xs",
        title: `Contribute +$50 to ${g.name}`,
      },
      [makeIcon("plus", "ui-icon-sm"), "$50 Fund"]
    );
    fundBtn.addEventListener("click", () => contributeGoal(g, 5000, fundBtn));

    const editBtn = el(
      "button",
      {
        type: "button",
        class: "btn btn-soft btn-xs",
        title: `Edit ${g.name}`,
      },
      [makeIcon("edit", "ui-icon-sm"), "Edit"]
    );
    editBtn.addEventListener("click", () => {
      dndState.editingGoalId = dndState.editingGoalId === g.id ? null : g.id;
      renderGoalsList();
    });

    const optBtn = el(
      "button",
      {
        type: "button",
        class: "btn btn-ghost btn-xs",
        title: `Re-optimize ${g.name} in Purchase Optimizer`,
      },
      [makeIcon("sparkles", "ui-icon-sm"), "Optimize"]
    );
    optBtn.addEventListener("click", () => {
      if ($("gp-name")) $("gp-name").value = g.name || "";
      if ($("gp-price")) $("gp-price").value = (Number(g.target_cents || 0) / 100).toFixed(2);
      if ($("gp-saved")) $("gp-saved").value = (Number(g.saved_cents || 0) / 100).toFixed(2);
      if ($("gp-date")) $("gp-date").value = g.target_date || "";
      const planForm = $("goal-plan-form");
      if (planForm) {
        planForm.scrollIntoView({ behavior: "smooth", block: "center" });
        planForm.requestSubmit();
      }
    });

    const delBtn = el(
      "button",
      {
        type: "button",
        class: "btn btn-danger btn-xs",
        title: `Delete ${g.name} (with 8s Undo)`,
        "aria-label": `Delete ${g.name}`,
      },
      [makeIcon("trash", "ui-icon-sm"), "Delete"]
    );
    delBtn.addEventListener("click", () => destroyGoalWithUndo(g));

    const mainCol = el("div", { class: "goal-card-main" }, [
      el("div", { class: "item-row" }, [
        el("div", { class: "row-gap wrap", style: { gap: "7px" } }, [
          el("span", { class: "pill pill-soft", text: rankStr }),
          el("strong", { text: g.name }),
          el("span", { class: `pill ${isFunded ? "pill-calm" : "pill-blue"}`, text: `${pct.toFixed(0)}%` }),
          g.category ? el("span", { class: "pill pill-soft", text: String(g.category) }) : null,
        ]),
        el("span", {
          class: "tabular-nums",
          style: { fontWeight: "800", fontSize: "0.96rem" },
          text: `${fmtMoney(g.saved_cents)} / ${fmtMoney(g.target_cents)}`,
        }),
      ]),
      el("div", { class: "progress-track" }, [
        el("div", {
          class: "progress-fill",
          style: {
            width: `${pct.toFixed(1)}%`,
            backgroundColor: isFunded ? "var(--primary)" : "var(--cyan)",
          },
        }),
      ]),
      el("div", { class: "item-row muted small" }, [
        el("span", {
          class: "tabular-nums",
          text: `${fmtMoney(g.remaining_cents)} remaining${g.target_date ? ` · target ${g.target_date}` : ""}`,
        }),
        g.notes ? el("span", { text: g.notes }) : null,
      ]),
    ]);

    const surface = el("div", { class: "goal-card-surface" }, [
      handle,
      mainCol,
      el("div", { class: "goal-card-actions" }, [fundBtn, editBtn, optBtn, delBtn]),
      dndState.editingGoalId === g.id ? buildGoalInlineEditForm(g) : null,
    ]);

    const backdrop = el("div", { class: "goal-swipe-backdrop", "aria-hidden": "true" }, [
      el("span", { class: "goal-swipe-fund inline-icon-row" }, [makeIcon("plus", "ui-icon-sm"), "Add $50"]),
      el("span", { class: "goal-swipe-destroy inline-icon-row" }, ["Remove", makeIcon("trash", "ui-icon-sm")]),
    ]);

    const shell = el(
      "article",
      {
        class: "goal-swipe-shell",
        "data-goal-id": String(g.id),
      },
      [backdrop, surface]
    );

    bindGripReorder(handle, shell, g.id);
    bindSwipeGestures(surface, shell, g);
    return shell;
  });

  grid.replaceChildren(...nodes);
}

/* ============================================================================
   $1,000 Credit-Safe Goal & Purchase Optimizer
   ============================================================================ */

function renderGoalPlan(plan) {
  dndState.lastPlan = plan;
  state.lastGoalPlan = plan;
  const out = $("goal-plan-output");
  if (!out) return;
  out.classList.remove("hidden");

  const saveTrackedGoal = async (optIdx = null) => {
    const chosen = optIdx != null && plan.options[optIdx] ? plan.options[optIdx] : null;
    try {
      await api("/api/v1/goals", {
        method: "POST",
        body: JSON.stringify({
          name: plan.item_name,
          target_cents: Number(plan.target_cents),
          saved_cents: Number(plan.already_saved_cents || 0),
          target_date: (chosen && chosen.projected_ready_date) || null,
          category: "gadget",
          notes: chosen ? `${chosen.title} (${fmtMoney(chosen.per_paycheck_cents)}/paycheck)` : "",
        }),
      });
      toast(`Saved "${plan.item_name}" as a Tracked Goal.`);
      await fetchGoals();
      await refreshAll();
    } catch (err) {
      toast(err.message || "Could not save goal.", true);
    }
  };

  const saveTopBtn = el(
    "button",
    { type: "button", class: "btn btn-primary btn-sm" },
    [makeIcon("plus", "ui-icon-sm"), "Save as Tracked Goal"]
  );
  saveTopBtn.addEventListener("click", () => withBusy(saveTopBtn, () => saveTrackedGoal(0)));

  const headerNodes = [
    el("div", { class: "item-row" }, [
      el("div", {}, [
        el("h3", {
          text: `Plan for ${plan.item_name} (${fmtMoney(plan.target_cents)} · ~${plan.work_hours_cost}h work)`,
        }),
        el("p", {
          class: "muted small tabular-nums",
          text: `${fmtMoney(plan.remaining_cents)} remaining · Monthly flexible surplus: ${fmtMoney(plan.monthly_flexible_surplus_cents)}`,
        }),
      ]),
      saveTopBtn,
    ]),
  ];

  if (plan.high_apr_debt_warning) {
    headerNodes.push(
      el("div", { class: "notice-bar notice-warn details-spacer" }, [
        makeIcon("alert", "ui-icon-sm"),
        plan.high_apr_debt_warning,
      ])
    );
  }

  if (Array.isArray(plan.accelerators) && plan.accelerators.length) {
    headerNodes.push(
      el("div", { class: "chip-row" }, [
        el("span", { class: "chip-label", text: "Reach it faster:" }),
        ...plan.accelerators.map((a) => el("span", { class: "pill pill-soft", text: a.label || String(a) })),
      ])
    );
  }

  const optionsGrid = el(
    "div",
    { class: "three-col section-block" },
    (plan.options || []).map((opt, idx) => {
      const saveOptBtn = el(
        "button",
        {
          type: "button",
          class: `btn ${opt.recommended ? "btn-primary" : "btn-soft"} btn-sm`,
          style: { marginTop: "auto" },
        },
        [makeIcon("plus", "ui-icon-sm"), "Save This Path"]
      );
      saveOptBtn.addEventListener("click", () => withBusy(saveOptBtn, () => saveTrackedGoal(idx)));

      return el(
        "article",
        {
          class: "jar-card",
          style: opt.recommended ? { borderColor: "var(--primary-border)" } : {},
        },
        [
          el("div", { class: "jar-top" }, [
            el("strong", { class: "jar-name-text", text: opt.title }),
            opt.recommended
              ? el("span", { class: "pill pill-calm" }, [makeIcon("star", "ui-icon-sm"), "Recommended"])
              : el("span", {
                  class: `pill ${opt.credit_safe ? "pill-blue" : "pill-danger"}`,
                  text: opt.credit_safe ? "Credit-Safe" : "Credit Warning",
                }),
          ]),
          el("div", { class: "small", text: opt.summary }),
          el("div", {
            class: "muted small tabular-nums",
            text: `Ready ${opt.projected_ready_date} (${opt.weeks_to_goal}w) · ${fmtMoney(opt.per_paycheck_cents)}/paycheck · Interest ${fmtMoney(opt.interest_cost_cents)}`,
          }),
          el(
            "ul",
            { class: "stack-xs small", style: { margin: "4px 0", paddingLeft: "18px" } },
            (opt.steps || []).map((st) => el("li", { text: st }))
          ),
          el("div", { class: "micro-step", text: `Credit impact: ${opt.credit_impact_note}` }),
          saveOptBtn,
        ]
      );
    })
  );

  out.replaceChildren(...headerNodes, optionsGrid);
}

/* ============================================================================
   Auto-Categorize Rule Builder + Match Preview
   ============================================================================ */

function buildTriggerFromForm() {
  const rawField = ($("rl-trigger-field") && $("rl-trigger-field").value) || "merchant";
  const rawOp = ($("rl-trigger-op") && $("rl-trigger-op").value) || "contains";
  const rawVal = ($("rl-trigger-val") && $("rl-trigger-val").value.trim()) || "";
  if (!rawVal) throw new Error("Enter a trigger value.");

  if (rawField === "amount") {
    const cents = dollarsToCents(rawVal, { allowZero: true });
    if (rawOp === "lt") return { field: "amount_lt", op: "lt", value: cents };
    if (rawOp === "exact") return { field: "amount_exact", op: "eq", value: cents };
    return { field: "amount_gt", op: "gt", value: cents };
  }
  if (rawField === "tx_type") {
    return { field: "tx_type", op: "exact", value: rawVal.toLowerCase() };
  }
  return {
    field: rawField,
    op: ["contains", "starts_with", "ends_with", "exact", "regex"].includes(rawOp) ? rawOp : "contains",
    value: rawVal,
  };
}

function buildActionFromForm() {
  const kind = ($("rl-action-type") && $("rl-action-type").value) || "set_jar";
  const rawVal = ($("rl-action-val") && $("rl-action-val").value.trim()) || "";
  if (!rawVal) throw new Error("Enter an action value.");

  if (kind === "set_jar") {
    if (/^\d+$/.test(rawVal)) return { kind, value: Number(rawVal) };
    const jars = (state.dashboard && state.dashboard.jars) || [];
    const found = jars.find(
      (j) =>
        String(j.slug || "").toLowerCase() === rawVal.toLowerCase() ||
        String(j.name || "").toLowerCase().includes(rawVal.toLowerCase())
    );
    return { kind, value: found ? Number(found.id) : rawVal };
  }
  if (kind === "allocate_goal") {
    if (/^\d+$/.test(rawVal)) return { kind, value: Number(rawVal) };
    const found = dndState.goals.find((g) => String(g.name || "").toLowerCase().includes(rawVal.toLowerCase()));
    return { kind, value: found ? Number(found.id) : rawVal };
  }
  return { kind, value: rawVal };
}

function formatTriggerSummary(t) {
  const neg = t.negate || t.not ? "NOT " : "";
  const f = t.field || t.type || "field";
  const op = t.op || t.operator || "";
  let valStr = t.value;
  if (String(f).startsWith("amount_") && Number.isFinite(Number(t.value))) {
    valStr = fmtMoney(Number(t.value));
  }
  return `${neg}${f} ${op} "${valStr ?? ""}"`.trim();
}

function formatActionSummary(a) {
  const k = a.kind || a.type || "action";
  return `${k.replace(/_/g, " ")} -> ${typeof a.value === "object" ? JSON.stringify(a.value) : a.value ?? ""}`;
}

function renderDryRunOutput(rule, res) {
  const box = $("rule-dryrun-output");
  if (!box) return;
  box.classList.remove("hidden");

  const samples = Array.isArray(res.samples) ? res.samples : [];
  const closeBtn = el("button", { type: "button", class: "btn btn-ghost btn-xs", text: "Dismiss" });
  closeBtn.addEventListener("click", () => box.classList.add("hidden"));

  const applyBtn = el(
    "button",
    { type: "button", class: "btn btn-primary btn-xs" },
    [makeIcon("play", "ui-icon-sm"), `Apply to ${res.matched_count || 0} Match(es)`]
  );
  applyBtn.addEventListener("click", () => withBusy(applyBtn, () => applyRuleRetroactively(rule)));

  const sampleNodes = samples.length
    ? samples.slice(0, 8).map((s) =>
        el("div", { class: "item-row small", style: { borderTop: "1px solid var(--border)", paddingTop: "5px" } }, [
          el("span", { class: "tabular-nums", text: `${s.tx_date} · ${s.merchant}` }),
          el("span", { class: "tabular-nums", style: { fontWeight: "700" }, text: fmtMoney(s.amount_cents) }),
          el("span", {
            class: "muted small",
            text: Array.isArray(s.proposed_changes) ? s.proposed_changes.join(", ") : "",
          }),
        ])
      )
    : [el("div", { class: "muted small", text: "Zero transactions matched over the trailing 730 days." })];

  box.style.display = "flex";
  box.style.flexDirection = "column";
  box.style.alignItems = "stretch";
  box.replaceChildren(
    el("div", { class: "item-row" }, [
      el("span", { class: "inline-icon-row" }, [
        makeIcon("filter", "ui-icon-sm"),
        el("strong", {
          text: `Match Preview "${rule.name}": ${res.matched_count || 0} matched of ${res.scanned_count || 0} transactions`,
        }),
      ]),
      el("div", { class: "row-gap" }, [res.matched_count > 0 ? applyBtn : null, closeBtn]),
    ]),
    el("div", { class: "stack-xs" }, sampleNodes)
  );
}

async function dryRunRule(rule, btn = null) {
  await withBusy(btn, async () => {
    try {
      const res = await api(`/api/v1/rules/${rule.id}/test`, { method: "POST" });
      renderDryRunOutput(rule, res);
    } catch (err) {
      toast(err.message || "Dry-run failed.", true);
    }
  });
}

async function applyRuleRetroactively(rule, btn = null) {
  await withBusy(btn, async () => {
    try {
      const res = await api(`/api/v1/rules/${rule.id}/apply`, { method: "POST" });
      const count = Number((res && (res.updated_count ?? res.applied_count ?? res.matched_count)) || 0);
      toast(`Applied "${rule.name}" across ledger (${count} transaction(s) updated).`);
      await refreshAll();
    } catch (err) {
      toast(err.message || "Could not apply rule.", true);
    }
  });
}

function buildRuleEditForm(rule) {
  const form = el("form", { class: "inline-editor form-grid-compact" }, [
    el("input", { name: "name", type: "text", value: rule.name || "", required: true }),
    el("select", { name: "strict_and" }, [
      el("option", { value: "true", text: "Strict AND (all)", selected: Boolean(rule.strict_and) }),
      el("option", { value: "false", text: "ANY (or)", selected: !rule.strict_and }),
    ]),
    el("input", { name: "priority", type: "number", min: "0", max: "1000", value: String(rule.priority ?? 10) }),
    el("label", { class: "checkbox-row" }, [
      el("input", { name: "stop_processing", type: "checkbox", checked: Boolean(rule.stop_processing) }),
      el("span", { text: "Stop processing" }),
    ]),
    el("div", { class: "row-gap" }, [
      el("button", { type: "submit", class: "btn btn-primary btn-xs", text: "Save" }),
      el("button", { type: "button", class: "btn btn-ghost btn-xs", text: "Cancel", "data-cancel-rule": "1" }),
    ]),
  ]);

  form.querySelector("[data-cancel-rule]").addEventListener("click", () => {
    dndState.editingRuleId = null;
    renderRulesList();
  });

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const btn = form.querySelector("button[type=submit]");
    await withBusy(btn, async () => {
      try {
        await api(`/api/v1/rules/${rule.id}`, {
          method: "PATCH",
          body: JSON.stringify({
            name: form.elements.name.value.trim(),
            strict_and: form.elements.strict_and.value === "true",
            priority: parseInt(form.elements.priority.value, 10) || 10,
            stop_processing: form.elements.stop_processing.checked,
          }),
        });
        dndState.editingRuleId = null;
        toast("Automation rule updated.");
        await fetchRules();
      } catch (err) {
        toast(err.message || "Could not update rule.", true);
      }
    });
  });

  return form;
}

function renderRulesList() {
  const host = $("rules-list");
  if (!host) return;

  if (!dndState.rules.length) {
    host.replaceChildren(
      renderEmptyState({
        icon: "filter",
        title: "No auto-categorize rules yet",
        subtitle: "Create a rule to automatically sort merchants into jars, add tags, or assign notes.",
        actionLabel: "+ New Rule",
        onAction: () => {
          const f = $("rule-create-form");
          if (f) {
            f.classList.remove("hidden");
            if ($("rl-name")) $("rl-name").focus();
          }
        },
        compact: true,
      })
    );
    return;
  }

  const cards = dndState.rules.map((r) => {
    const toggleBtn = el("button", {
      type: "button",
      class: `btn btn-xs ${r.active ? "btn-win" : "btn-ghost"}`,
      text: r.active ? "Active" : "Paused",
    });
    toggleBtn.addEventListener("click", () =>
      withBusy(toggleBtn, async () => {
        try {
          await api(`/api/v1/rules/${r.id}`, {
            method: "PATCH",
            body: JSON.stringify({ active: !r.active }),
          });
          await fetchRules();
        } catch (err) {
          toast(err.message || "Could not toggle rule.", true);
        }
      })
    );

    const testBtn = el(
      "button",
      { type: "button", class: "btn btn-soft btn-xs" },
      [makeIcon("eye", "ui-icon-sm"), "Preview Matches"]
    );
    testBtn.addEventListener("click", () => dryRunRule(r, testBtn));

    const applyBtn = el(
      "button",
      { type: "button", class: "btn btn-primary btn-xs" },
      [makeIcon("play", "ui-icon-sm"), "Apply"]
    );
    applyBtn.addEventListener("click", () => applyRuleRetroactively(r, applyBtn));

    const editBtn = el(
      "button",
      { type: "button", class: "btn btn-ghost btn-xs" },
      [makeIcon("edit", "ui-icon-sm"), "Edit"]
    );
    editBtn.addEventListener("click", () => {
      dndState.editingRuleId = dndState.editingRuleId === r.id ? null : r.id;
      renderRulesList();
    });

    const delBtn = el(
      "button",
      { type: "button", class: "btn btn-danger btn-xs", "aria-label": `Delete rule ${r.name}` },
      [makeIcon("trash", "ui-icon-sm")]
    );
    delBtn.addEventListener("click", () =>
      withBusy(delBtn, async () => {
        try {
          await api(`/api/v1/rules/${r.id}`, { method: "DELETE" });
          toast(`Deleted rule "${r.name}".`);
          await fetchRules();
        } catch (err) {
          toast(err.message || "Could not delete rule.", true);
        }
      })
    );

    const triggers = Array.isArray(r.triggers) ? r.triggers : [];
    const actions = Array.isArray(r.actions) ? r.actions : [];

    return el("div", { class: "item-box", style: { opacity: r.active ? "1" : "0.68" } }, [
      el("div", { class: "item-row" }, [
        el("div", { class: "row-gap wrap", style: { gap: "6px" } }, [
          el("span", { class: "pill pill-soft", text: `P${r.priority ?? 10}` }),
          el("strong", { text: r.name }),
          el("span", {
            class: `pill ${r.strict_and ? "pill-blue" : "pill-watch"}`,
            text: r.strict_and ? "Match All" : "Match Any",
          }),
          r.stop_processing ? el("span", { class: "pill pill-danger", text: "Stop After Match" }) : null,
        ]),
        el("div", { class: "row-gap wrap" }, [toggleBtn, testBtn, applyBtn, editBtn, delBtn]),
      ]),
      el("div", { class: "chip-row", style: { marginTop: "2px" } }, [
        el("span", { class: "chip-label", text: "When:" }),
        ...triggers.map((t) => el("span", { class: "pill pill-soft", text: formatTriggerSummary(t) })),
        el("span", { class: "chip-label", style: { marginLeft: "6px" }, text: "Then:" }),
        ...actions.map((a) => el("span", { class: "pill pill-calm", text: formatActionSummary(a) })),
      ]),
      dndState.editingRuleId === r.id ? buildRuleEditForm(r) : null,
    ]);
  });

  host.replaceChildren(...cards);
}

/* ============================================================================
   Module Bootstrap
   ============================================================================ */

export function initGoalsDnd() {
  const newGoalBtn = $("btn-goals-new-toggle");
  const goalForm = $("goal-quick-create-form");
  if (newGoalBtn && goalForm) {
    newGoalBtn.addEventListener("click", () => {
      goalForm.classList.toggle("hidden");
      if (!goalForm.classList.contains("hidden") && $("gq-name")) {
        $("gq-name").focus();
      }
    });
  }

  const impulseBtn = $("btn-goals-open-impulse");
  if (impulseBtn) {
    impulseBtn.addEventListener("click", () => openDrawer("impulse-drawer-overlay"));
  }

  if (goalForm) {
    goalForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = goalForm.querySelector("button[type=submit]");
      await withBusy(btn, async () => {
        try {
          const rawCat = ($("gq-category") && $("gq-category").value) || "emergency";
          const catMap = {
            savings: "emergency",
            emergency: "emergency",
            gadget: "gadget",
            travel: "travel",
            debt: "debt_freedom",
            debt_freedom: "debt_freedom",
            invest: "invest",
            custom: "custom",
          };
          const body = {
            name: $("gq-name").value.trim(),
            target_cents: dollarsToCents($("gq-target").value),
            saved_cents: dollarsToCents($("gq-saved").value || "0", { allowZero: true }),
            category: catMap[rawCat] || "custom",
          };
          if ($("gq-date") && $("gq-date").value) body.target_date = $("gq-date").value;
          await api("/api/v1/goals", { method: "POST", body: JSON.stringify(body) });
          goalForm.reset();
          goalForm.classList.add("hidden");
          toast(`Created goal "${body.name}".`);
          await fetchGoals();
          await refreshAll();
        } catch (err) {
          toast(err.message || "Could not create goal.", true);
        }
      });
    });
  }

  // $1,000 Purchase Optimizer
  const planForm = $("goal-plan-form");
  if (planForm) {
    planForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = planForm.querySelector("button[type=submit]");
      await withBusy(btn, async () => {
        try {
          const body = {
            item_name: $("gp-name").value.trim(),
            target_cents: dollarsToCents($("gp-price").value),
            saved_cents: dollarsToCents($("gp-saved").value || "0", { allowZero: true }),
            prefer_credit_rewards: true,
          };
          if ($("gp-date") && $("gp-date").value) body.target_date = $("gp-date").value;
          const plan = await api("/api/v1/goals/plan", { method: "POST", body: JSON.stringify(body) });
          renderGoalPlan(plan);
        } catch (err) {
          toast(err.message || "Could not compute goal plan.", true);
        }
      });
    });
  }

  // Firefly Automation Rule Form
  const newRuleBtn = $("btn-rules-new-toggle");
  const ruleForm = $("rule-create-form");
  if (newRuleBtn && ruleForm) {
    newRuleBtn.addEventListener("click", () => {
      ruleForm.classList.toggle("hidden");
      if (!ruleForm.classList.contains("hidden") && $("rl-name")) {
        $("rl-name").focus();
      }
    });
  }

  if (ruleForm) {
    ruleForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = ruleForm.querySelector("button[type=submit]");
      await withBusy(btn, async () => {
        try {
          const trigger = buildTriggerFromForm();
          const action = buildActionFromForm();
          const body = {
            name: $("rl-name").value.trim(),
            strict_and: ($("rl-match-mode") && $("rl-match-mode").value) !== "any",
            priority: parseInt(($("rl-priority") && $("rl-priority").value) || "10", 10) || 10,
            stop_processing: Boolean($("rl-stop") && $("rl-stop").checked),
            triggers: [trigger],
            actions: [action],
            active: true,
          };
          await api("/api/v1/rules", { method: "POST", body: JSON.stringify(body) });
          ruleForm.reset();
          ruleForm.classList.add("hidden");
          toast(`Automation rule "${body.name}" saved.`);
          await fetchRules();
        } catch (err) {
          toast(err.message || "Could not create automation rule.", true);
        }
      });
    });
  }

  registerDashboardHook((dash) => {
    if (dash && Array.isArray(dash.goals)) {
      dndState.goals = sortGoalsByPriority(dash.goals);
      renderGoalsList();
    }
  });

  registerTabHook("goals", async () => {
    await Promise.all([fetchGoals(), fetchRules()]);
  });

  registerRefreshHook(async () => {
    if (state.activeTab === "goals") {
      await Promise.all([fetchGoals(), fetchRules()]);
    }
  });
}
