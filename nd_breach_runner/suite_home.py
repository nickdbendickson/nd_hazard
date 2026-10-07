# -*- coding: utf-8 -*-
"""The Dam suite home window - one view of a Dams folder for the three plugins.

Byte-identical in ND Dam Hazard Classification Helper, ND Dam Breach Runner and ND Dam Breach
Screening (written by suite/suite_shared/make_styles.py; edit it there). Each plugin opens it
from its menu ("Dam suite home..."); whichever plugin opened it, the window is the same: every
dam folder of a Dams folder with its dam.json state (screened / modelled / classified / reviewed),
its class, the suite's next action (damsuite.suite_home / damsuite.dam_record.next_action) and the
plugin that does it, and buttons that open that plugin's window with the dam filled in where the
window has a field for it (the Runner: Dams folder + dam ID; Screening: the Dams folder; ND Dam
Hazard: the dam folder).

Logic is damsuite.suite_home (pure Python, tested); this file holds only the widgets. Nothing
here has been shown in a real QGIS (Qt test doubles only).
"""
import os

from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QFileDialog, QMessageBox,
)

from .damsuite import suite_home as SH
from .damsuite import suitedata as SD
from .damsuite import versions as VS

TITLE = "Dam suite home"
SCOPE = "NDDamSuite"
ENTRY = "home_dams_folder"
COLUMNS = ("Dam ID", "Name", "Class", "Existing", "Screened", "Modelled", "Classified", "Reviewed",
           "Next", "Plugin", "Notes")
# plugin key -> (plugin folder name, how to point its window at the dam)
PLUGIN_FOLDER = VS.PLUGINS


class SuiteHomeDialog(QDialog):
    def __init__(self, parent=None, iface=None, opened_from=""):
        super().__init__(parent)
        self.iface = iface
        self.opened_from = opened_from
        self.rows = []
        self.setWindowTitle(TITLE)
        self.setMinimumWidth(900)
        self._build()
        self._load()
        self.refresh()

    # ------------------------------------------------------------------ build --
    def _build(self):
        lay = QVBoxLayout()
        info = QLabel(
            "Every dam folder of the Dams folder with its dam.json state and the next action the suite "
            "suggests. Classes are ND Dam Hazard's (the Runner's / Screening's previews are not counted). "
            "Nothing here changes a file.")
        info.setWordWrap(True)
        lay.addWidget(info)
        row = QHBoxLayout()
        row.addWidget(QLabel("Dams folder:"))
        self.dams_edit = QLineEdit()
        self.dams_edit.editingFinished.connect(self._changed)
        b = QPushButton("Browse...")
        b.clicked.connect(lambda _c=False: self._browse())
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(lambda _c=False: self.refresh())
        row.addWidget(self.dams_edit, 1)
        row.addWidget(b)
        row.addWidget(self.refresh_btn)
        lay.addLayout(row)
        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        lay.addWidget(self.summary_label)
        self.table = QTableWidget()
        self.table.setColumnCount(len(COLUMNS))
        try:
            self.table.setHorizontalHeaderLabels(list(COLUMNS))
            self.table.setSortingEnabled(True)
        except Exception:                                        # noqa
            pass
        self.table.cellDoubleClicked.connect(lambda r, c: self._open_for_row(r))
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.open_btn = QPushButton("Open the plugin for the selected dam")
        self.open_btn.clicked.connect(lambda _c=False: self._open_for_row(self.table.currentRow()))
        self.folder_btn = QPushButton("Open the dam folder")
        self.folder_btn.clicked.connect(lambda _c=False: self._open_folder(self.table.currentRow()))
        # 1.5.0: the suite data folder, the servers and "Get data for this dam" (suite_data.py)
        self.data_btn = QPushButton("Data and servers...")
        self.data_btn.setToolTip("Suite data folder, WMS / ArcGIS REST servers, and Get data for "
                                 "the selected dam (its tools then fill their empty inputs).")
        self.data_btn.clicked.connect(lambda _c=False: self.open_data(self.table.currentRow()))
        row.addWidget(self.open_btn)
        row.addWidget(self.folder_btn)
        row.addWidget(self.data_btn)
        row.addStretch(1)
        for key in ("ndhazard", "ndbreach", "ndscreen"):
            btn = QPushButton(VS.LABELS.get(key, key) + "...")
            btn.clicked.connect(lambda _c=False, k=key: self.open_plugin(k))
            setattr(self, "btn_" + key, btn)
            row.addWidget(btn)
        lay.addLayout(row)
        self.plugins_label = QLabel("")
        self.plugins_label.setWordWrap(True)
        lay.addWidget(self.plugins_label)
        self.setLayout(lay)

    # --------------------------------------------------------------- settings --
    def _load(self):
        try:
            from qgis.core import QgsProject
            text, ok = QgsProject.instance().readEntry(SCOPE, ENTRY, "")
            if ok and text:
                self.dams_edit.setText(text)
        except Exception:                                        # noqa
            pass
        if not self.dams_edit.text().strip():
            self.dams_edit.setText(SD.dams_folder())     # 1.5.0: the suite's Dams folder

    def _save(self):
        try:
            from qgis.core import QgsProject
            QgsProject.instance().writeEntry(SCOPE, ENTRY, self.dams_edit.text().strip())
        except Exception:                                        # noqa
            pass
        if self.dams_edit.text().strip() and not SD.dams_folder():
            SD.set_dams_folder(self.dams_edit.text().strip())

    def _changed(self, *args):
        self._save()
        self.refresh()

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Dams folder", self.dams_edit.text().strip() or "")
        if d:
            self.dams_edit.setText(d)
            self._changed()

    # ---------------------------------------------------------------- refresh --
    def refresh(self, *args):
        dams = self.dams_edit.text().strip()
        self.rows = SH.scan(dams) if dams and os.path.isdir(dams) else []
        s = SH.summary(self.rows)
        self.summary_label.setText(SH.summary_text(s) if dams else "Choose the Dams folder.")
        try:
            self.table.setSortingEnabled(False)
        except Exception:                                        # noqa
            pass
        self.table.setRowCount(len(self.rows))
        for i, r in enumerate(self.rows):
            notes = []
            if r["disputed"]:
                notes.append("{} disputed value(s)".format(r["disputed"]))
            if r["problem"]:
                notes.append(r["problem"])
            if not r["has_record"]:
                notes.append("no dam.json")
            vals = (r["dam_id"], r["name"], r["class"], r["existing_class"], r["screened"], r["modelled"],
                    r["classified"], r["reviewed"], "{}: {}".format(r["next"], r["next_text"]),
                    VS.LABELS.get(r["plugin"], r["plugin"]), "; ".join(notes))
            for j, v in enumerate(vals):
                self.table.setItem(i, j, QTableWidgetItem(str(v)))
        try:
            self.table.setSortingEnabled(True)
            self.table.resizeColumnsToContents()
        except Exception:                                        # noqa
            pass
        inst = SH.installed_plugins()
        self.plugins_label.setText("Installed: " + "; ".join(
            "{} {}".format(VS.LABELS[k], v or "not found") for k, v in inst.items()) +
            ". Opened from {}.".format(VS.LABELS.get(self.opened_from, self.opened_from or "?")))
        have = bool(self.rows)
        self.open_btn.setEnabled(have)
        self.folder_btn.setEnabled(have)

    def row_at(self, index):
        """The scan row shown at table row `index` (the table may be sorted)."""
        if index is None or index < 0:
            return None
        try:
            item = self.table.item(index, 0)
            did = item.text() if item is not None else None
        except Exception:                                        # noqa
            did = None
        for r in self.rows:
            if r["dam_id"] == did:
                return r
        return self.rows[index] if 0 <= index < len(self.rows) else None

    # ------------------------------------------------------------------ open --
    def _open_folder(self, index):
        r = self.row_at(index)
        if r and os.path.isdir(r["folder"]):
            QDesktopServices.openUrl(QUrl.fromLocalFile(r["folder"]))

    def open_data(self, index=None):
        """1.5.0: the Suite data and servers window, on the selected dam."""
        r = self.row_at(index) if index is not None else None
        try:
            from . import suite_data
        except Exception as e:                                   # noqa
            QMessageBox.information(self, TITLE, "Suite data window not available: {}".format(e))
            return None
        if self.dams_edit.text().strip() and not SD.dams_folder():
            SD.set_dams_folder(self.dams_edit.text().strip())
        return suite_data.open_suite_data(self, self.iface, self.opened_from, self,
                                          dam_folder=r["folder"] if r else "")

    def _open_for_row(self, index):
        r = self.row_at(index)
        if r is None:
            return False
        return self.open_plugin(r["plugin"], r)

    def _plugin_object(self, key):
        try:
            from qgis.utils import plugins
            return plugins.get(PLUGIN_FOLDER.get(key, key))
        except Exception:                                        # noqa
            return None

    def open_plugin(self, key, row=None):
        """Open plugin `key`'s window, pointed at the dam of `row` when it has a field for it.
        Returns True when the window opened."""
        plug = self._plugin_object(key)
        if plug is None or not hasattr(plug, "run_plugin"):
            QMessageBox.information(self, TITLE, "{} is not installed or not enabled in this QGIS.".format(
                VS.LABELS.get(key, key)))
            return False
        try:
            plug.run_plugin()
        except Exception as e:                                   # noqa
            QMessageBox.information(self, TITLE, "{} could not be opened ({}).".format(VS.LABELS.get(key, key), e))
            return False
        if row:
            point_window_at(getattr(plug, "dialog", None), key, self.dams_edit.text().strip(), row)
        return True


def point_window_at(dialog, key, dams_folder, row):
    """Fill the other plugin's window with the dam (duck typing on the fields the three windows
    have; anything missing is skipped). Returns what was set."""
    done = []
    if dialog is None:
        return done
    try:
        if key == "ndbreach":
            if hasattr(dialog, "dams_edit"):
                dialog.dams_edit.setText(dams_folder)
                done.append("dams_folder")
            if hasattr(dialog, "dam_edit"):
                dialog.dam_edit.setText(os.path.basename(os.path.normpath(row["folder"])))
                done.append("dam_id")
        elif key == "ndscreen":
            if hasattr(dialog, "dams_folder"):
                dialog.dams_folder.setText(dams_folder)
                done.append("dams_folder")
        elif key == "ndhazard":
            if hasattr(dialog, "dam_folder_edit"):
                dialog.dam_folder_edit.setText(row["folder"])
                done.append("dam_folder")
        for name in ("_changed", "refresh"):
            fn = getattr(dialog, name, None)
            if callable(fn):
                fn()
                break
    except Exception:                                            # noqa
        pass
    return done


# ------------------------------------------------------------ the suite menu --
SUITE_MENU = "ND Dam Suite"


def _suite_menu_actions(iface):
    """The actions already in Plugins > ND Dam Suite (another suite plugin may have added them)."""
    out = {}
    try:
        from qgis.PyQt.QtWidgets import QMenu
        for menu in iface.pluginMenu().findChildren(QMenu):
            if menu.title().replace("&", "") == SUITE_MENU:
                for a in menu.actions():
                    out[a.objectName()] = a
    except Exception:                                            # noqa - no such menu yet / a test double
        pass
    return out


def add_suite_action(iface, label, object_name, slot):
    """Add an entry to the shared Plugins > ND Dam Suite menu (1.4.0: one menu for the three
    plugins). An entry with the same object name that another plugin already added is not added
    again. Returns the QAction, or None when it exists already or cannot be made."""
    if iface is None:
        return None
    if object_name in _suite_menu_actions(iface):
        return None
    try:
        try:                              # Qt6 (QGIS 4): QAction lives in QtGui
            from qgis.PyQt.QtGui import QAction
        except ImportError:               # Qt5 (QGIS 3.x): QtWidgets
            from qgis.PyQt.QtWidgets import QAction
        action = QAction(label, iface.mainWindow())
        action.setObjectName(object_name)
        action.triggered.connect(slot)
        iface.addPluginToMenu(SUITE_MENU, action)
        return action
    except Exception:                                            # noqa
        return None


def remove_suite_actions(iface, actions):
    for a in actions or []:
        try:
            iface.removePluginMenu(SUITE_MENU, a)
        except Exception:                                        # noqa
            pass
        try:
            a.deleteLater()
        except Exception:                                        # noqa
            pass


def open_home(plugin, iface, opened_from, parent=None):
    """One home window per plugin instance: re-created when Qt destroyed it. Returns it."""
    dlg = getattr(plugin, "home_dialog", None)
    alive = False
    if dlg is not None:
        try:
            try:
                from qgis.PyQt import sip
            except ImportError:               # pragma: no cover
                import sip
            alive = not sip.isdeleted(dlg)
        except Exception:                                        # noqa
            alive = False
    if not alive:
        dlg = SuiteHomeDialog(parent=parent, iface=iface, opened_from=opened_from)
        plugin.home_dialog = dlg
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    return dlg
