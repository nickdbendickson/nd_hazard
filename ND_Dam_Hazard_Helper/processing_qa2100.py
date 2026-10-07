# -*- coding: utf-8 -*-
"""v2.10: Processing algorithms of the 2.10 QA tools (for the provider's loadAlgorithms):

  ndhazard:ensemble    class probability from an ensemble of breach runs (ensemble.py)
  ndhazard:modelgate   model gate - checks of the model results before a classification
                       (modelgate.py)
  ndhazard:quickcreep  statewide quick creep screen with a NEW structures layer, without
                       re-running any model or classification (quickcreep.py)

ALGORITHMS lists the classes; processing_provider.Provider.loadAlgorithms adds one of each.
Nothing here has been run in a real QGIS.
"""

import os

from qgis.core import (
    QgsProcessingException, QgsProcessingParameterFile, QgsProcessingParameterBoolean,
    QgsProcessingParameterNumber, QgsProcessingParameterVectorLayer, QgsProcessingOutputFile,
    QgsProcessingOutputString, QgsProcessingOutputFolder,
)

from .helpers import CancelledByUser
from .processing_provider import (
    _Base, _progress, _report_traceback, FILE_FOLDER, FILE_FILE, NUM_INTEGER, NUM_DOUBLE,
    NO_THREADING,
)


def _opt_float(alg, parameters, name, context, default=None):
    """An optional number parameter: None (or `default`) when it is not given."""
    v = parameters.get(name) if hasattr(parameters, "get") else None
    if v is None or v == "":
        return default
    try:
        return float(alg.parameterAsDouble(parameters, name, context))
    except (TypeError, ValueError, AttributeError):
        try:
            return float(v)
        except (TypeError, ValueError):
            return default


# ================================================================ ensemble ==
class EnsembleAlgorithm(_Base):
    """v2.10: class probability over an ensemble of breach runs (ensemble.py)."""

    def name(self):
        return "ensemble"

    def displayName(self):
        return "Hazard class probability from an ensemble of breach runs"

    def shortHelpString(self):
        return ("Classifies every run of an ensemble (e.g. the ND Dam Breach Runner's "
                "<dam>/Probabilistic/Runs/run_NNN folders, or any folder of sub-folders with "
                "depth / velocity / arrival rasters) with this plugin's pipeline and the SAME "
                "settings and data as the dam's own last run (outputs/QA/Run_Settings.json; "
                "downloads are re-used, not fetched per run), and writes "
                "outputs/QA/Class_Probability.csv / .html: P(ND High / Medium / Low), "
                "P(TM 11 classes), weighted when every run.json has a weight, with the run "
                "IDs of each class, and per structure the probability of the probable loss "
                "of life zone. The probabilities are only as good as the ensemble's "
                "parameter distributions - the runner's defaults are placeholders. Each "
                "run's outputs: outputs/QA/Ensemble/<run>. A finished run is not "
                "classified again when its rasters and settings are unchanged.")

    def flags(self):
        return super().flags() | NO_THREADING

    def initAlgorithm(self, config=None):
        from . import ensemble as EN
        P = self.addParameter
        P(QgsProcessingParameterFile("ENSEMBLE_FOLDER", "Ensemble folder (run sub-folders; "
                                     "a dam folder or its Probabilistic folder also works)",
                                     behavior=FILE_FOLDER))
        P(QgsProcessingParameterFile("DAM_FOLDER", "Dam folder (its settings, data and "
                                     "outputs/QA are used)", behavior=FILE_FOLDER))
        P(QgsProcessingParameterFile("CONFIG_JSON", "Advanced: JSON file with pipeline "
                                     "settings (as ndhazard:classify)", behavior=FILE_FILE,
                                     extension="json", optional=True))
        P(QgsProcessingParameterNumber("MAX_RUNS", "Largest number of runs to classify (the "
                                       "first ones in name order)", type=NUM_INTEGER,
                                       defaultValue=EN.DEFAULT_MAX_RUNS, minValue=1,
                                       maxValue=EN.MAX_RUNS_LIMIT))
        P(QgsProcessingParameterBoolean("DOWNLOADS", "Allow downloads (off: only the data the "
                                        "dam's own run read)", defaultValue=False))
        P(QgsProcessingParameterBoolean("RESUME", "Keep runs already classified with the "
                                        "same rasters and settings", defaultValue=True))
        P(QgsProcessingParameterBoolean("GATE", "Model gate for every run (status in the "
                                        "run table)", defaultValue=True))
        self.addOutput(QgsProcessingOutputFile("CSV", "Class_Probability.csv"))
        self.addOutput(QgsProcessingOutputFile("HTML", "Class_Probability.html"))
        self.addOutput(QgsProcessingOutputFile("RUNS_CSV", "Class_Probability_Runs.csv"))
        self.addOutput(QgsProcessingOutputFolder("OUTPUT_FOLDER", "Run outputs"))
        self.addOutput(QgsProcessingOutputString("SUMMARY", "Class probability (one line)"))

    def processAlgorithm(self, parameters, context, feedback):
        from . import ensemble as EN
        ens = self.parameterAsFile(parameters, "ENSEMBLE_FOLDER", context)
        dam = self.parameterAsFile(parameters, "DAM_FOLDER", context)
        for label, f in (("Ensemble folder", ens), ("Dam folder", dam)):
            if not f or not os.path.isdir(f):
                raise QgsProcessingException("{} not found: {}".format(label, f))
        cj = self.parameterAsFile(parameters, "CONFIG_JSON", context) or None
        try:
            res = EN.run_ensemble(
                ens, dam, config_json=cj,
                max_runs=self._int(parameters, "MAX_RUNS", context, EN.DEFAULT_MAX_RUNS),
                downloads=self.parameterAsBool(parameters, "DOWNLOADS", context),
                resume=self.parameterAsBool(parameters, "RESUME", context),
                gate=self.parameterAsBool(parameters, "GATE", context),
                progress=_progress(feedback), log=lambda m: feedback.pushInfo(str(m)))
        except CancelledByUser as e:
            raise QgsProcessingException("Cancelled") from e
        except (ValueError, OSError, RuntimeError) as e:
            raise QgsProcessingException(str(e)) from e
        except Exception as e:                        # never an unhandled crash
            _report_traceback(feedback)
            raise QgsProcessingException("Unexpected error: {!r}".format(e)) from e
        feedback.pushInfo(res["summary"])
        return {"CSV": res["csv"], "HTML": res["html"], "RUNS_CSV": res["runs_csv"],
                "OUTPUT_FOLDER": res["folder"], "SUMMARY": res["summary"]}


# ================================================================ model gate ==
class ModelGateAlgorithm(_Base):
    """v2.10: checks of the model results before the classification (modelgate.py)."""

    def name(self):
        return "modelgate"

    def displayName(self):
        return "Model gate (checks of the breach model results)"

    def shortHelpString(self):
        return ("Checks the flood rasters of a dam folder BEFORE they are classified: flood at "
                "the model edge, no-data holes inside the wet area, mass balance and warnings "
                "of an ND Dam Breach Runner run report, velocities near the breach (dam point "
                "from dam.json), units (depth that looks like elevation, arrival in minutes "
                "...) and the runner's run manifest against the rasters. Each finding is "
                "PASS / WARN / FAIL (SKIP: could not be checked) with the reason, in "
                "outputs/QA/Model_Gate.csv. Every threshold is a screening value you choose - "
                "the defaults are starting values, not requirements. 'Stop on FAIL' makes a "
                "FAIL an error of this algorithm.")

    def flags(self):
        return super().flags() | NO_THREADING

    def initAlgorithm(self, config=None):
        from . import modelgate as MG
        d = MG.DEFAULTS
        P = self.addParameter
        P(QgsProcessingParameterFile("DAM_FOLDER", "Dam folder (rasters found by name)",
                                     behavior=FILE_FOLDER))
        P(QgsProcessingParameterVectorLayer("MODEL_BOUNDARY", "Model boundary polygon (2D "
                                            "flow area; empty: the raster edge)",
                                            optional=True))
        P(QgsProcessingParameterNumber("EDGE_WARN_FT", "Edge: WARN above this (added) depth, "
                                       "ft", type=NUM_DOUBLE, defaultValue=d["edge_warn_ft"],
                                       minValue=0))
        P(QgsProcessingParameterNumber("EDGE_FAIL_FT", "Edge: FAIL above this (added) depth, "
                                       "ft (empty: never)", type=NUM_DOUBLE, optional=True,
                                       minValue=0))
        P(QgsProcessingParameterNumber("EDGE_INCR_FT", "Edge, with a baseline: breach adds at "
                                       "least (ft)", type=NUM_DOUBLE,
                                       defaultValue=d["edge_incr_ft"], minValue=0))
        P(QgsProcessingParameterNumber("HOLE_GAP_CELLS", "Holes: widest no-data gap (cells)",
                                       type=NUM_INTEGER, defaultValue=d["hole_max_gap_cells"],
                                       minValue=1, maxValue=50))
        P(QgsProcessingParameterNumber("HOLE_FAIL_FRACTION", "Holes: FAIL above this share "
                                       "of the wet cells (empty: never)", type=NUM_DOUBLE,
                                       defaultValue=d["hole_fail_fraction"], optional=True,
                                       minValue=0))
        P(QgsProcessingParameterNumber("MASS_WARN_PCT", "Mass balance: WARN above (% volume "
                                       "error)", type=NUM_DOUBLE,
                                       defaultValue=d["mass_warn_pct"], minValue=0))
        P(QgsProcessingParameterNumber("MASS_FAIL_PCT", "Mass balance: FAIL above (% volume "
                                       "error; empty: never)", type=NUM_DOUBLE,
                                       defaultValue=d["mass_fail_pct"], optional=True,
                                       minValue=0))
        P(QgsProcessingParameterNumber("BREACH_RADIUS_FT", "Near the breach: within (ft) of "
                                       "the dam point", type=NUM_DOUBLE,
                                       defaultValue=d["breach_radius_ft"], minValue=0))
        P(QgsProcessingParameterNumber("BREACH_VEL_FPS", "Near the breach: WARN above (ft/s)",
                                       type=NUM_DOUBLE, defaultValue=d["breach_vel_warn_fps"],
                                       minValue=0))
        P(QgsProcessingParameterBoolean("STOP_ON_FAIL", "Stop on FAIL (the algorithm ends "
                                        "with an error)", defaultValue=False))
        self.addOutput(QgsProcessingOutputFile("CSV", "Model_Gate.csv"))
        self.addOutput(QgsProcessingOutputString("STATUS", "PASS / WARN / FAIL / SKIP"))

    def processAlgorithm(self, parameters, context, feedback):
        from . import modelgate as MG
        dam = self.parameterAsFile(parameters, "DAM_FOLDER", context)
        if not dam or not os.path.isdir(dam):
            raise QgsProcessingException("Dam folder not found: {}".format(dam))
        d = MG.DEFAULTS
        p = {"edge_warn_ft": _opt_float(self, parameters, "EDGE_WARN_FT", context,
                                        d["edge_warn_ft"]),
             "edge_fail_ft": _opt_float(self, parameters, "EDGE_FAIL_FT", context, None),
             "edge_incr_ft": _opt_float(self, parameters, "EDGE_INCR_FT", context,
                                        d["edge_incr_ft"]),
             "hole_max_gap_cells": int(_opt_float(self, parameters, "HOLE_GAP_CELLS", context,
                                                  d["hole_max_gap_cells"])),
             "hole_fail_fraction": _opt_float(self, parameters, "HOLE_FAIL_FRACTION", context,
                                              d["hole_fail_fraction"]
                                              if "HOLE_FAIL_FRACTION" not in parameters
                                              else None),
             "mass_warn_pct": _opt_float(self, parameters, "MASS_WARN_PCT", context,
                                         d["mass_warn_pct"]),
             "mass_fail_pct": _opt_float(self, parameters, "MASS_FAIL_PCT", context,
                                         d["mass_fail_pct"] if "MASS_FAIL_PCT" not in parameters
                                         else None),
             "breach_radius_ft": _opt_float(self, parameters, "BREACH_RADIUS_FT", context,
                                            d["breach_radius_ft"]),
             "breach_vel_warn_fps": _opt_float(self, parameters, "BREACH_VEL_FPS", context,
                                               d["breach_vel_warn_fps"]),
             "stop_on_fail": self.parameterAsBool(parameters, "STOP_ON_FAIL", context)}
        from .autodetect import find_rasters
        from .constants import OUTPUT_FOLDER_NAME
        found = find_rasters(dam)
        target = {k: found.get(k) for k in ("depth", "vel", "arr", "dv", "base_depth",
                                             "base_vel", "base_arr")}
        target.update({"dam_folder": dam, "output_folder": os.path.join(dam, OUTPUT_FOLDER_NAME),
                       "model_boundary_layer": self.parameterAsVectorLayer(
                           parameters, "MODEL_BOUNDARY", context)})
        try:
            res = MG.gate(target, p, write=True, progress=_progress(feedback),
                          log=lambda m: feedback.pushInfo(str(m)))
        except CancelledByUser as e:
            raise QgsProcessingException("Cancelled") from e
        if res["stop"]:
            raise QgsProcessingException("Model gate FAILED (stop on FAIL): " + "; ".join(
                r["Reason"] for r in res["rows"] if r["Status"] == MG.FAIL)[:1000])
        return {"CSV": res["csv"], "STATUS": res["status"]}


# ================================================================ quick creep ==
class QuickCreepAlgorithm(_Base):
    """v2.10: statewide quick creep screen with a new structures layer (quickcreep.py)."""

    def name(self):
        return "quickcreep"

    def displayName(self):
        return "Quick creep screen: new structures layer against every dam's flood"

    def shortHelpString(self):
        return ("For every dam folder directly inside the chosen folder: samples the NEW "
                "structures layer (e.g. a new FEMA USA Structures or NSI release; points or "
                "polygons) on the depth raster the dam's last classification used and "
                "compares the wet structures with the structures of that run (matched by "
                "location). Lists NEW structures in the flood area and structures that are "
                "gone, with a 're-run recommended' flag per dam, in Quick_Creep.csv / .html "
                "in the chosen folder. It does NOT re-classify anything and runs no model - "
                "it tells which dams to re-run. A screen: a point reads the cell it lies in, "
                "a polygon the largest value of the cells whose centre is inside it (the "
                "classification reads the outline plus a 5-ft ring).")

    def flags(self):
        return super().flags() | NO_THREADING

    def initAlgorithm(self, config=None):
        from . import quickcreep as QC
        d = QC.DEFAULTS
        P = self.addParameter
        P(QgsProcessingParameterFile("PARENT", "Folder containing the dam folders",
                                     behavior=FILE_FOLDER))
        P(QgsProcessingParameterVectorLayer("STRUCTURES", "NEW structures layer (points or "
                                            "polygons)"))
        P(QgsProcessingParameterVectorLayer("PREVIOUS", "Previous release of that layer "
                                            "(optional)", optional=True))
        P(QgsProcessingParameterNumber("WET_FT", "Wet: depth above (ft)", type=NUM_DOUBLE,
                                       defaultValue=d["wet_ft"], minValue=0))
        P(QgsProcessingParameterNumber("MATCH_FT", "Same structure: within (ft)",
                                       type=NUM_DOUBLE, defaultValue=d["match_ft"], minValue=0))
        P(QgsProcessingParameterNumber("MIN_NEW", "Re-run recommended from this many new "
                                       "structures in the flood", type=NUM_INTEGER,
                                       defaultValue=d["min_new"], minValue=1))
        self.addOutput(QgsProcessingOutputFile("CSV", "Quick_Creep.csv"))
        self.addOutput(QgsProcessingOutputFile("HTML", "Quick_Creep.html"))
        self.addOutput(QgsProcessingOutputFile("STRUCTURES_CSV", "Quick_Creep_Structures.csv"))
        self.addOutput(QgsProcessingOutputString("SUMMARY", "What the screen found"))

    def processAlgorithm(self, parameters, context, feedback):
        from . import quickcreep as QC
        parent = self.parameterAsFile(parameters, "PARENT", context)
        if not parent or not os.path.isdir(parent):
            raise QgsProcessingException("Folder not found: {}".format(parent))
        new = self.parameterAsVectorLayer(parameters, "STRUCTURES", context)
        if new is None or not new.isValid():
            raise QgsProcessingException("The new structures layer is not valid")
        prev = self.parameterAsVectorLayer(parameters, "PREVIOUS", context)
        d = QC.DEFAULTS
        p = {"wet_ft": _opt_float(self, parameters, "WET_FT", context, d["wet_ft"]),
             "match_ft": _opt_float(self, parameters, "MATCH_FT", context, d["match_ft"]),
             "min_new": self._int(parameters, "MIN_NEW", context, d["min_new"])}
        try:
            res = QC.run_quick_creep(parent, new, prev, p, progress=_progress(feedback),
                                     log=lambda m: feedback.pushInfo(str(m)))
        except CancelledByUser as e:
            raise QgsProcessingException("Cancelled") from e
        except (ValueError, OSError) as e:
            raise QgsProcessingException(str(e)) from e
        feedback.pushInfo(res["summary"])
        return {"CSV": res["csv"], "HTML": res["html"], "STRUCTURES_CSV": res["structures_csv"],
                "SUMMARY": res["summary"]}


ALGORITHMS = (EnsembleAlgorithm, ModelGateAlgorithm, QuickCreepAlgorithm)
