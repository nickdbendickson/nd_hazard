"""Sensitivity: rerun borderline dams with other breach equations and roughness."""
import os

from qgis import processing
from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterString, QgsProcessingParameterField, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterFile, QgsProcessingParameterEnum,
                       QgsProcessingParameterNumber, QgsProcessingParameterFolderDestination,
                       QgsProcessingParameterFeatureSink,
                       QgsProcessingParameterRasterLayer, QgsFeature,
                       QgsProcessingUtils, QgsProcessingException)

from ..core import breach_hydro as bh, hazard as hz
from . import _common as cm
from .hazard_classification import CRIT_LABELS
from .run_breach_sim import ENGINES, EXE_FILTER
from ..compat import (FILE_FILE, FILE_FOLDER, FT_INT, FT_STRING, NUM_DOUBLE, NUM_INT, SRC_LINE, SRC_POINT, SRC_POLYGON)

VARIANTS = [("F2008", 1.0, "base: Froehlich 2008, n x1"),
            ("F1995", 1.0, "Froehlich 1995"),
            ("VTG-E", 1.0, "Von Thun & Gillette, easily erodible"),
            ("VTG-R", 1.0, "Von Thun & Gillette, erosion resistant"),
            ("F2008", 0.75, "Manning n x0.75"),
            ("F2008", 1.25, "Manning n x1.25"),
            # 0.11: the Runner's other equations (appended so the older indices do not move)
            ("MLM", 1.0, "MacDonald & Langridge-Monopolis 1984 (assumed 12 ft crest, 3H:1V)"),
            ("TR60", 1.0, "NRCS TR-60 peak, triangular hydrograph")]


class Sensitivity(QgsProcessingAlgorithm):
    def name(self): return "sensitivity"
    def displayName(self): return "9 - Sensitivity of the class (borderline dams)"
    def group(self): return "Dam screening"
    def groupId(self): return "dams"
    def createInstance(self): return Sensitivity()

    def shortHelpString(self):
        return ("Re-runs tools 5, 7 and 8 for the dams you give it (tip: select the review queue or "
                "the dams near a class boundary and tick 'Selected features only') with each chosen "
                "variant: the breach equations (Froehlich 2008 / 1995, Von Thun & Gillette "
                "erodible / resistant, MacDonald & Langridge-Monopolis 1984, NRCS TR-60 peak with a "
                "triangular hydrograph) and Manning n x0.75 and x1.25. Reports the class of every "
                "variant, the lowest and highest class, and class_stable = yes if all agree. A class "
                "that flips between reasonable assumptions needs an engineer's look.")

    def initAlgorithm(self, config=None):
        P = self.addParameter
        P(QgsProcessingParameterFeatureSource("DAMS", "Dams with storage (tool 3 output; select a subset)",
                                              [SRC_POINT]))
        P(QgsProcessingParameterFile("CURVES", "Stage-storage curve folder (tool 3)",
                                     behavior=FILE_FOLDER))
        P(QgsProcessingParameterFeatureSource("BPTS", "Breach points (tool 4)", [SRC_POINT]))
        P(QgsProcessingParameterFile("DEMDIR", "Folder of cached dam DEMs (tool 2)",
                                     behavior=FILE_FOLDER))
        P(QgsProcessingParameterEnum("VARS", "Variants", [v[2] for v in VARIANTS], allowMultiple=True,
                                     defaultValue=list(range(len(VARIANTS)))))
        P(QgsProcessingParameterEnum("ENGINE", "Flood engine", ENGINES, defaultValue=0))
        P(QgsProcessingParameterFile("EXE", "LISFLOOD-FP executable", behavior=FILE_FILE,
                                     fileFilter=EXE_FILTER, optional=True))  # 0.17.0: no extension
        P(QgsProcessingParameterNumber("WORKERS", "Dams to run at the same time",
                                       NUM_INT,
                                       max((os.cpu_count() or 2) - 1, 1), minValue=1))
        P(QgsProcessingParameterFeatureSource("STREAMS", "Stream lines for road-crossing cuts",
                                              [SRC_LINE], optional=True))
        P(QgsProcessingParameterFeatureSource("DAMLINES", "DWR dam lines", [SRC_LINE],
                                              optional=True))
        P(QgsProcessingParameterRasterLayer("NLCD", "NLCD land cover (optional)", optional=True))
        P(QgsProcessingParameterFeatureSource("BLD", "Structures", [SRC_POINT,
                                              SRC_POLYGON], optional=True))
        P(QgsProcessingParameterFeatureSource("ROADS", "Roads", [SRC_LINE], optional=True))
        P(QgsProcessingParameterFeatureSource("RAIL", "Railroads", [SRC_LINE], optional=True))
        for key, label, parent, default in (
                ("OCC", "Structures: occupancy class field (as tool 8)", "BLD", "OCC_CLS"),
                ("PRIM", "Structures: primary occupancy field (as tool 8)", "BLD", "PRIM_OCC"),
                ("ROAD_AADT", "Roads: AADT field (as tool 8)", "ROADS", "aadt"),
                ("ROAD_CLASS", "Roads: class field (as tool 8)", "ROADS", "road_class"),
                ("RAIL_TRAINS", "Railroads: trains per day field (as tool 8)", "RAIL", "trains_day"),
                ("RAIL_PAX", "Railroads: passenger field (as tool 8)", "RAIL", "PASSNGR"),
                ("R_SOLE", "Roads: sole access field (as tool 8)", "ROADS", "sole_access"),
                ("L_KIND", "Lifelines: kind field (as tool 8)", "LIFELINES", "kind"),
                ("DAM_CLASS", "Dams: existing hazard class field (as tool 8, 5.3 cascading)", "DAMS",
                 "haz_existing")):
            P(QgsProcessingParameterField(key, label, defaultValue=default,
                                          parentLayerParameterName=parent, optional=True))
        P(QgsProcessingParameterFeatureSource("LIFELINES", "Lifeline facilities (as tool 8)",
                                              [SRC_POINT, SRC_POLYGON],
                                              optional=True))
        P(QgsProcessingParameterString("UNINHABITED", "Structures: occupancy words that mark an UNINHABITED "
                                       "building (as tool 8)", defaultValue=", ".join(hz.UNINHABITED)))
        P(QgsProcessingParameterString("PAXCODES", "Passenger codes (as tool 8)",
                                       defaultValue=", ".join(hz.PAX_CODES)))
        for key, label in (("UNKNOWN_AADT", "Class of an overtopped road with no AADT (as tool 8)"),
                           ("UNKNOWN_TRAINS", "Class of an overtopped railroad with no trains a day (as tool 8)")):
            P(QgsProcessingParameterEnum(key, label, list(hz.nh.CLASSES),
                                         defaultValue=hz.nh.CLASSES.index(hz.nh.MEDIUM)))
        for key, label, default, lo in (
                ("MIND_FT", "Structures, lifelines, dams: inundated from (ft; as tool 8)", hz.DEFAULT_WET_FT,
                 hz.MIN_WET_FT),
                ("RDEP_FT", "Roads/railroads: overtopped from (ft; as tool 8)", hz.DEFAULT_WET_FT, hz.MIN_WET_FT),
                ("RULE", "ND 1D rule depth (ft; as tool 8)", 2.0, 0.1)):
            P(QgsProcessingParameterNumber(key, label, NUM_DOUBLE, default, minValue=lo))
        P(QgsProcessingParameterEnum("CRIT", "Lethality at habitable structures (as tool 8)",
                                     CRIT_LABELS, defaultValue=0))
        P(QgsProcessingParameterFile("CHART_FILE", "Digitized ACER TM 11 chart file (as tool 8; empty = "
                                     "core/acer_tm11_charts.json)", extension="json", optional=True))
        P(QgsProcessingParameterFile("CURVE", "Curve CSV (comparison-only curve criterion)", extension="csv",
                                     optional=True))
        P(QgsProcessingParameterFolderDestination("OUTDIR", "Working folder"))
        P(QgsProcessingParameterFeatureSink("OUT", "Dams with sensitivity result", SRC_POINT))

    def processAlgorithm(self, params, context, feedback):
        dams = self.parameterAsSource(params, "DAMS", context)
        chosen = self.parameterAsEnums(params, "VARS", context) or [0]
        if 0 not in chosen:
            chosen = [0] + list(chosen)
        outdir = self.parameterAsString(params, "OUTDIR", context)
        if hz.METHODS[self.parameterAsEnum(params, "CRIT", context)] == hz.nh.LETHALITY_CHARTS:
            try:                            # fail before the runs, not after them
                hz.load_ready_charts(self.parameterAsString(params, "CHART_FILE", context) or None)
            except hz.nh.ChartsNotDigitized as e:
                raise QgsProcessingException(str(e))
            except (OSError, ValueError) as e:
                raise QgsProcessingException(f"Could not read the chart file: {e}")
        os.makedirs(outdir, exist_ok=True)
        classes = {}                       # dam_id -> {variant label: class}
        for j, vi in enumerate(chosen):
            if feedback.isCanceled(): break
            meth, nm, label = VARIANTS[vi]
            vdir = os.path.join(outdir, f"v{vi}_{meth}_n{nm:g}")
            feedback.pushInfo(f"--- variant {j + 1}/{len(chosen)}: {label}")
            feedback.setProgress(100.0 * j / len(chosen))
            h = processing.run("nddam:breachhydrograph", {
                "DAMS": params["DAMS"], "CURVES": params["CURVES"], "BPTS": params["BPTS"], "MODE": 0,
                "METHOD": list(bh.BREACH_METHODS).index(meth), "SUNNY": False, "EXP": 2.0,
                "OUTDIR": os.path.join(vdir, "hydro"), "OUT": "TEMPORARY_OUTPUT"},
                context=context, feedback=feedback, is_child_algorithm=True)
            s = processing.run("nddam:runbreachsim", {
                "DAMS": h["OUT"], "BPTS": params["BPTS"], "DEMDIR": params["DEMDIR"], "TSRC": 0,
                "ENGINE": self.parameterAsEnum(params, "ENGINE", context), "EXE": self.parameterAsString(params, "EXE", context), "SCEN": 0,
                "CURVES": params["CURVES"], "CELL": 10, "SIMH": 12, "SIMMAX": 96, "CORR": True,
                "CORRW": 600, "RMAX": 12000, "STREAMS": params.get("STREAMS"),
                "DAMLINES": params.get("DAMLINES"), "CASCADE": 1, "NLCD": params.get("NLCD"),
                "NDEF": 0.05, "NMULT": nm, "WORKERS": self.parameterAsInt(params, "WORKERS", context), "TMO": 120,
                "SKIPTRI": False, "KEEP": False, "OUTDIR": os.path.join(vdir, "sim"),
                "OUT": "TEMPORARY_OUTPUT"}, context=context, feedback=feedback, is_child_algorithm=True)
            z = processing.run("nddam:hazardclassification", {
                "SIM": s["OUT"], "BLD": params.get("BLD"), "ROADS": params.get("ROADS"),
                "RAIL": params.get("RAIL"), "LIFELINES": params.get("LIFELINES"),
                **{k: self.parameterAsString(params, k, context) for k in
                   ("OCC", "PRIM", "ROAD_AADT", "ROAD_CLASS", "RAIL_TRAINS", "RAIL_PAX", "R_SOLE", "L_KIND",
                    "DAM_CLASS", "UNINHABITED", "PAXCODES")},
                **{k: self.parameterAsEnum(params, k, context) for k in ("UNKNOWN_AADT", "UNKNOWN_TRAINS")},
                **{k: self.parameterAsDouble(params, k, context) for k in ("MIND_FT", "RDEP_FT", "RULE")},
                "CRIT": self.parameterAsEnum(params, "CRIT", context), "CURVE": self.parameterAsString(params, "CURVE", context),
                "CHART_FILE": self.parameterAsString(params, "CHART_FILE", context),
                "OUT": "TEMPORARY_OUTPUT", "IMP": "TEMPORARY_OUTPUT"},
                context=context, feedback=feedback, is_child_algorithm=True)
            lyr = QgsProcessingUtils.mapLayerFromString(z["OUT"], context)
            if lyr is None:
                raise QgsProcessingException("could not read the hazard output of a variant")
            for f in lyr.getFeatures():
                classes.setdefault(str(f["dam_id"]), {})[label] = f["hz_class"]

        new = [("sens_runs", FT_INT), ("class_min", FT_STRING), ("class_max", FT_STRING),
               ("class_stable", FT_STRING), ("sens_detail", FT_STRING)]
        fields, put = cm.extend_fields(dams.fields(), new)
        sink, dest = cm.us_sink(self, params, "OUT", context, fields, dams.wkbType(), dams.sourceCrs())
        names = dams.fields().names()
        for f in dams.getFeatures():
            dam_id = str(cm.fval(f, names, "dam_id") or f.id())
            c = classes.get(dam_id, {})
            ranked = [v for v in c.values() if v in hz.RANK]
            a = cm.blank_attrs(fields, f)
            put(a, "sens_runs", len(c))
            if ranked:
                lo = min(ranked, key=lambda v: hz.RANK[v]); hi = max(ranked, key=lambda v: hz.RANK[v])
                put(a, "class_min", lo); put(a, "class_max", hi)
                put(a, "class_stable", "yes" if lo == hi and len(ranked) == len(c) else "no")
            put(a, "sens_detail", "; ".join(f"{k}: {v}" for k, v in c.items()))
            o = QgsFeature(fields); o.setGeometry(f.geometry()); o.setAttributes(a)
            sink.addFeature(o)
        return {"OUT": dest}
