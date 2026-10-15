"""Spillway adequacy tool of the provider (core/adequacy.py): can the dam pass its Inflow Design
Flood under ND REG_05.2026 (Table 6.1, Sections 8.1.1 and 9)?"""
import os

from qgis.core import (QgsProcessingException, QgsProcessingOutputFile, QgsProcessingOutputString,
                       QgsProcessingParameterEnum, QgsProcessingParameterFolderDestination)

from .algorithms_tools import _ToolBase
from .core import adequacy as AD
from .core import breach as br
from .core import outlets as outmod

HAZARD_CHOICES = [("high", "High: IDF = PMF"), ("medium", "Medium: IDF = 0.1 % AEP (1,000-year)"),
                  ("low", "Low: IDF = 1 % AEP (100-year)")]


def _field(name, kind):
    from qgis.core import QgsField
    try:
        from qgis.PyQt.QtCore import QMetaType
        t = {"s": QMetaType.Type.QString, "d": QMetaType.Type.Double, "b": QMetaType.Type.Bool}[kind]
        return QgsField(name, t)
    except Exception:
        from qgis.PyQt.QtCore import QVariant
        return QgsField(name, {"s": QVariant.String, "d": QVariant.Double, "b": QVariant.Bool}[kind])


_KIND = {"storm_id": "s", "storm_type": "s", "duration_hr": "s", "overtops": "b", "passes": "b",
         "max_multiple_capped": "b", "routing_complete": "b", "file": "s"}


class SpillwayAdequacyAlgorithm(_ToolBase):
    def name(self):
        return "spillwayadequacy"

    def displayName(self):
        return "Spillway adequacy (pass the Inflow Design Flood?)"

    def shortHelpString(self):
        return ("Checks whether the dam passes its Inflow Design Flood (ND REG_05.2026). Each "
                "inflow hydrograph in the folder (nd_pmp's <out>/hydrographs/<storm_id>.csv: '#' "
                "header lines, columns read by name time_hr and flow_cfs; or plain time_hr,flow_cfs "
                "files) is routed through the reservoir WITHOUT a breach: from the starting pool "
                "(default the crest of the spillway rating, Section 9), through the spillway "
                "rating and optional outlets / gate, and over the embankment crest (broad-crested "
                "weir C = 2.6) above the top of dam. A storm passes when the peak pool is at or "
                "below the post-settlement top of dam minus the required residual freeboard "
                "(Section 8.1.1: high / medium the greater of 1 ft and the wind setup + runup; "
                "low 3 ft). The largest multiple of each inflow the dam passes is found by "
                "bisection (up to 2x). The storm with the highest pool governs.\n\n"
                "Required IDF by hazard class (Table 6.1): High = PMF, Medium = 0.1 % AEP, Low = 1 "
                "% AEP. Give the hydrographs of that flood: all PMP storm types and durations of "
                "the ND Statewide PMP Study (with snowmelt as appropriate). Use the entire "
                "drainage area, including non-contributing areas unless their exclusion is "
                "justified (6.2); an incremental damage analysis (6.4) may lower the IDF but the dam "
                "must still pass the 24-hour, 500-year rainstorm without overtopping.\n\n"
                "Pass / fail comes from the routed peak pool only - never from the flood "
                "scenario's 'failed' flag, which means a breach was modelled at the peak pool. "
                "Screening aid: an engineer must review the inputs and results.")

    def initAlgorithm(self, config=None):
        self._file("INFLOWS", "Folder of inflow hydrograph CSVs (an nd_pmp output folder or its "
                   "hydrographs/ folder)", folder=True, optional=False)
        self.addParameter(QgsProcessingParameterEnum(
            "HAZARD", "Hazard classification", [c[1] for c in HAZARD_CHOICES], defaultValue=0))
        self._num("TOP_FT", "Top of dam, post-settlement (ft; roadway excluded)")
        self._file("SPILLWAY_CSV", "Spillway rating CSV (elevation ft, discharge cfs)", "csv")
        self._num("CREST_LENGTH", "Embankment crest length (ft), for flow over the dam", optional=True,
                  minimum=0)
        self._num("START_FT", "Starting pool (ft; empty = the crest of the spillway rating, the "
                  "lowest ungated outlet or spillway)", optional=True)
        self._file("STAGE_STORAGE_CSV", "Stage-storage CSV (elevation ft, storage ac-ft); empty = "
                   "power law from the values below", "csv")
        self._num("STORAGE_TOP", "Storage at the top of dam (ac-ft), without a stage-storage table",
                  optional=True, minimum=0)
        self._num("AREA_TOP", "Surface area at the top of dam (ac), without a table",
                  optional=True, minimum=0)
        self._num("HEIGHT_FT", "Dam height (ft), without a table", optional=True, minimum=0)
        self._num("WIND_FT", "Wind setup + runup (ft; high / medium: freeboard = the greater of "
                  "1 ft and this)", optional=True, minimum=0)
        self._num("FREEBOARD_FT", "Required residual freeboard override (ft; empty = by hazard "
                  "class)", optional=True, minimum=0, advanced=True)
        self._text("OUTLETS", "Outlet works, one per line 'name; invert ft; diameter ft (or width "
                   "x height); cd; gate hours:fraction, ...'", advanced=True)
        self._text("SPILLWAY_GATE", "Gate on the spillway rating, hours:fraction pairs like "
                   "'0:1, 6:0.5'", advanced=True)
        self._bool("BISECT", "Find the largest multiple of each inflow that passes", True)
        self.addParameter(QgsProcessingParameterFolderDestination(
            "OUT_DIR", "Output folder (adequacy.csv, adequacy.md, governing_pool.png)"))
        try:                                   # (not in the test suite's stand-in qgis.core)
            from qgis.core import QgsProcessingParameterFeatureSink
            self.addParameter(QgsProcessingParameterFeatureSink(
                "OUTPUT", "Adequacy table", optional=True))
        except ImportError:
            pass
        self.addOutput(QgsProcessingOutputString("VERDICT", "Verdict"))
        self.addOutput(QgsProcessingOutputFile("CSV", "Adequacy CSV"))
        self.addOutput(QgsProcessingOutputFile("REPORT", "Adequacy report (markdown)"))
        self.addOutput(QgsProcessingOutputFile("PLOT", "Pool against time, governing storm (PNG)"))

    def processAlgorithm(self, parameters, context, feedback):
        o = lambda n: self._opt(parameters, n, context)            # noqa: E731
        folder = self._fileparam(parameters, "INFLOWS", context)
        top = o("TOP_FT")
        if not folder or top is None:
            raise QgsProcessingException("Give the inflow folder and the top of dam")
        hazard = HAZARD_CHOICES[self.parameterAsEnum(parameters, "HAZARD", context)][0]
        out_dir = self.parameterAsString(parameters, "OUT_DIR", context)
        os.makedirs(out_dir, exist_ok=True)
        dam = None
        ss = self._fileparam(parameters, "STAGE_STORAGE_CSV", context) or None
        if not ss:
            if o("STORAGE_TOP") is None or o("HEIGHT_FT") is None:
                raise QgsProcessingException("Give a stage-storage CSV, or the storage at the top "
                                             "of dam and the dam height")
            dam = br.DamData(dam_id="dam", dam_height_ft=o("HEIGHT_FT"),
                             storage_top_acft=o("STORAGE_TOP"), top_of_dam_ft=top,
                             surface_area_top_ac=o("AREA_TOP"))
        try:
            outlets = outmod.parse_outlets(self._str(parameters, "OUTLETS", context))
            sched = outmod.parse_schedule(self._str(parameters, "SPILLWAY_GATE", context))
            res = AD.assess(AD.find_inflows(folder), top, hazard,
                            spillway_csv=self._fileparam(parameters, "SPILLWAY_CSV", context) or None,
                            crest_length_ft=o("CREST_LENGTH"), start_pool_ft=o("START_FT"),
                            freeboard_ft=o("FREEBOARD_FT"), wind_setup_runup_ft=o("WIND_FT"),
                            stage_storage_csv=ss, dam=dam, outlets=outlets,
                            spillway_schedule=sched,
                            bisect=self.parameterAsBool(parameters, "BISECT", context))
        except (AD.AdequacyError, br.BreachInputError, ValueError) as e:
            raise QgsProcessingException(str(e))
        csv_p = AD.write_csv(res, os.path.join(out_dir, "adequacy.csv"))
        md_p = AD.write_report(res, os.path.join(out_dir, "adequacy.md"))
        png = AD.plot_governing(res, os.path.join(out_dir, "governing_pool.png"), top) or ""
        from qgis.core import QgsFeature, QgsFeatureSink, QgsFields, QgsWkbTypes
        fields = QgsFields()
        for c in AD.COLUMNS:
            fields.append(_field(c, _KIND.get(c, "d")))
        sink, dest = None, None
        if parameters.get("OUTPUT"):
            sink, dest = self.parameterAsSink(parameters, "OUTPUT", context, fields,
                                              QgsWkbTypes.NoGeometry)
        if sink is not None:
            for r in res["rows"]:
                f = QgsFeature(fields)
                f.setAttributes([r.get(c) if _KIND.get(c) != "s" else str(r.get(c) or "")
                                 for c in AD.COLUMNS])
                sink.addFeature(f, QgsFeatureSink.FastInsert)
        for r in res["rows"]:
            feedback.pushInfo("{storm_id}: peak inflow {peak_inflow_cfs} cfs, outflow "
                              "{peak_outflow_cfs} cfs, pool {peak_pool_ft} ft, residual freeboard "
                              "{residual_freeboard_ft} ft -> {}".format(
                                  "passes" if r["passes"] else "DOES NOT PASS", **r))
        v = res["verdict"]["text"]
        feedback.pushInfo(v)
        for n in res["notes"]:
            feedback.pushInfo("Note: " + n)
        out = {"VERDICT": v, "CSV": csv_p, "REPORT": md_p, "PLOT": png, "OUT_DIR": out_dir}
        if sink is not None:
            out["OUTPUT"] = dest
        return out


ADEQUACY_ALGORITHMS = (SpillwayAdequacyAlgorithm,)
