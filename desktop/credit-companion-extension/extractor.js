"use strict";

/**
 * Deep DOM + ShadowRoot + SVG + JSON-State extractor for Credit Karma
 * (TransUnion & Equifax VantageScore 3.0), Experian (FICO Score 8), and AnnualCreditReport.
 *
 * Why a custom walker instead of `document.body.innerText` alone:
 * 1. Credit Karma's Intuit Design System renders score gauges & cards inside Shadow DOM
 *    and `<svg><text>` nodes, both of which `HTMLElement.innerText` skips in Chromium.
 * 2. Model labels like "VantageScore 3.0" or "+4 pts" contain digits that break naive
 *    `[^\d]+` regexes between "TransUnion"/"Equifax" and the 3-digit score.
 */
function chronoxExtractPageSnapshot() {
  const host = (location.hostname || "").toLowerCase();
  const path = (location.pathname || "").toLowerCase();
  const isCreditKarma = host.includes("creditkarma.com");
  const isExperian = host.includes("experian.com");
  const todayIso = new Date().toISOString().slice(0, 10);

  // 1. Walk open Shadow Roots, ARIA labels, data-testid nodes, and SVG <text> nodes
  const textChunks = [];
  const ariaSnippets = [];
  const visited = new Set();

  function walkTree(root) {
    if (!root || visited.has(root)) return;
    visited.add(root);
    const walker = document.createTreeWalker(
      root,
      NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT,
      null
    );
    let node = walker.currentNode;
    while (node) {
      if (node.nodeType === Node.ELEMENT_NODE) {
        const tag = (node.tagName || "").toLowerCase();
        if (tag === "script" || tag === "style" || tag === "noscript") {
          node = walker.nextSibling() || walker.nextNode();
          continue;
        }
        if (node.id === "chronox-companion-pill-root") {
          node = walker.nextSibling() || walker.nextNode();
          continue;
        }
        const aria = (node.getAttribute && node.getAttribute("aria-label")) || "";
        if (aria && aria.length <= 240) {
          ariaSnippets.push(aria.trim());
        }
        if (node.shadowRoot) {
          walkTree(node.shadowRoot);
        }
      } else if (node.nodeType === Node.TEXT_NODE) {
        const val = (node.nodeValue || "").replace(/\s+/g, " ").trim();
        if (val) textChunks.push(val);
      }
      node = walker.nextNode();
    }
  }

  walkTree(document.documentElement || document.body);

  const bodyInnerText = (document.body && document.body.innerText) || "";
  const deepText = textChunks.join("\n");
  const fullText = `${ariaSnippets.join("\n")}\n${bodyInnerText}\n${deepText}`;

  // 2. Scan embedded JSON script tags (__NEXT_DATA__, Apollo/Relay state) for scores & accounts
  const reports = [];
  const tradelines = [];
  const inquiries = [];
  const derogatories = [];
  const seenScores = new Set();
  const scaleAnchors = new Set([300, 579, 669, 739, 799, 850]);

  const addScore = (bureau, model, rawVal) => {
    const n = Number(rawVal);
    if (!Number.isInteger(n) || n < 300 || n > 850 || scaleAnchors.has(n)) return;
    const bNorm = String(bureau || "").toLowerCase();
    const key = `${bNorm}|${model}`;
    if (seenScores.has(key)) return;
    seenScores.add(key);
    reports.push({
      bureau: bNorm,
      model,
      score: n,
      pulled_date: todayIso,
      source: isCreditKarma
        ? "credit_karma_edge"
        : isExperian
        ? "experian_edge"
        : "browser_companion",
    });
  };

  function scanJsonObjectForCredit(obj, depth = 0) {
    if (!obj || typeof obj !== "object" || depth > 12) return;
    if (Array.isArray(obj)) {
      for (let i = 0; i < Math.min(obj.length, 250); i++) {
        scanJsonObjectForCredit(obj[i], depth + 1);
      }
      return;
    }
    const keys = Object.keys(obj);
    // Score object heuristic in Intuit/CK/Experian GraphQL caches
    const rawBureau =
      obj.bureau || obj.bureauId || obj.creditBureau || obj.source || obj.agency || "";
    const rawScore =
      obj.score ?? obj.scoreValue ?? obj.creditScore ?? obj.vantageScore ?? obj.value;
    if (rawBureau && rawScore != null && /^(3|4|5|6|7|8)\d{2}$/.test(String(rawScore).trim())) {
      const bLow = String(rawBureau).toLowerCase();
      if (bLow.includes("trans") || bLow === "tu") {
        addScore("transunion", "vantage3", parseInt(rawScore, 10));
      } else if (bLow.includes("equifax") || bLow === "efx" || bLow === "eq") {
        addScore("equifax", "vantage3", parseInt(rawScore, 10));
      } else if (bLow.includes("experian") || bLow === "exp") {
        addScore("experian", isCreditKarma ? "vantage3" : "fico8", parseInt(rawScore, 10));
      }
    }

    // Tradeline / Account object heuristic in GraphQL caches
    const acctName = obj.creditorName || obj.institutionName || obj.accountName || obj.lenderName;
    const acctBal = obj.balance ?? obj.currentBalance ?? obj.amountOwed;
    if (
      acctName &&
      typeof acctName === "string" &&
      acctBal !== undefined &&
      acctBal !== null &&
      !isNaN(Number(String(acctBal).replace(/[$,]/g, "")))
    ) {
      const statusStr = String(obj.status || obj.accountStatus || "open").toLowerCase();
      if (!statusStr.includes("closed")) {
        const rawLim = obj.creditLimit ?? obj.limit ?? obj.highCredit ?? 0;
        const rawKind = String(obj.accountType || obj.category || obj.type || "credit_card");
        tradelines.push({
          name: acctName.trim().slice(0, 80),
          issuer: String(obj.issuerName || acctName).trim().slice(0, 60),
          kind: rawKind,
          balance: Number(String(acctBal).replace(/[$,]/g, "")),
          credit_limit: Number(String(rawLim || 0).replace(/[$,]/g, "")) || 0,
          apr: obj.apr ?? obj.interestRate ?? undefined,
          opened_date: obj.dateOpened || obj.openedDate || undefined,
        });
      }
    }

    for (const k of keys) {
      const v = obj[k];
      if (v && typeof v === "object") {
        scanJsonObjectForCredit(v, depth + 1);
      }
    }
  }

  document.querySelectorAll('script[type="application/json"], script#__NEXT_DATA__').forEach((scr) => {
    const raw = (scr.textContent || "").trim();
    if (!raw || raw.length > 5_000_000) return;
    try {
      scanJsonObjectForCredit(JSON.parse(raw), 0);
    } catch (_e) {
      // Ignore non-JSON or malformed script blocks
    }
  });

  // 3. Robust text/gauge score extraction (neutralizing "3.0", "8", "+/-pts", and dates first!)
  const scrubbedForScores = fullText
    .slice(0, 30000)
    .replace(/VantageScore\s*®?\s*3(?:\.0)?/gi, "VantageScore")
    .replace(/FICO\s*®?\s*(?:Score\s*)?(?:8|9|10T?)/gi, "FICOScore")
    .replace(/[+-]\s*\d+\s*(?:pts?|points?)/gi, " ")
    .replace(/\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:,?\s+\d{4})?/gi, " ");

  if (isCreditKarma) {
    // Forward: "TransUnion ... 618" (allows newlines, punctuation, words up to 140 chars)
    const fw = /\b(TransUnion|Trans\s+Union|Equifax)\b[\s\S]{0,140}?(?<![\$.,\d])\b([3-8]\d{2})\b(?![\d%])/gi;
    let m;
    while ((m = fw.exec(scrubbedForScores)) !== null) {
      const b = m[1].toLowerCase().includes("trans") ? "transunion" : "equifax";
      addScore(b, "vantage3", parseInt(m[2], 10));
    }
    // Reverse: "618 ... TransUnion" (Credit Karma dial often renders number above bureau label)
    const rv = /(?<![\$.,\d])\b([3-8]\d{2})\b(?![\d%])[\s\S]{0,140}?\b(TransUnion|Trans\s+Union|Equifax)\b/gi;
    while ((m = rv.exec(scrubbedForScores)) !== null) {
      const b = m[2].toLowerCase().includes("trans") ? "transunion" : "equifax";
      addScore(b, "vantage3", parseInt(m[1], 10));
    }
    // Single-bureau subpage fallback (/credit-health/transunion/... or /equifax/...)
    if (!reports.length && (path.includes("transunion") || path.includes("equifax"))) {
      const targetB = path.includes("transunion") ? "transunion" : "equifax";
      const singleRe = /(?<![\$.,\d])\b([3-8]\d{2})\b(?![\d%])\s*(?:Poor|Fair|Good|Very\s+Good|Excellent|Needs\s+work)/i;
      const sm = singleRe.exec(scrubbedForScores);
      if (sm) addScore(targetB, "vantage3", parseInt(sm[1], 10));
    }
  } else if (isExperian) {
    const expRe = /(?:FICOScore|Experian\s+credit\s+report)[\s\S]{0,180}?(?<![\$.,\d])\b([3-8]\d{2})\b(?![\d%])/i;
    const m = expRe.exec(scrubbedForScores);
    if (m) addScore("experian", "fico8", parseInt(m[1], 10));
  }

  let bureauHint = "auto";
  if (isExperian) bureauHint = "experian";
  else if (path.includes("transunion")) bureauHint = "transunion";
  else if (path.includes("equifax")) bureauHint = "equifax";

  return {
    source: isCreditKarma
      ? "credit_karma_edge"
      : isExperian
      ? "experian_edge"
      : "browser_companion",
    url: location.href,
    title: document.title || "",
    pulled_date: todayIso,
    bureau_hint: bureauHint,
    reports,
    tradelines,
    inquiries,
    derogatories,
    page_text: fullText.slice(0, 120000),
  };
}

if (typeof window !== "undefined") {
  window.__chronoxExtractPageSnapshot = chronoxExtractPageSnapshot;
}
