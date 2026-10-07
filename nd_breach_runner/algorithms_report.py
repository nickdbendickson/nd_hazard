"""The submittal report algorithm of the provider (Word report from a dam folder)."""
import os

from qgis.core import (QgsProcessingException, QgsProcessingOutputFile, QgsProcessingOutputString,
                       QgsProcessingParameterFile, QgsProcessingParameterFileDestination)

from .algorithms_tools import _ToolBase
from .compat import ALG_NO_THREADING, FILE_FILE
from .core import report_docx


class SubmittalReportAlgorithm(_ToolBase):
    def flags(self):
        # SVG figures may be drawn with Qt (fonts, painters), which belongs on the main thread
        return super().flags() | ALG_NO_THREADING

    def name(self):
        return "submittal"

    def displayName(self):
        return "Submittal report (Word)"

    def shortHelpString(self):
        return ("Writes a Word (.docx) submittal report from a dam folder: summary of results, "
                "dam and reservoir data, breach parameters and method with the run's warnings, "
                "flood scenario, hydraulic model settings and mass balance, results (depth, "
                "velocity and arrival tables, the EAP map, hydrograph and gauge figures), "
                "receptors and consequences, sensitivity and cascade results, the assumptions "
                "and what must be verified, the run manifest, a reviewer checklist pre-filled "
                "from what the folder contains, and a file list. Whatever the folder does not "
                "hold is left out. It reads the run report in _breach_model, the CSV and "
                "GeoJSON products, consequences*.csv/.md and run_manifest.json when present. "
                "Figures are SVG in the folder; they are embedded as PNG when QGIS's Qt SVG, "
                "cairosvg, rsvg-convert, inkscape or LibreOffice can convert them, and are "
                "listed by file name otherwise. Word builds the table of contents (page "
                "numbers) when the file is opened: answer Yes to update fields.")

    def initAlgorithm(self, config=None):
        self._file("DAM_FOLDER", "Dam folder (the one holding 'Depth (Max).tif' and "
                   "_breach_model)", folder=True, optional=False)
        self._text("DAM_ID", "Dam ID (empty = from the run report, else the folder name)")
        self._text("DAM_NAME", "Dam name (empty = from the run report)")
        self._text("TITLE", "Report title", "Dam Breach Inundation Analysis")
        self._text("PROJECT", "Project or submittal name")
        self._text("ENGINEER", "Engineer (name, licence)")
        self._text("FIRM", "Firm")
        self.addParameter(QgsProcessingParameterFile(
            "LOGO", "Logo image for the cover (PNG or JPEG, optional)", FILE_FILE,
            optional=True, fileFilter="Images (*.png *.jpg *.jpeg)"))
        self._bool("INC_FIGURES", "Include the figures (EAP map, hydrograph, gauges)", True)
        self._bool("INC_SENSITIVITY", "Include sensitivity, cascade and probabilistic results",
                   True)
        self._bool("INC_CONSEQUENCES", "Include consequences (consequences*.csv / .md) and the ND "
                   "5.2 hazard classification", True)
        self._bool("INC_REPRO", "Include reproducibility (run manifest, run settings)", True)
        self._bool("INC_CHECKLIST", "Include the reviewer checklist", True)
        self._bool("INC_FILES", "Include the file list", True)
        self._bool("INC_RUNREPORT", "Append the run report text as written by the tool", False,
                   advanced=True)
        self._num("FIG_WIDTH", "Figure resolution (pixels wide)", 1600, minimum=600,
                  maximum=4000, integer=True, advanced=True)
        self.addParameter(QgsProcessingParameterFileDestination(
            "OUTPUT", "Submittal report", "Word documents (*.docx)"))
        self.addOutput(QgsProcessingOutputFile("REPORT", "Submittal report (Word)"))
        self.addOutput(QgsProcessingOutputString("RESULT", "Result"))

    def processAlgorithm(self, parameters, context, feedback):
        folder = self.parameterAsFile(parameters, "DAM_FOLDER", context)
        if not folder or not os.path.isdir(folder):
            raise QgsProcessingException("The dam folder does not exist: {}".format(folder))
        out = self._out(parameters, context)
        if not out.lower().endswith(".docx"):
            out += ".docx"
        b = lambda k: self.parameterAsBool(parameters, k, context)      # noqa: E731
        logo = self._fileparam(parameters, "LOGO", context)
        opt = report_docx.SubmittalOptions(
            title=self._str(parameters, "TITLE", context) or "Dam Breach Inundation Analysis",
            dam_name=self._str(parameters, "DAM_NAME", context),
            project=self._str(parameters, "PROJECT", context),
            engineer=self._str(parameters, "ENGINEER", context),
            firm=self._str(parameters, "FIRM", context),
            logo=logo if logo and os.path.isfile(logo) else "",
            include_figures=b("INC_FIGURES"), include_sensitivity=b("INC_SENSITIVITY"),
            include_consequences=b("INC_CONSEQUENCES"), include_reproducibility=b("INC_REPRO"),
            include_checklist=b("INC_CHECKLIST"), include_file_list=b("INC_FILES"),
            include_run_report=b("INC_RUNREPORT"),
            figure_width_px=int(self.parameterAsInt(parameters, "FIG_WIDTH", context) or 1600))
        if logo and not opt.logo:
            feedback.reportError("Check: the logo file {} was not found; the cover has no "
                                 "logo.".format(logo), fatalError=False)
        feedback.setProgress(5)
        try:
            res = report_docx.build_submittal(folder, self._str(parameters, "DAM_ID", context),
                                              out, opt)
        except OSError as e:
            raise QgsProcessingException("Could not write {}: {}".format(out, e))
        feedback.setProgress(100)
        feedback.pushInfo("Wrote {} section(s): {}".format(
            len(res.sections), "; ".join("{} {}".format(n, t) for n, t in res.sections)))
        for f in res.figures:
            if f["embedded"]:
                feedback.pushInfo("Figure embedded: {} (via {})".format(
                    f["name"], res.svg_backend or "an existing image"))
            else:
                feedback.reportError("Check: figure not embedded, listed by file name: {} ({}). "
                                     "{}".format(f["name"], f["source"], f["note"]),
                                     fatalError=False)
        for w in res.warnings:
            feedback.reportError("Check: " + w, fatalError=False)
        if res.missing:
            feedback.pushInfo("Not found in the dam folder (sections skipped or marked in the "
                              "checklist): " + ", ".join(res.missing))
        c = res.counts
        msg = "{} sections, {} tables, {} images: {}".format(
            len(res.sections), c.get("tables", 0), c.get("images", 0), out)
        feedback.pushInfo(msg)
        return {"REPORT": out, "RESULT": msg}


REPORT_ALGORITHMS = (SubmittalReportAlgorithm,)
