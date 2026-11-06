"""2.35.11 (builder 1.4): two more barrier layers in the offline pack - the SARP inventory's
unsurveyed road-stream crossings (read from its national road-crossings download) and
USGS_stream_crossings (the USGS Database of Stream Crossings in the United States).
2.35.15 (packbuild 1.1): the unsurveyed crossings are ABI_road_crossings_unsurveyed, the shared
layer of both suites (damsuite.abi, engine kind "abi"; every column kept, names lower case);
SARP_unsurveyed_crossings is its alias.
Run: python3 -m ND_Dam_Hazard_Helper.tests.run_tests (from the folder above the plugin)."""
import os
import sys
import tempfile
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
from ND_Dam_Hazard_Helper.tests import fake_qgis  # noqa: E402
from ND_Dam_Hazard_Helper.tests.registry import test  # noqa: E402

fake_qgis.install()
from ND_Dam_Hazard_Helper.damsuite import gpkgpack as GP  # noqa: E402
from ND_Dam_Hazard_Helper.damsuite import offline_pack as OP  # noqa: E402
from ND_Dam_Hazard_Helper import build_nd_pack as B  # noqa: E402
from ND_Dam_Hazard_Helper import constants as C  # noqa: E402

HEAD = ('"lat","lon","Name","SARPID","Source","SourceID","CrossingCode","USGSCrossingID","River",'
        '"CrossingType","Surveyed","OnNetwork","Removed","Excluded","HUC8","HUC12","State",'
        '"County","SARP_Score"')
BOX = (-104.5, 45.5, -96.0, 49.5)


def _row(sarpid, lat, lon, state="North Dakota", surveyed="not likely", huc8="09020104"):
    return ('{},{},"","{}","USGS Database of Stream Crossings","x1","","3602476","Swan Creek",'
            '"culvert","{}","1","0","0","{}","090201040501","{}","Cass","0.5"').format(
        lat, lon, sarpid, surveyed, huc8, state)


def _csv_lines():
    return [HEAD,
            _row("cr1", 46.84, -97.15),                                  # kept
            _row("cr2", 46.85, -97.16, surveyed="yes"),                  # surveyed: not here
            _row("cr3", 46.86, -97.17, surveyed="likely"),               # surveyed: not here
            _row("cr4", 45.0, -96.5, state="South Dakota"),              # other state
            _row("cr5", 47.9, -97.0),                                    # kept
            _row("cr6", 46.0, -110.0)]                                   # ND label, outside box


@test
def both_crossing_layers_are_in_the_pack_under_barriers():
    from ND_Dam_Hazard_Helper.damsuite import abi as A
    table = {t[0]: t for t in B.datasets()}
    s = table["ABI_road_crossings_unsurveyed"]
    u = table["USGS_stream_crossings"]
    assert "SARP_unsurveyed_crossings" not in table
    assert s[2] == "barriers" and s[3] == "abi", s
    assert s[4]["mode"] == "zip" and A.url_of(s[4]).endswith("/downloads/national/road_crossings.zip")
    assert u[2] == "barriers" and u[3] == "arcgis", u
    assert u[4]["url"] == B.USGS_STREAM_CROSSINGS_URL == C.USGS_STREAM_CROSSINGS_URL
    assert "stream_crossings/FeatureServer/0" in u[4]["url"]
    assert u[4]["fields"].startswith("OBJECTID,")           # a crossing id repeats per road line
    assert "10.5066/P9YX6KTB" in u[4]["source"]
    for n in ("ABI_road_crossings_unsurveyed", "SARP_unsurveyed_crossings", "USGS_stream_crossings"):
        assert GP.group_of(n) == "barriers", n


def _zip(path):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("aquatic_barrier_ranks.csv", "\n".join(_csv_lines()) + "\n")
        z.writestr("README.txt", "Data version: 3.26.0\n")


@test
def unsurveyed_rows_of_north_dakota_inside_the_box_only():
    from ND_Dam_Hazard_Helper.damsuite import abi as A
    d = tempfile.mkdtemp()
    _zip(os.path.join(d, "road_crossings.zip"))

    def dl(url, dest, refresh, log, max_age=86400):
        return dest, False                                   # the file above, as it is
    head, rows, prov = A.abi_rows({"mode": "zip", "kind": "road_crossings"}, d, dl)
    feats = A.abi_features(head, rows, BOX)
    assert [f["properties"]["sarpid"] for f in feats] == ["cr1", "cr5"], feats
    p = feats[0]["properties"]
    assert feats[0]["geometry"] == {"type": "Point", "coordinates": [-97.15, 46.84]}
    assert p["surveyed"] == "not likely" and p["state"] == "North Dakota"
    assert p["huc8"] == "09020104"                           # leading zero: kept as text
    assert p["usgscrossingid"] == 3602476
    assert p["network"] == "on-network" and p["barrier_status"] == "present"
    assert p["sarp_score"] == 0.5                            # every column kept
    assert prov["data_version"] == "3.26.0"


@test
def fetch_reads_the_zip_into_the_pack():
    d = tempfile.mkdtemp()
    db = os.path.join(d, "work", "p.sqlite")
    dl = os.path.join(d, "work", "downloads", "abi")
    os.makedirs(dl)
    _zip(os.path.join(dl, "road_crossings.zip"))
    w = OP.PackWriter(db, BOX, created_by="t")
    logs = []
    ctx = B._Ctx(w, 1, logs.append, lambda p, m="": None, lambda t: None, False)
    w.begin("ABI_road_crossings_unsurveyed", "unsurveyed", "x")
    # a fresh download in the work folder is used as it is: no network here
    note = B.fetch_abi(ctx, BOX, {"mode": "zip", "kind": "road_crossings"})
    w.finish(note)
    w.close(finalize=True)
    assert "3.26.0" in note and "not likely" in note, note
    assert any("kept from an earlier run" in m for m in logs), logs
    r = OP.PackReader(db)
    assert r.info("ABI_road_crossings_unsurveyed")["count"] == 2
    # the old ND name answers
    assert r.info("SARP_unsurveyed_crossings")["count"] == 2


@test
def an_old_download_is_fetched_again():
    d = tempfile.mkdtemp()
    dest = os.path.join(d, "a.zip")
    with open(dest, "wb") as f:
        f.write(b"old")
    old = time.time() - B.ZIP_MAX_AGE_S - 60
    os.utime(dest, (old, old))
    calls = []

    class _R:
        status = 200
        headers = {"Content-Length": "3"}

        def __init__(self):
            self._b = [b"new", b""]

        def read(self, _n):
            return self._b.pop(0)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    real = B.urlopen
    B.urlopen = lambda req, timeout=None: calls.append(req.full_url) or _R()
    try:
        path, new = B._download_resume("http://example.invalid/a.zip", dest)
    finally:
        B.urlopen = real
    assert new and calls == ["http://example.invalid/a.zip"]
    with open(path, "rb") as f:
        assert f.read() == b"new"
    # a young file is used as it is
    B.urlopen = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no download"))
    try:
        assert B._download_resume("http://example.invalid/a.zip", dest) == (dest, False)
    finally:
        B.urlopen = real
