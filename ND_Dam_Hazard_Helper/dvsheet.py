# -*- coding: utf-8 -*-
"""v2.20.0: the structures on the engineer's depth-velocity spreadsheet and in
the report.

After the classification, every flooded habitable structure of the
governing scenario (the structures of the report's table, report.py
report_structures) is written into a COPY of the engineer's hazard
spreadsheet (templates/Hazard_Spreadsheet.xlsx, or the template chosen in the
settings) and plotted on its first tab, on the "Houses on Foundation" and
"Mobile Homes" depth-velocity charts. The template itself is never written.

The first tab of the template (sheet "Structure Data Input ") holds:
  * the "QGIS csv Export" block (P:T): POI, V (ft/s), D (ft), AT (h), V+1
  * the "Houses on Foundation" table (B:E: Structure, Velocity, Depth,
    Arrival Time in minutes) with formulas into the export block
  * the "Mobile Homes" table below it, the same way
  * a chart per table: the zone areas (sheet "Chart Create Data") on a
    category axis with the structures as an XY series on top. Excel puts a
    category axis' first category (velocity 0) at x = 1, which is why the
    template plots V+1 (the note on T8). The copy keeps that.

The copy:
  * the export block lists every plotted structure (houses first, then
    mobile homes; POI = the plugin's Struct_ID) plus the ACER zone and the
    type; V+1 is MIN(V, figure maximum)+1: a velocity above the figure is
    drawn at its right edge, which is also where the classification reads
    the curves (constants.CURVE_V_MAX)
  * each table gets one row per structure of its type; when there are more
    than the template's rows, rows are inserted (the Mobile Homes block and
    its chart move down) - never fewer rows than the template
  * the chart series point at the new rows; the depth axis is raised when a
    structure is deeper than the template's axis (10 ft / 3.5 ft)
  * per-point labels are the Structure column (the template's
    "value from cells" labels)
  * the zoom inset of the template's sample data (a third chart over the
    houses chart, a dotted box and two dashed connectors) is left out: its
    fixed 4-6 ft/s x 0-2 ft window only fits the sample
  * formulas keep their cached values and Excel recalculates on opening

The plugin's classification uses the REG_05.2026 table (damsuite.tm11_curves);
the template draws its zones from the older quartic fits ("Chart Create
Data"), which differ from the table by up to about 0.2 ft. The report charts
(chart_svg) are drawn from the table, so they show the zones the
classification used.

Pure Python (standard library): the .xlsx is edited as XML inside the zip so
that every other part of the template (tabs, charts, styles, comments) is
copied unchanged. No QGIS import: tested outside QGIS.
"""

import datetime
import math
import os
import posixpath
import re
import uuid
import zipfile
from xml.sax.saxutils import escape

TEMPLATE_NAME = "Hazard_Spreadsheet.xlsx"
HOUSE, MOBILE = "house", "mobile"
# figure ranges (velocity ft/s) - the same as constants.CURVE_V_MAX
V_MAX = {HOUSE: 25.0, MOBILE: 16.0}
LABEL_MAX = 60                  # report charts: point labels when at most this many points
HOUSES_LABEL = "houses on foundation"
MOBILE_LABEL = "mobile homes"
EXPORT_LABEL = "qgis csv export"
SVG_FILES = {HOUSE: "DV_Chart_Houses_on_Foundation.svg", MOBILE: "DV_Chart_Mobile_Homes.svg"}
CLOSE_UP_FILES = {HOUSE: "DV_Chart_Houses_on_Foundation_close-up.svg",
                  MOBILE: "DV_Chart_Mobile_Homes_close-up.svg"}

_NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


class TemplateError(Exception):
    """The workbook does not have the layout this module fills."""


def default_template():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", TEMPLATE_NAME)


# ------------------------------------------------------------------ rows --
ZONE_NAMES = {1: "HIGH", 2: "JUDGMENT", 3: "LOW"}


def plot_rows(recs, mobile_type="Mobile home"):
    """[{id, kind, vel, depth, arr_hr, zone, outcome}] of the structures to
    plot (houses first, then mobile homes, by Struct_ID). `recs` are the
    report's structures (report_structures)."""
    rows = []
    for s in recs:
        if s.get("max_depth") is None:
            continue
        kind = MOBILE if s.get("acer_type") == mobile_type else HOUSE
        rows.append({"id": s.get("id"), "kind": kind, "vel": s.get("max_vel"),
                     "depth": float(s["max_depth"]), "arr_hr": s.get("arrival"),
                     "zone": ZONE_NAMES.get(s.get("zone"), ""), "outcome": s.get("outcome") or ""})
    rows.sort(key=lambda r: (r["kind"] != HOUSE, _id_key(r["id"])))
    return rows


def _id_key(v):
    try:
        return (0, float(v))
    except (TypeError, ValueError):
        return (1, str(v))


def output_name(template, dam_name, when=None):
    """<template name>_<dam name>_<YYYY-MM-DD>.xlsx"""
    stem = os.path.splitext(os.path.basename(template))[0] or "Hazard_Spreadsheet"
    dam = re.sub(r"[^A-Za-z0-9._-]+", "_", str(dam_name or "dam")).strip("._") or "dam"
    day = (when or datetime.date.today()).strftime("%Y-%m-%d")
    return "{}_{}_{}.xlsx".format(stem, dam, day)


# ------------------------------------------------------------ cell helpers --
def col_num(letters):
    n = 0
    for ch in letters.upper():
        n = n * 26 + ord(ch) - 64
    return n


def col_letters(n):
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


_CELL = re.compile(r'<c r="([A-Z]+)(\d+)"[^>]*?(?:/>|>.*?</c>)', re.S)
_ROW = re.compile(r'<row r="(\d+)"([^>]*?)(?:/>|>(.*?)</row>)', re.S)
_STYLE = re.compile(r'\ss="(\d+)"')


def _style(cell_xml):
    m = _STYLE.search(cell_xml[:cell_xml.find(">") + 1])
    return m.group(1) if m else None


def _num(v):
    r = repr(float(v))
    return r[:-2] if r.endswith(".0") else r


def _c_empty(ref, s):
    return '<c r="{}"{}/>'.format(ref, ' s="{}"'.format(s) if s else "")


def _c_num(ref, s, v):
    return '<c r="{}"{}><v>{}</v></c>'.format(ref, ' s="{}"'.format(s) if s else "", _num(v))


def _c_formula(ref, s, f, v):
    val = "" if v is None else "<v>{}</v>".format(_num(v))
    return '<c r="{}"{}><f>{}</f>{}</c>'.format(ref, ' s="{}"'.format(s) if s else "",
                                               escape(f), val)


def _c_str(ref, s, text):
    return '<c r="{}"{} t="inlineStr"><is><t xml:space="preserve">{}</t></is></c>'.format(
        ref, ' s="{}"'.format(s) if s else "", escape(str(text)))


def _recol(cell_xml, new_ref):
    return re.sub(r'^<c r="[A-Z]+\d+"', '<c r="{}"'.format(new_ref), cell_xml)


# ------------------------------------------------------------ zip helpers --
class _Pkg(object):
    def __init__(self, path):
        with zipfile.ZipFile(path) as z:
            self.names = z.namelist()
            self.data = {n: z.read(n) for n in self.names}
            self.info = {n: z.getinfo(n) for n in self.names}

    def text(self, name):
        return self.data[name].decode("utf-8")

    def put(self, name, text):
        if name not in self.data:
            self.names.append(name)
        self.data[name] = text.encode("utf-8") if isinstance(text, str) else text

    def drop(self, name):
        if name in self.data:
            del self.data[name]
            self.names.remove(name)

    def rels_of(self, part):
        d, b = posixpath.split(part)
        return posixpath.join(d, "_rels", b + ".rels")

    def targets(self, part):
        """{rId: (type, absolute target)} of a part."""
        rp = self.rels_of(part)
        if rp not in self.data:
            return {}
        out = {}
        base = posixpath.dirname(part)
        for m in re.finditer(r"<Relationship ([^>]*?)/>", self.text(rp)):
            a = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
            if a.get("TargetMode") == "External":
                continue
            out[a["Id"]] = (a["Type"].rsplit("/", 1)[-1],
                            posixpath.normpath(posixpath.join(base, a["Target"])))
        return out

    def save(self, path):
        tmp = path + ".tmp"
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for n in self.names:
                info = self.info.get(n)
                zi = zipfile.ZipInfo(n, date_time=info.date_time if info else
                                     datetime.datetime.now().timetuple()[:6])
                zi.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(zi, self.data[n])
        os.replace(tmp, path)


def _ref_rows(ref):
    """'Sheet'!$D$9:$D$22 -> (sheet, col, first row, last row)."""
    m = re.match(r"^(?:'((?:[^']|'')+)'|([^!]+))!\$?([A-Z]+)\$?(\d+)(?::\$?([A-Z]+)\$?(\d+))?$",
                 ref.strip())
    if not m:
        return None
    sheet = (m.group(1) or m.group(2) or "").replace("''", "'")
    return sheet, m.group(3), int(m.group(4)), int(m.group(6) or m.group(4))


def _quote_sheet(name):
    return "'{}'".format(name.replace("'", "''"))


# --------------------------------------------------------------- workbook --
def write_workbook(template, out_path, rows, info_line=""):
    """Copy `template` to `out_path` with `rows` (plot_rows) on the first tab
    and its two charts. Returns a dict: houses, mobile, rows_added,
    depth_axis {kind: max}, clipped (ids drawn at the velocity edge),
    no_velocity (ids not drawn), inset_removed. Raises TemplateError /
    OSError; never writes the template."""
    if os.path.abspath(template) == os.path.abspath(out_path):
        raise TemplateError("the copy would overwrite the template")
    pkg = _Pkg(template)
    wb = pkg.text("xl/workbook.xml")
    m = re.search(r'<sheet [^>]*?name="([^"]*)"[^>]*?r:id="(rId\d+)"', wb)
    if not m:
        raise TemplateError("no sheets in the workbook")
    sheet_name = _unescape(m.group(1))
    wb_t = pkg.targets("xl/workbook.xml")
    sheet_part = wb_t.get(m.group(2), (None, None))[1]
    if not sheet_part or sheet_part not in pkg.data or "worksheets" not in sheet_part:
        raise TemplateError("the first tab is not a worksheet")
    sheet = pkg.text(sheet_part)

    # ---- the layout of the first tab
    shared = _shared_strings(pkg)
    labels = _find_labels(sheet, shared)
    for key, what in ((HOUSES_LABEL, '"Houses on Foundation"'), (MOBILE_LABEL, '"Mobile Homes"'),
                      (EXPORT_LABEL, '"QGIS csv Export"')):
        if key not in labels:
            raise TemplateError("{} not found on the first tab".format(what))
    h_title, m_title = labels[HOUSES_LABEL][1], labels[MOBILE_LABEL][1]
    exp_col, exp_title = labels[EXPORT_LABEL]
    if not (h_title < m_title):
        raise TemplateError("the Mobile Homes table must be below the houses table")

    # ---- the charts on the first tab
    sheet_t = pkg.targets(sheet_part)
    drawing = next((t for (k, t) in sheet_t.values() if k == "drawing"), None)
    if drawing is None:
        raise TemplateError("the first tab has no charts")
    dr_t = pkg.targets(drawing)
    charts = {}
    for rid, (k, part) in dr_t.items():
        if k != "chart":
            continue
        refs = _scatter_refs(pkg.text(part))
        if refs is None:
            continue
        y = _ref_rows(refs["y"])
        if not y or y[0] != sheet_name:
            continue
        charts[rid] = (part, refs, y)
    h_first, m_first = h_title + 2, m_title + 2
    main = {}
    for rid, (part, refs, y) in charts.items():
        for kind, first in ((HOUSE, h_first), (MOBILE, m_first)):
            if y[2] == first and (kind not in main or y[3] > main[kind][2][3]):
                main[kind] = (rid, part, y, refs)
    for kind in (HOUSE, MOBILE):
        if kind not in main:
            raise TemplateError("no chart plots the {} table (an XY series of the rows from "
                                "{})".format(kind, h_first if kind == HOUSE else m_first))
    h_last = main[HOUSE][2][3]
    m_last = main[MOBILE][2][3]
    if h_last >= m_title:
        raise TemplateError("the houses chart's rows run into the Mobile Homes table")
    insets = [rid for rid in charts if rid not in (main[HOUSE][0], main[MOBILE][0])]

    houses = [r for r in rows if r["kind"] == HOUSE]
    mobiles = [r for r in rows if r["kind"] == MOBILE]
    h_cap, m_cap = h_last - h_first + 1, m_last - m_first + 1
    h_n, m_n = max(h_cap, len(houses)), max(m_cap, len(mobiles))
    dh, dm = h_n - h_cap, m_n - m_cap

    def new_row(r):
        if r < h_first:
            return r
        if r <= h_last:
            return None
        if r < m_first:
            return r + dh
        if r <= m_last:
            return None
        return r + dh + dm

    def shift_of(r):                       # anchors / merged ranges
        if r <= h_last:
            return 0
        if r <= m_last:
            return dh
        return dh + dm

    # ---- rows of the template
    sd0, sd1 = sheet.find("<sheetData"), sheet.find("</sheetData>")
    if sd0 < 0 or sd1 < 0:
        raise TemplateError("the first tab has no cells")
    sd_open_end = sheet.find(">", sd0) + 1
    if sheet[sd0:sd_open_end].endswith("/>"):
        raise TemplateError("the first tab has no cells")
    trows = {}
    for rm in _ROW.finditer(sheet[sd_open_end:sd1]):
        r = int(rm.group(1))
        attrs = re.sub(r'\sspans="[^"]*"', "", rm.group(2) or "")
        cells = {}
        for cm in _CELL.finditer(rm.group(3) or ""):
            cells[col_num(cm.group(1))] = cm.group(0)
        trows[r] = (attrs, cells)
    exp_c = col_num(exp_col)
    if exp_title + 1 >= h_first or exp_title + 1 not in trows:
        raise TemplateError("the export block's header row is not above the table rows")

    def styles_from(r, cols):
        cells = trows.get(r, ("", {}))[1]
        return {c: _style(cells[c]) if c in cells else None for c in cols}

    # styles of a filled data row (B..E) and the export block
    t_cols = (2, 3, 4, 5)
    def filled_row(first, last):
        for r in range(first, last + 1):
            cells = trows.get(r, ("", {}))[1]
            if 3 in cells and "<f>" in cells[3]:
                return r
        return first
    h_style = styles_from(filled_row(h_first, h_last), t_cols)
    m_style = styles_from(filled_row(m_first, m_last), t_cols)
    e_cols = [exp_c + i for i in range(5)]
    e_style = styles_from(h_first, e_cols)
    head_cells = trows[exp_title + 1][1]
    head_style = _style(head_cells.get(exp_c + 4, "")) if exp_c + 4 in head_cells else None
    right_edge = exp_c - 1        # columns of the tables / charts: left of the export block

    out = {}                      # new row -> (attrs, {col: xml})

    def keep_left(r_old, r_new, keep_right=False):
        attrs, cells = trows.get(r_old, ("", {}))
        kept = {c: _recol(x, "{}{}".format(col_letters(c), r_new)) for c, x in cells.items()
                if keep_right or c <= right_edge}
        out[r_new] = (attrs, kept)

    for r in sorted(trows):
        nr = new_row(r)
        if nr is None:
            continue
        keep_left(r, nr, keep_right=r <= exp_title + 1)

    # ---- the two tables (cells B..E, the rest of the row from the template row)
    exp_first = h_first
    exp_row = {}                  # structure index -> export row
    for i, rec in enumerate(houses + mobiles):
        exp_row[i] = exp_first + i

    clipped, no_vel = [], []

    def table(first_old, last_old, first_new, n, recs, base_index, st):
        for k in range(n):
            r_old = first_old + min(k, last_old - first_old)
            r_new = first_new + k
            attrs, cells = trows.get(r_old, ("", {}))
            row = {c: _recol(x, "{}{}".format(col_letters(c), r_new))
                   for c, x in cells.items() if c <= right_edge and c not in t_cols}
            if k < len(recs):
                rec, er = recs[k], exp_row[base_index + k]
                row[2] = _c_num("B{}".format(r_new), st[2], rec["id"]) if \
                    _is_num(rec["id"]) else _c_str("B{}".format(r_new), st[2], rec["id"])
                row[3] = _c_formula("C{}".format(r_new), st[3], "{}{}".format(
                    col_letters(exp_c + 1), er), rec["vel"])
                row[4] = _c_formula("D{}".format(r_new), st[4], "{}{}".format(
                    col_letters(exp_c + 2), er), rec["depth"])
                if rec["arr_hr"] is not None:
                    row[5] = _c_formula("E{}".format(r_new), st[5], "{}{}*60".format(
                        col_letters(exp_c + 3), er), rec["arr_hr"] * 60.0)
                else:
                    row[5] = _c_empty("E{}".format(r_new), st[5])
            else:
                for c in t_cols:
                    row[c] = _c_empty("{}{}".format(col_letters(c), r_new), st[c])
            old = out.get(r_new)
            if old:
                old[1].update(row)
            else:
                out[r_new] = (attrs, row)

    table(h_first, h_last, h_first, h_n, houses, 0, h_style)
    table(m_first, m_last, m_first + dh, m_n, mobiles, len(houses), m_style)

    # ---- the export block: POI, V, D, AT, V+1 (+ zone, type)
    for i, rec in enumerate(houses + mobiles):
        r = exp_row[i]
        attrs, cells = out.setdefault(r, ("", {}))
        L = [col_letters(c) for c in e_cols]
        cells[e_cols[0]] = _c_num("{}{}".format(L[0], r), e_style[e_cols[0]], rec["id"]) if \
            _is_num(rec["id"]) else _c_str("{}{}".format(L[0], r), e_style[e_cols[0]], rec["id"])
        if rec["vel"] is not None:
            cells[e_cols[1]] = _c_num("{}{}".format(L[1], r), e_style[e_cols[1]], rec["vel"])
            vmax = V_MAX[rec["kind"]]
            if rec["vel"] > vmax:
                clipped.append(rec["id"])
            cells[e_cols[4]] = _c_formula("{}{}".format(L[4], r), e_style[e_cols[4]],
                                          "MIN({}{},{})+1".format(L[1], r, _num(vmax)),
                                          min(rec["vel"], vmax) + 1.0)
        else:
            no_vel.append(rec["id"])
        cells[e_cols[2]] = _c_num("{}{}".format(L[2], r), e_style[e_cols[2]], rec["depth"])
        if rec["arr_hr"] is not None:
            cells[e_cols[3]] = _c_num("{}{}".format(L[3], r), e_style[e_cols[3]], rec["arr_hr"])
        zc, tc = exp_c + 5, exp_c + 6
        cells[zc] = _c_str("{}{}".format(col_letters(zc), r), None, rec["zone"])
        cells[tc] = _c_str("{}{}".format(col_letters(tc), r), None,
                           "Mobile home" if rec["kind"] == MOBILE else "House on foundation")
    hr = exp_title + 1
    attrs, cells = out.setdefault(hr, ("", {}))
    for c, text in ((exp_c + 5, "ACER zone"), (exp_c + 6, "Type")):
        cells[c] = _c_str("{}{}".format(col_letters(c), hr), head_style, text)

    # ---- the information line (the row above the houses title, else row 5)
    info_r = next((r for r in range(h_title - 1, 1, -1) if r not in out), None)
    if info_line and info_r:
        out[info_r] = ("", {2: _c_str("B{}".format(info_r), None, info_line)})

    # ---- sheet XML
    body = []
    last_col = 2
    for r in sorted(out):
        attrs, cells = out[r]
        if not cells and not attrs.strip():
            continue
        last_col = max([last_col] + list(cells))
        body.append('<row r="{}"{}>{}</row>'.format(
            r, attrs, "".join(cells[c] for c in sorted(cells))) if cells else
            '<row r="{}"{}/>'.format(r, attrs))
    last_row = max(out) if out else 1
    new_sheet = sheet[:sd_open_end] + "".join(body) + sheet[sd1:]
    new_sheet = re.sub(r'<dimension ref="[^"]*"/>',
                       '<dimension ref="A1:{}{}"/>'.format(col_letters(last_col), last_row),
                       new_sheet, count=1)
    new_sheet = _shift_merges(new_sheet, shift_of)
    if re.search(r"<(conditionalFormatting|dataValidations|tableParts)\b", new_sheet) and \
            (dh or dm):
        # not moved with the rows - say so rather than leave them pointing at the wrong rows
        new_sheet = re.sub(r"<conditionalFormatting\b.*?</conditionalFormatting>", "",
                           new_sheet, flags=re.S)
        new_sheet = re.sub(r"<dataValidations\b.*?</dataValidations>", "", new_sheet,
                           flags=re.S)
    pkg.put(sheet_part, new_sheet)

    # ---- drawing: move the anchors below the houses table, drop the inset
    inset_rids = set(insets)
    pkg.put(drawing, _fix_drawing(pkg.text(drawing), shift_of, inset_rids,
                                  main[HOUSE][0]))
    for rid in inset_rids:
        part = dr_t[rid][1]
        _remove_part(pkg, part)
    if inset_rids:
        rp = pkg.rels_of(drawing)
        rels = pkg.text(rp)
        for rid in inset_rids:
            rels = re.sub(r'<Relationship [^>]*Id="{}"[^>]*/>'.format(rid), "", rels)
        pkg.put(rp, rels)

    # ---- the chart series
    sq = _quote_sheet(sheet_name)
    axis = {}
    t_col = col_letters(exp_c + 4)
    for kind, recs, t_first, n, base in ((HOUSE, houses, h_first, h_n, 0),
                                          (MOBILE, mobiles, m_first + dh, m_n, len(houses))):
        rid, part, _y, refs = main[kind]
        last = t_first + n - 1
        if recs:
            x0 = exp_row[base]
            xr = "{}!${}${}:${}${}".format(sq, t_col, x0, t_col, x0 + n - 1)
            xs = [None if r["vel"] is None else min(r["vel"], V_MAX[kind]) + 1.0 for r in recs]
        else:                        # nothing of this type: an empty column of the table
            xr = "{}!$C${}:$C${}".format(sq, t_first, last)
            xs = []
        yr = "{}!$D${}:$D${}".format(sq, t_first, last)
        lr = "{}!$B${}:$B${}".format(sq, t_first, last)
        ys = [r["depth"] if r["vel"] is not None else None for r in recs]
        lbl = [str(r["id"]) for r in recs]
        dmax = max([0.0] + [r["depth"] for r in recs])
        text, axis[kind] = _fix_chart(pkg.text(part), xr, yr, lr, xs, ys, lbl, n, dmax)
        pkg.put(part, text)

    # ---- workbook: recalculate on opening, no stale calculation chain
    wb = pkg.text("xl/workbook.xml")
    if "<calcPr" in wb:
        if "fullCalcOnLoad" not in wb:
            wb = wb.replace("<calcPr", '<calcPr fullCalcOnLoad="1"', 1)
    else:
        wb = wb.replace("</workbook>", '<calcPr fullCalcOnLoad="1"/></workbook>')
    pkg.put("xl/workbook.xml", wb)
    for rid, (k, part) in wb_t.items():
        if k == "calcChain":
            _remove_part(pkg, part)
            rp = pkg.rels_of("xl/workbook.xml")
            pkg.put(rp, re.sub(r'<Relationship [^>]*Id="{}"[^>]*/>'.format(rid), "",
                               pkg.text(rp)))
    # the first tab is the one that opens
    pkg.save(out_path)
    return {"houses": len(houses), "mobile": len(mobiles), "rows_added": dh + dm,
            "depth_axis": axis, "clipped": clipped, "no_velocity": no_vel,
            "inset_removed": bool(inset_rids), "sheet": sheet_name}


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _unescape(s):
    return (s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
            .replace("&apos;", "'").replace("&amp;", "&"))


def _shared_strings(pkg):
    name = "xl/sharedStrings.xml"
    if name not in pkg.data:
        return []
    out = []
    for si in re.findall(r"<si>(.*?)</si>", pkg.text(name), re.S):
        out.append(_unescape("".join(re.findall(r"<t[^>]*>(.*?)</t>", si, re.S))))
    return out


def _find_labels(sheet, shared):
    """{lower-case label: (column letters, row)} of the text cells that name
    the blocks of the first tab (first occurrence)."""
    found = {}
    for cm in _CELL.finditer(sheet):
        x = cm.group(0)
        text = None
        if ' t="s"' in x[:x.find(">") + 1]:
            v = re.search(r"<v>(\d+)</v>", x)
            if v and int(v.group(1)) < len(shared):
                text = shared[int(v.group(1))]
        elif 't="inlineStr"' in x:
            text = _unescape("".join(re.findall(r"<t[^>]*>(.*?)</t>", x, re.S)))
        if not text:
            continue
        key = " ".join(text.split()).lower()
        for want in (HOUSES_LABEL, MOBILE_LABEL, EXPORT_LABEL):
            if key.startswith(want) and want not in found:
                found[want] = (cm.group(1), int(cm.group(2)))
    return found


def _scatter_refs(chart):
    m = re.search(r"<c:scatterChart>.*?</c:scatterChart>", chart, re.S)
    if not m:
        return None
    sc = m.group(0)
    x = re.search(r"<c:xVal>.*?<c:f>(.*?)</c:f>", sc, re.S)
    y = re.search(r"<c:yVal>.*?<c:f>(.*?)</c:f>", sc, re.S)
    if not y:
        return None
    return {"x": _unescape(x.group(1)) if x else "", "y": _unescape(y.group(1))}


def _shift_merges(sheet, shift_of):
    def fix(m):
        a = re.match(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", m.group(1))
        if not a:
            return m.group(0)
        s = shift_of(int(a.group(2)))
        return '<mergeCell ref="{}{}:{}{}"/>'.format(a.group(1), int(a.group(2)) + s,
                                                     a.group(3), int(a.group(4)) + s)
    return re.sub(r'<mergeCell ref="([^"]+)"/>', fix, sheet)


_ANCHOR = re.compile(r"<xdr:(twoCellAnchor|oneCellAnchor)\b.*?</xdr:\1>", re.S)


def _fix_drawing(text, shift_of, inset_rids, house_rid):
    """Shift every anchor by the rows inserted above its top row (keeping its
    size), and drop the zoom inset: its charts and the plain shapes (no text,
    no picture) drawn over the houses chart."""
    house_box = None
    for m in _ANCHOR.finditer(text):
        if 'r:id="{}"'.format(house_rid) in m.group(0):
            house_box = _box(m.group(0))
    pieces, pos = [], 0
    for m in _ANCHOR.finditer(text):
        a = m.group(0)
        pieces.append(text[pos:m.start()])
        pos = m.end()
        rid = re.search(r'<c:chart [^>]*r:id="(rId\d+)"', a)
        if rid and rid.group(1) in inset_rids:
            continue
        if inset_rids and house_box and re.search(r"<xdr:(sp|cxnSp)\b", a) and \
                "<a:t>" not in a and "<xdr:pic" not in a:
            b = _box(a)
            if b and b[0] >= house_box[0] and b[2] <= house_box[2] and \
                    b[1] >= house_box[1] and b[3] <= house_box[3]:
                continue
        fr = re.search(r"<xdr:from>.*?<xdr:row>(\d+)</xdr:row>", a, re.S)
        if fr:
            s = shift_of(int(fr.group(1)) + 1)
            if s:
                a = re.sub(r"(<xdr:(?:from|to)>.*?<xdr:row>)(\d+)(</xdr:row>)",
                           lambda q: q.group(1) + str(int(q.group(2)) + s) + q.group(3), a,
                           flags=re.S)
        pieces.append(a)
    pieces.append(text[pos:])
    return "".join(pieces)


def _box(anchor):
    f = re.search(r"<xdr:from><xdr:col>(\d+)</xdr:col>.*?<xdr:row>(\d+)</xdr:row>", anchor, re.S)
    t = re.search(r"<xdr:to><xdr:col>(\d+)</xdr:col>.*?<xdr:row>(\d+)</xdr:row>", anchor, re.S)
    if not f or not t:
        return None
    return int(f.group(1)), int(f.group(2)), int(t.group(1)), int(t.group(2))


def _remove_part(pkg, part):
    """Drop a part, its relationships and its content type. Parts only it
    used (chart styles, images) are left in the package; Excel ignores them."""
    pkg.drop(part)
    pkg.drop(pkg.rels_of(part))
    ct = pkg.text("[Content_Types].xml")
    ct = re.sub(r'<Override PartName="/{}"[^>]*/>'.format(re.escape(part)), "", ct)
    pkg.put("[Content_Types].xml", ct)


def _num_cache(values, fmt="General"):
    pts = "".join('<c:pt idx="{}"><c:v>{}</c:v></c:pt>'.format(i, _num(v))
                  for i, v in enumerate(values) if v is not None)
    return "<c:numCache><c:formatCode>{}</c:formatCode><c:ptCount val=\"{}\"/>{}</c:numCache>" \
        .format(fmt, len(values), pts)


def _fix_chart(chart, xref, yref, lref, xs, ys, labels, n, dmax):
    """Point the chart's XY series at the new rows, refresh the cached values,
    label every point from the Structure column, raise the depth axis when a
    structure is deeper than the template's. Returns (xml, depth axis max)."""
    m = re.search(r"<c:scatterChart>.*?</c:scatterChart>", chart, re.S)
    sc = m.group(0)
    pad = lambda vals: list(vals) + [None] * (n - len(vals))  # noqa: E731
    sc = re.sub(r"(<c:xVal><c:numRef><c:f>).*?(</c:f>).*?(</c:numRef></c:xVal>)",
                lambda q: q.group(1) + escape(xref) + q.group(2) + _num_cache(pad(xs)) +
                q.group(3), sc, count=1, flags=re.S)
    sc = re.sub(r"(<c:yVal><c:numRef><c:f>).*?(</c:f>).*?(</c:numRef></c:yVal>)",
                lambda q: q.group(1) + escape(yref) + q.group(2) + _num_cache(pad(ys), "0.00") +
                q.group(3), sc, count=1, flags=re.S)
    cache = "<c:ptCount val=\"{}\"/>".format(n) + "".join(
        '<c:pt idx="{}"><c:v>{}</c:v></c:pt>'.format(i, escape(t)) for i, t in enumerate(labels))
    sc = re.sub(r"(<c15:datalabelsRange><c15:f>).*?(</c15:f>)<c15:dlblRangeCache>.*?"
                r"</c15:dlblRangeCache>",
                lambda q: q.group(1) + escape(lref) + q.group(2) + "<c15:dlblRangeCache>" +
                cache + "</c15:dlblRangeCache>", sc, count=1, flags=re.S)
    # the template's per-point labels (some blanked): one "value from cells"
    # label per point, written the way Excel writes the template's (LibreOffice
    # only shows these; Excel also reads the series-level setting)
    pattern = next((d for d in re.findall(r"<c:dLbl>.*?</c:dLbl>", sc, re.S)
                    if 'type="CELLRANGE"' in d), None)
    sc = re.sub(r"<c:dLbl>.*?</c:dLbl>", "", sc, flags=re.S)
    if pattern:
        pattern = re.sub(r'<c:ext uri="\{C3380CC4[^"]*"[^>]*>.*?</c:ext>', "", pattern,
                         flags=re.S)
        per = "".join(
            re.sub(r'<a:fld id="\{[^}]*\}"', '<a:fld id="{{{}}}"'.format(
                str(uuid.uuid4()).upper()),
                re.sub(r'<c:idx val="\d+"/>', '<c:idx val="{}"/>'.format(i), pattern, count=1),
                count=1)
            for i in range(len(labels)))
        sc = sc.replace("<c:dLbls>", "<c:dLbls>" + per, 1)
    chart = chart[:m.start()] + sc + chart[m.end():]
    # depth axes (left and the hidden right one of the zone area): raise to fit
    blocks = [b for b in re.findall(r"<c:valAx>.*?</c:valAx>", chart, re.S)
              if re.search(r'<c:axPos val="[lr]"/>', b) and "<c:max " in b]
    maxes = [float(re.search(r'<c:max val="([^"]+)"/>', b).group(1)) for b in blocks]
    cur = max(maxes) if maxes else None
    if cur is not None and dmax > cur:
        unit = 5.0 if cur >= 5 else 0.5
        new = math.ceil(dmax / unit) * unit
        for b in blocks:
            chart = chart.replace(b, re.sub(r'<c:max val="[^"]+"/>',
                                            '<c:max val="{}"/>'.format(_num(new)), b, count=1))
        cur = new
    return chart, cur


# ------------------------------------------------------------ report SVG --
_LOW_FILL = "#999371"        # the template's green over red (40 % / 50 % on white)
_JUDG_FILL = "#FFC040"       # its yellow over red
_TEXT = {"high": "#C00000", "judg": "#C55A11", "low": "#375623"}
_TITLES = {HOUSE: ("Houses on Foundation", "ND Figure 5.1 (ACER TM 11 Figure 2)"),
           MOBILE: ("Mobile Homes", "ND Figure 5.2 (ACER TM 11 Figure 3)")}


def _nice_step(span, max_ticks):
    for step in (0.1, 0.2, 0.25, 0.5, 1.0, 2.0, 2.5, 5.0, 10.0, 20.0, 50.0):
        if span / step <= max_ticks:
            return step
    return span / max_ticks


def close_up_window(kind, rows):
    """(v_max, d_max) of a close-up of the structures of `kind` when the box
    that holds them all is at most a third of the figure, else None."""
    pts = [r for r in rows if r["kind"] == kind and r["vel"] is not None]
    if not pts:
        return None
    vmax, dfig = V_MAX[kind], (10.0 if kind == HOUSE else 3.5)
    v = max(min(r["vel"], vmax) for r in pts) * 1.15
    d = max(r["depth"] for r in pts) * 1.15
    vs, ds = _nice_step(v or 1.0, 8), _nice_step(d or 0.5, 8)
    win = (min(vmax, max(vs, math.ceil(v / vs) * vs)), max(ds, math.ceil(d / ds) * ds))
    if win[0] * min(win[1], dfig) > vmax * dfig / 3.0:
        return None
    return win


def chart_svg(kind, rows, curve_at, window=None, labels=None):
    """SVG text of one depth-velocity chart in the style of the template's:
    zones from `curve_at(level, v)` ("low" / "high", the classification's
    curves), the structures of `kind` from plot_rows as numbered dots.
    Velocities above the figure are drawn at its right edge as a triangle.
    window: (velocity max, depth max) of a close-up, else the whole figure."""
    pts = [r for r in rows if r["kind"] == kind and r["vel"] is not None]
    fig_v = V_MAX[kind]
    if window:
        vmax, dmax = float(window[0]), float(window[1])
    else:
        vmax = fig_v
        dmax = max([10.0 if kind == HOUSE else 3.5] + [r["depth"] for r in pts])
        unit = 5.0 if kind == HOUSE else 0.5
        dmax = math.ceil(dmax / unit - 1e-9) * unit
    W, H = 900, 560
    L, R, T, B = 78, 24, 58, 70
    pw, ph = W - L - R, H - T - B

    def X(v):
        return L + pw * min(max(v, 0.0), vmax) / vmax

    def Y(d):
        return T + ph * (1.0 - min(max(d, 0.0), dmax) / dmax)

    o = ['<svg xmlns="http://www.w3.org/2000/svg" width="{0}" height="{1}" '
         'viewBox="0 0 {0} {1}" font-family="Arial, Helvetica, sans-serif">'.format(W, H),
         '<rect x="0" y="0" width="{}" height="{}" fill="#FFFFFF"/>'.format(W, H)]
    title, fig = _TITLES[kind]
    if window:
        fig = "close-up, {} ft/s x {} ft".format(_num(vmax), _num(dmax))
    o.append('<rect x="{}" y="8" width="{}" height="26" fill="#FFC000"/>'.format(L, pw))
    o.append('<text x="{}" y="27" font-size="17" font-weight="bold" text-anchor="middle" '
             'fill="#000000">{} - {}</text>'.format(L + pw / 2.0, escape(title), escape(fig)))
    vs = [vmax * i / 200.0 for i in range(201)]
    low = [max(0.0, curve_at("low", v)) for v in vs]
    high = [max(0.0, curve_at("high", v)) for v in vs]
    poly = lambda p: " ".join("{:.1f},{:.1f}".format(x, y) for x, y in p)  # noqa: E731
    o.append('<polygon points="{}" fill="{}" stroke="none"/>'.format(
        poly([(X(0), Y(0))] + [(X(v), Y(d)) for v, d in zip(vs, low)] + [(X(vmax), Y(0))]),
        _LOW_FILL))
    o.append('<polygon points="{}" fill="{}" stroke="none"/>'.format(
        poly([(X(v), Y(d)) for v, d in zip(vs, low)] +
             [(X(v), Y(d)) for v, d in reversed(list(zip(vs, high)))]), _JUDG_FILL))
    # grid: labelled depth lines, minor lines between; a line per velocity step
    if window:
        dstep = _nice_step(dmax, 8)
        dminor = dstep / 2.0
        vstep = _nice_step(vmax, 16)
    else:
        dstep, dminor = (5.0, 1.0) if kind == HOUSE else (0.5, 0.25)
        vstep = 1.0
    k = 0
    while k * dminor <= dmax + 1e-9:
        d = k * dminor
        major = abs(d / dstep - round(d / dstep)) < 1e-6
        o.append('<line x1="{0}" y1="{1:.1f}" x2="{2}" y2="{1:.1f}" stroke="{3}" '
                 'stroke-width="{4}"/>'.format(L, Y(d), L + pw, "#7F7F7F" if major else
                                               "#BFBFBF", 0.9 if major else 0.5))
        if major:
            o.append('<text x="{}" y="{:.1f}" font-size="14" text-anchor="end">{}</text>'.format(
                L - 8, Y(d) + 5, _num(round(d, 6))))
        k += 1
    k = 0
    while k * vstep <= vmax + 1e-9:
        v = k * vstep
        o.append('<line x1="{0:.1f}" y1="{1}" x2="{0:.1f}" y2="{2}" stroke="#BFBFBF" '
                 'stroke-width="0.5"/>'.format(X(v), T, T + ph))
        o.append('<text x="{:.1f}" y="{}" font-size="13" text-anchor="middle">{}</text>'.format(
            X(v), T + ph + 18, _num(round(v, 6))))
        k += 1
    o.append('<rect x="{}" y="{}" width="{}" height="{}" fill="none" stroke="#404040" '
             'stroke-width="1.2"/>'.format(L, T, pw, ph))
    # zone names (only where the zone shows)
    vh = vmax * 0.55
    hi, lo = curve_at("high", vh), max(0.0, curve_at("low", vh))
    if hi < dmax * 0.85:
        o.append('<text x="{:.1f}" y="{:.1f}" font-size="15" font-weight="bold" '
                 'text-anchor="middle" fill="{}">HIGH DANGER ZONE</text>'.format(
                     X(vh), Y(min(dmax * 0.95, hi + dmax * 0.12)), _TEXT["high"]))
    if lo < dmax * 0.9 and (min(hi, dmax) - lo) > dmax * 0.06:
        o.append('<text x="{:.1f}" y="{:.1f}" font-size="13" font-weight="bold" '
                 'text-anchor="middle" fill="{}">JUDGEMENT ZONE</text>'.format(
                     X(vh), Y((lo + min(hi, dmax)) / 2.0) + 5, _TEXT["judg"]))
    if lo > dmax * 0.06:
        o.append('<text x="{:.1f}" y="{:.1f}" font-size="14" font-weight="bold" '
                 'text-anchor="middle" fill="{}">LOW DANGER ZONE</text>'.format(
                     X(vh), Y(min(lo, dmax) / 2.0) + 5, _TEXT["low"]))
    o.append('<text x="{:.1f}" y="{}" font-size="15" font-weight="bold" text-anchor="middle">'
             'Velocity (feet per second)</text>'.format(L + pw / 2.0, H - 18))
    o.append('<text x="22" y="{0:.1f}" font-size="15" font-weight="bold" text-anchor="middle" '
             'transform="rotate(-90 22 {0:.1f})">Depth (feet)</text>'.format(T + ph / 2.0))
    show_labels = len(pts) <= LABEL_MAX if labels is None else labels
    rad = 11 if show_labels else 6
    for r in pts:
        if r["vel"] > vmax and vmax < fig_v:
            continue                      # outside the close-up
        x, y = X(r["vel"]), Y(r["depth"])
        if r["vel"] > fig_v:
            o.append('<polygon points="{:.1f},{:.1f} {:.1f},{:.1f} {:.1f},{:.1f}" '
                     'fill="#595959"/>'.format(x + 4, y, x - 14, y - rad, x - 14, y + rad))
        else:
            o.append('<circle cx="{:.1f}" cy="{:.1f}" r="{}" fill="#595959"/>'.format(x, y, rad))
        if show_labels:
            t = str(r["id"])
            fs = 11 if len(t) <= 2 else 9 if len(t) <= 4 else 7
            o.append('<text x="{:.1f}" y="{:.1f}" font-size="{}" font-weight="bold" '
                     'text-anchor="middle" fill="#FFFFFF">{}</text>'.format(
                         x - (5 if r["vel"] > fig_v else 0), y + fs * 0.36, fs, escape(t)))
    if not pts:
        o.append('<text x="{:.1f}" y="{:.1f}" font-size="15" text-anchor="middle" '
                 'fill="#404040">No flooded {} in this run</text>'.format(
                     L + pw / 2.0, T + 30, "houses on foundations" if kind == HOUSE else
                     "mobile homes"))
    o.append("</svg>")
    return "\n".join(o)


def curve_at_tm11(kind):
    """curve_at(level, v) for chart_svg from the plugin's REG_05.2026 table."""
    try:
        from .damsuite import tm11_curves as T
    except ImportError:                  # outside the package (tests)
        from damsuite import tm11_curves as T  # noqa
    name = "house" if kind == HOUSE else "mobile"
    return lambda level, v: T.depth_at(name, level, v)


def write_svg(kind, rows, path, window=None):
    """One report chart (chart_svg with the classification's curves) to `path`."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(chart_svg(kind, rows, curve_at_tm11(kind), window=window))
    return path
