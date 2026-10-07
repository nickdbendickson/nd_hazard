"""Tests of scripts/ndbreach_cli.py (the headless command line) and of scripts/install_macos.sh's
settings editing. The CLI is loaded from its file, as it would be run."""
import contextlib
import csv
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np

from nd_breach_runner.core.grid import Grid
from nd_breach_runner.tests.registry import test, model_test
from nd_breach_runner.tests.synthetic import valley

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
CLI_PATH = os.path.join(PLUGIN, "scripts", "ndbreach_cli.py")
INSTALLER = os.path.join(PLUGIN, "scripts", "install_macos.sh")

_CLI = []


def cli():
    if not _CLI:
        spec = importlib.util.spec_from_file_location("ndbreach_cli_under_test", CLI_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _CLI.append(mod)
    return _CLI[0]


def run_cli(*argv):
    """(exit code, stdout, stderr) of main(argv) in this process."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli().main(list(argv))
    return code, out.getvalue(), err.getvalue()


def write_dem(folder, name="dem.asc", cell=20.0, x0=-3000.0, x1=14000.0, y0=-3000.0, y1=3000.0):
    xs = x0 + (np.arange(int(round((x1 - x0) / cell))) + 0.5) * cell
    ys = y1 - (np.arange(int(round((y1 - y0) / cell))) + 0.5) * cell
    X, Y = np.meshgrid(xs, ys)
    path = os.path.join(folder, name)
    Grid(valley(X, Y), x0, y1, cell).write_asc(path)
    return path


def fc(features, crs=None):
    d = {"type": "FeatureCollection", "features": features}
    if crs:
        d["crs"] = {"type": "name", "properties": {"name": crs}}
    return d


def pt(x, y, **props):
    return {"type": "Feature", "properties": props,
            "geometry": {"type": "Point", "coordinates": [x, y]}}


def line(coords, **props):
    return {"type": "Feature", "properties": props,
            "geometry": {"type": "LineString", "coordinates": coords}}


def dump(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f)
    return path


def base_config(folder, **over):
    """A small, valid configuration for the synthetic valley (dam at 0, 0)."""
    write_dem(folder)
    cfg = {
        "dem": {"path": "dem.asc", "z_units": "meters"},
        "dam": {"dam_id": "T1", "point": [0.0, 0.0], "dam_height_ft": 25.0,
                "storage_top_acft": 20.0, "surface_area_top_ac": 4.0, "top_of_dam_ft": 1850.0},
        "breach": {"method": "piping"},
        "model": {"cell_size_m": 20.0, "initial_length_mi": 1.0, "half_width_mi": 0.3,
                  "max_iterations": 1, "threads": 1},
        "output": {"folder": "out"},
    }
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return dump(os.path.join(folder, "config.json"), cfg), cfg


class Tmp(object):
    def __enter__(self):
        self.d = tempfile.mkdtemp()
        return self.d

    def __exit__(self, *a):
        shutil.rmtree(self.d, ignore_errors=True)


# ------------------------------------------------------------------------------- config
@test
def cli_template_is_valid_config():
    c = cli()
    with Tmp() as d:
        p = os.path.join(d, "t.json")
        with open(p, "w") as f:
            f.write(c.template_text())
        cfg = c.load_config(p)                       # every key of the template is known
        assert cfg.get("model", "tier") == "lisflood"
        assert cfg.get("scenario", "type") == "sunny"
    code, out, _err = run_cli("template")
    assert code == 0 and json.loads(out)["dem"]["z_units"]


@test
def cli_config_errors_are_clear():
    c = cli()
    with Tmp() as d:
        def load(obj):
            return c.load_config(dump(os.path.join(d, "c.json"), obj))

        def bad(obj, *words):
            try:
                load(obj)
            except c.CliError as e:
                for w in words:
                    assert w in str(e), (w, str(e))
                return
            raise AssertionError("expected CliError for {}".format(obj))
        bad({"dem": {"pth": "x"}}, "dem.pth", "did you mean 'path'")
        bad({"modle": {}}, "modle")
        bad({"model": {"cell_size_m": "big"}}, "cell_size_m")
        bad({"model": {"tier": "quantum"}}, "tier")
        bad([1, 2], "object")
        p = os.path.join(d, "broken.json")
        with open(p, "w") as f:
            f.write("{not json")
        try:
            c.load_config(p)
            raise AssertionError("expected CliError")
        except c.CliError as e:
            assert "broken.json" in str(e)
        # keys starting with _ are comments, and paths resolve against the file
        cfg = load({"_note": 1, "dem": {"_help": "x", "path": "sub/a.asc"}})
        assert cfg.get_path("dem", "path") == os.path.join(d, "sub", "a.asc")


@test
def cli_num_reads_table_values():
    c = cli()
    assert c.num("1,250.5") == 1250.5 and c.num(" 3 ") == 3.0 and c.num(7) == 7.0
    assert c.num("") is None and c.num("N/A") is None and c.num("abc", 9) == 9
    assert c.num(float("nan"), 2) == 2 and c.num(None) is None


# ------------------------------------------------------------------------------- GeoJSON
@test
def cli_geojson_reader():
    c = cli()
    data = fc([
        pt(1, 2, name="a"),
        {"type": "Feature", "properties": {"k": 1},
         "geometry": {"type": "MultiPoint", "coordinates": [[3, 4], [5, 6, 99]]}},
        line([[0, 0], [10, 0], [10, 10]], w=3),
        {"type": "Feature", "properties": {},
         "geometry": {"type": "MultiLineString", "coordinates": [[[0, 0], [1, 1]], [[2, 2], [3, 3]]]}},
        {"type": "Feature", "properties": {},
         "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [4, 0], [4, 4], [0, 0]],
                                                       [[1, 1], [2, 1], [2, 2], [1, 1]]]}},
        {"type": "Feature", "properties": {}, "geometry": None},
    ], crs="urn:ogc:def:crs:EPSG::26914")
    lay = c.parse_geojson(data, "x.geojson")
    assert lay.epsg == "EPSG:26914"
    assert [k for k, _p, _pr in lay.features] == ["point", "point", "line", "line", "polygon"]
    pts = lay.of_kind("point")
    assert pts[0][0][0].tolist() == [[1.0, 2.0]] and pts[1][0][1].tolist() == [[5.0, 6.0]]
    assert len(lay.of_kind("line")[1][0]) == 2                      # multi-line -> two parts
    assert lay.of_kind("polygon")[0][0][0].shape == (4, 2)          # outer ring only
    assert lay.field_names() == ["name", "k", "w"]
    assert lay.all_xy().shape[1] == 2
    # a bare geometry and a single feature are fine too
    assert len(c.parse_geojson({"type": "Point", "coordinates": [1, 2]}).features) == 1
    assert len(c.parse_geojson(pt(1, 2)).features) == 1
    for bad in ({"type": "Topology"}, [], {"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": {"type": "Circle", "coordinates": []}}]}):
        try:
            c.parse_geojson(bad, "bad")
            raise AssertionError("expected CliError")
        except c.CliError:
            pass
    with Tmp() as d:
        try:
            c.read_geojson(os.path.join(d, "missing.geojson"))
            raise AssertionError("expected CliError")
        except c.CliError as e:
            assert "missing.geojson" in str(e)
        p = os.path.join(d, "junk.geojson")
        with open(p, "w") as f:
            f.write("<html>")
        try:
            c.read_geojson(p)
            raise AssertionError("expected CliError")
        except c.CliError as e:
            assert "junk.geojson" in str(e)


class _FakeSRS(object):
    def __init__(self, tag=""):
        self.tag = tag
        self.strategy = None

    def SetFromUserInput(self, text):
        if text.startswith("BAD"):
            raise RuntimeError("cannot parse")
        self.tag = text

    def SetAxisMappingStrategy(self, s):
        self.strategy = s

    def IsSame(self, other):
        return self.tag == other.tag


class _FakeCT(object):
    def __init__(self, src, dst):
        self.src, self.dst = src, dst

    def TransformPoint(self, x, y):
        return (x + 1000.0, y - 5.0, 0.0)

    def TransformPoints(self, pts):
        return [(p[0] + 1000.0, p[1] - 5.0, 0.0) for p in pts]


class _FakeOsr(object):
    OAMS_TRADITIONAL_GIS_ORDER = 7
    SpatialReference = _FakeSRS
    CoordinateTransformation = _FakeCT


@test
def cli_frame_transforms_with_a_fake_osr():
    c = cli()
    dem, work = _FakeSRS("A"), _FakeSRS("B")
    fr = c.Frame(dem, work, _FakeOsr, dem_epsg="EPSG:1")
    assert not fr.identity
    assert fr.xy(1.0, 2.0) == (1001.0, -3.0)
    a = fr.array([[0, 0], [1, 1]])
    assert a.tolist() == [[1000.0, -5.0], [1001.0, -4.0]]
    other = fr.srs("C")
    assert other.strategy == 7
    assert fr.array([[0, 0]], other).tolist() == [[1000.0, -5.0]]
    assert fr.array([[0, 0]], fr.srs("B")).tolist() == [[0.0, 0.0]]        # already the model CRS
    assert fr.to_dem(1.0, 1.0, fr.srs("C")) == (1001.0, -4.0)
    assert fr.to_dem(1.0, 1.0, fr.srs("A")) == (1.0, 1.0)
    try:
        fr.srs("BAD thing")
        raise AssertionError("expected CliError")
    except c.CliError as e:
        assert "BAD thing" in str(e)
    same = c.Frame(_FakeSRS("A"), _FakeSRS("A"), _FakeOsr)
    assert same.identity and same.xy(3, 4) == (3.0, 4.0)
    # a layer that declares another CRS goes through the transform
    lay = c.parse_geojson(fc([pt(0, 0)], crs="EPSG:9999"))
    out = c.layer_to_work(lay, fr, None, True)
    assert out[0][1][0].tolist() == [[1000.0, -5.0]]
    # without GDAL the same request is refused with a reason
    nofr = c.Frame()
    try:
        c.layer_to_work(lay, nofr, None, True)
        raise AssertionError("expected CliError")
    except c.CliError as e:
        assert "GDAL" in str(e)


@test
def cli_lonlat_layers_in_a_projected_dem_are_refused():
    c = cli()
    lay = c.parse_geojson(fc([pt(-100.5, 47.2)]), "wgs.geojson")
    try:
        c.layer_to_work(lay, c.Frame(), None, True)
        raise AssertionError("expected CliError")
    except c.CliError as e:
        assert "longitude/latitude" in str(e) and "wgs.geojson" in str(e)
    ok = c.parse_geojson(fc([pt(350000, 5230000)]), "ok.geojson")
    assert c.layer_to_work(ok, c.Frame(), None, True)[0][0] == "point"


# ------------------------------------------------------------------------------- terrain
@test
def cli_asc_terrain_and_writer():
    c = cli()
    with Tmp() as d:
        p = write_dem(d, cell=20.0)
        t = c.AscTerrain(p, c.gdal_io.Z_METERS)
        assert t.z_to_m == 1.0
        b = (1000.0, 100.0, 1200.0, 300.0)                # smooth ground, off the embankment
        g = t.read(b, 10.0)
        assert g.data.shape == (20, 20) and np.isfinite(g.data).all()
        xs = 1000.0 + (np.arange(20) + 0.5) * 10.0
        ys = 300.0 - (np.arange(20) + 0.5) * 10.0
        X, Y = np.meshgrid(xs, ys)
        err = np.abs(g.data - valley(X, Y)).max()
        assert err < 0.05, err
        assert t.manning(b, 10.0) is None
        # feet: values are scaled and the source unit is applied
        tf = c.AscTerrain(p, c.gdal_io.Z_FEET)
        assert abs(tf.z_to_m - 0.3048) < 1e-9
        w = c.AscWriter()
        out = os.path.join(d, "depth.tif")
        gg = Grid(np.array([[1.0, np.nan], [0.5, 2.0]]), 0.0, 20.0, 10.0)
        w(gg, out)
        actual = c.AscWriter.actual(out)
        assert actual.endswith(".asc") and os.path.exists(actual)
        back = Grid.read_asc(actual)
        assert back.nx == 2 and back.dx == 10.0
        assert not back.valid()[0, 1] and back.data[1, 1] == 2.0


@test
def cli_dam_point_outside_dem_is_refused():
    with Tmp() as d:
        p, cfg = base_config(d)
        cfg["dam"]["point"] = [9e7, 0.0]
        dump(p, cfg)
        code, _o, err = run_cli("hydrograph", p)
        assert code == 0                                    # the hydrograph needs no DEM
        code, _o, err = run_cli("run", p, "--lisflood", sys.executable)
        assert code == 2 and "outside the DEM" in err, (code, err)


# ------------------------------------------------------------------------------- commands
@test
def cli_hydrograph_command():
    with Tmp() as d:
        p, _cfg = base_config(d)
        code, out, err = run_cli("hydrograph", p)
        assert code == 0, err
        f = os.path.join(d, "out", "T1", "breach_hydrograph.csv")
        assert os.path.exists(f) and os.path.exists(f.replace(".csv", ".svg"))
        with open(f) as fh:
            rows = list(csv.reader(fh))
        assert len(rows) > 5
        q = [float(r[1]) for r in rows[1:]]
        assert 3000 < max(q) < 6000, max(q)                 # piping failure of 20 ac-ft, 25 ft
        assert "peak_cfs" in out and "froehlich_1995_peak_cfs" in out


@test
def cli_bad_input_exits_2_with_a_message():
    with Tmp() as d:
        p, cfg = base_config(d)
        code, _o, err = run_cli("hydrograph", os.path.join(d, "nothing.json"))
        assert code == 2 and "nothing.json" in err
        cfg["dam"]["dam_height_ft"] = None
        dump(p, cfg)
        code, _o, err = run_cli("hydrograph", p)
        assert code == 2 and "dam_height_ft" in err, err
        cfg["dam"]["dam_height_ft"] = 25.0
        cfg["breach"]["method"] = "magic"
        dump(p, cfg)
        code, _o, err = run_cli("hydrograph", p)
        assert code == 2 and "method" in err, err
        cfg["breach"]["method"] = "piping"
        cfg["dem"]["path"] = "missing.asc"
        dump(p, cfg)
        code, _o, err = run_cli("run", p, "--lisflood", sys.executable)
        assert code == 2 and "missing.asc" in err, err
        cfg["dem"]["path"] = "dem.asc"
        cfg["scenario"] = {"type": "flood"}
        dump(p, cfg)
        code, _o, err = run_cli("run", p, "--lisflood", sys.executable)
        assert code == 2 and "inflow" in err, err
        cfg["scenario"] = {"type": "sunny"}
        dump(p, cfg)
        code, _o, err = run_cli("run", p, "--lisflood", os.path.join(d, "no_such_exe"))
        assert code == 2 and "LISFLOOD" in err, err
        # a model needs both to exist
        cfg["model"]["tier"] = "lisflood+anuga"
        dump(p, cfg)
        code, _o, err = run_cli("run", p, "--lisflood", sys.executable,
                                "--anuga-python", os.path.join(d, "no_python"))
        assert code == 2 and "ANUGA" in err, err


@test
def cli_check_reports_missing_lisflood():
    c = cli()
    keep = c.nd_data.detect_lisflood, c.nd_data.detect_anuga_python
    old_env = os.environ.pop("NDBREACH_LISFLOOD", None)
    c.nd_data.detect_lisflood = lambda *a, **k: ""
    c.nd_data.detect_anuga_python = lambda *a, **k: ""
    try:
        code, out, err = run_cli("check")
    finally:
        c.nd_data.detect_lisflood, c.nd_data.detect_anuga_python = keep
        if old_env is not None:
            os.environ["NDBREACH_LISFLOOD"] = old_env
    text = out + err
    assert code == 1, (code, text)
    assert "PROBLEM LISFLOOD-FP not found" in text and "OK numpy" in text, text


@test
def cli_flow_path_layers_are_read():
    c = cli()
    with Tmp() as d:
        p, _cfg = base_config(d)
        dump(os.path.join(d, "houses.geojson"), fc([pt(500, 0, name="H1"), pt(1500, 20, name="H2"),
                                                     pt(9e6, 0, name="far")]))
        dump(os.path.join(d, "road.geojson"), fc([line([[400, -200], [400, 200]])]))
        dump(os.path.join(d, "gauges.geojson"), fc([pt(700, 0, Name="upper"), pt(1400, 0, Name="lower")]))
        dump(os.path.join(d, "culverts.geojson"), fc([
            line([[300, -20], [300, 20]], kind="bridge", width=30, height=8, barrels=1),
            line([[900, -10], [900, 10]], NAME="C2", width=3, height=3, shape="round", barrels=2),
            line([[8e6, 0], [8e6, 5]])]))
        dump(os.path.join(d, "channel.geojson"), fc([line([[0, 0], [2000, 0]])]))
        dump(os.path.join(d, "pond.geojson"), fc([{"type": "Feature", "properties": {}, "geometry": {
            "type": "Polygon", "coordinates": [[[1000, 100], [1100, 100], [1100, 200], [1000, 100]]]}}]))
        cfg = json.load(open(p))
        cfg["layers"] = {"receptor_points": "houses.geojson", "receptor_roads": "road.geojson",
                         "gauges": "gauges.geojson", "gauge_name_field": "Name",
                         "structures": "culverts.geojson", "channels": "channel.geojson",
                         "ponds": "pond.geojson", "refine_lines": "channel.geojson"}
        dump(p, cfg)
        cf = c.load_config(p)
        opts = c.build_parser().parse_args(["run", p, "--lisflood", sys.executable])
        prep = c.prepare(cf, opts)
        st = prep.st
        assert len(st.receptors) > 3                                  # two houses and the road
        assert any('2 structures' in n for n in prep.notes), prep.notes
        assert [g["name"] for g in st.gauges] == ["upper", "lower"]
        assert len(st.structures) == 2, st.structures
        kinds = sorted(s.kind for s in st.structures)
        assert kinds == ["bridge", "culvert"], kinds
        pipe = [s for s in st.structures if s.kind == "culvert"][0]
        assert pipe.name == "C2" and pipe.barrels == 2 and pipe.shape == "pipe"
        assert abs(pipe.width_m - 3 * 0.3048) < 1e-9
        assert st.edits is not None and len(st.edits.channels) == 1 and len(st.edits.ponds) == 1
        assert len(st.refine_lines) == 1
        assert any("structure" in n for n in prep.notes)
        assert any("culvert" in n for n in prep.notes)
        # a layer wholly outside the domain is called out rather than silently ignored
        cfg["layers"] = {"gauges": "gauges.geojson"}
        dump(os.path.join(d, "g2.geojson"), fc([pt(9e6, 0)]))
        cfg["layers"]["gauges"] = "g2.geojson"
        dump(p, cfg)
        prep = c.prepare(c.load_config(p), opts)
        assert prep.st.gauges == [] and any("layers.gauges" in n for n in prep.notes), prep.notes
        # unknown structure field names are an error that lists the valid ones
        cfg["layers"] = {"structures": "culverts.geojson", "structure_fields": {"S_WIDE": "width"}}
        dump(p, cfg)
        try:
            c.prepare(c.load_config(p), opts)
            raise AssertionError("expected CliError")
        except c.CliError as e:
            assert "S_WIDE" in str(e) and "S_WIDTH" in str(e), str(e)


# ------------------------------------------------------------------------------- batch
class _FakeResult(object):
    def __init__(self, dam_id, breach=True):
        self.summary = {"dam_id": dam_id, "final_model": "LISFLOOD-FP", "peak_breach_cfs": 1234.0,
                        "storage_at_failure_acft": 20.0, "flooded_acres": 12.5,
                        "max_depth_ft": 3.0, "max_velocity_fps": 4.0, "latest_arrival_hr": 1.5,
                        "lisflood_runs": 1, "domain_mi": [1.0, 0.5], "breach_occurs": breach}
        self.warnings = ["check the crest"]
        self.files = {}


@test
def cli_batch_plumbing():
    c = cli()
    calls = []
    with Tmp() as d:
        p, cfg = base_config(d)
        cfg["dam"] = {"point": None}
        cfg["batch"] = {"crs": None}
        cfg["scenario"] = {"type": "sunny"}
        dump(p, cfg)
        table = os.path.join(d, "dams.csv")
        with open(table, "w") as f:
            f.write("NIDID,Dam Name,Dam Height (ft),Max Storage (acre-feet),Surface Area (acres),"
                    "Top of Dam (ft),x,y\n")
            f.write("A1,Alpha,25,20,4,1850,0,0\n")
            f.write("A2,Beta,30,\"1,200\",40,1900,500,100\n")
            f.write("A3,Gamma,,20,4,1850,0,0\n")           # no height: fails, the batch goes on
            f.write("A4,Delta,25,20,4,1850,9e7,0\n")       # outside the DEM
            f.write("A5,Eps,25,20,4,1850,0,0\n")           # will not breach
        fake_exe = os.path.join(d, "lisflood")
        with open(fake_exe, "w") as f:
            f.write("x")

        def fake_run_dam(dam, dam_xy, st, terrain, writer, folder, inflow_xy, progress, log,
                         cancel):
            calls.append((dam, dam_xy, st, folder))
            progress(50.0, "half")
            log("fake run of " + dam.dam_id)
            if dam.dam_id == "A5":
                return _FakeResult(dam.dam_id, breach=False)
            return _FakeResult(dam.dam_id)
        keep = c.workflow.run_dam
        c.workflow.run_dam = fake_run_dam
        try:
            code, out, err = run_cli("batch", table, p, "--lisflood", fake_exe)
            csvp = os.path.join(d, "out", "Batch_Breach_Runs.csv")
            with open(csvp) as fh:
                rows = {r["Dam"]: r for r in csv.DictReader(fh)}
            assert code == 3, (code, err)                        # some failed
            assert [x[0].dam_id for x in calls] == ["A1", "A2", "A5"]
            assert rows["A1"]["Status"] == "done" and rows["A1"]["Flooded_ac"] == "12.5"
            assert rows["A2"]["Status"] == "done"
            assert rows["A3"]["Status"] == "failed" and "dam_height_ft" in rows["A3"]["Message"]
            assert rows["A4"]["Status"] == "failed" and "outside the DEM" in rows["A4"]["Message"]
            assert rows["A5"]["Status"] == "no breach"
            dam2, xy2, st2, folder2 = calls[1]
            assert dam2.storage_top_acft == 1200.0 and dam2.dam_height_ft == 30.0
            assert dam2.top_of_dam_ft == 1900.0 and dam2.name == "Beta"
            assert xy2 == (500.0, 100.0)
            assert folder2.endswith(os.path.join("out", "A2"))
            assert "NIDID" in out and "F_HEIGHT" in out         # the column mapping is shown
            # a second pass skips the dams that already have results
            for a in ("A1", "A2", "A5"):
                os.makedirs(os.path.join(d, "out", a), exist_ok=True)
                open(os.path.join(d, "out", a, "Depth (Max).tif"), "w").write("x")
            del calls[:]
            code, out, err = run_cli("batch", table, p, "--lisflood", fake_exe)
            assert code == 3 and calls == [], (code, [x[0].dam_id for x in calls])
            with open(csvp) as fh:
                rows = {r["Dam"]: r for r in csv.DictReader(fh)}
            assert rows["A1"]["Status"] == "skipped" and rows["A3"]["Status"] == "failed"
            # --limit
            code, out, err = run_cli("batch", table, p, "--lisflood", fake_exe, "--limit", "1")
            with open(csvp) as fh:
                assert len(list(csv.DictReader(fh))) == 1
        finally:
            c.workflow.run_dam = keep


@test
def cli_batch_column_mapping_and_errors():
    c = cli()
    with Tmp() as d:
        p, _cfg = base_config(d)
        cfg = c.load_config(p)
        heads = ["NIDID", "Dam_Name", "Dam Height", "Max Storage", "Easting", "Northing", "Pool"]
        fld, used, (xc, yc) = c.map_columns(heads, cfg)
        assert fld["F_ID"] == "NIDID" and fld["F_HEIGHT"] == "Dam Height"
        assert (xc, yc) == ("Easting", "Northing")
        assert fld["F_POOL"] == "" and used                      # the pool column is never guessed
        raw = json.load(open(p))
        raw["batch"] = {"fields": {"F_POOL": "Pool", "F_HEIGHT": "Max Storage"},
                        "x_field": "Northing", "y_field": "Easting"}
        dump(p, raw)
        fld, _u, (xc, yc) = c.map_columns(heads, c.load_config(p))
        assert fld["F_POOL"] == "Pool" and fld["F_HEIGHT"] == "Max Storage"
        assert (xc, yc) == ("Northing", "Easting")
        for bad in ({"fields": {"F_POOL": "Nope"}}, {"fields": {"F_WHAT": "Pool"}},
                    {"x_field": "Nope"}):
            raw["batch"] = bad
            dump(p, raw)
            try:
                c.map_columns(heads, c.load_config(p))
                raise AssertionError("expected CliError for {}".format(bad))
            except c.CliError:
                pass
        table = os.path.join(d, "t.csv")
        for text, word in (("", "empty"), ("a,b\n", "no rows")):
            with open(table, "w") as f:
                f.write(text)
            try:
                c.read_dams_table(table)
                raise AssertionError("expected CliError")
            except c.CliError as e:
                assert word in str(e), str(e)
        with open(table, "w") as f:
            f.write("NIDID;Dam Height;x;y\nA;25;1;2\n")         # semicolon-separated
        heads, rows = c.read_dams_table(table)
        assert heads == ["NIDID", "Dam Height", "x", "y"] and rows[0]["x"] == "1"
        code, _o, err = run_cli("batch", os.path.join(d, "none.csv"), p)
        assert code == 2 and "none.csv" in err


# ------------------------------------------------------------------------------- model run
@model_test
def cli_run_end_to_end():
    exe = os.environ.get("NDBREACH_LISFLOOD", "")
    assert exe and os.path.isfile(exe), "NDBREACH_LISFLOOD must point to the patched lisflood"
    with Tmp() as d:
        p, cfg = base_config(d)
        dump(os.path.join(d, "houses.geojson"), fc([pt(600, 0, name="House A"),
                                                     pt(1200, 30, name="House B")]))
        dump(os.path.join(d, "gauges.geojson"), fc([pt(500, 0, name="G1")]))
        cfg["layers"] = {"receptor_points": "houses.geojson", "gauges": "gauges.geojson"}
        cfg["model"]["lisflood_exe"] = exe
        dump(p, cfg)
        code, out, err = run_cli("run", p)
        assert code == 0, (code, out[-2000:], err)
        folder = os.path.join(d, "out", "T1")
        depth = Grid.read_asc(os.path.join(folder, "Depth (Max).asc"))
        assert (depth.data[depth.valid()] > 0.05).sum() > 50
        for name in ("Velocity (Max).asc", "Arrival Time.asc", "WSE (Max).asc"):
            assert os.path.exists(os.path.join(folder, name)), name
        assert os.path.exists(os.path.join(folder, "_breach_model", "breach_hydrograph.csv"))
        assert os.path.exists(os.path.join(folder, "_breach_model", "run_report.md"))
        assert "flooded_acres" in out and "Results:" in out
        # resume reuses the finished run instead of failing
        code, out, err = run_cli("run", p, "--resume")
        assert code == 0, (code, err)
        # the flood scenario with an inflow CSV works from the same configuration
        with open(os.path.join(d, "pmf.csv"), "w") as f:
            f.write("hr,cfs\n0,0\n1,400\n2,1500\n3,900\n4,300\n6,50\n")
        cfg["scenario"] = {"type": "flood", "inflow_csv": "pmf.csv", "normal_pool_ft": 1845.0}
        cfg["output"] = {"folder": "outflood"}
        dump(p, cfg)
        code, out, err = run_cli("run", p)
        assert code == 0, (code, out[-2000:], err)
        assert os.path.exists(os.path.join(d, "outflood", "T1", "Incremental Depth (Max).asc"))


@test
def cli_runs_as_a_script():
    """Plain `python3 ndbreach_cli.py` (the way it is used outside QGIS)."""
    with Tmp() as d:
        p, _cfg = base_config(d)
        r = subprocess.run([sys.executable, CLI_PATH, "hydrograph", p], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True, cwd=d)
        assert r.returncode == 0, r.stderr
        assert "peak_cfs" in r.stdout
        r = subprocess.run([sys.executable, CLI_PATH, "hydrograph", os.path.join(d, "x.json")],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, cwd=d)
        assert r.returncode == 2 and "ndbreach: error" in r.stderr
        r = subprocess.run([sys.executable, CLI_PATH], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True, cwd=d)
        assert r.returncode == 2                                 # argparse: a command is required


# ------------------------------------------------------------------------------- installer
KEY_LF = "Configuration\\NDBREACH_LISFLOOD_EXE"
KEY_AN = "Configuration\\NDBREACH_ANUGA_PYTHON"


def bash_available():
    return shutil.which("bash") is not None


def sh(args, env=None, path_prepend=None, cwd=None):
    """(exit code, stdout, stderr) of scripts/install_macos.sh."""
    e = dict(os.environ)
    e.update(env or {})
    if path_prepend:
        e["PATH"] = path_prepend + os.pathsep + e.get("PATH", "")
    r = subprocess.run(["bash", INSTALLER] + list(args), stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, universal_newlines=True, env=e, cwd=cwd)
    return r.returncode, r.stdout, r.stderr


def make_exe(path, body="#!/bin/sh\nexit 0\n"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, 0o755)
    return path


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def backups(ini):
    d = os.path.dirname(ini)
    return sorted(f for f in os.listdir(d) if f.startswith(os.path.basename(ini) + ".ndbreach-backup-"))


SAMPLE_INI = (
    "[General]\nlocale\\userLocale=en_US\n\n"
    "[Processing]\nConfiguration\\ACTIVATE_GRASS7=true\nConfiguration\\KEEP_DIALOG_OPEN=false\n\n"
    "[UI]\ncustomization\\enabled=false\n")


@test
def installer_script_parses_and_documents_itself():
    if not bash_available():
        print("   (skipped: no bash)")
        return
    r = subprocess.run(["bash", "-n", INSTALLER], stderr=subprocess.PIPE, universal_newlines=True)
    assert r.returncode == 0, r.stderr
    if shutil.which("shellcheck"):
        r = subprocess.run(["shellcheck", "-s", "bash", "-S", "warning", INSTALLER],
                           stdout=subprocess.PIPE, universal_newlines=True)
        assert r.returncode == 0, r.stdout
    text = open(INSTALLER).read()
    for banned in ("declare -A", "mapfile", "readarray", "[[", "${!", "&>>", ";;&"):
        assert banned not in text, "bash 3.2 (macOS) has no {}".format(banned)
    code, out, err = sh(["--help"])
    assert code == 0 and "QGIS3.ini" in out and "--uninstall" in out and "NDBREACH_LISFLOOD_EXE" in out
    assert "#!" not in out.splitlines()[0]
    code, out, err = sh(["--bogus"])
    assert code == 2 and "Unknown option" in err
    code, out, err = sh(["--prefix", "relative/dir", "--dry-run"])
    assert code == 2 and "absolute" in err
    # the settings names the script writes are the ones the plugin reads
    src = open(os.path.join(PLUGIN, "algorithms.py")).read()
    assert 'SET_LISFLOOD = "NDBREACH_LISFLOOD_EXE"' in src and 'SET_ANUGA = "NDBREACH_ANUGA_PYTHON"' in src
    assert KEY_LF in text and KEY_AN in text
    # and the default place is one the plugin finds by itself
    assert '.ndbreach", "bin", "lisflood"' in open(os.path.join(PLUGIN, "core", "nd_data.py")).read()


@test
def installer_writes_settings_idempotently():
    if not bash_available():
        print("   (skipped: no bash)")
        return
    with Tmp() as d:
        lf = make_exe(os.path.join(d, "bin", "lisflood"))
        an = make_exe(os.path.join(d, "my env", "bin", "python"))          # a space is fine
        ini = os.path.join(d, "QGIS", "QGIS3.ini")
        os.makedirs(os.path.dirname(ini))
        with open(ini, "w", newline="") as f:
            f.write(SAMPLE_INI)
        os.chmod(ini, 0o600)
        args = ["--write-settings-only", "--ini", ini, "--lisflood-exe", lf, "--anuga-python", an,
                "--prefix", os.path.join(d, "prefix")]
        code, out, err = sh(args)
        assert code == 0, (out, err)
        assert KEY_LF + ": added" in out and KEY_AN + ": added" in out and "backup:" in out, out
        text = open(ini, newline="").read()
        lines = text.split("\n")
        i = lines.index("[Processing]")
        body = lines[i + 1:i + 5]
        assert body == ["Configuration\\ACTIVATE_GRASS7=true", "Configuration\\KEEP_DIALOG_OPEN=false",
                        KEY_LF + "=" + lf, KEY_AN + "=" + an], body      # appended inside the section
        # everything else is byte for byte what it was
        rest = text.replace(KEY_LF + "=" + lf + "\n", "").replace(KEY_AN + "=" + an + "\n", "")
        assert rest == SAMPLE_INI
        assert (os.stat(ini).st_mode & 0o777) == 0o600
        b = backups(ini)
        assert len(b) == 1 and read_bytes(os.path.join(d, "QGIS", b[0])) == SAMPLE_INI.encode()
        # again: nothing changes, no new backup, the file is untouched
        before = read_bytes(ini)
        code, out, err = sh(args)
        assert code == 0 and out.count(": unchanged") == 2 and "backup" not in out, out
        assert read_bytes(ini) == before and len(backups(ini)) == 1
        # a new path updates the value in place and backs up again
        lf2 = make_exe(os.path.join(d, "bin2", "lisflood"))
        code, out, err = sh(["--write-settings-only", "--ini", ini, "--lisflood-exe", lf2])
        assert code == 0 and KEY_LF + ": changed" in out and "backup:" in out, out
        t2 = open(ini).read()
        assert t2.count(KEY_LF + "=") == 1 and KEY_LF + "=" + lf2 in t2 and KEY_AN + "=" + an in t2
        assert len(backups(ini)) == 2


@test
def installer_settings_edge_cases():
    if not bash_available():
        print("   (skipped: no bash)")
        return
    with Tmp() as d:
        lf = make_exe(os.path.join(d, "lisflood"))
        base = ["--write-settings-only", "--lisflood-exe", lf]
        # no [Processing] section, no trailing newline, CRLF line endings
        ini = os.path.join(d, "a", "QGIS3.ini")
        os.makedirs(os.path.dirname(ini))
        with open(ini, "w", newline="") as f:
            f.write("[General]\r\nx=1\r\n\r\n[UI]\r\ny=2")
        code, out, err = sh(base + ["--ini", ini])
        assert code == 0, (out, err)
        raw = read_bytes(ini).decode()
        assert "\r\n" in raw and "\n" not in raw.replace("\r\n", ""), repr(raw)   # endings kept
        assert raw.endswith("[Processing]\r\n" + KEY_LF + "=" + lf + "\r\n"), repr(raw)
        assert raw.startswith("[General]\r\nx=1\r\n\r\n[UI]\r\ny=2\r\n\r\n[Processing]")
        # duplicates: the first is updated and the others removed
        ini2 = os.path.join(d, "b", "QGIS3.ini")
        os.makedirs(os.path.dirname(ini2))
        with open(ini2, "w") as f:
            f.write("[Processing]\n" + KEY_LF + "=/old/one\nfoo=1\n" + KEY_LF + "=/old/two\n[UI]\nz=3\n")
        code, out, err = sh(base + ["--ini", ini2])
        assert code == 0 and KEY_LF + ": changed" in out
        assert open(ini2).read() == "[Processing]\n" + KEY_LF + "=" + lf + "\nfoo=1\n[UI]\nz=3\n"
        # the section is last and has blank lines after its keys
        ini3 = os.path.join(d, "c", "QGIS3.ini")
        os.makedirs(os.path.dirname(ini3))
        with open(ini3, "w") as f:
            f.write("[Processing]\na=1\n\n\n")
        code, out, err = sh(base + ["--ini", ini3])
        assert code == 0 and open(ini3).read() == "[Processing]\na=1\n" + KEY_LF + "=" + lf + "\n\n\n"
        # a missing file in an existing profile folder is created
        ini4 = os.path.join(d, "d", "QGIS3.ini")
        os.makedirs(os.path.dirname(ini4))
        code, out, err = sh(base + ["--ini", ini4])
        assert code == 0 and "created:" in out
        assert open(ini4).read() == "[Processing]\n" + KEY_LF + "=" + lf + "\n"
        assert backups(ini4) == []
        # a missing profile folder is refused unless forced
        ini5 = os.path.join(d, "nothing", "here", "QGIS3.ini")
        code, out, err = sh(base + ["--ini", ini5])
        assert code == 2 and "does not exist" in err and not os.path.exists(os.path.dirname(ini5))
        code, out, err = sh(base + ["--ini", ini5, "--force"])
        assert code == 0 and os.path.exists(ini5)
        # --write-settings-only without paths, and with a file that is not executable
        code, out, err = sh(["--write-settings-only", "--ini", ini4])
        assert code == 2 and "--lisflood-exe" in err
        plain = os.path.join(d, "plain")
        open(plain, "w").write("x")
        code, out, err = sh(["--write-settings-only", "--ini", ini4, "--lisflood-exe", plain])
        assert code == 1 and "not an executable" in err
        # --no-settings prints the paths instead
        ini6 = os.path.join(d, "e", "QGIS3.ini")
        os.makedirs(os.path.dirname(ini6))
        code, out, err = sh(base + ["--ini", ini6, "--no-settings"])
        assert code == 0 and not os.path.exists(ini6) and lf in out


@test
def installer_refuses_unsafe_values_and_a_running_qgis():
    if not bash_available():
        print("   (skipped: no bash)")
        return
    with Tmp() as d:
        ini = os.path.join(d, "p", "QGIS3.ini")
        os.makedirs(os.path.dirname(ini))
        with open(ini, "w") as f:
            f.write(SAMPLE_INI)
        for name in ("has,comma", "has'quote", "has;semi", "héllo", 'has"dq', "back\\slash"):
            exe = make_exe(os.path.join(d, name, "lisflood"))
            code, out, err = sh(["--write-settings-only", "--ini", ini, "--lisflood-exe", exe])
            assert code == 2 and "use a plain path" in err, (name, code, err)
            assert open(ini).read() == SAMPLE_INI and backups(ini) == []
        # a running QGIS blocks the write (fake pgrep), --force overrides
        exe = make_exe(os.path.join(d, "ok", "lisflood"))
        fakebin = os.path.join(d, "fakebin")
        make_exe(os.path.join(fakebin, "pgrep"), "#!/bin/sh\nexit 0\n")
        code, out, err = sh(["--write-settings-only", "--ini", ini, "--lisflood-exe", exe],
                            path_prepend=fakebin)
        assert code == 2 and "QGIS is running" in err and open(ini).read() == SAMPLE_INI
        code, out, err = sh(["--write-settings-only", "--ini", ini, "--lisflood-exe", exe,
                             "--force"], path_prepend=fakebin)
        assert code == 0 and KEY_LF + "=" + exe in open(ini).read()


class FakeToolchain(object):
    """A fake build script, conda and pgrep so the whole installer runs without macOS."""

    def __init__(self, root):
        self.root = root
        self.bin = os.path.join(root, "fakebin")
        self.conda_root = os.path.join(root, "conda")
        self.log = os.path.join(root, "calls.log")
        self.src = os.path.join(root, "lisflood-src")
        os.makedirs(self.src)
        open(os.path.join(self.src, "CMakeLists.txt"), "w").write("project(x)\n")
        def tmpl(text):
            return text.replace("@LOG@", self.log).replace("@CR@", self.conda_root)
        self.build = make_exe(os.path.join(root, "fake_build.sh"), tmpl(
            "#!/bin/bash\n"
            "echo \"build $1 -> $2\" >> '@LOG@'\n"
            "mkdir -p \"$2\"\n"
            "printf '#!/bin/sh\\n# inittm_depth\\nexit 0\\n' > \"$2/lisflood\"\n"
            "chmod +x \"$2/lisflood\"\n"))
        self.env_script = make_exe(os.path.join(root, "fake_env.sh"), tmpl(
            "#!/bin/bash\n"
            "echo \"env $1\" >> '@LOG@'\n"
            "d=\"@CR@/envs/$1/bin\"\n"
            "mkdir -p \"$d\"\n"
            "cat > \"$d/python\" <<'EOF'\n"
            "#!/bin/sh\n"
            "if [ \"$1\" = \"-c\" ]; then\n"
            "  case \"$2\" in *sys.executable*) echo \"$0\";; esac\n"
            "  exit 0\n"
            "fi\n"
            "case \"$1\" in *ndbreach_cli.py)\n"
            "  echo \"cli $*\" >> '@LOG@'\n"
            "  [ -z \"$FAKE_CHECK_FAIL\" ] || exit 1;;\n"
            "esac\n"
            "exit 0\n"
            "EOF\n"
            "chmod +x \"$d/python\"\n"))
        make_exe(os.path.join(self.bin, "conda"), tmpl(
            "#!/bin/bash\n"
            "case \"$1\" in\n"
            "  run)\n"
            "    shift\n"
            "    [ \"$1\" = -n ] && shift\n"
            "    n=\"$1\"\n"
            "    shift\n"
            "    shift\n"
            "    p=\"@CR@/envs/$n/bin/python\"\n"
            "    [ -x \"$p\" ] || exit 1\n"
            "    exec \"$p\" \"$@\";;\n"
            "  env)\n"
            "    if [ \"$2\" = remove ]; then\n"
            "      echo \"conda env remove $*\" >> '@LOG@'\n"
            "      for a in \"$@\"; do n=\"$a\"; done\n"
            "      rm -rf \"@CR@/envs/$n\"\n"
            "      exit 0\n"
            "    fi;;\n"
            "esac\n"
            "exit 0\n"))
        make_exe(os.path.join(self.bin, "pgrep"), "#!/bin/sh\nexit 1\n")

    def env(self, **extra):
        e = {"NDBREACH_INSTALL_TEST": "1", "NDBREACH_BUILD_SCRIPT": self.build,
             "NDBREACH_ENV_SCRIPT": self.env_script, "HOME": self.root}
        e.update(extra)
        return e

    def calls(self, word):
        if not os.path.exists(self.log):
            return 0
        return sum(1 for ln in open(self.log) if ln.startswith(word))

    def run(self, prefix, ini, *extra, **env):
        return sh(["--prefix", prefix, "--ini", ini, "--lisflood-src", self.src, "--yes"] +
                  list(extra), env=self.env(**env), path_prepend=self.bin)


@test
def installer_full_flow_with_a_fake_toolchain():
    if not bash_available():
        print("   (skipped: no bash)")
        return
    with Tmp() as d:
        tc = FakeToolchain(d)
        prefix = os.path.join(d, "prefix")
        ini = os.path.join(d, "QGIS", "QGIS3.ini")
        os.makedirs(os.path.dirname(ini))
        with open(ini, "w") as f:
            f.write(SAMPLE_INI)
        # a dry run changes nothing at all
        code, out, err = tc.run(prefix, ini, "--dry-run")
        assert code == 0 and "dry run" in out, (out, err)
        assert not os.path.exists(prefix) and open(ini).read() == SAMPLE_INI and tc.calls("build") == 0
        # the real thing
        code, out, err = tc.run(prefix, ini)
        assert code == 0, (out, err)
        exe = os.path.join(prefix, "bin", "lisflood")
        py = os.path.join(d, "conda", "envs", "anuga", "bin", "python")
        assert os.path.isfile(exe) and os.access(exe, os.X_OK) and os.path.isfile(py)
        text = open(ini).read()
        assert KEY_LF + "=" + exe in text and KEY_AN + "=" + py in text
        assert tc.calls("build") == 1 and tc.calls("env") == 1 and tc.calls("cli") == 1
        assert "check --lisflood " + exe + " --anuga-python " + py in open(tc.log).read()
        man = open(os.path.join(prefix, "install-manifest.txt")).read()
        assert "lisflood_exe=" + exe in man and "anuga_python=" + py in man and "anuga_env=anuga" in man
        # nothing outside the prefix, the conda root and the settings file was touched
        assert sorted(os.listdir(os.path.join(d, "QGIS"))) == sorted(
            ["QGIS3.ini"] + backups(ini)) and len(backups(ini)) == 1
        # running it again builds nothing and changes no settings
        before = read_bytes(ini)
        code, out, err = tc.run(prefix, ini)
        assert code == 0, (out, err)
        assert tc.calls("build") == 1 and tc.calls("env") == 1, open(tc.log).read()
        assert "already built" in out and "environment exists" in out and read_bytes(ini) == before
        # --rebuild and --recreate-env do what they say
        code, out, err = tc.run(prefix, ini, "--rebuild", "--recreate-env")
        assert code == 0 and tc.calls("build") == 2 and tc.calls("env") == 2
        assert tc.calls("conda env remove") == 1
        # the check passes, and notices when the executable disappears or the setting is stale
        code, out, err = sh(["--check", "--prefix", prefix, "--ini", ini], env=tc.env(),
                            path_prepend=tc.bin)
        assert code == 0 and "Everything looks fine." in out and "OK LISFLOOD-FP setting" in out, out
        os.remove(exe)
        code, out, err = sh(["--check", "--prefix", prefix, "--ini", ini], env=tc.env(),
                            path_prepend=tc.bin)
        assert code == 1 and "PROBLEM: LISFLOOD-FP setting points to" in out, out
        code, out, err = tc.run(prefix, ini)                    # repairs it
        assert code == 0 and os.path.isfile(exe) and tc.calls("build") == 3
        # a failing model check stops before the settings are written
        ini_b = os.path.join(d, "QGIS", "other", "QGIS3.ini")
        os.makedirs(os.path.dirname(ini_b))
        prefix_b = os.path.join(d, "prefix_b")
        code, out, err = tc.run(prefix_b, ini_b, FAKE_CHECK_FAIL="1")
        assert code == 1 and "check of the installed models failed" in err, (out, err)
        assert not os.path.exists(ini_b) and not os.path.exists(os.path.join(prefix_b, "install-manifest.txt"))
        code, out, err = tc.run(prefix_b, ini_b, "--no-verify", FAKE_CHECK_FAIL="1")
        assert code == 0 and KEY_LF in open(ini_b).read()
        # QGIS running: refused before anything is built
        prefix_c = os.path.join(d, "prefix_c")
        make_exe(os.path.join(tc.bin, "pgrep"), "#!/bin/sh\nexit 0\n")
        n = tc.calls("build")
        code, out, err = tc.run(prefix_c, ini)
        assert code == 2 and "QGIS is running" in err and tc.calls("build") == n
        make_exe(os.path.join(tc.bin, "pgrep"), "#!/bin/sh\nexit 1\n")


@test
def installer_uninstall_removes_only_what_it_added():
    if not bash_available():
        print("   (skipped: no bash)")
        return
    with Tmp() as d:
        tc = FakeToolchain(d)
        prefix = os.path.join(d, "prefix")
        ini = os.path.join(d, "QGIS", "QGIS3.ini")
        os.makedirs(os.path.dirname(ini))
        with open(ini, "w") as f:
            f.write(SAMPLE_INI)
        assert tc.run(prefix, ini)[0] == 0
        os.makedirs(os.path.join(prefix, "src"), exist_ok=True)
        open(os.path.join(prefix, "src", "keep.txt"), "w").write("x")
        code, out, err = sh(["--uninstall", "--dry-run", "--prefix", prefix, "--ini", ini],
                            env=tc.env(), path_prepend=tc.bin)
        assert code == 0 and os.path.exists(os.path.join(prefix, "bin", "lisflood"))
        code, out, err = sh(["--uninstall", "--prefix", prefix, "--ini", ini], env=tc.env(),
                            path_prepend=tc.bin)
        assert code == 0, (out, err)
        assert KEY_LF + ": removed" in out and KEY_AN + ": removed" in out, out
        assert open(ini).read() == SAMPLE_INI                              # exactly as before
        assert not os.path.exists(os.path.join(prefix, "bin", "lisflood"))
        assert not os.path.exists(os.path.join(prefix, "install-manifest.txt"))
        assert os.path.exists(os.path.join(prefix, "src", "keep.txt"))      # sources kept
        assert os.path.exists(os.path.join(d, "conda", "envs", "anuga", "bin", "python"))
        assert tc.calls("conda env remove") == 0
        # a setting the user pointed somewhere else in the meantime is left alone
        prefix2 = os.path.join(d, "prefix2")
        ini2 = os.path.join(d, "QGIS", "second", "QGIS3.ini")
        os.makedirs(os.path.dirname(ini2))
        assert tc.run(prefix2, ini2)[0] == 0
        mine = make_exe(os.path.join(d, "mine", "lisflood"))
        assert sh(["--write-settings-only", "--ini", ini2, "--lisflood-exe", mine])[0] == 0
        os.makedirs(os.path.join(prefix2, "src"), exist_ok=True)
        code, out, err = sh(["--uninstall", "--remove-src", "--remove-env", "--prefix", prefix2,
                             "--ini", ini2], env=tc.env(), path_prepend=tc.bin)
        assert code == 0, (out, err)
        assert KEY_LF + ": kept" in out and KEY_AN + ": removed" in out, out
        assert KEY_LF + "=" + mine in open(ini2).read() and KEY_AN not in open(ini2).read()
        assert not os.path.exists(os.path.join(prefix2, "src")) and tc.calls("conda env remove") == 1
        assert not os.path.exists(prefix2)
        # --remove-src refuses a folder the installer did not create (no manifest)
        stranger = os.path.join(d, "stranger")
        os.makedirs(os.path.join(stranger, "src"))
        open(os.path.join(stranger, "src", "mine.txt"), "w").write("x")
        code, out, err = sh(["--uninstall", "--remove-src", "--prefix", stranger, "--ini", ini2],
                            env=tc.env(), path_prepend=tc.bin)
        assert code == 0 and os.path.isdir(os.path.join(stranger, "src")) and "not removing" in err
