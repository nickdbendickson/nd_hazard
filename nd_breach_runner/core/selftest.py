"""Self-test of an installation: what works on this computer, in plain checks.

Written because the GDAL side of the plugin (GeoTIFF writing and reading, warping, VRT
mosaics, gap filling, NLCD roughness) and the model executables cannot be run where the
plugin is developed. Every check here is a plain function that returns a short detail text
when it passes and raises (usually AssertionError with the numbers) when it does not, so it
can be run three ways:

* from QGIS: Processing > ND Dam Breach Runner > "Self-test the installation in this QGIS"
  (algorithms_selftest.py builds the QGIS-only checks and calls `run_all`);
* from a terminal, with QGIS's own Python so GDAL is importable:
      /Applications/QGIS-LTR.app/Contents/MacOS/bin/python3 -m nd_breach_runner.core.selftest
  (or `ndbreach_cli.py selftest`); needs the plugin folder's parent on PYTHONPATH;
* in the tests, with stand-ins for `gdal_io` (see tests/test_selftest.py).

`Report` collects PASS / FAIL / SKIP per check with the exact exception text and produces the
full text report and a compact block to paste to the developer. Nothing here needs QGIS.
"""
import datetime
import os
import platform
import shutil
import sys
import tempfile
import time
import traceback

import numpy as np

from . import gdal_io
from .grid import Grid
from .units import FT, US_FT
from ..damsuite import gdalsafe as GS

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
CRS = "EPSG:26914"                       # NAD83 / UTM zone 14N, North Dakota's western half
OTHER_CRS = "EPSG:26913"
# Same projection with the false easting moved 1000 m east: a warp into it is an exact shift,
# which makes the expected raster known without trusting any datum transformation.
# 3.4.3: written as transverse Mercator with UTM zone 14's parameters. "+proj=utm ... +x_0=501000"
# does NOT shift: PROJ's utm always resets the false easting to 500000 m, so the check read a
# window 100 cells off the data (7,722 no-data cells - the first real-QGIS run, QGIS 3.44.14 /
# GDAL 3.13 / Windows 11, 2026-10-02).
SHIFTED_SRS = ("+proj=tmerc +lat_0=0 +lon_0=-99 +k=0.9996 +x_0=501000 +y_0=0 +datum=NAD83 "
               "+units=m +no_defs")
SHIFT_M = 1000.0

# the small synthetic valley the GDAL checks work on (coordinates like real ND UTM data)
X0, Y0, DX, NX, NY = 350000.0, 5230000.0, 10.0, 120, 80
# the model-sized valley for the end-to-end run (dam at the origin of local coordinates)
DAM_X, DAM_Y = 350000.0, 5230000.0
TOP_FT, DAM_H_FT = 1850.0, 25.0
MODEL_CELL_M = 20.0
MIN_PYTHON = (3, 8)
MIN_QGIS = 32800


class Skip(Exception):
    """Raised by a check that cannot run here (the reason is the message)."""


# ---------------------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------------------
def short_path(p):
    """A path for a report: the plugin-relative part for our files, else the base name."""
    p = str(p).replace("\\", "/")
    i = p.rfind("nd_breach_runner/")
    return p[i:] if i >= 0 else os.path.basename(p)


def mask_home(text, home=None):
    """Replace the user's home folder (and so the user name) in a text with ~."""
    home = home if home is not None else os.path.expanduser("~")
    text = str(text)
    if home and home not in ("/", "~"):
        text = text.replace(home, "~")
    return text


class Result(object):
    def __init__(self, name, status, detail="", seconds=0.0, trace="", where=""):
        self.name, self.status, self.detail = name, status, detail
        self.seconds, self.trace, self.where = seconds, trace, where


class Report(object):
    """Results of the checks, in order. `on_result(result)` is called as each one finishes
    (the QGIS tool logs it live); `cancelled()` skips whatever has not run yet."""

    def __init__(self, on_result=None, cancelled=None, title="ND Dam Breach Runner self-test"):
        self.title = title
        self.results = []
        self.info = []              # (label, text) for the header
        self.ctx = {}               # what later checks need (synthetic case, executable paths)
        self.on_result = on_result
        self.cancelled = cancelled or (lambda: False)
        self.started = datetime.datetime.now()

    # -- collecting -----------------------------------------------------------------
    def add(self, name, status, detail="", seconds=0.0, trace="", where=""):
        r = Result(name, status, detail, seconds, trace, where)
        self.results.append(r)
        if self.on_result is not None:
            try:
                self.on_result(r)
            except Exception:
                pass                    # logging must never break the test
        return r

    def run(self, name, fn, *args, **kwargs):
        """Run one check. Returns its Result: PASS with the returned text, SKIP when it
        raises Skip, FAIL with the exact exception text."""
        if self.cancelled():
            return self.add(name, SKIP, "cancelled")
        t0 = time.time()
        try:
            out = fn(*args, **kwargs)
            return self.add(name, PASS, "" if out is None else str(out), time.time() - t0)
        except Skip as e:
            return self.add(name, SKIP, str(e), time.time() - t0)
        except Exception as e:
            where = ""
            try:
                fr = traceback.extract_tb(sys.exc_info()[2])[-1]
                where = "{}:{} in {}".format(short_path(fr.filename), fr.lineno, fr.name)
            except Exception:
                pass
            return self.add(name, FAIL, "{}: {}".format(type(e).__name__, e),
                            time.time() - t0, traceback.format_exc(), where)

    def skip(self, name, reason):
        return self.add(name, SKIP, reason)

    def status_of(self, name):
        for r in self.results:
            if r.name == name:
                return r.status
        return None

    def counts(self):
        c = {PASS: 0, FAIL: 0, SKIP: 0}
        for r in self.results:
            c[r.status] = c.get(r.status, 0) + 1
        return c

    def ok(self):
        return self.counts()[FAIL] == 0

    # -- writing --------------------------------------------------------------------
    def counts_text(self):
        c = self.counts()
        return "{} PASS, {} FAIL, {} SKIP".format(c[PASS], c[FAIL], c[SKIP])

    def text(self, home=None, with_trace=True):
        out = [self.title, "=" * len(self.title),
               "Run: " + self.started.strftime("%Y-%m-%d %H:%M:%S")]
        for k, v in self.info:
            out.append("{}: {}".format(k, mask_home(v, home)))
        out.append("")
        for r in self.results:
            tail = "  ({:.1f} s)".format(r.seconds) if r.seconds >= 0.05 else ""
            out.append("[{}] {}{}".format(r.status, r.name, tail))
            for ln in mask_home(r.detail, home).splitlines() or [""]:
                if ln:
                    out.append("      " + ln)
            if r.status == FAIL and with_trace and r.trace:
                out.append("      -- traceback --")
                for ln in mask_home(r.trace, home).rstrip().splitlines():
                    out.append("      " + ln)
        out += ["", "Result: " + self.counts_text()]
        return "\n".join(out)

    def developer_block(self, home=None, max_detail=500):
        """The compact text to paste to the developer: versions, one line per check, the
        exact exception and where it was raised for each failure. No user name or paths
        outside the plugin (the home folder is written as ~)."""
        head = ["===== COPY FROM HERE ====="]
        head.append("{} | {}".format(self.title, self.started.strftime("%Y-%m-%d %H:%M")))
        for k, v in self.info:
            head.append("{}: {}".format(k, mask_home(v, home)))
        head.append("Result: " + self.counts_text())
        body = []
        for r in self.results:
            detail = mask_home(r.detail, home).strip()
            first = detail.splitlines()[0] if detail else ""
            if r.status == FAIL:
                body.append("[FAIL] {}".format(r.name))
                text = detail if len(detail) <= max_detail else detail[:max_detail] + " ..."
                for ln in text.splitlines():
                    body.append("       " + ln)
                if r.where:
                    body.append("       at " + r.where)
            else:
                if len(first) > 160:
                    first = first[:157] + "..."
                body.append("[{}] {}{}".format(r.status, r.name, " - " + first if first else ""))
        return "\n".join(head + body + ["===== END ====="])


# ---------------------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------------------
def plugin_version(plugin_dir=None):
    """`version=` from metadata.txt, or '?'."""
    plugin_dir = plugin_dir or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(plugin_dir, "metadata.txt"), encoding="utf-8") as f:
            for ln in f:
                if ln.strip().lower().startswith("version="):
                    return ln.split("=", 1)[1].strip()
    except OSError:
        pass
    return "?"


def module_version(name):
    """(version text, error text) of an importable module."""
    try:
        m = __import__(name)
        return str(getattr(m, "__version__", "?")), ""
    except Exception as e:
        return "", "{}: {}".format(type(e).__name__, e)


def environment_info():
    """[(label, text)] for the report header. Never raises."""
    info = [("Plugin version", plugin_version()),
            ("System", "{} {} ({})".format(platform.system(), platform.release(),
                                          platform.machine())),
            ("Python", "{} at {}".format(sys.version.split()[0], sys.executable))]
    for label, mod in (("numpy", "numpy"), ("scipy", "scipy"), ("contourpy", "contourpy")):
        v, err = module_version(mod)
        info.append((label, v or "not importable ({})".format(err)))
    try:
        from osgeo import gdal
        info.append(("GDAL", gdal.VersionInfo("RELEASE_NAME")))
    except Exception as e:
        info.append(("GDAL", "not importable ({}: {})".format(type(e).__name__, e)))
    for var in ("NDBREACH_LISFLOOD", "NDBREACH_ANUGA_PYTHON"):
        if os.environ.get(var):
            info.append((var, os.environ[var]))
    return info


def check_python():
    v = sys.version_info
    text = "Python {}.{}.{} ({} {}) at {}".format(v[0], v[1], v[2], platform.system(),
                                                   platform.machine(), sys.executable)
    if (v[0], v[1]) < MIN_PYTHON:
        raise RuntimeError("{}: the plugin needs Python {}.{} or newer".format(text, *MIN_PYTHON))
    return text


def check_numpy():
    a = np.linspace(0.0, 1.0, 5)
    assert abs(float(a.sum()) - 2.5) < 1e-12, "numpy arithmetic is wrong"
    return "numpy " + np.__version__


def check_scipy():
    import scipy
    from scipy import ndimage
    lab, n = ndimage.label(np.array([[1, 0, 1]]))
    assert n == 2, "scipy.ndimage.label found {} regions, expected 2".format(n)
    d = ndimage.distance_transform_edt(np.array([[True, True, False]]))
    assert abs(float(d[0, 0]) - 2.0) < 1e-9, "distance transform gave {}, expected 2".format(
        float(d[0, 0]))
    return "scipy {} (ndimage works; used for the domain and the receptor stop rule)".format(
        scipy.__version__)


def check_contourpy():
    """The flood-domain outline (ANUGA mesh): contourpy when installed, else the built-in
    cell-edge tracer (3.4.4 - QGIS 3.28's Python has no contourpy; that is not a failure)."""
    from . import terrain
    g = Grid(np.zeros((7, 7)), 0.0, 7.0, 1.0)
    mask = np.zeros((7, 7), dtype=bool)
    mask[2:5, 2:5] = True
    ring = terrain.mask_outline(g, mask, 0.05)
    area = abs(terrain._area(ring))
    assert 7.0 < area <= 9.0 + 1e-9, "outline of a 3 x 3 block has area {:.2f}, expected 8.5 " \
        "(contourpy) or 9 (built-in)".format(area)
    if terrain._have_contourpy():
        import contourpy
        return "contourpy {} (ANUGA mesh outline)".format(contourpy.__version__)
    return "contourpy not installed: the built-in outline tracer is used (ANUGA mesh outline) - " \
           "nothing to install"


def check_keep_awake():
    """v3.12.0: can the computer be kept awake while a model runs (damsuite.keepawake)?"""
    from ..damsuite import keepawake
    ok, what = keepawake.probe()
    if ok:
        return "the computer is kept awake while a model runs ({})".format(what)
    raise Skip("cannot keep the computer awake here: {}. Runs still work; for long runs set "
               "the power settings so the computer does not sleep".format(what))


def check_qgis_version(version_int, version_text="", minimum=MIN_QGIS):
    """`version_int` is Qgis.QGIS_VERSION_INT (32812 = 3.28.12)."""
    if version_int is None:
        raise RuntimeError("could not read the QGIS version")
    v = int(version_int)
    text = "QGIS {} ({})".format(version_text or "?", v)
    if v < minimum:
        raise RuntimeError("{}: the plugin needs QGIS {}.{} or newer".format(
            text, minimum // 10000, (minimum // 100) % 100))
    return text


def check_gdal():
    gdal, osr = gdal_io._gdal()
    ver = gdal.VersionInfo("RELEASE_NAME")
    missing = [d for d in ("GTiff", "VRT", "MEM", "AAIGrid") if gdal.GetDriverByName(d) is None]
    if missing:
        raise RuntimeError("GDAL {} has no driver for {}".format(ver, ", ".join(missing)))
    s = osr.SpatialReference()
    s.ImportFromEPSG(26914)       # fails with PROJ's message when proj.db is not found
    if not s.IsProjected() or abs(s.GetLinearUnits() - 1.0) > 1e-9:
        raise RuntimeError("EPSG:26914 did not resolve to a projected CRS in metres")
    proj = ""
    try:
        proj = ", PROJ {}.{}.{}".format(osr.GetPROJVersionMajor(), osr.GetPROJVersionMinor(),
                                        osr.GetPROJVersionMicro())
    except Exception:
        pass
    return "GDAL {}{} at {}; {} drivers; drivers GTiff, VRT, MEM, AAIGrid present; " \
           "EPSG:26914 resolves".format(ver, proj, os.path.dirname(gdal.__file__),
                                        gdal.GetDriverCount())


def check_ogr_geopackage(workdir):
    """The GeoPackage of flood polygons goes through products.write_gpkg (OGR)."""
    from . import products
    path = os.path.join(workdir, "selftest.gpkg")
    ring = [(0.0, 0.0), (0.0, 100.0), (100.0, 100.0), (100.0, 0.0), (0.0, 0.0)]
    layers = {"poly": ("polygon", [([(ring, [])], {"depth_ft": 2.5})])}
    out = products.write_gpkg(path, layers, CRS)
    if not out:
        raise RuntimeError("products.write_gpkg returned nothing: OGR is not available")
    ds = GS.open_vector(path)
    lyr = ds.GetLayer(0)
    n = lyr.GetFeatureCount()
    assert n == 1, "GeoPackage has {} features, expected 1".format(n)
    code = lyr.GetSpatialRef().GetAuthorityCode(None) if lyr.GetSpatialRef() else None
    assert code == "26914", "GeoPackage CRS is {}, expected 26914".format(code)
    return "OGR GeoPackage writer works (1 polygon, EPSG:{})".format(code)


# ---------------------------------------------------------------------------------------
# synthetic data for the GDAL checks
# ---------------------------------------------------------------------------------------
def synthetic_valley_grid(nx=NX, ny=NY, dx=DX, x0=X0, y0=Y0):
    """A valley falling east at 1 m/km with walls 12 m high, an embankment across it at the
    middle (the dam) and a flat reservoir surface upstream of it. Metres, NaN nowhere."""
    xs = x0 + (np.arange(nx) + 0.5) * dx
    ys = y0 - (np.arange(ny) + 0.5) * dx
    X, Y = np.meshgrid(xs, ys)
    u = X - (x0 + 0.5 * nx * dx)              # east of the dam
    v = Y - (y0 - 0.5 * ny * dx)              # north of the valley axis
    z = 500.0 - 0.001 * u + 12.0 * (1.0 - np.exp(-(v / 250.0) ** 2)) \
        + 8.0 * np.exp(-(u / 40.0) ** 2)
    z = np.where(u < -40.0, np.maximum(z, 505.0), z)         # the reservoir's water surface
    return Grid(z, x0, y0, dx)


def plane_grid(nx=NX, ny=NY, dx=DX, x0=X0, y0=Y0):
    """z = 100 + 0.01 (x - x0) + 0.02 (y - y0): a linear surface, so any symmetric resampling
    kernel reproduces it exactly away from the edges."""
    xs = x0 + (np.arange(nx) + 0.5) * dx
    ys = y0 - (np.arange(ny) + 0.5) * dx
    X, Y = np.meshgrid(xs, ys)
    return Grid(100.0 + 0.01 * (X - x0) + 0.02 * (Y - y0), x0, y0, dx)


def class_codes(nx=NX, ny=NY):
    """An NLCD-like class raster in blocks, and {class: block slices} for the checks.
    Rows 60 and below are no-data (NaN)."""
    a = np.full((ny, nx), 82.0)
    blocks = {}
    a[20:40, 0:40] = 41.0
    blocks[41] = (slice(20, 40), slice(0, 40))
    a[20:40, 40:80] = 23.0
    blocks[23] = (slice(20, 40), slice(40, 80))
    a[20:40, 80:120] = 11.0
    blocks[11] = (slice(20, 40), slice(80, 120))
    a[40:60, 0:60] = 99.0                     # not an NLCD class: gets the default n
    blocks[99] = (slice(40, 60), slice(0, 60))
    a[60:, :] = np.nan
    blocks["nodata"] = (slice(60, ny), slice(0, nx))
    blocks[82] = (slice(0, 20), slice(0, nx))
    return a, blocks


def interior(a, k=1):
    return a[k:a.shape[0] - k, k:a.shape[1] - k]


def max_abs_diff(a, b, k=1):
    """Largest |a - b| over the interior (NaN-aware: NaN must match NaN)."""
    a, b = interior(np.asarray(a), k), interior(np.asarray(b), k)
    assert a.shape == b.shape, "shapes differ: {} vs {}".format(a.shape, b.shape)
    na, nb = np.isnan(a), np.isnan(b)
    assert (na == nb).all(), "no-data cells differ in {} places".format(int((na != nb).sum()))
    if na.all():
        return 0.0
    return float(np.max(np.abs(a[~na] - b[~na])))


class Case(object):
    """The synthetic DEM written with GDAL, shared by the GDAL checks."""

    def __init__(self, workdir, grid, path):
        self.workdir, self.grid, self.path = workdir, grid, path
        self.bounds, self.cell = grid.bounds, grid.dx

    def file(self, name):
        return os.path.join(self.workdir, name)


def _set_unit_type(path, unit):
    gdal_io._gdal()
    ds = GS.open_raster(path, update=True)
    ds.GetRasterBand(1).SetUnitType(unit)
    ds.FlushCache()
    ds = None


def make_case(workdir):
    """Write the synthetic valley with GeoTiffWriter and read it back with read_grid."""
    g = synthetic_valley_grid()
    path = os.path.join(workdir, "valley.tif")
    gdal_io.GeoTiffWriter(CRS)(g, path)
    assert os.path.exists(path), "GeoTiffWriter wrote no file"
    b = gdal_io.read_grid(path)
    assert b.data.shape == g.data.shape, "read back {} cells, wrote {}".format(
        b.data.shape, g.data.shape)
    assert abs(b.x0 - g.x0) < 1e-6 and abs(b.y0 - g.y0) < 1e-6 and abs(b.dx - g.dx) < 1e-9, \
        "georeference changed: wrote ({}, {}, {}) read ({}, {}, {})".format(
            g.x0, g.y0, g.dx, b.x0, b.y0, b.dx)
    err = float(np.nanmax(np.abs(b.data - g.data)))
    assert err < 1e-3, "elevations differ by up to {:.6f} m after a GeoTIFF round trip".format(err)
    epsg = gdal_io.epsg_of(b.crs_wkt)
    assert epsg == CRS, "CRS read back as {}, wrote {}".format(epsg, CRS)
    return Case(workdir, g, path), "{} x {} cells of {:g} m, EPSG {}, elevations within {:.1e} m " \
        "after a round trip".format(b.nx, b.ny, b.dx, epsg, err)


def check_crs_helpers(case=None):
    assert gdal_io.utm_epsg(-100.7, 47.2) == 26914, "utm_epsg(-100.7, 47.2) is {}".format(
        gdal_io.utm_epsg(-100.7, 47.2))
    assert gdal_io.utm_epsg(-98.0, 47.0) == 26914 and gdal_io.utm_epsg(-103.0, 47.0) == 26913
    m = gdal_io.linear_unit_m(CRS)
    assert m == 1.0, "linear unit of {} is {}".format(CRS, m)
    g = gdal_io.linear_unit_m("EPSG:4269")
    assert g is None, "a geographic CRS should have no linear unit, got {}".format(g)
    assert gdal_io.same_crs(CRS, CRS) and not gdal_io.same_crs(CRS, OTHER_CRS), \
        "same_crs does not tell {} from {}".format(CRS, OTHER_CRS)
    return "utm_epsg, linear_unit_m ({} m for {}), same_crs behave".format(m, CRS)


def check_terrain_read(case):
    t = gdal_io.GdalTerrain(case.path, CRS, gdal_io.Z_METERS)
    g = t.read(case.bounds, case.cell)
    assert g.data.shape == case.grid.data.shape, "read {} cells for a {} raster".format(
        g.data.shape, case.grid.data.shape)
    err = max_abs_diff(g.data, case.grid.data)
    assert err < 0.01, "GdalTerrain.read on the raster's own grid differs by up to {:.4f} m " \
                       "(a half-cell shift would show as about 0.5 m)".format(err)
    tf = gdal_io.GdalTerrain(case.path, CRS, gdal_io.Z_FEET)
    gf = tf.read(case.bounds, case.cell)
    ef = max_abs_diff(gf.data, case.grid.data * FT)
    assert ef < 0.01, "feet -> metres factor is off by {:.4f} m".format(ef)
    assert abs(tf.z_to_m - FT) < 1e-12, "z_to_m for feet is {}".format(tf.z_to_m)
    return "read on the raster's own grid within {:.1e} m; feet factor {:.4f}; " \
           "no-data value {}".format(err, tf.z_to_m, t.src_nodata)


def check_z_units(case):
    f, note = gdal_io.z_to_m_factor(case.path, gdal_io.Z_AUTO)
    assert f == 1.0 and "check" in note, \
        "a raster with no unit in a metre CRS should give metres with a 'check' note, got " \
        "{} / {}".format(f, note)
    got = []
    for unit, want in (("ft", FT), ("us-ft", US_FT), ("m", 1.0)):
        p = case.file("valley_{}.tif".format(unit.replace("-", "")))
        shutil.copyfile(case.path, p)
        _set_unit_type(p, unit)
        f2, note2 = gdal_io.z_to_m_factor(p, gdal_io.Z_AUTO)
        assert abs(f2 - want) < 1e-12 and "raster unit" in note2, \
            "band unit '{}' gave factor {} ({}), expected {}".format(unit, f2, note2, want)
        got.append(note2)
    t = gdal_io.GdalTerrain(case.file("valley_ft.tif"), CRS, gdal_io.Z_AUTO)
    assert abs(t.z_to_m - FT) < 1e-12, "GdalTerrain(auto) z_to_m {} for a feet raster".format(
        t.z_to_m)
    return "auto-detect: none -> metres with a 'check' note; ft, us-ft, m read from the band"


def check_manning_rasters(case):
    g = case.grid
    n = np.full(g.data.shape, 0.045)
    n[3, 3], n[4, 4], n[5, 5] = 0.0, 2.0, np.nan            # zero, above 1, no-data
    p = case.file("n.tif")
    gdal_io.GeoTiffWriter(CRS)(g.like(n), p)
    t = gdal_io.GdalTerrain(case.path, CRS, gdal_io.Z_METERS, p, 0.07)
    m = t.manning(case.bounds, case.cell)
    assert m is not None and m.data.shape == g.data.shape, "manning() returned {}".format(m)
    assert max_abs_diff(m.data[6:, 6:], np.full(m.data[6:, 6:].shape, 0.045), 1) < 1e-6, \
        "n values read from the raster are not 0.045"
    bad = [float(m.data[3, 3]), float(m.data[4, 4]), float(m.data[5, 5])]
    assert all(abs(b - 0.07) < 1e-6 for b in bad), \
        "zero, >1 and no-data cells should get the default n 0.07, got {}".format(bad)
    # NLCD class raster + lookup table
    codes, blocks = class_codes()
    q = case.file("nlcd.tif")
    gdal_io.GeoTiffWriter(CRS)(g.like(codes), q)
    from . import nd_data
    lut = nd_data.manning_lut(None, 0.07)
    tl = gdal_io.GdalTerrain(case.path, CRS, gdal_io.Z_METERS, q, 0.07, manning_lut=lut)
    ml = tl.manning(case.bounds, case.cell)
    seen = []
    for cls, (rs, cs) in blocks.items():
        want = 0.07 if cls in (99, "nodata") else lut[cls]
        got = ml.data[rs, cs]
        sub = got[1:-1, 1:-1]
        assert np.all(np.abs(sub - want) < 1e-6), \
            "class {}: n should be {} but the block holds {}..{}".format(
                cls, want, float(sub.min()), float(sub.max()))
        seen.append("{}={:g}".format(cls, want))
    return "n raster: bad cells -> default; NLCD lookup {}".format(", ".join(seen))


def check_initial_depth(case):
    from . import conditioning as cond
    g = case.grid
    x0, y0 = g.x0, g.y0
    cx, cy = x0 + 0.5 * g.nx * g.dx, y0 - 0.5 * g.ny * g.dx      # the dam
    poly = np.array([[cx - 500.0, cy - 100.0], [cx - 500.0, cy + 100.0],
                     [cx - 200.0, cy + 100.0], [cx - 200.0, cy - 100.0]])
    pond = cond.PondLower(poly, 3.0, name="selftest pond")
    edits = cond.Edits(ponds=[pond])
    want_grid = edits.apply(g)[1]
    assert want_grid is not None, "the pond did not find any water cells in the synthetic DEM"
    ct = cond.ConditionedTerrain(gdal_io.GdalTerrain(case.path, CRS, gdal_io.Z_METERS), edits)
    init = ct.initial_depth(case.bounds, case.cell)
    assert init is not None, "initial_depth returned nothing"
    err = max_abs_diff(init.data, want_grid.data)
    assert err < 0.01, "initial water depth differs from the in-memory result by {:.4f} m".format(
        err)
    n = int((init.data > 0).sum())
    assert n >= 50 and abs(float(init.data.max()) - 3.0) < 0.01, \
        "pond has {} wet cells, deepest {:.3f} m (expected 3.000)".format(n, float(init.data.max()))
    p = case.file("init_depth.tif")
    gdal_io.GeoTiffWriter(CRS)(init, p)
    back = gdal_io.read_grid(p)
    e2 = max_abs_diff(back.data, init.data, 0)
    assert e2 < 1e-4, "initial depth changed by {:.6f} m in the GeoTIFF".format(e2)
    dem = ct.read(case.bounds, case.cell)
    lowered = float(np.nanmin(g.data[init.data > 0]) - np.nanmin(dem.data[init.data > 0]))
    assert abs(lowered - 3.0) < 0.01, "the pond bed is {:.3f} m lower, expected 3.000".format(
        lowered)
    return "pond of {} cells: initial depth {:.2f} m, bed lowered {:.2f} m, GeoTIFF round trip " \
           "exact".format(n, float(init.data.max()), lowered)


def check_warp(case):
    b = case.bounds
    # 1. an exact shift of the CRS (false easting +1000 m)
    t = gdal_io.GdalTerrain(case.path, SHIFTED_SRS, gdal_io.Z_METERS)
    sb = (b[0] + SHIFT_M, b[1], b[2] + SHIFT_M, b[3])
    g = t.read(sb, case.cell)
    e1 = max_abs_diff(g.data, case.grid.data)
    assert e1 < 0.01, "warp into a CRS shifted 1000 m east differs by up to {:.4f} m (a cell " \
                      "misalignment or a datum shift would show here)".format(e1)
    # 2. resampling to a coarser cell on a linear surface
    pl = plane_grid()
    pp = case.file("plane.tif")
    gdal_io.GeoTiffWriter(CRS)(pl, pp)
    coarse = 2.0 * case.cell
    tg = gdal_io.GdalTerrain(pp, CRS, gdal_io.Z_METERS).read(b, coarse)
    ny, nx = tg.data.shape
    xs = b[0] + (np.arange(nx) + 0.5) * coarse
    ys = b[3] - (np.arange(ny) + 0.5) * coarse
    X, Y = np.meshgrid(xs, ys)
    want = 100.0 + 0.01 * (X - X0) + 0.02 * (Y - Y0)
    e2 = max_abs_diff(tg.data, want, 2)
    assert e2 < 0.005, "coarser read of a plane differs by up to {:.4f} m".format(e2)
    # 3. GeoTiffWriter re-projecting on the way out
    shifted = Grid(case.grid.data, b[0] + SHIFT_M, b[3], case.cell)
    out = case.file("reproj.tif")
    gdal_io.GeoTiffWriter(SHIFTED_SRS, CRS, case.cell)(shifted, out)
    r = gdal_io.read_grid(out)
    assert abs(r.x0 - b[0]) < 0.01 and abs(r.y0 - b[3]) < 0.01, \
        "re-projected raster starts at ({:.3f}, {:.3f}), expected ({:.3f}, {:.3f})".format(
            r.x0, r.y0, b[0], b[3])
    ny2, nx2 = min(r.ny, case.grid.ny), min(r.nx, case.grid.nx)
    assert abs(r.ny - case.grid.ny) <= 1 and abs(r.nx - case.grid.nx) <= 1, \
        "re-projected raster is {} x {} cells, expected about {} x {}".format(
            r.nx, r.ny, case.grid.nx, case.grid.ny)
    e3 = max_abs_diff(r.data[:ny2, :nx2], case.grid.data[:ny2, :nx2])
    assert e3 < 0.01, "re-projected values differ by up to {:.4f} m".format(e3)
    return "shifted-CRS warp within {:.1e} m; 2x coarser read of a plane within {:.1e} m; " \
           "writer re-projection within {:.1e} m".format(e1, e2, e3)


def check_vrt(case):
    g = case.grid
    tiles = case.file("tiles")
    os.makedirs(tiles, exist_ok=True)
    w = gdal_io.GeoTiffWriter(CRS)
    w(g.window(0, g.ny, 0, g.nx // 2), os.path.join(tiles, "tile_west.tif"))
    w(g.window(0, g.ny, g.nx // 2, g.nx), os.path.join(tiles, "tile_east.tif"))
    stray = Grid(np.full((10, 10), 100.0), g.x0, g.y0 + 5000.0, g.dx)
    gdal_io.GeoTiffWriter(OTHER_CRS)(stray, os.path.join(tiles, "tile_other_crs.tif"))
    vrt = case.file("mosaic.vrt")
    r = gdal_io.build_vrt(tiles, vrt)
    assert r["tiles"] == 2, "VRT holds {} tiles, expected the 2 in the majority CRS".format(
        r["tiles"])
    assert [os.path.basename(x) for x in r["left_out"]] == ["tile_other_crs.tif"], \
        "tile in another CRS was not left out: {}".format(r["left_out"])
    assert r["cell_sizes"] == [g.dx], "cell sizes {}".format(r["cell_sizes"])
    for a, b in zip(r["bounds"], g.bounds):
        assert abs(a - b) < 1e-6, "VRT bounds {} differ from the tiles' {}".format(
            r["bounds"], g.bounds)
    t = gdal_io.GdalTerrain(vrt, CRS, gdal_io.Z_METERS)
    m = t.read(case.bounds, case.cell)
    e = max_abs_diff(m.data, g.data)
    assert e < 0.01, "reading the mosaic across the seam differs by up to {:.4f} m".format(e)
    return "VRT of 2 tiles (third tile in another CRS left out); read across the seam within " \
           "{:.1e} m".format(e)


def check_fill_gaps(case):
    g = case.grid
    hole = g.data.copy()
    hole[38:43, 80:86] = np.nan                      # 5 x 6 cells on the valley floor
    n_hole = 5 * 6
    src = case.file("holes.tif")
    gdal_io.GeoTiffWriter(CRS)(g.like(hole), src)
    chk = gdal_io.read_grid(src)
    assert int(np.isnan(chk.data).sum()) == n_hole, \
        "no-data hole read back as {} cells, wrote {}".format(int(np.isnan(chk.data).sum()),
                                                              n_hole)
    out = case.file("filled.tif")
    r = gdal_io.fill_gaps(src, out, None, 20, 0)
    assert r["cells_filled"] == n_hole and r["cells_left"] == 0, \
        "fill_gaps reports {} filled, {} left; expected {} and 0".format(
            r["cells_filled"], r["cells_left"], n_hole)
    f = gdal_io.read_grid(out)
    assert not np.isnan(f.data).any(), "filled raster still has no-data cells"
    inside = f.data[38:43, 80:86] - g.data[38:43, 80:86]
    assert float(np.abs(inside).max()) < 0.5, \
        "filled values are off by up to {:.3f} m (the valley floor is nearly flat there)".format(
            float(np.abs(inside).max()))
    keep = np.ones(g.data.shape, dtype=bool)
    keep[38:43, 80:86] = False
    assert float(np.abs(f.data[keep] - g.data[keep]).max()) < 1e-3, \
        "cells outside the hole changed"
    # only an extent: 30 x 20 cells around the hole
    ext = (g.x0 + 60 * g.dx, g.y0 - 50 * g.dx, g.x0 + 90 * g.dx, g.y0 - 30 * g.dx)
    out2 = case.file("filled_ext.tif")
    r2 = gdal_io.fill_gaps(src, out2, ext, 20, 0)
    f2 = gdal_io.read_grid(out2)
    assert (f2.nx, f2.ny) == (30, 20), "extent fill gave {} x {} cells, expected 30 x 20".format(
        f2.nx, f2.ny)
    assert r2["cells_filled"] == n_hole and r2["cells_left"] == 0, \
        "extent fill reports {} filled, {} left".format(r2["cells_filled"], r2["cells_left"])
    return "hole of {} cells filled within {:.3f} m, rest untouched; extent fill gives 30 x 20 " \
           "cells".format(n_hole, float(np.abs(inside).max()))


def check_nlcd_raster(case):
    from . import nd_data
    g = case.grid
    codes, blocks = class_codes()
    p = case.file("nlcd_classes.tif")
    gdal_io.GeoTiffWriter(CRS)(g.like(codes), p)
    lut = nd_data.manning_lut(None, 0.06)
    out = case.file("n_from_nlcd.tif")
    r = gdal_io.nlcd_to_manning_raster(p, out, lut)
    assert r["classes"] == [11, 23, 41, 82, 99], "classes found {}".format(r["classes"])
    assert r["unknown_classes"] == [99], "unknown classes {}".format(r["unknown_classes"])
    n = gdal_io.read_grid(out)
    for cls, (rs, cs) in blocks.items():
        want = 0.06 if cls in (99, "nodata") else lut[cls]
        sub = n.data[rs, cs]
        assert np.all(np.abs(sub - want) < 1e-6), \
            "class {}: n should be {} but the block holds {}..{}".format(
                cls, want, float(np.nanmin(sub)), float(np.nanmax(sub)))
    out2 = case.file("n_from_nlcd_like.tif")
    gdal_io.nlcd_to_manning_raster(p, out2, lut, case.path)      # the warp-to-DEM-grid path
    n2 = gdal_io.read_grid(out2)
    assert n2.data.shape == g.data.shape, "n raster on the DEM grid has {} cells, DEM {}".format(
        n2.data.shape, g.data.shape)
    e = max_abs_diff(n2.data, n.data)
    assert e < 1e-6, "matching the DEM grid changed n by {}".format(e)
    return "classes {} -> n {}; class 99 got the default; matching the DEM grid works".format(
        r["classes"], ", ".join("{}={:g}".format(c, lut[c]) for c in (11, 23, 41, 82)))


# ---------------------------------------------------------------------------------------
# the models
# ---------------------------------------------------------------------------------------
def check_lisflood_exe(exe, workdir):
    """Find-and-run check of the LISFLOOD-FP build (the plugin's own check_lisflood)."""
    from . import lisflood
    if not exe:
        raise Skip("LISFLOOD-FP not found (set it in Processing > Options > Providers > "
                   "ND Dam Breach Runner, or build it with scripts/install_macos.sh)")
    if not os.path.isfile(exe):
        raise RuntimeError("the LISFLOOD-FP path does not exist: {}".format(exe))
    if not lisflood.is_builtin(exe) and not os.access(exe, os.X_OK):
        raise RuntimeError("the LISFLOOD-FP file is not executable: {}".format(exe))
    r = lisflood.check_lisflood(exe, os.path.join(workdir, "lisflood_check"))
    if not r["ok"]:
        raise RuntimeError("{}: {}".format(exe, r["message"]))
    if not r.get("patched"):
        raise RuntimeError("{} runs but is not the patched build (no arrival-depth keyword): "
                           "{}".format(exe, r["message"]))
    return "{}: {}".format(exe, r["message"])


def check_lisflood_calibrate(exe, workdir, threads=0):
    """calibrate() on a small flood: the timing the run-time estimate is based on."""
    from . import lisflood
    if not exe or not os.path.isfile(exe):
        raise Skip("no LISFLOOD-FP executable")
    t0 = time.time()
    rate = lisflood.calibrate(exe, os.path.join(workdir, "lisflood_cal"), threads,
                              cell_m=MODEL_CELL_M, sim_s=900.0)
    dt = time.time() - t0
    if not rate:
        raise RuntimeError("calibrate() returned {}: the small test flood did not run (see the "
                           "LISFLOOD-FP check above for the reason)".format(rate))
    assert rate > 1e3, "calibrate() rate {} cell-seconds per second is implausibly low".format(rate)
    return "test flood ran in {:.1f} s: {:,.0f} cell-seconds of simulation per second at " \
           "{:g} m".format(dt, rate, MODEL_CELL_M)


def check_anuga_env(python, workdir):
    from . import anuga_model
    if not python:
        raise Skip("no ANUGA Python interpreter found (only needed for the ANUGA tier)")
    r = anuga_model.check_anuga(python, os.path.join(workdir, "anuga_check"))
    if not r["ok"]:
        raise RuntimeError("{}: {}".format(python, r["message"]))
    return "{}: {}".format(python, r["message"])


def model_valley(x, y):
    """The valley the model tests use, in local metres: a reservoir west of an embankment at
    x = 0 and a prairie valley falling east at 1 m/km with a small channel."""
    top_m, h_m = TOP_FT * FT, DAM_H_FT * FT
    bed = top_m - h_m - 0.001 * np.clip(x, 0, None)
    z = bed + 0.004 * np.abs(y) - np.where(np.abs(y) < 12, 1.0 - np.abs(y) / 12, 0.0)
    z = z + 0.3 * np.sin(x / 700.0) * np.cos(y / 500.0)
    res = np.where(x < -40, np.maximum(z, top_m - 1.0), z)
    emb = top_m - np.abs(x) / 40.0 * (top_m - bed)
    on_emb = (np.abs(x) <= 40) & (np.abs(y) < 400)
    return np.where(on_emb, np.maximum(res, emb), res)


def build_model_dem(workdir, dx=20.0):
    """The model valley as a GeoTIFF (UTM 14N, metres) with the dam at (DAM_X, DAM_Y)."""
    lo_x, hi_x, lo_y, hi_y = -2500.0, 3000.0, -2500.0, 2500.0
    nx, ny = int(round((hi_x - lo_x) / dx)), int(round((hi_y - lo_y) / dx))
    xs = lo_x + (np.arange(nx) + 0.5) * dx
    ys = hi_y - (np.arange(ny) + 0.5) * dx
    X, Y = np.meshgrid(xs, ys)
    g = Grid(model_valley(X, Y), DAM_X + lo_x, DAM_Y + hi_y, dx)
    path = os.path.join(workdir, "model_valley.tif")
    gdal_io.GeoTiffWriter(CRS)(g, path)
    return path, g


def small_dam():
    from . import breach
    return breach.DamData(dam_id="SELFTEST", dam_height_ft=DAM_H_FT, storage_top_acft=20.0,
                          top_of_dam_ft=TOP_FT, surface_area_top_ac=4.0, name="self-test valley")


def run_small_model(exe, workdir, tier="lisflood", anuga_python="", threads=0, terrain=None,
                    writer=None, dam_xy=None, progress=None, log=None, cancel=None,
                    products=True):
    """Run workflow.run_dam on the model valley with the smallest domain (1 mi x 0.6 mi) and a
    20 m cell: seconds for LISFLOOD-FP, under a minute with ANUGA. Uses GDAL (a GeoTIFF DEM
    and GeoTiffWriter) unless `terrain` and `writer` are given. Returns the RunResult."""
    from . import breach, workflow
    if terrain is None:
        path, _g = build_model_dem(workdir)
        terrain = gdal_io.GdalTerrain(path, CRS, gdal_io.Z_METERS)
        writer = gdal_io.GeoTiffWriter(CRS)
        dam_xy = (DAM_X, DAM_Y)
    st = workflow.RunSettings(
        tier=tier, breach_method=breach.METHOD_PIPING, cell_size_m=MODEL_CELL_M,
        initial_length_mi=1.0, half_width_mi=0.3, max_iterations=1, lisflood_exe=exe,
        anuga_python=anuga_python, threads=threads, products=products,
        anuga_fine_area_m2=800.0, anuga_coarse_area_m2=3000.0, anuga_near_field_mi=0.3,
        eap_map="svg")      # v3.2.0: the self-test checks the preview is drawn
    out = os.path.join(workdir, "SELFTEST_" + ("anuga" if tier != "lisflood" else "lisflood"))
    return workflow.run_dam(small_dam(), dam_xy or (0.0, 0.0), st, terrain, writer, out, None,
                            progress, log, cancel)


def verify_model_rasters(res, cell_m=MODEL_CELL_M, expect_model="LISFLOOD-FP"):
    """Numeric sanity of the rasters a run wrote, read back through GDAL."""
    from .workflow import FINAL_NAMES
    s = res.summary
    assert s.get("breach_occurs", True), "the small dam did not fail: {}".format(s)
    assert s.get("final_model") == expect_model, \
        "final model is {}, expected {}".format(s.get("final_model"), expect_model)
    assert s.get("flooded_acres", 0) > 5.0, "flooded only {} acres".format(s.get("flooded_acres"))
    rd = {}
    for kind, name in FINAL_NAMES.items():
        p = res.final.get(kind) or os.path.join(res.folder, name)
        assert os.path.exists(p), "{} was not written ({})".format(name, p)
        rd[kind] = gdal_io.read_grid(p)
    d = rd["depth"]
    assert abs(d.dx - cell_m) < 1e-6, "depth raster cell is {} m, expected {}".format(d.dx, cell_m)
    epsg = gdal_io.epsg_of(d.crs_wkt)
    assert epsg == CRS, "depth raster CRS is {}, expected {}".format(epsg, CRS)
    wet = np.isfinite(d.data)
    n = int(wet.sum())
    assert n > 50, "depth raster has only {} flooded cells".format(n)
    dmax = float(np.nanmax(d.data))
    assert abs(dmax - s["max_depth_ft"]) < 0.05, \
        "max depth in the raster {:.2f} ft differs from the report's {}".format(
            dmax, s["max_depth_ft"])
    assert 0.5 < dmax < 80.0, "max depth {:.2f} ft is not plausible for this dam".format(dmax)
    ac = n * cell_m ** 2 / 4046.8564224
    assert abs(ac - s["flooded_acres"]) <= 0.02 * s["flooded_acres"] + 0.5, \
        "raster holds {:.1f} acres, report says {}".format(ac, s["flooded_acres"])
    v = rd["velocity"].data
    assert np.isfinite(v).any() and float(np.nanmax(v)) > 0.1, "no velocity in the raster"
    a = rd["arrival"].data
    assert np.isfinite(a).any() and float(np.nanmin(a)) >= 0.0, "arrival times are missing or < 0"
    w = rd["wse"].data
    assert np.isfinite(w).any(), "no water-surface elevations in the raster"
    return "{}: {:,} cells, {:.1f} acres, max depth {:.1f} ft, max velocity {:.1f} ft/s, " \
           "latest arrival {} h".format(expect_model, n, ac, dmax, float(np.nanmax(v)),
                                        s.get("latest_arrival_hr"))


def verify_products(res, expect_gpkg=True):
    bad = [w for w in res.warnings if "Could not write" in w]
    assert not bad, "; ".join(bad)
    need = ["depth_polygons", "extent", "arrival_zones"] + (["geopackage"] if expect_gpkg else [])
    missing = [k for k in need if not res.vectors.get(k) or not os.path.exists(res.vectors[k])]
    assert not missing, "not written: {} (have {})".format(", ".join(missing),
                                                           sorted(res.vectors))
    assert res.eap_map and os.path.exists(res.eap_map) and os.path.getsize(res.eap_map) > 1000, \
        "EAP inundation map missing or empty"
    for p in (res.report_md, res.report_json):
        assert p and os.path.exists(p), "run report missing ({})".format(p)
    return "flood polygons, extent, arrival zones{}, EAP map and run report written".format(
        ", GeoPackage" if expect_gpkg else "")


# ---------------------------------------------------------------------------------------
# everything, in order
# ---------------------------------------------------------------------------------------
GDAL_CHECKS = (("GDAL: write and read back a synthetic valley DEM", None),
               ("GDAL: CRS helpers", check_crs_helpers),
               ("GDAL: GdalTerrain.read (metres and feet)", check_terrain_read),
               ("GDAL: elevation units from the raster", check_z_units),
               ("GDAL: Manning's n raster and NLCD lookup", check_manning_rasters),
               ("GDAL: pond initial depth", check_initial_depth),
               ("GDAL: warp, resample, re-project on write", check_warp),
               ("GDAL: VRT mosaic of two tiles", check_vrt),
               ("GDAL: fill no-data gaps", check_fill_gaps),
               ("GDAL: NLCD to Manning's n raster", check_nlcd_raster))


def _case_check(report, workdir):
    case, detail = make_case(workdir)
    report.ctx["case"] = case
    return detail


# ---------------------------------------------------------------------------------------
# Processing registry checks (pure: the caller passes the registry, so fakes can stand in)
# ---------------------------------------------------------------------------------------
def check_algorithms(registry, classes, provider_id="ndbreach"):
    """Every algorithm class is registered under provider_id and builds its parameters.

    `classes` are the plugin's algorithm classes; `registry` is QgsApplication's
    processingRegistry(). Each one is instantiated fresh, initAlgorithm() is run, the parameter
    names must be unique and non-empty, and the registered instance must have the same
    parameters. Raises AssertionError listing every problem."""
    problems, n_params, names = [], 0, []
    for cls in classes:
        label = getattr(cls, "__name__", str(cls))
        try:
            alg = cls()
            name = alg.name()
            label = "{} ({})".format(name, label)
            alg.initAlgorithm()
            defs = list(alg.parameterDefinitions())
            pnames = [d.name() for d in defs]
            if not name or not alg.displayName():
                problems.append("{}: empty name or display name".format(label))
            if not pnames and not list(getattr(alg, "outputDefinitions", lambda: [])()):
                problems.append("{}: defines no parameters and no outputs".format(label))
            dup = sorted(set(x for x in pnames if pnames.count(x) > 1))
            if dup or any(not x for x in pnames):
                problems.append("{}: duplicate or empty parameter names {}".format(label, dup))
            n_params += len(pnames)
            names.append(name)
            reg = registry.algorithmById("{}:{}".format(provider_id, name))
            if reg is None:
                problems.append("{}: not registered as {}:{} (is the provider loaded?)".format(
                    label, provider_id, name))
            else:
                rnames = [d.name() for d in reg.parameterDefinitions()]
                if rnames != pnames:
                    problems.append("{}: the registered algorithm's parameters differ from a "
                                    "fresh instance's".format(label))
            inst = alg.createInstance()
            if type(inst) is not cls:
                problems.append("{}: createInstance() returned {}".format(
                    label, type(inst).__name__))
        except Exception as e:
            problems.append("{}: {}: {}".format(label, type(e).__name__, e))
    if len(set(names)) != len(names):
        problems.append("duplicate algorithm names: {}".format(
            sorted(set(x for x in names if names.count(x) > 1))))
    if problems:
        raise AssertionError("{} problem(s):\n{}".format(len(problems), "\n".join(problems)))
    return "{} algorithms registered as {}:*, {} parameters built".format(
        len(names), provider_id, n_params)


def describe_ndhazard(registry, provider_id="ndhazard"):
    """The ndhazard provider's algorithms with their parameter names (one line each). Raises
    Skip when the provider is not installed."""
    prov = registry.providerById(provider_id)
    if prov is None:
        raise Skip("the '{}' provider is not installed (only needed for the hazard-class "
                   "hand-off)".format(provider_id))
    algs = list(prov.algorithms())
    if not algs:
        raise AssertionError("the '{}' provider has no algorithms (loaded but empty)".format(
            provider_id))
    lines = ["provider '{}' with {} algorithm(s):".format(provider_id, len(algs))]
    for a in algs:
        names = [d.name() for d in a.parameterDefinitions()]
        lines.append("{}: {}".format(a.id(), ", ".join(names) or "(no parameters)"))
    return "\n".join(lines)


def check_handoff_dry_run(registry, workdir, find=None, build=None):
    """The hand-off to ndhazard: locate its classification algorithm and build its parameters
    for a fake dam, without running it. `find`/`build` default to core.handoff's. Skips (with
    the reason) when there is no ndhazard or the parameters need a mapping."""
    from . import handoff
    find = find or handoff.find_algorithm
    build = build or handoff.build_parameters
    alg = find("classify", registry=registry)
    if alg is None:
        raise Skip("no ndhazard classification algorithm found (hand-off is optional)")
    folder = os.path.join(workdir, "handoff_dam")
    os.makedirs(folder, exist_ok=True)
    rasters = dict((k, os.path.join(folder, k + ".tif")) for k in
                   ("depth", "velocity", "arrival", "wse"))
    try:
        params = build(alg, "SELFTEST", folder, workdir, rasters, None,
                       os.path.join(folder, "_classification"))
    except handoff.HandoffError as e:
        raise Skip("{} needs a mapping JSON in the Classify tool: {}".format(alg.id(), e))
    return "{}: {} of its parameters filled automatically ({})".format(
        alg.id(), len(params), ", ".join(sorted(params)))


def run_all(report, workdir, lisflood_exe="", anuga_python="", run_model=True, threads=0,
            before=(), after=(), progress=None):
    """Run every check into `report`. `before` / `after` are (name, callable) pairs the caller
    adds around the built-in ones (the QGIS-only checks). `progress(percent, text)` is
    called after each check."""
    os.makedirs(workdir, exist_ok=True)
    steps = 7 + len(GDAL_CHECKS) + 3 + len(before) + len(after) + (3 if run_model else 0)
    done = [0]

    def tick(text):
        done[0] += 1
        if progress:
            progress(min(100.0, 100.0 * done[0] / steps), text)

    def run(name, fn, *a, **k):
        r = report.run(name, fn, *a, **k)
        tick(name)
        return r

    def skip(name, why):
        r = report.skip(name, why)
        tick(name)
        return r

    for name, fn in before:
        run(name, fn)
    run("Python", check_python)
    run("numpy", check_numpy)
    run("scipy", check_scipy)
    run("Flood-domain outline (contourpy or built-in)", check_contourpy)
    run("Keep the computer awake during runs", check_keep_awake)
    gdal_ok = run("GDAL (osgeo) importable", check_gdal).status == PASS
    if gdal_ok:
        run("OGR GeoPackage writer", check_ogr_geopackage, workdir)
    else:
        skip("OGR GeoPackage writer", "GDAL is not importable")
    report.ctx.pop("case", None)
    for name, fn in GDAL_CHECKS:
        if not gdal_ok:
            skip(name, "GDAL is not importable")
        elif fn is None:                                  # the DEM every other check reads
            run(name, _case_check, report, workdir)
        elif report.ctx.get("case") is None:
            skip(name, "the synthetic DEM could not be written and read back")
        else:
            run(name, fn, report.ctx["case"])
    lf_ok = run("LISFLOOD-FP executable", check_lisflood_exe, lisflood_exe,
                workdir).status == PASS
    if lf_ok:
        run("LISFLOOD-FP calibrate() on a small flood", check_lisflood_calibrate, lisflood_exe,
            workdir, threads)
    else:
        skip("LISFLOOD-FP calibrate() on a small flood", "no working LISFLOOD-FP executable")
    an_ok = run("ANUGA environment", check_anuga_env, anuga_python, workdir).status == PASS
    if run_model:
        name = "Small model: LISFLOOD-FP end to end"
        model_ok = False
        if not lf_ok:
            skip(name, "LISFLOOD-FP is not installed or failed its check")
            skip("Small model: products", "no model run")
        elif not gdal_ok:
            skip(name, "GDAL is not importable")
            skip("Small model: products", "no model run")
        else:
            box = {}

            def small():
                box["res"] = run_small_model(lisflood_exe, workdir, "lisflood", "", threads)
                return verify_model_rasters(box["res"], expect_model="LISFLOOD-FP")
            model_ok = run(name, small).status == PASS
            if box.get("res") is not None:
                gp = report.status_of("OGR GeoPackage writer") == PASS
                run("Small model: products", verify_products, box["res"], gp)
            else:
                skip("Small model: products", "the model run failed")
        name = "Small model: ANUGA tier"
        if not an_ok:
            skip(name, "no working ANUGA environment")
        elif not model_ok:
            skip(name, "the LISFLOOD-FP small model did not pass")
        else:
            def small_anuga():
                res = run_small_model(lisflood_exe, workdir, "lisflood+anuga", anuga_python,
                                      threads, products=False)
                return verify_model_rasters(res, expect_model="ANUGA")
            run(name, small_anuga)
    for name, fn in after:
        run(name, fn)
    return report


def main(argv=None):
    """python -m nd_breach_runner.core.selftest [--workdir D] [--lisflood EXE] [--anuga-python PY]
    [--no-model] [--report FILE] [--keep]"""
    import argparse
    from . import nd_data
    ap = argparse.ArgumentParser(prog="selftest", description=main.__doc__)
    ap.add_argument("--workdir", default="")
    ap.add_argument("--lisflood", default="")
    ap.add_argument("--anuga-python", default="")
    ap.add_argument("--no-model", action="store_true")
    ap.add_argument("--threads", type=int, default=0)
    ap.add_argument("--report", default="")
    ap.add_argument("--keep", action="store_true", help="keep the working files")
    a = ap.parse_args(argv)
    exe = a.lisflood or nd_data.detect_lisflood()
    py = a.anuga_python or nd_data.detect_anuga_python()
    work = a.workdir or tempfile.mkdtemp(prefix="ndbreach_selftest_")
    rep = Report(on_result=lambda r: print("[{}] {}".format(r.status, r.name), flush=True))
    rep.info = environment_info()
    print("Working in", work, flush=True)
    run_all(rep, work, exe, py, not a.no_model, a.threads)
    text = rep.text()
    print()
    print(text)
    print()
    print(rep.developer_block())
    if a.report:
        with open(a.report, "w", encoding="utf-8") as f:
            f.write(text + "\n\n" + rep.developer_block() + "\n")
    if not a.workdir and not a.keep and rep.ok():
        shutil.rmtree(work, ignore_errors=True)
    return 0 if rep.ok() else 1


if __name__ == "__main__":
    sys.exit(main())
