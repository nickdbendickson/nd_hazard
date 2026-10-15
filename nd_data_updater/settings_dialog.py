"""Settings and status dialog.

1.4.0: the folder chooser adopts the folder for the whole suite (damsuite.suitedata.adopt_folder
through the plugin), so one choice sets the updater's data folder, the plugins' suite data
folder, the pack location, the data source and the Dams folder - they used to be separate
settings and a user who set only this one got plugins looking in an empty folder. The window
also shows the running run's layer-by-layer status with a Stop button, offers "Rebuild only
missing layers" (the refresh already behaved that way; nothing said so), and opens the pack
window, which is where the per-layer rows, the USB copy and the service check live."""

import datetime
import os

from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .packsource import MODES_3DHP
from .qtcompat import (BB_CANCEL, BB_SAVE, HEADER_STRETCH, HEADER_TO_CONTENTS, ITEM_IS_EDITABLE,
                       NO_EDIT_TRIGGERS, NO_SELECTION)
from .sources import SOURCE_INFO

NSI_MODE_LABELS = (
    ("merged", "2026 and 2022 merged (2026 wins where both have a structure)"),
    ("nsi2026", "2026 only"),
    ("nsi2022", "2022 only"),
)


def _fmt_time(value):
    if not value:
        return "never"
    try:
        dt = datetime.datetime.fromisoformat(value)
    except ValueError:
        return value
    return dt.strftime("%b %d, %Y %I:%M %p").replace(" 0", " ")


def _folder_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _fmt_size(n):
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024.0


class SettingsDialog(QDialog):
    def __init__(self, settings, plugin, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.plugin = plugin
        self.setWindowTitle("ND Data Updater")
        self.setMinimumWidth(640)
        layout = QVBoxLayout(self)

        # --- where and when
        general = QGroupBox("Local data")
        form = QFormLayout(general)
        row = QHBoxLayout()
        self.data_dir = QLineEdit(settings.data_dir)
        browse = QPushButton("Choose…")
        browse.setToolTip("One choice: the pack, the drop-in folders and the three dam plugins "
                          "all follow this folder.")
        browse.clicked.connect(self._browse)
        row.addWidget(self.data_dir)
        row.addWidget(browse)
        form.addRow("Data folder:", row)
        self.folder_note = QLabel("")
        self.folder_note.setWordWrap(True)
        form.addRow("", self.folder_note)
        self.auto_check = QCheckBox("Check for updates automatically while QGIS is open")
        self.auto_check.setChecked(settings.auto_check)
        form.addRow("", self.auto_check)
        self.check_hours = QSpinBox()
        self.check_hours.setRange(1, 24 * 30)
        self.check_hours.setSuffix(" hours")
        self.check_hours.setValue(settings.check_hours)
        form.addRow("Check at most every:", self.check_hours)
        layout.addWidget(general)

        # --- sources
        box = QGroupBox("Datasets to keep up to date")
        src_form = QFormLayout(box)
        self.enabled = {}
        for sid, (name, description, _default) in SOURCE_INFO.items():
            cb = QCheckBox(name)
            cb.setChecked(settings.enabled(sid))
            cb.setToolTip(description)
            self.enabled[sid] = cb
            src_form.addRow(cb)
            if sid == "nsi":
                self.nsi_days = QSpinBox()
                self.nsi_days.setRange(1, 365)
                self.nsi_days.setSuffix(" days")
                self.nsi_days.setValue(settings.nsi_interval_days)
                self.nsi_days.setToolTip("The NSI has no version marker, so it is re-downloaded "
                                         "on this schedule and replaced only if it changed.")
                src_form.addRow("    Re-check NSI every:", self.nsi_days)
                self.nsi_mode = QComboBox()
                for value, label in NSI_MODE_LABELS:
                    self.nsi_mode.addItem(label, value)
                index = self.nsi_mode.findData(settings.nsi_mode)
                self.nsi_mode.setCurrentIndex(max(index, 0))
                self.nsi_mode.setToolTip(
                    "Merged: every NSI 2026 point is kept. A 2022 point within the match "
                    "distance of a 2026 point is folded into it (the 2026 data wins; the 2022 "
                    "id, occupancy and a mobile-home flag are kept in nsi22_* fields). 2022 "
                    "points with no 2026 point nearby are kept with nsi_version = 2022.")
                src_form.addRow("    NSI version:", self.nsi_mode)
                self.nsi_match = QSpinBox()
                self.nsi_match.setRange(5, 100)
                self.nsi_match.setSuffix(" m")
                self.nsi_match.setValue(settings.nsi_match_m)
                self.nsi_match.setToolTip("How close a 2022 point must be to a 2026 point to be "
                                          "treated as the same structure.")
                self.nsi_mode.currentIndexChanged.connect(
                    lambda _i: self.nsi_match.setEnabled(self.nsi_mode.currentData() == "merged"))
                self.nsi_match.setEnabled(settings.nsi_mode == "merged")
                src_form.addRow("    Same structure within:", self.nsi_match)
            if sid == "pack":
                self.pack_days = QSpinBox()
                self.pack_days.setRange(1, 3650)
                self.pack_days.setSuffix(" days")
                self.pack_days.setValue(settings.pack_days)
                self.pack_days.setToolTip("A pack layer older than this is fetched again at the "
                                          "next check. A layer that is missing or incomplete is "
                                          "built at every check.")
                src_form.addRow("    Refresh pack layers older than:", self.pack_days)
                self.pack_3dhp = QComboBox()
                for value, label in MODES_3DHP:
                    self.pack_3dhp.addItem(label, value)
                self.pack_3dhp.setCurrentIndex(max(self.pack_3dhp.findData(settings.pack_3dhp), 0))
                self.pack_3dhp.setToolTip(
                    "The USGS 3DHP web service often answers HTTP 504 to a state-sized request. "
                    "With the National Map file, the flowlines and waterbodies are cut out of "
                    "one download (FileGDB zip, about 12 GB, kept in <data folder>/3dhp).")
                src_form.addRow("    3DHP flowlines and waterbodies:", self.pack_3dhp)
                self.pack_3dhp_minutes = QSpinBox()
                self.pack_3dhp_minutes.setRange(1, 60)
                self.pack_3dhp_minutes.setSuffix(" minutes")
                self.pack_3dhp_minutes.setValue(settings.pack_3dhp_minutes)
                self.pack_3dhp_minutes.setToolTip(
                    "In \"auto\" the service is asked a small question and the statewide one "
                    "it will really be given. When it has not answered both within this time, "
                    "the National Map file is used instead and the log says why. Without the "
                    "cap the service answered the small question and then returned HTTP 504 "
                    "for twenty-five minutes.")
                src_form.addRow("    Give the 3DHP service at most:", self.pack_3dhp_minutes)
                self.pack_share = QCheckBox("Make the ND dam plugins read this pack")
                self.pack_share.setChecked(settings.pack_share)
                self.pack_share.setToolTip("Sets the offline pack location and the data source "
                                           "of the Hazard Helper, Breach Runner and Breach "
                                           "Screening to this pack after each build.")
                src_form.addRow("", self.pack_share)
                self.pack_full = QCheckBox("Fetch every pack layer again at the next check")
                self.pack_full.setChecked(False)
                self.pack_full.setToolTip("One time only. Takes an hour or more.")
                src_form.addRow("", self.pack_full)
            if sid == "usa_structures":
                self.usa_url = QLineEdit(settings.usa_structures_url)
                self.usa_url.setPlaceholderText("Paste the North Dakota download link from "
                                                "FEMA's USA Structures page")
                src_form.addRow("    Download link:", self.usa_url)
        layout.addWidget(box)

        # --- status
        status_box = QGroupBox("Status")
        status_layout = QVBoxLayout(status_box)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Dataset", "Last updated", "Last checked",
                                              "Size", "Note"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(NO_EDIT_TRIGGERS)
        self.table.setSelectionMode(NO_SELECTION)
        header = self.table.horizontalHeader()
        for col in range(4):
            header.setSectionResizeMode(col, HEADER_TO_CONTENTS)
        header.setSectionResizeMode(4, HEADER_STRETCH)
        status_layout.addWidget(self.table)
        buttons_row = QHBoxLayout()
        self.check_now = QPushButton("Save and Check All Now")
        self.check_now.setToolTip("Checks every enabled dataset, ignoring the NSI schedule.")
        self.check_now.clicked.connect(self._check_now)
        self.missing_now = QPushButton("Rebuild Only Missing Layers")
        self.missing_now.setToolTip("Builds the pack layers that are not there yet and leaves "
                                    "every layer it already has alone, however old it is. A "
                                    "refresh already works this way for missing layers; this "
                                    "button is how you ask for only those.")
        self.missing_now.clicked.connect(self._missing_now)
        pack_window = QPushButton("Offline Pack…")
        pack_window.setToolTip("The pack layer by layer, the drop-in folders, carrying the pack "
                               "on a USB stick, and a check of the services.")
        pack_window.clicked.connect(self._show_pack)
        open_folder = QPushButton("Open Data Folder")
        open_folder.clicked.connect(self.plugin.open_data_folder)
        buttons_row.addWidget(self.check_now)
        buttons_row.addWidget(self.missing_now)
        buttons_row.addWidget(pack_window)
        buttons_row.addWidget(open_folder)
        buttons_row.addStretch(1)
        status_layout.addLayout(buttons_row)
        run_row = QHBoxLayout()
        self.progress = QLabel("")
        self.progress.setWordWrap(True)
        self.progress.setToolTip("Which layer the running update is on.")
        self.cancel = QPushButton("Stop the update")
        self.cancel.setToolTip("The layers already built are kept and the next run continues "
                               "from there.")
        self.cancel.clicked.connect(self.plugin.cancel_update)
        self.cancel.setEnabled(False)
        run_row.addWidget(self.progress, 1)
        run_row.addWidget(self.cancel)
        status_layout.addLayout(run_row)
        layout.addWidget(status_box)

        buttons = QDialogButtonBox(BB_SAVE | BB_CANCEL)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.refresh_status()

    def _browse(self):
        """Choose the folder - and hand it to the three dam plugins in the same breath."""
        try:
            path = QFileDialog.getExistingDirectory(self, "Choose data folder",
                                                    self.data_dir.text())
            if not path:
                return
            self.data_dir.setText(path)
            note = self.plugin.adopt_data_folder(path)
            self.folder_note.setText(note)
            self.refresh_status()
        except Exception as e:  # noqa: BLE001 - never raise out of a slot
            self.folder_note.setText(f"Could not set the folder: {e}")

    def _show_pack(self):
        try:
            self.save()
            self.plugin.show_pack()
        except Exception as e:  # noqa: BLE001
            self.folder_note.setText(f"Could not open the pack window: {e}")

    def show_progress(self, text, running):
        """Called by the plugin's timer while a run is going (and once when it ends)."""
        self.progress.setText(text or "")
        self.cancel.setEnabled(bool(running))
        self.check_now.setEnabled(not running)
        self.missing_now.setEnabled(not running)

    def save(self):
        s = self.settings
        s.data_dir = self.data_dir.text().strip() or s.data_dir
        s.auto_check = self.auto_check.isChecked()
        s.check_hours = self.check_hours.value()
        s.nsi_interval_days = self.nsi_days.value()
        s.nsi_mode = self.nsi_mode.currentData()
        s.nsi_match_m = self.nsi_match.value()
        s.usa_structures_url = self.usa_url.text()
        s.pack_days = self.pack_days.value()
        s.pack_3dhp = self.pack_3dhp.currentData()
        s.pack_3dhp_minutes = self.pack_3dhp_minutes.value()
        s.pack_share = self.pack_share.isChecked()
        s.pack_full = self.pack_full.isChecked()
        for sid, cb in self.enabled.items():
            s.set_enabled(sid, cb.isChecked())

    def accept(self):
        self.save()
        super().accept()

    def _check_now(self):
        self.save()
        self.plugin.start_update(force=True, quiet=False)

    def _missing_now(self):
        """Build what is not in the pack yet, and nothing else."""
        try:
            self.save()
            self.plugin.start_update(force=True, quiet=False, only={"pack"}, missing_only=True)
        except Exception as e:  # noqa: BLE001
            self.folder_note.setText(f"Could not start the build: {e}")

    def refresh_status(self):
        manifest = self.plugin.make_updater().load_manifest()
        data_dir = self.settings.data_dir
        rows = []
        for source in self.plugin.all_sources():
            entry = manifest.get(source.id, {})
            path = os.path.join(data_dir, source.folder)
            size = _fmt_size(_folder_size(path)) if os.path.isdir(path) else "—"
            note = entry.get("last_error") or ("OK" if entry.get("updated_at") else "not downloaded")
            if source.id == "pack":
                note = entry.get("last_error") or self.plugin.pack_summary()
            enabled = self.settings.enabled(source.id)
            if not enabled:
                note = "turned off" + (f" ({note})" if entry else "")
            rows.append((source.name, _fmt_time(entry.get("updated_at")),
                         _fmt_time(entry.get("checked_at")), size, note))
        self.table.setRowCount(len(rows))
        for r, values in enumerate(rows):
            for c, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~ITEM_IS_EDITABLE)
                if c == 4:
                    item.setToolTip(value)
                self.table.setItem(r, c, item)
        self.show_progress(self.plugin.task_status(), self.plugin.is_running())
