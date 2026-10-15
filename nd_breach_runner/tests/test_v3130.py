"""Tests of 3.13.0 (ideas from the DamSafety plugins audit): the pre-run check
(damsuite.solvegate), the breach inflow fallbacks (core.terrain.place_inflow), culvert
candidates where roads cross the flow path, the DWR dam line as the crest, the wizard's DWR
dam IDs and the dam-layer loader's list."""
import json
import os
import shutil
import stat
import sys
import tempfile

import numpy as np

from nd_breach_runner.core import terrain
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.tests.registry import test


class _Env:
    """Set (value) or remove (None) environment variables for the block."""

    def __init__(self, **kw):
        self.kw = kw

    def __enter__(self):
        self.old = {k: os.environ.get(k) for k in self.kw}
        for k, v in self.kw.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _script(folder, name, body):
    p = os.path.join(folder, name)
    with open(p, "w") as f:
        f.write("#!/bin/sh\n" + body + "\n")
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return p


def _reader(zfun):
    """read(bounds, cell) -> Grid of z = zfun(X, Y) (cell centres)."""
    def read(bounds, cell):
        x0, y0, x1, y1 = bounds
        nx = max(1, int(round((x1 - x0) / cell)))
        ny = max(1, int(round((y1 - y0) / cell)))
        xs = x0 + (np.arange(nx) + 0.5) * cell
        ys = y1 - (np.arange(ny) + 0.5) * cell
        X, Y = np.meshgrid(xs, ys)
        return Grid(zfun(X, Y), x0, y1, cell)
    return read


# ----------------------------------------------------------------- solvegate --
@test
def v3130_solvegate_dem_and_hydrograph_checks():
    from nd_breach_runner.damsuite import solvegate as SG
    assert SG.dem_covers([(0, 0, 100, 100)], (50, 50), 0.1)["status"] == SG.OK
    assert SG.dem_covers([(0, 0, 100, 100)], (150, 50))["status"] == SG.FAIL
    assert SG.dem_covers([(0, 0, 100, 100)], (50, 50), 0.8)["status"] == SG.FAIL
    assert SG.dem_covers([], (50, 50))["status"] == SG.FAIL
    t = [0, 600, 1200, 3600]
    assert SG.hydro_stats(t, [0, 500, 100, 0])["status"] == SG.OK
    assert SG.hydro_stats(t, [0, 0, 0, 0])["status"] == SG.FAIL
    assert SG.hydro_stats([0, 600], [100, 0])["status"] == SG.WARN
    assert SG.hydro_stats([], [])["status"] == SG.FAIL
    assert SG.one_crs(["EPSG:26914", "EPSG:26914"])["status"] == SG.OK
    assert SG.one_crs(["EPSG:26914", "EPSG:5070"])["status"] == SG.FAIL
    msg = SG.message([SG.hydro_stats(t, [0, 0, 0, 0]), SG.one_crs(["EPSG:1"])])
    assert msg.startswith("Stopped before the flood run (pre-run check)") and "peak" in msg
    assert SG.message([SG.one_crs(["EPSG:1"])]) == ""


@test
def v3130_solvegate_layer_overlap_tells_no_crs_from_no_overlap():
    from nd_breach_runner.damsuite import solvegate as SG
    box = (0, 0, 1000, 1000)
    no_crs = SG.layer_overlap("Structures", False, None, box, 10)
    assert no_crs["status"] == SG.FAIL and "no CRS" in no_crs["detail"]
    miss = SG.layer_overlap("Structures", True, (5000, 5000, 6000, 6000), box, 10)
    assert miss["status"] == SG.FAIL and "not a missing CRS" in miss["detail"]
    # a statewide layer with nothing near the dam meets the box: not an error
    state = SG.layer_overlap("Structures", True, (-1e6, -1e6, 1e6, 1e6), box, 10)
    assert state["status"] == SG.OK
    assert SG.layer_overlap("Structures", True, None, box, 10)["status"] == SG.WARN
    assert SG.layer_overlap("Structures", False, None, box, 0)["status"] == SG.OK


@test
def v3130_solvegate_storage_check():
    from nd_breach_runner.damsuite import solvegate as SG
    a = SG.ACFT
    assert SG.storage_check(1000 * a, 1100 * a).startswith("inventory storage")
    assert SG.storage_check(1000 * a, 2000 * a).startswith("differs:")
    assert SG.storage_check(0, 500 * a).startswith("differs:")
    assert SG.storage_check(1000 * a, None) == "" and SG.storage_check(1000 * a, 0) == ""


@test
def v3130_lisflood_smoke_passes_fails_and_caches():
    if os.name == "nt":
        return
    from nd_breach_runner.damsuite import solvegate as SG
    d = tempfile.mkdtemp()
    try:
        good = _script(d, "lf_good", 'test -f "$1" && test -f dem.asc && '
                                     ': > out/smoke.max && exit 0; exit 3')
        quiet = _script(d, "lf_quiet", 'echo "usage: lisflood file.par"; exit 0')
        bad = _script(d, "lf_bad", 'echo "error while loading shared libraries" >&2; exit 127')
        with _Env(ND_DAM_PRE_RUN_SMOKE=None):
            SG._SMOKE_CACHE.clear()
            r = SG.lisflood_smoke(good)
            assert r["status"] == SG.OK, r
            assert len(SG._SMOKE_CACHE) == 1
            # cached: the same answer without running it again (the file is gone from the run)
            assert SG.lisflood_smoke(good)["status"] == SG.OK
            r = SG.lisflood_smoke(bad)
            assert r["status"] == SG.FAIL and "127" in r["detail"] and "shared" in r["detail"]
            assert len(SG._SMOKE_CACHE) == 1                     # a failure is not cached
            assert SG.lisflood_smoke(os.path.join(d, "none"))["status"] == SG.FAIL
            r = SG.lisflood_smoke(quiet)
            assert r["status"] == SG.FAIL and "no result" in r["detail"] and "usage" in r["detail"]
            # the bundled Linux build really runs (when this computer can run it)
            from nd_breach_runner.core import lisflood as lf
            exe = lf.bundled_exe()
            if exe and sys.platform.startswith("linux") and os.access(exe, os.X_OK):
                r = SG.lisflood_smoke(exe)
                assert r["status"] == SG.OK, r
            slow = _script(d, "lf_slow", "sleep 5")
            r = SG.lisflood_smoke(slow, timeout_s=1)
            assert r["status"] == SG.FAIL and "no answer" in r["detail"]
        with _Env(ND_DAM_PRE_RUN_SMOKE="0"):
            assert SG.lisflood_smoke(bad)["status"] == SG.OK
    finally:
        SG._SMOKE_CACHE.clear()
        shutil.rmtree(d, ignore_errors=True)


@test
def v3130_damsuite_is_1_9_with_solvegate():
    from nd_breach_runner import damsuite
    from nd_breach_runner.damsuite import versions
    from nd_breach_runner.core import workflow as wf
    assert damsuite.SUITE_VERSION >= "1.10.0" and "solvegate" in damsuite.__all__
    assert damsuite.selfcheck() == []
    # 3.18.0: the release table names this plugin's own version (the literals went stale every release)
    # 3.20.0: and a fourth plugin, the ND Data Updater, which is in the release table but not in
    # PLUGINS - nothing requires a version of it, so only versions.ALL carries it
    assert set(versions.RELEASE) == {"ndhazard", "ndbreach", "ndscreen", "ndupdater"}
    assert "ndupdater" not in versions.PLUGINS and "ndupdater" in versions.ALL
    assert versions.RELEASE["ndbreach"] == wf.VERSION


# ------------------------------------------------------------ inflow fallback --
@test
def v3130_inflow_first_search_is_unchanged():
    # a valley right below the dam point: the old search finds it, nothing moves
    read = _reader(lambda X, Y: 100.0 - 0.1 * np.maximum(X, 0))
    xy, note, moved = terrain.place_inflow(read, (0.0, 0.0), 5.0, 100.0, 5.0)
    assert moved is None and "lowest ground" in note
    assert xy[0] > 0


@test
def v3130_inflow_wide_search_takes_the_nearest_low_cell():
    # 5 m dam (search radius 90 m); the ground stays high for 200 m, then drops
    read = _reader(lambda X, Y: np.where(X < 200.0, 99.0, np.where(X < 380.0, 94.0, 80.0)))
    xy, note, moved = terrain.place_inflow(read, (0.0, 0.0), 5.0, 100.0, 5.0)
    assert moved is None and "toe outside" in note
    assert 195.0 <= xy[0] <= 210.0            # the nearest low cell, not the lowest (x > 380)


@test
def v3130_inflow_moves_to_the_crest_only_when_the_top_is_known():
    # dam point 300 m short of the embankment (ridge at the top of dam, 300-500 m), valley
    # beyond 500 m: out of reach of the 400 m search from the given point
    def z(X, Y):
        return np.where(X < 300.0, 99.0, np.where(X <= 500.0, 100.0, 85.0))
    read = _reader(z)
    xy, note, moved = terrain.place_inflow(read, (0.0, 0.0), 5.0, 100.0, 5.0)
    assert moved is not None and 300.0 <= moved[0] <= 400.0
    assert "dam point moved" in note and xy[0] > 500.0
    try:
        terrain.place_inflow(read, (0.0, 0.0), 5.0, 100.0, 5.0, top_known=False)
    except ValueError as e:
        assert "half" in str(e) or "below the top of dam" in str(e)
    else:
        raise AssertionError("an unknown top of dam must not move the dam point")


@test
def v3130_crest_is_not_a_hill():
    read = _reader(lambda X, Y: np.where(X > 200.0, 110.0, 99.0))
    g = read((-400, -400, 400, 400), 5.0)
    xy, why = terrain.crest_near(g, (0.0, 0.0), 100.0, 5.0)
    assert xy is None and "not the top of dam" in why


# -------------------------------------------------------- culvert candidates --
@test
def v3130_culvert_candidates_at_every_road_crossing():
    from nd_breach_runner.core import structures as S
    path = [(0, 0), (1000, 0), (2000, 0)]
    roads = [[(500, -100), (500, 100)],                       # crosses once
             [(1500, -100), (1600, 0), (1500, 100)],          # local segment is diagonal
             [(0, 500), (2000, 500)]]                         # never crosses
    c = S.culvert_candidates(roads, path)
    assert [o["road"] for o in c] == [1, 2]
    assert abs(c[0]["x"] - 500) < 1e-6 and c[0]["angle_deg"] == 90
    assert c[1]["angle_deg"] == 45                           # the local segment, not end to end
    assert all(o["source"] == "candidate" and o["surveyed"] is False for o in c)
    assert c[0]["name"].startswith("candidate 1 (road 1 at 0.31 mi")

    class _St:
        line = [(495.0, -5.0), (505.0, 5.0)]
    assert [o["road"] for o in S.culvert_candidates(roads, path, [_St()])] == [2]
    # a road that zigzags over the path is one candidate per crossing (30 m apart or more)
    zig = [[(100, -50), (110, 50), (200, -50), (210, 50)]]
    assert len(S.culvert_candidates(zig, path)) == 3
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "c.csv")
        S.write_candidates_csv(p, c)
        text = open(p, encoding="utf-8").read()
        assert "candidate 1" in text and "surveyed" in text
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------- DWR dam line crest --
@test
def v3130_crest_line_by_dwr_structure_id_else_nearest():
    from nd_breach_runner import algorithms as A
    assert A._dwr_ids("DWR-02801", None) == {"2801"}
    assert A._dwr_ids("ND00123", {"dwr_structure_id": 2801.0}) == {"2801"}
    assert A._dwr_ids("ND00123", None) == set()
    near = [(0, 50), (100, 50)]
    far = [(0, 400), (100, 400)]
    lines = [(near, {"structure_id": 1}), (far, {"structure_id": "2801"})]
    ln, d, how = A._pick_crest_line(lines, (50, 0), {"2801"}, "structure_id")
    assert ln is far and abs(d - 400) < 1e-6 and "2801" in how
    ln, d, how = A._pick_crest_line(lines, (50, 0), set(), "structure_id")
    assert ln is near and how == "nearest line"
    ln, d, why = A._pick_crest_line([(far, {})], (50, -200), set(), None)
    assert ln is None and "600" in why
    dist, q = A._nearest_on_line([(0, 0), (100, 0), (100, 100)], (150, 50))
    assert abs(dist - 50) < 1e-9 and q == (100.0, 50.0)


@test
def v3130_crest_line_fills_from_the_dam_folder():
    from nd_breach_runner import provider
    assert provider._RUN_MAPPING["CREST_LINE"] == "dwr_dam_lines"


# ----------------------------------------------------------- wizard and menu --
@test
def v3130_wizard_names_dwr_dams_and_reads_dwr_storage():
    from nd_breach_runner import wizard_logic as wl
    names = ["national_id", "dam_name", "max_dam_height", "max_pool_vol", "max_pool_area",
             "max_pool_elev", "structure_id"]
    m = wl.field_mapping(names)["mapping"]
    assert m["F_STORAGE"] == "max_pool_vol" and m["F_AREA"] == "max_pool_area"
    assert "F_TOP" not in m                                  # local datum, not a top of dam
    v = wl.dam_values_from_attrs({"national_id": None, "dam_name": "Elm River Dam 1",
                                  "max_dam_height": 35.0, "max_pool_vol": 1616.0,
                                  "structure_id": 2801.0}, m)
    assert v["DAM_ID"] == "DWR-2801" and v["STORAGE_ACFT"] == 1616.0
    v = wl.dam_values_from_attrs({"national_id": "ND00123", "structure_id": 5}, m)
    assert v["DAM_ID"] == "ND00123"
    assert wl.dwr_dam_id({}) == ""


@test
def v3130_load_layers_lists_dam_folder_then_suite_dwr():
    from nd_breach_runner import suite_fill
    from nd_breach_runner.damsuite import damdata as DD
    from nd_breach_runner.damsuite import dam_record as DR
    d = tempfile.mkdtemp()
    try:
        dam = os.path.join(d, "dams", "DWR-1")
        os.makedirs(os.path.join(dam, "data"))
        rec = DR.load(dam, create=True)
        for role, name in (("roads", "roads.gpkg"), ("dwr_dam_lines", "lines.gpkg")):
            p = os.path.join(dam, "data", name)
            open(p, "w").close()
            DD.set_entry(rec, role, p, source="test")
        rec.save()
        suite = os.path.join(d, "suite")
        os.makedirs(os.path.join(suite, "dwr"))
        for name in ("DWR_Dams.gpkg", "DWR_Dam_Lines.gpkg", "DWR_Dugouts.gpkg"):
            open(os.path.join(suite, "dwr", name), "w").close()
        out = suite_fill.layer_list(dam, suite)
        names = [o[0] for o in out]
        assert "roads" in names and "DWR dam lines" in names
        assert "DWR dams (suite data)" in names                 # the dam folder has none
        assert "DWR dam lines (suite data)" not in names        # the dam folder's wins
        assert not any("ugout" in o[1] for o in out)
        assert all(o[2] == "vector" for o in out)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3130_run_stops_before_the_solve_when_lisflood_does_not_start():
    if os.name == "nt":
        return
    from nd_breach_runner.core import breach as br
    from nd_breach_runner.core import workflow
    from nd_breach_runner.damsuite import solvegate as SG
    from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer
    d = tempfile.mkdtemp()
    try:
        bad = _script(d, "lisflood", 'echo "error while loading shared libraries" >&2; exit 127')
        st = workflow.RunSettings(cell_size_m=20.0, initial_length_mi=1.0, max_iterations=1,
                                  lisflood_exe=bad)
        dam = br.DamData(dam_id="T1", dam_height_ft=25.0, storage_top_acft=500.0,
                         top_of_dam_ft=1850.0, surface_area_top_ac=60.0)
        with _Env(ND_DAM_PRE_RUN_SMOKE=None):
            SG._SMOKE_CACHE.clear()
            try:
                workflow.run_dam(dam, (0.0, 0.0), st, SyntheticTerrain(), npy_writer,
                                 os.path.join(d, "T1"))
            except br.BreachInputError as e:
                assert "pre-run check" in str(e) and "LISFLOOD-FP starts" in str(e), e
            else:
                raise AssertionError("a LISFLOOD-FP that does not start must stop the run")
        assert not os.path.exists(os.path.join(d, "T1", "lisflood")) or not any(
            n.endswith(".max") for _r, _d, fs in os.walk(os.path.join(d, "T1")) for n in fs)
    finally:
        SG._SMOKE_CACHE.clear()
        shutil.rmtree(d, ignore_errors=True)


@test
def v3130_terrain_cache_asks_for_a_failed_tile_in_quarters():
    try:
        from osgeo import gdal, osr
    except ImportError:
        return
    from nd_breach_runner.damsuite import terrain_cache as TC
    calls = []

    def fetch(tb, cell, size, out_path):
        calls.append(size)
        if size[0] >= 1000:
            raise IOError("timed out")
        nx, ny = size
        ds = gdal.GetDriverByName("GTiff").Create(out_path, nx, ny, 1, gdal.GDT_Float32)
        ds.SetGeoTransform((tb[0], cell, 0, tb[3], 0, -cell))
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(5070)
        ds.SetProjection(srs.ExportToWkt())
        xs = tb[0] + (np.arange(nx) + 0.5) * cell
        ys = tb[3] - (np.arange(ny) + 0.5) * cell
        X, Y = np.meshgrid(xs, ys)
        ds.GetRasterBand(1).WriteArray((X * 0.001 + Y * 0.002).astype(np.float32))
        ds = None

    d = tempfile.mkdtemp()
    try:
        cdir = os.path.join(d, "cache")
        logs = []
        res = TC.get((100.0, 100.0, 900.0, 900.0), 1.0, cdir, fetch, os.path.join(d, "a.vrt"),
                     tile_px=1000, log=logs.append)
        assert res["fetched"] == 1 and calls == [(1000, 1000)] + [(500, 500)] * 4, calls
        t = gdal.Open(res["tiles"][0])
        assert (t.RasterXSize, t.RasterYSize) == (1000, 1000)
        gt = t.GetGeoTransform()
        assert gt[0] == 0.0 and gt[3] == 1000.0 and gt[1] == 1.0
        a = t.GetRasterBand(1).ReadAsArray()
        # each quarter is in its place: the cell centre's value
        for r, c in ((10, 10), (10, 900), (900, 10), (900, 900), (499, 500), (500, 499)):
            x, y = 0.5 + c, 1000 - 0.5 - r
            assert abs(a[r, c] - (x * 0.001 + y * 0.002)) < 1e-3, (r, c, a[r, c])
        assert any("4 parts" in m for m in logs)
        assert not [f for f in os.listdir(cdir) if not f.endswith(".tif") or ".q" in f]
        # every quarter failing too: the tile fails as before
        def never(tb, cell, size, out_path):
            raise IOError("no network")
        try:
            TC.get((0.0, 0.0, 10.0, 10.0), 1.0, os.path.join(d, "c2"), never,
                   os.path.join(d, "b.vrt"), tile_px=1000)
        except TC.TerrainError as e:
            assert "no network" in str(e)
        else:
            raise AssertionError("a tile that cannot be fetched must fail")
    finally:
        shutil.rmtree(d, ignore_errors=True)
