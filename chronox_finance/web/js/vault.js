/**
 * ChronoX Finance — Vault Gate, Onboarding, Command, Activity/Splits/Bills & Connectors Module
 * Handles mandatory envelope-encrypted launch lock, 1-time Recovery Key 4-word verify,
 * 3-track first-run onboarding, Command dashboard, 2-year ledger with Splits/Tags/Comments,
 * Plaid Hosted Link / SimpleFIN / IMAP Email Scanner, and Multi-Provider AI settings.
 */

import {
  $,
  HERO_RING_CIRCUMFERENCE,
  animateMoney,
  animatePercent,
  api,
  clearBootSkeletons,
  choreographEntrance,
  closeDrawer,
  closeOverlay,
  dispatchDashboardHooks,
  dollarsToCents,
  el,
  fmtMoney,
  fmtShortDate,
  getDesktopBridge,
  isoDateOffset,
  jarIconKey,
  makeIcon,
  openDrawer,
  openOverlay,
  positionTabIndicator,
  pushTraySnapshot,
  refreshAll,
  registerTabHook,
  setCsrfToken,
  setNodeIconText,
  setVaultGateHandler,
  setVaultTitlebarState,
  state,
  switchTab,
  syncModeControls,
  syncProviderControls,
  toast,
  triggerCelebrate,
  withBusy,
} from "./core.js";

let _pendingRecoveryData = null;
let _plaidPollTimer = null;
let _onUnlockedCallback = null;
let _activeSplitsDraft = [];
let _activeTagsDraft = [];
let _activeTxObj = null;

/* ============================================================================
   1. MANDATORY LAUNCH VAULT GATE (Setup -> 4-Word Verify -> Unlock -> Recover)
   ============================================================================ */

function showVaultPane(paneName) {
  const panes = {
    setup: $("vault-pane-setup"),
    verify: $("vault-pane-recovery-verify"),
    unlock: $("vault-pane-unlock"),
    recover: $("vault-pane-recover"),
  };
  for (const [k, node] of Object.entries(panes)) {
    if (node) node.classList.toggle("hidden", k !== paneName);
  }
  const title = $("vault-gate-title");
  if (title) {
    const titles = {
      setup: "INITIALIZE ENCRYPTED VAULT",
      verify: "RECORD ONE-TIME RECOVERY KEY",
      unlock: "CHRONOX VAULT LOCKED",
      recover: "RECOVER VAULT WITH RECOVERY KEY",
    };
    title.textContent = titles[paneName] || "CHRONOX VAULT LOCKED";
  }
}

export function openVaultGate(mode = "locked", reason = "") {
  clearBootSkeletons();
  stopPlaidPolling();
  const normalized = mode === "needs_setup" ? "setup" : mode === "recover" ? "recover" : "unlock";
  state.vaultStatus = mode === "needs_setup" ? "needs_setup" : "locked";
  setVaultTitlebarState("locked", mode === "needs_setup" ? "VAULT SETUP" : "VAULT LOCKED");
  pushTraySnapshot();

  showVaultPane(normalized);
  if (reason && normalized === "unlock") {
    const sub = $("vault-unlock-subtitle");
    if (sub) sub.textContent = reason;
  }

  openOverlay($("vault-overlay"));
  requestAnimationFrame(() => {
    const focusTarget =
      normalized === "setup"
        ? $("vault-setup-pwd")
        : normalized === "recover"
        ? $("vault-recover-key")
        : $("vault-unlock-pwd");
    if (focusTarget) focusTarget.focus();
  });
}

function closeVaultGate() {
  state.vaultStatus = "unlocked";
  setVaultTitlebarState("active", "LOCAL VAULT ACTIVE");
  closeOverlay($("vault-overlay"));
  pushTraySnapshot();
}

function parseRecoveryTokens(resp) {
  if (Array.isArray(resp.recovery_groups) && resp.recovery_groups.length >= 4) {
    return resp.recovery_groups.map((g) => String(g).trim());
  }
  const raw = String(resp.recovery_key || "").trim();
  if (!raw) return [];
  if (raw.includes("-")) return raw.split("-").map((s) => s.trim()).filter(Boolean);
  return raw.split(/\s+/).map((s) => s.trim()).filter(Boolean);
}

function renderRecoveryVerifyPane(setupResp) {
  const tokens = parseRecoveryTokens(setupResp);
  const rawIndices =
    Array.isArray(setupResp.challenge_indices) && setupResp.challenge_indices.length > 0
      ? setupResp.challenge_indices.map((i) => Number(i)).filter((i) => Number.isInteger(i))
      : [];
  // security.py's build_recovery_challenge returns 1-based indices (1..tokens.length); normalize to 0-based.
  const isOneBased =
    rawIndices.length > 0 && rawIndices.every((i) => i >= 1 && i <= tokens.length);
  let indices = (isOneBased ? rawIndices.map((i) => i - 1) : rawIndices).filter(
    (i) => i >= 0 && i < tokens.length
  );
  if (indices.length !== 4 && tokens.length >= 4) {
    const step = Math.max(1, Math.floor(tokens.length / 4));
    indices = [0, step, step * 2, Math.min(tokens.length - 1, step * 3)];
  }
  _pendingRecoveryData = {
    recoveryKey: String(setupResp.recovery_key || tokens.join("-")),
    tokens,
    indices,
    onboardingCompleted: Boolean(setupResp.status && setupResp.status.onboarding_completed),
  };

  const keyBox = $("vault-recovery-key-box");
  if (keyBox) {
    keyBox.replaceChildren(
      ...tokens.map((tok, idx) =>
        el("div", { class: "recovery-word" }, [
          el("span", { class: "recovery-word-idx", text: String(idx + 1).padStart(2, "0") }),
          el("strong", { text: tok }),
        ])
      )
    );
  }

  const promptEl = $("vault-verify-prompt");
  if (promptEl) {
    const labels = indices.map((i) => `#${i + 1}`).join(", ");
    promptEl.textContent = `Enter groups ${labels} exactly to confirm you have recorded your recovery key:`;
  }

  const inputsWrap = $("vault-verify-inputs");
  if (inputsWrap) {
    inputsWrap.replaceChildren(
      ...indices.map((tokIdx) =>
        el("label", { class: "field" }, [
          el("span", { text: `Group #${tokIdx + 1}` }),
          el("input", {
            type: "text",
            autocomplete: "off",
            spellcheck: "false",
            required: "true",
            placeholder: `Enter #${tokIdx + 1}`,
            "data-verify-idx": String(tokIdx),
          }),
        ])
      )
    );
  }

  const err = $("vault-verify-error");
  if (err) err.classList.add("hidden");
  showVaultPane("verify");
}

export async function checkVaultBootStatus(onUnlocked) {
  _onUnlockedCallback = onUnlocked || null;
  setVaultGateHandler((mode, reason) => openVaultGate(mode, reason));

  try {
    const st = await api("/api/v1/auth/status");
    if (st.csrf_token) setCsrfToken(st.csrf_token);
    state.onboardingCompleted = Boolean(st.onboarding_completed);
    state.vaultStatus = st.state || (st.unlocked ? "unlocked" : st.initialized ? "locked" : "needs_setup");

    if (state.vaultStatus === "unlocked" && st.authenticated) {
      closeVaultGate();
      if (_onUnlockedCallback) await _onUnlockedCallback();
      if (!state.onboardingCompleted) openOnboarding();
      return true;
    }

    openVaultGate(state.vaultStatus);
    return false;
  } catch (err) {
    openVaultGate("locked", err && err.message ? err.message : "Unable to verify vault status.");
    return false;
  }
}

async function lockVaultNow() {
  try {
    await api("/api/v1/auth/logout", { method: "POST" });
  } catch (_err) {
    // Even if session already expired, enforce locked UI immediately
  }
  state.dashboard = null;
  state.transactions = [];
  state.connections = [];
  openVaultGate("locked", "Vault locked. Enter your master password to resume.");
  toast("Vault locked.");
}

/* ============================================================================
   2. DISMISSIBLE 3-TRACK FIRST-RUN ONBOARDING
   ============================================================================ */

export function openOnboarding() {
  openOverlay($("onboarding-overlay"));
}

async function completeOnboarding(track = "skipped") {
  closeOverlay($("onboarding-overlay"));
  state.onboardingCompleted = true;
  try {
    await api("/api/v1/onboarding/complete", {
      method: "POST",
      body: JSON.stringify({ track }),
    });
  } catch (_err) {
    // Non-blocking: user already dismissed modal
  }
}

/* ============================================================================
   3. COMMAND DASHBOARD, JARS, IMPULSE DRAWER & 5-MIN RESET DRAWER
   ============================================================================ */

function mathLine(label, value, bold = false) {
  return el("div", { class: "math-line" }, [
    el(bold ? "strong" : "span", { text: label }),
    el(bold ? "strong" : "span", { class: "num", text: value }),
  ]);
}

export function renderCommandDashboard(dash) {
  state.dashboard = dash;
  const s = dash.safe_to_spend || {};
  const stats = dash.adhd_stats || {
    level: 1,
    dopamine_xp: 0,
    weekly_reset_streak: 0,
    impulse_money_saved_cents: 0,
    level_title: "Operator",
    grace_days_available: 1,
    recent_wins: [],
  };

  const xpLv = $("xp-level");
  const xpSt = $("xp-streak");
  if (xpLv) xpLv.textContent = `LV ${stats.level} · ${stats.dopamine_xp} XP`;
  if (xpSt) xpSt.textContent = `${stats.weekly_reset_streak}W`;

  if (dash.intelligence_mode) syncModeControls(dash.intelligence_mode);
  if (dash.active_ai_provider) {
    const pref = (state.settings && state.settings.ai_provider) || "auto";
    syncProviderControls(pref, dash.active_ai_provider);
  }

  const impCount = (dash.cooling_impulses || []).length;
  const bImp = $("badge-impulse");
  if (bImp) {
    bImp.textContent = String(impCount);
    bImp.classList.toggle("hidden", impCount === 0);
  }

  const triageCount = Number(dash.needs_triage_count || 0);
  const bTri = $("badge-triage");
  const bResetTri = $("badge-reset-triage");
  if (bTri) {
    bTri.textContent = String(triageCount);
    bTri.classList.toggle("hidden", triageCount === 0);
  }
  if (bResetTri) {
    bResetTri.textContent = String(triageCount);
    bResetTri.classList.toggle("hidden", triageCount === 0);
  }

  const creditAlerts = (dash.credit_summary && dash.credit_summary.alerts_count) || 0;
  const bCred = $("badge-credit");
  if (bCred) {
    bCred.textContent = String(creditAlerts);
    bCred.classList.toggle("hidden", creditAlerts === 0);
  }

  // Hero Safe-to-Spend Gauge
  animateMoney($("hero-daily-safe"), s.daily_safe_to_spend_cents || 0, "heroDailySafe");
  animateMoney($("hero-total-safe"), s.safe_to_spend_cents || 0, "heroTotalSafe");
  const payDate = $("hero-payday-date");
  const payChip = $("payday-chip");
  if (payDate) payDate.textContent = fmtShortDate(s.next_payday_date);
  if (payChip) payChip.textContent = `${s.days_until_payday ?? "--"}D UNTIL PAYDAY`;

  const liquid = Math.max(0, Number(s.total_liquid_cash_cents) || 0);
  const safeTotal = Math.max(0, Number(s.safe_to_spend_cents) || 0);
  const ratio = liquid > 0 ? Math.max(0, Math.min(1, safeTotal / liquid)) : 0;
  const ringOffset = HERO_RING_CIRCUMFERENCE * (1 - ratio);
  const ringProg = $("hero-ring-progress");
  if (ringProg) {
    requestAnimationFrame(() => {
      ringProg.setAttribute("stroke-dashoffset", ringOffset.toFixed(2));
    });
  }
  animatePercent($("hero-ring-pct"), ratio * 100, "heroRingPct", 0);
  const ringLabel = $("hero-ring-label");
  if (ringLabel) ringLabel.textContent = ratio >= 0.2 ? "FREE CASH" : ratio > 0 ? "TIGHT" : "SHIELDED";

  const mathBox = $("safe-math-breakdown");
  if (mathBox) {
    mathBox.replaceChildren(
      mathLine("Liquid cash (checking + savings + cash)", fmtMoney(s.total_liquid_cash_cents)),
      mathLine("- Unpaid bills due before payday", `-${fmtMoney(s.unpaid_bills_until_payday_cents)}`),
      mathLine("- Credit card payoff reserve", `-${fmtMoney(s.credit_card_reserved_cents)}`),
      mathLine("- Ring-fenced Safety & Invest jars", `-${fmtMoney(s.locked_jar_reserves_cents)}`),
      mathLine("- Emergency overdraft buffer", `-${fmtMoney(s.emergency_buffer_cents)}`),
      mathLine("= Safe to Spend Until Payday", fmtMoney(s.safe_to_spend_cents), true),
      mathLine(
        `/ ${s.days_until_payday || 1} days = Daily Safe Allowance`,
        fmtMoney(s.daily_safe_to_spend_cents),
        true
      )
    );
  }

  animateMoney($("hero-safe-invest"), dash.safe_to_invest_cents || 0, "heroSafeInvest");
  animateMoney($("hero-impulse-saved"), stats.impulse_money_saved_cents || 0, "heroImpulseSaved");
  const lvTitle = $("hero-level-title");
  const graceNote = $("hero-grace-note");
  if (lvTitle) lvTitle.textContent = stats.level_title || "Operator";
  if (graceNote) graceNote.textContent = `${stats.grace_days_available ?? 1} grace freeze ready`;

  const cs = dash.credit_summary || {};
  const gCount = (dash.goals || []).length;
  const pulse = $("hero-credit-pulse");
  if (pulse) {
    if (cs.total_debt_cents > 0 || cs.aggregate_utilization_pct > 0) {
      pulse.textContent =
        `UTIL: ${Number(cs.aggregate_utilization_pct || 0).toFixed(1)}% · ` +
        `DTI: ${Number(cs.dti_pct || 0).toFixed(1)}% · ` +
        `DEBT: ${fmtMoney(cs.total_debt_cents || 0)} · ${gCount} ACTIVE GOAL(S)`;
    } else {
      pulse.textContent = `ZERO TRACKED DEBT · ${gCount} ACTIVE GOAL(S) — OPEN CREDIT OR GOALS ANYTIME.`;
    }
  }

  // Rebalance Banner
  const rebalBanner = $("rebalance-banner");
  const plans = dash.rebalance_plans || [];
  if (rebalBanner) {
    if (plans.length > 0) {
      rebalBanner.classList.remove("hidden");
      rebalBanner.replaceChildren(
        el("strong", { class: "inline-icon-row" }, [
          makeIcon("shield", "ui-icon-sm"),
          "ZERO-GUILT AUTO-REBALANCE READY",
        ]),
        ...plans.map((p) =>
          el("div", { class: "rebal-row" }, [
            el("span", { text: p.calm_message }),
            el("button", {
              type: "button",
              class: "btn btn-primary btn-xs",
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
  }

  renderJars(dash.jars || []);
  renderBillRadar(dash.upcoming_bills || []);
  renderImpulses(dash.cooling_impulses || [], stats);
  renderDopamineWins(stats.recent_wins || []);
  renderAccounts(dash.accounts || []);
  renderBillAnomalies(dash.bill_anomalies || []);
  renderAllBills(dash.upcoming_bills || []);
  renderAuditLog(dash.recent_budget_events || []);
  populateSelectors(dash.jars || [], dash.accounts || []);

  clearBootSkeletons();
  setVaultTitlebarState("active", "LOCAL VAULT ACTIVE");
  if (!state.hydrated) {
    state.hydrated = true;
    choreographEntrance($("tab-today"));
  }
  requestAnimationFrame(() => positionTabIndicator(true));
  pushTraySnapshot();
  dispatchDashboardHooks(dash);
}

export function renderJars(jars) {
  const jarsGrid = $("jars-grid");
  if (!jarsGrid) return;
  jarsGrid.replaceChildren(
    ...jars.map((j) => {
      const needsSetup = Boolean(j.needs_setup);
      const pct = Math.min(100, Math.max(0, Number(j.pct_used) || 0));
      const healthClass = needsSetup ? "pill pill-soft" : `pill pill-${j.health || "calm"}`;
      const healthLabel = needsSetup
        ? "SETUP · $0 FUNDED"
        : { calm: "NOMINAL", watch: "WATCH PACE", rebalance: "REBALANCE" }[j.health] ||
          String(j.health || "NOMINAL").toUpperCase();
      const isEditing = state.editingJarId === j.id;
      const fundedCents = Number(j.funded_cents ?? j.auto_allocated_cents ?? 0);
      const targetCents = Number(j.monthly_target_cents || 0);
      const projectedCents = Number(j.projected_allocation_cents || 0);

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
            el("label", { class: "checkbox-row details-spacer" }, [
              el("input", { type: "checkbox", name: "rollover_enabled", checked: j.rollover_enabled }),
              el("span", { text: "Roll over unused surplus" }),
            ]),
            el("label", { class: "checkbox-row" }, [
              el("input", { type: "checkbox", name: "is_locked", checked: j.is_locked }),
              el("span", { text: "Ring-fence (shield from Safe-to-Spend)" }),
            ]),
            el("div", { class: "row-gap details-spacer" }, [
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

      const metaLine = needsSetup
        ? `Cash funded: ${fmtMoney(fundedCents)} · Target: ${fmtMoney(targetCents)}/mo (${Number(j.target_pct || 0).toFixed(0)}%)`
        : `Spent ${fmtMoney(j.spent_cents)} / ${fmtMoney(fundedCents)} funded (${Number(j.target_pct || 0).toFixed(0)}%${targetCents > 0 ? ` · Target ${fmtMoney(targetCents)}/mo` : ""})`;

      return el("div", { class: "jar-card", style: { borderTopColor: j.color || "var(--primary)" } }, [
        el("div", { class: "jar-top" }, [
          el("span", { class: "jar-name" }, [
            el("span", { class: "jar-icon-badge", style: { color: j.color || "var(--primary)" } }, [
              makeIcon(jarIconKey(j), "ui-icon-sm"),
            ]),
            j.name,
          ]),
          el("div", { class: "row-gap" }, [
            el("span", { class: healthClass, text: healthLabel }),
            el(
              "button",
              {
                type: "button",
                class: "btn btn-ghost btn-xs",
                title: `Edit ${j.name}`,
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
          style: { color: j.available_cents < 0 ? "var(--crimson)" : needsSetup ? "var(--muted)" : j.color || "var(--text)" },
        }),
        el("div", { class: "progress-track" }, [
          el("div", {
            class: "progress-fill",
            style: {
              width: `${pct}%`,
              backgroundColor: j.available_cents < 0 ? "var(--crimson)" : j.color || "var(--primary)",
            },
          }),
        ]),
        el("div", {
          class: "muted small",
          text: metaLine,
        }),
        needsSetup && projectedCents > 0
          ? el("div", {
              class: "muted small",
              text: `Projected allocation: ${fmtMoney(projectedCents)}/mo (pending cash deposit)`,
            })
          : null,
        j.rollover_cents > 0
          ? el("div", { class: "muted small", text: `+${fmtMoney(j.rollover_cents)} rolled over` })
          : null,
        el("div", { class: "small muted", text: j.calm_note || "" }),
        editor,
      ]);
    })
  );
}

function renderBillRadar(upcomingBills) {
  const bills14 = upcomingBills.filter((b) => b.days_until_due <= 14);
  const dueSum = bills14.reduce((acc, b) => acc + (b.amount_cents || 0), 0);
  const pill = $("bills-total-pill");
  if (pill) pill.textContent = `${fmtMoney(dueSum)} due in 14d`;

  const radar = $("bill-radar-list");
  if (!radar) return;
  if (!bills14.length) {
    radar.replaceChildren(el("p", { class: "muted small", text: "No bills due in the next 14 days." }));
    return;
  }
  radar.replaceChildren(
    ...bills14.map((b) => {
      const dueLabel =
        b.days_until_due <= 0 ? "DUE TODAY" : `IN ${b.days_until_due}D (${fmtShortDate(b.next_due_date)})`;
      const rangeStr =
        b.amount_min_cents != null && b.amount_max_cents != null
          ? ` (${fmtMoney(b.amount_min_cents)}-${fmtMoney(b.amount_max_cents)})`
          : "";
      const pills = [el("span", { class: "pill pill-soft", text: dueLabel })];
      if (b.price_hike_cents > 0) {
        pills.push(el("span", { class: "pill pill-watch", text: `+${fmtMoney(b.price_hike_cents)} HIKE` }));
      }
      return el("div", { class: "item-box" }, [
        el("div", { class: "item-row" }, [
          el("strong", { text: b.name }),
          el("strong", { class: "num", text: `${fmtMoney(b.amount_cents)}${rangeStr}` }),
        ]),
        el("div", { class: "item-row" }, pills),
      ]);
    })
  );
}

function renderImpulses(impulses, stats) {
  const statsEl = $("impulse-vault-stats");
  if (statsEl) {
    statsEl.textContent = `${stats.impulses_skipped_count || 0} skipped · ${fmtMoney(stats.impulse_money_saved_cents || 0)} kept`;
  }
  const list = $("impulse-list");
  if (!list) return;
  if (!impulses.length) {
    list.replaceChildren(
      el("div", { class: "item-box muted small", text: "No items cooling right now. Park an impulse anytime." })
    );
    return;
  }
  list.replaceChildren(
    ...impulses.map((imp) => {
      const ready = imp.status === "ready";
      const badge = ready
        ? el("span", { class: "pill pill-calm", text: "COOLDOWN COMPLETE" })
        : el("span", { class: "pill pill-blue" }, [makeIcon("snowflake", "ui-icon-sm"), `${imp.hours_remaining}h left`]);
      return el("div", { class: "jar-card" }, [
        el("div", { class: "jar-top" }, [el("strong", { text: imp.item_name }), badge]),
        el("div", { class: "jar-avail", text: fmtMoney(imp.price_cents) }),
        el("div", {
          class: "muted small",
          text: `${imp.work_hours_cost} work hrs · delays goal ~${imp.goal_delay_days}d`,
        }),
        imp.reason ? el("div", { class: "small muted", text: `Trigger: ${imp.reason}` }) : null,
        el("div", { class: "row-gap wrap" }, [
          el(
            "button",
            {
              type: "button",
              class: "btn btn-win btn-xs",
              "data-impulse-decide": String(imp.id),
              "data-decision": "skip",
            },
            [makeIcon("trophy", "ui-icon-sm"), "Skipped (+75 XP)"]
          ),
          el("button", {
            type: "button",
            class: "btn btn-ghost btn-xs",
            text: imp.can_afford_safely ? "Buy (Fits Safe)" : "Buy Anyway",
            "data-impulse-decide": String(imp.id),
            "data-decision": "buy",
          }),
        ]),
      ]);
    })
  );
}

function renderDopamineWins(wins) {
  const targets = [$("dopamine-wins-list"), $("command-recent-wins")].filter(Boolean);
  for (const box of targets) {
    if (!wins || !wins.length) {
      box.replaceChildren(
        el("p", {
          class: "muted small",
          text: "Park an impulse, import statements, or complete a Weekly Reset to log momentum wins.",
        })
      );
      continue;
    }
    const sliced = box.id === "command-recent-wins" ? wins.slice(0, 4) : wins;
    box.replaceChildren(
      ...sliced.map((w) =>
        el("div", { class: "win-item" }, [
          el("span", { class: "inline-icon-row" }, [makeIcon("check", "ui-icon-sm"), String(w)]),
        ])
      )
    );
  }
}

export async function loadWeeklyReset() {
  try {
    const data = await api("/api/v1/weekly-reset");
    const pill = $("reset-status-pill");
    if (pill) {
      pill.className = data.already_completed_this_week ? "pill pill-calm" : "pill pill-watch";
      if (data.already_completed_this_week) setNodeIconText(pill, "check", "DONE THIS WEEK");
      else pill.textContent = "READY (~3 MIN)";
    }

    const triage = $("reset-triage-list");
    const jars = (state.dashboard && state.dashboard.jars) || [];
    if (triage) {
      if (!(data.needs_triage_transactions || []).length) {
        triage.replaceChildren(
          el("p", { class: "small inline-icon-row" }, [
            makeIcon("check", "ui-icon-sm"),
            "Zero uncategorized transactions.",
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
    }

    const subs = $("reset-subs-list");
    if (subs) {
      if (!(data.zombie_subscriptions || []).length) {
        subs.replaceChildren(el("p", { class: "small muted", text: "No creeping subscriptions detected." }));
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
                    class: "btn btn-ghost btn-xs",
                    text: "Archive",
                    "data-archive-bill": String(b.id),
                  }),
                ]),
              ]),
            ])
          )
        );
      }
    }

    const imps = $("reset-impulses-list");
    if (imps) {
      if (!(data.matured_impulses || []).length) {
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
                    class: "btn btn-win btn-xs",
                    "data-impulse-decide": String(imp.id),
                    "data-decision": "skip",
                  },
                  [makeIcon("trophy", "ui-icon-sm"), "Claim Skipped (+75 XP)"]
                ),
              ]),
            ])
          )
        );
      }
    }

    const rebal = $("reset-rebalance-list");
    if (rebal) {
      if (!(data.rebalance_plans || []).length) {
        rebal.replaceChildren(
          el("p", { class: "small inline-icon-row" }, [makeIcon("check", "ui-icon-sm"), "All 5 jars balanced."])
        );
      } else {
        rebal.replaceChildren(...data.rebalance_plans.map((p) => el("div", { class: "item-box small", text: p.calm_message })));
      }
    }
  } catch (err) {
    toast(err.message, true);
  }
}

/* ============================================================================
   4. ACTIVITY, ACCOUNTS, 2Y LEDGER, SPLITS/TAGS/COMMENTS, BILLS & EMAIL QUEUE
   ============================================================================ */

export function renderAccounts(accounts) {
  const box = $("accounts-list");
  if (!box) return;
  if (!accounts.length) {
    box.replaceChildren(el("p", { class: "muted small", text: "No accounts yet. Connect Plaid/SimpleFIN, import a statement, or add manually below." }));
    return;
  }
  box.replaceChildren(
    ...accounts.map((a) => {
      const isReconciling = state.reconcilingAcctId === a.id;
      const currency = String(a.currency || "USD").toUpperCase();
      const nonUsd = currency !== "USD";
      const fmtAcctVal = (cents) =>
        nonUsd ? `${currency} ${(Number(cents || 0) / 100).toFixed(2)}` : fmtMoney(cents);
      const maskSuffix = a.mask ? ` ••••${a.mask}` : "";
      const availNote =
        a.available_cents != null && Number(a.available_cents) !== Number(a.balance_cents)
          ? ` · Avail ${fmtAcctVal(a.available_cents)}`
          : "";

      const reconcileForm = isReconciling
        ? el("form", { class: "inline-editor", "data-reconcile-form": String(a.id) }, [
            el("div", { class: "row-gap" }, [
              el("input", {
                type: "number",
                name: "balance",
                step: "0.01",
                value: (a.balance_cents / 100).toFixed(2),
                placeholder: "Exact balance ($)",
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
            el("div", { class: "row-gap wrap", style: { gap: "6px" } }, [
              el("strong", { text: `${a.name}${maskSuffix}` }),
              nonUsd ? el("span", { class: "pill pill-watch", text: `${currency} · ISOLATED` }) : null,
            ]),
            el("div", {
              class: "muted small",
              text: `${String(a.account_type || "").toUpperCase()} · ${a.institution || "Local"}${availNote}`,
            }),
          ]),
          el("div", { class: "row-gap" }, [
            el("strong", { class: "num", text: fmtAcctVal(a.balance_cents) }),
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

function renderBillAnomalies(anomalies) {
  const box = $("bill-anomalies-list");
  if (!box) return;
  if (!anomalies.length) {
    box.replaceChildren();
    return;
  }
  box.replaceChildren(
    ...anomalies.map((a) =>
      el("div", { class: "notice-bar notice-warn" }, [
        makeIcon("alert", "ui-icon-sm"),
        el("span", { text: a.summary }),
      ])
    )
  );
}

function renderAllBills(bills) {
  const cnt = $("all-bills-count");
  if (cnt) cnt.textContent = `${bills.length} active`;
  const box = $("all-bills-list");
  if (!box) return;
  if (!bills.length) {
    box.replaceChildren(el("p", { class: "muted small", text: "No recurring bills yet. Add one below or import statements." }));
    return;
  }
  box.replaceChildren(
    ...bills.map((b) => {
      const dueStr = b.days_until_due <= 0 ? "Due today" : `Due in ${b.days_until_due}d (${b.next_due_date})`;
      const rangeStr =
        b.amount_min_cents != null && b.amount_max_cents != null
          ? ` · range ${fmtMoney(b.amount_min_cents)}-${fmtMoney(b.amount_max_cents)}`
          : "";
      const winStr = b.due_window_days ? ` · +/-${b.due_window_days}d` : "";
      return el("div", { class: "item-box" }, [
        el("div", { class: "item-row" }, [
          el("div", {}, [
            el("strong", { text: b.name }),
            el("div", { class: "muted small", text: `${dueStr} · ${b.cadence}${rangeStr}${winStr}${b.is_subscription ? " · sub" : ""}` }),
          ]),
          el("strong", { class: "num", text: fmtMoney(b.amount_cents) }),
        ]),
        el("div", { class: "item-row" }, [
          el("div", { class: "row-gap wrap" }, [
            b.price_hike_cents > 0 ? el("span", { class: "pill pill-watch", text: `+${fmtMoney(b.price_hike_cents)} hike` }) : null,
            b.flagged_zombie ? el("span", { class: "pill pill-rebalance", text: "Flagged" }) : null,
          ]),
          el("div", { class: "row-gap" }, [
            el("button", {
              type: "button",
              class: "btn btn-ghost btn-xs",
              text: b.flagged_zombie ? "Keep" : "Flag",
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
  if (!box) return;
  if (!events.length) {
    box.replaceChildren(el("p", { class: "muted small", text: "Auto-budget audit events will appear here." }));
    return;
  }
  box.replaceChildren(
    ...events.map((e) =>
      el("div", { class: "item-box" }, [
        el("div", { class: "small", text: e.summary }),
        el("span", { class: "muted small", text: `${e.event_type} · ${String(e.created_at || "").replace("T", " ").replace("Z", " UTC")}` }),
      ])
    )
  );
}

function populateSelectors(jars, accounts) {
  const filterSel = $("tx-jar-filter");
  if (filterSel) {
    const prev = filterSel.value;
    filterSel.replaceChildren(
      el("option", { value: "", text: "All Jars" }),
      ...jars.map((j) => el("option", { value: String(j.id), text: j.name }))
    );
    filterSel.value = prev;
  }

  const manualJar = $("manual-tx-jar");
  if (manualJar) {
    const prev = manualJar.value;
    manualJar.replaceChildren(
      el("option", { value: "", text: "Auto-Categorize Jar" }),
      ...jars.map((j) => el("option", { value: String(j.id), text: j.name }))
    );
    manualJar.value = prev;
  }

  const manualAcct = $("manual-tx-account");
  if (manualAcct) {
    const prev = manualAcct.value;
    manualAcct.replaceChildren(
      ...accounts.map((a) => el("option", { value: String(a.id), text: `${a.name} (${a.account_type})` }))
    );
    if (prev) manualAcct.value = prev;
  }

  const csvAcct = $("csv-account-select");
  if (csvAcct) {
    const prev = csvAcct.value;
    csvAcct.replaceChildren(
      el("option", { value: "", text: "Auto-Match from Statement / Default Checking" }),
      ...accounts.map((a) => el("option", { value: String(a.id), text: `${a.name} (${a.account_type})` }))
    );
    if (prev) csvAcct.value = prev;
  }
}

const TX_PAGE_SIZE = 500;
let _txRenderLimit = TX_PAGE_SIZE;

export async function loadTransactions() {
  try {
    const q = ($("tx-search") && $("tx-search").value.trim()) || "";
    const jarId = ($("tx-jar-filter") && $("tx-jar-filter").value) || "";
    const range = ($("tx-date-range") && $("tx-date-range").value) || "90";
    const params = new URLSearchParams({ limit: "250000" });
    if (q) params.set("search", q);
    if (jarId) params.set("jar_id", jarId);
    if (range !== "all") {
      const days = parseInt(range, 10) || 90;
      params.set("start_date", isoDateOffset(-days));
    }
    const txs = await api(`/api/v1/transactions?${params.toString()}`);
    state.transactions = Array.isArray(txs) ? txs : [];
    _txRenderLimit = TX_PAGE_SIZE;
    renderTransactionsTable(state.transactions);
  } catch (err) {
    if (err.status !== 401) toast(err.message, true);
  }
}

export function renderTransactionsTable(txs) {
  const jars = (state.dashboard && state.dashboard.jars) || [];
  const tbody = $("tx-table-body");
  const pill = $("tx-count-pill");
  if (pill) pill.textContent = `${txs.length} ${txs.length === 1 ? "item" : "items"}`;
  if (!tbody) return;

  if (!txs.length) {
    tbody.replaceChildren(
      el("tr", {}, [el("td", { colspan: "5", class: "muted small", text: "No matching transactions in selected window." })])
    );
    return;
  }

  const visibleTxs = txs.slice(0, _txRenderLimit);
  const rows = [];
  for (const tx of visibleTxs) {
    const isEditing = state.editingTxId === tx.id;
    const isTransfer = tx.tx_type === "transfer";
    const nonJarLabel = tx.tx_type === "income" ? "Income" : isTransfer ? "Transfer" : "Unassigned";
    if (isEditing) {
      const jarSelect = el("select", { name: "jar_id", "aria-label": "Edit jar" }, [
        el("option", { value: "", text: nonJarLabel }),
        ...jars.map((j) => el("option", { value: String(j.id), text: j.name, selected: tx.jar_id === j.id })),
      ]);
      if (tx.tx_type === "income" || isTransfer) jarSelect.disabled = true;

      rows.push(
        el("tr", { class: "tx-edit-row" }, [
          el("td", { colspan: "5" }, [
            el("form", { class: "tx-edit-form", "data-tx-edit-form": String(tx.id) }, [
              el("input", { type: "date", name: "tx_date", value: tx.tx_date, required: "true" }),
              el("input", { type: "text", name: "merchant", value: tx.merchant, required: "true" }),
              el("input", { type: "number", name: "amount", step: "0.01", min: "0.01", value: (tx.amount_cents / 100).toFixed(2), required: "true" }),
              jarSelect,
              el("input", { type: "text", name: "memo", value: tx.memo || "", placeholder: "Memo" }),
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
      el("option", { value: "", text: nonJarLabel }),
      ...jars.map((j) => el("option", { value: String(j.id), text: j.name, selected: tx.jar_id === j.id })),
    ]);
    if (tx.tx_type === "income" || isTransfer) select.disabled = true;

    const metaBadges = [];
    if (isTransfer) metaBadges.push(el("span", { class: "pill pill-soft", text: "Transfer" }));
    if (tx.needs_review) metaBadges.push(el("span", { class: "pill pill-watch", text: "Review" }));
    if (Array.isArray(tx.splits) && tx.splits.length > 0) {
      metaBadges.push(el("span", { class: "pill pill-blue", text: `${tx.splits.length} Splits` }));
    }
    if (tx.comment_count > 0) {
      metaBadges.push(el("span", { class: "pill pill-soft", text: `${tx.comment_count} Note${tx.comment_count > 1 ? "s" : ""}` }));
    }
    if (Array.isArray(tx.tags)) {
      for (const tg of tx.tags.slice(0, 3)) {
        const tagName = typeof tg === "string" ? tg : tg.name;
        metaBadges.push(el("span", { class: "pill pill-soft", text: `#${tagName}` }));
      }
    }

    const amtSign =
      tx.tx_type === "income" || (isTransfer && tx.transfer_direction === "in")
        ? "+"
        : tx.tx_type === "expense" || (isTransfer && tx.transfer_direction === "out")
        ? "-"
        : "";
    const amtClass =
      tx.tx_type === "income"
        ? "tx-income"
        : isTransfer
        ? "muted"
        : "tx-expense";

    rows.push(
      el("tr", {}, [
        el("td", { class: "num-inline", text: tx.tx_date }),
        el("td", {}, [
          el("div", { class: "row-gap wrap" }, [el("strong", { text: tx.merchant }), ...metaBadges]),
          tx.memo ? el("span", { class: "tx-memo-sub", text: tx.memo }) : null,
        ]),
        el("td", {}, [select]),
        el("td", {
          class: `num ${amtClass}`,
          text: `${amtSign}${fmtMoney(tx.amount_cents)}`,
        }),
        el("td", { class: "num" }, [
          el("div", { class: "tx-actions" }, [
            el(
              "button",
              {
                type: "button",
                class: "btn btn-ghost btn-xs",
                title: "Splits, Tags & Comments",
                "aria-label": `Inspect splits, tags, and comments for ${tx.merchant}`,
                "data-tx-inspect": String(tx.id),
              },
              [makeIcon("split", "ui-icon-sm")]
            ),
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
  if (txs.length > visibleTxs.length) {
    const remaining = txs.length - visibleTxs.length;
    const moreBtn = el("button", {
      type: "button",
      class: "btn btn-soft btn-xs",
      text: `Show next ${Math.min(TX_PAGE_SIZE, remaining)} (${visibleTxs.length} of ${txs.length} shown)`,
    });
    moreBtn.addEventListener("click", () => {
      _txRenderLimit += TX_PAGE_SIZE;
      renderTransactionsTable(txs);
    });
    const allBtn = el("button", {
      type: "button",
      class: "btn btn-ghost btn-xs",
      text: `Show all ${txs.length}`,
    });
    allBtn.addEventListener("click", () => {
      _txRenderLimit = txs.length;
      renderTransactionsTable(txs);
    });
    rows.push(
      el("tr", {}, [
        el("td", { colspan: "5" }, [
          el("div", { class: "row-gap wrap", style: { justifyContent: "center", padding: "6px 0" } }, [
            moreBtn,
            allBtn,
          ]),
        ]),
      ])
    );
  }
  tbody.replaceChildren(...rows);
}

/* --- Transaction Inspector Drawer: Splits, Multi-Tags & Comments --- */

function renderSplitsDraft() {
  const wrap = $("tx-splits-rows");
  const badge = $("tx-splits-balance-badge");
  if (!wrap || !_activeTxObj) return;
  const jars = (state.dashboard && state.dashboard.jars) || [];
  const parentCents = Number(_activeTxObj.amount_cents) || 0;
  const sumCents = _activeSplitsDraft.reduce((acc, s) => acc + (Number(s.amount_cents) || 0), 0);
  const diff = parentCents - sumCents;

  if (badge) {
    if (!_activeSplitsDraft.length) {
      badge.className = "pill pill-soft";
      badge.textContent = `Parent: ${fmtMoney(parentCents)}`;
    } else if (diff === 0) {
      badge.className = "pill pill-calm";
      badge.textContent = "EXACT SUM MATCH";
    } else {
      badge.className = "pill pill-watch";
      badge.textContent = diff > 0 ? `${fmtMoney(diff)} REMAINING` : `${fmtMoney(Math.abs(diff))} OVER`;
    }
  }

  if (!_activeSplitsDraft.length) {
    wrap.replaceChildren(el("p", { class: "muted small", text: "No splits configured. Click + Add Split Line to divide this charge across jars." }));
    return;
  }

  wrap.replaceChildren(
    ..._activeSplitsDraft.map((item, idx) => {
      const jarSel = el("select", { "data-split-idx": String(idx), "data-split-field": "jar_id" }, [
        el("option", { value: "", text: "Select Jar" }),
        ...jars.map((j) => el("option", { value: String(j.id), text: j.name, selected: Number(item.jar_id) === j.id })),
      ]);
      const amtInput = el("input", {
        type: "number",
        step: "0.01",
        min: "0.01",
        value: ((Number(item.amount_cents) || 0) / 100).toFixed(2),
        "data-split-idx": String(idx),
        "data-split-field": "amount",
      });
      const memoInput = el("input", {
        type: "text",
        value: item.memo || "",
        placeholder: "Memo",
        "data-split-idx": String(idx),
        "data-split-field": "memo",
      });
      const rmBtn = el("button", {
        type: "button",
        class: "btn btn-ghost btn-xs",
        "data-split-remove": String(idx),
        "aria-label": "Remove split line",
      }, [makeIcon("x", "ui-icon-sm")]);

      return el("div", { class: "row-gap" }, [jarSel, amtInput, memoInput, rmBtn]);
    })
  );
}

function renderTagsDraft() {
  const list = $("tx-tags-list");
  if (!list) return;
  if (!_activeTagsDraft.length) {
    list.replaceChildren(el("span", { class: "muted small", text: "No tags yet." }));
    return;
  }
  list.replaceChildren(
    ..._activeTagsDraft.map((tag) =>
      el("span", { class: "pill pill-blue" }, [
        `#${tag}`,
        el("button", {
          type: "button",
          class: "btn btn-ghost btn-xs",
          style: { height: "18px", padding: "0 4px", border: "none" },
          "data-tx-tag-remove": tag,
          "aria-label": `Remove tag ${tag}`,
        }, [makeIcon("x", "ui-icon-sm")]),
      ])
    )
  );
}

async function openTxInspector(txId) {
  const tx = state.transactions.find((t) => t.id === txId);
  if (!tx) return;
  state.txDetailId = txId;
  _activeTxObj = tx;
  _activeSplitsDraft = Array.isArray(tx.splits)
    ? tx.splits.map((s) => ({ jar_id: s.jar_id, goal_id: s.goal_id || null, amount_cents: s.amount_cents, memo: s.memo || "" }))
    : [];
  _activeTagsDraft = Array.isArray(tx.tags)
    ? tx.tags.map((t) => (typeof t === "string" ? t : t.name))
    : [];

  const title = $("tx-detail-title");
  if (title) title.textContent = `${tx.merchant} (#${tx.id})`;
  const hdr = $("tx-detail-header");
  if (hdr) {
    hdr.replaceChildren(
      el("div", { class: "item-row" }, [
        el("strong", { text: tx.merchant }),
        el("strong", { class: "num", text: fmtMoney(tx.amount_cents) }),
      ]),
      el("div", { class: "muted small", text: `${tx.tx_date} · ${tx.tx_type.toUpperCase()} · ${tx.source}` })
    );
  }

  renderSplitsDraft();
  renderTagsDraft();
  openDrawer("tx-detail-overlay");

  const commentsBox = $("tx-comments-list");
  if (commentsBox) commentsBox.replaceChildren(el("p", { class: "muted small", text: "Loading comments..." }));
  try {
    const comments = await api(`/api/v1/transactions/${txId}/comments`);
    renderTxComments(Array.isArray(comments) ? comments : []);
  } catch (err) {
    if (commentsBox) commentsBox.replaceChildren(el("p", { class: "muted small", text: err.message }));
  }
}

function renderTxComments(comments) {
  const box = $("tx-comments-list");
  if (!box) return;
  if (!comments.length) {
    box.replaceChildren(el("p", { class: "muted small", text: "No comments yet." }));
    return;
  }
  box.replaceChildren(
    ...comments.map((c) =>
      el("div", { class: "item-box" }, [
        el("div", { class: "item-row" }, [
          el("span", { class: `pill ${c.author === "ai_coach" ? "pill-blue" : "pill-soft"}`, text: c.author === "ai_coach" ? "AI COACH" : "USER" }),
          el("span", { class: "muted small", text: String(c.created_at || "").replace("T", " ").slice(0, 16) }),
        ]),
        el("div", { class: "small", text: c.body }),
      ])
    )
  );
}

/* --- Email Bill Review Queue --- */

export async function loadEmailQueue() {
  try {
    const items = await api("/api/v1/email/queue");
    const list = Array.isArray(items) ? items : [];
    state.emailQueue = list;
    const pending = list.filter((i) => i.status === "pending");

    const cnt = $("email-queue-count");
    if (cnt) cnt.textContent = `${pending.length} pending`;

    const cmdAlert = $("command-email-alert");
    if (cmdAlert) {
      if (pending.length > 0) {
        setNodeIconText(cmdAlert, "mail", `${pending.length} extracted email bill(s) awaiting review in Activity & Bills.`);
        cmdAlert.classList.remove("hidden");
      } else {
        cmdAlert.classList.add("hidden");
      }
    }

    const box = $("email-queue-list");
    if (!box) return;
    if (!pending.length) {
      box.replaceChildren(el("p", { class: "muted small", text: "No pending email bills. Configure optional Read-Only IMAP in Vault & Connectors." }));
      return;
    }
    box.replaceChildren(
      ...pending.map((item) =>
        el("div", { class: "item-box" }, [
          el("div", { class: "item-row" }, [
            el("div", {}, [
              el("strong", { text: `${item.extracted_vendor || item.sender} — ${item.subject}` }),
              el("div", { class: "muted small", text: `${item.received_at}${item.due_date ? ` · due ${item.due_date}` : ""} · ${item.kind}` }),
            ]),
            item.amount_cents != null ? el("strong", { class: "num", text: fmtMoney(item.amount_cents) }) : null,
          ]),
          el("div", { class: "row-gap" }, [
            el("button", { type: "button", class: "btn btn-primary btn-xs", text: "Approve as Bill", "data-email-approve": String(item.id) }),
            el("button", { type: "button", class: "btn btn-ghost btn-xs", text: "Dismiss", "data-email-dismiss": String(item.id) }),
          ]),
        ])
      )
    );
  } catch (_err) {
    // Email queue endpoint optional when locked
  }
}

/* ============================================================================
   5. VAULT & CONNECTORS SETTINGS (Plaid Hosted Link, SimpleFIN, IMAP, AI)
   ============================================================================ */

function stopPlaidPolling() {
  if (_plaidPollTimer) {
    clearInterval(_plaidPollTimer);
    _plaidPollTimer = null;
  }
}

export async function loadConnections() {
  const box = $("connections-list");
  try {
    const conns = await api("/api/v1/connections");
    const list = Array.isArray(conns) ? conns : [];
    state.connections = list;
    if (!box) return;
    if (!list.length) {
      box.replaceChildren(el("p", { class: "muted small", text: "No bank or broker connections yet. Configure Plaid Hosted Link or SimpleFIN below." }));
      return;
    }
    box.replaceChildren(
      ...list.map((c) => {
        const needsReauth = c.status === "needs_reauth";
        const statusCls =
          c.status === "active"
            ? "pill-calm"
            : c.status === "paused_locked"
            ? "pill-watch"
            : "pill-danger";
        const statusLabel =
          c.status === "paused_locked"
            ? "SYNC PAUSED: VAULT LOCKED"
            : needsReauth
            ? "RECONNECT REQUIRED"
            : String(c.status || "active").replace(/_/g, " ").toUpperCase();
        const windowNote =
          c.imported_start_date && c.imported_end_date
            ? ` · Window: ${c.imported_start_date} to ${c.imported_end_date}`
            : "";
        return el("div", { class: "item-box" }, [
          el("div", { class: "item-row wrap" }, [
            el("div", {}, [
              el("div", { class: "row-gap wrap", style: { gap: "6px" } }, [
                el("strong", { text: `${c.institution || c.provider.toUpperCase()} (${c.provider})` }),
                c.env && c.env !== "production" ? el("span", { class: "pill pill-watch", text: "TEST ENV" }) : null,
              ]),
              el("div", {
                class: "muted small",
                text: `Last sync: ${c.last_synced_at ? c.last_synced_at.replace("T", " ").slice(0, 16) : "Never"}${windowNote}`,
              }),
              c.last_error
                ? el("div", { class: "small error-text", text: c.last_error })
                : null,
            ]),
            el("div", { class: "row-gap wrap" }, [
              el("span", { class: `pill ${statusCls}`, text: statusLabel }),
              c.provider === "plaid" && c.backfill_status === "backfill_pending"
                ? el("span", { class: "pill pill-watch", text: "BACKFILL PENDING" })
                : null,
              c.needs_relink_for_730d
                ? el("span", { class: "pill pill-watch", text: "RE-LINK FOR 730D HISTORY" })
                : null,
              el("button", {
                type: "button",
                class: "btn btn-soft btn-xs",
                text: needsReauth ? "Retry Sync" : "Sync",
                "data-conn-sync": String(c.id),
                "data-conn-provider": c.provider,
              }),
              el("button", {
                type: "button",
                class: "btn btn-ghost btn-xs",
                text: "Remove",
                "data-conn-delete": String(c.id),
              }),
            ]),
          ]),
        ]);
      })
    );
  } catch (err) {
    if (box && err.status !== 401) {
      box.replaceChildren(el("p", { class: "muted small", text: err.message }));
    }
  }
}

export async function loadEmailConfig() {
  try {
    const cfg = await api("/api/v1/email/config");
    const cb = $("email-enabled");
    const host = $("email-host");
    const port = $("email-port");
    const user = $("email-username");
    const folder = $("email-folder");
    const badge = $("email-scanner-badge");
    if (cb) cb.checked = Boolean(cfg.enabled);
    if (host) host.value = cfg.imap_host || "";
    if (port) port.value = String(cfg.imap_port || 993);
    if (user) user.value = cfg.username || "";
    if (folder) folder.value = cfg.folder || "INBOX";
    if (badge) {
      badge.className = cfg.enabled ? "pill pill-calm" : "pill pill-soft";
      badge.textContent = cfg.enabled ? "READ-ONLY ACTIVE" : "DISABLED";
    }
  } catch (_err) {
    // Ignore when locked
  }
}

export async function fetchProviderModels(provider, forceRefresh, selectedModel) {
  const isGemini = provider === "gemini";
  const sel = isGemini ? $("set-gemini-model") : $("set-opencode-model");
  if (!sel) return;
  const fallbackModel = isGemini ? "gemini-2.5-flash" : "glm-5.3-flash";
  try {
    const res = await api(
      `/api/v1/ai/models?provider=${encodeURIComponent(provider)}&refresh=${forceRefresh ? "true" : "false"}`
    );
    const models = res.models || [];
    if (isGemini) state.geminiModels = models;
    else state.opencodeModels = models;

    const target =
      selectedModel ||
      sel.value ||
      (state.settings && (isGemini ? state.settings.gemini_model : state.settings.opencode_model)) ||
      fallbackModel;
    const hasTarget = models.some((m) => m.id === target);
    const items = hasTarget ? models : [{ id: target, family: "Custom", note: "Current" }, ...models];

    sel.replaceChildren(
      ...items.map((m) =>
        el("option", {
          value: m.id,
          text: `${m.id}${m.note || m.family ? ` — ${m.note || m.family}` : ""}`,
          selected: m.id === target,
        })
      )
    );
    if (forceRefresh) {
      toast(res.live ? `Loaded ${models.length} live models.` : res.notice || `Loaded ${models.length} models.`);
    }
  } catch (err) {
    if (forceRefresh) toast(err.message, true);
    if (!sel.options.length) sel.replaceChildren(el("option", { value: fallbackModel, text: fallbackModel }));
  }
}

export async function loadSettings() {
  try {
    const s = await api("/api/v1/settings");
    state.settings = s;
    state.onboardingCompleted = Boolean(s.onboarding_completed);

    syncModeControls(s.intelligence_mode || "hybrid");
    syncProviderControls(s.ai_provider || "auto", s.active_provider_resolved || "none");

    const setVal = (id, v) => {
      const n = $(id);
      if (n) n.value = String(v ?? "");
    };

    setVal("set-opencode-url", s.opencode_base_url || "https://opencode.ai/zen/go/v1");
    setVal("set-opencode-proto", s.opencode_protocol || "auto");
    setVal("set-opencode-quota", s.opencode_max_daily_requests || 60);
    const ocBadge = $("key-status-badge");
    if (ocBadge) {
      ocBadge.className = s.opencode_key_configured ? "pill pill-calm" : "pill pill-soft";
      ocBadge.textContent = s.opencode_key_configured
        ? `${s.opencode_key_source === "env" ? "Env" : "Sealed"} · ${s.opencode_key_masked || "Set"}`
        : "Not set";
    }

    setVal("set-gemini-url", s.gemini_base_url || "https://generativelanguage.googleapis.com/v1beta");
    const gemBadge = $("gemini-key-status-badge");
    if (gemBadge) {
      gemBadge.className = s.gemini_api_key_set ? "pill pill-blue" : "pill pill-soft";
      gemBadge.textContent = s.gemini_api_key_set
        ? `${s.gemini_key_source === "env" ? "Env" : "Sealed"} · ${s.gemini_key_masked || "Set"}`
        : "Not set";
    }

    setVal("set-codex-url", s.codex_base_url || "https://api.openai.com/v1");
    setVal("set-codex-model", s.codex_model || "gpt-4.1-mini");
    const cxBadge = $("codex-key-status-badge");
    if (cxBadge) {
      cxBadge.className = s.codex_key_configured ? "pill pill-calm" : "pill pill-soft";
      cxBadge.textContent = s.codex_key_configured
        ? `${s.codex_key_source === "env" ? "Env" : "Sealed"} · ${s.codex_key_masked || "Set"}`
        : "Not set";
    }

    setVal("set-wrapper-url", s.wrapper_base_url || "");
    setVal("set-wrapper-model", s.wrapper_model || "");
    setVal("set-wrapper-wire-api", s.wrapper_wire_api || "chat_completions");
    const cliCb = $("set-codex-cli-enabled");
    if (cliCb) cliCb.checked = Boolean(s.codex_cli_enabled);
    const wrBadge = $("wrapper-key-status-badge");
    if (wrBadge) {
      wrBadge.className = s.wrapper_base_url ? "pill pill-blue" : "pill pill-soft";
      wrBadge.textContent = s.wrapper_base_url ? "Configured" : "Optional";
    }

    setVal("set-hourly-wage", Math.round((s.hourly_wage_cents || 3000) / 100));
    setVal("set-emergency-buffer", Math.round((s.emergency_buffer_cents || 50000) / 100));
    setVal("set-cooling-hours", s.default_cooling_hours || 48);
    setVal("set-payday-cadence", s.payday_cadence_days || 14);
    setVal("set-next-payday", s.next_payday_date || "");
    setVal("set-expected-paycheck", Math.round((s.expected_paycheck_cents ?? 0) / 100));
    setVal("set-auto-lock-minutes", s.vault_auto_lock_minutes ?? 30);

    await Promise.all([
      fetchProviderModels("opencode_go", false, s.opencode_model),
      fetchProviderModels("gemini", false, s.gemini_model),
      loadConnections(),
      loadEmailConfig(),
    ]);
  } catch (err) {
    if (err.status !== 401) toast(err.message, true);
  }
}

/* ============================================================================
   6. EVENT BINDINGS FOR VAULT, ONBOARDING, COMMAND, ACTIVITY & SETTINGS
   ============================================================================ */

export function initVaultAndShell() {
  if ($("bill-due")) $("bill-due").value = isoDateOffset(7);
  if ($("manual-tx-date")) $("manual-tx-date").value = isoDateOffset(0);

  registerTabHook("activity", () => {
    loadTransactions();
    loadEmailQueue();
  });
  registerTabHook("settings", () => loadSettings());

  // --- Vault Setup Form ---
  const setupForm = $("vault-setup-form");
  if (setupForm) {
    setupForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const pwd = $("vault-setup-pwd").value;
      const conf = $("vault-setup-confirm").value;
      const errEl = $("vault-setup-error");
      if (pwd.length < 8) {
        errEl.textContent = "Master password must be at least 8 characters.";
        errEl.classList.remove("hidden");
        return;
      }
      if (pwd !== conf) {
        errEl.textContent = "Passwords do not match.";
        errEl.classList.remove("hidden");
        return;
      }
      errEl.classList.add("hidden");
      await withBusy(setupForm.querySelector("button[type=submit]"), async () => {
        try {
          const res = await api("/api/v1/auth/setup", {
            method: "POST",
            body: JSON.stringify({ password: pwd }),
          });
          if (res.csrf_token) setCsrfToken(res.csrf_token);
          $("vault-setup-pwd").value = "";
          $("vault-setup-confirm").value = "";
          renderRecoveryVerifyPane(res);
        } catch (err) {
          errEl.textContent = err.message;
          errEl.classList.remove("hidden");
        }
      });
    });
  }

  // --- Copy Recovery Key ---
  const copyRecBtn = $("btn-copy-recovery-key");
  if (copyRecBtn) {
    copyRecBtn.addEventListener("click", async () => {
      if (!_pendingRecoveryData) return;
      try {
        await navigator.clipboard.writeText(_pendingRecoveryData.recoveryKey);
        toast("Recovery key copied. Store it offline immediately.");
      } catch (_err) {
        toast("Select and copy the key text manually.", true);
      }
    });
  }

  // --- 4-Word Recovery Verification Check ---
  const verifyForm = $("vault-recovery-verify-form");
  if (verifyForm) {
    verifyForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!_pendingRecoveryData) return;
      const errEl = $("vault-verify-error");
      const inputs = Array.from(verifyForm.querySelectorAll("input[data-verify-idx]"));
      for (const inp of inputs) {
        const idx = Number(inp.dataset.verifyIdx);
        const expected = String(_pendingRecoveryData.tokens[idx] || "").trim().toUpperCase();
        const actual = String(inp.value || "").trim().toUpperCase();
        if (!actual || actual !== expected) {
          errEl.textContent = `Group #${idx + 1} does not match your Recovery Key. Check your backup and retry.`;
          errEl.classList.remove("hidden");
          inp.focus();
          return;
        }
      }
      errEl.classList.add("hidden");
      const needOnboard = !_pendingRecoveryData.onboardingCompleted;
      _pendingRecoveryData = null;
      closeVaultGate();
      toast("Encrypted vault initialized and recovery key verified.");
      if (_onUnlockedCallback) await _onUnlockedCallback();
      if (needOnboard) openOnboarding();
    });
  }

  // --- Vault Unlock Form ---
  const unlockForm = $("vault-unlock-form");
  if (unlockForm) {
    unlockForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const errEl = $("vault-unlock-error");
      await withBusy(unlockForm.querySelector("button[type=submit]"), async () => {
        try {
          const res = await api("/api/v1/auth/login", {
            method: "POST",
            body: JSON.stringify({ password: $("vault-unlock-pwd").value }),
          });
          $("vault-unlock-pwd").value = "";
          errEl.classList.add("hidden");
          if (res && res.csrf_token) setCsrfToken(res.csrf_token);
          if (res && res.onboarding_completed !== undefined) {
            state.onboardingCompleted = Boolean(res.onboarding_completed);
          }
          closeVaultGate();
          if (_onUnlockedCallback) await _onUnlockedCallback();
          if (!state.onboardingCompleted) openOnboarding();
        } catch (err) {
          errEl.textContent = err.message;
          errEl.classList.remove("hidden");
        }
      });
    });
  }

  // --- Recovery Key Unlock Form ---
  const btnShowRecover = $("btn-vault-show-recover");
  const btnBackUnlock = $("btn-vault-back-unlock");
  if (btnShowRecover) btnShowRecover.addEventListener("click", () => showVaultPane("recover"));
  if (btnBackUnlock) btnBackUnlock.addEventListener("click", () => showVaultPane("unlock"));

  const recoverForm = $("vault-recover-form");
  if (recoverForm) {
    recoverForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const errEl = $("vault-recover-error");
      const newPwd = $("vault-recover-new-pwd").value;
      const confPwd = $("vault-recover-confirm-pwd").value;
      if (newPwd !== confPwd) {
        errEl.textContent = "New passwords do not match.";
        errEl.classList.remove("hidden");
        return;
      }
      await withBusy(recoverForm.querySelector("button[type=submit]"), async () => {
        try {
          const res = await api("/api/v1/auth/recover", {
            method: "POST",
            body: JSON.stringify({
              recovery_key: $("vault-recover-key").value.trim(),
              new_password: newPwd,
            }),
          });
          recoverForm.reset();
          errEl.classList.add("hidden");
          if (res && res.csrf_token) setCsrfToken(res.csrf_token);
          closeVaultGate();
          toast("Vault recovered and DEK rewrapped with your new password.");
          if (_onUnlockedCallback) await _onUnlockedCallback();
        } catch (err) {
          errEl.textContent = err.message;
          errEl.classList.remove("hidden");
        }
      });
    });
  }

  // --- Lock Vault Buttons ---
  const btnLockTop = $("btn-lock-vault");
  const btnLockSet = $("btn-settings-lock-vault");
  if (btnLockTop) btnLockTop.addEventListener("click", lockVaultNow);
  if (btnLockSet) btnLockSet.addEventListener("click", lockVaultNow);

  // --- Change Master Password ---
  const vcpForm = $("vault-change-pwd-form");
  if (vcpForm) {
    vcpForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const newPwd = $("vcp-new").value;
      if (newPwd !== $("vcp-confirm").value) {
        toast("New passwords do not match.", true);
        return;
      }
      await withBusy(vcpForm.querySelector("button[type=submit]"), async () => {
        try {
          await api("/api/v1/auth/change-password", {
            method: "POST",
            body: JSON.stringify({
              current_password: $("vcp-current").value,
              new_password: newPwd,
            }),
          });
          vcpForm.reset();
          toast("Master password updated (DEK rewrapped cleanly).");
        } catch (err) {
          toast(err.message, true);
        }
      });
    });
  }

  const btnSaveAutoLock = $("btn-save-auto-lock");
  if (btnSaveAutoLock) {
    btnSaveAutoLock.addEventListener("click", (e) =>
      withBusy(e.currentTarget, async () => {
        const mins = parseInt($("set-auto-lock-minutes").value, 10) || 30;
        try {
          await api("/api/v1/settings", {
            method: "PATCH",
            body: JSON.stringify({ vault_auto_lock_minutes: mins }),
          });
          toast(`Vault auto-lock timeout set to ${mins} minutes.`);
        } catch (err) {
          toast(err.message, true);
        }
      })
    );
  }

  // --- Onboarding Modal Buttons ---
  const bindClick = (id, fn) => {
    const n = $(id);
    if (n) n.addEventListener("click", fn);
  };
  bindClick("btn-onboarding-skip", () => completeOnboarding("skipped"));
  bindClick("btn-onboarding-done", () => completeOnboarding("skipped"));
  bindClick("btn-onboard-plaid", async () => {
    await completeOnboarding("plaid");
    switchTab("settings");
    const elNode = $("set-plaid-client-id");
    if (elNode) elNode.focus();
  });
  bindClick("btn-onboard-simplefin", async () => {
    await completeOnboarding("simplefin");
    switchTab("settings");
    const elNode = $("set-simplefin-token");
    if (elNode) elNode.focus();
  });
  bindClick("btn-onboard-local", async () => {
    await completeOnboarding("local");
    switchTab("activity");
    const elNode = $("csv-file-input");
    if (elNode) elNode.focus();
  });
  bindClick("btn-reopen-onboarding", () => openOnboarding());

  // --- Drawers (Impulse Vault, 5-Min Reset, Transaction Inspector) ---
  bindClick("btn-open-impulse-drawer", () => openDrawer("impulse-drawer-overlay"));
  bindClick("btn-goals-open-impulse", () => openDrawer("impulse-drawer-overlay"));
  bindClick("btn-impulse-drawer-close", () => closeDrawer("impulse-drawer-overlay"));
  bindClick("btn-open-reset-drawer", () => {
    openDrawer("reset-drawer-overlay");
    loadWeeklyReset();
  });
  bindClick("btn-reset-drawer-close", () => closeDrawer("reset-drawer-overlay"));
  bindClick("btn-tx-detail-close", () => closeDrawer("tx-detail-overlay"));

  for (const overlayId of ["impulse-drawer-overlay", "reset-drawer-overlay", "tx-detail-overlay"]) {
    const ov = $(overlayId);
    if (ov) {
      ov.addEventListener("click", (e) => {
        if (e.target === ov) closeDrawer(overlayId);
      });
    }
  }

  // --- Quick-Add & Auto-Sync ---
  const qaForm = $("quick-add-form");
  if (qaForm) {
    qaForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const input = $("quick-add-input");
      const text = input.value.trim();
      if (!text) return;
      await withBusy(qaForm.querySelector("button[type=submit]"), async () => {
        try {
          const res = await api("/api/v1/quick-add", {
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
  }

  document.querySelectorAll("[data-quick]").forEach((chip) => {
    chip.addEventListener("click", () => {
      const val = chip.dataset.quick;
      const inp = $("quick-add-input");
      if (!inp) return;
      inp.value = val;
      inp.focus();
      if (!val.endsWith(" ")) $("quick-add-form").requestSubmit();
    });
  });

  bindClick("btn-sync-now", (e) =>
    withBusy(e.currentTarget, async () => {
      try {
        const res = await api("/api/v1/budget/sync", { method: "POST" });
        toast(`Auto-budget synced. Safe-to-Spend: ${fmtMoney(res.safe_to_spend_cents)}`);
        await refreshAll();
      } catch (err) {
        toast(err.message, true);
      }
    })
  );

  // --- Impulse & Weekly Reset Actions ---
  const impForm = $("impulse-form");
  if (impForm) {
    impForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      await withBusy(impForm.querySelector("button[type=submit]"), async () => {
        try {
          await api("/api/v1/impulses", {
            method: "POST",
            body: JSON.stringify({
              item_name: $("imp-name").value.trim(),
              price_cents: dollarsToCents($("imp-price").value),
              cooling_hours: parseInt($("imp-hours").value, 10) || 48,
              reason: $("imp-reason").value.trim(),
            }),
          });
          impForm.reset();
          triggerCelebrate(impForm, true);
          toast("Parked in Impulse Vault (+15 XP).");
          await refreshAll();
        } catch (err) {
          toast(err.message, true);
        }
      });
    });
  }

  bindClick("btn-complete-reset", (e) =>
    withBusy(e.currentTarget, async () => {
      try {
        await api("/api/v1/weekly-reset/complete", {
          method: "POST",
          body: JSON.stringify({ apply_rebalances: true }),
        });
        triggerCelebrate(e.currentTarget, true);
        toast("Weekly Money Reset complete (+100 XP & streak protected).");
        await refreshAll();
        await loadWeeklyReset();
      } catch (err) {
        toast(err.message, true);
      }
    })
  );

  // --- Accounts & Universal Statement Import ---
  const acctForm = $("account-form");
  if (acctForm) {
    acctForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      await withBusy(acctForm.querySelector("button[type=submit]"), async () => {
        try {
          await api("/api/v1/accounts", {
            method: "POST",
            body: JSON.stringify({
              name: $("acct-name").value.trim(),
              account_type: $("acct-type").value,
              balance_cents: dollarsToCents($("acct-balance").value, { allowNegative: true, allowZero: true }),
              is_liquid: ["checking", "savings", "cash"].includes($("acct-type").value),
            }),
          });
          acctForm.reset();
          toast("Account added.");
          await refreshAll();
        } catch (err) {
          toast(err.message, true);
        }
      });
    });
  }

  const csvFileInp = $("csv-file-input");
  if (csvFileInp) {
    csvFileInp.addEventListener("change", () => {
      if (csvFileInp.files.length > 0) {
        state.desktopStatementFile = null;
        const box = $("desktop-selected-statement");
        if (box) box.classList.add("hidden");
      }
    });
  }

  const buildStatementFormData = () => {
    const file = (csvFileInp && csvFileInp.files[0]) || state.desktopStatementFile;
    if (!file) throw new Error("Choose a statement file (.csv, .pdf, .ofx, .qfx, .qif, or .json) first.");
    const fd = new FormData();
    fd.append("file", file, file.name || "statement.csv");
    fd.append("institution", $("csv-institution").value || "auto");
    const targetAcct = $("csv-account-select") && $("csv-account-select").value;
    if (targetAcct) fd.append("account_id", targetAcct);
    fd.append("skip_pending", $("csv-skip-pending").checked ? "true" : "false");
    fd.append("flip_expense_sign", $("csv-flip-sign").checked ? "true" : "false");
    fd.append("detect_transfers", $("csv-detect-transfers").checked ? "true" : "false");
    fd.append("sync_balance", $("csv-sync-balance").checked ? "true" : "false");
    return fd;
  };

  bindClick("btn-preview-statement", (e) =>
    withBusy(e.currentTarget, async () => {
      try {
        const res = await api("/api/v1/import/preview", { method: "POST", body: buildStatementFormData() });
        const box = $("csv-import-result");
        const warnTxt =
          Array.isArray(res.warnings) && res.warnings.length ? ` Warnings: ${res.warnings.join(" | ")}` : "";
        const acctTxt = res.target_account_name ? ` -> ${res.target_account_name}` : "";
        box.textContent =
          `Preview (${String(res.format_detected || "").toUpperCase()}${acctTxt}): ` +
          `${res.new_count} new, ${res.duplicate_count} duplicate, ${res.transfers_detected} transfer(s).${warnTxt}`;
        box.className = `notice-bar details-spacer${res.parse_failed ? " notice-warn" : ""}`;
        box.classList.remove("hidden");
        if (res.parse_failed) toast(res.warnings && res.warnings[0] ? res.warnings[0] : "Statement file yielded 0 transactions.", true);
      } catch (err) {
        toast(err.message, true);
      }
    })
  );

  const csvForm = $("csv-upload-form");
  if (csvForm) {
    csvForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      await withBusy(csvForm.querySelector("button[type=submit]"), async () => {
        try {
          const res = await api("/api/v1/import/statement", { method: "POST", body: buildStatementFormData() });
          const box = $("csv-import-result");
          const warnTxt =
            Array.isArray(res.warnings) && res.warnings.length ? ` (${res.warnings.join(" | ")})` : "";
          const acctTxt = res.target_account_name ? ` into ${res.target_account_name}` : "";
          box.textContent = `Imported ${res.imported_count} transaction(s)${acctTxt} (${res.duplicate_count} duplicate, ${res.transfers_detected} transfer isolated).${warnTxt}`;
          box.className = `notice-bar details-spacer${res.parse_failed ? " notice-warn" : ""}`;
          box.classList.remove("hidden");
          state.desktopStatementFile = null;
          const deskBox = $("desktop-selected-statement");
          if (deskBox) deskBox.classList.add("hidden");
          if (res.parse_failed) {
            toast(res.warnings && res.warnings[0] ? res.warnings[0] : "Could not parse any transactions from statement.", true);
          } else {
            toast(`Imported ${res.imported_count} transactions.`);
          }
          await refreshAll();
        } catch (err) {
          toast(err.message, true);
        }
      });
    });
  }

  // --- Transactions Search / Filter / Manual Entry ---
  const txSearch = $("tx-search");
  const txJarFlt = $("tx-jar-filter");
  const txRange = $("tx-date-range");
  if (txSearch) txSearch.addEventListener("input", () => loadTransactions());
  if (txJarFlt) txJarFlt.addEventListener("change", () => loadTransactions());
  if (txRange) txRange.addEventListener("change", () => loadTransactions());

  const manTxForm = $("manual-tx-form");
  if (manTxForm) {
    manTxForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      await withBusy(manTxForm.querySelector("button[type=submit]"), async () => {
        try {
          const jarVal = $("manual-tx-jar").value;
          const acctVal = $("manual-tx-account").value;
          const rawType = $("manual-tx-type").value;
          const txType =
            rawType === "transfer_out" || rawType === "transfer_in" ? "transfer" : rawType;
          const transferDirection =
            rawType === "transfer_out" ? "out" : rawType === "transfer_in" ? "in" : null;
          await api("/api/v1/transactions", {
            method: "POST",
            body: JSON.stringify({
              tx_type: txType,
              transfer_direction: transferDirection,
              merchant: $("manual-tx-merchant").value.trim(),
              amount_cents: dollarsToCents($("manual-tx-amount").value),
              tx_date: $("manual-tx-date").value || isoDateOffset(0),
              account_id: acctVal ? Number(acctVal) : null,
              jar_id: txType === "expense" && jarVal ? Number(jarVal) : null,
              memo: $("manual-tx-memo").value.trim(),
              source: "manual_detailed",
            }),
          });
          manTxForm.reset();
          $("manual-tx-date").value = isoDateOffset(0);
          toast("Transaction saved.");
          await refreshAll();
        } catch (err) {
          toast(err.message, true);
        }
      });
    });
  }

  // --- Transaction Inspector (Splits, Tags, Comments) ---
  bindClick("btn-tx-split-add-row", () => {
    if (!_activeTxObj) return;
    const sum = _activeSplitsDraft.reduce((a, s) => a + (Number(s.amount_cents) || 0), 0);
    const rem = Math.max(100, (Number(_activeTxObj.amount_cents) || 0) - sum);
    const firstJar = state.dashboard && state.dashboard.jars && state.dashboard.jars[0];
    _activeSplitsDraft.push({
      jar_id: firstJar ? firstJar.id : null,
      goal_id: null,
      amount_cents: rem,
      memo: "",
    });
    renderSplitsDraft();
  });

  bindClick("btn-tx-split-clear", () => {
    _activeSplitsDraft = [];
    renderSplitsDraft();
  });

  const splitsWrap = $("tx-splits-rows");
  if (splitsWrap) {
    splitsWrap.addEventListener("input", (e) => {
      const idx = Number(e.target.dataset.splitIdx);
      const field = e.target.dataset.splitField;
      if (!Number.isInteger(idx) || !_activeSplitsDraft[idx]) return;
      if (field === "amount") {
        const num = Number(e.target.value);
        _activeSplitsDraft[idx].amount_cents = Number.isFinite(num) ? Math.round(num * 100) : 0;
        const badge = $("tx-splits-balance-badge");
        const diff = (Number(_activeTxObj.amount_cents) || 0) - _activeSplitsDraft.reduce((a, s) => a + s.amount_cents, 0);
        if (badge) {
          badge.className = diff === 0 ? "pill pill-calm" : "pill pill-watch";
          badge.textContent = diff === 0 ? "EXACT SUM MATCH" : diff > 0 ? `${fmtMoney(diff)} REMAINING` : `${fmtMoney(Math.abs(diff))} OVER`;
        }
      } else if (field === "memo") {
        _activeSplitsDraft[idx].memo = e.target.value;
      }
    });
    splitsWrap.addEventListener("change", (e) => {
      const idx = Number(e.target.dataset.splitIdx);
      if (e.target.dataset.splitField === "jar_id" && _activeSplitsDraft[idx]) {
        _activeSplitsDraft[idx].jar_id = e.target.value ? Number(e.target.value) : null;
      }
    });
    splitsWrap.addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-split-remove]");
      if (!btn) return;
      _activeSplitsDraft.splice(Number(btn.dataset.splitRemove), 1);
      renderSplitsDraft();
    });
  }

  bindClick("btn-tx-split-save", (e) =>
    withBusy(e.currentTarget, async () => {
      if (!_activeTxObj) return;
      const sum = _activeSplitsDraft.reduce((a, s) => a + (Number(s.amount_cents) || 0), 0);
      if (_activeSplitsDraft.length > 0 && sum !== Number(_activeTxObj.amount_cents)) {
        toast(`Split total (${fmtMoney(sum)}) must equal parent (${fmtMoney(_activeTxObj.amount_cents)}).`, true);
        return;
      }
      try {
        if (_activeSplitsDraft.length === 0) {
          await api(`/api/v1/transactions/${_activeTxObj.id}/splits`, { method: "DELETE" });
          toast("Transaction splits cleared.");
        } else {
          await api(`/api/v1/transactions/${_activeTxObj.id}/splits`, {
            method: "POST",
            body: JSON.stringify({ splits: _activeSplitsDraft }),
          });
          toast("Exact-sum splits saved.");
        }
        await refreshAll();
        await loadTransactions();
      } catch (err) {
        toast(err.message, true);
      }
    })
  );

  const tagAddForm = $("tx-tag-add-form");
  if (tagAddForm) {
    tagAddForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!_activeTxObj) return;
      const raw = $("tx-tag-input").value.trim().toLowerCase().replace(/^#/, "");
      if (!raw) return;
      if (!_activeTagsDraft.includes(raw)) _activeTagsDraft.push(raw);
      $("tx-tag-input").value = "";
      renderTagsDraft();
      try {
        await api(`/api/v1/transactions/${_activeTxObj.id}/tags`, {
          method: "PUT",
          body: JSON.stringify({ tags: _activeTagsDraft }),
        });
        await loadTransactions();
      } catch (err) {
        toast(err.message, true);
      }
    });
  }

  const tagsList = $("tx-tags-list");
  if (tagsList) {
    tagsList.addEventListener("click", async (e) => {
      const btn = e.target.closest("button[data-tx-tag-remove]");
      if (!btn || !_activeTxObj) return;
      _activeTagsDraft = _activeTagsDraft.filter((t) => t !== btn.dataset.txTagRemove);
      renderTagsDraft();
      try {
        await api(`/api/v1/transactions/${_activeTxObj.id}/tags`, {
          method: "PUT",
          body: JSON.stringify({ tags: _activeTagsDraft }),
        });
        await loadTransactions();
      } catch (err) {
        toast(err.message, true);
      }
    });
  }

  const commentForm = $("tx-comment-form");
  if (commentForm) {
    commentForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!_activeTxObj) return;
      const body = $("tx-comment-input").value.trim();
      if (!body) return;
      try {
        await api(`/api/v1/transactions/${_activeTxObj.id}/comments`, {
          method: "POST",
          body: JSON.stringify({ body, author: "user" }),
        });
        $("tx-comment-input").value = "";
        const comments = await api(`/api/v1/transactions/${_activeTxObj.id}/comments`);
        renderTxComments(Array.isArray(comments) ? comments : []);
        await loadTransactions();
      } catch (err) {
        toast(err.message, true);
      }
    });
  }

  // --- Recurring Bill Form (with Min/Max Ranges) ---
  const billForm = $("bill-form");
  if (billForm) {
    billForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      await withBusy(billForm.querySelector("button[type=submit]"), async () => {
        try {
          const payload = {
            name: $("bill-name").value.trim(),
            amount_cents: dollarsToCents($("bill-amount").value),
            cadence: $("bill-cadence").value,
            next_due_date: $("bill-due").value,
            due_window_days: parseInt($("bill-window-days").value, 10) || 4,
            is_subscription: $("bill-is-sub").checked,
            is_essential: !$("bill-is-sub").checked,
          };
          const minRaw = $("bill-min").value.trim();
          const maxRaw = $("bill-max").value.trim();
          if (minRaw) payload.amount_min_cents = dollarsToCents(minRaw, { allowZero: true });
          if (maxRaw) payload.amount_max_cents = dollarsToCents(maxRaw, { allowZero: true });
          await api("/api/v1/bills", { method: "POST", body: JSON.stringify(payload) });
          billForm.reset();
          $("bill-due").value = isoDateOffset(7);
          $("bill-window-days").value = "4";
          toast("Recurring bill saved.");
          await refreshAll();
        } catch (err) {
          toast(err.message, true);
        }
      });
    });
  }

  // --- Plaid Hosted Link (BYOK) + SimpleFIN + IMAP Email Scanner ---
  const plaidCfgForm = $("plaid-config-form");
  if (plaidCfgForm) {
    plaidCfgForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      await withBusy(plaidCfgForm.querySelector("button[type=submit]"), async () => {
        try {
          await api("/api/v1/plaid/config", {
            method: "POST",
            body: JSON.stringify({
              client_id: $("set-plaid-client-id").value.trim(),
              secret: $("set-plaid-secret").value.trim(),
              env: "production",
            }),
          });
          $("set-plaid-secret").value = "";
          const pill = $("plaid-status-pill");
          if (pill) {
            pill.className = "pill pill-calm";
            pill.textContent = "CREDENTIALS SEALED";
          }
          toast("Plaid BYOK credentials sealed in vault.");
        } catch (err) {
          toast(err.message, true);
        }
      });
    });
  }

  bindClick("btn-plaid-connect", (e) =>
    withBusy(e.currentTarget, async () => {
      const stBox = $("plaid-link-status");
      try {
        const lt = await api("/api/v1/plaid/link-token", {
          method: "POST",
          body: JSON.stringify({
            products: ["transactions"],
            optional_products: ["liabilities", "investments"],
            days_requested: 730,
          }),
        });
        const hostedUrl = lt.hosted_link_url;
        if (hostedUrl) {
          const desktop = getDesktopBridge();
          if (desktop && typeof desktop.openExternal === "function") {
            await desktop.openExternal(hostedUrl);
          } else {
            window.open(hostedUrl, "_blank", "noopener,noreferrer");
          }
        }
        if (stBox) {
          stBox.textContent = "Hosted Link opened in system browser — waiting for bank completion (up to 730d requested)...";
          stBox.className = "notice-bar details-spacer";
          stBox.classList.remove("hidden");
        }
        stopPlaidPolling();
        let attempts = 0;
        _plaidPollTimer = setInterval(async () => {
          attempts += 1;
          if (attempts > 60 || state.vaultStatus !== "unlocked") {
            stopPlaidPolling();
            return;
          }
          let poll = null;
          try {
            poll = await api("/api/v1/plaid/poll-link", {
              method: "POST",
              body: JSON.stringify({ link_token: lt.link_token }),
            });
          } catch (_pollErr) {
            return;
          }
          if (!poll) return;
          if (poll.status === "exited") {
            stopPlaidPolling();
            const exitMsg = poll.error_message || "Plaid Hosted Link closed before linking completed.";
            if (stBox) {
              stBox.textContent = exitMsg;
              stBox.className = "notice-bar notice-warn details-spacer";
              stBox.classList.remove("hidden");
            }
            toast(exitMsg, true);
            return;
          }
          if (poll.public_token) {
            stopPlaidPolling();
            try {
              const exRes = await api("/api/v1/plaid/exchange", {
                method: "POST",
                body: JSON.stringify({
                  public_token: poll.public_token,
                  institution: poll.institution || "Plaid Institution",
                }),
              });
              const newConnId = exRes && (exRes.id || exRes.connection_id);
              const syncRes = await api("/api/v1/plaid/sync", {
                method: "POST",
                body: JSON.stringify(newConnId ? { connection_id: newConnId } : {}),
              });
              const note =
                (syncRes && syncRes.history_note) ||
                "Plaid bank linked & initial transaction sync complete.";
              if (stBox) {
                stBox.textContent = note;
                stBox.className = "notice-bar details-spacer";
                stBox.classList.remove("hidden");
              }
              toast(note);
              await loadConnections();
              await refreshAll();
            } catch (syncErr) {
              const msg = (syncErr && syncErr.message) || "Plaid link or initial sync failed.";
              if (stBox) {
                stBox.textContent = msg;
                stBox.className = "notice-bar notice-warn details-spacer";
                stBox.classList.remove("hidden");
              }
              toast(msg, true);
              await loadConnections();
            }
          }
        }, 3000);
      } catch (err) {
        if (stBox) {
          stBox.textContent = err.message;
          stBox.className = "notice-bar notice-warn details-spacer";
          stBox.classList.remove("hidden");
        }
        toast(err.message, true);
      }
    })
  );

  const sfinForm = $("simplefin-claim-form");
  if (sfinForm) {
    sfinForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const stBox = $("simplefin-status");
      await withBusy(sfinForm.querySelector("button[type=submit]"), async () => {
        try {
          await api("/api/v1/simplefin/claim", {
            method: "POST",
            body: JSON.stringify({
              setup_token: $("set-simplefin-token").value.trim(),
              institution: "SimpleFIN Bridge",
            }),
          });
          $("set-simplefin-token").value = "";
          const syncRes = await api("/api/v1/simplefin/sync", { method: "POST" });
          if (stBox) {
            stBox.textContent = `SimpleFIN claimed & synced (${syncRes.imported_count || 0} new tx).`;
            stBox.classList.remove("hidden");
          }
          toast("SimpleFIN Bridge connected & synced.");
          await loadConnections();
          await refreshAll();
        } catch (err) {
          if (stBox) {
            stBox.textContent = err.message;
            stBox.classList.remove("hidden");
          }
          toast(err.message, true);
        }
      });
    });
  }

  bindClick("btn-refresh-connections", () => loadConnections());

  // --- Email Scanner Config & Scan ---
  const emailCfgForm = $("email-config-form");
  if (emailCfgForm) {
    emailCfgForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      await withBusy(emailCfgForm.querySelector("button[type=submit]"), async () => {
        try {
          const body = {
            enabled: $("email-enabled").checked,
            imap_host: $("email-host").value.trim(),
            imap_port: parseInt($("email-port").value, 10) || 993,
            username: $("email-username").value.trim(),
            folder: $("email-folder").value.trim() || "INBOX",
          };
          const pwd = $("email-password").value.trim();
          if (pwd) body.app_password = pwd;
          await api("/api/v1/email/config", { method: "POST", body: JSON.stringify(body) });
          $("email-password").value = "";
          toast("Read-only IMAP scanner settings saved.");
          await loadEmailConfig();
        } catch (err) {
          toast(err.message, true);
        }
      });
    });
  }

  const runEmailScan = (e) =>
    withBusy(e.currentTarget, async () => {
      try {
        const res = await api("/api/v1/email/scan", { method: "POST" });
        toast(`Email scan complete: ${res.queued_count || 0} item(s) added to review queue.`);
        await loadEmailQueue();
      } catch (err) {
        toast(err.message, true);
      }
    });
  bindClick("btn-email-scan-now", runEmailScan);
  bindClick("btn-email-test-scan", runEmailScan);

  // --- Multi-Provider AI Settings Forms ---
  const ocForm = $("opencode-settings-form");
  if (ocForm) {
    ocForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      await withBusy(ocForm.querySelector("button[type=submit]"), async () => {
        const body = {
          opencode_base_url: $("set-opencode-url").value.trim(),
          opencode_model: $("set-opencode-model").value,
          opencode_protocol: $("set-opencode-proto").value,
          opencode_max_daily_requests: parseInt($("set-opencode-quota").value, 10) || 60,
        };
        const key = $("set-opencode-key").value.trim();
        if (key) body.opencode_api_key = key;
        try {
          await api("/api/v1/settings", { method: "PATCH", body: JSON.stringify(body) });
          $("set-opencode-key").value = "";
          toast("OpenCode Go settings sealed in vault.");
          await loadSettings();
        } catch (err) {
          toast(err.message, true);
        }
      });
    });
  }
  bindClick("btn-clear-opencode-key", (e) =>
    withBusy(e.currentTarget, async () => {
      await api("/api/v1/settings", { method: "PATCH", body: JSON.stringify({ clear_opencode_key: true }) });
      toast("Cleared OpenCode Go key.");
      await loadSettings();
    })
  );
  bindClick("btn-fetch-models", (e) => withBusy(e.currentTarget, () => fetchProviderModels("opencode_go", true)));
  bindClick("btn-test-opencode", (e) =>
    withBusy(e.currentTarget, async () => {
      const out = $("opencode-test-result");
      out.textContent = "Testing OpenCode Go...";
      out.classList.remove("hidden");
      try {
        const res = await api("/api/v1/ai/test?provider=opencode_go", { method: "POST" });
        setNodeIconText(out, res.ok ? "check" : "alert", res.message);
      } catch (err) {
        setNodeIconText(out, "alert", err.message);
      }
    })
  );

  const gemForm = $("gemini-settings-form");
  if (gemForm) {
    gemForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      await withBusy(gemForm.querySelector("button[type=submit]"), async () => {
        const body = {
          gemini_base_url: $("set-gemini-url").value.trim(),
          gemini_model: $("set-gemini-model").value,
        };
        const key = $("set-gemini-key").value.trim();
        if (key) body.gemini_api_key = key;
        try {
          await api("/api/v1/settings", { method: "PATCH", body: JSON.stringify(body) });
          $("set-gemini-key").value = "";
          toast("Google Gemini settings sealed in vault.");
          await loadSettings();
        } catch (err) {
          toast(err.message, true);
        }
      });
    });
  }
  bindClick("btn-clear-gemini-key", (e) =>
    withBusy(e.currentTarget, async () => {
      await api("/api/v1/settings", { method: "PATCH", body: JSON.stringify({ clear_gemini_key: true }) });
      toast("Cleared Gemini key.");
      await loadSettings();
    })
  );
  bindClick("btn-fetch-gemini-models", (e) => withBusy(e.currentTarget, () => fetchProviderModels("gemini", true)));
  bindClick("btn-test-gemini", (e) =>
    withBusy(e.currentTarget, async () => {
      const out = $("gemini-test-result");
      out.textContent = "Testing Google Gemini...";
      out.classList.remove("hidden");
      try {
        const res = await api("/api/v1/ai/test?provider=gemini", { method: "POST" });
        setNodeIconText(out, res.ok ? "check" : "alert", res.message);
      } catch (err) {
        setNodeIconText(out, "alert", err.message);
      }
    })
  );

  const cxForm = $("codex-settings-form");
  if (cxForm) {
    cxForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      await withBusy(cxForm.querySelector("button[type=submit]"), async () => {
        const body = {
          codex_base_url: $("set-codex-url").value.trim(),
          codex_model: $("set-codex-model").value.trim(),
        };
        const key = $("set-codex-key").value.trim();
        if (key) body.codex_api_key = key;
        try {
          await api("/api/v1/settings", { method: "PATCH", body: JSON.stringify(body) });
          $("set-codex-key").value = "";
          toast("OpenAI / Codex settings sealed in vault.");
          await loadSettings();
        } catch (err) {
          toast(err.message, true);
        }
      });
    });
  }
  bindClick("btn-clear-codex-key", (e) =>
    withBusy(e.currentTarget, async () => {
      await api("/api/v1/settings", { method: "PATCH", body: JSON.stringify({ clear_codex_key: true }) });
      toast("Cleared OpenAI / Codex key.");
      await loadSettings();
    })
  );
  bindClick("btn-test-codex", (e) =>
    withBusy(e.currentTarget, async () => {
      const out = $("codex-test-result");
      out.textContent = "Testing OpenAI / Codex...";
      out.classList.remove("hidden");
      try {
        const res = await api("/api/v1/ai/test?provider=openai_codex", { method: "POST" });
        setNodeIconText(out, res.ok ? "check" : "alert", res.message);
      } catch (err) {
        setNodeIconText(out, "alert", err.message);
      }
    })
  );

  const wrForm = $("wrapper-settings-form");
  if (wrForm) {
    wrForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      await withBusy(wrForm.querySelector("button[type=submit]"), async () => {
        const body = {
          wrapper_base_url: $("set-wrapper-url").value.trim(),
          wrapper_model: $("set-wrapper-model").value.trim(),
          wrapper_wire_api: $("set-wrapper-wire-api").value,
          codex_cli_enabled: $("set-codex-cli-enabled").checked,
        };
        const key = $("set-wrapper-key").value.trim();
        if (key) body.wrapper_api_key = key;
        try {
          await api("/api/v1/settings", { method: "PATCH", body: JSON.stringify(body) });
          $("set-wrapper-key").value = "";
          toast("Chat-Wrapper & Codex CLI settings saved.");
          await loadSettings();
        } catch (err) {
          toast(err.message, true);
        }
      });
    });
  }
  bindClick("btn-test-wrapper", (e) =>
    withBusy(e.currentTarget, async () => {
      const out = $("wrapper-test-result");
      out.textContent = "Testing Chat-Wrapper...";
      out.classList.remove("hidden");
      try {
        const res = await api("/api/v1/ai/test?provider=chat_wrapper", { method: "POST" });
        setNodeIconText(out, res.ok ? "check" : "alert", res.message);
      } catch (err) {
        setNodeIconText(out, "alert", err.message);
      }
    })
  );

  // --- ADHD Profile & Backup Restore ---
  const profForm = $("profile-settings-form");
  if (profForm) {
    profForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      await withBusy(profForm.querySelector("button[type=submit]"), async () => {
        try {
          const body = {
            hourly_wage_cents: dollarsToCents($("set-hourly-wage").value),
            emergency_buffer_cents: dollarsToCents($("set-emergency-buffer").value, { allowZero: true }),
            default_cooling_hours: parseInt($("set-cooling-hours").value, 10) || 48,
            payday_cadence_days: parseInt($("set-payday-cadence").value, 10) || 14,
            expected_paycheck_cents: dollarsToCents($("set-expected-paycheck").value, { allowZero: true }),
          };
          if ($("set-next-payday").value) body.next_payday_date = $("set-next-payday").value;
          await api("/api/v1/settings", { method: "PATCH", body: JSON.stringify(body) });
          toast("ADHD profile updated.");
          await refreshAll();
        } catch (err) {
          toast(err.message, true);
        }
      });
    });
  }

  const backupInp = $("backup-import-input");
  if (backupInp) {
    backupInp.addEventListener("change", async (e) => {
      const file = e.target.files[0];
      if (!file) return;
      const previewBox = $("backup-preview-box");
      const fd = new FormData();
      fd.append("file", file);
      try {
        const prev = await api("/api/v1/backup/preview", { method: "POST", body: fd });
        e.target.value = "";
        if (!previewBox) return;
        const counts = (prev && prev.counts) || {};
        const dr = (prev && (prev.date_range || prev.tx_date_range)) || {};
        const rangeTxt = dr.start && dr.end ? `${dr.start} to ${dr.end}` : "No transactions";
        const goalsCnt = counts.goals ?? counts.savings_goals ?? 0;
        const confirmBtn = el("button", {
          type: "button",
          class: "btn btn-primary btn-xs",
          text: "Confirm & Replace Ledger",
        });
        const cancelBtn = el("button", {
          type: "button",
          class: "btn btn-ghost btn-xs",
          text: "Cancel",
        });
        cancelBtn.addEventListener("click", () => {
          previewBox.classList.add("hidden");
          previewBox.replaceChildren();
        });
        confirmBtn.addEventListener("click", () =>
          withBusy(confirmBtn, async () => {
            const importFd = new FormData();
            importFd.append("file", file);
            try {
              await api("/api/v1/backup/import", { method: "POST", body: importFd });
              previewBox.classList.add("hidden");
              previewBox.replaceChildren();
              toast("JSON backup restored.");
              await refreshAll({ refreshAI: true });
            } catch (impErr) {
              toast(impErr.message, true);
            }
          })
        );
        previewBox.classList.remove("hidden");
        previewBox.replaceChildren(
          el("div", { class: "stack-xs" }, [
            el("strong", { text: `Backup Preview (${file.name})` }),
            el("div", {
              class: "small tabular-nums",
              text:
                `${counts.accounts || 0} account(s) · ${counts.transactions || 0} transaction(s) (${rangeTxt}) · ` +
                `${counts.recurring_bills || 0} bill(s) · ${goalsCnt} goal(s) · ${counts.holdings || 0} holding(s)`,
            }),
            el("div", {
              class: "small muted",
              text: "Warning: Restoring this backup replaces your current local ledger tables (sealed API credentials stay untouched).",
            }),
            el("div", { class: "row-gap" }, [confirmBtn, cancelBtn]),
          ])
        );
      } catch (err) {
        e.target.value = "";
        toast(err.message, true);
      }
    });
  }

  // --- Delegated Inline Forms (Jar edit, Account reconcile, Tx edit) ---
  document.body.addEventListener("submit", async (e) => {
    const jarForm = e.target.closest("form[data-jar-edit-form]");
    if (jarForm) {
      e.preventDefault();
      const jarId = Number(jarForm.dataset.jarEditForm);
      const targetPct = Number(jarForm.elements.target_pct.value);
      if (!Number.isFinite(targetPct) || targetPct < 0 || targetPct > 100) {
        toast("Target share must be between 0% and 100%.", true);
        return;
      }
      try {
        await api(`/api/v1/jars/${jarId}`, {
          method: "PATCH",
          body: JSON.stringify({
            target_pct: targetPct,
            rollover_enabled: jarForm.elements.rollover_enabled.checked,
            is_locked: jarForm.elements.is_locked.checked,
          }),
        });
        state.editingJarId = null;
        toast("Jar updated.");
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
        await api(`/api/v1/accounts/${acctId}/balance`, {
          method: "PATCH",
          body: JSON.stringify({ balance_cents: balanceCents }),
        });
        state.reconcilingAcctId = null;
        toast("Account balance reconciled.");
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
        const payload = {
          tx_date: txEditForm.elements.tx_date.value,
          merchant: txEditForm.elements.merchant.value.trim(),
          amount_cents: dollarsToCents(txEditForm.elements.amount.value),
          memo: txEditForm.elements.memo.value.trim(),
          learn_merchant_rule: true,
        };
        const jarVal = txEditForm.elements.jar_id && !txEditForm.elements.jar_id.disabled ? txEditForm.elements.jar_id.value : "";
        if (jarVal) payload.jar_id = Number(jarVal);
        await api(`/api/v1/transactions/${txId}`, { method: "PATCH", body: JSON.stringify(payload) });
        state.editingTxId = null;
        toast("Transaction updated.");
        await refreshAll();
        await loadTransactions();
      } catch (err) {
        toast(err.message, true);
      }
    }
  });

  // --- Delegated Click Actions ---
  document.body.addEventListener("click", async (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;

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
    if (btn.dataset.txInspect) {
      openTxInspector(Number(btn.dataset.txInspect));
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
    if (btn.dataset.deleteTx) {
      await withBusy(btn, async () => {
        try {
          await api(`/api/v1/transactions/${btn.dataset.deleteTx}`, { method: "DELETE" });
          toast("Transaction deleted.");
          await refreshAll();
          await loadTransactions();
        } catch (err) {
          toast(err.message, true);
        }
      });
      return;
    }
    if (btn.dataset.rebalanceFrom) {
      await withBusy(btn, async () => {
        try {
          await api("/api/v1/budget/rebalance", {
            method: "POST",
            body: JSON.stringify({
              from_jar_id: Number(btn.dataset.rebalanceFrom),
              to_jar_id: Number(btn.dataset.rebalanceTo),
              amount_cents: Number(btn.dataset.rebalanceAmt),
              reason: "One-click zero-guilt rebalance",
            }),
          });
          toast("Jars rebalanced.");
          await refreshAll();
        } catch (err) {
          toast(err.message, true);
        }
      });
      return;
    }
    if (btn.dataset.impulseDecide) {
      await withBusy(btn, async () => {
        try {
          const res = await api(`/api/v1/impulses/${btn.dataset.impulseDecide}/decide`, {
            method: "POST",
            body: JSON.stringify({ decision: btn.dataset.decision, sweep_savings_pct: 50 }),
          });
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
          await api(`/api/v1/transactions/${btn.dataset.triageTx}`, {
            method: "PATCH",
            body: JSON.stringify({ jar_id: Number(btn.dataset.triageJar), learn_merchant_rule: true }),
          });
          toast("Categorized & merchant rule learned.");
          await refreshAll();
          await loadWeeklyReset();
        } catch (err) {
          toast(err.message, true);
        }
      });
      return;
    }
    if (btn.dataset.toggleZombie) {
      await withBusy(btn, async () => {
        try {
          await api(`/api/v1/bills/${btn.dataset.toggleZombie}/zombie`, {
            method: "PATCH",
            body: JSON.stringify({ flagged: btn.dataset.zombieNext === "true" }),
          });
          toast("Bill review flag updated.");
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
          await api(`/api/v1/bills/${btn.dataset.archiveBill}`, { method: "DELETE" });
          toast("Bill archived.");
          await refreshAll();
        } catch (err) {
          toast(err.message, true);
        }
      });
      return;
    }
    if (btn.dataset.emailApprove) {
      await withBusy(btn, async () => {
        try {
          await api(`/api/v1/email/queue/${btn.dataset.emailApprove}/approve`, { method: "POST" });
          toast("Email bill approved & added.");
          await loadEmailQueue();
          await refreshAll();
        } catch (err) {
          toast(err.message, true);
        }
      });
      return;
    }
    if (btn.dataset.emailDismiss) {
      await withBusy(btn, async () => {
        try {
          await api(`/api/v1/email/queue/${btn.dataset.emailDismiss}/dismiss`, { method: "POST" });
          toast("Email queue item dismissed.");
          await loadEmailQueue();
        } catch (err) {
          toast(err.message, true);
        }
      });
      return;
    }
    if (btn.dataset.connSync) {
      await withBusy(btn, async () => {
        try {
          const prov = btn.dataset.connProvider;
          const connId = Number(btn.dataset.connSync);
          const url =
            prov === "plaid"
              ? "/api/v1/plaid/sync"
              : prov === "simplefin"
              ? "/api/v1/simplefin/sync"
              : prov && prov.startsWith("credit_api_")
              ? `/api/v1/credit/apis/${connId}/sync`
              : `/api/v1/brokers/${connId}/sync`;
          const opts =
            prov === "plaid" || prov === "simplefin"
              ? { method: "POST", body: JSON.stringify({ connection_id: connId }) }
              : { method: "POST" };
          const syncRes = await api(url, opts);
          toast(
            (syncRes && syncRes.history_note) || `Synced ${String(prov || "").toUpperCase()} connection.`
          );
          await loadConnections();
          await refreshAll();
        } catch (err) {
          toast(err.message, true);
          await loadConnections();
        }
      });
      return;
    }
    if (btn.dataset.connDelete) {
      await withBusy(btn, async () => {
        try {
          await api(`/api/v1/connections/${btn.dataset.connDelete}`, { method: "DELETE" });
          toast("Connection removed.");
          await loadConnections();
        } catch (err) {
          toast(err.message, true);
        }
      });
    }
  });

  // Inline jar select on transactions table
  document.body.addEventListener("change", async (e) => {
    const sel = e.target.closest("select[data-tx-jar-select]");
    if (!sel || !sel.value) return;
    try {
      await api(`/api/v1/transactions/${sel.dataset.txJarSelect}`, {
        method: "PATCH",
        body: JSON.stringify({ jar_id: Number(sel.value), learn_merchant_rule: true }),
      });
      toast("Jar updated & merchant rule learned.");
      await refreshAll();
    } catch (err) {
      toast(err.message, true);
    }
  });
}
