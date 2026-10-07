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
from ..core.proposed import PHRASE as PROPOSED_PHRASE
from . import _common as cm
from ..core import us_units as uu
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
              ("incr_tif", FT_STRING), ("buildings", FT_STRING)]
ENGINES = ["LISFLOOD-FP ACC (fast, local inertial)", "LISFLOOD-FP FV1 (full shallow water)",
           "Built-in (no install; hydrograph injection)",
           "Built-in, reservoir in the grid (gradual breach, like DSS-WISE)"]
TERRAIN = ["Cached clips only (tool 2)", "USGS 3DEP service", "Local DEM folder"]
SCENARIOS = ["td - top of dam (ND standard)", "sd - sunny day"]
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
                "sim_notes and the 'buildings' field. Screening approximation; not run in a real QGIS.")

    def initAlgorithm(self, config=None):
        P = self.addParameter
        P(QgsProcessingParameterFeatureSource("DAMS", "Dams (output of tool 5 or tool 6)",
                                              [SRC_POINT]))
        P(QgsProcessingParameterFeatureSource("BPTS", "Breach points (output of tool 4)",
                                              [SRC_POINT]))
        P(QgsProcessingParameterFile("DEMDIR", "Folder of cached dam DEMs (tool 2)",
                                     behavior=FILE_FOLDER))
        P(QgsProcessingParameterEnum("TSRC", "Terrain for larger clips", TERRAIN, defaultValue=0))
        P(QgsProcessingParameterFile("LOCAL", "Local DEM folder (if selected above)",
                                     behavior=FILE_FOLDER, optional=True))
        P(QgsProcessingParameterEnum("ENGINE", "Flood engine", ENGINES, defaultValue=0))
        P(QgsProcessingParameterFile("EXE", "LISFLOOD-FP executable (LISFLOOD engines only)",
                                     behavior=FILE_FILE, fileFilter=EXE_FILTER, optional=True))
        P(QgsProcessingParameterEnum("SCEN", "Scenario", SCENARIOS, defaultValue=0))
        P(QgsProcessingParameterFile("CURVES", "Stage-storage curve folder (tool 3; reservoir-in-grid "
                                     "bed calibration)", behavior=FILE_FOLDER,
                                     optional=True))
        cm.distance_params(P, "CELL_FT", "CELL", "Model cell size (ft), 0 = automatic by reservoir size",
                           33.0, min_ft=0.0)
        P(QgsProcessingParameterNumber("SIMH", "Simulated time (h), 0 = automatic",
                                       NUM_DOUBLE, 12.0, minValue=0.0))
        P(QgsProcessingParameterNumber("SIMMAX", "Longest allowed simulated time (h)",
                                       NUM_DOUBLE, 96.0, minValue=1.0))
        P(QgsProcessingParameterBoolean("CORR", "Model only a corridor along the flow path",
                                        defaultValue=True))
        cm.distance_params(P, "CORRW_FT", "CORRW", "Starting corridor half-width (ft)", 2000.0, min_ft=150.0)
        cm.distance_params(P, "RMAX_FT", "RMAX", "Largest terrain clip half-width (ft)", 40000.0, min_ft=1500.0)
        # v0.20.0: opt-in (it was always on from 0.15.0)
        P(QgsProcessingParameterBoolean("PREGROW", "Enlarge the clip before the flood run when the "
                                        "downstream trace reaches its edge (otherwise it grows "
                                        "after a run that reaches the edge)", defaultValue=False))
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
        curves = self.parameterAsString(params, "CURVES", context)
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
        feats, cases, hydro = {}, [], {}
        for f in dams.getFeatures():
            dam_id = str(get(f, "dam_id") or f.id())
            feats[dam_id] = f
            csvp = get(f, f"{scen}_csv")
            if csvp and os.path.exists(str(csvp)):
                hydro[dam_id] = sio.read_hydrograph_csv(str(csvp))
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
            cases.append(rec)

        opts = dict(engine=engine, exe=exe, cell="auto" if cell <= 0 else cell, corridor=corr,
                    corridor_hw_m=corrw, radius_m=None, radius_max_m=rmax,
                    sim_s=simh * 3600.0 if simh > 0 else None, sim_max_s=simmax * 3600.0,
                    manning_default=ndef, n_mult=nmult, timeout_s=tmo,
                    cascade="breach" if cascade == "breach" else "flag", workdir=work, keep=keep,
                    threads_per_run=1 if workers > 1 else max((os.cpu_count() or 2) - 1, 1),
                    baseline=baseline, baseline_cfs=base_cfs, buildings=bmode,
                    building_raise_m=raise_m, building_n=bld_n, trace_grow=pregrow)

        def paths(dam_id):
            b = os.path.join(outdir, f"{dam_id}_{scen}")
            return {k: f"{b}_{k}.tif" for k in ("depth", "vel", "dv", "arrive") + BASE_KEYS}

        MAIN = ("depth", "vel", "dv", "arrive")

        def us_path(p):
            """<...>_depth.tif -> <...>_depth_ft.tif, _vel.tif -> _vel_fps.tif, _dv.tif -> _dv_ft2s.tif"""
            for suf, us in (("_base_depth.tif", "_base_depth_ft.tif"), ("_incr_depth.tif", "_incr_depth_ft.tif"),
                            ("_depth.tif", "_depth_ft.tif"), ("_vel.tif", "_vel_fps.tif"), ("_dv.tif", "_dv_ft2s.tif")):
                if p.endswith(suf):
                    return p[:-len(suf)] + us
            return p[:-4] + "_us.tif"

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
            pth = paths(dam_id)
            need = MAIN + (BASE_KEYS if baseline else ())
            if all(os.path.exists(pth[k]) for k in need):
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
                return loader.load(dam_id, center, half, c, rec["_streams"], rec["_protect"], rec["_roads"])

            def load_man(gt, shape):
                if nlcd_src is None:
                    return None
                return sio.manning_from_nlcd(cm.warp_to_grid(nlcd_src, gt, shape), ndef)
            r0 = min(max(radius0, 300.0), rmax)
            # with cached clips only there is no terrain beyond the clip: do not try to grow
            o = dict(opts, radius_m=r0, radius_max_m=rmax if tsrc != "cache" else r0, dem_cell=dem_cell,
                     bld_polys=rec.get("_bld"))
            logs = []                       # feedback is only touched on the main thread
            r = rn.simulate(rec, load_dem, load_man, o, log=logs.append,
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

        total = len(cases) or 1
        done = [0]
        counts = {"ok": 0, "cached": 0, "triage": 0, "noflood": 0, "excluded": 0, "FAILED": 0, "cancelled": 0}

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
            o = QgsFeature(fields); o.setGeometry(f.geometry()); o.setAttributes(a)
            sink.addFeature(o)

        feedback.pushInfo(f"{len(cases)} dams, engine {engine}, {workers} at a time")
        rn.run_batch(cases, worker, workers=workers, cancel=feedback.isCanceled, on_done=on_done)
        feedback.pushInfo("simulations: " + ", ".join(f"{k} {v}" for k, v in counts.items() if v))
        return {"OUT": dest}
