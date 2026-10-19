"""16 - Find ponds and dugouts: water-body polygons + LiDAR -> dams the other tools can screen.

For every water body of a layer (NHD waterbodies, NWI wetlands or your own polygons) the LiDAR
terrain decides whether the water is HELD ABOVE the ground by an embankment (an embankment pond),
or sits in a hole (a dugout) or a natural depression (core/ponds.py). The result is written in the
formats of tools 1-4 - the dam inventory with the tool-3 storage fields, the stage-storage curves,
the breach points and flow arrows, and one terrain clip per breachable pond - so that tool 5
(breach hydrograph), 6, 7, 8 (hazard classification), 9 and 10 run on ponds unchanged.

A dugout releases only the water that stands above the natural ground; where there is none the
pond is marked no_flood = YES ("no breach flood") and the later tools report it as such instead
of routing a flood.
"""
import math
import os
import zlib

import numpy as np
from osgeo import gdal, ogr, osr
from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterField, QgsProcessingParameterFile,
                       QgsProcessingParameterEnum, QgsProcessingParameterNumber,
                       QgsProcessingParameterString, QgsProcessingParameterBoolean,
                       QgsProcessingParameterFolderDestination, QgsProcessingParameterFeatureSink,
                       QgsProcessingException, QgsFields, QgsField, QgsFeature, QgsGeometry,
                       QgsPointXY, QgsFeatureRequest, QgsRectangle, QgsCoordinateTransform,
                       QgsCoordinateReferenceSystem, QgsProject)

from ..core import ponds as pd, terrain
from ..core import inventory as invmod
from ..compat import (FIELD_ANY, FILE_FOLDER, FT_DOUBLE, FT_STRING, NUM_DOUBLE, SRC_LINE, SRC_POINT,
                      SRC_POLYGON, WKB_LINESTRING, WKB_POINT)
from ..damsuite import gdalsafe as GS
from ..damsuite import solvegate as SG
from . import _common as cm
from ..core import errors as er
from .breach_direction import PT_FIELDS
from .estimate_storage import NEW_FIELDS as STORAGE_FIELDS
from .prepare_dams import OUT_FIELDS as INVENTORY_FIELDS

AC_M2 = 4046.8564224
AF_M3 = pd.AF_M3
FT_M = pd.FT
MAX_WINDOW_CELLS = 30_000_000

POND_FIELDS = [("pond_type", FT_STRING), ("pond_conf", FT_STRING), ("no_flood", FT_STRING),
               ("no_flood_why", FT_STRING), ("above_grade_m3", FT_DOUBLE),
               ("above_grade_af", FT_DOUBLE), ("grade_m", FT_DOUBLE), ("head_ft", FT_DOUBLE),
               ("freeboard_ft", FT_DOUBLE), ("arc_deg", FT_DOUBLE), ("bank_deg", FT_DOUBLE),
               ("pool_method", FT_STRING), ("area_ac", FT_DOUBLE), ("wb_id", FT_STRING),
               ("wb_type", FT_STRING), ("pond_notes", FT_STRING)]
EXCLUDED_FIELDS = [("wb_id", FT_STRING), ("wb_type", FT_STRING), ("pond_type", FT_STRING),
                   ("reason", FT_STRING), ("area_ac", FT_DOUBLE), ("pool_m", FT_DOUBLE),
                   ("notes", FT_STRING)]


def _cell_of(path):
    """The cell size of a raster (inf when it cannot be read)."""
    try:
        ds = gdal.Open(path)
        return abs(ds.GetGeoTransform()[1]) if ds is not None else float("inf")
    except Exception:                                            # noqa
        return float("inf")


def pond_id(x, y):
    """Stable dam ID of a water body: from its centroid in EPSG:5070 rounded to 1 m, so the same
    water body gets the same ID in every run (and the terrain / curve files are found again)."""
    return "POND-{:08X}".format(zlib.crc32("{:.0f},{:.0f}".format(x, y).encode("ascii")) & 0xFFFFFFFF)


def words(text):
    return [w.strip().lower() for w in str(text or "").replace(";", ",").split(",") if w.strip()]


def mentions(value, keywords):
    v = str(value or "").lower()
    return any(k in v for k in keywords)


def rasterize(wkbs, gt, shape):
    """Boolean mask of polygon WKBs (EPSG:5070) on a grid."""
    mem = GS.checked(gdal.GetDriverByName("MEM").Create("", shape[1], shape[0], 1, gdal.GDT_Byte),
                     "making the water-body mask")
    mem.SetGeoTransform(gt)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(5070)
    mem.SetProjection(srs.ExportToWkt())
    vds = (ogr.GetDriverByName("MEM") or ogr.GetDriverByName("Memory")).CreateDataSource("wb")
    lyr = vds.CreateLayer("wb", srs, ogr.wkbPolygon)
    n = 0
    for w in wkbs:
        geom = ogr.CreateGeometryFromWkb(bytes(w))
        if geom is None:
            continue
        ft = ogr.Feature(lyr.GetLayerDefn())
        ft.SetGeometry(geom)
        lyr.CreateFeature(ft)
        n += 1
    if n:
        gdal.RasterizeLayer(mem, [1], lyr, burn_values=[1])
    return mem.ReadAsArray().astype(bool)


def clip_covers(path, ext):
    ds = cm.gdal_open(path)
    gt = ds.GetGeoTransform()
    x0, y1 = gt[0], gt[3]
    x1, y0 = x0 + ds.RasterXSize * gt[1], y1 + ds.RasterYSize * gt[5]
    return x0 <= ext[0] + 1 and y0 <= ext[1] + 1 and x1 >= ext[2] - 1 and y1 >= ext[3] - 1


class FindPonds(QgsProcessingAlgorithm):
    def name(self): return "findponds"
    def displayName(self): return "16 - Find ponds and dugouts"
    def group(self): return "Dam screening"
    def groupId(self): return "dams"
    def createInstance(self): return FindPonds()

    def shortHelpString(self):
        return (
            "Finds the ponds and dugouts among a layer of water bodies and writes them as dams, so the "
            "screening tools (5 breach hydrograph, 6 triage, 7 flood simulation, 8 hazard "
            "classification, 9, 10) run on them unchanged. This tool takes the place of tools 1-4 for "
            "ponds: it writes the dam inventory with the tool-3 storage fields, the stage-storage "
            "curves, the breach points and flow arrows, and a terrain clip for every pond that can "
            "breach. Use a separate work folder from the DWR dam run.\n"
            "INPUT: water-body polygons - NHD waterbodies (the data package of tool 0 has them), NWI "
            "wetlands, or your own layer - and the LiDAR terrain (USGS 3DEP or your local DEM tiles). "
            "The tool does not download water bodies: load the layer in QGIS and pick it here.\n"
            "HOW IT DECIDES (core/ponds.py). The pool is the flat water surface of the DEM, else the "
            "shoreline. The ground around the water is cut into 16 compass sectors; in each, the RIM is "
            "the highest ground within the 'crest search distance' of the water and the TOE the foot of "
            "the embankment face: where the ground below the rim stops falling steeper than your "
            "'face' angle (natural slopes are gentler), searched out to the 'toe search distance' (feet). Where the pool is higher than the "
            "toe by at least your 'head' value the sector holds water above the ground. An arc of at "
            "least your 'minimum arc' = EMBANKMENT POND: the crest (top of dam) is the lowest rim of "
            "those sectors, the natural ground the breach cuts down to is their lowest toe, and the "
            "breach point is the rim of the sector with the lowest toe, flow away from the water. "
            "Otherwise the water sits in a hole: a DUGOUT when the banks are steep, a raised spoil "
            "rim surrounds it or the layer's own type field says excavated, else a NATURAL "
            "DEPRESSION.\n"
            "DUGOUTS RELEASE ONLY WHAT IS ABOVE THE GROUND: the volume between the natural ground and "
            "the lowest rim (a spoil rim can hold some). Below your 'smallest breach flood' volume - or "
            "when the rim is lower than the 0.3 m breach height tool 5 can route - the pond is marked "
            "no_flood = YES, no flood is simulated, and tool 8 reports 'Low - no breach flood'.\n"
            "STORAGE: above the pool from the DEM, below the pool by the shoreline-slope proxy of "
            "tool 3 (the LiDAR cannot see under the water; the greatest depth is YOUR assumption). "
            "No inventory storage exists for ponds, so stor_method = estimated.\n"
            "EVERY THRESHOLD IS YOUR VALUE: the starting values are not standards. Natural depressions "
            "(prairie potholes) are left out of the dam layer unless you ask for them and are listed "
            "in the excluded-water-bodies layer with the reason. Check the classes on a few ponds "
            "against an aerial image before running a whole county. Screening only: the LiDAR is not a "
            "survey, small ponds are rarely hydro-flattened, and the official classification is the "
            "Department's. Not run in a real QGIS.")

    def initAlgorithm(self, config=None):
        P = self.addParameter
        P(QgsProcessingParameterFeatureSource(
            "WB", "Water bodies (polygons: NHD waterbodies, NWI wetlands or your own)", [SRC_POLYGON]))
        P(QgsProcessingParameterField(
            "WB_ID", "Water bodies: ID field (optional; kept as wb_id)", parentLayerParameterName="WB",
            type=FIELD_ANY, optional=True))
        P(QgsProcessingParameterField(
            "WB_TYPE", "Water bodies: type / description field (optional; e.g. NWI ATTRIBUTE, NHD FType)",
            parentLayerParameterName="WB", type=FIELD_ANY, optional=True))
        # 0.21.0: a storage value the water-body layer itself carries (compared, never used)
        P(QgsProcessingParameterField(
            "WB_STOR", "Water bodies: inventory storage field (optional, acre-feet; compared with "
            "the LiDAR storage, the LiDAR curve is kept)", parentLayerParameterName="WB",
            type=FIELD_ANY, optional=True))
        # 0.19.0: DWR's dugout inventory joined to its dugout polygons by structure number
        P(QgsProcessingParameterFeatureSource(
            "INV", "DWR dugout records (optional points: DWR_Dugouts; joined to the water bodies by "
            "the ID field above, e.g. structure_id of DWR_Dugouts_Polygons)", [SRC_POINT], optional=True))
        P(QgsProcessingParameterField(
            "INV_ID", "DWR dugout records: structure ID field", defaultValue="structure_id",
            parentLayerParameterName="INV", type=FIELD_ANY, optional=True))
        P(QgsProcessingParameterString(
            "INV_KEEP", "DWR dugout records: keep only these statuses (comma-separated; empty = all)",
            defaultValue="Intact, Pending", optional=True))
        P(QgsProcessingParameterString(
            "DUG_WORDS", "Words in that field that mark an EXCAVATED water body (comma-separated; "
            "empty = the layer is not used)", defaultValue="", optional=True))
        P(QgsProcessingParameterString(
            "ONLY_IDS", "Only these water bodies (comma-separated: values of the ID field, pond IDs such as "
            "POND-1a2b3c4d, or feature ids; empty = all) - 'Run one dam' uses this", defaultValue="", optional=True))
        P(QgsProcessingParameterNumber(
            "MIN_AC", "Smallest water body to analyse (acres; YOUR value; 0 = no limit)", NUM_DOUBLE,
            0.05, minValue=0))
        P(QgsProcessingParameterNumber(
            "MAX_AC", "Largest water body to analyse (acres; YOUR value; bigger ones are reservoirs "
            "for the dam inventory; 0 = no limit)", NUM_DOUBLE, 50.0, minValue=0))
        P(QgsProcessingParameterEnum("SRC", "Terrain source", ["USGS 3DEP service", "Local DEM folder"],
                                     defaultValue=0))
        P(QgsProcessingParameterFile("LOCAL", "Local DEM folder (if local source)",
                                     behavior=FILE_FOLDER, optional=True))
        cm.distance_params(P, "CELL_FT", "CELL", "Analysis cell size (ft; the LiDAR's own resolution or coarser)",
                           6.6, min_ft=1.6)
        cm.distance_params(P, "RING_FT", "RING", "Toe search distance beyond the water edge (ft; YOUR value)",
                           200.0, min_ft=30.0)
        cm.distance_params(P, "CREST_FT", "CREST", "Crest search distance beyond the water edge (ft; YOUR value)",
                           80.0, min_ft=12.0)
        P(QgsProcessingParameterNumber(
            "FACE_DEG", "Embankment face: ground below the rim that falls steeper than (degrees; YOUR "
            "value; gentler ground is the natural slope)", NUM_DOUBLE, 8.0, minValue=1, maxValue=60))
        P(QgsProcessingParameterNumber(
            "HEAD_FT", "Water counts as held above the ground when the pool is higher than the toe by "
            "at least (ft; YOUR value)", NUM_DOUBLE, 1.0, minValue=0.1))
        P(QgsProcessingParameterNumber(
            "ARC", "Embankment pond when at least this much of the shore holds water above the ground "
            "(degrees of 360; YOUR value)", NUM_DOUBLE, 45.0, minValue=22.5, maxValue=360))
        P(QgsProcessingParameterNumber(
            "DUG_DEG", "Dugout when the banks are steeper than (degrees; YOUR value)", NUM_DOUBLE, 14.0,
            minValue=1, maxValue=80))
        P(QgsProcessingParameterNumber(
            "DEPTH_FT", "Greatest water depth to assume (ft; YOUR assumption - the LiDAR cannot see "
            "below the water)", NUM_DOUBLE, 12.0, minValue=1))
        P(QgsProcessingParameterNumber(
            "RELEASE_AF", "Smallest breach flood: water above the natural ground of at least (acre-ft; "
            "YOUR value; less = no breach flood)", NUM_DOUBLE, 0.1, minValue=0))
        P(QgsProcessingParameterNumber(
            "SHAPE", "Below-pool shape factor (calibration, as tool 3)", NUM_DOUBLE, 1.0, minValue=0.1,
            maxValue=1.5))
        P(QgsProcessingParameterBoolean(
            "KEEP_DEP", "Also write natural depressions to the dam layer (they never flood)",
            defaultValue=False))
        cm.distance_params(P, "RADIUS_FT", "RADIUS", "Half-width of the terrain clip kept for each breachable "
                           "pond (ft)", 5000.0, min_ft=600.0)
        cm.distance_params(P, "CLIP_CELL_FT", "CLIP_CELL", "Cell size of that clip (ft; as tool 2)",
                           9.8, min_ft=1.6)
        cm.distance_params(P, "ARROW_FT", "ARROW_M", "Direction arrow length (ft)", 800.0, min_ft=30.0)
        P(QgsProcessingParameterFolderDestination("TERRAIN", "Terrain clips folder (dam_<ID>.tif)"))
        P(QgsProcessingParameterFolderDestination("CURVES", "Folder for stage-storage curves (CSV)"))
        P(QgsProcessingParameterFeatureSink("OUT", "Ponds and dugouts (dams with storage)", SRC_POINT))
        P(QgsProcessingParameterFeatureSink("OUTP", "Breach points", SRC_POINT))
        P(QgsProcessingParameterFeatureSink("OUTL", "Flow direction arrows", SRC_LINE))
        P(QgsProcessingParameterFeatureSink("OUTX", "Water bodies not written (excluded, failed)",
                                            SRC_POINT, optional=True))

    # ------------------------------------------------------------------ terrain --
    def _dem_window(self, ext, cell, pid, src, tmp):
        """(array in m with NaN for no data, geotransform) on a grid of `cell` over `ext`."""
        n = int(round((ext[2] - ext[0]) / cell))
        m = int(round((ext[3] - ext[1]) / cell))
        if n * m > MAX_WINDOW_CELLS:
            raise ValueError("the water body needs a {}x{} cell window: raise the cell size or lower the "
                             "largest water body".format(n, m))
        clip = os.path.join(self._terrain, "dam_{}.tif".format(pid))
        tiles = []
        # 0.21.0: the dam clip only when it is as fine as the analysis cell. It is written at the
        # clip cell (often coarser), so a second run read the pond from the coarse clip and gave
        # a different storage (the test pond: 2,800 m3 first run, 1,600 m3 on the rerun).
        if os.path.exists(clip) and clip_covers(clip, ext) and _cell_of(clip) <= cell * 1.001:
            source = clip
        elif self._vrt:
            source = self._vrt
        else:
            tiles = terrain.fetch_3dep(ext, cell, tmp, "p{}".format(pid))
            source = tiles
        try:
            ds = GS.checked(gdal.Warp("", source, format="MEM", dstSRS=cm.ALBERS, outputBounds=ext,
                                      width=n, height=m, resampleAlg="average", srcNodata=-9999,
                                      dstNodata=-9999, outputType=gdal.GDT_Float32),
                            "warping the terrain")
        finally:
            for t in tiles:
                try:
                    os.remove(t)
                except OSError:
                    pass
        arr = ds.GetRasterBand(1).ReadAsArray().astype("float64")
        arr[(arr <= -9998.0) | ~np.isfinite(arr)] = np.nan
        if np.isnan(arr).mean() > 0.5:
            raise ValueError("no terrain data over most of this water body (check the terrain source "
                             "covers it)")
        return arr, ds.GetGeoTransform()

    def _write_clip(self, dst, centre, radius, clip_cell, pid, src, tmp):
        """tool 2's dam_<ID>.tif for a breachable pond (an existing file is reused - 0.17.0: only
        when cut for this extent, cell and source, as in tool 2)."""
        ext = (centre[0] - radius, centre[1] - radius, centre[0] + radius, centre[1] + radius)
        if cm.clip_reusable(dst, ext, clip_cell, self._src_key)[0]:
            return False
        cm.drop_clip(dst)
        if self._vrt:
            GS.checked(gdal.Warp(dst, self._vrt, dstSRS=cm.ALBERS, outputBounds=ext, xRes=clip_cell,
                                 yRes=clip_cell, resampleAlg="bilinear", outputType=gdal.GDT_Float32,
                                 dstNodata=-9999, creationOptions=["COMPRESS=DEFLATE"]), "clipping the DEM")
        else:
            tiles = terrain.fetch_3dep(ext, clip_cell, tmp, "c{}".format(pid))
            try:
                GS.checked(gdal.Warp(dst, tiles, dstNodata=-9999, creationOptions=["COMPRESS=DEFLATE"]),
                           "mosaicking the 3DEP tiles")
            finally:
                for t in tiles:
                    try:
                        os.remove(t)
                    except OSError:
                        pass
        terrain.set_unit_m(dst)                                   # 0.17.0: tagged as metres
        cm.write_clip_meta(dst, ext, clip_cell, self._src_key)
        return True

    def _inventory(self, params, context, feedback):
        """0.19.0: {structure ID text: record} of the DWR dugout points, or {}."""
        src = self.parameterAsSource(params, "INV", context)
        if src is None:
            return {}
        names = src.fields().names()
        fid = self.parameterAsString(params, "INV_ID", context) or "structure_id"
        fld, _n = invmod.resolve_fields({"ID": fid if fid in names else None}, names, invmod.KIND_DUGOUT)
        lower = {x.lower(): x for x in names}
        purpose = lower.get("purpose")
        out = {}
        for f in src.getFeatures():
            a = invmod.standardize(lambda n, _f=f: _f[n], fld, False, "DUG", f.id(), invmod.KIND_DUGOUT)
            a["purpose"] = invmod._txt(f[purpose]) if purpose else ""
            out[a["id"]] = a
        feedback.pushInfo("DWR dugout records: {} (joined by {})".format(len(out), fld.get("ID")))
        return out

    # --------------------------------------------------------------------- run --
    def processAlgorithm(self, params, context, feedback):
        wbs = self.parameterAsSource(params, "WB", context)
        S = lambda k: self.parameterAsString(params, k, context) or None
        D = lambda k: self.parameterAsDouble(params, k, context)
        f_id, f_type = S("WB_ID"), S("WB_TYPE")
        names = wbs.fields().names()
        f_id = f_id if f_id in names else None
        f_type = f_type if f_type in names else None
        f_stor = S("WB_STOR")
        f_stor = f_stor if f_stor in names else None
        dug_words = words(S("DUG_WORDS")) if f_type else []
        min_ac, max_ac = D("MIN_AC"), D("MAX_AC")
        cell = cm.distance_m(self, params, context, "CELL_FT", "CELL", 6.6)
        clip_cell = cm.distance_m(self, params, context, "CLIP_CELL_FT", "CLIP_CELL", 9.8)
        radius = cm.distance_m(self, params, context, "RADIUS_FT", "RADIUS", 5000.0)
        keep_dep = self.parameterAsBool(params, "KEEP_DEP", context)
        arrow = cm.distance_m(self, params, context, "ARROW_FT", "ARROW_M", 800.0)
        ring_m = cm.distance_m(self, params, context, "RING_FT", "RING", 200.0)
        crest_m = cm.distance_m(self, params, context, "CREST_FT", "CREST", 80.0)
        pp = pd.PondParams(ring_m=ring_m, crest_m=min(crest_m, ring_m),
                           min_head_m=D("HEAD_FT") * FT_M, min_arc_deg=D("ARC"), face_deg=D("FACE_DEG"),
                           max_depth_m=D("DEPTH_FT") * FT_M, dugout_slope_deg=D("DUG_DEG"),
                           release_min_m3=D("RELEASE_AF") * AF_M3, shape_factor=D("SHAPE"))
        self._terrain = self.parameterAsString(params, "TERRAIN", context)
        curves = self.parameterAsString(params, "CURVES", context)
        os.makedirs(self._terrain, exist_ok=True)
        os.makedirs(curves, exist_ok=True)
        tmp = os.path.join(self._terrain, "_tmp")
        self._vrt = None
        self._src_key = "3dep"
        if self.parameterAsEnum(params, "SRC", context) == 1:
            folder = S("LOCAL")
            tiles = terrain.list_local_tiles(folder) if folder else []
            if not tiles:
                raise QgsProcessingException("No DEM tiles found in the local DEM folder")
            self._vrt = os.path.join(self._terrain, "_local_mosaic.vrt")
            # 0.17.0: feet tiles converted, mixed CRSs warped to EPSG:5070 (no tile dropped)
            try:
                cm.local_mosaic(tiles, self._vrt, feedback.pushInfo)
            except RuntimeError as e:
                raise QgsProcessingException(str(e))
            self._src_key = "local:" + cm.mosaic_key(tiles)

        alb, wgs = cm.albers(), QgsCoordinateReferenceSystem("EPSG:4326")
        prj = QgsProject.instance()
        w2a = QgsCoordinateTransform(wbs.sourceCrs(), alb, prj)
        a2w = QgsCoordinateTransform(alb, wbs.sourceCrs(), prj)
        a2g = QgsCoordinateTransform(alb, wgs, prj)

        fields = QgsFields()
        for n, t in INVENTORY_FIELDS + STORAGE_FIELDS + POND_FIELDS:
            fields.append(QgsField(n, t))
        pf = QgsFields()
        for n, t in PT_FIELDS:
            pf.append(QgsField(n, t))
        lf = QgsFields()
        for n, t in (("dam_id", FT_STRING), ("flow_az", FT_DOUBLE), ("dir_conf", FT_STRING)):
            lf.append(QgsField(n, t))
        xf = QgsFields()
        for n, t in EXCLUDED_FIELDS:
            xf.append(QgsField(n, t))
        sink, dest = cm.us_sink(self, params, "OUT", context, fields, WKB_POINT, wgs)
        sp, dp = cm.us_sink(self, params, "OUTP", context, pf, WKB_POINT, alb)
        sl, dl = self.parameterAsSink(params, "OUTL", context, lf, WKB_LINESTRING, alb)
        sx, dx = self.parameterAsSink(params, "OUTX", context, xf, WKB_POINT, wgs)

        total = wbs.featureCount() or 1
        counts, results = {}, []
        n_size = n_fail = n_clip = n_empty = 0
        used_ids = set()
        pad = pp.ring_m + 4.0 * cell

        def excluded(f_wb_id, f_wb_type, kind, reason, area_ac, pool, notes, xy):
            if kind == "FAILED":
                counts[kind] = counts.get(kind, 0) + 1
            if sx is None or xy is None:
                return
            g = QgsGeometry.fromPointXY(QgsPointXY(xy[0], xy[1]))
            g.transform(a2g)
            o = QgsFeature(xf)
            o.setGeometry(g)
            o.setAttributes([f_wb_id, f_wb_type, kind, reason, area_ac, pool, notes])
            sx.addFeature(o)

        only = {x.strip().upper() for x in (S("ONLY_IDS") or "").split(",") if x.strip()}
        n_only = 0
        invmap, n_status = self._inventory(params, context, feedback), 0
        inv_keep = invmod.parse_status_list(S("INV_KEEP") or "")
        for i, f in enumerate(wbs.getFeatures()):
            if feedback.isCanceled():
                break
            feedback.setProgress(100.0 * i / total)
            g = f.geometry()
            if g is None or g.isNull() or g.isEmpty():
                n_empty += 1
                continue
            g.transform(w2a)
            area_ac = g.area() / AC_M2
            wb_id = str(f[f_id]) if f_id and f[f_id] not in (None, "") else str(f.id())
            wb_type = str(f[f_type]) if f_type and f[f_type] not in (None, "") else ""
            c = g.centroid().asPoint()
            if not g.contains(QgsGeometry.fromPointXY(QgsPointXY(c.x(), c.y()))):
                c = g.pointOnSurface().asPoint()
            xy = (c.x(), c.y())
            pid = pond_id(*xy)
            rec = invmap.get(wb_id) if invmap else None
            if rec is not None:              # 0.19.0: a DWR dugout keeps its DWR number
                pid = "DUG-" + wb_id
            if only and not any(str(v).strip().upper() in only for v in (wb_id, pid, f.id())):
                n_only += 1
                continue
            if (min_ac and area_ac < min_ac) or (max_ac and area_ac > max_ac):
                n_size += 1
                continue
            if rec is not None and not invmod.status_ok(rec["status"], inv_keep):
                n_status += 1
                excluded(wb_id, wb_type, "DWR status", "DWR status '{}' not kept".format(rec["status"]),
                         area_ac, None, rec["name"], xy)
                continue
            if invmap and rec is None:
                n_status += 1
                excluded(wb_id, wb_type, "no DWR record", "no DWR dugout record with this ID", area_ac,
                         None, "", xy)
                continue
            k = 2
            base = pid
            while pid in used_ids:                          # two water bodies, one centroid metre
                pid = "{}-{}".format(base, k)
                k += 1
            used_ids.add(pid)
            try:
                bb = g.boundingBox()
                x0 = math.floor((bb.xMinimum() - pad) / cell) * cell
                y0 = math.floor((bb.yMinimum() - pad) / cell) * cell
                x1 = math.ceil((bb.xMaximum() + pad) / cell) * cell
                y1 = math.ceil((bb.yMaximum() + pad) / cell) * cell
                ext = (x0, y0, x1, y1)
                dem, gt = self._dem_window(ext, cell, pid, None, tmp)
                near = [a2w.transformBoundingBox(QgsRectangle(*ext))]
                others = []
                for nb in wbs.getFeatures(QgsFeatureRequest().setFilterRect(near[0])):
                    if nb.id() == f.id():
                        continue
                    ng = nb.geometry()
                    if ng is None or ng.isNull() or ng.isEmpty():
                        continue
                    ng.transform(w2a)
                    others.append(bytes(ng.asWkb()))
                shape = dem.shape
                water = rasterize([bytes(g.asWkb())], gt, shape)
                other = rasterize(others, gt, shape) if others else None
                hint = bool(dug_words and mentions(wb_type, dug_words))
                res = pd.analyze_pond(dem, water, cell, pp, other=other, excavated_hint=hint)
            except Exception as e:                           # keep the batch going
                n_fail += 1
                feedback.reportError("water body {}: {}".format(wb_id, er.plain(e, wb_id, "16")))
                excluded(wb_id, wb_type, "FAILED", er.plain(e, wb_id, "16"), area_ac, None, "", xy)
                continue

            results.append(res)
            counts[res.pond_type] = counts.get(res.pond_type, 0) + 1
            if res.no_flood:
                counts["no breach flood"] = counts.get("no breach flood", 0) + 1
            if res.pond_type == pd.T_DEPRESSION and not keep_dep:
                excluded(wb_id, wb_type, res.pond_type, "natural depression: " + res.no_flood_reason,
                         area_ac, res.pool_m, "; ".join(res.notes), xy)
                continue

            notes = list(res.notes)
            if res.no_flood:
                notes.insert(0, "no breach flood: " + res.no_flood_reason)
            elif res.pond_type == pd.T_DUGOUT:
                notes.insert(0, "dugout: only the {:.2f} ac-ft above the natural ground is released".format(
                    res.above_grade_m3 / AF_M3))
            inv_differs, inv_ratio = False, None
            if f_stor and rec is None:        # 0.21.0: the layer's own storage value
                try:
                    inv_af = float(f[f_stor])
                except (TypeError, ValueError):
                    inv_af = None
                snote = SG.storage_check(res.total_m3, inv_af * AF_M3) if inv_af else ""
                if inv_af and inv_af > 0 and res.total_m3 > 0:
                    inv_ratio = inv_af * AF_M3 / res.total_m3
                if snote:
                    if snote.startswith("differs"):
                        snote = "CHECK: " + snote[len("differs: "):]
                        inv_differs = True
                    notes.insert(0, snote)
            gg = QgsGeometry.fromPointXY(QgsPointXY(xy[0], xy[1]))
            gg.transform(a2g)
            o = QgsFeature(fields)
            o.setGeometry(gg)
            height = res.height_m if res.height_m > 0 else None
            name, src, hz, status, inv_above = "{} {}".format(res.pond_type, wb_id), "POND", "", "Intact", None
            extras = ["pond", "measured (LiDAR)" if height else "none", "measured (LiDAR)", None, None,
                      None, None, ""]
            conf_out = res.confidence
            if inv_differs:
                conf_out = "low"
            used_m3, used_method, curve_k = res.total_m3, "estimated", 1.0
            if rec is not None:              # 0.19.0: the DWR record's values beside the LiDAR's
                name = rec["name"] or "DWR dugout {}".format(wb_id)
                src, hz, status = "DWR dugout", rec["hz"], rec["status"] or "Intact"
                inv_above = rec["above"]
                cmp_notes = ["DWR dugout {} ({}); LiDAR type: {}".format(wb_id, rec["purpose"] or "purpose not "
                                                                      "recorded", res.pond_type)]
                lid_af = res.above_grade_m3 / AF_M3
                if rec["above"]:
                    dwr_af = rec["above"] / AF_M3
                    cmp_notes.append("above-grade storage: DWR {:,.1f} ac-ft, LiDAR {:,.1f} ac-ft".format(dwr_af, lid_af))
                    if res.no_flood and dwr_af >= pp.release_min_m3 / AF_M3:
                        cmp_notes.append("CHECK: DWR records water above grade but the LiDAR finds no rim "
                                         "holding it")
                        conf_out = "low"
                    elif lid_af > 0 and not 0.5 <= dwr_af / lid_af <= 2.0:
                        cmp_notes.append("CHECK: DWR and LiDAR above-grade storage differ by more than 2x")
                        conf_out = "low"
                elif rec["total"]:
                    cmp_notes.append("DWR total storage {:,.1f} ac-ft (mostly below grade, not released); "
                                     "LiDAR above grade {:,.1f} ac-ft".format(rec["total"] / AF_M3, lid_af))
                if rec["total"] and used_m3 > rec["total"] * 1.001:
                    # the pond cannot release more than it holds: the DEM pool spread past the pond
                    cmp_notes.append("CHECK: the LiDAR release {:,.1f} ac-ft is more than the DWR total "
                                     "storage {:,.1f} ac-ft: release held to the DWR total".format(
                                         used_m3 / AF_M3, rec["total"] / AF_M3))
                    curve_k = rec["total"] / used_m3
                    used_m3, used_method, conf_out = rec["total"], "inventory total (cap)", "low"
                if rec["purpose"] and "lagoon" in rec["purpose"].lower():
                    cmp_notes.append("waste lagoon: a release would also carry wastewater (not part of the "
                                     "ND flood-hazard criteria; note it for the engineer)")
                notes = cmp_notes + notes
                extras = ["dugout", "measured (LiDAR)" if height else "none", "measured (LiDAR)",
                          rec["total"], rec["above"], rec["depth"], rec["dug_h"], "; ".join(cmp_notes)]
            row = [pid, name, src, "", height, None, inv_above, None, hz,
                   status, res.crest_len_m, ""] + extras + [
                   res.pool_m, res.crest_m, res.area_m2, res.below_pool_m3, res.above_pool_m3,
                   res.total_m3, res.max_depth_m, res.shore_slope_deg, used_m3, used_method,
                   conf_out, inv_ratio, "; ".join(notes), curve_k,
                   res.curve[-1][1] * curve_k if res.curve else None,
                   None, None,
                   res.pond_type, res.confidence, "YES" if res.no_flood else "", res.no_flood_reason,
                   res.above_grade_m3, res.above_grade_m3 / AF_M3, res.ground_m, res.head_m / FT_M,
                   res.freeboard_m / FT_M, res.arc_deg, res.bank_slope_deg, res.pool_method, area_ac,
                   wb_id, wb_type, "; ".join(notes)]
            o.setAttributes(row)
            sink.addFeature(o)
            with open(os.path.join(curves, "curve_{}.csv".format(pid)), "w") as fh:
                fh.write("elev_m,storage_m3,elev_ft,storage_acft\n")
                for e_, v_ in res.curve:
                    v_ *= curve_k                # 0.19.0: held to the DWR total storage
                    fh.write("{:.3f},{:.1f},{:.2f},{:.3f}\n".format(e_, v_, e_ / 0.3048, v_ / 1233.48183754752))

            if res.no_flood or res.breach_rc is None or res.flow_az is None:
                continue
            try:                                              # the terrain the flood runs over
                n_clip += bool(self._write_clip(os.path.join(self._terrain, "dam_{}.tif".format(pid)),
                                                xy, radius, clip_cell, pid, None, tmp))
            except Exception as e:
                n_fail += 1
                feedback.reportError("pond {}: terrain clip not written: {}".format(pid, e))
            r, cc = res.breach_rc
            bx, by = gt[0] + (cc + 0.5) * gt[1], gt[3] + (r + 0.5) * gt[5]
            conf = "medium" if res.confidence == "medium" and res.pond_type == pd.T_EMBANKMENT else "low"
            bp = QgsFeature(pf)
            bp.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(bx, by)))
            bp.setAttributes([pid, "{} {}".format(res.pond_type, wb_id), None, 0, res.crest_len_m,
                              (res.flow_az + 90.0) % 360.0, res.flow_az,
                              "pond rim at the lowest toe", conf, "pond", res.height_m,
                              "; ".join(notes)])
            sp.addFeature(bp)
            az = math.radians(res.flow_az)
            ln = QgsFeature(lf)
            ln.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(bx, by), QgsPointXY(
                bx + arrow * math.sin(az), by + arrow * math.cos(az))]))
            ln.setAttributes([pid, res.flow_az, conf])
            sl.addFeature(ln)

        n_written = sum(1 for r in results if not (r.pond_type == pd.T_DEPRESSION and not keep_dep))
        feedback.pushInfo("water bodies: {} analysed, {} written to the dam layer, {} outside your size "
                          "limits, {} without geometry, {} failed{}".format(
                              len(results) + n_fail, n_written, n_size, n_empty, n_fail,
                              ", {} not in the chosen IDs".format(n_only) if only else ""))
        if invmap:
            feedback.pushInfo("DWR dugout records: {} water bodies left out by DWR status or with no "
                              "DWR record (listed in the excluded layer)".format(n_status))
        if only and not results and not n_fail:
            raise QgsProcessingException("None of the water bodies matches the chosen IDs ({}); give a value of "
                                         "the ID field, a pond ID (POND-...) or a feature id.".format(
                                             ", ".join(sorted(only))))
        feedback.pushInfo("by type: " + (", ".join("{} {}".format(v, k) for k, v in sorted(counts.items()))
                                         or "none"))
        n_nf = sum(1 for r in results if r.no_flood and not (r.pond_type == pd.T_DEPRESSION and not keep_dep))
        feedback.pushInfo("{} of the written water bodies have no breach flood (no_flood = YES: nothing "
                          "stands above the natural ground); {} terrain clip(s) written for the others"
                          .format(n_nf, n_clip))
        feedback.pushInfo("Screening estimate from the LiDAR and your thresholds; check a few ponds on an "
                          "aerial image. Not run in a real QGIS.")
        return {"OUT": dest, "OUTP": dp, "OUTL": dl, "OUTX": dx, "TERRAIN": self._terrain, "CURVES": curves}
