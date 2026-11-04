"""2.35.10 (second GUI check 2026-11-04): Dam suite home's pages scroll. At 640 x 480 the window
shrank to the screen and the Start tab's "The four steps" rows were squeezed to about 20 of their
25 px (44 crushed cells across the three nd plugins). Needs real PyQGIS - skipped otherwise.

    python3 -m ND_Dam_Hazard_Helper.tests.run_tests suite_home_rows
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


def _app():
    if MODE != "real":
        raise Skip("Qt widgets need a real PyQGIS")
    from qgis.core import QgsApplication
    app = QgsApplication.instance()
    if app is None:
        app = QgsApplication([], True)
        _APP.append(app)
    return app


@test
def suite_home_rows_keep_their_height_in_a_small_window():
    app = _app()
    from qgis.PyQt.QtWidgets import QPushButton, QScrollArea
    from ND_Dam_Hazard_Helper.suite_home import SuiteHomeDialog
    dlg = SuiteHomeDialog(None, None, "test")
    try:
        dlg.show()
        dlg.resize(592, 400)                     # what a 640 x 480 screen leaves the window
        for _ in range(6):
            app.processEvents()
        squeezed = []
        for i in range(dlg.tabs.count()):
            dlg.tabs.setCurrentIndex(i)
            for _ in range(3):
                app.processEvents()
            page = dlg.tabs.widget(i)
            assert isinstance(page, QScrollArea), (dlg.tabs.tabText(i), type(page).__name__)
            for b in page.findChildren(QPushButton):
                if b.isVisible() and b.height() < b.minimumSizeHint().height():
                    squeezed.append((dlg.tabs.tabText(i), b.text(), b.height()))
        assert not squeezed, squeezed
    finally:
        dlg.close()
        dlg.deleteLater()
