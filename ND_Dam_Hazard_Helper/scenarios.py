# -*- coding: utf-8 -*-
"""Breach scenarios and the no-breach baseline for incremental analysis.

FEMA P-333 bases hazard potential on INCREMENTAL consequences: "under the
same conditions (e.g., flood ...), the difference in impacts that would
occur due to failure or mis-operation of the dam over those that would
have occurred without failure". A breach scenario may name a baseline
(the same flood without failure); consequences present without the breach
are then not charged to the dam. Several breach scenarios (sunny-day,
PMF, ...) are classified separately and the most severe one governs.

v2.9: a scenario can come from a HEC-RAS plan results file (.p##.hdf) in
place of rasters (Scenario.from_plan, or a plan path in the depth column of
the Setup tab / the scenario table). resolve() - called by
resolve_scenarios() before the run, or by load_layers() at the latest -
converts it through the plan cache (rasimport.plan_to_rasters_cached; worker
thread safe) and the scenario then is a raster scenario like any other:
depth, and velocity / arrival unless the user gave rasters for them.
"""

import os
import re

import numpy as np
from qgis.core import QgsRasterLayer, QgsCoordinateTransform

from .constants import (CLASS_HIGH, CLASS_JUDGMENT, DEFAULT_INCREMENTAL_DEPTH_FT,
                        vehicle_curve_available)
from .helpers import transform_xy, classify_values, curve_pair

ROLE_BREACH, ROLE_BASELINE = "breach", "baseline"


def _is_plan_path(path):
    """A HEC-RAS plan results file name (<project>.p##.hdf)? Name only."""
    from .rasimport import PLAN_RE
    return bool(path) and isinstance(path, str) and bool(PLAN_RE.search(os.path.basename(
        path.strip())))


def _same_file(a, b):
    if not a or not b:
        return False
    return os.path.normcase(os.path.abspath(a.strip())) == \
        os.path.normcase(os.path.abspath(b.strip()))


class Scenario(object):
    """One set of hydraulic results. Paths only - rasters are opened when
    the scenario is run."""

    # v2.9: HEC-RAS plan the rasters are made from (class defaults: scenarios
    # saved by earlier versions - evaluation cache - load unchanged)
    plan = None                 # the .p##.hdf
    plan_settings = None        # rasimport.plan_settings() dict
    cache_root = None           # where the conversion is cached
    plan_fields = ()            # of "velocity", "arrival": taken from the plan
    plan_rasters = None         # after resolve(): what the conversion gave

    def __init__(self, name, depth, velocity, arrival, dv=None,
                 role=ROLE_BREACH, baseline=None, source="", velocity_units_note="",
                 plan=None, plan_settings=None, cache_root=None):
        self.name = (name or "Scenario").strip()
        self.depth, self.velocity, self.arrival = depth, velocity, arrival
        self.dv = dv or None
        self.role = role if role in (ROLE_BREACH, ROLE_BASELINE) else ROLE_BREACH
        self.baseline = baseline or None
        self.source = source or ""
        self.velocity_units_note = velocity_units_note
        # v2.9: a plan named here or a plan file in the depth column
        if not plan and _is_plan_path(depth):
            plan = depth.strip()
        if plan:
            self._set_plan(plan, plan_settings, cache_root)

    def _set_plan(self, plan, settings, cache_root):
        from .rasimport import plan_settings as normalise, RasImportError
        self.plan = plan
        try:
            self.plan_settings = normalise(settings)
        except RasImportError:
            self.plan_settings = dict(settings or {})        # reported by missing()
        self.cache_root = cache_root or None
        self.depth = plan                      # until resolve(): the plan file
        # velocity / arrival from the plan unless the user gave other rasters
        self.plan_fields = [f for f in ("velocity", "arrival")
                            if not getattr(self, f) or _same_file(getattr(self, f), plan)]
        for f in self.plan_fields:
            setattr(self, f, None)
        if self.dv and _same_file(self.dv, plan):
            self.dv = None                     # the plan import has no D x V

    @classmethod
    def from_plan(cls, name, hdf_path, settings=None, cache_root=None, velocity=None,
                  arrival=None, dv=None, role=ROLE_BREACH, baseline=None):
        """v2.9: a scenario made from a HEC-RAS plan results file. settings:
        rasimport.PLAN_DEFAULTS keys (dem, breach_hr, arrival_ft, cell_ft);
        velocity / arrival rasters given here are used instead of the plan's."""
        return cls(name, hdf_path, velocity, arrival, dv=dv, role=role, baseline=baseline,
                   plan=hdf_path, plan_settings=settings, cache_root=cache_root)

    @property
    def plan_pending(self):
        """True while a plan scenario's rasters are not made yet."""
        return bool(self.plan) and not self.plan_rasters

    def resolve(self, progress=None, log=None, cache_root=None):
        """v2.9: a plan scenario's rasters, converted through the plan cache
        (re-used when the plan and the options are unchanged). No project or
        widget access - call it in the worker thread. Nothing to do for a
        raster scenario or when done already. cache_root: used when the
        scenario has none (else a temp folder). Returns the conversion
        result (rasimport.plan_to_rasters_cached) or None."""
        if not self.plan_pending:
            return None
        from . import rasimport as RI
        for f in ("velocity", "arrival"):
            v = getattr(self, f)
            if v and _is_plan_path(v) and not _same_file(v, self.plan):
                raise RI.RasImportError("{}: the {} column holds another HEC-RAS plan ({}) - "
                                        "one plan per scenario".format(self.name, f,
                                                                        os.path.basename(v)))
        root = self.cache_root or cache_root or RI.default_cache_root(None)
        res = RI.plan_to_rasters_cached(self.plan, root, self.plan_settings, progress=progress,
                                        log=log)
        self.depth = res["depth"]
        if "velocity" in self.plan_fields:
            self.velocity = res.get("vel")
        if "arrival" in self.plan_fields:
            self.arrival = res.get("arr")
        # JSON-safe (the evaluation cache keeps scenarios as JSON)
        self.plan_rasters = {k: res.get(k) for k in ("depth", "vel", "arr", "wse", "peak",
                                                     "folder", "key", "cached")}
        self.plan_rasters["notes"] = list(res.get("notes") or [])
        return res

    @property
    def slug(self):
        return re.sub(r"[^A-Za-z0-9]+", "_", self.name).strip("_")[:40] or "Scenario"

    def to_dict(self):
        d = {"name": self.name, "depth": self.depth, "velocity": self.velocity,
             "arrival": self.arrival, "dv": self.dv, "role": self.role, "baseline": self.baseline, "source": self.source}
        if self.plan:
            d.update({"plan": self.plan, "plan_settings": self.plan_settings,
                      "cache_root": self.cache_root, "plan_fields": list(self.plan_fields)})
        return d

    @classmethod
    def from_dict(cls, d):
        if d.get("plan"):
            # the columns the plan fills are filled again when it is resolved
            fields = d.get("plan_fields")
            fields = ("velocity", "arrival") if fields is None else fields
            return cls(d.get("name"), d["plan"],
                       None if "velocity" in fields else d.get("velocity"),
                       None if "arrival" in fields else d.get("arrival"),
                       d.get("dv"), d.get("role", ROLE_BREACH), d.get("baseline"),
                       d.get("source", ""), plan=d["plan"], plan_settings=d.get("plan_settings"),
                       cache_root=d.get("cache_root"))
        return cls(d.get("name"), d.get("depth"), d.get("velocity"), d.get("arrival"),
                   d.get("dv"), d.get("role", ROLE_BREACH), d.get("baseline"),
                   d.get("source", ""))

    def _plan_problems(self):
        from . import rasimport as RI
        name = os.path.basename(self.plan)
        if not os.path.isfile(self.plan):
            return ["{}: HEC-RAS plan results file not found ({})".format(self.name, self.plan)]
        if not RI.is_hdf5(self.plan):
            return ["{}: {} is not a HEC-RAS plan results file (HDF5)".format(self.name, name)]
        out = []
        try:
            s = RI.plan_settings(self.plan_settings)
            if s["dem"] and not os.path.isfile(s["dem"]):
                out.append("{}: terrain raster for plan {} not found ({})".format(
                    self.name, name, s["dem"]))
        except RI.RasImportError as e:
            out.append("{}: {}".format(self.name, e))
        for f in ("velocity", "arrival"):
            v = getattr(self, f)
            if v and _is_plan_path(v) and not _same_file(v, self.plan):
                out.append("{}: the {} column holds another HEC-RAS plan ({}) - one plan per "
                           "scenario".format(self.name, f, os.path.basename(v)))
        return out

    def missing(self):
        """Human-readable problems (empty list = usable)."""
        out = []
        if self.plan_pending:
            out.extend(self._plan_problems())
        elif not self.depth or not os.path.isfile(self.depth):
            out.append("{}: depth raster missing".format(self.name))
        if self.velocity and not os.path.isfile(self.velocity):
            out.append("{}: velocity raster not found".format(self.name))
        if self.arrival and not os.path.isfile(self.arrival):
            out.append("{}: arrival raster not found".format(self.name))
        if self.dv and not os.path.isfile(self.dv):
            out.append("{}: D x V raster not found".format(self.name))
        return out

    def load_layers(self):
        if self.plan_pending:
            self.resolve()             # v2.9: normally done by resolve_scenarios() already
        names = {"depth": "Depth", "vel": "Velocity", "arr": "Arrival", "dv": "DxV"}
        out = {}
        for key, path in (("depth", self.depth), ("vel", self.velocity),
                          ("arr", self.arrival), ("dv", self.dv)):
            out[key] = QgsRasterLayer(path, "{} {}".format(names[key], self.name)) \
                if path else None
        return out


def order_for_run(scenarios):
    """Baselines first (they are needed by the breaches), then breaches;
    a breach whose baseline name does not exist loses the link."""
    names = {s.name for s in scenarios if s.role == ROLE_BASELINE}
    for s in scenarios:
        if s.role == ROLE_BREACH and s.baseline and s.baseline not in names:
            s.baseline = None
    return [s for s in scenarios if s.role == ROLE_BASELINE] + \
        [s for s in scenarios if s.role == ROLE_BREACH]


def resolve_scenarios(scenarios, progress=None, log=None, cache_root=None):
    """v2.9: make the rasters of every plan scenario (through the plan cache)
    so all scenarios are raster scenarios. Worker thread safe (no project,
    no widgets); progress(pct 0-100 over all plans, msg), log(msg);
    CancelledByUser from progress stops it. Call it before the scenarios'
    missing() / the evaluation key. Returns the number of plan scenarios."""
    todo = [s for s in scenarios or [] if s.plan_pending]
    for i, sc in enumerate(todo):
        def sub(pct, msg, i=i, sc=sc):
            progress(100.0 * (i + min(max(float(pct), 0.0), 100.0) / 100.0) / len(todo),
                     "{}: {}".format(sc.name, msg))
        sc.resolve(sub if progress else None, log, cache_root)
    return len(todo)


def raster_reuse_problems(scenarios, warnings=None):
    """v2.9.1: the same raster FILE given for two different things - a
    breach's no-breach baseline that is the breach flood itself (nothing would
    be charged to the dam: every structure would already be flooded without
    the breach), or one file given as depth and as velocity / arrival / D x V.
    Such a run would give a wrong class without an error, so it is stopped.
    A scenario still waiting for its HEC-RAS plan conversion (all its fields
    are the .hdf) is compared by its plan file only. Returns messages (they
    stop the run). A baseline that has its own depth raster but shares the
    breach's velocity or arrival raster is not stopped: it goes to `warnings`
    (a list), because the baseline is then read more severe than it is and
    less is charged to the dam."""
    out = []
    by_name = {s.name: s for s in scenarios or []}
    labels = (("depth", "depth"), ("velocity", "velocity"), ("arrival", "arrival"),
              ("dv", "D x V"))
    for sc in scenarios or []:
        if not getattr(sc, "plan_pending", False):
            seen = {}
            for attr, label in labels:
                p = getattr(sc, attr, None)
                if not p:
                    continue
                for other_label, other in seen.items():
                    if _same_file(p, other):
                        out.append("{}: the same raster is given as the {} and the {} ({})"
                                   .format(sc.name, other_label, label, p))
                seen[label] = p
        if sc.role != ROLE_BREACH or not sc.baseline:
            continue
        base = by_name.get(sc.baseline)
        if base is None or base is sc:
            continue
        if getattr(sc, "plan", None) and getattr(base, "plan", None) and \
                _same_file(sc.plan, base.plan):
            out.append("{}: the no-breach baseline '{}' is the same HEC-RAS plan as the "
                       "breach ({}) - a flood cannot be its own baseline".format(
                           sc.name, base.name, sc.plan))
            continue
        if sc.depth and base.depth and _same_file(sc.depth, base.depth):
            out.append("{}: the no-breach baseline '{}' uses the same depth raster as the "
                       "breach ({}) - a flood cannot be its own baseline; nothing would be "
                       "charged to the dam".format(sc.name, base.name, sc.depth))
            continue
        for attr, label in labels[1:3]:
            a, b = getattr(sc, attr, None), getattr(base, attr, None)
            if a and b and _same_file(a, b) and warnings is not None:
                warnings.append("{}: the no-breach baseline '{}' uses the breach's {} raster "
                                "({}) - the baseline is read with the breach {}, so less may "
                                "be charged to the dam; give the baseline its own {} raster"
                                .format(sc.name, base.name, label, a, label, label))
    return out


# ------------------------------------------------------------- incremental --
EVAL_KEYS = ("max_depth", "max_vel", "arrival", "zone_found", "zone_mobile", "zone",
             "zone_mm", "vel_missing", "dv_max", "margin_high", "margin_judg", "near",
             "zone_note", "outcome", "region", "loc", "near_raised",
             # v2.6: evaluation area and the cells read (Structure_Evaluation_Areas),
             # per-curve results for apply_type
             "reach_ft", "region_kind", "n_cells", "sampling", "cells_xy", "_types",
             # v2.8: per-cell depth / velocity kept for the sensitivity table
             "_dv")


def snapshot(structs):
    return {rec["id"]: {k: rec.get(k) for k in EVAL_KEYS} for rec in structs}


def restore(structs, snap):
    for rec in structs:
        rec.update(snap.get(rec["id"], {}))


def _outcome_rank(outcome):
    from .structures import OUT_DRY, OUT_ECONOMIC, OUT_JUDGMENT, OUT_PROBABLE
    return {OUT_DRY: 0, OUT_ECONOMIC: 1, OUT_JUDGMENT: 2, OUT_PROBABLE: 3}.get(outcome, 0)


def _zone_outcome_rank(zone):
    """Outcome rank a (wet) ACER zone leads to; None = no zone (not comparable)."""
    if zone is None:
        return None
    return 3 if zone == CLASS_HIGH else 2 if zone == CLASS_JUDGMENT else 1


def structure_increment(rec, base, min_depth_ft=DEFAULT_INCREMENTAL_DEPTH_FT,
                        same_method=True, one_d=False):
    """(incremental?, note) of the breach result in `rec` against the
    baseline result `base` (a snapshot entry). Outcomes and zones are
    compared on the same basis on both sides: the scenario's own when breach
    and baseline used the same ACER method, otherwise max depth with max
    velocity for both (a side without a velocity raster has no such zone and
    is compared by depth only). With the 1D criterion the outcome follows
    from depth alone and zones are not compared."""
    d = rec.get("max_depth")
    if d is None or d <= 0:
        return False, ""
    base = base or {}
    bd = base.get("max_depth")
    if bd is None or bd <= 0:
        return True, "dry without breach"
    if same_method or one_d:
        ro, bo = _outcome_rank(rec.get("outcome")), _outcome_rank(base.get("outcome"))
        ro_txt, bo_txt = rec.get("outcome"), base.get("outcome")
    else:
        ro, bo = _zone_outcome_rank(rec.get("zone_mm")), _zone_outcome_rank(base.get("zone_mm"))
        names = {1: "inundated", 2: "judgment zone", 3: "probable loss of life"}
        ro_txt = "{} at max/max".format(names.get(ro))
        bo_txt = "{} at max/max".format(names.get(bo))
    if ro is not None and bo is not None and ro > bo:
        return True, "worse outcome with breach ({} -> {})".format(bo_txt, ro_txt)
    if not one_d:
        key = "zone" if same_method else "zone_mm"
        rz, bz = rec.get(key), base.get(key)
        if rz is not None and bz is not None and rz < bz:
            return True, "ACER zone worse with breach"
    if d - bd >= min_depth_ft:
        return True, "+{:.1f} ft with breach".format(d - bd)
    return False, "flooded without breach ({:.1f} ft)".format(bd)


def mark_structures_incremental(structs, base_snap, min_depth_ft=DEFAULT_INCREMENTAL_DEPTH_FT,
                                same_method=True, one_d=False):
    n = 0
    for rec in structs:
        base = base_snap.get(rec["id"], {})
        inc, note = structure_increment(rec, base, min_depth_ft, same_method, one_d)
        rec["incremental"] = inc if rec.get("max_depth") else None
        rec["incr_note"] = note
        rec["base_depth"] = base.get("max_depth")
        rec["base_zone"] = base.get("zone")
        # access: only a cut the breach causes is charged to the dam
        if rec.get("cut_off"):
            rec["cut_incremental"] = not base.get("cut_off")
        else:
            rec["cut_incremental"] = None
        n += 1 if inc else 0
    return n


def line_baseline_depth(results, base_depth_sampler, analysis_crs, transform_ctx,
                        base_vel_sampler=None):
    """Baseline max depth (and, with a velocity sampler, the baseline
    vehicle zone from max depth with max velocity) along each flooded road /
    rail stretch."""
    def xform(sampler):
        if sampler is None or sampler.crs is None:
            return None
        return QgsCoordinateTransform(analysis_crs, sampler.crs, transform_ctx)
    xf, xv = xform(base_depth_sampler), xform(base_vel_sampler)
    veh = vehicle_curve_available()
    curves = curve_pair("vehicle") if veh else None
    for r in results:
        r["base_vzone_mm"] = None
        if base_depth_sampler is None or not base_depth_sampler.ok or "xs" not in r:
            r["base_depth"] = None
            continue
        x, y = transform_xy(r["xs"], r["ys"], xf)
        d = base_depth_sampler.values_at(x, y)
        wet = np.isfinite(d) & (d > 0)
        r["base_depth"] = float(d[wet].max()) if wet.any() else 0.0
        # without a baseline velocity raster there is no comparable zone
        if veh and wet.any() and base_vel_sampler is not None and base_vel_sampler.ok:
            vx, vy = transform_xy(r["xs"], r["ys"], xv)
            v = base_vel_sampler.values_at(vx, vy)
            v = np.where(np.isfinite(v), v, 0.0)
            r["base_vzone_mm"] = int(classify_values(d[wet], v[wet], *curves).min())


def _own_vzone_mm(r):
    if not vehicle_curve_available() or "depth" not in r:
        return None
    d = np.asarray(r["depth"], dtype=np.float64)
    v = np.asarray(r.get("vel", np.zeros(d.shape)), dtype=np.float64)
    ok = np.isfinite(d) & (d > 0)
    if not ok.any():
        return None
    v = np.where(np.isfinite(v), v, 0.0)
    return int(classify_values(d[ok], v[ok], *curve_pair("vehicle")).min())


def mark_lines_incremental(results, min_depth_ft=DEFAULT_INCREMENTAL_DEPTH_FT,
                           own_velocity=True):
    """Road / rail stretches charged to the breach: dry without it, a worse
    passenger-vehicle zone (max depth with max velocity on both sides; only
    when both scenarios have a velocity raster), or at least `min_depth_ft`
    deeper."""
    n = 0
    for r in results:
        bd = r.get("base_depth")
        own_vz = _own_vzone_mm(r) if own_velocity else None
        base_vz = r.get("base_vzone_mm")
        if bd is None or bd <= 0:
            inc, note = True, "dry without breach"
        elif own_vz is not None and base_vz is not None and own_vz < base_vz:
            inc, note = True, "vehicle zone worse with breach"
        elif r["max_depth"] - bd >= min_depth_ft:
            inc, note = True, "+{:.1f} ft with breach".format(r["max_depth"] - bd)
        else:
            inc, note = False, "overtopped without breach ({:.1f} ft)".format(bd)
        r["incremental"], r["incr_note"] = inc, note
        n += 1 if inc else 0
    return n


# consequence fields cleared for impacts that also occur without the breach
_CLEARED = {"zone": None, "zone_found": None, "zone_mobile": None, "near": "",
            "lol": None, "lol_low": None, "lol_high": None, "par": None,
            "fatality_rate": None, "severity": None, "damage_usd": None,
            "damage_nonres_usd": None, "par_night": None, "par_day": None,
            "lol_night": None, "lol_day": None,
            "val_struct": None, "val_cont": None, "ff_depth": None}


def incremental_view(structs, roads, rails):
    """Copies in which consequences that also occur without the breach are
    removed, for classify_nd / classify_tm11 and the totals."""
    from .structures import OUT_DRY
    s2 = []
    for rec in structs:
        c = rec
        if rec.get("incremental") is False:
            c = dict(rec)
            c.update(_CLEARED)
            c["outcome"] = OUT_DRY
        if rec.get("cut_incremental") is False:
            c = dict(c) if c is rec else c
            c["cut_off"] = False
        s2.append(c)
    r2 = [r for r in roads if r.get("incremental") is not False]
    l2 = [r for r in rails if r.get("incremental") is not False]
    return s2, r2, l2


# ------------------------------------------------ the dialog's scenario table --
ROW_KEYS = ("name", "role", "baseline", "depth", "velocity", "arrival", "dv")
MAIN_NAME = "Breach"


def clean_rows(rows):
    """Rows of the scenario table with every key present, text stripped and
    rows without any entry dropped. v2.9: a row's own HEC-RAS plan import
    options ("plan_settings" dict) are kept."""
    out = []
    for r in rows or []:
        row = {k: str((r or {}).get(k) or "").strip() for k in ROW_KEYS}
        if row["role"] not in (ROLE_BREACH, ROLE_BASELINE):
            row["role"] = ROLE_BREACH
        if isinstance((r or {}).get("plan_settings"), dict):
            row["plan_settings"] = dict(r["plan_settings"])
        if any(row[k] for k in ("name", "depth", "velocity", "arrival", "dv")):
            out.append(row)
    return out


def _unique(name, taken, slugs):
    base, n = name, 1
    while name.lower() in taken or Scenario(name, None, None, None).slug.lower() in slugs:
        n += 1
        name = "{} ({})".format(base, n)
    return name


def rows_to_scenarios(main, rows, plan_settings=None, cache_root=None):
    """(scenarios, problems, notes) from the Setup-tab rasters (`main`: dict
    with name, depth, velocity, arrival, dv, baseline) and the table rows.
    The main scenario is always a breach. Names are made unique (also as
    file names). Problems (a row without a depth raster or with a missing
    file, a scenario without a breach) stop the run; notes are things fixed
    or dropped on the way.
    v2.9: a HEC-RAS plan results file (.p##.hdf) in a depth column makes a
    plan scenario (velocity / arrival from the plan unless other rasters are
    given; the plan's path there means the same). plan_settings: the import
    options (rasimport.PLAN_DEFAULTS; the HEC-RAS plan tab's) - a row or
    `main` may carry its own "plan_settings"; cache_root: where conversions
    are cached (rasimport.default_cache_root(output folder))."""
    problems, notes = [], []
    taken, slugs = set(), set()
    scenarios = []

    def add(name, depth, velocity, arrival, dv, role, baseline, settings=None):
        name = _unique(name, taken, slugs)
        sc = Scenario(name, depth, velocity or None, arrival or None, dv=dv or None,
                      role=role, baseline=baseline or None,
                      plan_settings=settings if settings is not None else plan_settings,
                      cache_root=cache_root)
        taken.add(name.lower())
        slugs.add(sc.slug.lower())
        scenarios.append(sc)
        if sc.plan:
            notes.append("Scenario '{}': HEC-RAS plan {} - its depth{} raster(s) are made when "
                         "the run starts (re-used while the plan and the import options are "
                         "unchanged)".format(name, os.path.basename(sc.plan), "".join(
                             " / " + f for f in sc.plan_fields)))
        return sc
    main = main or {}
    main_sc = add((main.get("name") or MAIN_NAME).strip() or MAIN_NAME, main.get("depth"),
                  main.get("velocity"), main.get("arrival"), main.get("dv"), ROLE_BREACH,
                  (main.get("baseline") or "").strip(), main.get("plan_settings"))
    rows = clean_rows(rows)
    wanted = {id(main_sc): main_sc.baseline}
    for i, r in enumerate(rows, 1):
        label = r["name"] or "row {}".format(i)
        if not r["depth"]:
            problems.append("Scenario '{}': no depth raster".format(label))
            continue
        sc = add(r["name"] or "Scenario {}".format(i), r["depth"], r["velocity"], r["arrival"],
                 r["dv"], r["role"], r["baseline"] if r["role"] == ROLE_BREACH else "",
                 r.get("plan_settings"))
        if sc.name != r["name"] and r["name"]:
            notes.append("Scenario name '{}' is used by another scenario - renamed '{}'".format(
                r["name"], sc.name))
        wanted[id(sc)] = sc.baseline
    for sc in scenarios:
        problems.extend(sc.missing())
    baselines = {s.name for s in scenarios if s.role == ROLE_BASELINE}
    for sc in scenarios:
        want = wanted.get(id(sc))
        if want and sc.role == ROLE_BREACH and want not in baselines:
            # the table stores names; a renamed baseline keeps working when
            # its case differs
            hit = next((b for b in baselines if b.lower() == want.lower()), None)
            if hit:
                sc.baseline = hit
            else:
                sc.baseline = None
                notes.append("Scenario '{}': baseline '{}' is not in the table - run without "
                             "one".format(sc.name, want))
    if not [s for s in scenarios if s.role == ROLE_BREACH]:
        problems.append("No breach scenario")
    return scenarios, problems, notes


# ------------------------------------------------------ side-by-side results --
COMPARE_COLS = ["Scenario", "Baseline", "ND class", "TM 11 class", "Lives in jeopardy",
                "Habitable structures flooded", "Population at risk", "Est. fatalities",
                "Roads", "Rail"]


def comparison_rows(results, governing=None):
    """One row per breach scenario of a run's results (`res["results"]`), in
    the counting the classification used (with a baseline: only what the
    breach adds). Returns [dict keyed by COMPARE_COLS] plus a "governs" flag."""
    out = []
    for name, r in (results or {}).items():
        nd, tm = r.get("nd"), r.get("tm11")
        if not nd or not tm:
            continue
        views = r.get("views") or ([], [], [])
        s_view, r_view, l_view = views
        c = r.get("conseq") or {}
        inc = r.get("incremental") or {}
        wet_hab = sum(1 for s in s_view if s.get("habitable") == "Yes"
                      and (s.get("max_depth") or 0) > 0)
        out.append({"Scenario": name, "Baseline": inc.get("baseline") or "-",
                    "ND class": nd["cls"], "TM 11 class": tm["cls"],
                    "Lives in jeopardy": "{:.1f}".format(tm["lij"]),
                    "Habitable structures flooded": str(wet_hab),
                    "Population at risk": "{:.0f}".format(c.get("par") or 0),
                    "Est. fatalities": "{:.1f}".format(c.get("lol") or 0),
                    "Roads": str(len(r_view)), "Rail": str(len(l_view)),
                    "governs": name == governing})
    return out
