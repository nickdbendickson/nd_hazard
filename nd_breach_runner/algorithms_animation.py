"""Flood animation tool of the provider (id 'animation').

Import it through `algorithms` (which the provider does), like the other tool modules.

The part that loads the frames as time-enabled layers ('Load as time-enabled layers')
was written against the QGIS 3.28 API but has NOT been run in real QGIS; every QGIS
call there is guarded so a failure is reported, never fatal."""
import datetime
import json
import os
import zipfile

import numpy as np
from qgis.core import (QgsProcessingException, QgsProcessingOutputFile,
                       QgsProcessingOutputFolder, QgsProcessingOutputString,
                       QgsProcessingParameterEnum, QgsProcessingParameterRasterLayer)

from .algorithms import ZUNITS, _Feedback
from .algorithms_tools import _ToolBase
from .compat import set_advanced
from .core import animation as anim
from .core import gdal_io, workflow
from .core.grid import Grid

try:                                   # real QGIS; not in the test stand-in
    from qgis.core import QgsProcessingLayerPostProcessorInterface as _PostBase
except ImportError:
    _PostBase = object

DEFAULT_BASE_TIME = "2000-01-01 00:00"
_KEEP = []                             # post-processors must outlive processAlgorithm


def parse_base_time(text):
    """'2000-01-01 00:00' (also with seconds, a 'T', or a date only) -> datetime."""
    if hasattr(text, "toPyDateTime"):
        return text.toPyDateTime()
    t = str(text or "").strip() or DEFAULT_BASE_TIME
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(t, fmt)
        except ValueError:
            continue
    raise ValueError("'{}' is not a date-time like {}".format(t, DEFAULT_BASE_TIME))


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _geojson_crs(folder):
    """The model CRS the products recorded in their GeoJSON ('EPSG:26914'), if any."""
    for fn in ("Flood Extent.geojson", "Flood Polygons (Max Depth).geojson",
               "Arrival Zones.geojson"):
        d = _read_json(os.path.join(folder, fn))
        name = ((d.get("crs") or {}).get("properties") or {}).get("name")
        if name:
            return name
    return None


def snapshot_grid(folder, info, report):
    """The grid the snapshots lie on, and how it was found: recorded with the
    snapshots; else a raster in the dam folder of the same size (written in the
    model CRS); else the last LISFLOOD-FP domain in run_report.json."""
    ny, nx = info["ny"], info["nx"]
    blank = np.full((ny, nx), np.nan)
    meta = info["meta"]
    if all(k in meta for k in ("x0", "y0", "dx")):
        return (Grid(blank, meta["x0"], meta["y0"], meta["dx"], crs_wkt=meta.get("crs_wkt") or None),
                "recorded with the snapshots")
    model_dir = os.path.join(folder, workflow.MODEL_FOLDER)
    for p in (os.path.join(folder, workflow.FINAL_NAMES["depth"]),
              os.path.join(model_dir, "lisflood_" + workflow.QA_NAMES["depth"])):
        if not os.path.exists(p):
            continue
        try:
            g = gdal_io.read_grid(p)
        except ImportError:
            break
        except (RuntimeError, ValueError, OSError):
            continue
        if g.data.shape == (ny, nx):
            return (Grid(blank, g.x0, g.y0, g.dx, crs_wkt=g.crs_wkt),
                    "the grid of '{}'".format(os.path.basename(p)))
    runs = report.get("lisflood_runs") or []
    cs = (report.get("settings") or {}).get("cell_size_m")
    if runs and cs and runs[-1].get("bounds"):
        b = runs[-1]["bounds"]
        if (int(round((b[2] - b[0]) / cs)) == nx and int(round((b[3] - b[1]) / cs)) == ny):
            return (Grid(blank, b[0], b[3], cs, crs_wkt=_geojson_crs(folder)),
                    "the last LISFLOOD-FP domain in run_report.json")
    raise QgsProcessingException(
        "Cannot tell where the snapshots ({} x {} cells) lie: there is no grid recorded with "
        "them ('{}'), no raster of that size in the dam folder (the rasters are "
        "re-projected when the DEM's CRS is not the model's) and no matching domain in "
        "run_report.json.".format(nx, ny, os.path.basename(anim.sidecar_path(
            anim.SNAPSHOT_FILE))))


# --------------------------------------------------------------------------------------
# time-enabled layers (real QGIS only; untested there)
# --------------------------------------------------------------------------------------
def _qdt(dt):
    from qgis.PyQt.QtCore import QDate, QDateTime, QTime, Qt
    utc = getattr(Qt, "UTC", None)
    if utc is None:
        utc = Qt.TimeSpec.UTC
    return QDateTime(QDate(dt.year, dt.month, dt.day),
                     QTime(dt.hour, dt.minute, dt.second, dt.microsecond // 1000), utc)


def set_fixed_range(layer, begin, end):
    """Fixed temporal range [begin, end) on a raster layer (QGIS 3.28 API)."""
    from qgis.core import QgsDateTimeRange, QgsRasterLayerTemporalProperties
    mode = getattr(QgsRasterLayerTemporalProperties, "ModeFixedTemporalRange", None)
    if mode is None:
        from qgis.core import Qgis
        mode = Qgis.RasterTemporalMode.FixedTemporalRange
    tp = layer.temporalProperties()
    tp.setIsActive(True)
    tp.setMode(mode)
    tp.setFixedTemporalRange(QgsDateTimeRange(_qdt(begin), _qdt(end), True, False))


def style_depth(layer):
    """The EAP map's depth classes (ft) as a discrete pseudocolor renderer."""
    from qgis.core import QgsColorRampShader, QgsRasterShader, QgsSingleBandPseudoColorRenderer
    from qgis.PyQt.QtGui import QColor
    from .core.products import DEPTH_CLASSES_FT, DEPTH_COLORS
    shader = QgsColorRampShader()
    disc = getattr(QgsColorRampShader, "Discrete", None)
    if disc is None:
        from qgis.core import Qgis
        disc = Qgis.ShaderInterpolationMethod.Discrete
    shader.setColorRampType(disc)
    uppers = list(DEPTH_CLASSES_FT[1:]) + [float("inf")]
    items = []
    for i, (hi, col) in enumerate(zip(uppers, DEPTH_COLORS)):
        lo = DEPTH_CLASSES_FT[i]
        lab = "{:g} - {:g} ft".format(lo, hi) if hi != float("inf") else "{:g}+ ft".format(lo)
        items.append(QgsColorRampShader.ColorRampItem(hi, QColor(col), lab))
    shader.setColorRampItemList(items)
    rs = QgsRasterShader()
    rs.setRasterShaderFunction(shader)
    layer.setRenderer(QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, rs))
    layer.triggerRepaint()


def move_to_group(project, layer, group_name):
    root = project.layerTreeRoot()
    grp = root.findGroup(group_name)
    if grp is None:
        grp = root.insertGroup(0, group_name)
        grp.setExpanded(False)
    node = root.findLayer(layer.id())
    if node is None:
        grp.addLayer(layer)
        return
    parent = node.parent()
    if parent is not None and parent.name() == group_name and parent is not root:
        return
    grp.addChildNode(node.clone())
    if parent is not None:
        parent.removeChildNode(node)


def set_project_time(project, begin, end, step_s):
    """Project time range and, with a map canvas, the temporal controller's
    extent and frame length."""
    from qgis.core import QgsDateTimeRange, QgsInterval
    rng = QgsDateTimeRange(_qdt(begin), _qdt(end))
    try:
        project.timeSettings().setTemporalRange(rng)
    except Exception:
        pass
    try:
        from qgis.utils import iface
        nav = iface.mapCanvas().temporalController()
        nav.setTemporalExtents(rng)
        nav.setFrameDuration(QgsInterval(float(step_s)))
    except Exception:
        pass


class _TemporalFrame(_PostBase):
    """Runs on QGIS's main thread when a frame layer is loaded: fixed time range,
    depth-class style, the animation's group."""

    def __init__(self, begin, end, group, first=False, extent=None, step_s=None):
        _PostBase.__init__(self)
        self.begin, self.end, self.group = begin, end, group
        self.first, self.extent, self.step_s = first, extent, step_s

    def postProcessLayer(self, layer, context, feedback):
        try:
            set_fixed_range(layer, self.begin, self.end)
        except Exception as e:
            if feedback is not None:
                feedback.reportError("Time range not set on {}: {}".format(layer.name(), e),
                                     fatalError=False)
        try:
            style_depth(layer)
        except Exception:
            pass
        try:
            project = context.project()
            if project is None:
                from qgis.core import QgsProject
                project = QgsProject.instance()
            move_to_group(project, layer, self.group)
            if self.first and self.extent:
                set_project_time(project, self.extent[0], self.extent[1], self.step_s)
        except Exception:
            pass


# --------------------------------------------------------------------------------------
class AnimationAlgorithm(_ToolBase):
    def name(self):
        return "animation"

    def displayName(self):
        return "Flood animation (GIF / MP4 / time-enabled layers)"

    def shortHelpString(self):
        return ("Animates a breach run from the depth snapshots LISFLOOD-FP wrote "
                "(<dam folder>/_breach_model/depth_snapshots.npz): each frame is a hillshade "
                "of the DEM with the depth in the EAP map's classes, the time after breach "
                "initiation (T-hh:mm before it, in the flood scenario), a timeline, a legend and "
                "a scale bar, and a line around the area the flood has reached at the arrival "
                "depth so far. Writes PNG frames, an animated GIF and, when ffmpeg is on the "
                "PATH, an MP4. The frames show only the area the flood reaches; a grid larger "
                "than the size asked for is reduced by the deepest cell of each block.\n\n"
                "Optionally writes one depth GeoTIFF (ft) per frame and 'Depth frames.json' "
                "with their time ranges, and loads them as time-enabled layers in a group, so "
                "QGIS's temporal controller plays the flood: breach initiation is the base "
                "date-time (default 2000-01-01 00:00, so the clock reads the hours after "
                "initiation). Loading the time-enabled layers was written against the QGIS 3.28 "
                "API but has not been tested in QGIS; if a range is not set, the GeoTIFFs and "
                "the JSON index are still there.\n\n"
                "The hillshade uses the DEM you give, or the model's own DEM when the model "
                "files were kept. The snapshots come from LISFLOOD-FP even when the final "
                "rasters come from ANUGA.")

    def initAlgorithm(self, config=None):
        self._file("DAM_FOLDER", "Dam folder", folder=True, optional=False)
        self.addParameter(QgsProcessingParameterRasterLayer(
            "DEM", "DEM for the hillshade (empty = the model's own DEM, if kept)",
            optional=True))
        self.addParameter(set_advanced(QgsProcessingParameterEnum(
            "DEM_Z", "DEM elevation units", [z[1] for z in ZUNITS], defaultValue=0)))
        self._text("TITLE", "Title (empty = the dam's name and ID)")
        self._num("FPS", "Frames per second", 4, minimum=1, maximum=30, integer=True)
        self._num("MAX_PX", "Map size: longer side at most (pixels)", 900, minimum=200,
                  maximum=4000, integer=True)
        self._num("STEP", "Use every Nth snapshot", 1, minimum=1, integer=True, advanced=True)
        self._num("FRONT_FT", "Line around the area reached by this depth (ft; 0 = none; "
                  "starting value 1 is the engineer's)", 1.0, minimum=0.0, advanced=True)
        self._bool("GIF", "Animated GIF", True)
        self._bool("MP4", "MP4 video (needs ffmpeg on the PATH)", True)
        self._bool("GEOTIFF", "One depth GeoTIFF (ft) per frame, with a JSON index of their "
                   "times", False)
        self._bool("LOAD_TEMPORAL", "Load as time-enabled layers (writes the GeoTIFFs)", False)
        self._text("BASE_TIME", "Date-time of breach initiation for the time-enabled layers",
                   DEFAULT_BASE_TIME)
        self._bool("KEEP_FRAMES", "Keep the PNG frames", True, advanced=True)
        self._file("OUT_FOLDER", "Output folder (empty = <dam folder>/Animation)", folder=True)
        self.addOutput(QgsProcessingOutputFolder("FOLDER", "Animation folder"))
        self.addOutput(QgsProcessingOutputFile("GIF", "Animated GIF"))
        self.addOutput(QgsProcessingOutputFile("MP4", "MP4 video"))
        self.addOutput(QgsProcessingOutputFile("INDEX", "Frame times (JSON)"))
        self.addOutput(QgsProcessingOutputString("RESULT", "Result"))

    # -- inputs -------------------------------------------------------------------------
    def _dem(self, parameters, context, grid, folder, feedback):
        lyr = self.parameterAsRasterLayer(parameters, "DEM", context) \
            if parameters.get("DEM") else None
        if lyr is not None:
            try:
                z = gdal_io.read_like(lyr.source(), grid, "bilinear")
                choice = ZUNITS[self.parameterAsEnum(parameters, "DEM_Z", context)][0]
                try:
                    f, how = gdal_io.z_to_m_factor(lyr.source(), choice)
                except Exception:
                    f, how = 1.0, "metres (assumed)"
            except ImportError:
                raise QgsProcessingException("GDAL's Python bindings are not available; run this "
                                             "from QGIS.")
            except (RuntimeError, ValueError, OSError) as e:
                raise QgsProcessingException("Could not read the DEM: {}".format(e))
            feedback.pushInfo("Hillshade from {} (elevations in {})".format(lyr.source(), how))
            return np.asarray(z, float) * f
        asc = os.path.join(folder, workflow.MODEL_FOLDER, "lisflood", "run.dem.asc")
        if os.path.exists(asc):
            try:
                g = Grid.read_asc(asc)
                if g.data.shape == grid.data.shape:
                    feedback.pushInfo("Hillshade from the model's DEM ({})".format(asc))
                    return g.masked()
            except (OSError, ValueError, KeyError):
                pass
        feedback.reportError("Check: no DEM (none given, and the model's own DEM was not kept), "
                             "so the frames have a plain background. Give the DEM for a "
                             "hillshade.", fatalError=False)
        return np.zeros(grid.data.shape)

    def processAlgorithm(self, parameters, context, feedback):
        folder = self.parameterAsFile(parameters, "DAM_FOLDER", context)
        if not folder or not os.path.isdir(folder):
            raise QgsProcessingException("No dam folder '{}'".format(folder))
        npz = os.path.join(folder, workflow.MODEL_FOLDER, anim.SNAPSHOT_FILE)
        if not os.path.exists(npz):
            raise QgsProcessingException(
                "No depth snapshots in {}: run the breach model with the animation option on "
                "(it writes {}/{}).".format(folder, workflow.MODEL_FOLDER, anim.SNAPSHOT_FILE))
        try:
            info = anim.npz_info(npz)
        except (OSError, ValueError, KeyError, zipfile.BadZipFile) as e:
            raise QgsProcessingException("Cannot read {}: {}".format(npz, e))
        if info["n"] == 0:
            raise QgsProcessingException("{} holds no snapshots".format(npz))
        report = _read_json(os.path.join(folder, workflow.MODEL_FOLDER, "run_report.json"))
        grid, how = snapshot_grid(folder, info, report)
        feedback.pushInfo("{} snapshots of {} x {} cells; grid: {}".format(
            info["n"], info["nx"], info["ny"], how))
        dem = grid.like(self._dem(parameters, context, grid, folder, feedback))
        meta = info["meta"]
        t_ref = meta.get("t_ref_hr")
        if t_ref is None:
            t_ref = (report.get("hydrograph") or {}).get("breach_initiation_hr") or 0.0
        dam_xy = meta.get("dam_xy") or report.get("dam_xy")
        dam_xy = tuple(dam_xy) if dam_xy else None
        d = report.get("dam") or {}
        title = self._str(parameters, "TITLE", context) or meta.get("title") or (
            "{} ({})".format(d["name"], d.get("dam_id")) if d.get("name") else
            d.get("dam_id") or os.path.basename(os.path.normpath(folder)))
        crs_txt = ""
        if grid.crs_wkt:
            crs_txt = grid.crs_wkt if str(grid.crs_wkt).upper().startswith("EPSG:") else ""
            if not crs_txt:
                try:
                    crs_txt = gdal_io.epsg_of(grid.crs_wkt) or ""
                except Exception:
                    crs_txt = ""
        tier = (report.get("settings") or {}).get("tier", "")
        note = "LISFLOOD-FP depth snapshots" + (", " + crs_txt if crs_txt else "")
        if tier == workflow.TIER_BOTH:
            note += " (the final rasters are ANUGA's)"
            feedback.pushInfo("The animation shows LISFLOOD-FP; the final rasters are ANUGA's.")
        unit = 1.0                              # metres per grid unit (the scale bar)
        if grid.crs_wkt:
            try:
                unit = gdal_io.linear_unit_m(grid.crs_wkt) or 1.0
            except Exception:
                unit = 1.0
        load_time = self.parameterAsBool(parameters, "LOAD_TEMPORAL", context)
        base = None
        if load_time:
            try:
                base = parse_base_time(self._str(parameters, "BASE_TIME", context))
            except ValueError as e:
                raise QgsProcessingException(str(e))
        writer = None
        if self.parameterAsBool(parameters, "GEOTIFF", context) or load_time:
            if not grid.crs_wkt:
                raise QgsProcessingException(
                    "The model CRS is not recorded with the snapshots or in the dam folder, so "
                    "the GeoTIFF frames cannot be georeferenced. Untick the GeoTIFF options, or "
                    "re-run the breach model.")
            writer = gdal_io.GeoTiffWriter(grid.crs_wkt)
        out_dir = self._fileparam(parameters, "OUT_FOLDER", context) or os.path.join(
            folder, "Animation")
        fb = _Feedback(feedback)
        try:
            r = anim.render_run(
                npz, dem, out_dir, t_ref, title, dam_xy,
                max_px=self.parameterAsInt(parameters, "MAX_PX", context),
                fps=self.parameterAsInt(parameters, "FPS", context),
                make_gif=self.parameterAsBool(parameters, "GIF", context),
                make_mp4=self.parameterAsBool(parameters, "MP4", context),
                geotiff_writer=writer, crs_note=note,
                step=self.parameterAsInt(parameters, "STEP", context),
                front_ft=self.parameterAsDouble(parameters, "FRONT_FT", context) or None,
                unit_m=unit, keep_frames=self.parameterAsBool(parameters, "KEEP_FRAMES", context),
                progress=fb.progress, cancel=fb.cancel)
        except anim.Cancelled:
            raise QgsProcessingException("Cancelled")
        except ImportError:
            raise QgsProcessingException("GDAL's Python bindings are not available; run this "
                                         "from QGIS (or untick the GeoTIFF options).")
        except (OSError, ValueError, RuntimeError) as e:
            raise QgsProcessingException(str(e))
        for n in r.notes:
            feedback.pushInfo(n)
        loaded = 0
        if load_time and r.index_json:
            loaded = self._load_time_layers(r, base, title, context, feedback)
        parts = ["{} frames, {} x {} px".format(len(r.times_s), r.size[0], r.size[1])]
        for what, p in (("GIF", r.gif), ("MP4", r.mp4)):
            if p:
                parts.append("{} {}".format(what, p))
        if r.geotiffs:
            parts.append("{} GeoTIFFs".format(len(r.geotiffs)))
        if load_time:
            parts.append("{} time-enabled layers".format(loaded))
        msg = "; ".join(parts)
        feedback.pushInfo(msg)
        return {"FOLDER": out_dir, "GIF": r.gif, "MP4": r.mp4, "INDEX": r.index_json,
                "RESULT": msg}

    def _load_time_layers(self, r, base, title, context, feedback):
        """Queue the frame GeoTIFFs to load when the algorithm finishes, each with
        a post-processor that sets its fixed time range (on QGIS's main thread)."""
        ranges = anim.temporal_ranges(r.index_json, base)
        if not ranges:
            return 0
        step_s = (ranges[0][2] - ranges[0][1]).total_seconds()
        group = "{} flood animation".format(title)
        if len(_KEEP) > 5000:
            del _KEEP[:-1000]
        n, err = 0, None
        for i, (p, t0, t1) in enumerate(ranges):
            self.load(context, p, "{} {}".format(title, os.path.splitext(os.path.basename(p))[0]))
            pp = _TemporalFrame(t0, t1, group, first=(i == 0),
                                extent=(ranges[0][1], ranges[-1][2]), step_s=step_s)
            try:
                context.layerToLoadOnCompletionDetails(p).setPostProcessor(pp)
                _KEEP.append(pp)
                n += 1
            except Exception as e:             # not real QGIS, or another API
                err = err or e
        if err is not None:
            feedback.reportError("Check: the frames were queued to load, but their time ranges "
                                 "could not be attached ({}); set them from '{}' by hand.".format(
                                     err, os.path.basename(r.index_json)), fatalError=False)
        return n


ANIMATION_ALGORITHMS = (AnimationAlgorithm,)
