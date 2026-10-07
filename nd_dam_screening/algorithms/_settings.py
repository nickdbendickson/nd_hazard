"""The suite settings file as the defaults of the Screening tools (0.13): the shared adapter
(suite_settings_tools.py, the same in the three plugins) with this plugin's key, settings tabs
(core/settings_groups.FAMILIES) and toolbox groups (Screening steps / Advanced)."""
from ..core import settings_groups as SG
from ..core import workflow as wf
from .. import suite_settings_tools as T

PLUGIN = SG.PLUGIN

# the shared functions, unchanged
definitions = T.definitions
param_defs = T.param_defs
tool_name = T.tool_name
set_default = T.set_default
original = T.original
fresh = T.fresh


def catalog_of(alg, tool=None, plugin=PLUGIN):
    return T.catalog_of(alg, plugin, SG.family, tool)


def apply_file_defaults(alg, tool=None, settings=None):
    return T.apply_file_defaults(alg, PLUGIN, SG.family, tool, settings)


def settings_aware(cls, step=None):
    """A Screening tool class that takes its defaults from the settings file and sits in the
    'Screening steps' or 'Advanced' toolbox group."""
    return T.settings_aware(cls, PLUGIN, SG.family, SG.group_for(step) if step else None, step)


def full_catalog(classes, plugin=PLUGIN):
    return T.full_catalog(classes, plugin, SG.family, SG.TAB_ORDER, SG.NOT_IN_GRID)


def step_of_tool(tool):
    """The workflow step key of an algorithm name ('findponds' -> '16')."""
    for k, a in wf.ALG.items():
        if a == tool:
            return k
    return None
