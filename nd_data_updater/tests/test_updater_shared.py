"""1.8.0 (suite 1.18.8): ONE data updater engine and source list for both suites. core.py,
nsi_merge.py and osm_layers.py are shims: after the import they ARE damsuite/updatecore.py,
nsi_merge.py and osm_layers.py, the same modules DamSafety Data uses; sources.py takes the list
from damsuite/updatesources.py and adds this plugin's offline pack source."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
from nd_data_updater.tests.registry import test  # noqa: E402

from nd_data_updater import core, nsi_merge, osm_layers, sources  # noqa: E402
from nd_data_updater.damsuite import updatecore as UC  # noqa: E402
from nd_data_updater.damsuite import updatesources as US  # noqa: E402
from nd_data_updater.packsource import PackSource  # noqa: E402


@test
def test_the_shims_are_the_shared_modules():
    from nd_data_updater.damsuite import nsi_merge as NM, osm_layers as OL
    assert core is UC and nsi_merge is NM and osm_layers is OL


@test
def test_one_source_list_plus_the_pack():
    assert list(sources.SOURCE_INFO) == ["nsi", "microsoft", "osm", "osm_layers",
                                         "usa_structures", "overture", "address_points", "pack",
                                         "nsi_generator"]
    for sid, info in US.SOURCE_INFO.items():
        assert sources.SOURCE_INFO[sid] == info, sid
    got = {s.id: s for s in sources.build_sources({"data_dir": "/tmp/x"})}
    # the defaults: DamSafety's address points now here too, Overture off as there
    assert list(got) == ["nsi", "microsoft", "osm", "osm_layers", "address_points", "pack"]
    assert isinstance(got["pack"], PackSource) and isinstance(
        got["address_points"], core.ArcGISFeatureSource)
    got = {s.id: s for s in sources.build_sources({"enabled": {"overture": True}})}
    assert isinstance(got["overture"], core.OvertureSource)
