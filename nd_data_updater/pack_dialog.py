"""The offline pack, layer by layer: the window a user who is stuck opens (1.4.0).

Everything about the pack that was previously only in the log or only in a file name is here as
a row with a button:

  Set-up tab      the data folder (one choice, adopted by the three dam plugins through
                  suitedata.adopt_folder), the suite readiness lights from damsuite.suitecheck,
                  and one row per drop-in sub-folder (dwr/, hifld/, dem/, nlcd/, terrain/) with
                  what it is for, where its files come from, how many it holds and "Open folder"
                  - so a novice never has to guess a file name.
  Pack layers tab one row per layer with its vintage and feature count, "Fetch again" (that one
                  layer, now) and "Replace with my file..." (a user with a better county roads
                  file drops it in), plus "Rebuild only missing layers", the USB copy and import,
                  the pack change report and the data-vintage list.
  Services tab    a small, time-limited probe of NSI, 3DHP (small and statewide), NID, FEMA, DWR
                  and MRLC, on demand or when the window opens: the point is to warn *before* a
                  user starts a run that would hang.

Decisions:
  * No callback here may raise: every button body is wrapped and reports through the QGIS
    message bar, because an exception out of a Qt slot in QGIS takes the window with it.
  * The window never does network work on the GUI thread. The service check runs on a worker
    thread and a timer reads the rows; the pack copy and import are file work and run in front
    of a modal QProgressDialog with a Cancel button (minutes, not the 90 of a build).
  * The long-run status line and Cancel are at the bottom of this window as well as the settings
    window: the plugin's timer calls show_progress() on both.
  * The logic lives in packmove / servicecheck / runreport / packsource, all of them pure Python
    and tested without Qt; this file is layout and wiring only.
"""

import os
import threading

from qgis.PyQt.QtCore import QTimer, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QIcon
from qgis.PyQt.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QProgressDialog, QPushButton,
    QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from . import packmove, packsource, runreport, servicecheck
from .qtcompat import (BB_CLOSE, HEADER_STRETCH, HEADER_TO_CONTENTS, ITEM_IS_EDITABLE, MB_CANCEL,
                       MB_YES, NO_EDIT_TRIGGERS, SELECT_ROWS, TEXT_NO_WRAP, WINDOW_MODAL,
                       exec_dialog)

LEVEL_MARK = {"ok": "OK", "info": "-", "warn": "!", "error": "X"}
STATE_MARK = {"up": "OK", "slow": "slow", "down": "DOWN"}


def _table(headers, stretch=-1):
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(list(headers))
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(NO_EDIT_TRIGGERS)
    table.setSelectionBehavior(SELECT_ROWS)
    header = table.horizontalHeader()
    for col in range(len(headers)):
        header.setSectionResizeMode(col, HEADER_STRETCH if col == stretch
                                    else HEADER_TO_CONTENTS)
    return table


def _item(text, tooltip=""):
    cell = QTableWidgetItem("" if text is None else str(text))
    cell.setFlags(cell.flags() & ~ITEM_IS_EDITABLE)
    if tooltip:
        cell.setToolTip(tooltip)
    return cell


def _buttons(*pairs):
    """A cell holding small buttons: (label, tooltip, slot)."""
    box = QWidget()
    row = QHBoxLayout(box)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(4)
    for label, tooltip, slot in pairs:
        button = QPushButton(label)
        button.setToolTip(tooltip)
        button.clicked.connect(slot)
        row.addWidget(button)
    row.addStretch(1)
    return box


def _count(value):
    return "" if value is None else f"{int(value):,}"


class TextWindow(QDialog):
    """A read-only report with Save and Copy (the change report, the vintage list, a check)."""

    def __init__(self, title, text, parent=None, save_to=""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(760, 520)
        self.text = text or ""
        self.save_to = save_to
        layout = QVBoxLayout(self)
        view = QPlainTextEdit(self.text)
        view.setReadOnly(True)
        view.setLineWrapMode(TEXT_NO_WRAP)
        layout.addWidget(view)
        row = QHBoxLayout()
        save = QPushButton("Save…")
        save.clicked.connect(self._save)
        copy = QPushButton("Copy to clipboard")
        copy.clicked.connect(self._copy)
        row.addWidget(save)
        row.addWidget(copy)
        row.addStretch(1)
        layout.addLayout(row)
        buttons = QDialogButtonBox(BB_CLOSE)
        for b in buttons.buttons():                  # no icons on action buttons (docs/gui-grammar.md)
            b.setIcon(QIcon())
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    def _save(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save the report", self.save_to,
                                              "Text files (*.txt)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.text)
        except OSError as e:
            QMessageBox.warning(self, "Not saved", f"Could not write {path}: {e}")

    def _copy(self):
        try:
            from qgis.PyQt.QtWidgets import QApplication
            QApplication.clipboard().setText(self.text)
        except Exception:  # noqa: BLE001 - no clipboard in some sessions
            pass


class PackDialog(QDialog):
    def __init__(self, settings, plugin, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.plugin = plugin
        self.service_rows = []
        self._service_thread = None
        self.setWindowTitle("ND Data Updater — Offline pack and data folder")
        self.setMinimumSize(900, 700)
        layout = QVBoxLayout(self)

        # ---- the one folder everything hangs off
        folder_row = QHBoxLayout()
        folder_row.addWidget(QLabel("Data folder:"))
        self.folder = QLineEdit(settings.data_dir)
        self.folder.setReadOnly(True)
        choose = QPushButton("Choose…")
        choose.setToolTip("One choice: the pack, the drop-in folders, the terrain cache and the "
                          "three dam plugins all follow this folder.")
        choose.clicked.connect(self.choose_folder)
        open_folder = QPushButton("Open")
        open_folder.clicked.connect(lambda: self._open(self.settings.data_dir))
        folder_row.addWidget(self.folder, 1)
        folder_row.addWidget(choose)
        folder_row.addWidget(open_folder)
        layout.addLayout(folder_row)
        self.folder_note = QLabel("")
        self.folder_note.setWordWrap(True)
        layout.addWidget(self.folder_note)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._setup_tab(), "Set-up")
        self.tabs.addTab(self._layers_tab(), "Pack layers")
        self.tabs.addTab(self._services_tab(), "Services")
        layout.addWidget(self.tabs, 1)

        # ---- the running run
        run_row = QHBoxLayout()
        self.progress = QLabel("")
        self.progress.setWordWrap(True)
        self.cancel = QPushButton("Stop the update")
        self.cancel.clicked.connect(self.plugin.cancel_update)
        self.cancel.setEnabled(False)
        run_row.addWidget(self.progress, 1)
        run_row.addWidget(self.cancel)
        layout.addLayout(run_row)

        buttons = QDialogButtonBox(BB_CLOSE)
        for b in buttons.buttons():                  # no icons on action buttons (docs/gui-grammar.md)
            b.setIcon(QIcon())
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.poll = QTimer(self)
        self.poll.setInterval(900)
        self.poll.timeout.connect(self._poll_services)
        self.refresh_all()
        if settings.service_check_on_open:
            QTimer.singleShot(200, self.check_services)

    # ------------------------------------------------------------------ tabs
    def _setup_tab(self):
        page = QWidget()
        box = QVBoxLayout(page)
        box.addWidget(QLabel("<b>Is the suite ready to run?</b>"))
        self.next_step = QLabel("")
        self.next_step.setWordWrap(True)
        box.addWidget(self.next_step)
        self.ready = _table(("", "What", "Now", "What to do"), stretch=3)
        box.addWidget(self.ready, 1)
        row = QHBoxLayout()
        report = QPushButton("Copy this as text…")
        report.setToolTip("The whole set-up as text, to paste into a message.")
        report.clicked.connect(self.show_setup_text)
        row.addWidget(report)
        row.addStretch(1)
        box.addLayout(row)
        box.addWidget(QLabel("<b>Folders you can drop files into</b>"))
        self.folders = _table(("Folder", "What goes in it", "Files", "Where it comes from", ""),
                              stretch=1)
        box.addWidget(self.folders, 1)
        return page

    def _layers_tab(self):
        page = QWidget()
        box = QVBoxLayout(page)
        self.pack_note = QLabel("")
        self.pack_note.setWordWrap(True)
        box.addWidget(self.pack_note)
        self.layers = _table(("Layer", "What it is", "Downloaded", "Features", "State", ""),
                             stretch=1)
        box.addWidget(self.layers, 1)
        row = QHBoxLayout()
        for label, tip, slot in (
                ("Rebuild only missing layers",
                 "Builds the layers that are not in the pack and leaves everything else alone. "
                 "This is what a refresh already does for missing layers; the button makes it "
                 "something you can ask for.", self.rebuild_missing),
                ("Copy pack to USB…",
                 "Copies the pack folder to a drive with a checksum file, for a field laptop "
                 "with no internet.", self.copy_to_usb),
                ("Import pack…",
                 "Copies a pack from a drive into this data folder, checking every file against "
                 "its checksum first, and points the dam plugins at it.", self.import_pack),
                ("Pack change report…",
                 "What the last refresh changed, and which already-classified dams that makes "
                 "out of date.", self.show_change_report),
                ("Data vintage…",
                 "Layer, source, download date and feature count, to stamp a report with.",
                 self.show_vintage)):
            button = QPushButton(label)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            row.addWidget(button)
        row.addStretch(1)
        box.addLayout(row)
        return page

    def _services_tab(self):
        page = QWidget()
        box = QVBoxLayout(page)
        box.addWidget(QLabel("<b>Do the services answer?</b> Asked with one small question each, "
                             "so you know before a run that would hang."))
        self.services = _table(("", "Service", "Time", "What it is for / what it said"),
                               stretch=3)
        box.addWidget(self.services, 1)
        self.service_note = QLabel("Not checked yet.")
        self.service_note.setWordWrap(True)
        box.addWidget(self.service_note)
        row = QHBoxLayout()
        check = QPushButton("Check now")
        check.clicked.connect(self.check_services)
        self.on_open = QCheckBox("Check when this window opens")
        self.on_open.setChecked(self.settings.service_check_on_open)
        self.on_open.toggled.connect(self._save_on_open)
        row.addWidget(check)
        row.addWidget(self.on_open)
        row.addStretch(1)
        box.addLayout(row)
        return page

    # ------------------------------------------------------------------ helpers
    def _bar(self):
        return self.plugin.iface.messageBar()

    def _warn(self, text):
        try:
            self._bar().pushWarning("ND Data Updater", text)
        except Exception:  # noqa: BLE001 - no message bar in a test harness
            pass

    def _info(self, text):
        try:
            self._bar().pushInfo("ND Data Updater", text)
        except Exception:  # noqa: BLE001
            pass

    def _open(self, path):
        try:
            os.makedirs(path, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        except OSError as e:
            self._warn(f"Could not open {path}: {e}")

    def show_progress(self, text, running):
        """Called by the plugin's timer while a run is going (and once when it ends)."""
        self.progress.setText(text or "")
        self.cancel.setEnabled(bool(running))

    # ------------------------------------------------------------------ refresh
    def refresh_all(self):
        for name, fn in (("readiness", self._fill_ready), ("folders", self._fill_folders),
                         ("layers", self._fill_layers)):
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - a window that cannot draw one table is
                self._warn(f"Could not show the {name}: {e}")   # still worth having
        self.folder.setText(self.settings.data_dir)
        self.show_progress(self.plugin.task_status(), self.plugin.is_running())

    def _fill_ready(self):
        from .ndpack.damsuite import suitecheck as SC
        items = SC.check(self.settings.data_dir)
        steps = SC.step_rows(items)
        step = SC.next_step(steps)
        if step and not step["ready"]:
            self.next_step.setText(f"<b>Next:</b> {step['label']} ({step['plugin_label']}) - "
                                   f"waiting on {step['blocked_by']}")
        elif step:
            self.next_step.setText(f"<b>Next:</b> {step['label']} in {step['plugin_label']}. "
                                   "Nothing is missing here.")
        else:
            self.next_step.setText("")
        self.ready.setRowCount(len(items))
        for r, it in enumerate(items):
            self.ready.setItem(r, 0, _item(LEVEL_MARK.get(it["level"], "?"), it["level"]))
            self.ready.setItem(r, 1, _item(it["label"], it.get("source") or ""))
            self.ready.setItem(r, 2, _item(it["text"], it["text"]))
            self.ready.setItem(r, 3, _item(it.get("fix") or "", it.get("fix") or ""))

    def _fill_folders(self):
        from .ndpack.damsuite import suitedata as SD
        rows = SD.subfolder_info(self.settings.data_dir)
        self.folders.setRowCount(len(rows))
        for r, (_name, path, label, why, where, n) in enumerate(rows):
            self.folders.setItem(r, 0, _item(label, path))
            self.folders.setItem(r, 1, _item(why, why))
            self.folders.setItem(r, 2, _item(f"{n} file(s)"))
            self.folders.setItem(r, 3, _item(where, where))
            self.folders.setCellWidget(r, 4, _buttons(
                ("Open folder", path, lambda _c=False, p=path: self._open(p))))

    def _fill_layers(self):
        pack = self.plugin.pack_path()
        rows = packsource.layer_rows(pack)
        self.pack_note.setText(
            f"{pack} - {self.plugin.pack_summary()}. \"Fetch again\" downloads that one layer "
            "now; \"Replace with my file…\" puts your own file in the pack instead (a county "
            "roads file you trust more, for example) and records it as yours. A layer holding "
            "your own file is left alone by every refresh until you press \"Fetch again\" on "
            "it.")
        self.layers.setRowCount(len(rows))
        for r, row in enumerate(rows):
            state = row["status"] if row["kind"] != "missing" else "not in the pack"
            if row.get("replaced"):
                state = "your own file" + (f" ({row['replaced_at'][:10]})"
                                           if row["replaced_at"] else "")
            self.layers.setItem(r, 0, _item(row["name"], row["source"]))
            self.layers.setItem(r, 1, _item(row["plain"], row["label"]))
            self.layers.setItem(r, 2, _item(row["fetched"][:10] or "—", row["source"]))
            self.layers.setItem(r, 3, _item(_count(row["count"])))
            note = row["note"] or state
            if row.get("replaced"):
                note = ("Your own file" + (f", put in by {row['replaced_by']}"
                                           if row["replaced_by"] else "")
                        + (f" on {row['replaced_at'][:10]}" if row["replaced_at"] else "")
                        + ". A refresh leaves this layer alone; \"Fetch again\" brings the "
                          "downloaded version back.")
            self.layers.setItem(r, 4, _item(state, note))
            name = row["name"]
            self.layers.setCellWidget(r, 5, _buttons(
                ("Fetch again", "Download this layer again now",
                 lambda _c=False, n=name: self.fetch_again(n)),
                ("Replace with my file…", "Put your own file in the pack as this layer",
                 lambda _c=False, n=name: self.replace_layer(n))))

    # ------------------------------------------------------------------ the folder
    def choose_folder(self):
        try:
            path = QFileDialog.getExistingDirectory(self, "Choose the data folder",
                                                    self.settings.data_dir)
            if not path:
                return
            note = self.plugin.adopt_data_folder(path)
            self.folder.setText(self.settings.data_dir)
            self.folder_note.setText(note)
            self._info(note)
            self.refresh_all()
        except Exception as e:  # noqa: BLE001 - never out of a slot
            self._warn(f"Could not set the data folder: {e}")

    def show_setup_text(self):
        try:
            from .ndpack.damsuite import suitecheck as SC
            items = SC.check(self.settings.data_dir)
            text = SC.text_report(items, self.settings.data_dir, SC.step_rows(items))
            exec_dialog(TextWindow("ND dam suite set-up", text, self,
                       os.path.join(self.settings.data_dir, "suite_setup.txt")))
        except Exception as e:  # noqa: BLE001
            self._warn(f"Could not make the set-up report: {e}")

    # ------------------------------------------------------------------ the layers
    def fetch_again(self, name):
        try:
            if self.plugin.is_running():
                self._info("An update is already running; wait for it or stop it first.")
                return
            self.plugin.start_update(force=True, quiet=False, only={"pack"}, layers=[name])
            self._info(f"Fetching {name} again. The layer it is on is shown at the bottom of "
                       "this window.")
        except Exception as e:  # noqa: BLE001
            self._warn(f"Could not start the fetch: {e}")

    def rebuild_missing(self):
        try:
            if self.plugin.is_running():
                self._info("An update is already running; wait for it or stop it first.")
                return
            self.plugin.start_update(force=True, quiet=False, only={"pack"}, missing_only=True)
            self._info("Building the layers that are not in the pack yet. Everything already "
                       "there is left alone.")
        except Exception as e:  # noqa: BLE001
            self._warn(f"Could not start the build: {e}")

    def replace_layer(self, name):
        try:
            path, _ = QFileDialog.getOpenFileName(
                self, f"Choose your file for {name}", self.settings.data_dir,
                "Vector files (*.gpkg *.shp *.geojson *.json *.gdb *.zip *.kml);;All files (*)")
            if not path:
                return
            pack = self.plugin.pack_path()
            if not os.path.isfile(os.path.join(pack, "manifest.json")):
                self._warn("There is no pack yet: build it first (Check for Data Updates Now).")
                return
            if QMessageBox.question(
                    self, "Replace the layer",
                    f"Put {os.path.basename(path)} in the pack as {name}?\n\nThe layer the pack "
                    "holds now is overwritten, and the manifest will record your file as its "
                    "source. Refreshes will then leave this layer alone - press \"Fetch "
                    "again\" on it when you want the downloaded version back.",
                    MB_YES | MB_CANCEL) != MB_YES:
                return
            self.plugin.busy(True)
            try:
                entry = packsource.replace_layer(pack, name, path, log=self._info)
            finally:
                self.plugin.busy(False)
            self._info(f"{name}: {entry.get('count', 0):,} features from your file.")
            self._fill_layers()
        except Exception as e:  # noqa: BLE001 - GDAL and bad files both end up here
            self.plugin.busy(False)
            self._warn(f"Could not put your file in the pack: {e}")

    def show_change_report(self):
        try:
            report = self.plugin.change_report()
            text = report.get("text") or (report.get("summary") or "") + "\n"
            exec_dialog(TextWindow("Pack change report", text, self,
                       os.path.join(self.plugin.pack_path(), runreport.REPORT_NAME)))
        except Exception as e:  # noqa: BLE001
            self._warn(f"Could not make the change report: {e}")

    def show_vintage(self):
        try:
            pack = self.plugin.pack_path()
            rows = runreport.vintage_rows(pack)
            exec_dialog(TextWindow("Data vintage", runreport.vintage_text(rows), self,
                       os.path.join(pack, "data_vintage.txt")))
        except Exception as e:  # noqa: BLE001
            self._warn(f"Could not read the data vintage: {e}")

    # ------------------------------------------------------------------ USB
    def _file_job(self, title, work):
        """Run a file job (copy / import / verify) in front of a cancellable progress dialog."""
        box = QProgressDialog(title, "Cancel", 0, 100, self)
        box.setWindowTitle("ND Data Updater")
        box.setWindowModality(WINDOW_MODAL)
        box.setMinimumDuration(0)
        box.setAutoClose(False)
        box.setAutoReset(False)
        box.show()

        def progress(done, total):
            box.setValue(int(100.0 * done / (total or 1)))
            box.setLabelText(f"{title}\n{done / 1073741824.0:.2f} of "
                             f"{total / 1073741824.0:.2f} GB")
            from qgis.PyQt.QtCore import QCoreApplication
            QCoreApplication.processEvents()

        try:
            return work(progress, box.wasCanceled)
        finally:
            box.close()

    def copy_to_usb(self):
        try:
            pack = self.plugin.pack_path()
            if not packmove.is_pack(pack):
                self._warn("There is no pack to copy yet (no manifest.json in "
                           f"{pack}). Build it first.")
                return
            dest = QFileDialog.getExistingDirectory(self, "Choose the drive or folder to copy "
                                                          "the pack to", "")
            if not dest:
                return
            size = packmove.total_size(pack) / 1073741824.0
            if QMessageBox.question(
                    self, "Copy the pack",
                    f"Copy {size:.1f} GB to {dest}?\n\nA checksum file is written beside the "
                    "data so the laptop that imports it can tell whether every file arrived.",
                    MB_YES | MB_CANCEL) != MB_YES:
                return
            out = self._file_job(
                f"Copying the pack to {dest}",
                lambda progress, cancelled: packmove.copy_pack(
                    pack, dest, progress=progress, cancelled=cancelled, log=self._info))
            self._info(f"Copied {out['files']} file(s), "
                       f"{out['bytes'] / 1073741824.0:.1f} GB, to {out['folder']} with "
                       f"{packmove.SUMS_NAME}.")
        except packmove.PackMoveError as e:
            self._warn(f"The pack was not copied: {e}")
        except Exception as e:  # noqa: BLE001
            self._warn(f"The pack was not copied: {e}")

    def import_pack(self):
        try:
            src = QFileDialog.getExistingDirectory(
                self, "Choose the pack folder on the drive (the one holding manifest.json)", "")
            if not src:
                return
            if not packmove.is_pack(src):
                self._warn(f"{src} is not a pack folder: choose the folder that holds "
                           "manifest.json (usually ND_Dam_Suite_Pack).")
                return
            have_sums = bool(packmove.read_sums(src))
            question = (f"Import the pack in {src} into {self.settings.data_dir}?\n\n"
                        + ("Every file is checked against its checksum first; nothing is "
                           "replaced if one does not match."
                           if have_sums else
                           "This pack has no checksum file, so there is no way to tell whether "
                           "it arrived whole. It can still be imported."))
            if QMessageBox.question(self, "Import a pack", question,
                                    MB_YES | MB_CANCEL) != MB_YES:
                return
            out = self._file_job(
                "Importing the pack",
                lambda progress, cancelled: packmove.import_pack(
                    src, self.settings.data_dir, progress=progress, cancelled=cancelled,
                    log=self._info, check=have_sums))
            note = self.plugin.adopt_data_folder(self.settings.data_dir)
            self.folder_note.setText(note)
            self._info(f"Imported {out['files']} file(s) into {out['folder']}"
                       + (" (checksums verified). " if out["checked"] else ". ") + note)
            self.refresh_all()
        except packmove.PackMoveError as e:
            self._warn(f"The pack was not imported: {e}")
        except Exception as e:  # noqa: BLE001
            self._warn(f"The pack was not imported: {e}")

    # ------------------------------------------------------------------ services
    def _save_on_open(self, value):
        try:
            self.settings.service_check_on_open = bool(value)
        except Exception as e:  # noqa: BLE001
            self._warn(f"Could not save that setting: {e}")

    def check_services(self):
        """Probe the services on a worker thread; a timer draws the rows when they arrive."""
        if self._service_thread is not None and self._service_thread.is_alive():
            return
        self.service_note.setText("Asking the services (a few seconds)...")
        self.service_rows = []
        box = {"rows": None}

        def work():
            try:
                box["rows"] = servicecheck.check()
            except Exception as e:  # noqa: BLE001 - a failed check is rows of "down", not a crash
                box["rows"] = [{"key": "?", "label": "The check itself failed", "url": "",
                                "what": str(e), "state": "down", "seconds": None,
                                "detail": str(e)}]

        self._service_box = box
        self._service_thread = threading.Thread(target=work, name="nd-service-check",
                                                daemon=True)
        self._service_thread.start()
        self.poll.start()

    def _poll_services(self):
        box = getattr(self, "_service_box", None)
        if box is None or box.get("rows") is None:
            return
        self.poll.stop()
        self.service_rows = box["rows"]
        self._fill_services()

    def _fill_services(self):
        rows = self.service_rows
        self.services.setRowCount(len(rows))
        for r, row in enumerate(rows):
            secs = "" if row.get("seconds") is None else f"{row['seconds']:.1f} s"
            self.services.setItem(r, 0, _item(STATE_MARK.get(row["state"], "?"), row["state"]))
            self.services.setItem(r, 1, _item(row["label"], row.get("url") or ""))
            self.services.setItem(r, 2, _item(secs))
            detail = row.get("detail") or ""
            self.services.setItem(r, 3, _item(f"{row.get('what', '')} ({detail})"
                                              if detail else row.get("what", ""),
                                              detail))
        warn = servicecheck.warning_text(rows)
        self.service_note.setText(warn or "Every service answered. A run should not hang.")
        if warn:
            self._warn(warn)
