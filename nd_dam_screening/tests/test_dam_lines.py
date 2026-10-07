"""Real-data check of the DWR dam-line layer (GeoPackage) against the DWR dam inventory.

Set DAM_LINES_GPKG and DAMS_DBF; skipped if either is missing.  Uses only sqlite3.
"""
import os, re, sqlite3, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))
sys.path.insert(0, os.path.dirname(__file__))

gp, dbf = os.environ.get("DAM_LINES_GPKG"), os.environ.get("DAMS_DBF")
if not (gp and dbf and os.path.exists(gp) and os.path.exists(dbf)):
    print("SKIP (set DAM_LINES_GPKG and DAMS_DBF)"); sys.exit(0)
from _dbf import read_dbf  # noqa: E402

c = sqlite3.connect(gp)
assert c.execute("select table_name from gpkg_contents where data_type='features'").fetchone()[0] == "dams_linear_features"
assert c.execute("select srs_id from gpkg_geometry_columns").fetchone()[0] == 2266     # ND South, US ft
ids = {int(r[0]) for r in c.execute("select structure_id from dams_linear_features")}
_, rows = read_dbf(dbf)
intact = [r for r in rows if r.get("status") == "Intact"]
hit = sum(int(r["structure_"]) in ids for r in intact)
print("intact dams with a line by structure ID: %d of %d" % (hit, len(intact)))
assert hit / len(intact) > 0.99
big = [r for r in intact if (r.get("max_dam_he") or 0) >= 25 or (r.get("normal_vol") or 0) >= 50]
assert all(int(r["structure_"]) in ids for r in big)                                  # every screened-size dam has a line
print("OK")
