"""The suite's shared terrain tile cache (1.5.0).

Every DEM the plugins download from a web service (USGS 3DEP ImageServer, a WCS, an ImageServer the
user added) goes through this cache: the area is split into tiles on ONE fixed grid in EPSG:5070
(tile edges at multiples of TILE_PX x cell from the origin), each tile is downloaded once into
<suite data folder>/terrain/<service>_<cell>m/t_<ix>_<iy>.tif, and a request is answered with a
VRT over the cached tiles. Neighbouring dams, repeat runs, the Screening tools and the Breach Runner
then read the same files instead of downloading the same ground again.

Decisions:
  * The VRT covers the request box snapped OUTWARD to whole cells of the grid, so no cell of a
    tile is resampled by the VRT itself; callers that need an exact box warp it as they warped the
    service's own tiles before (their outputBounds / resampling are unchanged).
  * A tile is written to <tile>.<pid>_<thread>.part and renamed when complete: an interrupted download never
    leaves a half tile in the cache; two runs fetching the same tile at once both succeed (the
    last rename wins, the bytes are the same).
  * NoData in the cache is -9999 (the value the Screening tools already use). `normalize`
    rewrites a downloaded tile with that NoData when GDAL is present (the service's own NoData,
    often -3.4e38, would otherwise be read as an elevation by a VRT without that value).
  * Tiles never expire on their own (terrain does not change between runs); `clear` removes a
    service / cell folder, and `max_age_days` in `get` re-downloads older tiles on request.
  * A failed tile stops the request with the tiles that failed (no DEM with a hole is returned).
  * 1.9.0: a tile whose single request fails is asked for again as 2 x 2 quarters and joined
    (`fetch_split`, needs GDAL); the grid and the tile size do not change, so caches stay valid.
Pure Python standard library; GDAL (osgeo) only in `normalize` / `fetch_split` and only when
installed.
"""
import math
import os
import threading
import time

TILE_PX = 2000                 # tile side in cells (3DEP export allows up to 8000)
NODATA = -9999.0
CRS = "EPSG:5070"
# NAD83 / Conus Albers, as WKT (written into the VRTs)
WKT_5070 = (
    'PROJCS["NAD83 / Conus Albers",GEOGCS["NAD83",DATUM["North_American_Datum_1983",'
    'SPHEROID["GRS 1980",6378137,298.257222101,AUTHORITY["EPSG","7019"]],'
    'AUTHORITY["EPSG","6269"]],PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],'
    'UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],AUTHORITY["EPSG","4269"]],'
    'PROJECTION["Albers_Conic_Equal_Area"],PARAMETER["latitude_of_center",23],'
    'PARAMETER["longitude_of_center",-96],PARAMETER["standard_parallel_1",29.5],'
    'PARAMETER["standard_parallel_2",45.5],PARAMETER["false_easting",0],'
    'PARAMETER["false_northing",0],UNIT["metre",1,AUTHORITY["EPSG","9001"]],'
    'AXIS["Easting",EAST],AXIS["Northing",NORTH],AUTHORITY["EPSG","5070"]]')


class TerrainError(Exception):
    """A terrain request that cannot be answered; the message says what to do."""


def _part_name(path):
    """A temporary name only this process / thread writes (two QGIS sessions, or two tools,
    fetching the same tile never write into one file)."""
    return "{}.{}_{}.part".format(path, os.getpid(), threading.get_ident())


def _cell_text(cell):
    t = ("%.3f" % float(cell)).rstrip("0").rstrip(".")
    return t.replace(".", "p")


def cache_dir(root, service_id, cell):
    """<root>/<service>_<cell>m (root = the data folder's terrain/ folder)."""
    sid = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (service_id or "dem"))
    return os.path.join(root, "{}_{}m".format(sid, _cell_text(cell)))


def snap(box, cell):
    """The box snapped outward to whole cells of the grid (origin 0, 0)."""
    x0 = math.floor(box[0] / cell + 1e-9) * cell
    y0 = math.floor(box[1] / cell + 1e-9) * cell
    x1 = math.ceil(box[2] / cell - 1e-9) * cell
    y1 = math.ceil(box[3] / cell - 1e-9) * cell
    if x1 <= x0:
        x1 = x0 + cell
    if y1 <= y0:
        y1 = y0 + cell
    return (x0, y0, x1, y1)


def tiles_for(box, cell, tile_px=TILE_PX):
    """[(ix, iy, (xmin, ymin, xmax, ymax))] of the grid tiles that touch the box."""
    if not (box[0] < box[2] and box[1] < box[3]):
        raise TerrainError("empty area {}".format(box))
    if cell <= 0:
        raise TerrainError("the cell size must be positive")
    step = tile_px * cell
    ix0, iy0 = int(math.floor(box[0] / step)), int(math.floor(box[1] / step))
    ix1 = int(math.ceil(box[2] / step - 1e-9)) - 1
    iy1 = int(math.ceil(box[3] / step - 1e-9)) - 1
    out = []
    for ix in range(ix0, max(ix0, ix1) + 1):
        for iy in range(iy0, max(iy0, iy1) + 1):
            out.append((ix, iy, (ix * step, iy * step, (ix + 1) * step, (iy + 1) * step)))
    return out


def tile_path(cdir, ix, iy):
    return os.path.join(cdir, "t_{}_{}.tif".format(ix, iy))


def _is_tiff(path):
    try:
        with open(path, "rb") as f:
            return f.read(4) in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")
    except OSError:
        return False


def write_vrt(path, tiles, cell, box, nodata=NODATA, srs_wkt=WKT_5070, tile_px=TILE_PX):
    """A VRT over cached tiles. tiles: [(tile path, tile box)], all tile_px x tile_px cells of
    `cell`; box: the VRT extent (snapped to the grid). Tiles outside the box are skipped; parts
    of the box without a tile read as NoData."""
    box = snap(box, cell)
    nx = int(round((box[2] - box[0]) / cell))
    ny = int(round((box[3] - box[1]) / cell))
    if nx <= 0 or ny <= 0:
        raise TerrainError("empty VRT")
    lines = ['<VRTDataset rasterXSize="{}" rasterYSize="{}">'.format(nx, ny),
             "  <SRS>{}</SRS>".format(srs_wkt.replace("&", "&amp;").replace("<", "&lt;")),
             "  <GeoTransform>{!r}, {!r}, 0.0, {!r}, 0.0, {!r}</GeoTransform>".format(
                 float(box[0]), float(cell), float(box[3]), -float(cell)),
             '  <VRTRasterBand dataType="Float32" band="1">',
             "    <NoDataValue>{}</NoDataValue>".format(nodata)]
    for tpath, tb in tiles:
        # overlap of tile and box, in cells
        ox0, oy0 = max(tb[0], box[0]), max(tb[1], box[1])
        ox1, oy1 = min(tb[2], box[2]), min(tb[3], box[3])
        if ox1 <= ox0 or oy1 <= oy0:
            continue
        sx = int(round((ox0 - tb[0]) / cell))
        sy = int(round((tb[3] - oy1) / cell))
        w = int(round((ox1 - ox0) / cell))
        h = int(round((oy1 - oy0) / cell))
        dx = int(round((ox0 - box[0]) / cell))
        dy = int(round((box[3] - oy1) / cell))
        src = os.path.abspath(tpath).replace("&", "&amp;").replace("<", "&lt;")
        lines += ["    <ComplexSource>",
                  '      <SourceFilename relativeToVRT="0">{}</SourceFilename>'.format(src),
                  "      <SourceBand>1</SourceBand>",
                  '      <SourceProperties RasterXSize="{0}" RasterYSize="{0}" DataType="Float32"'
                  ' BlockXSize="256" BlockYSize="256" />'.format(tile_px),
                  '      <SrcRect xOff="{}" yOff="{}" xSize="{}" ySize="{}" />'.format(sx, sy, w, h),
                  '      <DstRect xOff="{}" yOff="{}" xSize="{}" ySize="{}" />'.format(dx, dy, w, h),
                  "      <NODATA>{}</NODATA>".format(nodata),
                  "    </ComplexSource>"]
    lines += ["  </VRTRasterBand>", "</VRTDataset>", ""]
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = _part_name(path)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    os.replace(tmp, path)
    return path


def normalize(path, nodata=NODATA):
    """Rewrite a downloaded tile as a tiled, compressed float32 GeoTIFF with NoData -9999 (the
    service's NoData value is mapped to it). No GDAL: the file is left as it is (returns False)."""
    try:
        from osgeo import gdal
    except Exception:                                            # noqa
        return False
    from . import gdalsafe as GS
    src = GS.open_raster(path)
    band = src.GetRasterBand(1)
    snd = band.GetNoDataValue()
    tmp = path + ".n.tif"
    kw = dict(format="GTiff", dstNodata=nodata, outputType=gdal.GDT_Float32,
              creationOptions=["TILED=YES", "COMPRESS=DEFLATE", "PREDICTOR=3"])
    if snd is not None:
        kw["srcNodata"] = snd
    GS.checked(gdal.Warp(tmp, src, **kw), "normalising the terrain tile", gdal)
    src = None
    os.replace(tmp, path)
    return True


def get(box, cell, cdir, fetch_tile, vrt_path, tile_px=TILE_PX, max_age_days=None,
        cancel=None, progress=None, log=None, normalize_tiles=True):
    """Answer a terrain request from the cache, downloading the missing tiles.

    box: (xmin, ymin, xmax, ymax) in EPSG:5070 metres; cell: metres.
    fetch_tile(tile_box, cell, (width, height), out_path): downloads one GeoTIFF tile of exactly
    that box and size to out_path (raises on failure).
    Returns {"vrt", "tiles", "fetched", "cached", "box"}; TerrainError lists failed tiles."""
    log = log or (lambda m: None)
    want = tiles_for(box, cell, tile_px)
    os.makedirs(cdir, exist_ok=True)
    fetched, cached, failed = 0, 0, []
    now = time.time()
    for i, (ix, iy, tb) in enumerate(want):
        if cancel and cancel():
            raise TerrainError("cancelled")
        p = tile_path(cdir, ix, iy)
        fresh = os.path.isfile(p) and _is_tiff(p) and (
            max_age_days is None or (now - os.path.getmtime(p)) / 86400.0 <= max_age_days)
        if fresh:
            cached += 1
        else:
            part = _part_name(p)
            try:
                try:
                    fetch_tile(tb, cell, (tile_px, tile_px), part)
                    if not _is_tiff(part):
                        raise TerrainError("the answer is not a GeoTIFF")
                except Exception as e:                           # noqa
                    # 1.9.0: a big tile can time out as one request; ask for it in quarters
                    if not fetch_split(fetch_tile, tb, cell, tile_px, part, log=log, why=e):
                        raise
                if normalize_tiles:
                    normalize(part)
                os.replace(part, p)
                fetched += 1
            except Exception as e:                               # noqa
                failed.append("t_{}_{} ({})".format(ix, iy, e))
                try:
                    os.remove(part)
                except OSError:
                    pass
        if progress:
            progress(100.0 * (i + 1) / len(want))
    if failed:
        raise TerrainError("{} of {} terrain tile(s) could not be downloaded: {}{} - run again to "
                           "fetch only these".format(len(failed), len(want), "; ".join(failed[:5]),
                                                     " ..." if len(failed) > 5 else ""))
    log("terrain: {} tile(s) from the cache, {} downloaded ({})".format(cached, fetched, cdir))
    sb = snap(box, cell)
    write_vrt(vrt_path, [(tile_path(cdir, ix, iy), tb) for ix, iy, tb in want], cell, sb,
              tile_px=tile_px)
    return {"vrt": vrt_path, "tiles": [tile_path(cdir, ix, iy) for ix, iy, _tb in want],
            "fetched": fetched, "cached": cached, "box": sb}


SPLIT_MIN_PX = 1000             # tiles this size or larger are asked for in quarters on failure


def fetch_split(fetch_tile, tb, cell, tile_px, out_path, log=None, why=""):
    """1.9.0: download one cache tile as 2 x 2 requests of half the size and join them into
    out_path (the tile's grid and size are unchanged, so the cache stays valid). Used when the
    whole-tile request failed (the DamSafety audit saw a 3DEP export of ~3,200 px time out).
    Needs GDAL; returns False (nothing written) without it, for a small tile, or when a quarter
    fails too."""
    if tile_px < SPLIT_MIN_PX or tile_px % 2:
        return False
    try:
        from osgeo import gdal
        if not hasattr(gdal, "BuildVRT"):
            return False
    except Exception:                                            # noqa
        return False
    half = tile_px // 2
    step = half * cell
    parts = []
    try:
        for i in range(2):
            for j in range(2):
                qb = (tb[0] + i * step, tb[1] + j * step, tb[0] + (i + 1) * step,
                      tb[1] + (j + 1) * step)
                q = "{}.q{}{}.tif".format(out_path, i, j)
                parts.append(q)
                fetch_tile(qb, cell, (half, half), q)
                if not _is_tiff(q):
                    return False
        vrt = out_path + ".q.vrt"
        parts.append(vrt)
        ds = gdal.BuildVRT(vrt, parts[:4], outputBounds=tuple(tb), xRes=cell, yRes=cell)
        if ds is None:
            return False
        ds = None
        ds = gdal.Translate(out_path, vrt, format="GTiff", width=tile_px, height=tile_px,
                            projWin=(tb[0], tb[3], tb[2], tb[1]))
        if ds is None:
            return False
        ds = None
        if log:
            log("terrain: tile {} came in 4 parts (the whole tile failed: {})".format(
                os.path.basename(out_path).split(".")[0], why))
        return _is_tiff(out_path)
    except Exception:                                            # noqa
        return False
    finally:
        for q in parts:
            try:
                os.remove(q)
            except OSError:
                pass


def arcgis_fetcher(service, http_get):
    """fetch_tile for an ArcGIS ImageServer (exportImage, float32 GeoTIFF, EPSG:5070)."""
    from . import services as SV

    def fetch(tb, cell, size, out_path):
        data = http_get(SV.export_image_url(service, tb, size, sr=5070))
        if data[:4] not in (b"II*\x00", b"MM\x00*"):
            raise TerrainError("not a TIFF: {!r}".format(bytes(data[:80])))
        with open(out_path, "wb") as f:
            f.write(data)
    return fetch


def wcs_fetcher(service, http_get):
    """fetch_tile for a WCS 2.0 coverage in EPSG:5070."""
    from . import services as SV

    def fetch(tb, cell, size, out_path):
        data = http_get(SV.wcs_coverage_url(service, tb, size))
        if data[:4] not in (b"II*\x00", b"MM\x00*"):
            raise TerrainError("not a TIFF: {!r}".format(bytes(data[:80])))
        with open(out_path, "wb") as f:
            f.write(data)
    return fetch


def fetcher_for(service, http_get):
    kind = (service or {}).get("kind")
    if kind == "arcgis_image":
        return arcgis_fetcher(service, http_get)
    if kind == "wcs":
        return wcs_fetcher(service, http_get)
    raise TerrainError("{} cannot give terrain (an ImageServer or a WCS can)".format(
        (service or {}).get("name", "this service")))


def clear(cdir):
    """Remove one cache folder's tiles (VRTs that point at them stop working)."""
    n = 0
    if os.path.isdir(cdir):
        for f in os.listdir(cdir):
            if f.startswith("t_") and (f.endswith(".tif") or f.endswith(".part")):
                try:
                    os.remove(os.path.join(cdir, f))
                    n += 1
                except OSError:
                    pass
    return n


def summary(root):
    """[(folder name, tiles, MB)] of the cache."""
    out = []
    if not root or not os.path.isdir(root):
        return out
    for d in sorted(os.listdir(root)):
        p = os.path.join(root, d)
        if not os.path.isdir(p):
            continue
        files = [f for f in os.listdir(p) if f.startswith("t_") and f.endswith(".tif")]
        mb = sum(os.path.getsize(os.path.join(p, f)) for f in files) / 1e6
        out.append((d, len(files), round(mb, 1)))
    return out
