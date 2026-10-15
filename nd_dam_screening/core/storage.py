"""Reservoir storage estimation from a DEM plus a water-body mask.

Pure numpy/scipy (no QGIS imports) so it can be unit-tested outside QGIS.

Why two parts: LiDAR DEMs are hydro-flattened, so the DEM says nothing about
the bed below the water surface.  Storage is therefore
  (a) ABOVE normal pool  -> measured directly from the DEM, and
  (b) BELOW normal pool  -> estimated by projecting the shoreline slope
      inward (depth = distance-from-shore * tan(slope)), capped at a
      maximum depth.
All units here are metres / square metres / cubic metres, in an equal-area CRS.
"""
import math
from dataclasses import dataclass, field

import numpy as np

try:  # scipy ships with the QGIS bundles, but degrade gracefully if absent
    from scipy import ndimage as ndi
except Exception:  # pragma: no cover
    ndi = None


@dataclass
class StorageResult:
    pool_elev: float
    crest_elev: float
    surface_area_m2: float
    below_pool_m3: float
    above_pool_m3: float          # pool -> crest, from the DEM
    total_m3: float
    max_depth_m: float
    shore_slope_deg: float
    confidence: str
    curve: list = field(default_factory=list)   # [(elev_m, storage_m3), ...]
    notes: list = field(default_factory=list)


def pool_elevation(dem, water_mask):
    """Hydro-flattened water is (near) constant; use the median."""
    vals = dem[water_mask & np.isfinite(dem)]
    if vals.size == 0:
        raise ValueError("water mask does not overlap valid DEM cells")
    return float(np.median(vals))


def shore_slope_deg(dem, water_mask, cell, ring_m=30.0):
    """Mean slope of the ground in a ring just outside the water edge."""
    if ndi is None:
        return None
    dist = ndi.distance_transform_edt(~water_mask) * cell
    ring = (dist > 0) & (dist <= ring_m)
    if ring.sum() < 10:
        return None
    filled = np.where(np.isfinite(dem), dem, np.nanmedian(dem))
    gy, gx = np.gradient(filled, cell)
    slope = np.degrees(np.arctan(np.hypot(gx, gy)))
    return float(np.median(slope[ring]))


def _pool_cells(d, water_mask, stage):
    """Cells below `stage` connected (4-connectivity) to the water body."""
    below = d < stage
    lab, _ = ndi.label(below)
    ids = np.unique(lab[water_mask & below])
    ids = ids[ids > 0]
    return np.isin(lab, ids)


def _touches_edge(keep):
    return bool(keep[0, :].any() or keep[-1, :].any() or keep[:, 0].any() or keep[:, -1].any())


def above_pool_curve(dem, water_mask, pool, crest, cell, steps=20):
    """Storage between pool and crest: sum (stage - dem) over cells that are
    below the stage AND connected to the reservoir."""
    return above_pool_curve_guarded(dem, water_mask, pool, crest, cell, steps, guard=False)[0]


# 0.19.0: the "storage leak". Between two stages the pool can spill over a low point of the rim
# (a saddle, the spillway, a road) into the next valley; the flooded region then jumps from the
# reservoir to a whole landscape (Absaraka: 774 -> 26,757 ac-ft in one 0.8 ft step; Aarestad 8,091
# vs 484 ac-ft). Water cannot be stored above the level where it runs out, so the curve stops there.
LEAK_JUMP = 8.0          # a step adds this many times the volume of the step before ...
LEAK_AREA = 3.0          # ... or the flooded area grows this many times in one step
LEAK_MIN_STEP = 2        # the first steps are left alone (a hydro-flattened pool spreads onto its banks)


def above_pool_curve_guarded(dem, water_mask, pool, crest, cell, steps=20, guard=True):
    """(curve, spill elevation or None, note). With guard=True the curve ends at the elevation
    where the pool escapes the reservoir: the flooded cells reach the edge of the DEM window, or
    the volume added in one step jumps (LEAK_JUMP) together with the flooded area (LEAK_AREA).
    The spill elevation is found by bisection between the last good stage and the bad one, and
    the curve is recomputed from the pool to it with the same number of steps."""
    if ndi is None:
        raise RuntimeError("scipy is required for the above-pool volume")
    area = cell * cell
    d = np.where(np.isfinite(dem), dem, np.inf)
    d = np.where(water_mask, np.minimum(d, pool), d)  # water surface = pool

    def at(stage):
        keep = _pool_cells(d, water_mask, stage)
        return float(((stage - d[keep]).sum()) * area), int(keep.sum()), keep

    stages = np.linspace(pool, crest, steps + 1)
    if guard:
        # the pool already escapes at the pool level: the ground past the dam (or a channel through
        # it at DEM resolution) is lower than the pool, so every stage floods the valley below.
        # Above-pool storage is then the pond footprint times the rise (a prism).
        _v0, n0, keep0 = at(pool)
        nw = int(np.asarray(water_mask, bool).sum())
        if nw > 0 and (n0 > LEAK_AREA * nw or (_touches_edge(keep0) and not _touches_edge(water_mask))):
            v_pool = float(((pool - d[water_mask]).clip(min=0)).sum() * area)
            curve = [(float(st), v_pool + nw * area * float(st - pool)) for st in stages]
            note = ("the DEM pool already leaks at the pool level ({:.2f} m, {:.1f} ft): the ground past "
                    "the dam is lower than the pool at this DEM resolution, so storage above the pool is "
                    "the pond area times the rise (prism; DEM storage leak guard)".format(pool, pool / 0.3048))
            return curve, None, note
    curve, cells = [], []
    bad = None
    for k, stage in enumerate(stages):
        vol, n, keep = at(stage)
        if guard and k >= 1:
            edge = _touches_edge(keep) and not _touches_edge(_pool_cells(d, water_mask, stages[k - 1]))
            jump = False
            if k >= LEAK_MIN_STEP and len(curve) >= 2:
                dv_prev = curve[-1][1] - curve[-2][1]
                dv = vol - curve[-1][1]
                jump = dv_prev > 0 and dv > LEAK_JUMP * dv_prev and cells[-1] > 0 and n > LEAK_AREA * cells[-1]
            if edge or jump:
                bad = (k, "the pool runs out past the rim to the border of the DEM window" if edge else
                       "the pool spills into the next low ground (volume x{:.0f} in one step)".format(
                           (vol - curve[-1][1]) / max(curve[-1][1] - curve[-2][1], 1e-9)))
                break
        curve.append((float(stage), vol))
        cells.append(n)
    if bad is None:
        return curve, None, ""
    k, why = bad
    lo, hi = float(stages[k - 1]), float(stages[k])
    n_lo = cells[-1]
    for _i in range(10):                     # bisection for the spill level
        mid = 0.5 * (lo + hi)
        _v, n, keep = at(mid)
        if _touches_edge(keep) or n > LEAK_AREA * max(n_lo, 1):
            hi = mid
        else:
            lo = mid
    spill = lo
    if spill - pool < 1e-3:
        return [(float(pool), curve[0][1])], spill, why
    curve = [(float(st), at(st)[0]) for st in np.linspace(pool, spill, steps + 1)]
    note = ("storage held at the spill level {:.2f} m ({:.1f} ft): above it {} (DEM storage leak "
            "guard)".format(spill, spill / 0.3048, why))
    return curve, spill, note


def below_pool_volume(dem, water_mask, cell, slope_deg, max_depth, shape_factor=1.0):
    """Bathymetry proxy: depth = distance-to-shore * tan(slope), capped."""
    if ndi is None:
        raise RuntimeError("scipy is required for the below-pool volume")
    dist = ndi.distance_transform_edt(water_mask) * cell
    tan = np.tan(np.radians(max(slope_deg, 0.5)))
    depth = np.minimum(dist * tan, max_depth) * shape_factor
    return float(depth[water_mask].sum() * cell * cell), float(depth.max())


def estimate_storage(dem, water_mask, cell, dam_xy_idx=None, crest_radius_m=50.0,
                     known_height_m=None, max_depth_m=None, steps=20,
                     shape_factor=1.0, leak_guard=True):
    """Main entry.  dem/water_mask are 2-D arrays on the same grid.

    dam_xy_idx: (row, col) of the dam point, used to find the crest.
    known_height_m: NID/state dam height, used to cap depth and crest.
    """
    notes = []
    # v0.11.1: a -9999 fill value the raster does not declare as no-data is no-data too (no real
    # ground is below -9998 m); it used to give a "valid" pool at -9999 m
    dem = np.asarray(dem, float)
    if np.any(dem <= -9998.0):
        dem = np.where(dem <= -9998.0, np.nan, dem)
    water_mask = water_mask.astype(bool) & np.isfinite(dem)
    pool = pool_elevation(dem, water_mask)
    area_m2 = float(water_mask.sum() * cell * cell)

    # --- crest elevation -------------------------------------------------
    crest = None
    if dam_xy_idx is not None:
        r, c = dam_xy_idx
        n = int(round(crest_radius_m / cell))
        win = dem[max(r - n, 0): r + n + 1, max(c - n, 0): c + n + 1]
        win = win[np.isfinite(win) & (win > pool)]
        if win.size:
            crest = float(np.percentile(win, 98))
            notes.append("crest from DEM near dam point")
    if crest is not None and known_height_m and crest - pool > known_height_m + 0.3:
        # 0.19.0: the top of dam cannot be more than the dam height above the pool (the pool is
        # above the old stream bed); a higher DEM value near the point is a road, rail or hill
        notes.append("DEM crest {:.1f} ft above the pool, more than the dam height: crest = pool + "
                     "height".format((crest - pool) / 0.3048))
        crest = pool + known_height_m
    if crest is None or crest - pool < 0.3:
        if known_height_m:
            crest = pool + 0.8 * known_height_m
            notes.append("crest = pool + 0.8*height (DEM crest not found)")
        else:
            crest = pool + 1.0
            notes.append("crest defaulted to pool + 1 m")

    # --- slope / depth ----------------------------------------------------
    slope = shore_slope_deg(dem, water_mask, cell)
    if slope is None:
        slope = 8.0
        notes.append("shore slope defaulted to 8 deg")
    if max_depth_m is None:
        max_depth_m = 0.85 * known_height_m if known_height_m else 4.0
        notes.append("max depth from dam height" if known_height_m
                     else "max depth defaulted to 4 m")

    below, dmax = below_pool_volume(dem, water_mask, cell, slope, max_depth_m,
                                    shape_factor)
    curve_above, spill, snote = above_pool_curve_guarded(dem, water_mask, pool, crest, cell, steps,
                                                         guard=leak_guard)
    if spill is not None and known_height_m and crest > spill + 0.01 and len(curve_above) >= 2:
        # 0.19.0: a dam of known height holds water to its crest; above the spill level the fill
        # has left the reservoir, so the curve goes on with the reservoir area at the spill level
        (e1, v1), (e0, v0) = curve_above[-1], curve_above[-2]
        a_spill = (v1 - v0) / (e1 - e0) if e1 > e0 else 0.0
        ext = np.linspace(spill, crest, steps + 1)[1:]
        curve_above = list(curve_above) + [(float(e), v1 + a_spill * float(e - spill)) for e in ext]
        snote = snote.replace("storage held at the spill level", "storage leak at the spill level") + \
            "; above it the curve goes on to the crest {:.1f} ft with the reservoir area at the spill " \
            "level ({:,.1f} ac)".format(crest / 0.3048, a_spill / 4046.8564224)
    elif spill is not None:                  # 0.19.0: water runs out above this level
        crest = min(crest, spill)
    if snote:
        notes.append(snote)
    above = curve_above[-1][1]
    curve = [(e, v + below) for e, v in curve_above]

    conf = "medium"
    if any("defaulted" in s or "not found" in s or "spill level" in s or "leaks at the pool" in s
           for s in notes):
        conf = "low"
    elif known_height_m:
        conf = "medium"
    return StorageResult(pool, crest, area_m2, below, above, below + above,
                         dmax, slope, conf, curve, notes)


def calibrate_below_pool(curve, below_est_m3, inv_normal_m3, lo=0.1, hi=10.0):
    """Use inventory NORMAL-pool storage to correct the estimated below-pool volume.

    Like is compared with like: inventory normal storage vs the estimate AT pool.
    The above-pool part of the curve comes straight from the DEM and is kept as is.
    Returns (new_curve, scale, note).  scale is clamped to [lo, hi]; a clamped value
    means the inventory and the terrain disagree badly and is noted for review.
    """
    if not inv_normal_m3 or inv_normal_m3 <= 0 or not below_est_m3 or below_est_m3 <= 0:
        return list(curve), 1.0, ""
    raw = inv_normal_m3 / below_est_m3
    scale = min(max(raw, lo), hi)
    note = "" if scale == raw else f"inventory/estimate storage ratio {raw:.2f} clamped to {scale:.2f}"
    new = [(e, (v - below_est_m3) + below_est_m3 * scale) for e, v in curve]
    return new, float(scale), note


def estimate_height(dem, water_mask, cell, dam_xy_idx, crest, radius_m=60.0):
    """0.19.0: a dam height for a structure the inventory has none for: the crest minus the
    lowest natural ground within `radius_m` of the dam point outside the water (the downstream
    toe, or the old stream bed at the dam). The 2nd percentile, not the minimum, so one bad DEM
    cell does not set it. Returns (height_m or None, note)."""
    r, c = dam_xy_idx
    n = max(1, int(round(radius_m / cell)))
    r0, c0 = max(r - n, 0), max(c - n, 0)
    win = np.array(dem[r0: r + n + 1, c0: c + n + 1], float)
    wm = water_mask[r0: r + n + 1, c0: c + n + 1]
    yy, xx = np.mgrid[r0: r0 + win.shape[0], c0: c0 + win.shape[1]]
    inside = ((yy - r) ** 2 + (xx - c) ** 2) * cell * cell <= radius_m ** 2
    vals = win[inside & ~wm & np.isfinite(win)]
    if vals.size < 5:
        return None, "height not estimated (no ground cells near the dam point)"
    toe = float(np.percentile(vals, 2))
    h = crest - toe
    if not np.isfinite(h) or h < 0.3:
        return None, "height not estimated (crest only {:.1f} ft above the ground near the dam " \
                     "point)".format(max(h, 0) / 0.3048)
    return float(h), "height ESTIMATED = DEM crest {:.1f} ft - lowest ground within {:.0f} ft of the " \
                     "dam point {:.1f} ft".format(crest / 0.3048, radius_m / 0.3048, toe / 0.3048)


def scale_to_total(curve, target_m3, lo=0.1, hi=10.0):
    """0.19.0: the whole curve scaled so its top (storage at the crest) equals an inventory MAXIMUM
    storage, for records that have a maximum but no normal storage (most DWR unknown structures).
    Returns (curve, scale, note); scale clamped to [lo, hi]."""
    top = curve[-1][1] if curve else 0.0
    if not target_m3 or target_m3 <= 0 or top <= 0:
        return list(curve), 1.0, ""
    raw = target_m3 / top
    k = min(max(raw, lo), hi)
    note = "curve scaled x{:.2f} to the inventory maximum storage".format(k)
    if k != raw:
        note += " (ratio {:.2f} clamped: inventory and terrain disagree)".format(raw)
    return [(e, v * k) for e, v in curve], float(k), note


def hold_at(curve, target_m3):
    """0.19.0: the curve cut where its storage reaches target_m3 (an inventory MAXIMUM), the
    elevation interpolated. Returns (curve, elevation) or (curve, None) when never reached."""
    if not curve or curve[-1][1] <= target_m3:
        return list(curve), None
    out = []
    for (e0, v0), (e1, v1) in zip(curve, curve[1:]):
        out.append((e0, v0))
        if v1 >= target_m3:
            e = e0 + (e1 - e0) * ((target_m3 - v0) / (v1 - v0) if v1 > v0 else 0.0)
            out.append((float(e), float(target_m3)))
            return out, float(e)
    return list(curve), None


def flat_water_mask(dem, cell, dam_rc, search_m, min_area_m2=400.0, tol_m=0.03):
    """0.19.0: a water body taken from the DEM itself, for a structure with no mapped water body
    within the search distance: LiDAR DEMs are hydro-flattened, so a pond is a patch of cells of
    one elevation. The patch (cells within `tol_m` of a neighbour, connected, at least
    `min_area_m2`, elevation spread under 0.15 m) nearest to the dam point within `search_m`.
    The patch may run past the search window (a large pond); it is followed over the whole DEM.
    Returns (full-size mask or None, note)."""
    if ndi is None:
        return None, "scipy missing"
    d = np.asarray(dem, float)
    ok = np.isfinite(d)
    f = np.where(ok, d, -1e9)
    flat = np.zeros_like(ok)
    h = np.abs(f[:, 1:] - f[:, :-1]) <= tol_m
    v = np.abs(f[1:, :] - f[:-1, :]) <= tol_m
    flat[:, 1:] |= h
    flat[:, :-1] |= h
    flat[1:, :] |= v
    flat[:-1, :] |= v
    flat &= ok
    lab, n = ndi.label(flat)
    if n == 0:
        return None, "no flat (hydro-flattened) water in the DEM near the point"
    r, c = dam_rc
    k = int(math.ceil(search_m / cell)) + 1
    r0, r1, c0, c1 = max(r - k, 0), min(r + k + 1, d.shape[0]), max(c - k, 0), min(c + k + 1, d.shape[1])
    sub = lab[r0:r1, c0:c1]
    rr, cc = np.indices(sub.shape)
    dist = np.hypot(rr + r0 - r, cc + c0 - c) * cell
    near = np.unique(sub[(sub > 0) & (dist <= search_m)])
    if near.size == 0:
        return None, "no flat (hydro-flattened) water in the DEM within the search distance"
    sizes = np.bincount(lab.ravel())
    dmins = ndi.minimum(dist, sub, near)
    best = None
    for i, dmin in sorted(zip(near.tolist(), np.atleast_1d(dmins).tolist()), key=lambda t: t[1]):
        if sizes[i] * cell * cell < min_area_m2:
            continue
        m = lab == i
        if float(np.ptp(d[m])) > 0.15:       # a road or a field sloping by steps, not water
            continue
        best = (dmin, m)
        break
    if best is None:
        return None, "no flat (hydro-flattened) water of at least {:.2f} ac within the search " \
                     "distance".format(min_area_m2 / 4046.8564224)
    m = best[1]
    return m, "water body taken from the DEM (flat water surface, {:.1f} ac, {:.0f} ft from the " \
              "point; no mapped water body)".format(m.sum() * cell * cell / 4046.8564224, best[0] / 0.3048)


def _filled(d):
    """Depressions filled to their spill level (morphological reconstruction by erosion, the
    result of a priority flood); NaN cells count as high ground."""
    big = float(np.nanmax(d)) + 10.0
    d = np.where(np.isfinite(d), d, big)
    seed = np.full_like(d, big)
    seed[0, :], seed[-1, :], seed[:, 0], seed[:, -1] = d[0, :], d[-1, :], d[:, 0], d[:, -1]
    fp = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], bool)
    for _i in range(20000):
        new = np.maximum(d, ndi.grey_erosion(seed, footprint=fp))
        if np.array_equal(new, seed):
            break
        seed = new
    return seed, d


def depression_mask(dem, cell, dam_rc, search_m, min_area_m2=400.0, min_depth_m=0.05, bottom_m=0.15,
                    margin_m=300.0):
    """0.19.0: the basin of a DRY pond (no mapped water body and no flat water in the DEM): the
    closed depression of the terrain nearest to the dam point within `search_m` (cells the
    filled DEM raises by more than `min_depth_m`, at least `min_area_m2`). The returned mask is
    the bottom of that basin (cells within `bottom_m` of its lowest cell), so tool 3 measures the
    whole basin above it from the DEM. Returns (full-size mask or None, note)."""
    if ndi is None:
        return None, "scipy missing"
    d = np.asarray(dem, float)
    r, c = dam_rc
    k = int(math.ceil((search_m + margin_m) / cell))
    r0, r1, c0, c1 = max(r - k, 0), min(r + k + 1, d.shape[0]), max(c - k, 0), min(c + k + 1, d.shape[1])
    w = d[r0:r1, c0:c1]
    if not np.isfinite(w).any():
        return None, "no terrain near the point"
    f, wf = _filled(w)
    dep = f - wf
    lab, n = ndi.label(dep > min_depth_m)
    if n == 0:
        return None, "no closed depression in the DEM near the point"
    rr, cc = np.indices(w.shape)
    dist = np.hypot(rr + r0 - r, cc + c0 - c) * cell
    ids = np.arange(1, n + 1)
    sizes = ndi.sum(np.ones_like(dep), lab, ids) * cell * cell
    dmin = ndi.minimum(dist, lab, ids)
    ok = [(float(dm), int(i)) for i, s, dm in zip(ids, sizes, dmin) if s >= min_area_m2 and dm <= search_m]
    if not ok:
        return None, "no closed depression of at least {:.2f} ac within the search distance".format(
            min_area_m2 / 4046.8564224)
    dm, i = min(ok)
    basin = lab == i
    low = float(wf[basin].min())
    sub = basin & (wf <= low + bottom_m)
    m = np.zeros(d.shape, bool)
    m[r0:r1, c0:c1] = sub
    depth = float(dep[basin].max())
    vol = float(dep[basin].sum() * cell * cell)
    return m, ("no water in the DEM: the dry basin nearest the point used ({:.2f} ac, {:.1f} ft deep, "
               "{:.2f} ac-ft to its spill level, {:.0f} ft from the point)".format(
                   basin.sum() * cell * cell / 4046.8564224, depth / 0.3048, vol / 1233.48183754752, dm / 0.3048))


def set_top(curve, target_m3):
    """0.19.0: the curve made to hold target_m3 (an inventory MAXIMUM storage) at its top, the top
    of dam. Only the part above the first point (the pool, which a normal-storage calibration has
    already set) is stretched; when the target is at or below the pool storage the whole curve is
    scaled. Returns (curve, factor on the stretched part)."""
    if not curve or not target_m3 or target_m3 <= 0:
        return list(curve), 1.0
    v0, top = curve[0][1], curve[-1][1]
    if target_m3 > v0 and top > v0:
        k = (target_m3 - v0) / (top - v0)
        return [(e, v0 + (v - v0) * k) for e, v in curve], float(k)
    k = target_m3 / top if top > 0 else 1.0
    return [(e, v * k) for e, v in curve], float(k)


AF_M3 = 1233.48183754752      # one acre-foot in cubic metres
FT_M = 0.3048


NO_BASIN = "no basin in the DEM within the search distance"
SHALLOW_BASIN_WHY = "the basin found in the DEM is only {:.1f} ft deep against a dam height of " \
                    "{:.1f} ft, so it is not the reservoir of this dam"


def inventory_curve(dem, dam_rc, height_m, volume_m3, which="maximum", why=NO_BASIN):
    """0.26.0: the last-resort stage-storage curve of a dam with no basin in the DEM (no mapped
    water body, no flat water and no closed depression within the search distance) but an
    inventory storage and a height (0.28.3: `why` says instead why the DEM basin was not used):
    a quadratic curve from the bed (the median DEM elevation of
    the 5 x 5 cells around the dam point, NaN ignored) to the crest (bed + height) that holds the
    inventory volume at the top: 11 points (bed + i*height/10, V*(i/10)^2). `which` names the
    inventory value used in the note ("maximum" / "normal"). Returns a StorageResult with
    confidence low, or None when the DEM has no elevation around the point, the height is
    0.5 m or less, or the volume is not positive (the caller then fails the dam as before)."""
    try:
        h, v = float(height_m), float(volume_m3)
    except (TypeError, ValueError):
        return None
    if not (np.isfinite(h) and np.isfinite(v)) or h <= 0.5 or v <= 0:
        return None
    d = np.asarray(dem, float)
    r, c = int(dam_rc[0]), int(dam_rc[1])
    if r < 0 or c < 0 or r >= d.shape[0] or c >= d.shape[1]:
        return None
    win = d[max(r - 2, 0):r + 3, max(c - 2, 0):c + 3]
    vals = win[np.isfinite(win)]
    if vals.size == 0:
        return None
    bed = float(np.median(vals))
    crest = bed + h
    curve = [(bed + i * h / 10.0, v * (i / 10.0) ** 2) for i in range(11)]
    note = ("ASSUMPTION: {}; stage-storage curve assumed "
            "from the inventory {} storage {:,.0f} ac-ft and height {:.1f} ft (quadratic); verify the "
            "location and the DWR values".format(why or NO_BASIN, which, v / AF_M3, h / FT_M))
    return StorageResult(bed, crest, 0.0, 0.0, v, v, h, 0.0, "low", curve, [note])
