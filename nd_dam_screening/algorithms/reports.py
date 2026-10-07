"""Per-dam HTML reports and the statewide summary / review queue."""
import os

import numpy as np
from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterFile, QgsProcessingParameterEnum,
                       QgsProcessingParameterFolderDestination, QgsProcessingOutputFile)

from ..core import report as rp, gridutil as gu, solver_io as sio
from . import _common as cm
from ..compat import (FILE_FOLDER, SRC_POINT)

FIELDS = ["dam_id", "name", "hz_class", "hz_reason", "hz_conf", "haz_existing", "hz_vs_existing",
          "n_bldg_wet", "n_bldg_lethal", "n_road_wet", "n_rail_wet", "height_m", "store_af",
          "td_qp_m3s", "td_tf_min", "sim_engine", "sim_cell_m", "sim_h", "max_depth_m", "area_ha",
          "edge_flag", "dir_conf", "hz_notes", "sim_notes", "brch_notes", "cascade", "audit",
          "sim_status", "tri_result"]


class Reports(QgsProcessingAlgorithm):
    def name(self): return "reports"
    def displayName(self): return "10 - Reports and statewide summary"
    def group(self): return "Dam screening"
    def groupId(self): return "dams"
    def createInstance(self): return Reports()

    def shortHelpString(self):
        return ("Writes index.html (counts by class, the review queue and a sortable, filterable "
                "table of every dam), summary.csv, and one page per dam in dams/ with a maximum-depth "
                "map over hillshade, the impacted features, the breach hydrograph and the notes. "
                "Pages are self-contained (open offline, print to PDF). The review queue lists dams "
                "whose screened class differs from DWR's, low confidence, truncated floods, uncertain "
                "breach direction, unstable class in tool 9, cascade overtopping, triage audit "
                "mismatches and failures.")

    def initAlgorithm(self, config=None):
        P = self.addParameter
        P(QgsProcessingParameterFeatureSource("HAZ", "Hazard classification (tool 8)", [SRC_POINT]))
        P(QgsProcessingParameterFeatureSource("IMP", "Impacted features (tool 8, optional)",
                                              [SRC_POINT], optional=True))
        P(QgsProcessingParameterFeatureSource("SENS", "Sensitivity result (tool 9, optional)",
                                              [SRC_POINT], optional=True))
        P(QgsProcessingParameterFeatureSource("BPTS", "Breach points (tool 4, optional)",
                                              [SRC_POINT], optional=True))
        P(QgsProcessingParameterFile("DEMDIR", "Folder of cached dam DEMs (for the hillshade, optional)",
                                     behavior=FILE_FOLDER, optional=True))
        P(QgsProcessingParameterEnum("WHICH", "Per-dam pages for", [
            "Medium and High dams plus the review queue", "every dam with a flood result"], defaultValue=0))
        P(QgsProcessingParameterFolderDestination("OUTDIR", "Report folder"))
        self.addOutput(QgsProcessingOutputFile("INDEX", "Summary page"))

    def processAlgorithm(self, params, context, feedback):
        haz = self.parameterAsSource(params, "HAZ", context)
        imp = self.parameterAsSource(params, "IMP", context)
        sens = self.parameterAsSource(params, "SENS", context)
        bpts = self.parameterAsSource(params, "BPTS", context)
        demdir = self.parameterAsString(params, "DEMDIR", context)
        every = self.parameterAsEnum(params, "WHICH", context) == 1
        outdir = self.parameterAsString(params, "OUTDIR", context)
        os.makedirs(os.path.join(outdir, "dams"), exist_ok=True)

        stable = {}
        if sens is not None:
            sn = sens.fields().names()
            for f in sens.getFeatures():
                stable[str(f["dam_id"])] = cm.fval(f, sn, "class_stable")
        impacts = {}
        if imp is not None:
            ta = cm.to_albers(imp)
            for f in imp.getFeatures():
                g = f.geometry(); g.transform(ta); p = g.asPoint()
                impacts.setdefault(str(f["dam_id"]), []).append(
                    {"kind": f["kind"], "depth_m": f["depth_m"], "vel_ms": f["vel_ms"],
                     "dv_m2s": f["dv_m2s"] if "dv_m2s" in imp.fields().names() else None,
                     "lethal": f["lethal"], "level": f["level"] if "level" in imp.fields().names() else "",
                     "xy": (p.x(), p.y())})
        breach = {}
        if bpts is not None:
            ta = cm.to_albers(bpts)
            for f in bpts.getFeatures():
                g = f.geometry(); g.transform(ta); p = g.asPoint()
                breach[str(f["dam_id"])] = (p.x(), p.y())

        names = haz.fields().names()
        recs = []
        total = haz.featureCount() or 1
        for i, f in enumerate(haz.getFeatures()):
            if feedback.isCanceled(): break
            feedback.setProgress(100.0 * i / total)
            r = {k: cm.fval(f, names, k) for k in FIELDS}
            r["dam_id"] = str(r["dam_id"] or f.id())
            r["qp_m3s"], r["tf_min"] = r.pop("td_qp_m3s"), r.pop("td_tf_min")
            r["engine"], r["cell_m"] = r.pop("sim_engine"), r.pop("sim_cell_m")
            r["status_sim"] = r.pop("sim_status")
            r["class_stable"] = stable.get(r["dam_id"])
            recs.append(r)
            want = every or r.get("hz_class") in ("Medium", "High") or rp.review_reasons(r)
            dtif = cm.fval(f, names, "depth_tif")
            if not want or not dtif or not os.path.exists(str(dtif)):
                continue
            try:
                depth, gt = cm.read_tif(str(dtif))
                depth = np.where(depth > gu.NODATA + 1, depth, 0.0)
                hill = np.full(depth.shape, 0.6)
                demp = os.path.join(demdir, f"dam_{r['dam_id']}.tif") if demdir else None
                if demp and os.path.exists(demp):
                    dem = cm.warp_to_grid(demp, gt, depth.shape, "bilinear")
                    dem[dem < -1000] = np.nan
                    hill = gu.hillshade(np.where(np.isnan(dem), gu.NODATA, dem), abs(gt[1]))
                feats = []
                for it in impacts.get(r["dam_id"], []):
                    rr, cc = gu.xy_to_rc(gt, *it["xy"])
                    if gu.inside(depth.shape, rr, cc):
                        kind = "lethal" if it["lethal"] == "YES" else (
                            "structure" if it["kind"] in ("structure", "mobile home") else it["kind"])
                        feats.append((rr, cc, kind))
                brc = gu.xy_to_rc(gt, *breach[r["dam_id"]]) if r["dam_id"] in breach else None
                png = rp.png_bytes(rp.flood_map(depth, hill, feats, brc))
                hyd = None
                csvp = cm.fval(f, names, "td_csv")
                if csvp and os.path.exists(str(csvp)):
                    hyd = sio.read_hydrograph_csv(str(csvp))
                rp.dam_page(r, png, hyd, impacts.get(r["dam_id"], []),
                            os.path.join(outdir, "dams", f"{r['dam_id']}.html"))
            except Exception as e:  # a bad page must not stop the report
                feedback.reportError(f"dam {r['dam_id']}: report page failed: {e}")
        idx, csvp = rp.summary(recs, outdir)
        feedback.pushInfo(f"summary: {idx}")
        return {"OUTDIR": outdir, "INDEX": idx}
