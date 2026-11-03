"""Hazard Helper 2.35.3 (port of DWR_Plugins #254): rasters named with units after the
quantity ("Depth_ft.tif", "Velocity_ftps.tif", "Depth_m.asc", "Velocity_ft_per_s.tif") are
found in the dam folder (they were taken for time steps and skipped) and their units come
from the name; metric names in a dam folder are flagged.

    python3 -m ND_Dam_Hazard_Helper.tests.run_tests dsswise_unit
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
from ND_Dam_Hazard_Helper.tests.registry import test  # noqa: E402


def _mods():
    from ND_Dam_Hazard_Helper import autodetect as A
    from ND_Dam_Hazard_Helper import dsswise as D
    return A, D


def _touch(p):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "wb").close()


@test
def dsswise_unit_names_give_role_and_units():
    A, D = _mods()
    cases = [
        ("Depth_ft.tif", D.R_DEPTH, "ft"), ("depth_FT.asc", D.R_DEPTH, "ft"),
        ("DEPTH-feet.tif", D.R_DEPTH, "ft"), ("Depth_m.tif", D.R_DEPTH, "m"),
        ("Velocity_ftps.tif", D.R_VEL, "ft/s"), ("velocity_FPS.asc", D.R_VEL, "ft/s"),
        ("Velocity_ft_s.tif", D.R_VEL, "ft/s"), ("Velocity_ft_per_s.tif", D.R_VEL, "ft/s"),
        ("Velocity_mps.tif", D.R_VEL, "m/s"), ("Velocity_m_s.tif", D.R_VEL, "m/s"),
        ("MaxDepth_in_Ft.tif", D.R_DEPTH, "ft"),
        ("MaxVelocity_in_M_per_Sec.tif", D.R_VEL, "m/s"),
        ("MaxVelocity_in_Ft_per_Sec.tif", D.R_VEL, "ft/s"),
    ]
    for name, role, units in cases:
        r = D.detect_role(name)
        assert r == role, (name, r)
        assert D.detect_units(name, r) == (units, "file name"), (name, D.detect_units(name, r))
        assert not A._is_time_step(name), name


@test
def dsswise_unit_names_keep_time_steps_and_defaults():
    A, D = _mods()
    for name in ("Depth_0100.tif", "Velocity (01JAN2020 12:00:00).tif", "Depth_m_0100.tif"):
        assert A._is_time_step(name), name
    u, basis = D.detect_units("Depth (Max).tif", D.R_DEPTH)
    assert u == "ft" and basis.startswith("assumed"), (u, basis)


@test
def dsswise_unit_names_found_in_dam_folder():
    A, _ = _mods()
    with tempfile.TemporaryDirectory() as td:
        dam = os.path.join(td, "DWR-9999")
        _touch(os.path.join(dam, "DSSWISE", "Results", "Depth_ft.tif"))
        _touch(os.path.join(dam, "DSSWISE", "Results", "Velocity_ftps.tif"))
        f = A.find_rasters(dam)
        assert f["depth"] and f["depth"].endswith("Depth_ft.tif"), f
        assert f["vel"] and f["vel"].endswith("Velocity_ftps.tif"), f
        assert not any("WARNING" in n for n in f["notes"]), f["notes"]
    with tempfile.TemporaryDirectory() as td:
        dam = os.path.join(td, "DWR-9998")
        _touch(os.path.join(dam, "Depth_m.asc"))
        _touch(os.path.join(dam, "Velocity_m_s.asc"))
        f = A.find_rasters(dam)
        assert f["depth"] and f["vel"], f
        assert sum("WARNING" in n and "named in m" in n for n in f["notes"]) == 2, f["notes"]
