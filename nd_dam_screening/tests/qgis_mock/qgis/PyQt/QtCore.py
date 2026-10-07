"""Test double of qgis.PyQt.QtCore. QVariant keeps the string type names the screening mock
uses; the rest (Qt, QObject, QUrl, pyqtSignal, QT6 / FLAVOR) is for the Qt widget fakes."""
import enum
import os


FLAVOR = os.environ.get("FAKE_QGIS_FLAVOR", "3.28")
QT6 = FLAVOR.startswith("4")


class QVariant:
    # PyQt6 has no QVariant.Type members: Qt6 code must use QMetaType.Type
    if not QT6:
        Double, String, Int, LongLong, Bool = "double", "string", "int", "longlong", "bool"
QT_VERSION_STR = "6.8.3" if QT6 else "5.15.13"

_QV_TYPES = {"Invalid": 0, "Bool": 1, "Int": 2, "UInt": 3, "LongLong": 4, "ULongLong": 5,
             "Double": 6, "Map": 8, "List": 9, "String": 10, "StringList": 11, "Date": 14,
             "Time": 15, "DateTime": 16}


class QMetaType(object):
    # PyQt6 enums are not ints; PyQt5's are
    Type = (enum.Enum if QT6 else enum.IntEnum)("Type", {
        "UnknownType": 0, "Bool": 1, "Int": 2, "UInt": 3, "LongLong": 4, "ULongLong": 5,
        "Double": 6, "QVariantMap": 8, "QVariantList": 9, "QString": 10, "QStringList": 11,
        "QDate": 14, "QTime": 15, "QDateTime": 16})


class Qt(object):
    pass


if QT6:
    Qt.WindowModality = enum.Enum("WindowModality", {"NonModal": 0, "WindowModal": 1,
                                                     "ApplicationModal": 2})
else:
    Qt.WindowModal, Qt.ApplicationModal, Qt.ISODate = 1, 2, 1
    if FLAVOR != "3.28":
        Qt.WindowModality = enum.IntEnum("WindowModality", {"NonModal": 0, "WindowModal": 1,
                                                            "ApplicationModal": 2})

# v2.9 (review sorting): Qt.SortOrder - scoped only on Qt6, legacy (+ scoped
# on 3.40) on Qt5
if QT6:
    Qt.SortOrder = enum.Enum("SortOrder", {"AscendingOrder": 0, "DescendingOrder": 1})
else:
    Qt.AscendingOrder, Qt.DescendingOrder = 0, 1
    if FLAVOR != "3.28":
        Qt.SortOrder = enum.IntEnum("SortOrder", {"AscendingOrder": 0, "DescendingOrder": 1})


# 0.13 (settings grid / review tab): item flags, check states, data roles, alignment - scoped
# only on Qt6, legacy (+ scoped on 3.40) on Qt5
_ITEM_FLAGS = {"NoItemFlags": 0, "ItemIsSelectable": 1, "ItemIsEditable": 2, "ItemIsUserCheckable": 16,
               "ItemIsEnabled": 32}
_CHECK = {"Unchecked": 0, "PartiallyChecked": 1, "Checked": 2}
_ROLES = {"DisplayRole": 0, "EditRole": 2, "ToolTipRole": 3, "UserRole": 256}
_ALIGN = {"AlignLeft": 1, "AlignRight": 2, "AlignHCenter": 4, "AlignVCenter": 128}
if QT6:
    Qt.ItemFlag = enum.IntFlag("ItemFlag", _ITEM_FLAGS)
    Qt.CheckState = enum.Enum("CheckState", _CHECK)
    Qt.ItemDataRole = enum.IntEnum("ItemDataRole", _ROLES)
    Qt.AlignmentFlag = enum.IntFlag("AlignmentFlag", _ALIGN)
else:
    for _d in (_ITEM_FLAGS, _CHECK, _ROLES, _ALIGN):
        for _k, _v in _d.items():
            setattr(Qt, _k, _v)
    if FLAVOR != "3.28":
        Qt.ItemFlag = enum.IntFlag("ItemFlag", _ITEM_FLAGS)
        Qt.CheckState = enum.IntEnum("CheckState", _CHECK)
        Qt.ItemDataRole = enum.IntEnum("ItemDataRole", _ROLES)
        Qt.AlignmentFlag = enum.IntFlag("AlignmentFlag", _ALIGN)


class QObject(object):
    def __init__(self, parent=None):
        self._parent = parent
        self._deleted = False

    def deleteLater(self):
        self._deleted = True


class _BoundSignal(object):
    def __init__(self):
        self._slots = []

    def connect(self, fn, *a):
        self._slots.append(fn)

    def disconnect(self, fn=None):
        if fn is None:
            self._slots = []
        elif fn in self._slots:
            self._slots.remove(fn)

    def emit(self, *args):
        for fn in list(self._slots):
            fn(*args)


class pyqtSignal(object):
    """Synchronous stand-in: emit() calls the connected slots at once."""

    def __init__(self, *a, **k):
        self._name = None

    def __set_name__(self, owner, name):
        self._name = name

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        d = obj.__dict__.setdefault("_signals", {})
        return d.setdefault(self._name, _BoundSignal())


class QUrl(object):
    def __init__(self, s=""):
        self._s = s

    def toString(self):
        return self._s

    @staticmethod
    def fromLocalFile(path):
        u = QUrl("file://" + str(path))
        u._local = str(path)
        return u

    def toLocalFile(self):
        return getattr(self, "_local", "")


