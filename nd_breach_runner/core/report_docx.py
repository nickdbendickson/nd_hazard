"""Word (.docx) submittal report from a dam folder, with no third-party dependency.

The OOXML is written by hand with `zipfile` and XML strings (python-docx is not
part of QGIS's Python), and only numpy-free standard-library code is used here.

Three layers:

* `Docx`: a small document builder (headings 1-3, paragraphs with bold / italic /
  code runs, bullet and numbered lists, tables with a shaded repeating header row,
  fixed column widths and borders, page breaks, PNG and JPEG images with correct
  EMU sizing and captions, a title block, header and footer with PAGE / NUMPAGES
  fields, a table-of-contents field with a plain-list fallback, core properties).
* `add_markdown`: converts the markdown subset the tool's own reports use
  (headings, bullets, pipe tables, bold, code spans, and a few extras) into `Docx`
  calls, so `run_report.md`, `Cascade_Report.md` and `consequences*.md` can be inserted.
  The ND 5.2 hazard classification written by the `ndhazardclass` tool (`Hazard
  Classification (ND 5.2).json|md` and its chart SVGs) is summarised the same way.
* `svg_to_png` and `build_submittal`: the figures the tool writes are SVG; Word needs
  PNG or JPEG, so `svg_to_png` tries whatever converter exists. `build_submittal`
  assembles the sectioned report from what the dam folder holds and skips what it
  does not.

Word fills the table of contents (and page numbers in it) when the file is opened
(`updateFields`); until then the field shows a plain, clickable list of headings.
"""
import csv
import datetime
import fnmatch
import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from xml.sax.saxutils import escape as _sax_escape

EMU_PER_IN = 914400
TWIPS_PER_IN = 1440

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_DECL = ('xmlns:w="{}" xmlns:r="{}" '
            'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
            'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"'.format(
                W_NS, R_NS))
_XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'

COLOR_HEAD = "1F3864"
SHADE_HEADER = "D9E2F3"
SHADE_KEY = "F2F2F2"
SHADE_WARN = "FFF6E0"
STATUS_SHADES = {"yes": "E2EFDA", "check": "FFF2CC", "no": "F8CBAD", "n/a": "EDEDED"}


# --------------------------------------------------------------------------------------
# text and XML helpers
# --------------------------------------------------------------------------------------
_BAD_XML = re.compile(u"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")


def clean_text(s):
    """Text with the characters XML 1.0 cannot carry removed."""
    if s is None:
        return ""
    if isinstance(s, bytes):
        s = s.decode("utf-8", "replace")
    return _BAD_XML.sub("", str(s))


def xesc(s):
    return _sax_escape(clean_text(s), {'"': "&quot;"})


def _rpr(b=False, i=False, u=False, code=False, color=None, size=None, sup=False, strike=False,
         shade=None):
    """Run properties in the element order the schema wants."""
    p = []
    if code:
        p.append('<w:rStyle w:val="Code"/>')
    if b:
        p.append("<w:b/><w:bCs/>")
    if i:
        p.append("<w:i/><w:iCs/>")
    if strike:
        p.append("<w:strike/>")
    if color:
        p.append('<w:color w:val="{}"/>'.format(color))
    if size:
        hp = int(round(size * 2))
        p.append('<w:sz w:val="{0}"/><w:szCs w:val="{0}"/>'.format(hp))
    if u:
        p.append('<w:u w:val="single"/>')
    if shade:
        p.append('<w:shd w:val="clear" w:color="auto" w:fill="{}"/>'.format(shade))
    if sup:
        p.append('<w:vertAlign w:val="superscript"/>')
    return "<w:rPr>{}</w:rPr>".format("".join(p)) if p else ""


def run_xml(text, **fmt):
    """One run. Tabs and newlines in `text` become <w:tab/> and <w:br/>."""
    text = clean_text(text)
    if not text:
        return ""
    body = []
    for part in re.split(u"(\t|\n)", text):
        if part == "\t":
            body.append("<w:tab/>")
        elif part == "\n":
            body.append("<w:br/>")
        elif part:
            body.append('<w:t xml:space="preserve">{}</w:t>'.format(_sax_escape(part)))
    return "<w:r>{}{}</w:r>".format(_rpr(**fmt), "".join(body))


def runs_xml(runs, **defaults):
    """`runs`: a string, or a list of strings / dicts {text, b, i, u, code, color, size}.
    `defaults` are formatting applied to every run (a run's own keys win)."""
    if runs is None:
        return ""
    if isinstance(runs, (str, bytes)):
        runs = [runs]
    out = []
    for r in runs:
        if isinstance(r, dict):
            fmt = dict(defaults)
            fmt.update({k: v for k, v in r.items() if k != "text"})
            out.append(run_xml(r.get("text", ""), **fmt))
        else:
            out.append(run_xml(r, **defaults))
    return "".join(out)


def _field_xml(instr, result="1", **fmt):
    rp = _rpr(**fmt)
    return ('<w:r>{0}<w:fldChar w:fldCharType="begin"/></w:r>'
            '<w:r>{0}<w:instrText xml:space="preserve"> {1} </w:instrText></w:r>'
            '<w:r>{0}<w:fldChar w:fldCharType="separate"/></w:r>'
            '<w:r>{0}<w:t>{2}</w:t></w:r>'
            '<w:r>{0}<w:fldChar w:fldCharType="end"/></w:r>'.format(rp, xesc(instr),
                                                                   xesc(result)))


def _ppr(style=None, keep_next=False, keep_lines=False, page_break_before=False, num=None,
         border_bottom=None, border_left=None, shade=None, tabs=None, space_before=None,
         space_after=None, line=None, ind_left=None, ind_hanging=None, align=None):
    p = []
    if style:
        p.append('<w:pStyle w:val="{}"/>'.format(style))
    if keep_next:
        p.append("<w:keepNext/>")
    if keep_lines:
        p.append("<w:keepLines/>")
    if page_break_before:
        p.append("<w:pageBreakBefore/>")
    if num:
        p.append('<w:numPr><w:ilvl w:val="{}"/><w:numId w:val="{}"/></w:numPr>'.format(
            num[1], num[0]))
    if border_bottom or border_left:
        b = []
        if border_left:
            b.append('<w:left w:val="single" w:sz="24" w:space="6" w:color="{}"/>'.format(
                border_left))
        if border_bottom:
            b.append('<w:bottom w:val="single" w:sz="6" w:space="1" w:color="{}"/>'.format(
                border_bottom))
        p.append("<w:pBdr>{}</w:pBdr>".format("".join(b)))
    if shade:
        p.append('<w:shd w:val="clear" w:color="auto" w:fill="{}"/>'.format(shade))
    if tabs:
        p.append("<w:tabs>{}</w:tabs>".format("".join(
            '<w:tab w:val="{}" w:pos="{}"/>'.format(k, pos) for k, pos in tabs)))
    if space_before is not None or space_after is not None or line is not None:
        a = ""
        if space_before is not None:
            a += ' w:before="{}"'.format(int(space_before))
        if space_after is not None:
            a += ' w:after="{}"'.format(int(space_after))
        if line is not None:
            a += ' w:line="{}" w:lineRule="auto"'.format(int(line))
        p.append("<w:spacing{}/>".format(a))
    if ind_left is not None or ind_hanging is not None:
        a = ""
        if ind_left is not None:
            a += ' w:left="{}"'.format(int(ind_left))
        if ind_hanging is not None:
            a += ' w:hanging="{}"'.format(int(ind_hanging))
        p.append("<w:ind{}/>".format(a))
    if align:
        p.append('<w:jc w:val="{}"/>'.format(
            {"justify": "both", "centre": "center"}.get(align, align)))
    return "<w:pPr>{}</w:pPr>".format("".join(p)) if p else ""


def para_xml(content_xml="", **ppr):
    return "<w:p>{}{}</w:p>".format(_ppr(**ppr), content_xml)


# --------------------------------------------------------------------------------------
# inline markdown -> runs
# --------------------------------------------------------------------------------------
_ESC_MAP = {"\\*": u"", "\\_": u"", "\\`": u"", "\\|": u"",
            "\\\\": u"", "\\#": u"", "\\[": u"", "\\]": u""}
_UNESC = {v: k[1] for k, v in _ESC_MAP.items()}
_INLINE = re.compile(
    r"(`[^`\n]+`"                                           # code span
    r"|\*\*[^*\n]+?\*\*"                                    # **bold**
    r"|__[^_\n]+?__"                                        # __bold__
    r"|(?<![\w*])\*(?![\s*])[^*\n]+?(?<![\s*])\*(?![\w*])"  # *italic*
    r"|(?<![\w])_(?![\s_])[^_\n]+?(?<![\s_])_(?![\w]))")   # _italic_ (not inside_words)
_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")


def _unescape(s):
    return "".join(_UNESC.get(c, c) for c in s)


def parse_inline(text):
    """Markdown inline text -> list of run dicts. Supports **bold**, *italic*,
    _italic_ (only at word edges, so file_names stay intact), `code`, [text](url)
    (shown as "text (url)") and backslash escapes."""
    s = clean_text(text)
    for k, v in _ESC_MAP.items():
        s = s.replace(k, v)
    s = _LINK.sub(lambda m: "{} ({})".format(m.group(1), m.group(2)), s)
    runs, pos = [], 0
    for m in _INLINE.finditer(s):
        if m.start() > pos:
            runs.append({"text": _unescape(s[pos:m.start()])})
        tok = m.group(0)
        if tok.startswith("`"):
            runs.append({"text": _unescape(tok[1:-1]), "code": True})
        elif tok.startswith(("**", "__")):
            runs.append({"text": _unescape(tok[2:-2]), "b": True})
        else:
            runs.append({"text": _unescape(tok[1:-1]), "i": True})
        pos = m.end()
    if pos < len(s):
        runs.append({"text": _unescape(s[pos:])})
    return [r for r in runs if r["text"]]


# --------------------------------------------------------------------------------------
# images
# --------------------------------------------------------------------------------------
PNG_SIG = b"\x89PNG\r\n\x1a\n"


def image_info(data):
    """(format, width_px, height_px, dpi_x, dpi_y) of PNG or JPEG bytes. dpi is None
    when the file does not say. Raises ValueError for anything else or a damaged file."""
    if data[:8] == PNG_SIG:
        if len(data) < 24 or data[12:16] != b"IHDR":
            raise ValueError("damaged PNG")
        w, h = struct.unpack(">II", data[16:24])
        dpi = None
        pos = 8
        while pos + 8 <= len(data):
            ln, typ = struct.unpack(">I4s", data[pos:pos + 8])
            if typ == b"pHYs" and ln >= 9:
                px, py, unit = struct.unpack(">IIB", data[pos + 8:pos + 17])
                if unit == 1 and px and py:
                    dpi = (round(px * 0.0254), round(py * 0.0254))
                break
            if typ in (b"IDAT", b"IEND"):
                break
            pos += 12 + ln
        if not w or not h:
            raise ValueError("PNG with no size")
        return ("png", w, h, dpi[0] if dpi else None, dpi[1] if dpi else None)
    if data[:2] == b"\xff\xd8":
        pos, n = 2, len(data)
        dpi = None
        while pos + 4 <= n:
            if data[pos] != 0xFF:
                pos += 1
                continue
            marker = data[pos + 1]
            if marker == 0xFF:
                pos += 1
                continue
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                pos += 2
                continue
            ln = struct.unpack(">H", data[pos + 2:pos + 4])[0]
            seg = data[pos + 4:pos + 2 + ln]
            if marker == 0xE0 and seg[:5] == b"JFIF\x00" and len(seg) >= 12:
                unit, xd, yd = struct.unpack(">BHH", seg[7:12])
                if unit == 1 and xd and yd:
                    dpi = (float(xd), float(yd))
                elif unit == 2 and xd and yd:
                    dpi = (xd * 2.54, yd * 2.54)
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                if len(seg) < 5:
                    break
                h, w = struct.unpack(">HH", seg[1:5])
                if not w or not h:
                    break
                return ("jpeg", w, h, dpi[0] if dpi else None, dpi[1] if dpi else None)
            pos += 2 + ln
        raise ValueError("damaged JPEG")
    raise ValueError("not a PNG or JPEG image")


def _sane_dpi(d):
    return d if d and 30.0 <= d <= 2400.0 else 96.0


def read_image(source):
    """(bytes, info) for a file path or bytes."""
    if isinstance(source, (bytes, bytearray)):
        data = bytes(source)
    else:
        with open(source, "rb") as f:
            data = f.read()
    return data, image_info(data)


# --------------------------------------------------------------------------------------
# the document builder
# --------------------------------------------------------------------------------------
class _Toc(object):
    def __init__(self, title, levels, fallback):
        self.title, self.levels, self.fallback = title, levels, fallback


PAGES = {"letter": (12240, 15840), "a4": (11906, 16838)}     # twips


class Docx(object):
    """Collects paragraphs, tables and images, then writes the package.

    Set `number_headings=True` to prefix headings with 1, 1.1, 1.1.1."""

    def __init__(self, title="", author="", subject="", keywords="", description="",
                 header_left="", header_right="", footer_left="", page_numbers=True,
                 page="letter", landscape=False, margins_in=1.0, font="Calibri", font_pt=10.5,
                 number_headings=False, update_fields=True, created=None, language="en-US"):
        self.title, self.author, self.subject = title, author, subject
        self.keywords, self.description = keywords, description
        self.header_left, self.header_right = header_left, header_right
        self.footer_left, self.page_numbers = footer_left, page_numbers
        pw, ph = PAGES.get(str(page).lower(), PAGES["letter"])
        if landscape:
            pw, ph = ph, pw
        self.page_w, self.page_h = pw, ph
        self.landscape = bool(landscape)
        self.margin = int(round(margins_in * TWIPS_PER_IN))
        self.font, self.font_pt = font, font_pt
        self.number_headings = number_headings
        self.update_fields = update_fields
        self.created = created or datetime.datetime.now(datetime.timezone.utc).replace(
            tzinfo=None, microsecond=0)
        self.language = language
        self.body = []
        self._media = []                    # [(rid, name, bytes, ext)]
        self._media_by_hash = {}
        self._rid = 100
        self._docpr = 0
        self._bm = 0
        self._headings = []                 # (level, text, bookmark name)
        self._hcount = [0, 0, 0]
        self._caption_n = {}
        self._nums = []                     # numbered-list instances -> numId = 2 + index
        self._cover = False

    # -- geometry -----------------------------------------------------------------
    @property
    def text_width_twips(self):
        return self.page_w - 2 * self.margin

    @property
    def text_width_in(self):
        return self.text_width_twips / float(TWIPS_PER_IN)

    @property
    def text_height_in(self):
        return (self.page_h - 2 * self.margin) / float(TWIPS_PER_IN)

    # -- marks (a section can be dropped if it turned out to be empty) -----------------
    def mark(self):
        return (len(self.body), len(self._headings), list(self._hcount),
                dict(self._caption_n), len(self._nums))

    def rollback(self, mark):
        n, nh, hc, cap, nn = mark
        del self.body[n:]
        del self._headings[nh:]
        self._hcount = list(hc)
        self._caption_n = dict(cap)
        del self._nums[nn:]

    def is_empty_since(self, mark):
        return len(self.body) <= mark[0] + (1 if len(self._headings) > mark[1] else 0)

    # -- blocks ---------------------------------------------------------------------
    def heading(self, text, level=1, page_break_before=False):
        """Heading 1-3; returns the text as written (with its number when numbering)."""
        level = max(1, min(3, int(level)))
        text = clean_text(text)
        if self.number_headings:
            self._hcount[level - 1] += 1
            for k in range(level, 3):
                self._hcount[k] = 0
            text = "{} {}".format(".".join(str(c) for c in self._hcount[:level]), text)
        self._bm += 1
        name = "_Toc{:09d}".format(100000000 + self._bm)
        self._headings.append((level, text, name))
        self.body.append(para_xml(
            '<w:bookmarkStart w:id="{0}" w:name="{1}"/>{2}<w:bookmarkEnd w:id="{0}"/>'.format(
                self._bm, name, run_xml(text)),
            style="Heading{}".format(level), page_break_before=page_break_before))
        return text

    def paragraph(self, content="", style=None, align=None, bold=False, italic=False,
                  size=None, color=None, keep_next=False, keep_lines=False, markup=False,
                  space_before=None, space_after=None, shade=None, border_left=None,
                  border_bottom=None, page_break_before=False):
        """`content`: str, or a list of runs. markup=True reads **bold**, *italic*, `code`."""
        if markup and isinstance(content, str):
            content = parse_inline(content)
        fmt = {}
        if bold:
            fmt["b"] = True
        if italic:
            fmt["i"] = True
        if size:
            fmt["size"] = size
        if color:
            fmt["color"] = color
        self.body.append(para_xml(runs_xml(content, **fmt), style=style, align=align,
                                  keep_next=keep_next, keep_lines=keep_lines,
                                  space_before=space_before, space_after=space_after,
                                  shade=shade, border_left=border_left,
                                  border_bottom=border_bottom,
                                  page_break_before=page_break_before))

    def callout(self, content, kind="note", markup=False):
        """A shaded paragraph with a coloured left rule (kind: note or warn)."""
        warn = kind == "warn"
        self.paragraph(content, markup=markup, shade=SHADE_WARN if warn else "EEF3FA",
                       border_left="E0A000" if warn else "5B84C4", keep_lines=True,
                       space_before=60, space_after=120)

    def horizontal_rule(self):
        self.body.append(para_xml("", border_bottom="BFBFBF", space_after=120))

    def page_break(self):
        self.body.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')

    def code_block(self, text):
        """Preformatted lines in one shaded paragraph."""
        self.body.append(para_xml(run_xml(clean_text(text).rstrip("\n"), code=True),
                                  style="CodeBlock"))

    def _new_num(self):
        self._nums.append(1)
        return 2 + len(self._nums) - 1

    def bullets(self, items, ordered=False, level=0, markup=False, shade=None):
        """Bullet (or numbered) list. An item is a string, a list of runs, or a tuple
        (item, [children]) for a nested list."""
        num_id = self._new_num() if ordered else 1
        self._list(items, num_id, level, markup, shade)

    def _list(self, items, num_id, level, markup, shade):
        for it in items:
            children = None
            if isinstance(it, tuple):
                it, children = it
            content = parse_inline(it) if (markup and isinstance(it, str)) else it
            self.body.append(para_xml(
                runs_xml(content), style="ListParagraph", num=(num_id, min(level, 8)),
                shade=shade, border_left="E0A000" if shade else None))
            if children:
                self._list(children, num_id, level + 1, markup, shade)

    def list_item(self, content, level=0, num_id=1, markup=False, shade=None):
        if markup and isinstance(content, str):
            content = parse_inline(content)
        self.body.append(para_xml(runs_xml(content), style="ListParagraph",
                                  num=(num_id, min(level, 8)), shade=shade,
                                  border_left="E0A000" if shade else None))

    def new_numbered_list(self):
        return self._new_num()

    # -- captions ---------------------------------------------------------------------
    def caption(self, kind, text, above=False):
        """'Figure 1. text' with a SEQ field so Word can renumber it."""
        n = self._caption_n.get(kind, 0) + 1
        self._caption_n[kind] = n
        content = (run_xml(kind + " ") + _field_xml("SEQ {} \\* ARABIC".format(kind), str(n))
                   + run_xml(". " + clean_text(text)))
        self.body.append(para_xml(content, style="Caption", keep_next=above,
                                  align="left" if above else "center"))
        return n

    # -- tables -----------------------------------------------------------------------
    @staticmethod
    def _norm_cell(c, markup):
        spec = {"runs": [], "shade": None, "align": None, "bold": False}
        if isinstance(c, dict):
            spec["shade"], spec["align"] = c.get("shade"), c.get("align")
            spec["bold"] = bool(c.get("bold"))
            c = c.get("runs", c.get("text", ""))
        if c is None:
            c = ""
        if not isinstance(c, (str, list, tuple)):
            c = str(c)
        if isinstance(c, str):
            spec["runs"] = parse_inline(c) if markup else [{"text": c}]
        else:
            spec["runs"] = list(c)
        return spec

    def table(self, rows, header=True, widths=None, align=None, font_pt=None,
              shade=SHADE_HEADER, borders=True, caption=None, markup=False,
              first_col_bold=False, first_col_shade=None, zebra=False, total_width_in=None,
              header_rows=1):
        """`rows`: list of lists; a cell is a string, a list of runs, or a dict
        {text|runs, shade, align, bold}. `widths`: relative column weights (they are
        scaled to the text width), or None to size the columns from their content.
        `align`: per column 'left'|'center'|'right'."""
        if not rows:
            return
        cells = [[self._norm_cell(c, markup) for c in r] for r in rows]
        ncols = max(len(r) for r in cells)
        for r in cells:
            r.extend(self._norm_cell("", False) for _ in range(ncols - len(r)))
        total = int(round((total_width_in or self.text_width_in) * TWIPS_PER_IN))
        if font_pt is None:
            font_pt = 9.0 if ncols <= 6 else (8.0 if ncols <= 9 else 7.0)
        if widths:
            w = [float(x) for x in widths][:ncols]
            w += [1.0] * (ncols - len(w))
            tw = _scale_widths(w, total)
        else:
            tw = _scale_widths(_auto_widths(cells, ncols, total / float(TWIPS_PER_IN), font_pt,
                                            header), total)
        if caption:
            self.caption("Table", caption, above=True)
        x = ['<w:tbl><w:tblPr><w:tblStyle w:val="ReportTable"/>'
             '<w:tblW w:w="{}" w:type="dxa"/>'.format(total)]
        edge = ('w:val="single" w:sz="4" w:space="0" w:color="A6A6A6"' if borders
                else 'w:val="nil"')
        x.append("<w:tblBorders>{}</w:tblBorders>".format("".join(
            "<w:{} {}/>".format(s, edge) for s in ("top", "left", "bottom", "right",
                                                   "insideH", "insideV"))))
        x.append('<w:tblLayout w:type="fixed"/><w:tblCellMar><w:top w:w="30" w:type="dxa"/>'
                 '<w:left w:w="80" w:type="dxa"/><w:bottom w:w="30" w:type="dxa"/>'
                 '<w:right w:w="80" w:type="dxa"/></w:tblCellMar>'
                 '<w:tblLook w:val="04A0" w:firstRow="1" w:lastRow="0" w:firstColumn="1" '
                 'w:lastColumn="0" w:noHBand="0" w:noVBand="1"/></w:tblPr>')
        x.append("<w:tblGrid>{}</w:tblGrid>".format(
            "".join('<w:gridCol w:w="{}"/>'.format(t) for t in tw)))
        aligns = list(align or []) + [None] * ncols
        for ri, row in enumerate(cells):
            is_head = bool(header) and ri < header_rows
            trpr = "<w:cantSplit/>" + ("<w:tblHeader/>" if is_head else "")
            x.append("<w:tr><w:trPr>{}</w:trPr>".format(trpr))
            for ci, c in enumerate(row):
                shd = c["shade"]
                if shd is None:
                    if is_head and shade:
                        shd = shade
                    elif ci == 0 and first_col_shade and not is_head:
                        shd = first_col_shade
                    elif zebra and not is_head and (ri % 2 == 0):
                        shd = "F7F7F7"
                tcpr = '<w:tcW w:w="{}" w:type="dxa"/>'.format(tw[ci])
                if shd:
                    tcpr += '<w:shd w:val="clear" w:color="auto" w:fill="{}"/>'.format(shd)
                al = c["align"] or ("center" if is_head and aligns[ci] == "right" else aligns[ci])
                bold = is_head or c["bold"] or (first_col_bold and ci == 0)
                x.append("<w:tc><w:tcPr>{}</w:tcPr>{}</w:tc>".format(
                    tcpr, para_xml(runs_xml(c["runs"], size=font_pt, **({"b": True} if bold
                                                                          else {})),
                                   style="TableText", align=al, keep_next=is_head)))
            x.append("</w:tr>")
        x.append("</w:tbl>")
        self.body.append("".join(x))
        self.body.append(para_xml("", space_after=60, line=120))     # keeps tables apart

    # -- images -----------------------------------------------------------------------
    def _add_media(self, data, info):
        key = hashlib.sha1(data).hexdigest()
        if key in self._media_by_hash:
            return self._media_by_hash[key]
        self._rid += 1
        rid = "rId{}".format(self._rid)
        ext = "png" if info[0] == "png" else "jpeg"
        name = "image{}.{}".format(len(self._media) + 1, ext)
        self._media.append((rid, name, data, ext))
        self._media_by_hash[key] = (rid, name)
        return rid, name

    def _size_in(self, info, width_in, height_in, max_width_in, max_height_in):
        _f, wpx, hpx, dx, dy = info
        nat_w = wpx / _sane_dpi(dx)
        nat_h = hpx / _sane_dpi(dy)
        if width_in and height_in:
            w, h = float(width_in), float(height_in)
        elif width_in:
            w = float(width_in)
            h = w * hpx / float(wpx)
        elif height_in:
            h = float(height_in)
            w = h * wpx / float(hpx)
        else:
            w, h = nat_w, nat_h
        mw = max_width_in or self.text_width_in
        mh = max_height_in or (self.text_height_in - 1.0)
        f = min(1.0, mw / w, mh / h)
        return w * f, h * f

    def image_run(self, source, width_in=None, height_in=None, max_width_in=None,
                  max_height_in=None, alt=""):
        """Inline picture run XML and its size in inches: (xml, w_in, h_in)."""
        data, info = read_image(source)
        rid, name = self._add_media(data, info)
        w_in, h_in = self._size_in(info, width_in, height_in, max_width_in, max_height_in)
        cx, cy = int(round(w_in * EMU_PER_IN)), int(round(h_in * EMU_PER_IN))
        self._docpr += 1
        pid = self._docpr
        fname = xesc(os.path.basename(source) if isinstance(source, str) else name)
        xml = ('<w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0">'
               '<wp:extent cx="{cx}" cy="{cy}"/><wp:effectExtent l="0" t="0" r="0" b="0"/>'
               '<wp:docPr id="{pid}" name="Picture {pid}" descr="{alt}"/>'
               '<wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/>'
               '</wp:cNvGraphicFramePr>'
               '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/'
               '2006/picture"><pic:pic><pic:nvPicPr><pic:cNvPr id="{pid}" name="{fname}" '
               'descr="{alt}"/><pic:cNvPicPr/></pic:nvPicPr><pic:blipFill>'
               '<a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
               '<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
               '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic>'
               '</a:graphicData></a:graphic></wp:inline></w:drawing></w:r>').format(
                   cx=cx, cy=cy, pid=pid, alt=xesc(alt), fname=fname, rid=rid)
        return xml, w_in, h_in

    def image(self, source, width_in=None, height_in=None, max_width_in=None,
              max_height_in=None, alt="", caption=None, align="center"):
        """A picture in its own paragraph (PNG or JPEG file or bytes). Without a size
        it is drawn at its own resolution (96 dpi when the file has none), shrunk to
        fit the page. Returns (width_in, height_in). Raises ValueError for another
        format or a damaged file."""
        xml, w_in, h_in = self.image_run(source, width_in, height_in, max_width_in,
                                         max_height_in, alt)
        self.body.append(para_xml(xml, align=align, keep_next=bool(caption), space_after=60))
        if caption:
            self.caption("Figure", caption)
        return w_in, h_in

    # -- cover page and contents --------------------------------------------------------
    def title_block(self, title, subtitle="", info=None, logo=None, note=""):
        """Cover: optional logo, title, subtitle, a borderless label/value table and a
        note, then a page break. The header and footer are left off this page."""
        self._cover = True
        if logo:
            try:
                xml, _w, _h = self.image_run(logo, max_width_in=2.6, max_height_in=1.0,
                                             alt="Logo")
                self.body.append(para_xml(xml, space_after=240))
            except (ValueError, OSError):
                logo = None
        self.body.append(para_xml(run_xml(title), style="Title",
                                  space_before=0 if logo else 1800))
        if subtitle:
            self.body.append(para_xml(run_xml(subtitle), style="Subtitle"))
        self.horizontal_rule()
        rows = [[{"text": k, "bold": True}, v] for k, v in (info or []) if v not in (None, "")]
        if rows:
            self.table(rows, header=False, widths=[1.3, 4.7], borders=False, font_pt=10.5)
        if note:
            self.paragraph(note, italic=True, size=9, color="595959", space_before=240)
        self.page_break()

    def toc(self, title="Contents", levels="1-3", fallback=True):
        """A TOC field. Word fills it (with page numbers) on open; `fallback` writes the
        headings as a plain list into the field's result so any other viewer shows them."""
        self.body.append(para_xml(run_xml(title), style="TOCHeading"))
        self.body.append(_Toc(title, levels, fallback))

    def _toc_xml(self, toc):
        lo, hi = 1, 3
        m = re.match(r"(\d)-(\d)", toc.levels)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
        heads = [h for h in self._headings if lo <= h[0] <= hi]
        instr = '<w:r><w:instrText xml:space="preserve"> TOC \\o "{}-{}" \\h \\z \\u </w:instrText></w:r>'.format(lo, hi)
        begin = '<w:r><w:fldChar w:fldCharType="begin"/></w:r>' + instr + \
            '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        end = '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
        if not (toc.fallback and heads):
            note = run_xml("Right-click here and choose Update Field to build the table of "
                           "contents.", i=True, color="7F7F7F")
            return para_xml(begin + note + end, style="TOC1")
        out = []
        for k, (lvl, text, bm) in enumerate(heads):
            link = '<w:hyperlink w:anchor="{}" w:history="1">{}</w:hyperlink>'.format(
                bm, run_xml(text))
            out.append(para_xml((begin if k == 0 else "") + link, style="TOC{}".format(lvl)))
        out.append(para_xml(end, style="TOC1", space_after=0))
        return "".join(out)

    # -- package ------------------------------------------------------------------------
    def _render_body(self):
        return "".join(self._toc_xml(b) if isinstance(b, _Toc) else b for b in self.body)

    def _styles_xml(self):
        f, hp = self.font, int(round(self.font_pt * 2))
        tw = self.text_width_twips
        fonts = '<w:rFonts w:ascii="{0}" w:hAnsi="{0}" w:eastAsia="{0}" w:cs="{0}"/>'.format(f)

        def para_style(sid, name, based="Normal", nxt=None, ppr="", rpr="", ui=None, q=True,
                       link=None):
            return ('<w:style w:type="paragraph" w:styleId="{}"><w:name w:val="{}"/>{}{}{}{}'
                    '{}{}{}</w:style>'.format(
                        sid, name,
                        '<w:basedOn w:val="{}"/>'.format(based) if based else "",
                        '<w:next w:val="{}"/>'.format(nxt) if nxt else "",
                        '<w:uiPriority w:val="{}"/>'.format(ui) if ui is not None else "",
                        "<w:qFormat/>" if q else "",
                        "", "<w:pPr>{}</w:pPr>".format(ppr) if ppr else "",
                        "<w:rPr>{}</w:rPr>".format(rpr) if rpr else ""))

        def hd(n, sz, before, after):
            return para_style(
                "Heading{}".format(n), "heading {}".format(n), nxt="Normal", ui=9,
                ppr='<w:keepNext/><w:keepLines/>{}<w:spacing w:before="{}" w:after="{}"/>'
                    '<w:outlineLvl w:val="{}"/>'.format(
                        '<w:pBdr><w:bottom w:val="single" w:sz="6" w:space="2" '
                        'w:color="BFBFBF"/></w:pBdr>' if n == 1 else "", before, after, n - 1),
                rpr='<w:b/><w:bCs/><w:color w:val="{}"/><w:sz w:val="{}"/><w:szCs w:val="{}"/>'
                    .format(COLOR_HEAD, sz, sz))

        toc_tab = [('<w:tabs><w:tab w:val="right" w:leader="dot" w:pos="{}"/></w:tabs>'
                    '<w:spacing w:after="40"/><w:ind w:left="{}"/>').format(tw, k * 240)
                   for k in range(3)]
        s = [_XML_HEAD, '<w:styles xmlns:w="{}">'.format(W_NS),
             '<w:docDefaults><w:rPrDefault><w:rPr>{}<w:sz w:val="{}"/><w:szCs w:val="{}"/>'
             '<w:lang w:val="{}" w:eastAsia="en-US" w:bidi="ar-SA"/></w:rPr></w:rPrDefault>'
             '<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="264" w:lineRule="auto"/>'
             '</w:pPr></w:pPrDefault></w:docDefaults>'.format(fonts, hp, hp, self.language),
             '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name '
             'w:val="Normal"/><w:qFormat/></w:style>',
             '<w:style w:type="character" w:default="1" w:styleId="DefaultParagraphFont">'
             '<w:name w:val="Default Paragraph Font"/><w:uiPriority w:val="1"/><w:semiHidden/>'
             '</w:style>',
             '<w:style w:type="table" w:default="1" w:styleId="TableNormal"><w:name '
             'w:val="Normal Table"/><w:uiPriority w:val="99"/><w:semiHidden/><w:tblPr>'
             '<w:tblInd w:w="0" w:type="dxa"/><w:tblCellMar><w:top w:w="0" w:type="dxa"/>'
             '<w:left w:w="108" w:type="dxa"/><w:bottom w:w="0" w:type="dxa"/>'
             '<w:right w:w="108" w:type="dxa"/></w:tblCellMar></w:tblPr></w:style>',
             '<w:style w:type="numbering" w:default="1" w:styleId="NoList"><w:name '
             'w:val="No List"/><w:uiPriority w:val="99"/><w:semiHidden/></w:style>',
             hd(1, 32, 360, 120), hd(2, 26, 280, 100), hd(3, 23, 200, 80),
             para_style("Title", "Title", nxt="Normal", ui=10,
                        ppr='<w:spacing w:before="0" w:after="120" w:line="240" '
                            'w:lineRule="auto"/>',
                        rpr='<w:b/><w:bCs/><w:color w:val="{}"/><w:sz w:val="56"/>'
                            '<w:szCs w:val="56"/>'.format(COLOR_HEAD)),
             para_style("Subtitle", "Subtitle", nxt="Normal", ui=11,
                        ppr='<w:spacing w:after="240"/>',
                        rpr='<w:color w:val="595959"/><w:sz w:val="32"/><w:szCs w:val="32"/>'),
             para_style("Caption", "caption", nxt="Normal", ui=35,
                        ppr='<w:keepLines/><w:spacing w:before="60" w:after="200" '
                            'w:line="240" w:lineRule="auto"/>',
                        rpr='<w:i/><w:iCs/><w:color w:val="404040"/><w:sz w:val="18"/>'
                            '<w:szCs w:val="18"/>'),
             para_style("ListParagraph", "List Paragraph", ui=34,
                        ppr='<w:spacing w:after="40"/>'),
             para_style("Header", "header", q=False, ui=99,
                        ppr='<w:pBdr><w:bottom w:val="single" w:sz="4" w:space="4" '
                            'w:color="A6A6A6"/></w:pBdr><w:tabs><w:tab w:val="right" '
                            'w:pos="{}"/></w:tabs><w:spacing w:after="0" w:line="240" '
                            'w:lineRule="auto"/>'.format(tw),
                        rpr='<w:color w:val="595959"/><w:sz w:val="18"/><w:szCs w:val="18"/>'),
             para_style("Footer", "footer", q=False, ui=99,
                        ppr='<w:pBdr><w:top w:val="single" w:sz="4" w:space="4" '
                            'w:color="A6A6A6"/></w:pBdr><w:tabs><w:tab w:val="right" '
                            'w:pos="{}"/></w:tabs><w:spacing w:after="0" w:line="240" '
                            'w:lineRule="auto"/>'.format(tw),
                        rpr='<w:color w:val="595959"/><w:sz w:val="18"/><w:szCs w:val="18"/>'),
             para_style("TOCHeading", "TOC Heading", nxt="Normal", ui=39,
                        ppr='<w:keepNext/><w:spacing w:before="0" w:after="160"/>',
                        rpr='<w:b/><w:bCs/><w:color w:val="{}"/><w:sz w:val="32"/>'
                            '<w:szCs w:val="32"/>'.format(COLOR_HEAD)),
             para_style("TOC1", "toc 1", nxt="Normal", ui=39, q=False, ppr=toc_tab[0],
                        rpr="<w:b/><w:bCs/>"),
             para_style("TOC2", "toc 2", nxt="Normal", ui=39, q=False, ppr=toc_tab[1]),
             para_style("TOC3", "toc 3", nxt="Normal", ui=39, q=False, ppr=toc_tab[2],
                        rpr='<w:sz w:val="19"/><w:szCs w:val="19"/>'),
             para_style("TableText", "Table Text", q=False,
                        ppr='<w:spacing w:before="0" w:after="0" w:line="240" '
                            'w:lineRule="auto"/>',
                        rpr='<w:sz w:val="18"/><w:szCs w:val="18"/>'),
             para_style("CodeBlock", "Code Block", q=False,
                        ppr='<w:keepLines/><w:pBdr><w:left w:val="single" w:sz="12" '
                            'w:space="6" w:color="BFBFBF"/></w:pBdr><w:shd w:val="clear" '
                            'w:color="auto" w:fill="F5F5F5"/><w:spacing w:after="120" '
                            'w:line="240" w:lineRule="auto"/>',
                        rpr='<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:cs="Consolas"/>'
                            '<w:sz w:val="18"/><w:szCs w:val="18"/>'),
             '<w:style w:type="character" w:styleId="Code"><w:name w:val="Code"/>'
             '<w:uiPriority w:val="1"/><w:qFormat/><w:rPr><w:rFonts w:ascii="Consolas" '
             'w:hAnsi="Consolas" w:cs="Consolas"/><w:sz w:val="19"/><w:szCs w:val="19"/>'
             '<w:shd w:val="clear" w:color="auto" w:fill="F2F2F2"/></w:rPr></w:style>',
             '<w:style w:type="character" w:styleId="Hyperlink"><w:name w:val="Hyperlink"/>'
             '<w:uiPriority w:val="99"/><w:rPr><w:color w:val="0563C1"/><w:u w:val="single"/>'
             '</w:rPr></w:style>',
             '<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/>'
             '<w:basedOn w:val="TableNormal"/><w:uiPriority w:val="39"/><w:tblPr><w:tblBorders>'
             + "".join('<w:{} w:val="single" w:sz="4" w:space="0" w:color="auto"/>'.format(b)
                       for b in ("top", "left", "bottom", "right", "insideH", "insideV"))
             + '</w:tblBorders></w:tblPr></w:style>',
             '<w:style w:type="table" w:styleId="ReportTable"><w:name w:val="Report Table"/>'
             '<w:basedOn w:val="TableNormal"/><w:uiPriority w:val="40"/><w:tblPr><w:tblBorders>'
             + "".join('<w:{} w:val="single" w:sz="4" w:space="0" w:color="A6A6A6"/>'.format(b)
                       for b in ("top", "left", "bottom", "right", "insideH", "insideV"))
             + '</w:tblBorders></w:tblPr></w:style>',
             "</w:styles>"]
        return "".join(s)

    def _numbering_xml(self):
        def lvl(i, fmt, text, left, font=None):
            rp = ""
            if font:
                rp = '<w:rPr><w:rFonts w:ascii="{0}" w:hAnsi="{0}" w:hint="default"/></w:rPr>'\
                    .format(font)
            return ('<w:lvl w:ilvl="{}"><w:start w:val="1"/><w:numFmt w:val="{}"/>'
                    '<w:lvlText w:val="{}"/><w:lvlJc w:val="left"/><w:pPr><w:ind w:left="{}" '
                    'w:hanging="270"/></w:pPr>{}</w:lvl>'.format(i, fmt, text, left, rp))
        marks = [u"•", u"–", u"▪"]
        bul = "".join(lvl(i, "bullet", marks[i % 3], 450 + 360 * i, self.font)
                      for i in range(9))
        dec = "".join(lvl(i, ("decimal", "lowerLetter", "lowerRoman")[i % 3],
                          "%{}.".format(i + 1), 450 + 360 * i) for i in range(9))
        out = [_XML_HEAD, '<w:numbering xmlns:w="{}">'.format(W_NS),
               '<w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="hybridMultilevel"/>'
               '{}</w:abstractNum>'.format(bul),
               '<w:abstractNum w:abstractNumId="1"><w:multiLevelType w:val="hybridMultilevel"/>'
               '{}</w:abstractNum>'.format(dec),
               '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>']
        for k in range(len(self._nums)):
            out.append('<w:num w:numId="{}"><w:abstractNumId w:val="1"/><w:lvlOverride '
                       'w:ilvl="0"><w:startOverride w:val="1"/></w:lvlOverride></w:num>'.format(
                           2 + k))
        out.append("</w:numbering>")
        return "".join(out)

    def _settings_xml(self):
        return (_XML_HEAD + '<w:settings xmlns:w="{}"><w:zoom w:percent="100"/>'
                '<w:defaultTabStop w:val="720"/><w:characterSpacingControl '
                'w:val="doNotCompress"/>{}<w:compat><w:compatSetting '
                'w:name="compatibilityMode" w:uri="http://schemas.microsoft.com/office/word" '
                'w:val="15"/></w:compat></w:settings>').format(
                    W_NS, '<w:updateFields w:val="true"/>' if self.update_fields else "")

    def _hf_xml(self, kind, left, right, fields):
        style = "Header" if kind == "hdr" else "Footer"
        tag = "w:hdr" if kind == "hdr" else "w:ftr"
        content = run_xml(left)
        if right or fields:
            content += "<w:r><w:tab/></w:r>" + run_xml(right)
            if fields:
                content += (run_xml("Page ") + _field_xml("PAGE", "1") + run_xml(" of ")
                            + _field_xml("NUMPAGES", "1"))
        if not (left or right or fields):
            return _XML_HEAD + "<{0} {1}><w:p/></{0}>".format(tag, _NS_DECL)
        return _XML_HEAD + "<{0} {1}>{2}</{0}>".format(tag, _NS_DECL, para_xml(content,
                                                                               style=style))

    def _core_xml(self):
        def stamp(d):
            return d.strftime("%Y-%m-%dT%H:%M:%SZ")
        return (_XML_HEAD + '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/'
                'package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/'
                '1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:dcmitype="http://purl.org/'
                'dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
                '<dc:title>{}</dc:title><dc:subject>{}</dc:subject><dc:creator>{}</dc:creator>'
                '<cp:keywords>{}</cp:keywords><dc:description>{}</dc:description>'
                '<cp:lastModifiedBy></cp:lastModifiedBy>'
                '<dcterms:created xsi:type="dcterms:W3CDTF">{}</dcterms:created>'
                '<dcterms:modified xsi:type="dcterms:W3CDTF">{}</dcterms:modified>'
                '</cp:coreProperties>').format(
                    xesc(self.title), xesc(self.subject), xesc(self.author),
                    xesc(self.keywords), xesc(self.description), stamp(self.created),
                    stamp(self.created))

    def to_bytes(self):
        import io
        body = self._render_body()
        used = set(re.findall(r'r:embed="(rId\d+)"', body))
        media = [m for m in self._media if m[0] in used]
        has_hdr = bool(self.header_left or self.header_right)
        has_ftr = bool(self.footer_left or self.page_numbers)
        cover = self._cover and (has_hdr or has_ftr)
        rels = [("rId1", "styles", "styles.xml"), ("rId2", "numbering", "numbering.xml"),
                ("rId3", "settings", "settings.xml")]
        parts = {}
        sect = ""
        if has_hdr:
            rels.append(("rId4", "header", "header1.xml"))
            parts["word/header1.xml"] = self._hf_xml("hdr", self.header_left,
                                                     self.header_right, False)
            sect += '<w:headerReference w:type="default" r:id="rId4"/>'
        if has_ftr:
            rels.append(("rId5", "footer", "footer1.xml"))
            parts["word/footer1.xml"] = self._hf_xml("ftr", self.footer_left, "",
                                                     self.page_numbers)
            sect += '<w:footerReference w:type="default" r:id="rId5"/>'
        if cover:
            if has_hdr:
                rels.append(("rId6", "header", "header2.xml"))
                parts["word/header2.xml"] = self._hf_xml("hdr", "", "", False)
                sect += '<w:headerReference w:type="first" r:id="rId6"/>'
            if has_ftr:
                rels.append(("rId7", "footer", "footer2.xml"))
                parts["word/footer2.xml"] = self._hf_xml("ftr", "", "", False)
                sect += '<w:footerReference w:type="first" r:id="rId7"/>'
        sect += ('<w:pgSz w:w="{}" w:h="{}"{}/><w:pgMar w:top="{m}" w:right="{m}" w:bottom="{m}" '
                 'w:left="{m}" w:header="{h}" w:footer="{h}" w:gutter="0"/><w:cols '
                 'w:space="720"/>{}').format(
                     self.page_w, self.page_h,
                     ' w:orient="landscape"' if self.landscape else "",
                     "<w:titlePg/>" if cover else "", m=self.margin,
                     h=int(self.margin * 0.5))
        parts["word/document.xml"] = (
            _XML_HEAD + "<w:document {}><w:body>{}<w:sectPr>{}</w:sectPr></w:body>"
            "</w:document>".format(_NS_DECL, body, sect))
        parts["word/styles.xml"] = self._styles_xml()
        parts["word/numbering.xml"] = self._numbering_xml()
        parts["word/settings.xml"] = self._settings_xml()
        parts["docProps/core.xml"] = self._core_xml()
        parts["docProps/app.xml"] = (
            _XML_HEAD + '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/'
            '2006/extended-properties"><Application>ND Dam Breach Runner</Application>'
            '</Properties>')
        rel_xml = "".join(
            '<Relationship Id="{}" Type="{}/{}" Target="{}"/>'.format(rid, R_NS, typ, tgt)
            for rid, typ, tgt in rels)
        rel_xml += "".join(
            '<Relationship Id="{}" Type="{}/image" Target="media/{}"/>'.format(rid, R_NS, name)
            for rid, name, _d, _e in media)
        parts["word/_rels/document.xml.rels"] = (
            _XML_HEAD + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            'relationships">{}</Relationships>'.format(rel_xml))
        parts["_rels/.rels"] = (
            _XML_HEAD + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            'relationships"><Relationship Id="rId1" Type="{0}/officeDocument" '
            'Target="word/document.xml"/><Relationship Id="rId2" Type="http://schemas.'
            'openxmlformats.org/package/2006/relationships/metadata/core-properties" '
            'Target="docProps/core.xml"/><Relationship Id="rId3" Type="{0}/extended-properties" '
            'Target="docProps/app.xml"/></Relationships>'.format(R_NS))
        wml = "application/vnd.openxmlformats-officedocument.wordprocessingml"
        over = [("/word/document.xml", wml + ".document.main+xml"),
                ("/word/styles.xml", wml + ".styles+xml"),
                ("/word/numbering.xml", wml + ".numbering+xml"),
                ("/word/settings.xml", wml + ".settings+xml"),
                ("/docProps/core.xml", "application/vnd.openxmlformats-package.core-properties"
                                       "+xml"),
                ("/docProps/app.xml", "application/vnd.openxmlformats-officedocument."
                                      "extended-properties+xml")]
        for name in sorted(parts):
            if "header" in name:
                over.append(("/" + name, wml + ".header+xml"))
            elif "footer" in name:
                over.append(("/" + name, wml + ".footer+xml"))
        parts["[Content_Types].xml"] = (
            _XML_HEAD + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
            'content-types"><Default Extension="rels" ContentType="application/vnd.'
            'openxmlformats-package.relationships+xml"/><Default Extension="xml" '
            'ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/>'
            '<Default Extension="jpeg" ContentType="image/jpeg"/><Default Extension="jpg" '
            'ContentType="image/jpeg"/>{}</Types>'.format("".join(
                '<Override PartName="{}" ContentType="{}"/>'.format(n, t) for n, t in over)))
        buf = io.BytesIO()
        dt = self.created.timetuple()[:6]
        if dt[0] < 1980:
            dt = (1980, 1, 1, 0, 0, 0)
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            order = ["[Content_Types].xml", "_rels/.rels", "word/document.xml"] + sorted(
                n for n in parts if n not in ("[Content_Types].xml", "_rels/.rels",
                                              "word/document.xml"))
            for name in order:
                zi = zipfile.ZipInfo(name, dt)
                zi.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(zi, parts[name].encode("utf-8"))
            for _rid, name, data, _ext in media:
                zi = zipfile.ZipInfo("word/media/" + name, dt)
                zi.compress_type = zipfile.ZIP_STORED
                z.writestr(zi, data)
        return buf.getvalue()

    def save(self, path):
        data = self.to_bytes()
        d = os.path.dirname(os.path.abspath(path))
        if d and not os.path.isdir(d):
            os.makedirs(d)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return path


# --------------------------------------------------------------------------------------
# table column widths
# --------------------------------------------------------------------------------------
def _cell_text_len(c):
    return sum(len(clean_text(r.get("text", "") if isinstance(r, dict) else r))
               for r in c["runs"])


def _auto_widths(cells, ncols, total_in, font_pt, header):
    """Column widths (inches, unscaled) from the content: enough for the longest word,
    up to about the 90th-percentile cell on one line."""
    cw = font_pt * 0.0078            # average character width, inches
    pad = 0.16
    mins, wants = [], []
    for j in range(ncols):
        lens, longest = [], 0
        for r in cells:
            t = "".join(x.get("text", "") if isinstance(x, dict) else x for x in r[j]["runs"])
            lens.append(len(t))
            for word in re.split(r"[\s/]+", t):
                longest = max(longest, len(word))
        lens.sort()
        p90 = lens[min(len(lens) - 1, int(0.9 * len(lens)))] if lens else 4
        mins.append(min(longest, 28) * cw + pad)
        wants.append(max(min(p90, 60), 4) * cw + pad)
    if sum(wants) <= total_in:
        return wants
    if sum(mins) >= total_in:
        return mins
    room = total_in - sum(mins)
    extra = [max(w - m, 0.0) for w, m in zip(wants, mins)]
    tot = sum(extra) or 1.0
    return [m + room * e / tot for m, e in zip(mins, extra)]


def _scale_widths(w, total_twips):
    s = float(sum(w)) or 1.0
    tw = [max(300, int(round(x / s * total_twips))) for x in w]
    tw[-1] += total_twips - sum(tw)
    if tw[-1] < 300:
        tw = [int(round(x / float(sum(tw)) * total_twips)) for x in tw]
    return tw


# --------------------------------------------------------------------------------------
# inspecting a finished file (used by tests and to report what was written)
# --------------------------------------------------------------------------------------
def inspect_docx(path):
    """{'parts', 'headings': [(level, text)], 'tables', 'images', 'paragraphs', 'text'}
    read back from a .docx with the standard library only."""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        root = ET.fromstring(z.read("word/document.xml"))
    ns = {"w": W_NS}
    W = "{%s}" % W_NS
    heads, text, npara = [], [], 0
    for p in root.iter(W + "p"):
        npara += 1
        st = p.find("w:pPr/w:pStyle", ns)
        t = "".join((e.text or "") if e.tag == W + "t" else ("\t" if e.tag == W + "tab" else "")
                    for e in p.iter() if e.tag in (W + "t", W + "tab"))
        text.append(t)
        sid = st.get(W + "val") if st is not None else ""
        if sid.startswith("Heading") and sid[7:].isdigit():
            heads.append((int(sid[7:]), t))
    return {"parts": names, "headings": heads, "tables": len(list(root.iter(W + "tbl"))),
            "images": len(list(root.iter(W + "drawing"))), "paragraphs": npara,
            "text": "\n".join(text)}


# --------------------------------------------------------------------------------------
# markdown subset -> Docx
# --------------------------------------------------------------------------------------
_HEAD_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET_RE = re.compile(r"^(\s*)([-*+])\s+(.*)$")
_NUM_RE = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_HR_RE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_SEP_CELL = re.compile(r"^:?-{1,}:?$")
_NUMERIC_CELL = re.compile(r"^[\s$~<>=+\-−]*[\d,]+(\.\d+)?\s*(%|[a-zA-Z/²³°]{0,6})?$")


def _split_row(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    cells, cur, k = [], [], 0
    while k < len(s):
        ch = s[k]
        if ch == "\\" and k + 1 < len(s) and s[k + 1] == "|":
            cur.append("\\|")
            k += 2
            continue
        if ch == "|":
            cells.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        k += 1
    cells.append("".join(cur).strip())
    return cells


def add_markdown(doc, md, heading_shift=0, skip_title=False, join_lines=False,
                 table_font_pt=None, warn_headings=("Check these",)):
    """Add markdown to `doc`. Returns the list of (level, text) headings added.

    Handled: `#`-`###` headings (deeper ones become bold paragraphs), `-`/`*`/`+`
    bullets with nesting, `1.` numbered lists, pipe tables (separator row dropped,
    right-aligned numeric columns; an empty header row makes a label / value table),
    fenced code, `>` quotes, rules, **bold**, *italic*, `code`. Each non-blank line is
    its own paragraph like the tool's HTML report; `join_lines=True` merges the lines
    of a paragraph as standard markdown does. `heading_shift` is added to each
    heading level; `skip_title` drops a leading `# title`. Lists under a heading that
    starts with any of `warn_headings` get the amber callout shading, as in the HTML."""
    lines = clean_text(md).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    added = []
    i, n = 0, len(lines)
    warn = False
    first_heading = True
    para = []
    list_num = None          # numId of the numbered list being written
    stack = []               # indentation widths of the open bullet levels

    def flush():
        if para:
            doc.paragraph(" ".join(para), markup=True)
            del para[:]

    while i < n:
        ln = lines[i]
        s = ln.strip()
        if not s:
            flush()
            list_num, stack = None, []
            i += 1
            continue
        m = re.match(r"^\s*(```|~~~)", ln)
        if m:
            flush()
            fence = m.group(1)
            j = i + 1
            buf = []
            while j < n and not lines[j].strip().startswith(fence):
                buf.append(lines[j])
                j += 1
            doc.code_block("\n".join(buf))
            i = j + 1
            list_num, stack = None, []
            continue
        m = _HEAD_RE.match(ln)
        if m and not ln.startswith(" " * 4):
            flush()
            list_num, stack = None, []
            lvl, text = len(m.group(1)), m.group(2)
            if skip_title and first_heading and lvl == 1:
                first_heading = False
                i += 1
                continue
            first_heading = False
            warn = any(text.startswith(w) for w in warn_headings)
            plain = "".join(r["text"] for r in parse_inline(text))
            level = lvl + heading_shift
            if level <= 3:
                added.append((level, doc.heading(plain, max(1, level))))
            else:
                doc.paragraph(parse_inline(text), bold=True, keep_next=True, space_before=120)
            i += 1
            continue
        if s.startswith("|"):
            flush()
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append(_split_row(lines[i]))
                i += 1
            _md_table(doc, rows, table_font_pt)
            list_num, stack = None, []
            continue
        if _HR_RE.match(ln) and not _BULLET_RE.match(ln):
            flush()
            doc.horizontal_rule()
            i += 1
            continue
        if s.startswith(">"):
            flush()
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip())
                i += 1
            doc.callout(" ".join(buf), markup=True)
            continue
        m = _BULLET_RE.match(ln)
        mn = _NUM_RE.match(ln)
        if m or mn:
            flush()
            mm = m or mn
            indent = len(mm.group(1).replace("\t", "    "))
            text = mm.group(3)
            while stack and indent < stack[-1]:
                stack.pop()
            if not stack or indent > stack[-1]:
                stack.append(indent)
            level = len(stack) - 1
            shade = SHADE_WARN if warn else None
            if mn and not m:
                if list_num is None:
                    list_num = doc.new_numbered_list()
                doc.list_item(text, level=level, num_id=list_num, markup=True, shade=shade)
            else:
                doc.list_item(text, level=level, num_id=1, markup=True, shade=shade)
            i += 1
            continue
        list_num, stack = None, []
        if join_lines:
            para.append(s)
            if ln.endswith("  "):
                flush()
        else:
            doc.paragraph(parse_inline(s))
        i += 1
    flush()
    return added


def _md_table(doc, rows, font_pt):
    align = None
    body = []
    for r in rows:
        if r and all(_SEP_CELL.match(c.replace(" ", "")) for c in r if c) and any(r):
            if any(":" in c for c in r):          # explicit alignment; else guess from the data
                align = ["center" if c.startswith(":") and c.endswith(":") and len(c) > 1
                         else "right" if c.endswith(":") else "left" for c in r]
            continue
        body.append(r)
    if not body:
        return
    ncols = max(len(r) for r in body)
    body = [r + [""] * (ncols - len(r)) for r in body]
    head_empty = not any(body[0])
    header = not head_empty
    if head_empty:
        body = body[1:]
        if not body:
            return
    if align is None:
        align = []
        data = body[1:] if header else body
        for j in range(ncols):
            col = [r[j] for r in data if r[j]]
            plain = ["".join(x["text"] for x in parse_inline(c)) for c in col]
            align.append("right" if plain and all(_NUMERIC_CELL.match(c) for c in plain)
                         else "left")
    doc.table(body, header=header, align=align, markup=True, font_pt=font_pt,
              first_col_shade=SHADE_KEY if head_empty else None,
              first_col_bold=head_empty)


# --------------------------------------------------------------------------------------
# SVG -> PNG
# --------------------------------------------------------------------------------------
LAST_BACKEND = None
SVG_BACKENDS = ("qt", "cairosvg", "rsvg-convert", "inkscape", "soffice")


def svg_size(svg_path):
    """(width, height) of an SVG from its viewBox, else width / height. None if unknown."""
    try:
        with open(svg_path, "rb") as f:
            head = f.read(4096).decode("utf-8", "replace")
    except OSError:
        return None
    m = re.search(r"<svg\b[^>]*>", head, re.S)
    if not m:
        return None
    tag = m.group(0)
    vb = re.search(r'viewBox\s*=\s*"([^"]+)"', tag)
    if vb:
        try:
            v = [float(x) for x in re.split(r"[\s,]+", vb.group(1).strip())]
            if len(v) == 4 and v[2] > 0 and v[3] > 0:
                return v[2], v[3]
        except ValueError:
            pass

    def dim(name):
        d = re.search(r'\b%s\s*=\s*"([\d.]+)' % name, tag)
        return float(d.group(1)) if d else None
    w, h = dim("width"), dim("height")
    return (w, h) if w and h else None


def _hidden_run(cmd, timeout=90):
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000        # no console window
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                       **kw)
    return p.returncode, (p.stderr or b"").decode("utf-8", "replace")


def _valid_png(path):
    try:
        if os.path.getsize(path) < 60:
            return False
        with open(path, "rb") as f:
            image_info(f.read())
        return True
    except (OSError, ValueError):
        return False


def _svg_qt(svg, png, width, height):
    from qgis.PyQt.QtSvg import QSvgRenderer
    from qgis.PyQt.QtGui import QImage, QPainter, QColor
    from qgis.PyQt.QtCore import QCoreApplication
    if QCoreApplication.instance() is None:
        raise RuntimeError("no Qt application (fonts need one)")
    r = QSvgRenderer(svg)
    if not r.isValid():
        raise RuntimeError("Qt could not read the SVG")
    fmt = getattr(QImage, "Format_ARGB32", None) or QImage.Format.Format_ARGB32
    img = QImage(int(width), int(height), fmt)
    img.fill(QColor(255, 255, 255))
    p = QPainter(img)
    hint = getattr(QPainter, "Antialiasing", None) or QPainter.RenderHint.Antialiasing
    p.setRenderHint(hint, True)
    r.render(p)
    p.end()
    if not img.save(png, "PNG"):
        raise RuntimeError("Qt could not write the PNG")


def _svg_cairosvg(svg, png, width, height):
    import cairosvg
    cairosvg.svg2png(url=svg, write_to=png, output_width=int(width), background_color="white")


def _svg_rsvg(svg, png, width, height):
    exe = shutil.which("rsvg-convert")
    if not exe:
        raise RuntimeError("rsvg-convert not on PATH")
    rc, err = _hidden_run([exe, "-w", str(int(width)), "-b", "white", "-f", "png", "-o", png,
                           svg])
    if rc:
        raise RuntimeError("rsvg-convert: " + err.strip()[:200])


def _svg_inkscape(svg, png, width, height):
    exe = shutil.which("inkscape")
    if not exe:
        raise RuntimeError("inkscape not on PATH")
    rc, err = _hidden_run([exe, svg, "--export-type=png", "--export-filename=" + png, "-w",
                           str(int(width)), "--export-background=white",
                           "--export-background-opacity=1"])
    if rc or not _valid_png(png):
        rc, err = _hidden_run([exe, "-z", "-e", png, "-w", str(int(width)), "-b", "white", svg])
    if rc:
        raise RuntimeError("inkscape: " + err.strip()[:200])


def _svg_soffice(svg, png, width, height):
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if not exe:
        raise RuntimeError("LibreOffice not on PATH")
    if not _soffice_has_draw(exe):                       # 3.9.0: it cannot open an SVG
        raise RuntimeError("LibreOffice without its Draw module cannot read an SVG")
    work = tempfile.mkdtemp(prefix="ndbr_lo_")
    try:
        prof = "file://" + os.path.join(work, "profile").replace("\\", "/")
        if not prof.startswith("file:///"):
            prof = prof.replace("file://", "file:///")
        flt = ('png:draw_png_Export:{"PixelWidth":{"type":"long","value":"%d"},'
               '"PixelHeight":{"type":"long","value":"%d"}}' % (int(width), int(height)))
        out = os.path.join(work, "out")
        rc, err = _hidden_run([exe, "-env:UserInstallation=" + prof, "--headless",
                               "--convert-to", flt, "--outdir", out, svg], timeout=180)
        made = os.path.join(out, os.path.splitext(os.path.basename(svg))[0] + ".png")
        if not os.path.exists(made):
            raise RuntimeError("LibreOffice made no PNG " + err.strip()[:200])
        shutil.copyfile(made, png)
    finally:
        shutil.rmtree(work, ignore_errors=True)


_SVG_FUNCS = {"qt": _svg_qt, "cairosvg": _svg_cairosvg, "rsvg-convert": _svg_rsvg,
              "inkscape": _svg_inkscape, "soffice": _svg_soffice}


def _soffice_has_draw(exe):
    """3.9.0: LibreOffice reads an SVG only with its Draw module (sd: libsdlo.so / sdlo.dll /
    libsdlo.dylib next to the program, or in ../Frameworks on macOS). A core-only install (e.g.
    libreoffice-core without libreoffice-draw) was listed as usable and then made no PNG. True
    when the module is found, or when the program folder cannot be looked at (not ruled out)."""
    try:
        prog = os.path.dirname(os.path.realpath(exe))
        folders = [prog, os.path.join(os.path.dirname(prog), "Frameworks")]
        seen = False
        for d in folders:
            if not os.path.isdir(d):
                continue
            seen = True
            if any(re.match(r"^(lib)?sdlo\.(so|dll|dylib)$", n, re.I) for n in os.listdir(d)):
                return True
        return not seen
    except (OSError, ValueError):
        return True


def available_svg_backends():
    """Names of the converters that look usable here (cheap checks, nothing is run)."""
    out = []
    try:
        from qgis.PyQt.QtSvg import QSvgRenderer  # noqa: F401
        from qgis.PyQt.QtGui import QImage  # noqa: F401
        out.append("qt")
    except Exception:
        pass
    try:
        import cairosvg  # noqa: F401
        out.append("cairosvg")
    except Exception:
        pass
    for name, exes in (("rsvg-convert", ("rsvg-convert",)), ("inkscape", ("inkscape",))):
        if any(shutil.which(e) for e in exes):
            out.append(name)
    lo = shutil.which("soffice") or shutil.which("libreoffice")
    if lo and _soffice_has_draw(lo):
        out.append("soffice")
    return out


def svg_to_png(svg_path, png_path, width_px=1600, backends=None, errors=None):
    """Render an SVG file to a PNG of `width_px` pixels wide (height from the SVG's
    aspect ratio, white background). Tries, in order: Qt's QSvgRenderer (only inside QGIS,
    with a Qt application), cairosvg, `rsvg-convert`, `inkscape`, then LibreOffice
    (`soffice`) as a last resort. Returns `png_path`, or None when nothing worked (then
    the caller lists the figure as a file path). `errors`, if a list, receives one line
    per backend that failed."""
    size = svg_size(svg_path) or (1000.0, 700.0)
    width = max(64, int(width_px))
    height = max(32, int(round(width * size[1] / size[0])))
    d = os.path.dirname(os.path.abspath(png_path))
    if d and not os.path.isdir(d):
        os.makedirs(d)
    for name in (backends or SVG_BACKENDS):
        fn = _SVG_FUNCS.get(name)
        if fn is None:
            continue
        try:
            if os.path.exists(png_path):
                os.remove(png_path)
            fn(svg_path, png_path, width, height)
            if _valid_png(png_path):
                global LAST_BACKEND
                LAST_BACKEND = name
                return png_path
            raise RuntimeError("no valid PNG was written")
        except Exception as e:           # ImportError, missing exe, timeout, bad SVG ...
            if errors is not None:
                errors.append("{}: {}: {}".format(name, type(e).__name__, str(e)[:160]))
            try:
                if os.path.exists(png_path):
                    os.remove(png_path)
            except OSError:
                pass
    return None


# ======================================================================================
# the submittal report
# ======================================================================================
MODEL_DIR = "_breach_model"
# v3.2.0: one EAP map. ND Dam Hazard's map book (mapbook.py: BOOK_FILE "EAP_Map_Book.pdf" in
# <dam folder>/outputs/EAP/, a time-stamped name when the file was locked) is the EAP map;
# this tool's SVG is a preview (new name) - the 3.1.x name is still read.
EAP_PREVIEW_SVG = "EAP Inundation Map (preview).svg"
EAP_OLD_SVG = "EAP Inundation Map.svg"
MAPBOOK_PATTERN = "EAP_Map_Book*.pdf"
MAPBOOK_DIRS = (("outputs", "EAP"), ("EAP",))


def find_mapbook(folder):
    """ND Dam Hazard's EAP map book PDF for a dam folder (newest), or ""."""
    found = []
    for parts in MAPBOOK_DIRS:
        d = folder
        for part in parts:
            try:
                names = os.listdir(d)
            except OSError:
                d = None
                break
            match = [n for n in names if n.lower() == part.lower()
                     and os.path.isdir(os.path.join(d, n))]
            d = os.path.join(d, match[0]) if match else None
            if d is None:
                break
        if not d:
            continue
        try:
            for n in os.listdir(d):
                p = os.path.join(d, n)
                if fnmatch.fnmatch(n.lower(), MAPBOOK_PATTERN.lower()) and os.path.isfile(p):
                    found.append(p)
        except OSError:
            pass
    if not found:
        return ""
    return max(found, key=lambda p: (os.path.getmtime(p), p))
EM = u"—"

FINAL_FILES = (("Depth (Max).tif", "Maximum flood depth", "ft"),
               ("Velocity (Max).tif", "Maximum flow velocity", "ft/s"),
               ("Arrival Time.tif", "Flood arrival time", "h after breach initiation"),
               ("WSE (Max).tif", "Maximum water-surface elevation", "ft (DEM datum)"),
               ("Baseline Depth (Max).tif", "Maximum depth without the breach", "ft"),
               ("Baseline Velocity (Max).tif", "Maximum velocity without the breach", "ft/s"),
               ("Baseline Arrival Time.tif", "Arrival time without the breach",
                "h after initiation"),
               ("Baseline WSE (Max).tif", "Maximum WSE without the breach", "ft"),
               ("Incremental Depth (Max).tif", "Depth with the breach minus without", "ft"),
               ("Incremental WSE Rise (Max).tif", "Water-surface rise from the breach", "ft"))

FILE_DESC = {
    "run_report.md": "Run report (markdown)", "run_report.json": "Run report data (JSON)",
    "run_report.html": "Run report (HTML)", "breach_hydrograph.csv": "Breach outflow hydrograph",
    "no_breach_hydrograph.csv": "Outflow without the breach", "hydrograph.svg":
        "Outflow hydrograph plot", "gauges.svg": "Discharge past the gauges (plot)",
    "receptors_flooded.csv": "Receptors reached by the flood", "flow_trace.csv":
        "Flow path used to lay out the domain", "state.json": "Run checkpoint",
    "qa_dam_inflow_and_flow_path.geojson": "QA layer: dam, inflow point, flow path",
    "run_manifest.json": "Run manifest (versions, input hashes, settings)",
    "eap inundation map.svg": "EAP inundation map (this tool's, up to 3.1.x)",
    "eap inundation map (preview).svg": "EAP inundation map preview (this tool's; the EAP map "
                                        "is ND Dam Hazard's map book)",
    "flood extent.geojson": "Flood extent polygons",
    "dam.json": "Shared dam record of the ND dam suite (values, sources, status)",
    "flood polygons (max depth).geojson": "Flood polygons by depth class",
    "arrival zones.geojson": "Arrival-time zones", "incremental depth polygons.geojson":
        "Incremental depth polygons", "breach products.gpkg": "GeoPackage of the vector products",
    "sensitivity_summary.csv": "Sensitivity cases", "sensitivity_report.html":
        "Sensitivity report", "cascade_report.md": "Cascade failure report"}
# written into the dam folder by the ND 5.2 hazard classification tool (algorithms_ndhazard)
HAZARD_FILES = {"markdown": "Hazard Classification (ND 5.2).md",
                "json": "Hazard Classification (ND 5.2).json",
                "csv": "Hazard Classification Findings.csv",
                "geojson": "Hazard Classification Findings.geojson",
                "svg_house": "ND Fig 5.1 Houses.svg",
                "svg_mobile": "ND Fig 5.2 Mobile Homes.svg"}
FILE_DESC.update({
    HAZARD_FILES["markdown"].lower(): "Hazard classification, ND 5.2 (screening, markdown)",
    HAZARD_FILES["json"].lower(): "Hazard classification, ND 5.2 (screening, data)",
    HAZARD_FILES["csv"].lower(): "Hazard classification findings",
    HAZARD_FILES["geojson"].lower(): "Hazard classification findings (points)",
    HAZARD_FILES["svg_house"].lower(): "ACER TM 11 chart with the houses reached (ND Fig. 5.1)",
    HAZARD_FILES["svg_mobile"].lower():
        "ACER TM 11 chart with the mobile homes reached (ND Fig. 5.2)"})
for _f, _d, _u in FINAL_FILES:
    FILE_DESC[_f.lower()] = "{} ({})".format(_d, _u)

_UNIT_SUFFIX = ((u"_acft", u" (ac-ft)"), (u"_cfs", u" (cfs)"), (u"_ft2s", u" (ft²/s)"),
                (u"_fps", u" (ft/s)"), (u"_ft", u" (ft)"), (u"_hr", u" (h)"), (u"_mi", u" (mi)"),
                (u"_min", u" (min)"), (u"_pct", u" (%)"), (u"_ac", u" (ac)"),
                (u"_m2", u" (m²)"), (u"_m", u" (m)"))
LABELS = {
    "starting_wse_ft": "Starting water-surface elevation (ft)",
    "starting_wse_rule": "Starting level rule", "breach_bottom_ft": "Breach bottom elevation (ft)",
    "hw_ft": "Water depth above breach bottom, Hw (ft)", "hb_ft": "Breach height, Hb (ft)",
    "storage_at_failure_acft": "Storage at failure (ac-ft)", "dead_storage_acft":
        "Dead storage below the breach (ac-ft)", "releasable_acft": "Releasable storage (ac-ft)",
    "storage_exponent_m": "Storage-curve exponent (volume ~ depth^m)",
    "breach_parameters": "Breach parameter method", "failure_mode": "Failure mode",
    "avg_breach_width_ft": "Average breach width (ft)", "bottom_width_ft":
        "Breach bottom width (ft)", "side_slope_h": "Breach side slope (H:1V)",
    "formation_time_hr": "Formation time (h)", "released_acft": "Volume released (ac-ft)",
    "pipe_initial_dia_ft": "Initial pipe diameter (ft)", "pipe_collapse_dia_ft":
        "Pipe diameter at roof collapse (ft)", "pipe_phase_hr": "Pipe phase duration (h)",
    "tr60_peak_cfs": "TR-60 peak (cfs)", "tr60_rule": "TR-60 rule used",
    "breach_factor_br_ac": "TR-60 breach factor Br", "embankment_area_ft2":
        u"Embankment cross-section area (ft²)", "rise_time_hr": "Hydrograph rise time (h)",
    "base_time_hr": "Hydrograph base time (h)", "shape": "Hydrograph shape",
    "froehlich_1995_peak_cfs": "Check: Froehlich (1995) peak (cfs)",
    "tr60_peak_cfs_check": "Check: TR-60 peak (cfs)", "tr60_rule_check": "Check: TR-60 rule",
    "peak_cfs": "Peak breach outflow, routed (cfs)", "time_to_peak_hr": "Time to peak (h)",
    "duration_hr": "Hydrograph duration (h)", "volume_acft": "Hydrograph volume (ac-ft)",
    "source": "Source", "dv_ge_7_ft2s": u"Depth x velocity at least 7 ft²/s",
    "depth_ge_1ft": "Depth at least 1 ft", "depth_ge_3ft": "Depth at least 3 ft",
    "max_depth_ft": "Maximum depth (ft)", "earliest_arrival_hr": "Earliest arrival (h)",
    "dam_id": "Dam ID", "name": "Dam name", "dam_height_ft": "Dam height (ft)",
    "top_of_dam_ft": "Top of dam elevation (ft)", "storage_top_acft":
        "Storage at top of dam (ac-ft)", "surface_area_top_ac": "Surface area at top of dam (ac)",
    "pmf_max_wse_ft": "PMF maximum reservoir level (ft)", "crest_width_ft": "Crest width (ft)",
    "crest_length_ft": "Crest length (ft)", "us_slope_h": "Upstream slope (H:1V)",
    "ds_slope_h": "Downstream slope (H:1V)", "valley_width_ft": "Valley width (ft)"}
BREACH_KEYS = ("starting_wse_ft", "starting_wse_rule", "breach_bottom_ft", "hw_ft", "hb_ft",
               "storage_at_failure_acft", "dead_storage_acft", "releasable_acft",
               "storage_exponent_m", "breach_parameters", "failure_mode", "avg_breach_width_ft",
               "bottom_width_ft", "side_slope_h", "formation_time_hr", "pipe_initial_dia_ft",
               "pipe_collapse_dia_ft", "pipe_phase_hr", "tr60_rule", "breach_factor_br_ac",
               "embankment_area_ft2", "rise_time_hr", "base_time_hr", "shape", "released_acft",
               "peak_cfs", "time_to_peak_hr", "duration_hr", "volume_acft",
               "froehlich_1995_peak_cfs", "tr60_peak_cfs_check", "tr60_rule_check", "source")
SENS_LABELS = {"case": "Case", "group": "Varied", "method": "Method", "peak_cfs": "Peak (cfs)",
               "flooded_ac": "Flooded (ac)", "flooded_ac_over_arrival_depth":
                   "Flooded > arrival depth (ac)", "max_depth_ft": "Max depth (ft)",
               "max_velocity_fps": "Max velocity (ft/s)", "latest_arrival_hr":
                   "Latest arrival (h)", "structures_flooded": "Structures flooded",
               "structures_depth_ge_3ft": "Structures >= 3 ft", "road_km_flooded":
                   "Road flooded (km)", "rail_km_flooded": "Rail flooded (km)",
               "hazard_class": "Hazard class", "domain_too_small": "Domain too small"}


def label_of(key):
    if key in LABELS:
        return LABELS[key]
    k = str(key)
    for suf, txt in _UNIT_SUFFIX:
        if k.endswith(suf):
            k = k[:-len(suf)] + txt
            break
    k = k.replace("_", " ")
    return k[:1].upper() + k[1:]


def fmt_val(v, nd=3):
    """A value as report text: no trailing zeros, thousands separators, Yes/No."""
    if v is None or v == "":
        return EM
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, int):
        return "{:,}".format(v) if abs(v) >= 10000 else str(v)
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            return EM
        if abs(v) >= 1000:
            return "{:,.0f}".format(v)
        s = "{:.{}f}".format(v, 1 if abs(v) >= 100 else nd)
        return s.rstrip("0").rstrip(".") if "." in s else s
    if isinstance(v, (list, tuple)):
        return ", ".join(fmt_val(x, nd) for x in v[:40]) + (" ..." if len(v) > 40 else "")
    if isinstance(v, dict):
        return "; ".join("{}: {}".format(label_of(k), fmt_val(x, nd)) for k, x in v.items())
    return clean_text(v)


def _num(x):
    try:
        f = float(str(x).replace(",", ""))
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _median(v):
    v = sorted(v)
    n = len(v)
    return v[n // 2] if n % 2 else 0.5 * (v[n // 2 - 1] + v[n // 2])


def _safe_id(name):
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(name))


def _fmt_bytes(n):
    for unit in ("B", "kB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "{:.0f} {}".format(n, unit) if unit == "B" else "{:.1f} {}".format(n, unit)
        n /= 1024.0


class SubmittalOptions(object):
    """What goes into the report. Unknown names raise TypeError."""
    DEFAULTS = dict(
        title="Dam Breach Inundation Analysis", dam_name="", engineer="", firm="", project="",
        logo="", date="", author="", include_figures=True, include_sensitivity=True,
        include_consequences=True, include_reproducibility=True, include_assumptions=True,
        include_checklist=True, include_file_list=True, include_run_report=False,
        include_toc=True, figure_width_px=1600, page="letter", update_fields=True,
        max_table_rows=60, svg_backends=None, created=None)

    def __init__(self, **kw):
        for k, v in self.DEFAULTS.items():
            setattr(self, k, v)
        for k, v in kw.items():
            if k not in self.DEFAULTS:
                raise TypeError("unknown option {}".format(k))
            setattr(self, k, v)

    @classmethod
    def from_any(cls, o):
        if o is None:
            return cls()
        if isinstance(o, cls):
            return o
        return cls(**dict(o))


class SubmittalResult(object):
    def __init__(self):
        self.path = ""
        self.sections = []           # (number, title) as written
        self.figures = []            # {"name", "source", "embedded", "note"}
        self.missing = []            # expected files that were not there
        self.warnings = []           # problems while building (never fatal)
        self.svg_backend = None
        self.counts = {}

    def as_dict(self):
        return dict(self.__dict__)


class _Files(object):
    """Case-insensitive lookups in a dam folder and its _breach_model folder."""

    def __init__(self, root):
        self.root = root
        self._ls = {}

    def ls(self, d):
        if d not in self._ls:
            try:
                self._ls[d] = sorted(os.listdir(d))
            except OSError:
                self._ls[d] = []
        return self._ls[d]

    def dirs(self, *subs):
        return [os.path.join(self.root, s) if s else self.root for s in subs]

    def find(self, name, *subs):
        subs = subs or ("", MODEL_DIR)
        low = name.lower()
        for d in self.dirs(*subs):
            for f in self.ls(d):
                if f.lower() == low and os.path.isfile(os.path.join(d, f)):
                    return os.path.join(d, f)
        return ""

    def glob(self, pattern, *subs):
        subs = subs or ("", MODEL_DIR)
        pat = pattern.lower()
        out = []
        for d in self.dirs(*subs):
            for f in self.ls(d):
                if fnmatch.fnmatch(f.lower(), pat) and os.path.isfile(os.path.join(d, f)):
                    out.append(os.path.join(d, f))
        return out

    def subdir(self, name):
        for f in self.ls(self.root):
            if f.lower() == name.lower() and os.path.isdir(os.path.join(self.root, f)):
                return os.path.join(self.root, f)
        return ""


def _read_text(path, limit=8 * 1024 * 1024):
    with open(path, "rb") as f:
        return f.read(limit).decode("utf-8-sig", "replace")


def _load_json(path):
    if not path:
        return None
    try:
        return json.loads(_read_text(path))
    except (OSError, ValueError):
        return None


def _read_csv(path, limit=None):
    """(header, rows, truncated) or None. Rows are lists of strings."""
    if not path:
        return None
    try:
        text = _read_text(path)
    except OSError:
        return None
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    delim = ";" if lines[0].count(";") > lines[0].count(",") else ","
    try:
        rows = list(csv.reader(lines, delimiter=delim))
    except csv.Error:
        return None
    header, body = rows[0], rows[1:]
    trunc = False
    if limit and len(body) > limit:
        body, trunc = body[:limit], True
    return header, body, trunc


def _read_geojson_features(path):
    d = _load_json(path)
    if not isinstance(d, dict):
        return None
    if d.get("type") == "FeatureCollection":
        return [f for f in d.get("features", []) if isinstance(f, dict)]
    return [d] if d.get("type") == "Feature" else None


def _ring_area(ring):
    a = 0.0
    n = len(ring)
    for k in range(n):
        x1, y1 = ring[k][0], ring[k][1]
        x2, y2 = ring[(k + 1) % n][0], ring[(k + 1) % n][1]
        a += x1 * y2 - x2 * y1
    return 0.5 * a


def _geom_area(geom):
    if not isinstance(geom, dict):
        return 0.0
    t, c = geom.get("type"), geom.get("coordinates")
    polys = c if t == "MultiPolygon" else ([c] if t == "Polygon" else [])
    tot = 0.0
    for poly in polys or []:
        for k, ring in enumerate(poly):
            a = abs(_ring_area(ring))
            tot += a if k == 0 else -a
    return tot


def _flatten(obj, prefix=""):
    """(dotted key, text) rows for a nested dict / list."""
    rows = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = "{}.{}".format(prefix, k) if prefix else str(k)
            if isinstance(v, dict) and v:
                rows.extend(_flatten(v, key))
            elif isinstance(v, list) and v and any(isinstance(x, (dict, list)) for x in v):
                for n, x in enumerate(v):
                    rows.extend(_flatten(x, "{}[{}]".format(key, n)) if isinstance(
                        x, (dict, list)) else [("{}[{}]".format(key, n), fmt_val(x))])
            else:
                rows.append((key, fmt_val(v, 6)))
    elif isinstance(obj, list):
        for n, x in enumerate(obj):
            rows.extend(_flatten(x, "{}[{}]".format(prefix, n)))
    else:
        rows.append((prefix, fmt_val(obj, 6)))
    return rows


_HEXISH = re.compile(r"^[0-9a-fA-F]{24,}$")


def _html_items(text, heading):
    m = re.search(r"<h2>\s*%s\s*</h2>\s*<ul[^>]*>(.*?)</ul>" % re.escape(heading), text,
                  re.S | re.I)
    if not m:
        return []
    import html as _html
    return [_html.unescape(re.sub(r"<[^>]+>", "", x)).strip()
            for x in re.findall(r"<li>(.*?)</li>", m.group(1), re.S)]


def _dicts(v):
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _sanitize_report(rep):
    """run_report.json with every field the report reads coerced to the shape it expects, so
    an odd or hand-edited file cannot break a section."""
    r = dict(rep)
    for k in ("summary", "hydrograph", "settings", "dam", "flood", "incremental", "receptors",
              "receptors_incremental", "comparison", "inflow_point"):
        if not isinstance(r.get(k), dict):
            r.pop(k, None)
    r["summary"] = dict(r.get("summary") or {})
    r["settings"] = dict(r.get("settings") or {})
    s, st = r["summary"], r["settings"]
    if not (isinstance(s.get("domain_mi"), list) and len(s["domain_mi"]) >= 2):
        s.pop("domain_mi", None)
    if not isinstance(s.get("incremental_acres"), dict):
        s.pop("incremental_acres", None)
    for k in ("flood", "overrides", "edits"):
        if not isinstance(st.get(k), dict):
            st.pop(k, None)
    if not isinstance(st.get("gauges"), list):
        st.pop("gauges", None)
    w = r.get("warnings")
    r["warnings"] = [str(x) for x in w] if isinstance(w, list) else []
    st_ = r.get("structures")
    if isinstance(st_, list):
        r["structures"] = [str(x) for x in st_]
    else:
        r.pop("structures", None)
    runs = []
    for it in _dicts(r.get("lisflood_runs")):
        it = dict(it)
        for k in ("stats", "edge_max_depth_ft"):
            if not isinstance(it.get(k), dict):
                it[k] = {}
        if not (isinstance(it.get("size_mi"), list) and len(it["size_mi"]) >= 2):
            it["size_mi"] = ["", ""]
        for k in ("edges_over_tolerance", "edges_over_after_receptors",
                  "edges_over_after_class_stop"):
            if k in it and not isinstance(it[k], list):
                it.pop(k)
        runs.append(it)
    r["lisflood_runs"] = runs
    r["anuga_runs"] = [dict(a, stats=a.get("stats") if isinstance(a.get("stats"), dict) else {})
                       for a in _dicts(r.get("anuga_runs"))]
    dec = []
    for x in _dicts(r.get("receptor_edge_decisions")):
        dec.append({"run": x.get("run"), "edges": {k: v for k, v in (x.get("edges") or {}).items()
                                                   if isinstance(v, dict)}
                    if isinstance(x.get("edges"), dict) else {}})
    r["receptor_edge_decisions"] = dec
    inc = r.get("incremental")
    if isinstance(inc, dict) and not isinstance(inc.get("acres_at_or_above"), dict):
        inc["acres_at_or_above"] = {}
    rec = r.get("receptors")
    if isinstance(rec, dict):
        r["receptors"] = {k: v for k, v in rec.items()
                          if not (k in ("structure", "road", "rail") and not isinstance(v, dict))}
    return r


class _Report(object):
    def __init__(self, folder, dam_id, out_path, opt):
        self.folder = os.path.abspath(folder) if folder else ""
        self.out_path = out_path
        self.opt = opt
        self.res = SubmittalResult()
        self.files = _Files(self.folder)
        self.tmp = None
        self._svg_dead = False
        self.secnum = {}
        self._break_before_next = False
        self.rep_path = self.files.find("run_report.json")
        rep = _load_json(self.rep_path)
        self.rep = _sanitize_report(rep) if isinstance(rep, dict) else {}
        if self.rep_path and not self.rep:
            self.res.warnings.append("run_report.json could not be read; sections that depend "
                                     "on it are left out.")
        self.summary = self.rep.get("summary") or {}
        self.hy = self.rep.get("hydrograph") or {}
        self.settings = self.rep.get("settings") or {}
        self.dam = self.rep.get("dam") or {}
        self.warnings = [str(w) for w in (self.rep.get("warnings") or [])]
        self.flood_mode = (self.rep.get("scenario") or self.summary.get("scenario")) == "flood"
        self.dam_id = str(dam_id or self.dam.get("dam_id") or self.summary.get("dam_id")
                          or os.path.basename(self.folder) or "")
        self.dam_name = str(opt.dam_name or self.dam.get("name") or "")
        self.dam_label = ("{} ({})".format(self.dam_name, self.dam_id) if self.dam_name
                          else self.dam_id)
        self.date = opt.date or datetime.date.today().isoformat()
        self.doc = Docx(
            title="{} - {}".format(opt.title, self.dam_label).strip(" -"),
            author=opt.author, subject="Dam breach analysis submittal",
            keywords="dam breach; inundation; dam safety; North Dakota",
            header_left="{}: {}".format(opt.title, self.dam_label) if self.dam_label
            else opt.title,
            header_right=opt.firm, footer_left=str(self.rep.get("tool") or
                                                   "ND Dam Breach Runner"),
            page=opt.page, number_headings=True, update_fields=opt.update_fields,
            created=opt.created)
        self.model_dir = os.path.join(self.folder, MODEL_DIR)
        self.consequence_files = []
        self._hazard = None

    # -- small helpers -----------------------------------------------------------------
    def ref(self, key):
        n = self.secnum.get(key)
        return "Section {}".format(n) if n else ""

    def kv(self, rows, widths=(2.7, 3.8), head=("Item", "Value"), caption=None):
        rows = [[a, b] for a, b in rows]
        if not rows:
            return False
        self.doc.table([list(head)] + rows, widths=list(widths), caption=caption,
                       first_col_shade=SHADE_KEY)
        return True

    def sub(self, title):
        return self.doc.heading(title, 2)

    def note(self, text, italic=True):
        self.doc.paragraph(text, italic=italic, size=9, color="595959")

    def csv_table(self, path, caption=None, pretty=None, drop_empty=False, max_rows=None):
        got = _read_csv(path, max_rows or self.opt.max_table_rows)
        if not got:
            return False
        header, rows, trunc = got
        ncols = len(header)
        rows = [r + [""] * (ncols - len(r)) for r in rows]
        keep = list(range(ncols))
        if drop_empty:
            keep = [j for j in keep if any(r[j].strip() not in ("", "None") for r in rows)]
        if not keep:
            return False
        head = [(pretty or {}).get(header[j], header[j]) for j in keep]
        align = []
        for j in keep:
            col = [r[j] for r in rows if r[j].strip()]
            align.append("right" if col and all(_num(c) is not None for c in col) else None)
        self.doc.table([head] + [[r[j] for j in keep] for r in rows], align=align,
                       caption=caption)
        if trunc:
            self.note("Only the first {} rows are shown; the full table is in {}.".format(
                len(rows), os.path.relpath(path, self.folder)))
        return True

    def figure(self, name, paths, caption):
        """Embed the first of `paths` that exists (PNG / JPEG directly, SVG through
        svg_to_png); otherwise list the figure by file name."""
        src = next((p for p in paths if p and os.path.exists(p)), "")
        if not src:
            return False
        ext = os.path.splitext(src)[1].lower()
        rec = {"name": name, "source": src, "embedded": False, "note": ""}
        self.res.figures.append(rec)
        png = ""
        if ext in (".png", ".jpg", ".jpeg"):
            png = src
        elif ext == ".svg":
            for e in (".png", ".jpg"):
                sib = os.path.splitext(src)[0] + e
                if os.path.exists(sib) and os.path.getmtime(sib) >= os.path.getmtime(src):
                    png = sib
                    break
            if not png and not self._svg_dead:
                if self.tmp is None:
                    self.tmp = tempfile.mkdtemp(prefix="ndbr_fig_")
                out = os.path.join(self.tmp, "fig{}.png".format(len(self.res.figures)))
                errs = []
                r = svg_to_png(src, out, self.opt.figure_width_px, self.opt.svg_backends, errs)
                if r:
                    png = r
                    self.res.svg_backend = LAST_BACKEND
                else:
                    rec["note"] = "; ".join(errs) or "no SVG converter available"
                    if not available_svg_backends():
                        self._svg_dead = True
                        self.res.warnings.append(
                            "No SVG converter is available (QGIS Qt SVG, cairosvg, "
                            "rsvg-convert, inkscape, LibreOffice), so figures are listed by "
                            "file name instead of embedded.")
        if png:
            try:
                self.doc.image(png, width_in=min(6.3, self.doc.text_width_in),
                               max_height_in=7.3, alt=caption, caption=caption)
                rec["embedded"] = True
                return True
            except (ValueError, OSError) as e:
                rec["note"] = "could not embed: {}".format(e)
        self.doc.paragraph(u"{} (not embedded; the file is {})".format(caption, src),
                           italic=True, size=9, color="595959")
        return True

    # -- section 1 -----------------------------------------------------------------------
    def sec_summary(self):
        d, s, h = self.doc, self.summary, self.hy
        if not self.rep:
            md = self.files.find("run_report.md")
            if md:
                d.paragraph("run_report.json was not found, so the key numbers below are the "
                            "run report text as the tool wrote it.")
                add_markdown(d, _read_text(md), heading_shift=0, skip_title=True)
            else:
                d.paragraph("No run report (run_report.json or run_report.md) was found in the "
                            "dam folder. The sections that depend on it are left out; the "
                            "reviewer checklist lists what is missing.")
            return True
        rows = []

        def add(k, v, unit=""):
            if v not in (None, ""):
                rows.append([k, fmt_val(v) + (" " + unit if unit else "")])
        add("Dam", self.dam_label)
        add("Scenario", "Flood inflow (PMF), with and without the breach" if self.flood_mode
            else "Sunny day (no inflow)")
        if "breach_occurs" in s:
            add("Dam fails in this scenario", bool(s.get("breach_occurs")))
        method = h.get("method")
        try:
            from . import breach as brmod
            method_txt = brmod.METHOD_LABELS.get(method, method)
        except Exception:
            method_txt = method
        add("Breach method", method_txt or s.get("breach_method"))
        add("Peak breach outflow", s.get("peak_breach_cfs"), "cfs")
        add("Time to peak", h.get("time_to_peak_hr"), "h")
        add("Storage at failure", s.get("storage_at_failure_acft"), "ac-ft")
        add("Flooded area", s.get("flooded_acres"), "ac")
        add("Flooded area at or above the arrival depth",
            s.get("flooded_acres_over_arrival_depth"), "ac")
        add("Maximum depth", s.get("max_depth_ft"), "ft")
        add("Maximum velocity", s.get("max_velocity_fps"), "ft/s")
        add("Latest arrival", s.get("latest_arrival_hr"), "h after breach initiation")
        if s.get("domain_mi"):
            add("Model domain", "{} x {} mi after {} LISFLOOD-FP run(s)".format(
                s["domain_mi"][0], s["domain_mi"][1], s.get("lisflood_runs")))
        add("Final rasters from", s.get("final_model"))
        for k, v in (s.get("incremental_acres") or {}).items():
            add("Area where the breach adds at least " + k, v, "ac")
        add("Receptors flooded", s.get("receptors_flooded"))
        hz = self.find_hazard()
        if hz and hz[2] and hz[2].get("hazard_class"):
            add("Hazard class (ND 5.2, screening)", str(hz[2]["hazard_class"]).upper())
        add("Items flagged for review by the run", len(self.warnings) if self.warnings else 0)
        add("Run finished", self.rep.get("run_finished"))
        add("Software", self.rep.get("tool"))
        self.kv(rows, head=("Item", "Result"), caption="Key results")
        if s.get("units"):
            self.note("Units: {}.".format(s["units"]))
        if s.get("breach_occurs") is False:
            d.callout("The dam does not fail in this flood, so no breach flood was modelled.",
                      kind="warn")
        if self.warnings:
            d.callout("The run flagged {} item(s) for review. They are listed with the breach "
                      "parameters, and the reviewer checklist refers to them.".format(
                          len(self.warnings)), kind="warn")
        return True

    # -- section 2 -----------------------------------------------------------------------
    def sec_dam(self):
        dam = self.dam
        if not dam:
            return False
        rows = []
        for k in ("name", "dam_id", "dam_height_ft", "top_of_dam_ft", "storage_top_acft",
                  "surface_area_top_ac", "pmf_max_wse_ft", "breach_bottom_ft", "crest_width_ft",
                  "crest_length_ft", "us_slope_h", "ds_slope_h", "valley_width_ft"):
            if dam.get(k) not in (None, ""):
                rows.append([label_of(k), fmt_val(dam[k])])
        if self.rep.get("dem_crest_ft") is not None:
            rows.append(["Highest DEM point within 30 m of the dam point (ft)",
                         fmt_val(self.rep["dem_crest_ft"])])
        if self.rep.get("dem_elevation_units"):
            rows.append(["DEM elevation units", self.rep["dem_elevation_units"]])
        if self.rep.get("dam_xy"):
            rows.append(["Dam point (working CRS, m)", ", ".join(
                "{:,.1f}".format(v) for v in self.rep["dam_xy"][:2])])
        ip = self.rep.get("inflow_point")
        if ip:
            rows.append(["Breach inflow point (working CRS, m)", "{} ({})".format(
                ", ".join("{:,.1f}".format(v) for v in ip.get("xy", [])[:2]), ip.get("how", ""))])
        self.kv(rows, head=("Parameter", "Value"), caption="Dam and reservoir data")
        rel = [w for w in self.warnings if re.search(r"top of dam|dem crest|dem elevation|"
                                                     r"highest dem", w, re.I)]
        if rel:
            d = self.doc
            d.paragraph("Notes on these data from the run:", bold=True, keep_next=True)
            d.bullets(rel, shade=SHADE_WARN)
        return True

    # -- section 3 -----------------------------------------------------------------------
    def sec_breach(self):
        h, d = self.hy, self.doc
        if not h and not self.warnings:
            return False
        if h:
            try:
                from . import breach as brmod
                lab = brmod.METHOD_LABELS.get(h.get("method"), h.get("method"))
            except Exception:
                lab = h.get("method")
            d.paragraph([{"text": "Method: ", "b": True}, {"text": fmt_val(lab)}])
            rows = [[label_of(k), fmt_val(h[k])] for k in BREACH_KEYS
                    if k in h and h[k] is not None and k != "source"]
            if h.get("source"):
                rows.append(["Source", fmt_val(h["source"])])
            self.kv(rows, head=("Parameter", "Value"), caption="Breach parameters and hydrograph")
            ov = (self.settings.get("overrides") or {})
            given = [[label_of(k), fmt_val(v)] for k, v in ov.items()
                     if v is not None and k in ("avg_width_ft", "formation_hr", "side_slope_h",
                                                "tr60_rise_hr", "pipe_initial_dia_ft")]
            if given:
                self.sub("Values supplied by the user")
                d.paragraph("These replace the computed breach parameters:", keep_next=True)
                self.kv(given, head=("Parameter", "Value"))
            d.paragraph("The Froehlich (1995) and TR-60 peaks are checks only. The routed peak "
                        "can be well above the regression peaks; the breach width and formation "
                        "time should be reviewed against the dam's embankment and materials.",
                        italic=True, size=9, color="595959")
        if self.warnings:
            self.sub("Notes and warnings recorded by the run")
            d.bullets(self.warnings, shade=SHADE_WARN)
        return True

    # -- section 4 -----------------------------------------------------------------------
    def sec_flood(self):
        rep, d = self.rep, self.doc
        if not rep:
            return False
        if not self.flood_mode:
            d.paragraph("This is a sunny-day (no inflow) breach: the reservoir starts at the "
                        "starting level given with the breach parameters, no flood inflow or "
                        "spillway flow is routed, and no baseline (no-breach) or incremental "
                        "results were computed.")
            return True
        fl = rep.get("flood") or {}
        st = (self.settings.get("flood") or {})
        if fl.get("user_hydrographs"):
            d.paragraph("The with-breach and without-breach outflow hydrographs are those "
                        "supplied by the user; each starts at breach initiation. The reservoir "
                        "was not routed by this tool.")
        rows = []
        for src, keys in ((st, ("inflow_peak_cfs", "trigger", "trigger_value", "normal_pool_ft",
                                "crest_length_ft", "spillway", "baseline_csv")),
                          (self.hy, ("inflow_volume_acft",)),
                          (fl, ("breach_initiation_hr", "pool_at_breach_ft",
                                "peak_pool_ft_no_breach"))):
            for k in keys:
                if src.get(k) not in (None, ""):
                    lab = {"spillway": "Spillway rating supplied", "trigger":
                           "Breach trigger", "trigger_value": "Trigger value (ft, or h)",
                           "baseline_csv": "No-breach outflow file",
                           "inflow_peak_cfs": "PMF inflow peak (cfs)",
                           "inflow_volume_acft": "PMF inflow volume (ac-ft)",
                           "breach_initiation_hr": "Breach initiation (h into the flood)",
                           "pool_at_breach_ft": "Pool level at breach initiation (ft)",
                           "peak_pool_ft_no_breach": "Peak pool level without the breach (ft)",
                           "normal_pool_ft": "Normal pool level (ft)",
                           "crest_length_ft": "Crest length overtopped (ft)"}.get(k, label_of(k))
                    rows.append([lab, fmt_val(src[k])])
        wrote = False
        if rows:
            wrote = self.kv(rows, head=("Item", "Value"), caption="Flood inflow and reservoir")
        if fl.get("peak_outflow_with_breach_cfs") is not None:
            self.sub("Baseline and breach outflow")
            d.table([["Outflow", "Peak (cfs)"],
                     ["Without the breach (spillway and, if overtopped, crest)",
                      fmt_val(fl.get("peak_outflow_no_breach_cfs"))],
                     ["With the breach (total)", fmt_val(fl.get("peak_outflow_with_breach_cfs"))],
                     ["Breach component alone", fmt_val(fl.get("peak_breach_component_cfs"))]],
                    widths=[4.0, 1.5], align=[None, "right"], caption="Peak outflow",
                    total_width_in=5.5)
            wrote = True
        inc = rep.get("incremental")
        if inc:
            self.sub("Incremental effect of the breach")
            d.table([["Depth added by the breach at least", "Area (ac)"]] + [
                [k, fmt_val(v)] for k, v in (inc.get("acres_at_or_above") or {}).items()],
                widths=[3.5, 1.5], align=[None, "right"], caption="Incremental area",
                total_width_in=5.0)
            if inc.get("note"):
                d.paragraph(inc["note"], italic=True, size=9, color="595959")
            wrote = True
        return True

    # -- section 5 -----------------------------------------------------------------------
    def sec_model(self):
        rep, d, st = self.rep, self.doc, self.settings
        runs = rep.get("lisflood_runs") or []
        if not rep or not (runs or st):
            return False
        tier = st.get("tier")
        rows = [["Model", "LISFLOOD-FP (inertial solver)" + (
            "; final results from ANUGA" if tier == "lisflood+anuga" else "")],
            ["Inflow", "Breach hydrograph as a point source just below the dam; free outflow "
                       "at the domain edges"]]
        for k, lab in (("cell_size_m", "Cell size (m)"), ("manning_n", "Manning's n"),
                       ("arrival_depth_ft", "Arrival depth (ft)"),
                       ("edge_tolerance_ft", "Edge depth tolerance (ft)"),
                       ("initial_length_mi", "Initial domain length (mi)"),
                       ("half_width_mi", "Domain half-width (mi)"),
                       ("expand_step_mi", "Domain extension step (mi)"),
                       ("max_extent_mi", "Largest domain (mi)"),
                       ("max_iterations", "Most runs"),
                       ("sim_time_hr", "Simulation time (h)"),
                       ("front_speed_mps", "Front speed for the automatic duration (m/s)")):
            if k in st:
                v = st[k]
                rows.append([lab, "automatic" if k == "sim_time_hr" and v is None
                             else fmt_val(v)])
        if runs:
            rows.append(["Elapsed time (min)", fmt_val(rep.get("elapsed_min"))])
        self.kv(rows, head=("Setting", "Value"), caption="Hydraulic model settings")
        if runs:
            self.sub("Model domain and downstream extent")
            d.paragraph("Each run checks the deepest water on every domain edge. An edge "
                        "deeper than {} ft moves out by {} mi and the model runs again.".format(
                            fmt_val(st.get("edge_tolerance_ft", 1.0)),
                            fmt_val(st.get("expand_step_mi", 3.0))))
            table = [["Run", "Size (mi)", "Cells", "Hours", "Edge depth N/S/E/W (ft)",
                      "Edges over", "Vol. error (%)", "Min"]]
            for it in runs:
                e = it.get("edge_max_depth_ft") or {}
                over = it.get("edges_over_after_class_stop", it.get(
                    "edges_over_after_receptors", it.get("edges_over_tolerance"))) or []
                size = it.get("size_mi") or ["", ""]
                table.append([fmt_val(it.get("run")), "{} x {}".format(*size[:2]),
                              fmt_val(it.get("cells")), fmt_val(it.get("sim_time_hr")),
                              "/".join(fmt_val(e.get(k)) for k in "NSEW"),
                              ", ".join(over) or "none",
                              fmt_val((it.get("stats") or {}).get("max_volume_error_pct")),
                              fmt_val(it.get("runtime_min"))])
            d.table(table, align=[None, None, "right", "right", None, None, "right", "right"],
                    caption="LISFLOOD-FP runs")
            dec = rep.get("receptor_edge_decisions") or []
            drows = []
            for rec in dec:
                for edge, info in (rec.get("edges") or {}).items():
                    drows.append([fmt_val(rec.get("run")), edge, fmt_val(info.get("edge_depth_ft")),
                                  "extend" if info.get("extend") else "stop",
                                  info.get("reason", "")])
            if drows:
                d.paragraph("With receptor layers, an edge is left alone when no structure, "
                            "road or rail lies in the flood's path within {} mi. The decisions "
                            "are listed for review.".format(
                                fmt_val(st.get("impact_look_ahead_mi", 5.0))))
                d.table([["Run", "Edge", "Depth (ft)", "Decision", "Reason"]] + drows,
                        widths=[0.5, 0.5, 0.8, 0.8, 3.9], caption="Downstream extent decisions")
            last = runs[-1].get("stats") or {}
            mb = [(label_of(k), fmt_val(last.get(k))) for k in (
                "cells", "cell_size_m", "sim_time_hr", "injected_acft", "in_domain_acft",
                "max_volume_error_pct", "worst_interval_error_pct", "peak_inflow_cfs",
                "flooded_acres", "max_depth_ft", "max_velocity_fps") if last.get(k) is not None]
            if mb:
                self.sub("Mass balance (last run)")
                self.kv(mb, head=("Quantity", "Value"), caption="LISFLOOD-FP mass balance")
                err = last.get("max_volume_error_pct")
                if isinstance(err, (int, float)):
                    d.paragraph("The maximum volume error is {}% of the injected volume{}.".format(
                        fmt_val(err), " (above the 1% the tool flags)" if err > 1.0 else
                        " (within the 1% the tool flags)"))
        ar = rep.get("anuga_runs")
        if ar:
            self.sub("ANUGA")
            d.table([["Mesh buffer (m)", "Triangles", "Runtime (min)", "Depth at mesh edge (ft)"]]
                    + [[fmt_val(a.get("buffer_m")), fmt_val((a.get("stats") or {}).get(
                        "triangles")), fmt_val((a.get("stats") or {}).get("runtime_min")),
                        fmt_val((a.get("stats") or {}).get("max_edge_depth_ft"))] for a in ar],
                    align=[None, "right", "right", "right"], caption="ANUGA runs")
            cmp_ = rep.get("comparison")
            if cmp_:
                self.kv([(label_of(k), fmt_val(v)) for k, v in cmp_.items()],
                        head=("Comparison with LISFLOOD-FP", "Value"),
                        caption="ANUGA compared with LISFLOOD-FP")
        extra = []
        if rep.get("structures"):
            extra.append(("Culverts, bridges and notches", rep["structures"]))
        ed = st.get("edits")
        if isinstance(ed, dict) and any(ed.values()):
            extra.append(("Terrain edits", ["{}: {}".format(k, v) for k, v in ed.items() if v]))
        if st.get("gauges"):
            extra.append(("Gauges", [str(g.get("name", "gauge")) if isinstance(g, dict) else str(g)
                                     for g in st["gauges"]]))
        for title, items in extra:
            d.paragraph(title + ":", bold=True, keep_next=True)
            d.bullets([str(x) for x in items])
        return True

    # -- section 6 -----------------------------------------------------------------------
    def sec_results(self):
        d, wrote = self.doc, False
        present = [(f, dsc, u) for f, dsc, u in FINAL_FILES if self.files.find(f, "")]
        if present or self.summary:
            self.sub("Result values")
            s = self.summary
            rows = [(k, v) for k, v in (
                ("Maximum depth (ft)", s.get("max_depth_ft")),
                ("Maximum velocity (ft/s)", s.get("max_velocity_fps")),
                ("Latest arrival (h after breach initiation)", s.get("latest_arrival_hr")),
                ("Flooded area (ac)", s.get("flooded_acres")),
                ("Flooded area at or above the arrival depth (ac)",
                 s.get("flooded_acres_over_arrival_depth"))) if v is not None]
            if rows:
                self.kv([(a, fmt_val(b)) for a, b in rows], head=("Quantity", "Result"),
                        caption="Depth, velocity and arrival")
                wrote = True
            if present:
                d.table([["Raster", "Contents", "Units"]] + [[f, dsc, u] for f, dsc, u in present],
                        widths=[2.4, 2.6, 1.5], caption="Result rasters in the dam folder")
                wrote = True
        feats = _read_geojson_features(self.files.find("Flood Polygons (Max Depth).geojson", ""))
        unit_scale = 1.0
        if feats:
            feats = [f for f in feats if isinstance(f.get("properties"), dict)]
            tot_ac = sum(_num(f["properties"].get("acres")) or 0.0 for f in feats)
            tot_a = sum(_geom_area(f.get("geometry")) for f in feats)
            if tot_ac > 0 and tot_a > 0:
                r = tot_ac * 4046.8564224 / tot_a
                if 0.05 <= r <= 20:
                    unit_scale = r
            rows = []
            for f in sorted(feats, key=lambda f: _num(f["properties"].get("min_depth_ft")) or 0):
                p = f["properties"]
                rows.append([str(p.get("class", "")), fmt_val(_num(p.get("acres")))])
            if rows:
                self.sub("Flooded area by depth class")
                rows.append([{"text": "Total", "bold": True},
                             {"text": fmt_val(round(tot_ac, 1)), "bold": True}])
                d.table([["Maximum depth class", "Area (ac)"]] + rows, widths=[3.0, 1.5],
                        align=[None, "right"], caption="Flooded area by maximum depth",
                        total_width_in=4.5)
                wrote = True
        az = _read_geojson_features(self.files.find("Arrival Zones.geojson", ""))
        if az:
            rows = []
            for f in sorted(az, key=lambda f: _num((f.get("properties") or {}).get(
                    "hours_after_breach")) or 0):
                hrs = _num((f.get("properties") or {}).get("hours_after_breach"))
                area = _geom_area(f.get("geometry")) * unit_scale / 4046.8564224
                if hrs is not None:
                    rows.append([fmt_val(hrs), fmt_val(round(area, 1))])
            if rows:
                self.sub("Arrival times")
                d.table([["Flood has arrived within (h after breach initiation)",
                          "Area reached (ac)"]] + rows, widths=[3.4, 1.4],
                        align=["right", "right"], caption="Area reached by time",
                        total_width_in=4.8)
                if not (feats and unit_scale != 1.0) and not feats:
                    self.note("Areas are computed from the polygon geometry with map units "
                              "taken as metres.")
                wrote = True
        rc = _read_csv(self.files.find("receptors_flooded.csv"))
        if rc:
            header, rows, _t = rc
            idx = {h.strip().lower(): k for k, h in enumerate(header)}
            by = {}
            for r in rows:
                kind = r[idx["kind"]] if "kind" in idx and idx["kind"] < len(r) else "receptor"
                by.setdefault(kind, []).append(r)
            table = []
            for kind, rs in sorted(by.items()):
                def col(name):
                    k = idx.get(name)
                    return [x for x in (_num(r[k]) for r in rs if k is not None and k < len(r))
                            if x is not None]
                dep, vel, arr = col("depth_ft"), col("velocity_fps"), col("arrival_hr")
                rng = lambda v: "{} / {} / {}".format(  # noqa: E731
                    *(fmt_val(x) for x in (min(v), _median(v), max(v)))) if v else EM
                table.append([kind, str(len(rs)), rng(dep), rng(vel),
                              rng(arr)])
            if table:
                self.sub("Conditions at flooded receptors")
                d.table([["Receptor kind", "Flooded", "Depth (ft)\nmin / median / max",
                          "Velocity (ft/s)\nmin / median / max",
                          "Arrival (h)\nearliest / median / latest"]] + table,
                        widths=[1.2, 0.8, 1.5, 1.5, 1.5], align=[None, "right", None, None, None],
                        caption="Depth, velocity and arrival at flooded receptors")
                wrote = True
        g_rows = []
        for gp in sorted(self.files.glob("gauge_*.csv", MODEL_DIR)):
            got = _read_csv(gp)
            if not got:
                continue
            pairs = [(_num(r[0]), _num(r[1])) for r in got[1] if len(r) >= 2]
            pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
            if pairs:
                t, q = max(pairs, key=lambda x: x[1])
                g_rows.append([os.path.splitext(os.path.basename(gp))[0][6:], fmt_val(q),
                               fmt_val(t)])
        if g_rows:
            self.sub("Gauges")
            d.table([["Gauge", "Peak discharge (cfs)", "Time of peak (h)"]] + g_rows,
                    widths=[2.6, 1.8, 1.6], align=[None, "right", "right"],
                    caption="Discharge past the gauges (LISFLOOD-FP)", total_width_in=6.0)
            wrote = True
        book = find_mapbook(self.folder) if self.folder else ""
        if book:
            self.sub("EAP inundation map")
            d.paragraph("The EAP inundation map is ND Dam Hazard's map book on aerial "
                        "imagery: {} (PDF, not embedded). {}".format(
                            os.path.relpath(book, self.folder).replace(os.sep, "/"),
                            "This tool's SVG preview in the dam folder is not the EAP map."
                            if self.eap_preview() else "").strip())
            wrote = True
        if self.opt.include_figures:
            prev = [] if book else self.eap_preview(all_forms=True)
            figs = [("EAP inundation map", prev,
                     "EAP inundation map preview (this tool's): maximum depth classes and "
                     "arrival-time lines; the EAP map is ND Dam Hazard's map book"),
                    ("Outflow hydrograph", [self.files.find("hydrograph.png", MODEL_DIR),
                                            self.files.find("hydrograph.svg", MODEL_DIR)],
                     "Outflow from the dam"),
                    ("Gauge discharge", [self.files.find("gauges.png", MODEL_DIR),
                                         self.files.find("gauges.svg", MODEL_DIR)],
                     "Discharge past the gauges")]
            if any(p for _n, ps, _c in figs for p in ps):
                self.sub("Figures")
                for name, ps, cap in figs:
                    if self.figure(name, ps, cap):
                        wrote = True
        hc = self.files.find("breach_hydrograph.csv", MODEL_DIR)
        got = _read_csv(hc)
        if got:
            pts = [(_num(r[0]), _num(r[1])) for r in got[1] if len(r) >= 2]
            pts = [(a, b) for a, b in pts if a is not None and b is not None]
            if len(pts) >= 2:
                k_peak = max(range(len(pts)), key=lambda k: pts[k][1])
                want = {0, len(pts) - 1, k_peak}
                for k in range(1, 11):
                    want.add(int(round(k * (len(pts) - 1) / 11.0)))
                sel = [pts[k] for k in sorted(want)]
                self.sub("Breach outflow hydrograph ordinates")
                d.table([["Time (h)", "Discharge (cfs)"]] + [[fmt_val(a), fmt_val(b)]
                                                             for a, b in sel],
                        widths=[2.0, 2.0], align=["right", "right"], total_width_in=4.0,
                        caption="Selected breach hydrograph ordinates ({} points in {})".format(
                            len(pts), os.path.basename(hc)))
                wrote = True
        return wrote

    def eap_preview(self, all_forms=False):
        """This tool's EAP map preview (v3.2.0 name first, then the 3.1.x name). all_forms:
        [PNG and SVG of both names] for figure()."""
        names = (EAP_PREVIEW_SVG, EAP_OLD_SVG)
        if all_forms:
            out = []
            for n in names:
                out += [self.files.find(n[:-4] + ".png", ""), self.files.find(n, "")]
            return out
        for n in names:
            p = self.files.find(n, "") or self.files.find(n[:-4] + ".png", "")
            if p:
                return p
        return ""

    # -- section 7 -----------------------------------------------------------------------
    def find_consequences(self):
        if self.consequence_files or not self.opt.include_consequences:
            return self.consequence_files
        out = self.files.glob("consequences*.csv") + self.files.glob("consequences*.md")
        for name in self.files.ls(self.folder):
            p = os.path.join(self.folder, name)
            if os.path.isdir(p) and name.lower().startswith("consequences"):
                for f in sorted(os.listdir(p)):
                    if f.lower().endswith((".csv", ".md")):
                        out.append(os.path.join(p, f))
        self.consequence_files = out
        return out

    def find_hazard(self):
        """(json path, markdown path, data or None) of the ND 5.2 hazard classification in
        the dam folder, or None. Switched with the consequences."""
        if self._hazard is None:
            jp = self.files.find(HAZARD_FILES["json"], "")
            mp = self.files.find(HAZARD_FILES["markdown"], "")
            data = _load_json(jp)
            self._hazard = (jp, mp, data if isinstance(data, dict) else None) \
                if (jp or mp) else ()
        return self._hazard if self.opt.include_consequences else ()

    def sec_hazard(self):
        hz = self.find_hazard()
        if not hz:
            return False
        jp, mp, data = hz
        d = self.doc
        self.sub("Hazard classification (ND 5.2)")
        if data and data.get("hazard_class"):
            st = data.get("settings") or {}
            ch = data.get("charts") or {}
            cnt = data.get("counts") or {}
            rows = [("Hazard class (screening)", str(data["hazard_class"]).upper()),
                    ("Basis", data.get("policy") or ""),
                    ("Potentially lethal by", data.get("lethality_label")
                     or st.get("lethality") or "")]
            if ch:
                rows.append(("Depth-velocity charts", "{} (status {}, digitized by {}, "
                             "checked by {})".format(ch.get("source") or "", ch.get("status"),
                                                     ch.get("digitized_by") or EM,
                                                     ch.get("checked_by") or EM)))
            rows += [("Borderline, higher class assigned (5.3)",
                      "Yes" if data.get("borderline_raised") else "No"),
                     ("'Any depth' taken as", "{} ft".format(fmt_val(st.get("wet_ft"))))]
            rows += [(label_of(k), fmt_val(v)) for k, v in cnt.items()]
            rows.append(("Module", "{} ({})".format(data.get("module_version") or "",
                                                    data.get("created") or "")))
            self.kv(rows, caption="Hazard classification (ND 5.2)")
            gov = [g for g in _dicts(data.get("governing"))]
            if gov:
                d.paragraph("Governing findings", bold=True, keep_next=True)
                items = ["{} ({}){}".format(g.get("reason", ""), g.get("section", ""),
                                            " - borderline, higher class assigned (5.3)"
                                            if g.get("borderline") else "") for g in gov[:30]]
                if len(gov) > 30:
                    items.append("... and {} more".format(len(gov) - 30))
                d.bullets(items)
            gaps = [str(g) for g in (data.get("data_gaps") or [])]
            if gaps:
                d.paragraph("Data gaps", bold=True, keep_next=True)
                d.bullets(gaps, shade=SHADE_WARN)
            notes = [str(n) for n in (data.get("notes") or [])]
            if notes:
                d.paragraph("Notes", bold=True, keep_next=True)
                d.bullets(notes)
        elif mp:
            add_markdown(d, _read_text(mp), heading_shift=1, skip_title=True)
        if self.opt.include_figures:
            for key, cap in (("svg_house", "ND Fig. 5.1 (ACER TM 11 Fig. 2): habitable "
                                           "structures reached by the flood"),
                             ("svg_mobile", "ND Fig. 5.2 (ACER TM 11 Fig. 3): mobile homes "
                                            "reached by the flood")):
                p = self.files.find(HAZARD_FILES[key], "")
                if p:
                    self.figure(HAZARD_FILES[key], [p], cap)
        self.note("From {}, written by the ND hazard classification tool: a screening "
                  "classification for the engineer. The official classification is made by "
                  "the ND Department of Water Resources.".format(
                      ", ".join(os.path.relpath(x, self.folder) for x in (jp, mp) if x)))
        return True

    def sec_receptors(self):
        rep, d, wrote = self.rep, self.doc, False
        rec = rep.get("receptors")
        if rec:
            d.paragraph("Receptors in the model area: {}. Reached by the flood: {}.".format(
                fmt_val(rec.get("receptors_in_model_area")),
                fmt_val(rec.get("receptors_flooded"))))
            table = []
            for kind in ("structure", "road", "rail"):
                v = rec.get(kind)
                if not isinstance(v, dict):
                    continue
                u = " km" if v.get("unit") == "km" else ""
                dv = next((k for k in v if k.startswith("dv_ge_")), None)
                table.append([kind, fmt_val(v.get("flooded")) + u,
                              fmt_val(v.get("depth_ge_1ft")) + u,
                              fmt_val(v.get("depth_ge_3ft")) + u,
                              (fmt_val(v.get(dv)) + u) if dv else EM,
                              fmt_val(v.get("max_depth_ft")),
                              fmt_val(v.get("earliest_arrival_hr"))])
            if table:
                d.table([["Kind", "Flooded", u"Depth ≥ 1 ft", u"Depth ≥ 3 ft",
                          u"D×V ≥ 7 ft²/s", "Max depth (ft)", "Earliest arrival (h)"]]
                        + table, align=[None] + ["right"] * 6, caption="Receptors flooded")
            wrote = True
        ri = rep.get("receptors_incremental")
        if ri:
            self.sub("Receptors affected by the incremental depth")
            d.table([["Breach adds at least", "Receptors"]] + [[k, fmt_val(v)]
                                                                for k, v in ri.items()],
                    widths=[2.5, 4.0], caption="Incremental receptors")
            wrote = True
        rc = self.files.find("receptors_flooded.csv")
        got = _read_csv(rc, 100000)
        if got:
            header, rows, _t = got
            idx = {h.strip().lower(): k for k, h in enumerate(header)}
            ka = idx.get("arrival_hr")

            def key(r):
                v = _num(r[ka]) if ka is not None and ka < len(r) else None
                return (v is None, v if v is not None else 0.0)
            rows = sorted(rows, key=key)
            n = self.opt.max_table_rows // 2 or 25
            self.sub("Receptors reached first")
            d.table([header] + [r + [""] * (len(header) - len(r)) for r in rows[:n]],
                    align=["left", "left"] + ["right"] * (len(header) - 2),
                    caption="Earliest-arriving flooded receptors")
            self.note("{} flooded receptor(s) in all; the full list is in {}.".format(
                len(rows), os.path.relpath(rc, self.folder)))
            wrote = True
        cons = self.find_consequences()
        if cons:
            self.sub("Consequences")
            self.note("From files written by the consequences module, included as found: "
                      + ", ".join(os.path.relpath(c, self.folder) for c in cons))
            for c in cons:
                try:
                    if c.lower().endswith(".csv"):
                        self.csv_table(c, caption=os.path.basename(c))
                    else:
                        add_markdown(d, _read_text(c), heading_shift=1, skip_title=True)
                except Exception as e:
                    self.res.warnings.append("{}: {}".format(os.path.basename(c), e))
            wrote = True
        if self.sec_hazard():
            wrote = True
        return wrote

    # -- section 8 -----------------------------------------------------------------------
    def find_cascade(self):
        parent = os.path.dirname(self.folder)
        if not parent:
            return ""
        mine = {self.dam_id.lower(), _safe_id(self.dam_id).lower(),
                os.path.basename(self.folder).lower()}
        for name in sorted(self.files.ls(parent)):
            if not name.lower().startswith("cascade_"):
                continue
            p = os.path.join(parent, name, "Cascade_Report.md")
            if os.path.exists(p):
                m = re.search(r"Dams upstream to downstream:\s*(.*)", _read_text(p))
                if m and mine & {x.strip().lower() for x in m.group(1).split(",")}:
                    return p
        return ""

    def sec_sensitivity(self):
        d, wrote = self.doc, False
        sd = self.files.subdir("Sensitivity")
        summ = ""
        if sd:
            f = _Files(sd)
            summ = f.find("Sensitivity_Summary.csv", "")
            html_p = f.find("Sensitivity_Report.html", "")
        if summ:
            self.sub("Sensitivity of the breach model")
            d.paragraph("Every case uses the base run's inflow point and domain (LISFLOOD-FP "
                        "only).")
            if html_p:
                txt = _read_text(html_p)
                flags = _html_items(txt, "What the spread means")
                if flags:
                    d.paragraph("What the spread means:", bold=True, keep_next=True)
                    d.bullets(flags, shade=SHADE_WARN if any("CHANGES" in x for x in flags)
                              else None)
                chk = _html_items(txt, "Check these")
                if chk:
                    d.paragraph("Checks recorded by the sensitivity run:", bold=True,
                                keep_next=True)
                    d.bullets(chk, shade=SHADE_WARN)
            self.csv_table(summ, caption="Sensitivity cases", pretty=SENS_LABELS,
                           drop_empty=True)
            env = [n for n in f.ls(sd) if n.lower().startswith("sensitivity") and
                   n.lower().endswith(".tif")]
            if env:
                d.paragraph("Envelope rasters (in the Sensitivity folder): " + ", ".join(env),
                            size=9)
            wrote = True
        cas = self.find_cascade()
        if cas:
            self.sub("Cascade failure")
            self.note("From {}.".format(os.path.relpath(cas, os.path.dirname(self.folder))))
            add_markdown(d, _read_text(cas), heading_shift=1, skip_title=True)
            wrote = True
        prob = []
        for pat in ("probabilistic*", "monte_carlo*", "montecarlo*", "uncertainty*"):
            for ext in (".md", ".csv"):
                prob += self.files.glob(pat + ext, "", MODEL_DIR, "Sensitivity")
        if prob:
            self.sub("Probabilistic and uncertainty results")
            for p in prob:
                try:
                    if p.lower().endswith(".csv"):
                        self.csv_table(p, caption=os.path.basename(p))
                    else:
                        add_markdown(d, _read_text(p), heading_shift=1, skip_title=True)
                except Exception as e:
                    self.res.warnings.append("{}: {}".format(os.path.basename(p), e))
            wrote = True
        return wrote

    # -- section 9 -----------------------------------------------------------------------
    def sec_assumptions(self):
        d, st, h = self.doc, self.settings, self.hy
        method = h.get("breach_parameters") or h.get("method") or "the chosen method"
        a = [
            "Breach size and formation time come from {} or from a hydrograph supplied by the "
            "user. They are estimates from regression equations that differ from one another "
            "by a factor of two or more; they are not measured behaviour of this dam.".format(
                method),
            "The starting reservoir level is the top of dam, or the PMF maximum reservoir level "
            "when that is lower. The breach is routed through the reservoir with the "
            "broad-crested weir equation and a power-law storage curve fitted to the storage "
            "and surface area given (volume proportional to depth squared when no surface area "
            "is given).",
            "The flood scenario uses level-pool routing, a weir crest and the spillway rating "
            "supplied. Gated spillways, outlet works and pool operations are not modelled "
            "unless they are in the rating.",
            "The DEM is taken as the channel and floodplain. Bare-earth LiDAR leaves road "
            "embankments as walls unless culverts, bridges or notches are supplied; channels "
            "are burned and ponds lowered only where layers were given. The source DEM is "
            "never changed. The vertical datum and units are as recorded in the raster or as "
            "assumed and warned in the run report.",
            "Roughness is a single Manning's n ({}) or values from a land-cover raster; these "
            "are typical floodplain values and have not been calibrated.".format(
                fmt_val(st.get("manning_n", 0.06))),
            "LISFLOOD-FP's inertial solver is not meant for supercritical flow, so velocities "
            "within a few cells of the breach are the least reliable part of the result. "
            "Culverts are open cuts through the embankment in LISFLOOD-FP (no roof, no inlet "
            "control), which passes more flow than a real culvert.",
            "Water leaves the model domain freely at its edges (no downstream boundary "
            "condition). The domain is extended until the depth at each edge is below the "
            "tolerance, or until no receptor lies ahead of the flood; the flood is cut off "
            "wherever that could not be met.",
            "Arrival time is measured from breach initiation to the moment the depth first "
            "reaches the arrival depth. Incremental depth is the depth with the breach minus "
            "the depth without it, on the same domain.",
            "The model is not calibrated against an observed flood. It does not include "
            "sediment transport, debris, bulking, blockage of bridges or culverts, or "
            "the failure of other structures unless a cascade run was made.",
            "Receptor layers (structures, roads, rail) are used as supplied, without field "
            "verification and without first-floor elevations. This tool's EAP inundation "
            "map is a preview; the EAP map is ND Dam Hazard's map book on aerial imagery."]
        if st.get("tier") == "lisflood+anuga":
            a.append("ANUGA results replace the LISFLOOD-FP rasters as the final result; the "
                     "two models are compared in the hydraulic model section.")
        v = [
            "Dam height, top of dam elevation and its vertical datum, storage and surface area "
            "against the as-built drawings, inspection reports and stage-storage data.",
            "DEM currency, vertical datum and units, data gaps along the flood path, and that "
            "the bare-earth surface represents the channel and any structures that matter.",
            "That the breach method and its width, formation time and side slope are justified "
            "for this embankment and its materials, and that the sensitivity of the result to "
            "them is understood.",
            "That the downstream extent is enough, and that each warning in the run report has "
            "been resolved or explained.",
            "That the structure, road and rail inventory is complete, and the population at "
            "risk and other consequences are documented.",
            "That the reviewing agency accepts LISFLOOD-FP and ANUGA results for this "
            "submittal. The ND 5.2 hazard classification tool gives a screening class only; the "
            "classification is the engineer's, and the official one is ND DWR's.",
            "That this report was generated from the final model run: rerun the report after "
            "any change to the inputs."]
        d.paragraph("These are the assumptions the tool makes and what a reviewer should "
                    "confirm before the results are relied on.")
        self.sub("Assumptions made by the tool")
        d.bullets(a)
        self.sub("To be verified before submittal")
        d.bullets(v)
        return True

    # -- section 10 ----------------------------------------------------------------------
    def sec_repro(self):
        d, wrote = self.doc, False
        mp = self.files.find("run_manifest.json")
        man = _load_json(mp)
        if man not in (None, {}, []):
            self.sub("Run manifest")
            self.note("From {} (versions, input hashes and settings).".format(
                os.path.relpath(mp, self.folder)))
            self._json_tables(man)
            wrote = True
        elif mp:
            self.res.warnings.append("run_manifest.json could not be read.")
        if self.rep:
            self.sub("Run record")
            rows = [(k, fmt_val(self.rep.get(k))) for k in ("tool", "policy", "run_started",
                                                            "run_finished", "elapsed_min")
                    if self.rep.get(k) is not None]
            self.kv(rows, head=("Item", "Value"), caption="Run record")
            if self.settings:
                self.sub("Run settings")
                rows = [r for r in _flatten(self.settings) if r[1] != EM]
                d.table([["Setting", "Value"]] + [[k, v] for k, v in rows], widths=[2.6, 3.9],
                        font_pt=8, caption="Run settings (from run_report.json)")
            wrote = True
        return wrote

    def _json_tables(self, obj):
        d = self.doc
        if isinstance(obj, dict):
            general, blocks = [], []
            for k, v in obj.items():
                if isinstance(v, dict) and v:
                    blocks.append((k, v))
                elif isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                    blocks.append((k, v))
                else:
                    general.append((str(k), v))
            if general:
                self._kv_code([(k, fmt_val(v, 6)) for k, v in general])
            for k, v in blocks:
                d.paragraph(str(k), bold=True, keep_next=True, space_before=120)
                if isinstance(v, dict):
                    self._kv_code(_flatten(v))
                else:
                    cols = []
                    for x in v:
                        for c in x:
                            if c not in cols:
                                cols.append(c)
                    if 1 <= len(cols) <= 6:
                        d.table([cols] + [[self._code_cell(c, x.get(c)) for c in cols]
                                          for x in v[:200]], font_pt=8)
                    else:
                        self._kv_code(_flatten({k: v}))
        else:
            self._kv_code(_flatten(obj, "value"))

    @staticmethod
    def _code_cell(key, val):
        t = fmt_val(val, 6)
        if _HEXISH.match(t) or "sha" in str(key).lower() or "hash" in str(key).lower():
            return [{"text": t, "code": True, "size": 7.5}]
        return t

    def _kv_code(self, rows):
        if rows:
            self.doc.table([["Item", "Value"]] + [[k, self._code_cell(k, v)] for k, v in rows],
                           widths=[2.4, 4.1], font_pt=8.5)

    # -- section 11 ----------------------------------------------------------------------
    def checklist_items(self):
        f, rep, s, h = self.files, self.rep, self.summary, self.hy
        rk = self.ref
        items = []

        def add(item, status, evidence, key=""):
            r = rk(key) if key else ""
            items.append((item, status, evidence + (" ({})".format(r) if r and status != "No"
                                                    else "")))
        has_h = bool(h) or bool(f.find("breach_hydrograph.csv"))
        add("Breach parameters justified", "Check" if h else "No",
            "Method, parameters and checks are listed; the justification is the engineer's"
            if h else "No breach parameters found in the run report", "breach")
        add("Breach hydrograph produced and routed through the reservoir",
            "Yes" if has_h else "No",
            "Peak {} cfs".format(fmt_val(h.get("peak_cfs", s.get("peak_breach_cfs"))))
            if has_h and (h.get("peak_cfs") or s.get("peak_breach_cfs")) else
            ("breach_hydrograph.csv found" if has_h else "not found"), "breach")
        add("Starting reservoir level documented",
            "Yes" if h.get("starting_wse_ft") is not None else "No",
            "{} ft ({})".format(fmt_val(h.get("starting_wse_ft")), h.get("starting_wse_rule", ""))
            if h.get("starting_wse_ft") is not None else "not in the run report", "breach")
        runs = rep.get("lisflood_runs") or []
        if runs:
            last = runs[-1]
            over = last.get("edges_over_after_class_stop", last.get(
                "edges_over_after_receptors", last.get("edges_over_tolerance"))) or []
            cut = [w for w in self.warnings if re.search(
                r"Stopped after|maximum extent|still advancing|leaves the base domain", w)]
            stop = [w for w in self.warnings if "domain stops growing" in w]
            if cut or over:
                add("Downstream extent criterion (5.1.1(f)) met", "No",
                    "The flood was still leaving the domain when the run stopped: "
                    + (cut[0][:110] if cut else ", ".join(over)), "model")
            elif stop:
                add("Downstream extent criterion (5.1.1(f)) met", "Check",
                    "Edges are below the tolerance, but the domain stopped growing where no "
                    "receptor lies ahead{}; confirm nothing lies beyond".format(
                        " or where the class-stop rule accepted the edge"
                        if any(w.startswith("Class-stop rule") for w in stop) else ""),
                    "model")
            else:
                add("Downstream extent criterion (5.1.1(f)) met", "Yes",
                    "{} run(s); the flood stays inside the last domain (edge depth below {} ft)"
                    .format(len(runs), fmt_val(self.settings.get("edge_tolerance_ft", 1.0))),
                    "model")
        else:
            add("Downstream extent criterion (5.1.1(f)) met", "No",
                "no model run record found")
        cons = self.find_consequences() if self.opt.include_consequences else \
            (f.glob("consequences*.csv") + f.glob("consequences*.md"))
        inc = rep.get("incremental")
        if self.flood_mode:
            base_ok = bool(f.find("Baseline Depth (Max).tif", "")) or bool(inc)
            add("Without-breach (baseline) flood modelled", "Yes" if base_ok else "No",
                "baseline rasters found" if base_ok else "no baseline results", "flood")
        else:
            add("Without-breach (baseline) flood modelled", "N/A", "sunny-day scenario")
        if inc and rep.get("receptors_incremental"):
            add("Incremental consequences computed", "Yes",
                "Incremental area and receptors are tabulated", "flood")
        elif inc:
            add("Incremental consequences computed", "Check",
                "Incremental area is tabulated; there were no receptor layers, so incremental "
                "receptors are not counted", "flood")
        elif self.flood_mode:
            add("Incremental consequences computed", "No", "no incremental results")
        else:
            add("Incremental consequences computed", "N/A",
                "sunny-day scenario; no baseline to subtract")
        add("Consequences documented (consequences*.csv / .md)", "Yes" if cons else "No",
            ", ".join(os.path.basename(c) for c in cons[:3]) if cons else "no such file found",
            "receptors")
        book = find_mapbook(self.folder) if self.folder else ""
        prev = self.eap_preview()
        if book:
            add("EAP inundation map produced", "Yes", "ND Dam Hazard map book: {}".format(
                os.path.relpath(book, self.folder).replace(os.sep, "/")), "results")
        else:
            add("EAP inundation map produced", "Yes" if prev else "No",
                "{} (this tool's preview; the EAP map is ND Dam Hazard's map book, "
                "outputs/EAP/EAP_Map_Book.pdf)".format(os.path.basename(prev)) if prev
                else "not found (neither ND Dam Hazard's EAP_Map_Book.pdf nor this tool's "
                     "preview)", "results")
        have = [n for n, *_ in FINAL_FILES[:4] if f.find(n, "")]
        missing = [n for n, *_ in FINAL_FILES[:4] if n not in have]
        add("Depth, velocity, arrival-time and WSE rasters produced",
            "Yes" if not missing else ("Check" if have else "No"),
            "all four present" if not missing else "missing: " + ", ".join(missing), "results")
        vec = [n for n in ("Flood Polygons (Max Depth).geojson", "Flood Extent.geojson",
                           "Arrival Zones.geojson", "Breach Products.gpkg") if f.find(n, "")]
        add("Flood polygons and arrival zones produced", "Yes" if len(vec) >= 2 else
            ("Check" if vec else "No"), ", ".join(vec) if vec else "no vector products found",
            "results")
        add("Receptors reached listed with depth, velocity and arrival",
            "Yes" if f.find("receptors_flooded.csv") else "No",
            "receptors_flooded.csv" if f.find("receptors_flooded.csv") else
            "no receptor layers were used or none were flooded", "receptors")
        err = ((runs[-1].get("stats") or {}).get("max_volume_error_pct") if runs else None)
        add("Model mass balance within 1%", "No" if err is None else (
            "Yes" if err <= 1.0 else "Check"),
            "not recorded" if err is None else "maximum volume error {}%".format(fmt_val(err)),
            "model")
        add("Run warnings resolved or explained", "Check" if self.warnings else (
            "Yes" if rep else "No"),
            "{} warning(s) are listed".format(len(self.warnings)) if self.warnings else
            ("none recorded" if rep else "no run report"), "breach")
        sd = self.files.subdir("Sensitivity")
        add("Sensitivity to the breach parameters evaluated",
            "Yes" if sd and _Files(sd).find("Sensitivity_Summary.csv", "") else "No",
            "Sensitivity folder found" if sd else "no Sensitivity folder", "sensitivity")
        cmp_ok = bool(rep.get("comparison")) or bool(self.files.subdir("_benchmark"))
        add("Cross-check with a second model (ANUGA or HEC-RAS benchmark)",
            "Yes" if cmp_ok else "No", "comparison found" if cmp_ok else "not run (optional)",
            "model")
        st = rep.get("structures")
        add("Culverts, bridges and embankments represented",
            "Yes" if st else "Check", "{} structure(s) modelled".format(len(st)) if st else
            "no structure layer was supplied; confirm none lie in the flow path", "model")
        add("Cascade failure of downstream dams considered",
            "Yes" if self.find_cascade() else "Check",
            "Cascade report found" if self.find_cascade() else
            "no cascade run found; confirm no downstream dam is affected", "sensitivity")
        add("Run manifest with software versions and input hashes",
            "Yes" if f.find("run_manifest.json") else "No",
            "run_manifest.json" if f.find("run_manifest.json") else "not found", "repro")
        if self.opt.include_consequences:
            hz = self.find_hazard()
        else:
            jp, mp = f.find(HAZARD_FILES["json"], ""), f.find(HAZARD_FILES["markdown"], "")
            hz = (jp, mp, _load_json(jp)) if (jp or mp) else ()
        hzd = hz[2] if hz and isinstance(hz[2], dict) else {}
        if hzd.get("hazard_class"):
            ev = "class {}; {}; {} data gap(s)".format(
                str(hzd["hazard_class"]).upper(), hzd.get("lethality_label") or
                (hzd.get("settings") or {}).get("lethality", ""), len(hzd.get("data_gaps") or []))
        elif hz:
            ev = os.path.basename(hz[0] or hz[1])
        else:
            ev = "not run (ND hazard classification tool)"
        add("Hazard classification screened (ND 5.2)", "Yes" if hz else "No", ev, "receptors")
        cls_dir = self.files.subdir("_classification")
        add("Hazard classification made by the engineer", "Yes" if cls_dir and self.files.ls(
            cls_dir) else "Check", "results in _classification" if cls_dir else
            ("screening class {} from this tool; the classification is the engineer's".format(
                str(hzd.get("hazard_class")).upper()) if hzd.get("hazard_class") else
             "this tool gives a screening class only"))
        add("Engineer of record reviewed and signed", "Check", "to be completed")
        return items

    def sec_appendix(self):
        d, wrote = self.doc, False
        if self.opt.include_checklist:
            self.sub("Reviewer checklist")
            d.paragraph("Status is filled from what the dam folder contains: Yes (found or "
                        "met), Check (present but needs the reviewer's judgement), No (not "
                        "found) or N/A. The last two columns are for the reviewer.",
                        size=9, color="595959")
            rows = [["#", "Item", "Status", "Evidence", "Reviewer OK / comment"]]
            for n, (item, status, ev) in enumerate(self.checklist_items(), 1):
                rows.append([str(n), item, {"text": status, "bold": True, "align": "center",
                                            "shade": STATUS_SHADES[status.lower()]}, ev, ""])
            d.table(rows, widths=[0.3, 2.0, 0.6, 2.4, 1.2], font_pt=8.5,
                    caption="Reviewer checklist")
            wrote = True
        if self.opt.include_file_list:
            self.sub("Files in the dam folder")
            rows = self.file_rows()
            if rows:
                d.table([["File", "Size", "What it is"]] + rows, widths=[3.3, 0.8, 2.4],
                        font_pt=8, align=[None, "right", None], caption="Files in {}".format(
                            os.path.basename(self.folder)))
                wrote = True
        if self.opt.include_run_report:
            md = self.files.find("run_report.md")
            if md:
                self.sub("Run report as written by the tool")
                add_markdown(d, _read_text(md), heading_shift=1, skip_title=True)
                wrote = True
        return wrote

    def file_rows(self, cap=250):
        rows = []
        root = self.folder
        me = os.path.abspath(self.out_path) if self.out_path else ""

        def desc(name):
            low = name.lower()
            if low in FILE_DESC:
                return FILE_DESC[low]
            if low.startswith("gauge_") and low.endswith(".csv"):
                return "Discharge past a gauge"
            if low.startswith("consequences"):
                return "Consequences (from the consequences module)"
            if low.startswith("sensitivity") and low.endswith(".tif"):
                return "Sensitivity envelope raster"
            if low.endswith(".docx"):
                return "Word report"
            if low.endswith(".tif") and low.split("_")[0] in ("lisflood", "anuga", "diff"):
                return "Model raster (QA)"
            return ""

        def walk(d, depth):
            try:
                names = sorted(os.listdir(d), key=lambda n: (
                    os.path.isdir(os.path.join(d, n)), n.lower()))
            except OSError:
                return
            for n in names:
                p = os.path.join(d, n)
                rel = os.path.relpath(p, root).replace(os.sep, "/")
                if n.startswith(".") or n == "__pycache__" or os.path.abspath(p) == me \
                        or n.endswith(".tmp"):
                    continue
                if os.path.isdir(p):
                    if depth >= 1 or n.lower() in ("lisflood", "lisflood_base", "anuga",
                                                   "anuga_base"):
                        cnt, tot = 0, 0
                        for r, _ds, fs in os.walk(p):
                            for x in fs:
                                cnt += 1
                                try:
                                    tot += os.path.getsize(os.path.join(r, x))
                                except OSError:
                                    pass
                        rows.append([rel + "/", _fmt_bytes(tot), "{} file(s)".format(cnt)])
                    else:
                        walk(p, depth + 1)
                else:
                    try:
                        size = _fmt_bytes(os.path.getsize(p))
                    except OSError:
                        size = ""
                    rows.append([rel, size, desc(n)])
        walk(root, 0)
        if len(rows) > cap:
            extra = len(rows) - cap
            rows = rows[:cap] + [["... and {} more".format(extra), "", ""]]
        return rows

    # -- assembly --------------------------------------------------------------------------
    def run_section(self, key, title, fn, enabled=True):
        if not enabled:
            return
        d = self.doc
        mark = d.mark()
        pb = self._break_before_next
        try:
            head = d.heading(title, 1, page_break_before=pb)
            wrote = fn()
        except Exception as e:                    # a section must never sink the report
            d.rollback(mark)
            head = d.heading(title, 1, page_break_before=pb)
            d.paragraph("This section could not be generated ({}: {}).".format(
                type(e).__name__, str(e)[:200]), italic=True)
            self.res.warnings.append("{}: {}: {}".format(title, type(e).__name__, e))
            wrote = True
        if not wrote:
            d.rollback(mark)
            return
        self._break_before_next = False
        num = head.split(" ", 1)[0]
        self.secnum[key] = num
        self.res.sections.append((num, head.split(" ", 1)[1] if " " in head else head))

    def build(self):
        d, opt = self.doc, self.opt
        try:
            info = [("Dam", self.dam_name or self.dam_id), ("Dam ID", self.dam_id),
                    ("Project", opt.project), ("Prepared by", opt.engineer),
                    ("Firm", opt.firm), ("Date", self.date),
                    ("Scenario", None if not self.rep else (
                        "Flood inflow (PMF), with and without the breach" if self.flood_mode
                        else "Sunny day"))]
            d.title_block(opt.title, self.dam_label if self.dam_name else "", info,
                          logo=opt.logo if opt.logo and os.path.exists(opt.logo) else None,
                          note="Generated from the files in the dam folder by ND Dam Breach "
                               "Runner. It reports what the model run produced and needs the "
                               "review and signature of the engineer of record.")
        except Exception as e:
            self.res.warnings.append("cover page: {}".format(e))
        if opt.include_toc:
            d.toc(levels="1-2")
            self._break_before_next = True      # a break paragraph after the field can strand a page
        # sections are numbered as they are written, so a skipped one leaves no gap
        self.run_section("summary", "Summary of results", self.sec_summary)
        self.run_section("dam", "Dam and reservoir data", self.sec_dam)
        self.run_section("breach", "Breach parameters and method", self.sec_breach)
        self.run_section("flood", "Flood scenario", self.sec_flood, enabled=bool(self.rep))
        self.run_section("model", "Hydraulic model", self.sec_model)
        self.run_section("results", "Results", self.sec_results)
        self.run_section("receptors", "Receptors and impacts", self.sec_receptors)
        self.run_section("sensitivity", "Sensitivity, cascade and probabilistic results",
                         self.sec_sensitivity, enabled=opt.include_sensitivity)
        self.run_section("assumptions", "Assumptions and limitations", self.sec_assumptions,
                         enabled=opt.include_assumptions)
        self.run_section("repro", "Reproducibility", self.sec_repro,
                         enabled=opt.include_reproducibility)
        self.run_section("appendix", "Appendix", self.sec_appendix,
                         enabled=opt.include_checklist or opt.include_file_list
                         or opt.include_run_report)
        for name in ("run_report.json", "hydrograph.svg",
                     "receptors_flooded.csv", "run_manifest.json") + tuple(
                         n for n, *_ in FINAL_FILES[:4]):
            if not (self.files.find(name) or self.files.find(name.replace(".svg", ".png"))):
                self.res.missing.append(name)
        if not (self.eap_preview() or (self.folder and find_mapbook(self.folder))):
            self.res.missing.append("EAP map (ND Dam Hazard's outputs/EAP/EAP_Map_Book.pdf or "
                                    "this tool's {})".format(EAP_PREVIEW_SVG))
        d.save(self.out_path)
        self.res.path = self.out_path
        try:
            ins = inspect_docx(self.out_path)
            self.res.counts = {"headings": len(ins["headings"]), "tables": ins["tables"],
                               "images": ins["images"], "paragraphs": ins["paragraphs"]}
        except Exception:
            pass
        return self.res

    def cleanup(self):
        if self.tmp:
            shutil.rmtree(self.tmp, ignore_errors=True)


def build_submittal(folder, dam_id, out_path, options=None):
    """Write a Word submittal report for the dam folder `folder` to `out_path`.

    Reads what the tool writes there (see the module docstring and the README):
    `_breach_model/run_report.json` (and `.md`), `breach_hydrograph.csv`,
    `hydrograph.svg`, `gauges.svg`, `gauge_*.csv`, `receptors_flooded.csv`, the
    result rasters' names, `Flood Polygons (Max Depth).geojson`, `Arrival Zones.geojson`,
    `EAP Inundation Map (preview).svg` (3.1.x: `EAP Inundation Map.svg`; ND Dam Hazard's
    `outputs/EAP/EAP_Map_Book*.pdf` is listed as the EAP map when present),
    `Sensitivity/Sensitivity_Summary.csv` and
    `Sensitivity_Report.html`, a sibling `Cascade_*/Cascade_Report.md`, and, written by
    other modules, `consequences*.csv|md`, `Hazard Classification (ND 5.2).json|md` with
    `ND Fig 5.1 Houses.svg` / `ND Fig 5.2 Mobile Homes.svg`, `run_manifest.json` and
    `probabilistic*|uncertainty*.csv|md`. Whatever is missing is skipped. Returns a
    `SubmittalResult` (path, sections, figures, missing, warnings, counts); only an
    unwritable `out_path` raises."""
    opt = SubmittalOptions.from_any(options)
    rp = _Report(folder, dam_id, out_path, opt)
    if not folder or not os.path.isdir(folder):
        rp.res.warnings.append("The dam folder {!r} does not exist; the report has no "
                               "content from it.".format(folder))
    try:
        return rp.build()
    finally:
        rp.cleanup()
