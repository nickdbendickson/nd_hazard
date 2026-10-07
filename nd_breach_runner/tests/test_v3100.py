"""Tests of 3.10.0's coordinate-system fixes: DEM elevation units in a feet CRS, flooded road
lengths in the rasters' own units, and the command line's transformation cache."""
import os
import shutil
import tempfile
import types

import numpy as np

from nd_breach_runner.core import gdal_io, impacts
from nd_breach_runner.core.units import FT
from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests.test_cli import cli


def _has_gdal():
    try:
        from osgeo import gdal, osr  # noqa: F401
        return True
    except ImportError:
        return False


def _dem(path, z, epsg, unit=None):
    from osgeo import gdal, osr
    a = np.asarray(z, dtype=np.float32)
    ds = gdal.GetDriverByName("GTiff").Create(path, a.shape[1], a.shape[0], 1, gdal.GDT_Float32)
    ds.SetGeoTransform((1900000.0, 30.0, 0, 500000.0, 0, -30.0))
    s = osr.SpatialReference()
    s.ImportFromEPSG(epsg)
    ds.SetProjection(s.ExportToWkt())
    b = ds.GetRasterBand(1)
    if unit:
        b.SetUnitType(unit)
    b.WriteArray(a)
    ds = None


@test
def feet_crs_with_metre_elevations_reads_metres():
    """A 3DEP metre DEM warped to ND State Plane South (feet) with no unit tag: the elevations
    (about 500-560) only fit North Dakota as metres, so they stay metres."""
    if not _has_gdal():
        return
    d = tempfile.mkdtemp()
    try:
        ramp = np.linspace(0, 1, 40)[None, :] * np.ones((30, 1))
        cases = (("m_in_ft_crs.tif", 500 + 60 * ramp, None, 1.0, "below North Dakota"),
                 ("ft_in_ft_crs.tif", 1650 + 200 * ramp, None, FT, "only as feet"),
                 ("ambiguous.tif", 880 + 40 * ramp, None, FT, "fit both"),
                 ("tagged_m.tif", 1650 + 200 * ramp, "m", 1.0, "raster unit"))
        for name, z, unit, want, words in cases:
            p = os.path.join(d, name)
            _dem(p, z, 2266, unit)
            f, note = gdal_io.z_to_m_factor(p, gdal_io.Z_AUTO)
            assert abs(f - want) < 1e-12 and words in note, (name, f, note)
        z = 500 + 60 * ramp
        z[0, 0] = -9999.0
        p = os.path.join(d, "nodata.tif")
        _dem(p, z, 2266)
        assert gdal_io.z_to_m_factor(p)[0] == 1.0
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def road_lengths_are_metres_in_a_feet_crs():
    """The same 1 km road in metres and in feet coordinates gives the same length and about
    the same number of sample points."""
    rm, rf = impacts.Receptors(), impacts.Receptors()
    line_m = np.array([[0.0, 0.0], [1000.0, 0.0]])
    rm.add_lines([line_m], ids=["r"])
    rf.add_lines([line_m / FT], ids=["r"], unit_m=FT)
    assert abs(rm.length_m.sum() - rf.length_m.sum()) < 1.0, (rm.length_m.sum(),
                                                             rf.length_m.sum())
    assert abs(len(rm) - len(rf)) <= 1, (len(rm), len(rf))


@test
def haversine_ground_distance():
    """The ground distance _ground_m_per_unit measures with: one degree of latitude."""
    from nd_breach_runner import algorithms  # noqa: F401  (loads algorithms_consequences)
    from nd_breach_runner import algorithms_consequences as ac
    d = ac._haversine_m(-100.0, 47.0, -100.0, 48.0)
    assert abs(d - 111195.0) < 50.0, d


@test
def cli_transform_cache_follows_the_crs_not_the_object():
    """The cache keeps each SRS alive, so an SRS for another CRS made after the first is freed
    can never take over its id() and its transformation."""
    if not _has_gdal():
        return
    from osgeo import osr
    mod = cli()
    frame = types.SimpleNamespace(_cts={}, osr=osr)
    utm = osr.SpatialReference()
    utm.ImportFromEPSG(26914)
    ll = osr.SpatialReference()
    ll.ImportFromEPSG(4326)
    ll.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    sp = osr.SpatialReference()
    sp.ImportFromEPSG(2266)
    t1 = mod.Frame._ct(frame, ll, utm)
    assert mod.Frame._ct(frame, ll, utm) is t1
    del ll                                        # the cache still holds it
    t2 = mod.Frame._ct(frame, sp, utm)
    assert t2 is not t1 and len(frame._cts) == 2
    x, y, _ = t2.TransformPoint(1900000.0, 500000.0)
    assert 300000 < x < 700000 and 5000000 < y < 5300000, (x, y)


@test
def executable_parameters_accept_names_without_an_extension():
    """Every file parameter without an extension (LISFLOOD-FP, ANUGA python) has the filter
    'All files (*)'; QGIS's default 'All files (*.*)' refuses /opt/lisflood/lisflood."""
    from nd_breach_runner import algorithms
    seen = 0
    for cls in algorithms.ALGORITHMS:
        alg = cls()
        alg.initAlgorithm()
        for p in alg.parameterDefinitions():
            if p.name() in ("LISFLOOD_EXE", "ANUGA_PYTHON"):
                ff = p.fileFilter() if hasattr(p, "fileFilter") else p.kwargs.get("fileFilter")
                assert ff == "All files (*)", (alg.name(), p.name(), ff)
                seen += 1
    assert seen >= 6, seen
