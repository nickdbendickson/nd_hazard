# -*- coding: utf-8 -*-
"""Version-independent enum names for QGIS 3.28 - 3.44 (Qt5 / PyQt5) and QGIS 4.x (Qt6 / PyQt6).

Qt6 / PyQt6 only accepts fully scoped enums, field types are QMetaType.Type there (and from
QGIS 3.38), and several QGIS enums moved into `Qgis.*`. Every such name is resolved here once -
newest spelling first, legacy spelling second - so the rest of the plugin never uses a
version-specific spelling (tools/enum_scan.py checks that). Same approach as ND Dam Hazard's
compat.py and the Runner's compat.py.
"""
from qgis.core import (Qgis, QgsField, QgsProcessing, QgsProcessingParameterFile,
                       QgsProcessingParameterNumber, QgsVectorFileWriter, QgsWkbTypes)

_MISSING = object()


def _first(*getters, **kw):
    """Value of the first getter that works; `default=` when none does, else AttributeError."""
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


def _core(name):
    return getattr(__import__("qgis.core", fromlist=[name]), name)


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

# --- Processing parameters ----------------------------------------------------------------
NUM_DOUBLE = _first(lambda: Qgis.ProcessingNumberParameterType.Double,
                    lambda: QgsProcessingParameterNumber.Type.Double,
                    lambda: QgsProcessingParameterNumber.Double)
NUM_INT = _first(lambda: Qgis.ProcessingNumberParameterType.Integer,
                 lambda: QgsProcessingParameterNumber.Type.Integer,
                 lambda: QgsProcessingParameterNumber.Integer)
FILE_FILE = _first(lambda: Qgis.ProcessingFileParameterBehavior.File,
                   lambda: QgsProcessingParameterFile.Behavior.File,
                   lambda: QgsProcessingParameterFile.File)
FILE_FOLDER = _first(lambda: Qgis.ProcessingFileParameterBehavior.Folder,
                     lambda: QgsProcessingParameterFile.Behavior.Folder,
                     lambda: QgsProcessingParameterFile.Folder)
SRC_POINT = _first(lambda: Qgis.ProcessingSourceType.VectorPoint,
                   lambda: QgsProcessing.SourceType.TypeVectorPoint,
                   lambda: QgsProcessing.TypeVectorPoint)
SRC_LINE = _first(lambda: Qgis.ProcessingSourceType.VectorLine,
                  lambda: QgsProcessing.SourceType.TypeVectorLine,
                  lambda: QgsProcessing.TypeVectorLine)
SRC_POLYGON = _first(lambda: Qgis.ProcessingSourceType.VectorPolygon,
                     lambda: QgsProcessing.SourceType.TypeVectorPolygon,
                     lambda: QgsProcessing.TypeVectorPolygon)
FIELD_ANY = _first(lambda: Qgis.ProcessingFieldParameterDataType.Any,
                   lambda: _core("QgsProcessingParameterField").DataType.Any,
                   lambda: _core("QgsProcessingParameterField").Any)
FIELD_NUMERIC = _first(lambda: Qgis.ProcessingFieldParameterDataType.Numeric,
                       lambda: _core("QgsProcessingParameterField").DataType.Numeric,
                       lambda: _core("QgsProcessingParameterField").Numeric)
FLAG_ADVANCED = _first(lambda: Qgis.ProcessingParameterFlag.Advanced,
                       lambda: _core("QgsProcessingParameterDefinition").Flag.FlagAdvanced,
                       lambda: _core("QgsProcessingParameterDefinition").FlagAdvanced,
                       default=None)


def set_advanced(param):
    """Mark a Processing parameter as advanced (no-op where the flag is unknown)."""
    if FLAG_ADVANCED is not None:
        try:
            param.setFlags(param.flags() | FLAG_ADVANCED)
        except Exception:                                        # noqa
            pass
    return param


# --- geometry ------------------------------------------------------------------------------
GEOM_POINT = _first(lambda: Qgis.GeometryType.Point,
                    lambda: QgsWkbTypes.GeometryType.PointGeometry,
                    lambda: QgsWkbTypes.PointGeometry)
GEOM_LINE = _first(lambda: Qgis.GeometryType.Line,
                   lambda: QgsWkbTypes.GeometryType.LineGeometry,
                   lambda: QgsWkbTypes.LineGeometry)
GEOM_POLYGON = _first(lambda: Qgis.GeometryType.Polygon,
                      lambda: QgsWkbTypes.GeometryType.PolygonGeometry,
                      lambda: QgsWkbTypes.PolygonGeometry)
WKB_POINT = _first(lambda: Qgis.WkbType.Point, lambda: QgsWkbTypes.Type.Point,
                   lambda: QgsWkbTypes.Point)
WKB_LINESTRING = _first(lambda: Qgis.WkbType.LineString, lambda: QgsWkbTypes.Type.LineString,
                        lambda: QgsWkbTypes.LineString)
WKB_POLYGON = _first(lambda: Qgis.WkbType.Polygon, lambda: QgsWkbTypes.Type.Polygon,
                     lambda: QgsWkbTypes.Polygon, default=None)

# --- vector file writer ----------------------------------------------------------------------
WRITER_NO_ERROR = _first(lambda: QgsVectorFileWriter.WriterError.NoError,
                         lambda: QgsVectorFileWriter.NoError)
WRITER_OVERWRITE_FILE = _first(
    lambda: QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile,
    lambda: QgsVectorFileWriter.CreateOrOverwriteFile)
WRITER_OVERWRITE_LAYER = _first(
    lambda: QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer,
    lambda: QgsVectorFileWriter.CreateOrOverwriteLayer)


# --- field types ----------------------------------------------------------------------------
def _metatype_types():
    """QMetaType.Type members when this build's QgsField takes them (QGIS >= 3.38, all Qt6
    builds); None otherwise."""
    try:
        from qgis.PyQt.QtCore import QMetaType
    except ImportError:
        return None
    T = getattr(QMetaType, "Type", QMetaType)

    def m(name):
        return _first(lambda: getattr(T, name), lambda: getattr(QMetaType, name))
    try:
        types = {"Int": m("Int"), "LongLong": m("LongLong"), "Double": m("Double"),
                 "String": m("QString"), "Bool": m("Bool")}
        probe = QgsField("probe", types["Double"])
        if enum_value(probe.type()) != enum_value(types["Double"]):
            return None
        return types
    except Exception:
        return None


def _qvariant_types():
    from qgis.PyQt.QtCore import QVariant
    T = getattr(QVariant, "Type", QVariant)

    def v(name):
        return _first(lambda: getattr(T, name), lambda: getattr(QVariant, name))
    return {"Int": v("Int"), "LongLong": v("LongLong"), "Double": v("Double"),
            "String": v("String"), "Bool": v("Bool")}


_FT = _metatype_types()
FIELD_TYPES_ARE_METATYPE = _FT is not None
if _FT is None:
    _FT = _qvariant_types()
FT_INT = _FT["Int"]
FT_LONGLONG = _FT["LongLong"]
FT_DOUBLE = _FT["Double"]
FT_STRING = _FT["String"]
FT_BOOL = _FT["Bool"]
