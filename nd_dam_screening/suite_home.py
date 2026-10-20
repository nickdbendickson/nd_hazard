# -*- coding: utf-8 -*-
"""The Dam suite home window - the front door of the suite, and one view of a Dams folder.

Byte-identical in ND Dam Hazard Classification Helper, ND Dam Breach Runner and ND Dam Breach
Screening (the Hazard Helper's copy is the master; the release sync copies it). Each plugin opens
it from its menu ("Dam suite home...").

Three tabs:

  Start            the suite folder, chosen once, and the four numbered steps - Data, Screen,
                   Model, Classify - each with a readiness light, the plugin that does it and,
                   when it cannot run, the reason. Under them every thing that can be set up,
                   as lights with an "Open folder" button and a one-line "what goes here", and
                   "Check my setup" which prints the lot as text to paste into a message.
  Dams             every dam folder of a Dams folder with its dam.json state (screened /
                   modelled / classified / reviewed), its class, the suite's next action
                   (damsuite.suite_home / damsuite.dam_record.next_action) and the plugin that
                   does it, and buttons that open that plugin's window with the dam filled in.
  Getting started  the order to do things in on a new machine, in plain words.

Why the Start tab exists (1.16.0): a new user could not tell which window came first, which
folder went where, or why a Run button was grey. The four steps answer the first, one folder
chooser that calls damsuite.suitedata.adopt_folder answers the second (it sets the data folder,
makes the sub-folders, adopts a pack in it and takes a Dams folder, so the updater's folder and
the plugins' folder can no longer drift apart), and the lights plus each step's "blocked by"
line answer the third.

Logic is damsuite.suite_home, damsuite.suitecheck and damsuite.suitedata (pure Python, tested);
this file holds only widgets. Nothing here has been shown in a real QGIS (Qt test doubles only).
"""
import os

from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QLineEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QFileDialog, QMessageBox, QTabWidget, QWidget, QGroupBox, QTextEdit,
    QScrollArea,
)

from .damsuite import suite_home as SH
from .tokens import TOKENS
from .damsuite import suitecheck as SC
from .damsuite import suitedata as SD
from .damsuite import versions as VS

TITLE = "Dam suite home"
SCOPE = "NDDamSuite"
ENTRY = "home_dams_folder"
COLUMNS = ("Dam ID", "Name", "Class", "Existing", "Screened", "Modelled", "Classified", "Reviewed",
           "Next", "Plugin", "Notes")
# plugin key -> (plugin folder name, how to point its window at the dam)
PLUGIN_FOLDER = VS.PLUGINS
# 1.16.0: the light for each check level - a colour and a word, never colour alone
MARKS = {"ok": ("●", TOKENS["success"], "OK"), "info": ("○", TOKENS["muted"], "-"),
         "warn": ("●", TOKENS["warn"], "check"), "error": ("●", TOKENS["error"], "stop")}
GUIDE = """\
Getting started on a new machine

1. Install all four plugins from the same release, in the QGIS Plugin Manager:
   ND Data Updater, ND Dam Breach Screening, ND Dam Breach Runner, ND Dam Hazard
   Classification Helper. A mixed set is the commonest cause of a hand-off that does nothing;
   the Start tab tells you when the versions do not match.

2. Choose one folder for the whole suite on the Start tab and press "Set up this folder".
   Everything lives under it:

       terrain/   filled by the plugins as you run dams; nothing to put here
       nlcd/      one land-cover raster for the state (or let the updater fetch it)
       dem/       your own elevation mosaic, if you have one
       dwr/       the DWR dams inventory and the dam lines, exported from DWR
       hifld/     the HIFLD lifeline layers
       Dams/      one folder per dam, written by the plugins
       ND_Dam_Suite_Pack/   the offline pack, built by the updater

   You only choose this folder once: every window reads the same setting.

3. Put your own files in dwr/ and hifld/ (and dem/, nlcd/ if you have them). The Start tab has
   an "Open folder" button and a line saying what goes in each one.

4. Step 1, Data: open the ND Data Updater and build the offline pack. It takes a while and can
   be left running; what it cannot fetch it says plainly at the end, and the suite runs without
   those layers.

5. Step 2, Screen: open ND Dam Breach Screening and screen a county, or one dam. You get a
   first hazard class for every dam and a priority list of the ones worth modelling.

6. Step 3, Model: open the ND Dam Breach Runner on a dam from that list and run the breach
   model. The Screening window's "Open Runner..." button carries the dam over.

7. Step 4, Classify: open the ND Dam Hazard Classification Helper on the same dam and run the
   classification. The Runner's "Open ND Dam Hazard..." button carries the dam over, and the
   report comes out at the end.

A worked example to check your set-up against: Absaraka Dam (DWR 2801) screens Medium, with 26
structures wet and about 1,615 acre-feet of storage, in roughly a quarter of an hour.

If a Run button is grey, the Start tab says what is missing. If something looks wrong, press
"Check my setup" and paste the text into your message.
"""


class SuiteHomeDialog(QDialog):
    def __init__(self, parent=None, iface=None, opened_from=""):
        super().__init__(parent)
        self.iface = iface
        self.opened_from = opened_from
        self.rows = []
        self.items = []
        self.steps = []
        self.step_widgets = {}
        self.check_rows = {}
        self.setWindowTitle(TITLE)
        self.setMinimumWidth(1000)          # 900 truncated the plugin button labels
        try:
            from qgis.PyQt.QtWidgets import QApplication
            self.setMaximumHeight(max(600, QApplication.primaryScreen().geometry().height() - 80))
        except Exception:                                        # noqa
            pass
        self._build()
        self._load()
        self.refresh()
        try:
            from .ui_widgets import ellipsize
            ellipsize(self)                       # one '…' on every opener button
        except Exception:                                        # noqa: the tests' stand-in Qt
            pass

    # ------------------------------------------------------------------ build --
    def _build(self):
        lay = QVBoxLayout()
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_start(), "Start")
        self.tabs.addTab(self._build_dams(), "Dams")
        self.tabs.addTab(self._build_guide(), "Getting started")
        lay.addWidget(self.tabs, 1)
        self.setLayout(lay)

    # ----- the Start tab: one folder, four steps, every light ------------------
    def _build_start(self):
        page = QWidget()
        lay = QVBoxLayout()
        intro = QLabel("One folder for the whole suite, then the four steps in order. A step "
                       "that cannot run says why.")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        box = QGroupBox("Suite folder")
        row = QHBoxLayout()
        self.folder_edit = QLineEdit()
        self.folder_edit.editingFinished.connect(lambda: self._folder_changed())
        browse = QPushButton("Browse...")
        browse.clicked.connect(lambda _c=False: self._browse_folder())
        self.setup_btn = QPushButton("Set up this folder")
        self.setup_btn.setToolTip("Make the sub-folders, adopt an offline pack that is already "
                                  "in the folder, and point every plugin at it.")
        self.setup_btn.clicked.connect(lambda _c=False: self._setup_folder())
        row.addWidget(self.folder_edit, 1)
        row.addWidget(browse)
        row.addWidget(self.setup_btn)
        box.setLayout(row)
        lay.addWidget(box)

        self.ready_label = QLabel("")
        self.ready_label.setWordWrap(True)
        lay.addWidget(self.ready_label)

        steps_box = QGroupBox("The four steps")
        grid = QGridLayout()
        for i, (key, label, plugin, why, _needs) in enumerate(SC.STEPS):
            light = QLabel("")
            btn = QPushButton(label.split(". ", 1)[-1] + "…")
            btn.setMinimumWidth(110)
            btn.clicked.connect(lambda _c=False, k=plugin: self.open_plugin(k))
            num = QLabel(label.split(".", 1)[0] + ".")
            text = QLabel(why)
            text.setWordWrap(True)
            note = QLabel("")
            note.setWordWrap(True)
            grid.addWidget(light, i, 0)
            grid.addWidget(num, i, 1)
            grid.addWidget(btn, i, 2)
            grid.addWidget(text, i, 3)
            grid.addWidget(note, i, 4)
            try:
                grid.setColumnStretch(3, 1)
            except Exception:                                    # noqa
                pass
            self.step_widgets[key] = {"light": light, "button": btn, "note": note}
        steps_box.setLayout(grid)
        lay.addWidget(steps_box)

        checks_box = QGroupBox("What is set up")
        cl = QVBoxLayout()
        self.checks_grid = QGridLayout()
        self.checks_host = QWidget()
        self.checks_host.setLayout(self.checks_grid)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(self.checks_host)
        cl.addWidget(area, 1)
        row = QHBoxLayout()
        self.check_btn = QPushButton("Check my setup")
        self.check_btn.setToolTip("Print all of this as text, to paste into a message.")
        self.check_btn.clicked.connect(lambda _c=False: self.show_check_text())
        self.recheck_btn = QPushButton("Check again")
        self.recheck_btn.clicked.connect(lambda _c=False: self.refresh())
        self.data_btn = QPushButton("Data and servers...")
        self.data_btn.setToolTip("Suite data folder, WMS / ArcGIS REST servers, and Get data for "
                                 "the selected dam (its tools then fill their empty inputs).")
        self.data_btn.clicked.connect(lambda _c=False: self.open_data(None))
        row.addWidget(self.check_btn)
        row.addWidget(self.recheck_btn)
        row.addWidget(self.data_btn)
        row.addStretch(1)
        cl.addLayout(row)
        checks_box.setLayout(cl)
        lay.addWidget(checks_box, 1)
        page.setLayout(lay)
        return page

    # ----- the Dams tab: what every earlier release showed --------------------
    def _build_dams(self):
        page = QWidget()
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
        # two rows of buttons: six in one row truncated their labels at 900 px
        row = QHBoxLayout()
        self.open_btn = QPushButton("Open the plugin for the selected dam")
        self.open_btn.clicked.connect(lambda _c=False: self._open_for_row(self.table.currentRow()))
        self.folder_btn = QPushButton("Open the dam folder")
        self.folder_btn.clicked.connect(lambda _c=False: self._open_folder(self.table.currentRow()))
        self.dam_data_btn = QPushButton("Data for this dam...")
        self.dam_data_btn.setToolTip("Suite data folder, servers, and Get data for the selected "
                                     "dam.")
        self.dam_data_btn.clicked.connect(lambda _c=False: self.open_data(self.table.currentRow()))
        row.addWidget(self.open_btn)
        row.addWidget(self.folder_btn)
        row.addWidget(self.dam_data_btn)
        row.addStretch(1)
        lay.addLayout(row)
        row = QHBoxLayout()
        for key in ("ndupdater", "ndscreen", "ndbreach", "ndhazard"):
            btn = QPushButton(VS.LABELS.get(key, key) + "…")
            btn.clicked.connect(lambda _c=False, k=key: self.open_plugin(k))
            setattr(self, "btn_" + key, btn)
            row.addWidget(btn)
        lay.addLayout(row)
        self.plugins_label = QLabel("")
        self.plugins_label.setWordWrap(True)
        lay.addWidget(self.plugins_label)
        page.setLayout(lay)
        return page

    def _build_guide(self):
        page = QWidget()
        lay = QVBoxLayout()
        self.guide_text = QTextEdit()
        self.guide_text.setReadOnly(True)
        self.guide_text.setPlainText(GUIDE)
        try:
            self.guide_text.setLineWrapMode(QTextEdit.NoWrap)
        except Exception:                                        # noqa
            pass
        lay.addWidget(self.guide_text, 1)
        page.setLayout(lay)
        return page

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
        try:
            self.folder_edit.setText(SD.data_folder())
        except Exception:                                        # noqa
            pass

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

    # ------------------------------------------------------- the suite folder --
    def _browse_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Suite folder",
                                             self.folder_edit.text().strip() or "")
        if d:
            self.folder_edit.setText(d)
            self._setup_folder()

    def _folder_changed(self):
        """The text was edited: show that folder's lights, but create nothing.

        Typing a path must not make sub-folders in it - a typo would litter whatever folder was
        named. The lights then say "sub-folders missing: press Set up this folder", and Browse
        and the button are what actually set it up."""
        self.refresh()

    def _setup_folder(self):
        """One folder for the whole suite (damsuite.suitedata.adopt_folder)."""
        path = self.folder_edit.text().strip()
        if not path:
            self.refresh()
            return None
        try:
            got = SD.adopt_folder(path)
        except Exception as e:                                   # noqa
            QMessageBox.information(self, TITLE, "That folder could not be set up: {}".format(e))
            return None
        said = []
        if got.get("created"):
            said.append("made " + ", ".join(got["created"]) + "/")
        if got.get("pack"):
            said.append("offline pack: " + got["pack"])
        if got.get("dams"):
            said.append("Dams folder: " + got["dams"])
            if not self.dams_edit.text().strip():
                self.dams_edit.setText(got["dams"])
                self._save()
        self.refresh()
        if said:
            self.ready_label.setText(self.ready_label.text() + "  (" + "; ".join(said) + ")")
        return got

    # ---------------------------------------------------------------- refresh --
    def refresh(self, *args):
        self._refresh_checks()
        self._refresh_dams()
        return True

    def _refresh_checks(self):
        folder = self.folder_edit.text().strip() or SD.data_folder()
        try:
            self.items = SC.check(folder)
        except Exception as e:                                   # noqa
            self.items = []
            self.ready_label.setText("The set-up could not be checked: {}".format(e))
        try:
            self.steps = SC.step_rows(self.items)
        except Exception:                                        # noqa
            self.steps = []
        if self.items:
            self.ready_label.setText(SC.summary_text(self.items))
        for s in self.steps:
            w = self.step_widgets.get(s["key"])
            if not w:
                continue
            self._set_light(w["light"], s["level"])
            w["note"].setText(s["blocked_by"] or "")
            tip = s["blocked_by"] or "Ready. Opens {}.".format(s["plugin_label"])
            w["button"].setToolTip(tip)
        self._fill_check_rows()

    def _set_light(self, label, level):
        dot, colour, word = MARKS.get(level, MARKS["info"])
        label.setText("{} {}".format(dot, word))
        try:
            label.setStyleSheet("color: {};".format(colour))
        except Exception:                                        # noqa
            pass

    def _fill_check_rows(self):
        """One row per check: light, name, what is true, and an Open folder button."""
        for i, item in enumerate(self.items):
            row = self.check_rows.get(item["key"])
            if row is None:
                light, name = QLabel(""), QLabel("")
                text = QLabel("")
                text.setWordWrap(True)
                btn = QPushButton("Open folder")
                btn.setVisible(False)
                row = {"light": light, "name": name, "text": text, "button": btn, "folder": ""}
                btn.clicked.connect(lambda _c=False, r=row: self._open_path(r["folder"]))
                self.checks_grid.addWidget(light, i, 0)
                self.checks_grid.addWidget(name, i, 1)
                self.checks_grid.addWidget(text, i, 2)
                self.checks_grid.addWidget(btn, i, 3)
                try:
                    self.checks_grid.setColumnStretch(2, 1)
                except Exception:                                # noqa
                    pass
                self.check_rows[item["key"]] = row
            self._set_light(row["light"], item["level"])
            row["name"].setText(item["label"])
            row["text"].setText(item["text"] + (("  " + item["fix"])
                                                if item["fix"] and item["level"] in
                                                ("warn", "error") else ""))
            tip = [item["text"]]
            if item.get("source"):
                tip.append("From: " + item["source"])
            if item.get("fix"):
                tip.append(item["fix"])
            row["text"].setToolTip("\n".join(tip))
            row["folder"] = item.get("folder") or ""
            row["button"].setVisible(bool(row["folder"]))
            row["button"].setEnabled(bool(row["folder"]) and os.path.isdir(row["folder"]))

    def _refresh_dams(self):
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

    # ------------------------------------------------------------- check text --
    def check_text(self):
        return SC.text_report(self.items, self.folder_edit.text().strip() or SD.data_folder(),
                              self.steps)

    def show_check_text(self):
        """The whole check as text in a window the user can select and copy."""
        text = self.check_text()
        dlg = QDialog(self)
        dlg.setWindowTitle("Check my setup")
        dlg.setMinimumWidth(700)
        lay = QVBoxLayout()
        box = QTextEdit()
        box.setPlainText(text)
        box.setReadOnly(True)
        try:
            box.setLineWrapMode(QTextEdit.NoWrap)
        except Exception:                                        # noqa
            pass
        lay.addWidget(box, 1)
        row = QHBoxLayout()
        copy = QPushButton("Copy")
        copy.clicked.connect(lambda _c=False: self._copy(text))
        close = QPushButton("Close")
        close.clicked.connect(lambda _c=False: dlg.accept())
        row.addStretch(1)
        row.addWidget(copy)
        row.addWidget(close)
        lay.addLayout(row)
        dlg.setLayout(lay)
        dlg.exec_() if hasattr(dlg, "exec_") else dlg.exec()
        return text

    def _copy(self, text):
        try:
            from qgis.PyQt.QtWidgets import QApplication
            QApplication.clipboard().setText(text)
        except Exception:                                        # noqa
            pass

    # ------------------------------------------------------------------ open --
    def _open_path(self, path):
        if path and os.path.isdir(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
            return True
        return False

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
        plug, started = load_plugin_object(PLUGIN_FOLDER.get(key, key))
        if started:
            self._note_started(key)
        return plug

    def _note_started(self, key):
        """Dam suite release with ND Dam Hazard 2.28.0: the window's status line (the Installed
        line) says that the plugin was installed but not loaded and has now been started."""
        try:
            text = self.plugins_label.text()
            self.plugins_label.setText((text + " " if text else "") +
                                       "{} was installed but not loaded: it has been started."
                                       .format(VS.LABELS.get(key, key)))
        except Exception:                                        # noqa
            pass

    def open_plugin(self, key, row=None):
        """Open plugin `key`'s window, pointed at the dam of `row` when it has a field for it.
        Returns True when the window opened."""
        folder = getattr(VS, "ALL", VS.PLUGINS).get(key, key)
        plug, started = load_plugin_object(folder)
        if started:
            self._note_started(key)
        if plug is None or not hasattr(plug, "run_plugin"):
            QMessageBox.information(self, TITLE, "{} is not installed in this QGIS, or it could not "
                                    "be started.".format(VS.LABELS.get(key, key)))
            return False
        try:
            plug.run_plugin()
        except Exception as e:                                   # noqa
            QMessageBox.information(self, TITLE, "{} could not be opened ({}).".format(VS.LABELS.get(key, key), e))
            return False
        if row:
            point_window_at(getattr(plug, "dialog", None), key, self.dams_edit.text().strip(), row)
        return True


def load_plugin_object(name):
    """(plugin object or None, started) for the plugin whose folder is `name`.

    Dam suite release with ND Dam Hazard 2.28.0: a plugin that is installed but not loaded
    (unchecked in the Plugin Manager, or not started yet in this QGIS session) is loaded and
    started first - qgis.utils.loadPlugin(name) then qgis.utils.startPlugin(name), both of which
    return a bool - and looked up again; `started` is True when that gave the plugin object.
    Never raises: (None, False) when the plugin is not installed or will not start."""
    try:
        from qgis import utils as qgis_utils
    except Exception:                                            # noqa
        return None, False
    try:
        plug = qgis_utils.plugins.get(name)
    except Exception:                                            # noqa
        plug = None
    if plug is not None:
        return plug, False
    loaded = False
    try:
        loaded = bool(qgis_utils.loadPlugin(name))
    except Exception:                                            # noqa
        loaded = False
    if loaded:
        try:
            qgis_utils.startPlugin(name)
        except Exception:                                        # noqa
            pass
        try:                                     # stays enabled at the next QGIS start
            from qgis.core import QgsSettings
            QgsSettings().setValue("PythonPlugins/" + name, True)
        except Exception:                                        # noqa
            pass
    try:
        plug = qgis_utils.plugins.get(name)
    except Exception:                                            # noqa
        plug = None
    return plug, plug is not None


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
        # 2.30.1: QGIS parents a plugin submenu to the main window, not to Plugins, so
        # findChildren on the Plugins menu found nothing and every plugin added its own copy of
        # "Dam suite home", "Suite settings" and "Suite data and servers" (three of each). The
        # submenus are the Plugins menu's own actions' menus.
        plugin_menu = iface.pluginMenu()
        menus = [a.menu() for a in plugin_menu.actions() if a.menu() is not None]
        menus += [m for m in plugin_menu.findChildren(QMenu) if m not in menus]
        for menu in menus:
            if menu.title().replace("&", "") == SUITE_MENU:
                for a in menu.actions():
                    if a.objectName():
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
