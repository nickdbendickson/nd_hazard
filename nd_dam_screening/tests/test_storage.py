"""Synthetic check: a paraboloid bowl with a known volume.

Bed z = z0 + k*r^2 out to the rim.  Water fills to pool elevation.  We give
the estimator the DEM with the wet area FLATTENED (as LiDAR does) and check the
above-pool part exactly and the below-pool part loosely.
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))
import storage  # noqa: E402

cell = 5.0
n = 400
yy, xx = np.mgrid[0:n, 0:n]
r = np.hypot(xx - n / 2, yy - n / 2) * cell
k = 0.002                      # bed rises 0.002 m per m^2
bed = 100.0 + k * r ** 2
pool, crest = 105.0, 108.0

water = bed < pool
dem = np.where(water, pool, bed)   # hydro-flattened DEM

# exact volumes from the analytic bed
area = cell * cell
true_below = float((pool - bed[water]).sum() * area)
stage_mask = bed < crest
true_above = float(((crest - np.maximum(bed[stage_mask], pool)).sum()) * area)

res = storage.estimate_storage(
    dem, water, cell, dam_xy_idx=None, known_height_m=None,
    max_depth_m=pool - 100.0)
# force the crest for the exact comparison
curve = storage.above_pool_curve(dem, water, pool, crest, cell, 20)
above = curve[-1][1]

print("above-pool  est %.0f  true %.0f  err %.2f%%"
      % (above, true_above, 100 * (above - true_above) / true_above))
assert abs(above - true_above) / true_above < 0.01

below, dmax = storage.below_pool_volume(dem, water, cell, res.shore_slope_deg,
                                        pool - 100.0)
print("below-pool  est %.0f  true %.0f  err %.1f%%  (slope %.1f deg, dmax %.2f)"
      % (below, true_below, 100 * (below - true_below) / true_below,
         res.shore_slope_deg, dmax))
print("confidence:", res.confidence, "| notes:", res.notes)

# ---- calibrating with inventory NORMAL-pool storage fixes the below-pool overestimate ----
# curve to the TRUE crest (the estimator above ran without a crest, so its curve stops 1 m above pool)
curve = [(e, v + res.below_pool_m3) for e, v in storage.above_pool_curve(dem, water, pool, crest, cell, 20)]
cal, scale, note = storage.calibrate_below_pool(curve, res.below_pool_m3, true_below)
true_total = true_below + true_above
print("calibration: scale %.2f  total est %.0f -> %.0f (true %.0f, err %.1f%% -> %.1f%%)"
      % (scale, curve[-1][1], cal[-1][1], true_total,
         100 * (curve[-1][1] - true_total) / true_total, 100 * (cal[-1][1] - true_total) / true_total))
assert abs(cal[-1][1] - true_total) / true_total < 0.02
assert abs(cal[0][1] - true_below) < 1.0 and note == ""
_, s2, n2 = storage.calibrate_below_pool(curve, res.below_pool_m3, 1000 * true_below)
assert s2 == 10.0 and "clamped" in n2                       # absurd inventory value is clamped and flagged
assert storage.calibrate_below_pool(curve, res.below_pool_m3, None)[1] == 1.0
print("OK")
