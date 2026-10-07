"""Ponds and dugouts from a LiDAR DEM and a water-body polygon (v0.12).

Pure numpy / scipy, no QGIS imports, so it is unit-tested outside QGIS.  Metres, square metres,
cubic metres, an equal-area grid (EPSG:5070).

WHAT IT DECIDES
  For one water body: is the water HELD ABOVE the surrounding ground (an embankment pond), or does
  it sit in a hole (a dugout) or a natural depression?  And how much water is above the natural
  ground, i.e. could run out if the embankment failed?

HOW
  1. Pool elevation: the flat water surface of a hydro-flattened DEM, else the shoreline.
  2. The ground around the water is cut into 16 compass sectors and bands of distance from the
     water edge.  In each sector the RIM is the highest band inside `crest_m` of the edge and the
     TOE is where the ground below the rim stops falling steeper than `face_deg` (the foot of the
     embankment face; natural slopes are gentler), searched up to `ring_m`.
  3. head(sector) = pool - toe.  A sector holds water above the ground ("impounds") when its head
     is at least `min_head_m` and its rim is not below the pool.  Impounded arc >= `min_arc_deg`
     -> embankment pond.
  4. The CREST (top of dam) is the lowest rim of the impounding sectors (the water would spill
     there); the natural GROUND the breach cuts down to is the lowest toe of those sectors.
     For a water body that impounds nothing the crest is the lowest rim of all sectors and the
     ground is the lowest toe, so only water held by a spoil rim or a lip counts.
  5. Storage: above the pool from the DEM (as tool 3), below the pool from the shoreline-slope
     proxy (as tool 3; the LiDAR cannot see below the water).  ABOVE-GRADE storage = storage at the
     crest minus storage at the ground level.  Below `release_min_m3` (or a breach height under
     `min_height_m`, which tool 5 would refuse) the water body has NO BREACH FLOOD.

Every threshold is a PondParams value the engineer chooses; the defaults are starting values, not
standards.  The result is a screening estimate of what the DEM shows, not a survey.
"""
import math
from dataclasses import dataclass, field

import numpy as np

try:  # scipy ships with the QGIS bundles
    from scipy import ndimage as ndi
except Exception:  # pragma: no cover
    ndi = None

from . import storage as st
from . import us_units as uu

FT = 0.3048
AF_M3 = 1233.4818375
N_SECTORS = 16
SECTOR_DEG = 360.0 / N_SECTORS

T_EMBANKMENT = "embankment pond"
T_DUGOUT = "dugout"
T_DEPRESSION = "natural depression"
TYPES = (T_EMBANKMENT, T_DUGOUT, T_DEPRESSION)


@dataclass
class PondParams:
    """Engineer-chosen values. The defaults are starting values ("YOUR value" in the tool)."""
    ring_m: float = 60.0              # how far beyond the water edge to look for the toe
    crest_m: float = 25.0             # the rim is looked for within this distance of the edge
    min_head_m: float = 1.0 * FT      # water this far above the toe counts as held above grade
    min_arc_deg: float = 45.0         # impounded arc of the shore needed for an embankment pond
    face_deg: float = 8.0             # ground falling steeper than this below the rim is the face
    max_depth_m: float = 12.0 * FT    # assumed greatest water depth (the DEM cannot see it)
    dugout_slope_deg: float = 14.0    # bank steeper than this (or a spoil rim) = excavated
    release_min_m3: float = 0.1 * AF_M3   # less water than this above grade = no breach flood
    min_height_m: float = 0.3         # tool 5 will not route a breach lower than this
    flat_tol_m: float = 0.10          # water surface "flat" when its 10-90 % spread is below this
    shape_factor: float = 1.0         # below-pool calibration, as tool 3
    steps: int = 20


@dataclass
class PondResult:
    pond_type: str
    pool_m: float
    pool_method: str
    crest_m: float                    # top of dam (never below pool + min_height_m)
    ground_m: float                   # natural ground the breach cuts down to
    height_m: float                   # crest - ground (the breach height tool 5 uses)
    head_m: float                     # pool - ground (positive: water above the ground)
    freeboard_m: float                # crest - pool
    arc_deg: float                    # impounded part of the shore
    area_m2: float
    below_pool_m3: float
    above_pool_m3: float
    total_m3: float
    above_grade_m3: float
    max_depth_m: float
    shore_slope_deg: float
    bank_slope_deg: float
    crest_len_m: float
    no_flood: bool
    no_flood_reason: str
    breach_rc: tuple                  # (row, col) of the breach cell on the window grid, or None
    flow_az: float                    # degrees from north, clockwise, or None
    confidence: str
    notes: list = field(default_factory=list)
    curve: list = field(default_factory=list)       # [(elev_m, storage_m3), ...] pool -> crest
    sectors: list = field(default_factory=list)     # [(az_deg, rim_m, toe_m, head_m, impounds)]


def _need_scipy():
    if ndi is None:
        raise RuntimeError("scipy is required for the pond analysis")


def sector_of(rows, cols, r0, c0, n=N_SECTORS):
    """Compass sector (0 = north, clockwise) of cells seen from (r0, c0)."""
    dx = np.asarray(cols, float) - c0
    dy = -(np.asarray(rows, float) - r0)
    az = (np.degrees(np.arctan2(dx, dy)) + 360.0) % 360.0
    return np.minimum((az / (360.0 / n)).astype(int), n - 1)


def estimate_pool(dem, water, flat_tol_m=0.10):
    """(pool elevation, method text, spread). A hydro-flattened water surface is (nearly)
    constant: its median. Where the DEM water is not flat (small ponds are rarely flattened) the
    waterline is the polygon's edge cells: their median."""
    _need_scipy()
    ok = water & np.isfinite(dem)
    if not ok.any():
        raise ValueError("the water polygon does not overlap valid terrain")
    inner = ndi.binary_erosion(water) & np.isfinite(dem)
    vals = dem[inner] if inner.sum() >= 4 else dem[ok]
    spread = float(np.percentile(vals, 90) - np.percentile(vals, 10))
    if spread <= flat_tol_m:
        return float(np.median(vals)), "flat water surface in the DEM", spread
    edge = water & ~ndi.binary_erosion(water) & np.isfinite(dem)
    ev = dem[edge] if edge.any() else dem[ok]
    return float(np.median(ev)), "shoreline elevation (the DEM water is not flat)", spread


def depth_proxy(water, cell, slope_deg, max_depth_m, shape_factor=1.0):
    """Water depth (m) per cell: distance from shore x tan(slope), capped. The same proxy as
    storage.below_pool_volume (tool 3); 0 outside the water."""
    _need_scipy()
    dist = ndi.distance_transform_edt(water) * cell
    tan = math.tan(math.radians(max(slope_deg, 0.5)))
    return np.where(water, np.minimum(dist * tan, max_depth_m) * shape_factor, 0.0)


def _profile(dem, water, other, cell, centre, p):
    """Band medians of the ground around the water: Z[sector, band] and the cell bookkeeping."""
    bw = max(float(cell), 2.0)
    nb = max(1, int(math.ceil(p.ring_m / bw)))
    dist, (ir, ic) = ndi.distance_transform_edt(~water, return_indices=True)
    dist = dist * cell
    ring = (dist > 0) & (dist <= p.ring_m) & np.isfinite(dem)
    if other is not None:
        ring &= ~other
    rr, cc = np.nonzero(ring)
    sect = sector_of(rr, cc, *centre)
    band = np.clip(np.ceil(dist[rr, cc] / bw).astype(int) - 1, 0, nb - 1)
    key = sect * nb + band
    order = np.argsort(key, kind="stable")
    ks, zs = key[order], dem[rr, cc][order]
    Z = np.full(N_SECTORS * nb, np.nan)
    if ks.size:
        uniq, start = np.unique(ks, return_index=True)
        ends = np.append(start[1:], ks.size)
        for u, a, b in zip(uniq, start, ends):
            Z[u] = float(np.median(zs[a:b]))
    return {"Z": Z.reshape(N_SECTORS, nb), "bw": bw, "nb": nb, "rr": rr, "cc": cc, "sect": sect,
            "band": band, "ir": ir, "ic": ic}


def _rim_toe(row, near_k, bw, face_deg):
    """(rim, toe, rim band, toe band, still falling at the end) of one sector's band medians.

    The embankment FACE is a run of bands that each fall by at least bw * tan(face_deg) (natural
    slopes fall less), starting within `near_k` bands of the water; of several runs the one with
    the biggest total fall is the face. The rim is where that run starts (the outer edge of the
    crest), the toe where it ends. With no such run the sector has no face: rim = toe = the highest
    ground in the near zone."""
    valid = np.isfinite(row)
    if not valid[:near_k].any():
        return None
    n = len(row)
    step = bw * math.tan(math.radians(face_deg))
    falls = lambda i: i + 1 < n and np.isfinite(row[i]) and np.isfinite(row[i + 1]) and row[i] - row[i + 1] >= step
    best, k = None, 0
    while k < min(near_k, n - 1):
        if falls(k):
            k0 = k
            while falls(k):
                k += 1
            drop = float(row[k0] - row[k])
            if best is None or drop > best[0]:
                best = (drop, k0, k)
        k += 1
    if best is None:
        top = float(np.nanmax(row[:near_k]))
        k_rim = int(np.nonzero(row[:near_k] >= top - 0.05)[0].max())
        return float(row[k_rim]), float(row[k_rim]), k_rim, k_rim, False
    _drop, k0, k1 = best
    still_falling = bool(k1 == int(np.nonzero(valid)[0].max()) and (k1 + 1 >= n or not np.isfinite(row[k1 + 1])))
    return float(row[k0]), float(row[k1]), k0, k1, still_falling


def analyze_pond(dem, water, cell, params=None, other=None, excavated_hint=False):
    """Analyse one water body. `dem` (m, NaN = no data) and `water` (bool) share a grid whose cell
    is `cell` m; the window must reach `params.ring_m` beyond the water. `other` marks other water
    bodies (left out of the ground statistics). `excavated_hint`: the layer's own attribute says
    the water body is excavated (used only to tell a dugout from a natural depression)."""
    _need_scipy()
    p = params or PondParams()
    dem = np.asarray(dem, float).copy()
    dem[dem <= -9998.0] = np.nan
    water = np.asarray(water, bool) & np.isfinite(dem)
    if water.sum() < 4:
        raise ValueError("the water body covers fewer than 4 valid terrain cells at {:g} m".format(cell))
    other = None if other is None else (np.asarray(other, bool) & ~water)
    notes = []

    pool, pool_how, spread = estimate_pool(dem, water, p.flat_tol_m)
    conf_low = "shoreline" in pool_how
    if conf_low:
        notes.append("pool read from the shoreline (the DEM water surface is not flat, spread "
                     "{:.2f} m): +-0.3 m or more".format(spread))
    area = float(water.sum() * cell * cell)

    rws, cls = np.nonzero(water)
    centre = (float(rws.mean()), float(cls.mean()))
    prof = _profile(dem, water, other, cell, centre, p)
    Z, bw, nb = prof["Z"], prof["bw"], prof["nb"]
    near_k = max(1, int(math.ceil(p.crest_m / bw)))

    sec = []                                    # per sector: dict or None
    for s in range(N_SECTORS):
        rt = _rim_toe(Z[s], near_k, bw, p.face_deg)
        if rt is None:
            sec.append(None)
            continue
        rim, toe, k_rim, k_toe, falling = rt
        head = pool - toe
        sec.append({"s": s, "rim": rim, "toe": toe, "k_rim": k_rim, "k_toe": k_toe, "head": head,
                    "falling": falling,
                    "imp": bool(head >= p.min_head_m and rim >= pool - 0.1)})
    valid = [d for d in sec if d is not None]
    if len(valid) < N_SECTORS // 2:
        raise ValueError("terrain covers only {} of {} compass sectors around the water: the clip is "
                         "too small or has no data there".format(len(valid), N_SECTORS))
    if len(valid) < N_SECTORS:
        notes.append("{} of {} compass sectors have no terrain data".format(
            N_SECTORS - len(valid), N_SECTORS))
        conf_low = True
    imp = [d for d in valid if d["imp"]]
    arc = len(imp) * SECTOR_DEG

    bank = st.shore_slope_deg(dem, water, cell, ring_m=max(6.0, 3.0 * cell))
    shore = st.shore_slope_deg(dem, water, cell)
    if bank is None:
        bank = shore if shore is not None else 8.0
    if shore is None:
        shore = 8.0
        notes.append("shore slope defaulted to 8 deg")

    if arc >= p.min_arc_deg:
        ptype, sel = T_EMBANKMENT, imp
    else:
        if imp:
            notes.append("{:.0f} deg of the shore holds water above the ground, under the {:.0f} deg "
                         "needed for an embankment pond".format(arc, p.min_arc_deg))
        sel = valid
        berm = sum(1 for d in valid if d["rim"] - d["toe"] >= p.min_head_m)
        why = []
        if excavated_hint:
            why.append("the layer says excavated")
        if bank >= p.dugout_slope_deg:
            why.append("steep banks ({:.0f} deg)".format(bank))
        if berm >= len(valid) / 2.0:
            why.append("a raised rim round the water")
        if why:
            ptype = T_DUGOUT
            notes.append("dugout because: " + ", ".join(why))
        else:
            ptype = T_DEPRESSION
    if ptype == T_EMBANKMENT and excavated_hint:
        notes.append("the layer says excavated, but the LiDAR shows water held above the ground "
                     "(spoil dike?)")

    crest_raw = min(d["rim"] for d in sel)
    ground = min(d["toe"] for d in sel)
    lead = min(sel, key=lambda d: d["toe"])
    if any(d["falling"] for d in sel if d["imp"]):
        notes.append("the embankment face is still falling steeply at {:.0f} ft from the water: the toe "
                     "may be lower (raise the toe search distance)".format(p.ring_m / uu.FT))
        conf_low = True
    crest = max(crest_raw, pool + p.min_height_m)
    if crest_raw < pool + p.min_height_m:
        notes.append("crest {:.1f} ft is under pool + {:.1f} ft: set to pool + {:.1f} ft".format(
            (crest_raw - pool) / uu.FT, p.min_height_m / uu.FT, p.min_height_m / uu.FT))
        if ptype == T_EMBANKMENT:
            conf_low = True

    # ---- storage (as tool 3) and the volume above the ground
    max_depth = p.max_depth_m
    depth = depth_proxy(water, cell, shore, max_depth, p.shape_factor)
    below = float(depth[water].sum() * cell * cell)
    top = max(crest, ground + 1e-6)
    curve_up = st.above_pool_curve(dem, water, pool, top, cell, max(p.steps, 20))
    ce = np.array([e for e, _ in curve_up]); cv = np.array([v for _, v in curve_up])
    bed = pool - depth[water]
    A = cell * cell

    def vol(z):
        if z >= pool:
            return below + float(np.interp(z, ce, cv))
        return float(np.maximum(z - bed, 0.0).sum() * A)

    above_pool = float(np.interp(crest, ce, cv))
    total = below + above_pool
    above_grade = max(0.0, vol(crest) - vol(ground)) if crest > ground else 0.0
    height = crest - ground
    curve = [(e, v + below) for e, v in st.above_pool_curve(dem, water, pool, crest, cell, p.steps)]

    no_flood, why_none = False, ""
    if height < p.min_height_m:
        no_flood = True
        why_none = ("the lowest rim is only {:.2f} ft above the natural ground: the water sits in a "
                    "hole and nothing stands above grade".format(max(height, 0.0) / FT))
    elif above_grade < p.release_min_m3:
        no_flood = True
        why_none = "{:.3f} ac-ft of water above the natural ground (limit {:g} ac-ft)".format(
            above_grade / AF_M3, p.release_min_m3 / AF_M3)

    # ---- breach point, direction, crest length
    breach_rc, az, crest_len = None, None, None
    if not no_flood:
        m = (prof["sect"] == lead["s"]) & (np.abs(prof["band"] - lead["k_rim"]) <= 1)
        if m.any():
            rr, cc = prof["rr"][m], prof["cc"][m]
            z = dem[rr, cc]
            j = int(np.argmax(z))
            breach_rc = (int(rr[j]), int(cc[j]))
            vx = cc - prof["ic"][rr, cc]
            vy = -(rr - prof["ir"][rr, cc])
            n = np.hypot(vx, vy)
            n[n == 0] = 1.0
            mx, my = float((vx / n).mean()), float((vy / n).mean())
            if abs(mx) + abs(my) > 1e-9:
                az = (math.degrees(math.atan2(mx, my)) + 360.0) % 360.0
        edge = water & ~ndi.binary_erosion(water)
        er, ec = np.nonzero(edge)
        es = sector_of(er, ec, *centre)
        use = {d["s"] for d in sel if d["imp"]} if ptype == T_EMBANKMENT else {d["s"] for d in valid}
        crest_len = float(sum(1 for s in es if s in use) * cell)
        if ptype == T_EMBANKMENT and crest_len <= 0:
            crest_len = None

    if not no_flood and ptype == T_EMBANKMENT and crest - pool < 0.3:
        conf_low = True
    return PondResult(
        pond_type=ptype, pool_m=pool, pool_method=pool_how, crest_m=crest, ground_m=ground,
        height_m=height, head_m=pool - ground, freeboard_m=crest - pool, arc_deg=arc, area_m2=area,
        below_pool_m3=below, above_pool_m3=above_pool, total_m3=total, above_grade_m3=above_grade,
        max_depth_m=float(depth.max()), shore_slope_deg=shore, bank_slope_deg=bank,
        crest_len_m=crest_len, no_flood=no_flood, no_flood_reason=why_none, breach_rc=breach_rc,
        flow_az=az, confidence="low" if conf_low else "medium", notes=notes, curve=curve,
        sectors=[((d["s"] + 0.5) * SECTOR_DEG, d["rim"], d["toe"], d["head"], d["imp"])
                 for d in valid])


def type_counts(results):
    """{type: n, 'no breach flood': n} over an iterable of PondResult."""
    out = {}
    for r in results:
        out[r.pond_type] = out.get(r.pond_type, 0) + 1
        if r.no_flood:
            out["no breach flood"] = out.get("no breach flood", 0) + 1
    return out
