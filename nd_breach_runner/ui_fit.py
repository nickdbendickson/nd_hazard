"""Keep a plugin window on the screen and scroll a long tab.

Canonical copy: shared/ui/ui_fit.py (Hazard, Runner, Screening and the Data Updater ship
byte-identical copies); edit it there and run tools/sync_ui.py."""
from qgis.PyQt.QtCore import Qt, QSize
from qgis.PyQt import QtWidgets
from qgis.PyQt.QtWidgets import QApplication, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

# pages that scroll themselves (tables, text boxes); a Qt test double may not have the class
_SELF_SCROLLING = getattr(QtWidgets, "QAbstractScrollArea", QScrollArea)


def _policy(name):
    enum = getattr(QSizePolicy, "Policy", QSizePolicy)
    return getattr(enum, name)


class FitScroll(QScrollArea):
    """Scrolls a long page. The size hint stays small so the page does not cover the log."""

    def sizeHint(self):
        return QSize(640, 240)

    def minimumSizeHint(self):
        return QSize(240, 120)


def _scroll(page):
    scroll = FitScroll()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(getattr(getattr(QScrollArea, "Shape", QScrollArea), "NoFrame"))
    bar = getattr(Qt, "ScrollBarPolicy", Qt)
    scroll.setHorizontalScrollBarPolicy(bar.ScrollBarAsNeeded)
    scroll.setVerticalScrollBarPolicy(bar.ScrollBarAsNeeded)
    scroll.setSizePolicy(_policy("Expanding"), _policy("Expanding"))
    scroll.setMinimumHeight(120)
    scroll.setWidget(page)
    return scroll


def wrap_tabs(tabs):
    """Put each tab page in a scroll area. A short page does not show a bar. A page that scrolls
    itself (a table, a text box) is left as it is."""
    if tabs is None:
        return
    current = tabs.currentIndex()
    i = 0
    while i < tabs.count():
        page = tabs.widget(i)
        if isinstance(page, _SELF_SCROLLING):
            i += 1
            continue
        title = tabs.tabText(i)
        tabs.removeTab(i)
        tabs.insertTab(i, _scroll(page), title)
        i += 1
    tabs.setSizePolicy(_policy("Expanding"), _policy("Expanding"))
    if 0 <= current < tabs.count():
        tabs.setCurrentIndex(current)


def scroll_page(page):
    """Scroll a wizard page without changing its fields."""
    if page is None or isinstance(page, QScrollArea):
        return
    lay = page.layout()
    if lay is None:
        return
    holder = QWidget()
    holder.setLayout(lay)
    outer = QVBoxLayout()
    outer.setContentsMargins(0, 0, 0, 0)
    outer.addWidget(_scroll(holder))
    page.setLayout(outer)


def wrap_wizard(wizard):
    if wizard is None:
        return
    for page_id in wizard.pageIds():
        scroll_page(wizard.page(page_id))


def _saved_size(key):
    if not key:
        return None
    try:
        from qgis.PyQt.QtCore import QSettings
        v = QSettings().value(key + "/size")
        if v is None:
            return None
        if hasattr(v, "width"):
            return int(v.width()), int(v.height())
        w, h = v
        return int(w), int(h)
    except Exception:                                            # noqa
        return None


def save_size(dialog, key):
    """Remember the window size under QSettings `key` (call it on close)."""
    if not key:
        return
    try:
        from qgis.PyQt.QtCore import QSettings
        QSettings().setValue(key + "/size", QSize(int(dialog.width()), int(dialog.height())))
    except Exception:                                            # noqa
        pass


def _screen_for(dialog):
    """The screen the window opens on: its own once it is shown, else its parent window's
    screen, else the screen under the mouse, else the primary screen (3.10.21 - before show
    it fell back to the primary screen, not the one under the mouse)."""
    try:
        if dialog.isVisible() and dialog.windowHandle() is not None \
                and dialog.windowHandle().screen() is not None:
            return dialog.windowHandle().screen()
    except Exception:                                                    # noqa
        pass
    parent = dialog.parentWidget() if hasattr(dialog, "parentWidget") else None
    if parent is not None:
        try:
            win = parent.window()
            scr = win.screen() if hasattr(win, "screen") else None      # Qt >= 5.14
            if scr is None and win.windowHandle() is not None:
                scr = win.windowHandle().screen()
            if scr is not None:
                return scr
        except Exception:                                                # noqa
            pass
    try:
        from qgis.PyQt.QtGui import QCursor
        scr = QApplication.screenAt(QCursor.pos())
        if scr is not None:
            return scr
    except Exception:                                                    # noqa
        pass
    return QApplication.primaryScreen()


def fit_size(prefer, avail, margin=(48, 80)):
    """(width, height, min_w, min_h) for a window preferring `prefer` on a screen whose
    available geometry is `avail` (QRect): never larger than the screen."""
    max_w = max(1, avail.width() - margin[0]) if avail.width() > 2 * margin[0] else avail.width()
    max_h = max(1, avail.height() - margin[1]) if avail.height() > 2 * margin[1] else avail.height()
    return (min(prefer[0], max_w), min(prefer[1], max_h), min(640, max_w), min(420, max_h))


def _follow_screen(dialog):
    """Moved to another monitor: the size cap follows that monitor's available area."""
    if getattr(dialog, "_fit_follows_screen", False):
        return
    try:
        dialog.winId()                          # creates the window handle (no show)
        handle = dialog.windowHandle()
        if handle is None:
            return

        def _on_screen(scr, _d=dialog):
            if scr is None:
                return
            a = scr.availableGeometry()
            _d.setMaximumSize(a.width(), a.height())
            _d.setMinimumSize(min(_d.minimumWidth(), a.width()), min(_d.minimumHeight(), a.height()))
            if _d.width() > a.width() or _d.height() > a.height():
                _d.resize(min(_d.width(), a.width()), min(_d.height(), a.height()))
        handle.screenChanged.connect(_on_screen)
        dialog._fit_follows_screen = True
    except Exception:                                                    # noqa
        pass


_screen_of = _screen_for         # the 3.10.15 name


def fit_window(dialog, prefer=(980, 680), key=None):
    """Size the window to the screen it opens on, and never larger than that screen. With
    `key`, the size save_size() stored under that QSettings key is preferred (still clamped).

    3.10.21 (the Hazard 2.18.36 fix): the screen falls back to the one under the mouse, not
    the primary one; the window is placed inside that screen's available area (centred on
    the parent); the size cap is that screen's available area and follows the window to
    another monitor; and the old 520 x 400 floor (larger than a small screen) is gone."""
    prefer = _saved_size(key) or prefer
    screen = _screen_for(dialog)
    if screen is None:
        dialog.resize(prefer[0], prefer[1])
        return
    avail = screen.availableGeometry()
    w, h, min_w, min_h = fit_size(prefer, avail)
    dialog.setMinimumSize(min_w, min_h)
    dialog.setMaximumSize(avail.width(), avail.height())     # never larger than this screen
    dialog.resize(w, h)
    _follow_screen(dialog)
    parent = dialog.parentWidget() if hasattr(dialog, "parentWidget") else None
    try:
        if parent is not None and avail.intersects(parent.window().frameGeometry()):
            centre = parent.window().frameGeometry().center()
        else:
            centre = avail.center()
        x = min(max(avail.left(), centre.x() - w // 2), avail.right() + 1 - w)
        y = min(max(avail.top(), centre.y() - h // 2), avail.bottom() + 1 - h)
        dialog.move(max(avail.left(), x), max(avail.top(), y))
    except Exception:                                                    # noqa
        pass
