"""midcrest - the dam point at mid-crest (1.18.2).

The dam point goes on the crest at the middle of the dam line: the line's midpoint by length,
then the highest ground on a profile across the line through it (perpendicular, +-25 m at
0.1 m steps, bilinear on the DEM). The DWR dam line is drawn near the crest but not on it;
an inventory point can sit on the toe, a road or higher ground off the dam (DWR-4031: the
old point read 1643.63 ft from a hill; mid-crest is 1630.49 ft NAVD88).

Pure Python (no numpy, no GDAL): the caller gives a sampler z(x, y) -> metres (None / NaN
off the data), or an array and a GDAL geotransform for bilinear() to sample. Planar metric
coordinates (EPSG:5070 or the run's working CRS).

  midcrest(line_xy, z_fn, original_xy=None, half_width_m=25.0, step_m=0.1) -> dict or None
  bilinear(arr, gt, row0=0, col0=0) -> z_fn (cell-centre bilinear, NaN outside / no data)
  summary_line(rec) -> one plain-text line for the reports

Edge check (Nick 2026-10-29, "change them"; the method is unchanged): when the highest point
is at the end of the profile (|offset| within half a step of +-25 m) the crest may be beyond
the profile or the pick may be a hill / road next to the dam - the record gets
"at_edge": True and "review" (EDGE_REVIEW), and summary_line says REVIEW.
"""
import math

FT = 0.3048
HALF_WIDTH_M = 25.0
STEP_M = 0.1
METHOD = ("dam line midpoint by length; highest DEM point on the perpendicular profile "
          "+-{:.0f} m at {:.1f} m steps (bilinear)")
EDGE_REVIEW = ("the highest point is at the edge of the +-{:.0f} m profile ({:+.1f} m off the dam "
               "line's midpoint): the crest pick may be unreliable (the crest may lie beyond the "
               "profile, or the pick is a hill / road next to the dam) - check the crest")


def _pts(line_xy):
    out = []
    for p in line_xy or []:
        try:
            x, y = float(p[0]), float(p[1])
        except (TypeError, ValueError, IndexError):
            continue
        if math.isfinite(x) and math.isfinite(y) and (not out or (x, y) != out[-1]):
            out.append((x, y))
    return out


def line_length(line_xy):
    p = _pts(line_xy)
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(p[:-1], p[1:]))


def point_at(line_xy, s):
    """(point, unit normal) at distance s along the line; normal = (-dy, dx) of its segment.
    None for a line shorter than two distinct points."""
    p = _pts(line_xy)
    if len(p) < 2:
        return None
    seg = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(p[:-1], p[1:])]
    cum = 0.0
    k = len(seg) - 1
    for i, L in enumerate(seg):
        if s < cum + L or i == len(seg) - 1:
            k = i
            break
        cum += L
    L = seg[k]
    u = (s - cum) / L
    a, b = p[k], p[k + 1]
    dx, dy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
    return (a[0] + u * (b[0] - a[0]), a[1] + u * (b[1] - a[1])), (-dy, dx)


def _ok(z):
    try:
        z = float(z)
    except (TypeError, ValueError):
        return None
    return z if math.isfinite(z) and z > -9998.0 else None


def bilinear(arr, gt, row0=0, col0=0):
    """z(x, y) on `arr` (rows x cols, arr[r][c]) whose cell (0, 0) is the source raster's
    (row0, col0) under GDAL geotransform `gt` (north-up). Cell-centre bilinear; NaN when
    any of the four cells is missing or no data (NaN / <= -9998)."""
    ny = len(arr)
    nx = len(arr[0]) if ny else 0

    def z(x, y):
        fx = (x - gt[0]) / gt[1] - col0 - 0.5
        fy = (y - gt[3]) / gt[5] - row0 - 0.5
        i, j = int(math.floor(fx)), int(math.floor(fy))
        if i < 0 or j < 0 or i + 1 >= nx or j + 1 >= ny:
            return float("nan")
        tx, ty = fx - i, fy - j
        q = [_ok(arr[j][i]), _ok(arr[j][i + 1]), _ok(arr[j + 1][i]), _ok(arr[j + 1][i + 1])]
        if any(v is None for v in q):
            return float("nan")
        return (q[0] * (1 - tx) * (1 - ty) + q[1] * tx * (1 - ty) + q[2] * (1 - tx) * ty
                + q[3] * tx * ty)
    return z


def midcrest(line_xy, z_fn, original_xy=None, half_width_m=HALF_WIDTH_M, step_m=STEP_M):
    """The mid-crest dam point, or None (no line / no DEM on the profile). Returns
    {"xy": [x, y], "z_m", "z_ft", "midpoint_xy": [x, y], "midpoint_z_m", "offset_m" (along
    the normal, + = left of the line's direction), "line_length_m", "original_xy",
    "moved_m" (from original_xy, else from the midpoint), "method"}."""
    p = _pts(line_xy)
    L = line_length(p)
    if len(p) < 2 or L <= 0:
        return None
    mid, n = point_at(p, 0.5 * L)
    k = int(round(2.0 * half_width_m / step_m))
    best = None
    for i in range(k + 1):
        o = -half_width_m + i * step_m
        z = _ok(z_fn(mid[0] + o * n[0], mid[1] + o * n[1]))
        if z is not None and (best is None or z > best[1]):
            best = (o, z)
    if best is None:
        return None
    o, z = best
    xy = (mid[0] + o * n[0], mid[1] + o * n[1])
    orig = mid if original_xy is None else (float(original_xy[0]), float(original_xy[1]))
    zm = _ok(z_fn(mid[0], mid[1]))
    edge = abs(o) >= half_width_m - 0.5 * step_m
    out = {"xy": [round(xy[0], 3), round(xy[1], 3)], "z_m": round(z, 4), "z_ft": round(z / FT, 2),
            "midpoint_xy": [round(mid[0], 3), round(mid[1], 3)],
            "midpoint_z_m": None if zm is None else round(zm, 4),
            "offset_m": round(o, 2), "line_length_m": round(L, 2),
            "original_xy": [round(orig[0], 3), round(orig[1], 3)],
            "moved_m": round(math.hypot(xy[0] - orig[0], xy[1] - orig[1]), 2),
            "method": METHOD.format(half_width_m, step_m), "_z_m": z, "_xy": xy,
            "at_edge": edge}
    if edge:
        out["review"] = EDGE_REVIEW.format(half_width_m, o)
    return out


def public(rec):
    return None if rec is None else {k: v for k, v in rec.items() if not k.startswith("_")}


def summary_line(rec):
    """'Dam point at mid-crest: ...' (plain text) or '' without a record."""
    if not rec:
        return ""
    if rec.get("warning"):
        return "Dam point NOT moved to mid-crest: " + str(rec["warning"])
    rv = " REVIEW: " + str(rec["review"]) + "." if rec.get("review") else ""
    if rec.get("moved") is False:
        return ("Dam point kept at the breach position ({:.3f}, {:.3f}); DEM crest at mid-crest "
                "({:.3f}, {:.3f}) {:.2f} ft NAVD88 ({}).".format(
                    rec["original_xy"][0], rec["original_xy"][1], rec["xy"][0], rec["xy"][1],
                    rec["z_ft"], rec.get("method") or METHOD)) + rv
    return ("Dam point at mid-crest: ({:.3f}, {:.3f}), DEM crest {:.2f} ft NAVD88; moved {:.2f} m "
            "from ({:.3f}, {:.3f}); {:+.1f} m off the dam line's midpoint ({}).".format(
                rec["xy"][0], rec["xy"][1], rec["z_ft"], rec["moved_m"], rec["original_xy"][0],
                rec["original_xy"][1], rec["offset_m"], rec.get("method") or METHOD)) + rv
