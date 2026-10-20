#!/usr/bin/env python3
"""GUI lint: open every ND dam plugin window with the real Qt of QGIS (offscreen) and check it
against docs/gui-grammar.md. Every rule here is a bug the 2026-10-20 consistency check found,
so it cannot come back.

    QT_QPA_PLATFORM=offscreen <QGIS python> tools/gui_lint.py [shots_dir]
    xvfb-run -s "-screen 0 1920x1080x24" env QT_QPA_PLATFORM=xcb <QGIS python> tools/gui_lint.py

The sideways-scroll rule needs a 1080p screen, so it runs only under the second form.

Exit 1 on any failure (one FAIL line each). With a folder argument it also saves a screenshot
of every window. Opens no Processing dialog and runs nothing. The QGIS-free half of the rules
(shared/ui drift, colour literals in window code) is in tools/ci_checks.py.
"""
import os
import re
import shutil
import sys
import tempfile
import traceback

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools"))
PLUGINS = ("ND_Dam_Hazard_Helper", "nd_breach_runner", "nd_dam_screening", "nd_data_updater")

# the plugins import as packages under their installed names, from a scratch folder
ROOT = tempfile.mkdtemp(prefix="gui_lint_")
for _p in PLUGINS:
    os.symlink(os.path.join(REPO, _p), os.path.join(ROOT, _p))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.pop("NDSUITE_SETTINGS", None)
_HOME = tempfile.mkdtemp(prefix="gui_lint_home_")
os.environ["HOME"] = _HOME                        # no window reads or writes the real profile

from qgis.core import QgsApplication  # noqa: E402
_app = QgsApplication([], True)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", sys.prefix), True)
_app.initQgis()

from qgis.PyQt.QtWidgets import (QAbstractButton, QDialog, QTabBar, QWidget,  # noqa: E402
                                 QWizard)

from ND_Dam_Hazard_Helper.tokens import TOKENS  # noqa: E402

SHOTS = sys.argv[1] if len(sys.argv) > 1 else ""
MAX_MIN_HEIGHT = 1000        # a window must be able to shrink to fit a 1080p screen (taskbar too)
MAX_MIN_WIDTH = 1400
HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?![0-9a-zA-Z])")
TOKEN_HEX = {v.lower() for v in TOKENS.values()}
LONE_AMP = re.compile(r"(?<!&)&(?!&)")
# buttons that open a picker or another window: the label ends with the ellipsis character
OPENERS = ("browse", "choose", "load", "save as", "file", "new")
FAILS = []


def fail(label, msg):
    FAILS.append("FAIL {}: {}".format(label, msg))


def pump(n=6):
    for _ in range(n):
        _app.processEvents()


def text_of(w):
    try:
        return w.text()
    except Exception:                                            # noqa
        return ""


# ------------------------------------------------------------------------- the rules ------------
def rule_title(label, win, top):
    """A top window is named '<plugin name> <version>' (docs/gui-grammar.md): the name as
    metadata.txt gives it, the version with no 'v'. A window a plugin opens from its main one
    (a wizard, the pack window) is '<plugin name> — <Window>'."""
    t = win.windowTitle()
    main = t.startswith("ND ") and re.search(r" \d+\.\d+\.\d+$", t) and " v" not in t
    secondary = t.startswith("ND ") and " — " in t and not re.search(r"\d+\.\d+\.\d+", t)
    if top and not (main or secondary):
        fail(label, "window title {!r} is not '<plugin name> <version>' or '<plugin name> "
             "— <Window>'".format(t))


def rule_tab_amp(label, win):
    for bar in win.findChildren(QTabBar):
        for i in range(bar.count()):
            t = bar.tabText(i)
            if LONE_AMP.search(t):
                fail(label, "tab {!r} has a lone '&' (Qt draws it as an underline; write '&&')"
                     .format(t))


def rule_fits(label, win):
    hint = win.minimumSizeHint()
    h = max(win.minimumHeight(), hint.height() if hint.isValid() else 0)
    w = max(win.minimumWidth(), hint.width() if hint.isValid() else 0)
    if h > MAX_MIN_HEIGHT or w > MAX_MIN_WIDTH:
        fail(label, "cannot shrink below {}x{} (limit {}x{}): its bottom buttons go off a 1080p "
             "screen".format(w, h, MAX_MIN_WIDTH, MAX_MIN_HEIGHT))


def rule_colours(label, win):
    bad = set()
    for w in [win] + win.findChildren(QWidget):
        for hx in HEX_RE.findall(w.styleSheet() or ""):
            if hx.lower() not in TOKEN_HEX:
                bad.add("{} {}".format(type(w).__name__, hx))
    if bad:
        fail(label, "colours not from tokens.py: {}".format(", ".join(sorted(bad)[:6])))


def rule_ellipsis(label, win):
    bad = set()
    for b in win.findChildren(QAbstractButton):
        t = text_of(b).replace("&", "").strip()
        if t.endswith("..."):
            bad.add(t)
        elif t and t.rstrip("…").strip().lower() in OPENERS and not t.endswith("…"):
            bad.add(t)
    if bad:
        fail(label, "buttons that open another window need one '…' character: {}".format(
            sorted(bad)))


def rule_buttons(label, win):
    """Action buttons: no icon, no ▶ prefix (docs/gui-grammar.md, Primary action and Buttons)."""
    from qgis.PyQt.QtWidgets import QPushButton
    bad = set()
    for b in win.findChildren(QPushButton):
        t = text_of(b).replace("&", "").strip()
        if not b.icon().isNull() and t:
            bad.add("{} (icon)".format(t))
        if t.startswith("\u25b6"):
            bad.add("{} (\u25b6 prefix)".format(t))
    if bad:
        fail(label, "action buttons carry no icon or \u25b6: {}".format(sorted(bad)))


def rule_close(label, win):
    btns = [b for b in win.findChildren(QAbstractButton)
            if text_of(b).replace("&", "").strip() == "Close" and b.isVisibleTo(win)]
    if not btns:
        return
    b = btns[-1]
    p = b.mapTo(win, b.rect().center())
    if p.x() < win.width() * 0.6 or p.y() < win.height() * 0.6:
        fail(label, "'Close' is not at the bottom right ({}, {} in {}x{})".format(
            p.x(), p.y(), win.width(), win.height()))


def rule_tables(label, win):
    """A settings table shows its Value column without scrolling sideways."""
    from qgis.PyQt.QtWidgets import QTableWidget, QTabWidget
    seen = set()
    for tabs in [None] + win.findChildren(QTabWidget):
        for i in range(tabs.count() if tabs is not None else 1):
            if tabs is not None:
                tabs.setCurrentIndex(i)
                pump(2)
            for t in win.findChildren(QTableWidget):
                if id(t) not in seen and t.isVisibleTo(win):
                    seen.add(id(t))
                    _table_fits(label, win, t)


def rule_no_sideways(label, win):
    """A scrolling page scrolls down, never sideways (its right-hand buttons would hide)."""
    from qgis.PyQt.QtWidgets import QApplication, QScrollArea, QTabWidget
    if QApplication.primaryScreen().availableGeometry().width() < 1280:
        return          # Qt5 offscreen is 800x600; run under a 1080p display (xvfb) to check this
    bad = set()
    for tabs in win.findChildren(QTabWidget):
        if not tabs.isVisible() or tabs.window() is not win:
            continue    # a child window's tabs (settings), checked with that window
        for i in range(tabs.count()):
            tabs.setCurrentIndex(i)
            pump(2)
            for sa in [tabs.widget(i)]:
                if isinstance(sa, QScrollArea) and sa.widget() is not None and \
                        sa.widget().width() > sa.viewport().width() + 2:
                    bad.add("{} ({} px in {})".format(tabs.tabText(i), sa.widget().width(),
                                                       sa.viewport().width()))
    if bad:
        fail(label, "pages scroll sideways: {}".format(sorted(bad)))


def _table_fits(label, win, t):
    from qgis.PyQt.QtWidgets import QTableWidget
    for t in [t]:
        if not t.isVisibleTo(win) or t.rowCount() == 0:
            continue
        heads = [t.horizontalHeaderItem(i).text() if t.horizontalHeaderItem(i) else ""
                 for i in range(t.columnCount())]
        if "Value" not in heads:
            continue
        c = heads.index("Value")
        right = t.horizontalHeader().sectionPosition(c) + t.columnWidth(c)
        if right > t.viewport().width() + 2:
            fail(label, "the {} table's Value column ends at {} px, past its {} px width: it "
                 "needs a sideways scroll".format(" / ".join(h for h in heads if h), right,
                                                   t.viewport().width()))


def check(label, win, top=True, close=True):
    if win is None:
        if label.endswith(" settings"):
            return                  # a settings TAB, not a window, in this plugin
        fail(label, "did not open")
        return
    win.resize(max(win.width(), 980), max(win.height(), 680))
    win.show()
    pump()
    for rule in (rule_tab_amp, rule_fits, rule_colours, rule_ellipsis, rule_buttons,
                 rule_tables, rule_no_sideways):
        try:
            rule(label, win)
        except Exception:                                        # noqa
            fail(label, "rule {} errored: {}".format(rule.__name__, traceback.format_exc()))
    rule_title(label, win, top)
    if close:
        rule_close(label, win)
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        win.grab().save(os.path.join(SHOTS, re.sub(r"\W+", "_", label).strip("_") + ".png"))


def rule_icons():
    for p in PLUGINS:
        with open(os.path.join(REPO, p, "metadata.txt"), encoding="utf-8") as f:
            m = re.search(r"^icon=(.+)$", f.read(), re.M)
        if not m:
            fail(p, "metadata.txt has no icon=")
        elif not os.path.isfile(os.path.join(REPO, p, m.group(1).strip())):
            fail(p, "icon {} is missing".format(m.group(1).strip()))


# ------------------------------------------------------------------------ the windows -----------
def opened(label, make):
    try:
        return make()
    except Exception:                                            # noqa
        fail(label, "could not open: " + traceback.format_exc().strip().splitlines()[-1])
        return None


def main():
    rule_icons()
    import sync_ui
    for p in sync_ui.check(REPO):
        fail("shared/ui", p)

    from ND_Dam_Hazard_Helper.dialog import NDDamHazardDialog
    check("Hazard main", opened("Hazard main", lambda: NDDamHazardDialog(parent=None)))

    from nd_breach_runner.dialog import NDBreachRunnerDialog
    run = opened("Runner main", lambda: NDBreachRunnerDialog())
    check("Runner main", run)
    if run is not None:
        check("Runner settings", getattr(run, "settings_dialog", None))
    from nd_breach_runner import wizard
    check("Runner wizard", opened("Runner wizard", lambda: wizard._classes()["Wizard"](None)),
          close=False)

    from nd_dam_screening.dialog import NDDamScreeningDialog
    scr = opened("Screening main", lambda: NDDamScreeningDialog(parent=None, iface=None))
    check("Screening main", scr)
    if scr is not None:
        check("Screening settings", getattr(scr, "settings_window", None))

    from ND_Dam_Hazard_Helper.suite_home import SuiteHomeDialog
    check("Dam suite home", opened("Dam suite home", lambda: SuiteHomeDialog(None, None, "lint")),
          top=False)

    from nd_data_updater.plugin import NdDataUpdaterPlugin
    from nd_data_updater.settings_dialog import SettingsDialog
    from nd_data_updater.pack_dialog import PackDialog
    data = NdDataUpdaterPlugin(None)          # no iface: initGui is not called
    check("Updater settings", opened("Updater settings", lambda: SettingsDialog(data.settings, data, None)),
          close=False)
    check("Updater pack", opened("Updater pack", lambda: PackDialog(data.settings, data, None)))

    for line in FAILS:
        print(line)
    print("gui_lint: {} failure(s)".format(len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    finally:
        shutil.rmtree(ROOT, ignore_errors=True)
        sys.stdout.flush()
    os._exit(code)                  # offscreen QGIS can crash on teardown; the result stands
