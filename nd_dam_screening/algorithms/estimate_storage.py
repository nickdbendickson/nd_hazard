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


class EstimateStorage(QgsProcessingAlgorithm):
    DAMS, WB, DEMDIR, SEARCH, SHAPE, OUT, CURVES = ("DAMS", "WB", "DEMDIR", "SEARCH",
                                                    "SHAPE", "OUT", "CURVES")

    def name(self): return "estimatestorage"
    def displayName(self): return "3 - Estimate reservoir storage"
    def group(self): return "Dam screening"
    def groupId(self): return "dams"
    def createInstance(self): return EstimateStorage()

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
                "Estimated values carry low confidence and are named in the reports.")

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
                            raise LookupError("no waterbody within search distance ({}; {})".format(wnote, dnote))
                        wnote = dnote
                h = f["height_m"] if "height_m" in names else None
                h = float(h) if h not in (None, "") else None
                res = storage.estimate_storage(arr, mask, cell, (row, col),
                                               known_height_m=h, shape_factor=shape)
                note = "; ".join(([wnote] if wnote else []) + res.notes)
                if wnote:
                    res.confidence = "low"
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
        return {self.OUT: dest}
