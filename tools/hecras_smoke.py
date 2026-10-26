#!/usr/bin/env python3
"""Headless HEC-RAS smoke check of the Runner's Build a HEC-RAS 2D model export.

Builds the synthetic dam the Runner's tests use (tests/test_hecras_model.make_run), exports it
with core/hecras_model.build_model, builds the RAS terrain with RasProcess.exe CreateTerrain,
runs Ras.exe -c (geometry, preprocessing, unsteady solve, post-processing) under Wine with the
real .NET 4.8, and checks the plan HDF: the solution finished, the volume error is small and
the 2D area got wet. Each check prints one line; exit 1 when one fails.

    python3 tools/hecras_smoke.py --build OUT       # export only (no Wine needed)
    python3 tools/hecras_smoke.py --check OUT       # read OUT's plan HDF (after the run)
    python3 tools/hecras_smoke.py OUT               # build, run (tools/hecras_wine_run.sh), check

Needs GDAL and h5py for --build / --check; the run needs Wine 9 + .NET 4.8 + HEC-RAS 6.6 in
WINEPREFIX (see .github/workflows/hecras-smoke.yml) and an X display (Xvfb).
"""
import argparse
import glob
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

MAX_VOLUME_ERROR_PCT = 2.0      # 0.6-0.7% on DWR-2801 in the 2026-10-26 runs


def build(out):
    from nd_breach_runner.core import hecras_model as hm
    from nd_breach_runner.tests.test_hecras_model import make_run
    work = os.path.join(out, "_run")
    os.makedirs(work, exist_ok=True)
    dfold, _dem, _dam, _hy = make_run(work, flood_crs=5070)
    r = hm.build_model(dfold)
    folder = r["folder"]
    print("exported:", folder)
    for n in r.get("notes") or []:
        print("  note:", n)
    return folder


def model_folder(out):
    hits = sorted(glob.glob(os.path.join(out, "**", "*.prj"), recursive=True))
    hits = [h for h in hits if os.path.basename(os.path.dirname(h)) != "GIS"]
    if not hits:
        raise SystemExit("no HEC-RAS project (.prj) under {}".format(out))
    return os.path.dirname(hits[0])


def check(folder):
    import h5py
    import numpy as np
    plans = sorted(glob.glob(os.path.join(folder, "*.p01.hdf")))
    ok = True

    def line(good, text):
        nonlocal ok
        ok = ok and good
        print("{}  {}".format("ok  " if good else "FAIL", text))

    line(bool(plans), "plan HDF written ({})".format(plans[0] if plans else "none"))
    if not plans:
        return False
    with h5py.File(plans[0], "r") as f:
        summ = f.get("Results/Unsteady/Summary")
        line(summ is not None, "unsteady results present")
        if summ is None:
            return False
        sol = summ.attrs.get("Solution", b"").decode(errors="replace")
        line("Finished Successfully" in sol, "solution: {}".format(sol or "(none)"))
        va = summ.get("Volume Accounting")
        err = float(va.attrs.get("Error Percent", np.nan)) if va is not None else float("nan")
        line(np.isfinite(err) and abs(err) <= MAX_VOLUME_ERROR_PCT,
             "volume error {:.2f}% (limit {}%)".format(err, MAX_VOLUME_ERROR_PCT))
        areas = f.get("Geometry/2D Flow Areas")
        name = None
        if areas is not None:
            name = next((k for k in areas if isinstance(areas[k], h5py.Group)), None)
        wet = 0
        if name:
            n = int(areas["Cell Info"][0][1]) if "Cell Info" in areas else None
            zmin = areas[name]["Cells Minimum Elevation"][:n]
            base = "Results/Unsteady/Output/Output Blocks/Base Output/Summary Output/2D Flow Areas/"
            wse = f[base + name + "/Maximum Water Surface"][0, :n]
            depth = np.where(np.isfinite(zmin) & np.isfinite(wse), wse - zmin, 0.0)
            wet = int((depth > 0.5).sum())
            line(wet > 0, "2D area '{}': {} cells deeper than 0.5, max depth {:.2f}".format(
                name, wet, float(np.nanmax(depth))))
        else:
            line(False, "no 2D flow area in the plan HDF")
    return ok


def run(folder):
    script = os.path.join(ROOT, "tools", "hecras_wine_run.sh")
    p = subprocess.run(["bash", script, folder], check=False)
    log = os.path.join(folder, "ras_c.log")
    if os.path.exists(log):
        with open(log, errors="replace") as f:
            tail = f.read().splitlines()[-25:]
        print("--- last lines of ras_c.log ---")
        print("\n".join(tail))
    return p.returncode


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--build", action="store_true", help="export only")
    g.add_argument("--check", action="store_true", help="check an exported, run model")
    a = ap.parse_args()
    if a.check:
        return 0 if check(model_folder(a.out)) else 1
    if os.path.isdir(a.out) and not a.build:
        shutil.rmtree(a.out)
    folder = build(a.out)
    if a.build:
        return 0
    rc = run(folder)
    print("hecras_wine_run.sh exit status", rc)
    return 0 if check(folder) else 1


if __name__ == "__main__":
    sys.exit(main())
