"""The suite settings file (1.4.0): every engineer-chosen value of the three plugins in one table.

One workbook (`damsuite_settings.xlsx`, or the same table as `.csv`) holds, per plugin, the
values of the tools' scalar parameters (numbers, yes/no, choices, words) and each popup
window's own choices. The plugins read it as the DEFAULTS of their Processing tools (the
dialogs open with these values; every value can still be changed in the dialog), the popups
edit it in a grid, and a copy goes into each run's work folder as the record of what was used.

    Settings sheet columns:
      plugin | tool | key | parameter | unit | value | default | your_value | help
    plugin  the suite key (ndscreen / ndbreach / ndhazard, damsuite.versions.PLUGINS)
    tool    the Processing algorithm name (e.g. findponds), or "window" for a popup's choices
    key     the parameter name (e.g. HEAD_FT)
    value   what the engineer chose; EMPTY = the tool's own default (the `default` column)
    unit, parameter, default, your_value, help  are written for the reader and are not read back
      (the plugin knows them from its parameter definitions; `your_value` = YES marks a threshold
      the engineer chooses - the suite has no standard for it).
    About sheet: name, note, created, format, the plugin versions that wrote it.

Reading is forgiving: unknown plugins / tools / keys are kept (another plugin's rows, or a tool
of a newer version) and reported by `Settings.unknown(catalog)`; a value of the wrong kind is
reported by `coerce` and the tool's default is used. Enum choices are stored as their option
TEXT (the file stays readable); an index is accepted too.

Pure Python: no QGIS, no Qt. The plugins hold the adapters that read their Processing parameter
definitions (`catalog_from_definitions` takes plain dicts) and set the defaults
(`apply_defaults`).
"""
import csv
import datetime
import os
import re

from . import xlsx_lite

FORMAT = "ndsuite/settings"
FORMAT_VERSION = 1
SHEET = "Settings"
ABOUT = "About"
WINDOW = "window"                 # tool name of a popup window's own choices
COLUMNS = ("plugin", "tool", "key", "parameter", "unit", "value", "default", "your_value", "help")
DEFAULT_NAME = "damsuite_settings.xlsx"
ENV_PATH = "NDSUITE_SETTINGS"      # headless runs: the settings file path
SETTINGS_KEY = "ndsuite/settings_file"      # QgsSettings key shared by the three plugins
KINDS = ("number", "bool", "enum", "text")
_UNITS = {"ft": "ft", "feet": "ft", "foot": "ft", "m": "m", "metres": "m", "meters": "m",
          "acres": "acres", "acre": "acres", "ac-ft": "ac-ft", "acre-ft": "ac-ft", "acre-feet": "ac-ft",
          "degrees": "degrees", "deg": "degrees", "%": "%", "percent": "%", "cfs": "cfs",
          "h": "h", "hours": "h", "hour": "h", "minutes": "minutes", "min": "minutes", "s": "s",
          "seconds": "s", "ft/s": "ft/s", "m/s": "m/s", "miles": "miles", "cells": "cells",
          "dams": "dams", "mm": "mm", "in": "in", "inches": "in", "n": "n", "sq mi": "sq mi"}
_PAREN = re.compile(r"\(([^()]*)\)")
_TRUE = ("true", "yes", "y", "1", "on", "x")
_FALSE = ("false", "no", "n", "0", "off", "")


class SettingsError(Exception):
    """A settings file that cannot be read or written; the message says what to do."""


# ------------------------------------------------------------------ catalog --
class Setting(object):
    """One settable parameter as the plugin defines it (the catalog entry)."""
    __slots__ = ("plugin", "tool", "tool_title", "key", "label", "kind", "default", "unit", "your",
                 "help", "options", "group", "integer", "minimum", "maximum", "order")

    def __init__(self, plugin, tool, key, label, kind, default, unit="", your=False, help="",
                 options=(), group="", tool_title="", integer=False, minimum=None, maximum=None, order=0):
        self.plugin, self.tool, self.key = plugin, tool, key
        self.label, self.kind, self.default = label, kind, default
        self.unit, self.your, self.help = unit, bool(your), help
        self.options, self.group, self.tool_title = tuple(options or ()), group, tool_title
        self.integer, self.minimum, self.maximum, self.order = bool(integer), minimum, maximum, order

    @property
    def id(self):
        return (self.plugin, self.tool, self.key)

    def __repr__(self):
        return "Setting({}:{}:{} {} default={!r})".format(self.plugin, self.tool, self.key, self.kind, self.default)


def unit_of(label):
    """The unit named in a parameter label: the first word of the first parenthesis when it is a
    unit the suite uses ('Smallest water body (acres; YOUR value)' -> 'acres'), else ''."""
    for m in _PAREN.finditer(str(label or "")):
        first = re.split(r"[;,]", m.group(1))[0].strip().lower()
        first = first.split()[0] if first else ""
        if first in _UNITS:
            return _UNITS[first]
    return ""


def is_your(label):
    """True when the label marks an engineer-chosen value ('YOUR value', 'YOUR assumption', ...)."""
    return "YOUR" in str(label or "")


def short_label(label):
    """The label without its parenthesised notes ('Toe search distance (m; YOUR value)' ->
    'Toe search distance'); the notes go to the help text."""
    s = str(label or "")
    out = _PAREN.sub("", s)
    out = re.sub(r"\s{2,}", " ", out).strip().rstrip(":").strip()
    return out or s


def label_notes(label):
    """The parenthesised notes of a label, joined ('m; YOUR value')."""
    return "; ".join(m.group(1).strip() for m in _PAREN.finditer(str(label or "")) if m.group(1).strip())


def kind_of(ptype):
    """Processing parameter type -> settings kind, or None when not a settable scalar."""
    t = str(ptype or "")
    if t.endswith("Number") or t == "number" or t == "distance":
        return "number"
    if t.endswith("Boolean") or t == "boolean":
        return "bool"
    if t.endswith("Enum") or t == "enum":
        return "enum"
    if t.endswith("String") or t == "string":
        return "text"
    return None


def catalog_from_definitions(plugin, tool, defs, group="", tool_title="", skip=()):
    """Catalog entries for the scalar parameters of one tool.

    `defs`: an iterable of dicts {name, description, type, default, options, hidden, integer,
    minimum, maximum} (the plugin's adapter builds them from QgsProcessingParameterDefinition).
    Hidden parameters (the legacy metre values kept for old models) and names in `skip` are left
    out; so are layer, file, folder, field and output parameters (not scalar)."""
    out = []
    for i, d in enumerate(defs):
        kind = kind_of(d.get("type"))
        name = d.get("name")
        if kind is None or not name or d.get("hidden") or name in skip:
            continue
        label = d.get("description") or name
        default = d.get("default")
        options = tuple(d.get("options") or ())
        if kind == "enum":
            if isinstance(default, (list, tuple)):
                default = default[0] if default else 0
            try:
                default = int(default) if default is not None else 0
            except (TypeError, ValueError):
                default = 0
        elif kind == "number" and default is not None:
            try:
                default = float(default)
                if d.get("integer") and default == int(default):
                    default = int(default)
            except (TypeError, ValueError):
                default = None
        elif kind == "bool":
            default = bool(default)
        elif kind == "text":
            default = "" if default is None else str(default)
        out.append(Setting(plugin, tool, name, short_label(label), kind, default, unit=unit_of(label),
                           your=is_your(label), help=(label_notes(label) + ". " if label_notes(label) else "") +
                           str(d.get("help") or ""), options=options, group=group, tool_title=tool_title,
                           integer=bool(d.get("integer")), minimum=d.get("minimum"),
                           maximum=d.get("maximum"), order=i))
    return out


def index_catalog(catalog):
    return {s.id: s for s in catalog}


# ------------------------------------------------------------------- values --
def enum_index(value, options):
    """The option index of a stored enum value (text or index); None when unknown."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and float(value) == int(value):
        i = int(value)
        return i if 0 <= i < len(options) or not options else None
    s = str(value).strip()
    for i, opt in enumerate(options):
        if s.lower() == str(opt).strip().lower():
            return i
    if re.fullmatch(r"\d+", s):
        i = int(s)
        return i if 0 <= i < len(options) or not options else None
    return None


def coerce(value, setting):
    """(typed value, problem) for a stored value against its catalog entry. The typed value is
    what the tool takes (an enum INDEX, a float / int, a bool, a str); None = use the default,
    with `problem` saying why when the value was not usable."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, ""
    kind = setting.kind
    if kind == "bool":
        if isinstance(value, bool):
            return value, ""
        s = str(value).strip().lower()
        if s in _TRUE:
            return True, ""
        if s in _FALSE:
            return False, ""
        return None, "'{}' is not yes/no".format(value)
    if kind == "number":
        try:
            v = float(value) if not isinstance(value, bool) else None
        except (TypeError, ValueError):
            v = None
        if v is None or v != v:
            return None, "'{}' is not a number".format(value)
        if setting.minimum is not None and v < float(setting.minimum):
            return None, "{} is below the smallest allowed value {}".format(value, setting.minimum)
        if setting.maximum is not None and v > float(setting.maximum):
            return None, "{} is above the largest allowed value {}".format(value, setting.maximum)
        if setting.integer:
            return int(round(v)), ""
        return (int(v) if v == int(v) and isinstance(value, int) else v), ""
    if kind == "enum":
        i = enum_index(value, setting.options)
        if i is None:
            return None, "'{}' is not one of the choices ({})".format(value, ", ".join(map(str, setting.options)))
        return i, ""
    return str(value), ""


def display_value(value, setting):
    """The value as the file / grid shows it: enum index -> option text, bool -> TRUE/FALSE."""
    if value is None:
        return ""
    if setting is not None and setting.kind == "enum":
        i = enum_index(value, setting.options)
        if i is not None and setting.options and i < len(setting.options):
            return str(setting.options[i])
        return str(value)
    if isinstance(value, bool) or (setting is not None and setting.kind == "bool"):
        v, _p = coerce(value, setting) if setting is not None else (bool(value), "")
        return "TRUE" if v else "FALSE"
    return value


def _from_text(text):
    """A CSV cell -> bool / int / float / str."""
    if text is None:
        return None
    s = str(text).strip()
    if s == "":
        return None
    if s.upper() in ("TRUE", "FALSE"):
        return s.upper() == "TRUE"
    if re.fullmatch(r"[-+]?\d+", s):
        return int(s)
    try:
        return float(s)
    except ValueError:
        return s


# --------------------------------------------------------------------- file --
class Settings(object):
    """The values of a settings file (and the About fields). `values` is {(plugin, tool, key): value}."""

    def __init__(self, values=None, meta=None, path=""):
        self.values = dict(values or {})
        self.meta = {"name": "", "note": "", "created": "", "format": FORMAT,
                     "format_version": FORMAT_VERSION, "versions": {}}
        self.meta.update(meta or {})
        self.path = path
        self.problems = []           # (plugin, tool, key, problem) found while reading

    # -- values
    def get(self, plugin, tool, key, default=None):
        v = self.values.get((plugin, tool, key))
        return default if v is None or v == "" else v

    def set(self, plugin, tool, key, value):
        if value is None or (isinstance(value, str) and value.strip() == ""):
            self.values.pop((plugin, tool, key), None)
        else:
            self.values[(plugin, tool, key)] = value

    def typed(self, setting):
        """The stored value of a catalog entry as the tool takes it, or None (= its default)."""
        v, problem = coerce(self.values.get(setting.id), setting)
        if problem:
            self.problems.append(setting.id + (problem,))
        return v

    def window(self, plugin):
        """{key: value} of a popup window's own choices."""
        return {k: v for (p, t, k), v in self.values.items() if p == plugin and t == WINDOW}

    def set_window(self, plugin, choices, keys=None):
        for k, v in dict(choices).items():
            if keys is None or k in keys:
                self.set(plugin, WINDOW, k, v)

    def for_plugin(self, plugin):
        return {(p, t, k): v for (p, t, k), v in self.values.items() if p == plugin}

    def changed(self, catalog):
        """[(setting, stored value)] of catalog entries whose stored value differs from the default."""
        out = []
        for s in catalog:
            v, _p = coerce(self.values.get(s.id), s)
            if v is not None and not _same(v, s.default, s):
                out.append((s, v))
        return out

    def unknown(self, catalog, plugin=None):
        """Stored (plugin, tool, key) that the catalog does not know (windows' rows excluded)."""
        known = {s.id for s in catalog}
        return sorted(i for i in self.values if i not in known and i[1] != WINDOW
                      and (plugin is None or i[0] == plugin))

    # -- table
    def rows(self, catalog=None, plugin=None):
        """The Settings sheet: every catalog entry (its stored value, empty = default) and every
        stored value the catalog does not know, as lists in COLUMNS order."""
        out = [list(COLUMNS)]
        seen = set()
        for s in sorted(catalog or [], key=lambda s: (s.plugin, s.group, s.tool, s.order)):
            if plugin and s.plugin != plugin:
                continue
            v = self.values.get(s.id)
            out.append([s.plugin, s.tool, s.key, s.label, s.unit, display_value(v, s),
                        display_value(s.default, s), "YES" if s.your else "", (s.help or "").strip()])
            seen.add(s.id)
        for (p, t, k), v in sorted(self.values.items(), key=lambda kv: (kv[0][0], kv[0][1] != WINDOW, kv[0][1], kv[0][2])):
            if (p, t, k) in seen or (plugin and p != plugin):
                continue
            out.append([p, t, k, "", "", display_value(v, None), "", "", ""])
        return out

    def about_rows(self):
        rows = [["key", "value"], ["format", FORMAT], ["format_version", FORMAT_VERSION],
                ["name", self.meta.get("name", "")], ["note", self.meta.get("note", "")],
                ["created", self.meta.get("created", "")]]
        for k, v in sorted((self.meta.get("versions") or {}).items()):
            rows.append([k, v])
        return rows

    # -- save / load
    def save(self, path=None, catalog=None, name=None, note=None, versions=None, extra_sheets=()):
        """Write the file (.xlsx by xlsx_lite, .csv otherwise). Returns the path."""
        path = path or self.path
        if not path:
            raise SettingsError("no file name for the settings")
        if name is not None:
            self.meta["name"] = name
        if note is not None:
            self.meta["note"] = note
        if versions:
            self.meta["versions"] = dict(versions)
        self.meta["created"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows = self.rows(catalog)
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext == ".xlsx":
                sheets = [(SHEET, rows), (ABOUT, self.about_rows())] + list(extra_sheets)
                xlsx_lite.write(path, sheets)
            else:
                with open(path, "w", newline="", encoding="utf-8") as fh:
                    w = csv.writer(fh)
                    for r in rows:
                        w.writerow(["" if c is None else c for c in r])
                    for k, v in self.about_rows()[1:]:
                        w.writerow(["about", k, "", "", "", v, "", "", ""])
        except OSError as e:
            raise SettingsError("the settings file {} cannot be written: {}".format(path, e))
        self.path = path
        return path

    @classmethod
    def load(cls, path):
        """Read a settings file (.xlsx or .csv). Raises SettingsError with a plain message."""
        if not path or not os.path.isfile(path):
            raise SettingsError("the settings file {} does not exist".format(path or "(none)"))
        ext = os.path.splitext(path)[1].lower()
        s = cls(path=path)
        try:
            if ext == ".xlsx":
                sheets = xlsx_lite.read(path)
                table = sheets.get(SHEET)
                if table is None:
                    # the first sheet with the expected header
                    for rows in sheets.values():
                        if rows and [str(c or "").strip().lower() for c in rows[0][:3]] == ["plugin", "tool", "key"]:
                            table = rows
                            break
                if table is None:
                    raise SettingsError("{} has no '{}' sheet (columns plugin, tool, key, ..., value)".format(
                        path, SHEET))
                s._read_table(table, typed=True)
                about = sheets.get(ABOUT) or []
                for r in about[1:]:
                    if r and r[0] is not None:
                        s._about(str(r[0]), r[1] if len(r) > 1 else "")
            else:
                with open(path, newline="", encoding="utf-8-sig") as fh:
                    table = [row for row in csv.reader(fh)]
                s._read_table(table, typed=False)
        except SettingsError:
            raise
        except (OSError, ValueError, KeyError, UnicodeDecodeError) as e:
            raise SettingsError("the settings file {} cannot be read: {}".format(path, e))
        except Exception as e:                                   # noqa - a damaged zip / xml
            raise SettingsError("the settings file {} cannot be read ({}: {})".format(path, type(e).__name__, e))
        return s

    def _about(self, key, value):
        k = key.strip().lower()
        if k in ("name", "note", "created", "format"):
            self.meta[k] = "" if value is None else str(value)
        elif k == "format_version":
            try:
                self.meta[k] = int(value)
            except (TypeError, ValueError):
                pass
        elif k:
            self.meta.setdefault("versions", {})[key.strip()] = "" if value is None else str(value)

    def _read_table(self, table, typed):
        if not table:
            raise SettingsError("the settings table is empty")
        header = [str(c or "").strip().lower() for c in table[0]]
        try:
            ip, it, ik, iv = header.index("plugin"), header.index("tool"), header.index("key"), header.index("value")
        except ValueError:
            raise SettingsError("the first row must name the columns plugin, tool, key and value "
                                "(found: {})".format(", ".join(h for h in header if h)))
        for row in table[1:]:
            if not row or all(c in (None, "") for c in row):
                continue
            cells = list(row) + [None] * (len(header) - len(row))
            p = str(cells[ip] or "").strip()
            t = str(cells[it] or "").strip()
            k = cells[ik]
            v = cells[iv]
            if p.lower() == "about":
                self._about(t, v)
                continue
            if not p or not t or k in (None, ""):
                continue
            if not typed:
                v = _from_text(v)
            elif isinstance(v, str):
                v = v.strip()
                if v == "":
                    v = None
            if v is None:
                continue
            self.values[(p, t, str(k).strip())] = v


def _same(a, b, setting):
    if setting.kind == "enum":
        return enum_index(a, setting.options) == enum_index(b, setting.options)
    if setting.kind == "number":
        try:
            return abs(float(a) - float(b)) < 1e-12
        except (TypeError, ValueError):
            return False
    if setting.kind == "bool":
        return bool(a) == bool(b)
    return str(a) == str(b)


# --------------------------------------------------------- the current file --
_current = {"path": None, "loaded": None, "mtime": None}


def current_path():
    """The settings file the plugins use: set_current_path() in this process, else the
    NDSUITE_SETTINGS environment variable, else the QGIS setting shared by the three plugins."""
    if _current["path"] is not None:
        return _current["path"]
    env = os.environ.get(ENV_PATH, "").strip()
    if env:
        return env
    try:
        from qgis.core import QgsSettings
        v = QgsSettings().value(SETTINGS_KEY, "")
        return str(v or "").strip()
    except Exception:                                            # noqa
        return ""


def set_current_path(path, remember=True):
    """Make `path` the settings file of this process (and, with remember, of this QGIS profile)."""
    _current["path"] = (path or "").strip()
    _current["loaded"] = None
    _current["mtime"] = None
    if remember:
        try:
            from qgis.core import QgsSettings
            QgsSettings().setValue(SETTINGS_KEY, _current["path"])
        except Exception:                                        # noqa
            pass
    return _current["path"]


def load_current(path=None):
    """The Settings of the current file, cached until the file changes; None when there is no
    file or it cannot be read (the problem is kept in load_current.problem)."""
    path = path or current_path()
    load_current.problem = ""
    if not path:
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        load_current.problem = "the settings file {} does not exist".format(path)
        return None
    if _current["loaded"] is not None and _current["loaded"].path == path and _current["mtime"] == mtime:
        return _current["loaded"]
    try:
        s = Settings.load(path)
    except SettingsError as e:
        load_current.problem = str(e)
        return None
    _current["loaded"], _current["mtime"] = s, mtime
    return s


load_current.problem = ""


def apply_defaults(settings, catalog, set_default):
    """Set the stored values as the defaults of live parameter definitions: `set_default(setting,
    typed value)` is the plugin's adapter. Returns [(setting, value)] applied and [(setting,
    problem)] for values that were not usable."""
    applied, bad = [], []
    if settings is None:
        return applied, bad
    for s in catalog:
        raw = settings.values.get(s.id)
        if raw is None:
            continue
        v, problem = coerce(raw, s)
        if problem:
            bad.append((s, problem))
            continue
        set_default(s, v)
        applied.append((s, v))
    return applied, bad


# ------------------------------------------------------------------ presets --
def list_presets(folders):
    """[(name, path)] of the settings files in `folders` (name = the About name, else the file
    name), sorted by name; folders that do not exist are skipped."""
    out = []
    for folder in folders:
        if not folder or not os.path.isdir(folder):
            continue
        for fn in sorted(os.listdir(folder)):
            if os.path.splitext(fn)[1].lower() not in (".xlsx", ".csv"):
                continue
            p = os.path.join(folder, fn)
            name = os.path.splitext(fn)[0].replace("_", " ")
            try:
                s = Settings.load(p)
                name = (s.meta.get("name") or "").strip() or name
            except SettingsError:
                continue
            out.append((name, p))
    return sorted(out, key=lambda t: t[0].lower())
