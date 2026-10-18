"""DEM resampling onto the model grid (core/gdal_io): an explicit parameter beats the
NDBREACH_DEM_RESAMPLE environment variable, which beats DEFAULT_RESAMPLE ("average", the value
the regression baselines use - bilinear ran flooded areas 3-40% high). Reverse-ported from the
DamSafety_Runner sibling. These tests stub GDAL, so they run without PyQGIS/GDAL."""
import os
import types

from nd_breach_runner.core import gdal_io
from nd_breach_runner.tests.registry import test


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
