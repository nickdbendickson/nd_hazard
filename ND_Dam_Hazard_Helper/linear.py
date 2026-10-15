# -*- coding: utf-8 -*-
"""Inundated roads (with the most recent AADT) and railroads (with trains
per day) for the hazard classification.

Lines are sampled every LINE_SAMPLE_STEP_FT; consecutive wet samples
(depth > 0) form an inundated stretch. ND REG_05.2026 5.2.1.2 treats a
road as impacted "if overtopped at any depth"; 5.2.1.3 does the same for
railroads. Bridges are flagged because a 2D depth grid shows water under
the deck.
"""

import math
import re

import numpy as np

from qgis.core import (
    QgsFeature, QgsField, QgsGeometry, QgsPointXY, QgsWkbTypes,
    QgsCoordinateTransform, QgsDistanceArea, QgsFeatureRequest,
)

from .compat import NULL, WKB_LINESTRING, FT_INT, FT_DOUBLE, FT_STRING, GEOM_POINT
from .constants import (
    CLASS_HIGH, CLASS_JUDGMENT, ZONE_LABELS, PROGRESS_INTERVAL,
    FEATURE_BATCH_SIZE, LINE_SAMPLE_STEP_FT, AADT_SEARCH_FT, CROSSING_AADT_SEARCH_FT,
    RAIL_CROSSING_SEARCH_MI, ND_AADT_LOW_BELOW, ND_AADT_HIGH_ABOVE, ND_RAIL_TRAINS_HIGH,
    ND_HIGH, ND_MEDIUM, ND_LOW, DEFAULT_VEHICLE_OCCUPANCY, DEFAULT_VEHICLE_SPEED_MPH,
    vehicle_curve_available,
)
from .helpers import (
    log_message, LimitedLogger, create_memory_layer, transform_geometry, transform_xy,
    buffer_distance_in_layer_units, classify_values, add_features_batched, is_null,
    curve_pair, finite_length_ft,
)

ROAD_INTERSTATE = "Interstate"
ROAD_US = "US Highway"
ROAD_STATE = "State Highway"
ROAD_PRIVATE = "Private"
ROAD_OTHER = "Local/County"

_RE_INTERSTATE = re.compile(r"\b(?:i|ih|interstate)\s*-?\s*\d+", re.I)
_RE_US = re.compile(r"\b(?:us|u\.\s?s\.?)\s*(?:hwy|highway|route|rte)?\s*-?\s*\d+", re.I)
_RE_STATE = re.compile(r"\b(?:nd|state)\s*(?:hwy|highway|route|rte)?\s*-?\s*\d+", re.I)
_RE_YEAR_COL = re.compile(r"^(?:aadt|adt|aadt_vn|cur_aadt)[_ ]?((?:19|20)\d{2})$"
                          r"|^((?:19|20)\d{2})[_ ]?(?:aadt|adt)$")
_UNPAVED = re.compile(r"\b(?:unpaved|gravel|fine gravel|dirt|earth|ground|graded|scoria|"
                      r"primitive|sand|grass|compacted|unimproved|clay|aggregate|track)\b")
_PAVED = re.compile(r"\b(?:paved|asphalt|concrete|bituminous|blacktop|chip seal|chipseal|"
                    r"sealed|hma|pcc|oil)\b")

_AADT_NAMES = ("aadt", "cur_aadt", "aadt_cur", "curr_aadt", "aadt_vn", "adt",
               "annualaveragedailytrafficcount", "aadt_total", "trafficvol", "traffic")
_AADT_YEAR_NAMES = ("aadt_year", "aadtyear", "aadt_yr", "year_record", "count_year",
                    "cnt_year", "countyear", "traffic_year", "annualaveragedailytrafficyear",
                    "data_year", "year", "yr")
_NAME_NAMES = ("name", "fullname", "full_name", "st_name", "road_name", "rd_name", "routename",
               "route_name", "streetname", "street", "label", "ref", "highwayname")
_CLASS_TEXT_HINTS = ("name", "ref", "route", "rte", "hwy", "highway", "sign", "class",
                     "label", "street", "shield")


def _low_index(fields):
    return {fields.at(i).name().lower(): i for i in range(fields.count())}


def _num(v):
    if is_null(v):
        return None
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


class AttrReader(object):
    """Per-layer field detection for AADT, road class, surface, bridge."""

    def __init__(self, layer):
        f = layer.fields()
        self.names = [f.at(i).name() for i in range(f.count())]
        self.low = _low_index(f)
        self.aadt = next((self.low[n] for n in _AADT_NAMES if n in self.low), None)
        self.year = next((self.low[n] for n in _AADT_YEAR_NAMES if n in self.low), None)
        self.year_cols = []
        for n, i in self.low.items():
            m = _RE_YEAR_COL.match(n)
            if m:
                self.year_cols.append((i, int(m.group(1) or m.group(2))))
        self.name = next((self.low[n] for n in _NAME_NAMES if n in self.low), None)
        self.class_text = [i for n, i in self.low.items()
                           if any(h in n for h in _CLASS_TEXT_HINTS)]
        self.name_like = [i for n, i in self.low.items()
                          if any(h in n for h in ("name", "ref", "route", "rte", "hwy",
                                                  "highway", "street", "sign", "label"))
                          and "class" not in n and "type" not in n]
        self.surface = [i for n, i in self.low.items()
                        if "surf" in n or "pave" in n or n in ("tracktype",)]
        self.bridge = [i for n, i in self.low.items()
                       if n in ("bridge", "structure_type", "struct_type", "structure",
                                "is_bridge", "bridge_id")]
        self.get = lambda attrs, n: attrs[self.low[n]] if n in self.low else None

    def aadt_candidates(self, attrs, label):
        out = []
        for i, yr in self.year_cols:
            v = _num(attrs[i])
            if v is not None and v > 0:
                out.append((v, yr, "{} ({} column)".format(label, self.names[i])))
        if self.aadt is not None:
            v = _num(attrs[self.aadt])
            if v is not None and v > 0:
                yr = _num(attrs[self.year]) if self.year is not None else None
                yr = int(yr) if yr and 1900 < yr < 2200 else None
                out.append((v, yr, label))
        return out

    def road_name(self, attrs):
        if self.name is None or is_null(attrs[self.name]):
            return ""
        return str(attrs[self.name])

    def name_text(self, attrs):
        """Every name / route text of a feature (name, ref, route, highway
        name ...), for comparing two layers' descriptions of a road."""
        parts = [self.road_name(attrs)]
        for i in self.name_like:
            if i != self.name and not is_null(attrs[i]):
                parts.append(str(attrs[i]))
        return " ".join(p for p in parts if p)

    def road_class(self, attrs):
        fs = _num(self.get(attrs, "f_system"))
        if fs is None:                  # ND GIS Hub NG9-1-1 roads: FUNC_SYS
            fs = _num(self.get(attrs, "func_sys"))
        rs = _num(self.get(attrs, "route_signing"))
        if rs is None:                  # HPMS feature service: routesigning
            rs = _num(self.get(attrs, "routesigning"))
        if fs == 1 or rs == 2:
            return ROAD_INTERSTATE, "HPMS F_SYSTEM/ROUTE_SIGNING"
        if rs == 3:
            return ROAD_US, "HPMS ROUTE_SIGNING"
        if rs == 4:
            return ROAD_STATE, "HPMS ROUTE_SIGNING"
        text = " ".join(str(attrs[i]) for i in self.class_text if not is_null(attrs[i]))
        if _RE_INTERSTATE.search(text):
            return ROAD_INTERSTATE, "route/name '{}'".format(_RE_INTERSTATE.search(text).group(0))
        if _RE_US.search(text):
            return ROAD_US, "route/name '{}'".format(_RE_US.search(text).group(0))
        if _RE_STATE.search(text):
            return ROAD_STATE, "route/name '{}'".format(_RE_STATE.search(text).group(0))
        hw = str(self.get(attrs, "highway") or "").lower()
        if hw in ("motorway", "motorway_link"):
            return ROAD_INTERSTATE, "OSM highway=motorway"
        if hw in ("trunk", "trunk_link", "primary", "primary_link"):
            return ROAD_STATE, "OSM highway={} (US/State)".format(hw)
        access = " ".join(str(self.get(attrs, n) or "") for n in ("access", "ownership", "owner"))
        if "private" in access.lower():
            return ROAD_PRIVATE, "access/ownership private"
        return ROAD_OTHER, "no highway designation found"

    def paved(self, attrs, road_class):
        for i in self.surface:
            v = attrs[i]
            if is_null(v):
                continue
            n = _num(v)
            if n is not None and self.names[i].lower() == "surface_type":
                return ("No" if int(n) == 1 else "Yes"), "HPMS SURFACE_TYPE {}".format(int(n))
            t = re.sub(r"[_\-]+", " ", str(v).lower())
            if _UNPAVED.search(t) or re.match(r"grade[2-5]", t):
                return "No", "{}={}".format(self.names[i], v)
            if _PAVED.search(t):
                return "Yes", "{}={}".format(self.names[i], v)
        if str(self.get(attrs, "highway") or "").lower() == "track":
            return "No", "OSM highway=track"
        if road_class in (ROAD_INTERSTATE, ROAD_US, ROAD_STATE):
            return "Yes", "assumed (highway)"
        return "Unknown", "no surface attribute"

    def is_bridge(self, attrs):
        for i in self.bridge:
            v = attrs[i]
            if is_null(v):
                continue
            t = str(v).strip().lower()
            if self.names[i].lower() in ("structure_type", "struct_type") and _num(v) == 1:
                return True
            if t in ("yes", "y", "true", "1", "viaduct", "bridge") or "bridge" in t:
                return True
        return False


# ------------------------------------------------------------------ sampling --
def line_parts(geom):
    """List of coordinate lists for each part of a (multi)line geometry."""
    try:
        if QgsWkbTypes.isCurvedType(geom.wkbType()):
            geom = QgsGeometry(geom.constGet().segmentize())
    except Exception:
        pass
    if geom.isMultipart():
        parts = geom.asMultiPolyline()
    else:
        parts = [geom.asPolyline()]
    return [[(p.x(), p.y()) for p in part] for part in parts if len(part) >= 2]


def densify(coords, step):
    pts = np.asarray(coords, dtype=np.float64)
    out = []
    for i in range(len(pts) - 1):
        p0, p1 = pts[i], pts[i + 1]
        seg = p1 - p0
        n = max(1, int(math.ceil(math.hypot(seg[0], seg[1]) / step)))
        t = np.arange(n, dtype=np.float64)[:, None] / n
        out.append(p0 + t * seg)
    out.append(pts[-1:])
    return np.vstack(out)


def wet_runs(depth):
    """(start, end) inclusive index pairs of consecutive samples with depth > 0."""
    wet = np.isfinite(depth) & (depth > 0)
    runs, start = [], None
    for i, w in enumerate(wet):
        if w and start is None:
            start = i
        elif not w and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, len(wet) - 1))
    return runs


class _Distance(object):
    def __init__(self, crs, ctx):
        self.crs, self.ctx = crs, ctx
        self.da = QgsDistanceArea()
        self.da.setSourceCrs(crs, ctx)
        try:
            self.da.setEllipsoid(crs.ellipsoidAcronym() or "EPSG:7019")
        except Exception:
            pass

    def length_ft(self, geom):
        v = finite_length_ft(self.da, geom, self.crs, self.ctx)     # v2.20.1: NaN fallback
        return v if v is not None else float("nan")


BATCH_POINTS = 2000000          # v2.6.2: line points per depth read (memory bound)
# ... and at most this many lines, so the stretches reach the road / rail
# analysis (and progress / Cancel are seen) every few hundred lines
BATCH_FEATURES = 500


def _stretches(pending, samplers, to_v, to_r, dist):
    """(feature, stretch) of every flooded stretch of the densified lines
    [(feature, points)], in their order: one depth read for all points and
    one velocity / arrival read for all wet stretches."""
    if not pending:
        return
    if len(pending) == 1:
        depths = [samplers["depth"].values_at(pending[0][1][:, 0], pending[0][1][:, 1])]
    else:
        d_all = samplers["depth"].values_at(np.concatenate([p[:, 0] for _f, p in pending]),
                                            np.concatenate([p[:, 1] for _f, p in pending]))
        depths = np.split(d_all, np.cumsum([len(p) for _f, p in pending])[:-1])
    runs = []                                   # (feature, points, depths, i0, i1)
    for (feat, pts), d in zip(pending, depths):
        for i0, i1 in wet_runs(d):
            runs.append((feat, pts, d, i0, i1))
    if not runs:
        return
    wx = np.concatenate([pts[i0:i1 + 1, 0] for _f, pts, _d, i0, i1 in runs])
    wy = np.concatenate([pts[i0:i1 + 1, 1] for _f, pts, _d, i0, i1 in runs])
    vx, vy = transform_xy(wx, wy, to_v)
    v_all = samplers["vel"].values_at(vx, vy)
    v_all = np.where(np.isnan(v_all), 0.0, v_all)
    ax, ay = transform_xy(wx, wy, to_r)
    a_all = samplers["arr"].values_at(ax, ay)
    off = 0
    for feat, pts, d, i0, i1 in runs:
        k = i1 - i0 + 1
        vs, a = v_all[off:off + k], a_all[off:off + k]
        off += k
        j0, j1 = i0, i1
        if j1 == j0:                      # single wet sample -> short segment
            j0, j1 = max(0, i0 - 1), min(len(pts) - 1, i1 + 1)
        seg = pts[j0:j1 + 1]
        sg = QgsGeometry.fromPolylineXY([QgsPointXY(x, y) for x, y in seg])
        ds = d[i0:i1 + 1]
        try:
            length_ft = max(LINE_SAMPLE_STEP_FT, dist.length_ft(sg))
        except Exception:
            length_ft = LINE_SAMPLE_STEP_FT * (i1 - i0 + 1)
        yield feat, {
            "geom": sg, "length_ft": length_ft, "depth": ds, "vel": vs,
            "xs": pts[i0:i1 + 1, 0].copy(), "ys": pts[i0:i1 + 1, 1].copy(),
            "max_depth": float(np.nanmax(ds)), "max_vel": float(vs.max()),
            "dv_max": float(np.nanmax(ds * vs)),
            "arrival": float(np.nanmin(a)) if np.isfinite(a).any() else None,
        }


def inundated_stretches(layer, samplers, analysis_crs, transform_ctx, filter_rect=None,
                        progress_cb=None, label="line"):
    """Yield (feature, stretch dict) for every flooded stretch of every line.
    Stretch: geom (analysis CRS), length_ft, max_depth, max_vel, arrival,
    depth/vel sample arrays."""
    step = buffer_distance_in_layer_units(LINE_SAMPLE_STEP_FT, analysis_crs)
    to_a = QgsCoordinateTransform(layer.crs(), analysis_crs, transform_ctx)
    # velocity / arrival are optional: a sampler without a raster has no CRS
    to_v = QgsCoordinateTransform(analysis_crs, samplers["vel"].crs, transform_ctx) \
        if getattr(samplers["vel"], "crs", None) is not None else None
    to_r = QgsCoordinateTransform(analysis_crs, samplers["arr"].crs, transform_ctx) \
        if getattr(samplers["arr"], "crs", None) is not None else None
    dist = _Distance(analysis_crs, transform_ctx)
    request = QgsFeatureRequest()
    if filter_rect is not None:
        try:
            back = QgsCoordinateTransform(analysis_crs, layer.crs(), transform_ctx)
            request.setFilterRect(filter_rect if back.isShortCircuited()
                                  else back.transformBoundingBox(filter_rect))
        except Exception:
            pass
    total = max(1, layer.featureCount())
    problems = LimitedLogger("{} sampling".format(label))
    # v2.6.2: the lines are densified first and the depth of many of them is
    # read in one call; velocity / arrival once for all their wet stretches -
    # the same cells and values as one read per line, in the same order. The
    # QGIS-provider reader keeps one read per line (its big windows are slow).
    batch = getattr(samplers["depth"], "backend", None) == "gdal"
    pending, npts, nfeat = [], 0, 0
    for n, feat in enumerate(layer.getFeatures(request)):
        if progress_cb and n % PROGRESS_INTERVAL == 0:
            progress_cb(min(99.0, 100.0 * n / total), "Sampling {}s: {} of {}".format(
                label, n, total))
        g = transform_geometry(feat.geometry(), to_a)
        if g is None or g.isNull() or g.isEmpty():
            continue
        try:
            parts = line_parts(g)
        except Exception as e:
            problems.log("feature {}: {}".format(feat.id(), e))
            continue
        for coords in parts:
            pts = densify(coords, step)
            pending.append((feat, pts))
            npts += len(pts)
        nfeat += 1
        if not batch or npts >= BATCH_POINTS or nfeat >= BATCH_FEATURES:
            for item in _stretches(pending, samplers, to_v, to_r, dist):
                yield item
            pending, npts, nfeat = [], 0, 0
    for item in _stretches(pending, samplers, to_v, to_r, dist):
        yield item
    problems.summary()


# --------------------------------------------------------------- neighbours --
class FeatureIndex(object):
    """Features of a layer in the analysis CRS with a grid index, for
    'within distance' lookups (AADT stations/segments, crossings)."""

    def __init__(self, layer, analysis_crs, transform_ctx, cell_ft):
        self.layer = layer
        self.reader = AttrReader(layer)
        self.cell = buffer_distance_in_layer_units(cell_ft, analysis_crs)
        self.items = []
        self.cells = {}
        xf = QgsCoordinateTransform(layer.crs(), analysis_crs, transform_ctx)
        for f in layer.getFeatures():
            g = transform_geometry(f.geometry(), xf)
            if g is None or g.isNull() or g.isEmpty():
                continue
            idx = len(self.items)
            self.items.append((f, g))
            bb = g.boundingBox()
            for i in range(int(math.floor(bb.xMinimum() / self.cell)),
                           int(math.floor(bb.xMaximum() / self.cell)) + 1):
                for j in range(int(math.floor(bb.yMinimum() / self.cell)),
                               int(math.floor(bb.yMaximum() / self.cell)) + 1):
                    self.cells.setdefault((i, j), []).append(idx)

    def within(self, geom, dist):
        bb = geom.boundingBox()
        seen = set()
        out = []
        for i in range(int(math.floor((bb.xMinimum() - dist) / self.cell)),
                       int(math.floor((bb.xMaximum() + dist) / self.cell)) + 1):
            for j in range(int(math.floor((bb.yMinimum() - dist) / self.cell)),
                           int(math.floor((bb.yMaximum() + dist) / self.cell)) + 1):
                for idx in self.cells.get((i, j), ()):
                    if idx in seen:
                        continue
                    seen.add(idx)
                    f, g = self.items[idx]
                    dd = g.distance(geom)
                    if dd <= dist:
                        out.append((dd, f, g))
        out.sort(key=lambda t: t[0])
        return out


def _zone_from_raster(sampler, xs, ys):
    """Most severe ACER class of a zone raster at the sample points (None if
    no zone raster or no valid class there)."""
    if sampler is None or not sampler.ok:
        return None
    vals = sampler.values_at(xs, ys)
    vals = vals[np.isfinite(vals) & (vals >= CLASS_HIGH) & (vals <= 3)]
    return int(vals.min()) if vals.size else None


AADT_CORRIDOR_FT = 50.0       # an AADT line belongs to a road it runs along ...
AADT_MIN_ALONG = 0.5          # ... for at least half of the flooded stretch
AADT_POINT_FT = 100.0         # AADT count stations: the nearest one this close
_NAME_STOP = {"st", "street", "rd", "road", "ave", "avenue", "hwy", "highway", "co", "county",
              "cr", "n", "s", "e", "w", "north", "south", "east", "west", "dr", "drive", "ln",
              "lane", "blvd", "the", "of", "and", "ave.", "st.", "rd.", "trl", "trail", "route",
              "rte", "sr", "us", "nd", "state", "twp", "township", "unnamed"}


def _name_tokens(text):
    toks = set(re.findall(r"[a-z0-9]+", str(text or "").lower()))
    return {t for t in toks if t not in _NAME_STOP}


def names_conflict(a, b):
    """True only when the names clearly describe different roads: route
    numbers on both sides that differ ("County Road 5" vs "US Hwy 2"), or
    street names on both sides with no word in common ("Main St" vs "Oak
    Ave"). A street name against a route number ("Main Street" vs "US 2" -
    a highway through town) is not a conflict; unknown names never are."""
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb or (ta & tb):
        return False
    na = {t for t in ta if t.isdigit()}
    nb = {t for t in tb if t.isdigit()}
    if na and nb:
        return True                         # both carry route numbers, none shared
    wa, wb = ta - na, tb - nb
    return bool(wa and wb and not na and not nb)


def along_fraction(stretch, other, corridor):
    """Share of the flooded stretch that lies within `corridor` of `other`
    (1.0 for a stretch shorter than the corridor that is that close)."""
    try:
        length = stretch.length()
        if length <= corridor:
            return 1.0 if stretch.distance(other) <= corridor else 0.0
        inter = stretch.intersection(other.buffer(corridor, 4))
        return 0.0 if inter is None or inter.isNull() else inter.length() / length
    except Exception:
        return 0.0


AADT_MAX_ANGLE_DEG = 30.0     # ... and in the same direction (not a crossing street)


def _local_direction(xs, ys, px, py):
    """Unit direction of the polyline (xs, ys) at its vertex pair nearest
    to (px, py); None if degenerate."""
    xs, ys = np.asarray(xs, dtype=np.float64), np.asarray(ys, dtype=np.float64)
    if xs.size < 2:
        return None
    x0, y0, x1, y1 = xs[:-1], ys[:-1], xs[1:], ys[1:]
    dx, dy = x1 - x0, y1 - y0
    l2 = dx * dx + dy * dy
    t = np.clip(np.where(l2 > 0, ((px - x0) * dx + (py - y0) * dy) / np.where(l2 > 0, l2, 1),
                         0.0), 0.0, 1.0)
    d = np.hypot(x0 + t * dx - px, y0 + t * dy - py)
    k = int(np.argmin(np.where(l2 > 0, d, np.inf)))
    n = math.hypot(dx[k], dy[k])
    return None if n <= 0 else (dx[k] / n, dy[k] / n)


def same_direction(st, geom, max_deg=AADT_MAX_ANGLE_DEG):
    """True when the candidate line runs within `max_deg` of the flooded
    stretch's direction at the stretch's middle (either way along it)."""
    try:
        xs, ys = st["xs"], st["ys"]
        m = len(xs) // 2
        lo, hi = max(0, m - 3), min(len(xs), m + 4)
        a = _local_direction(xs[lo:hi], ys[lo:hi], xs[m], ys[m])
        if a is None:
            return True                       # a single sample: no direction to test
        best = None
        for part in line_parts(geom):
            px = [p[0] for p in part]
            py = [p[1] for p in part]
            b = _local_direction(px, py, xs[m], ys[m])
            if b is not None:
                c = abs(a[0] * b[0] + a[1] * b[1])
                best = c if best is None else max(best, c)
        return best is None or best >= math.cos(math.radians(max_deg))
    except Exception:
        return True


def best_aadt(candidates):
    """Most recent AADT: newest year wins; ties -> own attribute before
    nearby layers, then the higher AADT (conservative)."""
    if not candidates:
        return None, None, None
    best = max(candidates, key=lambda c: (c[1] or 0, c[3], c[0]))
    return best[0], best[1], best[2]


def nd_road_class(road_class, aadt):
    """ND REG_05.2026 5.2.1.2 for an overtopped road. Returns
    (class, basis, needs_judgment)."""
    if road_class == ROAD_INTERSTATE:
        return ND_HIGH, "Interstate - probable loss of life", False
    if aadt is not None and aadt > ND_AADT_HIGH_ABOVE:
        return ND_HIGH, "AADT {:,.0f} > {:,} - probable loss of life".format(
            aadt, ND_AADT_HIGH_ABOVE), False
    if road_class in (ROAD_US, ROAD_STATE):
        if aadt is None:
            return ND_MEDIUM, "{} - transportation disruption; no AADT found - High if AADT " \
                "> {:,} (verify)".format(road_class, ND_AADT_HIGH_ABOVE), True
        return ND_MEDIUM, "{} - transportation disruption".format(road_class), False
    if aadt is not None and aadt >= ND_AADT_LOW_BELOW:
        return ND_MEDIUM, "AADT {:,.0f} (500-2,000) - transportation disruption".format(
            aadt), False
    if road_class == ROAD_PRIVATE:
        return ND_LOW, "private road", False
    if aadt is not None:
        return ND_LOW, "AADT {:,.0f} < 500 - no probable loss of life".format(aadt), False
    return ND_LOW, "no AADT found - verify traffic", True


def motorists(aadt, length_ft, speed_mph, occupancy):
    """Persons in vehicles on a flooded road stretch: the hourly traffic
    (AADT / 24) that is on the stretch at once (length / speed), times the
    persons per vehicle. 2.32.1: one formula for the run (analyze_roads) and
    the sensitivity table (sensitivity._persons), which kept a copy."""
    vehicles = aadt / 24.0 * (length_ft / 5280.0) / max(speed_mph, 1.0)
    return vehicles * occupancy


def analyze_roads(road_layer, samplers, analysis_crs, transform_ctx, filter_rect=None,
                  aadt_layers=(), crossing_index=None, occupancy=DEFAULT_VEHICLE_OCCUPANCY,
                  speed_mph=DEFAULT_VEHICLE_SPEED_MPH, progress_cb=None):
    """Flooded road stretches with road class, paving, AADT (most recent
    of: the road's own attributes, AADT layers within AADT_SEARCH_FT, FRA
    crossings within CROSSING_AADT_SEARCH_FT), ACER vehicle zone and ND class."""
    reader = AttrReader(road_layer)
    aadt_idx = [FeatureIndex(lyr, analysis_crs, transform_ctx, max(AADT_SEARCH_FT, 500.0))
                for lyr in aadt_layers if lyr is not None and lyr.isValid()]
    lat = _lat_of(samplers, analysis_crs, transform_ctx)
    search = _ft(AADT_SEARCH_FT, analysis_crs, lat)
    corridor = _ft(AADT_CORRIDOR_FT, analysis_crs, lat)
    point_near = _ft(AADT_POINT_FT, analysis_crs, lat)
    xsearch = _ft(CROSSING_AADT_SEARCH_FT, analysis_crs, lat)
    veh = vehicle_curve_available()
    curves = curve_pair("vehicle") if veh else None
    vmax = curves[2] if veh else None
    results = []
    for feat, st in inundated_stretches(road_layer, samplers, analysis_crs, transform_ctx,
                                        filter_rect, progress_cb, "road"):
        attrs = feat.attributes()
        rclass, cbasis = reader.road_class(attrs)
        paved, pbasis = reader.paved(attrs, rclass)
        rname = reader.road_name(attrs)
        rnames = reader.name_text(attrs)
        cands = [c + (2,) for c in reader.aadt_candidates(attrs, "road layer")]
        # AADT of THIS road only: a line must run along the stretch (not a
        # crossing street or a parallel highway nearby); a count station must
        # be close to it; names, when both known, must agree
        for idx in aadt_idx:
            for dd, f, g in idx.within(st["geom"], search):
                fa = f.attributes()
                if names_conflict(rnames, idx.reader.name_text(fa)):
                    continue
                if g.type() == GEOM_POINT:
                    if dd > point_near:
                        continue
                elif along_fraction(st["geom"], g, corridor) < AADT_MIN_ALONG or \
                        not same_direction(st, g):
                    continue
                cands.extend(c + (1,) for c in idx.reader.aadt_candidates(
                    fa, "'{}'".format(idx.layer.name())))
        if crossing_index is not None:
            for _dd, f, _g in crossing_index.within(st["geom"], xsearch)[:1]:
                fa = f.attributes()
                xname = " ".join(str(v) for v in (
                    crossing_index.reader.get(fa, "street"),
                    crossing_index.reader.get(fa, "highwayname")) if v and not is_null(v))
                if names_conflict(rnames, xname):
                    continue
                cid = crossing_index.reader.get(fa, "crossingid")
                cands.extend(c + (0,) for c in crossing_index.reader.aadt_candidates(
                    fa, "FRA crossing {}".format(cid or "")))
        aadt, year, src = best_aadt(cands)
        nd_cls, nd_basis, judge = nd_road_class(rclass, aadt)
        vzone = None
        if veh:
            vzone = _zone_from_raster(samplers.get("zone_vehicle"), st["xs"], st["ys"])
            if vzone is None:
                vzone = int(classify_values(st["depth"], st["vel"], *curves).min())
        persons = None
        if veh and aadt and paved != "No" and vzone in (CLASS_HIGH, CLASS_JUDGMENT):
            persons = motorists(aadt, st["length_ft"], speed_mph, occupancy)
        results.append({
            "fid": feat.id(), "name": rname, "road_class": rclass,
            "class_basis": cbasis, "paved": paved, "paved_basis": pbasis,
            "bridge": reader.is_bridge(attrs), "aadt": aadt, "aadt_year": year,
            "aadt_source": src, "nd_class": nd_cls, "nd_basis": nd_basis,
            "nd_judgment": judge, "vehicle_zone": vzone, "persons": persons,
            "beyond_chart": bool(veh and vmax is not None and st["max_vel"] > vmax), **st})
    log_message("Roads: {} inundated stretches".format(len(results)))
    return results


# --------------------------------------------------------------------- rail --
_OWNER_NAMES = ("rrowner1", "railroad", "railroadcode", "rr", "owner", "operator")
_PASS_NAMES = ("passngr", "passenger", "amtrak")


def trains_per_day(reader, attrs):
    """Daily trains at a crossing (FRA Form 71 / NTAD field names)."""
    def g(*names):
        return next((_num(reader.get(attrs, n)) for n in names
                     if n in reader.low and _num(reader.get(attrs, n)) is not None), None)
    total = g("totaltrn", "totaltrains", "total_trains")
    if total is None:
        day = g("totaldaylightthrutrains", "daythru")
        night = g("totalnighttimethrutrains", "nghtthru")
        if day is None and night is None:
            return None
        total = (day or 0) + (night or 0)
    sw = g("totalswt", "totalswitchingtrains")
    return total + (sw or 0)


def nd_rail_class(passenger, trains, confirmed=False):
    """confirmed: `trains` was set by the engineer (a 0 is then a real 0)."""
    if passenger:
        return ND_HIGH, "active passenger line - probable loss of life", False
    if trains is not None and trains >= ND_RAIL_TRAINS_HIGH:
        return ND_HIGH, "{:.0f} trains/day >= {} - probable loss of life".format(
            trains, ND_RAIL_TRAINS_HIGH), False
    # A stored 0 is usually a blank FRA field, not a counted timetable.
    # Any inundated line is still Medium (5.2.1.3); it is not a confirmed zero.
    if trains is None or (trains <= 0 and not confirmed):
        return ND_MEDIUM, "railroad impacted; trains/day unknown - verify with railroad", True
    return ND_MEDIUM, "railroad impacted ({:.0f} trains/day)".format(trains), False


# NARN "NET" codes of lines that carry no trains (abandoned, removed, trail)
# and of out-of-service lines (kept, as a judgment item)
NARN_NO_TRAFFIC = ("A", "R", "T")
NARN_OUT_OF_SERVICE = ("X",)
RAIL_ON_LINE_FT = 100.0


def _norm_sub(text):
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower()).replace("subdivision", "") \
        .replace("sub", "")


def analyze_rail(rail_layer, samplers, analysis_crs, transform_ctx, filter_rect=None,
                 crossing_index=None, progress_cb=None):
    reader = AttrReader(rail_layer)
    owner_i = next((reader.low[n] for n in _OWNER_NAMES if n in reader.low), None)
    pass_i = next((reader.low[n] for n in _PASS_NAMES if n in reader.low), None)
    sub_i = next((reader.low[n] for n in ("subdiv", "subdivision", "branch", "name")
                  if n in reader.low), None)
    net_i = reader.low.get("net")
    lat = _lat_of(samplers, analysis_crs, transform_ctx)
    search = _ft(RAIL_CROSSING_SEARCH_MI * 5280.0, analysis_crs, lat)
    on_line = _ft(RAIL_ON_LINE_FT, analysis_crs, lat)
    to_a = QgsCoordinateTransform(rail_layer.crs(), analysis_crs, transform_ctx)
    results = []
    skipped = 0
    for feat, st in inundated_stretches(rail_layer, samplers, analysis_crs, transform_ctx,
                                        filter_rect, progress_cb, "rail line"):
        attrs = feat.attributes()
        net = "" if net_i is None or is_null(attrs[net_i]) else str(attrs[net_i]).strip().upper()
        if net in NARN_NO_TRAFFIC:
            skipped += 1                    # abandoned / removed / trail: not a railroad
            continue
        owner = "" if owner_i is None or is_null(attrs[owner_i]) else str(attrs[owner_i])
        pval = "" if pass_i is None or is_null(attrs[pass_i]) else str(attrs[pass_i]).strip()
        passenger = pval.upper() not in ("", "N", "NO", "0", "FALSE", "NONE")
        sub = "" if sub_i is None or is_null(attrs[sub_i]) else str(attrs[sub_i])
        trains, tsrc = None, "no FRA crossing found within {:g} mi".format(RAIL_CROSSING_SEARCH_MI)
        unsure = False
        if crossing_index is not None:
            line_geom = transform_geometry(feat.geometry(), to_a)
            same, other = [], []
            for _dd, f, g in crossing_index.within(st["geom"], search):
                ca = f.attributes()
                code = str(crossing_index.reader.get(ca, "railroadcode")
                           or crossing_index.reader.get(ca, "railroad") or "")
                if owner and code and code.strip().upper() != owner.strip().upper():
                    continue
                t = trains_per_day(crossing_index.reader, ca)
                if t is None:
                    continue
                cid = crossing_index.reader.get(ca, "crossingid") or \
                    crossing_index.reader.get(ca, "crossing") or f.id()
                xsub = crossing_index.reader.get(ca, "railroadsubdivision")
                on_this = line_geom is not None and g.distance(line_geom) <= on_line
                if on_this or (sub and xsub and _norm_sub(sub) == _norm_sub(xsub)):
                    same.append((t, cid))
                elif sub and xsub:
                    continue                # another subdivision of the railroad
                else:
                    other.append((t, cid))
            used = same[:5] or other[:5]
            if used:
                trains = max(t for t, _c in used)
                tsrc = "FRA crossings " + ", ".join(str(c) for _t, c in used[:3])
                if not same:
                    unsure = True
                    tsrc += " (same railroad nearby; line / subdivision not confirmed)"
        nd_cls, nd_basis, judge = nd_rail_class(passenger, trains)
        if unsure and not judge:
            judge, nd_basis = True, nd_basis + " - crossing on this line not confirmed"
        if net in NARN_OUT_OF_SERVICE:
            judge, nd_basis = True, "out-of-service line (NARN NET = X) - " + nd_basis
        results.append({
            "fid": feat.id(), "railroad": owner, "subdivision": sub,
            "passenger": passenger, "passenger_value": pval, "trains": trains,
            "trains_source": tsrc, "nd_class": nd_cls, "nd_basis": nd_basis,
            "nd_judgment": judge, **st})
    log_message("Rail: {} inundated stretches{}".format(
        len(results), "; {} on abandoned / removed lines or trails left out".format(skipped)
        if skipped else ""))
    return results


def _lat_of(samplers, crs, ctx):
    """Latitude of the depth raster's centre (for Web Mercator / degrees)."""
    try:
        from .extent import center_latitude
        lyr = samplers["depth"].layer
        return center_latitude(lyr.extent(), crs, ctx) if lyr is not None else None
    except Exception:
        return None


def _ft(ft, crs, lat):
    """Feet in CRS units, corrected for Web Mercator / degrees when `lat`
    is known (the larger of the x / y distances)."""
    if lat is None:
        return buffer_distance_in_layer_units(ft, crs)
    from .extent import distance_xy_in_crs
    return max(distance_xy_in_crs(ft, crs, lat))


# ------------------------------------------------------------------ output --
ROAD_FIELDS = [
    ("Road_Name", FT_STRING), ("Road_Class", FT_STRING),
    ("Class_Basis", FT_STRING), ("Paved", FT_STRING),
    ("Paved_Basis", FT_STRING), ("Bridge", FT_STRING),
    ("Length_ft", FT_DOUBLE), ("Max_Depth_ft", FT_DOUBLE),
    ("Max_Vel_fps", FT_DOUBLE), ("First_Arrival_hr", FT_DOUBLE),
    ("AADT", FT_DOUBLE), ("AADT_Year", FT_INT), ("AADT_Source", FT_STRING),
    ("Vehicle_Zone", FT_INT), ("Vehicle_Zone_Label", FT_STRING),
    ("Persons_At_Risk", FT_DOUBLE), ("ND_Class", FT_STRING),
    ("ND_Basis", FT_STRING), ("Source_FID", FT_INT), ("Notes", FT_STRING),
]
RAIL_FIELDS = [
    ("Railroad", FT_STRING), ("Subdivision", FT_STRING),
    ("Passenger", FT_STRING), ("Length_ft", FT_DOUBLE),
    ("Max_Depth_ft", FT_DOUBLE), ("Max_Vel_fps", FT_DOUBLE),
    ("First_Arrival_hr", FT_DOUBLE), ("Trains_Per_Day", FT_DOUBLE),
    ("Trains_Source", FT_STRING), ("ND_Class", FT_STRING),
    ("ND_Basis", FT_STRING), ("Source_FID", FT_INT), ("Notes", FT_STRING),
]


def _nz(v):
    return NULL if v is None else v


def roads_to_layer(results, crs):
    lyr = create_memory_layer("Roads_Inundated", WKB_LINESTRING, crs,
                              [QgsField(n, t) for n, t in ROAD_FIELDS])
    fields = lyr.fields()
    feats = []
    for r in results:
        notes = []
        if r["bridge"]:
            notes.append("bridge - verify the deck is actually overtopped")
        if r["nd_judgment"]:
            notes.append("no AADT found - check NDDOT counts")
        if r["vehicle_zone"] is None:
            notes.append("ACER vehicle curve not configured")
        if r.get("beyond_chart"):
            notes.append("velocity above TM 11 Fig 4 range (16 ft/s) - evaluated at 16 ft/s")
        f = QgsFeature(fields)
        f.setGeometry(r["geom"])
        f.setAttributes([
            r["name"], r["road_class"], r["class_basis"], r["paved"], r["paved_basis"],
            "Yes" if r["bridge"] else "No", r["length_ft"], r["max_depth"], r["max_vel"],
            _nz(r["arrival"]), _nz(r["aadt"]), _nz(r["aadt_year"]), _nz(r["aadt_source"]),
            _nz(r["vehicle_zone"]), ZONE_LABELS.get(r["vehicle_zone"], "") or NULL,
            _nz(r["persons"]), r["nd_class"], r["nd_basis"], int(r["fid"]), "; ".join(notes)])
        feats.append(f)
    add_features_batched(lyr.dataProvider(), feats, FEATURE_BATCH_SIZE)
    lyr.updateExtents()
    return lyr


def rail_to_layer(results, crs):
    lyr = create_memory_layer("Rail_Inundated", WKB_LINESTRING, crs,
                              [QgsField(n, t) for n, t in RAIL_FIELDS])
    fields = lyr.fields()
    feats = []
    for r in results:
        notes = ["verify bridges/culverts - grid depth may be below the track"]
        if r["nd_judgment"]:
            notes.append("trains/day unknown")
        f = QgsFeature(fields)
        f.setGeometry(r["geom"])
        f.setAttributes([
            r["railroad"], r["subdivision"],
            ("Yes ({})".format(r["passenger_value"]) if r["passenger"] else "No"),
            r["length_ft"], r["max_depth"], r["max_vel"], _nz(r["arrival"]), _nz(r["trains"]),
            r["trains_source"], r["nd_class"], r["nd_basis"], int(r["fid"]), "; ".join(notes)])
        feats.append(f)
    add_features_batched(lyr.dataProvider(), feats, FEATURE_BATCH_SIZE)
    lyr.updateExtents()
    return lyr
