"""Runs the QGIS processing tools 0 and 5-10 end to end against test doubles of
qgis.core and GDAL (tests/qgis_mock) on a synthetic valley with three dams.

This checks the tools' own logic and data flow (parameters, fields, triage ->
simulation -> classification -> reports, sensitivity chaining).  It cannot check
that the real QGIS/GDAL APIs behave like the doubles; that needs a run in QGIS.
"""
import os, sys, tempfile, shutil
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))          # parent of the plugin package
from qgis.core import (QgsGeometry, QgsPointXY, QgsProcessingContext, QgsProcessingFeedback,  # noqa: E402
                       make_layer, QgsVectorFileWriter, QgsApplication)
from qgis.PyQt.QtCore import QVariant  # noqa: E402
from qgis import processing  # noqa: E402
from osgeo import gdal  # noqa: E402
import nd_dam_screening as pkg  # noqa: E402
from nd_dam_screening.core import breach_hydro as bh  # noqa: E402
from nd_dam_screening.plugin import NDDamScreeningPlugin  # noqa: E402

D, S, I = "double", "string", "int"
work = tempfile.mkdtemp(prefix="qgt_")
demdir, curves = os.path.join(work, "dem"), os.path.join(work, "curves")
os.makedirs(demdir); os.makedirs(curves)

# ---------------- provider loads all tools
plug = pkg.classFactory(None); plug.initGui()
algs = {a.name(): a for a in plug.provider.algs}
for a in algs.values():
    a.initAlgorithm()
processing.REGISTRY.update(algs)
print("tools:", ", ".join(sorted(a.displayName() for a in algs.values())))
assert len(algs) == 23                      # 0-10, 7b, 11-18, 19 (propose a dam, 0.14), 20 (download a DEM)
assert "getdamdata" in algs                 # 0.16.0: + Get data for this dam (the suite's shared tool)
# 0.16.0: tools that can name one dam fill empty data inputs from the dam folder (data_aware
# outside settings_aware: _nd_original stays the class as written)
assert algs["preparedams"]._nd_data_mapping == {"DWR": "dwr_dams", "NID": "nid_dams"}
assert algs["findponds"]._nd_data_mapping == {"WB": "waterbodies"}
assert type(algs["findponds"])._nd_original.__name__ == "FindPonds" and "_nd_data_aware" not in vars(
    type(algs["findponds"])._nd_original)

# ---------------- synthetic world, 10 m grid, EPSG:5070-like coordinates
cell, NY, NX = 10.0, 520, 260
X0, YT = 400000.0, 1800000.0
yy, xx = np.mgrid[0:NY, 0:NX]
Z = 300.0 - 0.003 * yy * cell + 0.0009 * np.abs((xx - NX / 2) * cell) ** 1.5
xy = lambda r, c: (X0 + (c + 0.5) * cell, YT - (r + 0.5) * cell)
Zd = Z.copy()
def embank(row, h, c0=0, c1=NX):
    zc = Z[row, NX // 2] + h
    Zd[row - 1:row + 2, c0:c1] = np.maximum(Zd[row - 1:row + 2, c0:c1], zc); return zc
crest1 = embank(80, 7.0); crest2 = embank(300, 2.5)
road_row = 230; Zd[road_row - 1:road_row + 2, :] = np.maximum(Zd[road_row - 1:road_row + 2, :], Z[road_row, NX // 2] + 3.0)
pool1 = crest1 - 2.0
Zd = np.where((yy < 79) & (Zd < pool1), pool1, Zd)                  # hydro-flattened pool
# dam 3: small pond on the east valley wall, draining west
r3, c3 = 60, 215
crest3 = Z[r3, c3] + 2.0
Zd[r3 - 3:r3 + 4, c3 - 1:c3 + 2] = np.maximum(Zd[r3 - 3:r3 + 4, c3 - 1:c3 + 2], crest3)


def clip(dam_id, r, c, half=120):
    a = Zd[max(r - half, 0):r + half, max(c - half, 0):c + half]
    x0 = X0 + max(c - half, 0) * cell; y1 = YT - max(r - half, 0) * cell
    ds = gdal.GetDriverByName("GTiff").Create(os.path.join(demdir, f"dam_{dam_id}.tif"), a.shape[1], a.shape[0], 1, 6)
    ds.SetGeoTransform((x0, cell, 0, y1, 0, -cell)); ds.GetRasterBand(1).WriteArray(a); ds.FlushCache()


# a big regional DEM for the "local folder" terrain option (clip growth)
regional = os.path.join(work, "regional"); os.makedirs(regional)
ds = gdal.GetDriverByName("GTiff").Create(os.path.join(regional, "nd.tif"), NX, NY, 1, 6)
ds.SetGeoTransform((X0, cell, 0, YT, 0, -cell)); ds.GetRasterBand(1).WriteArray(Zd); ds.FlushCache()


def curve(dam_id, mask_fn, zmin, zmax):
    el = np.linspace(zmin, zmax, 20)
    v = [float(((e - Z) * mask_fn(e)).clip(min=0).sum() * cell * cell) for e in el]
    with open(os.path.join(curves, f"curve_{dam_id}.csv"), "w") as f:
        f.write("elev_m,storage_m3\n")
        for e, vv in zip(el, v): f.write(f"{e:.3f},{vv:.1f}\n")


curve("DWR-1", lambda e: (yy < 79) & (Z < e), Z[80, NX // 2], crest1 + 0.5)
curve("DWR-2", lambda e: (yy > 230) & (yy < 299) & (Z < e), Z[300, NX // 2], crest2 + 0.5)
with open(os.path.join(curves, "curve_DWR-3.csv"), "w") as f:
    f.write("elev_m,storage_m3\n" + "".join(f"{Z[r3, c3] + k * 0.25:.3f},{800 * k * 0.25:.1f}\n" for k in range(10)))
clip("DWR-1", 80, NX // 2); clip("DWR-2", 300, NX // 2); clip("DWR-3", r3, c3)

# ---------------- layers as tools 3 and 4 would leave them
F3 = [("dam_id", S), ("name", S), ("height_m", D), ("est_pool_m", D), ("est_crest_m", D), ("est_maxdep_m", D),
      ("stor_conf", S), ("stor_method", S), ("crest_len_m", D), ("haz_existing", S), ("status", S)]
dams3 = make_layer(F3, [
    (QgsGeometry.fromPointXY(QgsPointXY(*xy(80, NX // 2))), dict(dam_id="DWR-1", name="Upper Dam", height_m=7.0,
     est_pool_m=pool1, est_crest_m=crest1, est_maxdep_m=5.0, stor_conf="medium", stor_method="inventory",
     crest_len_m=600.0, haz_existing="Low", status="Intact")),
    (QgsGeometry.fromPointXY(QgsPointXY(*xy(300, NX // 2))), dict(dam_id="DWR-2", name="Lower Dam", height_m=2.5,
     est_pool_m=crest2 - 1.0, est_crest_m=crest2, est_maxdep_m=1.5, stor_conf="medium", stor_method="estimated",
     crest_len_m=500.0, haz_existing="Significant", status="Intact")),
    (QgsGeometry.fromPointXY(QgsPointXY(*xy(r3, c3))), dict(dam_id="DWR-3", name="Stock Pond", height_m=2.0,
     est_pool_m=crest3 - 0.5, est_crest_m=crest3, est_maxdep_m=1.5, stor_conf="low", stor_method="estimated",
     crest_len_m=60.0, haz_existing="Low", status="Intact"))], "point", "dams3")
FB = [("dam_id", S), ("flow_az", D), ("crest_len_m", D), ("dir_conf", S)]
bpts = make_layer(FB, [
    (QgsGeometry.fromPointXY(QgsPointXY(*xy(81, NX // 2))), dict(dam_id="DWR-1", flow_az=180.0, crest_len_m=600.0, dir_conf="high")),
    (QgsGeometry.fromPointXY(QgsPointXY(*xy(301, NX // 2))), dict(dam_id="DWR-2", flow_az=180.0, crest_len_m=500.0, dir_conf="high")),
    (QgsGeometry.fromPointXY(QgsPointXY(*xy(r3, c3 - 2))), dict(dam_id="DWR-3", flow_az=270.0, crest_len_m=60.0, dir_conf="low"))],
    "point", "bpts")
line = lambda pts: QgsGeometry.fromPolylineXY([QgsPointXY(*p) for p in pts])
damlines = make_layer([("structure_id", I)], [
    (line([xy(80, 5), xy(80, NX - 6)]), {"structure_id": 1}),
    (line([xy(300, 5), xy(300, NX - 6)]), {"structure_id": 2}),
    (line([xy(r3 - 3, c3), xy(r3 + 3, c3)]), {"structure_id": 3})], "line", "damlines")
streams = make_layer([("gnis_name", S)], [(line([xy(0, NX // 2), xy(NY - 1, NX // 2)]), {"gnis_name": "Test Creek"})],
                     "line", "streams")
pt = lambda r, c: QgsGeometry.fromPointXY(QgsPointXY(*xy(r, c)))
FS = [("OCC_CLS", S), ("PRIM_OCC", S)]
bld = make_layer(FS, [(pt(180, NX // 2 + 3), {"OCC_CLS": "Residential", "PRIM_OCC": "Single Family Dwelling"}),
                      (pt(190, NX // 2 - 6), {"OCC_CLS": "Residential", "PRIM_OCC": "Manufactured Home"}),
                      (pt(185, NX // 2 + 2), {"OCC_CLS": "Agriculture", "PRIM_OCC": "Barn"}),
                      (pt(180, 10), {"OCC_CLS": "Residential", "PRIM_OCC": "Single Family Dwelling"})],
                 "point", "structures")
roads = make_layer([("AADT_2023", D), ("ROUTE", S)], [(line([xy(road_row, 0), xy(road_row, NX - 1)]),
                    {"AADT_2023": 800.0, "ROUTE": "County road"})], "line", "roads")
rail = make_layer([("PASSNGR", S), ("RROWNER1", S)], [(line([xy(470, 0), xy(470, NX - 1)]),
                   {"PASSNGR": "B", "RROWNER1": "BNSF"})], "line", "rail")

ctx, fb = QgsProcessingContext(), QgsProcessingFeedback()
lay = lambda src: ctx.add(src)
DAMS3, BPTS, DAMLINES, STREAMS, BLD, ROADS, RAIL = map(lay, (dams3, bpts, damlines, streams, bld, roads, rail))


def run(name, **params):
    r = algs[name].createInstance().run_mock(params, ctx, fb)
    return r, {k: ctx.layers.get(v) for k, v in r.items() if isinstance(v, str) and v in ctx.layers}


def rows(src):
    n = src.fields().names()
    return {f["dam_id"]: {k: f[k] for k in n} for f in src.getFeatures()}


# ---------------- tool 5
_, o5 = run("breachhydrograph", DAMS=DAMS3, CURVES=curves, BPTS=BPTS, MODE=0, METHOD=0, SUNNY=False,
            EXP=2.0, OUTDIR=os.path.join(work, "hydro"))
t5 = rows(o5["OUT"])
for k, r in t5.items():
    print(f"tool 5 {k}: Qp {r['td_qp_m3s'] or 0:.1f} m3/s, store {r['store_af'] or 0:.0f} ac-ft, conf {r['brch_conf']}, eq {r['brch_eq']}")
assert all(r["td_csv"] and os.path.exists(r["td_csv"]) for r in t5.values())
T5 = lay(o5["OUT"])

# ---------------- tool 6 triage
_, o6 = run("triage", DAMS=T5, BPTS=BPTS, DEMDIR=demdir, BLD=BLD, OCC="OCC_CLS", ROADS=ROADS,
            ROAD_AADT="AADT_2023", ROAD_CLASS="ROUTE", RAIL=RAIL, STREAMS=STREAMS, DAMLINES=DAMLINES,
            AUDIT=0.0, OUTDIR=os.path.join(work, "triage"))
t6 = rows(o6["OUT"])
for k, r in t6.items():
    print(f"tool 6 {k}: {r['tri_result']} - {r['tri_reason']}")
assert t6["DWR-1"]["tri_result"] == "needs 2D" and t6["DWR-3"]["tri_result"] == "clear"
T6 = lay(o6["OUT"])
# a lifeline facility, or another dam, inside DWR-3's envelope keeps it from 'clear' (ND 5.2.2, 5.3)
env = gdal.Open(t6["DWR-3"]["tri_tif"]); ea = env.GetRasterBand(1).ReadAsArray(); eg = env.GetGeoTransform()
rr, cc = [int(k[0]) for k in np.nonzero(ea > 0)]
wet_xy = (eg[0] + (cc + 0.5) * eg[1], eg[3] + (rr + 0.5) * eg[5])
life = make_layer([("kind", S)], [(QgsGeometry.fromMultiPointXY([QgsPointXY(*wet_xy)]), {"kind": "pump station"})],
                  "point", "lifelines")
_, o6b = run("triage", DAMS=T5, BPTS=BPTS, DEMDIR=demdir, LIFELINES=lay(life), AUDIT=0.0,
             OUTDIR=os.path.join(work, "triage_b"))
r3 = rows(o6b["OUT"])["DWR-3"]
assert r3["tri_result"] == "needs 2D" and "lifeline" in r3["tri_reason"], r3["tri_reason"]
F5 = [(fl.name(), fl._t) for fl in o5["OUT"].fields()]                 # every tool-5 field (test double)
extra = make_layer(F5, [(f.geometry(), {k: f[k] for k, _ in F5}) for f in o5["OUT"].getFeatures()] +
                   [(QgsGeometry.fromPointXY(QgsPointXY(*wet_xy)), {"dam_id": "DWR-99", "name": "Downstream pond"})],
                   "point", "dams_plus")
_, o6c = run("triage", DAMS=lay(extra), BPTS=BPTS, DEMDIR=demdir, AUDIT=0.0, OUTDIR=os.path.join(work, "triage_c"))
r3 = rows(o6c["OUT"])["DWR-3"]
assert r3["tri_result"] == "needs 2D" and "dam" in r3["tri_reason"], r3["tri_reason"]
print("tool 6: a lifeline or another dam inside the envelope prevents 'clear'")

# ---------------- tool 7 built-in engine, 2 at a time, clip growth from the local folder, cascade breach
_, o7 = run("runbreachsim", DAMS=T6, BPTS=BPTS, DEMDIR=demdir, TSRC=2, LOCAL=regional, ENGINE=2,
            SCEN=0, CURVES=curves, CELL=20.0, SIMH=0.0, SIMMAX=8.0, CORR=True, CORRW=300.0, RMAX=3000.0,
            STREAMS=STREAMS, DAMLINES=DAMLINES, LID="structure_id", CASCADE=2, NDEF=0.05, NMULT=1.0,
            WORKERS=2, TMO=30.0, SKIPTRI=True, KEEP=False, OUTDIR=os.path.join(work, "sim"))
t7 = rows(o7["OUT"])
for k, r in t7.items():
    print(f"tool 7 {k}: {r['sim_status']}, depth {r['max_depth_m']}, attempts {r['attempts']}, radius {r['radius_m']}, "
          f"cascade '{r['cascade']}', notes '{r['sim_notes']}'")
assert t7["DWR-3"]["sim_status"] == "triage"
assert t7["DWR-1"]["sim_status"] == "ok" and "DWR-2 overtopped" in (t7["DWR-1"]["cascade"] or "")
assert all(os.path.exists(t7["DWR-1"][k]) for k in ("depth_tif", "vel_tif", "dv_tif", "arrive_tif"))
T7 = lay(o7["OUT"])

# resume: a second run reads the rasters instead of re-running
_, o7b = run("runbreachsim", DAMS=T6, BPTS=BPTS, DEMDIR=demdir, ENGINE=2, CELL=20.0, WORKERS=1,
             OUTDIR=os.path.join(work, "sim"))
assert rows(o7b["OUT"])["DWR-1"]["sim_status"] == "cached"

# ---------------- tool 8 with the FEMA / Feinberg criterion (comparison only; enum index 3 since v0.8)
_, o8 = run("hazardclassification", SIM=T7, BLD=BLD, ROADS=ROADS, ROAD_AADT="AADT_2023", ROAD_CLASS="ROUTE",
            RAIL=RAIL, CRIT=3)
t8 = rows(o8["OUT"])
imp = list(o8["IMP"].getFeatures())
for k, r in t8.items():
    print(f"tool 8 {k}: {r['hz_class']} ({r['hz_conf']}) {r['hz_reason']} | vs DWR: {r['hz_vs_existing']}")
print("   impacted:", [(f["kind"], round(f["depth_m"], 2), None if f["dv_m2s"] is None else round(f["dv_m2s"], 2), f["lethal"], f["level"]) for f in imp])
assert t8["DWR-3"]["hz_class"] == "Low" and t8["DWR-3"]["hz_conf"] == "triage"
assert t8["DWR-1"]["hz_class"] in ("Medium", "High") and t8["DWR-1"]["n_bldg_wet"] >= 1
assert not any(f["kind"] == "structure" and abs(f.geometry().asPoint().x() - xy(180, 10)[0]) < 1 for f in imp)
assert all(f["kind"] != "Barn" for f in imp)
assert t8["DWR-1"]["hz_criterion"] == "feinberg" and t8["DWR-1"]["hz_basis"].startswith("Comparison only")
assert t8["DWR-1"]["hz_chart"] == "not used" and t8["DWR-1"]["hz_border"] in ("yes", "no")
T8, IMP = lay(o8["OUT"]), lay(o8["IMP"])
# v0.9: with a Dams folder, the dams that HAVE a folder get a 'screened' status in dam.json
from nd_dam_screening.damsuite import dam_record as dr  # noqa: E402
dams_dir = os.path.join(work, "Dams"); os.makedirs(os.path.join(dams_dir, "DWR-1"))
run("hazardclassification", SIM=T7, BLD=BLD, ROADS=ROADS, ROAD_AADT="AADT_2023", ROAD_CLASS="ROUTE",
    RAIL=RAIL, CRIT=3, DAMS_FOLDER=dams_dir)
rec8 = dr.load(os.path.join(dams_dir, "DWR-1"))
res8 = rec8.latest("screened")["result"]
assert res8["hz_class"] == t8["DWR-1"]["hz_class"] and res8["by"] == "tool 8" and res8["triage"]
assert rec8.fields["dam_id"] == "DWR-1" and rec8.fields["dwr_structure_id"] == "1"
assert sorted(os.listdir(dams_dir)) == ["DWR-1"]                  # no folder is created
print("tool 8 dam record:", res8)

# ---------------- tool 10 reports
r10, _ = run("reports", HAZ=T8, IMP=IMP, BPTS=BPTS, DEMDIR=demdir, WHICH=1, OUTDIR=os.path.join(work, "report"))
pages = os.listdir(os.path.join(work, "report", "dams"))
print("tool 10:", r10["INDEX"], "pages:", pages)
assert os.path.exists(r10["INDEX"]) and "DWR-1.html" in pages

# ---------------- tool 0 data package
r0, _ = run("datapackage", FLOW=STREAMS, DAML=DAMLINES, BLD=BLD, ROADS=ROADS, AADT="AADT_2023", RCLS="ROUTE",
            RAIL=RAIL, OUT=os.path.join(work, "nd.gpkg"))
w = {k[1]: v for k, v in QgsVectorFileWriter.written.items() if k[0] == r0["OUT"]}
print("tool 0 layers:", {k: len(v.feats) for k, v in w.items()})
sv = [f.attributes() for f in w["structures"].feats]
assert [a[0] for a in sv] == [1, 1, 0, 1] and [a[1] for a in sv] == [0, 1, 0, 0]
assert w["roads"].feats[0].attributes() == [800.0, "County road", "Medium"]
assert w["railroads"].feats[0].attributes()[0] == 1 and w["railroads"].feats[0].attributes()[2] == "High"

# ---------------- tool 9 sensitivity (base + Manning x1.25 for speed)
_, o9 = run("sensitivity", DAMS=DAMS3, CURVES=curves, BPTS=BPTS, DEMDIR=demdir, VARS=[0, 5], ENGINE=2,
            WORKERS=2, DAMLINES=DAMLINES, BLD=BLD, ROADS=ROADS, RAIL=RAIL, CRIT=1,   # ND 1D 2 ft rule
            OUTDIR=os.path.join(work, "sens"))
t9 = rows(o9["OUT"])
for k, r in t9.items():
    print(f"tool 9 {k}: runs {r['sens_runs']}, {r['class_min']}..{r['class_max']}, stable {r['class_stable']} | {r['sens_detail']}")
assert t9["DWR-1"]["sens_runs"] == 2 and t9["DWR-1"]["class_stable"] in ("yes", "no")
print("errors reported by tools:", fb.errors)
shutil.rmtree(work)
print("OK")
