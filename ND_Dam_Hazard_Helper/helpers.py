# -*- coding: utf-8 -*-
"""Generic utility functions: logging, naming, output-file handling, raster
sampling, field handling, styling. No plugin-state dependencies."""

import os
import sys
import math
import time
import warnings
import traceback
from contextlib import contextmanager, ExitStack

import numpy as np
from qgis.PyQt.QtGui import QColor

from qgis.core import (
    QgsProject, QgsMessageLog, QgsRectangle, QgsField, QgsFields,
    QgsVectorLayer, QgsVectorFileWriter, QgsColorRampShader, QgsRasterShader,
    QgsSingleBandPseudoColorRenderer, QgsUnitTypes, QgsWkbTypes, QgsGeometry,
    QgsMemoryProviderUtils,
)

from osgeo import gdal, ogr

from .compat import (
    NULL, MSG_INFO, MSG_WARNING, MSG_CRITICAL, SHADER_DISCRETE, WRITER_NO_ERROR,
    GEOM_OP_SUCCESS, DIST_FEET, DIST_METERS, DIST_DEGREES, DIST_UNKNOWN,
    FT_INT, FT_UINT, FT_LONGLONG, FT_ULONGLONG, FT_DOUBLE, FT_STRING, FT_INVALID,
    enum_value,
)
from .constants import (
    PLUGIN_NAME, CLASS_HIGH, CLASS_JUDGMENT, CLASS_LOW, CLASS_NODATA,
    HAZARD_COEFFICIENTS, TableCurve, _FT_PER_METER, _METERS_PER_DEGREE_EQUATOR,
    MAX_REPEATED_WARNINGS, XLSX_MAX_SHEET_NAME, CURVE_V_MAX,
)
from . import constants as _CONST


class CancelledByUser(Exception):
    pass


# ----------------------------------------------------------------- logging --
def log_message(message, level=MSG_INFO):
    try:
        QgsMessageLog.logMessage(str(message), PLUGIN_NAME, level)
    except Exception:
        pass
    try:
        print("[{}] {}".format(PLUGIN_NAME, message))
    except Exception:
        pass


class LimitedLogger(object):
    """Logs at most `limit` messages of one kind, then a single summary.
    Stops a systematic per-feature problem from flooding the log."""

    def __init__(self, label, limit=MAX_REPEATED_WARNINGS, level=MSG_WARNING):
        self.label = label
        self.limit = limit
        self.level = level
        self.count = 0

    def log(self, message):
        self.count += 1
        if self.count <= self.limit:
            log_message("{}: {}".format(self.label, message), self.level)
        elif self.count == self.limit + 1:
            log_message("{}: further messages of this kind suppressed".format(
                self.label), self.level)

    def summary(self):
        if self.count > self.limit:
            log_message("{}: {} occurrences in total".format(
                self.label, self.count), self.level)


@contextmanager
def gdal_exceptions():
    """Enable GDAL/OGR Python exceptions only for the enclosed block and
    restore the previous (session-wide) setting afterwards, so this plugin
    does not change GDAL behaviour for QGIS or other plugins."""
    if not on_main_thread():
        # UseExceptions() is process-wide in the GDAL bindings QGIS ships:
        # toggling it from a Processing worker would change GDAL for QGIS
        # and other plugins while the worker runs. Callers check results.
        yield
        return
    # GDAL >= 3.7 keeps the modules' error handlers on a stack: they must be
    # switched off in the reverse order they were switched on (ogr before
    # gdal), otherwise DontUseExceptions() raises "Cannot call
    # gdal.DontUseExceptions() at that point since the stack of error
    # handlers is: ogr gdal". Only modules that were off are switched, and
    # an ExitStack undoes them last-in first-out.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        with ExitStack() as stack:
            for module in (gdal, ogr):
                if not _get_use_exceptions(module):
                    module.UseExceptions()
                    stack.callback(_dont_use_exceptions, module)
            yield


def _dont_use_exceptions(module):
    try:
        module.DontUseExceptions()
    except Exception as e:  # never let restoring the setting break a run
        log_message("GDAL exception setting not restored for {}: {}".format(
            getattr(module, "__name__", module), e), MSG_WARNING)


def _get_use_exceptions(module):
    try:
        return bool(module.GetUseExceptions())
    except Exception:
        return False


# ------------------------------------------------------- writer utilities --
def _writer_error_code(result):
    if isinstance(result, tuple):
        return result[0]
    return result


def _writer_error_message(result):
    if isinstance(result, tuple) and len(result) > 1:
        return result[1] or "Unknown error"
    return "Unknown error"


# ------------------------------------------------------------ paths/names --
def get_layer_path(layer):
    if not layer or not layer.isValid():
        return None
    try:
        uri = layer.dataProvider().dataSourceUri()
        return uri.split("|")[0]
    except Exception:
        return None


def layer_file_path(layer):
    """File part of a layer's source, also for invalid layers."""
    try:
        return layer.source().split("|")[0]
    except Exception:
        return ""


def normalize_path(path):
    """Comparable form of a path. Windows and (by default) macOS file
    systems are case-insensitive; os.path.normcase only folds case on
    Windows, so fold it on macOS too."""
    if not path:
        return ""
    try:
        p = os.path.normcase(os.path.realpath(os.path.abspath(path)))
        if sys.platform == "darwin":
            p = p.lower()
        return p
    except Exception:
        return path


def same_path(a, b):
    return bool(a) and bool(b) and normalize_path(a) == normalize_path(b)


def evaluate_polynomial(coefficients, x_value):
    """Horner's method. Coeffs: [x^4, x^3, x^2, x, const]. Works in place on
    one float64 buffer to keep memory low on large arrays.

    v2.11.0: a constants.TableCurve (the REG_05.2026 Fig. 5.1 / 5.2 table) is
    read by linear interpolation instead, velocity below the table at its first
    value and above it at its last (the figure's range)."""
    x = np.asarray(x_value, dtype=np.float64)
    if isinstance(coefficients, TableCurve):
        return np.asarray(np.interp(x, coefficients.v, coefficients.d),
                          dtype=np.float64).reshape(x.shape)
    result = np.zeros_like(x)
    for coef in coefficients:
        result *= x
        result += coef
    return result


def dam_display_name(dam_folder):
    """The dam folder's own name (spaces kept) for titles and layer groups."""
    try:
        name = os.path.basename(os.path.normpath(dam_folder or "").rstrip("/\\")).strip()
    except Exception:
        name = ""
    return name[:80] if name and name not in (".", "..") else "Dam"


def get_dam_folder_name(dam_folder):
    if not dam_folder:
        return "Dam"
    try:
        folder_name = os.path.basename(dam_folder.rstrip('/\\'))
        folder_name = "".join(c if c.isalnum() else "_" for c in folder_name)
        return folder_name[:50] if folder_name else "Dam"
    except Exception:
        return "Dam"


# ------------------------------------------------------ project / outputs --
def add_layer_to_project(layer, name=None, visibility=True, group=None):
    """Add `layer` to the project. `group`: a path of layer-tree group names
    (e.g. plugin_group(dam, GRP_RESULTS)); the groups are created when
    missing and a layer of the same name already in that group (a previous
    run's) is replaced, so re-runs never list a layer twice."""
    try:
        if not layer or not layer.isValid():
            log_message("Cannot add invalid layer", MSG_WARNING)
            return False
        if name:
            layer.setName(name)
        project = QgsProject.instance()
        node = None
        if group:
            try:
                node = ensure_group(group)
            except Exception as e:
                log_message("Layer group {} not available ({}); layer added at the top "
                            "level".format(" / ".join(group), e), MSG_WARNING)
                node = None
        if node is None:
            project.addMapLayer(layer, True)          # 2nd argument = add to legend
            if not visibility:
                try:
                    tl = project.layerTreeRoot().findLayer(layer.id())
                    if tl is not None:
                        tl.setItemVisibilityChecked(False)
                except Exception:
                    pass
        else:
            _remove_same_name(node, layer)
            project.addMapLayer(layer, False)
            tl = node.insertLayer(_insert_index(node, layer), layer)
            if tl is not None:
                try:
                    tl.setItemVisibilityChecked(bool(visibility))
                except Exception:
                    pass
        log_message("Added layer: {}".format(layer.name()))
        return True
    except Exception:
        log_message("Error adding layer: {}".format(traceback.format_exc()), MSG_CRITICAL)
        return False


# -------------------------------------------------------------- layer tree --
def plugin_group(dam_name, category=None):
    """Layer-tree path of the plugin's group for a dam (and a category)."""
    from .constants import GROUP_ROOT_FMT
    root = GROUP_ROOT_FMT.format((dam_name or "").strip() or "project")
    return (root, category) if category else (root,)


def _is_group(node):
    from qgis.core import QgsLayerTreeGroup
    return isinstance(node, QgsLayerTreeGroup)


def _is_layer_node(node):
    from qgis.core import QgsLayerTreeLayer
    return isinstance(node, QgsLayerTreeLayer)


def ensure_group(path, root=None):
    """The layer-tree group at `path` (names from the root - the project's
    layer tree unless `root` is given), created where missing: the plugin's
    top group goes first in the tree, its category groups in GROUP_ORDER."""
    from .constants import GROUP_ORDER
    node = root if root is not None else QgsProject.instance().layerTreeRoot()
    for depth, name in enumerate(path):
        child = None
        for ch in node.children():
            if _is_group(ch) and ch.name() == name:
                child = ch
                break
        if child is None:
            if depth == 0:
                child = node.insertGroup(0, name)
            else:
                idx = len(node.children())
                if name in GROUP_ORDER:
                    rank = GROUP_ORDER.index(name)
                    idx = 0
                    for i, ch in enumerate(node.children()):
                        if _is_group(ch) and ch.name() in GROUP_ORDER and \
                                GROUP_ORDER.index(ch.name()) < rank:
                            idx = i + 1
                child = node.insertGroup(idx, name)
        node = child
    return node


def _insert_index(group, layer):
    """Vector layers above rasters inside a group (rasters would hide them)."""
    from qgis.core import QgsRasterLayer
    kids = group.children()
    if isinstance(layer, QgsRasterLayer):
        return len(kids)
    for i, ch in enumerate(kids):
        if _is_layer_node(ch) and isinstance(ch.layer(), QgsRasterLayer):
            return i
    return len(kids)


def _remove_same_name(group, layer):
    ids = []
    for ch in group.children():
        if _is_layer_node(ch) and ch.name() == layer.name() and ch.layer() is not layer:
            ids.append(ch.layerId())
    if ids:
        QgsProject.instance().removeMapLayers(ids)


def prune_empty_groups(path):
    """Remove category groups left empty under the plugin group (e.g. no
    roads this run). The top group is removed too when it is empty."""
    try:
        root = QgsProject.instance().layerTreeRoot()
        top = None
        for ch in root.children():
            if _is_group(ch) and ch.name() == path[0]:
                top = ch
                break
        if top is None:
            return
        for ch in list(top.children()):
            if _is_group(ch) and not ch.findLayers():
                top.removeChildNode(ch)
        if not top.children():
            root.removeChildNode(top)
    except Exception as e:
        log_message("Layer groups not tidied: {}".format(e))


def on_main_thread():
    """False inside a Processing / QgsTask worker thread: the project and
    its layers must not be touched there."""
    try:
        from qgis.PyQt.QtCore import QThread, QCoreApplication
        app = QCoreApplication.instance()
        return app is None or QThread.currentThread() == app.thread()
    except Exception:
        return True


def remove_project_layers_for_path(path):
    """Remove project layers that read from `path` (typically a previous
    run's output) so the file can be overwritten and is not listed twice."""
    if not path or not on_main_thread():
        return 0
    project = QgsProject.instance()
    ids = []
    for lid, lyr in project.mapLayers().items():
        if same_path(layer_file_path(lyr), path):
            ids.append(lid)
    if ids:
        project.removeMapLayers(ids)
        log_message("Removed {} project layer(s) using {} before overwriting".format(
            len(ids), os.path.basename(path)))
    return len(ids)


def _sidecar_files(path):
    if path.lower().endswith(".gpkg"):
        return [path + "-wal", path + "-shm", path + "-journal"]
    if path.lower().endswith((".tif", ".tiff")):
        return [path + ".aux.xml", path + ".ovr"]
    return []


def timestamped_path(path):
    base, ext = os.path.splitext(path)
    return "{}_{}{}".format(base, time.strftime("%Y%m%d_%H%M%S"), ext)


def prepare_output_path(path):
    """Make `path` writable: detach project layers using it and delete the
    old file. If the file is locked (e.g. open elsewhere on Windows), return
    a time-stamped alternative instead of failing the whole run."""
    remove_project_layers_for_path(path)
    if not os.path.exists(path):
        return path
    try:
        os.remove(path)
        for extra in _sidecar_files(path):
            if os.path.exists(extra):
                try:
                    os.remove(extra)
                except OSError:
                    pass
        return path
    except OSError as e:
        alt = timestamped_path(path)
        log_message("Could not overwrite {} ({}); writing {} instead".format(
            os.path.basename(path), e, os.path.basename(alt)), MSG_WARNING)
        _REDIRECTED.append((path, alt))
        return alt


_REDIRECTED = []


def redirected_outputs(clear=False):
    """[(wanted path, path used)] of outputs written under another name since
    the last clear because the usual file was locked (open in Excel, a PDF
    viewer ...)."""
    if clear:
        del _REDIRECTED[:]
        return []
    return list(_REDIRECTED)


def apply_acer11_style(lyr):
    """Apply ACER-11 styling: 1=Red/High, 2=Yellow/Judgment, 3=Green/Low."""
    try:
        if not lyr or not lyr.isValid():
            return False
        items = [
            QgsColorRampShader.ColorRampItem(CLASS_NODATA, QColor(0, 0, 0, 0), "No Data"),
            QgsColorRampShader.ColorRampItem(CLASS_HIGH, QColor("#F44336"), "HIGH DANGER ZONE"),
            QgsColorRampShader.ColorRampItem(CLASS_JUDGMENT, QColor("#FFEB3B"), "JUDGEMENT ZONE"),
            QgsColorRampShader.ColorRampItem(CLASS_LOW, QColor("#4CAF50"), "LOW DANGER ZONE"),
        ]
        ramp_shader = QgsColorRampShader()
        if SHADER_DISCRETE is not None:
            ramp_shader.setColorRampType(SHADER_DISCRETE)
        ramp_shader.setColorRampItemList(items)
        raster_shader = QgsRasterShader()
        raster_shader.setRasterShaderFunction(ramp_shader)
        renderer = QgsSingleBandPseudoColorRenderer(lyr.dataProvider(), 1, raster_shader)
        lyr.setRenderer(renderer)
        rend = lyr.renderer()
        if rend is not None and hasattr(rend, "invalidateCache"):    # QGIS 3.34: no such method
            rend.invalidateCache()
        lyr.triggerRepaint()
        return True
    except Exception as e:
        log_message("Style apply failed: {}".format(e), MSG_WARNING)
        return False


def apply_class_style(lyr, items):
    """Discrete colours for an integer class raster: items = [(value,
    "#rrggbb", label)]; NoData (CLASS_NODATA) transparent."""
    try:
        if not lyr or not lyr.isValid():
            return False
        ramp = [QgsColorRampShader.ColorRampItem(CLASS_NODATA, QColor(0, 0, 0, 0), "No Data")]
        ramp += [QgsColorRampShader.ColorRampItem(v, QColor(c), lbl) for v, c, lbl in items]
        ramp_shader = QgsColorRampShader()
        if SHADER_DISCRETE is not None:
            ramp_shader.setColorRampType(SHADER_DISCRETE)
        ramp_shader.setColorRampItemList(ramp)
        raster_shader = QgsRasterShader()
        raster_shader.setRasterShaderFunction(ramp_shader)
        lyr.setRenderer(QgsSingleBandPseudoColorRenderer(lyr.dataProvider(), 1, raster_shader))
        lyr.triggerRepaint()
        return True
    except Exception as e:
        log_message("Style apply failed: {}".format(e), MSG_WARNING)
        return False


def export_layer_to_formats(layer, output_folder, base_name, transform_ctx,
                            formats=("gpkg", "csv", "xlsx")):
    """Export a layer to GPKG, CSV, and XLSX (or the given `formats`).
    Tuple-safe error handling. Returns (any_ok, {fmt: ok}, {fmt: path or None})."""
    results = {}
    paths = {}
    for fmt, driver in [("gpkg", "GPKG"), ("csv", "CSV"), ("xlsx", "XLSX")]:
        if fmt not in formats:
            continue
        paths[fmt] = None
        results[fmt] = False
        try:
            opts = QgsVectorFileWriter.SaveVectorOptions()
            opts.driverName = driver
            opts.fileEncoding = "UTF-8"
            if fmt == "csv":
                opts.layerOptions = ["GEOMETRY=AS_WKT"]
            elif fmt == "xlsx":
                # Excel rejects sheet names longer than 31 characters
                opts.layerName = base_name[:XLSX_MAX_SHEET_NAME]
            else:
                opts.layerName = base_name
            out_file = prepare_output_path(
                os.path.join(output_folder, "{}.{}".format(base_name, fmt)))
            write_result = QgsVectorFileWriter.writeAsVectorFormatV3(
                layer, out_file, transform_ctx, opts)
            err_code = _writer_error_code(write_result)
            results[fmt] = enum_value(err_code) == enum_value(WRITER_NO_ERROR)
            if results[fmt]:
                paths[fmt] = out_file
            else:
                log_message("{} export failed ({}): {}".format(
                    fmt.upper(), err_code, _writer_error_message(write_result)), MSG_WARNING)
        except Exception as e:
            log_message("{} export error: {}".format(fmt.upper(), e), MSG_WARNING)
    return any(results.values()), results, paths


# ------------------------------------------------------------ geometry util --
def transform_geometry(geom, xform):
    """Return a copy of `geom` transformed by `xform` (None on failure).
    QgsCoordinateTransform has no QgsGeometry overload; geometries must be
    transformed in place on a copy."""
    if geom is None:
        return None
    try:
        if xform is None or xform.isShortCircuited():
            return geom
        g = QgsGeometry(geom)
        res = g.transform(xform)
        if not _operation_ok(res):
            return None
        return g
    except Exception:
        return None


def _operation_ok(res):
    """True for Qgis.GeometryOperationResult.Success (an int 0 on older
    builds, a scoped enum on Qt6)."""
    if res is None:
        return False
    try:
        if res == GEOM_OP_SUCCESS:
            return True
    except Exception:
        pass
    return enum_value(res) == 0


def coerce_geometry(geom, wkb_type):
    """Coerce `geom` to `wkb_type` (e.g. MultiPoint -> Point) so memory and
    GPKG layers accept it. Returns the original geometry if not possible."""
    try:
        if geom is None or geom.isNull() or geom.wkbType() == wkb_type:
            return geom
        res = geom.coerceToType(wkb_type)
        if isinstance(res, (list, tuple)):
            res = res[0] if res else None
        if res is not None and not res.isNull():
            return res
    except Exception:
        pass
    return geom


def transform_xy(xs, ys, xform):
    """Transform coordinate arrays point by point (NaN where it fails)."""
    xs = np.asarray(xs, dtype=np.float64)
    ys = np.asarray(ys, dtype=np.float64)
    if xform is None or xform.isShortCircuited():
        return xs, ys
    from qgis.core import QgsPointXY
    ox, oy = np.full(xs.shape, np.nan), np.full(ys.shape, np.nan)
    for i in range(xs.size):
        try:
            p = xform.transform(QgsPointXY(float(xs[i]), float(ys[i])))
            ox[i], oy[i] = p.x(), p.y()
        except Exception:
            pass
    return ox, oy


# v2.20.1: planar fallback for ellipsoidal measures. QGIS keeps the ellipsoid
# parameters in a process-wide cache; when the first lookup happened in a
# download worker thread (v2.6 parallel downloads - e.g. the FEMA USA
# Structures file written on a dam's first run), every later ellipsoidal area
# or length in another thread came back NaN: outline areas, the 400 ft2 shed
# rule and the snapping of points onto outlines all failed on first runs only.
_FALLBACK_CRS = "EPSG:5070"         # NAD83 / Conus Albers (equal area), metres


def prime_ellipsoids(*acronyms):
    """Look the ellipsoids up in THIS thread before worker threads do (keeps
    the QGIS ellipsoid cache usable here); never fails."""
    try:
        from qgis.core import QgsEllipsoidUtils
        for a in ("EPSG:7019", "EPSG:7030") + tuple(x for x in acronyms if x):
            QgsEllipsoidUtils.ellipsoidParameters(a)
    except Exception:
        pass


def _planar_measure(geom, crs, ctx, area):
    """Planar area (ft2) / length (ft) of `geom` in a projected `crs`, else
    in NAD83 / Conus Albers; None when it cannot be measured."""
    try:
        from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform
        g = QgsGeometry(geom)
        units = crs.mapUnits()
        if crs.isGeographic() or units in (DIST_DEGREES, DIST_UNKNOWN):
            dst = QgsCoordinateReferenceSystem(_FALLBACK_CRS)
            ctx = ctx if ctx is not None else QgsProject.instance().transformContext()
            if g.transform(QgsCoordinateTransform(crs, dst, ctx)) != GEOM_OP_SUCCESS:
                return None
            units = DIST_METERS
        ft = QgsUnitTypes.fromUnitToUnitFactor(units, DIST_FEET)
        v = g.area() * ft * ft if area else g.length() * ft
        return v if math.isfinite(v) else None
    except Exception:
        return None


def finite_area_ft2(da, geom, crs, ctx=None):
    """da.measureArea in ft2; the planar area when that is NaN / fails."""
    from .compat import AREA_SQFT
    try:
        v = da.convertAreaMeasurement(da.measureArea(geom), AREA_SQFT)
        if v is not None and math.isfinite(v):
            return v
    except Exception:
        pass
    return _planar_measure(geom, crs, ctx, True)


def finite_length_ft(da, geom, crs, ctx=None):
    """da.measureLength in ft; the planar length when that is NaN / fails."""
    try:
        v = da.convertLengthMeasurement(da.measureLength(geom), DIST_FEET)
        if v is not None and math.isfinite(v):
            return v
    except Exception:
        pass
    return _planar_measure(geom, crs, ctx, False)


def buffer_distance_in_layer_units(distance_ft, crs):
    """Convert feet to the CRS's linear units (feet/US ft/meters/degrees)."""
    try:
        if crs is None or not crs.isValid():
            return float(distance_ft)
        units = crs.mapUnits()
        if units == DIST_UNKNOWN:
            log_message("Unknown CRS map units for buffer conversion; assuming feet",
                        MSG_WARNING)
            return float(distance_ft)
        try:
            factor = QgsUnitTypes.fromUnitToUnitFactor(DIST_FEET, units)
            if factor and math.isfinite(factor) and factor > 0:
                return float(distance_ft) * factor
        except Exception:
            pass
        if units == DIST_FEET:
            return float(distance_ft)
        if units == DIST_METERS:
            return distance_ft / _FT_PER_METER
        if units == DIST_DEGREES:
            return (distance_ft / _FT_PER_METER) / _METERS_PER_DEGREE_EQUATOR
        log_message("Unknown CRS map units for buffer conversion; assuming feet", MSG_WARNING)
        return float(distance_ft)
    except Exception:
        return float(distance_ft)


def ground_distance_xy(distance_ft, crs, lat=None, rect=None, transform_ctx=None):
    """2.18.0: (dx, dy) in the CRS's units that cover `distance_ft` on the
    ground (extent.distance_xy_in_crs). The latitude is `lat` (degrees) or,
    when not given, the centre of `rect` (in `crs`). Without either a
    geographic / Web Mercator CRS falls back to the plain conversion;
    projected feet / metre CRSs are the plain conversion in any case."""
    try:
        from .extent import distance_xy_in_crs, center_latitude
        if lat is None and rect is not None:
            if transform_ctx is None:
                transform_ctx = QgsProject.instance().transformContext()
            lat = center_latitude(rect, crs, transform_ctx)
        dx, dy = distance_xy_in_crs(distance_ft, crs, lat)
        return float(dx), float(dy)
    except Exception:
        d = buffer_distance_in_layer_units(distance_ft, crs)
        return d, d


def ground_distance_in_layer_units(distance_ft, crs, lat=None, rect=None, transform_ctx=None):
    """2.18.0: a radius in the CRS's units that reaches `distance_ft` on the
    ground in every direction (the larger of ground_distance_xy)."""
    return max(ground_distance_xy(distance_ft, crs, lat, rect, transform_ctx))


def points_rect(xys):
    """2.18.0: QgsRectangle around (x, y) pairs (finite ones); None if none."""
    xs = [float(x) for x, y in xys if x is not None and y is not None
          and math.isfinite(x) and math.isfinite(y)]
    ys = [float(y) for x, y in xys if x is not None and y is not None
          and math.isfinite(x) and math.isfinite(y)]
    if not xs:
        return None
    return QgsRectangle(min(xs), min(ys), max(xs), max(ys))


# ----------------------------------------------------------- field helpers --
def sanitize_field_name(name):
    n = "".join(c if (c.isalnum() or c == "_") else "_" for c in str(name))
    return n if n else "fld"


def prefixed_field(name, prefix):
    return sanitize_field_name("{}_{}".format(prefix, name))


def _type_key(t):
    """Comparable key for a field type: QVariant.Type (Qt5) and
    QMetaType.Type (QGIS >= 3.38 / Qt6) share the same numeric values for
    all basic types, and PyQt6 enums are not ints."""
    return enum_value(t)


_NUMERIC_TYPES = {_type_key(t) for t in (FT_INT, FT_UINT, FT_LONGLONG, FT_ULONGLONG, FT_DOUBLE)}


def make_field(name, qtype, sub_type=None):
    """A clean QgsField (no length limit, no constraints) so values from
    several sources never get rejected by the memory provider."""
    if sub_type is not None and _type_key(sub_type) != _type_key(FT_INVALID):
        try:
            return QgsField(name, qtype, "", 0, 0, "", sub_type)
        except TypeError:
            pass        # type and sub type from different Qt enum families
    try:
        return QgsField(name, qtype)
    except TypeError:
        # a QVariant.Type from a source field on a build that wants
        # QMetaType.Type (or the reverse): map by the shared numeric value
        return QgsField(name, _FT_BY_KEY.get(_type_key(qtype), FT_STRING))


_FT_BY_KEY = {_type_key(t): t for t in (FT_INT, FT_UINT, FT_LONGLONG, FT_ULONGLONG,
                                        FT_DOUBLE, FT_STRING)}


def _promote_type(a, b):
    ka, kb = _type_key(a), _type_key(b)
    if ka == kb:
        return a
    if ka in _NUMERIC_TYPES and kb in _NUMERIC_TYPES:
        if _type_key(FT_DOUBLE) in (ka, kb):
            return FT_DOUBLE
        return FT_LONGLONG
    return FT_STRING


class FieldPlan(object):
    """Merged output schema for several source field lists.

    - names are matched case-insensitively (GPKG/XLSX column names are
      case-insensitive, so 'Name' and 'NAME' would break the export)
    - differing types are promoted (Int+Double -> Double, else String)
    - fields in `skip` / `reserved` (lower-case names) are dropped
    """

    def __init__(self, skip=(), reserved=()):
        self._skip = {s.lower() for s in skip} | {r.lower() for r in reserved}
        self._order = []
        self._specs = {}          # lower -> [name, type, subtype, promoted]

    def add_fields(self, fields, rename=None):
        for f in fields:
            src_name = f.name()
            out_name = rename(src_name) if rename else src_name
            key = out_name.lower()
            if src_name.lower() in self._skip or key in self._skip:
                continue
            if key not in self._specs:
                self._specs[key] = [out_name, f.type(), f.subType(), False]
                self._order.append(key)
            else:
                spec = self._specs[key]
                if _type_key(spec[1]) != _type_key(f.type()):
                    spec[1] = _promote_type(spec[1], f.type())
                    spec[2] = None
                    spec[3] = True

    def qgs_fields(self, extra=()):
        out = [make_field(self._specs[k][0], self._specs[k][1], self._specs[k][2])
               for k in self._order]
        out.extend(extra)
        return out

    def mapping(self, src_fields, out_fields, rename=None):
        """[(src_idx, dst_idx, converter_or_None)] for one source layer."""
        dst_index = {out_fields.at(i).name().lower(): i for i in range(out_fields.count())}
        result = []
        for si in range(src_fields.count()):
            f = src_fields.at(si)
            out_name = rename(f.name()) if rename else f.name()
            key = out_name.lower()
            if key not in self._specs or key not in dst_index:
                continue
            spec = self._specs[key]
            conv = None
            if spec[3] or _type_key(spec[1]) != _type_key(f.type()):
                conv = _converter_for(spec[1])
            result.append((si, dst_index[key], conv))
        return result


def _to_text(v):
    if isinstance(v, str):
        return v
    for attr in ("isoformat",):
        if hasattr(v, attr):
            try:
                return getattr(v, attr)()
            except Exception:
                pass
    if hasattr(v, "toString"):
        try:
            return v.toString()
        except Exception:
            pass
    return str(v)


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return NULL


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return NULL


def _converter_for(qtype):
    k = _type_key(qtype)
    if k == _type_key(FT_STRING):
        return _to_text
    if k == _type_key(FT_DOUBLE):
        return _to_float
    if k in (_type_key(FT_LONGLONG), _type_key(FT_INT)):
        return _to_int
    return None


def is_null(v):
    if v is None:
        return True
    try:
        return v == NULL
    except Exception:
        return False


def copy_attributes_into(attrs, src_attrs, mapping):
    """Copy values into an existing output attribute list using a FieldPlan
    mapping (index based - much faster than name lookups per value)."""
    for si, di, conv in mapping:
        v = src_attrs[si]
        if conv is not None and not is_null(v):
            v = conv(v)
        attrs[di] = v
    return attrs


def copy_attributes(src_attrs, mapping, n_out):
    """New output attribute list (NULL-filled) populated from `src_attrs`."""
    return copy_attributes_into([NULL] * n_out, src_attrs, mapping)


def create_memory_layer(name, wkb_type, crs, fields):
    """Memory layer that keeps the CRS even when it has no EPSG code
    (e.g. HEC-RAS ESRI-WKT projections), using QgsMemoryProviderUtils."""
    if crs is None:
        from qgis.core import QgsCoordinateReferenceSystem
        crs = QgsCoordinateReferenceSystem()
    qfields = QgsFields()
    for f in fields:
        qfields.append(f)
    lyr = None
    try:
        lyr = QgsMemoryProviderUtils.createMemoryLayer(name, qfields, wkb_type, crs)
    except Exception:
        lyr = None
    if lyr is None or not lyr.isValid():
        uri = "{}?crs={}".format(QgsWkbTypes.displayString(wkb_type),
                                 crs.authid() if crs is not None else "")
        lyr = QgsVectorLayer(uri, name, "memory")
        lyr.dataProvider().addAttributes(list(fields))
        lyr.updateFields()
        if crs is not None and crs.isValid() and not crs.authid():
            lyr.setCrs(crs)
    return lyr


def add_features_batched(provider, features, batch_size):
    ok_all = True
    for i in range(0, len(features), batch_size):
        chunk = features[i:i + batch_size]
        res = provider.addFeatures(chunk)
        ok = res[0] if isinstance(res, tuple) else bool(res)
        if not ok:
            ok_all = False
    if not ok_all:
        err = ""
        try:
            errs = provider.errors()
            err = errs[-1] if errs else ""
        except Exception:
            pass
        log_message("Some features could not be stored in the output layer. {}".format(err),
                    MSG_WARNING)
    return ok_all


# ----------------------------------------------------------- raster access --
def read_block_cells():
    """constants.READ_BLOCK_TARGET_CELLS, read when called (tests lower it)."""
    return int(_CONST.READ_BLOCK_TARGET_CELLS)


def strip_rows(width, height, max_rows=None, target_cells=None):
    """v2.12: rows per strip for a whole-raster pass so one strip holds at
    most `target_cells` cells (default constants.READ_BLOCK_TARGET_CELLS,
    ~32 MB of float64) and never more than `max_rows`. Always >= 1 and
    <= height."""
    if target_cells is None:
        target_cells = read_block_cells()
    width, height = max(1, int(width)), max(1, int(height))
    rows = max(1, int(target_cells) // width)
    if max_rows:
        rows = min(rows, max(1, int(max_rows)))
    return max(1, min(rows, height))


def invert_geotransform(gt):
    """Pure-python inverse of a GDAL geotransform (None if degenerate)."""
    try:
        g0, g1, g2, g3, g4, g5 = [float(v) for v in gt]
    except Exception:
        return None
    det = g1 * g5 - g2 * g4
    if det == 0 or not math.isfinite(det):
        return None
    i1 = g5 / det
    i2 = -g2 / det
    i4 = -g4 / det
    i5 = g1 / det
    i0 = -(g0 * i1 + g3 * i2)
    i3 = -(g0 * i4 + g3 * i5)
    return (i0, i1, i2, i3, i4, i5)


class RasterSampler(object):
    """Fast, reusable reader for one raster band.

    Opens the raster once through GDAL (falls back to the QGIS data provider
    if GDAL cannot open the source). Point sampling reads one small window
    per point and finds the nearest valid cell with numpy; zonal maxima
    rasterise the zone polygon into a mask with GDAL instead of testing each
    cell with GEOS.

    Validity rules (same as v1.1): value must be finite, > -9999 and not the
    band's NoData value.
    """

    def __init__(self, raster_layer, buffer_ft=0.0):
        self.layer = raster_layer
        self.ok = False
        self.backend = None
        self.buffer = 0.0
        self.buffer_xy = (0.0, 0.0)   # 2.18.0: ground reach east-west / north-south
        self.crs = None
        self._ds = None
        self._band = None
        self._provider = None
        self._ogr_ds = None
        self._ogr_lyr = None
        self._rasterize_failed = False
        self._zone_errors = None
        self.gt = self.inv_gt = None
        self.xsize = self.ysize = 0
        self.nodata = None
        self.scale, self.offset = 1.0, 0.0
        if raster_layer is None or not raster_layer.isValid():
            return
        self.crs = raster_layer.crs()
        if buffer_ft:
            # 2.18.0: latitude-aware (raster centre) - a plain feet conversion
            # reaches only cos(lat) of the distance in Web Mercator and uses
            # equator degrees east-west in a geographic CRS
            try:
                rect = raster_layer.extent()
            except Exception:
                rect = None
            self.buffer_xy = ground_distance_xy(buffer_ft, self.crs, rect=rect)
            self.buffer = max(self.buffer_xy)
        if not self._open_gdal(get_layer_path(raster_layer)):
            self._open_provider()

    # -- setup -------------------------------------------------------------
    def _open_gdal(self, path):
        if not path:
            return False
        try:
            ds = gdal.Open(path, gdal.GA_ReadOnly)
        except Exception:
            ds = None
        if ds is None:
            return False
        try:
            gt = ds.GetGeoTransform()
            inv = invert_geotransform(gt)
            if inv is None:
                return False
            band = ds.GetRasterBand(1)
            if band is None:
                return False
            self._ds, self._band = ds, band
            self.gt, self.inv_gt = tuple(float(v) for v in gt), inv
            self.xsize, self.ysize = int(ds.RasterXSize), int(ds.RasterYSize)
            self.nodata = band.GetNoDataValue()
            scale, offset = band.GetScale(), band.GetOffset()
            self.scale = 1.0 if scale in (None, 0) else float(scale)
            self.offset = 0.0 if offset is None else float(offset)
            self.ok = True
            self.backend = "gdal"
            return True
        except Exception as e:
            log_message("GDAL could not read {} ({}); using QGIS provider".format(
                path, e), MSG_INFO)
            self._ds = self._band = None
            return False

    def _open_provider(self):
        try:
            prov = self.layer.dataProvider()
            ext = self.layer.extent()
            rx = self.layer.rasterUnitsPerPixelX()
            ry = self.layer.rasterUnitsPerPixelY()
            self.gt = (ext.xMinimum(), rx, 0.0, ext.yMaximum(), 0.0, -ry)
            self.inv_gt = invert_geotransform(self.gt)
            self.xsize, self.ysize = int(self.layer.width()), int(self.layer.height())
            has_nd = prov.sourceHasNoDataValue(1)
            self.nodata = prov.sourceNoDataValue(1) if has_nd else None
            self._provider = prov
            self.ok = self.inv_gt is not None and self.xsize > 0 and self.ysize > 0
            self.backend = "provider"
        except Exception as e:
            log_message("Raster '{}' cannot be sampled: {}".format(
                self.layer.name(), e), MSG_WARNING)
            self.ok = False

    def close(self):
        if self._zone_errors is not None:
            self._zone_errors.summary()
            self._zone_errors = None
        self._band = None
        self._ds = None
        self._provider = None
        self._ogr_lyr = None
        self._ogr_ds = None
        self.ok = False

    # -- low level ---------------------------------------------------------
    def _window_for_bbox(self, xmin, ymin, xmax, ymax):
        """Pixel window (xoff, yoff, w, h) of all cells touching the bbox,
        clipped to the raster; None if outside."""
        vals = (xmin, ymin, xmax, ymax)
        if not all(math.isfinite(v) for v in vals):
            return None
        inv = self.inv_gt
        pxs, pys = [], []
        for x, y in ((xmin, ymin), (xmin, ymax), (xmax, ymin), (xmax, ymax)):
            pxs.append(inv[0] + x * inv[1] + y * inv[2])
            pys.append(inv[3] + x * inv[4] + y * inv[5])
        c0 = max(0, int(math.floor(min(pxs))))
        c1 = min(self.xsize - 1, int(math.floor(max(pxs))))
        r0 = max(0, int(math.floor(min(pys))))
        r1 = min(self.ysize - 1, int(math.floor(max(pys))))
        if c0 > c1 or r0 > r1:
            return None
        return c0, r0, c1 - c0 + 1, r1 - r0 + 1

    def _read(self, xoff, yoff, w, h):
        xoff, yoff, w, h = int(xoff), int(yoff), int(w), int(h)
        if self._band is not None:
            try:
                arr = self._band.ReadAsArray(xoff, yoff, w, h)
            except Exception:
                return None
            if arr is None:
                return None
            return np.asarray(arr, dtype=np.float64)
        if self._provider is None:
            return None
        gt = self.gt
        rect = QgsRectangle(gt[0] + xoff * gt[1], gt[3] + (yoff + h) * gt[5],
                            gt[0] + (xoff + w) * gt[1], gt[3] + yoff * gt[5])
        block = self._provider.block(1, rect, w, h)
        if block is None or not block.isValid():
            return None
        out = np.empty((h, w), dtype=np.float64)
        for r in range(h):
            for c in range(w):
                out[r, c] = np.nan if block.isNoData(r, c) else block.value(r, c)
        return out

    def _valid_mask(self, arr):
        with np.errstate(invalid="ignore"):
            mask = np.isfinite(arr) & (arr > -9999)
            nd = self.nodata
            if nd is not None and math.isfinite(nd):
                mask &= ~np.isclose(arr, nd, rtol=1e-5, atol=0.0)
        return mask

    def _scaled(self, value):
        v = float(value)
        if self.scale != 1.0 or self.offset != 0.0:
            v = v * self.scale + self.offset
        return v

    def _window_gt(self, c0, r0):
        gt = self.gt
        return (gt[0] + c0 * gt[1] + r0 * gt[2], gt[1], gt[2],
                gt[3] + c0 * gt[4] + r0 * gt[5], gt[4], gt[5])

    # -- public ------------------------------------------------------------
    def window_values(self, xoff, yoff, w, h):
        """Values of a pixel window (scaled; NaN where NoData / invalid), or
        None when it cannot be read."""
        if not self.ok:
            return None
        arr = self._read(xoff, yoff, w, h)
        if arr is None:
            return None
        ok = self._valid_mask(arr)
        if self.scale != 1.0 or self.offset != 0.0:
            arr = arr * self.scale + self.offset
        return np.where(ok, arr, np.nan)

    def cell_centres(self, cols, rows):
        """Raster-CRS x, y of the centres of cells (cols, rows arrays)."""
        c = np.asarray(cols, dtype=np.float64) + 0.5
        r = np.asarray(rows, dtype=np.float64) + 0.5
        gt = self.gt
        return gt[0] + c * gt[1] + r * gt[2], gt[3] + c * gt[4] + r * gt[5]

    def sample(self, point):
        """Value of the cell containing `point` (raster CRS); if that cell is
        NoData, the nearest valid cell whose centre is within the buffer."""
        r = self.sample_located(point)
        return None if r is None else r[0]

    def sample_located(self, point):
        """(value, x, y, moved) as sample(): x, y is where the value was read
        (raster CRS) - the point itself, or the centre of the nearest valid
        cell within the buffer (moved=True). None when there is no value."""
        if not self.ok or point is None:
            return None
        try:
            x, y = float(point.x()), float(point.y())
        except Exception:
            return None
        if not (math.isfinite(x) and math.isfinite(y)):
            return None
        b = self.buffer if self.buffer > 0 else 0.0
        win = self._window_for_bbox(x - b, y - b, x + b, y + b)
        if win is None:
            return None
        c0, r0, w, h = win
        arr = self._read(c0, r0, w, h)
        if arr is None:
            return None
        valid = self._valid_mask(arr)
        if not valid.any():
            return None
        inv = self.inv_gt
        pc = int(math.floor(inv[0] + x * inv[1] + y * inv[2])) - c0
        pr = int(math.floor(inv[3] + x * inv[4] + y * inv[5])) - r0
        if 0 <= pr < h and 0 <= pc < w and valid[pr, pc]:
            return self._scaled(arr[pr, pc]), x, y, False
        if b <= 0:
            return None
        gt = self.gt
        cols = np.arange(w, dtype=np.float64) + (c0 + 0.5)
        rows = np.arange(h, dtype=np.float64) + (r0 + 0.5)
        cx = gt[0] + cols[None, :] * gt[1] + rows[:, None] * gt[2]
        cy = gt[3] + cols[None, :] * gt[4] + rows[:, None] * gt[5]
        bx, by = self.buffer_xy
        if bx > 0 and by > 0 and bx != by:
            # 2.18.0: geographic CRS - the buffer is an ellipse in degrees;
            # distances are compared in buffer units (ground-proportional)
            d2 = ((cx - x) / bx) ** 2 + ((cy - y) / by) ** 2
            cand = valid & (d2 <= 1.0)
        else:
            d2 = (cx - x) ** 2 + (cy - y) ** 2
            cand = valid & (d2 <= b * b)
        if not cand.any():
            return None
        idx = int(np.argmin(np.where(cand, d2, np.inf)))   # first min, row-major
        return self._scaled(arr.flat[idx]), float(cx.flat[idx]), float(cy.flat[idx]), True

    def cells_in_geometry(self, geom, max_cells=None):
        """(xs, ys, values) of valid cells whose centres fall inside `geom`
        (geom in the raster CRS). Empty arrays if none. v2.12: a window of
        more than `max_cells` cells (a large zone on a county-scale raster)
        is read in strips of rows; the result is the same, in the same
        row-major order, as one read of the whole window."""
        empty = (np.empty(0), np.empty(0), np.empty(0))
        if not self.ok or geom is None or geom.isNull() or geom.isEmpty():
            return empty
        bb = geom.boundingBox()
        win = self._window_for_bbox(bb.xMinimum(), bb.yMinimum(),
                                    bb.xMaximum(), bb.yMaximum())
        if win is None:
            return empty
        c0, r0, w, h = win
        if max_cells is None:
            max_cells = read_block_cells()
        rows = strip_rows(w, h, target_cells=max_cells) if w * h > max_cells else h
        parts = []
        for y0 in range(r0, r0 + h, rows):
            hh = min(rows, r0 + h - y0)
            arr = self._read(c0, y0, w, hh)
            if arr is None:
                return empty
            valid = self._valid_mask(arr)
            if not valid.any():
                continue
            mask = self._rasterize(geom, c0, y0, w, hh)
            if mask is None:
                return empty
            sel = valid & mask
            if not sel.any():
                continue
            rr, cc = np.nonzero(sel)
            parts.append((rr + (y0 - r0), cc, arr[sel]))
        if not parts:
            return empty
        rr = np.concatenate([p[0] for p in parts])
        cc = np.concatenate([p[1] for p in parts])
        vals = np.concatenate([p[2] for p in parts])
        gt = self.gt
        colc = cc + (c0 + 0.5)
        rowc = rr + (r0 + 0.5)
        xs = gt[0] + colc * gt[1] + rowc * gt[2]
        ys = gt[3] + colc * gt[4] + rowc * gt[5]
        if self.scale != 1.0 or self.offset != 0.0:
            vals = vals * self.scale + self.offset
        return xs, ys, vals

    def any_valid_in_bbox(self, xmin, ymin, xmax, ymax, max_cells=None,
                          positive_only=False):
        """True if any valid cell (> 0 with positive_only, for depth grids
        that store 0 for dry cells) touches the bbox (raster CRS). A cheap
        test that lets callers skip GEOS work for features far from any
        flooded cell. Very large windows answer True (not worth reading)."""
        if not self.ok:
            return False
        win = self._window_for_bbox(xmin, ymin, xmax, ymax)
        if win is None:
            return False
        c0, r0, w, h = win
        if w * h > (read_block_cells() if max_cells is None else max_cells):
            return True
        arr = self._read(c0, r0, w, h)
        if arr is None:
            return True
        mask = self._valid_mask(arr)
        if positive_only:
            with np.errstate(invalid="ignore"):
                mask &= (arr * self.scale + self.offset) > 0
        return bool(mask.any())

    def max_in_geometry(self, geom):
        """Max valid value over cells whose centres fall inside `geom`
        (geom must be in the raster CRS)."""
        _xs, _ys, vals = self.cells_in_geometry(geom)
        return float(vals.max()) if vals.size else None

    def max_in_geometry_located(self, geom):
        """(max value, x, y of that cell centre) inside `geom`, or None."""
        xs, ys, vals = self.cells_in_geometry(geom)
        if not vals.size:
            return None
        i = int(np.argmax(vals))
        return float(vals[i]), float(xs[i]), float(ys[i])

    def values_at(self, xs, ys, max_window_cells=None):
        """Valid values at many points at once (raster CRS). Returns a float
        array with NaN where the point is outside the raster or the cell is
        NoData. Reads one window per group of nearby points instead of one
        read per point."""
        xs = np.asarray(xs, dtype=np.float64).ravel()
        ys = np.asarray(ys, dtype=np.float64).ravel()
        out = np.full(xs.shape, np.nan)
        if not self.ok or xs.size == 0:
            return out
        if max_window_cells is None:
            max_window_cells = read_block_cells()
        inv = self.inv_gt
        finite = np.isfinite(xs) & np.isfinite(ys)
        with np.errstate(invalid="ignore"):
            colf = np.where(finite, inv[0] + xs * inv[1] + ys * inv[2], -1.0)
            rowf = np.where(finite, inv[3] + xs * inv[4] + ys * inv[5], -1.0)
        cols = np.floor(colf).astype(np.int64)
        rows = np.floor(rowf).astype(np.int64)
        inside = finite & (cols >= 0) & (cols < self.xsize) & (rows >= 0) & (rows < self.ysize)
        idx = np.nonzero(inside)[0]
        if idx.size == 0:
            return out
        order = idx[np.lexsort((cols[idx], rows[idx]))]
        stack = [order]
        while stack:
            sel = stack.pop()
            r0, r1 = int(rows[sel].min()), int(rows[sel].max())
            c0, c1 = int(cols[sel].min()), int(cols[sel].max())
            w, h = c1 - c0 + 1, r1 - r0 + 1
            if w * h > max_window_cells and sel.size > 1:
                half = sel.size // 2
                stack.append(sel[:half])
                stack.append(sel[half:])
                continue
            arr = self._read(c0, r0, w, h)
            if arr is None:
                continue
            vals = arr[rows[sel] - r0, cols[sel] - c0]
            ok = self._valid_mask(vals)
            if self.scale != 1.0 or self.offset != 0.0:
                vals = vals * self.scale + self.offset
            out[sel[ok]] = vals[ok]
        return out

    def valid_extent(self, positive_only=True, rows_per_block=512):
        """Bounding box (QgsRectangle, raster CRS) of cells that are valid
        (and > 0 when positive_only) - i.e. the inundated area of a depth
        grid. Reads the raster in strips (v2.12: of at most
        READ_BLOCK_TARGET_CELLS cells, and `rows_per_block` rows). None if
        no such cells."""
        if not self.ok:
            return None
        cmin, cmax, rmin, rmax = None, None, None, None
        rows_per_block = strip_rows(self.xsize, self.ysize, rows_per_block)
        for y0 in range(0, self.ysize, rows_per_block):
            h = min(rows_per_block, self.ysize - y0)
            arr = self._read(0, y0, self.xsize, h)
            if arr is None:
                continue
            m = self._valid_mask(arr)
            if positive_only:
                with np.errstate(invalid="ignore"):
                    # v2.3.1: apply scale/offset before > 0 test (consistent with any_valid_in_bbox)
                    m &= (arr * self.scale + self.offset) > 0
            if not m.any():
                continue
            rr = np.nonzero(m.any(axis=1))[0]
            cc = np.nonzero(m.any(axis=0))[0]
            rmin = y0 + int(rr[0]) if rmin is None else rmin
            rmax = y0 + int(rr[-1])
            cmin = int(cc[0]) if cmin is None else min(cmin, int(cc[0]))
            cmax = int(cc[-1]) if cmax is None else max(cmax, int(cc[-1]))
        if rmin is None:
            return None
        gt = self.gt
        xs = [gt[0] + c * gt[1] + r * gt[2] for c in (cmin, cmax + 1) for r in (rmin, rmax + 1)]
        ys = [gt[3] + c * gt[4] + r * gt[5] for c in (cmin, cmax + 1) for r in (rmin, rmax + 1)]
        return QgsRectangle(min(xs), min(ys), max(xs), max(ys))

    def _ensure_ogr_layer(self):
        if self._ogr_lyr is not None:
            return self._ogr_lyr
        ds = None
        try:
            drv = ogr.GetDriverByName("MEM") or ogr.GetDriverByName("Memory")
            if drv is not None:
                ds = drv.CreateDataSource("zone")
        except Exception:
            ds = None
        if ds is None:
            try:   # GDAL >= 3.11: MEM driver handles vectors too
                ds = gdal.GetDriverByName("MEM").Create("zone", 0, 0, 0, gdal.GDT_Unknown)
            except Exception:
                ds = None
        if ds is None:
            return None
        try:
            lyr = ds.CreateLayer("zone", None, ogr.wkbUnknown)
        except Exception:
            lyr = None
        if lyr is None:
            return None
        self._ogr_ds, self._ogr_lyr = ds, lyr
        return lyr

    def _rasterize(self, geom, c0, r0, w, h):
        """Boolean mask of window cells whose centre lies inside geom."""
        if self._rasterize_failed:
            return None
        lyr = self._ensure_ogr_layer()
        if lyr is None:
            self._rasterize_failed = True
            log_message("Zone rasterisation unavailable (no OGR memory driver); "
                        "edge-buffer values will be empty", MSG_WARNING)
            return None
        if self._zone_errors is None:
            self._zone_errors = LimitedLogger("Zone rasterisation")
        try:
            og = ogr.CreateGeometryFromWkb(_wkb_bytes(geom))
            if og is None:
                self._zone_errors.log("could not convert a zone geometry")
                return None
            if og.HasCurveGeometry():
                og = og.GetLinearGeometry()
            og.FlattenTo2D()
            mem = gdal.GetDriverByName("MEM").Create("", int(w), int(h), 1, gdal.GDT_Byte)
            mem.SetGeoTransform(self._window_gt(c0, r0))
            feat = ogr.Feature(lyr.GetLayerDefn())
            feat.SetGeometry(og)
            lyr.CreateFeature(feat)
            try:
                err = gdal.RasterizeLayer(mem, [1], lyr, burn_values=[1])
            finally:
                lyr.DeleteFeature(feat.GetFID())
            if err not in (0, None):
                self._zone_errors.log("GDAL RasterizeLayer returned error {}".format(err))
                return None
            m = mem.GetRasterBand(1).ReadAsArray()
            mem = None
            if m is None:
                return None
            return np.asarray(m) > 0
        except Exception as e:
            self._zone_errors.log(str(e))
            return None


def reproject_raster(src_path, dst_path, src_crs, dst_crs, transform_ctx):
    """Nearest-cell copy of a single-band raster in another CRS (Float32
    GeoTIFF, NoData -9999), with about the same cell size on the ground. The
    source CRS is the layer's (it may differ from the file's own, e.g. a
    CRS assigned in QGIS to a file without one). Returns dst_path."""
    from qgis.core import QgsCoordinateTransform
    with gdal_exceptions():
        src = gdal.Open(src_path, gdal.GA_ReadOnly)
        if src is None:
            raise RuntimeError("Could not open {}".format(src_path))
        try:
            gt = src.GetGeoTransform()
            w, h = src.RasterXSize, src.RasterYSize
            ext = QgsRectangle(gt[0], gt[3] + h * gt[5], gt[0] + w * gt[1], gt[3])
            xf = QgsCoordinateTransform(src_crs, dst_crs, transform_ctx)
            out = xf.transformBoundingBox(ext)
            ow, oh = out.xMaximum() - out.xMinimum(), out.yMaximum() - out.yMinimum()
            cell = math.sqrt(max(ow * oh, 1e-12) / float(max(1, w * h)))
            nx = max(1, int(math.ceil(ow / cell)))
            ny = max(1, int(math.ceil(oh / cell)))
            nd = src.GetRasterBand(1).GetNoDataValue()
            kw = {"format": "GTiff", "width": nx, "height": ny,
                  "outputBounds": (out.xMinimum(), out.yMinimum(),
                                   out.xMinimum() + nx * cell, out.yMinimum() + ny * cell),
                  "srcSRS": src_crs.toWkt(), "dstSRS": dst_crs.toWkt(),
                  "outputType": gdal.GDT_Float32, "resampleAlg": "near", "dstNodata": -9999.0,
                  "creationOptions": ["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_SAFER"]}
            if nd is not None:
                kw["srcNodata"] = nd
            dst_path = prepare_output_path(dst_path)
            ds = gdal.Warp(dst_path, src, options=gdal.WarpOptions(**kw))
            if ds is None:
                raise RuntimeError("Reprojection failed for {}".format(src_path))
            ds.FlushCache()
            ds = None
        finally:
            src = None
    return dst_path


def geometry_from_wkb(data):
    """A QgsGeometry from WKB bytes. QgsGeometry.fromWkb is an INSTANCE method in the real API
    (it fills the geometry and returns None); calling it on the class fails in real QGIS
    ("first argument of unbound method must have type 'QgsGeometry'") - found by the first
    real-QGIS smoke run (2.12.1), which had made the saved evaluation unreadable."""
    g = QgsGeometry()
    if not data:
        return g
    try:
        r = g.fromWkb(bytes(data))
    except TypeError:                     # builds that only take a QByteArray
        from qgis.PyQt.QtCore import QByteArray
        r = g.fromWkb(QByteArray(bytes(data)))
    return r if isinstance(r, QgsGeometry) else g


def _wkb_bytes(geom):
    wkb = geom.asWkb()
    if isinstance(wkb, (bytes, bytearray)):
        return bytes(wkb)
    if hasattr(wkb, "data"):          # PyQt5 QByteArray
        return bytes(wkb.data())
    return bytes(wkb)


# ----------------------------------------------------------- classification --
def curve_pair(kind):
    """(low_coefficients, high_coefficients, v_max) for 'found', 'mobile'
    or 'vehicle'."""
    return (HAZARD_COEFFICIENTS[kind + "_low"], HAZARD_COEFFICIENTS[kind + "_high"],
            CURVE_V_MAX.get(kind))


def classify_values(d, v, low_curve, high_curve, v_max=None):
    """ACER-11 class for 1-D arrays of valid depth/velocity values.
    Velocities above v_max (the figure's range) are evaluated at v_max."""
    cls = np.full(np.shape(d), CLASS_LOW, dtype=np.int16)
    if v_max is not None:
        v = np.clip(np.asarray(v, dtype=np.float64), 0.0, v_max)
    thr = evaluate_polynomial(low_curve, v)
    # v2.11.0: a dry cell (depth 0) stays LOW where the low curve is at 0 ft
    # (houses at 19 ft/s and above in the REG_05.2026 table)
    cls[(d >= thr) & (d > 0)] = CLASS_JUDGMENT
    thr = evaluate_polynomial(high_curve, v)
    cls[d >= thr] = CLASS_HIGH
    return cls


def classify_scalar(h, v, low_key, high_key):
    v_max = CURVE_V_MAX.get(low_key.split("_")[0])
    v = float(v) if v_max is None else min(max(float(v), 0.0), v_max)
    lo = evaluate_polynomial(HAZARD_COEFFICIENTS[low_key], np.array([v]))[0]
    hi = evaluate_polynomial(HAZARD_COEFFICIENTS[high_key], np.array([v]))[0]
    if h >= hi:
        return CLASS_HIGH
    if h >= lo and h > 0:
        return CLASS_JUDGMENT
    return CLASS_LOW


def log_threshold_verification():
    """Threshold table + known-answer tests to the message log."""
    log_message("=" * 60)
    log_message("ACER TM 11 THRESHOLD VERIFICATION (houses / mobile homes: REG_05.2026 table; "
                "vehicles: quartic)")
    log_message("=" * 60)
    velocities = np.array([0.0, 4.0, 6.0, 10.0, 15.0])
    for label, kind in [("HOUSE ON FOUNDATION (Fig 2):", "found"),
                        ("MOBILE HOME (Fig 3):", "mobile"),
                        ("PASSENGER VEHICLE (Fig 4):", "vehicle")]:
        if kind + "_low" not in HAZARD_COEFFICIENTS:
            continue
        log_message(label)
        lo_c, hi_c, vmax = curve_pair(kind)
        for v in velocities:
            vv = np.array([min(v, vmax)])
            lo = evaluate_polynomial(lo_c, vv)[0]
            hi = evaluate_polynomial(hi_c, vv)[0]
            log_message("  V={:>5.1f} fps: low={:.2f} ft, high={:.2f} ft".format(v, lo, hi))
    names = {1: "HIGH(red)", 2: "JUDGMENT(yellow)", 3: "LOW(green)"}
    for h, v, kind in [(3.0, 6.0, "found"), (4.0, 4.0, "found"), (3.0, 12.0, "mobile"),
                       (2.5, 2.0, "vehicle")]:
        if kind + "_low" not in HAZARD_COEFFICIENTS:
            continue
        cls = classify_scalar(h, v, kind + "_low", kind + "_high")
        log_message("KNOWN TEST {} H={}, V={} -> Class {} = {}".format(
            kind, h, v, cls, names.get(cls, "NO DATA")))
    log_message("Expected: found 2, found 2, mobile 1 (HIGH), vehicle 2")
    log_message("=" * 60)
