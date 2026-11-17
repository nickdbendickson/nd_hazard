"""Hazard Helper: DSS-WISE package extraction refuses a decompression bomb (per-entry, total and
entry-count caps), a symlink member and a path-traversal member, and bounds nested-zip depth -
while a normal package still imports unchanged.

    python3 -m ND_Dam_Hazard_Helper.tests.run_tests dsswise_zip
"""
import io
import os
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
from ND_Dam_Hazard_Helper.tests.registry import test  # noqa: E402


def _D():
    from ND_Dam_Hazard_Helper import dsswise as D
    return D


def _zip(path, members):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members:
            z.writestr(name, data)
    return path


@test
def dsswise_zip_caps_refuse_a_bomb():
    D = _D()
    saved = (D.MAX_ENTRY_BYTES, D.MAX_TOTAL_BYTES, D.MAX_ZIP_ENTRIES)
    D.MAX_ENTRY_BYTES, D.MAX_TOTAL_BYTES, D.MAX_ZIP_ENTRIES = 2000, 5000, 5
    try:
        with tempfile.TemporaryDirectory() as t:
            # a legit small package still imports
            z = _zip(os.path.join(t, "ok.zip"), [("a.tif", b"x" * 100), ("b.csv", b"y" * 50)])
            out = D.open_package(z, os.path.join(t, "out1"))
            assert os.path.isfile(os.path.join(out, "a.tif"))
            for label, members in (
                ("per-entry", [("big.tif", b"x" * 3000)]),
                ("total", [("a", b"x" * 1800), ("b", b"x" * 1800), ("c", b"x" * 1800)]),
                ("entry-count", [(str(i), b"z") for i in range(7)]),
            ):
                z = _zip(os.path.join(t, label + ".zip"), members)
                raised = False
                try:
                    D.open_package(z, os.path.join(t, "o_" + label))
                except ValueError:
                    raised = True
                assert raised, "{} bomb was not refused".format(label)
    finally:
        D.MAX_ENTRY_BYTES, D.MAX_TOTAL_BYTES, D.MAX_ZIP_ENTRIES = saved


@test
def dsswise_zip_skips_traversal_and_symlink_members():
    D = _D()
    with tempfile.TemporaryDirectory() as t:
        z = _zip(os.path.join(t, "slip.zip"), [("ok.tif", b"y"), ("../escape.txt", b"x")])
        out = D.open_package(z, os.path.join(t, "out"))
        assert os.path.isfile(os.path.join(out, "ok.tif")), "legit member missing"
        assert not os.path.exists(os.path.join(os.path.dirname(out), "escape.txt")), "traversal member written"
        # a symlink member is skipped, not materialized
        sp = os.path.join(t, "lnk.zip")
        with zipfile.ZipFile(sp, "w") as zf:
            info = zipfile.ZipInfo("evil_link")
            info.external_attr = 0o120777 << 16   # S_IFLNK
            zf.writestr(info, "/etc/passwd")
            zf.writestr("real.tif", b"z")
        out = D.open_package(sp, os.path.join(t, "out2"))
        assert not os.path.lexists(os.path.join(out, "evil_link")), "symlink member extracted"
        assert os.path.isfile(os.path.join(out, "real.tif"))


@test
def dsswise_zip_depth_is_bounded():
    D = _D()
    saved = D.MAX_ZIP_DEPTH
    D.MAX_ZIP_DEPTH = 1
    try:
        with tempfile.TemporaryDirectory() as t:
            inner = io.BytesIO()
            with zipfile.ZipFile(inner, "w") as z:
                z.writestr("leaf.tif", b"x")
            mid = io.BytesIO()
            with zipfile.ZipFile(mid, "w") as z:
                z.writestr("inner.zip", inner.getvalue())
            z = _zip(os.path.join(t, "outer.zip"), [("mid.zip", mid.getvalue())])
            out = D.open_package(z, os.path.join(t, "out"))
            found = [f for _dp, _d, fs in os.walk(out) for f in fs if f == "leaf.tif"]
            assert not found, "a zip nested past the depth cap was extracted"
    finally:
        D.MAX_ZIP_DEPTH = saved


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("dsswise_zip_")]:
        fn()
        print("ok", fn.__name__)
