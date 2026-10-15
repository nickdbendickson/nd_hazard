"""Tests of 3.14.0 (damsuite 1.10.0): the offline pack as a folder with one GeoPackage per layer,
the shared data source (offline pack or download) and pack location, and the Runner's DWR layers
(wizard dam IDs, suite layer list) taken from the pack."""
import os
import shutil
import tempfile

from nd_breach_runner.tests.registry import test


class _Env:
    """Set (value) or remove (None) environment variables for the block."""

    def __init__(self, **kw):
        self.kw = kw

    def __enter__(self):
        self.old = {k: os.environ.get(k) for k in self.kw}
        for k, v in self.kw.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _pt(x, y, **props):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [x, y]},
            "properties": props}


def _line(x0, y0, x1, y1, **props):
    return {"type": "Feature", "geometry": {"type": "LineString",
                                            "coordinates": [[x0, y0], [x1, y1]]},
            "properties": props}


def _pack(root):
    from nd_breach_runner.damsuite import gpkgpack as GP
    from nd_breach_runner.damsuite import offline_pack as OP
    folder = os.path.join(root, GP.PACK_FOLDER)
    m = GP.new_manifest(OP.ND_BBOX, created_by="test_v3140")
    GP.add_dataset(folder, m, "DWR_dams",
                   lambda: [_pt(-98.0, 47.0, structure_id=5289, dam_name="Elm River Dam 1"),
                    _pt(-97.5, 47.5, structure_id=77, dam_name="")],
                   {"label": "DWR dams", "source": "ND DWR export (test)", "status": "complete"})
    GP.add_dataset(folder, m, "DWR_dam_lines",
                   lambda: [_line(-98.01, 47.0, -97.99, 47.0, structure_id=5289)],
                   {"label": "DWR dam lines", "source": "ND DWR export (test)",
                    "status": "complete"})
    GP.write_manifest(folder, m)
    return folder


@test
def v3140_folder_pack_reads_like_the_old_pack():
    from nd_breach_runner.damsuite import gpkgpack as GP
    from nd_breach_runner.damsuite import offline_pack as OP
    from nd_breach_runner.damsuite import suitegpkg as G
    root = tempfile.mkdtemp(prefix="v3140_")
    try:
        folder = _pack(root)
        r = OP.open_pack(folder)
        assert isinstance(r, GP.GpkgPackReader) and r.has("DWR_dams")
        assert r.path("DWR_dams") == os.path.join(folder, "dwr", "DWR_dams.gpkg")
        got = r.query("DWR_dams", (-98.1, 46.9, -97.9, 47.1))
        assert len(got) == 1 and got[0]["properties"]["structure_id"] == 5289
        assert r.query("DWR_dams", OP.ND_BBOX)[1]["properties"]["dam_name"] == ""
        lays = G.list_layers(r.path("DWR_dam_lines"))
        assert [x["name"] for x in lays] == ["DWR_dam_lines"] and lays[0]["srs_id"] == 4326
        txt = open(os.path.join(folder, GP.LAYERS_TXT), encoding="utf-8").read()
        assert "dwr/DWR_dams.gpkg" in txt and "ND DWR export (test)" in txt
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def v3140_dwr_layers_come_from_the_pack_unless_download():
    from nd_breach_runner.damsuite import suitedata as SD
    root = tempfile.mkdtemp(prefix="v3140_")
    try:
        data = os.path.join(root, "suite_data")
        os.makedirs(data)
        folder = _pack(root)
        with _Env(NDSUITE_DATA=data, NDSUITE_PACK=folder, NDSUITE_SOURCE="pack"):
            assert SD.data_source() == SD.SOURCE_PACK and SD.active_pack(data) == folder
            d = SD.dwr_layers(data)
            assert d["dwr_dams"] == os.path.join(folder, "dwr", "DWR_dams.gpkg"), d
            assert d["dwr_dam_lines"].endswith("DWR_dam_lines.gpkg") and d["dwr_dugouts"] == ""
            st = SD.status(data)
            assert st["pack_layout"] == "gpkg" and "one GeoPackage per layer" in "\n".join(SD.describe(st))
            assert SD.describe(st)[0].startswith("Data source: ")
        with _Env(NDSUITE_DATA=data, NDSUITE_PACK=folder, NDSUITE_SOURCE="download"):
            assert SD.active_pack(data) == "" and SD.dwr_layers(data)["dwr_dams"] == ""
        # no pack location: the pack inside the suite data folder is found
        moved = os.path.join(data, os.path.basename(folder))
        shutil.move(folder, moved)
        with _Env(NDSUITE_DATA=data, NDSUITE_PACK=None, NDSUITE_SOURCE=None):
            assert SD.active_pack(data) == moved
            assert SD.dwr_layers(data)["dwr_dams"].startswith(moved)
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def v3140_suite_layer_list_offers_the_pack_dwr_layers():
    from nd_breach_runner import suite_fill as SF
    root = tempfile.mkdtemp(prefix="v3140_")
    try:
        data = os.path.join(root, "suite_data")
        dam = os.path.join(root, "dams", "5289")
        os.makedirs(data)
        os.makedirs(dam)
        folder = _pack(root)
        with _Env(NDSUITE_DATA=data, NDSUITE_PACK=folder, NDSUITE_SOURCE="pack"):
            paths = [p for _n, p, _k, _o in SF.layer_list(dam, data)]
            assert os.path.join(folder, "dwr", "DWR_dams.gpkg") in paths, paths
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def v3140_versions():
    import os
    from nd_breach_runner.core import workflow as wf
    from nd_breach_runner.damsuite import SUITE_VERSION
    from nd_breach_runner.damsuite import versions as V
    assert wf.VERSION == wf.metadata_version() and SUITE_VERSION >= "1.10.0"
    assert V.RELEASE["ndbreach"] == wf.VERSION
    # the sibling plugins' metadata.txt, when the release tree is next to this plugin.
    # 3.20.0: the shared table is written first and each plugin's own metadata.txt follows when
    # its author bumps it, so a sibling BEHIND the table is a release in progress, not a fault.
    # A sibling AHEAD of the table is the fault this guards: then the shared table is stale and
    # version checks between the plugins would pass something that is not released.
    def _ver(text):
        return tuple(int(p) if p.isdigit() else 0 for p in str(text).split("."))
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for key, folder in (("ndhazard", "ND_Dam_Hazard_Helper"), ("ndscreen", "nd_dam_screening")):
        meta = os.path.join(os.path.dirname(here), folder, "metadata.txt")
        if os.path.isfile(meta):
            got = [l.split("=", 1)[1].strip() for l in open(meta, encoding="utf-8-sig") if l.startswith("version=")]
            assert got, (key, meta)
            assert _ver(got[0]) <= _ver(V.RELEASE[key]), (
                "{}: metadata.txt {} is ahead of the shared release table {}".format(
                    key, got[0], V.RELEASE[key]))


@test
def v3140_land_cover_goes_in_as_a_geopackage_raster_with_the_same_values():
    try:
        from osgeo import gdal
        import numpy as np
    except Exception:                                            # noqa - no GDAL: nothing to test
        return
    if not hasattr(gdal, "Translate"):
        return
    from nd_breach_runner.damsuite import gpkgpack as GP
    from nd_breach_runner.damsuite import offline_pack as OP
    root = tempfile.mkdtemp(prefix="v3140_")
    try:
        tif = os.path.join(root, "nlcd.tif")
        codes = np.array([[11, 21, 0], [41, 82, 95]], dtype=np.uint8)
        ds = gdal.GetDriverByName("GTiff").Create(tif, 3, 2, 1, gdal.GDT_Byte)
        ds.SetGeoTransform((-645015.0, 30.0, 0.0, 2940015.0, 0.0, -30.0))
        ds.SetProjection(_wkt_5070())
        ds.GetRasterBand(1).WriteArray(codes)
        ds = None
        folder = os.path.join(root, GP.PACK_FOLDER)
        m = GP.new_manifest(OP.ND_BBOX)
        e = GP.add_raster_file(folder, m, "NLCD_2021_landcover", tif, {"label": "NLCD"})
        assert e["file"] == "landcover/NLCD_2021_landcover.gpkg", e
        out = gdal.Open(os.path.join(folder, "landcover", "NLCD_2021_landcover.gpkg"))
        assert out.GetDriver().ShortName == "GPKG"
        assert out.GetGeoTransform() == (-645015.0, 30.0, 0.0, 2940015.0, 0.0, -30.0)
        assert (out.ReadAsArray() == codes).all()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _wkt_5070():
    from osgeo import osr
    s = osr.SpatialReference()
    s.ImportFromEPSG(5070)
    return s.ExportToWkt()
