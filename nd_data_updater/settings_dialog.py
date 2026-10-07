"""Settings and status dialog."""

import datetime
import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QSpinBox,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

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
        browse.clicked.connect(self._browse)
        row.addWidget(self.data_dir)
        row.addWidget(browse)
        form.addRow("Data folder:", row)
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
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        header = self.table.horizontalHeader()
        for col in range(4):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        status_layout.addWidget(self.table)
        buttons_row = QHBoxLayout()
        self.check_now = QPushButton("Save and Check All Now")
        self.check_now.setToolTip("Checks every enabled dataset, ignoring the NSI schedule.")
        self.check_now.clicked.connect(self._check_now)
        open_folder = QPushButton("Open Data Folder")
        open_folder.clicked.connect(self.plugin.open_data_folder)
        buttons_row.addWidget(self.check_now)
        buttons_row.addWidget(open_folder)
        buttons_row.addStretch(1)
        status_layout.addLayout(buttons_row)
        layout.addWidget(status_box)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.refresh_status()

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "Choose data folder", self.data_dir.text())
        if path:
            self.data_dir.setText(path)

    def save(self):
        s = self.settings
        s.data_dir = self.data_dir.text().strip() or s.data_dir
        s.auto_check = self.auto_check.isChecked()
        s.check_hours = self.check_hours.value()
        s.nsi_interval_days = self.nsi_days.value()
        s.nsi_mode = self.nsi_mode.currentData()
        s.nsi_match_m = self.nsi_match.value()
        s.usa_structures_url = self.usa_url.text()
        for sid, cb in self.enabled.items():
            s.set_enabled(sid, cb.isChecked())

    def accept(self):
        self.save()
        super().accept()

    def _check_now(self):
        self.save()
        self.plugin.start_update(force=True, quiet=False)

    def refresh_status(self):
        manifest = self.plugin.make_updater().load_manifest()
        data_dir = self.settings.data_dir
        rows = []
        for source in self.plugin.all_sources():
            entry = manifest.get(source.id, {})
            path = os.path.join(data_dir, source.folder)
            size = _fmt_size(_folder_size(path)) if os.path.isdir(path) else "—"
            note = entry.get("last_error") or ("OK" if entry.get("updated_at") else "not downloaded")
            enabled = self.settings.enabled(source.id)
            if not enabled:
                note = "turned off" + (f" ({note})" if entry else "")
            rows.append((source.name, _fmt_time(entry.get("updated_at")),
                         _fmt_time(entry.get("checked_at")), size, note))
        self.table.setRowCount(len(rows))
        for r, values in enumerate(rows):
            for c, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if c == 4:
                    item.setToolTip(value)
                self.table.setItem(r, c, item)
        self.check_now.setEnabled(not self.plugin.is_running())
