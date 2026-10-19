"""DEM resampling onto the model grid (core/gdal_io): an explicit parameter beats the
NDBREACH_DEM_RESAMPLE environment variable, which beats DEFAULT_RESAMPLE ("average", the value
the regression baselines use - bilinear ran flooded areas 3-40% high). The method is also offered
as a tool parameter (DEM_RESAMPLE) and a CLI key (dem.resample), a bad value is refused as a clean
QgsProcessingException / CliError (not an unhandled ValueError traceback), and the choice is
recorded in the run manifest (extra.dem_resample). Reverse-ported from the DamSafety_Runner
sibling. These tests stub GDAL, so they run without PyQGIS/GDAL."""
import os
import types

from nd_breach_runner.core import gdal_io, workflow
from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests.test_cli import cli


class _Env:
    """NDBREACH_DEM_RESAMPLE set (or removed, value None) for a with-block."""
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        self.saved = os.environ.get(gdal_io.ENV_RESAMPLE)
        os.environ.pop(gdal_io.ENV_RESAMPLE, None)
        if self.value is not None:
            os.environ[gdal_io.ENV_RESAMPLE] = self.value

    def __exit__(self, *a):
        os.environ.pop(gdal_io.ENV_RESAMPLE, None)
        if self.saved is not None:
            os.environ[gdal_io.ENV_RESAMPLE] = self.saved


def _terrain(resample=None):
    """A GdalTerrain without GDAL: the raster open and z-units lookup are stubbed."""
    saved = (gdal_io._gdal, gdal_io.GS.open_raster, gdal_io.z_to_m_factor)
    band = types.SimpleNamespace(GetNoDataValue=lambda: None)
    gdal_io._gdal = lambda: (None, None)
    gdal_io.GS.open_raster = lambda p: types.SimpleNamespace(GetRasterBand=lambda i: band)
    gdal_io.z_to_m_factor = lambda p, c: (1.0, "metres")
    try:
        return gdal_io.GdalTerrain("dem.tif", "EPSG:26914", resample=resample)
    finally:
        gdal_io._gdal, gdal_io.GS.open_raster, gdal_io.z_to_m_factor = saved


@test
def dem_resample_default_is_average_not_bilinear():
    # the regression this guards against: GdalTerrain used to default to "bilinear".
    assert gdal_io.DEFAULT_RESAMPLE == "average"
    with _Env(None):
        assert gdal_io.resolve_resample() == ("average", "default")
        t = _terrain()                                 # nothing passed, nothing in the environment
        assert (t.resample, t.resample_source) == ("average", "default")


@test
def dem_resample_precedence_param_over_env_over_default():
    with _Env(None):
        assert gdal_io.resolve_resample() == ("average", "default")
        assert _terrain().resample == "average"
    with _Env("average"):
        assert gdal_io.resolve_resample() == ("average", "environment")
        assert gdal_io.resolve_resample("") == ("average", "environment")      # blank = not set
        t = _terrain()
        assert (t.resample, t.resample_source) == ("average", "environment")
        t = _terrain("near")                                                   # explicit wins
        assert (t.resample, t.resample_source) == ("near", "parameter")
    with _Env("bilinear"):
        assert gdal_io.resolve_resample() == ("bilinear", "environment")    # env still overrides
    with _Env("  "):
        assert gdal_io.resolve_resample() == ("average", "default")          # blank env ignored
    assert gdal_io.resolve_resample(None, {"NDBREACH_DEM_RESAMPLE": "Average"}) == \
        ("average", "environment")


@test
def dem_resample_rejects_names_gdal_does_not_have():
    for bad in ("bicubic", "avg", "nearest neighbour"):
        try:
            gdal_io.check_resample(bad)
        except ValueError as e:
            assert "average" in str(e) and repr(bad) in str(e)
        else:
            raise AssertionError(bad)
    with _Env("smooth"):
        try:
            _terrain()
        except ValueError as e:
            assert gdal_io.ENV_RESAMPLE in str(e)
        else:
            raise AssertionError("a bad environment value must not be ignored")
    assert gdal_io.check_resample(" Average ") == "average"
    assert gdal_io.DEFAULT_RESAMPLE in gdal_io.RESAMPLE_METHODS and "average" in gdal_io.RESAMPLE_METHODS


def _algs():
    """nd_breach_runner.algorithms with the fake QGIS installed (so it imports off QGIS)."""
    from nd_breach_runner.tests import fake_qgis
    fake_qgis.install()
    import importlib
    import nd_breach_runner.algorithms as algs
    importlib.reload(algs)
    return algs, fake_qgis


@test
def dem_resample_tool_parameter_maps_to_gdal_names():
    algs, _ = _algs()
    # the "Automatic" choice (index 0) and an empty value mean "resolve from env / default".
    assert algs.dem_resample_param(0) is None and algs.dem_resample_param(None) is None
    for i, name in enumerate(gdal_io.RESAMPLE_METHODS, start=1):
        assert algs.dem_resample_param(i) == name
    assert algs.dem_resample_param(len(gdal_io.RESAMPLE_METHODS) + 1) is None
    # the parameter lands in the model parameters, advanced, defaulting to "Automatic" (index 0).
    alg = algs._Base()
    alg.add_model_params()
    p = alg.parameterDefinition("DEM_RESAMPLE")
    assert p is not None and p.defaultValue() == 0
    assert p.options()[0] == algs.DEM_RESAMPLE_AUTO and list(p.options()[1:]) == list(
        gdal_io.RESAMPLE_METHODS)
    src = open(algs.__file__, encoding="utf-8").read()
    assert '"DEM_RESAMPLE"' in src and "resample_source" in src


@test
def dem_resample_tool_default_is_average_and_explicit_overrides_env():
    algs, _ = _algs()
    assert gdal_io.DEFAULT_RESAMPLE == "average"
    with _Env(None):
        # nothing chosen (the "Automatic" default, index 0) -> "average" from DEFAULT_RESAMPLE.
        t = _terrain(algs.dem_resample_param(0))
        assert (t.resample, t.resample_source) == ("average", "default")
    with _Env("bilinear"):
        # an explicit tool choice beats the NDBREACH_DEM_RESAMPLE environment variable.
        idx = list(gdal_io.RESAMPLE_METHODS).index("near") + 1
        t = _terrain(algs.dem_resample_param(idx))
        assert (t.resample, t.resample_source) == ("near", "parameter")
        # "Automatic" with the env set -> the env value, not the default.
        t = _terrain(algs.dem_resample_param(0))
        assert (t.resample, t.resample_source) == ("bilinear", "environment")


@test
def dem_resample_bad_value_raises_qgsprocessingexception_not_valueerror():
    algs, fake_qgis = _algs()
    alg = algs._Base()
    alg.add_model_params()                         # DEM_RESAMPLE, WORK_CRS, MANNING_RASTER, ...
    crs = fake_qgis.QgsCoordinateReferenceSystem("EPSG:26914")
    dem = types.SimpleNamespace(source=lambda: "dem.tif", crs=lambda: crs)
    params = {"DEM": dem, "WORK_CRS": crs}         # "Automatic" -> resolve from the env
    ctx = fake_qgis.QgsProcessingContext()
    saved = (gdal_io._gdal, gdal_io.z_to_m_factor, gdal_io.linear_unit_m)
    gdal_io._gdal = lambda: (None, None)
    gdal_io.z_to_m_factor = lambda p, c: (1.0, "metres")
    gdal_io.linear_unit_m = lambda wkt: 1.0
    try:
        with _Env("smooth"):                       # a bad NDBREACH_DEM_RESAMPLE value
            try:
                alg.gis(params, ctx, crs, fake_qgis.QgsPointXY(0.0, 0.0))
            except fake_qgis.QgsProcessingException as e:
                assert gdal_io.ENV_RESAMPLE in str(e)        # a clean, named message
            except ValueError:
                raise AssertionError("ValueError leaked; gis() must raise QgsProcessingException")
            else:
                raise AssertionError("a bad NDBREACH_DEM_RESAMPLE must be refused")
    finally:
        gdal_io._gdal, gdal_io.z_to_m_factor, gdal_io.linear_unit_m = saved


@test
def dem_resample_cli_key_is_validated_and_in_the_template():
    c = cli()
    assert c.SECTIONS["dem"]["resample"] == ("enum", gdal_io.RESAMPLE_METHODS)
    assert "resample" in c.TEMPLATE["dem"] and c.TEMPLATE["dem"]["resample"] is None
    assert c._check_value("dem.resample", "average", c.SECTIONS["dem"]["resample"]) == "average"
    try:
        c._check_value("dem.resample", "bicubic", c.SECTIONS["dem"]["resample"])
    except c.CliError as e:
        assert "average" in str(e)
    else:
        raise AssertionError("bicubic accepted")
    src = open(c.__file__, encoding="utf-8").read()
    assert "else average" in src and "else bilinear" not in src


@test
def dem_resample_recorded_in_the_manifest_extra():
    t = _terrain("average")
    rs = workflow._dem_resample_of(t)
    assert rs == {"method": "average", "source": "parameter"}
    wrapped = types.SimpleNamespace(base=t)                   # e.g. ConditionedTerrain
    assert workflow._dem_resample_of(wrapped) == rs
    assert workflow._dem_resample_of(types.SimpleNamespace()) is None   # ASCII / fake terrain
    report = {"settings": {}, "dem_resample": rs}
    extra = workflow._manifest_extra(report, {"final_model": "lisflood"}, [])
    assert extra["dem_resample"] == {"method": "average", "source": "parameter"}
    assert "dem_resample" not in workflow._manifest_extra({"settings": {}}, {"final_model": "x"}, [])
    # "Automatic" with nothing set -> recorded as the default.
    with _Env(None):
        d = workflow._dem_resample_of(_terrain(None))
        rep = {"settings": {}, "dem_resample": d}
        assert workflow._manifest_extra(rep, {"final_model": "lisflood"}, [])["dem_resample"] == \
            {"method": "average", "source": "default"}
