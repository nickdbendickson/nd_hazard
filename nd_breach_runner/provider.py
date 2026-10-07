"""Processing provider "ND Dam Breach Runner" (id ndbreach)."""
import os

from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon

from .algorithms import ALGORITHMS as ORIGINAL_ALGORITHMS, PROVIDER_ID, SET_ANUGA, SET_LISFLOOD, SET_THREADS, \
    SET_BUILTIN
from .algorithms_tools import _ToolBase
from .core import damjson, nd_data
from . import popup_logic as pl
from . import suite_data_tools as SDT
from . import suite_settings_tools as ST


def _tool_name(cls):
    try:
        return cls().name()
    except Exception:                                            # noqa
        return ""


def _run_dam_folder(alg, parameters, context=None):
    """3.9.0: the dam folder of a run tool: <Dams folder>/<Dam ID> named as the tool names it
    (algorithms._safe_name, the same rule as damjson.folder_name), when it exists."""
    parameters = parameters or {}
    dams = SDT.param_text(alg, parameters, "DAMS_FOLDER", context)
    did = SDT.param_text(alg, parameters, "DAM_ID", context).strip()
    if dams and did:
        f = os.path.join(dams, damjson.folder_name(did))
        if os.path.isdir(f):
            return f
    return ""


# 3.9.0: the inputs each tool fills from the dam folder ("Get data for this dam" writes them in
# dam.json's data section) when they are EMPTY - a choice the user made is never replaced.
# {tool: (mapping {parameter: data role}, fields {parameter: dam.json field}, required, folder_of)}
# Left out on purpose: the run tools' STRUCTURES (culvert / bridge / notch LINES, upstream end
# first - the suite's "bridges" are NBI points, not that), animation's DEM (empty = the model's
# own DEM), batch / cascade (a layer of dams, not one dam folder) and the tools that name no dam
# (fillgaps, nlcdmanning, conditiondem, buildvrt). NORMAL_POOL_FT is optional with no default
# (empty = none) in every run tool, so dam.json's normal pool may fill it. PONDS is NOT filled:
# NHD waterbodies include the dam's own reservoir and every pond is lowered and started full -
# a modelling choice the engineer makes in the tool, not a data default.
_RUN_MAPPING = {"DEM": "dem", "RECEPTOR_POINTS": "structures", "RECEPTOR_ROADS": "roads",
                "RECEPTOR_RAIL": "rail", "NLCD_RASTER": "nlcd"}
_RUN = (_RUN_MAPPING, {"NORMAL_POOL_FT": "normal_pool_ft"}, ("DEM",), _run_dam_folder)
DATA_INPUTS = {
    "rundam": _RUN, "sensitivity": _RUN, "estimate": _RUN, "nested": _RUN, "probabilistic": _RUN,
    # life safety: NSI-style structure points carry occupants and values (outlines get defaults)
    "consequences": ({"STRUCTURES": "structure_points", "ROADS": "roads", "RAIL": "rail"}, {},
                     ("STRUCTURES",), None),
    "ndhazardclass": ({"STRUCTURES": "structures", "ROADS": "roads", "RAIL": "rail",
                       "LIFELINES": "lifelines", "DOWNSTREAM_DAMS": "nid_dams"}, {}, (), None),
}


def _data_aware(cls):
    """3.9.0: data_aware around settings_aware (so `_nd_original` stays the class as written)."""
    spec = DATA_INPUTS.get(_tool_name(cls))
    if spec is None:
        return cls
    mapping, fields, required, folder_of = spec
    return SDT.data_aware(cls, mapping, fields, required, folder_of)


# 3.5: every tool takes its parameter defaults from the suite settings file (suite_settings_tools)
# 3.9.0: and its empty data inputs from the dam folder (suite_data_tools.data_aware)
ALGORITHMS = tuple(_data_aware(ST.settings_aware(c, pl.PLUGIN_KEY, pl.settings_group))
                   for c in ORIGINAL_ALGORITHMS)
# 3.9.0: "Get data for this dam (or all dams)" (ndbreach:getdamdata), shared by the three plugins.
# Not in ALGORITHMS: its values are not the Runner's settings (the settings grid reads ALGORITHMS).
GET_DATA = SDT.get_data_algorithm(_ToolBase)

ICON = os.path.join(os.path.dirname(__file__), "icon.svg")


class Provider(QgsProcessingProvider):
    def id(self):
        return PROVIDER_ID

    def name(self):
        return "ND Dam Breach Runner"

    def longName(self):
        return "ND Dam Breach Runner (LISFLOOD-FP / ANUGA)"

    def icon(self):
        return QIcon(ICON) if os.path.exists(ICON) else QgsProcessingProvider.icon(self)

    def load(self):
        try:
            from processing.core.ProcessingConfig import ProcessingConfig, Setting
            ProcessingConfig.settingIcons[self.name()] = self.icon()
            # first-run defaults from the usual install places (no subprocesses at startup)
            lf = (nd_data.lisflood_candidates() or [""])[0]
            an = (nd_data.anuga_python_candidates() or [""])[0]
            ProcessingConfig.addSetting(Setting(
                self.name(), SET_LISFLOOD, "LISFLOOD-FP executable (built with the "
                "arrival-depth patch; empty = the one bundled with the plugin on Windows and "
                "Linux, else the built-in engine)", lf, valuetype=Setting.FILE))
            # 3.11.0
            ProcessingConfig.addSetting(Setting(
                self.name(), SET_BUILTIN, "Use the built-in flood engine instead of LISFLOOD-FP "
                "(no install; slower)", False))
            ProcessingConfig.addSetting(Setting(
                self.name(), SET_ANUGA, "Python interpreter of the ANUGA conda environment "
                "(set by 'Set up ANUGA (one-time download)'; or e.g. ~/miniforge3/envs/anuga/bin/python)", an, valuetype=Setting.FILE))
            ProcessingConfig.addSetting(Setting(
                self.name(), SET_THREADS, "Threads for the models (0 = all cores)", 0,
                valuetype=Setting.INT))
            ProcessingConfig.readSettings()
        except Exception:   # settings are optional; parameters can carry the paths
            pass
        self.refreshAlgorithms()
        return True

    def unload(self):
        try:
            from processing.core.ProcessingConfig import ProcessingConfig
            for k in (SET_LISFLOOD, SET_ANUGA, SET_THREADS, SET_BUILTIN):
                ProcessingConfig.removeSetting(k)
        except Exception:
            pass

    def loadAlgorithms(self):
        for cls in ALGORITHMS + (GET_DATA,):
            self.addAlgorithm(cls())
