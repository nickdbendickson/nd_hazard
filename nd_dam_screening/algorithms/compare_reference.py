"""11 - Compare a tool-7 flood with a reference model (HEC-RAS 2D, DSS-WISE Lite, the Runner)."""
import os

import numpy as np
from osgeo import gdal
from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterFile, QgsProcessingParameterEnum,
                       QgsProcessingParameterNumber, QgsProcessingParameterString,
                       QgsProcessingParameterRasterLayer, QgsProcessingParameterFolderDestination,
                       QgsProcessingException)

from ..core import benchmark as bm, gridutil as gu
from . import _common as cm
from ..compat import (FILE_FOLDER, NUM_DOUBLE, SRC_POINT)
from ..damsuite import gdalsafe as GS

REF_UNITS = ["US customary: depth ft, velocity ft/s, arrival h (HEC-RAS, DSS-WISE Lite after ND Dam "
             "Hazard's import, ND Dam Breach Runner)",
             "Metric: depth m, velocity m/s, arrival h"]
SCENARIOS = ["td - top of dam (ND standard)", "sd - sunny day"]
M2_PER_AC = 4046.8564224


class CompareReference(QgsProcessingAlgorithm):
    def name(self): return "comparereference"
    def displayName(self): return "11 - Compare with a reference model"
    def group(self): return "Dam screening"
    def groupId(self): return "dams"
    def createInstance(self): return CompareReference()

    def shortHelpString(self):
        return ("Compares the tool-7 flood of a dam (max depth, max velocity, arrival time) with "
                "the same flood from another model - a HEC-RAS 2D run, a DSS-WISE Lite result "
                "(convert its rasters to GeoTIFF / feet first: ND Dam Hazard's import does that), "
                "or the ND Dam Breach Runner - with the Runner's own benchmark metrics (the same "
                "functions): fit index F = A∩B / A∪B (= critical success index), hit rate, "
                "false-alarm ratio, wet-area bias; depth bias / RMSE / MAE / 90th percentile error "
                "and depth-class agreement (classes 1, 3, 6 ft) over the cells both models flood; "
                "velocity and arrival the same way.\n"
                "WHICH DAM: the tool-7 results layer (its depth_tif ... fields) or the tool-7 "
                "output folder (<dam>_<scenario>_depth.tif ...). One dam ID, or empty = every dam "
                "that has a reference in the reference folder.\n"
                "REFERENCE: three rasters for one dam, or a folder: <folder>/<dam ID>/ with rasters "
                "named by kind (Depth (Max).tif, Velocity..., Arrival...), or files whose name "
                "holds the dam ID and the kind (DWR-5289_depth.tif). Units: US (ft, ft/s, h) or "
                "metric. The reference is resampled onto the tool-7 grid (nearest neighbour), as "
                "the Runner does. Tool 7's rasters are METRES and are converted to feet here.\n"
                "WET THRESHOLD: a cell counts as flooded from this depth (ft) in both models; "
                "1.0 ft is a screening value you choose (the Runner's default too).\n"
                "OUTPUT per dam: <out>/<dam ID>/Benchmark_Summary.csv, Benchmark_Report.html, "
                "'Benchmark Depth Difference (ft).tif' (this plugin minus the reference, feet) and "
                "'Benchmark Extent Agreement.tif' (1 = only this plugin floods the cell, 2 = only "
                "the reference, 3 = both); with several dams also <out>/Benchmark_All.csv.\n"
                "A difference points at terrain, roughness, breach hydrograph or boundary choices "
                "to look at; it does not say which model is right. Not run in a real QGIS during "
                "development (test doubles only).")

    def initAlgorithm(self, config=None):
        P = self.addParameter
        P(QgsProcessingParameterFeatureSource("SIM", "Simulation results (tool 7; or give the folder below)",
                                              [SRC_POINT], optional=True))
        P(QgsProcessingParameterFile("SIMDIR", "Tool-7 output folder (used when no results layer is given)",
                                     behavior=FILE_FOLDER, optional=True))
        P(QgsProcessingParameterEnum("SCEN", "Scenario of the tool-7 rasters", SCENARIOS, defaultValue=0))
        P(QgsProcessingParameterString("DAM_ID", "Dam ID (empty = every dam with a reference in the folder)",
                                       defaultValue="", optional=True))
        P(QgsProcessingParameterRasterLayer("REF_DEPTH", "Reference: maximum depth raster (one dam)",
                                            optional=True))
        P(QgsProcessingParameterRasterLayer("REF_VEL", "Reference: maximum velocity raster (optional)",
                                            optional=True))
        P(QgsProcessingParameterRasterLayer("REF_ARR", "Reference: arrival time raster (hours; optional)",
                                            optional=True))
        P(QgsProcessingParameterFile("REF_FOLDER", "Reference folder (per-dam sub-folders, or files named "
                                     "by dam ID)", behavior=FILE_FOLDER, optional=True))
        P(QgsProcessingParameterEnum("REF_UNITS", "Reference units", REF_UNITS, defaultValue=0))
        P(QgsProcessingParameterNumber("THRESH_FT", "Wet threshold (ft; a screening value you choose)",
                                       NUM_DOUBLE, 1.0, minValue=0.01))
        P(QgsProcessingParameterString("CLASSES_FT", "Depth class limits (ft, comma-separated)",
                                       defaultValue="1, 3, 6"))
        P(QgsProcessingParameterFolderDestination("OUTDIR", "Benchmark output folder"))

    # ------------------------------------------------------------------ helpers
    def _raster_path(self, params, key, context):
        """Path of an optional raster parameter: the layer's source, else the text given."""
        lyr = None
        try:
            lyr = self.parameterAsRasterLayer(params, key, context)
        except Exception:                               # the test double has no raster layers
            lyr = None
        if lyr is not None and hasattr(lyr, "source"):
            return lyr.source()
        return self.parameterAsString(params, key, context) or None

    @staticmethod
    def _warp(path, gt, shape):
        ds = gdal.Warp("", path, format="MEM", dstSRS=cm.ALBERS,
                       outputBounds=(gt[0], gt[3] + shape[0] * gt[5], gt[0] + shape[1] * gt[1], gt[3]),
                       width=shape[1], height=shape[0], resampleAlg="near", dstNodata=-9999.0,
                       outputType=gdal.GDT_Float32)
        GS.checked(ds, "reading {} on the model grid".format(path))
        a = ds.GetRasterBand(1).ReadAsArray().astype("float64")
        a[a == -9999.0] = np.nan
        return a

    def processAlgorithm(self, params, context, feedback):
        sim = self.parameterAsSource(params, "SIM", context)
        simdir = self.parameterAsString(params, "SIMDIR", context)
        scen = ["td", "sd"][self.parameterAsEnum(params, "SCEN", context)]
        dam_id = (self.parameterAsString(params, "DAM_ID", context) or "").strip()
        ref_folder = self.parameterAsString(params, "REF_FOLDER", context)
        ref_single = {k: self._raster_path(params, p, context)
                      for k, p in (("depth", "REF_DEPTH"), ("velocity", "REF_VEL"), ("arrival", "REF_ARR"))}
        ref_single = {k: v for k, v in ref_single.items() if v}
        units = "us" if self.parameterAsEnum(params, "REF_UNITS", context) == 0 else "metric"
        thr = self.parameterAsDouble(params, "THRESH_FT", context)
        try:
            classes = tuple(sorted(float(x) for x in self.parameterAsString(params, "CLASSES_FT", context)
                                   .replace(";", ",").split(",") if x.strip()))
        except ValueError:
            raise QgsProcessingException("Depth class limits must be numbers (ft), e.g. '1, 3, 6'.")
        if not classes:
            classes = (1.0, 3.0, 6.0)
        outdir = self.parameterAsString(params, "OUTDIR", context)
        if sim is None and not simdir:
            raise QgsProcessingException("Give the tool-7 results layer or the tool-7 output folder.")
        if not ref_single and not ref_folder:
            raise QgsProcessingException("Give the reference depth raster (one dam) or a reference folder.")
        if ref_single and not dam_id and sim is None:
            raise QgsProcessingException("With single reference rasters give the dam ID (or the results layer "
                                         "with one dam).")

        # ---- the tool-7 rasters per dam
        ours = {}                                   # dam_id -> {kind: path}
        if sim is not None:
            names = sim.fields().names()
            for f in sim.getFeatures():
                did = str(cm.fval(f, names, "dam_id") or f.id())
                st = str(cm.fval(f, names, "sim_status") or "")
                p = {k: cm.fval(f, names, fk) for k, fk in (("depth", "depth_tif"), ("velocity", "vel_tif"),
                                                            ("arrival", "arrive_tif"))}
                if not p["depth"] and simdir:
                    p = {k: os.path.join(simdir, f"{did}_{scen}_{s}.tif")
                         for k, s in (("depth", "depth"), ("velocity", "vel"), ("arrival", "arrive"))}
                if p["depth"] and os.path.exists(str(p["depth"])):
                    ours[did] = {k: str(v) for k, v in p.items() if v and os.path.exists(str(v))}
                elif st in ("ok", "cached"):
                    feedback.reportError(f"dam {did}: the tool-7 depth raster {p['depth']} is missing")
        else:
            suffix = f"_{scen}_depth.tif"
            for n in sorted(os.listdir(simdir)):
                if n.endswith(suffix):
                    did = n[:-len(suffix)]
                    b = os.path.join(simdir, f"{did}_{scen}")
                    ours[did] = {k: f"{b}_{s}.tif" for k, s in (("depth", "depth"), ("velocity", "vel"),
                                                                ("arrival", "arrive"))
                                 if os.path.exists(f"{b}_{s}.tif")}
        if dam_id:
            if dam_id not in ours:
                raise QgsProcessingException(f"No tool-7 depth raster for dam {dam_id} (scenario {scen}) in "
                                             f"the results layer / folder {simdir or ''}.")
            todo = [dam_id]
        elif ref_single:
            if len(ours) != 1:
                raise QgsProcessingException("Single reference rasters with a results layer of several dams: "
                                             "give the dam ID.")
            todo = list(ours)
        else:
            todo = bm.dams_with_reference(ref_folder, sorted(ours))
            if not todo:
                raise QgsProcessingException(f"No dam of the tool-7 results has a reference in {ref_folder} "
                                             "(a sub-folder named by the dam ID with a depth raster, or a file "
                                             "whose name holds the dam ID and 'depth').")
        feedback.pushInfo(f"{len(todo)} dam(s) to compare; wet threshold {thr:g} ft; reference units {units}; "
                          f"depth classes {', '.join(f'{c:g}' for c in classes)} ft")
        os.makedirs(outdir, exist_ok=True)

        rows, results = [], {}
        for i, did in enumerate(todo):
            if feedback.isCanceled():
                break
            feedback.setProgress(100.0 * i / max(len(todo), 1))
            ref = ref_single if ref_single else bm.find_reference(ref_folder, did)
            if not ref:
                feedback.reportError(f"dam {did}: no reference depth raster found; skipped")
                continue
            try:
                depth_m, gt = cm.read_tif(ours[did]["depth"])
                cell = abs(gt[1])
                mine = {"depth": depth_m}
                for k in ("velocity", "arrival"):
                    if k in ours[did]:
                        mine[k], _ = cm.read_tif(ours[did][k])
                mine = bm.to_us(mine, "metric", nodata=gu.NODATA)
                theirs = {k: self._warp(p, gt, depth_m.shape) for k, p in ref.items()}
                theirs = bm.to_us(theirs, units)
                kinds = [k for k in ("velocity", "arrival") if k in mine and k in theirs]
                metrics = bm.compare_fields(mine, theirs, cell * cell / M2_PER_AC, thr, classes)
                metrics["dam_id"] = did
                metrics["scenario"] = scen
                metrics["cell_m"] = cell
                metrics["compared"] = "depth" + "".join(", " + k for k in kinds)
                arrays = bm.difference_arrays(mine, theirs, thr)
                ddir = os.path.join(outdir, did)
                os.makedirs(ddir, exist_ok=True)
                paths = []
                for name, a in arrays.items():
                    p = os.path.join(ddir, name)
                    cm.write_tif(p, np.where(np.isfinite(a), a, gu.NODATA), gt, nodata=gu.NODATA)
                    paths.append(p)
                bm.write_summary_csv(os.path.join(ddir, bm.SUMMARY_CSV), metrics)
                inputs = [(f"tool 7 {k} (m -> ft)" if k != "arrival" else "tool 7 arrival (h)", p)
                          for k, p in ours[did].items()]
                inputs += [(f"reference {k} ({units})", p) for k, p in ref.items()]
                with open(os.path.join(ddir, bm.REPORT_HTML), "w", encoding="utf-8") as fh:
                    fh.write(bm.report_html(metrics, "Benchmark against a reference model", did, inputs,
                                            paths, thr))
                rows.append(bm.all_row(did, metrics, scen, ref["depth"]))
                results[did] = metrics
                feedback.pushInfo(f"{did}: {metrics['verdict']}")
            except Exception as e:                   # keep the batch going
                feedback.reportError(f"dam {did}: {e}")
        out = {"OUTDIR": outdir, "N": len(rows)}
        if len(rows) > 1:
            out["ALL_CSV"] = bm.write_all_csv(os.path.join(outdir, bm.ALL_CSV), rows)
        if rows:
            out["F_MIN"] = min(r["fit_index_F"] for r in rows)
        feedback.pushInfo(f"compared {len(rows)} dam(s); outputs in {outdir}")
        return out
