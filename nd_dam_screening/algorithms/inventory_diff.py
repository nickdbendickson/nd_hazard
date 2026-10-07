"""13 - Inventory diff: what changed between two dam inventories (tool-1 layers or DWR exports)."""
import csv
import datetime
import os

from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterFile, QgsProcessingParameterNumber,
                       QgsProcessingParameterEnum, QgsProcessingParameterFileDestination,
                       QgsProcessingParameterFeatureSink, QgsFields, QgsField,
                       QgsFeature, QgsGeometry, QgsPointXY)

from ..core import invdiff as idf, inventory as inv
from . import _common as cm
from ..compat import (FILE_FOLDER, FT_INT, FT_STRING, NUM_DOUBLE, SRC_POINT, WKB_POINT)

OUT_FIELDS = [("dam_id", FT_STRING), ("name", FT_STRING), ("change", FT_STRING),
              ("n_changes", FT_INT), ("fields", FT_STRING), ("detail", FT_STRING),
              ("dam_json", FT_STRING)]
UNITS = ["Feet / acre-feet / acres (DWR export)", "Metres / m3 / m2"]
TOOL, VERSION = "ND Dam Breach Screening tool 13", "0.11"


def read_inventory(src, metric=False, tag="DWR"):
    """{dam_id: record} of a tool-1 layer (has dam_id) or a raw DWR export (tool 1's field
    defaults; ids become DWR-<structure_>). x / y in EPSG:5070."""
    names = src.fields().names()
    tr = cm.to_albers(src)
    out = {}
    standardized = "dam_id" in names
    for i, f in enumerate(src.getFeatures()):
        g = f.geometry()
        xy = None
        if g is not None and not g.isEmpty():
            g.transform(tr)
            xy = cm.rep_point(g)
        if standardized:
            rec = {k: cm.fval(f, names, k) for k in ("dam_id", "name", "height_m", "inv_norm_m3", "inv_max_m3",
                                                   "status", "haz_existing")}
            rec["dam_id"] = str(rec["dam_id"] if rec["dam_id"] is not None else f.id())
        else:
            fld = {k: (v if v in names else None) for k, v in inv.DWR_DEFAULTS.items()}
            a = inv.standardize(lambda n, _f=f: _f[n], fld, metric, tag, i)
            rec = {"dam_id": f"{tag}-{a['id']}", "name": a["name"], "height_m": a["h"], "inv_norm_m3": a["ns"],
                   "inv_max_m3": a["ms"], "status": a["status"], "haz_existing": a["hz"]}
        rec["x"], rec["y"] = (xy if xy else (None, None))
        out[rec["dam_id"]] = rec
    return out, standardized


class InventoryDiff(QgsProcessingAlgorithm):
    def name(self): return "inventorydiff"
    def displayName(self): return "13 - Inventory diff (what changed since the last run)"
    def group(self): return "Dam screening"
    def groupId(self): return "dams"
    def createInstance(self): return InventoryDiff()

    def shortHelpString(self):
        return ("Compares two dam inventories by dam ID: two tool-1 layers, or a new DWR 'Dams by "
                "Hazard Class' export against the last one (tool 1's default field names: structure_, "
                "dam_name, max_dam_he, normal_vol, max_pool_v, hazard_cla, status; a layer with a "
                "dam_id field is taken as a tool-1 layer). Reports new dams, removed dams, and dams "
                "whose height, normal / max storage, status, existing class, name or location "
                "changed.\n"
                f"Thresholds are screening values you choose: height from {idf.HEIGHT_TOL_FT:g} ft, "
                f"storage from {idf.STORAGE_TOL_PCT:g} % (and at least {idf.STORAGE_TOL_ACFT:g} ac-ft), "
                f"location moved more than {idf.MOVE_M / 0.3048:,.0f} ft.\n"
                "Output: inventory_changes.csv (one row per new / removed dam and per changed field, "
                "values in ft / ac-ft next to the metric ones) and a point layer of the changed dams "
                "(the new location; the old one for removed dams). With a Dams folder, every changed "
                "dam that has <Dams>/<dam ID>/dam.json gets an 'inventory_change' entry ({when, "
                "fields, note}) so the suite's 'next action' can say 'refresh'; nothing else in the "
                "record is touched and no folder is created. Not run in a real QGIS during "
                "development (test doubles only).")

    def initAlgorithm(self, config=None):
        P = self.addParameter
        P(QgsProcessingParameterFeatureSource("OLD", "Old inventory (tool-1 layer or DWR export)",
                                              [SRC_POINT]))
        P(QgsProcessingParameterFeatureSource("NEW", "New inventory (tool-1 layer or DWR export)",
                                              [SRC_POINT]))
        P(QgsProcessingParameterEnum("UNITS", "Units of a raw DWR export (ignored for tool-1 layers)", UNITS,
                                     defaultValue=0))
        P(QgsProcessingParameterNumber("HTOL_FT", "Height: report a change from (ft)",
                                       NUM_DOUBLE, idf.HEIGHT_TOL_FT, minValue=0.0))
        P(QgsProcessingParameterNumber("STOL_PCT", "Storage: report a change from (%)",
                                       NUM_DOUBLE, idf.STORAGE_TOL_PCT, minValue=0.0))
        P(QgsProcessingParameterNumber("STOL_AF", "Storage: ... and at least (ac-ft)",
                                       NUM_DOUBLE, idf.STORAGE_TOL_ACFT, minValue=0.0))
        cm.distance_params(P, "MOVE_FT", "MOVE_M", "Location: report a move of more than (ft)",
                           round(idf.MOVE_M / 0.3048), min_ft=0.0)
        P(QgsProcessingParameterFile("DAMS_FOLDER", "Dams folder: note the change in each changed dam's dam.json "
                                     "(optional)", behavior=FILE_FOLDER, optional=True))
        P(QgsProcessingParameterFileDestination("OUT_CSV", "inventory_changes.csv", fileFilter="CSV (*.csv)"))
        P(QgsProcessingParameterFeatureSink("OUT", "Changed dams", SRC_POINT))

    def processAlgorithm(self, params, context, feedback):
        old_src = self.parameterAsSource(params, "OLD", context)
        new_src = self.parameterAsSource(params, "NEW", context)
        metric = self.parameterAsEnum(params, "UNITS", context) == 1
        tol = dict(height_tol_ft=self.parameterAsDouble(params, "HTOL_FT", context),
                   storage_tol_pct=self.parameterAsDouble(params, "STOL_PCT", context),
                   storage_tol_acft=self.parameterAsDouble(params, "STOL_AF", context),
                   move_m=cm.distance_m(self, params, context, "MOVE_FT", "MOVE_M", round(idf.MOVE_M / 0.3048)))
        dams_folder = self.parameterAsString(params, "DAMS_FOLDER", context)
        out_csv = self.parameterAsFileOutput(params, "OUT_CSV", context)

        old, old_std = read_inventory(old_src, metric)
        new, new_std = read_inventory(new_src, metric)
        feedback.pushInfo(f"old inventory: {len(old)} dams ({'tool-1 layer' if old_std else 'DWR export, tool-1 field defaults'}); "
                          f"new inventory: {len(new)} dams ({'tool-1 layer' if new_std else 'DWR export, tool-1 field defaults'})")
        rows = idf.diff_inventories(old, new, **tol)
        s = idf.summary(rows)
        os.makedirs(os.path.dirname(out_csv) or ".", exist_ok=True)
        with open(out_csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=idf.CSV_COLUMNS)
            w.writeheader()
            for r in rows:
                w.writerow(r)

        fields = QgsFields()
        for n, t in OUT_FIELDS:
            fields.append(QgsField(n, t))
        sink, dest = self.parameterAsSink(params, "OUT", context, fields, WKB_POINT, cm.albers())
        when = datetime.datetime.now().isoformat(timespec="seconds")
        n_json = 0
        by_dam = {}
        for r in rows:
            by_dam.setdefault(r["dam_id"], []).append(r)
        for did, rs in by_dam.items():
            kind = rs[0]["change"]
            rec = (new if did in new else old)[did]
            jpath = ""
            if dams_folder:
                folder = os.path.join(dams_folder, did)
                if os.path.isdir(folder):
                    try:
                        from ..damsuite import dam_record as dr
                        dj = dr.load(folder)
                        if dj is None:
                            feedback.pushInfo(f"{did}: folder without dam.json; nothing written")
                        else:
                            dj.extra["inventory_change"] = idf.record_change(rs, when)
                            jpath = dj.save()
                            n_json += 1
                    except (ValueError, OSError) as e:
                        feedback.reportError(f"{did}: dam.json not updated: {e}")
            o = QgsFeature(fields)
            if rec.get("x") is not None:
                o.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(rec["x"], rec["y"])))
            o.setAttributes([did, rec.get("name") or "", kind, len(rs) if kind == "changed" else 1,
                             ", ".join(r["field"] for r in rs if r["field"]) if kind == "changed" else kind,
                             "; ".join(r["note"] for r in rs)[:254], jpath])
            sink.addFeature(o)
        feedback.pushInfo(f"{s['dams']} dams differ: {s['new']} new, {s['removed']} removed, {s['changed']} changed"
                          + (" (" + ", ".join(f"{k} {v}" for k, v in sorted(s["by_field"].items())) + ")"
                             if s["by_field"] else "")
                          + (f"; dam.json updated for {n_json} dam(s)" if dams_folder else ""))
        return {"OUT": dest, "OUT_CSV": out_csv, "N_NEW": s["new"], "N_REMOVED": s["removed"],
                "N_CHANGED": s["changed"], "N_DAMJSON": n_json}
