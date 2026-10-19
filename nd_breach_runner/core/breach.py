"""Breach hydrograph, following North Dakota Dam Safety Standards Policy
REG_05.2026, Section 5.1.1:

* sunny day: starting water surface at the top of dam, or at the maximum
  reservoir level reached by the PMF if that is lower; no additional reservoir
  inflow and no coincident downstream flooding;
* breach parameters from an assumed piping (or overtopping) failure, OR the
  peak breach discharge from NRCS TR-60.

The flood-inflow (PMF) scenario, where the reservoir keeps receiving inflow and
the spillway keeps discharging, is in `flood.py` and uses the pieces here.

Everything is plain Python + numpy so it can be tested without QGIS.
Inputs are US customary units; the routing runs in SI.
"""
import math
import dataclasses
from dataclasses import dataclass, field, asdict
from typing import List, Optional

import numpy as np

from .units import FT, ACFT, ACRE, CFS, G

METHOD_PIPING = "piping"
METHOD_PIPING_ORIFICE = "piping_orifice"
METHOD_OVERTOPPING = "overtopping"
METHOD_F95_PIPING = "froehlich1995_piping"
METHOD_F95_OVERTOPPING = "froehlich1995_overtopping"
METHOD_MLM = "mlm1984"
METHOD_VTG = "vtg1990"
METHOD_TR60 = "tr60"
METHOD_TR60_CURVE = "tr60_curvilinear"
METHOD_USER = "user"

METHOD_LABELS = {
    METHOD_PIPING: "Piping failure, Froehlich (2008) parameters, routed",
    METHOD_PIPING_ORIFICE: "Piping failure with a pipe (orifice) phase, Froehlich (2008) size "
                           "and time",
    METHOD_OVERTOPPING: "Overtopping failure, Froehlich (2008) parameters, routed",
    METHOD_F95_PIPING: "Piping failure, Froehlich (1995) parameters, routed",
    METHOD_F95_OVERTOPPING: "Overtopping failure, Froehlich (1995) parameters, routed",
    METHOD_MLM: "MacDonald & Langridge-Monopolis (1984) earthfill parameters, routed",
    METHOD_VTG: "Von Thun & Gillette (1990) parameters, routed",
    METHOD_TR60: "NRCS TR-60 peak breach discharge, triangular hydrograph",
    METHOD_TR60_CURVE: "NRCS TR-60 peak breach discharge, curvilinear hydrograph",
    METHOD_USER: "My own hydrograph (CSV: time hr, discharge cfs)",
}
METHODS = tuple(METHOD_LABELS)
ROUTED_METHODS = (METHOD_PIPING, METHOD_PIPING_ORIFICE, METHOD_OVERTOPPING, METHOD_F95_PIPING,
                  METHOD_F95_OVERTOPPING, METHOD_MLM, METHOD_VTG)
TR60_METHODS = (METHOD_TR60, METHOD_TR60_CURVE)

# Broad-crested trapezoidal weir, as used by DAMBRK/BREACH (Fread):
# Q = 3.1 b h^1.5 + 2.45 z h^2.5 in US units; the SI equivalents are below.
WEIR_C_BOTTOM_SI = 3.1 * FT ** 0.5          # ~1.711
WEIR_C_SIDE_SI = 2.45 * FT ** 0.5           # ~1.353

# Froehlich (2008) coefficients by failure mode.
FROEHLICH_2008 = {
    "piping": {"ko": 1.0, "z": 0.7},
    "overtopping": {"ko": 1.3, "z": 1.0},
}
FROEHLICH_1995 = {
    "piping": {"ko": 1.0, "z": 0.9},
    "overtopping": {"ko": 1.4, "z": 1.4},
}


class BreachInputError(ValueError):
    pass


@dataclass
class DamData:
    """What the breach analysis needs, in US customary units."""
    dam_id: str
    dam_height_ft: float
    storage_top_acft: float                      # storage at the top of dam
    top_of_dam_ft: float                         # elevation (same datum as the DEM)
    surface_area_top_ac: Optional[float] = None  # at the top of dam
    pmf_max_wse_ft: Optional[float] = None       # max reservoir level in the PMF
    breach_bottom_ft: Optional[float] = None     # default: top of dam - dam height
    crest_width_ft: Optional[float] = None       # for TR-60 / MLM embankment area
    us_slope_h: Optional[float] = None           # upstream face, H in H:1V
    ds_slope_h: Optional[float] = None           # downstream face, H in H:1V
    valley_width_ft: Optional[float] = None      # TR-60 narrow-valley check
    name: str = ""
    crest_length_ft: Optional[float] = None      # embankment crest length (overtopping)

    def validate(self):
        problems = []
        if not self.dam_height_ft or self.dam_height_ft <= 0:
            problems.append("dam height must be greater than 0")
        if not self.storage_top_acft or self.storage_top_acft <= 0:
            problems.append("storage at the top of dam must be greater than 0")
        if self.top_of_dam_ft is None:
            problems.append("top of dam elevation is required")
        if self.surface_area_top_ac is not None and self.surface_area_top_ac <= 0:
            problems.append("surface area must be greater than 0 when given")
        if problems:
            raise BreachInputError("{}: {}".format(self.dam_id, "; ".join(problems)))


@dataclass
class BreachOverrides:
    """Optional user values that replace the computed breach parameters."""
    avg_width_ft: Optional[float] = None
    formation_hr: Optional[float] = None
    side_slope_h: Optional[float] = None
    tr60_rise_hr: Optional[float] = None
    # pipe (orifice) phase of METHOD_PIPING_ORIFICE
    pipe_time_fraction: float = 0.5        # share of the formation time spent as a pipe
    pipe_initial_dia_ft: float = 0.5
    pipe_collapse_frac: float = 0.15       # pipe diameter at roof collapse / breach height
    pipe_cd: float = 0.6
    vtg_erodible: bool = True              # Von Thun & Gillette formation time
    depth_fraction: Optional[float] = None  # partial breach: breach depth / dam height (0.1-1)


@dataclass
class Hydrograph:
    t_s: np.ndarray            # seconds from the start of the record
    q_cms: np.ndarray          # outflow, m3/s
    method: str
    params: dict = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    t_ref_s: float = 0.0       # breach initiation within the record (arrival times count from here)

    @property
    def peak_cms(self):
        return float(self.q_cms.max()) if len(self.q_cms) else 0.0

    @property
    def peak_cfs(self):
        return self.peak_cms / CFS

    @property
    def time_to_peak_hr(self):
        return (float(self.t_s[int(np.argmax(self.q_cms))]) - self.t_ref_s) / 3600.0

    @property
    def duration_s(self):
        return float(self.t_s[-1])

    @property
    def volume_m3(self):
        return float(np.trapezoid(self.q_cms, self.t_s)) if hasattr(np, "trapezoid") \
            else float(np.trapz(self.q_cms, self.t_s))

    def to_csv(self, path):
        with open(path, "w", encoding="utf-8") as f:
            f.write("time_hr,discharge_cfs\n")
            for t, q in zip(self.t_s, self.q_cms):
                f.write("{:.5f},{:.3f}\n".format(t / 3600.0, q / CFS))

    def resampled(self, dt_s):
        """Breakpoints for the flood models: every original breakpoint plus a
        regular grid every dt_s. The models interpolate linearly between
        breakpoints, so a short or sharply peaked hydrograph is reproduced
        exactly instead of being clipped by the grid. Ends with a zero."""
        t_end = float(self.t_s[-1])
        grid = np.arange(0.0, t_end, dt_s)
        orig = np.asarray(self.t_s, dtype=float)
        # regular points too close to a breakpoint would print as the same time in the model
        # files; the breakpoints themselves are kept exactly (they hold the peak)
        far = np.abs(grid[:, None] - orig[None, :]).min(axis=1) > 0.002 if len(orig) < 5000 \
            else np.ones(len(grid), bool)
        t = np.unique(np.concatenate([orig, grid[far], [t_end]]))
        q = np.interp(t, self.t_s, self.q_cms, right=0.0)
        if q[-1] != 0.0:
            t = np.append(t, t[-1] + dt_s)
            q = np.append(q, 0.0)
        return t, q

    def volume_until(self, t_s):
        """Volume (m3) released up to t_s seconds."""
        t = np.unique(np.concatenate([self.t_s[self.t_s < t_s], [min(t_s, self.t_s[-1])]]))
        q = np.interp(t, self.t_s, self.q_cms)
        return float(np.trapezoid(q, t)) if hasattr(np, "trapezoid") else float(np.trapz(q, t))

    def thinned(self, max_points=2500, tol=0.004, max_volume_error=0.002):
        """Fewer breakpoints (Douglas-Peucker on the normalised curve) with the
        peak kept and the volume within `max_volume_error`; long routed
        hydrographs have thousands of steps."""
        if len(self.t_s) <= max_points:
            return self
        from .terrain import douglas_peucker
        ts, qs = self.t_s[-1] or 1.0, max(self.peak_cms, 1e-12)
        pts = np.column_stack([self.t_s / ts, self.q_cms / qs])
        v0 = self.volume_m3
        i = int(np.argmax(self.q_cms))
        tol_k = tol
        while True:
            keep = douglas_peucker(pts, tol_k)
            t = keep[:, 0] * ts
            q = keep[:, 1] * qs
            if not np.any(np.isclose(t, self.t_s[i])):      # the peak is always kept
                j = int(np.searchsorted(t, self.t_s[i]))
                t = np.insert(t, j, self.t_s[i])
                q = np.insert(q, j, self.q_cms[i])
            q = np.clip(q, 0.0, None)
            vol = float(np.trapezoid(q, t)) if hasattr(np, "trapezoid") else float(np.trapz(q, t))
            if v0 <= 0 or abs(vol - v0) <= max_volume_error * v0 or tol_k < 1e-7:
                break
            tol_k *= 0.5
        return Hydrograph(t, q, self.method, self.params, self.notes, self.t_ref_s)

    def summary(self):
        d = dict(self.params)
        d.update({"method": self.method, "peak_cfs": round(self.peak_cfs, 1),
                  "time_to_peak_hr": round(self.time_to_peak_hr, 3),
                  "duration_hr": round(self.duration_s / 3600.0, 3),
                  "volume_acft": round(self.volume_m3 / ACFT, 2)})
        if self.t_ref_s:
            d["breach_initiation_hr"] = round(self.t_ref_s / 3600.0, 3)
        return d


def read_csv_series(path, what="hydrograph", y_scale=1.0, x_scale=1.0):
    """Two numeric columns; header lines and blank lines are skipped."""
    xs, ys = [], []
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = [p.strip() for p in line.replace(";", ",").split(",")]
            if len(parts) < 2:
                parts = line.split()
            if len(parts) < 2:
                continue
            try:
                xv, yv = float(parts[0]), float(parts[1])
            except ValueError:
                continue
            xs.append(xv * x_scale)
            ys.append(yv * y_scale)
    if len(xs) < 2:
        raise BreachInputError("the {} file needs at least two numeric rows: {}".format(what, path))
    return np.asarray(xs), np.asarray(ys)


def read_user_hydrograph(path):
    """CSV with time (hours) and discharge (cfs) in the first two columns.
    A header line is allowed."""
    t, q = read_csv_series(path, "hydrograph", CFS, 3600.0)
    if np.any(np.diff(t) <= 0):
        raise BreachInputError("hydrograph times must increase")
    t = t - t[0]
    q = np.clip(q, 0.0, None)
    if q[-1] > 0:
        t = np.append(t, t[-1] + max(60.0, t[-1] - t[-2]))
        q = np.append(q, 0.0)
    return Hydrograph(t, q, METHOD_USER, {"source": path})


# ----------------------------------------------------------------------------
# Reservoir
# ----------------------------------------------------------------------------
class Reservoir:
    """Storage as a power law of depth above the reservoir bottom:
    V(y) = Vt (y / Ht)^m, with m chosen so the surface area at the top of dam
    matches (dV/dy = SA at y = Ht  =>  m = SA * Ht / Vt). Above the top of dam
    the same curve is extended.

    LiDAR does not see below the water surface, so the reservoir is described
    by its storage and surface area rather than by the DEM."""

    def __init__(self, bottom_m, top_m, v_top_m3, sa_top_m2=None):
        self.bottom = float(bottom_m)
        self.top = float(top_m)
        self.ht = self.top - self.bottom
        if self.ht <= 0:
            raise BreachInputError("the breach bottom must be below the top of dam")
        self.vt = float(v_top_m3)
        self.notes = []
        if sa_top_m2:
            m = sa_top_m2 * self.ht / self.vt
            if m < 1.0 or m > 6.0:
                self.notes.append(
                    "Storage-area exponent {:.2f} from storage, surface area and height is "
                    "outside 1-6; limited to that range. Check the storage and area values."
                    .format(m))
            self.m = min(6.0, max(1.0, m))
        else:
            self.m = 2.0
            self.notes.append("No surface area given: storage assumed to vary with depth "
                              "squared (m = 2).")

    def volume(self, wse):
        y = np.clip(np.asarray(wse, dtype=float) - self.bottom, 0.0, None)
        return self.vt * (y / self.ht) ** self.m

    def wse(self, volume):
        v = np.clip(np.asarray(volume, dtype=float), 0.0, None)
        return self.bottom + self.ht * (v / self.vt) ** (1.0 / self.m)


def _positive(name, value, allow_zero=False):
    """`value` as a float, or ValueError in plain words when it is not a positive finite number
    (zero allowed with allow_zero). v0.11.1 / 3.4.1: the equations below would otherwise return
    NaN or complex numbers, or divide by zero, for a missing / negative / zero input."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError("{} must be a number (got {!r})".format(name, value)) from None
    if not math.isfinite(v) or v < 0 or (v == 0 and not allow_zero):
        raise ValueError("{} must be a {} finite number (got {!r})".format(
            name, "non-negative" if allow_zero else "positive", value))
    return v


# ----------------------------------------------------------------------------
# Breach parameters and peak-discharge equations
# ----------------------------------------------------------------------------
def froehlich_2008(vw_m3, hb_m, mode="piping"):
    """Average breach width (m), formation time (s) and side slope (H:1V).
    Froehlich, D.C. (2008) J. Hydraulic Engineering 134(12)."""
    vw_m3, hb_m = _positive("reservoir volume Vw", vw_m3), _positive("breach height hb", hb_m)
    c = FROEHLICH_2008[mode]
    b_avg = 0.27 * c["ko"] * vw_m3 ** 0.32 * hb_m ** 0.04
    tf = 63.2 * math.sqrt(vw_m3 / (G * hb_m ** 2))
    return b_avg, tf, c["z"]


def froehlich_1995(vw_m3, hb_m, mode="piping"):
    """Froehlich (1995) average breach width (m), formation time (s), side slope.
    Bave = 0.1803 Ko Vw^0.32 hb^0.19; tf = 0.00254 Vw^0.53 hb^-0.90 (hours);
    Ko 1.4 overtopping / 1.0 piping; side slopes 1.4 / 0.9 H:1V."""
    vw_m3, hb_m = _positive("reservoir volume Vw", vw_m3), _positive("breach height hb", hb_m)
    c = FROEHLICH_1995[mode]
    b_avg = 0.1803 * c["ko"] * vw_m3 ** 0.32 * hb_m ** 0.19
    tf = 0.00254 * vw_m3 ** 0.53 * hb_m ** -0.90 * 3600.0
    return b_avg, tf, c["z"]


def froehlich_1995_peak(vw_m3, hw_m):
    """Froehlich (1995) regression for peak breach outflow, m3/s (a check)."""
    vw_m3, hw_m = _positive("reservoir volume Vw", vw_m3), _positive("water height hw", hw_m)
    return 0.607 * vw_m3 ** 0.295 * hw_m ** 1.24


def mlm_1984(vw_m3, hw_m, embankment_area_m2):
    """MacDonald & Langridge-Monopolis (1984), earthfill: eroded volume
    Ver = 0.0261 (Vw hw)^0.769 (m3), formation time tf = 0.0179 Ver^0.364 (hr).
    The average width is the eroded volume divided by the embankment
    cross-section area at the breach; side slopes taken as 1H:1V."""
    vw_m3, hw_m = _positive("reservoir volume Vw", vw_m3), _positive("water height hw", hw_m)
    embankment_area_m2 = _positive("embankment cross-section area", embankment_area_m2)
    ver = 0.0261 * (vw_m3 * hw_m) ** 0.769
    tf = 0.0179 * ver ** 0.364 * 3600.0
    return ver / embankment_area_m2, tf, 1.0


def vtg_1990(vw_m3, hw_m, erodible=True):
    """Von Thun & Gillette (1990): Bave = 2.5 hw + Cb (m), Cb from the reservoir
    size; tf = 0.015 hw (easily erodible) or 0.02 hw + 0.25 (erosion resistant)
    in hours; side slopes taken as 0.5H:1V."""
    vw_m3, hw_m = _positive("reservoir volume Vw", vw_m3), _positive("water height hw", hw_m)
    acft = vw_m3 / ACFT
    if acft < 1000.0:
        cb = 6.1
    elif acft < 5000.0:
        cb = 18.3
    elif acft < 10000.0:
        cb = 42.7
    else:
        cb = 54.9
    b = 2.5 * hw_m + cb
    tf_h = 0.015 * hw_m if erodible else 0.02 * hw_m + 0.25
    return b, tf_h * 3600.0, 0.5


def embankment_area_ft2(height_ft, crest_ft, us_h, ds_h):
    """Cross-section area of the embankment at the breach (ft2), trapezoid."""
    return height_ft * (crest_ft + 0.5 * (us_h + ds_h) * height_ft)


def tr60_peak_cfs(vs_acft, hw_ft, a_ft2=None, valley_width_ft=None):
    """NRCS TR-60 peak breach discharge (cfs) and the governing equation.

    Qmax = 1100 Br^1.35 with Br = Vs Hw / A (acres); not less than 3.2 Hw^2.5
    and need not exceed 65 Hw^1.85. For Hw >= 103 ft, 65 Hw^1.85 applies.
    In a narrow valley (L < 65 Hw^0.35 / 0.416) the upper limit is
    0.416 L Hw^1.5."""
    vs_acft, hw_ft = _positive("storage Vs", vs_acft, allow_zero=True), _positive("water height Hw", hw_ft)
    q_min = 3.2 * hw_ft ** 2.5
    q_max = 65.0 * hw_ft ** 1.85
    upper = "65 Hw^1.85"
    if valley_width_ft:
        t_width = 65.0 * hw_ft ** 0.35 / 0.416
        if valley_width_ft < t_width:
            q_max = 0.416 * valley_width_ft * hw_ft ** 1.5
            upper = "0.416 L Hw^1.5 (narrow valley)"
    if hw_ft >= 103.0:
        return q_max, upper, None
    if not a_ft2:
        return None, "needs the embankment cross-section area", None
    br = vs_acft * hw_ft / a_ft2
    q = 1100.0 * br ** 1.35
    rule = "1100 Br^1.35"
    if q < q_min:
        q, rule = q_min, "3.2 Hw^2.5 (lower limit)"
    if q > q_max:
        q, rule = q_max, upper + " (upper limit)"
    return q, rule, br


# ----------------------------------------------------------------------------
# Context shared by the hydrograph methods
# ----------------------------------------------------------------------------
def starting_wse_ft(dam):
    """ND 5.1.1: top of dam, or the PMF maximum reservoir level if lower."""
    if dam.pmf_max_wse_ft is not None and dam.pmf_max_wse_ft < dam.top_of_dam_ft:
        return dam.pmf_max_wse_ft, "PMF maximum reservoir level (lower than top of dam)"
    return dam.top_of_dam_ft, "top of dam"


class _Ctx:
    """Shared set-up for the hydrograph methods (SI units)."""


def _common(dam, wse0_m=None, wse0_rule=None):
    """The reservoir bottom is the top of dam minus the dam height. The breach
    bottom defaults to it but may be higher; storage below the breach bottom
    (dead storage) stays behind the dam. wse0_m is the pool level at breach
    initiation (default: the sunny-day rule)."""
    dam.validate()
    c = _Ctx()
    c.top_m = dam.top_of_dam_ft * FT
    res_bottom_m = c.top_m - dam.dam_height_ft * FT
    bottom_ft = (dam.breach_bottom_ft if dam.breach_bottom_ft is not None
                 else dam.top_of_dam_ft - dam.dam_height_ft)
    c.bottom_m = max(bottom_ft * FT, res_bottom_m)
    c.res = Reservoir(res_bottom_m, c.top_m, dam.storage_top_acft * ACFT,
                      dam.surface_area_top_ac * ACRE if dam.surface_area_top_ac else None)
    if wse0_m is None:
        wse0_ft, wse0_rule = starting_wse_ft(dam)
        c.wse0_m = wse0_ft * FT
    else:
        c.wse0_m = float(wse0_m)
        wse0_ft = c.wse0_m / FT
    if c.wse0_m <= c.bottom_m:
        raise BreachInputError("{}: the starting water surface is not above the breach bottom"
                               .format(dam.dam_id))
    c.vs = float(c.res.volume(c.wse0_m))                   # storage at failure
    c.v_dead = float(c.res.volume(c.bottom_m))             # below the breach bottom
    c.v_release = c.vs - c.v_dead
    c.notes = list(c.res.notes)
    if bottom_ft * FT < res_bottom_m - 1e-6:
        c.notes.append("The breach bottom is below the reservoir bottom (top of dam minus dam "
                       "height); it was raised to the reservoir bottom.")
    c.params = {
        "starting_wse_ft": round(wse0_ft, 2), "starting_wse_rule": wse0_rule,
        "breach_bottom_ft": round(c.bottom_m / FT, 2),
        "hw_ft": round((c.wse0_m - c.bottom_m) / FT, 2),
        "hb_ft": round((c.top_m - c.bottom_m) / FT, 2),
        "storage_at_failure_acft": round(c.vs / ACFT, 2),
        "dead_storage_acft": round(c.v_dead / ACFT, 2),
        "releasable_acft": round(c.v_release / ACFT, 2),
        "storage_exponent_m": round(c.res.m, 3),
    }
    return c


def _embankment_area(dam):
    if dam.crest_width_ft and dam.us_slope_h and dam.ds_slope_h:
        return embankment_area_ft2(dam.dam_height_ft, dam.crest_width_ft,
                                   dam.us_slope_h, dam.ds_slope_h)
    return None


def breach_parameters(method, c, dam, ov):
    """Average width (m), formation time (s), side slope, source text, mode
    ('piping' or 'overtopping'), notes."""
    hb = c.top_m - c.bottom_m
    hw = c.wse0_m - c.bottom_m
    notes = []
    mode = "piping"
    if method in (METHOD_PIPING, METHOD_PIPING_ORIFICE):
        b, tf, z = froehlich_2008(c.vs, hb, "piping")
        src = "Froehlich (2008), piping"
    elif method == METHOD_OVERTOPPING:
        mode = "overtopping"
        b, tf, z = froehlich_2008(c.vs, hb, "overtopping")
        src = "Froehlich (2008), overtopping"
    elif method == METHOD_F95_PIPING:
        b, tf, z = froehlich_1995(c.vs, hb, "piping")
        src = "Froehlich (1995), piping"
    elif method == METHOD_F95_OVERTOPPING:
        mode = "overtopping"
        b, tf, z = froehlich_1995(c.vs, hb, "overtopping")
        src = "Froehlich (1995), overtopping"
    elif method == METHOD_MLM:
        a_ft2 = _embankment_area(dam)
        if a_ft2 is None:
            a_ft2 = embankment_area_ft2(dam.dam_height_ft, 12.0, 3.0, 3.0)
            notes.append("MacDonald & Langridge-Monopolis needs the embankment cross-section "
                         "(crest width and slopes); none given, so a 12 ft crest with 3H:1V "
                         "slopes was assumed. Enter the real geometry.")
        b, tf, z = mlm_1984(c.vs, hw, a_ft2 * FT * FT)
        src = "MacDonald & Langridge-Monopolis (1984), earthfill"
        notes.append("MacDonald & Langridge-Monopolis side slope taken as 1H:1V; override it "
                     "if the embankment differs.")
    elif method == METHOD_VTG:
        b, tf, z = vtg_1990(c.vs, hw, ov.vtg_erodible)
        src = "Von Thun & Gillette (1990), {}".format("easily erodible" if ov.vtg_erodible
                                                       else "erosion resistant")
        notes.append("Von Thun & Gillette side slope taken as 0.5H:1V; override it if needed.")
    else:
        raise BreachInputError("not a routed breach method: {}".format(method))
    if ov.avg_width_ft:
        b = ov.avg_width_ft * FT
        src += "; width from user"
    if ov.formation_hr:
        tf = ov.formation_hr * 3600.0
        src += "; formation time from user"
    if ov.side_slope_h is not None:
        z = ov.side_slope_h
        src += "; side slope from user"
    return b, tf, z, src, mode, notes


def _weir(b, z, h):
    if h <= 0:
        return 0.0
    return WEIR_C_BOTTOM_SI * b * h ** 1.5 + WEIR_C_SIDE_SI * z * h ** 2.5


class BreachOpening:
    """The opening in the embankment as a function of time since initiation.

    Weir opening: a trapezoid whose crest lowers from the water surface (or
    the top of dam, if the pool is above it) to the breach bottom, and whose
    bottom width grows to `b_bottom`, both linearly over the formation time.

    With `pipe` set (piping with an orifice phase): a circular pipe at the
    breach bottom grows from d0 to dc over pipe_frac * tf and flows as an
    orifice; then the roof collapses and the opening becomes a weir channel at
    the breach bottom whose width grows from dc to b_bottom and whose side
    slopes grow from 0 to z by tf."""

    def __init__(self, top_m, bottom_m, wse_start_m, b_bottom, z, tf, pipe=None):
        self.top, self.bottom = float(top_m), float(bottom_m)
        self.zc0 = min(float(wse_start_m), self.top)
        self.b_bottom, self.z, self.tf = float(b_bottom), float(z), float(tf)
        self.pipe = pipe            # None or (d0_m, dc_m, tc_s, cd)

    def q(self, t, wse):
        if self.pipe is None:
            frac = min(1.0, t / self.tf)
            zb = self.zc0 - frac * (self.zc0 - self.bottom)
            return _weir(self.b_bottom * frac, self.z, wse - zb)
        d0, dc, tc, cd = self.pipe
        if t < tc:
            d = d0 + (dc - d0) * t / tc
            h = wse - self.bottom
            if h <= 0:
                return 0.0
            if wse >= self.bottom + d:
                return cd * (math.pi * d * d / 4.0) * math.sqrt(2.0 * G * (h - d / 2.0))
            return _weir(d, 0.0, h)
        s = 1.0 if self.tf <= tc else min(1.0, (t - tc) / (self.tf - tc))
        b = dc + max(0.0, self.b_bottom - dc) * s
        return _weir(b, self.z * s, wse - self.bottom)

    def top_width(self, t, wse):
        """Approximate width of the water surface across the opening (m)."""
        if self.pipe is None:
            frac = min(1.0, t / self.tf)
            zb = self.zc0 - frac * (self.zc0 - self.bottom)
            h = max(0.0, wse - zb)
            return self.b_bottom * frac + 2.0 * self.z * h
        d0, dc, tc, cd = self.pipe
        if t < tc:
            return 0.0
        s = 1.0 if self.tf <= tc else min(1.0, (t - tc) / (self.tf - tc))
        return dc + max(0.0, self.b_bottom - dc) * s + 2.0 * self.z * s * max(0.0, wse - self.bottom)


def make_opening(method, c, dam, ov, wse_start_m):
    """Opening geometry and the parameter dict entries for a routed method."""
    b_avg, tf, z, src, mode, notes = breach_parameters(method, c, dam, ov)
    hb = c.top_m - c.bottom_m
    b_bottom = b_avg - z * hb
    if b_bottom < 0:
        notes.append("The average breach width is narrower than a triangle of this height and "
                     "side slope; bottom width set to 0.")
        b_bottom = 0.0
    tf = max(tf, 60.0)
    pipe = None
    p = {}
    if method == METHOD_PIPING_ORIFICE:
        d0 = max(0.02, ov.pipe_initial_dia_ft * FT)
        dc = min(max(ov.pipe_collapse_frac * hb, 2.0 * d0), 0.6 * hb)
        tc = min(max(ov.pipe_time_fraction, 0.05), 0.95) * tf
        pipe = (d0, dc, tc, ov.pipe_cd)
        p.update({"pipe_initial_dia_ft": round(d0 / FT, 2), "pipe_collapse_dia_ft": round(dc / FT, 2),
                  "pipe_phase_hr": round(tc / 3600.0, 3), "pipe_cd": ov.pipe_cd})
        notes.append("Pipe phase: the pipe sits at the breach bottom, grows linearly to the "
                     "collapse diameter, then the roof falls in and the opening widens as a "
                     "weir channel. The pipe size, the collapse diameter and the share of the "
                     "formation time are assumptions; adjust them to the geotechnical review.")
    opening = BreachOpening(c.top_m, c.bottom_m, wse_start_m, b_bottom, z, tf, pipe)
    p.update({"breach_parameters": src, "failure_mode": mode,
              "avg_breach_width_ft": round(b_avg / FT, 1),
              "bottom_width_ft": round(b_bottom / FT, 1), "side_slope_h": z,
              "formation_time_hr": round(tf / 3600.0, 3)})
    return opening, p, notes


# ----------------------------------------------------------------------------
# Sunny-day hydrographs
# ----------------------------------------------------------------------------
def apply_partial(dam, ov):
    """A dam whose breach bottom is raised for a partial breach (`ov.depth_fraction` of the
    dam height, measured down from the top of dam). The storage below the breach bottom stays
    behind the dam. Without the option, or when the bottom the user gave is already higher,
    the dam is returned unchanged."""
    f = getattr(ov, "depth_fraction", None) if ov is not None else None
    if not f or f >= 0.999:
        return dam
    if f < 0.05:
        raise BreachInputError("{}: the partial breach depth must be at least 5% of the dam "
                               "height".format(dam.dam_id))
    if dam.top_of_dam_ft is None or not dam.dam_height_ft:
        return dam
    bottom = dam.top_of_dam_ft - f * dam.dam_height_ft
    if dam.breach_bottom_ft is not None and dam.breach_bottom_ft >= bottom:
        return dam
    return dataclasses.replace(dam, breach_bottom_ft=bottom)


def routed_hydrograph(dam, method=METHOD_PIPING, overrides=None, dt_s=None, max_hours=240.0):
    """Level-pool routing through the opening (no inflow, no tailwater: sunny
    day, no downstream flooding)."""
    ov = overrides or BreachOverrides()
    dam = apply_partial(dam, ov)
    c = _common(dam)
    res, wse0_m, vs, params = c.res, c.wse0_m, c.vs, c.params
    opening, p, bnotes = make_opening(method, c, dam, ov, wse0_m)
    tf = opening.tf
    notes = list(c.notes) + bnotes
    if dt_s is None:
        dt_s = min(10.0, tf / 300.0)

    def q_at(t, v):
        return opening.q(t, float(res.wse(v)))

    t, v = 0.0, vs
    ts, qs = [0.0], [q_at(0.0, vs)]
    t_max = max_hours * 3600.0
    qpeak = 0.0
    while t < t_max:
        q1 = q_at(t, v)
        v_half = max(0.0, v - 0.5 * dt_s * q1)
        q2 = q_at(t + 0.5 * dt_s, v_half)
        dv = min(v, dt_s * q2)
        v -= dv
        t += dt_s
        q = q_at(t, v)
        ts.append(t)
        qs.append(q)
        qpeak = max(qpeak, q)
        if t > tf and (v - c.v_dead < 0.002 * c.v_release
                       or (qpeak > 0 and q < 0.002 * qpeak)):
            break
        # coarser steps on the recession
        if t > 2 * tf:
            dt_s = min(60.0, dt_s * 1.02)
    if qs[-1] > 0:
        ts.append(ts[-1] + dt_s)
        qs.append(0.0)
    left = v - c.v_dead
    if left > 0.01 * c.v_release:
        notes.append("The breach outflow was stopped after {:.0f} h with {:.0f} ac-ft ({:.1f}%) "
                     "of the releasable storage still in the reservoir (low head over a large "
                     "reservoir). The flood volume is that much smaller than the storage; "
                     "raise the breach width or check the dam height and storage."
                     .format(t / 3600.0, left / ACFT, 100.0 * left / c.v_release))
    params.update(p)
    params.update({
        "released_acft": round((c.vs - v) / ACFT, 2),
        "froehlich_1995_peak_cfs": round(froehlich_1995_peak(vs, wse0_m - c.bottom_m) / CFS, 0),
    })
    hy = Hydrograph(np.asarray(ts), np.asarray(qs), method, params, notes)
    _add_tr60_check(dam, hy)
    return hy


def piping_hydrograph(dam, overrides=None, dt_s=None, max_hours=240.0):
    """Piping-mode Froehlich (2008) breach, routed (the default method)."""
    return routed_hydrograph(dam, METHOD_PIPING, overrides, dt_s, max_hours)


def curvilinear_shape(qp, tr, volume, n=200):
    """Curvilinear breach hydrograph: a smooth (sin^2) rise to the peak at tr
    and an exponential recession whose time constant makes the volume equal
    `volume`. Returns None when the volume is too small for that shape."""
    qp, tr = _positive("peak discharge", qp), _positive("time to peak", tr)
    volume = _positive("hydrograph volume", volume, allow_zero=True)
    # rise: qp * tr / 2; recession cut at 7 time constants: qp * k * (1 - e^-7)
    k = (volume / qp - 0.5 * tr) / (1.0 - math.exp(-7.0))
    if k <= 0.05 * tr:
        return None
    t_rise = np.linspace(0.0, tr, n)
    q_rise = qp * np.sin(0.5 * math.pi * t_rise / tr) ** 2
    t_rec = tr + np.linspace(0.0, 7.0 * k, 2 * n)[1:]
    q_rec = qp * np.exp(-(t_rec - tr) / k)
    t = np.concatenate([t_rise, t_rec, [t_rec[-1] + 0.005 * k]])
    q = np.concatenate([q_rise, q_rec, [0.0]])
    return t, q


def tr60_hydrograph(dam, overrides=None, shape="triangular", wse0_m=None, wse0_rule=None):
    """TR-60 peak with a hydrograph whose volume equals the releasable storage
    (the storage above the breach bottom). Triangular, or curvilinear (sin^2
    rise, exponential recession). The rise time is the Froehlich (2008) piping
    formation time unless given."""
    ov = overrides or BreachOverrides()
    dam = apply_partial(dam, ov)
    c = _common(dam, wse0_m, wse0_rule)
    top_m, bottom_m, wse0_m, vs, params = c.top_m, c.bottom_m, c.wse0_m, c.vs, c.params
    a = _embankment_area(dam)
    q_cfs, rule, br = tr60_peak_cfs(vs / ACFT, (wse0_m - bottom_m) / FT, a, dam.valley_width_ft)
    if q_cfs is None:
        raise BreachInputError("{}: TR-60 needs the crest width and the upstream and "
                               "downstream slopes to compute the embankment area"
                               .format(dam.dam_id))
    qp = q_cfs * CFS
    if ov.tr60_rise_hr:
        tr = ov.tr60_rise_hr * 3600.0
    else:
        _, tr, _ = froehlich_2008(vs, top_m - bottom_m, "piping")
    tb = 2.0 * c.v_release / qp
    notes = list(c.notes)
    if tr >= tb:
        tr = 0.25 * tb
        notes.append("Rise time longer than the triangle base allowed by the volume; set to "
                     "a quarter of the base.")
    curve = curvilinear_shape(qp, tr, c.v_release) if shape == "curvilinear" else None
    if shape == "curvilinear" and curve is None:
        notes.append("The releasable volume is too small for a curvilinear shape with this "
                     "peak; a triangle was used.")
    if curve is not None:
        t, q = curve
        params.update({"shape": "curvilinear (sin^2 rise, exponential recession), volume = "
                                "releasable storage",
                       "base_time_hr": round(float(t[-1]) / 3600.0, 3)})
    else:
        t = np.array([0.0, tr, tb])
        q = np.array([0.0, qp, 0.0])
        params.update({"shape": "triangular, volume = releasable storage",
                       "base_time_hr": round(tb / 3600.0, 3)})
    params.update({"tr60_peak_cfs": round(q_cfs, 0), "tr60_rule": rule,
                   "breach_factor_br_ac": None if br is None else round(br, 2),
                   "embankment_area_ft2": None if a is None else round(a, 0),
                   "rise_time_hr": round(tr / 3600.0, 3),
                   "shape_note": "TR-60 gives the peak only; TR-66 postulates triangular and "
                                 "curvilinear shapes without publishing ordinates. The rise "
                                 "time is the Froehlich (2008) formation time. Use your own CSV "
                                 "if ND DWR expects another shape."})
    return Hydrograph(t, q, METHOD_TR60_CURVE if curve is not None else METHOD_TR60, params, notes)


def _add_tr60_check(dam, hy):
    """Report the TR-60 peak next to the routed peak, when it can be computed."""
    try:
        vs = hy.params["storage_at_failure_acft"]
        hw = hy.params["hw_ft"]
        q, rule, _ = tr60_peak_cfs(vs, hw, _embankment_area(dam), dam.valley_width_ft)
        if q is not None:
            hy.params["tr60_peak_cfs_check"] = round(q, 0)
            hy.params["tr60_rule_check"] = rule
    except Exception:  # a check only
        pass


def make_hydrograph(dam, method=METHOD_PIPING, overrides=None, user_csv=None):
    """Sunny-day breach hydrograph for any method."""
    if method in ROUTED_METHODS:
        return routed_hydrograph(dam, method, overrides)
    if method == METHOD_TR60:
        return tr60_hydrograph(dam, overrides)
    if method == METHOD_TR60_CURVE:
        return tr60_hydrograph(dam, overrides, shape="curvilinear")
    if method == METHOD_USER:
        if not user_csv:
            raise BreachInputError("a hydrograph CSV is required for the user method")
        hy = read_user_hydrograph(user_csv)
        if dam is not None:
            try:
                c = _common(dam)
                hy.params.update(c.params)
                if hy.volume_m3 > 1.05 * c.v_release:
                    hy.notes.append("The hydrograph volume ({:.0f} ac-ft) is more than the "
                                    "releasable storage ({:.0f} ac-ft)."
                                    .format(hy.volume_m3 / ACFT, c.v_release / ACFT))
            except BreachInputError:
                pass
        return hy
    raise BreachInputError("unknown breach method: {}".format(method))


def dam_from_dict(d):
    keys = DamData.__dataclass_fields__.keys()
    return DamData(**{k: d[k] for k in keys if k in d})


def dam_to_dict(dam):
    return asdict(dam)
