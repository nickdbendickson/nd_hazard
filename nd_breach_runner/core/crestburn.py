"""Crest-burn terrain prep (ND Runner 3.28.0 / DamSafety Runner 3.15.0): keep road and rail embankments in a coarse model grid.

Why
---
A model cell of 10 m or more is the AVERAGE of the 1 m lidar inside it. A section-line road
embankment is 8-15 m wide at its toe and 1-2 m high, so the averaged cell stands well below the
real crest, and on a flat plain the flood sheets over roads that a finer grid (or the real
ground) keeps it behind. DWR-2801 (Absaraka), 2026-11-05: burning the true 1 m crest into the
10 m cells crossed by a road or rail line took the extent from 1.46x to 1.33x of the DSS-WISE
reference at > 0.5 ft (LISFLOOD 5 m: 1.14x) with the cell size unchanged; aligning a mesh to the
same lines WITHOUT the burn changed nothing. The lever is whether the crest survives in the
terrain, not the mesh or the solver.

What it does
------------
`CrestBurnTerrain` wraps the run's terrain source. For every model read at a cell of
MIN_CELL_M or more, each cell that a road or rail line crosses is raised to the HIGHEST
elevation of a fine (1 m) DEM inside that cell. It never lowers a cell, and leaves alone:

  * cells near a known bridge (an inventory crossing of kind "bridge"; the deck is not in a
    bare-earth DEM, so the cell's maximum is the abutment and would close the opening);
  * cells near the run's own flow path (the traced main channel below the breach, which a road
    crosses on a bridge or a culvert the DEM cannot show; burning it would dam the breach);
  * raises larger than MAX_RAISE_M (an overpass approach or a line drawn on the wrong road
    puts a hill top in the cell, not an embankment);
  * reads made before the wrapper is armed (the inflow search, the flow trace and the planning
    grid read the plain DEM, so the domain the run picks does not depend on the burn).

The fine DEM is, in this order: the one the engineer names (CREST_BURN_DEM / the provider
setting), the run's own DEM when its pixel is already 2 m or finer, the suite data folder's
local DEM (dem/, e.g. the statewide 1 m VRT) when it is that fine, else USGS 3DEP at 1 m through
the suite's terrain tile cache. GDAL's "max" resampling gives the per-cell maximum directly from
the fine raster, so a whole 1 m domain is never held in memory.

Pure numpy + GDAL; no QGIS.
"""
import math
import os
import time

import numpy as np

from . import conditioning as cond

MIN_CELL_M = 4.0           # reads finer than this are not burned (the crest read near the dam)
FINE_MAX_PX_M = 2.0        # a DEM this fine or finer can serve as the 1 m source
MAX_RAISE_M = 5.0          # a larger raise is a hill or a mis-drawn line, not an embankment
PATH_GUARD_CELLS = 1.5     # flow-path cells kept open: this many model cells either side
BRIDGE_GUARD_CELLS = 1.5   # around a bridge point (plus half its span when known)
CHUNK_CELLS = 256          # fine reads are made in blocks of this many model cells square
RETRY_WAIT_S = (10.0, 30.0, 60.0)  # 3DEP 1 m tile retries (USGS gateway errors are common)
FT = 0.3048

SRC_PARAMETER = "parameter"
SRC_RUN_DEM = "run DEM"
SRC_SUITE_DEM = "suite data folder DEM"
SRC_3DEP = "USGS 3DEP 1 m (terrain cache)"


def line_cells(shape, x0, y0, dx, lines):
    """Boolean mask of the cells of a north-up grid (top-left x0 / y0, cell dx) that any of
    `lines` (polylines of (x, y), same CRS) passes through. Lines are walked at dx / 4."""
    ny, nx = shape
    mask = np.zeros((ny, nx), dtype=bool)
    step = dx / 4.0
    xmax, ymin = x0 + nx * dx, y0 - ny * dx
    for ln in lines or []:
        a = np.asarray(ln, dtype=float)
        if a.ndim != 2 or len(a) < 2:
            continue
        if (a[:, 0].max() < x0 or a[:, 0].min() > xmax or a[:, 1].max() < ymin
                or a[:, 1].min() > y0):
            continue
        p = cond.densify(a, step)
        c = np.floor((p[:, 0] - x0) / dx).astype(int)
        r = np.floor((y0 - p[:, 1]) / dx).astype(int)
        ok = (r >= 0) & (r < ny) & (c >= 0) & (c < nx)
        mask[r[ok], c[ok]] = True
    return mask


def guard_cells(shape, x0, y0, dx, paths=(), points=()):
    """Cells to leave alone: within PATH_GUARD_CELLS of any polyline in `paths`, and within
    BRIDGE_GUARD_CELLS (+ the point's own radius) of each (x, y, radius_m) in `points`."""
    ny, nx = shape
    keep = np.zeros((ny, nx), dtype=bool)
    xc = x0 + (np.arange(nx) + 0.5) * dx
    yc = y0 - (np.arange(ny) + 0.5) * dx

    def disk(x, y, rad):
        c0 = max(0, int(math.floor((x - rad - x0) / dx)))
        c1 = min(nx, int(math.ceil((x + rad - x0) / dx)) + 1)
        r0 = max(0, int(math.floor((y0 - y - rad) / dx)))
        r1 = min(ny, int(math.ceil((y0 - y + rad) / dx)) + 1)
        if c0 >= c1 or r0 >= r1:
            return
        X, Y = np.meshgrid(xc[c0:c1], yc[r0:r1])
        keep[r0:r1, c0:c1] |= np.hypot(X - x, Y - y) <= rad

    rad_p = PATH_GUARD_CELLS * dx
    for pth in paths or ():
        a = np.asarray(pth, dtype=float)
        if a.ndim != 2 or not len(a):
            continue
        pts = cond.densify(a, dx / 2.0) if len(a) > 1 else a
        for x, y in pts:
            disk(float(x), float(y), rad_p)
    for x, y, extra in points or ():
        disk(float(x), float(y), BRIDGE_GUARD_CELLS * dx + float(extra or 0.0))
    return keep


def burn(base, fine_max, lines_mask, keep=None, max_raise_m=MAX_RAISE_M):
    """Pure: base (model-cell elevations, m) raised to fine_max (the fine DEM's per-cell
    maximum, m) where lines_mask and not keep, the fine value is finite and above the base,
    and the raise is at most max_raise_m. Returns (new array, stats dict)."""
    base = np.asarray(base, dtype=float)
    fm = np.asarray(fine_max, dtype=float)
    cand = lines_mask & np.isfinite(base) & np.isfinite(fm)
    if keep is not None:
        cand &= ~keep
    rise = np.where(cand, fm - base, 0.0)
    too_big = cand & (rise > max_raise_m)
    up = cand & (rise > 0.0) & ~too_big
    out = base.copy()
    out[up] = fm[up]
    stats = {"line_cells": int(lines_mask.sum()), "kept_open": int((lines_mask & keep).sum())
             if keep is not None else 0,
             "raised": int(up.sum()), "skipped_over_max": int(too_big.sum()),
             "mean_raise_m": float(rise[up].mean()) if up.any() else 0.0,
             "max_raise_m": float(rise[up].max()) if up.any() else 0.0}
    return out, stats


def _no_overviews(gdal, kw):
    """gdalwarp -ovr NONE: read the full-resolution 1 m cells, never the source's overviews (a
    VRT of 3DEP COG tiles exposes their overviews, and GDAL would otherwise read the 8 m level
    for a 10 m target - the "maximum" would then be of pre-averaged ground)."""
    try:
        import inspect
        if "overviewLevel" in inspect.signature(gdal.WarpOptions).parameters:
            kw["overviewLevel"] = "NONE"
            return kw
    except (TypeError, ValueError, AttributeError):
        pass
    kw["options"] = list(kw.get("options") or []) + ["-ovr", "NONE"]
    return kw


def pixel_m(path):
    """A raster's pixel size in metres (None when unknown); degrees at its centre latitude."""
    from .gdal_io import _gdal, GS
    gdal, osr = _gdal()
    ds = GS.open_raster(path)
    gt = ds.GetGeoTransform()
    px = min(abs(gt[1]), abs(gt[5]))
    wkt = ds.GetProjection()
    if not wkt:
        return None
    srs = osr.SpatialReference()
    srs.ImportFromWkt(wkt)
    if srs.IsGeographic():
        lat = gt[3] + gt[5] * ds.RasterYSize / 2.0
        return float(px * 111320.0 * max(math.cos(math.radians(lat)), 0.1))
    return float(px * (srs.GetLinearUnits() or 1.0))


def _run_dem_px(terrain_src):
    px = getattr(terrain_src, "src_cell_m", None)
    if px:
        return float(px)
    fn = getattr(terrain_src, "native_cell_m", None)
    try:
        return float(fn()) if fn else None
    except Exception:                                              # noqa
        return None


class FineSource:
    """Per-model-cell maximum of a fine DEM file (GDAL warp, resampleAlg "max")."""

    def __init__(self, path, work_srs, z_to_m=1.0, label="", nodata=None):
        self.path = path
        self.work_srs = work_srs
        self.z_to_m = float(z_to_m or 1.0)
        self.label = label or path
        self.nodata = nodata

    def max_grid(self, bounds, cell, shape):
        from .gdal_io import _gdal, GS, NODATA
        gdal, _ = _gdal()
        ny, nx = shape
        opts = gdal.WarpOptions(**_no_overviews(gdal, dict(
            format="MEM", dstSRS=self.work_srs, outputBounds=bounds, width=nx, height=ny,
            resampleAlg="max", dstNodata=NODATA, srcNodata=self.nodata,
            outputType=gdal.GDT_Float32, multithread=True)))
        out = GS.checked(gdal.Warp("", self.path, options=opts),
                         "reading the 1 m crest DEM {}".format(self.path))
        a = out.GetRasterBand(1).ReadAsArray().astype(np.float64)
        a[a == NODATA] = np.nan
        a[a < -1000] = np.nan
        return a * self.z_to_m


class ThreeDepSource:
    """USGS 3DEP at 1 m through the suite's terrain tile cache (EPSG:5070 tiles, downloaded once
    into <data folder>/terrain/<service>_1m/ and shared with every other tool)."""

    CELL = 1.0

    def __init__(self, data_folder, work_srs, http_get=None, cancel=None, log=None):
        from ..damsuite import services as SV
        from ..damsuite import suitedata as SD
        self.SV, self.SD = SV, SD
        self.data_folder = data_folder
        self.work_srs = work_srs
        self.http_get = http_get or SV.urllib_get
        self.cancel = cancel
        self.log = log or (lambda m: None)
        self.label = SRC_3DEP
        self.service = None
        for s in getattr(SV, "BUILTIN", []):
            if s.get("id") == "usgs_3dep_image":
                self.service = dict(s)
        if self.service is None:
            self.service = {"id": "usgs_3dep_image", "kind": "arcgis_image",
                            "name": "USGS 3DEP elevation (ImageServer)",
                            "url": "https://elevation.nationalmap.gov/arcgis/rest/services/"
                                   "3DEPElevation/ImageServer"}
        self.fetched = 0
        self.cached = 0
        self.sleep = time.sleep

    def _box_5070(self, bounds):
        from .gdal_io import _gdal
        _, osr = _gdal()
        a, b = osr.SpatialReference(), osr.SpatialReference()
        a.SetFromUserInput(self.work_srs)
        b.ImportFromEPSG(5070)
        for s in (a, b):
            try:
                s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            except Exception:                                      # noqa - GDAL 2
                pass
        tr = osr.CoordinateTransformation(a, b)
        x0, y0, x1, y1 = bounds
        xs, ys = [], []
        for t in np.linspace(0.0, 1.0, 5):
            for (x, y) in ((x0 + t * (x1 - x0), y0), (x0 + t * (x1 - x0), y1),
                           (x0, y0 + t * (y1 - y0)), (x1, y0 + t * (y1 - y0))):
                p = tr.TransformPoint(float(x), float(y))
                xs.append(p[0])
                ys.append(p[1])
        pad = 4.0
        return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)

    def max_grid(self, bounds, cell, shape):
        from ..damsuite import terrain_cache as TC
        root = os.path.join(self.data_folder, self.SD.TERRAIN)
        cdir = TC.cache_dir(root, self.service["id"], self.CELL)
        os.makedirs(cdir, exist_ok=True)
        vrt = os.path.join(cdir, "crestburn_{}.vrt".format(abs(hash((bounds, cell))) % 10 ** 10))
        box = self._box_5070(bounds)
        for attempt in range(len(RETRY_WAIT_S) + 1):
            try:
                res = TC.get(box, self.CELL, cdir, TC.fetcher_for(self.service, self.http_get),
                             vrt, cancel=self.cancel, log=self.log)
                break
            except TC.TerrainError as e:
                # USGS often answers 502 / 504 for a while; tiles that did arrive stay in the
                # cache, so a retry fetches only the missing ones.
                if attempt >= len(RETRY_WAIT_S) or (self.cancel and self.cancel()):
                    raise
                wait = RETRY_WAIT_S[attempt]
                self.log("Crest burn: 1 m tiles failed ({}); trying again in {:g} s".format(
                    str(e)[:120], wait))
                self.sleep(wait)
        self.fetched += int(res.get("fetched") or 0)
        self.cached += int(res.get("cached") or 0)
        try:
            return FineSource(res["vrt"], self.work_srs, 1.0, self.label,
                              nodata=TC.NODATA).max_grid(bounds, cell, shape)
        finally:
            try:
                os.remove(res["vrt"])
            except OSError:
                pass


def pick_source(explicit, terrain_src, work_srs, data_folder="", http_get=None, cancel=None,
                log=None, allow_download=True):
    """(source or None, note): where the 1 m crest elevations come from (see the module text)."""
    from . import gdal_io

    def file_source(path, how):
        try:
            z, _note = gdal_io.z_to_m_factor(path, gdal_io.Z_AUTO)
        except ValueError:            # no unit recorded (DamSafety refuses to guess for a run)
            guess = getattr(gdal_io, "z_factor_or_guess", None)
            z = guess(path)[0] if guess else 1.0
            how = "{}, unit not recorded: x{:g} to metres".format(how, z)
        px = pixel_m(path)
        ds = gdal_io.GS.open_raster(path)
        nd = ds.GetRasterBand(1).GetNoDataValue()
        ds = None
        # a wrong unit guess makes every raise implausible (x3.28) and MAX_RAISE_M drops them
        return FineSource(path, work_srs, z, "{} ({})".format(how, path), nodata=nd), px

    if explicit:
        if not os.path.exists(explicit):
            return None, "the crest-burn DEM {} does not exist".format(explicit)
        src, px = file_source(explicit, SRC_PARAMETER)
        if px and px > FINE_MAX_PX_M:
            return None, ("the crest-burn DEM {} has {:.1f} m pixels; it needs {:g} m or finer"
                          .format(explicit, px, FINE_MAX_PX_M))
        return src, src.label
    run_dem = getattr(terrain_src, "dem_path", None)
    px = _run_dem_px(terrain_src)
    if run_dem and px and px <= FINE_MAX_PX_M:
        src = FineSource(run_dem, work_srs, getattr(terrain_src, "z_to_m", 1.0),
                         "{} ({})".format(SRC_RUN_DEM, run_dem),
                         nodata=getattr(terrain_src, "src_nodata", None))
        return src, src.label
    if data_folder:
        try:
            from ..damsuite import suitedata as SD
            local = SD.local_dem(data_folder)
        except Exception:                                          # noqa
            local = ""
        if local:
            try:
                src, lpx = file_source(local, SRC_SUITE_DEM)
                if lpx and lpx <= FINE_MAX_PX_M:
                    return src, src.label
            except Exception:                                      # noqa - fall through
                pass
        if allow_download:
            src = ThreeDepSource(data_folder, work_srs, http_get, cancel, log)
            return src, src.label
    return None, ("no 1 m terrain: give a crest-burn DEM, use a 1 m DEM for the run, or set "
                  "the suite data folder so USGS 3DEP 1 m can be downloaded")


class CrestBurnTerrain:
    """terrain_src wrapper; see the module text. `arm(paths, bridges)` switches it on once the
    run has traced its flow path; until then reads pass straight through."""

    def __init__(self, base, lines, fine, log=None):
        self.base = base
        self.lines = [np.asarray(ln, dtype=float) for ln in (lines or []) if len(ln) >= 2]
        self.fine = fine
        self.log = log or (lambda m: None)
        self.armed = False
        self.paths = []
        self.bridges = []
        self._chunks = {}           # (cell, ic, ir) -> fine max array (NaN where not needed)
        self.stats = {}             # cell -> stats of the last read at that cell
        self.errors = []
        self.z_to_m = getattr(base, "z_to_m", 1.0)
        self.z_note = getattr(base, "z_note", "")
        self.src_cell_m = getattr(base, "src_cell_m", None)

    def arm(self, paths=(), bridges=()):
        self.paths = [np.asarray(p, dtype=float) for p in paths or () if p is not None and len(p)]
        self.bridges = list(bridges or ())
        self.armed = True

    def _fine_chunked(self, g, mask):
        """Fine per-cell max on g's grid, read in globally aligned chunks (so domain growth
        reuses them) where the grid is aligned to whole cells, else in one read."""
        cell = g.dx
        out = np.full(g.data.shape, np.nan)
        aligned = (abs(g.x0 / cell - round(g.x0 / cell)) < 1e-6 and
                   abs(g.y0 / cell - round(g.y0 / cell)) < 1e-6)
        if not aligned:
            if mask.any():
                out[:] = self.fine.max_grid(g.bounds, cell, g.data.shape)
            return out
        C = CHUNK_CELLS
        gc0 = int(round(g.x0 / cell))                 # global column of the grid's first col
        gr0 = int(round(-g.y0 / cell))                # global row (rows grow southward)
        ny, nx = g.data.shape
        for ic in range(gc0 // C, (gc0 + nx - 1) // C + 1):
            for ir in range(gr0 // C, (gr0 + ny - 1) // C + 1):
                c0, c1 = max(0, ic * C - gc0), min(nx, (ic + 1) * C - gc0)
                r0, r1 = max(0, ir * C - gr0), min(ny, (ir + 1) * C - gr0)
                if c0 >= c1 or r0 >= r1 or not mask[r0:r1, c0:c1].any():
                    continue
                key = (round(cell, 6), ic, ir)
                if key not in self._chunks:
                    bx = (ic * C * cell, -(ir + 1) * C * cell, (ic + 1) * C * cell,
                          -ir * C * cell)
                    self._chunks[key] = self.fine.max_grid(bx, cell, (C, C))
                a = self._chunks[key]
                out[r0:r1, c0:c1] = a[r0 + gr0 - ir * C:r1 + gr0 - ir * C,
                                      c0 + gc0 - ic * C:c1 + gc0 - ic * C]
        return out

    def read(self, bounds, cell):
        g = self.base.read(bounds, cell)
        if not self.armed or cell < MIN_CELL_M or not self.lines:
            return g
        mask = line_cells(g.data.shape, g.x0, g.y0, g.dx, self.lines)
        if not mask.any():
            return g
        keep = guard_cells(g.data.shape, g.x0, g.y0, g.dx, self.paths, self.bridges)
        try:
            fm = self._fine_chunked(g, mask & ~keep)
        except Exception as e:                                     # noqa - never stop the run
            msg = "{}: {}".format(type(e).__name__, e)
            if msg not in self.errors:
                self.errors.append(msg)
                self.log("Crest burn skipped for this read: " + msg)
            return g
        g.data, st = burn(g.data, fm, mask, keep)
        st["fine_missing"] = int((mask & ~keep & ~np.isfinite(fm)).sum())
        self.stats[round(cell, 3)] = st
        return g

    def __getattr__(self, name):          # anything else the source offers
        if name == "base":
            raise AttributeError(name)
        return getattr(self.base, name)


def bridge_points(crossings):
    """(x, y, half span m) of the inventory crossings that are bridges (gaps.py dicts)."""
    out = []
    for c in crossings or []:
        try:
            if str(c.get("kind", "")).lower() != "bridge":
                continue
            span = float(c.get("span_ft") or 0.0) * FT
            out.append((float(c["x"]), float(c["y"]), span / 2.0))
        except (TypeError, ValueError, KeyError):
            continue
    return out
