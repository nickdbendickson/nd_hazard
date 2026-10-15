"""Tests of 3.6.0: inventory inlet / outlet elevations (NDDOT centerline pipes through ND Dam
Hazard's culvert tool, or any structures layer with invert fields) are checked against the
LiDAR bed before the models use them (core/structures.reconcile_inverts, the S_INVERT_TOL_FT
parameter). Synthetic terrain, no QGIS, no model run."""
import configparser
import os
import types

import numpy as np

from nd_breach_runner.tests.registry import test

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FT = 0.3048


def _road_over_channel(x, y):
    """A channel along x = 0 flowing south (bed 100 m, falling 2 mm/m), a 5 m road embankment
    along y = 0 (crest 105 m, 1:2 slopes) with the channel's slot filled - as LiDAR shows it."""
    bed = 100.0 + 0.002 * y + 0.02 * np.abs(x)
    emb = 105.0 - np.maximum(0.0, np.abs(y) - 6.0) * 0.5
    return np.maximum(bed, np.where(np.abs(y) < 16.0, emb, -1e9))


class _Terrain:
    def __init__(self, fn=_road_over_channel, nodata_at=None):
        self.fn, self.nodata_at, self.reads = fn, nodata_at, []

    def read(self, bounds, cell):
        from nd_breach_runner.core.grid import Grid
        xmin, ymin, xmax, ymax = bounds
        nx, ny = int(round((xmax - xmin) / cell)), int(round((ymax - ymin) / cell))
        xs = xmin + (np.arange(nx) + 0.5) * cell
        ys = ymax - (np.arange(ny) + 0.5) * cell
        X, Y = np.meshgrid(xs, ys)
        z = self.fn(X, Y)
        if self.nodata_at is not None:
            z = np.where(np.hypot(X - self.nodata_at[0], Y - self.nodata_at[1]) < 2.5, -9999.0, z)
        self.reads.append((bounds, cell))
        return Grid(z, xmin, ymax, cell)


def _culvert(up, dn, kind="culvert", rise=1.0, name="C"):
    from nd_breach_runner.core import structures as S
    return S.Structure(kind, np.array([[0.0, 20.0], [0.0, -20.0]]), width_m=1.0, height_m=rise,
                       invert_up_m=up, invert_dn_m=dn, name=name)


@test
def v360_inventory_inverts_within_tolerance_are_used_as_given():
    from nd_breach_runner.core import structures as S
    tol = 3.0 * FT
    s = _culvert(100.3, 99.8)                          # bed about 100.04 / 99.96
    notes = S.reconcile_inverts([s], _Terrain().read, tol, 1.0)
    assert s.invert_up_m == 100.3 and s.invert_dn_m == 99.8, (s.invert_up_m, s.invert_dn_m)
    assert s.invert_inventory == (100.3, 99.8)
    assert len(notes) == 1 and "inventory inverts used" in notes[0] and "within 3.0 ft" in notes[0], notes
    assert "+0.8 ft" in notes[0] and "-0.5 ft" in notes[0], notes
    assert "rise does not fit" not in notes[0]
    # a structure without inverts is left alone, silently
    s2 = _culvert(None, None)
    assert S.reconcile_inverts([s2], _Terrain().read, tol, 1.0) == []
    assert s2.invert_up_m is None and s2.invert_inventory is None and s2.invert_note == ""


@test
def v360_a_datum_shift_is_corrected_and_disagreement_falls_back_to_lidar():
    from nd_breach_runner.core import structures as S
    tol = 3.0 * FT
    s = _culvert(102.04, 101.96)                       # both ends 2.0 m above the bed
    notes = S.reconcile_inverts([s], _Terrain().read, tol, 1.0)
    assert abs(s.invert_up_m - 100.04) < 0.05 and abs(s.invert_dn_m - 99.96) < 0.05, (s.invert_up_m, s.invert_dn_m)
    assert abs((s.invert_up_m - s.invert_dn_m) - 0.08) < 1e-9        # the slope survives
    assert "datum shift" in notes[0] and "+6.5 ft" in notes[0], notes
    assert s.invert_inventory == (102.04, 101.96)
    s = _culvert(103.0, 99.0)                          # +3 m and -1 m: not one shift
    notes = S.reconcile_inverts([s], _Terrain().read, tol, 1.0)
    assert s.invert_up_m is None and s.invert_dn_m is None
    assert "disagree with the LiDAR" in notes[0] and "LiDAR inverts used" in notes[0], notes
    assert s.invert_inventory == (103.0, 99.0)
    # a tolerance of 0 accepts nothing but a datum shift
    s = _culvert(100.3, 99.8)
    S.reconcile_inverts([s], _Terrain().read, 0.0, 1.0)
    assert s.invert_up_m is None and "disagree" in s.invert_note


@test
def v360_one_end_only_cover_check_nodata_and_errors():
    from nd_breach_runner.core import structures as S
    tol = 3.0 * FT
    s = _culvert(100.3, None)                          # the missing end: bed + the same offset
    S.reconcile_inverts([s], _Terrain().read, tol, 1.0)
    assert s.invert_up_m == 100.3 and abs(s.invert_dn_m - (99.96 + 0.26)) < 0.05, s.invert_dn_m
    assert "n/a" in s.invert_note
    # a 10 m rise cannot sit under a 5 m embankment: noted, the inverts still used
    s = _culvert(100.3, 99.8, rise=10.0)
    S.reconcile_inverts([s], _Terrain().read, tol, 1.0)
    assert s.invert_up_m == 100.3 and "rise does not fit under the road crown" in s.invert_note
    assert "32.8 ft rise" in s.invert_note and "cover -" in s.invert_note, s.invert_note
    # bridges get no cover note (no roof)
    b = _culvert(100.3, 99.8, kind="bridge", rise=10.0)
    S.reconcile_inverts([b], _Terrain().read, tol, 1.0)
    assert "rise does not fit" not in b.invert_note and "inventory inverts used" in b.invert_note
    # no-data cells (-9999) near an end are not "the lowest ground"
    s = _culvert(100.3, 99.8)
    S.reconcile_inverts([s], _Terrain(nodata_at=(0.0, 20.0)).read, tol, 1.0)
    assert s.invert_up_m == 100.3 and "inventory inverts used" in s.invert_note, s.invert_note
    # no DEM at all: kept, unchecked, and said so
    def boom(bounds, cell):
        raise IOError("no raster here")
    s = _culvert(100.3, 99.8)
    notes = S.reconcile_inverts([s], boom, tol, 1.0)
    assert s.invert_up_m == 100.3 and "kept unchecked" in notes[0] and "no raster here" in notes[0]
    # the report line carries the note
    rows = S.describe([s])
    assert "kept unchecked" in rows[0] and rows[0].startswith("C (culvert")


@test
def v360_lidar_bed_is_the_lowest_finite_ground_within_the_radius():
    from nd_breach_runner.core import structures as S
    g = _Terrain().read((-10, -30, 10, 30), 1.0)
    assert abs(S.lidar_bed(g, (0.0, 20.0), 2.0) - (100.0 + 0.002 * 19.5 + 0.01)) < 0.02
    assert S.lidar_bed(g, (500.0, 500.0), 2.0) is None
    g2 = _Terrain(nodata_at=(0.0, 20.0)).read((-10, -30, 10, 30), 1.0)
    bed = S.lidar_bed(g2, (0.0, 20.0), 3.0)
    assert bed is not None and bed > 99.0, bed


@test
def v360_run_dam_algorithm_has_the_tolerance_parameter_and_passes_it_on():
    from nd_breach_runner.tests import fake_qgis
    fake_qgis.install()
    import importlib
    from nd_breach_runner import algorithms as A
    importlib.reload(A)
    from nd_breach_runner.tests.fake_qgis import FakeGeometry, FakeFeature, FakeSource
    alg = A.RunDamAlgorithm()
    alg.initAlgorithm()
    pdef = {p.name(): p for p in alg.parameterDefinitions()}
    assert "S_INVERT_TOL_FT" in pdef and pdef["S_INVERT_TOL_FT"].defaultValue() == 3.0
    desc = pdef["S_INVERT_TOL_FT"].description
    assert "YOUR value" in (desc() if callable(desc) else desc)
    src = FakeSource([FakeFeature(FakeGeometry("line", [[(1530.0, 20.0), (1530.0, -20.0)]]),
                                  kind="culvert", span_ft=4.0, rise_ft=4.0, shape="pipe",
                                  invert_up=1850.5, invert_dn=1849.0, name="P1")],
                     names=["kind", "span_ft", "rise_ft", "shape", "invert_up", "invert_dn", "name"])
    st = types.SimpleNamespace(max_extent_mi=45.0)
    alg.apply_features(st, {"STRUCTURES": src, "S_INVERT_TOL_FT": 5.0}, fake_qgis.QgsProcessingContext(),
                       fake_qgis.QgsCoordinateReferenceSystem("EPSG:5070"), (0.0, 0.0))
    assert abs(st.invert_tol_m - 5.0 * FT) < 1e-9
    s = st.structures[0]
    assert s.name == "P1" and abs(s.invert_up_m - 1850.5 * FT) < 1e-9 and abs(s.invert_dn_m - 1849.0 * FT) < 1e-9
    assert s.shape == "pipe" and abs(s.width_m - 4.0 * FT) < 1e-9 and s.invert_inventory is None
    # left out of the parameters (an old model): the starting value
    st2 = types.SimpleNamespace(max_extent_mi=45.0)
    alg.apply_features(st2, {"STRUCTURES": src}, fake_qgis.QgsProcessingContext(),
                       fake_qgis.QgsCoordinateReferenceSystem("EPSG:5070"), (0.0, 0.0))
    assert abs(st2.invert_tol_m - 3.0 * FT) < 1e-9
    # RunSettings carries the tolerance for the workflow
    from nd_breach_runner.core import workflow
    rs = workflow.RunSettings()
    assert abs(rs.invert_tol_m - 3.0 * FT) < 1e-9


@test
def v360_version():
    from nd_breach_runner.core import workflow
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(os.path.join(PLUGIN, "metadata.txt"), encoding="utf-8")
    assert cp.get("general", "version") == workflow.VERSION == workflow.metadata_version()
    text = open(os.path.join(PLUGIN, "README.md"), encoding="utf-8").read() if os.path.exists(
        os.path.join(PLUGIN, "README.md")) else open(os.path.join(PLUGIN, "README.txt"), encoding="utf-8").read()
    assert "S_INVERT_TOL_FT" in text or "inventory" in text.lower()
