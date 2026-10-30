"""Hazard classification (ND Dam Safety Standards REG_05.2026 Sec. 5.2 / 5.3) from flood rasters.

The rules are core/nd_hazard.py (shared, byte-identical, with ND Dam Breach Runner); this tool
samples the rasters, converts m and m/s to ft and ft/s (core/hazard.py adapter) and writes
the result per dam."""
import numpy as np
from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterField, QgsProcessingParameterFile,
                       QgsProcessingParameterNumber, QgsProcessingParameterFeatureSink,
                       QgsProcessingParameterEnum, QgsProcessingParameterString,
                       QgsProcessingParameterBoolean,
                       QgsFields, QgsField, QgsFeature, QgsGeometry,
                       QgsPointXY, QgsFeatureRequest, QgsRectangle,
                       QgsProcessingException)

import os

from ..core import hazard as hz, gridutil as gu
from ..core import nd_hazard as nh
from ..core import damrecord_io as dri, handoff as ho
from ..core import arrival as ar, breach_band as bbd, scenarios as sc
from . import _common as cm
from ..compat import (FILE_FOLDER, FT_DOUBLE, FT_INT, FT_STRING, NUM_DOUBLE, SRC_LINE, SRC_POINT, SRC_POLYGON, WKB_POINT)

NEW = [("hz_class", FT_STRING), ("hz_conf", FT_STRING),
       ("hz_reason", FT_STRING), ("n_bldg_wet", FT_INT),
       ("n_bldg_lethal", FT_INT), ("n_road_wet", FT_INT),
       ("n_rail_wet", FT_INT), ("hz_criterion", FT_STRING),
       ("hz_notes", FT_STRING), ("hz_vs_existing", FT_STRING),
       ("audit", FT_STRING), ("hz_basis", FT_STRING),
       ("hz_border", FT_STRING), ("hz_gaps", FT_STRING),
       ("hz_chart", FT_STRING),
       # 0.28.0: the governing loading case (core/scenarios.py), the warning time (core/arrival.py)
       # and the breach-parameter sensitivity row (core/breach_band.py)
       ("hz_scen", FT_STRING), ("hz_scen_class", FT_STRING), ("hz_scen_why", FT_STRING),
       ("hz_warn", FT_STRING),
       ("bp_class_lo", FT_STRING), ("bp_class_hi", FT_STRING), ("bp_stable", FT_STRING),
       ("bp_detail", FT_STRING)]
# enum order = hz.METHODS: ND charts (default), ND 1D 2 ft, ND conservative all wet,
# then the two comparison-only criteria
CRIT_LABELS = [hz.METHOD_LABELS[m] for m in hz.METHODS]


class HazardClassification(QgsProcessingAlgorithm):
    def name(self): return "hazardclassification"
    def displayName(self): return "8 - Hazard classification"
    def group(self): return "Dam screening"
    def groupId(self): return "dams"
    def createInstance(self): return HazardClassification()

    def shortHelpString(self):
        return ("Overlays the flood rasters on what ND Dam Safety Standards REG_05.2026 Sections 5.2 / 5.3 "
                "list and classifies each dam with core/nd_hazard.py - the same module, byte for byte, "
                "as ND Dam Breach Runner, sampled the same way, so both plugins give a dam the same "
                "class from the same flood.\n"
                "CHECKED: habitable structures (5.2.1.1), roads (5.2.1.2), railroads (5.2.1.3), lifeline "
                "facilities (5.2.2) and the other dams of the input layer that this dam's flood reaches "
                "(5.3 cascading). HIGH: potentially lethal to occupants of a habitable structure; "
                "interstate or AADT > 2,000; >= 10 trains a day or an active passenger line; a High "
                "dam reached downstream. MEDIUM: a habitable structure inundated at any depth "
                "(economic loss); AADT 500-2,000 or sole access to an occupied location; any other "
                "railroad; a lifeline facility inundated; a Medium (or unclassified) dam reached. "
                "LOW: only uninhabited buildings, private or AADT < 500 roads, or nothing of these is "
                "reached. A habitable structure in the charts' judgment zone is MEDIUM by rule (not "
                "potentially lethal). Borderline results take the higher class (5.3): an unknown "
                "velocity counts as potentially lethal (hz_border = yes when every "
                "governing finding is borderline or rests on an assumption).\n"
                "TWO LOADING CASES (0.28.0): when tool 7 ran both (SCEN_BOTH), the scen2_ rasters are "
                "classified too and the WORSE class governs - a hazard class follows the incremental "
                "consequences of a breach, and the sunny-day flood arrives on a dry day. hz_scen says "
                "which case governs, hz_scen_class both classes, hz_scen_why the reason.\n"
                "WARNING TIME (0.28.0): every impacted feature carries the flood's arrival time "
                "(arrive_hr) and the warning band it falls in (warn_time: under 15 minutes / 15 to 60 / "
                "over 60, the same bands as the suite's loss-of-life work). The warning time is taken "
                "as the arrival time: the dam fails at minute zero and nobody is warned before it. The "
                "dam's soonest one is in hz_warn.\n"
                "BREACH PARAMETERS (0.28.0, BREACH_BAND, on by default): the breach equations ND 5.3 "
                "practice accepts disagree by a factor of two on the width, so each dam gets a row at "
                "the narrowest / middle / widest breach (bp_detail), with the class under each: "
                "bp_stable = no and a note 'class depends on breach parameters' where they disagree, "
                "and the confidence drops to 'review'. The band classes come from the modelled flood "
                "scaled by the change in routed peak outflow (d ~ Q^0.6, v ~ Q^0.4) - a screening "
                "approximation of a re-run, not a re-run: a wider breach can raise the class of a "
                "structure ALREADY in the flood, but it cannot find structures outside it, so the row "
                "is a lower bound on the uncertainty. The dam's own class is never changed by it. "
                "Needs the tool-3 stage-storage curves (CURVES) to route each band.\n"
                "LETHALITY (default): the ACER TM 11 depth-velocity charts (ND Fig. 5.1 houses, 5.2 "
                "mobile homes) - they must be digitized and checked first; the chart file shipped is "
                "NOT, so this method stops with a message until then. Until then use the conservative "
                "'every inundated habitable structure' method. The 2 ft rule is allowed only for 1D "
                "models (5.2.1.1) and these floods are 2D. COMPARISON ONLY, not the ND authority: "
                "FEMA/Feinberg thresholds (DSS-WISE HCOM) and your own curve CSV (velocity_ms,depth_m).\n"
                "SAMPLING (as the runner): points - the cell under the point; polygons - the deepest cell "
                "whose centre is inside each part's exterior ring (the cell under the centroid when none "
                "is), with the largest velocity over the wet cells; lines - every half cell, the deepest. "
                "Multi-part features use their deepest part. Rasters are m and m/s, converted to ft and "
                "ft/s. ANY DEPTH: from 0.03048 m (0.1 ft, the runner's WET_FT).\n"
                "HABITABLE: a tool-0 'habitable' yes/no field wins; otherwise every structure is "
                "habitable unless its occupancy texts name an uninhabited building (the editable "
                "word list: barn, shed, garage, silo, grain bin, outbuilding, well pad ...). Mobile "
                "homes (Fig. 5.2): 'mobile' / 'manufactured' / 'trailer' / NSI RES2. ROADS: a missing, "
                "zero or NaN AADT is unknown - the 'unknown AADT' class you choose (default Medium), "
                "listed in hz_gaps; interstate from I-94 / I 94 / IH-29 / 'Interstate' / TIGER 'I'. "
                "Same for a railroad without a train count.\n"
                "OUTPUT: hz_class, hz_basis (method), hz_border, hz_gaps, hz_chart (chart status and "
                "the first 12 characters of the chart file's SHA-256). Dams cleared by triage are LOW "
                "(hz_conf = triage). For audited dams, 'audit' says whether the 2D model agreed. "
                "Screening only: the official classification is the Department's.\n"
                "DAM RECORDS (optional, v0.9): with a Dams folder, every dam that already has a "
                "folder there (<Dams folder>/<dam ID>/, e.g. made by tool 7b or ND Dam Hazard) gets "
                "a 'screened' status in its dam.json (the suite's shared dam record): the triage "
                "result (clear / needs 2D / not run), hz_class, hz_border and hz_gaps, plus the dam's "
                "ID, name, NID ID, DWR number, height and existing class where the record has none. "
                "No folder is created; a value another tool wrote is never overwritten.\n"
                "INCREMENTAL (v0.11, off by default): with tool 7's no-breach baseline (BASELINE), "
                "structures, roads, railroads and lifelines count as inundated by the breach only where "
                "the INCREMENTAL depth (<dam>_<scenario>_incr_depth.tif = breach depth - no-breach "
                "baseline depth) meets the depth threshold; the velocity (and depth x velocity) still "
                "come from the breach run. hz_basis says 'incremental vs no-breach baseline (ambient X "
                "cfs)'; a dam without the baseline raster is classified from the breach flood and gets "
                "a data gap. A screening approximation of ND's flood-scenario comparison (the real "
                "design-flood routing is the Runner's / the engineer's); the 5.3 check of other dams "
                "reached uses the same incremental depth.")

    def initAlgorithm(self, config=None):
        P = self.addParameter
        pt, ln, pg = SRC_POINT, SRC_LINE, SRC_POLYGON
        F = lambda key, label, parent, default=None: P(QgsProcessingParameterField(
            key, label, defaultValue=default, parentLayerParameterName=parent, optional=True))
        P(QgsProcessingParameterFeatureSource("SIM", "Simulation results (tool 7)", [pt]))
        F("DAM_CLASS", "Dams: existing hazard class field (5.3 cascading; DWR hazard_cla also found)",
          "SIM", "haz_existing")
        P(QgsProcessingParameterFeatureSource("BLD", "Structures (points or polygons)", [pg, pt], optional=True))
        F("OCC", "Structures: occupancy class field", "BLD", "OCC_CLS")
        F("PRIM", "Structures: primary occupancy field (also finds manufactured homes)", "BLD", "PRIM_OCC")
        P(QgsProcessingParameterString("UNINHABITED", "Structures: occupancy words that mark an UNINHABITED "
                                       "building (everything else, blank or unknown is habitable)",
                                       defaultValue=", ".join(nh.UNINHABITED_KEYWORDS)))
        P(QgsProcessingParameterFeatureSource("ROADS", "Roads (lines)", [ln], optional=True))
        F("ROAD_AADT", "Roads: AADT field", "ROADS", "aadt")
        F("ROAD_CLASS", "Roads: class / route field (interstate: I-94, I 94, IH-29, Interstate, TIGER 'I')",
          "ROADS", "road_class")
        F("R_SOLE", "Roads: sole access to an occupied location field (yes/no)", "ROADS", "sole_access")
        P(QgsProcessingParameterEnum("UNKNOWN_AADT", "Class of an overtopped road with no AADT (a data gap)",
                                     list(nh.CLASSES), defaultValue=nh.CLASSES.index(nh.MEDIUM)))
        P(QgsProcessingParameterFeatureSource("RAIL", "Railroads (lines)", [ln], optional=True))
        F("RAIL_TRAINS", "Railroads: trains per day field", "RAIL", "trains_day")
        F("RAIL_PAX", "Railroads: passenger field (FRA PASSNGR or yes/no)", "RAIL", "PASSNGR")
        P(QgsProcessingParameterString("PAXCODES", "Passenger codes counted as an active passenger line",
                                       defaultValue=", ".join(hz.PAX_CODES)))
        P(QgsProcessingParameterEnum("UNKNOWN_TRAINS", "Class of an overtopped railroad with no trains a "
                                     "day (a data gap; at least Medium)", list(nh.CLASSES),
                                     defaultValue=nh.CLASSES.index(nh.MEDIUM)))
        P(QgsProcessingParameterFeatureSource("LIFELINES", "Lifeline facilities: utilities, emergency response "
                                              "(points or polygons)", [pt, pg], optional=True))
        F("L_KIND", "Lifelines: kind of facility field", "LIFELINES", "kind")
        P(QgsProcessingParameterFile("DAMS_FOLDER", "Dams folder: add a 'screened' status to dam.json of "
                                     "the dams that have a folder there (optional)",
                                     behavior=FILE_FOLDER, optional=True))
        P(QgsProcessingParameterFile("DEM_FOR_RECORD", "DEM to record in dam.json for ND Dam Breach Runner / ND Dam "
                                     "Hazard (optional; the chain passes the local DEM mosaic of step 2)",
                                     optional=True))
        P(QgsProcessingParameterBoolean("CREATE_DAM_FOLDERS", "Create the dam folder (and dam.json) in the Dams "
                                        "folder for every dam that has none - a run of chosen dams "
                                        "hands them to ND Dam Breach Runner / ND Dam Hazard this way",
                                        defaultValue=False))
        P(QgsProcessingParameterEnum("CRIT", "Lethality at habitable structures (ND 5.2.1.1)", CRIT_LABELS,
                                     defaultValue=0))
        P(QgsProcessingParameterFile("CHART_FILE", "Digitized ACER TM 11 chart file (.json; empty = "
                                     "core/acer_tm11_charts.json)", extension="json", optional=True))
        P(QgsProcessingParameterFile("CURVE", "Depth-velocity curve CSV (comparison-only curve criterion)",
                                     extension="csv", optional=True))
        P(QgsProcessingParameterNumber("MIND_FT", "Structures, lifelines, dams: inundated from a depth of (ft; ND "
                                       "says 'any depth' - the starting value 0.1 ft is YOUR allowance for "
                                       "model noise)", NUM_DOUBLE, hz.DEFAULT_WET_FT,
                                       minValue=hz.MIN_WET_FT))
        P(QgsProcessingParameterNumber("RDEP_FT", "Roads/railroads: overtopped from a depth of (ft; ND 'any depth' - "
                                       "the starting value 0.1 ft is YOUR allowance for model noise)",
                                       NUM_DOUBLE, hz.DEFAULT_WET_FT, minValue=hz.MIN_WET_FT))
        P(cm.hide(QgsProcessingParameterNumber("MIND", "(before 0.11) inundated from a depth of (m)",
                                               NUM_DOUBLE, hz.DEFAULT_WET_M,
                                               minValue=hz.MIN_WET_M, optional=True)))
        P(cm.hide(QgsProcessingParameterNumber("RDEP", "(before 0.11) overtopped from a depth of (m)",
                                               NUM_DOUBLE, hz.DEFAULT_WET_M,
                                               minValue=hz.MIN_WET_M, optional=True)))
        P(QgsProcessingParameterNumber("RULE", "ND 1D rule: lethal depth at structures (ft; 1D models only)",
                                       NUM_DOUBLE, 2.0, minValue=0.1))
        P(QgsProcessingParameterBoolean("INCREMENTAL", "Count impacts from the INCREMENTAL depth (breach minus "
                                        "tool-7 no-breach baseline; screening approximation of ND's "
                                        "flood-scenario comparison)", defaultValue=False))
        # 0.28.0: the breach-parameter sensitivity row needs the stage-storage curves to route
        # each band's breach (the same routing step 5 uses)
        P(QgsProcessingParameterFile("CURVES", "Stage-storage curve folder (tool 3; for the "
                                     "breach-parameter sensitivity row)", behavior=FILE_FOLDER,
                                     optional=True))
        P(QgsProcessingParameterBoolean("BREACH_BAND", "Add the breach-parameter sensitivity row: the "
                                        "class under the narrowest and the widest breach the "
                                        "equations give", defaultValue=True))
        P(QgsProcessingParameterFeatureSink("OUT", "Dams with hazard classification", SRC_POINT))
        P(QgsProcessingParameterFeatureSink("IMP", "Impacted features", SRC_POINT))

    @staticmethod
    def _grid(dtif, vtif, dvtif, itif=None):
        """Sampling grid: depth from `itif` (the incremental raster) when given, else `dtif`;
        velocity and depth x velocity always from the breach run."""
        d, gt = cm.read_tif(dtif)
        v, _ = cm.read_tif(vtif)
        dv = cm.read_tif(dvtif)[0] if dvtif else None
        if itif:
            inc, igt = cm.read_tif(itif)
            if inc.shape != d.shape or any(abs(a - b) > 1e-6 for a, b in zip(igt, gt)):
                raise QgsProcessingException(f"{itif}: not on the grid of {dtif}")
            d = np.where(d > gu.NODATA + 1, np.clip(inc, 0.0, None), d)
        try:
            g = hz.Grid(d, v, gt, gu.NODATA, dv)
        except AssertionError as e:
            raise QgsProcessingException(f"{dtif}: {e}")
        return g, QgsRectangle(*g.extent)

    @staticmethod
    def incr_path(f, get):
        """The incremental-depth raster of a tool-7 result: the incr_tif field, else derived from
        depth_tif (<base>_depth.tif -> <base>_incr_depth.tif).  None when neither exists."""
        p = get(f, "incr_tif")
        if p and os.path.exists(str(p)):
            return str(p)
        d = str(get(f, "depth_tif") or "")
        if d.endswith("_depth.tif"):
            p = d[:-len("_depth.tif")] + "_incr_depth.tif"
            if os.path.exists(p):
                return p
        return None

    def processAlgorithm(self, params, context, feedback):
        sim = self.parameterAsSource(params, "SIM", context)
        bld = self.parameterAsSource(params, "BLD", context)
        roads = self.parameterAsSource(params, "ROADS", context)
        rail = self.parameterAsSource(params, "RAIL", context)
        life = self.parameterAsSource(params, "LIFELINES", context)
        S = lambda k: self.parameterAsString(params, k, context) or None
        uninhabited = hz.words(S("UNINHABITED")) or nh.UNINHABITED_KEYWORDS
        paxcodes = hz.words(S("PAXCODES")) or hz.PAX_CODES
        crit = hz.METHODS[self.parameterAsEnum(params, "CRIT", context)]
        unknown_aadt = nh.CLASSES[self.parameterAsEnum(params, "UNKNOWN_AADT", context)]
        unknown_trains = nh.CLASSES[self.parameterAsEnum(params, "UNKNOWN_TRAINS", context)]
        curve_p = S("CURVE")
        if crit == hz.COMPARISON_CURVE and not curve_p:
            raise QgsProcessingException("The curve criterion needs a curve CSV")
        curve = hz.load_curve(curve_p) if curve_p and crit == hz.COMPARISON_CURVE else None
        charts = None
        if crit == nh.LETHALITY_CHARTS:
            try:
                charts = hz.load_ready_charts(S("CHART_FILE"))
            except nh.ChartsNotDigitized as e:
                raise QgsProcessingException(str(e))
            except (OSError, ValueError) as e:
                raise QgsProcessingException("Could not read the chart file {}: {}".format(
                    S("CHART_FILE") or nh.CHART_FILE, e))
            feedback.pushInfo("ACER TM 11 charts: {} (status {}, digitized by {}, signed off by {}, "
                              "SHA-256 {})".format(charts.path, charts.status, charts.digitized_by or "-",
                                                   charts.signer or "-", charts.sha256[:12]))
        mind, mind_ft, _s1 = cm.feet_or_legacy(self, params, context, "MIND_FT", "MIND", hz.DEFAULT_WET_FT)
        rdep, rdep_ft, _s2 = cm.feet_or_legacy(self, params, context, "RDEP_FT", "RDEP", hz.DEFAULT_WET_FT)
        feedback.pushInfo("Inundated from {:.2f} ft ({:.4f} m) at structures / lifelines / dams, {:.2f} ft "
                          "({:.4f} m) on roads and railroads".format(mind_ft, mind, rdep_ft, rdep))
        if min(mind, rdep) < hz.MIN_WET_M:
            raise QgsProcessingException("The wet depths must be at least {} m (0.01 ft): a dry cell reads 0"
                                         .format(hz.MIN_WET_M))
        wet_m = min(mind, rdep)          # nd_hazard's wet_ft; both thresholds are applied here too
        wet_ft = wet_m / hz.M_PER_FT
        rule_ft = self.parameterAsDouble(params, "RULE", context)
        incremental = self.parameterAsBool(params, "INCREMENTAL", context)
        curves_dir = (self.parameterAsString(params, "CURVES", context) or "").strip()   # 0.28.0
        breach_band = self.parameterAsBool(params, "BREACH_BAND", context)
        basis = hz.METHOD_LABELS[crit]
        feedback.pushInfo("Basis: {}; lethality: {}".format(nh.POLICY, basis))
        if incremental:
            feedback.pushInfo("Incremental: impacts counted where the breach adds depth over tool 7's no-breach "
                              "baseline (screening approximation of ND's flood-scenario comparison).")
        if crit == nh.LETHALITY_1D:
            feedback.reportError("Warning: " + hz.ONE_D_WARNING)
        if crit in hz.COMPARISON:
            feedback.reportError("Lethality: {}. ND 5.2.1.1 uses the ACER TM 11 charts; use these "
                                 "classes for comparison only.".format(basis))

        # fields: the named field when the layer has it; tool-0 standard fields otherwise
        has = lambda src, f: bool(pick(src, f))

        def pick(src, f):
            """The layer's own spelling of field `f` (0.18.0: matched ignoring case, so a layer
            with AADT is found by the default 'aadt'), or None."""
            if not f or src is None:
                return None
            nm = src.fields().names()
            if f in nm:
                return f
            low = {n.lower(): n for n in nm}
            hit = low.get(str(f).lower())
            if hit:
                feedback.pushInfo("Field '{}' found as '{}' (case ignored).".format(f, hit))
            return hit
        occ = pick(bld, S("OCC"))
        prim = pick(bld, S("PRIM"))
        f_hab = "habitable" if has(bld, "habitable") else None
        f_mob = "mobile" if has(bld, "mobile") else None
        if bld is not None and occ is None and prim is None and f_hab is None:
            feedback.pushInfo("Structures: no occupancy or habitable field - every structure is habitable "
                              "(5.2.1.1, 5.3).")
        f_aadt = pick(roads, S("ROAD_AADT"))
        f_rcls = pick(roads, S("ROAD_CLASS"))
        f_sole = pick(roads, S("R_SOLE"))
        if roads is not None and f_aadt is None:
            feedback.reportError("Roads: no AADT field - every overtopped road is a data gap classified "
                                 "{} by assumption.".format(unknown_aadt))
        f_trn = pick(rail, S("RAIL_TRAINS"))
        f_pax = pick(rail, S("RAIL_PAX")) or pick(rail, "passenger")
        f_kind = pick(life, S("L_KIND"))
        f_dcls = pick(sim, S("DAM_CLASS")) or pick(sim, "hazard_cla")

        names = sim.fields().names()
        get = lambda f, k, c=None: cm.fval(f, names, k, c)
        dams_folder = S("DAMS_FOLDER")
        create_folders = bool(self.parameterAsBool(params, "CREATE_DAM_FOLDERS", context))   # 0.26.0
        dem_record = (S("DEM_FOR_RECORD") or "").strip()                                   # 0.26.0
        if dem_record and not os.path.isfile(dem_record.split("|")[0]):
            feedback.pushWarning("DEM to record not found, not recorded: {}".format(dem_record))
            dem_record = ""
        version = dri.plugin_version()
        n_records = 0
        fields, put = cm.extend_fields(sim.fields(), NEW)
        sink, dest = cm.us_sink(self, params, "OUT", context, fields, sim.wkbType(), sim.sourceCrs())
        ifields = QgsFields()
        for n, t in (("dam_id", FT_STRING), ("kind", FT_STRING), ("depth_m", FT_DOUBLE),
                     ("vel_ms", FT_DOUBLE), ("dv_m2s", FT_DOUBLE), ("lethal", FT_STRING),
                     ("level", FT_STRING),
                     # 0.28.0: the loading case this came from, and the warning time the people at
                     # this feature have (core/arrival.py; the bands are damsuite.loss's)
                     ("scenario", FT_STRING), ("arrive_hr", FT_DOUBLE), ("warn_time", FT_STRING)):
            ifields.append(QgsField(n, t))
        isink, idest = cm.us_sink(self, params, "IMP", context, ifields, WKB_POINT, cm.albers())
        tf = lambda src: (cm.to_albers(src), cm.from_albers(src)) if src is not None else (None, None)
        tb, fb = tf(bld)
        tr, fr = tf(roads)
        tl, fl = tf(rail)
        tt, ft_ = tf(life)
        total = sim.featureCount() or 1
        tally, n_gaps, n_border = {}, 0, 0
        chart_txt = hz.chart_text(charts)

        # all dams of the input layer, for 5.3 cascading (points in EPSG:5070)
        t_sim = cm.to_albers(sim)
        others = []
        for f in sim.getFeatures():
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            g.transform(t_sim)
            p = cm.rep_point(g)
            if p is not None:
                others.append((str(get(f, "dam_id") or f.id()), p,
                               str(get(f, f_dcls) or "") if f_dcls else ""))
        oxy = np.array([o[1] for o in others], float).reshape(-1, 2)


        def assess(dam_id, dtif, vtif, dvtif, itif=None, atif=None):
            """Sample one flood at every receptor and classify it (ND 5.2 / 5.3).

            0.28.0: this was the body of the per-dam loop. It is a function so that the SECOND
            loading case (tool 7's SCEN_BOTH: the scen2_ rasters) can be classified the same way
            and the worse class govern - running one scenario can only miss consequences. It also
            hands back the samples it collected, which is what the breach-parameter sensitivity
            row re-classifies without a second flood model.

            Returns (classification, grid, where, samples, arrival) where samples is
            (structures, roads, railroads, lifelines, downstream dams) and arrival is
            (array, geotransform) of the arrival raster, or None."""
            grid, ext = self._grid(dtif, vtif, dvtif, itif)
            s_in, r_in, t_in, l_in, d_in = [], [], [], [], []
            where = {}                           # (kind, id) -> ((x, y), sample, label)

            def feats(src, fback):
                return src.getFeatures(QgsFeatureRequest().setFilterRect(fback.transformBoundingBox(ext)))

            if bld is not None:
                for b in feats(bld, fb):
                    kind, parts = cm.albers_parts(b, tb)
                    s = grid.sample(kind, parts, wet_ft) if parts else None
                    if not s or s["depth_m"] < mind:
                        continue
                    hab = hz.is_habitable(b[occ] if occ else None, b[prim] if prim else None,
                                          flag=b[f_hab] if f_hab else None, uninhabited=uninhabited)
                    mob = hz.is_mobile(b[f_mob] if f_mob else None, b[prim] if prim else None,
                                       b[occ] if occ else None)
                    sid = str(b.id())
                    s_in.append(dict(s, id=sid, mobile=mob, habitable=hab))
                    where[("structure", sid)] = ((s["x"], s["y"]), s, ("mobile home" if mob else "structure")
                                                 if hab else "uninhabited building")

            def lines(src, tfm, fback, kind, out, attrs):
                for r in feats(src, fback):
                    k, parts = cm.albers_parts(r, tfm)
                    s = grid.sample("line" if k == "line" else k, parts, wet_ft) if parts else None
                    if s and s["depth_m"] >= rdep:
                        rid = str(r.id())
                        rec = {"id": rid, "depth_m": s["depth_m"], "x": s["x"], "y": s["y"]}
                        rec.update(attrs(r))
                        out.append(rec)
                        where[(kind, rid)] = ((s["x"], s["y"]), s, kind)

            if roads is not None:
                lines(roads, tr, fr, "road", r_in, lambda r: {
                    "aadt": r[f_aadt] if f_aadt else None,
                    "road_class": str(r[f_rcls]) if f_rcls and r[f_rcls] is not None else "",
                    "sole_access": nh.yes_no(r[f_sole]) is True if f_sole else False})
            if rail is not None:
                lines(rail, tl, fl, "railroad", t_in, lambda r: {
                    "trains_per_day": r[f_trn] if f_trn else None,
                    "passenger": hz.rail_passenger(r[f_pax], paxcodes) if f_pax else False})
            if life is not None:
                for L in feats(life, ft_):
                    kind, parts = cm.albers_parts(L, tt)
                    s = grid.sample(kind, parts, wet_ft) if parts else None
                    if s and s["depth_m"] >= mind:
                        lid = str(L.id())
                        l_in.append({"id": lid, "depth_m": s["depth_m"], "x": s["x"], "y": s["y"],
                                     "kind": str(L[f_kind] or "") if f_kind else ""})
                        where[("lifeline", lid)] = ((s["x"], s["y"]), s, "lifeline")
            if len(oxy):
                x0, y0, x1, y1 = grid.extent
                inside = np.nonzero((oxy[:, 0] >= x0) & (oxy[:, 0] < x1) & (oxy[:, 1] > y0) &
                                    (oxy[:, 1] <= y1))[0]
                for k in inside:
                    oid, (x, y), ocls = others[k]
                    if oid == dam_id:
                        continue
                    dd, vv = grid.point(x, y)
                    if dd is not None and dd >= mind:
                        d_in.append(hz.nd_downstream_dam(oid, ocls, dd))
                        where[("dam", oid)] = ((x, y), {"depth_m": dd, "vel_ms": vv, "dv_m2s": None},
                                               "dam")

            try:
                c = hz.classify_nd(s_in, r_in, t_in, method=crit, wet_m=wet_m, rule_depth_ft=rule_ft,
                                   unknown_aadt=unknown_aadt, charts=charts, curve=curve,
                                   lifelines=l_in, downstream_dams=d_in, unknown_trains=unknown_trains)
            except nh.ChartsNotDigitized as e:
                raise QgsProcessingException(str(e))

            arr = None
            if atif and os.path.exists(str(atif)):
                try:
                    arr = cm.read_tif(str(atif))
                except Exception as e:                           # noqa - no warning time, not no class
                    feedback.pushWarning("dam {}: arrival raster not read ({}): no warning time".format(
                        dam_id, e))
            return c, grid, where, (s_in, r_in, t_in, l_in, d_in), arr

        def scaled_class(samples, df, vf):
            """The class this dam would get if every sampled depth were df times deeper and every
            velocity vf times faster - the same rules module, not a second model. Used for the
            breach-parameter bands (0.28.0)."""
            s_in, r_in, t_in, l_in, d_in = samples

            def sc_rec(rec):
                out = dict(rec)
                if out.get("depth_m") is not None:
                    out["depth_m"] = out["depth_m"] * df
                if out.get("vel_ms") is not None:
                    out["vel_ms"] = out["vel_ms"] * vf
                if out.get("dv_m2s") is not None:
                    out["dv_m2s"] = out["dv_m2s"] * df * vf
                return out
            # the downstream dams are left alone: their class, not their depth, drives 5.3 cascading
            got = hz.classify_nd([sc_rec(x) for x in s_in], [sc_rec(x) for x in r_in],
                                 [sc_rec(x) for x in t_in], method=crit, wet_m=wet_m,
                                 rule_depth_ft=rule_ft, unknown_aadt=unknown_aadt, charts=charts,
                                 curve=curve, lifelines=[sc_rec(x) for x in l_in],
                                 downstream_dams=d_in, unknown_trains=unknown_trains)
            return got.hazard_class

        def band_row(dam_id, f, samples, scen_key):
            """(verdict, rows) of the breach-parameter sensitivity row, or (None, {}) when this
            dam has nothing to build it from (no volume, no height, no stage-storage curve, or
            the bands all route to the same peak - which would otherwise read as "stable")."""
            vw = cm.fval(f, names, "{}_vw_m3".format(scen_key), float)
            hb = cm.fval(f, names, "{}_hb_m".format(scen_key), float)
            start = cm.fval(f, names, "{}_start_m".format(scen_key), float)
            invert = cm.fval(f, names, "base_m", float)
            bands = bbd.params(vw, hb) if (vw and hb) else {}
            if not bands:
                return None, {}
            cpath = os.path.join(curves_dir, "curve_{}.csv".format(dam_id)) if curves_dir else ""
            if not (cpath and os.path.isfile(cpath) and start is not None and invert is not None):
                return None, bands                  # the widths and times, with no class
            try:
                d = np.loadtxt(cpath, delimiter=",", skiprows=1, ndmin=2)
                bands = bbd.peaks(bands, d[:, 0], d[:, 1], start, invert)
            except Exception as e:                                # noqa - a row, never the class
                feedback.pushWarning("dam {}: breach-parameter row: the curve could not be routed "
                                     "({})".format(dam_id, e))
                return None, bands
            qs = set(round(r["peak_m3s"], 6) for r in bands.values() if r.get("peak_m3s"))
            if len(qs) < 2:
                return None, bands
            rows = bbd.classes(bands, lambda df, vf: scaled_class(samples, df, vf))
            return bbd.verdict(rows), rows

        for i, f in enumerate(sim.getFeatures()):
            if feedback.isCanceled(): break
            feedback.setProgress(100 * i / total)
            dam_id = str(get(f, "dam_id") or f.id())
            a = cm.blank_attrs(fields, f)
            res = {k: None for k, _ in NEW}
            res.update({"hz_criterion": crit, "hz_basis": basis, "hz_chart": chart_txt})
            status = str(get(f, "sim_status") or get(f, "status") or "")
            dtif, vtif = get(f, "depth_tif"), get(f, "vel_tif")
            if status == "excluded" or cm.excluded(f, names)[0]:
                # 0.13: the Review tab excluded this dam: no class, the reason recorded
                res.update({"hz_class": "", "hz_conf": "excluded",
                            "hz_reason": cm.excluded(f, names)[1] or "excluded by review",
                            "n_bldg_wet": 0, "n_bldg_lethal": 0, "n_road_wet": 0, "n_rail_wet": 0,
                            "hz_border": "no", "hz_gaps": "",
                            "hz_notes": "excluded by the reviewer (Review tab); not classified"})
            elif status == "noflood":
                # tool 16: a pond / dugout with no water above the natural ground. Nothing leaves the
                # pond, so nothing downstream is reached: Low, by the same reasoning as a triage clear
                res.update({"hz_class": "Low", "hz_conf": "no flood",
                            "hz_reason": "no breach flood: " + str(get(f, "no_flood_why") or
                                                                    "no water above the natural ground"),
                            "n_bldg_wet": 0, "n_bldg_lethal": 0, "n_road_wet": 0, "n_rail_wet": 0,
                            "hz_border": "no", "hz_gaps": "",
                            "hz_notes": "pond / dugout: a breach releases only water above the natural "
                                        "ground and there is none (screening, from the LiDAR)"})
            elif status == "triage":
                res.update({"hz_class": "Low", "hz_conf": "triage",
                            "hz_reason": "triage: " + str(get(f, "tri_reason") or "nothing inside the envelope"),
                            "n_bldg_wet": 0, "n_bldg_lethal": 0, "n_road_wet": 0, "n_rail_wet": 0,
                            "hz_border": "no", "hz_gaps": ""})
                # 0.19.0: a triage clear on an estimated volume says so (the envelope rests on it)
                est = [n for k, n in (("h_src", "height"), ("stor_src", "storage"))
                       if "estimated" in str(get(f, k) or "").lower()]
                tnotes = []
                if est:
                    tnotes.append("{} ESTIMATED from the DEM (no DWR value)".format(" and ".join(est)))
                if str(get(f, "kind") or "") == "unknown structure":
                    tnotes.append("DWR unknown structure: never inspected; location and values unverified")
                if tnotes:
                    res["hz_notes"] = "; ".join(tnotes)
            elif status not in ("ok", "cached") or not dtif or not vtif:
                res.update({"hz_class": "NoData", "hz_conf": "none",
                            "hz_notes": "no simulation result: " + str(get(f, "sim_notes") or get(f, "notes") or "")})
            else:
                notes, extra_gaps = [], []
                itif = None
                if incremental:
                    itif = self.incr_path(f, get)
                    if itif:
                        cfs = get(f, "base_cfs", float)
                        res["hz_basis"] = basis + "; incremental vs no-breach baseline (ambient {} cfs)".format(
                            f"{cfs:g}" if cfs is not None else "unknown")
                        notes.append("incremental depth (breach minus no-breach baseline) used for the impacts; "
                                     "screening approximation of ND's flood-scenario comparison")
                    else:
                        extra_gaps.append("no no-breach baseline raster for this dam (tool 7 BASELINE off or "
                                          "incr_depth.tif missing): classified from the breach flood")
                c, grid, where, samples, arr = assess(
                    dam_id, dtif, vtif, get(f, "dv_tif"), itif, get(f, "arrive_tif"))
                scen_used = str(get(f, "scenario") or sc.PRIMARY)
                runs = {scen_used: (c, grid, where, samples, arr)}
                # 0.28.0: the second loading case tool 7 ran (SCEN_BOTH). Both are classified and
                # the WORSE class governs (core/scenarios.py: the incremental rule).
                scen2 = str(get(f, "scen2") or "")
                d2, v2 = str(get(f, "scen2_depth_tif") or ""), str(get(f, "scen2_vel_tif") or "")
                if scen2 and scen2 != scen_used and d2 and v2 and os.path.exists(d2) and os.path.exists(v2):
                    try:
                        runs[scen2] = assess(dam_id, d2, v2, get(f, "scen2_dv_tif"), None,
                                             get(f, "scen2_arrive_tif"))
                    except QgsProcessingException:
                        raise
                    except Exception as e:                       # noqa - one scenario, not the dam
                        feedback.pushWarning("dam {}: the {} scenario was not classified ({}); the {} "
                                             "class stands".format(dam_id, scen2, e, scen_used))
                elif scen2 and scen2 != scen_used:
                    extra_gaps.append("the {} loading case was asked for but has no flood raster: the "
                                      "class is from {} alone".format(sc.SHORT.get(scen2, scen2),
                                                                      sc.SHORT.get(scen_used, scen_used)))
                gov = sc.governing(dict((k, v[0].hazard_class) for k, v in runs.items()))
                if len(runs) > 1 and gov.get("scenario") in runs:
                    scen_used = gov["scenario"]
                    c, grid, where, samples, arr = runs[scen_used]
                    res.update({"hz_scen": scen_used, "hz_scen_class": "; ".join(
                        "{} {}".format(k, runs[k][0].hazard_class) for k in sc.ORDER if k in runs),
                        "hz_scen_why": gov.get("why") or ""})
                    gnote = sc.note(gov)
                    if gnote:
                        notes.append(gnote)
                else:
                    res["hz_scen"] = scen_used
                warn_hours = []                      # 0.28.0: the arrival times at the receptors
                for fnd in c.findings:
                    w = where.get((fnd.kind, fnd.id))
                    if w is None:
                        continue
                    lethal = "n/a"
                    if fnd.kind == "structure" and w[2] != "uninhabited building":
                        lethal = ("YES (judgment zone, 5.3; old option)" if fnd.zone == nh.ZONE_JUDGMENT else
                                  "YES (borderline, 5.3)" if fnd.borderline else "YES") \
                            if fnd.level == nh.HIGH else \
                            ("no (judgment zone, MEDIUM by rule)" if fnd.zone == nh.ZONE_JUDGMENT
                             else "no")
                    hrs = ar.sample(arr[0], arr[1], w[0][0], w[0][1]) if arr else None
                    if hrs is not None:
                        warn_hours.append(hrs)
                    it = QgsFeature(ifields)
                    it.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(w[0][0], w[0][1])))
                    it.setAttributes([dam_id, w[2], w[1]["depth_m"], w[1].get("vel_ms"), w[1].get("dv_m2s"),
                                      lethal, fnd.level, scen_used, hrs, ar.band_text(hrs)])
                    isink.addFeature(it)

                cls = c.hazard_class
                if bld is None:
                    notes.append("no structure layer supplied - structures not assessed")
                if crit == nh.LETHALITY_1D:
                    notes.append("1D rule on a 2D result (5.2.1.1 allows it only for 1D models)")
                conf = "medium"
                # 0.28.0: the edge flag of the scenario that governs, not always the first one
                edge_field = "edge_flag" if scen_used == str(get(f, "scenario") or sc.PRIMARY) \
                    else "scen2_edge_flag"
                if str(get(f, edge_field)) == "YES":
                    conf = "review"; notes.append("flood truncated at the terrain edge; impacts beyond "
                                                  "it were not counted, so this class is a floor")
                # 0.19.0: a class built on an estimated height or storage is a low-confidence class
                est = [w for w, k in (("height", "h_src"), ("storage", "stor_src"))
                       if "estimated" in str(get(f, k) or "").lower()]
                if est:
                    notes.append("{} ESTIMATED from the DEM (no DWR value)".format(" and ".join(est)))
                    if conf == "medium":
                        conf = "low"
                if str(get(f, "kind") or "") == "unknown structure":
                    notes.append("DWR unknown structure: never inspected; location and values unverified")
                if str(get(f, "stor_conf") or "") == "low" and conf == "medium":
                    conf = "low"
                eng = str(get(f, "sim_engine") or get(f, "solver") or "")
                if "acc" in eng and cls != "Low":
                    notes.append("ACC velocities near the breach may be unreliable")
                if crit == hz.COMPARISON_FEINBERG and "upper bound" in str(get(f, "dv_kind") or ""):
                    notes.append("depth x velocity is an upper bound (ACC engine)")
                if c.borderline_raised:
                    notes.append("borderline: the higher class was assigned (ND 5.3)")
                if c.counts["downstream_dams"]:
                    notes.append("{} other dam(s) reached by the flood (5.3 cascading)".format(
                        c.counts["downstream_dams"]))
                notes += [n for n in c.notes if "Engineer" in n]
                # 0.28.0: how much warning the people downstream had (core/arrival.py)
                if warn_hours:
                    res["hz_warn"] = ar.summary_text(warn_hours)
                # 0.28.0: does the class depend on the breach parameters? (core/breach_band.py)
                if breach_band:
                    v, brows = band_row(dam_id, f, samples, scen_used)
                    if brows:
                        res["bp_detail"] = bbd.detail_text(brows)
                    if v:
                        res.update({"bp_class_lo": v["class_min"], "bp_class_hi": v["class_max"],
                                    "bp_stable": "yes" if v["stable"] else "no"})
                        if v["stable"] is False:
                            # FIRST in the list: hz_notes is cut to 254 characters and this is the
                            # note that says the one class in the column is not the whole answer
                            notes.insert(0, v["text"] + " (" + bbd.SCALED + ")")
                            if conf == "medium":
                                conf = "review"   # never "low" -> "review": core/review.py lists
                                                  # a low-confidence dam and would then lose it
                res.update({"hz_class": cls, "hz_conf": conf, "hz_reason": hz.governing_summary(c),
                            "n_bldg_wet": c.counts["habitable_wet"],
                            "n_bldg_lethal": int(c.counts["habitable_lethal"]),
                            "n_road_wet": c.counts["roads_overtopped"],
                            "n_rail_wet": c.counts["railroads_overtopped"],
                            "hz_notes": hz.limit_text(notes),
                            "hz_border": "yes" if c.borderline_raised else "no",
                            "hz_gaps": hz.limit_text(list(c.data_gaps) + extra_gaps)})
                n_gaps += bool(c.data_gaps or extra_gaps)
                n_border += bool(c.borderline_raised)
                for gap in (list(c.data_gaps) + extra_gaps)[:20]:
                    feedback.pushInfo(f"dam {dam_id}: data gap: {gap}")
                if str(get(f, "tri_audit") or "") == "yes":
                    res["audit"] = "agree" if cls == "Low" else "mismatch"
            if res["hz_class"] is not None:
                res["hz_vs_existing"] = hz.compare_existing(res["hz_class"], get(f, f_dcls) if f_dcls else None)
                tally[res["hz_class"]] = tally.get(res["hz_class"], 0) + 1
            for k, _ in NEW:
                put(a, k, res[k])
            out = QgsFeature(fields); out.setGeometry(f.geometry()); out.setAttributes(a)
            sink.addFeature(out)
            if dams_folder:
                dfold = os.path.join(dams_folder, ho.safe_name(dam_id))
                if create_folders and not os.path.isdir(dfold):
                    try:
                        os.makedirs(dfold, exist_ok=True)
                        feedback.pushInfo("dam {}: dam folder created: {}".format(dam_id, dfold))
                    except OSError as e:
                        feedback.pushWarning("dam {}: dam folder not created ({})".format(dam_id, e))
                if os.path.isdir(dfold):
                    attrs = {n: get(f, n) for n in names}
                    attrs.update({k: res[k] for k in ("hz_class", "hz_border", "hz_gaps")})
                    # 0.28.0: the loading case the class came from, so the dam record hands the
                    # Runner the case that governed and not the first one that ran
                    attrs["hz_scen"] = res.get("hz_scen") or attrs.get("scenario")
                    try:                                   # 0.26.0: the dam point for the record
                        g = f.geometry()
                        if g is not None and not g.isEmpty():
                            pt = g.centroid().asPoint()
                            attrs["_x"], attrs["_y"], attrs["_crs"] = pt.x(), pt.y(), sim.sourceCrs().authid()
                    except Exception:                      # noqa
                        pass
                    if status == "noflood":      # recorded as a triage clear with its reason
                        attrs["tri_result"] = "clear"
                        attrs["tri_reason"] = res["hz_reason"]
                    area_top, area_note = None, ""     # 0.32.4: as tool 7b sends AREA_AC
                    if curves_dir:
                        try:
                            area_top, area_note = ho.area_for_runner(
                                os.path.join(curves_dir, "curve_{}.csv".format(dam_id)),
                                cm.fval(f, names, "est_crest_m", float),
                                cm.fval(f, names, "height_m", float))
                        except Exception as e:          # noqa - the record is written without it
                            area_note = "no top-of-dam area: the curve could not be read ({})".format(e)
                    if area_note:
                        feedback.pushInfo("dam {}: {}".format(dam_id, area_note))
                    try:
                        jpath, jlog = dri.write(dfold, dri.tool8_values(attrs, f_dcls, area_top),
                                                dri.screening_result(attrs, "tool 8"), version,
                                                create=False, field_notes=dri.estimate_notes(attrs),
                                                data={"dem": dem_record} if dem_record else None)
                        n_records += bool(jpath)
                        for line in jlog:
                            feedback.pushInfo("dam {}: {}".format(dam_id, line))
                    except Exception as e:      # a record must never stop the classification
                        feedback.reportError("dam {}: dam.json not written: {}".format(dam_id, e))
                    try:                        # 0.11: the model-chain manifest for this dam
                        from ..core import manifest as mf
                        rasters = {"depth": get(f, "depth_tif"), "velocity": get(f, "vel_tif"),
                                   "dv": get(f, "dv_tif"), "arrival": get(f, "arrive_tif"),
                                   "increment": get(f, "incr_tif")}
                        mp = mf.dam_manifest(dfold, dam_id, rasters, res, params, version)
                        if mp:
                            feedback.pushInfo("dam {}: screening manifest {}".format(dam_id, mp))
                    except Exception as e:      # noqa
                        feedback.reportError("dam {}: screening manifest not written: {}".format(dam_id, e))
        if dams_folder:
            feedback.pushInfo("dam records (dam.json) updated: {} (dams with a folder in {})".format(
                n_records, dams_folder))
        feedback.pushInfo("classes: " + ", ".join(f"{k} {v}" for k, v in sorted(tally.items())))
        feedback.pushInfo(f"dams with data gaps: {n_gaps}; borderline raised (ND 5.3): {n_border}")
        feedback.pushInfo("Screening classification; the official classification is made by the ND "
                          "Department of Water Resources.")
        return {"OUT": dest, "IMP": idest}
