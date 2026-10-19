# -*- coding: utf-8 -*-
"""Severity-ranked view of checks.check_all() results.

ERROR   -> red banner "N error(s) must be fixed before classifying", the
           primary button is disabled (tooltip says why), "Check inputs ⚠ N".
WARNING -> amber WARN rows in a collapsible box; the run is not blocked,
           "Check inputs ◐ N".
OK      -> one collapsed "N checks passed" line.
The panel only shows the list; the dialog still logs every result.

Ported from DamSafety_Hazard/ui_check_panel.py with two fixes:
 * the collapsible-header colour is composed as a full stylesheet string, not
   patched into the base string with a fragile ``.replace("}", ...)`` (which
   depends on the exact base text and rewrites every ``}``);
 * the unused ``errors_fixed`` signal is dropped - the panel drives the primary
   button's enabled state directly in set_results()/_reset().
Enum members are resolved scoped-first (PyQt6) then legacy (PyQt5), the way
compat.py resolves enums, so the panel works on QGIS 3.28 / Qt5 and Qt6.
"""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QFrame, QLabel, QToolButton, QVBoxLayout, QHBoxLayout, QWidget

from .checks import ERROR, WARNING, OK

BANNER_QSS = ("QFrame#checkErrorBanner {background:#b3261e; border:1px solid #8c1d18;"
              " border-radius:4px;} QFrame#checkErrorBanner QLabel {color:white;}")
WARN_PILL_QSS = ("QLabel {background:#f9a825; color:#3e2723; font-weight:bold;"
                 " font-size:10px; border-radius:3px; padding:1px 5px;}")
# the collapsible header: one base style plus the two coloured variants, each a
# complete stylesheet string (the fragile ``.replace("}", ...)`` of the original
# rewrote every ``}`` in whatever string was there)
HEADER_QSS = "QToolButton {border:none; font-weight:bold; text-align:left;}"
WARN_HEADER_QSS = "QToolButton {border:none; font-weight:bold; text-align:left; color:#a15c00;}"
OK_HEADER_QSS = "QToolButton {border:none; font-weight:bold; text-align:left; color:#2e7d32;}"


def _qt_enum(group, name):
    """A Qt enum member resolved scoped-first (PyQt6 requires the scoped
    spelling; recent PyQt5 has it too), legacy Qt5 spelling second - the way
    compat.py resolves enums (compat.py has no shim for these two)."""
    for get in (lambda: getattr(getattr(Qt, group), name), lambda: getattr(Qt, name)):
        try:
            v = get()
        except Exception:                                        # noqa
            continue
        if v is not None:
            return v
    return None


TOOLBTN_TEXT_ONLY = _qt_enum("ToolButtonStyle", "ToolButtonTextOnly")
ALIGN_TOP = _qt_enum("AlignmentFlag", "AlignTop")


def _plural(n, word):
    return "{} {}{}".format(n, word, "" if n == 1 else "s")


class Collapsible(QWidget):
    """A ▸/▾ header button that shows or hides one body widget."""

    def __init__(self, title, body, expanded=False, parent=None):
        super().__init__(parent)
        self._title = title
        self.header = QToolButton(self)
        self.header.setToolButtonStyle(TOOLBTN_TEXT_ONLY)
        self.header.setAutoRaise(True)
        self.header.setCheckable(True)
        self.header.setStyleSheet(HEADER_QSS)
        self.body = body
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addWidget(self.header)
        lay.addWidget(body)
        self.header.toggled.connect(self._apply)
        self.header.setChecked(expanded)
        self._apply(expanded)

    def title(self):
        return self._title

    def is_expanded(self):
        return self.header.isChecked()

    def _apply(self, expanded):
        # the text is rebuilt from the stored title, so toggling never drifts
        self.header.setText(("▾ " if expanded else "▸ ") + self._title)
        self.body.setVisible(expanded)


class CheckPanel(QWidget):
    """Renders [(level, text)] from checks.check_all() by severity."""

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
        self._reset()
        self.setVisible(False)

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
            self.check_button.setText("{} ⚠ {}".format(self._base_check_text, len(errors)))
            self.primary_button.setEnabled(False)
            self.primary_button.setToolTip("Blocked: {} - see the red box".format(
                _plural(len(errors), "input error")))
        elif warnings:
            self.check_button.setText("{} ◐ {}".format(self._base_check_text, len(warnings)))
            self.primary_button.setToolTip("{}\n{} - the run proceeds, check them".format(
                self._base_primary_tip, _plural(len(warnings), "warning")))
        if warnings:
            self._add_warning_rows(warnings)
        if oks:
            self._add_ok_summary(len(oks))
        self.setVisible(bool(results))

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
            lbl = QLabel("• " + t)
            lbl.setWordWrap(True)
            v.addWidget(lbl)
        self._layout.addWidget(banner)
        self._parts.append(banner)

    def _add_warning_rows(self, warnings):
        inner = QWidget()
        v = QVBoxLayout(inner)
        v.setContentsMargins(14, 0, 0, 0)
        v.setSpacing(4)
        for t in warnings:
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(6)
            pill = QLabel("WARN")
            pill.setStyleSheet(WARN_PILL_QSS)
            lbl = QLabel(t)
            lbl.setWordWrap(True)
            h.addWidget(pill, 0, ALIGN_TOP)
            h.addWidget(lbl, 1)
            v.addWidget(row)
        box = Collapsible("{} — the run proceeds, check them".format(
            _plural(len(warnings), "warning")), inner, expanded=True, parent=self)
        box.header.setStyleSheet(WARN_HEADER_QSS)
        self._layout.addWidget(box)
        self._parts.append(box)

    def _add_ok_summary(self, n):
        inner = QLabel("All passed — the log lists the values.")
        inner.setStyleSheet("color:#777;")
        box = Collapsible(_plural(n, "check") + " passed", inner, expanded=False, parent=self)
        box.header.setStyleSheet(OK_HEADER_QSS)
        self._layout.addWidget(box)
        self._parts.append(box)
