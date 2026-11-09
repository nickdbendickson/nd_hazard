"""3.29.2 (Nick, 2026-11-06 1:17 PM): Froehlich (2008) piping always replaces the run's breach
width / side slopes / formation time in the HEC-RAS plan (breach_f2008 'fill_missing' is the
opt-in old behaviour); the breach equations use Vw = the maximum storage (DWR MAX_STORAGE),
the elevation-volume curve at the starting pool is a warning check only. Synthetic numbers."""
from nd_breach_runner.core import breach as br
from nd_breach_runner.core import hecras
from nd_breach_runner.tests.registry import test


def _dam(**k):
    d = dict(dam_id="S2", dam_height_ft=30.0, storage_top_acft=2000.0, top_of_dam_ft=1100.0,
             surface_area_top_ac=150.0)
    d.update(k)
    return br.DamData(**d)


def _rows(dam, p):
    return {r["item"]: r for r in hecras.breach_parameter_rows(dam, p)}


MLM = {"method": "mlm1984", "failure_mode": "piping", "side_slope_h": 1.0,
       "avg_breach_width_ft": 55.0, "formation_time_hr": 2.0,
       "breach_parameters": "MacDonald & Langridge-Monopolis (1984)"}


@test
def v3292_f2008_overwrites_the_runs_breach_values_by_default():
    dam = _dam()
    f = hecras.froehlich_2008_piping_ft(dam, {}, 1070.0)
    r = _rows(dam, MLM)
    assert r["Left side slope"]["value"] == 0.7 == r["Right side slope"]["value"]
    assert abs(r["Full formation time"]["value"] - f["tf_hr"]) < 1e-9
    assert abs(r["Final bottom width"]["value"] - (f["avg_width_ft"] - 0.7 * 30.0)) < 1e-6
    assert "replaces the run's values" in r["Froehlich (2008) piping"]["note"]


@test
def v3292_fill_missing_is_opt_in():
    r = _rows(_dam(), dict(MLM, breach_f2008="fill_missing"))
    assert r["Left side slope"]["value"] == 1.0 and r["Full formation time"]["value"] == 2.0
    assert "Froehlich (2008) piping" not in r
    try:
        _rows(_dam(), dict(MLM, breach_f2008="sometimes"))
    except hecras.HecRasError:
        pass
    else:
        raise AssertionError("a bad breach_f2008 was accepted")


@test
def v3292_breach_equations_use_max_storage_and_curve_only_warns():
    low = _dam(pmf_max_wse_ft=1095.0)                    # pool starts 5 ft below the crest
    h = br.routed_hydrograph(low)
    assert h.params["vw_acft"] == 2000.0 and h.params["storage_at_failure_acft"] < 1900.0
    b_m, _, _ = br.froehlich_2008(2000.0 * br.ACFT, 30.0 * br.FT, "piping")
    assert abs(h.params["avg_breach_width_ft"] - b_m / br.FT) < 0.06, h.params
    assert any(n.startswith("Sunny day: the breach equations use Vw") for n in h.notes), h.notes
    top = br.routed_hydrograph(_dam())                   # at the crest: curve == Vw, no note
    assert not any(n.startswith("Sunny day: the breach equations use Vw") for n in top.notes)
    assert top.params["vw_acft"] == top.params["storage_at_failure_acft"] == 2000.0
