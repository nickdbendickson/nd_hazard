"""15 - Run the steps in order (headless): the window's chain as a Processing tool, for qgis_process
and overnight runs on a server. Same rules as the window (core/workflow.Chain): every step that is
not done or is out of date, in order, with the values the window would fill in from a settings
JSON; stops at the first failure or missing input; checks each output."""
import json
import os

from qgis import processing
from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterFile, QgsProcessingParameterString,
                       QgsProcessingParameterEnum, QgsProcessingParameterBoolean,
                       QgsProcessingParameterFileDestination, QgsProcessingException)

from ..core import workflow as wf
from ..compat import FILE_FOLDER

OPTIONAL_LABELS = ["0 - Build offline data package", "6 - Quick screen (triage)", "9 - Sensitivity",
                   "16 - Find ponds and dugouts (instead of 1-4)",
                   "18 - Merge dams and ponds (with 16: both kinds in one folder)"]
OPTIONAL_KEYS = ["0", "6", "9", "16", "18"]


def load_settings(text_or_path):
    """Settings for core/workflow.defaults from a JSON file path or a JSON string ('' = the
    window's defaults), or (0.13) from the suite settings file (.xlsx / .csv): its "window" rows
    are the window's choices and the file becomes the tools' defaults for this run. The window
    saves the same dict in the QGIS project (NDDamScreening/state)."""
    s = dict(wf.DEFAULT_SETTINGS)
    t = (text_or_path or "").strip()
    if not t:
        return s
    if os.path.isfile(t) and os.path.splitext(t)[1].lower() in (".xlsx", ".csv"):
        from ..damsuite import settings as DS
        try:
            sf = DS.Settings.load(t)
        except DS.SettingsError as e:
            raise QgsProcessingException(str(e))
        s.update(wf.window_settings_from(sf))
        s["settings_file"] = t
        DS.set_current_path(t, remember=False)
        return s
    if os.path.isfile(t):
        with open(t, encoding="utf-8") as fh:
            t = fh.read()
    try:
        loaded = json.loads(t)
    except ValueError as e:
        raise QgsProcessingException("Settings are not JSON ({}): give the file the window exports "
                                     "or a JSON object".format(e))
    if not isinstance(loaded, dict):
        raise QgsProcessingException("Settings must be a JSON object")
    s.update(wf.migrate_settings(loaded))
    return s


class RunChain(QgsProcessingAlgorithm):
    def name(self): return "runchain"
    def displayName(self): return "15 - Run the steps in order (headless)"
    def group(self): return "Dam screening"
    def groupId(self): return "dams"
    def createInstance(self): return RunChain()

    def shortHelpString(self):
        return ("The window's 'Run the steps in order' as a tool, so a statewide run can go through "
                "qgis_process or a script overnight. Steps 1, 2, 3, 4, 5, 7, 8 and 10 that are not done "
                "(or are out of date: an input changed since they ran) run one after the other in the work "
                "folder with the values the window would fill in (the work folder's files, the data package, "
                "the settings JSON the window exports). Optional steps 0, 6 and 9 run if ticked; 7b and 11-14 "
                "never. It stops at the first step that fails, lacks an input or writes no output, and says "
                "which. Settings JSON keys: see core/workflow.DEFAULT_SETTINGS (layers as file paths; the "
                "data package fills the rest; \"suite_folder\" names the suite data folder for a computer with "
                "no QGIS setting, 0.28.3).\n\nqgis_process run nddam:runchain --WORK=/path/to/work "
                "--SETTINGS=/path/to/damsuite_settings.xlsx --INCLUDE=1\n\nThe preflight check (17) runs first "
                "and a FAIL stops the chain before anything starts (PREFLIGHT=false skips it). A copy of the "
                "settings used goes to <work>/settings_used.xlsx. Not run in a real QGIS.\n\n"
                "0.28.3: a chain that fails, is refused by the preflight or cannot start now FAILS the "
                "tool, so qgis_process exits non-zero and a script notices. FAIL_ON_ERROR=false restores "
                "the old behaviour (OK=false in the results, exit code 0).")

    def initAlgorithm(self, config=None):
        P = self.addParameter
        P(QgsProcessingParameterFile("WORK", "Work folder", behavior=FILE_FOLDER))
        P(QgsProcessingParameterString("SETTINGS", "Settings (the window's JSON, or the suite settings "
                                       ".xlsx / .csv; file path or text; empty = defaults)",
                                       defaultValue="", optional=True, multiLine=True))
        P(QgsProcessingParameterString("DAM_ID", "Dam ID filter (DWR or NID ID; empty = all dams in the "
                                       "settings layer)", defaultValue="", optional=True))
        P(QgsProcessingParameterBoolean("PREFLIGHT", "Preflight check first (refuse to start on a FAIL)",
                                        defaultValue=True))
        P(QgsProcessingParameterEnum("INCLUDE", "Optional steps to include", OPTIONAL_LABELS,
                                     allowMultiple=True, optional=True))
        P(QgsProcessingParameterBoolean("DRY_RUN", "Only list what would run (nothing is started)",
                                        defaultValue=False))
        # 0.28.3: without this a failed chain still ended qgis_process with exit code 0, so a
        # script or a scheduled job could not tell a finished run from a failed one
        P(QgsProcessingParameterBoolean("FAIL_ON_ERROR", "Fail the tool when the chain fails (so "
                                        "qgis_process exits non-zero)", defaultValue=True))
        P(QgsProcessingParameterFileDestination("LOG", "Chain log (text)", fileFilter="Text (*.txt)",
                                                optional=True))

    def processAlgorithm(self, params, context, feedback):
        work = self.parameterAsString(params, "WORK", context)
        if not work:
            raise QgsProcessingException("Work folder path is empty")
        if not os.path.isdir(work):
            try:
                os.makedirs(work, exist_ok=True)
            except OSError as e:
                raise QgsProcessingException("Cannot create work folder {}: {}".format(work, e))
        settings = load_settings(self.parameterAsString(params, "SETTINGS", context))
        settings["work"] = work
        dam_id = (self.parameterAsString(params, "DAM_ID", context) or "").strip()
        if dam_id:
            settings["only_ids"] = dam_id
        # 0.22.1: QGIS gives option 0 for an optional multiple enum left out (qgis_process with no
        # --INCLUDE), which made every headless chain run step 0; nothing given = no optional step
        raw = params.get("INCLUDE")
        inc = [] if raw is None or raw == "" or raw == [] else \
            (self.parameterAsEnums(params, "INCLUDE", context) or [])
        include = tuple(OPTIONAL_KEYS[i] for i in inc if 0 <= i < len(OPTIONAL_KEYS))
        dry = self.parameterAsBool(params, "DRY_RUN", context)
        hard = self.parameterAsBool(params, "FAIL_ON_ERROR", context)
        log_path = self.parameterAsFileOutput(params, "LOG", context) or ""
        lines = []
        live = self._live = self._open_log(log_path)

        def log(m):
            lines.append(str(m))
            feedback.pushInfo(str(m))
            if live[0] is not None:          # 0.33.1: line by line, so a killed run leaves its log
                try:
                    live[0].write(str(m) + "\n")
                    live[0].flush()
                except (OSError, ValueError):
                    live[0] = None

        if dam_id:
            log("DAM_ID filter: {}".format(dam_id))

        plan = wf.chain_plan(work, settings, include)
        first, problem = wf.chain_next(work, settings, include, ())
        log("Plan: {}".format(", ".join(plan) if plan else "nothing to run"))
        if first is None and problem:
            log("Cannot start: " + problem)
        for st in plan:
            for line in wf.chain_summary(st, work, settings):
                log("    " + line)
        if dry or not plan:
            return self._finish(lines, log_path, ok=not problem, ran=[], result="dry run" if dry else
                                (problem or "nothing to run"), hard=hard)
        if self.parameterAsBool(params, "PREFLIGHT", context):
            from ..core import preflight as pf
            s2 = dict(settings)
            s2["chain"] = list(include)
            checks = pf.run_checks(work, s2, steps=plan)
            for line in pf.report_lines(checks):
                log(line)
            if not pf.ready(checks):
                return self._finish(lines, log_path, ok=False, ran=[],
                                    result="preflight: " + pf.summary(checks), hard=hard)
        # the record of the settings this run used (0.13)
        try:
            wf.write_run_settings(work, settings, log)
        except Exception as e:                                   # noqa
            log("settings record not written: {}".format(e))

        def definitions(aid):
            try:
                from qgis.core import QgsApplication
                alg = QgsApplication.processingRegistry().algorithmById(aid)
                out = []
                for d in alg.parameterDefinitions():
                    try:
                        from qgis.core import QgsProcessingParameterDefinition as D
                        try:
                            flag = D.Flag.FlagOptional
                        except AttributeError:
                            flag = D.FlagOptional
                        optional = bool(d.flags() & flag)
                    except Exception:                            # noqa
                        optional = False
                    out.append((d.name(), d.description(), optional, d.defaultValue() is not None,
                                bool(d.isDestination())))
                return out
            except Exception:                                    # noqa (the mock: no definitions)
                return []

        def problems(step, aid, p):
            try:
                return ["{} is empty".format(m) for m in wf.required_missing(definitions(aid), p)]
            except Exception:                                    # noqa
                return []

        # item 4: intercept pushWarning so warnings appear in the .log file too
        class _CaptureFeedback(type(feedback)):
            def pushWarning(self, warning):
                log("Warning: " + str(warning))
                super().pushWarning(warning)

            def reportError(self, error, fatalError=False):
                log(("Error: " if fatalError else "Warning: ") + str(error))
                super().reportError(error, fatalError)

        cap_fb = _CaptureFeedback()

        def start(step, aid, p, on_done):
            if feedback.isCanceled():
                return on_done(False, "cancelled")
            try:
                processing.run(aid, p, context=context, feedback=cap_fb, is_child_algorithm=True)
            except Exception as e:                               # noqa
                return on_done(False, str(e))
            on_done(True, "")

        chain = wf.Chain(work, lambda: settings, include, start, log=log, problems=problems)
        chain.begin()
        return self._finish(lines, log_path, ok=chain.ok, ran=chain.ran, result=chain.result,
                            hard=hard)

    @staticmethod
    def _open_log(log_path):
        """[open file or None]: the chain log written as the run goes (_finish rewrites it whole)."""
        if not log_path or log_path.upper() == "TEMPORARY_OUTPUT":
            return [None]
        try:
            os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
            return [open(log_path, "w", encoding="utf-8")]
        except OSError:
            return [None]

    def _finish(self, lines, log_path, ok, ran, result, hard=False):
        live = getattr(self, "_live", None)
        if live and live[0] is not None:
            try:
                live[0].close()
            except OSError:
                pass
            live[0] = None
        if log_path and log_path.upper() != "TEMPORARY_OUTPUT":
            try:
                with open(log_path, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + "\n")
            except OSError:
                log_path = ""
        out = {"OK": bool(ok), "RAN": ",".join(ran), "RESULT": result, "LOG": log_path}
        if hard and not ok:
            # 0.28.3: an exception is what makes qgis_process exit non-zero; the log file above
            # is written first, so the run leaves the same record either way
            raise QgsProcessingException(
                "the chain did not finish: {}{}. Steps that ran: {}. (FAIL_ON_ERROR=false "
                "returns OK=false instead of failing.)".format(
                    result, " (log: {})".format(log_path) if log_path else "",
                    ", ".join(ran) or "none"))
        return out
