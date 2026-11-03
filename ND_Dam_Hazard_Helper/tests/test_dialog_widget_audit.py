"""Lumo's dialog.py review (11/03/2026), item 22: _set_busy and the settings save/restore read
their widgets with getattr(self, name, None), so a typo'd or renamed widget name silently
stops being disabled during a run (or saved with the project). The window now checks both
lists when it is built (_audit_widget_names, one warning in the QGIS log); this test makes
the same check a failure. Needs the full dialog (real PyQGIS) - skipped otherwise.

    python3 -m ND_Dam_Hazard_Helper.tests.run_tests dialog_widget_audit
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


@test
def every_busy_and_setting_widget_name_is_a_widget_of_the_built_window():
    if MODE != "real":
        raise Skip("Qt widgets need a real PyQGIS")
    from qgis.core import QgsApplication
    app = QgsApplication.instance() or QgsApplication([], False)   # noqa: F841
    from ND_Dam_Hazard_Helper.dialog import NDDamHazardDialog
    dlg = NDDamHazardDialog()
    try:
        assert dlg._audit_widget_names() == [], dlg._audit_widget_names()
        # and the audit does catch a missing name
        dlg._BUSY_WIDGETS = dlg._BUSY_WIDGETS + ("no_such_btn",)
        assert dlg._audit_widget_names() == ["no_such_btn"]
    finally:
        dlg.close()
