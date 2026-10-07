"""0.17.0: coordinate-system and elevation-unit fixes, checked with the REAL GDAL (the qgis.core
double is still used): the z-unit helper, densified lon/lat boxes, a local mosaic of tiles in
two CRSs and two elevation units, the unit tag on written DEMs, the review's breach-move
distance check, and the keyed terrain caches."""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
MOCK = os.path.join(HERE, "qgis_mock")
# the real osgeo first (the mock one is a single-CRS stand-in), then the qgis double
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != MOCK]
from osgeo import gdal, osr  # noqa: E402
assert hasattr(gdal, "Translate"), "test_crs_017 needs the real GDAL"
sys.path.insert(0, MOCK)
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)

import numpy as np  # noqa: E402
import _gpkg  # noqa: E402
from nd_dam_screening.algorithms import _common as cm  # noqa: E402
from nd_dam_screening.core import dem_download as dd, review as rv, workflow as wf, terrain  # noqa: E402

gdal.PushErrorHandler("CPLQuietErrorHandler")
work = tempfile.mkdtemp(prefix="crs017_")


def srs(code):
    s = osr.SpatialReference()
    s.ImportFromEPSG(code)
    s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return s


def write(path, epsg, x0, y1, cell, arr, unit=None, nodata=-9999.0):
    ds = gdal.GetDriverByName("GTiff").Create(path, arr.shape[1], arr.shape[0], 1, gdal.GDT_Float32)
    ds.SetGeoTransform((x0, cell, 0, y1, 0, -cell))
    ds.SetProjection(srs(epsg).ExportToWkt())
    b = ds.GetRasterBand(1)
    b.SetNoDataValue(nodata)
    if unit is not None:
        b.SetUnitType(unit)
    b.WriteArray(arr)
    ds = None
    return path


def sample(src, x, y):
    """The value at an EPSG:5070 point (one 1 m cell, nearest)."""
    ds = gdal.Warp("", src, format="MEM", dstSRS="EPSG:5070", outputBounds=(x - 0.5, y - 0.5, x + 0.5, y + 0.5),
                   width=1, height=1, resampleAlg="near", dstNodata=-9999)
    return float(ds.GetRasterBand(1).ReadAsArray()[0, 0])


# ---------------------------------------------------------------- 1. the z-unit helper
for u, want in (("m", ("m", 1.0)), ("metre", ("m", 1.0)), ("Meters", ("m", 1.0)), ("", ("", 1.0)), (None, ("", 1.0)),
                ("ft", ("ft", 0.3048)), ("foot", ("ft", 0.3048)), ("Feet", ("ft", 0.3048)), ("ft_intl", ("ft", 0.3048)),
                ("us-ft", ("us-ft", 1200 / 3937)), ("ftUS", ("us-ft", 1200 / 3937)),
                ("US survey foot", ("us-ft", 1200 / 3937)), ("foot_us", ("us-ft", 1200 / 3937)),
                ("US_ft", ("us-ft", 1200 / 3937)), ("cm", ("other", 1.0))):
    sc, kind = cm.z_unit_scale(u)
    assert kind == want[0] and abs(sc - want[1]) < 1e-12, (u, sc, kind)

# ---------------------------------------------------------------- 2. densified lon/lat boxes
ct = osr.CoordinateTransformation(srs(4326), srs(5070))
ct66 = osr.CoordinateTransformation(srs(4326), srs(2266))
for code, t, box in ((2266, ct66, dd.COUNTIES["Burleigh"]), (2266, ct66, dd.STATE), (5070, ct, dd.COUNTIES["Cass"]),
                     (5070, ct, dd.STATE)):
    got = dd._box_2266(*box) if code == 2266 else dd.to_5070(*box)
    fine = [t.TransformPoint(lon, lat)[:2] for lon, lat in dd.edge_points(*box, n=400)]
    tol = 20.0 if code == 2266 else 5.0           # 20 points an edge vs 400: within a few metres (ft)
    for i, f in enumerate((min, min, max, max)):
        exact = f(p[i % 2] for p in fine)
        assert abs(got[i] - exact) < tol, (code, i, got[i], exact)
# Burleigh straddles ND South's central meridian (100.5 W): its southern parallel sags below
# the corners (Lambert), and the 4-corner box missed that strip
box = dd.COUNTIES["Burleigh"]
corners = [ct66.TransformPoint(lon, lat)[:2] for lon, lat in ((box[0], box[1]), (box[0], box[3]),
                                                              (box[2], box[1]), (box[2], box[3]))]
assert dd._box_2266(*box)[1] < min(p[1] for p in corners) - 50.0
x, y = ct.TransformPoint(-97.0, 47.0)[:2]
pb = dd.point_box(-97.0, 47.0, 1.0)                       # a point box is unchanged
assert abs(pb[0] - (x - dd.MI)) < 1e-6 and abs(pb[3] - (y + dd.MI)) < 1e-6

# ---------------------------------------------------------------- 3. a local mosaic: two CRSs, feet and metres
X0, Y1 = ct.TransformPoint(-99.0, 47.0)[:2]
X0, Y1 = round(X0, -1), round(Y1, -1)
tiles = os.path.join(work, "tiles")
os.makedirs(tiles)
a = write(os.path.join(tiles, "a_5070_m.tif"), 5070, X0, Y1, 10.0, np.full((100, 100), 100.0, "float32"), "m")
ft = np.full((100, 100), 1000.0, "float32")
ft[0, 0] = -9999.0                                        # no data stays no data after the scaling
c = write(os.path.join(tiles, "c_5070_ft.tif"), 5070, X0 + 1000.0, Y1, 10.0, ft, "ft")
to_utm = osr.CoordinateTransformation(srs(5070), srs(26914))
ux, uy = to_utm.TransformPoint(X0, Y1 - 1000.0)[:2]       # a UTM 14N tile south of tile a
b = write(os.path.join(tiles, "b_utm_untagged.tif"), 26914, ux - 500.0, uy + 500.0, 10.0,
          np.full((150, 150), 200.0, "float32"))
ul = write(os.path.join(tiles, "d_5070_usft.tif"), 5070, X0 + 1000.0, Y1 - 1000.0, 10.0,
           np.full((100, 100), 3937.0, "float32"), "US survey foot")
# the defect: BuildVRT keeps the first tile's CRS and drops the UTM one
old = gdal.BuildVRT(os.path.join(work, "old.vrt"), sorted([a, b, c, ul]))
assert len(old.GetFileList()) < 5
old = None
msgs = []
vrt = cm.local_mosaic(sorted([a, b, c, ul]), os.path.join(work, "_m.vrt"), msgs.append)
assert sample(vrt, X0 + 505, Y1 - 505) == 100.0                                 # a (metres)
assert abs(sample(vrt, X0 + 1505, Y1 - 505) - 304.8) < 1e-3                     # c (feet -> m)
assert sample(vrt, X0 + 1005, Y1 - 5) == -9999.0                                # c's no-data cell
assert abs(sample(vrt, X0 + 1505, Y1 - 1505) - 1200.0) < 0.01                   # d (US ft -> m)
assert abs(sample(vrt, X0 + 200, Y1 - 1300) - 200.0) < 1e-3                     # b (UTM: kept)
text = "\n".join(msgs)
assert "WARNING" in text and "b_utm_untagged.tif" in text and "METRES" in text
assert "2 CRSs" in text and "EPSG:26914" in text and "EPSG:5070" in text
assert "c_5070_ft.tif (ft)" in text
# one CRS, all metres: a plain VRT, nothing extra
m1 = cm.local_mosaic([a], os.path.join(work, "_one.vrt"), msgs.append)
assert not os.path.isdir(os.path.join(work, "_one_tiles")) and sample(m1, X0 + 5, Y1 - 5) == 100.0
# a tile set mixing a CRS-less tile with CRS tiles is refused with the tile named
nocrs = os.path.join(work, "nocrs.tif")
ds = gdal.GetDriverByName("GTiff").Create(nocrs, 4, 4, 1, gdal.GDT_Float32)
ds.SetGeoTransform((0, 1, 0, 4, 0, -1))
ds = None
try:
    cm.local_mosaic([a, b, nocrs], os.path.join(work, "_bad.vrt"))
    raise AssertionError("mixed CRS / no CRS accepted")
except RuntimeError as e:
    assert "nocrs.tif" in str(e)

# the ND LiDAR foot grids: tagged ft -> the metres VRT is tagged m and not converted twice
asc_dir = os.path.join(work, "dwr", "ASCII_Grids_ft", "gft1")
os.makedirs(asc_dir)
src = write(os.path.join(work, "asc_src.tif"), 2266, 1900000.0, 600000.0, 10.0, np.full((10, 10), 2000.0, "float32"))
asc = os.path.join(asc_dir, "gft1.asc")
gdal.Translate(asc, src, format="AAIGrid")
mvrt = dd.build_vrts([asc], os.path.join(work, "dwr"))
assert os.path.basename(mvrt) == "nd_lidar_ascii_m.vrt"


def unit_of(path):
    ds = gdal.Open(path)
    return ds.GetRasterBand(1).GetUnitType()


assert unit_of(mvrt) == "m" and unit_of(os.path.join(work, "dwr", "nd_lidar_ascii_ft.vrt")) == "ft"
both = cm.local_mosaic([mvrt], os.path.join(work, "_dwr.vrt"))
ds = gdal.Open(both)
v = ds.GetRasterBand(1).ReadAsArray()
ds = None
assert abs(float(v[5, 5]) - 609.6) < 1e-3                 # 2000 ft once, not 0.3048 twice

# ---------------------------------------------------------------- 4. DEMs written are tagged metres
w = dd.warp_to(a, os.path.join(work, "dem_2266.tif"), 2266)
ds = gdal.Open(w)
assert ds.GetRasterBand(1).GetUnitType() == "m" and "2266" in ds.GetSpatialRef().GetAuthorityCode(None)
ds = None
plain = write(os.path.join(work, "plain.tif"), 5070, X0, Y1, 10.0, np.zeros((4, 4), "float32"))
assert terrain.set_unit_m(plain) and unit_of(plain) == "m"
assert cm.dem_z_scale(plain)[:2] == (1.0, "m")

# ---------------------------------------------------------------- 5. the review: a far move is refused
W = os.path.join(work, "review")
os.makedirs(W)
BF = [("dam_id", "TEXT"), ("flow_az", "REAL"), ("dir_conf", "TEXT"), ("dir_method", "TEXT")]
_gpkg.write(wf.path(W, "16", "OUTP"), {"bp": ("Point", BF, [
    (("Point", (X0, Y1)), {"dam_id": "D1", "flow_az": 90.0, "dir_conf": "low", "dir_method": "rim"})])})
assert rv.SqliteApply().point_of(wf.path(W, "16", "OUTP"), "D1") == (X0, Y1)
assert rv._gpkg_point_xy(_gpkg.blob(("Point", (1.5, -2.5)))) == (1.5, -2.5)
for far in ("-99.0, 47.0", "{:.1f}, {:.1f}".format(X0 + 6000.0, Y1)):   # degrees typed; 6 km off
    try:
        rv.decide(W, "D1", "move_breach", far, "JD")
        raise AssertionError("a far move was accepted: " + far)
    except ValueError as e:
        assert "refused" in str(e) and "EPSG:5070" in str(e)
assert rv.read_log(W) == []                                # a refused move is not logged
try:
    rv.apply(W, {"dam_id": "D1", "action": "move_breach", "value": "0, 0", "reviewer": "JD", "when": "x"})
    raise AssertionError("apply accepted a far move")
except ValueError:
    pass
row, done = rv.decide(W, "D1", "move_breach", "{:.1f}, {:.1f}".format(X0 + 120.0, Y1 - 40.0), "JD")
assert done and done[0][1] == 1
assert rv.SqliteApply().point_of(wf.path(W, "16", "OUTP"), "D1") == (X0 + 120.0, Y1 - 40.0)
assert [r["action"] for r in rv.read_log(W)] == ["move_breach"]
# a dam with no current point is not checked
_gpkg.write(wf.path(W, "4", "OUTP"), {"bp": ("Point", BF, [])})
assert rv.move_xy(W, "NEW", "10, 20") == (10.0, 20.0)

# ---------------------------------------------------------------- 6. caches keyed by what they were made from
folder = os.path.join(work, "loader_tiles")
os.makedirs(folder)
write(os.path.join(folder, "t1.tif"), 5070, X0, Y1, 10.0, np.full((50, 50), 10.0, "float32"), "m")
wk = os.path.join(work, "_work")
os.makedirs(wk)
L1 = cm.TerrainLoader(wk, "local", folder, wk)
assert os.path.basename(L1.vrt).startswith("_terrain_mosaic_") and os.path.exists(L1.vrt)
assert cm.TerrainLoader(wk, "local", folder, wk).vrt == L1.vrt                  # same folder: reused
write(os.path.join(folder, "t2.tif"), 26914, ux - 500.0, uy + 500.0, 10.0, np.full((50, 50), 20.0, "float32"), "m")
L2 = cm.TerrainLoader(wk, "local", folder, wk, log=msgs.append)
assert L2.vrt != L1.vrt and os.path.exists(L2.vrt)                               # a tile added: a new mosaic
cx, cy = osr.CoordinateTransformation(srs(26914), srs(5070)).TransformPoint(ux - 250.0, uy + 250.0)[:2]
assert abs(sample(L2.vrt, cx, cy) - 20.0) < 1e-3                                  # the UTM tile is in it
k1 = cm.mosaic_key([a, b])
assert k1 == cm.mosaic_key([b, a]) and k1 != cm.mosaic_key([a])
# tool 2's clips: reused only for the same extent, cell and source
clip = os.path.join(work, "dam_D1.tif")
ext = (X0, Y1 - 500.0, X0 + 500.0, Y1)
gdal.Warp(clip, a, dstSRS="EPSG:5070", outputBounds=ext, xRes=5.0, yRes=5.0)
assert cm.clip_reusable(clip, ext, 5.0, "3dep")[0]                # before 0.17 (no .json): the raster fits
assert not cm.clip_reusable(clip, ext, 10.0, "3dep")[0]           # another cell
assert not cm.clip_reusable(clip, (X0 - 400.0, Y1 - 900.0, X0 + 900.0, Y1 + 400.0), 5.0, "3dep")[0]  # bigger
cm.write_clip_meta(clip, ext, 5.0, "local:" + k1)
assert cm.clip_reusable(clip, ext, 5.0, "local:" + k1) == (True, "")
ok, why = cm.clip_reusable(clip, ext, 5.0, "3dep")
assert not ok and "source" in why
ok, why = cm.clip_reusable(clip, (ext[0], ext[1] - 100.0, ext[2], ext[3]), 5.0, "local:" + k1)
assert not ok and "extent" in why
assert not cm.clip_reusable(clip, ext, 2.0, "local:" + k1)[0]
cm.drop_clip(clip)
assert not os.path.exists(clip) and not os.path.exists(clip + ".json")
assert cm.clip_reusable(clip, ext, 5.0, "3dep") == (False, "none yet")

# ---------------------------------------------------------------- 7. LISFLOOD-FP executables without an extension
from nd_dam_screening.algorithms.run_breach_sim import RunBreachSim, EXE_FILTER  # noqa: E402
from nd_dam_screening.algorithms.sensitivity import Sensitivity  # noqa: E402
from nd_dam_screening.algorithms.selftest import SelfTest  # noqa: E402
from nd_dam_screening.core import selftest as st  # noqa: E402
assert EXE_FILTER == "All files (*)"
for cls in (RunBreachSim, Sensitivity, SelfTest):
    alg = cls()
    params = []
    alg.addParameter = params.append
    alg.initAlgorithm()
    exe = [p for p in params if p.name == "EXE"]
    assert len(exe) == 1 and exe[0].fileFilter == EXE_FILTER and exe[0].optional, cls.__name__
# the self-test hands its executable to step 7 (and nothing when empty: tool 7's own fallback)
mk7 = {s[0]: s[2] for s in st.steps(work, {"streams": None, "damlines": None}, "dem", engine=0,
                                    exe="/opt/lisflood/lisflood")}["7"]
assert mk7({})["EXE"] == "/opt/lisflood/lisflood" and mk7({})["ENGINE"] == 0
mk7 = {s[0]: s[2] for s in st.steps(work, {"streams": None, "damlines": None}, "dem")}["7"]
assert "EXE" not in mk7({})
print("test_crs_017: OK")
