"""Processing algorithm "Nested run: fine grid near the dam, coarse downstream"
(ndbreach:nested)."""

from qgis.core import QgsProcessingException, QgsProcessingOutputFile

from .algorithms import (RunDamAlgorithm, _Feedback)
from .core import breach as brmod, nested as nestedmod
from .core.lisflood import Cancelled, ModelError


class NestedRunAlgorithm(RunDamAlgorithm):
    def name(self):
        return "nested"

    def displayName(self):
        return "Nested run: fine grid near the dam, coarse grid downstream"

    def shortHelpString(self):
        return ("LISFLOOD-FP runs on one cell size. This tool runs the first stretch below "
                "the dam on a fine grid (where the flow is fast and the embankment, tailrace "
                "and crossings matter), measures the discharge through a section across the "
                "flow at the end of that stretch, and starts a second run on the coarse grid "
                "(the 'Model cell size') from that section, extending downstream as usual. "
                "The rasters in the dam folder are the two merged on the coarse grid (fine "
                "results upstream of the section, coarse ones downstream); the fine rasters "
                "are kept in Nested/near_dam. With the flood scenario the baseline and the "
                "incremental rasters are merged the same way.\n\n"
                "One-way nesting: the coarse model does not affect the fine one, the section "
                "is a straight plane across the local flow direction, and only the discharge "
                "through it is handed over. LISFLOOD-FP only (no ANUGA).")

    def initAlgorithm(self, config=None):
        super().initAlgorithm(config)
        self._num("FINE_CELL", "Nested: fine cell size near the dam (m; 3 m = 9.8 ft, the "
                  "raster cell)", 3.0, minimum=0.5)
        self._num("REACH_MI", "Nested: length of the fine stretch below the dam (mi)", 1.5,
                  minimum=0.1)
        self._num("FINE_HALF_WIDTH_MI", "Nested: half-width of the fine domain (mi)", 0.35,
                  minimum=0.05, advanced=True)
        self.addOutput(QgsProcessingOutputFile("NESTED_REPORT", "Nested run report"))

    def processAlgorithm(self, parameters, context, feedback):
        dam, st, src, writer, dam_xy, inflow, folder = self.prepare(parameters, context,
                                                                    feedback)
        fb = _Feedback(feedback)
        g = lambda n: self.parameterAsDouble(parameters, n, context)
        try:
            out = nestedmod.run_nested(
                dam, dam_xy, st, src, writer, folder, inflow, g("FINE_CELL"), g("REACH_MI"),
                fine_half_width_mi=g("FINE_HALF_WIDTH_MI"), progress=fb.progress, log=fb.log,
                cancel=fb.cancel)
        except Cancelled:
            raise QgsProcessingException("Cancelled")
        except (ModelError, brmod.BreachInputError) as e:
            raise QgsProcessingException(str(e))
        for w in out.warnings:
            feedback.pushWarning("Check: " + w)
        for k, v in out.summary.items():
            feedback.pushInfo("{}: {}".format(k, v))

        class _R:            # what load_results reads
            final, increment, vectors = out.final, out.increment, {}
        self.load_results(context, _R, dam.dam_id)
        return {"DEPTH": out.final.get("depth"), "VELOCITY": out.final.get("velocity"),
                "ARRIVAL": out.final.get("arrival"), "WSE": out.final.get("wse"),
                "REPORT": out.report_md, "REPORT_HTML": "", "EAP_MAP": "", "FOLDER": folder,
                "NESTED_REPORT": out.report_md}


NESTED_ALGORITHMS = (NestedRunAlgorithm,)
