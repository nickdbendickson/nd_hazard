"""Pick a crest line from a GIS layer by structure ID or proximity.

Functions
---------
pick_crest_line    match by DWR ID then nearest within CREST_SNAP_M
crest_line_near    midpoint-nearest crest line within max_m
nearest_on_line    (distance, (x, y)) to the nearest point on a polyline
dwr_ids            extract DWR structure numbers from a dam record
"""
import math
import re

import numpy as np

CREST_SNAP_M = 500.0


def nearest_on_line(line, xy):
    """(distance, (x, y)) of the point of polyline `line` nearest `xy`."""
    p = np.asarray(line, float)
    best = None
    for a, b in zip(p[:-1], p[1:]):
        ab = b - a
        L2 = float(ab @ ab)
        t = 0.0 if L2 <= 0 else min(max(float((np.asarray(xy) - a) @ ab) / L2, 0.0), 1.0)
        q = a + t * ab
        d = float(math.hypot(q[0] - xy[0], q[1] - xy[1]))
        if best is None or d < best[0]:
            best = (d, (float(q[0]), float(q[1])))
    return best


def dwr_ids(dam_id, rec):
    """The DWR structure numbers a dam may carry: dam.json's
    dwr_structure_id and the digits of a 'DWR-2801' style dam ID."""
    out = set()
    v = getattr(rec, "get", lambda _k: None)("dwr_structure_id") if rec is not None else None
    for x in (v, ):
        if x not in (None, ""):
            out.add(str(x).strip().split(".")[0].lstrip("0") or "0")
    m = re.match(r"^\s*DWR[-_ ]?0*(\d+)\s*$", str(dam_id or ""), re.I)
    if m:
        out.add(m.group(1).lstrip("0") or "0")
    return out


def _point_along_line(line, frac):
    """(x, y) at fraction `frac` (0..1) along the polyline."""
    p = np.asarray(line, float)
    if len(p) < 2:
        return (float(p[0, 0]), float(p[0, 1])) if len(p) else (0.0, 0.0)
    segs = np.hypot(*np.diff(p, axis=0).T)
    total = segs.sum()
    if total <= 0:
        return (float(p[0, 0]), float(p[0, 1]))
    target = frac * total
    acc = 0.0
    for (x0, y0), (x1, y1), s in zip(p[:-1], p[1:], segs):
        if acc + s >= target or s == segs[-1]:
            t = (target - acc) / s if s > 0 else 0.0
            return (float(x0 + t * (x1 - x0)), float(y0 + t * (y1 - y0)))
        acc += s
    return (float(p[-1, 0]), float(p[-1, 1]))


def crest_line_near(lines, xy, max_m=500.0):
    """Crest line whose midpoint is nearest `xy` within `max_m` m (else None)."""
    best, bd = None, None
    for ln in lines:
        mx, my = _point_along_line(ln, 0.5)
        d = ((mx - xy[0]) ** 2 + (my - xy[1]) ** 2) ** 0.5
        if bd is None or d < bd:
            best, bd = ln, d
    if best is None or bd > max_m:
        return None
    return best


def pick_crest_line(lines_attrs, center_xy, ids, id_field):
    """(line, distance, how) of the crest line for this dam.

    The line whose structure id matches the dam's DWR number wins; otherwise
    the line nearest the dam point, within CREST_SNAP_M.
    `lines_attrs` is a list of (line, {field: value}).
    """
    if not lines_attrs:
        return None, None, "no crest line near the dam"
    if ids and id_field:
        hit = [(ln, at) for ln, at in lines_attrs
               if str(at.get(id_field) if at.get(id_field) is not None else "").strip()
               .split(".")[0].lstrip("0") in ids]
        if hit:
            ln = min((h[0] for h in hit), key=lambda l: nearest_on_line(l, center_xy)[0])
            return ln, nearest_on_line(ln, center_xy)[0], "DWR structure {}".format(
                sorted(ids)[0])
    ln = min((x[0] for x in lines_attrs), key=lambda l: nearest_on_line(l, center_xy)[0])
    d = nearest_on_line(ln, center_xy)[0]
    if d > CREST_SNAP_M:
        return None, d, "the nearest crest line is {:.0f} m from the dam point".format(d)
    return ln, d, "nearest line"
