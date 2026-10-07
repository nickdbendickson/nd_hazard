"""v0.12: ponds and dugouts from a DEM (core/ponds.py) on synthetic terrain with known answers.

Run: python3 tests/test_ponds.py   (no QGIS needed)
"""
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core import ponds as pd  # noqa: E402
from core import storage as st  # noqa: E402

FT = pd.FT
AF = pd.AF_M3
CELL = 2.0
N = 200


def grid():
    yy, xx = np.mgrid[0:N, 0:N]
    return (xx - N / 2) * CELL, (N / 2 - yy) * CELL          # east, north (m from the centre)


def embankment_pond(flatten=True):
    """A draw that rises to the north; a 6 m high dam across it at north = 0 (crest 102 m, 3H:1V
    faces, 6 m wide crest); natural ground south of the dam at 96 m; pool 100 m north of the dam."""
    e, n = grid()
    g = np.where(n < 0, 96.0, 96.0 + 0.05 * n)                # valley floor, rising north
    g = g + 0.15 * np.maximum(np.abs(e) - 20.0, 0.0)          # valley walls
    prof = 102.0 - np.maximum(np.abs(n) - 3.0, 0.0) / 3.0     # the embankment
    dam = np.where(np.abs(e) <= 60.0, prof, -1e9)
    z = np.maximum(g, dam)
    below = z < 100.0
    from scipy import ndimage as ndi
    lab, _ = ndi.label(below & (n > 0))
    water = lab == lab[int(N / 2 - 30), N // 2]               # the pond north of the dam
    dem = z.copy()
    if flatten:
        dem[water] = 100.0
    else:                                                     # the bed shows: a bowl under the pool
        dist = ndi.distance_transform_edt(water) * CELL
        dem[water] = (100.0 - np.minimum(dist * 0.1, 2.5))[water]
    return dem, water


def dugout(berm=0.0, slope=0.5, depth=3.0):
    """A round pit, 15 m radius at the water, banks rising `slope` m per m to the natural ground at
    100 m; pool at 100 - depth; optionally a spoil rim `berm` m high at 25-31 m from the centre."""
    e, n = grid()
    r = np.hypot(e, n)
    pool = 100.0 - depth
    z = np.where(r < 15.0, pool, np.minimum(100.0, pool + (r - 15.0) * slope))
    z = z + np.where((r >= 25.0) & (r <= 31.0), berm, 0.0)
    water = r < 15.0
    z[water] = pool
    return z, water


# ---------------------------------------------------------------- embankment pond
dem, water = embankment_pond()
res = pd.analyze_pond(dem, water, CELL)
print("embankment:", res.pond_type, "pool %.2f crest %.2f ground %.2f arc %.0f az %s above-grade %.0f m3" % (
    res.pool_m, res.crest_m, res.ground_m, res.arc_deg, res.flow_az, res.above_grade_m3))
assert res.pond_type == pd.T_EMBANKMENT
assert abs(res.pool_m - 100.0) < 1e-9 and "flat" in res.pool_method
assert abs(res.crest_m - 102.0) < 0.35, res.crest_m              # lowest rim of the impounding sectors
assert abs(res.ground_m - 96.0) < 0.35, res.ground_m             # natural ground south of the dam
assert abs(res.height_m - 6.0) < 0.5 and abs(res.head_m - 4.0) < 0.5
assert res.arc_deg >= 45.0 and not res.no_flood and res.no_flood_reason == ""
assert res.breach_rc is not None and res.flow_az is not None
d = abs(((res.flow_az - 180.0) + 180.0) % 360.0 - 180.0)
assert d < 25.0, res.flow_az                                     # the water leaves to the south
assert res.crest_len_m and res.crest_len_m > 20.0
# the breach cell lies on the dam (south of the pond, near north = 0..10 m)
br, bc = res.breach_rc
assert abs((N / 2 - br) * CELL) < 14.0, br
# storage parts: total = below + above-pool; above-grade lies between above-pool and the total
assert abs(res.total_m3 - (res.below_pool_m3 + res.above_pool_m3)) < 1e-6
assert res.above_pool_m3 - 1e-6 <= res.above_grade_m3 <= res.total_m3 + 1e-6
# the same below-pool volume as tool 3's proxy
b3, _dm = st.below_pool_volume(dem, water, CELL, res.shore_slope_deg, pd.PondParams().max_depth_m)
assert abs(b3 - res.below_pool_m3) < 1e-6
# the curve runs pool -> crest, rising
e = [x for x, _ in res.curve]
v = [y for _, y in res.curve]
assert abs(e[0] - res.pool_m) < 1e-9 and abs(e[-1] - res.crest_m) < 1e-9
assert all(b >= a - 1e-9 for a, b in zip(v, v[1:])) and abs(v[0] - res.below_pool_m3) < 1e-6
assert res.confidence in ("medium", "low")
print("  embankment OK")

# a taller dam holds more above grade
dem2, water2 = embankment_pond()
# ---------------------------------------------------------------- unflattened water
dem_b, water_b = embankment_pond(flatten=False)
rb = pd.analyze_pond(dem_b, water_b, CELL)
assert "shoreline" in rb.pool_method and rb.confidence == "low"
assert abs(rb.pool_m - 100.0) < 0.8, rb.pool_m
assert rb.pond_type == pd.T_EMBANKMENT
print("  unflattened water OK (pool %.2f, %s)" % (rb.pool_m, rb.confidence))

# ---------------------------------------------------------------- dugouts
dem, water = dugout()
rd = pd.analyze_pond(dem, water, CELL)
print("dugout:", rd.pond_type, rd.no_flood, rd.no_flood_reason, rd.notes)
assert rd.pond_type == pd.T_DUGOUT                               # 26 deg banks
assert rd.no_flood and "natural ground" in rd.no_flood_reason
assert rd.above_grade_m3 == 0.0 and rd.breach_rc is None and rd.flow_az is None
assert rd.head_m < 0                                             # water is BELOW the ground
assert rd.below_pool_m3 > 0 and rd.total_m3 >= rd.below_pool_m3  # still has a volume
assert any("steep banks" in n for n in rd.notes)

dem, water = dugout(berm=1.0)                                    # spoil rim 1 m high
rs = pd.analyze_pond(dem, water, CELL)
print("dugout with spoil rim:", rs.pond_type, "%.0f m3 above grade, height %.2f m" % (rs.above_grade_m3, rs.height_m))
assert rs.pond_type == pd.T_DUGOUT and not rs.no_flood
assert 1500.0 < rs.above_grade_m3 < 2400.0, rs.above_grade_m3    # ~ pi * 25^2 * 1 m
assert abs(rs.height_m - 1.0) < 0.2 and rs.breach_rc is not None
assert rs.confidence in ("medium", "low")

# a raised rim lower than the tool's minimum breach height is "no breach flood"
dem, water = dugout(berm=0.15)
rl = pd.analyze_pond(dem, water, CELL)
assert rl.no_flood, (rl.height_m, rl.above_grade_m3)

# the limit is the engineer's: a huge limit turns the spoil-rim dugout into "no flood"
dem, water = dugout(berm=1.0)
big = pd.analyze_pond(dem, water, CELL, pd.PondParams(release_min_m3=5000.0))
assert big.no_flood and "ac-ft" in big.no_flood_reason
print("  dugouts OK")

# ---------------------------------------------------------------- on a sloping field (3 %, falling south)
def tilt(dem, water, slope):
    e, n = grid()
    out = dem + slope * n
    out[water] = dem[water]                                     # the water surface stays level
    return out


dem0, water0 = dugout()
rd3 = pd.analyze_pond(tilt(dem0, water0, 0.03), water0, CELL)
print("dugout on a slope:", rd3.pond_type, rd3.no_flood, "%.2f ft above grade" % (rd3.height_m / FT))
assert rd3.pond_type == pd.T_DUGOUT and rd3.no_flood           # no embankment: the ground does not fall steeply
dem0, water0 = dugout(berm=1.0)
rs3 = pd.analyze_pond(tilt(dem0, water0, 0.03), water0, CELL)
assert rs3.pond_type == pd.T_DUGOUT and not rs3.no_flood and 0.5 < rs3.height_m < 1.6, rs3.height_m


def ring_dike_pond(slope=0.03):
    """A rectangular pond (40 x 30 m, pool 100) inside a dike (crest 102 m, 3H:1V) on a field that
    falls south at `slope`."""
    e, n = grid()
    dx = np.maximum(np.abs(e) - 20.0, 0.0)
    dy = np.maximum(np.abs(n) - 15.0, 0.0)
    d = np.hypot(dx, dy)                                        # distance outside the rectangle
    rect = (dx == 0) & (dy == 0)
    ring = 102.0 - np.maximum(d - 3.0, 0.0) / 3.0
    z = np.maximum(100.0 + slope * n, ring)
    z[rect] = 100.0
    return z, rect


dem_r, water_r = ring_dike_pond()
rr_ = pd.analyze_pond(dem_r, water_r, CELL)
print("ring dike on a slope:", rr_.pond_type, "crest %.2f ground %.2f head %.2f ft az %s" % (
    rr_.crest_m, rr_.ground_m, rr_.head_m / FT, rr_.flow_az))
assert rr_.pond_type == pd.T_EMBANKMENT and not rr_.no_flood
assert abs(rr_.crest_m - 102.0) < 0.4 and 98.6 < rr_.ground_m < 99.6, (rr_.crest_m, rr_.ground_m)
assert 140.0 < rr_.flow_az < 220.0, rr_.flow_az                 # downhill, to the south
assert rr_.above_grade_m3 > 0 and rr_.height_m > 2.0
print("  sloping field OK")

# ---------------------------------------------------------------- natural depression
dem, water = dugout(slope=0.05, depth=1.0)                       # 3 deg banks, no rim
rn = pd.analyze_pond(dem, water, CELL)
print("depression:", rn.pond_type, rn.bank_slope_deg)
assert rn.pond_type == pd.T_DEPRESSION and rn.no_flood
# the layer's own word 'excavated' turns it into a dugout
rh = pd.analyze_pond(dem, water, CELL, excavated_hint=True)
assert rh.pond_type == pd.T_DUGOUT and any("layer says excavated" in n for n in rh.notes)
# an excavated-by-the-layer water body that the LiDAR shows holding water: stays an embankment pond
dem_e, water_e = embankment_pond()
rx = pd.analyze_pond(dem_e, water_e, CELL, excavated_hint=True)
assert rx.pond_type == pd.T_EMBANKMENT and any("spoil dike" in n for n in rx.notes)
print("  depression OK")

# ---------------------------------------------------------------- other water bodies are not "ground"
dem, water = dugout()
dem2 = dem.copy()
other = np.zeros_like(water)
e, n = grid()
other[(np.hypot(e - 44.2, n - 8.8) < 12.0)] = True               # a neighbouring pond filling one whole sector
dem2[other] = 90.0                                               # ... whose surface is far lower
bad = pd.analyze_pond(dem2, water, CELL)                         # not told: its cells look like a drop
good = pd.analyze_pond(dem2, water, CELL, other=other)
assert bad.ground_m < 95.0 and abs(good.ground_m - 100.0) < 0.35, (bad.ground_m, good.ground_m)
assert good.no_flood
print("  neighbouring water OK")

# ---------------------------------------------------------------- bad input is a plain ValueError
dem, water = dugout()
for bad_args in ((np.full_like(dem, np.nan), water),):
    try:
        pd.analyze_pond(bad_args[0], bad_args[1], CELL)
        raise AssertionError("expected ValueError")
    except ValueError as ex:
        assert "valid terrain" in str(ex) or "fewer than 4" in str(ex), ex
dem_half = dem.copy()
dem_half[:, : N // 2 + 3] = np.nan; dem_half[N // 2 + 3:, :] = np.nan; dem_half[: N // 2 - 5, :] = np.nan                              # only a strip east of the water has data
try:
    pd.analyze_pond(dem_half, water, CELL)
    raise AssertionError("expected ValueError")
except ValueError as ex:
    assert "compass sectors" in str(ex), ex
try:
    pd.analyze_pond(dem, np.zeros_like(water), CELL)
    raise AssertionError("expected ValueError")
except ValueError:
    pass
dem3 = dem.copy()
dem3[water] = -9999.0                                            # undeclared fill under the water
try:
    pd.analyze_pond(dem3, water, CELL)
    raise AssertionError("expected ValueError")
except ValueError:
    pass
print("  bad input OK")

# type_counts
assert pd.type_counts([res, rd, rs, rn]) == {pd.T_EMBANKMENT: 1, pd.T_DUGOUT: 2, pd.T_DEPRESSION: 1,
                                             "no breach flood": 2}
print("OK")
