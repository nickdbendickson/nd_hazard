"""Terrain-aware 2D grid cell defaults for the Runner.

On flat section-road plains (low DEM relief and gentle slope over the sample
window) the model cell should start at 10 m, with 5 m offered when the engineer
can afford the run time. Coarser cells (20-30 m) average away road embankments
and ditch thalwegs, so the flood sheets across fields that finer grids keep in
the channels.

Evidence (DWR-2801 Absaraka, Claude Runner pack 2026-11-03; Docs & QA compare
2026-11-04): on a fixed domain vs DSS-WISE wet at >=0.5 ft, CSI went
0.36 -> 0.43 -> 0.57 as the cell went 20 -> 10 -> 5 m. Nick (Docs & QA chat,
2026-11-04): adopt 10 m on flat section-road plains, with 5 m when time allows.

Other terrain keeps the existing 10 m starting value. Setting keys (CELL /
grid_m) stay numeric metres; this module only classifies the terrain, suggests
the starting and finer cells, and builds the pre-run warning when a flat-plain
run is coarser than 10 m.

The post-run grid-spread advisory (3.27.7) still fires from the flood signature
itself; this module is the pre-run / default side of the same finding.
"""
from __future__ import annotations

import math

import numpy as np

# Relief (p95 - p5 elev, m) and mean |grad| (rise/run) that count as a flat
# section-road plain. Tuned to catch prairie / Red River Valley style domains
# (Absaraka) while leaving steep canyon / valley dams as "other".
FLAT_PLAIN_RELIEF_M = 25.0
FLAT_PLAIN_SLOPE = 0.010          # 1%

FLAT_PLAIN_DEFAULT_CELL_M = 10.0
FLAT_PLAIN_FINE_CELL_M = 5.0
OTHER_DEFAULT_CELL_M = 10.0       # unchanged starting value for other terrain
COARSE_ON_FLAT_M = 10.0           # warn when cell is coarser than this on a flat plain

# Sample window used when classifying from a terrain_src (metres from the dam).
SAMPLE_HALF_M = 4000.0
SAMPLE_CELL_M = 30.0              # coarse read; classification needs extent, not detail

KIND_FLAT_SECTION_PLAIN = "flat_section_plain"
KIND_OTHER = "other"
KIND_UNKNOWN = "unknown"

FINE_OPTION_LABEL = "5 m (finer, slower)"


def elev_stats(z, cell_m):
    """Stats for a 2D elevation array in metres (NaNs ignored).

    Returns dict: p5, p95, relief_m, mean_slope, n. mean_slope is the mean of
    finite |dz/dx| and |dz/dy| between neighbour cells. Empty / too-small
    arrays return n=0 and NaN metrics.
    """
    a = np.asarray(z, dtype=float)
    cell = float(cell_m)
    out = {"p5": float("nan"), "p95": float("nan"), "relief_m": float("nan"),
           "mean_slope": float("nan"), "n": 0, "cell_m": cell}
    if a.ndim != 2 or cell <= 0:
        return out
    finite = np.isfinite(a)
    n = int(finite.sum())
    out["n"] = n
    if n < 9:
        return out
    vals = a[finite]
    p5, p95 = np.percentile(vals, [5.0, 95.0])
    out["p5"] = float(p5)
    out["p95"] = float(p95)
    out["relief_m"] = float(p95 - p5)

    # neighbour gradients on the finite mask (ignore edges that hit NaN)
    dx = np.abs(np.diff(a, axis=1)) / cell
    dy = np.abs(np.diff(a, axis=0)) / cell
    mx = finite[:, 1:] & finite[:, :-1]
    my = finite[1:, :] & finite[:-1, :]
    parts = []
    if mx.any():
        parts.append(dx[mx])
    if my.any():
        parts.append(dy[my])
    if parts:
        out["mean_slope"] = float(np.mean(np.concatenate(parts)))
    return out


def classify_terrain(stats):
    """Return KIND_FLAT_SECTION_PLAIN, KIND_OTHER, or KIND_UNKNOWN from elev_stats."""
    if not stats or int(stats.get("n") or 0) < 9:
        return KIND_UNKNOWN
    relief = stats.get("relief_m")
    slope = stats.get("mean_slope")
    if relief is None or slope is None or not (math.isfinite(relief) and math.isfinite(slope)):
        return KIND_UNKNOWN
    if relief <= FLAT_PLAIN_RELIEF_M and slope <= FLAT_PLAIN_SLOPE:
        return KIND_FLAT_SECTION_PLAIN
    return KIND_OTHER


def default_cell_m(kind):
    """Suggested starting cell size (m) for the terrain kind."""
    if kind == KIND_FLAT_SECTION_PLAIN:
        return FLAT_PLAIN_DEFAULT_CELL_M
    return OTHER_DEFAULT_CELL_M


def fine_cell_m(kind):
    """Optional finer cell (m) when the engineer can afford it; None if none."""
    if kind == KIND_FLAT_SECTION_PLAIN:
        return FLAT_PLAIN_FINE_CELL_M
    return None


def cell_hint(kind):
    """One-line UI hint for the grid-size control."""
    if kind == KIND_FLAT_SECTION_PLAIN:
        return ("Flat section-road plain: start at {:g} m; {} when you can afford "
                "the run time. Coarser than {:g} m over-spreads the flood "
                "(DWR-2801).".format(FLAT_PLAIN_DEFAULT_CELL_M, FINE_OPTION_LABEL,
                                     COARSE_ON_FLAT_M))
    if kind == KIND_OTHER:
        return ("Starting cell {:g} m. On flat section-road plains use {:g} m "
                "(or {}); coarser cells over-spread.".format(
                    OTHER_DEFAULT_CELL_M, FLAT_PLAIN_DEFAULT_CELL_M, FINE_OPTION_LABEL))
    return ("Starting cell {:g} m. On flat section-road plains use {:g} m "
            "(or {}).".format(OTHER_DEFAULT_CELL_M, FLAT_PLAIN_DEFAULT_CELL_M,
                              FINE_OPTION_LABEL))


def coarse_cell_warning(cell_m, kind):
    """Pre-run warning when a flat-plain run is coarser than 10 m; else None."""
    try:
        cell = float(cell_m)
    except (TypeError, ValueError):
        return None
    if kind != KIND_FLAT_SECTION_PLAIN:
        return None
    if cell <= COARSE_ON_FLAT_M + 1e-9:
        return None
    return (
        "Flat section-road plain with a {:.0f} m cell: on DWR-2801 (Absaraka) a "
        "20 m cell scored CSI 0.36 vs DSS-WISE at >=0.5 ft, 10 m scored 0.43 and "
        "5 m scored 0.57. Use {:g} m (or {} when you can afford it); coarser "
        "cells average away road embankments and ditches so the flood sheets "
        "across fields. The sensitivity tool's cell-size convergence check "
        "compares grids on the same domain.".format(
            cell, FLAT_PLAIN_DEFAULT_CELL_M, FINE_OPTION_LABEL)
    )


def apply_default(current_cell_m, kind, untouched=True):
    """Suggested cell when applying terrain-aware defaults.

    Only changes the value when the terrain is a flat section-road plain, the
    control is still at its starting value (untouched), and the current cell is
    coarser than the flat-plain default. Never auto-picks 5 m (that stays an
    explicit engineer choice). Returns (cell_m, changed).
    """
    try:
        cur = float(current_cell_m)
    except (TypeError, ValueError):
        cur = OTHER_DEFAULT_CELL_M
    want = default_cell_m(kind)
    if kind == KIND_FLAT_SECTION_PLAIN and untouched and cur > want + 1e-9:
        return want, True
    return cur, False


def from_array(z, cell_m):
    """Classify a DEM window: {stats, kind, default_cell_m, fine_cell_m, hint}."""
    stats = elev_stats(z, cell_m)
    kind = classify_terrain(stats)
    return {
        "stats": stats,
        "kind": kind,
        "default_cell_m": default_cell_m(kind),
        "fine_cell_m": fine_cell_m(kind),
        "hint": cell_hint(kind),
    }


def from_terrain(terrain_src, dam_xy, half_m=SAMPLE_HALF_M, cell_m=SAMPLE_CELL_M):
    """Classify a window of terrain_src around dam_xy. Returns from_array result
    or a KIND_UNKNOWN stub when the read fails or is empty."""
    stub = {
        "stats": elev_stats(np.array([[np.nan]]), cell_m),
        "kind": KIND_UNKNOWN,
        "default_cell_m": OTHER_DEFAULT_CELL_M,
        "fine_cell_m": None,
        "hint": cell_hint(KIND_UNKNOWN),
    }
    if terrain_src is None or dam_xy is None:
        return stub
    try:
        x, y = float(dam_xy[0]), float(dam_xy[1])
        half = float(half_m)
        cell = float(cell_m)
        g = terrain_src.read((x - half, y - half, x + half, y + half), cell)
        z = getattr(g, "data", g)
        return from_array(z, cell)
    except Exception:
        return stub


def report_entry(info, cell_m):
    """Compact dict for run_report['terrain_grid']."""
    stats = info.get("stats") or {}
    kind = info.get("kind") or KIND_UNKNOWN
    warn = coarse_cell_warning(cell_m, kind)
    return {
        "kind": kind,
        "relief_m": _round(stats.get("relief_m"), 2),
        "mean_slope": _round(stats.get("mean_slope"), 5),
        "sample_n": int(stats.get("n") or 0),
        "sample_cell_m": _round(stats.get("cell_m"), 2),
        "default_cell_m": info.get("default_cell_m"),
        "fine_cell_m": info.get("fine_cell_m"),
        "cell_m": _round(cell_m, 2),
        "coarse_warning": bool(warn),
    }


def _round(v, n):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return round(v, n)
