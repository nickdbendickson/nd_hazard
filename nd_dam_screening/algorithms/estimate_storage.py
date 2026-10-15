"""Estimate reservoir storage per dam from cached DEM + NHD HR waterbodies."""
import os

import numpy as np
from osgeo import gdal, ogr, osr
from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterFolderDestination, QgsProcessingParameterFile,
                       QgsProcessingParameterNumber, QgsProcessingParameterFeatureSink,
                       QgsFields, QgsField, QgsFeature,
                       QgsCoordinateTransform, QgsCoordinateReferenceSystem, QgsProject,
                       QgsFeatureRequest, QgsRectangle)

from ..core import storage
from ..compat import (FILE_FOLDER, FT_DOUBLE, FT_STRING, NUM_DOUBLE, SRC_POINT, SRC_POLYGON)
from . import _common as cm
from ..core import errors as er
from ..damsuite import gdalsafe as GS

NEW_FIELDS = [("est_pool_m", FT_DOUBLE), ("est_crest_m", FT_DOUBLE),
              ("est_area_m2", FT_DOUBLE), ("est_below_m3", FT_DOUBLE),
              ("est_above_m3", FT_DOUBLE), ("est_total_m3", FT_DOUBLE),
              ("est_maxdep_m", FT_DOUBLE), ("shore_deg", FT_DOUBLE),
              ("stor_used_m3", FT_DOUBLE), ("stor_method", FT_STRING),
              ("stor_conf", FT_STRING), ("inv_vs_est", FT_DOUBLE),
              ("stor_notes", FT_STRING),
              ("below_scale", FT_DOUBLE), ("cal_total_m3", FT_DOUBLE),
              # 0.19.0: the DEM height estimate and the spill level of the leak guard
              ("est_h_m", FT_DOUBLE), ("spill_m", FT_DOUBLE)]

# 0.28.3: a basin found in the DEM shallower than this fraction of the dam height is not the
# reservoir of the dam (see processAlgorithm)
SHALLOW_BASIN = 0.5


class EstimateStorage(QgsProcessingAlgorithm):
    DAMS, WB, DEMDIR, SEARCH, SHAPE, OUT, CURVES = ("DAMS", "WB", "DEMDIR", "SEARCH",
                                                    "SHAPE", "OUT", "CURVES")

    def name(self): return "estimatestorage"
    def displayName(self): return "3 - Estimate reservoir storage"
    def group(self): return "Dam screening"
    def groupId(self): return "dams"
    def createInstance(self): return EstimateStorage()

    # 0.21.0: opened from the Processing Toolbox (no work folder), the dialog starts with the
    # last Standardized dams written (only the dialog: a run given no layer still stops)
    def createCustomParametersWidget(self, parent=None):
        return cm.dialog_with_last(self, parent, self.DAMS, "dams")

    def shortHelpString(self):
        return ("For each dam, finds the nearest NHD HR waterbody, reads the pool elevation "
                "from the hydro-flattened DEM, measures storage between pool and crest "
                "directly from the DEM, and ESTIMATES storage below pool by projecting the "
                "shoreline slope inward (capped by dam height). The below-pool part is the "
                "uncertain one: on a synthetic rounded pond it overestimated by ~40%. If the "
                "dam has inventory normal storage (DWR or NID), that value is used "
                "(stor_method = inventory): it calibrates the below-pool part of the curve "
                "(below_scale) while the above-pool part stays DEM-measured. inv_vs_est is "
                "inventory normal storage / estimated storage at normal pool. Use the shape factor to "
                "calibrate against dams with known storage.\n"
                "0.19.0: (1) storage leak guard - the curve stops at the level where the pool spills "
                "out of the reservoir (the flooded area reaches the clip edge or jumps in one step); "
                "(2) an inventory MAXIMUM storage holds the curve where it reaches it (when the DEM "
                "curve is more than 1.5 times the maximum) or, with no normal storage, scales it to "
                "the maximum (stor_method = inventory max); (3) a structure with no height gets one "
                "from the DEM (crest minus the lowest ground near the point): height_m is filled and "
                "h_src = 'estimated (DEM)'; with no inventory storage stor_src = 'estimated (DEM)'. "
                "Estimated values carry low confidence and are named in the reports.\n"
                "0.26.0: a dam with no basin at all in the DEM (no mapped water body, no flat water, "
                "no closed depression within the search distance) but an inventory storage (maximum, "
                "else normal) and a height no longer fails: a quadratic stage-storage curve from the "
                "ground at the dam point to ground + height holding the inventory volume is ASSUMED "
                "(stor_method = inventory curve, stor_src = 'inventory (assumed curve)', low confidence, "
                "a note starting with ASSUMPTION, a warning in the log). Verify the location and the "
                "DWR values of such dams. Without a storage or a height the dam still fails.\n"
                "0.28.3: the same assumed curve is used when the basin FOUND in the DEM (flat water "
                "or a closed depression, not a mapped water body) is less than half the dam height "
                "deep: such a basin is a puddle on the floor of a dry basin, and stretching its "
                "curve to the inventory maximum put almost all of the storage in the top few feet "
                "of the dam. THIS MOVES THE PEAK BREACH FLOW of dry dams screened before 0.28.3.")

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.DAMS, "Standardized dams", [SRC_POINT]))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.WB, "NHD HR waterbodies (polygons)", [SRC_POLYGON]))
        self.addParameter(QgsProcessingParameterFile(
            self.DEMDIR, "Folder of cached dam DEMs", behavior=FILE_FOLDER))
        cm.distance_params(self.addParameter, "SEARCH_FT", self.SEARCH,
                           "Waterbody search distance from dam point (ft)", 500.0, min_ft=30.0)
        self.addParameter(QgsProcessingParameterNumber(
            self.SHAPE, "Below-pool shape factor (calibration)",
            NUM_DOUBLE, 1.0, minValue=0.1, maxValue=1.5))
        self.addParameter(QgsProcessingParameterFolderDestination(
            self.CURVES, "Folder for stage-storage curves (CSV)"))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT, "Dams with storage", SRC_POINT))

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _rasterize(geoms_wkb, ds):
        gt = ds.GetGeoTransform()
        mem = GS.checked(gdal.GetDriverByName("MEM").Create("", ds.RasterXSize, ds.RasterYSize, 1, gdal.GDT_Byte),
                         "making the water-body mask")
        mem.SetGeoTransform(gt); mem.SetProjection(ds.GetProjection())
        srs = osr.SpatialReference(); srs.ImportFromEPSG(5070)
        vds = (ogr.GetDriverByName("MEM") or ogr.GetDriverByName("Memory")).CreateDataSource("wb")
        lyr = vds.CreateLayer("wb", srs, ogr.wkbPolygon)
        for w in geoms_wkb:
            ft = ogr.Feature(lyr.GetLayerDefn()); ft.SetGeometry(ogr.CreateGeometryFromWkb(w))
            lyr.CreateFeature(ft)
        gdal.RasterizeLayer(mem, [1], lyr, burn_values=[1])
        return mem.ReadAsArray().astype(bool)

    @staticmethod
    def _inventory_curve(f, names, arr, rc, why=storage.NO_BASIN):
        """0.26.0: the last-resort curve of a dam with no basin in the DEM: (StorageResult, volume
        used) from the inventory maximum storage (else the normal) and the height, or None when the
        record lacks either (height_m must be over 0.5 m) or the DEM has no ground at the point."""
        def num(k):
            v = f[k] if k in names else None
            try:
                v = float(v) if v not in (None, "") else None
            except (TypeError, ValueError):
                v = None
            return v if v is not None and np.isfinite(v) else None
        vmax, vnorm, h = num("inv_max_m3"), num("inv_norm_m3"), num("height_m")
        vol, which = (vmax, "maximum") if vmax and vmax > 0 else (vnorm, "normal")
        if not vol or vol <= 0 or h is None or h <= 0.5:
            return None
        res = storage.inventory_curve(arr, rc, h, vol, which, why)
        return (res, vol) if res is not None else None

    def processAlgorithm(self, params, context, feedback):
        dams = self.parameterAsSource(params, self.DAMS, context)
        wbs = self.parameterAsSource(params, self.WB, context)
        demdir = self.parameterAsString(params, self.DEMDIR, context)
        search = cm.distance_m(self, params, context, "SEARCH_FT", self.SEARCH, 500.0)
        shape = self.parameterAsDouble(params, self.SHAPE, context)
        curves = self.parameterAsString(params, self.CURVES, context)
        os.makedirs(curves, exist_ok=True)

        fields = QgsFields(dams.fields())
        for n, t in NEW_FIELDS: fields.append(QgsField(n, t))
        sink, dest = cm.us_sink(self, params, self.OUT, context, fields,
                                          dams.wkbType(), dams.sourceCrs())
        alb = QgsCoordinateReferenceSystem("EPSG:5070")
        d2a = QgsCoordinateTransform(dams.sourceCrs(), alb, QgsProject.instance())
        a2w = QgsCoordinateTransform(alb, wbs.sourceCrs(), QgsProject.instance())
        w2a = QgsCoordinateTransform(wbs.sourceCrs(), alb, QgsProject.instance())
        names = dams.fields().names()
        total = dams.featureCount() or 1

        for i, f in enumerate(dams.getFeatures()):
            if feedback.isCanceled(): break
            feedback.setProgress(100 * i / total)
            dam_id = str(f["dam_id"]) if "dam_id" in names else str(f.id())
            out = QgsFeature(fields); out.setGeometry(f.geometry())
            vals = list(f.attributes()) + [None] * len(NEW_FIELDS)
            res, note = None, ""
            assumed = None                   # 0.26.0: (result, volume) of the last-resort inventory curve
            try:
                dem_path = os.path.join(demdir, f"dam_{dam_id}.tif")
                if not os.path.exists(dem_path):
                    raise FileNotFoundError("no cached DEM")
                ds = cm.gdal_open(dem_path)
                arr = ds.GetRasterBand(1).ReadAsArray().astype("float64")
                nd = ds.GetRasterBand(1).GetNoDataValue()
                if nd is not None: arr[arr == nd] = np.nan
                gt = ds.GetGeoTransform(); cell = abs(gt[1])

                g = f.geometry(); g.transform(d2a); p = g.asPoint()
                box = QgsRectangle(p.x() - search, p.y() - search, p.x() + search, p.y() + search)
                box_w = a2w.transformBoundingBox(box)
                cands = []
                for w in wbs.getFeatures(QgsFeatureRequest().setFilterRect(box_w)):
                    wg = w.geometry(); wg.transform(w2a)
                    dist = wg.distance(g)
                    if dist <= search: cands.append((dist, -wg.area(), wg))
                col = int((p.x() - gt[0]) / gt[1]); row = int((p.y() - gt[3]) / gt[5])
                wnote = ""
                if cands:
                    cands.sort(key=lambda c: (round(c[0]), c[1]))
                    mask = self._rasterize([cands[0][2].asWkb()], ds)
                else:                        # 0.19.0: the hydro-flattened water in the DEM
                    mask, wnote = storage.flat_water_mask(arr, cell, (row, col), search)
                    if mask is None:         # a dry pond: the closed basin in the terrain
                        mask, dnote = storage.depression_mask(arr, cell, (row, col), search)
                        if mask is None:
                            # 0.26.0: no basin at all - an inventory storage and a height give an
                            # assumed quadratic curve instead of a FAILED dam
                            assumed = self._inventory_curve(f, names, arr, (row, col))
                            if assumed is None:
                                raise LookupError("no waterbody within search distance ({}; {})".format(wnote, dnote))
                            wnote = "{}; {}".format(wnote, dnote)
                        else:
                            wnote = dnote
                h = f["height_m"] if "height_m" in names else None
                h = float(h) if h not in (None, "") else None
                if assumed is not None:
                    res = assumed[0]
                    note = "{} (DEM: {})".format(res.notes[0], wnote)
                    feedback.pushWarning("dam {}: {}".format(dam_id, res.notes[0]))
                else:
                    res = storage.estimate_storage(arr, mask, cell, (row, col),
                                                   known_height_m=h, shape_factor=shape)
                    note = "; ".join(([wnote] if wnote else []) + res.notes)
                    if wnote:
                        res.confidence = "low"
                    # 0.28.3: a basin taken from the DEM (flat water or a closed depression, not
                    # a mapped water body) that is far shallower than the dam is not the
                    # reservoir: it is a puddle on the floor of a dry basin. Stretching its
                    # curve to the inventory maximum below put nearly all the volume in the top
                    # few feet, which handed the Runner a storage-area exponent at its cap. The
                    # full-height inventory curve is used instead, when the record allows it.
                    span = res.crest_elev - res.pool_elev
                    if wnote and h and span < SHALLOW_BASIN * h:
                        alt = self._inventory_curve(f, names, arr, (row, col),
                                                    storage.SHALLOW_BASIN_WHY.format(
                                                        span / cm.FT, h / cm.FT))
                        if alt is not None:
                            assumed, res = alt, alt[0]
                            note = "{} (DEM: {})".format(res.notes[0], wnote)
                            feedback.pushWarning("dam {}: {}".format(dam_id, res.notes[0]))
            except Exception as e:  # keep going, record why
                note = "FAILED: " + er.plain(e, dam_id, "3")
                feedback.reportError("dam {}: {}".format(dam_id, er.plain(e, dam_id, "3")))

            k = len(f.attributes())
            if res is not None:
                inv = f["inv_norm_m3"] if "inv_norm_m3" in names else None
                inv = float(inv) if inv not in (None, "") else None
                invmax = f["inv_max_m3"] if "inv_max_m3" in names else None
                invmax = float(invmax) if invmax not in (None, "") and float(invmax) > 0 else None
                curve, scale, cnote = storage.calibrate_below_pool(
                    res.curve, res.below_pool_m3, inv)
                if cnote: note = (note + "; " if note else "") + cnote
                crest_used = res.crest_elev
                method = None
                if invmax:
                    # 0.19.0: DWR's maximum storage is the volume at the top of dam (as the Runner
                    # uses it): the DEM curve is stretched to it when it falls short (conservative)
                    # or runs past 1.5x (the DEM fill left the reservoir)
                    top = curve[-1][1]
                    ratio = top / invmax
                    if ratio < 1.0 or ratio > 1.5:
                        curve, _k = storage.set_top(curve, invmax)
                        note += "; DWR maximum storage {:,.1f} ac-ft used at the top of dam (DEM curve " \
                                "{:,.1f} ac-ft there)".format(invmax / 1233.48183754752, top / 1233.48183754752)
                        method = "inventory max"
                        if ratio < 1.0 / 3.0 or ratio > 3.0:
                            note += "; CHECK: the DEM and DWR storage disagree (DEM x{:.2f} of DWR): verify " \
                                    "the DWR value and the location".format(ratio)
                            res.confidence = "low"
                cal_total = curve[-1][1]
                if inv:
                    used, method, conf = inv, "inventory", "low" if "CHECK: the DEM and DWR" in note else "high"
                elif method == "inventory max":
                    used, conf = invmax, "low" if "CHECK: the DEM and DWR" in note else "medium"
                else:
                    used, method, conf = cal_total, "estimated", "low" if \
                        res.confidence == "low" else res.confidence
                # 0.19.0: no inventory height -> estimate one from the DEM
                h_est = None
                if h is None:
                    h_est, hnote = storage.estimate_height(arr, mask, cell, (row, col), crest_used)
                    note += "; " + hnote
                    if h_est is not None:
                        if "height_m" in names:
                            vals[names.index("height_m")] = h_est
                        if "h_src" in names:
                            vals[names.index("h_src")] = "estimated (DEM)"
                        conf = "low"
                if method == "estimated" and "stor_src" in names:
                    vals[names.index("stor_src")] = "estimated (DEM)"
                    conf = "low"
                elif method == "inventory max" and "stor_src" in names and not vals[names.index("stor_src")]:
                    vals[names.index("stor_src")] = "inventory"
                if assumed is not None:
                    # 0.26.0: the assumed curve already holds the inventory volume at its top (no
                    # rescale note above); it is an assumption, whatever the inventory fields say
                    used, method, conf = assumed[1], "inventory curve", "low"
                    if "stor_src" in names:
                        vals[names.index("stor_src")] = "inventory (assumed curve)"
                spill = next((float(x.split("spill level ")[1].split(" m")[0]) for x in res.notes
                              if "spill level " in x), None)
                vals[k:] = [res.pool_elev, crest_used, res.surface_area_m2, res.below_pool_m3,
                            res.above_pool_m3, res.total_m3, res.max_depth_m, res.shore_slope_deg,
                            used, method, conf,
                            (inv / res.below_pool_m3) if inv and res.below_pool_m3 else None,
                            note.strip("; "), scale, cal_total, h_est, spill]
                with open(os.path.join(curves, f"curve_{dam_id}.csv"), "w") as fh:
                    fh.write("elev_m,storage_m3,elev_ft,storage_acft\n")     # calibrated curve
                    for e, v in curve: fh.write(f"{e:.3f},{v:.1f},{e / 0.3048:.2f},{v / 1233.48183754752:.3f}\n")
            else:
                vals[k + 9] = "none"; vals[k + 10] = "none"; vals[k + 12] = note
            out.setAttributes(vals)
            sink.addFeature(out)
        cm.remember_output("dams3", dest)               # 0.21.0: tool 4 starts with it
        return {self.OUT: dest}
