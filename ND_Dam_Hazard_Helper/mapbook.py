# -*- coding: utf-8 -*-
"""v2.5: EAP inundation map book (FEMA P-64 inundation maps).

EAP/EAP_Map_Book.pdf, US Letter landscape:
  page 1   index: the whole flooded area with the numbered map sheets and a
           list of the sheets (first arrival, number of notification items)
  page 2.. one sheet each, in the order the flood reaches them: arrival-time
           bands (else max depth), arrival contours, closed roads and the
           EAP notification items numbered as in EAP_Notification_List, with
           the items on that sheet listed beside the map
The sheets are a grid over the flooded area at a fixed scale (default
1 in = 1,000 ft); sheets without a flooded cell are left out. If more than
MAPBOOK_MAX_SHEETS are needed the next smaller scale is used. The sheet
grid is saved as EAP/EAP_Map_Sheets.gpkg; the layout is kept in the
project's Layout Manager ("ND Dam EAP Map Book - <dam>") for editing.

v2.9 (imagery=True): aerial imagery under the index map and every sheet
(the last layer of each map item), the flood raster semi-transparent in
those maps only, and an "Imagery: <service>" credit on each page; offline
the book is made without imagery and the pages and summary lines say so.
"""

import math
import os
import time

import numpy as np

from qgis.core import (
    QgsProject, QgsPrintLayout, QgsLayoutExporter, QgsRectangle, QgsFeature, QgsField,
    QgsGeometry, QgsVectorLayer, QgsRasterLayer, QgsLayoutItemScaleBar, QgsLayoutItemLegend,
)

from .compat import FT_INT, FT_DOUBLE, NULL, WKB_POLYGON, DIST_FEET, DIST_MILES
from .constants import (
    PLUGIN_NAME, PLUGIN_VERSION, MAPBOOK_SCALES, MAPBOOK_MAX_SHEETS, MAPBOOK_TABLE_ROWS,
)
from .helpers import (
    log_message, prepare_output_path, same_path, add_layer_to_project, create_memory_layer,
    export_layer_to_formats, remove_project_layers_for_path, RasterSampler,
)
from .extent import distance_xy_in_crs
from . import report as R

BOOK_FILE = "EAP_Map_Book.pdf"
SHEETS_BASE = "EAP_Map_Sheets"
# sheet page geometry (mm): map on the left, legend + item list on the right
MAP_X, MAP_Y, MAP_W, MAP_H = R.MARGIN, 22.0, 185.0, 168.0
COL_X = MAP_X + MAP_W + 6.0
COL_W = R.PAGE_W - R.MARGIN - COL_X
ITEM_HEAD = ["#", "Arrival", "Item"]
ITEM_WIDTHS = [7, 11, 50]
INDEX_HEAD = ["Sheet", "First arrival", "Items"]
INDEX_MAP_ID = "EAP index map"
_KEEP = []


def sheet_size(scale, crs, lat):
    """(dx, dy) of one sheet's map frame on the ground, in CRS units."""
    w_ft = MAP_W / 1000.0 * scale / 0.3048
    h_ft = MAP_H / 1000.0 * scale / 0.3048
    dx, _ = distance_xy_in_crs(w_ft, crs, lat)
    _, dy = distance_xy_in_crs(h_ft, crs, lat)
    return dx, dy


def grid(rect, dx, dy):
    """Sheet rectangles (xmin, ymin, xmax, ymax) covering rect, centred on it."""
    nx = max(1, int(math.ceil(rect.width() / dx - 1e-9)))
    ny = max(1, int(math.ceil(rect.height() / dy - 1e-9)))
    x0 = (rect.xMinimum() + rect.xMaximum()) / 2.0 - nx * dx / 2.0
    y0 = (rect.yMinimum() + rect.yMaximum()) / 2.0 - ny * dy / 2.0
    return [(x0 + i * dx, y0 + j * dy, x0 + (i + 1) * dx, y0 + (j + 1) * dy)
            for j in range(ny - 1, -1, -1) for i in range(nx)]


def _window_min(sampler, box, positive=False):
    """Smallest valid value of the raster inside box (raster CRS), or None."""
    if sampler is None or not sampler.ok:
        return None
    win = sampler._window_for_bbox(*box)
    if win is None:
        return None
    c0, r0, w, h = win
    if w * h > 16000000:
        return None
    arr = sampler._read(c0, r0, w, h)
    if arr is None:
        return None
    m = sampler._valid_mask(arr)
    vals = arr * sampler.scale + sampler.offset
    if positive:
        with np.errstate(invalid="ignore"):
            m &= vals > 0
    return float(vals[m].min()) if m.any() else None


def plan_sheets(depth_sampler, arr_sampler, crs, lat, scale, max_sheets=MAPBOOK_MAX_SHEETS):
    """(sheets [(box, first_arrival)], scale used, notes). Boxes in the
    depth raster CRS (= the analysis CRS)."""
    notes = []
    rect = depth_sampler.valid_extent(positive_only=True)
    if rect is None:
        return [], scale, ["no flooded cell"]
    scales = [s for _l, s in MAPBOOK_SCALES if s >= scale] or [scale]
    boxes = []
    for sc in scales:
        dx, dy = sheet_size(sc, crs, lat)
        boxes = [b for b in grid(rect, dx, dy)
                 if depth_sampler.any_valid_in_bbox(*b, positive_only=True)]
        if len(boxes) <= max_sheets:
            if sc != scale:
                notes.append("{} sheets at 1:{:,} would be needed - 1:{:,} used".format(
                    "More than {}".format(max_sheets), scale, sc))
            scale = sc
            break
    else:
        notes.append("{} sheets needed even at 1:{:,} - the first {} (earliest arrival) "
                     "are drawn".format(len(boxes), scales[-1], max_sheets))
        scale = scales[-1]
    same = arr_sampler is not None and arr_sampler.ok and arr_sampler.crs == crs
    out = [(b, _window_min(arr_sampler, b) if same else None) for b in boxes]
    # downstream order: earliest arrival first; sheets without arrival last,
    # in grid order (top-left to bottom-right)
    order = sorted(range(len(out)), key=lambda i: (out[i][1] is None,
                                                   out[i][1] if out[i][1] is not None else 0,
                                                   i))
    out = [out[i] for i in order][:max_sheets]
    return out, scale, notes


def _label_layer(lyr, field, size=8.0, expression=False):
    from qgis.PyQt.QtGui import QColor, QFont
    from qgis.core import (
        QgsPalLayerSettings, QgsVectorLayerSimpleLabeling, QgsTextFormat,
        QgsTextBufferSettings,
    )
    try:
        s = QgsPalLayerSettings()
        s.fieldName = field
        s.isExpression = bool(expression)
        fmt = QgsTextFormat()
        font = QFont("Arial")
        font.setBold(True)
        fmt.setFont(font)
        fmt.setSize(size)
        buf = QgsTextBufferSettings()
        buf.setEnabled(True)
        buf.setSize(0.8)
        buf.setColor(QColor(255, 255, 255))
        fmt.setBuffer(buf)
        s.setFormat(fmt)
        lyr.setLabeling(QgsVectorLayerSimpleLabeling(s))
        lyr.setLabelsEnabled(True)
        lyr.triggerRepaint()
    except Exception as e:
        log_message("Map book labels on {} skipped: {}".format(lyr.name(), e))


def _style_sheets(lyr):
    try:
        from qgis.core import QgsFillSymbol, QgsSingleSymbolRenderer
        sym = QgsFillSymbol.createSimple({"color": "0,0,0,0", "style": "no",
                                          "outline_color": "30,30,30",
                                          "outline_width": "0.6"})
        try:
            # v2.9: white casing under the dark line - readable over aerial imagery
            from qgis.core import QgsSimpleFillSymbolLayer
            casing = QgsSimpleFillSymbolLayer.create({
                "color": "0,0,0,0", "style": "no", "outline_color": "255,255,255,220",
                "outline_width": "1.2"})
            if casing is not None:
                sym.insertSymbolLayer(0, casing)        # index 0 = drawn first (under)
        except Exception:
            pass
        lyr.setRenderer(QgsSingleSymbolRenderer(sym))
    except Exception as e:
        log_message("Map sheet style skipped: {}".format(e))
    # sheet numbers on the index map and the QGIS map window only (on a sheet
    # page the number would sit in the middle of the map)
    _label_layer(lyr, "CASE WHEN @map_id IN ('canvas', '{}') THEN 'Sheet ' || \"Sheet\" END".format(
        INDEX_MAP_ID), size=11.0, expression=True)


def _project_layer(path, name, raster=False, add=True, group=None, style=None,
                   visible=False):
    """The project layer reading `path`, else a new one (added to the
    project when `add`, so a layout kept there keeps it)."""
    if not path:
        return None
    for lyr in QgsProject.instance().mapLayers().values():
        try:
            if same_path(lyr.source().split("|")[0], path):
                return lyr
        except Exception:
            continue
    lyr = QgsRasterLayer(path, name) if raster else QgsVectorLayer(path, name, "ogr")
    if not lyr.isValid():
        return None
    if style is not None:
        try:
            style(lyr)
        except Exception:
            pass
    if add:
        add_layer_to_project(lyr, visibility=visible, group=group)
    _KEEP.append(lyr)
    return lyr


def _sheet_layer(sheets, crs, folder, ctx, add, group):
    lyr = create_memory_layer(SHEETS_BASE, WKB_POLYGON, crs, [
        QgsField("Sheet", FT_INT), QgsField("First_Arrival_hr", FT_DOUBLE),
        QgsField("Items", FT_INT)])
    feats = []
    for i, s in enumerate(sheets):
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromRect(QgsRectangle(*s["box"])))
        f.setAttributes([i + 1, NULL if s["arrival"] is None else round(s["arrival"], 2),
                         len(s["items"])])
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    lyr.updateExtents()
    path = os.path.join(folder, SHEETS_BASE + ".gpkg")
    remove_project_layers_for_path(path)
    _ok, _r, paths = export_layer_to_formats(lyr, folder, SHEETS_BASE, ctx, formats=("gpkg",))
    show = lyr
    if paths.get("gpkg"):
        saved = QgsVectorLayer(paths["gpkg"], SHEETS_BASE, "ogr")
        if saved.isValid():
            show = saved
    _style_sheets(show)
    if add:
        add_layer_to_project(show, SHEETS_BASE, visibility=False, group=group)
    _KEEP.append(show)
    return show, paths.get("gpkg")


def _scale_bar(layout, m, x, y, w, page):
    try:
        sb = QgsLayoutItemScaleBar(layout)
        sb.setStyle("Single Box")
        sb.setLinkedMap(m)
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
        R._place(sb, x, y, w, 12, page)
    except Exception as e:
        log_message("Map book scale bar skipped: {}".format(e))


def _north(layout, m, x, y, page):
    """North arrow (QGIS default SVG), else a text arrow."""
    try:
        from qgis.core import QgsLayoutItemPicture, QgsSymbolLayerUtils, QgsPathResolver
        p = QgsSymbolLayerUtils.svgSymbolNameToPath("arrows/NorthArrow_02.svg",
                                                    QgsPathResolver())
        if p and os.path.exists(p):
            pic = QgsLayoutItemPicture(layout)
            pic.setPicturePath(p)
            try:
                pic.setLinkedMap(m)
            except Exception:
                pass
            layout.addLayoutItem(pic)
            R._place(pic, x, y, 9, 12, page)
            return
    except Exception:
        pass
    R._label(layout, "N\n↑", x, y, 9, 12, page, size=10, bold=True)


def _legend(layout, m, layers, x, y, w, h, page):
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
        try:
            leg.setResizeToContents(False)
        except Exception:
            pass
        layout.addLayoutItem(leg)
        R._place(leg, x, y, w, h, page)
    except Exception as e:
        log_message("Map book legend skipped: {}".format(e))


def _item_text(f):
    def g(k):
        try:
            v = f[k]
        except Exception:
            return ""
        return "" if v is None or v == NULL or str(v) == "NULL" else str(v)
    what = g("Name") or g("Address") or g("Category")
    typ = g("Type").split(" - ")[0]
    pri = "! " if g("Priority").startswith("1") else ""
    return "{}{}{}".format(pri, typ, ": " + what if what else "")[:70]


def build(eap_folder, dam_name, scenario_name, crs, lat, ctx, depth_layer, arrival_layer,
          bands_path=None, contours_path=None, notif_layer=None, roads_layer=None,
          scale=12000, add_to_project=True, group=None, max_sheets=MAPBOOK_MAX_SHEETS,
          imagery=True):
    """Export EAP_Map_Book.pdf. Returns (pdf path or None, summary lines).
    imagery: aerial imagery under the maps (a summary line says whether the
    maps have it)."""
    from .eap import style_arrival_bands
    from .styles import style_depth
    lines = []
    dsamp = RasterSampler(depth_layer)
    asamp = RasterSampler(arrival_layer) if arrival_layer is not None else None
    try:
        planned, scale, notes = plan_sheets(dsamp, asamp, crs, lat, scale, max_sheets)
    finally:
        dsamp.close()
        if asamp is not None:
            asamp.close()
    if not planned:
        return None, ["Map book not made: no flooded cell"]
    sheets = [{"box": b, "arrival": a, "items": []} for b, a in planned]
    # notification items per sheet (an item near a sheet edge is listed on
    # every sheet that shows it)
    feats = list(notif_layer.getFeatures()) if notif_layer is not None else []
    for f in feats:
        try:
            g = f.geometry()
            p = g.asPoint() if not g.isMultipart() else g.centroid().asPoint()
        except Exception:
            continue
        for s in sheets:
            x0, y0, x1, y1 = s["box"]
            if x0 <= p.x() <= x1 and y0 <= p.y() <= y1:
                s["items"].append(f)
    for s in sheets:
        s["items"].sort(key=lambda f: f["Order"] if f["Order"] not in (None, NULL) else 1e9)
        arrs = [f["Arrival_hr"] for f in s["items"] if f["Arrival_hr"] not in (None, NULL)]
        if s["arrival"] is None and arrs:
            s["arrival"] = min(arrs)
    del _KEEP[:]
    sheet_lyr, sheet_path = _sheet_layer(sheets, crs, eap_folder, ctx, add_to_project, group)
    # map layers, top to bottom
    base = _project_layer(bands_path, "EAP arrival bands ({})".format(scenario_name), True,
                          add_to_project, group, style_arrival_bands) if bands_path else None
    if base is None:
        base = _project_layer(depth_layer.source(), "Max depth ({})".format(scenario_name),
                              True, add_to_project, group, style_depth)
    contours = _project_layer(contours_path, "EAP arrival contours ({})".format(
        scenario_name), False, add_to_project, group) if contours_path else None
    if contours is not None:
        _label_layer(contours, "format_number(\"Arrival_hr\", 1) || ' h'", size=7.0,
                     expression=True)
    if notif_layer is not None:
        _label_layer(notif_layer, "Order", size=7.0)
    layers = [lyr for lyr in (notif_layer, sheet_lyr, roads_layer, contours, base)
              if lyr is not None]
    legend_layers = [lyr for lyr in layers if lyr is not sheet_lyr]
    _KEEP.extend(layers)
    union = QgsRectangle(*sheets[0]["box"])
    for s in sheets[1:]:
        union.combineExtentWith(QgsRectangle(*s["box"]))
    map_layers, styles, img_used, img_cap, img_note = R.layout_imagery(
        imagery, layers, add_to_project, union, crs)
    if img_used:
        _KEEP.append(map_layers[-1])              # the imagery layer

    project = QgsProject.instance()
    layout = QgsPrintLayout(project)
    layout.initializeDefaults()
    name = "ND Dam EAP Map Book - {}".format(dam_name)
    layout.setName(name)
    layout.pageCollection().page(0).setPageSize("Letter", R._landscape())
    stamp = "{} v{} - {} - scenario '{}' - map scale 1:{:,} (1 in = {:,.0f} ft) - North " \
            "up".format(PLUGIN_NAME, PLUGIN_VERSION, time.strftime("%Y-%m-%d"), scenario_name,
                        scale, scale / 12.0)

    # ---- index page ----
    R._label(layout, "{} - Emergency Action Plan inundation maps".format(dam_name),
             R.MARGIN, R.MARGIN, R.PAGE_W - 2 * R.MARGIN, 9, 0, size=14, bold=True)
    R._label(layout, "Index of map sheets (numbered in the order the flood reaches them). "
             "Numbers on the maps are the Order of EAP_Notification_List.", R.MARGIN, 19,
             R.PAGE_W - 2 * R.MARGIN, 5, 0, size=8)
    m = R._map(layout, crs, union, map_layers, MAP_X, 26, MAP_W, MAP_H - 4, 0, styles)
    try:
        m.setId(INDEX_MAP_ID)
    except Exception:
        pass
    _legend(layout, m, legend_layers, COL_X, 26, COL_W, 70, 0)
    rows = [[str(i + 1), _hhmm(s["arrival"]) or "-", str(len(s["items"]))]
            for i, s in enumerate(sheets)]
    shown = rows[:24]
    R._table(layout, INDEX_HEAD, shown, 0, COL_X, 100, COL_W, 90, size=6, repeat=False,
             title="map sheets", widths=[10, 18, 10])
    if len(rows) > len(shown):
        R._label(layout, "... {} more sheets - see {}.gpkg".format(len(rows) - len(shown),
                                                                   SHEETS_BASE),
                 COL_X, 192, COL_W, 5, 0, size=6)
    _scale_bar(layout, m, MAP_X + 2, R.PAGE_H - R.MARGIN - 14, 80, 0)
    R.imagery_label(layout, img_used, img_cap, img_note, MAP_X + 90, 26 + MAP_H - 4 + 1.0,
                    MAP_W - 90, 0)
    R._label(layout, stamp, MAP_X + 90, R.PAGE_H - R.MARGIN - 6, R.PAGE_W - MAP_X - 100, 5, 0,
             size=6)

    # ---- sheets ----
    for i, s in enumerate(sheets):
        p = R._add_page(layout)
        R._label(layout, "{} - EAP inundation map - sheet {} of {}{}".format(
            dam_name, i + 1, len(sheets), "  (first arrival {} h:min)".format(
                _hhmm(s["arrival"])) if s["arrival"] is not None else ""),
            R.MARGIN, R.MARGIN, R.PAGE_W - 2 * R.MARGIN, 8, p, size=12, bold=True)
        # the sheet box already has the frame's aspect and ground size, so the
        # map shows exactly the sheet (no setScale: in Web Mercator it would
        # shrink the frame by 1 / cos(latitude) and leave gaps between sheets)
        sm = R._map(layout, crs, QgsRectangle(*s["box"]), map_layers, MAP_X, MAP_Y, MAP_W,
                    MAP_H, p, styles)
        _legend(layout, sm, legend_layers, COL_X, MAP_Y, COL_W, 62, p)
        R._label(layout, "Notification items on this sheet ({}; '!' = vulnerable "
                 "occupants)".format(len(s["items"])), COL_X, MAP_Y + 64, COL_W, 5, p,
                 size=7, bold=True)
        items = [[str(f["Order"]), _hhmm(f["Arrival_hr"]) or "-", _item_text(f)]
                 for f in s["items"][:MAPBOOK_TABLE_ROWS]]
        if items:
            R._table(layout, ITEM_HEAD, items, p, COL_X, MAP_Y + 70, COL_W,
                     MAP_H - 70, size=6, repeat=False, title="sheet items",
                     widths=ITEM_WIDTHS)
        if len(s["items"]) > MAPBOOK_TABLE_ROWS:
            R._label(layout, "+ {} more - see EAP_Notification_List".format(
                len(s["items"]) - MAPBOOK_TABLE_ROWS), COL_X, MAP_Y + MAP_H - 4, COL_W, 5,
                p, size=6)
        _scale_bar(layout, sm, MAP_X + 2, R.PAGE_H - R.MARGIN - 14, 80, p)
        _north(layout, sm, MAP_X + MAP_W - 12, MAP_Y + 2, p)
        R.imagery_label(layout, img_used, img_cap, img_note, MAP_X + 90, MAP_Y + MAP_H + 1.0,
                        MAP_W - 90, p)
        R._label(layout, stamp, MAP_X + 90, R.PAGE_H - R.MARGIN - 6,
                 R.PAGE_W - MAP_X - 100, 5, p, size=6)

    wanted = os.path.join(eap_folder, BOOK_FILE)
    path = prepare_output_path(wanted)
    exporter = QgsLayoutExporter(layout)
    result = exporter.exportToPdf(path, QgsLayoutExporter.PdfExportSettings())
    if not R._export_ok(result):
        raise RuntimeError("map book PDF export failed (code {})".format(result))
    if add_to_project:
        mgr = project.layoutManager()
        old = mgr.layoutByName(name)
        if old is not None:
            mgr.removeLayout(old)
        mgr.addLayout(layout)
    lines.append("Map book: {} sheet(s) at 1:{:,} (1 in = {:,.0f} ft) plus an index page - "
                 "EAP/{}{}".format(len(sheets), scale, scale / 12.0, os.path.basename(path),
                                   " (the usual name was open in another program)"
                                   if not same_path(path, wanted) else ""))
    if imagery:
        lines.append("Aerial imagery under the maps: {}".format(img_cap) if img_used else
                     "Map book made without aerial imagery: {}".format(img_note))
    lines.extend(notes)
    if sheet_path:
        lines.append("Map sheet grid: EAP/{}".format(os.path.basename(sheet_path)))
    log_message("EAP map book: {} ({} pages)".format(path, layout.pageCollection().pageCount()))
    return path, lines


def _hhmm(hours):
    if hours is None or hours == NULL:
        return ""
    try:
        m = int(round(float(hours) * 60.0))
    except (TypeError, ValueError):
        return ""
    return "{}:{:02d}".format(m // 60, m % 60)
