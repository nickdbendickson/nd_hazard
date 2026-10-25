# -*- coding: utf-8 -*-
"""v2.5: HEC-RAS 6.x plan results (<project>.p##.hdf) to the plugin's
rasters, without the RAS Mapper export step.

Read from the plan HDF, for every 2D flow area:
  /Geometry/2D Flow Areas/<area>/Cells Center Coordinate, Cells Minimum
      Elevation, Cells Face and Orientation Info / Values
  /Results/Unsteady/Output/Output Blocks/Base Output/Summary Output/
      2D Flow Areas/<area>/Maximum Water Surface   (row 0 value, row 1 time
      in days), Maximum Face Velocity
  .../Unsteady Time Series/2D Flow Areas/<area>/Water Surface and
      .../Unsteady Time Series/Time (days)         (arrival time)
  root attributes "Projection" (WKT) and "Units System"

Written (GeoTIFF, Float32, NoData -9999; ft, ft/s, hours):
  WSE_ft.tif          maximum water surface of the cell containing the pixel
  Depth_ft.tif        with a terrain raster: max WSE - terrain on the
                      terrain grid (like RAS Mapper's "horizontal" WSE
                      rendering); without: max WSE - cell minimum elevation
                      (deepest point of each cell - conservative)
  Velocity_ftps.tif   largest |max face velocity| over the cell's faces
                      (face-normal speeds; RAS Mapper interpolates a full
                      velocity field - its rasters can differ at cell edges)
  Arrival_hr.tif      v2.34.4: per pixel, the first time the pixel depth (the
                      water-surface time series of its cell minus the pixel's
                      terrain - the DEM, else the cell minimum elevation)
                      reaches the arrival depth, interpolated between output
                      steps; where the saved steps never reach it but the
                      maximum depth does, the time of the maximum. Only where
                      depth >= the arrival depth. Hours after the breach start
                      (0 = simulation start); needs time-series output.
                      (Before 2.34.4: per cell, the first output step at which
                      WSE - cell minimum elevation exceeded the arrival depth,
                      kept wherever depth > 0.)
  TimeOfPeak_hr.tif   time of the maximum water surface, minus breach start
Pixels are assigned to the nearest cell centre (RAS cells are Voronoi-type
polygons, so this is the cell that contains the pixel) within a search
radius; pixels beyond it are NoData.

The HDF is read with h5py when QGIS has it, else with GDAL's
multidimensional HDF5 driver (GDAL >= 3.1).

v2.9 - a plan HDF used DIRECTLY as a classification input (no Import step):
  is_plan_hdf(path)          cheap check (name *.p##.hdf + HDF5 signature;
                             deep=True: the 2D summary results group)
  plan_settings(settings)    the import options as a dict (defaults as the
                             HEC-RAS plan tab): dem, breach_hr, arrival_ft,
                             cell_ft
  plan_key(path, settings)   cache key: plan size / mtime / first + last MB,
                             the options, the terrain file, the import code
  plan_to_rasters_cached()   import_plan() into <cache_root>/<plan>_<key12>/,
                             re-used while the key matches (manifest.json;
                             written in a temporary folder and renamed, so a
                             crash never leaves a folder that looks complete)
  plan_cache_lookup()        the cached rasters without converting (or None)
The conversion never touches the project or widgets: it can run in a worker
thread (the one project call inside, prepare_output_path, is a no-op off the
main thread and finds nothing to release in a fresh folder anyway).
"""

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time

import numpy as np

GEOM = "/Geometry/2D Flow Areas"
SUMMARY = "/Results/Unsteady/Output/Output Blocks/Base Output/Summary Output/2D Flow Areas"
TSERIES = "/Results/Unsteady/Output/Output Blocks/Base Output/Unsteady Time Series"
FT_PER_M = 3.28083989501312
NODATA = -9999.0
OUT_NAMES = {"wse": "WSE_ft.tif", "depth": "Depth_ft.tif", "vel": "Velocity_ftps.tif",
             "arr": "Arrival_hr.tif", "peak": "TimeOfPeak_hr.tif"}


class RasImportError(Exception):
    pass


def _txt(v):
    if v is None:
        return None
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).decode("utf-8", "replace").strip("\x00 ")
    if isinstance(v, np.ndarray):
        if v.size == 1:
            return _txt(v.ravel()[0])
        return v
    if isinstance(v, (list, tuple)) and len(v) == 1:
        return _txt(v[0])
    return v


# ----------------------------------------------------------------- readers --
class _H5(object):
    name = "h5py"

    def __init__(self, path):
        import h5py
        self._h5 = h5py
        self.f = h5py.File(path, "r")

    def groups(self, path):
        g = self.f.get(path)
        if g is None:
            return []
        return [k for k, v in g.items() if isinstance(v, self._h5.Group)]

    def has(self, path):
        return path in self.f

    def shape(self, path):
        return tuple(self.f[path].shape)

    def read(self, path, cols=None):
        ds = self.f[path]
        if cols is None:
            return np.asarray(ds[()])
        return np.asarray(ds[:, cols[0]:cols[1]])

    def attr(self, path, name):
        obj = self.f[path] if path not in ("", "/") else self.f
        return _txt(obj.attrs.get(name))

    def field(self, path, name):
        ds = self.f[path]
        try:
            return np.asarray(ds[name])
        except Exception:
            return None

    def close(self):
        try:
            self.f.close()
        except Exception:
            pass


class _GdalMD(object):
    name = "GDAL multidimensional"

    def __init__(self, path):
        from osgeo import gdal
        if not hasattr(gdal, "OF_MULTIDIM_RASTER"):
            raise RasImportError("this GDAL has no multidimensional API (GDAL >= 3.1)")
        self.ds = gdal.OpenEx(path, gdal.OF_MULTIDIM_RASTER)
        if self.ds is None:
            raise RasImportError("GDAL cannot open {} as HDF5".format(path))
        self.root = self.ds.GetRootGroup()

    def _group(self, path):
        g = self.root
        for part in [p for p in path.strip("/").split("/") if p]:
            try:
                g = g.OpenGroup(part)
            except Exception:
                g = None
            if g is None:
                return None
        return g

    def _array(self, path):
        parent, name = path.rsplit("/", 1)
        g = self._group(parent)
        if g is None:
            return None
        try:
            return g.OpenMDArray(name)
        except Exception:
            return None

    def groups(self, path):
        g = self._group(path)
        return list(g.GetGroupNames() or []) if g is not None else []

    def has(self, path):
        return self._array(path) is not None or self._group(path) is not None

    def shape(self, path):
        a = self._array(path)
        return tuple(int(d.GetSize()) for d in a.GetDimensions())

    def read(self, path, cols=None):
        a = self._array(path)
        if a is None:
            raise KeyError(path)
        if cols is None:
            return np.asarray(a.ReadAsArray())
        dims = [int(d.GetSize()) for d in a.GetDimensions()]
        return np.asarray(a.ReadAsArray(array_start_idx=[0, int(cols[0])],
                                        count=[dims[0], int(cols[1] - cols[0])]))

    def attr(self, path, name):
        obj = self._group(path) if path in ("", "/") or self._array(path) is None \
            else self._array(path)
        if obj is None:
            return None
        try:
            at = obj.GetAttribute(name)
            return _txt(at.Read()) if at is not None else None
        except Exception:
            return None

    def field(self, path, name):
        return None                       # compound datasets: not needed with GDAL

    def close(self):
        self.root = None
        self.ds = None


def open_hdf(path):
    errors = []
    for cls in (_H5, _GdalMD):
        try:
            return cls(path)
        except Exception as e:
            errors.append("{}: {}".format(cls.name, e))
    raise RasImportError("the plan HDF cannot be read here ({})".format("; ".join(errors)))


# ------------------------------------------------------------------ plan info --
def plan_info(h):
    """dict: areas, units ('US Customary' / 'SI Units' / ''), is_si,
    projection WKT, time steps, plan title."""
    areas = [a for a in h.groups(GEOM)]
    units = h.attr("/", "Units System") or ""
    proj = h.attr("/", "Projection") or ""
    t = TSERIES + "/Time"
    steps = h.shape(t)[0] if h.has(t) else 0
    title = h.attr("/Plan Data/Plan Information", "Plan Title") if h.has(
        "/Plan Data/Plan Information") else None
    with_results = [a for a in areas if h.has("{}/{}/Maximum Water Surface".format(SUMMARY, a))]
    return {"areas": areas, "with_results": with_results, "units": units or "",
            "is_si": "si" in (units or "").lower(), "projection": proj or "",
            "time_steps": int(steps), "title": title or "", "reader": h.name}


def _real_cells(h, area, n_all):
    """Number of real cells of an area (the arrays end with perimeter ghost
    cells): the 'Cell Count' of /Geometry/2D Flow Areas/Attributes when it
    can be read, else the cells with a finite minimum elevation."""
    names = h.field(GEOM + "/Attributes", "Name")
    counts = h.field(GEOM + "/Attributes", "Cell Count")
    if names is not None and counts is not None:
        for nm, c in zip(names, counts):
            if _txt(nm) == area:
                return min(int(c), n_all), None
    elev = h.read("{}/{}/Cells Minimum Elevation".format(GEOM, area))
    ok = np.isfinite(elev)
    if ok.all():
        return n_all, None
    # ghost cells are at the end; keep the leading finite block
    first_bad = int(np.argmin(ok)) if not ok[0] else 0
    return (first_bad if first_bad > 0 else int(ok.sum())), ok


def cell_velocity(face_vel, info, values, n):
    """Per-cell max |face velocity| (vectorised). info: (N, 2) start, count;
    values: (M, 2) face index, orientation."""
    face_vel = np.abs(np.asarray(face_vel, dtype=np.float64))
    starts = np.asarray(info[:n, 0], dtype=np.int64)
    counts = np.asarray(info[:n, 1], dtype=np.int64)
    faces = np.asarray(values[:, 0], dtype=np.int64)
    out = np.zeros(n, dtype=np.float64)
    ok = counts > 0
    if not ok.any():
        return out
    idx = np.repeat(np.arange(n)[ok], counts[ok])
    pos = np.concatenate([np.arange(s, s + c) for s, c in zip(starts[ok], counts[ok])]) \
        if ok.sum() < 2000 else _ranges(starts[ok], counts[ok])
    fv = face_vel[np.clip(faces[pos], 0, face_vel.size - 1)]
    np.maximum.at(out, idx, fv)
    return out


def _ranges(starts, counts):
    """np.concatenate([arange(s, s + c) ...]) without a Python loop."""
    total = int(counts.sum())
    ends = np.cumsum(counts)
    out = np.ones(total, dtype=np.int64)
    out[0] = starts[0]
    out[ends[:-1]] = starts[1:] - (starts[:-1] + counts[:-1] - 1)
    return np.cumsum(out)


DEPTH_BASIS_TERRAIN = "terrain"           # max WSE - the terrain raster on its grid
DEPTH_BASIS_CELL_MIN = "cell_min"         # max WSE - the cell's minimum elevation
ARRIVAL_BASIS = "pixel_series_interpolated"   # v2.34.4 (see Arrival_hr.tif above)
_GATHER_VALUES = 8_000_000                # pixels x time steps gathered at once


def first_reach_hr(series, z, thr, times_hr):
    """v2.34.4: per pixel, the first time (h, as times_hr) at which
    series - z >= thr, linearly interpolated between the output steps (the
    first step itself when it is already reached there); NaN where never.
    series: (P, T) water surface of each pixel's cell; z: (P,) the pixel's
    terrain; same units as thr."""
    series = np.asarray(series, dtype=np.float64)
    out = np.full(series.shape[0], np.nan)
    if series.size == 0:
        return out
    d = series - np.asarray(z, dtype=np.float64)[:, None]
    with np.errstate(invalid="ignore"):
        hit = d >= thr
    anyh = hit.any(axis=1)
    j = np.argmax(hit, axis=1)
    t = np.asarray(times_hr, dtype=np.float64)
    first = anyh & (j == 0)
    out[first] = t[0]
    mid = np.flatnonzero(anyh & (j > 0))
    if mid.size:
        jj = j[mid]
        d0, d1 = d[mid, jj - 1], d[mid, jj]
        with np.errstate(invalid="ignore", divide="ignore"):
            f = np.where(d1 > d0, (thr - d0) / (d1 - d0), 1.0)
        f = np.clip(np.where(np.isfinite(f), f, 1.0), 0.0, 1.0)
        out[mid] = t[jj - 1] + f * (t[jj] - t[jj - 1])
    return out


def pixel_arrival_hr(cidx, z, depth, peak_hr, series, times_hr, arrival_ft, breach_hr=0.0):
    """v2.34.4: arrival (h after the breach start) of a block of pixels.
    cidx: each pixel's row in `series` (the (n, T) water surface of the wet
    cells, ft; < 0 or NaN = none); z: pixel terrain (ft); depth: pixel max
    depth (ft); peak_hr: time of the maximum (h after the breach start).
    Defined exactly where depth >= arrival_ft and the cell has a series: the
    interpolated first reach of arrival_ft, else (the saved steps miss the
    peak) the time of the maximum. Returns (arrival, n_time_of_max)."""
    cidx = np.asarray(cidx, dtype=np.float64)
    out = np.full(cidx.shape, np.nan)
    with np.errstate(invalid="ignore"):
        want = (np.isfinite(depth) & (depth >= arrival_ft) & np.isfinite(cidx) & (cidx >= 0)
                & np.isfinite(z))
    pos = np.flatnonzero(want.ravel())
    if pos.size == 0 or series is None:
        return out, 0
    rows = cidx.ravel()[pos].astype(np.int64)
    zz = np.asarray(z, dtype=np.float64).ravel()[pos]
    nt = max(1, int(series.shape[1]))
    step = max(1, _GATHER_VALUES // nt)
    flat = out.ravel()
    pk = np.asarray(peak_hr, dtype=np.float64).ravel()[pos]
    n_tom = 0
    for i0 in range(0, pos.size, step):
        i1 = min(pos.size, i0 + step)
        r = rows[i0:i1]
        order = np.argsort(r, kind="stable")        # sorted reads from a memmap
        got = np.empty(i1 - i0)
        got[order] = first_reach_hr(np.asarray(series[r[order]]), zz[i0:i1][order],
                                    arrival_ft, times_hr)
        miss = ~np.isfinite(got)
        n_tom += int(miss.sum())
        got = np.where(miss, pk[i0:i1], np.maximum(got - breach_hr, 0.0))
        flat[pos[i0:i1]] = got
    return flat.reshape(cidx.shape), n_tom


def read_area(h, area, is_si, breach_hr=0.0, arrival_ft=0.1, progress=None, ts_path=None):
    """Cell arrays of one 2D flow area in ft / ft/s / h:
    {"x", "y", "zmin", "wse", "vel", "peak", "cidx", "series", "times_hr"}
    (NaN = no value). v2.34.4: "series" is the water-surface time series
    (ft) of the wet cells, one row per wet cell ("cidx" = its row; NaN for
    dry cells), written to a float32 memmap at ts_path when given; None
    without time-series output. Arrival is made per pixel from it."""
    g = "{}/{}".format(GEOM, area)
    s = "{}/{}".format(SUMMARY, area)
    xy = h.read(g + "/Cells Center Coordinate")
    n_all = xy.shape[0]
    n, ok_mask = _real_cells(h, area, n_all)
    zmin = h.read(g + "/Cells Minimum Elevation").astype(np.float64)[:n]
    mws = h.read(s + "/Maximum Water Surface").astype(np.float64)
    if mws.ndim != 2 or mws.shape[1] < n:
        raise RasImportError("{}: Maximum Water Surface has shape {} for {} cells".format(
            area, mws.shape, n))
    wse, t_max = mws[0, :n], mws[1, :n]
    k = FT_PER_M if is_si else 1.0
    out = {"x": xy[:n, 0].astype(np.float64), "y": xy[:n, 1].astype(np.float64)}
    wet = np.isfinite(wse) & np.isfinite(zmin) & (wse - zmin > 1e-4)
    out["zmin"] = zmin * k
    out["wse"] = np.where(wet, wse * k, np.nan)
    out["peak"] = np.where(wet, np.maximum(t_max * 24.0 - breach_hr, 0.0), np.nan)
    # velocity
    vel = np.full(n, np.nan)
    fvp = s + "/Maximum Face Velocity"
    if h.has(fvp) and h.has(g + "/Cells Face and Orientation Info"):
        fv = h.read(fvp).astype(np.float64)
        fv = fv[0] if fv.ndim == 2 else fv
        info = h.read(g + "/Cells Face and Orientation Info")
        vals = h.read(g + "/Cells Face and Orientation Values")
        vel = cell_velocity(fv, info, vals, n) * k
        vel = np.where(wet, vel, np.nan)
    out["vel"] = vel
    # v2.34.4: the time series of the wet cells, for the per-pixel arrival
    out["cidx"] = np.full(n, np.nan)
    out["series"], out["times_hr"] = None, None
    tsp = "{}/2D Flow Areas/{}/Water Surface".format(TSERIES, area)
    if h.has(tsp) and h.has(TSERIES + "/Time"):
        times = h.read(TSERIES + "/Time").astype(np.float64).ravel()
        shp = h.shape(tsp)
        wet_idx = np.flatnonzero(wet)
        nw, nt = int(wet_idx.size), int(min(shp[0], times.size))
        if ts_path:
            ser = np.lib.format.open_memmap(ts_path, mode="w+", dtype=np.float32,
                                            shape=(nw, nt))
        else:
            ser = np.empty((nw, nt), dtype=np.float32)
        row = np.full(n, -1, dtype=np.int64)
        row[wet_idx] = np.arange(nw)
        step = max(1, int(4.0e7 // max(1, nt)))
        for i0 in range(0, n, step):
            i1 = min(n, i0 + step)
            sel = np.flatnonzero(wet[i0:i1])
            if sel.size:
                ws = h.read(tsp, cols=(i0, i1)).astype(np.float64)
                ser[row[i0 + sel]] = (ws[:nt, sel] * k).T
            if progress:
                progress(100.0 * i1 / n, "{}: water-surface series {} of {} cells".format(
                    area, i1, n))
        if ts_path:
            ser.flush()
        out["cidx"][wet_idx] = np.arange(nw, dtype=np.float64)
        out["series"], out["times_hr"] = ser, times[:nt] * 24.0
    out["n"] = n
    return out


def _spacing(xs, ys, h=None, area=None, n=None):
    """Typical cell size (model units): sqrt of the median cell area when
    stored, else from the extent and the cell count."""
    if h is not None and area is not None:
        p = "{}/{}/Cells Surface Area".format(GEOM, area)
        if h.has(p):
            a = h.read(p).astype(np.float64)[:n]
            a = a[np.isfinite(a) & (a > 0)]
            if a.size:
                return float(np.sqrt(np.median(a))), float(np.sqrt(np.percentile(a, 95)))
    w = float(xs.max() - xs.min()) or 1.0
    ht = float(ys.max() - ys.min()) or 1.0
    s = float(np.sqrt(w * ht / max(1, xs.size)))
    return s, 2.0 * s


# ------------------------------------------------------------------ rasters --
def _write_csv(path, cells):
    """Cell centres and values as CSV (fast for millions of cells); GDAL
    reads it as points through the CSV driver's X / Y open options."""
    fields = ("wse", "zmin", "vel", "cidx", "peak")
    cols = [cells["x"], cells["y"]] + [np.where(np.isfinite(cells[f]), cells[f], NODATA)
                                       for f in fields]
    np.savetxt(path, np.column_stack(cols), delimiter=",", fmt="%.6f", comments="",
               header="x,y," + ",".join(fields))


def _open_points(tmp_base, cells, srs_wkt):
    """(OGR dataset of the cell points for gdal.Grid, layer name, files to
    remove): a CSV read as points; a GeoPackage if this GDAL cannot."""
    from osgeo import gdal
    csv_path = tmp_base + ".csv"
    _write_csv(csv_path, cells)
    try:
        ds = gdal.OpenEx(csv_path, gdal.OF_VECTOR, open_options=[
            "X_POSSIBLE_NAMES=x", "Y_POSSIBLE_NAMES=y", "AUTODETECT_TYPE=YES",
            "KEEP_GEOM_COLUMNS=NO"])
        if ds is not None and ds.GetLayerCount() == 1:
            lyr = ds.GetLayer(0)
            if lyr.GetGeomType() != 0 and lyr.GetFeatureCount() == cells["x"].size:
                return ds, lyr.GetName(), [csv_path]
        ds = None
    except Exception:
        ds = None
    gpkg = tmp_base + ".gpkg"
    _write_points(gpkg, cells, srs_wkt)
    return gdal.OpenEx(gpkg, gdal.OF_VECTOR), "cells", [csv_path, gpkg]


def _write_points(path, cells, srs_wkt):
    from osgeo import ogr, osr
    drv = ogr.GetDriverByName("GPKG")
    if os.path.exists(path):
        drv.DeleteDataSource(path)
    ds = drv.CreateDataSource(path)
    srs = None
    if srs_wkt:
        srs = osr.SpatialReference()
        srs.ImportFromWkt(srs_wkt)
    lyr = ds.CreateLayer("cells", srs, ogr.wkbPoint)
    fields = ("wse", "zmin", "vel", "cidx", "peak")
    for f in fields:
        lyr.CreateField(ogr.FieldDefn(f, ogr.OFTReal))
    defn = lyr.GetLayerDefn()
    lyr.StartTransaction()
    xs, ys = cells["x"], cells["y"]
    cols = {f: np.where(np.isfinite(cells[f]), cells[f], NODATA) for f in fields}
    for i in range(xs.size):
        ft = ogr.Feature(defn)
        for f in fields:
            ft.SetField(f, float(cols[f][i]))
        pt = ogr.Geometry(ogr.wkbPoint)
        pt.AddPoint_2D(float(xs[i]), float(ys[i]))
        ft.SetGeometry(pt)
        lyr.CreateFeature(ft)
    lyr.CommitTransaction()
    ds = None


def _grid(src, dst, field, bounds, width, height, radius, srs_wkt, layer="cells",
          float64=False):
    from osgeo import gdal
    opts = gdal.GridOptions(
        format="GTiff", outputType=gdal.GDT_Float64 if float64 else gdal.GDT_Float32, width=int(width), height=int(height),
        outputBounds=list(bounds), outputSRS=srs_wkt or None, noData=NODATA,
        algorithm="nearest:radius1={r}:radius2={r}:angle=0.0:nodata={nd}".format(
            r=radius, nd=NODATA),
        layers=[layer], zfield=field, creationOptions=["TILED=YES", "COMPRESS=DEFLATE"])
    ds = gdal.Grid(dst, src, options=opts)
    if ds is None:
        raise RasImportError("gdal.Grid failed for {}".format(field))
    ds = None


class _SeriesStack(object):
    """The wet-cell water-surface series of several 2D flow areas as one
    (n, T) array for row gathers (rows numbered across the areas)."""

    def __init__(self, parts, times_hr):
        self.parts = [p for p in parts if p is not None]
        self.times_hr = np.asarray(times_hr, dtype=np.float64)
        nt = min([self.times_hr.size] + [int(p.shape[1]) for p in self.parts])
        self.times_hr = self.times_hr[:nt]
        self.offsets = np.cumsum([0] + [int(p.shape[0]) for p in self.parts])
        self.shape = (int(self.offsets[-1]), nt)

    def __getitem__(self, rows):
        rows = np.asarray(rows, dtype=np.int64)
        out = np.full((rows.size, self.shape[1]), np.nan)
        for p, a, b in zip(self.parts, self.offsets[:-1], self.offsets[1:]):
            m = (rows >= a) & (rows < b)
            if m.any():
                out[m] = p[rows[m] - a, :self.shape[1]]
        return out


def import_plan(hdf_path, out_folder, dem_path=None, cell_ft=0.0, breach_hr=0.0,
                arrival_ft=0.1, progress=None):
    """Convert a plan's 2D results. Returns {"paths": {key: tif}, "lines",
    "notes", "info", "pixel_ft", "basis"}; "basis" (v2.34.4) records how
    depth and arrival were made: {"depth", "arrival", "arrival_ft",
    "arrival_time_of_max_px"}."""
    from osgeo import gdal
    notes, lines = [], []
    os.makedirs(out_folder, exist_ok=True)
    series_parts, series_files, times_hr, offset = [], [], None, 0
    h = open_hdf(hdf_path)
    try:
        info = plan_info(h)
        if not info["with_results"]:
            raise RasImportError("no 2D flow area results in {} (areas: {}) - run the plan "
                                 "with 2D output first".format(
                                     os.path.basename(hdf_path),
                                     ", ".join(info["areas"]) or "none"))
        is_si = info["is_si"]
        k = FT_PER_M if is_si else 1.0
        cells = {f: [] for f in ("x", "y", "zmin", "wse", "vel", "cidx", "peak")}
        spacing = []
        for ai, area in enumerate(info["with_results"]):
            def sub(p, m="", ai=ai):
                if progress:
                    progress(40.0 * (ai + p / 100.0) / len(info["with_results"]), m)
            ts_path = os.path.join(out_folder, "_series_{}.npy".format(ai))
            series_files.append(ts_path)
            c = read_area(h, area, is_si, breach_hr, arrival_ft, sub, ts_path=ts_path)
            if c["series"] is not None:
                c["cidx"] = c["cidx"] + offset            # rows across all the areas
                offset += int(c["series"].shape[0])
                series_parts.append(c["series"])
                if times_hr is None:
                    times_hr = c["times_hr"]
            for f in cells:
                cells[f].append(c[f])
            spacing.append(_spacing(c["x"], c["y"], h, area, c["n"]))
            lines.append("{}: {:,} cells, {:,} wet, max depth {:.1f} ft".format(
                area, c["n"], int(np.isfinite(c["wse"]).sum()),
                float(np.nanmax(c["wse"] - c["zmin"])) if np.isfinite(c["wse"]).any()
                else 0.0))
            if c["series"] is None:
                notes.append("{}: no time-series output - no arrival raster (turn on "
                             "'2D Flow Area' time-series output in the plan)".format(area))
            if not np.isfinite(c["vel"]).any():
                notes.append("{}: no face velocity output - no velocity raster".format(area))
        c = None
        cells = {f: np.concatenate(v) for f, v in cells.items()}
    except BaseException:
        c = None
        _drop_series(series_parts, series_files)
        raise
    finally:
        h.close()
    wkt = info["projection"]
    dem = gdal.Open(dem_path) if dem_path else None
    if dem is not None and not wkt:
        wkt = dem.GetProjection()
        notes.append("the plan HDF has no projection - the terrain raster's is used")
    if dem is not None and info["projection"] and dem.GetProjection():
        try:
            from osgeo import osr
            a, b = osr.SpatialReference(), osr.SpatialReference()
            a.ImportFromWkt(info["projection"])
            b.ImportFromWkt(dem.GetProjection())
            if not a.IsSame(b):
                notes.append("the terrain raster's coordinate system differs from the plan's "
                             "- nothing is reprojected; check that they match")
        except Exception:
            pass
    if not wkt:
        _drop_series(series_parts, series_files)
        raise RasImportError("the plan HDF has no projection (set it in RAS Mapper) - give "
                             "the terrain raster so its coordinate system can be used")
    med, p95 = (float(np.median([s[0] for s in spacing])), max(s[1] for s in spacing))
    radius = 1.5 * p95                          # model units (as the coordinates)
    xs, ys = cells["x"], cells["y"]
    xmin, xmax = float(xs.min()) - radius, float(xs.max()) + radius
    ymin, ymax = float(ys.min()) - radius, float(ys.max()) + radius
    if dem is not None:
        gt = dem.GetGeoTransform()
        if gt[2] != 0 or gt[4] != 0:
            raise RasImportError("rotated terrain rasters are not supported")
        c0 = max(0, int(np.floor((xmin - gt[0]) / gt[1])))
        c1 = min(dem.RasterXSize, int(np.ceil((xmax - gt[0]) / gt[1])))
        r0 = max(0, int(np.floor((ymax - gt[3]) / gt[5])))
        r1 = min(dem.RasterYSize, int(np.ceil((ymin - gt[3]) / gt[5])))
        if c1 <= c0 or r1 <= r0:
            raise RasImportError("the terrain raster does not cover the 2D flow areas (same "
                                 "coordinate system?)")
        width, height = c1 - c0, r1 - r0
        bounds = (gt[0] + c0 * gt[1], gt[3] + r0 * gt[5],
                  gt[0] + c1 * gt[1], gt[3] + r1 * gt[5])
        pix = abs(gt[1])
    else:
        pix = (cell_ft / k) if cell_ft and cell_ft > 0 else max(med / 2.0, 1e-6)
        width = int(np.ceil((xmax - xmin) / pix))
        height = int(np.ceil((ymax - ymin) / pix))
        bounds = (xmin, ymin + height * pix, xmin + width * pix, ymin)
        c0 = r0 = 0
        notes.append("no terrain raster: depth = max water surface - cell minimum elevation "
                     "(deepest point of each cell), {:.1f} ft pixels".format(pix * k))
    if width * height > 400e6:
        _drop_series(series_parts, series_files)
        raise RasImportError("output grid of {:,} x {:,} pixels is too large - give a "
                             "coarser cell size".format(width, height))
    if progress:
        progress(45.0, "Writing {:,} cell centres...".format(xs.size))
    src, lname, tmp_files = _open_points(os.path.join(out_folder, "_cells"), cells, wkt)
    if src is None:
        raise RasImportError("the cell centres could not be written for gridding")
    grids = {}
    old = gdal.GetConfigOption("GDAL_NUM_THREADS")
    gdal.SetConfigOption("GDAL_NUM_THREADS", "ALL_CPUS")
    try:
        fields = ["wse", "vel", "cidx", "peak"] + ([] if dem is not None else ["zmin"])
        for i, f in enumerate(fields):
            if f in ("vel", "cidx", "peak") and not np.isfinite(cells[f]).any():
                continue
            if progress:
                progress(50.0 + 30.0 * i / len(fields), "Gridding {}...".format(f))
            dst = os.path.join(out_folder, "_grid_{}.tif".format(f))
            # the cell row is gridded exactly as the water surface (same points,
            # nearest, same radius), so each pixel gets the series of its cell
            _grid(src, dst, f, bounds, width, height, radius, wkt, lname,
                  float64=(f == "cidx"))
            grids[f] = dst
    finally:
        src = None
        gdal.SetConfigOption("GDAL_NUM_THREADS", old)
    series = _SeriesStack(series_parts, times_hr) if series_parts else None
    stats = {}
    try:
        paths = _finish(out_folder, grids, dem, (c0, r0, width, height), k, progress,
                        series=series, arrival_ft=arrival_ft, breach_hr=breach_hr,
                        stats=stats)
    finally:
        series = None
        _drop_series(series_parts, series_files)
    for p in tmp_files + list(grids.values()):
        try:
            os.remove(p)
        except OSError:
            pass
    has_dem, dem = dem is not None, None
    basis = {"depth": DEPTH_BASIS_TERRAIN if has_dem else DEPTH_BASIS_CELL_MIN,
             "arrival": ARRIVAL_BASIS if "arr" in paths else None,
             "arrival_ft": float(arrival_ft),
             "arrival_time_of_max_px": int(stats.get("time_of_max", 0))}
    if "arr" in paths:
        notes.append("arrival: per pixel, first reach of {:g} ft depth over the {} "
                     "(interpolated between output steps; time of the maximum where the "
                     "steps never reach it: {:,} pixels)".format(
                         float(arrival_ft),
                         "terrain raster" if has_dem else "cell minimum elevation",
                         basis["arrival_time_of_max_px"]))
    return {"paths": paths, "lines": lines, "notes": notes, "info": info,
            "pixel_ft": pix * k, "basis": basis}


def _drop_series(parts, files):
    """Release (the last references: Windows keeps a mapped file locked) and
    remove the temporary series memmaps."""
    import gc
    del parts[:]
    gc.collect()
    for f in files:
        try:
            if os.path.exists(f):
                os.remove(f)
        except OSError:
            pass


def _finish(folder, grids, dem, win, k, progress=None, rows=512, series=None,
            arrival_ft=0.1, breach_hr=0.0, stats=None):
    """Final rasters from the gridded cell values, strip by strip: depth,
    and velocity / peak only where depth > 0. v2.34.4: arrival per pixel
    from its cell's water-surface series minus the pixel's terrain (see
    pixel_arrival_hr), only where depth >= arrival_ft."""
    from osgeo import gdal
    c0, r0, w, h = win
    ref = gdal.Open(grids["wse"])
    gt, prj = ref.GetGeoTransform(), ref.GetProjection()
    drv = gdal.GetDriverByName("GTiff")
    outs = {}
    from .helpers import prepare_output_path
    with_arr = series is not None and "cidx" in grids and "peak" in grids
    for key in ("wse", "depth", "vel", "arr", "peak"):
        if key in ("vel", "peak") and key not in grids:
            continue
        if key == "arr" and not with_arr:
            continue
        # a copy loaded in QGIS is released first; a locked one (open in
        # another program) gets a time-stamped name instead of failing
        p = prepare_output_path(os.path.join(folder, OUT_NAMES[key]))
        ds = drv.Create(p, w, h, 1, gdal.GDT_Float32,
                        ["TILED=YES", "COMPRESS=DEFLATE", "BIGTIFF=IF_SAFER"])
        if ds is None:
            raise RasImportError("{} could not be created".format(p))
        ds.SetGeoTransform(gt)
        ds.SetProjection(prj)
        ds.GetRasterBand(1).SetNoDataValue(NODATA)
        outs[key] = ds
    ds = None
    src = {kk: gdal.Open(v) for kk, v in grids.items()}
    dband = dem.GetRasterBand(1) if dem is not None else None
    dnd = dband.GetNoDataValue() if dband is not None else None
    for y0 in range(0, h, rows):
        hh = min(rows, h - y0)
        wse = src["wse"].GetRasterBand(1).ReadAsArray(0, y0, w, hh).astype(np.float64)
        wse[wse == NODATA] = np.nan
        if dband is not None:
            z = dband.ReadAsArray(c0, r0 + y0, w, hh).astype(np.float64)
            if dnd is not None:
                z[z == dnd] = np.nan
            z = z * k                                  # terrain in ft (as wse)
        else:
            z = src["zmin"].GetRasterBand(1).ReadAsArray(0, y0, w, hh).astype(np.float64)
            z[z == NODATA] = np.nan
        with np.errstate(invalid="ignore"):
            depth = wse - z
            wet = np.isfinite(depth) & (depth > 0)
        outs["wse"].GetRasterBand(1).WriteArray(np.where(np.isfinite(wse), wse, NODATA)
                                                .astype(np.float32), 0, y0)
        outs["depth"].GetRasterBand(1).WriteArray(np.where(wet, depth, NODATA)
                                                  .astype(np.float32), 0, y0)
        peak = None
        for key in ("vel", "peak"):
            if key not in outs:
                continue
            v = src[key].GetRasterBand(1).ReadAsArray(0, y0, w, hh).astype(np.float64)
            ok = wet & (v != NODATA) & np.isfinite(v)
            v = np.where(ok, v, np.nan)
            if key == "peak":
                peak = v
            outs[key].GetRasterBand(1).WriteArray(
                np.where(ok, v, NODATA).astype(np.float32), 0, y0)
        if "arr" in outs:
            ci = src["cidx"].GetRasterBand(1).ReadAsArray(0, y0, w, hh).astype(np.float64)
            ci[ci == NODATA] = np.nan
            arr, n_tom = pixel_arrival_hr(ci, z, np.where(wet, depth, np.nan), peak, series,
                                          series.times_hr, arrival_ft, breach_hr)
            if stats is not None:
                stats["time_of_max"] = stats.get("time_of_max", 0) + n_tom
            outs["arr"].GetRasterBand(1).WriteArray(
                np.where(np.isfinite(arr), arr, NODATA).astype(np.float32), 0, y0)
        if progress:
            progress(80.0 + 20.0 * (y0 + hh) / h, "Writing rasters...")
    paths = {}
    for key in list(outs):
        outs[key].FlushCache()
        paths[key] = outs[key].GetDescription()
        outs[key] = None                     # closes the file (Windows keeps it locked)
    src.clear()
    ref = None
    return paths


def slug(path):
    base = os.path.basename(path)
    base = re.sub(r"\.hdf$", "", base, flags=re.I)
    return re.sub(r"[^A-Za-z0-9]+", "_", base).strip("_")[:40] or "plan"


# ------------------------------------------ v2.9: plans read directly (cache) --
PLAN_RE = re.compile(r"\.p\d{2}\.hdf$", re.I)      # <project>.p01.hdf (not .p01.tmp.hdf)
HDF5_SIGNATURE = b"\x89HDF\r\n\x1a\n"
# Bump when import_plan() writes different rasters for the same input. The
# key also holds a digest of the conversion functions' source (_CODE_DIGEST),
# so a changed import invalidates old caches even if this is forgotten.
IMPORT_CODE_VERSION = "2.6.0"      # 2.34.4: per-pixel interpolated arrival
CACHE_FORMAT = 2                   # 2.34.4: manifests carry "basis"; format-1 folders are stale
CACHE_DIR_NAME = "HECRAS_plan_cache"
MANIFEST = "manifest.json"
CACHE_VERSIONS_KEPT = 3            # cache folders kept per plan (other settings / runs)
_TMP_PREFIX, _OLD_PREFIX = ".tmp_", ".old_"
_TMP_MAX_AGE_S = 12 * 3600         # a temporary folder this old is a crashed conversion
_HASH_BYTES = 1 << 20              # first and last MB of the plan / terrain file
RESULT_KEYS = ("depth", "vel", "arr", "dv", "wse", "peak")
# the import options (same defaults as the HEC-RAS plan tab and the
# 'hecrasplan' Processing algorithm):
#   dem         terrain raster (path or None: depth from each cell's lowest point)
#   breach_hr   breach start, hours after simulation start (arrival / peak are
#               hours after it)
#   arrival_ft  depth that counts as 'arrived' (ft)
#   cell_ft     pixel size without a terrain raster (ft, 0 = half the typical
#               cell size); ignored with a terrain raster (its grid is used)
PLAN_DEFAULTS = {"dem": None, "breach_hr": 0.0, "arrival_ft": 0.1, "cell_ft": 0.0}


def is_hdf5(path):
    """True when the file starts with the HDF5 signature (at 0 or after a
    user block of 512 * 2^n bytes). Reads a few bytes only."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            off = 0
            while off + 8 <= size and off <= 1 << 16:
                fh.seek(off)
                if fh.read(8) == HDF5_SIGNATURE:
                    return True
                off = 512 if off == 0 else off * 2
    except (OSError, TypeError, ValueError):
        return False
    return False


def is_plan_hdf(path, deep=False):
    """A HEC-RAS plan results file? Cheap: the name <project>.p##.hdf and the
    HDF5 signature. deep=True: the HDF5 signature and the 2D summary results
    group (the file is opened, no array is read; the name does not matter)."""
    try:
        if not path or not os.path.isfile(path):
            return False
    except (TypeError, ValueError):
        return False
    if not deep:
        return bool(PLAN_RE.search(os.path.basename(path))) and is_hdf5(path)
    if not is_hdf5(path):
        return False
    try:
        h = open_hdf(path)
    except Exception:
        return False
    try:
        return bool(h.has(SUMMARY))
    except Exception:
        return False
    finally:
        h.close()


def default_cache_root(output_folder=None):
    """<outputs>/HECRAS_plan_cache (kept by release_outputs, skipped by the
    raster search); without an outputs folder a folder in the temp dir."""
    if output_folder:
        return os.path.join(output_folder, CACHE_DIR_NAME)
    return os.path.join(tempfile.gettempdir(), "ND_Dam_Hazard_Helper", CACHE_DIR_NAME)


def plan_settings(settings=None):
    """The import options as a complete dict (PLAN_DEFAULTS for what is not
    given; "dem_path" is accepted for "dem"; unknown keys are dropped)."""
    s = dict(PLAN_DEFAULTS)
    for k, v in (settings or {}).items():
        k = "dem" if k == "dem_path" else k
        if k in s and v is not None:
            s[k] = v
    dem = str(s["dem"] or "").strip()
    s["dem"] = os.path.abspath(dem) if dem else None
    try:
        for k in ("breach_hr", "arrival_ft", "cell_ft"):
            s[k] = float(s[k])
    except (TypeError, ValueError):
        raise RasImportError("HEC-RAS plan import option {} = {!r} is not a number".format(
            k, s[k]))
    return s


def _content_sig(path):
    """[size, mtime (ns), md5 of the first and last MB]."""
    st = os.stat(path)
    h = hashlib.md5()
    with open(path, "rb") as fh:
        h.update(fh.read(_HASH_BYTES))
        if st.st_size > 2 * _HASH_BYTES:
            fh.seek(-_HASH_BYTES, os.SEEK_END)
            h.update(fh.read(_HASH_BYTES))
        elif st.st_size > _HASH_BYTES:
            h.update(fh.read())
    return [int(st.st_size), int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))),
            h.hexdigest()]


def _vrt_sources_sig(path):
    """Size / mtime of the files a VRT terrain mosaic points at."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(8 << 20)
    except OSError:
        return []
    out = []
    base = os.path.dirname(os.path.abspath(path))
    for rel, name in re.findall(r'<SourceFilename[^>]*relativeToVRT="(\d)"[^>]*>([^<]+)<',
                                text)[:5000]:
        p = os.path.join(base, name) if rel == "1" else name
        try:
            st = os.stat(p)
            out.append([name, int(st.st_size), int(getattr(st, "st_mtime_ns", 0))])
        except OSError:
            out.append([name, "missing"])
    return out


def plan_key(path, settings=None):
    """Stable cache key (sha256 hex) of a plan conversion: the plan file's
    size, time stamp and first / last MB, the import options (the terrain
    raster by its content signature, not its path; the pixel size only
    without terrain) and the import code version. The plan's path is not
    part of it: a copied or moved plan keeps its cache."""
    s = plan_settings(settings)
    try:
        plan_sig = _content_sig(path)
    except OSError as e:
        raise RasImportError("HEC-RAS plan results file cannot be read: {} ({})".format(path, e))
    dem = None
    if s["dem"]:
        try:
            dem = _content_sig(s["dem"])
        except OSError:
            dem = ["missing", os.path.normcase(s["dem"])]
        if s["dem"].lower().endswith(".vrt"):
            dem.append(_vrt_sources_sig(s["dem"]))
    parts = {"format": CACHE_FORMAT, "code": [IMPORT_CODE_VERSION, _CODE_DIGEST],
             "plan": plan_sig, "dem": dem, "breach_hr": repr(s["breach_hr"]),
             "arrival_ft": repr(s["arrival_ft"]),
             "cell_ft": repr(0.0 if s["dem"] else s["cell_ft"])}
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode("utf-8")).hexdigest()


def _folder_name(hdf_path, key):
    return "{}_{}".format(slug(hdf_path), key[:12])


def _read_manifest(folder):
    try:
        with open(os.path.join(folder, MANIFEST), "r", encoding="utf-8") as fh:
            m = json.load(fh)
        return m if isinstance(m, dict) else None
    except (OSError, ValueError):
        return None


def _result(folder, manifest, cached):
    files = manifest.get("files") or {}
    out = {k: (os.path.join(folder, files[k]) if files.get(k) else None) for k in RESULT_KEYS}
    out.update({"cached": bool(cached), "folder": folder, "key": manifest.get("key"),
                "lines": list(manifest.get("lines") or []),
                "notes": list(manifest.get("notes") or []),
                "pixel_ft": manifest.get("pixel_ft"), "plan": manifest.get("plan"),
                "basis": manifest.get("basis")})
    return out


def plan_cache_lookup(hdf_path, cache_root, settings=None, key=None):
    """The cached conversion of this plan with these options, as
    plan_to_rasters_cached() returns it (cached=True), or None when there is
    none (or it is incomplete). Converts nothing; cheap enough for the main
    thread (reads 2 MB of the plan to make the key)."""
    if not cache_root or not hdf_path or not os.path.isfile(hdf_path):
        return None
    try:
        key = key or plan_key(hdf_path, settings)
    except RasImportError:
        return None
    folder = os.path.join(cache_root, _folder_name(hdf_path, key))
    m = _read_manifest(folder)
    if m is None or m.get("key") != key or not m.get("complete") or \
            m.get("format") != CACHE_FORMAT:
        return None
    files = m.get("files") or {}
    if not files.get("depth"):
        return None
    for name in files.values():
        if name and not os.path.isfile(os.path.join(folder, name)):
            return None
    return _result(folder, m, True)


_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def _key_lock(key):
    """One conversion of a key at a time in this QGIS (dialog run and a
    Processing batch could ask for the same plan)."""
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def _discard(folder):
    """Remove a cache folder: the manifest first, so what is left of a
    folder that cannot be removed completely (a raster open in QGIS on
    Windows) is never taken for a complete one. True when it is gone."""
    try:
        os.remove(os.path.join(folder, MANIFEST))
    except OSError:
        pass
    shutil.rmtree(folder, ignore_errors=True)
    if not os.path.exists(folder):
        return True
    aside = os.path.join(os.path.dirname(folder), "{}{}_{}".format(
        _OLD_PREFIX, os.path.basename(folder), int(time.time() * 1000)))
    try:
        os.rename(folder, aside)
        shutil.rmtree(aside, ignore_errors=True)
        return True
    except OSError:
        return False


def _clean_leftovers(cache_root):
    """Temporary folders of conversions that crashed (older than
    _TMP_MAX_AGE_S) and folders put aside for deletion. Best effort."""
    try:
        names = os.listdir(cache_root)
    except OSError:
        return
    now = time.time()
    for n in names:
        p = os.path.join(cache_root, n)
        try:
            if n.startswith(_OLD_PREFIX) or (n.startswith(_TMP_PREFIX) and
                                             now - os.path.getmtime(p) > _TMP_MAX_AGE_S):
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass


def _prune(cache_root, hdf_path, keep_folder, keep=CACHE_VERSIONS_KEPT):
    """Older conversions of the same plan (other options, an earlier plan
    run) beyond the newest `keep`. Best effort."""
    me = os.path.normcase(os.path.abspath(hdf_path))
    pat = re.compile(r"^{}_[0-9a-f]{{12}}$".format(re.escape(slug(hdf_path))))
    found = []
    try:
        names = os.listdir(cache_root)
    except OSError:
        return 0
    for n in names:
        p = os.path.join(cache_root, n)
        if not pat.match(n) or os.path.normcase(p) == os.path.normcase(keep_folder):
            continue
        m = _read_manifest(p)
        if m is None or os.path.normcase(os.path.abspath(m.get("plan") or "")) != me:
            continue
        found.append((float(m.get("created_ts") or 0.0), p))
    found.sort(reverse=True)
    n = 0
    for _t, p in found[max(0, keep - 1):]:
        n += 1 if _discard(p) else 0
    return n


def _publish(tmp, final):
    """Rename the finished temporary folder to its cache name. False when
    another conversion put a folder there first. Retries: on Windows a
    virus scanner can hold a new file for a moment."""
    err = None
    for i in range(6):
        try:
            os.rename(tmp, final)
            return True
        except OSError as e:
            err = e
            if os.path.isdir(final):
                return False
            time.sleep(0.2 * (i + 1))
    raise RasImportError("the converted rasters could not be moved into the cache folder "
                         "{} ({})".format(final, err))


def plan_to_rasters_cached(hdf_path, cache_root, settings=None, progress=None, log=None):
    """A plan's rasters for the classification, converted with import_plan()
    into <cache_root>/<plan name>_<key[:12]>/ and re-used while plan_key()
    is unchanged. settings: see PLAN_DEFAULTS. progress(pct, msg) and
    log(msg) as the pipeline's; CancelledByUser raised by progress stops the
    conversion (nothing is left behind). Safe in a worker thread.
    Returns {"depth", "vel", "arr", "dv", "wse", "peak": path or None (dv:
    always None, the plan import has no D x V), "cached": re-used?, "folder",
    "key", "lines", "notes", "pixel_ft", "plan", "basis"} (basis: see
    import_plan; None for a cache from before 2.34.4 - those are not re-used)."""
    if not hdf_path or not os.path.isfile(hdf_path):
        raise RasImportError("HEC-RAS plan results file not found: {}".format(hdf_path))
    s = plan_settings(settings)
    if s["dem"] and not os.path.isfile(s["dem"]):
        raise RasImportError("Terrain raster for the HEC-RAS plan not found: {}".format(s["dem"]))
    cache_root = cache_root or default_cache_root(None)
    key = plan_key(hdf_path, s)
    final = os.path.join(cache_root, _folder_name(hdf_path, key))
    plan_name = os.path.basename(hdf_path)

    def say(msg):
        if log:
            log(msg)
    with _key_lock(key):
        hit = plan_cache_lookup(hdf_path, cache_root, s, key=key)
        if hit is not None:
            say(">>> HEC-RAS plan {}: rasters re-used from the cache (plan and import options "
                "unchanged) - {}".format(plan_name, final))
            if progress:
                progress(100.0, "HEC-RAS plan {}: cached rasters".format(plan_name))
            return hit
        os.makedirs(cache_root, exist_ok=True)
        _clean_leftovers(cache_root)
        if os.path.exists(final) and not _discard(final):
            raise RasImportError("the incomplete cache folder {} cannot be replaced - remove its "
                                 "rasters from the QGIS project or delete the folder".format(final))
        say(">>> HEC-RAS plan {}: converting its 2D results (breach start {:g} h, arrival "
            "depth {:g} ft, {}) ...".format(
                plan_name, s["breach_hr"], s["arrival_ft"],
                "terrain " + os.path.basename(s["dem"]) if s["dem"] else "no terrain raster"))
        tmp = tempfile.mkdtemp(prefix=_TMP_PREFIX + os.path.basename(final) + "_", dir=cache_root)
        t0 = time.time()
        try:
            res = import_plan(hdf_path, tmp, dem_path=s["dem"], cell_ft=s["cell_ft"],
                              breach_hr=s["breach_hr"], arrival_ft=s["arrival_ft"],
                              progress=progress)
            files = {k: os.path.basename(p) for k, p in (res.get("paths") or {}).items()
                     if p and k in RESULT_KEYS}
            if not files.get("depth"):
                raise RasImportError("the HEC-RAS plan import wrote no depth raster")
            manifest = {
                "format": CACHE_FORMAT, "key": key, "complete": True,
                "plan": os.path.abspath(hdf_path), "settings": s,
                "code": [IMPORT_CODE_VERSION, _CODE_DIGEST], "files": files,
                "lines": [str(x) for x in res.get("lines") or []],
                "notes": [str(x) for x in res.get("notes") or []],
                "pixel_ft": float(res.get("pixel_ft") or 0.0),
                "basis": res.get("basis"),
                "created": time.strftime("%Y-%m-%d %H:%M:%S"), "created_ts": time.time(),
                "seconds": round(time.time() - t0, 1)}
            path = os.path.join(tmp, MANIFEST)
            with open(path, "w", encoding="utf-8") as fh:        # written last
                json.dump(manifest, fh, indent=1)
            won = _publish(tmp, final)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        if not won:                          # another conversion finished first
            shutil.rmtree(tmp, ignore_errors=True)
            hit = plan_cache_lookup(hdf_path, cache_root, s, key=key)
            if hit is None:
                raise RasImportError("the cache folder {} is in use and incomplete".format(final))
            return hit
    _prune(cache_root, hdf_path, final)
    say(">>> HEC-RAS plan {}: converted in {:.0f} s ({:.1f} ft pixels) - {}".format(
        plan_name, manifest["seconds"], manifest["pixel_ft"], final))
    for n in manifest["notes"]:
        say("    NOTE: " + n)
    return _result(final, manifest, False)


# the conversion code whose source is part of the cache key
_CODE_FUNCS = ("plan_info", "_real_cells", "cell_velocity", "_ranges", "first_reach_hr",
               "pixel_arrival_hr", "_SeriesStack", "_drop_series", "read_area", "_spacing", "_write_csv", "_open_points", "_write_points", "_grid",
               "import_plan", "_finish")


def _code_digest():
    import inspect
    h = hashlib.sha256()
    for name in _CODE_FUNCS:
        try:
            h.update(inspect.getsource(globals()[name]).encode("utf-8"))
        except Exception:                    # no source (compiled install): the name
            h.update(name.encode("utf-8"))
    return h.hexdigest()[:16]


_CODE_DIGEST = _code_digest()
