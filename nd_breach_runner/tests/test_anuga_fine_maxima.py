"""Tiny synthetic mesh only; never import/run ANUGA or another hydraulic model."""
import ast
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from nd_breach_runner.core import anuga_fine_maxima as F
from nd_breach_runner.core import anuga_model as A
from nd_breach_runner.core import workflow as W
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.tests.registry import test


def _case(directory, support=False, fine=False):
    nodes = np.array([[0, 0], [4, 0], [0, 3], [4, 3]], float)
    domain = SimpleNamespace(nodes=nodes, triangles=np.array([[0, 1, 2], [1, 3, 2]]))
    env = SimpleNamespace(hmax=np.array([2., 5.]), vmax=np.array([.3, 1.2]),
                          wmax=np.array([102., -np.inf]))
    job = dict(keep_fine_maxima=True, keep_fine_support=support,
               dem=np.zeros((3, 5), np.float32), dem_x0=500000., dem_y0=5200003., dem_dx=1.,
               fine_maxima_crs='')
    if fine:
        np.save(Path(directory)/'anuga_elevation_fine.npy', np.ones((3, 5), np.float32))
        job.update(elev_fine_file='anuga_elevation_fine.npy', elev_fine_x0=500000.,
                   elev_fine_y0=5200003., elev_fine_dx=1.)
        job['dem_dx'] = 10.0        # export must not use this coarse cell size
    return job, domain, env


@test
def fine_maxima_defaults_off_and_processing_controls_exposed():
    st, ast_ = W.RunSettings(), A.AnugaSettings('unused')
    assert st.anuga_keep_fine_maxima is False and st.anuga_keep_fine_support is False
    assert ast_.keep_fine_maxima is False and ast_.keep_fine_support is False
    st = W.RunSettings(anuga_keep_fine_maxima=True, anuga_keep_fine_support=True)
    assert st.anuga_keep_fine_maxima and st.anuga_keep_fine_support
    # Controls feed the same settings catalogue as the existing ANUGA options.
    src = (Path(__file__).parents[1]/'algorithms.py').read_text()
    tree = ast.parse(src)
    defs = {n.args[0].value: n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == '_bool' and n.args and isinstance(n.args[0], ast.Constant)}
    for key in ('ANUGA_KEEP_FINE_MAXIMA', 'ANUGA_KEEP_FINE_SUPPORT'):
        assert defs[key].args[2].value is False
        assert 'self.parameterAsBool(parameters, "'+key+'", context)' in src


@test
def fine_maxima_job_keys_are_opt_in():
    with tempfile.TemporaryDirectory() as d:
        dem = Grid(np.ones((2, 2)), 10., 20., 1.)
        dem.crs_wkt = 'example CRS'
        poly = np.array([[10, 18], [12, 18], [12, 20], [10, 20]], float)
        hyd = SimpleNamespace(t_s=np.array([0., 1.]), q_cms=np.array([0., 0.]))
        st = A.AnugaSettings('unused')
        for enabled in (False, True):
            st.keep_fine_maxima, st.keep_fine_support = enabled, True
            A.write_job(str(Path(d)/'job.npz'), dem, poly, poly[:0], poly[:2], hyd, st, dem)
            with np.load(Path(d)/'job.npz') as data:
                assert ('keep_fine_maxima' in data) == enabled
                assert ('keep_fine_support' in data) == enabled
                if enabled:
                    assert bool(data['keep_fine_support'])
                    assert str(data['fine_maxima_crs']) == 'example CRS'


@test
def fine_maxima_disabled_even_when_support_requested():
    with tempfile.TemporaryDirectory() as d:
        job, dom, env = _case(d, support=True)
        job['keep_fine_maxima'] = False
        with patch.object(F.shutil, 'disk_usage', side_effect=AssertionError('must not check disk')):
            info = F.retain(d, job, dom, env, 500000., 5200000.)
        assert info == {'enabled': False, 'support_kept': False}
        assert not list(Path(d).iterdir())


@test
def fine_maxima_archive_exact_native_values_coordinates_and_fine_grid():
    with tempfile.TemporaryDirectory() as d:
        job, dom, env = _case(d, fine=True)
        with patch.object(F, 'disk_allows', return_value=True):
            info = F.retain(d, job, dom, env, 500000., 5200000.)
        assert info['saved'] and not info['support_kept']
        with np.load(Path(d)/F.ARCHIVE, allow_pickle=False) as data:
            np.testing.assert_array_equal(data['vertices'], dom.nodes+[500000., 5200000.])
            np.testing.assert_array_equal(data['triangles'], dom.triangles)
            np.testing.assert_array_equal(data['max_depth_m'], env.hmax)
            np.testing.assert_array_equal(data['max_velocity_mps'], env.vmax)
            assert data['max_wse_m'][0] == 102. and np.isnan(data['max_wse_m'][1])
            meta = json.loads(str(data['metadata']))
            assert meta['cell_m'] == 1. and meta['nx'] == 5 and meta['ny'] == 3
            assert meta['complete'] and meta['velocity_unit'] == 'm/s'


@test
def fine_maxima_disk_floor_boundary_and_unknown_space():
    amount = F.estimate_bytes(4, 2, 5, 3)
    assert amount >= 4*16+2*48+2*5*3*4
    with patch.object(F.shutil, 'disk_usage', return_value=SimpleNamespace(free=F.DISK_FLOOR+amount)):
        assert F.disk_allows('.', amount)
    with patch.object(F.shutil, 'disk_usage', return_value=SimpleNamespace(free=F.DISK_FLOOR+amount-1)):
        assert not F.disk_allows('.', amount)
    with patch.object(F.shutil, 'disk_usage', side_effect=OSError('unknown')):
        assert not F.disk_allows('.', amount)


@test
def fine_maxima_low_disk_skips_all_optional_retention_and_warns():
    with tempfile.TemporaryDirectory() as d:
        job, dom, env = _case(d, support=True)
        with patch.object(F, 'disk_allows', return_value=False):
            info = F.retain(d, job, dom, env, 500000., 5200000.)
        assert not info['saved'] and not info['support_kept']
        assert '18 GiB' in info['warning'] and not list(Path(d).iterdir())
        messages = []
        F.export_geotiffs(d, info, log=messages.append)
        assert 'WARNING:' in messages[0]


@test
def fine_maxima_support_size_and_fine_terrain_retained():
    with tempfile.TemporaryDirectory() as d:
        job, dom, env = _case(d, support=True)
        ck = Path(d)/'checkpoints'; ck.mkdir()
        (ck/'fake_checkpoint').write_bytes(b'not a model checkpoint')
        assert F.support_size(d, str(ck)) == 22
        with patch.object(F, 'disk_allows', return_value=True):
            info = F.retain(d, job, dom, env, 500000., 5200000., str(ck))
        assert info['saved'] and info['support_kept']
        assert (ck/'fake_checkpoint').read_bytes() == b'not a model checkpoint'
        np.testing.assert_array_equal(np.load(Path(d)/'anuga_elevation_fine.npy'), job['dem'])


@test
def fine_maxima_geotiffs_use_triangle_values_not_coarse_downscaling():
    try:
        from osgeo import gdal, osr
        import matplotlib.tri   # noqa: F401
    except ImportError:
        print("skip: GDAL / matplotlib unavailable")
        return
    with tempfile.TemporaryDirectory() as d:
        job, dom, env = _case(d, fine=True)
        crs = osr.SpatialReference(); crs.ImportFromEPSG(32614)
        job['fine_maxima_crs'] = crs.ExportToWkt()
        with patch.object(F, 'disk_allows', return_value=True):
            info = F.retain(d, job, dom, env, 500000., 5200000.)
            F.export_geotiffs(d, info)
        assert info['geotiffs'] == [F.DEPTH, F.VELOCITY], info
        for name, lower, upper, unit in ((F.DEPTH, 2., 5., 'm'), (F.VELOCITY, .3, 1.2, 'm/s')):
            ds = gdal.Open(str(Path(d)/name)); band = ds.GetRasterBand(1)
            assert ds.RasterXSize == 5 and ds.RasterYSize == 3
            assert ds.GetGeoTransform() == (500000., 1., 0., 5200003., 0., -1.)
            assert band.DataType == gdal.GDT_Float32 and band.GetNoDataValue() == -9999.
            assert band.GetUnitType() == unit and ds.GetProjection()
            arr = band.ReadAsArray()
            assert np.isclose(arr[2, 0], lower) and np.isclose(arr[0, 3], upper), arr
            assert np.all(arr[:, 4] == -9999.), arr
            band = None; ds = None


@test
def fine_maxima_export_rechecks_disk_without_losing_npz():
    with tempfile.TemporaryDirectory() as d:
        job, dom, env = _case(d)
        with patch.object(F, 'disk_allows', return_value=True):
            info = F.retain(d, job, dom, env, 500000., 5200000.)
        with patch.object(F, 'disk_allows', return_value=False):
            F.export_geotiffs(d, info)
        assert (Path(d)/F.ARCHIVE).exists() and not (Path(d)/F.DEPTH).exists()
        assert '18 GiB' in info['warning']


@test
def fine_maxima_optional_failure_does_not_raise():
    with tempfile.TemporaryDirectory() as d:
        job, dom, env = _case(d)
        with patch.object(F, 'disk_allows', side_effect=OSError('simulated failure')):
            info = F.retain(d, job, dom, env, 500000., 5200000.)
        assert not info['saved'] and not info['support_kept']
        assert 'simulated failure' in info['warning']
