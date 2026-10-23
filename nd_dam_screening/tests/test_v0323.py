"""v0.32.3: tool 8 (the dam record for a dam screened on its own) no longer writes the
normal-pool (mapped water) area as the top-of-dam surface area.

The Runner turns surface_area_ac into its storage-area exponent (area x height / storage).
est_area_m2 / inv_area_m2 are the normal-pool area, which on a dry flood-control dam is near
zero and forces the exponent to its floor of 1, about a third low on the peak breach flow
(Absaraka DWR-2801). The tool-7b / runchain hand-off already leaves it out; tool 8 now does
the same, so the Runner uses its depth-squared default when no top-of-dam area is known.

Run: PYTHONPATH=tests/qgis_mock:<parent of the plugin> python3 tests/test_v0323.py
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
PARENT = os.path.dirname(PLUGIN)
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, PARENT)
from nd_dam_screening.core import damrecord_io as dri  # noqa: E402

AF, AC = 1233.4818375, 4046.8564224

# a dry flood-control dam: a small mapped-water area, real height and storage
attrs = {"dam_id": "DWR-2801", "name": "Absaraka", "height_m": 27.5 * 0.3048,
         "est_total_m3": 1615.0 * AF, "est_area_m2": 0.94 * AC, "inv_area_m2": 0.94 * AC}

v = dri.tool8_values(attrs)
assert "surface_area_ac" not in v, v                      # the pool area is NOT the top-of-dam area
assert abs(v["height_ft"] - 27.5) < 0.01 and abs(v["storage_acft"] - 1615.0) < 0.1, v

# even a large inventory surface area (still a normal-pool area) is not passed as the top-of-dam area
v2 = dri.tool8_values(dict(attrs, est_area_m2=None, inv_area_m2=120.0 * AC))
assert "surface_area_ac" not in v2, v2

# the identity and the point still carry through unchanged
v3 = dri.tool8_values(dict(attrs, _x=622365.94, _y=5204685.03, _crs="EPSG:26914"))
assert v3["x"] == 622365.94 and v3["crs"] == "EPSG:26914" and "surface_area_ac" not in v3, v3

print("OK (tool 8 does not pass the normal-pool area as the top-of-dam area)")
