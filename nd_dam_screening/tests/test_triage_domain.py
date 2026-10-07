"""Road-crossing carving, triage envelope and domain helpers on synthetic terrain."""
import os, sys, math
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core import gridutil as gu, hydrocond as hc, triage as tg, domain as dm  # noqa: E402

cell = 5.0
ny, nx = 300, 200                       # 1.5 km long (north->south), 1 km wide
gt = (0.0, cell, 0.0, ny * cell, 0.0, -cell)
yy, xx = np.mgrid[0:ny, 0:nx]
x_m = (xx - nx / 2) * cell
dem = 100.0 - yy * cell * 0.002 + 0.02 * np.abs(x_m) ** 1.2     # valley draining south, slope 0.002
thalweg_x = 0.0

# road embankment across the valley at row 150 (4 m fill, 6 cells wide)
road_rows = slice(148, 154)
dem_road = dem.copy()
crown = dem[151, nx // 2] + 4.0
dem_road[road_rows, :] = np.maximum(dem_road[road_rows, :], crown)
# a "dam" (protected) at row 60
dem_road[58:62, :] = np.maximum(dem_road[58:62, :], dem[60, nx // 2] + 5.0)

# ---------- polyline cells are 4-connected
diag = [(10.0, 10.0), (500.0, 800.0)]
cells = gu.polyline_cells(diag, gt, dem.shape)
assert all(abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1 for a, b in zip(cells[:-1], cells[1:])), "not 4-connected"

# ---------- carving: stream line down the thalweg (digitised upstream -> downstream)
stream = [(thalweg_x + nx / 2 * cell + 2.5, ny * cell - 2.5), (thalweg_x + nx / 2 * cell + 2.5, 2.5)]
protect = np.zeros(dem.shape, bool); protect[56:64, :] = True
cond, info = hc.breach_barriers(dem_road, [stream], gt, protect=protect)
col = nx // 2
print("carving:", info)
assert info["cuts"] == 1 and info["skipped_protected"] == 1
assert cond[151, col] < dem_road[151, col] - 3.0              # road cut
assert cond[60, col] == dem_road[60, col]                     # dam kept
assert np.all(np.diff(cond[62:, col]) <= 1e-9)               # monotonic below the dam
# reversed digitising gives the same result
cond2, _ = hc.breach_barriers(dem_road, [stream[::-1]], gt, protect=protect)
assert np.allclose(cond, cond2)
# roads mask required but absent at the crossing -> skipped
noroad = np.zeros(dem.shape, bool)
_, i3 = hc.breach_barriers(dem_road, [stream], gt, protect=protect, roads=noroad)
assert i3["cuts"] == 0 and i3["skipped_no_road"] == 1

# ---------- normal depth: wide rectangular channel, strip method is exact
w = 21; xs = np.full(w, 50.0); xs[:] = 10.0; xs[0] = xs[-1] = 60.0
n, S, d_true = 0.04, 0.001, 2.0
q = (w - 2) * cell * (1 / n) * d_true ** (5 / 3) * math.sqrt(S)
wse, width, area = tg.normal_depth_stage(xs, q, S, n, cell)
assert abs((wse - 10.0) - d_true) < 1e-3, wse

# ---------- trace path escapes the road pit on the raw DEM
path, end = tg.trace_path(dem_road, (70, col))
assert end == "edge" and path[-1][0] >= ny - 2, (end, path[-1])
assert any(r > 154 for r, _ in path)

# ---------- envelope: small vs big release, terrace structure stays dry
res_small = tg.envelope(cond, gt, (70, col), q_peak=20.0, volume_m3=2.0e3,
                        n=0.05, half_width_m=400)
res_big = tg.envelope(cond, gt, (70, col), q_peak=800.0, volume_m3=5.0e6,
                      n=0.05, half_width_m=400)
print("small: reach %.0f m end %s | big: reach %.0f m end %s edge %s"
      % (res_small["reach_m"], res_small["end"], res_big["reach_m"], res_big["end"], res_big["edge"]))
assert res_small["end"] == "volume" and res_small["reach_m"] < res_big["reach_m"]
assert res_big["edge"]                                       # big flood leaves the clip -> needs 2D / bigger clip
assert res_small["mask"][100, col] and not res_small["mask"][250, col]
terrace = (100, col + 90)                                    # 450 m off the thalweg, ~ +32 m
assert not res_big["mask"][terrace]
wider = res_big["mask"].sum() > res_small["mask"].sum()
assert wider

# ---------- coarse screen matches the full-resolution envelope
scr = tg.screen(cond, gt, (70, col), q_peak=800.0, volume_m3=5.0e6, target_cell=10.0, n=0.05, half_width_m=400)
ratio = scr["mask"].sum() / res_big["mask"].sum()
print("coarse screen wet area / full-res: %.2f (factor %d)" % (ratio, scr["factor"]))
assert 0.8 < ratio < 1.25 and scr["edge"] and not scr["mask"][terrace]

# ---------- domain helpers
assert dm.auto_cell(50 * dm.AF_M3) == 3.0 and dm.auto_cell(5e4 * dm.AF_M3) == 20.0
assert dm.auto_cell(5e4 * dm.AF_M3, bavg_m=12.0, dem_cell=1.0) == 6.0
path_c, _ = tg.trace_path(cond, (70, col))           # conditioned terrain: straight down the valley
assert all(c == col for _, c in path_c)
m = dm.corridor_mask(dem.shape, cell, path_c, (70, col), 200.0)
print("corridor keeps %.0f%% of the square clip" % (100 * m.mean()))
assert m.mean() < 0.6 and m[200, col] and not m[200, 5]
depth = np.zeros(dem.shape); depth[200:220, col - 45:col + 45] = 1.0
e, wl = dm.touches_wall(depth, m)
assert wl and not e
depth2 = np.zeros(dem.shape); depth2[-1, col] = 1.0
assert dm.touches_wall(depth2, m)[0]
arr = np.full(dem.shape, 0.5); d = np.ones(dem.shape)
assert not dm.still_growing(arr, d, 3600)
arr[:10, :] = 0.95
assert dm.still_growing(arr, d, 3600)
print("OK")
