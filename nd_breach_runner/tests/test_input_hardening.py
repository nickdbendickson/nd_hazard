"""Bad input gives a plain message, not a freeze or a raw Python error (security audit pass 3):
the shared xlsx reader is bounded (damsuite.xlsx_lite 1.5.0), a fatality / curve CSV row with
extra values, a damaged depth snapshot file and cut-short built-in engine input files."""
import io
import os
import shutil
import tempfile
import time
import zipfile

import numpy as np

from nd_breach_runner.tests.registry import test as register  # "test" would be collected by pytest


def _tmp():
    return tempfile.mkdtemp(prefix="hardening_")


def _raises(exc, fn, *args, **kw):
    try:
        fn(*args, **kw)
    except exc as e:
        return str(e)
    raise AssertionError("{} not raised".format(exc.__name__))


# ---------------------------------------------------------------------------------------
# damsuite.xlsx_lite: a tiny file naming a far-away cell must not build a giant table
# ---------------------------------------------------------------------------------------
def _xlsx_with(d, row, ref):
    from nd_breach_runner.damsuite import xlsx_lite as X
    good = os.path.join(d, "good.xlsx")
    X.write(good, [("Settings", [["plugin", "tool", "key", "value"], ["a", "b", "c", 1.5]])])
    bad = os.path.join(d, "bad_{}_{}.xlsx".format(row, ref))
    with zipfile.ZipFile(good) as zi, zipfile.ZipFile(bad, "w", zipfile.ZIP_DEFLATED) as zo:
        for n in zi.namelist():
            b = zi.read(n)
            if n.endswith("sheet1.xml"):
                b = b.replace(b'<row r="2">', '<row r="{}">'.format(row).encode())
                b = b.replace(b'r="A2"', 'r="{}"'.format(ref).encode())
            zo.writestr(n, b)
    return good, bad


@register
def xlsx_reader_refuses_a_cell_at_row_fifty_million_at_once():
    from nd_breach_runner.damsuite import xlsx_lite as X
    d = _tmp()
    try:
        good, bad = _xlsx_with(d, 50000000, "A50000000")
        assert X.read(good)["Settings"][1] == ["a", "b", "c", 1.5]
        assert os.path.getsize(bad) < 4000
        t0 = time.time()
        msg = _raises(X.XlsxTooLarge, X.read, bad)
        assert time.time() - t0 < 2.0
        assert "50,000,000" in msg and "1,048,576 rows" in msg, msg
        assert issubclass(X.XlsxTooLarge, ValueError)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@register
def xlsx_reader_refuses_too_many_columns_and_cells():
    from nd_breach_runner.damsuite import xlsx_lite as X
    d = _tmp()
    try:
        for row, ref in ((2, "XFE2"), (2, "ZZZZZZZZZZZZ2")):
            _good, bad = _xlsx_with(d, row, ref)
            assert "past column XFD" in _raises(X.XlsxTooLarge, X.read, bad)
        _good, bad = _xlsx_with(d, 100000, "ZZ100000")     # each in range, the table is not
        assert "100,000 x 702 table" in _raises(X.XlsxTooLarge, X.read, bad)
        _good, ok = _xlsx_with(d, 1500, "ZZ1500")            # 1500 x 702 is allowed
        assert len(X.read(ok)["Settings"]) == 1500
    finally:
        shutil.rmtree(d, ignore_errors=True)


@register
def xlsx_reader_refuses_a_part_that_unpacks_too_large():
    from nd_breach_runner.damsuite import xlsx_lite as X
    d = _tmp()
    try:
        good, _bad = _xlsx_with(d, 2, "A2")
        bomb = os.path.join(d, "bomb.xlsx")
        with zipfile.ZipFile(good) as zi, zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as zo:
            for n in zi.namelist():
                zo.writestr(n, zi.read(n))
            zo.writestr("xl/sharedStrings.xml", b" " * (X.MAX_PART_BYTES + 1))
        assert os.path.getsize(bomb) < 200000
        assert "sharedStrings.xml unpacks to" in _raises(X.XlsxTooLarge, X.read, bomb)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@register
def settings_load_turns_the_huge_xlsx_into_a_settings_error():
    from nd_breach_runner.damsuite import settings as DS
    d = _tmp()
    try:
        _good, bad = _xlsx_with(d, 50000000, "A50000000")
        p = os.path.join(d, "damsuite_settings.xlsx")
        shutil.copy(bad, p)
        msg = _raises(DS.SettingsError, DS.Settings.load, p)
        assert "cannot be read" in msg and "50,000,000" in msg, msg
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------------------
# consequences: a CSV row with more values than the header
# ---------------------------------------------------------------------------------------
@register
def fatality_csv_with_extra_columns_is_a_plain_error():
    from nd_breach_runner.core import consequences as cons
    d = _tmp()
    try:
        p = os.path.join(d, "fatality.csv")
        with open(p, "w") as f:
            f.write("severity,none,short,some,adequate\n"
                    "low,0.01,0.005,0.002,0.0002\n"
                    "medium,0.1,0.05,0.02,0.002,0.9,extra\n"
                    "high,0.75,0.5,0.3,0.03\n")
        msg = _raises(ValueError, cons.load_fatality_csv, p)
        assert "fatality.csv line 3" in msg and "7 values" in msg and "5 columns" in msg, msg
        with open(p, "w") as f:
            f.write("severity,none,short,some,adequate\n"
                    "low,0.01,0.005,0.002,0.0002\n"
                    "medium,0.1,0.05,0.02,0.002\n"
                    "high,0.75,0.5,0.3,0.03\n")
        assert set(cons.load_fatality_csv(p)) == {"low", "medium", "high"}
        c = os.path.join(d, "curves.csv")
        with open(c, "w") as f:
            f.write("occupancy,depth_ft,structure_pct,contents_pct\nres1,0,0,0,7\n")
        assert "line 2" in _raises(ValueError, cons.load_curves_csv, c)
        with open(c, "w") as f:
            f.write("occupancy,depth_ft\n\"res1,0\n")             # an unclosed quote
        _raises(ValueError, cons.load_curves_csv, c)
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------------------
# animation: a damaged depth.npy inside the snapshot file
# ---------------------------------------------------------------------------------------
def _npz(d, name, depth_bytes):
    p = os.path.join(d, name + ".npz")
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        b = io.BytesIO()
        np.save(b, np.arange(5) * 600.0)
        z.writestr("times_s.npy", b.getvalue())
        z.writestr("depth.npy", depth_bytes)
    return p


@register
def damaged_depth_snapshots_raise_a_snapshot_error():
    from nd_breach_runner.core import animation as an
    d = _tmp()
    try:
        depth = np.random.default_rng(1).random((5, 20, 30)).astype(np.float32)
        raw = io.BytesIO()
        np.save(raw, depth)
        raw = raw.getvalue()

        def header(text):
            return raw[:10] + text.encode().ljust(117) + b"\n" + raw[128:]
        good = _npz(d, "good", raw)
        assert an.npz_info(good)["n"] == 5 and len(list(an.iter_depth_frames(good))) == 5
        cases = {
            "garbage": b"hello world" * 10, "empty": b"", "short_header": raw[:20],
            "half": raw[:len(raw) // 2],
            "two_d": header("{'descr': '<f4', 'fortran_order': False, 'shape': (5, 20), }"),
            "huge": header("{'descr': '<f4', 'fortran_order': False, "
                           "'shape': (5, 2000000, 3000000), }"),
            "objects": header("{'descr': '|O', 'fortran_order': False, 'shape': (5, 20, 30), }"),
            "negative": header("{'descr': '<f4', 'fortran_order': False, "
                               "'shape': (5, -20, 30), }"),
        }
        for k, v in cases.items():
            p = _npz(d, k, v)
            msg = _raises(an.SnapshotError, an.npz_info, p)
            assert "is damaged or incomplete" in msg and "Run the breach model again" in msg, msg
            _raises(an.SnapshotError, lambda: list(an.iter_depth_frames(p)))
        # a bad CRC in the compressed depth stream shows up while the frames are read
        b = bytearray(open(good, "rb").read())
        i = b.find(b"depth.npy") + 200
        b[i:i + 50] = b"\0" * 50
        crc = os.path.join(d, "crc.npz")
        with open(crc, "wb") as f:
            f.write(bytes(b))
        try:
            an.npz_info(crc)
            _raises(an.SnapshotError, lambda: list(an.iter_depth_frames(crc)))
        except an.SnapshotError:
            pass
        assert issubclass(an.SnapshotError, ValueError)
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------------------
# built-in engine: cut-short model files
# ---------------------------------------------------------------------------------------
@register
def builtin_engine_reports_truncated_input_files_by_name():
    from nd_breach_runner.core import builtin_lisflood as bl, lisflood as lf
    d = _tmp()
    try:
        assert lf.check_lisflood(lf.BUILTIN_EXE, os.path.join(d, "b"))["ok"]
        src = os.path.join(d, "b")

        def cut(name, keep, extra_par=""):
            w = os.path.join(d, "{}_{}".format(name.replace(".", "_"), keep))
            shutil.copytree(src, w, ignore=shutil.ignore_patterns("results"))
            p = os.path.join(w, name)
            if name == "run.gauge":
                with open(p, "w") as f:
                    f.write("3\n30.0 210.0 E 10.0\n")
            else:
                with open(p, "rb") as f:
                    b = f.read()
                with open(p, "wb") as f:
                    f.write(b[:keep] if keep >= 0 else b[:len(b) + keep])
            if extra_par:
                with open(os.path.join(w, "run.par"), "a") as f:
                    f.write(extra_par)
            return os.path.join(w, "run.par")

        cases = [("run.dem.asc", 40, "DEM file run.dem.asc is truncated"),
                 ("run.dem.asc", -200, "DEM file run.dem.asc is truncated"),
                 ("run.bdy", -300, "inflow (.bdy) file run.bdy is truncated"),
                 ("run.bdy", 70, "inflow (.bdy) file run.bdy is truncated"),
                 ("run.gauge", 0, "gauge file run.gauge is truncated")]
        for name, keep, words in cases:
            par = cut(name, keep, "gaugefile run.gauge\n" if name == "run.gauge" else "")
            msg = _raises(bl.EngineError, bl.run_par, par)
            assert words in msg, (name, keep, msg)
        par = cut("run.par", -1, "\nsim_time 12oo\n")
        assert "'sim_time' is not a number" in _raises(bl.EngineError, bl.run_par, par)
        # through the Runner's own driver the user sees a ModelError, not a raw traceback
        par = cut("run.dem.asc", 41)
        msg = _raises(lf.ModelError, lf._run_builtin, os.path.dirname(par), par, None)
        assert "built-in engine: the DEM file" in msg, msg
    finally:
        shutil.rmtree(d, ignore_errors=True)
