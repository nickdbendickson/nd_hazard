"""Run the dam-break flood model for every dam (several at once), with automatic domain handling."""
import os

import numpy as np
from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterFile, QgsProcessingParameterEnum,
                       QgsProcessingParameterNumber, QgsProcessingParameterBoolean,
                       QgsProcessingParameterRasterLayer, QgsProcessingParameterFolderDestination,
                       QgsProcessingParameterFeatureSink, QgsProcessingParameterField,
                       QgsFeature, QgsProcessingException, QgsWkbTypes,
                       QgsFeatureRequest, QgsRectangle)

from ..core import solver_io as sio, runner as rn, gridutil as gu
from ..core import flood_polygons as fp
from ..core import arrival as ar
from ..core import resume as rsm
from ..core import scenarios as sc
from ..core.proposed import PHRASE as PROPOSED_PHRASE
from . import _common as cm
from ..core import us_units as uu
from ..damsuite import solvegate as SG
from ..compat import (FILE_FILE, FILE_FOLDER, FT_DOUBLE, FT_INT, FT_STRING, GEOM_POLYGON, NUM_DOUBLE, NUM_INT, SRC_LINE, SRC_POINT, SRC_POLYGON)

NEW_FIELDS = [("scenario", FT_STRING), ("sim_status", FT_STRING),
              ("sim_engine", FT_STRING), ("sim_cell_m", FT_DOUBLE),
              ("sim_h", FT_DOUBLE), ("sim_run_s", FT_DOUBLE),
              ("edge_flag", FT_STRING), ("max_depth_m", FT_DOUBLE),
              ("max_vel_ms", FT_DOUBLE), ("max_dv_m2s", FT_DOUBLE),
              ("area_ha", FT_DOUBLE), ("depth_tif", FT_STRING),
              ("vel_tif", FT_STRING), ("dv_tif", FT_STRING),
              ("arrive_tif", FT_STRING), ("dv_kind", FT_STRING),
              ("attempts", FT_INT), ("radius_m", FT_DOUBLE),
              ("res_in_grid", FT_STRING), ("cascade", FT_STRING),
              ("n_cascade", FT_INT), ("sim_notes", FT_STRING),
              # v0.11: no-breach baseline / incremental flood, buildings as obstructions
              ("base_run", FT_STRING), ("base_cfs", FT_DOUBLE),
              ("base_max_m", FT_DOUBLE), ("incr_max_m", FT_DOUBLE),
              ("incr_tif", FT_STRING), ("buildings", FT_STRING),
              # 0.28.0: the second loading case (SCEN_BOTH). Named scen2_, not sd_, because the
              # chosen scenario can be either one: scen2 says which it was.
              ("scen2", FT_STRING), ("scen2_status", FT_STRING), ("scen2_edge_flag", FT_STRING),
              ("scen2_max_depth_m", FT_DOUBLE), ("scen2_max_vel_ms", FT_DOUBLE),
              ("scen2_max_dv_m2s", FT_DOUBLE), ("scen2_area_ha", FT_DOUBLE),
              ("scen2_depth_tif", FT_STRING), ("scen2_vel_tif", FT_STRING),
              ("scen2_dv_tif", FT_STRING), ("scen2_arrive_tif", FT_STRING),
              ("scen2_notes", FT_STRING)]
ENGINES = ["LISFLOOD-FP ACC (fast, local inertial)", "LISFLOOD-FP FV1 (full shallow water)",
           "Built-in (no install; hydrograph injection)",
           "Built-in, reservoir in the grid (gradual breach, like DSS-WISE)"]
TERRAIN = ["Cached clips only (tool 2)", "USGS 3DEP service", "Local DEM folder"]
SCENARIOS = ["td - top of dam (ND standard)", "sd - sunny day"]
# item 7: plain-language presets (cell_m, simh_max_h, simmax_h)
PRESETS = ["Quick screen (50 m grid, ~12 s/dam)", "Standard (20 m, ~2 min/dam)",
           "Detailed (10 m, ~15 min/dam)", "Custom (use settings below)"]
PRESET_VALS = [(50.0, 12.0, 48.0), (20.0, 24.0, 96.0), (10.0, 36.0, 144.0)]  # cell_m, simh, simmax
BUILDINGS = ["Not represented (bare terrain)", "Raise the DEM at building footprints",
             "Manning's n at building footprints"]                  # order = rn.BUILDING_MODES
BASE_KEYS = ("base_depth", "base_vel", "incr_depth")                 # extra rasters when BASELINE is on


def _polys_near(source, xy, radius):
    """Exterior rings (EPSG:5070 point lists) of the polygon features of `source` within
    `radius` of xy; point / line features are skipped.  QGIS objects: main thread only."""
    if source is None:
        return []
    ta, fa = cm.to_albers(source), cm.from_albers(source)
    box = fa.transformBoundingBox(QgsRectangle(xy[0] - radius, xy[1] - radius,
                                               xy[0] + radius, xy[1] + radius))
    out = []
    for f in source.getFeatures(QgsFeatureRequest().setFilterRect(box)):
        kind, parts = cm.albers_parts(f, ta)
        if kind == "polygon":
            out.extend(parts)
    return out


def _layer_overlap(source, cases, radius):
    """0.21.0: damsuite.solvegate.layer_overlap for the buildings layer: it must reach at least
    one dam's study box (EPSG:5070); no CRS and a wrong extent are told apart."""
    boxes = [(r["breach_xy"][0] - radius, r["breach_xy"][1] - radius,
              r["breach_xy"][0] + radius, r["breach_xy"][1] + radius)
             for r in cases if not r.get("skip") and r.get("breach_xy")]
    if not boxes:
        return SG.check(SG.OK, "Buildings layer", "no dam to run")
    try:
        crs = source.sourceCrs()
        valid = crs is not None and crs.isValid()
    except Exception:                                            # noqa
        valid, crs = True, None
    ext = None
    if valid and crs is not None:
        try:
            r = cm.to_albers(source).transformBoundingBox(source.sourceExtent())
            ext = (r.xMinimum(), r.yMinimum(), r.xMaximum(), r.yMaximum())
        except Exception:                                        # noqa
            ext = None
    try:
        n = int(source.featureCount())
    except Exception:                                            # noqa
        n = None
    union = (min(b[0] for b in boxes), min(b[1] for b in boxes),
             max(b[2] for b in boxes), max(b[3] for b in boxes))
    out = SG.layer_overlap("Buildings layer", valid, ext, union, n if n is not None and n >= 0 else None)
    if out["status"] == SG.OK and ext is not None and not any(SG.boxes_meet(ext, b) for b in boxes):
        out = SG.layer_overlap("Buildings layer", True, ext, boxes[0], n)
    return out


# 0.17.0: an extensionless executable (/opt/lisflood/lisflood) is refused by the default
# "All files (*.*)" filter of a file parameter with no extension ("Incorrect parameter value")
EXE_FILTER = "All files (*)"

class RunBreachSim(QgsProcessingAlgorithm):
    def name(self): return "runbreachsim"
    def displayName(self): return "7 - Run breach flood simulations"
    def group(self): return "Dam screening"
    def groupId(self): return "dams"
    def createInstance(self): return RunBreachSim()

    def shortHelpString(self):
        return ("Runs a 2D dam-break flood for every dam, several dams at once.\n"
                "ENGINES: LISFLOOD-FP ACC (fastest; needs the LISFLOOD-FP program), LISFLOOD-FP FV1, "
                "the built-in engine (nothing to install; explicit finite-volume shallow-water "
                "solver like DSS-WISE, exact maximum depth x velocity), or the built-in engine with "
                "the reservoir in the grid: the reservoir starts full at the top of dam, its bed "
                "under the LiDAR water surface is estimated and calibrated to the tool-3 storage, "
                "and the breach cells are lowered over the formation time. The first three inject "
                "the tool-5 hydrograph just below the dam.\n"
                "AUTOMATIC DOMAIN: cell size by reservoir size (0 = auto); only a corridor along the "
                "downstream flow path is modelled; if the flood reaches the corridor wall it is "
                "widened, if it reaches the terrain edge a larger clip is loaded (from 3DEP or the "
                "local folder when selected); if the flood is still spreading at the end, the run "
                "is extended.\n"
                "ROAD CROSSINGS: with stream lines (NHD flowlines) supplied, short road fills that "
                "block a stream are cut at the model resolution (culvert gap); DWR dam lines are "
                "never cut.\n"
                "CASCADE: other DWR dams reached by the flood are listed with the depth over their "
                "crest; with 'breach', an overtopped dam that is also in this batch fails too, its "
                "own hydrograph starting when the flood reaches it.\n"
                "Dams cleared by tool 6 (triage) are skipped unless picked for the audit. "
                "Resumes: dams with existing result rasters are read, not re-run.\n"
                "UNITS (0.18.0): the rasters the later steps read stay in metres (_depth.tif, _vel.tif, "
                "_dv.tif); every new run also writes feet companions next to them (_depth_ft.tif, "
                "_vel_fps.tif in ft/s, _dv_ft2s.tif in ft²/s) - open those to read depths in feet. The "
                "results table has a US twin (_ft, _fps, _ft2s, _ac, _acft, _cfs) of each metric field.\n"
                "NO-BREACH BASELINE (v0.11, off by default): screening models the breach flood only. "
                "With BASELINE on, a second run of the same domain releases only a steady ambient flow "
                "of BASELINE_CFS (a value YOU choose - the tool does not know the spillway or stream "
                "flow; 0 = dry baseline) at the breach site, with the dam intact, and writes "
                "<dam>_<scenario>_base_depth.tif (m), _base_vel.tif (m/s) and the increment "
                "_incr_depth.tif = breach depth - baseline depth (m, clipped at 0). Fields base_run, "
                "base_cfs, base_max_m, incr_max_m, incr_tif. Tool 8 can then count impacts from the "
                "increment (INCREMENTAL). This is a screening approximation of ND's flood-scenario "
                "comparison (breach vs no-breach flood); the real design-flood (PMF) routing is the "
                "Runner's / the engineer's. The baseline roughly doubles the run time of a dam.\n"
                "BUILDINGS (v0.11, off by default): LiDAR bare-earth terrain has no buildings. "
                "Optionally the cells whose centre lies inside a building footprint polygon (BLD; a "
                "point layer is ignored with a message) are raised by BUILDING_RAISE_FT (default 10 "
                "ft, YOUR value; water flows around the block) or get Manning's n = BUILDING_N "
                "(default 0.15, YOUR value, times the n multiplier; water passes, slowed). Breach "
                "injection cells, the modelled reservoir and the dam are never changed. Written to "
                "sim_notes and the 'buildings' field. Screening approximation; not run in a real QGIS.\n"
                "TERRAIN FOR LARGER CLIPS (0.28.0): the default is the USGS 3DEP service, not "
                "'cached clips only'. With cached clips only a flood that reaches the edge of the "
                "step 2 clip STOPS there, the run finishes, and the result reads like an answer: a "
                "3,937 ac-ft dam screened Medium with 0 buildings off a 3 km clip, and High with 27 "
                "buildings and 86 roads when the clip could grow. The edge_flag column says YES when "
                "it happened. PREGROW is on by default too, so the clip is sized from the downstream "
                "trace before the first run; switch it off for a large county (the clip then grows "
                "after a run that reaches the edge - a re-run, but it starts smaller).\n"
                "BOTH LOADING CASES (0.28.0, SCEN_BOTH, off by default): the chosen scenario and the "
                "other one are both modelled, each with its own rasters (<dam>_td_* and <dam>_sd_*), "
                "and the second one's summary goes in the scen2_ columns. Step 8 classifies both and "
                "the WORSE class governs: a hazard class follows the incremental consequences of a "
                "breach, and the sunny-day flood arrives on a dry day on a road people are driving "
                "on. It doubles the run time, so the window turns it on for a one-dam run and leaves "
                "it off for a county.\n"
                "RESUME (0.28.0): the output folder gets run_dams.json, one entry per dam, written as "
                "each dam finishes, so an interrupted county run says 'Resuming: 124 of 200 dam(s) "
                "already have a result' and models only the rest. A dam counts as done only while its "
                "rasters are on disk, and a FAILED dam is always tried again. SKIPDONE off re-runs "
                "every dam.\n"
                "FLOOD POLYGONS (0.26.0, on by default): one GeoPackage per run, 7_flood_polygons.gpkg in "
                "the output folder, with the layers extent (dam_id, scenario), velocity_class (vel_class: "
                "0-2 ft/s, 2-5 ft/s, over 5 ft/s), arrival (within_hr: 0.5, 1, 2, 4, 8 h) and, from "
                "0.28.0, arrival_contour: the flood front as LINES at 30 min, 1 h and 2 h, the "
                "times an emergency action plan is measured against. Wet means a "
                "depth of at least the suite's wet depth (0.1 ft). A dam run again replaces its own "
                "polygons.")

    def initAlgorithm(self, config=None):
        P = self.addParameter
        P(QgsProcessingParameterFeatureSource("DAMS", "Dams (output of tool 5 or tool 6)",
                                              [SRC_POINT]))
        P(QgsProcessingParameterFeatureSource("BPTS", "Breach points (output of tool 4)",
                                              [SRC_POINT]))
        P(QgsProcessingParameterFile("DEMDIR", "Folder of cached dam DEMs (tool 2)",
                                     behavior=FILE_FOLDER))
        # 0.28.0: USGS 3DEP, not "cached clips only". With cached clips only, a flood that reaches
        # the edge of the step 2 clip stops there and the run finishes looking finished: on a
        # 3,937 ac-ft dam that gave Medium with 0 buildings where a clip that could grow gave
        # High with 27 buildings and 86 roads. Choose "cached clips only" deliberately (offline,
        # or a clip you know is big enough), not by accident.
        P(QgsProcessingParameterEnum("TSRC", "Terrain for larger clips", TERRAIN, defaultValue=1))
        P(QgsProcessingParameterFile("LOCAL", "Local DEM folder (if selected above)",
                                     behavior=FILE_FOLDER, optional=True))
        P(QgsProcessingParameterEnum("ENGINE", "Flood engine", ENGINES, defaultValue=0))
        P(QgsProcessingParameterFile("EXE", "LISFLOOD-FP executable (LISFLOOD engines only)",
                                     behavior=FILE_FILE, fileFilter=EXE_FILTER, optional=True))
        P(QgsProcessingParameterEnum("SCEN", "Scenario", SCENARIOS, defaultValue=0))
        P(QgsProcessingParameterFile("CURVES", "Stage-storage curve folder (tool 3; reservoir-in-grid "
                                     "bed calibration)", behavior=FILE_FOLDER,
                                     optional=True))
        # item 7: preset; when not Custom, overrides cell size and sim times below
        P(QgsProcessingParameterEnum("PRESET", "Resolution preset", PRESETS, defaultValue=0))
        cm.distance_params(P, "CELL_FT", "CELL", "Custom: model cell size (ft), 0 = auto by reservoir size",
                           33.0, min_ft=0.0)
        P(QgsProcessingParameterNumber("SIMH", "Custom: simulated time (h), 0 = automatic",
                                       NUM_DOUBLE, 12.0, minValue=0.0))
        P(QgsProcessingParameterNumber("SIMMAX", "Custom: longest allowed simulated time (h)",
                                       NUM_DOUBLE, 96.0, minValue=1.0))
        P(QgsProcessingParameterBoolean("CORR", "Model only a corridor along the flow path",
                                        defaultValue=True))
        cm.distance_params(P, "CORRW_FT", "CORRW", "Starting corridor half-width (ft)", 2000.0, min_ft=150.0)
        cm.distance_params(P, "RMAX_FT", "RMAX", "Largest terrain clip half-width (ft)", 40000.0, min_ft=1500.0)
        # v0.20.0: opt-in (it was always on from 0.15.0). 0.28.0: on by default - the clip is
        # sized from the valley before a flood is paid for that can only hit the edge. It costs
        # run time; switch it off for a large county and let the clip grow after a run instead.
        P(QgsProcessingParameterBoolean("PREGROW", "Enlarge the clip before the flood run when the "
                                        "downstream trace reaches its edge (otherwise it grows "
                                        "after a run that reaches the edge)", defaultValue=True))
        # 0.28.0: both loading cases in one run (core/scenarios.py)
        P(QgsProcessingParameterBoolean("SCEN_BOTH", "Also run the other scenario (top of dam AND "
                                        "sunny day): the worse class governs", defaultValue=False))
        # 0.28.0: resume (core/resume.py) - the switch that forces a county to be modelled again
        P(QgsProcessingParameterBoolean("SKIPDONE", "Skip dams that already have a result (resume "
                                        "an interrupted run); off re-runs every dam",
                                        defaultValue=True))
        P(QgsProcessingParameterFeatureSource("STREAMS", "Stream lines for road-crossing cuts "
                                              "(NHD flowlines, optional)", [SRC_LINE],
                                              optional=True))
        P(QgsProcessingParameterFeatureSource("ROADCUT", "Only cut where these roads cross (optional)",
                                              [SRC_LINE], optional=True))
        P(QgsProcessingParameterFeatureSource("DAMLINES", "DWR dam lines (protect from cuts; cascade; "
                                              "reservoir-in-grid)", [SRC_LINE],
                                              optional=True))
        P(QgsProcessingParameterField("LID", "Dam lines: structure ID field", defaultValue="structure_id",
                                      parentLayerParameterName="DAMLINES", optional=True))
        P(QgsProcessingParameterEnum("CASCADE", "Downstream dams reached by the flood",
                                     ["ignore", "flag only", "flag and breach them too"], defaultValue=1))
        P(QgsProcessingParameterRasterLayer("NLCD", "NLCD land cover for Manning n (optional)",
                                            optional=True))
        P(QgsProcessingParameterNumber("NDEF", "Default Manning n", NUM_DOUBLE,
                                       0.05, minValue=0.01, maxValue=0.3))
        P(QgsProcessingParameterNumber("NMULT", "Manning n multiplier (sensitivity)",
                                       NUM_DOUBLE, 1.0, minValue=0.2, maxValue=5))
        P(QgsProcessingParameterNumber("WORKERS", "Dams to run at the same time",
                                       NUM_INT,
                                       max((os.cpu_count() or 2) - 1, 1), minValue=1, maxValue=64))
        P(QgsProcessingParameterNumber("TMO", "Time limit per dam (minutes)",
                                       NUM_DOUBLE, 120.0, minValue=1.0))
        P(QgsProcessingParameterBoolean("SKIPTRI", "Skip dams cleared by triage (tool 6)",
                                        defaultValue=True))
        P(QgsProcessingParameterBoolean("KEEP", "Keep raw solver files", defaultValue=False))
        # 0.26.0: flood polygons (extent, velocity class, arrival) in one GeoPackage per run
        P(QgsProcessingParameterBoolean("WRITE_POLYGONS", "Also write flood polygons (extent, velocity "
                                        "class, arrival) to 7_flood_polygons.gpkg", defaultValue=True))
        P(QgsProcessingParameterBoolean("BASELINE", "Also run a no-breach baseline (steady ambient flow, "
                                        "dam intact) and write the incremental depth", defaultValue=False))
        P(QgsProcessingParameterNumber("BASELINE_CFS", "Baseline: steady ambient flow through the dam site "
                                       "(cfs; YOUR value, 0 = dry baseline)",
                                       NUM_DOUBLE, 0.0, minValue=0.0))
        P(QgsProcessingParameterEnum("BUILDINGS", "Buildings in the terrain", BUILDINGS, defaultValue=0))
        P(QgsProcessingParameterFeatureSource("BLD", "Building footprints (polygons; points are ignored)",
                                              [SRC_POLYGON, SRC_POINT],
                                              optional=True))
        P(QgsProcessingParameterNumber("BUILDING_RAISE_FT", "Buildings: raise the DEM by (ft; YOUR value)",
                                       NUM_DOUBLE, 10.0, minValue=0.0))
        P(QgsProcessingParameterNumber("BUILDING_N", "Buildings: Manning's n at footprints (YOUR value)",
                                       NUM_DOUBLE, 0.15, minValue=0.01, maxValue=1.0))
        P(QgsProcessingParameterFolderDestination("OUTDIR", "Output folder for result rasters"))
        P(QgsProcessingParameterFeatureSink("OUT", "Simulation results", SRC_POINT))

    # ------------------------------------------------------------------
    def processAlgorithm(self, params, context, feedback):
        dams = self.parameterAsSource(params, "DAMS", context)
        bpts = self.parameterAsSource(params, "BPTS", context)
        demdir = self.parameterAsString(params, "DEMDIR", context)
        tsrc = ["cache", "3dep", "local"][self.parameterAsEnum(params, "TSRC", context)]
        local = self.parameterAsString(params, "LOCAL", context)
        engine = rn.ENGINES[self.parameterAsEnum(params, "ENGINE", context)]
        exe = self.parameterAsString(params, "EXE", context)
        scen = ["td", "sd"][self.parameterAsEnum(params, "SCEN", context)]
        # 0.28.0: the second loading case, and the resume switch
        both = self.parameterAsBool(params, "SCEN_BOTH", context)
        scen2 = (sc.SECOND if scen == sc.PRIMARY else sc.PRIMARY) if both else None
        skipdone = self.parameterAsBool(params, "SKIPDONE", context)
        curves = self.parameterAsString(params, "CURVES", context)
        preset_idx = self.parameterAsEnum(params, "PRESET", context)
        if preset_idx < len(PRESET_VALS):                       # item 7: Quick / Standard / Detailed
            cell, simh, simmax = PRESET_VALS[preset_idx]
            feedback.pushInfo("preset: {} (cell {:.0f} m, simh {:.0f} h, simmax {:.0f} h)".format(
                PRESETS[preset_idx], cell, simh, simmax))
        else:                                                    # Custom
            cell = cm.distance_m(self, params, context, "CELL_FT", "CELL", 33.0)
            simh = self.parameterAsDouble(params, "SIMH", context)
            simmax = self.parameterAsDouble(params, "SIMMAX", context)
        corr = self.parameterAsBool(params, "CORR", context)
        pregrow = self.parameterAsBool(params, "PREGROW", context)
        corrw = cm.distance_m(self, params, context, "CORRW_FT", "CORRW", 2000.0)
        rmax = cm.distance_m(self, params, context, "RMAX_FT", "RMAX", 40000.0)
        streams = self.parameterAsSource(params, "STREAMS", context)
        roadcut = self.parameterAsSource(params, "ROADCUT", context)
        damlines = self.parameterAsSource(params, "DAMLINES", context)
        lid = self.parameterAsString(params, "LID", context) or None
        cascade = ["off", "flag", "breach"][self.parameterAsEnum(params, "CASCADE", context)]
        nlcd = self.parameterAsRasterLayer(params, "NLCD", context)
        ndef = self.parameterAsDouble(params, "NDEF", context)
        nmult = self.parameterAsDouble(params, "NMULT", context)
        workers = self.parameterAsInt(params, "WORKERS", context)
        tmo = self.parameterAsDouble(params, "TMO", context) * 60.0
        skiptri = self.parameterAsBool(params, "SKIPTRI", context)
        keep = self.parameterAsBool(params, "KEEP", context)
        write_polys = self.parameterAsBool(params, "WRITE_POLYGONS", context)      # 0.26.0
        baseline = self.parameterAsBool(params, "BASELINE", context)
        base_cfs = self.parameterAsDouble(params, "BASELINE_CFS", context)
        bmode = rn.BUILDING_MODES[self.parameterAsEnum(params, "BUILDINGS", context)]
        bld = self.parameterAsSource(params, "BLD", context)
        raise_m = self.parameterAsDouble(params, "BUILDING_RAISE_FT", context) * rn.FT
        bld_n = self.parameterAsDouble(params, "BUILDING_N", context)
        outdir = self.parameterAsString(params, "OUTDIR", context)
        if bmode != "none" and bld is None:
            feedback.reportError("Buildings: no footprint layer (BLD) given - bare terrain is used.")
            bmode = "none"
        if bmode != "none" and QgsWkbTypes.geometryType(bld.wkbType()) != GEOM_POLYGON:
            feedback.reportError("Buildings: the footprint layer is not a polygon layer - points/lines are "
                                 "ignored; bare terrain is used.")
            bmode = "none"
        if baseline:
            feedback.pushInfo(f"No-breach baseline on: steady ambient flow {base_cfs:g} cfs at the dam site "
                              "(screening approximation of ND's flood-scenario comparison; roughly doubles "
                              "the run time)")
        if engine.startswith("lisflood") and not (exe and os.path.exists(exe)):
            raise QgsProcessingException("The LISFLOOD-FP engines need the LISFLOOD-FP executable; "
                                         "choose a built-in engine or give the executable.")
        os.makedirs(outdir, exist_ok=True)
        work = os.path.join(outdir, "_work"); os.makedirs(work, exist_ok=True)
        # 0.17.0: the local mosaic is named by its tile list (a changed folder gets a new one) and
        # logs feet / untagged / mixed-CRS tiles
        loader = cm.TerrainLoader(demdir, tsrc, local, work, log=feedback.pushInfo)
        nlcd_src = nlcd.source() if nlcd is not None else None

        names = dams.fields().names()
        get = lambda f, k, c=None: cm.fval(f, names, k, c)
        b2a = cm.to_albers(bpts)
        bmap = {}
        for b in bpts.getFeatures():
            g = b.geometry(); g.transform(b2a); p = cm.rep_point(g)
            if p is None:
                continue
            az = b["flow_az"]
            bmap[str(b["dam_id"])] = (p, None if az in (None, "") else float(az))
        if lid and damlines is not None and lid not in damlines.fields().names():
            lid = None

        fields, put = cm.extend_fields(dams.fields(), NEW_FIELDS)
        sink, dest = cm.us_sink(self, params, "OUT", context, fields, dams.wkbType(),
                                          dams.sourceCrs())

        # ---- gather everything each worker needs (QGIS objects stay on this thread)
        feats, cases, hydro, hydro2 = {}, [], {}, {}
        for f in dams.getFeatures():
            dam_id = str(get(f, "dam_id") or f.id())
            feats[dam_id] = f
            csvp = get(f, f"{scen}_csv")
            if csvp and os.path.exists(str(csvp)):
                hydro[dam_id] = sio.read_hydrograph_csv(str(csvp))
            if scen2:                                   # 0.28.0: the other scenario's hydrograph
                csvp = get(f, f"{scen2}_csv")
                if csvp and os.path.exists(str(csvp)):
                    hydro2[dam_id] = sio.read_hydrograph_csv(str(csvp))
        for dam_id, f in feats.items():
            rec = {"dam_id": dam_id, "skip": None}
            tri = str(get(f, "tri_result") or "")
            nf, nf_why = cm.no_flood(f, dams.fields().names())
            ex, ex_why = cm.excluded(f, dams.fields().names())
            if ex:                         # 0.13: the Review tab excluded this dam
                rec["skip"] = "excluded"; rec["why"] = ex_why
            elif nf:                       # tool 16: nothing above the natural ground -> nothing floods
                rec["skip"] = "noflood"; rec["why"] = nf_why
            elif skiptri and tri == "clear" and str(get(f, "tri_audit") or "") != "yes":
                rec["skip"] = "triage"
            elif dam_id not in hydro:
                rec["skip"] = f"no {scen} hydrograph (tool 5 skipped this dam)"
            elif dam_id not in bmap or bmap[dam_id][1] is None:
                rec["skip"] = "no breach point / flow direction (tool 4)"
            if rec["skip"]:
                cases.append(rec); continue
            bxy, az = bmap[dam_id]
            t, q = hydro[dam_id]
            reach = rmax + 500.0
            st = [a for _, arrs in cm.lines_near(streams, bxy, reach) for a in arrs] if streams else []
            rc = [a for _, arrs in cm.lines_near(roadcut, bxy, reach) for a in arrs] if roadcut else None
            dl = cm.lines_near(damlines, bxy, reach, lid) if damlines is not None else []
            own_sid = dam_id[4:] if dam_id.startswith("DWR-") else None
            own, others = None, []
            for key, arrs in dl:
                k = str(key).split(".")[0]
                if own_sid is not None and k == own_sid and own is None:
                    own = max(arrs, key=len)
                    continue
                od = {"dam_id": f"DWR-{k}", "line_xy": max(arrs, key=len)}
                if od["dam_id"] in bmap and od["dam_id"] in hydro and bmap[od["dam_id"]][1] is not None:
                    of = feats[od["dam_id"]]
                    od.update({"breach_xy": bmap[od["dam_id"]][0], "flow_az": bmap[od["dam_id"]][1],
                               "t": hydro[od["dam_id"]][0], "q": hydro[od["dam_id"]][1],
                               "hb_m": get(of, f"{scen}_hb_m", float), "bb_m": get(of, f"{scen}_bb_m", float)})
                others.append(od)
            if own is None and dl:                  # no ID match: nearest line within 300 m
                best = min(((np.min(np.hypot(a[:, 0] - bxy[0], a[:, 1] - bxy[1])), a)
                            for _, arrs in dl for a in arrs), key=lambda x: x[0])
                if best[0] < 300: own = best[1]
            protect = [o["line_xy"] for o in others] + ([own] if own is not None else [])
            pool = get(f, "est_pool_m", float)
            v_below = None
            if curves and pool is not None:
                cp = os.path.join(curves, f"curve_{dam_id}.csv")
                if os.path.exists(cp):
                    d = np.loadtxt(cp, delimiter=",", skiprows=1, ndmin=2)
                    v_below = float(np.interp(pool, d[:, 0], d[:, 1]))
            tf_min = get(f, f"{scen}_tf_min", float)
            rec["_bld"] = _polys_near(bld, bxy, reach) if bmode != "none" else None
            rec.update({"breach_xy": bxy, "flow_az": az, "t": t, "q": q,
                        "hb_m": get(f, f"{scen}_hb_m", float), "bb_m": get(f, f"{scen}_bb_m", float),
                        "bavg_m": get(f, f"{scen}_bavg_m", float), "vw_m3": get(f, f"{scen}_vw_m3", float),
                        "tf_s": tf_min * 60.0 if tf_min else None,
                        "start_elev": get(f, f"{scen}_start_m", float), "invert": get(f, "base_m", float),
                        "dam_line_xy": own, "pool_elev": pool, "v_below_m3": v_below,
                        "others": others if cascade != "off" else [],
                        "_streams": st, "_protect": protect, "_roads": rc})
            if scen2 and dam_id in hydro2:               # 0.28.0: what the second run changes
                t2, q2 = hydro2[dam_id]
                tf2_min = get(f, f"{scen2}_tf_min", float)
                rec["_second"] = {"t": t2, "q": q2,
                                  "hb_m": get(f, f"{scen2}_hb_m", float),
                                  "bb_m": get(f, f"{scen2}_bb_m", float),
                                  "bavg_m": get(f, f"{scen2}_bavg_m", float),
                                  "vw_m3": get(f, f"{scen2}_vw_m3", float),
                                  "tf_s": tf2_min * 60.0 if tf2_min else None,
                                  "start_elev": get(f, f"{scen2}_start_m", float)}
            elif scen2:
                rec["_second_why"] = "no {} hydrograph (tool 5 was not run for it)".format(scen2)
            cases.append(rec)

        # 0.21.0: pre-run check (damsuite.solvegate) - a broken LISFLOOD-FP stops the tool before
        # the first dam; a dam whose hydrograph is empty or whose breach point is off the terrain
        # fails on its own (the other dams run)
        if engine.startswith("lisflood"):
            smoke = SG.lisflood_smoke(exe)
            feedback.pushInfo("Pre-run check: " + SG.lines([smoke])[0])
            if SG.failures([smoke]):
                raise QgsProcessingException(SG.message([smoke]))
        for rec in cases:
            if rec.get("skip"):
                continue
            checks = [SG.hydro_stats(rec["t"], rec["q"])]
            try:
                on, why = loader.covers_point(rec["dam_id"], rec["breach_xy"])
            except Exception as e:                               # noqa - not known: not blamed
                on, why = None, str(e)
            if on is False:
                checks.append(SG.check(SG.FAIL, "DEM covers the dam", why,
                                       "Run tool 2 for this dam or give terrain that covers it."))
            for line in SG.lines(checks):
                feedback.pushInfo("{}: pre-run check: {}".format(rec["dam_id"], line))
            if SG.failures(checks):
                rec["skip"] = SG.message(checks)
        if bld is not None:
            ov = _layer_overlap(bld, cases, rmax)
            feedback.pushInfo("Pre-run check: " + SG.lines([ov])[0])
            if SG.failures([ov]):
                raise QgsProcessingException(SG.message([ov]))

        opts = dict(engine=engine, exe=exe, cell="auto" if cell <= 0 else cell, corridor=corr,
                    corridor_hw_m=corrw, radius_m=None, radius_max_m=rmax,
                    sim_s=simh * 3600.0 if simh > 0 else None, sim_max_s=simmax * 3600.0,
                    manning_default=ndef, n_mult=nmult, timeout_s=tmo,
                    cascade="breach" if cascade == "breach" else "flag", workdir=work, keep=keep,
                    threads_per_run=1 if workers > 1 else max((os.cpu_count() or 2) - 1, 1),
                    baseline=baseline, baseline_cfs=base_cfs, buildings=bmode,
                    building_raise_m=raise_m, building_n=bld_n, trace_grow=pregrow)

        def paths(dam_id, scen_key=None):
            b = os.path.join(outdir, f"{dam_id}_{scen_key or scen}")
            return {k: f"{b}_{k}.tif" for k in ("depth", "vel", "dv", "arrive") + BASE_KEYS}

        MAIN = ("depth", "vel", "dv", "arrive")

        def us_path(p):
            """<...>_depth.tif -> <...>_depth_ft.tif, _vel.tif -> _vel_fps.tif, _dv.tif -> _dv_ft2s.tif"""
            for suf, us in (("_base_depth.tif", "_base_depth_ft.tif"), ("_incr_depth.tif", "_incr_depth_ft.tif"),
                            ("_depth.tif", "_depth_ft.tif"), ("_vel.tif", "_vel_fps.tif"), ("_dv.tif", "_dv_ft2s.tif")):
                if p.endswith(suf):
                    return p[:-len(suf)] + us
            return p[:-4] + "_us.tif"

        def run_scenario(case, scen_key, with_baseline):
            """One scenario of one dam: re-read when its rasters are there (and SKIPDONE is on),
            else run the model and write them. `case` is what core/runner.simulate takes - for
            the second loading case the same dam with the other scenario's hydrograph and breach
            (0.28.0). Worker thread: no feedback, no QGIS objects."""
            dam_id = case["dam_id"]
            pth = paths(dam_id, scen_key)
            need = MAIN + (BASE_KEYS if with_baseline else ())
            if skipdone and all(os.path.exists(pth[k]) for k in need):
                out = {"dam_id": dam_id, "status": "cached", "notes": [], "cascade": [], "base_run": False}
                for k in MAIN:
                    out[k if k != "arrive" else "arrive_h"], out["gt"] = cm.read_tif(pth[k])
                out["cell_m"] = abs(out["gt"][1])
                if all(os.path.exists(pth[k]) for k in BASE_KEYS):      # baseline rasters of an earlier run
                    for k in BASE_KEYS:
                        out[k] = cm.read_tif(pth[k])[0]
                    out["base_run"] = True
                return out
            radius0 = 1500.0
            cached = loader._cached(dam_id)
            if cached:
                ds = cm.gdal_open(cached); g = ds.GetGeoTransform()
                radius0 = 0.5 * min(ds.RasterXSize * g[1], ds.RasterYSize * abs(g[5])) - 50.0
                dem_cell = abs(g[1])
            else:
                dem_cell = 1.0

            def load_dem(center, half, c):
                return loader.load(dam_id, center, half, c, case["_streams"], case["_protect"],
                                   case["_roads"])

            def load_man(gt, shape):
                if nlcd_src is None:
                    return None
                return sio.manning_from_nlcd(cm.warp_to_grid(nlcd_src, gt, shape), ndef)
            r0 = min(max(radius0, 300.0), rmax)
            # with cached clips only there is no terrain beyond the clip: do not try to grow
            o = dict(opts, radius_m=r0, radius_max_m=rmax if tsrc != "cache" else r0, dem_cell=dem_cell,
                     bld_polys=case.get("_bld"), baseline=with_baseline)
            logs = []                       # feedback is only touched on the main thread
            r = rn.simulate(case, load_dem, load_man, o, log=logs.append,
                            cancel=feedback.isCanceled)
            r["log"] = logs
            if r["status"] == "ok":
                for k, key in (("depth", "depth"), ("vel", "vel"), ("dv", "dv"), ("arrive", "arrive_h")):
                    cm.write_tif(pth[k], r[key], r["gt"], nodata=gu.NODATA if k == "arrive" else None)
                if r.get("base_run"):
                    for k in BASE_KEYS:
                        cm.write_tif(pth[k], r[k], r["gt"])
                # 0.18.0: the same rasters in feet, ft/s and ft2/s next to the metric ones
                for k, key, fac in (("depth", "depth", uu.FT), ("vel", "vel", uu.FT), ("dv", "dv", uu.FT * uu.FT)):
                    cm.write_tif(us_path(pth[k]), np.asarray(r[key], float) / fac, r["gt"])
                if r.get("base_run"):
                    for k in ("base_depth", "incr_depth"):
                        cm.write_tif(us_path(pth[k]), np.asarray(r[k], float) / uu.FT, r["gt"])
            return r

        def worker(rec):
            dam_id = rec["dam_id"]
            if rec.get("skip"):
                if rec["skip"] == "noflood":
                    return {"dam_id": dam_id, "status": "noflood",
                            "notes": ["no breach flood: " + rec.get("why", "")]}
                if rec["skip"] == "excluded":
                    return {"dam_id": dam_id, "status": "excluded", "notes": [rec.get("why", "")]}
                return {"dam_id": dam_id, "status": "triage" if rec["skip"] == "triage" else "FAILED",
                        "notes": [] if rec["skip"] == "triage" else [rec["skip"]]}
            r = run_scenario(rec, scen, baseline)
            if scen2 and r.get("status") in ("ok", "cached"):
                # 0.28.0: the second loading case. Its own rasters, its own paths; the no-breach
                # baseline belongs to the chosen scenario and is not repeated.
                if rec.get("_second"):
                    s = run_scenario(dict(rec, **rec["_second"]), scen2, False)
                    s["scenario"] = scen2
                    r["second"] = s
                    r.setdefault("log", []).extend(s.get("log") or [])
                else:
                    r["second"] = {"dam_id": dam_id, "scenario": scen2, "status": "FAILED",
                                   "notes": [rec.get("_second_why") or "nothing to run"]}
            return r

        total = len(cases) or 1
        done = [0]
        counts = {"ok": 0, "cached": 0, "triage": 0, "noflood": 0, "excluded": 0, "FAILED": 0, "cancelled": 0}
        # 0.26.0: the flood polygons of every dam of this run go to one GeoPackage, written on the
        # main thread (on_done) only - never in a worker
        poly_gpkg = os.path.join(outdir, fp.GPKG_NAME)
        poly_logged = [False]
        if write_polys and not fp.available():
            feedback.pushWarning("Flood polygons not written: this GDAL cannot polygonize or write a GeoPackage.")
            write_polys = False

        def write_polygons(dam_id, r, scen_key=None):
            scen_key = scen_key or scen
            try:
                n = fp.write_flood_polygons(poly_gpkg, dam_id, scen_key, r["depth"], r["vel"],
                                            r["arrive_h"], r["gt"])
            except Exception as e:                               # noqa - the rasters are the result; say why
                feedback.pushWarning("dam {}: flood polygons not written ({})".format(dam_id, e))
                return
            if not poly_logged[0]:
                feedback.pushInfo("Flood polygons (extent, velocity class, arrival, arrival contours at "
                                  "{}; wet = depth >= {:g} ft): {}".format(
                                      ", ".join(ar.label_for(h) for h in ar.CONTOUR_HOURS),
                                      fp.DEFAULT_WET_M / fp.FT, poly_gpkg))
                poly_logged[0] = True
            feedback.pushInfo("dam {} ({}): flood polygons: {} extent, {} velocity class, {} arrival, "
                              "{} arrival contour feature(s){}".format(
                                  dam_id, scen_key, n.get(fp.LAYER_EXTENT, 0), n.get(fp.LAYER_VEL, 0),
                                  n.get(fp.LAYER_ARRIVAL, 0), n.get(ar.LAYER_CONTOUR, 0),
                                  " (an earlier run's replaced)" if n.get("deleted") else ""))

        def on_done(rec, r):
            done[0] += 1
            feedback.setProgress(100.0 * done[0] / total)
            for m in r.get("log") or []:
                feedback.pushInfo(m)
            f = feats[rec["dam_id"]]
            a = cm.blank_attrs(fields, f)
            st = r.get("status", "FAILED")
            counts[st] = counts.get(st, 0) + 1
            put(a, "scenario", scen); put(a, "sim_status", st)
            put(a, "sim_engine", engine if st in ("ok", "cached") else None)
            notes = list(r.get("notes") or [])
            carried = " ".join(str(get(f, k) or "") for k in (
                "stor_notes", "status", "source", "brch_notes"))
            if PROPOSED_PHRASE in carried and PROPOSED_PHRASE not in " ".join(notes):
                notes.insert(0, PROPOSED_PHRASE)
            put(a, "sim_notes", "; ".join(notes))
            if st in ("ok", "cached"):
                pth = paths(rec["dam_id"])
                depth, vel, dv = r["depth"], r["vel"], r["dv"]
                cellm = r.get("cell_m") or abs(r["gt"][1])
                valid = depth > gu.NODATA + 1
                edge = r.get("edge_flag")
                if edge is None:
                    edge = bool(sio.edge_flooded(np.where(valid, depth, 0)))
                put(a, "sim_cell_m", cellm); put(a, "sim_h", (r.get("sim_s") or 0) / 3600.0 or None)
                put(a, "sim_run_s", r.get("run_s")); put(a, "edge_flag", "YES" if edge else "no")
                put(a, "max_depth_m", float(np.max(depth))); put(a, "max_vel_ms", float(np.max(vel)))
                put(a, "max_dv_m2s", float(np.max(dv)))
                put(a, "area_ha", float((depth > 0.05).sum() * cellm * cellm / 1e4))
                for k in ("depth", "vel", "dv", "arrive"):
                    put(a, f"{k}_tif", pth[k])
                if write_polys and r.get("arrive_h") is not None:          # 0.26.0: main thread
                    write_polygons(rec["dam_id"], r)
                put(a, "dv_kind", r.get("dv_kind")); put(a, "attempts", r.get("attempts"))
                put(a, "radius_m", r.get("radius_m"))
                put(a, "res_in_grid", "yes" if r.get("reservoir_in_grid") else "no")
                # v0.11 no-breach baseline / increment (screening approximation of ND's flood-scenario
                # comparison) and buildings
                if r.get("base_run"):
                    put(a, "base_run", "yes")
                    put(a, "base_cfs", r.get("baseline_cfs", base_cfs if baseline else None))
                    put(a, "base_max_m", float(np.max(r["base_depth"])))
                    put(a, "incr_max_m", float(np.max(r["incr_depth"])))
                    put(a, "incr_tif", pth["incr_depth"])
                else:
                    put(a, "base_run", "no")
                b_ = r.get("buildings")
                put(a, "buildings", (f"{b_['mode']}: {b_['cells']} cells" if b_ and b_["mode"] != "none"
                                     else "none") if st == "ok" else None)
                cas = r.get("cascade") or []
                put(a, "n_cascade", len(cas))
                put(a, "cascade", "; ".join(
                    f"{c['dam_id']} overtopped {c['depth_over_crest_m']:.2f} m at "
                    f"{(c['arrival_h'] or 0):.2f} h{' (breached)' if c['breached'] else ''}" for c in cas))
            elif st == "FAILED":
                feedback.reportError(f"dam {rec['dam_id']}: {'; '.join(r.get('notes') or [])}")
            # 0.28.0: the second loading case, on the same row (core/scenarios.py: the worse
            # class governs, and step 8 is the one that knows the classes)
            sec = r.get("second")
            if scen2:
                put(a, "scen2", scen2)
                put(a, "scen2_status", (sec or {}).get("status") or "not run")
                put(a, "scen2_notes", "; ".join((sec or {}).get("notes") or []))
            if sec and sec.get("status") in ("ok", "cached"):
                spth = paths(rec["dam_id"], scen2)
                sdepth, svel, sdv = sec["depth"], sec["vel"], sec["dv"]
                scell = sec.get("cell_m") or abs(sec["gt"][1])
                sedge = sec.get("edge_flag")
                if sedge is None:
                    sedge = bool(sio.edge_flooded(np.where(sdepth > gu.NODATA + 1, sdepth, 0)))
                put(a, "scen2_edge_flag", "YES" if sedge else "no")
                put(a, "scen2_max_depth_m", float(np.max(sdepth)))
                put(a, "scen2_max_vel_ms", float(np.max(svel)))
                put(a, "scen2_max_dv_m2s", float(np.max(sdv)))
                put(a, "scen2_area_ha", float((sdepth > 0.05).sum() * scell * scell / 1e4))
                for k in ("depth", "vel", "dv", "arrive"):
                    put(a, "scen2_{}_tif".format(k), spth[k])
                if write_polys and sec.get("arrive_h") is not None:
                    write_polygons(rec["dam_id"], sec, scen2)
            elif sec:
                feedback.pushWarning("dam {}: the {} scenario did not run: {}".format(
                    rec["dam_id"], scen2, "; ".join(sec.get("notes") or []) or "no reason given"))
            # 0.28.0: the resume manifest - written per dam, so an interrupted county run can be
            # picked up where it stopped (core/resume.py)
            try:
                rasters = [paths(rec["dam_id"])[k] for k in MAIN] if st in ("ok", "cached") else []
                rsm.record(outdir, rec["dam_id"], st, scenario=scen, rasters=rasters,
                           note="; ".join(notes), extra={"scen2": scen2 or "",
                                                         "scen2_state": (sec or {}).get("status") or ""})
            except Exception as e:                               # noqa - a lost note, not a lost run
                feedback.pushWarning("dam {}: the run manifest was not written ({})".format(
                    rec["dam_id"], e))
            o = QgsFeature(fields); o.setGeometry(f.geometry()); o.setAttributes(a)
            sink.addFeature(o)

        feedback.pushInfo(f"{len(cases)} dams, engine {engine}, {workers} at a time")
        if scen2:
            feedback.pushInfo("Both loading cases: {} and {}. {}".format(
                sc.SHORT[scen], sc.SHORT[scen2], sc.INCREMENTAL_RULE))
        # 0.28.0: resume. The manifest is what the last run got through; the rasters still decide.
        was = rsm.summary_text(outdir, total=len(cases))
        if was:
            feedback.pushInfo("Resuming: " + was if skipdone else
                              "Earlier run: " + was + " They are being RE-RUN (skip-done is off).")
        rn.run_batch(cases, worker, workers=workers, cancel=feedback.isCanceled, on_done=on_done)
        feedback.pushInfo("simulations: " + ", ".join(f"{k} {v}" for k, v in counts.items() if v))
        return {"OUT": dest}
