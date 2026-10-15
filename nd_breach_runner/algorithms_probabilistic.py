"""Probabilistic (Monte Carlo) breach runs tool of the provider (id 'probabilistic').

Builds on SensitivityAlgorithm (same dam, terrain, scenario and flow-path
parameters). Register it with `PROBABILISTIC_ALGORITHMS` (see the end of the
file), e.g. at the end of algorithms.py next to TOOL_ALGORITHMS."""
from qgis.core import QgsProcessingException, QgsProcessingOutputFile, QgsProcessingOutputFolder

from .algorithms import RunDamAlgorithm, SensitivityAlgorithm, _Feedback, _floats
from .core import breach as brmod, handoff, workflow
from .core import probabilistic as probmod
from .core.lisflood import Cancelled, ModelError


class ProbabilisticAlgorithm(SensitivityAlgorithm):
    def name(self):
        return "probabilistic"

    def displayName(self):
        return "Probabilistic breach runs (Monte Carlo)"

    def shortHelpString(self):
        return ("Runs the base breach model, then N Monte Carlo runs (LISFLOOD-FP only) on the "
                "base run's domain, inflow point and duration, with the breach width, formation "
                "time and Manning's n drawn as factors on the base run's values (Latin "
                "hypercube, reproducible from the seed), and optionally the breach method drawn "
                "from a list with weights. The n factor scales the Manning's n raster too.\n\n"
                "Distributions, one per input: 'tri min mode max', 'uniform min max', "
                "'lognormal median sigma' (sigma of ln x), 'fixed value'; empty = not varied. "
                "Methods: e.g. 'piping 0.6, overtopping 0.4' (ids as in the breach method list: "
                "piping, overtopping, piping_orifice, froehlich1995_piping, mlm1984, vtg1990, "
                "tr60, ...); empty = the chosen method in every run.\n\n"
                "READ FIRST. The default distributions (width x0.5/1.0/1.5, time x0.5/1.0/2.0, "
                "n x0.75/1.0/1.3, triangular) are ADJUSTABLE PLACEHOLDERS, not published "
                "values: set them from the uncertainty you can defend. The pool level at "
                "failure is not varied (the breach module has no input for it).\n\n"
                "Writes to <Dam folder>/Probabilistic/: 'Probability Depth at least X ft.tif' "
                "(share of the runs with at least X ft), 'Depth P10/P50/P90 (ft).tif', "
                "'Velocity P90 (ft-s).tif', 'Arrival P10 (hr).tif' (an early, plausible arrival: "
                "10th percentile over the runs that reach the cell; blank where fewer than 10% "
                "do), Probabilistic_Runs.csv, Probabilistic_Summary.csv, "
                "Probabilistic_Receptors.csv (each structure's probabilities, with a structure "
                "layer), a convergence plot, an HTML report and probabilistic_manifest.json "
                "(seed, distributions and every sample).\n\n"
                "Option 'Write each run's rasters for the class probability' (off by "
                "default): every finished run also gets Probabilistic/Runs/run_NNN/ with "
                "'Depth (Max).tif' (ft), 'Velocity (Max).tif' (ft/s), 'Arrival Time.tif' "
                "(hours) and run.json (the sample, its weight, the run key; the base run's "
                "'Baseline ...' rasters are referenced, not copied), listed in "
                "Probabilistic/Runs/index.json. ND Dam Hazard's class probability tool "
                "classifies these folders. The log estimates the disk space.\n\n"
                "It takes N + 1 model runs; the log gives an estimate after the base run. An "
                "interrupted study continues: runs already finished for the same samples are "
                "not run again. Use a coarser cell size for a first look.")

    def initAlgorithm(self, config=None):
        RunDamAlgorithm.initAlgorithm(self, config)     # not the sensitivity cases
        self._num("PROB_N", "Number of runs (N)", probmod.DEFAULT_N, minimum=probmod.MIN_RUNS,
                  maximum=probmod.MAX_RUNS, integer=True)
        self._num("PROB_SEED", "Random seed (the same seed and N give the same runs)",
                  probmod.DEFAULT_SEED, minimum=0, integer=True)
        self._text("PROB_THRESHOLDS", "Depth thresholds for the probability rasters (ft), "
                   "comma separated (starting values 1, 3 are the engineer's, screening values "
                   "you choose)", "1, 3")
        self._text("PROB_WIDTH", "Breach width factor: distribution (placeholder default; "
                   "empty = not varied)", probmod.default_width().to_text())
        self._text("PROB_TIME", "Formation time factor: distribution (placeholder default; "
                   "empty = not varied)", probmod.default_time().to_text())
        self._text("PROB_MANNING", "Manning's n factor: distribution (placeholder default; "
                   "empty = not varied)", probmod.default_manning().to_text())
        self._text("PROB_METHODS", "Breach methods to draw from, with weights (e.g. 'piping "
                   "0.6, overtopping 0.4'; empty = the chosen method)", "")
        self._bool("PROB_RUN_RASTERS", "Write each run's rasters for the class probability "
                   "(Probabilistic/Runs/run_NNN: depth, velocity, arrival and run.json, for ND "
                   "Dam Hazard's class probability tool; takes disk space)", False)
        self._bool("CLASSIFY", "Classify every run with the ND hazard classification plugin",
                   False, advanced=True)
        self._text("CLASS_ALG", "Classification algorithm ID (empty = ndhazard:classify)",
                   advanced=True)
        self._text("CLASS_MAP", "Classification parameters, JSON (see the README)",
                   advanced=True)
        self.addOutput(QgsProcessingOutputFile("PROB_RUNS", "Probabilistic_Runs.csv"))
        self.addOutput(QgsProcessingOutputFile("PROB_SUMMARY", "Probabilistic_Summary.csv"))
        self.addOutput(QgsProcessingOutputFile("PROB_REPORT", "Probabilistic report (HTML)"))
        self.addOutput(QgsProcessingOutputFolder("PROB_FOLDER", "Probabilistic folder"))

    def _dist_text(self, parameters, name, context):
        """A parameter left out (a script) takes its default; an empty one means 'not
        varied'."""
        v = parameters.get(name)
        if v is None or (hasattr(v, "isNull") and v.isNull()):
            return (self.parameterAsString(parameters, name, context) or "").strip()
        return self._str(parameters, name, context)

    def spec_from_params(self, parameters, context):
        try:
            return probmod.ProbSpec.from_parts(
                self._dist_text(parameters, "PROB_WIDTH", context),
                self._dist_text(parameters, "PROB_TIME", context),
                self._dist_text(parameters, "PROB_MANNING", context),
                self._str(parameters, "PROB_METHODS", context))
        except ValueError as e:
            raise QgsProcessingException(str(e))

    def processAlgorithm(self, parameters, context, feedback):
        spec = self.spec_from_params(parameters, context)       # a bad spec fails first
        thresholds = _floats(self._str(parameters, "PROB_THRESHOLDS", context),
                             probmod.DEFAULT_THRESHOLDS_FT)
        dam, st, src, writer, dam_xy, inflow, folder = self.prepare(parameters, context,
                                                                    feedback)
        st.tier = workflow.TIER_LISFLOOD
        classify = None
        if self.parameterAsBool(parameters, "CLASSIFY", context):
            try:
                classify = handoff.make_classifier(
                    self._str(parameters, "CLASS_ALG", context),
                    handoff.load_mapping(self._str(parameters, "CLASS_MAP", context)),
                    feedback)
            except handoff.HandoffError as e:
                feedback.reportError("Classification skipped: {}".format(e), fatalError=False)
        for a, b in spec.describe():
            feedback.pushInfo("{}: {}".format(a, b))
        feedback.pushInfo(probmod.DEFAULTS_NOTE)
        fb = _Feedback(feedback)
        try:
            out = probmod.run_probabilistic(
                dam, dam_xy, st, src, writer, folder, inflow, spec,
                self.parameterAsInt(parameters, "PROB_N", context),
                self.parameterAsInt(parameters, "PROB_SEED", context), thresholds,
                fb.progress, fb.log, fb.cancel, classify,
                run_rasters=self.parameterAsBool(parameters, "PROB_RUN_RASTERS", context))
        except Cancelled:
            raise QgsProcessingException("Cancelled (finished runs are kept; run again with "
                                         "the same seed and N to continue)")
        except (ModelError, brmod.BreachInputError, ValueError) as e:
            raise QgsProcessingException(str(e))
        for w in out.warnings:
            feedback.pushWarning("Check: " + w)
        for f in out.flags:
            feedback.pushInfo(f)
        for n, p in out.rasters.items():
            self.load(context, p, "{} {}".format(dam.dam_id, n[:-4]))
        if out.runs_index:
            feedback.pushInfo("Per-run folders for ND Dam Hazard's class probability: {} "
                              "(index: {})".format(out.runs_folder, out.runs_index))
        base = out.base
        return {"PROB_RUNS": out.runs_csv, "PROB_SUMMARY": out.summary_csv,
                "PROB_REPORT": out.report_html, "PROB_FOLDER": out.folder, "FOLDER": folder,
                "REPORT": base.report_md if base else "",
                "REPORT_HTML": base.report_html if base else "", "DEPTH": "", "VELOCITY": "",
                "ARRIVAL": "", "WSE": "", "EAP_MAP": ""}


PROBABILISTIC_ALGORITHMS = (ProbabilisticAlgorithm,)
