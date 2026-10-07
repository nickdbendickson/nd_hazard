# -*- coding: utf-8 -*-
"""Sole-access screening (ND REG_05.2026 5.2.1.2: a road that is the only
access to occupied locations).

The road network is cut into pieces at its vertices and wherever the flood
starts or ends along a road (sampled every LINE_SAMPLE_STEP_FT); a piece is
cut when it is wet in the depth raster ("overtopped at any depth"). Line
ends that stop on another road without a shared vertex (T-junctions) are
joined within SNAP_FT. A structure counts as cut off when the road piece
next to it is flooded, or when its mapped roads reach outside the model
area without the flood but not with it (a network that never leaves the
model area gives "unknown", not "cut off").
Dry segments are joined into connected pieces of network. A piece "reaches
out" when one of its vertices lies outside the depth raster (or within
EXIT_MARGIN_FT of its edge) - the road leaves the modelled area. Every
habitable structure is attached to the nearest road segment within the
access distance:
  - nearest segment cut          -> access cut (its own road is overtopped)
  - on a piece that reaches out  -> access kept
  - on an isolated piece         -> access cut (all routes out are flooded)
  - no road within the distance  -> unknown
This is a screening of the mapped network; private drives, trails and
bridges above the water surface need the engineer's review.
"""

import math

import numpy as np
from qgis.core import QgsCoordinateTransform, QgsFeatureRequest, QgsRectangle

from .constants import LINE_SAMPLE_STEP_FT, DEFAULT_ACCESS_SEARCH_FT
from .helpers import log_message, transform_geometry, LimitedLogger
from .compat import MSG_WARNING
from .extent import distance_xy_in_crs
from .linear import line_parts

EXIT_MARGIN_FT = 100.0
SNAP_FT = 5.0                # dangling road ends this close to another road are joined


BATCH_POINTS = 2000000          # v2.6.2: points per depth read (memory bound)


def _project(px, py, c, ks=None):
    """(distance, segment index, t, x, y) of the closest point of polyline
    c (n x 2 array) to (px, py); only segments `ks` when given."""
    if ks is None:
        ks = np.arange(len(c) - 1)
    ks = np.asarray(ks, dtype=np.int64)
    x0, y0, x1, y1 = c[ks, 0], c[ks, 1], c[ks + 1, 0], c[ks + 1, 1]
    dx, dy = x1 - x0, y1 - y0
    L2 = dx * dx + dy * dy
    t = np.where(L2 > 0, ((px - x0) * dx + (py - y0) * dy) / np.where(L2 > 0, L2, 1.0), 0.0)
    t = np.clip(t, 0.0, 1.0)
    qx, qy = x0 + t * dx, y0 + t * dy
    dist = np.hypot(px - qx, py - qy)
    i = int(np.argmin(dist))
    return float(dist[i]), int(ks[i]), float(t[i]), float(qx[i]), float(qy[i])


def _segment_grid(parts, cell):
    """{cell: {part: [segment indices]}}. Each segment is registered in the
    cells of points spaced <= one cell along it, so a point within `cell`
    of the segment is always found in the 3 x 3 cells around it (cost grows
    with length, not with the bounding box of a long diagonal road)."""
    grid = {}
    for pi, c in enumerate(parts):
        for k in range(len(c) - 1):
            x0, y0 = c[k]
            x1, y1 = c[k + 1]
            n = max(1, int(math.ceil(math.hypot(x1 - x0, y1 - y0) / cell)))
            seen = set()
            for i in range(n + 1):
                f = i / float(n)
                key = (int(math.floor((x0 + f * (x1 - x0)) / cell)),
                       int(math.floor((y0 + f * (y1 - y0)) / cell)))
                if key not in seen:
                    seen.add(key)
                    grid.setdefault(key, {}).setdefault(pi, []).append(k)
    return grid


def _node_line_ends(parts, snap):
    """Join line ends that stop on (within `snap` of) another line without
    a shared vertex: the end is moved onto that line and a vertex is
    inserted there."""
    if len(parts) < 2 or snap <= 0:
        return parts
    cell = snap * 50.0
    grid = _segment_grid(parts, cell)
    inserts = {}
    parts = [c.copy() for c in parts]
    for pi, c in enumerate(parts):
        for end in (0, -1):
            x, y = c[end]
            gx, gy = int(math.floor(x / cell)), int(math.floor(y / cell))
            cands = {}
            for i in (gx - 1, gx, gx + 1):
                for j in (gy - 1, gy, gy + 1):
                    for pj, ks in grid.get((i, j), {}).items():
                        if pj != pi:
                            cands.setdefault(pj, set()).update(ks)
            best = None
            for pj, ks in cands.items():
                dist, k, t, qx, qy = _project(x, y, parts[pj], sorted(ks))
                if dist <= snap and (best is None or dist < best[0]):
                    best = (dist, pj, k, t, qx, qy)
            if best is None or best[0] == 0.0 and _is_vertex(parts[best[1]], best[4], best[5]):
                continue
            _d, pj, k, t, qx, qy = best
            c[end] = (qx, qy)
            if 0.0 < t < 1.0:
                inserts.setdefault(pj, []).append((k, t, qx, qy))
    for pj, ins in inserts.items():
        c = parts[pj]
        pts = [tuple(p) for p in c]
        extra = {}
        for k, t, qx, qy in ins:
            extra.setdefault(k, []).append((t, qx, qy))
        out = []
        for k in range(len(pts) - 1):
            out.append(pts[k])
            for _t, qx, qy in sorted(extra.get(k, [])):
                out.append((qx, qy))
        out.append(pts[-1])
        parts[pj] = np.asarray(out, dtype=np.float64)
    return parts


def _is_vertex(c, x, y):
    return bool(np.any((c[:, 0] == x) & (c[:, 1] == y)))


class _UnionFind(object):
    def __init__(self):
        self.parent = []

    def add(self):
        self.parent.append(len(self.parent))
        return len(self.parent) - 1

    def find(self, a):
        p = self.parent
        root = a
        while p[root] != root:
            root = p[root]
        while p[a] != root:
            p[a], a = root, p[a]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _ft(ft, crs, lat):
    return max(distance_xy_in_crs(ft, crs, lat))


def _seg_dist(px, py, x0, y0, x1, y1):
    dx, dy = x1 - x0, y1 - y0
    L2 = dx * dx + dy * dy
    t = np.where(L2 > 0, ((px - x0) * dx + (py - y0) * dy) / np.where(L2 > 0, L2, 1.0), 0.0)
    t = np.clip(t, 0.0, 1.0)
    return np.hypot(px - (x0 + t * dx), py - (y0 + t * dy))


def access_analysis(road_layer, structs, depth_sampler, analysis_crs, transform_ctx,
                    clip=None, access_ft=DEFAULT_ACCESS_SEARCH_FT, lat=None,
                    progress_cb=None):
    """Sets rec["cut_off"] (True / False / None) and rec["access_note"] on
    habitable structures. Returns a stats dict (None if not evaluated)."""
    for rec in structs:
        rec["cut_off"], rec["access_note"] = None, ""
    if road_layer is None or not road_layer.isValid() or not depth_sampler.ok:
        return None
    step = _ft(LINE_SAMPLE_STEP_FT, analysis_crs, lat)
    tol = _ft(0.5, analysis_crs, lat)
    reach = _ft(access_ft, analysis_crs, lat)
    margin = _ft(EXIT_MARGIN_FT, analysis_crs, lat)
    gt, nx, ny = depth_sampler.gt, depth_sampler.xsize, depth_sampler.ysize
    xs_c = [gt[0] + c * gt[1] + r * gt[2] for c in (0, nx) for r in (0, ny)]
    ys_c = [gt[3] + c * gt[4] + r * gt[5] for c in (0, nx) for r in (0, ny)]
    margin = min(margin, 0.05 * min(max(xs_c) - min(xs_c), max(ys_c) - min(ys_c)))
    inner = (min(xs_c) + margin, min(ys_c) + margin, max(xs_c) - margin, max(ys_c) - margin)

    to_a = QgsCoordinateTransform(road_layer.crs(), analysis_crs, transform_ctx)
    req = QgsFeatureRequest()
    if clip is not None:
        dx, dy = distance_xy_in_crs(5280.0, clip.crs, clip.lat)    # roads a mile beyond
        big = QgsRectangle(clip.rect.xMinimum() - dx, clip.rect.yMinimum() - dy,
                           clip.rect.xMaximum() + dx, clip.rect.yMaximum() + dy)
        try:
            back = QgsCoordinateTransform(clip.crs, road_layer.crs(), transform_ctx)
            req.setFilterRect(big if back.isShortCircuited() else back.transformBoundingBox(big))
        except Exception:
            pass

    uf = _UnionFind()
    nodes, exits = {}, set()

    def node(x, y):
        k = (int(round(x / tol)), int(round(y / tol)))
        n = nodes.get(k)
        if n is None:
            n = nodes[k] = uf.add()
            if not (inner[0] <= x <= inner[2] and inner[1] <= y <= inner[3]):
                exits.add(n)
        return n

    problems = LimitedLogger("Access network")
    total = max(1, road_layer.featureCount())
    parts = []
    for i, feat in enumerate(road_layer.getFeatures(req)):
        if progress_cb and i % 200 == 0:
            progress_cb(min(60.0, 60.0 * i / total), "Road network: {} of {}".format(i, total))
        g = transform_geometry(feat.geometry(), to_a)
        if g is None or g.isNull() or g.isEmpty():
            continue
        try:
            for coords in line_parts(g):
                c = np.asarray(coords, dtype=np.float64)
                if len(c) >= 2:
                    parts.append(c)
        except Exception as e:
            problems.log("feature {}: {}".format(feat.id(), e))
    # T-junctions digitised without a shared vertex: a dangling line end
    # within SNAP_FT of another road is joined to it
    parts = _node_line_ends(parts, _ft(SNAP_FT, analysis_crs, lat))

    # every road is cut into pieces at its vertices and wherever the flood
    # starts or ends along it; dry pieces connect their end nodes
    def densify(c):
        seg_len = np.hypot(np.diff(c[:, 0]), np.diff(c[:, 1]))
        px, py, is_v = [], [], []
        for k in range(len(c) - 1):
            n = max(1, int(math.ceil(seg_len[k] / step)))
            t = np.arange(n, dtype=np.float64) / n
            px.append(c[k, 0] + t * (c[k + 1, 0] - c[k, 0]))
            py.append(c[k, 1] + t * (c[k + 1, 1] - c[k, 1]))
            v = np.zeros(n, dtype=bool)
            v[0] = True
            is_v.append(v)
        px.append(c[-1:, 0])
        py.append(c[-1:, 1])
        is_v.append(np.array([True]))
        return np.concatenate(px), np.concatenate(py), np.concatenate(is_v)

    # v2.6.2: the depth of many road pieces is read in one call (one raster
    # window per group of nearby points) instead of one read per piece - the
    # same cells and values. The QGIS-provider reader keeps one read per
    # piece (its large windows are slow).
    batch = getattr(depth_sampler, "backend", None) == "gdal"
    edges = []                   # (x0, y0, x1, y1, wet, node_a)
    pi = 0
    while pi < len(parts):
        chunk, npts = [], 0
        while pi < len(parts) and (not chunk or (batch and npts < BATCH_POINTS)):
            if progress_cb and pi % 500 == 0:       # (a Cancel raises here)
                progress_cb(60.0 + 15.0 * pi / max(1, len(parts)), "Road network: flooding")
            px, py, is_v = densify(parts[pi])
            chunk.append((pi, px, py, is_v))
            npts += len(px)
            pi += 1
        if len(chunk) > 1:
            d_all = depth_sampler.values_at(np.concatenate([ch[1] for ch in chunk]),
                                            np.concatenate([ch[2] for ch in chunk]))
            cuts = np.cumsum([len(ch[1]) for ch in chunk])[:-1]
            depths = np.split(d_all, cuts)
        else:
            depths = [depth_sampler.values_at(chunk[0][1], chunk[0][2])]
        for (idx, px, py, is_v), d in zip(chunk, depths):
            if progress_cb and idx % 500 == 0:
                progress_cb(75.0 + 15.0 * idx / max(1, len(parts)), "Road network: flooding")
            wet_pt = np.isfinite(d) & (d > 0)
            sub_wet = wet_pt[:-1] | wet_pt[1:]
            prev = node(px[0], py[0])
            x0, y0 = px[0], py[0]
            last = len(px) - 1
            for i in range(last):
                w = bool(sub_wet[i])
                j = i + 1
                if not (is_v[j] or j == last or bool(sub_wet[j]) != w):
                    continue
                n1 = node(px[j], py[j])
                edges.append((x0, y0, px[j], py[j], w, prev, n1))
                if not w:
                    uf.union(prev, n1)
                prev, x0, y0 = n1, px[j], py[j]
    problems.summary()
    if not edges:
        return None
    if not exits:
        log_message("Sole-access screening skipped: no road leaves the depth raster "
                    "(load a road layer that extends beyond the model area)", MSG_WARNING)
        return None
    out_roots = {uf.find(n) for n in exits}
    # the same network without the flood: a structure whose mapped roads do
    # not reach outside the model area even when dry is "unknown", not cut
    full = _UnionFind()
    full.parent = list(range(len(uf.parent)))
    for e in edges:
        full.union(e[5], e[6])
    full_out = {full.find(n) for n in exits}

    E = np.array([e[:4] for e in edges], dtype=np.float64)
    wet = np.array([e[4] for e in edges], dtype=bool)
    ea = np.array([e[5] for e in edges])
    cell = max(reach, 1e-9)
    grid = {}
    for idx in range(len(E)):
        x0, y0, x1, y1 = E[idx]
        for gx in range(int(math.floor(min(x0, x1) / cell)), int(math.floor(max(x0, x1) / cell)) + 1):
            for gy in range(int(math.floor(min(y0, y1) / cell)),
                            int(math.floor(max(y0, y1) / cell)) + 1):
                grid.setdefault((gx, gy), []).append(idx)

    # v2.6: every structure is placed on the network (a structure that an
    # engineer later marks habitable then has its answer already); the
    # statistics count the habitable ones (access_stats, repeatable)
    for rec in structs:
        rec["access_unknown"] = False
        rec["access_cut"] = None
        rec["access_raw_note"] = ""
        try:
            p = rec["geom"].centroid().asPoint()
            x, y = p.x(), p.y()
        except Exception:
            continue
        gx, gy = int(math.floor(x / cell)), int(math.floor(y / cell))
        cand = set()
        for i in (gx - 1, gx, gx + 1):
            for j in (gy - 1, gy, gy + 1):
                cand.update(grid.get((i, j), ()))
        if not cand:
            rec["access_unknown"] = True
            rec["access_raw_note"] = "no mapped road within {:g} ft".format(access_ft)
            continue
        cand = np.fromiter(cand, dtype=np.int64)
        dist = _seg_dist(x, y, E[cand, 0], E[cand, 1], E[cand, 2], E[cand, 3])
        k = int(np.argmin(dist))
        if dist[k] > reach:
            rec["access_unknown"] = True
            rec["access_raw_note"] = "no mapped road within {:g} ft".format(access_ft)
            continue
        e = int(cand[k])
        if wet[e]:
            rec["access_cut"] = True
            rec["access_raw_note"] = "nearest road overtopped"
        elif full.find(int(ea[e])) not in full_out:
            # isolated even without the flood: the mapped network is incomplete
            rec["access_unknown"] = True
            rec["access_raw_note"] = "mapped roads do not leave the model area even without flooding"
            continue
        else:
            rec["access_cut"] = uf.find(int(ea[e])) not in out_roots
            rec["access_raw_note"] = ("every mapped route out crosses flooded road"
                                  if rec["access_cut"] else "")
    stats = access_stats(structs, len(edges), int(wet.sum()))
    log_message("Sole-access screening: {} of {} habitable structures cut off ({} of them "
                "dry); {} of {} road segments overtopped".format(
                    stats["cut"], stats["structures"], stats["cut_dry"], stats["edges_cut"],
                    stats["edges"]))
    return stats


def access_stats(structs, edges, edges_cut):
    """The sole-access statistics of the habitable structures, from the
    per-structure results of access_analysis (recomputed after an engineer
    review changes which structures are habitable). Sets rec["cut_off"]:
    the network answer (access_cut) for habitable structures only, as
    before 2.6."""
    stats = {"structures": 0, "cut": 0, "cut_dry": 0, "persons_cut_dry": 0, "unknown": 0,
             "edges": int(edges), "edges_cut": int(edges_cut)}
    for rec in structs:
        if rec.get("habitable") != "Yes" or rec.get("access_unknown") \
                or rec.get("access_cut") is None:
            rec["cut_off"] = None                 # not a habitable structure / no road
            if rec.get("habitable") != "Yes":
                rec["access_note"] = ""           # only habitable structures carry a note
                continue
            rec["access_note"] = rec.get("access_raw_note") or ""
        else:
            rec["cut_off"] = bool(rec["access_cut"])
            rec["access_note"] = rec.get("access_raw_note") or ""
        stats["structures"] += 1
        if rec.get("access_unknown"):
            stats["unknown"] += 1
        elif rec.get("cut_off"):
            stats["cut"] += 1
            if not rec.get("max_depth") or rec["max_depth"] <= 0:
                stats["cut_dry"] += 1
                stats["persons_cut_dry"] += rec.get("persons") or 0
    return stats
