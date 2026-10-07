"""0.18.1: ND DWR QL2 LiDAR over HTTPS (tool 20 without an FTP link), with the REAL GDAL.

Offline: the dam box in EPSG:2266, tile URLs, the tile loop with a fake HTTPS getter (resume,
a failed tile stops the tool), tool 20 by dam and the dam point from dam.json.
Live (ND_LIVE=1): one McLean County dam box against lidar.dwr.nd.gov."""
import os
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
MOCK = os.path.join(HERE, "qgis_mock")
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != MOCK]
from osgeo import gdal, osr  # noqa: E402
assert hasattr(gdal, "Translate"), "test_dwr_https needs the real GDAL"
sys.path.insert(0, MOCK)
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)

import numpy as np  # noqa: E402
from nd_dam_screening.core import dem_download as dd  # noqa: E402

gdal.PushErrorHandler("CPLQuietErrorHandler")
work = tempfile.mkdtemp(prefix="dwrhttps_")

# --- the dam box: 47.5 N 101.2 W is (1795473, 669392) ft in EPSG:2266 -------------------------
x0, y0, x1, y1 = dd.point_box_2266(-101.2, 47.5, 1.0)
assert abs((x0 + x1) / 2 - 1795473) < 5 and abs((y0 + y1) / 2 - 669392) < 5, (x0, y0, x1, y1)
assert abs((x1 - x0) - 2 * 5280) < 1e-6

# --- tile URLs follow the map site's links --------------------------------------------------
t = {"tile": "03345262", "path": "Phase10", "project": "LiDAR_NorthDakota_QL2"}
assert dd.tile_url(t, "DEM_Imagine") == \
    "https://lidar.dwr.nd.gov/mapdata/LiDAR_NorthDakota_QL2/Phase10/DEM_Imagine/dem03345262.zip"
assert dd.tile_url(t, "ASCII_Grids_ft").endswith("/ASCII_Grids_ft/gft03345262.zip")

# --- by dam without a point is refused before anything is read ------------------------------
try:
    dd.tiles_for(0, "", work)
    raise AssertionError("dam scope without a box should fail")
except ValueError:
    pass


# --- the tile loop with a fake index and a fake HTTPS getter --------------------------------
def tile_zip(path, x0m, y0m):
    """A 10 x 10 one-metre Imagine tile in UTM 14N, zipped like the site's."""
    img = path[:-4] + ".img"
    ds = gdal.GetDriverByName("HFA").Create(img, 10, 10, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((x0m, 1, 0, y0m + 10, 0, -1))
    s = osr.SpatialReference()
    s.ImportFromEPSG(3721)
    ds.SetProjection(s.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(np.full((10, 10), 600.0, dtype=np.float32))
    ds = None
    with zipfile.ZipFile(path, "w") as z:
        z.write(img, os.path.basename(img))
    os.remove(img)


TILES = [{"tile": "A1", "path": "Phase10", "project": "LiDAR_NorthDakota_QL2", "ascii": ""},
         {"tile": "A2", "path": "Phase10", "project": "LiDAR_NorthDakota_QL2", "ascii": ""}]
got, boxes = [], []
src = os.path.join(work, "site")
os.makedirs(src)
for i, tl in enumerate(TILES):
    tile_zip(os.path.join(src, "dem{}.zip".format(tl["tile"])), 334000 + 10 * i, 5262000)


def fake_get(url, local, retries=3, timeout=120):
    got.append(url)
    name = url.rsplit("/", 1)[1]
    if "FAIL" in name:
        raise IOError("HTTP Error 404")
    with open(os.path.join(src, name), "rb") as f, open(local, "wb") as g:
        g.write(f.read())


def fake_tiles(scope, county, cache_dir, feedback=None, box_2266=None):
    boxes.append((scope, county, box_2266))
    return list(TILES)


real = dd._https_get, dd.tiles_for
dd._https_get, dd.tiles_for = fake_get, fake_tiles
try:
    out = os.path.join(work, "dl")
    box = dd.point_box_2266(-101.2, 47.5, 0.5)
    vrt, n = dd.fetch_dwr_area(0, "", out, ["DEM_Imagine"], box_2266=box)
    assert n == 2 and boxes[-1] == (0, "", box)
    assert got == [dd.tile_url(tl, "DEM_Imagine") for tl in TILES], got
    ds = gdal.Open(vrt)
    assert ds.RasterXSize == 20 and ds.RasterYSize == 10, (ds.RasterXSize, ds.RasterYSize)
    assert abs(ds.GetRasterBand(1).ReadAsArray().mean() - 600.0) < 1e-6
    assert ds.GetRasterBand(1).GetUnitType() == "m"
    ds = None
    # a rerun resumes: nothing is downloaded again
    got[:] = []
    dd.fetch_dwr_area(0, "", out, ["DEM_Imagine"], box_2266=box)
    assert got == [], got
    # a failed tile stops the tool and is listed; no DEM with a hole
    TILES.append({"tile": "FAIL", "path": "Phase10", "project": "LiDAR_NorthDakota_QL2", "ascii": ""})
    try:
        dd.fetch_dwr_area(1, "McLean", os.path.join(work, "dl2"), ["DEM_Imagine"])
        raise AssertionError("a failed tile should stop the download")
    except IOError as e:
        assert "1 of 3" in str(e), e
    lst = open(os.path.join(work, "dl2", "failed_tiles.txt")).read()
    assert "demFAIL.zip" in lst and "404" in lst and not os.path.exists(
        os.path.join(work, "dl2", "DEM_Imagine", "demFAIL.zip.part"))
    TILES.pop()
finally:
    dd._https_get, dd.tiles_for = real

# --- tool 20: ND LiDAR by dam (no FTP link) uses the point and buffer --------------------
from nd_dam_screening.algorithms import download_dem as alg_mod  # noqa: E402


class FB:
    def __init__(self):
        self.lines = []

    def pushInfo(self, s):
        self.lines.append(s)

    def reportError(self, s, *a):
        self.lines.append(s)

    def isCanceled(self):
        return False


calls = []
real_area = dd.fetch_dwr_area


def fake_area(scope, county, out, products, feedback=None, cache_dir=None, box_2266=None,
              transport="https"):
    calls.append((scope, county, products, box_2266))
    p = os.path.join(out, "x.vrt")
    os.makedirs(out, exist_ok=True)
    open(p, "w").close()
    return p, 4


class Alg(alg_mod.DownloadDem):
    def __init__(self, params):
        self.p = params

    def parameterAsString(self, parameters, name, context):
        return str(self.p.get(name, ""))

    def parameterAsEnum(self, parameters, name, context):
        return int(self.p.get(name, 0))

    def parameterAsBool(self, parameters, name, context):
        return bool(self.p.get(name, False))

    def parameterAsDouble(self, parameters, name, context):
        return float(self.p.get(name, 0.0))


dd.fetch_dwr_area = fake_area
try:
    params = {"SOURCE": 1, "SCOPE": 0, "DAM_POINT": "-101.2,47.5", "BUFFER_MI": 2.0,
              "IMAGINE": True, "OUT": os.path.join(work, "t20"), "DWR_PATH": ""}
    res = Alg(params).processAlgorithm(params, None, FB())
    assert res["VRT"].endswith("x.vrt")
    scope, county, products, b = calls[-1]
    assert scope == 0 and products == ["DEM_Imagine"]
    assert b == dd.point_box_2266(-101.2, 47.5, 2.0)
    # the dam point from dam.json when the field is empty
    real_rec = alg_mod._dam_record
    rec = {"lon": -101.2, "lat": 47.5}
    alg_mod._dam_record = lambda *a: (None, "")
    p2 = dict(params, DAM_POINT="")
    try:
        Alg(p2).processAlgorithm(p2, None, FB())
        raise AssertionError("no point and no dam.json should fail")
    except Exception as e:
        assert "longitude,latitude" in str(e), e
    assert alg_mod._dam_point(Alg(p2), p2, None, FB(), rec) == (-101.2, 47.5)
    alg_mod._dam_record = real_rec
    # county still goes the same way, without a box
    p3 = dict(params, SCOPE=1, COUNTY="McLean")
    Alg(p3).processAlgorithm(p3, None, FB())
    assert calls[-1][:2] == (1, "McLean") and calls[-1][3] is None
finally:
    dd.fetch_dwr_area = real_area

# --- live: one McLean County dam box over HTTPS --------------------------------------------
if os.environ.get("ND_LIVE") == "1":
    box = dd.point_box_2266(-101.2, 47.5, 0.3)
    tiles = dd.tiles_for(0, "", os.path.join(work, "noindex"), box_2266=box)
    names = sorted(t["tile"] for t in tiles)
    assert "03345262" in names, names
    vrt, n = dd.fetch_dwr_area(0, "", os.path.join(work, "live"), ["DEM_Imagine"], box_2266=box)
    ds = gdal.Open(vrt)
    a = ds.GetRasterBand(1).ReadAsArray()
    gt = ds.GetGeoTransform()
    assert abs(gt[1] - 1.0) < 1e-9 and 550 < float(a[a > 0].mean()) < 700, (gt, a[a > 0].mean())
    print("live: {} tiles, {} x {} at {} m".format(n, ds.RasterXSize, ds.RasterYSize, gt[1]))
    ds = None

print("test_dwr_https: OK")
