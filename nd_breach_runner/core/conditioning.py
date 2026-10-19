"""Hydro-conditioning of the DEM.

LiDAR sees the water surface, not the bed below it, and bare-earth models
leave road embankments across the channel where a culvert or bridge actually
lets the water through. These edits fix that on the fly, on whatever grid the
models ask for, so the source DEM is never modified:

* `ChannelBurn`: cut a channel of a given width and depth along a line, with
  the bed forced to fall steadily downstream (a pit in the LiDAR profile does
  not dam the channel).
* `PondLower`: lower a pond to its bed (LiDAR shows the flat pool surface) and
  return the initial water depth, so the pond starts full (tailwater).
* `Notch`: open a corridor through an embankment (bridge opening, culvert cut,
  breached road) down to an invert profile.
* v3.7.0 `EmbankmentInsert`: a proposed dam. The crest line and the slopes live in
  dam.json (extra["proposed_embankment"]); the embankment is raised onto whatever
  grid is read. The source DEM is not modified, and raising a grid that already
  has the crest does nothing further.
"""
import math
from dataclasses import dataclass
from typing import List, Optional

import numpy as np


# v3.4.0 building footprints: how they are represented (BUILDINGS parameter)
BUILDINGS_NONE = "none"
BUILDINGS_RAISE = "raise"
BUILDINGS_ROUGHNESS = "roughness"
BUILDINGS_MODES = (BUILDINGS_NONE, BUILDINGS_RAISE, BUILDINGS_ROUGHNESS)
BUILDINGS_LABELS = {
    BUILDINGS_NONE: "Not represented (bare terrain)",
    BUILDINGS_RAISE: "Raise the DEM at building footprints (flat-topped block)",
    BUILDINGS_ROUGHNESS: "Roughness: Manning's n at building footprints",
}
# the engineer's starting values, not standards: a one-storey house is about 10 ft to the
# eaves; 0.15 is a common "built-up area" roughness in flood-model practice
BUILDING_RAISE_FT_DEFAULT = 10.0
BUILDING_N_DEFAULT = 0.15
BUILDINGS_NOTE = ("Building footprints are an engineer's choice, not a requirement of ND "
                  "REG_05.2026: the raise height and the roughness are screening values you "
                  "choose. Only polygon footprints can be used; point structures are ignored "
                  "for this (the model keeps bare terrain there).")


def points_in_polygon(px, py, poly):
    """Even-odd ray casting; px, py arrays; poly (N, 2)."""
    px = np.asarray(px, dtype=float)
    py = np.asarray(py, dtype=float)
    poly = np.asarray(poly, dtype=float)
    inside = np.zeros(px.shape, dtype=bool)
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        cross = (yi > py) != (yj > py)
        with np.errstate(divide="ignore", invalid="ignore"):
            xint = (xj - xi) * (py - yi) / (yj - yi) + xi
        inside ^= cross & (px < xint)
        j = i
    return inside


def polyline_distance(px, py, line):
    """Distance from points to a polyline and the along-line distance of the
    nearest point. px, py arrays of any shape; line (N, 2)."""
    line = np.asarray(line, dtype=float)
    px = np.asarray(px, dtype=float)
    py = np.asarray(py, dtype=float)
    best = np.full(px.shape, np.inf)
    along = np.zeros(px.shape)
    s0 = 0.0
    for i in range(len(line) - 1):
        ax, ay = line[i]
        bx, by = line[i + 1]
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        seg = math.sqrt(seg2)
        if seg2 == 0:
            continue
        t = np.clip(((px - ax) * dx + (py - ay) * dy) / seg2, 0.0, 1.0)
        d = np.hypot(px - (ax + t * dx), py - (ay + t * dy))
        better = d < best
        best = np.where(better, d, best)
        along = np.where(better, s0 + t * seg, along)
        s0 += seg
    return best, along


def polyline_length(line):
    line = np.asarray(line, dtype=float)
    return float(np.hypot(*np.diff(line, axis=0).T).sum()) if len(line) > 1 else 0.0


def densify(line, spacing):
    """Points along a polyline every `spacing` metres (ends included)."""
    line = np.asarray(line, dtype=float)
    if len(line) < 2:
        return line.copy()
    seg = np.hypot(*np.diff(line, axis=0).T)
    s = np.concatenate([[0.0], np.cumsum(seg)])
    n = max(2, int(math.ceil(s[-1] / spacing)) + 1)
    ss = np.linspace(0.0, s[-1], n)
    return np.column_stack([np.interp(ss, s, line[:, 0]), np.interp(ss, s, line[:, 1])])


def _window(grid, xs, ys, margin):
    """Row and column slices covering the points plus a margin, clipped."""
    x0, x1 = float(np.min(xs)) - margin, float(np.max(xs)) + margin
    y0, y1 = float(np.min(ys)) - margin, float(np.max(ys)) + margin
    c0 = int(math.floor((x0 - grid.x0) / grid.dx))
    c1 = int(math.ceil((x1 - grid.x0) / grid.dx)) + 1
    r0 = int(math.floor((grid.y0 - y1) / grid.dx))
    r1 = int(math.ceil((grid.y0 - y0) / grid.dx)) + 1
    r0, c0 = max(0, r0), max(0, c0)
    r1, c1 = min(grid.ny, r1), min(grid.nx, c1)
    if r1 <= r0 or c1 <= c0:
        return None
    return r0, r1, c0, c1


def _xy_window(grid, w):
    r0, r1, c0, c1 = w
    xs = grid.x0 + (np.arange(c0, c1) + 0.5) * grid.dx
    ys = grid.y0 - (np.arange(r0, r1) + 0.5) * grid.dx
    return np.meshgrid(xs, ys)


@dataclass
class ChannelBurn:
    """Cut a channel along `line` (upstream to downstream)."""
    line: np.ndarray
    width_m: float = 10.0            # bottom width
    depth_m: float = 1.0             # below the enforced longitudinal profile
    side_h: float = 3.0              # bank slope, H in H:1V
    name: str = "channel"

    def apply(self, grid, z, init):
        line = np.asarray(self.line, dtype=float)
        if len(line) < 2:
            return 0
        half = 0.5 * max(self.width_m, grid.dx)
        run = max(2.0 * grid.dx, self.depth_m * self.side_h * 1.5)
        w = _window(grid, line[:, 0], line[:, 1], half + run + grid.dx)
        if w is None:
            return 0
        r0, r1, c0, c1 = w
        X, Y = _xy_window(grid, w)
        sub = z[r0:r1, c0:c1]
        d, s = polyline_distance(X, Y, line)
        # longitudinal profile: lowest ground across the bottom width at each station,
        # forced to fall (or stay level) downstream
        total = polyline_length(line)
        stn = np.arange(0.0, total + 0.5 * grid.dx, grid.dx)
        prof = np.full(len(stn), np.nan)
        near = (d <= half + 0.5 * grid.dx) & np.isfinite(sub)
        if near.any():
            sn, zn = s[near], sub[near]
            idx = np.clip(np.round(sn / grid.dx).astype(int), 0, len(stn) - 1)
            for k in np.unique(idx):
                prof[k] = zn[idx == k].min()
        good = np.isfinite(prof)
        if not good.any():
            return 0
        prof = np.interp(stn, stn[good], prof[good])
        prof = np.minimum.accumulate(prof)
        bed = np.interp(s, stn, prof) - self.depth_m
        target = bed + np.clip(d - half, 0.0, None) / max(self.side_h, 0.1)
        cut = (d <= half + run) & np.isfinite(sub) & (target < sub)
        sub[cut] = target[cut]
        z[r0:r1, c0:c1] = sub
        return int(cut.sum())


@dataclass
class PondLower:
    """Lower a pond whose LiDAR surface is the water surface. `polygon` is the
    pond outline; the water surface is the median LiDAR elevation inside it
    unless `wse_m` is given. The bed is `depth_m` below the surface (mean
    depth); the initial water depth is what fills it again."""
    polygon: np.ndarray
    depth_m: float = 1.5
    wse_m: Optional[float] = None
    name: str = "pond"

    def apply(self, grid, z, init):
        poly = np.asarray(self.polygon, dtype=float)
        w = _window(grid, poly[:, 0], poly[:, 1], grid.dx)
        if w is None:
            return 0
        r0, r1, c0, c1 = w
        X, Y = _xy_window(grid, w)
        sub = z[r0:r1, c0:c1]
        inside = points_in_polygon(X, Y, poly) & np.isfinite(sub)
        if not inside.any():
            return 0
        wse = self.wse_m if self.wse_m is not None else float(np.median(sub[inside]))
        # only cells at or near the water surface are pond; higher cells are island / bank
        pond = inside & (sub <= wse + 0.15)
        bed = wse - self.depth_m
        newz = np.where(pond, np.minimum(sub, bed), sub)
        sub_init = np.where(pond, np.clip(wse - newz, 0.0, None), 0.0)
        z[r0:r1, c0:c1] = newz
        init[r0:r1, c0:c1] = np.maximum(init[r0:r1, c0:c1], sub_init)
        return int(pond.sum())


@dataclass
class Notch:
    """Open a corridor through an embankment along `line` (upstream to
    downstream) down to the invert profile. Inverts default to the lowest
    ground next to each end of the line."""
    line: np.ndarray
    width_m: float = 5.0
    invert_up_m: Optional[float] = None
    invert_dn_m: Optional[float] = None
    name: str = "notch"

    def apply(self, grid, z, init):
        line = np.asarray(self.line, dtype=float)
        if len(line) < 2:
            return 0
        half = 0.5 * max(self.width_m, grid.dx)
        w = _window(grid, line[:, 0], line[:, 1], half + grid.dx)
        if w is None:
            return 0
        r0, r1, c0, c1 = w
        X, Y = _xy_window(grid, w)
        sub = z[r0:r1, c0:c1]
        d, s = polyline_distance(X, Y, line)
        total = max(polyline_length(line), 1e-9)
        corridor = (d <= half) & np.isfinite(sub)
        if not corridor.any():
            return 0
        inv_up, inv_dn = self.invert_up_m, self.invert_dn_m
        if inv_up is None or inv_dn is None:
            end = 2.0 * half
            lo = corridor & (s <= end)
            hi = corridor & (s >= total - end)
            if inv_up is None:
                inv_up = float(sub[lo].min()) if lo.any() else float(sub[corridor].min())
            if inv_dn is None:
                inv_dn = float(sub[hi].min()) if hi.any() else float(sub[corridor].min())
        target = inv_up + (inv_dn - inv_up) * np.clip(s / total, 0.0, 1.0)
        cut = corridor & (target < sub)
        sub[cut] = target[cut]
        z[r0:r1, c0:c1] = sub
        return int(cut.sum())


@dataclass
class TailwaterStage:
    """A river or lake already standing at `wse_m` when the breach flood arrives
    (tailwater / backwater). The ground is not changed: every cell inside
    `polygon` lower than the stage starts with water up to it. With `seed`
    (x, y) only the cells connected to the seed count, so a low field behind a
    levee is not filled. The flood then adds to that water; results show the
    water the flood adds above the starting depth."""
    polygon: np.ndarray
    wse_m: float = 0.0
    seed: Optional[tuple] = None
    name: str = "tailwater"

    def apply(self, grid, z, init):
        poly = np.asarray(self.polygon, dtype=float)
        w = _window(grid, poly[:, 0], poly[:, 1], grid.dx)
        if w is None:
            return 0
        r0, r1, c0, c1 = w
        X, Y = _xy_window(grid, w)
        sub = z[r0:r1, c0:c1]
        wet = points_in_polygon(X, Y, poly) & np.isfinite(sub) & (sub < self.wse_m)
        if self.seed is not None and wet.any():
            from scipy.ndimage import label
            lab, _n = label(wet)
            sr, sc = grid.rc(*self.seed)
            sr, sc = sr - r0, sc - c0
            if 0 <= sr < wet.shape[0] and 0 <= sc < wet.shape[1] and lab[sr, sc] > 0:
                wet = lab == lab[sr, sc]
            else:       # the seed is not on wet ground: use the wet cell nearest to it
                rr, cc = np.nonzero(wet)
                k = int(np.argmin((rr - sr) ** 2 + (cc - sc) ** 2))
                wet = lab == lab[rr[k], cc[k]]
        d = np.where(wet, self.wse_m - sub, 0.0)
        init[r0:r1, c0:c1] = np.maximum(init[r0:r1, c0:c1], d)
        return int(wet.sum())


def _footprint_cells(grid, z, polygon):
    """(window, inside mask) of the footprint's cells on the grid, or (None, None)."""
    poly = np.asarray(polygon, dtype=float)
    if len(poly) < 3:
        return None, None
    w = _window(grid, poly[:, 0], poly[:, 1], grid.dx)
    if w is None:
        return None, None
    r0, r1, c0, c1 = w
    X, Y = _xy_window(grid, w)
    sub = z[r0:r1, c0:c1]
    inside = points_in_polygon(X, Y, poly) & np.isfinite(sub)
    if not inside.any():
        # a footprint smaller than a cell: the cell under its centroid
        cx, cy = float(poly[:, 0].mean()), float(poly[:, 1].mean())
        rr, cc = grid.rc(cx, cy)
        if grid.inside(rr, cc) and np.isfinite(z[rr, cc]):
            inside = np.zeros_like(inside)
            if r0 <= rr < r1 and c0 <= cc < c1:
                inside[rr - r0, cc - c0] = True
    return w, inside


@dataclass
class BuildingRaise:
    """v3.4.0: raise the ground inside a building footprint to the highest ground
    inside it plus `raise_m`, a flat-topped block the flood goes around (bare-earth
    LiDAR has removed the building). Footprints smaller than a cell take the cell
    under their centroid."""
    polygon: np.ndarray
    raise_m: float = BUILDING_RAISE_FT_DEFAULT * 0.3048
    name: str = "building"

    def apply(self, grid, z, init):
        w, inside = _footprint_cells(grid, z, self.polygon)
        if w is None or not inside.any():
            return 0
        r0, r1, c0, c1 = w
        sub = z[r0:r1, c0:c1]
        top = float(np.nanmax(sub[inside])) + float(self.raise_m)
        sub[inside] = np.maximum(sub[inside], top)
        z[r0:r1, c0:c1] = sub
        return int(inside.sum())


@dataclass
class BuildingRoughness:
    """v3.4.0: Manning's n set to `n` inside a building footprint (the ground is not
    changed). Applied to the roughness grid the models read; where there is no
    roughness raster one is made from the default n."""
    polygon: np.ndarray
    n: float = BUILDING_N_DEFAULT
    name: str = "building"

    def apply(self, grid, z, init):          # changes no ground
        return 0

    def apply_n(self, grid, z, man):
        w, inside = _footprint_cells(grid, z, self.polygon)
        if w is None or not inside.any():
            return 0
        r0, r1, c0, c1 = w
        sub = man[r0:r1, c0:c1]
        sub[inside] = float(self.n)
        man[r0:r1, c0:c1] = sub
        return int(inside.sum())


def building_edits(footprints, mode, raise_ft=BUILDING_RAISE_FT_DEFAULT, n=BUILDING_N_DEFAULT):
    """The edits for `footprints` (list of outer rings) under `mode` (BUILDINGS_MODES).
    `raise_ft` is in feet (the parameter boundary; metres inside)."""
    if mode not in BUILDINGS_MODES:
        raise ValueError("unknown buildings mode '{}'".format(mode))
    if mode == BUILDINGS_NONE:
        return []
    out = []
    for i, poly in enumerate(footprints):
        name = "building {}".format(i + 1)
        if mode == BUILDINGS_RAISE:
            out.append(BuildingRaise(np.asarray(poly, float), float(raise_ft) * 0.3048, name))
        else:
            out.append(BuildingRoughness(np.asarray(poly, float), float(n), name))
    return out


def describe_buildings(edits, counts=None):
    """One line for the report / manifest about how building footprints were treated."""
    if edits is None or not getattr(edits, "buildings", None):
        return "Buildings: not represented (bare terrain)."
    b = edits.buildings
    raise_ = [e for e in b if isinstance(e, BuildingRaise)]
    rough = [e for e in b if isinstance(e, BuildingRoughness)]
    cells = ""
    if counts:
        n = sum(v for k, v in counts.items() if k.startswith("buildings:"))
        cells = " ({:,} cells on the last grid read)".format(int(n))
    if raise_:
        return ("Buildings: {} footprint(s) raised to the highest ground inside each plus "
                "{:.1f} ft (flat-topped blocks){}. {}".format(
                    len(raise_), raise_[0].raise_m / 0.3048, cells, BUILDINGS_NOTE))
    return ("Buildings: Manning's n = {:g} inside {} footprint(s){}. {}".format(
        rough[0].n, len(rough), cells, BUILDINGS_NOTE))


def line_for_spec(grid, spec):
    """The crest line in the grid's CRS. EPSG:5070 (or a grid with no code, or the
    spec's own CRS) uses line_xy; any other projected CRS is projected from line_lonlat."""
    xy = np.asarray(spec.get("line_xy") or [], dtype=float)
    code = None
    if getattr(grid, "crs_wkt", None):
        from . import gdal_io
        code = gdal_io.epsg_of(grid.crs_wkt)
    want = str(spec.get("crs") or "EPSG:5070")
    if code is None or code == want or not spec.get("line_lonlat"):
        return xy
    from osgeo import osr
    src = osr.SpatialReference()
    src.ImportFromEPSG(4326)
    dst = osr.SpatialReference()
    dst.ImportFromWkt(grid.crs_wkt)
    try:
        src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    except Exception:
        pass
    ct = osr.CoordinateTransformation(src, dst)
    out = []
    for lon, lat in spec["line_lonlat"]:
        x, y, _z = ct.TransformPoint(float(lon), float(lat))
        out.append((x, y))
    return np.asarray(out, dtype=float)


@dataclass
class EmbankmentInsert:
    """Raise a proposed embankment onto the grid being read. `spec` is
    dam.json extra["proposed_embankment"]. Cells already at the design surface
    are left as they are, so a conditioned clip and this edit agree."""
    spec: dict
    name: str = "proposed embankment"

    def apply(self, grid, z, init):
        from . import proposed as prop
        try:
            line = line_for_spec(grid, self.spec)
            if len(line) < 2:
                return 0
            gt = (grid.x0, grid.dx, 0.0, grid.y0, 0.0, -grid.dx)
            emb = prop.insert_embankment(
                z, gt, line, float(self.spec["crest_m"]),
                float(self.spec.get("crest_width_m") or 0.0),
                float(self.spec.get("us_slope_h") or 3.0),
                float(self.spec.get("ds_slope_h") or 3.0),
                self.spec.get("upstream") or "left", measure=False)
        except Exception:
            return 0
        raised = emb["raised"]
        if not np.any(raised):
            return 0
        z[raised] = emb["dem"][raised]
        return int(np.count_nonzero(raised))


@dataclass
class CrestRaise:
    """v3.11.0: lift the ground under the dam line to the top of dam on whatever grid is
    read. A DEM smooths a crest, so the model DEM's crest is lower than the real one and
    breach water spills back over it into the reservoir basin. Only cells lower than
    `top_m` are changed; the width is 1.5 cells so the raised line is closed to the
    4-neighbour flow of LISFLOOD-FP at any cell size. The source DEM is never modified."""
    line: np.ndarray
    top_m: float
    name: str = "dam line raised to top of dam"

    def apply(self, grid, z, init):
        line = np.asarray(self.line, dtype=float).reshape(-1, 2)
        if len(line) < 2 or not np.isfinite(self.top_m):
            return 0
        half = 0.75 * grid.dx
        w = _window(grid, line[:, 0], line[:, 1], half + grid.dx)
        if w is None:
            return 0
        r0, r1, c0, c1 = w
        X, Y = _xy_window(grid, w)
        d, _along = polyline_distance(X, Y, densify(line, 0.5 * grid.dx))
        sub = z[r0:r1, c0:c1]
        low = (d <= half) & np.isfinite(sub) & (sub < self.top_m)
        if not low.any():
            return 0
        sub[low] = self.top_m
        z[r0:r1, c0:c1] = sub
        return int(low.sum())


class Edits:
    """A set of DEM edits applied in order: ponds, channels, embankments (v3.7.0,
    a proposed dam), the raised dam line (v3.11.0), notches (which can cut the new crest),
    buildings (v3.4.0, raised footprints), then the tailwater stages (which change only the
    starting water). Roughness footprints change the Manning grid (apply_manning), not the
    ground."""

    def __init__(self, channels=None, ponds=None, notches=None, stages=None, buildings=None,
                 embankments=None, crests=None):
        self.channels: List[ChannelBurn] = list(channels or [])
        self.ponds: List[PondLower] = list(ponds or [])
        self.notches: List[Notch] = list(notches or [])
        self.stages: List[TailwaterStage] = list(stages or [])
        self.buildings: list = list(buildings or [])
        self.embankments: list = list(embankments or [])
        self.crests: List[CrestRaise] = list(crests or [])

    def copy(self):
        """A new Edits with its own lists (the edit objects themselves are shared)."""
        return Edits(self.channels, self.ponds, self.notches, self.stages, self.buildings,
                     self.embankments, self.crests)

    def empty(self):
        return not (self.channels or self.ponds or self.notches or self.stages
                    or self.buildings or self.embankments or self.crests)

    def has_water(self):
        return bool(self.ponds or self.stages)

    def has_roughness(self):
        return any(isinstance(b, BuildingRoughness) for b in self.buildings)

    def apply(self, grid):
        """Returns (edited grid, initial water depth grid or None, counts)."""
        z = grid.masked()
        init = np.zeros_like(z)
        counts = {}
        for kind, items in (("ponds", self.ponds), ("channels", self.channels),
                            ("embankments", self.embankments), ("crests", self.crests),
                            ("notches", self.notches),
                            ("buildings", self.buildings), ("stages", self.stages)):
            for e in items:
                n = e.apply(grid, z, init)
                counts["{}:{}".format(kind, e.name)] = n
        out = grid.like(np.where(np.isfinite(z), z, grid.nodata))
        wet0 = init > 0
        return out, (grid.like(np.where(wet0, init, 0.0)) if wet0.any() else None), counts

    def apply_manning(self, grid, man, default_n):
        """v3.4.0: the Manning grid with the roughness footprints applied. `man` is the
        roughness Grid the terrain gives (or None: one is made from `default_n`). Returns
        (Grid or None, counts)."""
        if not self.has_roughness():
            return man, {}
        z = grid.masked()
        if man is None:
            arr = np.full(z.shape, float(default_n))
        else:
            arr = np.array(man.data, dtype=float, copy=True)
            arr = np.where(np.isfinite(arr) & (arr != man.nodata), arr, float(default_n))
        counts = {}
        for e in self.buildings:
            if isinstance(e, BuildingRoughness):
                counts["buildings:" + e.name] = e.apply_n(grid, z, arr)
        return grid.like(arr, nodata=-9999.0), counts


class ConditionedTerrain:
    """Terrain source that applies `edits` to whatever the models read. `default_n` is the
    Manning's n used where there is no roughness raster (v3.4.0 roughness footprints)."""

    def __init__(self, base, edits, default_n=0.06):
        self.base = base
        self.edits = edits
        self.default_n = default_n
        self.z_to_m = getattr(base, "z_to_m", 1.0)
        self.z_note = getattr(base, "z_note", "")
        self.src_cell_m = getattr(base, "src_cell_m", None)      # 3.20.3: the DEM's own pixel
        self.last_counts = {}
        self.last_manning_counts = {}

    def read(self, bounds, cell):
        g = self.base.read(bounds, cell)
        if self.edits.empty():
            return g
        out, _, counts = self.edits.apply(g)
        self.last_counts = counts
        return out

    def initial_depth(self, bounds, cell):
        """Initial water depth (m) of the ponds, or None."""
        if not self.edits.has_water():
            return None
        g = self.base.read(bounds, cell)
        _, init, _ = self.edits.apply(g)      # all edits: the water sits on the edited ground
        return init

    def manning(self, bounds, cell):
        f = getattr(self.base, "manning", None)
        man = f(bounds, cell) if f else None
        if self.edits.has_roughness():
            g = self.base.read(bounds, cell)
            man, counts = self.edits.apply_manning(g, man, self.default_n)
            self.last_manning_counts = counts
        return man

    def __getattr__(self, name):          # anything else the source offers
        if name == "base":
            raise AttributeError(name)
        return getattr(self.base, name)
