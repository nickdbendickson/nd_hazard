"""Tests of the 3.20.0 items.

Usability: the window that fits a laptop (item 1), the screening hand-off (item 2), the reason
on a grey Run button (item 3) and the walk-through fixes (item 4, the DEM note's own row, the
ND LiDAR row, UNITS on a mosaic and the interim rasters named in the log).

Features: population at risk and loss of life (12), the road and railroad crossing table (13),
the cascade check after a run (4), crossings as embankment gaps (14), two runs compared (15) and
the queue over a list of dams (16).

No QGIS and no LISFLOOD-FP: the core modules are exercised directly (they are written without
QGIS for exactly this reason) and the Processing tools through tests/fake_qgis.py. The GDAL
tests need the real GDAL Python bindings, which this suite already relies on elsewhere
(test_hecras_model.py)."""
import configparser
import json
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.tests.registry import test

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tmp():
    return tempfile.mkdtemp(prefix="ndbr_v3200_")


def _grid(nx, ny, dx, value=0.0, x0=0.0):
    from nd_breach_runner.core.grid import Grid
    return Grid(np.full((ny, nx), float(value)), x0, ny * dx / 2.0, dx)


def _screened(folder, **result):
    """A "screened" status in the dam's record, the way the Screening plugin records its step."""
    from nd_breach_runner.damsuite import dam_record as DR
    rec = DR.load(folder, create=True)
    rec.add_status("screened", "ndscreen", "0.28.0", result, when="2026-10-10T09:00:00")
    rec.save()
    return rec


def _dam_record(folder, **values):
    """A real dam.json (damsuite.dam_record's own format, not a hand-written dict)."""
    from nd_breach_runner.damsuite import dam_record as DR
    os.makedirs(folder, exist_ok=True)
    rec = DR.load(folder, create=True)
    for k, v in values.items():
        rec.set(k, v, "test", "3.20.0")
    rec.save()
    return rec


def _algs():
    """(algorithms module, the 3.20.0 module, fake_qgis) with the fakes installed."""
    import importlib
    from nd_breach_runner.tests import fake_qgis as fq
    fq.install()
    import nd_breach_runner.algorithms as algs
    importlib.reload(algs)
    import nd_breach_runner.algorithms_release320 as mod
    importlib.reload(mod)
    return algs, mod, fq


# ======================================================== item 1: the window fits a laptop
@test
def v3200_the_window_fits_a_768_px_screen():
    """The review's first complaint: 902 px minimum height on a 768 px laptop."""
    from nd_breach_runner.tests.test_popup import qt_fakes
    with qt_fakes() as q:
        dialog = q.dialog
        assert dialog.MIN_HEIGHT <= 640, dialog.MIN_HEIGHT
        # the tool tabs get the freed space, and the log keeps a floor so it never vanishes
        assert dialog.TAB_MIN_H >= 150 and dialog.LOG_MIN_H >= 40
        assert dialog.MIN_HEIGHT + dialog.TAB_MIN_H < 902  # the old minimum, for the record
        assert dialog.ADVANCED_TAB == "Advanced"


@test
def v3200_the_dem_box_is_collapsible_and_collapses_once_a_dem_exists():
    from nd_breach_runner.tests.test_popup import qt_fakes
    with qt_fakes() as q:
        cls = q.dialog.NDBreachRunnerDialog
        assert hasattr(cls, "_dem_box_toggled") and hasattr(cls, "_collapse_dem_box")
    # the group is checkable: that is what makes it collapsible in Qt, and the window
    # collapses it itself once the dam has a DEM
    src = open(os.path.join(PLUGIN, "dialog.py"), encoding="utf-8").read()
    assert "setCheckable(True)" in src and "toggled.connect(self._dem_box_toggled)" in src
    assert "self._collapse_dem_box(bool(dem))" in src


# ======================================================== item 2: the screening hand-off
@test
def v3200_handoff_reads_the_screening_choices_from_dam_json():
    from nd_breach_runner.core import screenhandoff as sh
    d = _tmp()
    try:
        folder = os.path.join(d, "DWR-1")
        _dam_record(folder, height_ft=25.0, storage_acft=500.0)
        # a screening status result, as the screening plugin records its own step
        _screened(folder, cell_m=20.0, method="froehlich 2008 piping",
                  scenario="top of dam")
        info = sh.read(folder)
        assert info["cell_m"] == 20.0
        assert info["method"] == sh.brmod.METHOD_PIPING
        assert info["scenario"] == sh.workflow.SCENARIO_SUNNY
        assert sh.from_screening(info) and info["source"] == "dam.json"
        assert "20 m grid" in sh.values_text(info)
        text = sh.source_text(info)
        assert text.startswith("from dam.json (") and "0.28.0" in text and "2026-10-10" in text
        assert not sh.missing_text(info)          # nothing is missing: all three are there
        lines = sh.report_lines(info)
        assert any("0.28.0" in ln for ln in lines)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3200_handoff_says_what_the_screening_must_record_when_it_records_nothing():
    """Today the screening plugin records none of the three in dam.json. The hand-off must
    then name the keys its worker has to add, not guess them from a file name."""
    from nd_breach_runner.core import screenhandoff as sh
    d = _tmp()
    try:
        folder = os.path.join(d, "DWR-2")
        _dam_record(folder, height_ft=30.0)
        info = sh.read(folder)
        assert "cell_m" not in info and "method" not in info
        assert not sh.from_screening(info) and sh.source_text(info) == ""
        assert info["missing"] == list(sh.WANTED)
        missing = sh.missing_text(info)
        for key in ("cell_m", "method", "scenario"):
            assert key in missing, missing
        assert "screened" in missing          # it names where the screening must record them
        # nothing is guessed: an empty screening folder does not invent a cell size
        os.makedirs(os.path.join(folder, sh.SCREENING_FOLDER))
        assert "cell_m" not in sh.read(folder)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3200_handoff_also_reads_the_screening_run_manifest():
    from nd_breach_runner.core import screenhandoff as sh
    d = _tmp()
    try:
        folder = os.path.join(d, "DWR-3")
        _dam_record(folder, height_ft=20.0)
        m = os.path.join(folder, "_screening_model")
        os.makedirs(m)
        json.dump({"tool": "ND Dam Breach Screening", "version": "0.28.0",
                   "settings": {"cell_m": 30.0, "breach_method": "vonthun"},
                   "extra": {"screening_result": {"scenario": "flood"}}},
                  open(os.path.join(m, "run_manifest.json"), "w", encoding="utf-8"))
        info = sh.read(folder)
        assert info["cell_m"] == 30.0
        assert info["method"] == sh.brmod.METHOD_VTG
        assert info["scenario"] == sh.workflow.SCENARIO_FLOOD
        assert "run_manifest.json" in sh.source_text(info)
        assert "0.28.0" in sh.source_text(info)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3200_handoff_fills_the_tool_but_never_overwrites_a_choice():
    from nd_breach_runner import popup_logic as pl
    d = _tmp()
    try:
        folder = os.path.join(d, "DWR-9")
        _dam_record(folder, height_ft=25.0, storage_acft=500.0)
        _screened(folder, cell_m=20.0, method="froehlich 2008 piping", scenario="top of dam")
        start = pl.DEFAULT_SETTINGS["grid_m"]
        got, notes = pl.handoff_defaults("rundam", folder, {}, {"CELL": start})
        assert got.get("CELL") == 20.0 and "METHOD" in got and "SCENARIO" in got
        assert notes and "0.28.0" in " ".join(notes)
        assert "20 m" in " ".join(notes)
        # a cell size the engineer already set is kept
        kept, _n = pl.handoff_defaults("rundam", folder, {}, {"CELL": 5.0})
        assert "CELL" not in kept, kept
        # a method the caller already has is kept too
        kept2, _n = pl.handoff_defaults("rundam", folder, {}, {"CELL": start, "METHOD": 4})
        assert "METHOD" not in kept2 and kept2.get("CELL") == 20.0
        # a tool that takes no breach parameters is not touched
        none, _n = pl.handoff_defaults("buildvrt", folder, {}, {})
        assert none == {}
        # and the whole defaults() path fills them for the run tool
        p = pl.defaults("rundam", d, "DWR-9", {})
        assert p.get("CELL") == 20.0 and p.get("METHOD") is not None
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ======================================================== item 3: why is that button grey?
@test
def v3200_a_grey_run_button_says_what_is_missing():
    from nd_breach_runner.core import runblock as RB
    d = _tmp()
    try:
        # no dams folder at all
        st = RB.state("rundam", "", "", "", "", items=[])
        assert not st["ok"] and RB.NEED_DAMS_FOLDER in st["why"]
        assert RB.NEED_DAMS_FOLDER in st["tooltip"] and st["status"]
        # a dams folder and a dam, but no DEM: the message names the button to press
        folder = os.path.join(d, "DWR-1")
        _dam_record(folder, height_ft=25.0, storage_acft=500.0)
        st = RB.state("rundam", folder, "", d, "DWR-1", items=[])
        assert not st["ok"] and RB.NEED_DEM in st["why"]
        assert "Download DEM for this dam" in RB.NEED_DEM
        # with a DEM it is runnable
        dem = os.path.join(d, "dem.tif")
        open(dem, "wb").write(b"tif")
        st = RB.state("rundam", folder, dem, d, "DWR-1", items=[])
        assert st["ok"], st["why"]
        assert st["tooltip"] == "" and st["status"] == ""
        # ... and without the dam's values it says which values are missing
        bare = os.path.join(d, "DWR-0")
        os.makedirs(bare)
        no_vals = RB.state("rundam", bare, dem, d, "DWR-0", items=[])
        assert RB.NEED_VALUES in no_vals["why"]
        assert RB.log_lines(no_vals)[0].startswith("This run cannot start")
        # a results tool needs a finished run, and says so
        st = RB.state("consequences", folder, dem, d, "DWR-1", items=[])
        assert not st["ok"] and RB.NEED_RUN in st["why"]
        from nd_breach_runner.core import workflow as wf
        for k in ("depth", "velocity", "arrival"):
            open(os.path.join(folder, wf.FINAL_NAMES[k]), "wb").write(b"tif")
        # 3.21.0: the pass rasters alone are NOT a finished run (a cancelled dam leaves them);
        # the results tool stays grey until the run report says the run finished.
        assert not RB.has_run(folder)
        assert not RB.state("consequences", folder, dem, d, "DWR-1", items=[])["ok"]
        rep = os.path.join(folder, wf.RUN_REPORT_REL)
        os.makedirs(os.path.dirname(rep), exist_ok=True)
        with open(rep, "w") as f:
            json.dump({"run_finished": "2026-10-14T00:00:00"}, f)
        assert RB.has_run(folder)
        assert RB.state("consequences", folder, dem, d, "DWR-1", items=[])["ok"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3200_suite_errors_grey_the_run_tools_but_never_leave_nothing_to_press():
    from nd_breach_runner.core import runblock as RB
    items = [{"key": "suite", "label": "Suite folder", "level": "error",
              "text": "no suite folder", "fix": "choose one in Plugins > ND Dam Suite"},
             {"key": "nlcd", "label": "Land cover", "level": "warn",
              "text": "no land cover", "fix": "optional"}]
    reasons = RB.suite_reasons(items)
    assert reasons and any("suite folder" in r.lower() for r in reasons)
    assert not any("land cover" in r.lower() for r in reasons)   # a warning is not a reason
    d = _tmp()
    try:
        st = RB.state("rundam", "", "", d, "", items=items)
        assert not st["ok"] and any("suite folder" in r.lower() for r in st["why"])
        free = RB.state("check", "", "", "", "", items=items)
        assert free["ok"], free["why"]       # "Check the model installation" stays pressable
        for name in RB.FREE_TOOLS:
            assert RB.state(name, "", "", "", "", items=items)["ok"], name
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ======================================================== item 4: the walk-through fixes
@test
def v3200_units_metadata_on_a_raster_beats_the_crs_guess():
    """Step 2's mosaic records UNITS=metres. The reader must believe it instead of inferring
    feet from a state-plane CRS, which is what produced "no units recorded"."""
    from osgeo import gdal, osr
    from nd_breach_runner.core import gdal_io
    d = _tmp()
    try:
        tile = os.path.join(d, "tile.tif")
        drv = gdal.GetDriverByName("GTiff")
        ds = drv.Create(tile, 8, 8, 1, gdal.GDT_Float32)
        ds.SetGeoTransform((0.0, 3.0, 0.0, 24.0, 0.0, -3.0))
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(3721)                  # a CRS whose linear unit is FEET
        ds.SetProjection(srs.ExportToWkt())
        ds.GetRasterBand(1).WriteArray(np.full((8, 8), 100.0, dtype=np.float32))
        ds.SetMetadataItem("UNITS", "metres")
        ds = None
        factor, note = gdal_io.z_to_m_factor(tile)
        assert factor == 1.0 and "metres" in note and "UNITS" in note, (factor, note)
        # and through a VRT over it: the source tile's UNITS is found
        vrt = os.path.join(d, "m.vrt")
        gdal.BuildVRT(vrt, [tile])
        factor, note = gdal_io.z_to_m_factor(vrt)
        assert factor == 1.0 and "metres" in note, (factor, note)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3200_the_runners_own_vrt_records_its_units():
    src = open(os.path.join(PLUGIN, "core", "gdal_io.py"), encoding="utf-8").read()
    assert 'SetMetadataItem("UNITS"' in src, "the VRT builder must tag UNITS"


@test
def v3200_the_dem_source_note_has_its_own_row_and_nd_lidar_hides():
    src = open(os.path.join(PLUGIN, "dialog.py"), encoding="utf-8").read()
    # the note spans its own grid row instead of being drawn over the "Grid size" cell
    assert "grid.addWidget(self.dem_note, 3, 0, 1, 3)" in src, \
        "the DEM source note needs a row of its own"
    # the ND LiDAR path keeps a handle on its label, so the whole row can be hidden
    assert "self.dem_ftp_label" in src and "ND LiDAR path" in src
    assert "dem_ftp_label.setVisible" in src or "dem_ftp_label.hide" in src, \
        "the ND LiDAR path row must be hideable with its label"
    # and the note shows the file name, with the full path in the tooltip
    assert "os.path.basename(dem)" in src and "setToolTip(dem if from_folder" in src


@test
def v3200_each_pass_names_the_rasters_that_can_be_opened():
    """The log after pass 1 says which interim rasters are already openable (test_v3180 runs
    the model; this one only pins the wording the report carries)."""
    from nd_breach_runner.core import workflow as wf
    assert set(wf.PASS_KINDS) == {"depth", "velocity", "arrival"}, wf.PASS_KINDS


# ======================================================== item 12: PAR and loss of life
@test
def v3200_the_shipped_fatality_rate_table_is_empty_and_unsigned():
    from nd_breach_runner.core import lifeloss as ll
    p = ll.plugin_file()
    assert os.path.isfile(p), p
    table, problem = ll.load(p)
    assert not problem, problem
    assert table["status"] == ll.STATUS_UNSIGNED
    assert len(ll.empty_cells(table)) == 9          # three severities x three warning bands
    ok, text = ll.status_text(table)
    assert not ok and "signed" in text.lower()
    assert "NOT estimated" in ll.NOT_ESTIMATED


@test
def v3200_par_is_reported_without_a_rate_table_and_loss_of_life_is_not():
    from nd_breach_runner.core import lifeloss as ll
    table, _p = ll.load()
    rows = [{"pop2amu65": 4, "pop2pmu65": 1, "depth_ft": 20.0, "velocity_fts": 9.0,
             "arrival_hr": 0.2},
            {"pop2amu65": 3, "pop2pmu65": 0, "depth_ft": 2.0, "velocity_fts": 1.0,
             "arrival_hr": 3.0}]
    est = ll.assess(rows, table)
    s = ll.summary(est)
    assert s["night"]["par"] == 7.0 and s["day"]["par"] == 1.0
    assert s["night"]["by_severity"]["high"] == 4.0
    assert s["night"]["by_warning"]["under15"] == 4.0
    assert s["night"]["by_warning"]["over60"] == 3.0
    assert s["loss_of_life"] is None and not s["rates_ok"]
    md = "\n".join(ll.markdown_lines(est))
    assert "PAR 7" in md and "Loss of life is NOT estimated" in md


@test
def v3200_a_signed_rate_table_gives_a_number_and_a_holey_one_does_not():
    from nd_breach_runner.core import lifeloss as ll
    d = _tmp()
    try:
        full = {}
        for sev, vals in (("high", (0.9, 0.6, 0.2)), ("medium", (0.1, 0.03, 0.01)),
                          ("low", (0.01, 0.005, 0.0002))):
            for band, v in zip(("under15", "15to60", "over60"), vals):
                full["{}/{}".format(sev, band)] = v
        r = ll.write_rate_file(os.path.join(d, "rates.json"), full, "A. Engineer, PE",
                               "B. Checker", "Some published table, 2024, Table 3")
        assert r["status"] == ll.STATUS_SIGNED and not r["bad"] and not r["empty_cells"]
        assert r["sha256"] and len(r["sha256"]) == 64
        table, _p = ll.load(r["path"])
        ok, text = ll.status_text(table)
        assert ok and "A. Engineer, PE" in text
        rows = [{"pop2amu65": 4, "pop2pmu65": 1, "depth_ft": 20.0, "velocity_fts": 9.0,
                 "arrival_hr": 0.2}]
        s = ll.summary(ll.assess(rows, table))
        assert s["loss_of_life"]["night"] > 0 and s["loss_of_life"]["day"] > 0
        assert s["loss_of_life"]["night"] > s["loss_of_life"]["day"]      # 4 at night, 1 by day
        # one cell left out: damsuite.loss would quietly keep the running total, so lifeloss
        # refuses the whole number instead of reporting one that is too low
        hole = dict(full)
        hole.pop("high/over60")
        r2 = ll.write_rate_file(os.path.join(d, "hole.json"), hole, "A. Engineer, PE")
        t2, _p = ll.load(r2["path"])
        ok2, text2 = ll.status_text(t2)
        assert not ok2 and "high/over60" in text2 and "too low" in text2
        assert ll.summary(ll.assess(rows, t2))["loss_of_life"] is None
        # a percentage is refused, not read as 7500%
        r3 = ll.write_rate_file(os.path.join(d, "pct.json"), {"high/under15": 75},
                                "A. Engineer, PE")
        assert r3["bad"] and "fraction" in r3["bad"][0]
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3200_consequences_reports_par_beside_its_own_estimate_never_instead_of_it():
    """The PAR / loss-of-life roll-up is an addition: the tool's own damage and loss-of-life
    numbers must still be there, and a failure in the addition must not lose them."""
    from nd_breach_runner.core import consequences as cons
    import inspect
    src = inspect.getsource(cons.assess)
    assert "rate_table" in src and "summary[\"par\"]" in src
    for fn in (cons._par_summary, cons._crossing_summary):
        assert "problem" in inspect.getsource(fn)
    # a broken rate table does not raise and does not remove the estimate
    got = cons._par_summary([], None, {"rates": "not a table"})
    assert isinstance(got, dict)
    md = cons.markdown_summary.__doc__ or ""
    assert cons.OUTPUT_NAMES["csv"] == "Consequences (Structures).csv"


# ======================================================== item 13: the crossing table
@test
def v3200_crossing_table_has_a_row_per_wet_stretch_with_times_and_closure():
    from nd_breach_runner.core import crossings as crmod, impacts as impmod
    g = _grid(60, 40, 10.0)
    X, Y = np.meshgrid(g.x_centers(), g.y_centers())
    depth = np.where((X > 100) & (X < 400) & (np.abs(Y) < 60), 3.0, np.nan)
    fields = {"depth": depth, "velocity": np.where(np.isfinite(depth), 4.0, np.nan),
              "arrival": np.where(np.isfinite(depth), 1.5, np.nan)}
    rec = impmod.Receptors()
    rec.add_lines([np.array([[0.0, 0.0], [580.0, 0.0]])], impmod.KIND_ROAD, ids=["CR 12"],
                  unit_m=1.0)
    rec.add_lines([np.array([[250.0, -150.0], [250.0, 150.0]])], impmod.KIND_RAIL,
                  ids=["BNSF"], unit_m=1.0)
    rows = crmod.crossing_rows(rec, g, fields, "us", sim_end_hr=12.0)
    assert len(rows) == 2, rows
    byid = {r["id"]: r for r in rows}
    road = byid["CR 12"]
    assert road["kind"] == "road" and road["first_wet_hr"] == 1.5
    assert road["max_depth_ft"] == 3.0 and road["max_velocity_fps"] == 4.0
    assert road["closed"] == "yes" and road["wet_length_ft"] > 200
    assert road["duration_wet_hr"] == 10.5          # 12.0 - 1.5
    assert road["duration_basis"] == crmod.DURATION_TO_END
    summ = crmod.summary(rows)
    assert summ["closed"] == 2 and summ["road"]["crossings"] == 1 and summ["bounded_duration"]
    md = "\n".join(crmod.markdown_lines(rows, summ))
    assert "CR 12" in md and "UPPER bound" in md and "not a legal closure" in md
    d = _tmp()
    try:
        out = crmod.write_outputs(d, rows, "EPSG:26914")
        assert os.path.isfile(out["csv"]) and os.path.isfile(out["geojson"])
        head = open(out["csv"], encoding="utf-8").readline()
        for col in ("first_wet_hr", "max_depth_ft", "max_velocity_fps", "duration_wet_hr",
                    "closed"):
            assert col in head, head
        gj = json.load(open(out["geojson"], encoding="utf-8"))
        assert gj["type"] == "FeatureCollection" and len(gj["features"]) == 2
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3200_a_dry_road_and_two_separate_wet_stretches_are_counted_properly():
    from nd_breach_runner.core import crossings as crmod, impacts as impmod
    g = _grid(80, 40, 10.0)
    X, Y = np.meshgrid(g.x_centers(), g.y_centers())
    wet = ((X > 100) & (X < 200)) | ((X > 500) & (X < 600))
    depth = np.where(wet & (np.abs(Y) < 60), 1.0, np.nan)
    fields = {"depth": depth, "velocity": np.where(np.isfinite(depth), 0.2, np.nan),
              "arrival": np.where(np.isfinite(depth), 2.0, np.nan)}
    rec = impmod.Receptors()
    rec.add_lines([np.array([[0.0, 0.0], [780.0, 0.0]])], impmod.KIND_ROAD, ids=["US 2"],
                  unit_m=1.0)
    rec.add_lines([np.array([[0.0, 300.0], [780.0, 300.0]])], impmod.KIND_ROAD, ids=["dry"],
                  unit_m=1.0)
    rows = crmod.crossing_rows(rec, g, fields, "us", sim_end_hr=6.0)
    assert [r["id"] for r in rows] == ["US 2", "US 2"], rows
    assert {r["crossing"] for r in rows} == {1, 2}
    assert all(r["closed"] == "yes" for r in rows)    # 1 ft deep is over the 0.5 ft rule
    assert crmod.summary(rows)["crossings"] == 2


# ======================================================== item 4 (feature): cascade check
@test
def v3200_cascade_check_finds_the_downstream_dam_and_records_the_inflow():
    from nd_breach_runner.core import cascadecheck as cc
    d = _tmp()
    try:
        up, dn, far = (os.path.join(d, n) for n in ("DWR-1", "DWR-2", "DWR-3"))
        for f, lon, lat, name in ((up, -100.0, 47.35, "Upper"), (dn, -99.7, 47.1, "Lower"),
                                  (far, -98.0, 46.0, "Away")):
            os.makedirs(os.path.join(f, "inputs"), exist_ok=True)
            _dam_record(f, lon=lon, lat=lat, name=name, height_ft=30.0, storage_acft=500.0)
        ring = [[-100.0, 47.0], [-99.5, 47.0], [-99.5, 47.3], [-100.0, 47.3], [-100.0, 47.0]]
        json.dump({"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {},
             "geometry": {"type": "Polygon", "coordinates": [ring]}}]},
            open(os.path.join(up, cc.EXTENT_NAME), "w", encoding="utf-8"))
        hyd = os.path.join(up, "_breach_model", "breach_hydrograph.csv")
        os.makedirs(os.path.dirname(hyd), exist_ok=True)
        open(hyd, "w", encoding="utf-8").write("time_hr,discharge_cfs\n0,0\n1,5000\n2,1000\n")
        open(os.path.join(dn, "inputs", cc.PMF_NAME), "w", encoding="utf-8").write(
            "time_hr,discharge_cfs\n0,100\n1,9000\n2,500\n")
        assert sorted(x["dam_id"] for x in cc.dams_from_folders(d, "DWR-1")) == \
            ["DWR-2", "DWR-3"]
        info = cc.check(up, d, "DWR-1", source=(-100.0, 47.35))
        assert [h["dam_id"] for h in info["hits"]] == ["DWR-2"], info["hits"]
        assert "DWR-2" in info["text"] and not info["problem"]
        got = cc.write_jobs(up, info, d, hyd, "3.20.0")
        assert os.path.isfile(got["path"]) and got["path"].endswith(cc.JOB_NAME)
        jobs = json.load(open(got["path"], encoding="utf-8"))
        assert json.dumps(jobs).count("DWR-2") >= 1
        # the inflow is written into the DOWNSTREAM dam's own inputs folder ...
        inflow = os.path.join(dn, cc.INPUTS_DIR, cc.INFLOW_NAME)
        assert os.path.isfile(inflow)
        # ... its own PMF inflow is NOT replaced, and the sum is offered beside it
        assert open(os.path.join(dn, "inputs", cc.PMF_NAME), encoding="utf-8").read() == \
            "time_hr,discharge_cfs\n0,100\n1,9000\n2,500\n"
        summed = os.path.join(dn, cc.INPUTS_DIR, cc.SUM_NAME)
        assert os.path.isfile(summed)
        peaks = [float(ln.split(",")[1]) for ln in
                 open(summed, encoding="utf-8").read().strip().splitlines()[1:]]
        assert max(peaks) == 14000.0, peaks          # 9000 + 5000: a sum is never the milder
        lines = cc.report_lines(info, got)
        assert any(cc.NOT_CONSUMED.split(":")[0] in ln for ln in lines)
        assert "RECORDED, not used" in cc.NOT_CONSUMED
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3200_cascade_check_without_a_flood_extent_says_so_and_writes_nothing():
    from nd_breach_runner.core import cascadecheck as cc
    d = _tmp()
    try:
        up = os.path.join(d, "DWR-1")
        _dam_record(up, lon=-100.0, lat=47.35, height_ft=30.0)
        info = cc.check(up, d, "DWR-1", source=(-100.0, 47.35))
        assert info["hits"] == [] and info["problem"]
        assert "did not run" in info["text"]
        got = cc.write_jobs(up, info, d, "", "3.20.0")
        assert got["jobs"] == []
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3200_hydrographs_are_summed_on_the_union_of_their_times():
    from nd_breach_runner.core import cascadecheck as cc
    a = [(0.0, 10.0), (2.0, 30.0)]
    b = [(1.0, 100.0)]
    got = cc.add_hydrographs(a, b)
    times = [t for t, _q in got]
    assert times == [0.0, 1.0, 2.0], got
    at1 = dict(got)[1.0]
    assert at1 == 20.0 + 100.0, got           # A interpolated at 1 h plus B's own value


# ======================================================== item 14: crossings as gaps
@test
def v3200_a_road_embankment_over_a_crossing_is_reported_and_only_cut_when_asked():
    from nd_breach_runner.core import gaps as gp
    from nd_breach_runner.core.grid import Grid

    def read_dem(bounds, cell):
        x0, y0, x1, y1 = bounds
        nx = max(4, int((x1 - x0) / cell))
        ny = max(4, int((y1 - y0) / cell))
        g = Grid(np.zeros((ny, nx)), x0, y1, cell)
        _X, Y = np.meshgrid(g.x_centers(), g.y_centers())
        return Grid(np.where(np.abs(Y) < 8.0, 103.0, 100.0), x0, y1, cell)

    roads = [np.array([[-200.0, 0.0], [200.0, 0.0]])]
    crossings = [{"name": "NBI 1", "x": 0.0, "y": 2.0, "kind": "bridge", "span_ft": 40.0,
                  "rise_ft": 8.0, "source": "NBI"},
                 {"name": "No survey", "x": 60.0, "y": 1.0, "kind": "culvert"},
                 {"name": "Off road", "x": 0.0, "y": 500.0, "kind": "bridge"}]
    rows = gp.blocking_rows(crossings, roads, read_dem, 3.0)
    assert len(rows) == 3
    byname = {r["name"]: r for r in rows}
    assert byname["NBI 1"]["on_road"] and byname["NBI 1"]["surveyed"]
    assert 9.0 < byname["NBI 1"]["blocks_ft"] < 11.0      # 3 m of embankment, in feet
    assert not byname["No survey"]["surveyed"] and "no surveyed span" in \
        byname["No survey"]["reason"]
    assert byname["Off road"]["blocks_ft"] is None and not byname["Off road"]["on_road"]
    assert "within" in byname["Off road"]["reason"]
    assert {r["name"] for r in gp.blocking(rows)} == {"NBI 1", "No survey"}
    # only the surveyed one becomes a Structure, and it carries the SURVEYED opening
    structs, notes = gp.structures_from(rows, 3.0)
    assert len(structs) == 1 and structs[0].kind == "bridge"
    assert abs(structs[0].width_m - 40.0 * 0.3048) < 1e-6
    assert abs(structs[0].height_m - 8.0 * 0.3048) < 1e-6
    assert any("No survey" in n for n in notes)
    # the report section is written whether or not the gaps were cut, and says which
    off = "\n".join(gp.markdown_lines(rows, cut=False))
    assert "Not cut" in off and "lowers the water behind it" in off
    on = "\n".join(gp.markdown_lines(rows, cut=True))
    assert "NBI 1" in on and "Not cut" not in on.split("| Crossing")[0]


@test
def v3200_the_run_reports_the_crossings_whether_or_not_it_cut_them():
    from nd_breach_runner.core import workflow as wf
    st = wf.RunSettings(cell_size_m=20.0)
    assert st.crossings == [] and st.crossing_gaps is False
    # a 3.19.x checkpoint still resumes: the new keys are popped while at their defaults
    key = wf.key_settings(st)
    assert "crossings" not in key and "crossing_gaps" not in key
    st.crossing_gaps = True
    assert wf.key_settings(st).get("crossing_gaps") is True
    # the report section comes from gaps.markdown_lines
    import inspect
    src = inspect.getsource(wf.render_report)
    assert "crossing_gaps" in src and "markdown_lines" in src


# ======================================================== item 15: two runs compared
@test
def v3200_two_runs_are_compared_on_the_finer_grid():
    from nd_breach_runner.core import compareruns as cr
    a = _grid(10, 10, 10.0, 2.0)
    b = _grid(20, 20, 5.0, 3.0)
    diff, stats = cr.difference(a, b)
    assert diff.dx == 5.0 and diff.data.shape == (20, 20)
    assert stats["cell_m"] == 5.0 and stats["coarse_cell_m"] == 10.0
    assert stats["max_increase_ft"] == 1.0 and stats["mean_change_ft"] == 1.0
    assert stats["a_flooded_ac"] > 0 and stats["b_flooded_ac"] > 0
    assert np.allclose(diff.data, 1.0)
    # dry in both runs is no data, not a zero difference
    dry = _grid(10, 10, 10.0, np.nan)
    d2, s2 = cr.difference(dry, dry)
    assert not np.isfinite(d2.data).any() and s2["cells_compared"] == 0


@test
def v3200_which_structures_change_danger_zone():
    from nd_breach_runner.core import compareruns as cr, nd_hazard as nh
    a = _grid(20, 20, 5.0, 1.0)
    b = _grid(20, 20, 5.0, 9.0)
    av = _grid(20, 20, 5.0, 0.2)
    bv = _grid(20, 20, 5.0, 6.0)
    pts = [{"id": "S1", "x": 25.0, "y": 10.0, "mobile": False}]
    rows, counts = cr.zone_changes(pts, {"depth": a, "velocity": av},
                                   {"depth": b, "velocity": bv})
    assert len(rows) == 1 and rows[0]["a_depth_ft"] == 1.0 and rows[0]["b_depth_ft"] == 9.0
    assert rows[0]["depth_change_ft"] == 8.0
    # with no signed-off charts the zones are empty and the counts say why
    assert counts["zones"] is False and rows[0]["a_zone"] == ""
    charts = nh.load_charts()
    rows2, counts2 = cr.zone_changes(pts, {"depth": a, "velocity": av},
                                     {"depth": b, "velocity": bv}, charts)
    if counts2["zones"]:                         # charts signed off in this tree
        assert rows2[0]["change"] and counts2["changed"] == 1 and counts2["raised"] == 1
    else:
        assert counts2.get("chart_problem")
    assert cr.changed_only(rows) == []


@test
def v3200_the_comparison_writes_its_report_and_warns_about_two_different_dams():
    from nd_breach_runner.core import compareruns as cr, workflow as wf
    d = _tmp()
    try:
        a_folder, b_folder = os.path.join(d, "A"), os.path.join(d, "B")
        for f, dam, cell in ((a_folder, "DWR-1", 20.0), (b_folder, "DWR-2", 10.0)):
            os.makedirs(os.path.join(f, wf.MODEL_FOLDER))
            json.dump({"summary": {"dam_id": dam, "cell_m": cell, "scenario": "sunny",
                                   "max_depth_ft": 9.0, "sim_time_hr": 12.0}},
                      open(cr.report_path(f), "w", encoding="utf-8"))
        info, diff = cr.compare(a_folder, b_folder, lambda p: None)
        assert diff is None                        # neither folder has a depth raster
        assert any("DIFFERENT dams" in w for w in info["warnings"]), info["warnings"]
        assert info["a"]["dam_id"] == "DWR-1" and info["b"]["cell_m"] == 10.0
        info["rows"], info["counts"] = [], {"structures": 0, "changed": 0}
        out = cr.write_outputs(os.path.join(d, "out"), info)
        for k in ("csv", "json", "markdown"):
            assert os.path.isfile(out[k]), k
        md = open(out["markdown"], encoding="utf-8").read()
        assert "DWR-1" in md and "DWR-2" in md
        assert "Run Comparison" in cr.ROWS_NAME and cr.DIFF_NAME.endswith(".tif")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ======================================================== item 16: the queue
@test
def v3200_the_queue_reads_a_priority_list_and_resumes_where_it_stopped():
    from nd_breach_runner.core import batchqueue as bq
    d = _tmp()
    try:
        lst = os.path.join(d, "priority.csv")
        open(lst, "w", encoding="utf-8").write(
            "Rank,Dam ID,Name\n1,DWR-1,A\n2,DWR-2,B\n3,DWR-1,a repeat\n")
        ids, notes = bq.read_list(lst)
        assert ids == ["DWR-1", "DWR-2"], ids
        assert "Dam ID" in notes and "repeated" in notes, notes
        # a plain text list works too, and the order is kept
        txt = os.path.join(d, "ids.txt")
        open(txt, "w", encoding="utf-8").write("DWR-9\n# a comment\nDWR-3\n\n")
        assert bq.read_list(txt)[0] == ["DWR-9", "DWR-3"]
        q, note = bq.resume(d, ids, "priority.csv", 2, version="3.20.0")
        assert "new queue" in note and bq.pending(q) == ["DWR-1", "DWR-2"]
        assert q["workers"] == 2 and q["threads_per_dam"] == 1
        bq.mark(q, "DWR-1", bq.DONE, "peak 5000 cfs", 3.2)
        path = bq.save(q)
        assert os.path.isfile(path) and path.endswith(bq.QUEUE_NAME)
        # a new session picks it up and only the unfinished dams are left
        q2, note2 = bq.resume(d, ids + ["DWR-7"], "priority.csv", 2)
        assert "resumed" in note2 and "1 new dam" in note2
        assert bq.pending(q2) == ["DWR-2", "DWR-7"]
        assert bq.summary(q2)[bq.DONE] == 1
        # a dam left "running" by a crash is queued again, not lost
        bq.mark(q2, "DWR-2", bq.RUNNING)
        assert "DWR-2" in bq.pending(q2)
        assert bq.item(q2, "DWR-2")["status"] == bq.WAITING
        csv_path = bq.write_csv(os.path.join(d, "q.csv"), q2)
        head = open(csv_path, encoding="utf-8").readline().strip()
        assert head == ",".join(bq.ROW_COLUMNS)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3200_the_queue_skips_dams_that_are_already_run():
    from nd_breach_runner.core import batchqueue as bq, workflow as wf

    def _finish(folder):
        for k in ("depth", "velocity", "arrival"):
            open(os.path.join(folder, wf.FINAL_NAMES[k]), "wb").write(b"tif")
        rep = os.path.join(folder, wf.RUN_REPORT_REL)
        os.makedirs(os.path.dirname(rep), exist_ok=True)
        with open(rep, "w") as f:
            json.dump({"run_finished": "2026-10-14T00:00:00"}, f)

    d = _tmp()
    try:
        done = os.path.join(d, "DWR-1")
        os.makedirs(done)
        _finish(done)
        # 3.21.0: DWR-3 has the pass rasters but NO run report (a cancelled run). It is NOT
        # "done" and must be run again, not skipped as the old depth-file-only test asserted.
        cut = os.path.join(d, "DWR-3")
        os.makedirs(cut)
        for k in ("depth", "velocity", "arrival"):
            open(os.path.join(cut, wf.FINAL_NAMES[k]), "wb").write(b"tif")
        assert bq.is_done(d, "DWR-1")
        assert not bq.is_done(d, "DWR-2")
        assert not bq.is_done(d, "DWR-3")
        q, _n = bq.resume(d, ["DWR-1", "DWR-2", "DWR-3"], "", 2)
        left = bq.pending(q, bq.done_checker(d))
        assert left == ["DWR-2", "DWR-3"], left
        assert bq.item(q, "DWR-1")["status"] == bq.SKIPPED
        assert "already run" in bq.item(q, "DWR-1")["message"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3200_two_dams_at_one_thread_is_the_default_and_oversubscription_warns():
    from nd_breach_runner.core import batchqueue as bq
    assert bq.DEFAULT_WORKERS == 2 and bq.THREADS_PER_DAM == 1
    assert "1.76" in bq.SCALING_NOTE
    fine = bq.worker_note(2, 1, cores=8)
    assert "2 dam" in fine and "WARNING" not in fine
    too_many = bq.worker_note(16, 1, cores=2)
    assert "WARNING" in too_many and "oversubscrib" in too_many


# ======================================================== the Processing tools
@test
def v3200_the_new_tools_are_defined_like_the_others():
    algs, mod, fq = _algs()
    names = [c().name() for c in algs.ALGORITHMS]
    for n in ("crossings", "cascadecheck", "compareruns", "queue", "signrates"):
        assert names.count(n) == 1, (n, names.count(n))
    for cls in mod.RELEASE320_ALGORITHMS:
        a = cls()
        a.initAlgorithm()
        assert a.name() and a.displayName() and a.shortHelpString()
        assert len(a.params) >= 2, a.name()
        assert a.group() and a.groupId()


@test
def v3200_the_new_tools_are_in_the_window_and_fill_what_they_can():
    from nd_breach_runner import popup_logic as pl
    tabs = {t: [x[0] for x in tools] for t, tools in pl.TABS}
    assert "queue" in tabs["Dam"] and "cascadecheck" in tabs["Model"]
    assert "crossings" in tabs["Results"] and "compareruns" in tabs["Results"]
    assert "signrates" in tabs["Checks"]
    d = _tmp()
    try:
        folder = os.path.join(d, "DWR-1")
        os.makedirs(folder)
        open(os.path.join(folder, "dam.json"), "w", encoding="utf-8").write("{}")
        p = pl.defaults("crossings", d, "DWR-1", {"dem": ""})
        assert p.get("DAM_FOLDER") == folder
        p = pl.defaults("cascadecheck", d, "DWR-1", {"dem": ""})
        assert p.get("DAM_FOLDER") == folder and p.get("DAMS_FOLDER") == d
        p = pl.defaults("compareruns", d, "DWR-1", {"dem": ""})
        assert p.get("FOLDER_B") == folder and "FOLDER_A" not in p
        p = pl.defaults("queue", d, "DWR-1", {"dem": ""})
        assert p.get("DAMS_FOLDER") == d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3200_the_crossings_layer_reaches_the_run_settings_as_plain_dicts():
    algs, _mod, fq = _algs()
    a = algs.RunDamAlgorithm()
    a.initAlgorithm()
    for name in ("CROSSINGS", "CROSSING_GAPS", "X_SPAN", "X_RISE"):
        assert a.parameterDefinition(name) is not None, name
    feats = [fq.FakeFeature(fq.FakeGeometry("point", [[(100.0, 0.0)]]),
                            name="NBI 1", span_ft=40.0, rise_ft=8.0, type="bridge"),
             fq.FakeFeature(fq.FakeGeometry("point", [[(200.0, 0.0)]]), name="culvert 2",
                            type="culvert")]
    src = fq.FakeSource(feats)
    st = algs.workflow.RunSettings(cell_size_m=20.0)
    notes = a.apply_crossings(st, {"CROSSINGS": src, "CROSSING_GAPS": False},
                              fq.QgsProcessingContext(),
                              lambda s: (fq.QgsCoordinateTransform(),
                                         fq.QgsCoordinateTransform()),
                              (0.0, 0.0), 5000.0)
    assert len(st.crossings) == 2, st.crossings
    first = [c for c in st.crossings if c["name"] == "NBI 1"][0]
    assert first["span_ft"] == 40.0 and first["rise_ft"] == 8.0 and first["kind"] == "bridge"
    second = [c for c in st.crossings if c["name"] == "culvert 2"][0]
    assert second["span_ft"] is None and second["kind"] == "culvert"
    assert st.crossing_gaps is False
    assert notes and "1 with a surveyed span and rise" in notes[0]
    assert "only reported" in notes[0]
    # with the option on the note says the surveyed one will be cut
    notes = a.apply_crossings(st, {"CROSSINGS": src, "CROSSING_GAPS": True},
                              fq.QgsProcessingContext(),
                              lambda s: (fq.QgsCoordinateTransform(),
                                         fq.QgsCoordinateTransform()),
                              (0.0, 0.0), 5000.0)
    assert st.crossing_gaps is True and "only reported" not in notes[0]


@test
def v3200_the_crossings_layer_is_filled_from_the_offline_pack():
    import importlib
    from nd_breach_runner.tests import fake_qgis as fq
    fq.install()
    import nd_breach_runner.provider as prov
    importlib.reload(prov)
    mapping, _fields, _req, _folder = prov.DATA_INPUTS["rundam"]
    assert mapping["CROSSINGS"] == "bridges"
    from nd_breach_runner.damsuite import services as SV
    assert "bridges" in SV.ROLES


@test
def v3200_the_batch_tool_runs_two_dams_at_one_thread_by_default():
    algs, _mod, fq = _algs()
    a = algs.BatchAlgorithm()
    a.initAlgorithm()
    p = a.parameterDefinition("WORKERS")
    assert p.kwargs.get("defaultValue") == 2, p.kwargs
    assert "1.76" in p.description


@test
def v3200_sign_off_writes_the_rate_table_and_says_what_it_cannot_confirm():
    _algs_mod, mod, fq = _algs()
    d = _tmp()
    try:
        out = os.path.join(d, "rates.json")
        a = mod.SignRatesAlgorithm()
        a.initAlgorithm()
        fb = fq.QgsProcessingFeedback()
        rates = ", ".join("{}/{} = {}".format(s, b, v) for s, vals in
                          (("high", (0.9, 0.6, 0.2)), ("medium", (0.1, 0.03, 0.01)),
                           ("low", (0.01, 0.005, 0.0002)))
                          for b, v in zip(("under15", "15to60", "over60"), vals))
        got = a.processAlgorithm({"RATES": rates, "SIGNED_OFF_BY": "A. Engineer, PE",
                                 "CHECKED_BY": "", "SOURCE": "A published table, 2024",
                                 "SIGNED_ON": "", "NOTES": "", "OUTPUT": out},
                                fq.QgsProcessingContext(), fb)
        assert got["RATE_FILE"] == out and os.path.isfile(out)
        assert got["STATUS"] == "signed off"
        assert any("signed off by A. Engineer, PE" in i for i in fb.info), fb.info
        assert any("no second checker" in i for i in fb.info), fb.info
        assert any("SHA-256" in i for i in fb.info)
        d2 = json.load(open(out, encoding="utf-8"))
        assert d2["signed_off_by"] == "A. Engineer, PE"
        assert "cannot confirm" in d2["notes"]
        assert d2["rates"]["high"]["under15"] == 0.9
        # a draft: no name, so nothing is signed and the warning says loss of life is not
        # estimated
        fb2 = fq.QgsProcessingFeedback()
        draft = os.path.join(d, "draft.json")
        got2 = a.processAlgorithm({"RATES": "high/under15 = 0.9", "SIGNED_OFF_BY": "",
                                   "CHECKED_BY": "", "SOURCE": "", "SIGNED_ON": "",
                                   "NOTES": "", "OUTPUT": draft},
                                  fq.QgsProcessingContext(), fb2)
        assert got2["STATUS"] == "not signed off"
        assert any("8 of 9 cell" in w for w in fb2.errors), fb2.errors
        assert any("NOT estimated" in i for i in fb2.info), fb2.info
        # a bad date is a tool error, not a silently wrong file
        try:
            a.processAlgorithm({"RATES": "", "SIGNED_OFF_BY": "A", "CHECKED_BY": "",
                                "SOURCE": "", "SIGNED_ON": "last tuesday", "NOTES": "",
                                "OUTPUT": os.path.join(d, "bad.json")},
                               fq.QgsProcessingContext(), fq.QgsProcessingFeedback())
            raise AssertionError("expected a QgsProcessingException")
        except Exception as e:                                   # noqa
            assert "YYYY-MM-DD" in str(e), e
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3200_the_sign_off_text_parser_takes_what_a_person_would_type():
    _a, mod, _fq = _algs()
    got, bad = mod.SignRatesAlgorithm.parse_text(
        "high/under15 = 0.9\nhigh/15to60: 0.6, medium/over60=0.01")
    assert got == {"high/under15": "0.9", "high/15to60": "0.6", "medium/over60": "0.01"}
    assert bad == []
    got, bad = mod.SignRatesAlgorithm.parse_text("nonsense, high/under15 = 0.9")
    assert bad == ["nonsense"] and got == {"high/under15": "0.9"}
    assert mod.SignRatesAlgorithm.parse_text("") == ({}, [])


@test
def v3200_the_release_version_is_consistent_everywhere_it_is_written():
    from nd_breach_runner.core import workflow as wf
    from nd_breach_runner.damsuite import versions
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(os.path.join(PLUGIN, "metadata.txt"), encoding="utf-8")
    md = cp["general"]["version"].strip()
    assert md == "3.27.0" == wf.VERSION == wf.VERSION_FALLBACK
    assert versions.RELEASE["ndbreach"] == md
    changelog = cp["general"]["changelog"]
    assert changelog.strip().startswith("3.27.0 ") and "3.26.9 - " in changelog
    for word in ("crossing", "cascade", "queue", "population at risk"):
        assert word in changelog.lower().split("3.19.0")[0], word


# ======================================================== the tools end to end (real GDAL)
def _write_tif(path, array, cell=10.0, x0=0.0, y_top=0.0, epsg=26914, nodata=-9999.0):
    from osgeo import gdal, osr
    a = np.where(np.isfinite(array), array, nodata).astype(np.float32)
    ds = gdal.GetDriverByName("GTiff").Create(path, a.shape[1], a.shape[0], 1, gdal.GDT_Float32)
    ds.SetGeoTransform((x0, cell, 0.0, y_top, 0.0, -cell))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    ds.SetProjection(srs.ExportToWkt())
    b = ds.GetRasterBand(1)
    b.SetNoDataValue(nodata)
    b.WriteArray(a)
    b.FlushCache()
    ds = None
    return path


def _dam_folder_with_rasters(folder, depth=3.0, velocity=4.0, arrival=1.5, nx=60, ny=40,
                             cell=10.0):
    """A dam folder with the three final rasters: a flood band 100 - 400 m east, |y| < 60 m."""
    from nd_breach_runner.core import workflow as wf
    os.makedirs(folder, exist_ok=True)
    y_top = ny * cell / 2.0
    xs = x0 = 0.0
    X, Y = np.meshgrid(np.arange(nx) * cell + cell / 2.0 + x0,
                       y_top - (np.arange(ny) * cell + cell / 2.0))
    band = (X > 100) & (X < 400) & (np.abs(Y) < 60)
    for key, value in (("depth", depth), ("velocity", velocity), ("arrival", arrival)):
        _write_tif(os.path.join(folder, wf.FINAL_NAMES[key]),
                   np.where(band, value, np.nan), cell, x0, y_top)
    assert xs == 0.0
    return folder


@test
def v3200_the_crossings_tool_writes_the_table_from_a_dam_folder():
    algs, mod, fq = _algs()
    from nd_breach_runner.core import crossings as crmod, workflow as wf
    d = _tmp()
    try:
        folder = _dam_folder_with_rasters(os.path.join(d, "DWR-1"))
        os.makedirs(os.path.join(folder, wf.MODEL_FOLDER))
        json.dump({"summary": {"dam_id": "DWR-1", "sim_time_hr": 12.0}},
                  open(os.path.join(folder, wf.MODEL_FOLDER, "run_report.json"), "w",
                       encoding="utf-8"))
        roads = fq.FakeSource([fq.FakeFeature(
            fq.FakeGeometry("line", [[(0.0, 0.0), (580.0, 0.0)]]), name="CR 12")])
        rail = fq.FakeSource([fq.FakeFeature(
            fq.FakeGeometry("line", [[(250.0, -150.0), (250.0, 150.0)]]), name="BNSF")])
        a = mod.CrossingsAlgorithm()
        a.initAlgorithm()
        fb = fq.QgsProcessingFeedback()
        got = a.processAlgorithm({"DAM_FOLDER": folder, "ROADS": roads, "RAIL": rail,
                                  "R_NAME": "name", "RAIL_NAME": "name", "R_AADT": "",
                                  "WET_FT": crmod.WET_FT,
                                  "CLOSURE_FT": crmod.CLOSURE_DEPTH_FT,
                                  "CLOSURE_DV": crmod.CLOSURE_DV_FT2S,
                                  "WRITE_GEOJSON": True},
                                 fq.QgsProcessingContext(), fb)
        assert os.path.isfile(got["CSV"]) and got["CSV"].endswith(crmod.OUTPUT_NAMES["csv"])
        assert os.path.isfile(got["GEOJSON"]) and os.path.isfile(got["REPORT"])
        assert got["CLOSED"] == 2, got
        rows = open(got["CSV"], encoding="utf-8").read().strip().splitlines()
        assert len(rows) == 3                         # the header and two crossings
        assert "CR 12" in rows[1] + rows[2] and "BNSF" in rows[1] + rows[2]
        # the simulated time came out of the run report, so the duration is bounded, not unknown
        assert any("Simulated time from the run report" in i for i in fb.info), fb.info
        assert any("UPPER bound" in w and "not a measurement" in w for w in fb.errors), \
            fb.errors
        md = open(got["REPORT"], encoding="utf-8").read()
        assert "not a legal closure" in md
        # no road layer at all is a clear tool error, not an empty table
        try:
            a.processAlgorithm({"DAM_FOLDER": folder, "ROADS": None, "RAIL": None,
                                "R_NAME": "", "RAIL_NAME": "", "R_AADT": "",
                                "WET_FT": 0.1, "CLOSURE_FT": 0.5, "CLOSURE_DV": 7.0,
                                "WRITE_GEOJSON": False},
                               fq.QgsProcessingContext(), fq.QgsProcessingFeedback())
            raise AssertionError("expected a QgsProcessingException")
        except Exception as e:                                   # noqa
            assert "No road or railroad line" in str(e), e
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3200_the_compare_tool_writes_a_difference_raster_and_the_changed_structures():
    algs, mod, fq = _algs()
    from nd_breach_runner.core import compareruns as cr, workflow as wf
    d = _tmp()
    try:
        a_folder = _dam_folder_with_rasters(os.path.join(d, "A"), depth=2.0, velocity=1.0)
        b_folder = _dam_folder_with_rasters(os.path.join(d, "B"), depth=9.0, velocity=6.0)
        for f, cell in ((a_folder, 20.0), (b_folder, 10.0)):
            os.makedirs(os.path.join(f, wf.MODEL_FOLDER), exist_ok=True)
            json.dump({"summary": {"dam_id": "DWR-1", "cell_m": cell, "scenario": "sunny"}},
                      open(cr.report_path(f), "w", encoding="utf-8"))
        structures = fq.FakeSource([fq.FakeFeature(
            fq.FakeGeometry("point", [[(200.0, 0.0)]]), id="S1", occtype="RES1")])
        a = mod.CompareRunsAlgorithm()
        a.initAlgorithm()
        fb = fq.QgsProcessingFeedback()
        got = a.processAlgorithm({"FOLDER_A": a_folder, "FOLDER_B": b_folder,
                                  "STRUCTURES": structures, "S_ID": "id", "S_MOBILE": "",
                                  "CHARTS": "", "OUT_FOLDER": "", "WRITE_DIFF": True},
                                 fq.QgsProcessingContext(), fb)
        assert os.path.isfile(got["REPORT"]) and os.path.isfile(got["CSV"])
        assert os.path.isfile(got["DIFFERENCE"])
        assert got["FOLDER"] == os.path.join(b_folder, "_compare")
        assert "deepest increase 7.0 ft" in got["RESULT"], got["RESULT"]
        body = open(got["CSV"], encoding="utf-8").read()
        assert "S1" in body and "7.0" in body
        from nd_breach_runner.core import gdal_io
        diff = gdal_io.read_grid(got["DIFFERENCE"])
        wet = diff.data[np.isfinite(diff.data)]
        assert len(wet) and abs(float(wet.max()) - 7.0) < 1e-4
        # the same folder twice is refused rather than compared with itself
        try:
            a.processAlgorithm({"FOLDER_A": a_folder, "FOLDER_B": a_folder,
                                "STRUCTURES": None, "S_ID": "", "S_MOBILE": "",
                                "CHARTS": "", "OUT_FOLDER": "", "WRITE_DIFF": False},
                               fq.QgsProcessingContext(), fq.QgsProcessingFeedback())
            raise AssertionError("expected a QgsProcessingException")
        except Exception as e:                                   # noqa
            assert "same folder" in str(e), e
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3200_the_cascade_tool_writes_the_next_job_and_says_it_is_not_consumed():
    algs, mod, fq = _algs()
    from nd_breach_runner.core import cascadecheck as cc, workflow as wf
    d = _tmp()
    try:
        up = _dam_folder_with_rasters(os.path.join(d, "DWR-1"))
        dn = os.path.join(d, "DWR-2")
        os.makedirs(os.path.join(dn, "inputs"))
        _dam_record(up, lon=-100.0, lat=47.35, height_ft=30.0, storage_acft=500.0)
        _dam_record(dn, lon=-99.7, lat=47.1, height_ft=20.0, storage_acft=100.0)
        # the flood extent in longitude / latitude (the tool transforms when it has a CRS;
        # the fake transform is the identity, so the extent is written in lon / lat here)
        ring = [[-100.0, 47.0], [-99.5, 47.0], [-99.5, 47.3], [-100.0, 47.3], [-100.0, 47.0]]
        json.dump({"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {},
             "geometry": {"type": "Polygon", "coordinates": [ring]}}]},
            open(os.path.join(up, cc.EXTENT_NAME), "w", encoding="utf-8"))
        hyd = os.path.join(up, wf.MODEL_FOLDER, "breach_hydrograph.csv")
        os.makedirs(os.path.dirname(hyd), exist_ok=True)
        open(hyd, "w", encoding="utf-8").write("time_hr,discharge_cfs\n0,0\n1,5000\n2,1000\n")
        a = mod.CascadeCheckAlgorithm()
        a.initAlgorithm()
        fb = fq.QgsProcessingFeedback()
        got = a.processAlgorithm({"DAM_FOLDER": up, "DAMS_FOLDER": d, "DAMS": None,
                                  "D_ID": "", "D_NAME": "", "HYDROGRAPH": "",
                                  "WRITE_JOBS": True},
                                 fq.QgsProcessingContext(), fb)
        assert got["DAMS_FOUND"] == 1, got
        assert os.path.isfile(got["JOBS"]) and got["JOBS"].endswith(cc.JOB_NAME)
        assert os.path.isfile(os.path.join(dn, cc.INPUTS_DIR, cc.INFLOW_NAME))
        assert "DWR-2" in got["RESULT"]
        assert any("RECORDED, not used" in w for w in fb.errors), fb.errors
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3200_the_queue_tool_runs_each_dam_once_and_records_what_happened():
    """The queue is driven with workflow.run_dam stubbed: what is tested here is the queue
    bookkeeping and the cancel / resume path, not the flood model."""
    algs, mod, fq = _algs()
    from nd_breach_runner.core import batchqueue as bq
    import types
    d = _tmp()
    real_run = algs.workflow.run_dam
    prepared = []

    class _Res:
        def __init__(self, dam_id):
            self.summary = {"breach_occurs": True, "peak_breach_cfs": 5000.0,
                            "flooded_acres": 12.0, "dam_id": dam_id}
            self.warnings = []

    try:
        a = mod.QueueAlgorithm()
        a.initAlgorithm()
        for name in ("DAM_LIST", "WORKERS", "RESTART", "SKIP_DONE", "DAMS_FOLDER", "DEM"):
            assert a.parameterDefinition(name) is not None, name
        lst = os.path.join(d, "ids.txt")
        open(lst, "w", encoding="utf-8").write("DWR-1\nDWR-2\nboom\n")

        tried = []

        def fake_one(dam_id, parameters, context, feedback, fb):
            tried.append(dam_id)
            if dam_id == "boom":
                raise algs.brmod.BreachInputError("no dam height")
            prepared.append(dam_id)
            return dam_id, (dam_id, (0.0, 0.0), None, None, None,
                            os.path.join(d, dam_id), None)

        def fake_run_dam(dam, xy, st, src, writer, folder, inflow, progress=None, log=None,
                         cancel=None):
            return _Res(dam)
        a._one = fake_one
        algs.workflow.run_dam = fake_run_dam
        mod.workflow.run_dam = fake_run_dam
        fb = fq.QgsProcessingFeedback()
        got = a.processAlgorithm({"DAMS_FOLDER": d, "DAM_LIST": lst, "WORKERS": 1,
                                  "RESTART": False, "SKIP_DONE": True},
                                 fq.QgsProcessingContext(), fb)
        assert prepared == ["DWR-1", "DWR-2"] == tried[:2], (prepared, tried)
        q, problem = bq.load(d)
        assert not problem and q is not None
        assert bq.item(q, "DWR-1")["status"] == bq.DONE
        assert "peak 5000" in bq.item(q, "DWR-1")["message"]
        assert bq.item(q, "boom")["status"] == bq.FAILED
        assert "no dam height" in bq.item(q, "boom")["message"]
        assert "1 still to run" not in got["COUNTS"]
        assert os.path.isfile(got["QUEUE"]) and os.path.isfile(got["SUMMARY"])
        assert any("1.76" in i for i in fb.info), fb.info
        # run it again: the two finished dams are not run a second time, and the one that
        # failed is tried again (it fails again here, which is recorded, not lost)
        prepared[:] = []
        tried[:] = []
        fb2 = fq.QgsProcessingFeedback()
        got2 = a.processAlgorithm({"DAMS_FOLDER": d, "DAM_LIST": lst, "WORKERS": 1,
                                   "RESTART": False, "SKIP_DONE": True},
                                  fq.QgsProcessingContext(), fb2)
        assert tried == ["boom"] and prepared == [], (tried, prepared)
        assert any("no dam height" in e for e in fb2.errors), fb2.errors
        assert "1 failed" in got2["COUNTS"], got2["COUNTS"]
        # an empty list is a tool error with the two formats named
        open(lst, "w", encoding="utf-8").write("\n")
        try:
            a.processAlgorithm({"DAMS_FOLDER": d, "DAM_LIST": lst, "WORKERS": 1,
                                "RESTART": True, "SKIP_DONE": True},
                               fq.QgsProcessingContext(), fq.QgsProcessingFeedback())
            raise AssertionError("expected a QgsProcessingException")
        except Exception as e:                                   # noqa
            assert "Dam ID" in str(e), e
    finally:
        algs.workflow.run_dam = real_run
        mod.workflow.run_dam = real_run
        shutil.rmtree(d, ignore_errors=True)


@test
def v3200_the_consequences_tool_takes_a_signed_rate_table_and_logs_the_par():
    algs, _mod, fq = _algs()
    import importlib
    import nd_breach_runner.algorithms_consequences as ac
    importlib.reload(ac)
    a = ac.ConsequencesAlgorithm()
    a.initAlgorithm()
    assert a.parameterDefinition("RATE_FILE") is not None
    desc = a.parameterDefinition("RATE_FILE").description
    assert "fatality_rates.json" in desc and "Sign off" in desc
    # the log lines: PAR always, loss of life only with a usable table, and a failure inside
    # the addition is a note rather than the loss of the estimate
    fb = fq.QgsProcessingFeedback()
    ac.ConsequencesAlgorithm._report_par(
        {"par": {"night": {"par": 7.0}, "day": {"par": 1.0},
                 "rates_text": "nobody has signed it off"},
         "crossings": {"crossings": 2, "closed": 1}}, fb)
    assert any("Population at risk: night 7" in i for i in fb.info), fb.info
    assert any("nobody has signed it off" in w for w in fb.errors), fb.errors
    assert any("Road and railroad crossings wet: 2" in i for i in fb.info), fb.info
    fb2 = fq.QgsProcessingFeedback()
    ac.ConsequencesAlgorithm._report_par({"par": {"problem": "ValueError: x"}}, fb2)
    assert any("estimate above is unaffected" in w for w in fb2.errors), fb2.errors
