"""Tests of the 3.4.1 stability fixes: breach equations reject invalid inputs in plain words
(they returned NaN / complex numbers or divided by zero), hazard sampling treats NaN
coordinates and empty footprints as "no sample", result archives and logs are closed at once,
and GDAL's process-wide exception mode is no longer switched on (damsuite.gdalsafe)."""
import ast
import math
import os

from nd_breach_runner.tests.registry import test

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _raises_value_error(fn, *a):
    try:
        fn(*a)
    except ValueError as e:
        assert "must be" in str(e), str(e)
        return
    raise AssertionError("{}{} did not raise ValueError".format(fn.__name__, a))


@test
def v341_breach_equations_validate_inputs():
    from nd_breach_runner.core import breach as br
    b, tf, z = br.froehlich_2008(2.0e6, 8.0)
    assert b > 0 and tf > 0 and math.isfinite(b)
    for fn in (br.froehlich_2008, br.froehlich_1995, br.vtg_1990, br.froehlich_1995_peak):
        for args in ((-1.0, 8.0), (2.0e6, 0.0), (float("nan"), 8.0), (2.0e6, float("inf")), (None, 8.0)):
            _raises_value_error(fn, *args)
    _raises_value_error(br.mlm_1984, 2.0e6, 8.0, 0.0)
    _raises_value_error(br.tr60_peak_cfs, 100.0, -2.0)
    _raises_value_error(br.curvilinear_shape, 0.0, 3600.0, 1e6)
    assert br.curvilinear_shape(300.0, 3600.0, 0.0) is None          # zero volume: no shape


@test
def v341_breach_equations_unchanged_for_valid_inputs():
    """The checks must not change any number: the formulas, evaluated directly."""
    from nd_breach_runner.core import breach as br
    vw, hb = 3.7e6, 11.0
    b, tf, z = br.froehlich_2008(vw, hb, "piping")
    c = br.FROEHLICH_2008["piping"]
    assert b == 0.27 * c["ko"] * vw ** 0.32 * hb ** 0.04
    assert tf == 63.2 * math.sqrt(vw / (br.G * hb ** 2))
    q, rule, brr = br.tr60_peak_cfs(3000.0, 30.0, 9000.0)
    assert q == max(3.2 * 30.0 ** 2.5, min(1100.0 * (3000.0 * 30.0 / 9000.0) ** 1.35, 65.0 * 30.0 ** 1.85))


@test
def v341_hazard_sampling_nan_and_empty():
    import numpy as np
    from nd_breach_runner.core import nd_hazard as nh
    d = np.full((10, 10), 1.0)
    v = np.full((10, 10), 2.0)
    assert nh.sample_point(d, v, 0.0, 0.0, 10.0, float("nan"), -5.0) == (None, None)
    assert nh.sample_point(d, v, 0.0, 0.0, 10.0, 5.0, float("inf")) == (None, None)
    assert nh.sample_point(d, v, 0.0, 0.0, 10.0, 5.0, -5.0) == (1.0, 2.0)
    assert nh.sample_footprint(d, v, 0.0, 0.0, 10.0, [])[:2] == (None, None)
    assert nh.sample_footprint(d, v, 0.0, 0.0, 10.0, [(float("nan"), 1.0)])[:2] == (None, None)
    dd, vv, how = nh.sample_footprint(d, v, 0.0, 0.0, 10.0, [(5, -5), (35, -5), (35, -35), (5, -35)])
    assert dd == 1.0 and vv == 2.0 and "cells inside" in how


@test
def v341_no_process_wide_gdal_exception_switch():
    for root, dirs, files in os.walk(PLUGIN):
        dirs[:] = [d for d in dirs if d not in ("tests", "__pycache__", "scripts")]
        for fn in files:
            if fn.endswith(".py"):
                tree = ast.parse(open(os.path.join(root, fn), encoding="utf-8").read())
                calls = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call)
                         and isinstance(n.func, ast.Attribute) and n.func.attr == "UseExceptions"]
                assert not calls, "{}: {}".format(fn, calls)
    src = open(os.path.join(PLUGIN, "core", "gdal_io.py"), encoding="utf-8").read()
    assert "gdal.Open(" not in src.replace("GS.open_raster", ""), "gdal_io must open through gdalsafe"


@test
def v341_result_archives_and_logs_are_closed():
    for rel in ("core/anuga_model.py", "core/verification.py"):
        src = open(os.path.join(PLUGIN, rel), encoding="utf-8").read()
        assert "with np.load(result) as r:" in src and "r = np.load(result)" not in src, rel
    src = open(os.path.join(PLUGIN, "core", "lisflood.py"), encoding="utf-8").read()
    assert "logtxt = open(" not in src


@test
def v341_version():
    import configparser
    from nd_breach_runner.core import workflow
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(os.path.join(PLUGIN, "metadata.txt"), encoding="utf-8")
    assert cp.get("general", "version") == workflow.VERSION == workflow.metadata_version()


@test
def v341_init_processing_registers_one_provider():
    """initProcessing (QGIS / qgis_process) then initGui (which calls it again): one provider,
    and unload removes exactly that one."""
    import types
    from nd_breach_runner.tests.test_popup import qt_fakes
    with qt_fakes() as q:
        class Iface(object):
            def __init__(self): self.menu = []
            def mainWindow(self): return None
            def addPluginToMenu(self, name, action): self.menu.append((name, action))
            def removePluginMenu(self, name, action): pass
            def messageBar(self): return types.SimpleNamespace(pushMessage=lambda *a, **k: None)
        reg = q.core.QgsApplication.processingRegistry()
        before = list(reg.providers)
        p = q.plugin.NDBreachRunnerPlugin(Iface())
        p.initProcessing()
        first = p.provider
        p.initGui()
        p.initProcessing()
        assert p.provider is first
        assert [x for x in reg.providers if x not in before] == [first]
        p.unload()
        assert first not in reg.providers and p.provider is None


@test
def v343_shifted_crs_of_the_selftest_really_shifts():
    """The self-test's shifted CRS must not be "+proj=utm ... +x_0=...": PROJ's utm resets the
    false easting to 500000 m, so the warp check read a window 100 cells off its data in the
    first real-QGIS run (3.4.3). Transverse Mercator with UTM 14N's parameters honours x_0."""
    from nd_breach_runner.core import selftest as st
    s = st.SHIFTED_SRS
    assert "+proj=tmerc" in s and "+proj=utm" not in s
    for k in ("+lat_0=0", "+lon_0=-99", "+k=0.9996", "+x_0=501000", "+y_0=0", "+datum=NAD83"):
        assert k in s, k
    assert st.SHIFT_M == 1000.0


@test
def v344_outline_without_contourpy():
    """3.4.4: QGIS 3.28's Python has no contourpy (real-QGIS run on macOS). The ANUGA mesh outline
    falls back to the built-in cell-edge tracer, which gives a simple counter-clockwise ring of
    the largest region, holes filled, corner-only contacts closed."""
    import numpy as np
    from nd_breach_runner.core import terrain as T
    from nd_breach_runner.core.grid import Grid
    g = Grid(np.zeros((20, 20)), 0.0, 200.0, 10.0)
    m = np.zeros((20, 20), bool)
    m[3:15, 3:6] = True; m[3:6, 3:15] = True; m[12:15, 3:12] = True; m[11, 12] = True   # C, pinch
    m[8, 8] = True                                                                        # islet
    m[17:19, 17:19] = True                                                                # small 2nd region
    r = T.mask_outline(g, m, 0.1, backend="cells")
    assert T._area(r) > 0                                         # counter-clockwise
    assert len({tuple(p) for p in r}) == len(r)                   # no repeated vertex
    area = T._area(r)
    assert area == 83 * 100.0, area           # the C: 82 cells + 1 closing the pinch; not the islet or 2nd region
    blk = np.zeros((7, 7), bool); blk[2:5, 2:5] = True
    rb = T.mask_outline(Grid(np.zeros((7, 7)), 0.0, 7.0, 1.0), blk, 0.05, backend="cells")
    assert abs(T._area(rb) - 9.0) < 1e-9 and len(rb) == 4
    try:
        T.mask_outline(g, np.zeros((20, 20), bool), 1.0, backend="cells")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    real = T._have_contourpy
    T._have_contourpy = lambda: False
    try:
        auto = T.mask_outline(g, m, 0.1)
        assert np.array_equal(auto, r)
        from nd_breach_runner.core import selftest as st
        assert "built-in outline tracer" in st.check_contourpy()
    finally:
        T._have_contourpy = real
