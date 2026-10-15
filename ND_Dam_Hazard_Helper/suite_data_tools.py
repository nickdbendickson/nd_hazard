# -*- coding: utf-8 -*-
"""A dam's data for the Processing tools (1.5.0 of the suite) - no Qt widgets.

Byte-identical in ND Dam Hazard Classification Helper, ND Dam Breach Runner and ND Dam Breach
Screening. damsuite.damdata holds the logic ("Get data for this dam", dam.json's data section,
`autofill`); this module is the QGIS-facing adapter:

  * qgis_http_get(url): GET through QGIS's network stack (its proxy / SSL / authentication
    settings), urllib outside QGIS
  * dam_folder_of(alg, parameters, context): the dam folder a tool run is about (DAM_FOLDER, or
    DAMS_FOLDER + DAM_ID)
  * data_aware(cls, mapping, fields, required, folder_of): a subclass of a tool whose EMPTY
    inputs named in `mapping` / `fields` are filled from the dam folder's data (dam.json "data"
    section) and dam.json fields when the run names a dam. What the user gives is never replaced;
    each filled input is written to the run log. Parameters in `required` are made optional in the
    dialog (so they may be left empty for the dam folder to fill) and the run stops with a clear
    message when neither the user nor the dam folder gives them.
  * get_data_algorithm(base, group, group_id): the Processing tool "Get data for this dam"
    (<provider>:getdamdata), for one dam or every dam of a Dams folder

Works on QGIS 3.28 - 4.x and on the test doubles of QGIS. Nothing here has been run in a real
QGIS (simulated QGIS only).
"""
import os

from .damsuite import damdata as DD
from .damsuite import services as SV
from .damsuite import suitedata as SD


# ------------------------------------------------------------------ network --
def qgis_http_get(url, timeout=120):
    """GET through QGIS's network stack (proxy, SSL and authentication settings of QGIS); urllib
    outside QGIS. Returns the body as bytes; raises IOError with the server's message."""
    try:
        from qgis.core import QgsBlockingNetworkRequest
        from qgis.PyQt.QtCore import QUrl
        from qgis.PyQt.QtNetwork import QNetworkRequest
    except Exception:                                            # noqa
        return SV.urllib_get(url, timeout=timeout)
    req = QNetworkRequest(QUrl(url))
    try:
        req.setRawHeader(b"User-Agent", SV.USER_AGENT.encode("ascii"))
    except Exception:                                            # noqa
        pass
    br = QgsBlockingNetworkRequest()
    err = br.get(req, forceRefresh=True)
    if err != QgsBlockingNetworkRequest.NoError:
        raise IOError(br.errorMessage() or "network error {}".format(err))
    return bytes(br.reply().content())


# ------------------------------------------------------------- parameters --
def _get(obj, name, default=None):
    """An attribute that is a method on real QGIS objects and a plain attribute on the mock."""
    v = getattr(obj, name, default)
    if callable(v):
        try:
            return v()
        except Exception:                                        # noqa
            return default
    return v


def _definition(alg, name):
    try:
        p = alg.parameterDefinition(name)
        if p is not None:
            return p
    except Exception:                                            # noqa
        pass
    for p in (_get(alg, "parameterDefinitions", []) or []):
        if _get(p, "name", "") == name:
            return p
    return None


def _optional_flag():
    try:
        from qgis.core import Qgis
        return Qgis.ProcessingParameterFlag.Optional
    except Exception:                                            # noqa
        pass
    try:
        from qgis.core import QgsProcessingParameterDefinition
        return QgsProcessingParameterDefinition.FlagOptional
    except Exception:                                            # noqa
        return None


def make_optional(alg, name):
    """Mark parameter `name` optional (the dam folder may fill it). True when done."""
    p = _definition(alg, name)
    if p is None:
        return False
    flag = _optional_flag()
    try:
        if flag is not None and hasattr(p, "setFlags"):
            p.setFlags(p.flags() | flag)
            return True
    except Exception:                                            # noqa
        pass
    try:
        p.optional = True                                        # the test double
        return True
    except Exception:                                            # noqa
        return False


def _text(alg, parameters, name, context):
    if name not in parameters or parameters.get(name) in (None, ""):
        return ""
    for getter in ("parameterAsFile", "parameterAsString"):
        fn = getattr(alg, getter, None)
        if callable(fn):
            try:
                v = fn(parameters, name, context)
                if v not in (None, ""):
                    return str(v)
            except Exception:                                    # noqa
                continue
    v = parameters.get(name)
    return v if isinstance(v, str) else ""


param_text = _text                 # a text / folder parameter's value ("" when empty)


def dam_folder_of(alg, parameters, context=None):
    """The dam folder a run is about: DAM_FOLDER, else DAMS_FOLDER + DAM_ID (an existing folder
    holding dam.json or the legacy location file), else ""."""
    parameters = parameters or {}
    f = _text(alg, parameters, "DAM_FOLDER", context)
    if f and os.path.isdir(f):
        return f
    dams = _text(alg, parameters, "DAMS_FOLDER", context)
    did = _text(alg, parameters, "DAM_ID", context).strip()
    if dams and did:
        f = os.path.join(dams, did)
        if os.path.isdir(f):
            return f
    return ""


def _empty(v):
    return v is None or (isinstance(v, str) and not v.strip())


def fill_parameters(alg, parameters, context, mapping, fields=None, folder_of=None):
    """(parameters with empty inputs filled, notes, dam folder). `mapping` {parameter: data
    role}, `fields` {parameter: dam.json field}; only parameters the tool has are filled."""
    params = dict(parameters or {})
    folder = (folder_of or dam_folder_of)(alg, params, context)
    if not folder:
        return params, [], ""
    names = [n for n in list(mapping or {}) + list(fields or {}) if _definition(alg, n) is not None]
    values = {n: params.get(n) for n in names if _empty(params.get(n))}
    if not values:
        return params, [], folder
    filled, notes, used = DD.autofill(values, {k: v for k, v in (mapping or {}).items()
                                               if k in values},
                                      folder, fields={k: v for k, v in (fields or {}).items()
                                                      if k in values})
    for n in used:
        params[n] = filled[n]
    return params, notes, folder


def data_aware(cls, mapping, fields=None, required=(), folder_of=None, hint=""):
    """A subclass of tool class `cls` (a settings_aware one too) whose empty inputs are filled
    from the dam folder. `required`: parameters made optional in the dialog that must still end
    up with a value (the dam folder's or the user's)."""
    if getattr(cls, "_nd_data_aware", False):
        return cls
    mapping = dict(mapping or {})
    fields = dict(fields or {})
    required = tuple(required or ())

    def initAlgorithm(self, config=None):
        cls.initAlgorithm(self, config)
        for n in required:
            make_optional(self, n)

    def processAlgorithm(self, parameters, context, feedback):
        try:
            params, notes, folder = fill_parameters(self, parameters, context, mapping, fields,
                                                    folder_of)
        except Exception as e:                                   # noqa - never stop a tool here
            params, notes, folder = dict(parameters or {}), [], ""
            if feedback is not None:
                feedback.pushInfo("Dam folder data not read: {}".format(e))
        if feedback is not None:
            for n in notes:
                feedback.pushInfo(n)
        missing = [n for n in required if _empty(params.get(n))]
        if missing:
            from qgis.core import QgsProcessingException
            where = "the dam folder '{}' has none".format(os.path.basename(folder)) if folder \
                else "the run names no existing dam folder"
            bad = [n for n in notes if n.startswith("dam.json not read")]
            if bad:
                where = bad[0]
            raise QgsProcessingException(
                "{} needed: choose {} in the dialog, or run Plugins > ND Dam Suite > Suite data "
                "and servers > Get data for this dam first ({}){}.".format(
                    ", ".join(missing), "it" if len(missing) == 1 else "them", where, hint))
        return cls.processAlgorithm(self, params, context, feedback)

    def createInstance(self):
        return type(self)()

    body = {"initAlgorithm": initAlgorithm, "processAlgorithm": processAlgorithm,
            "createInstance": createInstance, "_nd_data_aware": True,
            "_nd_data_mapping": mapping, "_nd_data_fields": fields,
            "__module__": cls.__module__, "__doc__": cls.__doc__}
    for k in ("_nd_original", "_nd_step"):
        if hasattr(cls, k):
            body[k] = getattr(cls, k)
    return type(cls.__name__, (cls,), body)


# -------------------------------------------------------- Get data (tool) --
GET_DATA_HELP = (
    "Fills <dam folder>/data/ with what the dam's tools need and records the files in dam.json's "
    "data section: the DEM (your own DEM in the suite data folder's dem/, else the terrain server "
    "chosen in Suite data and servers, through the shared tile cache), land cover, building "
    "outlines and points, address points, roads, railroads, grade crossings, bridges, lifelines, "
    "NID and DWR dams, DWR dam lines, 3DHP flowlines and waterbodies, FEMA flood zones and counties "
    "(the offline pack when it covers the dam - Data source in Suite data and servers, where the "
    "pack location is chosen - else your files in dwr/ / hifld/, else the chosen server; with "
    "Data source = Download the pack is not read). The dam's NID / DWR values (height, storage, county, class, "
    "location) are offered to dam.json; a value already there is not replaced. Afterwards every "
    "tool of the three ND dam plugins fills its EMPTY inputs from the dam folder; your own choices "
    "are never replaced. Leave Dam ID empty for every dam of the Dams folder. Data already there "
    "is kept unless 'Replace' is ticked. One kind of data failing never stops the others: the log "
    "says what failed and why. Not run in a real QGIS (simulated QGIS only).")


def get_data_algorithm(base, group="", group_id=""):
    """The "Get data for this dam" tool as a subclass of the provider's algorithm base class."""
    from qgis.core import (
        QgsProcessingException, QgsProcessingParameterFile, QgsProcessingParameterString,
        QgsProcessingParameterNumber, QgsProcessingParameterBoolean, QgsProcessingOutputString,
        QgsProcessingOutputFolder,
    )
    try:
        from qgis.core import Qgis
        FOLDER = Qgis.ProcessingFileParameterBehavior.Folder
        DOUBLE = Qgis.ProcessingNumberParameterType.Double
    except Exception:                                            # noqa - QGIS 3.28 / the test double
        FOLDER = getattr(QgsProcessingParameterFile, "Folder", 1)
        DOUBLE = getattr(QgsProcessingParameterNumber, "Double", 1)

    class GetDamDataAlgorithm(base):
        def name(self):
            return "getdamdata"

        def displayName(self):
            return "Get data for this dam (or all dams)"

        def group(self):
            return group or base.group(self)

        def groupId(self):
            return group_id or base.groupId(self)

        def shortHelpString(self):
            return GET_DATA_HELP

        def createInstance(self):
            return type(self)()

        def initAlgorithm(self, config=None):
            P = self.addParameter
            P(QgsProcessingParameterFile("DAMS_FOLDER", "Dams folder (one folder per dam)",
                                         behavior=FOLDER, defaultValue=SD.dams_folder() or None,
                                         optional=True))
            P(QgsProcessingParameterString("DAM_ID", "Dam ID (the dam's folder name; empty = "
                                           "every dam of the Dams folder)", defaultValue="",
                                           optional=True))
            P(QgsProcessingParameterFile("DAM_FOLDER", "or: the dam folder itself",
                                         behavior=FOLDER, optional=True))
            P(QgsProcessingParameterFile("DATA_FOLDER", "Suite data folder (offline pack, "
                                         "terrain cache, dem/, nlcd/, dwr/, hifld/)",
                                         behavior=FOLDER, defaultValue=SD.data_folder() or None,
                                         optional=True))
            P(QgsProcessingParameterNumber("VECTOR_MI", "Data within (mi of the dam)", type=DOUBLE,
                                           defaultValue=DD.DEFAULT_VECTOR_HALF_MI, minValue=1.0,
                                           maxValue=100.0))
            P(QgsProcessingParameterNumber("DEM_MI", "Terrain within (mi of the dam)", type=DOUBLE,
                                           defaultValue=DD.DEFAULT_DEM_HALF_MI, minValue=1.0,
                                           maxValue=100.0))
            P(QgsProcessingParameterNumber("DEM_CELL_M", "Terrain cell size of downloads (m; "
                                           "10 m = 32.8 ft; your own DEM keeps its cells)",
                                           type=DOUBLE, defaultValue=DD.DEFAULT_DEM_CELL_M,
                                           minValue=1.0, maxValue=30.0))
            P(QgsProcessingParameterBoolean("OVERWRITE", "Replace data already there",
                                            defaultValue=False))
            self.addOutput(QgsProcessingOutputFolder("DATA", "The dam's data folder (one dam)"))
            self.addOutput(QgsProcessingOutputString("SUMMARY", "What was written"))

        def processAlgorithm(self, parameters, context, feedback):
            one = _text(self, parameters, "DAM_FOLDER", context)
            dams = _text(self, parameters, "DAMS_FOLDER", context) or SD.dams_folder()
            did = _text(self, parameters, "DAM_ID", context).strip()
            if one:
                folders = [one]
            elif did:
                folders = [os.path.join(dams, did)] if dams else []
            else:
                folders = DD.dam_folders(dams)
            folders = [f for f in folders if f and os.path.isdir(f)]
            if not folders:
                raise QgsProcessingException(
                    "No dam folder: give the Dams folder and the Dam ID, or the dam folder (a "
                    "folder holding dam.json).")
            data = _text(self, parameters, "DATA_FOLDER", context) or SD.data_folder()
            if not data and not SD.active_pack(""):
                raise QgsProcessingException(
                    "No suite data folder: choose one here or in Plugins > ND Dam Suite > Suite "
                    "data and servers.")

            def num(name, default):
                try:
                    return float(self.parameterAsDouble(parameters, name, context))
                except Exception:                                # noqa
                    return default
            try:
                over = bool(self.parameterAsBoolean(parameters, "OVERWRITE", context))
            except Exception:                                    # noqa
                over = bool(parameters.get("OVERWRITE"))
            pack = SD.active_pack(data or "")                  # 1.10.0: "" in Download mode
            if feedback is not None:
                feedback.pushInfo("Data source: {}{}".format(
                    SD.SOURCE_LABELS.get(SD.data_source(), SD.data_source()),
                    " ({})".format(pack) if pack else ""))
            make = None
            try:
                from . import suite_data as _UI                  # ND Dam Hazard's suite export
                make = _UI.make_package_fn(pack)
            except Exception:                                    # noqa - no Qt (qgis_process)
                make = None
            cancel = (lambda: feedback.isCanceled()) if feedback is not None else None
            lines, last = [], ""
            for i, f in enumerate(folders):
                if cancel and cancel():
                    break

                def prog(p, m="", i=i):
                    if feedback is not None:
                        feedback.setProgress((100.0 * i + float(p)) / len(folders))
                try:
                    r = DD.get_data_for_dam(
                        f, data_folder=data, http_get=qgis_http_get, make_package=make,
                        vector_half_mi=num("VECTOR_MI", DD.DEFAULT_VECTOR_HALF_MI),
                        dem_half_mi=num("DEM_MI", DD.DEFAULT_DEM_HALF_MI),
                        dem_cell_m=num("DEM_CELL_M", DD.DEFAULT_DEM_CELL_M), overwrite=over,
                        log=(feedback.pushInfo if feedback is not None else None),
                        progress=prog, cancel=cancel)
                    line = "{}: {} written, {} failed, {} kept".format(
                        os.path.basename(f), len(r["done"]), len(r["failed"]), len(r["skipped"]))
                    for role, why in sorted(r["failed"].items()):
                        line += "\n   {} failed: {}".format(DD.LABELS.get(role, role), why)
                    last = os.path.join(f, DD.DATA_DIR)
                except Exception as e:                           # noqa - one dam never stops the rest
                    line = "{}: {}".format(os.path.basename(f), e)
                    if len(folders) == 1:
                        raise QgsProcessingException(line)
                lines.append(line)
                if feedback is not None:
                    feedback.pushInfo(line)
            return {"DATA": last if len(folders) == 1 else "", "SUMMARY": "\n".join(lines)}

    return GetDamDataAlgorithm
