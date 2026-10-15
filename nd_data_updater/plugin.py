"""QGIS side of ND Data Updater: menu, toolbar, background task, layer handling.

1.4.0, making a 90-minute run safe to leave: the run already went through a QgsTask, but nothing
said what it was doing - the bar sat at 3% for forty minutes with no layer name and no way to
stop it from the plugin's own windows. The task now carries a one-line status ("layer 12 of 29:
USACE National Structure Inventory"), a GUI-side timer repeats it in the status bar and in the
open windows, every window has a Cancel button, and the run ends with a plain-words block saying
what was not fetched and what that costs. Without QgsTask (a QGIS build or a test harness
without the task manager) the same run happens in front of a QProgressDialog that can be
cancelled, which is the old blocking behaviour with a way out.
"""

import glob
import os
import time

from qgis.core import (
    QgsApplication, QgsFeedback, QgsMessageLog, QgsProject, QgsProviderRegistry, QgsVectorLayer,
)
from qgis.PyQt.QtCore import QCoreApplication, QTimer, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QIcon
from qgis.PyQt.QtWidgets import QApplication, QProgressDialog

from . import core, runreport
from .qtcompat import (MSG_INFO, MSG_WARNING, TASK_CAN_CANCEL, VECTOR_LAYER, WAIT_CURSOR,
                       WINDOW_MODAL, QAction)  # noqa: F401
from .http_qgis import QgisHttp, gdal_merger
from .packsource import PACK_FOLDER
from .settings import Settings
from .settings_dialog import SettingsDialog
from .sources import SOURCE_INFO, build_sources

try:
    from qgis.core import QgsTask
except ImportError:                              # no task manager: the blocking fallback runs
    QgsTask = None

LOG_TAG = "ND Data Updater"
MENU = "&ND Data Updater"
GROUP_NAME = "North Dakota data"
PLUGIN_DIR = os.path.dirname(__file__)


def log(message, level=MSG_INFO):
    QgsMessageLog.logMessage(message, LOG_TAG, level)


if QgsTask is not None:
    class UpdateTask(QgsTask):
        """The run, in the background. `status` is read from the GUI thread by a timer.

        The status is a plain string written by the worker and read by the GUI; nothing else is
        shared, so no lock is needed (and a status line one tick out of date is harmless)."""

        def __init__(self, make_updater, force, only, on_done):
            super().__init__("Updating North Dakota data", TASK_CAN_CANCEL)
            self.qgs_feedback = QgsFeedback()
            self.updater = make_updater(QgisHttp(self.qgs_feedback))
            self.force = force
            self.only = only
            self.on_done = on_done
            self.results = []
            self.error = None
            self.status = "starting..."

        def _status(self, text):
            self.status = text

        def run(self):
            feedback = core.Feedback(progress=self.setProgress, is_cancelled=self.isCanceled,
                                     log=log, status=self._status)
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
else:                                            # pragma: no cover - QGIS always has QgsTask
    UpdateTask = None


class BlockingRun:
    """The same run in front of a QProgressDialog, when there is no task manager.

    It keeps the shape of a finished task (results / error / status) so _on_task_done does not
    have to know which way the run happened."""

    def __init__(self, updater, force, only, parent=None):
        self.updater = updater
        self.force = force
        self.only = only
        self.results = []
        self.error = None
        self.status = ""
        self.dialog = QProgressDialog("Checking North Dakota data...", "Cancel", 0, 100, parent)
        self.dialog.setWindowTitle(LOG_TAG)
        self.dialog.setWindowModality(WINDOW_MODAL)
        self.dialog.setMinimumDuration(0)
        self.dialog.setAutoClose(False)
        self.dialog.setAutoReset(False)

    def _status(self, text):
        self.status = text
        self.dialog.setLabelText(text[:120])
        QCoreApplication.processEvents()

    def _progress(self, pct):
        self.dialog.setValue(int(max(0, min(100, pct))))
        QCoreApplication.processEvents()

    def isCanceled(self):  # noqa: N802 - the QgsTask name, so both runs look the same
        return self.dialog.wasCanceled()

    def run(self):
        feedback = core.Feedback(progress=self._progress, is_cancelled=self.isCanceled,
                                 log=log, status=self._status)
        self.dialog.show()
        QCoreApplication.processEvents()
        try:
            self.results = self.updater.check_and_update(feedback, self.force, self.only)
        except Exception as e:  # noqa: BLE001 - reported like a task failure
            self.error = e
        finally:
            self.dialog.close()
        return self


class NdDataUpdaterPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.settings = Settings()
        self.task = None
        self.task_quiet = True
        self.dialog = None
        self.pack_dialog = None
        self.actions = []
        self.timer = None
        self.status_timer = None
        self.last_report = {}            # the last pack change report (runreport.change_report)
        self.last_summary = []           # the last run's plain-words end-of-run block
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
        self.action_pack = QAction("Offline Pack and Data Folder…", win)
        self.action_pack.setToolTip("The pack layer by layer, the data folder, carrying the pack "
                                    "on a USB stick, and a check of the services")
        self.action_pack.triggered.connect(self.show_pack)
        self.action_cancel = QAction("Stop the Running Update", win)
        self.action_cancel.triggered.connect(self.cancel_update)
        self.action_cancel.setEnabled(False)
        self.actions = [self.action_check, self.action_load, self.action_folder,
                        self.action_pack, self.action_settings, self.action_cancel]
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

        # repeats the running task's one-line status in the status bar and the open windows
        self.status_timer = QTimer()
        self.status_timer.setInterval(1500)
        self.status_timer.timeout.connect(self._tick_status)
        self.status_timer.start()

    def unload(self):
        for timer in ("timer", "status_timer"):
            t = getattr(self, timer, None)
            if t:
                t.stop()
                setattr(self, timer, None)
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
        for dialog in (self.dialog, self.pack_dialog):
            if dialog:
                dialog.close()

    # --------------------------------------------------------------- engine

    def _config(self, layers=None, missing_only=False):
        config = self.settings.source_config()
        if layers:
            config["pack_only_layers"] = list(layers)
        if missing_only:
            config["pack_missing_only"] = True
        return config

    def enabled_sources(self, layers=None, missing_only=False):
        return build_sources(self._config(layers, missing_only), merger=self.merger)

    def all_sources(self):
        """Every dataset, enabled or not (for the status table)."""
        config = self._config()
        config["enabled"] = {sid: True for sid in SOURCE_INFO}
        config["usa_structures_url"] = (config["usa_structures_url"]
                                        or "https://example.invalid/usa_structures.zip")
        return build_sources(config, merger=self.merger)

    def pack_path(self):
        """Where the pack is (the pack folder in the data folder)."""
        return os.path.join(self.settings.data_dir, PACK_FOLDER)

    def adopt_data_folder(self, path):
        """Set the data folder here *and* in the three dam plugins, in one call.

        Returns a one-line message for the window. This is the fix for the commonest first-run
        problem: the updater's data folder and the plugins' ndsuite/data_folder used to be two
        settings, so a user who set only this one got plugins looking in an empty folder."""
        path = (path or "").strip()
        if not path:
            return "No folder was chosen."
        self.settings.data_dir = path
        try:
            from .ndpack.damsuite import suitedata as SD
            out = SD.adopt_folder(path) or {}
        except Exception as e:  # noqa: BLE001 - the updater still works without the suite
            log(f"Could not tell the dam plugins about {path}: {e}", MSG_WARNING)
            return f"Data folder set to {path} (the dam plugins could not be told: {e})."
        parts = [f"The dam plugins now use {path}"]
        if out.get("pack"):
            parts.append(f"pack {os.path.basename(out['pack'])}")
        if out.get("dams"):
            parts.append(f"Dams folder {out['dams']}")
        if out.get("created"):
            parts.append("created " + ", ".join(out["created"]))
        return "; ".join(parts) + "."

    def pack_summary(self):
        """One line on the offline pack for the status table."""
        pack = self.pack_path()
        try:
            from .ndpack.damsuite import gpkgpack as GP
            man = GP.read_manifest(pack) or {}
        except Exception:  # noqa: BLE001
            man = {}
        sets = man.get("datasets") or {}
        if not sets:
            return "not built yet"
        n = len(sets) + len(man.get("rasters") or {})
        bad = sorted(k for k, v in sets.items() if v.get("status") not in ("complete", "not available"))
        return f"{n} layers" + (f"; not complete: {', '.join(bad)}" if bad else "; all complete")

    def make_updater(self, http=None, layers=None, missing_only=False):
        return core.Updater(self.settings.data_dir,
                            self.enabled_sources(layers, missing_only), http)

    def is_running(self):
        return self.task is not None

    def task_status(self):
        """The running run's one-line status ("" when nothing is running)."""
        return getattr(self.task, "status", "") if self.task is not None else ""

    def cancel_update(self):
        """Stop the running update (the finished layers of a pack build are kept)."""
        if self.task is None:
            return
        try:
            self.task.cancel()
            self.iface.messageBar().pushInfo(LOG_TAG, "Stopping... the layers already built are "
                                                      "kept and the next run continues from "
                                                      "there.")
        except Exception as e:  # noqa: BLE001 - a cancel must never raise out of a button
            log(f"Could not cancel: {e}", MSG_WARNING)

    def _tick_status(self):
        """Repeat the task's status line where a user can see it (GUI thread)."""
        running = self.is_running()
        if getattr(self, "action_cancel", None) is not None:
            self.action_cancel.setEnabled(running)
        text = self.task_status()
        if running and text:
            try:
                self.iface.statusBarIface().showMessage(f"{LOG_TAG}: {text}"[:200], 4000)
            except Exception:  # noqa: BLE001 - no status bar in some builds
                pass
        for dialog in (self.dialog, self.pack_dialog):
            if dialog is None:
                continue
            try:
                dialog.show_progress(text if running else "", running)
            except (RuntimeError, AttributeError):
                pass

    # ---------------------------------------------------------------- update

    def auto_check(self):
        if not self.settings.auto_check or self.is_running():
            return
        # 1.5.0: never start a quiet build before a data folder has been chosen (here or in the
        # suite home). On a fresh machine the start-up timer used to download gigabytes unasked
        # into the default folder and then switch the dam plugins onto it. The first build needs
        # a deliberate click (Check for Data Updates Now, or the pack button in the Hazard home).
        if not self.settings.data_dir_is_set:
            return
        if time.time() - self.settings.last_auto_check < self.settings.check_hours * 3600:
            return
        self.start_update(force=False, quiet=True)

    def start_update(self, force=False, quiet=False, only=None, layers=None,
                     missing_only=False):
        """Run the update in the background.

        `only`: dataset ids to run (None = every enabled one). `layers`: pack layer names to
        fetch again ("Fetch again" on one row). `missing_only`: build only the pack layers that
        are not there yet (the "Rebuild only missing layers" button)."""
        bar = self.iface.messageBar()
        if self.is_running():
            if not quiet:
                bar.pushInfo(LOG_TAG, "An update check is already running (see the task bar "
                                      "at the bottom of the window). Use Plugins > ND Data "
                                      "Updater > Stop the Running Update to stop it.")
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

        def make(http):
            return self.make_updater(http, layers=layers, missing_only=missing_only)

        if UpdateTask is None:                   # pragma: no cover - no task manager
            self._run_blocking(make, force, only, quiet)
            return
        self.task = UpdateTask(make, force, only, self._on_task_done)
        self.settings.pack_full = False          # the one-time "fetch every pack layer again"
        QgsApplication.taskManager().addTask(self.task)
        self._refresh_dialog()
        if not quiet:
            bar.pushInfo(LOG_TAG, "Checking North Dakota datasets for updates in the "
                                  "background. The layer it is on is shown in the status bar; "
                                  "Cancel is in the task bar and in the plugin's windows.")

    def _run_blocking(self, make, force, only, quiet):  # pragma: no cover - needs Qt
        """Fallback when QGIS has no task manager: the old blocking run, with a Cancel button."""
        self.settings.pack_full = False
        run = BlockingRun(make(QgisHttp(None)), force, only, self.iface.mainWindow())
        self.task = run
        try:
            run.run()
        finally:
            self.task = None
        self.task_quiet = quiet
        self._on_task_done(run)

    def _on_task_done(self, task):
        self.task = None
        quiet = self.task_quiet
        bar = self.iface.messageBar()
        self._end_of_run(task)
        if task.error is not None:
            log(f"Update failed: {task.error}", MSG_WARNING)
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
        if self.last_summary:
            bar.pushInfo(LOG_TAG, " ".join(self.last_summary[-2:])[:400])
        if self.last_report.get("changes"):
            bar.pushInfo(LOG_TAG, "What changed in the pack: " + self.last_report["summary"]
                         + " " + self.last_report.get("stale_text", "")
                         + " (Plugins > ND Data Updater > Offline Pack and Data Folder > "
                           "Pack change report to read or save it.)")
        if by_status.get("cancelled"):
            bar.pushInfo(LOG_TAG, "Update check cancelled. Local data was not changed.")
        if not quiet and not (updated or errors or offline or by_status.get("cancelled")):
            bar.pushSuccess(LOG_TAG, "All North Dakota datasets are up to date.")
        self._refresh_dialog()

    def _end_of_run(self, task):
        """The plain-words block every run ends with, in the log and kept for the windows.

        What failed used to be scattered through a 2,000-line log; this says how many layers are
        in the pack and what is not, in the words a dam safety engineer would use, with the
        consequence."""
        pack = self.pack_path()
        try:
            self.last_summary = runreport.run_summary(getattr(task, "results", None), pack)
        except Exception as e:  # noqa: BLE001 - the summary must never hide the run's own result
            self.last_summary = [f"(the summary could not be made: {e})"]
        log("---- what this run did ----")
        for line in self.last_summary:
            log(line)
        self.last_report = {}
        for source in getattr(getattr(task, "updater", None), "sources", None) or []:
            report = getattr(source, "report", None)
            if report and report.get("changes"):
                self.last_report = report
        if not self.last_report:
            return
        log("---- what changed in the pack ----")
        for line in (self.last_report.get("text") or "").splitlines():
            log(line)

    def change_report(self, fresh=False):
        """The pack change report: the one the last run made, or a new one from the manifest
        copy kept in the pack (so the window can show it again after QGIS was restarted)."""
        if self.last_report.get("changes") and not fresh:
            return self.last_report
        pack = self.pack_path()
        try:
            from .ndpack.damsuite import suitedata as SD
            dams = SD.dams_folder()
        except Exception:  # noqa: BLE001
            dams = ""
        try:
            return runreport.change_report(pack, dams=dams)
        except Exception as e:  # noqa: BLE001 - a report must not raise out of a button
            return {"summary": f"The change report could not be made ({e}).", "changes": [],
                    "text": "", "stale": [], "stale_text": ""}

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
                        log(f"Could not open {uri}", MSG_WARNING)
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
                if details.type() == VECTOR_LAYER:
                    found.append((details.uri(), details.name() or base,
                                  details.providerKey()))
            if name_filter:
                wanted = [f for f in found
                          if any(s.lower() in f[1].lower() for s in name_filter)]
                if wanted:
                    return wanted
                log(f"No layer matching {', '.join(name_filter)} in {path}; adding all layers",
                    MSG_WARNING)
            if found:
                return found
        return [(path, base, "ogr")]

    # ------------------------------------------------------------------- misc

    def open_data_folder(self):
        os.makedirs(self.settings.data_dir, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(self.settings.data_dir))

    def show_pack(self):
        """The pack window: the layers, the data folder, USB copies and the service check."""
        from .pack_dialog import PackDialog
        if self.pack_dialog is None:
            self.pack_dialog = PackDialog(self.settings, self, self.iface.mainWindow())
            self.pack_dialog.finished.connect(self._pack_dialog_closed)
        self.pack_dialog.refresh_all()
        self.pack_dialog.show()
        self.pack_dialog.raise_()
        self.pack_dialog.activateWindow()

    def _pack_dialog_closed(self, _result):
        self.pack_dialog = None

    def busy(self, on=True):
        """The wait cursor around a short blocking job in a window (a layer replacement)."""
        try:
            if on:
                QApplication.setOverrideCursor(WAIT_CURSOR)
            else:
                QApplication.restoreOverrideCursor()
        except Exception:  # noqa: BLE001
            pass

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
        if self.pack_dialog is not None:
            try:
                self.pack_dialog.refresh_all()
            except RuntimeError:
                self.pack_dialog = None
