"""0.11.1: stability fixes.

Run: PYTHONPATH=tests/qgis_mock:<parent of the plugin> python3 tests/test_v0111.py
(any FAKE_QGIS_FLAVOR: 3.28, 3.40 or 4.0)

 1 compat.py resolves every name in this flavour; the plugin registers its provider once through
   initProcessing (what qgis_process calls) and metadata.txt declares it
 2 LISFLOOD-FP's output no longer fills an unread pipe (a long run used to hang until the
   timeout); cancel and timeout wait for the process
 3 one dam's unhandled error is that dam's FAILED result, in sequential and parallel batches
 4 triage: no traceable flow path -> untraced, never "nothing inside the envelope"
 5 built-in engine: NaN DEM cells are walls; an unstable run stops with a clear error; the
   faster flux code gives bit-for-bit the 0.11.0 numbers
 6 breach equations: invalid inputs -> ValueError in plain words (were NaN / complex / ZeroDivision)
 7 storage: an undeclared -9999 fill is no-data
 8 GDAL: a damaged raster names the file; no process-wide gdal.UseExceptions()
"""
import configparser
import os
import stat
import sys
import tempfile
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(1, os.path.dirname(os.path.dirname(HERE)))

from nd_dam_screening import compat as C  # noqa: E402
from nd_dam_screening.core import (breach_hydro as bh, fv_engine as fe, runner as rn,  # noqa: E402
                                   storage as ST, triage as TR)

FLAVOR = os.environ.get("FAKE_QGIS_FLAVOR", "3.28")
PLUGIN = os.path.dirname(HERE)
passed = []


def check(name):
    def deco(fn):
        fn()
        passed.append(name)
        print("ok  ", name)
        return fn
    return deco


# ---------------------------------------------------------------------------- 1
@check("compat names resolve")
def _():
    for n in ("NUM_DOUBLE", "NUM_INT", "FILE_FILE", "FILE_FOLDER", "SRC_POINT", "SRC_LINE", "SRC_POLYGON",
              "FIELD_ANY", "FIELD_NUMERIC", "GEOM_POINT", "GEOM_LINE", "GEOM_POLYGON", "WKB_POINT",
              "WKB_LINESTRING", "WRITER_NO_ERROR", "WRITER_OVERWRITE_FILE", "WRITER_OVERWRITE_LAYER",
              "FT_INT", "FT_LONGLONG", "FT_DOUBLE", "FT_STRING", "FT_BOOL"):
        assert getattr(C, n) is not None, n
    from qgis.PyQt.QtCore import QMetaType
    is_meta = isinstance(C.FT_DOUBLE, QMetaType.Type)
    assert C.FIELD_TYPES_ARE_METATYPE == is_meta
    assert is_meta == (FLAVOR != "3.28"), (FLAVOR, C.FT_DOUBLE)   # QGIS 3.28 takes QVariant types
    if FLAVOR.startswith("4"):
        from qgis.core import Qgis
        assert C.SRC_POINT is Qgis.ProcessingSourceType.VectorPoint
        assert C.NUM_DOUBLE is Qgis.ProcessingNumberParameterType.Double


@check("initProcessing registers the provider once; metadata declares it")
def _():
    from qgis.core import QgsApplication
    from nd_dam_screening import plugin as P
    reg = QgsApplication.processingRegistry()
    before = len(reg.providers)
    p = P.NDDamScreeningPlugin(None)          # qgis_process: no interface, initGui never called
    p.initProcessing()
    p.initProcessing()
    assert len(reg.providers) == before + 1 and p.provider is not None
    assert len(p.provider.algs) == 23, len(p.provider.algs)     # 0.16.0: + getdamdata
    p.unload()
    assert len(reg.providers) == before and p.provider is None
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(os.path.join(PLUGIN, "metadata.txt"), encoding="utf-8")
    g = cp["general"]
    assert g["hasProcessingProvider"] == "yes" and g["supportsQt6"] == "True"
    assert g["qgisMaximumVersion"] == "4.99" and g["version"] == "0.20.0"


# ---------------------------------------------------------------------------- 2
def _fake_solver(lines, sleep_s=0.0, code=0):
    d = tempfile.mkdtemp(prefix="nddam_fake_lf_")
    exe = os.path.join(d, "lisflood")
    with open(exe, "w") as f:
        f.write("#!{}\nimport sys, time\n".format(sys.executable))
        f.write("for i in range({}):\n    print('step %d ' % i + 'x' * 100)\n".format(lines))
        f.write("sys.stdout.flush()\ntime.sleep({})\nsys.exit({})\n".format(sleep_s, code))
    os.chmod(exe, os.stat(exe).st_mode | stat.S_IEXEC)
    return exe, d


@check("a solver writing 2 MB of output finishes (no pipe deadlock)")
def _():
    if os.name == "nt":
        return
    exe, d = _fake_solver(20000)                # ~2 MB, far beyond a pipe buffer
    t0 = time.time()
    rn.run_lisflood(exe, "run.par", d, timeout_s=60)
    assert time.time() - t0 < 30
    assert os.path.getsize(os.path.join(d, "lisflood.log")) > 1_000_000


@check("solver failure quotes the end of its log; timeout and cancel stop it")
def _():
    if os.name == "nt":
        return
    exe, d = _fake_solver(5, code=3)
    try:
        rn.run_lisflood(exe, "run.par", d, timeout_s=60)
        raise AssertionError("expected a failure")
    except RuntimeError as e:
        assert "solver exit 3" in str(e) and "step 4" in str(e)
    exe, d = _fake_solver(1, sleep_s=30)
    t0 = time.time()
    try:
        rn.run_lisflood(exe, "run.par", d, timeout_s=1)
        raise AssertionError("expected a timeout")
    except TimeoutError:
        assert time.time() - t0 < 10
    t0 = time.time()
    try:
        rn.run_lisflood(exe, "run.par", d, timeout_s=60, cancel=lambda: time.time() - t0 > 1)
        raise AssertionError("expected cancel")
    except rn.Cancelled:
        assert time.time() - t0 < 15


# ---------------------------------------------------------------------------- 3
@check("one dam's error does not end the batch (sequential and parallel)")
def _():
    def worker(it):
        if it["dam_id"] == "B":
            raise ZeroDivisionError("boom")
        return {"dam_id": it["dam_id"], "status": "ok"}
    items = [{"dam_id": k} for k in "ABC"]
    for workers in (1, 3):
        done = []
        res = rn.run_batch(items, worker, workers=workers, on_done=lambda it, r: done.append(r))
        st = {r["dam_id"]: r["status"] for r in res}
        assert st == {"A": "ok", "B": "FAILED", "C": "ok"}, st
        bad = [r for r in res if r["dam_id"] == "B"][0]
        assert "zero in the dam record of dam B" in bad["notes"][0]           # 0.13: plain words (core/errors.py)
        assert len(done) == 3


# ---------------------------------------------------------------------------- 4
@check("triage: no traceable flow path is 'untraced', not an empty envelope")
def _():
    y, x = np.mgrid[0:40, 0:50]
    dem = 100.0 + 0.05 * x                     # drains to the west edge
    gt = (500000.0, 10.0, 0.0, 5200000.0, 0.0, -10.0)
    r = TR.screen(dem, gt, (20, 0), 300.0, 2.0e6)       # breach toe on the edge
    assert r["untraced"] is True and not r["mask"].any()
    r = TR.screen(np.full((40, 50), 100.0), gt, (20, 25), 300.0, 2.0e6)    # flat ground
    assert r["untraced"] is True
    r = TR.screen(dem, gt, (20, 45), 300.0, 2.0e6)      # a normal path
    assert r["untraced"] is False and r["mask"].any()
    src = open(os.path.join(PLUGIN, "algorithms", "triage.py"), encoding="utf-8").read()
    assert 'if env.get("untraced"):' in src and src.index('if env.get("untraced"):') < src.index('result, reason = "clear"')


# ---------------------------------------------------------------------------- 5
@check("engine: NaN DEM cells are walls; bad inputs are clear errors")
def _():
    z = np.full((20, 30), 100.0)
    z[5:8, 5:8] = np.nan
    e = fe.Engine(z, 10.0)
    assert e.wall[5:8, 5:8].all() and np.isfinite(e.z).all()
    e.add_source([(10, 15)], [0, 300, 600], [0, 20, 0])
    r = e.run(300.0)
    assert np.isfinite(r["depth"]).all() and abs(r["mass"]["error_pct"]) < 1e-6
    for bad in (np.full((5, 5), np.nan), np.zeros((0, 3))):
        try:
            fe.Engine(bad, 10.0)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass
    for cell in (0.0, -5.0, float("nan")):
        try:
            fe.Engine(z, cell)
            raise AssertionError("expected ValueError for cell {}".format(cell))
        except ValueError:
            pass


@check("engine: an unstable run stops with FloatingPointError")
def _():
    z = np.zeros((10, 10))
    e = fe.Engine(z, 10.0)
    e.set_reservoir(np.ones(z.shape, bool), 1.0)
    e.qx[4, 4] = np.inf                         # what a blown-up step leaves behind
    try:
        e.run(60.0)
        raise AssertionError("expected FloatingPointError")
    except FloatingPointError as ex:
        assert "unstable" in str(ex)


@check("engine: faster flux code = the 0.11.0 formulas bit for bit")
def _():
    rng = np.random.default_rng(7)
    G, H = fe.G, fe.H_DRY
    for _ in range(20):
        n = 400
        hL, hR = rng.choice([0.0, 1e-5, 0.3, 2.0], n) * rng.uniform(0.5, 1.5, n), \
            rng.choice([0.0, 1e-5, 0.3, 2.0], n) * rng.uniform(0.5, 1.5, n)
        huL, huR, hvL, hvR = (rng.normal(0, 1, n) for _ in range(4))
        # the 0.11.0 HLL, verbatim
        wetL, wetR = hL > H, hR > H
        dL, dR = np.where(wetL, hL, 1.0), np.where(wetR, hR, 1.0)
        uL = np.where(wetL, huL / dL, 0.0); uR = np.where(wetR, huR / dR, 0.0)
        vL = np.where(wetL, hvL / dL, 0.0); vR = np.where(wetR, hvR / dR, 0.0)
        cL, cR = np.sqrt(G * hL), np.sqrt(G * hR)
        sL = np.minimum(uL - cL, uR - cR); sR = np.maximum(uL + cL, uR + cR)
        sL = np.where(~wetL, uR - 2.0 * cR, sL); sR = np.where(~wetR, uL + 2.0 * cL, sR)
        FL0, FR0 = hL * uL, hR * uR
        FL1, FR1 = FL0 * uL + 0.5 * G * hL * hL, FR0 * uR + 0.5 * G * hR * hR
        FL2, FR2 = FL0 * vL, FR0 * vR
        den = sR - sL; den = np.where(np.abs(den) < 1e-12, 1e-12, den)
        pos, neg, sLsR, both_dry = sL >= 0, sR <= 0, sL * sR, ~(wetL | wetR)

        def mix(FLk, FRk, UL, UR):
            mid = (sR * FLk - sL * FRk + sLsR * (UR - UL)) / den
            return np.where(both_dry, 0.0, np.where(pos, FLk, np.where(neg, FRk, mid)))
        ref = (mix(FL0, FR0, hL, hR), mix(FL1, FR1, FL0, FR0), mix(FL2, FR2, hL * vL, hR * vR))
        got = fe._hll(hL, huL, hvL, hR, huR, hvR)
        for a, b in zip(ref, got):
            assert np.array_equal(a, b), np.max(np.abs(a - b))


# ---------------------------------------------------------------------------- 6
@check("breach equations: invalid inputs are ValueErrors in plain words")
def _():
    good = 2.0e6, 8.0
    assert bh.froehlich_2008(*good)["bavg_m"] > 0
    for fn in (bh.froehlich_2008, bh.froehlich_1995, bh.macdonald, bh.von_thun_gillette):
        for vw, h in ((-1.0, 8.0), (2e6, 0.0), (float("nan"), 8.0), (2e6, float("inf")), (None, 8.0)):
            try:
                fn(vw, h)
                raise AssertionError("{}({}, {}) did not raise".format(fn.__name__, vw, h))
            except ValueError as e:
                assert "must be" in str(e)
    try:
        bh.curvilinear_shape(0.0, 3600.0, 1e6)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    assert bh.curvilinear_shape(300.0, 3600.0, 0.0) is None      # zero volume: no shape, as before


# ---------------------------------------------------------------------------- 7
@check("storage: an undeclared -9999 fill is no-data")
def _():
    dem = np.full((30, 30), -9999.0)
    mask = np.zeros(dem.shape, bool); mask[5:10, 5:10] = True
    try:
        ST.estimate_storage(dem, mask, 10.0)
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "does not overlap valid DEM cells" in str(e)


# ---------------------------------------------------------------------------- 8
@check("GDAL: no process-wide exception switch; a damaged file is named")
def _():
    for root, dirs, files in os.walk(PLUGIN):
        dirs[:] = [d for d in dirs if d not in ("tests", "__pycache__")]
        for fn in files:
            if fn.endswith(".py"):
                import ast
                tree = ast.parse(open(os.path.join(root, fn), encoding="utf-8").read())
                calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                         and isinstance(n.func, ast.Attribute) and n.func.attr == "UseExceptions"]
                assert not calls, fn
    from nd_dam_screening.algorithms import _common as cm
    from osgeo import gdal
    real = gdal.Open
    gdal.Open = lambda *a, **k: None
    try:
        cm.gdal_open("/data/dam_7.tif")
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "/data/dam_7.tif" in str(e)
    finally:
        gdal.Open = real


print("test_v0111: {} checks passed (flavour {})".format(len(passed), FLAVOR))
