"""
Comprehensive unit test suite for `chronox_finance.engines.exporter`.

Validates:
1. Rich populated ChronoX domain ledger export (Pydantic models -> ExportSnapshot -> CSV, CSV Pack ZIP, OOXML .xlsx).
2. Zero-data / $0 fresh profile safety across all 6 sheets and 5 native charts (zero empty/inverted ranges).
3. Adversarial strings (<script>&"', XML illegal control chars, =CMD|' /C calc'!A0, + / - / @ / \\t / \\r formula payloads,
   Unicode merchants, non-USD currencies, unknown cost basis).
4. Strict in-memory unzip & XML parsing of EVERY .xml and .rels file in the generated .xlsx package:
   - [Content_Types].xml overrides & defaults
   - Every relationship target (rId) resolves to an existing archive member
   - Valid sharedStrings & styles indices and mandatory number formats ($#,##0.00, 0.0%, yyyy-mm-dd)
   - Valid twoCellAnchor non-overlapping coordinates
   - Valid <c:barChart>, <c:doughnutChart>, <c:lineChart>, <c:pieChart> series <c:f> cell references & caches
   - Live spreadsheet formulas (SUM, IF, MAX) with precomputed cached values
5. Honesty rule & macOS-only `convert_to_native_numbers_if_macos` behavior.
6. Zero forbidden wording ("Simulator", "What-If", "Sandbox", fabricated score point deltas).
"""

from __future__ import annotations

import csv
import io
import posixpath
import re
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Set, Tuple

import pytest

import chronox_finance.engines.exporter as exporter_mod
from chronox_finance.engines.exporter import (
    CSV_PACK_FILENAMES,
    NUMBERS_MEDIA_TYPE,
    SHEET_CASHFLOW,
    SHEET_CREDIT,
    SHEET_EXECUTIVE,
    SHEET_GOALS_BILLS,
    SHEET_NAMES,
    SHEET_PORTFOLIO,
    SHEET_TRANSACTIONS,
    TRANSACTIONS_CSV_HEADERS,
    XLSX_MEDIA_TYPE,
    AccountExportRow,
    ExecutiveKPIs,
    ExportSnapshot,
    FinanceExporter,
    HoldingExportRow,
    TransactionExportRow,
    cents_to_decimal_str,
    col_index_to_letter,
    convert_to_native_numbers_if_macos,
    e8_to_decimal_str,
    iso_date_to_excel_serial,
    sanitize_csv_text,
)
from chronox_finance.models import (
    AccountOut,
    AccountType,
    ADHDStats,
    AnalyticsOverview,
    AutopayMode,
    AZEOAction,
    CreditFactorBreakdown,
    CreditFactorItem,
    CreditLineOut,
    CreditOptimizationReport,
    CreditReportOut,
    DailyCashflowPoint,
    DashboardResponse,
    DebtKind,
    DebtStrategyResult,
    GoalCategory,
    GoalOut,
    HoldingOut,
    JarHealth,
    JarKind,
    JarState,
    MonthlyTrendPoint,
    NetWorthPoint,
    PortfolioAllocationItem,
    PortfolioSummary,
    QuantForecastReport,
    RecurrenceCadence,
    RecurringBillOut,
    SafeToSpendBreakdown,
    SpendingVolatilityMetrics,
    TagOut,
    TopExpenseItem,
    TransactionOut,
    TransactionSplitOut,
    TransactionType,
)

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
NS_DOC_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
NS_XDR = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
NS_C = "http://schemas.openxmlformats.org/drawingml/2006/chart"

_CELL_REF_RE = re.compile(r"^([A-Z]+)(\d+)$")
_CHART_RANGE_RE = re.compile(r"^'([^']+)'!\$([A-Z]+)\$(\d+)(?::\$([A-Z]+)\$(\d+))?$")
_FORBIDDEN_WORDS_RE = re.compile(r"\b(simulator|what-if|what_if|sandbox|delta_min_pts)\b|\+\d+\s*pts", re.IGNORECASE)


def _col_letter_to_idx(letters: str) -> int:
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch) - 64)
    return idx


def _parse_xlsx_strict(xlsx_bytes: bytes) -> Dict[str, object]:
    """
    Exhaustively validate an OOXML .xlsx package in-memory and return parsed metadata.

    Asserts:
    - Valid ZIP integrity (`testzip() is None`)
    - Every .xml and .rels parses cleanly via `xml.etree.ElementTree.fromstring`
    - Complete `[Content_Types].xml` coverage and existing override targets
    - Every `.rels` relationship resolves to an existing ZIP entry
    - Strict worksheet row/column monotonicity, valid style & sharedString indices
    - Valid non-overlapping `twoCellAnchor` bounds on every drawing
    - Valid chart series `<c:f>` ranges matching existing cells, cell types, and cache counts
    """
    with zipfile.ZipFile(io.BytesIO(xlsx_bytes), "r") as zf:
        assert zf.testzip() is None, "Corrupt ZIP member detected"
        names = set(zf.namelist())
        parsed_xml: Dict[str, ET.Element] = {}
        raw_texts: Dict[str, str] = {}

        for name in sorted(names):
            data = zf.read(name)
            if name.endswith(".xml") or name.endswith(".rels"):
                text = data.decode("utf-8")
                raw_texts[name] = text
                parsed_xml[name] = ET.fromstring(text)  # noqa: S314

    # 1. Validate [Content_Types].xml
    assert "[Content_Types].xml" in parsed_xml
    ct_root = parsed_xml["[Content_Types].xml"]
    defaults = {
        el.attrib["Extension"]: el.attrib["ContentType"]
        for el in ct_root.findall(f"{{{NS_CT}}}Default")
    }
    assert defaults.get("rels") == "application/vnd.openxmlformats-package.relationships+xml"
    assert defaults.get("xml") == "application/xml"

    overrides = {
        el.attrib["PartName"]: el.attrib["ContentType"]
        for el in ct_root.findall(f"{{{NS_CT}}}Override")
    }
    for part_name in overrides:
        assert part_name.startswith("/")
        assert part_name[1:] in names, f"Content_Types override missing in ZIP: {part_name}"

    has_chart5 = "xl/charts/chart5.xml" in names
    expected_drawings = (1, 2, 3) if has_chart5 else (1, 2)
    expected_charts = (1, 2, 3, 4, 5) if has_chart5 else (1, 2, 3, 4)

    expected_overrides = {
        "/xl/workbook.xml": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
        "/xl/styles.xml": "application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml",
        "/xl/sharedStrings.xml": "application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml",
    }
    for d_idx in expected_drawings:
        expected_overrides[f"/xl/drawings/drawing{d_idx}.xml"] = (
            "application/vnd.openxmlformats-officedocument.drawing+xml"
        )
    for i in range(1, 7):
        expected_overrides[f"/xl/worksheets/sheet{i}.xml"] = (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
        )
    for c_idx in expected_charts:
        expected_overrides[f"/xl/charts/chart{c_idx}.xml"] = (
            "application/vnd.openxmlformats-officedocument.drawingml.chart+xml"
        )
    for part_name, expected_ct in expected_overrides.items():
        assert overrides.get(part_name) == expected_ct, f"Mismatch for {part_name}"

    # 2. Validate EVERY .rels file resolves to real ZIP members
    rels_map: Dict[str, Dict[str, Tuple[str, str]]] = {}
    for rels_path, rels_root in parsed_xml.items():
        if not rels_path.endswith(".rels"):
            continue
        owner_dir = posixpath.dirname(posixpath.dirname(rels_path))
        seen_rids: Set[str] = set()
        entries: Dict[str, Tuple[str, str]] = {}
        for rel in rels_root.findall(f"{{{NS_REL}}}Relationship"):
            rid = rel.attrib["Id"]
            rel_type = rel.attrib["Type"]
            target = rel.attrib["Target"]
            assert rid not in seen_rids, f"Duplicate {rid} in {rels_path}"
            seen_rids.add(rid)
            resolved = posixpath.normpath(posixpath.join(owner_dir, target)) if owner_dir else posixpath.normpath(target)
            assert resolved in names, f"Broken relationship {rels_path} {rid} -> {target} (resolved {resolved})"
            entries[rid] = (rel_type, resolved)
        rels_map[rels_path] = entries

    # 3. Validate xl/styles.xml
    styles_root = parsed_xml["xl/styles.xml"]
    style_children = [child.tag.split("}")[-1] for child in list(styles_root)]
    assert style_children == [
        "numFmts",
        "fonts",
        "fills",
        "borders",
        "cellStyleXfs",
        "cellXfs",
        "cellStyles",
    ]
    num_fmt_codes = {
        nf.attrib["numFmtId"]: nf.attrib["formatCode"]
        for nf in styles_root.findall(f"{{{NS_MAIN}}}numFmts/{{{NS_MAIN}}}numFmt")
    }
    assert any("$#,##0.00" in code for code in num_fmt_codes.values())
    assert "0.0%" in num_fmt_codes.values()
    assert "yyyy-mm-dd" in num_fmt_codes.values()

    fills = styles_root.findall(f"{{{NS_MAIN}}}fills/{{{NS_MAIN}}}fill")
    assert fills[0].find(f"{{{NS_MAIN}}}patternFill").attrib["patternType"] == "none"
    assert fills[1].find(f"{{{NS_MAIN}}}patternFill").attrib["patternType"] == "gray125"

    cell_xfs = styles_root.findall(f"{{{NS_MAIN}}}cellXfs/{{{NS_MAIN}}}xf")
    num_styles = len(cell_xfs)

    # 4. Validate xl/sharedStrings.xml
    sst_root = parsed_xml["xl/sharedStrings.xml"]
    shared_strings = [
        "".join(t.text or "" for t in si.findall(f".//{{{NS_MAIN}}}t"))
        for si in sst_root.findall(f"{{{NS_MAIN}}}si")
    ]
    assert int(sst_root.attrib["uniqueCount"]) == len(shared_strings)

    # 5. Validate workbook & sheets
    wb_root = parsed_xml["xl/workbook.xml"]
    wb_rels = rels_map["xl/_rels/workbook.xml.rels"]
    sheet_paths_by_name: Dict[str, str] = {}
    sheet_order: List[str] = []
    for sheet_el in wb_root.findall(f"{{{NS_MAIN}}}sheets/{{{NS_MAIN}}}sheet"):
        sname = sheet_el.attrib["name"]
        rid = sheet_el.attrib[f"{{{NS_DOC_REL}}}id"]
        assert len(sname) <= 31
        assert rid in wb_rels
        sheet_paths_by_name[sname] = wb_rels[rid][1]
        sheet_order.append(sname)

    assert tuple(sheet_order) == SHEET_NAMES

    # Parse every worksheet cell grid
    sheet_cells: Dict[str, Dict[str, Tuple[str, str, str]]] = {}  # sheet -> {A1: (kind, val, formula)}
    for sname, spath in sheet_paths_by_name.items():
        ws_root = parsed_xml[spath]
        cols = ws_root.findall(f"{{{NS_MAIN}}}cols/{{{NS_MAIN}}}col")
        assert len(cols) >= 6, f"Missing <cols> widths on {sname}"
        for col in cols:
            assert float(col.attrib["width"]) > 0

        cells_map: Dict[str, Tuple[str, str, str]] = {}
        prev_row = 0
        for row_el in ws_root.findall(f"{{{NS_MAIN}}}sheetData/{{{NS_MAIN}}}row"):
            r_num = int(row_el.attrib["r"])
            assert r_num > prev_row, f"Non-ascending rows in {sname}"
            prev_row = r_num
            prev_col = 0
            for c_el in row_el.findall(f"{{{NS_MAIN}}}c"):
                ref = c_el.attrib["r"]
                m = _CELL_REF_RE.match(ref)
                assert m is not None
                c_idx = _col_letter_to_idx(m.group(1))
                assert int(m.group(2)) == r_num
                assert c_idx > prev_col, f"Non-ascending columns in {sname} row {r_num}"
                prev_col = c_idx

                style_idx = int(c_el.attrib.get("s", "0"))
                assert 0 <= style_idx < num_styles

                t_attr = c_el.attrib.get("t", "")
                f_el = c_el.find(f"{{{NS_MAIN}}}f")
                v_el = c_el.find(f"{{{NS_MAIN}}}v")
                if t_attr == "s":
                    s_idx = int(v_el.text or "0")
                    assert 0 <= s_idx < len(shared_strings)
                    cells_map[ref] = ("str", shared_strings[s_idx], "")
                elif f_el is not None:
                    formula_text = f_el.text or ""
                    assert formula_text and not formula_text.startswith("="), f"Bad OOXML formula: {formula_text}"
                    assert v_el is not None and v_el.text is not None, f"Formula {ref} missing cached <v>"
                    cells_map[ref] = ("formula", v_el.text, formula_text)
                else:
                    assert v_el is not None and v_el.text is not None
                    cells_map[ref] = ("num", v_el.text, "")
        sheet_cells[sname] = cells_map

        # Verify non-overlapping mergeCells
        mc_parent = ws_root.find(f"{{{NS_MAIN}}}mergeCells")
        if mc_parent is not None:
            mc_nodes = mc_parent.findall(f"{{{NS_MAIN}}}mergeCell")
            assert int(mc_parent.attrib["count"]) == len(mc_nodes)
            merge_boxes: List[Tuple[int, int, int, int]] = []
            for mc in mc_nodes:
                a, b = mc.attrib["ref"].split(":")
                ma = _CELL_REF_RE.match(a)
                mb = _CELL_REF_RE.match(b)
                assert ma is not None and mb is not None
                c1, r1 = _col_letter_to_idx(ma.group(1)), int(ma.group(2))
                c2, r2 = _col_letter_to_idx(mb.group(1)), int(mb.group(2))
                assert c1 <= c2 and r1 <= r2
                for oc1, or1, oc2, or2 in merge_boxes:
                    overlaps = not (c2 < oc1 or oc2 < c1 or r2 < or1 or or2 < r1)
                    assert not overlaps, f"Overlapping mergeCells in {sname}: {mc.attrib['ref']}"
                merge_boxes.append((c1, r1, c2, r2))

        # Verify drawing r:id if present
        drawing_el = ws_root.find(f"{{{NS_MAIN}}}drawing")
        if drawing_el is not None:
            d_rid = drawing_el.attrib[f"{{{NS_DOC_REL}}}id"]
            s_rels_path = posixpath.join(posixpath.dirname(spath), "_rels", f"{posixpath.basename(spath)}.rels")
            assert s_rels_path in rels_map
            assert d_rid in rels_map[s_rels_path]

    # 6. Validate drawings & non-overlapping twoCellAnchor coordinates
    for d_idx in expected_drawings:
        d_path = f"xl/drawings/drawing{d_idx}.xml"
        d_rels_path = f"xl/drawings/_rels/drawing{d_idx}.xml.rels"
        d_root = parsed_xml[d_path]
        d_rels = rels_map[d_rels_path]
        anchors = d_root.findall(f"{{{NS_XDR}}}twoCellAnchor")
        assert len(anchors) >= 1
        boxes: List[Tuple[int, int, int, int]] = []
        seen_ids: Set[int] = set()
        for anc in anchors:
            fc = int(anc.find(f"{{{NS_XDR}}}from/{{{NS_XDR}}}col").text)
            fr = int(anc.find(f"{{{NS_XDR}}}from/{{{NS_XDR}}}row").text)
            tc = int(anc.find(f"{{{NS_XDR}}}to/{{{NS_XDR}}}col").text)
            tr = int(anc.find(f"{{{NS_XDR}}}to/{{{NS_XDR}}}row").text)
            assert 0 <= fc < tc, f"Invalid col span in {d_path}"
            assert 0 <= fr < tr, f"Invalid row span in {d_path}"
            for ofc, ofr, otc, otr in boxes:
                overlaps = not (tc <= ofc or otc <= fc or tr <= ofr or otr <= fr)
                assert not overlaps, f"Overlapping chart anchors in {d_path}"
            boxes.append((fc, fr, tc, tr))

            cnv = anc.find(f".//{{{NS_XDR}}}cNvPr")
            sid = int(cnv.attrib["id"])
            assert sid >= 2 and sid not in seen_ids
            seen_ids.add(sid)

            c_el = anc.find(f".//{{{NS_C}}}chart")
            c_rid = c_el.attrib[f"{{{NS_DOC_REL}}}id"]
            assert c_rid in d_rels

    # 7. Validate charts, series ranges, cell-type consistency, and caches
    chart_types: Dict[int, str] = {}
    for c_idx in expected_charts:
        c_path = f"xl/charts/chart{c_idx}.xml"
        c_root = parsed_xml[c_path]
        plot_area = c_root.find(f"{{{NS_C}}}chart/{{{NS_C}}}plotArea")
        assert plot_area is not None

        found_type = ""
        for candidate in ("barChart", "doughnutChart", "pieChart", "lineChart"):
            if plot_area.find(f"{{{NS_C}}}{candidate}") is not None:
                found_type = candidate
                break
        assert found_type, f"No recognized chart type in {c_path}"
        chart_types[c_idx] = found_type

        if found_type in ("barChart", "lineChart"):
            chart_node = plot_area.find(f"{{{NS_C}}}{found_type}")
            ax_ids = [el.attrib["val"] for el in chart_node.findall(f"{{{NS_C}}}axId")]
            cat_ax = plot_area.find(f"{{{NS_C}}}catAx")
            val_ax = plot_area.find(f"{{{NS_C}}}valAx")
            assert cat_ax is not None and val_ax is not None
            cat_id = cat_ax.find(f"{{{NS_C}}}axId").attrib["val"]
            val_id = val_ax.find(f"{{{NS_C}}}axId").attrib["val"]
            assert ax_ids == [cat_id, val_id]
            assert cat_ax.find(f"{{{NS_C}}}crossAx").attrib["val"] == val_id
            assert val_ax.find(f"{{{NS_C}}}crossAx").attrib["val"] == cat_id
        else:
            assert plot_area.find(f"{{{NS_C}}}catAx") is None
            assert plot_area.find(f"{{{NS_C}}}valAx") is None

        # Verify every strRef and numRef formula range
        for ref_tag, cache_tag, expected_kinds in (
            ("strRef", "strCache", {"str"}),
            ("numRef", "numCache", {"num", "formula"}),
        ):
            for ref_node in c_root.findall(f".//{{{NS_C}}}{ref_tag}"):
                f_text = ref_node.find(f"{{{NS_C}}}f").text or ""
                m = _CHART_RANGE_RE.match(f_text)
                assert m is not None, f"Invalid chart formula range in {c_path}: {f_text!r}"
                ref_sheet = m.group(1).replace("''", "'")
                c1, r1 = m.group(2), int(m.group(3))
                c2, r2 = m.group(4) or c1, int(m.group(5) or r1)
                assert ref_sheet in sheet_cells, f"Unknown sheet {ref_sheet!r} in {c_path}"
                assert c1 == c2 and 1 <= r1 <= r2, f"Inverted/invalid range {f_text}"
                expected_count = r2 - r1 + 1

                cache = ref_node.find(f"{{{NS_C}}}{cache_tag}")
                assert cache is not None
                pt_count = int(cache.find(f"{{{NS_C}}}ptCount").attrib["val"])
                pts = cache.findall(f"{{{NS_C}}}pt")
                assert pt_count == expected_count == len(pts)

                for row_num in range(r1, r2 + 1):
                    addr = f"{c1}{row_num}"
                    assert addr in sheet_cells[ref_sheet], f"Missing cell {ref_sheet}!{addr} referenced by {c_path}"
                    cell_kind = sheet_cells[ref_sheet][addr][0]
                    assert cell_kind in expected_kinds, (
                        f"Cell type mismatch for {ref_sheet}!{addr} in {c_path}: "
                        f"got {cell_kind}, expected {expected_kinds}"
                    )

    return {
        "names": names,
        "parsed_xml": parsed_xml,
        "raw_texts": raw_texts,
        "shared_strings": shared_strings,
        "sheet_order": sheet_order,
        "sheet_cells": sheet_cells,
        "chart_types": chart_types,
    }


def _build_rich_domain_snapshot() -> ExportSnapshot:
    """Construct a rich ExportSnapshot directly from real ChronoX Pydantic domain models."""
    accounts = [
        AccountOut(
            id=1,
            name="Navy Federal Everyday Checking",
            account_type=AccountType.CHECKING,
            balance_cents=485_000,
            institution="Navy Federal",
            is_liquid=True,
            currency="USD",
            available_cents=485_000,
            mask="4321",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-03-15T00:00:00Z",
        ),
        AccountOut(
            id=2,
            name="High-Yield Emergency Savings",
            account_type=AccountType.SAVINGS,
            balance_cents=820_000,
            institution="Marcus",
            is_liquid=True,
            currency="USD",
            available_cents=820_000,
            mask="8810",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-03-15T00:00:00Z",
        ),
        AccountOut(
            id=3,
            name="Sapphire Preferred",
            account_type=AccountType.CREDIT,
            balance_cents=-185_000,
            institution="Chase",
            is_liquid=False,
            currency="USD",
            credit_limit_cents=1_000_000,
            mask="9012",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-03-15T00:00:00Z",
        ),
    ]

    jars = [
        JarState(
            id=1,
            slug="bills",
            name="Bills & Housing",
            kind=JarKind.BILLS,
            icon="B",
            color="#0F172A",
            priority=1,
            target_pct=45.0,
            monthly_target_cents=270_000,
            auto_allocated_cents=270_000,
            funded_cents=270_000,
            rollover_cents=0,
            spent_cents=215_000,
            available_cents=55_000,
            pct_used=79.6,
            pace_delta_pct=2.1,
            health=JarHealth.CALM,
            is_locked=True,
            rollover_enabled=True,
            calm_note="Protected bills on track",
        ),
        JarState(
            id=2,
            slug="daily",
            name="Daily Essentials",
            kind=JarKind.DAILY,
            icon="D",
            color="#06B6D4",
            priority=2,
            target_pct=20.0,
            monthly_target_cents=120_000,
            auto_allocated_cents=120_000,
            funded_cents=120_000,
            rollover_cents=5_000,
            spent_cents=72_000,
            available_cents=53_000,
            pct_used=60.0,
            pace_delta_pct=-3.0,
            health=JarHealth.CALM,
            is_locked=False,
            rollover_enabled=True,
            calm_note="Steady grocery pace",
        ),
        JarState(
            id=3,
            slug="fun",
            name="Guilt-Free Fun",
            kind=JarKind.FUN,
            icon="F",
            color="#F59E0B",
            priority=3,
            target_pct=10.0,
            monthly_target_cents=60_000,
            auto_allocated_cents=60_000,
            funded_cents=60_000,
            rollover_cents=0,
            spent_cents=51_000,
            available_cents=9_000,
            pct_used=85.0,
            pace_delta_pct=11.0,
            health=JarHealth.WATCH,
            is_locked=False,
            rollover_enabled=False,
            calm_note="Pacing warm this cycle",
        ),
        JarState(
            id=4,
            slug="safety",
            name="Safety Buffer",
            kind=JarKind.SAFETY,
            icon="S",
            color="#10B981",
            priority=4,
            target_pct=15.0,
            monthly_target_cents=90_000,
            auto_allocated_cents=90_000,
            funded_cents=90_000,
            rollover_cents=15_000,
            spent_cents=0,
            available_cents=105_000,
            pct_used=0.0,
            pace_delta_pct=0.0,
            health=JarHealth.CALM,
            is_locked=True,
            rollover_enabled=True,
            calm_note="Reserve funded",
        ),
        JarState(
            id=5,
            slug="invest",
            name="Invest Vault",
            kind=JarKind.INVEST,
            icon="I",
            color="#6366F1",
            priority=5,
            target_pct=10.0,
            monthly_target_cents=60_000,
            auto_allocated_cents=60_000,
            funded_cents=60_000,
            rollover_cents=0,
            spent_cents=40_000,
            available_cents=20_000,
            pct_used=66.7,
            pace_delta_pct=0.0,
            health=JarHealth.CALM,
            is_locked=False,
            rollover_enabled=True,
            calm_note="Automated index buy",
        ),
    ]

    transactions = [
        TransactionOut(
            id=101,
            account_id=1,
            jar_id=None,
            jar_name="",
            tx_type=TransactionType.INCOME,
            amount_cents=320_000,
            merchant="Acme Corp Payroll",
            normalized_merchant="acme corp payroll",
            subcategory="Payroll",
            memo="Biweekly direct deposit",
            tx_date="2026-03-01",
            confidence=1.0,
            needs_review=False,
            source="simplefin",
            created_at="2026-03-01T08:00:00Z",
            tags=[TagOut(id=1, name="payroll", color="#10B981")],
        ),
        TransactionOut(
            id=102,
            account_id=1,
            jar_id=1,
            jar_name="Bills & Housing",
            tx_type=TransactionType.EXPENSE,
            amount_cents=185_000,
            merchant="Avalon Apartments",
            normalized_merchant="avalon apartments",
            subcategory="Rent",
            memo="March apartment rent",
            tx_date="2026-03-02",
            confidence=0.99,
            needs_review=False,
            source="simplefin",
            created_at="2026-03-02T08:00:00Z",
        ),
        TransactionOut(
            id=103,
            account_id=3,
            jar_id=2,
            jar_name="Daily Essentials",
            tx_type=TransactionType.EXPENSE,
            amount_cents=6_000,
            merchant="Target",
            normalized_merchant="target",
            subcategory="Groceries",
            memo="Hosting dinner supplies",
            tx_date="2026-03-08",
            confidence=0.95,
            needs_review=False,
            source="manual",
            created_at="2026-03-08T18:30:00Z",
            splits=[
                TransactionSplitOut(id=1, parent_tx_id=103, jar_id=2, jar_name="Daily Essentials", amount_cents=4_000, memo="Groceries"),
                TransactionSplitOut(id=2, parent_tx_id=103, jar_id=3, jar_name="Guilt-Free Fun", amount_cents=2_000, memo="Snacks"),
            ],
            tags=[TagOut(id=2, name="hosting", color="#06B6D4"), TagOut(id=3, name="split", color="#6366F1")],
        ),
        TransactionOut(
            id=104,
            account_id=1,
            jar_id=3,
            jar_name="Guilt-Free Fun",
            tx_type=TransactionType.EXPENSE,
            amount_cents=1_840,
            merchant="Uber",
            normalized_merchant="uber",
            subcategory="Transport",
            memo="Pouring rain commute home",
            tx_date="2026-03-11",
            confidence=0.98,
            needs_review=False,
            source="manual",
            created_at="2026-03-11T19:15:00Z",
        ),
    ]

    goals = [
        GoalOut(
            id=1,
            name="6-Month Emergency Runway",
            target_cents=1_500_000,
            saved_cents=820_000,
            remaining_cents=680_000,
            pct_funded=54.67,
            target_date="2026-12-31",
            priority=1,
            priority_order=0,
            category=GoalCategory.EMERGENCY,
            funding_jar_slug="safety",
            status="active",
            notes="Keep in HYSA",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-03-15T00:00:00Z",
        ),
        GoalOut(
            id=2,
            name="Mac Studio Workstation",
            target_cents=250_000,
            saved_cents=150_000,
            remaining_cents=100_000,
            pct_funded=60.0,
            target_date="2026-08-01",
            priority=2,
            priority_order=1,
            category=GoalCategory.GADGET,
            funding_jar_slug="fun",
            status="active",
            notes="Pay cash via sinking fund",
            created_at="2026-01-05T00:00:00Z",
            updated_at="2026-03-15T00:00:00Z",
        ),
    ]

    bills = [
        RecurringBillOut(
            id=1,
            name="Avalon Apartments Rent",
            normalized_merchant="avalon apartments",
            amount_cents=185_000,
            cadence=RecurrenceCadence.MONTHLY,
            next_due_date="2026-04-01",
            days_until_due=17,
            jar_id=1,
            is_subscription=False,
            is_essential=True,
            auto_pay=True,
            active=True,
            confidence=0.99,
            flagged_zombie=False,
        ),
        RecurringBillOut(
            id=2,
            name="Unused Streaming Bundle",
            normalized_merchant="unused streaming",
            amount_cents=2_299,
            cadence=RecurrenceCadence.MONTHLY,
            next_due_date="2026-03-22",
            days_until_due=7,
            jar_id=3,
            is_subscription=True,
            is_essential=False,
            auto_pay=True,
            active=True,
            confidence=0.92,
            flagged_zombie=True,
        ),
    ]

    quant_forecast = QuantForecastReport(
        generated_at="2026-03-15T12:00:00Z",
        horizon_days=30,
        starting_balance_cents=1_305_000,
        ending_balance_cents=1_420_000,
        lowest_balance_cents=1_110_000,
        lowest_balance_date="2026-03-29",
        overdraft_risk=False,
        safe_runway_days=30,
        daily_points=[
            DailyCashflowPoint(date="2026-03-15", balance_cents=1_305_000, inflow_cents=0, outflow_cents=4_500, events=["Baseline spend"]),
            DailyCashflowPoint(date="2026-03-22", balance_cents=1_260_000, inflow_cents=0, outflow_cents=2_299, events=["Streaming bill"]),
            DailyCashflowPoint(date="2026-03-29", balance_cents=1_110_000, inflow_cents=0, outflow_cents=15_000, events=["Card autopay"]),
            DailyCashflowPoint(date="2026-04-01", balance_cents=1_420_000, inflow_cents=320_000, outflow_cents=185_000, events=["Payroll + Rent"]),
        ],
        volatility=SpendingVolatilityMetrics(
            sample_days=60,
            daily_mean_cents=8_500,
            recommended_buffer_cents=95_000,
            stability_score_0_100=84,
        ),
    )

    dashboard = DashboardResponse(
        generated_at="2026-03-15T12:00:00Z",
        safe_to_spend=SafeToSpendBreakdown(
            total_liquid_cash_cents=1_305_000,
            unpaid_bills_until_payday_cents=187_299,
            locked_jar_reserves_cents=160_000,
            emergency_buffer_cents=100_000,
            credit_card_reserved_cents=185_000,
            safe_to_spend_cents=672_701,
            days_until_payday=14,
            daily_safe_to_spend_cents=48_050,
            next_payday_date="2026-03-29",
            estimated_paycheck_cents=320_000,
            cycle_progress_pct=50.0,
        ),
        safe_to_invest_cents=250_000,
        monthly_income_cents=640_000,
        monthly_spent_cents=378_000,
        smoothed_monthly_income_cents=640_000,
        accounts=accounts,
        jars=jars,
        rebalance_plans=[],
        upcoming_bills=bills,
        cooling_impulses=[],
        recent_transactions=transactions,
        adhd_stats=ADHDStats(
            weekly_reset_streak=4,
            grace_days_available=1,
            last_reset_date="2026-03-10",
            impulses_skipped_count=3,
            impulse_money_saved_cents=24_000,
            dopamine_xp=420,
            level=3,
            level_title="Calm Builder",
            recent_wins=["Completed weekly reset"],
        ),
        needs_triage_count=0,
        ai_status={},
        recent_budget_events=[],
        goals=goals,
        quant_forecast=quant_forecast,
    )

    analytics = AnalyticsOverview(
        period="90d",
        start_date="2026-01-01",
        end_date="2026-03-15",
        total_income_cents=1_600_000,
        total_expense_cents=980_000,
        net_surplus_cents=620_000,
        savings_rate_pct=38.75,
        avg_monthly_surplus_cents=206_667,
        expense_distribution=[
            TopExpenseItem(label="Housing & Rent", jar_slug="bills", amount_cents=555_000, pct_of_expenses=56.6, color="#0F172A"),
            TopExpenseItem(label="Groceries", jar_slug="daily", amount_cents=210_000, pct_of_expenses=21.4, color="#06B6D4"),
            TopExpenseItem(label="Dining & Coffee", jar_slug="fun", amount_cents=125_000, pct_of_expenses=12.8, color="#F59E0B"),
            TopExpenseItem(label="Transport", jar_slug="daily", amount_cents=90_000, pct_of_expenses=9.2, color="#10B981"),
        ],
        monthly_trends=[
            MonthlyTrendPoint(month="2026-01", income_cents=640_000, expense_cents=410_000, surplus_cents=230_000),
            MonthlyTrendPoint(month="2026-02", income_cents=640_000, expense_cents=380_000, surplus_cents=260_000),
            MonthlyTrendPoint(month="2026-03", income_cents=320_000, expense_cents=192_840, surplus_cents=127_160),
        ],
        net_worth_series=[
            NetWorthPoint(date="2026-03-15", liquid_cents=1_305_000, investment_cents=2_250_000, debt_cents=185_000, net_worth_cents=3_370_000),
        ],
    )

    portfolio = PortfolioSummary(
        generated_at="2026-03-15T12:00:00Z",
        total_value_cents=2_250_000,
        total_cost_basis_cents=1_500_000,
        known_cost_basis_value_cents=1_950_000,
        unknown_cost_basis_value_cents=300_000,
        known_cost_basis_holdings_count=2,
        unknown_cost_basis_holdings_count=1,
        cost_basis_coverage_pct=86.67,
        unrealized_pnl_cents=450_000,
        unrealized_pnl_pct=30.0,
        allocations=[
            PortfolioAllocationItem(asset_class="etf", value_cents=1_850_000, pct=82.22),
            PortfolioAllocationItem(asset_class="crypto", value_cents=400_000, pct=17.78),
        ],
        holdings=[
            HoldingOut(
                id=1,
                symbol="VTI",
                name="Vanguard Total Stock Market ETF",
                asset_class="etf",
                quantity_e8=6_500_000_000,  # 65 shares
                cost_basis_cents=1_420_000,
                cost_basis_known=True,
                price_e8=28_461_538_462,  # ~$284.61538462
                market_value_cents=1_850_000,
                unrealized_pnl_cents=430_000,
                unrealized_pnl_pct=30.28,
                source="schwab",
                updated_at="2026-03-15T12:00:00Z",
            ),
            HoldingOut(
                id=2,
                symbol="SHIB",
                name="Shiba Inu",
                asset_class="crypto",
                quantity_e8=414_078_674_948_240,  # 4,140,786.7494824 SHIB
                cost_basis_cents=80_000,
                cost_basis_known=True,
                price_e8=2_415,  # $0.00002415
                market_value_cents=100_000,
                unrealized_pnl_cents=20_000,
                unrealized_pnl_pct=25.0,
                source="coinbase",
                updated_at="2026-03-15T12:00:00Z",
            ),
            HoldingOut(
                id=3,
                symbol="BTC",
                name="Bitcoin",
                asset_class="crypto",
                quantity_e8=3_125_000,  # 0.03125 BTC
                cost_basis_cents=0,
                cost_basis_known=False,
                price_e8=9_600_000_000_000,  # $96,000.00
                market_value_cents=300_000,
                unrealized_pnl_cents=0,
                unrealized_pnl_pct=0.0,
                source="coinbase",
                updated_at="2026-03-15T12:00:00Z",
            ),
        ],
    )

    credit_report = CreditOptimizationReport(
        generated_at="2026-03-15T12:00:00Z",
        total_revolving_balance_cents=185_000,
        total_revolving_limit_cents=1_000_000,
        aggregate_utilization_pct=18.5,
        aggregate_tier="good",
        all_zero_penalty_risk=False,
        cards_with_balance_count=1,
        total_cards_count=1,
        total_debt_cents=185_000,
        total_monthly_minimums_cents=5_000,
        monthly_interest_burn_cents=3_389,
        dti_pct=0.78,
        dti_tier="excellent",
        autopay_missing_count=0,
        extra_monthly_budget_cents=50_000,
        credit_lines=[
            CreditLineOut(
                id=10,
                name="Sapphire Preferred",
                issuer="Chase",
                kind=DebtKind.CREDIT_CARD,
                balance_cents=185_000,
                credit_limit_cents=1_000_000,
                apr_bps=2199,
                effective_apr_bps=2199,
                min_payment_cents=5_000,
                effective_min_payment_cents=5_000,
                statement_closing_day=20,
                payment_due_day=15,
                next_statement_close_date="2026-03-20",
                days_until_close=5,
                next_due_date="2026-04-15",
                days_until_due=31,
                utilization_pct=18.5,
                utilization_tier="good",
                autopay_mode=AutopayMode.STATEMENT,
                monthly_interest_cents=3_389,
            )
        ],
        azeo_plan=[
            AZEOAction(
                credit_line_id=10,
                card_name="Sapphire Preferred",
                current_balance_cents=185_000,
                target_statement_balance_cents=89_000,
                pay_before_close_cents=96_000,
                pay_by_date="2026-03-18",
                statement_close_date="2026-03-20",
                resulting_util_pct=8.9,
                role="primary_reporting_card",
                explanation="Pay $960.00 before statement close to report 8.9% utilization.",
            )
        ],
        strategies=[
            DebtStrategyResult(
                strategy="avalanche",
                label="Avalanche (Highest APR First)",
                tagline="Minimizes total dollar interest paid.",
                recommended_for_adhd=True,
                months_to_debt_free=4,
                debt_free_date="2026-07-15",
                total_interest_cents=6_900,
                interest_saved_vs_minimums_cents=48_200,
                first_win_months=4,
                first_target_name="Sapphire Preferred",
            )
        ],
        recommended_strategy="avalanche",
        alerts=[],
        factor_analysis=CreditFactorBreakdown(
            latest_actual_scores=[
                CreditReportOut(
                    id=1,
                    bureau="experian",
                    model="fico8",
                    score=762,
                    pulled_date="2026-03-10",
                    source="manual",
                    notes="Official Experian portal",
                )
            ],
            fico8_factors=[
                CreditFactorItem(factor="payment_history", label="Payment History", weight_pct=35, status="optimal", summary="100% autopay active", guidance="Keep autopay enabled"),
                CreditFactorItem(factor="utilization", label="Amounts Owed / Utilization (AZEO)", weight_pct=30, status="good", summary="18.5% aggregate utilization", guidance="Pay $960 before close for <8.9%"),
                CreditFactorItem(factor="credit_age", label="Length of Credit History", weight_pct=15, status="good", summary="64 mo average age", guidance="Keep oldest no-AF card open"),
                CreditFactorItem(factor="new_credit", label="New Credit & Inquiries", weight_pct=10, status="optimal", summary="0 recent hard pulls", guidance="Space new applications"),
                CreditFactorItem(factor="credit_mix", label="Credit Mix", weight_pct=10, status="good", summary="Revolving card tracked", guidance="Never borrow solely for mix"),
            ],
            vantage3_factors=[
                CreditFactorItem(factor="payment_history", label="Payment History", weight_pct=40, status="optimal", summary="On-time autopay", guidance="Maintain on-time record"),
                CreditFactorItem(factor="depth_of_credit", label="Depth of Credit", weight_pct=21, status="good", summary="64 mo history", guidance="Age builds passively"),
                CreditFactorItem(factor="utilization", label="Credit Utilization", weight_pct=20, status="good", summary="18.5% utilization", guidance="Target single-digit utilization"),
                CreditFactorItem(factor="balances", label="Total Balances", weight_pct=11, status="good", summary="$1,850 revolving", guidance="Pay statement in full"),
                CreditFactorItem(factor="recent_credit", label="Recent Credit", weight_pct=5, status="optimal", summary="0 recent inquiries", guidance="Low velocity"),
                CreditFactorItem(factor="available_credit", label="Available Credit", weight_pct=3, status="optimal", summary="$8,150 unused credit", guidance="Healthy headroom"),
            ],
        ),
        credit_reports=[
            CreditReportOut(
                id=1,
                bureau="experian",
                model="fico8",
                score=762,
                pulled_date="2026-03-10",
                source="manual",
                notes="Official Experian portal",
            )
        ],
    )

    onetap_plan = {
        "cvar_95_buffer_cents": 118_500,
        "ruin_probability_pct": 1.2,
    }

    return FinanceExporter.build_snapshot(
        dashboard=dashboard,
        analytics=analytics,
        portfolio=portfolio,
        credit_report=credit_report,
        quant_forecast=quant_forecast,
        onetap_plan=onetap_plan,
        as_of_date="2026-03-15",
        currency="USD",
    )


class TestAppleNumbersGraphicalWorkbook:
    """Validate 6-sheet OOXML workbook structure, DrawingML charts, formulas, and zero-data safety."""

    def test_rich_populated_workbook_full_ooxml_and_charts(self) -> None:
        exporter = FinanceExporter()
        snapshot = _build_rich_domain_snapshot()
        artifact = exporter.export_apple_numbers_workbook(snapshot)

        # Honesty & metadata assertions
        assert artifact.filename == "chronox-executive-report-2026-03-15.xlsx"
        assert artifact.media_type == XLSX_MEDIA_TYPE
        assert artifact.format == "xlsx"
        assert artifact.native_numbers_converted is False
        assert artifact.sheet_names == SHEET_NAMES
        assert artifact.content[:2] == b"PK"

        parsed = _parse_xlsx_strict(artifact.content)

        # Verify all 5 embedded native chart types
        assert parsed["chart_types"] == {
            1: "barChart",
            2: "doughnutChart",
            3: "barChart",
            4: "lineChart",
            5: "pieChart",
        }

        # Verify zero forbidden wording or leaked Python Enum reprs anywhere in any XML part
        enum_leak_re = re.compile(
            r"\b(TransactionType|AccountType|JarKind|JarHealth|RecurrenceCadence|GoalCategory|DebtKind|AutopayMode)\."
        )
        for xml_path, xml_text in parsed["raw_texts"].items():
            assert _FORBIDDEN_WORDS_RE.search(xml_text) is None, (
                f"Forbidden wording found in {xml_path}"
            )
            assert enum_leak_re.search(xml_text) is None, (
                f"Leaked Enum repr found in {xml_path}"
            )

        # Sheet 1 KPIs & live formulas
        s1 = parsed["sheet_cells"][SHEET_EXECUTIVE]
        assert s1["B5"] == ("num", "13050.00", "")  # Liquid Cash
        assert s1["D5"] == ("num", "480.50", "")  # Safe-to-Spend Today
        assert s1["F5"] == ("num", "6727.01", "")  # Safe Until Payday
        assert s1["H5"] == ("num", "2500.00", "")  # Safe-to-Invest Cap
        assert s1["B6"] == ("num", "33700.00", "")  # Net Worth
        assert s1["D6"] == ("num", "1185.00", "")  # Statistical 95% Tail Cushion (from onetap CVaR95)
        assert s1["B7"][0] == "formula" and s1["B7"][2] == "F6-H6"
        # 5-Jar SUM totals on row 16 (5 jars in rows 11..15)
        assert s1["B16"] == ("formula", "6000.00", "SUM(B11:B15)")
        assert s1["C16"] == ("formula", "2420.00", "SUM(C11:C15)")
        assert s1["D16"] == ("formula", "3780.00", "SUM(D11:D15)")

        # Sheet 2 Cashflow & Runway: monthly net/savings formulas + runway trajectory
        s2 = parsed["sheet_cells"][SHEET_CASHFLOW]
        assert s2["A6"] == ("str", "2026-01", "")
        assert s2["B6"] == ("num", "6400.00", "")
        assert s2["C6"] == ("num", "4100.00", "")
        assert s2["D6"] == ("formula", "2300.00", "B6-C6")
        assert s2["B9"] == ("formula", "16000.00", "SUM(B6:B8)")
        assert s2["C9"] == ("formula", "9828.40", "SUM(C6:C8)")

        # Sheet 3 Portfolio: 8-decimal SHIB price + Unknown cost basis BTC
        s3 = parsed["sheet_cells"][SHEET_PORTFOLIO]
        assert s3["A7"] == ("str", "SHIB", "")
        assert s3["E7"] == ("num", "0.00002415", "")
        assert s3["A8"] == ("str", "BTC", "")
        assert s3["D8"] == ("num", "0.03125", "")
        assert s3["G8"] == ("str", "Unknown", "")
        assert s3["H8"] == ("str", "Unknown", "")
        # Portfolio totals row 9
        assert s3["F9"] == ("formula", "22500.00", "SUM(F6:F8)")
        assert s3["G9"] == ("formula", "15000.00", "SUM(G6:G8)")
        assert s3["H9"] == ("formula", "4500.00", "SUM(H6:H8)")

        # Sheet 4 Credit: official score 762, FICO 8 + VantageScore 3.0 weights, AZEO paydown
        s4 = parsed["sheet_cells"][SHEET_CREDIT]
        s4_str_values = {v for kind, v, _ in s4.values() if kind == "str"}
        assert "FICO 8" in s4_str_values
        assert "VantageScore 3.0" in s4_str_values
        assert "Sapphire Preferred" in s4_str_values
        assert any(v == "960.00" and kind == "num" for kind, v, _ in s4.values())  # AZEO paydown
        assert any(v == "762" and kind == "num" for kind, v, _ in s4.values())  # Reported bureau score

        # Sheet 5 Goals & Bills: live goal remaining/funded formulas + bills SUM & review flag
        s5 = parsed["sheet_cells"][SHEET_GOALS_BILLS]
        assert s5["A6"] == ("str", "6-Month Emergency Runway", "")
        assert s5["E6"] == ("formula", "6800.00", "MAX(0,C6-D6)")
        assert s5["C8"] == ("formula", "17500.00", "SUM(C6:C7)")
        assert s5["D8"] == ("formula", "9700.00", "SUM(D6:D7)")
        assert any(v == "Review Subscription" and kind == "str" for kind, v, _ in s5.values())
        assert any(kind == "formula" and v == "1872.99" and f.startswith("SUM(B") for kind, v, f in s5.values())

        # Sheet 6 Transactions Ledger: numeric amounts & live SUM row
        s6 = parsed["sheet_cells"][SHEET_TRANSACTIONS]
        assert s6["H5"] == ("num", "3200.00", "")  # Income +3200
        assert s6["H6"] == ("num", "-1850.00", "")  # Rent -1850
        assert s6["H7"] == ("num", "-60.00", "")  # Target -60
        assert s6["H8"] == ("num", "-18.40", "")  # Uber -18.40
        assert s6["H9"] == ("formula", "1271.60", "SUM(H5:H8)")

    def test_zero_data_fresh_profile_workbook_is_valid_no_repair_warnings(self) -> None:
        exporter = FinanceExporter()
        empty_snapshot = ExportSnapshot(as_of_date="2026-03-15", generated_at="2026-03-15T00:00:00Z")
        artifact = exporter.export_apple_numbers_workbook(empty_snapshot)

        parsed = _parse_xlsx_strict(artifact.content)

        # All 6 sheets exist; empty $0 portfolio cleanly skips blank zero-value pie chart (chart5)
        assert tuple(parsed["sheet_order"]) == SHEET_NAMES
        assert set(parsed["chart_types"].keys()) == {1, 2, 3, 4}
        s3_strs = {v for kind, v, _ in parsed["sheet_cells"][SHEET_PORTFOLIO].values() if kind == "str"}
        assert any("Portfolio Allocation chart" in s for s in s3_strs)

        # Zero invented balances or income on a fresh $0 profile
        s1 = parsed["sheet_cells"][SHEET_EXECUTIVE]
        assert s1["B5"] == ("num", "0.00", "")
        assert s1["D5"] == ("num", "0.00", "")
        assert s1["F5"] == ("num", "0.00", "")
        assert s1["H5"] == ("num", "0.00", "")
        assert s1["B6"] == ("num", "0.00", "")
        assert s1["D6"] == ("num", "0.00", "")

        # Sheet 6 still has valid SUM formula over placeholder row (H5:H5 -> 0.00)
        s6 = parsed["sheet_cells"][SHEET_TRANSACTIONS]
        assert s6["H5"] == ("num", "0.00", "")
        assert s6["H6"] == ("formula", "0.00", "SUM(H5:H5)")

        # Sheet 4 still shows factual published FICO 8 and VantageScore 3.0 weights
        s4_strs = {v for kind, v, _ in parsed["sheet_cells"][SHEET_CREDIT].values() if kind == "str"}
        assert "FICO 8" in s4_strs
        assert "VantageScore 3.0" in s4_strs

    def test_adversarial_strings_unicode_and_non_usd_currency(self) -> None:
        exporter = FinanceExporter()
        adversarial_snapshot = ExportSnapshot(
            as_of_date="2026-03-15",
            generated_at="2026-03-15T10:00:00Z",
            profile_currency="EUR",
            profile_name="=EVIL_TITLE() <script>&\"'",
            kpis=ExecutiveKPIs(liquid_cash_cents=250_000, net_worth_cents=250_000),
            accounts=(
                AccountExportRow(
                    id=1,
                    name="@SUM(1+1) <Checking>",
                    account_type="checking",
                    currency="EUR",
                    balance_cents=250_000,
                ),
            ),
            holdings=(
                HoldingExportRow(
                    symbol="=CMD",
                    name="Adversarial <Token> & \"Quote\" 'Single' \x00\x07\x1f",
                    asset_class="crypto",
                    quantity_e8=50_000_000,  # 0.5
                    price_e8=10_000_000_000,  # $100
                    market_value_cents=5_000,
                    cost_basis_cents=None,
                    cost_basis_known=False,
                ),
            ),
            transactions=(
                TransactionExportRow(
                    id=1,
                    tx_date="2026-03-12",
                    account_id=1,
                    account_name="@SUM(1+1) <Checking>",
                    currency="EUR",
                    direction="expense",
                    amount_cents=4_250,
                    merchant="=CMD|' /C calc'!A0",
                    category="+2+5*cmd|' /C calc'!A0",
                    jar="-negative-jar",
                    memo_reason="<script>&\"'\x00\x08\x1f payload",
                    tags="-tag1; \t=HYPERLINK(\"http://bad\")",
                    source="manual",
                    affects_balance=True,
                ),
                TransactionExportRow(
                    id=2,
                    tx_date="2026-03-13",
                    account_id=1,
                    account_name="Café René — 東京ラーメン — Müller & Söhne",
                    currency="GBP",
                    direction="income",
                    amount_cents=12_500,
                    merchant="Café René — 東京ラーメン — Müller & Söhne",
                    category="Consulting",
                    jar="Daily",
                    memo_reason="Client invoice € / £ / ¥",
                    source="manual",
                    affects_balance=True,
                ),
            ),
        )

        artifact = exporter.export_apple_numbers_workbook(adversarial_snapshot)
        parsed = _parse_xlsx_strict(artifact.content)

        s6 = parsed["sheet_cells"][SHEET_TRANSACTIONS]
        # OOXML shared strings (`t="s"`) never evaluate as formulas, so no spurious leading apostrophe junk
        assert s6["B5"] == ("str", "@SUM(1+1) <Checking>", "")
        assert s6["C5"] == ("str", "EUR", "")
        assert s6["E5"] == ("str", "=CMD|' /C calc'!A0", "")
        assert s6["F5"] == ("str", "+2+5*cmd|' /C calc'!A0", "")
        assert s6["G5"] == ("str", "-negative-jar", "")
        # XML special chars preserved safely, illegal XML 1.0 control chars stripped
        assert s6["I5"] == ("str", "<script>&\"' payload", "")
        # Numeric amount remains TRUE number, never neutralized as text
        assert s6["H5"] == ("num", "-42.50", "")
        # Unicode merchant & GBP preserved cleanly
        assert s6["C6"] == ("str", "GBP", "")
        assert s6["E6"] == ("str", "Café René — 東京ラーメン — Müller & Söhne", "")
        assert s6["H6"] == ("num", "125.00", "")
        # Live SUM formula across rows 5..6
        assert s6["H7"] == ("formula", "82.50", "SUM(H5:H6)")
        assert s6["C7"] == ("str", "MIXED", "")

        # Holding symbol clean in OOXML shared strings & unknown cost basis respected
        s3 = parsed["sheet_cells"][SHEET_PORTFOLIO]
        assert s3["A6"] == ("str", "=CMD", "")
        assert s3["G6"] == ("str", "Unknown", "")
        assert s3["H6"] == ("str", "Unknown", "")

    def test_auto_derived_analytics_and_dual_series_p50_p10_runway_chart(self) -> None:
        exporter = FinanceExporter()
        snap = FinanceExporter.build_snapshot(
            transactions=[
                {"id": 1, "tx_date": "2026-02-01", "tx_type": "income", "amount_cents": 400_000, "merchant": "Employer", "subcategory": "Salary"},
                {"id": 2, "tx_date": "2026-02-05", "tx_type": "expense", "amount_cents": 150_000, "merchant": "Landlord", "subcategory": "Rent", "jar_name": "Bills"},
                {"id": 3, "tx_date": "2026-03-01", "tx_type": "income", "amount_cents": 400_000, "merchant": "Employer", "subcategory": "Salary"},
                {"id": 4, "tx_date": "2026-03-10", "tx_type": "expense", "amount_cents": 35_000, "merchant": "Whole Foods", "subcategory": "Groceries", "jar_name": "Daily"},
            ],
            portfolio={
                "holdings": [
                    {"symbol": "AAPL", "name": "Apple Inc.", "asset_class": "equity", "quantity_e8": 1_000_000_000, "price_e8": 22_500_000_000, "cost_basis_cents": 180_000, "cost_basis_known": True},
                ]
            },
            onetap_plan={
                "cvar_95_buffer_cents": 85_000,
                "runway_points": [
                    {"date": "2026-03-15", "p50_balance_cents": 500_000, "p10_balance_cents": 460_000, "events": ["Start"]},
                    {"date": "2026-03-30", "p50_balance_cents": 620_000, "p10_balance_cents": 510_000, "events": ["Payday"]},
                ],
            },
            as_of_date="2026-03-15",
        )

        # Auto-derived top expenses, monthly trends, and allocations
        assert len(snap.top_expenses) == 2
        assert len(snap.monthly_cashflow) == 2
        assert len(snap.allocations) == 1
        assert snap.allocations[0].asset_class == "equity"

        artifact = exporter.export_apple_numbers_workbook(snap)
        parsed = _parse_xlsx_strict(artifact.content)

        # Verify Chart 4 lineChart has 2 series (p50 and p10)
        chart4_root = parsed["parsed_xml"]["xl/charts/chart4.xml"]
        line_series = chart4_root.findall(f".//{{{NS_C}}}lineChart/{{{NS_C}}}ser")
        assert len(line_series) == 2


class TestCSVAndExecutivePackExports:
    """Validate single-file transactions CSV and 7-file Executive CSV Pack ZIP."""

    def test_export_transactions_csv_columns_bom_and_formula_injection(self) -> None:
        exporter = FinanceExporter()
        accounts = [{"id": 1, "name": "@BankChecking", "currency": "EUR"}]
        txs = [
            {
                "id": 1,
                "tx_date": "2026-03-10",
                "account_id": 1,
                "tx_type": "expense",
                "amount_cents": 1_840,
                "merchant": "=CMD|' /C calc'!A0",
                "subcategory": "+SUM(1,2)",
                "jar_name": "-FunJar",
                "memo": "\t=HYPERLINK(\"http://evil\")",
                "tags": ["@admin", "rain"],
                "source": "\r=1+1",
                "affects_balance": 1,
                "splits": [{"jar_name": "=SplitJar", "amount_cents": 1_840}],
            },
            {
                "id": 2,
                "tx_date": "2026-03-11",
                "account_id": 1,
                "tx_type": "income",
                "amount_cents": 250_000,
                "merchant": "Café René — 東京",
                "subcategory": "Payroll",
                "jar_name": "",
                "memo": "  =leading-space-payload",
                "tags": ["salary"],
                "source": "simplefin",
                "affects_balance": True,
            },
            {
                "id": 3,
                "tx_date": "2026-03-12",
                "account_id": 1,
                "tx_type": "transfer",
                "transfer_direction": "out",
                "amount_cents": 10_000,
                "merchant": "Savings Sweep",
                "subcategory": "Transfer",
                "jar_name": "Safety",
                "memo": "Moved $100 to savings because bonus hit",
                "affects_balance": False,
                "source": "manual",
            },
        ]

        # With UTF-8 BOM (default)
        raw_bom = exporter.export_transactions_csv(txs, accounts=accounts, include_bom=True)
        assert raw_bom.startswith(b"\xef\xbb\xbf")

        # Without BOM
        raw_no_bom = exporter.export_transactions_csv(txs, accounts=accounts, include_bom=False)
        assert not raw_no_bom.startswith(b"\xef\xbb\xbf")

        rows = list(csv.DictReader(io.StringIO(raw_bom.decode("utf-8-sig"))))
        assert tuple(rows[0].keys()) == TRANSACTIONS_CSV_HEADERS
        assert len(rows) == 3

        # Row 0: All 6 formula prefixes neutralized on text cells, true numbers preserved
        r0 = rows[0]
        assert r0["account_name"] == "'@BankChecking"
        assert r0["currency"] == "EUR"
        assert r0["direction"] == "expense"
        assert r0["amount_dollars"] == "-18.40"
        assert Decimal(r0["amount_dollars"]) == Decimal("-18.40")
        assert r0["amount_cents"] == "-1840"
        assert r0["merchant"] == "'=CMD|' /C calc'!A0"
        assert r0["category"] == "'+SUM(1,2)"
        assert r0["jar"] == "'-FunJar"
        assert r0["memo_reason"] == "'\t=HYPERLINK(\"http://evil\")"
        assert r0["tags"] == "'@admin; rain"
        assert r0["source"] == "'\r=1+1"
        assert r0["affects_balance"] == "true"
        assert r0["split_summary"] == "'=SplitJar ($18.40)"

        # Row 1: Unicode preserved, leading-space formula neutralized, positive income numeric
        r1 = rows[1]
        assert r1["merchant"] == "Café René — 東京"
        assert r1["amount_dollars"] == "2500.00"
        assert r1["amount_cents"] == "250000"
        assert r1["memo_reason"] == "'  =leading-space-payload"

        # Row 2: Transfer out negative signed amount, affects_balance false
        r2 = rows[2]
        assert r2["direction"] == "transfer"
        assert r2["transfer_direction"] == "out"
        assert r2["amount_dollars"] == "-100.00"
        assert r2["amount_cents"] == "-10000"
        assert r2["affects_balance"] == "false"

        # Direct ExportSnapshot support on export_transactions_csv & artifact wrapper
        snap = _build_rich_domain_snapshot()
        art = exporter.export_transactions_csv_artifact(snap)
        assert art.filename == "chronox-transactions-2026-03-15.csv"
        assert art.row_counts["transactions"] == 4

    def test_export_executive_csv_pack_zip_seven_tables(self) -> None:
        exporter = FinanceExporter()
        snapshot = _build_rich_domain_snapshot()
        pack_bytes = exporter.export_executive_csv_pack_zip(snapshot, include_bom=True)

        with zipfile.ZipFile(io.BytesIO(pack_bytes), "r") as zf:
            assert zf.testzip() is None
            assert tuple(zf.namelist()) == CSV_PACK_FILENAMES

            for fname in CSV_PACK_FILENAMES:
                raw = zf.read(fname)
                assert raw.startswith(b"\xef\xbb\xbf"), f"{fname} missing UTF-8 BOM"
                parsed = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
                assert len(parsed) >= 1, f"{fname} unexpectedly empty"

            # Verify 01_executive_summary.csv KPIs
            summary_rows = list(csv.DictReader(io.StringIO(zf.read("01_executive_summary.csv").decode("utf-8-sig"))))
            metrics = {r["metric"]: r["value"] for r in summary_rows}
            assert metrics["liquid_cash_dollars"] == "13050.00"
            assert metrics["safe_to_spend_today_dollars"] == "480.50"
            assert metrics["statistical_95_tail_cushion_dollars"] == "1185.00"
            assert metrics["net_worth_dollars"] == "33700.00"

            # Verify 06_portfolio_holdings.csv unknown cost basis & 8-decimal precision
            holdings_rows = list(csv.DictReader(io.StringIO(zf.read("06_portfolio_holdings.csv").decode("utf-8-sig"))))
            by_sym = {r["symbol"]: r for r in holdings_rows}
            assert by_sym["SHIB"]["price_dollars"] == "0.00002415"
            assert by_sym["BTC"]["quantity"] == "0.03125"
            assert by_sym["BTC"]["cost_basis_dollars"] == "Unknown"
            assert by_sym["BTC"]["unrealized_pnl_dollars"] == "Unknown"

            # Verify 05_credit_and_debt.csv has zero fabricated score point deltas
            credit_csv_text = zf.read("05_credit_and_debt.csv").decode("utf-8-sig")
            assert _FORBIDDEN_WORDS_RE.search(credit_csv_text) is None

        # Verify artifact wrapper too
        pack_art = exporter.export_executive_csv_pack_artifact(snapshot, include_bom=True)
        assert pack_art.filename == "chronox-executive-csv-pack-2026-03-15.zip"
        assert pack_art.format == "zip"
        assert pack_art.row_counts["transactions"] == 4
        assert pack_art.row_counts["holdings"] == 3

        # Also verify empty $0 profile CSV pack produces all 7 valid headered CSV files
        empty_pack = exporter.export_executive_csv_pack_zip(ExportSnapshot(as_of_date="2026-03-15"), include_bom=False)
        with zipfile.ZipFile(io.BytesIO(empty_pack), "r") as zf:
            assert tuple(zf.namelist()) == CSV_PACK_FILENAMES
            for fname in CSV_PACK_FILENAMES:
                raw = zf.read(fname)
                assert not raw.startswith(b"\xef\xbb\xbf")
                assert len(raw.decode("utf-8").splitlines()) >= 1


class TestHonestyRuleAndMacOSNativeNumbersHelper:
    """Ensure .xlsx bytes are NEVER disguised as .numbers and macOS conversion works cleanly."""

    def test_honesty_rule_never_uses_numbers_extension_on_xlsx(self, monkeypatch: pytest.MonkeyPatch) -> None:
        exporter = FinanceExporter()
        snapshot = ExportSnapshot(as_of_date="2026-03-15")

        # Default export always returns .xlsx
        default_art = exporter.export_apple_numbers_workbook(snapshot)
        assert default_art.filename.endswith(".xlsx")
        assert not default_art.filename.endswith(".numbers")
        assert default_art.media_type == XLSX_MEDIA_TYPE
        assert default_art.native_numbers_converted is False

        # On Linux, requesting convert_to_native_numbers=True still honestly returns .xlsx
        monkeypatch.setattr(exporter_mod.sys, "platform", "linux")
        linux_art = exporter.export_apple_numbers_workbook(snapshot, convert_to_native_numbers=True)
        assert linux_art.filename == "chronox-executive-report-2026-03-15.xlsx"
        assert linux_art.media_type == XLSX_MEDIA_TYPE
        assert linux_art.format == "xlsx"
        assert linux_art.native_numbers_converted is False
        assert linux_art.content == default_art.content

    def test_macos_conversion_succeeds_only_when_numbers_app_and_osascript_succeed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exporter = FinanceExporter()
        xlsx_bytes = exporter.export_apple_numbers_workbook(ExportSnapshot(as_of_date="2026-03-15")).content

        monkeypatch.setattr(exporter_mod.sys, "platform", "darwin")
        monkeypatch.setattr(exporter_mod.shutil, "which", lambda cmd: "/usr/bin/osascript" if cmd == "osascript" else None)

        orig_exists = Path.exists

        def _fake_exists(self: Path) -> bool:
            if str(self) == "/Applications/Numbers.app":
                return True
            return orig_exists(self)

        monkeypatch.setattr(Path, "exists", _fake_exists)

        # Case 1: Simulated macOS AppleScript produces native .numbers bytes
        fake_numbers_payload = b"PK\x03\x04NATIVE_NUMBERS_IWA_ARCHIVE"

        def _fake_run_ok(cmd: List[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            out_file = Path(cmd[-1])
            out_file.write_bytes(fake_numbers_payload)
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(exporter_mod.subprocess, "run", _fake_run_ok)
        res_ok = convert_to_native_numbers_if_macos(xlsx_bytes, filename="chronox-executive-report-2026-03-15.xlsx")
        assert res_ok.filename == "chronox-executive-report-2026-03-15.numbers"
        assert res_ok.media_type == NUMBERS_MEDIA_TYPE
        assert res_ok.format == "numbers"
        assert res_ok.native_numbers_converted is True
        assert res_ok.content == fake_numbers_payload

        # Case 2: AppleScript fails -> transparently falls back to .xlsx without raising
        def _fake_run_fail(cmd: List[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="Numbers permission denied")

        monkeypatch.setattr(exporter_mod.subprocess, "run", _fake_run_fail)
        res_fail = convert_to_native_numbers_if_macos(xlsx_bytes, filename="chronox-executive-report-2026-03-15.xlsx")
        assert res_fail.filename == "chronox-executive-report-2026-03-15.xlsx"
        assert res_fail.media_type == XLSX_MEDIA_TYPE
        assert res_fail.format == "xlsx"
        assert res_fail.native_numbers_converted is False
        assert res_fail.content == xlsx_bytes


class TestPrecisionAndFormattingHelpers:
    """Direct unit checks for exact decimal, Excel date serial, and sanitization helpers."""

    def test_precision_date_and_column_helpers(self) -> None:
        assert cents_to_decimal_str(0) == "0.00"
        assert cents_to_decimal_str(-1840) == "-18.40"
        assert cents_to_decimal_str(123456789) == "1234567.89"

        assert e8_to_decimal_str(2415) == "0.00002415"
        assert e8_to_decimal_str(12_500_000) == "0.125"
        assert e8_to_decimal_str(100_000_000) == "1"
        assert e8_to_decimal_str(100_000_000, min_decimals=2) == "1.00"

        assert iso_date_to_excel_serial("2020-01-01") == 43831
        assert iso_date_to_excel_serial("invalid") is None
        assert iso_date_to_excel_serial("1899-12-31") is None

        assert col_index_to_letter(1) == "A"
        assert col_index_to_letter(26) == "Z"
        assert col_index_to_letter(27) == "AA"

        assert sanitize_csv_text("=1+1") == "'=1+1"
        assert sanitize_csv_text("+cmd") == "'+cmd"
        assert sanitize_csv_text("-note") == "'-note"
        assert sanitize_csv_text("@sum") == "'@sum"
        assert sanitize_csv_text("\t=x") == "'\t=x"
        assert sanitize_csv_text("\r=x") == "'\r=x"
        assert sanitize_csv_text("  =x") == "'  =x"
        assert sanitize_csv_text("Whole Foods Market") == "Whole Foods Market"
