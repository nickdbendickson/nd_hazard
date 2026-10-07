# -*- coding: utf-8 -*-
"""Project-layer discovery, raster samplers and the ACER TM 11 zone
rasters (Hazard_Foundations / Hazard_MobileHomes / Hazard_Vehicles)."""

import os
import re
import math

import numpy as np

from qgis.core import (
    QgsProject, QgsVectorLayer, QgsRasterLayer, QgsRectangle, QgsWkbTypes,
    QgsCoordinateTransform, QgsCsException,
)

from osgeo import gdal

from .compat import MSG_INFO, MSG_WARNING, GEOM_POINT, GEOM_LINE, GEOM_POLYGON
from .constants import (
    CLASS_NODATA, HAZARD_BLOCK_TARGET_CELLS, HAZARD_OVERVIEW_MIN_SIZE, vehicle_curve_available,
)
from .helpers import (
    log_message, get_layer_path, classify_values, curve_pair,
    log_threshold_verification, apply_acer11_style, add_layer_to_project,
    RasterSampler, buffer_distance_in_layer_units, gdal_exceptions,
    prepare_output_path, layer_file_path,
)

# NOTE: no module-level gdal.UseExceptions() - that silently changed GDAL
# behaviour for QGIS and every other plugin. See helpers.gdal_exceptions().

# Layer-name rules (matched on the lower-case name with "_", "-", "." read
# as spaces). "nsi"/"fema" must start a word ("Density", "Transit",
# "Female" do not match); "poi"/"pois" must be a whole word ("Points" does
# not match). Names containing "consolidated" are this plugin's outputs.
# FEMA point layers are recognised only so they are NOT taken as another
# group (v2.2: FEMA data enters through the FEMA_Structures outline file;
# FEMA point layers are no longer processed).
# The ND GIS Hub address points arrive under several names: the Hub item
# "NDGISHUB SiteStructure Address Points", the service layer
# "DBO_SiteStructureAddressPoints", or a user's "Address Points" export; a
# generic "address point(s)" name is only used when no NSI / FEMA rule
# matched first.
_GROUP_PATTERNS = (
    ("NSI", re.compile(r"(?<![a-z])nsi")),
    ("NDGISHUB", re.compile(r"gis ?hub|site ?structure")),
    (None, re.compile(r"(?<![a-z])fema(?!le)")),       # ignored (see above)
    ("NDGISHUB", re.compile(r"(?<![a-z])address ?points?(?![a-z])|(?<![a-z])addresspoints?")),
)


def _layer_name_key(name):
    return re.sub(r"[_\-.]+", " ", (name or "").lower())
_OUTPUT_NAME_MARKERS = ("consolidated", "poi_edited", "sample_points", "sample points")

RASTER_KEYS = ("depth", "vel", "arr")


def is_plugin_output_name(name):
    n = (name or "").lower()
    return any(m in n for m in _OUTPUT_NAME_MARKERS)


DATA_INDEX_FILE = "Downloaded_Data.csv"
# written when outputs/Data is created, so a download that stops part-way
# (cancel, error) still marks its layers as downloaded data
DATA_MARKER_FILE = ".nd_dam_downloaded_data"


def is_downloaded_data_layer(lyr):
    """v2.5.1: a layer saved by the Data & Output tab (outputs/Data, next to
    Downloaded_Data.csv). It is a record of the downloads - possibly limited
    to the flooded area - so auto-detection never takes it as an input (the
    runs read the same downloads from their cache). It can still be picked
    by name in a layer list."""
    try:
        from .constants import SUBDIR_DATA
        path = layer_file_path(lyr)
        folder = os.path.dirname(path)
        return bool(path) and os.path.basename(folder) == SUBDIR_DATA and (
            os.path.isfile(os.path.join(folder, DATA_INDEX_FILE))
            or os.path.isfile(os.path.join(folder, DATA_MARKER_FILE)))
    except Exception:
        return False


def discover_project_layers():
    """{"NSI": [...], "NDGISHUB": [...]}: the point layers of the project
    recognised by name. The plugin's own outputs and saved downloads are
    never inputs."""
    layer_dict = {"NSI": [], "NDGISHUB": []}
    for lyr in QgsProject.instance().mapLayers().values():
        if not isinstance(lyr, QgsVectorLayer) or not lyr.isValid():
            continue
        if is_plugin_output_name(lyr.name()) or is_downloaded_data_layer(lyr):
            continue
        if lyr.geometryType() != GEOM_POINT:
            continue
        name = _layer_name_key(lyr.name())
        for key, pattern in _GROUP_PATTERNS:
            if pattern.search(name):
                if key is not None:
                    layer_dict[key].append(lyr)
                break
    return layer_dict


def point_from_geometry(geom):
    """QgsPointXY of a (multi)point; a point on the surface for a line or
    polygon (e.g. a FEMA structure outline); None if not possible."""
    try:
        if geom is None or geom.isNull():
            return None
        gtype = geom.type()
        if gtype in (GEOM_LINE, GEOM_POLYGON):
            p = geom.pointOnSurface()
            return p.asPoint() if p is not None and not p.isNull() else None
        if QgsWkbTypes.isMultiType(geom.wkbType()):
            pts = geom.asMultiPoint()
            return pts[0] if pts else None
        return geom.asPoint()
    except Exception:
        return None


def safe_transform_point(xform, pt):
    """Transform a QgsPointXY; None if it lies outside the CRS's valid area."""
    if pt is None:
        return None
    if xform is None or xform.isShortCircuited():
        return pt
    try:
        return xform.transform(pt)
    except QgsCsException:
        return None


def raster_filter_rect(raster_layer, grow_ft, target_crs, transform_ctx):
    """Raster extent grown by `grow_ft` feet, expressed in `target_crs`
    (None means: do not filter). v1.1 transformed this rectangle in the
    wrong direction, which failed when point and raster CRSs differed."""
    try:
        rect = QgsRectangle(raster_layer.extent())
        rect.grow(buffer_distance_in_layer_units(grow_ft, raster_layer.crs()))
        xf = QgsCoordinateTransform(raster_layer.crs(), target_crs, transform_ctx)
        if xf.isShortCircuited():
            return rect
        out = xf.transformBoundingBox(rect)
        if out is None or out.isEmpty():
            return None
        if not all(math.isfinite(v) for v in (out.xMinimum(), out.yMinimum(),
                                                out.xMaximum(), out.yMaximum())):
            return None
        return out
    except QgsCsException:
        return None
    except Exception as e:
        log_message("Could not build spatial filter ({}); reading all features".format(e),
                    MSG_WARNING)
        return None


def make_samplers(depth_layer, velocity_layer, arrival_layer, buffer_ft):
    samplers = {}
    for key, lyr in zip(RASTER_KEYS, (depth_layer, velocity_layer, arrival_layer)):
        samplers[key] = RasterSampler(lyr, buffer_ft)
        if not samplers[key].ok:
            log_message("Raster '{}' could not be opened for sampling".format(
                lyr.name() if lyr else key), MSG_WARNING)
    return samplers


def close_samplers(samplers):
    for s in (samplers or {}).values():
        try:
            s.close()
        except Exception:
            pass


def _grids_aligned(ds_a, ds_b):
    gt_a, gt_b = ds_a.GetGeoTransform(), ds_b.GetGeoTransform()
    return (ds_a.RasterXSize == ds_b.RasterXSize
            and ds_a.RasterYSize == ds_b.RasterYSize
            and all(abs(a - b) < 1e-9 for a, b in zip(gt_a, gt_b))
            and ds_a.GetProjection() == ds_b.GetProjection())


def _aligned_velocity(ds_d, ds_v, v_nodata):
    """Velocity dataset on the depth grid. Uses a lazy in-memory VRT (reads
    and resamples only the rows being processed) instead of warping the whole
    raster into RAM."""
    if _grids_aligned(ds_d, ds_v):
        return ds_v
    width, height = ds_d.RasterXSize, ds_d.RasterYSize
    gt = ds_d.GetGeoTransform()
    proj = ds_d.GetProjection()
    kwargs = {"resampleAlg": "near"}
    if proj:
        kwargs["dstSRS"] = proj
    if v_nodata is not None:
        kwargs["srcNodata"] = v_nodata
    # cells outside the velocity raster must read as NoData, never as 0 ft/s
    # (the least severe velocity); callers treat <= -9999 as invalid
    dst_nd = v_nodata if v_nodata is not None else -9999.0
    kwargs["dstNodata"] = dst_nd
    north_up = gt[2] == 0 and gt[4] == 0 and gt[5] < 0
    if north_up:
        opts = gdal.WarpOptions(
            format="VRT", width=width, height=height,
            outputBounds=(gt[0], gt[3] + height * gt[5], gt[0] + width * gt[1], gt[3]),
            **kwargs)
        out = gdal.Warp("", ds_v, options=opts)
    else:
        # Rotated / south-up depth grid: warp into a dataset that carries the
        # exact depth geotransform so arrays line up cell for cell.
        out = gdal.GetDriverByName("MEM").Create("", width, height, 1, gdal.GDT_Float32)
        out.SetGeoTransform(gt)
        if proj:
            out.SetProjection(proj)
        band = out.GetRasterBand(1)
        band.SetNoDataValue(dst_nd)
        band.Fill(dst_nd)
        band = None
        gdal.Warp(out, ds_v, options=gdal.WarpOptions(**kwargs))
    if out is None:
        raise RuntimeError("Velocity raster alignment failed")
    return out


def _rows_per_block(width, height):
    rows = max(1, HAZARD_BLOCK_TARGET_CELLS // max(1, width))
    if rows >= 256:
        rows -= rows % 256          # whole output tiles per write
    return int(max(1, min(rows, height)))


def _create_class_raster(driver, path, width, height, gt, proj):
    ds = driver.Create(path, width, height, 1, gdal.GDT_Int16,
                       options=["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_SAFER"])
    if ds is None:
        raise RuntimeError("Could not create {}".format(path))
    ds.SetGeoTransform(gt)
    if proj:
        ds.SetProjection(proj)
    ds.GetRasterBand(1).SetNoDataValue(CLASS_NODATA)
    return ds


# v2.9: overview resampling of the class rasters (zone / band numbers: never
# averaged; "MODE" keeps the most common class but is slower)
ZONE_OVERVIEW_RESAMPLING = "NEAREST"


def _finalize_class_raster(ds, width, height, overviews=True):
    """Flush, (overviews=True) internal overviews on the open dataset -
    rasters larger than HAZARD_OVERVIEW_MIN_SIZE - and statistics.
    generate_hazard_rasters builds its overviews after closing the file
    instead (overviews.build_overviews)."""
    from .overviews import overview_levels
    band = ds.GetRasterBand(1)
    band.FlushCache()
    if overviews and max(width, height) > HAZARD_OVERVIEW_MIN_SIZE:
        levels = overview_levels(width, height)
        if levels:
            try:
                ds.BuildOverviews(ZONE_OVERVIEW_RESAMPLING, levels)
            except Exception as e:
                log_message("Overviews not built: {}".format(e), MSG_WARNING)
    try:
        band.ComputeStatistics(True)
    except Exception as e:
        # e.g. every cell is NoData - not a reason to fail the run
        log_message("Statistics not computed: {}".format(e), MSG_INFO)
    band = None
    ds.FlushCache()


def _scale_offset(band):
    scale, offset = band.GetScale(), band.GetOffset()
    scale = 1.0 if scale in (None, 0) else float(scale)
    offset = 0.0 if offset is None else float(offset)
    return scale, offset


def generate_hazard_rasters(output_folder, depth_layer, velocity_layer,
                            auto_style=True, progress_cb=None, add_to_project=True,
                            include_vehicle=True, group=None, overviews=True):
    """ACER TM 11 zone GeoTIFFs (1 High, 2 Judgment, 3 Low) on the depth
    grid from the maximum depth with the maximum velocity of each cell,
    processed in strips of rows (bounded memory). Returns {kind: path}.
    Hazard_Vehicles.tif (TM 11 Figure 4) is added when the vehicle curve is
    configured. Partial outputs are removed if cancelled or failed.
    add_to_project=False (worker threads): the caller loads the files.
    overviews: v2.9 - internal overviews (ZONE_OVERVIEW_RESAMPLING) in each
    file larger than HAZARD_OVERVIEW_MIN_SIZE px, built with
    overviews.build_overviews after the file is written (a failure is
    logged, never fails the run)."""
    from .hazard import ZONE_FILES
    d_path = get_layer_path(depth_layer)
    v_path = get_layer_path(velocity_layer) if velocity_layer is not None else None
    if not d_path:
        raise RuntimeError("Cannot determine the depth raster file path")
    if not v_path:
        raise RuntimeError("A velocity raster is needed for the ACER zone rasters "
                           "(max depth with max velocity)")

    def tick(pct, msg):
        if progress_cb:
            progress_cb(pct, msg)

    log_threshold_verification()
    os.makedirs(output_folder, exist_ok=True)
    kinds = ["found", "mobile"]
    if include_vehicle and vehicle_curve_available():
        kinds.append("vehicle")
    paths = {k: os.path.join(output_folder, ZONE_FILES[k]) for k in kinds}
    curves = {k: curve_pair(k) for k in kinds}
    _maxmax_rasters(d_path, v_path, paths, curves, kinds, tick, overviews=overviews)

    if add_to_project:
        tick(95, "Loading hazard layers...")
        for key in kinds:
            lyr = QgsRasterLayer(paths[key], "Hazard {}".format(key.capitalize()))
            if lyr.isValid():
                if auto_style:
                    apply_acer11_style(lyr)
                add_layer_to_project(lyr, group=group)
            else:
                log_message("Failed to load hazard raster: {}".format(paths[key]), MSG_WARNING)
    tick(100, "Hazard rasters done")
    return paths


def _zone_overviews(paths, kinds, width, height, tick):
    """v2.9: internal overviews in each written zone raster (large ones)."""
    if max(width, height) <= HAZARD_OVERVIEW_MIN_SIZE:
        return
    from .overviews import build_overviews
    for i, key in enumerate(kinds):
        tick(88 + 6.0 * i / max(1, len(kinds)), "Building overviews: {}...".format(
            os.path.basename(paths[key])))
        ok, text = build_overviews(paths[key], ZONE_OVERVIEW_RESAMPLING, external=False,
                                   compress="LZW")
        if not ok:
            log_message("Zone raster drawn without overviews - {}".format(text), MSG_WARNING)


def _maxmax_rasters(d_path, v_path, paths, curves, kinds, tick, overviews=True):
    with gdal_exceptions():
        ds_d = ds_v = ds_vw = band_d = band_v = None
        outs = {}
        created = []
        try:
            ds_d = gdal.Open(d_path, gdal.GA_ReadOnly)
            ds_v = gdal.Open(v_path, gdal.GA_ReadOnly)
            if ds_d is None or ds_v is None:
                raise RuntimeError("Could not open depth/velocity rasters with GDAL")

            tick(5, "Aligning velocity grid to depth grid...")
            width, height = ds_d.RasterXSize, ds_d.RasterYSize
            gt = ds_d.GetGeoTransform()
            proj = ds_d.GetProjection()
            v_nodata_src = ds_v.GetRasterBand(1).GetNoDataValue()
            ds_vw = _aligned_velocity(ds_d, ds_v, v_nodata_src)

            band_d = ds_d.GetRasterBand(1)
            band_v = ds_vw.GetRasterBand(1)
            d_nodata = band_d.GetNoDataValue()
            v_nodata = band_v.GetNoDataValue()
            if v_nodata is None:
                v_nodata = v_nodata_src
            d_scale, d_offset = _scale_offset(band_d)
            v_scale, v_offset = _scale_offset(ds_v.GetRasterBand(1))

            driver = gdal.GetDriverByName("GTiff")
            for key in kinds:
                paths[key] = prepare_output_path(paths[key])
                outs[key] = _create_class_raster(driver, paths[key], width, height, gt, proj)
                created.append(paths[key])

            rows = _rows_per_block(width, height)
            for y0 in range(0, height, rows):
                h = min(rows, height - y0)
                d = band_d.ReadAsArray(0, y0, width, h)
                v = band_v.ReadAsArray(0, y0, width, h)
                with np.errstate(invalid="ignore"):
                    # same validity rule as RasterSampler: > -9999 as well
                    # (ASCII grids may carry -9999 without a NoData tag)
                    valid = np.isfinite(d) & np.isfinite(v) & (d > -9999) & (v > -9999)
                if d_nodata is not None:
                    valid &= (d != d_nodata)
                if v_nodata is not None:
                    valid &= (v != v_nodata)
                # classify only the valid cells, in float64
                dv = d[valid].astype(np.float64)
                vv = v[valid].astype(np.float64)
                del d, v
                if d_scale != 1.0 or d_offset != 0.0:
                    dv = dv * d_scale + d_offset
                if v_scale != 1.0 or v_offset != 0.0:
                    vv = vv * v_scale + v_offset
                cls = np.empty((h, width), dtype=np.int16)
                for key in kinds:
                    cls.fill(CLASS_NODATA)
                    cls[valid] = classify_values(dv, vv, *curves[key])
                    outs[key].GetRasterBand(1).WriteArray(cls, 0, y0)
                del dv, vv, valid, cls
                tick(10 + 75.0 * (y0 + h) / height,
                     "Classifying hazard zones: row {} of {}".format(y0 + h, height))

            tick(86, "Finalising hazard GeoTIFFs (statistics)...")
            for key in list(outs):
                _finalize_class_raster(outs[key], width, height, overviews=False)
                outs[key] = None                 # closes the file
            outs.clear()
            if overviews:
                _zone_overviews(paths, kinds, width, height, tick)
        except Exception:
            outs.clear()                      # close before deleting
            for p in created:
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except OSError:
                    pass
            raise
        finally:
            band_d = band_v = None
            ds_vw = None
            ds_v = None
            ds_d = None
