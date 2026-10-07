"""Terrain acquisition: USGS 3DEP ImageServer or a local folder of ND LiDAR DEMs.

Everything is requested/warped to EPSG:5070 (CONUS Albers, equal-area) so
cell areas are true and volumes are not distorted (Web Mercator would inflate
areas ~1.5x at ND latitudes).
"""
import os
import threading
import time
import urllib.parse
import urllib.request

EXPORT_URL = ("https://elevation.nationalmap.gov/arcgis/rest/services/"
              "3DEPElevation/ImageServer/exportImage")
MAX_PX = 8000          # service maxImageWidth / maxImageHeight
TARGET_EPSG = 5070


def tile_extents(xmin, ymin, xmax, ymax, cell, max_px=4000):
    """Split an extent into tiles no larger than max_px on a side."""
    step = max_px * cell
    x = xmin
    while x < xmax:
        y = ymin
        while y < ymax:
            yield (x, y, min(x + step, xmax), min(y + step, ymax))
            y += step
        x += step


def fetch_3dep_tile(extent, cell, out_path, retries=3, timeout=120):
    """Download one GeoTIFF tile (float32, metres) in EPSG:5070."""
    xmin, ymin, xmax, ymax = extent
    w = int(round((xmax - xmin) / cell))
    h = int(round((ymax - ymin) / cell))
    if w > MAX_PX or h > MAX_PX:
        raise ValueError("tile exceeds service limit")
    params = {
        "f": "image", "format": "tiff", "pixelType": "F32",
        "bbox": f"{xmin},{ymin},{xmax},{ymax}",
        "bboxSR": TARGET_EPSG, "imageSR": TARGET_EPSG,
        "size": f"{w},{h}", "interpolation": "RSP_BilinearInterpolation",
    }
    url = EXPORT_URL + "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                data = r.read()
            if data[:4] not in (b"II*\x00", b"MM\x00*"):
                raise IOError("response is not a TIFF: %r" % data[:120])
            with open(out_path, "wb") as f:
                f.write(data)
            return out_path
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    host = urllib.parse.urlparse(url).hostname or "the USGS 3DEP service"
    raise IOError(f"3DEP request to {host} failed after {retries} tries: {last}. This computer may "
                  f"not be allowed to reach {host} (a firewall, proxy or restricted network): "
                  f"allow that host, or point the terrain source at a local DEM folder.")


def fetch_3dep(extent, cell, out_dir, stem, log=None):
    """Fetch all tiles for an extent; returns list of tile paths.

    0.16.0: with a suite data folder set and an ImageServer / WCS chosen as the terrain server
    (Suite data and servers), the tiles come from the shared terrain cache (`cached_dem`) and
    the list holds ONE file of this request in out_dir (callers delete what they get back: it
    is never a cache tile). Without them, the 3DEP ImageServer is asked directly, as before."""
    os.makedirs(out_dir, exist_ok=True)
    one = request_from_cache(extent, cell, out_dir, stem, log=log)
    if one:
        return [one]
    paths = []
    for i, ext in enumerate(tile_extents(*extent, cell)):
        p = os.path.join(out_dir, f"{stem}_t{i}.tif")
        paths.append(fetch_3dep_tile(ext, cell, p))
    return paths


# ------------------------------------------------------- 0.16.0: the suite cache --
_CACHE_LOCK = threading.Lock()      # one cache request at a time in this QGIS (parallel dams)


def _suite():
    """(terrain_cache, services, suitedata) of the shared damsuite package."""
    try:
        from ..damsuite import terrain_cache as TC, services as SV, suitedata as SD
    except ImportError:                                          # core/ itself on sys.path (tests)
        import importlib
        TC = importlib.import_module("damsuite.terrain_cache")
        SV = importlib.import_module("damsuite.services")
        SD = importlib.import_module("damsuite.suitedata")
    return TC, SV, SD


def _http_get(url, retries=3, timeout=120):
    """GET with the retries of fetch_3dep_tile (urllib, like the direct download)."""
    _TC, SV, _SD = _suite()
    last = None
    for attempt in range(retries):
        try:
            return SV.urllib_get(url, timeout=timeout)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise IOError("terrain request failed: {}".format(last))


def cache_setup(data_folder=None, catalog=None, http_get=None):
    """(cache folder root, chosen terrain service, fetch_tile factory) when terrain requests go
    through the suite cache, else None (no suite data folder, or the chosen terrain server is
    not an ImageServer / WCS - e.g. a DEM file of yours: the old direct 3DEP download then)."""
    try:
        TC, SV, SD = _suite()
        data = SD.data_folder() if data_folder is None else data_folder
        if not data:
            return None
        choice = (catalog or SV.load()).chosen("dem")
    except Exception:                                            # noqa - never stop a download here
        return None
    if not choice or choice.get("kind") not in ("arcgis_image", "wcs"):
        return None
    get = http_get or _http_get
    return SD.sub(data, SD.TERRAIN), choice, (lambda: TC.fetcher_for(choice, get))


def cached_dem(extent, cell, vrt_path, setup=None, log=None, cancel=None):
    """Answer an EPSG:5070 request from the suite terrain cache: missing tiles are downloaded
    once into <suite data>/terrain/<service>_<cell>m/, and vrt_path is written as a VRT over the
    cached tiles (absolute paths; the cache's grid, the box snapped outward to whole cells).
    Returns terrain_cache.get's dict, or None when the cache is not in use."""
    setup = setup if setup is not None else cache_setup()
    if not setup:
        return None
    TC, _SV, _SD = _suite()
    root, choice, fetcher = setup
    cdir = TC.cache_dir(root, choice.get("id") or "dem", cell)
    with _CACHE_LOCK:
        res = TC.get(tuple(float(v) for v in extent), float(cell), cdir, fetcher(), vrt_path,
                     cancel=cancel, log=log)
    if log:
        log("terrain: {} ({} m) through the suite cache: {} tile(s) downloaded, {} cached".format(
            choice.get("name", "terrain server"), cell, res["fetched"], res["cached"]))
    return res


def _aligned(extent, cell):
    return all(abs(v / cell - round(v / cell)) < 1e-6 for v in extent)


def request_from_cache(extent, cell, out_dir, stem, setup=None, log=None):
    """One file of this request in out_dir read from the suite cache, or None (cache not in
    use). With GDAL: <stem>_c.tif, the exact box and cell (as the service's own tiles were;
    bilinear when the box is off the cache's grid); without: <stem>_c.vrt over the cached tiles.
    Either one may be deleted by the caller - the cache tiles are never handed out."""
    setup = setup if setup is not None else cache_setup()
    if not setup:
        return None
    vrt = os.path.join(out_dir, "{}_c.vrt".format(stem))
    cached_dem(extent, cell, vrt, setup=setup, log=log)
    try:
        from osgeo import gdal
        if not hasattr(gdal, "Translate"):                       # a stand-in without GDAL's API
            raise ImportError
    except ImportError:
        return vrt
    tif = os.path.join(out_dir, "{}_c.tif".format(stem))
    ds = gdal.Warp(tif, vrt, format="GTiff", dstSRS="EPSG:{}".format(TARGET_EPSG),
                   outputBounds=tuple(extent), xRes=cell, yRes=cell,
                   resampleAlg="near" if _aligned(extent, cell) else "bilinear",
                   srcNodata=-9999, dstNodata=-9999, outputType=gdal.GDT_Float32,
                   creationOptions=["COMPRESS=DEFLATE", "TILED=YES"])
    if ds is None:
        raise IOError("GDAL could not cut the cached terrain to {}".format(tif))
    ds.GetRasterBand(1).SetUnitType("m")                         # 0.17.0: 3DEP is metres
    ds = None
    try:
        os.remove(vrt)
    except OSError:
        pass
    return tif


def set_unit_m(path):
    """0.17.0: tag band 1 of a DEM we wrote in metres with the unit "m", so a tool that reads it
    later (or another program) does not guess feet from a State Plane feet CRS. Best effort."""
    try:
        from osgeo import gdal
        ds = gdal.Open(path, gdal.GA_Update)
        if ds is not None:
            ds.GetRasterBand(1).SetUnitType("m")
            ds.FlushCache()
        ds = None
        return True
    except Exception:                                            # noqa - a stand-in without the API
        return False


def list_local_tiles(folder, exts=(".tif", ".tiff", ".img", ".vrt")):
    out = []
    for root, _d, files in os.walk(folder):
        out += [os.path.join(root, f) for f in files
                if f.lower().endswith(exts)]
    return sorted(out)
