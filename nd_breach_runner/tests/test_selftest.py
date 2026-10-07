"""Tests of core/selftest.py, algorithms_selftest.py and the reporting around them.

GDAL is not available where the plugin is developed, so the GDAL-side checks are exercised
against `FakeGdalIo`, a numpy stand-in for core.gdal_io with the same behaviour on the
synthetic rasters. That tests the checks' own logic (indices, tolerances, keys of the returned
dicts, what they do when a stand-in misbehaves) but not GDAL itself: only a real run on a
machine with GDAL (Processing > "Self-test the installation in this QGIS") does that."""
import contextlib
import importlib
import io
import json
import os
import re
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core import nd_data
from nd_breach_runner.core import selftest as st
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.core.units import FT, US_FT
from nd_breach_runner.tests.registry import test, model_test


# ------------------------------------------------------------------------------ the stand-in
class FakeGdalIo(object):
    """numpy replacement for core.gdal_io. Rasters are .npz files holding the array, the
    georeference, the CRS text and the band's unit."""
    Z_AUTO, Z_FEET, Z_US_FEET, Z_METERS = "auto", "feet", "us_feet", "meters"
    NODATA = -9999.0

    def __init__(self, half_cell_bug=False, fill_bug=False):
        self.half_cell_bug = half_cell_bug          # GdalTerrain.read shifted by half a cell
        self.fill_bug = fill_bug                    # fill_gaps miscounts

    # -- files ----------------------------------------------------------------------
    @staticmethod
    def _save(path, data, x0, y0, dx, crs, unit=""):
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        meta = json.dumps({"x0": x0, "y0": y0, "dx": dx, "crs": crs, "unit": unit})
        with open(path, "wb") as f:
            np.savez(f, data=np.asarray(data, dtype=np.float32), meta=np.array(meta))

    @staticmethod
    def _load(path):
        if path.lower().endswith(".vrt"):
            with open(path) as f:
                v = json.load(f)
            parts = [FakeGdalIo._load(t) for t in v["tiles"]]
            dx = parts[0][0].dx
            x0 = min(g.x0 for g, _u in parts)
            y0 = max(g.y0 for g, _u in parts)
            x1 = max(g.x0 + g.nx * dx for g, _u in parts)
            y1 = min(g.y0 - g.ny * dx for g, _u in parts)
            nx, ny = int(round((x1 - x0) / dx)), int(round((y0 - y1) / dx))
            out = np.full((ny, nx), np.nan)
            for g, _u in parts:
                r = int(round((y0 - g.y0) / dx))
                c = int(round((g.x0 - x0) / dx))
                out[r:r + g.ny, c:c + g.nx] = g.data
            return Grid(out, x0, y0, dx, crs_wkt=parts[0][0].crs_wkt), parts[0][1]
        z = np.load(path, allow_pickle=False)
        m = json.loads(str(z["meta"]))
        data = z["data"].astype(np.float64)
        data[data == FakeGdalIo.NODATA] = np.nan
        return Grid(data, m["x0"], m["y0"], m["dx"], crs_wkt=m["crs"]), m["unit"]

    def set_unit(self, path, unit):
        g, _u = self._load(path)
        self._save(path, g.data, g.x0, g.y0, g.dx, g.crs_wkt, unit)

    def _meta_unit(self, path):
        return self._load(path)[1]

    # -- crs helpers ----------------------------------------------------------------
    @staticmethod
    def utm_epsg(lon, lat):
        zone = int((lon + 180.0) // 6.0) + 1
        return 26900 + zone

    @staticmethod
    def epsg_of(text):
        m = re.match(r"^\s*EPSG:(\d+)\s*$", str(text or ""), re.I)
        return "EPSG:" + m.group(1) if m else None

    @staticmethod
    def linear_unit_m(text):
        m = re.match(r"^\s*EPSG:(\d+)\s*$", str(text or ""), re.I)
        if m and (26900 <= int(m.group(1)) <= 26999 or 32600 <= int(m.group(1)) <= 32799):
            return 1.0
        return None

    @staticmethod
    def same_crs(a, b):
        return str(a).strip().upper() == str(b).strip().upper()

    def z_to_m_factor(self, path, choice="auto"):
        if choice == self.Z_FEET:
            return FT, "feet (chosen)"
        if choice == self.Z_US_FEET:
            return US_FT, "US survey feet (chosen)"
        if choice == self.Z_METERS:
            return 1.0, "metres (chosen)"
        unit = self._meta_unit(path).strip().lower()
        if unit in ("us-ft", "us_ft", "us survey foot"):
            return US_FT, "US survey feet (raster unit '{}')".format(unit)
        if unit in ("ft", "foot", "feet"):
            return FT, "feet (raster unit '{}')".format(unit)
        if unit in ("m", "meter", "metre", "meters", "metres"):
            return 1.0, "metres (raster unit '{}')".format(unit)
        return 1.0, "metres (no units recorded in the raster; check)"

    # -- reading and writing --------------------------------------------------------
    def read_grid(self, path):
        g, _u = self._load(path)
        return g

    def GeoTiffWriter(self, work_srs, target_srs=None, target_res=None):
        fake = self

        class Writer(object):
            def __init__(self):
                self.work_srs, self.target_srs, self.target_res = work_srs, target_srs, target_res

            def __call__(self, grid, path):
                x0, crs = grid.x0, work_srs
                if target_srs:
                    if work_srs == st.SHIFTED_SRS and target_srs == st.CRS:
                        x0 -= st.SHIFT_M
                    crs = target_srs
                fake._save(path, np.where(np.isfinite(grid.data), grid.data, fake.NODATA),
                           x0, grid.y0, grid.dx, crs)
                return path
        return Writer()

    def GdalTerrain(self, path, work_srs, z_choice="auto", manning_path=None, default_n=0.06,
                    resample="bilinear", manning_lut=None):
        fake = self

        class Terrain(object):
            src_nodata = fake.NODATA

            def __init__(self):
                self.dem_path, self.work_srs = path, work_srs
                self.z_to_m, self.z_note = fake.z_to_m_factor(path, z_choice)
                self.manning_path, self.default_n, self.manning_lut = (manning_path, default_n,
                                                                       manning_lut)

            def _sample(self, p, bounds, cell, nearest):
                src, _u = fake._load(p)
                xmin, ymin, xmax, ymax = bounds
                nx, ny = int(round((xmax - xmin) / cell)), int(round((ymax - ymin) / cell))
                xs = xmin + (np.arange(nx) + 0.5) * cell
                ys = ymax - (np.arange(ny) + 0.5) * cell
                if fake.half_cell_bug:
                    xs = xs + 0.5 * cell
                if work_srs == st.SHIFTED_SRS and src.crs_wkt == st.CRS:
                    xs = xs - st.SHIFT_M
                X, Y = np.meshgrid(xs, ys)
                if nearest:
                    c = np.floor((X - src.x0) / src.dx).astype(int)
                    r = np.floor((src.y0 - Y) / src.dx).astype(int)
                    ok = (c >= 0) & (c < src.nx) & (r >= 0) & (r < src.ny)
                    v = np.where(ok, src.data[np.clip(r, 0, src.ny - 1),
                                              np.clip(c, 0, src.nx - 1)], np.nan)
                else:
                    v = src.sample_bilinear(X, Y)
                return Grid(v, xmin, ymax, cell, crs_wkt=work_srs)

            def read(self, bounds, cell):
                g = self._sample(self.dem_path, bounds, cell, False)
                g.data = g.data * self.z_to_m
                return g

            def manning(self, bounds, cell):
                if not self.manning_path:
                    return None
                g = self._sample(self.manning_path, bounds, cell, True)
                if self.manning_lut:
                    lut = dict(self.manning_lut)
                    lut["default"] = self.default_n
                    g.data = nd_data.nlcd_to_manning(g.data, lut)
                    return g
                bad = ~np.isfinite(g.data) | (g.data <= 0) | (g.data > 1.0)
                g.data[bad] = self.default_n
                return g
        return Terrain()

    # -- tools ----------------------------------------------------------------------
    def build_vrt(self, sources, out_path, nodata=None, resolution="highest"):
        files = sorted(os.path.join(sources, f) for f in os.listdir(sources)
                       if f.lower().endswith(".tif"))
        recs = []
        for f in files:
            g, unit = self._load(f)
            recs.append({"file": f, "res": g.dx, "crs": g.crs_wkt, "bounds": g.bounds,
                         "unit": unit})
        count = {}
        for r in recs:
            count[r["crs"]] = count.get(r["crs"], 0) + 1
        main = max(count, key=lambda k: count[k])
        use = [r for r in recs if r["crs"] == main]
        with open(out_path, "w") as f:
            json.dump({"tiles": [r["file"] for r in use]}, f)
        b = [r["bounds"] for r in use]
        return {"vrt": out_path, "tiles": len(use), "crs": main,
                "cell_sizes": sorted({round(r["res"], 4) for r in use}),
                "elevation_units": [], "bounds": (min(x[0] for x in b), min(x[1] for x in b),
                                                  max(x[2] for x in b), max(x[3] for x in b)),
                "left_out": [r["file"] for r in recs if r["crs"] != main], "problems": [],
                "notes": []}

    def fill_gaps(self, src_path, out_path, extent=None, max_distance_px=100, smoothing=0):
        g, unit = self._load(src_path)
        a, x0, y0 = g.data.copy(), g.x0, g.y0
        if extent:
            c0 = int(round((extent[0] - x0) / g.dx))
            c1 = int(round((extent[2] - x0) / g.dx))
            r0 = int(round((y0 - extent[3]) / g.dx))
            r1 = int(round((y0 - extent[1]) / g.dx))
            a = a[r0:r1, c0:c1]
            x0, y0 = x0 + c0 * g.dx, y0 - r0 * g.dx
        before = int(np.isnan(a).sum())
        for _ in range(max_distance_px):
            hole = np.isnan(a)
            if not hole.any():
                break
            p = np.pad(a, 1, constant_values=np.nan)
            nb = np.stack([p[:-2, 1:-1], p[2:, 1:-1], p[1:-1, :-2], p[1:-1, 2:]])
            n = (~np.isnan(nb)).sum(axis=0)
            mean = np.where(n > 0, np.nansum(nb, axis=0) / np.maximum(n, 1), np.nan)
            a = np.where(hole & (n > 0), mean, a)
        left = int(np.isnan(a).sum())
        self._save(out_path, np.where(np.isnan(a), self.NODATA, a), x0, y0, g.dx, g.crs_wkt,
                   unit)
        filled = before - left
        if self.fill_bug:
            filled += 1
        return {"out": out_path, "cells_filled": filled, "cells_left": left}

    def nlcd_to_manning_raster(self, nlcd_path, out_path, lut=None, like_path=None):
        g, _u = self._load(nlcd_path)
        if like_path:
            like, _l = self._load(like_path)
            assert (like.nx, like.ny, like.x0, like.y0) == (g.nx, g.ny, g.x0, g.y0), \
                "the fake only supports a like raster on the same grid"
        n = nd_data.nlcd_to_manning(g.data, lut or nd_data.manning_lut())
        self._save(out_path, n, g.x0, g.y0, g.dx, g.crs_wkt)
        seen = sorted({int(c) for c in np.unique(g.data[np.isfinite(g.data)])})
        unknown = [c for c in seen if c not in (lut or nd_data.NLCD_N)]
        return {"out": out_path, "classes": seen, "unknown_classes": unknown}


@contextlib.contextmanager
def fake_gdal(**bugs):
    """selftest runs on FakeGdalIo; the two checks that call GDAL/OGR directly are replaced."""
    fake = FakeGdalIo(**bugs)
    keep = (st.gdal_io, st.check_gdal, st.check_ogr_geopackage, st._set_unit_type)
    st.gdal_io = fake
    st.check_gdal = lambda: "GDAL fake 0.0"

    def no_ogr(workdir):
        raise st.Skip("OGR is not available in this test")
    st.check_ogr_geopackage = no_ogr
    st._set_unit_type = fake.set_unit
    try:
        yield fake
    finally:
        st.gdal_io, st.check_gdal, st.check_ogr_geopackage, st._set_unit_type = keep


@contextlib.contextmanager
def no_models():
    """Nothing found by auto-detection, whatever the developer's machine has."""
    keep = nd_data.detect_lisflood, nd_data.detect_anuga_python
    nd_data.detect_lisflood = lambda *a, **k: ""
    nd_data.detect_anuga_python = lambda *a, **k: ""
    try:
        yield
    finally:
        nd_data.detect_lisflood, nd_data.detect_anuga_python = keep


class Tmp(object):
    def __enter__(self):
        self.d = tempfile.mkdtemp()
        return self.d

    def __exit__(self, *a):
        shutil.rmtree(self.d, ignore_errors=True)


def by_name(rep):
    return dict((r.name, r) for r in rep.results)


# ------------------------------------------------------------------------------ the report
@test
def selftest_report_records_pass_fail_skip():
    seen = []
    rep = st.Report(on_result=seen.append)
    rep.info = [("Plugin version", "1.2")]

    def bad():
        raise ValueError("depth is 3 not 4")

    def missing():
        raise st.Skip("no exe")

    def boom_cb(r):
        raise RuntimeError("logging failed")
    assert rep.run("a", lambda: "fine").status == st.PASS
    assert rep.run("b", bad).status == st.FAIL
    assert rep.run("c", missing).status == st.SKIP
    assert rep.run("d", lambda: None).detail == ""
    assert rep.run("e", lambda x, y=2: x + y if False else "args {} {}".format(x, y), 1,
                   y=5).detail == "args 1 5"
    b = by_name(rep)["b"]
    assert b.detail == "ValueError: depth is 3 not 4"          # the exact exception text
    assert "ValueError" in b.trace and "test_selftest.py" in b.where, b.where
    assert [r.name for r in seen] == ["a", "b", "c", "d", "e"]
    assert rep.counts() == {st.PASS: 3, st.FAIL: 1, st.SKIP: 1}
    assert not rep.ok() and rep.counts_text() == "3 PASS, 1 FAIL, 1 SKIP"
    assert rep.status_of("c") == st.SKIP and rep.status_of("zz") is None
    rep.on_result = boom_cb                                     # a broken logger cannot break a run
    assert rep.run("f", lambda: "x").status == st.PASS
    stop = st.Report(cancelled=lambda: True)
    r = stop.run("g", lambda: 1 / 0)
    assert r.status == st.SKIP and r.detail == "cancelled"


@test
def selftest_report_text_and_developer_block():
    home = "/Users/jane.doe"
    rep = st.Report()
    rep.info = [("Python", "3.9.5 at " + home + "/miniforge3/bin/python")]
    rep.add("Python", st.PASS, "Python 3.9.5")
    rep.add("GDAL: warp", st.FAIL, "AssertionError: warp differs by 0.5 m\nsecond line",
            0.3, "Traceback (most recent call last):\n  File \"" + home + "/x.py\", line 3\n"
            "AssertionError: warp differs by 0.5 m", "nd_breach_runner/core/selftest.py:5 in f")
    rep.add("ANUGA", st.SKIP, "no ANUGA Python interpreter found")
    text = rep.text(home)
    assert "jane.doe" not in text and "~/miniforge3" in text
    assert "[FAIL] GDAL: warp  (0.3 s)" in text and "-- traceback --" in text
    assert "Result: 1 PASS, 1 FAIL, 1 SKIP" in text
    assert "-- traceback --" not in rep.text(home, with_trace=False)
    blk = rep.developer_block(home)
    lines = blk.splitlines()
    assert lines[0] == "===== COPY FROM HERE =====" and lines[-1] == "===== END ====="
    assert "jane.doe" not in blk
    assert "[FAIL] GDAL: warp" in blk and "       AssertionError: warp differs by 0.5 m" in blk
    assert "       at nd_breach_runner/core/selftest.py:5 in f" in blk
    assert "[PASS] Python - Python 3.9.5" in blk and "[SKIP] ANUGA - no ANUGA" in blk
    assert "Traceback" not in blk                               # compact: no full traces
    long = st.Report()
    long.add("x", st.FAIL, "E: " + "y" * 900)
    assert len(long.developer_block(max_detail=100)) < 400
    assert st.mask_home("/root/a/b", "/root") == "~/a/b" and st.mask_home("/x", "/") == "/x"
    assert st.short_path("/a/b/nd_breach_runner/core/selftest.py") == \
        "nd_breach_runner/core/selftest.py"


@test
def selftest_environment_checks():
    assert st.check_qgis_version(32812, "3.28.12-Firenze").startswith("QGIS 3.28.12")
    assert "3.28" in st.check_qgis_version(32800, "3.28.0")
    for bad in (32799, 31600):
        try:
            st.check_qgis_version(bad, "3.16")
            raise AssertionError("expected RuntimeError")
        except RuntimeError as e:
            assert "3.28" in str(e) and str(bad) in str(e), str(e)
    try:
        st.check_qgis_version(None)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "QGIS version" in str(e)
    assert st.check_qgis_version(40000, "4.0.0")
    assert "Python" in st.check_python() and "numpy" in st.check_numpy()
    info = dict(st.environment_info())
    assert "Plugin version" in info and "numpy" in info and "GDAL" in info
    assert st.plugin_version() != "?"
    assert st.plugin_version("/nonexistent") == "?"
    v, err = st.module_version("numpy")
    assert v and not err
    v, err = st.module_version("no_such_module_xyz")
    assert not v and "ModuleNotFoundError" in err or "ImportError" in err


# ------------------------------------------------------------------------------ orchestration
@test
def selftest_without_gdal_fails_it_and_skips_the_rest():
    keep = st.check_gdal, st.gdal_io
    st.check_gdal = lambda: (_ for _ in ()).throw(ImportError("No module named 'osgeo'"))
    try:
        with Tmp() as d, no_models():
            rep = st.Report()
            st.run_all(rep, d, "", "", True)
    finally:
        st.check_gdal, st.gdal_io = keep
    b = by_name(rep)
    assert b["GDAL (osgeo) importable"].status == st.FAIL
    assert b["GDAL (osgeo) importable"].detail == "ImportError: No module named 'osgeo'"
    assert b["OGR GeoPackage writer"].status == st.SKIP
    for name, _fn in st.GDAL_CHECKS:
        assert b[name].status == st.SKIP and "GDAL is not importable" in b[name].detail, name
    assert b["LISFLOOD-FP executable"].status == st.SKIP
    assert b["LISFLOOD-FP calibrate() on a small flood"].status == st.SKIP
    assert b["ANUGA environment"].status == st.SKIP
    assert b["Small model: LISFLOOD-FP end to end"].status == st.SKIP
    assert b["Small model: ANUGA tier"].status == st.SKIP
    assert b["Python"].status == st.PASS and b["numpy"].status == st.PASS
    assert rep.counts()[st.FAIL] == 1 and not rep.ok()


@test
def selftest_gdal_checks_pass_on_the_numpy_stand_in():
    with Tmp() as d, no_models(), fake_gdal():
        rep = st.Report()
        st.run_all(rep, d, "", "", False)
    b = by_name(rep)
    for name, _fn in st.GDAL_CHECKS:
        assert b[name].status == st.PASS, "{}: {}\n{}".format(name, b[name].detail,
                                                              b[name].trace)
    assert b["GDAL (osgeo) importable"].status == st.PASS
    assert b["OGR GeoPackage writer"].status == st.SKIP
    assert "elevations within" in b["GDAL: write and read back a synthetic valley DEM"].detail
    assert "hole of 30 cells" in b["GDAL: fill no-data gaps"].detail
    assert "VRT of 2 tiles" in b["GDAL: VRT mosaic of two tiles"].detail
    assert b["LISFLOOD-FP executable"].status == st.SKIP
    assert rep.ok()


@test
def selftest_checks_fail_with_numbers_when_the_stand_in_misbehaves():
    with Tmp() as d, no_models(), fake_gdal(half_cell_bug=True):
        rep = st.Report()
        st.run_all(rep, d, "", "", False)
    b = by_name(rep)
    r = b["GDAL: GdalTerrain.read (metres and feet)"]
    assert r.status == st.FAIL and "half-cell shift" in r.detail and "AssertionError" in r.detail
    assert r.where.startswith("nd_breach_runner/core/selftest.py"), r.where
    assert b["GDAL: warp, resample, re-project on write"].status == st.FAIL
    assert b["GDAL: VRT mosaic of two tiles"].status == st.FAIL
    with Tmp() as d, no_models(), fake_gdal(fill_bug=True):
        rep = st.Report()
        st.run_all(rep, d, "", "", False)
    b = by_name(rep)
    f = b["GDAL: fill no-data gaps"]
    assert f.status == st.FAIL and "31 filled" in f.detail and "expected 30" in f.detail, f.detail
    assert b["GDAL: VRT mosaic of two tiles"].status == st.PASS       # independent checks go on
    # a dead DEM makes every check that needs it SKIP with the reason, not crash
    with Tmp() as d, no_models(), fake_gdal() as fake:
        def broken(grid_path):
            raise IOError("disk full")
        fake.read_grid = broken
        rep = st.Report()
        st.run_all(rep, d, "", "", False)
    b = by_name(rep)
    assert b["GDAL: write and read back a synthetic valley DEM"].status == st.FAIL
    assert "disk full" in b["GDAL: write and read back a synthetic valley DEM"].detail
    assert b["GDAL: GdalTerrain.read (metres and feet)"].status == st.SKIP
    assert b["GDAL: CRS helpers"].status == st.SKIP


@test
def selftest_synthetic_data_is_what_the_checks_assume():
    g = st.synthetic_valley_grid()
    assert (g.nx, g.ny, g.dx) == (st.NX, st.NY, st.DX) and np.isfinite(g.data).all()
    assert g.bounds == (st.X0, st.Y0 - st.NY * st.DX, st.X0 + st.NX * st.DX, st.Y0)
    # the valley axis is low, the walls high, the reservoir flat at 505 m
    mid = st.NY // 2
    assert g.data[mid, 10] < g.data[2, 10] - 5.0
    assert np.allclose(g.data[mid, 10:40], 505.0)
    p = st.plane_grid()
    assert abs(p.data[0, 0] - (100.0 + 0.01 * 5.0 + 0.02 * (-5.0))) < 1e-9
    codes, blocks = st.class_codes()
    assert set(np.unique(codes[np.isfinite(codes)])) == {11.0, 23.0, 41.0, 82.0, 99.0}
    assert np.isnan(codes[st.NY - 1, 0]) and codes[0, 0] == 82.0
    for k, (rs, cs) in blocks.items():
        sub = codes[rs, cs]
        assert (np.isnan(sub).all() if k == "nodata" else (sub == k).all()), k
    assert st.max_abs_diff(np.zeros((5, 5)), np.ones((5, 5))) == 1.0
    a, b = np.zeros((5, 5)), np.zeros((5, 5))
    a[2, 2] = np.nan
    try:
        st.max_abs_diff(a, b)
        raise AssertionError("expected AssertionError")
    except AssertionError as e:
        assert "no-data cells differ" in str(e)
    # the model valley matches the one the model tests use
    from nd_breach_runner.tests.synthetic import valley
    x, y = np.meshgrid(np.linspace(-2000, 2000, 9), np.linspace(-300, 300, 7))
    assert np.allclose(st.model_valley(x, y), valley(x, y))


@test
def selftest_model_checks_skip_or_fail_clearly():
    with Tmp() as d:
        try:
            st.check_lisflood_exe("", d)
            raise AssertionError("expected Skip")
        except st.Skip as e:
            assert "install_macos.sh" in str(e)
        for fn, args in ((st.check_lisflood_calibrate, ("", d)), (st.check_anuga_env, ("", d))):
            try:
                fn(*args)
                raise AssertionError("expected Skip")
            except st.Skip:
                pass
        try:
            st.check_lisflood_exe(os.path.join(d, "nope"), d)
            raise AssertionError("expected RuntimeError")
        except RuntimeError as e:
            assert "does not exist" in str(e)
        plain = os.path.join(d, "plain")
        with open(plain, "w") as f:
            f.write("x")
        try:
            st.check_lisflood_exe(plain, d)
            raise AssertionError("expected RuntimeError")
        except RuntimeError as e:
            assert "not executable" in str(e)
        sh = os.path.join(d, "fake_lisflood")
        with open(sh, "w") as f:
            f.write("#!/bin/sh\necho nothing useful\nexit 3\n")
        os.chmod(sh, 0o755)
        try:
            st.check_lisflood_exe(sh, d)
            raise AssertionError("expected RuntimeError")
        except RuntimeError as e:
            assert sh in str(e)


# ------------------------------------------------------------------------------ registry checks
class _P(object):
    def __init__(self, name, ptype="number", desc="", optional=False, dest=False, default=None):
        self._n, self._t, self._d = name, ptype, desc
        self.optional, self._dest, self._default = optional, dest, default

    def name(self):
        return self._n

    def type(self):
        return self._t

    def description(self):
        return self._d or self._n

    def flags(self):
        return 0

    def defaultValue(self):
        return self._default

    def isDestination(self):
        return self._dest


class _Alg(object):
    def __init__(self, aid, params):
        self._id, self._params = aid, params

    def id(self):
        return self._id

    def parameterDefinitions(self):
        return list(self._params)


class _Prov(object):
    def __init__(self, algs):
        self._algs = algs

    def algorithms(self):
        return self._algs


class _Registry(object):
    def __init__(self, algs=(), providers=None):
        self.algs = dict((a.id(), a) for a in algs)
        self.providers = providers or {}

    def algorithmById(self, i):
        return self.algs.get(i)

    def providerById(self, i):
        return self.providers.get(i)

    def algorithms(self):
        return list(self.algs.values())


def _make_classes():
    class Good(object):
        def name(self):
            return "good"

        def displayName(self):
            return "Good"

        def initAlgorithm(self, config=None):
            self.p = [_P("A"), _P("B")]

        def parameterDefinitions(self):
            return self.p

        def createInstance(self):
            return type(self)()

    class Good2(Good):
        def name(self):
            return "good2"

    class Dup(Good):
        def name(self):
            return "dup"

        def initAlgorithm(self, config=None):
            self.p = [_P("A"), _P("A")]

    class Broken(Good):
        def name(self):
            return "broken"

        def initAlgorithm(self, config=None):
            raise KeyError("no such thing")
    return Good, Good2, Dup, Broken


def _registered(*classes):
    out = []
    for c in classes:
        a = c()
        a.initAlgorithm()
        out.append(_Alg("ndbreach:" + a.name(), a.parameterDefinitions()))
    return out


@test
def selftest_check_algorithms_reports_every_problem():
    Good, Good2, Dup, Broken = _make_classes()
    ok = st.check_algorithms(_Registry(_registered(Good, Good2)), [Good, Good2])
    assert "2 algorithms registered as ndbreach:*" in ok and "4 parameters" in ok
    # one is not registered
    try:
        st.check_algorithms(_Registry(_registered(Good)), [Good, Good2])
        raise AssertionError("expected AssertionError")
    except AssertionError as e:
        assert "good2" in str(e) and "not registered as ndbreach:good2" in str(e), str(e)
        assert "1 problem(s)" in str(e)
    # duplicate parameter names, an initAlgorithm that raises, and a registered copy that differs
    reg = _registered(Good, Dup)
    reg.append(_Alg("ndbreach:good", [_P("A")]))
    try:
        st.check_algorithms(_Registry(reg), [Good, Dup, Broken])
        raise AssertionError("expected AssertionError")
    except AssertionError as e:
        msg = str(e)
        assert "duplicate or empty parameter names ['A']" in msg, msg
        assert "broken (Broken): KeyError: 'no such thing'" in msg, msg
        assert "registered algorithm's parameters differ" in msg, msg
        assert "3 problem(s)" in msg or "4 problem(s)" in msg, msg


@test
def selftest_ndhazard_listing_and_handoff_dry_run():
    with Tmp() as d:
        empty = _Registry()
        try:
            st.describe_ndhazard(empty)
            raise AssertionError("expected Skip")
        except st.Skip as e:
            assert "ndhazard" in str(e)
        try:
            st.check_handoff_dry_run(empty, d)
            raise AssertionError("expected Skip")
        except st.Skip as e:
            assert "no ndhazard" in str(e)
        cls = _Alg("ndhazard:classify", [
            _P("DEPTH", "raster", "Max depth raster"), _P("VELOCITY", "raster", "Velocity raster"),
            _P("DAM_ID", "string", "Dam ID"), _P("OUTPUT", "vectorDestination", "Result", dest=True),
            _P("SCALE", "number", "Scale", default=1.0), _P("NOTE", "string", "Note", optional=True)])
        reg = _Registry([cls], {"ndhazard": _Prov([cls])})
        text = st.describe_ndhazard(reg)
        assert "provider 'ndhazard' with 1 algorithm(s)" in text
        assert "ndhazard:classify: DEPTH, VELOCITY, DAM_ID, OUTPUT, SCALE, NOTE" in text
        out = st.check_handoff_dry_run(reg, d)
        assert "ndhazard:classify: 4 of its parameters filled automatically" in out, out
        assert "DEPTH" in out and "OUTPUT" in out
        # a parameter it cannot fill: a SKIP telling the user to give a mapping
        odd = _Alg("ndhazard:classify", [_P("DEPTH", "raster", "Max depth raster"),
                                         _P("HAZARD_TABLE", "file", "Table of classes")])
        reg2 = _Registry([odd], {"ndhazard": _Prov([odd])})
        try:
            st.check_handoff_dry_run(reg2, d)
            raise AssertionError("expected Skip")
        except st.Skip as e:
            assert "mapping JSON" in str(e) and "HAZARD_TABLE" in str(e), str(e)
        # the provider is there but has no algorithms: that is a failure, not a skip
        try:
            st.describe_ndhazard(_Registry(providers={"ndhazard": _Prov([])}))
            raise AssertionError("expected AssertionError")
        except AssertionError as e:
            assert "no algorithms" in str(e)


# ------------------------------------------------------------------------------ the algorithm
def _algorithm_module():
    from nd_breach_runner.tests import fake_qgis
    fake_qgis.install()
    import nd_breach_runner.algorithms as algs
    importlib.reload(algs)
    import nd_breach_runner.algorithms_tools as tools
    importlib.reload(tools)
    import nd_breach_runner.algorithms_selftest as mod
    importlib.reload(mod)
    return algs, mod, fake_qgis


@test
def selftest_algorithm_definition():
    algs, mod, fq = _algorithm_module()
    assert mod.SELFTEST_ALGORITHMS == (mod.SelfTestAlgorithm,)
    a = mod.SelfTestAlgorithm()
    assert a.name() == "selftest"
    assert a.displayName() == "Self-test the installation in this QGIS"
    assert isinstance(a, tools_base(algs))
    a.initAlgorithm()
    names = [p.name() for p in a.parameterDefinitions()]
    assert names == ["RUN_MODEL", "LISFLOOD_EXE", "ANUGA_PYTHON", "THREADS", "KEEP", "OUTPUT"], names
    assert a.parameterAsBool({}, "RUN_MODEL", None) is True       # 'Run a small model' is on by default
    assert a.parameterAsBool({}, "KEEP", None) is False
    assert a.createInstance().__class__ is mod.SelfTestAlgorithm
    assert "REPORT" in a.outputs and "RESULT" in a.outputs
    assert "PASS" in a.shortHelpString() and "developer" in a.shortHelpString()
    assert a.flags() == 0                                      # threaded: the run does not block QGIS


def tools_base(algs):
    import nd_breach_runner.algorithms_tools as tools
    return tools._ToolBase


class _Reg(_Registry):
    """A registry holding every algorithm class of the plugin, as the real provider would."""

    def __init__(self, classes, hazard=False):
        algs = []
        for c in classes:
            a = c()
            a.initAlgorithm()
            algs.append(_Alg("ndbreach:" + a.name(), a.parameterDefinitions()))
        provs = {}
        if hazard:
            h = _Alg("ndhazard:classify", [_P("DEPTH", "raster", "Depth raster"),
                                           _P("OUT", "vectorDestination", "Out", dest=True)])
            algs.append(h)
            provs["ndhazard"] = _Prov([h])
        _Registry.__init__(self, algs, provs)


def _run_algorithm(mod, algs, fq, d, reg, params=None, version=(32812, "3.28.12-Firenze")):
    keep = mod._registry, mod.qgis_version
    mod._registry = lambda: reg
    mod.qgis_version = lambda: version
    try:
        a = mod.SelfTestAlgorithm()
        a.initAlgorithm()
        fb = fq.QgsProcessingFeedback()
        p = {"RUN_MODEL": False, "OUTPUT": os.path.join(d, "reports", "selftest.txt"),
             "LISFLOOD_EXE": "", "ANUGA_PYTHON": "", "THREADS": 0}
        p.update(params or {})
        out = a.processAlgorithm(p, fq.QgsProcessingContext(), fb)
        return out, fb
    finally:
        mod._registry, mod.qgis_version = keep


@test
def selftest_algorithm_runs_and_writes_the_report():
    algs, mod, fq = _algorithm_module()
    with Tmp() as d, no_models(), fake_gdal():
        classes = mod.algorithm_classes()
        assert any(c.__name__ == "SelfTestAlgorithm" for c in classes) and len(classes) > 10
        out, fb = _run_algorithm(mod, algs, fq, d, _Reg(classes, hazard=True))
        assert fb.errors == [] or all("LISFLOOD" not in e for e in fb.errors), fb.errors
        path = out["REPORT"]
        assert path == os.path.join(d, "reports", "selftest.txt") and os.path.exists(path)
        text = open(path, encoding="utf-8").read()
        assert "ND Dam Breach Runner self-test" in text and "===== COPY FROM HERE =====" in text
        assert "[PASS] QGIS version - QGIS 3.28.12-Firenze (32812)" in text
        assert "[PASS] Plugin algorithms registered and buildable" in text
        assert "ndhazard:classify: DEPTH, OUT" in text
        assert "[PASS] ndhazard hand-off (dry run)" in text or \
            "[SKIP] ndhazard hand-off (dry run)" in text
        assert "[SKIP] LISFLOOD-FP executable" in text
        assert re.match(r"^\d+ PASS, 0 FAIL, \d+ SKIP$", out["RESULT"]), out["RESULT"]
        live = [m for m in fb.info if m.startswith("[PASS] Python")]
        assert live, fb.info[:5]                                  # logged live, one line per check
        assert any("If anything failed, copy this" in m for m in fb.info)
        assert any(m.startswith("===== COPY FROM HERE") for m in fb.info)
        assert fb.progress == 100.0
        # everything passed: the working files are gone, unless asked to keep them
        work = [m for m in fb.info if m.startswith("Self-test in ")][0].split(" in ", 1)[1]
        assert not os.path.exists(work), work


@test
def selftest_algorithm_reports_failures_and_keeps_the_files():
    algs, mod, fq = _algorithm_module()
    with Tmp() as d, no_models(), fake_gdal(half_cell_bug=True):
        classes = [c for c in mod.algorithm_classes() if c.__name__ != "SelfTestAlgorithm"]
        out, fb = _run_algorithm(mod, algs, fq, d, _Reg(classes), version=(32700, "3.27.0"))
        text = open(out["REPORT"], encoding="utf-8").read()
        assert "[FAIL] QGIS version" in text and "the plugin needs QGIS 3.28" in text
        assert "[FAIL] Plugin algorithms registered and buildable" in text
        assert "not registered as ndbreach:selftest" in text          # the wiring is missing
        assert "[FAIL] GDAL: GdalTerrain.read (metres and feet)" in text
        assert "half-cell shift" in text
        assert "[SKIP] ndhazard provider and its algorithms" in text
        errs = "\n".join(fb.errors)
        assert "[FAIL] QGIS version" in errs and "check(s) failed. Working files kept in" in errs
        assert re.match(r"^\d+ PASS, [3-9] FAIL, \d+ SKIP$", out["RESULT"]), out["RESULT"]
        work = [m for m in fb.info if m.startswith("Self-test in ")][0].split(" in ", 1)[1]
        assert os.path.isdir(work)
        shutil.rmtree(work, ignore_errors=True)
    # keep=True keeps the files of a clean run as well
    with Tmp() as d, no_models(), fake_gdal():
        out, fb = _run_algorithm(mod, algs, fq, d, _Reg(mod.algorithm_classes()), {"KEEP": True})
        assert out["RESULT"].split(",")[1].strip() == "0 FAIL", out["RESULT"]
        work = [m for m in fb.info if m.startswith("Self-test in ")][0].split(" in ", 1)[1]
        assert os.path.isdir(work) and any("Working files kept" in m for m in fb.info)
        shutil.rmtree(work, ignore_errors=True)


@test
def selftest_algorithm_default_report_path_and_bad_folder():
    algs, mod, fq = _algorithm_module()
    with Tmp() as d, no_models(), fake_gdal():
        out, fb = _run_algorithm(mod, algs, fq, d, _Reg(mod.algorithm_classes()),
                                 {"OUTPUT": ""})
        try:
            assert re.search(r"ndbreach_selftest_\d{8}_\d{6}\.txt$", out["REPORT"]), out["REPORT"]
            assert os.path.exists(out["REPORT"])
        finally:
            if os.path.exists(out["REPORT"]):
                os.remove(out["REPORT"])
        blocker = os.path.join(d, "afile")
        open(blocker, "w").write("x")
        try:
            _run_algorithm(mod, algs, fq, d, _Reg(mod.algorithm_classes()),
                           {"OUTPUT": os.path.join(blocker, "sub", "r.txt")})
            raise AssertionError("expected QgsProcessingException")
        except Exception as e:
            assert type(e).__name__ == "QgsProcessingException" and "Cannot write the report" in str(e)


@test
def selftest_qgis_version_lookup():
    algs, mod, fq = _algorithm_module()
    import qgis.core as core
    keep = core.Qgis
    try:
        class Q1:
            QGIS_VERSION_INT = 32812
            QGIS_VERSION = "3.28.12-Firenze"

        class Q2:
            @staticmethod
            def versionInt():
                return 33400

            @staticmethod
            def version():
                return "3.34.0"

        class Q3:
            QGIS_VERSION_INT = "junk"
        core.Qgis = Q1
        assert mod.qgis_version() == (32812, "3.28.12-Firenze")
        core.Qgis = Q2
        assert mod.qgis_version() == (33400, "3.34.0")
        core.Qgis = Q3
        assert mod.qgis_version()[0] is None
        core.Qgis = type("Q4", (), {})
        assert mod.qgis_version() == (None, "")
    finally:
        core.Qgis = keep


@test
def selftest_command_line_main():
    with Tmp() as d, no_models(), fake_gdal():
        rep = os.path.join(d, "report.txt")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = st.main(["--no-model", "--workdir", os.path.join(d, "w"), "--report", rep])
        assert code == 0, out.getvalue()[-1500:]
        text = out.getvalue()
        assert "[PASS] Python" in text and "===== COPY FROM HERE =====" in text
        assert "Result: " in open(rep, encoding="utf-8").read()
        with fake_gdal(fill_bug=True):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = st.main(["--no-model", "--workdir", os.path.join(d, "w2")])
        assert code == 1 and "[FAIL] GDAL: fill no-data gaps" in out.getvalue()


# ------------------------------------------------------------------------------ with real models
def _exe():
    exe = os.environ.get("NDBREACH_LISFLOOD", "")
    assert exe and os.path.isfile(exe), "NDBREACH_LISFLOOD must point to the patched lisflood"
    return exe


@model_test
def selftest_small_model_runs_through_the_stand_in():
    from nd_breach_runner.core import workflow  # noqa: F401
    with Tmp() as d, fake_gdal():
        lines = []
        res = st.run_small_model(_exe(), d, "lisflood", "", 1, log=lines.append)
        detail = st.verify_model_rasters(res, expect_model="LISFLOOD-FP")
        assert "LISFLOOD-FP:" in detail and "acres" in detail, detail
        prod = st.verify_products(res, expect_gpkg=False)
        assert "EAP map" in prod, prod
        assert res.summary["flooded_acres"] > 20, res.summary
        assert any("LISFLOOD-FP run 1" in m for m in lines), lines[:5]
        # the verifier refuses a run that flooded nothing
        res.summary["flooded_acres"] = 1.0
        try:
            st.verify_model_rasters(res)
            raise AssertionError("expected AssertionError")
        except AssertionError as e:
            assert "flooded only" in str(e)


@model_test
def selftest_run_all_with_real_models_and_the_stand_in():
    exe = _exe()
    py = os.environ.get("NDBREACH_ANUGA_PYTHON", "")
    with Tmp() as d, fake_gdal():
        seen = []
        rep = st.Report()
        st.run_all(rep, d, exe, py, True, 1, progress=lambda p, t: seen.append((p, t)))
        b = by_name(rep)
        for name in ("LISFLOOD-FP executable", "LISFLOOD-FP calibrate() on a small flood",
                     "Small model: LISFLOOD-FP end to end", "Small model: products"):
            assert b[name].status == st.PASS, "{}: {}".format(name, b[name].detail)
        assert "cell-seconds" in b["LISFLOOD-FP calibrate() on a small flood"].detail
        if py and os.path.isfile(py):
            assert b["ANUGA environment"].status == st.PASS, b["ANUGA environment"].detail
            assert b["Small model: ANUGA tier"].status == st.PASS, \
                b["Small model: ANUGA tier"].detail
        assert rep.ok(), rep.text()
        assert seen[-1][0] == 100.0 and all(seen[i][0] <= seen[i + 1][0]
                                             for i in range(len(seen) - 1)), seen[-3:]
        # a LISFLOOD-FP that does not run: the model checks fail or skip, the rest still runs
    with Tmp() as d, fake_gdal():
        sh = os.path.join(d, "fake_lisflood")
        with open(sh, "w") as f:
            f.write("#!/bin/sh\nexit 3\n")
        os.chmod(sh, 0o755)
        rep = st.Report()
        st.run_all(rep, d, sh, "", True, 1)
        b = by_name(rep)
        assert b["LISFLOOD-FP executable"].status == st.FAIL
        assert b["LISFLOOD-FP calibrate() on a small flood"].status == st.SKIP
        assert b["Small model: LISFLOOD-FP end to end"].status == st.SKIP
        assert b["GDAL: fill no-data gaps"].status == st.PASS
