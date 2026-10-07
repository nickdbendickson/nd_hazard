# -*- coding: utf-8 -*-
"""Submittal report: a QGIS print layout (kept in the project's Layout
Manager so it can be edited and re-exported) exported to PDF.

Pages (US Letter, landscape):
  1. classification summary: ND Dam Safety Standards REG_05.2026 Sec. 5.2
     criteria table, ACER TM 11 Table 1 (lives in jeopardy), scenarios,
     items requiring engineering judgment
  2. overview map of the study area with legend and scale bar
  3..n. zoom maps of the areas with the most habitable structures in the
     ACER high / judgment zones (up to `max_pages`)
  then (v2.20.0): the depth-velocity charts, houses on foundations (ND Fig.
  5.1) and mobile homes (ND Fig. 5.2), with every flooded habitable
  structure plotted (dvsheet.py; the same structures as the spreadsheet copy)
  then: roads / railroads table, structures table (ND outcome and ACER
  zone of every flooded habitable structure)

The layout is rebuilt on every run (the previous one of the same name is
replaced), so the PDF and the layout always show the latest results.

v2.9: aerial imagery under every map (imagery=True): the imagery layer of
basemap.py is the LAST layer of each map item's explicit layer list (drawn
under everything) and an "Imagery: <service>" credit is printed under the
map. The depth raster is drawn semi-transparent in those map items only
(FLOOD_OPACITY_OVER_IMAGERY, a locked style of the map item - the layer and
the canvas are not changed). Offline (the "offline only" setting, or the
imagery service does not answer) the maps are made without imagery and the
map pages say so.
"""

import math
import os
import time

from qgis.PyQt.QtGui import QFont
from qgis.core import (
    Qgis, QgsProject, QgsPrintLayout, QgsLayoutItemMap, QgsLayoutItemLabel, QgsLayoutItemLegend,
    QgsLayoutItemScaleBar, QgsLayoutItemPage, QgsLayoutPoint, QgsLayoutSize,
    QgsLayoutExporter, QgsRectangle, QgsUnitTypes, QgsRasterLayer, QgsLayoutItemPicture,
)

from .compat import _first, enum_value, DIST_FEET, DIST_MILES
from .constants import (
    PLUGIN_NAME, PLUGIN_VERSION, CLASS_HIGH, CLASS_JUDGMENT, CLASS_LOW, ZONE_LABELS,
    ND_POLICY_REF, TM11_REF,
)
from .helpers import log_message, prepare_output_path, same_path, add_layer_to_project
from .styles import style_depth
from .structures import OUT_DRY
from . import basemap as BM

PAGE_W, PAGE_H = 279.4, 215.9          # US Letter landscape, mm
MARGIN = 10.0
REPORT_FILE = "Hazard_Classification_Report.pdf"
_KEEP = []                              # layers referenced by open layouts
# raster flood layers (depth, arrival bands) over aerial imagery: this opacity
# in the map items (a more transparent style of the user's is kept)
FLOOD_OPACITY_OVER_IMAGERY = 0.65

STRUCT_HEAD = ["ID", "FEMA category", "ACER type", "ACER zone", "ND outcome (5.2.1.1)",
               "Depth ft", "Vel ft/s", "DxV ft2/s", "Arrival h", "Persons", "Est. LOL",
               "Reviewed"]
ROAD_HEAD = ["Road / railroad", "Class", "AADT or trains/day", "Flooded ft", "Depth ft",
             "Vel ft/s", "Vehicle zone", "ND class", "ND basis"]
ND_HEAD = ["Section", "Criterion", "Found", "Result"]
TM_HEAD = ["ACER TM 11 Table 1", "Value"]
SCEN_HEAD = ["Scenario", "ND class", "TM 11 class", "LIJ", "Zone method", "Baseline"]
# relative column widths (text wraps inside a column; see _fit_widths)
STRUCT_WIDTHS = [10, 30, 28, 18, 38, 14, 14, 16, 14, 14, 14, 22]
ROAD_WIDTHS = [58, 22, 30, 18, 14, 14, 20, 16, 64]
ND_WIDTHS = [14, 102, 32, 22]
TM_WIDTHS = [63, 18]
SCEN_WIDTHS = [24, 12, 12, 8, 14, 12]


def _fit_widths(table, weights, total):
    """Column widths (mm) from relative `weights` so that the WHOLE table -
    text columns plus QGIS's cell margins (both sides of every column) and
    grid lines - is `total` mm wide (QGIS sizes the frame to the table)."""
    n = len(weights)
    cm, gs = 1.0, 0.5                      # QGIS defaults
    try:
        cm = float(table.cellMargin())
    except Exception:
        pass
    try:
        gs = float(table.gridStrokeWidth()) if table.showGrid() else 0.0
    except Exception:
        pass
    avail = max(n * 4.0, total - n * 2.0 * cm - (n + 1) * gs - 0.5)
    s = float(sum(weights)) or 1.0
    return [w * avail / s for w in weights]


def _mm():
    return _first(lambda: Qgis.LayoutUnit.Millimeters,
                  lambda: QgsUnitTypes.LayoutUnit.LayoutMillimeters,
                  lambda: QgsUnitTypes.LayoutMillimeters)


def _landscape():
    return _first(lambda: QgsLayoutItemPage.Orientation.Landscape,
                  lambda: QgsLayoutItemPage.Landscape)


def _export_ok(res):
    ok = _first(lambda: QgsLayoutExporter.ExportResult.Success,
                lambda: QgsLayoutExporter.Success, default=0)
    return enum_value(res) == enum_value(ok)


def _place(item, x, y, w, h, page=0):
    mm = _mm()
    item.attemptResize(QgsLayoutSize(w, h, mm))
    item.attemptMove(QgsLayoutPoint(x, y, mm), True, False, page)


def _text_format(size, bold=False, mono=False):
    font = QFont("Courier New" if mono else "Arial")
    font.setBold(bold)
    try:
        from qgis.core import QgsTextFormat
        fmt = QgsTextFormat()
        fmt.setFont(font)
        fmt.setSize(size)
        return fmt, font
    except Exception:
        return None, font


def _label(layout, text, x, y, w, h, page=0, size=9, bold=False, mono=False):
    lab = QgsLayoutItemLabel(layout)
    lab.setText(text)
    fmt, font = _text_format(size, bold, mono)
    try:
        if fmt is None:
            raise AttributeError("no QgsTextFormat")
        lab.setTextFormat(fmt)
    except Exception:
        try:
            font.setPointSizeF(size)
            lab.setFont(font)
        except Exception:
            pass
    layout.addLayoutItem(lab)
    _place(lab, x, y, w, h, page)
    return lab


def _map(layout, crs, extent, layers, x, y, w, h, page=0, styles=None):
    """A map item with an explicit layer list (top first) and, with
    `styles` ({layer id: style XML}, see flood_style_overrides), those
    layers drawn with the given styles in this item only."""
    m = QgsLayoutItemMap(layout)
    m.setCrs(crs)
    m.setLayers(layers)
    try:
        m.setKeepLayerSet(True)
    except Exception:
        pass
    if styles:
        try:
            m.setLayerStyleOverrides(styles)
            m.setKeepLayerStyles(True)
        except Exception as e:
            log_message("Map layer styles not locked ({}); flood layers drawn with their "
                        "own opacity".format(e))
    m.setFrameEnabled(True)
    layout.addLayoutItem(m)
    _place(m, x, y, w, h, page)
    m.setExtent(_fit(extent, w / h))
    return m


def flood_style_overrides(layers, opacity=FLOOD_OPACITY_OVER_IMAGERY):
    """{layer id: style XML} that draws the raster layers of `layers` (depth,
    arrival bands - opaque colour ramps) at `opacity`, so aerial imagery shows
    through them. For QgsLayoutItemMap.setLayerStyleOverrides (the "lock
    styles for layers" of a map item): the layers themselves keep their
    style - the opacity is set only while the style is read, then put back.
    Vector layers (points, lines) are left alone. Never raises."""
    out = {}
    try:
        from qgis.core import QgsMapLayerStyle
    except ImportError:
        return out
    for lyr in layers or []:
        if not isinstance(lyr, QgsRasterLayer):
            continue
        try:
            r = lyr.renderer()
            if r is None:
                continue
            prev = float(r.opacity())
            if prev <= opacity + 1e-6:
                continue                     # already see-through enough
            r.setOpacity(float(opacity))
            try:
                st = QgsMapLayerStyle()
                st.readFromLayer(lyr)
                xml = st.xmlData()
            finally:
                r.setOpacity(prev)
            if xml:
                out[lyr.id()] = xml
        except Exception as e:
            log_message("Map opacity of {} not changed: {}".format(lyr.name(), e))
    return out


def imagery_label(layout, used, caption, note, x, y, w, page, size=6):
    """Under a map: the imagery credit, or why the map has no imagery
    (nothing when imagery was not asked for)."""
    if used and caption:
        text = caption
    elif note:
        text = "Aerial imagery not shown: {}".format(note)
    else:
        return None
    return _label(layout, text, x, y, w, 5, page, size=size)


def layout_imagery(imagery, layers, add_to_project, rect, crs):
    """(map layers with the imagery last, style overrides or None, used,
    caption, note) for the maps of a layout. imagery=False: the layers as
    they are, no note."""
    if not imagery:
        return list(layers), None, False, "", ""
    img, cap, note = BM.layout_imagery(add_to_project, rect, crs)
    map_layers = BM.with_imagery(layers, img)
    used = img is not None and bool(map_layers) and map_layers[-1] is img
    if img is not None and not used:
        note = "the maps have no layers"
    if not used:
        return list(layers), None, False, "", note
    _KEEP.append(img)
    return map_layers, flood_style_overrides(layers), True, cap or "", ""


def _fit(rect, aspect):
    """rect grown to the map item's aspect ratio (and never degenerate)."""
    w = max(rect.width(), 1e-9)
    h = max(rect.height(), 1e-9)
    cx = (rect.xMinimum() + rect.xMaximum()) / 2.0
    cy = (rect.yMinimum() + rect.yMaximum()) / 2.0
    if w / h > aspect:
        h = w / aspect
    else:
        w = h * aspect
    return QgsRectangle(cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0)


def _add_page(layout):
    page = QgsLayoutItemPage(layout)
    page.setPageSize("Letter", _landscape())
    layout.pageCollection().addPage(page)
    return layout.pageCollection().pageCount() - 1


def _decorations(layout, m, layers, page):
    try:
        leg = QgsLayoutItemLegend(layout)
        leg.setTitle("Legend")
        leg.setLinkedMap(m)
        try:
            leg.setAutoUpdateModel(False)
            root = leg.model().rootGroup()
            root.removeAllChildren()
            for lyr in layers:
                root.addLayer(lyr)
        except Exception:
            pass
        layout.addLayoutItem(leg)
        _place(leg, PAGE_W - MARGIN - 62, 30, 62, 100, page)
    except Exception as e:
        log_message("Report legend skipped: {}".format(e))
    try:
        sb = QgsLayoutItemScaleBar(layout)
        sb.setStyle("Single Box")
        sb.setLinkedMap(m)
        # US customary units for an ND submittal: miles for large maps
        wide = False
        try:
            from .helpers import buffer_distance_in_layer_units
            wide = m.extent().width() > buffer_distance_in_layer_units(3 * 5280.0, m.crs())
        except Exception:
            pass
        units = DIST_MILES if wide and DIST_MILES is not None else DIST_FEET
        sb.applyDefaultSize(units)
        try:
            sb.setUnitLabel("mi" if units is DIST_MILES else "ft")
        except Exception:
            pass
        layout.addLayoutItem(sb)
        _place(sb, MARGIN + 2, PAGE_H - MARGIN - 14, 80, 12, page)
    except Exception as e:
        log_message("Report scale bar skipped: {}".format(e))


def _clusters(structs, cell, max_n):
    """Groups of habitable high / judgment structures (grid cells), largest
    first: [(bbox, [recs])]."""
    groups = {}
    for s in structs:
        if s.get("habitable") != "Yes" or s.get("zone") not in (CLASS_HIGH, CLASS_JUDGMENT):
            continue
        if s.get("outcome") == OUT_DRY:
            continue
        try:
            p = s["geom"].centroid().asPoint()
        except Exception:
            continue
        key = (int(math.floor(p.x() / cell)), int(math.floor(p.y() / cell)))
        groups.setdefault(key, []).append((s, p))
    out = []
    for items in sorted(groups.values(), key=lambda g: -len(g))[:max_n]:
        xs = [p.x() for _s, p in items]
        ys = [p.y() for _s, p in items]
        pad = cell * 0.25
        out.append((QgsRectangle(min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad),
                    [s for s, _p in items]))
    return out


def _f(v, fmt="{:.1f}"):
    return "" if v is None else fmt.format(v)


def report_structures(structs):
    """Structures listed in the report: every flooded habitable structure
    (ND 5.2.2 counts them all) and unverified ones in the high / judgment
    zones; most severe first."""
    out = []
    for s in structs:
        if s.get("outcome") in (None, OUT_DRY):
            continue
        if s.get("habitable") == "Yes" or (
                s.get("habitable") == "Unverified" and
                s.get("zone") in (CLASS_HIGH, CLASS_JUDGMENT)):
            out.append(s)
    rank = {CLASS_HIGH: 0, CLASS_JUDGMENT: 1, CLASS_LOW: 2}
    return sorted(out, key=lambda r: (rank.get(r.get("zone"), 3),
                                      -(r.get("max_depth") or 0), r.get("id", 0)))


def _structure_rows(recs, incremental):
    rows = []
    for s in recs:
        row = [str(s["id"]), (s.get("fema_category") or "")[:40], s.get("acer_type") or "",
               ZONE_LABELS.get(s.get("zone"), "") if s.get("zone") else "",
               s.get("outcome") or "", _f(s.get("max_depth")), _f(s.get("max_vel")),
               _f(s.get("dv_max")), _f(s.get("arrival"), "{:.2f}"),
               str(int(s.get("persons") or 0)), _f(s.get("lol"), "{:.2f}"),
               "Yes" if s.get("reviewed") else ""]
        if incremental:
            row.append("Yes" if s.get("incremental") else "No")
        rows.append(row)
    return rows


def _line_rows(roads, rails):
    rows = []
    for r in roads:
        a = "{:,.0f}{}".format(r["aadt"], " ({})".format(r["aadt_year"])
                               if r.get("aadt_year") else "") if r.get("aadt") else "no AADT"
        rows.append([r.get("name") or "unnamed road", r.get("road_class") or "", a,
                     _f(r.get("length_ft"), "{:,.0f}"), _f(r.get("max_depth")),
                     _f(r.get("max_vel")), ZONE_LABELS.get(r.get("vehicle_zone"), "") or "",
                     r.get("nd_class") or "", (r.get("nd_basis") or "")[:60]])
    for r in rails:
        t = "{:.0f} trains/day".format(r["trains"]) if r.get("trains") is not None else "?"
        rows.append(["{} {}".format(r.get("railroad") or "railroad",
                                    r.get("subdivision") or "").strip(),
                     "passenger" if r.get("passenger") else "rail", t,
                     _f(r.get("length_ft"), "{:,.0f}"), _f(r.get("max_depth")),
                     _f(r.get("max_vel")), "", r.get("nd_class") or "",
                     (r.get("nd_basis") or "")[:60]])
    return rows


def _columns(head, widths):
    from qgis.core import QgsLayoutTableColumn
    cols = []
    for i, c in enumerate(head):
        col = QgsLayoutTableColumn(c)
        if widths and i < len(widths):
            try:
                col.setWidth(float(widths[i]))       # mm; 0 = automatic
            except Exception:
                pass
        cols.append(col)
    return cols


def _table_options(t):
    """Headers on every page of a table that flows over several pages, and
    long texts wrapped inside their column instead of widening the table."""
    from qgis.core import QgsLayoutTable
    try:
        t.setHeaderMode(_first(lambda: Qgis.LayoutTableHeaderMode.AllFrames,
                               lambda: QgsLayoutTable.HeaderMode.AllFrames,
                               lambda: QgsLayoutTable.AllFrames))
    except Exception:
        pass
    try:
        t.setWrapBehavior(_first(lambda: QgsLayoutTable.WrapBehavior.WrapText,
                                 lambda: QgsLayoutTable.WrapText))
    except Exception:
        pass


def _table(layout, head, rows, page, x, y, w, h, size=7, repeat=True, title=None,
           widths=None):
    """A text table (flows onto extra pages when `repeat`, headers repeated),
    or a monospaced label if the table API is not available. `widths`:
    relative column widths; the table is made exactly `w` mm wide."""
    try:
        from qgis.core import QgsLayoutItemTextTable, QgsLayoutFrame, QgsLayoutMultiFrame
        t = QgsLayoutItemTextTable(layout)
        layout.addMultiFrame(t)
        t.setColumns(_columns(head, _fit_widths(t, widths, w) if widths else None))
        t.setContents(rows)
        _table_options(t)
        for setter, bold in (("setHeaderTextFormat", True), ("setContentTextFormat", False)):
            fmt, _font = _text_format(size, bold)
            try:
                if fmt is not None:
                    getattr(t, setter)(fmt)
            except Exception:
                pass
        try:
            mode = _first(lambda: QgsLayoutMultiFrame.ResizeMode.RepeatUntilFinished,
                          lambda: QgsLayoutMultiFrame.RepeatUntilFinished) if repeat else \
                _first(lambda: QgsLayoutMultiFrame.ResizeMode.UseExistingFrames,
                       lambda: QgsLayoutMultiFrame.UseExistingFrames)
            t.setResizeMode(mode)
        except Exception:
            pass
        fr = QgsLayoutFrame(layout, t)
        _place(fr, x, y, w, h, page)
        t.addFrame(fr)
        return True
    except Exception as e:
        log_message("Report table{}: text fallback ({})".format(
            " '{}'".format(title) if title else "", e))
        width = max(8, int(w / (len(head) * 1.6)))
        text = "  ".join("{:<{w}}".format(c[:width], w=width) for c in head) + "\n" + \
            "\n".join("  ".join("{:<{w}}".format(str(c)[:width], w=width) for c in r)
                      for r in rows[:45])
        if len(rows) > 45:
            text += "\n... {} more rows (see the GeoPackage outputs)".format(len(rows) - 45)
        _label(layout, text, x, y, w, h, page, size=6, mono=True)
        return False


def _depth_layer(depth, scenario, add_to_project, group=None):
    """The depth raster for the maps. A layout kept in the project needs a
    layer that is IN the project (otherwise the saved layout loses it): an
    existing project layer of the same file is reused, else the raster is
    added (styled blue)."""
    if depth is None or not depth.isValid():
        return None
    if not add_to_project:
        return depth
    src = depth.source().split("|")[0]
    for lyr in QgsProject.instance().mapLayers().values():
        try:
            if isinstance(lyr, QgsRasterLayer) and same_path(lyr.source().split("|")[0], src):
                return lyr
        except Exception:
            continue
    depth.setName("Max depth ({})".format(scenario.name))
    style_depth(depth)
    add_layer_to_project(depth, group=group)
    return depth


def _summary_page(layout, dam_name, res, scenario, all_results):
    nd, tm11 = res["nd"], res["tm11"]
    _label(layout, "{} - dam hazard classification (screening)".format(dam_name),
           MARGIN, MARGIN, PAGE_W - 2 * MARGIN, 9, 0, size=14, bold=True)
    sub = "ND hazard: {}    ACER TM 11: {} (lives in jeopardy {:.1f})    governing " \
          "scenario: {}{}".format(nd["cls"].upper(), tm11["cls"].upper(), tm11["lij"],
                                 scenario.name,
                                 "    (incremental - consequences the breach adds)"
                                 if res.get("incremental") else "")
    _label(layout, sub, MARGIN, 20, PAGE_W - 2 * MARGIN, 6, 0, size=9, bold=True)
    left_w = 170.0
    _label(layout, "North Dakota Dam Safety Standards - " + ND_POLICY_REF, MARGIN, 28,
           left_w, 5, 0, size=8, bold=True)
    crit = nd.get("criteria") or []
    rows = [[c["section"], c["criterion"], c["found"], c["result"]] for c in crit]
    # v2.3.1: repeat=True so extra rows don't silently clip in PDF
    _table(layout, ND_HEAD, rows, 0, MARGIN, 34, left_w, 100, size=7, repeat=True,
           title="ND criteria", widths=ND_WIDTHS)
    rx = MARGIN + left_w + 6
    rw = PAGE_W - MARGIN - rx
    _label(layout, TM11_REF + " - Table 1", rx, 28, rw, 5, 0, size=8, bold=True)
    from .classification import tm11_rows
    _table(layout, TM_HEAD, [list(r) for r in tm11_rows(tm11)], 0, rx, 34, rw, 52, size=7,
           repeat=False, title="TM 11", widths=TM_WIDTHS)
    breaches = [(name, r) for name, r in (all_results or {}).items() if r.get("nd")]
    if len(breaches) > 1:
        _label(layout, "Scenarios (the most severe governs)", rx, 90, rw, 5, 0, size=8,
               bold=True)
        srows = [[name, r["nd"]["cls"], r["tm11"]["cls"], "{:.1f}".format(r["tm11"]["lij"]),
                  r.get("method") or "", (r.get("incremental") or {}).get("baseline", "")]
                 for name, r in breaches]
        if len(srows) > 8:
            log_message("Report: {} scenario rows exceed frame - table continues "
                        "on extra page".format(len(srows)))
        _table(layout, SCEN_HEAD, srows, 0, rx, 96, rw, 40, size=6, repeat=True,
               title="scenarios", widths=SCEN_WIDTHS)
    judg = nd.get("judgment") or []
    margin = (res.get("margin") or {}).get("lines") or []
    text = ("Classification margin (5.3(f)): " + " ".join(margin[:2]) + "\n\n"
            if margin else "")
    nj = 7 if margin else 9                  # the margin text takes two lines
    text += "Items requiring engineering judgment:\n" + "\n".join(
        "- " + j for j in judg[:nj]) + ("\n- ... {} more in Hazard_Classification_Summary.txt"
                                       .format(len(judg) - nj) if len(judg) > nj else "")
    _label(layout, text, MARGIN, 140, PAGE_W - 2 * MARGIN, PAGE_H - 140 - MARGIN - 8, 0,
           size=7)
    _label(layout, "{} v{} - {} - screening result; the engineer remains responsible for the "
           "classification".format(PLUGIN_NAME, PLUGIN_VERSION, time.strftime("%Y-%m-%d")),
           MARGIN, PAGE_H - MARGIN - 6, PAGE_W - 2 * MARGIN, 5, 0, size=6)


def _dv_page(layout, dv):
    """The two depth-velocity charts (SVG from dvsheet.py) side by side, with
    how to read them. Nothing when the charts were not made."""
    svg = dv.get("svg") or {}
    if not svg:
        return
    from . import dvsheet as DV
    rows = dv.get("rows") or []
    n_h = sum(1 for r in rows if r["kind"] == DV.HOUSE)
    p = _add_page(layout)
    _label(layout, "Depth-velocity charts - flooded habitable structures ({} house(s) on "
           "foundation, {} mobile home(s))".format(n_h, len(rows) - n_h), MARGIN, MARGIN,
           PAGE_W - 2 * MARGIN, 8, p, size=11, bold=True)
    gap = 6.0
    w = (PAGE_W - 2 * MARGIN - gap) / 2.0
    h = w * 560.0 / 900.0
    for i, kind in enumerate((DV.HOUSE, DV.MOBILE)):
        path = svg.get(kind)
        if not path or not os.path.isfile(path):
            continue
        pic = QgsLayoutItemPicture(layout)
        pic.setPicturePath(path)
        layout.addLayoutItem(pic)
        _place(pic, MARGIN + i * (w + gap), 22, w, h, p)
    close = dv.get("close_up") or {}
    y_notes = 22 + h + 4
    if close:
        for i, kind in enumerate(k for k in (DV.HOUSE, DV.MOBILE) if close.get(k)):
            if not os.path.isfile(close[kind]):
                continue
            pic = QgsLayoutItemPicture(layout)
            pic.setPicturePath(close[kind])
            layout.addLayoutItem(pic)
            _place(pic, MARGIN + i * (w + gap), 22 + h + 3, w, h, p)
        y_notes = 22 + 2 * h + 6
    notes = [
        "Each dot is one flooded habitable structure of the governing scenario, numbered with "
        "its ID in the structures table: its maximum velocity and maximum depth. The zone of a "
        "structure is read cell by cell, so a dot can sit above the zone the structure was "
        "classified in.",
        "Zones: the REG_05.2026 Fig. 5.1 / 5.2 table the classification used. Triangles: "
        "faster than the figure, drawn at its right edge, where the classification reads "
        "the curves.",
    ]
    if len([r for r in rows if r["kind"] == DV.HOUSE]) > DV.LABEL_MAX or \
            len([r for r in rows if r["kind"] == DV.MOBILE]) > DV.LABEL_MAX:
        notes.append("Charts with more than {} structures are drawn without numbers; the "
                     "spreadsheet copy labels every point.".format(DV.LABEL_MAX))
    if close:
        notes.append("Close-ups (lower charts): the same structures at a larger scale when they "
                     "all fit in a third of the figure.")
    if dv.get("xlsx"):
        notes.append("The same structures are plotted on the first tab of {}.".format(
            os.path.basename(dv["xlsx"])))
    for n in dv.get("notes") or []:
        notes.append(n[0].upper() + n[1:] + ".")
    _label(layout, "\n".join(notes), MARGIN, y_notes, PAGE_W - 2 * MARGIN,
           PAGE_H - y_notes - MARGIN / 2.0, p, size=7 if close else 8)


def build_report(out_folder, dam_name, res, scenario, lines, structs, clip, scen_layers,
                 max_pages=8, add_to_project=True, all_results=None, hydraulics_group=None,
                 imagery=True, info=None):
    """Build the layout and export Hazard_Classification_Report.pdf.
    Returns (pdf path, note) - note says when the usual file name could not
    be used (the old PDF is open in another program).

    imagery: aerial imagery under the overview and zoom maps (skipped, with
    a note on the map pages, when offline). info: an optional dict that
    receives "imagery" (True when the maps have it), "imagery_caption" and
    "imagery_note" (why there is none; "" when there is or it was off)."""
    project = QgsProject.instance()
    crs = clip.crs
    vec = [res.get("layers", {}).get(k) for k in ("Habitable_Structures", "Access_Cut_Off",
                                                  "Roads_Inundated", "Rail_Inundated")]
    layers = [lyr for lyr in vec if lyr is not None]
    depth = _depth_layer(scen_layers.get("depth"), scenario, add_to_project,
                         group=hydraulics_group)
    if depth is not None:
        layers.append(depth)
    _KEEP[:] = layers
    map_layers, styles, img_used, img_cap, img_note = layout_imagery(
        imagery, layers, add_to_project, clip.rect, crs)
    if info is not None:
        info.update({"imagery": img_used, "imagery_caption": img_cap,
                     "imagery_note": img_note})
    if imagery:
        log_message("Report maps: {}".format(
            "aerial imagery under the maps ({})".format(img_cap) if img_used else
            "no aerial imagery - {}".format(img_note)))
    map_w, map_h = PAGE_W - 2 * MARGIN - 66, PAGE_H - 22 - MARGIN - 16
    layout = QgsPrintLayout(project)
    layout.initializeDefaults()
    name = "ND Dam Hazard Report - {}".format(dam_name)
    layout.setName(name)
    layout.pageCollection().page(0).setPageSize("Letter", _landscape())

    # 1. summary tables
    _summary_page(layout, dam_name, res, scenario, all_results)

    # 2. overview map
    p = _add_page(layout)
    _label(layout, "Overview - study area {}".format(clip.describe() if hasattr(
        clip, "describe") else ""), MARGIN, MARGIN, PAGE_W - 2 * MARGIN, 8, p, size=11,
        bold=True)
    m = _map(layout, crs, clip.rect, map_layers, MARGIN, 22, map_w, map_h, p, styles)
    _decorations(layout, m, layers, p)
    imagery_label(layout, img_used, img_cap, img_note, MARGIN + 90, 22 + map_h + 1.5,
                  map_w - 90, p)

    # 3. zoom maps
    cell = max(clip.rect.width(), clip.rect.height()) / 6.0
    clusters = _clusters(structs, cell, max(0, int(max_pages)))
    for i, (bbox, recs) in enumerate(clusters):
        p = _add_page(layout)
        _label(layout, "Area {} of {}: {} habitable structure(s) in the high / judgment "
               "zones".format(i + 1, len(clusters), len(recs)), MARGIN, MARGIN,
               PAGE_W - 2 * MARGIN, 8, p, size=11, bold=True)
        zm = _map(layout, crs, bbox, map_layers, MARGIN, 22, map_w, map_h, p, styles)
        _decorations(layout, zm, layers, p)
        imagery_label(layout, img_used, img_cap, img_note, MARGIN + 90, 22 + map_h + 1.5,
                      map_w - 90, p)

    # 4. v2.20.0: depth-velocity charts with the structures
    _dv_page(layout, res.get("dv_charts") or {})

    # 5. roads and railroads
    lrows = _line_rows(res.get("roads") or [], res.get("rails") or [])
    if lrows:
        p = _add_page(layout)
        _label(layout, "Overtopped roads and inundated railroads ({}) - ND 5.2.1.2 / "
               "5.2.1.3".format(len(lrows)), MARGIN, MARGIN, PAGE_W - 2 * MARGIN, 8, p,
               size=11, bold=True)
        _table(layout, ROAD_HEAD, lrows, p, MARGIN, 22, PAGE_W - 2 * MARGIN,
               PAGE_H - 22 - MARGIN, title="roads",
               widths=ROAD_WIDTHS)

    # 6. structures (last: its extra pages are appended at the end)
    incremental = bool(res.get("incremental"))
    recs = report_structures(structs)
    rows = _structure_rows(recs, incremental)
    if rows:
        p = _add_page(layout)
        _label(layout, "Flooded habitable structures ({}) - ND outcome (5.2.1.1 / 5.2.2) and "
               "ACER TM 11 zone{}".format(len(rows), "; 'Charged' = caused by the breach "
                                          "(incremental)" if incremental else ""),
               MARGIN, MARGIN, PAGE_W - 2 * MARGIN, 8, p, size=11, bold=True)
        head = STRUCT_HEAD + (["Charged"] if incremental else [])
        widths = STRUCT_WIDTHS + ([14] if incremental else [])
        _table(layout, head, rows, p, MARGIN, 22, PAGE_W - 2 * MARGIN, PAGE_H - 22 - MARGIN,
               title="structures", widths=widths)

    wanted = os.path.join(out_folder, REPORT_FILE)
    path = prepare_output_path(wanted)
    note = ""
    if not same_path(path, wanted):
        note = ("{} is open in another program, so the new report was saved as {}. Close "
                "the old PDF and run again to keep the usual name.".format(
                    REPORT_FILE, os.path.basename(path)))
        log_message(note)
    exporter = QgsLayoutExporter(layout)
    result = exporter.exportToPdf(path, QgsLayoutExporter.PdfExportSettings())
    if not _export_ok(result):
        raise RuntimeError("PDF export failed (code {})".format(enum_value(result)))
    if add_to_project:
        mgr = project.layoutManager()
        old = mgr.layoutByName(name)
        if old is not None:
            mgr.removeLayout(old)
        mgr.addLayout(layout)
    log_message("Report: {} ({} page(s))".format(path, layout.pageCollection().pageCount()))
    return path, note
