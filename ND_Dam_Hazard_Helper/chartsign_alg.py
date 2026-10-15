# -*- coding: utf-8 -*-
"""v2.10 Processing algorithm ndhazard:chartsignoff - ACER TM 11 chart sign-off
(chartsign.py): the chart file for ND Dam Breach Runner / Screening from ND Dam
Hazard's curves, overlay SVGs on the engineer's scans of TM 11 Figs 2 and 3,
and a sign-off record. Register it in processing_provider.Provider.loadAlgorithms.

Self-contained on purpose (own base class, no import of processing_provider),
so processing_provider may import this module at any point without a cycle.
"""

import datetime
import os
import traceback

from qgis.core import (
    Qgis, QgsProcessingAlgorithm, QgsProcessingException, QgsProcessingParameterFile,
    QgsProcessingParameterString, QgsProcessingParameterBoolean,
    QgsProcessingParameterFolderDestination, QgsProcessingOutputFile, QgsProcessingOutputString,
)

from .compat import _first

FILE_FILE = _first(lambda: Qgis.ProcessingFileParameterBehavior.File,
                   lambda: QgsProcessingParameterFile.Behavior.File,
                   lambda: QgsProcessingParameterFile.File)
IMAGE_FILTER = "Images (*.png *.PNG *.jpg *.JPG *.jpeg *.JPEG)"


class ChartSignOffAlgorithm(QgsProcessingAlgorithm):
    def name(self):
        return "chartsignoff"

    def displayName(self):
        return "ACER TM 11 chart sign-off (chart file for the Breach Runner / Screening)"

    def group(self):
        return "Dam hazard classification"

    def groupId(self):
        return "damhazard"

    def createInstance(self):
        return type(self)()

    def shortHelpString(self):
        return ("Writes acer_tm11_charts.json - the depth-velocity danger chart file that ND "
                "Dam Breach Runner and ND Dam Breach Screening (core/nd_hazard.py) read - from "
                "ND Dam Hazard's own curves (the REG_05.2026 table, sampled every 0.1 ft/s), so all "
                "three tools use the same chart. One engineer's sign-off is enough: the file is "
                "marked 'digitized' and signed off by the name in 'Signed off by' (a second "
                "engineer may be recorded in 'Checked by'; tick 'Require a second checker' to "
                "demand one). Without a name the polygons are written for review with status "
                "'not digitized' and the other tools refuse the chart method. With 'Install in "
                "the plugins' the signed-off file replaces core/acer_tm11_charts.json of the "
                "installed Runner and Screening, so their chart method uses it at once. The "
                "plugins ship with the file signed off by Nicholas Bendickson (2026-10-12).\n\n"
                "To check the curves against the printed figures, give a scan (PNG / JPEG) of "
                "TM 11 Figure 2 and / or 3 and a calibration: two or more reference points "
                "'x_px, y_px = velocity_ft/s, depth_ft' separated by ';' (two points: the axes "
                "are taken parallel to the image edges; three: exact affine fit; four or more: "
                "least squares with a residual). The SVGs show the curves on the scan with a "
                "grid and a legend. Without a scan the curves are drawn on a plain chart.\n\n"
                "Chart_SignOff.txt records the names as entered, what the curves are, their "
                "documented fit residuals and the check of nd_hazard's zones against ND Dam "
                "Hazard's zones. The tool cannot confirm that a check was made. Not yet run "
                "in a real QGIS.")

    def initAlgorithm(self, config=None):
        P = self.addParameter
        P(QgsProcessingParameterFile("FIG2_IMAGE", "TM 11 Figure 2 (houses) scan, PNG / JPEG",
                                     behavior=FILE_FILE, fileFilter=IMAGE_FILTER, optional=True))
        P(QgsProcessingParameterString("FIG2_CALIBRATION", "Figure 2 calibration: 'x_px, y_px = "
                                       "v, d; ...' (2 or more points)", defaultValue="",
                                       optional=True))
        P(QgsProcessingParameterFile("FIG3_IMAGE", "TM 11 Figure 3 (mobile homes) scan, PNG / "
                                     "JPEG", behavior=FILE_FILE, fileFilter=IMAGE_FILTER,
                                     optional=True))
        P(QgsProcessingParameterString("FIG3_CALIBRATION", "Figure 3 calibration: 'x_px, y_px = "
                                       "v, d; ...'", defaultValue="", optional=True))
        P(QgsProcessingParameterString("DIGITIZED_FROM", "Digitized from (source document)",
                                       defaultValue="", optional=True))
        from .damsuite import tm11_curves as _tm
        P(QgsProcessingParameterString("DIGITIZED_BY", "Signed off by (engineer; digitized by)",
                                       defaultValue=_tm.SIGNED_OFF_BY, optional=True))
        P(QgsProcessingParameterString("CHECKED_BY", "Checked by (optional second engineer)",
                                       defaultValue="", optional=True))
        P(QgsProcessingParameterString("DIGITIZED_ON", "Date (empty: today)", defaultValue="",
                                       optional=True))
        P(QgsProcessingParameterString("NOTES", "Notes", defaultValue="", optional=True))
        P(QgsProcessingParameterBoolean("REQUIRE_SECOND_CHECKER", "Require a second, different "
                                        "engineer as checker", defaultValue=False))
        P(QgsProcessingParameterBoolean("INSTALL_IN_PLUGINS", "Install the signed-off file in "
                                        "the Breach Runner and the Screening (their "
                                        "core/acer_tm11_charts.json)", defaultValue=True))
        P(QgsProcessingParameterFolderDestination("OUTPUT", "Output folder"))
        self.addOutput(QgsProcessingOutputFile("CHART_JSON", "Chart file (acer_tm11_charts.json)"))
        self.addOutput(QgsProcessingOutputFile("HOUSE_SVG", "Figure 2 overlay (SVG)"))
        self.addOutput(QgsProcessingOutputFile("MOBILE_SVG", "Figure 3 overlay (SVG)"))
        self.addOutput(QgsProcessingOutputFile("RECORD", "Sign-off record (text)"))
        self.addOutput(QgsProcessingOutputString("STATUS", "digitized / not digitized"))

    def processAlgorithm(self, parameters, context, feedback):
        from . import chartsign as CS
        s = lambda k: (self.parameterAsString(parameters, k, context) or "").strip()  # noqa
        out = self.parameterAsFileOutput(parameters, "OUTPUT", context)
        if not out:
            raise QgsProcessingException("Choose an output folder")
        images, cals = {}, {}
        for key, img_p, cal_p in ((CS.HOUSE, "FIG2_IMAGE", "FIG2_CALIBRATION"),
                                  (CS.MOBILE, "FIG3_IMAGE", "FIG3_CALIBRATION")):
            img = self.parameterAsFile(parameters, img_p, context)
            if img:
                if not os.path.isfile(img):
                    raise QgsProcessingException("Image not found: {}".format(img))
                images[key] = img
            cals[key] = s(cal_p)
        require = self.parameterAsBool(parameters, "REQUIRE_SECOND_CHECKER", context)
        install = self.parameterAsBool(parameters, "INSTALL_IN_PLUGINS", context)
        try:
            res = CS.sign_off(out, digitized_by=s("DIGITIZED_BY"), checked_by=s("CHECKED_BY"),
                              digitized_from=s("DIGITIZED_FROM"),
                              digitized_on=s("DIGITIZED_ON") or datetime.date.today().isoformat(),
                              notes=s("NOTES"), images=images, calibrations=cals,
                              require_different_checker=require, install=install)
        except (ValueError, OSError) as e:
            raise QgsProcessingException(str(e)) from e
        except Exception as e:                       # never an unhandled crash
            try:
                feedback.reportError(traceback.format_exc(), False)
            except Exception:
                pass
            raise QgsProcessingException("Unexpected error: {!r}".format(e)) from e
        for line in res["lines"]:
            feedback.pushInfo(str(line))
        return {"OUTPUT": out, "CHART_JSON": res["chart_file"], "HOUSE_SVG": res["svgs"]["house"],
                "MOBILE_SVG": res["svgs"]["mobile"], "RECORD": res["record"],
                "STATUS": res["status"]}
