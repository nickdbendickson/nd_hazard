"""3.26.3: the arrival depth (ARRIVAL_FT) defaults to 0.1 ft, the ND hazard wet depth, not 1 ft.

Same change as DWR_Plugins Runner 3.10.53 (#131, numbered 3.10.45 on its branch). One constant,
core/units.ARRIVAL_DEPTH_FT, feeds every default: RunSettings, hecras.read_mesh / terrain_depth /
reexport_results, Breach products from rasters, the LISFLOOD-FP and ANUGA settings, the CLI JSON
template and 'hecras-reexport --arrival-depth-ft', and the Run a dam, HEC-RAS import and Breach
products tools. A structure flooded 0.1-1 ft deep now gets an arrival time (it had none at 1 ft)."""
import csv
import inspect
import json
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core import anuga_model, hecras, lisflood, products, units, workflow
from nd_breach_runner.core.units import FT
from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests.test_hecras import DMAX, _FakeH5, _mesh_layout

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _default(fn, name):
    return inspect.signature(fn).parameters[name].default


@test
def v3263_arrival_depth_defaults_to_0_1_ft_everywhere():
    assert units.ARRIVAL_DEPTH_FT == 0.1
    assert workflow.RunSettings().arrival_depth_ft == 0.1
    for fn in (hecras.read_mesh, hecras.terrain_depth, hecras.reexport_results,
               products.products_from_rasters):
        assert _default(fn, "arrival_depth_ft") == 0.1, fn.__name__
    for cls in (lisflood.LisfloodSettings, anuga_model.AnugaSettings):
        assert abs(_default(cls.__init__, "arrival_depth_m") - 0.1 * FT) < 1e-12, cls.__name__
    # the domain-extension edge tolerance is not the arrival depth and stays 1 ft
    assert workflow.RunSettings().edge_tolerance_ft == 1.0
    # CLI: the JSON template and the hecras-reexport option
    from nd_breach_runner.tests.test_cli import cli as _cli
    cli = _cli()
    assert cli.TEMPLATE["model"]["arrival_depth_ft"] == 0.1
    ns = cli.build_parser().parse_args(["hecras-reexport", "/x.p01.hdf"])
    assert ns.arrival_depth_ft == 0.1


@test
def v3263_tool_arrival_parameters_default_to_0_1_ft():
    from nd_breach_runner.tests.test_hecras import _algs
    h, _ = _algs()
    import nd_breach_runner.algorithms as a
    import nd_breach_runner.algorithms_tools as t
    seen = 0
    for mod in (a, t, h):
        for name in dir(mod):
            cls = getattr(mod, name)
            if not (inspect.isclass(cls) and cls.__module__ == mod.__name__
                    and hasattr(cls, "initAlgorithm")):
                continue
            try:
                alg = cls()
                alg.initAlgorithm()
            except Exception:
                continue
            p = alg.parameterDefinition("ARRIVAL_FT") if hasattr(alg, "parameterDefinition") \
                else None
            if p is None:
                continue
            dv = p.defaultValue() if callable(p.defaultValue) else p.defaultValue
            desc = p.description() if callable(p.description) else p.description
            assert dv == 0.1, (name, dv)
            assert "starting value 1 " not in str(desc), name
            seen += 1
    # Run a dam, HEC-RAS import, Breach products from rasters
    assert seen >= 3, seen
    for f in ("algorithms.py", "algorithms_hecras.py", "algorithms_tools.py"):
        src = open(os.path.join(PLUGIN, f), encoding="utf-8").read()
        assert '"ARRIVAL_FT"' in src and "ARRIVAL_DEPTH_FT" in src, f


@test
def v3263_shallow_cells_get_an_arrival_time_at_the_default():
    """The synthetic mesh with its water lowered: columns 0-3 peak at 0.5, 0.45, 0.4, 0.35 ft.
    At 1 ft none of them has an arrival time; at the 0.1 ft default all of them do, at
    2 h x 0.1 / peak (the depth grows linearly for 2 h)."""
    layout, root = _mesh_layout()
    scale = 0.1                                          # peaks DMAX x 0.1 = 0.5 .. 0.35 ft
    for key, (arr, at) in list(layout.items()):
        if key.endswith("Maximum Water Surface") or key.endswith("/Water Surface"):
            a = np.array(arr, float)
            elev_key = [k for k in layout if k.endswith("Minimum Water Surface")][0]
            elev = np.array(layout[elev_key][0][0], float)
            if a.ndim == 2 and key.endswith("Maximum Water Surface"):
                a[0] = elev + (a[0] - elev) * scale
            else:
                a = elev[None, :] + (a - elev[None, :]) * scale
            layout[key] = (a, at)
    src = _FakeH5(layout, root)
    g_new, _ = hecras.import_results(src)
    g_old, _ = hecras.import_results(src, arrival_depth_ft=1.0)
    peak = DMAX * scale
    wet_cols = g_new["depth"].data[:, :4]
    assert np.allclose(wet_cols, np.tile(peak, (wet_cols.shape[0], 1)))
    assert np.isnan(g_old["arrival"].data).all()                   # nothing reached 1 ft
    arr = g_new["arrival"].data
    assert np.isfinite(arr[:, :4]).all() and np.isnan(arr[:, 4:]).all()
    assert np.allclose(arr[:, :4], np.tile(2.0 * 0.1 / peak, (arr.shape[0], 1)))
    m = hecras.read_mesh(src)
    assert m.info["arrival_depth_ft"] == 0.1
    assert np.isfinite(m.fields["arrival"][np.isfinite(m.fields["depth"])]).all()


@test
def v3263_lisflood_workflow_shallow_cells_and_structures_get_an_arrival_time():
    """The LISFLOOD-FP workflow (run_dam) at the default and at the old 1 ft, with the model
    stubbed: lf.run writes nothing and lf.read_results returns a flood whose arrival, as
    LISFLOOD-FP's .inittm does, is the first time the depth reaches the .par's inittm_depth.
    Depth falls from 2 ft at the dam to 0 at 1,200 m, so the cells and structures 590-1,140 m
    out are flooded 0.1-1 ft. At the default they get an arrival time and are counted in
    receptors_flooded; at 1 ft they are not."""
    from nd_breach_runner.core import breach as br, impacts
    from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer
    seen = {}

    def fake_run(wd, par, st, progress=None, cancel=None, log=None, name="run"):
        seen["par"] = open(par, encoding="utf-8").read()
        return 60.0

    def fake_read(wd, dem, st, hydrograph, name="run"):
        X, Y = np.meshgrid(dem.x_centers(), dem.y_centers())
        depth = np.where((X > 0) & (np.abs(Y) < 300), 0.6 * (1.0 - X / 1200.0), np.nan)
        depth = np.where(depth > 0.001, depth, np.nan)
        h = st.arrival_depth_m                    # the inittm_depth the workflow wrote
        arr = np.where(depth >= h, X / 3600.0 + 0.5 * h / depth, np.nan)
        stats = {"cells": int(dem.data.size), "cell_size_m": dem.dx, "sim_time_hr": 4.0,
                 "max_volume_error_pct": 0.1}
        wet = np.isfinite(depth)
        return lisflood.Envelopes(
            dem.like(depth, np.nan), dem.like(np.where(wet, 1.0, np.nan), np.nan),
            dem.like(arr, np.nan), dem.like(np.where(wet, dem.data + depth, np.nan), np.nan),
            stats, [], {})

    shallow_xy = [(700.0, 0.0), (850.0, 100.0), (1000.0, -100.0)]  # 0.82, 0.57, 0.33 ft
    deep_xy = [(200.0, 0.0)]                                         # 1.64 ft

    def run(folder, **k):
        rec = impacts.Receptors()
        rec.add_points(shallow_xy + deep_xy, ids=["S1", "S2", "S3", "D1"])
        st = workflow.RunSettings(cell_size_m=20.0, initial_length_mi=1.0, half_width_mi=0.5,
                                  max_iterations=1, sim_time_hr=4.0,
                                  lisflood_exe=lisflood.BUILTIN_EXE, **k)
        st.receptors = rec
        dam = br.DamData(dam_id="T1", dam_height_ft=25.0, storage_top_acft=500.0,
                         top_of_dam_ft=1850.0, surface_area_top_ac=60.0)
        r = workflow.run_dam(dam, (0.0, 0.0), st, SyntheticTerrain(), npy_writer, folder)
        arr = np.load(os.path.splitext(r.final["arrival"])[0] + ".npy")
        g = json.load(open(os.path.splitext(r.final["arrival"])[0] + ".json"))
        at = [arr[int((g["y0"] - y) // g["dx"]), int((x - g["x0"]) // g["dx"])]
              for x, y in shallow_xy]
        return r, np.array(at), seen["par"]

    old = (lisflood.run, lisflood.read_results)
    lisflood.run, lisflood.read_results = fake_run, fake_read
    d = tempfile.mkdtemp(prefix="v3263_")
    try:
        r, at, par = run(os.path.join(d, "new"))
        r1, at1, par1 = run(os.path.join(d, "old"), arrival_depth_ft=1.0)
    finally:
        lisflood.run, lisflood.read_results = old
    assert "inittm_depth {:.4f}".format(0.1 * FT) in par, par
    assert "inittm_depth {:.4f}".format(1.0 * FT) in par1, par1
    # the arrival raster: the shallow cells have a time at the default, none at 1 ft
    assert np.isfinite(at).all(), at
    assert np.isnan(at1).all(), at1
    # receptors_flooded and the structure count: all four at the default, only the deep one
    assert r.summary["receptors_flooded"] == "4 structures", r.summary.get("receptors_flooded")
    assert r1.summary["receptors_flooded"] == "1 structure", r1.summary.get("receptors_flooded")
    rec = json.load(open(r.report_json))["receptors"]
    assert rec[impacts.KIND_STRUCTURE]["flooded"] == 4, rec
    rows = {row["id"]: row for row in csv.DictReader(open(r.receptor_csv))}
    assert set(rows) == {"S1", "S2", "S3", "D1"}, rows
    assert all(rows[i]["arrival_hr"] != "" for i in ("S1", "S2", "S3")), rows
    assert all(float(rows[i]["depth_ft"]) < 1.0 for i in ("S1", "S2", "S3")), rows
    rows1 = {row["id"] for row in csv.DictReader(open(r1.receptor_csv))}
    assert rows1 == {"D1"}, rows1
    # the run summary's flooded area at the arrival depth grows with it
    assert r.summary["flooded_acres_over_arrival_depth"] > \
        r1.summary["flooded_acres_over_arrival_depth"], (r.summary, r1.summary)
    shutil.rmtree(d, ignore_errors=True)
