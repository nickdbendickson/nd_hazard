"""Just enough of qgis / qgis.PyQt / osgeo to IMPORT the plugin's modules without QGIS (v2.28).

install() does nothing when a real PyQGIS imports (the tests then run against it). Otherwise it
puts stand-in modules in sys.modules: any name asked of them is a class that accepts any
constructor arguments, whose attributes (on the class and on its objects) are more stand-ins,
whose calls return a stand-in, and which is false, zero and empty. That is enough for the
module-level code of compat.py / helpers.py / classification.py (enum look-ups, a probe
QgsField) - nothing here draws a map or writes a layer. Tests that need more set attributes on
the module they use (e.g. qgis.utils.plugins)."""
import sys
import types


class _Meta(type):
    def __getattr__(cls, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return Fake()


class Fake(metaclass=_Meta):
    """Stands in for any QGIS / Qt / GDAL class or object."""

    def __init__(self, *a, **k):
        pass

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return Fake()

    def __call__(self, *a, **k):
        return Fake()

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0

    def __bool__(self):
        return False

    def __int__(self):
        return 0

    def __index__(self):
        return 0

    def __float__(self):
        return 0.0

    def __or__(self, other):
        return self

    __ror__ = __and__ = __rand__ = __or__

    def __eq__(self, other):
        return self is other

    def __hash__(self):
        return id(self)

    def __repr__(self):
        return "<fake_qgis.Fake>"


_classes = {}


def _class(qualname):
    """One class per name, so `isinstance(x, QgsVectorLayer)` and `class A(QDialog)` work."""
    if qualname not in _classes:
        _classes[qualname] = _Meta(qualname.rsplit(".", 1)[-1], (Fake,), {})
    return _classes[qualname]


def _module(name):
    m = types.ModuleType(name)
    m.__getattr__ = lambda attr, _n=name: _class(_n + "." + attr)        # PEP 562
    m.__path__ = []                                                      # a package
    sys.modules[name] = m
    return m


INSTALLED = None            # "real" / "fake" after install()


def install():
    """Use the real PyQGIS when it imports, else the stand-ins. Returns "real" or "fake"."""
    global INSTALLED
    if INSTALLED:
        return INSTALLED
    try:
        import qgis.core  # noqa: F401
        INSTALLED = "real"
        return INSTALLED
    except Exception:                                            # noqa - no QGIS here
        pass
    qgis = _module("qgis")
    for sub in ("core", "gui", "utils", "analysis", "processing"):
        setattr(qgis, sub, _module("qgis." + sub))
    pyqt = _module("qgis.PyQt")
    qgis.PyQt = pyqt
    for sub in ("QtCore", "QtGui", "QtWidgets", "QtXml", "QtPrintSupport", "QtSvg", "QtNetwork",
                "sip"):
        setattr(pyqt, sub, _module("qgis.PyQt." + sub))
    # qgis.utils as the plugin reads it (tests replace these for a scenario)
    qgis.utils.plugins = {}
    qgis.utils.loadPlugin = lambda name: False
    qgis.utils.startPlugin = lambda name: False
    qgis.utils.pluginMetadata = lambda name, key: "__error__"
    qgis.utils.iface = None
    try:
        import osgeo  # noqa: F401
    except Exception:                                            # noqa - no GDAL here
        osgeo = _module("osgeo")
        for sub in ("gdal", "ogr", "osr", "gdal_array"):
            setattr(osgeo, sub, _module("osgeo." + sub))
    INSTALLED = "fake"
    return INSTALLED
