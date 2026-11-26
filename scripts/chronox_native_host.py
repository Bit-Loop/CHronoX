#!/usr/bin/env python3
"""
ChronoX Native Messaging Host (`com.chronox.credit_bridge`) for Microsoft Edge / Chrome.

Protocol (Chromium Native Messaging):
- Reads `[uint32_le length][UTF-8 JSON bytes]` from `sys.stdin.buffer`.
- Writes `[uint32_le length][UTF-8 JSON bytes]` to `sys.stdout.buffer`.

Security & Architecture:
- Spawned directly by Edge/Chrome ONLY for the allowlisted `chrome-extension://<ID>/` origin.
- Zero open TCP ports, zero bearer tokens to copy-paste, zero stored portal passwords.
- You authenticate & complete 2FA normally in your real Edge browser tab on Credit Karma
  or Experian; clicking "Sync to ChronoX" sends the rendered page snapshot straight over
  stdio into ChronoX's `OfficialCreditReportImporter` and SQLite WAL ledger (`finance.sqlite3`).
"""

from __future__ import annotations

import json
import os
import struct
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from chronox_finance.connectors.credit_providers import OfficialCreditReportImporter  # noqa: E402
from chronox_finance.engines.credit_debt import CreditDebtOptimizer  # noqa: E402
from chronox_finance.storage import FinanceStorage  # noqa: E402
from scripts.credit_bridge_server import CreditBridgeStore  # noqa: E402

DEFAULT_DATA_DIR = Path.home() / ".config" / "ChronoX Finance"


def resolve_db_path(data_dir: Optional[Path] = None) -> Path:
    env_db = (os.getenv("CHRONOX_FINANCE_DB_PATH") or "").strip()
    base = data_dir or DEFAULT_DATA_DIR
    desktop_db = base / "finance.sqlite3"
    if desktop_db.exists():
        return desktop_db
    if env_db:
        p = Path(env_db)
        return p if p.is_absolute() else (REPO_ROOT / p)
    return desktop_db


def _issuer_tokens(text: str) -> set[str]:
    stop = {"inc", "llc", "corp", "bank", "credit", "union", "cr", "na", "usa", "the", "and", "of", "card", "account", "finance", "auto", "loan"}
    alias_map = {
        "caf": "carmax",
        "depted": "central",
        "cri": "central",
        "nfcu": "navy",
    }
    raw_words = [w for w in "".join(ch.lower() if ch.isalnum() else " " for ch in text).split() if len(w) >= 3]
    expanded = {alias_map.get(w, w) for w in raw_words}
    return expanded - stop


def sync_snapshot_to_chronox_db(
    payload: Dict[str, Any],
    *,
    db_path: Optional[Path] = None,
    data_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Parse a browser-captured Credit Karma / Experian payload, merge into `CreditBridgeStore`,
    and idempotently upsert scores, tradelines, inquiries, and derogatories into `finance.sqlite3`.
    """
    eff_dir = data_dir or DEFAULT_DATA_DIR
    eff_db = db_path or resolve_db_path(eff_dir)
    eff_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    # Save local diagnostic trace (0600) so DOM layout drift can be inspected instantly
    diag_path = eff_dir / "last_extension_capture.json"
    try:
        raw_txt = str(payload.get("page_text") or payload.get("text") or "")
        diag_obj = {
            "captured_at": date.today().isoformat(),
            "url": str(payload.get("url") or ""),
            "title": str(payload.get("title") or ""),
            "source": str(payload.get("source") or ""),
            "bureau_hint": str(payload.get("bureau_hint") or ""),
            "structured_reports": payload.get("reports") or [],
            "structured_tradelines_count": len(payload.get("tradelines") or []),
            "text_length": len(raw_txt),
            "text_preview": raw_txt[:8000],
        }
        fd = os.open(diag_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(diag_obj, fh, indent=2)
    except OSError:
        pass

    importer = OfficialCreditReportImporter()
    bridge_store = CreditBridgeStore(eff_dir)
    bridge_store.ingest_payload(payload)

    # Parse ONLY the current payload for immediate DB upsert counts (while bridge_store retains full 3B cache)
    bundles = []
    if any(payload.get(k) for k in ("reports", "tradelines", "inquiries", "derogatories")):
        bundles.append(importer.import_structured(payload, needs_review=False))
    raw_text = str(payload.get("page_text") or payload.get("text") or "").strip()
    if raw_text:
        bundles.append(
            importer.import_portal_text(
                raw_text,
                bureau_hint=str(payload.get("bureau_hint") or payload.get("bureau") or "auto"),
                source_tag=str(payload.get("source") or "edge_credit_companion"),
                needs_review=False,
            )
        )

    counts = {
        "scores_upserted": 0,
        "tradelines_added": 0,
        "tradelines_updated": 0,
        "inquiries_added": 0,
        "derogatories_added": 0,
    }

    storage = FinanceStorage(eff_db)
    try:
        # 1. Scores (upsert same-day bureau+model, preserve historical dates)
        existing_reports = [r for r in storage.list_credit_reports(include_sandbox=False) if not r.get("is_sandbox")]
        rep_by_day = {(str(r["bureau"]), str(r["model"]), str(r["pulled_date"])): r for r in existing_reports}
        seen_score_keys: set[tuple[str, str, str]] = set()
        for b in bundles:
            for rep in b.reports:
                if rep.score is None or not (300 <= int(rep.score) <= 850):
                    continue
                model_val = rep.model if rep.model in ("fico8", "vantage3", "fico9", "other") else "other"
                day_key = (rep.bureau, model_val, rep.pulled_date)
                if day_key in seen_score_keys:
                    continue
                seen_score_keys.add(day_key)
                existing = rep_by_day.get(day_key)
                if existing is not None:
                    if int(existing["score"]) != int(rep.score):
                        with storage.transaction(immediate=True) as conn:
                            conn.execute(
                                "UPDATE credit_reports SET score = ?, source = ? WHERE id = ?",
                                (int(rep.score), rep.source or "edge_companion", int(existing["id"])),
                            )
                        counts["scores_upserted"] += 1
                else:
                    created_r = storage.create_credit_report(
                        {
                            "bureau": rep.bureau,
                            "model": model_val,
                            "score": int(rep.score),
                            "pulled_date": rep.pulled_date,
                            "source": rep.source or "edge_companion",
                        }
                    )
                    rep_by_day[day_key] = created_r
                    counts["scores_upserted"] += 1

        # 2. Tradelines (smart match by exact name OR unique single-kind issuer match so Plaid cards aren't duplicated)
        active_lines = [
            cl for cl in storage.list_credit_lines(active_only=False, include_sandbox=False) if not cl.get("is_sandbox")
        ]
        by_name = {str(cl["name"]).strip().lower(): cl for cl in active_lines}

        seen_tl_names: set[str] = set()
        for b in bundles:
            for tl in b.tradelines:
                pl = tl.to_credit_line_payload()
                tname = str(pl.get("name") or "").strip()
                if not tname:
                    continue
                tkey = tname.lower()
                if tkey in seen_tl_names:
                    continue
                seen_tl_names.add(tkey)

                target = by_name.get(tkey)
                if target is None:
                    # Exact (kind, balance_cents) + overlapping issuer brand match (handles 2 student loans under DEPTED/CRI)
                    cand_toks = _issuer_tokens(f"{tname} {pl.get('issuer', '')}")
                    if cand_toks:
                        bal_matches = [
                            cl
                            for cl in active_lines
                            if str(cl.get("kind")) == str(pl.get("kind"))
                            and int(cl.get("balance_cents", -1)) == int(pl.get("balance_cents", -2))
                            and bool(cand_toks & _issuer_tokens(f"{cl.get('name', '')} {cl.get('issuer', '')}"))
                        ]
                        if len(bal_matches) == 1:
                            target = bal_matches[0]
                        else:
                            matches = [
                                cl
                                for cl in active_lines
                                if str(cl.get("kind")) == str(pl.get("kind"))
                                and bool(cand_toks & _issuer_tokens(f"{cl.get('name', '')} {cl.get('issuer', '')}"))
                            ]
                            if len(matches) == 1:
                                target = matches[0]

                if target is not None:
                    has_plaid_ext = bool(str(target.get("external_account_id") or "").strip())
                    updates: Dict[str, Any] = {}
                    # Keep Plaid live balance/APR authoritative if present; fill missing limits/opened_date
                    if not has_plaid_ext and int(pl.get("balance_cents", 0)) >= 0:
                        updates["balance_cents"] = int(pl["balance_cents"])
                    if int(pl.get("credit_limit_cents", 0)) > 0 and int(target.get("credit_limit_cents", 0)) == 0:
                        updates["credit_limit_cents"] = int(pl["credit_limit_cents"])
                    if int(pl.get("apr_bps", 0)) > 0 and int(target.get("apr_bps", 0)) == 0:
                        updates["apr_bps"] = int(pl["apr_bps"])
                    if pl.get("opened_date") and not target.get("opened_date"):
                        updates["opened_date"] = pl["opened_date"]
                    if updates:
                        storage.update_credit_line(int(target["id"]), updates)
                        counts["tradelines_updated"] += 1
                else:
                    clean_pl = {
                        k: v
                        for k, v in pl.items()
                        if k not in ("apr_provided", "closing_day_provided", "due_day_provided", "statement_balance_cents", "next_due_date", "is_overdue", "source")
                    }
                    created_cl = storage.create_credit_line(clean_pl)
                    active_lines.append(created_cl)
                    by_name[tkey] = created_cl
                    counts["tradelines_added"] += 1

        # 3. Hard Inquiries
        existing_inqs = {
            (str(i["lender"]).strip().lower(), str(i["bureau"]), str(i["inquiry_date"]))
            for i in storage.list_credit_inquiries(include_sandbox=False)
        }
        for b in bundles:
            for inq in b.inquiries:
                ikey = (inq.lender.strip().lower(), inq.bureau, inq.inquiry_date)
                if ikey in existing_inqs:
                    continue
                storage.create_credit_inquiry(
                    {
                        "lender": inq.lender,
                        "bureau": inq.bureau,
                        "inquiry_date": inq.inquiry_date,
                        "counts_until": inq.counts_until,
                        "drops_off": inq.drops_off,
                    }
                )
                existing_inqs.add(ikey)
                counts["inquiries_added"] += 1

        # 4. Derogatories & Public Records
        valid_derog = {
            "late_30",
            "late_60",
            "late_90",
            "charge_off",
            "collection",
            "bankruptcy_ch7",
            "bankruptcy_ch13",
            "other",
        }
        existing_derogs = storage.list_credit_derogatories(active_only=False, include_sandbox=False)
        for b in bundles:
            for der in b.derogatories:
                dkind = der.kind if der.kind in valid_derog else "other"
                lender_str = (der.lender or der.creditor or "").strip()
                cand_toks = _issuer_tokens(lender_str)
                is_dup = False
                for ex in existing_derogs:
                    if str(ex["kind"]) != dkind:
                        continue
                    if dkind.startswith("bankruptcy") and str(ex["opened_date"]) == str(der.opened_date):
                        is_dup = True
                        break
                    ex_toks = _issuer_tokens(str(ex.get("lender", "")))
                    if cand_toks and ex_toks and (cand_toks & ex_toks):
                        is_dup = True
                        break
                    if lender_str.lower() == str(ex.get("lender", "")).strip().lower():
                        is_dup = True
                        break
                if is_dup:
                    continue
                created_d = storage.create_credit_derogatory(
                    {
                        "kind": dkind,
                        "lender": lender_str[:100],
                        "opened_date": der.opened_date,
                        "drops_off": der.drops_off,
                        "active": der.active,
                        "notes": f"Synced via ChronoX Browser Companion ({b.raw_source_format})",
                    }
                )
                existing_derogs.append(created_d)
                counts["derogatories_added"] += 1

        summary = build_live_credit_summary(storage)
        return {"ok": True, "synced": counts, "summary": summary}
    finally:
        storage.close()


def build_live_credit_summary(storage: FinanceStorage) -> Dict[str, Any]:
    opt = CreditDebtOptimizer()
    rep = opt.analyze(
        raw_lines=storage.list_credit_lines(active_only=True),
        smoothed_monthly_income_cents=400_000,
        credit_reports=storage.list_credit_reports(),
        credit_inquiries=storage.list_credit_inquiries(),
        credit_derogatories=storage.list_credit_derogatories(),
        today=date.today(),
    )
    scores = [
        {
            "bureau": s.bureau,
            "model": s.model,
            "score": s.score,
            "pulled_date": s.pulled_date,
        }
        for s in (rep.factor_analysis.latest_actual_scores or [])
    ]
    return {
        "latest_scores": scores,
        "total_debt_cents": rep.total_debt_cents,
        "aggregate_utilization_pct": rep.aggregate_utilization_pct,
        "credit_lines_count": len(rep.credit_lines),
        "inquiries_count": len(rep.credit_inquiries),
        "derogatories_count": len(rep.credit_derogatories),
        "recommended_strategy": rep.recommended_strategy,
    }


def handle_native_message(msg: Dict[str, Any], *, db_path: Optional[Path] = None, data_dir: Optional[Path] = None) -> Dict[str, Any]:
    action = str(msg.get("action") or "ping").strip().lower()
    eff_db = db_path or resolve_db_path(data_dir)
    if action == "ping":
        storage = FinanceStorage(eff_db)
        try:
            return {
                "ok": True,
                "mode": "native_messaging",
                "db_path": str(eff_db),
                "summary": build_live_credit_summary(storage),
            }
        finally:
            storage.close()

    if action == "sync_credit":
        payload = msg.get("payload") if isinstance(msg.get("payload"), dict) else msg
        return sync_snapshot_to_chronox_db(payload, db_path=eff_db, data_dir=data_dir)

    return {"ok": False, "error": f"Unknown action '{action}'"}


def _read_native_frame() -> Optional[Dict[str, Any]]:
    raw_len = sys.stdin.buffer.read(4)
    if not raw_len or len(raw_len) < 4:
        return None
    msg_len = struct.unpack("<I", raw_len)[0]
    if msg_len <= 0 or msg_len > 20 * 1024 * 1024:
        return None
    body = sys.stdin.buffer.read(msg_len)
    return json.loads(body.decode("utf-8"))


def _write_native_frame(payload: Dict[str, Any]) -> None:
    encoded = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(encoded)))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def main() -> int:
    while True:
        try:
            frame = _read_native_frame()
            if frame is None:
                break
            resp = handle_native_message(frame)
            _write_native_frame(resp)
        except Exception as exc:
            _write_native_frame({"ok": False, "error": str(exc)})
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
