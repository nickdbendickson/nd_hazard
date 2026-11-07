"""3.29.1: the HEC-RAS breach plan defaults to piping with Froehlich (2008) values
(Nick, 2026-11-06). A run with no failure mode (TR-60, a user hydrograph, older runs) writes
Breach Geom field 6 True with a piping elevation; Froehlich (2008) piping (K0 1.0, z 0.7)
fills a missing width / side slope / formation time; piping coefficient 0.5 and the initial
piping elevation half way up the breach. Only an explicit 'overtopping' writes False.
No 'Breach Calculator Data=' line is written. Pure Python, no model runs."""
import types

from nd_breach_runner.core import breach as br
from nd_breach_runner.core import hecras
from nd_breach_runner.core import hecras_model as hm
from nd_breach_runner.tests.registry import test

TOP, BED, VW = 1071.09, 1043.59, 1615.5     # DWR-2801 (Absaraka) crest, bed, Vw (ac-ft)


def _dam():
    return br.DamData(dam_id="T2801", dam_height_ft=TOP - BED, storage_top_acft=VW,
                      top_of_dam_ft=TOP)


def _geom(params):
    units = hm.Units(False)
    b, wse0, trig, notes = hm.breach_values(_dam(), params, units, 2000.0)
    b.update(center_station=733.0, trigger_wse=trig)
    m = types.SimpleNamespace(breach=b, breach_progression="linear")
    lines = hm.breach_block(m)
    g = [x for x in lines if x.startswith("Breach Geom=")]
    assert len(g) == 1, lines
    assert not any(x.startswith("Breach Calculator Data=") for x in lines)
    return g[0][len("Breach Geom="):].split(","), b, notes


def _rows(params):
    return {r["item"]: r for r in hecras.breach_parameter_rows(_dam(), params)}


@test
def hecras_breach_defaults_to_piping_without_failure_mode():
    for params in ({}, {"method": "tr60", "peak_cfs": 50000.0}, {"method": "user"}):
        f, b, notes = _geom(params)
        assert len(f) == 10, f
        assert f[5] == "True" and f[7] != "", f
        assert abs(float(f[6]) - 0.5) < 1e-9, f
        assert abs(float(f[7]) - (BED + 0.5 * (TOP - BED))) < 0.01, f
        assert any("HEC-RAS Piping is the default" in n for n in notes), notes


@test
def hecras_breach_explicit_overtopping_stays_overtopping():
    f, b, _ = _geom({"failure_mode": "overtopping", "side_slope_h": 1.0,
                     "avg_breach_width_ft": 100.0, "formation_time_hr": 0.5})
    assert len(f) == 10 and f[5] == "False" and f[7] == "", f
    assert "Piping coefficient" not in _rows({"failure_mode": "overtopping"})


@test
def hecras_breach_froehlich_2008_piping_2801_numbers():
    f, b, notes = _geom({})
    assert abs(float(f[1]) - 80.77) < 0.05, f            # bottom width, ft
    assert abs(float(f[2]) - BED) < 0.01, f
    assert float(f[3]) == 0.7 and float(f[4]) == 0.7, f
    assert abs(float(f[8]) - 0.944) < 0.001, f            # formation time, h
    r = _rows({})
    assert r["Breach mode"]["value"] == "Piping"
    assert r["Froehlich (2008) piping"]["value"] == "auto"
    assert any(hecras.F08_AUTO in n for n in notes), notes


@test
def hecras_breach_rows_keep_the_runs_own_values_and_pipe():
    p = {"failure_mode": "piping", "side_slope_h": 0.9, "avg_breach_width_ft": 120.0,
         "formation_time_hr": 1.5, "pipe_initial_dia_ft": 2.0, "pipe_cd": 0.6,
         "breach_f2008": "fill_missing"}   # the opt-in; the default overwrites since then
    r = _rows(p)
    assert r["Left side slope"]["value"] == 0.9 and r["Full formation time"]["value"] == 1.5
    assert "Froehlich (2008) piping" not in r
    assert abs(r["Initial piping elevation"]["value"] - (BED + 1.0)) < 1e-6
    assert r["Piping coefficient"]["value"] == 0.6


@test
def hecras_breach_vw_is_max_storage_and_curve_only_warns():
    """Addendum: Vw is the given maximum storage; the elevation-volume curve is a check only.
    Synthetic numbers."""
    dam = br.DamData(dam_id="S1", dam_height_ft=20.0, storage_top_acft=400.0,
                     top_of_dam_ft=1000.0)
    a = hecras.froehlich_2008_piping_ft(dam, {}, 980.0)
    b = hecras.froehlich_2008_piping_ft(dam, {"storage_at_failure_acft": 300.0}, 980.0)
    assert a["vw_acft"] == b["vw_acft"] == 400.0 and a["avg_width_ft"] == b["avg_width_ft"]
    assert a["curve_warning"] == b["curve_warning"] == ""   # 3.29.4: breach.py notes it


@test
def hecras_breach_notes_the_navd88_shift():
    """Synthetic VERTCON record: the note gives the shift; an unshifted NGVD29 run warns."""
    rec = {"applied": True, "shift_ft": 1.25, "method": "VERTCON", "source_datum": "NGVD29"}
    _, _, notes = _geom({"vertical_datum": rec})
    assert any("NAVD88" in n and "+1.2500 ft" in n for n in notes), notes
    _, _, notes = _geom({"vertical_datum": dict(rec, applied=False, method="none (x)")})
    assert any(n.startswith("WARNING: the run's elevations were not shifted") for n in notes)
