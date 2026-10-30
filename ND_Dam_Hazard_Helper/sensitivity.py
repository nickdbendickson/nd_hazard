# -*- coding: utf-8 -*-
"""v2.8: sensitivity of the hazard classification to model uncertainty.

A dam-break model is not exact: depths and velocities carry the error of the
terrain, the breach parameters and the roughness. This module answers "is
the classification robust?" without running the model or reading the rasters
again. It re-classifies the run with the depth and the velocity of every
cell scaled by a factor (default 0.8, 0.9, 1.0, 1.1, 1.2; optionally also
depth alone and velocity alone) and reports, for each factor,

  * the ND class and the ACER TM 11 class,
  * the number of habitable structures with probable loss of life and in the
    judgment zone,
  * how many structures / road stretches change zone or outcome against the
    unscaled result (and which ones, with their depth and velocity),

and one verdict line ("class unchanged over +-20 %" or "class changes at
depth x 0.9 (ND High -> Medium)").

How it works. evaluate_structures(..., keep_dv=True) keeps, for each WET
structure only, the depth and velocity of the cells its maximum was read from
(rec["_dv"], float32). Here those cells are scaled and classified with the
same code the pipeline uses: helpers.classify_values with the ACER curves
(house on foundation and mobile home), hazard.zone_margins, structures.apply_type
(borderline / 1D rule) and structures.structure_outcome, then
classification.classify_nd / extra_criteria / classify_tm11 on the result.
The work is done in chunks of whole structures, vectorised over the cells,
and a changed copy of a structure record is made only for structures whose
zone or outcome actually changes - so memory stays bounded for a very large
inventory. Roads and rail (linear.analyze_roads / analyze_rail results keep
their per-sample depth and velocity) are scaled the same way: the ACER
passenger-vehicle zone, the motorists counted in TM 11 and the incremental
flag follow the scaled values.

What is NOT scaled (and is said in the output): other dams, lifeline and
crossing facilities (fac_eval keeps only maxima), the sole-access analysis,
the economic-damage and life-loss estimates, and - with a no-breach baseline -
the baseline itself (the breach is scaled against the unchanged baseline).
Velocity is not scaled without a velocity raster (1D / depth-only run); that
is said in the output.

The factors are screening values chosen by the engineer, not rules of the
ND standards or TM 11.

v2.9 - consequence sensitivity (run_consequences). The loss-of-life screening
estimate (Graham 1999 / RCEM severity, consequences.life_loss_estimate) and
the population at risk are re-computed, one factor at a time around the run's
own settings, for other warning-issue times, other persons-per-dwelling values
(structures.reinterpret on a COPY of each record - the same rule the pipeline
uses), the other time of day and the other flood-severity method; plus the
envelope (lowest / highest estimate over those rows) and the two combined
rows (every factor at the value that gave its lowest / highest estimate).
Depth, velocity and arrival are the run's own (no raster is read); the
structure records are never written to (the work is done on copies). The
classification is not affected: the ND / TM 11 classes do not use the
life-loss estimate. The values tried are screening choices (defaults below),
not requirements.

No QGIS objects are needed except through the modules imported above.
"""

import csv
import math
import os

import numpy as np

from .constants import (
    DEFAULT_NEAR_THRESHOLD_FT, DEFAULT_INCREMENTAL_DEPTH_FT,
    DEFAULT_VEHICLE_OCCUPANCY, DEFAULT_VEHICLE_SPEED_MPH, CLASS_HIGH, CLASS_JUDGMENT,
    ZONE_LABELS, SUBDIR_QA, vehicle_curve_available,
)
from .helpers import classify_values, curve_pair, prepare_output_path
from .hazard import zone_margins
from .structures import (
    apply_type, structure_outcome, TYPE_HOUSE, TYPE_MOBILE, OUT_DRY,
)

# SCREENING VALUES CHOSEN BY THE ENGINEER - not requirements of REG_05.2026 or TM 11
DEFAULT_FACTORS = (0.8, 0.9, 1.0, 1.1, 1.2)
MAX_FACTORS = 15
MODE_BOTH, MODE_DEPTH, MODE_VEL = "both", "depth", "velocity"
MODE_LABELS = {MODE_BOTH: "Depth and velocity", MODE_DEPTH: "Depth only",
               MODE_VEL: "Velocity only"}
_MODE_SHORT = {MODE_BOTH: "depth and velocity", MODE_DEPTH: "depth", MODE_VEL: "velocity"}
_MODE_ORDER = {MODE_BOTH: 0, MODE_DEPTH: 1, MODE_VEL: 2}

CHUNK_CELLS = 2000000          # cells classified at once (bounds the working memory)
MAX_CHANGES_PER_ROW = 2000     # changed structures listed per scenario (counts are complete)

CSV_ROWS = "Sensitivity.csv"
CSV_STRUCTURES = "Sensitivity_Structures.csv"
ROW_HEAD = ["Factor", "What", "ND_Class", "TM11_Class", "Probable_LOL_Structures",
            "Judgment_Structures", "Lives_In_Jeopardy", "Flips_vs_Base", "Flipped_Structures",
            "Flipped_Roads", "Class_Changed", "Note"]
CHANGE_HEAD = ["Kind", "ID", "Name", "Habitable", "Factor", "What", "Base_Zone", "New_Zone",
               "Base_Outcome", "New_Outcome", "Base_Depth_ft", "New_Depth_ft",
               "Base_Vel_fps", "New_Vel_fps", "Change"]

# what classify_nd / classify_tm11 / extra_criteria / incremental_view read of a structure
_SLIM_KEYS = ("id", "habitable", "acer_type", "persons", "lifeline", "camp", "cut_off",
              "cut_incremental", "incremental", "incr_note", "damage_usd", "category",
              "max_depth", "max_vel", "zone", "zone_mm", "zone_found", "zone_mobile",
              "outcome", "near", "near_raised", "zone_note", "margin_high", "margin_judg",
              "base_depth", "base_zone")
_RELEVANT = ("Yes", "Unverified")       # "No" (not habitable) cannot change any class


# --------------------------------------------------------------------- options --
def _opt(cfg, key, default):
    v = (cfg or {}).get(key)
    return default if v is None else v


def clean_factors(factors):
    """Sorted, unique positive floats (rounded to 3 places), always with 1.0.
    Non-numbers, zero and negatives are dropped; at most MAX_FACTORS are kept."""
    out = {1.0}
    for f in factors or ():
        try:
            x = round(float(f), 3)
        except (TypeError, ValueError):
            continue
        if math.isfinite(x) and x > 0:
            out.add(x)
    out = sorted(out)
    if len(out) > MAX_FACTORS:                    # keep the ones nearest 1.0
        out = sorted(sorted(out, key=lambda x: (abs(x - 1.0), x))[:MAX_FACTORS])
    return tuple(out)


def scenario_list(factors, split=False, velocity=True):
    """[(mode, factor)] to run, base (both, 1.0) first. Without velocity data
    only depth is scaled (mode "depth" for every factor)."""
    fs = clean_factors(factors)
    out = [(MODE_BOTH if velocity else MODE_DEPTH, 1.0)]
    modes = [MODE_BOTH, MODE_DEPTH, MODE_VEL] if (split and velocity) else \
        [MODE_BOTH] if velocity else [MODE_DEPTH]
    for m in modes:
        out.extend((m, f) for f in fs if f != 1.0)
    return out


def _factors_of(mode, f):
    return (f if mode in (MODE_BOTH, MODE_DEPTH) else 1.0,
            f if mode in (MODE_BOTH, MODE_VEL) else 1.0)


def has_data(structs):
    """True when at least one structure kept its cells (keep_dv=True)."""
    return any(rec.get("_dv") is not None for rec in structs)


def strip_dv(structs):
    """Drop the kept cell arrays (before caching / pickling a state that does
    not need them)."""
    for rec in structs:
        rec.pop("_dv", None)
    return structs


# ---------------------------------------------------------- structures scaling --
def _labels(zone):
    return ZONE_LABELS.get(zone, "") if zone is not None else ""


def _base_snapshot_entry(rec, base_snap, one_d):
    """The baseline result of a structure for scenarios.structure_increment:
    the run's own snapshot when given, else rebuilt from base_depth / base_zone
    (the outcome follows from them; the max/max zone is not known)."""
    if base_snap is not None and rec["id"] in base_snap:
        return base_snap[rec["id"]]
    bd = rec.get("base_depth")
    ent = {"max_depth": bd, "zone": rec.get("base_zone"), "zone_mm": None, "outcome": OUT_DRY}
    if bd is not None and bd > 0:
        ent["outcome"] = structure_outcome({"max_depth": bd, "zone": rec.get("base_zone")},
                                           one_d)
    return ent


def _slim(rec, view):
    out = {k: rec[k] for k in _SLIM_KEYS if k in rec}
    for k in ("max_depth", "max_vel", "zone", "zone_mm", "outcome", "near", "near_raised",
              "zone_note", "margin_high", "margin_judg", "zone_found", "zone_mobile"):
        if k in view:
            out[k] = view[k]
    return out


def _chunks(items, chunk_cells):
    cur, n = [], 0
    for it in items:
        m = int(it[1]["_dv"][0].size)
        if cur and n + m > chunk_cells:
            yield cur
            cur, n = [], 0
        cur.append(it)
        n += m
    if cur:
        yield cur


def _scale_structures(structs, fd, fv, opts, changes=None, label=""):
    """(list of structures with the scaled results, n_flips). Structures whose
    zone / outcome / borderline flag / incremental flag do not change are the
    original records (no copy). `changes` (list) collects the change rows."""
    one_d, near_ft, bh = opts["one_d"], opts["near_ft"], opts["borderline_higher"]
    inc_on = opts["incremental"]
    items = [(i, rec) for i, rec in enumerate(structs)
             if rec.get("habitable") in _RELEVANT and rec.get("_dv") is not None
             and rec.get("_types") and rec.get("max_depth") and rec["_dv"][0].size]
    out = list(structs)
    flips = 0
    if not items:
        return out, 0
    curves = {TYPE_HOUSE: curve_pair("found"), TYPE_MOBILE: curve_pair("mobile")}
    for chunk in _chunks(items, opts["chunk_cells"]):
        lens = [rec["_dv"][0].size for _i, rec in chunk]
        starts = np.concatenate(([0], np.cumsum(lens)[:-1])).astype(np.intp)
        D = np.concatenate([rec["_dv"][0] for _i, rec in chunk]).astype(np.float64)
        has_v = [rec["_dv"][1] is not None for _i, rec in chunk]
        V = np.concatenate([rec["_dv"][1] if rec["_dv"][1] is not None
                            else np.full(rec["_dv"][0].shape, np.nan, dtype=np.float32)
                            for _i, rec in chunk]).astype(np.float64)
        if fd != 1.0:
            D *= fd
        if fv != 1.0:
            V *= fv
        maxd = np.maximum.reduceat(D, starts)
        with np.errstate(invalid="ignore"):
            maxv = np.fmax.reduceat(V, starts)
            res = {}
            for typ, cv in curves.items():
                cls = classify_values(D, V, *cv)
                mh, mj = zone_margins(D, V, *cv)
                res[typ] = (np.minimum.reduceat(cls, starts),
                            np.minimum.reduceat(mh, starts), np.minimum.reduceat(mj, starts))
        for k, (idx, rec) in enumerate(chunk):
            hv = has_v[k]
            types = {}
            for typ in (TYPE_HOUSE, TYPE_MOBILE):
                z, mh, mj = res[typ]
                types[typ] = {"zone": int(z[k]), "has_vel": hv, "loc": None,
                              "margin_high": float(mh[k]) if hv else None,
                              "margin_judg": float(mj[k]) if hv else None}
            view = {"_types": types, "acer_type": rec["acer_type"],
                    "max_depth": float(maxd[k]),
                    "max_vel": float(maxv[k]) if hv else None}
            view["zone_found"], view["zone_mobile"] = types[TYPE_HOUSE]["zone"], \
                types[TYPE_MOBILE]["zone"]
            apply_type(view, one_d, near_ft, bh)
            inc_new = None
            if inc_on and rec.get("incremental") is not None:
                inc_new = _increment_flag(rec, view, opts)
            same = (view.get("zone") == rec.get("zone") and view.get("zone_mm") ==
                    rec.get("zone_mm") and view.get("outcome") == rec.get("outcome")
                    and view.get("near") == rec.get("near")
                    and bool(view.get("near_raised")) == bool(rec.get("near_raised"))
                    and (inc_new is None or inc_new == rec.get("incremental")))
            if same:
                continue
            slim = _slim(rec, view)
            if inc_new is not None:
                slim["incremental"] = inc_new
            out[idx] = slim
            zone_or_outcome = (view.get("zone") != rec.get("zone")
                               or view.get("outcome") != rec.get("outcome"))
            if zone_or_outcome:
                flips += 1
            if changes is not None and len(changes) < opts["max_changes"] and \
                    (zone_or_outcome or inc_new is not None and inc_new != rec.get("incremental")):
                bits = []
                if view.get("zone") != rec.get("zone"):
                    bits.append("zone")
                if view.get("outcome") != rec.get("outcome"):
                    bits.append("outcome")
                if inc_new is not None and inc_new != rec.get("incremental"):
                    bits.append("counted as breach impact" if inc_new
                                else "no longer counted as breach impact")
                changes.append({
                    "kind": "Structure", "id": rec["id"], "name": _struct_name(rec),
                    "habitable": rec.get("habitable"), "label": label,
                    "base_zone": rec.get("zone"), "new_zone": view.get("zone"),
                    "base_outcome": rec.get("outcome"), "new_outcome": view.get("outcome"),
                    "base_depth": rec.get("max_depth"), "new_depth": view["max_depth"],
                    "base_vel": rec.get("max_vel"), "new_vel": view["max_vel"],
                    "change": ", ".join(bits)})
    return out, flips


def _struct_name(rec):
    try:
        from .structures import address_of
        a = address_of(rec)
        return ", ".join(x for x in a if x) if a else ""
    except Exception:
        return ""


def _increment_flag(rec, view, opts):
    """The incremental flag of the scaled structure: the run's flag, changed
    only where the decision itself changes between the unscaled and the scaled
    breach result (so a rebuilt baseline entry cannot invent differences)."""
    from .scenarios import structure_increment
    base = _base_snapshot_entry(rec, opts["base_snap"], opts["one_d"])
    key = ("inc0", rec["id"])
    cache = opts["_cache"]
    if key not in cache:
        cache[key] = structure_increment(rec, base, opts["incremental_ft"],
                                         opts["same_method"], opts["one_d"])[0]
    now = structure_increment(view, base, opts["incremental_ft"], opts["same_method"],
                              opts["one_d"])[0]
    return bool(rec.get("incremental")) if now == cache[key] else now


# ------------------------------------------------------------------ lines scaling --
def _line_data(r):
    d, v = r.get("depth"), r.get("vel")
    return (np.asarray(d, dtype=np.float64) if d is not None else None,
            np.asarray(v, dtype=np.float64) if v is not None else None)


def _vehicle_zone(d, v):
    if not vehicle_curve_available() or d is None:
        return None
    ok = np.isfinite(d) & (d > 0)
    if not ok.any():
        return None
    v = np.zeros(d.shape) if v is None else np.where(np.isfinite(v), v, 0.0)
    return int(classify_values(d[ok], v[ok], *curve_pair("vehicle")).min())


def _scale_lines(lines, fd, fv, opts, vehicle, changes=None, label="", kind="Road"):
    """(scaled list, n_flips) for road (vehicle=True) / rail stretches."""
    from .scenarios import mark_lines_incremental
    if not lines:
        return lines or [], 0
    scale_veh = vehicle and opts["veh"]
    inc_on = opts["incremental"]
    if not scale_veh and not (inc_on and fd != 1.0):
        return lines, 0
    out, flips, todo = [], 0, []
    for r in lines:
        d0, v0 = _line_data(r)
        if d0 is None:
            out.append(r)
            continue
        d = d0 * fd
        v = v0 * fv if v0 is not None else None
        nr = {k: val for k, val in r.items() if k not in ("depth", "vel")}
        nr["depth"], nr["vel"] = d, v
        with np.errstate(invalid="ignore"):
            nr["max_depth"] = float(np.nanmax(d)) if d.size and np.isfinite(d).any() \
                else r.get("max_depth")
        if scale_veh:
            vz = _vehicle_zone(d, v)
            nr["vehicle_zone"] = vz
            nr["persons"] = _persons(r, vz, opts)
        todo.append((r, nr))
        out.append(nr)
    if inc_on and todo:
        # the incremental decision, unscaled vs scaled (both re-judged the same way):
        # only a difference between the two changes the run's own flag
        own_vel = vehicle and opts["veh_vel"]
        b_list = [{**{k: val for k, val in r.items() if k not in ("depth", "vel")},
                   "depth": _line_data(r)[0], "vel": _line_data(r)[1]} for r, _nr in todo]
        mark_lines_incremental(b_list, opts["incremental_ft"], own_velocity=own_vel)
        mark_lines_incremental([nr for _r, nr in todo], opts["incremental_ft"],
                               own_velocity=own_vel)
        for (r, nr), b in zip(todo, b_list):
            kept = r.get("incremental")
            if kept is None:
                nr["incremental"] = None
            elif nr["incremental"] == b["incremental"]:
                nr["incremental"] = kept
    for r, nr in todo:
        chg = []
        if nr.get("vehicle_zone") != r.get("vehicle_zone") and vehicle:
            chg.append("vehicle zone")
        if nr.get("incremental") != r.get("incremental"):
            chg.append("counted as breach impact" if nr.get("incremental")
                       else "no longer counted as breach impact")
        if "vehicle zone" in chg:
            flips += 1
        if chg and changes is not None and len(changes) < opts["max_changes"]:
            changes.append({
                "kind": kind, "id": r.get("fid"), "name": r.get("name") or "",
                "habitable": "", "label": label,
                "base_zone": r.get("vehicle_zone") if vehicle else None,
                "new_zone": nr.get("vehicle_zone") if vehicle else None,
                "base_outcome": "", "new_outcome": "",
                "base_depth": r.get("max_depth"), "new_depth": nr.get("max_depth"),
                "base_vel": r.get("max_vel"),
                "new_vel": (r["max_vel"] * fv if r.get("max_vel") is not None else None),
                "change": ", ".join(chg)})
    return out, flips


def _persons(r, vz, opts):
    """Motorists of a road stretch in the scaled vehicle zone (as
    linear.analyze_roads counts them)."""
    if not opts["veh"] or not r.get("aadt") or r.get("paved") == "No":
        return None
    if vz not in (CLASS_HIGH, CLASS_JUDGMENT):
        return None
    if r.get("persons"):
        return r["persons"]                      # the run's own figure (its speed / occupancy)
    vehicles = r["aadt"] / 24.0 * (r.get("length_ft", 0.0) / 5280.0) / \
        max(opts["speed_mph"], 1.0)
    return vehicles * opts["occupancy"]


# ------------------------------------------------------------------ classification --
def _classify(structs, roads, rails, opts):
    from .classification import classify_nd, classify_tm11, extra_criteria
    from .scenarios import incremental_view
    if opts["incremental"]:
        s_view, r_view, l_view = incremental_view(structs, roads, rails)
    else:
        s_view, r_view, l_view = structs, roads, rails
    nd = classify_nd(s_view, r_view, l_view, judgment_as_probable=opts["judgment"],
                     one_d=opts["one_d"], roads_evaluated=opts["roads_evaluated"],
                     rail_evaluated=opts["rail_evaluated"],
                     structs_evaluated=opts.get("structs_evaluated", True))
    tm = classify_tm11(s_view, r_view, judgment_as_probable=opts.get("tm11_judgment", True))
    extra_criteria(nd, s_view, opts["res"] if opts["res"] is not None else {},
                   opts["cfg"] or {})
    return nd, tm


def _pct_text(factors):
    lo = min((f for f in factors if f < 1.0), default=None)
    hi = max((f for f in factors if f > 1.0), default=None)
    a = None if lo is None else round((1.0 - lo) * 100)
    b = None if hi is None else round((hi - 1.0) * 100)
    if a is not None and b is not None:
        return "+-{:g} %".format(a) if a == b else "-{:g} % to +{:g} %".format(a, b)
    if a is not None:
        return "-{:g} %".format(a)
    if b is not None:
        return "+{:g} %".format(b)
    return "0 %"


def _fmt_factor(f):
    return "{:g}".format(f)


def _verdict(base_row, rows, factors):
    """(text, [change dicts]) - the smallest departure at which the class changes."""
    changed = []
    for r in rows:
        if r["is_base"]:
            continue
        nd_ch = r["nd_class"] != base_row["nd_class"]
        tm_ch = r["tm11_class"] != base_row["tm11_class"]
        if nd_ch or tm_ch:
            changed.append({"factor": r["factor"], "mode": r["mode"], "what": r["what"],
                            "nd": (base_row["nd_class"], r["nd_class"]) if nd_ch else None,
                            "tm11": (base_row["tm11_class"], r["tm11_class"]) if tm_ch
                            else None})
    if not changed:
        return "class unchanged over {}".format(_pct_text(factors)), changed
    changed.sort(key=lambda c: (abs(c["factor"] - 1.0), _MODE_ORDER[c["mode"]], c["factor"]))
    c = changed[0]
    parts = []
    if c["nd"]:
        parts.append("ND {} -> {}".format(*c["nd"]))
    if c["tm11"]:
        parts.append("TM 11 {} -> {}".format(*c["tm11"]))
    text = "class changes at {} x {} ({})".format(_MODE_SHORT[c["mode"]],
                                                  _fmt_factor(c["factor"]), "; ".join(parts))
    if len(changed) > 1:
        text += "; {} more scenario(s) change the class".format(len(changed) - 1)
    return text, changed


# ------------------------------------------------------------------- entry point --
def run(structs, roads=None, rails=None, *, factors=DEFAULT_FACTORS, split=False, cfg=None,
        res=None, one_d=None, judgment_as_probable=None, near_ft=None, borderline_higher=None,
        incremental_ft=None, same_method=True, base_snap=None, incremental=None,
        roads_evaluated=True, rail_evaluated=True, structs_evaluated=True, occupancy=None,
        speed_mph=None, nd_base=None, tm11_base=None, chunk_cells=CHUNK_CELLS,
        max_changes=MAX_CHANGES_PER_ROW, progress_cb=None):
    """Re-classify with scaled depth / velocity (see the module text).

    structs: the evaluated structures of ONE breach scenario, evaluated with
    keep_dv=True (structures without rec["_dv"] are held at their unscaled
    result and counted in the notes). roads / rails: that scenario's
    analyze_roads / analyze_rail results. cfg: the pipeline settings (keys
    one_d, judgment_as_probable, near_ft, borderline_higher, incremental_ft,
    occupancy, speed_mph are used unless given as arguments); res: the
    scenario's results dict (facility / access / boundary data for
    extra_criteria; not scaled). incremental: whether a no-breach baseline
    was used (default: any structure / road carries an incremental flag);
    same_method / base_snap: as scenarios.mark_structures_incremental (the
    baseline snapshot lets the increment be re-judged exactly). nd_base /
    tm11_base: the run's own classes (str or the classify_* dicts) - a
    note is made when the re-computed base differs.

    Returns {"available", "rows", "changes", "notes", "verdict", "base",
    "factors", "split", "velocity", "scaled": {...}}."""
    roads, rails = list(roads or []), list(rails or [])
    fac = clean_factors(factors)
    notes = []
    wet = [rec for rec in structs if rec.get("habitable") in _RELEVANT
           and rec.get("max_depth") and rec.get("_types")]
    with_dv = [rec for rec in wet if rec.get("_dv") is not None and rec["_dv"][0].size]
    lines_ok = any(r.get("depth") is not None for r in roads + rails)
    result = {"available": False, "rows": [], "changes": [], "notes": notes, "verdict": "",
              "base": None, "factors": fac, "split": bool(split), "velocity": False,
              "scaled": {}}
    if wet and not with_dv and not lines_ok:
        notes.append("the structures were evaluated without keep_dv (or came from an "
                     "evaluation saved before v2.8): re-run the classification with the "
                     "sensitivity option on")
    if not with_dv and not lines_ok:
        if not wet:
            notes.append("no wet habitable structures or road stretches to scale")
        result["verdict"] = "sensitivity not made: " + "; ".join(notes)
        return result

    vel_struct = any(rec["_dv"][1] is not None for rec in with_dv)
    vel_lines = any(r.get("vel") is not None and r.get("max_vel") is not None
                    for r in roads)
    velocity = vel_struct or (not with_dv and vel_lines)
    one_d_o = bool(_opt(cfg, "one_d", False) if one_d is None else one_d)
    opts = {
        "one_d": one_d_o,
        # 2.35.0: "judgment" is the ND option (MEDIUM by rule unless on); TM 11 keeps its own
        "judgment": bool(_opt(cfg, "nd_judgment_as_probable", False)
                         if judgment_as_probable is None else judgment_as_probable),
        "tm11_judgment": bool(_opt(cfg, "judgment_as_probable", True)),
        "near_ft": float(_opt(cfg, "near_ft", DEFAULT_NEAR_THRESHOLD_FT)
                         if near_ft is None else near_ft),
        "borderline_higher": bool(_opt(cfg, "borderline_higher", False)
                                  if borderline_higher is None else borderline_higher),
        "incremental_ft": float(_opt(cfg, "incremental_ft", DEFAULT_INCREMENTAL_DEPTH_FT)
                                if incremental_ft is None else incremental_ft),
        "same_method": bool(same_method), "base_snap": base_snap,
        "occupancy": float(_opt(cfg, "occupancy", DEFAULT_VEHICLE_OCCUPANCY)
                           if occupancy is None else occupancy),
        "speed_mph": float(_opt(cfg, "speed_mph", DEFAULT_VEHICLE_SPEED_MPH)
                           if speed_mph is None else speed_mph),
        "roads_evaluated": bool(roads_evaluated), "rail_evaluated": bool(rail_evaluated),
        "structs_evaluated": bool(structs_evaluated),
        "res": res, "cfg": cfg, "chunk_cells": max(1000, int(chunk_cells)),
        "max_changes": max(1, int(max_changes)), "veh": vehicle_curve_available(),
        "veh_vel": velocity, "_cache": {},
    }
    if incremental is None:
        incremental = bool((res or {}).get("incremental")) or any(
            x.get("incremental") is not None for x in structs + roads + rails
            if isinstance(x, dict))
    opts["incremental"] = bool(incremental)
    if not velocity:
        notes.append("velocity not scaled: no velocity raster (1D / depth-only criterion) - "
                     "only depth is scaled")
    if wet and len(with_dv) < len(wet):
        notes.append("{} of {} wet habitable structure(s) have no stored cell data and are "
                     "held at their unscaled result".format(len(wet) - len(with_dv), len(wet)))
    if roads and not opts["veh"]:
        notes.append("passenger-vehicle curve (TM 11 Fig. 4) not configured: road "
                     "motorists are not scaled")
    if (roads or rails) and not lines_ok:
        notes.append("the road / rail results carry no per-sample depth: roads and rail are "
                     "held at their unscaled result - structures only")
    if not roads and not rails:
        pass
    if opts["incremental"]:
        notes.append("no-breach baseline: only the breach is scaled, the baseline stays as "
                     "it was")
    if opts["incremental"] and not opts["same_method"] and base_snap is None and \
            not opts["one_d"]:
        notes.append("breach and baseline differ in velocity data and no baseline snapshot "
                     "was given: the increment is re-judged by depth and flooded-when-dry "
                     "only")
    notes.append("not scaled: other dams, lifeline / crossing facilities, sole-access "
                 "analysis, economic damage and life-loss estimates (held at their unscaled "
                 "values) - the life-loss estimate has its own consequence sensitivity "
                 "(QA/{})".format(CSV_CONSEQ))

    # ---- the base (unscaled) classification ---------------------------------
    nd0, tm0 = _classify(structs, roads, rails, opts)
    base_row = _row(1.0, MODE_BOTH if velocity else MODE_DEPTH, nd0, tm0, 0, 0, 0, True, velocity)
    for label, given, mine in (("ND", nd_base, nd0["cls"]), ("TM 11", tm11_base, tm0["cls"])):
        g = given.get("cls") if isinstance(given, dict) else given
        if g is not None and str(g).lower() != str(mine).lower():
            notes.append("the {} class re-computed here ({}) differs from the run's ({}): "
                         "the base of this table is the re-computed one - facilities or "
                         "settings not passed in may explain it".format(label, mine, g))
    rows = [base_row]
    changes = []
    todo = [s for s in scenario_list(fac, split, velocity) if s[1] != 1.0]
    for k, (mode, f) in enumerate(todo):
        if progress_cb:
            progress_cb(100.0 * k / max(1, len(todo)),
                        "Sensitivity: {} x {:g}".format(_MODE_SHORT[mode], f))
        fd, fv = _factors_of(mode, f)
        label = _label(mode, f, velocity)
        rec_changes = []
        s2, n_s = _scale_structures(structs, fd, fv, opts, rec_changes, label)
        r2, n_r = _scale_lines(roads, fd, fv, opts, True, rec_changes, label, "Road")
        l2, n_l = _scale_lines(rails, fd, fv, opts, False, rec_changes, label, "Rail")
        nd, tm = _classify(s2, r2, l2, opts)
        rows.append(_row(f, mode, nd, tm, n_s, n_r + n_l, len(rec_changes), False, velocity,
                         base_row))
        for c in rec_changes:
            c["factor"], c["mode"] = f, mode
        changes.extend(rec_changes)
        if len(rec_changes) >= opts["max_changes"]:
            notes.append("{}: the list of changed items is cut at {} (the counts are "
                         "complete)".format(label, opts["max_changes"]))
        del s2, r2, l2
    rows.sort(key=lambda r: (_MODE_ORDER[r["mode"]] if not r["is_base"] else -1, r["factor"]))
    verdict, cchanges = _verdict(base_row, rows, fac)
    if progress_cb:
        progress_cb(100, "Sensitivity done")
    result.update({
        "available": True, "rows": rows, "changes": changes, "notes": notes,
        "verdict": verdict, "base": base_row, "velocity": velocity,
        "class_changes": cchanges,
        "scaled": {"structures": len(with_dv), "roads": sum(1 for r in roads
                                                            if r.get("depth") is not None),
                   "rail": sum(1 for r in rails if r.get("depth") is not None)}})
    return result


def _label(mode, f, velocity):
    if not velocity:
        return "Depth only (no velocity raster) x {:g}".format(f)
    return "{} x {:g}".format(MODE_LABELS[mode], f)


def _row(f, mode, nd, tm, n_s, n_l, n_changes, is_base, velocity, base=None):
    counts = nd.get("counts", {})
    what = MODE_LABELS[mode] if velocity else "Depth only (no velocity raster)"
    row = {"factor": f, "mode": mode, "what": what, "nd_class": nd["cls"],
           "tm11_class": tm["cls"],
           "probable": counts.get("habitable_probable", 0),
           "judgment": counts.get("habitable_judgment", 0), "lij": tm["lij"],
           "flips_vs_base": n_s + n_l, "flips_structures": n_s, "flips_roads": n_l,
           "is_base": is_base, "class_changed": False, "note": "base (unscaled)" if is_base
           else ""}
    if base is not None:
        row["class_changed"] = (row["nd_class"] != base["nd_class"]
                                or row["tm11_class"] != base["tm11_class"])
        if row["class_changed"]:
            row["note"] = "class changes"
    return row


# ---------------------------------------------------------------------- outputs --
def summary_lines(result):
    """Lines for the summary text (classification.summary_lines extra key
    "sensitivity"). A run_consequences result gives conseq_summary_lines."""
    if not result:
        return []
    if result.get("kind") == KIND_CONSEQ:
        return conseq_summary_lines(result)
    if not result.get("available"):
        return [result.get("verdict") or "sensitivity not made"]
    out = ["Sensitivity to depth / velocity (screening; factors chosen by the engineer, "
           "not a requirement): " + result["verdict"]]
    out.append("{:<24} {:>6}  {:<7} {:<12} {:>8} {:>9} {:>6}".format(
        "Scaled", "factor", "ND", "TM 11", "probable", "judgment", "flips"))
    for r in result["rows"]:
        out.append("{:<24} {:>6}  {:<7} {:<12} {:>8} {:>9} {:>6}{}".format(
            r["what"][:24], _fmt_factor(r["factor"]), r["nd_class"], r["tm11_class"],
            r["probable"], r["judgment"], r["flips_vs_base"],
            "  (base)" if r["is_base"] else ""))
    sc = result.get("scaled") or {}
    out.append("Scaled from the stored cells of {} wet habitable structure(s){}; the rasters "
               "were not read again.".format(
                   sc.get("structures", 0),
                   ", {} road stretch(es), {} rail stretch(es)".format(
                       sc.get("roads", 0), sc.get("rail", 0))
                   if sc.get("roads") or sc.get("rail") else ""))
    for n in result.get("notes") or []:
        out.append("Note: " + n)
    if result.get("changes"):
        out.append("Items that change zone or outcome: QA/{}".format(CSV_STRUCTURES))
    return out


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return round(v, 3)
    return v


def rows_table(result):
    """[dict keyed by ROW_HEAD] for QA/Sensitivity.csv."""
    out = []
    for r in (result or {}).get("rows") or []:
        out.append({"Factor": _fmt_factor(r["factor"]), "What": r["what"],
                    "ND_Class": r["nd_class"], "TM11_Class": r["tm11_class"],
                    "Probable_LOL_Structures": r["probable"],
                    "Judgment_Structures": r["judgment"],
                    "Lives_In_Jeopardy": round(float(r["lij"]), 2),
                    "Flips_vs_Base": r["flips_vs_base"],
                    "Flipped_Structures": r["flips_structures"],
                    "Flipped_Roads": r["flips_roads"],
                    "Class_Changed": "Yes" if r["class_changed"] else "No",
                    "Note": r["note"]})
    return out


def changes_table(result):
    """[dict keyed by CHANGE_HEAD] for QA/Sensitivity_Structures.csv."""
    out = []
    for c in (result or {}).get("changes") or []:
        out.append({"Kind": c["kind"], "ID": c["id"], "Name": c["name"],
                    "Habitable": c["habitable"], "Factor": _fmt_factor(c["factor"]),
                    "What": c["label"] or MODE_LABELS.get(c.get("mode"), ""),
                    "Base_Zone": _labels(c["base_zone"]), "New_Zone": _labels(c["new_zone"]),
                    "Base_Outcome": c["base_outcome"], "New_Outcome": c["new_outcome"],
                    "Base_Depth_ft": _cell(c["base_depth"]), "New_Depth_ft": _cell(c["new_depth"]),
                    "Base_Vel_fps": _cell(c["base_vel"]), "New_Vel_fps": _cell(c["new_vel"]),
                    "Change": c["change"]})
    return out


def _write(path, head, rows):
    path = prepare_output_path(path)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=head)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in head})
    return path


def write_rows_csv(folder, result):
    """QA/Sensitivity.csv ("folder" is the QA folder); returns the path."""
    return _write(os.path.join(folder, CSV_ROWS), ROW_HEAD, rows_table(result))


def write_structures_csv(folder, result):
    """QA/Sensitivity_Structures.csv (structures, roads and rail whose zone,
    outcome or breach-impact flag changes at each factor); returns the path."""
    return _write(os.path.join(folder, CSV_STRUCTURES), CHANGE_HEAD, changes_table(result))


def write_csvs(folder, result):
    """Both files; [] when the sensitivity was not made."""
    if not result or not result.get("available"):
        return []
    return [write_rows_csv(folder, result), write_structures_csv(folder, result)]


def qa_folder(out_folder):
    return os.path.join(out_folder, SUBDIR_QA)


# =============================================================================
# v2.9: consequence sensitivity - the loss-of-life screening estimate and the
# population at risk re-computed for other warning times, persons per dwelling,
# time of day and flood-severity method (one factor at a time + envelope)
# =============================================================================
# SCREENING VALUES CHOSEN BY THE ENGINEER - not requirements of REG_05.2026,
# TM 11, Graham (1999) or the RCEM guidelines
DEFAULT_WARNING_SHIFTS_HR = (-0.5, 0.5, 1.0)   # warning issued this much earlier (-) / later (+)
DEFAULT_PPD_VALUES = (2.0, 2.5, 3.0)           # persons per dwelling tried
MAX_CONSEQ_VALUES = 10                         # values kept per factor
KIND_CONSEQ = "consequences"
CSV_CONSEQ = "Sensitivity_Consequences.csv"
CONSEQ_HEAD = ["Factor", "Value", "Warning_Issued_hr", "Persons_per_Dwelling", "Population",
               "Severity_Method", "PAR", "LOL", "LOL_Low", "LOL_High", "PAR_Change",
               "LOL_Change", "LOL_Change_pct", "Structures_Changed", "Note"]
F_BASE, F_WARN, F_PPD, F_PERIOD, F_SEV, F_COMB = (
    "base", "warning", "persons_per_dwelling", "population", "severity", "combined")
FACTOR_LABELS = {F_BASE: "Base (as run)", F_WARN: "Warning issued",
                 F_PPD: "Persons per dwelling", F_PERIOD: "Population (time of day)",
                 F_SEV: "Flood severity method", F_COMB: "Combined"}
_FACTOR_ORDER = {F_BASE: 0, F_WARN: 1, F_PPD: 2, F_PERIOD: 3, F_SEV: 4, F_COMB: 5}
SEVERITY_LABELS = {"graham": "Graham (1999) DV bands", "rcem": "RCEM DV bands"}
# cfg keys (the pipeline's DEFAULTS would take these) - screening choices, see above
CONSEQ_DEFAULTS = {"conseq_sensitivity": False,
                   "conseq_sens_warning_hr": "-0.5, 0.5, 1.0",
                   "conseq_sens_ppd": "2.0, 2.5, 3.0",
                   "conseq_sens_population": True, "conseq_sens_severity": True}
# what consequences.life_loss_estimate / population_at_risk read of a structure
_LL_KEYS = ("habitable", "max_depth", "dv_max", "arrival", "persons", "persons_basis",
            "pop_night", "pop_day")


def _parse_values(v):
    """[floats] from "0.5, -0.5 1" or a list; None when nothing usable."""
    if v is None:
        return None
    items = v if isinstance(v, (list, tuple)) else \
        str(v).replace(";", " ").replace(",", " ").split()
    out = []
    for x in items:
        try:
            out.append(float(x))
        except (TypeError, ValueError):
            continue
    return out or None


def clean_values(values, positive=False, drop=None):
    """Sorted unique values (3 decimals); non-numbers dropped, non-positive ones
    too with `positive`, and `drop` (the base value). At most MAX_CONSEQ_VALUES."""
    out = set()
    for v in values or ():
        try:
            x = round(float(v), 3)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(x) or (positive and x <= 0):
            continue
        if drop is not None and abs(x - drop) < 1e-9:
            continue
        out.add(x)
    return tuple(sorted(out)[:MAX_CONSEQ_VALUES])


def conseq_options_from_cfg(cfg):
    """Keyword arguments for run_consequences from the pipeline cfg (keys of
    CONSEQ_DEFAULTS; missing / empty keys take the defaults)."""
    cfg = cfg or {}
    shifts = _parse_values(cfg.get("conseq_sens_warning_hr"))
    ppd = _parse_values(cfg.get("conseq_sens_ppd"))
    return {"warning_shifts_hr": DEFAULT_WARNING_SHIFTS_HR if shifts is None else shifts,
            "persons_per_dwelling": DEFAULT_PPD_VALUES if ppd is None else ppd,
            "population": bool(cfg.get("conseq_sens_population", True)),
            "severity": bool(cfg.get("conseq_sens_severity", True))}


def _base_settings(cfg):
    from .constants import (
        DEFAULT_WARNING_ISSUED_HR, TM11_PERSONS_PER_DWELLING, DEFAULT_MIN_UNVERIFIED_AREA_FT2,
    )
    cfg = cfg or {}
    try:
        ppd = int(_opt(cfg, "persons_per_dwelling", TM11_PERSONS_PER_DWELLING))  # as pipeline
    except (TypeError, ValueError):
        ppd = TM11_PERSONS_PER_DWELLING
    return {"issued": float(_opt(cfg, "warning_issued_hr", DEFAULT_WARNING_ISSUED_HR)),
            "understanding": _opt(cfg, "understanding", "vague"),
            "method": _opt(cfg, "severity_method", "graham"),
            "period": _opt(cfg, "population", "night"),
            "high": bool(_opt(cfg, "high_severity", False)),
            "ppd": float(ppd),
            "min_area": float(_opt(cfg, "min_area_ft2", DEFAULT_MIN_UNVERIFIED_AREA_FT2))}


def _issued_text(h):
    if abs(h) < 1e-9:
        return "at breach start"
    return "{:g} h {} breach start".format(abs(h), "after" if h > 0 else "before")


def _ppd_persons(items, ppd, base_ppd, min_area):
    """({item index: (persons, basis)}, n re-derived, n scaled) at `ppd` persons per
    dwelling - only for structures whose population at risk can use the persons
    count (no NSI population for one of the times of day, no engineer figure).
    structures.reinterpret runs on a COPY of the record (the original is never
    written); a record without the source data it needs is scaled in proportion."""
    from .structures import reinterpret
    out, n_re, n_scaled = {}, 0, 0
    for i, (rec, w) in enumerate(items):
        if w.get("persons_basis") == "engineer review":
            continue
        if w.get("pop_night") is not None and w.get("pop_day") is not None:
            continue
        try:
            c = dict(rec)
            reinterpret([c], min_area, ppd)
            out[i] = (c.get("persons"), c.get("persons_basis"))
            n_re += 1
        except Exception:
            basis = str(w.get("persons_basis") or "")
            if "per dwelling" in basis and base_ppd > 0 and w.get("persons"):
                out[i] = (float(w["persons"]) * ppd / base_ppd, basis + " (scaled)")
                n_scaled += 1
    return out, n_re, n_scaled


def _evaluate_conseq(items, s, persons=None):
    """(totals, [(par, rate)] per item) with the settings `s`, computed on copies
    by consequences.life_loss_estimate exactly as the pipeline calls it."""
    from .consequences import life_loss_estimate
    tot = {"par": 0.0, "lol": 0.0, "lol_low": 0.0, "lol_high": 0.0}
    per = []
    for i, (_rec, w) in enumerate(items):
        x = dict(w)
        if persons and i in persons:
            x["persons"], x["persons_basis"] = persons[i]
        life_loss_estimate(x, s["issued"], s["understanding"], s["method"], s["period"],
                           s["high"])
        for k in tot:
            tot[k] += x.get(k) or 0.0
        per.append((x.get("par") or 0.0, x.get("fatality_rate")))
    return tot, per


def _same_pr(a, b):
    (pa, ra), (pb, rb) = a, b
    if abs(pa - pb) > 1e-9:
        return False
    if ra is None or rb is None:
        return ra is rb
    return abs(ra - rb) <= 1e-12


def _crow(factor, value, value_text, s, tot, per, base=None, base_per=None, note=""):
    row = {"factor": factor, "what": FACTOR_LABELS[factor], "value": value,
           "value_text": value_text, "issued": s["issued"], "ppd": s["ppd"],
           "period": s["period"], "method": s["method"], "par": tot["par"], "lol": tot["lol"],
           "lol_low": tot["lol_low"], "lol_high": tot["lol_high"], "is_base": base is None,
           "par_change": 0.0, "lol_change": 0.0, "lol_change_pct": 0.0 if base is None else None,
           "changed": 0, "note": note}
    if base is not None:
        row["par_change"] = tot["par"] - base["par"]
        row["lol_change"] = tot["lol"] - base["lol"]
        if base["lol"] > 0:
            row["lol_change_pct"] = 100.0 * row["lol_change"] / base["lol"]
        row["changed"] = sum(1 for a, b in zip(per, base_per) if not _same_pr(a, b))
    return row


def _fmt_lol(x):
    if x is None:
        return "-"
    return "{:.3f}".format(x) if abs(x) < 10 else "{:,.1f}".format(x)


def _row_label(r):
    return "{} {}".format(r["what"].lower() if not r["is_base"] else "as run",
                          r["value_text"] if not r["is_base"] else "").strip()


def run_consequences(structs, cfg=None, base_conseq=None, *,
                     warning_shifts_hr=DEFAULT_WARNING_SHIFTS_HR,
                     persons_per_dwelling=DEFAULT_PPD_VALUES, population=True, severity=True,
                     incremental=None, progress_cb=None):
    """Sensitivity of the loss-of-life screening estimate (and PAR) to the
    consequence settings - one factor at a time around the run's own settings.

    structs: the evaluated structures of ONE breach scenario with its
    consequence fields (the governing snapshot restored - as after pipeline
    stage B). cfg: the pipeline settings (lol, warning_issued_hr, understanding,
    severity_method, population, high_severity, persons_per_dwelling,
    min_area_ft2). base_conseq: that scenario's consequences.totals (res["conseq"])
    - a note is made when the re-computed base differs. warning_shifts_hr: the
    warning is issued this much later (+) / earlier (-) than the setting;
    persons_per_dwelling: the values tried (the run's value is the base);
    population: also the other time of day; severity: also the other
    flood-severity method (Graham / RCEM). incremental: None (default) leaves
    out, like the run's totals, the structures a no-breach baseline marked as
    not caused by the breach (rec["incremental"] is False); False counts all.

    The structure records are only read. Returns {"kind": "consequences",
    "available", "rows", "envelope", "combined", "notes", "verdict", "base",
    "settings", "structures", "factor_ranges"}."""
    s0 = _base_settings(cfg)
    notes = []
    result = {"kind": KIND_CONSEQ, "available": False, "rows": [], "envelope": {},
              "combined": [], "notes": notes, "verdict": "", "base": None, "settings": s0,
              "structures": 0, "factor_ranges": {}}
    if not bool(_opt(cfg, "lol", True)):
        result["verdict"] = ("consequence sensitivity not made: the loss-of-life estimate is "
                             "switched off")
        return result
    items = [(rec, {k: rec.get(k) for k in _LL_KEYS}) for rec in structs
             if rec.get("habitable") == "Yes" and rec.get("max_depth")
             and rec["max_depth"] > 0
             and not (incremental is not False and rec.get("incremental") is False)]
    result["structures"] = len(items)
    if not items:
        result["verdict"] = ("consequence sensitivity not made: no flooded habitable "
                             "structure{}".format(" counted as a breach impact"
                                                  if any(r.get("incremental") is False
                                                         for r in structs) else ""))
        return result

    shifts = clean_values(warning_shifts_hr, drop=0.0)
    ppds = clean_values(persons_per_dwelling, positive=True, drop=s0["ppd"])
    other_period = "day" if s0["period"] == "night" else "night"
    other_method = "graham" if s0["method"] == "rcem" else "rcem"
    todo = [(F_WARN, x) for x in shifts] + [(F_PPD, x) for x in ppds]
    if population:
        todo.append((F_PERIOD, other_period))
    if severity:
        todo.append((F_SEV, other_method))

    base_tot, base_per = _evaluate_conseq(items, s0)
    base = _crow(F_BASE, None, "as run", s0, base_tot, base_per)
    base["note"] = "base (the run's settings)"
    rows = [base]
    ppd_cache = {}
    n_re = n_scaled = 0

    def persons_at(ppd):
        nonlocal n_re, n_scaled
        if ppd not in ppd_cache:
            ppd_cache[ppd], a, b = _ppd_persons(items, ppd, s0["ppd"], s0["min_area"])
            n_re, n_scaled = max(n_re, a), max(n_scaled, b)
        return ppd_cache[ppd]

    for k, (factor, x) in enumerate(todo):
        if progress_cb:
            progress_cb(100.0 * k / max(1, len(todo)),
                        "Consequence sensitivity: {}".format(FACTOR_LABELS[factor].lower()))
        s = dict(s0)
        persons, text, note = None, "", ""
        if factor == F_WARN:
            s["issued"] = s0["issued"] + x
            text = "{:+g} h (issued {})".format(x, _issued_text(s["issued"]))
            note = "earlier warning" if x < 0 else "later warning"
        elif factor == F_PPD:
            s["ppd"] = x
            persons = persons_at(x)
            text = "{:g}".format(x)
        elif factor == F_PERIOD:
            s["period"] = x
            text = x
        else:
            s["method"] = x
            text = SEVERITY_LABELS.get(x, x)
            if s0["high"]:
                note = "no effect: the near-instantaneous failure setting makes every " \
                       "location HIGH severity"
        tot, per = _evaluate_conseq(items, s, persons)
        rows.append(_crow(factor, x, text, s, tot, per, base_tot, base_per, note))

    oat = list(rows)
    varied = sorted({r["factor"] for r in oat if not r["is_base"]}, key=_FACTOR_ORDER.get)
    ranges = {}
    for f in varied:
        vals = [base["lol"]] + [r["lol"] for r in oat if r["factor"] == f]
        ranges[f] = (min(vals), max(vals))
    # the envelope over the one-factor rows (base included)
    lo_row = min(oat, key=lambda r: (r["lol"], not r["is_base"]))
    hi_row = max(oat, key=lambda r: (r["lol"], r["is_base"]))
    envelope = {"lol_min": lo_row["lol"], "lol_max": hi_row["lol"],
                "lol_min_row": _row_label(lo_row), "lol_max_row": _row_label(hi_row),
                "par_min": min(r["par"] for r in oat), "par_max": max(r["par"] for r in oat)}
    if not lo_row["is_base"]:
        lo_row["note"] = (lo_row["note"] + "; " if lo_row["note"] else "") + \
            "lowest estimate (one factor at a time)"
    if not hi_row["is_base"]:
        hi_row["note"] = (hi_row["note"] + "; " if hi_row["note"] else "") + \
            "highest estimate (one factor at a time)"

    # every factor at the value that gave its lowest / highest estimate (only
    # informative when more than one factor was varied)
    combined = []
    if len(varied) > 1:
        for label, pick in (("lowest", min), ("highest", max)):
            s = dict(s0)
            parts = []
            persons = None
            for f in varied:
                cand = [base] + [r for r in oat if r["factor"] == f]
                if pick is min:
                    r = min(cand, key=lambda r: (r["lol"], not r["is_base"]))
                else:
                    r = max(cand, key=lambda r: (r["lol"], r["is_base"]))
                if r["is_base"]:
                    continue
                if f == F_WARN:
                    s["issued"] = r["issued"]
                    parts.append("warning {:+g} h".format(r["value"]))
                elif f == F_PPD:
                    s["ppd"] = r["ppd"]
                    persons = persons_at(r["ppd"])
                    parts.append("{:g} per dwelling".format(r["ppd"]))
                elif f == F_PERIOD:
                    s["period"] = r["period"]
                    parts.append(r["period"])
                elif f == F_SEV:
                    s["method"] = r["method"]
                    parts.append(SEVERITY_LABELS.get(r["method"], r["method"]))
            tot, per = _evaluate_conseq(items, s, persons)
            row = _crow(F_COMB, label, "{} of each factor ({})".format(
                label, ", ".join(parts) if parts else "all as run"), s, tot, per,
                base_tot, base_per, "every factor at the value that gave its {} estimate"
                .format(label))
            combined.append(row)
        rows.extend(combined)

    # notes (what was varied and how)
    notes.append("values tried are screening choices by the engineer, not requirements "
                 "(defaults: warning issued 0.5 h earlier, 0.5 h and 1 h later; {} persons "
                 "per dwelling)".format(", ".join("{:g}".format(v) for v in DEFAULT_PPD_VALUES)))
    notes.append("depth, velocity (DV) and arrival are the run's own - no raster was read; "
                 "understanding of the warning: {}; the economic estimate does not depend on "
                 "these factors".format(s0["understanding"]))
    if ppds:
        notes.append("persons per dwelling: occupants re-derived with the plugin's own rule "
                     "(structures.reinterpret, on copies) for {} structure(s) without an NSI "
                     "population for both times of day (NSI populations and engineer-review "
                     "persons do not change); the TM 11 lives in jeopardy also follow the "
                     "persons count but are NOT re-classified here".format(n_re))
        if n_scaled:
            notes.append("{} structure(s) had no source data to re-derive their occupants: "
                         "their persons were scaled in proportion".format(n_scaled))
    if any(r.get("incremental") is False for r in structs) and incremental is not False:
        notes.append("no-breach baseline: only the structures counted as breach impacts "
                     "(as in the run's totals)")
    if s0["high"] and severity:
        notes.append("near-instantaneous failure setting: every location is HIGH severity - "
                     "the severity method has no effect")
    if base_conseq:
        for key, label in (("lol", "life-loss estimate"), ("par", "population at risk")):
            g = base_conseq.get(key)
            try:
                g = float(g)
            except (TypeError, ValueError):
                continue
            if abs(g - base[key]) > 1e-6 * max(1.0, abs(g)):
                notes.append("the {} re-computed here ({}) differs from the run's ({}): the "
                             "base of this table is the re-computed one".format(
                                 label, _fmt_lol(base[key]), _fmt_lol(g)))
    notes.append("the classification is not affected: the ND / TM 11 classes do not use "
                 "the life-loss estimate")

    verdict = "life-loss estimate {} as run (PAR {:,.1f})".format(_fmt_lol(base["lol"]),
                                                                   base["par"])
    if varied:
        verdict += "; one factor at a time {} to {} (lowest: {}; highest: {})".format(
            _fmt_lol(envelope["lol_min"]), _fmt_lol(envelope["lol_max"]),
            envelope["lol_min_row"], envelope["lol_max_row"])
        widest = max(varied, key=lambda f: (ranges[f][1] - ranges[f][0], -_FACTOR_ORDER[f]))
        verdict += "; most sensitive to {} ({} to {})".format(
            FACTOR_LABELS[widest].lower(), _fmt_lol(ranges[widest][0]),
            _fmt_lol(ranges[widest][1]))
        if combined:
            verdict += "; all factors combined {} to {}".format(
                _fmt_lol(combined[0]["lol"]), _fmt_lol(combined[1]["lol"]))
    else:
        verdict += "; no factor was varied"
    if progress_cb:
        progress_cb(100, "Consequence sensitivity done")
    result.update({"available": True, "rows": rows, "envelope": envelope,
                   "combined": combined, "verdict": verdict, "base": base,
                   "factor_ranges": ranges})
    return result


def conseq_summary_lines(result):
    """Lines for the summary text (a single line when not made)."""
    if not result:
        return []
    if not result.get("available"):
        return [result.get("verdict") or "consequence sensitivity not made"]
    out = ["Consequence sensitivity (screening; values chosen by the engineer, not a "
           "requirement; {} flooded habitable structure(s)): {}".format(
               result.get("structures", 0), result["verdict"])]
    out.append("{:<26} {:<46} {:>9} {:>9} {:>9}".format("Varied", "Value", "PAR", "LOL",
                                                        "change"))
    for r in result["rows"]:
        ch = "-" if r["is_base"] else ("{:+.3f}".format(r["lol_change"])
                                       if abs(r["lol_change"]) < 10
                                       else "{:+,.1f}".format(r["lol_change"]))
        out.append("{:<26} {:<46} {:>9} {:>9} {:>9}".format(
            r["what"][:26], str(r["value_text"])[:46], "{:,.1f}".format(r["par"]),
            _fmt_lol(r["lol"]), ch))
    for n in result.get("notes") or []:
        out.append("Note: " + n)
    out.append("Table: QA/{}".format(CSV_CONSEQ))
    return out


def conseq_rows_table(result):
    """[dict keyed by CONSEQ_HEAD] for QA/Sensitivity_Consequences.csv."""
    out = []
    for r in (result or {}).get("rows") or []:
        pct = r.get("lol_change_pct")
        out.append({"Factor": r["what"], "Value": r["value_text"],
                    "Warning_Issued_hr": round(r["issued"], 3),
                    "Persons_per_Dwelling": "{:g}".format(r["ppd"]),
                    "Population": r["period"],
                    "Severity_Method": SEVERITY_LABELS.get(r["method"], r["method"]),
                    "PAR": round(r["par"], 2), "LOL": round(r["lol"], 4),
                    "LOL_Low": round(r["lol_low"], 4), "LOL_High": round(r["lol_high"], 4),
                    "PAR_Change": round(r["par_change"], 2),
                    "LOL_Change": round(r["lol_change"], 4),
                    "LOL_Change_pct": "" if pct is None else round(pct, 1),
                    "Structures_Changed": r["changed"], "Note": r["note"]})
    return out


def write_conseq_csv(folder, result):
    """QA/Sensitivity_Consequences.csv ("folder" is the QA folder); [path], or []
    when the sensitivity was not made."""
    if not result or not result.get("available"):
        return []
    os.makedirs(folder, exist_ok=True)
    return [_write(os.path.join(folder, CSV_CONSEQ), CONSEQ_HEAD, conseq_rows_table(result))]
