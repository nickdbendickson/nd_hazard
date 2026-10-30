"""Hazard classification for the screening tools.

AUTHORITY: ND Dam Safety Standards REG_05.2026 Sections 5.2 / 5.3 with the Reclamation (1988)
/ ACER TM 11 depth-velocity danger charts (ND Fig. 5.1 houses, 5.2 mobile homes), as
implemented in core/nd_hazard.py. nd_hazard.py and acer_tm11_charts.json are byte-identical
copies of the ND Dam Breach Runner's files - NEVER edit them here. This module only
  * wraps nd_hazard's attribute rules (habitability, mobile homes, interstate, passenger, AADT,
    trains a day) for tools 0, 6, 8 so every tool reads a layer the same way the runner does;
  * samples the screening's rasters (metres, m/s) with nd_hazard's samplers;
  * builds nd_hazard receptors (ft, ft/s) and classifies (classify_nd);
  * keeps the COMPARISON-ONLY criteria (FEMA/Feinberg thresholds used by DSS-WISE HCOM, and a
    curve YOU supply). They are practice values, not the ND authority.
Pure Python / numpy (no QGIS).
"""
import math

import numpy as np

try:                                   # package import (QGIS, tests that import the package)
    from . import nd_hazard as nh
except ImportError:                    # tests that put core/ itself on sys.path
    import nd_hazard as nh

RANK = nh.RANK
FT = nh.M_PER_FT                       # 0.3048 m per ft
M_PER_FT = nh.M_PER_FT
FT2 = FT * FT                          # ft2/s -> m2/s for depth x velocity
FIELD_MAX = 254                        # longest text written to a field (shapefile limit)

UNINHABITED = nh.UNINHABITED_KEYWORDS
# FRA North American Rail Network PASSNGR codes counted as an active passenger line.
PAX_CODES = ("A", "B", "C")
# 'any depth' (nd_hazard's model-noise tolerance): exactly 0.1 ft, the runner's WET_FT default
DEFAULT_WET_FT = 0.1                   # the engineer's allowance for model noise (ft)
DEFAULT_WET_M = DEFAULT_WET_FT * M_PER_FT   # 0.03048 m
MIN_WET_FT = 0.01
MIN_WET_M = 0.003                      # just under nd_hazard.MIN_WET_FT (0.01 ft = 0.003048 m)


def m_to_ft(x):
    return None if x is None else float(x) / M_PER_FT


def words(text):
    """'a, b; c' -> ('a', 'b', 'c')"""
    return tuple(w.strip() for w in str(text or "").replace(";", ",").split(",") if w.strip())


# ------------------------------------------------------------------ attribute rules (nd_hazard)
def is_habitable(*texts, flag=None, uninhabited=UNINHABITED):
    """ND 5.2.1.1 with 5.3: a yes/no `flag` (tool 0's 'habitable' field) wins when it says yes
    or no; otherwise habitable unless one of the occupancy texts names an uninhabited building
    (nd_hazard.habitable_from_text). Blank / unknown -> habitable."""
    yn = nh.yes_no(flag) if flag is not None else None
    if yn is not None:
        return yn
    return all(nh.habitable_from_text(t, uninhabited) for t in texts)


def is_mobile(*values):
    """Mobile / manufactured home / trailer (or a yes/1 mobile flag) in any of the values."""
    return any(nh.mobile_from_text(v) for v in values if v is not None)


def is_interstate(*texts):
    return any(nh.interstate_from_text(t) for t in texts if t)


def rail_passenger(value, codes=PAX_CODES):
    return nh.passenger_from_value(value, tuple(codes))


def road_level(aadt=None, road_class="", unknown=nh.MEDIUM, sole_access=False):
    """ND 5.2.1.2 class of an overtopped road (nd_hazard.road_class). A missing, NaN or
    non-positive AADT is unknown: `unknown` is assumed (a data gap in tool 8)."""
    r = nh.Road("r", 1.0, aadt=nh.aadt_value(aadt), interstate=is_interstate(road_class),
                sole_access=bool(sole_access))
    return nh.road_class(r, nh.Settings(unknown_aadt=unknown))[0]


def rail_level(trains_per_day=None, passenger=False):
    r = nh.Railroad("t", 1.0, trains_per_day=nh.trains_value(trains_per_day),
                    passenger=bool(passenger))
    return nh.rail_class(r, nh.Settings())[0]


def normalize_existing(text):
    """DWR Low / Significant / High (NID L / S / H); Significant = Medium. None when unknown."""
    return nh.class_from_text(text)


def compare_existing(new, existing_text):
    if new not in RANK:
        return "n/a"
    e = normalize_existing(existing_text)
    if e is None:
        return "existing class undetermined"
    d = RANK[new] - RANK[e]
    return "agree" if d == 0 else ("higher than existing" if d > 0 else "lower than existing")


# ------------------------------------------------------------------ comparison-only criteria
COMPARISON_FEINBERG = "feinberg"
COMPARISON_CURVE = "curve"
COMPARISON = (COMPARISON_FEINBERG, COMPARISON_CURVE)
#   feinberg : FEMA (2015 draft) / Feinberg (2017) potentially lethal flood zones as given in
#              the DSS-WISE Lite Technical Report, Appendix C: residential >= 4 ft or
#              DV >= 75 ft2/s; mobile home >= 2 ft or DV >= 30 ft2/s
#   curve    : user-digitised depth-velocity boundary CSV (velocity_ms,depth_m)
FEINBERG = {"residential": (4.0 * FT, 75.0 * FT2), "mobile": (2.0 * FT, 30.0 * FT2)}


def load_curve(path):
    """CSV with header velocity_ms,depth_m : the boundary of the high-danger zone."""
    d = np.loadtxt(path, delimiter=",", skiprows=1, ndmin=2)
    o = np.argsort(d[:, 0])
    return d[o, 0], d[o, 1]


def structure_lethal(depth_m, vel_ms, curve=None, rule_depth_m=2 * FT, criterion=None,
                     dv_m2s=None, mobile=False):
    """Comparison criteria only: True if a structure sees lethal conditions.
    criterion 'curve' / 'feinberg', or the plain depth rule (depth > rule_depth_m)."""
    if depth_m <= 0:
        return False
    if criterion is None:
        criterion = "curve" if curve is not None else "depth"
    if criterion == "curve":
        if curve is None:
            raise ValueError("criterion 'curve' needs a curve file")
        v, d = curve
        return bool(depth_m >= np.interp(vel_ms, v, d))
    if criterion == "feinberg":
        dmax, dvmax = FEINBERG["mobile" if mobile else "residential"]
        dv = dv_m2s if dv_m2s is not None else depth_m * vel_ms
        return bool(depth_m >= dmax or dv >= dvmax)
    return bool(depth_m > rule_depth_m)


# ------------------------------------------------------------------ methods of tool 8
# enum order: nd_hazard's three methods, then the two comparison-only criteria
METHODS = (nh.LETHALITY_CHARTS, nh.LETHALITY_1D, nh.LETHALITY_ALL_WET) + COMPARISON
METHOD_LABELS = {
    nh.LETHALITY_CHARTS: "ND 5.2.1.1: ACER TM 11 depth-velocity charts (2D model)",
    nh.LETHALITY_1D: "ND 5.2.1.1: 1D model rule, habitable structure deeper than 2 ft (1D "
                     "models only)",
    nh.LETHALITY_ALL_WET: "ND 5.3 conservative: every inundated habitable structure "
                          "potentially lethal (no chart)",
    COMPARISON_FEINBERG: "Comparison only, not the ND authority: FEMA / Feinberg (DSS-WISE "
                         "HCOM) >= 4 ft or DV >= 75 ft2/s; mobile home >= 2 ft or 30 ft2/s",
    COMPARISON_CURVE: "Comparison only, not the ND authority: your depth-velocity curve CSV",
}
ONE_D_WARNING = ("ND 5.2.1.1 allows the 2 ft depth rule only for a 1D model. These floods are "
                 "2D (every engine of tool 7), so use the ACER TM 11 charts, or the "
                 "conservative method until the charts are digitized and checked.")


def charts_not_digitized_message(cs=None, path=None, conservative_label=None):
    """nd_hazard's own wording (it does not recommend the 1D rule for 2D results), plus the
    name of the conservative option in the calling tool."""
    if cs is None:
        cs = nh.ChartSet(path=path or "")
    try:
        cs.chart(False)
        return ""
    except nh.ChartsNotDigitized as e:
        msg = str(e)
    return msg + " In this tool that is the option '{}'.".format(
        conservative_label or METHOD_LABELS[nh.LETHALITY_ALL_WET])


def load_ready_charts(path=None, conservative_label=None):
    """ChartSet ready for the chart method, else nh.ChartsNotDigitized with the message."""
    cs = nh.load_charts(path or None)
    if not cs.ready:
        raise nh.ChartsNotDigitized(charts_not_digitized_message(cs, conservative_label=
                                                                 conservative_label))
    return cs


def chart_text(charts):
    """'digitized 1a2b3c4d5e6f' - status and the first 12 characters of the file's sha256."""
    if charts is None:
        return "not used"
    info = charts.info() if hasattr(charts, "info") else dict(charts)
    return "{} {}".format(info.get("status", "?"), (info.get("sha256") or "")[:12]).strip()


def limit_text(parts, limit=FIELD_MAX, sep="; "):
    """Join `parts`, cut to `limit` characters with a '(+N more)' tail when needed."""
    parts = [str(p) for p in parts if p]
    out = sep.join(parts)
    if len(out) <= limit:
        return out
    for k in range(len(parts) - 1, 0, -1):
        tail = "{}(+{} more)".format(sep, len(parts) - k)
        head = sep.join(parts[:k])
        if len(head) + len(tail) <= limit:
            return head + tail
    return out[:limit - 3] + "..."


def governing_summary(c, limit=FIELD_MAX):
    """Short text of the governing findings, grouped by kind and section."""
    groups = []
    for f in c.governing:
        key = (f.kind, f.section)
        for g in groups:
            if g[0] == key:
                g[1] += 1
                break
        else:
            groups.append([key, 1, f.reason])
    parts = []
    for (kind, sec), n, reason in groups:
        parts.append("{} ({})".format(reason, sec) if n == 1 else
                     "{} {}s, e.g. {} ({})".format(n, kind, reason, sec))
    if not parts:
        parts = ["nothing listed in ND 5.2 reached"]
    return limit_text(parts, limit)


# ------------------------------------------------------------------ sampling (nd_hazard's)
class Grid:
    """Depth (m) and velocity (m/s) arrays of one flood on a north-up, square-cell grid.
    No-data and non-finite values become NaN: dry depth, unknown velocity (None downstream)."""

    def __init__(self, depth, vel, gt, nodata=-9999.0, dv=None):
        assert abs(gt[2]) < 1e-12 and abs(gt[4]) < 1e-12, "rotated rasters are not supported"
        assert gt[5] < 0 and abs(abs(gt[5]) - gt[1]) <= 1e-9 * max(1.0, gt[1]), \
            "square, north-up cells are required (gt[1] = -gt[5])"
        clean = lambda a: np.where(np.isfinite(a) & (a > nodata + 1), a, np.nan)
        self.depth = clean(np.asarray(depth, float))
        self.vel = clean(np.asarray(vel, float)) if vel is not None else None
        self.dv = clean(np.asarray(dv, float)) if dv is not None else None
        self.x0, self.y_top, self.cell = float(gt[0]), float(gt[3]), float(gt[1])
        ny, nx = self.depth.shape
        self.extent = (self.x0, self.y_top - ny * self.cell, self.x0 + nx * self.cell, self.y_top)
        self.gt = tuple(gt)

    def point(self, x, y):
        return nh.sample_point(self.depth, self.vel, self.x0, self.y_top, self.cell, x, y)

    def _dv_point(self, x, y):
        if self.dv is None:
            return None
        return nh.sample_point(self.dv, None, self.x0, self.y_top, self.cell, x, y)[0]

    def sample(self, kind, parts, wet_ft=0.1):
        """{depth_m, vel_ms, dv_m2s, x, y, how} for a feature's parts, or None when it is off
        the grid. kind: 'point' (parts = [(x, y)...]: the deepest point), 'polygon' (exterior
        rings: nd_hazard.sample_footprint per part; the deepest part, the largest velocity of
        the wet parts), 'line' (vertex lists: nd_hazard.sample_line_max per part, the deepest).
        Velocity None = unknown."""
        best = None
        if kind == "point":
            for x, y in parts:
                d, v = self.point(x, y)
                if d is not None and (best is None or d > best["depth_m"]):
                    best = {"depth_m": d, "vel_ms": v, "dv_m2s": self._dv_point(x, y),
                            "x": x, "y": y, "how": "point"}
            return best
        if kind == "polygon":
            vmax, wet_any = None, False
            for ring in parts:
                d, v, how = nh.sample_footprint(self.depth, self.vel, self.x0, self.y_top,
                                                self.cell, ring, wet_ft, 1.0 / M_PER_FT)
                if d is None:
                    continue
                cx = sum(p[0] for p in ring) / len(ring)
                cy = sum(p[1] for p in ring) / len(ring)
                dv = None
                if self.dv is not None:
                    dv = nh.sample_footprint(self.dv, None, self.x0, self.y_top, self.cell,
                                             ring)[0]
                if d / M_PER_FT >= wet_ft:
                    wet_any = True
                    if v is not None:
                        vmax = v if vmax is None else max(vmax, v)
                if best is None or d > best["depth_m"]:
                    best = {"depth_m": d, "vel_ms": v, "dv_m2s": dv, "x": cx, "y": cy,
                            "how": "polygon: " + how}
            if best is not None and wet_any:
                best["vel_ms"] = vmax
            return best
        if kind == "line":
            for verts in parts:
                d = nh.sample_line_max(self.depth, self.x0, self.y_top, self.cell, verts)
                if d is not None and (best is None or d > best["depth_m"]):
                    x, y = self._line_where(verts, d)
                    best = {"depth_m": d, "vel_ms": None, "dv_m2s": None, "x": x, "y": y,
                            "how": "line: deepest half-cell sample"}
            return best
        return None

    def _line_where(self, verts, d):
        """Where along the line the depth `d` was found (display only; same half-cell steps
        as nd_hazard.sample_line_max)."""
        step = 0.5 * self.cell
        for (xa, ya), (xb, yb) in zip(verts[:-1], verts[1:]):
            n = max(1, int(math.ceil(math.hypot(xb - xa, yb - ya) / step)))
            for i in range(n + 1):
                x, y = xa + i / n * (xb - xa), ya + i / n * (yb - ya)
                if self.point(x, y)[0] == d:
                    return x, y
        return verts[0]


# ------------------------------------------------------------------ nd_hazard receptors
def nd_structure(ident, depth_m, vel_ms, mobile=False, habitable=True, x=None, y=None):
    """Velocity None stays None (unknown: nd_hazard counts it potentially lethal)."""
    return nh.Structure(str(ident), m_to_ft(depth_m), None if vel_ms is None else m_to_ft(vel_ms),
                        habitable=bool(habitable), mobile=bool(mobile), x=x, y=y)


def nd_road(ident, depth_m, aadt=None, road_class="", x=None, y=None, sole_access=False,
            name=""):
    """AADT via nd_hazard.aadt_value (missing, NaN, zero or negative = unknown -> data gap);
    interstate from the class text or the name (nd_hazard.interstate_from_text)."""
    return nh.Road(str(ident), m_to_ft(depth_m), aadt=nh.aadt_value(aadt),
                   interstate=is_interstate(road_class, name), sole_access=bool(sole_access),
                   x=x, y=y)


def nd_rail(ident, depth_m, trains_per_day=None, passenger=False, x=None, y=None):
    return nh.Railroad(str(ident), m_to_ft(depth_m),
                       trains_per_day=nh.trains_value(trains_per_day),
                       passenger=bool(passenger), x=x, y=y)


def nd_lifeline(ident, depth_m, kind="", x=None, y=None):
    return nh.Lifeline(str(ident), m_to_ft(depth_m), kind=str(kind or ""), x=x, y=y)


def nd_downstream_dam(ident, hazard_class, depth_m):
    """A dam of the input layer whose point is wet in this dam's flood (5.3 cascading)."""
    return nh.DownstreamDam(str(ident), str(hazard_class or ""), affected=True,
                            note="flood depth {:.1f} ft at the dam".format(m_to_ft(depth_m)))


def _comparison_finding(s, criterion, rule_depth_m, curve, wet_ft):
    """nh.Finding for one structure under a comparison-only criterion (or None when dry)."""
    if s.get("depth_m") is None or m_to_ft(s["depth_m"]) < wet_ft:
        return None
    d = m_to_ft(s["depth_m"])
    kw = dict(kind="structure", id=str(s["id"]), depth_ft=round(d, 2), x=s.get("x"),
              y=s.get("y"))
    if not s.get("habitable", True):
        return nh.Finding(nh.LOW, "5.2.1.1", reason="uninhabited building inundated ({:.1f} ft)"
                          .format(d), **kw)
    what = "mobile home" if s.get("mobile") else "habitable structure"
    if s.get("vel_ms") is None and (criterion == COMPARISON_CURVE or s.get("dv_m2s") is None):
        return nh.Finding(nh.HIGH, "comparison: " + criterion, reason="{} {:.1f} ft, velocity "
                          "unknown: taken as lethal".format(what, d), borderline=True,
                          gap="structure {}: no velocity at its location".format(s["id"]), **kw)
    v = s.get("vel_ms") or 0.0
    lethal = structure_lethal(s["depth_m"], v, curve, rule_depth_m, criterion, s.get("dv_m2s"),
                              bool(s.get("mobile")))
    kw["vel_fts"] = round(m_to_ft(v), 2)
    if lethal:
        return nh.Finding(nh.HIGH, "comparison: " + criterion, reason="{} {:.1f} ft at {:.1f} "
                          "ft/s: lethal by the {} criterion (comparison only)".format(
                              what, d, m_to_ft(v), criterion), **kw)
    return nh.Finding(nh.MEDIUM, "5.2.2", reason="{} inundated {:.1f} ft: economic loss"
                      .format(what, d), **kw)


def classify_nd(structures=(), roads=(), railroads=(), method=nh.LETHALITY_CHARTS,
                wet_m=DEFAULT_WET_M, rule_depth_ft=2.0, unknown_aadt=nh.MEDIUM, charts=None,
                curve=None, lifelines=(), downstream_dams=(), unknown_trains=nh.MEDIUM,
                judgment_zone_high=False):
    """Classify one dam through nd_hazard.classify.

    judgment_zone_high: the old option - a habitable structure in the ACER judgment zone counts
    as potentially lethal (HIGH). Off (default, Nick 10/30/2026): MEDIUM by rule.

    structures: dicts {id, depth_m, vel_ms (None = unknown), dv_m2s, mobile, habitable, x, y};
    roads: dicts {id, depth_m, aadt, road_class, name, sole_access, x, y}; railroads: dicts
    {id, depth_m, trains_per_day, passenger, x, y}; lifelines: dicts {id, depth_m, kind, x, y};
    downstream_dams: nh.DownstreamDam (already known to be wet). Depths in m and velocities in
    m/s (converted here). method: one of METHODS. The comparison criteria classify the
    structures with the old screening rules; everything else still follows nd_hazard.
    Raises nh.ChartsNotDigitized for the chart method without ready charts.
    """
    if method not in METHODS:
        raise ValueError("unknown lethality method '{}'".format(method))
    wet_ft = m_to_ft(wet_m)
    st = nh.Settings(lethality=method if method in nh.LETHALITY_METHODS else nh.LETHALITY_1D,
                     wet_ft=wet_ft, rule_depth_ft=float(rule_depth_ft),
                     unknown_aadt=unknown_aadt, unknown_trains=unknown_trains,
                     judgment_zone_high=bool(judgment_zone_high)).validate()
    if method == nh.LETHALITY_CHARTS and (charts is None or not charts.ready):
        raise nh.ChartsNotDigitized(charts_not_digitized_message(charts))
    rd = [nd_road(r["id"], r["depth_m"], r.get("aadt"), r.get("road_class", ""), r.get("x"),
                  r.get("y"), r.get("sole_access", False), r.get("name", "")) for r in roads]
    tr = [nd_rail(r["id"], r["depth_m"], r.get("trains_per_day"), r.get("passenger", False),
                  r.get("x"), r.get("y")) for r in railroads]
    ll = [nd_lifeline(r["id"], r["depth_m"], r.get("kind", ""), r.get("x"), r.get("y"))
          for r in lifelines]
    if method in nh.LETHALITY_METHODS:
        ss = [nd_structure(s["id"], s["depth_m"], s.get("vel_ms"), s.get("mobile"),
                           s.get("habitable", True), s.get("x"), s.get("y")) for s in structures]
        return nh.classify(ss, rd, tr, ll, list(downstream_dams), settings=st,
                           charts=charts if method == nh.LETHALITY_CHARTS else None)
    # comparison-only criteria: everything but the structures through nd_hazard, merged
    c = nh.classify((), rd, tr, ll, list(downstream_dams), settings=st, charts=None)
    extra = []
    for s in structures:
        f = _comparison_finding(s, method, rule_depth_ft * M_PER_FT, curve, wet_ft)
        if f is None:
            continue
        extra.append(f)
        if f.gap:
            c.data_gaps.append(f.gap)
        c.counts["structures_wet"] += 1
        if s.get("habitable", True):
            c.counts["habitable_wet"] += 1
            c.counts["habitable_lethal"] += f.level == nh.HIGH
    if extra:
        c.findings = extra + c.findings
        top = max(c.findings, key=lambda f: nh.RANK[f.level]).level
        c.hazard_class = top
        c.governing = [f for f in c.findings if f.level == top]
        c.borderline_raised = all(f.borderline for f in c.governing)
        c.notes = [n for n in c.notes if not n.startswith("Nothing listed in 5.2")]
    c.settings["lethality"] = method
    c.notes.insert(0, "Structures classified with '{}' - comparison only, not the ND "
                      "authority (ND 5.2.1.1 uses the ACER TM 11 charts).".format(
                          METHOD_LABELS[method]))
    return c
