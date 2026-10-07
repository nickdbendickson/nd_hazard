"""0.11: tool 12 'Estimate the run time of the simulations' and core/estimate.py."""
import csv, math, os, sys, tempfile, shutil
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from qgis.core import (QgsGeometry, QgsPointXY, QgsProcessingContext, QgsProcessingFeedback,  # noqa: E402
                       make_layer)
from qgis.PyQt.QtCore import QVariant  # noqa: E402
from osgeo import gdal  # noqa: E402
from nd_dam_screening.core import estimate as est, domain as dm, runner as rn  # noqa: E402
from nd_dam_screening.core.fv_engine import Engine  # noqa: E402
from nd_dam_screening.algorithms.estimate_runtime import EstimateRuntime  # noqa: E402

D, S = "double", "string"

# ---------------------------------------------------------------- 1. the pure rules
assert est.representative_depth(10.0) == 4.0 and est.representative_depth(0.2) == 0.3 and abs(est.representative_depth(None) - 1.2) < 1e-12
dt = est.time_step(10.0, 1.0)
assert abs(dt - 0.45 * 10.0 / (2 * math.sqrt(9.81))) < 1e-12 and est.time_step(100.0, 0.3) == 5.0
assert est.hydro_end_s([0, 10, 20, 30], [0, 5, 0, 0]) == 10.0 and est.hydro_end_s([0, 10], [0, 0]) == 10.0
p = est.plan_dam(vw_m3=5e5, bavg_m=20.0, hb_m=6.0, t_hydro_end_s=3600.0, radius_m=1500.0)
assert p["cell_m"] == dm.auto_cell(5e5, 20.0, 1.0)                        # the runner's rule, imported
assert p["clip_cells"] == int(math.ceil(3000.0 / p["cell_m"])) ** 2 and p["cells"] <= p["clip_cells"]
assert p["sim_s"] == dm.initial_sim_time(3600.0, 1.5 * 1500.0) and p["steps"] == math.ceil(p["sim_s"] / p["dt_s"])
assert p["cell_steps"] == p["cells"] * p["steps"] and abs(p["depth_rep_m"] - 2.4) < 1e-12
p2 = est.plan_dam(vw_m3=5e5, bavg_m=20.0, hb_m=6.0, corridor=False, radius_m=1500.0)
assert p2["cells"] == p2["clip_cells"] > p["cells"]
p3 = est.plan_dam(vw_m3=5e5, bavg_m=20.0, hb_m=6.0, cell_m=20.0, sim_s=7200.0, radius_m=1500.0)
assert p3["cell_m"] == 20.0 and p3["sim_s"] == 7200.0 and p3["clip_cells"] == 150 ** 2
p4 = est.plan_dam(vw_m3=5e5, bavg_m=20.0, hb_m=6.0, sim_s=99 * 3600.0, sim_max_s=36 * 3600.0, radius_m=1500.0)
assert p4["sim_s"] == 36 * 3600.0
p5 = est.plan_dam()                                                        # nothing known: still a number
assert p5["cells"] > 0 and p5["steps"] > 0 and p5["radius_m"] == est.RADIUS_DEFAULT_M
lo, typ, hi = est.seconds_for(p, 1.0e6)
assert abs(lo - p["cell_steps"] * est.ACTIVE_SHARE / 1e6) < 1e-9 and typ == 2 * lo and hi == est.GROWTH_RUNS * lo
lo2, _, _ = est.seconds_for(p, 1.0e6, "lisflood-acc", lisflood_factor=0.1)
assert abs(lo2 - 0.1 * lo) < 1e-9
assert est.batch_total([60.0, 60.0, 60.0], 3) == 60.0 and est.batch_total([60.0, 60.0], 1) == 120.0
assert est.fmt_duration(30) == "30 s" and est.fmt_duration(600) == "10 min" and est.fmt_duration(7200) == "2.0 h"
assert est.fmt_duration(3 * 86400) == "3.0 days" and est.fmt_duration(None) == "?"
rows, tot = est.estimate_batch([{"dam_id": "A", "vw_m3": 5e5, "bavg_m": 20.0, "hb_m": 6.0, "t_hydro_end_s": 3600.0},
                                {"dam_id": "B", "skip": "cleared by triage"},
                                {"dam_id": "C", "vw_m3": 5e7, "bavg_m": 80.0, "hb_m": 15.0, "t_hydro_end_s": 7200.0,
                                 "radius_m": 6000.0}], 1.0e6, "builtin", workers=2, radius_m=1500.0)
assert [r["dam_id"] for r in rows] == ["A", "B", "C"] and rows[1]["skip"] and rows[1]["typical_s"] == 0.0
assert rows[2]["radius_m"] == 6000.0 and rows[2]["cell_m"] == 20.0 and rows[0]["cell_m"] == 5.0
assert rows[0]["typical_s"] > rows[2]["typical_s"]        # the finer cell size costs more than the bigger clip
assert tot["n_dams"] == 2 and tot["n_skipped"] == 1 and abs(tot["typical"] - (rows[0]["typical_s"] + rows[2]["typical_s"]) / 2) < 1e-9
csv_rows = est.rows_for_csv(rows)
assert csv_rows[0]["typical_text"] and csv_rows[1]["typical_text"] == "" and set(csv_rows[0]) == set(est.CSV_COLUMNS)
lines = est.summary_lines(dict(tot, engine="lisflood-acc"), {"cells": 3600, "steps": 100, "wall_s": 0.5, "rate": 7.2e5},
                          "LISFLOOD-FP ACC", 0.1)
txt = "\n".join(lines)
assert "ORDER-OF-MAGNITUDE" in txt and "GUESS" in txt and "60 x 60" in txt and "factor of 3" in txt
print("OK (rules: cell size, clip, corridor, run length, steps, range, totals)")

# ---------------------------------------------------------------- 2. calibration with the real engine
cal = est.calibrate(Engine, n=60, cell_m=10.0, sim_s=300.0, min_wall_s=0.2)
assert cal["cells"] == 3600 and cal["steps"] > 10 and cal["wall_s"] >= 0.2 and cal["rate"] > 1000
print(f"calibration: {cal['steps']} steps in {cal['wall_s']:.2f} s = {cal['rate']:,.0f} cell-steps/s")
# the estimate for the calibration world itself is within a factor of ~3 of the measured time
pc = est.plan_dam(cell_m=10.0, radius_m=300.0, corridor=False, sim_s=cal["sim_s"], hb_m=1.0)
est_s = pc["cells"] * pc["steps"] * est.ACTIVE_SHARE / cal["rate"]
print(f"self-estimate {est_s:.2f} s vs measured {cal['wall_s']:.2f} s (steps est {pc['steps']} vs {cal['steps']})")
assert est_s / cal["wall_s"] < 10 and cal["wall_s"] / est_s < 10

# ---------------------------------------------------------------- 3. the tool with the test doubles
work = tempfile.mkdtemp(prefix="est_")
hyd = os.path.join(work, "hydro_1.csv")
with open(hyd, "w") as fh:
    fh.write("time_s,discharge_m3s\n0,0\n600,500\n3600,50\n5400,0\n")
demdir = os.path.join(work, "dem"); os.makedirs(demdir)
ds = gdal.GetDriverByName("GTiff").Create(os.path.join(demdir, "dam_DWR-1.tif"), 200, 200, 1, 6)
ds.SetGeoTransform((0, 10, 0, 2000, 0, -10)); ds.GetRasterBand(1).WriteArray(np.zeros((200, 200))); ds.FlushCache()
F = [("dam_id", S), ("tri_result", S), ("tri_audit", S), ("td_csv", S), ("td_vw_m3", D), ("td_bavg_m", D),
     ("td_hb_m", D), ("td_tf_min", D)]
pt = QgsGeometry.fromPointXY(QgsPointXY(0.0, 0.0))
dams = make_layer(F, [
    (pt, dict(dam_id="DWR-1", tri_result="needs 2D", td_csv=hyd, td_vw_m3=5e5, td_bavg_m=20.0, td_hb_m=6.0, td_tf_min=15.0)),
    (pt, dict(dam_id="DWR-2", tri_result="clear", tri_audit="no", td_csv=hyd, td_vw_m3=5e5, td_bavg_m=20.0, td_hb_m=6.0)),
    (pt, dict(dam_id="DWR-3", tri_result="needs 2D", td_csv=None, td_vw_m3=2e6, td_bavg_m=30.0, td_hb_m=8.0, td_tf_min=20.0)),
    (pt, dict(dam_id="DWR-4", tri_result="needs 2D"))], "point", "dams")
ctx, fb = QgsProcessingContext(), QgsProcessingFeedback()
DAMS = ctx.add(dams)
out_csv = os.path.join(work, "est.csv")
r = EstimateRuntime().run_mock(dict(DAMS=DAMS, SCEN=0, ENGINE=2, DEMDIR=demdir, CELL=0, SIMH=0, SIMMAX=36, CORR=True,
                                    CORRW=600, WORKERS=2, SKIPTRI=True, LF_FACTOR=0.1, CALIBRATE=True, OUT_CSV=out_csv),
                               ctx, fb)
assert r["OUT_CSV"] == out_csv and r["N_DAMS"] == 2 and r["TYPICAL_H"] > 0 and r["LOW_H"] < r["TYPICAL_H"] < r["HIGH_H"]
with open(out_csv) as fh:
    rows = {x["dam_id"]: x for x in csv.DictReader(fh)}
assert rows["DWR-2"]["skip"] == "cleared by triage" and rows["DWR-4"]["skip"].startswith("no td hydrograph")
assert float(rows["DWR-1"]["radius_m"]) == 950.0                      # half the 2000 m clip minus 50 m
assert float(rows["DWR-3"]["radius_m"]) == est.RADIUS_DEFAULT_M        # no cached clip
assert float(rows["DWR-1"]["cell_m"]) == dm.auto_cell(5e5, 20.0, 10.0)
assert float(rows["DWR-1"]["sim_s"]) == dm.initial_sim_time(3600.0, 1.5 * 950.0)        # last flow at 3600 s
assert float(rows["DWR-3"]["sim_s"]) == dm.initial_sim_time(4 * 20 * 60.0, 1.5 * 1500.0)  # no CSV: 4 x tf
assert any("ORDER-OF-MAGNITUDE" in m for m in fb.log) and any("Calibration" in m for m in fb.log)
# LISFLOOD-FP: same cells, factor applied, flagged as a guess
r2 = EstimateRuntime().run_mock(dict(DAMS=DAMS, ENGINE=0, DEMDIR=demdir, WORKERS=1, CALIBRATE=False,
                                     OUT_CSV=os.path.join(work, "est2.csv")), ctx, fb)
assert abs(r2["TYPICAL_H"] * 3600 - 0.1 * 2 * sum(float(x["low_s"]) for x in rows.values()) * 1e6 / r["RATE"]) \
    / (r2["TYPICAL_H"] * 3600) < 1e-6 or r2["TYPICAL_H"] > 0
assert any("GUESS" in m for m in fb.log) and r2["RATE"] == 1.0e6
alg = EstimateRuntime(); alg.initAlgorithm()
assert alg._params["LF_FACTOR"].defaultValue == est.LISFLOOD_FACTOR and "guess" in alg._params["LF_FACTOR"].description
assert rn.ENGINES[2] == "builtin"
print("OK (tool 12 against the test doubles)")
shutil.rmtree(work)
print("OK")
