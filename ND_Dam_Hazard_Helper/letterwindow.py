# -*- coding: utf-8 -*-
"""2.30.0: the window that shows the dam-owner letter and the county notification as drafts,
lets the engineer edit them, and exports .docx or .md (letters.py writes both).

One small window instead of a tab: the drafts are read once per run, edited and exported, and
the engineer does not want a permanent tab for them. What the window adds to letters.py is the
editing - nothing is exported that the engineer has not seen - and the list of the
[placeholders] still in the text, so an unfinished draft cannot be mistaken for a letter.
Every callback is guarded: a window of this plugin never lets an exception reach Qt."""

import os

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTextEdit, QComboBox, QMessageBox,
    QFileDialog,
)

from .constants import PLUGIN_NAME, PLUGIN_VERSION, ND_POLICY_REF
from .helpers import log_message
from . import letters as LT

TITLE = "Dam owner letter and county notification (drafts)"


class LetterWindow(QDialog):
    """`res`: the last run's result dict; `folder`: where the drafts are written by default."""

    def __init__(self, parent, res, folder, dam_name="", nid_id="", history=()):
        super().__init__(parent)
        self.setWindowTitle("{} - {}".format(PLUGIN_NAME, TITLE))
        self.setMinimumSize(820, 640)
        self.folder = folder or ""
        self.facts = LT.facts_from(res, card=(res or {}).get("card"), history=history,
                                   lifelines=self._lifelines(res), dam_name=dam_name,
                                   nid_id=nid_id, policy_ref=ND_POLICY_REF,
                                   plugin=PLUGIN_NAME, version=PLUGIN_VERSION)
        self._text = {}
        lay = QVBoxLayout(self)
        info = QLabel(
            "Drafts built from the last classification run. <b>They are drafts</b>: every "
            "[square bracket] is yours to complete, and nothing here states a duty, a notice "
            "or a deadline under {} - a plugin cannot know what a letter has to say to be "
            "one. Edit the text below, then export it.".format(ND_POLICY_REF))
        info.setWordWrap(True)
        lay.addWidget(info)
        row = QHBoxLayout()
        self.which = QComboBox()
        for key, label, _fn in LT.DRAFTS:
            self.which.addItem(label, key)
        self.which.currentIndexChanged.connect(self._switch)
        row.addWidget(self.which, stretch=2)
        self.reset_btn = QPushButton("Reset this draft")
        self.reset_btn.setToolTip("Build this draft again from the run - your edits to it are "
                                  "lost")
        self.reset_btn.clicked.connect(self._reset)
        row.addWidget(self.reset_btn)
        lay.addLayout(row)
        self.editor = QTextEdit()
        self.editor.setStyleSheet("font-family: 'Menlo', 'Courier New', monospace; "
                                  "font-size: 12px;")
        lay.addWidget(self.editor)
        self.todo = QLabel("")
        self.todo.setWordWrap(True)
        lay.addWidget(self.todo)
        row2 = QHBoxLayout()
        self.docx_btn = QPushButton("Save as .docx...")
        self.docx_btn.clicked.connect(lambda *a: self._save("docx"))
        row2.addWidget(self.docx_btn)
        self.md_btn = QPushButton("Save as .md...")
        self.md_btn.clicked.connect(lambda *a: self._save("md"))
        row2.addWidget(self.md_btn)
        self.both_btn = QPushButton("Save both drafts (.docx) to the outputs folder")
        self.both_btn.clicked.connect(self._save_both)
        row2.addWidget(self.both_btn)
        row2.addStretch()
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.accept)
        row2.addWidget(self.close_btn)
        lay.addLayout(row2)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        self._current = None
        self._switch()

    # ------------------------------------------------------------------ data --
    @staticmethod
    def _lifelines(res):
        """The lifeline detail rows of the run (letters.py only reads their text)."""
        gov = ((res or {}).get("results") or {}).get((res or {}).get("governing")) or {}
        rows = gov.get("lifeline_detail") or []
        try:
            from . import lifelineroutes as LR
            return [{"line": LR.row_line(r)} for r in rows]
        except Exception:                                        # noqa - optional detail
            return []

    def _key(self):
        return self.which.currentData() or "owner"

    def _build(self, key):
        try:
            _title, paras = LT.draft(key, self.facts)
            return LT.to_text(paras)
        except Exception as e:                                   # noqa - never raise in a slot
            log_message("Letter draft not built: {}".format(e))
            return "The draft could not be built ({}).".format(e)

    # --------------------------------------------------------------- actions --
    def _remember(self):
        if self._current:
            self._text[self._current] = self.editor.toPlainText()

    def _switch(self, *args):
        try:
            self._remember()
            key = self._key()
            self._current = key
            self.editor.setPlainText(self._text.get(key) or self._build(key))
            self._show_todo()
        except Exception as e:                                   # noqa
            log_message("Letter window: {}".format(e))

    def _reset(self, *args):
        try:
            key = self._key()
            self._text.pop(key, None)
            self._current = key
            self.editor.setPlainText(self._build(key))
            self._show_todo()
        except Exception as e:                                   # noqa
            log_message("Letter window: {}".format(e))

    def _show_todo(self):
        try:
            paras = LT.from_text(self.editor.toPlainText())
            left = LT.placeholders(paras)
            self.todo.setText("Still to complete: {}".format(", ".join(left[:12]) + (
                " and {} more".format(len(left) - 12) if len(left) > 12 else ""))
                if left else "No [placeholder] is left in this draft.")
        except Exception:                                        # noqa
            self.todo.setText("")

    def _paras(self):
        return LT.from_text(self.editor.toPlainText())

    def _title(self):
        return LT.OWNER_TITLE if self._key() == "owner" else LT.COUNTY_TITLE

    def _save(self, fmt):
        try:
            self._remember()
            self._show_todo()
            base = "Dam_Owner_Letter_Draft" if self._key() == "owner" \
                else "County_Notification_Draft"
            start = os.path.join(self.folder or "", base + "." + fmt)
            path, _f = QFileDialog.getSaveFileName(
                self, "Save the draft", start,
                "Word document (*.docx)" if fmt == "docx" else "Markdown (*.md)")
            if not path:
                return
            if fmt == "docx":
                LT.write_docx(path, self._title(), self._paras())
            else:
                LT.write_md(path, self._title(), self._paras())
            self.status.setText("Draft saved: {} (a DRAFT - complete and sign it)".format(path))
        except Exception as e:                                   # noqa - never raise in a slot
            log_message("Letter draft not saved: {}".format(e))
            QMessageBox.warning(self, TITLE, "The draft could not be saved: {}".format(e))

    def _save_both(self, *args):
        try:
            self._remember()
            folder = self.folder or ""
            if not folder or not os.path.isdir(folder):
                QMessageBox.warning(self, TITLE, "There is no outputs folder to write to - "
                                                 "use 'Save as ...' instead.")
                return
            done = []
            for key, title, _fn in LT.DRAFTS:
                paras = (LT.from_text(self._text[key]) if key in self._text
                         else LT.draft(key, self.facts)[1])
                base = "Dam_Owner_Letter_Draft" if key == "owner" \
                    else "County_Notification_Draft"
                done.append(LT.write_docx(os.path.join(folder, base + ".docx"), title, paras))
            self.status.setText("Drafts saved: {}".format(", ".join(
                os.path.basename(p) for p in done)))
        except Exception as e:                                   # noqa
            log_message("Letter drafts not saved: {}".format(e))
            QMessageBox.warning(self, TITLE, "The drafts could not be saved: {}".format(e))


def open_letters(parent, res, folder, dam_name="", nid_id="", history=()):
    """Show the window (modal to the plugin's window). Returns the window, or None when it
    could not be built - the caller is a UI callback and must not see an exception."""
    try:
        win = LetterWindow(parent, res, folder, dam_name, nid_id, history)
        win.exec() if hasattr(win, "exec") else win.exec_()
        return win
    except Exception as e:                                       # noqa
        log_message("Letter window not opened: {}".format(e))
        try:
            QMessageBox.warning(parent, TITLE, "The drafts could not be opened: {}".format(e))
        except Exception:                                        # noqa
            pass
        return None
