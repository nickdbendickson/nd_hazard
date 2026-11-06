"""2.35.14 (suite 1.18.6, pin v4): ONE offline pack builder for both suites. The Helper's
build_nd_pack.py / packfiles.py and the Updater's ndpack/ copies are shims over the shared
damsuite/packengine.py / packfiles.py, bound to the ND constants (PACK_LAYERS = "nd").
Run: python3 -m ND_Dam_Hazard_Helper.tests.run_tests (from the folder above the plugin)."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
from ND_Dam_Hazard_Helper.tests.registry import test  # noqa: E402

from ND_Dam_Hazard_Helper import build_nd_pack as B          # noqa: E402
from ND_Dam_Hazard_Helper import packfiles as PFS            # noqa: E402
from ND_Dam_Hazard_Helper import constants as C              # noqa: E402
from ND_Dam_Hazard_Helper.damsuite import packengine as E    # noqa: E402
from ND_Dam_Hazard_Helper.damsuite import packfiles as PF    # noqa: E402


@test
def helper_builder_is_the_shared_engine_with_the_nd_layers():
    assert B is E and PFS is PF
    assert E.C.__dict__["_mod"] is C and E.pack_layers() == "nd" == C.PACK_LAYERS
    names = [t[0] for t in E.datasets()]
    for n in ("3DHP_flowlines", "3DHP_waterbodies", "ABI_dams", "ABI_small_barriers_surveyed",
              "ABI_road_crossings_unsurveyed", "USGS_stream_crossings", "NDDEQ_landfills",
              E.NLCD_NAME):
        assert n in names, (n, names)
    assert not [n for n in names if n.startswith(("NHD_", "SARP_"))], names
    # 2.35.15 (packbuild 1.1): the barrier and ND DEQ layers are the same entries as DamSafety's
    dwr = {t[0]: t for t in E.datasets(layers="dwr")}
    for t in E.shared_datasets():
        assert t in E.datasets() and dwr[t[0]] == t, t[0]
    table = E.datasets()
    assert E._pick("nhd", table) == {"hydro"} and E._pick("sarp", table) == {"barriers"}
    # the DamSafety additions are there for both suites
    for f in ("fetch_arcgis_tiled", "_page_400", "_page_anchor", "fetch_local_gpkg",
              "download_dwr_shapefiles", "fetch_abi", "fetch_arcgis_tiles", "_add_rasters"):
        assert callable(getattr(E, f)), f


@test
def updater_builder_is_the_same_engine():
    from nd_data_updater.ndpack import build_nd_pack as UB
    from nd_data_updater.ndpack import packfiles as UPF
    from nd_data_updater.ndpack import constants as UC
    from nd_data_updater.damsuite import packengine as UE
    assert UB is UE and UPF.__name__ == "nd_data_updater.damsuite.packfiles"
    assert UE.C.__dict__["_mod"] is UC and UE.pack_layers() == "nd"
    root = os.path.dirname(PLUGIN_DIR)
    with open(os.path.join(root, "ND_Dam_Hazard_Helper", "damsuite", "packengine.py"), "rb") as a, \
            open(os.path.join(root, "nd_data_updater", "damsuite", "packengine.py"), "rb") as b:
        assert a.read() == b.read()
    # the Updater's copies of the Helper's constants / osm_parse stay byte-identical
    for name in ("constants.py", "osm_parse.py"):
        with open(os.path.join(root, "ND_Dam_Hazard_Helper", name), "rb") as a, \
                open(os.path.join(root, "nd_data_updater", "ndpack", name), "rb") as b:
            assert a.read() == b.read(), name


@test
def nd_build_writes_a_folder_pack():
    import tempfile
    d = tempfile.mkdtemp()
    calls = []

    def fake_run_one(ctx, name, label, kind, spec, bbox):
        calls.append(name)
        ctx.writer.begin(name, label, "x", resume=False, signature=kind)
        ctx.writer.add([{"type": "Feature", "geometry": {"type": "Point",
                                                         "coordinates": [-100.0, 47.0]},
                         "properties": {"OBJECTID": 1}, "id": 1}], E._uid_arcgis("OBJECTID"))
        return ctx.writer.finish("")
    real = E._run_one
    E._run_one = fake_run_one
    try:
        man = E.build(os.path.join(d, "pack"), only="NID_dams", log=lambda m: None)
    finally:
        E._run_one = real
    assert calls == ["NID_dams"], calls
    assert os.path.isfile(os.path.join(d, "pack", "manifest.json")), os.listdir(d)
    assert not os.path.exists(os.path.join(d, "pack.zip"))
    assert man["datasets"]["NID_dams"]["count"] == 1, man["datasets"]
