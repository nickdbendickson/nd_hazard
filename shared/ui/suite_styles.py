# -*- coding: utf-8 -*-
"""The suite's layer styles (QML in <plugin>/styles/), one look for the three plugins.

Byte-identical in ND Dam Hazard Classification Helper, ND Dam Breach Runner and ND Dam Breach
Screening. Canonical copy: shared/ui/suite_styles.py; edit it there and run tools/sync_ui.py
(tools/ci_checks.py fails on a stale copy). Pure Python except for the QGIS calls
inside try blocks: a style that cannot be applied never fails a run.

    style_for_path(path)      -> QML file name for an output file, by its name / fields, or None
    apply(layer, qml=None)    -> True when a style was loaded onto the layer
    attach(context, dest_id, qml) -> Processing post-processor so a tool's output is styled when
                                     QGIS loads it (QGIS 3.x / 4.x; silently nothing in a test double)
    styles_folder()           -> the folder of the QML files

Nothing here was opened in a real QGIS: the QML was written by hand to the 3.28 schema.
"""
import os
import re

STYLES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "styles")

# output file name (lower case, regex) -> QML
BY_NAME = (
    (r"^depth \(max\)\.tif$", "depth_ft.qml"),                  # the Runner (ft)
    (r"^baseline depth \(max\)\.tif$", "depth_ft.qml"),
    (r"^incremental depth \(max\)\.tif$", "depth_ft.qml"),
    (r"^velocity \(max\)\.tif$", "velocity_fps.qml"),
    (r"^baseline velocity \(max\)\.tif$", "velocity_fps.qml"),
    (r"^arrival time\.tif$", "arrival_h.qml"),
    (r"^baseline arrival time\.tif$", "arrival_h.qml"),
    (r"^hecras (depth|velocity|arrival)", None),                 # set below
    (r"_(base_)?depth\.tif$", "depth_m.qml"),                   # Screening tool 7 (m)
    (r"_incr_depth\.tif$", "depth_m.qml"),
    (r"_(base_)?vel\.tif$", "velocity_ms.qml"),
    (r"_arrive\.tif$", "arrival_h.qml"),
    (r"^hazard_(foundations|mobilehomes|vehicles)\.tif$", "acer_zones.qml"),   # ND zone rasters
    (r"^8_hazard\.gpkg$", "hazard_class_points.qml"),
    (r"^7b_runner_results\.gpkg$", "hazard_class_points.qml"),
    (r"^9_sensitivity\.gpkg$", "hazard_class_points.qml"),
    (r"^6_triage\.gpkg$", "triage_points.qml"),
    (r"^7_simulations\.gpkg$", "sim_status_points.qml"),
    (r"^(4|16_pond|18|19)_breach_points(_all)?\.gpkg$", "breach_points.qml"),
    (r"^(4|16_pond|18|19)_flow_arrows(_all)?\.gpkg$", "flow_arrows.qml"),
    (r"^(1_dams|3_dams_storage|5_dams_breach|13_changed_dams|16_ponds|18_dams_all|19_proposed)\.gpkg$", "dams_points.qml"),
    (r"^flood extent\.geojson$", "flood_extent.qml"),
    (r"^flood polygons \(max depth\)\.geojson$", "flood_extent.qml"),
)
HECRAS = {"depth": "depth_ft.qml", "velocity": "velocity_fps.qml", "arrival": "arrival_h.qml"}
# a vector layer with one of these fields gets that style (when the name says nothing)
BY_FIELD = (("hz_class", "hazard_class_points.qml"), ("ND_Class", "hazard_class_points_nd.qml"),
            ("ACER_Zone", "acer_zone_points.qml"), ("tri_result", "triage_points.qml"),
            ("sim_status", "sim_status_points.qml"))
_keep = []          # post-processor objects must stay alive until QGIS has used them


def styles_folder():
    return STYLES_DIR


def available():
    try:
        return sorted(n for n in os.listdir(STYLES_DIR) if n.endswith(".qml"))
    except OSError:
        return []


def style_for_path(path):
    """QML file name for an output file by its name, or None."""
    name = os.path.basename(str(path or "").split("|")[0]).lower()
    if not name:
        return None
    m = re.match(r"^hecras (depth|velocity|arrival)", name)
    if m:
        return HECRAS[m.group(1)]
    for pat, qml in BY_NAME:
        if qml and re.search(pat, name):
            return qml
    return None


def style_for_layer(layer):
    """QML for a layer: by its source file name, else by a telling field."""
    qml = None
    try:
        qml = style_for_path(layer.source())
    except Exception:                                            # noqa
        pass
    if qml:
        return qml
    try:
        names = set(layer.fields().names())
    except Exception:                                            # noqa
        return None
    for field, q in BY_FIELD:
        if field in names:
            return q
    return None


def apply(layer, qml=None, log=None):
    """Load a QML onto a layer. Returns True when applied; never raises."""
    qml = qml or style_for_layer(layer)
    if not qml:
        return False
    path = os.path.join(STYLES_DIR, qml)
    if not os.path.isfile(path):
        return False
    try:
        res = layer.loadNamedStyle(path)
        ok = True
        if isinstance(res, (tuple, list)) and len(res) >= 2:
            ok = bool(res[1])
        if ok:
            try:
                layer.triggerRepaint()
            except Exception:                                    # noqa
                pass
            if log:
                nm = getattr(layer, "name", "layer")
                try:
                    nm = nm() if callable(nm) else nm
                except Exception:                                # noqa
                    nm = "layer"
                log("style {} applied to {}".format(qml, nm))
        return ok
    except Exception as e:                                       # noqa
        if log:
            log("style {} not applied ({})".format(qml, e))
        return False


def attach(context, dest_id, qml=None, path_hint=None):
    """Ask Processing to style the output `dest_id` with `qml` (or the style for `path_hint`)
    when it loads it. Returns True when a post-processor was attached."""
    qml = qml or style_for_path(path_hint or dest_id)
    if not qml or not dest_id or not isinstance(dest_id, str):
        return False
    try:
        from qgis.core import QgsProcessingLayerPostProcessorInterface
    except Exception:                                            # noqa (test doubles)
        return False
    try:
        details = context.layerToLoadOnCompletionDetails(dest_id)
    except Exception:                                            # noqa
        return False

    class _Styler(QgsProcessingLayerPostProcessorInterface):
        def postProcessLayer(self, layer, ctx, feedback):
            apply(layer, qml, log=getattr(feedback, "pushInfo", None))

    try:
        pp = _Styler()
        _keep.append(pp)
        details.setPostProcessor(pp)
        return True
    except Exception:                                            # noqa
        return False


def attach_results(context, result, log=None):
    """attach() for every string output of a tool result that has a style. Returns the count."""
    n = 0
    for key, value in (result or {}).items():
        if isinstance(value, str) and value and value.upper() != "TEMPORARY_OUTPUT":
            if attach(context, value, path_hint=value):
                n += 1
    return n
