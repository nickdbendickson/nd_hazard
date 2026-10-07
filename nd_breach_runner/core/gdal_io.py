"""GDAL side: read the (statewide) DEM into the model's metric CRS and write
GeoTIFFs. Works offline on local files, VRT mosaics included."""
import os

import numpy as np

from .grid import Grid, NODATA
from .units import FT, US_FT
from ..damsuite import gdalsafe as GS

Z_AUTO, Z_FEET, Z_US_FEET, Z_METERS = "auto", "feet", "us_feet", "meters"
Z_CHOICES = (Z_AUTO, Z_FEET, Z_US_FEET, Z_METERS)

_FEET_NAMES = ("ft", "foot", "feet", "international foot", "intl foot")
_US_FEET_NAMES = ("us survey foot", "us-ft", "ftus", "us_ft", "foot_us", "us foot",
                  "us survey feet", "ft (us)", "survey foot")
_METER_NAMES = ("m", "metre", "meter", "metres", "meters")


def _gdal():
    # 3.4.1: no gdal.UseExceptions() here any more - it is process-wide and changed GDAL for
    # QGIS and the other plugins. Calls go through damsuite.gdalsafe (same errors, both modes).
    from osgeo import gdal, osr
    return gdal, osr


def z_to_m_factor(path, choice=Z_AUTO):
    """Factor that converts the DEM's elevations to metres, and how it was
    decided."""
    if choice == Z_FEET:
        return FT, "feet (chosen)"
    if choice == Z_US_FEET:
        return US_FT, "US survey feet (chosen)"
    if choice == Z_METERS:
        return 1.0, "metres (chosen)"
    gdal, osr = _gdal()
    ds = GS.open_raster(path)
    unit = (ds.GetRasterBand(1).GetUnitType() or "").strip().lower()
    if unit in _US_FEET_NAMES:
        return US_FT, "US survey feet (raster unit '{}')".format(unit)
    if unit in _FEET_NAMES:
        return FT, "feet (raster unit '{}')".format(unit)
    if unit in _METER_NAMES:
        return 1.0, "metres (raster unit '{}')".format(unit)
    srs = osr.SpatialReference()
    wkt = ds.GetProjection()
    if wkt:
        srs.ImportFromWkt(wkt)
        vert = None
        try:
            if srs.IsCompound():
                vert = srs.GetTargetLinearUnits("VERT_CS")
        except Exception:
            vert = None
        lin = vert or (srs.GetLinearUnits() if srs.IsProjected() else None)
        if lin and (abs(lin - US_FT) < 1e-7 or abs(lin - FT) < 1e-7):
            f, name = (US_FT, "US survey feet") if abs(lin - US_FT) < 1e-7 else (FT, "feet")
            # 3.10.0: a feet CRS alone does not make feet elevations - a 3DEP metre DEM warped
            # to ND State Plane feet keeps metre values. The elevations decide when only one
            # reading lands in North Dakota's ground range.
            med = _median_value(ds)
            if med is not None:
                as_ft, as_m = _nd_ground(med * f), _nd_ground(med)
                if as_m and not as_ft:
                    return 1.0, ("metres (no unit in the raster; its {} CRS suggests {}, but a "
                                 "median of {:.0f} is below North Dakota's ground in {}; "
                                 "check)").format(name, name, med, name)
                if as_ft and not as_m:
                    return f, ("{} (no unit in the raster; the CRS is in {} and a median of "
                               "{:.0f} fits North Dakota's ground only as {}; check)").format(
                                   name, name, med, name)
            return f, ("{} (assumed from the CRS units; the elevations fit both feet and "
                       "metres - set the units if this is wrong)").format(name)
    return 1.0, "metres (no units recorded in the raster; check)"


ND_GROUND_M = (200.0, 1100.0)      # North Dakota: 229 m (Pembina) to 1069 m (White Butte)


def _nd_ground(z_m):
    return ND_GROUND_M[0] <= z_m <= ND_GROUND_M[1]


def _median_value(ds, size=256):
    """Median of the DEM's valid cells from a decimated read, or None."""
    try:
        b = ds.GetRasterBand(1)
        nx, ny = ds.RasterXSize, ds.RasterYSize
        a = b.ReadAsArray(0, 0, nx, ny, buf_xsize=min(nx, size),
                          buf_ysize=min(ny, size)).astype(np.float64)
        nd = b.GetNoDataValue()
        ok = np.isfinite(a) & (a > -1e5) & (a < 1e5)
        if nd is not None:
            ok &= a != nd
        return float(np.median(a[ok])) if ok.any() else None
    except Exception:
        return None


def utm_epsg(lon, lat):
    """NAD83 / UTM zone N for a longitude (North Dakota: zones 13 and 14)."""
    zone = int((lon + 180.0) // 6.0) + 1
    if lat < 0 or not (1 <= zone <= 23):
        return 32600 + zone if lat >= 0 else 32700 + zone
    return 26900 + zone


class GdalTerrain:
    """terrain_src for workflow.run_dam."""

    def __init__(self, dem_path, work_srs, z_choice=Z_AUTO, manning_path=None,
                 default_n=0.06, resample="bilinear", manning_lut=None):
        gdal, _ = _gdal()
        self.dem_path = dem_path
        self.work_srs = work_srs          # "EPSG:26914" or WKT
        self.z_to_m, self.z_note = z_to_m_factor(dem_path, z_choice)
        self.manning_path = manning_path
        self.manning_lut = manning_lut       # set: the raster holds NLCD class codes
        self.default_n = default_n
        self.resample = resample
        ds = GS.open_raster(dem_path)
        self.src_nodata = ds.GetRasterBand(1).GetNoDataValue()

    def _warp(self, path, bounds, cell, alg, src_nodata=None):
        gdal, _ = _gdal()
        xmin, ymin, xmax, ymax = bounds
        nx = int(round((xmax - xmin) / cell))
        ny = int(round((ymax - ymin) / cell))
        xmax, ymin = xmin + nx * cell, ymax - ny * cell
        opts = gdal.WarpOptions(format="MEM", dstSRS=self.work_srs,
                                outputBounds=(xmin, ymin, xmax, ymax), width=nx, height=ny,
                                resampleAlg=alg, dstNodata=NODATA, outputType=gdal.GDT_Float32,
                                srcNodata=src_nodata, multithread=True)
        out = GS.checked(gdal.Warp("", path, options=opts), "warping {}".format(path))
        a = out.GetRasterBand(1).ReadAsArray().astype(np.float64)
        a[a == NODATA] = np.nan
        return Grid(a, xmin, ymax, cell, crs_wkt=out.GetProjection())

    def read(self, bounds, cell):
        g = self._warp(self.dem_path, bounds, cell, self.resample, self.src_nodata)
        g.data = g.data * self.z_to_m
        return g

    def manning(self, bounds, cell):
        if not self.manning_path:
            return None
        g = self._warp(self.manning_path, bounds, cell, "near")
        if self.manning_lut:
            from . import nd_data
            lut = dict(self.manning_lut)
            lut["default"] = self.default_n
            g.data = nd_data.nlcd_to_manning(g.data, lut)
            return g
        bad = ~np.isfinite(g.data) | (g.data <= 0) | (g.data > 1.0)
        g.data[bad] = self.default_n
        return g


class GeoTiffWriter:
    """writer for workflow.run_dam: writes the grid in the model CRS, then
    (optionally) re-projects it to the DEM's CRS with nearest-neighbour."""

    def __init__(self, work_srs, target_srs=None, target_res=None):
        self.work_srs = work_srs
        self.target_srs = target_srs
        self.target_res = target_res

    def __call__(self, grid, path):
        gdal, osr = _gdal()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".work.tif" if self.target_srs else path
        drv = gdal.GetDriverByName("GTiff")
        if os.path.exists(tmp):
            drv.Delete(tmp)
        ds = GS.checked(drv.Create(tmp, grid.nx, grid.ny, 1, gdal.GDT_Float32,
                                   options=["COMPRESS=DEFLATE", "TILED=YES", "PREDICTOR=3"]),
                        "creating {}".format(tmp))
        ds.SetGeoTransform((grid.x0, grid.dx, 0.0, grid.y0, 0.0, -grid.dx))
        srs = osr.SpatialReference()
        srs.SetFromUserInput(self.work_srs)
        ds.SetProjection(srs.ExportToWkt())
        b = ds.GetRasterBand(1)
        b.SetNoDataValue(NODATA)
        a = np.where(np.isfinite(grid.data), grid.data, NODATA).astype(np.float32)
        b.WriteArray(a)
        b.FlushCache()
        ds = None
        if self.target_srs:
            if os.path.exists(path):
                drv.Delete(path)
            kw = dict(dstSRS=self.target_srs, resampleAlg="near", srcNodata=NODATA,
                      dstNodata=NODATA, creationOptions=["COMPRESS=DEFLATE", "TILED=YES"])
            if self.target_res:
                kw.update(xRes=self.target_res, yRes=self.target_res)
            GS.checked(gdal.Warp(path, tmp, options=gdal.WarpOptions(**kw)),
                       "reprojecting {}".format(path))
            drv.Delete(tmp)
        return path


def same_crs(wkt_a, wkt_b):
    _, osr = _gdal()
    a, b = osr.SpatialReference(), osr.SpatialReference()
    try:
        a.SetFromUserInput(wkt_a)
        b.SetFromUserInput(wkt_b)
        return bool(a.IsSame(b))
    except Exception:
        return False


def linear_unit_m(wkt):
    """Metres per CRS unit, or None for geographic CRSs."""
    if not wkt:
        return None
    _, osr = _gdal()
    s = osr.SpatialReference()
    try:
        s.SetFromUserInput(wkt)
    except Exception:
        return None
    return s.GetLinearUnits() if s.IsProjected() else None


# --------------------------------------------------------------------------------------
# statewide LiDAR: tiles -> one VRT, gaps -> filled
# --------------------------------------------------------------------------------------
RASTER_EXT = (".tif", ".tiff", ".img", ".asc", ".vrt", ".dt2", ".hgt", ".bil")


def find_rasters(folder, recursive=True, exts=RASTER_EXT):
    out = []
    if recursive:
        for root, _d, files in os.walk(folder):
            out += [os.path.join(root, f) for f in files if f.lower().endswith(exts)]
    else:
        out = [os.path.join(folder, f) for f in os.listdir(folder) if f.lower().endswith(exts)]
    return sorted(out)


def build_vrt(sources, out_path, nodata=None, resolution="highest"):
    """Mosaic tiles (a folder or a list of files) into a VRT without copying
    any data. Returns a dict describing what went in and any problems: tiles
    in different CRSs cannot be mosaicked and are left out and listed."""
    gdal, osr = _gdal()
    if isinstance(sources, str):
        files = find_rasters(sources) if os.path.isdir(sources) else [sources]
        if os.path.isdir(sources):      # a mosaic or filled copy in the tile folder is no tile
            files = [f for f in files if not f.lower().endswith(".vrt")
                     and "_filled" not in os.path.basename(f).lower()]
    else:
        files = list(sources)
    files = [f for f in files if os.path.abspath(f) != os.path.abspath(out_path)]
    if not files:
        raise ValueError("no raster tiles found")
    info, crs_groups, known = [], {}, []
    for f in files:
        try:
            ds = GS.open_raster(f)
        except RuntimeError as e:
            info.append({"file": f, "problem": str(e)[:100]})
            continue
        gt = ds.GetGeoTransform()
        wkt = ds.GetProjection()
        srs = osr.SpatialReference()
        key = "none"
        if wkt:
            srs.SetFromUserInput(wkt)
            for k, other in known:      # the same CRS however it is labelled
                if srs.IsSame(other):
                    key = k
                    break
            else:
                key = srs.GetAuthorityCode(None) or srs.GetName() or "crs{}".format(len(known))
                if any(k == key for k, _o in known):
                    key = "{} ({})".format(key, len(known))
                known.append((key, srs))
        b = ds.GetRasterBand(1)
        rec = {"file": f, "res": abs(gt[1]), "crs": key, "nodata": b.GetNoDataValue(),
               "unit": (b.GetUnitType() or ""), "size": (ds.RasterXSize, ds.RasterYSize),
               "bounds": (gt[0], gt[3] + gt[5] * ds.RasterYSize, gt[0] + gt[1] * ds.RasterXSize,
                          gt[3])}
        info.append(rec)
        crs_groups.setdefault(key, []).append(rec)
    good = [r for r in info if "crs" in r]
    if not good:
        raise ValueError("none of the tiles could be opened")
    main_key = max(crs_groups, key=lambda k: len(crs_groups[k]))
    use = crs_groups[main_key]
    left_out = [r["file"] for k, v in crs_groups.items() if k != main_key for r in v]
    kw = {"resolution": resolution}
    nd = nodata if nodata is not None else use[0]["nodata"]
    if nd is not None:
        kw["srcNodata"] = nd
        kw["VRTNodata"] = nd
    if os.path.exists(out_path):
        os.remove(out_path)
    vrt = GS.checked(gdal.BuildVRT(out_path, [r["file"] for r in use],
                                   options=gdal.BuildVRTOptions(**kw)), "building {}".format(out_path))
    vrt = None
    res = sorted({round(r["res"], 4) for r in use})
    units = sorted({r["unit"] for r in use})
    if len(units) == 1 and units[0]:        # keep the elevation unit on the mosaic
        try:
            v = GS.open_raster(out_path, update=True)
            v.GetRasterBand(1).SetUnitType(units[0])
            v = None
        except Exception:
            pass
    b = [r["bounds"] for r in use]
    notes = []
    if left_out:
        notes.append("{} tile(s) in a different CRS than the majority ({}) were left out; "
                     "reproject them first: {}".format(len(left_out), main_key,
                                                       ", ".join(os.path.basename(x)
                                                                 for x in left_out[:5])))
    if len(res) > 1:
        notes.append("the tiles have different cell sizes {}; the VRT uses the {}".format(
            res, resolution))
    if len(units) > 1:
        notes.append("the tiles record different elevation units {}".format(units))
    nds = sorted({str(r["nodata"]) for r in use})
    if len(nds) > 1:
        notes.append("the tiles have different no-data values {}; only {} is applied, so "
                     "voids in the others read as terrain. Give the no-data value "
                     "explicitly or fix the tiles.".format(nds, nd))
    return {"vrt": out_path, "tiles": len(use), "crs": main_key, "cell_sizes": res,
            "elevation_units": units, "bounds": (min(x[0] for x in b), min(x[1] for x in b),
                                                 max(x[2] for x in b), max(x[3] for x in b)),
            "left_out": left_out, "problems": [r for r in info if "problem" in r],
            "notes": notes}


def fill_gaps(src_path, out_path, extent=None, max_distance_px=100, smoothing=0):
    """Fill no-data holes (water bodies, missing returns) by interpolation
    from the edges, on the whole raster or only `extent` (xmin, ymin, xmax,
    ymax in the raster's CRS). The whole file is read into memory, so clip to
    the area of interest for a statewide mosaic. Returns
    {"out", "cells_filled", "cells_left"}."""
    gdal, _ = _gdal()
    ds = GS.open_raster(src_path)
    nodata = ds.GetRasterBand(1).GetNoDataValue()
    if nodata is None:
        raise ValueError("the raster has no no-data value, so there are no gaps to fill")
    kw = {"format": "GTiff", "creationOptions": ["COMPRESS=DEFLATE", "TILED=YES",
                                                 "BIGTIFF=IF_SAFER"]}
    if extent:
        kw["projWin"] = (extent[0], extent[3], extent[2], extent[1])
    tmp = out_path + ".tmp.tif"
    GS.checked(gdal.Translate(tmp, ds, options=gdal.TranslateOptions(**kw)), "copying {}".format(src_path))
    o = GS.open_raster(tmp, update=True)
    band = o.GetRasterBand(1)
    a0 = band.ReadAsArray()
    holes0 = int((a0 == nodata).sum() if not np.isnan(nodata) else np.isnan(a0).sum())
    mask = o.GetRasterBand(1).GetMaskBand()
    gdal.FillNodata(band, mask, max_distance_px, smoothing)
    band.FlushCache()
    a1 = band.ReadAsArray()
    left = int((a1 == nodata).sum() if not np.isnan(nodata) else np.isnan(a1).sum())
    o = None
    ds = None
    if os.path.exists(out_path):
        os.remove(out_path)
    os.replace(tmp, out_path)
    return {"out": out_path, "cells_filled": holes0 - left, "cells_left": left}


def nlcd_to_manning_raster(nlcd_path, out_path, lut=None, like_path=None):
    """Write a Manning's n raster from an NLCD class raster (offline). The
    output is on the NLCD grid unless `like_path` is given (a DEM, say), in
    which case it is resampled (nearest) onto that grid first."""
    from . import nd_data
    gdal, _ = _gdal()
    src = GS.open_raster(nlcd_path)
    if like_path:
        like = GS.open_raster(like_path)
        gt = like.GetGeoTransform()
        opts = gdal.WarpOptions(format="MEM", dstSRS=like.GetProjection(), xRes=abs(gt[1]),
                                yRes=abs(gt[5]), outputBounds=(
            gt[0], gt[3] + gt[5] * like.RasterYSize, gt[0] + gt[1] * like.RasterXSize, gt[3]),
            resampleAlg="near", outputType=gdal.GDT_Float32)
        src = GS.checked(gdal.Warp("", src, options=opts), "warping {}".format(nlcd_path))
    band = src.GetRasterBand(1)
    codes = band.ReadAsArray().astype(np.float64)
    nd = band.GetNoDataValue()
    if nd is not None:
        codes[codes == nd] = np.nan
    n = nd_data.nlcd_to_manning(codes, lut or nd_data.manning_lut()).astype(np.float32)
    drv = gdal.GetDriverByName("GTiff")
    if os.path.exists(out_path):
        drv.Delete(out_path)
    out = GS.checked(drv.Create(out_path, src.RasterXSize, src.RasterYSize, 1, gdal.GDT_Float32,
                                options=["COMPRESS=DEFLATE", "TILED=YES"]), "creating {}".format(out_path))
    out.SetGeoTransform(src.GetGeoTransform())
    out.SetProjection(src.GetProjection())
    out.GetRasterBand(1).WriteArray(n)
    out.GetRasterBand(1).SetNoDataValue(-9999)
    out = None
    seen = sorted({int(c) for c in np.unique(codes[np.isfinite(codes)])})
    unknown = [c for c in seen if c not in (lut or nd_data.NLCD_N)]
    return {"out": out_path, "classes": seen, "unknown_classes": unknown}


def read_like(path, grid, alg="bilinear"):
    """Read any raster onto the grid of `grid` (a core.Grid, model CRS) as an
    array with NaN for no data. Used to compare with another model's rasters."""
    gdal, _ = _gdal()
    xmin, ymin, xmax, ymax = grid.bounds
    opts = gdal.WarpOptions(format="MEM", dstSRS=grid.crs_wkt or None,
                            outputBounds=(xmin, ymin, xmax, ymax), width=grid.nx,
                            height=grid.ny, resampleAlg=alg, dstNodata=NODATA,
                            outputType=gdal.GDT_Float32)
    out = GS.checked(gdal.Warp("", path, options=opts), "warping {}".format(path))
    a = out.GetRasterBand(1).ReadAsArray().astype(np.float64)
    a[a == NODATA] = np.nan
    return a


def read_grid(path):
    """A raster file as a core Grid (north-up, square cells assumed), NaN for
    no data, with its CRS."""
    gdal, _ = _gdal()
    ds = GS.open_raster(path)
    gt = ds.GetGeoTransform()
    b = ds.GetRasterBand(1)
    raw = b.ReadAsArray()
    a = raw.astype(np.float64)
    nd = b.GetNoDataValue()
    if nd is not None:
        if np.isnan(nd):
            pass
        elif raw.dtype.kind == "f":      # the file's own precision: -3.4e38 is not exact in f32
            a[raw == raw.dtype.type(nd)] = np.nan
        else:
            a[a == nd] = np.nan
    a[~np.isfinite(a)] = np.nan
    if abs(abs(gt[1]) - abs(gt[5])) > 1e-6 * abs(gt[1]) or gt[2] or gt[4] or gt[5] > 0:
        raise ValueError("{}: the cells are not square and north-up".format(path))
    return Grid(a, gt[0], gt[3], abs(gt[1]), crs_wkt=ds.GetProjection())


def epsg_of(wkt):
    """'EPSG:26914' for a CRS when it has a code, else None."""
    _, osr = _gdal()
    s = osr.SpatialReference()
    try:
        s.SetFromUserInput(wkt)
        code = s.GetAuthorityCode(None)
        if not code:
            try:
                s.AutoIdentifyEPSG()
            except Exception:
                pass
            code = s.GetAuthorityCode(None)
        return "EPSG:{}".format(code) if code else None
    except Exception:
        return None
