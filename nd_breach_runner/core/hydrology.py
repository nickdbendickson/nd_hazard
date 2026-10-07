"""Rainfall -> PMF inflow hydrograph.

The flood scenario (`flood.FloodInputs`) needs the reservoir inflow hydrograph as a CSV
(time hr, discharge cfs). This module builds it from a storm you supply:

    storm depth (PMP or a design storm; NOT generated here, take it from your own PMP
    study / HMR / NOAA Atlas)  x  temporal distribution  ->  rainfall increments
    rainfall increments - losses (SCS curve number, constant loss, or none)  ->  excess
    excess (*) unit hydrograph (SCS dimensionless, Clark, or SCS shape with a given lag)
    + constant base flow (snowmelt / antecedent flow)  ->  inflow hydrograph

Everything is lumped (one basin, one storm, one set of losses). Public API:

    hydrograph_from_settings(dict) -> HydrologyResult
    write_csv(result, path)
    unit_hydrograph(...)                      the unit hydrograph on its own
    tc_scs_lag_hr(...), tc_kirpich_hr(...)    time of concentration formulas
    scs_type2_fraction(x), triangular_fraction(x, p), read_cumulative_csv(path)

Settings (US units; only `area_mi2` and `storm_depth_in` are always required):

    area_mi2, storm_depth_in, duration_hr (24)
    distribution         "scs_type2" (default) | "uniform" | "triangular" | "user"
    peak_position        triangular: "front" 0.25 | "centre" 0.5 | "back" 0.75 | number 0-1
    dist_csv             user: CSV, time (hr) and cumulative fraction (0-1 or percent)
    loss_method          "cn" (default) | "constant" | "none"
    cn, amc ("II"), ia_ratio (0.2)                  curve number losses
    initial_loss_in, loss_rate_in_hr                constant losses
    base_flow_cfs (0)    constant base flow added over the whole record
    tc_method            "user" (default) | "scs_lag" | "kirpich"
    tc_hr                user time of concentration
    flow_length_ft, slope_pct                       for scs_lag / kirpich
    uh_method            "scs" (default) | "clark" | "lag"
    prf (484)            SCS peak rate factor (adjustable, usually 300-600)
    lag_hr               uh_method "lag": basin lag, used with the SCS shape
    clark_tc_hr (default tc), clark_r_hr            Clark parameters
    dt_min               time step override in minutes (default from tc)

Time zero of the result is the start of the storm. The flood scenario treats the inflow as
zero after the last row and starts routing from the normal pool at time zero.
"""
import math
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from .units import ACFT, CFS, MILE

IN_M = 0.0254
MI2_M2 = MILE * MILE
UNIT_VOLUME_M3 = MI2_M2 * IN_M                     # 1 inch of runoff over 1 mi2, m3
CFS_HR_PER_MI2_IN = 640.0 * 43560.0 / 12.0 / 3600.0    # = 645.33, the 484 hydrograph's area
VOLUME_TOLERANCE = 0.01                            # volume conservation, fraction
SCS_LAG_TC_RATIO = 0.6                             # lag = 0.6 tc
SCS_LAG_AREA_LIMIT_MI2 = 2000.0 / 640.0            # commonly cited limit of the lag equation
MAX_STEPS = 400000

DISTRIBUTIONS = ("scs_type2", "uniform", "triangular", "user")
LOSS_METHODS = ("cn", "constant", "none")
TC_METHODS = ("user", "scs_lag", "kirpich")
UH_METHODS = ("scs", "clark", "lag")
PEAK_POSITIONS = {"front": 0.25, "centre": 0.5, "center": 0.5, "back": 0.75}

# NRCS 24-hr Type II cumulative rainfall (P/P24) at the standard tabulated times.
# Enter/verify against NEH Part 630 Ch. 4 (or TR-55 Table B-1) before regulatory use.
_TYPE_II = ((0, 0.000), (1, 0.011), (2, 0.022), (3, 0.035), (4, 0.048), (5, 0.063),
            (6, 0.080), (7, 0.098), (8, 0.120), (8.5, 0.133), (9, 0.147), (9.5, 0.163),
            (10, 0.181), (10.5, 0.204), (11, 0.235), (11.5, 0.283), (11.75, 0.357),
            (12, 0.663), (12.5, 0.735), (13, 0.772), (13.5, 0.799), (14, 0.820),
            (15, 0.854), (16, 0.880), (17, 0.903), (18, 0.922), (19, 0.938), (20, 0.952),
            (21, 0.965), (22, 0.977), (23, 0.989), (24, 1.000))
SCS_TYPE_II_HR = np.array([p[0] for p in _TYPE_II], dtype=float)
SCS_TYPE_II_CUM = np.array([p[1] for p in _TYPE_II], dtype=float)

DEFAULTS = {
    "area_mi2": None, "storm_depth_in": None, "duration_hr": 24.0,
    "distribution": "scs_type2", "peak_position": 0.5, "dist_csv": None,
    "loss_method": "cn", "cn": None, "amc": "II", "ia_ratio": 0.2,
    "initial_loss_in": 0.0, "loss_rate_in_hr": 0.0,
    "base_flow_cfs": 0.0,
    "tc_method": "user", "tc_hr": None, "flow_length_ft": None, "slope_pct": None,
    "uh_method": "scs", "prf": 484.0, "lag_hr": None,
    "clark_tc_hr": None, "clark_r_hr": None,
    "dt_min": None,
}
# key: (minimum, maximum, minimum allowed itself, maximum allowed itself)
_NUMERIC = {
    "area_mi2": (0.0, None, False, True), "storm_depth_in": (0.0, None, False, True),
    "duration_hr": (0.0, None, False, True), "cn": (0.0, 100.0, False, True),
    "ia_ratio": (0.0, 0.5, True, True), "initial_loss_in": (0.0, None, True, True),
    "loss_rate_in_hr": (0.0, None, True, True), "base_flow_cfs": (0.0, None, True, True),
    "tc_hr": (0.0, None, False, True), "flow_length_ft": (0.0, None, False, True),
    "slope_pct": (0.0, None, False, True), "prf": (50.0, 1500.0, True, True),
    "lag_hr": (0.0, None, False, True), "clark_tc_hr": (0.0, None, False, True),
    "clark_r_hr": (0.0, None, False, True), "dt_min": (0.0, None, False, True),
}


class HydrologyError(ValueError):
    """Bad input, or a result that fails a check."""


def _trapz(y, x):
    return float(np.trapezoid(y, x)) if hasattr(np, "trapezoid") else float(np.trapz(y, x))


# ============================================================================================
# Temporal distributions (cumulative fraction of the storm depth against fraction of the time)
# ============================================================================================
def uniform_fraction(x):
    return np.clip(np.asarray(x, dtype=float), 0.0, 1.0)


def scs_type2_fraction(x):
    """NRCS Type II cumulative fraction at x = t / duration (the 24-hr curve stretched to the
    storm duration)."""
    return np.interp(np.asarray(x, dtype=float) * 24.0, SCS_TYPE_II_HR, SCS_TYPE_II_CUM)


def triangular_fraction(x, peak_position=0.5):
    """Triangular hyetograph: intensity rises linearly from zero to a peak at
    `peak_position` (fraction of the storm) and falls linearly to zero at the end."""
    p = float(peak_position)
    if not 0.0 < p < 1.0:
        raise HydrologyError("the triangular peak position must be between 0 and 1")
    x = np.clip(np.asarray(x, dtype=float), 0.0, 1.0)
    return np.where(x <= p, x * x / p, 1.0 - (1.0 - x) ** 2 / (1.0 - p))


def read_cumulative_csv(path):
    """A cumulative-rainfall CSV: time (hr from the storm start) and cumulative fraction of the
    storm depth (0-1, or percent). Header lines are skipped. Returns (x, F, last_time_hr) with
    x = time / last time; F starts at 0, never decreases and ends at 1."""
    t, f = [], []
    try:
        fh = open(path)
    except OSError as e:
        raise HydrologyError("cannot read the temporal distribution CSV: {}".format(e))
    with fh:
        for line in fh:
            parts = [p.strip() for p in line.replace(";", ",").split(",")]
            if len(parts) < 2:
                parts = line.split()
            if len(parts) < 2:
                continue
            try:
                tv, fv = float(parts[0]), float(parts[1])
            except ValueError:
                continue
            t.append(tv)
            f.append(fv)
    if len(t) < 2:
        raise HydrologyError("the temporal distribution CSV needs at least two numeric rows "
                             "(time hr, cumulative fraction): {}".format(path))
    t, f = np.asarray(t), np.asarray(f)
    if t[0] < 0 or np.any(np.diff(t) <= 0):
        raise HydrologyError("the temporal distribution times must be >= 0 and increase")
    if np.nanmax(f) > 1.5:
        if np.nanmax(f) > 100.5:
            raise HydrologyError("the cumulative fractions must be 0-1 (or percent, 0-100)")
        f = f / 100.0
    if t[0] > 0:
        t, f = np.concatenate(([0.0], t)), np.concatenate(([0.0], f))
    if abs(f[0]) > 1e-9:
        raise HydrologyError("the cumulative distribution must start at 0 at time 0")
    if np.any(np.diff(f) < -1e-9):
        raise HydrologyError("the cumulative distribution must not decrease")
    if not 0.99 <= f[-1] <= 1.01:
        raise HydrologyError("the cumulative distribution must end at 1 (100 %); it ends at "
                             "{:g}".format(f[-1]))
    f = np.maximum.accumulate(f / f[-1])
    return t / t[-1], f, float(t[-1])


# ============================================================================================
# Time of concentration
# ============================================================================================
def tc_scs_lag_hr(flow_length_ft, slope_pct, cn):
    """SCS (NRCS) lag equation, tc = lag / 0.6:

        lag [hr] = l^0.8 (S + 1)^0.7 / (1900 Y^0.5),   S = 1000 / CN - 10

    l = hydraulic length (ft), Y = average watershed slope (%), CN = the (AMC II) curve number.
    Developed for small watersheds (the limit usually quoted is 2,000 acres)."""
    s = 1000.0 / cn - 10.0
    lag = flow_length_ft ** 0.8 * (s + 1.0) ** 0.7 / (1900.0 * math.sqrt(slope_pct))
    return lag / SCS_LAG_TC_RATIO


def tc_kirpich_hr(flow_length_ft, slope_pct):
    """Kirpich: tc [min] = 0.0078 L^0.77 S^-0.385, L = length of the main flow path (ft),
    S = its slope (ft/ft; given here as percent). An empirical small-basin formula."""
    s = slope_pct / 100.0
    return 0.0078 * flow_length_ft ** 0.77 * s ** -0.385 / 60.0


# ============================================================================================
# Losses
# ============================================================================================
def adjust_cn(cn, amc="II"):
    """AMC I / III equivalents of an AMC II curve number (NEH-4 Ch. 10 conversions)."""
    amc = str(amc).upper()
    if amc == "II":
        return float(cn)
    if amc == "I":
        return min(100.0, 4.2 * cn / (10.0 - 0.058 * cn))
    if amc == "III":
        return min(100.0, 23.0 * cn / (10.0 + 0.13 * cn))
    raise HydrologyError("the antecedent condition must be I, II or III")


def scs_runoff_in(p_cum_in, cn, ia_ratio=0.2):
    """Cumulative runoff Q = (P - Ia)^2 / (P - Ia + S) for P > Ia, S = 1000/CN - 10,
    Ia = ia_ratio * S. (Ia = 0.2 S gives the familiar (P - 0.2S)^2 / (P + 0.8S).)"""
    p = np.asarray(p_cum_in, dtype=float)
    s = 1000.0 / cn - 10.0
    x = np.maximum(p - ia_ratio * s, 0.0)
    den = x + s
    return np.divide(x * x, den, out=np.zeros_like(x), where=den > 0)


def _excess(rain_in, dt_s, s, warnings):
    """Incremental excess rainfall (in) for each step."""
    method = s["loss_method"]
    p_total = float(rain_in.sum())
    cn_eff = None
    if method == "none":
        return rain_in.copy(), cn_eff
    if method == "cn":
        cn_eff = adjust_cn(s["cn"], s["amc"])
        q_cum = scs_runoff_in(np.concatenate(([0.0], np.cumsum(rain_in))), cn_eff, s["ia_ratio"])
        ex = np.maximum(np.diff(q_cum), 0.0)
        smax = 1000.0 / cn_eff - 10.0
        ia = s["ia_ratio"] * smax
        q_tot = float(q_cum[-1])
        if p_total <= ia:
            warnings.append("The storm depth ({:.2f} in) does not exceed the initial abstraction "
                            "({:.2f} in): no runoff. Check the curve number.".format(p_total, ia))
        elif q_tot < 0.5 * p_total:
            warnings.append(
                "The curve number {:.0f} (AMC {}) turns only {:.0f} % of a {:.1f} in storm into "
                "runoff. Curve numbers are calibrated on ordinary storms; for a PMP-scale storm "
                "check that the loss assumption is not unconservatively low for the PMF (wetter "
                "antecedent condition, higher CN, or frozen ground).".format(
                    cn_eff, s["amc"], 100.0 * q_tot / p_total, p_total))
        if s["amc"] == "I":
            warnings.append("Dry antecedent condition (AMC I) is an unusual assumption for a "
                            "PMF; check it.")
        return ex, cn_eff
    # constant initial loss and constant loss rate afterwards
    ia_left = s["initial_loss_in"]
    rate_step = s["loss_rate_in_hr"] * dt_s / 3600.0
    ex = np.zeros_like(rain_in)
    for j, dp in enumerate(rain_in):
        if dp <= 0:
            continue
        take = min(dp, ia_left)
        ia_left -= take
        rem = dp - take
        ex[j] = max(0.0, rem - rate_step * (rem / dp))
    if float(ex.sum()) <= 0:
        warnings.append("The losses absorb the whole storm: no runoff. Check the initial loss "
                        "and loss rate.")
    return ex, cn_eff


# ============================================================================================
# Unit hydrographs. All return (t_s, u) with u in m3/s per inch of excess over the basin, for
# an excess pulse of duration dt, sampled at t = m dt (u[0] = 0), with sum(u) * dt equal to
# 1 inch over the basin exactly.
# ============================================================================================
def gamma_shape_from_prf(prf):
    """M of the gamma (curvilinear) dimensionless hydrograph q/qp = (x e^(1-x))^M whose peak
    rate factor is `prf` (cfs per mi2 per inch, for time in hours). 484 gives M of about 3.7."""
    def peak_factor(m):
        return CFS_HR_PER_MI2_IN * math.exp((m + 1.0) * math.log(m) - m - math.lgamma(m + 1.0))
    lo, hi = 1e-3, 1e4
    if not peak_factor(lo) < prf < peak_factor(hi):
        raise HydrologyError("cannot fit a dimensionless hydrograph to a peak rate factor of "
                             "{:g}".format(prf))
    for _ in range(200):
        mid = math.sqrt(lo * hi)
        if peak_factor(mid) < prf:
            lo = mid
        else:
            hi = mid
    return math.sqrt(lo * hi)


def _scs_uh(area_mi2, dt_s, lag_s, prf):
    tp = 0.5 * dt_s + lag_s
    m = gamma_shape_from_prf(prf)
    n = max(4, int(math.ceil(40.0 * tp / dt_s)) + 2)
    x = np.arange(n) * dt_s / tp
    with np.errstate(over="ignore", under="ignore"):
        u = (x * np.exp(1.0 - x)) ** m
    above = np.nonzero(u > 1e-4 * u.max())[0]
    u = u[:int(above[-1]) + 2]
    return u


def _time_area(x):
    x = np.clip(np.asarray(x, dtype=float), 0.0, 1.0)
    r = math.sqrt(2.0)
    return np.where(x <= 0.5, r * x ** 1.5, 1.0 - r * (1.0 - x) ** 1.5)


def _clark_uh(area_mi2, dt_s, tc_s, r_s):
    v = area_mi2 * UNIT_VOLUME_M3
    n = max(1, int(math.ceil(tc_s / dt_s - 1e-9)))
    edges = np.minimum(np.arange(n + 1) * dt_s, tc_s)
    inflow = np.diff(_time_area(edges / tc_s)) * v / dt_s          # m3/s in each step
    c = min(1.0, 2.0 * dt_s / (2.0 * r_s + dt_s))
    o = [0.0]
    peak = 0.0
    k = 0
    while True:
        k += 1
        i = inflow[k - 1] if k <= n else 0.0
        ok = c * i + (1.0 - c) * o[-1]
        o.append(ok)
        peak = max(peak, ok)
        if k >= n and ok < 1e-4 * peak:
            break
        if k > MAX_STEPS:
            raise HydrologyError("the Clark unit hydrograph is too long for the time step; "
                                 "give a larger time step or a smaller R")
    o = np.asarray(o)
    return np.concatenate((0.5 * (o + np.concatenate(([0.0], o[:-1]))), [0.5 * o[-1]]))


def unit_hydrograph(area_mi2, dt_s, method="scs", lag_hr=None, prf=484.0, clark_tc_hr=None,
                    clark_r_hr=None):
    """Unit hydrograph for excess pulses of dt_s seconds over area_mi2. method "scs" and "lag"
    are the SCS shape (tp = dt/2 + lag, peak rate factor prf; give lag_hr, which for "scs"
    from a tc is 0.6 tc); "clark" is a linear reservoir (R) fed by a time-area curve (Tc).
    Returns (t_s, u_cms_per_inch)."""
    dt_s = float(dt_s)
    if method in ("scs", "lag"):
        if not lag_hr or lag_hr <= 0:
            raise HydrologyError("the SCS unit hydrograph needs a lag time")
        u = _scs_uh(area_mi2, dt_s, lag_hr * 3600.0, prf)
    elif method == "clark":
        if not clark_tc_hr or not clark_r_hr:
            raise HydrologyError("the Clark unit hydrograph needs Tc and R")
        u = _clark_uh(area_mi2, dt_s, clark_tc_hr * 3600.0, clark_r_hr * 3600.0)
    else:
        raise HydrologyError("unknown unit hydrograph method: {}".format(method))
    u = u * (area_mi2 * UNIT_VOLUME_M3 / (float(u.sum()) * dt_s))
    return np.arange(len(u)) * dt_s, u


# ============================================================================================
# Settings
# ============================================================================================
def _normalise(settings):
    if not isinstance(settings, dict):
        raise HydrologyError("settings must be a dict")
    unknown = sorted(set(settings) - set(DEFAULTS))
    if unknown:
        raise HydrologyError("unknown setting(s): {}".format(", ".join(unknown)))
    s = dict(DEFAULTS)
    for k, v in settings.items():
        if isinstance(v, str) and not v.strip():
            v = None
        if v is not None:
            s[k] = v
    for k, (lo, hi, lo_in, hi_in) in _NUMERIC.items():
        v = s[k]
        if v is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise HydrologyError("{} must be a number, not {!r}".format(k, v))
        if not math.isfinite(v):
            raise HydrologyError("{} must be a finite number".format(k))
        if lo is not None and (v < lo or (v == lo and not lo_in)):
            raise HydrologyError("{} must be {} {:g} (got {:g})".format(
                k, ">=" if lo_in else ">", lo, v))
        if hi is not None and (v > hi or (v == hi and not hi_in)):
            raise HydrologyError("{} must be {} {:g} (got {:g})".format(
                k, "<=" if hi_in else "<", hi, v))
        s[k] = v
    for k, choices in (("distribution", DISTRIBUTIONS), ("loss_method", LOSS_METHODS),
                       ("tc_method", TC_METHODS), ("uh_method", UH_METHODS)):
        v = str(s[k]).strip().lower()
        if v not in choices:
            raise HydrologyError("{} must be one of {} (got {!r})".format(
                k, ", ".join(choices), s[k]))
        s[k] = v
    s["amc"] = str(s["amc"]).strip().upper()
    if s["amc"] not in ("I", "II", "III"):
        raise HydrologyError("amc must be I, II or III")
    pp = s["peak_position"]
    if isinstance(pp, str) and pp.strip().lower() in PEAK_POSITIONS:
        pp = PEAK_POSITIONS[pp.strip().lower()]
    try:
        pp = float(pp)
    except (TypeError, ValueError):
        raise HydrologyError("peak_position must be front, centre, back or a number 0-1")
    if not 0.0 < pp < 1.0:
        raise HydrologyError("peak_position must be between 0 and 1")
    s["peak_position"] = pp
    for k in ("area_mi2", "storm_depth_in"):
        if s[k] is None:
            raise HydrologyError("{} is required".format(k))
    if s["distribution"] == "user" and not s["dist_csv"]:
        raise HydrologyError("give the cumulative distribution CSV for distribution 'user'")
    if s["loss_method"] == "cn" and s["cn"] is None:
        raise HydrologyError("give the curve number (cn) for the curve number losses")
    return s


def _time_of_concentration(s, warnings):
    """tc (hr) from the settings, or None when it was not given."""
    m = s["tc_method"]
    if m == "user":
        return s["tc_hr"]
    if s["flow_length_ft"] is None or s["slope_pct"] is None:
        raise HydrologyError("give the flow length (ft) and slope (%) for the {} time of "
                             "concentration".format("SCS lag" if m == "scs_lag" else "Kirpich"))
    if m == "scs_lag":
        if s["cn"] is None:
            raise HydrologyError("the SCS lag equation needs the curve number (cn)")
        if s["area_mi2"] > SCS_LAG_AREA_LIMIT_MI2:
            warnings.append("The SCS lag equation is usually quoted for watersheds under about "
                            "2,000 acres ({:.1f} mi2); this basin is {:.1f} mi2. Prefer a tc "
                            "from travel times or a calibrated value.".format(
                                SCS_LAG_AREA_LIMIT_MI2, s["area_mi2"]))
        return tc_scs_lag_hr(s["flow_length_ft"], s["slope_pct"], s["cn"])
    warnings.append("Kirpich is an empirical formula from small basins; for a larger basin "
                    "prefer a tc from travel times or a calibrated value.")
    return tc_kirpich_hr(s["flow_length_ft"], s["slope_pct"])


def choose_time_step(tc_hr, duration_hr, clark_r_hr=None, dt_min=None):
    """Computation time step (s): tc / 5, no longer than the Clark R or 1/96 of the storm (15 min
    for 24 hr, so the burst of a Type II storm is resolved), kept within 1 to 60 minutes, then
    adjusted to divide the storm evenly."""
    d_s = duration_hr * 3600.0
    if dt_min:
        dt = dt_min * 60.0
    else:
        dt = tc_hr * 3600.0 / 5.0
        if clark_r_hr:
            dt = min(dt, clark_r_hr * 3600.0)
        dt = min(dt, d_s / 96.0)
        dt = min(max(dt, 60.0), 3600.0)
    n = max(1, int(math.ceil(d_s / dt - 1e-9)))
    return d_s / n, n


# ============================================================================================
# Result
# ============================================================================================
@dataclass
class HydrologyResult:
    t_s: np.ndarray                     # time from the start of the storm (s)
    q_cms: np.ndarray                   # inflow incl. base flow (m3/s)
    q_direct_cms: np.ndarray            # direct runoff only (m3/s)
    dt_s: float
    rain_in: np.ndarray                 # rainfall per step (in)
    excess_in: np.ndarray               # excess rainfall per step (in)
    peak_cms: float
    peak_cfs: float
    time_to_peak_hr: float
    volume_acft: float                  # whole record, base flow included
    direct_volume_acft: float
    runoff_depth_in: float              # direct runoff over the basin
    runoff_coeff: float                 # runoff depth / storm depth
    tc_hr: Optional[float]
    lag_hr: Optional[float]
    volume_error: float                 # (hydrograph - excess x area) / (excess x area)
    warnings: List[str] = field(default_factory=list)
    summary: Dict[str, object] = field(default_factory=dict)
    markdown: str = ""
    settings: Dict[str, object] = field(default_factory=dict)

    @property
    def t_hr(self):
        return self.t_s / 3600.0

    @property
    def q_cfs(self):
        return self.q_cms / CFS


def _r(v, n):
    return None if v is None else round(float(v), n)


def _markdown(res, s, notes):
    sm = res.summary
    rows = [
        ("Drainage area", "{:g} mi2".format(s["area_mi2"])),
        ("Storm depth / duration", "{:g} in in {:g} hr (supplied by you)".format(
            s["storm_depth_in"], s["duration_hr"])),
        ("Temporal distribution", sm["distribution"]),
        ("Losses", sm["losses"]),
        ("Time of concentration", "n/a" if res.tc_hr is None else "{:.2f} hr".format(res.tc_hr)),
        ("Unit hydrograph", sm["unit_hydrograph"]),
        ("Base flow", "{:g} cfs".format(s["base_flow_cfs"])),
        ("Time step", "{:.1f} min".format(res.dt_s / 60.0)),
        ("**Peak inflow**", "**{:,.0f} cfs** at {:.2f} hr".format(res.peak_cfs,
                                                                  res.time_to_peak_hr)),
        ("Runoff depth / coefficient", "{:.2f} in / {:.2f}".format(res.runoff_depth_in,
                                                                 res.runoff_coeff)),
        ("Inflow volume", "{:,.0f} ac-ft ({:,.0f} ac-ft direct runoff)".format(
            res.volume_acft, res.direct_volume_acft)),
        ("Record length", "{:.1f} hr".format(res.t_s[-1] / 3600.0)),
        ("Volume check", "{:+.3f} % (limit {:.0f} %)".format(100.0 * res.volume_error,
                                                            100.0 * VOLUME_TOLERANCE)),
    ]
    lines = ["## Rainfall to inflow (PMF) hydrograph", "", "| Item | Value |", "|---|---|"]
    lines += ["| {} | {} |".format(k, v) for k, v in rows]
    lines += ["", "### Methods"] + ["- " + n for n in notes]
    if res.warnings:
        lines += ["", "### Check"] + ["- " + w for w in res.warnings]
    return "\n".join(lines) + "\n"


# ============================================================================================
# The generator
# ============================================================================================
def hydrograph_from_settings(settings):
    """Build the inflow hydrograph from a settings dict (see the module docstring)."""
    s = _normalise(settings)
    warnings = []           # type: List[str]
    notes = []              # type: List[str]
    area, depth, dur_hr = s["area_mi2"], s["storm_depth_in"], s["duration_hr"]
    uh = s["uh_method"]

    # -- time of concentration, lag, Clark parameters, time step --------------------------------
    tc = _time_of_concentration(s, warnings)
    lag_hr = None
    clark_tc = clark_r = None
    if uh == "scs":
        if tc is None:
            raise HydrologyError("give the time of concentration (tc_hr, or a flow length and "
                                 "slope) for the SCS unit hydrograph, or use uh_method 'lag'")
        lag_hr = SCS_LAG_TC_RATIO * tc
    elif uh == "lag":
        if s["lag_hr"] is None:
            raise HydrologyError("give the basin lag (lag_hr) for uh_method 'lag'")
        lag_hr = s["lag_hr"]
        if tc is None:
            tc = lag_hr / SCS_LAG_TC_RATIO
    else:
        clark_r = s["clark_r_hr"]
        if clark_r is None:
            raise HydrologyError("give the Clark storage coefficient R (clark_r_hr)")
        clark_tc = s["clark_tc_hr"] if s["clark_tc_hr"] is not None else tc
        if clark_tc is None:
            raise HydrologyError("give the Clark Tc (clark_tc_hr) or a time of concentration")
        if tc is None:
            tc = clark_tc
    dt_s, n = choose_time_step(tc if uh != "clark" else clark_tc, dur_hr, clark_r, s["dt_min"])
    if n > MAX_STEPS:
        raise HydrologyError("the storm would need {:,} time steps; give a larger dt_min".format(n))
    if s["dt_min"] and uh == "clark" and dt_s > 2.0 * clark_r * 3600.0:
        warnings.append("The time step is more than twice the Clark R; the reservoir routing is "
                        "limited and the hydrograph will be too peaky. Use a smaller step.")
    if dur_hr < tc:
        warnings.append(
            "The storm duration ({:g} hr) is shorter than the time of concentration ({:.2f} hr): "
            "the whole basin never contributes at once. Check that this duration is the critical "
            "one for the basin (a longer-duration storm may give the larger peak).".format(
                dur_hr, tc))
    ref_lag_s = (lag_hr * 3600.0) if lag_hr else (clark_tc * 3600.0 / 3.0)
    if ref_lag_s < 3.0 * dt_s:
        warnings.append("The {:.1f} min time step is coarse for a {:.1f} min lag/response time; "
                        "the peak may be poorly resolved. Use a smaller dt_min.".format(
                            dt_s / 60.0, ref_lag_s / 60.0))

    # -- rainfall ---------------------------------------------------------------------------------
    x = np.arange(n + 1) / float(n)
    dist = s["distribution"]
    if dist == "uniform":
        frac = uniform_fraction(x)
        dist_txt = "uniform"
    elif dist == "scs_type2":
        frac = scs_type2_fraction(x)
        dist_txt = "SCS Type II"
        if abs(dur_hr - 24.0) > 1e-6:
            warnings.append("The SCS Type II distribution is defined for a 24-hr storm; it is "
                            "stretched to {:g} hr. Use your PMP study's own temporal "
                            "distribution for other durations if you have one.".format(dur_hr))
    elif dist == "triangular":
        frac = triangular_fraction(x, s["peak_position"])
        dist_txt = "triangular, peak at {:.0%} of the storm".format(s["peak_position"])
    else:
        ux, uf, last_t = read_cumulative_csv(s["dist_csv"])
        frac = np.interp(x, ux, uf)
        dist_txt = "user curve ({})".format(s["dist_csv"].replace("\\", "/").split("/")[-1])
        if abs(last_t - dur_hr) > 0.01 * dur_hr:
            warnings.append("The distribution CSV spans {:g} hr but the storm duration is {:g} "
                            "hr; the curve is rescaled to the duration.".format(last_t, dur_hr))
    rain = depth * np.diff(frac)
    rain = np.maximum(rain, 0.0)
    rain *= depth / float(rain.sum())          # exact depth despite rounding

    # -- losses --------------------------------------------------------------------------------------
    excess, cn_eff = _excess(rain, dt_s, s, warnings)
    if s["loss_method"] == "cn":
        loss_txt = "SCS curve number {:g}{}, Ia = {:g} S".format(
            s["cn"], "" if s["amc"] == "II" else " (AMC {} -> {:.1f})".format(s["amc"], cn_eff),
            s["ia_ratio"])
        notes.append("Losses: SCS curve number, cumulative Q = (P - Ia)^2 / (P - Ia + S), "
                     "S = 1000/CN - 10, Ia = {:g} S; incremental excess is the change in Q.".format(
                         s["ia_ratio"]))
        if s["amc"] != "II":
            notes.append("Antecedent condition {}: CN converted from AMC II ({:g} -> {:.1f}).".format(
                s["amc"], s["cn"], cn_eff))
    elif s["loss_method"] == "constant":
        loss_txt = "initial loss {:g} in, then {:g} in/hr".format(s["initial_loss_in"],
                                                                 s["loss_rate_in_hr"])
        notes.append("Losses: constant initial loss, then a constant loss rate while it rains.")
    else:
        loss_txt = "none (all rain runs off)"
        notes.append("Losses: none; all rainfall becomes runoff.")
    notes.insert(0, "Rainfall: {} temporal distribution; storm depth and duration are inputs, "
                    "no PMP value is generated.".format(dist_txt))

    # -- unit hydrograph ---------------------------------------------------------------------------
    if uh == "clark":
        _, u = unit_hydrograph(area, dt_s, "clark", clark_tc_hr=clark_tc, clark_r_hr=clark_r)
        uh_txt = "Clark (Tc {:.2f} hr, R {:.2f} hr)".format(clark_tc, clark_r)
        notes.append("Unit hydrograph: Clark, time-area curve A(t)/A = sqrt(2) (t/Tc)^1.5 for "
                     "t/Tc <= 0.5, 1 - sqrt(2) (1 - t/Tc)^1.5 above, routed through a linear "
                     "reservoir with storage coefficient R.")
    else:
        prf = s["prf"]
        _, u = unit_hydrograph(area, dt_s, uh, lag_hr=lag_hr, prf=prf)
        uh_txt = "SCS dimensionless, peak rate factor {:g}, lag {:.2f} hr".format(prf, lag_hr)
        notes.append("Unit hydrograph: SCS dimensionless (gamma shape fitted to the peak rate "
                     "factor {:g}; 484 is the standard curvilinear hydrograph), tp = dt/2 + lag, "
                     "lag = {}.".format(prf, "0.6 tc" if uh == "scs" else "your value"))
        if not 300.0 <= prf <= 600.0:
            warnings.append("Peak rate factor {:g} is outside the 300-600 usually applied; "
                            "check it.".format(prf))
    if tc is not None and s["tc_method"] == "scs_lag":
        notes.append("Time of concentration: SCS lag equation, lag = l^0.8 (S + 1)^0.7 / "
                     "(1900 Y^0.5) hr (l ft, Y % slope, S = 1000/CN - 10), tc = lag / 0.6.")
    elif s["tc_method"] == "kirpich":
        notes.append("Time of concentration: Kirpich, tc = 0.0078 L^0.77 S^-0.385 min "
                     "(L ft, S ft/ft).")
    elif s["tc_method"] == "user":
        notes.append("Time of concentration: your value.")

    # -- convolution ---------------------------------------------------------------------------------
    q_direct = np.convolve(excess, u)
    q_direct = np.concatenate((q_direct, [0.0]))                   # ends at zero
    t = np.arange(len(q_direct)) * dt_s
    q = q_direct + s["base_flow_cfs"] * CFS
    if s["base_flow_cfs"] > 0:
        warnings.append("The record ends at {:.1f} hr with the base flow ({:g} cfs); the flood "
                        "scenario treats inflow after the last row as zero.".format(
                            t[-1] / 3600.0, s["base_flow_cfs"]))

    # -- checks and numbers -----------------------------------------------------------------------
    area_m2 = area * MI2_M2
    v_direct = _trapz(q_direct, t)
    v_expected = float(excess.sum()) * IN_M * area_m2
    err = (v_direct - v_expected) / v_expected if v_expected > 0 else 0.0
    if abs(err) > VOLUME_TOLERANCE:
        raise HydrologyError(
            "the hydrograph volume differs from the excess rainfall volume by {:+.1f} % "
            "(limit {:.0f} %); use a smaller time step".format(100.0 * err,
                                                              100.0 * VOLUME_TOLERANCE))
    runoff_in = float(excess.sum())
    if v_expected <= 0:
        warnings.append("No runoff: the inflow hydrograph is only the base flow.")
    i_pk = int(np.argmax(q))
    v_total = _trapz(q, t)
    res = HydrologyResult(
        t_s=t, q_cms=q, q_direct_cms=q_direct, dt_s=dt_s, rain_in=rain, excess_in=excess,
        peak_cms=float(q[i_pk]), peak_cfs=float(q[i_pk]) / CFS,
        time_to_peak_hr=float(t[i_pk]) / 3600.0, volume_acft=v_total / ACFT,
        direct_volume_acft=v_direct / ACFT, runoff_depth_in=runoff_in,
        runoff_coeff=runoff_in / depth, tc_hr=tc, lag_hr=lag_hr, volume_error=err,
        warnings=warnings, settings=s)
    res.summary = {
        "area_mi2": _r(area, 4), "storm_depth_in": _r(depth, 3), "duration_hr": _r(dur_hr, 3),
        "distribution": dist_txt, "losses": loss_txt, "unit_hydrograph": uh_txt,
        "cn_effective": _r(cn_eff, 2), "tc_hr": _r(tc, 3), "lag_hr": _r(lag_hr, 3),
        "dt_min": _r(dt_s / 60.0, 3), "base_flow_cfs": _r(s["base_flow_cfs"], 3),
        "peak_cfs": _r(res.peak_cfs, 1), "time_to_peak_hr": _r(res.time_to_peak_hr, 3),
        "volume_acft": _r(res.volume_acft, 1),
        "direct_volume_acft": _r(res.direct_volume_acft, 1),
        "runoff_depth_in": _r(runoff_in, 3), "runoff_coeff": _r(res.runoff_coeff, 3),
        "record_hr": _r(t[-1] / 3600.0, 2), "volume_error_pct": _r(100.0 * err, 4),
        "n_warnings": len(warnings),
    }
    res.markdown = _markdown(res, s, notes)
    return res


def write_csv(result, path):
    """Write the inflow hydrograph in the format the flood scenario reads
    (`FloodInputs.from_files`): time_hr,discharge_cfs, time from the start of the storm."""
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("time_hr,discharge_cfs\n")
        for t, q in zip(result.t_s, result.q_cms):
            f.write("{:.6f},{:.4f}\n".format(t / 3600.0, q / CFS))
    return path
