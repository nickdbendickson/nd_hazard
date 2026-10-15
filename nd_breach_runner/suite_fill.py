# -*- coding: utf-8 -*-
"""3.13.0: "Load the dam's data layers..." (Plugins > ND Dam Breach Runner).

The Runner's tools fill their EMPTY inputs from the dam folder when they run (suite_data_tools.
data_aware); this shows those layers in the project first, so you can see what a run will use and
pick them in any tool. Loaded into one layer group per dam:

  * every data file "Get data for this dam" recorded in the dam's dam.json (DEM, land cover,
    structures, roads, railroads, NID and DWR dams, DWR dam lines, ...)
  * the suite data folder's DWR dams and DWR dam lines when the dam folder has none (dwr/; the
    DWR unknown-structure and dugout files are not dams and are left out)

A file already in the project is not added again. Idea from the DamSafety plugins' "Fill layers
from suite folder" (2026-10-08 audit); built on the suite's own dam folder rules.
"""
import os

from .damsuite import damdata as DD
from .damsuite import suitedata as SD

GROUP = "Dam data: {}"
RASTER_ROLES = ("dem", "nlcd")
# the suite data folder's DWR files used when the dam folder has none
SUITE_DWR = (("dwr_dams", "DWR dams"), ("dwr_dam_lines", "DWR dam lines"))


def layer_list(dam_folder, data_folder=None):
    """[(name, path, "raster" | "vector", origin)] to load for a dam folder (no QGIS)."""
    out, have = [], set()
    if dam_folder and os.path.isdir(dam_folder):
        data = DD.dam_data(dam_folder)
        for role in DD.DATA_ROLES:
            if role in data:
                out.append((DD.LABELS.get(role, role), data[role],
                            "raster" if role in RASTER_ROLES else "vector", "dam folder"))
                have.add(role)
    try:
        dwr = SD.dwr_layers(SD.data_folder() if data_folder is None else data_folder)
    except Exception:                                            # noqa - no suite folder
        dwr = {}
    for role, label in SUITE_DWR:
        if role not in have and dwr.get(role):
            out.append((label + " (suite data)", dwr[role], "vector", "suite data folder"))
    return out


def _same(a, b):
    try:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    except (TypeError, ValueError):
        return False


def load_layers(dam_folder, data_folder=None, project=None):
    """Add layer_list() to the project under one group. Returns (added names, already there,
    not readable)."""
    from qgis.core import QgsProject, QgsRasterLayer, QgsVectorLayer
    project = project or QgsProject.instance()
    sources = [str(l.source() or "") for l in project.mapLayers().values()]
    added, there, bad = [], [], []
    group = None
    for name, path, kind, _origin in layer_list(dam_folder, data_folder):
        if any(_same(s.split("|")[0], path.split("|")[0]) and
               ("|" not in path or s.split("|", 1)[-1] == path.split("|", 1)[-1])
               for s in sources):
            there.append(name)
            continue
        lyr = QgsRasterLayer(path, name) if kind == "raster" else \
            QgsVectorLayer(path, name, "ogr")
        if not lyr.isValid():
            bad.append(name)
            continue
        if group is None:
            root = project.layerTreeRoot()
            title = GROUP.format(os.path.basename(os.path.normpath(dam_folder or "")) or
                                 "suite data")
            group = root.findGroup(title) or root.insertGroup(0, title)
        project.addMapLayer(lyr, False)
        group.addLayer(lyr)
        added.append(name)
    return added, there, bad
