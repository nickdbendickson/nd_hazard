"""Runs one dam from breach hydrograph to result rasters.

1. Breach hydrograph (ND 5.1.1): sunny day with piping / overtopping
   parameters or TR-60, or the flood-inflow scenario (PMF inflow routed
   through the reservoir with and without a breach).
2. Initial domain: a steepest-descent trace from the breach inflow point,
   buffered on both sides.
3. LISFLOOD-FP over that domain. Wherever the flood leaves an edge of the
   domain deeper than the tolerance, that edge moves out and the model runs
   again (ND 5.1.1(f): extend downstream until further impacts would not
   change the hazard classification). With receptor layers the extension stops
   where no structure, road or rail lies in the flood's path. v3.2.0, optional
   (st.class_stop, stoprule.py): an edge is also accepted where the breach
   flood's depth, or its rise over the no-breach baseline, has died out along
   the flow path near it (ND Dam Hazard's termination point) and no receptor
   lies beyond. The duration is also extended if the flood is still advancing
   at the end.
4. Flood scenario: the no-breach baseline runs on the final domain, and the
   incremental depth and water-surface rise are worked out.
5. Optionally ANUGA on a mesh around the LISFLOOD-FP flood extent, refined
   near the dam and around culverts. Its results become the final rasters.
6. Rasters in the dam folder, in feet, ft/s and hours, named so the ND
   hazard classification plugin finds them: "Depth (Max).tif",
   "Velocity (Max).tif", "Arrival Time.tif", "WSE (Max).tif".
7. Products: flood polygons, arrival contours, receptor list, an EAP map preview
   (v3.2.0: the EAP map is ND Dam Hazard's map book; st.eap_map) and a
   self-contained HTML report.
8. v3.2.0 (st.dam_record, set by the rundam / batch tools): the values the run
   used and a "modelled" status go into <dam folder>/dam.json (damjson.py);
   another tool's value is never overwritten, a conflict is a report warning.

The GIS parts (reading the DEM, writing GeoTIFFs) come in through the
`terrain` and `writer` objects so this module runs without QGIS."""
import datetime
import hashlib
import json
import math
import os
import time

import numpy as np

from . import breach as brmod
from . import conditioning as cond
from . import crest as crestmod
from . import flood as floodmod
from . import impacts as impmod
from . import structures as structmod
from . import terrain
from . import lisflood as lfmod
from . import anuga_model as anmod
from . import stoprule
from .grid import Grid
from .units import FT, MILE, ACFT, CFS

VERSION = "3.12.0"

TIER_LISFLOOD = "lisflood"
TIER_BOTH = "lisflood+anuga"
TIERS = (TIER_LISFLOOD, TIER_BOTH)

SCENARIO_SUNNY = "sunny"
SCENARIO_FLOOD = "flood"

MODEL_FOLDER = "_breach_model"
FINAL_NAMES = {"depth": "Depth (Max).tif", "velocity": "Velocity (Max).tif",
               "arrival": "Arrival Time.tif", "wse": "WSE (Max).tif"}
BASELINE_NAMES = {k: "Baseline " + v for k, v in FINAL_NAMES.items()}
# written by the ND 5.2 classification tool from the final rasters; removed when they change
STALE_ON_RERUN = ("Hazard Classification (ND 5.2).md", "Hazard Classification (ND 5.2).json",
                  "Hazard Classification Findings.csv", "Hazard Classification Findings.geojson",
                  "ND Fig 5.1 Houses.svg", "ND Fig 5.2 Mobile Homes.svg")
INCREMENT_NAMES = {"depth": "Incremental Depth (Max).tif", "wse": "Incremental WSE Rise (Max).tif"}
QA_NAMES = {"depth": "hmax_ft.tif", "velocity": "vmax_fps.tif", "arrival": "arr_hr.tif",
            "wse": "wsemax_ft.tif"}
WET_M = 0.003          # a cell counts as flooded above 0.01 ft
MAX_SIM_S = 336 * 3600.0   # longest simulation the automatic duration will ask for
STATE_FILE = "state.json"


class RunSettings:
    def __init__(self, **kw):
        self.tier = TIER_LISFLOOD
        self.breach_method = brmod.METHOD_PIPING
        self.overrides = brmod.BreachOverrides()
        self.user_hydrograph = None
        self.cell_size_m = 10.0
        self.planning_cell_m = 30.0
        self.manning_n = 0.06
        self.manning_scale = 1.0         # multiplies the Manning raster too (sensitivity)
        self.arrival_depth_ft = 1.0
        self.edge_tolerance_ft = 1.0
        self.initial_length_mi = 5.0
        self.half_width_mi = 1.0
        self.expand_step_mi = 3.0
        self.max_extent_mi = 60.0
        self.max_iterations = 6
        self.sim_time_hr = None          # None = automatic
        self.sim_max_hr = None           # None = the built-in cap; a number is the longest clock
        self.front_speed_mps = 0.5       # for the automatic duration
        self.lisflood_exe = ""
        self.anuga_python = ""
        self.threads = 0
        self.anuga_fine_area_m2 = 100.0
        self.anuga_coarse_area_m2 = 1000.0
        self.anuga_near_field_mi = 1.0
        self.anuga_buffer_m = 500.0
        self.anuga_algorithm = "DE0"
        self.keep_model_files = True
        # scenario
        self.scenario = SCENARIO_SUNNY
        self.flood = None                # flood.FloodInputs for the flood scenario
        self.baseline = True             # flood scenario: also run without the breach
        self.incr_thresholds_ft = (1.0, 2.0, 3.0)
        # things in the flow path
        self.receptors = None            # impacts.Receptors
        self.impact_look_ahead_mi = 5.0
        self.structures = []             # structures.Structure
        self.invert_tol_m = 3.0 * 0.3048  # 3.5: inventory inverts within this of the LiDAR bed are used
        self.edits = None                # conditioning.Edits (channels, ponds, notches)
        self.refine_lines = []           # polylines used as ANUGA mesh edges
        # outputs and run control
        self.products = True
        self.arrival_contours_hr = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0)
        self.resume = False
        self.gauges = []                 # [{"name", "xy"}]: discharge past these points
        self.gauge_massint_s = 60.0
        self.fixed_plan = None           # dict from a previous run: reuse inflow point and domain
        self.write_final = True          # write the rasters into the dam folder
        self.ambient_cfs = 0.0           # steady stream flow when the event starts (added at the inflow)
        self.spinup_hr = 2.0             # ambient flow runs this long before the breach hydrograph
        self.input_files = {}            # {label: path} of the inputs, recorded in the run manifest
        self.breach_xy = None            # point on the crest where the breach starts (default: dam_xy)
        self.inflow_width_m = None       # width the inflow is spread over (default: the breach width)
        self.user_hydrograph_obj = None  # breach.Hydrograph used as given (nested runs)
        self.user_baseline_obj = None    # its no-breach counterpart, for the incremental rasters
        self.snapshot_frames = 0         # >0: depth snapshots for an animation (about this many frames)
        self.animation = False           # render GIF / MP4 / frame files from the snapshots
        self.keep_arrays = False         # keep the result arrays on the RunResult
        # v3.2.0: stop extending the domain when the class stops changing (stoprule.py);
        # off by default. The values are ND Dam Hazard's termination-point defaults:
        # screening values chosen by the engineer, not requirements.
        self.class_stop = False
        self.class_stop_threshold_ft = stoprule.DEFAULT_THRESHOLD_FT
        self.class_stop_confirm = stoprule.DEFAULT_CONFIRM
        self.class_stop_station_mi = stoprule.DEFAULT_STATION_MI
        self.class_stop_search_ft = stoprule.DEFAULT_SEARCH_FT
        # v3.2.0: this tool's EAP map is a preview; ND Dam Hazard's map book is the EAP map.
        # "auto" = draw the preview only when ND Dam Hazard is not installed, "mapbook" =
        # never, "svg" = always (products.EAP_CHOICES)
        self.eap_map = "auto"
        # v3.2.0: write <dam folder>/dam.json after the run (the rundam / batch tools set it):
        # None, or {"point": {"lon", "lat", "x", "y", "crs"}, "read": [log lines],
        # "top_source": "parameter" | "record", "nid_id": text}
        self.dam_record = None
        # v3.4.0: LISFLOOD-FP floodplain solver keyword ("acceleration" everywhere by default;
        # "fv1" is what the near-dam refine uses for its fine run)
        self.lisflood_solver = lfmod.SOLVER_ACC
        # v3.4.0: near-dam refine (refine.py): when a receptor lies within refine_dist_mi of
        # the breach and the final rasters come from the ACC run, re-run that stretch on a
        # fine FV1 grid (refine_cell_m; 0 = half the main cell) and merge it in. Off by
        # default; the distance and cell are the engineer's screening values.
        self.refine_near = False
        self.refine_dist_mi = 1.0
        self.refine_cell_m = 0.0
        # v3.11.0: raise the dam line to the top of dam in the model DEM so breach water cannot
        # spill back over a smoothed crest into the reservoir basin (crest.py). The line is
        # crest_line_xy (the CREST_LINE layer) or traced from the DEM; needs a given top of dam.
        self.raise_crest = True
        self.crest_line_xy = None
        # v3.11.0: close the box edges that face upstream (the near-dam FV1 run sets this; a
        # free FV1 edge across ponded water brings water into the model)
        self.close_upstream_edges = False
        for k, v in kw.items():
            if not hasattr(self, k):
                raise TypeError("unknown setting {}".format(k))
            setattr(self, k, v)

    def as_dict(self):
        d = {}
        for k, v in self.__dict__.items():
            if k == "overrides":
                d[k] = v.__dict__
            elif k == "flood":
                d[k] = None if v is None else {
                    "trigger": v.trigger, "trigger_value": v.trigger_value,
                    "normal_pool_ft": v.normal_pool_ft, "crest_length_ft": v.crest_length_ft,
                    "spillway": v.spillway is not None, "inflow_peak_cfs": round(
                        float(v.inflow_q_cms.max()) / CFS, 0),
                    "baseline_csv": v.baseline_csv,
                    "outlets": [(c.name, c.invert_m, c.width_m, c.height_m, c.cd,
                                 c.schedule) for c in (v.outlets or [])],
                    "spillway_schedule": v.spillway_schedule}
            elif k == "receptors":
                d[k] = None if v is None else len(v)
            elif k == "structures":
                d[k] = [s.to_dict() for s in v]
            elif k == "edits":
                d[k] = None if v is None else {"channels": len(v.channels), "ponds": len(v.ponds),
                                              "notches": len(v.notches),
                                              "tailwater": [round(float(x.wse_m), 3) for x in v.stages],
                                              "buildings": len(getattr(v, "buildings", [])),
                                              "embankments": len(getattr(v, "embankments", [])),
                                              "crest_raise": len(getattr(v, "crests", []))}
            elif k in ("refine_lines", "fixed_plan"):
                d[k] = len(v) if k == "refine_lines" else (v is not None)
            elif k == "crest_line_xy":
                d[k] = None if v is None else _hash([[round(float(a), 1), round(float(b), 1)]
                                                    for a, b in v])
            elif k == "dam_record":
                d[k] = v is not None
            else:
                d[k] = v
        return d


_CLASS_STOP_KEYS = ("class_stop", "class_stop_threshold_ft", "class_stop_confirm",
                    "class_stop_station_mi", "class_stop_search_ft")


def key_settings(st):
    """st.as_dict() for checkpoint / run keys: without "resume", the EAP-map choice and the
    dam.json switch (they do not change the flood), and without the v3.2.0 class-stop
    settings while that rule is off - so a checkpoint of 3.1.x still resumes."""
    d = {k: v for k, v in st.as_dict().items() if k not in ("resume", "eap_map", "dam_record")}
    if not getattr(st, "class_stop", False):
        for k in _CLASS_STOP_KEYS:
            d.pop(k, None)
    # v3.4.0 keys are left out while they are at their defaults, so a 3.3.x checkpoint resumes.
    # v3.11.0: the near-dam refine never changes the main ACC runs (it re-runs after them), so
    # its settings are always left out; changing only REFINE_NEAR then reuses the finished runs
    for k in _REFINE_KEYS:
        d.pop(k, None)
    # v3.11.0 keys are left out while they are off, so a 3.10.x checkpoint of such a run resumes
    if not getattr(st, "raise_crest", False):
        d.pop("raise_crest", None)
        d.pop("crest_line_xy", None)
    if not getattr(st, "close_upstream_edges", False):
        d.pop("close_upstream_edges", None)
    if getattr(st, "lisflood_solver", lfmod.SOLVER_ACC) == lfmod.SOLVER_ACC:
        d.pop("lisflood_solver", None)
    ed = d.get("edits")
    if isinstance(ed, dict) and not ed.get("buildings"):
        d["edits"] = {k: v for k, v in ed.items() if k != "buildings"}
    ed = d.get("edits")
    if isinstance(ed, dict) and not ed.get("embankments"):
        d["edits"] = {k: v for k, v in ed.items() if k != "embankments"}
    ed = d.get("edits")
    if isinstance(ed, dict) and not ed.get("crest_raise"):
        d["edits"] = {k: v for k, v in ed.items() if k != "crest_raise"}
    return d


_REFINE_KEYS = ("refine_near", "refine_dist_mi", "refine_cell_m")


class RunResult:
    def __init__(self):
        self.dam_id = ""
        self.folder = ""
        self.final = {}           # kind -> path
        self.baseline = {}        # kind -> path (flood scenario)
        self.increment = {}       # kind -> path (flood scenario)
        self.qa = {}              # model -> {kind: path}
        self.report_md = ""
        self.report_json = ""
        self.report_html = ""
        self.hydrograph_csv = ""
        self.summary = {}
        self.warnings = []
        self.plan = None
        self.grid = None          # Grid of the final arrays
        self.final_si = None      # {kind: array} metres, m/s, hours, metres
        self.base_si = None
        self.gauges = {}          # name -> (t_s, q_cms)
        self.gauges_base = {}     # the same for the no-breach baseline run
        self.vectors = {}
        self.receptor_csv = ""
        self.eap_map = ""
        self.flood = None         # flood.FloodResult
        self.t_ref_hr = 0.0       # breach initiation within the record (flood scenario)
        self.cascade_offset_h = 0.0
        self.animation = None     # animation.AnimationResult when frames were rendered
        self.manifest = ""        # path of run_manifest.json
        self.lisflood_stats = {}  # v3.11.0: statistics of the last LISFLOOD-FP run (refine check)


class _Skip(Exception):
    """The dam does not fail in this flood: nothing to model."""


def _auto_sim_time(duration_s, bounds, st):
    if st.sim_time_hr:
        return st.sim_time_hr * 3600.0
    w, h = terrain.bounds_size(bounds)
    t = duration_s + math.hypot(w, h) / st.front_speed_mps
    return float(min(MAX_SIM_S, max(2 * 3600.0, t)))


def _to_us(kind, a):
    """Model results are SI; the rasters are ft, ft/s and hours."""
    if kind in ("depth", "velocity", "wse"):
        return a / FT
    return a


def _final_arrays(env, arrival_m, t_ref_hr=0.0, init=None):
    """Result arrays (SI) with the wet/dry rules applied. `init` is the initial
    water depth of ponds (m): the depth shown is the water the flood adds above
    it. Arrival times count from breach initiation (t_ref_hr)."""
    d = env.depth.data
    if init is not None:
        d = np.where(np.isfinite(d), np.clip(d - np.nan_to_num(init.data), 0.0, None), d)
    wet = np.isfinite(d) & (d > WET_M)
    arr = env.arrival_hr.data - t_ref_hr
    arr = np.where(np.isfinite(arr), np.clip(arr, 0.0, None), np.nan)
    out = {
        "depth": np.where(wet, d, np.nan),
        "velocity": np.where(wet, env.velocity.data, np.nan),
        "arrival": np.where(wet & (d >= arrival_m - lfmod.ARRIVAL_TOL_M), arr, np.nan),
        "wse": np.where(wet, env.wse.data, np.nan),
    }
    return out


def compare(a_env, b_env, thr_m):
    """Agreement between two flood extents on the same grid."""
    da, db = a_env.depth.data, b_env.depth.data
    wa = np.isfinite(da) & (da >= thr_m)
    wb = np.isfinite(db) & (db >= thr_m)
    both = wa & wb
    union = wa | wb
    cell = a_env.depth.dx ** 2 / 4046.8564224
    out = {"lisflood_acres": round(float(wa.sum()) * cell, 1),
           "anuga_acres": round(float(wb.sum()) * cell, 1),
           "both_acres": round(float(both.sum()) * cell, 1),
           "fit_index_F": round(float(both.sum()) / max(1, union.sum()), 3)}
    if both.any():
        diff = (db - da)[both] / FT
        out.update({"depth_diff_mean_ft": round(float(diff.mean()), 2),
                    "depth_diff_abs_p90_ft": round(float(np.percentile(np.abs(diff), 90)), 2)})
        va, vb = a_env.velocity.data[both], b_env.velocity.data[both]
        out["velocity_diff_mean_fps"] = round(float(np.nanmean(vb - va)) / FT, 2)
    return out


def _direction_at(path, xy, span=200.0):
    """Unit vector of the flow path around the vertex nearest `xy`."""
    p = np.asarray(path)
    i = int(np.argmin(np.hypot(p[:, 0] - xy[0], p[:, 1] - xy[1])))
    step = max(1, int(round(span / max(1.0, np.hypot(*(p[1] - p[0])) if len(p) > 1 else 1.0))))
    a, b = p[max(0, i - step)], p[min(len(p) - 1, i + step)]
    d = b - a
    n = float(np.hypot(*d)) or 1.0
    return (d[0] / n, d[1] / n), i


def _hash(*parts):
    h = hashlib.sha1()
    for p in parts:
        h.update(json.dumps(p, sort_keys=True, default=str).encode())
    return h.hexdigest()[:16]


def _span(progress, lo, hi):
    def f(p, m=""):
        progress(lo + (hi - lo) * max(0.0, min(100.0, p)) / 100.0, m)
    return f


class _State:
    """Checkpoint after each LISFLOOD-FP run so an interrupted dam can resume."""

    def __init__(self, model_dir, key, enabled):
        self.path = os.path.join(model_dir, STATE_FILE)
        self.key = key
        self.enabled = enabled
        self.data = {"key": key}
        if enabled and os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    old = json.load(f)
                if old.get("key") == key:
                    self.data = old
                    self.resumed = True
                    return
            except (OSError, ValueError):
                pass
        self.resumed = False

    def save(self, **kw):
        self.data.update(kw)
        self.data["key"] = self.key
        with open(self.path, "w") as f:
            json.dump(self.data, f, default=_json_default)

    def get(self, k, default=None):
        return self.data.get(k, default) if self.resumed else default


def _dam_line(st, dam, dam_xy, terrain_src, cs):
    """The dam line to raise (v3.11.0): (line (N, 2) in the working CRS or None, note). The
    CREST_LINE layer when one was given, otherwise traced from the DEM (crest.py)."""
    if st.crest_line_xy is not None and len(st.crest_line_xy) >= 2:
        return np.asarray(st.crest_line_xy, dtype=float), "the dam crest line you gave"
    clen = float(dam.crest_length_ft) * FT if getattr(dam, "crest_length_ft", None) else None
    half = min(crestmod.MAX_HALF_LEN_M, 1.1 * clen if clen else crestmod.DEFAULT_HALF_LEN_M) + 200.0
    g = terrain_src.read((dam_xy[0] - half, dam_xy[1] - half, dam_xy[0] + half,
                          dam_xy[1] + half), min(cs, 10.0))
    height_m = float(dam.dam_height_ft or 20.0) * FT
    return crestmod.trace_crest(g, dam_xy, height_m, clen)


def attach_proposed(st, out_folder, dam, warnings, log):
    """Insert a proposed embankment from <out_folder>/dam.json onto every grid the
    models read. Nothing is done when the record has no such block, so a run of an
    existing dam keeps the same checkpoint key."""
    from . import damjson
    from . import proposed as prop
    rec, problem = damjson.load(out_folder)
    if problem or rec is None:
        return
    spec = (rec.extra or {}).get("proposed_embankment")
    if not isinstance(spec, dict) or spec.get("phrase") != prop.PHRASE:
        return
    if not spec.get("line_xy") or spec.get("crest_m") in (None, ""):
        warnings.append("dam.json names a proposed embankment but has no crest line; "
                        "it was not inserted.")
        return
    st.proposed = spec
    if getattr(st, "edits", None) is None:
        st.edits = cond.Edits()
    if not any(isinstance(e, cond.EmbankmentInsert) for e in st.edits.embankments):
        st.edits.embankments.append(cond.EmbankmentInsert(spec))
    if not getattr(dam, "crest_length_ft", None) and spec.get("crest_len_m"):
        dam.crest_length_ft = float(spec["crest_len_m"]) / FT
        log("Crest length from the proposed embankment: {:.1f} ft.".format(dam.crest_length_ft))
    if prop.PHRASE not in warnings:
        warnings.append(prop.PHRASE)
    kind = spec.get("rim_kind")
    if kind and kind != "contained":
        rim = spec.get("rim_m")
        warnings.append(
            "Proposed dam: the pool leaves the basin ({}) below the design crest{}.".format(
                str(kind).replace("_", " "),
                "" if rim is None else " (rim {:.1f} ft)".format(float(rim) / FT)))


def run_dam(dam, dam_xy, st, terrain_src, writer, out_folder, inflow_xy=None,
            progress=None, log=None, cancel=None):
    """dam: breach.DamData; dam_xy / inflow_xy in the working (metric) CRS.
    terrain_src.read(bounds, cell) -> Grid of elevations in metres (NaN = no data)
    terrain_src.manning(bounds, cell) -> Grid or None
    terrain_src.z_to_m : factor that converts DEM elevations to metres
    writer(grid, path) writes a raster in the output CRS."""
    t_start = time.time()
    progress = progress or (lambda p, m="": None)
    log = log or (lambda m: None)
    cancel = cancel or (lambda: False)
    if st.breach_xy is not None:
        dam_xy = (float(st.breach_xy[0]), float(st.breach_xy[1]))
    res = RunResult()
    res.dam_id = dam.dam_id
    res.folder = out_folder
    model_dir = os.path.join(out_folder, MODEL_FOLDER)
    os.makedirs(model_dir, exist_ok=True)
    warnings = []
    flood_mode = st.scenario == SCENARIO_FLOOD
    report = {"tool": "ND Dam Breach Runner " + VERSION,
              "run_started": datetime.datetime.now().isoformat(timespec="seconds"),
              "dam": brmod.dam_to_dict(dam), "dam_xy": list(dam_xy),
              "settings": st.as_dict(), "scenario": st.scenario,
              "policy": "North Dakota Dam Safety Standards Policy REG_05.2026, Section 5.1 "
                        "(breach analysis) and 5.4 (outputs)"}
    arrival_m = st.arrival_depth_ft * FT
    tol_m = st.edge_tolerance_ft * FT
    cs = st.cell_size_m

    # DEM edits (channels, ponds, notches, v3.4.0 buildings, v3.7.0 a proposed
    # embankment from dam.json) apply to everything the models read
    raw_terrain = terrain_src                  # the near-dam refine run wraps the edits itself
    if st.edits is not None and st.edits.crests:       # v3.11.0: another dam's raised line
        st.edits = st.edits.copy()
        st.edits.crests = []
        if st.edits.empty():
            st.edits = None
    attach_proposed(st, out_folder, dam, warnings, log)
    if st.edits is not None and not st.edits.empty():
        terrain_src = cond.ConditionedTerrain(terrain_src, st.edits, default_n=st.manning_n)
    has_ponds = st.edits is not None and st.edits.has_water()

    def manning_for(bnds, cell):
        man = terrain_src.manning(bnds, cell) if hasattr(terrain_src, "manning") else None
        if man is not None and getattr(st, "manning_scale", 1.0) != 1.0:
            man = man.like(np.where(man.valid(), man.data * st.manning_scale, man.data))
        return man

    def dem_for(model, bounds, cell):
        g = terrain_src.read(bounds, cell)
        cuts = structmod.structure_edits(st.structures, cell, model)
        if cuts:
            g, _, _ = cond.Edits(notches=cuts).apply(g)
        return g

    # --- 3.5: inventory inlet / outlet elevations against the LiDAR -------------
    if st.structures:
        try:
            inv_notes = structmod.reconcile_inverts(st.structures, terrain_src.read,
                                                    getattr(st, "invert_tol_m", 3.0 * FT), min(cs, 3.0))
        except Exception as e:                                   # noqa - never stop the run for this
            inv_notes = ["inventory inverts not checked: {}".format(e)]
        for n in inv_notes:
            log(n)
        if inv_notes:
            report["structure_inverts"] = inv_notes

    # --- top of dam from the DEM if not given ---------------------------------
    near = terrain_src.read((dam_xy[0] - 60, dam_xy[1] - 60, dam_xy[0] + 60, dam_xy[1] + 60),
                            min(cs, 3.0))
    z_note = getattr(terrain_src, "z_note", "")
    report["dem_elevation_units"] = z_note
    # v3.10.0: what the HEC-RAS model builder needs to place the run (hecras_model.py)
    report["dem_z_to_m"] = getattr(terrain_src, "z_to_m", None)
    report["work_crs"] = getattr(writer, "work_srs", None)
    if "check" in z_note.lower():
        warnings.append("DEM elevation units: {}. Set 'DEM elevation units' explicitly if this "
                        "is wrong; a feet DEM read as metres gives a wrong flood without "
                        "any error.".format(z_note))
    z = near.masked()
    X, Y = np.meshgrid(near.x_centers(), near.y_centers())
    spec = getattr(st, "proposed", None)
    if spec:
        from .proposed import crest_check
        crest_dem_ft, err = crest_check(
            z, X, Y, dam_xy, float(spec["crest_m"]), float(spec.get("crest_width_m") or 0.0))
        if err:
            raise brmod.BreachInputError("{}: {}".format(dam.dam_id, err))
    else:
        sel = np.hypot(X - dam_xy[0], Y - dam_xy[1]) <= 30.0
        crest_dem_ft = float(np.nanmax(z[sel])) / FT if np.isfinite(z[sel]).any() else None
        if crest_dem_ft is None:
            raise brmod.BreachInputError("{}: no DEM data at the dam point".format(dam.dam_id))
    report["dem_crest_ft"] = round(crest_dem_ft, 2)
    if not spec and dam.top_of_dam_ft is not None and dam.dam_height_ft:
        diff = crest_dem_ft - dam.top_of_dam_ft
        if abs(diff) > max(0.6 * dam.dam_height_ft, 8.0):
            raise brmod.BreachInputError(
                "{}: the highest DEM point within 30 m of the dam point is {:.1f} ft but the "
                "top of dam is {:.1f} ft (difference {:+.1f} ft, dam height {:.1f} ft). "
                "Check the DEM elevation units ({}), the datum of the top of dam, and that "
                "the dam point is on the crest.".format(
                    dam.dam_id, crest_dem_ft, dam.top_of_dam_ft, diff, dam.dam_height_ft,
                    z_note or "unknown"))
        if abs(diff) > 3.0:
            warnings.append("The DEM crest near the dam point ({:.1f} ft) differs from the top "
                            "of dam ({:.1f} ft) by {:+.1f} ft{}.".format(
                                crest_dem_ft, dam.top_of_dam_ft, diff,
                                " (the model DEM raises the dam line to the top of dam: see "
                                "crest_raise in the run report)"
                                if getattr(st, "raise_crest", False) else ""))
    top_from_dem = dam.top_of_dam_ft is None
    if dam.top_of_dam_ft is None:
        dam.top_of_dam_ft = crest_dem_ft
        warnings.append("Top of dam not given: taken as the highest DEM point within 30 m of "
                        "the dam point ({:.1f} ft). Check it against the as-built crest."
                        .format(dam.top_of_dam_ft))
        report["dam"]["top_of_dam_ft"] = dam.top_of_dam_ft

    # --- v3.11.0: raise the dam line to the top of dam in the model DEM ---------------------
    crest_line = None
    report["crest_raise"] = {"applied": False, "reason": "off (RAISE_CREST not set)"}
    if getattr(st, "raise_crest", False):
        if top_from_dem:
            report["crest_raise"]["reason"] = ("no top of dam was given, so there is nothing to "
                                               "raise the line to")
        elif spec:
            report["crest_raise"]["reason"] = "a proposed embankment sets the crest"
        else:
            crest_line, note = _dam_line(st, dam, dam_xy, terrain_src, cs)
            report["crest_raise"]["reason"] = note
            if crest_line is None:
                warnings.append("The dam line was not raised in the model DEM: {}. Breach water "
                                "can spill back over a low DEM crest into the reservoir basin; "
                                "give the dam crest (line) to fix that.".format(note))
            else:
                top_m_r = dam.top_of_dam_ft * FT
                st.edits = st.edits.copy() if st.edits is not None else cond.Edits()
                st.edits.crests = [cond.CrestRaise(crest_line, top_m_r)]
                terrain_src = cond.ConditionedTerrain(raw_terrain, st.edits,
                                                      default_n=st.manning_n)
                terrain_src.read((crest_line[:, 0].min() - 3 * cs, crest_line[:, 1].min() - 3 * cs,
                                  crest_line[:, 0].max() + 3 * cs, crest_line[:, 1].max() + 3 * cs),
                                 cs)             # counts the raised cells on the model grid
                raised_cells = int(terrain_src.last_counts.get(
                    "crests:" + st.edits.crests[0].name, 0))
                report["crest_raise"].update({
                    "applied": True, "top_of_dam_ft": round(dam.top_of_dam_ft, 2),
                    "dem_crest_ft": round(crest_dem_ft, 2),
                    "line_ft": round(cond.polyline_length(crest_line) / FT, 0),
                    "cells_raised_on_the_model_grid": raised_cells,
                    "line_xy": [[round(float(x), 1), round(float(y), 1)] for x, y in crest_line]})
                if "give the crest line" in note:
                    warnings.append("Dam line: " + note)
                log("Dam line raised to the top of dam ({:.1f} ft) in the model DEM: {} "
                    "({:,.0f} ft, {:,} cells at {:g} m).".format(
                        dam.top_of_dam_ft, note, report["crest_raise"]["line_ft"], raised_cells,
                        cs))

    # --- breach hydrograph(s) ------------------------------------------------------
    progress(1, "breach hydrograph")
    hy_nb = None
    fr = None
    if st.user_hydrograph_obj is not None:
        hy, hy_nb = st.user_hydrograph_obj, st.user_baseline_obj
        warnings.append("The inflow hydrograph was handed over from another model run and is "
                        "used as given.")
    elif flood_mode:
        hy, hy_nb, fr = _flood_hydrographs(dam, st, warnings)
        res.flood = fr
        report["flood"] = fr.summary if fr is not None else {"user_hydrographs": True}
        if hy is None:
            report["summary"] = {"dam_id": dam.dam_id, "final_model": "none",
                                 "breach_occurs": False, "flooded_acres": 0}
            warnings += fr.notes
            if st.dam_record is not None and st.write_final:
                report["dam_record"] = _record_dam(dam, st, res, dam_xy, out_folder,
                                                   top_from_dem, "none", warnings, log)
            _write_no_breach(res, report, warnings, model_dir, hy_nb)
            return res
    else:
        hy = brmod.make_hydrograph(dam, st.breach_method, st.overrides, st.user_hydrograph)
    amb_cms = max(0.0, float(st.ambient_cfs or 0.0)) * CFS
    spin_s = max(0.0, float(st.spinup_hr)) * 3600.0 if amb_cms > 0 else 0.0
    res.hydrograph_csv = os.path.join(model_dir, "breach_hydrograph.csv")
    hy.to_csv(res.hydrograph_csv)                   # as computed, before any spin-up shift
    hy_summary = hy.summary()                       # the report and the HEC-RAS export use this
    hy_pre, hy_nb_pre = hy, hy_nb                   # before the spin-up shift (refine run)
    if amb_cms > 0:
        if hy_nb is None and st.baseline:
            hy_nb = _zero_hydrograph(hy, "no breach (ambient flow only)")
        if hy_nb is not None:
            hy_nb.to_csv(os.path.join(model_dir, "no_breach_hydrograph.csv"))
        hy, hy_nb = _shift(hy, spin_s), _shift(hy_nb, spin_s)
        hy.to_csv(os.path.join(model_dir, "model_inflow_hydrograph.csv"))
        warnings.append(
            "Ambient stream flow of {:,.0f} cfs is added at the breach inflow from the start, "
            "with {:.1f} h of spin-up before the breach hydrograph. The depth and flooded "
            "area include the water the ambient flow already carries; the incremental rasters "
            "(with the breach minus ambient flow only) show what the failure adds.".format(
                st.ambient_cfs, st.spinup_hr))
        report["ambient"] = {"cfs": st.ambient_cfs, "spinup_hr": st.spinup_hr,
                             "breach_starts_in_model_at_hr": (hy.t_ref_s / 3600.0)}
    elif hy_nb is not None:
        hy_nb.to_csv(os.path.join(model_dir, "no_breach_hydrograph.csv"))
    t_ref_hr = hy.t_ref_s / 3600.0
    res.t_ref_hr = t_ref_hr
    has_base = bool(hy_nb is not None and st.baseline and (
        flood_mode or amb_cms > 0 or st.user_baseline_obj is not None))
    report["hydrograph"] = hy_summary
    warnings += hy.notes
    log("Breach hydrograph ({}): peak {:.0f} cfs at {:.2f} h{}, {:.0f} ac-ft".format(
        hy.method, hy.peak_cfs, hy.time_to_peak_hr,
        " after initiation (breach at {:.2f} h of the flood)".format(t_ref_hr) if flood_mode
        else "", hy.volume_m3 / ACFT))

    state = _State(model_dir, _hash(brmod.dam_to_dict(dam), dam_xy, key_settings(st),
                                    hy.summary(), inflow_xy), st.resume)
    if st.resume and state.resumed:
        log("Resuming from the checkpoint in {}".format(model_dir))
    elif st.resume:
        log("No matching checkpoint (nothing saved, or the settings changed): starting over.")

    # --- inflow point -------------------------------------------------------------
    h_m = dam.dam_height_ft * FT
    top_m = dam.top_of_dam_ft * FT
    plan = st.fixed_plan
    if plan is not None:
        inflow_xy = plan["inflow_xy"]
        report["inflow_point"] = {"xy": list(inflow_xy), "how": "from the base run"}
    else:
        rad = max(90.0, 8.0 * h_m)
        local = terrain_src.read((dam_xy[0] - rad - 30, dam_xy[1] - rad - 30,
                                  dam_xy[0] + rad + 30, dam_xy[1] + rad + 30), cs)
        if inflow_xy is None:
            inflow_xy, why = terrain.find_inflow_point(local, dam_xy, h_m, top_m, rad)
            if inflow_xy is None:
                raise brmod.BreachInputError("{}: could not place the breach inflow: {}"
                                             .format(dam.dam_id, why))
            report["inflow_point"] = {"xy": list(inflow_xy), "how": why}
        else:
            report["inflow_point"] = {"xy": list(inflow_xy), "how": "given"}
    log("Breach inflow at {:.1f}, {:.1f} ({})".format(inflow_xy[0], inflow_xy[1],
                                                      report["inflow_point"]["how"]))

    # --- initial domain from a flow trace --------------------------------------
    progress(3, "tracing the flow path")
    L = st.initial_length_mi * MILE
    W = st.half_width_mi * MILE
    if plan is not None:
        path, direction, bounds = plan["path"], plan["direction"], tuple(plan["bounds"])
        plen = plan.get("trace_length_m", 0.0)
    else:
        half = L + W + 2 * st.planning_cell_m
        pl = terrain_src.read((inflow_xy[0] - half, inflow_xy[1] - half,
                               inflow_xy[0] + half, inflow_xy[1] + half), st.planning_cell_m)
        path, plen = terrain.trace_downstream(pl, inflow_xy, L)
        direction = terrain.flow_direction(path)
        bounds = terrain.path_bounds(path + [dam_xy], W, snap=cs)
        d_start = math.hypot(path[0][0] - dam_xy[0], path[0][1] - dam_xy[1])
        d_end = math.hypot(path[-1][0] - dam_xy[0], path[-1][1] - dam_xy[1])
        if plen < 0.25 * L or d_end < d_start + 150.0:
            warnings.append(
                "The steepest-descent path from the breach inflow point is only {:.0f} m long "
                "and ends {:.0f} m from the dam. The inflow point may be in the reservoir or in "
                "a closed basin; give the breach inflow point explicitly (just below the "
                "dam).".format(plen, d_end))
    report["trace"] = {"length_mi": round(plen / MILE, 2), "points": len(path)}
    np.savetxt(os.path.join(model_dir, "flow_trace.csv"), np.asarray(path), delimiter=",",
               header="x,y", comments="", fmt="%.2f")
    max_extent = st.max_extent_mi * MILE
    step = st.expand_step_mi * MILE
    duration_s = max(hy.duration_s, hy_nb.duration_s if hy_nb is not None else 0.0)
    sim_time = plan["sim_time"] if plan is not None else _auto_sim_time(duration_s, bounds, st)
    width = hy.params.get("avg_breach_width_ft")
    inlet_w = st.inflow_width_m or max(cs, (width or 100.0) * FT)
    inline = anmod.inlet_line(inflow_xy, direction, inlet_w)
    if st.sim_time_hr and st.sim_time_hr * 3600.0 < duration_s:
        warnings.append("The simulation duration ({:.1f} h) is shorter than the breach "
                        "hydrograph ({:.1f} h); the rest of the breach volume never enters the "
                        "model. Leave the duration automatic or lengthen it.".format(
                            st.sim_time_hr, duration_s / 3600.0))
    if st.structures:
        report["structures"] = structmod.describe(st.structures)
        # v3.12.0: the structures as the run used them (inverts after the LiDAR check above;
        # settings.structures has them as given), for the HEC-RAS model export
        report["structures_used"] = [s.to_dict() for s in st.structures]

    # --- LISFLOOD-FP run helper -----------------------------------------------------
    def gauge_list(bnds, gauges):
        gl = []
        for g in (gauges or []):
            gdir, _ = _direction_at(path, g["xy"])
            w_full = max(terrain.bounds_size(bnds))
            gg = lfmod.gauge_section(g["xy"], gdir, w_full)
            gg["name"] = g["name"]
            if not (bnds[0] < g["xy"][0] < bnds[2] and bnds[1] < g["xy"][1] < bnds[3]):
                msg = "Gauge '{}' is outside the model domain; no discharge record.".format(
                    g["name"])
                if msg not in warnings:
                    warnings.append(msg)
                continue
            gl.append(gg)
        return gl

    def extra_sources(sim_s):
        if amb_cms <= 0:
            return []
        return [{"name": "ambient", "xy": inflow_xy, "line": inline,
                 "t_s": [0.0, sim_s + 3600.0], "q_cms": [amb_cms, amb_cms]}]

    frames = int(st.snapshot_frames or (24 if st.animation else 0))

    def snap_for(sim_s):
        return max(60.0, round(sim_s / frames / 60.0) * 60.0) if frames > 0 else None

    def closed_edges():
        """v3.11.0: the near-dam FV1 run closes the edges that face the reservoir."""
        if getattr(st, "close_upstream_edges", False):
            from . import refine as refmod
            return refmod.upstream_edges(direction)
        return ()

    def lisflood_run(tag, hyd, bnds, sim_s, prog, gauges=None, snap=None):
        dem = dem_for("lisflood", bnds, cs)
        man = manning_for(bnds, cs)
        init = terrain_src.initial_depth(bnds, cs) if has_ponds else None
        gl = gauge_list(bnds, gauges)
        lst = lfmod.LisfloodSettings(st.lisflood_exe, st.threads, st.manning_n, man, arrival_m,
                                     sim_s, massint_s=st.gauge_massint_s if gl else 300.0,
                                     initial_depth=init, gauges=gl, snapshot_s=snap,
                                     extra_sources=extra_sources(sim_s),
                                     solver=getattr(st, "lisflood_solver", lfmod.SOLVER_ACC),
                                     closed_edges=closed_edges())
        wd = os.path.join(model_dir, "lisflood" + tag)
        if snap:        # snapshots of an earlier (smaller) run must not mix with these
            for _i, fp in lfmod.snapshot_files(wd):
                for ext in (".wd", ".Vx", ".Vy"):
                    try:
                        os.remove(fp[:-3] + ext)
                    except OSError:
                        pass
        par = lfmod.write_inputs(wd, dem, inflow_xy, hyd, lst, inflow_line=inline)
        runtime = lfmod.run(wd, par, lst, prog, cancel, log)
        env = lfmod.read_results(wd, dem, lst, hyd)
        return env, dem, init, runtime, wd, lst

    def load_lisflood(tag, hyd, bnds, sim_s):
        """Results of a finished run (resume)."""
        dem = dem_for("lisflood", bnds, cs)
        man = manning_for(bnds, cs)
        init = terrain_src.initial_depth(bnds, cs) if has_ponds else None
        gl = gauge_list(bnds, st.gauges)
        lst = lfmod.LisfloodSettings(st.lisflood_exe, st.threads, st.manning_n, man, arrival_m,
                                     sim_s, massint_s=st.gauge_massint_s if gl else 300.0,
                                     initial_depth=init, gauges=gl,
                                     extra_sources=extra_sources(sim_s),
                                     solver=getattr(st, "lisflood_solver", lfmod.SOLVER_ACC),
                                     closed_edges=closed_edges())
        return lfmod.read_results(os.path.join(model_dir, "lisflood" + tag), dem, lst, hyd), \
            dem, init

    # --- LISFLOOD-FP with domain extension ----------------------------------------
    iterations = list(state.get("iterations", []))
    lf_env, dem, init_grid = None, None, None
    lst, wd = None, None
    lf_span = (5.0, 60.0 if not has_base else 45.0)
    start_it = len(iterations) + 1
    saved_warn = state.get("warnings")
    if state.resumed and saved_warn:
        warnings += [w for w in saved_warn if w not in warnings]
    if state.resumed and iterations:
        bounds = tuple(state.get("next_bounds", bounds))
        sim_time = state.get("next_sim_time", sim_time)
    edge_info_all = []
    class_stop_all = []
    pre_base = None           # a no-breach run made for the class-stop rule (reused if final)
    if state.resumed and state.get("lisflood_done"):
        bounds = tuple(state.get("final_bounds"))
        sim_time = state.get("final_sim_time", sim_time)
        lf_env, dem, init_grid = load_lisflood("", hy, bounds, sim_time)
        log("LISFLOOD-FP results loaded from the checkpoint.")
        start_it = st.max_iterations + 1
    max_it = 1 if plan is not None else st.max_iterations
    for it in range(start_it, max_it + 1):
        if cancel():
            raise lfmod.Cancelled("cancelled")
        base = lf_span[0] + (lf_span[1] - lf_span[0]) * (it - 1) / max_it
        span = (lf_span[1] - lf_span[0]) / max_it

        def prog(p, m, base=base, span=span, it=it):
            progress(base + span * p / 100.0, "run {}: {}".format(it, m))

        nx_ = int(round((bounds[2] - bounds[0]) / cs))
        ny_ = int(round((bounds[3] - bounds[1]) / cs))
        log("LISFLOOD-FP run {}: {:.1f} x {:.1f} mi, {:,} cells, {:.1f} h".format(
            it, (bounds[2] - bounds[0]) / MILE, (bounds[3] - bounds[1]) / MILE, nx_ * ny_,
            sim_time / 3600.0))
        lf_env, dem, init_grid, runtime, wd, lst = lisflood_run("", hy, bounds, sim_time, prog,
                                                                st.gauges, snap_for(sim_time))
        cover = float(np.isfinite(dem.masked()).mean())
        depth_eff = lf_env.depth.data
        if init_grid is not None:
            depth_eff = np.clip(depth_eff - np.nan_to_num(init_grid.data), 0.0, None)
        edges = terrain.edge_depths(depth_eff, dem.valid())
        over = [e for e in terrain.EDGES if edges[e] > tol_m]
        arr_max = float(np.nanmax(lf_env.arrival_hr.data)) if np.isfinite(
            lf_env.arrival_hr.data).any() else 0.0
        still_moving = arr_max > 0.9 * sim_time / 3600.0
        rec = {"run": it, "bounds": [round(b, 1) for b in bounds],
               "size_mi": [round((bounds[2] - bounds[0]) / MILE, 2),
                           round((bounds[3] - bounds[1]) / MILE, 2)],
               "cells": int(dem.data.size), "dem_coverage": round(cover, 4),
               "sim_time_hr": round(sim_time / 3600.0, 2), "runtime_min": round(runtime / 60, 1),
               "edge_max_depth_ft": {k: round(v / FT, 2) for k, v in edges.items()},
               "edges_over_tolerance": over, "latest_arrival_hr": round(arr_max, 2),
               "stats": lf_env.stats}
        iterations.append(rec)
        warnings += [n for n in lf_env.notes if n not in warnings]
        nodata = ~dem.valid()
        if nodata.any() and not any("no-data" in w for w in warnings):
            from scipy.ndimage import binary_dilation
            touch = binary_dilation(nodata, structure=np.ones((3, 3), bool)) & (
                np.nan_to_num(lf_env.depth.data) > WET_M)
            if touch.any():
                warnings.append(
                    "The flood reaches {:,} cells next to DEM no-data cells. LISFLOOD-FP treats "
                    "no-data as very high ground, so water piles up against it. Fill the gaps "
                    "in the DEM (or add the missing tiles) and run again.".format(
                        int(touch.sum())))
        # receptors decide which edges still matter (ND 5.1.1(f))
        if over and st.receptors is not None and len(st.receptors) and plan is None:
            need, info = impmod.edge_decisions(
                st.receptors, terrain_src, dem, depth_eff, over, tol_m,
                st.impact_look_ahead_mi * MILE, st.planning_cell_m)
            rec["edge_decisions"] = info
            edge_info_all.append({"run": it, "edges": info})
            for e in over:
                if e not in need:
                    i = info[e]
                    msg = ("The flood leaves through the {} edge {:.1f} ft deep, but {}; the "
                           "domain stops growing there (ND 5.1.1(f)). Confirm nothing that "
                           "would change the classification lies beyond.".format(
                               e, i["edge_depth_ft"], i["reason"]))
                    log(msg)
                    if msg not in warnings:
                        warnings.append(msg)
            over = [e for e in over if e in need]
            rec["edges_over_after_receptors"] = over
        # v3.2.0: stop where the class stops changing (optional rule, stoprule.py)
        if over and st.class_stop and plan is None:
            has_layers = st.receptors is not None and len(st.receptors) > 0
            rinfo = rec.get("edge_decisions") or {}
            base_eff, fpath = None, None
            if not has_layers or any(not rinfo.get(e, {}).get("extend", True) for e in over):
                if has_base:
                    log("Class-stop rule: running the no-breach flood on this domain for the "
                        "rise over the baseline.")
                    b_env, _bd, _bi, rt_b0, _bw, _bl = lisflood_run(
                        "_base", hy_nb, bounds, sim_time, prog, st.gauges)
                    pre_base = {"bounds": tuple(bounds), "sim_time": sim_time, "env": b_env,
                                "runtime": rt_b0}
                    base_eff = b_env.depth.data
                    if init_grid is not None:
                        base_eff = np.clip(base_eff - np.nan_to_num(init_grid.data), 0.0, None)
                try:
                    fpath = stoprule.flow_path(dem, inflow_xy)
                except (ValueError, IndexError) as ex:
                    fpath = None
                    log("Class-stop rule: the flow path could not be traced ({}).".format(ex))
            decs = {}
            for e in over:
                decs[e] = stoprule.judge_edge(
                    e, dem, depth_eff, fpath, base_eff, st.class_stop_threshold_ft,
                    st.class_stop_confirm, st.class_stop_station_mi, st.class_stop_search_ft,
                    receptors=rinfo.get(e) if has_layers else None)
                msg = "Class-stop rule, {} edge ({:.1f} ft deep at the edge): {}.".format(
                    e, edges[e] / FT, decs[e]["reason"])
                log(msg)
                if decs[e]["accept"]:
                    msg += (" The domain stops growing there (ND 5.1.1(f)). Confirm that "
                            "nothing beyond it would change the classification.")
                    if msg not in warnings:
                        warnings.append(msg)
            rec["class_stop"] = decs
            class_stop_all.append({"run": it, "edges": decs})
            over = [e for e in over if not decs[e]["accept"]]
            rec["edges_over_after_class_stop"] = over
        if plan is not None:
            if over:
                warnings.append("The flood leaves the base domain through {} deeper than "
                                "{:.1f} ft in this case.".format(", ".join(over),
                                                                 st.edge_tolerance_ft))
            break
        if not over and not still_moving:
            log("Flood stays inside the domain (edge depths below {:.1f} ft, or nothing "
                "downstream to reach).".format(st.edge_tolerance_ft))
            break
        cap_now = MAX_SIM_S if not st.sim_max_hr else min(MAX_SIM_S, float(st.sim_max_hr) * 3600.0)
        if still_moving and st.sim_time_hr and not over and not (
                st.sim_max_hr and sim_time < cap_now - 1.0):
            warnings.append("The flood is still advancing at the end of the {:.1f} h "
                            "simulation (latest arrival {:.2f} h); use a longer duration or "
                            "leave it automatic.".format(sim_time / 3600.0, arr_max))
            break
        if it == st.max_iterations:
            why = []
            if over:
                why.append("leaving the domain through {} deeper than {:.1f} ft".format(
                    ", ".join(over), st.edge_tolerance_ft))
            if still_moving:
                why.append("still advancing at the end of the simulation")
            warnings.append("Stopped after {} runs with the flood {}; results are cut off "
                            "there. Raise the number of runs or the step.".format(
                                it, " and ".join(why)))
            break
        new_bounds = terrain.expand_bounds(bounds, over, step)
        w, h = terrain.bounds_size(new_bounds)
        if over and (w > max_extent or h > max_extent):
            warnings.append("The domain reached the maximum extent ({:.0f} mi) with the flood "
                            "still leaving through {}; results are cut off there."
                            .format(st.max_extent_mi, ", ".join(over)))
            break
        if over:
            log("Flood leaves through {} deeper than {:.1f} ft; extending.".format(
                ", ".join(over), st.edge_tolerance_ft))
            bounds = new_bounds
        new_sim = sim_time
        cap_s = MAX_SIM_S if not st.sim_max_hr else min(MAX_SIM_S, float(st.sim_max_hr) * 3600.0)
        if not st.sim_time_hr:
            new_sim = min(cap_s, max(_auto_sim_time(duration_s, bounds, st),
                                     sim_time * (1.5 if still_moving else 1.0)))
        elif still_moving and sim_time < cap_s - 1.0 and st.sim_max_hr:
            # a chosen duration still lengthens, up to the longest clock, while the
            # flood is moving (the same rule as the screening tool)
            new_sim = min(cap_s, sim_time * 1.5)
        if not over and new_sim <= sim_time * 1.001:
            warnings.append("The flood is still advancing after {:.0f} h, the longest "
                            "simulation the tool will run; results stop there.".format(
                                sim_time / 3600.0))
            break
        sim_time = new_sim
        state.save(iterations=iterations, next_bounds=list(bounds), next_sim_time=sim_time,
                   warnings=warnings, lisflood_done=False)
    state.save(iterations=iterations, lisflood_done=True, final_bounds=list(bounds),
               final_sim_time=sim_time, warnings=warnings)
    report["lisflood_runs"] = iterations
    snap_npz = os.path.join(model_dir, "depth_snapshots.npz")
    if frames > 0 and lst is not None and lst.snapshot_s:
        try:
            times, _p = lfmod.collect_snapshots(wd, lst, snap_npz)
            report["animation_frames"] = 0 if times is None else int(len(times))
        except Exception as e:
            warnings.append("Could not pack the depth snapshots: {}: {}".format(
                type(e).__name__, e))
    if edge_info_all:
        report["receptor_edge_decisions"] = edge_info_all
    if st.class_stop:
        report["class_stop_rule"] = {
            "threshold_ft": st.class_stop_threshold_ft, "confirm": st.class_stop_confirm,
            "station_mi": st.class_stop_station_mi, "search_ft": st.class_stop_search_ft,
            "note": stoprule.DEFAULTS_NOTE, "decisions": class_stop_all}
    res.lisflood_stats = dict(lf_env.stats)
    res.gauges = dict(lf_env.gauges)
    for name, (tt, qq) in res.gauges.items():
        with open(os.path.join(model_dir, "gauge_{}.csv".format(_safe(name))), "w") as f:
            f.write("time_hr,discharge_cfs\n")
            for a, b in zip(tt, qq):
                f.write("{:.5f},{:.3f}\n".format(a / 3600.0, b / CFS))
    res.plan = {"inflow_xy": tuple(inflow_xy), "path": path, "direction": direction,
                "bounds": tuple(bounds), "sim_time": sim_time, "trace_length_m": plen}

    def write_qa(tag, env, model, init):
        fa = _final_arrays(env, arrival_m, t_ref_hr, init)
        res.qa.setdefault(model, {})
        for kind, arr in fa.items():
            p = os.path.join(model_dir, "{}{}_{}".format(model, tag, QA_NAMES[kind]))
            writer(dem.like(_to_us(kind, arr), np.nan), p)
            res.qa[model]["baseline_" + kind if tag else kind] = p
        return fa

    lf_final = write_qa("", lf_env, "lisflood", init_grid)
    final_env, final_model, final_arrays = lf_env, "LISFLOOD-FP", lf_final
    lf_base_env = None
    lf_base_arrays = None

    # --- no-breach baseline (flood scenario) ----------------------------------------
    if has_base:
        progress(46, "no-breach baseline")
        if state.resumed and state.get("baseline_done"):
            lf_base_env, _, _ = load_lisflood("_base", hy_nb, bounds, sim_time)
            res.gauges_base = dict(lf_base_env.gauges)
        elif pre_base is not None and tuple(pre_base["bounds"]) == tuple(bounds) and \
                pre_base["sim_time"] == sim_time:
            lf_base_env = pre_base["env"]          # already run for the class-stop rule
            state.save(baseline_done=True)
            report["baseline_runtime_min"] = round(pre_base["runtime"] / 60.0, 1)
        else:
            lf_base_env, _, _, rt_b, _, _ = lisflood_run("_base", hy_nb, bounds, sim_time,
                                                          _span(progress, 46, 60), st.gauges)
            state.save(baseline_done=True)
            report["baseline_runtime_min"] = round(rt_b / 60.0, 1)
        lf_base_arrays = write_qa("_base", lf_base_env, "lisflood", init_grid)
        res.gauges_base = dict(lf_base_env.gauges)
        warnings += [n for n in lf_base_env.notes if n not in warnings]

    # --- ANUGA ------------------------------------------------------------------------
    an_base_env = None
    if st.tier == TIER_BOTH:
        if sim_time > 48 * 3600.0:
            warnings.append("ANUGA will simulate {:.0f} h; that can take a very long time. "
                            "Consider LISFLOOD-FP only for long floods.".format(
                                sim_time / 3600.0))
        dem_a = dem_for("anuga", bounds, cs)
        flooded = np.isfinite(lf_env.depth.data) & (lf_env.depth.data > 0.05)
        man_a = manning_for(dem_a.bounds, cs)
        ast = anmod.AnugaSettings(st.anuga_python, st.threads, st.manning_n, man_a,
                                  arrival_m, sim_time, 300.0, st.anuga_near_field_mi * MILE,
                                  st.anuga_fine_area_m2, st.anuga_coarse_area_m2,
                                  st.anuga_buffer_m, st.anuga_algorithm)
        width_m = max(3 * cs, inlet_w)
        hy_a, hy_nb_a = _with_flow(hy, amb_cms, sim_time), _with_flow(hy_nb, amb_cms, sim_time)
        a_runs = []
        an_env = None
        reuse = bool(state.resumed and state.get("anuga_done"))
        for k in range(2):
            def aprog(p, m, k=k):
                progress(60 + 10 * k + 10 * p / 100.0, m)
            an_env, outer, fine = anmod.run(
                os.path.join(model_dir, "anuga"), dem_a, flooded, inflow_xy, direction, width_m,
                hy_a, ast, aprog, cancel, log, structures=st.structures, initial_depth=init_grid,
                refine_lines=st.refine_lines, reuse=reuse and k == 0)
            a_runs.append({"buffer_m": ast.buffer_m, "stats": an_env.stats})
            warnings += [n for n in an_env.notes if n not in warnings]
            edge_ft = an_env.stats.get("max_edge_depth_ft", 0.0)
            if edge_ft <= st.edge_tolerance_ft or k == 1:
                if edge_ft > st.edge_tolerance_ft:
                    warnings.append("ANUGA: the flood reaches the mesh boundary {:.1f} ft "
                                    "deep; its extent is limited by the LISFLOOD-FP domain."
                                    .format(edge_ft))
                break
            log("ANUGA flood reaches the mesh edge ({:.1f} ft); widening the mesh.".format(
                edge_ft))
            flooded = np.isfinite(an_env.depth.data) & (an_env.depth.data > 0.05) | flooded
            ast.buffer_m *= 2
        state.save(anuga_done=True)
        report["anuga_runs"] = a_runs
        an_final = write_qa("", an_env, "anuga", init_grid)
        report["comparison"] = compare(lf_env, an_env, arrival_m)
        res.qa["diff"] = {}
        dd = np.where(np.isfinite(an_env.depth.data) | np.isfinite(lf_env.depth.data),
                      (np.nan_to_num(an_env.depth.data) - np.nan_to_num(lf_env.depth.data)) / FT,
                      np.nan)
        p = os.path.join(model_dir, "diff_depth_anuga_minus_lisflood_ft.tif")
        writer(dem.like(dd, np.nan), p)
        res.qa["diff"]["depth"] = p
        final_env, final_model, final_arrays = an_env, "ANUGA", an_final
        if has_base:
            progress(82, "ANUGA baseline")
            fl_b = np.isfinite(lf_env.depth.data) & (lf_env.depth.data > 0.05)
            an_base_env, _, _ = anmod.run(
                os.path.join(model_dir, "anuga_base"), dem_a, fl_b, inflow_xy, direction,
                width_m, hy_nb_a, ast, _span(progress, 82, 92), cancel, log,
                structures=st.structures, initial_depth=init_grid, refine_lines=st.refine_lines,
                reuse=reuse)
            lf_base_arrays = write_qa("_base", an_base_env, "anuga", init_grid)

    # --- v3.4.0: what the DEM conditioning did (report + manifest) -------------------------
    if isinstance(terrain_src, cond.ConditionedTerrain):
        report["dem_conditioning"] = {
            "edits": {k: int(v) for k, v in terrain_src.last_counts.items()},
            "manning_edits": {k: int(v) for k, v in terrain_src.last_manning_counts.items()},
            "buildings": cond.describe_buildings(st.edits, terrain_src.last_counts
                                                 or terrain_src.last_manning_counts),
            "note": "cell counts are on the last grid the models read; the source DEM is "
                    "never changed"}
    # --- v3.4.0: near-dam refine on a fine FV1 grid -----------------------------------------
    fin = final_arrays
    if getattr(st, "refine_near", False):
        from . import refine as refmod
        decision = refmod.decide(st, final_model, st.receptors, st.structures, dam_xy)
        if not decision["refine"]:
            log("Near-dam refine not applied: {}.".format(decision["reason"]))
            report["refine_near_dam"] = {"applied": False, "reason": decision["reason"],
                                         "dist_mi": decision["dist_mi"],
                                         "fine_cell_m": decision["fine_cell_m"],
                                         "counts": decision.get("counts")}
        else:
            progress(86, "near-dam refine (FV1 fine grid)")
            try:
                fin, base_m, rinfo = refmod.run_refine(
                    dam, dam_xy, st, raw_terrain, writer, model_dir, hy_pre, hy_nb_pre,
                    inflow_xy, path, sim_time, dem, fin, lf_base_arrays, decision,
                    _span(progress, 86, 92), log, cancel, crest_line=crest_line)
                if lf_base_arrays is not None and base_m is not None:
                    lf_base_arrays = base_m
                report["refine_near_dam"] = rinfo
                if rinfo.get("warning"):
                    warnings.append("Near-dam refine: " + rinfo["warning"])
                if rinfo.get("applied"):
                    warnings.append("Near-dam refine: " + rinfo["summary"] + " Review the fine "
                                    "run's own report ({}).".format(rinfo["fine_report"]))
            except lfmod.Cancelled:
                raise
            except (lfmod.ModelError, brmod.BreachInputError, ValueError, OSError) as e:
                msg = ("Near-dam refine failed ({}: {}); the ACC results are kept everywhere."
                       .format(type(e).__name__, e))
                log(msg)
                warnings.append(msg)
                report["refine_near_dam"] = {"applied": False, "reason": msg,
                                             "dist_mi": decision["dist_mi"],
                                             "fine_cell_m": decision["fine_cell_m"]}

    # --- final rasters for the classification plugin ------------------------------
    progress(93, "writing rasters")
    if st.write_final:
        # a hazard classification of the previous rasters no longer applies
        for stale in STALE_ON_RERUN:
            try:
                os.remove(os.path.join(out_folder, stale))
            except OSError:
                pass
        if st.products:     # v3.2.0: the preview is drawn again below only when wanted
            from .products import EAP_NAMES
            for stale in EAP_NAMES:
                try:
                    os.remove(os.path.join(out_folder, stale))
                except OSError:
                    pass
        for kind, arr in fin.items():
            p = os.path.join(out_folder, FINAL_NAMES[kind])
            writer(dem.like(_to_us(kind, arr), np.nan), p)
            res.final[kind] = p
    incr = None
    if lf_base_arrays is not None:
        thr_m = [t * FT for t in st.incr_thresholds_ft]
        inc_d, inc_w, inc_stats = floodmod.incremental(
            fin["depth"], lf_base_arrays["depth"], fin["wse"], lf_base_arrays["wse"], thr_m)
        incr = {"depth": inc_d, "wse": inc_w}
        if st.write_final:
            for kind, arr in lf_base_arrays.items():
                p = os.path.join(out_folder, BASELINE_NAMES[kind])
                writer(dem.like(_to_us(kind, arr), np.nan), p)
                res.baseline[kind] = p
            for kind, arr in incr.items():
                p = os.path.join(out_folder, INCREMENT_NAMES[kind])
                writer(dem.like(_to_us(kind, arr), np.nan), p)
                res.increment[kind] = p
        cell_ac = dem.dx ** 2 / 4046.8564224
        report["incremental"] = {
            "thresholds_ft": list(st.incr_thresholds_ft),
            "acres_at_or_above": {"{:g} ft".format(t): round(inc_stats[t * FT] * cell_ac, 1)
                                  for t in st.incr_thresholds_ft},
            "note": "incremental = depth (or water-surface elevation) with the breach minus the "
                    "same flood without it; compare with the ND REG_05.2026 6.4 criteria"}
        res.base_si = lf_base_arrays
    res.grid = dem
    if st.animation and os.path.exists(snap_npz):
        progress(94, "animation")
        try:
            from . import animation
            ar = animation.render_run(
                snap_npz, dem, os.path.join(out_folder, "Animation"), t_ref_hr,
                title="{} breach flood".format(dam.dam_id), dam_xy=dam_xy,
                geotiff_writer=writer if st.write_final else None)
            res.animation = ar
            report["animation"] = {"gif": ar.gif, "mp4": ar.mp4, "frames": len(ar.frames),
                                   "geotiff_index": ar.index_json, "notes": ar.notes}
            if final_model == "ANUGA":
                warnings.append("The animation shows the LISFLOOD-FP flood; the final rasters "
                                "come from ANUGA.")
        except Exception as e:
            warnings.append("Could not render the animation: {}: {}".format(type(e).__name__, e))
    if st.keep_arrays:
        res.final_si = fin
    d = fin["depth"]
    cell_ac = dem.dx ** 2 / 4046.8564224

    # --- receptors -----------------------------------------------------------------------
    if st.receptors is not None and len(st.receptors):
        rsum, rrows = impmod.summarize(st.receptors, dem, fin, arrival_m)
        report["receptors"] = rsum
        res.receptor_csv = os.path.join(model_dir, "receptors_flooded.csv")
        impmod.write_rows(res.receptor_csv, rrows)
        if incr is not None:
            inc_rows = {}
            v = st.receptors.sample(dem, incr["depth"])
            inb = st.receptors.in_bounds(dem.bounds)
            for t in st.incr_thresholds_ft:
                m = inb & np.isfinite(v) & (v >= t * FT)
                inc_rows["{:g} ft".format(t)] = impmod.count_text(st.receptors, m)
            report["receptors_incremental"] = inc_rows
        log("Reaches: {}".format(rsum.get("receptors_flooded")))

    summary = {
        "dam_id": dam.dam_id, "final_model": final_model,
        "peak_breach_cfs": round(hy.peak_cfs, 0), "breach_method": hy.method,
        "storage_at_failure_acft": hy.params.get("storage_at_failure_acft"),
        "flooded_acres": round(float(np.isfinite(d).sum()) * cell_ac, 1),
        "flooded_acres_over_arrival_depth": round(float((d >= arrival_m).sum()) * cell_ac, 1),
        "max_depth_ft": round(float(np.nanmax(d)) / FT, 2) if np.isfinite(d).any() else 0,
        "max_velocity_fps": (round(float(np.nanmax(fin["velocity"])) / FT, 2)
                             if np.isfinite(fin["velocity"]).any() else 0),
        "latest_arrival_hr": (round(float(np.nanmax(fin["arrival"])), 2)
                              if np.isfinite(fin["arrival"]).any() else None),
        "lisflood_runs": len(iterations),
        "domain_mi": iterations[-1]["size_mi"] if iterations else None,
        "scenario": st.scenario, "breach_occurs": True,
        "units": "depth ft, velocity ft/s, arrival hours after breach initiation, WSE ft",
    }
    if "receptors" in report:
        summary["receptors_flooded"] = report["receptors"].get("receptors_flooded")
    if "incremental" in report:
        summary["incremental_acres"] = report["incremental"]["acres_at_or_above"]
    if report.get("refine_near_dam", {}).get("applied"):
        summary["refine_near_dam"] = report["refine_near_dam"]["summary"]
    if report.get("dem_conditioning"):
        summary["buildings"] = report["dem_conditioning"]["buildings"].split(".")[0]
    report["summary"] = summary
    report["warnings"] = warnings
    report["outputs"] = {"final": res.final, "baseline": res.baseline,
                         "increment": res.increment, "qa": res.qa,
                         "hydrograph": res.hydrograph_csv}
    res.summary = summary
    res.warnings = warnings

    # --- products -------------------------------------------------------------------------
    if st.products:
        progress(95, "vector products, EAP map and report")
        try:
            from . import products
            products.write_products(res, report, st, dam, dam_xy, dem, fin, incr, hy, hy_nb,
                                    path, out_folder, model_dir, arrival_m,
                                    crs=getattr(writer, "work_srs", None))
        except Exception as e:          # products are extras; never lose the run for them
            warnings.append("Could not write the vector products / map: {}: {}".format(
                type(e).__name__, e))
    if st.dam_record is not None and st.write_final:
        report["dam_record"] = _record_dam(dam, st, res, dam_xy, out_folder, top_from_dem,
                                           summary["final_model"], warnings, log, dem, d)
    report["run_finished"] = datetime.datetime.now().isoformat(timespec="seconds")
    report["elapsed_min"] = round((time.time() - t_start) / 60.0, 1)
    res.report_json = os.path.join(model_dir, "run_report.json")
    with open(res.report_json, "w") as f:
        json.dump(report, f, indent=2, default=_json_default)
    res.report_md = os.path.join(model_dir, "run_report.md")
    with open(res.report_md, "w") as f:
        f.write(render_report(report))
    if st.products:
        try:
            from . import products
            res.report_html = products.write_html_report(report, model_dir, res)
        except Exception as e:
            warnings.append("Could not write the HTML report: {}".format(e))
    try:
        from . import provenance
        man = provenance.build_manifest(
            report["tool"], report["settings"], report["dam"], dam_xy, report.get("inflow_point"),
            report.get("hydrograph"), st.input_files,
            provenance.flatten_outputs(("", res.final), ("baseline ", res.baseline),
                                       ("incremental ", res.increment),
                                       ("", {"hydrograph": res.hydrograph_csv})),
            st.lisflood_exe, st.anuga_python, started=report["run_started"],
            extra=_manifest_extra(report, summary, warnings))
        res.manifest = provenance.write_manifest(os.path.join(model_dir, provenance.MANIFEST_NAME),
                                                 man)
    except Exception as e:      # the manifest is an extra; never lose the run for it
        warnings.append("Could not write the run manifest: {}: {}".format(type(e).__name__, e))
    if not st.keep_model_files:
        _clean(model_dir)
    progress(100, "done")
    return res


def preview(dam, dam_xy, st, terrain_src, inflow_xy=None, out_folder=None):
    """What a run would model, without running any flood model: the breach
    hydrograph, the first domain and simulation time. Used for the run-time
    estimate."""
    warnings = []
    cs = st.cell_size_m
    if out_folder:
        attach_proposed(st, out_folder, dam, warnings, lambda _m: None)
    if st.breach_xy is not None:
        dam_xy = (float(st.breach_xy[0]), float(st.breach_xy[1]))
    if st.edits is not None and not st.edits.empty():
        terrain_src = cond.ConditionedTerrain(terrain_src, st.edits)
    near = terrain_src.read((dam_xy[0] - 60, dam_xy[1] - 60, dam_xy[0] + 60, dam_xy[1] + 60),
                            min(cs, 3.0))
    z = near.masked()
    X, Y = np.meshgrid(near.x_centers(), near.y_centers())
    sel = np.hypot(X - dam_xy[0], Y - dam_xy[1]) <= 30.0
    if not np.isfinite(z[sel]).any() and not getattr(st, "proposed", None):
        raise brmod.BreachInputError("{}: no DEM data at the dam point".format(dam.dam_id))
    if getattr(st, "proposed", None) and dam.top_of_dam_ft is None:
        dam.top_of_dam_ft = float(st.proposed["crest_m"]) / FT
    elif dam.top_of_dam_ft is None:
        dam.top_of_dam_ft = float(np.nanmax(z[sel])) / FT
    flood_mode = st.scenario == SCENARIO_FLOOD
    hy_nb = None
    if flood_mode:
        hy, hy_nb, _ = _flood_hydrographs(dam, st, warnings)
        if hy is None:
            return {"breach_occurs": False, "warnings": warnings}
    else:
        hy = brmod.make_hydrograph(dam, st.breach_method, st.overrides, st.user_hydrograph)
    h_m = dam.dam_height_ft * FT
    top_m = dam.top_of_dam_ft * FT
    if inflow_xy is None:
        rad = max(90.0, 8.0 * h_m)
        local = terrain_src.read((dam_xy[0] - rad - 30, dam_xy[1] - rad - 30,
                                  dam_xy[0] + rad + 30, dam_xy[1] + rad + 30), cs)
        inflow_xy, why = terrain.find_inflow_point(local, dam_xy, h_m, top_m, rad)
        if inflow_xy is None:
            raise brmod.BreachInputError("{}: could not place the breach inflow: {}".format(
                dam.dam_id, why))
    L = st.initial_length_mi * MILE
    W = st.half_width_mi * MILE
    half = L + W + 2 * st.planning_cell_m
    pl = terrain_src.read((inflow_xy[0] - half, inflow_xy[1] - half,
                           inflow_xy[0] + half, inflow_xy[1] + half), st.planning_cell_m)
    path, plen = terrain.trace_downstream(pl, inflow_xy, L)
    bounds = terrain.path_bounds(path + [dam_xy], W, snap=cs)
    duration_s = max(hy.duration_s, hy_nb.duration_s if hy_nb is not None else 0.0)
    if st.ambient_cfs and st.ambient_cfs > 0:
        duration_s += max(0.0, float(st.spinup_hr)) * 3600.0
    sim_time = _auto_sim_time(duration_s, bounds, st)
    w, h = terrain.bounds_size(bounds)
    return {"breach_occurs": True, "cells": int(round(w / cs)) * int(round(h / cs)),
            "size_mi": (w / MILE, h / MILE), "cell_m": cs, "sim_time_s": sim_time,
            "peak_cfs": hy.peak_cfs, "inflow_xy": inflow_xy,
            "baseline": bool((flood_mode or (st.ambient_cfs or 0) > 0) and st.baseline
                             and (hy_nb is not None or not flood_mode)),
            "trace_mi": plen / MILE, "warnings": warnings, "path": path, "bounds": bounds,
            "direction": terrain.flow_direction(path)}


def _shift(h, spin_s):
    """The hydrograph delayed by spin_s: its first value is held for that long so the model
    starts from the ambient flow, and the breach initiation moves with it."""
    if h is None or spin_s <= 0:
        return h
    t = np.concatenate([[0.0], np.asarray(h.t_s, float) + spin_s])
    q = np.concatenate([[float(h.q_cms[0])], np.asarray(h.q_cms, float)])
    return brmod.Hydrograph(t, q, h.method, h.params, h.notes, h.t_ref_s + spin_s)


def _zero_hydrograph(h, label):
    """No outflow over the same record: the baseline of a sunny-day run that has ambient flow."""
    return brmod.Hydrograph(np.array([0.0, float(h.t_s[-1])]), np.zeros(2), label,
                            {"scenario": label}, [], t_ref_s=h.t_ref_s)


def _with_flow(h, q_cms, horizon_s):
    """The hydrograph plus a steady flow, held until horizon_s (for models that take a single
    inflow series, ANUGA)."""
    if h is None or q_cms <= 0:
        return h
    t = np.unique(np.concatenate([np.asarray(h.t_s, float), [max(horizon_s, h.t_s[-1] + 1.0)]]))
    q = np.interp(t, h.t_s, h.q_cms, right=0.0) + q_cms
    return brmod.Hydrograph(t, q, h.method, h.params, h.notes, h.t_ref_s)


def _safe(name):
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(name))


def _flood_hydrographs(dam, st, warnings):
    """(with-breach hydrograph, no-breach hydrograph, FloodResult). The first is
    None when the dam does not fail."""
    fi = st.flood
    if fi is None:
        raise brmod.BreachInputError("the flood scenario needs the inflow hydrograph")
    if st.breach_method == brmod.METHOD_USER:
        if not st.user_hydrograph or not fi.baseline_csv:
            raise brmod.BreachInputError(
                "with your own hydrographs the flood scenario needs both: the outflow with the "
                "breach and the outflow without it, each starting at breach initiation")
        hy = brmod.read_user_hydrograph(st.user_hydrograph)
        nb = floodmod.hydrograph_from_csv(fi.baseline_csv, "no-breach hydrograph")
        warnings.append("Your own hydrographs are used as given: time zero is breach initiation "
                        "in both.")
        return hy, nb, None
    fr = floodmod.route_flood(dam, st.breach_method, st.overrides, fi)
    if not fr.failed:
        return None, fr.no_breach, fr
    return fr.with_breach, fr.no_breach, fr


def _write_no_breach(res, report, warnings, model_dir, hy_nb):
    report["warnings"] = warnings
    report["run_finished"] = datetime.datetime.now().isoformat(timespec="seconds")
    res.summary = report["summary"]
    res.warnings = warnings
    if hy_nb is not None:
        hy_nb.to_csv(os.path.join(model_dir, "no_breach_hydrograph.csv"))
    res.report_json = os.path.join(model_dir, "run_report.json")
    with open(res.report_json, "w") as f:
        json.dump(report, f, indent=2, default=_json_default)
    res.report_md = os.path.join(model_dir, "run_report.md")
    from . import damjson
    with open(res.report_md, "w") as f:
        f.write("# Breach model run: {}\n\nThe dam does not fail in this flood.\n\n{}\n".format(
            res.dam_id, "\n".join("- " + w for w in warnings)))
        if report.get("dam_record"):
            f.write("\n" + "\n".join(damjson.report_lines(report["dam_record"])) + "\n")


def _record_dam(dam, st, res, dam_xy, out_folder, top_from_dem, final_model, warnings, log,
                grid=None, depth=None):
    """v3.2.0: write what the run used into <dam folder>/dam.json (damjson.record_run) and add
    its "modelled" status. Conflicts with another tool's values become warnings. Never fails
    the run."""
    from . import damjson
    dr = st.dam_record or {}
    try:
        top_source = "dem" if top_from_dem else dr.get("top_source", "parameter")
        vals = damjson.values_used(dam, st, top_source, dr.get("point"), dr.get("nid_id"))

        def rel(p):
            return os.path.relpath(p, out_folder).replace(os.sep, "/")
        breach = final_model != "none"
        result = {"breach_occurs": breach, "scenario": st.scenario,
                  "final_model": final_model,
                  "final_rasters": [rel(p) for p in res.final.values()],
                  "manifest": damjson.MANIFEST_REL, "run_report": damjson.REPORT_REL}
        if res.baseline:
            result["baseline_rasters"] = [rel(p) for p in res.baseline.values()]
        if breach and grid is not None and depth is not None:
            result["extent_ft"] = damjson.extent_ft(grid, depth, dam_xy, WET_M)
            w, h = terrain.bounds_size(grid.bounds)
            result["domain_mi"] = [round(w / MILE, 2), round(h / MILE, 2)]
        info = damjson.record_run(
            out_folder, vals, result, dr.get("read"),
            dem_top_note="highest DEM point within 30 m of the dam point" if top_from_dem
            else "")
    except Exception as e:           # the record is an extra; never lose the run for it
        info = {"error": "dam.json not updated: {}: {}".format(type(e).__name__, e),
                "warnings": [], "read": list(dr.get("read") or [])}
        info["warnings"].append(info["error"])
    for w in info.get("warnings") or []:
        log(w)
        if w not in warnings:
            warnings.append(w)
    return info


def _manifest_extra(report, summary, warnings):
    """The manifest's "extra" block. v3.4.0 adds the near-dam refine and the DEM conditioning
    (buildings) so the manifest says what the rasters were made with."""
    extra = {"warnings": len(warnings), "final_model": summary["final_model"]}
    extra["lisflood_solver"] = report["settings"].get("lisflood_solver", lfmod.SOLVER_ACC)
    r = report.get("refine_near_dam")
    if r is not None:
        extra["refine_near_dam"] = {
            "applied": bool(r.get("applied")), "reason": r.get("reason"),
            "dist_mi": r.get("dist_mi"), "fine_cell_m": r.get("fine_cell_m"),
            "solver": r.get("solver"), "cells_replaced": r.get("cells_replaced"),
            "receptors_inside": (r.get("counts") or {}).get("total")}
    c = report.get("dem_conditioning")
    if c is not None:
        extra["dem_conditioning"] = {"buildings": c["buildings"], "edits": c["edits"],
                                     "manning_edits": c["manning_edits"]}
    return extra


def _json_default(o):
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, np.bool_):
        return bool(o)
    return str(o)


def _clean(model_dir):
    for sub in ("lisflood", "lisflood_base", "anuga", "anuga_base"):
        d = os.path.join(model_dir, sub)
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            for fn in files:
                if fn.endswith((".asc", ".npz", ".msh", ".Vx", ".Vy", ".wd", ".Qx", ".Qy",
                                ".start")):
                    try:
                        os.remove(os.path.join(root, fn))
                    except OSError:
                        pass


def render_report(r):
    s = r["summary"]
    h = r["hydrograph"]
    lines = ["# Breach model run: {}".format(s["dam_id"]), "",
             "{} · {} · finished {}".format(r["tool"], r["policy"], r["run_finished"]), "",
             "## Result", "",
             "| | |", "|---|---|",
             "| Scenario | {} |".format("flood inflow (PMF) with and without the breach"
                                        if s.get("scenario") == SCENARIO_FLOOD else "sunny day"),
             "| Final rasters from | {} |".format(s["final_model"]),
             "| Peak breach outflow | {:,.0f} cfs ({}) |".format(s["peak_breach_cfs"],
                                                                s["breach_method"]),
             "| Storage at failure | {} ac-ft |".format(s["storage_at_failure_acft"]),
             "| Flooded area | {:,} ac ({:,} ac deeper than the arrival depth) |".format(
                 s["flooded_acres"], s["flooded_acres_over_arrival_depth"]),
             "| Maximum depth / velocity | {} ft / {} ft/s |".format(s["max_depth_ft"],
                                                                  s["max_velocity_fps"]),
             "| Latest arrival | {} h after breach initiation |".format(s["latest_arrival_hr"]),
             "| Domain | {} x {} mi after {} LISFLOOD-FP run(s) |".format(
                 s["domain_mi"][0], s["domain_mi"][1], s["lisflood_runs"])]
    if s.get("receptors_flooded"):
        lines.append("| Reaches | {} |".format(s["receptors_flooded"]))
    if s.get("refine_near_dam"):
        lines.append("| Near the dam (v3.4.0) | {} |".format(s["refine_near_dam"]))
    if s.get("buildings"):
        lines.append("| Buildings (v3.4.0) | {} |".format(s["buildings"]))
    lines.append("")
    if r.get("warnings"):
        lines += ["## Check these", ""] + ["- " + w for w in r["warnings"]] + [""]
    lines += ["## Breach (ND 5.1.1)", ""]
    for k in ("starting_wse_ft", "starting_wse_rule", "breach_bottom_ft", "hw_ft", "hb_ft",
              "storage_at_failure_acft", "storage_exponent_m", "breach_parameters",
              "failure_mode", "avg_breach_width_ft", "bottom_width_ft", "side_slope_h",
              "formation_time_hr", "pipe_initial_dia_ft", "pipe_collapse_dia_ft",
              "pipe_phase_hr", "tr60_peak_cfs", "tr60_rule", "breach_factor_br_ac",
              "embankment_area_ft2", "rise_time_hr", "base_time_hr", "shape",
              "froehlich_1995_peak_cfs", "tr60_peak_cfs_check", "tr60_rule_check", "peak_cfs",
              "time_to_peak_hr", "volume_acft", "source"):
        if k in h and h[k] is not None:
            lines.append("- {}: {}".format(k.replace("_", " "), h[k]))
    if "flood" in r and r["flood"]:
        lines += ["", "## Flood inflow scenario", ""]
        for k, v in r["flood"].items():
            lines.append("- {}: {}".format(k.replace("_", " "), v))
    if "incremental" in r:
        i = r["incremental"]
        lines += ["", "## Incremental effect of the breach", "",
                  "Area where the breach adds at least: " + ", ".join(
                      "{} — {} ac".format(k, v) for k, v in i["acres_at_or_above"].items()),
                  "", i["note"]]
        if "receptors_incremental" in r:
            lines += ["", "Receptors with an incremental depth of at least: " + "; ".join(
                "{} — {}".format(k, v) for k, v in r["receptors_incremental"].items())]
    lines += ["", "Breach inflow point: {:.1f}, {:.1f} ({}).".format(
        r["inflow_point"]["xy"][0], r["inflow_point"]["xy"][1], r["inflow_point"]["how"]), "",
        "## Downstream extent (ND 5.1.1(f))", "",
        "Each run checks the deepest water on every domain edge. An edge deeper than {} ft "
        "moves out by {} mi and the model runs again{}.".format(
            r["settings"]["edge_tolerance_ft"], r["settings"]["expand_step_mi"],
            "; with receptor layers an edge is left alone when no structure, road or rail lies "
            "in the flood's path within {} mi".format(r["settings"]["impact_look_ahead_mi"])
            if r["settings"].get("receptors") else ""), "",
        "| Run | Size (mi) | Cells | Hours | Edge depth N/S/E/W (ft) | Over | Minutes |",
        "|---|---|---|---|---|---|---|"]
    for it in r["lisflood_runs"]:
        e = it["edge_max_depth_ft"]
        lines.append("| {} | {} x {} | {:,} | {} | {}/{}/{}/{} | {} | {} |".format(
            it["run"], it["size_mi"][0], it["size_mi"][1], it["cells"], it["sim_time_hr"],
            e["N"], e["S"], e["E"], e["W"],
            ", ".join(it.get("edges_over_after_class_stop", it.get(
                "edges_over_after_receptors", it["edges_over_tolerance"]))) or "none",
            it["runtime_min"]))
    last = r["lisflood_runs"][-1]["stats"]
    lines += ["", "LISFLOOD-FP (last run): max volume error {}% of the breach volume, "
              "peak inflow {} cfs.".format(last.get("max_volume_error_pct"),
                                           last.get("peak_inflow_cfs")), ""]
    cs = r.get("class_stop_rule")
    if cs:
        lines += ["## Class-stop rule (v3.2.0)", "",
                  "An edge the flood leaves deeper than the tolerance is accepted when, along "
                  "the flow path near it, the breach flood's depth (or, with a no-breach "
                  "baseline, its rise over the baseline) stays below {} ft - ND Dam Hazard's "
                  "termination point (a station below the threshold confirmed by {} more; a "
                  "station every {} mi, deepest wet cell within {} ft) lies inside the domain "
                  "and every station from it to the edge is below - and no structure, road or "
                  "rail lies within the receptor swath beyond the edge. {}".format(
                      cs["threshold_ft"], cs["confirm"], cs["station_mi"], cs["search_ft"],
                      cs["note"]), ""]
        if not cs.get("decisions"):
            lines += ["No edge needed the rule (the flood stayed inside the domain or the "
                      "receptor rule decided).", ""]
        for run in cs.get("decisions") or []:
            for e, dec in sorted(run["edges"].items()):
                lines.append("- Run {}, {} edge: {}.".format(run["run"], e, dec["reason"]))
                tab = stoprule.station_table(dec)
                if tab and dec.get("accept"):
                    lines += [""] + tab + [""]
        lines.append("")
    if r.get("refine_near_dam") is not None:
        from . import refine as refmod
        lines += refmod.report_lines(r["refine_near_dam"])
    if r.get("dem_conditioning"):
        c = r["dem_conditioning"]
        lines += ["## DEM conditioning", "", "- " + c["buildings"]]
        for k, v in sorted(c.get("edits", {}).items()):
            lines.append("- {}: {:,} cells".format(k, v))
        for k, v in sorted(c.get("manning_edits", {}).items()):
            lines.append("- Manning's n {}: {:,} cells".format(k, v))
        lines += ["- " + c["note"], ""]
    if r.get("structures"):
        lines += ["## Structures", ""] + ["- " + x for x in r["structures"]] + [""]
    if r.get("receptors"):
        lines += ["## Receptors", ""]
        for k, v in r["receptors"].items():
            if isinstance(v, dict):
                v = ", ".join("{} {}".format(a.replace("_", " "), b) for a, b in v.items())
            lines.append("- {}: {}".format(k.replace("_", " "), v))
        lines.append("")
    if "anuga_runs" in r:
        lines += ["## ANUGA", ""]
        for a in r["anuga_runs"]:
            st = a["stats"]
            lines.append("- mesh buffer {} m: {:,} triangles, {} min, deepest water at the mesh "
                         "edge {} ft".format(a["buffer_m"], st.get("triangles", 0),
                                             st.get("runtime_min"), st.get("max_edge_depth_ft")))
        c = r.get("comparison", {})
        lines += ["", "Comparison with LISFLOOD-FP (cells deeper than the arrival depth): "
                  "LISFLOOD-FP {} ac, ANUGA {} ac, both {} ac, fit index F = {}; mean depth "
                  "difference (ANUGA - LISFLOOD-FP) {} ft, 90th percentile |difference| {} ft."
                  .format(c.get("lisflood_acres"), c.get("anuga_acres"), c.get("both_acres"),
                          c.get("fit_index_F"), c.get("depth_diff_mean_ft"),
                          c.get("depth_diff_abs_p90_ft")), ""]
    lines += ["## Outputs", ""]
    for kind, p in r["outputs"]["final"].items():
        lines.append("- {}: {}".format(kind, p))
    for kind, p in r["outputs"].get("baseline", {}).items():
        lines.append("- baseline {}: {}".format(kind, p))
    for kind, p in r["outputs"].get("increment", {}).items():
        lines.append("- incremental {}: {}".format(kind, p))
    for kind, p in r["outputs"].get("vectors", {}).items():
        lines.append("- {}: {}".format(kind.replace("_", " "), p))
    if r["outputs"].get("eap_map"):
        lines.append("- EAP inundation map preview (the EAP map is ND Dam Hazard's map book on "
                     "aerial imagery): {}".format(r["outputs"]["eap_map"]))
    elif r.get("eap_map_note"):
        lines.append("- EAP map: {}".format(r["eap_map_note"]))
    if r.get("dam_record"):
        from . import damjson
        lines += [""] + damjson.report_lines(r["dam_record"])
    lines += ["", "Units: {}.".format(s["units"]),
              "These rasters are the input for the ND hazard classification; this tool does "
              "not classify the dam.", ""]
    return "\n".join(lines)
