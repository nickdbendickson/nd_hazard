"""Hazard classification per the North Dakota Dam Safety Standards Policy (REG_05.2026),
Sections 5.2 and 5.3, with the Reclamation (1988) depth-velocity flood danger charts
(ACER Technical Memorandum No. 11, "Downstream Hazard Classification Guidelines": Figure 2,
houses built on foundations = ND Figure 5.1; Figure 3, mobile homes = ND Figure 5.2).

ONE module for both plugins. nd_breach_runner and nd_dam_screening each carry an identical
copy of this file and of `acer_tm11_charts.json` next to it, so a dam gets the same class
from either tool. Only the standard library and numpy are used; no QGIS, no GDAL, no
package-relative imports. Keep the copies identical (MODULE_VERSION and the tests check it).

What the policy says and how it is applied here (section numbers are the policy's):

* 5.2.1.1 Habitable structures (dwellings, trailers and mobile homes, regularly occupied
  commercial/industrial/agricultural buildings, overnight recreation) are assumed occupied.
  - 2D model: potentially lethal conditions from the depth-velocity danger charts
    (Figure 5.1 houses, 5.2 mobile homes) -> HIGH.
  - 1D model: depth greater than 2 ft -> HIGH (the only case the policy allows depth alone).
  - Inundation at any depth -> economic loss (5.2.2) -> at least MEDIUM. The policy lets the
    Engineer judge low vs medium (Section 1.5); this module assigns MEDIUM and says so.
  - Damage only to agricultural land, uninhabited buildings or isolated unoccupied oil well
    pads -> LOW.
* 5.2.1.2 Roads, impacted if overtopped at any depth: AADT > 2,000 or interstate -> HIGH;
  AADT 500-2,000 -> MEDIUM; private or AADT < 500 -> LOW; sole access to an occupied
  location -> MEDIUM.
* 5.2.1.3 Railroads, impacted if overtopped at any depth: at least MEDIUM; freight of 10 or
  more trains a day or an active passenger line -> HIGH.
* 5.2.2 Lifeline facilities (public utilities, emergency response) whose effectiveness or
  access the breach reduces -> at least MEDIUM.
* 5.3 Borderline -> the higher class. Applied to the charts: the JUDGMENT ZONE is borderline,
  so it counts as potentially lethal (HIGH); a point within the digitizing tolerance of a
  higher zone takes that zone; a point beyond the chart's axes takes the high danger zone.
  Missing data (AADT, trains a day) is listed as a data gap, with the assumption used.
* 5.3 Cascading: at least as high as a downstream dam whose failure this dam could cause.
* 5.3 No allowance for evacuation or emergency action (none is made anywhere here).

"Any depth" needs a small numerical tolerance for model noise: `wet_ft` (default 0.1 ft).
Depth and velocity at a structure are the maxima at its location; they may occur at
different times, so the chart point is an upper bound (conservative).

The chart boundaries are polygons in (velocity ft/s, depth ft) digitized from the figures and
stored in `acer_tm11_charts.json` with the engineer who digitized and signed them off (one
engineer is enough since suite 1.14.1; a second checker is optional). Until that file says
"digitized" and names the engineer, the chart method refuses to run (ChartsNotDigitized) rather
than guess. The file shipped with the plugin is signed off (damsuite.tm11_curves.SIGNED_OFF_BY).
"""
import datetime
import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field, asdict
from typing import List, Optional

MODULE_VERSION = "1.1.0"
POLICY = "North Dakota Dam Safety Standards Policy REG_05.2026, Sections 5.2 and 5.3"
CHART_REFERENCE = ("Reclamation (1988), ACER Technical Memorandum No. 11, Downstream Hazard "
                   "Classification Guidelines: Fig. 2 houses built on foundations (ND Fig. 5.1), "
                   "Fig. 3 mobile homes (ND Fig. 5.2)")
CHART_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "acer_tm11_charts.json")

LOW, MEDIUM, HIGH = "Low", "Medium", "High"
RANK = {LOW: 0, MEDIUM: 1, HIGH: 2}
CLASSES = (LOW, MEDIUM, HIGH)

ZONE_LOW = "low danger"
ZONE_JUDGMENT = "judgment"
ZONE_HIGH = "high danger"
ZONE_ORDER = {ZONE_LOW: 0, ZONE_JUDGMENT: 1, ZONE_HIGH: 2}

LETHALITY_CHARTS = "acer_tm11"        # 2D model: Figures 5.1 / 5.2
LETHALITY_1D = "depth_2ft"            # 1D model: depth > 2 ft
LETHALITY_ALL_WET = "all_wet"         # no chart: every inundated habitable structure lethal
LETHALITY_METHODS = (LETHALITY_CHARTS, LETHALITY_1D, LETHALITY_ALL_WET)
LETHALITY_LABELS = {
    LETHALITY_CHARTS: "2D model: ACER TM 11 depth-velocity danger charts (ND Fig. 5.1/5.2)",
    LETHALITY_1D: "1D model: habitable structure deeper than 2 ft (ND 5.2.1.1)",
    LETHALITY_ALL_WET: "Conservative, no chart: every inundated habitable structure counted "
                       "as potentially lethal"}

CHART_HOUSE = "house"
CHART_MOBILE = "mobile"

M_PER_FT = 0.3048
MIN_WET_FT = 0.01


class ChartsNotDigitized(ValueError):
    """The chart file has no checked digitization of the ACER TM 11 figures."""


# ------------------------------------------------------------------------------ charts
def _point_in_polygon(x, y, poly):
    """Ray casting; points on an edge count as inside."""
    n = len(poly)
    inside = False
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        # on the edge?
        cross = (x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)
        if abs(cross) <= 1e-9 * max(1.0, abs(x2 - x1) + abs(y2 - y1)) and \
                min(x1, x2) - 1e-12 <= x <= max(x1, x2) + 1e-12 and \
                min(y1, y2) - 1e-12 <= y <= max(y1, y2) + 1e-12:
            return True
        if (y1 > y) != (y2 > y):
            xi = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xi:
                inside = not inside
    return inside


def _dist_to_polygon(x, y, poly, sx, sy):
    """Distance from (x, y) to the polygon's edges in axis-scaled units (x/sx, y/sy)."""
    best = float("inf")
    px, py = x / sx, y / sy
    n = len(poly)
    for i in range(n):
        ax, ay = poly[i][0] / sx, poly[i][1] / sy
        bx, by = poly[(i + 1) % n][0] / sx, poly[(i + 1) % n][1] / sy
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
        qx, qy = ax + t * dx, ay + t * dy
        best = min(best, ((px - qx) ** 2 + (py - qy) ** 2) ** 0.5)
    return best


@dataclass
class DangerChart:
    """One depth-velocity flood danger chart. Polygons are lists of [velocity_fts, depth_ft]
    covering the high danger zone and the judgment zone; the rest of the chart is low
    danger."""
    key: str
    title: str
    figure: str
    v_max_fts: float
    d_max_ft: float
    high: list
    judgment: list
    tolerance: float = 0.01          # digitizing tolerance, share of each axis range

    def validate(self):
        for name, poly in (("high", self.high), ("judgment", self.judgment)):
            if not poly or len(poly) < 3:
                raise ValueError("chart '{}': the {} zone polygon needs at least 3 points"
                                 .format(self.key, name))
            for v, d in poly:
                if not (-1e-6 <= v <= self.v_max_fts * 1.0001 and
                        -1e-6 <= d <= self.d_max_ft * 1.0001):
                    raise ValueError("chart '{}': point ({}, {}) of the {} zone is outside the "
                                     "chart axes".format(self.key, v, d, name))
        if self.v_max_fts <= 0 or self.d_max_ft <= 0:
            raise ValueError("chart '{}': axis maxima must be positive".format(self.key))
        return self

    def zone(self, depth_ft, vel_fts):
        """(zone, note). Beyond the axes -> high danger; within the digitizing tolerance of
        a higher zone -> that zone (ND 5.3: borderline takes the higher class)."""
        d = max(0.0, float(depth_ft))
        v = max(0.0, float(vel_fts))
        if d > self.d_max_ft or v > self.v_max_fts:
            return ZONE_HIGH, "beyond the chart ({:.1f} ft, {:.1f} ft/s; axes to {:g} ft, {:g} " \
                              "ft/s): taken as high danger".format(d, v, self.d_max_ft,
                                                                   self.v_max_fts)
        sx, sy = self.v_max_fts, self.d_max_ft
        if _point_in_polygon(v, d, self.high):
            return ZONE_HIGH, ""
        if _dist_to_polygon(v, d, self.high, sx, sy) <= self.tolerance:
            return ZONE_HIGH, "on the high danger boundary (within the digitizing tolerance)"
        if _point_in_polygon(v, d, self.judgment):
            return ZONE_JUDGMENT, ""
        if _dist_to_polygon(v, d, self.judgment, sx, sy) <= self.tolerance:
            return ZONE_JUDGMENT, "on the judgment zone boundary (within the digitizing " \
                                  "tolerance)"
        return ZONE_LOW, ""


@dataclass
class ChartSet:
    status: str = "not digitized"
    source: str = CHART_REFERENCE
    digitized_from: str = ""
    digitized_by: str = ""
    digitized_on: str = ""
    checked_by: str = ""
    signed_off_by: str = ""
    signed_off_on: str = ""
    notes: str = ""
    charts: dict = field(default_factory=dict)       # key -> DangerChart
    path: str = ""
    sha256: str = ""

    @property
    def signer(self):
        """The engineer who signed the charts off: signed_off_by, else the digitizer, else the
        checker. One name is enough (suite 1.14.1); a second checker is optional."""
        return (self.signed_off_by or "").strip() or (self.digitized_by or "").strip() or \
            (self.checked_by or "").strip()

    @property
    def signed_on(self):
        return (self.signed_off_on or "").strip() or (self.digitized_on or "").strip()

    @property
    def ready(self):
        return self.status == "digitized" and CHART_HOUSE in self.charts and \
            CHART_MOBILE in self.charts and bool(self.signer)

    def chart(self, mobile):
        if not self.ready:
            have = CHART_HOUSE in self.charts and CHART_MOBILE in self.charts
            raise ChartsNotDigitized(
                "The ACER TM 11 depth-velocity charts are not signed off in {} (status: "
                "'{}', digitized by '{}', signed off by '{}'). {}Load a signed-off chart "
                "file, or, for these 2D results, use the conservative method (every inundated "
                "habitable structure counted as potentially lethal). The 2 ft depth rule is only "
                "allowed for a 1D model (5.2.1.1).".format(
                    self.path or "the chart file", self.status, self.digitized_by,
                    self.signer,
                    "The zone polygons are in the file, but it does not record the engineer who "
                    "signed them off: fill in signed_off_by (or digitized_by) and set status "
                    "'digitized' (ND Dam Hazard > Chart sign-off writes the file that way; one "
                    "engineer is enough). " if have else ""))
        return self.charts[CHART_MOBILE if mobile else CHART_HOUSE]

    def info(self):
        return {"status": self.status, "source": self.source,
                "digitized_from": self.digitized_from, "digitized_by": self.digitized_by,
                "digitized_on": self.digitized_on, "checked_by": self.checked_by,
                "signed_off_by": self.signer, "signed_off_on": self.signed_on,
                "file": self.path, "sha256": self.sha256}


def load_charts(path=None):
    """ChartSet from a JSON file (default: acer_tm11_charts.json next to this module)."""
    path = path or CHART_FILE
    with open(path, "rb") as f:
        raw = f.read()
    d = json.loads(raw.decode("utf-8"))
    cs = ChartSet(status=d.get("status", "not digitized"), source=d.get("source", CHART_REFERENCE),
                  digitized_from=d.get("digitized_from", ""), digitized_by=d.get(
                      "digitized_by", ""), digitized_on=d.get("digitized_on", ""),
                  checked_by=d.get("checked_by", ""), signed_off_by=d.get("signed_off_by", ""),
                  signed_off_on=d.get("signed_off_on", ""), notes=d.get("notes", ""), path=path,
                  sha256=hashlib.sha256(raw).hexdigest())
    for key, c in (d.get("charts") or {}).items():
        if not c:
            continue
        cs.charts[key] = DangerChart(
            key=key, title=c.get("title", key), figure=c.get("figure", ""),
            v_max_fts=float(c["v_max_fts"]), d_max_ft=float(c["d_max_ft"]),
            high=[tuple(map(float, p)) for p in c["high"]],
            judgment=[tuple(map(float, p)) for p in c["judgment"]],
            tolerance=float(c.get("tolerance", 0.01))).validate()
    if cs.status == "digitized" and not (CHART_HOUSE in cs.charts and CHART_MOBILE in cs.charts):
        raise ValueError("{}: status is 'digitized' but the house and mobile home charts are "
                         "not both present".format(path))
    return cs


def save_charts(path, cs):
    d = {"status": cs.status, "source": cs.source, "digitized_from": cs.digitized_from,
         "digitized_by": cs.digitized_by, "digitized_on": cs.digitized_on,
         "checked_by": cs.checked_by, "signed_off_by": cs.signed_off_by,
         "signed_off_on": cs.signed_off_on, "notes": cs.notes,
         "charts": {k: {"title": c.title, "figure": c.figure, "v_max_fts": c.v_max_fts,
                        "d_max_ft": c.d_max_ft, "tolerance": c.tolerance,
                        "high": [list(p) for p in c.high],
                        "judgment": [list(p) for p in c.judgment]}
                    for k, c in cs.charts.items()}}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)
    return path


# ------------------------------------------------------------------------------ receptors
@dataclass
class Structure:
    id: str
    depth_ft: float                  # maximum flood depth at the structure (ground)
    vel_fts: float = 0.0             # maximum velocity at the structure
    habitable: bool = True           # unknown -> habitable (5.2.1.1: assumed occupied)
    mobile: bool = False             # trailer / mobile / manufactured home -> Fig. 5.2
    x: Optional[float] = None
    y: Optional[float] = None
    note: str = ""


@dataclass
class Road:
    id: str
    max_depth_ft: float              # deepest water over the road
    aadt: Optional[float] = None
    interstate: bool = False
    private: bool = False
    sole_access: bool = False        # sole access to an occupied location
    x: Optional[float] = None
    y: Optional[float] = None


@dataclass
class Railroad:
    id: str
    max_depth_ft: float
    trains_per_day: Optional[float] = None
    passenger: bool = False
    x: Optional[float] = None
    y: Optional[float] = None


@dataclass
class Lifeline:
    id: str
    max_depth_ft: float              # deepest water at the facility or its access
    kind: str = ""
    x: Optional[float] = None
    y: Optional[float] = None


@dataclass
class DownstreamDam:
    id: str
    hazard_class: str                # that dam's classification
    affected: bool = True            # this dam's failure could contribute to its failure
    note: str = ""


@dataclass
class Settings:
    lethality: str = LETHALITY_CHARTS
    wet_ft: float = 0.1              # "any depth", less model noise
    rule_depth_ft: float = 2.0       # 1D rule
    unknown_aadt: str = MEDIUM       # class used for an overtopped road with no AADT
    unknown_trains: str = MEDIUM     # class used for a railroad with no train count

    def validate(self):
        if self.lethality not in LETHALITY_METHODS:
            raise ValueError("unknown lethality method '{}'".format(self.lethality))
        if not (self.wet_ft >= MIN_WET_FT) or self.rule_depth_ft <= 0:
            raise ValueError("the wet depth must be at least {:g} ft (a dry cell reads 0 ft, so 0 "
                             "would count every structure and road as inundated) and the rule "
                             "depth positive".format(MIN_WET_FT))
        for v in (self.unknown_aadt, self.unknown_trains):
            if v not in RANK:
                raise ValueError("unknown class '{}'".format(v))
        return self


# ------------------------------------------------------------------------------ attributes
# Shared by both plugins so the same layer gives the same receptors.
UNINHABITED_KEYWORDS = ("uninhabited", "unoccupied", "shed", "garage", "barn", "silo",
                        "grain bin", "outbuilding", "well pad", "oil well", "pump house")
_YES = ("y", "yes", "true", "1")
_NO = ("n", "no", "false", "0")


def number(v):
    """float or None (None, blank, text, NaN, inf)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(str(v).strip()) if isinstance(v, str) else float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def aadt_value(v):
    """AADT or None when not usable (missing, NaN, zero or negative: not counted)."""
    x = number(v)
    return x if x is not None and x > 0 else None


def trains_value(v):
    x = number(v)
    return x if x is not None and x >= 0 else None


def yes_no(v):
    """True / False / None (not a yes/no value)."""
    if isinstance(v, bool):
        return v
    t = str(v).strip().lower() if v is not None else ""
    if t in _YES:
        return True
    if t in _NO:
        return False
    return None


def habitable_from_text(text, uninhabited=UNINHABITED_KEYWORDS):
    """5.2.1.1 with 5.3: a structure is habitable unless its occupancy text clearly names an
    uninhabited building (barn, shed, garage ...). Blank or unknown -> habitable."""
    t = str(text or "").strip().lower()
    if not t or t == "null":
        return True
    return not any(k.strip().lower() and k.strip().lower() in t for k in uninhabited)


def mobile_from_text(text):
    t = str(text or "").strip().lower()
    return yes_no(t) is True or any(k in t for k in ("mobile", "manufactured", "trailer")) or \
        bool(re.search(r"(?<![a-z0-9])res2(?![0-9])", t))


_INTERSTATE = re.compile(r"^\s*(interstate|i|ih)\s*[-\s]?\s*\d+", re.I)


def interstate_from_text(text):
    """'Interstate 94', 'I-94', 'I 94', 'I94', 'IH-29', or the TIGER route type 'I'."""
    t = str(text or "").strip()
    return t.upper() == "I" or "interstate" in t.lower() or bool(_INTERSTATE.match(t))


def passenger_from_value(v, codes=("A", "B", "C")):
    """FRA PASSNGR code in `codes`, or yes/true/1. 'T' (tourist) and 'X' are not."""
    t = str(v or "").strip()
    if t.upper() in {c.strip().upper() for c in codes if c.strip()}:
        return True
    return yes_no(t) is True and t.lower() not in ("t",)


def class_from_text(text):
    """Low / Medium / High from a class text (Significant and Intermediate = Medium; NID codes
    H/S/L), or None."""
    t = str(text or "").strip().lower()
    return {"low": LOW, "l": LOW, "significant": MEDIUM, "s": MEDIUM, "medium": MEDIUM,
            "m": MEDIUM, "intermediate": MEDIUM, "high": HIGH, "h": HIGH}.get(t)


# ------------------------------------------------------------------------------ sampling
# Rasters as numpy arrays with a north-up transform: x0 = left edge, y_top = top edge,
# cell = cell size (square cells). Depth and velocity arrays hold NaN or 0 where dry.
def _cell(x, y, x0, y_top, cell, shape):
    try:
        if not (math.isfinite(x) and math.isfinite(y)):
            return None                  # Runner 3.4.1 / Screening 0.11.1: NaN / inf = "outside the grid"
    except TypeError:
        return None
    c = int(math.floor((x - x0) / cell))
    r = int(math.floor((y_top - y) / cell))
    return (r, c) if 0 <= r < shape[0] and 0 <= c < shape[1] else None


def _val(a, rc):
    if a is None or rc is None:
        return None
    v = float(a[rc])
    return v if math.isfinite(v) else None


def sample_point(depth, vel, x0, y_top, cell, x, y):
    """(depth, velocity) at a point; (None, None) outside the grid; NaN -> None."""
    rc = _cell(x, y, x0, y_top, cell, depth.shape)
    if rc is None:
        return None, None
    return _val(depth, rc), _val(vel, rc)


def _inside_mask(xs, ys, ring):
    import numpy as np
    inside = np.zeros(xs.shape, bool)
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if y1 == y2:
            continue
        cond = (y1 > ys) != (y2 > ys)
        xi = x1 + (ys - y1) * (x2 - x1) / (y2 - y1)
        inside ^= cond & (xs < xi)
    return inside


def sample_footprint(depth, vel, x0, y_top, cell, ring, wet_ft=0.1, to_ft=1.0):
    """A structure polygon (exterior ring [(x, y), ...]): (depth at the deepest cell whose
    centre is inside, the maximum velocity over the wet cells inside, how). A footprint with
    no cell centre inside uses the cell under its centroid. Depth units follow the arrays;
    `to_ft` converts them to ft for the wet test."""
    import numpy as np
    ring = [p for p in (ring or ()) if math.isfinite(p[0]) and math.isfinite(p[1])]
    if not ring:                         # 3.4.1 / 0.11.1: empty or all-NaN ring: no sample
        return None, None, "footprint has no valid vertex"
    xs_ = [p[0] for p in ring]
    ys_ = [p[1] for p in ring]
    c0 = max(0, int(math.floor((min(xs_) - x0) / cell)))
    c1 = min(depth.shape[1] - 1, int(math.floor((max(xs_) - x0) / cell)))
    r0 = max(0, int(math.floor((y_top - max(ys_)) / cell)))
    r1 = min(depth.shape[0] - 1, int(math.floor((y_top - min(ys_)) / cell)))
    if c0 <= c1 and r0 <= r1:
        cc, rr = np.meshgrid(np.arange(c0, c1 + 1), np.arange(r0, r1 + 1))
        xs = x0 + (cc + 0.5) * cell
        ys = y_top - (rr + 0.5) * cell
        m = _inside_mask(xs, ys, ring)
        if m.any():
            d = np.asarray(depth[r0:r1 + 1, c0:c1 + 1], float)[m]
            d = np.where(np.isfinite(d), d, -np.inf)
            k = int(np.argmax(d))
            dmax = float(d[k]) if np.isfinite(d[k]) else None
            vmax = None
            if vel is not None:
                v = np.asarray(vel[r0:r1 + 1, c0:c1 + 1], float)[m]
                wet = np.isfinite(d) & (d * to_ft >= wet_ft) & np.isfinite(v)
                if wet.any():
                    vmax = float(v[wet].max())
                elif dmax is not None and np.isfinite(v[k]):
                    vmax = float(v[k])
            return dmax, vmax, "deepest of {} cells inside".format(int(m.sum()))
    cx, cy = sum(xs_) / len(xs_), sum(ys_) / len(ys_)
    d, v = sample_point(depth, vel, x0, y_top, cell, cx, cy)
    return d, v, "no cell centre inside: cell under the centroid"


def sample_line_max(depth, x0, y_top, cell, vertices):
    """Deepest water along a line, sampled every half cell; None when entirely outside."""
    best = None
    step = 0.5 * cell
    for (xa, ya), (xb, yb) in zip(vertices[:-1], vertices[1:]):
        L = math.hypot(xb - xa, yb - ya)
        n = max(1, int(math.ceil(L / step)))
        for i in range(n + 1):
            t = i / n
            d, _ = sample_point(depth, None, x0, y_top, cell, xa + t * (xb - xa),
                                ya + t * (yb - ya))
            if d is not None and (best is None or d > best):
                best = d
    if best is None and len(vertices) == 1:
        best, _ = sample_point(depth, None, x0, y_top, cell, *vertices[0])
    return best


# ------------------------------------------------------------------------------ result
@dataclass
class Finding:
    level: str
    section: str
    kind: str                        # structure / road / railroad / lifeline / dam
    id: str
    reason: str
    zone: str = ""
    borderline: bool = False
    depth_ft: Optional[float] = None
    vel_fts: Optional[float] = None
    x: Optional[float] = None
    y: Optional[float] = None
    gap: str = ""


@dataclass
class Classification:
    hazard_class: str
    governing: List[Finding]
    findings: List[Finding]
    borderline_raised: bool
    data_gaps: List[str]
    notes: List[str]
    settings: dict
    charts: dict
    counts: dict
    module_version: str = MODULE_VERSION
    policy: str = POLICY
    created: str = ""

    def to_dict(self):
        d = asdict(self)
        return d

    def reasons(self):
        return ["{} ({})".format(f.reason, f.section) for f in self.governing]


def road_class(r, st):
    """(class, reason, gap or '')."""
    r = Road(r.id, r.max_depth_ft, aadt_value(r.aadt), r.interstate, r.private, r.sole_access,
             r.x, r.y)
    if r.interstate:
        return HIGH, "interstate overtopped", ""
    if r.aadt is not None:
        if r.aadt > 2000:
            return HIGH, "road with AADT {:,.0f} (> 2,000) overtopped".format(r.aadt), ""
        if r.aadt >= 500:
            lvl, why = MEDIUM, "road with AADT {:,.0f} (500-2,000) overtopped".format(r.aadt)
        else:
            lvl, why = LOW, "road with AADT {:,.0f} (< 500) overtopped".format(r.aadt)
    elif r.private:
        lvl, why = LOW, "private road overtopped"
    else:
        lvl = st.unknown_aadt
        why = "road overtopped, AADT unknown (assumed {})".format(lvl)
        gap = "road {}: AADT not given; classified {} by assumption".format(r.id, lvl)
        if r.sole_access and RANK[MEDIUM] > RANK[lvl]:
            return MEDIUM, why + "; sole access to an occupied location", gap
        return lvl, why, gap
    if r.sole_access and RANK[MEDIUM] > RANK[lvl]:
        return MEDIUM, why + "; sole access to an occupied location", ""
    return lvl, why, ""


def rail_class(r, st):
    r = Railroad(r.id, r.max_depth_ft, trains_value(r.trains_per_day), r.passenger, r.x, r.y)
    if r.passenger:
        return HIGH, "active passenger railroad overtopped", ""
    if r.trains_per_day is not None:
        if r.trains_per_day >= 10:
            return HIGH, "railroad with {:g} trains a day (>= 10) overtopped".format(
                r.trains_per_day), ""
        return MEDIUM, "railroad with {:g} trains a day overtopped".format(r.trains_per_day), ""
    lvl = max(MEDIUM, st.unknown_trains, key=lambda c: RANK[c])
    return lvl, "railroad overtopped, trains a day unknown (assumed {})".format(lvl), \
        "railroad {}: trains a day not given; classified {} by assumption".format(r.id, lvl)


def _dry(depth_ft, wet_ft):
    """None, NaN or shallower than wet_ft."""
    try:
        d = float(depth_ft)
    except (TypeError, ValueError):
        return True
    return not (d >= wet_ft)             # NaN compares False -> dry


def structure_finding(s, st, charts=None):
    """Finding for one structure, or None when it is dry (below wet_ft)."""
    if _dry(s.depth_ft, st.wet_ft):
        return None
    vv = number(s.vel_fts)
    kw = dict(kind="structure", id=str(s.id), depth_ft=round(float(s.depth_ft), 2),
              vel_fts=None if vv is None else round(vv, 2), x=s.x, y=s.y)
    if not s.habitable:
        return Finding(LOW, "5.2.1.1", reason="uninhabited building inundated "
                       "({:.1f} ft)".format(s.depth_ft), **kw)
    what = "mobile home" if s.mobile else "habitable structure"
    if st.lethality == LETHALITY_1D:
        if s.depth_ft > st.rule_depth_ft:
            return Finding(HIGH, "5.2.1.1", reason="{} inundated {:.1f} ft (> {:g} ft, 1D "
                           "rule)".format(what, s.depth_ft, st.rule_depth_ft), **kw)
        return Finding(MEDIUM, "5.2.2", reason="{} inundated {:.1f} ft: economic loss"
                       .format(what, s.depth_ft), **kw)
    if st.lethality == LETHALITY_ALL_WET:
        return Finding(HIGH, "5.2.1.1/5.3", reason="{} inundated {:.1f} ft, {:.1f} ft/s: counted "
                       "potentially lethal (no chart used)".format(what, s.depth_ft,
                                                                   vv or 0.0),
                       borderline=True, **kw)
    ch = charts.chart(s.mobile)
    if number(s.vel_fts) is None:
        return Finding(HIGH, "5.2.1.1, 5.3", reason="{} inundated {:.1f} ft, velocity unknown: "
                       "borderline, taken as potentially lethal".format(
                           "mobile home" if s.mobile else "habitable structure", s.depth_ft),
                       borderline=True, gap="structure {}: no velocity at its location; "
                       "counted potentially lethal".format(s.id),
                       **dict(kw, vel_fts=None))
    zone, note = ch.zone(s.depth_ft, s.vel_fts)
    fig = "Fig. 5.2" if s.mobile else "Fig. 5.1"
    pt = "{:.1f} ft at {:.1f} ft/s".format(s.depth_ft, s.vel_fts)
    if zone == ZONE_HIGH:
        return Finding(HIGH, "5.2.1.1 " + fig, reason="{} in the high danger zone ({}){}".format(
            what, pt, "; " + note if note else ""), zone=zone, borderline=bool(note), **kw)
    if zone == ZONE_JUDGMENT:
        return Finding(HIGH, "5.2.1.1 " + fig + ", 5.3", reason="{} in the judgment zone ({}): "
                       "borderline, the higher class applies{}".format(
                           what, pt, "; " + note if note else ""), zone=zone, borderline=True,
                       **kw)
    return Finding(MEDIUM, "5.2.2", reason="{} inundated, low danger zone ({}): economic loss"
                   .format(what, pt), zone=zone, **kw)


def classify(structures=(), roads=(), railroads=(), lifelines=(), downstream_dams=(),
             settings=None, charts=None):
    """Classification of one dam from what its breach flood reaches."""
    st = (settings or Settings()).validate()
    if st.lethality == LETHALITY_CHARTS and charts is None:
        charts = load_charts()
    if st.lethality == LETHALITY_CHARTS and not charts.ready:
        charts.chart(False)                  # raises ChartsNotDigitized with the reason
    findings, gaps, notes = [], [], []
    counts = {"structures_wet": 0, "habitable_wet": 0, "habitable_lethal": 0,
              "judgment_zone": 0, "roads_overtopped": 0, "railroads_overtopped": 0,
              "lifelines_wet": 0, "downstream_dams": 0}
    for s in structures:
        f = structure_finding(s, st, charts)
        if f is None:
            continue
        findings.append(f)
        if f.gap:
            gaps.append(f.gap)
        counts["structures_wet"] += 1
        if s.habitable:
            counts["habitable_wet"] += 1
            if f.level == HIGH:
                counts["habitable_lethal"] += 1
            if f.zone == ZONE_JUDGMENT:
                counts["judgment_zone"] += 1
    for r in roads:
        if _dry(r.max_depth_ft, st.wet_ft):
            continue
        counts["roads_overtopped"] += 1
        lvl, why, gap = road_class(r, st)
        findings.append(Finding(lvl, "5.2.1.2", "road", str(r.id), why + " ({:.1f} ft)".format(
            r.max_depth_ft), depth_ft=round(r.max_depth_ft, 2), x=r.x, y=r.y,
            borderline=bool(gap)))
        if gap:
            gaps.append(gap)
    for r in railroads:
        if _dry(r.max_depth_ft, st.wet_ft):
            continue
        counts["railroads_overtopped"] += 1
        lvl, why, gap = rail_class(r, st)
        findings.append(Finding(lvl, "5.2.1.3", "railroad", str(r.id), why + " ({:.1f} ft)"
                                .format(r.max_depth_ft), depth_ft=round(r.max_depth_ft, 2),
                                x=r.x, y=r.y, borderline=bool(gap)))
        if gap:
            gaps.append(gap)
    for L in lifelines:
        if _dry(L.max_depth_ft, st.wet_ft):
            continue
        counts["lifelines_wet"] += 1
        findings.append(Finding(MEDIUM, "5.2.2", "lifeline", str(L.id), "lifeline facility{} "
                                "inundated ({:.1f} ft)".format(" (" + L.kind + ")" if L.kind
                                                               else "", L.max_depth_ft),
                                depth_ft=round(L.max_depth_ft, 2), x=L.x, y=L.y))
    for dd in downstream_dams:
        if not dd.affected:
            continue
        c = class_from_text(dd.hazard_class)
        if c is None:
            c = MEDIUM
            gaps.append("downstream dam {}: class '{}' not recognised; taken as Medium (5.3: "
                        "classify it)".format(dd.id, dd.hazard_class))
        counts["downstream_dams"] += 1
        findings.append(Finding(c, "5.3 cascading", "dam", str(dd.id), "downstream dam {} ({}) "
                                "could fail as a result{}".format(dd.id, c, "; " + dd.note
                                                                  if dd.note else ""),
                                borderline=class_from_text(dd.hazard_class) is None))
    if findings:
        top = max(findings, key=lambda f: RANK[f.level]).level
        governing = [f for f in findings if f.level == top]
    else:
        top, governing = LOW, []
        notes.append("Nothing listed in 5.2 is reached: no habitable structure, road, "
                     "railroad or lifeline facility inundated (5.2.1.1: damage only to "
                     "agricultural land, uninhabited buildings or unoccupied well pads is low).")
    raised = bool(governing) and all(f.borderline for f in governing)
    if top == MEDIUM and any(f.section == "5.2.2" for f in governing) and \
            all(f.section == "5.2.2" for f in governing):
        notes.append("Medium on economic loss or lifeline impact only: the policy lets the "
                     "Engineer judge low or medium here (Section 1.5).")
    if st.lethality == LETHALITY_CHARTS:
        notes.append("Chart points use the maximum depth and the maximum velocity at each "
                     "structure, which may not occur together: an upper bound.")
    if st.lethality == LETHALITY_ALL_WET:
        notes.append("No depth-velocity chart was used: every inundated habitable structure "
                     "was counted as potentially lethal. This over-classifies; use the charts.")
    notes.append("No allowance for evacuation or emergency action (5.3).")
    if st.wet_ft > 0:
        notes.append("'Any depth' is taken as {:.2f} ft or more (model noise below that).".format(
            st.wet_ft))
    return Classification(
        hazard_class=top, governing=governing, findings=findings, borderline_raised=raised,
        data_gaps=gaps, notes=notes, settings=asdict(st),
        charts=charts.info() if charts is not None else {}, counts=counts,
        created=datetime.datetime.now().isoformat(timespec="seconds"))


# ------------------------------------------------------------------------------ output
def class_label(c):
    """Human-readable class with a note when the conservative rule is the only reason for High."""
    cls = c.hazard_class
    if cls == HIGH and c.settings.get("lethality") == LETHALITY_ALL_WET:
        return "{} (conservative: TM 11 chart not signed off)".format(cls)
    return cls


def markdown(c, dam_id="", title="Hazard classification"):
    lbl = class_label(c)
    lines = ["# {}{}".format(title, ": " + dam_id if dam_id else ""), "",
             "**Class: {}**".format(lbl.upper()), "",
             "Basis: {}. Lethality: {}.".format(c.policy, LETHALITY_LABELS.get(
                 c.settings.get("lethality"), c.settings.get("lethality"))), ""]
    if c.charts:
        lines += ["Charts: {} (status {}, digitized by {}, signed off by {}{}, SHA-256 {}).".format(
            c.charts.get("source"), c.charts.get("status"), c.charts.get("digitized_by") or "-",
            c.charts.get("signed_off_by") or c.charts.get("checked_by") or "-",
            " on " + c.charts["signed_off_on"] if c.charts.get("signed_off_on") else "",
            (c.charts.get("sha256") or "")[:12]), ""]
    if c.governing:
        lines += ["## Governing", ""]
        lines += ["- {} ({}){}".format(f.reason, f.section,
                                       " - borderline, higher class assigned (5.3)"
                                       if f.borderline else "") for f in c.governing[:30]]
        if len(c.governing) > 30:
            lines.append("- ... and {} more".format(len(c.governing) - 30))
        lines.append("")
    lines += ["## Counts", ""] + ["- {}: {}".format(k.replace("_", " "), v)
                                  for k, v in c.counts.items()] + [""]
    if c.data_gaps:
        lines += ["## Data gaps", ""] + ["- " + g for g in c.data_gaps] + [""]
    lines += ["## Notes", ""] + ["- " + n for n in c.notes] + [""]
    gov = {id(f) for f in c.governing}
    other = [f for f in c.findings if id(f) not in gov]
    if other:
        lines += ["## Other findings", "", "| Level | Kind | ID | Reason | Section |",
                  "|---|---|---|---|---|"]
        lines += ["| {} | {} | {} | {} | {} |".format(f.level, f.kind, f.id, f.reason, f.section)
                  for f in other[:200]]
        lines.append("")
    lines.append("Module {} ({}). The official classification is made by the ND Department of "
                 "Water Resources.".format(c.module_version, c.created))
    return "\n".join(lines) + "\n"


FINDING_COLUMNS = ["level", "section", "kind", "id", "reason", "zone", "borderline",
                   "depth_ft", "vel_fts", "x", "y"]


def finding_rows(c):
    return [{k: getattr(f, k) for k in FINDING_COLUMNS} for f in c.findings]


def chart_svg(chart, structures=(), width=560, height=380, title=None):
    """SVG of one danger chart with its zones and the structures on it (review figure)."""
    ml, mr, mt, mb = 56, 16, 30, 44
    W, H = width - ml - mr, height - mt - mb
    sx = lambda v: ml + W * min(v, chart.v_max_fts) / chart.v_max_fts
    sy = lambda d: mt + H - H * min(d, chart.d_max_ft) / chart.d_max_ft
    poly = lambda p: " ".join("{:.1f},{:.1f}".format(sx(v), sy(d)) for v, d in p)
    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="{}" height="{}" '
           'font-family="Helvetica,Arial,sans-serif" font-size="11">'.format(width, height),
           '<rect width="100%" height="100%" fill="#ffffff"/>',
           '<text x="{}" y="18" font-size="13" font-weight="bold">{}</text>'.format(
               ml, _esc(title or chart.title)),
           '<rect x="{}" y="{}" width="{}" height="{}" fill="#e8f3e8" stroke="#555"/>'.format(
               ml, mt, W, H),
           '<polygon points="{}" fill="#fbe7b5" stroke="#b58400"/>'.format(poly(chart.judgment)),
           '<polygon points="{}" fill="#f4b6b0" stroke="#a3271c"/>'.format(poly(chart.high))]
    for i in range(6):
        v = chart.v_max_fts * i / 5.0
        d = chart.d_max_ft * i / 5.0
        out.append('<text x="{:.1f}" y="{}" text-anchor="middle">{:g}</text>'.format(
            sx(v), mt + H + 14, round(v, 2)))
        out.append('<text x="{}" y="{:.1f}" text-anchor="end">{:g}</text>'.format(
            ml - 4, sy(d) + 4, round(d, 2)))
    out.append('<text x="{}" y="{}" text-anchor="middle">Velocity (ft/s)</text>'.format(
        ml + W / 2, height - 8))
    out.append('<text x="14" y="{}" transform="rotate(-90 14 {})" text-anchor="middle">'
               'Depth (ft)</text>'.format(mt + H / 2, mt + H / 2))
    for s in structures:
        v, d = float(s.vel_fts or 0.0), float(s.depth_ft or 0.0)
        beyond = v > chart.v_max_fts or d > chart.d_max_ft
        out.append('<circle cx="{:.1f}" cy="{:.1f}" r="3.2" fill="{}" stroke="#000" '
                   'stroke-width="0.6"><title>{}: {:.1f} ft, {:.1f} ft/s</title></circle>'.format(
                       sx(v), sy(d), "#000" if beyond else "#1f4e9c", _esc(s.id), d, v))
    lx = ml + W - 150
    for i, (lab, col) in enumerate((("High danger", "#f4b6b0"), ("Judgment", "#fbe7b5"),
                                    ("Low danger", "#e8f3e8"))):
        out.append('<rect x="{}" y="{}" width="12" height="10" fill="{}" stroke="#555"/>'
                   '<text x="{}" y="{}">{}</text>'.format(lx, mt + 8 + 15 * i, col, lx + 16,
                                                         mt + 17 + 15 * i, lab))
    out.append('</svg>')
    return "\n".join(out)


def _esc(t):
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
