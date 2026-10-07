# -*- coding: utf-8 -*-
"""The suite settings file as the DEFAULTS of a plugin's Processing tools (no Qt).

Byte-identical in ND Dam Hazard Classification Helper, ND Dam Breach Runner and ND Dam Breach
Screening (written by suite/suite_shared/make_styles.py; edit it there). damsuite.settings holds
the file; this module is the QGIS-facing adapter: it reads a tool's parameter definitions into the
pure catalog, and sets the file's values as the parameters' defaults when a tool is created
(`settings_aware` wraps a tool class), so the Processing dialogs open with the engineer's values,
the popups' chains use them and qgis_process does too. Every value can still be changed in the
dialog; a parameter the caller passes wins over the default as always. Hidden (legacy metre)
parameters are never in the file. Works on QGIS 3.28 - 4.x and on the test doubles of QGIS
(attributes instead of methods).

Each plugin gives: its suite key (ndscreen / ndbreach / ndhazard), a `group_of(tool)` function
-> (tab name, order) for the grid, and optionally `toolbox_group(tool)` -> (group, group id) for
the Processing toolbox.
"""
from .damsuite import settings as DS


def _get(obj, name, default=None):
    """An attribute that is a method on real QGIS objects and a plain attribute on the mock."""
    v = getattr(obj, name, default)
    if callable(v):
        try:
            return v()
        except Exception:                                        # noqa
            return default
    return v


def _hidden(p):
    flags = _get(p, "flags", 0) or 0
    for get in (lambda: __import__("qgis.core", fromlist=["Qgis"]).Qgis.ProcessingParameterFlag.Hidden,
                lambda: __import__("qgis.core", fromlist=["QgsProcessingParameterDefinition"])
                .QgsProcessingParameterDefinition.FlagHidden):
        try:
            flag = get()
            try:
                return bool(int(flags) & int(flag))
            except (TypeError, ValueError):
                return bool(flags & flag)
        except Exception:                                        # noqa
            continue
    return False


def _type_name(p):
    t = _get(p, "type", None)
    if isinstance(t, str) and t:
        return t
    return type(p).__name__


def _is_integer(p):
    dt = _get(p, "dataType", None)
    if dt is None:
        return False
    for get in (lambda: __import__("qgis.core", fromlist=["Qgis"]).Qgis.ProcessingNumberParameterType.Integer,
                lambda: __import__("qgis.core", fromlist=["QgsProcessingParameterNumber"])
                .QgsProcessingParameterNumber.Integer):
        try:
            integer = get()
        except Exception:                                        # noqa
            continue
        try:
            return int(dt) == int(integer)
        except (TypeError, ValueError):
            return dt == integer
    return False


def _bound(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return None if abs(v) > 1e300 else v


def definitions(alg):
    """The parameter definitions of a tool instance (after initAlgorithm)."""
    defs = _get(alg, "parameterDefinitions", None)
    if defs is None:
        defs = list(getattr(alg, "_params", {}).values())        # the mock
    return list(defs or [])


def param_defs(alg):
    """[{name, description, type, default, options, hidden, integer, minimum, maximum}] of a tool."""
    out = []
    for p in definitions(alg):
        if _get(p, "isDestination", False):
            continue
        d = {"name": _get(p, "name", ""), "description": _get(p, "description", "") or "",
             "type": _type_name(p), "default": _get(p, "defaultValue", None), "hidden": _hidden(p)}
        opts = _get(p, "options", None)
        d["options"] = list(opts) if opts else []
        if DS.kind_of(d["type"]) == "number":
            d["integer"] = _is_integer(p)
            d["minimum"] = _bound(_get(p, "minimum", None))
            d["maximum"] = _bound(_get(p, "maximum", None))
        out.append(d)
    return out


def tool_name(alg):
    return _get(alg, "name", "") or ""


def catalog_of(alg, plugin, group_of, tool=None):
    """Catalog entries of one tool instance (its CURRENT defaults are the entries' defaults, so
    call it on a fresh, un-overlaid instance for the starting values)."""
    tool = tool or tool_name(alg)
    fam, _order = group_of(tool)
    return DS.catalog_from_definitions(plugin, tool, param_defs(alg), group=fam,
                                       tool_title=_get(alg, "displayName", tool) or tool)


def set_default(alg, name, value):
    """Set a parameter's default (method on QGIS, attribute on the mock). Returns True when set."""
    for p in definitions(alg):
        if _get(p, "name", "") == name:
            setter = getattr(p, "setDefaultValue", None)
            if callable(setter):
                try:
                    setter(value)
                    return True
                except Exception:                                # noqa
                    return False
            try:
                p.defaultValue = value
                return True
            except Exception:                                    # noqa
                return False
    return False


def apply_file_defaults(alg, plugin, group_of, tool=None, settings=None):
    """Set the current settings file's values as `alg`'s parameter defaults. Returns
    ([(key, value)] applied, [(key, problem)] refused); nothing when there is no file."""
    s = settings if settings is not None else DS.load_current()
    if s is None:
        return [], []
    tool = tool or tool_name(alg)
    cat = catalog_of(alg, plugin, group_of, tool)
    applied, bad = DS.apply_defaults(s, cat, lambda st, v: set_default(alg, st.key, v))
    return [(st.key, v) for st, v in applied], [(st.key, p) for st, p in bad]


def settings_aware(cls, plugin, group_of, toolbox_group=None, step=None):
    """A subclass of a tool class whose instances take their parameter defaults from the settings
    file; `toolbox_group` = (group, group id) puts it in that Processing group. `_nd_original` is
    the class as written (its defaults are the 'starting values')."""
    if getattr(cls, "_nd_original", None) is not None:
        return cls

    def initAlgorithm(self, config=None):
        cls.initAlgorithm(self, config)
        try:
            self._nd_applied, self._nd_refused = apply_file_defaults(self, plugin, group_of)
        except Exception:                                        # noqa - never stop a tool
            self._nd_applied, self._nd_refused = [], []

    def createInstance(self):
        return type(self)()

    body = {"initAlgorithm": initAlgorithm, "createInstance": createInstance, "_nd_original": cls,
            "_nd_step": step, "__module__": cls.__module__, "__doc__": cls.__doc__}
    if toolbox_group:
        body["group"] = lambda self: toolbox_group[0]
        body["groupId"] = lambda self: toolbox_group[1]
    return type(cls.__name__, (cls,), body)


def original(alg_or_cls):
    """The un-wrapped class of a tool (class or instance)."""
    c = alg_or_cls if isinstance(alg_or_cls, type) else type(alg_or_cls)
    return getattr(c, "_nd_original", None) or c


def fresh(alg_or_cls):
    """A fresh instance of the original class with its own defaults (no file applied)."""
    inst = original(alg_or_cls)()
    inst.initAlgorithm()
    return inst


def full_catalog(classes, plugin, group_of, tab_order=(), skip=()):
    """The catalog of every tool in `classes` (wrapped or not), in settings-tab order, with the
    tools' own defaults as starting values. Tools named in `skip` are left out."""
    out = []
    for c in classes:
        try:
            inst = fresh(c)
        except Exception:                                        # noqa - a tool that needs more than the test double
            continue
        tool = tool_name(inst)
        if tool in skip:
            continue
        out += catalog_of(inst, plugin, group_of, tool)
    tabs = {t: i for i, t in enumerate(tab_order)}
    out.sort(key=lambda s: (tabs.get(s.group, 99), group_of(s.tool)[1], s.order))
    return out
