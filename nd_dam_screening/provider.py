from qgis.core import QgsProcessingAlgorithm, QgsProcessingProvider
from .algorithms.data_package import DataPackage
from .algorithms.prepare_dams import PrepareDams
from .algorithms.terrain_cache import TerrainCache
from .algorithms.download_dem import DownloadDem
from .algorithms.estimate_storage import EstimateStorage
from .algorithms.breach_direction import BreachDirection
from .algorithms.breach_hydrograph import BreachHydrograph
from .algorithms.triage import Triage
from .algorithms.run_breach_sim import RunBreachSim
from .algorithms.runner_handoff import RunnerHandoff
from .algorithms.hazard_classification import HazardClassification
from .algorithms.sensitivity import Sensitivity
from .algorithms.reports import Reports
from .algorithms.compare_reference import CompareReference
from .algorithms.estimate_runtime import EstimateRuntime
from .algorithms.inventory_diff import InventoryDiff
from .algorithms.selftest import SelfTest
from .algorithms.run_chain import RunChain
from .algorithms.find_ponds import FindPonds
from .algorithms.propose_dam import ProposeDam
from .algorithms.preflight import Preflight
from .algorithms.merge_ponds import MergePonds
from .algorithms import _settings as ST
from .core import workflow as _wf
from . import suite_data_tools as SDT


# the tools as written (their defaults are the "starting values" of the settings grid)
ORIGINAL_ALGORITHMS = (DataPackage, PrepareDams, TerrainCache, DownloadDem, EstimateStorage, BreachDirection, BreachHydrograph,
                       Triage, RunBreachSim, RunnerHandoff, HazardClassification, Sensitivity, Reports,
                       CompareReference, EstimateRuntime, InventoryDiff, SelfTest, RunChain, FindPonds,
                       ProposeDam, Preflight, MergePonds)


def _step_of(cls):
    try:
        name = cls().name()
    except Exception:                                            # noqa
        return None
    for k, a in _wf.ALG.items():
        if a == name:
            return k
    return None


# 0.13: every tool takes its parameter defaults from the suite settings file and sits in the
# "Screening steps" or "Advanced" group of the toolbox (algorithms/_settings.settings_aware)
_SETTINGS_AWARE = tuple(ST.settings_aware(c, _step_of(c)) for c in ORIGINAL_ALGORITHMS)


def dam_folder_of(alg, parameters, context=None):
    """0.16.0: the dam folder a tool run is about: DAM_FOLDER, or DAMS_FOLDER + DAM_ID (the suite's
    rule), else ONE dam in ONLY_IDS ('Run one dam') whose folder - holding dam.json - is in the
    tool's Dams folder or the suite's Dams folder (Plugins > ND Dam Suite > Suite data and
    servers); "" when the run is not about one dam folder."""
    f = SDT.dam_folder_of(alg, parameters, context)
    if f:
        return f
    parameters = parameters or {}
    text = getattr(SDT, "param_text", None) or SDT._text
    only = text(alg, parameters, "ONLY_IDS", context)
    ids = [x.strip() for x in only.split(",") if x.strip()]
    if len(ids) != 1:
        return ""
    roots = [text(alg, parameters, "DAMS_FOLDER", context)]
    try:
        from .damsuite import suitedata as SD
        roots.append(SD.dams_folder())
    except Exception:                                            # noqa
        pass
    return _wf.dam_folder_in(roots, ids[0])


# 0.16.0: tools that can name one dam fill their EMPTY data inputs from the dam folder's data
# (dam.json's data section, written by "Get data for this dam"): {tool: ({parameter: data role},
# parameters made optional because the dam folder may give them)}. A layer the user gives is never
# replaced; the run log names what was filled.
DATA_INPUTS = {
    "preparedams": ({"DWR": "dwr_dams", "NID": "nid_dams"}, ()),     # tool 1 (ONLY_IDS = one dam)
    "findponds": ({"WB": "waterbodies"}, ("WB",)),                   # tool 16 (ONLY_IDS = one pond)
}
DATA_HINT = " (or give the layer here)"


def _data_aware(cls):
    try:
        name = cls().name()
    except Exception:                                            # noqa
        return cls
    if name not in DATA_INPUTS:
        return cls
    mapping, required = DATA_INPUTS[name]
    return SDT.data_aware(cls, mapping, required=required, folder_of=dam_folder_of, hint=DATA_HINT)


def _with_manifest(cls):
    """0.11: after a tool has run, write a run manifest next to each file / folder it wrote
    (core/manifest.py: what it read and wrote, by content). Done here once for every tool
    rather than inside each one; a manifest problem never fails the tool."""
    if getattr(cls, "_manifest_wrapped", False):
        return cls
    inner = cls.processAlgorithm

    def processAlgorithm(self, params, context, feedback):
        result = inner(self, params, context, feedback)
        try:
            from .core import manifest as mf
            written = mf.record("nddam:" + self.name(), params, result)
            for w in written:
                feedback.pushInfo("run manifest: {}".format(w))
        except Exception as e:                                   # noqa
            try:
                feedback.pushInfo("run manifest not written ({})".format(e))
            except Exception:                                    # noqa
                pass
        try:                                                     # the suite's layer styles
            from . import suite_styles
            suite_styles.attach_results(context, result)
        except Exception:                                        # noqa
            pass
        return result

    processAlgorithm.__doc__ = inner.__doc__
    cls.processAlgorithm = processAlgorithm
    cls._manifest_wrapped = True
    return cls


for _cls in _SETTINGS_AWARE:
    _with_manifest(_cls)

# data_aware OUTSIDE settings_aware (and the manifest), so `_nd_original` stays the raw class and
# the manifest records the inputs the dam folder filled in
ALGORITHMS = tuple(_data_aware(c) for c in _SETTINGS_AWARE)

# 0.16.0: "Get data for this dam" (nddam:getdamdata, the same tool in the three plugins), in the
# Screening steps group (it fills the inputs of the steps); not in ALGORITHMS (it has no
# settings-grid values)
GET_DATA = SDT.get_data_algorithm(QgsProcessingAlgorithm, *ST.SG.GROUP_MAIN)


class NDDamProvider(QgsProcessingProvider):
    def loadAlgorithms(self):
        for cls in ALGORITHMS:
            self.addAlgorithm(cls())
        try:
            self.addAlgorithm(GET_DATA())
        except Exception:                                        # noqa - never lose the other tools
            pass

    def id(self):
        return "nddam"

    def name(self):
        return "ND Dam Breach Screening"
