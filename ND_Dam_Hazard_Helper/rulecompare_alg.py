# -*- coding: utf-8 -*-
"""v2.10 Processing algorithm ndhazard:rulecompare - ND Dam Hazard's results
re-classified with the Breach Runner / Screening rules (rulecompare.py).
Register it in processing_provider.Provider.loadAlgorithms.

Self-contained on purpose (own base class, no import of processing_provider),
so processing_provider may import this module at any point without a cycle.
"""

import os
import traceback

from qgis.core import (
    Qgis, QgsProcessingAlgorithm, QgsProcessingException, QgsProcessingParameterFile,
    QgsProcessingParameterRasterLayer, QgsProcessingParameterEnum, QgsProcessingParameterNumber,
    QgsProcessingOutputFile, QgsProcessingOutputString,
)

from .compat import _first
from .helpers import get_layer_path

FILE_FOLDER = _first(lambda: Qgis.ProcessingFileParameterBehavior.Folder,
                     lambda: QgsProcessingParameterFile.Behavior.Folder,
                     lambda: QgsProcessingParameterFile.Folder)
FILE_FILE = _first(lambda: Qgis.ProcessingFileParameterBehavior.File,
                   lambda: QgsProcessingParameterFile.Behavior.File,
                   lambda: QgsProcessingParameterFile.File)
NUM_DOUBLE = _first(lambda: Qgis.ProcessingNumberParameterType.Double,
                    lambda: QgsProcessingParameterNumber.Type.Double,
                    lambda: QgsProcessingParameterNumber.Double)
METHOD_OPTIONS = ["nd_hazard charts made from ND Dam Hazard's curves (only sampling / "
                  "habitability / rules differ)",
                  "nd_hazard conservative method (no chart)"]


class RuleCompareAlgorithm(QgsProcessingAlgorithm):
    def name(self):
        return "rulecompare"

    def displayName(self):
        return "Compare with the Breach Runner / Screening rules (nd_hazard)"

    def group(self):
        return "Dam hazard classification"

    def groupId(self):
        return "damhazard"

    def createInstance(self):
        return type(self)()

    def shortHelpString(self):
        return ("Re-classifies the structures of a finished ND Dam Hazard run with the rules of "
                "ND Dam Breach Runner / ND Dam Breach Screening (their core/nd_hazard.py, found "
                "in the installed plugins or given here): the depth / velocity rasters sampled "
                "their way (cells inside the outline, maximum depth with maximum velocity), "
                "habitability and mobile home from the occupancy text, and either ND Dam "
                "Hazard's own TM 11 curves as charts (so only sampling, habitability and the "
                "rules differ), the conservative no-chart method, or a signed-off chart file.\n\n"
                "Writes outputs/QA/Rules_Comparison.csv and .html: per structure both results, "
                "whether they differ and why (sampling area, habitability, mobile home type, "
                "chart / method, judgment zone rule), the dam class from each and a verdict. "
                "ND Dam Hazard remains the classification authority. Not yet run in a real "
                "QGIS.")

    def initAlgorithm(self, config=None):
        P = self.addParameter
        P(QgsProcessingParameterFile("OUTPUT_FOLDER", "The dam folder or its outputs folder",
                                     behavior=FILE_FOLDER))
        P(QgsProcessingParameterRasterLayer("DEPTH", "Breach maximum depth (ft) - empty: the "
                                            "one in QA/Run_Settings.json", optional=True))
        P(QgsProcessingParameterRasterLayer("VELOCITY", "Breach maximum velocity (ft/s) - "
                                            "empty: from QA/Run_Settings.json", optional=True))
        P(QgsProcessingParameterEnum("METHOD", "nd_hazard lethality method",
                                     options=METHOD_OPTIONS, defaultValue=0))
        P(QgsProcessingParameterFile("CHART_FILE", "Or: a signed-off chart file (.json) - "
                                     "overrides the method", behavior=FILE_FILE,
                                     extension="json", optional=True))
        P(QgsProcessingParameterFile("ND_HAZARD_PY", "nd_hazard.py (empty: from the installed "
                                     "Breach Runner / Screening)", behavior=FILE_FILE,
                                     extension="py", optional=True))
        P(QgsProcessingParameterNumber("WET_FT", "nd_hazard 'any depth' (ft)", type=NUM_DOUBLE,
                                       defaultValue=0.1, minValue=0.01))
        self.addOutput(QgsProcessingOutputFile("CSV", "Rules_Comparison.csv"))
        self.addOutput(QgsProcessingOutputFile("HTML", "Rules_Comparison.html"))
        self.addOutput(QgsProcessingOutputString("VERDICT", "Verdict"))

    def _raster(self, parameters, name, context):
        v = parameters.get(name) if isinstance(parameters, dict) else None
        if isinstance(v, str) and v and os.path.isfile(v):
            return v
        lyr = self.parameterAsRasterLayer(parameters, name, context)
        return get_layer_path(lyr) if lyr is not None else None

    def processAlgorithm(self, parameters, context, feedback):
        from . import rulecompare as RC
        folder = self.parameterAsFile(parameters, "OUTPUT_FOLDER", context)
        if not folder or not os.path.isdir(folder):
            raise QgsProcessingException("Folder not found: {}".format(folder))
        try:
            m = int(self.parameterAsEnum(parameters, "METHOD", context))
        except (TypeError, ValueError):
            m = 0
        method = RC.METHODS[m] if 0 <= m < len(RC.METHODS) else RC.METHOD_CURVES
        try:
            wet = float(self.parameterAsDouble(parameters, "WET_FT", context))
        except (TypeError, ValueError):
            wet = 0.1
        try:
            out = RC.compare_folder(
                folder, depth_path=self._raster(parameters, "DEPTH", context),
                vel_path=self._raster(parameters, "VELOCITY", context), method=method,
                chart_file=self.parameterAsFile(parameters, "CHART_FILE", context) or None,
                nh_path=self.parameterAsFile(parameters, "ND_HAZARD_PY", context) or None,
                wet_ft=wet)
        except (ValueError, OSError) as e:
            raise QgsProcessingException(str(e)) from e
        except Exception as e:                       # never an unhandled crash
            try:
                feedback.reportError(traceback.format_exc(), False)
            except Exception:
                pass
            raise QgsProcessingException("Unexpected error: {!r}".format(e)) from e
        feedback.pushInfo(out["verdict"])
        for n in out["notes"]:
            feedback.pushInfo(n)
        return {"CSV": out["csv"], "HTML": out["html"], "VERDICT": out["verdict"]}
