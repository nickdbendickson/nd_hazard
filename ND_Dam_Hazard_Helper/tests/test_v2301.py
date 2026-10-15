"""2.30.1: the shared Plugins > ND Dam Suite menu holds each entry once.

QGIS parents a plugin submenu to the main window, not to the Plugins menu, so the old lookup
(findChildren on the Plugins menu) never found the suite menu and each of the three plugins added
its own "Dam suite home...", "Suite settings..." and "Suite data and servers..." - nine lines
for three. Seen in QGIS 3.34; this test builds the menus the way QGIS does, with real Qt.
Run: python3 -m ND_Dam_Hazard_Helper.tests.run_tests (from the folder above the plugin)."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
from ND_Dam_Hazard_Helper.tests.registry import test  # noqa: E402


@test
def the_suite_menu_holds_each_shared_entry_once():
    from qgis.core import QgsApplication
    app = QgsApplication.instance() or QgsApplication([], False)          # noqa: F841
    from qgis.PyQt.QtWidgets import QMenu, QWidget
    from ND_Dam_Hazard_Helper import suite_home as SH

    class Iface(object):
        """Plugins > <name> the way QgisApp::getPluginMenu builds it: a QMenu whose parent is
        the main window, shown through its menuAction() in the Plugins menu."""

        def __init__(self):
            self.main = QWidget()
            self.plugins = QMenu("Plugins", self.main)
            self.subs = {}

        def mainWindow(self):
            return self.main

        def pluginMenu(self):
            return self.plugins

        def addPluginToMenu(self, name, action):
            if name not in self.subs:
                self.subs[name] = QMenu(name, self.main)              # NOT a child of Plugins
                self.plugins.addAction(self.subs[name].menuAction())
            self.subs[name].addAction(action)

        def removePluginMenu(self, name, action):
            self.subs[name].removeAction(action)

    iface = Iface()
    added = []
    for plugin_label, own in (("ND Dam Hazard Classification Helper...", "NDSuiteHazardWindow"),
                              ("ND Dam Breach Runner...", "NDSuiteRunnerWindow"),
                              ("ND Dam Breach Screening...", "NDSuiteScreeningWindow")):
        for label, name in ((plugin_label, own),
                            ("Dam suite home...", "NDSuiteHome"),
                            ("Suite settings...", "NDSuiteSettings"),
                            ("Suite data and servers...", "NDSuiteData")):
            a = SH.add_suite_action(iface, label, name, lambda *_: None)
            if a is not None:
                added.append(a)
    texts = [a.text() for a in iface.subs[SH.SUITE_MENU].actions()]
    assert texts.count("Dam suite home...") == 1, texts
    assert texts.count("Suite settings...") == 1 and texts.count("Suite data and servers...") == 1
    assert len(texts) == 6 and len(added) == 6, texts
    SH.remove_suite_actions(iface, added)
    assert iface.subs[SH.SUITE_MENU].actions() == []
