"""Flood-inflow (PMF) scenario and the no-breach baseline.

The sunny-day breach in `breach.py` has no inflow. For the incremental damage
analysis (ND REG_05.2026 6.4) the dam is also run with a flood: the inflow
hydrograph fills the reservoir, the spillway (and the crest, once overtopped)
discharges, and at a chosen moment the embankment breaches. The same inflow is
routed once more with no breach. Both outflow hydrographs go to the flood
models; the difference between the two floods is the incremental effect of the
failure.

Time zero of both hydrographs is the start of the inflow record; the breach
starts at `t_ref_s` (`Hydrograph.t_ref_s`), and arrival times count from there.
"""
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

from . import breach as br
from . import outlets as outmod
from .units import FT, ACFT, CFS


TRIGGER_PEAK = "peak"          # at the highest pool level of the no-breach routing
TRIGGER_TIME = "time"          # trigger_value hours after the start of the inflow
TRIGGER_WSE = "wse"            # when the pool first reaches trigger_value (ft)
TRIGGERS = (TRIGGER_PEAK, TRIGGER_TIME, TRIGGER_WSE)
CREST_C_US = 2.6               # broad-crested weir coefficient for the embankment crest (US)


@dataclass
class Spillway:
    """Discharge rating of the principal + emergency spillways: elevation (m)
    against discharge (m3/s), rising."""
    elev_m: np.ndarray
    q_cms: np.ndarray

    @classmethod
    def from_csv(cls, path):
        """CSV with pool elevation (ft) and discharge (cfs) in the first two columns."""
        z, q = br.read_csv_series(path, "spillway rating", CFS, FT)   # y: cfs, x: ft
        order = np.argsort(z)
        z, q = z[order], np.maximum.accumulate(np.clip(q[order], 0.0, None))
        return cls(z, q)

    @classmethod
    def from_weir(cls, crest_ft, length_ft, c_us=3.0, span_ft=30.0):
        h = np.arange(0.0, span_ft + 0.001, 0.25)
        return cls((crest_ft + h) * FT, c_us * length_ft * h ** 1.5 * CFS)

    def q(self, wse_m):
        z, q = self.elev_m, self.q_cms
        if wse_m <= z[0]:
            return 0.0
        if wse_m <= z[-1]:
            return float(np.interp(wse_m, z, q))
        h0 = max(z[-1] - z[0], 1e-6)
        return float(q[-1] * ((wse_m - z[0]) / h0) ** 1.5)      # weir-like beyond the table

    @property
    def first_flow_m(self):
        """Elevation of the first rating row with flow > 0 (the conservative crest: the rating
        gives no flow below it). The table's bottom when no row flows."""
        nz = np.nonzero(self.q_cms > 0)[0]
        return float(self.elev_m[nz[0]]) if len(nz) else float(self.elev_m[0])

    @property
    def crest_gap_m(self):
        """Elevation gap between the last q = 0 row and the first q > 0 row (0 if none)."""
        nz = np.nonzero(self.q_cms > 0)[0]
        if not len(nz) or nz[0] == 0:
            return 0.0
        return float(self.elev_m[nz[0]] - self.elev_m[nz[0] - 1])

    @property
    def crest_m(self):
        nz = np.nonzero(self.q_cms > 0)[0]
        return float(self.elev_m[max(0, nz[0] - 1)]) if len(nz) else float(self.elev_m[0])


@dataclass
class FloodInputs:
    inflow_t_s: np.ndarray
    inflow_q_cms: np.ndarray
    spillway: Optional[Spillway] = None
    normal_pool_ft: Optional[float] = None
    crest_length_ft: Optional[float] = None
    trigger: str = TRIGGER_PEAK
    trigger_value: Optional[float] = None
    baseline_csv: Optional[str] = None       # your own no-breach outflow (METHOD_USER)
    crest_c_us: float = CREST_C_US
    outlets: list = field(default_factory=list)      # outlets.Conduit: low-level outlet works
    spillway_schedule: Optional[list] = None          # [(t_s, fraction)] gate on the spillway rating

    @classmethod
    def from_files(cls, inflow_csv, spillway_csv=None, **kw):
        t, q = br.read_csv_series(inflow_csv, "inflow hydrograph", CFS, 3600.0)
        if np.any(np.diff(t) <= 0):
            raise br.BreachInputError("inflow hydrograph times must increase")
        t = t - t[0]
        sp = Spillway.from_csv(spillway_csv) if spillway_csv else None
        return cls(t, np.clip(q, 0.0, None), sp, **kw)

    @classmethod
    def triangle(cls, peak_cfs, rise_hr, base_hr, **kw):
        """A triangular inflow when no hydrograph is available."""
        t = np.array([0.0, rise_hr * 3600.0, base_hr * 3600.0])
        q = np.array([0.0, peak_cfs * CFS, 0.0])
        return cls(t, q, **kw)

    def qin(self, t):
        return float(np.interp(t, self.inflow_t_s, self.inflow_q_cms, right=0.0))

    @property
    def inflow_end_s(self):
        return float(self.inflow_t_s[-1])

    @property
    def inflow_volume_m3(self):
        t, q = self.inflow_t_s, self.inflow_q_cms
        return float(np.trapezoid(q, t)) if hasattr(np, "trapezoid") else float(np.trapz(q, t))


class FloodResult:
    def __init__(self):
        self.failed = True             # False when the trigger was never reached
        self.with_breach = None        # Hydrograph, total outflow with the failure
        self.no_breach = None          # Hydrograph, total outflow without it
        self.breach_only = None        # Hydrograph, the breach component
        self.t_breach_s = 0.0
        self.stage_t_s = None
        self.stage_nb_m = None         # pool level without the breach
        self.stage_wb_m = None
        self.notes: List[str] = []
        self.summary = {}


def _step(qin, qout, t, v, dt):
    q1 = qin(t) - qout(t, v)
    vh = max(0.0, v + 0.5 * dt * q1)
    q2 = qin(t + 0.5 * dt) - qout(t + 0.5 * dt, vh)
    return max(0.0, v + dt * q2)


def route_static(qin, qout, v0, t_in, t_max, dt):
    """No-breach routing loop (route_flood step 1, also used by adequacy.route_no_breach):
    midpoint _step every dt from volume v0 until t_max, stopping once past t_in when the
    outflow is ~0 or < 0.2 % of its peak. qin(t), qout(t, v). Returns (t, v, q) arrays and the
    last time reached."""
    ts, vs, qs = [0.0], [v0], [qout(0.0, v0)]
    t, v, qpk = 0.0, v0, qs[0]
    while t < t_max:
        v = _step(qin, qout, t, v, dt)
        t += dt
        q = qout(t, v)
        ts.append(t)
        vs.append(v)
        qs.append(q)
        qpk = max(qpk, q)
        if t > t_in and (q <= 1e-9 or q < 0.002 * qpk):
            break
    return np.asarray(ts), np.asarray(vs), np.asarray(qs), t


def route_flood(dam, method, overrides, fi, max_hours=240.0, dt_hydro_s=30.0):
    """Route the inflow with and without a breach. `method` is a routed method
    or a TR-60 method of breach.py."""
    ov = overrides or br.BreachOverrides()
    if method == br.METHOD_USER:
        raise br.BreachInputError("route_flood does not handle your own hydrograph")
    if method not in br.ROUTED_METHODS and method not in br.TR60_METHODS:
        raise br.BreachInputError("unknown breach method: {}".format(method))
    dam.validate()
    dam = br.apply_partial(dam, ov)      # partial breach: the bottom is raised for the routing too
    spill = fi.spillway
    pool_ft = fi.normal_pool_ft
    if pool_ft is None and spill is not None:
        pool_ft = spill.crest_m / FT
    if pool_ft is None:
        raise br.BreachInputError("{}: the flood scenario needs the normal pool elevation or a "
                                  "spillway rating".format(dam.dam_id))
    if pool_ft >= dam.top_of_dam_ft:
        raise br.BreachInputError("{}: the normal pool elevation ({:.1f} ft) is not below the "
                                  "top of dam ({:.1f} ft)".format(dam.dam_id, pool_ft,
                                                                 dam.top_of_dam_ft))
    c0 = br._common(dam, pool_ft * FT, "normal pool")
    res, top_m = c0.res, c0.top_m
    notes = list(c0.notes)
    crest_ft = fi.crest_length_ft or dam.crest_length_ft
    crest_m = crest_ft * FT if crest_ft else None
    c_ot = fi.crest_c_us * FT ** 0.5
    if not crest_m:
        notes.append("No embankment crest length given: water above the top of dam is not "
                     "released except through the breach, so the no-breach pool can rise "
                     "above the crest without overtopping. Give the crest length.")
    if spill is None and not fi.outlets:
        notes.append("No spillway rating given: the only releases are the breach and, above "
                     "the crest, the overtopping flow.")
    if fi.outlets:
        notes.append("Outlet works: " + "; ".join(outmod.describe(fi.outlets)) + ". Textbook "
                     "weir/orifice flow without conduit friction or tailwater.")
    if fi.spillway_schedule:
        notes.append("The spillway rating is multiplied by the gate schedule "
                     "(hours:fraction) {}.".format(", ".join(
                         "{:g}:{:g}".format(t / 3600.0, f) for t, f in fi.spillway_schedule)))

    def static_q(wse, open_w=0.0, t=0.0):
        q = spill.q(wse) if spill is not None else 0.0
        if spill is not None and fi.spillway_schedule:
            q *= outmod.schedule_factor(fi.spillway_schedule, t)
        if fi.outlets:
            q += outmod.q_total(fi.outlets, wse, t)
        h = wse - top_m
        if h > 0 and crest_m:
            q += c_ot * max(0.0, crest_m - open_w) * h ** 1.5
        return q

    def qin(t):
        return fi.qin(t)

    t_max = max_hours * 3600.0
    t_in = fi.inflow_end_s
    # a gate that opens (or closes) later than the inflow ends must still be routed
    t_in = max([t_in] + [pt[0] for pt in (fi.spillway_schedule or [])]
               + [pt[0] for cd in (fi.outlets or []) for pt in (cd.schedule or [])])

    # -- 1. no breach -----------------------------------------------------------------
    dt = dt_hydro_s
    v0 = float(res.volume(pool_ft * FT))
    out_nb = lambda tt, vv: static_q(float(res.wse(vv)), 0.0, tt)
    ts, vs_, qs, t = route_static(qin, out_nb, v0, t_in, t_max, dt)
    stage_t = ts.copy()
    wse_nb = res.wse(vs_)
    i_peak = int(np.argmax(wse_nb))
    peak_stage_ft = float(wse_nb[i_peak]) / FT
    if peak_stage_ft * FT > top_m and crest_m:
        notes.append("Without a breach the pool rises to {:.1f} ft, {:.1f} ft over the top of "
                     "dam: the embankment overtops in this flood.".format(
                         peak_stage_ft, peak_stage_ft - dam.top_of_dam_ft))

    # -- 2. when the breach starts --------------------------------------------------------
    trig = fi.trigger
    if trig == TRIGGER_PEAK and peak_stage_ft <= dam.top_of_dam_ft:
        notes.append("The breach is ASSUMED at the peak pool ({:.1f} ft, {:.1f} ft below the top of "
                     "dam): the flood scenario models a failure there, it does not find one. "
                     "Whether the dam passes the flood is the no-breach peak pool against the top "
                     "of dam and freeboard (tool 'Spillway adequacy').".format(
                         peak_stage_ft, dam.top_of_dam_ft - peak_stage_ft))
    if trig == TRIGGER_PEAK:
        k_b = i_peak
    elif trig == TRIGGER_TIME:
        if fi.trigger_value is None:
            raise br.BreachInputError("the breach time (hr) is required")
        k_b = int(np.searchsorted(ts, fi.trigger_value * 3600.0))
        k_b = min(k_b, len(ts) - 1)
    elif trig == TRIGGER_WSE:
        if fi.trigger_value is None:
            raise br.BreachInputError("the trigger pool elevation (ft) is required")
        hit = np.nonzero(wse_nb >= fi.trigger_value * FT)[0]
        if not len(hit):
            out = FloodResult()
            out.failed = False
            out.no_breach = br.Hydrograph(ts, qs, "no-breach routed",
                                          {"peak_pool_ft": round(peak_stage_ft, 2)}, notes)
            out.notes = notes + ["The pool never reaches {:.1f} ft (peak {:.1f} ft): no breach "
                                 "in this flood.".format(fi.trigger_value, peak_stage_ft)]
            out.summary = {"failed": False, "peak_pool_ft_no_breach": round(peak_stage_ft, 2)}
            return out
        k_b = int(hit[0])
    else:
        raise br.BreachInputError("unknown breach trigger: {}".format(trig))
    t_b = float(ts[k_b])
    v_b = float(vs_[k_b])
    wse_b = float(res.wse(v_b))
    if wse_b <= c0.bottom_m + 0.1:
        raise br.BreachInputError("{}: the pool is below the breach bottom when the breach "
                                  "starts".format(dam.dam_id))

    # -- 3. with breach ----------------------------------------------------------------------
    rule = "pool at breach initiation ({})".format(
        {"peak": "peak pool of the flood", "time": "user time", "wse": "trigger elevation"}[trig])
    c = br._common(dam, wse_b, rule)
    v_release0 = c.v_release
    tw, qw, qb, sw, vw = [t_b], [], [], [wse_b], [v_b]
    params = c.params
    if method in br.ROUTED_METHODS:
        opening, p, bnotes = br.make_opening(method, c, dam, ov, wse_b)
        notes += bnotes
        tf = opening.tf
        dtb = min(10.0, tf / 300.0)

        def out_wb(tt, vv):
            wse = float(res.wse(vv))
            tr_ = tt - t_b
            qbr = opening.q(tr_, wse)
            return qbr + static_q(wse, opening.top_width(tr_, wse), tt)

        qtot0 = out_wb(t_b, v_b)
        qw.append(qtot0)
        qb.append(opening.q(0.0, wse_b))
        qpk_w = qtot0
        t, v, dt = t_b, v_b, dtb
        while t < t_max:
            v = _step(qin, out_wb, t, v, dt)
            t += dt
            wse = float(res.wse(v))
            qbr = opening.q(t - t_b, wse)
            q = qbr + static_q(wse, opening.top_width(t - t_b, wse), t)
            tw.append(t)
            qw.append(q)
            qb.append(qbr)
            sw.append(wse)
            vw.append(v)
            qpk_w = max(qpk_w, q)
            if t > t_b + tf and t > t_in and (v - c.v_dead < 0.002 * v_release0
                                              or q < 0.002 * qpk_w):
                break
            if t > t_b + 2 * tf:
                dt = min(60.0, dt * 1.02)
        params.update(p)
        tw, qw, qb = np.asarray(tw), np.asarray(qw), np.asarray(qb)
    else:      # TR-60: peak and volume from TR-60, added to the no-breach flow
        shape = "curvilinear" if method == br.METHOD_TR60_CURVE else "triangular"
        tri = br.tr60_hydrograph(dam, ov, shape, wse_b, rule)
        notes += tri.notes
        params.update(tri.params)
        tt = t_b + tri.t_s
        grid = np.unique(np.concatenate([ts[ts < t_b], [t_b], tt, ts[ts > t_b]]))
        q_nb_g = np.interp(grid, ts, qs, right=0.0)
        q_tri = np.interp(grid - t_b, tri.t_s, tri.q_cms, left=0.0, right=0.0)
        tw = grid[grid >= t_b]
        qb = q_tri[grid >= t_b]
        qw = q_nb_g[grid >= t_b] + qb
        notes.append("TR-60 breach: the triangle is added to the no-breach outflow, so the "
                     "spillway flow is not reduced as the pool drains (slightly high).")

    # assemble the full with-breach record
    before = ts < t_b
    t_full = np.concatenate([ts[before], tw])
    q_full = np.concatenate([qs[before], qw])
    qb_full = np.concatenate([np.zeros(int(before.sum())), qb])
    if q_full[-1] > 0:
        t_full = np.append(t_full, t_full[-1] + 60.0)
        q_full = np.append(q_full, 0.0)
        qb_full = np.append(qb_full, 0.0)
    if qs[-1] > 0:
        ts = np.append(ts, ts[-1] + 60.0)
        qs = np.append(qs, 0.0)

    params = dict(params)
    params.update({"scenario": "flood inflow", "breach_trigger": trig,
                   "breach_initiation_hr": round(t_b / 3600.0, 3),
                   "pool_at_breach_ft": round(wse_b / FT, 2),
                   "peak_pool_ft_no_breach": round(peak_stage_ft, 2),
                   "inflow_peak_cfs": round(float(fi.inflow_q_cms.max()) / CFS, 0),
                   "inflow_volume_acft": round(fi.inflow_volume_m3 / ACFT, 1)})
    wb = br.Hydrograph(t_full, q_full, method, params, notes, t_ref_s=t_b)
    nb = br.Hydrograph(ts, qs, "no-breach routed",
                       {"scenario": "flood inflow, no breach",
                        "peak_pool_ft": round(peak_stage_ft, 2)}, [], t_ref_s=t_b)
    bo = br.Hydrograph(t_full, qb_full, method + " (breach only)", {}, [], t_ref_s=t_b)
    out = FloodResult()
    out.failed = True
    out.with_breach, out.no_breach, out.breach_only = wb.thinned(max_points=400), nb.thinned(max_points=400), bo.thinned(max_points=400)
    out.t_breach_s = t_b
    out.inflow_t_s, out.inflow_q_cms = fi.inflow_t_s, fi.inflow_q_cms
    out.notes = notes
    out.stage_t_s = stage_t
    out.stage_nb_m = wse_nb
    out.summary = {
        "failed": True, "breach_initiation_hr": round(t_b / 3600.0, 3),
        "pool_at_breach_ft": round(wse_b / FT, 2),
        "peak_pool_ft_no_breach": round(peak_stage_ft, 2),
        "peak_outflow_with_breach_cfs": round(wb.peak_cfs, 0),
        "peak_outflow_no_breach_cfs": round(nb.peak_cfs, 0),
        "peak_breach_component_cfs": round(bo.peak_cfs, 0),
        "inflow_peak_cfs": params["inflow_peak_cfs"],
    }
    return out


def hydrograph_from_csv(path, what):
    """A user outflow hydrograph (hours, cfs) that starts at breach initiation."""
    t, q = br.read_csv_series(path, what, CFS, 3600.0)
    if np.any(np.diff(t) <= 0):
        raise br.BreachInputError("{} times must increase".format(what))
    t = t - t[0]
    q = np.clip(q, 0.0, None)
    if q[-1] > 0:
        t = np.append(t, t[-1] + max(60.0, t[-1] - t[-2]))
        q = np.append(q, 0.0)
    return br.Hydrograph(t, q, br.METHOD_USER, {"source": path})


def incremental(depth_wb, depth_nb, wse_wb, wse_nb, thresholds_m):
    """Incremental depth and water-surface rise (with breach minus without),
    and how much area exceeds each rise threshold. Arrays in metres, NaN = dry."""
    dw = np.nan_to_num(depth_wb, nan=0.0)
    dn = np.nan_to_num(depth_nb, nan=0.0)
    inc_depth = np.clip(dw - dn, 0.0, None)
    dry_wb = ~np.isfinite(depth_wb) | (dw <= 0)
    inc_wse = np.where(np.isfinite(wse_wb), wse_wb - np.where(np.isfinite(wse_nb), wse_nb,
                                                              wse_wb - dw), np.nan)
    inc_wse = np.where(dry_wb, np.nan, np.clip(inc_wse, 0.0, None))
    inc_depth = np.where(dry_wb, np.nan, inc_depth)
    stats = {}
    for th in thresholds_m:
        stats[th] = int(np.nansum(inc_depth >= th))
    return inc_depth, inc_wse, stats
