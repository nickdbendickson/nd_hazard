"""The dam wizard with the real Qt of QGIS (not the fakes of the suite), offscreen: the
DamCard, the Required / Improves the estimate rows led by their marks, a problem shown on the
page (no message box), the DEM coverage line and the Advanced header.

    QT_QPA_PLATFORM=offscreen <QGIS python> nd_breach_runner/tests/realqt_wizard.py [shots_dir]

Not named test_*: run_tests.py imports every test_* module into its fake-Qt process. Opens no
Processing dialog and runs no model. With a folder argument it also saves screenshots.
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
RUNNER = os.path.dirname(HERE)
ROOT = tempfile.mkdtemp()
os.symlink(RUNNER, os.path.join(ROOT, "nd_breach_runner"))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.pop("NDSUITE_SETTINGS", None)
os.environ["HOME"] = tempfile.mkdtemp(prefix="realqt_wizard_home_")

from qgis.core import QgsApplication, QgsProject, QgsRasterLayer, QgsVectorLayer  # noqa: E402
_app = QgsApplication([], True)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", sys.prefix), True)
_app.initQgis()

from osgeo import gdal, ogr, osr  # noqa: E402
gdal.UseExceptions()
ogr.UseExceptions()
osr.UseExceptions()

from nd_breach_runner import wizard as WZ  # noqa: E402

SHOTS = sys.argv[1] if len(sys.argv) > 1 else ""
DATA = tempfile.mkdtemp(prefix="realqt_wizard_")


def pump():
    for _ in range(8):
        _app.processEvents()


def shot(win, name):
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        win.grab().save(os.path.join(SHOTS, name + ".png"))


def make_dams():
    """A dam layer whose fields the wizard matches by name: two dams, one without a storage."""
    path = os.path.join(DATA, "dams.gpkg")
    sr = osr.SpatialReference()
    sr.ImportFromEPSG(4326)
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(path)
    lyr = ds.CreateLayer("dams", sr, ogr.wkbPoint)
    cols = (("DAM_ID", ogr.OFTString), ("DAM_NAME", ogr.OFTString),
            ("HEIGHT_FT", ogr.OFTReal), ("STORAGE_ACFT", ogr.OFTReal))
    for name, kind in cols:
        lyr.CreateField(ogr.FieldDefn(name, kind))
    for vals, xy in ((("1046", "Homme Dam", 51.0, 5400.0), (-97.79, 48.41)),
                     (("2801", "Absaraka Dam", 24.0, None), (-97.39, 46.98))):
        f = ogr.Feature(lyr.GetLayerDefn())
        for (name, _k), v in zip(cols, vals):
            if v is not None:
                f.SetField(name, v)
        g = ogr.Geometry(ogr.wkbPoint)
        g.AddPoint_2D(*xy)
        f.SetGeometry(g)
        lyr.CreateFeature(f)
    ds = None
    return path + "|layername=dams"


def make_dem():
    """A 0.1 degree DEM around Homme Dam only."""
    import numpy as np
    path = os.path.join(DATA, "dem_homme.tif")
    ds = gdal.GetDriverByName("GTiff").Create(path, 100, 100, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((-97.85, 0.001, 0, 48.45, 0, -0.001))
    sr = osr.SpatialReference()
    sr.ImportFromEPSG(4326)
    ds.SetProjection(sr.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(np.full((100, 100), 300, dtype="float32"))
    ds = None
    return path


def main():
    dams = QgsVectorLayer(make_dams(), "dams", "ogr")
    dem = QgsRasterLayer(make_dem(), "dem_homme")
    assert dams.isValid() and dem.isValid()
    QgsProject.instance().addMapLayer(dams)
    QgsProject.instance().addMapLayer(dem)
    w = WZ._classes()["Wizard"](None)
    w.show()
    pump()
    assert w.windowTitle() == "ND Dam Breach Runner — Model one dam", w.windowTitle()
    pg = w.currentPage()
    pg.layer.setCurrentIndex(pg.layer.findData(dams.id()))
    pump()
    labels = [pg.feature.itemText(i) for i in range(pg.feature.count())]
    assert len(labels) == 2, labels
    # 1. the DamCard sums the dam up
    title = pg.card.title_label.text()
    assert "Homme Dam · 51 ft · 5,400 ac-ft" in title, title
    assert "ID 1046" in pg.card.path_label.text(), pg.card.path_label.text()
    # 2. every row is led by its mark: required filled = check, optional empty = circle
    assert pg.mark["HEIGHT_FT"].text() == "✓" and pg.mark["point"].text() == "✓"
    assert pg.mark["AREA_AC"].text() == "○", pg.mark["AREA_AC"].text()
    if pg.card.warn_label.isVisible():               # the page's warnings are amber, not red
        assert pg.card.warn_label.text().startswith("⚠"), pg.card.warn_label.text()
        print("dam warnings:", pg.card.warn_label.text().replace("\n", " | "))
    shot(w, "wizard_dam")
    # a dam with no storage: the required row is red, the card's warning is amber
    pg.feature.setCurrentIndex(1)
    pump()
    assert pg.mark["STORAGE_ACFT"].text() == "✕", pg.mark["STORAGE_ACFT"].text()
    assert "Absaraka Dam · 24 ft" in pg.card.title_label.text(), pg.card.title_label.text()
    shot(w, "wizard_dam_missing")
    pg.feature.setCurrentIndex(0)
    pump()
    # 3. a problem is shown in red on the page, Next stays on the page, no message box
    seen = []
    WZ._message = lambda *a, **k: seen.append(a)
    pg.value_edit["HEIGHT_FT"].setText("abc")
    pump()
    assert pg.mark["HEIGHT_FT"].text() == "✕"
    w.next()
    pump()
    assert w.currentId() == 0 and not seen, seen
    assert pg.problems.isVisible() and "not a number" in pg.problems.text(), pg.problems.text()
    shot(w, "wizard_dam_problem")
    pg.value_edit["HEIGHT_FT"].setText("51")
    w.next()
    pump()
    assert not pg.problems.isVisible()
    # 4. the terrain page: the coverage line and the Advanced header with its values
    tp = w.currentPage()
    tp.dem.setCurrentIndex(tp.dem.findData(dem.id()))
    pump()
    assert tp.coverage.text().startswith("✓"), tp.coverage.text()
    assert tp.advanced.title().startswith("Advanced") and "0.060" in tp.advanced.title()
    tp.n.setValue(0.08)
    assert "0.080" in tp.advanced.title(), tp.advanced.title()
    assert tp.dem_note.text() == ""                        # 3.18.0's note is still there
    shot(w, "wizard_terrain")
    # a dam the DEM does not cover: the line turns red
    w.answers["point"] = (-97.39, 46.98, "EPSG:4326")
    tp.show_coverage()
    assert tp.coverage.text().startswith("✕"), tp.coverage.text()
    w.close()
    print("realqt_wizard: all checks passed")
    return 0


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    except Exception:                                            # noqa
        import traceback
        traceback.print_exc()
    finally:
        shutil.rmtree(ROOT, ignore_errors=True)
        shutil.rmtree(DATA, ignore_errors=True)
        sys.stdout.flush()
    os._exit(code)
