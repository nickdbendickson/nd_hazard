# -*- coding: utf-8 -*-
"""
Plugin registration: Toolbar/menu wiring and dialog lifecycle.
This file connects QGIS to the NDDamHazardDialog.
"""

import os
from qgis.PyQt.QtCore import QObject
from qgis.PyQt.QtGui import QIcon

try:                                    # Qt6 (QGIS 4): QAction lives in QtGui
    from qgis.PyQt.QtGui import QAction
except ImportError:                     # Qt5 (QGIS 3.x): QtWidgets
    from qgis.PyQt.QtWidgets import QAction

try:
    from qgis.PyQt import sip
except ImportError:  # pragma: no cover - very old builds
    import sip

from .constants import PLUGIN_NAME


def _task_running(task):
    """True while a QgsTask of the Suite data window has not ended (False for None)."""
    if task is None:
        return False
    try:
        from qgis.core import QgsTask
        return task.status() not in (QgsTask.Complete, QgsTask.Terminated)
    except RuntimeError:                # the task manager has deleted it: it has ended
        return False
    except Exception:
        return True


class NDdamHazardHelperPlugin(QObject):
    """Main plugin class registered by __init__.py."""

    def __init__(self, iface):
        """
        Initialize the plugin.
        :param iface: QGIS interface object.
        """
        super().__init__()
        self.iface = iface
        self.action = None
        self.dialog = None
        self.home_action = None
        self.home_dialog = None
        self.data_action = None         # 2.17.0: Suite data and servers
        self.provider = None
        self._versions_checked = False

    def initProcessing(self):
        """Register the Processing provider (toolbox, models, qgis_process).
        A failure here must not stop the plugin's menu entry. Called by QGIS
        (hasProcessingProvider=yes) and by initGui - registered only once."""
        if self.provider is not None:
            return
        try:
            from qgis.core import QgsApplication
            from .processing_provider import Provider
            self.provider = Provider()
            QgsApplication.processingRegistry().addProvider(self.provider)
        except Exception as e:           # pragma: no cover - depends on QGIS build
            self.provider = None
            try:
                from qgis.core import QgsMessageLog
                QgsMessageLog.logMessage("Processing provider not available: {}".format(e),
                                         PLUGIN_NAME)
            except Exception:
                pass

    def initGui(self):
        """
        Create the action that will be displayed in the menu/toolbar.
        Called when the plugin is enabled.
        """
        self.initProcessing()
        # Try to load an icon; if missing, use a blank icon
        icon_path = os.path.join(os.path.dirname(__file__), "icon.png")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()

        self.action = QAction(icon, PLUGIN_NAME, self.iface.mainWindow())
        self.action.setObjectName("NDDamHazardHelperAction")
        self.action.triggered.connect(self.run_plugin)

        # Add to Plugins Menu
        self.iface.addPluginToMenu(PLUGIN_NAME, self.action)
        # v2.12: the Dam suite home (the same window in all three plugins)
        self.home_action = QAction(icon, "Dam suite home...", self.iface.mainWindow())
        self.home_action.setObjectName("NDDamHazardSuiteHomeAction")
        self.home_action.triggered.connect(self.run_home)
        self.iface.addPluginToMenu(PLUGIN_NAME, self.home_action)
        # 2.17.0: the suite data folder, the servers and "Get data for this dam" (suite_data.py)
        self.data_action = QAction(icon, "Suite data and servers...", self.iface.mainWindow())
        self.data_action.setObjectName("NDDamHazardSuiteDataAction")
        self.data_action.triggered.connect(self.run_suite_data)
        self.iface.addPluginToMenu(PLUGIN_NAME, self.data_action)
        # v2.13: the shared Plugins > ND Dam Suite menu (one menu for the three plugins)
        self.suite_actions = []
        try:
            from . import suite_home
            for label, name, slot in ((PLUGIN_NAME + "...", "NDSuiteHazardWindow", self.run_plugin),
                                      ("Dam suite home...", "NDSuiteHome", self.run_home),
                                      ("Suite data and servers...", "NDSuiteData",
                                       self.run_suite_data),
                                      ("Suite settings...", "NDSuiteSettings", self.run_settings)):
                a = suite_home.add_suite_action(self.iface, label, name, slot)
                if a is not None:
                    self.suite_actions.append(a)
        except Exception:                                        # noqa
            pass

        # Optional: Add to a custom toolbar (uncomment if you want a dedicated toolbar)
        # self.iface.addToolBarIcon(self.action)

        # v2.10: the other dam-suite plugins' versions, once QGIS has loaded every plugin
        try:
            self.iface.initializationCompleted.connect(self.check_versions)
        except Exception:
            pass
        try:
            from qgis.PyQt.QtCore import QTimer
            QTimer.singleShot(0, self.check_versions)
        except Exception:
            pass

    def check_versions(self, versions=None):
        """v2.10: warn (message log and bar) when ND Dam Breach Runner or ND Dam Breach
        Screening is installed in a version this release does not work with (damsuite
        versions table). Runs once; never raises. Returns the messages."""
        if self._versions_checked and versions is None:
            return []
        self._versions_checked = True
        try:
            from .damsuite import versions as VS
            msgs = VS.check("ndhazard", versions=versions)
            lines = VS.report_lines("ndhazard", versions=versions)
        except Exception:
            return []
        try:
            from .helpers import log_message
            from .compat import MSG_INFO, MSG_WARNING, MSG_CRITICAL
            for line in lines:
                log_message("Dam suite: " + line, MSG_INFO)
            for level, text in msgs:
                log_message(text, MSG_CRITICAL if level == "error" else MSG_WARNING)
            bar = self.iface.messageBar() if self.iface is not None else None
            for level, text in msgs:
                if bar is not None:
                    bar.pushMessage(PLUGIN_NAME, text,
                                    level=MSG_CRITICAL if level == "error" else MSG_WARNING,
                                    duration=0 if level == "error" else 15)
        except Exception:
            pass
        return msgs

    def _dialog_alive(self):
        if self.dialog is None:
            return False
        try:
            return not sip.isdeleted(self.dialog)
        except Exception:
            return False

    def run_settings(self, *args):
        """Open the window on its Suite settings tab (v2.13)."""
        self.run_plugin()
        try:
            dlg = self.dialog
            for i in range(dlg.tab_widget.count()):
                if dlg.tab_widget.tabText(i) == "Suite settings":
                    dlg.tab_widget.setCurrentIndex(i)
        except Exception:                                        # noqa
            pass

    def run_home(self, *args):
        """v2.12: the Dam suite home window (suite_home.py, the same in all three plugins)."""
        from . import suite_home
        return suite_home.open_home(self, self.iface, "ndhazard", self.iface.mainWindow())

    def _current_dam_folder(self):
        """The dam folder of the open window ("" without one)."""
        if not self._dialog_alive():
            return ""
        try:
            folder = (self.dialog.config_dam_folder or "").strip()
        except Exception:
            return ""
        return folder if folder and os.path.isdir(folder) else ""

    def run_suite_data(self, *args):
        """2.17.0: the Suite data and servers window (suite_data.py, the same in all three
        plugins), on the window's current dam folder."""
        from . import suite_data
        return suite_data.open_suite_data(self, self.iface, "ndhazard", self.iface.mainWindow(),
                                          dam_folder=self._current_dam_folder())

    def run_plugin(self):
        """
        Open the main dialog when the action is triggered.
        Ensures only one instance of the dialog is open at a time, and
        recreates it if Qt has already destroyed it.
        """
        if not self._dialog_alive():
            # Imported lazily so a missing optional dependency cannot stop
            # QGIS from loading the plugin menu entry.
            from .dialog import NDDamHazardDialog
            self.dialog = NDDamHazardDialog(parent=self.iface.mainWindow())

        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def unload(self):
        """
        Remove the plugin action from the menu/toolbar and free the dialog.
        Called when the plugin is disabled, reloaded, or QGIS closes.
        Safe to call more than once.
        """
        try:
            self.iface.initializationCompleted.disconnect(self.check_versions)
        except Exception:
            pass
        try:                                                    # v2.13: the shared suite menu
            from . import suite_home
            suite_home.remove_suite_actions(self.iface, getattr(self, "suite_actions", []))
        except Exception:
            pass
        self.suite_actions = []
        # v2.6.1: requests of a cancelled download may still be running -
        # wait for them before Python lets their thread pools go
        try:
            from .download import wait_for_parked_pools
            wait_for_parked_pools()
        except Exception:
            pass
        if self.action is not None:
            try:
                self.iface.removePluginMenu(PLUGIN_NAME, self.action)
                # self.iface.removeToolBarIcon(self.action) # Uncomment if toolbar icon added
                self.action.triggered.disconnect(self.run_plugin)
            except (RuntimeError, TypeError):
                pass
            self.action.deleteLater()
            self.action = None
        if self.home_action is not None:
            try:
                self.iface.removePluginMenu(PLUGIN_NAME, self.home_action)
                self.home_action.triggered.disconnect(self.run_home)
            except (RuntimeError, TypeError, AttributeError):
                pass
            self.home_action.deleteLater()
            self.home_action = None
        if self.data_action is not None:                        # 2.17.0
            try:
                self.iface.removePluginMenu(PLUGIN_NAME, self.data_action)
                self.data_action.triggered.disconnect(self.run_suite_data)
            except (RuntimeError, TypeError, AttributeError):
                pass
            self.data_action.deleteLater()
            self.data_action = None
        dlg = getattr(self, "_suite_data_dialog", None)         # suite_data.open_suite_data's
        if dlg is not None:
            try:
                dlg.close()
                # a "Get data" / "Update data" task still running reports to the window
                # when it ends: the window is only deleted when none is
                if not any(_task_running(t) for t in getattr(dlg, "tasks", []) or []):
                    dlg.deleteLater()
            except Exception:
                pass
            self._suite_data_dialog = None
        if self.home_dialog is not None:
            try:
                self.home_dialog.close()
                self.home_dialog.deleteLater()
            except Exception:
                pass
            self.home_dialog = None

        if self.provider is not None:
            try:
                from qgis.core import QgsApplication
                QgsApplication.processingRegistry().removeProvider(self.provider)
            except Exception:
                pass
            self.provider = None

        if self._dialog_alive():
            if getattr(self.dialog, "_busy", False):
                # v2.3.1: defer deletion until run completes
                self.dialog.hide()
                from qgis.PyQt.QtCore import QTimer
                QTimer.singleShot(5000, self.unload)
                return
            try:
                self.dialog.close()
                self.dialog.deleteLater()
            except RuntimeError:
                pass
        self.dialog = None
