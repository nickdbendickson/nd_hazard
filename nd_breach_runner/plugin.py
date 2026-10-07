"""QGIS plugin entry: registers the Processing provider and adds a menu.

v3.9.0: "Suite data and servers..." in the plugin's menu and in the shared Plugins > ND Dam
Suite menu (object name NDSuiteData, so it is there once for the three plugins): the suite data
folder, the servers and "Get data for this dam", on the Runner window's dam when one is chosen.

v3.3.0: Plugins > ND Dam Breach Runner > "Open the Runner window..." opens a popup window
(dialog.py): the Dams folder and dam, what is in the dam's folder, and every tool below on tabs.
The menu entries stay.

v3.2.0: once QGIS has loaded its plugins (a zero-delay timer after initGui), the versions of
the other ND dam suite plugins are checked (damsuite.versions.check("ndbreach")); problems go
to the message bar and the "ND Dam Breach Runner" log. The plugin loads and works without ND
Dam Hazard or ND Dam Breach Screening installed."""
import os

from qgis.core import QgsApplication
from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtGui import QDesktopServices

from .provider import Provider

MENU = "ND &Dam Breach Runner"
LOG_TAG = "ND Dam Breach Runner"


def version_messages(versions=None):
    """[(level, text)] of damsuite.versions.check("ndbreach") ("error" below a hard minimum,
    "warning" below a soft one), plus the one-line summary per suite plugin at level "info".
    `versions` ({key: version}) replaces the QGIS lookup (tests). Never raises."""
    try:
        from .damsuite import versions as dv
        out = list(dv.check("ndbreach", versions))
        out += [("info", ln) for ln in dv.report_lines("ndbreach", versions)]
        return out
    except Exception as e:           # a version check must never stop the plugin loading
        return [("info", "Suite version check skipped: {}".format(e))]


class NDBreachRunnerPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.provider = None
        self.actions = []
        self.suite_actions = []           # 3.5: entries in the shared Plugins > ND Dam Suite menu
        self.action = None
        self.dialog = None
        self.home_action = None
        self.home_dialog = None
        self.data_action = None           # 3.9.0: Suite data and servers
        self._suite_data_dialog = None

    def initProcessing(self):
        """Register the Processing provider once. QGIS / qgis_process call this for a
        hasProcessingProvider=yes plugin and initGui calls it too: 3.4.1 keeps the first
        provider (a second one was refused by the registry but replaced self.provider, so
        unload removed the wrong object and left the first registered - a crash risk on
        plugin reload)."""
        if self.provider is not None:
            return
        self.provider = Provider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def initGui(self):
        self.initProcessing()
        self._schedule_version_check()
        try:
            from qgis.PyQt.QtWidgets import QAction
        except ImportError:  # Qt6 moved QAction
            from qgis.PyQt.QtGui import QAction
        self.action = QAction("Open the Runner window...", self.iface.mainWindow())
        self.action.setObjectName("NDBreachRunnerAction")
        self.action.triggered.connect(self.run_plugin)
        self.iface.addPluginToMenu(MENU, self.action)
        self.actions.append(self.action)
        self.home_action = QAction("Dam suite home...", self.iface.mainWindow())
        self.home_action.setObjectName("NDBreachRunnerHomeAction")
        self.home_action.triggered.connect(self.run_home)
        self.iface.addPluginToMenu(MENU, self.home_action)
        self.actions.append(self.home_action)
        # 3.9.0: the suite data folder, the servers and "Get data for this dam" (suite_data.py)
        self.data_action = QAction("Suite data and servers...", self.iface.mainWindow())
        self.data_action.setObjectName("NDBreachRunnerSuiteDataAction")
        self.data_action.triggered.connect(self.run_suite_data)
        self.iface.addPluginToMenu(MENU, self.data_action)
        self.actions.append(self.data_action)
        sep0 = QAction(self.iface.mainWindow())
        sep0.setSeparator(True)
        self.iface.addPluginToMenu(MENU, sep0)
        self.actions.append(sep0)
        items = [("Guided setup for one dam (wizard)...", "wizard:"),
                 ("Run breach model for one dam...", "ndbreach:rundam"),
                 ("Run breach models for a layer of dams...", "ndbreach:batch"),
                 ("Nested fine grid near the dam...", "ndbreach:nested"),
                 ("Sensitivity of the breach model...", "ndbreach:sensitivity"),
                 ("Probabilistic (Monte Carlo) run...", "ndbreach:probabilistic"),
                 ("Cascade failure of dams in a chain...", "ndbreach:cascade"),
                 ("Estimate the run time...", "ndbreach:estimate"),
                 ("Breach hydrograph only...", "ndbreach:hydrograph"),
                 ("Rainfall to PMF inflow hydrograph...", "ndbreach:rainfallrunoff"),
                 None,
                 ("Get data for this dam (or all dams)...", "ndbreach:getdamdata"),
                 ("Build a statewide LiDAR mosaic (VRT)...", "ndbreach:buildvrt"),
                 ("Fill no-data gaps in a DEM...", "ndbreach:fillgaps"),
                 ("Manning's n from NLCD...", "ndbreach:nlcdmanning"),
                 ("Preview the DEM conditioning...", "ndbreach:conditiondem"),
                 None,
                 ("Life safety and damage estimate...", "ndbreach:consequences"),
                 ("ND hazard classification (Section 5.2)...", "ndbreach:ndhazardclass"),
                 ("Animation of the flood...", "ndbreach:animation"),
                 ("Submittal report (Word)...", "ndbreach:submittal"),
                 ("Compare with a reference model (HEC-RAS)...", "ndbreach:benchmark"),
                 ("Export to HEC-RAS...", "ndbreach:hecrasexport"),
                 ("Build a HEC-RAS 2D model...", "ndbreach:hecrasmodel"),
                 ("Import HEC-RAS results...", "ndbreach:hecrasimport"),
                 ("Hand the results to the hazard classification...", "ndbreach:classify"),
                 ("Flood polygons and EAP map from rasters...", "ndbreach:products"),
                 None,
                 ("Verify a run manifest...", "ndbreach:verifymanifest"),
                 ("Verification against analytical solutions...", "ndbreach:verification"),
                 ("Validation record (Word)...", "ndbreach:validationrecord"),
                 ("Check the model installation...", "ndbreach:check"),
                 ("Set up ANUGA (one-time download)...", "ndbreach:setupanuga"),
                 ("Self-test in this QGIS...", "ndbreach:selftest")]
        for item in items:
            if item is None:
                sep = QAction(self.iface.mainWindow())
                sep.setSeparator(True)
                self.iface.addPluginToMenu(MENU, sep)
                self.actions.append(sep)
                continue
            label, alg = item
            a = QAction(label, self.iface.mainWindow())
            a.triggered.connect(lambda _=False, alg=alg: self._open(alg))
            self.iface.addPluginToMenu(MENU, a)
            self.actions.append(a)
        s = QAction("Model paths (Processing options)...", self.iface.mainWindow())
        s.triggered.connect(self._settings)
        self.iface.addPluginToMenu(MENU, s)
        self.actions.append(s)
        h = QAction("Help (README)", self.iface.mainWindow())
        h.triggered.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(
            os.path.join(os.path.dirname(__file__), "README.md"))))
        self.iface.addPluginToMenu(MENU, h)
        self.actions.append(h)
        # 3.5: the shared Plugins > ND Dam Suite menu (one menu for the three plugins)
        try:
            from . import suite_home
            for label, name, slot in (("ND Dam Breach Runner...", "NDSuiteRunnerWindow", self.run_plugin),
                                      ("Dam suite home...", "NDSuiteHome", self.run_home),
                                      ("Suite settings...", "NDSuiteSettings", self.run_settings),
                                      ("Suite data and servers...", "NDSuiteData",
                                       self.run_suite_data)):
                a = suite_home.add_suite_action(self.iface, label, name, slot)
                if a is not None:
                    self.suite_actions.append(a)
        except Exception:                                        # noqa
            pass

    def run_settings(self, *args):
        """Open the window on its Settings tab (the suite settings grid)."""
        self.run_plugin()
        try:
            self.dialog.tabs.setCurrentIndex(self.dialog.tabs.count() - 1)
        except Exception:                                        # noqa
            pass

    def _schedule_version_check(self):
        """After QGIS has loaded every plugin (the event loop runs the timer), not now: ND
        Dam Hazard may load after this plugin."""
        try:
            from qgis.PyQt.QtCore import QTimer
            QTimer.singleShot(0, self.check_versions)
        except Exception:
            self.check_versions()

    def check_versions(self, versions=None):
        """Show the suite version problems in the message bar and the log. Returns them."""
        msgs = version_messages(versions)
        try:
            from qgis.core import QgsMessageLog
            from .compat import MSG_INFO, MSG_WARNING, MSG_CRITICAL
        except Exception:
            return msgs
        level = {"error": MSG_CRITICAL, "warning": MSG_WARNING, "info": MSG_INFO}
        for kind, text in msgs:
            try:
                QgsMessageLog.logMessage(text, LOG_TAG, level.get(kind, MSG_INFO))
            except Exception:
                pass
            if kind in ("error", "warning"):
                try:
                    self.iface.messageBar().pushMessage(LOG_TAG, text, level[kind],
                                                        0 if kind == "error" else 15)
                except Exception:
                    pass
        return msgs

    def _dialog_alive(self):
        if self.dialog is None:
            return False
        try:
            try:
                from qgis.PyQt import sip
            except ImportError:           # pragma: no cover - very old builds
                import sip
            return not sip.isdeleted(self.dialog)
        except Exception:
            return False

    def run_home(self, *args):
        """The Dam suite home window (suite_home.py, the same in all three plugins)."""
        from . import suite_home
        return suite_home.open_home(self, self.iface, "ndbreach", self.iface.mainWindow())

    def popup_dam_folder(self):
        """The dam folder of the Runner window's dam when the window is open and the folder
        exists, else ""."""
        if not self._dialog_alive():
            return ""
        try:
            f = self.dialog.folder()
            return f if f and os.path.isdir(f) else ""
        except Exception:                                        # noqa
            return ""

    def run_suite_data(self, *args):
        """3.9.0: the Suite data and servers window (suite_data.py, the same in all three
        plugins), on the window's dam when one is chosen."""
        from . import suite_data
        return suite_data.open_suite_data(self, self.iface, "ndbreach", self.iface.mainWindow(),
                                          dam_folder=self.popup_dam_folder())

    def run_plugin(self, *args):
        """Open the window; one instance, re-created if Qt has destroyed it."""
        if not self._dialog_alive():
            # imported here so a problem in the window cannot stop the plugin from loading
            from .dialog import NDBreachRunnerDialog
            self.dialog = NDBreachRunnerDialog(parent=self.iface.mainWindow(), iface=self.iface)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def _open(self, alg):
        if alg == "wizard:":
            from . import wizard
            wizard.run_wizard(self.iface)
            return
        import processing
        processing.execAlgorithmDialog(alg)

    def _settings(self):
        try:
            self.iface.showOptionsDialog(self.iface.mainWindow(), currentPage="processingOptions")
        except TypeError:
            self.iface.showOptionsDialog(self.iface.mainWindow())

    def unload(self):
        for a in self.actions:
            self.iface.removePluginMenu(MENU, a)
        self.actions = []
        try:
            from . import suite_home
            suite_home.remove_suite_actions(self.iface, self.suite_actions)
        except Exception:                                        # noqa
            pass
        self.suite_actions = []
        if self.action is not None:
            try:
                self.action.triggered.disconnect(self.run_plugin)
            except (RuntimeError, TypeError, AttributeError):
                pass
            self.action = None
        if self._dialog_alive():
            try:
                self.dialog.close()
                self.dialog.deleteLater()
            except RuntimeError:
                pass
        self.dialog = None
        if self.home_action is not None:
            try:
                self.home_action.triggered.disconnect(self.run_home)
            except (RuntimeError, TypeError, AttributeError):
                pass
            self.home_action = None
        if self.home_dialog is not None:
            try:
                self.home_dialog.close()
                self.home_dialog.deleteLater()
            except Exception:
                pass
            self.home_dialog = None
        if self.data_action is not None:
            try:
                self.data_action.triggered.disconnect(self.run_suite_data)
            except (RuntimeError, TypeError, AttributeError):
                pass
            self.data_action = None
        dlg = getattr(self, "_suite_data_dialog", None)        # set by suite_data.open_suite_data
        if dlg is not None:
            try:
                dlg.close()
                dlg.deleteLater()
            except Exception:
                pass
            self._suite_data_dialog = None
        if self.provider is not None:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None
