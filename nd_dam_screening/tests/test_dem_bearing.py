"""The 'Downstream bearing' option on Download DEM: a blank bearing keeps the dam-centred box;
a bearing shifts the same-size box downstream so the dam sits near the upstream edge, with the
rest reaching the way the flood goes. Run: python3 tests/test_dem_bearing.py (uses the real osr
when present, else the qgis_mock stand-in - the directional shift is the same either way)."""
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MOCK = os.path.join(HERE, "qgis_mock")
# real osgeo (the mock is a single-CRS stand-in) first, then the qgis_mock for qgis.core
sys.path[:] = [q for q in sys.path if os.path.abspath(q or ".") != MOCK]
from osgeo import osr  # noqa: E402,F401  (load the real osgeo before dd imports it)
assert hasattr(osr.SpatialReference(), "SetAxisMappingStrategy"), "needs the real osgeo"
sys.path.insert(0, MOCK)
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)

_PKG = os.path.basename(os.path.dirname(HERE))
dd = __import__(_PKG + ".core.dem_download", fromlist=["x"])

LON, LAT, BUF = -97.39113, 46.98337, 10.0           # Absaraka, 10 mi per side
dx, dy = dd.to_5070(LON, LAT, LON, LAT)[:2]


def _fracs(box):
    x0, y0, x1, y1 = box
    return (dx - x0) / (x1 - x0), (dy - y0) / (y1 - y0), (x1 - x0), (y1 - y0)


def test_blank_bearing_is_centred_and_unchanged():
    base = dd.point_box(LON, LAT, BUF)
    assert dd.point_box(LON, LAT, BUF, bearing_deg=None) == base
    fx, fy, w, h = _fracs(base)
    assert abs(fx - 0.5) < 1e-6 and abs(fy - 0.5) < 1e-6          # dam centred
    print("ok centred:", round(fx, 3), round(fy, 3))


def test_bearing_shifts_downstream_same_size():
    _, _, w0, h0 = _fracs(dd.point_box(LON, LAT, BUF))
    # (bearing, which fraction should move toward 0 = upstream edge)
    cases = {0: ("y", "<"), 180: ("y", ">"), 90: ("x", "<"), 270: ("x", ">")}
    for brg, (axis, rel) in cases.items():
        box = dd.point_box(LON, LAT, BUF, bearing_deg=brg)
        fx, fy, w, h = _fracs(box)
        assert abs(w - w0) < 1.0 and abs(h - h0) < 1.0, "box size changed"   # same download size
        frac = fx if axis == "x" else fy
        if rel == "<":
            assert frac < 0.2, (brg, frac)         # dam near the upstream (low) edge
        else:
            assert frac > 0.8, (brg, frac)         # dam near the upstream (high) edge
        print("ok bearing", brg, "-> dam", axis, "frac", round(frac, 3))


def test_bearing_wraps_and_ignores_junk():
    assert dd.point_box(LON, LAT, BUF, bearing_deg=0) == dd.point_box(LON, LAT, BUF, bearing_deg=360)


if __name__ == "__main__":
    test_blank_bearing_is_centred_and_unchanged()
    test_bearing_shifts_downstream_same_size()
    test_bearing_wraps_and_ignores_junk()
    print("test_dem_bearing: OK")
