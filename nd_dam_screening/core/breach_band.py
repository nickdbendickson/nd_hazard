"""Does the class depend on the breach parameters? (0.28.0) One row per dam, three breach sizes.

A screening run picks ONE breach width and ONE formation time - Froehlich (2008) unless the
engineer says otherwise - and reports one class. The breach equations that ND 5.3 accepts
disagree with each other by a factor of two on the width and more than that on the time, and for
a dam near a class boundary that disagreement IS the answer: the honest result is not "Medium",
it is "Medium, but High with a wider breach". This module computes that row without a second
flood model:

    bands = params(vw_m3, hb_m, mode="piping")      # low / mid / high width and time
    bands = peaks(bands, curve_e, curve_v, start, invert)   # routed peak for each (bh.route_breach)
    rows  = classes(bands, classify)                # the class under each, caller-supplied
    v     = verdict(rows)                           # "class depends on breach parameters" or not

LOW / MID / HIGH are the spread of the equations, not an invented percentage: low is the
narrowest width with the longest formation time among the equations (the smallest peak outflow),
high is the widest with the shortest time (the largest), mid is the equation the run used. All
the hydraulics are core/breach_hydro.py - the same weir routing the real step 5 does, so a band's
peak is comparable with the dam's own.

THE CLASS UNDER A BAND. There is no second 2D model: the flood the dam already has is re-read at
the structures with its depth and velocity scaled by the change in peak outflow. Manning normal
depth in a channel gives d ~ Q^0.6 and v ~ Q^0.4 (so depth x velocity ~ Q, which is what the
ACER TM 11 charts read), and `scale_factors` is exactly that. It is a screening approximation of
a re-run and it is stated as one: a dam this row marks as uncertain is a dam to model properly,
not a dam to re-classify from here. The dam's own class is NEVER changed by this module - it
only ever adds "the class depends on the breach parameters" - because a scaled depth is not a
modelled depth and the suite does not move a result on an approximation.

Pure numpy through core/breach_hydro; no QGIS.
"""
import numpy as np

from . import breach_hydro as bh

BANDS = ("low", "mid", "high")
BAND_TEXT = {"low": "narrowest breach, slowest to form",
             "mid": "the equation this run used",
             "high": "widest breach, fastest to form"}
# the width / time equations ND 5.3 practice accepts (core/breach_hydro.BREACH_METHODS); TR-60 is
# a peak-discharge method, not a width / time one, so it is not part of the spread
SPREAD_METHODS = ("F2008", "F1995", "VTG-E", "VTG-R", "MLM")
MID_METHOD = "F2008"
# Manning normal depth in a prismatic channel: d ~ Q^0.6, v ~ Q^0.4 (d x v ~ Q)
DEPTH_EXP, VEL_EXP = 0.6, 0.4
UNCERTAIN = "class depends on breach parameters"
STABLE = "class does not depend on the breach parameters"
APPROX = ("the band classes come from the modelled flood scaled by the change in peak outflow "
          "(d ~ Q^0.6, v ~ Q^0.4), not from a second flood model")
# the same caveat in a few words: hz_notes holds 254 characters for EVERY note a dam has, and a
# note that is cut off by a longer one is a note nobody reads (APPROX stays, for the help text)
SCALED = "scaled from the modelled flood, not re-modelled"


def equations(vw_m3, hb_m, mode="piping", methods=None):
    """{method: {"bavg_m", "tf_s", "z", "label"}} for every equation that can be evaluated for
    this dam (an equation that refuses the inputs is left out, not guessed)."""
    out = {}
    for key in (methods or SPREAD_METHODS):
        label, fn = bh.BREACH_METHODS.get(key, (key, None))
        if fn is None:                                   # TR-60: no width / time
            continue
        try:
            p = fn(vw_m3, hb_m, mode)
        except (ValueError, TypeError, ZeroDivisionError, OverflowError, FloatingPointError):
            continue                                     # this equation has nothing to say here
        if not p or not p.get("bavg_m") or not p.get("tf_s"):
            continue
        if not (np.isfinite(p["bavg_m"]) and np.isfinite(p["tf_s"])):
            continue
        out[key] = {"bavg_m": float(p["bavg_m"]), "tf_s": float(p["tf_s"]),
                    "z": float(p.get("z") or 0.7), "label": label}
    return out


def params(vw_m3, hb_m, mode="piping", methods=None, mid=MID_METHOD):
    """The low / mid / high breach of one dam.

    {band: {"bavg_m", "bavg_ft", "tf_s", "tf_min", "z", "from", "why"}} - {} when no equation
    could be evaluated (no volume, no height). Low is the smallest outflow (narrow and slow),
    high the largest (wide and fast); mid is the equation the run used, or the middle width."""
    eqs = equations(vw_m3, hb_m, mode, methods)
    if not eqs:
        return {}
    widths = sorted(eqs.items(), key=lambda kv: kv[1]["bavg_m"])
    times = sorted(eqs.items(), key=lambda kv: kv[1]["tf_s"])
    mid_key = mid if mid in eqs else widths[len(widths) // 2][0]
    picks = {"low": (widths[0][0], times[-1][0]),          # narrowest width, longest time
             "mid": (mid_key, mid_key),
             "high": (widths[-1][0], times[0][0])}         # widest width, shortest time
    out = {}
    for band, (wkey, tkey) in picks.items():
        bavg, tf = eqs[wkey]["bavg_m"], eqs[tkey]["tf_s"]
        frm = eqs[wkey]["label"] if wkey == tkey else "{} width, {} time".format(
            eqs[wkey]["label"], eqs[tkey]["label"])
        out[band] = {"bavg_m": bavg, "bavg_ft": bavg * bh.M_FT, "tf_s": tf, "tf_min": tf / 60.0,
                     "z": eqs[wkey]["z"], "from": frm, "why": BAND_TEXT[band]}
    return out


def peaks(bands, curve_e, curve_v, start_elev, invert_m):
    """Add "peak_m3s" and "peak_cfs" to each band by routing its breach through the dam's
    stage-storage curve (core/breach_hydro.route_breach - the same routing step 5 uses). Bands
    that cannot be routed keep peak_m3s None. The input is not changed."""
    out = {}
    e, v = bh.prep_curve(curve_e, curve_v)
    for band, p in (bands or {}).items():
        row = dict(p)
        row["peak_m3s"] = row["peak_cfs"] = None
        try:
            r = bh.route_breach(e, v, float(start_elev), float(invert_m), row["bavg_m"],
                                row["tf_s"], row["z"])
            if r and np.isfinite(r["peak_m3s"]) and r["peak_m3s"] > 0:
                row["peak_m3s"] = float(r["peak_m3s"])
                row["peak_cfs"] = float(r["peak_m3s"]) / bh.CFS_M3S
        except (ValueError, TypeError, ZeroDivisionError, IndexError, FloatingPointError):
            pass                                            # no peak for this band; said as None
        out[band] = row
    return out


def scale_factors(q_band, q_mid):
    """(depth factor, velocity factor) for a band's peak against the modelled one.

    Manning normal depth: d ~ Q^0.6 and v ~ Q^0.4. (1.0, 1.0) when either peak is missing or
    not positive - an unknown change is not a change."""
    try:
        qb, qm = float(q_band), float(q_mid)
    except (TypeError, ValueError):
        return 1.0, 1.0
    if not (qb > 0 and qm > 0) or not (np.isfinite(qb) and np.isfinite(qm)):
        return 1.0, 1.0
    r = qb / qm
    return float(r ** DEPTH_EXP), float(r ** VEL_EXP)


def classes(bands, classify, mid_peak=None):
    """The class under each band.

    `classify(depth_factor, velocity_factor)` -> a class string (or "" when it cannot be worked
    out); it is the caller's, so this module needs no flood raster and no QGIS. Returns
    {band: {... , "depth_factor", "vel_factor", "hazard_class"}}."""
    bands = bands or {}
    qm = mid_peak if mid_peak is not None else (bands.get("mid") or {}).get("peak_m3s")
    out = {}
    for band in BANDS:
        if band not in bands:
            continue
        row = dict(bands[band])
        df, vf = scale_factors(row.get("peak_m3s"), qm)
        row["depth_factor"], row["vel_factor"] = df, vf
        try:
            row["hazard_class"] = str(classify(df, vf) or "")
        except Exception:                                   # noqa - a class is never worth a crash
            row["hazard_class"] = ""
        out[band] = row
    return out


def verdict(rows):
    """{"stable", "class_min", "class_max", "text"} over the bands' classes.

    stable is None when fewer than two bands produced a class (nothing to compare)."""
    from . import scenarios as sc                            # the class ranking, in one place
    got = [(b, str((rows or {}).get(b, {}).get("hazard_class") or "")) for b in BANDS]
    ranked = [(b, c, sc.rank(c)) for b, c in got if sc.rank(c) is not None]
    out = {"stable": None, "class_min": "", "class_max": "", "text": ""}
    if not ranked:
        out["text"] = "no class could be worked out for the breach parameter bands"
        return out
    lo = min(ranked, key=lambda r: r[2])
    hi = max(ranked, key=lambda r: r[2])
    out["class_min"], out["class_max"] = lo[1], hi[1]
    if len(ranked) < 2:
        out["text"] = "only the {} breach gave a class".format(ranked[0][0])
        return out
    out["stable"] = lo[2] == hi[2]
    if out["stable"]:
        out["text"] = "{} ({} to {} breach)".format(STABLE, BANDS[0], BANDS[-1])
    else:
        out["text"] = "{}: {} with the {} breach, {} with the {} breach".format(
            UNCERTAIN, lo[1], lo[0], hi[1], hi[0])
    return out


def detail_text(rows):
    """One field's worth of the whole row: "low 42 ft / 71 min -> Medium; mid ...; high ...". """
    parts = []
    for band in BANDS:
        r = (rows or {}).get(band)
        if not r:
            continue
        bit = "{} {:.0f} ft / {:.0f} min".format(band, r.get("bavg_ft") or 0.0,
                                                 r.get("tf_min") or 0.0)
        if r.get("peak_cfs"):
            bit += " / {:,.0f} cfs".format(r["peak_cfs"])
        if r.get("hazard_class"):
            bit += " -> " + r["hazard_class"]
        parts.append(bit)
    return "; ".join(parts)
