"""Minimal, behaviour-recording stand-ins for the Qt widgets the dialog
uses. Anything not modelled explicitly is a harmless no-op."""

import enum

from qgis.PyQt.QtCore import QObject, QT6, FLAVOR

EVENTS = {"messages": [], "processEvents": 0}


class _Signal(object):
    def __init__(self):
        self._slots = []

    def connect(self, fn):
        self._slots.append(fn)

    def disconnect(self, fn=None):
        if fn is None:
            self._slots = []
        elif fn in self._slots:
            self._slots.remove(fn)
        else:
            raise TypeError("not connected")

    def emit(self, *args):
        import inspect
        for s in list(self._slots):
            try:                                 # real PyQt trims to what the slot accepts
                params = inspect.signature(s).parameters.values()
                has_var = any(p.kind == p.VAR_POSITIONAL for p in params)
                npos = sum(1 for p in params
                           if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD))
            except (TypeError, ValueError):      # C/builtin callables with no signature
                s(*args)
                continue
            s(*(args if has_var else args[:npos]))


class _NoOp(object):
    def __call__(self, *a, **k):
        return None

    def __getattr__(self, name):                 # any_signal.connect(...) etc.
        if name.startswith("__"):
            raise AttributeError(name)
        return _NoOp()


class QWidget(QObject):
    def __init__(self, *a, **k):
        super().__init__(a[0] if a else None)
        self._enabled = True
        self._visible = False
        self._tooltip = ""
        self._style = ""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _NoOp()

    def setEnabled(self, v):
        self._enabled = bool(v)

    def isEnabled(self):
        return self._enabled

    def setToolTip(self, t):
        self._tooltip = t

    def toolTip(self):
        return self._tooltip

    def setStyleSheet(self, s):
        self._style = s

    def setVisible(self, v):
        self._visible = bool(v)

    def isVisible(self):
        return self._visible

    def show(self):
        self._visible = True

    def close(self):
        self._visible = False
        return True


class QDialog(QWidget):
    def closeEvent(self, event):
        event.accept()


class QLineEdit(QWidget):
    def __init__(self, *a):
        super().__init__()
        self._text = ""
        self.textChanged = _Signal()
        self.editingFinished = _Signal()

    def text(self):
        return self._text

    def setText(self, t):
        if t != self._text:
            self._text = t
            self.textChanged.emit(t)


class QPushButton(QWidget):
    def __init__(self, text="", *a):
        super().__init__()
        self._text = text
        self.clicked = _Signal()

    def text(self):                              # 0.28.1: the Continue button names its target
        return self._text

    def setText(self, t):
        self._text = t

    def click(self):
        if self._enabled:
            self.clicked.emit()


class QLabel(QWidget):
    def __init__(self, text="", *a):
        super().__init__()
        self._text = text

    def setText(self, t):
        self._text = t

    def text(self):
        return self._text


class QSpinBox(QWidget):
    def __init__(self, *a):
        super().__init__()
        self._v = 0

    def setValue(self, v):
        self._v = v

    def value(self):
        return self._v


class QDoubleSpinBox(QWidget):
    def __init__(self, *a):
        super().__init__()
        self._v = 0.0

    def setValue(self, v):
        self._v = float(v)

    def value(self):
        return self._v


class _UnusedSpin(QSpinBox):
    pass


class QCheckBox(QWidget):
    def __init__(self, *a):
        super().__init__()
        self._c = False
        self.toggled = _Signal()
        self.stateChanged = _Signal()

    def setChecked(self, v):
        changed = bool(v) != self._c
        self._c = bool(v)
        if changed:                      # like Qt: only on a real change
            self.toggled.emit(self._c)
            self.stateChanged.emit(2 if self._c else 0)

    def isChecked(self):
        return self._c


class _ScrollBar(object):
    def maximum(self):
        return 100

    def setValue(self, v):
        pass


class QTextEdit(QWidget):
    def __init__(self, *a):
        super().__init__()
        self.lines = []

    def append(self, t):
        self.lines.append(t)

    def setPlainText(self, t):
        self._plain = t

    def toPlainText(self):
        return getattr(self, "_plain", "")

    def verticalScrollBar(self):
        return _ScrollBar()


class QTabWidget(QWidget):
    def __init__(self, *a):
        super().__init__()
        self.titles = []
        self.currentChanged = _Signal()

    def addTab(self, w, title):
        self.titles.append(title)
        return len(self.titles) - 1

    def tabText(self, i):
        return self.titles[i] if 0 <= i < len(self.titles) else ""

    def count(self):
        return len(self.titles)


class QComboBox(QWidget):
    def __init__(self, *a):
        super().__init__()
        self._items = []
        self._cur = -1
        self.currentIndexChanged = _Signal()

    def addItem(self, text, data=None):
        self._items.append((text, data))
        if self._cur < 0:
            self._cur = 0

    def addItems(self, items):
        for t in items:
            self.addItem(t)

    def clear(self):
        self._items = []
        self._cur = -1

    def count(self):
        return len(self._items)

    def itemData(self, i):
        return self._items[i][1]

    def itemText(self, i):
        return self._items[i][0]

    def currentData(self):
        return self._items[self._cur][1] if 0 <= self._cur < len(self._items) else None

    def currentText(self):
        return self._items[self._cur][0] if 0 <= self._cur < len(self._items) else ""

    def currentIndex(self):
        return self._cur

    def setCurrentIndex(self, i):
        changed = i != self._cur
        self._cur = i
        if changed:
            self.currentIndexChanged.emit(i)

    def findData(self, data):
        for i in range(self.count()):
            if self.itemData(i) == data:
                return i
        return -1

    def select_data(self, data):
        for i, (_t, d) in enumerate(self._items):
            if d == data:
                self._cur = i
                return True
        return False


class QScrollArea(QWidget):
    def setWidget(self, w):
        self._widget = w

    def widget(self):
        return self.__dict__.get("_widget")


class QTableWidgetItem(object):
    def __init__(self, text=""):
        self._text = str(text)
        self._flags = 1 | 2 | 32                      # selectable, editable, enabled (Qt's default)
        self._check = None
        self._data = {}
        self._tooltip = ""
        self._table = None

    def text(self):
        return self._text

    def setText(self, t):
        self._text = str(t)
        if self._table is not None:
            self._table.itemChanged.emit(self)

    def setFlags(self, f):
        self._flags = int(f)

    def flags(self):
        return self._flags

    def setCheckState(self, st):
        self._check = st
        if self._table is not None:
            self._table.itemChanged.emit(self)

    def checkState(self):
        return self._check

    def setData(self, role, v):
        self._data[int(role)] = v

    def data(self, role):
        return self._data.get(int(role))

    def setToolTip(self, t):
        self._tooltip = t

    def toolTip(self):
        return self._tooltip

    def setBackground(self, b):
        self._background = b

    def setForeground(self, b):
        self._foreground = b

    def setFont(self, f):
        self._font = f

    def setTextAlignment(self, a):
        self._align = a

    def row(self):
        if self._table is None:
            return -1
        for (r, c), it in self._table._items.items():
            if it is self:
                return r
        return -1

    def column(self):
        if self._table is None:
            return -1
        for (r, c), it in self._table._items.items():
            if it is self:
                return c
        return -1


class QRadioButton(QCheckBox):
    pass


class QAbstractItemView(QWidget):
    """0.26.0 (tests): the selection behaviour and edit triggers the 0.23.0 dam table sets - scoped
    on Qt6 and 3.40, legacy names on Qt5 only (like Qt: a Qt6 build has no QAbstractItemView.SelectRows)."""
    if QT6 or FLAVOR != "3.28":
        SelectionBehavior = enum.IntEnum("SelectionBehavior", {"SelectItems": 0, "SelectRows": 1, "SelectColumns": 2})
        EditTrigger = enum.IntFlag("EditTrigger", {"NoEditTriggers": 0, "DoubleClicked": 2, "AllEditTriggers": 31})
    if not QT6:
        SelectItems, SelectRows, SelectColumns = 0, 1, 2
        NoEditTriggers, DoubleClicked, AllEditTriggers = 0, 2, 31


class QHeaderView(QWidget):
    """v2.9: horizontal header with the sort indicator. click() does what Qt
    does on a click: flips its own indicator first, then emits sectionClicked.
    PyQt6 only takes a Qt.SortOrder member (not an int)."""
    if QT6 or FLAVOR != "3.28":
        ResizeMode = enum.IntEnum("ResizeMode", {"Interactive": 0, "Stretch": 1, "Fixed": 2, "ResizeToContents": 3})
    if not QT6:
        Interactive, Stretch, Fixed, ResizeToContents = 0, 1, 2, 3

    def __init__(self, *a):
        super().__init__()
        from qgis.PyQt.QtCore import Qt
        self._qt = Qt
        self.sectionClicked = _Signal()
        self._clickable = True
        self._shown = False
        self._section = 0                 # Qt's defaults
        self._order = self._orders()[1]

    def _orders(self):
        so = self._qt.SortOrder if QT6 else self._qt
        return so.AscendingOrder, so.DescendingOrder

    def setSectionsClickable(self, v):
        self._clickable = bool(v)

    def sectionsClickable(self):
        return self._clickable

    def setSortIndicatorShown(self, v):
        self._shown = bool(v)

    def isSortIndicatorShown(self):
        return self._shown

    def setSortIndicator(self, section, order):
        if QT6 and not isinstance(order, self._qt.SortOrder):
            raise TypeError("setSortIndicator(): argument 2 has unexpected type")
        self._section, self._order = int(section), order

    def sortIndicatorSection(self):
        return self._section

    def sortIndicatorOrder(self):
        return self._order

    def click(self, section):
        if not self._clickable:
            return
        asc, desc = self._orders()
        self.setSortIndicator(section, asc if section != self._section or self._order == desc
                              else desc)
        self.sectionClicked.emit(section)


class QTableWidget(QWidget):
    def __init__(self, *a):
        super().__init__()
        self._rows, self._cols = 0, 0
        self._items, self._widgets = {}, {}
        self._cur = (-1, -1)
        self.cellDoubleClicked = _Signal()
        self.cellClicked = _Signal()
        self.itemChanged = _Signal()
        self.currentCellChanged = _Signal()
        self.itemSelectionChanged = _Signal()
        self._hheader = QHeaderView()     # v2.9
        self._sorting = False
        self._hidden = set()
        self._blocked = False

    def blockSignals(self, b):
        old = self._blocked
        self._blocked = bool(b)
        return old

    def setRowHidden(self, r, h):
        if h:
            self._hidden.add(r)
        else:
            self._hidden.discard(r)

    def isRowHidden(self, r):
        return r in self._hidden

    def selectRow(self, r):
        self._cur = (r, 0)

    def horizontalHeader(self):
        return self._hheader

    def setSortingEnabled(self, v):
        self._sorting = bool(v)

    def isSortingEnabled(self):
        return self._sorting

    def setColumnCount(self, n):
        self._cols = n

    def columnCount(self):
        return self._cols

    def setRowCount(self, n):
        self._rows = n
        self._items = {k: v for k, v in self._items.items() if k[0] < n}
        self._widgets = {k: v for k, v in self._widgets.items() if k[0] < n}

    def rowCount(self):
        return self._rows

    def setItem(self, r, c, item):
        self._items[(r, c)] = item
        item._table = self

    def item(self, r, c):
        return self._items.get((r, c))

    def setCellWidget(self, r, c, w):
        self._widgets[(r, c)] = w

    def cellWidget(self, r, c):
        return self._widgets.get((r, c))

    def insertRow(self, r):
        """0.28.0 (tests): a real insert. It used to fall through to QWidget's catch-all, so every
        row a window filled landed on row 0 and rowCount() stayed 0."""
        self._items = {(rr + (rr >= r), c): v for (rr, c), v in self._items.items()}
        self._widgets = {(rr + (rr >= r), c): v for (rr, c), v in self._widgets.items()}
        self._rows += 1

    def removeRow(self, r):
        self._items = {(rr - (rr > r), c): v for (rr, c), v in self._items.items() if rr != r}
        self._widgets = {(rr - (rr > r), c): v for (rr, c), v in self._widgets.items()
                         if rr != r}
        self._rows -= 1

    def setCurrentCell(self, r, c):
        old = self._cur
        self._cur = (r, c)
        self.currentCellChanged.emit(r, c, old[0], old[1])

    def currentRow(self):
        return self._cur[0]

    def currentColumn(self):
        return self._cur[1]


class QInputDialog(object):
    NEXT = []

    @staticmethod
    def getText(*a, **k):
        return (QInputDialog.NEXT.pop(0), True) if QInputDialog.NEXT else ("", False)

    @staticmethod
    def getItem(*a, **k):
        return (QInputDialog.NEXT.pop(0), True) if QInputDialog.NEXT else ("", False)


class QProgressBar(QWidget):
    """0.26.0 (tests): the 0.23.0 step progress bar; setValue / setFormat / setRange are no-ops."""
    def __init__(self, *a):
        super().__init__()
        self._value = 0

    def setValue(self, v):
        self._value = int(v)

    def value(self):
        return self._value


class QGroupBox(QWidget):
    def __init__(self, title="", *a):
        super().__init__()
        self._title = title

    def title(self):
        return self._title


class QVBoxLayout(QWidget):
    pass


class QHBoxLayout(QWidget):
    pass


class QFormLayout(QWidget):
    pass


class QGridLayout(QWidget):
    pass


class QProgressDialog(QWidget):
    # tests set this to an int N to "click Cancel" after N wasCanceled() calls
    CANCEL_AFTER = None
    INSTANCES = []

    def __init__(self, *a):
        super().__init__()
        self.values = []
        self.labels = []
        self._checks = 0
        self.modality = None
        QProgressDialog.INSTANCES.append(self)

    def setWindowModality(self, m):
        self.modality = m

    def setValue(self, v):
        self.values.append(v)

    def setLabelText(self, t):
        self.labels.append(t)

    def wasCanceled(self):
        self._checks += 1
        n = QProgressDialog.CANCEL_AFTER
        return n is not None and self._checks >= n


class _QAction(QWidget):
    def __init__(self, *a):
        super().__init__()
        self.triggered = _Signal()


if not QT6:
    QAction = _QAction


class QMessageBox(object):
    StandardButton = (enum.Flag if QT6 else enum.IntFlag)(
        "StandardButton", {"Ok": 0x400, "Yes": 0x4000, "No": 0x10000, "Cancel": 0x400000})
    QUESTION_ANSWER = StandardButton.Yes

    @staticmethod
    def information(parent, title, text, *a):
        EVENTS["messages"].append(("information", title, text))
        return _SB.Ok

    @staticmethod
    def warning(parent, title, text, *a):
        EVENTS["messages"].append(("warning", title, text))
        return _SB.Ok

    @staticmethod
    def critical(parent, title, text, *a):
        EVENTS["messages"].append(("critical", title, text))
        return _SB.Ok

    @staticmethod
    def question(parent, title, text, *a):
        EVENTS["messages"].append(("question", title, text))
        return QMessageBox.QUESTION_ANSWER


class QFileDialog(object):
    NEXT = []

    @staticmethod
    def getExistingDirectory(*a):
        return QFileDialog.NEXT.pop(0) if QFileDialog.NEXT else ""

    @staticmethod
    def getOpenFileName(*a):
        return (QFileDialog.NEXT.pop(0) if QFileDialog.NEXT else "", "")

    @staticmethod
    def getSaveFileName(*a):
        return (QFileDialog.NEXT.pop(0) if QFileDialog.NEXT else "", "")


class QApplication(object):
    @staticmethod
    def processEvents():
        EVENTS["processEvents"] += 1


_SB = QMessageBox.StandardButton
if not QT6:
    for _b in QMessageBox.StandardButton:
        setattr(QMessageBox, _b.name, _b)
    if FLAVOR == "3.28":
        del QMessageBox.StandardButton
