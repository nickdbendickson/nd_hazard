# -*- coding: utf-8 -*-
"""QGIS Processing provider "ND Dam Hazard": the main steps as Processing
algorithms, so they can be run from the toolbox, in batch mode, in
graphical models and from the command line (qgis_process).

  ndhazard:acerzones   ACER TM 11 zone rasters (Dmax / Vmax) - runs in the
                       background
  ndhazard:classify    ND REG_05.2026 / TM 11 hazard classification (uses and
                       adds project layers, so it runs on the main thread)
  ndhazard:dsswise     import a DSS-WISE Lite results package
  ndhazard:batch       every dam folder of a folder (v2.9: WORKERS > 1 runs
                       that many dams at once as qgis_process children)
  ndhazard:creepwatch  hazard-creep watch over a folder of dams (watch.py)
  ndhazard:damsinseries, portfolio, reproduce, fillform (v2.8 / v2.9)
  v2.10 (the dam suite with ND Dam Breach Runner / ND Dam Breach Screening):
  ndhazard:ensemble        class probability over a model ensemble (ensemble.py)
  ndhazard:modelgate       checks of the flood model before the classification
  ndhazard:quickcreep      new structures in every dam's flood area, nothing re-run
  ndhazard:reviewpackage   reviewer's view of a submittal package; reviewsummary
  ndhazard:runnerculverts  culvert / bridge lines for the ND Dam Breach Runner
  ndhazard:suiteexport     data package for ND Dam Breach Screening (tool 0)
  ndhazard:chartsignoff    ACER TM 11 chart file for the other two plugins, sign-off
  ndhazard:rulecompare     this plugin's rules against the Runner / Screening rules
  2.17.0:
  ndhazard:getdamdata      Get data for this dam (or all dams): <dam folder>/data/ and
                           dam.json's data section (suite_data_tools, the same tool in the
                           three plugins)
"""

import os
import traceback

from qgis.core import (
    Qgis, QgsProcessingProvider, QgsProcessingAlgorithm, QgsProcessingException,
    QgsProcessingParameterRasterLayer, QgsProcessingParameterVectorLayer,
    QgsProcessingParameterFile, QgsProcessingParameterFolderDestination,
    QgsProcessingParameterBoolean, QgsProcessingParameterNumber, QgsProcessingParameterEnum,
    QgsProcessingParameterString,
    QgsProcessing, QgsProcessingOutputRasterLayer, QgsProcessingOutputString,
    QgsProcessingOutputFile, QgsProcessingOutputFolder,
)

from .compat import _first
from .constants import (
    PLUGIN_NAME, DEFAULT_EXTENT_BUFFER_FT, OUTPUT_FOLDER_NAME, MAPBOOK_SCALES,
    MAPBOOK_DEFAULT_SCALE,
)
from .helpers import CancelledByUser, get_layer_path

PROVIDER_ID = "ndhazard"
WT_TIMEOUT_MIN = 60             # watch worker processes: default limit per dam (minutes)


def _enum(*getters, default=None):
    return _first(*getters, default=default)


NUM_INTEGER = _enum(lambda: Qgis.ProcessingNumberParameterType.Integer,
                    lambda: QgsProcessingParameterNumber.Type.Integer,
                    lambda: QgsProcessingParameterNumber.Integer)
NUM_DOUBLE = _enum(lambda: Qgis.ProcessingNumberParameterType.Double,
                   lambda: QgsProcessingParameterNumber.Type.Double,
                   lambda: QgsProcessingParameterNumber.Double)
FILE_FOLDER = _enum(lambda: Qgis.ProcessingFileParameterBehavior.Folder,
                    lambda: QgsProcessingParameterFile.Behavior.Folder,
                    lambda: QgsProcessingParameterFile.Folder)
FILE_FILE = _enum(lambda: Qgis.ProcessingFileParameterBehavior.File,
                  lambda: QgsProcessingParameterFile.Behavior.File,
                  lambda: QgsProcessingParameterFile.File)
NO_THREADING = _enum(lambda: Qgis.ProcessingAlgorithmFlag.NoThreading,
                     lambda: QgsProcessingAlgorithm.Flag.FlagNoThreading,
                     lambda: QgsProcessingAlgorithm.FlagNoThreading)
SRC_LINE = _enum(lambda: Qgis.ProcessingSourceType.VectorLine,
                 lambda: QgsProcessing.SourceType.TypeVectorLine,
                 lambda: QgsProcessing.TypeVectorLine)
SRC_POINT = _enum(lambda: Qgis.ProcessingSourceType.VectorPoint,
                  lambda: QgsProcessing.SourceType.TypeVectorPoint,
                  lambda: QgsProcessing.TypeVectorPoint)
SRC_POLYGON = _enum(lambda: Qgis.ProcessingSourceType.VectorPolygon,
                    lambda: QgsProcessing.SourceType.TypeVectorPolygon,
                    lambda: QgsProcessing.TypeVectorPolygon)


# ---------------------------------------------------------------- worker mode --
# The hazard creep watch (watch.py) and, since v2.9, the batch classification
# can run every dam as its own `qgis_process
# run ndhazard:classify` child process. The classify algorithm's parameters do
# not cover every setting the watch needs (reuse_evaluation, save_evaluation,
# add_to_project, download_flowlines, ... - the pipeline's DEFAULTS), so the
# optional CONFIG_JSON parameter names a small JSON file {"<config key>": value}
# with pipeline settings. The file wins over the matching parameters. Only the
# JSON-safe scalar settings of pipeline.DEFAULTS are accepted (bool / number /
# text, of the same kind as the default); layers, "scenarios", "output_folder"
# and "dam_folder" are never taken from the file (the dam folder is the
# DAM_FOLDER parameter, its rasters are found in it). Unknown or wrongly typed
# keys are ignored and named in the log. Anything else a worker needs travels
# in the environment (offline data pack: ND_DAM_HAZARD_OFFLINE_PACK / _ONLY).
# v2.9: the file may also name, under RESULT_KEY, a file where the algorithm
# writes its result as JSON (classes, lives in jeopardy, governing reason,
# judgment items, report) - how the parent reads a worker's result without
# parsing its text output. Older plugin versions ignore the key (and say so).
_WORKER_RESERVED = ("output_folder", "dam_folder", "scenarios")
_SCALARS = (bool, int, float, str)
RESULT_KEY = "_result_file"
RESULT_VERSION = 1


def worker_setting_keys():
    """The config keys a CONFIG_JSON file may set."""
    from .pipeline import DEFAULTS
    return sorted(k for k, v in DEFAULTS.items()
                  if k not in _WORKER_RESERVED and not k.endswith("_layer")
                  and (v is None or isinstance(v, _SCALARS)))


def _setting_ok(default, value):
    if value is not None and not isinstance(value, _SCALARS):
        return False
    if default is None or value is None:
        return True
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, str)


def worker_settings(cfg):
    """The part of a pipeline config a worker process can be given."""
    from .pipeline import DEFAULTS
    return {k: cfg[k] for k in worker_setting_keys() if k in cfg
            and _setting_ok(DEFAULTS.get(k), cfg[k])}


def write_worker_settings(path, settings):
    import json
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=1, sort_keys=True)
    return path


def _read_worker_file(path):
    """(settings, ignored keys, result file path or "") of a CONFIG_JSON file."""
    import json
    from .pipeline import DEFAULTS
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("the settings file must hold a JSON object {\"setting\": value}")
    target = data.get(RESULT_KEY)
    target = target if isinstance(target, str) else ""
    ok = set(worker_setting_keys())
    good = {k: v for k, v in data.items() if k in ok and _setting_ok(DEFAULTS.get(k), v)}
    ignored = sorted(k for k in data if k not in good and not (k == RESULT_KEY and target))
    return good, ignored, target


def read_worker_settings(path):
    """(settings, ignored keys) of a CONFIG_JSON file. Raises ValueError /
    OSError when the file cannot be read or is not a JSON object."""
    good, ignored, _target = _read_worker_file(path)
    return good, ignored


def write_worker_result(path, cfg, res):
    """The result of a worker's classification as JSON (RESULT_KEY)."""
    import json
    import time
    nd, tm = res.get("nd") or {}, res.get("tm11") or {}
    try:
        lij = float(tm.get("lij") or 0.0)
    except (TypeError, ValueError):
        lij = 0.0
    data = {"version": RESULT_VERSION, "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "dam_folder": cfg.get("dam_folder") or "",
            "output_folder": cfg.get("output_folder") or "",
            "governing": str(res.get("governing") or ""),
            "nd": nd.get("cls") or "", "tm11": tm.get("cls") or "", "lij": lij,
            "reasons": [str(r) for r in (nd.get("reasons") or [])],
            "judgment_items": len(nd.get("judgment") or []),
            "report": str(res.get("report") or ""), "one_d": bool(cfg.get("one_d"))}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, path)
    return path


def read_worker_result(path):
    """The dict written by write_worker_result, or None (missing, unreadable,
    another version)."""
    import json
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != RESULT_VERSION:
        return None
    return data


def _progress(feedback, lo=0.0, hi=100.0):
    def cb(pct, msg=""):
        if feedback is None:
            return
        if feedback.isCanceled():
            raise CancelledByUser()
        feedback.setProgress(lo + (hi - lo) * max(0.0, min(100.0, float(pct))) / 100.0)
        if msg:
            try:
                feedback.setProgressText(msg)
            except Exception:
                pass
    return cb


def _report_traceback(feedback):
    """Put the traceback of the exception being handled in the Processing log."""
    try:
        feedback.reportError(traceback.format_exc(), False)
    except Exception:
        pass


class _Base(QgsProcessingAlgorithm):
    def group(self):
        return "Dam hazard classification"

    def groupId(self):
        return "damhazard"

    def createInstance(self):
        return type(self)()

    def _raster_path(self, parameters, name, context):
        lyr = self.parameterAsRasterLayer(parameters, name, context)
        return get_layer_path(lyr) if lyr is not None else None

    def _int(self, parameters, name, context, default):
        try:
            return int(self.parameterAsInt(parameters, name, context))
        except AttributeError:
            pass
        try:
            return int(parameters.get(name, default))
        except (TypeError, ValueError):
            return int(default)


class AcerZonesAlgorithm(_Base):
    def name(self):
        return "acerzones"

    def displayName(self):
        return "ACER TM 11 hazard zone rasters"

    def shortHelpString(self):
        return ("Houses on foundations, mobile homes and passenger vehicles (TM 11 Figs 2-4) "
                "from the maximum depth with the maximum velocity (Dmax / Vmax).")

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer("DEPTH", "Maximum depth (ft)"))
        self.addParameter(QgsProcessingParameterRasterLayer(
            "VELOCITY", "Maximum velocity (ft/s)"))
        self.addParameter(QgsProcessingParameterFolderDestination("OUTPUT", "Output folder"))
        for key, label in (("FOUNDATIONS", "ACER zones - houses on foundations"),
                           ("MOBILE", "ACER zones - mobile homes"),
                           ("VEHICLES", "ACER zones - passenger vehicles")):
            self.addOutput(QgsProcessingOutputRasterLayer(key, label))
        self.addOutput(QgsProcessingOutputString("METHOD_USED", "Method used"))

    def processAlgorithm(self, parameters, context, feedback):
        from .hazard import METHOD_MAXMAX
        from .processing import generate_hazard_rasters
        depth = self.parameterAsRasterLayer(parameters, "DEPTH", context)
        vel = self.parameterAsRasterLayer(parameters, "VELOCITY", context)
        if vel is None:
            raise QgsProcessingException("A maximum velocity raster is needed (Dmax / Vmax).")
        # parameterAsFileOutput resolves "Save to temporary folder"
        out = self.parameterAsFileOutput(parameters, "OUTPUT", context)
        os.makedirs(out, exist_ok=True)
        try:
            res = generate_hazard_rasters(out, depth, vel, progress_cb=_progress(feedback),
                                          add_to_project=False)
        except CancelledByUser as e:
            raise QgsProcessingException("Cancelled") from e
        except (RuntimeError, ValueError, OSError) as e:
            raise QgsProcessingException(str(e)) from e
        except Exception as e:                    # never an unhandled crash
            _report_traceback(feedback)
            raise QgsProcessingException("Unexpected error: {!r}".format(e)) from e
        feedback.pushInfo("Method: max depth with max velocity (Dmax / Vmax)")
        return {"OUTPUT": out, "FOUNDATIONS": res.get("found"), "MOBILE": res.get("mobile"),
                "VEHICLES": res.get("vehicle"), "METHOD_USED": METHOD_MAXMAX}


class ClassifyAlgorithm(_Base):
    def name(self):
        return "classify"

    def displayName(self):
        return "Dam hazard classification (ND REG_05.2026 / ACER TM 11)"

    def shortHelpString(self):
        return ("Runs the plugin's Hazard Classification for a dam folder: structures "
                "(project NSI / ND GIS Hub layers, FEMA_Structures outlines in the outputs "
                "folder, optional downloads), roads, rail, consequences, optional no-breach "
                "baseline for the incremental classification, summary and PDF report in "
                "<dam folder>/outputs.")

    def flags(self):
        return super().flags() | NO_THREADING

    def initAlgorithm(self, config=None):
        P = self.addParameter
        P(QgsProcessingParameterFile("DAM_FOLDER", "Dam project folder", behavior=FILE_FOLDER))
        P(QgsProcessingParameterRasterLayer("DEPTH", "Breach: maximum depth (ft) - empty: "
                                            "found in the dam folder", optional=True))
        P(QgsProcessingParameterRasterLayer("VELOCITY", "Breach: maximum velocity (ft/s)",
                                            optional=True))
        P(QgsProcessingParameterRasterLayer("ARRIVAL", "Breach: arrival time (h)",
                                            optional=True))
        P(QgsProcessingParameterRasterLayer("DXV", "Breach: max depth x velocity (ft2/s)",
                                            optional=True))
        P(QgsProcessingParameterRasterLayer("BASE_DEPTH", "No-breach baseline: max depth",
                                            optional=True))
        P(QgsProcessingParameterRasterLayer("BASE_VELOCITY", "No-breach baseline: max "
                                            "velocity", optional=True))
        P(QgsProcessingParameterRasterLayer("BASE_ARRIVAL", "No-breach baseline: arrival",
                                            optional=True))
        P(QgsProcessingParameterVectorLayer("ROADS", "Road lines", [SRC_LINE], optional=True))
        P(QgsProcessingParameterVectorLayer("AADT", "AADT layer", optional=True))
        P(QgsProcessingParameterVectorLayer("RAIL", "Rail lines (empty: download)", [SRC_LINE],
                                            optional=True))
        P(QgsProcessingParameterVectorLayer("OUTLINES", "Extra building outlines",
                                            [SRC_POLYGON], optional=True))
        P(QgsProcessingParameterNumber("EXTENT_FT", "Study area: raster extent + (ft)",
                                       type=NUM_DOUBLE, defaultValue=DEFAULT_EXTENT_BUFFER_FT,
                                       minValue=0))
        P(QgsProcessingParameterBoolean("KEEP_DRY", "Keep structures without flood values",
                                        defaultValue=True))
        P(QgsProcessingParameterBoolean("ONE_D", "1D model (depth > 2 ft rule)",
                                        defaultValue=False))
        P(QgsProcessingParameterBoolean("JUDGMENT", "Judgment zone counts as probable loss "
                                        "of life", defaultValue=True))
        P(QgsProcessingParameterBoolean("DOWNLOAD_NSI", "Download NSI", defaultValue=True))
        P(QgsProcessingParameterBoolean("DOWNLOAD_ADDRESS", "Download ND GIS Hub address "
                                        "points (if none loaded)", defaultValue=True))
        P(QgsProcessingParameterBoolean("DOWNLOAD_USA", "Download FEMA USA Structures "
                                        "outlines (only if no FEMA_Structures.gpkg)",
                                        defaultValue=True))
        P(QgsProcessingParameterBoolean("DOWNLOAD_RAIL", "Download rail lines and crossings",
                                        defaultValue=True))
        P(QgsProcessingParameterBoolean("DOWNLOAD_DAMS", "Download National Inventory of Dams "
                                        "(dams in series, 5.3(c))", defaultValue=True))
        P(QgsProcessingParameterString("SUBJECT_NID", "NID ID of the dam being classified",
                                       optional=True))
        P(QgsProcessingParameterBoolean("DOWNLOAD_LIFELINES", "Download lifeline facilities "
                                        "(ND GIS Hub, EIA, OpenStreetMap)",
                                        defaultValue=True))
        P(QgsProcessingParameterVectorLayer("LIFELINES", "Lifeline facilities layer",
                                            optional=True))
        P(QgsProcessingParameterVectorLayer("MODEL_BOUNDARY", "Model boundary polygon (2D flow "
                                            "area)", [SRC_POLYGON], optional=True))
        P(QgsProcessingParameterBoolean("EAP", "EAP notification list and arrival-time maps",
                                        defaultValue=True))
        P(QgsProcessingParameterBoolean("DOWNLOAD_ROADS", "Download ND roads and traffic "
                                        "counts when no road layer is given",
                                        defaultValue=True))
        P(QgsProcessingParameterBoolean("DOWNLOAD_CROSSINGS", "Download bridges, culverts and "
                                        "crossings (NBI, OpenStreetMap)", defaultValue=True))
        P(QgsProcessingParameterBoolean("SPREADSHEETS", "Also write CSV / XLSX copies of the "
                                        "result layers", defaultValue=True))
        P(QgsProcessingParameterNumber("WARNING_HR", "Warning issued (h after breach start)",
                                       type=NUM_DOUBLE, defaultValue=0.0, minValue=0))
        P(QgsProcessingParameterBoolean("REPORT", "PDF report", defaultValue=True))
        # v2.20.0
        P(QgsProcessingParameterBoolean("DV_SPREADSHEET", "Plot the structures on a copy of the "
                                        "depth-velocity spreadsheet (and in the report)",
                                        defaultValue=True))
        P(QgsProcessingParameterFile("DV_TEMPLATE", "Depth-velocity spreadsheet template "
                                     "(empty: the one that comes with the plugin)",
                                     behavior=FILE_FILE, extension="xlsx", optional=True))
        # v2.5
        P(QgsProcessingParameterBoolean("DOWNLOAD_CAMPS", "Download campgrounds / RV parks "
                                        "(OpenStreetMap)", defaultValue=True))
        P(QgsProcessingParameterVectorLayer("PARCELS", "Parcels (future development "
                                            "screening)", [SRC_POLYGON], optional=True))
        P(QgsProcessingParameterBoolean("EAP_LIST", "EAP: notification list and arrival "
                                        "maps", defaultValue=True))
        P(QgsProcessingParameterBoolean("EAP_MAPBOOK", "EAP: inundation map book (PDF)",
                                        defaultValue=True))
        P(QgsProcessingParameterEnum("MAPBOOK_SCALE", "EAP: map book scale",
                                     options=[t for t, _s in MAPBOOK_SCALES],
                                     defaultValue=[s for _t, s in MAPBOOK_SCALES].index(
                                         MAPBOOK_DEFAULT_SCALE)))
        P(QgsProcessingParameterBoolean("EAP_TRAVEL", "EAP: travel-time table along the "
                                        "channel", defaultValue=True))
        P(QgsProcessingParameterVectorLayer("CENTERLINE", "EAP: channel centerline (empty: "
                                            "trace USGS NHD flowlines)", [SRC_LINE],
                                            optional=True))
        P(QgsProcessingParameterBoolean("COMPARE", "Compare with the last run",
                                        defaultValue=True))
        # v2.8: pipeline settings without a parameter of their own (hazard creep watch
        # worker processes; see worker_setting_keys)
        P(QgsProcessingParameterFile("CONFIG_JSON", "Advanced: JSON file with pipeline "
                                     "settings (overrides the parameters above)",
                                     behavior=FILE_FILE, extension="json", optional=True))
        self.addOutput(QgsProcessingOutputFolder("OUTPUT_FOLDER", "Output folder"))
        self.addOutput(QgsProcessingOutputString("ND_CLASS", "ND hazard class"))
        self.addOutput(QgsProcessingOutputString("TM11_CLASS", "ACER TM 11 class"))
        self.addOutput(QgsProcessingOutputFile("REPORT_PDF", "PDF report"))
        self.addOutput(QgsProcessingOutputFile("SUMMARY", "Summary (TXT)"))

    def processAlgorithm(self, parameters, context, feedback):
        from .pipeline import config, run_classification
        from .scenarios import Scenario, ROLE_BASELINE
        dam = self.parameterAsFile(parameters, "DAM_FOLDER", context)
        if not dam or not os.path.isdir(dam):
            raise QgsProcessingException("Dam folder not found: {}".format(dam))
        rp = {k: self._raster_path(parameters, k, context) for k in (
            "DEPTH", "VELOCITY", "ARRIVAL", "DXV", "BASE_DEPTH", "BASE_VELOCITY",
            "BASE_ARRIVAL")}
        if not rp["DEPTH"]:
            from .autodetect import find_rasters, describe
            found = find_rasters(dam)
            if not found.get("depth"):
                raise QgsProcessingException("No depth raster given and none found in {}"
                                             .format(dam))
            for key, fk in (("DEPTH", "depth"), ("VELOCITY", "vel"), ("ARRIVAL", "arr"),
                            ("DXV", "dv"), ("BASE_DEPTH", "base_depth"),
                            ("BASE_VELOCITY", "base_vel"), ("BASE_ARRIVAL", "base_arr")):
                rp[key] = rp[key] or found.get(fk)
            feedback.pushInfo("Rasters found in the dam folder: " + describe(found, dam))
        breach = Scenario("Breach", rp["DEPTH"], rp["VELOCITY"], rp["ARRIVAL"], dv=rp["DXV"])
        scenarios = [breach]
        if rp["BASE_DEPTH"]:
            scenarios.append(Scenario("No breach", rp["BASE_DEPTH"], rp["BASE_VELOCITY"],
                                      rp["BASE_ARRIVAL"], role=ROLE_BASELINE))
            breach.baseline = "No breach"
        from .scenarios import raster_reuse_problems
        reuse_warnings = []
        reuse = raster_reuse_problems(scenarios, reuse_warnings)            # v2.9.1
        for w in reuse_warnings:
            feedback.pushInfo("WARNING: " + w)
        if reuse:
            raise QgsProcessingException(
                "; ".join(reuse) + ". Give DAM_FOLDER only (the rasters are found by name, "
                "baselines included) or the right raster for each parameter - see the "
                "README, 'Handing results to this plugin'.")

        def lyr(name):
            return self.parameterAsVectorLayer(parameters, name, context)
        rail = lyr("RAIL")
        dl_rail = self.parameterAsBool(parameters, "DOWNLOAD_RAIL", context)
        cfg = config(
            output_folder=os.path.join(dam, OUTPUT_FOLDER_NAME), dam_folder=dam,
            scenarios=scenarios,
            extent_buffer_ft=self.parameterAsDouble(parameters, "EXTENT_FT", context),
            keep_dry=self.parameterAsBool(parameters, "KEEP_DRY", context),
            one_d=self.parameterAsBool(parameters, "ONE_D", context),
            judgment_as_probable=self.parameterAsBool(parameters, "JUDGMENT", context),
            download_nsi=self.parameterAsBool(parameters, "DOWNLOAD_NSI", context),
            download_address=self.parameterAsBool(parameters, "DOWNLOAD_ADDRESS", context),
            download_usa_structures=self.parameterAsBool(parameters, "DOWNLOAD_USA", context),
            download_rail=dl_rail and rail is None, download_crossings=dl_rail,
            road_layer=lyr("ROADS"), aadt_layer=lyr("AADT"), rail_layer=rail,
            extra_outline_layer=lyr("OUTLINES"),
            warning_issued_hr=self.parameterAsDouble(parameters, "WARNING_HR", context),
            report_pdf=self.parameterAsBool(parameters, "REPORT", context),
            download_dams=self.parameterAsBool(parameters, "DOWNLOAD_DAMS", context),
            subject_dam_nid=self.parameterAsString(parameters, "SUBJECT_NID", context) or "",
            download_lifelines=self.parameterAsBool(parameters, "DOWNLOAD_LIFELINES", context),
            lifeline_layer=lyr("LIFELINES"), model_boundary_layer=lyr("MODEL_BOUNDARY"),
            eap=self.parameterAsBool(parameters, "EAP", context),
            download_roads=self.parameterAsBool(parameters, "DOWNLOAD_ROADS", context),
            download_crossings_structures=self.parameterAsBool(parameters,
                                                               "DOWNLOAD_CROSSINGS", context),
            spreadsheets=self.parameterAsBool(parameters, "SPREADSHEETS", context),
            download_camps=self.parameterAsBool(parameters, "DOWNLOAD_CAMPS", context),
            parcel_layer=lyr("PARCELS"),
            eap_list=self.parameterAsBool(parameters, "EAP_LIST", context),
            eap_mapbook=self.parameterAsBool(parameters, "EAP_MAPBOOK", context),
            mapbook_scale=[s for _t, s in MAPBOOK_SCALES][
                self.parameterAsEnum(parameters, "MAPBOOK_SCALE", context)],
            eap_travel=self.parameterAsBool(parameters, "EAP_TRAVEL", context),
            centerline_layer=lyr("CENTERLINE"),
            compare_last_run=self.parameterAsBool(parameters, "COMPARE", context),
            dv_spreadsheet=self.parameterAsBool(parameters, "DV_SPREADSHEET", context),
            dv_template=self.parameterAsFile(parameters, "DV_TEMPLATE", context) or "")
        extra_path = self.parameterAsFile(parameters, "CONFIG_JSON", context)
        result_path = ""
        if extra_path:
            try:
                extra, ignored, result_path = _read_worker_file(extra_path)
            except (OSError, ValueError) as e:
                raise QgsProcessingException("Settings file {}: {}".format(extra_path, e)) \
                    from e
            cfg.update(extra)
            feedback.pushInfo("Settings from {}: {} setting(s){}".format(
                extra_path, len(extra), "; ignored: " + ", ".join(ignored) if ignored else ""))
        try:
            res = run_classification(cfg, progress=_progress(feedback),
                                     log=lambda m: feedback.pushInfo(str(m)))
        except CancelledByUser as e:
            raise QgsProcessingException("Cancelled") from e
        except (RuntimeError, ValueError, OSError) as e:
            raise QgsProcessingException(str(e)) from e
        except Exception as e:                    # never an unhandled crash
            _report_traceback(feedback)
            raise QgsProcessingException("Unexpected error: {!r}".format(e)) from e
        for line in res["lines"]:
            feedback.pushInfo(line)
        if result_path:
            try:
                write_worker_result(result_path, cfg, res)
            except (OSError, ValueError, TypeError) as e:
                feedback.pushInfo("WARNING: the result file {} could not be written: {}"
                                  .format(result_path, e))
        return {"OUTPUT_FOLDER": cfg["output_folder"], "ND_CLASS": res["nd"]["cls"],
                "TM11_CLASS": res["tm11"]["cls"], "REPORT_PDF": res.get("report"),
                "SUMMARY": os.path.join(cfg["output_folder"],
                                        "Hazard_Classification_Summary.txt")}


class DssWiseAlgorithm(_Base):
    def name(self):
        return "dsswise"

    def displayName(self):
        return "Import DSS-WISE Lite results"

    def shortHelpString(self):
        return ("Converts a DSS-WISE Lite Simulation Results Package (zip or folder) to "
                "depth (ft), velocity (ft/s), depth x velocity (ft2/s) and arrival (h) "
                "GeoTIFFs and summarises the HCOM and discharge tables. Roles and units are "
                "recognised from the file names; check DSSWISE_Import_Summary.txt.")

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFile("PACKAGE", "Results package (zip)",
                                                     behavior=FILE_FILE, extension="zip",
                                                     optional=True))
        self.addParameter(QgsProcessingParameterFile("FOLDER", "or: unzipped package folder",
                                                     behavior=FILE_FOLDER, optional=True))
        for key, label, opts in (
                ("DEPTH_UNITS", "Depth units", ["from file names", "ft", "m"]),
                ("VEL_UNITS", "Velocity units", ["from file names", "ft/s", "m/s"]),
                ("DV_UNITS", "Depth x velocity units", ["from file names", "ft2/s", "m2/s"]),
                ("TIME_UNITS", "Arrival time units", ["from file names", "h", "min", "s"])):
            self.addParameter(QgsProcessingParameterEnum(key, label, options=opts,
                                                         defaultValue=0))
        self.addParameter(QgsProcessingParameterBoolean(
            "ACCEPT_ASSUMED", "Accept US units (ft, ft/s, h) where a file name states none",
            defaultValue=False))
        self.addParameter(QgsProcessingParameterFolderDestination("OUTPUT", "Output folder"))
        for key, label in (("DEPTH", "Depth (ft)"), ("VELOCITY", "Velocity (ft/s)"),
                           ("DXV", "Depth x velocity (ft2/s)"), ("ARRIVAL", "Arrival (h)")):
            self.addOutput(QgsProcessingOutputRasterLayer(key, label))

    UNIT_PARAMS = {"DEPTH_UNITS": ("Max depth", [None, "ft", "m"]),
                   "VEL_UNITS": ("Max velocity", [None, "ft/s", "m/s"]),
                   "DV_UNITS": ("Max depth x velocity", [None, "ft2/s", "m2/s"]),
                   "TIME_UNITS": ("Arrival time", [None, "h", "min", "s"])}

    def processAlgorithm(self, parameters, context, feedback):
        from . import dsswise as DW
        src = self.parameterAsFile(parameters, "PACKAGE", context) or \
            self.parameterAsFile(parameters, "FOLDER", context)
        if not src:
            raise QgsProcessingException("Give the package zip or its folder.")
        out = self.parameterAsFileOutput(parameters, "OUTPUT", context)
        try:
            os.makedirs(out, exist_ok=True)
            folder = DW.open_package(src, out)
            items = DW.scan(folder)
        except Exception as e:
            raise QgsProcessingException("The package could not be read: {}".format(e)) from e
        overrides = {}
        for key, (role, choices) in self.UNIT_PARAMS.items():
            u = choices[self.parameterAsEnum(parameters, key, context)]
            if u:
                overrides[role] = u
        for it in items:
            if it.role in overrides:
                it.units, it.basis = overrides[it.role], "set by user"
        dups = DW.duplicate_roles(items)
        for role, names in dups.items():
            msg = "{} files recognised as '{}': {} - {} is used".format(
                len(names), role, ", ".join(names), DW.chosen(items, role).name)
            if hasattr(feedback, "pushWarning"):
                feedback.pushWarning(msg)
            else:
                feedback.pushInfo(msg)
        assumed = DW.assumed_units(items)
        if assumed and not self.parameterAsBool(parameters, "ACCEPT_ASSUMED", context):
            raise QgsProcessingException(
                "The units of {} are not stated in the file names. Set the unit parameters "
                "(check the DSS-WISE report) or tick 'Accept US units'.".format(
                    ", ".join(it.name for it in assumed)))
        for it in items:
            feedback.pushInfo("{}: {} [{}{}]".format(it.name, it.role, it.units or "-",
                                                     ", " + it.basis if it.basis else ""))
        try:
            res = DW.convert(items, out, name=os.path.splitext(os.path.basename(os.path.normpath(src)))[0],
                             progress=_progress(feedback))
        except CancelledByUser as e:
            raise QgsProcessingException("Cancelled") from e
        except (RuntimeError, ValueError, OSError) as e:
            raise QgsProcessingException(str(e)) from e
        except Exception as e:                    # never an unhandled crash
            _report_traceback(feedback)
            raise QgsProcessingException("Unexpected error: {!r}".format(e)) from e
        for line in res["lines"] + ["NOTE: " + n for n in res["notes"]]:
            feedback.pushInfo(line)
        p = res["paths"]
        return {"OUTPUT": out, "DEPTH": p.get(DW.R_DEPTH), "VELOCITY": p.get(DW.R_VEL),
                "DXV": p.get(DW.R_DV), "ARRIVAL": p.get(DW.R_ARR)}


BATCH_FIELDS = ("Dam", "Folder", "Status", "ND_Class", "TM11_Class", "Lives_In_Jeopardy",
                "Governing_Reason", "Judgment_Items", "Depth_Raster", "Baseline", "Report",
                "Message")
BATCH_CSV = "Batch_Hazard_Classification.csv"
_ONE_D_NOTE = "depth-only (1D) criterion - no velocity data"
_NO_DOWNLOADS = ("download_nsi", "download_address", "download_usa_structures", "download_rail",
                 "download_crossings", "download_roads", "download_dams", "download_lifelines",
                 "download_crossings_structures", "download_camps", "download_flowlines",
                 "download_places")


def _prepare_dam(folder, cfg_over):
    """(row, kw) of one dam folder: the batch row with Dam / Folder / Depth_Raster /
    Baseline, and the pipeline settings (config keywords) of its run - kw is None
    when the dam is not classified (row Status "skipped" / "failed" says why).
    The same for the sequential and the parallel mode."""
    from .autodetect import find_rasters
    from .helpers import dam_display_name
    from .scenarios import Scenario, ROLE_BASELINE
    row = dict.fromkeys(BATCH_FIELDS, "")
    row.update({"Dam": dam_display_name(folder), "Folder": folder})
    try:
        found = find_rasters(folder)
    except Exception as e:                  # one dam never stops the batch
        row.update({"Status": "failed", "Message": "rasters not readable: {}".format(e)[:250]})
        return row, None
    if not found.get("depth"):
        row.update({"Status": "skipped", "Message": "no depth raster found"})
        return row, None
    scen = [Scenario("Breach", found["depth"], found.get("vel"), found.get("arr"),
                     dv=found.get("dv"),
                     baseline="No breach" if found.get("base_depth") else None)]
    if found.get("base_depth"):
        scen.append(Scenario("No breach", found["base_depth"], found.get("base_vel"),
                             found.get("base_arr"), role=ROLE_BASELINE))
    row.update({"Depth_Raster": os.path.relpath(found["depth"], folder),
                "Baseline": os.path.relpath(found["base_depth"], folder)
                if found.get("base_depth") else ""})
    kw = dict(output_folder=os.path.join(folder, OUTPUT_FOLDER_NAME), dam_folder=folder,
              scenarios=scen, add_to_project=False,
              # v2.5.1: ACER zones need Vmax (Dmax / Vmax only); without it the
              # depth-only (1D) criterion is used
              one_d=not found.get("vel"))
    kw.update(cfg_over or {})
    return row, kw


def _classified(row, nd_cls, tm11_cls, lij, reasons, judgment_items, report, one_d,
                message=None):
    """Fill a batch row of a classified dam (both modes write the same values)."""
    row.update({"Status": "done", "ND_Class": nd_cls or "", "TM11_Class": tm11_cls or "",
                "Lives_In_Jeopardy": "{:.1f}".format(float(lij or 0.0)),
                "Governing_Reason": (list(reasons or []) or [""])[0][:250],
                "Judgment_Items": judgment_items, "Report": report or "",
                "Message": message if message is not None else
                (_ONE_D_NOTE if one_d else "")})
    return row


def _batch_sequential(folders, cfg_over, progress, log):
    from .pipeline import config, run_classification
    rows = []
    n = max(1, len(folders))
    for i, folder in enumerate(folders):
        row, kw = _prepare_dam(folder, cfg_over)
        rows.append(row)
        if kw is None:
            if log and row["Status"] == "failed":
                log("{}: FAILED - {}".format(row["Dam"], row["Message"]))
            continue
        name = row["Dam"]

        def sub(p, m="", i=i, name=name):
            if progress:
                progress(100.0 * (i + max(0.0, min(100.0, p)) / 100.0) / n,
                         "{} ({}/{}): {}".format(name, i + 1, n, m))
        try:
            res = run_classification(config(**kw), progress=sub, log=log)
            nd = res["nd"]
            _classified(row, nd["cls"], res["tm11"]["cls"], res["tm11"]["lij"],
                        nd["reasons"], len(nd.get("judgment") or []), res.get("report"),
                        kw["one_d"])
        except CancelledByUser:
            raise
        except Exception as e:
            row.update({"Status": "failed", "Message": str(e)[:250]})
            if log:
                log("{}: FAILED - {}".format(name, e))
    return rows


def _worker_outcome(row, ok, message, result_path, one_d, snap_mark):
    """Fill the batch row of a dam run by a worker - from the files the worker
    wrote (its result file, else the dam's new Last_Run_Snapshot.json), never
    from its text output."""
    from . import watch as WT
    from . import history as HIS
    if not ok:
        row.update({"Status": "failed", "Message": message or "the worker failed"})
        return row
    res = read_worker_result(result_path)
    if res is not None:
        return _classified(row, res.get("nd"), res.get("tm11"), res.get("lij"),
                           res.get("reasons"), res.get("judgment_items", ""),
                           res.get("report"), one_d)
    folder = row["Folder"]
    mark = WT.file_mark(WT.snapshot_path(folder))
    snap = HIS.read_snapshot(WT.qa_folder(folder)) if mark != snap_mark else None
    if snap is not None:          # a worker of an older plugin version: no result file
        return _classified(row, snap.get("nd"), snap.get("tm11"), snap.get("lij"), [], "",
                           "", one_d, "classes read from QA/Last_Run_Snapshot.json - the "
                           "worker wrote no result file (is this plugin version enabled in "
                           "the qgis_process profile?)")
    row.update({"Status": "failed", "Message": "the worker finished but wrote neither its "
                "result file nor a new QA/Last_Run_Snapshot.json"})
    return row


def _batch_parallel(preps, exe, workers, timeout_s, progress, log, popen, worker_log, what):
    """Rows (in folder order) of the prepared dams [(row, kw)] run as qgis_process
    workers (watch.run_workers). Raises watch.WorkerUnavailable when the first
    worker cannot be started (the caller falls back to the sequential mode)."""
    import shutil
    import tempfile
    from . import watch as WT
    from .pipeline import config
    jobs, extra = [], {}
    for row, kw in preps:
        if kw is None:
            continue
        folder = row["Folder"]
        jobs.append({"name": row["Dam"], "folder": folder,
                     "settings": worker_settings(config(**kw))})
        extra[folder] = (row, bool(kw.get("one_d")),
                         WT.file_mark(WT.snapshot_path(folder)))
    if log:
        for row, kw in preps:
            if kw is None and row["Status"] == "failed":
                log("{}: FAILED - {}".format(row["Dam"], row["Message"]))
    if jobs:
        work = tempfile.mkdtemp(prefix="nd_batch_")
        try:
            for i, job in enumerate(jobs):
                job["result_path"] = os.path.join(work, "result{:03d}.json".format(i + 1))
            results = WT.run_workers(
                jobs, exe, min(workers, len(jobs)), timeout_s, work, progress=progress,
                log=log, popen=popen, log_name=worker_log, what=what)
            for job in jobs:
                row, one_d, snap_mark = extra[job["folder"]]
                ok, message = results.get(job["folder"], (False, "not run"))
                _worker_outcome(row, ok, message, job["result_path"], one_d, snap_mark)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return [row for row, _kw in preps]


def write_batch_csv(parent, rows, mode=None):
    """<parent>/Batch_Hazard_Classification.csv (the same columns in every mode;
    the closing comment line names the run mode)."""
    import csv
    import time
    from .helpers import prepare_output_path
    path = prepare_output_path(os.path.join(parent, BATCH_CSV))
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=BATCH_FIELDS)
        w.writeheader()
        w.writerows(rows)
        fh.write("# {} - {}{}\n".format(PLUGIN_NAME, time.strftime("%Y-%m-%d %H:%M"),
                                        " - run mode: " + mode if mode else ""))
    return path


def batch_classify(parent, cfg_over=None, progress=None, log=None, workers=1,
                   timeout_s=WT_TIMEOUT_MIN * 60, info=None, qgis_process=None, popen=None,
                   label="Batch classification", worker_log=None):
    """Classify every dam folder directly under `parent` whose rasters are
    found (autodetect.find_rasters). One failure never stops the batch.
    Writes <parent>/Batch_Hazard_Classification.csv. Returns (rows, path).

    v2.9: `workers` > 1 (at most watch.MAX_WORKERS) classifies that many dams
    at a time, each in its own `qgis_process run ndhazard:classify` child (the
    creep watch's machinery, watch.run_workers): the settings go to the child
    in a CONFIG_JSON file, its result comes back in a result file (else the
    dam's new QA/Last_Run_Snapshot.json); `timeout_s` limits each dam (0 =
    none); a failed or timed-out dam is a "failed" row with the tail of the
    worker's error output (full output: <dam>/outputs/Batch_Worker_Log.txt);
    Cancel (CancelledByUser from `progress`) ends every child and writes no
    CSV. Falls back to one dam after another, with the reason, when
    qgis_process is not found, this is no real QGIS, `cfg_over` holds settings
    a child cannot receive (layers), or the first worker cannot start. `info`
    (a dict) is filled as by watch.plan_mode (mode, workers, fallback_reason,
    text ...). `qgis_process` skips the search for the executable, `popen`
    replaces subprocess.Popen (tests); `label` names the run in the log and
    `worker_log` the failed worker's log file."""
    from . import watch as WT
    folders = WT.dam_folders(parent)
    state = WT.plan_mode(workers, cfg_over, timeout_s, qgis_process, info)
    rows = None
    if state["mode"] == "parallel":
        if log:
            log(">>> {} mode: {}".format(label, state["text"]))
        preps = [_prepare_dam(f, cfg_over) for f in folders]
        try:
            rows = _batch_parallel(preps, state["executable"], state["workers"],
                                   state["timeout_s"], progress, log, popen,
                                   worker_log or WT.BATCH_WORKER_LOG, label.lower())
        except WT.WorkerUnavailable as e:
            WT.fall_back(state, "the worker process could not be started ({})".format(e))
    if rows is None:
        if log:
            log(">>> {} mode: {}".format(label, state["text"]))
        rows = _batch_sequential(folders, cfg_over, progress, log)
    return rows, write_batch_csv(parent, rows, state["text"])


class BatchAlgorithm(_Base):
    def name(self):
        return "batch"

    def displayName(self):
        return "Batch hazard classification (folder of dams)"

    def shortHelpString(self):
        return ("Runs the hazard classification for every dam folder directly inside the "
                "chosen folder. Each dam folder's rasters are found by name (RAS Mapper "
                "'Depth (Max)', 'Velocity (Max)', 'Arrival Time', 'D * V (Max)', time-step "
                "folders, no-breach rasters; DSS-WISE names). Results go to each dam's "
                "outputs folder; Batch_Hazard_Classification.csv lists the class of every "
                "dam. A dam that fails or has no depth raster is listed and skipped. "
                "WORKERS > 1 classifies that many dams at the same time, each in its own "
                "qgis_process child process (needs qgis_process and the plugin enabled in "
                "that QGIS profile - set ND_QGIS_PROCESS to its full path if it is not "
                "found; falls back to one dam after another when it is missing, and says "
                "so in MODE). TIMEOUT_MIN ends a worker that takes longer (that dam is "
                "listed as failed). WORKERS = 1 (default) runs the dams one after another "
                "in this session.")

    def flags(self):
        return super().flags() | NO_THREADING

    def initAlgorithm(self, config=None):
        P = self.addParameter
        P(QgsProcessingParameterFile("PARENT", "Folder containing the dam folders",
                                     behavior=FILE_FOLDER))
        P(QgsProcessingParameterBoolean("DOWNLOADS", "Use the internet downloads (NSI, ND GIS "
                                        "Hub, FEMA, roads, NID, lifelines, bridges)",
                                        defaultValue=True))
        P(QgsProcessingParameterBoolean("REPORT", "PDF report per dam", defaultValue=True))
        P(QgsProcessingParameterBoolean("EAP", "EAP outputs per dam", defaultValue=True))
        # v2.9
        P(QgsProcessingParameterNumber("WORKERS", "Parallel worker processes (1 = one dam "
                                       "after another in this session)", type=NUM_INTEGER,
                                       defaultValue=1, minValue=1, maxValue=16))
        P(QgsProcessingParameterNumber("TIMEOUT_MIN", "Worker processes: time limit per dam "
                                       "(minutes, 0 = none)", type=NUM_INTEGER,
                                       defaultValue=int(WT_TIMEOUT_MIN), minValue=0,
                                       optional=True))
        self.addOutput(QgsProcessingOutputFile("SUMMARY", "Batch summary (CSV)"))
        self.addOutput(QgsProcessingOutputString("COUNTS", "Dams classified / failed / "
                                                 "skipped"))
        self.addOutput(QgsProcessingOutputString("MODE", "How the batch ran (sequential / "
                                                 "parallel workers)"))

    def processAlgorithm(self, parameters, context, feedback):
        parent = self.parameterAsFile(parameters, "PARENT", context)
        if not parent or not os.path.isdir(parent):
            raise QgsProcessingException("Folder not found: {}".format(parent))
        dl = self.parameterAsBool(parameters, "DOWNLOADS", context)
        over = {"report_pdf": self.parameterAsBool(parameters, "REPORT", context),
                "eap": self.parameterAsBool(parameters, "EAP", context)}
        if not dl:
            over.update({k: False for k in _NO_DOWNLOADS})
        workers = self._int(parameters, "WORKERS", context, 1)
        timeout = self._int(parameters, "TIMEOUT_MIN", context, WT_TIMEOUT_MIN) * 60
        info = {}
        try:
            rows, path = batch_classify(parent, over, progress=_progress(feedback),
                                        log=lambda m: feedback.pushInfo(str(m)),
                                        workers=workers, timeout_s=timeout, info=info)
        except CancelledByUser as e:
            raise QgsProcessingException("Cancelled") from e
        except OSError as e:
            raise QgsProcessingException(str(e)) from e
        counts = {k: sum(1 for r in rows if r["Status"] == k)
                  for k in ("done", "failed", "skipped")}
        for r in rows:
            feedback.pushInfo("{}: {} {}".format(r["Dam"], r["Status"],
                                                 r["ND_Class"] or r["Message"]))
        return {"SUMMARY": path, "COUNTS": "{done} classified, {failed} failed, "
                "{skipped} skipped".format(**counts), "MODE": info.get("text", "")}


class WatchAlgorithm(_Base):
    """v2.7: hazard-creep watch (watch.py)."""

    def name(self):
        return "creepwatch"

    def displayName(self):
        return "Hazard creep watch (folder of dams, fresh data)"

    def shortHelpString(self):
        return ("Re-classifies every dam folder directly inside the chosen folder with its "
                "own flood rasters but FRESH data (NSI, ND GIS Hub address points, FEMA "
                "outlines, roads, ...), compares each result with that dam's previous run "
                "and writes Hazard_Creep_Watch.csv and Hazard_Creep_Watch_<date>.html in "
                "the chosen folder - dams whose class changed, or with newly flooded "
                "structures, roads or lifelines, first. It is a list of dams to look at, not "
                "a classification. To run it on a schedule: qgis_process run "
                "ndhazard:creepwatch --PARENT=<folder> (Windows Task Scheduler, cron). "
                "WORKERS > 1 classifies that many dams at the same time, each in its own "
                "qgis_process child process (QGIS stays usable; needs qgis_process - set "
                "ND_QGIS_PROCESS to its full path if it is not found; falls back to one dam "
                "after another when it is missing). WORKERS = 1 (default) runs the dams one "
                "after another in this session. Batch_Hazard_Classification.csv (the class "
                "of every dam) is written as well.")

    def flags(self):
        return super().flags() | NO_THREADING

    def initAlgorithm(self, config=None):
        P = self.addParameter
        P(QgsProcessingParameterFile("PARENT", "Folder containing the dam folders",
                                     behavior=FILE_FOLDER))
        P(QgsProcessingParameterBoolean("DOWNLOADS", "Use the internet downloads (or the "
                                        "offline data pack, if one is chosen)",
                                        defaultValue=True))
        P(QgsProcessingParameterBoolean("REPORT", "PDF report per dam", defaultValue=False))
        P(QgsProcessingParameterBoolean("EAP", "EAP outputs per dam", defaultValue=False))
        # v2.8
        P(QgsProcessingParameterNumber("WORKERS", "Parallel worker processes (1 = one dam "
                                       "after another in this session)", type=NUM_INTEGER,
                                       defaultValue=1, minValue=1, maxValue=16))
        P(QgsProcessingParameterNumber("TIMEOUT_MIN", "Worker processes: time limit per dam "
                                       "(minutes, 0 = none)", type=NUM_INTEGER,
                                       defaultValue=int(WT_TIMEOUT_MIN), minValue=0,
                                       optional=True))
        self.addOutput(QgsProcessingOutputFile("SUMMARY", "Watch list (CSV)"))
        self.addOutput(QgsProcessingOutputFile("HTML", "Watch list (HTML)"))
        self.addOutput(QgsProcessingOutputString("ALERTS", "What the watch found"))
        self.addOutput(QgsProcessingOutputString("MODE", "How the watch ran (sequential / "
                                                 "parallel workers)"))

    def processAlgorithm(self, parameters, context, feedback):
        from . import watch as WT
        parent = self.parameterAsFile(parameters, "PARENT", context)
        if not parent or not os.path.isdir(parent):
            raise QgsProcessingException("Folder not found: {}".format(parent))
        over = {"report_pdf": self.parameterAsBool(parameters, "REPORT", context),
                "eap": self.parameterAsBool(parameters, "EAP", context)}
        if not self.parameterAsBool(parameters, "DOWNLOADS", context):
            over.update({k: False for k in _NO_DOWNLOADS})
        workers = self._int(parameters, "WORKERS", context, 1)
        timeout = self._int(parameters, "TIMEOUT_MIN", context, WT_TIMEOUT_MIN) * 60
        info = {}
        try:
            rows, paths = WT.run_watch(parent, over, progress=_progress(feedback),
                                       log=lambda m: feedback.pushInfo(str(m)),
                                       workers=workers, timeout_s=timeout, info=info)
        except CancelledByUser as e:
            raise QgsProcessingException("Cancelled") from e
        except (ValueError, OSError) as e:
            raise QgsProcessingException(str(e)) from e
        for r in WT.sort_rows(rows):
            feedback.pushInfo("{}: {} {}".format(r["Alert"], r["Dam"], r["Message"]))
        return {"SUMMARY": paths["csv"], "HTML": paths["html"], "ALERTS": WT.alerts_text(rows),
                "MODE": info.get("text", "")}


class RasPlanAlgorithm(_Base):
    """v2.5: HEC-RAS 6.x plan results (.p##.hdf) to depth / velocity / arrival
    / water surface / time-of-peak rasters (rasimport.py)."""

    def name(self):
        return "hecrasplan"

    def displayName(self):
        return "Import HEC-RAS plan results (2D, .hdf)"

    def shortHelpString(self):
        return ("Reads the 2D flow area results of a HEC-RAS 6.x plan (<project>.p##.hdf) "
                "and writes Depth_ft, Velocity_ftps, Arrival_hr, WSE_ft and TimeOfPeak_hr "
                "GeoTIFFs. With the terrain raster, depth is water surface minus terrain on "
                "the terrain grid; without it, water surface minus each cell's lowest "
                "point. Arrival / time of peak are hours after the breach start (hours "
                "after simulation start). Check the first results against RAS Mapper.")

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFile("PLAN", "Plan results (.hdf)",
                                                     behavior=FILE_FILE, extension="hdf"))
        self.addParameter(QgsProcessingParameterRasterLayer("TERRAIN", "Terrain raster",
                                                            optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            "BREACH_HR", "Breach start (h after simulation start)", type=NUM_DOUBLE,
            defaultValue=0.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterNumber(
            "ARRIVAL_FT", "Arrival depth (ft)", type=NUM_DOUBLE, defaultValue=0.1,
            minValue=0.01))
        self.addParameter(QgsProcessingParameterNumber(
            "CELL_FT", "Pixel size without terrain (ft, 0 = automatic)", type=NUM_DOUBLE,
            defaultValue=0.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterFolderDestination("OUTPUT", "Output folder"))
        for key, label in (("DEPTH", "Max depth (ft)"), ("VELOCITY", "Max velocity (ft/s)"),
                           ("ARRIVAL", "Arrival time (h)"), ("WSE", "Max water surface (ft)"),
                           ("PEAK", "Time of peak (h)")):
            self.addOutput(QgsProcessingOutputRasterLayer(key, label))

    def processAlgorithm(self, parameters, context, feedback):
        from . import rasimport as RI
        plan = self.parameterAsFile(parameters, "PLAN", context)
        if not plan or not os.path.isfile(plan):
            raise QgsProcessingException("Plan file not found: {}".format(plan))
        dem = self._raster_path(parameters, "TERRAIN", context)
        out = self.parameterAsString(parameters, "OUTPUT", context)
        try:
            res = RI.import_plan(
                plan, out, dem_path=dem or None,
                cell_ft=self.parameterAsDouble(parameters, "CELL_FT", context),
                breach_hr=self.parameterAsDouble(parameters, "BREACH_HR", context),
                arrival_ft=self.parameterAsDouble(parameters, "ARRIVAL_FT", context),
                progress=_progress(feedback))
        except CancelledByUser as e:
            raise QgsProcessingException("Cancelled") from e
        except Exception as e:
            _report_traceback(feedback)
            raise QgsProcessingException(str(e) or repr(e)) from e
        for line in res["lines"] + ["NOTE: " + n for n in res["notes"]]:
            feedback.pushInfo(line)
        p = res["paths"]
        return {"DEPTH": p.get("depth"), "VELOCITY": p.get("vel"), "ARRIVAL": p.get("arr"),
                "WSE": p.get("wse"), "PEAK": p.get("peak")}


class SeriesAlgorithm(_Base):
    """v2.8: dams-in-series screen over a folder of studied dams (series.py)."""

    def name(self):
        return "damsinseries"

    def displayName(self):
        return "Dams in series screen (folder of dams)"

    def shortHelpString(self):
        return ("Screens the dam folders directly inside the chosen folder (each with its own "
                "dam-break rasters and outputs/QA/Last_Run_Snapshot.json): for every pair, is "
                "the downstream dam inside the upstream dam's breach flood, how deep and when "
                "does the water arrive there, and is the downstream dam's own class higher. "
                "Writes Dams_In_Series_Screen.csv / .html in the chosen folder and in each dam's QA "
                "folder. A SCREENING TABLE only - it reads each upstream dam's own breach "
                "flood at the downstream dam's point and does not compute a combined "
                "breach. The depth threshold and search distance are the engineer's own "
                "screening values. Dam locations come from dam_location.json / .csv in the "
                "dam folder, or from an NID layer given here (matched by NID id or name).")

    def flags(self):
        return super().flags() | NO_THREADING

    def initAlgorithm(self, config=None):
        from . import series as SER
        P = self.addParameter
        P(QgsProcessingParameterFile("PARENT", "Folder containing the dam folders",
                                     behavior=FILE_FOLDER))
        P(QgsProcessingParameterNumber("DEPTH_FT", "Cascade candidate when the depth at the "
                                       "downstream dam is above (ft)", type=NUM_DOUBLE,
                                       defaultValue=float(SER.DEFAULT_DEPTH_THRESHOLD_FT),
                                       minValue=0.0))
        P(QgsProcessingParameterNumber("TOLERANCE_FT", "Search the nearest wet cell within "
                                       "(ft) of the downstream dam's point", type=NUM_DOUBLE,
                                       defaultValue=float(SER.DEFAULT_TOLERANCE_FT),
                                       minValue=0.0))
        P(QgsProcessingParameterVectorLayer("NID", "National Inventory of Dams layer (to "
                                            "locate dams without a location file)",
                                            optional=True))
        self.addOutput(QgsProcessingOutputFile("CSV", "Dams_In_Series_Screen.csv"))
        self.addOutput(QgsProcessingOutputFile("HTML", "Dams_In_Series_Screen.html"))
        self.addOutput(QgsProcessingOutputString("SUMMARY", "What the screen found"))

    def processAlgorithm(self, parameters, context, feedback):
        from . import series as SER
        parent = self.parameterAsFile(parameters, "PARENT", context)
        if not parent or not os.path.isdir(parent):
            raise QgsProcessingException("Folder not found: {}".format(parent))
        nid = self.parameterAsVectorLayer(parameters, "NID", context)

        def num(name, default):
            try:
                return float(self.parameterAsDouble(parameters, name, context))
            except AttributeError:
                pass
            try:
                return float(parameters.get(name, default))
            except (TypeError, ValueError):
                return float(default)
        try:
            res = SER.screen_folder(
                parent, nid_layer=nid, progress=_progress(feedback),
                depth_threshold_ft=num("DEPTH_FT", SER.DEFAULT_DEPTH_THRESHOLD_FT),
                tolerance_ft=num("TOLERANCE_FT", SER.DEFAULT_TOLERANCE_FT))
            paths = SER.write_reports(res, parent)
        except CancelledByUser as e:
            raise QgsProcessingException("Cancelled") from e
        except (ValueError, OSError) as e:
            raise QgsProcessingException(str(e)) from e
        try:
            SER.write_qa(res)                 # the per-dam copies are optional
        except (ValueError, OSError) as e:
            feedback.pushInfo("WARNING: the per-dam files could not all be written ({})"
                              .format(e))
        lines = SER.summary_lines(res)
        for line in lines:
            feedback.pushInfo(str(line))
        return {"CSV": paths["csv"], "HTML": paths["html"], "SUMMARY": "\n".join(lines)}


class PortfolioAlgorithm(_Base):
    """v2.9: one-page dashboard over a folder of classified dams (portfolio.py)."""

    def name(self):
        return "portfolio"

    def displayName(self):
        return "Dam portfolio dashboard (folder of dams)"

    def shortHelpString(self):
        return ("Reads the outputs of the dam folders directly inside the chosen folder "
                "(their summary, run log and QA snapshot files - nothing is re-run) and "
                "writes Dam_Portfolio.html and .csv: class, lives in jeopardy, "
                "open review items and data vintage per dam. A dam whose files are "
                "missing is a row of '-'. v2.10: also each dam's dam.json, the ND Dam Breach "
                "Screening results (summary.csv / tool-8 GeoPackage found in or directly "
                "inside the folder, or the Screening folder given here) and the ND Dam Breach "
                "Runner files, and a Next action column (screen, model, classify, review, "
                "refresh, none); dams needing an action come first. The Screening and Runner "
                "classes are previews; this plugin's class is the classification.")

    def flags(self):
        return super().flags() | NO_THREADING

    def initAlgorithm(self, config=None):
        P = self.addParameter
        P(QgsProcessingParameterFile("PARENT_FOLDER", "Folder containing the dam folders",
                                     behavior=FILE_FOLDER))
        P(QgsProcessingParameterString("OUTPUT_HTML", "Dashboard file (.html; empty = "
                                       "Dam_Portfolio.html in that folder)",
                                       defaultValue="", optional=True))
        P(QgsProcessingParameterFile("SCREENING", "ND Dam Breach Screening report folder "
                                     "(empty: looked for in the folder of dams)",
                                     behavior=FILE_FOLDER, optional=True))
        self.addOutput(QgsProcessingOutputFile("HTML", "Dam_Portfolio.html"))
        self.addOutput(QgsProcessingOutputFile("CSV", "Dam_Portfolio.csv"))
        self.addOutput(QgsProcessingOutputString("SUMMARY", "Dams in the dashboard"))

    def processAlgorithm(self, parameters, context, feedback):
        from . import portfolio as PF
        parent = self.parameterAsFile(parameters, "PARENT_FOLDER", context)
        if not parent or not os.path.isdir(parent):
            raise QgsProcessingException("Folder not found: {}".format(parent))
        out = (parameters.get("OUTPUT_HTML") or "").strip() or None
        try:
            scr = self.parameterAsFile(parameters, "SCREENING", context) or None
            html_path, csv_path, rows = PF.build(parent, out, screening=scr)
        except (ValueError, OSError) as e:
            raise QgsProcessingException(str(e)) from e
        feedback.pushInfo("{} dam(s) in {}".format(len(rows), html_path))
        return {"HTML": html_path, "CSV": csv_path, "SUMMARY": "{} dam(s)".format(len(rows))}


class ReproduceAlgorithm(_Base):
    """v2.9: re-run a submittal / reproduce package and compare (reproduce.py)."""

    def name(self):
        return "reproduce"

    def displayName(self):
        return "Reproduce a past run from its package"

    def shortHelpString(self):
        return ("Re-runs the classification recorded in a submittal / reproduce package "
                "(folder or .zip; Run_Settings.json and the packaged inputs) in a NEW "
                "work folder and compares the result with the package's. Internet "
                "downloads are off; the flood rasters are taken from the package or from "
                "the rasters folder given here. Writes Reproduction_Check.csv / .txt in "
                "the work folder. Says IDENTICAL only when the compared values match; a "
                "difference may come from a changed plugin version or a missing input.")

    def flags(self):
        return super().flags() | NO_THREADING

    def initAlgorithm(self, config=None):
        P = self.addParameter
        P(QgsProcessingParameterFile("PACKAGE", "Package (folder or .zip)",
                                     behavior=FILE_FOLDER))
        P(QgsProcessingParameterFolderDestination("WORK_FOLDER", "Work folder (new or empty)"))
        P(QgsProcessingParameterFile("RASTERS_FOLDER", "Folder with the flood rasters (if "
                                     "they are not in the package)", behavior=FILE_FOLDER,
                                     optional=True))
        self.addOutput(QgsProcessingOutputString("STATUS", "IDENTICAL / DIFFERENT / "
                                                 "NOT COMPARED / FAILED"))
        self.addOutput(QgsProcessingOutputFile("CHECK_CSV", "Reproduction_Check.csv"))
        self.addOutput(QgsProcessingOutputFile("CHECK_TXT", "Reproduction_Check.txt"))

    def processAlgorithm(self, parameters, context, feedback):
        from . import reproduce as RP
        pkg = self.parameterAsFile(parameters, "PACKAGE", context)
        work = self.parameterAsString(parameters, "WORK_FOLDER", context)
        ras = self.parameterAsFile(parameters, "RASTERS_FOLDER", context) or None
        if not pkg or not os.path.exists(pkg):
            raise QgsProcessingException("Package not found: {}".format(pkg))
        if not work:
            raise QgsProcessingException("Choose a work folder")
        try:
            res = RP.reproduce(pkg, work, progress=_progress(feedback),
                               log=lambda m: feedback.pushInfo(str(m)), rasters_folder=ras)
        except CancelledByUser as e:
            raise QgsProcessingException("Cancelled") from e
        except (ValueError, OSError) as e:
            raise QgsProcessingException(str(e)) from e
        for line in res.get("lines") or []:
            feedback.pushInfo(str(line))
        return {"STATUS": res.get("status", ""), "CHECK_CSV": res.get("csv", ""),
                "CHECK_TXT": res.get("text", "")}


class FormFillAlgorithm(_Base):
    """v2.9: fill a form template's {{KEY}} placeholders from a dam's results."""

    def name(self):
        return "fillform"

    def displayName(self):
        return "Fill a form template from a dam's results"

    def shortHelpString(self):
        return ("Fills the {{KEY}} placeholders of a template (.docx, .txt, .md, .html or "
                ".csv) with values read from the dam's outputs folder (Form_Values.csv "
                "lists every KEY). It never guesses: a placeholder that has no KEY, or no "
                "recorded value, is left as it is and reported. The plugin does not "
                "contain the State's form - use your own template.")

    def flags(self):
        return super().flags() | NO_THREADING

    def initAlgorithm(self, config=None):
        P = self.addParameter
        P(QgsProcessingParameterFile("OUTPUT_FOLDER", "The dam's outputs folder (or the dam "
                                     "folder)", behavior=FILE_FOLDER))
        P(QgsProcessingParameterFile("TEMPLATE", "Template (.docx, .txt, .md, .html, .csv)"))
        P(QgsProcessingParameterString("OUT_FILE", "Filled form (file; empty = next to the "
                                       "template, with _filled added)", defaultValue="",
                                       optional=True))
        self.addOutput(QgsProcessingOutputFile("FILLED", "Filled form"))
        self.addOutput(QgsProcessingOutputString("SUMMARY", "Placeholders not filled"))

    def processAlgorithm(self, parameters, context, feedback):
        from . import formfill as FF
        from . import portfolio as PF
        folder = self.parameterAsFile(parameters, "OUTPUT_FOLDER", context)
        tmpl = self.parameterAsFile(parameters, "TEMPLATE", context)
        if not folder or not os.path.isdir(folder):
            raise QgsProcessingException("Folder not found: {}".format(folder))
        if not tmpl or not os.path.isfile(tmpl):
            raise QgsProcessingException("Template not found: {}".format(tmpl))
        sub = PF.output_folder_of(folder)
        if os.path.isdir(sub):
            folder = sub
        out = (parameters.get("OUT_FILE") or "").strip()
        if not out:
            base, ext = os.path.splitext(tmpl)
            out = base + "_filled" + ext
        try:
            vals = FF.available_values(folder)
            path, unknown, missing = FF.fill_template(tmpl, vals, out)
        except (ValueError, OSError) as e:
            raise QgsProcessingException(str(e)) from e
        if unknown:
            feedback.pushInfo("No such KEY (left as is): {}".format(", ".join(unknown)))
        if missing:
            feedback.pushInfo("No value recorded (left as is): {}".format(", ".join(missing)))
        return {"FILLED": path, "SUMMARY": "{} unknown, {} without value".format(
            len(unknown), len(missing))}


SUITE_KEY = "ndhazard"                    # the suite settings file key (damsuite.versions)
SETTINGS_TABS = ("Classification", "Models", "Review & QA", "Dam suite", "Other")
_TOOL_TAB = {"acerzones": "Classification", "classify": "Classification", "batch": "Classification",
             "fillform": "Classification", "dsswise": "Models", "hecrasplan": "Models", "damsinseries": "Models",
             "portfolio": "Models", "reproduce": "Models", "creepwatch": "Review & QA", "ensemble": "Review & QA",
             "modelgate": "Review & QA", "quickcreep": "Review & QA", "reviewpackage": "Review & QA",
             "reviewsummary": "Review & QA", "markreviewed": "Review & QA", "runnerculverts": "Dam suite",
             "suiteexport": "Dam suite", "chartsignoff": "Dam suite", "rulecompare": "Dam suite",
             "getdamdata": "Dam suite"}


def settings_group(tool):
    """(settings tab, order) of a tool for the suite settings grid."""
    tab = _TOOL_TAB.get(str(tool or "").lower(), "Other")
    return tab, list(_TOOL_TAB).index(str(tool or "").lower()) if str(tool or "").lower() in _TOOL_TAB else 99


class Provider(QgsProcessingProvider):
    def id(self):
        return PROVIDER_ID

    def name(self):
        return "ND Dam Hazard"

    def longName(self):
        return PLUGIN_NAME

    def loadAlgorithms(self):
        for cls in self.algorithm_classes():
            self.addAlgorithm(cls())

    @staticmethod
    def original_classes():
        """The tool classes as written (their defaults are the settings grid's starting values)."""
        # v2.10 - the dam suite (imported here: processing_qa2100 / processing_review use
        # this module's base class, so they cannot be imported at the top)
        from .processing_qa2100 import ALGORITHMS as QA_ALGORITHMS
        from .processing_review import algorithms as review_algorithms
        from .suite_algorithms import RunnerCulvertsAlgorithm, SuiteExportAlgorithm
        from .chartsign_alg import ChartSignOffAlgorithm
        from .rulecompare_alg import RuleCompareAlgorithm
        from .markreviewed_alg import MarkReviewedAlgorithm          # v2.12
        classes = [AcerZonesAlgorithm, ClassifyAlgorithm, BatchAlgorithm, DssWiseAlgorithm, RasPlanAlgorithm,
                   WatchAlgorithm, SeriesAlgorithm, PortfolioAlgorithm, ReproduceAlgorithm, FormFillAlgorithm]
        classes += list(QA_ALGORITHMS)
        classes += [type(a) for a in review_algorithms()]
        classes += [RunnerCulvertsAlgorithm, SuiteExportAlgorithm, ChartSignOffAlgorithm, RuleCompareAlgorithm,
                    MarkReviewedAlgorithm]
        # 2.17.0: "Get data for this dam" in the dam-suite group (suite_algorithms.SUITE_GROUP)
        try:
            from . import suite_data_tools as SDT
            from .suite_algorithms import SUITE_GROUP
            classes.append(SDT.get_data_algorithm(_Base, SUITE_GROUP[0], SUITE_GROUP[1]))
        except Exception as e:                                   # noqa - never lose the other tools
            try:
                from qgis.core import QgsMessageLog
                QgsMessageLog.logMessage("Get data for this dam not available: {}".format(e),
                                         PLUGIN_NAME)
            except Exception:
                pass
        return classes

    @classmethod
    def algorithm_classes(cls):
        """v2.13: every tool takes its parameter defaults from the suite settings file
        (suite_settings_tools.settings_aware); the grid groups them by the tool's own group."""
        from . import suite_settings_tools as ST
        return [ST.settings_aware(c, SUITE_KEY, settings_group) for c in cls.original_classes()]
