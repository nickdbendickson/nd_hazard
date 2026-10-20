from qgis.core import QgsApplication
from .provider import NDDamProvider

LOG_TAG = "ND dam suite"
MENU_NAME = "ND Dam Breach Screening"


def _level(name):
    """Qgis.MessageLevel.<name> (QGIS 3.x / 4.x), the legacy Qgis.<name>, else None."""
    try:
        from qgis.core import Qgis
    except Exception:
        return None
    for get in (lambda: getattr(Qgis.MessageLevel, name), lambda: getattr(Qgis, name)):
        try:
            return get()
        except Exception:
            continue
    return None


def suite_messages(versions=None):
    """[(level, text)] of the suite version check for this plugin (damsuite.versions)."""
    from .damsuite import versions as vs
    return vs.check("ndscreen", versions=versions)


def _plugin_icon():
    """The plugin's icon.svg for its menu entry (every ND dam plugin has one)."""
    import os
    from qgis.PyQt.QtGui import QIcon
    return QIcon(os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.svg"))


class NDDamScreeningPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.provider = None
        self.action = None
        self.home_action = None
        self.suite_actions = []            # 0.13: entries in the shared Plugins > ND Dam Suite menu
        self.dialog = None
        self.home_dialog = None
        self._suite_data_dialog = None      # 0.16.0: suite_data.open_suite_data keeps it here
        self._versions_checked = False

    def initProcessing(self):
        """Register the Processing provider (toolbox, models, qgis_process). Called by QGIS
        for hasProcessingProvider=yes plugins - also by qgis_process, which never calls
        initGui - and by initGui; registers only once. A failure is logged and must not stop
        the menu entry."""
        if self.provider is not None:
            return
        try:
            self.provider = NDDamProvider()
            if QgsApplication.processingRegistry().addProvider(self.provider) is False:
                self._log_failure("Processing provider 'nddam' was not registered (already present?)")
        except Exception as e:                                   # noqa
            self.provider = None
            self._log_failure("Processing provider not available: {}".format(e))

    def initGui(self):
        self.initProcessing()
        self._add_menu_action()
        # suite version check once QGIS has loaded every plugin (at start-up: when the
        # interface is ready; enabled later: right after this call returns to the event loop)
        try:
            self.iface.initializationCompleted.connect(self.check_versions)
        except Exception:
            pass
        try:
            from qgis.PyQt.QtCore import QTimer
            QTimer.singleShot(0, self.check_versions)
        except Exception:
            pass

    # ------------------------------------------------------------ popup window --
    def _add_menu_action(self):
        """Plugins > ND Dam Breach Screening opens the popup window (dialog.py). A failure
        here must not stop the Processing provider."""
        if self.iface is None or self.action is not None:
            return
        try:
            try:                              # Qt6 (QGIS 4): QAction lives in QtGui
                from qgis.PyQt.QtGui import QAction
            except ImportError:               # Qt5 (QGIS 3.x): QtWidgets
                from qgis.PyQt.QtWidgets import QAction
            self.action = QAction(_plugin_icon(), MENU_NAME, self.iface.mainWindow())
            self.action.setObjectName("NDDamScreeningAction")
            self.action.triggered.connect(self.run_plugin)
            self.iface.addPluginToMenu(MENU_NAME, self.action)
            self.home_action = QAction("Dam suite home...", self.iface.mainWindow())
            self.home_action.setObjectName("NDDamScreeningHomeAction")
            self.home_action.triggered.connect(self.run_home)
            self.iface.addPluginToMenu(MENU_NAME, self.home_action)
        except Exception as e:                                   # noqa
            self.action = None
            self._log_failure("Menu entry not available: {}".format(e))
        self._add_suite_menu()

    def _add_suite_menu(self):
        """0.13: the shared Plugins > ND Dam Suite menu (one menu, the three plugins add their
        window to it; 'Dam suite home', 'Suite settings' and 'Suite data and servers' once)."""
        try:
            from . import suite_home
            for label, name, slot in (("ND Dam Breach Screening...", "NDSuiteScreeningWindow", self.run_plugin),
                                      ("Dam suite home...", "NDSuiteHome", self.run_home),
                                      ("Suite settings...", "NDSuiteSettings", self.run_settings),
                                      # 0.16.0: the suite data folder, servers and "Get data for this dam"
                                      ("Suite data and servers...", "NDSuiteData", self.run_suite_data)):
                a = suite_home.add_suite_action(self.iface, label, name, slot)
                if a is not None:
                    self.suite_actions.append(a)
        except Exception as e:                                   # noqa
            self._log_failure("Suite menu not available: {}".format(e))

    def run_suite_data(self, *args):
        """0.16.0: the Suite data and servers window (suite_data.py, the same in all three
        plugins): the suite data folder, the offline pack, the terrain cache, the servers and
        "Get data for this dam"."""
        from . import suite_data
        parent = self.iface.mainWindow() if self.iface is not None else None
        return suite_data.open_suite_data(self, self.iface, "ndscreen", parent)

    def run_settings(self, *args):
        """Open the window on its Settings tab (the suite settings grid)."""
        self.run_plugin()
        try:
            self.dialog.show_settings()
        except Exception:                                        # noqa
            pass

    def _log_failure(self, text):
        try:
            from qgis.core import QgsMessageLog
            QgsMessageLog.logMessage(text, LOG_TAG, _level("Warning"))
        except Exception:
            pass

    def _dialog_alive(self):
        if self.dialog is None:
            return False
        try:
            from qgis.PyQt import sip
        except ImportError:                   # pragma: no cover - very old builds
            import sip
        try:
            return not sip.isdeleted(self.dialog)
        except Exception:
            return False

    def run_home(self, *args):
        """The Dam suite home window (suite_home.py, the same in all three plugins)."""
        from . import suite_home
        parent = self.iface.mainWindow() if self.iface is not None else None
        return suite_home.open_home(self, self.iface, "ndscreen", parent)

    def run_plugin(self, *args):
        """Open the window; one instance, re-created if Qt has destroyed it."""
        if not self._dialog_alive():
            # imported here so a problem in the window cannot stop the plugin from loading
            from .dialog import NDDamScreeningDialog
            parent = self.iface.mainWindow() if self.iface is not None else None
            self.dialog = NDDamScreeningDialog(parent=parent, iface=self.iface)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
        return self.dialog                  # 1.18: the suite hand-offs point the window at a dam

    def check_versions(self, versions=None):
        """Warn in the message log and bar when another suite plugin is too old. Runs once;
        never raises (the check must not stop the plugin)."""
        if self._versions_checked and versions is None:
            return []
        self._versions_checked = True
        try:
            from .damsuite import versions as vs
            msgs = suite_messages(versions)
            lines = vs.report_lines("ndscreen", versions=versions)
        except Exception:
            return []
        try:
            from qgis.core import QgsMessageLog
            for line in lines:
                QgsMessageLog.logMessage(line, LOG_TAG, _level("Info"))
            for level, text in msgs:
                QgsMessageLog.logMessage(text, LOG_TAG, _level("Critical" if level == "error" else "Warning"))
        except Exception:
            pass
        try:
            bar = self.iface.messageBar() if self.iface is not None else None
            for level, text in msgs:
                if bar is not None:
                    bar.pushMessage("ND Dam Breach Screening", text,
                                    level=_level("Critical" if level == "error" else "Warning"),
                                    duration=0 if level == "error" else 15)
        except Exception:
            pass
        return msgs

    def unload(self):
        try:
            self.iface.initializationCompleted.disconnect(self.check_versions)
        except Exception:
            pass
        if self.action is not None:
            try:
                self.iface.removePluginMenu(MENU_NAME, self.action)
                self.action.triggered.disconnect(self.run_plugin)
            except (RuntimeError, TypeError, AttributeError):
                pass
            try:
                self.action.deleteLater()
            except Exception:
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
                self.iface.removePluginMenu(MENU_NAME, self.home_action)
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
        dlg = getattr(self, "_suite_data_dialog", None)        # 0.16.0
        if dlg is not None:
            try:
                dlg.close()
                dlg.deleteLater()
            except Exception:
                pass
            self._suite_data_dialog = None
        try:
            from . import suite_home
            suite_home.remove_suite_actions(self.iface, self.suite_actions)
        except Exception:
            pass
        self.suite_actions = []
        if self.provider is not None:
            try:
                QgsApplication.processingRegistry().removeProvider(self.provider)
            except Exception:                                    # noqa
                pass
            self.provider = None
