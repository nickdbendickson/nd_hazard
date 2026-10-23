"""v2.34.3: the model-extent check knows an ND Dam Breach Runner result's own model domain.

A LISFLOOD-FP result whose rasters were reprojected (a UTM model grid written back to the
DEM's CRS) has no data on every raster edge; the raster-edge check then saw 0 cells and
called the flood contained though the edges were wet. boundary.runner_domain reads the run
folder next to the depth raster so a LISFLOOD grid edge without data counts as dry and an
ANUGA mesh boundary is used directly; when the domain is not known and the outer ring is all
no-data, the check says the extent was NOT checked instead of "does not reach the edge".

Runs without QGIS: runner_domain reads files and describe is pure. tests/fake_qgis.py stands
in for PyQGIS.

    python3 -m ND_Dam_Hazard_Helper.tests.run_tests v2343
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
from ND_Dam_Hazard_Helper.tests.registry import test  # noqa: E402
from ND_Dam_Hazard_Helper import boundary as BND  # noqa: E402


def _runner_folder(final_model, ncols=100, nrows=80, depth_name="Depth (Max).tif",
                   mesh=None):
    """A dam folder with a Runner _breach_model next to a depth raster; returns the depth path."""
    d = tempfile.mkdtemp(prefix="rdom_")
    depth = os.path.join(d, depth_name)
    open(depth, "w").close()
    model = os.path.join(d, "_breach_model")
    os.makedirs(os.path.join(model, "lisflood"))
    with open(os.path.join(model, "run_report.json"), "w", encoding="utf-8") as fh:
        json.dump({"summary": {"final_model": final_model},
                   "outputs": {"final": {"depth": depth}}}, fh)
    with open(os.path.join(model, "lisflood", "run.dem.asc"), "w", encoding="utf-8") as fh:
        fh.write("ncols {}\nnrows {}\nxllcorner 0\nyllcorner 0\ncellsize 20\n"
                 "NODATA_value -9999\n".format(ncols, nrows))
    if mesh is not None:
        os.makedirs(os.path.join(model, "anuga"))
        with open(os.path.join(model, "anuga", "mesh_boundary.csv"), "w", encoding="utf-8") as fh:
            fh.write("x,y\n")
            for x, y in mesh:
                fh.write("{},{}\n".format(x, y))
    return depth


@test
def v2343_runner_domain_lisflood_grid():
    depth = _runner_folder("LISFLOOD-FP", ncols=100, nrows=80)
    assert BND.runner_domain(depth, 100, 80) == ("grid", None)
    # a raster of a different size than the LISFLOOD grid is not the model grid
    assert BND.runner_domain(depth, 120, 80) is None
    # size unknown: trust the run report
    assert BND.runner_domain(depth, None, None) == ("grid", None)


@test
def v2343_runner_domain_anuga_mesh():
    ring = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]
    depth = _runner_folder("ANUGA", mesh=ring)
    dom = BND.runner_domain(depth, 100, 80)
    assert dom is not None and dom[0] == "mesh" and len(dom[1]) == 4


@test
def v2343_runner_domain_not_a_runner_result():
    # no _breach_model next to it, and a plain path, give nothing
    assert BND.runner_domain("/no/such/Depth (Max).tif") is None
    assert BND.runner_domain(None) is None
    # the report's final depth names another raster than the one asked about
    depth = _runner_folder("LISFLOOD-FP")
    other = os.path.join(os.path.dirname(depth), "Depth (Max 2).tif")
    assert BND.runner_domain(other, 100, 80) is None


@test
def v2343_describe_unchecked_vs_dry():
    # the outer ring is all no-data and the domain is unknown: NOT checked, and flagged
    res = {"method": "edge of the depth raster", "checked": 0, "wet": 0, "per_side": {},
           "baseline": False, "unchecked": True, "flagged": True}
    line = BND.describe(res)
    assert "NOT checked" in line and "0 edge cells" in line, line
    assert BND.judgment_text(res) == line            # no "extend the model" tail when unchecked

    # a LISFLOOD grid with dry edges is genuinely contained, not "unchecked"
    dry = {"method": "edge of the depth raster (the LISFLOOD-FP model grid)", "checked": 6496,
           "wet": 0, "per_side": {}, "baseline": False}
    line2 = BND.describe(dry)
    assert "does not reach" in line2 and "NOT checked" not in line2, line2
    # the LISFLOOD-grid wording does not add the "2D flow area may end inside the raster" note
    assert "2D flow area may end inside" not in line2, line2
