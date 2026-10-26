"""Tests of the HEC-RAS interchange (core/hecras.py, algorithms_hecras.py).

No real HEC-RAS file was available. The reader is exercised with a small stand-in for an
h5py file (same interface the reader uses: visititems, attrs, shape, dtype, indexing) holding
a 6 x 6 mesh in the layout described in core/hecras.py; when h5py is installed the same
layout is also written to a real HDF5 file and read back."""
import importlib
import json
import math
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core import benchmark, breach as br, hecras
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.core.units import ACFT, CFS, FT, G
from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests.synthetic import npy_writer


def _close(a, b, rel=1e-6, abs_=0.0):
    assert abs(a - b) <= max(abs_, rel * max(abs(a), abs(b))), "{} != {}".format(a, b)


def _has_h5py():
    try:
        import h5py  # noqa: F401
        return True
    except ImportError:
        return False


# ============================================================================ export
def _tri(peak_cfs=12345.67, t_peak_s=1980.0, t_end_s=7200.0):
    return br.Hydrograph(np.array([0.0, t_peak_s, t_end_s]),
                         np.array([0.0, peak_cfs * CFS, 0.0]), "test")


def _parse_block(text):
    lines = text.splitlines()
    assert lines[0].startswith("Flow Hydrograph="), lines[0]
    n = int(lines[0].split("=")[1])
    vals = []
    for i, ln in enumerate(lines[1:]):
        assert len(ln) % 8 == 0 and len(ln) <= 80, repr(ln)
        if i < len(lines) - 2:
            assert len(ln) == 80, "all but the last line hold 10 values"
        vals += [float(ln[k:k + 8]) for k in range(0, len(ln), 8)]
    return n, vals


@test
def hecras_hydrograph_block_format_and_peak():
    d = tempfile.mkdtemp()
    try:
        hy = _tri()
        r = hecras.write_hydrograph_files(hy, d, 5)
        n, vals = _parse_block(open(r["txt"]).read())
        assert n == len(vals) == 25 == r["info"]["n"]          # 120 min / 5 min + 1
        assert r["info"]["interval"] == "5MIN"
        assert max(vals) == 12345.67, max(vals)                # peak kept exactly
        assert abs(r["info"]["peak_time_exported_hr"] - 33.0 / 60.0) <= 2.5 / 60.0 + 1e-9
        assert vals[0] == 0.0 and vals[-1] == 0.0
        # an ordinary sample: 20 min on the rising limb, 20/33 of the peak
        _close(vals[4], 12345.67 * 20.0 / 33.0, abs_=0.01)
        rows = [ln.split(",") for ln in open(r["csv"]).read().splitlines()]
        assert rows[0] == ["time_hr", "discharge_cfs"] and len(rows) == 26
        _close(float(rows[2][0]), 5.0 / 60.0, abs_=1e-5)
        assert max(float(x[1]) for x in rows[1:]) == 12345.67
        # a finer step and a step HEC-RAS does not offer
        r1 = hecras.write_hydrograph_files(hy, d, 1, "one")
        assert _parse_block(open(r1["txt"]).read())[0] == 121
        r7 = hecras.resample_hydrograph(hy, 7)[2]
        assert r7["interval"] is None and any("not one of" in x for x in r7["notes"])
        # numbers that need the whole field, and the optional Interval line
        big = hecras.flow_hydrograph_block([123456.78, 0.5, 12.0, 1234567.8], interval="1HOUR")
        ls = big.splitlines()
        assert ls[0] == "Interval=1HOUR" and ls[1] == "Flow Hydrograph= 4"
        assert [float(ls[2][i:i + 8]) for i in range(0, len(ls[2]), 8)] == [
            123456.8, 0.5, 12.0, 1234568.0]              # decimals dropped to fit 8 characters
        assert len(ls[2]) == 32
        huge = hecras.flow_hydrograph_block([123456789.0])
        assert len(huge.splitlines()[1]) == 8
    finally:
        shutil.rmtree(d)


@test
def hecras_hydrograph_of_a_routed_breach_keeps_its_peak():
    hy = br.routed_hydrograph(_dam())
    t, q, info = hecras.resample_hydrograph(hy, 5)
    _close(q.max(), hy.peak_cfs, rel=1e-9)
    assert abs(info["volume_change_pct"]) < 2.0, info["volume_change_pct"]
    assert abs(info["peak_time_exported_hr"] - hy.time_to_peak_hr) <= 2.5 / 60.0 + 1e-9
    assert np.all(np.diff(t) > 0) and q[-1] == 0.0


def _dam(**k):
    d = dict(dam_id="T1", dam_height_ft=25.0, storage_top_acft=500.0, top_of_dam_ft=1850.0,
             surface_area_top_ac=60.0)
    d.update(k)
    return br.DamData(**d)


def _row(rows, item):
    return [r for r in rows if r["item"] == item][0]


@test
def hecras_breach_table_matches_hand_computation():
    dam = _dam()
    hy = br.routed_hydrograph(dam)                        # Froehlich (2008) piping, sunny day
    rows = hecras.breach_parameter_rows(dam, hy.params, hy.peak_cfs)
    # by hand: the pool starts at the top of dam, so Vw = 500 ac-ft, hb = 25 ft
    vw, hb = 500.0 * ACFT, 25.0 * FT
    b_avg = 0.27 * vw ** 0.32 * hb ** 0.04               # m
    tf_hr = 63.2 * math.sqrt(vw / (G * hb ** 2)) / 3600.0
    bottom_w_ft = (b_avg - 0.7 * hb) / FT
    _close(_row(rows, "Final bottom width")["value"], bottom_w_ft, abs_=0.06)
    _close(_row(rows, "Full formation time")["value"], tf_hr, abs_=0.0006)
    assert _row(rows, "Final bottom elevation")["value"] == 1825.0
    assert _row(rows, "Left side slope")["value"] == _row(rows, "Right side slope")["value"] == 0.7
    assert _row(rows, "Breach weir coefficient")["value"] == 3.1
    assert _row(rows, "Breach center station")["value"] is None
    assert _row(rows, "Breach center station")["basis"] == hecras.BASIS_BLANK
    assert _row(rows, "Final bottom width")["basis"] == hecras.BASIS_EST
    assert _row(rows, "Breach initiation trigger")["value"] == "Water surface elevation"
    assert _row(rows, "Trigger value")["value"] == 1850.0
    assert _row(rows, "Breach mode")["value"] == "Piping"
    _close(_row(rows, "Peak breach outflow (plugin)")["value"], hy.peak_cfs)

    # explicit parameters, no bottom width given: 100 - 0.7 x 25 = 82.5 ft
    p = {"breach_bottom_ft": 1825.0, "avg_breach_width_ft": 100.0, "side_slope_h": 0.7,
         "formation_time_hr": 0.5, "failure_mode": "overtopping", "starting_wse_ft": 1850.0,
         "breach_parameters": "Froehlich (2008), overtopping; width from user"}
    rows = hecras.breach_parameter_rows(dam, p)
    assert _row(rows, "Final bottom width")["value"] == 82.5
    assert "yours" in _row(rows, "Final bottom width")["note"]
    assert _row(rows, "Breach mode")["value"] == "Overtopping"
    assert not [r for r in rows if r["item"] == "Piping coefficient"]

    # a higher user breach bottom, TR-60 (no side slope, no formation time), pipe phase, flood
    rows = hecras.breach_parameter_rows(_dam(breach_bottom_ft=1830.0),
                                        {"rise_time_hr": 0.4, "starting_wse_ft": 1850.0})
    assert _row(rows, "Final bottom elevation")["value"] == 1830.0
    assert _row(rows, "Final bottom elevation")["basis"] == hecras.BASIS_USER
    assert _row(rows, "Left side slope")["value"] is None
    assert _row(rows, "Left side slope")["basis"] == hecras.BASIS_NONE
    assert _row(rows, "Full formation time")["value"] is None
    assert "0.4" in _row(rows, "Full formation time")["note"]
    rows = hecras.breach_parameter_rows(dam, {
        "breach_bottom_ft": 1825.0, "failure_mode": "piping", "pipe_initial_dia_ft": 0.5,
        "pipe_cd": 0.6, "side_slope_h": 0.7, "bottom_width_ft": 60.0,
        "formation_time_hr": 1.0, "breach_trigger": "time", "breach_initiation_hr": 6.5})
    assert _row(rows, "Piping coefficient")["value"] == 0.6
    assert _row(rows, "Initial piping elevation")["value"] == 1825.25
    assert _row(rows, "Breach initiation trigger")["value"] == "Set time"
    assert _row(rows, "Trigger value")["value"] == 6.5
    rows = hecras.breach_parameter_rows(dam, {"breach_trigger": "peak",
                                              "pool_at_breach_ft": 1851.2,
                                              "breach_initiation_hr": 9.0})
    assert _row(rows, "Trigger value")["value"] == 1851.2


@test
def hecras_breach_table_files_mark_the_estimates():
    d = tempfile.mkdtemp()
    try:
        dam = _dam()
        hy = br.routed_hydrograph(dam)
        r = hecras.write_breach_table(dam, hy.params, d, peak_cfs=hy.peak_cfs, notes=["a|b"])
        import csv
        rows = list(csv.DictReader(open(r["csv"])))
        assert [x["item"] for x in rows][:3] == ["Breach center station",
                                                 "Final bottom elevation", "Final bottom width"]
        assert rows[0]["value"] == "" and rows[0]["basis"] == hecras.BASIS_BLANK
        assert {x["basis"] for x in rows} >= {hecras.BASIS_EST, hecras.BASIS_ASSUME}
        md = open(r["md"], encoding="utf-8").read()
        assert "estimates" in md and "(blank)" in md and "| Final bottom width |" in md
        assert "a/b" in md and "Froehlich (2008), piping" in md
    finally:
        shutil.rmtree(d)


def _fake_run(folder, dam=None, method=br.METHOD_PIPING):
    """A dam folder as the workflow leaves it: _breach_model/ with the CSV and the report."""
    dam = dam or _dam()
    hy = br.make_hydrograph(dam, method)
    m = os.path.join(folder, hecras.MODEL_FOLDER)
    os.makedirs(m)
    hy.to_csv(os.path.join(m, hecras.HYDROGRAPH_CSV))
    with open(os.path.join(m, hecras.RUN_REPORT_JSON), "w") as f:
        json.dump({"dam": br.dam_to_dict(dam), "hydrograph": hy.summary()}, f)
    return hy


@test
def hecras_export_run_reads_the_workflow_files():
    d = tempfile.mkdtemp()
    try:
        hy = _fake_run(d)
        r = hecras.export_run(d, None, 10)
        assert r["hydrograph_txt"].startswith(os.path.join(d, "_hecras"))
        n, vals = _parse_block(open(r["hydrograph_txt"]).read())
        assert n == len(vals) == r["info"]["n"] and r["info"]["interval"] == "10MIN"
        assert abs(max(vals) - hy.peak_cfs) <= 0.005 + 1e-4 * hy.peak_cfs   # CSV has 3 decimals
        assert os.path.exists(r["table_csv"]) and os.path.exists(r["table_md"])
        assert _row(r["rows"], "Final bottom elevation")["value"] == 1825.0
        try:
            hecras.export_run(tempfile.gettempdir() + "/no_such_dam_folder_x")
            assert False, "expected HecRasError"
        except hecras.HecRasError as e:
            assert "breach_hydrograph.csv" in str(e)
    finally:
        shutil.rmtree(d)


# ============================================================================ HDF stand-in
class _DS:
    def __init__(self, arr, attrs=None):
        self._a = np.asarray(arr)
        self.attrs = dict(attrs or {})
        self.shape, self.dtype = self._a.shape, self._a.dtype

    def __getitem__(self, k):
        return self._a[k]


class _Grp:
    def __init__(self):
        self.attrs = {}


class _FakeH5:
    """The parts of h5py.File the reader uses."""

    def __init__(self, layout, root_attrs):
        self.layout, self.attrs = layout, dict(root_attrs)

    def visititems(self, fn):
        groups = set()
        for p in self.layout:
            parts = p.split("/")
            groups.update("/".join(parts[:i]) for i in range(1, len(parts)))
        for g in sorted(groups):
            fn(g, _Grp())
        for p in sorted(self.layout):
            arr, at = self.layout[p]
            fn(p, _DS(arr, at))


N = 6
GEO = "Geometry/2D Flow Areas/{}/"
OUT = "Results/Unsteady/Output/Output Blocks/Base Output/"
SUMM = OUT + "Summary Output/2D Flow Areas/{}/"
TS = OUT + "Unsteady Time Series/"
WKT = 'PROJCS["fake_ft",GEOGCS["x"],UNIT["ft",0.3048]]'
DMAX = 5.0 - 0.5 * np.arange(4)          # depth in columns 0-3: 5, 4.5, 4, 3.5 ft; columns 4-5 dry


def _mesh_layout(si=False, lower=False, cell_vel=False, time_series=True, ghost=True,
                 min_elev=True, area="Perimeter 1"):
    """A 6 x 6 mesh of 10 ft cells, cell c = j * 6 + i at x = 1005 + 10 i, y = 2005 + 10 j.
    Ground 100 + 0.5 i ft; water surface 105 ft in columns 0-3, dry beyond."""
    ii, jj = [a.ravel() for a in np.meshgrid(np.arange(N), np.arange(N))]
    x, y = 1005.0 + 10 * ii, 2005.0 + 10 * jj
    elev = 100.0 + 0.5 * ii
    wet = ii <= 3
    wse = np.where(wet, 105.0, elev)
    faces, fv = [], []
    for j in range(N):
        for i in range(N - 1):
            faces.append((j * N + i, j * N + i + 1))
            fv.append(2.0 + i)                          # across columns i | i+1
    for j in range(N - 1):
        for i in range(N):
            faces.append((j * N + i, (j + 1) * N + i))
            fv.append(1.0)
    faces += [(0, N * N), (5, -1)]                      # faces to a ghost cell and to nothing
    fv += [9.0, 7.0]
    t_hr = np.arange(5.0)
    frac = np.clip(t_hr / 2.0, 0.0, 1.0)[:, None]       # depth grows linearly for 2 h
    dmax = np.where(wet, 105.0 - elev, 0.0)
    wse_t = elev[None, :] + dmax[None, :] * frac
    k = FT if si else 1.0                               # ft -> file units

    def conv(a):
        return np.asarray(a) * k
    u_len, u_vel = (b"m", b"m/s") if si else (b"ft", b"ft/s")
    xy = np.column_stack([x, y])
    if ghost:
        xy = np.vstack([xy, [[999.0, 2005.0], [999.0, 2015.0], [999.0, 2025.0], [999.0, 2035.0]]])
    g, s = GEO.format(area), SUMM.format(area)
    L = {g + "Cells Center Coordinate": (xy, {}),
         g + "Faces Cell Indexes": (np.array(faces, dtype=np.int32), {}),
         s + "Maximum Water Surface": (np.vstack([conv(wse), np.full(N * N, 3.0 / 24.0)]),
                                       {"Units": np.bytes_(u_len)}),
         s + "Maximum Water Surface Error": (np.full((2, N * N), 999.0), {}),
         s + "Minimum Water Surface": (np.vstack([conv(elev), np.zeros(N * N)]), {})}
    if min_elev:
        L[g + "Cells Minimum Elevation"] = (
            conv(np.concatenate([elev, [0.0] * 4]) if ghost else elev),
            {"Units": np.bytes_(u_len)})
    if cell_vel:
        L[s + "Maximum Cell Velocity"] = (conv(np.where(wet, 3.0, 0.0)),
                                          {"Units": np.bytes_(u_vel)})
    else:
        L[s + "Maximum Face Velocity"] = (np.vstack([conv(fv), np.zeros(len(fv))]),
                                          {"Units": np.bytes_(u_vel)})
    if time_series:
        L[TS + "Time"] = (t_hr / 24.0, {"Time": np.bytes_(b"Days")})
        L[TS + "Time Date Stamp"] = (np.array([b"01JAN2000 00:00:00"] * 5), {})
        L[TS + "2D Flow Areas/{}/Water Surface".format(area)] = (conv(wse_t),
                                                                 {"Units": np.bytes_(u_len)})
    if lower:
        L = {p.lower(): v for p, v in L.items()}
    root = {"Projection": np.bytes_(WKT.encode()), "Units System": "SI Metric" if si else "US Customary",
            "File Type": "HEC-RAS Results"}
    return L, root


def _write_h5(path, layout, root):
    import h5py
    with h5py.File(path, "w") as f:
        for k, v in root.items():
            f.attrs[k] = v
        for p, (arr, at) in layout.items():
            ds = f.create_dataset(p, data=arr)
            for k, v in at.items():
                ds.attrs[k] = v


def _sources(**kw):
    """The same layout as the stand-in and, with h5py installed, a real HDF5 file."""
    layout, root = _mesh_layout(**kw)
    out = [("stand-in", _FakeH5(layout, root), None)]
    if _has_h5py():
        d = tempfile.mkdtemp()
        p = os.path.join(d, "t.p01.hdf")
        _write_h5(p, layout, root)
        out.append(("h5py", p, d))
    return out


def _each_source(fn, **kw):
    for label, src, tmp in _sources(**kw):
        try:
            fn(src)
        except AssertionError as e:
            raise AssertionError("{}: {}".format(label, e))
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)


def _nan_eq(a, b, tol=1e-6):
    a, b = np.asarray(a, float), np.asarray(b, float)
    assert a.shape == b.shape, (a.shape, b.shape)
    assert np.array_equal(np.isnan(a), np.isnan(b)), (a, b)
    ok = ~np.isnan(a)
    assert np.allclose(a[ok], b[ok], atol=tol), (a, b)


def _expected_columns(vals):
    return np.tile(np.array(list(vals) + [np.nan, np.nan]), (N, 1))


# ============================================================================ import
@test
def hecras_import_grids_from_a_synthetic_mesh():
    def check(src):
        grids, info = hecras.import_results(src)
        assert info["area"] == "Perimeter 1" and info["n_cells"] == 36    # ghost cells dropped
        assert info["units"] == "us" and info["cell_size"] == 10.0
        assert (info["x0"], info["y_top"], info["nx"], info["ny"]) == (1000.0, 2060.0, 6, 6)
        assert grids["depth"].crs_wkt == WKT
        g = grids["depth"]
        assert (g.x0, g.y0, g.dx) == (1000.0, 2060.0, 10.0)
        _nan_eq(g.data, _expected_columns(DMAX))                          # max WSE - ground
        _nan_eq(grids["wse"].data, _expected_columns([105.0] * 4))
        v = grids["velocity"].data
        _close(v[N - 1 - 3, 1], (2.0 + 3.0 + 1.0 + 1.0) / 4.0)            # cell (i=1, j=3)
        _close(v[N - 1 - 0, 0], (2.0 + 1.0 + 9.0) / 3.0)                  # corner, ghost face
        assert np.isnan(v[:, 4:]).all()
        arr = grids["arrival"].data                                       # depth reaches 0.1 ft
        _nan_eq(arr, _expected_columns(0.2 / DMAX))                       # (default, 3.26.3)
        g1, _ = hecras.import_results(src, arrival_depth_ft=1.0)          # the old 1 ft
        _nan_eq(g1["arrival"].data, _expected_columns(2.0 / DMAX))
        _close(grids["time_of_max"].data[0, 0], 3.0)                      # days -> hours
        assert not any("No " in n for n in info["notes"]), info["notes"]
        assert "faces averaged" in info["used"]["velocity"]
        # arrival counted from the breach: 0.25 h after the start, and clipped at 0
        g2, _ = hecras.import_results(src, t0_hr=0.25, arrival_depth_ft=1.0)
        _close(g2["arrival"].data[0, 0], 0.4 - 0.25)
        g3, _ = hecras.import_results(src, t0_hr=1.0, arrival_depth_ft=1.0)
        assert g3["arrival"].data[0, 0] == 0.0
        # a 2 ft arrival depth
        g4, _ = hecras.import_results(src, arrival_depth_ft=2.0)
        _close(g4["arrival"].data[0, 0], 0.8)
        # own cell size and extent: 5 ft cells over the western half
        g5, i5 = hecras.import_results(src, cell_size=5.0, extent=(1000, 2000, 1030, 2060))
        assert (i5["nx"], i5["ny"]) == (6, 12)
        _nan_eq(g5["depth"].data[0, :], [5.0, 5.0, 4.5, 4.5, 4.0, 4.0])
    _each_source(check)


@test
def hecras_import_si_file_returns_feet():
    def check(src):
        grids, info = hecras.import_results(src)
        assert info["units"] == "si"
        _nan_eq(grids["depth"].data, _expected_columns(DMAX), tol=1e-6)
        _nan_eq(grids["wse"].data, _expected_columns([105.0] * 4), tol=1e-6)
        _close(grids["velocity"].data[2, 1], 1.75)
        _nan_eq(grids["arrival"].data, _expected_columns(0.2 / DMAX))     # 0.1 ft default
        # a wrong 'units' request is honoured over the file (values read as feet)
        g, _ = hecras.import_results(src, units="us")
        _close(np.nanmax(g["wse"].data), 105.0 * FT)
    _each_source(check, si=True)


@test
def hecras_import_tolerates_other_layouts_and_reports_gaps():
    # lower-case names, a per-cell velocity, no time series and no ghost cells
    def check(src):
        grids, info = hecras.import_results(src, area="PERIMETER 1")
        _nan_eq(grids["depth"].data, _expected_columns(DMAX))
        _close(grids["velocity"].data[0, 0], 3.0)
        assert "arrival" not in grids and "time_of_max" in grids
        assert any("time series" in n for n in info["notes"]), info["notes"]
    _each_source(check, lower=True, cell_vel=True, time_series=False, ghost=False)

    # two areas: the largest is used and the note says so; a named one can be chosen
    layout, root = _mesh_layout()
    small = _mesh_layout(area="Small Area")[0]
    layout.update({p: v for p, v in small.items() if "Small Area" in p})
    layout["Geometry/2D Flow Areas/Small Area/Cells Center Coordinate"] = (
        np.array([[0.0, 0.0], [10.0, 0.0], [0.0, 10.0], [10.0, 10.0]]), {})
    layout[SUMM.format("Small Area") + "Maximum Water Surface"] = (
        np.array([[5.0, 5.0, 5.0, 5.0], [0.0] * 4]), {})
    layout["Geometry/2D Flow Areas/Small Area/Cells Minimum Elevation"] = (np.zeros(4), {})
    del layout["Geometry/2D Flow Areas/Small Area/Faces Cell Indexes"]
    f = _FakeH5(layout, root)
    m = hecras.read_mesh(f, want_arrival=False)
    assert m.info["area"] == "Perimeter 1" and m.n == 36
    assert any("Small Area" in n for n in m.info["notes"])
    m2 = hecras.read_mesh(f, area="small area", want_arrival=False)
    assert m2.n == 4 and m2.info["area"] == "Small Area"
    assert "velocity" not in m2.fields and any("velocity" in n for n in m2.info["notes"])
    try:
        hecras.read_mesh(f, area="Nope")
        assert False, "expected HecRasError"
    except hecras.HecRasError as e:
        assert "Perimeter 1" in str(e) and "Nope" in str(e)


@test
def hecras_import_errors_say_what_was_found():
    layout, root = _mesh_layout(min_elev=False)
    try:
        hecras.read_mesh(_FakeH5(layout, root))
        assert False, "expected HecRasError"
    except hecras.HecRasError as e:
        s = str(e)
        assert "Cells Minimum Elevation" in s and "Maximum Water Surface" in s
        assert "Cells Center Coordinate" in s          # the listing of what the file holds
    try:
        hecras.read_mesh(_FakeH5({"Other/Thing": (np.zeros(3), {})}, {}))
        assert False, "expected HecRasError"
    except hecras.HecRasError as e:
        assert "Cells Center Coordinate" in str(e) and "1 datasets" in str(e)
    if not _has_h5py():
        try:
            hecras.read_mesh("/no/such/file.p01.hdf")
            assert False, "expected HecRasError"
        except hecras.HecRasError as e:
            assert "h5py" in str(e) and "pip install h5py" in str(e)


@test
def hecras_spatial_hash_matches_brute_force():
    rng = np.random.RandomState(7)
    # an irregular mesh: a fine patch inside a coarse field
    pts = np.vstack([rng.uniform(0, 1000, (600, 2)), rng.uniform(400, 500, (900, 2))])
    x, y = pts[:, 0], pts[:, 1]
    q = rng.uniform(-100, 1100, (3000, 2))
    for h in (7.0, 40.0, 300.0):
        hs = hecras.SpatialHash(x, y, h)
        idx, d = hs.nearest(q[:, 0], q[:, 1])
        bd = np.sqrt(((q[:, None, :] - pts[None, :, :]) ** 2).sum(axis=2))
        assert np.allclose(d, bd.min(axis=1)), h
        assert np.array_equal(idx >= 0, np.isfinite(d))
        # limited distance: nothing is returned beyond it
        idx2, d2 = hs.nearest(q[:, 0], q[:, 1], max_dist=25.0)
        far = bd.min(axis=1) > 25.0
        assert (idx2[far] == -1).all() and np.allclose(d2[~far], bd.min(axis=1)[~far])
    # neighbour distance excludes the point itself
    nn, med = hecras.cell_spacing(x, y)
    bd = np.sqrt(((pts[:, None, :] - pts[None, :, :]) ** 2).sum(axis=2))
    np.fill_diagonal(bd, np.inf)
    assert np.allclose(nn, bd.min(axis=1)) and abs(med - np.median(nn)) < 1e-9


@test
def hecras_import_as_reference_feeds_the_benchmark():
    def check(src):
        # a 5 ft grid one cell wider than the mesh on the west and south edges: 5 ft cells,
        # 14 x 14, x 980 to 1050, top 2050
        like = Grid(np.zeros((14, 14)), 980.0, 2050.0, 5.0)
        ref = hecras.import_as_reference(src, like)
        assert set(ref) == {"depth", "wse", "velocity", "arrival"}
        assert all(a.shape == (14, 14) for a in ref.values())
        # grid columns 0-3 (x 982.5-997.5) are 12+ ft from any centre (7.5 for column 3, the
        # limit): outside the mesh
        assert np.isnan(ref["depth"][:, :3]).all()
        # column c >= 4 has its centre at x = 980 + 5 c + 2.5; mesh column i = floor((x-1000)/10)
        for c, want in ((4, DMAX[0]), (6, DMAX[1]), (8, DMAX[2]), (10, DMAX[3])):
            i = int((980 + 5 * c + 2.5 - 1000) // 10)
            assert i == (c - 4) // 2
            _close(ref["depth"][6, c], DMAX[i])
        assert np.isnan(ref["depth"][6, 12])                              # dry columns
        # compare: the reference against itself agrees fully; against a shallower copy it
        # does not
        m = benchmark.compare_fields({"depth": ref["depth"].copy(), "wse": ref["wse"],
                                      "velocity": ref["velocity"], "arrival": ref["arrival"]},
                                     ref, 0.002, 1.0)
        assert m["fit_index_F"] == 1.0 and m["depth_ft"]["rmse"] == 0.0
        m2 = benchmark.compare_fields({"depth": ref["depth"] - 3.0}, ref, 0.002, 1.0)
        assert m2["fit_index_F"] < 1.0 and abs(m2["depth_ft"]["bias"] + 3.0) < 1e-9
        # no overlap says so
        far = Grid(np.zeros((4, 4)), 50000.0, 60000.0, 5.0)
        try:
            hecras.import_as_reference(src, far)
            assert False, "expected HecRasError"
        except hecras.HecRasError as e:
            assert "does not overlap" in str(e)
    _each_source(check)


# ============================================================================ algorithms
def _algs():
    from nd_breach_runner.tests import fake_qgis
    fake_qgis.install()
    import nd_breach_runner.algorithms as a
    importlib.reload(a)
    import nd_breach_runner.algorithms_tools as t
    importlib.reload(t)
    import nd_breach_runner.algorithms_hecras as h
    importlib.reload(h)
    return h, fake_qgis


@test
def hecras_algorithms_initialise():
    h, _ = _algs()
    assert [c().name() for c in h.HECRAS_ALGORITHMS] == ["hecrasexport", "hecrasimport",
                                                        "hecrasmodel"]
    names = [c().displayName() for c in h.HECRAS_ALGORITHMS]
    assert names == ["Export the breach to HEC-RAS (hydrograph and breach table)",
                     "Import HEC-RAS 2D results as rasters",
                     "Build a HEC-RAS 2D dam-breach model from the run"]
    for cls in h.HECRAS_ALGORITHMS:
        a = cls()
        a.initAlgorithm()
        assert a.shortHelpString() and a.group() and len(a.params) >= 2
    assert h.STEPS[h.STEP_DEFAULT][0] == "5MIN"


@test
def hecras_export_algorithm_runs():
    h, fq = _algs()
    d = tempfile.mkdtemp()
    try:
        _fake_run(d)
        a = h.HecRasExportAlgorithm()
        a.initAlgorithm()
        fb = fq.QgsProcessingFeedback()
        step = [i for i, s in enumerate(h.STEPS) if s[0] == "15MIN"][0]
        r = a.processAlgorithm({"DAM_FOLDER": d, "STEP": step}, fq.QgsProcessingContext(), fb)
        for k in ("HYDROGRAPH_TXT", "HYDROGRAPH_CSV", "BREACH_TABLE", "BREACH_TABLE_MD"):
            assert os.path.exists(r[k]), k
        assert os.path.dirname(r["HYDROGRAPH_TXT"]) == os.path.join(d, "_hecras")
        n, vals = _parse_block(open(r["HYDROGRAPH_TXT"]).read())
        assert n == len(vals) and any("15MIN" in m for m in fb.info)
        assert "cfs" in r["RESULT"]
        # default step is 5 minutes; a chosen output folder is used
        out = os.path.join(d, "elsewhere")
        r2 = a.processAlgorithm({"DAM_FOLDER": d, "OUT_FOLDER": out},
                                fq.QgsProcessingContext(), fq.QgsProcessingFeedback())
        assert os.path.dirname(r2["BREACH_TABLE"]) == out
        assert _parse_block(open(r2["HYDROGRAPH_TXT"]).read())[0] > n
        empty = tempfile.mkdtemp()
        try:
            a.processAlgorithm({"DAM_FOLDER": empty, "STEP": step}, fq.QgsProcessingContext(),
                               fq.QgsProcessingFeedback())
            assert False, "expected QgsProcessingException"
        except fq.QgsProcessingException as e:
            assert "breach_hydrograph.csv" in str(e)
        finally:
            shutil.rmtree(empty)
    finally:
        shutil.rmtree(d)


class _Patch:
    """Swap attributes for the length of a with block (GDAL is not available in the tests)."""

    def __init__(self, *items):
        self.items, self.old = items, []

    def __enter__(self):
        for obj, name, val in self.items:
            self.old.append((obj, name, getattr(obj, name)))
            setattr(obj, name, val)

    def __exit__(self, *a):
        for obj, name, val in reversed(self.old):
            setattr(obj, name, val)


@test
def hecras_import_algorithm_runs_with_and_without_benchmark():
    h, fq = _algs()
    layout, root = _mesh_layout()
    fake = _FakeH5(layout, root)
    d = tempfile.mkdtemp()
    like = Grid(np.zeros((6, 6)), 1000.0, 2060.0, 10.0, crs_wkt=WKT)
    ours_depth = _expected_columns(DMAX)
    try:
        with _Patch((hecras, "_open_hdf", lambda p: (fake, lambda: None)),
                    (h.gdal_io, "GeoTiffWriter", lambda crs: npy_writer),
                    (h.gdal_io, "read_grid", lambda p: like),
                    (h.gdal_io, "read_like", lambda p, g, alg="bilinear": ours_depth.copy()),
                    (h.gdal_io, "linear_unit_m", lambda wkt: 0.3048),
                    (h._ToolBase, "_need_gdal", lambda self: None)):
            a = h.HecRasImportAlgorithm()
            a.initAlgorithm()
            # 1. no raster to match: the grid comes from the mesh
            out = os.path.join(d, "out1")
            fb = fq.QgsProcessingFeedback()
            ctx = fq.QgsProcessingContext()
            r = a.processAlgorithm({"HDF": "run.p01.hdf", "OUT_FOLDER": out}, ctx, fb)
            assert os.path.basename(r["DEPTH"]) == "HECRAS Depth (Max).tif"
            for name in ("HECRAS Depth (Max)", "HECRAS Velocity (Max)", "HECRAS Arrival Time",
                         "HECRAS WSE (Max)"):
                assert os.path.exists(os.path.join(out, name + ".tif")), name
            _nan_eq(np.load(os.path.join(out, "HECRAS Depth (Max).npy")),
                    _expected_columns(DMAX))
            meta = json.load(open(os.path.join(out, "HECRAS Depth (Max).json")))
            assert meta == {"x0": 1000.0, "y0": 2060.0, "dx": 10.0}
            assert any("Perimeter 1" in m and "36 cells" in m for m in fb.info)
            assert len(ctx.loaded) >= 4 and r["SUMMARY"] == ""
            # 2. a dam folder's Depth (Max).tif sets the grid, and the benchmark runs
            dam = os.path.join(d, "dam")
            os.makedirs(dam)
            open(os.path.join(dam, "Depth (Max).tif"), "w").write("x")
            fb = fq.QgsProcessingFeedback()
            r = a.processAlgorithm({"HDF": "run.p01.hdf", "DAM_FOLDER": dam, "COMPARE": True,
                                    "T0_HR": 0.0}, fq.QgsProcessingContext(), fb)
            assert r["FOLDER"] == os.path.join(dam, "_hecras")
            assert os.path.exists(r["SUMMARY"]) and "extents agree closely" in r["RESULT"]
            assert os.path.exists(os.path.join(dam, "_hecras", "benchmark",
                                               "Benchmark_Report.html"))
            # 3. errors are clean
            for bad, msg in (({"HDF": "x.hdf"}, "dam folder or an output folder"),
                             ({"HDF": "x.hdf", "DAM_FOLDER": d, "COMPARE": True},
                              "Depth (Max).tif")):
                try:
                    a.processAlgorithm(bad, fq.QgsProcessingContext(),
                                       fq.QgsProcessingFeedback())
                    assert False, "expected QgsProcessingException"
                except fq.QgsProcessingException as e:
                    assert msg in str(e), str(e)
            # a file with the datasets missing reports what it holds
            bad_layout = {p: v for p, v in layout.items() if "Minimum Elevation" not in p}
            bad = _FakeH5(bad_layout, root)
            with _Patch((hecras, "_open_hdf", lambda p: (bad, lambda: None))):
                try:
                    a.processAlgorithm({"HDF": "x.hdf", "OUT_FOLDER": out},
                                       fq.QgsProcessingContext(), fq.QgsProcessingFeedback())
                    assert False, "expected QgsProcessingException"
                except fq.QgsProcessingException as e:
                    assert "Cells Minimum Elevation" in str(e)
    finally:
        shutil.rmtree(d)
