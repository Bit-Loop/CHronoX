"""
Universal Multi-Format Financial Data Ingestion & Sanitized Export Engine.

Supports:
1. Bank CSV / TSV (Chase, BofA, Capital One, Amex, Wells Fargo, Citi, Discover,
   Apple Card, PayPal, Venmo, Mint, YNAB, Monarch, Copilot, Generic):
   - Auto-skips bank summary preamble lines above the real table header
   - Auto-detects delimiter (`,`, `;`, `\t`)
   - Supports signed Amount, split Debit/Credit, OR unsigned Amount + Type column
   - Supports US & European decimals (`1.234,56`), accounting parens `(42.50)`
2. OFX / QFX (Open Financial Exchange SGML v1 & XML v2):
   - Parses `<STMTTRN>` blocks, `<FITID>` bank IDs, and `<LEDGERBAL>` balance!
3. QIF (Quicken Interchange Format):
   - Parses `!Type:Bank`, `!Type:CCard`, `D/T/P/M/^` records.
4. JSON Statements (Plaid / SimpleFIN / generic arrays).
5. Internal transfer & credit-card payment classification to prevent double-counting.
6. Formula-injection-safe CSV export (`sanitize_csv_cell`).
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from chronox_finance.security import sanitize_csv_cell

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%Y/%m/%d",
    "%d-%b-%Y",
    "%b %d, %Y",
    "%m-%d-%Y",
    "%d/%m/%Y",
    "%Y%m%d",
)
_DATE_COLS = (
    "date", "transaction date", "posting date", "post date",
    "trans. date", "trans date", "clearing date", "datetime",
)
_DESC_COLS = (
    "description", "merchant", "payee", "name", "original description",
    "merchant name", "counterparty", "details", "title",
)
_AMT_COLS = ("amount", "transaction amount", "amount (usd)", "value", "total")
_DEBIT_COLS = ("debit", "outflow", "withdrawal", "charge", "money out")
_CREDIT_COLS = ("credit", "inflow", "deposit", "money in")
_TYPE_COLS = ("type", "transaction type", "dr/cr", "direction")
_MEMO_COLS = ("memo", "category", "notes", "note", "extended details")
_ID_COLS = ("fitid", "transaction id", "id", "reference", "reference number")

_TRANSFER_RE = re.compile(
    r"\b(credit\s*crd\s*(?:autopay|epay|pmt|payment)|payment\s+thank\s+you|"
    r"online\s+transfer\s+(?:to|from)|internal\s+transfer|transfer\s+(?:to|from)\s+(?:checking|savings)|"
    r"autopay\s+payment\s+received|chase\s+card\s+serv|amex\s+epayment|citi\s+autopay)\b",
    re.IGNORECASE,
)


@dataclass
class ImportedRow:
    tx_date: str
    merchant: str
    amount_cents: int
    tx_type: str  # "expense", "income", or "transfer"
    memo: str = ""
    external_id: str = ""


@dataclass
class IngestionResult:
    format_detected: str  # "csv", "ofx", "qif", "json"
    rows: List[ImportedRow] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    detected_balance_cents: Optional[int] = None
    transfers_detected: int = 0


def _parse_date(raw: str) -> Optional[str]:
    text = (raw or "").strip()
    if not text:
        return None
    # OFX timestamps: YYYYMMDDHHMMSS[.XXX:TZ]
    ofx_m = re.match(r"^(\d{8})", text)
    if ofx_m and len(text) >= 8 and text[:8].isdigit():
        text = ofx_m.group(1)
    # QIF apostrophe year format: 11/15'26 or 11/15'2026
    if "'" in text:
        text = text.replace("'", "/").replace(" ", "")
    # ISO timestamps: 2026-10-01T12:30:00Z
    if len(text) >= 10 and re.match(r"^\d{4}-\d{2}-\d{2}", text):
        text = text[:10]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _parse_money_to_cents(raw: str) -> Optional[int]:
    text = (raw or "").strip()
    if not text:
        return None
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1].strip()
    text = re.sub(r"[$€£¥\s]", "", text)
    if not text or text in ("-", "--", "N/A", "null"):
        return None

    # Handle European format "1.234,56" vs US "1,234.56"
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        parts = text.split(",")
        if len(parts) == 2 and len(parts[1]) in (1, 2):
            text = text.replace(",", ".")
        else:
            text = text.replace(",", "")

    try:
        dec = Decimal(text)
    except InvalidOperation:
        return None
    cents = int((dec * 100).quantize(Decimal("1")))
    return -abs(cents) if negative else cents


def _classify_transfer(merchant: str, memo: str, default_type: str, detect_transfers: bool) -> str:
    if detect_transfers and _TRANSFER_RE.search(f"{merchant} {memo}"):
        return "transfer"
    return default_type


def _find_col(headers_lower: Dict[str, str], candidates: Tuple[str, ...]) -> Optional[str]:
    for cand in candidates:
        if cand in headers_lower:
            return headers_lower[cand]
    for cand in candidates:
        for h_low, h_orig in headers_lower.items():
            if cand in h_low:
                return h_orig
    return None


def _detect_csv_delimiter(sample: str) -> str:
    """Detect delimiter cleanly even when bank preamble summary rows precede the table."""
    lines = [line for line in sample.splitlines()[:25] if line.strip()]
    best_delim = ","
    best_score = -1
    for delim in (",", ";", "\t"):
        counts = [line.count(delim) for line in lines if line.count(delim) >= 2]
        if not counts:
            continue
        # Score by number of consistent multi-column rows * median columns
        mode_count = max(set(counts), key=counts.count)
        consistent_rows = sum(1 for c in counts if c == mode_count)
        score = consistent_rows * 10 + mode_count
        if score > best_score:
            best_score = score
            best_delim = delim
    return best_delim


def _find_csv_header_line(lines: List[str], delim: str) -> int:
    """Skip multi-line bank preamble headers (BofA / Wells Fargo / USAA) to find the real header."""
    for idx, line in enumerate(lines[:20]):
        cols = [c.strip().strip('"').lower() for c in line.split(delim)]
        has_date = any(any(dc in c for dc in ("date",)) for c in cols)
        has_desc = any(any(k in c for k in ("desc", "merchant", "payee", "name", "details")) for c in cols)
        has_amt = any(any(k in c for k in ("amount", "debit", "credit", "outflow", "inflow", "value")) for c in cols)
        if has_date and (has_desc or has_amt):
            return idx
    return 0


def parse_bank_csv(
    csv_text: str,
    flip_expense_sign: bool = False,
    detect_transfers: bool = True,
) -> Tuple[List[ImportedRow], List[str]]:
    """Parse bank CSV/TSV content into normalized `ImportedRow` items."""
    if not csv_text or not csv_text.strip():
        return [], ["CSV content is empty"]

    cleaned = csv_text.lstrip("\ufeff")
    lines = cleaned.splitlines()
    delim = _detect_csv_delimiter("\n".join(lines[:15]))
    header_idx = _find_csv_header_line(lines, delim)
    table_text = "\n".join(lines[header_idx:])

    reader = csv.DictReader(io.StringIO(table_text), delimiter=delim)
    if not reader.fieldnames:
        return [], ["CSV file is missing a header row"]

    headers_lower = {h.strip().lower(): h for h in reader.fieldnames if h}
    date_col = _find_col(headers_lower, _DATE_COLS)
    desc_col = _find_col(headers_lower, _DESC_COLS)
    amt_col = _find_col(headers_lower, _AMT_COLS)
    debit_col = _find_col(headers_lower, _DEBIT_COLS)
    credit_col = _find_col(headers_lower, _CREDIT_COLS)
    type_col = _find_col(headers_lower, _TYPE_COLS)
    memo_col = _find_col(headers_lower, _MEMO_COLS)
    id_col = _find_col(headers_lower, _ID_COLS)

    if not date_col or not desc_col or (not amt_col and not (debit_col or credit_col)):
        return [], [
            "Could not detect required CSV columns (Date, Description/Merchant, and Amount or Debit/Credit)."
        ]

    rows: List[ImportedRow] = []
    warnings: List[str] = []

    for idx, raw_row in enumerate(reader, start=header_idx + 2):
        tx_date = _parse_date(raw_row.get(date_col, ""))
        merchant = (raw_row.get(desc_col) or "").strip()
        if not tx_date or not merchant:
            warnings.append(f"Row {idx}: skipped (missing date or merchant)")
            continue

        tx_type = "expense"
        amount_cents = 0

        if amt_col:
            signed = _parse_money_to_cents(raw_row.get(amt_col, ""))
            if signed is None or signed == 0:
                warnings.append(f"Row {idx}: skipped (invalid or zero amount)")
                continue
            # Check explicit Type column when amount is unsigned/positive
            type_hint = (raw_row.get(type_col) or "").strip().lower() if type_col else ""
            if flip_expense_sign:
                signed = -signed
            if type_hint in ("debit", "dr", "sale", "purchase", "withdrawal", "fee", "charge") and signed > 0:
                signed = -signed
            elif type_hint in ("credit", "cr", "deposit", "refund", "income", "dividend") and signed < 0:
                signed = abs(signed)

            tx_type = "income" if signed > 0 else "expense"
            amount_cents = abs(signed)
        else:
            debit = _parse_money_to_cents(raw_row.get(debit_col, "")) if debit_col else None
            credit = _parse_money_to_cents(raw_row.get(credit_col, "")) if credit_col else None
            if credit and abs(credit) > 0:
                tx_type = "income"
                amount_cents = abs(credit)
            elif debit and abs(debit) > 0:
                tx_type = "expense"
                amount_cents = abs(debit)
            else:
                warnings.append(f"Row {idx}: skipped (no debit or credit amount)")
                continue

        memo = (raw_row.get(memo_col) or "").strip() if (memo_col and memo_col != desc_col) else ""
        ext_id = (raw_row.get(id_col) or "").strip() if id_col else ""
        final_type = _classify_transfer(merchant, memo, tx_type, detect_transfers)

        rows.append(
            ImportedRow(
                tx_date=tx_date,
                merchant=merchant[:120],
                amount_cents=amount_cents,
                tx_type=final_type,
                memo=memo[:240],
                external_id=ext_id[:80],
            )
        )

    return rows, warnings


def _extract_ofx_tag(block: str, tag: str) -> str:
    # Works for both SGML (<TAG>value\n) and XML (<TAG>value</TAG>)
    m = re.search(rf"<{tag}>\s*([^<\r\n]+)", block, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def parse_ofx_statement(text: str, detect_transfers: bool = True) -> IngestionResult:
    """Parse OFX / QFX bank & credit card statements (SGML v1 & XML v2)."""
    blocks = re.findall(r"<STMTTRN>([\s\S]*?)(?:</STMTTRN>|(?=<STMTTRN>)|(?=</BANKTRANLIST>))", text, re.IGNORECASE)
    rows: List[ImportedRow] = []
    warnings: List[str] = []
    transfers = 0

    for idx, blk in enumerate(blocks, start=1):
        dt_str = _parse_date(_extract_ofx_tag(blk, "DTPOSTED") or _extract_ofx_tag(blk, "DTUSER"))
        amt = _parse_money_to_cents(_extract_ofx_tag(blk, "TRNAMT"))
        name = _extract_ofx_tag(blk, "NAME") or _extract_ofx_tag(blk, "MEMO")
        memo = _extract_ofx_tag(blk, "MEMO")
        fitid = _extract_ofx_tag(blk, "FITID")
        trntype = _extract_ofx_tag(blk, "TRNTYPE").upper()

        if not dt_str or amt is None or amt == 0 or not name:
            warnings.append(f"OFX entry {idx}: skipped incomplete record")
            continue

        base_type = "income" if amt > 0 else "expense"
        if trntype == "XFER":
            final_type = "transfer" if detect_transfers else base_type
        else:
            final_type = _classify_transfer(name, memo, base_type, detect_transfers)
        if final_type == "transfer":
            transfers += 1

        rows.append(
            ImportedRow(
                tx_date=dt_str,
                merchant=name[:120],
                amount_cents=abs(amt),
                tx_type=final_type,
                memo=(memo if memo != name else "")[:240],
                external_id=fitid[:80],
            )
        )

    bal_block = re.search(r"<LEDGERBAL>([\s\S]*?)(?:</LEDGERBAL>|(?=<AVAILBAL>)|(?=</STMTRS>)|(?=</CCSTMTRS>))", text, re.IGNORECASE)
    detected_bal = _parse_money_to_cents(_extract_ofx_tag(bal_block.group(1), "BALAMT")) if bal_block else None

    return IngestionResult(
        format_detected="ofx",
        rows=rows,
        warnings=warnings,
        detected_balance_cents=detected_bal,
        transfers_detected=transfers,
    )


def parse_qif_statement(text: str, detect_transfers: bool = True) -> IngestionResult:
    """Parse Quicken Interchange Format (.qif) files."""
    rows: List[ImportedRow] = []
    warnings: List[str] = []
    transfers = 0
    rec: Dict[str, str] = {}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("!"):
            continue
        if line == "^":
            dt_str = _parse_date(rec.get("D", ""))
            amt = _parse_money_to_cents(rec.get("T", "") or rec.get("U", ""))
            payee = rec.get("P", "") or rec.get("M", "")
            memo = rec.get("M", "")
            if dt_str and amt is not None and amt != 0 and payee:
                base_type = "income" if amt > 0 else "expense"
                final_type = _classify_transfer(payee, memo, base_type, detect_transfers)
                if final_type == "transfer":
                    transfers += 1
                rows.append(
                    ImportedRow(
                        tx_date=dt_str,
                        merchant=payee[:120],
                        amount_cents=abs(amt),
                        tx_type=final_type,
                        memo=(memo if memo != payee else "")[:240],
                    )
                )
            elif rec:
                warnings.append("QIF record skipped (missing date, amount, or payee)")
            rec = {}
            continue
        code, val = line[0].upper(), line[1:].strip()
        rec[code] = val

    return IngestionResult(format_detected="qif", rows=rows, warnings=warnings, transfers_detected=transfers)


def parse_json_statement(text: str, flip_expense_sign: bool = False, detect_transfers: bool = True) -> IngestionResult:
    """Parse structured JSON statement arrays (Plaid / SimpleFIN / generic)."""
    data = json.loads(text)
    items = data.get("transactions", []) if isinstance(data, dict) else data
    if not isinstance(items, list):
        return IngestionResult(format_detected="json", warnings=["JSON must be an array or object with 'transactions' list"])

    rows: List[ImportedRow] = []
    warnings: List[str] = []
    transfers = 0

    for idx, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        dt_str = _parse_date(str(item.get("date") or item.get("tx_date") or item.get("posted") or ""))
        merchant = str(item.get("merchant") or item.get("name") or item.get("description") or "").strip()
        raw_amt = item.get("amount_cents")
        if raw_amt is not None and isinstance(raw_amt, int):
            signed = raw_amt
        else:
            signed = _parse_money_to_cents(str(item.get("amount", "")))
        if not dt_str or not merchant or signed is None or signed == 0:
            warnings.append(f"JSON item {idx}: skipped (missing date, merchant, or amount)")
            continue
        if flip_expense_sign:
            signed = -signed
        base_type = str(item.get("tx_type") or ("income" if signed > 0 else "expense")).lower()
        if base_type not in ("income", "expense", "transfer"):
            base_type = "income" if signed > 0 else "expense"
        memo = str(item.get("memo") or "").strip()
        final_type = _classify_transfer(merchant, memo, base_type, detect_transfers) if base_type != "transfer" else "transfer"
        if final_type == "transfer":
            transfers += 1
        rows.append(
            ImportedRow(
                tx_date=dt_str,
                merchant=merchant[:120],
                amount_cents=abs(signed),
                tx_type=final_type,
                memo=memo[:240],
                external_id=str(item.get("transaction_id") or item.get("id") or "")[:80],
            )
        )

    return IngestionResult(format_detected="json", rows=rows, warnings=warnings, transfers_detected=transfers)


def ingest_statement(
    content: str,
    *,
    filename: str = "",
    flip_expense_sign: bool = False,
    detect_transfers: bool = True,
) -> IngestionResult:
    """Auto-detect statement format (OFX/QFX, QIF, JSON, or CSV/TSV) and parse cleanly."""
    cleaned = (content or "").lstrip("\ufeff").strip()
    if not cleaned:
        return IngestionResult(format_detected="unknown", warnings=["Uploaded statement is empty"])

    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    upper_head = cleaned[:400].upper()

    if ext in ("ofx", "qfx") or "<OFX>" in upper_head or "OFXHEADER:" in upper_head or "<STMTTRN>" in upper_head:
        return parse_ofx_statement(cleaned, detect_transfers=detect_transfers)

    if ext == "qif" or cleaned.startswith("!Type:") or cleaned.startswith("!type:"):
        return parse_qif_statement(cleaned, detect_transfers=detect_transfers)

    if ext == "json" or cleaned[0] in ("{", "["):
        try:
            return parse_json_statement(cleaned, flip_expense_sign=flip_expense_sign, detect_transfers=detect_transfers)
        except json.JSONDecodeError:
            pass

    rows, warnings = parse_bank_csv(cleaned, flip_expense_sign=flip_expense_sign, detect_transfers=detect_transfers)
    transfers = sum(1 for r in rows if r.tx_type == "transfer")
    return IngestionResult(format_detected="csv", rows=rows, warnings=warnings, transfers_detected=transfers)


def export_transactions_csv(transactions: List[Dict[str, Any]]) -> str:
    """Export transactions to RFC-4180 CSV with spreadsheet formula injection protection."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Date", "Type", "Merchant", "Jar", "Subcategory", "Amount", "Memo", "Source"])
    for tx in transactions:
        amt = int(tx["amount_cents"]) / 100.0
        if tx["tx_type"] == "income":
            signed_str = f"{amt:.2f}"
        elif tx["tx_type"] == "transfer":
            signed_str = "0.00" if amt == 0 else f"{amt:.2f}"
        else:
            signed_str = f"-{amt:.2f}"
        writer.writerow(
            [
                sanitize_csv_cell(tx["tx_date"]),
                sanitize_csv_cell(tx["tx_type"]),
                sanitize_csv_cell(tx["merchant"]),
                sanitize_csv_cell(tx.get("jar_name", "")),
                sanitize_csv_cell(tx.get("subcategory", "")),
                signed_str,
                sanitize_csv_cell(tx.get("memo", "")),
                sanitize_csv_cell(tx.get("source", "")),
            ]
        )
    return buf.getvalue()
