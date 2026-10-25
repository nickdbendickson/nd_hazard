"""v2.34.4: HEC-RAS plan import - arrival per pixel, interpolated, consistent with depth.

rasimport.py made Arrival_hr.tif per cell (the first output step at which WSE - the cell
minimum elevation passed the arrival depth) and kept it wherever depth > 0. A ditch through
a cell then gave the whole cell, banks included, the ditch's arrival. Now each pixel takes
its cell's water-surface series minus the pixel's own terrain (DEM, else cell minimum),
interpolates the first reach of the arrival depth between steps, falls back to the time of
the maximum when the saved steps never reach it, and has an arrival only where depth >= the
arrival depth (as the Runner, nd_hazard #13 / DWR #173). The basis is recorded and the
plan cache (code version, format 2) is not re-used across the change.

The pure helpers run anywhere; the end-to-end ditch-cell plan needs GDAL (osgeo) and is
skipped without it. tests/fake_qgis.py stands in for PyQGIS.

    python3 -m ND_Dam_Hazard_Helper.tests.run_tests v2344
"""
import json
import os
import shutil
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
from ND_Dam_Hazard_Helper.tests.registry import test  # noqa: E402
from ND_Dam_Hazard_Helper import rasimport as RI  # noqa: E402

TIMES_HR = np.array([0.0, 1.0, 2.0])


def _near(a, b, tol=1e-6):
    return abs(float(a) - float(b)) <= tol


@test
def v2344_first_reach_is_interpolated_per_pixel_terrain():
    # one cell, its water surface 90 -> 92.5 -> 95 ft; a ditch pixel (90) and a bank pixel (93)
    ser = np.array([[90.0, 92.5, 95.0], [90.0, 92.5, 95.0]])
    got = RI.first_reach_hr(ser, np.array([90.0, 93.0]), 0.1, TIMES_HR)
    assert _near(got[0], 0.04), got          # 0.1 ft of 2.5 ft over the first hour
    assert _near(got[1], 1.24), got          # bank: -0.5 -> 2.0 ft over the second hour
    # already at the threshold at the first step: that step's time
    got = RI.first_reach_hr(np.array([[90.1, 91.0, 92.0]]), np.array([90.0]), 0.1, TIMES_HR)
    assert _near(got[0], 0.0), got
    # never reached in the saved steps: NaN (the caller uses the time of the maximum)
    got = RI.first_reach_hr(np.array([[90.0, 90.05, 90.05]]), np.array([90.0]), 0.1, TIMES_HR)
    assert np.isnan(got[0]), got


@test
def v2344_pixel_arrival_threshold_and_time_of_max():
    ser = np.array([[90.0, 92.5, 95.0],          # row 0: rises past both pixels
                    [90.0, 91.0, 93.05]])        # row 1: saved steps peak 0.05 ft over 93
    cidx = np.array([[0.0, 0.0, 1.0, 1.0, np.nan]])
    z = np.array([[90.0, 93.0, 90.0, 93.0, 90.0]])
    depth = np.array([[5.0, 2.0, 3.5, 0.5, 1.0]])     # max depth (summary output)
    peak = np.array([[2.0, 2.0, 1.5, 1.5, 2.0]])
    arr, n_tom = RI.pixel_arrival_hr(cidx, z, depth, peak, ser, TIMES_HR, 0.1, breach_hr=0.0)
    a = arr[0]
    assert _near(a[0], 0.04) and _near(a[1], 1.24), a
    assert _near(a[2], 0.1), a                        # 90 -> 91 over the first hour
    assert _near(a[3], 1.5) and n_tom == 1, (a, n_tom)    # time-of-max fallback
    assert np.isnan(a[4]), a                          # no series for the pixel's cell
    # below the arrival depth: no arrival even though the pixel is wet
    arr, _n = RI.pixel_arrival_hr(cidx, z, np.array([[5.0, 0.05, 3.5, 0.09, 1.0]]), peak,
                                  ser, TIMES_HR, 0.1)
    assert np.isnan(arr[0][1]) and np.isnan(arr[0][3]), arr
    # breach start subtracted and clipped at 0 (the fallback peak is already after it)
    arr, _n = RI.pixel_arrival_hr(cidx, z, depth, peak, ser, TIMES_HR, 0.1, breach_hr=1.0)
    assert _near(arr[0][0], 0.0) and _near(arr[0][1], 0.24), arr


@test
def v2344_cache_from_before_the_change_is_not_reused():
    assert RI.CACHE_FORMAT == 2 and RI.IMPORT_CODE_VERSION == "2.6.0"
    for name in ("first_reach_hr", "pixel_arrival_hr", "_SeriesStack", "read_area", "_finish"):
        assert name in RI._CODE_FUNCS, name
    root = tempfile.mkdtemp(prefix="ricache_")
    try:
        plan = os.path.join(root, "Dam.p01.hdf")
        with open(plan, "wb") as fh:
            fh.write(b"\x89HDF\r\n\x1a\n" + b"0" * 1000)
        key = RI.plan_key(plan, {})
        folder = os.path.join(root, RI._folder_name(plan, key))
        os.makedirs(folder)
        open(os.path.join(folder, "Depth_ft.tif"), "w").close()
        man = {"format": 1, "key": key, "complete": True, "files": {"depth": "Depth_ft.tif"}}
        with open(os.path.join(folder, RI.MANIFEST), "w") as fh:
            json.dump(man, fh)
        assert RI.plan_cache_lookup(plan, root, {}) is None       # format 1: stale
        man.update(format=2, basis={"depth": "terrain", "arrival": RI.ARRIVAL_BASIS})
        with open(os.path.join(folder, RI.MANIFEST), "w") as fh:
            json.dump(man, fh)
        hit = RI.plan_cache_lookup(plan, root, {})
        assert hit is not None and hit["basis"]["arrival"] == RI.ARRIVAL_BASIS, hit
    finally:
        shutil.rmtree(root, ignore_errors=True)


class _FakePlan(object):
    """Just enough of rasimport's HDF reader interface for a 4-cell 2D area."""
    name = "fake"

    def __init__(self, wkt, data):
        self.wkt, self.data = wkt, data

    def groups(self, path):
        return ["Ditch"] if path == RI.GEOM else []

    def has(self, path):
        return path in self.data

    def shape(self, path):
        return tuple(np.asarray(self.data[path]).shape)

    def read(self, path, cols=None):
        a = np.asarray(self.data[path])
        return a if cols is None else a[:, cols[0]:cols[1]]

    def attr(self, path, name):
        return {"Units System": "US Customary", "Projection": self.wkt}.get(name) \
            if path in ("", "/") else None

    def field(self, path, name):
        return None

    def close(self):
        pass


@test
def v2344_ditch_cell_plan_end_to_end():
    try:
        from osgeo import gdal, osr
    except ImportError:
        print("    (skipped: no GDAL)")
        return
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(26914)
    wkt = srs.ExportToWkt()
    d = tempfile.mkdtemp(prefix="riditch_")
    try:
        # 20 x 20 ft of 1-ft terrain at 93 ft with a 2-ft ditch at 90 ft (columns 9 and 10)
        dem_path = os.path.join(d, "dem.tif")
        z = np.full((20, 20), 93.0, dtype=np.float32)
        z[:, 9:11] = 90.0
        ds = gdal.GetDriverByName("GTiff").Create(dem_path, 20, 20, 1, gdal.GDT_Float32)
        ds.SetGeoTransform((0.0, 1.0, 0.0, 20.0, 0.0, -1.0))
        ds.SetProjection(wkt)
        ds.GetRasterBand(1).WriteArray(z)
        ds = None
        g, s = RI.GEOM + "/Ditch", RI.SUMMARY + "/Ditch"
        # cells: A (5,15) B (15,15) top, C (5,5) D (15,5) bottom; the ditch is in A and B (top)
        # and C and D (bottom); every cell's minimum elevation is the ditch's 90 ft
        ws = np.array([[90.0, 90.0, 90.0, 90.0],
                       [92.5, 92.5, 91.0, 90.5],
                       [95.0, 95.0, 93.05, 93.05]])
        data = {
            g + "/Cells Center Coordinate": np.array([[5.0, 15.0], [15.0, 15.0],
                                                      [5.0, 5.0], [15.0, 5.0]]),
            g + "/Cells Minimum Elevation": np.full(4, 90.0),
            # max WSE and time of max (days); C peaks between the saved steps
            s + "/Maximum Water Surface": np.array([[95.0, 95.0, 93.5, 93.05],
                                                    [2 / 24.0, 2 / 24.0, 1.5 / 24.0, 2 / 24.0]]),
            RI.TSERIES + "/Time": TIMES_HR / 24.0,
            RI.TSERIES + "/2D Flow Areas/Ditch/Water Surface": ws,
        }
        orig = RI.open_hdf
        RI.open_hdf = lambda path: _FakePlan(wkt, data)
        try:
            out = os.path.join(d, "out")
            res = RI.import_plan(os.path.join(d, "Ditch.p01.hdf"), out, dem_path=dem_path,
                                 arrival_ft=0.1)
        finally:
            RI.open_hdf = orig
        assert res["basis"]["depth"] == RI.DEPTH_BASIS_TERRAIN, res["basis"]
        assert res["basis"]["arrival"] == RI.ARRIVAL_BASIS, res["basis"]
        assert res["basis"]["arrival_time_of_max_px"] > 0, res["basis"]
        assert not [f for f in os.listdir(out) if f.startswith("_")], os.listdir(out)

        def band(key):
            rds = gdal.Open(res["paths"][key])
            a = rds.GetRasterBand(1).ReadAsArray().astype(float)
            rds = None
            a[a == RI.NODATA] = np.nan
            return a
        dep, arr = band("depth"), band("arr")
        # rows 0-9 are the top cells (A, B), rows 10-19 the bottom (C, D)
        assert _near(arr[2, 9], 0.04, 1e-4), arr[2]          # A: ditch
        assert _near(arr[2, 3], 1.24, 1e-4), arr[2]          # A: bank, later
        assert _near(arr[2, 10], 0.04, 1e-4), arr[2]         # B: ditch
        assert _near(arr[15, 9], 0.1, 1e-4), arr[15]         # C: ditch, 90 -> 91
        assert _near(arr[15, 3], 1.5, 1e-4), arr[15]         # C: bank, time of max
        assert _near(dep[15, 15], 0.05, 1e-3), dep[15]       # D: bank wet but shallow ...
        assert np.isnan(arr[15, 15]), arr[15]                # ... no arrival
        # arrival exactly where depth >= the arrival depth
        with np.errstate(invalid="ignore"):
            deep = np.isfinite(dep) & (dep >= 0.1)
        assert (np.isfinite(arr) == deep).all()
    finally:
        shutil.rmtree(d, ignore_errors=True)
