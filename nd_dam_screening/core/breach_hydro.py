"""Breach parameters and outflow hydrograph for one dam.  Pure numpy, SI units.

Follows the ND Dam Safety Standards policy (Sec. 5.1.1): start at the top of
dam, no added inflow, no coincident downstream flooding, piping-type breach.
An optional "sunny day" run starts at normal pool.

Method
  * Breach width and formation time : Froehlich (2008) by default; Froehlich
    (1995), Von Thun & Gillette (1990), MacDonald & Langridge-Monopolis (1984)
    and NRCS TR-60 (peak discharge with a triangular or curvilinear hydrograph
    instead of a routed breach) are available, as in the ND Dam Breach Runner
        Bavg = 0.27 * Ko * Vw^0.32 * hb^0.04        (m)   Ko = 1.0 piping, 1.3 overtopping
        tf   = 63.2 * sqrt(Vw / (g * hb^2))         (s)
        side slope z = 0.7 (piping), 1.0 (overtopping)
  * User parameters : `user` = {"bavg_m", "tf_s", "z", "invert_m"} replaces the
    equation's values where given (resolve_params records which source gave
    each value; the QGIS tool reads them from fields of the dams layer or from
    one set of typed values).
  * Outflow : level-pool routing through a trapezoidal breach that grows
    linearly in depth and bottom width over tf, using weir flow
        Q = 1.71 * Bb * h^1.5 + 1.35 * z * h^2.5    (SI form of 3.1 / 2.45 in US units)
    with no tailwater submergence (conservative for screening).
    The breach geometry equations match the Runner. The routed peak does not:
    the Runner routes the same weir through its storage curve, including
    tailwater, and that peak is the one a single-dam run should use. Pass
    `hydrograph=(t_seconds, q_m3s)` to use that series as the outflow. The
    breach geometry is still computed here.
  * Cross-checks : Froehlich (1995) peak, NRCS TR-60 peak (65 * Hw^1.85, Hw > 10.5 ft).

Screening-level limits: piping is treated as a weir breach that erodes from the
top down (no orifice/roof-collapse phase); vertical breach growth is linear.
"""
import math

import numpy as np

_FT, _CFS, _AF = 0.3048, 0.028316846592, 1233.48183754752     # m per ft, m3/s per cfs, m3 per ac-ft

G = 9.81
M_FT = 3.280839895
CFS_M3S = 0.028316846592
AF_M3 = 1233.4818375
KO = {"piping": 1.0, "overtopping": 1.3}
SIDE_Z = {"piping": 0.7, "overtopping": 1.0}


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


# ------------------------------------------------------------ parameters
def froehlich_2008(vw_m3, hb_m, mode="piping"):
    vw_m3, hb_m = _positive("reservoir volume Vw", vw_m3), _positive("breach height hb", hb_m)
    ko, z = KO[mode], SIDE_Z[mode]
    bavg = 0.27 * ko * vw_m3 ** 0.32 * hb_m ** 0.04
    tf = 63.2 * np.sqrt(vw_m3 / (G * hb_m ** 2))
    return {"bavg_m": float(bavg), "tf_s": float(tf), "z": z, "ko": ko}


def froehlich_1995(vw_m3, hb_m, mode="piping"):
    """Froehlich (1995a): Bavg = 0.1803 Ko Vw^0.32 hb^0.19 (m); tf = 0.00254 Vw^0.53 hb^-0.9 (h)."""
    vw_m3, hb_m = _positive("reservoir volume Vw", vw_m3), _positive("breach height hb", hb_m)
    ko = 1.4 if mode == "overtopping" else 1.0
    z = 1.4 if mode == "overtopping" else 0.9
    bavg = 0.1803 * ko * vw_m3 ** 0.32 * hb_m ** 0.19
    tf = 0.00254 * vw_m3 ** 0.53 * hb_m ** -0.9 * 3600.0
    return {"bavg_m": float(bavg), "tf_s": float(tf), "z": z, "ko": ko}


# Cb by storage, as the Runner (core/breach.py vtg_1990): < 1,000 / 5,000 / 10,000 acre-ft
VTG_CB = [(1000.0 * AF_M3, 6.1), (5000.0 * AF_M3, 18.3), (10000.0 * AF_M3, 42.7), (float("inf"), 54.9)]


def von_thun_gillette(vw_m3, hw_m, erodible=True, z=0.5):
    """Von Thun & Gillette (1990): Bavg = 2.5 hw + Cb (m).

    tf = 0.015 hw (h) easily erodible, 0.020 hw + 0.25 (h) erosion resistant.
    Side slope: the method gives 1H:1V for non-cohesive and 0.33-0.5H:1V for
    cohesive embankments; 0.5 is used because ND embankments are mostly
    compacted clay/till (change `z` if yours are not).
    """
    vw_m3, hw_m = _positive("reservoir volume Vw", vw_m3), _positive("water height hw", hw_m)
    cb = next(c for lim, c in VTG_CB if vw_m3 < lim)
    bavg = 2.5 * hw_m + cb
    tf_h = 0.015 * hw_m if erodible else 0.020 * hw_m + 0.25
    return {"bavg_m": float(bavg), "tf_s": float(tf_h * 3600.0), "z": z, "ko": None}


# --- copied from the ND Dam Breach Runner (nd_breach_runner/core/breach.py: mlm_1984,
# --- embankment_area_ft2, tr60_peak_cfs, curvilinear_shape). Both plugins must give the same
# --- numbers; tests/test_tools_breach.py compares them when the Runner is on the machine.
# --- Screening has no crest width or face slopes in its inventory, so the Runner's own
# --- fallback embankment (12 ft crest, 3H:1V faces) is used here for every dam (noted).
MLM_CREST_FT, MLM_SLOPE_H = 12.0, 3.0


def mlm_1984(vw_m3, hw_m, embankment_area_m2):
    """MacDonald & Langridge-Monopolis (1984), earthfill: eroded volume
    Ver = 0.0261 (Vw hw)^0.769 (m3), formation time tf = 0.0179 Ver^0.364 (hr).
    The average width is the eroded volume divided by the embankment
    cross-section area at the breach; side slopes taken as 1H:1V.
    Returns (bavg_m, tf_s, z) exactly as the Runner's function."""
    vw_m3, hw_m = _positive("reservoir volume Vw", vw_m3), _positive("water height hw", hw_m)
    embankment_area_m2 = _positive("embankment cross-section area", embankment_area_m2)
    ver = 0.0261 * (vw_m3 * hw_m) ** 0.769
    tf = 0.0179 * ver ** 0.364 * 3600.0
    return ver / embankment_area_m2, tf, 1.0


def embankment_area_ft2(height_ft, crest_ft, us_h, ds_h):
    """Cross-section area of the embankment at the breach (ft2), trapezoid."""
    return height_ft * (crest_ft + 0.5 * (us_h + ds_h) * height_ft)


def macdonald(vw_m3, hb_m, mode="piping"):
    """MLM 1984 in the BREACH_METHODS form, with the assumed embankment section."""
    vw_m3, hb_m = _positive("reservoir volume Vw", vw_m3), _positive("breach height hb", hb_m)
    a_ft2 = embankment_area_ft2(hb_m * M_FT, MLM_CREST_FT, MLM_SLOPE_H, MLM_SLOPE_H)
    b, tf, z = mlm_1984(vw_m3, hb_m, a_ft2 / (M_FT * M_FT))
    return {"bavg_m": float(b), "tf_s": float(tf), "z": z, "ko": None,
            "note": f"MacDonald & Langridge-Monopolis: embankment section assumed "
                    f"{MLM_CREST_FT:g} ft crest, {MLM_SLOPE_H:g}H:1V faces (the inventory has "
                    f"neither); side slope 1H:1V"}


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


def curvilinear_shape(qp, tr, volume, n=200):
    """Curvilinear breach hydrograph: a smooth (sin^2) rise to the peak at tr
    and an exponential recession whose time constant makes the volume equal
    `volume`. Returns None when the volume is too small for that shape."""
    qp, tr = _positive("peak discharge", qp), _positive("time to peak", tr)
    volume = _positive("hydrograph volume", volume, allow_zero=True)
    import math
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
# --- end of the copies from the Runner


def tr60_hydrograph(vw_m3, v_dead_m3, hb_m, hw_m, shape="triangular"):
    """TR-60 peak (assumed embankment section, as the Runner without geometry) with a
    hydrograph whose volume is the releasable storage vw - v_dead; rise time = the
    Froehlich (2008) piping formation time, as the Runner. Returns (t, q, info)."""
    a_ft2 = embankment_area_ft2(hb_m * M_FT, MLM_CREST_FT, MLM_SLOPE_H, MLM_SLOPE_H)
    q_cfs, rule, br = tr60_peak_cfs(vw_m3 / AF_M3, hw_m * M_FT, a_ft2)
    qp = q_cfs * CFS_M3S
    tr = froehlich_2008(vw_m3, hb_m, "piping")["tf_s"]
    v_rel = max(vw_m3 - v_dead_m3, 0.0)
    tb = 2.0 * v_rel / qp
    notes = [f"NRCS TR-60 peak {q_cfs:,.0f} cfs by {rule}; embankment section assumed "
             f"{MLM_CREST_FT:g} ft crest, {MLM_SLOPE_H:g}H:1V faces; volume = releasable storage; "
             f"rise time = Froehlich (2008) formation time (TR-60 gives the peak only)"]
    if tr >= tb:
        tr = 0.25 * tb
        notes.append("rise time longer than the triangle base allowed by the volume; set to a "
                     "quarter of the base")
    curve = curvilinear_shape(qp, tr, v_rel) if shape == "curvilinear" else None
    if shape == "curvilinear" and curve is None:
        notes.append("releasable volume too small for a curvilinear shape; triangle used")
    if curve is not None:
        t, q = curve
    else:
        t, q = np.array([0.0, tr, tb]), np.array([0.0, qp, 0.0])
    return t, q, {"qp_m3s": float(qp), "tr_s": float(tr), "rule": rule, "br": br,
                  "shape": "curvilinear" if curve is not None else "triangular",
                  "released_m3": float(v_rel), "notes": notes}


BREACH_METHODS = {
    "F2008": ("Froehlich (2008)", lambda vw, hb, mode: froehlich_2008(vw, hb, mode)),
    "F1995": ("Froehlich (1995)", lambda vw, hb, mode: froehlich_1995(vw, hb, mode)),
    "VTG-E": ("Von Thun & Gillette (1990), easily erodible",
              lambda vw, hb, mode: von_thun_gillette(vw, hb, True)),
    "VTG-R": ("Von Thun & Gillette (1990), erosion resistant",
              lambda vw, hb, mode: von_thun_gillette(vw, hb, False)),
    "MLM": ("MacDonald & Langridge-Monopolis (1984), earthfill",
            lambda vw, hb, mode: macdonald(vw, hb, mode)),
    # TR-60: not a width / time equation; analyze_dam builds the hydrograph from the peak
    "TR60": ("NRCS TR-60 peak discharge, triangular hydrograph", None),
    "TR60-C": ("NRCS TR-60 peak discharge, curvilinear hydrograph", None),
}
TR60_METHODS = ("TR60", "TR60-C")
# which parameters the equation (or the user) can give
USER_KEYS = ("bavg_m", "tf_s", "z", "invert_m")


def resolve_params(eq, user, method="F2008"):
    """Merge the equation's parameters with the user's. `eq` = {"bavg_m", "tf_s", "z"} (None
    for a TR-60 method); `user` = {key: (value, source_label)} or {key: value} (None = not
    given). Returns (params, sources) where sources = {key: "equation <method>" | label}.
    `invert_m` only comes from the user (None when not given)."""
    p, src = {}, {}
    user = user or {}
    for k in USER_KEYS:
        u = user.get(k)
        label = "user"
        if isinstance(u, tuple):
            u, label = u
        if u is not None and (k == "invert_m" or float(u) > 0):
            p[k] = float(u); src[k] = label
        elif k != "invert_m" and eq is not None and eq.get(k) is not None:
            p[k] = float(eq[k]); src[k] = f"equation {method}"
        else:
            p[k] = None
    return p, src


def source_summary(sources, method):
    """One short text for the brch_src field: 'equation F2008' when nothing came from the
    user, else e.g. 'fields: width, time; equation F2008: slope'."""
    names = {"bavg_m": "width", "tf_s": "time", "z": "slope", "invert_m": "bottom"}
    by = {}
    for k, s in sources.items():
        by.setdefault(s, []).append(names[k])
    if list(by) == [f"equation {method}"] or not by:
        return f"equation {method}"
    return "; ".join(f"{s}: {', '.join(v)}" for s, v in by.items())


def froehlich_1995_peak(vw_m3, hw_m):
    return float(0.607 * vw_m3 ** 0.295 * hw_m ** 1.24)


def tr60_peak(hw_m):
    """NRCS TR-60 upper-limit peak 65 Hw^1.85 (the cross-check field qp_tr60; kept as before);
    only stated for Hw > 10.5 ft. The TR60 methods use tr60_peak_cfs with its limits."""
    hw_ft = hw_m * M_FT
    if hw_ft <= 10.5:
        return None
    return float(65.0 * hw_ft ** 1.85 * CFS_M3S)


# ----------------------------------------------------------------- curve
def prep_curve(elev, vol):
    e = np.asarray(elev, float); v = np.asarray(vol, float)
    o = np.argsort(e); e, v = e[o], v[o]
    v = np.maximum.accumulate(v)
    keep = np.concatenate(([True], np.diff(v) > 1e-9))
    return e[keep], v[keep]


def extend_below_pool(e, v, bed_elev, exponent=2.0, n=15):
    """Add storage below normal pool: V(z) = V_pool * ((z-bed)/(pool-bed))^p."""
    pool = e[0]
    if bed_elev >= pool - 0.05:
        return e, v
    z = np.linspace(bed_elev, pool, n)[:-1]
    vv = v[0] * ((z - bed_elev) / (pool - bed_elev)) ** exponent
    return np.concatenate((z, e)), np.concatenate((vv, v))


# --------------------------------------------------------------- routing
def route_breach(e, v, start_elev, invert, bavg, tf_s, z):
    """Level-pool routing; returns dict with time series and summary."""
    hb = start_elev - invert
    bb_final = max(bavg - z * hb, 0.1 * bavg)
    v0 = float(np.interp(start_elev, e, v))
    dt = float(max(1.0, min(tf_s / 200.0, 20.0)))

    def q_at(t, vol):
        f = min(t / tf_s, 1.0)
        zb = start_elev - f * hb
        pool = float(np.interp(vol, v, e))
        h = pool - zb
        if h <= 1e-3:
            return 0.0, pool
        q = 1.71 * f * bb_final * h ** 1.5 + 1.35 * z * h ** 2.5
        avail = max(vol - float(np.interp(zb, e, v)), 0.0)
        return min(q, avail / dt), pool

    t, vol, released = 0.0, v0, 0.0
    ts, qs, pools = [0.0], [0.0], [start_elev]
    peak, t_peak = 0.0, 0.0
    tmax = max(24 * 3600.0, 10 * tf_s)
    while t < tmax:
        q1, _ = q_at(t, vol)
        q2, _ = q_at(t + dt, max(vol - q1 * dt, 0.0))
        qm = 0.5 * (q1 + q2)
        vol -= qm * dt
        released += qm * dt
        t += dt
        pool = float(np.interp(vol, v, e))
        ts.append(t); qs.append(q2); pools.append(pool)
        if q2 > peak:
            peak, t_peak = q2, t
        if t >= tf_s and (q2 < 0.01 * peak or q2 < 1e-3):
            break
    # end the hydrograph at zero: the solver holds the last value after the final
    # time, so a non-zero tail would keep injecting water for the whole simulation
    ts.append(t + dt); qs.append(0.0); pools.append(pools[-1])
    return {"t": np.array(ts), "q": np.array(qs), "pool": np.array(pools),
            "peak_m3s": float(peak), "t_peak_s": float(t_peak),
            "released_m3": float(released), "v0_m3": v0, "v_end_m3": float(vol),
            "duration_s": float(t), "bb_final_m": float(bb_final)}


# --------------------------------------------------------------- one dam
def analyze_dam(curve_e, curve_v, pool, crest, base, mode="piping", crest_len=None,
                scale=1.0, bed_elev=None, below_exp=2.0, sunny=False, method="F2008",
                user=None, hydrograph=None):
    """Run the top-of-dam scenario (and optionally sunny day) for one dam.

    curve_e/v : stage-storage curve from pool to crest (m, m3)
    base      : ground elevation at the downstream toe = breach invert (m)
    scale     : multiplies storage (used to honour inventory storage)
    user      : {"bavg_m", "tf_s", "z", "invert_m"} values (or (value, source) tuples) that
                replace the equation's where given; see resolve_params. A user invert
                above `base` raises the breach bottom (dead storage stays behind the dam).
    Each scenario dict carries "sources" ({parameter: where it came from}) and "src_text".
    """
    notes = []
    e, v = prep_curve(curve_e, curve_v)
    v = v * scale
    if bed_elev is None:
        bed_elev = base
    bed_elev = min(bed_elev, pool - 0.1)
    e, v = extend_below_pool(e, v, bed_elev, below_exp)
    if crest > e[-1] + 1e-6:
        notes.append("crest above end of storage curve; storage clamped")
    if method not in BREACH_METHODS:
        raise KeyError(f"unknown breach method {method!r}")
    user = user or {}
    inv_u = user.get("invert_m")
    inv_u = inv_u[0] if isinstance(inv_u, tuple) else inv_u
    invert = base
    if inv_u is not None:
        invert = float(inv_u)
        if invert < base - 1e-6:
            notes.append(f"user breach bottom {invert / _FT:.1f} ft is below the toe {base / _FT:.1f} ft; "
                         "the toe is used")
            invert = base
        elif invert > base + 1e-6:
            notes.append(f"breach bottom from user: {invert / _FT:.1f} ft ({(invert - base) / _FT:.1f} ft above the toe)")

    out = {}
    scen = [("td", crest, "top of dam")]
    if sunny:
        scen.append(("sd", pool, "normal pool"))
    for key, start, label in scen:
        hb = start - invert
        if hb < 0.3:
            out[key] = {"skipped": f"breach height {hb:.2f} m too small"}
            continue
        vw = float(np.interp(start, e, v))
        v_dead = float(np.interp(invert, e, v))
        n2 = []
        if method in TR60_METHODS:
            eq = {"bavg_m": None, "tf_s": None, "z": None}
            p, sources = resolve_params(eq, user, method)
            t, q, info = tr60_hydrograph(vw, v_dead, hb, hb,
                                         "curvilinear" if method == "TR60-C" else "triangular")
            n2 += info["notes"]
            if p["tf_s"]:
                n2.append("TR-60 shape uses the Froehlich (2008) rise time; the user formation "
                          "time is recorded but not used")
            out[key] = {"label": label, "start_elev": start, "vw_m3": vw, "hb_m": hb,
                        "bavg_m": p["bavg_m"], "bb_m": None, "z": p["z"], "tf_s": p["tf_s"] or info["tr_s"],
                        "qp_route": info["qp_m3s"], "tp_s": info["tr_s"],
                        "qp_f95": froehlich_1995_peak(vw, hb), "qp_tr60": tr60_peak(hb),
                        "released_m3": info["released_m3"], "t": t, "q": q, "notes": n2,
                        "method": method, "sources": sources,
                        "src_text": source_summary(sources, method)}
            continue
        eq = BREACH_METHODS[method][1](vw, hb, mode)
        if eq.get("note"):
            n2.append(eq["note"])
        p, sources = resolve_params(eq, user, method)
        z, bavg, tf = p["z"], p["bavg_m"], p["tf_s"]
        if crest_len and bavg + z * hb > crest_len:
            bavg = max(crest_len - z * hb, 0.3 * crest_len)
            n2.append(f"breach width limited by crest length {crest_len / _FT:,.0f} ft")
        r = route_breach(e, v, start, invert, bavg, tf, z)
        t_h, q_h = r["t"], r["q"]
        peak, t_peak = r["peak_m3s"], r["t_peak_s"]
        out[key] = {"label": label, "start_elev": start, "vw_m3": vw, "hb_m": hb,
                    "bavg_m": bavg, "bb_m": r["bb_final_m"], "z": z, "tf_s": tf,
                    "qp_route": peak, "tp_s": t_peak,
                    "qp_f95": froehlich_1995_peak(vw, hb), "qp_tr60": tr60_peak(hb),
                    "released_m3": r["released_m3"], "t": t_h, "q": q_h, "notes": n2,
                    "method": method, "sources": sources,
                    "src_text": source_summary(sources, method)}
    if hydrograph is not None:
        td = out.get("td") or {}
        if "skipped" in td:
            notes.append("supplied hydrograph not used: the top-of-dam scenario was skipped")
        elif td:
            t_h, q_h, hn = supplied_hydrograph(hydrograph)
            vol = float(np.trapezoid(q_h, t_h)) if hasattr(np, "trapezoid") else float(np.trapz(q_h, t_h))
            hn.append(f"outflow hydrograph taken from the supplied series (peak {float(q_h.max()) / _CFS:,.0f} cfs, "
                      f"volume {vol / _AF:,.0f} ac-ft); breach geometry is still {method}; the routed "
                      f"{method} peak would be {td['qp_route'] / _CFS:,.0f} cfs")
            if vol > 1.05 * td["vw_m3"]:
                hn.append(f"supplied hydrograph volume {vol / _AF:,.0f} ac-ft is more than the storage at the "
                          f"top of dam {td['vw_m3'] / _AF:,.0f} ac-ft - check the units (the file is read as m3/s and seconds)")
            k = int(np.argmax(q_h))
            td.update({"t": t_h, "q": q_h, "qp_route": float(q_h[k]), "tp_s": float(t_h[k]),
                       "released_m3": vol, "hydro_source": "supplied"})
            td["notes"] = td["notes"] + hn
    out["notes"] = notes
    out["invert_m"] = invert
    return out


def supplied_hydrograph(hydrograph):
    """(t_s, q_m3s, notes) of a supplied outflow series, checked: two equal-length 1-D series of
    finite numbers, at least two points, time strictly increasing in seconds, discharge >= 0
    (m3/s) with a positive peak. A series that does not end at zero gets a zero appended one step
    later (the solver holds the last value, as route_breach). ValueError otherwise."""
    try:
        t = np.asarray(hydrograph[0], float).ravel()
        q = np.asarray(hydrograph[1], float).ravel()
    except (TypeError, ValueError, IndexError) as exc:
        raise ValueError(f"hydrograph must be (t_seconds, q_m3s): {exc}") from None
    if len(t) != len(q) or len(t) < 2:
        raise ValueError(f"hydrograph needs two series of the same length >= 2 (got {len(t)} and {len(q)})")
    if not (np.all(np.isfinite(t)) and np.all(np.isfinite(q))):
        raise ValueError("hydrograph contains a missing or non-finite value")
    if np.any(np.diff(t) <= 0):
        raise ValueError("hydrograph time must be strictly increasing (seconds)")
    if np.any(q < 0):
        raise ValueError("hydrograph discharge must be >= 0 (m3/s)")
    if q.max() <= 0:
        raise ValueError("hydrograph has no flow")
    notes = []
    if q[-1] > 0:
        t = np.append(t, t[-1] + max(float(t[-1] - t[-2]), 1.0))
        q = np.append(q, 0.0)
        notes.append("supplied hydrograph did not end at zero; a zero was appended")
    return t, q, notes
