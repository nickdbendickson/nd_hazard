# -*- coding: utf-8 -*-
"""The suite settings grid - one table of every engineer-chosen value, HEC-RAS style.

Byte-identical in ND Dam Hazard Classification Helper, ND Dam Breach Runner and ND Dam Breach
Screening (written by suite/suite_shared/make_styles.py; edit it there). Each plugin's popup
shows it on its Settings tab: one sub-tab per family of parameters, rows = the parameters with
their Value, Unit and Starting value, a Find box, "show only YOUR values" (the engineer-chosen
thresholds), "show only changed", the description of the selected row, and Reset / Load / Save.
The values live in a damsuite.settings.Settings object that the plugin saves as the shared
`damsuite_settings.xlsx` (or .csv) - the file the tools read as their defaults.

Logic is damsuite.settings (pure Python, tested); this file holds only the widgets. Nothing here
has been shown in a real QGIS (Qt test doubles only).
"""
import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QFont
from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QCheckBox, QTabWidget,
    QTableWidget, QTableWidgetItem, QComboBox, QFileDialog, QMessageBox,
)

from .damsuite import settings as DS

COLUMNS = ("#", "Parameter", "Value", "Unit", "Starting value")
C_NUM, C_LABEL, C_VALUE, C_UNIT, C_DEFAULT = range(5)
FILE_FILTER = "Settings workbook (*.xlsx);;Settings table (*.csv)"
YOUR_MARK = "  [YOUR value]"


def _qt(name, member):
    """Qt.<name>.<member> (Qt6 / scoped) or Qt.<member> (Qt5 legacy); None when neither exists."""
    for get in (lambda: getattr(getattr(Qt, name), member), lambda: getattr(Qt, member)):
        try:
            return get()
        except AttributeError:
            continue
    return None


def _flags(*names):
    out = None
    for n in names:
        f = _qt("ItemFlag", n)
        if f is None:
            continue
        out = f if out is None else (out | f)
    return out


USER_ROLE = _qt("ItemDataRole", "UserRole")
CHECKED, UNCHECKED = _qt("CheckState", "Checked"), _qt("CheckState", "Unchecked")
FLAGS_FIXED = _flags("ItemIsSelectable", "ItemIsEnabled")
FLAGS_EDIT = _flags("ItemIsSelectable", "ItemIsEnabled", "ItemIsEditable")
FLAGS_CHECK = _flags("ItemIsSelectable", "ItemIsEnabled", "ItemIsUserCheckable")


def _set_flags(item, flags):
    if flags is not None:
        try:
            item.setFlags(flags)
        except Exception:                                        # noqa
            pass


def _bold(item, on):
    try:
        f = QFont()
        f.setBold(bool(on))
        item.setFont(f)
    except Exception:                                            # noqa
        pass


def _fmt(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float):
        return "{:g}".format(v)
    return str(v)


class SettingsGrid(QWidget):
    """The grid. `catalog` = [damsuite.settings.Setting] of this plugin's tools (in tab order),
    `settings` = the damsuite.settings.Settings object edited in place. `on_change(settings)` is
    called after every edit, load or reset; `on_file(path)` after a load / save."""

    def __init__(self, plugin, catalog, settings=None, parent=None, on_change=None, on_file=None,
                 versions=None, tab_order=None, extra_tabs=()):
        super().__init__(parent)
        self.extra_tabs = list(extra_tabs or [])          # [(title, widget)] shown before the grid's tabs
        self.plugin = plugin
        self.catalog = list(catalog or [])
        self.settings = settings if settings is not None else DS.Settings()
        self.on_change, self.on_file = on_change, on_file
        self.versions = dict(versions or {})
        self.by_id = DS.index_catalog(self.catalog)
        self.groups = []
        for s in self.catalog:
            if s.group not in self.groups:
                self.groups.append(s.group)
        if tab_order:
            self.groups.sort(key=lambda g: (tab_order.index(g) if g in tab_order else 99))
        self.tables = {}                  # group -> QTableWidget
        self.items = {}                   # setting id -> (table, row)
        self.combos = {}                  # setting id -> QComboBox (enums)
        self._filling = False
        self._build()
        self.fill()

    # ------------------------------------------------------------------ build --
    def _build(self):
        lay = QVBoxLayout()
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Find:"))
        self.find_edit = QLineEdit()
        self.find_edit.setToolTip("Part of a parameter name or of its help text; empty = all rows.")
        self.find_edit.textChanged.connect(lambda _t: self.apply_filter())
        bar.addWidget(self.find_edit)
        self.only_your = QCheckBox("show only YOUR values")
        self.only_your.setToolTip("The thresholds the engineer chooses - the suite has no standard for them.")
        self.only_your.toggled.connect(lambda _c: self.apply_filter())
        self.only_changed = QCheckBox("show only changed")
        self.only_changed.toggled.connect(lambda _c: self.apply_filter())
        bar.addWidget(self.only_your)
        bar.addWidget(self.only_changed)
        bar.addStretch(1)
        self.file_label = QLabel("")
        bar.addWidget(self.file_label)
        lay.addLayout(bar)
        self.tabs = QTabWidget()
        for title, widget in self.extra_tabs:
            self.tabs.addTab(widget, title)
        for g in self.groups:
            t = QTableWidget()
            t.setColumnCount(len(COLUMNS))
            try:
                t.setHorizontalHeaderLabels(list(COLUMNS))
                t.verticalHeader().setVisible(False)
                t.setAlternatingRowColors(True)
            except Exception:                                    # noqa
                pass
            t.itemChanged.connect(self._item_changed)
            t.currentCellChanged.connect(lambda r, c, pr, pc, tbl=t: self._show_help(tbl, r))
            t.cellClicked.connect(lambda r, c, tbl=t: self._show_help(tbl, r))
            self.tables[g] = t
            self.tabs.addTab(t, g)
        lay.addWidget(self.tabs, 1)
        legend = QLabel("bold = changed from the starting value.  [YOUR value] = an engineer-chosen threshold, "
                        "not a standard.  Cell sizes stay in metres (the DEM's unit); every other distance is "
                        "in feet, areas in acres, volumes in acre-feet.")
        legend.setWordWrap(True)
        lay.addWidget(legend)
        self.help_label = QLabel("")
        self.help_label.setWordWrap(True)
        self.help_label.setMinimumHeight(48)
        lay.addWidget(self.help_label)
        row = QHBoxLayout()
        self.reset_tab_btn = QPushButton("Reset this tab")
        self.reset_tab_btn.clicked.connect(lambda _c=False: self.reset_tab())
        self.reset_all_btn = QPushButton("Reset all")
        self.reset_all_btn.clicked.connect(lambda _c=False: self.reset_all())
        self.load_btn = QPushButton("Load...")
        self.load_btn.clicked.connect(lambda _c=False: self.load())
        self.save_btn = QPushButton("Save")
        self.save_btn.clicked.connect(lambda _c=False: self.save())
        self.save_as_btn = QPushButton("Save as...")
        self.save_as_btn.clicked.connect(lambda _c=False: self.save_as())
        for b in (self.reset_tab_btn, self.reset_all_btn, self.load_btn, self.save_btn, self.save_as_btn):
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        self.setLayout(lay)

    # ------------------------------------------------------------------- fill --
    def fill(self):
        """(Re)build every table from the catalog and the current values."""
        self._filling = True
        try:
            self.items, self.combos = {}, {}
            for g, t in self.tables.items():
                rows = [s for s in self.catalog if s.group == g]
                t.blockSignals(True)
                t.setRowCount(len(rows))
                for i, s in enumerate(rows):
                    stored = self.settings.values.get(s.id)
                    typed, _problem = DS.coerce(stored, s)
                    changed = typed is not None and not DS._same(typed, s.default, s)
                    num = QTableWidgetItem(str(i + 1))
                    _set_flags(num, FLAGS_FIXED)
                    label = QTableWidgetItem(s.label + (YOUR_MARK if s.your else "") +
                                             ("" if not s.tool_title else ""))
                    label.setToolTip("{} - {} ({})".format(s.tool_title or s.tool, s.key, s.help or "").strip())
                    _set_flags(label, FLAGS_FIXED)
                    unit = QTableWidgetItem(s.unit)
                    _set_flags(unit, FLAGS_FIXED)
                    default = QTableWidgetItem(_fmt(DS.display_value(s.default, s)))
                    _set_flags(default, FLAGS_FIXED)
                    t.setItem(i, C_NUM, num)
                    t.setItem(i, C_LABEL, label)
                    t.setItem(i, C_UNIT, unit)
                    t.setItem(i, C_DEFAULT, default)
                    value = QTableWidgetItem("")
                    value.setData(USER_ROLE if USER_ROLE is not None else 256, "|".join(s.id))
                    shown = s.default if typed is None else typed
                    if s.kind == "enum":
                        combo = QComboBox()
                        for j, opt in enumerate(s.options):
                            combo.addItem(str(opt), j)
                        idx = DS.enum_index(shown, s.options)
                        if idx is not None and 0 <= idx < max(len(s.options), 1):
                            combo.setCurrentIndex(idx)
                        combo.currentIndexChanged.connect(lambda j, sid=s.id: self._combo_changed(sid, j))
                        _set_flags(value, FLAGS_FIXED)
                        t.setItem(i, C_VALUE, value)
                        t.setCellWidget(i, C_VALUE, combo)
                        self.combos[s.id] = combo
                    elif s.kind == "bool":
                        _set_flags(value, FLAGS_CHECK)
                        value.setText("TRUE" if shown else "FALSE")
                        if CHECKED is not None:
                            value.setCheckState(CHECKED if shown else UNCHECKED)
                        t.setItem(i, C_VALUE, value)
                    else:
                        _set_flags(value, FLAGS_EDIT)
                        value.setText(_fmt(shown))
                        t.setItem(i, C_VALUE, value)
                    _bold(value, changed)
                    _bold(label, changed)
                    self.items[s.id] = (t, i)
                t.blockSignals(False)
                try:
                    t.resizeColumnsToContents()
                except Exception:                                # noqa
                    pass
        finally:
            self._filling = False
        self.apply_filter()
        self._update_file_label()

    def _update_file_label(self):
        n = len(self.settings.changed(self.catalog))
        path = self.settings.path or ""
        self.file_label.setText(("{} ".format(os.path.basename(path)) if path else "no settings file ") +
                                "({} changed)".format(n))

    # ------------------------------------------------------------------ edits --
    def _setting_of(self, item):
        sid = item.data(USER_ROLE if USER_ROLE is not None else 256)
        if not sid:
            return None
        return self.by_id.get(tuple(str(sid).split("|")))

    def _item_changed(self, item):
        if self._filling:
            return
        s = self._setting_of(item)
        if s is None or s.kind == "enum":
            return
        if s.kind == "bool":
            st = item.checkState()
            if st is None:
                raw = item.text()
            else:
                raw = (st == CHECKED)
        else:
            raw = item.text()
        self.set_value(s.id, raw, from_widget=True)

    def _combo_changed(self, sid, index):
        if self._filling:
            return
        self.set_value(sid, index, from_widget=True)

    def set_value(self, sid, raw, from_widget=False):
        """Store a value (text as typed, an enum index, or a bool); empty = back to the default.
        Returns the problem text ('' when accepted)."""
        s = self.by_id.get(tuple(sid))
        if s is None:
            return "unknown setting"
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            self.settings.set(*s.id, value=None)
            problem = ""
        else:
            typed, problem = DS.coerce(raw, s)
            if problem:
                if from_widget:
                    QMessageBox.information(self, "Settings", "{}: {}".format(s.label, problem))
                self._refresh_row(s)
                return problem
            if s.kind == "enum":
                self.settings.set(*s.id, value=str(s.options[typed]) if s.options else typed)
            else:
                self.settings.set(*s.id, value=typed)
        self._refresh_row(s)
        self._update_file_label()
        if self.on_change:
            self.on_change(self.settings)
        return problem

    def value(self, sid):
        """The typed value in effect for a setting (stored, else its default)."""
        s = self.by_id.get(tuple(sid))
        if s is None:
            return None
        v, _p = DS.coerce(self.settings.values.get(s.id), s)
        return s.default if v is None else v

    def _refresh_row(self, s):
        t, i = self.items.get(s.id, (None, None))
        if t is None:
            return
        typed, _p = DS.coerce(self.settings.values.get(s.id), s)
        shown = s.default if typed is None else typed
        changed = typed is not None and not DS._same(typed, s.default, s)
        self._filling = True
        try:
            t.blockSignals(True)
            item = t.item(i, C_VALUE)
            if s.kind == "enum":
                combo = self.combos.get(s.id)
                idx = DS.enum_index(shown, s.options)
                if combo is not None and idx is not None:
                    combo.setCurrentIndex(idx)
            elif s.kind == "bool":
                if item is not None:
                    item.setText("TRUE" if shown else "FALSE")
                    if CHECKED is not None:
                        item.setCheckState(CHECKED if shown else UNCHECKED)
            elif item is not None:
                item.setText(_fmt(shown))
            if item is not None:
                _bold(item, changed)
            lab = t.item(i, C_LABEL)
            if lab is not None:
                _bold(lab, changed)
            t.blockSignals(False)
        finally:
            self._filling = False

    # ------------------------------------------------------------------ filter --
    def apply_filter(self):
        text = (self.find_edit.text() or "").strip().lower()
        your, changed_only = self.only_your.isChecked(), self.only_changed.isChecked()
        changed = {s.id for s, _v in self.settings.changed(self.catalog)}
        for s in self.catalog:
            t, i = self.items.get(s.id, (None, None))
            if t is None:
                continue
            hide = False
            if text and text not in (s.label + " " + (s.help or "") + " " + s.key + " " + (s.tool_title or "")).lower():
                hide = True
            if your and not s.your:
                hide = True
            if changed_only and s.id not in changed:
                hide = True
            try:
                t.setRowHidden(i, hide)
            except Exception:                                    # noqa
                pass

    def visible_ids(self):
        """[setting id] of the rows the filter shows (for tests)."""
        out = []
        for s in self.catalog:
            t, i = self.items.get(s.id, (None, None))
            if t is not None and not t.isRowHidden(i):
                out.append(s.id)
        return out

    # -------------------------------------------------------------------- help --
    def _show_help(self, table, row):
        for s in self.catalog:
            t, i = self.items.get(s.id, (None, None))
            if t is table and i == row:
                self.help_label.setText("Row {} - {} - {}: {}{}".format(
                    i + 1, s.tool_title or s.tool, s.key, s.help or s.label,
                    "" if not s.your else "  YOUR value: the starting value is a working guess, not a standard."))
                return
        self.help_label.setText("")

    # ------------------------------------------------------------------- reset --
    def current_group(self):
        try:
            i = self.tabs.currentIndex()
        except Exception:                                        # noqa
            i = 0
        if not isinstance(i, int):
            i = 0
        i -= len(self.extra_tabs)
        if i < 0 or i >= len(self.groups):
            i = 0
        return self.groups[i] if self.groups else None

    def reset_tab(self, group=None):
        group = group or self.current_group()
        for s in self.catalog:
            if s.group == group:
                self.settings.set(*s.id, value=None)
        self.fill()
        if self.on_change:
            self.on_change(self.settings)

    def reset_all(self):
        for s in self.catalog:
            self.settings.set(*s.id, value=None)
        self.fill()
        if self.on_change:
            self.on_change(self.settings)

    # ------------------------------------------------------------- load / save --
    def load(self, path=None, quiet=False):
        if path is None:
            start = os.path.dirname(self.settings.path) if self.settings.path else ""
            path, _f = QFileDialog.getOpenFileName(self, "Settings file", start, FILE_FILTER)
        if not path:
            return False
        try:
            loaded = DS.Settings.load(path)
        except DS.SettingsError as e:
            QMessageBox.warning(self, "Settings", str(e))
            return False
        # keep the other plugins' rows that this grid does not show
        self.settings.values = loaded.values
        self.settings.meta = loaded.meta
        self.settings.path = path
        unknown = self.settings.unknown(self.catalog, self.plugin)
        bad = []
        for s in self.catalog:
            v, problem = DS.coerce(self.settings.values.get(s.id), s)
            if problem:
                bad.append("{}: {}".format(s.label, problem))
        self.fill()
        if (unknown or bad) and not quiet:
            QMessageBox.information(self, "Settings", "Loaded {}.\n{}{}".format(
                path, ("{} row(s) this version does not know (kept as they are): {}\n".format(
                    len(unknown), ", ".join("{}/{}".format(t, k) for _p, t, k in unknown[:8]))) if unknown else "",
                ("Values not usable (the starting value is used): " + "; ".join(bad[:8])) if bad else ""))
        if self.on_change:
            self.on_change(self.settings)
        if self.on_file:
            self.on_file(path)
        return True

    def save(self, path=None, name=None):
        path = path or self.settings.path
        if not path:
            return self.save_as()
        try:
            self.settings.save(path, catalog=self.catalog, name=name, versions=self.versions or None)
        except DS.SettingsError as e:
            QMessageBox.warning(self, "Settings", str(e))
            return ""
        self._update_file_label()
        if self.on_file:
            self.on_file(path)
        return path

    def save_as(self, path=None):
        if path is None:
            start = self.settings.path or DS.DEFAULT_NAME
            path, _f = QFileDialog.getSaveFileName(self, "Save the settings as", start, FILE_FILTER)
        if not path:
            return ""
        if not os.path.splitext(path)[1]:
            path += ".xlsx"
        return self.save(path)
