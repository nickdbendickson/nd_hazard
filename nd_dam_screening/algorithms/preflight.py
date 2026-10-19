"""17 - Preflight check: what would make the run fail halfway, checked in a few seconds
(core/preflight.py). The popup's 'Check before running' button and the headless chain (15) use
the same list."""
import os

from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterFile, QgsProcessingParameterString,
                       QgsProcessingParameterEnum, QgsProcessingParameterFileDestination,
                       QgsProcessingException)

from ..core import preflight as pf
from ..compat import FILE_FOLDER
from . import run_chain as rc


class Preflight(QgsProcessingAlgorithm):
    def name(self): return "preflight"
    def displayName(self): return "17 - Preflight check"
    def group(self): return "Dam screening"
    def groupId(self): return "dams"
    def createInstance(self): return Preflight()

    def shortHelpString(self):
        return ("Checks, in a few seconds, the things that otherwise fail a run halfway: the work folder "
                "and disk space, the USGS 3DEP service or the local DEM folder, the LISFLOOD-FP executable "
                "(when that engine is chosen), the dam / water-body / dam-line / structures layers and "
                "their fields and CRS, dams without a dam line, the ACER TM 11 chart file, the Runner "
                "plugin for step 7b, and the settings file. OK / WARN / FAIL per item with what to do; "
                "the headless chain (15) refuses to start on a FAIL. Reads GeoPackage layers directly; a "
                "project layer that is not a file is reported as 'could not open here' (the popup checks "
                "those through QGIS). Not run in a real QGIS.")

    def initAlgorithm(self, config=None):
        P = self.addParameter
        P(QgsProcessingParameterFile("WORK", "Work folder", behavior=FILE_FOLDER))
        P(QgsProcessingParameterString("SETTINGS", "Settings (the window's JSON, or the suite settings "
                                       ".xlsx / .csv; file path or text; empty = defaults)",
                                       defaultValue="", optional=True, multiLine=True))
        P(QgsProcessingParameterEnum("INCLUDE", "Optional steps to include", rc.OPTIONAL_LABELS,
                                     allowMultiple=True, optional=True))
        P(QgsProcessingParameterFileDestination("REPORT", "Preflight report (text)", fileFilter="Text (*.txt)",
                                                optional=True))

    def processAlgorithm(self, params, context, feedback):
        work = self.parameterAsString(params, "WORK", context)
        if not work:
            raise QgsProcessingException("Work folder: none given")
        settings = rc.load_settings(self.parameterAsString(params, "SETTINGS", context))
        settings["work"] = work
        inc = self.parameterAsEnums(params, "INCLUDE", context) or []
        settings["chain"] = [rc.OPTIONAL_KEYS[i] for i in inc if 0 <= i < len(rc.OPTIONAL_KEYS)]
        checks = pf.run_checks(work, settings)
        lines = pf.report_lines(checks)
        for line in lines:
            feedback.pushInfo(line)
        report = self.parameterAsFileOutput(params, "REPORT", context) or ""
        if report and report.upper() != "TEMPORARY_OUTPUT":
            try:
                os.makedirs(os.path.dirname(report) or ".", exist_ok=True)
                with open(report, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + "\n")
            except OSError as e:
                feedback.pushInfo("report not written: {}".format(e))
                report = ""
        return {"READY": pf.ready(checks), "SUMMARY": pf.summary(checks),
                "FAILS": sum(1 for c in checks if c["status"] == pf.FAIL),
                "WARNINGS": sum(1 for c in checks if c["status"] == pf.WARN), "REPORT": report}
