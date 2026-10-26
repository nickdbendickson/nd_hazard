"""3.26.5: the water leaving the model also extends the domain, not only an edge's depth.

The domain grew only while an edge was deeper than the edge tolerance (1 ft). At Absaraka
(DWR-2801) a sheet under 1 ft carried 36-40% of the breach volume out of the south and east
edges with the domain never growing there (runs-notes/edge-rule-extent-2026-10-26). Now, once
outflow_extend_pct (5% by default, OUTFLOW_EXTEND_PCT) of the breach volume has left, every edge
that carries a real share of it (terrain.edge_flux_shares >= OUTFLOW_EDGE_SHARE) and is wet past
the arrival depth moves out too. LISFLOOD-FP is stubbed as in test_v320 (fake_models)."""
import contextlib
import json
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests.test_v320 import _dam, _settings


@contextlib.contextmanager
def _sheet(depth_m, outflow_pct):
    """A flood band |y| < 300 m east of the dam, `depth_m` deep, reaching the east edge of every
    domain, with `outflow_pct` of the breach volume reported as left through the edges."""
    from nd_breach_runner.core import lisflood as lf

    def fake_run(wd, par, st, progress=None, cancel=None, log=None, name="run"):
        return 60.0

    def fake_read(wd, dem, st, hydrograph, name="run"):
        X, Y = np.meshgrid(dem.x_centers(), dem.y_centers())
        d = np.where((X > 0) & (np.abs(Y) < 300), depth_m, np.nan)
        vel = np.where(np.isfinite(d), 1.0, np.nan)
        arr = np.where(np.isfinite(d), 0.5, np.nan)
        wse = np.where(np.isfinite(d), dem.data + depth_m, np.nan)
        stats = {"max_volume_error_pct": 0.1, "peak_inflow_cfs": 1000.0,
                 "boundary_outflow_pct": outflow_pct, "boundary_outflow_acft": 5.0 * outflow_pct}
        return lf.Envelopes(dem.like(d, np.nan), dem.like(vel, np.nan), dem.like(arr, np.nan),
                            dem.like(wse, np.nan), stats, [], {})
    from nd_breach_runner.core import workflow as wf
    old = (lf.run, lf.read_results, wf.SG.lisflood_smoke)
    lf.run, lf.read_results = fake_run, fake_read
    # the pre-run smoke test of the stub executable (sys.executable) is not what is tested here
    wf.SG.lisflood_smoke = lambda exe, *a, **k: wf.SG.check(wf.SG.OK, "LISFLOOD-FP starts")
    try:
        yield
    finally:
        lf.run, lf.read_results, wf.SG.lisflood_smoke = old


def _run(folder, depth_m, outflow_pct, **k):
    from nd_breach_runner.core import workflow as wf
    from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer
    with _sheet(depth_m, outflow_pct):
        r = wf.run_dam(_dam(), (0.0, 0.0), _settings(max_iterations=2, **k), SyntheticTerrain(),
                       npy_writer, folder)
    with open(r.report_json, encoding="utf-8") as f:
        return r, json.load(f)


@test
def v3265_edge_flux_shares_weigh_the_edges_by_depth_to_the_five_thirds():
    from nd_breach_runner.core import terrain
    d = np.full((5, 8), np.nan)
    d[1:-1, -1] = 0.1                    # east edge: 3 cells, 0.1 m (not the corners)
    d[-1, 2:4] = 0.4                     # south edge: 2 cells, 0.4 m
    s = terrain.edge_flux_shares(d)
    e, south = 3 * 0.1 ** (5 / 3.0), 2 * 0.4 ** (5 / 3.0)
    assert abs(s["E"] - e / (e + south)) < 1e-9 and abs(s["S"] - south / (e + south)) < 1e-9
    assert s["N"] == 0.0 and s["W"] == 0.0 and abs(sum(s.values()) - 1.0) < 1e-9
    assert terrain.edge_flux_shares(np.full((3, 3), np.nan)) == {"N": 0.0, "S": 0.0,
                                                                 "W": 0.0, "E": 0.0}
    valid = np.ones((5, 8), bool)
    valid[:, -1] = False                 # cells off the DEM carry nothing
    assert terrain.edge_flux_shares(d, valid)["E"] == 0.0


@test
def v3265_a_shallow_sheet_that_carries_the_water_out_extends_the_domain():
    from nd_breach_runner.core import workflow as wf
    assert wf.RunSettings().outflow_extend_pct == wf.OUTFLOW_WARN_PCT == 5.0
    assert wf.RunSettings().edge_tolerance_ft == 1.0          # the depth rule itself is unchanged
    tmp = tempfile.mkdtemp(prefix="ndbr_v3265_")
    try:
        # 0.5 ft deep (under the 1 ft tolerance), 30% of the volume leaving: east moves out
        _r, rep = _run(os.path.join(tmp, "A"), 0.5 * 0.3048, 30.0)
        runs = rep["lisflood_runs"]
        assert len(runs) == 2, runs
        assert runs[0]["edges_over_outflow"] == ["E"] and runs[0]["edges_over_tolerance"] == ["E"]
        assert runs[1]["size_mi"][0] > runs[0]["size_mi"][0]
        assert rep["settings"]["outflow_extend_pct"] == 5.0
        assert rep["cut_off"]["reason"] == wf.CUT_OFF_RUNS
        assert any("left the model through its edges when the run stopped (domain-run limit)"
                   in w for w in rep["warnings"]), rep["warnings"]
        md = open(_r.report_md, encoding="utf-8").read()
        assert "Once 5.0% of the breach volume has left the model, an edge the water" in md

        # the rule off: the depth rule alone, as before 3.26.5 - one run, the water still leaves
        _r, rep = _run(os.path.join(tmp, "B"), 0.5 * 0.3048, 30.0, outflow_extend_pct=0.0)
        runs = rep["lisflood_runs"]
        assert len(runs) == 1 and runs[0]["edges_over_tolerance"] == [], runs
        assert runs[0]["edges_over_outflow"] == []
        assert any("30% of the breach volume" in w for w in rep["warnings"]), rep["warnings"]
        assert "an edge the water leaves" not in open(_r.report_md, encoding="utf-8").read()

        # under the threshold (2% left) the shallow edge is left alone
        _r, rep = _run(os.path.join(tmp, "C"), 0.5 * 0.3048, 2.0)
        assert len(rep["lisflood_runs"]) == 1
        assert rep["lisflood_runs"][0]["edges_over_outflow"] == []
        assert not any("left the model" in w for w in rep["warnings"]), rep["warnings"]

        # water under the arrival depth (0.1 ft) is not a flood edge, whatever the outflow
        _r, rep = _run(os.path.join(tmp, "D"), 0.05 * 0.3048, 30.0)
        assert len(rep["lisflood_runs"]) == 1
        assert rep["lisflood_runs"][0]["edges_over_outflow"] == []

        # an edge already over the depth rule is not listed twice
        _r, rep = _run(os.path.join(tmp, "E"), 2.0 * 0.3048, 30.0)
        runs = rep["lisflood_runs"]
        assert runs[0]["edges_over_tolerance"] == ["E"] and runs[0]["edges_over_outflow"] == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@test
def v3265_the_tolerance_prints_as_given():
    """A 0.25 ft tolerance printed as '0.2 ft' in the run log and warnings ({:.1f})."""
    from nd_breach_runner.core import workflow as wf
    import inspect
    src = inspect.getsource(wf.run_dam)
    assert "deeper than {:.1f} ft" not in src and "below {:.1f} ft" not in src
