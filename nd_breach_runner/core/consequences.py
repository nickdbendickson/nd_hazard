"""Life-safety and economic consequences of a breach flood, incremental.

For a set of structures (points; a polygon is represented by its centroid) this
samples the with-breach depth, velocity and arrival rasters and, when given, the
same flood without the breach, and estimates for each case:

* direct damage: structure and contents, from depth-damage curves on the depth
  above the first floor, with 100% damage where depth x velocity or the depth
  itself passes a "destroyed" threshold;
* population at risk (PAR): occupants of structures the flood reaches;
* warning time: arrival time minus a detection / notification lag, in classes;
* estimated loss of life: PAR x a fatality rate looked up by flood severity and
  warning class.

The incremental result is with-breach minus without-breach, structure by
structure, never negative, then summed. ND REG_05.2026 6.4 is about the
incremental effect of the breach; this module only produces the with-minus-
without numbers and checks no criterion of that section.

READ THIS BEFORE USING THE NUMBERS
----------------------------------
* The built-in depth-damage curves are GENERIC PLACEHOLDERS: round illustrative
  numbers, not taken from any published source. Replace them with the curves
  your reviewer accepts (CSV: occupancy, depth_ft, structure_pct, contents_pct).
* The built-in fatality-rate table is an APPROXIMATE DEFAULT written from memory
  of the style of the Graham (1999) / Reclamation RCEM guidance. The numbers
  are not authoritative and MUST be checked against the current published
  tables (or replaced with the table you are required to use) before any
  submittal. Supply your own table as CSV: severity, none, short, some, adequate.
* The thresholds for "destroyed", flood severity and PAR are user-adjustable
  placeholders, not standards. The default destruction threshold on depth x
  velocity (75 ft2/s, about 7 m2/s) is from a recollection of European
  literature and is unverified. Do not confuse it with the 7 ft2/s people-
  stability figure used in impacts.py.
* Loss of life is an expected value (fractional persons) for screening, not a
  prediction. Basement (below-grade) damage is not represented.

Units: the module works in US customary units (ft, ft/s, hours, dollars),
like the rasters in a dam folder. Arrays in SI (m, m/s, hours), as the run
workflow keeps them, are accepted with units="si".

Pure numpy and text; no QGIS needed. Python 3.9 compatible.
"""
import csv
import math
import numbers
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

from .units import FT

# --------------------------------------------------------------------------------------
# occupancy, foundation, defaults
# --------------------------------------------------------------------------------------
OCC_RESIDENTIAL = "residential"
OCC_COMMERCIAL = "commercial"
OCC_INDUSTRIAL = "industrial"
OCC_PUBLIC = "public"
OCC_AGRICULTURAL = "agricultural"
OCC_OTHER = "other"
OCCUPANCIES = (OCC_RESIDENTIAL, OCC_COMMERCIAL, OCC_INDUSTRIAL, OCC_PUBLIC,
               OCC_AGRICULTURAL, OCC_OTHER)

FOUNDATIONS = ("slab", "crawlspace", "basement", "pier", "other")

# first-floor height above grade (ft) used when the structure has none: generic placeholders
FFH_BY_OCCUPANCY_FT = {OCC_RESIDENTIAL: 1.0, OCC_COMMERCIAL: 0.5, OCC_INDUSTRIAL: 0.5,
                       OCC_PUBLIC: 0.5, OCC_AGRICULTURAL: 0.0, OCC_OTHER: 0.5}
# a known foundation type overrides the occupancy default (still a generic placeholder)
FFH_BY_FOUNDATION_FT = {"slab": 0.5, "crawlspace": 2.0, "basement": 3.0, "pier": 4.0}

SEVERITIES = ("low", "medium", "high")
WARNING_KEYS = ("none", "short", "some", "adequate")

CURVE_SOURCE_GENERIC = "generic placeholder"

_OCC_PREFIXES = (("res", OCC_RESIDENTIAL), ("com", OCC_COMMERCIAL), ("ind", OCC_INDUSTRIAL),
                 ("gov", OCC_PUBLIC), ("edu", OCC_PUBLIC), ("rel", OCC_PUBLIC),
                 ("agr", OCC_AGRICULTURAL))
_OCC_WORDS = (("resid", OCC_RESIDENTIAL), ("home", OCC_RESIDENTIAL), ("house", OCC_RESIDENTIAL),
              ("dwelling", OCC_RESIDENTIAL), ("mobile", OCC_RESIDENTIAL),
              ("apartment", OCC_RESIDENTIAL), ("commerc", OCC_COMMERCIAL),
              ("retail", OCC_COMMERCIAL), ("office", OCC_COMMERCIAL), ("industr", OCC_INDUSTRIAL),
              ("manufact", OCC_INDUSTRIAL), ("warehouse", OCC_INDUSTRIAL),
              ("public", OCC_PUBLIC), ("school", OCC_PUBLIC), ("church", OCC_PUBLIC),
              ("government", OCC_PUBLIC), ("hospital", OCC_PUBLIC), ("agri", OCC_AGRICULTURAL),
              ("farm", OCC_AGRICULTURAL), ("barn", OCC_AGRICULTURAL))


def normalize_occupancy(text):
    """One of OCCUPANCIES for free text or an NSI-style code ('RES1-1SNB' -> residential).
    Unknown or empty text gives 'other'."""
    t = str(text or "").strip().lower()
    if not t:
        return OCC_OTHER
    if t in OCCUPANCIES:
        return t
    for word, occ in _OCC_WORDS:
        if word in t:
            return occ
    for pre, occ in _OCC_PREFIXES:
        if t.startswith(pre):
            return occ
    return OCC_OTHER


def normalize_foundation(text):
    """One of FOUNDATIONS ('' and unknown give 'other'). Single letters follow the NSI style
    (S slab, C crawlspace, B basement, P pier)."""
    t = str(text or "").strip().lower()
    if not t:
        return "other"
    if t in FOUNDATIONS:
        return t
    if t in ("s", "slab on grade", "slab-on-grade", "slab_on_grade"):
        return "slab"
    if t in ("c", "crawl", "crawl space", "crawl_space") or t.startswith("crawl"):
        return "crawlspace"
    if t in ("b", "bsmt") or t.startswith("basement"):
        return "basement"
    if t in ("p", "piers", "pile", "piles", "stilts") or t.startswith("pier"):
        return "pier"
    return "other"


# --------------------------------------------------------------------------------------
# built-in curves and table (PLACEHOLDERS: see the module docstring)
# --------------------------------------------------------------------------------------
_CURVE_DEPTHS = [0.0, 1.0, 2.0, 4.0, 6.0, 8.0, 12.0]
GENERIC_CURVES = {
    OCC_RESIDENTIAL: {"structure_pct": [0, 15, 25, 40, 55, 70, 90],
                      "contents_pct": [0, 10, 25, 45, 60, 75, 90]},
    OCC_COMMERCIAL: {"structure_pct": [0, 10, 20, 35, 50, 60, 80],
                     "contents_pct": [0, 15, 30, 50, 70, 85, 100]},
    OCC_INDUSTRIAL: {"structure_pct": [0, 8, 15, 30, 45, 55, 75],
                     "contents_pct": [0, 10, 25, 45, 65, 80, 95]},
    OCC_PUBLIC: {"structure_pct": [0, 10, 20, 35, 50, 60, 80],
                 "contents_pct": [0, 10, 20, 40, 60, 75, 90]},
    OCC_AGRICULTURAL: {"structure_pct": [0, 5, 10, 20, 30, 40, 60],
                       "contents_pct": [0, 10, 20, 40, 60, 80, 100]},
    OCC_OTHER: {"structure_pct": [0, 10, 20, 35, 50, 60, 80],
                "contents_pct": [0, 15, 30, 50, 70, 85, 100]},
}

# Fatality rate (fraction of PAR) by flood severity (rows) and warning class (columns).
# "none" is no warning (arrival before the notification lag ends) and "short" is under the
# first edge (default 15 min): both are the "no warning / under 15 min" column of the
# Graham (1999) style tables. "some" is 15-60 min and "adequate" over 60 min by default.
# APPROXIMATE, FROM MEMORY OF THE PUBLISHED GUIDANCE: check before use.
DEFAULT_FATALITY_TABLE = {
    "low": {"none": 0.01, "short": 0.01, "some": 0.007, "adequate": 0.0003},
    "medium": {"none": 0.15, "short": 0.15, "some": 0.04, "adequate": 0.03},
    "high": {"none": 0.75, "short": 0.75, "some": 0.15, "adequate": 0.04},
}

DISCLAIMER = (
    "Built-in depth-damage curves are GENERIC PLACEHOLDERS (round illustrative numbers, not "
    "from a published source). The built-in fatality rates are APPROXIMATE DEFAULTS written "
    "from memory of the Graham (1999) / Reclamation RCEM style of guidance: they are not "
    "authoritative and MUST be checked against the current published tables, or replaced by "
    "the table you must use, before any submittal. The thresholds for destroyed structures, "
    "flood severity and population at risk are user-adjustable placeholders, not standards. "
    "Loss of life is an expected value (fractional persons) for screening only.")

INCREMENTAL_NOTE = (
    "Incremental = with the breach minus without it, structure by structure, never negative, "
    "then summed (the ND REG_05.2026 6.4 idea of incremental damage; no criterion of that "
    "section is checked here).")


# --------------------------------------------------------------------------------------
# parameters
# --------------------------------------------------------------------------------------
@dataclass
class ConsequenceParams:
    """Every threshold is a user-adjustable placeholder, not a standard."""
    persons_per_unit: float = 2.5            # residential unit count -> occupants
    default_units_residential: float = 1.0   # units assumed when a residential structure has none
    population: str = "night"                # "night", "day" or "max" of the two
    contents_ratio: float = 0.5              # contents value / structure value when not given
    default_occupancy: str = OCC_RESIDENTIAL  # when the layer has no occupancy
    wet_ft: float = 0.01                     # depth at which a structure counts as flooded
    par_depth_ft: float = 0.5                # depth above grade at which occupants are at risk
    destroy_dv_ft2s: float = 75.0            # depth x velocity: destroyed (about 7 m2/s)
    destroy_depth_ft: float = 20.0           # depth above grade: destroyed
    sev_med_depth_ft: float = 3.0            # flood severity "medium" from this depth ...
    sev_med_dv_ft2s: float = 7.0             # ... or this depth x velocity
    sev_high_depth_ft: float = 20.0          # severity "high" from this depth ...
    sev_high_dv_ft2s: float = 75.0           # ... or this depth x velocity
    lag_hr: float = 0.0                      # detection / notification lag after breach initiation
    baseline_lag_hr: Optional[float] = None  # lag for the no-breach flood (None = lag_hr)
    warning_edges_min: tuple = (15.0, 60.0)  # end of "short" and of "some" warning (min)
    road_wet_ft: float = 0.5                 # road / rail counts as flooded from this depth
    vehicle_window_hr: float = 1.0           # hours of traffic exposed at a flooded road
    include_dry: bool = False                # keep structures the flood never reaches in the table

    def validate(self):
        if self.population not in ("night", "day", "max"):
            raise ValueError("population must be 'night', 'day' or 'max'")
        for k in ("persons_per_unit", "default_units_residential", "contents_ratio", "wet_ft",
                  "par_depth_ft", "destroy_dv_ft2s", "destroy_depth_ft", "sev_med_depth_ft",
                  "sev_med_dv_ft2s", "sev_high_depth_ft", "sev_high_dv_ft2s", "road_wet_ft",
                  "vehicle_window_hr"):
            v = getattr(self, k)
            if not isinstance(v, numbers.Real) or not math.isfinite(v) or v < 0:
                raise ValueError("{} must be a number >= 0".format(k))
        e = tuple(float(x) for x in self.warning_edges_min)
        if len(e) != 2 or not (0 < e[0] < e[1]):
            raise ValueError("warning_edges_min needs two increasing values above 0 (minutes)")
        if not math.isfinite(self.lag_hr) or self.lag_hr < 0:
            raise ValueError("lag_hr must be >= 0")
        if self.baseline_lag_hr is not None and self.baseline_lag_hr < 0:
            raise ValueError("baseline_lag_hr must be >= 0")
        self.default_occupancy = normalize_occupancy(self.default_occupancy)
        return self

    def as_dict(self):
        d = dict(self.__dict__)
        d["warning_edges_min"] = list(self.warning_edges_min)
        return d


# --------------------------------------------------------------------------------------
# curves and the fatality table
# --------------------------------------------------------------------------------------
def interp_curve(depth_ft, xs, ys):
    """Piecewise-linear damage (percent, 0-100) at `depth_ft` (scalar or array) on the
    curve points (xs ascending, ys). Below the first point the damage is 0; above the last
    point it stays at the last value."""
    xs = np.asarray(xs, dtype=float)
    ys = np.asarray(ys, dtype=float)
    d = np.asarray(depth_ft, dtype=float)
    out = np.interp(np.nan_to_num(d, nan=-1e9), xs, ys, left=0.0, right=float(ys[-1]))
    out = np.clip(out, 0.0, 100.0)
    return float(out) if out.ndim == 0 else out


def default_curves():
    """{key: curve dict} of the generic placeholder curves."""
    out = {}
    for occ, c in GENERIC_CURVES.items():
        out[occ] = {"depth_ft": np.array(_CURVE_DEPTHS), "structure_pct": np.array(
            c["structure_pct"], dtype=float), "contents_pct": np.array(
            c["contents_pct"], dtype=float), "source": CURVE_SOURCE_GENERIC}
    return out


def _read_csv_rows(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        if rd.fieldnames is None:
            raise ValueError("{} is empty".format(path))
        rows = []
        for r in rd:
            rows.append({(k or "").strip().lower(): (v or "").strip() for k, v in r.items()})
    return rows


def _num_cell(row, key, where):
    try:
        v = float(row[key])
    except (KeyError, ValueError):
        raise ValueError("{}: '{}' is missing or not a number (value: {!r})".format(
            where, key, row.get(key)))
    if not math.isfinite(v):
        raise ValueError("{}: '{}' is not finite".format(where, key))
    return v


def parse_curves(rows, source="user CSV"):
    """Curves from rows with occupancy, depth_ft, structure_pct, contents_pct. The occupancy
    text is kept as given (lower case): a structure whose own occupancy text matches is
    looked up by it, else by its category (residential ...), else 'other'."""
    groups = {}
    for i, r in enumerate(rows, start=2):
        where = "curve CSV row {}".format(i)
        occ = (r.get("occupancy") or "").strip().lower()
        if not occ:
            raise ValueError("{}: occupancy is empty".format(where))
        depth = _num_cell(r, "depth_ft", where)
        sp = _num_cell(r, "structure_pct", where)
        cp = _num_cell(r, "contents_pct", where)
        for name, v in (("structure_pct", sp), ("contents_pct", cp)):
            if not 0.0 <= v <= 100.0:
                raise ValueError("{}: {} must be a percent from 0 to 100 (got {})".format(
                    where, name, v))
        groups.setdefault(occ, []).append((depth, sp, cp))
    if not groups:
        raise ValueError("the curve CSV has no rows")
    out = {}
    for occ, pts in groups.items():
        pts.sort(key=lambda p: p[0])
        xs = [p[0] for p in pts]
        if len(set(xs)) != len(xs):
            raise ValueError("curve '{}' has a depth listed twice".format(occ))
        out[occ] = {"depth_ft": np.array(xs), "structure_pct": np.array([p[1] for p in pts]),
                    "contents_pct": np.array([p[2] for p in pts]), "source": source}
    return out


def load_curves_csv(path):
    """Curves from a CSV file (columns occupancy, depth_ft, structure_pct, contents_pct)."""
    return parse_curves(_read_csv_rows(path), source=os.path.basename(path))


def merge_curves(user=None):
    """The generic curves, with the user's curves replacing any of the same key."""
    c = default_curves()
    if user:
        c.update(user)
    return c


def _severity_name(text):
    t = str(text or "").strip().lower()
    if t.startswith("med"):
        return "medium"
    if t in SEVERITIES:
        return t
    raise ValueError("severity must be low, medium or high (got {!r})".format(text))


def parse_fatality_table(rows):
    """{severity: {warning key: rate}} from rows with severity, none, short, some, adequate
    (rates as fractions of the population at risk, 0 to 1)."""
    table = {}
    for i, r in enumerate(rows, start=2):
        where = "fatality CSV row {}".format(i)
        sev = _severity_name(r.get("severity"))
        rec = {}
        for k in WARNING_KEYS:
            v = _num_cell(r, k, where)
            if not 0.0 <= v <= 1.0:
                raise ValueError("{}: {} must be a fraction from 0 to 1 (got {})".format(
                    where, k, v))
            rec[k] = v
        table[sev] = rec
    missing = [s for s in SEVERITIES if s not in table]
    if missing:
        raise ValueError("the fatality table has no row for: {}".format(", ".join(missing)))
    return table


def load_fatality_csv(path):
    return parse_fatality_table(_read_csv_rows(path))


def _table_array(table):
    return np.array([[float(table[s][k]) for k in WARNING_KEYS] for s in SEVERITIES])


def warning_class(minutes, edges=(15.0, 60.0)):
    """Warning class index (0 none, 1 short, 2 some, 3 adequate) for warning time in
    minutes (scalar or array). No warning is a time of zero or less; NaN (unknown) counts as
    no warning. WARNING_KEYS names the classes."""
    w = np.asarray(minutes, dtype=float)
    w = np.where(np.isfinite(w), w, -1.0)
    idx = np.where(w <= 0.0, 0, 1 + (w >= edges[0]).astype(int) + (w >= edges[1]).astype(int))
    return int(idx) if idx.ndim == 0 else idx


def warning_labels(edges=(15.0, 60.0)):
    return {"none": "no warning", "short": "under {:g} min".format(edges[0]),
            "some": "{:g} to {:g} min".format(edges[0], edges[1]),
            "adequate": "over {:g} min".format(edges[1])}


# --------------------------------------------------------------------------------------
# structures
# --------------------------------------------------------------------------------------
@dataclass
class Structure:
    ident: str
    x: float
    y: float
    occupancy: str = OCC_OTHER               # one of OCCUPANCIES
    occupancy_text: str = ""                 # as given (curve lookup key)
    occupants_night: float = 0.0
    occupants_day: float = 0.0
    structure_value: Optional[float] = None  # None = unknown (counted as 0, and reported)
    contents_value: Optional[float] = None
    ffh_ft: float = 0.0
    foundation: str = "other"
    notes: List[str] = field(default_factory=list)   # how defaults were filled in
    props: Dict[str, object] = field(default_factory=dict)


def _pos(v):
    return isinstance(v, numbers.Real) and not isinstance(v, bool) and math.isfinite(v)


def make_structure(ident, x, y, params=None, occupancy=None, occupants_night=None,
                   occupants_day=None, units=None, structure_value=None, contents_value=None,
                   contents_pct=None, ffh_ft=None, foundation=None, props=None):
    """A Structure with the defaults filled in. Every argument but the location is optional;
    `notes` says what was assumed.

    * Occupants: the night / day counts if given (a missing one copies the other); else the
      unit count x persons_per_unit; else, for a residential structure, default units x
      persons_per_unit; else 0 ("no_occupant_data").
    * Contents value: given; else contents_pct (percent) of the structure value; else
      contents_ratio x the structure value.
    * First-floor height: given; else by foundation type; else by occupancy.
    """
    p = params or ConsequenceParams()
    notes = []
    text = str(occupancy).strip() if occupancy not in (None, "") else ""
    occ = normalize_occupancy(text) if text else p.default_occupancy
    if not text:
        notes.append("occupancy_default")
    night = float(occupants_night) if _pos(occupants_night) and occupants_night >= 0 else None
    day = float(occupants_day) if _pos(occupants_day) and occupants_day >= 0 else None
    if night is not None or day is not None:
        if night is None:
            night = day
            notes.append("night_occupants_copied_from_day")
        if day is None:
            day = night
            notes.append("day_occupants_copied_from_night")
    elif _pos(units) and units >= 0:
        night = day = float(units) * p.persons_per_unit
        notes.append("occupants_from_units")
    elif occ == OCC_RESIDENTIAL:
        night = day = p.default_units_residential * p.persons_per_unit
        notes.append("occupants_from_default_units")
    else:
        night = day = 0.0
        notes.append("no_occupant_data")
    sval = float(structure_value) if _pos(structure_value) and structure_value >= 0 else None
    cval = float(contents_value) if _pos(contents_value) and contents_value >= 0 else None
    if cval is None and sval is not None:
        if _pos(contents_pct) and contents_pct >= 0:
            cval = sval * float(contents_pct) / 100.0
            notes.append("contents_from_pct")
        else:
            cval = sval * p.contents_ratio
            notes.append("contents_from_ratio")
    found = normalize_foundation(foundation)
    if _pos(ffh_ft):
        ffh = float(ffh_ft)
    elif found in FFH_BY_FOUNDATION_FT:
        ffh = FFH_BY_FOUNDATION_FT[found]
        notes.append("ffh_from_foundation")
    else:
        ffh = FFH_BY_OCCUPANCY_FT[occ]
        notes.append("ffh_from_occupancy")
    if sval is None:
        notes.append("no_structure_value")
    return Structure(str(ident), float(x), float(y), occ, text.lower(), night, day, sval, cval,
                     ffh, found, notes, dict(props or {}))


# --------------------------------------------------------------------------------------
# evaluation of one flood
# --------------------------------------------------------------------------------------
def _sample(grid, x, y, arr):
    """Value of `arr` (on `grid`) at points, NaN outside the grid. Returns (values, inside)."""
    c = np.floor((x - grid.x0) / grid.dx).astype(int)
    r = np.floor((grid.y0 - y) / grid.dx).astype(int)
    ok = (r >= 0) & (r < grid.ny) & (c >= 0) & (c < grid.nx)
    out = np.full(len(x), np.nan)
    out[ok] = arr[r[ok], c[ok]]
    return out, ok


def _to_us(fields, units, shape):
    """{depth, velocity, arrival} as float arrays in ft, ft/s, hours (NaN where missing)."""
    if units not in ("us", "si"):
        raise ValueError("units must be 'us' or 'si'")
    out = {}
    for k in ("depth", "velocity", "arrival"):
        a = fields.get(k) if fields else None
        if a is None:
            if k == "depth":
                raise ValueError("a depth array is required")
            out[k] = np.full(shape, np.nan)
            continue
        a = np.asarray(a, dtype=float)
        if a.shape != tuple(shape):
            raise ValueError("the {} array is {} but the grid is {}".format(
                k, a.shape, tuple(shape)))
        out[k] = a / FT if (units == "si" and k in ("depth", "velocity")) else a
    return out


def _curve_key(s, curves):
    if s.occupancy_text and s.occupancy_text in curves:
        return s.occupancy_text
    if s.occupancy in curves:
        return s.occupancy
    return OCC_OTHER


def evaluate(structs, grid, fields, params, curves=None, table=None, lag_hr=None, units="us"):
    """One flood over the structures. Returns a dict of arrays, one value per structure:
    depth_ft, velocity_fps, dv, arrival_hr, inside, wet, above_floor_ft, destroyed,
    struct_pct, contents_pct, struct_loss, contents_loss, loss, par, warning_min,
    warning_class, warning_unknown, severity, rate, lol, curve (list of keys)."""
    p = params or ConsequenceParams()
    curves = curves or default_curves()
    tab = _table_array(table or DEFAULT_FATALITY_TABLE)
    lag = p.lag_hr if lag_hr is None else lag_hr
    n = len(structs)
    f = _to_us(fields, units, grid.data.shape)
    x = np.array([s.x for s in structs], dtype=float)
    y = np.array([s.y for s in structs], dtype=float)
    d_raw, inside = _sample(grid, x, y, f["depth"])
    v_raw, _ = _sample(grid, x, y, f["velocity"])
    a_raw, _ = _sample(grid, x, y, f["arrival"])
    d = np.where(np.isfinite(d_raw), np.clip(d_raw, 0.0, None), 0.0)
    wet = d >= p.wet_ft
    dv = d * np.where(np.isfinite(v_raw), v_raw, 0.0)
    destroyed = wet & ((dv >= p.destroy_dv_ft2s) | (d >= p.destroy_depth_ft))
    ffh = np.array([s.ffh_ft for s in structs], dtype=float)
    above = d - ffh
    sp = np.zeros(n)
    cp = np.zeros(n)
    keys = [_curve_key(s, curves) for s in structs]
    for k in set(keys):
        m = np.array([kk == k for kk in keys])
        c = curves[k]
        sp[m] = interp_curve(above[m], c["depth_ft"], c["structure_pct"])
        cp[m] = interp_curve(above[m], c["depth_ft"], c["contents_pct"])
    sp = np.where(wet, sp, 0.0)
    cp = np.where(wet, cp, 0.0)
    sp[destroyed] = 100.0
    cp[destroyed] = 100.0
    sval = np.array([s.structure_value or 0.0 for s in structs], dtype=float)
    cval = np.array([s.contents_value or 0.0 for s in structs], dtype=float)
    struct_loss = sval * sp / 100.0
    contents_loss = cval * cp / 100.0
    night = np.array([s.occupants_night for s in structs], dtype=float)
    day = np.array([s.occupants_day for s in structs], dtype=float)
    occ = {"night": night, "day": day, "max": np.maximum(night, day)}[p.population]
    par = np.where(d >= max(p.par_depth_ft, p.wet_ft), occ, 0.0)
    warn_min = (a_raw - lag) * 60.0
    cls = warning_class(warn_min, p.warning_edges_min)
    unknown = wet & ~np.isfinite(a_raw)
    sev = np.where((dv >= p.sev_high_dv_ft2s) | (d >= p.sev_high_depth_ft), 2,
                   np.where((dv >= p.sev_med_dv_ft2s) | (d >= p.sev_med_depth_ft), 1, 0))
    rate = tab[sev, cls]
    lol = par * rate
    return {"depth_ft": d, "velocity_fps": v_raw, "dv": dv, "arrival_hr": a_raw,
            "inside": inside, "wet": wet, "above_floor_ft": above, "destroyed": destroyed,
            "struct_pct": sp, "contents_pct": cp, "struct_loss": struct_loss,
            "contents_loss": contents_loss, "loss": struct_loss + contents_loss, "par": par,
            "warning_min": warn_min, "warning_class": cls, "warning_unknown": unknown,
            "severity": sev, "rate": rate, "lol": lol, "curve": keys}


def _zero_eval(n):
    z = np.zeros(n)
    zb = np.zeros(n, dtype=bool)
    return {"depth_ft": z, "velocity_fps": np.full(n, np.nan), "dv": z,
            "arrival_hr": np.full(n, np.nan), "inside": zb, "wet": zb, "above_floor_ft": z,
            "destroyed": zb, "struct_pct": z, "contents_pct": z, "struct_loss": z,
            "contents_loss": z, "loss": z, "par": z, "warning_min": np.full(n, np.nan),
            "warning_class": np.zeros(n, dtype=int), "warning_unknown": zb,
            "severity": np.zeros(n, dtype=int), "rate": z, "lol": z, "curve": [""] * n}


def incremental(with_ev, base_ev):
    """with minus without, per structure, never below zero: {loss, struct_loss,
    contents_loss, par, lol}."""
    out = {}
    for k in ("loss", "struct_loss", "contents_loss", "par", "lol"):
        out[k] = np.maximum(0.0, with_ev[k] - base_ev[k])
    return out


# --------------------------------------------------------------------------------------
# roads and rail
# --------------------------------------------------------------------------------------
def assess_roads(rec, grid, with_fields, base_fields=None, params=None, units="us", aadt=None):
    """Flooded length of roads and rail (impacts.Receptors samples, each standing for
    `length_m`) with and without the breach, and the vehicles exposed at flooded roads.

    `aadt` is an optional {road identifier: annual average daily traffic}. Vehicles exposed
    at a flooded road feature = AADT / 24 x vehicle_window_hr, counted once per road feature
    (not per crossing) and only where the road floods. Incremental values are with minus
    without per feature, never negative. Returns a dict, {} without roads or rail."""
    p = params or ConsequenceParams()
    kinds = np.asarray(rec.kind)
    sel = (kinds == "road") | (kinds == "rail")
    if not len(rec) or not sel.any():
        return {}
    shape = grid.data.shape
    fw = _to_us(with_fields, units, shape)
    dw, _ = _sample(grid, np.asarray(rec.x), np.asarray(rec.y), fw["depth"])
    aw, _ = _sample(grid, np.asarray(rec.x), np.asarray(rec.y), fw["arrival"])
    wet_w = np.isfinite(dw) & (dw >= p.road_wet_ft)
    if base_fields is not None:
        fb = _to_us(base_fields, units, shape)
        db, _ = _sample(grid, np.asarray(rec.x), np.asarray(rec.y), fb["depth"])
        wet_b = np.isfinite(db) & (db >= p.road_wet_ft)
    else:
        db = np.full(len(rec), np.nan)
        wet_b = np.zeros(len(rec), dtype=bool)
    length = np.asarray(rec.length_m, dtype=float)
    idents = [str(i) for i in rec.ident]
    order = []
    seen = set()
    for i in np.nonzero(sel)[0]:
        key = (str(kinds[i]), idents[i])
        if key not in seen:
            seen.add(key)
            order.append(key)
    feats = []
    for kind, ident in order:
        m = sel & (kinds == kind) & np.array([s == ident for s in idents])
        lw = float(length[m & wet_w].sum()) / 1000.0
        lb = float(length[m & wet_b].sum()) / 1000.0
        arr = aw[m & wet_w]
        arr = arr[np.isfinite(arr)]
        row = {"id": ident, "kind": kind, "flooded_km": round(lw, 3),
               "baseline_flooded_km": round(lb, 3),
               "incremental_km": round(max(0.0, lw - lb), 3),
               "max_depth_ft": round(float(np.nanmax(dw[m])), 2) if np.isfinite(dw[m]).any()
               else "",
               "earliest_arrival_hr": round(float(arr.min()), 2) if len(arr) else "",
               "aadt": "", "vehicles_exposed": "", "baseline_vehicles_exposed": "",
               "incremental_vehicles": ""}
        v = (aadt or {}).get(ident) if kind == "road" else None
        if _pos(v) and v >= 0:
            per_hr = float(v) / 24.0 * p.vehicle_window_hr
            vw = per_hr if lw > 0 else 0.0
            vb = per_hr if lb > 0 else 0.0
            row.update({"aadt": v, "vehicles_exposed": round(vw, 1),
                        "baseline_vehicles_exposed": round(vb, 1),
                        "incremental_vehicles": round(max(0.0, vw - vb), 1)})
        feats.append(row)

    def tot(kind, key):
        return round(sum(r[key] for r in feats if r["kind"] == kind and r[key] != ""), 3)

    out = {"features": [r for r in feats if r["flooded_km"] > 0 or r["baseline_flooded_km"] > 0],
           "flood_depth_ft": p.road_wet_ft, "vehicle_window_hr": p.vehicle_window_hr,
           "note": "Vehicles exposed = AADT / 24 x window, once per flooded road feature; "
                   "a screening count, not a traffic model."}
    for kind in ("road", "rail"):
        if any(r["kind"] == kind for r in feats):
            out[kind] = {"flooded_km": tot(kind, "flooded_km"),
                         "baseline_flooded_km": tot(kind, "baseline_flooded_km"),
                         "incremental_km": tot(kind, "incremental_km")}
    if any(r["vehicles_exposed"] != "" for r in feats):
        out["vehicles"] = {"exposed": tot("road", "vehicles_exposed"),
                           "baseline_exposed": tot("road", "baseline_vehicles_exposed"),
                           "incremental": tot("road", "incremental_vehicles")}
    return out


# --------------------------------------------------------------------------------------
# the assessment
# --------------------------------------------------------------------------------------
ROW_COLUMNS = [
    "id", "x", "y", "occupancy", "foundation", "occupants_used", "structure_value",
    "contents_value", "first_floor_ft", "depth_ft", "velocity_fps", "dv_ft2s", "arrival_hr",
    "depth_above_floor_ft", "destroyed", "structure_dmg_pct", "contents_dmg_pct", "damage_usd",
    "warning_min", "warning_class", "severity", "par", "fatality_rate", "est_lol",
    "base_depth_ft", "base_damage_usd", "base_par", "base_lol",
    "inc_damage_usd", "inc_par", "inc_lol", "curve", "notes"]


def _r(v, nd=2):
    return round(float(v), nd)


def _totals(ev):
    wet = ev["wet"]
    cls = ev["warning_class"]
    out = {"structures_flooded": int(wet.sum()), "structures_destroyed": int(ev["destroyed"].sum()),
           "structure_damage_usd": round(float(ev["struct_loss"].sum())),
           "contents_damage_usd": round(float(ev["contents_loss"].sum())),
           "damage_usd": round(float(ev["loss"].sum())),
           "par": _r(ev["par"].sum()), "structures_with_par": int((ev["par"] > 0).sum()),
           "estimated_lol": _r(ev["lol"].sum(), 3),
           "par_by_warning": {k: _r(ev["par"][cls == i].sum()) for i, k in
                              enumerate(WARNING_KEYS)},
           "lol_by_severity": {s: _r(ev["lol"][ev["severity"] == i].sum(), 3)
                               for i, s in enumerate(SEVERITIES)}}
    return out


def assess(structs, grid, with_fields, base_fields=None, params=None, curves=None,
           fatality_table=None, units="us", roads=None, road_aadt=None, curves_source=None,
           fatality_source=None, rate_table=None, crossings=True, sim_end_hr=None):
    """Consequences of the with-breach flood, of the flood without the breach (when
    `base_fields` is given) and the incremental difference.

    structs        list of Structure (see make_structure)
    grid           core.grid.Grid the rasters are on (only the georeference is used)
    with_fields    {"depth", "velocity", "arrival"} arrays (ft, ft/s, hours; units="si"
                   for m, m/s, hours); velocity and arrival are optional
    base_fields    the same for the no-breach flood, or None (then the baseline is zero
                   and the incremental equals the with-breach result: right for a sunny-day
                   failure where nothing floods without it)
    roads          optional impacts.Receptors with road / rail lines; road_aadt {id: AADT}
    rate_table     3.20.0: the signed-off fatality rate table (core/lifeloss.py); None = the
                   plugin's own file, which ships unsigned, so loss of life is reported as
                   "not estimated" until an engineer signs one
    crossings      3.20.0: also work out the road / rail crossing table (core/crossings.py)
                   when road or rail lines are given
    sim_end_hr     3.20.0: the run's simulated time, so a crossing's "how long wet" has an
                   upper bound to report instead of nothing

    Returns (rows, summary): rows is a list of dicts (ROW_COLUMNS), one per structure the
    flood reaches in either case (all structures when params.include_dry); summary is a
    JSON-friendly dict."""
    p = (params or ConsequenceParams()).validate()
    structs = list(structs)
    n = len(structs)
    curves = curves or default_curves()
    table = fatality_table or DEFAULT_FATALITY_TABLE
    lag_b = p.lag_hr if p.baseline_lag_hr is None else p.baseline_lag_hr
    if n:
        ev = evaluate(structs, grid, with_fields, p, curves, table, p.lag_hr, units)
        if base_fields is not None:
            eb = evaluate(structs, grid, base_fields, p, curves, table, lag_b, units)
        else:
            eb = _zero_eval(n)
        inc = incremental(ev, eb)
    else:
        ev = eb = _zero_eval(0)
        inc = incremental(ev, eb)
    occ_used = np.array([{"night": s.occupants_night, "day": s.occupants_day,
                          "max": max(s.occupants_night, s.occupants_day)}[p.population]
                         for s in structs], dtype=float)

    rows = []
    keep = np.ones(n, dtype=bool) if p.include_dry else (ev["wet"] | eb["wet"])
    for i in np.nonzero(keep)[0]:
        s = structs[i]
        wet = bool(ev["wet"][i])
        rows.append({
            "id": s.ident, "x": _r(s.x, 1), "y": _r(s.y, 1), "occupancy": s.occupancy,
            "foundation": s.foundation, "occupants_used": _r(occ_used[i]),
            "structure_value": "" if s.structure_value is None else round(s.structure_value),
            "contents_value": "" if s.contents_value is None else round(s.contents_value),
            "first_floor_ft": _r(s.ffh_ft), "depth_ft": _r(ev["depth_ft"][i]),
            "velocity_fps": _r(ev["velocity_fps"][i]) if np.isfinite(ev["velocity_fps"][i])
            else "", "dv_ft2s": _r(ev["dv"][i]),
            "arrival_hr": _r(ev["arrival_hr"][i]) if np.isfinite(ev["arrival_hr"][i]) else "",
            "depth_above_floor_ft": _r(ev["above_floor_ft"][i]) if wet else "",
            "destroyed": bool(ev["destroyed"][i]),
            "structure_dmg_pct": _r(ev["struct_pct"][i]),
            "contents_dmg_pct": _r(ev["contents_pct"][i]),
            "damage_usd": round(float(ev["loss"][i])),
            "warning_min": _r(ev["warning_min"][i], 1) if np.isfinite(ev["warning_min"][i])
            else "",
            "warning_class": WARNING_KEYS[int(ev["warning_class"][i])] if wet else "",
            "severity": SEVERITIES[int(ev["severity"][i])] if wet else "",
            "par": _r(ev["par"][i]), "fatality_rate": _r(ev["rate"][i], 4) if wet else "",
            "est_lol": _r(ev["lol"][i], 4),
            "base_depth_ft": _r(eb["depth_ft"][i]), "base_damage_usd": round(float(eb["loss"][i])),
            "base_par": _r(eb["par"][i]), "base_lol": _r(eb["lol"][i], 4),
            "inc_damage_usd": round(float(inc["loss"][i])), "inc_par": _r(inc["par"][i]),
            "inc_lol": _r(inc["lol"][i], 4), "curve": ev["curve"][i],
            "notes": ";".join(s.notes + (["arrival_unknown_treated_as_no_warning"]
                                         if ev["warning_unknown"][i] else [])
                              + (["outside_raster"] if not ev["inside"][i] else []))})

    # data gaps among structures the with-breach flood reaches
    w = ev["wet"]
    has = lambda tag: np.array([tag in s.notes for s in structs], dtype=bool)
    gaps = {
        "no_structure_value": int((w & has("no_structure_value")).sum()),
        "no_occupant_data": int((w & has("no_occupant_data")).sum()),
        "occupants_from_default_units": int((w & has("occupants_from_default_units")).sum()),
        "contents_value_estimated": int((w & (has("contents_from_ratio")
                                              | has("contents_from_pct"))).sum()),
        "first_floor_height_defaulted": int((w & (has("ffh_from_foundation")
                                                  | has("ffh_from_occupancy"))).sum()),
        "no_velocity": int((w & ~np.isfinite(ev["velocity_fps"])).sum()),
        "arrival_unknown": int(ev["warning_unknown"].sum()),
        "structures_outside_raster": int((~ev["inside"]).sum()),
    }
    if base_fields is not None:
        gaps["baseline_no_velocity"] = int((eb["wet"] & ~np.isfinite(eb["velocity_fps"])).sum())
    curve_keys = sorted(set(ev["curve"][i] for i in np.nonzero(w)[0])) if n else []
    cs = curves_source or ("user curves: " + ", ".join(
        sorted(k for k, c in curves.items() if c.get("source") != CURVE_SOURCE_GENERIC))
        if any(c.get("source") != CURVE_SOURCE_GENERIC for c in curves.values())
        else CURVE_SOURCE_GENERIC)
    user_curve_used = [k for k in curve_keys if curves[k].get("source") != CURVE_SOURCE_GENERIC]
    generic_used = [k for k in curve_keys if curves[k].get("source") == CURVE_SOURCE_GENERIC]
    default_table = fatality_table is None
    notes = []
    if generic_used:
        notes.append("Generic placeholder depth-damage curves were used for: {}. Replace them "
                     "with your own curves.".format(", ".join(generic_used)))
    if default_table:
        notes.append("The built-in fatality table is approximate and from memory of the "
                     "Graham (1999) / RCEM style of guidance: check it against the current "
                     "published tables before submittal.")
    if base_fields is None:
        notes.append("No baseline (no-breach) rasters: nothing is assumed to flood without the "
                     "breach, so the incremental equals the with-breach result.")
    if gaps["no_velocity"]:
        notes.append("{} flooded structure(s) have no velocity: the depth-velocity rules were "
                     "not applied to them (severity from depth only).".format(gaps["no_velocity"]))
    if gaps["arrival_unknown"]:
        notes.append("{} flooded structure(s) have no arrival time (the flood never reached the "
                     "arrival depth there): treated as no warning.".format(
                         gaps["arrival_unknown"]))
    if gaps["no_structure_value"]:
        notes.append("{} flooded structure(s) have no structure value: no dollar damage "
                     "counted for them.".format(gaps["no_structure_value"]))
    if gaps["no_occupant_data"]:
        notes.append("{} flooded structure(s) have no occupant data: PAR counted as 0 for "
                     "them.".format(gaps["no_occupant_data"]))
    if gaps["structures_outside_raster"]:
        notes.append("{} structure(s) lie outside the rasters and were treated as dry.".format(
            gaps["structures_outside_raster"]))
    notes.append("Basement (below-grade) damage is not represented; first-floor heights "
                 "given or defaulted are heights above grade.")

    summary = {
        "disclaimer": DISCLAIMER, "incremental_note": INCREMENTAL_NOTE,
        "units": "depth ft, velocity ft/s, arrival hours after breach initiation, dollars",
        "structures": n, "structures_in_table": len(rows),
        "with_breach": _totals(ev),
        "baseline": _totals(eb) if base_fields is not None else None,
        "incremental": {
            "structures_with_incremental_damage": int((inc["loss"] > 0).sum()),
            "structure_damage_usd": round(float(inc["struct_loss"].sum())),
            "contents_damage_usd": round(float(inc["contents_loss"].sum())),
            "damage_usd": round(float(inc["loss"].sum())),
            "par": _r(inc["par"].sum()), "structures_with_par": int((inc["par"] > 0).sum()),
            "estimated_lol": _r(inc["lol"].sum(), 3)},
        "data_gaps": gaps,
        "curves": {"source": cs, "generic_placeholders_used": generic_used,
                   "user_curves_used": user_curve_used},
        "fatality_table": {"source": fatality_source or ("built-in approximate default"
                                                         if default_table else "user"),
                           "values": {s: dict(table[s]) for s in SEVERITIES}},
        "warning_classes": warning_labels(p.warning_edges_min),
        "parameters": p.as_dict(),
        "notes": notes,
    }
    if roads is not None and len(roads):
        r = assess_roads(roads, grid, with_fields, base_fields, p, units, road_aadt)
        if r:
            summary["roads"] = r
    # 3.20.0 (feature 12): population at risk day and night, the flood-severity split and the
    # warning-time split from the suite's shared rules (damsuite.loss), and loss of life only
    # when an engineer has signed a rate table (lifeloss.py). This is reported BESIDE the
    # estimate above, never instead of it: the numbers above come from this module's own
    # thresholds and its own fatality table, and swapping one set for the other without saying
    # so would change a number in a report somebody has already submitted.
    summary["par"] = _par_summary(structs, ev, rate_table)
    # 3.20.0 (feature 13): the road and rail crossing table the county can post
    if crossings and roads is not None and len(roads):
        summary["crossings"] = _crossing_summary(roads, grid, with_fields, units,
                                                 sim_end_hr)
    return rows, summary


def _par_summary(structs, ev, rate_table=None):
    """The lifeloss / damsuite.loss roll-up for the summary ({"problem": ...} on any failure:
    the damage and loss-of-life estimate above must not be lost over this addition)."""
    try:
        from . import lifeloss
        table = rate_table if rate_table is not None else (lifeloss.load()[0] or {})
        one = bool(structs) and all(s.occupants_day == s.occupants_night for s in structs)
        got = lifeloss.assess(lifeloss.rows_from_evaluate(structs, ev), table, one_count=one)
        return lifeloss.summary(got)
    except Exception as e:                                       # noqa
        return {"problem": "{}: {}".format(type(e).__name__, e)}


def _crossing_summary(roads, grid, with_fields, units, sim_end_hr=None):
    """{"rows", ...} of the road / rail crossings ({"problem": ...} on any failure)."""
    try:
        from . import crossings as crmod
        rows = crmod.crossing_rows(roads, grid, with_fields, units, sim_end_hr=sim_end_hr)
        out = crmod.summary(rows)
        out["rows"] = rows
        return out
    except Exception as e:                                       # noqa
        return {"problem": "{}: {}".format(type(e).__name__, e)}


# --------------------------------------------------------------------------------------
# outputs
# --------------------------------------------------------------------------------------
OUTPUT_NAMES = {"csv": "Consequences (Structures).csv", "roads_csv": "Consequences (Roads).csv",
                "markdown": "Consequences Summary.md", "json": "Consequences Summary.json",
                "geojson": "Consequences (Affected Structures).geojson"}


def write_rows_csv(path, rows, columns=None):
    cols = columns or ROW_COLUMNS
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return path


def affected_features(rows):
    """GeoJSON point features (coordinates in the raster's CRS) of the structures flooded
    with the breach."""
    feats = []
    for r in rows:
        if not (isinstance(r["depth_ft"], numbers.Real) and r["depth_ft"] > 0):
            continue
        props = {k: r[k] for k in ROW_COLUMNS if k not in ("x", "y")}
        feats.append({"type": "Feature", "properties": props,
                      "geometry": {"type": "Point", "coordinates": [r["x"], r["y"]]}})
    return feats


def write_affected_geojson(path, rows, crs=None):
    """Writes the affected structures with products.write_geojson (crs like 'EPSG:26914')."""
    from . import products
    return products.write_geojson(path, affected_features(rows), crs)


def _usd(v):
    return "${:,.0f}".format(v)


def markdown_summary(summary, title="Life-safety and damage estimate (incremental)"):
    p = summary["parameters"]
    wb, bl, inc = summary["with_breach"], summary["baseline"], summary["incremental"]
    L = ["# " + title, "", "> **Screening estimate. Read before use.** " + summary["disclaimer"],
         "", "> " + summary["incremental_note"], "",
         "{} structures read; {} flooded with the breach.".format(
             summary["structures"], wb["structures_flooded"]), "",
         "## Results", "",
         "| | With breach | Without breach | Incremental |", "|---|---:|---:|---:|"]
    def line(label, a, b, c):
        L.append("| {} | {} | {} | {} |".format(label, a, b, c))
    nb = lambda k, f=str: f(bl[k]) if bl else "n/a"
    line("Structures flooded", wb["structures_flooded"], nb("structures_flooded"),
         inc["structures_with_incremental_damage"])
    line("Structures destroyed", wb["structures_destroyed"], nb("structures_destroyed"), "")
    line("Structure damage", _usd(wb["structure_damage_usd"]),
         nb("structure_damage_usd", _usd), _usd(inc["structure_damage_usd"]))
    line("Contents damage", _usd(wb["contents_damage_usd"]),
         nb("contents_damage_usd", _usd), _usd(inc["contents_damage_usd"]))
    line("**Total damage**", "**{}**".format(_usd(wb["damage_usd"])),
         nb("damage_usd", _usd), "**{}**".format(_usd(inc["damage_usd"])))
    line("Population at risk", wb["par"], nb("par"), inc["par"])
    line("**Estimated loss of life**", "**{:.2f}**".format(wb["estimated_lol"]),
         nb("estimated_lol", lambda v: "{:.2f}".format(v)),
         "**{:.2f}**".format(inc["estimated_lol"]))
    lab = summary["warning_classes"]
    L += ["", "## Population at risk by warning time (with breach)", "",
          "Warning = arrival time minus a {:g} h detection / notification lag "
          "(both counted from breach initiation).".format(p["lag_hr"]), "",
          "| Warning | PAR |", "|---|---:|"]
    for k in WARNING_KEYS:
        L.append("| {} | {} |".format(lab[k], wb["par_by_warning"][k]))
    L += ["", "## Fatality rates used (fraction of PAR) - {}".format(
        summary["fatality_table"]["source"]), "",
        "| Severity | " + " | ".join(lab[k] for k in WARNING_KEYS) + " |",
        "|---|" + "---:|" * len(WARNING_KEYS)]
    for s in SEVERITIES:
        L.append("| {} | ".format(s) + " | ".join(
            "{:g}".format(summary["fatality_table"]["values"][s][k]) for k in WARNING_KEYS)
                 + " |")
    L += ["", "Severity: medium from a depth of {:g} ft or depth x velocity of {:g} ft2/s; "
          "high from {:g} ft or {:g} ft2/s. Destroyed (100% damage) from {:g} ft2/s or "
          "{:g} ft. PAR counts occupants ({} basis) where the depth is at least {:g} ft. "
          "All of these are adjustable placeholders.".format(
              p["sev_med_depth_ft"], p["sev_med_dv_ft2s"], p["sev_high_depth_ft"],
              p["sev_high_dv_ft2s"], p["destroy_dv_ft2s"], p["destroy_depth_ft"],
              p["population"], p["par_depth_ft"])]
    roads = summary.get("roads")
    if roads:
        L += ["", "## Roads and rail (flooded from {:g} ft)".format(roads["flood_depth_ft"]), "",
              "| | With breach (km) | Without (km) | Incremental (km) |", "|---|---:|---:|---:|"]
        for k in ("road", "rail"):
            if k in roads:
                r = roads[k]
                L.append("| {} | {} | {} | {} |".format(k, r["flooded_km"],
                                                        r["baseline_flooded_km"],
                                                        r["incremental_km"]))
        if "vehicles" in roads:
            v = roads["vehicles"]
            L += ["", "Vehicles exposed in {:g} h of traffic at flooded roads: {} with breach, "
                  "{} without, {} incremental. {}".format(
                      roads["vehicle_window_hr"], v["exposed"], v["baseline_exposed"],
                      v["incremental"], roads["note"])]
    # 3.20.0: the suite's own PAR / severity / warning section and the crossing table
    par = summary.get("par")
    if par:
        try:
            from . import lifeloss
            L += [""] + lifeloss.markdown_from_summary(par)
        except Exception as e:                                   # noqa
            L += ["", "## Population at risk", "", "- not written ({})".format(e)]
    cross = summary.get("crossings")
    if cross:
        try:
            from . import crossings as crmod
            L += [""] + (["## Road and railroad crossings in the flood", "",
                          "- not written ({})".format(cross["problem"]), ""]
                         if cross.get("problem") else
                         crmod.markdown_lines(cross.get("rows"), cross))
        except Exception as e:                                   # noqa
            L += ["", "## Road and railroad crossings", "", "- not written ({})".format(e)]
    L += ["", "## Data gaps (structures the flood reaches)", ""]
    for k, v in summary["data_gaps"].items():
        if v:
            L.append("- {}: {}".format(k.replace("_", " "), v))
    if not any(summary["data_gaps"].values()):
        L.append("- none")
    L += ["", "## Notes", ""] + ["- " + n for n in summary["notes"]]
    L += ["", "## Inputs", "", "- Depth-damage curves: {}".format(summary["curves"]["source"]),
          "- Parameters: {}".format(", ".join("{}={}".format(k, v) for k, v in p.items())), ""]
    return "\n".join(L)


def write_outputs(folder, rows, summary, crs=None, geojson=True):
    """CSV, markdown, JSON and (optionally) GeoJSON of the affected structures in `folder`.
    Returns {"csv", "markdown", "json", "geojson" (or ""), "roads_csv" (or "")}."""
    import json
    os.makedirs(folder, exist_ok=True)
    out = {}
    out["csv"] = write_rows_csv(os.path.join(folder, OUTPUT_NAMES["csv"]), rows)
    out["markdown"] = os.path.join(folder, OUTPUT_NAMES["markdown"])
    with open(out["markdown"], "w") as f:
        f.write(markdown_summary(summary))
    out["json"] = os.path.join(folder, OUTPUT_NAMES["json"])
    with open(out["json"], "w") as f:
        json.dump(summary, f, indent=2, default=str)
    out["geojson"] = ""
    if geojson:
        out["geojson"] = write_affected_geojson(os.path.join(folder, OUTPUT_NAMES["geojson"]),
                                                rows, crs)
    # 3.20.0: the crossing table and layer, and the PAR table
    cross = summary.get("crossings") or {}
    if cross.get("rows"):
        try:
            from . import crossings as crmod
            out.update({"crossings_" + k: v for k, v in
                        crmod.write_outputs(folder, cross["rows"], crs, geojson).items()})
        except Exception as e:                                   # noqa
            out["crossings_csv"] = ""
            summary.setdefault("notes", []).append(
                "the crossing table was not written ({})".format(e))
    par = summary.get("par") or {}
    if par and not par.get("problem"):
        try:
            from . import lifeloss
            p = os.path.join(folder, lifeloss.OUTPUT_NAMES["csv"])
            with open(p, "w", newline="", encoding="utf-8") as f:
                f.write(",".join(lifeloss.CSV_COLUMNS) + "\n")
                for r in par.get("cells") or []:
                    f.write(",".join(str(r.get(c, "")) for c in lifeloss.CSV_COLUMNS) + "\n")
            out["par_csv"] = p
        except Exception as e:                                   # noqa
            out["par_csv"] = ""
            summary.setdefault("notes", []).append(
                "the population-at-risk table was not written ({})".format(e))
    out["roads_csv"] = ""
    feats = (summary.get("roads") or {}).get("features")
    if feats:
        out["roads_csv"] = write_rows_csv(
            os.path.join(folder, OUTPUT_NAMES["roads_csv"]), feats,
            ["id", "kind", "flooded_km", "baseline_flooded_km", "incremental_km",
             "max_depth_ft", "earliest_arrival_hr", "aadt", "vehicles_exposed",
             "baseline_vehicles_exposed", "incremental_vehicles"])
    return out


# --------------------------------------------------------------------------------------
# reading a dam folder / a RunResult
# --------------------------------------------------------------------------------------
def read_dam_folder(folder):
    """(grid, with_fields, base_fields or None, notes) from the rasters the workflow writes
    in `folder` (ft, ft/s, hours): 'Depth (Max).tif', 'Velocity (Max).tif',
    'Arrival Time.tif' and the 'Baseline ...' versions of them. Needs GDAL. The grid is the
    depth raster's; the others are read onto it (nearest cell)."""
    from . import gdal_io
    from .workflow import FINAL_NAMES, BASELINE_NAMES
    dpath = os.path.join(folder, FINAL_NAMES["depth"])
    if not os.path.exists(dpath):
        raise FileNotFoundError("no '{}' in {}".format(FINAL_NAMES["depth"], folder))
    g = gdal_io.read_grid(dpath)
    notes = []
    with_f = {"depth": g.data}
    for k in ("velocity", "arrival"):
        p = os.path.join(folder, FINAL_NAMES[k])
        if os.path.exists(p):
            with_f[k] = gdal_io.read_like(p, g, "near")
        else:
            notes.append("No '{}': {} not used.".format(
                FINAL_NAMES[k], "the depth-velocity rules" if k == "velocity"
                else "warning time (every structure counts as no warning)"))
    base = None
    bp = os.path.join(folder, BASELINE_NAMES["depth"])
    if os.path.exists(bp):
        base = {"depth": gdal_io.read_like(bp, g, "near")}
        for k in ("velocity", "arrival"):
            p = os.path.join(folder, BASELINE_NAMES[k])
            if os.path.exists(p):
                base[k] = gdal_io.read_like(p, g, "near")
    else:
        notes.append("No '{}': there is no no-breach case, so the incremental equals the "
                     "with-breach result.".format(BASELINE_NAMES["depth"]))
    return g, with_f, base, notes


def assess_dam_folder(folder, structs, params=None, curves=None, fatality_table=None,
                      roads=None, road_aadt=None, write=True, geojson=True):
    """read_dam_folder + assess (+ write_outputs into the folder). Returns
    (rows, summary, paths, grid)."""
    g, wf, bf, notes = read_dam_folder(folder)
    rows, summary = assess(structs, g, wf, bf, params, curves, fatality_table, "us", roads,
                           road_aadt)
    summary["notes"] = notes + summary["notes"]
    paths = {}
    if write:
        crs = None
        try:
            from . import gdal_io
            crs = gdal_io.epsg_of(g.crs_wkt) if g.crs_wkt else None
        except Exception:
            crs = None
        paths = write_outputs(folder, rows, summary, crs, geojson)
    return rows, summary, paths, g


def assess_run_result(res, structs, params=None, curves=None, fatality_table=None, roads=None,
                      road_aadt=None, folder=None):
    """For the workflow: consequences from a workflow.RunResult. Uses the SI arrays when the
    run kept them (RunSettings.keep_arrays) and res.base_si for the baseline; otherwise
    reads the rasters from the dam folder. Writes nothing; call write_outputs with the
    result. Returns (rows, summary)."""
    if getattr(res, "final_si", None) is not None and getattr(res, "grid", None) is not None:
        rows, summary = assess(structs, res.grid, res.final_si, getattr(res, "base_si", None),
                               params, curves, fatality_table, "si", roads, road_aadt)
        return rows, summary
    g, wf, bf, notes = read_dam_folder(folder or res.folder)
    rows, summary = assess(structs, g, wf, bf, params, curves, fatality_table, "us", roads,
                           road_aadt)
    summary["notes"] = notes + summary["notes"]
    return rows, summary
