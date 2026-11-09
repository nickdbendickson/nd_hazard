"""Optional ANUGA triangle envelopes; no ANUGA import and no model reruns.

The worker stores native per-triangle maxima. QGIS/GDAL exports cell-centre samples,
not interpolated/downscaled coarse grids. Raster work is bounded to small tiles.
"""
import json
import math
import os
import shutil

import numpy as np

ARCHIVE = "anuga_fine_maxima.npz"
DEPTH = "anuga_max_depth_fine.tif"
VELOCITY = "anuga_max_velocity_fine.tif"
DISK_FLOOR = 18 * 1024 ** 3       # GiB; deliberately stricter than decimal GB


def estimate_bytes(nvertices, ntriangles, nx, ny, support_bytes=0):
    """Uncompressed float64 mesh/envelopes + two float32 TIFFs + 25% and 16 MiB.

    Support files already occupy disk; counting them again reserves headroom while
    opting out of their normal cleanup. No credit is taken for compressibility.
    """
    raw = nvertices * 2 * 8 + ntriangles * (3 * 8 + 3 * 8)
    return int(math.ceil(1.25 * (raw + 2 * nx * ny * 4 + support_bytes))) + 16 * 1024 ** 2


def disk_allows(directory, estimate):
    try:
        free = shutil.disk_usage(directory).free
    except OSError:
        return False                 # cannot establish safety: fail closed
    return free - estimate >= DISK_FLOOR


def support_size(workdir, ckdir):
    total = 0
    for path in (os.path.join(workdir, "anuga_elevation_fine.npy"), ckdir):
        if path and os.path.isdir(path):
            for root, _, names in os.walk(path):
                total += sum(os.path.getsize(os.path.join(root, n)) for n in names)
        elif path and os.path.isfile(path):
            total += os.path.getsize(path)
    return total


def retain(workdir, job, domain, env, x_ref, y_ref, ckdir=None):
    """Called only after successful evolve. Returns a JSON-serializable decision."""
    if not bool(job.get("keep_fine_maxima", False)):
        return {"enabled": False, "support_kept": False}
    info = {"enabled": True, "saved": False, "support_kept": False}
    tmp = None
    bed_tmp = None
    try:
        if "elev_fine_file" in job:
            path = os.path.join(workdir, str(job["elev_fine_file"]))
            bed = np.load(path, mmap_mode="r", allow_pickle=False)
            ny, nx = bed.shape
            del bed
            x0, y0, dx = (float(job["elev_fine_" + k]) for k in ("x0", "y0", "dx"))
        else:
            ny, nx = job["dem"].shape
            x0, y0, dx = (float(job["dem_" + k]) for k in ("x0", "y0", "dx"))
        keep_support = bool(job.get("keep_fine_support", False))
        reserve = support_size(workdir, ckdir) if keep_support else 0
        if keep_support and "elev_fine_file" not in job:
            reserve += job["dem"].nbytes
        estimate = estimate_bytes(len(domain.nodes), len(domain.triangles), nx, ny, reserve)
        info.update(estimated_bytes=estimate, disk_floor_bytes=DISK_FLOOR)
        if not disk_allows(workdir, estimate):
            info["warning"] = "ANUGA fine maxima skipped: estimated outputs would leave less than 18 GiB free (or free space is unknown)."
            return info
        meta = {"crs_wkt": str(job.get("fine_maxima_crs", "")),
                "x0": x0, "y0": y0, "cell_m": dx, "nx": nx, "ny": ny,
                "depth_unit": "m", "velocity_unit": "m/s", "wse_unit": "m",
                "sampling": "piecewise-constant triangle centroid envelope at cell centre",
                "complete": True}
        # ANUGA uses DEM-local coordinates; archive vertices use the model CRS.
        nodes = np.asarray(domain.nodes, dtype=np.float64) + [x_ref, y_ref]
        path = os.path.join(workdir, ARCHIVE)
        tmp = path + ".tmp"
        with open(tmp, "wb") as stream:
            np.savez(stream, vertices=nodes, triangles=np.asarray(domain.triangles, np.int64),
                     max_depth_m=env.hmax, max_velocity_mps=env.vmax,
                     max_wse_m=np.where(np.isfinite(env.wmax), env.wmax, np.nan),
                     metadata=json.dumps(meta))
        os.replace(tmp, path)
        if keep_support and "elev_fine_file" not in job:
            # The nd_hazard fork ships its fine terrain in the job, not a separate NPY.
            bed_path = os.path.join(workdir, "anuga_elevation_fine.npy")
            bed_tmp = bed_path + ".tmp"
            with open(bed_tmp, "wb") as stream:
                np.save(stream, job["dem"], allow_pickle=False)
            os.replace(bed_tmp, bed_path)
        info.update(saved=True, archive=ARCHIVE, support_kept=keep_support,
                    raster_cell_m=dx)
    except Exception as exc:         # optional outputs must not discard successful coarse results
        for partial in (tmp, bed_tmp):
            if partial and os.path.isfile(partial):
                try:
                    os.remove(partial)
                except OSError:
                    pass
        info["warning"] = "ANUGA fine maxima could not be retained: " + str(exc)
    return info


def export_geotiffs(workdir, record, log=None):
    """Host-side GDAL export (also on reattach); ANUGA's Python needs no GDAL.

    Guard is checked again because another job may have consumed disk meanwhile.
    The NPZ remains useful even when raster export cannot be completed.
    """
    if not record.get("saved"):
        if record.get("warning") and log:
            log("WARNING: " + record["warning"])
        return
    tmp = None
    try:
        with np.load(os.path.join(workdir, ARCHIVE), allow_pickle=False) as data:
            meta = json.loads(str(data["metadata"]))
            nx, ny, dx = int(meta["nx"]), int(meta["ny"]), float(meta["cell_m"])
            estimate = int(1.25 * nx * ny * 8) + 16 * 1024 ** 2
            if not disk_allows(workdir, estimate):
                raise OSError("fine GeoTIFFs skipped: would leave less than 18 GiB free (or free space is unknown)")
            from osgeo import gdal
            import matplotlib.tri as mtri
            vertices, triangles = data["vertices"], data["triangles"]
            # Local coordinates avoid loss of precision on large projected coordinates.
            origin = vertices.min(axis=0)
            vertices = vertices - origin
            finder = mtri.Triangulation(vertices[:, 0], vertices[:, 1], triangles).get_trifinder()
            outputs = []
            for name, key, unit in ((DEPTH, "max_depth_m", "m"),
                                    (VELOCITY, "max_velocity_mps", "m/s")):
                values = data[key]
                path = os.path.join(workdir, name)
                tmp = path + ".tmp"
                ds = gdal.GetDriverByName("GTiff").Create(tmp, nx, ny, 1, gdal.GDT_Float32,
                       options=["TILED=YES", "COMPRESS=DEFLATE", "BIGTIFF=IF_SAFER", "NUM_THREADS=1"])
                if ds is None:
                    raise OSError("Cannot create " + tmp)
                try:
                    ds.SetGeoTransform((meta["x0"], dx, 0, meta["y0"], 0, -dx))
                    ds.SetProjection(meta["crs_wkt"])
                    band = ds.GetRasterBand(1)
                    band.SetNoDataValue(-9999.0)
                    band.SetUnitType(unit)
                    for row in range(0, ny, 256):
                        for col in range(0, nx, 256):
                            if not disk_allows(workdir, 2 * 256 * 256 * 4):
                                raise OSError("fine GeoTIFF export stopped at the 18 GiB disk floor")
                            xs = meta["x0"] - origin[0] + (np.arange(col, min(col+256, nx))+.5)*dx
                            ys = meta["y0"] - origin[1] - (np.arange(row, min(row+256, ny))+.5)*dx
                            xx, yy = np.meshgrid(xs, ys)
                            ids = finder(xx, yy)
                            tile = values[np.maximum(ids, 0)].astype(np.float32)
                            tile[(ids < 0) | ~np.isfinite(tile)] = -9999.0
                            if band.WriteArray(tile, col, row) not in (None, 0):
                                raise OSError("Cannot write fine GeoTIFF tile")
                    if ds.FlushCache() not in (None, 0):
                        raise OSError("Cannot flush fine GeoTIFF")
                finally:
                    band = None
                    ds = None
                os.replace(tmp, path)
                outputs.append(name)
            record["geotiffs"] = outputs
    except Exception as exc:
        if tmp and os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        record["warning"] = "ANUGA fine maxima GeoTIFF export: " + str(exc)
        if log:
            log("WARNING: " + record["warning"])
