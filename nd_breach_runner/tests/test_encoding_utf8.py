"""UTF-8 hardening of the text report / CSV / JSON open() calls.

nd_breach_runner.core.{breach,consequences,workflow} write reports, CSVs and
JSON and read back CSV / checkpoint files.  When those open() calls do not pass
encoding=, Python uses the platform default (cp1252 on Windows), so a non-ASCII
dam name or symbol raises UnicodeEncodeError on write (or mojibake / a decode
error on read).  DWR's DamSafety_Runner passes encoding="utf-8"; this test is
the nd-side regression guard.

Fail-before / pass-after mechanism: PEP 597 EncodingWarning.  Run the
interpreter with -X warn_default_encoding and treat EncodingWarning as an
error; any open() in the exercised paths that omits encoding then raises.  The
__main__ block re-executes itself with the flag so `python3 test_encoding_utf8.py`
also works.  (workflow.py pulls in the LISFLOOD / scipy / PyQGIS stack and is
not importable in this sandbox, so its identical open() changes are covered by
py_compile, not here.)
"""
import os
import sys
import tempfile
import warnings

# A dam id / label with non-ASCII characters that cp1252 can and cannot encode.
NON_ASCII = "Dåm — Ñódak ☀"   # Dåm — Ñódak ☀


def _repo_root():
    # .../nd_breach_runner/tests/test_encoding_utf8.py -> repo root is three up
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _run():
    sys.path.insert(0, _repo_root())
    import numpy as np
    from nd_breach_runner.core import breach as brmod
    from nd_breach_runner.core import consequences as cs
    from nd_breach_runner.core.grid import Grid

    tmp = tempfile.mkdtemp(prefix="nd_enc_")

    # a 4x4 synthetic flood (as in test_consequences.py), one wet structure whose
    # id carries the non-ASCII text
    g = Grid(np.zeros((4, 4)), 0.0, 40.0, 10.0)
    d = np.zeros((4, 4)); v = np.full((4, 4), 1.0); a = np.full((4, 4), np.nan)
    d[0, :3] = [3.0, 5.0, 2.0]; v[0, :3] = [1.0, 2.0, 40.0]; a[0, :3] = [2.0, 0.1, 0.5]
    fields = {"depth": d, "velocity": v, "arrival": a}
    p = cs.ConsequenceParams()
    structs = [cs.make_structure(NON_ASCII, 5, 35, p, occupancy="residential",
                                 occupants_night=4, structure_value=200000, ffh_ft=1.0)]
    rows, summary = cs.assess(structs, g, fields, params=p)
    # drop the optional blocks whose writers (lifeloss / crossings) are not the
    # target of this test and may pull extra imports
    for k in ("par", "crossings", "roads"):
        summary.pop(k, None)

    # a tiny hydrograph for breach.to_csv / read_csv_series
    hy = brmod.Hydrograph(np.array([0.0, 1800.0, 3600.0]),
                          np.array([0.0, 100.0, 0.0]), brmod.METHOD_PIPING)
    hy_csv = os.path.join(tmp, "breach_hydrograph.csv")

    # Any default-encoding open() in the exercised code now raises.
    with warnings.catch_warnings():
        warnings.simplefilter("error", EncodingWarning)
        # consequences: CSV (write_rows_csv), markdown and JSON writers
        out = cs.write_outputs(tmp, rows, summary, crs=None, geojson=False)
        # breach: to_csv (write) and read_csv_series (read)
        hy.to_csv(hy_csv)
        tt, qq = brmod.read_csv_series(hy_csv, "hydrograph", brmod.CFS, 3600.0)

    # the non-ASCII id (a per-structure value) survived the UTF-8 round trip in
    # the structures CSV, which is the text product that carries it verbatim
    with open(out["csv"], encoding="utf-8") as f:
        assert NON_ASCII in f.read(), "non-ASCII id missing from structures CSV"
    # the JSON and markdown summaries must at least decode cleanly as UTF-8
    import json
    with open(out["json"], encoding="utf-8") as f:
        assert isinstance(json.load(f), dict), "JSON summary did not parse"
    with open(out["markdown"], encoding="utf-8") as f:
        f.read()  # must decode as UTF-8 without error
    assert len(tt) >= 2 and len(qq) >= 2, "hydrograph CSV did not round-trip"
    return True


def test_text_outputs_are_utf8():
    """write/read paths pass encoding='utf-8' (no EncodingWarning, round-trips)."""
    assert _run()


if __name__ == "__main__":
    # Re-exec with -X warn_default_encoding so plain `python3 test_...py` also
    # exercises the fail-before/pass-after behaviour.
    if not getattr(sys.flags, "warn_default_encoding", 0):
        os.execv(sys.executable,
                 [sys.executable, "-I", "-X", "warn_default_encoding", __file__])
    test_text_outputs_are_utf8()
    print("PASS: text outputs are UTF-8 (no default-encoding open in the write/read paths)")
