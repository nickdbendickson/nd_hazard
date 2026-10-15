"""Checks that must pass before a flood solve starts (1.9.0).

A failure here costs seconds; the same failure inside the solver costs the run. The Runner
(core/workflow.run_dam) and the Screening flood tool (tool 7) call these just before the
first model run. Pure Python: no QGIS, no numpy; GDAL only in `raster_info`, imported when
called.

    check(status, title, detail, fix)        one result: status "ok" | "warn" | "fail"
    one_crs(codes)                           every terrain tile has the same CRS
    dem_covers(extents, xy, nodata_fraction) the dam point is on the terrain
    hydro_stats(t_s, q_cms)                  the breach hydrograph has a peak and a volume
    layer_overlap(title, crs_valid, layer_extent, study_box, n_features)
                                             a layer that was given meets the study area;
                                             no CRS and no overlap are told apart
    storage_check(dem_m3, inventory_m3)      a DEM stage-storage volume against an inventory
    lisflood_smoke(exe)                      LISFLOOD-FP runs 1 s on a 5 x 5 grid (cached per
                                             executable and its modification time;
                                             ND_DAM_PRE_RUN_SMOKE=0 in the environment skips it)
    raster_info(path)                        {"epsg", "extent"} of a raster (GDAL)
    failures(checks) / message(checks)       what stops the run

Idea from the DamSafety plugins' core/solve_gate.py (2026-10-08 audit); written here with
these changes: a statewide layer with nothing near the dam is not an error (only a layer whose
whole extent misses the study area is), the smoke test is cached so a batch pays for it once,
and the built-in engine is never smoke-tested (it is this package's own Python).
"""
import os
import shutil
import subprocess
import tempfile

OK, WARN, FAIL = "ok", "warn", "fail"
CFS = 35.31466672148859         # cfs per m3/s
ACFT = 1233.48183754752         # m3 per acre-foot
# 3DEP ImageServer exports: a 10-mile box at 10 m is about 3,200 pixels a side, and the
# DamSafety audit recorded that as one tile timing out. Screening tiles at most this size.
TILE_PX = 1200
SMOKE_TIMEOUT_S = 60

_SMOKE_CACHE = {}               # (real path, mtime) -> check


def check(status, title, detail="", fix=""):
    return {"status": status, "title": title, "detail": detail, "fix": fix}


def one_crs(codes, title="Terrain CRS"):
    found = []
    for c in codes or []:
        c = str(c or "").strip()
        if c and c not in found:
            found.append(c)
    if not found:
        return check(FAIL, title, "the terrain has no CRS",
                     "Give the DEM a CRS (or warp it to one) before the run.")
    if len(found) > 1:
        return check(FAIL, title, "the terrain tiles have different CRSs: {}".format(
            ", ".join(found)), "Warp every tile to one CRS. Do not mix feet and metre tiles.")
    return check(OK, title, found[0])


def _inside(ext, xy):
    return ext is not None and ext[0] <= xy[0] <= ext[2] and ext[1] <= xy[1] <= ext[3]


def dem_covers(extents, xy, nodata_fraction=None, title="DEM covers the dam"):
    """extents: [(xmin, ymin, xmax, ymax)] in the CRS of xy. nodata_fraction: share of the
    cells around the dam without data (None = not known)."""
    if xy is None:
        return check(FAIL, title, "no dam point", "Give the dam point.")
    boxes = [e for e in (extents or []) if e is not None]
    if not boxes:
        return check(FAIL, title, "no terrain extent", "Choose the terrain raster.")
    if not any(_inside(e, xy) for e in boxes):
        return check(FAIL, title, "the dam point {:.0f}, {:.0f} is outside the terrain".format(
            xy[0], xy[1]), "Download or clip terrain that contains the dam.")
    if nodata_fraction is not None and nodata_fraction > 0.5:
        return check(FAIL, title, "{:.0%} of the ground around the dam point has no data".format(
            nodata_fraction), "The dam is at the edge of the terrain; use terrain that covers "
                              "the valley below it.")
    return check(OK, title, "the dam point is on the terrain")


def hydro_stats(t_s, q_cms, title="Breach hydrograph"):
    t = [float(v) for v in (t_s if t_s is not None else [])]
    q = [float(v) for v in (q_cms if q_cms is not None else [])]
    if len(t) < 2 or len(t) != len(q):
        return check(FAIL, title, "no hydrograph", "Compute the breach hydrograph first.")
    peak_i = max(range(len(q)), key=lambda i: q[i])
    vol = sum(0.5 * (q[i] + q[i - 1]) * (t[i] - t[i - 1]) for i in range(1, len(t)))
    detail = "peak {:,.0f} cfs at {:.2f} h, volume {:,.0f} ac-ft, {} points".format(
        q[peak_i] * CFS, t[peak_i] / 3600.0, vol / ACFT, len(t))
    if q[peak_i] <= 0 or vol <= 0:
        return check(FAIL, title, detail + ": the peak or the volume is zero",
                     "Check the storage, the dam height and the breach parameters.")
    if len(t) == 2:
        return check(WARN, title, detail + " (a straight line, not a breach hydrograph)",
                     "Check the storage and the breach parameters.")
    return check(OK, title, detail)


def boxes_meet(a, b):
    return a is not None and b is not None and not (
        a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def layer_overlap(title, crs_valid, layer_extent, study_box, n_features=None, fix=""):
    """A layer that was given must meet the study area. Two different failures:
    no CRS (its coordinates can't be compared) and a CRS but no overlap (wrong layer or wrong
    extent). A statewide layer with nothing near this dam meets the box and passes.
    layer_extent: the layer's extent in the study CRS (None when it could not be transformed)."""
    if n_features == 0:
        return check(OK, title, "the layer is empty")
    if not crs_valid:
        return check(FAIL, title, "the layer has no CRS, so its coordinates can't be placed",
                     "Set the layer's CRS. This is a missing CRS, not an extent mismatch.")
    if layer_extent is None:
        return check(WARN, title, "the layer's extent could not be put in the study CRS")
    if not boxes_meet(layer_extent, study_box):
        return check(FAIL, title, "the layer has a CRS but none of it reaches the study area "
                                  "(an extent mismatch, not a missing CRS)",
                     fix or "Use a layer that covers this dam's valley.")
    return check(OK, title, "the layer reaches the study area")


def storage_check(dem_m3, inventory_m3, tol=0.5):
    """'' or a note comparing the DEM stage-storage volume with an inventory volume. The DEM
    curve is kept either way. The note starts with 'differs' when they differ by more than
    tol (a fraction of the DEM volume)."""
    try:
        inv = float(inventory_m3)
        dem = float(dem_m3)
    except (TypeError, ValueError):
        return ""
    if inv <= 0:
        return ""
    if dem <= 0:
        return "differs: the DEM stage-storage is empty; the inventory says {:,.0f} ac-ft".format(
            inv / ACFT)
    if abs(inv / dem - 1.0) > tol:
        return ("differs: inventory storage {:,.0f} ac-ft vs the DEM curve {:,.0f} ac-ft; the "
                "DEM curve is used".format(inv / ACFT, dem / ACFT))
    return "inventory storage {:,.0f} ac-ft agrees with the DEM curve {:,.0f} ac-ft".format(
        inv / ACFT, dem / ACFT)


_SMOKE_PAR = ("DEMfile dem.asc\nresroot smoke\ndirroot out\nsim_time 1\ninitial_tstep 1\n"
              "saveint 1\nmassint 1\nfpfric 0.05\nacceleration\n")


def lisflood_smoke(exe, work=None, timeout_s=SMOKE_TIMEOUT_S, title="LISFLOOD-FP starts"):
    """Run LISFLOOD-FP for 1 simulated second on a 5 x 5 grid. Proves the binary starts on this
    computer (libraries, OpenMP runtime, permissions) and writes its maximum-depth file. Cached
    per executable and its modification time."""
    exe = str(exe or "").strip()
    if os.environ.get("ND_DAM_PRE_RUN_SMOKE", "").strip() == "0":
        return check(OK, title, "not tested (ND_DAM_PRE_RUN_SMOKE=0)")
    if not exe or not os.path.isfile(exe):
        return check(FAIL, title, "executable not found: {}".format(exe or "(none)"),
                     "Give the LISFLOOD-FP executable, or choose the built-in engine.")
    real = os.path.realpath(exe)
    try:
        key = (real, os.path.getmtime(real))
    except OSError:
        key = (real, 0)
    if key in _SMOKE_CACHE:
        return dict(_SMOKE_CACHE[key])
    folder = work or tempfile.mkdtemp(prefix="lf_smoke_")
    try:
        os.makedirs(os.path.join(folder, "out"), exist_ok=True)
        with open(os.path.join(folder, "dem.asc"), "w") as fh:
            fh.write("ncols 5\nnrows 5\nxllcorner 0\nyllcorner 0\ncellsize 10\n"
                     "NODATA_value -9999\n")
            fh.write("\n".join(" ".join(["100"] * 5) for _ in range(5)) + "\n")
        with open(os.path.join(folder, "smoke.par"), "w") as fh:
            fh.write(_SMOKE_PAR)
        flags = 0x08000000 if os.name == "nt" else 0          # no console window on Windows
        env = dict(os.environ, OMP_NUM_THREADS="1")
        try:
            r = subprocess.run([exe, "smoke.par"], cwd=folder, capture_output=True, text=True,
                               timeout=timeout_s, env=env, creationflags=flags)
        except subprocess.TimeoutExpired:
            out = check(FAIL, title, "no answer in {} s on a 5 x 5 grid".format(timeout_s),
                        "The executable hangs. Fix it or choose the built-in engine.")
        except OSError as e:
            out = check(FAIL, title, "it does not start: {}".format(e),
                        "Check the file (a program for this computer, allowed to run).")
        else:
            if r.returncode != 0:
                tail = (r.stderr or r.stdout or "").strip().splitlines()
                out = check(FAIL, title, "exit code {}: {}".format(
                    r.returncode, tail[-1] if tail else "no message"),
                    "Fix the executable (missing libraries?) or choose the built-in engine.")
            elif not os.path.isfile(os.path.join(folder, "out", "smoke.max")):
                tail = (r.stdout or r.stderr or "").strip().splitlines()
                out = check(FAIL, title, "it ran but wrote no result ({})".format(
                    tail[-1] if tail else "no message"),
                    "This may not be LISFLOOD-FP. Give the LISFLOOD-FP executable, or choose "
                    "the built-in engine.")
            else:
                out = check(OK, title, "ran 1 s on a 5 x 5 test grid")
    finally:
        if work is None:
            shutil.rmtree(folder, ignore_errors=True)
    if out["status"] == OK:
        _SMOKE_CACHE[key] = dict(out)
    return out


def raster_info(path):
    """{"epsg": "EPSG:n" or "", "extent": (xmin, ymin, xmax, ymax) or None} via GDAL."""
    try:
        from osgeo import gdal, osr
        ds = gdal.Open(str(path))
        if ds is None:
            return {"epsg": "", "extent": None}
        gt = ds.GetGeoTransform()
        nx, ny = ds.RasterXSize, ds.RasterYSize
        xs = (gt[0], gt[0] + nx * gt[1] + ny * gt[2])
        ys = (gt[3], gt[3] + nx * gt[4] + ny * gt[5])
        epsg = ""
        wkt = ds.GetProjection()
        if wkt:
            srs = osr.SpatialReference()
            srs.ImportFromWkt(wkt)
            try:
                srs.AutoIdentifyEPSG()
            except Exception:                                    # noqa - not every CRS has one
                pass
            code = srs.GetAuthorityCode(None)
            epsg = "EPSG:" + code if code else srs.ExportToProj4() or "custom CRS"
        return {"epsg": epsg, "extent": (min(xs), min(ys), max(xs), max(ys))}
    except Exception:                                            # noqa
        return {"epsg": "", "extent": None}


def failures(checks):
    return [c for c in checks or [] if c and c.get("status") == FAIL]


def lines(checks):
    return ["{} {}: {}".format(c["status"].upper(), c["title"], c["detail"])
            for c in checks or [] if c]


def message(checks):
    """The text of the error that stops the run ('' when nothing failed)."""
    bad = failures(checks)
    if not bad:
        return ""
    parts = ["{}: {}{}".format(c["title"], c["detail"], (" ({})".format(c["fix"]) if c["fix"]
                                                           else "")) for c in bad]
    return "Stopped before the flood run (pre-run check): " + "; ".join(parts)
