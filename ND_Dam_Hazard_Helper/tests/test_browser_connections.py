"""2.35.13 (suite pin v3): the suite's map services become QGIS Browser connections when a plugin
starts (damsuite/connections.py), each added once, named 'ND Dam Suite: ...'.
Run: python3 -m ND_Dam_Hazard_Helper.tests.run_tests (from the folder above the plugin)."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
from ND_Dam_Hazard_Helper.tests import fake_qgis  # noqa: E402
from ND_Dam_Hazard_Helper.tests.registry import test  # noqa: E402

fake_qgis.install()
from ND_Dam_Hazard_Helper.damsuite import connections as CN  # noqa: E402
from ND_Dam_Hazard_Helper.damsuite import versions as VS  # noqa: E402


class _Settings(object):
    def __init__(self, d=None):
        self.d = dict(d or {})

    def value(self, key, default=None):
        return self.d.get(key, default)

    def setValue(self, key, value):              # noqa: N802 - QGIS API
        self.d[key] = value


@test
def browser_connections_added_once_with_the_nd_prefix():
    assert VS.CONNECTION_PREFIX == "ND Dam Suite"
    s = _Settings()
    first = CN.register(s, prefix=VS.CONNECTION_PREFIX, qgis_version_int=33400)
    plan = CN.planned(prefix=VS.CONNECTION_PREFIX)
    assert len(first["added"]) == len(plan) >= 30, first
    assert all(n.startswith("ND Dam Suite: ") for n in first["added"])
    wms = "connections/ows/items/wms/connections/items/ND Dam Suite: ND DWR MapService (WMS)/url"
    assert s.d[wms] == "https://mapservice.dwr.nd.gov/swcwms"
    arc = [k for k in s.d if k.startswith("connections/arcgisfeatureserver/items/ND Dam Suite: "
                                           "USGS road-stream crossings")]
    assert arc and s.d[arc[0]].endswith("/stream_crossings/FeatureServer"), arc
    del s.d[wms]                                  # the user deletes one: it stays deleted
    again = CN.register(s, prefix=VS.CONNECTION_PREFIX, qgis_version_int=33400)
    assert again["added"] == [] and wms not in s.d
    off = _Settings({CN.ENABLED_KEY: False})
    assert CN.register(off, prefix="x", qgis_version_int=33400)["off"]


@test
def every_nd_plugin_registers_at_start():
    root = os.path.dirname(PLUGIN_DIR)
    for pl in ("ND_Dam_Hazard_Helper", "nd_breach_runner", "nd_dam_screening", "nd_data_updater"):
        with open(os.path.join(root, pl, "plugin.py"), encoding="utf-8") as f:
            src = f.read()
        init = src.split("def initGui", 1)[1].split("\n    def ", 1)[0]
        assert "connections.register_in_qgis" in init.replace("_conn.", "connections."), pl
