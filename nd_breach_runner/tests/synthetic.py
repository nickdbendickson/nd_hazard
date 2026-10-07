"""Synthetic terrain and output writers for tests (no GDAL needed)."""
import os

import numpy as np

from nd_breach_runner.core.grid import Grid

TOP_M = 1850.0 * 0.3048
H_M = 25.0 * 0.3048


def valley(x, y):
    """A reservoir to the west held by an embankment at x = 0, and a prairie
    valley running east at 1 m/km with a small incised channel."""
    bed = TOP_M - H_M - 0.001 * np.clip(x, 0, None)
    z = bed + 0.004 * np.abs(y) - np.where(np.abs(y) < 12, 1.0 - np.abs(y) / 12, 0.0)
    z = z + 0.3 * np.sin(x / 700.0) * np.cos(y / 500.0)            # gentle relief
    pool = TOP_M - 1.0
    res = np.where(x < -40, np.maximum(z, pool), z)                # LiDAR water surface
    emb = TOP_M - np.abs(x) / 40.0 * (TOP_M - bed)                  # ~5H:1V embankment
    on_emb = (np.abs(x) <= 40) & (np.abs(y) < 400)
    return np.where(on_emb, np.maximum(res, emb), res)


class SyntheticTerrain:
    z_to_m = 1.0
    z_note = "metres (synthetic)"

    def __init__(self, fn=valley, n=0.06):
        self.fn = fn
        self.n = n
        self.reads = []

    def read(self, bounds, cell):
        xmin, ymin, xmax, ymax = bounds
        nx = int(round((xmax - xmin) / cell))
        ny = int(round((ymax - ymin) / cell))
        xs = xmin + (np.arange(nx) + 0.5) * cell
        ys = ymax - (np.arange(ny) + 0.5) * cell
        X, Y = np.meshgrid(xs, ys)
        self.reads.append((bounds, cell))
        return Grid(self.fn(X, Y), xmin, ymax, cell)

    def manning(self, bounds, cell):
        return None


def npy_writer(grid, path):
    """Stand-in for the GeoTIFF writer: .npy array + .json georeference."""
    import json
    base = os.path.splitext(path)[0]
    np.save(base + ".npy", grid.data)
    with open(base + ".json", "w") as f:
        json.dump({"x0": grid.x0, "y0": grid.y0, "dx": grid.dx}, f)
    open(path, "w").write("placeholder for " + os.path.basename(base) + ".npy\n")
