"""A small .xlsx writer and reader with no dependency beyond the standard library (1.4.0).

QGIS's Python does not always have openpyxl, and GDAL's XLSX driver is not built into every
QGIS; the suite's settings workbook must open and save everywhere, so this module writes and
reads the subset of Office Open XML that a table of settings needs:

  * write(path, sheets): sheets = [(name, rows), ...]; rows = lists of cells; a cell is None, a
    bool, an int, a float or text. The first row of each sheet is bold and frozen. Column widths
    follow the longest text. Text is stored inline (no shared-string table).
  * read(path) -> {sheet name: rows} with the same cell types (shared strings, inline strings,
    numbers, booleans and formula results are read; dates come back as Excel serial numbers;
    errors as None). Rows are padded to the sheet's widest row; trailing empty rows are dropped.

Round-trips with Excel, LibreOffice and openpyxl were checked in the suite's tests (openpyxl
reads what this writes and this reads what openpyxl writes). Formulas, styles beyond the bold
header, merged cells, charts and comments are outside the subset: writing never produces them,
reading ignores them.
"""
import re
import zipfile
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
_CTRL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")
MAX_SHEET_NAME = 31
_BAD_SHEET_CHARS = re.compile(r"[\\/*?:\[\]]")


# ------------------------------------------------------------------ write --
def col_letter(i):
    """0 -> A, 25 -> Z, 26 -> AA."""
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def col_index(letters):
    """'A' -> 0, 'AA' -> 26."""
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch.upper()) - 64)
    return n - 1


def sheet_name(name, taken=()):
    """A sheet name Excel accepts: no \\ / * ? : [ ], at most 31 characters, unique."""
    s = _BAD_SHEET_CHARS.sub("_", str(name or "Sheet")).strip("'") or "Sheet"
    s = s[:MAX_SHEET_NAME]
    base, k = s, 2
    while s in taken:
        tail = " ({})".format(k)
        s = base[:MAX_SHEET_NAME - len(tail)] + tail
        k += 1
    return s


def _text(v):
    return _CTRL.sub("", str(v))


def _cell_xml(ref, v, style=0):
    s = ' s="{}"'.format(style) if style else ""
    if v is None or v == "":
        return ""
    if isinstance(v, bool):
        return '<c r="{}" t="b"{}><v>{}</v></c>'.format(ref, s, 1 if v else 0)
    if isinstance(v, (int, float)):
        if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
            return '<c r="{}" t="inlineStr"{}><is><t>{}</t></is></c>'.format(ref, s, v)
        return '<c r="{}"{}><v>{!r}</v></c>'.format(ref, s, v)
    t = escape(_text(v))
    space = ' xml:space="preserve"' if t != t.strip() or "\n" in t else ""
    return '<c r="{}" t="inlineStr"{}><is><t{}>{}</t></is></c>'.format(ref, s, space, t)


def _sheet_xml(rows, bold_header=True, freeze=True):
    widths = {}
    body = []
    for r, row in enumerate(rows):
        cells = []
        for c, v in enumerate(row):
            if v is None or v == "":
                continue
            widths[c] = max(widths.get(c, 0), min(len(_text(v)), 60))
            cells.append(_cell_xml("{}{}".format(col_letter(c), r + 1), v,
                                   1 if (bold_header and r == 0) else 0))
        if cells:
            body.append('<row r="{}">{}</row>'.format(r + 1, "".join(cells)))
    cols = "".join('<col min="{0}" max="{0}" width="{1:.2f}" customWidth="1"/>'.format(
        c + 1, max(8.0, w * 1.1 + 2.0)) for c, w in sorted(widths.items()))
    views = ('<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
             'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>') if freeze and rows else ""
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="{ns}" xmlns:r="{nsr}">{views}'
            '{cols}<sheetData>{body}</sheetData></worksheet>').format(
        ns=NS_MAIN, nsr=NS_REL, views=views, cols=("<cols>" + cols + "</cols>") if cols else "",
        body="".join(body))


_STYLES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<styleSheet xmlns="{ns}"><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
           '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
           '<fills count="2"><fill><patternFill patternType="none"/></fill>'
           '<fill><patternFill patternType="gray125"/></fill></fills>'
           '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
           '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
           '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
           '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
           '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
           '</styleSheet>').format(ns=NS_MAIN)


def write(path, sheets, bold_header=True, freeze=True):
    """Write sheets = [(name, rows), ...] to an .xlsx file. Returns the sheet names used."""
    sheets = list(sheets) or [("Sheet1", [])]
    names = []
    for name, _rows in sheets:
        names.append(sheet_name(name, names))
    n = len(sheets)
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-'
          'officedocument.spreadsheetml.sheet.main+xml"/>'
          '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-'
          'officedocument.spreadsheetml.styles+xml"/>' +
          "".join('<Override PartName="/xl/worksheets/sheet{}.xml" ContentType="application/vnd.'
                  'openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'.format(i + 1)
                  for i in range(n)) + "</Types>")
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="{}"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>').format(NS_PKG)
    wb = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<workbook xmlns="{}" xmlns:r="{}"><sheets>'.format(NS_MAIN, NS_REL) +
          "".join('<sheet name="{}" sheetId="{}" r:id="rId{}"/>'.format(escape(nm, {'"': "&quot;"}), i + 1, i + 1)
                  for i, nm in enumerate(names)) + "</sheets></workbook>")
    wb_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<Relationships xmlns="{}">'.format(NS_PKG) +
               "".join('<Relationship Id="rId{0}" Type="http://schemas.openxmlformats.org/officeDocument/'
                       '2006/relationships/worksheet" Target="worksheets/sheet{0}.xml"/>'.format(i + 1)
                       for i in range(n)) +
               '<Relationship Id="rId{}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
               'relationships/styles" Target="styles.xml"/></Relationships>'.format(n + 1))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/styles.xml", _STYLES)
        for i, (_name, rows) in enumerate(sheets):
            z.writestr("xl/worksheets/sheet{}.xml".format(i + 1), _sheet_xml(rows, bold_header, freeze))
    return names


# ------------------------------------------------------------------- read --
def _local(tag):
    return tag.split("}", 1)[-1]


def _strings(z):
    try:
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    out = []
    for si in root:
        if _local(si.tag) != "si":
            continue
        out.append("".join(t.text or "" for t in si.iter() if _local(t.tag) == "t"))
    return out


def _number(text):
    t = (text or "").strip()
    if not t:
        return None
    try:
        if re.fullmatch(r"[-+]?\d+", t):
            return int(t)
        return float(t)
    except ValueError:
        return t


def _cell_value(c, strings):
    t = c.get("t", "n")
    v_el = None
    is_el = None
    for ch in c:
        name = _local(ch.tag)
        if name == "v":
            v_el = ch
        elif name == "is":
            is_el = ch
    if t == "inlineStr":
        return "".join(x.text or "" for x in is_el.iter() if _local(x.tag) == "t") if is_el is not None else ""
    v = v_el.text if v_el is not None else None
    if v is None:
        return None
    if t == "s":
        try:
            return strings[int(v)]
        except (ValueError, IndexError):
            return None
    if t == "b":
        return v.strip() in ("1", "true", "TRUE")
    if t in ("str", "e"):
        return None if t == "e" else v
    return _number(v)


def _ref_col(ref):
    m = re.match(r"([A-Za-z]+)", ref or "")
    return col_index(m.group(1)) if m else None


def read(path):
    """{sheet name: rows} of an .xlsx file (see the module text for the cell types)."""
    out = {}
    with zipfile.ZipFile(path) as z:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = {}
        try:
            for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels")):
                rels[r.get("Id")] = r.get("Target")
        except KeyError:
            pass
        strings = _strings(z)
        sheets = []
        for el in wb.iter():
            if _local(el.tag) == "sheet":
                rid = el.get("{%s}id" % NS_REL) or el.get("r:id")
                target = rels.get(rid, "")
                if not target:
                    continue
                target = target.lstrip("/")
                if not target.startswith("xl/"):
                    target = "xl/" + target
                sheets.append((el.get("name"), target))
        for name, target in sheets:
            try:
                root = ET.fromstring(z.read(target))
            except KeyError:
                out[name] = []
                continue
            rows = {}
            for row in root.iter():
                if _local(row.tag) != "row":
                    continue
                try:
                    r = int(row.get("r")) - 1
                except (TypeError, ValueError):
                    r = len(rows)
                cells = rows.setdefault(r, {})
                nxt = 0
                for c in row:
                    if _local(c.tag) != "c":
                        continue
                    ci = _ref_col(c.get("r"))
                    if ci is None:
                        ci = nxt
                    nxt = ci + 1
                    val = _cell_value(c, strings)
                    if val is not None and val != "":
                        cells[ci] = val
            if not rows:
                out[name] = []
                continue
            width = max((max(cells) + 1 if cells else 0) for cells in rows.values())
            last = max(r for r, cells in rows.items() if cells) if any(rows.values()) else -1
            table = []
            for r in range(last + 1):
                cells = rows.get(r, {})
                table.append([cells.get(c) for c in range(width)])
            out[name] = table
    return out
