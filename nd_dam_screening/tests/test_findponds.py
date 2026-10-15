"""0.12: tool 16 'Find ponds and dugouts' end to end against the QGIS test doubles, and the
'no breach flood' path through tools 5, 6, 7 and 8 on its output.

A synthetic landscape on a field that falls south at 3 %: a ring-dike pond (embankment), a round
dugout (nothing above the ground), a dugout with a spoil rim (some water above the ground), a
natural depression, a pond under the size limit and one outside the terrain. LiDAR = a local GeoTIFF.
Real GDAL, simulated QGIS: not run in a real QGIS.
"""
import csv
import math
import os
import shutil
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from qgis.core import (FakeSource, QgsGeometry, QgsPointXY, QgsProcessingContext,  # noqa: E402
                       QgsProcessingFeedback, make_layer)
from osgeo import gdal, osr  # noqa: E402
from nd_dam_screening.algorithms import find_ponds as fp  # noqa: E402
from nd_dam_screening.algorithms.find_ponds import FindPonds  # noqa: E402
from nd_dam_screening.algorithms.breach_hydrograph import BreachHydrograph  # noqa: E402
from nd_dam_screening.algorithms.triage import Triage  # noqa: E402
from nd_dam_screening.algorithms.run_breach_sim import RunBreachSim  # noqa: E402
from nd_dam_screening.algorithms.hazard_classification import HazardClassification  # noqa: E402
from nd_dam_screening.core import ponds as pd  # noqa: E402

S = "string"
CELL = 2.0
X0, Y0 = 500000.0, 2599000.0                  # lower-left of the terrain tile, EPSG:5070
W, H = 1400.0, 1000.0

# ---------------------------------------------------------------- 1. the helpers
assert fp.pond_id(1.0, 2.0) == fp.pond_id(1.4, 2.4) != fp.pond_id(1.0, 3.0)      # 1 m rounding, stable
assert fp.pond_id(1.0, 2.0).startswith("POND-") and len(fp.pond_id(1.0, 2.0)) == 13
assert fp.words("Excavated; dugout , ") == ["excavated", "dugout"] and fp.words(None) == []
assert fp.mentions("PUBHx excavated", ["excavated"]) and not fp.mentions("PEM1A", ["excavated"])

# ---------------------------------------------------------------- 2. the terrain
nx, ny = int(W / CELL), int(H / CELL)
col, row = np.meshgrid(np.arange(nx), np.arange(ny))
E = (col + 0.5) * CELL                        # metres east of the tile's west edge
N = H - (row + 0.5) * CELL                    # metres north of its south edge
z = 100.0 + 0.03 * (N - 500.0)                # a field falling south at 3 %


def circle(cx, cy, r, n=48):
    return [(cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n)) for i in range(n)]


def pit(cx, cy, depth, slope, r_water=15.0, berm=0.0):
    r = np.hypot(E - cx, N - cy)
    ground = 100.0 + 0.03 * (cy - 500.0)
    pool = ground - depth
    bank = np.minimum(z, pool + (r - r_water) * slope)
    zz = np.where(r < r_water, pool, bank)
    zz = zz + np.where((r >= 25.0) & (r <= 31.0), berm, 0.0)
    return np.where(r < 65.0, zz, z), r < r_water


A = (200.0, 500.0)
dx = np.maximum(np.abs(E - A[0]) - 20.0, 0.0)
dy = np.maximum(np.abs(N - A[1]) - 15.0, 0.0)
d_out = np.hypot(dx, dy)
rect = (dx == 0) & (dy == 0)
ring = 102.0 - np.maximum(d_out - 3.0, 0.0) / 3.0
z = np.where(d_out < 60.0, np.maximum(z, ring), z)
z[rect] = 100.0
for (cx, cy), depth, slope, berm in (((500.0, 500.0), 3.0, 0.5, 0.0),      # B: dugout
                                     ((800.0, 500.0), 3.0, 0.5, 1.0),      # C: dugout with a spoil rim
                                     ((1100.0, 500.0), 1.0, 0.05, 0.0)):   # E: natural depression
    z, _w = pit(cx, cy, depth, slope, berm=berm)

work = tempfile.mkdtemp(prefix="ponds_")
demdir = os.path.join(work, "lidar")
os.makedirs(demdir)
ds = gdal.GetDriverByName("GTiff").Create(os.path.join(demdir, "tile.tif"), nx, ny, 1, gdal.GDT_Float32)
ds.SetGeoTransform((X0, CELL, 0.0, Y0 + H, 0.0, -CELL))
srs = osr.SpatialReference(); srs.ImportFromEPSG(5070)
ds.SetProjection(srs.ExportToWkt())
ds.GetRasterBand(1).SetNoDataValue(-9999.0)
ds.GetRasterBand(1).WriteArray(z.astype("float32"))
ds.FlushCache(); ds = None

# ---------------------------------------------------------------- 3. the water bodies
def poly(pts):
    return QgsGeometry.fromPolygonXY([[QgsPointXY(X0 + x, Y0 + y) for x, y in pts] + [QgsPointXY(X0 + pts[0][0], Y0 + pts[0][1])]])


rows = [
    (poly([(180, 485), (220, 485), (220, 515), (180, 515)]), dict(wb_id="A", ftype="PUBHh")),
    (poly(circle(500, 500, 15)), dict(wb_id="B", ftype="PUBHx excavated")),
    (poly(circle(800, 500, 15)), dict(wb_id="C", ftype="PUBHx excavated")),
    (poly(circle(1100, 500, 15)), dict(wb_id="E", ftype="PEM1A")),
    (poly(circle(1100, 800, 5)), dict(wb_id="D", ftype="PUBH")),                 # under the size limit
    (poly(circle(5000, 500, 15)), dict(wb_id="F", ftype="PUBH")),                # outside the terrain
]
wb = make_layer([("wb_id", S), ("ftype", S)], rows, "polygon", "water")
ctx, fb = QgsProcessingContext(), QgsProcessingFeedback()
WB = ctx.add(wb)


def pid(letter):
    g = [g for g, a in rows if a["wb_id"] == letter][0]
    c = g.centroid().asPoint()
    return fp.pond_id(c.x(), c.y())


ids = {k: pid(k) for k in "ABCDEF"}
terrain_dir, curves = os.path.join(work, "16_terrain"), os.path.join(work, "16_curves")
base = dict(WB=WB, WB_ID="wb_id", WB_TYPE="ftype", DUG_WORDS="", MIN_AC=0.05, MAX_AC=50.0, SRC=1, LOCAL=demdir,
            CELL=CELL, RADIUS_FT=984.252, CLIP_CELL=4.0, ARROW_FT=328.084, TERRAIN=terrain_dir, CURVES=curves)   # 300 m, 100 m

# ---------------------------------------------------------------- 4. tool 16
alg = FindPonds()
alg.initAlgorithm()
assert alg.name() == "findponds" and alg.displayName().startswith("16 - ")
for k in ("HEAD_FT", "ARC", "DUG_DEG", "DEPTH_FT", "RELEASE_AF", "MIN_AC", "MAX_AC", "RING_FT", "CREST_FT", "FACE_DEG"):
    assert "YOUR" in alg._params[k].description, k             # every threshold is the engineer's
# 0.13: user-facing distances are in feet; the metre twins are hidden and still read (old models)
assert "(ft" in alg._params["RING_FT"].description and "(ft" in alg._params["RADIUS_FT"].description
assert alg._params["RING"].description.startswith("(before 0.13)")
assert alg._params["CELL_FT"].description.startswith("Analysis cell size (ft") and alg._params["CELL"].description.startswith("(before 0.13)")
r16 = FindPonds().run_mock(dict(base), ctx, fb)
out = ctx.layers[r16["OUT"]]
by = {str(f["dam_id"]): f for f in out.getFeatures()}
print("written:", {k: by[v]["pond_type"] for k, v in ids.items() if v in by})
assert set(by) == {ids["A"], ids["B"], ids["C"]}, sorted(by)   # E excluded (depression), D too small, F failed
a, b, c = by[ids["A"]], by[ids["B"]], by[ids["C"]]
assert a["pond_type"] == pd.T_EMBANKMENT and not a["no_flood"] and a["source"] == "POND"
assert abs(a["est_pool_m"] - (100.0)) < 0.2 and abs(a["est_crest_m"] - 102.0) < 0.5, (a["est_pool_m"], a["est_crest_m"])
assert a["height_m"] > 2.0 and abs(a["height_m"] - (a["est_crest_m"] - a["grade_m"])) < 1e-9
assert a["stor_method"] == "estimated" and a["status"] == "Intact" and a["stor_used_m3"] == a["est_total_m3"]
assert a["above_grade_m3"] > 0 and abs(a["above_grade_af"] - a["above_grade_m3"] / pd.AF_M3) < 1e-9
assert b["pond_type"] == pd.T_DUGOUT and b["no_flood"] == "YES" and "natural ground" in b["no_flood_why"]
assert "no breach flood" in b["stor_notes"] and b["above_grade_m3"] == 0.0
assert c["pond_type"] == pd.T_DUGOUT and not c["no_flood"] and c["above_grade_m3"] > 100.0, c["above_grade_m3"]
assert "only the" in c["stor_notes"] and "ac-ft above the natural ground" in c["stor_notes"]
assert a["wb_id"] == "A" and b["wb_type"] == "PUBHx excavated" and a["area_ac"] > 0.2
# curves for every written pond (pool -> crest), terrain clips only for the ones that can breach
for k in "ABC":
    assert os.path.exists(os.path.join(curves, "curve_{}.csv".format(ids[k]))), k
for k, expect in (("A", True), ("B", False), ("C", True), ("E", False)):
    assert os.path.exists(os.path.join(terrain_dir, "dam_{}.tif".format(ids[k]))) == expect, k
with open(os.path.join(curves, "curve_{}.csv".format(ids["A"]))) as fh:
    cv = list(csv.DictReader(fh))
assert abs(float(cv[0]["elev_m"]) - a["est_pool_m"]) < 1e-3 and abs(float(cv[-1]["elev_m"]) - a["est_crest_m"]) < 1e-3
# breach points and arrows: only for the two that can breach
bp = {str(f["dam_id"]): f for f in ctx.layers[r16["OUTP"]].getFeatures()}
ar = {str(f["dam_id"]): f for f in ctx.layers[r16["OUTL"]].getFeatures()}
assert set(bp) == set(ar) == {ids["A"], ids["C"]}
assert 120.0 < bp[ids["A"]]["flow_az"] < 240.0, bp[ids["A"]]["flow_az"]       # downhill, south
assert bp[ids["A"]]["brch_method"] == "pond" and bp[ids["A"]]["height_m"] == a["est_crest_m"] - a["grade_m"]
assert bp[ids["A"]]["dir_conf"] in ("medium", "low") and bp[ids["C"]]["crest_len_m"] > 0
# not written: the natural depression (listed with its reason) and the pond outside the terrain (failed)
ex = {str(f["wb_id"]): f for f in ctx.layers[r16["OUTX"]].getFeatures()}
assert set(ex) == {"E", "F"}, set(ex)
assert ex["E"]["pond_type"] == pd.T_DEPRESSION and "natural depression" in ex["E"]["reason"]
assert ex["F"]["pond_type"] == "FAILED" and "terrain" in ex["F"]["reason"]
assert any("water body F" in e for e in fb.errors)
log = "\n".join(fb.log)
assert "by type:" in log and "1 of the written water bodies have no breach flood" in log and "Not run in a real QGIS" in log
print("OK (tool 16: types, storage, curves, clips, breach points, excluded list)")

# the same run again reuses the clips and gives the same IDs (resumable)
fb2 = QgsProcessingFeedback()
r16b = FindPonds().run_mock(dict(base), ctx, fb2)
assert {str(f["dam_id"]) for f in ctx.layers[r16b["OUT"]].getFeatures()} == set(by)
# 0.21.0: ... and the same storage (a rerun read the pond from the coarser dam clip: 2,800 -> 1,600 m3)
again = {str(f["dam_id"]): f["est_total_m3"] for f in ctx.layers[r16b["OUT"]].getFeatures()}
assert all(abs(again[k] - by[k]["est_total_m3"]) < 1e-6 for k in by), (again, {k: by[k]["est_total_m3"] for k in by})
# KEEP_DEP writes the depression to the dam layer too, flagged no flood
r16c = FindPonds().run_mock(dict(base, KEEP_DEP=True), ctx, QgsProcessingFeedback())
kept = {str(f["wb_id"]): f for f in ctx.layers[r16c["OUT"]].getFeatures()}
assert set(kept) == {"A", "B", "C", "E"} and kept["E"]["no_flood"] == "YES"
# the layer's own 'excavated' word turns the depression into a dugout (a word list the engineer gives)
r16d = FindPonds().run_mock(dict(base, DUG_WORDS="pem", KEEP_DEP=False), ctx, QgsProcessingFeedback())
assert {str(f["wb_id"]) for f in ctx.layers[r16d["OUT"]].getFeatures()} == {"A", "B", "C", "E"}
print("OK (resumable; keep depressions; excavated words)")

# 0.21.0: an inventory storage field (ac-ft): more than 50% from the DEM curve is a CHECK with low
# confidence; the DEM curve is still used. A record that agrees gets a note only.
a_af = a["est_total_m3"] / pd.AF_M3
rows_inv = [(g, dict(at, inv_af={"A": a_af * 3.0, "C": c["est_total_m3"] / pd.AF_M3 * 1.1}.get(at["wb_id"])))
            for g, at in rows]
WBI = ctx.add(make_layer([("wb_id", S), ("ftype", S), ("inv_af", "double")], rows_inv, "polygon", "water_inv"))
r16i = FindPonds().run_mock(dict(base, WB=WBI, WB_STOR="inv_af"), ctx, QgsProcessingFeedback())
bi = {str(f["wb_id"]): f for f in ctx.layers[r16i["OUT"]].getFeatures()}
assert bi["A"]["stor_notes"].startswith("CHECK: inventory storage") and bi["A"]["stor_conf"] == "low", bi["A"]["stor_notes"]
assert abs(bi["A"]["inv_vs_est"] - 3.0) < 0.01 and a["inv_vs_est"] is None
assert bi["A"]["stor_used_m3"] == a["stor_used_m3"]                      # the DEM curve is kept
assert bi["C"]["stor_notes"].startswith("inventory storage") and "agrees" in bi["C"]["stor_notes"]
assert bi["C"]["stor_conf"] == c["stor_conf"] and abs(bi["C"]["inv_vs_est"] - 1.1) < 0.01
assert not str(bi["B"]["stor_notes"]).startswith(("CHECK", "inventory"))   # no value: no note
print("OK (0.21.0: inventory storage check)")

# ---------------------------------------------------------------- 5. tool 5 on the ponds
r5 = BreachHydrograph().run_mock(dict(DAMS=r16["OUT"], CURVES=curves, BPTS=r16["OUTP"], MODE=0, METHOD=0,
                                      SUNNY=False, EXP=2.0, OUTDIR=os.path.join(work, "hydro")), ctx, fb)
t5 = {str(f["dam_id"]): f for f in ctx.layers[r5["OUT"]].getFeatures()}
assert t5[ids["A"]]["td_qp_m3s"] > 0 and t5[ids["A"]]["td_rel_m3"] > 0 and t5[ids["A"]]["brch_conf"] in ("medium", "low")
assert t5[ids["C"]]["td_qp_m3s"] > 0
# release only above grade: the breach cuts down to the ground, so the water released is the water above it
assert abs(t5[ids["A"]]["base_m"] - a["grade_m"]) < 1e-6
assert t5[ids["A"]]["td_rel_m3"] <= a["est_total_m3"] + 1e-6
assert t5[ids["C"]]["td_rel_m3"] < 0.7 * c["est_total_m3"], (t5[ids["C"]]["td_rel_m3"], c["est_total_m3"])
nb5 = t5[ids["B"]]
assert nb5["td_qp_m3s"] is None and nb5["brch_conf"] == "none" and "no breach flood" in nb5["brch_notes"]
assert not any("dam " + ids["B"] in e for e in fb.errors) and not any("FAILED" in str(nb5["brch_notes"]) for _ in [0])
assert any("1 with no breach flood" in m for m in fb.log), fb.log[-6:]
print("OK (tool 5: embankment and dugout-with-rim breach; no-flood dugout skipped, not failed)")

# ---------------------------------------------------------------- 6. tool 6 (triage)
fb6 = QgsProcessingFeedback()
r6 = Triage().run_mock(dict(DAMS=r5["OUT"], BPTS=r16["OUTP"], DEMDIR=terrain_dir, AUDIT=100.0,
                            OUTDIR=os.path.join(work, "triage")), ctx, fb6)
t6 = {str(f["dam_id"]): f for f in ctx.layers[r6["OUT"]].getFeatures()}
assert t6[ids["B"]]["tri_result"] == "clear" and t6[ids["B"]]["tri_reason"].startswith("no breach flood")
assert t6[ids["B"]]["tri_audit"] == "no"                       # never sent to 2D by the audit: nothing to run
assert t6[ids["A"]]["tri_result"] in ("clear", "needs 2D")
print("OK (tool 6: the no-flood dugout is cleared with its reason and never audited)")

# ---------------------------------------------------------------- 7 and 8 on the no-flood dugout
src5 = ctx.layers[r5["OUT"]]
only_b = FakeSource(src5.fields(), [f for f in src5.getFeatures() if str(f["dam_id"]) == ids["B"]], src5.wkbType())
fb7 = QgsProcessingFeedback()
r7 = RunBreachSim().run_mock(dict(DAMS=ctx.add(only_b), BPTS=r16["OUTP"], DEMDIR=terrain_dir, ENGINE=2, SKIPTRI=False,
                                  OUTDIR=os.path.join(work, "sims")), ctx, fb7)
t7 = list(ctx.layers[r7["OUT"]].getFeatures())
assert len(t7) == 1 and t7[0]["sim_status"] == "noflood" and "no breach flood" in t7[0]["sim_notes"]
assert any("noflood 1" in m for m in fb7.log) and not fb7.errors
fb8 = QgsProcessingFeedback()
r8 = HazardClassification().run_mock(dict(SIM=r7["OUT"], CRIT=2, OUT_FOLDER=work), ctx, fb8)
t8 = list(ctx.layers[r8["OUT"]].getFeatures())
assert t8[0]["hz_class"] == "Low" and t8[0]["hz_conf"] == "no flood" and t8[0]["hz_reason"].startswith("no breach flood")
assert t8[0]["n_bldg_wet"] == 0 and t8[0]["hz_border"] == "no" and "natural ground" in t8[0]["hz_notes"]
assert any("Low 1" in m for m in fb8.log)
print("OK (tool 7: status noflood, no simulation; tool 8: Low, 'no flood')")

shutil.rmtree(work)
print("OK")
