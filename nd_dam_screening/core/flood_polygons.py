"""0.26.0: flood polygons from the step 7 rasters, in one GeoPackage per run
(<output folder>/7_flood_polygons.gpkg) with four layers, all EPSG:5070:

    extent          dam_id, scenario             the wet area
    velocity_class  dam_id, scenario, vel_class  "0-2 ft/s", "2-5 ft/s", "over 5 ft/s" (wet cells)
    arrival         dam_id, scenario, within_hr  the wet area reached within 0.5, 1, 2, 4 and 8 h
    arrival_contour dam_id, scenario, within_hr, label   0.28.0: the same front as LINES at 30
                                                 min, 1 h and 2 h - the times an emergency action
                                                 plan is measured against (core/arrival.py)

Wet = depth >= the suite's wet depth (core/hazard.DEFAULT_WET_M, 0.1 ft), the same allowance the
hazard step uses, not 1 ft. One multipolygon feature per dam, scenario (and class / hour).

The masks are pure numpy (tested without GDAL); the polygons come from gdal.Polygonize on an
in-memory byte mask and are appended with OGR. Before a dam + scenario is written, its existing
features are deleted from each layer, so a rerun does not duplicate. Step 7 runs its dams in
worker threads: `write_flood_polygons` must be called on the main thread only (the on_done
callback), never inside a worker.
"""
import os

import numpy as np

try:
    from osgeo import gdal, ogr, osr
except Exception:                                   # pragma: no cover - no GDAL at all
    gdal = ogr = osr = None

try:                                                # package import (QGIS, the package tests)
    from .hazard import DEFAULT_WET_M
    from .arrival import CONTOUR_HOURS, LAYER_CONTOUR, label_for as arrival_label
except ImportError:                                 # core/ on sys.path as a plain folder
    from hazard import DEFAULT_WET_M
    from arrival import CONTOUR_HOURS, LAYER_CONTOUR, label_for as arrival_label

FT = 0.3048
NODATA = -9999.0
GPKG_NAME = "7_flood_polygons.gpkg"
VEL_CLASSES = (("0-2 ft/s", 0.0, 2.0), ("2-5 ft/s", 2.0, 5.0), ("over 5 ft/s", 5.0, None))   # ft/s
ARRIVAL_HOURS = (0.5, 1.0, 2.0, 4.0, 8.0)
LAYER_EXTENT, LAYER_VEL, LAYER_ARRIVAL = "extent", "velocity_class", "arrival"


# ---------------------------------------------------------------- the masks (numpy only)
def wet_mask(depth, wet_m=DEFAULT_WET_M, nodata=NODATA):
    """Cells with a valid depth of at least `wet_m` (the suite's wet depth, 0.1 ft)."""
    d = np.asarray(depth, float)
    return np.isfinite(d) & (d > nodata + 1) & (d >= wet_m)


def velocity_class_masks(vel_ms, wet):
    """[(label, mask)] for VEL_CLASSES: the wet cells whose peak velocity (m/s in, judged in ft/s)
    falls in each class; the top class is open-ended."""
    v = np.asarray(vel_ms, float) / FT
    out = []
    for label, lo, hi in VEL_CLASSES:
        m = wet & np.isfinite(v) & (v >= lo)
        if hi is not None:
            m = m & (v < hi)
        out.append((label, m))
    return out


def arrival_masks(arrive_h, wet, hours=ARRIVAL_HOURS, nodata=NODATA):
    """[(hours, mask)]: the wet cells the flood reached within each of `hours` (cumulative; a
    cell with no arrival time, the raster's no-data, is left out)."""
    a = np.asarray(arrive_h, float)
    ok = wet & np.isfinite(a) & (a > nodata + 1) & (a >= 0)
    return [(float(h), ok & (a <= h)) for h in hours]


# ---------------------------------------------------------------- GDAL / OGR
def available():
    """True when this GDAL can polygonize and write a GeoPackage (the tests' doubles cannot)."""
    try:
        return (gdal is not None and hasattr(gdal, "Polygonize")
                and ogr.GetDriverByName("GPKG") is not None)
    except Exception:                                            # noqa
        return False


def _srs():
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(5070)
    return srs


def polygonize(mask, gt):
    """The OGR polygons of the True cells of `mask` (geotransform `gt`, EPSG:5070), from
    gdal.Polygonize on an in-memory byte raster that is also its own mask (0 cells skipped)."""
    m = np.asarray(mask, bool)
    if m.ndim != 2 or not m.any():
        return []
    ny, nx = m.shape
    src = gdal.GetDriverByName("MEM").Create("", int(nx), int(ny), 1, gdal.GDT_Byte)
    if src is None:
        raise RuntimeError("GDAL could not make the in-memory mask raster")
    src.SetGeoTransform(tuple(float(x) for x in gt))
    srs = _srs()
    src.SetProjection(srs.ExportToWkt())
    band = src.GetRasterBand(1)
    band.WriteArray(m.astype(np.uint8))
    vds = (ogr.GetDriverByName("MEM") or ogr.GetDriverByName("Memory")).CreateDataSource("flood")
    lyr = vds.CreateLayer("flood", srs, ogr.wkbPolygon)
    lyr.CreateField(ogr.FieldDefn("v", ogr.OFTInteger))
    gdal.Polygonize(band, band, lyr, 0, [], callback=None)
    out = []
    lyr.ResetReading()
    for f in lyr:
        g = f.GetGeometryRef()
        if g is not None and not g.IsEmpty():
            out.append(g.Clone())
    return out


def _open_gpkg(path):
    drv = ogr.GetDriverByName("GPKG")
    if drv is None:
        raise RuntimeError("this GDAL has no GeoPackage driver")
    ds = drv.Open(path, 1) if os.path.exists(path) else None
    if ds is None:
        ds = drv.CreateDataSource(path)
    if ds is None:
        raise RuntimeError("GDAL could not open or create {}".format(path))
    return ds


def _layer(ds, name, extra=()):
    lyr = ds.GetLayerByName(name)
    if lyr is None:
        lyr = ds.CreateLayer(name, _srs(), ogr.wkbMultiPolygon)
        if lyr is None:
            raise RuntimeError("GDAL could not make the layer {}".format(name))
        lyr.CreateField(ogr.FieldDefn("dam_id", ogr.OFTString))
        lyr.CreateField(ogr.FieldDefn("scenario", ogr.OFTString))
        for fname, ftype in extra:
            lyr.CreateField(ogr.FieldDefn(fname, ogr.OFTString if ftype == "str" else ogr.OFTReal))
    return lyr


def _line_layer(ds, name, extra=()):
    """0.28.0: the arrival-contour layer - lines, not polygons (the front at 30 min, 1 h, 2 h)."""
    lyr = ds.GetLayerByName(name)
    if lyr is None:
        lyr = ds.CreateLayer(name, _srs(), ogr.wkbMultiLineString)
        if lyr is None:
            raise RuntimeError("GDAL could not make the layer {}".format(name))
        lyr.CreateField(ogr.FieldDefn("dam_id", ogr.OFTString))
        lyr.CreateField(ogr.FieldDefn("scenario", ogr.OFTString))
        for fname, ftype in extra:
            lyr.CreateField(ogr.FieldDefn(fname, ogr.OFTString if ftype == "str" else ogr.OFTReal))
    return lyr


def contour_lines(mask, gt):
    """The outline of the True cells of `mask` as line geometries: the arrival-time contour is
    the boundary of "reached within this time". A polygon boundary, not a marching-squares
    contour, so the line follows the cell edges the model actually resolved - at a 50 m cell a
    smoothed contour would claim a precision the flood does not have."""
    out = []
    for g in polygonize(mask, gt):
        try:
            b = g.Boundary()
        except Exception:                                        # noqa - no boundary: skip it
            b = None
        if b is not None and not b.IsEmpty():
            out.append(b.Clone())
    return out


def _add_lines(lyr, geoms, attrs):
    """One multilinestring feature of `geoms`; 0 when there is nothing to add."""
    if not geoms:
        return 0
    multi = ogr.Geometry(ogr.wkbMultiLineString)
    for g in geoms:
        t = g.GetGeometryType()
        if t in (ogr.wkbMultiLineString, ogr.wkbMultiLineString25D, ogr.wkbGeometryCollection):
            for i in range(g.GetGeometryCount()):
                multi.AddGeometry(g.GetGeometryRef(i))
        else:
            multi.AddGeometry(g)
    f = ogr.Feature(lyr.GetLayerDefn())
    f.SetGeometry(multi)
    for k, v in attrs:
        f.SetField(k, v)
    if lyr.CreateFeature(f) != 0:
        raise RuntimeError("GDAL could not add a feature to {}".format(lyr.GetName()))
    return 1


def _sql_text(v):
    return "'" + str(v).replace("'", "''") + "'"


def _delete_existing(lyr, dam_id, scenario):
    """Delete the features of this dam + scenario (a rerun replaces, never duplicates)."""
    lyr.SetAttributeFilter("dam_id = {} AND scenario = {}".format(_sql_text(dam_id), _sql_text(scenario)))
    lyr.ResetReading()
    fids = [f.GetFID() for f in lyr]
    lyr.SetAttributeFilter(None)
    for fid in fids:
        lyr.DeleteFeature(fid)
    return len(fids)


def _add(lyr, polys, attrs):
    """One multipolygon feature of `polys` with the attributes; 0 when there is nothing to add."""
    if not polys:
        return 0
    multi = ogr.Geometry(ogr.wkbMultiPolygon)
    for g in polys:
        if g.GetGeometryType() in (ogr.wkbMultiPolygon, ogr.wkbMultiPolygon25D):
            for i in range(g.GetGeometryCount()):
                multi.AddGeometry(g.GetGeometryRef(i))
        else:
            multi.AddGeometry(g)
    f = ogr.Feature(lyr.GetLayerDefn())
    f.SetGeometry(multi)
    for k, v in attrs:
        f.SetField(k, v)
    if lyr.CreateFeature(f) != 0:
        raise RuntimeError("GDAL could not add a feature to {}".format(lyr.GetName()))
    return 1


def write_flood_polygons(gpkg_path, dam_id, scenario, depth, vel_ms, arrive_h, gt,
                         wet_m=DEFAULT_WET_M, nodata=NODATA):
    """Write the extent, velocity-class and arrival polygons of one dam and scenario into the
    run's GeoPackage (made when missing; the dam + scenario's earlier features replaced).
    depth (m), vel_ms (m/s) and arrive_h (hours, no-data = dry) are the step 7 grids on the
    geotransform `gt` (EPSG:5070). Main thread only. Returns the feature counts
    {"extent", "velocity_class", "arrival", "deleted"}."""
    dam_id, scenario = str(dam_id), str(scenario)
    wet = wet_mask(depth, wet_m, nodata)
    counts = {"deleted": 0}
    ds = _open_gpkg(gpkg_path)
    try:
        ident = [("dam_id", dam_id), ("scenario", scenario)]
        lyr = _layer(ds, LAYER_EXTENT)
        counts["deleted"] += _delete_existing(lyr, dam_id, scenario)
        counts[LAYER_EXTENT] = _add(lyr, polygonize(wet, gt), ident)
        lyr = _layer(ds, LAYER_VEL, (("vel_class", "str"),))
        counts["deleted"] += _delete_existing(lyr, dam_id, scenario)
        n = 0
        for label, m in velocity_class_masks(vel_ms, wet):
            n += _add(lyr, polygonize(m, gt), ident + [("vel_class", label)])
        counts[LAYER_VEL] = n
        lyr = _layer(ds, LAYER_ARRIVAL, (("within_hr", "real"),))
        counts["deleted"] += _delete_existing(lyr, dam_id, scenario)
        n = 0
        for h, m in arrival_masks(arrive_h, wet, nodata=nodata):
            n += _add(lyr, polygonize(m, gt), ident + [("within_hr", h)])
        counts[LAYER_ARRIVAL] = n
        # 0.28.0: the same arrival grid as CONTOUR LINES at the times an emergency action plan is
        # measured against (30 min, 1 h, 2 h) - a map of where the front is, not of what is wet
        lyr = _line_layer(ds, LAYER_CONTOUR, (("within_hr", "real"), ("label", "str")))
        counts["deleted"] += _delete_existing(lyr, dam_id, scenario)
        n = 0
        for h, m in arrival_masks(arrive_h, wet, hours=CONTOUR_HOURS, nodata=nodata):
            n += _add_lines(lyr, contour_lines(m, gt),
                            ident + [("within_hr", h), ("label", arrival_label(h))])
        counts[LAYER_CONTOUR] = n
    finally:
        ds = None
    return counts
