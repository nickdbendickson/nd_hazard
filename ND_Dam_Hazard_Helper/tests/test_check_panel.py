# -*- coding: utf-8 -*-
"""The severity-ranked "Check inputs" panel (ui_check_panel.py), ported from
DamSafety_Hazard.

    QT_QPA_PLATFORM=offscreen python3 ND_Dam_Hazard_Helper/tests/test_check_panel.py
    SHOTS=<folder> also saves screenshots of the three states.

The pure-panel cases (error / warning / ok / reset / wiring) need only an
off-screen Qt: real PyQGIS is used when it imports, otherwise a real PyQt
binding (PyQt5 / PyQt6) stands in under ``qgis.PyQt`` with fake_qgis's stubs for
``qgis.core`` / ``osgeo`` (the panel builds only Qt widgets - it never touches
GDAL or QGIS). The GDAL smoke test and the full-dialog wiring test need real
PyQGIS and skip without it. With no Qt binding at all the whole file skips.
"""
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))           # the folder holding ND_Dam_Hazard_Helper
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# the three check levels are plain strings in checks.py; name them here so the
# test data does not force an import of checks (and its GDAL / QGIS chain) just
# to be collected.
ERROR, WARNING, OK = "ERROR", "WARNING", "OK"

ERRS = [(ERROR, "depth values look like water-surface ELEVATIONS (median 1,512) - give the depth raster"),
        (ERROR, "no velocity raster: ACER zones use max depth with max velocity")]
WARNS = [(WARNING, "no arrival raster - no warning times"),
         (WARNING, "maximum depth 180 ft - check the units")]
OKS = [(OK, "depth: 1,000 wet cells"), (OK, "velocity: fine"), (OK, "outlines found")]

_QT = None          # qgis.PyQt.QtWidgets once a usable (real) Qt is available
_PANEL = None       # CheckPanel
_COLL = None        # Collapsible
_APP = None         # the QApplication - kept alive for the life of the process


def _install_env():
    """Make a usable Qt available under ``qgis.PyQt``. Returns 'real' (real
    PyQGIS imports), 'hybrid' (fake qgis.core / osgeo plus a real PyQt binding,
    enough for the pure-panel cases) or 'none'."""
    try:
        import qgis.core            # noqa: F401 - real PyQGIS
        import qgis.PyQt.QtWidgets  # noqa: F401
        return "real"
    except Exception:                                            # noqa - no PyQGIS here
        pass
    import importlib
    real_qt = None
    for binding in ("PyQt5", "PyQt6"):
        try:
            for sub in ("QtCore", "QtGui", "QtWidgets"):
                importlib.import_module(binding + "." + sub)
            real_qt = binding
            break
        except Exception:                                        # noqa
            continue
    if real_qt is None:
        return "none"
    from ND_Dam_Hazard_Helper.tests import fake_qgis
    fake_qgis.install()                      # stubs qgis.*, qgis.PyQt.*, osgeo.*
    import qgis.PyQt as _pyqt
    for sub in ("QtCore", "QtGui", "QtWidgets"):      # re-point qgis.PyQt at the real binding
        mod = importlib.import_module(real_qt + "." + sub)
        sys.modules["qgis.PyQt." + sub] = mod
        setattr(_pyqt, sub, mod)
    return "hybrid"


def _ensure():
    """Load CheckPanel against a real off-screen Qt. True when ready; False (and
    the caller skips) when only the fake stand-ins or no Qt are available."""
    global _QT, _PANEL, _COLL, _APP
    if _PANEL is not None:
        return True
    try:
        from qgis.PyQt import QtWidgets
    except Exception:                                            # noqa
        return False
    try:                                     # a real QLabel round-trips its text; a fake does not
        # keep the app in a module global: an unreferenced QApplication is
        # garbage-collected and the next QWidget then aborts the process
        _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
        probe = QtWidgets.QLabel()
        probe.setText("x")
        if probe.text() != "x":
            return False
    except Exception:                                            # noqa
        return False
    try:
        from ND_Dam_Hazard_Helper.ui_check_panel import CheckPanel, Collapsible
    except Exception:                                            # noqa
        return False
    _QT, _PANEL, _COLL = QtWidgets, CheckPanel, Collapsible
    return True


def _have_real_qgis():
    """True only when real PyQGIS (not the fake_qgis stand-in) is importable."""
    try:
        from qgis.core import QgsApplication
        return "fake_qgis" not in getattr(QgsApplication, "__module__", "")
    except Exception:                                            # noqa
        return False


def _make():
    QtW = _QT
    host = QtW.QWidget()
    host.resize(640, 10)
    v = QtW.QVBoxLayout(host)
    row = QtW.QHBoxLayout()
    chk, prim = QtW.QPushButton("Check inputs"), QtW.QPushButton("Classify")
    prim.setToolTip("base tip")
    row.addWidget(chk)
    row.addWidget(prim)
    row.addStretch()
    v.addLayout(row)
    panel = _PANEL(chk, prim, action="classifying")
    v.addWidget(panel)
    v.addStretch()
    return host, panel, chk, prim


def _texts(w):
    return [lbl.text() for lbl in w.findChildren(_QT.QLabel)]


def _shot(host, name):
    folder = os.environ.get("SHOTS")
    if folder:
        host.show()
        host.adjustSize()
        _QT.QApplication.instance().processEvents()
        host.grab().save(os.path.join(folder, name))


def test_errors_block():
    if not _ensure():
        print("    SKIP test_errors_block: no off-screen Qt")
        return
    host, panel, chk, prim = _make()
    panel.set_results(ERRS + WARNS + OKS)
    assert panel.blocked and panel.n_errors == 2 and panel.n_warnings == 2
    assert chk.text() == "Check inputs ⚠ 2"
    assert not prim.isEnabled() and "Blocked" in prim.toolTip()
    t = _texts(panel)
    assert "2 errors must be fixed before classifying" in t
    assert any("ELEVATIONS" in x for x in t)
    _shot(host, "check_panel_error.png")


def test_warnings_only():
    if not _ensure():
        print("    SKIP test_warnings_only: no off-screen Qt")
        return
    host, panel, chk, prim = _make()
    panel.set_results(WARNS + OKS)
    assert prim.isEnabled() and chk.text() == "Check inputs ◐ 2"
    assert "2 warnings" in prim.toolTip()
    assert _texts(panel).count("WARN") == 2
    boxes = panel.findChildren(_COLL)
    titles = [b.title() for b in boxes]
    assert "2 warnings — the run proceeds, check them" in titles
    assert "3 checks passed" in titles
    _shot(host, "check_panel_warning.png")


def test_all_ok_and_reset():
    if not _ensure():
        print("    SKIP test_all_ok_and_reset: no off-screen Qt")
        return
    host, panel, chk, prim = _make()
    panel.set_results(ERRS)
    panel.set_results(OKS)                  # re-call resets everything
    assert prim.isEnabled() and prim.toolTip() == "base tip"
    assert chk.text() == "Check inputs" and not panel.blocked
    assert "WARN" not in _texts(panel) and not any("must be fixed" in x for x in _texts(panel))
    (box,) = panel.findChildren(_COLL)
    assert not box.is_expanded() and box.header.text() == "▸ 3 checks passed"
    _shot(host, "check_panel_ok.png")
    for _ in range(3):                      # header text stays right across toggles
        box.header.toggle()
    assert box.is_expanded() and box.header.text() == "▾ 3 checks passed"
    box.header.toggle()
    assert box.header.text() == "▸ 3 checks passed"
    panel.set_results(ERRS)
    panel.clear()
    assert prim.isEnabled() and chk.text() == "Check inputs" and panel.isHidden()


def test_wiring():
    """Pure-panel wiring: the panel drives the buttons it was given, and clear()
    (fired by an input change in the dialog) puts them back."""
    if not _ensure():
        print("    SKIP test_wiring: no off-screen Qt")
        return
    host, panel, chk, prim = _make()
    assert panel.check_button is chk and panel.primary_button is prim
    panel.set_results(ERRS)
    assert not prim.isEnabled() and chk.text() == "Check inputs ⚠ 2" and not panel.isHidden()
    panel.clear()                           # an input change clears it
    assert prim.isEnabled() and chk.text() == "Check inputs" and panel.isHidden()
    panel.set_results(WARNS)                # a warning does not disable the primary button
    assert prim.isEnabled() and not panel.isHidden()
    panel.set_results([])                   # empty results hide the panel again
    assert panel.isHidden() and chk.text() == "Check inputs"


def _raster(path, arr):
    from osgeo import gdal, osr
    ds = gdal.GetDriverByName("GTiff").Create(path, arr.shape[1], arr.shape[0], 1, gdal.GDT_Float32)
    ds.SetGeoTransform((600000, 10, 0, 5200000, 0, -10))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(26914)
    ds.SetProjection(srs.ExportToWkt())
    b = ds.GetRasterBand(1)
    b.WriteArray(arr)
    b.SetNoDataValue(-9999)
    ds = None


def test_smoke_wse_raster_as_depth():
    """A water-surface-elevation raster given as depth fires the red banner.
    Needs real PyQGIS + GDAL - skipped otherwise."""
    if not (_have_real_qgis() and _ensure()):
        print("    SKIP test_smoke_wse_raster_as_depth: needs real PyQGIS / GDAL")
        return
    import tempfile
    import numpy as np
    from ND_Dam_Hazard_Helper import checks as CK
    d = tempfile.mkdtemp()
    dep, vel = os.path.join(d, "wse.tif"), os.path.join(d, "vel.tif")
    _raster(dep, 1500 + np.random.RandomState(1).rand(50, 50).astype("float32") * 20)
    _raster(vel, np.full((50, 50), 3.0, "float32"))
    res = CK.check_all({"depth": dep, "vel": vel}, cfg={})
    assert any(l == CK.ERROR and "ELEVATIONS" in t for l, t in res), res
    host, panel, chk, prim = _make()
    panel.set_results(res)
    assert panel.blocked and not prim.isEnabled() and chk.text().startswith("Check inputs ⚠")
    assert any("ELEVATIONS" in x for x in _texts(panel))
    _shot(host, "check_panel_smoke_wse.png")


def test_dialog_wiring():
    """The dialog wires the panel to check_btn / classify_btn and an input change
    clears it. Needs the full dialog (real PyQGIS) - skipped otherwise."""
    if not _have_real_qgis():
        print("    SKIP test_dialog_wiring: needs real PyQGIS")
        return
    from ND_Dam_Hazard_Helper.dialog import NDDamHazardDialog
    dlg = NDDamHazardDialog()
    assert dlg.check_panel.check_button is dlg.check_btn
    assert dlg.check_panel.primary_button is dlg.classify_btn
    dlg.check_panel.set_results(ERRS)
    assert not dlg.classify_btn.isEnabled()
    dlg.depth_edit.setText(dlg.depth_edit.text() + "x")    # an input change clears it
    assert dlg.classify_btn.isEnabled() and dlg.check_btn.text() == "Check inputs"
    dlg.close()


if __name__ == "__main__":
    mode = _install_env()
    print("Qt: {} ({})".format(mode, sys.version.split()[0]))
    if mode == "none":
        print("SKIP: no PyQt binding (PyQt5 / PyQt6) - the pure-panel cases need off-screen Qt")
        sys.exit(0)
    n = fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            n += 1
            try:
                fn()
                print("ok    ", name)
            except Exception:
                fails += 1
                print("FAIL  ", name)
                traceback.print_exc()
    print("\n{} tests, {} failed".format(n, fails))
    sys.exit(1 if fails else 0)
