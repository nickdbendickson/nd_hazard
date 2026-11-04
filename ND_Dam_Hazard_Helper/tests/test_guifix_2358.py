"""QGIS Dev's 800x600 GUI review (11/03/2026): the windows fit an 800x600 screen (and a 768 px
laptop). Needs real PyQGIS - skipped otherwise.

    python3 -m ND_Dam_Hazard_Helper.tests.run_tests guifix_2358
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
from ND_Dam_Hazard_Helper.tests import fake_qgis  # noqa: E402
from ND_Dam_Hazard_Helper.tests.registry import Skip, test  # noqa: E402

MODE = fake_qgis.install()


_APP = []


def _keep(app):
    _APP.append(app)
    return app


def _app():
    if MODE != "real":
        raise Skip("Qt widgets need a real PyQGIS")
    from qgis.core import QgsApplication
    return QgsApplication.instance() or _keep(QgsApplication([], True))


@test
def guifix_helper_window_minimum_fits_800x600():
    _app()
    from ND_Dam_Hazard_Helper.dialog import NDDamHazardDialog
    dlg = NDDamHazardDialog()
    try:
        assert dlg.minimumWidth() <= 780 and dlg.minimumHeight() <= 560, dlg.minimumSize()
    finally:
        dlg.close()


@test
def guifix_long_tick_boxes_keep_their_head_and_the_sentence_in_the_tooltip():
    _app()
    from qgis.PyQt.QtWidgets import QCheckBox, QWidget, QVBoxLayout
    from ND_Dam_Hazard_Helper import data_tab as DT
    long = ("Lifeline facilities - hospitals, care homes, schools, fire / EMS stations, "
            "wastewater plants and every other kind of facility the pack holds")
    page = QWidget()
    lay = QVBoxLayout(page)
    a, b = QCheckBox(long), QCheckBox("Short")
    a.setToolTip("old tip")
    lay.addWidget(a)
    lay.addWidget(b)
    done = DT.fit_check_texts(page)
    assert done == [a], done
    assert a.text() == "Lifeline facilities", a.text()
    assert a.sizeHint().width() <= DT.CHECK_MAX_PX
    assert a.toolTip().startswith(long) and a.toolTip().endswith("old tip")
    assert b.text() == "Short"
    assert DT._short_text("x" * 100).endswith("...")
    from qgis.PyQt.QtWidgets import QComboBox
    co = QComboBox()
    co.addItem("Study area - depth raster extent + study-area buffer, " * 3)
    lay.addWidget(co)
    assert co.minimumSizeHint().width() > DT.CHECK_MAX_PX
    assert co in DT.fit_check_texts(page)
    assert co.minimumWidth() <= DT.CHECK_MAX_PX and co.sizeHint().width() <= DT.CHECK_MAX_PX


@test
def guifix_suite_data_window_scrolls_and_fits_800x600():
    _app()
    from qgis.PyQt.QtWidgets import QScrollArea
    from ND_Dam_Hazard_Helper import suite_data as SDW
    dlg = SDW.SuiteDataDialog()
    try:
        assert dlg.minimumWidth() <= 780, dlg.minimumWidth()
        assert dlg.findChildren(QScrollArea), "the rows are not in a scroll area"
        m = dlg.minimumSizeHint()
        assert m.width() <= 800 and m.height() <= 560, m
    finally:
        dlg.close()


@test
def guifix_runner_dem_box_rows_scroll_instead_of_being_crushed():
    _app()
    try:
        from nd_breach_runner import dialog as RD
    except Exception as e:                                       # noqa - not beside this plugin
        raise Skip("nd_breach_runner is not importable here: {}".format(e))
    from qgis.PyQt.QtWidgets import QScrollArea
    dlg = RD.NDBreachRunnerDialog()
    try:
        assert dlg.minimumWidth() <= 780, dlg.minimumWidth()
        if dlg.dem_point is not None:                            # Screening is installed
            assert isinstance(dlg.dem_body, QScrollArea), type(dlg.dem_body)
            rows = dlg.dem_body.widget()
            assert dlg.dem_body.sizeHint().height() >= rows.sizeHint().height()
            assert dlg.dem_body.minimumSizeHint().height() <= RD.DEM_ROWS_MIN_H
    finally:
        dlg.close()
