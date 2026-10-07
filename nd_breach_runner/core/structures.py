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
import math
from dataclasses import dataclass, field
from typing import List, Optional

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
