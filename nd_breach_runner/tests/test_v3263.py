"""3.26.3: the arrival depth (ARRIVAL_FT) defaults to 0.1 ft, the ND hazard wet depth, not 1 ft.

Same change as DWR_Plugins Runner 3.10.45 (#131). One constant, core/units.ARRIVAL_DEPTH_FT,
feeds every default: RunSettings, hecras.read_mesh / terrain_depth / reexport_results, Breach
products from rasters, the LISFLOOD-FP and ANUGA settings, the CLI JSON template and
'hecras-reexport --arrival-depth-ft', and the Run a dam, HEC-RAS import and Breach products tools.
A structure flooded 0.1-1 ft deep now gets an arrival time (it had none at 1 ft)."""
import inspect
import os

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
