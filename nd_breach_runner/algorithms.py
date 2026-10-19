"""Processing algorithms of the ND Dam Breach Runner provider (id ndbreach)."""
import copy
import csv
import math
import os
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY,
                       QgsProcessingAlgorithm, QgsProcessingContext, QgsProcessingException,
                       QgsProcessingOutputFile, QgsProcessingOutputFolder,
                       QgsProcessingOutputRasterLayer, QgsProcessingOutputString,
                       QgsProcessingParameterBoolean, QgsProcessingParameterCrs,
                       QgsProcessingParameterEnum, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterField, QgsProcessingParameterFile,
                       QgsProcessingParameterFileDestination, QgsProcessingParameterNumber,
                       QgsProcessingParameterPoint, QgsProcessingParameterRasterLayer,
                       QgsProcessingParameterString)

from . import qgis_io as qio
from .compat import (NUM_DOUBLE, NUM_INT, FILE_FILE, FILE_FOLDER, SOURCE_POINT, SOURCE_LINE,
                     SOURCE_POLYGON, FIELD_NUMERIC, FIELD_ANY, set_advanced)
from .core import (workflow, breach as brmod, flood as floodmod, impacts as impmod,
                   structures as structmod, conditioning as condmod, nd_data, handoff,
                   sensitivity as sensmod, cascade as cascmod, estimate as estmod,
                   outlets as outmod, damjson, stoprule, products as prodmod,
                   batchqueue)
from .core.lisflood import Cancelled, ModelError, check_lisflood
from .core.anuga_model import check_anuga
from .core.units import FT, MILE
from .damsuite import solvegate as SG

ALL_FILES = "All files (*)"     # 3.10.0: any file name, with or without an extension

PROVIDER_ID = "ndbreach"
SET_LISFLOOD = "NDBREACH_LISFLOOD_EXE"
SET_ANUGA = "NDBREACH_ANUGA_PYTHON"
SET_THREADS = "NDBREACH_THREADS"
SET_BUILTIN = "NDBREACH_USE_BUILTIN"   # 3.11.0

METHODS = [(m, brmod.METHOD_LABELS[m]) for m in brmod.METHODS]
TIERS = [(workflow.TIER_LISFLOOD, "LISFLOOD-FP only"),
         (workflow.TIER_BOTH, "LISFLOOD-FP for the extent, then ANUGA (final results from ANUGA)")]
ZUNITS = [("auto", "Detect from the raster"), ("feet", "Feet"),
          ("us_feet", "US survey feet"), ("meters", "Meters")]
# v3.2.0: one EAP map (ND Dam Hazard's map book); this tool's SVG is a preview
EAP_MAP_CHOICES = [(c, prodmod.EAP_LABELS[c]) for c in prodmod.EAP_CHOICES]
SCENARIOS = [(workflow.SCENARIO_SUNNY, "Sunny day: no inflow, breach at the top of dam "
                                        "(ND 5.1.1)"),
             (workflow.SCENARIO_FLOOD, "Flood: PMF inflow, with the breach and without it "
                                       "(incremental damage, ND 6.4)")]
TRIGGERS = [(floodmod.TRIGGER_PEAK, "at the highest pool level"),
            (floodmod.TRIGGER_TIME, "at a set time (hours after the inflow starts)"),
            (floodmod.TRIGGER_WSE, "when the pool reaches an elevation (ft)")]
STRUCT_KINDS = [(structmod.KIND_CULVERT, "Culvert"), (structmod.KIND_BRIDGE, "Bridge"),
                (structmod.KIND_NOTCH, "Open notch / breached road")]
# 3.20.0 (feature 14): the crossings layer (NBI bridges and culverts, rail grade crossings,
# the offline pack's surveyed road-stream crossings). Only a SURVEYED span and rise can be cut
# as a gap, so the span / rise synonyms deliberately do not include a generic "width".
CROSSING_FIELD_SYNONYMS = {
    "X_NAME": ("name", "id", "structure_number", "struct_num", "bridge_id", "crossing_id",
               "facility", "label"),
    "X_KIND": ("type", "kind", "structure_type", "structure_kind", "crossing_type"),
    "X_SPAN": ("span_ft", "span", "max_span_ft", "maxspan", "diameter", "dia", "culv_span_ft"),
    "X_RISE": ("rise_ft", "rise", "height_ft", "culv_rise_ft", "vert_clearance_ft"),
    "X_BARRELS": ("barrels", "n_barrels", "num_barrels", "cells", "num_cells"),
    "X_SHAPE": ("shape", "culvert_shape", "barrel_shape"),
    "X_INV_UP": ("invert_up", "inv_up", "upstream_invert", "invert_us", "inlet_invert"),
    "X_INV_DN": ("invert_dn", "inv_dn", "downstream_invert", "invert_ds", "outlet_invert"),
}

STRUCT_FIELD_SYNONYMS = {
    "S_KIND": ("type", "kind", "structure_type", "structtype"),
    "S_WIDTH": ("width", "span", "width_ft", "diameter", "dia", "span_ft"),
    "S_HEIGHT": ("height", "rise", "height_ft", "rise_ft"),
    "S_BARRELS": ("barrels", "n_barrels", "num_barrels", "cells"),
    "S_SHAPE": ("shape", "culvert_shape"),
    "S_INV_UP": ("invert_up", "inv_up", "upstream_invert", "invert_us"),
    "S_INV_DN": ("invert_dn", "inv_dn", "downstream_invert", "invert_ds"),
    "S_NAME": ("name", "id", "structure_id", "label"),
}


def _attr_of(feature, name):
    """A feature attribute by field name, None when the field is not there (3.20.0)."""
    if not name:
        return None
    try:
        return feature[name]
    except (KeyError, IndexError):
        return None


def _setting(key, default=""):
    try:
        from processing.core.ProcessingConfig import ProcessingConfig
        v = ProcessingConfig.getSetting(key)
        return default if v in (None, "") else v
    except Exception:
        return default


def _safe_name(s):
    s = re.sub(r"[^\w\- ]+", "_", str(s)).strip()
    return s or "dam"


def _point_along_nearest(lines, xy, frac):
    """The point `frac` (0-1) of the way along the line nearest `xy`."""
    best, bd = None, None
    for ln in lines:
        p = np.asarray(ln, float)
        d = np.min(np.hypot(p[:, 0] - xy[0], p[:, 1] - xy[1]))
        if bd is None or d < bd:
            best, bd = p, d
    seg = np.hypot(*np.diff(best, axis=0).T)
    total = float(seg.sum())
    if total <= 0:
        return (float(best[0][0]), float(best[0][1]))
    target = min(max(frac, 0.0), 1.0) * total
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    i = int(np.clip(np.searchsorted(cum, target, side="right") - 1, 0, len(seg) - 1))
    t = (target - cum[i]) / seg[i] if seg[i] > 0 else 0.0
    q = best[i] + t * (best[i + 1] - best[i])
    return (float(q[0]), float(q[1]))


CREST_SNAP_M = 500.0      # 3.13.0: a crest line farther than this from the dam point is not its line


def _nearest_on_line(line, xy):
    """(distance, (x, y)) of the point of polyline `line` nearest `xy` (segments, not vertices)."""
    p = np.asarray(line, float)
    best = None
    for a, b in zip(p[:-1], p[1:]):
        ab = b - a
        L2 = float(ab @ ab)
        t = 0.0 if L2 <= 0 else min(max(float((np.asarray(xy) - a) @ ab) / L2, 0.0), 1.0)
        q = a + t * ab
        d = float(math.hypot(q[0] - xy[0], q[1] - xy[1]))
        if best is None or d < best[0]:
            best = (d, (float(q[0]), float(q[1])))
    return best


def _dwr_ids(dam_id, rec):
    """3.13.0: the DWR structure numbers a dam may carry: dam.json's dwr_structure_id and the
    digits of a 'DWR-2801' style dam ID. Strings without leading zeros."""
    out = set()
    v = getattr(rec, "get", lambda _k: None)("dwr_structure_id") if rec is not None else None
    for x in (v, ):
        if x not in (None, ""):
            out.add(str(x).strip().split(".")[0].lstrip("0") or "0")
    m = re.match(r"^\s*DWR[-_ ]?0*(\d+)\s*$", str(dam_id or ""), re.I)
    if m:
        out.add(m.group(1).lstrip("0") or "0")
    return out


def _pick_crest_line(lines_attrs, center_xy, ids, id_field):
    """3.13.0: (line, distance, how) of the crest line for this dam, or (None, d, reason).
    The line whose structure id matches the dam's DWR number wins; otherwise the line nearest
    the dam point, and only within CREST_SNAP_M."""
    if not lines_attrs:
        return None, None, "no crest line near the dam"
    if ids and id_field:
        hit = [(ln, at) for ln, at in lines_attrs
               if str(at.get(id_field) if at.get(id_field) is not None else "").strip()
               .split(".")[0].lstrip("0") in ids]
        if hit:
            ln = min((h[0] for h in hit), key=lambda l: _nearest_on_line(l, center_xy)[0])
            return ln, _nearest_on_line(ln, center_xy)[0], "DWR structure {}".format(
                sorted(ids)[0])
    ln = min((x[0] for x in lines_attrs), key=lambda l: _nearest_on_line(l, center_xy)[0])
    d = _nearest_on_line(ln, center_xy)[0]
    if d > CREST_SNAP_M:
        return None, d, "the nearest crest line is {:.0f} m from the dam point".format(d)
    return ln, d, "nearest line"


def _layer_overlap(src, title, work, box, context):
    """3.13.0: damsuite.solvegate.layer_overlap for a Processing feature source."""
    try:
        n = int(src.featureCount())
    except Exception:                                            # noqa
        n = None
    try:
        crs = src.sourceCrs()
        valid = crs is not None and crs.isValid()
    except Exception:                                            # noqa - not known: not blamed
        crs, valid = None, True
    ext = None
    if valid and crs is not None:
        try:
            r = QgsCoordinateTransform(crs, work, context.transformContext()).transformBoundingBox(
                src.sourceExtent())
            ext = (r.xMinimum(), r.yMinimum(), r.xMaximum(), r.yMaximum())
        except Exception:                                        # noqa - reported as unknown
            ext = None
    return SG.layer_overlap(title, valid, ext, box, n if n is not None and n >= 0 else None)


def _floats(text, default=()):
    """'0.7, 1.4' -> (0.7, 1.4)"""
    out = []
    for p in re.split(r"[,;\s]+", str(text or "").strip()):
        if p:
            try:
                out.append(float(p))
            except ValueError:
                raise QgsProcessingException("'{}' is not a number in the list '{}'".format(
                    p, text))
    return tuple(out) if out else tuple(default)


DEM_RESAMPLE_AUTO = "Automatic"


def dem_resample_param(index):
    """The DEM_RESAMPLE enum index as a GdalTerrain `resample` (None = automatic)."""
    from .core import gdal_io
    i = int(index or 0)
    return gdal_io.RESAMPLE_METHODS[i - 1] if 0 < i <= len(gdal_io.RESAMPLE_METHODS) else None


class _Feedback:
    """Adapts QgsProcessingFeedback to the workflow callbacks."""

    def __init__(self, feedback, lo=0.0, hi=100.0):
        self.fb = feedback
        self.lo, self.hi = lo, hi
        self._last = ""

    def progress(self, p, msg=""):
        self.fb.setProgress(self.lo + (self.hi - self.lo) * max(0.0, min(100.0, p)) / 100.0)
        if msg and msg != self._last:
            self.fb.setProgressText(msg)
            self._last = msg

    def log(self, msg):
        self.fb.pushInfo(msg)

    def cancel(self):
        return self.fb.isCanceled()

    # 3.21.0: QgsProcessingFeedback pass-throughs, so code written against a real feedback
    # (RunDamAlgorithm.prepare calls pushInfo / pushWarning) does not raise when it is handed
    # this adapter. The queue's per-dam set-up used to crash here on the first dam.
    def pushInfo(self, msg):
        self.log(msg)

    def pushWarning(self, msg):
        self.fb.pushWarning(msg)

    def pushDebugInfo(self, msg):
        self.fb.pushDebugInfo(msg)

    def reportError(self, msg, fatalError=False):
        self.fb.reportError(msg, fatalError)

    def setProgress(self, p):
        self.progress(p)

    def setProgressText(self, msg):
        self.fb.setProgressText(msg)

    def isCanceled(self):
        return self.cancel()


def _srs_text(crs):
    """A CRS as text GDAL can read: EPSG code when there is one, else WKT (custom CRSs have
    ids like USER:100000 that GDAL cannot resolve)."""
    a = crs.authid() or ""
    return a if a.upper().startswith(("EPSG:", "ESRI:", "IGNF:")) else crs.toWkt()


class _Base(QgsProcessingAlgorithm):
    def group(self):
        return "Dam breach modelling"

    def groupId(self):
        return "breach"

    def createInstance(self):
        return type(self)()

    # -- parameter helpers ------------------------------------------------------
    def _num(self, name, desc, default=None, optional=False, minimum=None, maximum=None,
             integer=False, advanced=False):
        p = QgsProcessingParameterNumber(name, desc, NUM_INT if integer else NUM_DOUBLE,
                                         defaultValue=default, optional=optional)
        if minimum is not None:
            p.setMinimum(minimum)
        if maximum is not None:
            p.setMaximum(maximum)
        if advanced:
            set_advanced(p)
        self.addParameter(p)

    def _text(self, name, desc, default="", advanced=False, optional=True):
        p = QgsProcessingParameterString(name, desc, default, optional=optional)
        if advanced:
            set_advanced(p)
        self.addParameter(p)

    def _bool(self, name, desc, default=False, advanced=False):
        p = QgsProcessingParameterBoolean(name, desc, default)
        if advanced:
            set_advanced(p)
        self.addParameter(p)

    def _file(self, name, desc, ext=None, folder=False, optional=True, advanced=False):
        # 3.10.0: a file with no extension given accepts any name; QGIS's default filter
        # "All files (*.*)" refused names without a dot (a Linux / macOS LISFLOOD-FP or python)
        kw = {"extension": ext} if ext else {} if folder else {"fileFilter": ALL_FILES}
        p = QgsProcessingParameterFile(name, desc, FILE_FOLDER if folder else FILE_FILE,
                                       optional=optional, **kw)
        if advanced:
            set_advanced(p)
        self.addParameter(p)

    def _layer(self, name, desc, kind, advanced=False):
        kinds = list(kind) if isinstance(kind, (list, tuple)) else [kind]
        p = QgsProcessingParameterFeatureSource(name, desc, kinds, optional=True)
        if advanced:
            set_advanced(p)
        self.addParameter(p)

    def _field(self, name, desc, parent, numeric=True, optional=True, advanced=False):
        p = QgsProcessingParameterField(
            name, desc, parentLayerParameterName=parent,
            type=FIELD_NUMERIC if numeric else FIELD_ANY, optional=optional)
        if advanced:
            set_advanced(p)
        self.addParameter(p)

    def _opt(self, parameters, name, context):
        v = parameters.get(name)
        if v is None or v == "" or (hasattr(v, "isNull") and v.isNull()):
            return None
        try:
            return self.parameterAsDouble(parameters, name, context)
        except Exception:
            return None

    def _str(self, parameters, name, context):
        v = parameters.get(name)
        if v is None or (hasattr(v, "isNull") and v.isNull()):
            return ""
        return (self.parameterAsString(parameters, name, context) or "").strip()

    def _fileparam(self, parameters, name, context):
        v = parameters.get(name)
        if v is None or v == "":
            return ""
        return self.parameterAsFile(parameters, name, context) or ""

    # -- parameter groups ------------------------------------------------------------
    def add_breach_params(self, per_dam=True):
        self.addParameter(QgsProcessingParameterEnum(
            "METHOD", "Breach (ND 5.1.1(d))", [m[1] for m in METHODS], defaultValue=0))
        if per_dam:
            self._file("USER_CSV", "Hydrograph CSV (for 'my own hydrograph')", "csv")
            self._num("CREST_WIDTH", "Crest width (ft): TR-60, MacDonald & Langridge-Monopolis",
                      optional=True, minimum=0)
            self._num("US_SLOPE", "Upstream slope, H in H:1V (TR-60, MacDonald)", optional=True,
                      minimum=0)
            self._num("DS_SLOPE", "Downstream slope, H in H:1V (TR-60, MacDonald)",
                      optional=True, minimum=0)
            self._num("CREST_LENGTH", "Crest length (ft), for overtopping in the flood scenario",
                      optional=True, minimum=0)
            self._num("VALLEY_WIDTH", "Valley width at the water surface (ft), for the "
                      "TR-60 narrow-valley limit", optional=True, minimum=0, advanced=True)
            self._num("BREACH_WIDTH", "Override: average breach width (ft)", optional=True,
                      minimum=0, advanced=True)
            self._num("FORMATION_HR", "Override: breach formation time (hr)", optional=True,
                      minimum=0, advanced=True)
            self._num("SIDE_SLOPE", "Override: breach side slope, H in H:1V", optional=True,
                      minimum=0, advanced=True)
            self._num("PIPE_FRACTION", "Pipe phase: share of the formation time as a pipe",
                      0.5, minimum=0.05, maximum=0.95, advanced=True)
            self._num("PIPE_DIA_FT", "Pipe phase: starting pipe diameter (ft; starting value "
                      "0.5 is the engineer's, a screening value you choose)", 0.5,
                      minimum=0.05, advanced=True)
            self._bool("VTG_ERODIBLE", "Von Thun & Gillette: erodible embankment (else "
                       "resistant)", True, advanced=True)
            self._num("PARTIAL_DEPTH", "Partial breach: breach depth as a share of the dam "
                      "height (0.1-1; empty = full depth)", optional=True, minimum=0.1,
                      maximum=1.0, advanced=True)

    def add_scenario_params(self, per_dam=True):
        self.addParameter(QgsProcessingParameterEnum(
            "SCENARIO", "Scenario", [s[1] for s in SCENARIOS], defaultValue=0))
        self._file("INFLOW_CSV", "Flood scenario: PMF inflow hydrograph CSV (time hr, "
                   "discharge cfs)", "csv")
        self._file("SPILLWAY_CSV", "Flood scenario: spillway rating CSV (elevation ft, "
                   "discharge cfs; empty = no spillway)", "csv")
        if per_dam:
            self._num("NORMAL_POOL_FT", "Flood scenario: normal pool elevation (ft)",
                      optional=True)
        self.addParameter(QgsProcessingParameterEnum(
            "TRIGGER", "Flood scenario: the dam fails", [t[1] for t in TRIGGERS],
            defaultValue=0))
        self._num("TRIGGER_VALUE", "Flood scenario: failure time (hr) or pool elevation (ft)",
                  optional=True)
        if per_dam:
            self._file("BASELINE_CSV", "Flood scenario, own hydrographs: outflow without the "
                       "breach CSV", "csv", advanced=True)
        self._bool("BASELINE", "Flood scenario: also run without the breach and write the "
                   "incremental depth and WSE rise", True)
        self._text("INCR_FT", "Incremental depth thresholds (ft), comma separated (starting "
                   "values 1, 2, 3 are the engineer's; compare with ND 6.4 yourself)", "1, 2, 3",
                   advanced=True)
        self._num("AMBIENT_CFS", "Ambient stream flow when the event starts (cfs; added at the "
                  "breach inflow; also gives a sunny-day baseline for incremental results)",
                  optional=True, minimum=0, advanced=True)
        self._num("SPINUP_HR", "Ambient flow: spin-up before the breach hydrograph (hr)", 2.0,
                  minimum=0.0, advanced=True)
        self._text("OUTLETS", "Flood scenario: outlet works, one per line 'name; invert ft; "
                   "diameter ft (or width x height); cd; gate hours:fraction, ...'",
                   advanced=True)
        self._text("SPILLWAY_GATE", "Flood scenario: gate on the spillway rating, hours:fraction "
                   "pairs like '0:1, 6:0.5' (steps)", advanced=True)

    def add_feature_params(self):
        """Things in the flow path: receptors, structures, DEM conditioning, gauges."""
        self._layer("RECEPTOR_POINTS", "Structures (points, e.g. NSI, or building footprint "
                    "polygons): decide how far downstream to model and are reported when "
                    "flooded; polygons also serve the 'Buildings' option below",
                    [SOURCE_POINT, SOURCE_POLYGON])
        self._layer("RECEPTOR_ROADS", "Roads (lines)", SOURCE_LINE)
        self._layer("RECEPTOR_RAIL", "Railways (lines)", SOURCE_LINE)
        self._num("LOOKAHEAD_MI", "Stop extending the domain when no receptor lies within "
                  "(mi) of the flood's path beyond it", 5.0, minimum=0.1, advanced=True)
        self._layer("STRUCTURES", "Culverts, bridges and notches (lines, upstream end first)",
                    SOURCE_LINE)
        for key, desc in (("S_KIND", "  kind field (culvert / bridge / notch)"),
                          ("S_WIDTH", "  span or diameter field (ft)"),
                          ("S_HEIGHT", "  rise field (ft)"),
                          ("S_BARRELS", "  barrels field"),
                          ("S_SHAPE", "  shape field (box / pipe)"),
                          ("S_INV_UP", "  upstream invert elevation field (ft)"),
                          ("S_INV_DN", "  downstream invert elevation field (ft)"),
                          ("S_NAME", "  name field")):
            self._field(key, "Structures:" + desc, "STRUCTURES", numeric=False, advanced=True)
        self.addParameter(set_advanced(QgsProcessingParameterEnum(
            "S_DEFAULT_KIND", "Structures: kind when there is no kind field",
            [k[1] for k in STRUCT_KINDS], defaultValue=0)))
        self._num("S_DEFAULT_WIDTH_FT", "Structures: span when there is no span field (ft; "
                  "starting value 4 is the engineer's, a screening value you choose)",
                  4.0, minimum=0.5, advanced=True)
        self._num("S_DEFAULT_HEIGHT_FT", "Structures: rise when there is no rise field (ft; "
                  "starting value 4 is the engineer's, a screening value you choose)",
                  4.0, minimum=0.5, advanced=True)
        self._num("S_INVERT_TOL_FT", "Structures: inventory inlet / outlet elevations are used when within "
                  "this of the LiDAR bed at each end (ft; YOUR value). Both ends off by the same amount = "
                  "a datum shift, moved to the LiDAR; farther off = the LiDAR inverts are used and the run "
                  "report says so", 3.0, minimum=0.0, advanced=True)
        # 3.20.0 (feature 14): inventory crossings measured against the model's embankments
        # (core/gaps.py). Reported always; cut only with CROSSING_GAPS, because opening an
        # embankment lowers the water behind it.
        self._layer("CROSSINGS", "Crossings (points): NBI bridges and culverts, rail grade "
                    "crossings, surveyed road-stream crossings. Each one is measured against "
                    "the embankment the bare-earth DEM leaves across it and reported in the "
                    "run report", SOURCE_POINT, advanced=True)
        for key, desc in (("X_NAME", "  name field"),
                          ("X_KIND", "  kind field (bridge / culvert)"),
                          ("X_SPAN", "  surveyed span or diameter field (ft)"),
                          ("X_RISE", "  surveyed rise field (ft)"),
                          ("X_BARRELS", "  barrels field"),
                          ("X_SHAPE", "  shape field (box / pipe)"),
                          ("X_INV_UP", "  upstream invert elevation field (ft)"),
                          ("X_INV_DN", "  downstream invert elevation field (ft)")):
            self._field(key, "Crossings:" + desc, "CROSSINGS", numeric=False, advanced=True)
        self._bool("CROSSING_GAPS", "Crossings: cut the ones whose opening is surveyed as gaps "
                   "in the embankment (off by default: a gap LOWERS the water behind the road, "
                   "so the result upstream of it is less conservative than the default; a "
                   "crossing without a surveyed opening is never cut)", False, advanced=True)
        # v3.4.0: building footprints as obstructions (polygons in the structures layer)
        self.addParameter(set_advanced(QgsProcessingParameterEnum(
            "BUILDINGS", "Buildings: how polygon footprints in the structures layer enter the "
            "model (point structures are ignored for this: bare terrain there)",
            [condmod.BUILDINGS_LABELS[m] for m in condmod.BUILDINGS_MODES], defaultValue=0)))
        self._num("BUILDING_RAISE_FT", "Buildings: raise the DEM inside each footprint by (ft "
                  "above the highest ground inside it; starting value {:g} is the engineer's, "
                  "a screening value you choose)".format(condmod.BUILDING_RAISE_FT_DEFAULT),
                  condmod.BUILDING_RAISE_FT_DEFAULT, minimum=0.1, advanced=True)
        self._num("BUILDING_N", "Buildings: Manning's n inside each footprint (starting value "
                  "{:g} is the engineer's, a screening value you choose)".format(
                      condmod.BUILDING_N_DEFAULT),
                  condmod.BUILDING_N_DEFAULT, minimum=0.01, maximum=5.0, advanced=True)
        self._layer("CHANNEL_LINES", "Channels to burn into the DEM (lines, upstream to "
                    "downstream)", SOURCE_LINE, advanced=True)
        self._num("CH_WIDTH_FT", "Channels: bottom width (ft; starting value 15 is the "
                  "engineer's)", 15.0, minimum=1, advanced=True)
        self._num("CH_DEPTH_FT", "Channels: depth below the enforced falling profile (ft; "
                  "starting value 1 is the engineer's, a screening value you choose)", 1.0,
                  minimum=0, advanced=True)
        self._layer("PONDS", "Ponds and reservoirs that are full when the flood arrives "
                    "(polygons; lowered to their bed, started full)", SOURCE_POLYGON,
                    advanced=True)
        self._num("POND_DEPTH_FT", "Ponds: mean depth below the LiDAR water surface (ft; "
                  "starting value 5 is the engineer's, a screening value you choose)", 5.0,
                  minimum=0.1, advanced=True)
        self._layer("TAILWATER", "Tailwater: river or lake reaches already standing at a stage "
                    "when the flood arrives (polygons; every cell inside lower than the stage "
                    "starts wet, so draw them around the water only)", SOURCE_POLYGON, advanced=True)
        self._num("TW_STAGE_FT", "Tailwater: water surface elevation (ft, DEM datum)",
                  optional=True, advanced=True)
        self._field("TW_STAGE_FIELD", "Tailwater: stage field (ft), instead of the number",
                    "TAILWATER", advanced=True)
        self._layer("CREST_LINE", "Dam crest (line): raised to the top of dam in the model DEM "
                    "(the DEM smooths a crest, and breach water spills back over it); with a "
                    "position the breach also starts there instead of at the dam point. "
                    "Without a line the crest is traced from the DEM", SOURCE_LINE, advanced=True)
        self._bool("RAISE_CREST", "Raise the dam line to the top of dam in the model DEM, so "
                   "breach water cannot flow back over a smoothed crest into the reservoir "
                   "basin (needs the top of dam; the source DEM is not changed; on by default)",
                   True, advanced=True)
        self._num("BREACH_POSITION", "Breach position along the crest line (0 = first vertex, "
                  "1 = last; leave empty to breach where the dam point meets the crest line)",
                  None, optional=True, minimum=0.0, maximum=1.0, advanced=True)
        self._layer("REFINE_LINES", "ANUGA: lines the mesh follows (levees, road embankments, "
                    "channel banks)", SOURCE_LINE, advanced=True)
        self._layer("GAUGE_POINTS", "Discharge gauges: hydrograph past these points "
                    "(points)", SOURCE_POINT, advanced=True)
        self._field("G_NAME", "Gauges: name field", "GAUGE_POINTS", numeric=False,
                    advanced=True)

    def add_model_params(self):
        self.addParameter(QgsProcessingParameterRasterLayer("DEM", "DEM (LiDAR; a VRT mosaic "
                                                            "is fine)"))
        self.addParameter(QgsProcessingParameterEnum("DEM_Z", "DEM elevation units",
                                                     [z[1] for z in ZUNITS], defaultValue=0))
        from .core import gdal_io
        self.addParameter(set_advanced(QgsProcessingParameterEnum(
            "DEM_RESAMPLE", "DEM resampling onto the model grid (Automatic = the {} "
            "environment variable, else {})".format(gdal_io.ENV_RESAMPLE, gdal_io.DEFAULT_RESAMPLE),
            [DEM_RESAMPLE_AUTO] + list(gdal_io.RESAMPLE_METHODS), defaultValue=0, optional=True)))
        self.addParameter(QgsProcessingParameterEnum("TIER", "Models",
                                                     [t[1] for t in TIERS], defaultValue=0))
        self._bool("FORCE_ANUGA", "Force ANUGA: final results come from ANUGA even when "
                   "Models is set to LISFLOOD-FP only", False)
        self._num("CELL", "Model cell size (m; 10 m = 32.8 ft, the raster cell, not a depth)",
                  10.0, minimum=0.5)
        self._num("MANNING", "Manning's n (where there is no n raster)", 0.06, minimum=0.005,
                  maximum=1.0)
        self.addParameter(QgsProcessingParameterRasterLayer(
            "MANNING_RASTER", "Manning's n raster (values of n)", optional=True))
        self.addParameter(QgsProcessingParameterRasterLayer(
            "NLCD_RASTER", "or an NLCD land-cover raster (class codes; n from the table)",
            optional=True))
        self._text("NLCD_N", "NLCD: your own n for some classes, JSON like {\"82\": 0.05}",
                   advanced=True)
        self._num("ARRIVAL_FT", "Arrival depth (ft): arrival time is when the depth first "
                  "exceeds this (starting value 1 is the engineer's, a screening value you "
                  "choose)", 1.0, minimum=0.01)
        self._num("EDGE_TOL_FT", "Extend the domain while the flood leaves an edge deeper "
                  "than (ft; starting value 1 is the engineer's, a screening value you choose)",
                  1.0, minimum=0.01)
        # v3.4.0: near-dam refine on a fine FV1 grid (refine.py). v3.12.0: off by default
        # again - LISFLOOD-FP 8.1 writes no FV1 maximum velocity, so the fine box kept depth
        # but had zero velocity (the ACC velocities are now kept there; see refine.py)
        self._bool("REFINE_NEAR", "Near-dam refine: re-run the zone near the dam on a fine "
                   "grid with the FV1 solver when receptors sit within the flagged distance "
                   "(the ACC velocities there are unreliable; LISFLOOD-FP final results only; "
                   "off by default)", False, advanced=True)
        self._num("REFINE_DIST_MI", "Near-dam refine: flagged distance from the breach point "
                  "(mi; starting value 1 is the engineer's, a screening value you choose)",
                  1.0, minimum=0.05, advanced=True)
        self._num("REFINE_CELL", "Near-dam refine: fine cell size (m; 0 = half the model cell; "
                  "10 m = 32.8 ft)", 0.0, minimum=0.0, advanced=True)
        self._bool("STOP_RULE", "Class-stop rule: accept an edge where the flood's depth (or "
                   "its rise over the no-breach baseline) stays below the threshold along the "
                   "flow path near it and no receptor lies beyond (ND Dam Hazard's "
                   "termination-point rule; off by default)", False, advanced=True)
        self._num("STOP_THRESHOLD_FT", "Class-stop rule: threshold for the depth or the rise "
                  "(ft; starting value 2 = ND Dam Hazard's default, a screening value you "
                  "choose)", stoprule.DEFAULT_THRESHOLD_FT, minimum=0.01, advanced=True)
        self._num("STOP_CONFIRM", "Class-stop rule: stations that confirm the first station "
                  "below the threshold (starting value 3 = ND Dam Hazard's default)",
                  stoprule.DEFAULT_CONFIRM, minimum=1, maximum=50, integer=True, advanced=True)
        self._num("STOP_STATION_MI", "Class-stop rule: station spacing along the flow path "
                  "(mi; starting value 0.5 = ND Dam Hazard's default)",
                  stoprule.DEFAULT_STATION_MI, minimum=0.02, advanced=True)
        self._num("STOP_SEARCH_FT", "Class-stop rule: a station's value is the deepest wet "
                  "cell within (ft; starting value 150 = ND Dam Hazard's default)",
                  stoprule.DEFAULT_SEARCH_FT, minimum=1.0, advanced=True)
        self._num("LENGTH_MI", "First domain: downstream length (mi)", 5.0, minimum=0.25)
        self._num("HALF_WIDTH_MI", "First domain: half-width either side of the flow path (mi)",
                  1.0, minimum=0.1)
        self._num("STEP_MI", "Extension step (mi)", 3.0, minimum=0.1, advanced=True)
        self._num("MAX_EXTENT_MI", "Largest domain side (mi)", 60.0, minimum=1.0, advanced=True)
        self._num("MAX_RUNS", "Most LISFLOOD-FP runs", 6, minimum=1, integer=True, advanced=True)
        self._num("DURATION_HR", "Simulation duration (hr), 0 = automatic", 0.0, minimum=0.0)
        self._num("DURATION_MAX_HR", "Longest simulated time (hr): the clock is lengthened "
                  "up to this while the flood is still moving", 96.0, minimum=1.0)
        self.addParameter(set_advanced(QgsProcessingParameterCrs(
            "WORK_CRS", "Model CRS (metric; empty = NAD83 UTM zone of the dam)", optional=True)))
        self._num("ANUGA_FINE", "ANUGA: largest triangle near the dam (m²; 100 m² = 1,076 ft²)",
                  100.0, minimum=1.0, advanced=True)
        self._num("ANUGA_COARSE", "ANUGA: largest triangle elsewhere (m²; 1,000 m² = 10,764 "
                  "ft², about a quarter acre)", 1000.0, minimum=1.0, advanced=True)
        self._num("ANUGA_NEAR_MI", "ANUGA: refined area radius from the dam (mi)", 1.0,
                  minimum=0.0, advanced=True)
        self._num("ANUGA_BUFFER", "ANUGA: mesh margin around the LISFLOOD-FP extent (m; 500 m "
                  "= 1,640 ft)", 500.0, minimum=0.0, advanced=True)
        self._num("THREADS", "Threads (0 = the provider setting / all)", 0, minimum=0,
                  integer=True, advanced=True)
        self._file("LISFLOOD_EXE", "LISFLOOD-FP executable (empty = provider setting, an installed LISFLOOD-FP, the "
                   "one bundled with the plugin, else the built-in engine; 'builtin' = the "
                   "built-in engine)", advanced=True)
        # 3.9.0: a file picker again (as before 3.8.0; the same name, so saved values still
        # apply); a conda env's python symlink is still resolved (nd_data.resolve_python)
        self._file("ANUGA_PYTHON", "ANUGA Python interpreter (a conda env's python symlink is "
                   "resolved to the real file). Empty = provider setting or auto-detected",
                   advanced=True)
        self._bool("KEEP_FILES", "Keep the model input and output files", True, advanced=True)
        self._bool("PRODUCTS", "Write flood polygons, arrival zones, a GeoPackage, the EAP "
                   "inundation map preview and the HTML report", True, advanced=True)
        self.addParameter(set_advanced(QgsProcessingParameterEnum(
            "EAP_MAP", "EAP map (the EAP map is ND Dam Hazard's map book on aerial imagery)",
            [c[1] for c in EAP_MAP_CHOICES], defaultValue=0)))
        self._text("CONTOURS_HR", "Arrival zones (hours after breach initiation), comma "
                   "separated", "0.25, 0.5, 1, 2, 4, 8", advanced=True)
        self._bool("RESUME", "Resume: reuse LISFLOOD-FP runs already finished for the same "
                   "settings", False, advanced=True)
        self._bool("ANIMATE", "Animation: save depth snapshots and render a GIF / MP4 and "
                   "one GeoTIFF per frame (Animation folder)", False, advanced=True)
        self._num("ANIM_FRAMES", "Animation: about this many frames", 24, minimum=4,
                  maximum=240, integer=True, advanced=True)
        self._file("DAMS_FOLDER", "Dams folder (each dam gets a folder named after its ID)",
                   folder=True, optional=False)

    # -- reading parameters ------------------------------------------------------
    def find_lisflood(self, parameters, context):
        # 3.11.0: "builtin" = the built-in engine; empty and nothing installed = the bundled
        # LISFLOOD-FP, else the built-in engine (nd_data.detect_lisflood)
        from .core import lisflood as lfmod
        exe = self._fileparam(parameters, "LISFLOOD_EXE", context)
        if not exe and str(_setting(SET_BUILTIN, False)).lower() in ("true", "1", "yes"):
            return lfmod.BUILTIN_EXE
        exe = exe or _setting(SET_LISFLOOD)
        if exe and str(exe).strip().lower() in lfmod.BUILTIN_WORDS:
            return lfmod.BUILTIN_EXE
        if not exe or not os.path.isfile(exe):
            found = nd_data.detect_lisflood()
            if found:
                exe = found
        return exe

    def find_anuga(self, parameters, context):
        py = self._fileparam(parameters, "ANUGA_PYTHON", context) or _setting(SET_ANUGA)
        if py:
            py = nd_data.resolve_python(py)
        if not py or not os.path.isfile(py):
            found = nd_data.detect_anuga_python()
            if found:
                py = found
        return py

    def run_settings(self, parameters, context, method=None, need_models=True):
        g = lambda n: self.parameterAsDouble(parameters, n, context)
        exe = self.find_lisflood(parameters, context)
        py = self.find_anuga(parameters, context)
        threads = int(g("THREADS")) or int(_setting(SET_THREADS, 0) or 0)
        tier = TIERS[self.parameterAsEnum(parameters, "TIER", context)][0]
        if (self.parameterDefinition("FORCE_ANUGA") is not None
                and self.parameterAsBool(parameters, "FORCE_ANUGA", context)):
            tier = workflow.TIER_BOTH
        if need_models and (not exe or not os.path.isfile(exe)):
            raise QgsProcessingException(
                "Set the LISFLOOD-FP executable in Processing > Options > Providers > "
                "ND Dam Breach Runner (or in the advanced parameters); none was found on this "
                "computer. Found: '{}'".format(exe))
        if need_models and tier == workflow.TIER_BOTH and (not py or not os.path.isfile(py)):
            raise QgsProcessingException(
                "Set the ANUGA Python interpreter in Processing > Options > Providers > "
                "ND Dam Breach Runner; none was found on this computer. Found: '{}'".format(py))
        dur = g("DURATION_HR")
        sim_max = g("DURATION_MAX_HR") if self.parameterDefinition("DURATION_MAX_HR") is not None else 0.0
        scen = SCENARIOS[min(1, self.parameterAsEnum(parameters, "SCENARIO", context))][0] \
            if self.parameterDefinition("SCENARIO") is not None else workflow.SCENARIO_SUNNY
        st = workflow.RunSettings(
            tier=tier, breach_method=method or METHODS[self.parameterAsEnum(
                parameters, "METHOD", context)][0],
            cell_size_m=g("CELL"), manning_n=g("MANNING"), arrival_depth_ft=g("ARRIVAL_FT"),
            edge_tolerance_ft=g("EDGE_TOL_FT"), initial_length_mi=g("LENGTH_MI"),
            half_width_mi=g("HALF_WIDTH_MI"), expand_step_mi=g("STEP_MI"),
            max_extent_mi=g("MAX_EXTENT_MI"), max_iterations=int(g("MAX_RUNS")),
            sim_time_hr=dur if dur > 0 else None,
            sim_max_hr=sim_max if sim_max > 0 else None, lisflood_exe=exe, anuga_python=py,
            threads=threads, anuga_fine_area_m2=g("ANUGA_FINE"),
            anuga_coarse_area_m2=g("ANUGA_COARSE"), anuga_near_field_mi=g("ANUGA_NEAR_MI"),
            anuga_buffer_m=g("ANUGA_BUFFER"), scenario=scen,
            keep_model_files=self.parameterAsBool(parameters, "KEEP_FILES", context),
            products=self.parameterAsBool(parameters, "PRODUCTS", context),
            arrival_contours_hr=_floats(self._str(parameters, "CONTOURS_HR", context),
                                        (0.25, 0.5, 1, 2, 4, 8)),
            resume=self.parameterAsBool(parameters, "RESUME", context),
            impact_look_ahead_mi=g("LOOKAHEAD_MI") if self.parameterDefinition("LOOKAHEAD_MI") is not None else 5.0)
        st.planning_cell_m = max(30.0, st.cell_size_m)
        if self.parameterDefinition("PIPE_FRACTION") is not None:
            st.overrides = brmod.BreachOverrides(
                self._opt(parameters, "BREACH_WIDTH", context),
                self._opt(parameters, "FORMATION_HR", context),
                self._opt(parameters, "SIDE_SLOPE", context),
                pipe_time_fraction=g("PIPE_FRACTION"), pipe_initial_dia_ft=g("PIPE_DIA_FT"),
                vtg_erodible=self.parameterAsBool(parameters, "VTG_ERODIBLE", context))
        if self.parameterDefinition("BASELINE") is not None:
            st.baseline = self.parameterAsBool(parameters, "BASELINE", context)
            st.incr_thresholds_ft = _floats(self._str(parameters, "INCR_FT", context),
                                            (1.0, 2.0, 3.0))
        st.user_hydrograph = self._fileparam(parameters, "USER_CSV", context) or None
        if self.parameterDefinition("PARTIAL_DEPTH") is not None:
            st.overrides.depth_fraction = self._opt(parameters, "PARTIAL_DEPTH", context)
        if self.parameterDefinition("AMBIENT_CFS") is not None:
            st.ambient_cfs = self._opt(parameters, "AMBIENT_CFS", context) or 0.0
            st.spinup_hr = g("SPINUP_HR")
        if self.parameterDefinition("ANIMATE") is not None and \
                self.parameterAsBool(parameters, "ANIMATE", context):
            st.animation = True
            st.snapshot_frames = int(g("ANIM_FRAMES"))
        if self.parameterDefinition("STOP_RULE") is not None:
            st.class_stop = self.parameterAsBool(parameters, "STOP_RULE", context)
            st.class_stop_threshold_ft = g("STOP_THRESHOLD_FT")
            st.class_stop_confirm = int(g("STOP_CONFIRM"))
            st.class_stop_station_mi = g("STOP_STATION_MI")
            st.class_stop_search_ft = g("STOP_SEARCH_FT")
        if self.parameterDefinition("EAP_MAP") is not None:
            st.eap_map = EAP_MAP_CHOICES[min(len(EAP_MAP_CHOICES) - 1, self.parameterAsEnum(
                parameters, "EAP_MAP", context))][0]
        if self.parameterDefinition("RAISE_CREST") is not None:        # v3.11.0
            st.raise_crest = self.parameterAsBool(parameters, "RAISE_CREST", context)
        if self.parameterDefinition("REFINE_NEAR") is not None:        # v3.4.0
            st.refine_near = self.parameterAsBool(parameters, "REFINE_NEAR", context)
            st.refine_dist_mi = g("REFINE_DIST_MI") or 1.0
            st.refine_cell_m = g("REFINE_CELL") or 0.0
        self._inputs = {}
        st.input_files = self._inputs        # filled as the inputs are read; see _record
        return st

    def _record(self, label, path):
        """Remember an input file for the run manifest (hashes are taken at the end of the run)."""
        if path and os.path.isfile(str(path).split("|")[0]):
            self.__dict__.setdefault("_inputs", {})[label] = str(path).split("|")[0]

    def _vector_path(self, parameters, name, context):
        try:
            lyr = self.parameterAsVectorLayer(parameters, name, context)
            return lyr.source().split("|")[0] if lyr is not None else ""
        except Exception:
            return ""

    def flood_inputs(self, parameters, context, dam, files=None, pool_ft=None):
        """FloodInputs for the flood scenario. `files` (a dam folder's inputs)
        win over the parameters."""
        files = files or {}
        inflow = files.get("pmf_inflow") or self._fileparam(parameters, "INFLOW_CSV", context)
        if not inflow:
            raise brmod.BreachInputError("the flood scenario needs the PMF inflow hydrograph "
                                         "(a CSV: time hr, discharge cfs)")
        spill = files.get("spillway") or self._fileparam(parameters, "SPILLWAY_CSV", context)
        pool = pool_ft if pool_ft is not None else self._opt(parameters, "NORMAL_POOL_FT",
                                                             context)
        # 3.9.0: an empty normal pool may be filled from dam.json (provider DATA_INPUTS); a
        # value recorded on a local datum is not an elevation of the DEM
        rec = getattr(self, "_dam_rec", None)
        if pool_ft is None and pool is not None and damjson.local_datum(rec) and \
                rec.get("normal_pool_ft") is not None and \
                abs(float(rec.get("normal_pool_ft")) - pool) <= 1e-6 * max(1.0, abs(pool)):
            raise brmod.BreachInputError(
                "the normal pool elevation {:g} ft is dam.json's, whose datum is '{}' (not the "
                "DEM's): give the normal pool in the DEM datum".format(
                    pool, rec.get("top_of_dam_datum")))
        trig = TRIGGERS[self.parameterAsEnum(parameters, "TRIGGER", context)][0]
        val = self._opt(parameters, "TRIGGER_VALUE", context)
        if trig != floodmod.TRIGGER_PEAK and val is None:
            raise brmod.BreachInputError("give the failure time or pool elevation for the "
                                         "chosen trigger")
        base = files.get("baseline") or self._fileparam(parameters, "BASELINE_CSV", context)
        self._record("PMF inflow hydrograph", inflow)
        self._record("Spillway rating", spill)
        self._record("Baseline outflow", base)
        fi = floodmod.FloodInputs.from_files(
            inflow, spill or None, normal_pool_ft=pool, crest_length_ft=dam.crest_length_ft,
            trigger=trig, trigger_value=val, baseline_csv=base or None)
        if self.parameterDefinition("OUTLETS") is not None:
            fi.outlets = outmod.parse_outlets(self._str(parameters, "OUTLETS", context))
            fi.spillway_schedule = outmod.parse_schedule(
                self._str(parameters, "SPILLWAY_GATE", context))
        return fi

    def gis(self, parameters, context, sample_point_crs, sample_point):
        """Terrain source, writer and the model CRS for a dam at sample_point."""
        from .core import gdal_io
        dem = self.parameterAsRasterLayer(parameters, "DEM", context)
        if dem is None:
            raise QgsProcessingException("No DEM")
        work = self.parameterAsCrs(parameters, "WORK_CRS", context) \
            if self.parameterDefinition("WORK_CRS") is not None else QgsCoordinateReferenceSystem("")
        if not work.isValid():
            to_geo = QgsCoordinateTransform(sample_point_crs,
                                            QgsCoordinateReferenceSystem("EPSG:4269"),
                                            context.transformContext())
            ll = to_geo.transform(QgsPointXY(sample_point))
            work = QgsCoordinateReferenceSystem("EPSG:{}".format(gdal_io.utm_epsg(ll.x(),
                                                                                  ll.y())))
        work_srs = _srs_text(work)
        if gdal_io.linear_unit_m(work.toWkt()) != 1.0:
            raise QgsProcessingException("The model CRS must be projected in metres ({} is "
                                         "not)".format(work.authid()))
        has = lambda n: self.parameterDefinition(n) is not None
        man = self.parameterAsRasterLayer(parameters, "MANNING_RASTER", context) \
            if has("MANNING_RASTER") else None
        nlcd = self.parameterAsRasterLayer(parameters, "NLCD_RASTER", context) \
            if has("NLCD_RASTER") else None
        zc = ZUNITS[self.parameterAsEnum(parameters, "DEM_Z", context)][0] \
            if has("DEM_Z") else "auto"
        n_default = self.parameterAsDouble(parameters, "MANNING", context) \
            if has("MANNING") else 0.06
        kw = {}
        if man is None and nlcd is not None:
            try:
                kw["manning_lut"] = nd_data.manning_lut(self._str(parameters, "NLCD_N", context),
                                                        n_default)
            except ValueError as e:
                raise QgsProcessingException(str(e))
        self._record("DEM", dem.source())
        self._record("Manning / NLCD raster", (man or nlcd).source() if (man or nlcd) else "")
        kw["resample"] = dem_resample_param(self.parameterAsEnum(parameters, "DEM_RESAMPLE", context)
                                            if has("DEM_RESAMPLE") else 0)
        try:
            src = gdal_io.GdalTerrain(dem.source(), work_srs, zc,
                                      (man or nlcd).source() if (man or nlcd) else None,
                                      n_default, **kw)
        except ValueError as e:                       # a bad NDBREACH_DEM_RESAMPLE value
            raise QgsProcessingException(str(e))
        self._record("DEM resampling", "{} (from {})".format(src.resample, src.resample_source))
        dem_wkt = dem.crs().toWkt()
        target, res = None, None
        unit = gdal_io.linear_unit_m(dem_wkt) if dem.crs().isValid() else None
        if unit and not gdal_io.same_crs(dem_wkt, work.toWkt()):
            target = _srs_text(dem.crs()) or dem_wkt
            res = (self.parameterAsDouble(parameters, "CELL", context) if has("CELL")
                   else 10.0) / unit
        writer = gdal_io.GeoTiffWriter(work_srs, target, res)
        to_work = QgsCoordinateTransform(sample_point_crs, work, context.transformContext())
        return src, writer, to_work, work

    def apply_crossings(self, st, parameters, context, tf, center_xy, radius):
        """3.20.0 (feature 14): the crossings layer as plain dicts for core/gaps.py, in the work
        CRS. Never raises: a crossing that cannot be read is simply not in the list, and the
        run report says how many were read. Returns notes."""
        st.crossings = []
        st.crossing_gaps = bool(self.parameterAsBool(parameters, "CROSSING_GAPS", context)) \
            if self.parameterDefinition("CROSSING_GAPS") is not None else False
        if self.parameterDefinition("CROSSINGS") is None:
            return []
        src = self.parameterAsSource(parameters, "CROSSINGS", context)
        if src is None:
            return []
        names = qio.field_names(src)
        low = {n.lower(): n for n in names}
        fld = {}
        for key, syns in CROSSING_FIELD_SYNONYMS.items():
            chosen = self._str(parameters, key, context)
            fld[key] = chosen or next((low[s] for s in syns if s in low), "")
        a, b = tf(src)
        surveyed = 0
        for i, f in enumerate(qio._features(src, center_xy, radius, b)):
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            try:
                p = a.transform(g.centroid().asPoint())
            except Exception:                                    # noqa - one crossing
                continue
            at = lambda key: _attr_of(f, fld[key])
            ktxt = str(at("X_KIND") or "").lower()
            kind = (structmod.KIND_BRIDGE if "bridge" in ktxt else
                    structmod.KIND_CULVERT if ktxt else structmod.KIND_BRIDGE)
            shape_txt = str(at("X_SHAPE") or "").lower()
            span, rise = qio.num(at("X_SPAN")), qio.num(at("X_RISE"))
            if span and rise:
                surveyed += 1
            st.crossings.append({
                "name": str(at("X_NAME") or "crossing {}".format(i + 1)), "kind": kind,
                "x": p.x(), "y": p.y(), "span_ft": span, "rise_ft": rise,
                "barrels": int(qio.num(at("X_BARRELS"), 1) or 1),
                "shape": "pipe" if any(w in shape_txt for w in ("pipe", "round", "circ", "cmp"))
                else "box",
                "invert_up_ft": qio.num(at("X_INV_UP")), "invert_dn_ft": qio.num(at("X_INV_DN")),
                "source": "crossings layer"})
        if not st.crossings:
            return []
        used = ", ".join("{} = {}".format(k[2:].lower(), v) for k, v in sorted(fld.items()) if v)
        return ["Crossings within {:.0f} mi of the dam: {} ({} with a surveyed span and rise, "
                "so {} can be cut as gaps){}{}".format(
                    radius / MILE, len(st.crossings), surveyed,
                    surveyed if st.crossing_gaps else "none",
                    "" if st.crossing_gaps else "; 'cut the surveyed ones as gaps' is off, so "
                    "every crossing is only reported",
                    "; fields: " + used if used else "")]

    def apply_features(self, st, parameters, context, work, center_xy):
        """Read the flow-path layers around the dam into `st`. Returns notes."""
        if self.parameterDefinition("STRUCTURES") is None:
            return []
        notes = []
        radius = 0.75 * st.max_extent_mi * MILE
        tf = lambda src: (QgsCoordinateTransform(src.sourceCrs(), work, context.transformContext()),
                          QgsCoordinateTransform(work, src.sourceCrs(),
                                                 context.transformContext()))
        rec = impmod.Receptors()
        footprints = []                 # v3.4.0: building outlines from a polygon layer
        for name, kind in (("RECEPTOR_POINTS", impmod.KIND_STRUCTURE),
                           ("RECEPTOR_ROADS", impmod.KIND_ROAD),
                           ("RECEPTOR_RAIL", impmod.KIND_RAIL)):
            src = self.parameterAsSource(parameters, name, context)
            if src is None:
                continue
            a, b = tf(src)
            if kind == impmod.KIND_STRUCTURE:
                xy, ids, footprints, gc = qio.points_and_footprints(src, a, b, center_xy, radius)
                rec.add_points(xy, kind, ids)
                if gc["polygons"]:
                    notes.append("Structures layer: {} polygon footprint(s) (centroids are the "
                                 "receptor points) and {} point(s)".format(gc["polygons"],
                                                                           gc["points"]))
            else:
                ln = qio.lines(src, a, b, center_xy, radius)
                rec.add_lines([x[0] for x in ln], kind)
                if kind == impmod.KIND_ROAD:                 # 3.13.0: culvert candidates
                    st.road_lines = [x[0] for x in ln]
        for name in ("RECEPTOR_POINTS", "RECEPTOR_ROADS", "RECEPTOR_RAIL", "STRUCTURES",
                     "CROSSINGS", "CHANNEL_LINES", "PONDS", "TAILWATER", "CREST_LINE",
                     "REFINE_LINES", "GAUGE_POINTS"):
            self._record("Layer " + name, self._vector_path(parameters, name, context))
        # 3.13.0: a structures / culverts layer that misses the study area stops the run here,
        # and says whether the layer has no CRS or simply covers somewhere else
        box = (center_xy[0] - radius, center_xy[1] - radius, center_xy[0] + radius,
               center_xy[1] + radius)
        gate = []
        for name, title in (("RECEPTOR_POINTS", "Structures layer"),
                            ("STRUCTURES", "Culverts, bridges and notches layer")):
            src = self.parameterAsSource(parameters, name, context)
            if src is not None:
                gate.append(_layer_overlap(src, title, work, box, context))
        for line in SG.lines(gate):
            notes.append("Pre-run check: " + line)
        if SG.failures(gate):
            raise QgsProcessingException(SG.message(gate))
        st.receptors = rec if len(rec) else None
        if st.receptors is not None:
            notes.append("Receptors within {:.0f} mi of the dam: {}".format(
                radius / MILE, impmod.count_text(rec, np.ones(len(rec), bool))))
        # structures
        src = self.parameterAsSource(parameters, "STRUCTURES", context)
        structs = []
        if src is not None:
            names = qio.field_names(src)
            fld = {}
            for key, syns in STRUCT_FIELD_SYNONYMS.items():
                chosen = self._str(parameters, key, context)
                if not chosen:
                    low = {n.lower(): n for n in names}
                    chosen = next((low[s] for s in syns if s in low), "")
                fld[key] = chosen
            dk = STRUCT_KINDS[self.parameterAsEnum(parameters, "S_DEFAULT_KIND", context)][0]
            dw = self.parameterAsDouble(parameters, "S_DEFAULT_WIDTH_FT", context)
            dh = self.parameterAsDouble(parameters, "S_DEFAULT_HEIGHT_FT", context)
            a, b = tf(src)
            for i, (line, at) in enumerate(qio.lines(src, a, b, center_xy, radius,
                                                    fields=list(fld.values()))):
                ktxt = str(at.get(fld["S_KIND"]) or "").lower()
                kind = (structmod.KIND_BRIDGE if "bridge" in ktxt else
                        structmod.KIND_NOTCH if any(w in ktxt for w in ("notch", "breach", "open"))
                        else structmod.KIND_CULVERT if ktxt else dk)
                shape_txt = str(at.get(fld["S_SHAPE"]) or "").lower()
                shape = "pipe" if any(w in shape_txt for w in ("pipe", "round", "circ", "cmp")) \
                    else "box"
                w_ft = qio.num(at.get(fld["S_WIDTH"]), dw)
                h_ft = w_ft if shape == "pipe" else qio.num(at.get(fld["S_HEIGHT"]), dh)
                inv_u = qio.num(at.get(fld["S_INV_UP"]))
                inv_d = qio.num(at.get(fld["S_INV_DN"]))
                structs.append(structmod.Structure(
                    kind, line, w_ft * FT, h_ft * FT,
                    int(qio.num(at.get(fld["S_BARRELS"]), 1) or 1), shape,
                    inv_u * FT if inv_u is not None else None,
                    inv_d * FT if inv_d is not None else None,
                    name=str(at.get(fld["S_NAME"]) or "{} {}".format(kind, i + 1))))
        st.structures = structs
        notes += self.apply_crossings(st, parameters, context, tf, center_xy, radius)
        try:
            st.invert_tol_m = float(self.parameterAsDouble(parameters, "S_INVERT_TOL_FT", context)) * FT
        except Exception:                                        # noqa
            st.invert_tol_m = 3.0 * FT
        # DEM conditioning
        ed = condmod.Edits()
        src = self.parameterAsSource(parameters, "CHANNEL_LINES", context)
        if src is not None:
            a, b = tf(src)
            w = self.parameterAsDouble(parameters, "CH_WIDTH_FT", context) * FT
            d = self.parameterAsDouble(parameters, "CH_DEPTH_FT", context) * FT
            for i, (line, _at) in enumerate(qio.lines(src, a, b, center_xy, radius)):
                ed.channels.append(condmod.ChannelBurn(line, w, d, name="channel {}".format(i + 1)))
        src = self.parameterAsSource(parameters, "PONDS", context)
        if src is not None:
            a, b = tf(src)
            d = self.parameterAsDouble(parameters, "POND_DEPTH_FT", context) * FT
            for i, (poly, _at) in enumerate(qio.polygons(src, a, b, center_xy, radius)):
                ed.ponds.append(condmod.PondLower(poly, d, name="pond {}".format(i + 1)))
        src = self.parameterAsSource(parameters, "TAILWATER", context)
        if src is not None:
            a, b = tf(src)
            stage = self._opt(parameters, "TW_STAGE_FT", context)
            sfld = self._str(parameters, "TW_STAGE_FIELD", context)
            for i, (poly, at) in enumerate(qio.polygons(src, a, b, center_xy, radius,
                                                        fields=[sfld] if sfld else ())):
                v = qio.num(at.get(sfld)) if sfld else None
                v = v if v is not None else stage
                if v is None:
                    raise QgsProcessingException(
                        "Tailwater polygon {} has no stage: give the stage number or a stage "
                        "field".format(i + 1))
                ed.stages.append(condmod.TailwaterStage(poly, v * FT, None,
                                                        "tailwater {}".format(i + 1)))
        # v3.4.0: buildings as obstructions (polygon footprints only)
        if self.parameterDefinition("BUILDINGS") is not None:
            mode = condmod.BUILDINGS_MODES[min(len(condmod.BUILDINGS_MODES) - 1,
                                               self.parameterAsEnum(parameters, "BUILDINGS",
                                                                    context))]
            if mode != condmod.BUILDINGS_NONE:
                if footprints:
                    ed.buildings = condmod.building_edits(
                        footprints, mode,
                        self.parameterAsDouble(parameters, "BUILDING_RAISE_FT", context),
                        self.parameterAsDouble(parameters, "BUILDING_N", context))
                    notes.append(condmod.describe_buildings(ed))
                else:
                    notes.append("Buildings: '{}' was chosen but the structures layer has no "
                                 "polygon footprints (points cannot be used for this); the "
                                 "model keeps bare terrain.".format(condmod.BUILDINGS_LABELS[mode]))
        st.edits = None if ed.empty() else ed
        st.breach_xy = None
        st.crest_line_xy = None
        src = self.parameterAsSource(parameters, "CREST_LINE", context)
        if src is not None:                       # v3.11.0: the line that is raised
            a, b = tf(src)
            # 3.13.0: the DWR dam line of this dam (structure id), else the nearest within
            # CREST_SNAP_M; the dam point is moved onto it (the line is the crest, an
            # inventory point often is not). The DWR dam lines layer fills CREST_LINE from the
            # dam folder ("Get data for this dam") when it is left empty.
            id_field = next((n for n in qio.field_names(src)
                             if n.lower() in ("structure_id", "structure_", "structure")), "")
            ids = getattr(self, "_crest_ids", None) or set()
            self._crest_ids = None
            clines = qio.lines(src, a, b, center_xy, radius, fields=[id_field] if id_field else ())
            best, dist, how = _pick_crest_line(clines, center_xy, ids, id_field)
            # 3.21.0: the breach position, when given, is measured along the line _pick_crest_line
            # chose (the DWR-matched line, or the nearest within CREST_SNAP_M), never along the
            # nearest line in the whole search box. BREACH_POSITION is now optional with no
            # default: empty means put the breach where the dam point meets that line. Before,
            # a 0.5 default made the position path always run and place the breach half way along
            # the nearest line of ANY dam in the 45 mi box, ignoring the match and the 500 m cap.
            pos = self._opt(parameters, "BREACH_POSITION", context)
            if best is not None:
                st.crest_line_xy = [(float(x), float(y)) for x, y in best]
                if pos is not None:
                    st.breach_xy = _point_along_nearest([best], center_xy, pos)
                    notes.append("Breach starts at {:.0f}% along the dam crest line ({}).".format(
                        100 * pos, how))
                elif dist > 1.0:
                    d, q = _nearest_on_line(best, center_xy)
                    st.breach_xy = q
                    notes.append("Dam point moved {:.0f} m onto the dam crest line ({}).".format(
                        d, how))
                else:
                    notes.append("Dam crest line: {} ({:.0f} m from the dam point).".format(
                        how, dist))
            elif clines:
                notes.append("Dam crest line not used: {}.".format(how))
        src = self.parameterAsSource(parameters, "REFINE_LINES", context)
        st.refine_lines = []
        if src is not None:
            a, b = tf(src)
            st.refine_lines = [x[0] for x in qio.lines(src, a, b, center_xy, radius)]
        src = self.parameterAsSource(parameters, "GAUGE_POINTS", context)
        st.gauges = []
        if src is not None:
            gname = self._str(parameters, "G_NAME", context)
            a, b = tf(src)
            xy, ids = qio.points(src, a, b, center_xy, radius, id_field=gname or None)
            st.gauges = [{"name": ids[i] if gname else "gauge {}".format(i + 1),
                          "xy": (float(xy[i][0]), float(xy[i][1]))} for i in range(len(xy))]
        if st.structures:
            notes.append("{} culvert/bridge/notch structure(s) read".format(len(st.structures)))
        return notes

    def load(self, context, path, name):
        if path and os.path.exists(path):
            context.addLayerToLoadOnCompletion(
                path, QgsProcessingContext.LayerDetails(name, context.project(), name))
            # 3.9.0: styled like the popup's "Add the dam's results" (styles/*.qml, by file
            # name) through a post-processor when QGIS loads it; nothing when no style fits
            try:
                from . import suite_styles
                suite_styles.attach(context, path, path_hint=path)
            except Exception:                    # noqa - a style never fails a run
                pass

    def point_info(self, pt, crs, work, dam_xy, context):
        """{"lon", "lat", "x", "y", "crs"} of the dam point for dam.json. x / y / crs are left
        out when the record keeps its point in another CRS (they would not compare)."""
        info = {}
        try:
            geo = QgsCoordinateTransform(crs, QgsCoordinateReferenceSystem("EPSG:4326"),
                                         context.transformContext()).transform(QgsPointXY(pt))
            if -180.0 <= geo.x() <= 180.0 and -90.0 <= geo.y() <= 90.0:
                info.update({"lon": round(geo.x(), 7), "lat": round(geo.y(), 7)})
        except Exception:
            pass
        rec = getattr(self, "_dam_rec", None)
        wid = work.authid() if work is not None else ""
        if wid and (rec is None or not rec.get("crs") or str(rec.get("crs")) == wid):
            info.update({"x": round(float(dam_xy[0]), 2), "y": round(float(dam_xy[1]), 2),
                         "crs": wid})
        return info

    def load_results(self, context, res, dam_id):
        names = {"depth": "Depth (Max)", "velocity": "Velocity (Max)",
                 "arrival": "Arrival Time", "wse": "WSE (Max)"}
        for k, p in res.final.items():
            self.load(context, p, "{} {}".format(dam_id, names[k]))
        if res.increment.get("depth"):
            self.load(context, res.increment["depth"], "{} Incremental Depth (Max)".format(dam_id))
        for k in ("depth_polygons", "arrival_zones"):
            self.load(context, res.vectors.get(k), "{} {}".format(dam_id, k.replace("_", " ")))


# ============================================================================
class RunDamAlgorithm(_Base):
    def name(self):
        return "rundam"

    def displayName(self):
        return "Run breach model for one dam"

    def shortHelpString(self):
        return ("Dam breach flood for one dam per the North Dakota Dam Safety Standards Policy "
                "(REG_05.2026, Section 5.1). Sunny day: starting at the top of dam or the PMF "
                "maximum level if lower, no inflow. Flood: the PMF inflow is routed through the "
                "reservoir with and without the breach and the flood is modelled both ways, "
                "giving the incremental depth and WSE rise (Section 6.4). The breach comes "
                "from Froehlich (2008 or 1995), MacDonald & Langridge-Monopolis, Von Thun & "
                "Gillette, TR-60 or your CSV. LISFLOOD-FP runs on the DEM and the domain grows "
                "until the flood no longer leaves it deeper than the tolerance, or until no "
                "structure, road or rail lies in its path if you give those layers; ANUGA "
                "can then re-run it on a mesh refined near the dam.\n\n"
                "Results go to <Dams folder>/<Dam ID>/ as 'Depth (Max).tif' (ft), "
                "'Velocity (Max).tif' (ft/s), 'Arrival Time.tif' (hours after breach "
                "initiation) and 'WSE (Max).tif' (ft), ready for the ND hazard classification, "
                "with 'Baseline ...' and 'Incremental ...' rasters for the flood scenario, flood "
                "polygons, arrival zones, a GeoPackage and an EAP inundation map preview (the "
                "EAP map is ND Dam Hazard's map book; see 'EAP map'). The run report "
                "(Markdown, JSON and HTML) is in the _breach_model subfolder.\n\n"
                "dam.json (v3.2.0): a height, storage, surface area, top of dam, name, NID ID "
                "or dam point left empty is taken from <Dams folder>/<Dam ID>/dam.json (the "
                "log says which, set by which tool and when); after the run the values used "
                "are written there (tool 'ndbreach') with a 'modelled' status. Another tool's "
                "value is never overwritten: a difference is listed as a warning in the run "
                "report with both values.\n\n"
                "Class-stop rule (advanced, off by default): an edge the flood leaves deeper "
                "than the tolerance is accepted when the breach flood's depth (or its rise "
                "over the no-breach baseline) stays below the threshold along the flow path "
                "near it, as ND Dam Hazard's termination point, and no receptor lies beyond "
                "it; each decision is in the run report.\n\n"
                "v3.4.0 (engineer's screening values; 'Near-dam refine' was on by default in "
                "3.8.0 - 3.11.x and is off again from 3.12.0, at half the model cell; "
                "'Buildings' is off): 'Near-dam refine' "
                "re-runs the first stretch below the breach on a fine grid with LISFLOOD-FP's "
                "FV1 solver when a structure, road, rail or crossing lies within the flagged "
                "distance and the final rasters come from the ACC run (its velocities there are "
                "unreliable), and merges the fine results in as the nested tool does; the run "
                "report, manifest and log say what was done. 'Buildings' raises the DEM or "
                "sets Manning's n inside polygon footprints of the structures layer (points "
                "cannot be used). Neither has been run against a real LISFLOOD-FP or in a real "
                "QGIS.")

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterString("DAM_ID", "Dam ID (NID ID)"))
        self._text("DAM_NAME", "Dam name (for the map title; empty = from dam.json)",
                   optional=True)
        self._text("NID_ID", "NID ID, when it differs from the dam ID (empty = from dam.json)",
                   optional=True, advanced=True)
        self.addParameter(QgsProcessingParameterPoint(
            "DAM_POINT", "Dam (point on the crest; empty = from the dam folder's dam.json)",
            optional=True))
        self._num("HEIGHT_FT", "Dam height (ft; empty = from dam.json)", optional=True,
                  minimum=0.1)
        self._num("STORAGE_ACFT", "Storage at the top of dam (ac-ft; empty = from dam.json)",
                  optional=True, minimum=0.01)
        self._num("TOP_FT", "Top of dam elevation (ft, DEM datum; empty = from dam.json, else "
                  "read from the DEM near the dam point)", optional=True)
        self._num("AREA_AC", "Surface area at the top of dam (ac; empty = from dam.json)",
                  optional=True, minimum=0)
        self._num("PMF_FT", "Maximum reservoir level in the PMF (ft; used if lower than the "
                  "top of dam)", optional=True)
        self._num("BOTTOM_FT", "Breach bottom elevation (ft; empty = top of dam - height)",
                  optional=True)
        self.addParameter(QgsProcessingParameterPoint(
            "INFLOW_POINT", "Breach inflow point below the dam (empty = found from the DEM)",
            optional=True))
        self.add_breach_params(per_dam=True)
        self.add_scenario_params(per_dam=True)
        self.add_feature_params()
        self.add_model_params()
        self.addOutput(QgsProcessingOutputRasterLayer("DEPTH", "Depth (Max)"))
        self.addOutput(QgsProcessingOutputRasterLayer("VELOCITY", "Velocity (Max)"))
        self.addOutput(QgsProcessingOutputRasterLayer("ARRIVAL", "Arrival Time"))
        self.addOutput(QgsProcessingOutputRasterLayer("WSE", "WSE (Max)"))
        self.addOutput(QgsProcessingOutputFile("REPORT", "Run report"))
        self.addOutput(QgsProcessingOutputFile("REPORT_HTML", "Run report (HTML)"))
        self.addOutput(QgsProcessingOutputFile("EAP_MAP", "EAP inundation map"))
        self.addOutput(QgsProcessingOutputFolder("FOLDER", "Dam folder"))

    def dam_folder(self, parameters, context, dam_id):
        parent = self._fileparam(parameters, "DAMS_FOLDER", context) \
            if self.parameterDefinition("DAMS_FOLDER") is not None else ""
        return os.path.join(parent, _safe_name(dam_id)) if parent else ""

    def dam_from_params(self, parameters, context):
        """The dam from the parameters; v3.2.0: an empty height, storage, surface area, top of
        dam, name or NID ID is taken from <Dams folder>/<Dam ID>/dam.json (logged). Leaves
        self._dam_rec (the record or None) and self._rec_info (for st.dam_record)."""
        o = lambda n: self._opt(parameters, n, context)
        dam_id = self.parameterAsString(parameters, "DAM_ID", context).strip()
        if not dam_id:
            raise QgsProcessingException("Dam ID is required")
        rec, problem = damjson.load(self.dam_folder(parameters, context, dam_id))
        vals = {"HEIGHT_FT": o("HEIGHT_FT"), "STORAGE_ACFT": o("STORAGE_ACFT"),
                "AREA_AC": o("AREA_AC"), "TOP_FT": o("TOP_FT"),
                "DAM_NAME": self._str(parameters, "DAM_NAME", context) or None,
                "NID_ID": (self._str(parameters, "NID_ID", context) or None)
                if self.parameterDefinition("NID_ID") is not None else None}
        vals, notes, used = damjson.fill(vals, rec)
        crest_len = o("CREST_LENGTH")
        if crest_len in (None, "") and rec is not None and rec.get("dam_length_ft"):
            crest_len = rec.get("dam_length_ft")
            notes.append("Crest length (ft) from dam.json ({}): {}".format(
                damjson.source_text(rec, "dam_length_ft"), crest_len))
        if problem:
            notes.insert(0, problem)
        self._dam_rec = rec
        self._rec_info = {"read": notes, "nid_id": vals.get("NID_ID"),
                          "top_source": "record" if "TOP_FT" in used else "parameter"}
        for key, label in (("HEIGHT_FT", "Dam height"), ("STORAGE_ACFT", "Storage at the top "
                                                                         "of dam")):
            if vals.get(key) is None:
                raise QgsProcessingException(
                    "{} is required: give it, or record it in the dam folder's dam.json "
                    "({}).".format(label, "none found" if rec is None else
                                   "it has no value for it"))
        return brmod.DamData(
            dam_id=dam_id, dam_height_ft=vals["HEIGHT_FT"], storage_top_acft=vals["STORAGE_ACFT"],
            top_of_dam_ft=vals["TOP_FT"], surface_area_top_ac=vals["AREA_AC"],
            pmf_max_wse_ft=o("PMF_FT"), breach_bottom_ft=o("BOTTOM_FT"),
            crest_width_ft=o("CREST_WIDTH"), us_slope_h=o("US_SLOPE"), ds_slope_h=o("DS_SLOPE"),
            valley_width_ft=o("VALLEY_WIDTH"), crest_length_ft=crest_len,
            name=vals.get("DAM_NAME") or "")

    def dam_point(self, parameters, context, crs):
        """The dam point in `crs`: the parameter, else dam.json's point (v3.2.0)."""
        if parameters.get("DAM_POINT"):
            return self.parameterAsPoint(parameters, "DAM_POINT", context, crs)
        got, note = damjson.point_from_record(getattr(self, "_dam_rec", None))
        if got is None:
            raise QgsProcessingException("Give the dam point (on the crest): it is empty and "
                                         "the dam folder's dam.json has no location.")
        _kind, x, y, rcrs = got
        tf = QgsCoordinateTransform(QgsCoordinateReferenceSystem(rcrs), crs,
                                    context.transformContext())
        self.__dict__.setdefault("_rec_info", {}).setdefault("read", []).append(note)
        return tf.transform(QgsPointXY(x, y))

    def prepare(self, parameters, context, feedback):
        """(dam, st, src, writer, dam_xy, inflow_xy, folder) for one dam."""
        dam = self.dam_from_params(parameters, context)
        st = self.run_settings(parameters, context)
        dem = self.parameterAsRasterLayer(parameters, "DEM", context)
        pt = self.dam_point(parameters, context, dem.crs())
        src, writer, to_work, work = self.gis(parameters, context, dem.crs(), pt)
        dam_xy = to_work.transform(QgsPointXY(pt))
        info = self.__dict__.setdefault("_rec_info", {})
        info["point"] = self.point_info(pt, dem.crs(), work, (dam_xy.x(), dam_xy.y()), context)
        for n in info.get("read") or []:
            feedback.pushInfo(n)
        inflow = None
        if parameters.get("INFLOW_POINT"):
            ip = self.parameterAsPoint(parameters, "INFLOW_POINT", context, dem.crs())
            q = to_work.transform(QgsPointXY(ip))
            inflow = (q.x(), q.y())
        folder = os.path.join(self.parameterAsFile(parameters, "DAMS_FOLDER", context),
                              _safe_name(dam.dam_id))
        feedback.pushInfo("Model CRS {}; DEM elevations in {}".format(work.authid(), src.z_note))
        self._crest_ids = _dwr_ids(dam.dam_id, getattr(self, "_dam_rec", None))   # 3.13.0
        for n in self.apply_features(st, parameters, context, work, (dam_xy.x(), dam_xy.y())):
            feedback.pushInfo(n)
        try:
            if st.scenario == workflow.SCENARIO_FLOOD:
                st.flood = self.flood_inputs(parameters, context, dam,
                                             nd_data.dam_input_files(folder))
        except brmod.BreachInputError as e:
            raise QgsProcessingException(str(e))
        return dam, st, src, writer, (dam_xy.x(), dam_xy.y()), inflow, folder

    def processAlgorithm(self, parameters, context, feedback):
        dam, st, src, writer, dam_xy, inflow, folder = self.prepare(parameters, context,
                                                                    feedback)
        st.dam_record = dict(getattr(self, "_rec_info", None) or {})   # v3.2.0: dam.json
        fb = _Feedback(feedback)
        try:
            res = workflow.run_dam(dam, dam_xy, st, src, writer, folder, inflow,
                                   fb.progress, fb.log, fb.cancel)
        except Cancelled:
            raise QgsProcessingException("Cancelled")
        except (ModelError, brmod.BreachInputError) as e:
            raise QgsProcessingException(str(e))
        for k, v in res.summary.items():
            feedback.pushInfo("{}: {}".format(k, v))
        if res.warnings:
            feedback.pushInfo("--- Things to check ---")
            for w in res.warnings:
                feedback.pushWarning("Check: " + w)
        self.load_results(context, res, dam.dam_id)
        return {"DEPTH": res.final.get("depth"), "VELOCITY": res.final.get("velocity"),
                "ARRIVAL": res.final.get("arrival"), "WSE": res.final.get("wse"),
                "REPORT": res.report_md, "REPORT_HTML": res.report_html,
                "EAP_MAP": res.eap_map, "FOLDER": folder}


# ============================================================================
class SensitivityAlgorithm(RunDamAlgorithm):
    def name(self):
        return "sensitivity"

    def displayName(self):
        return "Sensitivity of the breach model for one dam"

    def shortHelpString(self):
        return ("Runs the base breach model and then variations of the breach width, formation "
                "time and Manning's n, and the other breach parameter methods, all on the same "
                "domain (LISFLOOD-FP only). Writes low / median / high depth envelope rasters, "
                "the fastest water and the earliest arrival over all cases, a per-case table, "
                "and says whether the spread changes what is flooded (with receptor layers) or "
                "the hazard class (with the ND hazard classification plugin installed).\n\n"
                "It takes as many runs as there are cases, so use a coarser cell size for a "
                "first look.\n\n"
                "Cell-size convergence check (v3.4.0, on by default): the base case runs again "
                "at half the cell size on the same domain (or at the next coarser factor that "
                "fits the cell budget, and the report says so). The table gets the fit index F "
                "between the two depth rasters (on the coarse grid), the class of the Runner's "
                "own ND 5.2 preview for every run, the deepest and fastest water at the "
                "receptors, and 'class_stable_grid: yes/no'. The preview has no structure "
                "attributes and can over-classify; the official classification is ND DWR's. "
                "Nothing here has been run in a real QGIS or against a real LISFLOOD-FP.")

    def initAlgorithm(self, config=None):
        super().initAlgorithm(config)
        self._text("SENS_WIDTH", "Breach width factors", "0.7, 1.4")
        self._text("SENS_TIME", "Formation time factors", "0.5, 2")
        self._text("SENS_N", "Manning's n factors", "0.75, 1.3")
        self._bool("SENS_METHODS", "Also run the other parameter methods (Froehlich 2008 "
                   "overtopping, Froehlich 1995, MacDonald, Von Thun & Gillette, TR-60)", True)
        self._bool("SENS_CELL", "Cell-size convergence check: run the base case again at half "
                   "the cell size on the same domain and report the fit index between the two "
                   "depth rasters and whether the Runner's ND 5.2 preview class holds "
                   "(class_stable_grid)", True)
        self._num("SENS_MAX_CELLS", "Cell-size check: most cells in the finer run (the budget; "
                  "when half the cell size needs more, the next coarser factor that fits is "
                  "run and the report says so; starting value {:,} is the engineer's)".format(
                      sensmod.DEFAULT_MAX_CELLS),
                  sensmod.DEFAULT_MAX_CELLS, minimum=1000, integer=True, advanced=True)
        self._num("SENS_MAX", "Most cases (0 = all)", 0, minimum=0, integer=True, advanced=True)
        self._bool("CLASSIFY", "Classify every case with the ND hazard classification plugin",
                   False, advanced=True)
        self._text("CLASS_ALG", "Classification algorithm ID (empty = ndhazard:classify)",
                   advanced=True)
        self._text("CLASS_MAP", "Classification parameters, JSON (see the README)",
                   advanced=True)
        self.addOutput(QgsProcessingOutputFile("SUMMARY", "Sensitivity_Summary.csv"))
        self.addOutput(QgsProcessingOutputFile("SENS_REPORT", "Sensitivity report (HTML)"))

    def processAlgorithm(self, parameters, context, feedback):
        dam, st, src, writer, dam_xy, inflow, folder = self.prepare(parameters, context,
                                                                    feedback)
        st.tier = workflow.TIER_LISFLOOD
        has_geom = bool(dam.crest_width_ft and dam.us_slope_h and dam.ds_slope_h)
        cases = sensmod.default_cases(
            st.breach_method, _floats(self._str(parameters, "SENS_WIDTH", context), ()),
            _floats(self._str(parameters, "SENS_TIME", context), ()),
            _floats(self._str(parameters, "SENS_N", context), ()),
            self.parameterAsBool(parameters, "SENS_METHODS", context), has_geom)
        classify = None
        if self.parameterAsBool(parameters, "CLASSIFY", context):
            try:
                classify = handoff.make_classifier(
                    self._str(parameters, "CLASS_ALG", context),
                    handoff.load_mapping(self._str(parameters, "CLASS_MAP", context)),
                    feedback)
            except handoff.HandoffError as e:
                feedback.reportError("Classification skipped: {}".format(e), fatalError=False)
        fb = _Feedback(feedback)
        try:
            out = sensmod.run_sensitivity(
                dam, dam_xy, st, src, writer, folder, inflow, cases, fb.progress, fb.log,
                fb.cancel, classify, int(self.parameterAsInt(parameters, "SENS_MAX", context))
                or None,
                cell_check=self.parameterAsBool(parameters, "SENS_CELL", context),
                max_cells=int(self.parameterAsInt(parameters, "SENS_MAX_CELLS", context))
                or sensmod.DEFAULT_MAX_CELLS)
        except Cancelled:
            raise QgsProcessingException("Cancelled")
        except (ModelError, brmod.BreachInputError) as e:
            raise QgsProcessingException(str(e))
        for w in out.warnings:
            feedback.pushWarning("Check: " + w)
        for f in out.flags:
            feedback.pushInfo(f)
        for n, p in out.envelope.items():
            self.load(context, p, "{} {}".format(dam.dam_id, n[:-4]))
        return {"SUMMARY": out.summary_csv, "SENS_REPORT": out.report_html, "FOLDER": folder,
                "REPORT": out.base.report_md if out.base else "",
                "REPORT_HTML": out.base.report_html if out.base else "", "DEPTH": "",
                "VELOCITY": "", "ARRIVAL": "", "WSE": "", "EAP_MAP": ""}


# ============================================================================
BATCH_FIELDS = ("Dam", "Folder", "Status", "Scenario", "Final_Model", "Peak_Breach_cfs",
                "Storage_At_Failure_acft", "Flooded_ac", "Max_Depth_ft", "Max_Velocity_fps",
                "Latest_Arrival_hr", "Receptors_Flooded", "Runs", "Domain_mi", "Minutes",
                "Warnings", "Message")
BATCH_SCENARIOS = [SCENARIOS[0][1], SCENARIOS[1][1],
                   "Per dam: flood if its folder has inputs/pmf_inflow.csv, else sunny day"]
BATCH_FIELD_KEYS = (("F_ID", "Dam ID field", False), ("F_NAME", "Dam name field", False),
                    ("F_HEIGHT", "Dam height field (ft)", True),
                    ("F_STORAGE", "Storage at the top of dam field (ac-ft)", True),
                    ("F_TOP", "Top of dam elevation field (ft)", True),
                    ("F_AREA", "Surface area field (ac)", True),
                    ("F_PMF", "PMF maximum reservoir level field (ft)", True),
                    ("F_BOTTOM", "Breach bottom elevation field (ft)", True),
                    ("F_CREST", "Crest width field (ft)", True),
                    ("F_US", "Upstream slope field (H)", True),
                    ("F_DS", "Downstream slope field (H)", True),
                    ("F_LENGTH", "Crest length field (ft)", True),
                    ("F_POOL", "Normal pool elevation field (ft), flood scenario", True))


def _apply_run_result(row, res, dam_id, feedback):
    """Update a batch row dict from a workflow.RunResult (thread-safe: pure dict ops)."""
    s = res.summary
    if not s.get("breach_occurs", True):
        row.update({"Status": "no breach", "Message": "the dam does not fail in this flood"})
    else:
        row.update({"Status": "done", "Final_Model": s["final_model"],
                    "Peak_Breach_cfs": s["peak_breach_cfs"],
                    "Storage_At_Failure_acft": s["storage_at_failure_acft"],
                    "Flooded_ac": s["flooded_acres"],
                    "Max_Depth_ft": s["max_depth_ft"],
                    "Max_Velocity_fps": s["max_velocity_fps"],
                    "Latest_Arrival_hr": s["latest_arrival_hr"],
                    "Receptors_Flooded": s.get("receptors_flooded", ""),
                    "Runs": s["lisflood_runs"],
                    "Domain_mi": "{} x {}".format(*s["domain_mi"]),
                    "Warnings": " | ".join(res.warnings)})
    if res.warnings:
        feedback.pushInfo("{}: --- Things to check ---".format(dam_id))
        for w in res.warnings:
            feedback.pushWarning("{}: {}".format(dam_id, w))
    return row


class BatchAlgorithm(_Base):
    def name(self):
        return "batch"

    def displayName(self):
        return "Run breach models for a layer of dams (batch)"

    def shortHelpString(self):
        return ("Runs 'Run breach model for one dam' for every dam point in a layer (or the "
                "selected ones), taking the dam values from its fields. Fields you leave empty "
                "are matched by name (NID / ND inventory names such as NIDID, DAM_HEIGHT, "
                "MAX_STORAGE); the log lists what was matched. Each dam gets "
                "<Dams folder>/<Dam ID>/ with the rasters the ND hazard classification's batch "
                "tool looks for, and Batch_Breach_Runs.csv lists every dam.\n\n"
                "For the flood scenario put each dam's inputs in <Dams folder>/<Dam ID>/inputs/: "
                "pmf_inflow.csv, spillway_rating.csv (optional), baseline_outflow.csv and "
                "breach_hydrograph.csv (own-hydrograph method only). A dam that fails is "
                "reported and skipped. With 'classify afterwards' the ND hazard batch "
                "classification is run on the dams folder and its result is merged into "
                "Batch_Breach_Runs.csv.")

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource("DAMS", "Dams (points)",
                                                              [SOURCE_POINT]))
        for key, desc, num in BATCH_FIELD_KEYS:
            self._field(key, desc + " (empty = match by name)", "DAMS", numeric=num,
                        optional=True)
        self.addParameter(QgsProcessingParameterEnum(
            "METHOD", "Breach (ND 5.1.1(d))", [m[1] for m in METHODS], defaultValue=0))
        self.addParameter(QgsProcessingParameterEnum(
            "SCENARIO", "Scenario", BATCH_SCENARIOS, defaultValue=0))
        self._file("INFLOW_CSV", "Flood scenario: PMF inflow CSV for dams without their own "
                   "inputs/pmf_inflow.csv", "csv")
        self._file("SPILLWAY_CSV", "Flood scenario: spillway rating CSV for dams without their "
                   "own", "csv")
        self.addParameter(QgsProcessingParameterEnum(
            "TRIGGER", "Flood scenario: the dam fails", [t[1] for t in TRIGGERS],
            defaultValue=0))
        self._num("TRIGGER_VALUE", "Flood scenario: failure time (hr) or pool elevation (ft)",
                  optional=True)
        self._bool("BASELINE", "Flood scenario: also run without the breach", True)
        self._text("INCR_FT", "Incremental depth thresholds (ft; starting values 1, 2, 3 are "
                   "the engineer's)", "1, 2, 3", advanced=True)
        self.add_feature_params()
        # item 7: preset; when not Custom, overrides cell size and sim times from add_model_params
        self.addParameter(QgsProcessingParameterEnum(
            "PRESET", "Resolution preset",
            ["Quick screen (50 m grid, ~12 s/dam)", "Standard (20 m, ~2 min/dam)",
             "Detailed (10 m, ~15 min/dam)", "Custom (use cell and time settings below)"],
            defaultValue=0))
        self.add_model_params()
        self._bool("SKIP_DONE", "Skip dams whose folder already holds a finished run", True)
        # 3.20.0 (feature 16): two dams side by side at one thread each is the default. It
        # measured 1.76x faster than one dam at two threads, and it cannot oversubscribe the
        # machine the way "one per core" did on a small one. Nothing about a result changes.
        self._num("WORKERS", "Dams to run at the same time (0 = one per CPU core; the starting "
                  "value 2, one thread each, measured 1.76x faster than one dam on two "
                  "threads)", batchqueue.DEFAULT_WORKERS, minimum=0, integer=True,
                  advanced=True)
        self._bool("CLASSIFY", "Classify afterwards with the ND hazard plugin's batch tool "
                   "and merge the result", False)
        self._text("CLASS_ALG", "Classification batch algorithm ID (empty = ndhazard:batch)",
                   advanced=True)
        self._text("CLASS_MAP", "Classification parameters, JSON (see the README)",
                   advanced=True)
        self.addOutput(QgsProcessingOutputFile("SUMMARY", "Batch_Breach_Runs.csv"))
        self.addOutput(QgsProcessingOutputString("COUNTS", "Counts"))

    def _mapping(self, parameters, context, src_layer, feedback):
        names = qio.field_names(src_layer)
        auto, how = nd_data.suggest_fields(names)
        fld, used = {}, []
        for key, _d, _n in BATCH_FIELD_KEYS:
            chosen = self._str(parameters, key, context)
            if chosen:
                fld[key] = chosen
            elif key in auto:
                fld[key] = auto[key]
                used.append(key)
            else:
                fld[key] = ""
        if used:
            feedback.pushInfo("Fields matched by name: " + "; ".join(
                "{} -> {}".format(k, fld[k]) for k in used))
        return fld

    def processAlgorithm(self, parameters, context, feedback):
        src_layer = self.parameterAsSource(parameters, "DAMS", context)
        if src_layer is None:
            raise QgsProcessingException("No dams layer")
        base_st = self.run_settings(parameters, context,
                                    METHODS[self.parameterAsEnum(parameters, "METHOD",
                                                                 context)][0])
        # item 7: preset overrides cell size and sim time
        _preset_vals = [(50.0, 12.0, 96.0), (20.0, 24.0, 96.0), (10.0, 36.0, 144.0)]
        _pi = self.parameterAsEnum(parameters, "PRESET", context) if self.parameterDefinition("PRESET") is not None else 3
        if _pi < len(_preset_vals):
            _cell_m, _sim_time_hr, _sim_max_hr = _preset_vals[_pi]
            base_st.cell_size_m = _cell_m
            base_st.planning_cell_m = max(30.0, _cell_m)
            base_st.sim_time_hr = None  # automatic; sim_max_hr caps it
            base_st.sim_max_hr = _sim_max_hr
            feedback.pushInfo("preset: {} (cell {:.0f} m, sim_max {:.0f} h)".format(
                ["Quick", "Standard", "Detailed"][_pi], _cell_m, _sim_max_hr))
        scen_i = self.parameterAsEnum(parameters, "SCENARIO", context)
        fld = self._mapping(parameters, context, src_layer, feedback)
        if not fld.get("F_ID"):
            raise QgsProcessingException(
                "No field was found for '{}'; choose it in the parameters.".format(
                    dict((k, d) for k, d, _n in BATCH_FIELD_KEYS)["F_ID"]))
        for need in ("F_HEIGHT", "F_STORAGE"):      # v3.2.0: dam.json can give them
            if not fld.get(need):
                feedback.reportError(
                    "No field was found for '{}': each dam's dam.json must give it (choose "
                    "the field in the parameters otherwise).".format(
                        dict((k, d) for k, d, _n in BATCH_FIELD_KEYS)[need]), fatalError=False)
        parent = self.parameterAsFile(parameters, "DAMS_FOLDER", context)
        skip = self.parameterAsBool(parameters, "SKIP_DONE", context)
        dem = self.parameterAsRasterLayer(parameters, "DEM", context)
        to_dem = QgsCoordinateTransform(src_layer.sourceCrs(), dem.crs(),
                                        context.transformContext())
        feats = list(src_layer.getFeatures())
        n = max(1, len(feats))
        rows = []

        # item 13: run dams in parallel, one thread each (oversubscription guard)
        raw_workers = self.parameterAsInt(parameters, "WORKERS", context)
        workers = raw_workers if raw_workers > 0 else max((os.cpu_count() or 2) - 1, 1)
        if workers > 1 and not base_st.threads:
            base_st.threads = 1   # one thread per dam prevents oversubscription
        feedback.pushInfo(batchqueue.worker_note(workers, base_st.threads or 1))

        _log_lock = threading.Lock()

        def _make_fb(parallel=False):
            fb = _Feedback(feedback)
            if parallel:
                _orig_log = fb.log
                def _safe_log(msg):
                    with _log_lock:
                        _orig_log(msg)
                fb.log = _safe_log
                fb.progress = lambda p, msg="": None   # no per-dam progress bar in parallel
            return fb

        def val(feat, key):
            name = fld.get(key)
            return qio.num(feat[name]) if name else None

        # futures_info (parallel mode only): (future, row, dam_id, t0)
        futures_info = []
        ex = ThreadPoolExecutor(max_workers=workers) if workers > 1 else None

        try:
            for i, feat in enumerate(feats):
                if feedback.isCanceled():
                    break
                dam_id = str(feat[fld["F_ID"]]).strip() if fld["F_ID"] else str(feat.id())
                folder = os.path.join(parent, _safe_name(dam_id))
                row = dict.fromkeys(BATCH_FIELDS, "")
                row.update({"Dam": dam_id, "Folder": folder})
                t0 = time.time()
                if skip and workflow.run_finished(folder):   # 3.21.0: a finished run, not just a
                    # cancelled pass's Depth (Max).tif
                    row.update({"Status": "skipped", "Message": "already run (finished)"})
                    if ex is None:
                        rows.append(row)
                    else:
                        row["Minutes"] = round((time.time() - t0) / 60.0, 1)
                        futures_info.append((None, row, dam_id, t0))
                    continue
                feedback.pushInfo("=== {} ({}/{})".format(dam_id, i + 1, len(feats)))
                try:
                    g = feat.geometry()
                    if g is None or g.isEmpty():
                        raise brmod.BreachInputError("no geometry")
                    p = to_dem.transform(g.centroid().asPoint())
                    # v3.2.0: values the layer leaves empty come from the dam's dam.json
                    rec, problem = damjson.load(folder)
                    name = str(feat[fld["F_NAME"]]) if fld.get("F_NAME") else ""
                    vals, notes, used = damjson.fill(
                        {"HEIGHT_FT": val(feat, "F_HEIGHT"), "STORAGE_ACFT": val(feat, "F_STORAGE"),
                         "AREA_AC": val(feat, "F_AREA"), "TOP_FT": val(feat, "F_TOP"),
                         "DAM_NAME": name if name.strip() and name.upper() != "NULL" else None},
                        rec)
                    if problem:
                        notes.insert(0, problem)
                    for n_ in notes:
                        feedback.pushInfo("{}: {}".format(dam_id, n_))
                    for key, label in (("HEIGHT_FT", "height"), ("STORAGE_ACFT", "storage")):
                        if vals.get(key) is None:
                            raise brmod.BreachInputError(
                                "no dam {}: the layer has no value and dam.json {}".format(
                                    label, "has none" if rec is not None else "does not exist"))
                    dam = brmod.DamData(
                        dam_id=dam_id, dam_height_ft=vals["HEIGHT_FT"],
                        storage_top_acft=vals["STORAGE_ACFT"], top_of_dam_ft=vals["TOP_FT"],
                        surface_area_top_ac=vals["AREA_AC"], pmf_max_wse_ft=val(feat, "F_PMF"),
                        breach_bottom_ft=val(feat, "F_BOTTOM"), crest_width_ft=val(feat, "F_CREST"),
                        us_slope_h=val(feat, "F_US"), ds_slope_h=val(feat, "F_DS"),
                        crest_length_ft=val(feat, "F_LENGTH"), name=vals.get("DAM_NAME") or "")
                    files = nd_data.dam_input_files(folder)
                    st = copy.copy(base_st)
                    self._inputs = {}            # fresh input-file dict per dam
                    st.input_files = self._inputs
                    flood = scen_i == 1 or (scen_i == 2 and "pmf_inflow" in files)
                    st.scenario = workflow.SCENARIO_FLOOD if flood else workflow.SCENARIO_SUNNY
                    if files.get("breach"):
                        st.user_hydrograph = files["breach"]
                    if st.breach_method == brmod.METHOD_USER and not st.user_hydrograph:
                        raise brmod.BreachInputError(
                            "own-hydrograph method: no inputs/breach_hydrograph.csv in the dam "
                            "folder")
                    src, writer, to_work, work = self.gis(parameters, context, dem.crs(), p)
                    xy = to_work.transform(QgsPointXY(p))
                    self._dam_rec = rec
                    st.dam_record = {"read": notes, "top_source": "record" if "TOP_FT" in used
                                     else "parameter",
                                     "point": self.point_info(p, dem.crs(), work, (xy.x(), xy.y()),
                                                              context)}
                    self.apply_features(st, parameters, context, work, (xy.x(), xy.y()))
                    if flood:
                        st.flood = self.flood_inputs(parameters, context, dam, files,
                                                     pool_ft=val(feat, "F_POOL"))
                    st.input_files = dict(st.input_files)  # snapshot before threading
                    row["Scenario"] = st.scenario
                    fb = _make_fb(parallel=ex is not None)
                    if ex is not None:
                        fut = ex.submit(workflow.run_dam, dam, (xy.x(), xy.y()), st, src, writer,
                                        folder, None, fb.progress, fb.log, fb.cancel)
                        futures_info.append((fut, row, dam_id, t0))
                    else:
                        res = workflow.run_dam(dam, (xy.x(), xy.y()), st, src, writer, folder, None,
                                               fb.progress, fb.log, fb.cancel)
                        row = _apply_run_result(row, res, dam_id, feedback)
                        row["Minutes"] = round((time.time() - t0) / 60.0, 1)
                        rows.append(row)
                except Cancelled:
                    row.update({"Status": "cancelled"})
                    row["Minutes"] = round((time.time() - t0) / 60.0, 1)
                    rows.append(row)
                    break
                except (ModelError, brmod.BreachInputError, QgsProcessingException) as e:
                    row.update({"Status": "failed", "Message": str(e)[:500]})
                    feedback.reportError("{}: {}".format(dam_id, e), fatalError=False)
                    row["Minutes"] = round((time.time() - t0) / 60.0, 1)
                    if ex is None:
                        rows.append(row)
                    else:
                        futures_info.append((None, row, dam_id, t0))
                except Exception as e:
                    row.update({"Status": "failed",
                                "Message": "{}: {}".format(type(e).__name__, e)})
                    feedback.reportError("{}: {}".format(dam_id, traceback.format_exc()),
                                        fatalError=False)
                    row["Minutes"] = round((time.time() - t0) / 60.0, 1)
                    if ex is None:
                        rows.append(row)
                    else:
                        futures_info.append((None, row, dam_id, t0))

            # collect parallel results in submission order
            for fut, row, dam_id, t0 in futures_info:
                if fut is None:
                    rows.append(row)
                    continue
                try:
                    res = fut.result()
                    row = _apply_run_result(row, res, dam_id, feedback)
                except Cancelled:
                    row.update({"Status": "cancelled"})
                except (ModelError, brmod.BreachInputError, QgsProcessingException) as e:
                    row.update({"Status": "failed", "Message": str(e)[:500]})
                    feedback.reportError("{}: {}".format(dam_id, e), fatalError=False)
                except Exception as e:
                    row.update({"Status": "failed",
                                "Message": "{}: {}".format(type(e).__name__, e)})
                    feedback.reportError("{}: {}".format(dam_id, traceback.format_exc()),
                                        fatalError=False)
                row["Minutes"] = round((time.time() - t0) / 60.0, 1)
                rows.append(row)
        finally:
            if ex is not None:
                ex.shutdown(wait=True, cancel_futures=True)
        path = os.path.join(parent, "Batch_Breach_Runs.csv")
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=BATCH_FIELDS)
            w.writeheader()
            w.writerows(rows)
        counts = {k: sum(1 for r in rows if r["Status"] == k)
                  for k in ("done", "failed", "skipped", "cancelled", "no breach")}
        msg = "{done} run, {failed} failed, {skipped} skipped, {no breach} with no breach".format(
            **counts)
        feedback.pushInfo(msg)
        # item 4: aggregate "Things to check" across all dams at the end of the batch
        all_warnings = [(r["Dam"], w) for r in rows
                        for w in (r.get("Warnings") or "").split(" | ") if w]
        if all_warnings:
            feedback.pushInfo("--- Things to check ---")
            for dam_id, w in all_warnings:
                feedback.pushWarning("{}: {}".format(dam_id, w))
        if self.parameterAsBool(parameters, "CLASSIFY", context) and counts["done"] \
                and not counts["cancelled"] and not feedback.isCanceled():
            try:
                r = handoff.classify_batch(
                    parent, self._str(parameters, "CLASS_ALG", context),
                    handoff.load_mapping(self._str(parameters, "CLASS_MAP", context)), feedback)
                csvs = [v for v in r["outputs"].values() if isinstance(v, str)
                        and v.lower().endswith(".csv") and os.path.exists(v)]
                if csvs:
                    out = os.path.join(parent, "Batch_Breach_Runs_Classified.csv")
                    handoff.merge_csv(path, csvs[0], out)
                    feedback.pushInfo("Classification merged into " + out)
                    path = out
                else:
                    feedback.pushInfo("The classification ran; it returned no CSV to merge.")
            except handoff.HandoffError as e:
                feedback.reportError("Classification: {}".format(e), fatalError=False)
            except Exception as e:
                feedback.reportError("Classification failed: {}".format(e), fatalError=False)
        return {"SUMMARY": path, "COUNTS": msg}


# ============================================================================
class CascadeAlgorithm(_Base):
    def name(self):
        return "cascade"

    def displayName(self):
        return "Cascade failure of dams in a chain"

    def shortHelpString(self):
        return ("Models an upstream dam's failure and follows the flood to the next dam down "
                "the valley. The flood's discharge is measured just upstream of the next dam; "
                "if it overtops that dam (or you assume the failure) the next dam is modelled "
                "failing, with the wave added to its own inflow when its normal pool level (and "
                "PMF inflow / spillway files in <Dams folder>/<Dam ID>/inputs/) are given. "
                "Reservoirs listed in the Ponds layer are treated as full for the upstream "
                "flood (tailwater). Writes 'Cascade ...' rasters: the deepest and fastest water "
                "of all runs and the earliest arrival after the first dam's breach.\n\n"
                "Dams are ordered upstream first by the order field (or the layer order).")

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource("DAMS", "Dams in the chain "
                                                              "(points)", [SOURCE_POINT]))
        for key, desc, num in BATCH_FIELD_KEYS:
            self._field(key, desc + " (empty = match by name)", "DAMS", numeric=num)
        self._field("F_ORDER", "Order field (1 = most upstream; empty = layer order)", "DAMS")
        self.addParameter(QgsProcessingParameterEnum(
            "FAIL_RULE", "A downstream dam fails", [
                "only if the flood from upstream overtops it",
                "always (assumed)"], defaultValue=0))
        self.addParameter(QgsProcessingParameterEnum(
            "METHOD", "Breach (ND 5.1.1(d))", [m[1] for m in METHODS], defaultValue=0))
        self.add_feature_params()
        self.add_model_params()
        self.addOutput(QgsProcessingOutputFile("REPORT", "Cascade report"))
        self.addOutput(QgsProcessingOutputFolder("FOLDER", "Cascade folder"))

    def processAlgorithm(self, parameters, context, feedback):
        src_layer = self.parameterAsSource(parameters, "DAMS", context)
        if src_layer is None:
            raise QgsProcessingException("No dams layer")
        st = self.run_settings(parameters, context,
                               METHODS[self.parameterAsEnum(parameters, "METHOD", context)][0])
        st.scenario = workflow.SCENARIO_SUNNY
        names = qio.field_names(src_layer)
        auto, _how = nd_data.suggest_fields(names)
        fld = {k: (self._str(parameters, k, context) or auto.get(k, ""))
               for k, _d, _n in BATCH_FIELD_KEYS}
        order = self._str(parameters, "F_ORDER", context)
        feats = list(src_layer.getFeatures())
        if order:
            feats.sort(key=lambda f: qio.num(f[order], 1e9))
        if len(feats) < 2:
            raise QgsProcessingException("A cascade needs at least two dams")
        parent = self.parameterAsFile(parameters, "DAMS_FOLDER", context)
        dem = self.parameterAsRasterLayer(parameters, "DEM", context)
        to_dem = QgsCoordinateTransform(src_layer.sourceCrs(), dem.crs(),
                                        context.transformContext())
        val = lambda f, k: qio.num(f[fld[k]]) if fld.get(k) else None
        chain, src, writer, work = [], None, None, None
        for f in feats:
            dam_id = str(f[fld["F_ID"]]).strip() if fld.get("F_ID") else str(f.id())
            g = f.geometry()
            p = to_dem.transform(g.centroid().asPoint())
            if src is None:
                src, writer, to_work, work = self.gis(parameters, context, dem.crs(), p)
            xy = to_work.transform(QgsPointXY(p))
            dam = brmod.DamData(
                dam_id=dam_id, dam_height_ft=val(f, "F_HEIGHT"),
                storage_top_acft=val(f, "F_STORAGE"), top_of_dam_ft=val(f, "F_TOP"),
                surface_area_top_ac=val(f, "F_AREA"), pmf_max_wse_ft=val(f, "F_PMF"),
                breach_bottom_ft=val(f, "F_BOTTOM"), crest_width_ft=val(f, "F_CREST"),
                us_slope_h=val(f, "F_US"), ds_slope_h=val(f, "F_DS"),
                crest_length_ft=val(f, "F_LENGTH"),
                name=str(f[fld["F_NAME"]]) if fld.get("F_NAME") else "")
            fi = None
            files = nd_data.dam_input_files(os.path.join(parent, _safe_name(dam_id)))
            pool = val(f, "F_POOL")
            if pool is not None or files.get("pmf_inflow"):
                try:
                    fi = floodmod.FloodInputs.from_files(
                        files["pmf_inflow"], files.get("spillway"), normal_pool_ft=pool,
                        crest_length_ft=dam.crest_length_ft) if files.get("pmf_inflow") \
                        else floodmod.FloodInputs.triangle(0.0, 1.0, 2.0, normal_pool_ft=pool,
                                                           crest_length_ft=dam.crest_length_ft)
                    if files.get("spillway") and not files.get("pmf_inflow"):
                        fi.spillway = floodmod.Spillway.from_csv(files["spillway"])
                except brmod.BreachInputError as e:
                    raise QgsProcessingException("{}: {}".format(dam_id, e))
            chain.append(cascmod.CascadeDam(dam, (xy.x(), xy.y()), None, fi))
        for n in self.apply_features(st, parameters, context, work, chain[0].xy):
            feedback.pushInfo(n)
        rule = ("overtop", "assume")[self.parameterAsEnum(parameters, "FAIL_RULE", context)]
        fb = _Feedback(feedback)
        try:
            out = cascmod.run_cascade(chain, st, src, writer, parent, rule, fb.progress, fb.log,
                                      fb.cancel)
        except Cancelled:
            raise QgsProcessingException("Cancelled")
        except (ModelError, brmod.BreachInputError) as e:
            raise QgsProcessingException(str(e))
        for w in out.warnings:
            feedback.pushWarning("Check: " + w)
        for d in out.decisions:
            feedback.pushInfo("{}: {} ({})".format(d["dam"], "fails" if d["fails"] else "holds",
                                                   d["reason"]))
        for n, p in out.combined.items():
            self.load(context, p, n[:-4])
        return {"REPORT": out.report_md, "FOLDER": out.folder}


# ============================================================================
class HydrographAlgorithm(_Base):
    def name(self):
        return "hydrograph"

    def displayName(self):
        return "Breach hydrograph only"

    def shortHelpString(self):
        return ("Computes the ND 5.1.1 breach hydrograph without running a flood model, for "
                "checking the breach parameters. Writes the hydrograph CSV and a plot and prints "
                "the parameters, the routed peak, the Froehlich (1995) peak and, when the "
                "embankment geometry is given, the TR-60 peak.")

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterString("DAM_ID", "Dam ID", "dam"))
        self._num("HEIGHT_FT", "Dam height (ft)", minimum=0.1)
        self._num("STORAGE_ACFT", "Storage at the top of dam (ac-ft)", minimum=0.01)
        self._num("TOP_FT", "Top of dam elevation (ft; empty = equal to the height)",
                  optional=True)
        self._num("AREA_AC", "Surface area at the top of dam (ac)", optional=True, minimum=0)
        self._num("PMF_FT", "Maximum reservoir level in the PMF (ft)", optional=True)
        self._num("BOTTOM_FT", "Breach bottom elevation (ft)", optional=True)
        self.add_breach_params(per_dam=True)
        self.addParameter(QgsProcessingParameterFileDestination(
            "OUTPUT", "Hydrograph CSV", "CSV files (*.csv)"))

    def processAlgorithm(self, parameters, context, feedback):
        o = lambda n: self._opt(parameters, n, context)
        h = o("HEIGHT_FT")
        dam = brmod.DamData(
            dam_id=self.parameterAsString(parameters, "DAM_ID", context) or "dam",
            dam_height_ft=h, storage_top_acft=o("STORAGE_ACFT"),
            top_of_dam_ft=o("TOP_FT") if o("TOP_FT") is not None else h,
            surface_area_top_ac=o("AREA_AC"), pmf_max_wse_ft=o("PMF_FT"),
            breach_bottom_ft=o("BOTTOM_FT"), crest_width_ft=o("CREST_WIDTH"),
            us_slope_h=o("US_SLOPE"), ds_slope_h=o("DS_SLOPE"), valley_width_ft=o("VALLEY_WIDTH"),
            crest_length_ft=o("CREST_LENGTH"))
        ov = brmod.BreachOverrides(
            o("BREACH_WIDTH"), o("FORMATION_HR"), o("SIDE_SLOPE"),
            pipe_time_fraction=self.parameterAsDouble(parameters, "PIPE_FRACTION", context),
            pipe_initial_dia_ft=self.parameterAsDouble(parameters, "PIPE_DIA_FT", context),
            vtg_erodible=self.parameterAsBool(parameters, "VTG_ERODIBLE", context))
        method = METHODS[self.parameterAsEnum(parameters, "METHOD", context)][0]
        try:
            hy = brmod.make_hydrograph(dam, method, ov,
                                       self.parameterAsFile(parameters, "USER_CSV", context))
        except brmod.BreachInputError as e:
            raise QgsProcessingException(str(e))
        out = self.parameterAsFileOutput(parameters, "OUTPUT", context)
        hy.to_csv(out)
        try:
            from .core import products
            svg = os.path.splitext(out)[0] + ".svg"
            with open(svg, "w", encoding="utf-8") as f:
                f.write(products.hydrograph_plot(hy))
            feedback.pushInfo("Plot: " + svg)
        except Exception as e:
            feedback.pushInfo("No plot: {}".format(e))
        for k, v in hy.summary().items():
            feedback.pushInfo("{}: {}".format(k, v))
        for n in hy.notes:
            feedback.pushWarning("Check: " + n)
        return {"OUTPUT": out}


# ============================================================================
class EstimateAlgorithm(RunDamAlgorithm):
    def name(self):
        return "estimate"

    def displayName(self):
        return "Estimate the run time for one dam"

    def shortHelpString(self):
        return ("Sets up the dam as 'Run breach model for one dam' would (breach hydrograph, "
                "inflow point, first domain) without running the flood, times a small "
                "LISFLOOD-FP test flood on this computer and estimates how long the run will "
                "take. An estimate, good to within a factor of two or so; deep floods from "
                "tall dams are slower than the test flood.")

    def initAlgorithm(self, config=None):
        super().initAlgorithm(config)

    def processAlgorithm(self, parameters, context, feedback):
        import tempfile
        from .core import lisflood as lfmod
        dam, st, src, writer, dam_xy, inflow, folder = self.prepare(parameters, context,
                                                                    feedback)
        try:
            plan = workflow.preview(dam, dam_xy, st, src, inflow, folder)
        except brmod.BreachInputError as e:
            raise QgsProcessingException(str(e))
        if not plan.get("breach_occurs"):
            msg = "The dam does not fail in this flood: there is nothing to model."
            feedback.pushInfo(msg)
            return {"REPORT": msg}
        feedback.pushInfo("Timing a test flood...")
        with tempfile.TemporaryDirectory(prefix="ndbreach_cal_") as tmp:
            rate = lfmod.calibrate(st.lisflood_exe, tmp, st.threads)
        if not rate:
            raise QgsProcessingException("The LISFLOOD-FP test flood did not run; use 'Check "
                                         "the model installation'.")
        est = estmod.estimate(plan, rate, st.tier == workflow.TIER_BOTH)
        lines = estmod.describe(plan, est)
        for ln in lines:
            feedback.pushInfo(ln)
        return {"REPORT": " ".join(lines)}


# ============================================================================
class CheckAlgorithm(_Base):
    def name(self):
        return "check"

    def displayName(self):
        return "Check the model installation"

    def shortHelpString(self):
        return ("Looks for LISFLOOD-FP and the ANUGA conda environment (using the paths below "
                "or the provider settings, then the usual install places), runs LISFLOOD-FP on "
                "a tiny synthetic channel (confirming the arrival-depth patch is built in) and "
                "imports ANUGA in its Python environment. Prints what it found so the "
                "provider settings can be filled in.")

    def initAlgorithm(self, config=None):
        self._file("LISFLOOD_EXE", "LISFLOOD-FP executable (empty = provider setting, an installed LISFLOOD-FP, the "
                   "one bundled with the plugin, else the built-in engine; 'builtin' = the "
                   "built-in engine)")
        self._file("ANUGA_PYTHON", "ANUGA Python interpreter (a conda env's python symlink is "
                   "resolved to the real file). Empty = provider setting or auto-detected")
        self.addOutput(QgsProcessingOutputString("RESULT", "Result"))

    def processAlgorithm(self, parameters, context, feedback):
        import tempfile
        exe = self.find_lisflood(parameters, context)
        py = self.find_anuga(parameters, context)
        lines = []
        with tempfile.TemporaryDirectory(prefix="ndbreach_check_") as tmp:
            if not exe:
                lines.append("PROBLEM LISFLOOD-FP not found. Build it with "
                             "scripts/build_lisflood_macos.sh and set its path in Processing > "
                             "Options > Providers > ND Dam Breach Runner.")
            else:
                r = check_lisflood(exe, os.path.join(tmp, "lisflood"))
                lines.append(("OK " if r["ok"] and r.get("patched") else "PROBLEM ") +
                             r["message"])
            if py:
                a = check_anuga(py, tmp)
                lines.append(("OK " if a["ok"] else "PROBLEM ") +
                             "ANUGA ({}): {}".format(py, a["message"]))
            else:
                lines.append("ANUGA: no conda environment named like 'anuga' found (only needed "
                             "for the ANUGA tier)")
        for ln in lines:
            if ln.startswith(("OK", "ANUGA: no")):
                feedback.pushInfo(ln)
            else:
                feedback.reportError(ln, fatalError=False)
        return {"RESULT": "\n".join(lines)}


CORE_ALGORITHMS = (RunDamAlgorithm, BatchAlgorithm, SensitivityAlgorithm, CascadeAlgorithm,
                   HydrographAlgorithm, EstimateAlgorithm, CheckAlgorithm)
from .algorithms_tools import TOOL_ALGORITHMS      # noqa: E402  (they build on _Base)
from .algorithms_hydrology import HYDROLOGY_ALGORITHMS          # noqa: E402
from .algorithms_consequences import CONSEQUENCE_ALGORITHMS     # noqa: E402
from .algorithms_ndhazard import NDHAZARD_ALGORITHMS           # noqa: E402
from .algorithms_report import REPORT_ALGORITHMS                # noqa: E402
from .algorithms_hecras import HECRAS_ALGORITHMS                # noqa: E402
from .algorithms_probabilistic import PROBABILISTIC_ALGORITHMS  # noqa: E402
from .algorithms_animation import ANIMATION_ALGORITHMS          # noqa: E402
from .algorithms_verification import VERIFICATION_ALGORITHMS    # noqa: E402
from .algorithms_provenance import PROVENANCE_ALGORITHMS        # noqa: E402
from .algorithms_selftest import SELFTEST_ALGORITHMS            # noqa: E402
from .algorithms_anugasetup import SETUPANUGA_ALGORITHMS        # noqa: E402
from .algorithms_nested import NESTED_ALGORITHMS                # noqa: E402
from .algorithms_release320 import RELEASE320_ALGORITHMS        # noqa: E402
from .algorithms_adequacy import ADEQUACY_ALGORITHMS            # noqa: E402
ALGORITHMS = (CORE_ALGORITHMS + NESTED_ALGORITHMS + PROBABILISTIC_ALGORITHMS + TOOL_ALGORITHMS + HYDROLOGY_ALGORITHMS
              + CONSEQUENCE_ALGORITHMS + NDHAZARD_ALGORITHMS + ANIMATION_ALGORITHMS + REPORT_ALGORITHMS
              + HECRAS_ALGORITHMS + PROVENANCE_ALGORITHMS + VERIFICATION_ALGORITHMS
              + SELFTEST_ALGORITHMS + SETUPANUGA_ALGORITHMS + RELEASE320_ALGORITHMS + ADEQUACY_ALGORITHMS)
