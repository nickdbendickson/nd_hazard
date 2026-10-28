"""Culverts, bridges and other openings through embankments.

Bare-earth LiDAR leaves a road embankment as a solid wall across the channel
(a culvert cannot be seen) and removes bridge decks (the opening is already in
the ground). Each structure is a line from its upstream end to its downstream
end:

* LISFLOOD-FP: its inertial solver has no weir or culvert links, so every
  structure is cut through the embankment in the DEM (an open cut: no roof, so
  no pressure flow and a capacity that a real culvert would not have).
* ANUGA: a bridge or notch is cut into the DEM the same way; a culvert stays
  in the DEM and is modelled with ANUGA's culvert operators (Boyd box or
  pipe: inlet control, outlet control, losses), which do limit the flow.

The report says which representation each structure got.
"""
from dataclasses import dataclass
from typing import Optional

import numpy as np

from . import conditioning as cond

KIND_CULVERT = "culvert"
KIND_BRIDGE = "bridge"
KIND_NOTCH = "notch"
KINDS = (KIND_CULVERT, KIND_BRIDGE, KIND_NOTCH)


@dataclass
class Structure:
    kind: str
    line: np.ndarray                    # upstream end first, work CRS (m)
    width_m: float = 1.0                # box span or pipe diameter (one barrel)
    height_m: float = 1.0               # rise (equals width for a pipe)
    barrels: int = 1
    shape: str = "box"                  # "box" or "pipe"
    invert_up_m: Optional[float] = None
    invert_dn_m: Optional[float] = None
    manning: float = 0.013
    losses: float = 1.0
    name: str = ""
    invert_note: str = ""               # 3.5: what became of inventory inverts (reconcile_inverts)
    invert_inventory: Optional[tuple] = None   # (up, dn) metres as the inventory gave them

    def corridor_width(self, cell):
        return max(cell, self.width_m * max(1, self.barrels) * 1.1)

    def notch(self, cell):
        return cond.Notch(self.line, self.corridor_width(cell), self.invert_up_m,
                          self.invert_dn_m, name=self.name or self.kind)

    def to_dict(self):
        d = {k: getattr(self, k) for k in ("kind", "width_m", "height_m", "barrels", "shape",
                                           "invert_up_m", "invert_dn_m", "manning", "losses",
                                           "name")}
        d["line"] = np.asarray(self.line, dtype=float).tolist()
        return d


def structure_edits(structures, cell, models):
    """Notches to cut for `models` ("lisflood" or "anuga")."""
    out = []
    for s in structures:
        if s.kind in (KIND_BRIDGE, KIND_NOTCH) or models == "lisflood":
            out.append(s.notch(cell))
    return out


def anuga_culverts(structures, dem, mesh_polygon=None):
    """ANUGA culvert specs for the worker (dicts with end points and inverts
    read from the DEM), and notes for those that cannot be used."""
    specs, notes = [], []
    for s in structures:
        if s.kind != KIND_CULVERT:
            continue
        line = np.asarray(s.line, dtype=float)
        if len(line) < 2:
            notes.append("Culvert {}: needs a line with two ends.".format(s.name or "?"))
            continue
        ends = np.array([line[0], line[-1]])
        if polyline_len(line) < 2.0 * max(s.width_m, 1.0):
            notes.append("Culvert {}: shorter than twice its width; the ANUGA operator needs "
                         "more distance between the ends.".format(s.name or "?"))
            continue
        if mesh_polygon is not None and not cond.points_in_polygon(
                ends[:, 0], ends[:, 1], mesh_polygon).all():
            notes.append("Culvert {}: an end lies outside the ANUGA mesh (the flooded area); "
                         "left out of the ANUGA run.".format(s.name or "?"))
            continue
        z = dem.sample_bilinear(ends[:, 0], ends[:, 1])
        inv = [s.invert_up_m if s.invert_up_m is not None else float(z[0]),
               s.invert_dn_m if s.invert_dn_m is not None else float(z[1])]
        if not np.all(np.isfinite(inv)):
            notes.append("Culvert {}: no DEM at an end and no invert given.".format(s.name or "?"))
            continue
        specs.append({"end_points": ends.tolist(), "invert_elevations": inv,
                      "width": s.width_m, "height": s.height_m, "barrels": int(s.barrels),
                      "shape": s.shape, "manning": s.manning, "losses": s.losses,
                      "apron": max(2.0, 1.0 * s.width_m), "name": s.name})
    return specs, notes


def polyline_len(line):
    return cond.polyline_length(line)


# ----------------------------------------------------------- inventory inverts --
FT = 0.3048


def lidar_bed(grid, xy, radius):
    """The lowest finite ground within `radius` of xy on a Grid (metres), or None."""
    try:
        X, Y = np.meshgrid(grid.x_centers(), grid.y_centers())
        z = grid.masked()                                        # no-data as NaN
        near = (np.hypot(X - xy[0], Y - xy[1]) <= radius) & np.isfinite(z)
        if not near.any():
            return None
        return float(np.nanmin(z[near]))
    except Exception:                                            # noqa
        return None


CHANNEL_REACH_M = 30.0     # how far past each end the channel bed is looked for
CHANNEL_NEAR_M = 10.0      # the bed at the end is read over the first 10 m
CHANNEL_BED_PCT = 10.0     # low percentile of the slope-corrected thalweg
CHANNEL_MIN_BINS = 4       # thalweg points needed to fit the channel's slope
CHANNEL_MIN_DROP_M = 0.15


def thalweg_profile(grid, line, end, half, reach=CHANNEL_REACH_M):
    """(along, bed) arrays of the channel beyond one end of a culvert line: the
    lowest finite ground in each 1 m (or one-cell) bin of a strip from the end outward
    along the line's own direction (0..reach m), up to `half` (at least a cell) to either
    side. None without ground there."""
    line = np.asarray(line, dtype=float)
    p, q = (line[0], line[1]) if end == 0 else (line[-1], line[-2])
    u = p - q
    n_ = float(np.hypot(*u))
    if n_ <= 0:
        return None
    u = u / n_
    X, Y = np.meshgrid(grid.x_centers(), grid.y_centers())
    z = grid.masked()
    dx, dy = X - p[0], Y - p[1]
    along = dx * u[0] + dy * u[1]
    across = np.abs(-dx * u[1] + dy * u[0])
    w = max(float(half), float(abs(grid.dx)))
    near = (along >= 0) & (along <= reach) & (across <= w) & np.isfinite(z)
    if not near.any():
        return None
    a, b = along[near], z[near]
    step = max(1.0, float(abs(grid.dx)))
    k = np.floor(a / step).astype(int)
    bins = np.unique(k)
    return (np.array([(i + 0.5) * step for i in bins]),
            np.array([float(b[k == i].min()) for i in bins]))


def channel_slope(along, bed):
    """The bed's fall per metre along the profile (Theil-Sen: the median of the
    pairwise slopes). 0 with too few points."""
    if along is None or len(along) < CHANNEL_MIN_BINS:
        return 0.0
    i, j = np.triu_indices(len(along), 1)
    d = along[j] - along[i]
    ok = d > 0
    if not ok.any():
        return 0.0
    return float(np.median((bed[j] - bed[i])[ok] / d[ok]))


def channel_bed(grid, line, end, half, reach=CHANNEL_REACH_M, near=CHANNEL_NEAR_M,
                pct=CHANNEL_BED_PCT):
    """The channel bed just beyond one end of a culvert line, from the strip of
    thalweg_profile. None without ground there.

    The channel's slope is fitted over the whole reach (channel_slope), each thalweg
    point is carried back to the end along it, and the bed is the `pct` percentile
    of those values over the first `near` m (a low percentile, not the minimum, so
    one pit does not decide it)."""
    try:
        prof = thalweg_profile(grid, line, end, half, reach)
        if prof is None:
            return None
        along, bed = prof
        slope = channel_slope(along, bed)
        at_end = bed - slope * along
        sel = along <= max(float(near), 1.0)
        if not sel.any():
            sel = np.ones_like(along, dtype=bool)
        return float(np.percentile(at_end[sel], pct))
    except Exception:
        return None


def end_bed(disc, channel):
    """The LiDAR bed at one end: the channel beyond the end when it reads at least
    CHANNEL_MIN_DROP_M below the disc at the end, else the disc."""
    if disc is None:
        return channel
    if channel is None or disc - channel < CHANNEL_MIN_DROP_M:
        return disc
    return channel


def reconcile_inverts(structures, read_dem, tol_m, cell):
    """3.5: inventory inlet / outlet elevations (NDDOT pipes, a user's layer) against the LiDAR.

    The models only ever LOWER ground to an invert, so an inventory elevation that is wrong
    against the DEM trenches the stream (too low) or leaves the embankment uncut (too high).
    For each structure whose inverts were given:
      * both ends within `tol_m` of the LiDAR bed -> the inventory inverts are used as given
        (slope and any perch survive);
      * both ends off by about the same amount (within 1 ft of each other) -> a datum shift:
        both moved by the mean offset;
      * otherwise -> the LiDAR bed at both ends (the inventory values kept for the report).
    A cover check notes a rise that does not fit under the road crown in the LiDAR.
    `read_dem(bounds, cell)` returns a Grid. Returns the notes; the structures are updated in
    place (invert_inventory keeps the given values)."""
    notes = []
    for s in structures:
        if s.invert_up_m is None and s.invert_dn_m is None:
            continue
        line = np.asarray(s.line, dtype=float)
        if len(line) < 2:
            continue
        s.invert_inventory = (s.invert_up_m, s.invert_dn_m)
        half = 0.5 * s.corridor_width(cell)
        r = 2.0 * half + cell
        pad = r + 2.0 * cell
        bounds = (min(line[:, 0]) - pad, min(line[:, 1]) - pad, max(line[:, 0]) + pad, max(line[:, 1]) + pad)
        try:
            g = read_dem(bounds, cell)
        except Exception as e:                                   # noqa
            s.invert_note = "inventory inverts kept unchecked (no DEM here: {})".format(e)
            notes.append("{}: {}".format(s.name or s.kind, s.invert_note))
            continue
        bed_up, bed_dn = lidar_bed(g, line[0], r), lidar_bed(g, line[-1], r)
        inv_up, inv_dn = s.invert_up_m, s.invert_dn_m
        off_up = (inv_up - bed_up) if (inv_up is not None and bed_up is not None) else None
        off_dn = (inv_dn - bed_dn) if (inv_dn is not None and bed_dn is not None) else None
        offs = [o for o in (off_up, off_dn) if o is not None]
        name = s.name or s.kind

        def ft(v):
            return "{:+.1f} ft".format(v / FT)
        if not offs:
            s.invert_note = "inventory inverts kept unchecked (no DEM at the ends)"
        elif all(abs(o) <= tol_m for o in offs):
            s.invert_note = "inventory inverts used ({} / {} from the LiDAR bed, within {:.1f} ft)".format(
                ft(off_up) if off_up is not None else "n/a", ft(off_dn) if off_dn is not None else "n/a", tol_m / FT)
            if inv_up is None and bed_up is not None:
                s.invert_up_m = bed_up + (off_dn or 0.0)
            if inv_dn is None and bed_dn is not None:
                s.invert_dn_m = bed_dn + (off_up or 0.0)
        elif len(offs) == 2 and abs(off_up - off_dn) <= 1.0 * FT:
            shift = 0.5 * (off_up + off_dn)
            s.invert_up_m, s.invert_dn_m = inv_up - shift, inv_dn - shift
            s.invert_note = ("inventory elevations {} from the LiDAR at both ends: taken as a datum shift, "
                             "moved to the LiDAR (slope kept)").format(ft(shift))
        else:
            s.invert_up_m, s.invert_dn_m = None, None
            s.invert_note = ("inventory elevations disagree with the LiDAR ({} / {}): LiDAR inverts used").format(
                ft(off_up) if off_up is not None else "n/a", ft(off_dn) if off_dn is not None else "n/a")
        # cover (culverts): does the rise fit under the road crown in the LiDAR?
        crown = None
        if s.kind == KIND_CULVERT:
            try:
                X, Y = np.meshgrid(g.x_centers(), g.y_centers())
                z = g.masked()
                d, _sv = cond.polyline_distance(X, Y, line)
                corr = (d <= half) & np.isfinite(z)
                crown = float(np.nanmax(z[corr])) if corr.any() else None
            except Exception:                                    # noqa
                crown = None
        used = [v for v in (s.invert_up_m, s.invert_dn_m) if v is not None]
        if used and crown is not None:
            top = max(used) + s.height_m
            if top > crown + 0.3:
                s.invert_note += "; the {:.1f} ft rise does not fit under the road crown in the LiDAR " \
                                 "(cover {:+.1f} ft): check the size or the elevations".format(
                                     s.height_m / FT, (crown - top) / FT)
        notes.append("{}: {}".format(name, s.invert_note))
    return notes


def describe(structures):
    """One line per structure for the run report."""
    rows = []
    for s in structures:
        if s.kind == KIND_CULVERT:
            rows.append("{} (culvert, {} x {:.2f} x {:.2f} m {}): LISFLOOD-FP open cut; ANUGA "
                        "culvert operator{}".format(s.name or "?", s.barrels, s.width_m,
                                                    s.height_m, s.shape,
                                                    "; " + s.invert_note if s.invert_note else ""))
        else:
            rows.append("{} ({}, {:.1f} m span): cut through the embankment in both models{}".format(
                s.name or "?", s.kind, s.width_m, "; " + s.invert_note if s.invert_note else ""))
    return rows


# --- 3.13.0: culvert candidates (idea from the DamSafety audit, written here) ----------------
CANDIDATE_MERGE_M = 30.0      # crossings closer than this to each other or to a given structure are one


def _seg_cross(a, b, c, d):
    """(t, (x, y)) where segment a-b crosses c-d (t along a-b), or None."""
    rx, ry = b[0] - a[0], b[1] - a[1]
    sx, sy = d[0] - c[0], d[1] - c[1]
    den = rx * sy - ry * sx
    if abs(den) < 1e-12:
        return None
    t = ((c[0] - a[0]) * sy - (c[1] - a[1]) * sx) / den
    u = ((c[0] - a[0]) * ry - (c[1] - a[1]) * rx) / den
    if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
        return t, (a[0] + t * rx, a[1] + t * ry)
    return None


def culvert_candidates(roads, path, structures=(), merge_m=CANDIDATE_MERGE_M):
    """Where a road crosses the flood's flow path there is probably a culvert or a bridge. These
    are CANDIDATES for the engineer to check, not structures: they are listed in the run report
    and in the HEC-RAS export, and never put in the model (their size is unknown, and a guessed
    size can raise or lower the depths).

    roads: polylines [(x, y), ...] in the work CRS; path: the flow trace [(x, y), ...];
    structures: the run's Structure objects (a crossing near one of them is already covered).
    Returns [{"name", "x", "y", "along_mi", "road", "angle_deg", "source": "candidate",
    "surveyed": False}] sorted downstream."""
    pts = [tuple(map(float, p)) for p in (path or [])]
    if len(pts) < 2:
        return []
    cum = [0.0]
    for a, b in zip(pts[:-1], pts[1:]):
        cum.append(cum[-1] + float(np.hypot(b[0] - a[0], b[1] - a[1])))
    given = []
    for s in structures or []:
        ln = np.asarray(getattr(s, "line", []), float)
        if len(ln):
            given.append(ln.mean(axis=0))
    out = []
    for ri, road in enumerate(roads or []):
        r = [tuple(map(float, p)) for p in np.asarray(road, float)]
        for j in range(len(r) - 1):
            for i in range(len(pts) - 1):
                hit = _seg_cross(pts[i], pts[i + 1], r[j], r[j + 1])
                if hit is None:
                    continue
                t, (x, y) = hit
                along = cum[i] + t * (cum[i + 1] - cum[i])
                if any(np.hypot(x - g[0], y - g[1]) < merge_m for g in given):
                    continue
                if any(np.hypot(x - o["x"], y - o["y"]) < merge_m for o in out):
                    continue
                rdx, rdy = r[j + 1][0] - r[j][0], r[j + 1][1] - r[j][1]
                fdx, fdy = pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]
                cosang = abs(rdx * fdx + rdy * fdy) / max(
                    1e-9, np.hypot(rdx, rdy) * np.hypot(fdx, fdy))
                out.append({"x": round(x, 2), "y": round(y, 2), "along_mi": along / 1609.344,
                            "road": ri + 1,
                            "angle_deg": round(float(np.degrees(np.arccos(min(1.0, cosang)))), 0),
                            "source": "candidate", "surveyed": False})
    out.sort(key=lambda o: o["along_mi"])
    for k, o in enumerate(out, 1):
        o["along_mi"] = round(o["along_mi"], 3)
        o["name"] = "candidate {} (road {} at {:.2f} mi)".format(k, o["road"], o["along_mi"])
    return out


def write_candidates_csv(path, cands):
    with open(path, "w") as f:
        # 3.26.7: the inventory crossing near the candidate, when the run had one (gaps.py)
        f.write("name,x,y,along_mi,road,angle_deg,source,surveyed,inventory,inventory_kind,"
                "inventory_span_ft,inventory_rise_ft,inventory_barrels,inventory_distance_m\n")
        for c in cands:
            inv = c.get("inventory") or {}
            f.write('"{}",{},{},{},{},{},{},no,"{}",{},{},{},{},{}\n'.format(
                c["name"], c["x"], c["y"], c["along_mi"], c["road"], c["angle_deg"],
                c["source"], inv.get("name", ""), inv.get("kind", ""),
                "" if inv.get("span_ft") is None else inv["span_ft"],
                "" if inv.get("rise_ft") is None else inv["rise_ft"],
                inv.get("barrels", ""), inv.get("distance_m", "")))
