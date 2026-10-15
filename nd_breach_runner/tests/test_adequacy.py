"""Spillway adequacy (core/adequacy.py): CSV reading by header name (nd_pmp v0.2.0 / v0.3.0,
plain two-column), analytic routing checks on synthetic reservoirs, agreement with
flood.route_flood's no-breach routing, freeboard rules, bisection of the largest multiple."""
import math
import os
import shutil
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from nd_breach_runner.core import adequacy as AD  # noqa: E402
from nd_breach_runner.core import breach as br, flood as fl  # noqa: E402
from nd_breach_runner.core.units import FT, ACFT, CFS  # noqa: E402
from nd_breach_runner.tests.registry import test  # noqa: E402

TMP = tempfile.mkdtemp(prefix="adequacy_")


def _w(name, text):
    p = os.path.join(TMP, name)
    with open(p, "w") as f:
        f.write(text)
    return p


def _const_area_table(z0, z1, area_ac):
    return _w("ss.csv", "elev_ft,storage_acft\n{},0\n{},{}\n".format(z0, z1, (z1 - z0) * area_ac))


@test
def adequacy_reads_by_header():
    v3 = _w("v3.csv", "# storm_id: s3\n# peak_cfs: 20\ntime_hr,flow_cfs,precip_in,excess_in\n"
            "1,10,0.5,0.1\n2,20,0.4,0.2\n3,5,0,0\n")
    v2 = _w("v2.csv", "# storm_id: s2\ntime_hr,precip_in,excess_in,flow_cfs\n"
            "1,0.5,0.1,10\n2,0.4,0.2,20\n3,0,0,5\n")
    plain = _w("plain.csv", "0,0\n1,10\n2,20\n3,5\n")
    named = _w("named.csv", "time_hr,flow_cfs\n0,0\n1,10\n2,20\n3,5\n")
    for p in (v3, v2, plain, named):
        t, q, meta = AD.read_inflow_csv(p)
        assert list(np.round(t / 3600.0, 6)) == [0, 1, 2, 3], (p, t)
        assert list(np.round(q / CFS, 6)) == [0, 10, 20, 5], (p, q / CFS)
    assert AD.read_inflow_csv(v3)[2]["storm_id"] == "s3"
    assert AD.read_inflow_csv(plain)[2]["storm_id"] == "plain"
    bad = _w("bad.csv", "time_hr,precip_in,excess_in\n1,1,1\n2,1,1\n")
    try:
        AD.read_inflow_csv(bad)
        raise AssertionError("no flow column accepted")
    except AD.AdequacyError:
        pass


@test
def adequacy_volume_balance_no_outflow():
    # constant-area reservoir, no spillway, no crest flow: rise = inflow volume / area
    area_ac, z0, top = 100.0, 1000.0, 1020.0
    ss = _const_area_table(z0 - 10, top + 30, area_ac)
    inflow = _w("tri.csv", "time_hr,flow_cfs\n0,0\n2,1000\n6,0\n")   # 3 h * 1000 cfs = 2000 cfs-hr
    res = AD.TableReservoir.from_csv(ss)
    t, q, _ = AD.read_inflow_csv(inflow)
    fi = fl.FloodInputs(t, q, None)
    r = AD.route_no_breach(res, fi, top * FT, z0 * FT)
    vol_acft = 1000 * 3 * 3600 * CFS / ACFT
    expect = z0 + vol_acft / area_ac
    assert abs(r["peak_wse_m"] / FT - expect) < 0.01, (r["peak_wse_m"] / FT, expect)


@test
def adequacy_linear_reservoir_analytic():
    # A dz/dt = Q - k (z - z0), z(0) = z0, constant Q: z = z0 + Q/k (1 - exp(-k t / A))
    area_ac, z0, top = 50.0, 500.0, 520.0
    k_cfs_per_ft = 200.0
    ss = _const_area_table(z0 - 5, top + 20, area_ac)
    rating = _w("lin.csv", "elev_ft,q_cfs\n{},0\n{},{}\n".format(z0, z0 + 40, 40 * k_cfs_per_ft))
    Q, hrs = 1000.0, 10.0
    inflow = _w("const.csv", "time_hr,flow_cfs\n0,{0}\n{1},{0}\n{2},0\n".format(Q, hrs, hrs + 0.001))
    res = AD.TableReservoir.from_csv(ss)
    t, q, _ = AD.read_inflow_csv(inflow)
    fi = fl.FloodInputs(t, q, fl.Spillway.from_csv(rating))
    r = AD.route_no_breach(res, fi, top * FT, z0 * FT)
    A = area_ac * 43560.0
    k = k_cfs_per_ft
    t_s = hrs * 3600.0
    expect = z0 + Q / k * (1 - math.exp(-k * t_s / A))
    assert abs(r["peak_wse_m"] / FT - expect) < 0.005, (r["peak_wse_m"] / FT, expect)


@test
def adequacy_matches_route_flood_no_breach():
    d = br.DamData(dam_id="SYN", dam_height_ft=30.0, storage_top_acft=1500.0, top_of_dam_ft=30.0,
                   surface_area_top_ac=120.0, crest_length_ft=800.0)
    rating = _w("weir.csv", "elev_ft,q_cfs\n" + "".join(
        "{},{}\n".format(20 + h, 3.0 * 100 * h ** 1.5) for h in np.arange(0, 15.01, 0.5)))
    inflow = _w("pmf.csv", "time_hr,flow_cfs\n0,0\n4,9000\n16,0\n")
    fi = fl.FloodInputs.from_files(inflow, rating, normal_pool_ft=20.0, crest_length_ft=800.0)
    ref = fl.route_flood(d, br.METHOD_PIPING if hasattr(br, "METHOD_PIPING") else "piping",
                         None, fi)
    out = AD.assess([inflow], 30.0, "high", spillway_csv=rating, crest_length_ft=800.0, dam=d,
                    bisect=False, start_pool_ft=20.0)
    row = out["rows"][0]
    assert abs(row["peak_pool_ft"] - ref.summary["peak_pool_ft_no_breach"]) <= 0.011, (
        row["peak_pool_ft"], ref.summary["peak_pool_ft_no_breach"])
    dflt = AD.assess([inflow], 30.0, "high", spillway_csv=rating, crest_length_ft=800.0, dam=d,
                     bisect=False)
    assert dflt["rows"][0]["start_pool_ft"] == 20.5   # default: first rating row with flow


@test
def adequacy_freeboard_rules_and_bisection():
    assert AD.required_freeboard_ft("high") == 1.0 and AD.required_freeboard_ft("low") == 3.0
    assert AD.required_freeboard_ft("medium", wind_ft=2.4) == 2.4
    assert AD.required_freeboard_ft("high", wind_ft=0.5) == 1.0
    assert AD.required_freeboard_ft("low", override_ft=2.0) == 2.0
    # constant area, no outflow: peak = z0 + m V / A, so the largest multiple is analytic
    area_ac, z0, top = 100.0, 1000.0, 1010.0
    ss = _const_area_table(z0 - 10, top + 30, area_ac)
    inflow = _w("tri2.csv", "time_hr,flow_cfs\n0,0\n2,4000\n6,0\n")
    vol_acft = 4000 * 3 * 3600 * CFS / ACFT
    out = AD.assess([inflow], top, "high", stage_storage_csv=ss, start_pool_ft=z0)
    row = out["rows"][0]
    expect_m = (top - 1.0 - z0) * area_ac / vol_acft
    assert abs(row["max_multiple_passed"] - expect_m) < 0.01, (row["max_multiple_passed"], expect_m)
    assert row["passes"] == (expect_m >= 1.0) and not row["max_multiple_capped"]
    v = out["verdict"]
    assert v["governing_storm"] == "tri2" and v["required_idf"].startswith("Probable Maximum")
    for p, f in (("a.csv", AD.write_csv), ("a.md", AD.write_report)):
        f(out, os.path.join(TMP, p))
    assert "DOES NOT PASS" in open(os.path.join(TMP, "a.md")).read() or row["passes"]


if __name__ == "__main__":
    from nd_breach_runner.tests.registry import TESTS
    failed = 0
    for fn in [f for f in TESTS if f.__module__ == __name__]:
        try:
            fn()
            print("ok  ", fn.__name__)
        except Exception as e:
            failed += 1
            import traceback
            traceback.print_exc()
            print("FAIL", fn.__name__, e)
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1 if failed else 0)


def _box(top=1020.0, z0=980.0, area=100.0):
    return _const_area_table(z0, top + 30, area)


@test
def adequacy_start_pool_coarse_rating_and_outlet():   # B1
    ss = _box()
    rating = _w("coarse.csv", "elev_ft,q_cfs\n990,0\n1005,50\n1010,2000\n")
    inflow = _w("b1.csv", "time_hr,flow_cfs\n0,0\n2,500\n6,0\n")
    out = AD.assess([inflow], 1020.0, "high", spillway_csv=rating, stage_storage_csv=ss,
                    wind_setup_runup_ft=1.0, bisect=False)
    v = out["verdict"]
    assert v["start_pool_ft"] == 1005.0, v
    assert "first rating elevation with flow" in v["start_pool_rule"]
    assert any("jumps from no flow" in n for n in out["notes"])
    rp = os.path.join(TMP, "b1.md")
    AD.write_report(out, rp)
    assert "1005" in open(rp).read()
    from nd_breach_runner.core import outlets as om
    outs = om.parse_outlets("pipe; 1000; 2; 0.6|gated; 995; 2; 0.6; 0:0")
    out = AD.assess([inflow], 1020.0, "high", spillway_csv=rating, stage_storage_csv=ss,
                    wind_setup_runup_ft=1.0, outlets=outs, bisect=False)
    v = out["verdict"]
    assert v["start_pool_ft"] == 1000.0 and "pipe" in v["start_pool_rule"], v   # gated ignored


@test
def adequacy_header_units():   # B2
    rows = "30,10\n60,20\n90,5\n"
    tm, _, _ = AD.read_inflow_csv(_w("m.csv", "time_min,flow_cfs\n0,0\n" + rows))
    assert list(np.round(tm / 3600.0, 6)) == [0, 0.5, 1.0, 1.5], tm
    _, qc, _ = AD.read_inflow_csv(_w("c.csv", "time_hr,flow_cms\n0,0\n1,10\n2,1\n"))
    assert abs(qc[1] - 10.0) < 1e-9, qc
    ts, _, _ = AD.read_inflow_csv(_w("s.csv", "time_s,flow_cfs\n0,0\n3600,1\n"))
    assert ts[1] == 3600.0
    for name, head in (("u1.csv", "time,flow"), ("u2.csv", "t_x,q_y"), ("u3.csv", "time_min,flow")):
        try:
            AD.read_inflow_csv(_w(name, head + "\n0,0\n1,1\n"))
            raise AssertionError(head + " accepted")
        except AD.AdequacyError:
            pass
    _, _, meta = AD.read_inflow_csv(_w("nh.csv", "0,0\n1,10\n"))
    assert any("no header" in w for w in meta["warnings"])


@test
def adequacy_t0_row_only_for_end_of_step():   # N2
    t, q, m = AD.read_inflow_csv(_w("mid.csv", "time_hr,flow_cfs\n5,100\n6,100\n7,0\n"))
    assert list(np.round(t / 3600.0, 6)) == [0, 1, 2] and q[0] / CFS == 100, (t, q)
    assert m["warnings"]
    t, q, _ = AD.read_inflow_csv(_w("bf.csv", "# baseflow: constant\ntime_hr,flow_cfs\n"
                                    "1,50\n2,80\n3,50\n"))
    assert len(t) == 4 and abs(q[0] / CFS - 50) < 1e-9, q / CFS


@test
def adequacy_freeboard_warnings_and_fail():   # N3, N4, high-hazard FAIL end to end
    ss = _box()
    inflow = _w("big.csv", "time_hr,flow_cfs\n0,0\n2,20000\n10,0\n")
    out = AD.assess([inflow], 1020.0, "high", stage_storage_csv=ss, start_pool_ft=1010.0,
                    bisect=True)
    assert not out["verdict"]["passes"] and "DOES NOT PASS" in out["verdict"]["text"]
    assert out["rows"][0]["max_multiple_passed"] < 1.0
    assert any("no wind setup" in n for n in out["notes"])
    assert any("NOT checked" in n and "Normal freeboard" in n for n in out["notes"])
    out = AD.assess([inflow], 1020.0, "low", stage_storage_csv=ss, start_pool_ft=990.0,
                    freeboard_ft=0.2, bisect=False)
    assert out["verdict"]["freeboard_override"] and "override" in out["verdict"]["text"]
    assert any("below the REG_05.2026 minimum" in n for n in out["notes"])


@test
def adequacy_start_above_allowed_multiple_zero():   # max_multiple ok(0.0) false branch
    ss = _box()
    inflow = _w("small.csv", "time_hr,flow_cfs\n0,0\n1,10\n2,0\n")
    out = AD.assess([inflow], 1020.0, "low", stage_storage_csv=ss, start_pool_ft=1018.0)
    r = out["rows"][0]
    assert r["max_multiple_passed"] == 0.0 and not r["passes"], r


@test
def adequacy_long_storm_and_duplicate_ids():   # N6, N7
    ss = _box()
    a = _w("dupA.csv", "# storm_id: X\ntime_hr,flow_cfs\n0,0\n10,100\n300,0\n")
    b = _w("dupB.csv", "# storm_id: X\ntime_hr,flow_cfs\n0,0\n2,50\n4,0\n")
    rating = _w("lr.csv", "elev_ft,q_cfs\n1000,0\n1000.1,100\n1010,5000\n")
    out = AD.assess([a, b], 1020.0, "high", spillway_csv=rating, stage_storage_csv=ss,
                    wind_setup_runup_ft=1.0, bisect=False)
    ids = [r["storm_id"] for r in out["rows"]]
    assert len(set(ids)) == 2 and set(ids) == set(out["series"]), ids
    assert out["rows"][0]["routing_complete"], out["rows"][0]
    assert any("more than one file" in n for n in out["notes"])
    res = AD.TableReservoir.from_csv(ss)
    t, q, _ = AD.read_inflow_csv(a)
    r = AD.route_no_breach(res, fl.FloodInputs(t, q, None), 1020 * FT, 1000 * FT, max_hours=240)
    assert not r["complete"]
