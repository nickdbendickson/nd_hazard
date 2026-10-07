"""Model-domain decisions: cell size, downstream corridor, run length, growth checks.

numpy only.  Used by the simulation runner for every engine.
"""
import numpy as np

from .gridutil import dilate, boundary_ring, NODATA

AF_M3 = 1233.4818375

# (storage upper bound in acre-ft, cell size in m).  EDIT to taste.
CELL_TABLE = [(100.0, 3.0), (1000.0, 5.0), (10000.0, 10.0), (float("inf"), 20.0)]


def auto_cell(vw_m3, bavg_m=None, dem_cell=1.0, table=None, min_cells_across_breach=2):
    """Cell size by reservoir size, fine enough to put >= 2 cells across the breach."""
    af = (vw_m3 or 0.0) / AF_M3
    cell = next(c for lim, c in (table or CELL_TABLE) if af <= lim)
    if bavg_m:
        cell = min(cell, max(bavg_m / min_cells_across_breach, dem_cell))
    return float(max(cell, dem_cell))


def corridor_mask(shape, cell, path_rc, keep_rc, half_width_m, keep_radius_m=150.0):
    """Cells within `half_width_m` of the downstream path, plus a disk at `keep_rc`.

    Everything outside becomes a wall (no-data) in the model, which roughly
    halves the cell count of a square clip.  If the flood reaches the corridor
    wall, the runner widens the corridor and runs again.
    """
    m = np.zeros(shape, bool)
    for r, c in path_rc:
        m[r, c] = True
    kr, kc = keep_rc
    rad = int(keep_radius_m / cell)
    yy, xx = np.ogrid[:shape[0], :shape[1]]
    m |= (yy - kr) ** 2 + (xx - kc) ** 2 <= rad * rad
    return dilate(m, max(int(half_width_m / cell), 1))


def apply_mask(dem, mask):
    return np.where(mask, dem, NODATA)


def touches_wall(depth, valid, thresh=0.05, terrain=None):
    """(touches_terrain_edge, touches_corridor_wall) for wet cells.

    terrain: cells that have terrain data.  Wet cells on its boundary (grid edge
    or next to missing terrain) mean the clip is too small; wet cells on the
    boundary of `valid` inside the terrain mean the corridor is too narrow.
    Without `terrain`, only the grid edge counts as terrain edge.
    """
    wet = depth > thresh
    if terrain is None:
        terrain = np.ones(valid.shape, bool)
    ring_t = boundary_ring(terrain)
    on_edge = bool((wet & ring_t).any())
    ring_c = boundary_ring(valid) & ~ring_t
    on_wall = bool((wet & ring_c).any())
    return on_edge, on_wall


def initial_sim_time(t_hydro_end_s, path_len_m, celerity_ms=1.0, factor=1.2,
                     min_s=1800.0, max_s=48 * 3600.0):
    """First guess at run length: hydrograph duration plus travel along the corridor."""
    t = (t_hydro_end_s + path_len_m / celerity_ms) * factor
    return float(min(max(t, min_s), max_s))


def still_growing(arrive_h, depth, sim_s, late_frac=0.85, depth_min=0.15, tol=0.01):
    """True if > tol of the meaningfully wet area first got wet in the last part of the run."""
    wet = depth > depth_min
    n = int(wet.sum())
    if n == 0:
        return False
    late = wet & (arrive_h > late_frac * sim_s / 3600.0)
    return late.sum() / n > tol
