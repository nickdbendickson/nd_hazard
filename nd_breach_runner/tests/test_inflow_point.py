"""find_inflow_point: the usual radius takes the LOWEST low-enough cell (the note says so); only
the wider 400 m fallback takes the NEAREST. Taking the nearest in the usual radius put the
inflow on the upstream face of the embankment / the floor of a dry reservoir."""
import numpy as np

from nd_breach_runner.core import terrain
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.tests.registry import test as register  # "test" would be collected by pytest
from nd_breach_runner.tests.synthetic import TOP_M, H_M


def _grid(fn, half=200.0, cell=10.0):
    n = int(2 * half / cell)
    xs = -half + (np.arange(n) + 0.5) * cell
    ys = half - (np.arange(n) + 0.5) * cell
    X, Y = np.meshgrid(xs, ys)
    return Grid(fn(X, Y), -half, half, cell)


def _dry_dam(X, Y):
    """A dry flood-control dam at x = 0: the empty reservoir floor (west) is half the dam height
    down, right against the upstream toe; the channel below the dam (east) is lower."""
    up = TOP_M - 0.55 * H_M + 0.0 * X
    down = TOP_M - H_M - 0.002 * np.clip(X, 0, None) - np.where(np.abs(Y) < 12, 0.3, 0.0)
    z = np.where(X < 0, up, down)
    emb = TOP_M - np.abs(X) / 40.0 * H_M
    return np.where(np.abs(X) <= 40, np.maximum(z, emb), z)


@register
def inflow_point_of_a_dry_dam_is_below_the_dam_not_on_the_reservoir_floor():
    pt, why = terrain.find_inflow_point(_grid(_dry_dam), (0.0, 0.0), H_M, TOP_M)
    assert pt is not None and pt[0] > 0, (pt, why)
    assert why.startswith("lowest ground within")


@register
def inflow_point_wider_search_takes_the_nearest_low_cell():
    # nothing low enough within the usual 90 m: a wide embankment, the toe 150 m east and a
    # deeper far valley 350 m east; the wider search takes the toe, not the far valley
    def wide(X, Y):
        z = np.full_like(X, TOP_M - 0.2 * H_M)
        z = np.where((X > 140) & (X < 170), TOP_M - 0.6 * H_M, z)
        return np.where(X > 330, TOP_M - 2.0 * H_M, z)
    pt, why = terrain.find_inflow_point(_grid(wide, half=420.0), (0.0, 0.0), H_M, TOP_M)
    assert pt is not None and 140 < pt[0] < 170, (pt, why)
    assert why.startswith("toe is outside the usual")


# ------------------------------------------------- downstream side of the crest (Absaraka)
def _basin_lower_than_toe(X, Y):
    """The wizard case at Absaraka DWR-2801: a closed dry basin west of the embankment whose
    floor is LOWER than the toe east of it (lowest-in-radius alone picks the reservoir floor).
    The basin is ringed by ground near the top of dam; the downstream valley drains east off
    the grid."""
    floor = TOP_M - 0.9 * H_M                          # reservoir floor
    toe = TOP_M - 0.6 * H_M - 0.002 * np.clip(X, 0, None)
    z = np.where(X < 0, floor, toe)
    rim = (X < -380) | ((X < 0) & (np.abs(Y) > 380))     # the basin is closed on its other sides
    z = np.where(rim, TOP_M - 0.05 * H_M, z)
    emb = TOP_M - np.abs(X) / 40.0 * H_M
    return np.where(np.abs(X) <= 40, np.maximum(z, emb), z)


def _setup(half=1000.0, cell=20.0):
    g = _grid(_basin_lower_than_toe, half=half, cell=cell)
    pool = g.like(terrain.pool_mask(g, H_M).astype(float), nodata=-1.0)
    return g, pool


@register
def inflow_lowest_alone_picks_the_basin_floor_lower_than_the_toe():
    g, _pool = _setup()
    pt, _why = terrain.find_inflow_point(g, (0.0, 0.0), H_M, TOP_M)
    assert pt is not None and pt[0] < 0, pt                     # the bug the constraint fixes


@register
def inflow_pool_and_direction_find_the_downstream_toe():
    g, pool = _setup()
    assert pool.data[g.rc(-200.0, 0.0)] == 1.0 and pool.data[g.rc(200.0, 0.0)] == 0.0
    v, how = terrain.downstream_direction(g, (0.0, 0.0), H_M)
    assert v[0] > 0.9 and "pool" in how, (v, how)
    pt, why = terrain.find_inflow_point(g, (0.0, 0.0), H_M, TOP_M, downstream=v, pool=pool)
    assert pt is not None and pt[0] > 0, (pt, why)
    assert why.startswith("lowest ground within")
    # the side test alone also works, and so does a crest line (oriented away from the pool)
    pt2, _ = terrain.find_inflow_point(g, (0.0, 0.0), H_M, TOP_M, downstream=v)
    assert pt2[0] > 0
    n, how = terrain.downstream_direction(g, (0.0, 0.0), H_M, crest_line=[(0, 300), (0, -300)])
    assert n[0] > 0.99 and "crest-line" in how, (n, how)
    n, _ = terrain.downstream_direction(g, (0.0, 0.0), H_M, crest_line=[(0, -300), (0, 300)])
    assert n[0] > 0.99


@register
def inflow_ties_on_a_flat_floor_go_to_the_nearest():
    def flat(X, Y):
        z = np.full_like(X, TOP_M - 0.8 * H_M)
        return np.where(np.abs(X) <= 20, TOP_M, z)
    g = _grid(flat, half=200.0, cell=10.0)
    pt, _why = terrain.find_inflow_point(g, (0.0, 0.0), H_M, TOP_M, downstream=(1.0, 0.0))
    assert pt is not None and abs(pt[0] - 25.0) < 1e-6 and abs(pt[1]) <= 5.0, pt


@register
def inflow_with_no_downstream_cell_warns_and_falls_back():
    g, pool = _setup()
    pt, why = terrain.find_inflow_point(g, (0.0, 0.0), H_M, TOP_M, downstream=(-1.0, 0.0),
                                        pool=pool)          # everything west is the pool
    assert pt is not None and why.startswith(terrain.NO_DOWNSTREAM), why


@register
def inflow_workflow_helper_uses_the_pool_read_and_warns():
    from nd_breach_runner.core import workflow

    class Src(object):
        def read(self, bounds, cell):
            half = (bounds[2] - bounds[0]) / 2.0
            return _grid(_basin_lower_than_toe, half=half, cell=cell)

    class Dam(object):
        crest_line_xy = None
    rad = max(90.0, 8.0 * H_M)
    local = Src().read((-430, -430, 430, 430), 20.0)
    warnings, logs = [], []
    pt, why = workflow._downstream_inflow(Src(), local, Dam(), (0.0, 0.0), H_M, TOP_M, rad, 20.0,
                                          warnings, logs.append)
    assert pt is not None and pt[0] > 0 and "downstream side" in why and not warnings, (pt, why)
    assert any("Downstream side of the crest" in m for m in logs)
