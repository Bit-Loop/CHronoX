"""
Universal Multi-Format Financial Data Ingestion & Sanitized Export Engine.

Supports:
1. Bank CSV / TSV (Navy Federal Credit Union, Chase, BofA, Capital One, Amex,
   Wells Fargo, Citi, Discover, Apple Card, PayPal, Venmo, Mint, YNAB, Monarch,
   Copilot, Plaid CSV, Generic):
   - Auto-skips bank summary preamble lines above the real table header
   - Auto-detects delimiter (`,`, `;`, `\t`)
   - Supports signed Amount, split Debit/Credit, OR unsigned Amount + Type /
     Navy Federal `Credit Debit Indicator` (`Credit`/`Debit`/`CR`/`DR`/`C`/`D`)
   - Supports US & European decimals (`1.234,56`), accounting parens `(42.50)`,
     and trailing minus signs (`45.00-`)
   - Optional Pending-status filtering & running `Balance` extraction
2. Navy Federal Credit Union Local PDF Statements (100% zero 3rd-party):
   - Pure-Python (or optional `pypdf`) local extraction of combined Share
     Savings / Checking / Credit Card monthly PDF statements
3. OFX / QFX (Open Financial Exchange SGML v1 & XML v2):
   - Parses `<STMTTRN>` blocks, `<FITID>` bank IDs, and `<LEDGERBAL>` balance
4. QIF (Quicken Interchange Format):
   - Parses `!Type:Bank`, `!Type:CCard`, `D/T/P/M/^` records
5. JSON Statements (Official Plaid `/transactions/get` & `/transactions/sync`,
   SimpleFIN, and generic ChronoX JSON arrays) with Plaid sign normalization
   and `accounts[].balances` extraction.
6. Internal transfer & credit-card payment classification (including Navy
   Federal Share Savings / Checking / cashRewards / More Rewards / Flagship /
   Platinum transfers) to prevent double-counting.
7. Formula-injection-safe CSV export (`sanitize_csv_cell`).
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import zlib
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple, Union

from chronox_finance.security import sanitize_csv_cell

logger = logging.getLogger(__name__)

VALID_INSTITUTIONS = ("auto", "generic", "navy_federal", "plaid")

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
    "posting date", "post date", "date", "transaction date",
    "trans. date", "trans date", "clearing date", "datetime",
)
_DESC_COLS = (
    "description", "merchant", "merchant name", "payee", "name",
    "original description", "counterparty", "details", "title",
)
_AMT_COLS = ("amount", "transaction amount", "amount (usd)", "value", "total")
_DEBIT_COLS = ("debit", "outflow", "withdrawal", "charge", "money out")
_CREDIT_COLS = ("credit", "inflow", "deposit", "money in")
_TYPE_COLS = (
    "credit debit indicator", "debit/credit indicator", "credit/debit indicator",
    "cr/dr", "dr/cr", "indicator", "transaction type", "type", "direction",
)
_STATUS_COLS = ("status", "transaction status", "posting status", "state")
_BALANCE_COLS = ("balance", "running balance", "available balance", "ending balance")
_MEMO_COLS = ("memo", "category", "grouping", "notes", "note", "extended details")
_ID_COLS = ("fitid", "transaction id", "transaction_id", "id", "reference", "reference number")

_TRANSFER_RE = re.compile(
    r"\b(credit\s*crd\s*(?:autopay|epay|pmt|payment)|payment\s+thank\s+you|"
    r"online\s+transfer\s+(?:to|from)|internal\s+transfer|transfer\s+(?:to|from)\s+(?:checking|savings)|"
    r"transfer\s+(?:to|from)\s+(?:share\s+savings|everyday\s+checking|active\s+duty\s+checking|"
    r"flagship\s+checking|campus\s+checking|free\s+easy\s+checking|money\s+market)|"
    r"share[\s-]+to[\s-]+share|share\s+transfer|member\s+transfer|nfcu\s+transfer|"
    r"(?:navy\s+federal|nfcu)\s+(?:credit\s+card|card|loan)\s+(?:payment|pmt|autopay)|"
    r"(?:cashrewards|more\s+rewards|go\s+rewards|nfcu\s+platinum|flagship\s+rewards)\s+(?:payment|pmt|autopay)|"
    r"autopay\s+payment\s+received|chase\s+card\s+serv|amex\s+epayment|citi\s+autopay)\b",
    re.IGNORECASE,
)

_NFCU_DETECT_RE = re.compile(
    r"\b(navy\s+federal|nfcu|share\s+savings|everyday\s+checking|active\s+duty\s+checking|"
    r"flagship\s+checking|cashrewards|more\s+rewards|go\s+rewards)\b",
    re.IGNORECASE,
)

_PENDING_STATUS_VALUES = {"pending", "processing", "authorized", "unposted", "hold"}
_DEBIT_INDICATOR_VALUES = {"debit", "dr", "d", "sale", "purchase", "withdrawal", "fee", "charge", "outflow"}
_CREDIT_INDICATOR_VALUES = {"credit", "cr", "c", "deposit", "refund", "income", "dividend", "inflow"}


@dataclass
class ImportedRow:
    tx_date: str
    merchant: str
    amount_cents: int
    tx_type: str  # "expense", "income", or "transfer"
    memo: str = ""
    external_id: str = ""
    transfer_direction: str = ""  # "out" | "in" | ""
    account_hint: str = ""        # Optional section/account name or external account_id


@dataclass
class IngestionResult:
    format_detected: str  # "csv", "ofx", "qif", "json", "pdf"
    rows: List[ImportedRow] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    detected_balance_cents: Optional[int] = None
    transfers_detected: int = 0
    detected_institution: str = "generic"  # "generic", "navy_federal", "plaid"
    pending_skipped: int = 0
    detected_account_hint: str = ""
    detected_account_type: str = ""  # "checking", "savings", "credit", or ""
    parse_failed: bool = False


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
    # Handle trailing minus sign (e.g. Navy Federal statement exports: "45.00-")
    if text.endswith("-") and not text.startswith("-"):
        negative = True
        text = text[:-1].strip()
    elif text.endswith("+") and not text.startswith("+"):
        text = text[:-1].strip()
    if not text or text in ("-", "--"):
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


def _normalize_institution(institution: str) -> str:
    val = (institution or "auto").strip().lower().replace("-", "_").replace(" ", "_")
    if val in ("nfcu", "navyfed", "navy_fed", "navy_federal_credit_union"):
        return "navy_federal"
    return val if val in VALID_INSTITUTIONS else "auto"


def _detect_csv_institution(
    headers_lower: Dict[str, str],
    filename: str,
    sample_text: str,
    requested: str,
) -> str:
    req = _normalize_institution(requested)
    if req != "auto":
        return req
    fn = (filename or "").lower()
    if any(tok in fn for tok in ("nfcu", "navyfed", "navy_fed", "navy-fed", "navyfederal")):
        return "navy_federal"
    if "plaid" in fn:
        return "plaid"
    if "credit debit indicator" in headers_lower or "debit/credit indicator" in headers_lower:
        return "navy_federal"
    if "personal_finance_category" in headers_lower or "transaction_id" in headers_lower:
        return "plaid"
    if _NFCU_DETECT_RE.search(sample_text[:4000]):
        return "navy_federal"
    return "generic"


def _parse_bank_csv_detailed(
    csv_text: str,
    *,
    filename: str = "",
    flip_expense_sign: bool = False,
    detect_transfers: bool = True,
    institution: str = "auto",
    skip_pending: bool = True,
) -> IngestionResult:
    """Parse bank CSV/TSV content with optional Navy Federal & Plaid intelligence."""
    if not csv_text or not csv_text.strip():
        return IngestionResult(format_detected="csv", warnings=["CSV content is empty"])

    cleaned = csv_text.lstrip("\ufeff")
    lines = cleaned.splitlines()
    delim = _detect_csv_delimiter("\n".join(lines[:15]))
    header_idx = _find_csv_header_line(lines, delim)
    table_text = "\n".join(lines[header_idx:])

    reader = csv.DictReader(io.StringIO(table_text), delimiter=delim)
    if not reader.fieldnames:
        return IngestionResult(format_detected="csv", warnings=["CSV file is missing a header row"])

    headers_lower = {h.strip().lower(): h for h in reader.fieldnames if h}
    detected_inst = _detect_csv_institution(headers_lower, filename, cleaned, institution)

    date_col = _find_col(headers_lower, _DATE_COLS)
    desc_col = _find_col(headers_lower, _DESC_COLS)
    amt_col = _find_col(headers_lower, _AMT_COLS)
    debit_col = _find_col(headers_lower, _DEBIT_COLS)
    credit_col = _find_col(headers_lower, _CREDIT_COLS)
    type_col = _find_col(headers_lower, _TYPE_COLS)
    status_col = _find_col(headers_lower, _STATUS_COLS)
    balance_col = _find_col(headers_lower, _BALANCE_COLS)
    memo_col = _find_col(headers_lower, _MEMO_COLS)
    id_col = _find_col(headers_lower, _ID_COLS)

    if not date_col or not desc_col or (not amt_col and not (debit_col or credit_col)):
        return IngestionResult(
            format_detected="csv",
            detected_institution=detected_inst,
            warnings=[
                "Could not detect required CSV columns (Date, Description/Merchant, and Amount or Debit/Credit)."
            ],
        )

    rows: List[ImportedRow] = []
    warnings: List[str] = []
    pending_skipped = 0
    transfers = 0
    balance_samples: List[Tuple[str, int, int]] = []  # (tx_date, row_idx, balance_cents)

    for idx, raw_row in enumerate(reader, start=header_idx + 2):
        # Skip Pending / Authorized rows when skip_pending is enabled (NFCU & Plaid CSV)
        if status_col and skip_pending:
            status_val = (raw_row.get(status_col) or "").strip().lower()
            if status_val in _PENDING_STATUS_VALUES or status_val == "true":
                pending_skipped += 1
                continue

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
            type_hint = (raw_row.get(type_col) or "").strip().lower() if type_col else ""
            if flip_expense_sign:
                signed = -signed
            if type_hint in _DEBIT_INDICATOR_VALUES and signed > 0:
                signed = -signed
            elif type_hint in _CREDIT_INDICATOR_VALUES and signed < 0:
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

        if balance_col:
            bal_c = _parse_money_to_cents(raw_row.get(balance_col, ""))
            if bal_c is not None:
                balance_samples.append((tx_date, idx, bal_c))

        memo = (raw_row.get(memo_col) or "").strip() if (memo_col and memo_col != desc_col) else ""
        ext_id = (raw_row.get(id_col) or "").strip() if id_col else ""
        acct_col_val = ""
        for cand_acct_col in ("account", "account name", "account_name", "account_id", "account number"):
            if cand_acct_col in headers_lower:
                acct_col_val = (raw_row.get(headers_lower[cand_acct_col]) or "").strip()
                if acct_col_val:
                    break
        final_type = _classify_transfer(merchant, memo, tx_type, detect_transfers)
        xfer_dir = ""
        if final_type == "transfer":
            transfers += 1
            xfer_dir = "in" if tx_type == "income" else "out"

        rows.append(
            ImportedRow(
                tx_date=tx_date,
                merchant=merchant[:120],
                amount_cents=amount_cents,
                tx_type=final_type,
                memo=memo[:240],
                external_id=ext_id[:80],
                transfer_direction=xfer_dir,
                account_hint=acct_col_val[:80],
            )
        )

    detected_balance_cents: Optional[int] = None
    if balance_samples:
        max_date = max(s[0] for s in balance_samples)
        is_descending = balance_samples[0][0] > balance_samples[-1][0]
        on_max = [s for s in balance_samples if s[0] == max_date]
        detected_balance_cents = on_max[0][2] if is_descending else on_max[-1][2]

    if pending_skipped > 0:
        warnings.append(f"Skipped {pending_skipped} pending authorization(s) to prevent settlement duplicates.")

    preamble_text = "\n".join(lines[: max(1, header_idx)]).lower()
    detected_acct_type = ""
    detected_acct_hint = ""
    if "savings" in preamble_text or "hysa" in preamble_text:
        detected_acct_type = "savings"
        detected_acct_hint = "Share Savings" if detected_inst == "navy_federal" else "Savings"
    elif "credit card" in preamble_text or "cashrewards" in preamble_text:
        detected_acct_type = "credit"
    elif "checking" in preamble_text:
        detected_acct_type = "checking"

    return IngestionResult(
        format_detected="csv",
        rows=rows,
        warnings=warnings,
        detected_balance_cents=detected_balance_cents,
        transfers_detected=transfers,
        detected_institution=detected_inst,
        pending_skipped=pending_skipped,
        detected_account_hint=detected_acct_hint,
        detected_account_type=detected_acct_type,
        parse_failed=(len(rows) == 0 and pending_skipped == 0),
    )


def parse_bank_csv(
    csv_text: str,
    flip_expense_sign: bool = False,
    detect_transfers: bool = True,
    *,
    institution: str = "auto",
    skip_pending: bool = True,
) -> Tuple[List[ImportedRow], List[str]]:
    """Parse bank CSV/TSV content into normalized `ImportedRow` items."""
    res = _parse_bank_csv_detailed(
        csv_text,
        flip_expense_sign=flip_expense_sign,
        detect_transfers=detect_transfers,
        institution=institution,
        skip_pending=skip_pending,
    )
    return res.rows, res.warnings


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
        xfer_dir = ""
        if final_type == "transfer":
            transfers += 1
            xfer_dir = "in" if base_type == "income" else "out"

        rows.append(
            ImportedRow(
                tx_date=dt_str,
                merchant=name[:120],
                amount_cents=abs(amt),
                tx_type=final_type,
                memo=(memo if memo != name else "")[:240],
                external_id=fitid[:80],
                transfer_direction=xfer_dir,
            )
        )

    bal_block = re.search(r"<LEDGERBAL>([\s\S]*?)(?:</LEDGERBAL>|(?=<AVAILBAL>)|(?=</STMTRS>)|(?=</CCSTMTRS>))", text, re.IGNORECASE)
    detected_bal = _parse_money_to_cents(_extract_ofx_tag(bal_block.group(1), "BALAMT")) if bal_block else None
    ofx_acct_type_raw = _extract_ofx_tag(text, "ACCTTYPE").upper()
    ofx_acct_id = _extract_ofx_tag(text, "ACCTID")
    if "<CCSTMTRS>" in text.upper() or ofx_acct_type_raw == "CREDITLINE":
        ofx_acct_type = "credit"
    elif "SAV" in ofx_acct_type_raw:
        ofx_acct_type = "savings"
    elif "CHECK" in ofx_acct_type_raw:
        ofx_acct_type = "checking"
    else:
        ofx_acct_type = ""

    return IngestionResult(
        format_detected="ofx",
        rows=rows,
        warnings=warnings,
        detected_balance_cents=detected_bal,
        transfers_detected=transfers,
        detected_account_hint=ofx_acct_id[:80],
        detected_account_type=ofx_acct_type,
        parse_failed=(len(rows) == 0),
    )


def parse_qif_statement(text: str, detect_transfers: bool = True) -> IngestionResult:
    """Parse Quicken Interchange Format (.qif) files."""
    rows: List[ImportedRow] = []
    warnings: List[str] = []
    transfers = 0
    rec: Dict[str, str] = {}
    qif_acct_type = ""

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("!"):
            low_hdr = line.lower()
            if "ccard" in low_hdr:
                qif_acct_type = "credit"
            elif "oth a" in low_hdr or "sav" in low_hdr:
                qif_acct_type = "savings"
            elif "bank" in low_hdr:
                qif_acct_type = "checking"
            continue
        if line == "^":
            dt_str = _parse_date(rec.get("D", ""))
            amt = _parse_money_to_cents(rec.get("T", "") or rec.get("U", ""))
            payee = rec.get("P", "") or rec.get("M", "")
            memo = rec.get("M", "")
            if dt_str and amt is not None and amt != 0 and payee:
                base_type = "income" if amt > 0 else "expense"
                final_type = _classify_transfer(payee, memo, base_type, detect_transfers)
                xfer_dir = ""
                if final_type == "transfer":
                    transfers += 1
                    xfer_dir = "in" if base_type == "income" else "out"
                rows.append(
                    ImportedRow(
                        tx_date=dt_str,
                        merchant=payee[:120],
                        amount_cents=abs(amt),
                        tx_type=final_type,
                        memo=(memo if memo != payee else "")[:240],
                        transfer_direction=xfer_dir,
                    )
                )
            elif rec:
                warnings.append("QIF record skipped (missing date, amount, or payee)")
            rec = {}
            continue
        code, val = line[0].upper(), line[1:].strip()
        rec[code] = val

    return IngestionResult(
        format_detected="qif",
        rows=rows,
        warnings=warnings,
        transfers_detected=transfers,
        detected_account_type=qif_acct_type,
        parse_failed=(len(rows) == 0),
    )


_PLAID_ITEM_KEYS = {
    "transaction_id",
    "personal_finance_category",
    "iso_currency_code",
    "payment_channel",
    "pending_transaction_id",
    "authorized_date",
    "account_id",
}
_PLAID_TRANSFER_CATEGORIES = {"TRANSFER_IN", "TRANSFER_OUT", "LOAN_PAYMENTS"}


def _is_official_plaid_payload(data: Any, items: List[Any]) -> bool:
    if isinstance(data, dict):
        if "added" in data or "modified" in data or "removed" in data:
            return True
        accounts = data.get("accounts")
        if isinstance(accounts, list) and any(
            isinstance(a, dict) and ("account_id" in a or isinstance(a.get("balances"), dict))
            for a in accounts
        ):
            return True
    for item in items[:5]:
        if isinstance(item, dict) and (_PLAID_ITEM_KEYS & set(item.keys())):
            return True
    return False


def parse_json_statement(
    text: str,
    flip_expense_sign: bool = False,
    detect_transfers: bool = True,
    *,
    institution: str = "auto",
    skip_pending: bool = True,
) -> IngestionResult:
    """Parse structured JSON statements (Official Plaid /transactions/get & /sync, SimpleFIN, generic)."""
    data = json.loads(text)
    if isinstance(data, dict):
        if isinstance(data.get("transactions"), list):
            items = list(data["transactions"])
        elif isinstance(data.get("added"), list) or isinstance(data.get("modified"), list):
            items = list(data.get("added") or []) + list(data.get("modified") or [])
        else:
            items = []
    elif isinstance(data, list):
        items = data
    else:
        return IngestionResult(
            format_detected="json",
            warnings=["JSON must be an array or object with 'transactions' (or Plaid 'added') list"],
        )

    req_inst = _normalize_institution(institution)
    is_plaid_api = _is_official_plaid_payload(data, items)
    if req_inst == "plaid":
        detected_inst = "plaid"
    elif is_plaid_api:
        detected_inst = "plaid"
    elif req_inst != "auto":
        detected_inst = req_inst
    else:
        detected_inst = "generic"

    # Extract current/available balance & account metadata from official Plaid `accounts` array when present
    detected_balance_cents: Optional[int] = None
    json_acct_names: Dict[str, str] = {}
    detected_acct_type = ""
    if isinstance(data, dict) and isinstance(data.get("accounts"), list):
        for acct in data["accounts"]:
            if not isinstance(acct, dict):
                continue
            aid = str(acct.get("account_id") or acct.get("id") or "").strip()
            aname = str(acct.get("name") or "").strip()
            asub = str(acct.get("subtype") or acct.get("type") or "").strip().lower()
            if aid and aname:
                json_acct_names[aid] = aname
            if not detected_acct_type:
                if "sav" in asub:
                    detected_acct_type = "savings"
                elif "credit" in asub:
                    detected_acct_type = "credit"
                elif "check" in asub:
                    detected_acct_type = "checking"
            bals = acct.get("balances")
            if isinstance(bals, dict) and detected_balance_cents is None:
                raw_bal = bals.get("current") if bals.get("current") is not None else bals.get("available")
                if raw_bal is not None:
                    bal_c = _parse_money_to_cents(str(raw_bal))
                    if bal_c is not None:
                        is_credit_or_loan = ("credit" in asub or "loan" in asub or str(acct.get("type") or "").strip().lower() in ("credit", "loan"))
                        if is_plaid_api and is_credit_or_loan and bal_c > 0:
                            bal_c = -bal_c
                        detected_balance_cents = bal_c

    rows: List[ImportedRow] = []
    warnings: List[str] = []
    transfers = 0
    pending_skipped = 0

    for idx, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        if skip_pending and item.get("pending") is True:
            pending_skipped += 1
            continue

        dt_str = _parse_date(
            str(item.get("date") or item.get("authorized_date") or item.get("tx_date") or item.get("posted") or "")
        )
        merchant = str(
            item.get("merchant_name") or item.get("merchant") or item.get("name") or item.get("description") or ""
        ).strip()
        raw_amt = item.get("amount_cents")
        if raw_amt is not None and isinstance(raw_amt, int):
            signed = raw_amt
        else:
            signed = _parse_money_to_cents(str(item.get("amount", "")))
        if not dt_str or not merchant or signed is None or signed == 0:
            warnings.append(f"JSON item {idx}: skipped (missing date, merchant, or amount)")
            continue

        # Official Plaid API convention: positive amount = outflow (expense), negative = inflow (income)
        item_is_plaid = is_plaid_api or bool(_PLAID_ITEM_KEYS & set(item.keys()))
        if item_is_plaid and "tx_type" not in item:
            signed = -signed
        if flip_expense_sign:
            signed = -signed

        pfc = item.get("personal_finance_category")
        pfc_primary = str(pfc.get("primary") or "").upper() if isinstance(pfc, dict) else ""
        pfc_detail = str(pfc.get("detailed") or "").upper() if isinstance(pfc, dict) else ""

        base_type = str(item.get("tx_type") or ("income" if signed > 0 else "expense")).lower()
        if base_type not in ("income", "expense", "transfer"):
            base_type = "income" if signed > 0 else "expense"

        memo = str(item.get("memo") or "").strip()
        if not memo and pfc_detail:
            memo = pfc_detail.replace("_", " ").title()

        if detect_transfers and (
            pfc_primary in _PLAID_TRANSFER_CATEGORIES or "CREDIT_CARD_PAYMENT" in pfc_detail
        ):
            final_type = "transfer"
        elif base_type == "transfer":
            final_type = "transfer"
        else:
            final_type = _classify_transfer(merchant, memo, base_type, detect_transfers)

        xfer_dir = ""
        if final_type == "transfer":
            transfers += 1
            if pfc_primary == "TRANSFER_IN":
                xfer_dir = "in"
            elif pfc_primary == "TRANSFER_OUT":
                xfer_dir = "out"
            else:
                xfer_dir = "in" if signed > 0 else "out"

        item_acct_id = str(item.get("account_id") or "").strip()
        row_acct_hint = json_acct_names.get(item_acct_id, item_acct_id)

        rows.append(
            ImportedRow(
                tx_date=dt_str,
                merchant=merchant[:120],
                amount_cents=abs(signed),
                tx_type=final_type,
                memo=memo[:240],
                external_id=str(item.get("transaction_id") or item.get("id") or "")[:80],
                transfer_direction=xfer_dir,
                account_hint=row_acct_hint[:80],
            )
        )

    if pending_skipped > 0:
        warnings.append(f"Skipped {pending_skipped} pending Plaid transaction(s).")

    return IngestionResult(
        format_detected="json",
        rows=rows,
        warnings=warnings,
        detected_balance_cents=detected_balance_cents,
        transfers_detected=transfers,
        detected_institution=detected_inst,
        pending_skipped=pending_skipped,
        detected_account_type=detected_acct_type,
        parse_failed=(len(rows) == 0 and pending_skipped == 0),
    )


def _unescape_pdf_literal(raw: bytes) -> str:
    """Decode a PDF literal string `(...)` handling backslash escapes & octal codes."""
    out = bytearray()
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == 92 and i + 1 < n:  # backslash
            nxt = raw[i + 1]
            escapes = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12}
            if nxt in escapes:
                out.append(escapes[nxt])
                i += 2
            elif nxt in (ord("("), ord(")"), ord("\\")):
                out.append(nxt)
                i += 2
            elif 48 <= nxt <= 55:  # octal
                oct_digits = raw[i + 1 : i + 4]
                m = re.match(rb"^[0-7]{1,3}", oct_digits)
                if m:
                    out.append(int(m.group(0), 8) & 0xFF)
                    i += 1 + len(m.group(0))
                else:
                    i += 2
            elif nxt in (10, 13):  # line continuation
                i += 2
            else:
                out.append(nxt)
                i += 2
        else:
            out.append(ch)
            i += 1
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        return out.decode("latin-1", errors="replace")


def _extract_pdf_text_pure_python(raw_bytes: bytes) -> str:
    """Zero-dependency local PDF text extractor for uncompressed & FlateDecode streams."""
    streams: List[bytes] = []
    for match in re.finditer(rb"stream\r?\n([\s\S]*?)\r?\nendstream", raw_bytes):
        chunk = match.group(1)
        if not chunk:
            continue
        # Try zlib FlateDecode first; fall back to raw stream bytes
        try:
            streams.append(zlib.decompress(chunk))
        except zlib.error:
            streams.append(chunk)

    lines: List[str] = []
    for stream in streams:
        if b"BT" not in stream and b"Tj" not in stream and b"TJ" not in stream:
            continue
        # Split into text blocks BT ... ET
        blocks = re.findall(rb"BT([\s\S]*?)ET", stream) or [stream]
        for blk in blocks:
            # Extract lines split by Td/TD/T*/Tm positioning operators
            parts: List[str] = []
            for tok in re.finditer(
                rb"\((?:\\.|[^\\()])*\)\s*Tj|\[([\s\S]*?)\]\s*TJ|\bT\*|\bTd\b|\bTD\b|\bTm\b",
                blk,
            ):
                full = tok.group(0)
                if full.endswith(b"Tj"):
                    inner = full[1 : full.rfind(b")")]
                    parts.append(_unescape_pdf_literal(inner))
                elif full.endswith(b"TJ"):
                    arr = tok.group(1) or b""
                    seg = "".join(
                        _unescape_pdf_literal(m.group(1))
                        for m in re.finditer(rb"\(((?:\\.|[^\\()])*)\)", arr)
                    )
                    if seg:
                        parts.append(seg)
                else:
                    if parts:
                        line = " ".join(p.strip() for p in parts if p.strip()).strip()
                        if line:
                            lines.append(line)
                        parts = []
            if parts:
                line = " ".join(p.strip() for p in parts if p.strip()).strip()
                if line:
                    lines.append(line)
    return "\n".join(lines)


def _extract_pdf_text(raw_bytes: bytes) -> str:
    """Extract text locally from PDF bytes (prefers optional pypdf, falls back to pure-Python)."""
    try:
        import pypdf  # type: ignore[import-not-found]

        reader = pypdf.PdfReader(io.BytesIO(raw_bytes))
        pages = [page.extract_text() or "" for page in reader.pages]
        combined = "\n".join(pages).strip()
        if combined:
            return combined
    except Exception as exc:
        logger.debug("Optional pypdf extraction unavailable/skipped (%s); using pure-Python PDF parser.", exc)
    return _extract_pdf_text_pure_python(raw_bytes)


_PDF_SECTION_RE = re.compile(
    r"\b(share\s+savings|everyday\s+checking|active\s+duty\s+checking|flagship\s+checking|"
    r"campus\s+checking|free\s+easy\s+checking|money\s+market|cashrewards|more\s+rewards|"
    r"go\s+rewards|platinum\s+credit\s+card|checking\s+account|savings\s+account)\b",
    re.IGNORECASE,
)
_PDF_YEAR_RE = re.compile(
    r"(?:statement\s+period|through|ending|statement\s+date)[^\r\n]{0,40}\b(20\d{2})\b"
    r"|\b\d{1,2}/\d{1,2}/(20\d{2})\b",
    re.IGNORECASE,
)
_PDF_TX_LINE_RE = re.compile(
    r"^(?P<dt>\d{1,2}/\d{1,2}(?:/\d{2,4})?)\s+"
    r"(?P<desc>.+?)\s+"
    r"(?P<amt>[\(\-\$+]?\$?\d[\d,]*\.\d{2}[\)\-]?)"
    r"(?:\s+(?P<bal>[\(\-\$+]?\$?\d[\d,]*\.\d{2}[\)\-]?))?$"
)
_PDF_ENDING_BAL_RE = re.compile(
    r"(?:ending\s+balance|new\s+balance|current\s+balance)\s*[:\-]?\s*([\(\-\$+]?\$?\d[\d,]*\.\d{2}[\)\-]?)",
    re.IGNORECASE,
)


def parse_navy_federal_pdf(
    raw_bytes: bytes,
    *,
    detect_transfers: bool = True,
    skip_pending: bool = True,
    institution: str = "auto",
) -> IngestionResult:
    """100% local parser for Navy Federal Credit Union combined monthly PDF statements."""
    text = _extract_pdf_text(raw_bytes)
    if not text.strip():
        return IngestionResult(
            format_detected="pdf",
            detected_institution="navy_federal" if institution == "navy_federal" else "generic",
            warnings=[
                "Could not extract text from PDF (scanned image PDFs need OCR; download a CSV from Navy Federal Online Banking for instant results)."
            ],
            parse_failed=True,
        )

    detected_inst = (
        "navy_federal"
        if _normalize_institution(institution) == "navy_federal" or _NFCU_DETECT_RE.search(text)
        else _normalize_institution(institution) if _normalize_institution(institution) != "auto" else "generic"
    )

    # Determine statement year (default current year if statement only prints MM/DD)
    year_match = _PDF_YEAR_RE.search(text)
    stmt_year = int(year_match.group(1) or year_match.group(2)) if year_match else date.today().year

    rows: List[ImportedRow] = []
    warnings: List[str] = []
    transfers = 0
    pending_skipped = 0
    current_section = ""
    detected_balance_cents: Optional[int] = None

    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue

        end_bal_m = _PDF_ENDING_BAL_RE.search(line)
        if end_bal_m:
            bal_c = _parse_money_to_cents(end_bal_m.group(1))
            if bal_c is not None:
                detected_balance_cents = bal_c
            continue

        if len(line) <= 72 and not re.match(r"^\d{1,2}/\d{1,2}", line):
            sec_m = _PDF_SECTION_RE.search(line)
            if sec_m:
                current_section = sec_m.group(1).strip().title()
                continue

        m = _PDF_TX_LINE_RE.match(line)
        if not m:
            continue

        raw_dt = m.group("dt")
        desc = m.group("desc").strip()
        amt_str = m.group("amt").strip()
        bal_str = (m.group("bal") or "").strip()

        if skip_pending and "pending" in desc.lower():
            pending_skipped += 1
            continue

        if re.match(r"^\d{1,2}/\d{1,2}$", raw_dt):
            tx_date = _parse_date(f"{raw_dt}/{stmt_year}")
        else:
            tx_date = _parse_date(raw_dt)

        signed = _parse_money_to_cents(amt_str)
        if not tx_date or not desc or signed is None or signed == 0:
            continue

        # Skip summary lines masquerading as transaction rows
        desc_low = desc.lower()
        if desc_low.startswith(("beginning balance", "ending balance", "previous balance", "total ")):
            if "ending balance" in desc_low:
                detected_balance_cents = signed
            continue

        if bal_str:
            bal_c = _parse_money_to_cents(bal_str)
            if bal_c is not None:
                detected_balance_cents = bal_c

        base_type = "income" if signed > 0 else "expense"
        memo = f"NFCU {current_section}".strip() if current_section else ""
        final_type = _classify_transfer(desc, memo, base_type, detect_transfers)
        xfer_dir = ""
        if final_type == "transfer":
            transfers += 1
            xfer_dir = "in" if base_type == "income" else "out"

        rows.append(
            ImportedRow(
                tx_date=tx_date,
                merchant=desc[:120],
                amount_cents=abs(signed),
                tx_type=final_type,
                memo=memo[:240],
                transfer_direction=xfer_dir,
                account_hint=current_section[:80],
            )
        )

    if not rows:
        warnings.append(
            "No transaction rows matched in this PDF. Tip: Navy Federal Activity CSV exports include exact columns and running balances."
        )

    sec_low = current_section.lower()
    pdf_acct_type = (
        "savings"
        if "saving" in sec_low or "money market" in sec_low
        else ("credit" if "reward" in sec_low or "credit" in sec_low else ("checking" if "checking" in sec_low else ""))
    )

    return IngestionResult(
        format_detected="pdf",
        rows=rows,
        warnings=warnings,
        detected_balance_cents=detected_balance_cents,
        transfers_detected=transfers,
        detected_institution=detected_inst,
        pending_skipped=pending_skipped,
        detected_account_hint=current_section[:80],
        detected_account_type=pdf_acct_type,
        parse_failed=(len(rows) == 0 and pending_skipped == 0),
    )


def ingest_statement(
    content: Union[str, bytes],
    *,
    filename: str = "",
    flip_expense_sign: bool = False,
    detect_transfers: bool = True,
    institution: str = "auto",
    skip_pending: bool = True,
) -> IngestionResult:
    """Auto-detect statement format (PDF, OFX/QFX, QIF, JSON/Plaid, or CSV/TSV) and parse cleanly."""
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""

    if isinstance(content, (bytes, bytearray)):
        raw_bytes = bytes(content)
        if not raw_bytes:
            return IngestionResult(format_detected="unknown", warnings=["Uploaded statement is empty"])
        if ext == "pdf" or raw_bytes[:5] == b"%PDF-":
            return parse_navy_federal_pdf(
                raw_bytes,
                detect_transfers=detect_transfers,
                skip_pending=skip_pending,
                institution=institution,
            )
        decoded = ""
        for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
            try:
                decoded = raw_bytes.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if not decoded:
            decoded = raw_bytes.decode("latin-1", errors="replace")
        content_str = decoded
    else:
        content_str = content or ""
        if ext == "pdf" or content_str.lstrip().startswith("%PDF-"):
            return parse_navy_federal_pdf(
                content_str.encode("latin-1", errors="ignore"),
                detect_transfers=detect_transfers,
                skip_pending=skip_pending,
                institution=institution,
            )

    cleaned = content_str.lstrip("\ufeff").strip()
    if not cleaned:
        return IngestionResult(format_detected="unknown", warnings=["Uploaded statement is empty"])

    upper_head = cleaned[:400].upper()

    if ext in ("ofx", "qfx") or "<OFX>" in upper_head or "OFXHEADER:" in upper_head or "<STMTTRN>" in upper_head:
        res = parse_ofx_statement(cleaned, detect_transfers=detect_transfers)
        if _normalize_institution(institution) != "auto":
            res.detected_institution = _normalize_institution(institution)
        elif _NFCU_DETECT_RE.search(cleaned[:2000]):
            res.detected_institution = "navy_federal"
        return res

    if ext == "qif" or cleaned.startswith("!Type:") or cleaned.startswith("!type:"):
        res = parse_qif_statement(cleaned, detect_transfers=detect_transfers)
        if _normalize_institution(institution) != "auto":
            res.detected_institution = _normalize_institution(institution)
        return res

    if ext == "json" or cleaned[0] in ("{", "["):
        try:
            return parse_json_statement(
                cleaned,
                flip_expense_sign=flip_expense_sign,
                detect_transfers=detect_transfers,
                institution=institution,
                skip_pending=skip_pending,
            )
        except json.JSONDecodeError:
            pass

    return _parse_bank_csv_detailed(
        cleaned,
        filename=filename,
        flip_expense_sign=flip_expense_sign,
        detect_transfers=detect_transfers,
        institution=institution,
        skip_pending=skip_pending,
    )


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
