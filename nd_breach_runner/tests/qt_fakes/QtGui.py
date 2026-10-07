class QIcon(object):
    def __init__(self, *a):
        pass


class QFont(object):
    def __init__(self, family="", *a):
        self.family = family
        self.bold = False
        self.size = None

    def setBold(self, b):
        self.bold = bool(b)

    def setPointSizeF(self, s):
        self.size = s


class QColor(object):
    def __init__(self, *a):
        self.args = a


class QBrush(object):
    def __init__(self, *a):
        self.args = a


from qgis.PyQt.QtCore import QT6 as _QT6

if _QT6:                       # Qt6: QAction moved from QtWidgets to QtGui
    from qgis.PyQt.QtWidgets import _QAction as QAction  # noqa: F401


OPENED_URLS = []


class QDesktopServices(object):
    @staticmethod
    def openUrl(url):
        OPENED_URLS.append(url.toLocalFile() or url.toString())
        return True
