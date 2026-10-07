"""Tests of core/verification.py and algorithms_verification.py.

Fast tests: the analytic reference solutions against hand calculations and their defining
identities (Ritter's mass integral, Stoker's Rankine-Hugoniot and Riemann-invariant
conditions, Manning's normal depth), the plot writer, the Word / markdown / CSV records
(written, parsed back), the validation record from a benchmark folder written by
benchmark.write_outputs, and the two Processing algorithms through the fake harness with the
model runs stubbed.

Model tests (NDBREACH_LISFLOOD, NDBREACH_ANUGA_PYTHON): the real suite in quick mode. They
also show the suite can fail (a wrong reference makes a case FAIL)."""
import csv
import importlib
import math
import os
import shutil
import tempfile
import time
import xml.etree.ElementTree as ET

import numpy as np

from nd_breach_runner.core import benchmark, report_docx as R, verification as V
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.tests.registry import test, model_test
from nd_breach_runner.tests.synthetic import npy_writer


class Tmp(object):
    def __enter__(self):
        self.d = tempfile.mkdtemp(prefix="ndv_")
        return self.d

    def __exit__(self, *a):
        shutil.rmtree(self.d, ignore_errors=True)


def _close(a, b, rel=1e-9, ab=0.0):
    return abs(a - b) <= max(ab, rel * max(abs(a), abs(b)))


# ------------------------------------------------------------------------ analytic solutions
@test
def verification_ritter_solution_conserves_mass_and_has_the_known_landmarks():
    g, h0, t = V.G_LISFLOOD, 1.0, 40.0
    c0 = math.sqrt(g * h0)
    x = np.linspace(-3.0 * c0 * t, 4.0 * c0 * t, 700001)
    h = V.ritter_depth(x, t, h0)
    # the reservoir was h0 over x < 0; on [-L, L'] the volume is h0 L: mass is conserved
    L = 3.0 * c0 * t
    vol_now = V._trapz(h[x <= 0.0], x[x <= 0.0]) + V._trapz(h[x >= 0.0], x[x >= 0.0])
    assert _close(vol_now, h0 * L, rel=1e-5), (vol_now, h0 * L)
    # the dam site: depth 4/9 h0, velocity 2/3 c0 (critical flow: u = sqrt(g h))
    assert _close(float(V.ritter_depth(0.0, t, h0)), 4.0 / 9.0 * h0)
    u0 = float(V.ritter_velocity(0.0, t, h0))
    assert _close(u0, 2.0 / 3.0 * c0) and _close(u0, math.sqrt(g * 4.0 / 9.0 * h0))
    # the toe of the fan, the wet front, and undisturbed water behind
    assert _close(float(V.ritter_depth(-c0 * t, t, h0)), h0)
    assert float(V.ritter_depth(2.0 * c0 * t + 1.0, t, h0)) == 0.0
    assert float(V.ritter_depth(-c0 * t - 5.0, t, h0)) == h0
    assert _close(V.ritter_front(t, h0), 2.0 * c0 * t)
    # depth at 5% of h0 sits behind the front by 3 sqrt(g d) t
    xf = V.ritter_front(t, h0, depth=0.05 * h0)
    assert _close(float(V.ritter_depth(xf, t, h0)), 0.05 * h0, rel=1e-9)
    # momentum: integral of (h u) over the fan equals the momentum impulse, checked through
    # the discharge: q = h u = (2 c0 - xi)(c0 + xi) 2 / 27 g  with xi = x/t
    q = h * V.ritter_velocity(x, t, h0)
    mom = V._trapz(q, x)                          # total momentum per unit width
    # d/dt of the momentum equals the hydrostatic force difference: g h0^2 / 2 (per unit time)
    assert _close(mom / t, 0.5 * g * h0 ** 2, rel=2e-4), (mom / t, 0.5 * g * h0 ** 2)


@test
def verification_stoker_middle_state_satisfies_the_jump_and_invariant_conditions():
    g = V.G_LISFLOOD
    for ratio in (0.05, 0.1, 0.25, 0.4, 0.5, 0.8, 0.95):
        h0, h1 = 1.0, ratio
        st = V.stoker_state(h0, h1)
        h2, u2, s = st["h2"], st["u2"], st["s"]
        c0 = math.sqrt(g * h0)
        # Riemann invariant through the rarefaction fan
        assert _close(u2 + 2.0 * math.sqrt(g * h2), 2.0 * c0, rel=1e-10)
        # Rankine-Hugoniot at the bore with still water (h1, 0) ahead: mass and momentum
        assert _close(h2 * (u2 - s), h1 * (0.0 - s), rel=1e-9, ab=1e-12)
        mom_left = h2 * u2 * (u2 - s) + 0.5 * g * h2 ** 2
        mom_right = h1 * 0.0 * (0.0 - s) + 0.5 * g * h1 ** 2
        assert _close(mom_left, mom_right, rel=1e-9), (ratio, mom_left, mom_right)
        assert h1 < h2 < h0 and u2 > 0.0 and s > u2
        assert _close(st["froude2"], u2 / math.sqrt(g * h2))
    # an independent solution of the same two equations by a brute-force scan of h2
    # (no bisection), for the classic h1/h0 = 0.1: h2/h0 = 0.3962, the Froude number 1.18
    g0 = V.G_LISFLOOD
    hh = np.linspace(0.1, 1.0, 900001)
    f = (np.sqrt(g0 * hh * (0.1 + hh) / 0.2) * (1.0 - 0.1 / hh)
         - 2.0 * (math.sqrt(g0) - np.sqrt(g0 * hh)))
    k = int(np.argmax(f > 0))
    h2_scan = hh[k - 1] - f[k - 1] * (hh[k] - hh[k - 1]) / (f[k] - f[k - 1])
    st = V.stoker_state(1.0, 0.1)
    assert abs(st["h2"] - h2_scan) < 1e-6 and abs(st["h2"] - 0.3962) < 1e-3, (st, h2_scan)
    assert abs(st["froude2"] - 1.1775) < 1e-3, st
    # the depth profile: h0 in the reservoir, h1 far ahead, h2 between the fan and the bore,
    # and the volume balance (mass): d/dt of the volume beyond the bore front equals ...
    t, c0 = 30.0, math.sqrt(g)
    x = np.linspace(-4.0 * c0 * t, 4.0 * c0 * t, 400001)
    h = V.stoker_depth(x, t, 1.0, 0.25)
    assert _close(float(h[0]), 1.0) and _close(float(h[-1]), 0.25)
    vol0 = 1.0 * (4.0 * c0 * t) + 0.25 * (4.0 * c0 * t)
    assert _close(V._trapz(h, x), vol0, rel=1e-5), (V._trapz(h, x), vol0)
    st = V.stoker_state(1.0, 0.25)
    mid = 0.5 * (st["fan_tail"] * t + st["s"] * t)
    assert _close(float(V.stoker_depth(mid, t, 1.0, 0.25)), st["h2"])
    try:
        V.stoker_state(1.0, 1.2)
        raise AssertionError("h1 > h0 must be refused")
    except ValueError:
        pass
    try:
        V.stoker_state(1.0, 0.0)
        raise AssertionError("a dry bed belongs to Ritter's solution")
    except ValueError:
        pass


@test
def verification_manning_normal_depth_matches_the_hand_calculation():
    # q = 1 m2/s, n = 0.03, S = 0.001: h = (q n / sqrt(S))^(3/5)
    #   q n / sqrt(S) = 0.03 / 0.0316227766 = 0.9486833; ln = -0.0526803; x 0.6 = -0.0316082
    #   exp(-0.0316082) = 0.968885
    h = V.manning_normal_depth(1.0, 0.03, 0.001)
    assert abs(h - 0.968885) < 2e-6, h
    # and back: q = h^(5/3) sqrt(S) / n
    assert _close(h ** (5.0 / 3.0) * math.sqrt(0.001) / 0.03, 1.0, rel=1e-12)
    # rectangular channel with walls: b = 10, Q = 10 -> deeper than the wide-channel depth
    y = V.manning_normal_depth_rect(10.0, 10.0, 0.03, 0.001)
    R_h = 10.0 * y / (10.0 + 2.0 * y)
    Q = (1.0 / 0.03) * (10.0 * y) * R_h ** (2.0 / 3.0) * math.sqrt(0.001)
    assert _close(Q, 10.0, rel=1e-9)
    assert y > h
    # and the rectangular solution tends to the wide-channel one as b grows
    yw = V.manning_normal_depth_rect(1000.0, 1000.0, 0.03, 0.001)
    assert abs(yw - h) < 0.01 * h, (yw, h)
    for bad in ((0, 0.03, 0.001), (1, 0, 0.001), (1, 0.03, 0)):
        try:
            V.manning_normal_depth(*bad)
            raise AssertionError("non-positive input must be refused")
        except ValueError:
            pass


# ------------------------------------------------------------------------------ small pieces
@test
def verification_plot_svg_is_valid_xml_and_marks_the_data():
    x = np.linspace(0, 10, 50)
    svg = V.plot_svg([{"label": "model", "x": x, "y": np.sin(x)},
                      {"label": "analytic", "x": x, "y": np.cos(x), "dash": True, "marker": False},
                      {"label": "with gaps", "x": x, "y": np.where(x < 5, x, np.nan)}],
                     "A & B <test>", "x (m)", "depth (m)", vlines=[(5.0, "front")])
    root = ET.fromstring(svg)                       # parses: text is escaped
    assert root.tag.endswith("svg")
    assert "A &amp; B &lt;test&gt;" in svg and "model" in svg and "analytic" in svg
    assert "front" in svg and "polyline" in svg or "path" in svg
    # all-NaN and empty input do not raise
    ET.fromstring(V.plot_svg([{"label": "none", "x": [1, 2], "y": [np.nan, np.nan]}], "t", "x", "y"))
    ET.fromstring(V.plot_svg([], "t", "x", "y"))


@test
def verification_select_cases_and_groups():
    assert V.select_cases(None) == list(V.CASE_IDS)
    assert V.select_cases("ritter, stoker") == ["ritter", "stoker"]
    assert V.select_cases(["mass", "mass", "radial"]) == ["mass", "radial"]
    assert V.select_cases("anuga") == ["anuga_ritter", "anuga_uniform", "anuga_mass"]
    assert V.select_cases("lisflood") == ["ritter", "stoker", "uniform", "mass", "radial"]
    assert V.select_cases("") == list(V.CASE_IDS)
    try:
        V.select_cases("ritter,nonsense")
        raise AssertionError("unknown ids must be refused")
    except ValueError as e:
        assert "nonsense" in str(e) and "ritter" in str(e)
    # the case table is complete and every case names its model
    assert set(V.CASE_INFO) == set(V.CASE_IDS) == set(V.CASE_FUNCS)
    assert all(V.CASE_INFO[c][1] in ("LISFLOOD-FP", "ANUGA") for c in V.CASE_IDS)


@test
def verification_criteria_are_stated_and_graded():
    c = V.new_case("ritter")
    V._m(c, "a", 0.10, "A")
    V._m(c, "b", -0.30, "B")
    V._m(c, "c", 1.02, "C")
    V._crit(c, "a", "<=", 0.25, "expected behaviour", "why a")
    V._crit(c, "b", "abs<=", 0.5, "accuracy", "why b")
    V._crit(c, "c", "between", (0.9, 1.1), "conservation", "why c")
    V._finish(c)
    assert [k["passed"] for k in c["criteria"]] == [True, True, True] and c["status"] == V.PASS
    assert all(k["text"] and k["basis"] for k in c["criteria"])
    V._m(c, "a", 0.30, "A")
    c["criteria"] = []
    V._crit(c, "a", "<=", 0.25, "accuracy", "why a")
    V._crit(c, "missing", "<=", 1.0, "accuracy", "a measure that was not produced fails")
    V._finish(c)
    assert c["status"] == V.FAIL and [k["passed"] for k in c["criteria"]] == [False, False]


# --------------------------------------------------------------------------- the records
def _fake_case(cid, status=V.PASS, **kw):
    c = V.new_case(cid)
    x = np.linspace(0, 100, 40)
    c["arrays"] = {"title": "Fake plot of " + cid, "xlabel": "x (m)", "ylabel": "h (m)",
                   "series": [{"label": "model", "x": x, "y": x / 100.0},
                              {"label": "analytic", "x": x, "y": x / 100.0 + 0.01, "dash": True}]}
    c["description"] = "Fabricated case for the record test."
    c["setup"] = [("Grid", "1 m"), ("Time", "60 s")]
    c["reference"] = "Ritter (1892)"
    c["references"] = ["ritter"] if "ritter" in V.REFS else []
    V._m(c, "err", 0.01 if status == V.PASS else 0.5, "Error measure")
    V._crit(c, "err", "<=", 0.1, "accuracy", "a stated basis for the tolerance")
    c["notes"] = ["a finding"]
    c["summary"] = "err {}".format(c["metrics"]["err"])
    V._finish(c)
    if status == V.SKIP:
        c["status"], c["criteria"], c["error"] = V.SKIP, [], "no ANUGA interpreter"
        c["summary"] = "skipped"
    c.update(kw)
    return c


def _stub_cases(monkeypatch=None, fail=("stoker",), skip=()):
    """Replace the case functions by fabricated ones. Returns a restore function."""
    saved = dict(V.CASE_FUNCS)

    def make(cid):
        def fn(ctx):
            ctx.progress(50.0, "half")
            ctx.log("fake case " + cid)
            return _fake_case(cid, V.FAIL if cid in fail else (V.SKIP if cid in skip else V.PASS))
        return fn
    for cid in V.CASE_IDS:
        V.CASE_FUNCS[cid] = make(cid)

    def restore():
        V.CASE_FUNCS.clear()
        V.CASE_FUNCS.update(saved)
    return restore


def _fake_exe(d, content=b"fake lisflood binary", name="lisflood"):
    p = os.path.join(d, name)
    with open(p, "wb") as f:
        f.write(content)
    return p


@test
def verification_run_suite_writes_docx_markdown_html_csv_and_plots():
    import hashlib
    restore = _stub_cases(fail=("stoker",))
    try:
        with Tmp() as d:
            exe = _fake_exe(d)
            py = _fake_exe(d, b"anuga python", "python")
            logs, prog = [], []
            res = V.run_suite(exe, anuga_python=py, out_dir=os.path.join(d, "out"), quick=True,
                              progress=lambda p, m="": prog.append((p, m)), log=logs.append,
                              figures=False)
            assert [k["id"] for k in res.cases] == list(V.CASE_IDS)
            assert res.counts()[V.FAIL] == 1 and not res.ok
            assert res.case("stoker")["status"] == V.FAIL and res.case("ritter")["status"] == V.PASS
            assert prog and prog[-1][0] == 100.0 and max(p for p, _m in prog) == 100.0
            assert all(b[0] >= a[0] for a, b in zip(prog, prog[1:])), "progress goes backwards"
            assert any("fake case ritter" in s for s in logs)
            f = res.files
            for key in ("docx", "markdown", "html", "csv"):
                assert os.path.isfile(f[key]), key
            assert os.path.basename(f["docx"]) == "Verification Record.docx"
            assert len(f["plots"]) == len(V.CASE_IDS)
            for p in f["plots"].values():
                ET.fromstring(open(p, encoding="utf-8").read())
            # the csv: one row per measure, with the case, its status and the criterion
            rows = list(csv.DictReader(open(f["csv"], newline="", encoding="utf-8")))
            assert len(rows) == len(V.CASE_IDS)
            r = [x for x in rows if x["case_id"] == "stoker"][0]
            assert r["status"] == "FAIL" and r["passed"] == "no" and r["metric"] == "err"
            assert "0.1" in r["criterion"]
            md = open(f["markdown"], encoding="utf-8").read()
            assert "NOT all checks passed" in md and "acceptance criteria" in md
            assert "| **FAIL** |" in md and md.count("| **PASS** |") == len(V.CASE_IDS) - 1
            html = open(f["html"], encoding="utf-8").read()
            assert "<table" in html and "FAIL" in html
            # the record: parse it back
            info = R.inspect_docx(f["docx"])
            heads = " | ".join(t for _l, t in info["headings"])
            for cid in V.CASE_IDS:
                name = V.CASE_INFO[cid][0]
                assert name in heads, (name, heads)
            for h in ("Summary of results", "Computer and software", "Sign-off"):
                assert h in heads, h
            text = info["text"]
            assert "Verification Record" in text
            assert hashlib.sha256(open(exe, "rb").read()).hexdigest() in text
            assert hashlib.sha256(open(py, "rb").read()).hexdigest() in text
            import platform
            import sys
            assert platform.system() in text and sys.version.split()[0] in text
            assert np.__version__ in text and sys.executable in text
            assert "Prepared by" in text and "Reviewed by" in text
            assert "a stated basis for the tolerance" in text
            assert "not embedded" in text and "plots/ritter.svg" in text.replace("\\", "/")
            assert info["tables"] >= 3 + len(V.CASE_IDS)
            # machine info is also returned on the result
            labels = [a for a, _b in res.info]
            for want in ("Date and time", "Platform", "Python", "numpy", "LISFLOOD-FP executable"):
                assert want in labels, (want, labels)
            assert res.executables["lisflood"]["sha256"] == hashlib.sha256(
                open(exe, "rb").read()).hexdigest()
    finally:
        restore()


@test
def verification_all_pass_record_and_keep_work_option():
    restore = _stub_cases(fail=())
    try:
        with Tmp() as d:
            res = V.run_suite(_fake_exe(d), anuga_python=None, out_dir=os.path.join(d, "o"),
                              cases=["ritter", "uniform"], figures=False, keep_work=False)
            assert res.ok and [k["id"] for k in res.cases] == ["ritter", "uniform"]
            assert not os.path.exists(os.path.join(d, "o", "work"))
            assert "work" not in res.files
            assert "all checks passed" in open(res.files["markdown"], encoding="utf-8").read()
    finally:
        restore()


@test
def verification_anuga_cases_are_skipped_with_a_reason_when_there_is_no_interpreter():
    with Tmp() as d:
        res = V.run_suite(None, anuga_python=None, out_dir=os.path.join(d, "o"), cases="anuga",
                          figures=False)
        assert [k["status"] for k in res.cases] == [V.SKIP] * 3
        assert all("ANUGA" in k["error"] and k["summary"] for k in res.cases)
        assert not res.ok                                    # nothing passed: not "verified"
        info = R.inspect_docx(res.files["docx"])
        assert "SKIP" in info["text"] and "No ANUGA Python interpreter" in info["text"]
        # LISFLOOD cases without an executable are errors (a suite that ran nothing is no proof)
        res = V.run_suite("", anuga_python=None, out_dir=os.path.join(d, "o2"), cases="ritter",
                          figures=False)
        assert res.cases[0]["status"] == V.ERROR and "executable" in res.cases[0]["error"]
        # a case that raises is reported, not propagated
        res = V.run_suite(os.path.join(d, "missing_exe"), out_dir=os.path.join(d, "o3"),
                          cases="uniform", figures=False)
        assert res.cases[0]["status"] in (V.ERROR, V.FAIL) and not res.ok


@test
def verification_run_suite_can_be_cancelled():
    from nd_breach_runner.core.lisflood import Cancelled
    restore = _stub_cases(fail=())
    try:
        with Tmp() as d:
            n = [0]

            def cancel():
                n[0] += 1
                return n[0] > 2
            try:
                V.run_suite(_fake_exe(d), out_dir=os.path.join(d, "o"), cancel=cancel,
                            figures=False)
                raise AssertionError("expected Cancelled")
            except Cancelled:
                pass
    finally:
        restore()


# ------------------------------------------------------------------ the validation record
def _bench_folder(d, shift=6, with_html=True):
    rng = np.random.default_rng(3)
    dep = np.where(rng.random((60, 80)) < 0.6, rng.random((60, 80)) * 8.0, np.nan)
    ref = np.roll(dep, shift, axis=1)
    m = benchmark.compare_fields({"depth": dep, "velocity": np.where(np.isfinite(dep), 2.0, np.nan)},
                                 {"depth": ref, "velocity": np.where(np.isfinite(ref), 3.0, np.nan)},
                                 0.02)
    folder = os.path.join(d, "DAM-042", "_benchmark")
    benchmark.write_outputs(folder, m, benchmark.difference_arrays({"depth": dep}, {"depth": ref}),
                            npy_writer, Grid(dep, 0, 60, 1.0))
    if not with_html:
        os.remove(os.path.join(folder, "Benchmark_Report.html"))
    return folder, m


@test
def verification_read_benchmark_folder_recovers_metrics_matrix_and_rasters():
    with Tmp() as d:
        folder, m = _bench_folder(d)
        b = V.read_benchmark_folder(folder)
        assert _close(b["metrics"]["fit_index_F"], m["fit_index_F"], rel=1e-6)
        assert _close(b["metrics"]["depth_ft.rmse"], m["depth_ft"]["rmse"], rel=1e-6)
        assert b["verdict"] == m["verdict"] and b["verdict"]
        mx = b["matrix"]
        assert mx and np.array(mx["rows"]).sum() == int(np.sum(np.array(
            m["depth_class_matrix"]["rows_ours_cols_reference"])))
        assert np.array(mx["rows"]).tolist() == np.array(
            m["depth_class_matrix"]["rows_ours_cols_reference"]).tolist()
        assert len(mx["classes"]) == len(m["depth_class_matrix"]["classes_ft"])
        assert len(b["files"]) == 2 and all(len(f["sha256"]) == 64 for f in b["files"] + b["rasters"])
        assert V.guess_dam_id(folder) == "DAM-042"
        assert V.guess_dam_id(os.path.join(d, "X", "_hecras", "benchmark")) == "X"
        try:
            V.read_benchmark_folder(d)
            raise AssertionError("a folder without Benchmark_Summary.csv must be refused")
        except FileNotFoundError as e:
            assert "Benchmark_Summary.csv" in str(e)


@test
def verification_validation_record_is_written_and_parses():
    with Tmp() as d:
        folder, m = _bench_folder(d)
        out = os.path.join(d, "records", "Validation Record.docx")
        os.makedirs(os.path.dirname(out))
        r = V.validation_record(
            folder, out, reference_description="Breach sunny-day run, 2019 HEC-RAS 2D model",
            study_title="Dam Failure Analysis, Report 17-03", study_by="Example Engineering",
            study_date="2019-05-01", approval="ND State Engineer letter of 2019-08-12",
            reference_version="6.3", this_model="ANUGA, 10 ft mesh")
        assert r["path"] == out and os.path.getsize(out) > 5000
        assert r["dam_id"] == "DAM-042" and r["verdict"] == m["verdict"]
        assert r["matrix"] and len(r["rasters"]) == 2
        assert "Benchmark Extent Agreement.tif" in r["rasters"]
        info = R.inspect_docx(out)
        heads = " | ".join(t for _l, t in info["headings"])
        for h in ("Study identification", "Comparison", "Depth-class agreement",
                  "Rasters and files", "Acceptance", "Sign-off"):
            assert h in heads, (h, heads)
        text = info["text"]
        assert "Validation Record" in text and "DAM-042" in text
        assert "Dam Failure Analysis, Report 17-03" in text and "Example Engineering" in text
        assert "ND State Engineer letter" in text and "ANUGA, 10 ft mesh" in text
        assert m["verdict"].rstrip(".") in text
        assert "Fit index" in text and "{:.3f}".format(m["fit_index_F"]).rstrip("0")[:4] in text
        assert "Prepared by" in text and "Reviewed by" in text
        assert "To be completed by the reviewing engineer" in text     # acceptance left blank
        assert "Accepted with conditions" in text
        assert "Benchmark Depth Difference (ft).tif" in text
        # the confusion matrix table holds the counts
        mrows = np.array(m["depth_class_matrix"]["rows_ours_cols_reference"])
        assert "{:,}".format(int(mrows[0][0])) in text
        assert "flooded by both" in text
        # a typed acceptance statement replaces the blank block
        out2 = os.path.join(d, "v2.docx")
        V.validation_record(folder, out2, dam_id="OVERRIDE-1",
                            acceptance_statement="Accepted for EAP mapping per engineer.")
        t2 = R.inspect_docx(out2)["text"]
        assert "Accepted for EAP mapping per engineer." in t2 and "OVERRIDE-1" in t2
        assert "To be completed by the reviewing engineer" not in t2
        # study fields left empty stay empty: no invented values
        assert "Example Engineering" not in t2


@test
def verification_validation_record_without_the_html_report_warns_but_writes():
    with Tmp() as d:
        folder, m = _bench_folder(d, with_html=False)
        r = V.validation_record(folder, os.path.join(d, "v.docx"))
        assert r["matrix"] is None and any("matrix" in w for w in r["warnings"])
        t = R.inspect_docx(r["path"])["text"]
        assert "Not available" in t and "Sign-off" in t


# ------------------------------------------------------------------------ the algorithms
def _algorithm_module():
    from nd_breach_runner.tests import fake_qgis
    fake_qgis.install()
    import nd_breach_runner.algorithms as algs
    importlib.reload(algs)
    import nd_breach_runner.algorithms_tools as tools
    importlib.reload(tools)
    import nd_breach_runner.algorithms_verification as mod
    importlib.reload(mod)
    return algs, tools, mod, fake_qgis


@test
def verification_algorithms_are_defined_like_the_other_tools():
    algs, tools, mod, fq = _algorithm_module()
    assert mod.VERIFICATION_ALGORITHMS == (mod.VerificationSuiteAlgorithm,
                                           mod.ValidationRecordAlgorithm)
    a, b = mod.VerificationSuiteAlgorithm(), mod.ValidationRecordAlgorithm()
    assert isinstance(a, tools._ToolBase) and isinstance(b, tools._ToolBase)
    assert (a.name(), a.displayName()) == ("verification",
                                           "Verification suite (analytic dam-break tests)")
    assert (b.name(), b.displayName()) == ("validationrecord",
                                           "Validation record from a benchmark comparison")
    assert a.group() == "Data preparation and tools" and a.groupId() == "tools"
    for x in (a, b):
        x.initAlgorithm()
        assert x.shortHelpString() and x.createInstance().__class__ is x.__class__
        assert "REPORT" in x.outputs and "RESULT" in x.outputs
    names_a = [p.name() for p in a.parameterDefinitions()]
    assert names_a == ["QUICK", "CASES", "OUT_FOLDER", "LISFLOOD_EXE", "ANUGA_PYTHON", "THREADS",
                       "KEEP_WORK"], names_a
    assert a.parameterAsBool({}, "QUICK", None) is True
    names_b = [p.name() for p in b.parameterDefinitions()]
    assert names_b[0] == "BENCH_FOLDER" and names_b[-1] == "OUTPUT" and "ACCEPTANCE" in names_b
    assert "acceptance criteria" in a.shortHelpString() and "not a standard" in a.shortHelpString()
    assert "blank" in b.shortHelpString() and "Prepared by" in b.shortHelpString()
    # ids are unique among the provider's algorithms
    ids = [c().name() for c in algs.ALGORITHMS]
    assert ids.count("verification") == 1 and ids.count("validationrecord") == 1


@test
def verification_algorithm_runs_the_suite_and_returns_the_paths():
    algs, tools, mod, fq = _algorithm_module()
    calls = {}
    real = mod.verification.run_suite

    def fake_run_suite(exe, anuga_python=None, out_dir=None, cases=None, quick=True, **kw):
        calls.update(exe=exe, py=anuga_python, out=out_dir, cases=cases, quick=quick, kw=kw)
        kw["progress"](40.0, "working")
        kw["log"]("fake log")
        return real(exe, anuga_python=anuga_python, out_dir=out_dir, cases=cases, quick=quick,
                    figures=False, **{k: v for k, v in kw.items() if k != "figures"})
    mod.verification.run_suite = fake_run_suite
    restore = _stub_cases(fail=("mass",))
    try:
        with Tmp() as d:
            exe = _fake_exe(d)
            a = mod.VerificationSuiteAlgorithm()
            a.initAlgorithm()
            fb = fq.QgsProcessingFeedback()
            out = a.processAlgorithm(
                {"QUICK": True, "CASES": "ritter, mass", "OUT_FOLDER": os.path.join(d, "v"),
                 "LISFLOOD_EXE": exe, "ANUGA_PYTHON": "", "THREADS": 2, "KEEP_WORK": True},
                fq.QgsProcessingContext(), fb)
            assert calls["cases"] == ["ritter", "mass"] and calls["quick"] is True
            assert calls["exe"] == exe and calls["out"] == os.path.join(d, "v")
            assert calls["kw"]["threads"] == 2 and calls["kw"]["keep_work"] is True
            assert os.path.isfile(out["REPORT"]) and out["REPORT"].endswith("Verification Record.docx")
            assert out["FOLDER"] == os.path.join(d, "v")
            assert out["RESULT"] == "1 PASS, 1 FAIL, 0 SKIP, 0 ERROR", out["RESULT"]
            assert any("[FAIL]" in e for e in fb.errors), fb.errors        # a failure is an error line
            assert any("[PASS]" in i for i in fb.info) and any("fake log" in i for i in fb.info)
            assert fb.progress == 100.0
            # a bad case id, and a missing LISFLOOD-FP, are Processing errors with a hint
            detect = algs.nd_data.detect_lisflood
            algs.nd_data.detect_lisflood = lambda: ""      # not the NDBREACH_LISFLOOD of a model run
            try:
                for params, want in (({"CASES": "ritter,bogus", "LISFLOOD_EXE": exe}, "bogus"),
                                     ({"CASES": "ritter",
                                       "LISFLOOD_EXE": os.path.join(d, "none")},
                                      "LISFLOOD-FP executable")):
                    p = {"QUICK": True, "OUT_FOLDER": os.path.join(d, "w"), "ANUGA_PYTHON": "",
                         "THREADS": 0}
                    p.update(params)
                    try:
                        a.processAlgorithm(p, fq.QgsProcessingContext(),
                                           fq.QgsProcessingFeedback())
                        raise AssertionError("expected QgsProcessingException")
                    except fq.QgsProcessingException as e:
                        assert want in str(e), str(e)
            finally:
                algs.nd_data.detect_lisflood = detect
    finally:
        mod.verification.run_suite = real
        restore()


@test
def verification_validation_record_algorithm_writes_the_docx():
    algs, tools, mod, fq = _algorithm_module()
    with Tmp() as d:
        folder, m = _bench_folder(d)
        a = mod.ValidationRecordAlgorithm()
        a.initAlgorithm()
        fb = fq.QgsProcessingFeedback()
        out_path = os.path.join(d, "sub", "Validation Record.docx")
        out = a.processAlgorithm(
            {"BENCH_FOLDER": folder, "DAM_ID": "", "REFERENCE_MODEL": "HEC-RAS 2D",
             "STUDY_TITLE": "Study X", "REFERENCE_DESC": "Run 3", "OUTPUT": out_path},
            fq.QgsProcessingContext(), fb)
        assert out["REPORT"] == out_path and os.path.isfile(out_path)
        assert m["verdict"].split(".")[0][:20] in out["RESULT"] or "Verdict" in out["RESULT"]
        text = R.inspect_docx(out_path)["text"]
        assert "Study X" in text and "Run 3" in text and "DAM-042" in text
        # errors: a folder that is not a benchmark folder, and no folder at all
        for bad in (d, os.path.join(d, "nope"), ""):
            try:
                a.processAlgorithm({"BENCH_FOLDER": bad, "OUTPUT": out_path},
                                   fq.QgsProcessingContext(), fq.QgsProcessingFeedback())
                raise AssertionError("expected QgsProcessingException")
            except fq.QgsProcessingException as e:
                assert str(e)


# =================================================================== model tests (real runs)
def _lf():
    exe = os.environ.get("NDBREACH_LISFLOOD", "")
    if not exe or not os.path.isfile(exe):
        return ""
    return exe


def _anuga():
    py = os.environ.get("NDBREACH_ANUGA_PYTHON", "")
    return py if py and os.path.isfile(py) else ""


def _metric(res, cid, key):
    return res.case(cid)["metrics"][key]


@model_test
def verification_lisflood_cases_pass_in_quick_mode_and_match_the_analytic_answers():
    exe = _lf()
    if not exe:
        print("   (skipped: NDBREACH_LISFLOOD not set)")
        return
    with Tmp() as d:
        t0 = time.time()
        res = V.run_suite(exe, anuga_python=None, out_dir=os.path.join(d, "o"), cases="lisflood",
                          quick=True, figures=False, keep_work=False)
        dt = time.time() - t0
        for k in res.cases:
            print("   {:8s} {} {:5.1f} s  {}".format(k["id"], k["status"], k["runtime_s"],
                                                     k["summary"]))
        assert [k["status"] for k in res.cases] == [V.PASS] * 5, [
            (k["id"], k["status"], k.get("error"), k["summary"]) for k in res.cases]
        assert dt < 90.0, dt
        # independent checks of the numbers, not of the suite's own tolerances
        mu = res.case("uniform")["metrics"]
        assert abs(mu["model_depth_m"] / mu["normal_depth_m"] - 1.0) < 0.02, mu
        assert abs(mu["normal_depth_m"] - V.manning_normal_depth(1.0, 0.03, 0.001)) < 0.05, mu
        assert abs(_metric(res, "mass", "balance_error_pct")) < 1.0
        assert _metric(res, "ritter", "rmse_over_h0") < 0.25
        assert 0.95 <= _metric(res, "radial", "diagonal_over_axis") <= 1.05
        # the record parses and has one section per case
        info = R.inspect_docx(res.files["docx"])
        assert sum(1 for _l, t in info["headings"] if "(LISFLOOD-FP)" in t) == 5


@model_test
def verification_anuga_cases_pass_in_quick_mode():
    exe, py = _lf(), _anuga()
    if not py:
        print("   (skipped: NDBREACH_ANUGA_PYTHON not set)")
        return
    with Tmp() as d:
        t0 = time.time()
        res = V.run_suite(exe or None, anuga_python=py, out_dir=os.path.join(d, "o"),
                          cases="anuga", quick=True, figures=False, keep_work=False)
        dt = time.time() - t0
        for k in res.cases:
            print("   {:14s} {} {:5.1f} s  {}".format(k["id"], k["status"], k["runtime_s"],
                                                      k["summary"]))
        assert [k["status"] for k in res.cases] == [V.PASS] * 3, [
            (k["id"], k["status"], k.get("error"), k["summary"]) for k in res.cases]
        assert dt < 120.0, dt
        assert _metric(res, "anuga_ritter", "rmse_over_h0") < 0.02
        assert abs(_metric(res, "anuga_mass", "balance_error_pct")) < 0.1
        assert res.executables["anuga_python"]["sha256"]


@model_test
def verification_record_embeds_the_plots_and_a_wrong_reference_fails():
    exe = _lf()
    if not exe:
        print("   (skipped: NDBREACH_LISFLOOD not set)")
        return
    with Tmp() as d:
        res = V.run_suite(exe, out_dir=os.path.join(d, "o"), cases="uniform", quick=True)
        assert res.ok
        info = R.inspect_docx(res.files["docx"])
        if res.files.get("figures_embedded"):
            assert info["images"] == res.files["figures_embedded"] == 1
        else:
            print("   (no SVG converter here: plots referenced, not embedded)")
        # the suite is capable of failing: a reference 5% too deep is caught by the 2% limit
        real = V.manning_normal_depth
        V.manning_normal_depth = lambda q, n, s: 1.05 * real(q, n, s)
        try:
            bad = V.run_suite(exe, out_dir=os.path.join(d, "bad"), cases="uniform", quick=True,
                              figures=False, keep_work=False)
        finally:
            V.manning_normal_depth = real
        assert bad.case("uniform")["status"] == V.FAIL and not bad.ok
        print("   wrong reference (+5%): " + bad.case("uniform")["summary"])
