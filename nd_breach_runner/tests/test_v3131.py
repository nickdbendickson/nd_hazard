"""Tests of 3.13.1: the county outlines (core.nd_counties, the same file as ND Dam Breach
Screening's) and the wizard's county filter for the dam list."""
import filecmp
import os

from nd_breach_runner.core import nd_counties as cty
from nd_breach_runner.tests.registry import test

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)


@test
def counties_outlines():
    assert len(cty.names()) == 53 and "Eddy" in cty.names()
    assert cty.county_of(-96.79, 46.88) == "Cass"            # Fargo
    assert cty.county_of(-100.78, 46.81) == "Burleigh"       # Bismarck
    assert cty.county_of(-98.90, 47.75) == "Eddy"            # New Rockford
    assert cty.county_of(-90.0, 40.0) == ""
    assert cty.normalize("CASS county") == "Cass" and cty.normalize("") == ""
    assert cty.parse("Eddy,Cass") == ["Eddy", "Cass"]


@test
def counties_same_file_as_screening():
    """When ND Dam Breach Screening sits beside this plugin, its copy is the same."""
    other = os.path.join(os.path.dirname(PLUGIN), "nd_dam_screening", "core")
    if not os.path.isdir(other):
        return
    for n in ("nd_counties.py", "nd_counties.geojson"):
        assert filecmp.cmp(os.path.join(PLUGIN, "core", n), os.path.join(other, n), shallow=False), n


class _Pt:
    def __init__(self, x, y):
        self._x, self._y = x, y

    def x(self):
        return self._x

    def y(self):
        return self._y


class _Geom:
    def __init__(self, x, y):
        self.x, self.y = x, y

    def isEmpty(self):
        return False

    def transform(self, _tr):
        pass

    def centroid(self):
        return self

    def asPoint(self):
        return _Pt(self.x, self.y)


class _Field:
    def __init__(self, n):
        self._n = n

    def name(self):
        return self._n


class _Feat(dict):
    def __init__(self, x, y, **attrs):
        dict.__init__(self, attrs)
        self._g = _Geom(x, y)

    def geometry(self):
        return self._g


class _Layer:
    def __init__(self, names):
        self._names = names

    def fields(self):
        return [_Field(n) for n in self._names]

    def crs(self):
        return None


@test
def wizard_county_filter():
    import sys
    import types
    from nd_breach_runner import wizard
    # QGIS stand-ins for the transform (the geometry stand-in ignores it)
    core = types.ModuleType("qgis.core")
    core.QgsCoordinateReferenceSystem = lambda *a: None
    core.QgsCoordinateTransform = lambda *a: None
    core.QgsGeometry = lambda g: g

    class _P:
        @staticmethod
        def instance():
            return None
    core.QgsProject = _P
    old = {k: sys.modules.get(k) for k in ("qgis", "qgis.core")}
    sys.modules["qgis"] = sys.modules.get("qgis") or types.ModuleType("qgis")
    sys.modules["qgis.core"] = core
    try:
        lay = _Layer(["structure_id", "county"])
        keep = wizard._county_test(lay, "Eddy", {})
        assert keep(_Feat(-98.90, 47.75, county="Eddy"))
        assert not keep(_Feat(-96.79, 46.88, county="Eddy"))    # the point wins over the field
        assert keep(_Feat(-90.0, 40.0, county="Eddy County"))   # outside every outline: the field
        assert not keep(_Feat(-90.0, 40.0, county=""))
        assert wizard._county_test(lay, "", {})(_Feat(-90.0, 40.0))   # no county: every dam
    finally:
        for k, v in old.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
