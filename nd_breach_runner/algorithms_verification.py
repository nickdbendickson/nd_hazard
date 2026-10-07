"""Processing algorithms "Verification suite (analytic dam-break tests)" (ndbreach:verification)
and "Validation record from a benchmark comparison" (ndbreach:validationrecord).

The first runs the small problems with known answers of core/verification.py through the
installed LISFLOOD-FP (and ANUGA when there is an interpreter) and writes the Verification
Record (Word), a markdown/HTML summary, a CSV of every measure and one SVG plot per case. The
second turns a benchmark folder (written by "Compare with an approved study (benchmark)" or by
the HEC-RAS import) into a Word Validation Record with blank sign-off and acceptance blocks.

Wiring (in algorithms.py, after the tools import, as for algorithms_selftest.py):
    from .algorithms_verification import VERIFICATION_ALGORITHMS      # noqa: E402
    ALGORITHMS = CORE_ALGORITHMS + TOOL_ALGORITHMS + ... + VERIFICATION_ALGORITHMS
"""
import os
import tempfile
import time

from qgis.core import (QgsProcessingException, QgsProcessingOutputFile,
                       QgsProcessingOutputFolder, QgsProcessingOutputString,
                       QgsProcessingParameterFileDestination)

from .algorithms import SET_THREADS, _setting
from .algorithms_tools import _ToolBase
from .core import verification
from .core.lisflood import Cancelled


class VerificationSuiteAlgorithm(_ToolBase):
    def name(self):
        return "verification"

    def displayName(self):
        return "Verification suite (analytic dam-break tests)"

    def shortHelpString(self):
        return ("Runs small problems with a known answer through the model codes installed on "
                "this computer, exactly as the plugin runs them, and writes a Verification "
                "Record: Ritter's dry-bed and Stoker's wet-bed dam breaks, steady uniform "
                "flow against Manning's normal depth, the volume balance of a breach flood on "
                "a synthetic valley and the grid-orientation symmetry of radial spreading "
                "(LISFLOOD-FP); Ritter, uniform flow and volume balance with ANUGA when an "
                "ANUGA interpreter is set. Every case reports its measures against stated "
                "tolerances and PASS, FAIL or SKIP (with the reason).\n\n"
                "The tolerances are this tool's acceptance criteria, not a standard. Where "
                "the local-inertial solver of LISFLOOD-FP is known not to reproduce a "
                "solution (fast, supercritical flow; Neal et al. 2012) the criterion bounds "
                "the expected behaviour and is marked as such in the record.\n\n"
                "Output: 'Verification Record.docx' (machine and software identification with "
                "SHA-256 of the executables, a PASS/FAIL table, a section and plot per case, "
                "and a blank sign-off table), a markdown and HTML summary, a CSV of all "
                "measures and the SVG plots. Quick mode takes under a minute on a laptop; the "
                "full mode uses finer grids and meshes.")

    def initAlgorithm(self, config=None):
        self._bool("QUICK", "Quick mode (coarser grids and meshes; about a minute)", True)
        self._text("CASES", "Cases (empty = all; ids separated by commas: {}; or 'lisflood', "
                   "'anuga')".format(", ".join(verification.CASE_IDS)), advanced=True)
        self._file("OUT_FOLDER", "Output folder (empty = a new folder in the temporary "
                   "directory)", folder=True)
        self._file("LISFLOOD_EXE", "LISFLOOD-FP executable (empty = provider setting, an installed LISFLOOD-FP, the "
                   "one bundled with the plugin, else the built-in engine; 'builtin' = the "
                   "built-in engine)", advanced=True)
        self._file("ANUGA_PYTHON", "ANUGA Python interpreter (empty = provider setting or "
                   "auto-detected; without one the ANUGA cases are skipped)", advanced=True)
        self._num("THREADS", "Threads for the models (0 = provider setting, then all cores)", 0,
                  minimum=0, integer=True, advanced=True)
        self._bool("KEEP_WORK", "Keep the model input and output files (work folder)", True,
                   advanced=True)
        self.addOutput(QgsProcessingOutputFile("REPORT", "Verification Record (Word)"))
        self.addOutput(QgsProcessingOutputFolder("FOLDER", "Output folder"))
        self.addOutput(QgsProcessingOutputString("RESULT", "Result"))

    def processAlgorithm(self, parameters, context, feedback):
        quick = self.parameterAsBool(parameters, "QUICK", context)
        cases_text = self._str(parameters, "CASES", context)
        try:
            todo = verification.select_cases(cases_text or None)
        except ValueError as e:
            raise QgsProcessingException(str(e))
        exe = self.find_lisflood(parameters, context)
        py = self.find_anuga(parameters, context)
        if not py or not os.path.isfile(py):
            py = None
        need_lf = any(verification.CASE_INFO[c][1] == "LISFLOOD-FP" for c in todo)
        if need_lf and (not exe or not os.path.isfile(exe)):
            raise QgsProcessingException(
                "Set the LISFLOOD-FP executable in Processing > Options > Providers > ND Dam "
                "Breach Runner (or in the advanced parameters); none was found on this "
                "computer. Found: '{}'".format(exe))
        if not need_lf:
            exe = exe if exe and os.path.isfile(exe) else None
        need_anuga = any(verification.CASE_INFO[c][1] == "ANUGA" for c in todo)
        if need_anuga and not py:
            feedback.pushInfo("No ANUGA Python interpreter found: the ANUGA cases will be "
                              "reported as SKIP.")
        threads = int(self.parameterAsDouble(parameters, "THREADS", context)) or int(
            _setting(SET_THREADS, 0) or 0)
        out_dir = self._fileparam(parameters, "OUT_FOLDER", context)
        if not out_dir:
            out_dir = os.path.join(tempfile.gettempdir(), "ndbreach_verification_{}".format(
                time.strftime("%Y%m%d_%H%M%S")))
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            raise QgsProcessingException("Cannot write to {}: {}".format(out_dir, e))
        keep = self.parameterAsBool(parameters, "KEEP_WORK", context)

        def progress(p, msg=""):
            feedback.setProgress(p)
            if msg:
                feedback.setProgressText(msg)

        try:
            res = verification.run_suite(
                exe, anuga_python=py, out_dir=out_dir, cases=todo, quick=quick,
                progress=progress, log=feedback.pushInfo, cancel=feedback.isCanceled,
                threads=threads, keep_work=keep)
        except Cancelled:
            raise QgsProcessingException("Cancelled")
        except (OSError, ValueError, RuntimeError) as e:
            raise QgsProcessingException("The verification suite could not run: {}".format(e))
        for c in res.cases:
            line = "[{}] {} ({}): {}".format(c["status"], c["name"], c["model"], c["summary"])
            if c["status"] in (verification.FAIL, verification.ERROR):
                feedback.reportError(line, fatalError=False)
            else:
                feedback.pushInfo(line)
        docx = res.files.get("docx", "")
        if docx:
            feedback.pushInfo("Verification Record: " + docx)
        else:
            feedback.reportError("The Word record could not be written; the markdown and HTML "
                                 "summaries are in " + out_dir, fatalError=False)
        return {"REPORT": docx, "FOLDER": out_dir, "RESULT": res.counts_text()}


class ValidationRecordAlgorithm(_ToolBase):
    def name(self):
        return "validationrecord"

    def displayName(self):
        return "Validation record from a benchmark comparison"

    def shortHelpString(self):
        return ("Writes a Word Validation Record from the folder that 'Compare with an "
                "approved study (benchmark)' (or the HEC-RAS import with the benchmark "
                "option) wrote: the identification of the study and of this model run, the "
                "agreement metrics with their verdict, the depth-class confusion matrix, the "
                "list of difference and agreement rasters, an acceptance statement, and a "
                "blank sign-off table (Prepared by / Reviewed by / Date).\n\n"
                "Fields you leave empty stay blank in the record to be filled in by hand. "
                "The acceptance statement is what the responsible engineer decides; it is "
                "left blank unless you type it here. The record shows the tool's metrics and "
                "verdict; it does not state that the model is approved.")

    def initAlgorithm(self, config=None):
        self._file("BENCH_FOLDER", "Benchmark folder (with Benchmark_Summary.csv)",
                   folder=True, optional=False)
        self._text("DAM_ID", "Dam ID (empty = taken from the folder name)")
        self._text("DAM_NAME", "Dam name", advanced=True)
        self._text("REFERENCE_MODEL", "Reference model", "HEC-RAS 2D")
        self._text("REFERENCE_VERSION", "Reference model version", advanced=True)
        self._text("REFERENCE_DESC", "Description of the reference results (which run, "
                   "scenario, terrain, roughness)")
        self._text("STUDY_TITLE", "Approved study: title or report number")
        self._text("STUDY_BY", "Approved study: prepared by", advanced=True)
        self._text("STUDY_DATE", "Approved study: date", advanced=True)
        self._text("APPROVAL", "Approval reference (letter, permit or date)", advanced=True)
        self._text("THIS_MODEL", "This tool's model and settings (empty = left to fill in)",
                   advanced=True)
        self._text("ACCEPTANCE", "Acceptance statement (empty = left blank for the "
                   "responsible engineer)", advanced=True)
        self.addParameter(QgsProcessingParameterFileDestination(
            "OUTPUT", "Validation Record", "Word documents (*.docx)"))
        self.addOutput(QgsProcessingOutputFile("REPORT", "Validation Record (Word)"))
        self.addOutput(QgsProcessingOutputString("RESULT", "Result"))

    def processAlgorithm(self, parameters, context, feedback):
        folder = self._fileparam(parameters, "BENCH_FOLDER", context)
        if not folder or not os.path.isdir(folder):
            raise QgsProcessingException("Choose the benchmark folder ('{}' is not a "
                                         "folder)".format(folder))
        out = self._out(parameters, context)
        s = lambda n: self._str(parameters, n, context)
        try:
            r = verification.validation_record(
                folder, out, dam_id=s("DAM_ID"), reference_description=s("REFERENCE_DESC"),
                reference_model=s("REFERENCE_MODEL") or "HEC-RAS 2D",
                study_title=s("STUDY_TITLE"), study_by=s("STUDY_BY"), study_date=s("STUDY_DATE"),
                approval=s("APPROVAL"), reference_version=s("REFERENCE_VERSION"),
                this_model=s("THIS_MODEL"), dam_name=s("DAM_NAME"),
                acceptance_statement=s("ACCEPTANCE"))
        except (OSError, ValueError, KeyError) as e:
            raise QgsProcessingException("Could not write the validation record: {}".format(e))
        for w in r.get("warnings") or []:
            feedback.reportError("Check: " + w, fatalError=False)
        msg = "Verdict: {}. Record: {}".format(r.get("verdict") or "n/a", r["path"])
        feedback.pushInfo(msg)
        return {"REPORT": r["path"], "RESULT": msg}


VERIFICATION_ALGORITHMS = (VerificationSuiteAlgorithm, ValidationRecordAlgorithm)
