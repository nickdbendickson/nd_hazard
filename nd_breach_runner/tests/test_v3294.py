"""3.29.4 (port of DamSafety 3.16.4; Nick, 2026-11-06 8:41 PM CST): sunny-day breaches use Vw = the maximum storage from
the DWR values (unless the user gives another storage); PMF / flood-inflow breaches follow the
ND Dam Safety Standards: Vw = the storage at the pool when the breach starts. The HEC-RAS writer
takes the run's Vw. Plus: no false NAVD88 warning for a local-datum dam tied to the DEM. Synthetic numbers."""
import types

from nd_breach_runner.core import breach as br
from nd_breach_runner.core import flood as fl
from nd_breach_runner.core import hecras
from nd_breach_runner.core import hecras_model as hm
from nd_breach_runner.tests.registry import test


def _dam(**k):
    d = dict(dam_id="S4", dam_height_ft=30.0, storage_top_acft=2000.0, top_of_dam_ft=1100.0,
             surface_area_top_ac=150.0, crest_length_ft=600.0)
    d.update(k)
    return br.DamData(**d)


def _flood(dam, method=br.METHOD_PIPING, trigger="wse", value=1094.0):
    fi = fl.FloodInputs.triangle(3000.0, 12.0, 48.0, normal_pool_ft=1090.0,
                                 crest_length_ft=600.0, trigger=trigger, trigger_value=value)
    return fl.route_flood(dam, method, br.BreachOverrides(), fi)


@test
def v3294_sunny_day_vw_is_max_storage():
    h = br.routed_hydrograph(_dam(pmf_max_wse_ft=1095.0))       # pool 5 ft below the crest
    assert h.params["vw_acft"] == 2000.0 and h.params["storage_at_failure_acft"] < 1900.0
    assert h.params["vw_source"].startswith("maximum storage")
    b_m, _, _ = br.froehlich_2008(2000.0 * br.ACFT, 30.0 * br.FT, "piping")
    assert abs(h.params["avg_breach_width_ft"] - b_m / br.FT) < 0.06, h.params
    assert any(n.startswith("Sunny day: the breach equations use Vw") for n in h.notes)
    assert not any(n.startswith("Check: the elevation-volume curve") for n in h.notes)


@test
def v3294_pmf_breach_vw_is_storage_at_the_breach_pool():
    out = _flood(_dam())                                         # breach when the pool hits 1094
    p = out.with_breach.params
    assert p["scenario"] == "flood inflow"
    assert p["vw_source"].startswith("storage at the pool")
    assert abs(p["vw_acft"] - p["storage_at_failure_acft"]) < 0.01, p
    assert p["vw_acft"] < 1900.0, p                              # below the crest: < max storage
    b_m, _, _ = br.froehlich_2008(p["vw_acft"] * br.ACFT, 30.0 * br.FT, "piping")
    assert abs(p["avg_breach_width_ft"] - b_m / br.FT) < 0.06, p
    assert not any(n.startswith("Sunny day:") for n in out.notes), out.notes


@test
def v3294_pmf_tr60_uses_the_breach_pool():
    out = _flood(_dam(crest_width_ft=12.0, us_slope_h=3.0, ds_slope_h=3.0), br.METHOD_TR60)
    p = out.with_breach.params
    assert abs(p["vw_acft"] - p["storage_at_failure_acft"]) < 0.01, p


@test
def v3294_hecras_takes_the_runs_vw():
    dam = _dam()
    sunny = hecras.froehlich_2008_piping_ft(dam, {"vw_acft": 2000.0,
                                                  "storage_at_failure_acft": 1500.0}, 1070.0)
    pmf = hecras.froehlich_2008_piping_ft(dam, {"vw_acft": 1500.0, "scenario": "flood inflow",
                                                "storage_at_failure_acft": 1500.0}, 1070.0)
    old_pmf = hecras.froehlich_2008_piping_ft(dam, {"scenario": "flood inflow",
                                                    "storage_at_failure_acft": 1500.0}, 1070.0)
    none = hecras.froehlich_2008_piping_ft(dam, {}, 1070.0)
    assert sunny["vw_acft"] == none["vw_acft"] == 2000.0
    assert pmf["vw_acft"] == old_pmf["vw_acft"] == 1500.0
    assert pmf["avg_width_ft"] < sunny["avg_width_ft"]
    assert sunny["curve_warning"] == ""


@test
def v3294_local_datum_is_not_a_navd88_warning():
    rec = {"applied": False, "local": True, "source_datum": "Local", "method": "DEM tie",
           "shift_ft": 0.0}
    _, _, _, notes = hm.breach_values(_dam(), {"vertical_datum": rec}, hm.Units(False), 2000.0)
    assert not any(n.startswith("WARNING: the run's elevations were not shifted") for n in notes)
    assert any("local datum was tied to the NAVD88 DEM" in n for n in notes), notes
    rec = {"applied": False, "source_datum": "NGVD29", "method": "none (x)"}
    _, _, _, notes = hm.breach_values(_dam(), {"vertical_datum": rec}, hm.Units(False), 2000.0)
    assert any(n.startswith("WARNING: the run's elevations were not shifted") for n in notes)
