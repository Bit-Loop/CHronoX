#!/usr/bin/env python3
"""
ChronoX Local Loopback Credit Bridge (`127.0.0.1:8749`).

Bridges user-authenticated Credit Karma (TransUnion + Equifax VantageScore 3.0) and
Experian.com (FICO Score 8) sessions from your real browser into ChronoX's built-in
`BureauAPIAdapter(vendor="custom")` — with ZERO B2B cost, ZERO stored portal passwords,
and ZERO anti-bot/CAPTCHA interference (you log in and complete 2FA normally in your
own browser tab, then click "Capture Tab -> ChronoX" in the companion extension or
drop a saved PDF/HTML into the credit-drop folder).

Endpoints (bound strictly to `127.0.0.1`):
- `GET  /health`          -> Bridge status & cached record counts
- `GET  /credit/snapshot` -> Structured JSON consumed by ChronoX `custom` Credit API
- `POST /credit/ingest`   -> Receives DOM/text/JSON capture from Companion Extension
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import shutil
import sys
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from chronox_finance.connectors.credit_providers import OfficialCreditReportImporter  # noqa: E402

DEFAULT_DATA_DIR = Path.home() / ".config" / "ChronoX Finance"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8749
MAX_BODY_BYTES = 15 * 1024 * 1024


def _atomic_write_0600(path: Path, raw: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(raw)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def load_or_create_bridge_token(token_path: Path) -> str:
    if token_path.exists():
        existing = token_path.read_text(encoding="utf-8").strip()
        if len(existing) >= 24:
            return existing
    tok = f"cxcred_{secrets.token_urlsafe(32)}"
    _atomic_write_0600(token_path, (tok + "\n").encode("utf-8"))
    return tok


class CreditBridgeStore:
    """Thread-safe local 3-bureau state accumulator + drop-folder scanner."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.state_path = data_dir / "credit_bridge_state.json"
        self.drop_dir = data_dir / "credit-drop"
        self.processed_dir = self.drop_dir / "processed"
        self.drop_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.processed_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.importer = OfficialCreditReportImporter()

    def load_state(self) -> Dict[str, Any]:
        if not self.state_path.exists():
            return {
                "bureau": "experian",
                "pulled_date": date.today().isoformat(),
                "reports": [],
                "tradelines": [],
                "inquiries": [],
                "derogatories": [],
            }
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return {
            "bureau": "experian",
            "pulled_date": date.today().isoformat(),
            "reports": [],
            "tradelines": [],
            "inquiries": [],
            "derogatories": [],
        }

    def save_state(self, state: Dict[str, Any]) -> None:
        _atomic_write_0600(
            self.state_path,
            json.dumps(state, indent=2, sort_keys=True).encode("utf-8"),
        )

    def merge_bundle_dict(self, incoming: Dict[str, Any]) -> Dict[str, int]:
        state = self.load_state()
        today_iso = date.today().isoformat()
        state["pulled_date"] = str(incoming.get("pulled_date") or today_iso)

        # 1. Merge scores (keep latest per bureau+model+pulled_date)
        reports_by_key: Dict[str, Dict[str, Any]] = {}
        for r in state.get("reports", []):
            if isinstance(r, dict) and r.get("score") is not None:
                k = f"{r.get('bureau')}|{r.get('model')}|{r.get('pulled_date')}"
                reports_by_key[k] = r
        added_scores = 0
        for r in incoming.get("reports", []):
            if not isinstance(r, dict) or r.get("score") is None:
                continue
            k = f"{r.get('bureau')}|{r.get('model')}|{r.get('pulled_date') or today_iso}"
            reports_by_key[k] = {
                "bureau": r.get("bureau", "other"),
                "model": r.get("model", "vantage3"),
                "score": int(r["score"]),
                "pulled_date": r.get("pulled_date") or today_iso,
                "source": r.get("source") or "credit_companion_bridge",
            }
            added_scores += 1
        state["reports"] = list(reports_by_key.values())

        # 2. Merge tradelines by normalized name (upserting fresher balances/limits)
        tl_by_name: Dict[str, Dict[str, Any]] = {
            str(t.get("name", "")).strip().lower(): dict(t)
            for t in state.get("tradelines", [])
            if isinstance(t, dict) and str(t.get("name", "")).strip()
        }
        upserted_tl = 0
        raw_tls = incoming.get("credit_line_payloads") or incoming.get("tradelines") or []
        for t in raw_tls:
            if not isinstance(t, dict):
                continue
            name = str(t.get("name") or "").strip()
            if not name:
                continue
            key = name.lower()
            prev = tl_by_name.get(key, {})
            merged = dict(prev)
            for fk, fv in t.items():
                if fv is not None and fv != "" and not (fk in ("credit_limit_cents", "apr_bps", "min_payment_cents") and fv == 0 and prev.get(fk, 0) > 0):
                    merged[fk] = fv
            tl_by_name[key] = merged
            upserted_tl += 1
        state["tradelines"] = list(tl_by_name.values())

        # 3. Merge inquiries
        inq_by_key: Dict[str, Dict[str, Any]] = {
            f"{str(i.get('lender','')).strip().lower()}|{i.get('bureau')}|{i.get('inquiry_date')}": i
            for i in state.get("inquiries", [])
            if isinstance(i, dict)
        }
        added_inq = 0
        for idx, i in enumerate(incoming.get("inquiries", [])):
            if not isinstance(i, dict) or not i.get("inquiry_date"):
                continue
            k = f"{str(i.get('lender','')).strip().lower()}|{i.get('bureau','other')}|{i.get('inquiry_date')}|{idx}"
            # Preserve multiple same-day auto-shopping pulls from the same lender if explicitly present
            dedup_k = f"{str(i.get('lender','')).strip().lower()}|{i.get('bureau','other')}|{i.get('inquiry_date')}"
            if dedup_k not in inq_by_key:
                inq_by_key[dedup_k] = i
                added_inq += 1
            elif k not in inq_by_key and incoming.get("preserve_same_day_duplicates"):
                inq_by_key[k] = i
                added_inq += 1
        state["inquiries"] = list(inq_by_key.values())

        # 4. Merge derogatories
        der_by_key: Dict[str, Dict[str, Any]] = {
            f"{d.get('kind')}|{str(d.get('creditor') or d.get('lender') or '').strip().lower()}|{d.get('opened_date')}": d
            for d in state.get("derogatories", [])
            if isinstance(d, dict)
        }
        added_der = 0
        for d in incoming.get("derogatories", []):
            if not isinstance(d, dict) or not d.get("opened_date"):
                continue
            k = f"{d.get('kind')}|{str(d.get('creditor') or d.get('lender') or '').strip().lower()}|{d.get('opened_date')}"
            der_by_key[k] = d
            added_der += 1
        state["derogatories"] = list(der_by_key.values())

        self.save_state(state)
        return {
            "scores_merged": added_scores,
            "tradelines_merged": upserted_tl,
            "inquiries_merged": added_inq,
            "derogatories_merged": added_der,
        }

    def ingest_payload(self, body: Dict[str, Any]) -> Dict[str, Any]:
        counts = {"scores_merged": 0, "tradelines_merged": 0, "inquiries_merged": 0, "derogatories_merged": 0}
        bureau_hint = str(body.get("bureau_hint") or body.get("bureau") or "auto")

        # 1. Structured fields sent directly by Companion Extension DOM extractor
        if any(body.get(k) for k in ("reports", "tradelines", "inquiries", "derogatories")):
            structured = self.importer.import_structured(body, needs_review=False).to_dict()
            c1 = self.merge_bundle_dict(structured)
            for k in counts:
                counts[k] += c1[k]

        # 2. Raw rendered page text fallback (Credit Karma / Experian page innerText)
        raw_text = str(body.get("page_text") or body.get("text") or "").strip()
        if raw_text:
            parsed = self.importer.import_portal_text(
                raw_text,
                bureau_hint=bureau_hint,
                source_tag=str(body.get("source") or "credit_companion_dom"),
                needs_review=False,
            ).to_dict()
            c2 = self.merge_bundle_dict(parsed)
            for k in counts:
                counts[k] += c2[k]

        return {"ok": True, "merged": counts, "snapshot": self.load_state()}

    def scan_drop_folder(self) -> int:
        processed_files = 0
        for item in sorted(self.drop_dir.iterdir()):
            if not item.is_file() or item.name.startswith("."):
                continue
            ext = item.suffix.lower()
            if ext not in (".pdf", ".txt", ".html", ".htm", ".json"):
                continue
            try:
                raw = item.read_bytes()
                if ext == ".pdf" or raw[:5] == b"%PDF-":
                    bundle = self.importer.import_pdf(raw).to_dict()
                elif ext == ".json":
                    bundle = self.importer.import_structured(raw.decode("utf-8-sig", errors="replace")).to_dict()
                else:
                    bundle = self.importer.import_portal_text(
                        raw.decode("utf-8-sig", errors="replace"),
                        needs_review=False,
                    ).to_dict()
                self.merge_bundle_dict(bundle)
                dest = self.processed_dir / item.name
                shutil.move(str(item), str(dest))
                processed_files += 1
            except Exception as exc:
                print(f"[credit-bridge] Failed to parse {item.name}: {exc}", file=sys.stderr)
        return processed_files


def make_handler(store: CreditBridgeStore, expected_token: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write(f"[credit-bridge] {fmt % args}\n")

        def _send_json(self, code: int, payload: Dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # Allow user-clicked Browser Companion Extension (`chrome-extension://` / `moz-extension://`)
            origin = self.headers.get("Origin") or ""
            if origin.startswith(("chrome-extension://", "moz-extension://")):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()
            self.wfile.write(body)

        def _check_bearer(self) -> bool:
            auth = (self.headers.get("Authorization") or "").strip()
            if not auth.lower().startswith("bearer "):
                return False
            provided = auth[7:].strip()
            return bool(provided) and hmac.compare_digest(provided, expected_token)

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._send_json(200, {"ok": True})

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/health":
                state = store.load_state()
                self._send_json(
                    200,
                    {
                        "status": "ok",
                        "drop_folder": str(store.drop_dir),
                        "scores": len(state.get("reports", [])),
                        "tradelines": len(state.get("tradelines", [])),
                        "inquiries": len(state.get("inquiries", [])),
                        "derogatories": len(state.get("derogatories", [])),
                    },
                )
                return

            if path == "/credit/snapshot":
                if not self._check_bearer():
                    self._send_json(401, {"error": "Invalid or missing Bearer token"})
                    return
                store.scan_drop_folder()
                self._send_json(200, store.load_state())
                return

            self._send_json(404, {"error": "Not found"})

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path != "/credit/ingest":
                self._send_json(404, {"error": "Not found"})
                return
            if not self._check_bearer():
                self._send_json(401, {"error": "Invalid or missing Bearer token"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY_BYTES:
                self._send_json(400, {"error": "Invalid payload length"})
                return
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._send_json(400, {"error": f"Invalid JSON: {exc}"})
                return
            if not isinstance(body, dict):
                self._send_json(400, {"error": "JSON object required"})
                return
            res = store.ingest_payload(body)
            self._send_json(200, res)

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description="ChronoX Local Loopback Credit Bridge")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Bind host (must be loopback)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Bind port (default 8749)")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR, help="ChronoX data directory")
    parser.add_argument("--print-token", action="store_true", help="Print Bearer token and exit")
    args = parser.parse_args()

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("Refusing to bind Credit Bridge outside loopback.", file=sys.stderr)
        return 2

    token_path = args.data_dir / "credit_bridge_token"
    token = load_or_create_bridge_token(token_path)
    if args.print_token:
        print(token)
        return 0

    store = CreditBridgeStore(args.data_dir)
    store.scan_drop_folder()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(store, token))
    print(f"ChronoX Credit Bridge listening on http://{args.host}:{args.port}/credit/snapshot")
    print(f"Drop folder: {store.drop_dir}")
    print(f"Token file:  {token_path} (chmod 0600)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
