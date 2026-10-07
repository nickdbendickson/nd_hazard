# -*- coding: utf-8 -*-
"""Evacuation timing screening: how many minutes are left between the time
people can start to leave and the time the water reaches them or the roads
out become impassable.

THIS IS A SCREENING TABLE, NOT A LIFE-SAFETY MODEL. It uses the mapped road
network, one travel speed, one preparation delay, and the arrival times of
the dam-break raster; it does not model traffic, road capacity, bridges
above the water, people on foot, or what people actually do.

Model
-----
  Departure time   t0 = warning issued (hours after breach start) x 60
                        + preparation / mobilisation delay (min)
  Road network     the road layer is cut into pieces (edges) at its vertices
                   and, on long stretches, at most every EDGE_PIECE_FT
                   (T-junctions digitised without a shared vertex are joined
                   within access.SNAP_FT, as in access.py). Every edge is
                   used in both directions at the motorist speed.
  Edge closure     an edge is CLOSED from the time the water first arrives at
                   any of its sample points (every LINE_SAMPLE_STEP_FT) whose
                   maximum depth is at least PASSABLE_DEPTH_FT: the closure
                   time is the arrival raster's first-arrival time there. (The
                   first arrival is earlier than the time the depth reaches
                   the passable depth, so this is conservative.) A sample
                   that is deep enough but has no arrival time closes its edge
                   from time 0 and is counted.
  Using an edge    a vehicle can use an edge only if it ENTERS before the
                   edge closes and can TRAVERSE it before it closes
                   (enter time + travel time <= closure time; the whole edge
                   counts as closed from the earliest closure of any of its
                   samples). Waiting never helps, so this is simple and exact
                   for the model.
  Safe place       a road node outside the depth raster (the road leaves the
                   modelled area, as in access.py: beyond EXIT_MARGIN_FT of the
                   raster's edge) - or, when `dry_safe_ft` is given, also a node
                   that is dry with a dry ring of that radius around it.
  Structures       each habitable structure (the flooded ones, optionally the
                   near-flooded, or all) is attached to the nearest road piece
                   within the access distance and leaves after t0: the
                   drive to the road at ACCESS_LEG_MPH, then along the road.

Search
------
Instead of one search per structure, ONE multi-source search from all safe
nodes over the reversed graph computes, for every node, the LATEST TIME at
which one can be at it and still reach a safe node (a Dijkstra that maximises
that time: L(u) = max over edges (u,v) of min(closure(u,v), L(v)) - travel
time; safe nodes have L = infinity). A structure's latest departure is the best
of its two edge ends minus the drive to them; it also has to leave before the
water reaches the structure itself (its First_Arrival_hr).

    margin (min)  = min(latest departure by road, water arrival at the
                        structure) - t0
    margin >= 0   -> a safe exit is possible in this model
    margin <  0   -> cannot get out in time (screening)
    no limit      -> neither the roads nor the structure ever close (blank)

`evac_time_min` is the travel time along the route with the LATEST
departure (the most robust route, not necessarily the shortest).

Map layers (v2.9). The search also keeps, per node, the next node / piece
toward the safe place (two integer arrays), so the route of any structure is
rebuilt afterwards by following them - only for the structures reported, no
second search. routes_layer() gives EAP_Evacuation_Routes (one line per
structure of the timing table: structure -> road -> safe place, with the same
numbers as QA/Evacuation_Timing.csv) and closures_layer() EAP_Road_Closures
(the road pieces that close: closure time in minutes after breach start and
the deepest sample). Both are memory layers in the depth raster's CRS (the
analysis CRS in the pipeline); evacuation_timing(keep_routes=True) keeps what
they need and route_layers() releases it again.

All thresholds are SCREENING VALUES CHOSEN BY THE ENGINEER, not regulatory
numbers (see the defaults below).
"""

import csv
import heapq
import math
import os
from collections import defaultdict

import numpy as np
from qgis.core import QgsCoordinateTransform, QgsFeatureRequest, QgsRectangle

from . import consequences as CQ
from .access import EXIT_MARGIN_FT, SNAP_FT, _ft, _node_line_ends, _seg_dist
from .constants import (
    DEFAULT_ACCESS_SEARCH_FT, DEFAULT_VEHICLE_SPEED_MPH, DEFAULT_WARNING_ISSUED_HR,
    LINE_SAMPLE_STEP_FT,
)
from .extent import distance_xy_in_crs
from .helpers import LimitedLogger, prepare_output_path, transform_geometry, transform_xy
from .linear import AttrReader, line_parts

# ------------------------------------------------------------------ settings --
# SCREENING VALUES CHOSEN BY THE ENGINEER (not regulatory numbers)
DEFAULT_PREP_MIN = 15.0              # mobilisation delay after the warning is issued
DEFAULT_PASSABLE_DEPTH_FT = 1.0      # a road is closed where the depth reaches this
DEFAULT_ACCESS_LEG_MPH = 10.0        # structure to the road (driveway / parking lot)
DEFAULT_EDGE_PIECE_FT = 500.0        # longest road piece (shorter pieces = finer closures)
DEFAULT_TIGHT_MARGIN_MIN = 30.0      # margins below this are reported as "tight"
INCLUDE_MODES = ("flooded", "near", "all")
BATCH_POINTS = 2000000               # road sample points per raster read (memory bound)
CSV_NAME = "Evacuation_Timing.csv"
HEAD = ["Struct_ID", "Persons", "Max_Depth_ft", "Status", "Safe_Exit_Possible",
        "Depart_min", "Water_Arrival_min", "Road_Latest_Departure_min",
        "Latest_Departure_min", "Margin_min", "Evac_Time_min", "Limiting",
        "Limiting_Road", "Limiting_Edge", "Limiting_Close_min", "Limiting_X",
        "Limiting_Y", "Road_Distance_ft"]

DEFAULTS = {
    # cfg keys the pipeline would add (see options_from_cfg); speed_mph,
    # warning_issued_hr, access_ft and population already exist
    "evacuation": False,
    "evac_prep_min": DEFAULT_PREP_MIN,
    "evac_passable_ft": DEFAULT_PASSABLE_DEPTH_FT,
    "evac_access_leg_mph": DEFAULT_ACCESS_LEG_MPH,
    "evac_include": "flooded",
    "evac_dry_safe_ft": 0.0,
    "evac_tight_min": DEFAULT_TIGHT_MARGIN_MIN,
    # v2.9: keep the routes for EAP_Evacuation_Routes / EAP_Road_Closures
    "evac_routes": True,
}

STATUS_OK = "ok"
STATUS_NO_ROAD = "no mapped road within the access distance"
STATUS_NO_EXIT = "no route to a safe place on the mapped roads"

CAVEATS = (
    "Evacuation timing is a screening table, not a life-safety model: mapped roads only "
    "(no private drives, trails or bridges above the water), one speed, no traffic, no "
    "people on foot, and a single preparation delay",
)


def options_from_cfg(cfg):
    """Keyword arguments for evacuation_timing / run_evacuation from the
    pipeline's cfg dict (missing keys take DEFAULTS / the plugin's defaults)."""
    def g(k, default=None):
        v = cfg.get(k)
        return DEFAULTS.get(k, default) if v is None else v
    return {
        "warning_issued_hr": float(g("warning_issued_hr", DEFAULT_WARNING_ISSUED_HR)),
        "prep_min": float(g("evac_prep_min")),
        "speed_mph": float(g("speed_mph", DEFAULT_VEHICLE_SPEED_MPH)),
        "passable_depth_ft": float(g("evac_passable_ft")),
        "access_ft": float(g("access_ft", DEFAULT_ACCESS_SEARCH_FT)),
        "access_leg_mph": float(g("evac_access_leg_mph")),
        "include": g("evac_include"),
        "dry_safe_ft": float(g("evac_dry_safe_ft") or 0.0) or None,
        "tight_margin_min": float(g("evac_tight_min")),
        "period": "day" if cfg.get("population") == "day" else "night",
        "keep_routes": bool(g("evac_routes")),
    }


# ------------------------------------------------------------------ the graph --
class RoadGraph(object):
    """Road pieces (edges) and their nodes in the depth raster's CRS.
    Arrays (E edges, N nodes): eu, ev (end nodes), elen_ft, etau (min),
    eclose (min; inf = never), ex0, ey0, ex1, ey1 (chord), epart; node_x,
    node_y, safe (bool). `ok` False with `message` when there is no usable
    network."""

    def __init__(self):
        self.ok = False
        self.message = ""
        self.n_nodes = self.n_edges = 0
        self.part_names = []
        self.stats = {}


def _units_per_ft(crs, lat):
    kx, ky = distance_xy_in_crs(1.0, crs, lat)
    return kx, ky


def _depth_rect(depth):
    gt = depth.gt
    xs = [gt[0] + c * gt[1] + r * gt[2] for c in (0, depth.xsize) for r in (0, depth.ysize)]
    ys = [gt[3] + c * gt[4] + r * gt[5] for c in (0, depth.xsize) for r in (0, depth.ysize)]
    return QgsRectangle(min(xs), min(ys), max(xs), max(ys))


def _nearest_sample(s, targets, a, b):
    """Index in (a, b) of the sample whose arc length is closest to each target."""
    pos = np.clip(np.searchsorted(s, targets), a + 1, b)
    left = np.clip(pos - 1, a + 1, b)
    pick = np.where(np.abs(s[left] - targets) <= np.abs(s[pos] - targets), left, pos)
    return pick[(pick > a) & (pick < b)]


def _densify(c, step):
    """Sample points of a polyline every <= step, with a flag at its vertices."""
    seg = np.hypot(np.diff(c[:, 0]), np.diff(c[:, 1]))
    px, py, vf = [], [], []
    for k in range(len(c) - 1):
        n = max(1, int(math.ceil(seg[k] / step * (1.0 - 1e-9))))   # (no float-noise extra sample)
        t = np.arange(n, dtype=np.float64) / n
        px.append(c[k, 0] + t * (c[k + 1, 0] - c[k, 0]))
        py.append(c[k, 1] + t * (c[k + 1, 1] - c[k, 1]))
        v = np.zeros(n, dtype=bool)
        v[0] = True
        vf.append(v)
    px.append(c[-1:, 0])
    py.append(c[-1:, 1])
    vf.append(np.array([True]))
    return np.concatenate(px), np.concatenate(py), np.concatenate(vf)


def build_road_graph(road_layer, depth, arrival, transform_ctx, filter_rect=None, lat=None,
                     speed_mph=DEFAULT_VEHICLE_SPEED_MPH,
                     passable_depth_ft=DEFAULT_PASSABLE_DEPTH_FT,
                     piece_ft=DEFAULT_EDGE_PIECE_FT, dry_safe_ft=None, progress_cb=None):
    """RoadGraph of `road_layer` in the depth raster's CRS with the closure time
    of every edge. `depth` / `arrival`: RasterSamplers (arrival: hours after
    breach start, may be in another CRS). `filter_rect`: only roads that touch
    it (depth raster's CRS; default the depth raster grown by one mile).
    Never raises for missing data: `graph.ok` / `graph.message`."""
    g = RoadGraph()
    crs = depth.crs
    kx, ky = _units_per_ft(crs, lat)
    step = max(kx, ky) * LINE_SAMPLE_STEP_FT
    tol = _ft(0.5, crs, lat)
    drect = _depth_rect(depth)
    if filter_rect is None:
        dx, dy = distance_xy_in_crs(5280.0, crs, lat)
        filter_rect = QgsRectangle(drect.xMinimum() - dx, drect.yMinimum() - dy,
                                   drect.xMaximum() + dx, drect.yMaximum() + dy)
    to_a = QgsCoordinateTransform(road_layer.crs(), crs, transform_ctx)
    req = QgsFeatureRequest()
    try:
        back = QgsCoordinateTransform(crs, road_layer.crs(), transform_ctx)
        req.setFilterRect(filter_rect if back.isShortCircuited()
                          else back.transformBoundingBox(filter_rect))
    except Exception:
        pass
    reader = AttrReader(road_layer)
    problems = LimitedLogger("Evacuation road network")
    parts, names = [], []
    total = max(1, road_layer.featureCount())
    for i, feat in enumerate(road_layer.getFeatures(req)):
        if progress_cb and i % 200 == 0:
            progress_cb(min(30.0, 30.0 * i / total), "Evacuation: road network")
        geom = transform_geometry(feat.geometry(), to_a)
        if geom is None or geom.isNull() or geom.isEmpty():
            continue
        try:
            attrs = feat.attributes()
            name = reader.road_name(attrs) or reader.name_text(attrs)
            for coords in line_parts(geom):
                c = np.asarray(coords, dtype=np.float64)
                if len(c) >= 2 and np.isfinite(c).all():
                    parts.append(c)
                    names.append(name)
        except Exception as e:                       # noqa: BLE001
            problems.log("feature {}: {}".format(feat.id(), e))
    problems.summary()
    if not parts:
        g.message = "no road of the road layer lies in or near the model area"
        return g
    parts = _node_line_ends(parts, _ft(SNAP_FT, crs, lat))

    # ---- sample points of every part, ground arc length, edge cuts ----------
    piece = max(float(piece_ft), 1.0)
    sxs, sys_, sarc, starts, ends, epart = [], [], [], [], [], []
    off = 0
    for pi, c in enumerate(parts):
        px, py, isv = _densify(c, step)
        s = np.concatenate(([0.0], np.cumsum(np.hypot(np.diff(px) / kx, np.diff(py) / ky))))
        vidx = np.nonzero(isv)[0]
        cuts = [vidx]
        for j in np.nonzero(np.diff(s[vidx]) > piece)[0]:
            a, b = int(vidx[j]), int(vidx[j + 1])
            m = int(math.ceil((s[b] - s[a]) / piece - 1e-9))
            cuts.append(_nearest_sample(s, s[a] + (s[b] - s[a]) * np.arange(1, m) / m, a, b))
        cut = np.unique(np.concatenate(cuts))
        starts.append(cut[:-1] + off)
        ends.append(cut[1:] + off)
        epart.append(np.full(len(cut) - 1, pi, dtype=np.int64))
        sxs.append(px)
        sys_.append(py)
        sarc.append(s)
        off += len(px)
    px, py = np.concatenate(sxs), np.concatenate(sys_)
    arc = np.concatenate(sarc)
    e0, e1, epart = np.concatenate(starts), np.concatenate(ends), np.concatenate(epart)
    if len(e0) == 0:
        g.message = "the road layer has no usable line"
        return g

    # ---- depth and arrival at the sample points ------------------------------
    n = len(px)
    dep = np.empty(n)
    for i0 in range(0, n, BATCH_POINTS):
        if progress_cb:
            progress_cb(30.0 + 30.0 * i0 / max(1, n), "Evacuation: flooding of the roads")
        dep[i0:i0 + BATCH_POINTS] = depth.values_at(px[i0:i0 + BATCH_POINTS],
                                                    py[i0:i0 + BATCH_POINTS])
    close_s = np.full(n, np.inf)
    unk_s = np.zeros(n, dtype=bool)               # deep enough but no arrival time
    n_unknown_arrival = 0
    with np.errstate(invalid="ignore"):
        block = np.isfinite(dep) & (dep >= float(passable_depth_ft))
    if block.any():
        bi = np.nonzero(block)[0]
        ax, ay = px[bi], py[bi]
        if getattr(arrival, "crs", None) is not None and arrival.crs != crs:
            ax, ay = transform_xy(ax, ay, QgsCoordinateTransform(crs, arrival.crs,
                                                                transform_ctx))
        arr = arrival.values_at(ax, ay)
        with np.errstate(invalid="ignore"):
            good = np.isfinite(arr) & (arr >= 0)
        close_s[bi] = np.where(good, arr * 60.0, 0.0)
        unk_s[bi] = ~good
        n_unknown_arrival = int((~good).sum())
    eclose = np.minimum(np.minimum.reduceat(close_s, e0), close_s[e1])
    eunk = np.logical_or(np.logical_or.reduceat(unk_s, e0), unk_s[e1])
    # v2.9: deepest sample of each piece (EAP_Road_Closures); NaN = no depth on it
    with np.errstate(invalid="ignore"):
        dd = np.where(np.isfinite(dep), dep, -np.inf)
    emaxd = np.maximum(np.maximum.reduceat(dd, e0), dd[e1])
    emaxd = np.where(np.isfinite(emaxd), emaxd, np.nan)
    elen = arc[e1] - arc[e0]                 # ground ft (arc restarts in each part)
    speed_ft_min = max(float(speed_mph), 0.01) * 5280.0 / 60.0
    etau = elen / speed_ft_min

    # ---- nodes ---------------------------------------------------------------
    ex0, ey0, ex1, ey1 = px[e0], py[e0], px[e1], py[e1]
    keys = np.stack([np.rint(np.concatenate([ex0, ex1]) / tol),
                     np.rint(np.concatenate([ey0, ey1]) / tol)], axis=1).astype(np.int64)
    _u, first, inv = np.unique(keys, axis=0, return_index=True, return_inverse=True)
    inv = np.asarray(inv).reshape(-1)
    nx_all = np.concatenate([ex0, ex1])[first]
    ny_all = np.concatenate([ey0, ey1])[first]
    ne = len(e0)
    eu, ev = inv[:ne], inv[ne:]
    N = len(first)
    # exits: outside the depth raster's inner rectangle (as access.py)
    margin = _ft(EXIT_MARGIN_FT, crs, lat)
    margin = min(margin, 0.05 * min(drect.width(), drect.height()))
    inner = (drect.xMinimum() + margin, drect.yMinimum() + margin,
             drect.xMaximum() - margin, drect.yMaximum() - margin)
    safe = (nx_all < inner[0]) | (nx_all > inner[2]) | (ny_all < inner[1]) | (ny_all > inner[3])
    n_exit = int(safe.sum())
    if dry_safe_ft and dry_safe_ft > 0:
        safe |= _dry_nodes(depth, nx_all, ny_all, float(dry_safe_ft), kx, ky)
    g.eu, g.ev, g.elen_ft, g.etau, g.eclose, g.eunk = eu, ev, elen, etau, eclose, eunk
    g.ex0, g.ey0, g.ex1, g.ey1, g.epart = ex0, ey0, ex1, ey1, epart
    g.emaxd, g.crs = emaxd, crs
    g.node_x, g.node_y, g.safe = nx_all, ny_all, safe
    g.n_nodes, g.n_edges = N, ne
    g.part_names = names
    g.speed_ft_min = speed_ft_min
    g.kx, g.ky = kx, ky
    g.stats = {"parts": len(parts), "edges": ne, "nodes": N, "edges_closing": int(
        np.isfinite(eclose).sum()), "edges_no_arrival": int(eunk.sum()),
        "samples_no_arrival": n_unknown_arrival, "exit_nodes": n_exit,
        "safe_nodes": int(safe.sum()), "length_ft": float(elen.sum())}
    if not safe.any():
        g.message = ("no road leaves the depth raster (load a road layer that extends beyond "
                     "the model area, or give a dry-safe distance)")
        return g
    g.ok = True
    return g


def _dry_nodes(depth, nx, ny, r_units_ft, kx, ky):
    """Nodes that are dry with a dry ring of radius r (ft) around them (8
    points); NoData / outside the raster counts as dry."""
    ang = np.arange(8) * (math.pi / 4.0)
    xs = [nx] + [nx + r_units_ft * kx * math.cos(a) for a in ang]
    ys = [ny] + [ny + r_units_ft * ky * math.sin(a) for a in ang]
    dry = np.ones(len(nx), dtype=bool)
    for x, y in zip(xs, ys):
        d = depth.values_at(x, y)
        with np.errstate(invalid="ignore"):
            dry &= ~(np.isfinite(d) & (d > 0))
    return dry


# ------------------------------------------------------------------- the search --
def latest_departure(graph):
    """(L, D, bind) per node: L = latest time (min after breach start) one can be
    at the node and still reach a safe node (inf at safe nodes, -inf when no
    route exists), D = travel time (min) along that route, bind = the edge whose
    closure limits L (-1: none). Multi-source Dijkstra on the reversed (here
    undirected) graph that maximises L."""
    return latest_departure_tree(graph)[:3]


def latest_departure_tree(graph):
    """latest_departure plus the route itself: (L, D, bind, nxt_node, nxt_edge)
    where nxt_node[n] / nxt_edge[n] are the next node / edge from n toward the
    safe place along the route that gives L[n] and D[n] (-1 at safe nodes and
    where no route exists). Following them from a node reproduces D exactly
    (D[n] = D[nxt_node[n]] + travel time of nxt_edge[n])."""
    N, E = graph.n_nodes, graph.n_edges
    src = np.concatenate([graph.eu, graph.ev])
    dst = np.concatenate([graph.ev, graph.eu])
    eid = np.concatenate([np.arange(E), np.arange(E)])
    order = np.argsort(src, kind="stable")
    indptr = np.searchsorted(src[order], np.arange(N + 1)).tolist()
    adj_v, adj_e = dst[order].tolist(), eid[order].tolist()
    tau, close = graph.etau.tolist(), graph.eclose.tolist()
    inf = float("inf")
    L = [-inf] * N
    D = [inf] * N
    bind = [-1] * N
    nxt_n = [-1] * N
    nxt_e = [-1] * N
    done = [False] * N
    heap = []
    for n in np.nonzero(graph.safe)[0].tolist():
        L[n], D[n] = inf, 0.0
        heap.append((-inf, 0.0, n))
    heapq.heapify(heap)
    while heap:
        negl, d, u = heapq.heappop(heap)
        if done[u]:
            continue
        done[u] = True
        lu = -negl
        for k in range(indptr[u], indptr[u + 1]):
            v = adj_v[k]
            if done[v]:
                continue
            e = adj_e[k]
            c = close[e]
            cand = (c if c < lu else lu) - tau[e]
            nd = d + tau[e]
            if cand > L[v] or (cand == L[v] and nd < D[v]):
                L[v], D[v] = cand, nd
                bind[v] = e if c < lu else bind[u]
                nxt_n[v], nxt_e[v] = u, e
                heapq.heappush(heap, (-cand, nd, v))
    return (np.array(L), np.array(D), np.array(bind, dtype=np.int64),
            np.array(nxt_n, dtype=np.int64), np.array(nxt_e, dtype=np.int64))


# ------------------------------------------------------------------ structures --
def _edge_grid(graph, cell):
    grid = defaultdict(list)
    x0, x1 = np.minimum(graph.ex0, graph.ex1), np.maximum(graph.ex0, graph.ex1)
    y0, y1 = np.minimum(graph.ey0, graph.ey1), np.maximum(graph.ey0, graph.ey1)
    gx0, gx1 = np.floor(x0 / cell).astype(np.int64), np.floor(x1 / cell).astype(np.int64)
    gy0, gy1 = np.floor(y0 / cell).astype(np.int64), np.floor(y1 / cell).astype(np.int64)
    for e in range(graph.n_edges):
        for i in range(gx0[e], gx1[e] + 1):
            for j in range(gy0[e], gy1[e] + 1):
                grid[(i, j)].append(e)
    return grid


def _in_scope(rec, include):
    if rec.get("habitable") != "Yes":
        return False
    wet = bool(rec.get("max_depth")) and rec["max_depth"] > 0
    if include == "all" or wet:
        return True
    if include == "near":
        return bool(rec.get("near")) or rec.get("cut_off") is True
    return False


def _road_name(graph, e):
    if e is None or e < 0:
        return ""
    return graph.part_names[int(graph.epart[e])] or ""


def evaluate_structures(graph, structs, warning_issued_hr=DEFAULT_WARNING_ISSUED_HR,
                        prep_min=DEFAULT_PREP_MIN, access_ft=DEFAULT_ACCESS_SEARCH_FT,
                        access_leg_mph=DEFAULT_ACCESS_LEG_MPH, include="flooded",
                        period="night", annotate=True):
    """Per-structure timing rows (list of dicts) from a RoadGraph. Sets
    rec["evac_margin_min"], ["evac_safe_exit"] (True / False / None),
    ["evac_time_min"], ["evac_note"] on every structure when `annotate`.
    v2.9: a row with a route keeps row["_route"] (where it joins the road and
    at which end of that piece it leaves) and the search's next-hop arrays are
    kept on the graph (graph.nxt_node / graph.nxt_edge), so route_of() can
    rebuild the route of any row later without searching again."""
    L, D, bind, nxt_n, nxt_e = latest_departure_tree(graph)
    graph.L, graph.D, graph.nxt_node, graph.nxt_edge = L, D, nxt_n, nxt_e
    t0 = float(warning_issued_hr) * 60.0 + float(prep_min)
    leg_ft_min = max(float(access_leg_mph), 0.01) * 5280.0 / 60.0
    kx, ky = graph.kx, graph.ky
    reach = max(access_ft * kx, access_ft * ky)
    grid = _edge_grid(graph, max(reach, 1e-9))
    cell = max(reach, 1e-9)
    inf = float("inf")
    rows = []
    for rec in structs:
        if annotate:
            rec["evac_margin_min"], rec["evac_safe_exit"] = None, None
            rec["evac_time_min"], rec["evac_note"] = None, ""
        if not _in_scope(rec, include):
            continue
        persons, _basis = CQ.population_at_risk(rec, period)
        arrival_hr = rec.get("arrival")
        wet = bool(rec.get("max_depth")) and rec["max_depth"] > 0
        water = float(arrival_hr) * 60.0 if (arrival_hr is not None and wet
                                             and np.isfinite(arrival_hr)) else None
        row = {"id": rec.get("id"), "persons": persons, "max_depth": rec.get("max_depth"),
               "status": STATUS_OK, "safe_exit": None, "depart_min": t0,
               "water_min": water, "road_latest_min": None, "latest_min": None,
               "margin_min": None, "evac_time_min": None, "limiting": "", "limiting_road": "",
               "limiting_edge": None, "limiting_close_min": None, "limiting_x": None,
               "limiting_y": None, "road_dist_ft": None}
        rows.append(row)
        try:
            p = rec["geom"].centroid().asPoint()
            x, y = p.x(), p.y()
        except Exception:
            row["status"] = STATUS_NO_ROAD
            continue
        gx, gy = int(math.floor(x / cell)), int(math.floor(y / cell))
        cand = set()
        for i in (gx - 1, gx, gx + 1):
            for j in (gy - 1, gy, gy + 1):
                cand.update(grid.get((i, j), ()))
        if not cand:
            row["status"] = STATUS_NO_ROAD
            continue
        cand = np.fromiter(cand, dtype=np.int64)
        dist = _seg_dist(x, y, graph.ex0[cand], graph.ey0[cand], graph.ex1[cand],
                         graph.ey1[cand])
        k = int(np.argmin(dist))
        if dist[k] > reach:
            row["status"] = STATUS_NO_ROAD
            continue
        e = int(cand[k])
        x0, y0, x1, y1 = graph.ex0[e], graph.ey0[e], graph.ex1[e], graph.ey1[e]
        dx, dy = x1 - x0, y1 - y0
        L2 = dx * dx + dy * dy
        t = min(1.0, max(0.0, ((x - x0) * dx + (y - y0) * dy) / L2)) if L2 > 0 else 0.0
        qx, qy = x0 + t * dx, y0 + t * dy
        leg_ft = math.hypot((x - qx) / kx, (y - qy) / ky)
        row["road_dist_ft"] = leg_ft
        lam = leg_ft / leg_ft_min                                   # drive to the road
        best = None                               # (latest, duration, edge, node, fraction)
        for node, frac in ((int(graph.eu[e]), t), (int(graph.ev[e]), 1.0 - t)):
            if L[node] == -inf:
                continue
            s_min = frac * graph.elen_ft[e] / graph.speed_ft_min       # along this edge
            c = graph.eclose[e]
            latest = (c if c < L[node] else L[node]) - lam - s_min
            dur = lam + s_min + D[node]
            lim = e if c < L[node] else int(bind[node])
            if best is None or latest > best[0] or (latest == best[0] and dur < best[1]):
                best = (latest, dur, lim, node, frac)
        if best is None:
            row["status"] = STATUS_NO_EXIT
            if annotate:
                rec["evac_note"] = STATUS_NO_EXIT
            continue
        latest_road, dur, lim, node, frac = best
        row["_route"] = (float(x), float(y), float(qx), float(qy), e, node, float(frac),
                         float(leg_ft), float(lam))
        row["road_latest_min"] = None if latest_road == inf else float(latest_road)
        row["evac_time_min"] = float(dur)
        cap = latest_road
        limiting = "road" if latest_road < inf else ""
        if water is not None and water <= cap:
            cap = water
            limiting = "water"
        row["latest_min"] = None if cap == inf else float(cap)
        if cap == inf:
            row["safe_exit"] = True
            row["margin_min"] = None
            row["limiting"] = "none (roads and structure never close)"
        else:
            row["margin_min"] = float(cap - t0)
            row["safe_exit"] = bool(row["margin_min"] >= 0)
            if limiting == "water":
                row["limiting"] = "water reaches the structure at {:.0f} min".format(water)
            elif lim is not None and lim >= 0:
                row["limiting_edge"] = int(lim)
                row["limiting_road"] = _road_name(graph, lim)
                row["limiting_close_min"] = float(graph.eclose[lim])
                row["limiting_x"] = float((graph.ex0[lim] + graph.ex1[lim]) / 2.0)
                row["limiting_y"] = float((graph.ey0[lim] + graph.ey1[lim]) / 2.0)
                row["limiting"] = "road closes at {:.0f} min{}".format(
                    graph.eclose[lim], " ({})".format(row["limiting_road"])
                    if row["limiting_road"] else "")
            else:
                row["limiting"] = "road closes"
        if annotate:
            rec["evac_margin_min"], rec["evac_safe_exit"] = row["margin_min"], row["safe_exit"]
            rec["evac_time_min"], rec["evac_note"] = dur, row["limiting"]
    return rows


def _summary(rows, tight):
    ev = [r for r in rows if r["status"] == STATUS_OK]
    neg = [r for r in ev if r["margin_min"] is not None and r["margin_min"] < 0]
    tightr = [r for r in ev if r["margin_min"] is not None and 0 <= r["margin_min"] < tight]
    unlimited = [r for r in ev if r["margin_min"] is None]
    margins = [r["margin_min"] for r in ev if r["margin_min"] is not None]
    by_road = defaultdict(lambda: [0, 0.0])
    for r in neg:
        key = r["limiting_road"] or ("water reaches the structure" if r["limiting_edge"] is None
                                     and r["limiting"].startswith("water") else
                                     "unnamed road")
        by_road[key][0] += 1
        by_road[key][1] += r["persons"] or 0
    top = sorted(by_road.items(), key=lambda kv: (-kv[1][1], -kv[1][0], kv[0]))[:3]
    return {
        "structures": len(rows), "evaluated": len(ev),
        "persons": sum(r["persons"] or 0 for r in rows),
        "persons_evaluated": sum(r["persons"] or 0 for r in ev),
        "negative": len(neg), "persons_negative": sum(r["persons"] or 0 for r in neg),
        "negative_water": sum(1 for r in neg if r["limiting"].startswith("water")),
        "negative_road": sum(1 for r in neg if not r["limiting"].startswith("water")),
        "tight": len(tightr), "persons_tight": sum(r["persons"] or 0 for r in tightr),
        "unlimited": len(unlimited),
        "no_road": sum(1 for r in rows if r["status"] == STATUS_NO_ROAD),
        "no_exit": sum(1 for r in rows if r["status"] == STATUS_NO_EXIT),
        "min_margin": min(margins) if margins else None,
        "median_margin": float(np.median(margins)) if margins else None,
        "limiting_roads": [(k, v[0], v[1]) for k, v in top],
    }


def evacuation_timing(road_layer, structs, depth_sampler, arrival_sampler, transform_ctx,
                      warning_issued_hr=DEFAULT_WARNING_ISSUED_HR, prep_min=DEFAULT_PREP_MIN,
                      speed_mph=DEFAULT_VEHICLE_SPEED_MPH,
                      passable_depth_ft=DEFAULT_PASSABLE_DEPTH_FT,
                      access_ft=DEFAULT_ACCESS_SEARCH_FT,
                      access_leg_mph=DEFAULT_ACCESS_LEG_MPH, include="flooded", period="night",
                      dry_safe_ft=None, tight_margin_min=DEFAULT_TIGHT_MARGIN_MIN,
                      piece_ft=DEFAULT_EDGE_PIECE_FT, filter_rect=None, lat=None,
                      annotate=True, progress_cb=None, keep_routes=False):
    """Evacuation timing of the habitable structures. `structs`: the pipeline's
    structure records (rec["geom"] in the depth raster's CRS, "habitable",
    "max_depth", "arrival" in hours, persons / population fields). Returns a
    result dict: "status" ("ok" or "skipped"), "message", "rows" (per structure),
    "summary" (counts), "assumptions", "graph" stats. Missing inputs (no road
    layer, no arrival raster ...) give status "skipped" with the reason - never
    an exception. keep_routes (v2.9): keep the road graph and the search in
    res["_graph"] for routes_layer / closures_layer / route_of (route_layers
    releases it again)."""
    res = {"status": "skipped", "message": "", "rows": [], "summary": {}, "graph": {},
           "assumptions": {}}
    for rec in structs:
        if annotate:
            rec["evac_margin_min"], rec["evac_safe_exit"] = None, None
            rec["evac_time_min"], rec["evac_note"] = None, ""
    if road_layer is None or not road_layer.isValid():
        res["message"] = "no road layer - evacuation timing not computed"
        return res
    if depth_sampler is None or not getattr(depth_sampler, "ok", False):
        res["message"] = "the depth raster cannot be read - evacuation timing not computed"
        return res
    if arrival_sampler is None or not getattr(arrival_sampler, "ok", False):
        res["message"] = ("no arrival-time raster - evacuation timing needs the time the "
                          "flood reaches each cell; not computed")
        return res
    if include not in INCLUDE_MODES:
        include = "flooded"
    try:
        graph = build_road_graph(road_layer, depth_sampler, arrival_sampler, transform_ctx,
                                 filter_rect, lat, speed_mph, passable_depth_ft, piece_ft,
                                 dry_safe_ft, progress_cb)
    except Exception as e:                       # noqa: BLE001 - a bad layer must not stop the run
        if type(e).__name__ == "CancelledByUser":
            raise
        res["message"] = "evacuation timing failed while reading the roads: {}".format(e)
        return res
    res["graph"] = graph.stats
    if not graph.ok:
        res["message"] = "evacuation timing not computed: {}".format(graph.message)
        return res
    if progress_cb:
        progress_cb(70.0, "Evacuation: searching")
    rows = evaluate_structures(graph, structs, warning_issued_hr, prep_min, access_ft,
                               access_leg_mph, include, period, annotate)
    if keep_routes:
        res["_graph"] = graph
    res.update({"status": "ok", "rows": rows,
                "summary": _summary(rows, tight_margin_min),
                "assumptions": {
                    "depart_min": float(warning_issued_hr) * 60.0 + float(prep_min),
                    "warning_issued_hr": float(warning_issued_hr), "prep_min": float(prep_min),
                    "speed_mph": float(speed_mph), "passable_depth_ft": float(passable_depth_ft),
                    "access_leg_mph": float(access_leg_mph), "include": include,
                    "access_ft": float(access_ft), "piece_ft": float(piece_ft),
                    "tight_margin_min": float(tight_margin_min), "period": period,
                    "dry_safe_ft": dry_safe_ft}})
    if not rows:
        res["message"] = "no habitable structure in scope ({})".format(include)
    return res


# ----------------------------------------------------------------------- output --
def summary_lines(res):
    """Summary text lines for the run (a single line when skipped)."""
    if res.get("status") != "ok":
        return ["Evacuation timing: {}".format(res.get("message") or "not computed")]
    a, s, g = res["assumptions"], res["summary"], res["graph"]
    scope = {"flooded": "flooded", "near": "flooded or near-flooded",
             "all": "all"}[a["include"]]
    lines = [
        "Evacuation timing (screening table, not a life-safety model): {} {} habitable "
        "structure(s) ({:,.0f} persons). Warning issued {:g} h after breach start + {:g} min "
        "preparation = leaving {:.0f} min after breach start; motorists {:g} mph; a road piece "
        "is closed from the first arrival of a cell at least {:g} ft deep on it; safe = a "
        "road node outside the depth raster{}".format(
            s["structures"], scope, s["persons"], a["warning_issued_hr"], a["prep_min"],
            a["depart_min"], a["speed_mph"], a["passable_depth_ft"],
            " or dry with a dry ring of {:g} ft".format(a["dry_safe_ft"])
            if a.get("dry_safe_ft") else "")]
    if not s["evaluated"]:
        lines.append("  no structure could be placed on a road with a route to a safe place "
                     "({} without a mapped road within {:g} ft, {} without a route)".format(
                         s["no_road"], a["access_ft"], s["no_exit"]))
    else:
        lines.append(
            "  cannot leave in time (the water reaches the structure, or every route out "
            "closes, before they can leave): {} structure(s), {:,.0f} persons ({} because of "
            "the roads, {} because the water reaches the structure first)".format(
                s["negative"], s["persons_negative"], s["negative_road"], s["negative_water"]))
        lines.append("  tight (margin under {:g} min): {} structure(s), {:,.0f} persons; no "
                     "limit (nothing closes): {}; smallest margin {}, median {}".format(
                         a["tight_margin_min"], s["tight"], s["persons_tight"], s["unlimited"],
                         "-" if s["min_margin"] is None else "{:.0f} min".format(s["min_margin"]),
                         "-" if s["median_margin"] is None
                         else "{:.0f} min".format(s["median_margin"])))
        if s["limiting_roads"]:
            lines.append("  roads that close first for those who cannot leave: " + "; ".join(
                "{} ({} structure(s), {:,.0f} persons)".format(k, n, p)
                for k, n, p in s["limiting_roads"]))
        if s["no_road"] or s["no_exit"]:
            lines.append("  not evaluated: {} without a mapped road within {:g} ft, {} with no "
                         "route to a safe place on the mapped roads".format(
                             s["no_road"], a["access_ft"], s["no_exit"]))
    if g.get("edges_no_arrival"):
        lines.append("  {} road piece(s) are deep enough to close but have no arrival time "
                     "(treated as closed from the start)".format(g["edges_no_arrival"]))
    lines.append(CAVEATS[0])
    return lines


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, (bool, np.bool_)):
        return "Yes" if v else "No"
    if isinstance(v, (float, np.floating)):
        return round(float(v), 2)
    return v


def write_csv(folder, res):
    """QA/Evacuation_Timing.csv (one row per structure evaluated); the path, or
    None when there is nothing to write."""
    if res.get("status") != "ok" or not res.get("rows"):
        return None
    path = prepare_output_path(os.path.join(folder, CSV_NAME))
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(HEAD)
        for r in res["rows"]:
            w.writerow([_cell(v) for v in (
                r["id"], r["persons"], r["max_depth"], r["status"], r["safe_exit"],
                r["depart_min"], r["water_min"], r["road_latest_min"], r["latest_min"],
                r["margin_min"], r["evac_time_min"], r["limiting"], r["limiting_road"],
                r["limiting_edge"], r["limiting_close_min"], r["limiting_x"], r["limiting_y"],
                r["road_dist_ft"])])
    return path


def run_evacuation(road_layer, structs, depth_sampler, arrival_sampler, transform_ctx,
                   out_folder=None, log=None, **opts):
    """evacuation_timing + summary lines + QA/Evacuation_Timing.csv.
    Returns (lines, written_paths, result); never raises for bad input."""
    res = evacuation_timing(road_layer, structs, depth_sampler, arrival_sampler,
                            transform_ctx, **opts)
    lines = summary_lines(res)
    written = []
    if res["status"] == "ok" and out_folder:
        try:
            os.makedirs(out_folder, exist_ok=True)
            p = write_csv(out_folder, res)
            if p:
                written.append(p)
        except OSError as e:
            lines.append("Evacuation timing file not written: {}".format(e))
    if log:
        if res["status"] != "ok":
            log(">>> NOTE: {}".format(lines[0]))
        elif res["summary"].get("negative"):
            log(">>> NOTE: evacuation timing: {} structure(s) ({:,.0f} persons) cannot leave in "
                "time in this screening - see the summary / QA/{}".format(
                    res["summary"]["negative"], res["summary"]["persons_negative"], CSV_NAME))
    return lines, written, res


# ------------------------------------------------------------ v2.9: route layers --
ROUTES_LAYER = "EAP_Evacuation_Routes"
CLOSURES_LAYER = "EAP_Road_Closures"
ROUTE_FIELDS = [("Persons", "d"), ("Status", "s"), ("Safe_Exit_Possible", "s"),
                ("Depart_min", "d"), ("Water_Arrival_min", "d"),
                ("Road_Latest_Departure_min", "d"), ("Latest_Departure_min", "d"),
                ("Margin_min", "d"), ("Evac_Time_min", "d"), ("Route_Length_ft", "d"),
                ("Limiting", "s"), ("Limiting_Road", "s"), ("Limiting_Edge", "i"),
                ("Exit_X", "d"), ("Exit_Y", "d")]
CLOSURE_FIELDS = [("Edge", "i"), ("Road", "s"), ("Close_min", "d"), ("Close_hhmm", "s"),
                  ("Max_Depth_ft", "d"), ("No_Arrival_Time", "s"), ("Length_ft", "d"),
                  ("Limits_Structures", "i"), ("Limits_Persons", "d")]


def route_of(res, row):
    """The route of one timing row (from its structure to the safe place), rebuilt
    from the search kept by evacuation_timing(keep_routes=True): {"points":
    [(x, y)] structure -> road -> ... -> safe node (depth raster CRS), "edges":
    [edge] in driving order (the piece it joins first), "length_ft",
    "time_min" (= the row's Evac_Time_min), "road_latest_min" (latest departure
    from the closures along THIS route; None = nothing on it closes), "exit_node"}.
    None when the row has no route or the graph was not kept / was released."""
    g = (res or {}).get("_graph")
    info = (row or {}).get("_route")
    if g is None or info is None or getattr(g, "nxt_node", None) is None:
        return None
    x, y, qx, qy, e, node, frac, leg_ft, lam = info
    pts = [(x, y), (qx, qy)]
    s_min = frac * float(g.elen_ft[e]) / g.speed_ft_min
    t = lam + s_min
    length = leg_ft + frac * float(g.elen_ft[e])
    inf = float("inf")
    latest = min(inf, float(g.eclose[e]) - t)
    edges = [int(e)]
    n = int(node)
    pts.append((float(g.node_x[n]), float(g.node_y[n])))
    steps = 0
    while not g.safe[n]:
        k = int(g.nxt_edge[n])
        m = int(g.nxt_node[n])
        if k < 0 or m < 0 or steps > g.n_nodes:        # no route (never for a routed row)
            return None
        t += float(g.etau[k])
        length += float(g.elen_ft[k])
        latest = min(latest, float(g.eclose[k]) - t)
        edges.append(k)
        n = m
        pts.append((float(g.node_x[n]), float(g.node_y[n])))
        steps += 1
    clean = [pts[0]]
    for p in pts[1:]:
        if p != clean[-1]:
            clean.append(p)
    return {"points": clean, "edges": edges, "length_ft": length, "time_min": t,
            "road_latest_min": None if latest == inf else latest, "exit_node": n}


def _qfields(spec, id_type=None):
    from qgis.core import QgsField
    from .compat import FT_DOUBLE, FT_INT, FT_STRING
    kinds = {"d": FT_DOUBLE, "i": FT_INT, "s": FT_STRING}
    out = [QgsField("Struct_ID", id_type)] if id_type is not None else []
    return out + [QgsField(n, kinds[k]) for n, k in spec]


def _num(v, nd=2):
    from .compat import NULL
    if v is None:
        return NULL
    try:
        f = float(v)
    except (TypeError, ValueError):
        return NULL
    return round(f, nd) if math.isfinite(f) else NULL


def _yes_no(v):
    from .compat import NULL
    return NULL if v is None else ("Yes" if v else "No")


def _crs_of(res, crs):
    if crs is not None:
        return crs
    g = (res or {}).get("_graph")
    return getattr(g, "crs", None)


def routes_layer(res, crs=None, name=ROUTES_LAYER):
    """Memory line layer EAP_Evacuation_Routes: one feature per structure in the
    timing table (the same rows as QA/Evacuation_Timing.csv, same rounded
    numbers), the line from the structure to the road and along the route of
    the LATEST departure to the safe place. Rows without a route ("no mapped
    road ..." / "no route ...") have no geometry. None when there is nothing
    to show or the routes were not kept (evacuation_timing(keep_routes=True)).
    crs: the layer CRS - the depth raster's (the graph's) by default, which is
    the analysis CRS in the pipeline; geometries are never re-projected here."""
    from qgis.core import QgsFeature, QgsGeometry, QgsPointXY
    from .compat import FT_INT, FT_STRING, WKB_LINESTRING, NULL
    from .helpers import create_memory_layer, add_features_batched
    if (res or {}).get("status") != "ok" or not res.get("rows") or res.get("_graph") is None:
        return None
    rows = res["rows"]
    ids = [r["id"] for r in rows]
    id_int = all(isinstance(i, (int, np.integer)) and not isinstance(i, bool) for i in ids)
    lyr = create_memory_layer(name, WKB_LINESTRING, _crs_of(res, crs),
                              _qfields(ROUTE_FIELDS, FT_INT if id_int else FT_STRING))
    feats = []
    for r in rows:
        route = route_of(res, r) if r["status"] == STATUS_OK else None
        f = QgsFeature(lyr.fields())
        if route is not None and len(route["points"]) >= 2:
            f.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(px, py)
                                                      for px, py in route["points"]]))
        ex = ey = None
        if route is not None:
            g = res["_graph"]
            ex, ey = float(g.node_x[route["exit_node"]]), float(g.node_y[route["exit_node"]])
        f.setAttributes([
            int(r["id"]) if id_int else ("" if r["id"] is None else str(r["id"])),
            _num(r["persons"]), r["status"], _yes_no(r["safe_exit"]),
            _num(r["depart_min"]), _num(r["water_min"]), _num(r["road_latest_min"]),
            _num(r["latest_min"]), _num(r["margin_min"]), _num(r["evac_time_min"]),
            _num(route["length_ft"], 1) if route is not None else NULL,
            r["limiting"] or NULL, r["limiting_road"] or NULL,
            NULL if r["limiting_edge"] is None else int(r["limiting_edge"]),
            _num(ex, 3), _num(ey, 3)])
        feats.append(f)
    add_features_batched(lyr.dataProvider(), feats, 5000)
    lyr.updateExtents()
    return lyr


def _hhmm(minutes):
    m = int(round(float(minutes)))
    sign = "-" if m < 0 else ""
    m = abs(m)
    return "{}{}:{:02d}".format(sign, m // 60, m % 60)


def closures_layer(res, crs=None, name=CLOSURES_LAYER):
    """Memory line layer EAP_Road_Closures: every road piece of the evacuation
    network that closes (a sample at least the passable depth deep), with the
    closure time in minutes after breach start (Close_min; the first arrival
    there - 0 when a deep sample has no arrival time, No_Arrival_Time = Yes),
    its deepest sample (Max_Depth_ft), and how many structures / persons of the
    timing table this piece limits (Limiting_Edge in Evacuation_Timing.csv =
    Edge here). None when nothing closes or the graph was not kept."""
    from qgis.core import QgsFeature, QgsGeometry, QgsPointXY
    from .compat import WKB_LINESTRING, NULL
    from .helpers import create_memory_layer, add_features_batched
    g = (res or {}).get("_graph")
    if (res or {}).get("status") != "ok" or g is None:
        return None
    closing = np.nonzero(np.isfinite(g.eclose))[0]
    if not len(closing):
        return None
    limits = defaultdict(lambda: [0, 0.0])
    for r in res.get("rows") or []:
        if r.get("limiting_edge") is not None and r.get("margin_min") is not None:
            limits[int(r["limiting_edge"])][0] += 1
            limits[int(r["limiting_edge"])][1] += r.get("persons") or 0.0
    lyr = create_memory_layer(name, WKB_LINESTRING, _crs_of(res, crs), _qfields(CLOSURE_FIELDS))
    feats = []
    for e in closing.tolist():
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(float(g.ex0[e]), float(g.ey0[e])),
                                                  QgsPointXY(float(g.ex1[e]), float(g.ey1[e]))]))
        n, p = limits.get(e, (0, 0.0))
        f.setAttributes([int(e), _road_name(g, e) or NULL, _num(g.eclose[e]),
                         _hhmm(g.eclose[e]), _num(g.emaxd[e]),
                         "Yes" if bool(g.eunk[e]) else "No", _num(g.elen_ft[e], 1),
                         int(n), _num(p)])
        feats.append(f)
    add_features_batched(lyr.dataProvider(), feats, 5000)
    lyr.updateExtents()
    return lyr


def release_routes(res):
    """Drop the kept road graph and the route data from a result (memory)."""
    if res:
        res.pop("_graph", None)
        for r in res.get("rows") or []:
            r.pop("_route", None)
    return res


def route_layers(res, crs=None, release=True):
    """(EAP_Evacuation_Routes, EAP_Road_Closures) memory layers (either may be
    None) - then, with `release`, the kept graph is dropped (release_routes).
    Never raises: a failure gives (None, None) and is logged."""
    routes = closures = None
    try:
        routes = routes_layer(res, crs)
        closures = closures_layer(res, crs)
    except Exception as e:                        # noqa: BLE001 - never fails the run
        from .helpers import log_message
        log_message("Evacuation route layers not made: {}".format(e))
        routes = closures = None
    finally:
        if release:
            release_routes(res)
    return routes, closures


def route_summary_line(routes, closures):
    """One summary line for the two layers (empty when neither exists)."""
    parts = []
    if routes is not None:
        parts.append("{} ({} structure(s))".format(ROUTES_LAYER, routes.featureCount()))
    if closures is not None:
        parts.append("{} ({} road piece(s) that close)".format(CLOSURES_LAYER,
                                                              closures.featureCount()))
    if not parts:
        return ""
    return ("  map layers: " + "; ".join(parts) + " - the route drawn is the one with the "
            "latest possible departure (screening)")
