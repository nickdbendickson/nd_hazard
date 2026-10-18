"""14 - Self-test in this QGIS: the synthetic valley through tools 1-8, 10 and 16 (core/selftest.py)."""
import os
import platform
import tempfile

from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterBoolean, QgsProcessingParameterNumber,
                       QgsProcessingParameterEnum, QgsProcessingParameterFileDestination,
                       QgsProcessingParameterFile,
                       QgsProcessingParameterFolderDestination, QgsProcessingException)

from ..core import selftest as st
from . import _common as cm
from . import run_breach_sim as rbs
from ..compat import FILE_FILE, NUM_DOUBLE

QVARIANT = {"string": "String", "double": "Double", "int": "Int"}


def qgis_layers(specs, crs="EPSG:5070"):
    """Memory layers from core.selftest.layer_specs() in a real QGIS."""
    from qgis.core import QgsVectorLayer, QgsFeature, QgsGeometry, QgsPointXY
    kinds = {"point": "Point", "line": "LineString", "polygon": "Polygon"}
    out = {}
    for name, (kind, fields, feats) in specs.items():
        uri = "{}?crs={}&".format(kinds[kind], crs) + "&".join(
            "field={}:{}".format(f, {"string": "string", "double": "double", "int": "integer"}[t]) for f, t in fields)
        lyr = QgsVectorLayer(uri, "selftest_" + name, "memory")
        if not lyr.isValid():
            raise QgsProcessingException("memory layer '{}' could not be created".format(name))
        pr = lyr.dataProvider()
        rows = []
        for (gkind, coords), attrs in feats:
            f = QgsFeature(lyr.fields())
            if gkind == "point":
                f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(*coords)))
            elif gkind == "line":
                f.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(*p) for p in coords]))
            else:
                f.setGeometry(QgsGeometry.fromPolygonXY([[QgsPointXY(*p) for p in coords]]))
            f.setAttributes([attrs.get(fn) for fn, _t in fields])
            rows.append(f)
        pr.addFeatures(rows)
        lyr.updateExtents()
        out[name] = lyr
    return out


def software_versions():
    v = {"python": platform.python_version()}
    try:
        from qgis.core import Qgis
        v["qgis"] = str(getattr(Qgis, "QGIS_VERSION", "") or getattr(Qgis, "version", lambda: "")())
    except Exception:                                            # noqa
        v["qgis"] = "not available (test double)"
    try:
        from osgeo import gdal
        v["gdal"] = gdal.VersionInfo("RELEASE_NAME")
    except Exception:                                            # noqa
        pass
    try:
        import numpy
        v["numpy"] = numpy.__version__
    except Exception:                                            # noqa
        pass
    try:
        from ..core import damrecord_io
        v["plugin"] = damrecord_io.plugin_version()
    except Exception:                                            # noqa
        pass
    return v


class SelfTest(QgsProcessingAlgorithm):
    def name(self): return "selftest"
    def displayName(self): return "14 - Self-test in this QGIS"
    def group(self): return "Dam screening"
    def groupId(self): return "dams"
    def createInstance(self): return SelfTest()

    def shortHelpString(self):
        return ("Builds a synthetic valley with one dam in a temporary folder (10 m DEM, 7 m embankment, pool, "
                "road, structures, railroad, stream, dam line, a DWR-style inventory row) and runs tools 1, 2 "
                "(local DEM folder), 3, 4, 5, 6, 7 (built-in engine, small grid), 8, 10 and 16 (ponds and dugouts, on the pool above the dam) one after the other "
                "through processing.run, checking that each wrote what the next one needs. PASS / FAIL per step "
                "with timings and the QGIS / GDAL / numpy versions go to the report. Run it first in a new QGIS "
                "or after a QGIS upgrade: a failure names the step and the error. It says nothing about model "
                "accuracy - nothing about the dam is real. Tools 0, 7b, 9 and 11-13 are not covered.\n\n"
                "This plugin was developed without a real QGIS (simulated QGIS / Qt only), so this self-test is "
                "the first thing that has to pass.")

    def initAlgorithm(self, config=None):
        P = self.addParameter
        P(QgsProcessingParameterEnum("ENGINE", "Flood engine for step 7", rbs.ENGINES, defaultValue=2))
        # 0.17.0: the LISFLOOD-FP engines (0 / 1) need the executable; it was never passed to step 7
        P(QgsProcessingParameterFile("EXE", "LISFLOOD-FP executable for step 7 (LISFLOOD engines only; "
                                     "empty = what tool 7 falls back to)", behavior=FILE_FILE,
                                     fileFilter=rbs.EXE_FILTER, optional=True))
        cm.distance_params(P, "CELL_FT", "CELL", "Model cell size for step 7 (ft; larger = quick)",
                           65.0, min_ft=16.0)
        P(QgsProcessingParameterNumber("SIMH", "Simulated time for step 7 (h)", NUM_DOUBLE,
                                       1.0, minValue=0.1))
        P(QgsProcessingParameterBoolean("KEEP", "Keep the temporary folder (to look at the outputs)",
                                        defaultValue=False))
        P(QgsProcessingParameterFolderDestination("WORK", "Work folder (empty = a temporary folder)",
                                                  optional=True))
        P(QgsProcessingParameterFileDestination("REPORT", "Report (text)", fileFilter="Text (*.txt)"))

    def processAlgorithm(self, params, context, feedback):
        from qgis import processing
        engine = self.parameterAsEnum(params, "ENGINE", context)
        exe = (self.parameterAsString(params, "EXE", context) or "").strip()
        cell = cm.distance_m(self, params, context, "CELL_FT", "CELL", 65.0)
        simh = self.parameterAsDouble(params, "SIMH", context)
        keep = self.parameterAsBool(params, "KEEP", context)
        work = self.parameterAsString(params, "WORK", context) or ""
        report = self.parameterAsFileOutput(params, "REPORT", context)
        if not work or work.upper() == "TEMPORARY_OUTPUT":
            work = tempfile.mkdtemp(prefix="nddam_selftest_")
            created_work = True                      # a temp folder the self-test owns outright
        else:
            created_work = not os.path.isdir(work)   # a user path we make fresh, else their own folder
        # Never delete the user's WORK root or anything it already held: snapshot the entries of a
        # folder we did not create so cleanup removes only what the self-test itself writes below.
        preexisting = set() if created_work else set(os.listdir(work))
        os.makedirs(work, exist_ok=True)
        dem_folder, _path = st.write_dem(os.path.join(work, "dem"))
        layers = self.make_layers(st.layer_specs())
        feedback.pushInfo("Synthetic world in {}".format(work))

        def run(aid, p):
            if feedback.isCanceled():
                raise QgsProcessingException("cancelled")
            return processing.run(aid, p, context=context, feedback=feedback, is_child_algorithm=True)

        result = st.run_selftest(work, layers, dem_folder, run, log=feedback.pushInfo, engine=engine,
                                 cell_m=cell, sim_h=simh, versions=software_versions(), exe=exe)
        text = result["report"] + "\nWork folder: {}{}\n".format(work, "" if keep else " (temporary)")
        if report and report.upper() != "TEMPORARY_OUTPUT":
            os.makedirs(os.path.dirname(os.path.abspath(report)) or ".", exist_ok=True)
            with open(report, "w", encoding="utf-8") as fh:
                fh.write(text)
        for line in text.splitlines():
            feedback.pushInfo(line)
        if not keep:
            try:
                import shutil
                if created_work:
                    shutil.rmtree(work, ignore_errors=True)      # a folder the self-test created
                else:
                    for name in os.listdir(work):                # user's WORK: remove only what we added
                        if name in preexisting:
                            continue
                        p = os.path.join(work, name)
                        if os.path.isdir(p) and not os.path.islink(p):
                            shutil.rmtree(p, ignore_errors=True)
                        else:
                            try:
                                os.remove(p)
                            except OSError:
                                pass
            except Exception:                                    # noqa
                pass
        return {"REPORT": report, "OK": result["ok"], "FAILED_STEP": next(
            (r["step"] for r in result["rows"] if r["status"] == "FAIL"), ""), "WORK": work if keep else ""}

    def make_layers(self, specs):
        """Real QGIS memory layers (the tests replace this with the mock's layers)."""
        return qgis_layers(specs)
