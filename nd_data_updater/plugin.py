"""QGIS side of ND Data Updater: menu, toolbar, background task, layer handling."""

import glob
import os
import time

from qgis.core import (
    Qgis, QgsApplication, QgsFeedback, QgsMapLayerType, QgsMessageLog, QgsProject,
    QgsProviderRegistry, QgsTask, QgsVectorLayer,
)
from qgis.PyQt.QtCore import QTimer, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QIcon
from qgis.PyQt.QtWidgets import QAction

from . import core
from .http_qgis import QgisHttp, gdal_merger
from .settings import Settings
from .settings_dialog import SettingsDialog
from .sources import SOURCE_INFO, build_sources

LOG_TAG = "ND Data Updater"
MENU = "&ND Data Updater"
GROUP_NAME = "North Dakota data"
PLUGIN_DIR = os.path.dirname(__file__)


def log(message, level=Qgis.Info):
    QgsMessageLog.logMessage(message, LOG_TAG, level)


class UpdateTask(QgsTask):
    def __init__(self, make_updater, force, only, on_done):
        super().__init__("Updating North Dakota data", QgsTask.CanCancel)
        self.qgs_feedback = QgsFeedback()
        self.updater = make_updater(QgisHttp(self.qgs_feedback))
        self.force = force
        self.only = only
        self.on_done = on_done
        self.results = []
        self.error = None

    def run(self):
        feedback = core.Feedback(progress=self.setProgress, is_cancelled=self.isCanceled,
                                 log=log)
        try:
            self.results = self.updater.check_and_update(feedback, self.force, self.only)
        except Exception as e:  # noqa: BLE001 - reported in finished()
            self.error = e
            return False
        return True

    def cancel(self):
        self.qgs_feedback.cancel()
        super().cancel()

    def finished(self, ok):
        self.on_done(self)


class NdDataUpdaterPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.settings = Settings()
        self.task = None
        self.task_quiet = True
        self.dialog = None
        self.actions = []
        self.timer = None
        self.merger = gdal_merger()

    # ------------------------------------------------------------------ setup

    def initGui(self):  # noqa: N802 - QGIS API
        icon = QIcon(os.path.join(PLUGIN_DIR, "icon.svg"))
        win = self.iface.mainWindow()
        self.action_check = QAction(icon, "Check for Data Updates Now", win)
        self.action_check.setToolTip("ND Data Updater: check North Dakota datasets for updates")
        self.action_check.triggered.connect(lambda: self.start_update(force=False, quiet=False))
        self.action_load = QAction("Add North Dakota Layers to Project", win)
        self.action_load.triggered.connect(self.load_layers)
        self.action_folder = QAction("Open Data Folder", win)
        self.action_folder.triggered.connect(self.open_data_folder)
        self.action_settings = QAction("Settings and Status…", win)
        self.action_settings.triggered.connect(self.show_settings)
        self.actions = [self.action_check, self.action_load, self.action_folder,
                        self.action_settings]
        for action in self.actions:
            self.iface.addPluginToMenu(MENU, action)
        self.iface.addToolBarIcon(self.action_check)

        # Check shortly after start-up, then hourly; auto_check() applies the
        # "at most every N hours" setting so this never checks more than asked.
        self.timer = QTimer()
        self.timer.setInterval(60 * 60 * 1000)
        self.timer.timeout.connect(self.auto_check)
        self.timer.start()
        QTimer.singleShot(20 * 1000, self.auto_check)

    def unload(self):
        if self.timer:
            self.timer.stop()
            self.timer = None
        if self.task is not None:
            try:
                self.task.on_done = lambda task: None
                self.task.cancel()
            except RuntimeError:
                pass
            self.task = None
        for action in self.actions:
            self.iface.removePluginMenu(MENU, action)
        self.iface.removeToolBarIcon(self.action_check)
        if self.dialog:
            self.dialog.close()

    # --------------------------------------------------------------- engine

    def _config(self):
        return self.settings.source_config()

    def enabled_sources(self):
        return build_sources(self._config(), merger=self.merger)

    def all_sources(self):
        """Every dataset, enabled or not (for the status table)."""
        config = self._config()
        config["enabled"] = {sid: True for sid in SOURCE_INFO}
        config["usa_structures_url"] = (config["usa_structures_url"]
                                        or "https://example.invalid/usa_structures.zip")
        return build_sources(config, merger=self.merger)

    def make_updater(self, http=None):
        return core.Updater(self.settings.data_dir, self.enabled_sources(), http)

    def is_running(self):
        return self.task is not None

    # ---------------------------------------------------------------- update

    def auto_check(self):
        if not self.settings.auto_check or self.is_running():
            return
        if time.time() - self.settings.last_auto_check < self.settings.check_hours * 3600:
            return
        self.start_update(force=False, quiet=True)

    def start_update(self, force=False, quiet=False):
        bar = self.iface.messageBar()
        if self.is_running():
            if not quiet:
                bar.pushInfo(LOG_TAG, "An update check is already running (see the task bar "
                                      "at the bottom of the window).")
            return
        if not self.enabled_sources():
            if not quiet:
                bar.pushWarning(LOG_TAG, "No datasets are turned on. Choose some in "
                                         "Plugins > ND Data Updater > Settings and Status.")
            return
        try:
            os.makedirs(self.settings.data_dir, exist_ok=True)
        except OSError as e:
            bar.pushCritical(LOG_TAG, f"Can't use data folder {self.settings.data_dir}: {e}")
            return
        self.settings.last_auto_check = time.time()
        self.task_quiet = quiet
        self.task = UpdateTask(self.make_updater, force, None, self._on_task_done)
        QgsApplication.taskManager().addTask(self.task)
        self._refresh_dialog()
        if not quiet:
            bar.pushInfo(LOG_TAG, "Checking North Dakota datasets for updates in the "
                                  "background. Progress is shown in the task bar.")

    def _on_task_done(self, task):
        self.task = None
        quiet = self.task_quiet
        bar = self.iface.messageBar()
        if task.error is not None:
            log(f"Update failed: {task.error}", Qgis.Warning)
            if not quiet or not isinstance(task.error, core.UpdateError):
                bar.pushWarning(LOG_TAG, f"Update check failed: {task.error}")
            self._refresh_dialog()
            return

        results = task.results
        by_status = {}
        for r in results:
            by_status.setdefault(r.status, []).append(r)
        updated = by_status.get("updated", [])
        errors = by_status.get("error", [])
        offline = by_status.get("offline", [])
        missing = [r for r in offline if not r.message.startswith("using local copy")]

        if updated:
            self._reload_layers([r.folder for r in updated])
            bar.pushSuccess(LOG_TAG, "Downloaded new data: "
                            + ", ".join(r.name for r in updated) + ".")
        for r in errors:
            bar.pushWarning(LOG_TAG, f"{r.name}: {r.message}")
        if missing and (not quiet or not updated):
            bar.pushWarning(LOG_TAG, "No internet connection, and these datasets have not "
                                     "been downloaded yet: "
                            + ", ".join(r.name for r in missing) + ".")
        elif offline and not quiet:
            bar.pushInfo(LOG_TAG, "No internet connection. Using the local copies already "
                                  "on this Mac.")
        if by_status.get("cancelled"):
            bar.pushInfo(LOG_TAG, "Update check cancelled. Local data was not changed.")
        if not quiet and not (updated or errors or offline or by_status.get("cancelled")):
            bar.pushSuccess(LOG_TAG, "All North Dakota datasets are up to date.")
        self._refresh_dialog()

    def _reload_layers(self, folders):
        """Point already-loaded layers at the freshly installed files."""
        roots = [os.path.realpath(os.path.join(self.settings.data_dir, f)) + os.sep
                 for f in folders]
        for layer in QgsProject.instance().mapLayers().values():
            path = layer.source().split("|")[0]
            if not path:
                continue
            real = os.path.realpath(path)
            if any(real.startswith(root) for root in roots):
                try:
                    layer.setDataSource(layer.source(), layer.name(), layer.providerType())
                except Exception:  # noqa: BLE001
                    layer.reload()
                layer.triggerRepaint()
                log(f"Reloaded layer {layer.name()}")

    # ---------------------------------------------------------------- layers

    def load_layers(self):
        bar = self.iface.messageBar()
        project = QgsProject.instance()
        existing_sources = {l.source() for l in project.mapLayers().values()}
        root = project.layerTreeRoot()
        added = 0
        for source in self.enabled_sources():
            folder = os.path.join(self.settings.data_dir, source.folder)
            if not os.path.isdir(folder):
                continue
            paths = []
            for pattern in source.layer_globs:
                paths.extend(sorted(glob.glob(os.path.join(folder, pattern), recursive=True)))
            for path in dict.fromkeys(paths):
                for uri, name, provider in self._sublayers(path, source.layer_filter):
                    if uri in existing_sources:
                        continue
                    layer = QgsVectorLayer(uri, f"{source.name}: {name}", provider)
                    if not layer.isValid():
                        log(f"Could not open {uri}", Qgis.Warning)
                        continue
                    group = root.findGroup(GROUP_NAME) or root.insertGroup(0, GROUP_NAME)
                    project.addMapLayer(layer, False)
                    group.addLayer(layer)
                    existing_sources.add(uri)
                    added += 1
        if added:
            bar.pushSuccess(LOG_TAG, f"Added {added} layer(s) to the '{GROUP_NAME}' group.")
        elif not os.path.isdir(self.settings.data_dir) or not any(
                os.path.isdir(os.path.join(self.settings.data_dir, s.folder))
                for s in self.enabled_sources()):
            bar.pushWarning(LOG_TAG, "Nothing has been downloaded yet. Use "
                                     "Check for Data Updates Now first.")
        else:
            bar.pushInfo(LOG_TAG, "All downloaded layers are already in the project.")

    @staticmethod
    def _sublayers(path, name_filter=()):
        """Vector layers inside a file or folder (a .gdb or .gpkg can hold several).

        name_filter: substrings; when given, only layers whose name contains one
        of them are returned (falls back to all layers if none match).
        """
        base = os.path.splitext(os.path.basename(path.rstrip(os.sep)))[0]
        if path.lower().endswith((".gdb", ".gdb" + os.sep, ".gpkg")):
            found = []
            for details in QgsProviderRegistry.instance().querySublayers(path):
                if details.type() == QgsMapLayerType.VectorLayer:
                    found.append((details.uri(), details.name() or base,
                                  details.providerKey()))
            if name_filter:
                wanted = [f for f in found
                          if any(s.lower() in f[1].lower() for s in name_filter)]
                if wanted:
                    return wanted
                log(f"No layer matching {', '.join(name_filter)} in {path}; adding all layers",
                    Qgis.Warning)
            if found:
                return found
        return [(path, base, "ogr")]

    # ------------------------------------------------------------------- misc

    def open_data_folder(self):
        os.makedirs(self.settings.data_dir, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(self.settings.data_dir))

    def show_settings(self):
        if self.dialog is None:
            self.dialog = SettingsDialog(self.settings, self, self.iface.mainWindow())
            self.dialog.finished.connect(self._dialog_closed)
        self.dialog.refresh_status()
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def _dialog_closed(self, _result):
        self.dialog = None

    def _refresh_dialog(self):
        if self.dialog is not None:
            try:
                self.dialog.refresh_status()
            except RuntimeError:
                self.dialog = None
