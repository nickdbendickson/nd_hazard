# -*- coding: utf-8 -*-
"""Shared window widgets of the ND dam plugins (Runner, Screening, Hazard).

Canonical copy: shared/ui/ui_widgets.py. Each plugin carries a byte-identical copy (the plugins
are installed separately, so none imports another's); edit the canonical file, run
tools/sync_ui.py and raise SHARED_VERSION. tools/ci_checks.py fails the build on a stale copy.
Colours come from tokens.py (also synced); docs/gui-grammar.md says where each widget is used.

    WrapLabel     a word-wrapped label that is never cut off
    InfoBanner    the muted disclaimer / context banner
    Collapsible   a ▸/▾ disclosure section (title kept apart from the arrow)
    StageStrip    stage chips ✓ yes / ◐ part / ○ no / ▶ active / ✕ error, optionally clickable
    EngineChip    a borderless "LISFLOOD-FP ✓" status label (not a button)
    ToolRow       one tool: a left-aligned button, a status tag and a grey description
    DamCard       a dam's title, path, warnings, status chips and lines
    CheckPanel    check results ranked by severity (errors block the primary button)

Only qgis.PyQt is imported, so the same file works under QGIS on Qt5 and Qt6.
"""
import html

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
                                 QToolButton, QVBoxLayout, QWidget)

from .tokens import TOKENS as T

SHARED_VERSION = "1.3.0"

YES, PART, NO, ACTIVE, ERR = "yes", "part", "no", "active", "error"
MARKS = {YES: "\u2713", PART: "\u25d0", NO: "\u25cb", ACTIVE: "\u25b6", ERR: "\u2715"}
COLORS = {YES: T["success"], PART: T["warn"], NO: T["off"], ACTIVE: T["link"], ERR: T["error"]}
TAG_TEXT = {YES: "\u2713 done", PART: "\u25d0 partial", NO: "not run yet"}

# the check levels (the same strings as the Hazard plugin's checks.py)
ERROR, WARNING, OK = "ERROR", "WARNING", "OK"

BANNER_QSS = ("QFrame#checkErrorBanner {background:%s; border:1px solid %s;"
              " border-radius:4px;} QFrame#checkErrorBanner QLabel {color:white;}"
              % (T["error_fill"], T["error_edge"]))
WARN_PILL_QSS = ("QLabel {background:%s; color:%s; font-weight:bold;"
                 " font-size:10px; border-radius:3px; padding:1px 5px;}"
                 % (T["warn_fill"], T["warn_ink"]))
WARN_BOX_QSS = ("QLabel {color:%s; background:%s; border:1px solid %s;"
                " border-radius:4px; padding:4px;}" % (T["warn"], T["warn_bg"], T["warn_edge"]))
MUTED_QSS = "color:%s;" % T["muted"]


def ellipsize(root):
    """Give every button under `root` whose text ends in three dots the one ellipsis character the
    grammar asks for (docs/gui-grammar.md, Buttons). Call it once the window is built; a button
    made later gets its text right at the source."""
    from qgis.PyQt.QtWidgets import QAbstractButton
    for b in root.findChildren(QAbstractButton):
        t = b.text()
        if t.endswith("..."):
            b.setText(t[:-3] + "\u2026")


def _plural(n, word):
    return "{} {}{}".format(n, word, "" if n == 1 else "s")


def _flag(name):
    """Qt.<name> on Qt5 and on Qt6 (where the flags are scoped)."""
    for scope in ("", "TextInteractionFlag.", "ToolButtonStyle.", "AlignmentFlag.", "TextFormat."):
        obj = Qt
        try:
            for part in (scope + name).split("."):
                obj = getattr(obj, part)
            return obj
        except AttributeError:
            continue
    return None


class WrapLabel(QLabel):
    """A word-wrapped label whose minimum height follows its width, so a layout never cuts its
    last lines off (a plain wrapped QLabel can be laid out at its one-line height)."""

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setWordWrap(True)

    def _fit(self):
        try:
            h = self.heightForWidth(self.width())
            if h > 0 and h != self.minimumHeight():
                self.setMinimumHeight(h)
        except Exception:                                        # noqa
            pass

    def setText(self, text):
        super().setText(text)
        self._fit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()


class InfoBanner(QFrame):
    """The disclaimer, muted so it reads as context, not instructions."""

    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.setObjectName("info_banner")
        self.setStyleSheet("QFrame#info_banner {background:%s; border:1px solid %s;"
                           "border-radius:4px;} QLabel {color:%s;}"
                           % (T["subtle"], T["frame"], T["muted"]))
        h = QHBoxLayout(self)
        h.setContentsMargins(10, 6, 10, 6)
        h.addWidget(QLabel("\u2139"))
        self.label = QLabel(text)
        self.label.setWordWrap(True)
        h.addWidget(self.label, 1)


class Collapsible(QWidget):
    """A ▸/▾ header button that shows or hides one body widget. The text is rebuilt from the
    stored title, so toggling or retitling never drifts."""

    def __init__(self, title, body, expanded=False, parent=None):
        super().__init__(parent)
        self._title = title
        self.header = QToolButton(self)
        style = _flag("ToolButtonTextOnly")
        if style is not None:
            self.header.setToolButtonStyle(style)
        self.header.setAutoRaise(True)
        self.header.setCheckable(True)
        self.header.setStyleSheet("QToolButton {border:none; font-weight:bold; text-align:left;}")
        self.body = body
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addWidget(self.header)
        lay.addWidget(body)
        self.header.toggled.connect(self._apply)
        self.header.setChecked(bool(expanded))
        self._apply(bool(expanded))

    def inner(self):
        return self.body

    def title(self):
        return self._title

    def set_title(self, title):
        self._title = title
        self._apply(self.is_expanded())

    def is_expanded(self):
        return bool(self.header.isChecked())

    def set_expanded(self, on):
        self.header.setChecked(bool(on))
        self._apply(bool(on))

    def _apply(self, expanded):
        self.header.setText(("\u25be " if expanded else "\u25b8 ") + self._title)
        self.body.setVisible(bool(expanded))


class StageStrip(QWidget):
    """Stage chips with an arrow between them. clickable=True makes each chip a flat button
    that emits clicked(index)."""

    YES, PART, NO, ACTIVE, ERR = YES, PART, NO, ACTIVE, ERR
    clicked = pyqtSignal(int)

    def __init__(self, stages, clickable=False, parent=None):
        super().__init__(parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        self.names = list(stages)
        self.clickable = bool(clickable)
        self.chips = []
        for i, name in enumerate(self.names):
            if i:
                arrow = QLabel("\u2192")
                arrow.setStyleSheet("color:%s;" % T["off"])
                h.addWidget(arrow)
            if self.clickable:
                chip = QToolButton()
                chip.setAutoRaise(True)
                chip.clicked.connect(lambda _c=False, n=i: self.clicked.emit(n))
            else:
                chip = QLabel(name)
            self.chips.append(chip)
            h.addWidget(chip)
        h.addStretch(1)
        self.states = []
        self.set_states([NO] * len(self.names))

    def set_states(self, states, tips=None):
        self.states = [st if st in MARKS else NO for st in states]
        kind = "QToolButton" if self.clickable else "QLabel"
        for i, (chip, st) in enumerate(zip(self.chips, self.states)):
            chip.setText("{} {}".format(MARKS[st], self.names[i]))
            chip.setStyleSheet("%s {color:%s; padding:2px 8px; border:1px solid %s; "
                               "border-radius:9px;%s}" % (kind, COLORS[st], COLORS[st],
                                                         " font-weight:bold;" if st == ACTIVE else ""))
            if tips and i < len(tips):
                chip.setToolTip(tips[i])


class EngineChip(QLabel):
    """'✓ LISFLOOD-FP' - a status label, borderless by default so it does not look clickable.
    The mark leads, like every mark in the suite. A missing optional engine is amber, not red:
    red is for what blocks the run."""

    def __init__(self, name, bordered=False, parent=None):
        super().__init__(parent)
        self.name = name
        self.bordered = bool(bordered)
        self.found = None
        self.set_state(None, pending=True)

    def set_state(self, found, note="", pending=False, hint="", optional=False):
        """found: True / False / None (could not be checked). optional: the run does not need it."""
        self.found = found
        color = {True: T["success"], False: T["warn"] if optional else T["error"]}.get(
            found, T["off"])
        mark = "\u2026" if pending else {True: "\u2713", False: "\u2715"}.get(found, "?")
        self.setText("{} {}".format(mark, self.name))
        border = "border:1px solid %s; border-radius:4px; padding:2px 8px;" % color \
            if self.bordered else "border:none; padding:2px 4px;"
        self.setStyleSheet("QLabel {color:%s; %s}" % (color, border))
        if not pending:
            self.setToolTip("{}: {}{}{}".format(
                self.name, {True: "installed", False: "not installed"}.get(found, "could not be checked"),
                " - " + note if note else "", "\n" + hint if hint else ""))


class ToolRow(QWidget):
    """One tool: a left-aligned button (fixed width, so the tags line up), a status tag and the
    description in grey."""

    def __init__(self, label, tip, tooltip=None, button_width=360, tag_width=90, parent=None):
        super().__init__(parent)
        self.button = QPushButton(label.replace("...", "\u2026"))
        self.button.setStyleSheet("QPushButton {text-align:left; padding:3px 10px;}")
        self.button.setFixedWidth(button_width)
        self.button.setMinimumHeight(26)
        if tooltip is not None:
            self.button.setToolTip(tooltip)
        self.tag = QLabel("")
        self.tag.setFixedWidth(tag_width)
        self.desc = QLabel(tip)
        self.desc.setWordWrap(True)
        self.desc.setStyleSheet(MUTED_QSS)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addWidget(self.button)
        row.addWidget(self.tag)
        row.addWidget(self.desc, 1)

    def set_status(self, state, show=True):
        self.tag.setText(TAG_TEXT.get(state, "") if show else "")
        self.tag.setStyleSheet("color:%s;" % COLORS.get(state, COLORS[NO]))


class DamCard(QFrame):
    """A dam: bold title, the folder path, the amber warnings (above the chips, so they are seen
    without scrolling), the status chips and the status / record / suggestion lines.
    chip_rows = ((status row key, chip text), ...); per_row chips on each line."""

    def __init__(self, chip_rows=(), per_row=4, parent=None):
        super().__init__(parent)
        self.setObjectName("dam_card")
        self.setStyleSheet("QFrame#dam_card {border:1px solid %s; border-radius:4px;"
                           " background:%s;}" % (T["frame"], T["card"]))
        self.chip_rows = tuple(chip_rows)
        self.layout_ = QVBoxLayout(self)
        self.title_label = QLabel("")
        fmt = _flag("RichText")
        if fmt is not None:
            self.title_label.setTextFormat(fmt)
        self.path_label = QLabel("")
        self.path_label.setStyleSheet(MUTED_QSS)
        sel = _flag("TextSelectableByMouse")
        if sel is not None:
            self.path_label.setTextInteractionFlags(sel)
        self.warn_label = WrapLabel("")      # never cut off (no elided last line)
        self.warn_label.setStyleSheet(WARN_BOX_QSS)
        self.warn_label.setVisible(False)
        grid = QGridLayout()
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(4)
        grid.setContentsMargins(0, 0, 0, 0)
        self.chips = {}
        for n, (key, text) in enumerate(self.chip_rows):
            lab = QLabel(text)
            self.chips[key] = lab
            grid.addWidget(lab, n // per_row, n % per_row)
        grid.setColumnStretch(per_row, 1)
        self.chips_row = QWidget()
        self.chips_row.setLayout(grid)
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.record_label = QLabel("")
        self.record_label.setWordWrap(True)
        self.record_label.setStyleSheet(MUTED_QSS)
        self.suggest_label = QLabel("")
        self.suggest_label.setWordWrap(True)
        self.suggest_label.setStyleSheet("font-weight:bold;")
        for w in (self.title_label, self.path_label, self.warn_label, self.chips_row,
                  self.status_label, self.record_label, self.suggest_label):
            self.layout_.addWidget(w)

    def add_section(self, widget):
        self.layout_.addWidget(widget)

    def set_context(self, title, path, empty="No dam chosen"):
        """Title and path are plain text: they are escaped here, so no caller can forget."""
        self.title_label.setText("<b>{}</b>".format(html.escape(title)) if title else
                                 "<span style='color:{}'>{}</span>".format(T["off"], html.escape(empty)))
        self.path_label.setText(path or "")

    def set_warnings(self, warnings):
        warnings = list(warnings or [])
        self.warn_label.setText("\n".join("\u26a0 " + w for w in warnings))
        self.warn_label.setVisible(bool(warnings))

    def set_rows(self, rows):
        """Chips from status rows [(key, label, 'yes'/'part'/'no', detail)]."""
        st = {k: (v, d) for k, _l, v, d in rows}
        labels = {k: l for k, l, _v, _d in rows}
        for key, text in self.chip_rows:
            v, d = st.get(key, (NO, ""))
            v = v if v in (YES, PART, NO) else NO
            chip = self.chips[key]
            chip.setText("{} {}".format(MARKS[v], text))
            chip.setStyleSheet("QLabel {color:%s; padding:1px 6px; border:1px solid %s;"
                               "border-radius:8px;}" % (COLORS[v], COLORS[v]))
            chip.setToolTip("{}: {}{}".format(labels.get(key, text), v, " - " + d if d else ""))


class CheckPanel(QWidget):
    """Renders [(level, text)] check results by severity:
    ERROR   -> red banner "N error(s) must be fixed before <action>", the primary button is
               disabled (tooltip says why), the check button reads "<text> ⚠ N".
    WARNING -> amber WARN rows in an open collapsible box; the run is not blocked, "<text> ◐ N".
    OK      -> one collapsed "N checks passed" line.
    action is the word after "before" ("classifying", "screening", "running", ...)."""

    errors_fixed = pyqtSignal(bool)     # True = no ERROR in the latest results

    def __init__(self, check_button, primary_button, action="classifying", parent=None):
        super().__init__(parent)
        self.check_button = check_button
        self.primary_button = primary_button
        self.action = action
        self._base_check_text = check_button.text()
        self._base_primary_tip = primary_button.toolTip()
        self._parts = []
        self.n_errors = self.n_warnings = self.n_ok = 0
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)
        self.setVisible(False)

    @property
    def blocked(self):
        return self.n_errors > 0

    def clear(self):
        """Back to the state before any check (e.g. an input changed)."""
        had_errors = self.n_errors > 0
        self._reset()
        self.setVisible(False)
        if had_errors:
            self.errors_fixed.emit(True)

    def _reset(self):
        for w in self._parts:
            self._layout.removeWidget(w)
            w.setParent(None)
            w.deleteLater()
        self._parts = []
        self.n_errors = self.n_warnings = self.n_ok = 0
        self.check_button.setText(self._base_check_text)
        self.primary_button.setEnabled(True)
        self.primary_button.setToolTip(self._base_primary_tip)

    def set_results(self, results):
        self._reset()
        results = list(results or [])
        errors = [t for lvl, t in results if lvl == ERROR]
        warnings = [t for lvl, t in results if lvl == WARNING]
        oks = [t for lvl, t in results if lvl == OK]
        self.n_errors, self.n_warnings, self.n_ok = len(errors), len(warnings), len(oks)
        if errors:
            self._add_error_banner(errors)
            self.check_button.setText("{} \u26a0 {}".format(self._base_check_text, len(errors)))
            self.primary_button.setEnabled(False)
            self.primary_button.setToolTip("Blocked: {} - see the red box".format(
                _plural(len(errors), "input error")))
        elif warnings:
            self.check_button.setText("{} \u25d0 {}".format(self._base_check_text, len(warnings)))
            self.primary_button.setToolTip("{}\n{} - the run proceeds, check them".format(
                self._base_primary_tip, _plural(len(warnings), "warning")))
        if warnings:
            self._add_warning_rows(warnings)
        if oks:
            self._add_ok_summary(len(oks))
        self.setVisible(bool(results))
        self.errors_fixed.emit(not errors)

    def _add_error_banner(self, errors):
        banner = QFrame(self)
        banner.setObjectName("checkErrorBanner")
        banner.setStyleSheet(BANNER_QSS)
        v = QVBoxLayout(banner)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(3)
        head = QLabel("{} must be fixed before {}".format(
            _plural(len(errors), "error"), self.action))
        head.setStyleSheet("font-weight:bold;")
        head.setWordWrap(True)
        v.addWidget(head)
        for t in errors:
            lbl = QLabel("\u2022 " + t)
            lbl.setWordWrap(True)
            v.addWidget(lbl)
        self._layout.addWidget(banner)
        self._parts.append(banner)

    def _add_warning_rows(self, warnings):
        inner = QWidget()
        v = QVBoxLayout(inner)
        v.setContentsMargins(14, 0, 0, 0)
        v.setSpacing(4)
        top = _flag("AlignTop")
        for t in warnings:
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(6)
            pill = QLabel("WARN")
            pill.setStyleSheet(WARN_PILL_QSS)
            lbl = QLabel(t)
            lbl.setWordWrap(True)
            if top is not None:
                h.addWidget(pill, 0, top)
            else:
                h.addWidget(pill)
            h.addWidget(lbl, 1)
            v.addWidget(row)
        box = Collapsible("{} \u2014 the run proceeds, check them".format(
            _plural(len(warnings), "warning")), inner, expanded=True, parent=self)
        box.header.setStyleSheet(box.header.styleSheet().replace("}", " color:%s;}" % T["warn"]))
        self._layout.addWidget(box)
        self._parts.append(box)

    def _add_ok_summary(self, n):
        inner = QLabel("All passed \u2014 the log lists the values.")
        inner.setStyleSheet(MUTED_QSS)
        box = Collapsible(_plural(n, "check") + " passed", inner, expanded=False, parent=self)
        box.header.setStyleSheet(box.header.styleSheet().replace("}", " color:%s;}" % T["success"]))
        self._layout.addWidget(box)
        self._parts.append(box)
