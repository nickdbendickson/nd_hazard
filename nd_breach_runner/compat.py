"""Enum lookups that work from QGIS 3.28 (Qt5) to QGIS 4 (Qt6).

Newer QGIS moved many enums into `Qgis.*` and removed the old flat names in
Qt6 builds. Each lookup tries the newest location first."""
from qgis.core import (Qgis, QgsProcessing, QgsProcessingAlgorithm,
                       QgsProcessingParameterDefinition, QgsProcessingParameterFile,
                       QgsProcessingParameterNumber)


def _first(*getters, default=None):
    for g in getters:
        try:
            v = g()
        except Exception:
            continue
        if v is not None:
            return v
    return default


NUM_DOUBLE = _first(lambda: Qgis.ProcessingNumberParameterType.Double,
                    lambda: QgsProcessingParameterNumber.Double)
NUM_INT = _first(lambda: Qgis.ProcessingNumberParameterType.Integer,
                 lambda: QgsProcessingParameterNumber.Integer)
FILE_FILE = _first(lambda: Qgis.ProcessingFileParameterBehavior.File,
                   lambda: QgsProcessingParameterFile.File)
FILE_FOLDER = _first(lambda: Qgis.ProcessingFileParameterBehavior.Folder,
                     lambda: QgsProcessingParameterFile.Folder)
FLAG_ADVANCED = _first(lambda: Qgis.ProcessingParameterFlag.Advanced,
                       lambda: QgsProcessingParameterDefinition.FlagAdvanced)
SOURCE_POINT = _first(lambda: Qgis.ProcessingSourceType.VectorPoint,
                      lambda: QgsProcessing.TypeVectorPoint)
SOURCE_LINE = _first(lambda: Qgis.ProcessingSourceType.VectorLine,
                     lambda: QgsProcessing.TypeVectorLine)
SOURCE_POLYGON = _first(lambda: Qgis.ProcessingSourceType.VectorPolygon,
                        lambda: QgsProcessing.TypeVectorPolygon)
FIELD_NUMERIC = _first(lambda: Qgis.ProcessingFieldParameterDataType.Numeric,
                       lambda: __import__("qgis.core", fromlist=["x"])
                       .QgsProcessingParameterField.Numeric)
FIELD_ANY = _first(lambda: Qgis.ProcessingFieldParameterDataType.Any,
                   lambda: __import__("qgis.core", fromlist=["x"])
                   .QgsProcessingParameterField.Any)
ALG_NO_THREADING = _first(lambda: Qgis.ProcessingAlgorithmFlag.NoThreading,
                          lambda: QgsProcessingAlgorithm.FlagNoThreading)


def set_advanced(param):
    param.setFlags(param.flags() | FLAG_ADVANCED)
    return param

# message levels (QgsMessageLog, the message bar)
MSG_INFO = _first(lambda: Qgis.MessageLevel.Info, lambda: Qgis.Info, default=0)
MSG_WARNING = _first(lambda: Qgis.MessageLevel.Warning, lambda: Qgis.Warning, default=1)
MSG_CRITICAL = _first(lambda: Qgis.MessageLevel.Critical, lambda: Qgis.Critical, default=2)
