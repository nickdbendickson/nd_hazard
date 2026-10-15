"""Version-independent Qt and QGIS names for QGIS 3.28 - 3.44 (Qt5 / PyQt5) and QGIS 4.x
(Qt6 / PyQt6). 1.4.1.

PyQt6 only accepts fully qualified enums (QMessageBox.StandardButton.Yes,
Qt.WindowModality.WindowModal, Qgis.MessageLevel.Warning, ...), QAction moved to QtGui and
exec_() is gone. Each name is resolved here once, new spelling first and legacy spelling second,
so the rest of the plugin never uses a version-specific name. The same pattern as the Hazard
Helper's compat.py.
"""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtNetwork import QNetworkRequest
from qgis.PyQt.QtWidgets import (QAbstractItemView, QDialogButtonBox, QHeaderView,
                                 QMessageBox, QPlainTextEdit)
from qgis.core import Qgis, QgsBlockingNetworkRequest, QgsTask

try:                                   # Qt6: QtGui, Qt5: QtWidgets
    from qgis.PyQt.QtGui import QAction  # noqa: F401
except ImportError:                    # pragma: no cover - depends on Qt build
    from qgis.PyQt.QtWidgets import QAction  # noqa: F401

try:
    from qgis.core import QgsMapLayerType
except ImportError:                    # pragma: no cover - very new builds
    QgsMapLayerType = None


def _first(*getters):
    """Value of the first getter that works; AttributeError when none does."""
    for g in getters:
        try:
            v = g()
        except Exception:
            continue
        if v is not None:
            return v
    raise AttributeError("none of the enum spellings exist in this Qt / QGIS build")


def exec_dialog(dialog):
    """dialog.exec() on Qt6, dialog.exec_() on Qt5."""
    run = getattr(dialog, "exec", None) or getattr(dialog, "exec_")
    return run()


# QMessageBox / QDialogButtonBox buttons
MB_YES = _first(lambda: QMessageBox.StandardButton.Yes, lambda: QMessageBox.Yes)
MB_CANCEL = _first(lambda: QMessageBox.StandardButton.Cancel, lambda: QMessageBox.Cancel)
BB_CLOSE = _first(lambda: QDialogButtonBox.StandardButton.Close, lambda: QDialogButtonBox.Close)
BB_SAVE = _first(lambda: QDialogButtonBox.StandardButton.Save, lambda: QDialogButtonBox.Save)
BB_CANCEL = _first(lambda: QDialogButtonBox.StandardButton.Cancel, lambda: QDialogButtonBox.Cancel)

# Qt
WINDOW_MODAL = _first(lambda: Qt.WindowModality.WindowModal, lambda: Qt.WindowModal)
WAIT_CURSOR = _first(lambda: Qt.CursorShape.WaitCursor, lambda: Qt.WaitCursor)
ITEM_IS_EDITABLE = _first(lambda: Qt.ItemFlag.ItemIsEditable, lambda: Qt.ItemIsEditable)

# item views
NO_EDIT_TRIGGERS = _first(lambda: QAbstractItemView.EditTrigger.NoEditTriggers,
                          lambda: QAbstractItemView.NoEditTriggers)
SELECT_ROWS = _first(lambda: QAbstractItemView.SelectionBehavior.SelectRows,
                     lambda: QAbstractItemView.SelectRows)
NO_SELECTION = _first(lambda: QAbstractItemView.SelectionMode.NoSelection,
                      lambda: QAbstractItemView.NoSelection)
HEADER_STRETCH = _first(lambda: QHeaderView.ResizeMode.Stretch, lambda: QHeaderView.Stretch)
HEADER_TO_CONTENTS = _first(lambda: QHeaderView.ResizeMode.ResizeToContents,
                            lambda: QHeaderView.ResizeToContents)
TEXT_NO_WRAP = _first(lambda: QPlainTextEdit.LineWrapMode.NoWrap, lambda: QPlainTextEdit.NoWrap)

# network
CACHE_LOAD_CONTROL = _first(lambda: QNetworkRequest.Attribute.CacheLoadControlAttribute,
                            lambda: QNetworkRequest.CacheLoadControlAttribute)
ALWAYS_NETWORK = _first(lambda: QNetworkRequest.CacheLoadControl.AlwaysNetwork,
                        lambda: QNetworkRequest.AlwaysNetwork)
HTTP_STATUS = _first(lambda: QNetworkRequest.Attribute.HttpStatusCodeAttribute,
                     lambda: QNetworkRequest.HttpStatusCodeAttribute)
BLOCKING_NO_ERROR = _first(lambda: QgsBlockingNetworkRequest.ErrorCode.NoError,
                           lambda: QgsBlockingNetworkRequest.NoError)

# QGIS
MSG_INFO = _first(lambda: Qgis.MessageLevel.Info, lambda: Qgis.Info)
MSG_WARNING = _first(lambda: Qgis.MessageLevel.Warning, lambda: Qgis.Warning)
TASK_CAN_CANCEL = _first(lambda: QgsTask.Flag.CanCancel, lambda: QgsTask.CanCancel)
VECTOR_LAYER = _first(lambda: Qgis.LayerType.Vector,
                      lambda: QgsMapLayerType.VectorLayer)
