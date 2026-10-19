"""Crossings the bare-earth DEM turns into a dam (3.20.0).

Feature 14 of the review: take the NBI bridges and the surveyed crossings of the offline pack
into the built-in engine's terrain as gaps in embankments, the way the HEC-RAS export treats
culverts.

What this does and does not do, and why
--------------------------------------
The machinery to cut a gap already exists and is not reinvented here: a
`structures.Structure` of kind bridge / notch is cut through the embankment in the model DEM
for LISFLOOD-FP and the built-in engine alike (structures.structure_edits), and a culvert keeps
its roof for ANUGA's culvert operator. What was missing is the step BEFORE that: knowing that a
crossing is there at all, and with what opening. So this module

  * reports, for every inventory crossing inside the flood corridor, how high the model's
    embankment stands above the channel there - "this road blocks flow by 8 ft at an NBI bridge"
    is the sentence the engineer needs, and it needs no hydraulics to be true;
  * builds the Structure objects for the crossings whose opening is actually surveyed (a span
    and a rise), so they can be cut - but only when the engineer turns it on.

Cutting them is NOT the default, and that is a deliberate choice about conservatism rather than
about effort. Opening a gap in an embankment lets water through: the pool behind the road falls
and the water downstream of it arrives sooner and deeper. The first of those is a result moving
in the LESS conservative direction for anybody upstream of the road, and this suite's rule is
that a release never does that on its own. A crossing with a guessed opening is worse again: an
8 ft box culvert invented for a 60 ft bridge changes both numbers and nobody can tell from the
output. So:

  * a crossing with a surveyed span and rise can be cut, with the option on;
  * a crossing without one is reported, never cut, exactly as culvert candidates already are
    (structures.culvert_candidates, 3.13.0);
  * the run report lists every blocking embankment at a known crossing whether the option is on
    or off, so a run that did not cut them still says what it did not do.

    rows = blocking_rows(crossings, roads, read_dem, cell_m)
    structs, notes = structures_from(rows, cell_m)       # only the surveyed ones

`crossings` are plain dicts, as the caller reads them from the dam's data layers
(damdata roles "bridges" = NBI_bridges and "crossings" = FRA grade crossings) or from the
engineer's own surveyed layer:
    {"name", "x", "y", "kind" ("bridge" / "culvert"), "span_ft", "rise_ft", "barrels",
     "shape", "invert_up_ft", "invert_dn_ft", "source"}
x / y are in the work CRS, like everything the workflow handles.

Pure numpy; no QGIS.
"""
import math

import numpy as np

from . import structures as structmod
from .units import FT

MATCH_M = 40.0            # a crossing further than this from a road line is not on it
CORRIDOR_M = 25.0         # half-width of the road corridor the crown is taken from
REACH_M = 60.0            # how far either side of the road the channel bed is looked for
MIN_SPAN_FT = 1.0         # a smaller opening is not a crossing we can cut
BLOCK_NOTE = ("A crossing that the terrain closes: bare-earth LiDAR leaves the road "
              "embankment as a solid wall where the bridge or culvert opening is. The model "
              "treats it as ground unless the crossing is given as a structure.")
NOT_CUT = ("Not cut: opening the embankment lowers the water behind it, which this release "
           "does not do on its own. Turn on 'Cut surveyed crossings as gaps' to cut the ones "
           "whose span and rise are surveyed.")


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _nearest_on_line(line, x, y):
    """(distance, (px, py), (ux, uy)) - nearest point on a polyline and its unit direction."""
    pts = np.asarray(line, dtype=float)
    if len(pts) < 2:
        return None
    best = None
    for a, b in zip(pts[:-1], pts[1:]):
        v = b - a
        n2 = float(v[0] ** 2 + v[1] ** 2)
        if n2 <= 0.0:
            continue
        t = max(0.0, min(1.0, float(((x - a[0]) * v[0] + (y - a[1]) * v[1]) / n2)))
        p = a + t * v
        d = math.hypot(x - p[0], y - p[1])
        if best is None or d < best[0]:
            n = math.sqrt(n2)
            best = (d, (float(p[0]), float(p[1])), (float(v[0] / n), float(v[1] / n)))
    return best


def match_to_roads(crossing, roads, match_m=MATCH_M):
    """(point on the road, direction along it, distance) for a crossing, or None.

    The gap has to be cut ACROSS the embankment, so the road's own direction at the crossing is
    what decides the line; a gap cut at a guessed angle would miss the channel."""
    x, y = _num(crossing.get("x")), _num(crossing.get("y"))
    if x is None or y is None:
        return None
    best = None
    for line in roads or []:
        got = _nearest_on_line(line, x, y)
        if got is None:
            continue
        if best is None or got[0] < best[0]:
            best = got
    if best is None or best[0] > match_m:
        return None
    return {"point": best[1], "direction": best[2], "distance_m": best[0]}


def gap_line(point, direction, length_m):
    """The line to cut: across the road through `point`, `length_m` long (upstream end first
    is decided by the caller; for a gap the two ends are interchangeable)."""
    ux, uy = direction
    nx, ny = -uy, ux                       # perpendicular to the road
    half = 0.5 * float(length_m)
    return np.array([[point[0] - nx * half, point[1] - ny * half],
                     [point[0] + nx * half, point[1] + ny * half]], dtype=float)


def _crown_and_bed(grid, point, direction, corridor_m=CORRIDOR_M, reach_m=REACH_M):
    """(crown, bed) metres: the highest ground along the road at the crossing, and the lowest
    ground within `reach_m` either side of it off the road."""
    z = grid.masked()
    X, Y = np.meshgrid(grid.x_centers(), grid.y_centers())
    ux, uy = direction
    along = (X - point[0]) * ux + (Y - point[1]) * uy
    across = (X - point[0]) * (-uy) + (Y - point[1]) * ux
    on_road = (np.abs(along) <= corridor_m) & (np.abs(across) <= 0.5 * corridor_m) & \
        np.isfinite(z)
    off_road = (np.abs(along) <= corridor_m) & (np.abs(across) > 0.5 * corridor_m) & \
        (np.abs(across) <= reach_m) & np.isfinite(z)
    crown = float(np.nanmax(z[on_road])) if on_road.any() else None
    bed = float(np.nanmin(z[off_road])) if off_road.any() else None
    return crown, bed


def blocking_rows(crossings, roads, read_dem, cell_m, match_m=MATCH_M,
                  corridor_m=CORRIDOR_M, reach_m=REACH_M):
    """One row per crossing: where it is, how much the terrain blocks it, and whether its
    opening is surveyed well enough to cut.

    `read_dem(bounds, cell)` returns a Grid of the model's terrain (the workflow's own reader).
    A crossing that is not on a road line, or has no DEM, is still reported - with the reason -
    because "we could not check this crossing" is itself worth reading."""
    rows = []
    for c in crossings or []:
        name = str(c.get("name") or c.get("id") or "crossing").strip()
        kind = str(c.get("kind") or structmod.KIND_BRIDGE).strip().lower()
        if kind not in structmod.KINDS:
            kind = structmod.KIND_BRIDGE
        span = _num(c.get("span_ft")) or _num(c.get("width_ft"))
        rise = _num(c.get("rise_ft")) or _num(c.get("height_ft"))
        row = {"name": name, "kind": kind, "source": str(c.get("source") or ""),
               "x": _num(c.get("x")), "y": _num(c.get("y")),
               "span_ft": span, "rise_ft": rise,
               "barrels": int(_num(c.get("barrels")) or 1),
               "shape": str(c.get("shape") or "box"),
               "invert_up_ft": _num(c.get("invert_up_ft")),
               "invert_dn_ft": _num(c.get("invert_dn_ft")),
               "blocks_ft": None, "crown_ft": None, "bed_ft": None,
               "on_road": False, "surveyed": bool(span and span >= MIN_SPAN_FT and rise),
               "reason": ""}
        match = match_to_roads(c, roads, match_m)
        if match is None:
            row["reason"] = ("no road or railroad line within {:.0f} ft of it, so the "
                             "embankment could not be measured".format(match_m / FT))
            rows.append(row)
            continue
        row["on_road"] = True
        row["road_distance_ft"] = round(match["distance_m"] / FT, 1)
        length = max(4.0 * float(cell_m), 2.0 * reach_m)
        line = gap_line(match["point"], match["direction"], length)
        pad = reach_m + 2.0 * float(cell_m)
        bounds = (float(min(line[:, 0])) - pad, float(min(line[:, 1])) - pad,
                  float(max(line[:, 0])) + pad, float(max(line[:, 1])) + pad)
        try:
            grid = read_dem(bounds, float(cell_m))
        except Exception as e:                                   # noqa - one crossing
            row["reason"] = "no terrain here ({}: {})".format(type(e).__name__, e)
            rows.append(row)
            continue
        crown, bed = _crown_and_bed(grid, match["point"], match["direction"], corridor_m,
                                    reach_m)
        if crown is None or bed is None:
            row["reason"] = "the terrain around it is no-data"
            rows.append(row)
            continue
        row["crown_ft"] = round(crown / FT, 2)
        row["bed_ft"] = round(bed / FT, 2)
        row["blocks_ft"] = round(max(0.0, crown - bed) / FT, 2)
        row["line"] = line.tolist()
        row["point"] = list(match["point"])
        row["direction"] = list(match["direction"])
        if not row["surveyed"]:
            row["reason"] = ("no surveyed span and rise, so the opening is unknown: reported "
                             "only, never modelled (a guessed size moves the depths both ways)")
        rows.append(row)
    rows.sort(key=lambda r: (-(r.get("blocks_ft") or 0.0), r["name"]))
    return rows


def blocking(rows, min_ft=1.0):
    """The rows where the terrain really does close a known crossing."""
    return [r for r in rows or [] if (r.get("blocks_ft") or 0.0) >= min_ft]


def structures_from(rows, cell_m, kind_override=""):
    """([structures.Structure], notes) for the crossings whose opening is surveyed.

    The Structure carries the surveyed span and rise, so the gap that is cut is the opening that
    was measured; the existing code does the cutting (structures.structure_edits for
    LISFLOOD-FP and the built-in engine, the culvert operator for ANUGA) and the existing
    invert reconciliation checks the elevations against the LiDAR
    (structures.reconcile_inverts). Nothing new is invented about the hydraulics."""
    out, notes = [], []
    for r in rows or []:
        if not r.get("surveyed") or not r.get("line"):
            if r.get("blocks_ft"):
                notes.append("{}: {}".format(r["name"], r.get("reason") or
                                             "not cut (no surveyed opening)"))
            continue
        span_m = float(r["span_ft"]) * FT
        rise_m = float(r["rise_ft"]) * FT
        kind = kind_override or r.get("kind") or structmod.KIND_BRIDGE
        s = structmod.Structure(
            kind=kind, line=np.asarray(r["line"], dtype=float), width_m=span_m,
            height_m=rise_m, barrels=max(1, int(r.get("barrels") or 1)),
            shape=str(r.get("shape") or "box"),
            invert_up_m=(float(r["invert_up_ft"]) * FT
                         if r.get("invert_up_ft") is not None else None),
            invert_dn_m=(float(r["invert_dn_ft"]) * FT
                         if r.get("invert_dn_ft") is not None else None),
            name="{} ({})".format(r["name"], r.get("source") or "surveyed crossing"))
        out.append(s)
        notes.append("{}: {:.0f} ft x {:.1f} ft {} cut as a gap in the embankment ({} blocked "
                     "{} ft)".format(r["name"], r["span_ft"], r["rise_ft"], kind,
                                     "the terrain", r.get("blocks_ft")))
    return out, notes


def markdown_lines(rows, cut=False, title="Road and railroad crossings as embankment gaps"):
    """The run report section (always written, whether the gaps were cut or not)."""
    lines = ["## " + title, ""]
    if not rows:
        lines += ["- No inventory crossing (NBI bridge, grade crossing, surveyed culvert) was "
                  "found near this flood's roads.", ""]
        return lines
    blocked = blocking(rows)
    lines.append("- " + BLOCK_NOTE)
    lines.append("- {} crossing(s) checked; {} where the terrain blocks the opening by 1 ft or "
                 "more; {} with a surveyed span and rise.".format(
                     len(rows), len(blocked), sum(1 for r in rows if r.get("surveyed"))))
    lines.append("- " + ("Surveyed crossings were cut as gaps in this run." if cut
                         else NOT_CUT))
    lines.append("")
    lines.append("| Crossing | Kind | Blocks (ft) | Span x rise (ft) | Cut | Note |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for r in rows[:40]:
        lines.append("| {} | {} | {} | {} | {} | {} |".format(
            r["name"], r["kind"],
            "" if r.get("blocks_ft") is None else r["blocks_ft"],
            "{:g} x {:g}".format(r["span_ft"], r["rise_ft"]) if r.get("surveyed")
            else "not surveyed",
            "yes" if (cut and r.get("surveyed")) else "no", r.get("reason") or ""))
    if len(rows) > 40:
        lines.append("| ... {} more | | | | | |".format(len(rows) - 40))
    lines.append("")
    return lines
