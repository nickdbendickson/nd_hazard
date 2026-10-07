# -*- coding: utf-8 -*-
"""Second, independent population-at-risk estimate from a USER-SUPPLIED
population layer, compared with the plugin's structure-based estimate.

The plugin counts people structure by structure (NSI, address points,
building outlines, TM 11 persons per dwelling). This module counts them a
different way, from a layer the engineer supplies (nothing is downloaded):

  - a POLYGON layer with a population attribute (census blocks / block
    groups ...; the engineer names the field, and optionally a second
    field for the daytime population and one for housing units), or
  - a POPULATION RASTER (people per cell, LandScan-like; or people per
    square mile / per square kilometre - the engineer says which).

FLOODED AREA. The flooded area is the set of cells of the depth raster
that are wet: valid and deeper than `min_depth_ft` (default 0.0, i.e. any
wet cell; NoData counts as dry - many depth grids store NoData for dry
cells).

POLYGONS. The population of a polygon is assumed spread evenly over its
area (an area-weighted estimate; a real block has its houses somewhere in
it). The flooded share of a polygon is measured at the depth raster's
resolution: the polygons are burned into the depth grid (GDAL rasterises
every polygon in one pass per strip of the raster, so thousands of blocks
and large rasters stay cheap) and

    share flooded = wet cells / cells inside the polygon
                    (cells whose CENTRE lies inside the polygon)
    people at risk = population x share flooded

A polygon that reaches beyond the depth raster is scaled by the part of
it that lies inside the raster (its area outside the raster is not
counted as flooded). A polygon too small to hold a cell centre takes the
state of the raster cell under its representative point (all or nothing);
a polygon outside the raster gets 0. Block polygons are read in the
depth raster's CRS (QgsCoordinateTransform); invalid geometries are
repaired with makeValid; NULL, negative (census "suppressed" sentinels
are often negative), and non-numeric populations count as 0 and are
reported; multipart polygons are burned as one. Overlapping polygons are
not expected (a cell goes to the last polygon burned).

RASTERS. Two ways to put a population raster on the depth grid (default
"auto"):
  "centre"   each population cell is looked up at its CENTRE in the depth
             raster (nearest depth cell); its people count if that cell is
             wet. Right when the population cells are about as fine as the
             depth cells or finer.
  "fraction" each population cell is weighted by the share of it that is
             wet (wet depth cells x depth cell area / population cell
             area, at most 1). Right when the population cells are
             coarser than the depth cells (LandScan / WorldPop / GHSL
             against a 10 - 30 ft flood grid), where "centre" would call a
             partly flooded 1 km cell all wet or all dry. A population cell
             that reaches beyond the depth raster is divided by its whole
             area (the part outside is not counted as flooded).
  "auto"     "fraction" when a population cell has at least
             AUTO_FRACTION_RATIO x the ground area of a depth cell.
Density rasters (per square mile / kilometre) are converted with the
GROUND area of the population cell: the depth cell's ground area (in the
depth CRS's units, or at the model area's latitude for a geographic
depth raster) times the measured ratio of the two cell areas (one
population cell mapped into the depth CRS), so a geographic (degree)
population raster needs no separate degree-to-foot conversion.

Coordinates between the two rasters' CRSs go through a bilinear lattice
of QgsCoordinateTransform nodes (LATTICE_NODES per side over the area
mapped), not one transform per cell, so a large raster in another CRS is
still fast.

CAVEATS (repeated in the summary text). Census counts and structure
counts differ by construction (people by residence, group quarters,
seasonal housing, vacant units, structures NSI / address points do not
map); the day population and housing units exist only when the engineer
supplies the fields; the population is treated as evenly spread over each
polygon / cell. The comparison does not change the classification.

Everything here is a screening estimate, thresholds are parameters.
"""

import csv
import math
import os

import numpy as np

from qgis.core import (
    QgsCoordinateTransform, QgsFeatureRequest, QgsGeometry, QgsPointXY, QgsRasterLayer,
    QgsRectangle, QgsVectorLayer,
)
from osgeo import gdal, ogr

from . import crosscheck as XC
from .compat import GEOM_POLYGON, FT_INT, FT_UINT, FT_LONGLONG, FT_ULONGLONG, FT_DOUBLE, \
    enum_value
from .extent import center_latitude, distance_xy_in_crs
from .helpers import (
    LimitedLogger, RasterSampler, _wkb_bytes, is_null, prepare_output_path, transform_geometry,
)

# ------------------------------------------------------------------ settings --
# SCREENING VALUES CHOSEN BY THE ENGINEER (not regulatory numbers)
DEFAULT_MIN_DEPTH_FT = 0.0            # a cell is wet when its depth is above this
DEFAULT_DEPTH_CLASSES_FT = (2.0, 5.0, 10.0)     # upper edges of the depth classes
AUTO_FRACTION_RATIO = 2.0             # "auto": fraction method from this cell-area ratio
LATTICE_NODES = 33                    # per side, CRS mapping between two rasters
MAX_STRIP_CELLS = 4000000             # cells per strip read (memory bound)

RASTER_UNITS = {                      # label, ft2 of ground per unit of area
    "per_cell": ("people per cell", None),
    "per_sq_mi": ("people per square mile", 27878400.0),
    "per_sq_km": ("people per square kilometre", 10763910.416709722),
}
METHODS = ("auto", "centre", "fraction")

CROSSCHECK_CSV = "Population_Source_CrossCheck.csv"
POLYGON_CSV = "Population_Source_Polygons.csv"
CROSSCHECK_HEAD = ["Population_source", "Quantity", "Source_value", "Source_field",
                   "Plugin_value", "Plugin_quantity", "Ratio_plugin_to_source", "Verdict"]

DEFAULTS = {
    # cfg keys the pipeline would add (see options_from_cfg)
    "pop_source": None,               # QgsVectorLayer (polygons) or QgsRasterLayer
    "pop_field": "",                  # polygon layer: population attribute
    "pop_day_field": "",              # optional: daytime population attribute
    "pop_housing_field": "",          # optional: housing units attribute
    "pop_id_field": "",               # optional: label of each polygon in the CSV
    "pop_period": "night",            # time of day the main field / raster stands for
    "pop_raster_units": "per_cell",   # per_cell | per_sq_mi | per_sq_km
    "pop_method": "auto",             # auto | centre | fraction (rasters)
    "pop_min_depth_ft": DEFAULT_MIN_DEPTH_FT,
}

CAVEATS = (
    "Census counts and structure counts differ by construction (people by residence, group "
    "quarters, seasonal and vacant housing, structures that NSI / address / outline layers do "
    "not map); the population is treated as spread evenly over each polygon or cell. The "
    "comparison is a reason to look, it does not change the classification",
)


# ---------------------------------------------------------------- small utils --
def options_from_cfg(cfg):
    """Keyword arguments for estimate_population_at_risk / run_check from the
    pipeline's cfg dict (missing keys take DEFAULTS)."""
    def g(k):
        v = cfg.get(k)
        return DEFAULTS[k] if v is None else v
    return {
        "pop_field": g("pop_field"), "day_field": g("pop_day_field"),
        "housing_field": g("pop_housing_field"), "id_field": g("pop_id_field"),
        "period": g("pop_period"), "raster_units": g("pop_raster_units"),
        "method": g("pop_method"), "min_depth_ft": float(g("pop_min_depth_ft")),
    }


def _num(v):
    """(float or None, reason) of an attribute value. reason: "" (ok), "null",
    "text" (not a number), "negative" (kept as None), "nonfinite"."""
    if is_null(v):
        return None, "null"
    try:
        f = float(str(v).replace(",", "").strip()) if isinstance(v, str) else float(v)
    except (TypeError, ValueError):
        return None, "text"
    if not math.isfinite(f):
        return None, "nonfinite"
    if f < 0:
        return None, "negative"
    return f, ""


def numeric_fields(layer):
    """Names of the numeric fields of a vector layer (for the field pickers)."""
    numeric = {enum_value(t) for t in (FT_INT, FT_UINT, FT_LONGLONG, FT_ULONGLONG, FT_DOUBLE)}
    out = []
    try:
        flds = layer.fields()
        for i in range(flds.count()):
            f = flds.at(i)
            try:
                ok = f.isNumeric()
            except Exception:
                ok = enum_value(f.type()) in numeric
            if ok:
                out.append(f.name())
    except Exception:
        pass
    return out


def suggest_fields(layer):
    """{"pop": name or None, "day": ..., "housing": ...}: numeric fields whose NAME
    suggests the role (only a suggestion for the field picker: no data product's
    column layout is assumed, and the engineer chooses)."""
    names = numeric_fields(layer)
    low = {n: n.lower() for n in names}

    def pick(*words, exclude=()):
        for n in names:
            l = low[n]
            if any(w in l for w in words) and not any(x in l for x in exclude):
                return n
        return None
    return {"pop": pick("pop", exclude=("day", "hu", "hous")),
            "day": pick("day"),
            "housing": pick("housing", "hu", "dwell", "units")}


def _find_field(fields, name):
    """Index of `name` in a QgsFields (exact, then case-insensitive); -1 if none."""
    if not name:
        return -1
    i = fields.indexOf(name)
    if i >= 0:
        return i
    low = str(name).lower()
    for j in range(fields.count()):
        if fields.at(j).name().lower() == low:
            return j
    return -1


def raster_rect(sampler):
    """Bounding box (QgsRectangle) of a RasterSampler's grid, in its CRS."""
    gt = sampler.gt
    xs = [gt[0] + c * gt[1] + r * gt[2] for c in (0, sampler.xsize) for r in (0, sampler.ysize)]
    ys = [gt[3] + c * gt[4] + r * gt[5] for c in (0, sampler.xsize) for r in (0, sampler.ysize)]
    return QgsRectangle(min(xs), min(ys), max(xs), max(ys))


def _pixel_of(sampler, x, y):
    inv = sampler.inv_gt
    return inv[0] + x * inv[1] + y * inv[2], inv[3] + x * inv[4] + y * inv[5]


def _to_int_index(v):
    """floor of an array with NaN -> -1 (int64)."""
    ok = np.isfinite(v)
    return np.where(ok, np.floor(np.where(ok, v, 0.0)), -1.0).astype(np.int64)


def cell_area_ft2(sampler, lat=None):
    """Ground area (ft2) of one cell of a raster (its CRS's units converted with
    the latitude for geographic / Web Mercator rasters)."""
    gt = sampler.gt
    kx, ky = distance_xy_in_crs(1.0, sampler.crs, lat)          # CRS units per foot
    w = math.hypot(gt[1], gt[4]) / kx
    h = math.hypot(gt[2], gt[5]) / ky
    return w * h


class _Mapper(object):
    """Coordinates from src_crs to dst_crs. Identity for the same CRS,
    otherwise bilinear interpolation over a lattice of LATTICE_NODES^2 exact
    QgsCoordinateTransform results covering `rect` (in src_crs); points outside
    the rectangle are extrapolated linearly. NaN where a lattice node fails."""

    def __init__(self, src_crs, dst_crs, ctx, rect, nodes=LATTICE_NODES):
        self.identity = True
        self.x0, self.y0 = rect.xMinimum(), rect.yMinimum()
        self.w, self.h = max(rect.width(), 1e-9), max(rect.height(), 1e-9)
        try:
            xf = QgsCoordinateTransform(src_crs, dst_crs, ctx)
            self.identity = bool(xf.isShortCircuited())
        except Exception:
            xf = None
            self.identity = False
        if self.identity:
            return
        n = int(nodes)
        self.n = n
        w, h = self.w, self.h
        X = np.full((n, n), np.nan)
        Y = np.full((n, n), np.nan)
        for j in range(n):
            for i in range(n):
                try:
                    p = xf.transform(QgsPointXY(self.x0 + w * i / (n - 1.0),
                                                self.y0 + h * j / (n - 1.0)))
                    X[j, i], Y[j, i] = p.x(), p.y()
                except Exception:
                    pass
        self.X, self.Y = X, Y

    def __call__(self, x, y):
        x = np.asarray(x, dtype=np.float64)
        y = np.asarray(y, dtype=np.float64)
        if self.identity:
            return x, y
        n = self.n
        fx = (x - self.x0) / self.w * (n - 1.0)
        fy = (y - self.y0) / self.h * (n - 1.0)
        with np.errstate(invalid="ignore"):
            i0 = np.clip(np.floor(np.where(np.isfinite(fx), fx, 0.0)), 0, n - 2).astype(np.int64)
            j0 = np.clip(np.floor(np.where(np.isfinite(fy), fy, 0.0)), 0, n - 2).astype(np.int64)
        tx, ty = fx - i0, fy - j0

        def lerp(A):
            return ((1 - ty) * ((1 - tx) * A[j0, i0] + tx * A[j0, i0 + 1])
                    + ty * ((1 - tx) * A[j0 + 1, i0] + tx * A[j0 + 1, i0 + 1]))
        return lerp(self.X), lerp(self.Y)

    def image_bbox(self):
        """(xmin, ymin, xmax, ymax) of the lattice nodes in the destination CRS."""
        if self.identity:
            return self.x0, self.y0, self.x0 + self.w, self.y0 + self.h
        ok = np.isfinite(self.X) & np.isfinite(self.Y)
        if not ok.any():
            return None
        return (float(self.X[ok].min()), float(self.Y[ok].min()),
                float(self.X[ok].max()), float(self.Y[ok].max()))


def depth_class_labels(edges):
    e = [float(v) for v in edges]
    if not e:
        return ["all wet"]
    out = ["<= {:g} ft".format(e[0])]
    for a, b in zip(e[:-1], e[1:]):
        out.append("{:g} - {:g} ft".format(a, b))
    out.append("> {:g} ft".format(e[-1]))
    return out


def _empty_result(kind, name, **kw):
    r = {"kind": kind, "layer": name, "ok": False, "error": None, "method": None,
         "period": "night", "min_depth_ft": DEFAULT_MIN_DEPTH_FT, "par": None, "par_day": None,
         "housing": None, "pop_field": "", "day_field": "", "housing_field": "",
         "pop_in_model": None, "pop_wet_polygons": None, "classes": [], "par_by_class": [],
         "n_features": 0, "n_used": 0, "n_wet": 0, "n_null": 0, "n_negative": 0,
         "n_text": 0, "n_repaired": 0, "n_bad_geometry": 0, "n_outside": 0, "n_subcell": 0,
         "n_partial": 0, "n_strips_unread": 0, "rows": [], "notes": []}
    r.update(kw)
    return r


# ------------------------------------------------------------------- polygons --
def _polygonal(geom, result):
    """A valid polygon / multipolygon geometry from `geom` (repaired with
    makeValid when needed), or None."""
    if geom is None or geom.isNull() or geom.isEmpty():
        return None
    try:
        valid = bool(geom.isGeosValid())
    except Exception:
        valid = True
    if not valid:
        try:
            fixed = geom.makeValid()
        except Exception:
            fixed = None
        if fixed is None or fixed.isNull() or fixed.isEmpty():
            return None
        geom = fixed
        result["n_repaired"] += 1
    try:
        if geom.type() != GEOM_POLYGON:
            # makeValid can return a collection (polygon + a sliver line ...)
            parts = [g for g in geom.asGeometryCollection() if g.type() == GEOM_POLYGON]
            if not parts:
                return None
            geom = QgsGeometry.collectGeometry(parts)
    except Exception:
        return None
    return geom


def _area(geom):
    try:
        a = float(geom.area())
        if math.isfinite(a):
            return a
    except Exception:
        pass
    total = 0.0
    try:
        for poly in geom.asMultiPolygon():
            for k, ring in enumerate(poly):
                s = 0.5 * abs(sum(ring[j].x() * ring[j + 1].y() - ring[j + 1].x() * ring[j].y()
                                  for j in range(len(ring) - 1)))
                total += s if k == 0 else -s
    except Exception:
        pass
    return total


def _new_ogr_layer():
    ds = None
    try:
        drv = ogr.GetDriverByName("MEM") or ogr.GetDriverByName("Memory")
        if drv is not None:
            ds = drv.CreateDataSource("popsource")
    except Exception:
        ds = None
    if ds is None:
        try:
            ds = gdal.GetDriverByName("MEM").Create("popsource", 0, 0, 0, gdal.GDT_Unknown)
        except Exception:
            ds = None
    if ds is None:
        return None, None
    try:
        lyr = ds.CreateLayer("popsource", None, ogr.wkbUnknown)
        lyr.CreateField(ogr.FieldDefn("pid", ogr.OFTInteger))
    except Exception:
        return None, None
    return ds, lyr


def _read_polygons(layer, depth, ctx, fields_idx, result, progress_cb):
    """[(index, id label, population, day, housing, bbox, area, geom-in-raster-CRS
    WKB)] of the polygons of `layer` that touch the depth raster's extent."""
    to_r = QgsCoordinateTransform(layer.crs(), depth.crs, ctx)
    req = QgsFeatureRequest()
    try:
        rect = raster_rect(depth)
        back = QgsCoordinateTransform(depth.crs, layer.crs(), ctx)
        req.setFilterRect(rect if back.isShortCircuited() else back.transformBoundingBox(rect))
    except Exception:
        pass
    try:
        req.setSubsetOfAttributes([i for i in fields_idx.values() if i >= 0])
    except Exception:
        pass
    problems = LimitedLogger("Population layer")
    total = max(1, layer.featureCount())
    out = []
    ip, iday, ihu, iid = (fields_idx[k] for k in ("pop", "day", "hu", "id"))
    for n, feat in enumerate(layer.getFeatures(req)):
        if progress_cb and n % 500 == 0:
            progress_cb(min(20.0, 20.0 * n / total), "Population polygons: {}".format(n))
        result["n_features"] += 1
        attrs = feat.attributes()
        vals = []
        for i in (ip, iday, ihu):
            if i < 0:
                vals.append(None)
                continue
            v, why = _num(attrs[i])
            if i == ip and why:
                result["n_" + {"null": "null", "text": "text", "negative": "negative",
                               "nonfinite": "text"}[why]] += 1
            vals.append(v)
        geom = _polygonal(feat.geometry(), result)
        if geom is None:
            result["n_bad_geometry"] += 1
            continue
        g = transform_geometry(geom, to_r)
        if g is None or g.isNull() or g.isEmpty():
            result["n_bad_geometry"] += 1
            problems.log("feature {}: could not be transformed".format(feat.id()))
            continue
        try:
            wkb = _wkb_bytes(g)
            bb = g.boundingBox()
            area = _area(g)
            rep = g.pointOnSurface().asPoint()
        except Exception as e:
            result["n_bad_geometry"] += 1
            problems.log("feature {}: {}".format(feat.id(), e))
            continue
        label = feat.id() if iid < 0 or is_null(attrs[iid]) else attrs[iid]
        out.append({"label": label, "pop": vals[0] or 0.0, "day": vals[1], "hu": vals[2],
                    "bbox": (bb.xMinimum(), bb.yMinimum(), bb.xMaximum(), bb.yMaximum()),
                    "area": area, "wkb": wkb, "rep": (rep.x(), rep.y())})
    problems.summary()
    return out


def _burn_ids(polys, depth, thr, edges, progress_cb):
    """Per polygon: cells inside, wet cells, wet cells per depth class. One
    GDAL rasterisation per strip of the depth raster (ids of the polygons
    burned into an Int32 grid)."""
    n = len(polys)
    ncls = len(edges) + 1
    n_cells = np.zeros(n, dtype=np.int64)
    n_wet = np.zeros(n, dtype=np.int64)
    by_cls = np.zeros(n * ncls, dtype=np.int64)
    unread = 0
    ds, lyr = _new_ogr_layer()
    if lyr is None:
        raise RuntimeError("no OGR memory driver: polygons cannot be rasterised")
    feats = []
    for i, p in enumerate(polys):
        og = ogr.CreateGeometryFromWkb(p["wkb"])
        if og is None:
            continue
        if og.HasCurveGeometry():
            og = og.GetLinearGeometry()
        og.FlattenTo2D()
        f = ogr.Feature(lyr.GetLayerDefn())
        f.SetGeometry(og)
        f.SetField("pid", i + 1)
        lyr.CreateFeature(f)
        feats.append(f)                     # keep the features (and geometries) alive
    bbs = np.array([p["bbox"] for p in polys], dtype=np.float64).reshape(-1, 4)
    W, H = depth.xsize, depth.ysize
    rows_per = max(1, min(H, MAX_STRIP_CELLS // max(1, W)))
    for y0 in range(0, H, rows_per):
        h = min(rows_per, H - y0)
        if progress_cb:
            progress_cb(20.0 + 70.0 * y0 / max(1, H), "Population polygons on the depth grid")
        gt = depth._window_gt(0, y0)
        xs = [gt[0] + c * gt[1] + r * gt[2] for c in (0, W) for r in (0, h)]
        ys = [gt[3] + c * gt[4] + r * gt[5] for c in (0, W) for r in (0, h)]
        sx0, sx1, sy0, sy1 = min(xs), max(xs), min(ys), max(ys)
        if not np.any((bbs[:, 0] <= sx1) & (bbs[:, 2] >= sx0) & (bbs[:, 1] <= sy1)
                      & (bbs[:, 3] >= sy0)):
            continue
        arr = depth.window_values(0, y0, W, h)
        if arr is None:
            unread += 1
            continue
        try:
            mem = gdal.GetDriverByName("MEM").Create("", int(W), int(h), 1, gdal.GDT_Int32)
            mem.SetGeoTransform(gt)
            lyr.SetSpatialFilterRect(sx0, sy0, sx1, sy1)
            err = gdal.RasterizeLayer(mem, [1], lyr, options=["ATTRIBUTE=pid"])
            lyr.SetSpatialFilter(None)
            ids = mem.GetRasterBand(1).ReadAsArray()
            mem = None
        except Exception as e:
            raise RuntimeError("polygon rasterisation failed: {}".format(e))
        if err not in (0, None) or ids is None:
            raise RuntimeError("polygon rasterisation failed (GDAL error {})".format(err))
        inside = ids > 0
        if not inside.any():
            continue
        pid = ids[inside].astype(np.int64) - 1
        n_cells += np.bincount(pid, minlength=n)
        d = arr[inside]
        wet = np.isfinite(d) & (d > thr)
        if wet.any():
            pw = pid[wet]
            n_wet += np.bincount(pw, minlength=n)
            cls = np.digitize(d[wet], edges, right=True) if edges else np.zeros(pw.size, int)
            by_cls += np.bincount(pw * ncls + cls, minlength=n * ncls)
    return n_cells, n_wet, by_cls.reshape(n, ncls), unread


def _polygon_population(layer, depth, ctx, opts, progress_cb):
    result = _empty_result("polygons", layer.name())
    result.update({"period": opts["period"], "min_depth_ft": opts["min_depth_ft"],
                   "pop_field": opts["pop_field"], "day_field": opts["day_field"] or "",
                   "housing_field": opts["housing_field"] or "", "method": "polygon cells"})
    if layer.geometryType() != GEOM_POLYGON:
        result["error"] = "the population layer '{}' is not a polygon layer".format(layer.name())
        return result
    flds = layer.fields()
    idx = {"pop": _find_field(flds, opts["pop_field"]),
           "day": _find_field(flds, opts["day_field"]),
           "hu": _find_field(flds, opts["housing_field"]),
           "id": _find_field(flds, opts["id_field"])}
    if idx["pop"] < 0:
        result["error"] = "population field '{}' not found in '{}' (numeric fields: {})".format(
            opts["pop_field"], layer.name(), ", ".join(numeric_fields(layer)) or "none")
        return result
    for key, what in (("day", "day population"), ("hu", "housing units"), ("id", "id")):
        want = {"day": opts["day_field"], "hu": opts["housing_field"], "id": opts["id_field"]}[key]
        if want and idx[key] < 0:
            result["notes"].append("{} field '{}' not found - ignored".format(what, want))
    edges = list(opts["depth_classes_ft"])
    thr = float(opts["min_depth_ft"])
    polys = _read_polygons(layer, depth, ctx, idx, result, progress_cb)
    result["classes"] = depth_class_labels(edges)
    if not polys:
        result.update({"ok": True, "par": 0.0, "pop_in_model": 0.0, "pop_wet_polygons": 0.0,
                       "par_by_class": [0.0] * len(result["classes"])})
        if idx["day"] >= 0:
            result["par_day"] = 0.0
        if idx["hu"] >= 0:
            result["housing"] = 0.0
        result["notes"].append("no polygon of the layer touches the depth raster")
        return result
    n_cells, n_wet, by_cls, unread = _burn_ids(polys, depth, thr, edges, progress_cb)
    result["n_strips_unread"] = unread
    rext = raster_rect(depth)
    cell_a = abs(depth.gt[1] * depth.gt[5] - depth.gt[2] * depth.gt[4])
    tol = 1e-9 * max(1.0, abs(rext.xMaximum()), abs(rext.yMaximum()))
    share = np.zeros(len(polys))
    basis = [""] * len(polys)
    rep_x = np.array([p["rep"][0] for p in polys])
    rep_y = np.array([p["rep"][1] for p in polys])
    sub = np.nonzero(n_cells == 0)[0]
    sub_d = depth.values_at(rep_x[sub], rep_y[sub]) if sub.size else np.empty(0)
    sub_pos = {int(k): j for j, k in enumerate(sub)}
    for i, p in enumerate(polys):
        bx0, by0, bx1, by1 = p["bbox"]
        inside_r = (bx0 >= rext.xMinimum() - tol and bx1 <= rext.xMaximum() + tol
                    and by0 >= rext.yMinimum() - tol and by1 <= rext.yMaximum() + tol)
        if n_cells[i] > 0:
            s = n_wet[i] / float(n_cells[i])
            basis[i] = "cells in polygon"
            if not inside_r:
                result["n_partial"] += 1
                if p["area"] > 0:
                    s *= min(1.0, n_cells[i] * cell_a / p["area"])
                basis[i] = "cells in polygon; polygon reaches beyond the raster (scaled)"
            share[i] = s
        elif (bx1 < rext.xMinimum() or bx0 > rext.xMaximum() or by1 < rext.yMinimum()
              or by0 > rext.yMaximum()):
            result["n_outside"] += 1
            basis[i] = "outside the raster"
        else:
            d = sub_d[sub_pos[i]]
            result["n_subcell"] += 1
            share[i] = 1.0 if (np.isfinite(d) and d > thr) else 0.0
            basis[i] = "smaller than a cell: cell at its representative point"
    share = np.clip(share, 0.0, 1.0)
    pop = np.array([p["pop"] for p in polys])
    par_i = pop * share
    result["n_used"] = len(polys)
    result["n_wet"] = int(np.count_nonzero(share > 0))
    result["par"] = float(par_i.sum())
    result["pop_in_model"] = float(pop.sum())
    result["pop_wet_polygons"] = float(pop[share > 0].sum())
    if idx["day"] >= 0:
        day = np.array([p["day"] or 0.0 for p in polys])
        result["par_day"] = float((day * share).sum())
    if idx["hu"] >= 0:
        hu = np.array([p["hu"] or 0.0 for p in polys])
        result["housing"] = float((hu * share).sum())
    # split by depth class: the share of the polygon's flooded area in each class
    with np.errstate(invalid="ignore", divide="ignore"):
        frac_cls = np.where(n_wet[:, None] > 0, by_cls / np.maximum(n_wet, 1)[:, None], 0.0)
    result["par_by_class"] = [float(v) for v in (par_i[:, None] * frac_cls).sum(axis=0)]
    result["rows"] = [{
        "Poly_ID": p["label"], "Population": p["pop"],
        "Population_Day": p["day"] if idx["day"] >= 0 else None,
        "Housing_Units": p["hu"] if idx["hu"] >= 0 else None,
        "Cells_In_Polygon": int(n_cells[i]), "Wet_Cells": int(n_wet[i]),
        "Share_Flooded": round(float(share[i]), 4),
        "People_At_Risk": round(float(par_i[i]), 3),
        "People_At_Risk_Day": (round(float(p["day"] or 0.0) * float(share[i]), 3)
                               if idx["day"] >= 0 else None),
        "Basis": basis[i]} for i, p in enumerate(polys)]
    result["ok"] = True
    return result


# --------------------------------------------------------------------- raster --
def _people(values, units, area_ft2):
    """(people per cell array, valid mask, negative count) from raw cell values."""
    valid = np.isfinite(values)
    with np.errstate(invalid="ignore"):
        neg = valid & (values < 0)
        valid &= values >= 0
    people = np.where(valid, values, 0.0)
    if units != "per_cell":
        people = people * (area_ft2 / RASTER_UNITS[units][1])
    return people, valid, int(neg.sum())


def _fraction_counts(depth, pop, to_pop, win, thr, edges, progress_cb):
    """Wet depth cells per (population cell, depth class): sorted unique keys
    (row * width + col in the population window) x class -> counts."""
    c0, r0, ww, hh = win
    ncls = len(edges) + 1
    keys, cnts = [], []
    W, H = depth.xsize, depth.ysize
    rows_per = max(1, min(H, MAX_STRIP_CELLS // max(1, W)))
    for y0 in range(0, H, rows_per):
        h = min(rows_per, H - y0)
        if progress_cb:
            progress_cb(15.0 + 40.0 * y0 / max(1, H), "Depth grid on the population grid")
        arr = depth.window_values(0, y0, W, h)
        if arr is None:
            continue
        wet = np.isfinite(arr) & (arr > thr)
        if not wet.any():
            continue
        rr, cc = np.nonzero(wet)
        x, y = depth.cell_centres(cc, rr + y0)
        X, Y = to_pop(x, y)
        pcf, prf = _pixel_of(pop, X, Y)
        col, row = _to_int_index(pcf) - c0, _to_int_index(prf) - r0
        ok = (col >= 0) & (col < ww) & (row >= 0) & (row < hh)
        if not ok.any():
            continue
        cls = np.digitize(arr[wet][ok], edges, right=True) if edges else np.zeros(ok.sum(), int)
        k = (row[ok] * ww + col[ok]) * ncls + cls
        u, c = np.unique(k, return_counts=True)
        keys.append(u)
        cnts.append(c)
    if not keys:
        return np.empty(0, np.int64), np.empty(0, np.int64)
    allk, allc = np.concatenate(keys), np.concatenate(cnts)
    uk, inv = np.unique(allk, return_inverse=True)
    return uk, np.bincount(inv, weights=allc).astype(np.int64)


def _raster_population(pop_layer, depth, ctx, opts, lat, progress_cb):
    result = _empty_result("raster", pop_layer.name())
    result.update({"period": opts["period"], "min_depth_ft": opts["min_depth_ft"],
                   "pop_field": RASTER_UNITS[opts["raster_units"]][0]})
    pop = RasterSampler(pop_layer)
    if not pop.ok:
        result["error"] = "the population raster '{}' could not be read".format(pop_layer.name())
        return result
    try:
        units = opts["raster_units"]
        edges = list(opts["depth_classes_ft"])
        ncls = len(edges) + 1
        thr = float(opts["min_depth_ft"])
        result["classes"] = depth_class_labels(edges)
        drect = raster_rect(depth)
        to_pop = _Mapper(depth.crs, pop.crs, ctx, drect)
        bb = to_pop.image_bbox()
        if bb is None:
            result["error"] = "the depth raster's extent cannot be transformed into the " \
                              "population raster's CRS"
            return result
        pcs, prs = _pixel_of(pop, np.array([bb[0], bb[2], bb[0], bb[2]]),
                             np.array([bb[1], bb[1], bb[3], bb[3]]))
        c0 = max(0, int(math.floor(np.nanmin(pcs))) - 1)
        c1 = min(pop.xsize - 1, int(math.floor(np.nanmax(pcs))) + 1)
        r0 = max(0, int(math.floor(np.nanmin(prs))) - 1)
        r1 = min(pop.ysize - 1, int(math.floor(np.nanmax(prs))) + 1)
        if c0 > c1 or r0 > r1:
            result.update({"ok": True, "par": 0.0, "pop_in_model": 0.0,
                           "par_by_class": [0.0] * ncls})
            result["notes"].append("the population raster does not cover the depth raster")
            return result
        ww, hh = c1 - c0 + 1, r1 - r0 + 1
        # the mapping of population cell centres onto the depth grid: the
        # window's corners in the population CRS give the lattice's extent
        wx = [pop.gt[0] + c * pop.gt[1] + r * pop.gt[2]
              for c in (c0, c0 + ww) for r in (r0, r0 + hh)]
        wy = [pop.gt[3] + c * pop.gt[4] + r * pop.gt[5]
              for c in (c0, c0 + ww) for r in (r0, r0 + hh)]
        to_depth = _Mapper(pop.crs, depth.crs, ctx,
                           QgsRectangle(min(wx), min(wy), max(wx), max(wy)))
        # cell areas. The ratio of a population cell to a depth cell is measured
        # by mapping one population cell (mid-window) into the depth CRS, so it is
        # consistent with the coordinate mapping whatever the two CRSs are; the
        # ground area (ft2) of the depth cell then gives the population cell's.
        dep_area = cell_area_ft2(depth, lat)
        mc, mr = c0 + ww // 2, r0 + hh // 2
        corners = [(pop.gt[0] + (mc + i) * pop.gt[1] + (mr + j) * pop.gt[2],
                    pop.gt[3] + (mc + i) * pop.gt[4] + (mr + j) * pop.gt[5])
                   for i, j in ((0, 0), (1, 0), (0, 1))]
        mx, my = to_depth(np.array([p[0] for p in corners]), np.array([p[1] for p in corners]))
        area_units = abs((mx[1] - mx[0]) * (my[2] - my[0]) - (mx[2] - mx[0]) * (my[1] - my[0]))
        depth_units = abs(depth.gt[1] * depth.gt[5] - depth.gt[2] * depth.gt[4])
        ratio = float(area_units / depth_units) if depth_units > 0 and np.isfinite(area_units) \
            else float("nan")
        if not (math.isfinite(ratio) and ratio > 0):
            result["error"] = "the population raster's cell size cannot be related to the " \
                              "depth raster's"
            return result
        pop_area = ratio * dep_area
        method = opts["method"]
        if method == "auto":
            method = "fraction" if ratio >= AUTO_FRACTION_RATIO else "centre"
        result["method"] = method
        result["cell_ratio"] = ratio
        result["pop_cell_ft2"], result["depth_cell_ft2"] = pop_area, dep_area
        fu, fc = None, None
        if method == "fraction":
            fu, fc = _fraction_counts(depth, pop, to_pop, (c0, r0, ww, hh), thr, edges,
                                      progress_cb)
            fcls, fkey = fu % ncls, fu // ncls
        par = 0.0
        in_model = 0.0
        by_cls = np.zeros(ncls)
        n_wet_cells = 0
        rows_per = max(1, MAX_STRIP_CELLS // max(1, ww))
        for ra in range(r0, r1 + 1, rows_per):
            h = min(rows_per, r1 - ra + 1)
            if progress_cb:
                progress_cb(55.0 + 40.0 * (ra - r0) / max(1, hh), "Population raster")
            vals = pop.window_values(c0, ra, ww, h)
            if vals is None:
                result["n_strips_unread"] += 1
                continue
            people, valid, neg = _people(vals, units, pop_area)
            result["n_negative"] += neg
            m = valid & (people > 0)
            if not m.any():
                continue
            rr, cc = np.nonzero(m)
            x, y = pop.cell_centres(cc + c0, rr + ra)
            X, Y = to_depth(x, y)
            dcf, drf = _pixel_of(depth, X, Y)
            dc, dr = _to_int_index(dcf), _to_int_index(drf)
            inside = (dc >= 0) & (dc < depth.xsize) & (dr >= 0) & (dr < depth.ysize)
            p_here = people[m]
            in_model += float(p_here[inside].sum())
            if method == "centre":
                if not inside.any():
                    continue
                d = depth.values_at(X[inside], Y[inside])
                wet = np.isfinite(d) & (d > thr)
                if not wet.any():
                    continue
                pw = p_here[inside][wet]
                par += float(pw.sum())
                n_wet_cells += int(wet.sum())
                cls = np.digitize(d[wet], edges, right=True) if edges else np.zeros(pw.size, int)
                by_cls += np.bincount(cls, weights=pw, minlength=ncls)
            else:
                key = (rr + (ra - r0)) * ww + cc
                cnt = np.zeros((len(p_here), ncls))
                for j in range(ncls):
                    sel = fcls == j
                    if not sel.any():
                        continue
                    kj, cj = fkey[sel], fc[sel]
                    pos = np.minimum(np.searchsorted(kj, key), len(kj) - 1)
                    hit = kj[pos] == key
                    cnt[:, j] = np.where(hit, cj[pos], 0)
                frac = cnt * (dep_area / pop_area)
                tot = frac.sum(axis=1)
                scale = np.where(tot > 1.0, 1.0 / np.where(tot > 0, tot, 1.0), 1.0)
                frac = frac * scale[:, None]
                par += float((p_here * frac.sum(axis=1)).sum())
                by_cls += (p_here[:, None] * frac).sum(axis=0)
                n_wet_cells += int(np.count_nonzero(tot > 0))
        result["par"] = par
        result["pop_in_model"] = in_model
        result["par_by_class"] = [float(v) for v in by_cls]
        result["n_used"] = int(ww * hh)
        result["n_wet"] = n_wet_cells
        result["ok"] = True
        if opts.get("day_field"):
            result["notes"].append("a population raster has no day / night split; the day "
                                   "population field is ignored")
        return result
    finally:
        pop.close()


# ------------------------------------------------------------------ public API --
def estimate_population_at_risk(source, depth_sampler, transform_ctx, pop_field="",
                                day_field="", housing_field="", id_field="", period="night",
                                raster_units="per_cell", method="auto",
                                min_depth_ft=DEFAULT_MIN_DEPTH_FT,
                                depth_classes_ft=DEFAULT_DEPTH_CLASSES_FT, lat=None,
                                progress_cb=None):
    """Population in the flooded area from a polygon layer or a population
    raster. `source`: QgsVectorLayer (polygons; `pop_field` required) or
    QgsRasterLayer. `depth_sampler`: RasterSampler of the depth raster (ft).
    Returns a result dict (see _empty_result): "ok", "error", "par" (people at
    risk from the main field / raster), "par_day", "housing", "pop_in_model",
    "pop_wet_polygons", "par_by_class", "classes", "rows" (polygons), counters,
    "notes". Never raises for bad input (the reason is in "error")."""
    name = source.name() if source is not None else ""
    kind = "raster" if isinstance(source, QgsRasterLayer) else "polygons"
    result = _empty_result(kind, name)
    if source is None or not source.isValid():
        result["error"] = "no valid population layer"
        return result
    if depth_sampler is None or not getattr(depth_sampler, "ok", False):
        result["error"] = "the depth raster cannot be read"
        return result
    if raster_units not in RASTER_UNITS:
        result["error"] = "unknown raster units '{}'".format(raster_units)
        return result
    if method not in METHODS:
        result["error"] = "unknown method '{}'".format(method)
        return result
    opts = {"pop_field": pop_field, "day_field": day_field, "housing_field": housing_field,
            "id_field": id_field, "period": "day" if period == "day" else "night",
            "raster_units": raster_units, "method": method,
            "min_depth_ft": float(min_depth_ft),
            "depth_classes_ft": tuple(sorted(float(v) for v in depth_classes_ft))}
    try:
        if isinstance(source, QgsRasterLayer):
            if lat is None:
                lat = center_latitude(raster_rect(depth_sampler), depth_sampler.crs,
                                      transform_ctx)
            res = _raster_population(source, depth_sampler, transform_ctx, opts, lat,
                                     progress_cb)
        elif isinstance(source, QgsVectorLayer):
            res = _polygon_population(source, depth_sampler, transform_ctx, opts, progress_cb)
        else:
            result["error"] = "the population source is neither a vector nor a raster layer"
            return result
    except Exception as e:                       # noqa: BLE001 - a bad layer must not stop the run
        if type(e).__name__ == "CancelledByUser":
            raise
        result["error"] = "population estimate failed: {}".format(e)
        return result
    if res.get("ok") and res.get("n_strips_unread"):
        res["notes"].append("{} strip(s) of the depth raster could not be read - the estimate is "
                            "low".format(res["n_strips_unread"]))
    return res


def _fmt(v):
    return "{:,.0f}".format(v or 0.0)


def summary_lines(res):
    """Text lines describing the estimate (source, method, counts, depth-class
    split, data problems, caveats)."""
    if not res.get("ok"):
        return ["Second population source not evaluated: {}".format(res.get("error"))]
    per = res["period"]
    thr = res["min_depth_ft"]
    wet = "any wet cell" if thr <= 0 else "depth above {:g} ft".format(thr)
    lines = []
    if res["kind"] == "polygons":
        lines.append(
            "Second population source: layer '{}', field '{}' ({} population): {} polygon(s) "
            "touch the model area, {} have flooded cells ({}). Population in the flooded area "
            "(each polygon's population x its flooded share of cells, measured on the depth "
            "grid): {}; whole population of the polygons with flooding: {} of {} in all the "
            "polygons that touch the model area".format(
                res["layer"], res["pop_field"], per, res["n_used"], res["n_wet"], wet,
                _fmt(res["par"]), _fmt(res["pop_wet_polygons"]), _fmt(res["pop_in_model"])))
        if res.get("par_day") is not None:
            lines.append("  daytime population field '{}': {} in the flooded area".format(
                res["day_field"], _fmt(res["par_day"])))
        if res.get("housing") is not None:
            lines.append("  housing units field '{}': {} in the flooded area".format(
                res["housing_field"], _fmt(res["housing"])))
    else:
        how = {"centre": "each population cell looked up at its centre in the depth grid",
               "fraction": "each population cell weighted by its wet share of the depth "
                           "grid (population cells {:.1f}x the area of a depth cell)".format(
                               res.get("cell_ratio") or 0.0)}.get(
            res["method"], "no overlap with the depth grid")
        lines.append(
            "Second population source: raster '{}' (values are {}; {} population; {}): "
            "population in the flooded area ({}): {} of {} in the population cells within the "
            "model area".format(
                res["layer"], res["pop_field"], per, how, wet, _fmt(res["par"]),
                _fmt(res["pop_in_model"])))
    if res.get("classes") and res.get("par_by_class") and len(res["classes"]) > 1 \
            and (res["par"] or 0) > 0:
        lines.append("  by depth of the flooded cells: " + "; ".join(
            "{} {}".format(c, _fmt(v)) for c, v in zip(res["classes"], res["par_by_class"])))
    probs = []
    for key, txt in (("n_null", "NULL population"), ("n_negative", "negative population"),
                     ("n_text", "non-numeric population"),
                     ("n_bad_geometry", "unusable geometry"),
                     ("n_outside", "outside the depth raster"),
                     ("n_subcell", "smaller than a depth cell (all-or-nothing)"),
                     ("n_partial", "reach beyond the depth raster (scaled to the part inside)"),
                     ("n_repaired", "invalid geometry repaired")):
        if res.get(key):
            probs.append("{} {}".format(res[key], txt))
    if probs:
        lines.append("  data notes: " + "; ".join(probs) + " (NULL / negative / text counted as 0)")
    lines.extend("  " + n for n in res.get("notes", []))
    if res.get("par_day") is None and res["kind"] == "polygons":
        lines.append("  no daytime population field given: the day / night comparison is "
                     "not available")
    if res["kind"] == "raster":
        lines.append("  a population raster has one time of day only: day / night is not "
                     "available")
    lines.append(CAVEATS[0])
    return lines


def _source_verdict(src, plug, kind):
    """(ratio, text) like crosscheck.verdict (same ratio band, same rules), with
    wording for a population layer instead of DSS-WISE."""
    r, text = XC.verdict(src, plug)
    if r is None:
        if text.startswith("the plugin counts people where"):
            return None, "the plugin counts people where the population source counts none - " \
                         "check"
        return None, text
    if text == "consistent":
        return r, text
    if r > XC.RATIO_HIGH:
        return r, ("plugin higher ({:.1f}x) - check for structures the population source "
                   "shows few residents for (non-residential, campgrounds, seasonal housing), "
                   "or a persons-per-dwelling that is too high".format(r))
    return r, ("plugin lower ({:.1f}x of the population source) - check for missing structure "
               "data (NSI / address / outline layers), or a population source that spreads "
               "people over land where there are no mapped structures".format(r))


def compare(res, conseq, population="night"):
    """(lines, rows) comparing the estimate with the plugin's structure-based
    consequence totals (consequences.totals). Like with like: the main field /
    raster against the night population when it stands for night (else day), the
    day field against the day population. `population` (the run's setting) is
    only used to name the plugin quantity of a combined comparison."""
    lines, rows = [], []
    if not res.get("ok"):
        return ["Second population source not compared: {}".format(res.get("error"))], rows
    src_name = res["layer"]
    per = res["period"]
    pairs = [("population ({}) in the flooded area".format(per), res["par"], res["pop_field"],
              conseq.get("par_" + per), "population at risk, {}".format(per))]
    if res.get("par_day") is not None:
        pairs.append(("population (day) in the flooded area", res["par_day"], res["day_field"],
                      conseq.get("par_day"), "population at risk, day"))
    for qty, sval, sfield, pval, plabel in pairs:
        r, text = _source_verdict(sval, pval, res["kind"])
        rows.append({"Population_source": src_name, "Quantity": qty, "Source_value": sval,
                     "Source_field": sfield, "Plugin_value": pval, "Plugin_quantity": plabel,
                     "Ratio_plugin_to_source": None if r is None else round(r, 2),
                     "Verdict": text})
        lines.append("{} - {}: population source {} (field '{}') against the plugin's {} "
                     "({}): {}".format(src_name, qty, _fmt(sval), sfield, _fmt(pval), plabel,
                                       text))
    if res.get("classes") and (res.get("par") or 0) > 0:
        for c, v in zip(res["classes"], res["par_by_class"]):
            rows.append({"Population_source": src_name,
                         "Quantity": "population in the flooded area, depth {}".format(c),
                         "Source_value": round(v, 2), "Source_field": res["pop_field"],
                         "Plugin_value": None, "Plugin_quantity": "",
                         "Ratio_plugin_to_source": None, "Verdict": "split only"})
    return lines, rows


def is_off_ratio(row):
    """True when a comparison row is outside the consistent band (for a run-log
    note like the HCOM check's)."""
    r = row.get("Ratio_plugin_to_source")
    if r is None:
        return row.get("Verdict", "").startswith("the plugin counts people")
    return not XC.RATIO_LOW <= r <= XC.RATIO_HIGH


def write_cross_check_csv(folder, rows):
    """QA/Population_Source_CrossCheck.csv; returns the path."""
    path = prepare_output_path(os.path.join(folder, CROSSCHECK_CSV))
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CROSSCHECK_HEAD)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in CROSSCHECK_HEAD})
    return path


def polygon_csv_head(res):
    head = ["Poly_ID", "Population"]
    if res.get("day_field"):
        head.append("Population_Day")
    if res.get("housing_field"):
        head.append("Housing_Units")
    head += ["Cells_In_Polygon", "Wet_Cells", "Share_Flooded", "People_At_Risk"]
    if res.get("day_field"):
        head.append("People_At_Risk_Day")
    head.append("Basis")
    return head


def write_polygon_csv(folder, res):
    """QA/Population_Source_Polygons.csv (one row per polygon: id, population,
    share flooded, people at risk ...); None for a raster source."""
    if res.get("kind") != "polygons" or not res.get("rows"):
        return None
    head = polygon_csv_head(res)
    path = prepare_output_path(os.path.join(folder, POLYGON_CSV))
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=head)
        w.writeheader()
        for r in res["rows"]:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in head})
    return path


def run_check(source, depth_sampler, transform_ctx, conseq, out_folder=None,
              population="night", log=None, progress_cb=None, **opts):
    """Estimate, compare, write the CSVs. `opts` are the keyword arguments of
    estimate_population_at_risk (see options_from_cfg). Returns
    (lines, written_paths, result); lines are ready for the summary; never
    raises for bad input. `out_folder` is the QA folder (None: no files)."""
    if source is None:
        return [], [], None
    res = estimate_population_at_risk(source, depth_sampler, transform_ctx,
                                      progress_cb=progress_cb, **opts)
    lines = summary_lines(res)
    written = []
    if res.get("ok"):
        cmp_lines, rows = compare(res, conseq or {}, population)
        lines = lines[:-1] + cmp_lines + lines[-1:]     # caveat stays last
        if out_folder:
            try:
                os.makedirs(out_folder, exist_ok=True)
                written.append(write_cross_check_csv(out_folder, rows))
                p = write_polygon_csv(out_folder, res)
                if p:
                    written.append(p)
            except OSError as e:
                lines.append("Population source files not written: {}".format(e))
        bad = [r for r in rows if r["Verdict"] != "split only" and is_off_ratio(r)]
        if bad and log:
            log(">>> NOTE: the population at risk differs from the population layer '{}' in {} "
                "comparison(s) - see the summary / QA/{}".format(res["layer"], len(bad),
                                                                 CROSSCHECK_CSV))
    elif log:
        log(">>> WARNING: second population source: {}".format(res.get("error")))
    return lines, written, res
