# -*- coding: utf-8 -*-
"""2.30.0: the suite's four main steps as Processing algorithms, so the whole chain can be
built in the Model Designer and run headless (qgis_process) for a statewide batch:

  ndhazard:buildpack    1. Build / update the offline data pack (build_nd_pack.py - the same
                        builder the "Suite data and servers" window uses)
  ndhazard:screendam    2. Screen one dam        -> ND Dam Breach Screening's "nddam:triage"
  ndhazard:runbreach    3. Run the breach model  -> ND Dam Breach Runner's "ndbreach:rundam"
  ndhazard:classify     4. Classify (processing_provider.ClassifyAlgorithm, since v2.0)
  ndhazard:suitechain   all four in order for one dam folder

Decisions:
  * Steps 2 and 3 belong to the other two plugins, and this plugin does not reimplement them:
    these algorithms LOOK UP the other provider's algorithm and run it, passing the dam folder
    and the dam id through. If that plugin is not installed, the algorithm says exactly which
    plugin and which algorithm id is missing instead of failing with a Processing error - the
    commonest reason a model "does nothing" is a mixed set of plugin versions.
  * Every parameter name a step takes is passed through as given (PARAMETERS, a JSON object or
    "KEY=VALUE" lines), because the other plugins' parameters change faster than this file can
    follow. What this file fixes is the CHAIN - folder in, dam id in, the next step's inputs
    come from the step before - not the other tools' option lists.
  * The chain stops at the first step that fails and says which one, so a statewide batch's log
    names the dam and the step, and nothing downstream runs on a half-finished model.
  * Nothing here changes a hazard result: step 4 is the existing classify algorithm with the
    existing defaults.
  * Not run inside a real QGIS (no QGIS in the build container): the parameter handling and the
    chain's step list are unit-tested through the pure helpers at the bottom.
"""

import json
import os

from qgis.core import (
    Qgis, QgsProcessingAlgorithm, QgsProcessingException,
    QgsProcessingParameterBoolean, QgsProcessingParameterFile, QgsProcessingParameterString,
    QgsProcessingOutputString, QgsProcessingOutputFolder,
)

from .compat import _first

GROUP = ("Dam suite chain", "damsuitechain")
# the four steps: (key, label, provider:algorithm, which plugin it comes from)
STEPS = (
    ("pack", "Build / update the offline data pack", "ndhazard:buildpack",
     "ND Dam Hazard Classification Helper (this plugin)"),
    ("screen", "Screen the dam", "nddam:triage", "ND Dam Breach Screening"),
    ("breach", "Run the breach model", "ndbreach:rundam", "ND Dam Breach Runner"),
    ("classify", "Classify the dam", "ndhazard:classify",
     "ND Dam Hazard Classification Helper (this plugin)"),
)
STEP_KEYS = tuple(k for k, _l, _a, _p in STEPS)
FILE_FOLDER = _first(lambda: Qgis.ProcessingFileParameterBehavior.Folder,
                     lambda: QgsProcessingParameterFile.Behavior.Folder,
                     lambda: QgsProcessingParameterFile.Folder)
NO_THREADING = _first(lambda: Qgis.ProcessingAlgorithmFlag.NoThreading,
                      lambda: QgsProcessingAlgorithm.Flag.FlagNoThreading,
                      lambda: QgsProcessingAlgorithm.FlagNoThreading)


# ------------------------------------------------------------- pure helpers --
def parse_parameters(text):
    """The extra parameters of a step: a JSON object, or "KEY=VALUE" one per line (or both
    separated by ";"). Returns a dict; an empty / unreadable text gives {}.

    Values are kept as text unless they are plain JSON scalars, because the other plugins'
    parameters are their own business: "true", "3", "a/path" all reach them unchanged in the
    form they expect."""
    text = (text or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            got = json.loads(text)
            return {str(k): v for k, v in got.items()} if isinstance(got, dict) else {}
        except ValueError:
            return {}
    out = {}
    for part in text.replace(";", "\n").splitlines():
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key:
            continue
        low = value.lower()
        if low in ("true", "false"):
            out[key] = low == "true"
            continue
        try:
            out[key] = int(value) if value.lstrip("+-").isdigit() else float(value)
        except ValueError:
            out[key] = value
    return out


def dam_id_of(dam_folder, given=""):
    """The dam id of a dam folder: what was given, else dam.json's id, else the folder name
    (the suite's convention - one folder per dam, named after the dam id)."""
    given = (given or "").strip()
    if given:
        return given
    folder = dam_folder or ""
    try:
        path = os.path.join(folder, "dam.json")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                rec = json.load(fh) or {}
            for key in ("dam_id", "id", "nid_id"):
                if str(rec.get(key) or "").strip():
                    return str(rec[key]).strip()
    except (OSError, ValueError):
        pass
    return os.path.basename(os.path.normpath(folder)) if folder else ""


def chain_steps(pack=False, screen=True, breach=True, classify=True):
    """The steps of a chain run, in order: [(key, label, algorithm id, plugin)]."""
    want = {"pack": pack, "screen": screen, "breach": breach, "classify": classify}
    return [s for s in STEPS if want.get(s[0])]


def missing_message(step):
    """What to say when a step's algorithm is not registered in this QGIS."""
    _key, label, alg, plugin = step
    return ("{} cannot run: the algorithm '{}' is not in this QGIS. Install and enable {} (the "
            "whole suite is one release set - a mixed set is the commonest cause of a chain "
            "that does nothing).".format(label, alg, plugin))


def _registry_algorithm(alg_id):
    """The Processing algorithm with this id, or None (never raises)."""
    try:
        from qgis.core import QgsApplication
        return QgsApplication.processingRegistry().algorithmById(alg_id)
    except Exception:                                            # noqa - no registry here
        return None


def run_algorithm(alg_id, params, context=None, feedback=None):
    """processing.run for a step; a missing algorithm raises QgsProcessingException with a
    message that names the plugin to install."""
    if _registry_algorithm(alg_id) is None:
        step = next((s for s in STEPS if s[2] == alg_id), ("", alg_id, alg_id, "that plugin"))
        raise QgsProcessingException(missing_message(step))
    import processing
    return processing.run(alg_id, params, context=context, feedback=feedback)


class _ChainBase(QgsProcessingAlgorithm):
    def group(self):
        return GROUP[0]

    def groupId(self):
        return GROUP[1]

    def createInstance(self):
        return type(self)()

    def flags(self):
        return super().flags() | NO_THREADING

    def _params(self, parameters, context, name="PARAMETERS"):
        try:
            text = self.parameterAsString(parameters, name, context)
        except Exception:                                        # noqa - headless dict
            text = parameters.get(name) or ""
        return parse_parameters(text)

    def _folder(self, parameters, context, name="DAM_FOLDER"):
        try:
            folder = self.parameterAsFile(parameters, name, context)
        except Exception:                                        # noqa
            folder = parameters.get(name) or ""
        return str(folder or "")

    def _flag(self, parameters, context, name, default=True):
        try:
            return bool(self.parameterAsBool(parameters, name, context))
        except Exception:                                        # noqa
            return bool(parameters.get(name, default))

    def _text(self, parameters, context, name, default=""):
        try:
            return str(self.parameterAsString(parameters, name, context) or default)
        except Exception:                                        # noqa
            return str(parameters.get(name, default) or default)


# ------------------------------------------------------- 1. the offline pack --
class BuildPackAlgorithm(_ChainBase):
    def name(self):
        return "buildpack"

    def displayName(self):
        return "1 - Build / update the offline data pack"

    def shortHelpString(self):
        return ("Builds (or refreshes) the suite's offline data pack - buildings, roads, rail, "
                "lifelines, dams, 3DHP streams and waterbodies, land cover and the DWR / HIFLD "
                "layers from the files in the data folder - with the same builder as 'Suite "
                "data and servers' > 'Build / update the offline pack'. Needs internet access "
                "and GDAL (run it inside QGIS); the first build takes an hour or more.\n\n"
                "LAYERS ONLY: name the data sets to build, comma separated, to refresh a few "
                "instead of all of them. The log and LAYERS.txt say what is in the pack and "
                "what is not.")

    def initAlgorithm(self, config=None):
        P = self.addParameter
        P(QgsProcessingParameterFile("PACK", "Pack folder (or .zip) to build - empty: the "
                                     "suite data folder's pack", behavior=FILE_FOLDER,
                                     optional=True))
        P(QgsProcessingParameterString("LAYERS", "Layers only (comma separated; empty: every "
                                       "layer)", optional=True))
        P(QgsProcessingParameterBoolean("REFRESH", "Fetch data sets that are already there "
                                        "again", defaultValue=False))
        self.addOutput(QgsProcessingOutputFolder("PACK_FOLDER", "Pack"))
        self.addOutput(QgsProcessingOutputString("SUMMARY", "Summary"))

    def processAlgorithm(self, parameters, context, feedback):
        from . import build_nd_pack as BNP
        from .damsuite import suitedata as SD
        pack = self._folder(parameters, context, "PACK")
        if not pack:
            folder = SD.data_folder()
            if not folder:
                raise QgsProcessingException(
                    "No pack folder given and no suite data folder is set. Choose a folder in "
                    "'Suite data and servers' (it sets the whole suite up) or give PACK.")
            pack = os.path.join(folder, SD.PACK_NAMES[0] if hasattr(SD, "PACK_NAMES")
                                else "ND_Dam_Suite_Pack")
        only = (self._text(parameters, context, "LAYERS") or "").strip() or None
        refresh = self._flag(parameters, context, "REFRESH", False)

        def progress(pct, msg=""):
            if feedback is not None:
                if feedback.isCanceled():
                    raise BNP.BuildCancelled("cancelled")
                feedback.setProgress(max(0.0, min(100.0, float(pct))))
                if msg:
                    feedback.setProgressText(str(msg))
        try:
            man = BNP.build(pack, only=only, refresh=refresh, progress=progress,
                            log=lambda m: feedback.pushInfo(str(m)) if feedback else None)
        except Exception as e:                                   # noqa - a build problem
            raise QgsProcessingException("The pack was not built: {}".format(e)) from e
        sets = (man or {}).get("datasets") or {}
        bad = sorted(k for k, v in sets.items()
                     if (v or {}).get("status") not in ("complete", "not available"))
        summary = "{} data set(s) in the pack{}".format(
            len(sets), "; not complete: " + ", ".join(bad) if bad else "")
        if feedback is not None:
            feedback.pushInfo(summary)
        return {"PACK_FOLDER": pack, "SUMMARY": summary}


# ---------------------------------------------- 2. / 3. the other two plugins --
class _DelegatingAlgorithm(_ChainBase):
    """A step that belongs to another plugin of the suite: the dam folder and the dam id are
    passed through, everything else comes from PARAMETERS as the other tool spells it."""
    step_key = ""

    def _step(self):
        return next(s for s in STEPS if s[0] == self.step_key)

    def initAlgorithm(self, config=None):
        P = self.addParameter
        P(QgsProcessingParameterFile("DAM_FOLDER", "Dam folder (one folder per dam, named "
                                     "after the dam id)", behavior=FILE_FOLDER))
        P(QgsProcessingParameterString("DAM_ID", "Dam id (empty: dam.json, else the folder "
                                       "name)", optional=True))
        P(QgsProcessingParameterString(
            "PARAMETERS", "Other parameters of this step: JSON, or KEY=VALUE per line",
            optional=True, multiLine=True))
        self.addOutput(QgsProcessingOutputString("ALGORITHM", "Algorithm run"))
        self.addOutput(QgsProcessingOutputString("SUMMARY", "Summary"))

    def processAlgorithm(self, parameters, context, feedback):
        step = self._step()
        folder = self._folder(parameters, context)
        if not folder or not os.path.isdir(folder):
            raise QgsProcessingException("The dam folder does not exist: {}".format(folder))
        dam_id = dam_id_of(folder, self._text(parameters, context, "DAM_ID"))
        params = dict(self._params(parameters, context))
        params.setdefault("DAMS_FOLDER", os.path.dirname(os.path.normpath(folder)))
        params.setdefault("DAM_FOLDER", folder)
        params.setdefault("DAM_ID", dam_id)
        if feedback is not None:
            feedback.pushInfo("{}: {} for dam '{}'".format(step[1], step[2], dam_id))
        out = run_algorithm(step[2], params, context, feedback)
        summary = "{} finished ({} output(s))".format(step[1], len(out or {}))
        return {"ALGORITHM": step[2], "SUMMARY": summary}


class ScreenDamAlgorithm(_DelegatingAlgorithm):
    step_key = "screen"

    def name(self):
        return "screendam"

    def displayName(self):
        return "2 - Screen one dam (ND Dam Breach Screening)"

    def shortHelpString(self):
        return ("Runs ND Dam Breach Screening's quick screen ('{}') for one dam folder, so "
                "screening, modelling and classification can be one model or one headless "
                "command. The screening plugin must be installed and enabled; its own "
                "parameters go in PARAMETERS (JSON or KEY=VALUE lines) and reach it "
                "unchanged.".format("nddam:triage"))


class RunBreachAlgorithm(_DelegatingAlgorithm):
    step_key = "breach"

    def name(self):
        return "runbreach"

    def displayName(self):
        return "3 - Run the breach model for one dam (ND Dam Breach Runner)"

    def shortHelpString(self):
        return ("Runs ND Dam Breach Runner's '{}' for one dam folder: the depth, velocity and "
                "arrival-time rasters the classification reads. The runner plugin must be "
                "installed and enabled; its own parameters (breach equation, DEM, solver ...) "
                "go in PARAMETERS (JSON or KEY=VALUE lines).".format("ndbreach:rundam"))


# ------------------------------------------------------------- the whole chain --
class SuiteChainAlgorithm(_ChainBase):
    def name(self):
        return "suitechain"

    def displayName(self):
        return "Run the suite's steps in order for one dam (headless)"

    def shortHelpString(self):
        return ("Screen -> model -> classify for one dam folder, with the offline pack built "
                "first when asked. Each step is the other plugin's own algorithm; the chain "
                "stops at the first failure and says which step and which dam it was.\n\n"
                "For a statewide batch, run this from the command line once per dam folder "
                "(qgis_process run ndhazard:suitechain ...), or put it in a model with an "
                "iterator over the dam folders. The classification's own options are the "
                "defaults of 'Dam hazard classification'; give CLASSIFY_PARAMETERS to change "
                "them.")

    def initAlgorithm(self, config=None):
        P = self.addParameter
        P(QgsProcessingParameterFile("DAM_FOLDER", "Dam folder", behavior=FILE_FOLDER))
        P(QgsProcessingParameterString("DAM_ID", "Dam id (empty: dam.json, else the folder "
                                       "name)", optional=True))
        P(QgsProcessingParameterBoolean("BUILD_PACK", "1 - build / update the offline pack "
                                        "first", defaultValue=False))
        P(QgsProcessingParameterBoolean("SCREEN", "2 - screen the dam", defaultValue=True))
        P(QgsProcessingParameterBoolean("BREACH", "3 - run the breach model",
                                        defaultValue=True))
        P(QgsProcessingParameterBoolean("CLASSIFY", "4 - classify the dam", defaultValue=True))
        for key, label in (("SCREEN_PARAMETERS", "Screening parameters (JSON / KEY=VALUE)"),
                           ("BREACH_PARAMETERS", "Breach model parameters (JSON / KEY=VALUE)"),
                           ("CLASSIFY_PARAMETERS", "Classification parameters "
                                                   "(JSON / KEY=VALUE)")):
            P(QgsProcessingParameterString(key, label, optional=True, multiLine=True))
        self.addOutput(QgsProcessingOutputString("STEPS_RUN", "Steps run"))
        self.addOutput(QgsProcessingOutputString("ND_CLASS", "ND hazard class"))
        self.addOutput(QgsProcessingOutputString("SUMMARY", "Summary"))

    def processAlgorithm(self, parameters, context, feedback):
        folder = self._folder(parameters, context)
        if not folder or not os.path.isdir(folder):
            raise QgsProcessingException("The dam folder does not exist: {}".format(folder))
        dam_id = dam_id_of(folder, self._text(parameters, context, "DAM_ID"))
        steps = chain_steps(self._flag(parameters, context, "BUILD_PACK", False),
                            self._flag(parameters, context, "SCREEN", True),
                            self._flag(parameters, context, "BREACH", True),
                            self._flag(parameters, context, "CLASSIFY", True))
        if not steps:
            raise QgsProcessingException("No step is ticked - nothing to run.")
        extra = {"screen": self._params(parameters, context, "SCREEN_PARAMETERS"),
                 "breach": self._params(parameters, context, "BREACH_PARAMETERS"),
                 "classify": self._params(parameters, context, "CLASSIFY_PARAMETERS")}
        done, nd_class = [], ""
        for i, step in enumerate(steps):
            key, label, alg, _plugin = step
            if feedback is not None:
                feedback.setProgress(100.0 * i / len(steps))
                feedback.pushInfo("Step {} of {}: {} ({}) for dam '{}'".format(
                    i + 1, len(steps), label, alg, dam_id))
            params = dict(extra.get(key) or {})
            if key == "pack":
                params.setdefault("PACK", "")
            else:
                params.setdefault("DAM_FOLDER", folder)
                params.setdefault("DAM_ID", dam_id)
                params.setdefault("DAMS_FOLDER", os.path.dirname(os.path.normpath(folder)))
            try:
                out = run_algorithm(alg, params, context, feedback) or {}
            except QgsProcessingException:
                raise
            except Exception as e:                               # noqa - one step's failure
                raise QgsProcessingException(
                    "The chain stopped at step {} of {} ({}) for dam '{}': {}".format(
                        i + 1, len(steps), label, dam_id, e)) from e
            done.append(key)
            if key == "classify":
                nd_class = str(out.get("ND_CLASS") or out.get("ND_HAZARD_CLASS") or "")
        summary = "Dam '{}': {} step(s) run ({}){}".format(
            dam_id, len(done), ", ".join(done),
            "; ND hazard class {}".format(nd_class) if nd_class else "")
        if feedback is not None:
            feedback.pushInfo(summary)
        return {"STEPS_RUN": ",".join(done), "ND_CLASS": nd_class, "SUMMARY": summary}


ALGORITHMS = (BuildPackAlgorithm, ScreenDamAlgorithm, RunBreachAlgorithm, SuiteChainAlgorithm)
