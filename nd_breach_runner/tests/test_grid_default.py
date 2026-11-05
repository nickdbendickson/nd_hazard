"""3.27.8: terrain-aware grid default on flat section-road plains.

On flat plains (low DEM relief and gentle slope) the Runner starts at 10 m, with
5 m offered when the engineer can afford the run time. Coarser cells (20-30 m)
over-spread: DWR-2801 vs DSS-WISE wet at >=0.5 ft scored CSI 0.36 / 0.43 / 0.57
at 20 / 10 / 5 m (Claude pack 2026-11-03; Docs & QA compare; Nick "Do that").

Pure unit tests of elev_stats / classify_terrain / default selection / warning.
No DEM files, no model runs.
"""
import math

import numpy as np

from nd_breach_runner.core import grid_default as gd
from nd_breach_runner.tests.registry import test


def _flat_dem(n=40, cell=30.0, base=300.0, ripple=0.3):
    """Gentle prairie: ~few metres of relief, mean slope well under 1%."""
    yy, xx = np.mgrid[0:n, 0:n]
    z = base + ripple * np.sin(xx / 8.0) + 0.15 * np.sin(yy / 11.0)
    # tiny regional tilt (~0.2 m over the window)
    z = z + 0.2 * (xx + yy) / (2.0 * (n - 1))
    return z, cell


def _steep_dem(n=40, cell=30.0, base=500.0):
    """Canyon / valley: tens of metres of relief and steep mean slope."""
    yy, xx = np.mgrid[0:n, 0:n]
    # ~80 m drop across the window -> slope ~ 80/(39*30) ~ 0.068
    z = base - 80.0 * (xx + yy) / (2.0 * (n - 1))
    z = z + 2.0 * np.sin(xx / 3.0)
    return z, cell


@test
def elev_stats_flat_and_steep():
    z, cell = _flat_dem()
    s = gd.elev_stats(z, cell)
    assert s["n"] == z.size
    assert s["relief_m"] < gd.FLAT_PLAIN_RELIEF_M
    assert s["mean_slope"] < gd.FLAT_PLAIN_SLOPE
    assert math.isfinite(s["p5"]) and math.isfinite(s["p95"])

    z2, cell2 = _steep_dem()
    s2 = gd.elev_stats(z2, cell2)
    assert s2["n"] == z2.size
    assert s2["relief_m"] > gd.FLAT_PLAIN_RELIEF_M
    assert s2["mean_slope"] > gd.FLAT_PLAIN_SLOPE


@test
def elev_stats_empty_and_nan():
    s = gd.elev_stats(np.full((2, 2), np.nan), 30.0)
    assert s["n"] == 0 and math.isnan(s["relief_m"])
    s2 = gd.elev_stats(np.array([1.0, 2.0, 3.0]), 30.0)  # not 2D
    assert s2["n"] == 0
    s3 = gd.elev_stats(np.ones((3, 3)), 0.0)             # bad cell
    assert s3["n"] == 0


@test
def classify_flat_other_unknown():
    z, cell = _flat_dem()
    assert gd.classify_terrain(gd.elev_stats(z, cell)) == gd.KIND_FLAT_SECTION_PLAIN
    z2, cell2 = _steep_dem()
    assert gd.classify_terrain(gd.elev_stats(z2, cell2)) == gd.KIND_OTHER
    assert gd.classify_terrain({"n": 0}) == gd.KIND_UNKNOWN
    assert gd.classify_terrain({"n": 100, "relief_m": float("nan"),
                                "mean_slope": 0.001}) == gd.KIND_UNKNOWN
    # relief alone is not enough: steep mean slope keeps it "other"
    assert gd.classify_terrain({"n": 100, "relief_m": 10.0,
                                "mean_slope": 0.05}) == gd.KIND_OTHER
    # slope alone is not enough: big relief keeps it "other"
    assert gd.classify_terrain({"n": 100, "relief_m": 80.0,
                                "mean_slope": 0.002}) == gd.KIND_OTHER


@test
def default_and_fine_cells():
    assert gd.default_cell_m(gd.KIND_FLAT_SECTION_PLAIN) == 10.0
    assert gd.fine_cell_m(gd.KIND_FLAT_SECTION_PLAIN) == 5.0
    assert gd.default_cell_m(gd.KIND_OTHER) == 10.0
    assert gd.fine_cell_m(gd.KIND_OTHER) is None
    assert gd.default_cell_m(gd.KIND_UNKNOWN) == 10.0
    assert gd.fine_cell_m(gd.KIND_UNKNOWN) is None
    hint = gd.cell_hint(gd.KIND_FLAT_SECTION_PLAIN)
    assert "10" in hint and "5 m (finer, slower)" in hint


@test
def coarse_cell_warning_only_on_flat_coarse():
    # flat + 20 m -> warn (the 2801 finding)
    w = gd.coarse_cell_warning(20.0, gd.KIND_FLAT_SECTION_PLAIN)
    assert w and "0.36" in w and "0.43" in w and "0.57" in w
    assert "5 m (finer, slower)" in w
    # flat + 10 m / 5 m -> no warn
    assert gd.coarse_cell_warning(10.0, gd.KIND_FLAT_SECTION_PLAIN) is None
    assert gd.coarse_cell_warning(5.0, gd.KIND_FLAT_SECTION_PLAIN) is None
    # other / unknown never warn, even at 30 m
    assert gd.coarse_cell_warning(30.0, gd.KIND_OTHER) is None
    assert gd.coarse_cell_warning(30.0, gd.KIND_UNKNOWN) is None


@test
def apply_default_only_when_untouched_and_coarse():
    # untouched + flat + 30 m nickreview-style -> pull down to 10
    cell, changed = gd.apply_default(30.0, gd.KIND_FLAT_SECTION_PLAIN, untouched=True)
    assert changed and cell == 10.0
    # already at 10 -> leave
    cell, changed = gd.apply_default(10.0, gd.KIND_FLAT_SECTION_PLAIN, untouched=True)
    assert not changed and cell == 10.0
    # engineer picked 5 m -> leave (never auto-pick 5, and never coarsen)
    cell, changed = gd.apply_default(5.0, gd.KIND_FLAT_SECTION_PLAIN, untouched=False)
    assert not changed and cell == 5.0
    # touched (handoff / user edit) even if coarse -> leave
    cell, changed = gd.apply_default(20.0, gd.KIND_FLAT_SECTION_PLAIN, untouched=False)
    assert not changed and cell == 20.0
    # other terrain never auto-changes
    cell, changed = gd.apply_default(30.0, gd.KIND_OTHER, untouched=True)
    assert not changed and cell == 30.0


@test
def from_array_and_report_entry():
    z, cell = _flat_dem()
    info = gd.from_array(z, cell)
    assert info["kind"] == gd.KIND_FLAT_SECTION_PLAIN
    assert info["default_cell_m"] == 10.0
    assert info["fine_cell_m"] == 5.0
    assert "finer, slower" in info["hint"]

    # report at 20 m flags coarse_warning
    entry = gd.report_entry(info, 20.0)
    assert entry["kind"] == gd.KIND_FLAT_SECTION_PLAIN
    assert entry["coarse_warning"] is True
    assert entry["default_cell_m"] == 10.0 and entry["fine_cell_m"] == 5.0
    assert entry["relief_m"] is not None and entry["mean_slope"] is not None

    entry10 = gd.report_entry(info, 10.0)
    assert entry10["coarse_warning"] is False

    # steep -> other, no fine option, no coarse warning even at 30 m
    z2, cell2 = _steep_dem()
    info2 = gd.from_array(z2, cell2)
    assert info2["kind"] == gd.KIND_OTHER
    assert info2["fine_cell_m"] is None
    assert gd.report_entry(info2, 30.0)["coarse_warning"] is False


@test
def from_terrain_stub_on_bad_src():
    info = gd.from_terrain(None, (0.0, 0.0))
    assert info["kind"] == gd.KIND_UNKNOWN
    assert info["fine_cell_m"] is None

    class Boom:
        def read(self, *a, **k):
            raise RuntimeError("no dem")

    info2 = gd.from_terrain(Boom(), (100.0, 200.0))
    assert info2["kind"] == gd.KIND_UNKNOWN

    class FlatSrc:
        def read(self, bounds, cell):
            z, _ = _flat_dem(n=50, cell=cell)
            class G:
                data = z
            return G()

    info3 = gd.from_terrain(FlatSrc(), (500000.0, 5200000.0))
    assert info3["kind"] == gd.KIND_FLAT_SECTION_PLAIN
    assert info3["default_cell_m"] == 10.0
