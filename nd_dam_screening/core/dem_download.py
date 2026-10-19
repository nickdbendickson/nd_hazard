"""USGS 3DEP download extents for a dam, a North Dakota county, or the state.

County boxes are download clips, padded, not survey boundaries. The service
returns metres in EPSG:5070.
"""
import os

from osgeo import gdal, osr

from . import terrain
from . import nd_counties
from ..damsuite import netproxy       # QGIS's proxy setting applies (0.28.2)


def safe_extract(zf, dest):
    """Unzip `zf` into `dest`, refusing the whole archive when any entry would land outside
    `dest` (an absolute path or "..": the zip-slip problem). 0.28.1."""
    root = os.path.realpath(dest)
    for member in zf.infolist():
        target = os.path.realpath(os.path.join(dest, member.filename))
        if target != root and not target.startswith(root + os.sep):
            raise ValueError("archive entry outside the destination: " + member.filename)
    zf.extractall(dest)


# (west, south, east, north) in degrees, padded so a dam on the county line is inside.
# 0.29.0: the county download extents are derived from the county outlines the suite ships
# (nd_counties.geojson, the same file prepare_dams and the DEM-tab map use), not hand-typed.
# The hand-typed table ended INSIDE 48 of the 53 counties (McLean by ~34 mi on the west), so a
# county DEM download quietly stopped short and dams in the missing strip had no terrain.
COUNTY_PAD_DEG = 0.03          # ~3 km outward, so a dam on the county line is inside the download
COUNTIES = {name: nd_counties.box([name], COUNTY_PAD_DEG) for name in nd_counties.names()}
STATE = nd_counties.box(nd_counties.names(), COUNTY_PAD_DEG)
MI = 1609.344


def county_names():
    return sorted(COUNTIES)


EDGE_POINTS = 20       # 0.17.0: points per edge when a lon/lat box is projected


def edge_points(west, south, east, north, n=EDGE_POINTS):
    """0.17.0: the outline of a lon/lat box, n points per edge (corners included). A projected
    box from the 4 corners alone misses the bulge of the parallels (Albers, Lambert): the
    northern edge of a county bows up to a few hundred metres past its corners."""
    out = []
    for i in range(n):
        t = i / float(n - 1)
        lon, lat = west + t * (east - west), south + t * (north - south)
        out += [(lon, south), (lon, north), (west, lat), (east, lat)]
    return out


def box_in(srs, west, south, east, north, n=EDGE_POINTS):
    """(xmin, ymin, xmax, ymax) in `srs` (osr, traditional GIS order) of a lon/lat box, from
    its densified outline."""
    wgs = osr.SpatialReference()
    wgs.ImportFromEPSG(4326)
    wgs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    ct = osr.CoordinateTransformation(wgs, srs)
    pts = [ct.TransformPoint(lon, lat)[:2] for lon, lat in edge_points(west, south, east, north, n)]
    return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))


def _epsg(code):
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(code)
    return srs


def to_5070(west, south, east, north):
    return box_in(_epsg(5070), west, south, east, north)       # 0.17.0: densified edges


def point_box(lon, lat, buffer_mi):
    x0, y0, x1, y1 = to_5070(lon, lat, lon, lat)
    b = buffer_mi * MI
    return (x0 - b, y0 - b, x1 + b, y1 + b)


def cell_tag(cell_m):
    """'10' for 10 m, '2p5' for 2.5 m (a file-name part)."""
    t = ("%.3f" % float(cell_m)).rstrip("0").rstrip(".")
    return t.replace(".", "p")


def output_stem(scope, cell_m, dam_id="", county="", point=None):
    """0.16.0: the file name (no extension) of a 3DEP download, so a second dam or county in the
    same folder no longer overwrites the first (0.15 wrote dam.vrt for every dam):
    <dam ID>_dem_<cell>m (a dam without an ID: dam_<lon>_<lat>_dem_<cell>m), <county>_dem_<cell>m,
    north_dakota_dem_<cell>m. scope: 0 dam, 1 county, 2 state."""
    tag = "_dem_{}m".format(cell_tag(cell_m))
    if scope == 0:
        name = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(dam_id or "").strip())
        if not name.strip("_") and point is not None:
            name = "dam_{:.4f}_{:.4f}".format(point[0], point[1]).replace("-", "m").replace(".", "p")
        return (name.strip("_") or "dam") + tag
    if scope == 1:
        return (str(county or "").strip().replace(" ", "_") or "county") + tag
    return "north_dakota" + tag


def download(extent, cell_m, out_dir, stem, feedback=None):
    """Download 3DEP tiles for an EPSG:5070 extent and write <stem>.vrt. Returns the VRT path.

    0.16.0: with a suite data folder and an ImageServer / WCS terrain server chosen, the tiles
    come from the shared terrain cache and <stem>.vrt points at the cached tiles (absolute
    paths; nothing is copied into out_dir)."""
    os.makedirs(out_dir, exist_ok=True)
    vrt = os.path.join(out_dir, stem + ".vrt")
    log = feedback.pushInfo if feedback is not None else None
    if terrain.cached_dem(extent, cell_m, vrt, log=log,
                          cancel=(feedback.isCanceled if feedback is not None else None)):
        terrain.set_unit_m(vrt)                                   # 0.17.0: 3DEP is metres
        return vrt
    tiles = list(terrain.tile_extents(*extent, cell_m))
    if feedback is not None:
        feedback.pushInfo("{} tiles at {:.1f} ft ({} m)".format(len(tiles), float(cell_m) / 0.3048, cell_m))
    paths = terrain.fetch_3dep(extent, cell_m, out_dir, stem)
    ds = gdal.BuildVRT(vrt, paths)
    ds = None
    terrain.set_unit_m(vrt)                                       # 0.17.0: 3DEP is metres
    return vrt


FT_TO_M = 0.3048


def _product_of(path):
    p = path.replace("\\", "/")
    if "/ASCII_Grids_ft/" in p or "/gft" in p.lower() or p.lower().endswith(".asc"):
        return "ASCII_Grids_ft"
    return "DEM_Imagine"


def build_vrts(rasters, out_dir):
    """One VRT per product, never a mosaic of feet and metres. The foot grids get a
    second VRT scaled to metres (the Runner and Screening read elevations in metres).
    Returns the metres VRT to use: DEM_Imagine when present, else the converted foot grids."""
    rasters = [r for r in rasters if not r.lower().endswith(".rrd")]
    by = {}
    for r in rasters:
        by.setdefault(_product_of(r), []).append(r)
    use = None
    if by.get("DEM_Imagine"):
        use = os.path.join(out_dir, "nd_lidar_imagine_m.vrt")
        gdal.BuildVRT(use, by["DEM_Imagine"])
        terrain.set_unit_m(use)                                   # 0.17.0: the Imagine DEMs are metres
    if by.get("ASCII_Grids_ft"):
        ft = os.path.join(out_dir, "nd_lidar_ascii_ft.vrt")
        ds = gdal.BuildVRT(ft, by["ASCII_Grids_ft"])
        if ds is not None:
            # 0.17.0: tagged, so a local DEM folder holding this VRT is read as feet (converted)
            ds.GetRasterBand(1).SetUnitType("ft")
            ds = None
        m = os.path.join(out_dir, "nd_lidar_ascii_m.vrt")
        gdal.Translate(m, ft, format="VRT", outputType=gdal.GDT_Float32,
                       scaleParams=[[0.0, 1.0, 0.0, FT_TO_M]])
        ds = gdal.Open(m, gdal.GA_Update)
        if ds is not None:
            ds.GetRasterBand(1).SetUnitType("m")
            ds = None
        use = use or m
    return use


DWR_HOST = "lidarftp.swc.nd.gov"
DWR_USER = "swc"
DWR_PASSWORD = "water"   # published guest login on the ND LiDAR download page
DWR_PRODUCTS = ("ASCII_Grids_ft", "DEM_Imagine")


def parse_dwr_path(text):
    """Accept a pasted FTP URL, a /temp/<id> path, or the clipboard text from the map site."""
    raw = (text or "").strip()
    for line in raw.splitlines():
        if "path:" in line.lower():
            raw = line.split(":", 1)[1].strip()
            break
    raw = raw.replace("ftp://", "")
    if "@" in raw:
        raw = raw.split("@", 1)[1]
    if raw.startswith(DWR_HOST):
        raw = raw[len(DWR_HOST):]
    raw = raw.split("?", 1)[0].strip().strip("/")
    if not raw.startswith("temp/"):
        raise ValueError("expected an ND LiDAR path like /temp/<id> from lidar.dwr.nd.gov")
    return "/" + raw


def fetch_dwr(package, out_dir, products, feedback=None):
    """Download QL2 ASCII foot grids and/or Imagine DEMs from a staged map-site package."""
    import ftplib
    import zipfile
    path = parse_dwr_path(package)
    wanted = [p for p in products if p in DWR_PRODUCTS]
    if not wanted:
        raise ValueError("choose ASCII_Grids_ft, DEM_Imagine, or both")
    os.makedirs(out_dir, exist_ok=True)
    ftp = ftplib.FTP(DWR_HOST, timeout=120)
    ftp.login(DWR_USER, DWR_PASSWORD)
    found = []

    def walk(folder):
        lines = []
        ftp.cwd(folder)
        ftp.retrlines("LIST", lines.append)
        for line in lines:
            name = line.split(None, 8)[-1]
            if name in (".", ".."):
                continue
            full = folder.rstrip("/") + "/" + name
            if line.startswith("d"):
                walk(full)
            elif any("/" + product + "/" in full + "/" or full.endswith("/" + product) for product in wanted):
                if name.lower().endswith((".zip", ".img", ".asc", ".grd")):
                    found.append(full)

    walk(path)
    if not found:
        ftp.quit()
        raise IOError("no {} files under {}".format(", ".join(wanted), path))
    if feedback is not None:
        feedback.pushInfo("{} ND LiDAR files in {}".format(len(found), path))
    rasters = []
    for i, remote in enumerate(found):
        local = os.path.join(out_dir, os.path.basename(remote))
        if feedback is not None:
            feedback.pushInfo("getting {} ({}/{})".format(os.path.basename(remote), i + 1, len(found)))
        with open(local, "wb") as f:
            ftp.retrbinary("RETR " + remote, f.write)
        if local.lower().endswith(".zip"):
            dest = os.path.splitext(local)[0]
            os.makedirs(dest, exist_ok=True)
            with zipfile.ZipFile(local) as z:
                safe_extract(z, dest)
            rasters += [os.path.join(root, fn) for root, _d, files in os.walk(dest)
                        for fn in files if fn.lower().endswith((".img", ".asc", ".grd", ".tif"))]
        else:
            rasters.append(local)
    ftp.quit()
    vrt = build_vrts(rasters, out_dir)
    if not vrt:
        raise IOError("no rasters in the ND LiDAR package {}".format(path))
    return vrt


INDEX_DIR = "LiDAR_NorthDakota_QL2"
INDEX_NAME = "IndexLiDARNorthDakotaQL2"


def _ftp():
    import ftplib
    ftp = ftplib.FTP(DWR_HOST, timeout=120)
    ftp.login(DWR_USER, DWR_PASSWORD)
    return ftp


def ensure_index(cache_dir, feedback=None):
    """Cache the statewide QL2 tile index (EPSG:2266 feet). Returns the shapefile path."""
    os.makedirs(cache_dir, exist_ok=True)
    shp = os.path.join(cache_dir, INDEX_NAME + ".shp")
    if os.path.isfile(shp) and os.path.isfile(shp.replace(".shp", ".dbf")):
        return shp
    ftp = _ftp()
    for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
        remote = "/{}/{}{}".format(INDEX_DIR, INDEX_NAME, ext)
        local = os.path.join(cache_dir, INDEX_NAME + ext)
        if feedback is not None:
            feedback.pushInfo("index {}".format(ext))
        with open(local, "wb") as f:
            ftp.retrbinary("RETR " + remote, f.write)
    ftp.quit()
    return shp


def _box_2266(west, south, east, north):
    return box_in(_epsg(2266), west, south, east, north)       # 0.17.0: densified edges


# 0.18.1: the same statewide index and tiles over HTTPS from the map site. FTP (port 21) is
# blocked on many networks (cloud machines, some offices) while HTTPS is not; the tile files are
# the same, at https://lidar.dwr.nd.gov/mapdata/<project>/<path>/<product>/<pre><tile>.zip.
HTTPS_ROOT = "https://lidar.dwr.nd.gov/mapdata"
INDEX_URL = "{}/{}/{}.shp".format(HTTPS_ROOT, INDEX_DIR, INDEX_NAME)
PREFIX = {"ASCII_Grids_ft": "gft", "DEM_Imagine": "dem"}
FT_PER_MI = 5280.0                       # EPSG:2266 is international feet


def _open_index(cache_dir, feedback=None):
    """0.18.1: the tile index as an OGR data source: a cached copy, else read in place over HTTPS
    (GDAL /vsicurl/ reads only the records it needs; the .dbf is about 300 MB), else the FTP copy."""
    from osgeo import ogr
    shp = os.path.join(cache_dir, INDEX_NAME + ".shp") if cache_dir else ""
    if shp and os.path.isfile(shp) and os.path.isfile(shp.replace(".shp", ".dbf")):
        return ogr.Open(shp)
    opts = netproxy.gdal_options(INDEX_URL)                    # 0.28.2: QGIS's proxy
    for k in ("GDAL_HTTP_PROXY", "GDAL_HTTP_PROXYUSERPWD"):   # this thread only, kept for the reads
        gdal.SetThreadLocalConfigOption(k, opts.get(k))
    ds = ogr.Open("/vsicurl/" + INDEX_URL)
    if ds is not None:
        if feedback is not None:
            feedback.pushInfo("tile index: {}".format(INDEX_URL))
        return ds
    if feedback is not None:
        feedback.pushInfo("tile index not readable over HTTPS; trying the FTP copy")
    return ogr.Open(ensure_index(cache_dir, feedback))


def point_box_2266(lon, lat, buffer_mi):
    """0.18.1: a dam point and buffer as an EPSG:2266 box (feet)."""
    x0, y0, x1, y1 = _box_2266(lon, lat, lon, lat)
    b = float(buffer_mi) * FT_PER_MI
    return (x0 - b, y0 - b, x1 + b, y1 + b)


def tiles_for(scope, county, cache_dir, feedback=None, box_2266=None):
    """Tile records whose box meets a county clip or the state. scope 1 = county, 2 = state.
    0.18.1: scope 0 = a dam, with box_2266 from point_box_2266()."""
    if scope == 0:
        if box_2266 is None:
            raise ValueError("ND DWR LiDAR by dam needs the dam point")
        box = None
    elif scope == 1:
        box = COUNTIES.get(county)
        if box is None:
            raise ValueError("Unknown county {!r}".format(county))
    else:
        box = STATE
    ds = _open_index(cache_dir, feedback)
    if ds is None:
        raise IOError("the ND LiDAR tile index could not be read ({})".format(INDEX_URL))
    layer = ds.GetLayer(0)
    srs = layer.GetSpatialRef()
    if box is None:
        xmin, ymin, xmax, ymax = box_2266
        if srs is not None and not srs.IsSame(_epsg(2266)):
            raise IOError("the tile index is no longer in EPSG:2266")
    elif srs is not None:                   # the index's own CRS; EPSG:2266 only as a fallback
        xmin, ymin, xmax, ymax = box_in(srs, *box)                # 0.17.0: densified edges
    else:
        xmin, ymin, xmax, ymax = _box_2266(*box)
    layer.SetSpatialFilterRect(xmin, ymin, xmax, ymax)
    tiles = []
    for feat in layer:
        tiles.append({
            "tile": feat.GetField("TileNum"),
            "path": feat.GetField("Path"),
            "project": feat.GetField("Project") or INDEX_DIR,
            "ascii": feat.GetField("ASCII_Path"),
        })
    return tiles


def tile_url(tile, product):
    """0.18.1: the HTTPS address of one tile's zip."""
    return "{}/{}/{}/{}/{}{}.zip".format(HTTPS_ROOT, tile["project"], tile["path"], product,
                                         PREFIX[product], tile["tile"])


def _https_get(url, local, retries=3, timeout=120):
    """0.18.1: download url to local (through local.part, so a cut transfer is never kept)."""
    import shutil
    import time
    import urllib.request
    part = local + ".part"
    err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "nd_dam_screening"})
            with netproxy.urlopen(req, timeout=timeout) as r, open(part, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            os.replace(part, local)
            return
        except Exception as e:                                  # noqa - retried, then reported
            err = e
            time.sleep(2 * (attempt + 1))
    try:
        os.remove(part)
    except OSError:
        pass
    raise IOError(str(err))


def fetch_dwr_area(scope, county, out_dir, products, feedback=None, cache_dir=None,
                   box_2266=None, transport="https"):
    """Download QL2 tiles for a dam, one county or the state from the statewide index.

    0.18.1: over HTTPS from lidar.dwr.nd.gov by default (transport "ftp" is the old route);
    scope 0 (a dam) with box_2266."""
    import zipfile
    cache_dir = cache_dir or os.path.join(out_dir, "_index")
    tiles = tiles_for(scope, county, cache_dir, feedback, box_2266=box_2266)
    if feedback is not None:
        feedback.pushInfo("{} QL2 tiles ({})".format(len(tiles), transport.upper()))
    if not tiles:
        raise IOError("no QL2 tiles in that area")
    os.makedirs(out_dir, exist_ok=True)
    ftp = _ftp() if transport == "ftp" else None
    rasters, failed = [], []
    try:
        for i, tile in enumerate(tiles):
            for product in products:
                url = tile_url(tile, product)
                remote = url[len(HTTPS_ROOT):]          # /<project>/<path>/<product>/<file>.zip
                local = os.path.join(out_dir, product, os.path.basename(remote))
                os.makedirs(os.path.dirname(local), exist_ok=True)
                if feedback is not None and i % 25 == 0:
                    feedback.pushInfo("{} {}/{}".format(os.path.basename(remote), i + 1, len(tiles)))
                if feedback is not None and feedback.isCanceled():
                    raise IOError("cancelled")
                if not (os.path.isfile(local) and zipfile.is_zipfile(local)):   # a rerun resumes
                    try:
                        if ftp is not None:
                            with open(local, "wb") as f:
                                ftp.retrbinary("RETR " + remote, f.write)
                        else:
                            _https_get(url, local)
                    except Exception as e:                          # noqa
                        failed.append("{} ({})".format(url if ftp is None else remote, e))
                        try:
                            os.remove(local)
                        except OSError:
                            pass
                        continue
                dest = os.path.splitext(local)[0]
                os.makedirs(dest, exist_ok=True)
                try:
                    with zipfile.ZipFile(local) as z:
                        safe_extract(z, dest)
                except zipfile.BadZipFile:
                    failed.append("{} (damaged zip)".format(remote))
                    continue
                except ValueError as e:
                    failed.append("{} (unsafe zip: {})".format(remote, e))
                    continue
                rasters += [os.path.join(root, fn) for root, _d, files in os.walk(dest)
                            for fn in files if fn.lower().endswith((".img", ".asc", ".grd", ".tif"))]
    finally:
        if ftp is not None:
            ftp.quit()
    if failed:
        lst = os.path.join(out_dir, "failed_tiles.txt")
        with open(lst, "w") as f:
            f.write("\n".join(failed) + "\n")
        raise IOError("{} of {} tile downloads failed (listed in {}). A DEM with holes is not "
                      "built; run the tool again to fetch the rest.".format(len(failed),
                                                                         len(tiles) * len(products), lst))
    vrt = build_vrts(rasters, out_dir)
    if not vrt:
        raise IOError("no rasters in the downloaded tiles")
    return vrt, len(tiles)


WORK_CRS = (
    ("EPSG:5070", "EPSG:5070 metres (3DEP / screening)"),
    ("EPSG:26914", "EPSG:26914 UTM 14N metres (QL2 Imagine, eastern ND)"),
    ("EPSG:2265", "EPSG:2265 ND North feet"),
    ("EPSG:2266", "EPSG:2266 ND South feet"),
)


def county_features():
    """(name, ring of lon/lat) for the download clips."""
    out = []
    for name, (w, s, e, n) in sorted(COUNTIES.items()):
        out.append((name, [(w, s), (e, s), (e, n), (w, n), (w, s)]))
    return out


def warp_to(src, dest, epsg):
    """Warp one raster to dest in the given EPSG. Returns dest.

    0.17.0: the elevations stay in metres whatever the CRS's linear unit (EPSG:2265 / 2266 are
    feet), and the band says so ("m"); a tool reading the file no longer guesses feet."""
    ds = gdal.Warp(dest, src, dstSRS="EPSG:{}".format(epsg), format="GTiff",
                   resampleAlg="bilinear", creationOptions=["COMPRESS=DEFLATE", "TILED=YES"])
    if ds is None:
        raise IOError("GDAL could not warp {} to EPSG:{}".format(src, epsg))
    ds.GetRasterBand(1).SetUnitType("m")
    ds = None
    return dest


# 0.24.0: standardised local DEM: GeoTIFF float32, tiled, DEFLATE, EPSG:5070, metres.
# Named <stem>_10m_5070.tif or <stem>_1m_5070.tif; sidecar <stem>_10m_5070_dem.txt.

def local_tif_stem(scope, cell_m, dam_id="", county="", point=None):
    """Stem for the standardised local GeoTIFF: <name>_10m_5070 or <name>_1m_5070."""
    cell_int = int(round(float(cell_m)))
    tag = "_{}m_5070".format(cell_int)
    if scope == 0:
        name = "".join(c if c.isalnum() or c in "-_" else "_"
                       for c in str(dam_id or "").strip())
        if not name.strip("_") and point is not None:
            name = "dam_{:.4f}_{:.4f}".format(point[0], point[1]).replace(
                "-", "m").replace(".", "p")
        return (name.strip("_") or "dam") + tag
    if scope == 1:
        return (str(county or "").strip().replace(" ", "_") or "county") + tag
    return "north_dakota" + tag


_FEET_UNITS = ("ft", "feet", "foot", "us-ft", "usft", "us survey foot",
               "us survey feet", "international foot")


def warp_to_local(src, dest, cell_m, feedback=None):
    """Warp to the standardised format: EPSG:5070, float32, tiled, DEFLATE at cell_m metres.

    PRECONDITION: the elevation VALUES in ``src`` must already be in metres. gdal.Warp only
    reprojects horizontally and resamples - it never converts vertical units - yet this
    function stamps UNITS="m" on the output, so a foot source would be silently mislabelled
    metres and feed feet into the breach/flood model. The DWR foot grids are converted with
    ``build_vrts`` (its FT_TO_M scale); callers must pass that metres VRT, never a raw foot
    grid. As a guard, a source whose band 1 is explicitly tagged in feet is refused here
    rather than relabelled metres."""
    if feedback is not None:
        feedback.pushInfo("warping to EPSG:5070 at {} m (float32 GeoTIFF)".format(cell_m))
    try:
        src_ds = gdal.Open(src)
    except Exception:
        src_ds = None
    if src_ds is not None:
        unit = (src_ds.GetRasterBand(1).GetUnitType() or "").strip().lower()
        src_ds = None
        if unit in _FEET_UNITS:
            raise ValueError(
                "warp_to_local needs a metres source; band 1 of {} is tagged {!r}. Convert the "
                "foot grid to metres with build_vrts first - warp_to_local only reprojects "
                "horizontally, it does not convert elevations.".format(src, unit))
    xres = yres = float(cell_m)
    ds = gdal.Warp(dest, src, dstSRS="EPSG:5070", format="GTiff",
                   resampleAlg="bilinear", outputType=gdal.GDT_Float32,
                   xRes=xres, yRes=yres,
                   creationOptions=["COMPRESS=DEFLATE", "TILED=YES"])
    if ds is None:
        raise IOError("GDAL could not warp {} to EPSG:5070".format(src))
    ds.GetRasterBand(1).SetUnitType("m")
    ds = None
    return dest


_NATIVE = {
    "3dep":  "EPSG:5070, metres, GeoTIFF",
    "dwr":   "feet, EPSG:3721 / EPSG:2266, Erdas Imagine .img at 1 m",
}


def write_sidecar(tif_path, source_label, cell_m, source_key="3dep"):
    """Write <tif_path without .tif>_dem.txt listing source, CRS, units, file type, cell,
    and native source facts."""
    cell_int = int(round(float(cell_m)))
    txt = tif_path[:-4] + "_dem.txt" if tif_path.lower().endswith(".tif") else tif_path + "_dem.txt"
    native = _NATIVE.get(source_key, source_key)
    with open(txt, "w", encoding="utf-8") as f:
        f.write("Source: {}\n".format(source_label))
        f.write("CRS: EPSG:5070\n")
        f.write("Units: metres\n")
        f.write("File type: GeoTIFF float32\n")
        f.write("Cell: {} m\n".format(cell_int))
        f.write("Native: {}\n".format(native))
    tag_local_tif(tif_path, source_label, cell_m)          # 0.26.0: the same facts on the raster
    return txt


def tag_local_tif(tif_path, source_label, cell_m):
    """0.26.0: the sidecar's facts as GeoTIFF metadata items on the raster itself (SOURCE, CRS,
    UNITS, CELL_M), so a file that travels without its _dem.txt still says what it is. Opens the
    file in update mode, sets the items and closes it. Returns True when written; False (never
    raises) when the file is not a raster GDAL can update."""
    ds = None
    try:
        ds = gdal.Open(tif_path, getattr(gdal, "GA_Update", 1))
    except Exception:                                            # noqa - GDAL with exceptions on
        ds = None
    if ds is None:
        return False
    try:
        ds.SetMetadataItem("SOURCE", str(source_label))
        ds.SetMetadataItem("CRS", "EPSG:5070")
        ds.SetMetadataItem("UNITS", "metres")
        ds.SetMetadataItem("CELL_M", "{:g}".format(float(cell_m)))
        ds.FlushCache()
        return True
    except Exception:                                            # noqa
        return False
    finally:
        ds = None
