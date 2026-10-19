"""Receptors (structures, roads, rail) and what the flood does to them.

ND REG_05.2026 5.1.1(f) asks for the breach model to be extended downstream
until further impacts would not change the hazard classification. Edge depth
alone is a proxy for that. With receptor layers the domain stops growing when
nothing that matters lies in the flood's path downstream, and the run report
lists what the flood reaches.

Structures are points; roads and rail are lines, sampled every `spacing`
metres so each sample stands for that much length.
"""
import csv

import numpy as np

from . import conditioning as cond
from . import terrain
from .units import FT, MILE

KIND_STRUCTURE = "structure"
KIND_ROAD = "road"
KIND_RAIL = "rail"
LINE_SPACING_M = 30.0
DV_DANGER_FT2S = 7.0          # depth x velocity above which a person is swept off their feet


class Receptors:
    def __init__(self):
        self.x = np.zeros(0)
        self.y = np.zeros(0)
        self.kind = np.zeros(0, dtype="<U16")
        self.ident = np.zeros(0, dtype=object)
        self.length_m = np.zeros(0)       # what each sample stands for (0 for a point)

    def __len__(self):
        return len(self.x)

    def _append(self, xy, kind, ids, length):
        xy = np.asarray(xy, dtype=float).reshape(-1, 2)
        n = len(xy)
        self.x = np.concatenate([self.x, xy[:, 0]])
        self.y = np.concatenate([self.y, xy[:, 1]])
        self.kind = np.concatenate([self.kind, np.full(n, kind, dtype="<U16")])
        self.ident = np.concatenate([self.ident, np.asarray(ids, dtype=object)])
        self.length_m = np.concatenate([self.length_m, np.full(n, float(length))])

    def add_points(self, xy, kind=KIND_STRUCTURE, ids=None):
        xy = np.asarray(xy, dtype=float).reshape(-1, 2)
        ids = ids if ids is not None else [str(i) for i in range(len(xy))]
        self._append(xy, kind, ids, 0.0)

    def add_lines(self, lines, kind=KIND_ROAD, ids=None, spacing=LINE_SPACING_M, unit_m=1.0):
        """`unit_m`: ground metres per coordinate unit (3.10.0: 0.3048 for a feet CRS, about
        cos(latitude) for Web Mercator); lengths are kept in metres."""
        unit_m = float(unit_m or 1.0)
        for i, ln in enumerate(lines):
            pts = cond.densify(np.asarray(ln, dtype=float), spacing / unit_m)
            if not len(pts):
                continue
            seg = (cond.polyline_length(ln) * unit_m / max(1, len(pts) - 1) if len(pts) > 1
                   else spacing)
            self._append(pts, kind, [ids[i] if ids else str(i)] * len(pts), seg)

    def subset(self, mask):
        r = Receptors()
        r.x, r.y, r.kind = self.x[mask], self.y[mask], self.kind[mask]
        r.ident, r.length_m = self.ident[mask], self.length_m[mask]
        return r

    def in_bounds(self, bounds):
        xmin, ymin, xmax, ymax = bounds
        return (self.x >= xmin) & (self.x <= xmax) & (self.y >= ymin) & (self.y <= ymax)

    def sample(self, grid, arr):
        """Value of `arr` (on `grid`) at each receptor, NaN outside."""
        c = np.floor((self.x - grid.x0) / grid.dx).astype(int)
        r = np.floor((grid.y0 - self.y) / grid.dx).astype(int)
        ok = (r >= 0) & (r < grid.ny) & (c >= 0) & (c < grid.nx)
        out = np.full(len(self.x), np.nan)
        out[ok] = arr[r[ok], c[ok]]
        return out


def count_text(rec, mask):
    """'12 structures, 3.4 km of road' for the receptors under `mask`."""
    parts = []
    for kind, label in ((KIND_STRUCTURE, "structure"), (KIND_ROAD, "road"), (KIND_RAIL, "rail")):
        m = mask & (rec.kind == kind)
        if not m.any():
            continue
        if kind == KIND_STRUCTURE:
            parts.append("{} structure{}".format(int(m.sum()), "" if m.sum() == 1 else "s"))
        else:
            parts.append("{:.1f} km of {}".format(float(rec.length_m[m].sum()) / 1000.0, label))
    return ", ".join(parts) or "nothing"


def edge_cells(dem, depth, edge, tol_m):
    """Deepest wet cell on a domain edge and the length of the wet run around it."""
    line = {"N": depth[0, :], "S": depth[-1, :], "W": depth[:, 0], "E": depth[:, -1]}[edge]
    line = np.where(np.isfinite(line), line, 0.0)
    k = int(np.argmax(line))
    lo = k
    while lo > 0 and line[lo - 1] > tol_m:
        lo -= 1
    hi = k
    while hi < len(line) - 1 and line[hi + 1] > tol_m:
        hi += 1
    r, c = {"N": (0, k), "S": (dem.ny - 1, k), "W": (k, 0), "E": (k, dem.nx - 1)}[edge]
    return dem.xy(r, c), float(line[k]), (hi - lo + 1) * dem.dx


OUTWARD = {"N": (0.0, 1.0), "S": (0.0, -1.0), "E": (1.0, 0.0), "W": (-1.0, 0.0)}


def edge_decisions(rec, terrain_src, dem, depth, over_edges, tol_m, look_ahead_m,
                   planning_cell=30.0, min_swath_m=400.0):
    """For each edge the flood leaves through deeper than the tolerance: is there
    anything downstream that would matter? Traces the flow path from the
    deepest cell on that edge for `look_ahead_m` and looks for receptors within
    a swath of it, outside the current domain.

    Returns (edges that still need modelling, {edge: info})."""
    need, info = [], {}
    bx0, by0, bx1, by1 = dem.bounds
    outside = ~rec.in_bounds((bx0, by0, bx1, by1)) if len(rec) else np.zeros(0, dtype=bool)
    for e in over_edges:
        (x, y), d_max, span = edge_cells(dem, depth, e, tol_m)
        ox, oy = OUTWARD[e]
        half = look_ahead_m + 3 * planning_cell + 2 * min_swath_m
        cx, cy = x + ox * look_ahead_m * 0.5, y + oy * look_ahead_m * 0.5
        rec_info = {"edge": e, "edge_depth_ft": round(d_max / FT, 2), "wet_span_m": round(span),
                    "look_ahead_mi": round(look_ahead_m / MILE, 1)}
        try:
            plan = terrain_src.read((cx - half, cy - half, cx + half, cy + half), planning_cell)
            path, plen = terrain.trace_downstream(plan, (x, y), look_ahead_m * 1.25)
        except Exception as ex:      # no terrain beyond: cannot tell, keep going
            rec_info.update({"extend": True, "reason": "no terrain beyond the edge to look "
                                                       "along ({})".format(str(ex)[:60])})
            info[e] = rec_info
            need.append(e)
            continue
        path = np.asarray(path)
        beyond = ~((path[:, 0] >= bx0) & (path[:, 0] <= bx1) & (path[:, 1] >= by0)
                   & (path[:, 1] <= by1))
        if not beyond.any():
            rec_info.update({"extend": True, "reason": "the flow path from the edge turns back "
                                                       "into the domain"})
            info[e] = rec_info
            need.append(e)
            continue
        first = int(np.argmax(beyond))
        line = path[max(0, first - 1):]
        swath = max(min_swath_m, 0.5 * span, 3 * planning_cell)
        d, _ = cond.polyline_distance(rec.x, rec.y, line) if len(rec) else (np.zeros(0), None)
        ahead = outside & (d <= swath)
        rec_info.update({"path_length_m": round(float(plen)), "swath_m": round(swath),
                         "ahead": count_text(rec, ahead), "extend": bool(ahead.any())})
        rec_info["reason"] = ("receptors downstream: " + rec_info["ahead"]) if ahead.any() else \
            "no structures, roads or rail within {:.1f} mi downstream".format(
                look_ahead_m / MILE)
        info[e] = rec_info
        if ahead.any():
            need.append(e)
    return need, info


def summarize(rec, grid, final, arrival_m, dv_limit=DV_DANGER_FT2S):
    """What the flood reaches. `final` holds SI arrays (m, m/s, hours) on `grid`.
    Returns (summary dict, rows for the CSV)."""
    if not len(rec):
        return {}, []
    inb = rec.in_bounds(grid.bounds)
    d = rec.sample(grid, final["depth"])
    v = rec.sample(grid, final["velocity"])
    a = rec.sample(grid, final["arrival"])
    wet = inb & np.isfinite(d) & (d >= arrival_m)
    dv = np.nan_to_num(d) / FT * np.nan_to_num(v) / FT
    summ = {"receptors_in_model_area": count_text(rec, inb),
            "receptors_flooded": count_text(rec, wet)}
    for kind in (KIND_STRUCTURE, KIND_ROAD, KIND_RAIL):
        k = wet & (rec.kind == kind)
        if not k.any():
            continue
        summ[kind] = {
            "flooded": int(k.sum()) if kind == KIND_STRUCTURE
            else round(float(rec.length_m[k].sum()) / 1000.0, 2),
            "unit": "count" if kind == KIND_STRUCTURE else "km",
            "depth_ge_1ft": int((k & (d >= 1.0 * FT)).sum()) if kind == KIND_STRUCTURE
            else round(float(rec.length_m[k & (d >= 1.0 * FT)].sum()) / 1000.0, 2),
            "depth_ge_3ft": int((k & (d >= 3.0 * FT)).sum()) if kind == KIND_STRUCTURE
            else round(float(rec.length_m[k & (d >= 3.0 * FT)].sum()) / 1000.0, 2),
            "dv_ge_{:g}_ft2s".format(dv_limit): int((k & (dv >= dv_limit)).sum())
            if kind == KIND_STRUCTURE
            else round(float(rec.length_m[k & (dv >= dv_limit)].sum()) / 1000.0, 2),
            "max_depth_ft": round(float(np.nanmax(d[k])) / FT, 2),
            "earliest_arrival_hr": (round(float(np.nanmin(a[k])), 2)
                                    if np.isfinite(a[k]).any() else None),
        }
    rows = []
    for i in np.nonzero(wet)[0]:
        rows.append({"id": rec.ident[i], "kind": rec.kind[i], "x": round(float(rec.x[i]), 1),
                     "y": round(float(rec.y[i]), 1), "depth_ft": round(float(d[i]) / FT, 2),
                     "velocity_fps": round(float(v[i]) / FT, 2) if np.isfinite(v[i]) else "",
                     "arrival_hr": round(float(a[i]), 2) if np.isfinite(a[i]) else ""})
    return summ, rows


def write_rows(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["id", "kind", "x", "y", "depth_ft", "velocity_fps",
                                          "arrival_hr"])
        w.writeheader()
        w.writerows(rows)
