"""A small north-up raster container used between the GIS side and the
models, with ESRI ASCII grid I/O (the format LISFLOOD-FP reads and writes)."""
import numpy as np

NODATA = -9999.0


class Grid:
    """data[row, col], row 0 at the top. x0/y0 are the left and top edges."""

    def __init__(self, data, x0, y0, dx, nodata=NODATA, crs_wkt=None):
        self.data = np.asarray(data, dtype=np.float64)
        self.x0 = float(x0)
        self.y0 = float(y0)
        self.dx = float(dx)
        self.nodata = float(nodata)
        self.crs_wkt = crs_wkt

    # -- geometry ------------------------------------------------------------
    @property
    def ny(self):
        return self.data.shape[0]

    @property
    def nx(self):
        return self.data.shape[1]

    @property
    def bounds(self):
        """(xmin, ymin, xmax, ymax)"""
        return (self.x0, self.y0 - self.ny * self.dx, self.x0 + self.nx * self.dx, self.y0)

    @property
    def xll(self):
        return self.x0

    @property
    def yll(self):
        return self.y0 - self.ny * self.dx

    def like(self, data, nodata=None):
        return Grid(data, self.x0, self.y0, self.dx,
                    self.nodata if nodata is None else nodata, self.crs_wkt)

    def x_centers(self):
        return self.x0 + (np.arange(self.nx) + 0.5) * self.dx

    def y_centers(self):
        return self.y0 - (np.arange(self.ny) + 0.5) * self.dx

    def rc(self, x, y):
        """Row and column of the cell containing (x, y) (may be outside)."""
        c = int(np.floor((x - self.x0) / self.dx))
        r = int(np.floor((self.y0 - y) / self.dx))
        return r, c

    def xy(self, r, c):
        return self.x0 + (c + 0.5) * self.dx, self.y0 - (r + 0.5) * self.dx

    def inside(self, r, c):
        return 0 <= r < self.ny and 0 <= c < self.nx

    def valid(self):
        return np.isfinite(self.data) & (self.data != self.nodata)

    def masked(self):
        a = self.data.copy()
        a[~self.valid()] = np.nan
        return a

    def sample_bilinear(self, x, y):
        """Bilinear value at points (arrays); NaN outside or on no-data."""
        a = self.masked()
        x = np.asarray(x, dtype=float)
        y = np.asarray(y, dtype=float)
        fc = (x - self.x0) / self.dx - 0.5
        fr = (self.y0 - y) / self.dx - 0.5
        c0 = np.floor(fc).astype(int)
        r0 = np.floor(fr).astype(int)
        tc = fc - c0
        tr = fr - r0
        c0c = np.clip(c0, 0, self.nx - 1)
        c1c = np.clip(c0 + 1, 0, self.nx - 1)
        r0c = np.clip(r0, 0, self.ny - 1)
        r1c = np.clip(r0 + 1, 0, self.ny - 1)
        v = (a[r0c, c0c] * (1 - tc) * (1 - tr) + a[r0c, c1c] * tc * (1 - tr)
             + a[r1c, c0c] * (1 - tc) * tr + a[r1c, c1c] * tc * tr)
        # fall back to nearest where a neighbour is missing
        near = a[np.clip(np.round(fr).astype(int), 0, self.ny - 1),
                 np.clip(np.round(fc).astype(int), 0, self.nx - 1)]
        v = np.where(np.isnan(v), near, v)
        out = (fc < -0.5) | (fr < -0.5) | (fc > self.nx - 0.5) | (fr > self.ny - 0.5)
        return np.where(out, np.nan, v)

    def window(self, r0, r1, c0, c1):
        return Grid(self.data[r0:r1, c0:c1], self.x0 + c0 * self.dx, self.y0 - r0 * self.dx,
                    self.dx, self.nodata, self.crs_wkt)

    # -- ASCII grid I/O ------------------------------------------------------
    def write_asc(self, path, fmt="%.3f"):
        a = np.where(self.valid(), self.data, self.nodata)
        header = ("ncols {}\nnrows {}\nxllcorner {:.6f}\nyllcorner {:.6f}\ncellsize {:.6f}\n"
                  "NODATA_value {:g}".format(self.nx, self.ny, self.xll, self.yll, self.dx,
                                             self.nodata))
        np.savetxt(path, a, fmt=fmt, header=header, comments="")

    @staticmethod
    def read_asc(path, crs_wkt=None):
        hdr = {}
        with open(path) as f:
            for _ in range(6):
                pos = f.tell()
                line = f.readline()
                parts = line.split()
                if len(parts) != 2 or parts[0][0].isdigit() or parts[0][0] == "-":
                    f.seek(pos)
                    break
                hdr[parts[0].lower()] = float(parts[1])
            data = np.loadtxt(f, ndmin=2)
        ny, nx = int(hdr["nrows"]), int(hdr["ncols"])
        data = data.reshape(ny, nx)
        dx = hdr["cellsize"]
        x0 = hdr.get("xllcorner", hdr.get("xllcenter", 0.0) - 0.5 * dx)
        yll = hdr.get("yllcorner", hdr.get("yllcenter", 0.0) - 0.5 * dx)
        return Grid(data, x0, yll + ny * dx, dx, hdr.get("nodata_value", NODATA), crs_wkt)
