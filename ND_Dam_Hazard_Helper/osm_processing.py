# -*- coding: utf-8 -*-
"""The building-outline file (FEMA_Structures): finding it in the outputs
folder, downloading it once, recognising it. The file was called
OSM_Outlines before v2.2 - in North Dakota it holds the FEMA USA Structures
outlines; the old file name is still read. (The outline consolidation,
All_Consolidated join and edge-buffer zone stages of 1.1 - 2.5 were
replaced by the structure inventory of the classification in 2.6.)"""

import os
import re

from qgis.core import QgsProject, QgsVectorLayer

from .compat import MSG_WARNING, GEOM_POLYGON
from .constants import STRUCTURES_TAG, LEGACY_STRUCTURES_TAG
from .helpers import log_message, get_dam_folder_name
from .processing import is_plugin_output_name, is_downloaded_data_layer


def structure_outline_files(output_folder, dam_folder):
    """Candidate paths of the building-outline file, in order of preference:
    {dam}_FEMA_Structures.gpkg, FEMA_Structures.gpkg, then the names used
    before v2.2 ({dam}_OSM_Outlines.gpkg, OSM_Outlines.gpkg - the same FEMA
    USA Structures data under its old name)."""
    dam = get_dam_folder_name(dam_folder)
    names = ["{}_{}.gpkg".format(dam, STRUCTURES_TAG), "{}.gpkg".format(STRUCTURES_TAG),
             "{}_{}.gpkg".format(dam, LEGACY_STRUCTURES_TAG),
             "{}.gpkg".format(LEGACY_STRUCTURES_TAG)]
    return [os.path.join(output_folder, n) for n in names]


def _key(name):
    return re.sub(r"[_\-.]+", " ", (name or "").lower())


def _is_fema_structures_name(name):
    n = _key(name)
    return bool(re.search(r"(?<![a-z])fema(?!le)", n) and "struct" in n)


def _is_osm_name(name):
    return bool(re.search(r"(?<![a-z])osm(?![a-z])", _key(name)))


def is_fema_structures_layer(lyr, output_folder="", dam_folder=""):
    """True when `lyr` is the FEMA USA Structures outline file (by file name,
    old OSM_Outlines name included, or by layer name)."""
    if lyr is None:
        return False
    try:
        src = os.path.normcase(os.path.abspath(lyr.source().split("|")[0]))
    except Exception:
        src = ""
    for p in structure_outline_files(output_folder, dam_folder) if output_folder else []:
        if src and src == os.path.normcase(os.path.abspath(p)):
            return True
    base = os.path.basename(src)
    return _is_fema_structures_name(lyr.name()) or _is_fema_structures_name(base) or \
        LEGACY_STRUCTURES_TAG.lower() in base.lower()


def download_structures_file(output_folder, dam_folder, extent_4326, transform_ctx,
                             progress_cb=None):
    """FEMA USA Structures outlines for the study area, saved as
    {dam}_FEMA_Structures.gpkg in the outputs folder so later runs (and the
    engineer's edits) use that file. Returns (layer, count, path) - layer
    None when the download failed or found nothing."""
    from .download import fetch_usa_structures, try_download
    from .helpers import export_layer_to_formats
    lyr, n, _cache = try_download("FEMA USA Structures", fetch_usa_structures, extent_4326,
                                  output_folder, progress_cb=progress_cb)
    if lyr is None or not n:
        return None, 0, None
    base = "{}_{}".format(get_dam_folder_name(dam_folder), STRUCTURES_TAG)
    _ok, _res, paths = export_layer_to_formats(lyr, output_folder, base, transform_ctx,
                                               formats=("gpkg",))
    path = paths.get("gpkg")
    if not path:
        return lyr, n, None
    saved = QgsVectorLayer(path, STRUCTURES_TAG, "ogr")
    log_message("FEMA USA Structures: {} outlines saved to {}".format(n, os.path.basename(path)))
    return (saved if saved.isValid() else lyr), n, path


def get_osm_candidates(output_folder, dam_folder):
    """Find the building-outline layer (FEMA USA Structures for ND): prefer
    {dam}_FEMA_Structures.gpkg in the outputs folder, then
    FEMA_Structures.gpkg, then the pre-v2.2 names {dam}_OSM_Outlines.gpkg /
    OSM_Outlines.gpkg, then a project polygon layer named like
    "FEMA Structures" (or "OSM ...") - plugin outputs excluded."""
    for path in structure_outline_files(output_folder, dam_folder):
        if os.path.isfile(path):
            lyr = QgsVectorLayer(path, STRUCTURES_TAG, "ogr")
            if lyr.isValid() and lyr.geometryType() == GEOM_POLYGON:
                if LEGACY_STRUCTURES_TAG in os.path.basename(path):
                    log_message("Building outlines read from {} (old name - rename it to "
                                "{}.gpkg)".format(os.path.basename(path), STRUCTURES_TAG))
                return lyr
            log_message("{} exists but is not a valid polygon layer".format(
                os.path.basename(path)), MSG_WARNING)
    polys = [lyr for lyr in QgsProject.instance().mapLayers().values()
             if isinstance(lyr, QgsVectorLayer) and lyr.isValid()
             and lyr.geometryType() == GEOM_POLYGON and not is_plugin_output_name(lyr.name())
             and not is_downloaded_data_layer(lyr)]
    for match in (_is_fema_structures_name, _is_osm_name):
        for lyr in polys:
            if match(lyr.name()):
                return lyr
    return None
