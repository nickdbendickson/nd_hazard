"""Version checks between the three ND dam suite plugins (run at plugin start-up).

    PLUGINS   key -> plugin folder name
    LABELS    key -> human name (the `name=` of each metadata.txt)
    RELEASE   key -> the version of this release set
    REQUIRES  (A, B) -> (hard_min, soft_min, reason): what A needs of B when both are installed

The keys are this package's own short names; they are not necessarily the Processing
provider ids (ND Dam Breach Screening's provider id is "nddam", its key here is "ndscreen").

Decisions:
  * `parse` takes the leading digits of each dot-separated part after any non-digit prefix
    ("v3" -> 3, "0rc1" -> 0, "beta" ignored); "" / None -> (0,). Comparisons pad with zeros,
    so "2.10" == "2.10.0".
  * A plugin that is not installed (version None) is never an error or a warning: each plugin
    works on its own; the hand-offs are optional.
  * `versions` (a {key: version} dict) replaces the QGIS lookup completely when given: a key
    missing from it counts as not installed. Used by tests and outside QGIS.
  * `installed` asks qgis.utils.pluginMetadata(folder, "version") first (QGIS returns
    "__error__" when it has no such plugin), then reads metadata.txt next to the loaded
    plugin module (qgis.utils.plugins / sys.modules). Any failure -> None, never raises.
Nothing here has been run inside a real QGIS.
"""
import configparser
import os
import re
import sys

PLUGINS = {"ndhazard": "ND_Dam_Hazard_Helper",
           "ndbreach": "nd_breach_runner",
           "ndscreen": "nd_dam_screening"}
# 1.16.0: the ND Data Updater is part of the release set but has no hand-off with the other
# three (it only writes the offline pack they read), so it is not in PLUGINS: nothing requires
# a version of it. ALL is PLUGINS plus the updater, for "which versions are installed".
UPDATER = "ndupdater"
ALL = dict(PLUGINS, ndupdater="nd_data_updater")
LABELS = {"ndhazard": "ND Dam Hazard Classification Helper",
          "ndbreach": "ND Dam Breach Runner",
          "ndscreen": "ND Dam Breach Screening",
          "ndupdater": "ND Data Updater"}
RELEASE = {"ndhazard": "2.35.1", "ndbreach": "3.27.0", "ndscreen": "0.33.1",
           "ndupdater": "1.7.0"}
# minimum version of B that A needs when both are installed: (A, B): (hard_min, soft_min, reason)
REQUIRES = {
    ("ndbreach", "ndhazard"): ("2.9.1", "2.17.0",
                               "hand-off refuses a breach raster as its own baseline from 2.9.1; "
                               "dam.json from 2.10.0; the REG_05.2026 Fig. 5.1 / 5.2 curve table "
                               "(the same curves as this plugin) from 2.11.0; the suite package "
                               "for Get data for this dam and the servers chosen in Suite data "
                               "and servers from 2.17.0"),
    ("ndhazard", "ndbreach"): ("3.1.1", "3.7.0",
                               "hand-off passes the dam folder only from 3.1.1; the culvert tool's "
                               "NDDOT inlet / outlet elevations are checked against the LiDAR only "
                               "from 3.6.0 (older Runners use them as given); a proposed embankment "
                               "in dam.json is inserted on the grid only from 3.7.0"),
    ("ndscreen", "ndbreach"): ("3.1.0", "3.7.0",
                               "tool 7b parameters; dam.json from 3.2.0; a proposed embankment is "
                               "inserted on the grid the Runner reads only from 3.7.0"),
    ("ndscreen", "ndhazard"): ("0", "2.17.0", "export for screening from 2.10.0; the REG_05.2026 "
                               "Fig. 5.1 / 5.2 curve table (the same curves as this plugin) from "
                               "2.11.0; NHD waterbodies in the offline pack and the suite package "
                               "for Get data for this dam from 2.17.0"),
}

_PART = re.compile(r"\D*(\d+)")


def parse(v):
    """Version text -> tuple of ints ("3.2.0" -> (3, 2, 0)); non-numeric parts ignored."""
    out = []
    for part in str(v or "").strip().split("."):
        m = _PART.match(part)
        if m:
            out.append(int(m.group(1)))
    return tuple(out) or (0,)


def _cmp(a, b):
    """-1 / 0 / 1 comparing two version texts (zero-padded)."""
    pa, pb = list(parse(a)), list(parse(b))
    n = max(len(pa), len(pb))
    pa += [0] * (n - len(pa))
    pb += [0] * (n - len(pb))
    return (pa > pb) - (pa < pb)


def _valid(v):
    return isinstance(v, str) and v.strip() and v.strip() != "__error__" and \
        re.search(r"\d", v) is not None


def _metadata_version(module):
    """version= of the metadata.txt next to a module's package, or None."""
    path = getattr(module, "__file__", None)
    if not path:
        return None
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    md = os.path.join(os.path.dirname(os.path.abspath(path)), "metadata.txt")
    if not os.path.isfile(md):
        return None
    with open(md, encoding="utf-8-sig") as f:
        cp.read_file(f)
    v = cp.get("general", "version", fallback=None)
    return v.strip() if _valid(v) else None


def installed(key):
    """Installed version text of plugin `key` inside QGIS, else None (never raises)."""
    try:
        folder = ALL[key]
    except (KeyError, TypeError):
        return None
    try:
        from qgis import utils as qutils
    except Exception:
        return None
    try:
        v = qutils.pluginMetadata(folder, "version")
        if _valid(v):
            return v.strip()
    except Exception:
        pass
    try:
        mod = sys.modules.get(folder)
        if mod is None:
            inst = (getattr(qutils, "plugins", None) or {}).get(folder)
            if inst is not None:
                mod = sys.modules.get(type(inst).__module__.split(".")[0])
        if mod is not None:
            return _metadata_version(mod)
    except Exception:
        pass
    return None


def _versions(versions):
    if versions is not None:
        return {k: (str(v) if v not in (None, "") else None) for k, v in versions.items()}
    return {k: installed(k) for k in PLUGINS}


def _label(key):
    return LABELS.get(key, key)


def check(me, versions=None):
    """[(level, text)] for plugin `me`: "error" below a hard minimum, "warning" below a soft
    one, of each other installed plugin it works with. [] = all fine (or none installed)."""
    vers = _versions(versions)
    out = []
    for (a, b), (hard, soft, reason) in sorted(REQUIRES.items()):
        if a != me:
            continue
        vb = vers.get(b)
        if not vb:
            continue
        if _cmp(vb, hard) < 0:
            out.append(("error", "{} needs {} {} or newer; {} is installed ({}). Update {}.".format(
                _label(a), _label(b), hard, vb, reason, _label(b))))
        elif _cmp(vb, soft) < 0:
            out.append(("warning", "{} works best with {} {} or newer; {} is installed ({})."
                        .format(_label(a), _label(b), soft, vb, reason)))
    return out


def require(me, other, versions=None):
    """None, or RuntimeError(text) when `other` is installed below what `me` needs (hard
    minimum). A plugin that is not installed, or a pair without a requirement, passes."""
    req = REQUIRES.get((me, other))
    if req is None:
        return None
    vb = _versions(versions).get(other)
    if vb and _cmp(vb, req[0]) < 0:
        raise RuntimeError("{} needs {} {} or newer; {} is installed ({}). Update {}.".format(
            _label(me), _label(other), req[0], vb, req[2], _label(other)))
    return None


def report_lines(me, versions=None):
    """One line per suite plugin for a log, e.g.
    "ND Dam Hazard Classification Helper 2.9.1 (needs 2.9.1+, recommended 2.10.0+: WARNING)"."""
    vers = _versions(versions)
    lines = []
    for key in PLUGINS:
        v = vers.get(key)
        text = "{} {}".format(_label(key), v or ("version unknown" if key == me
                                                    else "not installed"))
        if key == me:
            text += " (this plugin; release set {})".format(RELEASE[key])
        else:
            req = REQUIRES.get((me, key))
            if req:
                hard, soft = req[0], req[1]
                if not v:
                    state = "not installed"
                elif _cmp(v, hard) < 0:
                    state = "ERROR"
                elif _cmp(v, soft) < 0:
                    state = "WARNING"
                else:
                    state = "OK"
                need = "needs {}+, recommended {}+".format(hard, soft) if parse(hard) != (0,) \
                    else "recommended {}+".format(soft)
                text += " ({}: {})".format(need, state)
        lines.append(text)
    return lines
