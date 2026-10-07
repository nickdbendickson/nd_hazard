"""Processing algorithm "Self-test the installation in this QGIS" (ndbreach:selftest).

Runs the checks of core/selftest.py inside QGIS, where GDAL, the Processing registry and the
model executables are the ones the user's tools really use, and adds the checks that only make
sense there: the QGIS version, that every algorithm of this provider is registered and builds
its parameters, and the ndhazard provider. Writes a text report and prints, at the end of the
log, a block to paste to the developer.

Wiring (in algorithms.py, after the tools import):
    from .algorithms_selftest import SELFTEST_ALGORITHMS      # noqa: E402
    ALGORITHMS = CORE_ALGORITHMS + TOOL_ALGORITHMS + SELFTEST_ALGORITHMS
"""
import os
import shutil
import tempfile
import time

from qgis.core import (QgsProcessingException, QgsProcessingOutputFile,
                       QgsProcessingOutputString, QgsProcessingParameterFileDestination)

from .algorithms import SET_THREADS, _setting
from .algorithms_tools import _ToolBase
from .core import selftest


def qgis_version():
    """(QGIS_VERSION_INT, version text) from qgis.core.Qgis, whatever this QGIS calls them."""
    from qgis.core import Qgis
    num = getattr(Qgis, "QGIS_VERSION_INT", None)
    text = getattr(Qgis, "QGIS_VERSION", None)
    if num is None:
        try:
            num = Qgis.versionInt()
        except Exception:
            num = None
    if text is None:
        try:
            text = Qgis.version()
        except Exception:
            text = ""
    try:
        num = int(num) if num is not None else None
    except (TypeError, ValueError):
        num = None
    return num, str(text or "")


def algorithm_classes():
    """The plugin's algorithm classes, including this one whether or not algorithms.py lists
    it yet (so a missing registration is reported as a problem, not hidden)."""
    from . import algorithms
    classes = list(algorithms.ALGORITHMS)
    if not any(c.__name__ == SelfTestAlgorithm.__name__ and c.__module__ == SelfTestAlgorithm.__module__
               for c in classes):
        classes.append(SelfTestAlgorithm)
    return classes


def _registry():
    from qgis.core import QgsApplication
    return QgsApplication.processingRegistry()


class SelfTestAlgorithm(_ToolBase):
    def group(self):
        return "Dam breach modelling"

    def groupId(self):
        return "breach"

    def name(self):
        return "selftest"

    def displayName(self):
        return "Self-test the installation in this QGIS"

    def shortHelpString(self):
        return ("Checks that this QGIS can run the plugin: GDAL and the QGIS, Python and numpy "
                "versions; that every tool of the plugin is registered; LISFLOOD-FP (and a "
                "tiny flood run) and the ANUGA environment; and, on a small synthetic valley, "
                "the GeoTIFF writing and reading, unit handling, reprojection, DEM mosaics, "
                "gap filling and land-cover roughness. With 'Run a small model as well' it "
                "runs a whole dam breach on that valley (seconds with LISFLOOD-FP). Each check "
                "is PASS, FAIL (with the exact error) or SKIP (with the reason).\n\n"
                "The report is written to a text file and printed in the log, ending with a "
                "block to copy and send to the developer if anything failed.")

    def initAlgorithm(self, config=None):
        self._bool("RUN_MODEL", "Run a small model as well (a few seconds with LISFLOOD-FP; "
                   "ANUGA adds under a minute)", True)
        self._file("LISFLOOD_EXE", "LISFLOOD-FP executable (empty = provider setting, an installed LISFLOOD-FP, the "
                   "one bundled with the plugin, else the built-in engine; 'builtin' = the "
                   "built-in engine)", advanced=True)
        self._file("ANUGA_PYTHON", "ANUGA Python interpreter (empty = provider setting or "
                   "auto-detected)", advanced=True)
        self._num("THREADS", "Threads for the models (0 = provider setting, then all cores)", 0,
                  minimum=0, integer=True, advanced=True)
        self._bool("KEEP", "Keep the working files even when everything passes", False,
                   advanced=True)
        self.addParameter(QgsProcessingParameterFileDestination(
            "OUTPUT", "Report", "Text files (*.txt)", optional=True))
        self.addOutput(QgsProcessingOutputFile("REPORT", "Report"))
        self.addOutput(QgsProcessingOutputString("RESULT", "Result"))

    # -- the QGIS-only checks ----------------------------------------------------------------
    def _qgis_checks(self):
        def version():
            num, text = qgis_version()
            return selftest.check_qgis_version(num, text)

        def algorithms():
            return selftest.check_algorithms(_registry(), algorithm_classes(), "ndbreach")

        def hazard():
            return selftest.describe_ndhazard(_registry())

        def handoff(workdir):
            return selftest.check_handoff_dry_run(_registry(), workdir)
        return version, algorithms, hazard, handoff

    def processAlgorithm(self, parameters, context, feedback):
        run_model = self.parameterAsBool(parameters, "RUN_MODEL", context)
        keep = self.parameterAsBool(parameters, "KEEP", context)
        exe = self.find_lisflood(parameters, context)
        py = self.find_anuga(parameters, context)
        threads = int(self.parameterAsDouble(parameters, "THREADS", context)) or int(
            _setting(SET_THREADS, 0) or 0)
        out = self.parameterAsFileOutput(parameters, "OUTPUT", context)
        if not out:
            out = os.path.join(tempfile.gettempdir(), "ndbreach_selftest_{}.txt".format(
                time.strftime("%Y%m%d_%H%M%S")))
        try:
            os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        except OSError as e:
            raise QgsProcessingException("Cannot write the report to {}: {}".format(out, e))
        work = tempfile.mkdtemp(prefix="ndbreach_selftest_")

        def live(r):
            first = (r.detail or "").strip().splitlines()[:1]
            line = "[{}] {}{}".format(r.status, r.name, " - " + first[0] if first else "")
            if r.status == selftest.FAIL:
                feedback.reportError(line, fatalError=False)
            else:
                feedback.pushInfo(line)

        report = selftest.Report(on_result=live, cancelled=feedback.isCanceled)
        report.info = selftest.environment_info()
        num, text = qgis_version()
        report.info.insert(0, ("QGIS", "{} ({})".format(text or "?", num)))
        report.info.append(("LISFLOOD-FP used", exe or "none found"))
        report.info.append(("ANUGA Python used", py or "none found"))
        report.info.append(("Working folder", work))
        version, algorithms, hazard, handoff = self._qgis_checks()

        def progress(p, msg):
            feedback.setProgress(p)
            feedback.setProgressText(msg)

        feedback.pushInfo("Self-test in {}".format(work))
        selftest.run_all(
            report, work, exe, py, run_model, threads,
            before=[("QGIS version", version), ("Plugin algorithms registered and buildable",
                                                algorithms)],
            after=[("ndhazard provider and its algorithms", hazard),
                   ("ndhazard hand-off (dry run)", lambda: handoff(work))],
            progress=progress)
        full = report.text()
        block = report.developer_block()
        try:
            with open(out, "w", encoding="utf-8") as f:
                f.write(full + "\n\n" + block + "\n")
        except OSError as e:
            feedback.reportError("Could not write the report to {}: {}".format(out, e),
                                 fatalError=False)
        feedback.pushInfo("")
        feedback.pushInfo(full)
        feedback.pushInfo("")
        feedback.pushInfo("Report written to " + out)
        feedback.pushInfo("If anything failed, copy this and send it to the developer:")
        feedback.pushInfo(block)
        if report.ok():
            if keep:
                feedback.pushInfo("Working files kept in " + work)
            else:
                shutil.rmtree(work, ignore_errors=True)
        else:
            feedback.reportError("{} check(s) failed. Working files kept in {}".format(
                report.counts()[selftest.FAIL], work), fatalError=False)
        return {"REPORT": out, "RESULT": report.counts_text()}


SELFTEST_ALGORITHMS = (SelfTestAlgorithm,)
