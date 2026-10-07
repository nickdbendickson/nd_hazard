"""Inventory logic checked against the REAL ND DWR 'Dams by Hazard Class' dBase file.

Set DAMS_DBF to the .dbf path; the real-data part is skipped if it is missing.
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))
sys.path.insert(0, os.path.dirname(__file__))
import inventory as inv  # noqa: E402

# ---- synthetic unit checks --------------------------------------------------
fld = dict(inv.DWR_DEFAULTS)
rec = {"structure_": 12345.0, "dam_name": " Test Dam ", "max_dam_he": 20.0, "normal_vol": 100.0,
       "max_pool_v": 0.0, "normal_are": 10.0, "hazard_cla": "Significant", "status": "Intact",
       "national_i": "nd00123", "dam_length": 0.0}
s = inv.standardize(rec.get, fld)
assert s["id"] == "12345" and s["name"] == "Test Dam" and s["nidref"] == "ND00123"
assert abs(s["h"] - 6.096) < 1e-9 and abs(s["ns"] - 123348.18375) < 1e-6
assert s["ms"] is None and s["len"] is None                       # zero = not recorded
assert inv.standardize(rec.get, fld, metric=True)["h"] == 20.0    # metric passthrough
assert inv.status_ok("Intact", inv.parse_status_list("Intact")) and \
    not inv.status_ok("Breached", inv.parse_status_list("intact, Under Construction"))
assert inv.status_ok("anything", inv.parse_status_list(""))
ft, af = inv.FT_M, inv.AF_M3
assert inv.size_skip(5 * ft, 10 * af, 15, 15)                     # both known, both below
assert not inv.size_skip(5 * ft, None, 15, 15)                    # unknown storage: never skipped
assert not inv.size_skip(30 * ft, 10 * af, 15, 15)                # tall enough
assert not inv.size_skip(5 * ft, 10 * af, 0, 0)                   # filter off
assert inv.shared_volume_flags([1e6, 1e6, 5e5, None, 100.0, 100.0]) == [True, True, False, False, False, False]

# ---- real data -----------------------------------------------------------------
path = os.environ.get("DAMS_DBF", "/home/claude/data/DamsbyHazardClass.dbf")
if not os.path.exists(path):
    print("real DBF not found: skipping real-data checks"); print("OK"); sys.exit(0)
from _dbf import read_dbf  # noqa: E402
_, rows = read_dbf(path)
recs = [inv.standardize(r.get, fld, idx=i) for i, r in enumerate(rows)]
keep = inv.parse_status_list("Intact")
kept = [a for a in recs if inv.status_ok(a["status"], keep)]
print("records %d -> intact %d" % (len(recs), len(kept)))
assert len(recs) == 10089 and len(kept) == 7662
assert len({a["id"] for a in recs}) == len(recs)                  # DWR ids are unique
gar = [a for a in recs if a["name"] == "Garrison Dam"][0]
print("Garrison: %.1f m high, %.3e m3 max storage, %.0f m long" % (gar["h"], gar["ms"], gar["len"]))
assert abs(gar["h"] - 202 * ft) < 1e-6 and 2.9e10 < gar["ms"] < 3.1e10       # ~24.2 million ac-ft

sk = [a for a in kept if inv.size_skip(a["h"], a["ns"] or a["ms"], 15, 15)]
print("size filter (<15 ft AND <15 ac-ft, both known): skips %d of %d intact" % (len(sk), len(kept)))
# independent recount straight from the raw feet / acre-feet columns
raw = [r for r in rows if (r["status"] or "").strip().lower() == "intact"]
def _v(r): return r["normal_vol"] or r["max_pool_v"]          # zero/None = not recorded
recount = [r for r in raw if r["max_dam_he"] and _v(r) and r["max_dam_he"] < 15 and _v(r) < 15]
assert len(sk) == len(recount), (len(sk), len(recount))
unknown = [r for r in raw if not r["max_dam_he"] or not _v(r)]
assert not any(inv.size_skip(a["h"], a["ns"] or a["ms"], 15, 15) for a in kept
               if a["h"] is None or (a["ns"] or a["ms"]) is None)
print("intact dams with height or storage unknown (never skipped): %d" % len(unknown))
sh = inv.shared_volume_flags([a["ms"] or a["ns"] for a in kept])
names = [a["name"] for a, f in zip(kept, sh) if f]
print("shared-volume flags among intact: %d  e.g. %s" % (sum(sh), names[:4]))
assert any("Reach Embankment" in n for n in names)
nid_ids = [a["nidref"] for a in kept if a["nidref"]]
print("intact with NID id: %d, unique %d, sample %s" % (len(nid_ids), len(set(nid_ids)), nid_ids[:3]))
print("OK")
