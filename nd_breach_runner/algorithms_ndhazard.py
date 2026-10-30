"""Hazard classification tool of the provider (id 'ndhazardclass'): ND Dam Safety Standards
REG_05.2026 Sections 5.2 / 5.3 with the ACER TM 11 depth-velocity charts, applied to the
rasters in a dam folder. The rules are in core/nd_hazard.py (shared with nd_dam_screening);
this module only reads the layers, samples the rasters and writes the results.

Import it through `algorithms` (which the provider does), like the other tool modules."""
import csv
import json
import math
import os
import re

import numpy as np
from qgis.core import (QgsCoordinateTransform, QgsProcessingException, QgsProcessingOutputFile,
                       QgsProcessingOutputString, QgsProcessingParameterBoolean,
                       QgsProcessingParameterEnum,
                       QgsProcessingParameterFeatureSource)

from . import qgis_io as qio
from .algorithms_consequences import ConsequencesAlgorithm, _attr, _txt
from .algorithms_tools import _ToolBase
from .compat import SOURCE_LINE, SOURCE_POINT, SOURCE_POLYGON, set_advanced
from .core import consequences as cons
from .core import nd_hazard as nh

OUTPUT_NAMES = {"markdown": "Hazard Classification (ND 5.2).md",
                "json": "Hazard Classification (ND 5.2).json",
                "csv": "Hazard Classification Findings.csv",
                "geojson": "Hazard Classification Findings.geojson",
                "svg_house": "ND Fig 5.1 Houses.svg",
                "svg_mobile": "ND Fig 5.2 Mobile Homes.svg"}

# ND 5.2.1.1 with 5.3: every structure is habitable unless its occupancy names an uninhabited
# building (nd_hazard.UNINHABITED_KEYWORDS, editable in the tool).
UNINHABITED_DEFAULT = ", ".join(nh.UNINHABITED_KEYWORDS)
UNKNOWN_WORDS = {"", "unknown", "unclassified", "unk", "none", "null", "n/a", "na", "other",
                 "-", "?"}
PASSENGER_DEFAULT = "A, B, C"

STRUCT_SYNONYMS = {
    "S_ID": ("id", "structure_id", "struct_id", "fd_id", "build_id", "bldg_id", "uuid",
             "objectid", "fid", "name"),
    "S_HABITABLE": ("habitable", "inhabited", "is_habitable", "occupied", "is_occupied"),
    "S_OCC": ("occtype", "occ_cls", "prim_occ", "occupancy", "occ_type", "occ", "st_damcat",
              "bldg_use", "use"),
    "S_MOBILE": ("mobile", "mobile_home", "is_mobile", "manufactured", "trailer", "prim_occ"),
}
ROAD_SYNONYMS = {
    "R_ID": ("name", "road_name", "fullname", "rte_name", "route_name", "route", "id",
             "objectid"),
    "R_AADT": ("aadt", "adt", "aadt_2024", "aadt_2023", "aadt_2022", "aadt_vn", "traffic",
               "vehicles_per_day"),
    "R_CLASS": ("rttyp", "route_type", "rte_type", "road_class", "class", "fclass",
                "func_class", "functional_class", "route_sign", "route", "highway",
                "fullname", "name"),
    "R_PRIVATE": ("private", "is_private"),      # never ownership / access codes
    "R_SOLE": ("sole_access", "sole_acc", "only_access", "sole"),
}
RAIL_SYNONYMS = {
    "T_ID": ("name", "subdiv", "rrowner1", "fraarcid", "id", "objectid"),
    "T_TRAINS": ("trains_per_day", "trains_day", "trainsperday", "daily_trains", "trains",
                 "train_count", "tpd"),
    "T_PASSENGER": ("passngr", "passenger", "is_passenger", "amtrak"),
}
LIFELINE_SYNONYMS = {
    "L_ID": ("name", "fac_name", "facility", "id", "objectid"),
    "L_KIND": ("kind", "type", "facility_type", "fac_type", "category", "naics_desc", "use"),
}
DAM_SYNONYMS = {
    "D_ID": ("dam_id", "nidid", "nid_id", "state_id", "id", "dam_name", "name"),
    "D_CLASS": ("hazard_class", "hazard", "haz_class", "hazard_potential", "nid_hazard",
                "class"),
}
NID_CLASS = {"h": "High", "s": "Significant", "l": "Low", "m": "Medium"}

_YES = {"y", "yes", "true", "t", "1", "x", "on"}
_NO = {"n", "no", "false", "f", "0", "off"}

SAMPLING = ("Points: the raster cell under the point. Polygons: the deepest cell whose centre "
            "falls inside the footprint (the cell under the centroid when none does), with the "
            "maximum velocity over the footprint's wet cells, which is conservative (the "
            "velocity at the deepest cell is kept as well). Lines: sampled every half cell, the "
            "deepest sample. Only features inside the rasters are read.")


# ------------------------------------------------------------------------------ helpers
def _yesno(v):
    """True / False from a boolean-ish value, None when it says neither."""
    if v is None or (hasattr(v, "isNull") and v.isNull()):
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return None if v != v else v != 0
    t = str(v).strip().lower()
    if t in _YES:
        return True
    if t in _NO:
        return False
    try:
        x = float(t)
    except ValueError:
        return None
    return None if x != x else x != 0


def habitable_from(flag_value, occ_text, uninhabited=nh.UNINHABITED_KEYWORDS):
    """(habitable, how): a habitable field that says yes or no (or 'uninhabited' /
    'unoccupied'), else the occupancy text (habitable unless it names an uninhabited building:
    nd_hazard.habitable_from_text, shared with the screening plugin), else habitable."""
    yn = _yesno(flag_value)
    if yn is None and flag_value is not None:
        t = _txt(flag_value).lower()
        if re.search(r"uninhab|unoccup|not habitable", t):
            yn = False
        elif re.search(r"habitable|inhabited|occupied", t):
            yn = True
    if yn is not None:
        return yn, "field"
    occ = (occ_text or "").strip()
    if occ.lower() in UNKNOWN_WORDS:
        return True, "unknown"
    return nh.habitable_from_text(occ, uninhabited), "class"


def is_mobile(flag_value, occ_text):
    if _yesno(flag_value):
        return True
    return any(nh.mobile_from_text(t) for t in (_txt(flag_value), occ_text or "") if t)


def is_interstate(class_text, name_text=""):
    return nh.interstate_from_text(class_text) or nh.interstate_from_text(name_text)


def is_private(v):
    """Only an explicit private flag: yes/true/1 or text containing 'private'."""
    return nh.yes_no(v) is True or "private" in _txt(v).lower()


def is_passenger(v, codes):
    toks = [t for t in re.split(r"[,;/\s]+", _txt(v)) if t] or [_txt(v)]
    return any(nh.passenger_from_value(t, tuple(codes)) for t in toks)


def dam_class(v):
    return nh.class_from_text(_txt(v)) or _txt(v)


def _inside_ring(px, py, ring):
    """Even-odd test of many points against one ring (vectorised over the points)."""
    x, y = ring[:, 0], ring[:, 1]
    x2, y2 = np.roll(x, -1), np.roll(y, -1)
    inside = np.zeros(px.shape, dtype=bool)
    for i in range(len(x)):
        if y[i] == y2[i]:
            continue
        cond = (y[i] > py) != (y2[i] > py)
        xi = x[i] + (py - y[i]) * (x2[i] - x[i]) / (y2[i] - y[i])
        inside ^= cond & (px < xi)
    return inside


class Sampler:
    """Maximum depth (ft) and velocity (ft/s) at points, polygons and lines on the dam
    folder's grid. Depth and velocity are arrays on `grid`; NaN / no-data count as dry."""

    def __init__(self, grid, depth, velocity=None, wet_ft=0.1):
        self.g = grid
        d = np.asarray(depth, dtype=float)
        bad = ~np.isfinite(d) | (d == grid.nodata) | (d < 0)
        self.d = np.where(bad, 0.0, d)
        if velocity is None:
            self.v = np.zeros_like(self.d)
        else:
            v = np.asarray(velocity, dtype=float)
            # no-data velocity stays unknown (NaN): nd_hazard counts it potentially lethal
            self.v = np.where(~np.isfinite(v) | (v == grid.nodata) | (v < 0), np.nan, v)
        self.wet = float(wet_ft)

    def _rc(self, x, y):
        c = np.floor((np.asarray(x, dtype=float) - self.g.x0) / self.g.dx).astype(int)
        r = np.floor((self.g.y0 - np.asarray(y, dtype=float)) / self.g.dx).astype(int)
        ok = (r >= 0) & (r < self.g.ny) & (c >= 0) & (c < self.g.nx)
        return r, c, ok

    def points(self, pts):
        """{depth_ft, vel_fts, vel_at_max_fts, x, y, cells, how} of the deepest of the
        points, or None when none is on the grid."""
        pts = np.asarray(pts, dtype=float).reshape(-1, 2)
        r, c, ok = self._rc(pts[:, 0], pts[:, 1])
        if not ok.any():
            return None
        r, c, pts = r[ok], c[ok], pts[ok]
        d = self.d[r, c]
        k = int(np.argmax(d))
        v = float(self.v[r[k], c[k]])
        v = v if math.isfinite(v) else None
        return {"depth_ft": float(d[k]), "vel_fts": v, "vel_at_max_fts": v,
                "x": float(pts[k, 0]), "y": float(pts[k, 1]), "cells": 1, "how": "point"}

    def polygon(self, rings, centroid):
        """Deepest cell whose centre is inside any of the outer rings; velocity is the
        maximum over the footprint's wet cells (conservative)."""
        g = self.g
        rr, cc = [], []
        for ring in rings:
            ring = np.asarray(ring, dtype=float)
            if len(ring) < 3:
                continue
            x0, y0 = ring.min(axis=0)
            x1, y1 = ring.max(axis=0)
            c0 = max(0, int(math.floor((x0 - g.x0) / g.dx - 0.5)))
            c1 = min(g.nx - 1, int(math.ceil((x1 - g.x0) / g.dx - 0.5)))
            r0 = max(0, int(math.floor((g.y0 - y1) / g.dx - 0.5)))
            r1 = min(g.ny - 1, int(math.ceil((g.y0 - y0) / g.dx - 0.5)))
            if c1 < c0 or r1 < r0:
                continue
            R, C = np.meshgrid(np.arange(r0, r1 + 1), np.arange(c0, c1 + 1), indexing="ij")
            R, C = R.ravel(), C.ravel()
            px = g.x0 + (C + 0.5) * g.dx
            py = g.y0 - (R + 0.5) * g.dx
            m = _inside_ring(px, py, ring)
            rr.append(R[m])
            cc.append(C[m])
        cells = sorted(set(zip(np.concatenate(rr).tolist(), np.concatenate(cc).tolist()))) \
            if rr else []
        if not cells:
            s = self.points([centroid])
            if s is not None:
                s["how"] = "polygon smaller than a cell: the cell under the centroid"
            return s
        r = np.array([a for a, _ in cells])
        c = np.array([b for _, b in cells])
        d = self.d[r, c]
        v = self.v[r, c]
        k = int(np.argmax(d))
        wet = (d >= self.wet) & np.isfinite(v)
        vk = float(v[k]) if np.isfinite(v[k]) else None
        vmax = float(v[wet].max()) if wet.any() else vk
        if d[k] > 0:
            x, y = g.xy(int(r[k]), int(c[k]))
        else:
            x, y = float(centroid[0]), float(centroid[1])
        return {"depth_ft": float(d[k]), "vel_fts": vmax,
                "vel_at_max_fts": vk, "x": float(x), "y": float(y),
                "cells": len(cells), "how": "polygon: deepest of {} cell(s)".format(len(cells))}

    def lines(self, parts):
        """Deepest sample along the parts, sampled every half cell."""
        step = 0.5 * self.g.dx
        pts = []
        for part in parts:
            part = np.asarray(part, dtype=float)
            for a, b in zip(part[:-1], part[1:]):
                n = max(1, int(math.ceil(float(np.hypot(*(b - a))) / step)))
                t = np.arange(n + 1)[:, None] / float(n)
                pts.append(a + t * (b - a))
        if not pts:
            return None
        s = self.points(np.vstack(pts))
        if s is not None:
            s["how"] = "line: deepest sample"
        return s


def _geom_kind(g):
    try:
        t = g.type()
        t = int(getattr(t, "value", t))
    except Exception:
        return ""
    return {0: "point", 1: "line", 2: "polygon"}.get(t, "")


def _parts(g, to_r):
    """(kind, parts, centroid) in the raster CRS: points [(x, y)], lines [array], polygons
    [outer ring array] (interior rings are ignored)."""
    tf = lambda p: (lambda q: (q.x(), q.y()))(to_r.transform(p))
    kind = _geom_kind(g)
    multi = g.isMultipart()
    if kind == "polygon":
        polys = g.asMultiPolygon() if multi else [g.asPolygon()]
        parts = [np.asarray([tf(p) for p in rings[0]], dtype=float) for rings in polys
                 if rings and len(rings[0]) >= 3]
        return kind, parts, tf(g.centroid().asPoint())
    if kind == "line":
        lines = g.asMultiPolyline() if multi else [g.asPolyline()]
        return kind, [np.asarray([tf(p) for p in ln], dtype=float) for ln in lines
                      if len(ln) >= 2], None
    pts = g.asMultiPoint() if multi else [g.asPoint()]
    return "point", [tf(p) for p in pts], None


def _layer_name(src, parameters, key):
    try:
        n = src.sourceName()
        if n:
            return str(n)
    except Exception:
        pass
    v = parameters.get(key)
    if isinstance(v, str) and v:
        return os.path.basename(v)
    return key.lower()


def _dam_id(folder):
    try:
        with open(os.path.join(folder, "_breach_model", "run_report.json")) as f:
            rid = (json.load(f).get("dam") or {}).get("dam_id")
        if rid:
            return str(rid)
    except (OSError, ValueError, AttributeError):
        pass
    return os.path.basename(os.path.normpath(folder)) if folder else ""


def not_digitized_message(cs):
    return ("The ACER TM 11 depth-velocity charts (ND Fig. 5.1 houses, Fig. 5.2 mobile homes) "
            "are not signed off in {} (status '{}', digitized by '{}', signed off by "
            "'{}'), so the chart method cannot run. These are 2D model results: until the "
            "charts are signed off (ND Dam Hazard > Chart sign-off; one engineer is enough), "
            "use the lethality method '{}' (it over-classifies), or give a signed-off chart "
            "file. The 2 ft depth rule is only allowed for a 1D model (5.2.1.1).".format(
                cs.path or "the chart file", cs.status, cs.digitized_by or "-",
                getattr(cs, "signer", "") or "-", nh.LETHALITY_LABELS[nh.LETHALITY_ALL_WET]))


# ------------------------------------------------------------------------------ algorithm
class NDHazardAlgorithm(_ToolBase):
    _raster_crs = staticmethod(ConsequencesAlgorithm._raster_crs)

    def name(self):
        return "ndhazardclass"

    def displayName(self):
        return "ND hazard classification (Section 5.2, ACER TM 11 charts)"

    def shortHelpString(self):
        return ("Classifies the dam Low / Medium / High from what its breach flood reaches, "
                "following the ND Dam Safety Standards Policy REG_05.2026 Sections 5.2 and 5.3 "
                "as core/nd_hazard.py applies them (the same module as the ND dam screening "
                "plugin). Reads 'Depth (Max).tif' (ft) and 'Velocity (Max).tif' (ft/s) from the "
                "dam folder.\n\n"
                "Habitable structures (5.2.1.1; unknown or blank occupancy counts as habitable): "
                "2D model: potentially lethal by the ACER TM 11 depth-velocity charts (ND Fig. "
                "5.1 houses, 5.2 mobile homes) -> High; a habitable structure in the judgment zone "
                "is Medium by rule (the old option 'judgment zone counts as potentially lethal' "
                "makes it High, 5.3). 1D model: deeper than 2 ft -> High. Inundated "
                "at any depth -> at least Medium (economic loss). Roads (5.2.1.2): AADT > 2,000 "
                "or interstate -> High, 500-2,000 -> Medium, private or < 500 -> Low, sole "
                "access to an occupied location -> Medium. Railroads (5.2.1.3): Medium, 10 or "
                "more trains a day or passenger -> High. Lifeline facilities (5.2.2) inundated "
                "-> Medium (access roads are not traced). A downstream dam the flood reaches "
                "(depth at its point at least the wet depth) passes its class on (5.3). 'Any "
                "depth' means the wet depth (default 0.1 ft). Missing AADT or train counts are "
                "data gaps, classified by the assumption you choose.\n\n"
                "READ FIRST. The chart method needs signed-off charts "
                "(core/acer_tm11_charts.json or your own chart file); the file shipped is signed "
                "off by the engineer named in it (one engineer is enough; ND Dam Hazard > Chart "
                "sign-off writes and installs another). The result is "
                "a screening classification for the engineer; the official classification is "
                "made by the ND Department of Water Resources.\n\n" + SAMPLING + "\n\n"
                "Writes 'Hazard Classification (ND 5.2).md' and '.json', 'Hazard Classification "
                "Findings.csv' and '.geojson', and with the chart method 'ND Fig 5.1 Houses.svg' "
                "and 'ND Fig 5.2 Mobile Homes.svg' into the dam folder.")

    def initAlgorithm(self, config=None):
        self._file("DAM_FOLDER", "Dam folder with 'Depth (Max).tif' and 'Velocity (Max).tif'",
                   folder=True, optional=False)
        self.addParameter(QgsProcessingParameterFeatureSource(
            "STRUCTURES", "Structures (points, or polygons: deepest cell inside)",
            [SOURCE_POINT, SOURCE_POLYGON], optional=True))
        for key, desc in (("S_ID", "identifier field"),
                          ("S_HABITABLE", "habitable field (yes/no, true/false, 1/0; blank = "
                                          "use the occupancy class)"),
                          ("S_OCC", "occupancy class field (NSI OCCTYPE, USA Structures "
                                    "OCC_CLS / PRIM_OCC ...)"),
                          ("S_MOBILE", "mobile home field (yes/no, or text containing "
                                       "manufactured / mobile / trailer)")):
            self._field(key, "Structures: " + desc, "STRUCTURES", numeric=False, advanced=True)
        self._text("UNINHABITED_WORDS", "Structures: occupancy words that mark an UNINHABITED "
                   "building (ND 5.2.1.1; every other structure, and a blank or unknown class, "
                   "is habitable - 5.3)", UNINHABITED_DEFAULT, advanced=True)
        self._layer("ROADS", "Roads (lines)", SOURCE_LINE)
        self._field("R_ID", "Roads: identifier / name field", "ROADS", numeric=False,
                    advanced=True)
        self._field("R_AADT", "Roads: AADT field (vehicles per day)", "ROADS", advanced=True)
        self._field("R_CLASS", "Roads: class / route field (interstate when it contains "
                    "'interstate' or starts 'I-' or 'I ' and a number)", "ROADS", numeric=False,
                    advanced=True)
        self._field("R_PRIVATE", "Roads: private road field (yes/no, or text 'private')",
                    "ROADS", numeric=False, advanced=True)
        self._field("R_SOLE", "Roads: sole access to an occupied location field (yes/no)",
                    "ROADS", numeric=False, advanced=True)
        self._layer("RAIL", "Railroads (lines)", SOURCE_LINE)
        self._field("T_ID", "Railroads: identifier field", "RAIL", numeric=False, advanced=True)
        self._field("T_TRAINS", "Railroads: trains a day field", "RAIL", advanced=True)
        self._field("T_PASSENGER", "Railroads: passenger field (yes/no, or a code below)",
                    "RAIL", numeric=False, advanced=True)
        self._text("PASSENGER_CODES", "Railroads: passenger field values meaning an active "
                   "passenger line (FRA PASSNGR codes), comma separated", PASSENGER_DEFAULT,
                   advanced=True)
        self.addParameter(QgsProcessingParameterFeatureSource(
            "LIFELINES", "Lifeline facilities: utilities, emergency response (points or "
            "polygons)", [SOURCE_POINT, SOURCE_POLYGON], optional=True))
        self._field("L_ID", "Lifelines: identifier field", "LIFELINES", numeric=False,
                    advanced=True)
        self._field("L_KIND", "Lifelines: kind of facility field", "LIFELINES", numeric=False,
                    advanced=True)
        self._layer("DOWNSTREAM_DAMS", "Downstream dams (points; affected when the flood depth "
                    "at the point is at least the wet depth)", SOURCE_POINT)
        self._field("D_ID", "Downstream dams: identifier field", "DOWNSTREAM_DAMS",
                    numeric=False, advanced=True)
        self._field("D_CLASS", "Downstream dams: hazard class field (Low / Significant / "
                    "Medium / High, or NID L / S / H)", "DOWNSTREAM_DAMS", numeric=False,
                    advanced=True)
        self.addParameter(QgsProcessingParameterEnum(
            "LETHALITY", "Potentially lethal to occupants of habitable structures",
            [nh.LETHALITY_LABELS[m] for m in nh.LETHALITY_METHODS],
            defaultValue=nh.LETHALITY_METHODS.index(nh.LETHALITY_CHARTS)))
        d = nh.Settings()
        self._num("WET_FT", "'Any depth': inundated or overtopped from a depth of (ft; at "
                  "least {:g}: a dry cell reads 0; the starting value {:g} is the engineer's "
                  "allowance for model noise, a screening value you choose)".format(
                      nh.MIN_WET_FT, d.wet_ft), d.wet_ft, minimum=nh.MIN_WET_FT)
        self.addParameter(set_advanced(QgsProcessingParameterEnum(
            "UNKNOWN_AADT", "Class of an overtopped road with no AADT (a data gap)",
            list(nh.CLASSES), defaultValue=nh.CLASSES.index(d.unknown_aadt))))
        self.addParameter(set_advanced(QgsProcessingParameterEnum(
            "UNKNOWN_TRAINS", "Class of an overtopped railroad with no trains a day (a data "
                              "gap; at least Medium)",
            list(nh.CLASSES), defaultValue=nh.CLASSES.index(d.unknown_trains))))
        self.addParameter(set_advanced(QgsProcessingParameterBoolean(
            "JUDGMENT_HIGH", "Old option: a habitable structure in the ACER judgment zone counts "
                             "as potentially lethal (High) - off: Medium by rule",
            defaultValue=d.judgment_zone_high)))
        self._file("CHART_FILE", "Digitized ACER TM 11 chart file (.json; empty = the one "
                   "shipped with the plugin)", ext="json", advanced=True)
        self.addOutput(QgsProcessingOutputString("CLASS", "Hazard class"))
        self.addOutput(QgsProcessingOutputFile("REPORT", OUTPUT_NAMES["markdown"]))
        self.addOutput(QgsProcessingOutputFile("JSON", OUTPUT_NAMES["json"]))
        self.addOutput(QgsProcessingOutputFile("CSV", OUTPUT_NAMES["csv"]))
        self.addOutput(QgsProcessingOutputFile("GEOJSON", OUTPUT_NAMES["geojson"]))

    # ------------------------------------------------------------------------------
    def _settings(self, parameters, context):
        e = lambda n: self.parameterAsEnum(parameters, n, context)
        try:
            return nh.Settings(
                lethality=nh.LETHALITY_METHODS[e("LETHALITY")],
                wet_ft=self.parameterAsDouble(parameters, "WET_FT", context),
                unknown_aadt=nh.CLASSES[e("UNKNOWN_AADT")],
                unknown_trains=nh.CLASSES[e("UNKNOWN_TRAINS")],
                judgment_zone_high=self.parameterAsBool(parameters, "JUDGMENT_HIGH", context)
            ).validate()
        except (ValueError, IndexError) as ex:
            raise QgsProcessingException(str(ex))

    def _charts(self, parameters, context):
        path = self._fileparam(parameters, "CHART_FILE", context)
        try:
            cs = nh.load_charts(path or None)
        except (OSError, ValueError) as ex:
            raise QgsProcessingException("Could not read the chart file {}: {}".format(
                path or nh.CHART_FILE, ex))
        if not cs.ready:
            raise QgsProcessingException(not_digitized_message(cs))
        return cs

    def _fields(self, parameters, context, names, synonyms):
        """{key: field name}: the chosen field, else a name match on the synonyms, else ''."""
        low = {n.lower(): n for n in names}
        out = {}
        for k in synonyms:
            chosen = self._str(parameters, k, context)
            if not chosen:
                chosen = next((low[s] for s in synonyms[k] if s in low), "")
            out[k] = chosen
        return out

    def _source(self, parameters, context, key, synonyms, grid, crs, feedback, label):
        """(features in the box, fields, to-raster transform, layer name), or None."""
        src = self.parameterAsSource(parameters, key, context)
        if src is None:
            return None
        fld = self._fields(parameters, context, qio.field_names(src), synonyms)
        used = ["{} = {}".format(k[2:].lower(), v) for k, v in fld.items() if v]
        feedback.pushInfo("{} fields: {}".format(label, ", ".join(used) or "none matched"))
        to_r = QgsCoordinateTransform(src.sourceCrs(), crs, context.transformContext())
        to_l = QgsCoordinateTransform(crs, src.sourceCrs(), context.transformContext())
        xmin, ymin, xmax, ymax = grid.bounds
        center = (0.5 * (xmin + xmax), 0.5 * (ymin + ymax))
        radius = 0.5 * float(np.hypot(xmax - xmin, ymax - ymin))
        return (qio._features(src, center, radius, to_l), fld, to_r,
                _layer_name(src, parameters, key))

    @staticmethod
    def _sample(sampler, g, to_r):
        if g is None or g.isEmpty():
            return None
        kind, parts, centroid = _parts(g, to_r)
        if not parts:
            return None
        if kind == "polygon":
            return sampler.polygon(parts, centroid)
        if kind == "line":
            return sampler.lines(parts)
        return sampler.points(parts)

    def _read_all(self, parameters, context, grid, crs, sampler, feedback, own_id):
        rec = {"structures": [], "roads": [], "railroads": [], "lifelines": [], "dams": []}
        extra, inputs, fields, outside = {}, {}, {}, {}
        # structures
        got = self._source(parameters, context, "STRUCTURES", STRUCT_SYNONYMS, grid, crs,
                           feedback, "Structures")
        if got:
            feats, fld, to_r, lname = got
            inputs["structures"], fields["structures"] = lname, fld
            words = tuple(w.strip() for w in re.split(r"[,;\n]+", self._str(
                parameters, "UNINHABITED_WORDS", context) or UNINHABITED_DEFAULT) if w.strip())
            how_n, out_n = {}, 0
            for f in feats:
                s = self._sample(sampler, f.geometry(), to_r)
                if s is None:
                    out_n += 1
                    continue
                ident = _txt(_attr(f, fld["S_ID"])) or str(f.id())
                occ = _txt(_attr(f, fld["S_OCC"]))
                prim = _txt(_attr(f, fld["S_MOBILE"])) if fld["S_MOBILE"] != fld["S_OCC"] else ""
                # uninhabited when either occupancy text names it (same as the screening plugin)
                hab, how = habitable_from(_attr(f, fld["S_HABITABLE"]),
                                          " | ".join(t for t in (occ, prim) if t), words)
                how_n[how] = how_n.get(how, 0) + 1
                mob = is_mobile(_attr(f, fld["S_MOBILE"]), occ)
                rec["structures"].append(nh.Structure(
                    ident, s["depth_ft"], s["vel_fts"], habitable=hab, mobile=mob, x=s["x"],
                    y=s["y"], note=s["how"]))
                extra[("structure", ident, s["x"], s["y"])] = {
                    "vel_at_max_depth_fts": None if s["vel_at_max_fts"] is None else round(
                        s["vel_at_max_fts"], 2), "habitable": hab,
                    "habitable_from": how, "mobile": mob, "occupancy": occ,
                    "sampled": s["how"]}
            outside["structures"] = out_n
            feedback.pushInfo("Structures: {} inside the rasters; habitable from the field {}, "
                              "from the occupancy class {}, unknown (taken as habitable) {}"
                              .format(len(rec["structures"]), how_n.get("field", 0),
                                      how_n.get("class", 0), how_n.get("unknown", 0)))
        # roads
        got = self._source(parameters, context, "ROADS", ROAD_SYNONYMS, grid, crs, feedback,
                           "Roads")
        if got:
            feats, fld, to_r, lname = got
            inputs["roads"], fields["roads"] = lname, fld
            out_n = 0
            for f in feats:
                s = self._sample(sampler, f.geometry(), to_r)
                if s is None:
                    out_n += 1
                    continue
                name = _txt(_attr(f, fld["R_ID"]))
                rec["roads"].append(nh.Road(
                    name or "road {}".format(f.id()), s["depth_ft"],
                    aadt=qio.num(_attr(f, fld["R_AADT"])),
                    interstate=is_interstate(_txt(_attr(f, fld["R_CLASS"])), name),
                    private=is_private(_attr(f, fld["R_PRIVATE"])),
                    sole_access=bool(_yesno(_attr(f, fld["R_SOLE"]))), x=s["x"], y=s["y"]))
            outside["roads"] = out_n
        # railroads
        got = self._source(parameters, context, "RAIL", RAIL_SYNONYMS, grid, crs, feedback,
                           "Railroads")
        if got:
            feats, fld, to_r, lname = got
            inputs["railroads"], fields["railroads"] = lname, fld
            codes = {t.strip().upper() for t in re.split(r"[,;\s]+", self._str(
                parameters, "PASSENGER_CODES", context) or PASSENGER_DEFAULT) if t.strip()}
            out_n = 0
            for f in feats:
                s = self._sample(sampler, f.geometry(), to_r)
                if s is None:
                    out_n += 1
                    continue
                rec["railroads"].append(nh.Railroad(
                    _txt(_attr(f, fld["T_ID"])) or "railroad {}".format(f.id()), s["depth_ft"],
                    trains_per_day=qio.num(_attr(f, fld["T_TRAINS"])),
                    passenger=is_passenger(_attr(f, fld["T_PASSENGER"]), codes) if
                    fld["T_PASSENGER"] else False, x=s["x"], y=s["y"]))
            outside["railroads"] = out_n
        # lifelines
        got = self._source(parameters, context, "LIFELINES", LIFELINE_SYNONYMS, grid, crs,
                           feedback, "Lifelines")
        if got:
            feats, fld, to_r, lname = got
            inputs["lifelines"], fields["lifelines"] = lname, fld
            out_n = 0
            for f in feats:
                s = self._sample(sampler, f.geometry(), to_r)
                if s is None:
                    out_n += 1
                    continue
                rec["lifelines"].append(nh.Lifeline(
                    _txt(_attr(f, fld["L_ID"])) or "lifeline {}".format(f.id()), s["depth_ft"],
                    kind=_txt(_attr(f, fld["L_KIND"])), x=s["x"], y=s["y"]))
            outside["lifelines"] = out_n
        # downstream dams
        dam_xy = {}
        got = self._source(parameters, context, "DOWNSTREAM_DAMS", DAM_SYNONYMS, grid, crs,
                           feedback, "Downstream dams")
        if got:
            feats, fld, to_r, lname = got
            inputs["downstream_dams"], fields["downstream_dams"] = lname, fld
            out_n = 0
            for f in feats:
                s = self._sample(sampler, f.geometry(), to_r)
                if s is None:
                    out_n += 1
                    continue
                ident = _txt(_attr(f, fld["D_ID"])) or "dam {}".format(f.id())
                if own_id and ident.lower() == own_id.lower():
                    feedback.pushInfo("Downstream dams: {} is this dam; left out.".format(ident))
                    continue
                aff = s["depth_ft"] >= sampler.wet
                rec["dams"].append(nh.DownstreamDam(
                    ident, dam_class(_attr(f, fld["D_CLASS"])), affected=aff,
                    note="flood depth {:.1f} ft at the dam".format(s["depth_ft"])))
                dam_xy[ident] = (s["x"], s["y"])
            outside["downstream_dams"] = out_n
            feedback.pushInfo("Downstream dams: {} inside the rasters, {} reached by the "
                              "flood".format(len(rec["dams"]),
                                             sum(1 for x in rec["dams"] if x.affected)))
        for k, n in outside.items():
            if n:
                feedback.pushInfo("{}: {} feature(s) outside the rasters were not used."
                                  .format(k.replace("_", " ").capitalize(), n))
        return rec, extra, dam_xy, inputs, fields

    def processAlgorithm(self, parameters, context, feedback):
        folder = self.parameterAsFile(parameters, "DAM_FOLDER", context)
        st = self._settings(parameters, context)
        charts = self._charts(parameters, context) if st.lethality == nh.LETHALITY_CHARTS \
            else None
        try:
            grid, with_f, _base, _notes = cons.read_dam_folder(folder)
        except ImportError:
            raise QgsProcessingException("GDAL's Python bindings are not available; run this "
                                         "from QGIS.")
        except (OSError, ValueError, RuntimeError) as ex:
            raise QgsProcessingException(str(ex))
        vel = with_f.get("velocity")
        if vel is None:
            if charts is not None:
                raise QgsProcessingException(
                    "The chart method needs 'Velocity (Max).tif' in the dam folder; it is "
                    "missing. Re-run the model, or use the conservative method.")
            feedback.reportError("Check: no 'Velocity (Max).tif'; velocities are reported as "
                                 "0 (not used by the chosen method).", fatalError=False)
        if st.lethality == nh.LETHALITY_1D:
            feedback.reportError("Check: ND 5.2.1.1 allows lethality from depth alone only for a "
                                 "1D hydraulic model. This plugin's results are 2D (LISFLOOD-FP / "
                                 "ANUGA): the policy expects the depth-velocity charts here.",
                                 fatalError=False)
        dam_id = _dam_id(folder)
        crs = self._raster_crs(grid.crs_wkt)
        sampler = Sampler(grid, with_f["depth"], vel, st.wet_ft)
        rec, extra, dam_xy, inputs, fields = self._read_all(
            parameters, context, grid, crs, sampler, feedback, dam_id)
        if not inputs:
            feedback.reportError("Check: no receptor layer was given; the class can only come "
                                 "out Low.", fatalError=False)
        try:
            c = nh.classify(rec["structures"], rec["roads"], rec["railroads"], rec["lifelines"],
                            rec["dams"], settings=st, charts=charts)
        except nh.ChartsNotDigitized as ex:
            raise QgsProcessingException(not_digitized_message(charts) if charts else str(ex))
        except ValueError as ex:
            raise QgsProcessingException(str(ex))
        for f in c.findings:
            if f.kind == "dam" and f.x is None and f.id in dam_xy:
                f.x, f.y = dam_xy[f.id]
        try:
            paths = self._write(folder, dam_id, c, rec, extra, inputs, fields, charts, grid)
        except OSError as ex:
            raise QgsProcessingException("Could not write the results: {}".format(ex))
        feedback.pushInfo("Hazard class: {} ({}; {}).".format(
            c.hazard_class.upper(), nh.POLICY, nh.LETHALITY_LABELS[st.lethality]))
        for r in c.reasons()[:30]:
            feedback.pushInfo("Governing: " + r)
        if len(c.governing) > 30:
            feedback.pushInfo("Governing: ... and {} more".format(len(c.governing) - 30))
        if c.borderline_raised:
            feedback.reportError("Check: every governing finding is borderline or rests on an "
                                 "assumption; the higher class was assigned (5.3).",
                                 fatalError=False)
        for gap in c.data_gaps:
            feedback.reportError("Data gap: " + gap, fatalError=False)
        for n in c.notes:
            feedback.pushInfo("Note: " + n)
        feedback.pushInfo("Screening classification for the engineer; the official "
                          "classification is made by the ND Department of Water Resources.")
        # v3.2.0: dam.json gets a "classified" status marked as a PREVIEW (never the authority)
        from .core import damjson
        state, text = damjson.classified_preview(folder)
        if state == "error":
            feedback.pushWarning("Check: " + text)
        else:
            feedback.pushInfo(text)
        if paths["geojson"]:
            self.load(context, paths["geojson"], "Hazard classification findings")
        return {"CLASS": c.hazard_class, "REPORT": paths["markdown"], "JSON": paths["json"],
                "CSV": paths["csv"], "GEOJSON": paths["geojson"]}

    # ------------------------------------------------------------------------------
    @staticmethod
    def _write(folder, dam_id, c, rec, extra, inputs, fields, charts, grid):
        from .core import products
        out = {k: os.path.join(folder, v) for k, v in OUTPUT_NAMES.items()}
        figures = []
        if charts is not None:
            wet = [s for s in rec["structures"] if s.habitable and s.depth_ft is not None
                   and s.depth_ft >= c.settings["wet_ft"]]
            for key, mobile in (("svg_house", False), ("svg_mobile", True)):
                ch = charts.charts[nh.CHART_MOBILE if mobile else nh.CHART_HOUSE]
                pts = [s for s in wet if s.mobile == mobile]
                with open(out[key], "w", encoding="utf-8") as f:
                    f.write(nh.chart_svg(ch, pts, title="{}{} ({} structure(s))".format(
                        dam_id + ": " if dam_id else "", ch.title, len(pts))))
                figures.append(OUTPUT_NAMES[key])
        else:
            for key in ("svg_house", "svg_mobile"):     # stale figures of an earlier chart run
                if os.path.exists(out[key]):
                    os.remove(out[key])
                out[key] = ""
        md = nh.markdown(c, dam_id, title="Hazard classification (ND 5.2)")
        lines = ["", "## Inputs", "", "- Dam folder: {}".format(folder)]
        for k in ("structures", "roads", "railroads", "lifelines", "downstream_dams"):
            if k in inputs:
                used = ", ".join("{} = {}".format(a[2:].lower(), b)
                                 for a, b in fields[k].items() if b) or "no field matched"
                lines.append("- {}: {} ({})".format(k.replace("_", " ").capitalize(),
                                                    inputs[k], used))
            else:
                lines.append("- {}: not given".format(k.replace("_", " ").capitalize()))
        lines += ["- Sampling: " + SAMPLING]
        if figures:
            lines += ["- Chart figures: " + ", ".join(figures)]
        with open(out["markdown"], "w", encoding="utf-8") as f:
            f.write(md + "\n".join(lines) + "\n")
        d = c.to_dict()
        d.update({"dam_id": dam_id, "dam_folder": folder, "inputs": inputs, "fields": fields,
                  "sampling": SAMPLING, "figures": figures,
                  "lethality_label": nh.LETHALITY_LABELS.get(c.settings.get("lethality"), ""),
                  "tool": "ndbreach:ndhazardclass",
                  "official": "The official classification is made by the ND Department of "
                              "Water Resources."})
        with open(out["json"], "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1, default=str)
        with open(out["csv"], "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=nh.FINDING_COLUMNS)
            w.writeheader()
            w.writerows(nh.finding_rows(c))
        feats = []
        for f in c.findings:
            if f.x is None or f.y is None:
                continue
            props = {k: getattr(f, k) for k in nh.FINDING_COLUMNS if k not in ("x", "y")}
            props.update(extra.get((f.kind, f.id, f.x, f.y), {}))
            feats.append({"type": "Feature", "properties": props,
                          "geometry": {"type": "Point", "coordinates": [f.x, f.y]}})
        epsg = None
        try:
            from .core import gdal_io
            epsg = gdal_io.epsg_of(grid.crs_wkt) if grid.crs_wkt else None
        except Exception:
            epsg = None
        products.write_geojson(out["geojson"], feats, epsg)
        return out


NDHAZARD_ALGORITHMS = (NDHazardAlgorithm,)
