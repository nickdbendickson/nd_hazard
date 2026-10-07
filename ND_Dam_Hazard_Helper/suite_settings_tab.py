# -*- coding: utf-8 -*-
"""v2.13: the "Suite settings" tab of the dialog - the suite settings file (every engineer-chosen
value of this plugin's Processing tools, shared with ND Dam Breach Runner and ND Dam Breach
Screening) as a grid. The tools open with the file's values; the dialog's own classification
options stay on their tabs and in the settings profiles (settings.py).

Logic: damsuite.settings and suite_settings_tools (tested); the grid widget is suite_settings.py
(the same file in the three plugins). Nothing here has been shown in a real QGIS.
"""
import os

from qgis.PyQt.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                                 QFileDialog, QMessageBox)

from .helpers import log_message

PLUGIN_KEY = "ndhazard"
FILE_FILTER = "Settings workbook (*.xlsx);;Settings table (*.csv);;All files (*)"


class SuiteSettingsTabMixin(object):

    def _build_suite_settings_tab(self):
        from . import suite_settings as SS
        from . import suite_settings_tools as ST
        from .damsuite import settings as DS
        w = QWidget()
        lay = QVBoxLayout(w)
        info = QLabel("The suite settings file: one table of the engineer-chosen values of the three "
                      "plugins' Processing tools (this plugin's are on the tabs below). The tools open "
                      "with these values; every one can still be changed in a tool's dialog. Edits here "
                      "are saved to the file at once. The classification options of this window live on "
                      "their own tabs and in the settings profiles, not here.")
        info.setWordWrap(True)
        lay.addWidget(info)
        row = QHBoxLayout()
        row.addWidget(QLabel("Settings file:"))
        self.suite_settings_file_edit = QLineEdit()
        self.suite_settings_file_edit.setText(DS.current_path() or "")
        self.suite_settings_file_edit.editingFinished.connect(lambda: self._suite_settings_file_edited())
        b = QPushButton("File...")
        b.clicked.connect(lambda _c=False: self._pick_suite_settings_file())
        n = QPushButton("New...")
        n.clicked.connect(lambda _c=False: self._new_suite_settings_file())
        row.addWidget(self.suite_settings_file_edit, 1)
        row.addWidget(b)
        row.addWidget(n)
        lay.addLayout(row)
        try:
            from .processing_provider import Provider, settings_group, SETTINGS_TABS
            catalog = ST.full_catalog(Provider.original_classes(), PLUGIN_KEY, settings_group, SETTINGS_TABS)
            tabs = SETTINGS_TABS
        except Exception as e:                                   # noqa
            catalog, tabs = [], ()
            log_message("Suite settings grid: {}".format(e))
        try:
            from .damsuite import versions as vs
            versions = {PLUGIN_KEY: vs.RELEASE.get(PLUGIN_KEY, "")}
        except Exception:                                        # noqa
            versions = {}
        self.suite_grid = SS.SettingsGrid(PLUGIN_KEY, catalog, DS.Settings(), parent=w,
                                          on_change=lambda _s: self._autosave_suite_settings(),
                                          on_file=self._suite_grid_file, versions=versions, tab_order=tabs)
        lay.addWidget(self.suite_grid, 1)
        p = self.suite_settings_file_edit.text().strip()
        if p and os.path.isfile(p):
            try:
                self.suite_grid.load(p, quiet=True)
            except Exception as e:                               # noqa
                log_message("Suite settings file: {}".format(e))
        return w

    def _suite_settings_file_edited(self):
        from .damsuite import settings as DS
        p = self.suite_settings_file_edit.text().strip()
        DS.set_current_path(p)
        if not p:
            self.suite_grid.settings = DS.Settings()
            self.suite_grid.fill()
            return False
        if not os.path.isfile(p):
            QMessageBox.warning(self, "Suite settings", "The settings file does not exist: {}".format(p))
            return False
        return self.suite_grid.load(p)

    def _pick_suite_settings_file(self):
        start = self.suite_settings_file_edit.text().strip() or ""
        p, _f = QFileDialog.getOpenFileName(self, "Settings file", start, FILE_FILTER)
        if p:
            self.suite_settings_file_edit.setText(p)
            self._suite_settings_file_edited()

    def _new_suite_settings_file(self):
        from .damsuite import settings as DS
        p, _f = QFileDialog.getSaveFileName(self, "New settings file", DS.DEFAULT_NAME,
                                            "Settings workbook (*.xlsx);;Settings table (*.csv)")
        if not p:
            return ""
        if not os.path.splitext(p)[1]:
            p += ".xlsx"
        self.suite_settings_file_edit.setText(p)
        self.suite_grid.settings.path = p
        self.suite_grid.save(p, name=os.path.splitext(os.path.basename(p))[0].replace("_", " "))
        DS.set_current_path(p)
        return p

    def _suite_grid_file(self, path):
        from .damsuite import settings as DS
        if path and path != self.suite_settings_file_edit.text().strip():
            self.suite_settings_file_edit.setText(path)
        DS.set_current_path(path)

    def _autosave_suite_settings(self):
        from .damsuite import settings as DS
        p = self.suite_settings_file_edit.text().strip()
        if not p:
            return
        try:
            self.suite_grid.settings.path = p
            self.suite_grid.settings.save(p, catalog=self.suite_grid.catalog,
                                          versions=self.suite_grid.versions or None)
            DS.set_current_path(p)
        except DS.SettingsError as e:
            log_message("Suite settings not saved: {}".format(e))
