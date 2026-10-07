"""USGS 3DEP download extents for a dam, a North Dakota county, or the state.

County boxes are download clips, padded, not survey boundaries. The service
returns metres in EPSG:5070.
"""
import os

from osgeo import gdal, osr

from . import terrain

# west, south, east, north in degrees. Padded so a dam on the county line is inside.
COUNTIES = {
    "Adams": (-103.00, 45.93, -102.05, 46.45),
    "Barnes": (-98.35, 46.60, -97.45, 47.35),
    "Benson": (-99.70, 47.75, -98.70, 48.35),
    "Billings": (-103.70, 46.65, -102.90, 47.30),
    "Bottineau": (-101.40, 48.45, -100.15, 49.01),
    "Bowman": (-104.05, 45.93, -102.95, 46.45),
    "Burke": (-102.95, 48.45, -102.05, 49.01),
    "Burleigh": (-101.05, 46.55, -100.05, 47.30),
    "Cass": (-97.45, 46.60, -96.55, 47.35),
    "Cavalier": (-98.95, 48.55, -97.85, 49.01),
    "Dickey": (-98.95, 45.93, -98.05, 46.45),
    "Divide": (-104.05, 48.50, -103.05, 49.01),
    "Dunn": (-103.25, 47.15, -102.15, 47.85),
    "Eddy": (-99.15, 47.45, -98.45, 47.95),
    "Emmons": (-100.55, 45.95, -99.55, 46.65),
    "Foster": (-99.35, 47.15, -98.55, 47.75),
    "Golden Valley": (-104.05, 46.55, -103.45, 47.25),
    "Grand Forks": (-97.65, 47.55, -96.75, 48.15),
    "Grant": (-102.05, 46.05, -101.00, 46.65),
    "Griggs": (-98.55, 47.25, -97.85, 47.75),
    "Hettinger": (-102.85, 46.05, -101.95, 46.70),
    "Kidder": (-100.25, 46.55, -99.25, 47.35),
    "LaMoure": (-98.95, 46.15, -98.05, 46.75),
    "Logan": (-99.65, 46.15, -98.85, 46.75),
    "McHenry": (-100.95, 47.85, -99.95, 48.55),
    "McIntosh": (-99.75, 45.93, -98.95, 46.45),
    "McKenzie": (-104.05, 47.35, -102.65, 48.15),
    "McLean": (-101.65, 47.25, -100.35, 48.05),
    "Mercer": (-102.35, 47.05, -101.35, 47.65),
    "Morton": (-101.65, 46.35, -100.35, 47.05),
    "Mountrail": (-102.85, 47.75, -101.55, 48.55),
    "Nelson": (-98.45, 47.65, -97.75, 48.25),
    "Oliver": (-101.65, 46.95, -100.85, 47.45),
    "Pembina": (-97.75, 48.55, -96.75, 49.01),
    "Pierce": (-100.35, 47.85, -99.45, 48.55),
    "Ramsey": (-99.05, 47.95, -98.25, 48.55),
    "Ransom": (-97.95, 46.25, -97.15, 46.85),
    "Renville": (-102.05, 48.45, -101.15, 49.01),
    "Richland": (-97.35, 45.93, -96.55, 46.55),
    "Rolette": (-100.25, 48.45, -99.45, 49.01),
    "Sargent": (-97.85, 45.93, -97.05, 46.45),
    "Sheridan": (-100.55, 47.25, -99.55, 47.95),
    "Sioux": (-101.65, 45.95, -100.45, 46.55),
    "Slope": (-104.05, 46.15, -103.15, 46.75),
    "Stark": (-103.25, 46.45, -101.95, 47.25),
    "Steele": (-98.05, 47.15, -97.35, 47.65),
    "Stutsman": (-99.35, 46.55, -98.25, 47.35),
    "Towner": (-99.55, 48.35, -98.75, 48.95),
    "Traill": (-97.55, 47.15, -96.65, 47.65),
    "Walsh": (-98.15, 48.15, -97.05, 48.65),
    "Ward": (-102.05, 47.85, -100.75, 48.65),
    "Wells": (-100.05, 47.25, -99.15, 47.95),
    "Williams": (-104.05, 47.95, -102.65, 48.65),
}
STATE = (-104.06, 45.93, -96.54, 49.01)
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
                z.extractall(dest)
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
            with urllib.request.urlopen(req, timeout=timeout) as r, open(part, "wb") as f:
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
                        z.extractall(dest)
                except zipfile.BadZipFile:
                    failed.append("{} (damaged zip)".format(remote))
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
