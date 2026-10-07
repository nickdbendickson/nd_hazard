"""v3.2.0: stop extending the domain when the hazard class stops changing (optional rule).

The domain grows while the flood leaves an edge deeper than the edge tolerance (ND
REG_05.2026 5.1.1(f): extend downstream until further impacts would not change the hazard
classification). This rule accepts such an edge - the domain stops growing there - when

  (a) along the flow path near that edge the breach flood has died out: its depth, or with
      a no-breach baseline (flood scenario, ambient flow) its RISE over the baseline, stays
      below `threshold_ft` for the last stations before the edge: the termination point of
      ND Dam Hazard (a station below the threshold confirmed by the `confirm` stations after
      it) lies inside the domain and every station from it to the edge is below; the
      deepest water leaving through that edge (depth, or rise cell by cell) is below the
      threshold too; and
  (b) no receptor (structure, road or rail from the layers given) lies within the receptor
      swath beyond that edge (impacts.edge_decisions, the receptor stop rule). With no
      receptor layer nothing beyond the edge can be checked; the decision says so.

Stations follow ND Dam Hazard's termination point (termination.py, traveltime.py): a station
every `station_mi` miles along the flow path, the value of a station is the deepest wet cell
within `search_ft` of it, rise = station breach depth - station baseline depth. The flow path
is the steepest-descent path from the breach inflow point over the current domain (on a
block-minimum copy of the model DEM when the grid is large); the rule is only applied to the
edge that path leaves through - the flood leaving a side edge is extended as before.

The defaults (threshold 2.0 ft for the depth and for the rise, 3 confirming stations, a
station every 0.5 mi, deepest cell within 150 ft) are ND Dam Hazard's termination-point
defaults: SCREENING VALUES CHOSEN BY THE ENGINEER, not requirements of REG_05.2026, FEMA
P-333 or TM 11. The rule is a screening aid for where to stop modelling; every decision is
written to the run report for the engineer to confirm.

Pure numpy (terrain.trace_downstream for the path); no QGIS, no GDAL.
"""
import math

import numpy as np

from . import terrain
from .grid import Grid
from .units import FT, MILE

# ND Dam Hazard termination.py / constants.py defaults - SCREENING VALUES CHOSEN BY THE
# ENGINEER, not rules
DEFAULT_THRESHOLD_FT = 2.0      # termination.DEFAULT_RISE_FT and DEFAULT_DEPTH_FT
DEFAULT_CONFIRM = 3             # termination.DEFAULT_CONFIRM
DEFAULT_STATION_MI = 0.5        # constants.TRAVEL_STATION_MI
DEFAULT_SEARCH_FT = 150.0       # constants.TRAVEL_SEARCH_FT
MAX_TRACE_CELLS = 500000        # the flow path is traced on a coarser copy of larger grids
WET_M = 0.003                   # workflow.WET_M: a cell counts as wet above 0.01 ft

DEFAULTS_NOTE = ("Class-stop rule defaults (threshold {:g} ft for the depth or the rise over "
                 "the baseline, {} confirming stations, a station every {:g} mi, deepest wet "
                 "cell within {:g} ft) are ND Dam Hazard's termination-point defaults: "
                 "screening values chosen by the engineer, not requirements of REG_05.2026, "
                 "FEMA P-333 or TM 11.".format(DEFAULT_THRESHOLD_FT, DEFAULT_CONFIRM,
                                               DEFAULT_STATION_MI, DEFAULT_SEARCH_FT))


def coarse_grid(dem, max_cells=MAX_TRACE_CELLS):
    """The DEM grid itself, or a block-minimum copy (keeps the channels) of at most about
    `max_cells` cells, for tracing the flow path."""
    n = dem.nx * dem.ny
    if n <= max_cells:
        return dem
    k = int(math.ceil(math.sqrt(n / float(max_cells))))
    ny, nx = int(math.ceil(dem.ny / float(k))), int(math.ceil(dem.nx / float(k)))
    a = np.full((ny * k, nx * k), np.nan)
    a[:dem.ny, :dem.nx] = dem.masked()
    b = a.reshape(ny, k, nx, k)
    with np.errstate(all="ignore"):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            m = np.nanmin(np.nanmin(b, axis=3), axis=1)
    return Grid(m, dem.x0, dem.y0, dem.dx * k, crs_wkt=dem.crs_wkt)


def flow_path(dem, start_xy):
    """Steepest-descent path from start_xy to the edge of the domain (list of (x, y))."""
    g = coarse_grid(dem)
    w, h = terrain.bounds_size(g.bounds)
    path, _length = terrain.trace_downstream(g, start_xy, 4.0 * (w + h))
    return path


def exit_edge(grid, path):
    """The domain edge ("N", "S", "E", "W") the path ends on, or None when it ends inside
    (a pit, or the trace stopped). The nearest edge within one path cell counts."""
    if path is None or len(path) < 2:
        return None
    x, y = path[-1]
    x0, y0, x1, y1 = grid.bounds
    step = max(math.hypot(path[-1][0] - path[-2][0], path[-1][1] - path[-2][1]), grid.dx)
    d = {"W": x - x0, "E": x1 - x, "S": y - y0, "N": y1 - y}
    e = min(d, key=d.get)
    return e if d[e] <= 1.5 * step else None


def _window_max(grid, arr, x, y, radius):
    """(max of the wet values within `radius` of (x, y), cells found). Dry = 0."""
    c0 = int(math.floor((x - radius - grid.x0) / grid.dx))
    c1 = int(math.floor((x + radius - grid.x0) / grid.dx)) + 1
    r0 = int(math.floor((grid.y0 - (y + radius)) / grid.dx))
    r1 = int(math.floor((grid.y0 - (y - radius)) / grid.dx)) + 1
    c0, r0 = max(0, c0), max(0, r0)
    c1, r1 = min(grid.nx, c1), min(grid.ny, r1)
    if c1 <= c0 or r1 <= r0:
        return 0.0, 0
    xs = grid.x0 + (np.arange(c0, c1) + 0.5) * grid.dx
    ys = grid.y0 - (np.arange(r0, r1) + 0.5) * grid.dx
    X, Y = np.meshgrid(xs, ys)
    inside = np.hypot(X - x, Y - y) <= radius
    if not inside.any():          # a cell coarser than the circle: the cell under the point
        r, c = grid.rc(x, y)
        if not grid.inside(r, c):
            return 0.0, 0
        v = arr[r, c]
        return (float(v) if np.isfinite(v) and v > WET_M else 0.0), 1
    sub = arr[r0:r1, c0:c1][inside]
    wet = sub[np.isfinite(sub) & (sub > WET_M)]
    return (float(wet.max()) if wet.size else 0.0), int(inside.sum())


def stations(path, grid, depth, base=None, spacing_m=DEFAULT_STATION_MI * MILE,
             radius_m=DEFAULT_SEARCH_FT * FT):
    """Stations along `path` every `spacing_m` from its start, plus its last point (the
    edge). depth / base: arrays (m) on `grid`, NaN = dry. Each station: {"rm" (mi along the
    path), "x", "y", "depth_ft", "base_ft" (None without a baseline), "value_ft" (depth,
    or rise = depth - baseline depth), "is_edge"}."""
    p = np.asarray(path, dtype=float)
    if len(p) == 0:
        return []
    seg = np.hypot(*np.diff(p, axis=0).T) if len(p) > 1 else np.zeros(0)
    s = np.concatenate([[0.0], np.cumsum(seg)])
    total = float(s[-1])
    at = list(np.arange(0.0, total + 1e-6, spacing_m))
    if not at or total - at[-1] > 1e-6:
        at.append(total)
    out = []
    for k, a in enumerate(at):
        x, y = float(np.interp(a, s, p[:, 0])), float(np.interp(a, s, p[:, 1]))
        d, _n = _window_max(grid, depth, x, y, radius_m)
        b = None
        if base is not None:
            b, _nb = _window_max(grid, base, x, y, radius_m)
        val = d - b if b is not None else d
        out.append({"rm": round(a / MILE, 3), "x": round(x, 1), "y": round(y, 1),
                    "depth_ft": round(d / FT, 3), "base_ft": None if b is None else round(
                        b / FT, 3), "value_ft": round(val / FT, 3),
                    "is_edge": k == len(at) - 1})
    return out


def edge_line_value(depth, edge, base=None):
    """The deepest water (or the largest rise, cell by cell) on one edge row / column (m)."""
    sl = {"N": (0, slice(None)), "S": (-1, slice(None)), "W": (slice(None), 0),
          "E": (slice(None), -1)}[edge]
    d = np.where(np.isfinite(depth[sl]) & (depth[sl] > WET_M), depth[sl], 0.0)
    if base is not None:
        b = np.where(np.isfinite(base[sl]) & (base[sl] > WET_M), base[sl], 0.0)
        d = d - b
    return float(d.max()) if d.size else 0.0


def judge_edge(edge, grid, depth, path, base=None, threshold_ft=DEFAULT_THRESHOLD_FT,
               confirm=DEFAULT_CONFIRM, station_mi=DEFAULT_STATION_MI,
               search_ft=DEFAULT_SEARCH_FT, receptors=None):
    """Decision on one edge the flood leaves deeper than the tolerance.

    depth / base: arrays (m) on `grid` (base None = no baseline: the depth is judged).
    path: the flow path from the breach inflow over the domain (flow_path).
    receptors: None = no receptor layer given; else the receptor stop rule's info for this
    edge ({"extend": bool, "reason": text}, impacts.edge_decisions) - extend True means
    receptors lie ahead.
    Returns {"edge", "accept", "criterion", "threshold_ft", "confirm", "stations_below",
    "stations_required", "from_rm", "to_rm", "edge_value_ft", "receptors", "reason",
    "stations"}; "reason" is the sentence for the run report."""
    confirm = max(1, int(confirm))
    crit = "rise" if base is not None else "depth"
    word = "rise over the baseline" if base is not None else "depth"
    need = confirm + 1
    out = {"edge": edge, "accept": False, "criterion": crit, "threshold_ft": float(threshold_ft),
           "confirm": confirm, "stations_below": 0, "stations_required": need, "from_rm": None,
           "to_rm": None, "edge_value_ft": None, "receptors": None, "stations": []}
    # (b) receptors first: when they lie ahead nothing else needs computing
    if receptors is not None and receptors.get("extend", True):
        out["receptors"] = receptors.get("reason") or "receptors downstream"
        out["reason"] = "edge kept (extended): {} (rule (b))".format(out["receptors"])
        return out
    out["receptors"] = (receptors.get("reason") if receptors is not None else
                        "no structure, road or rail layer was given, so nothing beyond the "
                        "edge was checked")
    ex = exit_edge(grid, path)
    if ex != edge:
        out["reason"] = ("edge kept (extended): the flow path from the breach leaves the domain "
                         "through {} - rule (a) is judged only on the edge the flow path "
                         "leaves through".format("the {} edge".format(ex) if ex else
                                                 "no edge (it ends inside the domain)"))
        return out
    st = stations(path, grid, depth, base, station_mi * MILE, search_ft * FT)
    out["stations"] = st
    thr = float(threshold_ft)
    n = 0
    for s in reversed(st):
        if s["value_ft"] < thr:
            n += 1
        else:
            break
    out["stations_below"] = n
    if n:
        out["from_rm"], out["to_rm"] = st[len(st) - n]["rm"], st[-1]["rm"]
    ev = edge_line_value(depth, edge, base) / FT
    out["edge_value_ft"] = round(ev, 2)
    if n < need:
        above = st[len(st) - n - 1] if len(st) > n else None
        out["reason"] = (
            "edge kept (extended): the {} is below {:g} ft for only {} station(s) before the "
            "edge (needed {}: a station below the threshold and {} confirming station(s), as "
            "ND Dam Hazard's termination point){}".format(
                word, thr, n, need, confirm,
                "; it is {:.2f} ft at station {:.1f} mi".format(above["value_ft"], above["rm"])
                if above is not None else ""))
        return out
    if ev >= thr:
        out["reason"] = ("edge kept (extended): the {} stays below {:g} ft for {} stations along "
                         "the flow path, but the deepest water leaving through the {} edge has "
                         "a {} of {:.2f} ft".format(word, thr, n, edge, word, ev))
        return out
    out["accept"] = True
    out["reason"] = ("edge accepted: {} below {:g} ft for {} stations from station {:.1f} mi "
                     "(to the edge at {:.1f} mi; at most {:.2f} ft on the edge); {}".format(
                         word, thr, n, out["from_rm"], out["to_rm"], ev, out["receptors"]))
    return out


def station_table(dec, limit=12):
    """Markdown lines of the last stations of a decision (for the run report)."""
    st = dec.get("stations") or []
    if not st:
        return []
    rows = st[-limit:]
    base = any(s.get("base_ft") is not None for s in rows)
    head = "| Station (mi) | Depth (ft) |" + (" Baseline (ft) | Rise (ft) |" if base else "")
    lines = [head, "|---|---|" + ("---|---|" if base else "")]
    for s in rows:
        lines.append("| {}{} | {:.2f} |".format(
            s["rm"], " (edge)" if s.get("is_edge") else "", s["depth_ft"]) + (
            " {:.2f} | {:.2f} |".format(s["base_ft"] or 0.0, s["value_ft"]) if base else ""))
    return lines
