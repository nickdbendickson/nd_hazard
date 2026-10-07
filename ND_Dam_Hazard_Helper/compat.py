# -*- coding: utf-8 -*-
"""Version-independent names for QGIS 3.28 - 3.44 (Qt5 / PyQt5) and
QGIS 4.x (Qt6 / PyQt6).

Qt6 / PyQt6 only accepts fully qualified enums (Qt.WindowModality.
WindowModal, Qgis.MessageLevel.Warning, ...), QAction moved to QtGui and
field types are QMetaType.Type instead of QVariant.Type. QGIS 3.28 lacks
some of the new spellings (Qgis.GeometryType, Qgis.WkbType, Qgis.DistanceUnit
arrived in 3.30). Every such name is resolved here once - new spelling
first, legacy spelling second - so the rest of the plugin never uses a
version-specific name.
"""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QMessageBox
from qgis.core import (
    Qgis, QgsWkbTypes, QgsUnitTypes, QgsVectorFileWriter, QgsBlockingNetworkRequest,
    QgsColorRampShader, QgsField,
)

try:                                   # Qt6: QtGui, Qt5: QtWidgets
    from qgis.PyQt.QtGui import QAction  # noqa: F401
except ImportError:                    # pragma: no cover - depends on Qt build
    from qgis.PyQt.QtWidgets import QAction  # noqa: F401

try:
    from qgis.core import NULL         # QVariant() on Qt5 builds
except ImportError:                    # pragma: no cover
    NULL = None

try:
    from qgis.PyQt import sip          # noqa: F401
except ImportError:                    # pragma: no cover
    import sip                         # noqa: F401


_MISSING = object()


def _first(*getters, **kw):
    """Value of the first getter that works. With default=..., that value is
    returned when none works (optional names); otherwise AttributeError."""
    for g in getters:
        try:
            v = g()
        except Exception:
            continue
        if v is not None:
            return v
    default = kw.get("default", _MISSING)
    if default is not _MISSING:
        return default
    raise AttributeError("none of the enum spellings exist in this QGIS build")


def enum_value(v):
    """Plain int for an enum member (PyQt6 enums are not ints)."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return getattr(v, "value", v)


try:
    QGIS_VERSION_INT = int(Qgis.QGIS_VERSION_INT)
except Exception:                      # pragma: no cover
    QGIS_VERSION_INT = 0
try:
    from qgis.PyQt.QtCore import QT_VERSION_STR
except ImportError:                    # pragma: no cover
    QT_VERSION_STR = "5"
QT6 = str(QT_VERSION_STR).startswith("6")

# --- message levels ---------------------------------------------------------
MSG_INFO = _first(lambda: Qgis.MessageLevel.Info, lambda: Qgis.Info)
MSG_WARNING = _first(lambda: Qgis.MessageLevel.Warning, lambda: Qgis.Warning)
MSG_CRITICAL = _first(lambda: Qgis.MessageLevel.Critical, lambda: Qgis.Critical)

# --- geometry categories (layer.geometryType()) ------------------------------
GEOM_POINT = _first(lambda: Qgis.GeometryType.Point,
                    lambda: QgsWkbTypes.GeometryType.PointGeometry,
                    lambda: QgsWkbTypes.PointGeometry)
GEOM_LINE = _first(lambda: Qgis.GeometryType.Line,
                   lambda: QgsWkbTypes.GeometryType.LineGeometry,
                   lambda: QgsWkbTypes.LineGeometry)
GEOM_POLYGON = _first(lambda: Qgis.GeometryType.Polygon,
                      lambda: QgsWkbTypes.GeometryType.PolygonGeometry,
                      lambda: QgsWkbTypes.PolygonGeometry)

# --- WKB types ----------------------------------------------------------------
WKB_POINT = _first(lambda: Qgis.WkbType.Point, lambda: QgsWkbTypes.Type.Point,
                   lambda: QgsWkbTypes.Point)
WKB_LINESTRING = _first(lambda: Qgis.WkbType.LineString, lambda: QgsWkbTypes.Type.LineString,
                        lambda: QgsWkbTypes.LineString)
WKB_POLYGON = _first(lambda: Qgis.WkbType.Polygon, lambda: QgsWkbTypes.Type.Polygon,
                     lambda: QgsWkbTypes.Polygon)
WKB_MULTIPOINT = _first(lambda: Qgis.WkbType.MultiPoint, lambda: QgsWkbTypes.Type.MultiPoint,
                        lambda: QgsWkbTypes.MultiPoint)
WKB_MULTILINESTRING = _first(lambda: Qgis.WkbType.MultiLineString,
                             lambda: QgsWkbTypes.Type.MultiLineString,
                             lambda: QgsWkbTypes.MultiLineString)
WKB_MULTIPOLYGON = _first(lambda: Qgis.WkbType.MultiPolygon,
                          lambda: QgsWkbTypes.Type.MultiPolygon,
                          lambda: QgsWkbTypes.MultiPolygon)
WKB_NOGEOMETRY = _first(lambda: Qgis.WkbType.NoGeometry, lambda: QgsWkbTypes.Type.NoGeometry,
                        lambda: QgsWkbTypes.NoGeometry)

# --- units --------------------------------------------------------------------
DIST_FEET = _first(lambda: Qgis.DistanceUnit.Feet,
                   lambda: QgsUnitTypes.DistanceUnit.DistanceFeet,
                   lambda: QgsUnitTypes.DistanceFeet)
DIST_METERS = _first(lambda: Qgis.DistanceUnit.Meters,
                     lambda: QgsUnitTypes.DistanceUnit.DistanceMeters,
                     lambda: QgsUnitTypes.DistanceMeters)
DIST_DEGREES = _first(lambda: Qgis.DistanceUnit.Degrees,
                      lambda: QgsUnitTypes.DistanceUnit.DistanceDegrees,
                      lambda: QgsUnitTypes.DistanceDegrees)
DIST_UNKNOWN = _first(lambda: Qgis.DistanceUnit.Unknown,
                      lambda: QgsUnitTypes.DistanceUnit.DistanceUnknownUnit,
                      lambda: QgsUnitTypes.DistanceUnknownUnit)
DIST_MILES = _first(lambda: Qgis.DistanceUnit.Miles,
                    lambda: QgsUnitTypes.DistanceUnit.DistanceMiles,
                    lambda: QgsUnitTypes.DistanceMiles, default=None)
AREA_SQFT = _first(lambda: Qgis.AreaUnit.SquareFeet,
                   lambda: QgsUnitTypes.AreaUnit.AreaSquareFeet,
                   lambda: QgsUnitTypes.AreaSquareFeet)

# --- misc QGIS enums ----------------------------------------------------------
WRITER_NO_ERROR = _first(lambda: QgsVectorFileWriter.WriterError.NoError,
                         lambda: QgsVectorFileWriter.NoError)
NET_NO_ERROR = _first(lambda: QgsBlockingNetworkRequest.ErrorCode.NoError,
                      lambda: QgsBlockingNetworkRequest.NoError)
SHADER_DISCRETE = _first(lambda: Qgis.ShaderInterpolationMethod.Discrete,
                         lambda: QgsColorRampShader.Type.Discrete,
                         lambda: QgsColorRampShader.Discrete, default=None)
SHADER_LINEAR = _first(lambda: Qgis.ShaderInterpolationMethod.Linear,
                       lambda: QgsColorRampShader.Type.Interpolated,
                       lambda: QgsColorRampShader.Interpolated, default=None)
GEOM_OP_SUCCESS = _first(lambda: Qgis.GeometryOperationResult.Success, lambda: 0)

# --- Qt -----------------------------------------------------------------------
WINDOW_MODAL = _first(lambda: Qt.WindowModality.WindowModal, lambda: Qt.WindowModal)
MB_YES = _first(lambda: QMessageBox.StandardButton.Yes, lambda: QMessageBox.Yes)
MB_NO = _first(lambda: QMessageBox.StandardButton.No, lambda: QMessageBox.No)
MB_CANCEL = _first(lambda: QMessageBox.StandardButton.Cancel, lambda: QMessageBox.Cancel)


# --- field types ----------------------------------------------------------------
def _metatype_types():
    """QMetaType.Type members if this QGIS build's QgsField accepts them
    (QGIS >= 3.38 and all Qt6 builds); None otherwise."""
    try:
        from qgis.PyQt.QtCore import QMetaType
    except ImportError:
        return None
    T = getattr(QMetaType, "Type", QMetaType)

    def m(*names):
        return _first(*[(lambda n=n: getattr(T, n)) for n in names] +
                      [(lambda n=n: getattr(QMetaType, n)) for n in names])
    try:
        types = {"Int": m("Int"), "UInt": m("UInt"), "LongLong": m("LongLong"),
                 "ULongLong": m("ULongLong"), "Double": m("Double"),
                 "String": m("QString"), "Bool": m("Bool"), "Date": m("QDate"),
                 "DateTime": m("QDateTime"), "Invalid": m("UnknownType"),
                 "List": m("QVariantList"), "StringList": m("QStringList")}
        probe = QgsField("probe", types["Double"])
        if enum_value(probe.type()) != enum_value(types["Double"]):
            return None
        return types
    except Exception:
        return None


def _qvariant_types():
    from qgis.PyQt.QtCore import QVariant
    return {"Int": QVariant.Int, "UInt": QVariant.UInt, "LongLong": QVariant.LongLong,
            "ULongLong": QVariant.ULongLong, "Double": QVariant.Double,
            "String": QVariant.String, "Bool": QVariant.Bool, "Date": QVariant.Date,
            "DateTime": QVariant.DateTime, "Invalid": QVariant.Invalid,
            "List": QVariant.List, "StringList": QVariant.StringList}


_FT = _metatype_types()
FIELD_TYPES_ARE_METATYPE = _FT is not None
if _FT is None:
    _FT = _qvariant_types()

FT_INT = _FT["Int"]
FT_UINT = _FT["UInt"]
FT_LONGLONG = _FT["LongLong"]
FT_ULONGLONG = _FT["ULongLong"]
FT_DOUBLE = _FT["Double"]
FT_STRING = _FT["String"]
FT_BOOL = _FT["Bool"]
FT_DATE = _FT["Date"]
FT_DATETIME = _FT["DateTime"]
FT_INVALID = _FT["Invalid"]
FT_LIST = _FT["List"]
FT_STRINGLIST = _FT["StringList"]


def environment_summary():
    """One line for the log: QGIS / Qt / Python / GDAL / numpy versions."""
    import sys
    try:
        from osgeo import gdal
        gv = gdal.__version__
    except Exception:
        gv = "?"
    try:
        import numpy
        nv = numpy.__version__
    except Exception:
        nv = "?"
    return "QGIS {} | Qt {} | Python {} | GDAL {} | numpy {} | {} | field types: {}".format(
        getattr(Qgis, "QGIS_VERSION", QGIS_VERSION_INT), QT_VERSION_STR,
        sys.version.split()[0], gv, nv, sys.platform,
        "QMetaType" if FIELD_TYPES_ARE_METATYPE else "QVariant")
