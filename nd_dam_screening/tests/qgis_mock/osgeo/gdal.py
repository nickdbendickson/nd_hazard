"""Test double for the small part of GDAL the plugin uses (single-band float rasters,
one CRS).  Rasters are stored as .npz files at the given path."""
import os

import numpy as np

GDT_Float32 = 6
GDT_Byte = 1
_UNSET = object()


def UseExceptions():
    pass


class Band:
    def __init__(self, ds): self.ds = ds
    def ReadAsArray(self): return self.ds.arr.copy()
    def GetNoDataValue(self): return self.ds.nodata
    def SetNoDataValue(self, v): self.ds.nodata = v
    def WriteArray(self, a): self.ds.arr[...] = a


class Dataset:
    def __init__(self, arr, gt, nodata=None, path=None):
        self.arr, self.gt, self.nodata, self.path = np.asarray(arr, "float64"), tuple(gt), nodata, path
        self.RasterYSize, self.RasterXSize = self.arr.shape
    def GetGeoTransform(self): return self.gt
    def SetGeoTransform(self, gt): self.gt = tuple(gt)
    def SetProjection(self, wkt): self._proj = wkt
    def GetProjection(self): return getattr(self, "_proj", "EPSG:5070")
    def GetRasterBand(self, i): return Band(self)
    def ReadAsArray(self): return self.arr.copy()
    def FlushCache(self):
        if self.path:
            with open(self.path, "wb") as f:
                np.savez(f, arr=self.arr, gt=np.array(self.gt),
                         nodata=np.array(np.nan if self.nodata is None else self.nodata))


class _Driver:
    def Create(self, path, nx, ny, nb, typ, opts=None):
        return Dataset(np.zeros((ny, nx)), (0, 1, 0, 0, 0, -1), None, path or None)


def RasterizeLayer(ds, bands, layer, burn_values=(1,), options=None):
    """Burn the layer's polygons (OGR mock geometries) into ds: a cell whose centre lies inside a
    ring (even-odd) gets the burn value. Points and lines mark the cell they fall in."""
    gt = ds.gt
    ny, nx = ds.arr.shape
    jj, ii = np.meshgrid(np.arange(nx), np.arange(ny))
    cx = gt[0] + (jj + 0.5) * gt[1]
    cy = gt[3] + (ii + 0.5) * gt[5]
    for feat in layer.features:
        g = feat.geom
        if g is None:
            continue
        mask = np.zeros((ny, nx), bool)
        if g.kind == "polygon":
            for ring in g.rings:
                inside = np.zeros((ny, nx), bool)
                n = len(ring)
                for i in range(n):
                    (x1, y1), (x2, y2) = ring[i], ring[(i + 1) % n]
                    if y1 == y2:
                        continue
                    cross = (y1 > cy) != (y2 > cy)
                    xi = x1 + (cy - y1) * (x2 - x1) / (y2 - y1)
                    inside ^= cross & (cx < xi)
                mask ^= inside
        else:
            for x, y in g.points:
                c = int((x - gt[0]) // gt[1]); r = int((y - gt[3]) // gt[5])
                if 0 <= r < ny and 0 <= c < nx:
                    mask[r, c] = True
        ds.arr[mask] = burn_values[0]
    return 0


def GetDriverByName(name): return _Driver()


def Open(path):
    if isinstance(path, Dataset):
        return path
    z = np.load(path)
    nd = float(z["nodata"])
    return Dataset(z["arr"], z["gt"], None if np.isnan(nd) else nd, None)


def _sources(src):
    if isinstance(src, (list, tuple)):
        return [Open(s) for s in src]
    if isinstance(src, Dataset) and hasattr(src, "parts"):
        return src.parts
    return [Open(src)]


def _sample(parts, x, y, fill):
    out = np.full(x.shape, fill, float)
    for d in parts:
        gt = d.gt
        c = np.floor((x - gt[0]) / gt[1]).astype(int)
        r = np.floor((y - gt[3]) / gt[5]).astype(int)
        ok = (r >= 0) & (r < d.RasterYSize) & (c >= 0) & (c < d.RasterXSize)
        v = np.full(x.shape, fill, float)
        v[ok] = d.arr[r[ok], c[ok]]
        if d.nodata is not None:
            v[ok & (v == d.nodata)] = fill
        take = ok & (out == fill)
        out[take] = v[take]
    return out


def Warp(dst, src, format=None, outputBounds=None, width=None, height=None, resampleAlg="near",
         dstSRS=None, srcNodata=None, dstNodata=None, xRes=None, yRes=None, outputType=None,
         creationOptions=None):
    parts = _sources(src)
    x0, y0, x1, y1 = outputBounds
    if width is None:
        width, height = int(round((x1 - x0) / xRes)), int(round((y1 - y0) / yRes))
    cx, cy = (x1 - x0) / width, (y1 - y0) / height
    fill = dstNodata if dstNodata is not None else -9999.0
    src_cell = min(abs(p.gt[1]) for p in parts)
    k = max(int(np.ceil(cx / src_cell)), 1) if resampleAlg == "average" else 1
    acc = np.zeros((height, width)); cnt = np.zeros((height, width))
    jj, ii = np.meshgrid(np.arange(width), np.arange(height))
    for a in range(k):
        for b in range(k):
            x = x0 + (jj + (b + 0.5) / k) * cx
            y = y1 - (ii + (a + 0.5) / k) * cy
            v = _sample(parts, x, y, fill)
            good = v != fill
            acc[good] += v[good]; cnt[good] += 1
    arr = np.where(cnt > 0, acc / np.maximum(cnt, 1), fill)
    ds = Dataset(arr, (x0, cx, 0.0, y1, 0.0, -cy), fill if dstNodata is not None else None,
                 dst if dst else None)
    if dst:
        ds.FlushCache()
    return ds


def BuildVRT(path, srcs):
    parts = [Open(s) for s in srcs]
    x0 = min(p.gt[0] for p in parts); y1 = max(p.gt[3] for p in parts)
    x1 = max(p.gt[0] + p.RasterXSize * p.gt[1] for p in parts)
    y0 = min(p.gt[3] + p.RasterYSize * p.gt[5] for p in parts)
    cell = min(p.gt[1] for p in parts)
    ds = Warp(path, parts, outputBounds=(x0, y0, x1, y1), width=int(round((x1 - x0) / cell)),
              height=int(round((y1 - y0) / cell)), dstNodata=-9999.0)
    ds.parts = parts
    return ds
