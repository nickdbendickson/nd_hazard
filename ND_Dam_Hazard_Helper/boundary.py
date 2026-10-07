# -*- coding: utf-8 -*-
"""Model-extent check: does the breach flood reach the edge of the model?

Wet cells on the edge of the model mean the flood (and possibly the
consequences) continues beyond what was evaluated. The edge is the model
boundary polygon when one is given (2D flow area / domain), otherwise the
first and last row and column of the depth raster. With a no-breach
baseline, the breach's added depth at those cells is reported too: a
flood that leaves the model still well above the no-breach flood means the
model should be extended downstream (or the engineer documents why the
remaining increment does not matter).

An upstream edge that is wet is normal (reservoir, inflow boundary); the
arrival time per side helps tell the downstream side: it is the one the
flood reaches last.
"""

import math

import numpy as np
from qgis.core import QgsCoordinateTransform, QgsFeature, QgsField, QgsGeometry, QgsPointXY

from .compat import WKB_POINT, FT_DOUBLE, FT_STRING, NULL
from .constants import BOUNDARY_MAX_POINTS
from .helpers import create_memory_layer, transform_geometry, log_message

SIDES = ("N", "S", "W", "E")
SIDE_NAMES = {"N": "north", "S": "south", "W": "west", "E": "east",
              "boundary": "model boundary"}


def _edge_cells(sampler):
    """[(side, cols, rows, depths)] of the raster's outer rows / columns;
    corner cells belong to the N / S rows only."""
    w, h = sampler.xsize, sampler.ysize
    out = []
    for side, (c0, r0, cw, rh) in (("N", (0, 0, w, 1)), ("S", (0, h - 1, w, 1)),
                                   ("W", (0, 1, 1, max(0, h - 2))),
                                   ("E", (w - 1, 1, 1, max(0, h - 2)))):
        if cw <= 0 or rh <= 0 or (side == "S" and h < 2) or (side == "E" and w < 2):
            continue
        vals = sampler.window_values(c0, r0, cw, rh)
        if vals is None:
            continue
        vals = vals.ravel()
        if rh == 1:
            cols, rows = np.arange(c0, c0 + cw), np.full(cw, r0)
        else:
            cols, rows = np.full(rh, c0), np.arange(r0, r0 + rh)
        out.append((side, cols, rows, vals))
    return out


def _valid(g):
    try:
        if not g.isGeosValid():
            fixed = g.makeValid()
            if fixed is not None and not fixed.isNull() and not fixed.isEmpty():
                return fixed
    except Exception:
        pass
    return g


def _polygon_edge_groups(sampler, geoms, rows_per_strip=512):
    """[("boundary", xs, ys, depths)] of the cells just inside the model
    boundary: cells whose centre is inside the (combined) polygons and that
    touch a cell outside them. All polygons are burnt into one mask, so the
    shared edge of two adjacent 2D flow areas is not a model edge. Read in
    strips of rows, so memory stays small on large rasters."""
    bbs = [g.boundingBox() for g in geoms]
    cell = max(abs(sampler.gt[1]), abs(sampler.gt[5]))
    win = sampler._window_for_bbox(min(b.xMinimum() for b in bbs) - 2 * cell,
                                   min(b.yMinimum() for b in bbs) - 2 * cell,
                                   max(b.xMaximum() for b in bbs) + 2 * cell,
                                   max(b.yMaximum() for b in bbs) + 2 * cell)
    if win is None:
        return [], 0
    c0, r0, ww, hh = win
    out, checked = [], 0
    for y0 in range(r0, r0 + hh, rows_per_strip):
        y1 = min(r0 + hh, y0 + rows_per_strip)
        ya, yb = max(r0, y0 - 1), min(r0 + hh, y1 + 1)       # one-row halo
        mask = np.zeros((yb - ya, ww), dtype=bool)
        for g in geoms:
            m = sampler._rasterize(g, c0, ya, ww, yb - ya)
            if m is not None:
                mask |= m
        if not mask.any():
            continue
        pad = np.pad(mask, 1, mode="constant", constant_values=False)
        if ya == 0:                     # beyond the raster's first / last row:
            pad[0, :] = False           # outside the model
        inner = mask.copy()
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy or dx:
                    inner &= pad[1 + dy:1 + dy + mask.shape[0], 1 + dx:1 + dx + mask.shape[1]]
        edge = (mask & ~inner)[y0 - ya:y0 - ya + (y1 - y0)]
        if not edge.any():
            continue
        vals = sampler.window_values(c0, y0, ww, y1 - y0)
        if vals is None:
            continue
        sel = edge & np.isfinite(vals)
        checked += int(edge.sum())
        rr, cc = np.nonzero(sel)
        if rr.size:
            xs, ys = sampler.cell_centres(cc + c0, rr + y0)
            out.append(("boundary", xs, ys, vals[rr, cc]))
    return out, checked


def check(depth_sampler, analysis_crs, transform_ctx, boundary_layer=None,
          base_sampler=None, arr_sampler=None, min_incr_ft=1.0):
    """Wet cells on the model edge. Returns a dict:
    method, checked (edge cells looked at), wet, per_side {side: {n, max,
    arrival}}, max_depth, max_incr (None without baseline), n_incr (cells
    with the breach >= min_incr_ft above the baseline), points [(x, y, side,
    depth, base, incr, arrival)] in `analysis_crs`, flagged (bool)."""
    res = {"method": None, "checked": 0, "wet": 0, "per_side": {}, "max_depth": None,
           "max_incr": None, "n_incr": 0, "points": [], "flagged": False,
           "baseline": base_sampler is not None and base_sampler.ok,
           "min_incr_ft": float(min_incr_ft)}
    if depth_sampler is None or not depth_sampler.ok:
        return res
    groups = []                                  # (side, xs, ys, depths) raster CRS
    if boundary_layer is not None and boundary_layer.isValid():
        res["method"] = "model boundary polygon '{}'".format(boundary_layer.name())
        xf = QgsCoordinateTransform(boundary_layer.crs(), depth_sampler.crs, transform_ctx)
        geoms = []
        for feat in boundary_layer.getFeatures():
            g = transform_geometry(feat.geometry(), xf)
            if g is not None and not g.isNull() and not g.isEmpty():
                geoms.append(_valid(g))
        if not geoms:
            raise ValueError("the model boundary layer has no polygon")
        groups, res["checked"] = _polygon_edge_groups(depth_sampler, geoms)
    else:
        res["method"] = "edge of the depth raster"
        for side, cols, rows, vals in _edge_cells(depth_sampler):
            ok = np.isfinite(vals)
            res["checked"] += int(ok.sum())
            xs, ys = depth_sampler.cell_centres(cols[ok], rows[ok])
            groups.append((side, xs, ys, vals[ok]))
    back = QgsCoordinateTransform(depth_sampler.crs, analysis_crs, transform_ctx) \
        if depth_sampler.crs is not None else None
    pts = []
    for side, xs, ys, vals in groups:
        wet = np.isfinite(vals) & (vals > 0)
        if not wet.any():
            continue
        xs, ys, d = xs[wet], ys[wet], vals[wet]
        base = base_sampler.values_at(xs, ys) if res["baseline"] else None
        arr = arr_sampler.values_at(xs, ys) \
            if arr_sampler is not None and arr_sampler.ok else None
        if base is not None:
            b0 = np.where(np.isfinite(base) & (base > 0), base, 0.0)
            incr = d - b0
        else:
            incr = None
        s = res["per_side"].setdefault(side, {"n": 0, "max": 0.0, "arrival": None,
                                              "max_incr": None, "n_incr": 0})
        s["n"] += int(d.size)
        s["max"] = max(s["max"], float(d.max()))
        if arr is not None and np.isfinite(arr).any():
            med = float(np.nanmedian(arr))
            s["arrival"] = med if s["arrival"] is None else min(s["arrival"], med)
        if incr is not None:
            s["max_incr"] = float(incr.max()) if s["max_incr"] is None \
                else max(s["max_incr"], float(incr.max()))
            s["n_incr"] += int((incr >= min_incr_ft).sum())
        for i in range(d.size):
            pts.append((float(xs[i]), float(ys[i]), side, float(d[i]),
                        None if base is None or not np.isfinite(base[i]) else float(base[i]),
                        None if incr is None else float(incr[i]),
                        None if arr is None or not np.isfinite(arr[i]) else float(arr[i])))
    res["wet"] = len(pts)
    if pts:
        res["max_depth"] = max(p[3] for p in pts)
        if res["baseline"]:
            res["max_incr"] = max(p[5] for p in pts)
            res["n_incr"] = sum(1 for p in pts if p[5] >= min_incr_ft)
            res["flagged"] = res["n_incr"] > 0
        else:
            res["flagged"] = True
    # most important cells first, thinned to BOUNDARY_MAX_POINTS
    pts.sort(key=lambda p: -(p[5] if p[5] is not None else p[3]))
    if len(pts) > BOUNDARY_MAX_POINTS:
        head = BOUNDARY_MAX_POINTS // 2
        step = int(math.ceil((len(pts) - head) / float(BOUNDARY_MAX_POINTS - head)))
        pts = pts[:head] + pts[head::step]
    if back is not None and not back.isShortCircuited():
        moved = []
        for p in pts:
            try:
                q = back.transform(QgsPointXY(p[0], p[1]))
                moved.append((q.x(), q.y()) + tuple(p[2:]))
            except Exception:
                continue
        pts = moved
    res["points"] = pts
    return res


def describe(res):
    """One line for the summary / judgment list."""
    if res and res.get("error"):
        return "Model extent NOT checked ({}) - check by hand whether the flood leaves the " \
            "model".format(res["error"])
    if not res or res.get("method") is None:
        return "Model extent not checked (no depth raster)"
    if not res["wet"]:
        line = "The flood does not reach the {} ({} edge cells checked)".format(
            res["method"], res["checked"])
        if res["method"].startswith("edge of the depth raster"):
            line += (" - the 2D flow area may end inside the raster; give the model boundary "
                     "polygon for a complete check")
        return line
    sides = []
    for side, s in sorted(res["per_side"].items(), key=lambda kv: -kv[1]["n"]):
        t = "{} {} cell(s), max {:.1f} ft".format(SIDE_NAMES.get(side, side), s["n"], s["max"])
        if s.get("max_incr") is not None:
            t += ", breach adds up to {:.1f} ft".format(s["max_incr"])
        if s.get("arrival") is not None:
            t += ", arrival ~{:.1f} h".format(s["arrival"])
        sides.append(t)
    line = "The breach flood reaches the {}: {}".format(res["method"], "; ".join(sides))
    if res["baseline"]:
        line += (" - {} cell(s) with the breach >= {:g} ft above the no-breach flood".format(
            res["n_incr"], res["min_incr_ft"]) if res["n_incr"] else
            " - nowhere >= {:g} ft above the no-breach flood (incremental flood ends inside "
            "the model)".format(res["min_incr_ft"]))
    return line


def judgment_text(res):
    if res.get("error"):
        return describe(res)
    return (describe(res) + ". Consequences beyond the model are not evaluated: extend the "
            "model downstream, or document why the remaining flood is not significant "
            "(a wet upstream / reservoir edge is expected - the side reached last is "
            "downstream)")


FIELDS = [("Scenario", FT_STRING), ("Side", FT_STRING), ("Depth_ft", FT_DOUBLE),
          ("Base_Depth_ft", FT_DOUBLE), ("Incr_ft", FT_DOUBLE), ("Arrival_hr", FT_DOUBLE),
          ("Flag", FT_STRING)]


def to_layer(res, scenario, crs, name):
    if not res or not res.get("points"):
        return None
    lyr = create_memory_layer(name, WKB_POINT, crs, [QgsField(n, t) for n, t in FIELDS])
    feats = []
    thr = res["min_incr_ft"]

    def r(v):
        return NULL if v is None else round(v, 2)
    for x, y, side, d, b, inc, a in res["points"]:
        flag = ("incremental >= {:g} ft".format(thr) if inc is not None and inc >= thr else
                "wet, below incremental threshold" if inc is not None else "wet")
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        f.setAttributes([scenario, SIDE_NAMES.get(side, side), r(d), r(b), r(inc), r(a), flag])
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    lyr.updateExtents()
    log_message("Model extent check ({}): {}".format(scenario, describe(res)))
    return lyr
