"""Cache one DEM clip per dam (EPSG:5070) from 3DEP or a local LiDAR folder."""
import os

from osgeo import gdal
from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterEnum, QgsProcessingParameterNumber,
                       QgsProcessingParameterFolderDestination,
                       QgsProcessingParameterFile, QgsProcessingException,
                       QgsCoordinateTransform, QgsCoordinateReferenceSystem,
                       QgsProject)

from ..core import terrain
from ..compat import (FILE_FOLDER, NUM_DOUBLE, SRC_POINT)
from ..damsuite import gdalsafe as GS
from . import _common as cm
from ..core import errors as er


class TerrainCache(QgsProcessingAlgorithm):
    DAMS, SRC, LOCAL, RADIUS, CELL, OUT, ID = ("DAMS", "SRC", "LOCAL", "RADIUS",
                                               "CELL", "OUT", "ID")

    def name(self): return "terraincache"
    def displayName(self): return "2 - Cache terrain per dam"
    def group(self): return "Dam screening"
    def groupId(self): return "dams"
    def createInstance(self): return TerrainCache()

    def shortHelpString(self):
        return ("Writes dam_<ID>.tif (float32 metres, EPSG:5070) for every dam. Source is "
                "either the USGS 3DEP service (needs internet, run once) or a local folder "
                "of ND LiDAR-derived DEM tiles (works offline). Existing files are reused, "
                "so the batch can be re-run or resumed (0.17.0: only when cut with the same "
                "half-width, cell size and source; dam_<ID>.tif.json keeps them). Local tiles "
                "tagged in feet are converted to metres; untagged tiles are taken as metres (a "
                "warning is logged); tiles in different CRSs are each warped to EPSG:5070. 3DEP serves the best available "
                "resolution per location; check the resulting cell size where it matters.")

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.DAMS, "Standardized dams", [SRC_POINT]))
        self.addParameter(QgsProcessingParameterEnum(
            self.SRC, "Terrain source", ["USGS 3DEP service", "Local DEM folder"], defaultValue=0))
        self.addParameter(QgsProcessingParameterFile(
            self.LOCAL, "Local DEM folder (if local source)",
            behavior=FILE_FOLDER, optional=True))
        cm.distance_params(self.addParameter, "RADIUS_FT", self.RADIUS,
                           "Half-width of clip around each dam (ft)", 5000.0, min_ft=600.0)
        cm.distance_params(self.addParameter, "CELL_FT", self.CELL,
                           "Cell size (ft; the DEM's own cell - a 1 m LiDAR DEM is 3.3 ft)", 3.3, min_ft=1.6)
        self.addParameter(QgsProcessingParameterFolderDestination(self.OUT, "Output folder"))

    def processAlgorithm(self, params, context, feedback):
        dams = self.parameterAsSource(params, self.DAMS, context)
        use_local = self.parameterAsEnum(params, self.SRC, context) == 1
        radius = cm.distance_m(self, params, context, "RADIUS_FT", self.RADIUS, 5000.0)
        cell = cm.distance_m(self, params, context, "CELL_FT", self.CELL, 3.3)
        out = self.parameterAsString(params, self.OUT, context)
        os.makedirs(out, exist_ok=True)

        vrt = None
        if use_local:
            folder = self.parameterAsString(params, self.LOCAL, context)
            tiles = terrain.list_local_tiles(folder)
            if not tiles:
                raise QgsProcessingException("No DEM tiles found in the local folder")
            vrt = os.path.join(out, "_local_mosaic.vrt")
            # 0.17.0: feet tiles converted, mixed CRSs warped to EPSG:5070 (no tile dropped)
            try:
                cm.local_mosaic(tiles, vrt, feedback.pushInfo)
            except RuntimeError as e:
                raise QgsProcessingException(str(e))
            source = "local:" + cm.mosaic_key(tiles)
        else:
            source = "3dep"

        alb = QgsCoordinateReferenceSystem("EPSG:5070")
        tr = QgsCoordinateTransform(dams.sourceCrs(), alb, QgsProject.instance())
        total = dams.featureCount() or 1
        ok = fail = 0
        for i, f in enumerate(dams.getFeatures()):
            if feedback.isCanceled(): break
            feedback.setProgress(100 * i / total)
            dam_id = str(f["dam_id"]) if "dam_id" in dams.fields().names() else str(f.id())
            dst = os.path.join(out, f"dam_{dam_id}.tif")
            g = f.geometry(); g.transform(tr); p = g.asPoint()
            ext = (p.x() - radius, p.y() - radius, p.x() + radius, p.y() + radius)
            # 0.17.0: reused only when cut for this extent, cell and source (was: any file)
            reuse, why = cm.clip_reusable(dst, ext, cell, source)
            if reuse:
                ok += 1; continue
            if os.path.exists(dst):
                feedback.pushInfo("dam {}: dam_{}.tif cut again ({})".format(dam_id, dam_id, why))
                cm.drop_clip(dst)
            try:
                if use_local:
                    GS.checked(gdal.Warp(dst, vrt, dstSRS="EPSG:5070", outputBounds=ext,
                                         xRes=cell, yRes=cell, resampleAlg="bilinear",
                                         outputType=gdal.GDT_Float32, dstNodata=-9999,
                                         creationOptions=["COMPRESS=DEFLATE"]), "clipping the DEM")
                else:
                    # 0.16.0: through the suite terrain cache when one is set up (the files handed
                    # back are this request's own, never cache tiles - removed below)
                    tiles = terrain.fetch_3dep(ext, cell, os.path.join(out, "_tmp"), f"d{dam_id}",
                                               log=feedback.pushInfo)
                    GS.checked(gdal.Warp(dst, tiles, dstNodata=-9999,
                                         creationOptions=["COMPRESS=DEFLATE"]), "mosaicking the 3DEP tiles")
                    for t in tiles: os.remove(t)
                terrain.set_unit_m(dst)                       # 0.17.0: tagged as metres
                cm.write_clip_meta(dst, ext, cell, source)
                ok += 1
            except Exception as e:  # keep the batch going
                fail += 1
                feedback.reportError("dam {}: {}".format(dam_id, er.plain(e, dam_id, "2")))
        feedback.pushInfo(f"terrain cached: {ok} ok, {fail} failed")
        return {self.OUT: out}
