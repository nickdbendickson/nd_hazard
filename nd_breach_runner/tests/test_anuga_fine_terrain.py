"""3.27.10 ANUGA terrain cell size option (anuga_cell_m): ANUGA samples its mesh elevation from a
finer DEM read only over the flooded footprint, while the model grid, the output rasters and
LISFLOOD-FP stay at the model cell size. These tests guard the plumbing without running ANUGA:
the footprint clip, the manning/init regrid, the elevation/output-grid independence in write_job,
and that _run ships the fine elevation grid but keeps the coarse output grid."""
import json
import os
import tempfile

import numpy as np

from nd_breach_runner.core.grid import Grid
from nd_breach_runner.core import anuga_model as A
from nd_breach_runner.core import workflow as W
from nd_breach_runner.tests.registry import test


@test
def footprint_bounds_clips_to_mask_and_grid():
    g = Grid(np.zeros((10, 10)), x0=0.0, y0=100.0, dx=10.0)   # world bounds (0, 0, 100, 100)
    mask = np.zeros((10, 10), bool)
    mask[2:4, 5:7] = True                                     # cols 5-6 -> x 50-70; rows 2-3 -> y 60-80
    assert W._footprint_bounds(g, mask, 0.0) == (50.0, 60.0, 70.0, 80.0)
    assert W._footprint_bounds(g, mask, 1000.0) == (0.0, 0.0, 100.0, 100.0)   # pad clipped to grid
    assert W._footprint_bounds(g, np.zeros((10, 10), bool), 0.0) == g.bounds  # empty -> whole grid


@test
def regrid_matches_reference_grid():
    src = Grid(np.full((5, 5), 3.0), x0=0.0, y0=50.0, dx=10.0)
    ref = Grid(np.zeros((25, 25)), x0=0.0, y0=50.0, dx=2.0)   # 2 m over the same 50 m square
    out = A._regrid(src, ref)
    assert out.data.shape == ref.data.shape and out.dx == ref.dx
    assert np.allclose(out.data[np.isfinite(out.data)], 3.0)  # a constant field is preserved
    assert A._regrid(None, ref) is None


@test
def anuga_cell_setting_defaults_off_and_roundtrips():
    assert W.RunSettings().anuga_cell_m == 0.0                # off by default
    st = W.RunSettings(anuga_cell_m=2.0)
    assert st.anuga_cell_m == 2.0
    assert st.as_dict()["anuga_cell_m"] == 2.0


class _Hyd:
    t_s = np.array([0.0, 3600.0])
    q_cms = np.array([0.0, 10.0])


def _settings(manning_grid=None):
    return A.AnugaSettings(python="/does/not/matter", threads=0, manning_n=0.05,
                           manning_grid=manning_grid, sim_time_s=3600.0, fine_area_m2=100.0,
                           coarse_area_m2=1000.0, buffer_m=200.0)


@test
def write_job_keeps_output_grid_independent_of_elevation_grid():
    """The feature rests on write_job shipping the elevation DEM and the output grid separately:
    a 2 m elevation grid with a 10 m output grid stores dem_dx=2 and out_dx=10."""
    elev = Grid(np.full((100, 100), 5.0), x0=0.0, y0=200.0, dx=2.0)
    out_grid = Grid(np.zeros((20, 20)), x0=0.0, y0=200.0, dx=10.0)
    with tempfile.TemporaryDirectory() as d:
        job = os.path.join(d, "job.npz")
        A.write_job(job, elev, np.zeros((0, 2)), np.zeros((0, 2)), np.zeros((2, 2)),
                    _Hyd(), _settings(), out_grid)
        jd = dict(np.load(job, allow_pickle=False))
    assert float(jd["dem_dx"]) == 2.0 and jd["dem"].shape == (100, 100)
    assert float(jd["out_dx"]) == 10.0 and int(jd["out_nx"]) == 20 and int(jd["out_ny"]) == 20


def _patch(obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    return (obj, name, old)


def _run_capturing(mesh_elev_dem, manning_grid=None, initial_depth=None):
    """Drive A._run with ANUGA mocked out; return what write_job received."""
    coarse = Grid(np.full((20, 20), 5.0), x0=0.0, y0=200.0, dx=10.0)   # model/output grid (10 m)
    flooded = np.zeros((20, 20), bool)
    flooded[5:15, 5:15] = True
    seen = {}

    def fake_write_job(path, dem, outer, fine_poly, inlet, hyd, st, out_grid, initial=None,
                       *a, **k):
        seen["elev_dx"] = dem.dx
        seen["out_dx"] = out_grid.dx
        seen["manning_dx"] = None if st.manning_grid is None else st.manning_grid.dx
        seen["init_dx"] = None if initial is None else initial.dx
        np.savez(path, stub=np.zeros(1))

    def fake_launch(st, job, result, workdir, progress, cancel):
        z = np.zeros((coarse.ny, coarse.nx))
        z[8, 8] = 1.0
        np.savez(result, depth=z, velocity=z, arrival_hr=z, wse=z,
                 stats=json.dumps({"max_edge_depth_m": 0.0, "breaklines_used": False,
                                   "triangles": 10, "culvert_errors": []}))

        class _P:
            returncode = 0
        return _P(), os.path.join(workdir, "anuga.log")

    patches = [
        _patch(A, "write_job", fake_write_job),
        _patch(A, "mesh_polygons", lambda *a, **k: (
            np.array([[0.0, 0.0], [100.0, 0.0], [100.0, 100.0]]), np.zeros((0, 2)))),
        _patch(A, "_launch", fake_launch),
        _patch(A.os.path, "isfile", lambda p: True),           # skip the interpreter check
    ]
    try:
        with tempfile.TemporaryDirectory() as d:
            A._run(d, coarse, flooded, (50.0, 100.0), (1.0, 0.0), 20.0, _Hyd(),
                   _settings(manning_grid=manning_grid), initial_depth=initial_depth,
                   mesh_elev_dem=mesh_elev_dem)
    finally:
        for obj, name, old in patches:
            setattr(obj, name, old)
    return seen


@test
def run_ships_fine_elevation_and_regrids_manning_init():
    coarse_frame = Grid(np.zeros((20, 20)), x0=0.0, y0=200.0, dx=10.0)
    fine = Grid(np.full((100, 100), 5.0), x0=0.0, y0=200.0, dx=2.0)
    manning = coarse_frame.like(np.full((20, 20), 0.06))
    init = coarse_frame.like(np.zeros((20, 20)))
    seen = _run_capturing(fine, manning_grid=manning, initial_depth=init)
    assert seen["elev_dx"] == 2.0            # elevation sampled from the fine grid
    assert seen["out_dx"] == 10.0            # output stays on the coarse grid
    assert seen["manning_dx"] == 2.0         # manning resampled to the fine grid
    assert seen["init_dx"] == 2.0            # initial depth resampled to the fine grid


@test
def run_without_fine_terrain_uses_the_model_grid():
    seen = _run_capturing(None)
    assert seen["elev_dx"] == 10.0 and seen["out_dx"] == 10.0     # default: one grid, unchanged
