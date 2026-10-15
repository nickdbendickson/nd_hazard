"""Just enough of qgis.core / qgis.PyQt to import and exercise the plugin's
Processing code without QGIS. Parameter reading goes through the same
parameterAs* calls the algorithms use, backed by a plain dict."""
import sys
import types


class _Any:
    def __init__(self, *a, **k):
        self.args, self.kwargs = a, k

    def __getattr__(self, name):
        return _Any()

    def __call__(self, *a, **k):
        return _Any()


class _EnumNS:
    def __getattr__(self, name):
        return name


class Qgis:
    ProcessingNumberParameterType = _EnumNS()
    ProcessingFileParameterBehavior = _EnumNS()
    ProcessingParameterFlag = types.SimpleNamespace(Advanced=8, Hidden=16)
    ProcessingSourceType = _EnumNS()
    ProcessingFieldParameterDataType = _EnumNS()
    ProcessingAlgorithmFlag = _EnumNS()


class QgsProcessingException(Exception):
    pass


class _Param:
    def __init__(self, name, description="", *a, **k):
        self._name = name
        self.description = description
        self.args, self.kwargs = a, k
        self._flags = 0

    def name(self):
        return self._name

    def flags(self):
        return self._flags

    def setFlags(self, f):
        self._flags = f

    def setMinimum(self, v):
        self.min = v

    def setMaximum(self, v):
        self.max = v

    # 3.5: the suite settings file sets defaults (suite_settings_tools reads these like QGIS)
    def defaultValue(self):
        return self.kwargs.get("defaultValue")

    def setDefaultValue(self, v):
        self.kwargs["defaultValue"] = v

    def isDestination(self):
        return type(self).__name__.endswith(("Sink", "Destination"))

    def options(self):
        return list(self.args[0]) if self.args and isinstance(self.args[0], (list, tuple)) else []

    def minimum(self):
        return getattr(self, "min", None)

    def maximum(self):
        return getattr(self, "max", None)


class QgsSettings:
    """A process-wide dict (the real one is the QGIS profile's settings)."""
    _store = {}

    def value(self, key, default=None):
        return QgsSettings._store.get(key, default)

    def setValue(self, key, v):
        QgsSettings._store[key] = v

    def remove(self, key):
        QgsSettings._store.pop(key, None)


def _param_class(n):
    return type(n, (_Param,), {})


class QgsProcessingAlgorithm:
    FlagNoThreading = 4

    def __init__(self):
        self.params = {}
        self.outputs = {}

    def addParameter(self, p):
        assert p.name() not in self.params, "duplicate parameter " + p.name()
        self.params[p.name()] = p
        return True

    def flags(self):
        return 0

    def parameterDefinition(self, name):
        return self.params.get(name)

    def parameterDefinitions(self):
        return list(self.params.values())

    def addOutput(self, o):
        self.outputs[o.name()] = o

    # parameters are stored already converted
    def parameterAsDouble(self, parameters, name, context):
        v = parameters.get(name, self._default(name))
        return float(v) if v not in (None, "") else 0.0

    def parameterAsInt(self, parameters, name, context):
        return int(self.parameterAsDouble(parameters, name, context))

    def parameterAsEnum(self, parameters, name, context):
        v = parameters.get(name, self._default(name))
        return int(v or 0)

    def parameterAsBool(self, parameters, name, context):
        return bool(parameters.get(name, self._default(name)))

    def parameterAsString(self, parameters, name, context):
        v = parameters.get(name, self._default(name))
        return "" if v is None else str(v)

    parameterAsFile = parameterAsString
    parameterAsFileOutput = parameterAsString

    def parameterAsPoint(self, parameters, name, context, crs=None):
        return parameters.get(name)

    def parameterAsRasterLayer(self, parameters, name, context):
        return parameters.get(name)

    def parameterAsCrs(self, parameters, name, context):
        return parameters.get(name) or QgsCoordinateReferenceSystem("")

    def parameterAsSource(self, parameters, name, context):
        return parameters.get(name)

    def parameterAsExtent(self, parameters, name, context, crs=None):
        return parameters.get(name)

    def parameterAsFields(self, parameters, name, context):
        return list(parameters.get(name) or [])

    def _default(self, name):
        p = self.params.get(name)
        return None if p is None else p.kwargs.get("defaultValue")


class QgsProcessingParameterNumber(_Param):
    Double, Integer = "Double", "Integer"

    def __init__(self, name, description="", type=None, defaultValue=None, optional=False,
                 **k):
        _Param.__init__(self, name, description, type, defaultValue=defaultValue,
                        optional=optional)


class QgsProcessingParameterFile(_Param):
    File, Folder = "File", "Folder"


class QgsProcessingParameterEnum(_Param):
    def __init__(self, name, description="", options=None, allowMultiple=False,
                 defaultValue=None, **k):
        _Param.__init__(self, name, description, options, defaultValue=defaultValue)


class QgsProcessingParameterBoolean(_Param):
    def __init__(self, name, description="", defaultValue=None, **k):
        _Param.__init__(self, name, description, defaultValue=defaultValue)


class QgsProcessingParameterString(_Param):
    def __init__(self, name, description="", defaultValue=None, **k):
        _Param.__init__(self, name, description, defaultValue=defaultValue)


class QgsProcessingParameterDefinition:
    FlagAdvanced = 8
    FlagHidden = 16


class QgsProcessing:
    TypeVectorPoint = 0
    TypeVectorLine = 1
    TypeVectorPolygon = 2


class QgsProcessingContext:
    class LayerDetails:
        def __init__(self, *a):
            self.args = a

    def __init__(self):
        self.loaded = []

    def addLayerToLoadOnCompletion(self, path, details):
        self.loaded.append(path)

    def project(self):
        return None

    def transformContext(self):
        return None


class QgsProcessingFeedback:
    def __init__(self):
        self.info, self.errors, self.progress = [], [], 0.0

    def setProgress(self, p):
        self.progress = p

    def setProgressText(self, t):
        pass

    def pushInfo(self, m):
        self.info.append(m)

    def pushWarning(self, m):
        self.errors.append(str(m))              # the tests read warnings from .errors

    def pushCommandInfo(self, m):
        self.info.append(m)

    def reportError(self, m, fatalError=False):
        self.errors.append(m)

    def isCanceled(self):
        return False


class QgsPointXY:
    def __init__(self, x=0.0, y=0.0):
        if isinstance(x, QgsPointXY):
            x, y = x.x(), x.y()
        self._x, self._y = float(x), float(y)

    def x(self):
        return self._x

    def y(self):
        return self._y


class QgsCoordinateReferenceSystem:
    def __init__(self, s=""):
        self.s = s

    def isValid(self):
        return bool(self.s)

    def authid(self):
        return self.s

    def toWkt(self):
        return self.s


class QgsRectangle:
    def __init__(self, x0=0.0, y0=0.0, x1=0.0, y1=0.0):
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1

    def intersects(self, o):
        return not (o.x0 > self.x1 or o.x1 < self.x0 or o.y0 > self.y1 or o.y1 < self.y0)


class QgsFeatureRequest:
    def __init__(self):
        self.rect = None

    def setFilterRect(self, r):
        self.rect = r
        return self


class FakeGeometry:
    """kind: point / line / polygon; parts: list of coordinate lists."""

    def __init__(self, kind, parts, multipart=False):
        self.kind, self.parts, self.multi = kind, parts, multipart

    def isEmpty(self):
        return not self.parts

    def isMultipart(self):
        return self.multi

    def type(self):
        """QgsWkbTypes.GeometryType: 0 point, 1 line, 2 polygon."""
        return {"point": 0, "line": 1, "polygon": 2}.get(self.kind, 3)

    def asPoint(self):
        return QgsPointXY(*self.parts[0][0])

    def asMultiPoint(self):
        return [QgsPointXY(*p[0]) for p in self.parts]

    def asPolyline(self):
        return [QgsPointXY(*p) for p in self.parts[0]]

    def asMultiPolyline(self):
        return [[QgsPointXY(*p) for p in part] for part in self.parts]

    def asPolygon(self):
        return [[QgsPointXY(*p) for p in self.parts[0]]]

    def asMultiPolygon(self):
        return [[[QgsPointXY(*p) for p in part]] for part in self.parts]

    def centroid(self):
        pts = [p for part in self.parts for p in part]
        g = FakeGeometry("point", [[(sum(p[0] for p in pts) / len(pts),
                                     sum(p[1] for p in pts) / len(pts))]])
        return g

    def bbox(self):
        pts = [p for part in self.parts for p in part]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return QgsRectangle(min(xs), min(ys), max(xs), max(ys))


class FakeFeature:
    _n = 0

    def __init__(self, geom, **attrs):
        FakeFeature._n += 1
        self._id = FakeFeature._n
        self._g, self.attrs = geom, attrs

    def id(self):
        return self._id

    def geometry(self):
        return self._g

    def __getitem__(self, k):
        if k not in self.attrs:
            raise KeyError(k)
        return self.attrs[k]


class FakeFields:
    def __init__(self, names):
        self._n = names

    def names(self):
        return list(self._n)


class FakeSource:
    def __init__(self, features, names=None, source_name=""):
        self.feats = features
        self._names = names or sorted({k for f in features for k in f.attrs})
        self._source_name = source_name

    def sourceName(self):
        return self._source_name

    def getFeatures(self, request=None):
        if request is None or request.rect is None:
            return iter(self.feats)
        return iter([f for f in self.feats if request.rect.intersects(f.geometry().bbox())])

    def fields(self):
        return FakeFields(self._names)

    def sourceCrs(self):
        return QgsCoordinateReferenceSystem("EPSG:26914")

    def featureCount(self):
        return len(self.feats)


class QgsCoordinateTransform:
    """Identity transform (tests use one CRS)."""

    def __init__(self, *a):
        pass

    def transform(self, p):
        return QgsPointXY(p)

    def transformBoundingBox(self, r):
        return r


class QgsProcessingProvider:
    def icon(self):
        return None

    def refreshAlgorithms(self):
        pass

    def addAlgorithm(self, a):
        pass


def install():
    core = types.ModuleType("qgis.core")
    names = dict(Qgis=Qgis, QgsProcessingException=QgsProcessingException,
                 QgsProcessingAlgorithm=QgsProcessingAlgorithm,
                 QgsProcessingParameterNumber=QgsProcessingParameterNumber,
                 QgsProcessingParameterFile=QgsProcessingParameterFile,
                 QgsProcessingParameterEnum=QgsProcessingParameterEnum,
                 QgsProcessingParameterBoolean=QgsProcessingParameterBoolean,
                 QgsProcessingParameterString=QgsProcessingParameterString,
                 QgsProcessingParameterDefinition=QgsProcessingParameterDefinition,
                 QgsProcessing=QgsProcessing, QgsProcessingContext=QgsProcessingContext,
                 QgsProcessingFeedback=QgsProcessingFeedback, QgsPointXY=QgsPointXY,
                 QgsCoordinateReferenceSystem=QgsCoordinateReferenceSystem,
                 QgsCoordinateTransform=QgsCoordinateTransform,
                 QgsProcessingProvider=QgsProcessingProvider, QgsApplication=_Any(),
                 QgsRectangle=QgsRectangle, QgsFeatureRequest=QgsFeatureRequest)
    for n in ("QgsProcessingOutputFile", "QgsProcessingOutputFolder",
              "QgsProcessingOutputRasterLayer", "QgsProcessingOutputString",
              "QgsProcessingParameterCrs", "QgsProcessingParameterFeatureSource",
              "QgsProcessingParameterField", "QgsProcessingParameterFileDestination",
              "QgsProcessingParameterPoint", "QgsProcessingParameterRasterLayer",
              "QgsProcessingParameterRasterDestination", "QgsProcessingParameterExtent",
              "QgsProcessingParameterFolderDestination", "QgsProcessingParameterVectorLayer",
              "QgsProcessingOutputNumber", "QgsProcessingOutputVectorLayer"):
        names[n] = _param_class(n)
    names["QgsProcessingParameterField"].Numeric = "Numeric"
    names["QgsProcessingParameterField"].Any = "Any"
    for k, v in names.items():
        setattr(core, k, v)
    qgis = types.ModuleType("qgis")
    qgis.core = core
    pyqt = types.ModuleType("qgis.PyQt")
    for sub in ("QtCore", "QtGui", "QtWidgets"):
        m = types.ModuleType("qgis.PyQt." + sub)
        for n in ("QIcon", "QUrl", "QDesktopServices", "QAction"):
            setattr(m, n, _Any)
        setattr(pyqt, sub, m)
        sys.modules["qgis.PyQt." + sub] = m
    qgis.PyQt = pyqt
    sys.modules.update({"qgis": qgis, "qgis.core": core, "qgis.PyQt": pyqt})
