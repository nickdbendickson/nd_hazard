"""The suite data folder (1.5.0): one folder per computer that the three plugins read their data
from, set once.

    <suite data folder>/
      nd_offline_pack.zip     the statewide vector pack (ND Dam Hazard's build_nd_pack; an
                              unpacked pack folder works too)
      terrain/                the shared terrain tile cache (terrain_cache)
      nlcd/                   land cover rasters (*.tif / *.img / *.vrt): NLCD for the state
      dem/                    your own DEMs (*.tif / *.vrt), e.g. a statewide 1 m LiDAR mosaic
      dwr/                    drop-in folder: ND DWR dams inventory and dam lines (any vector
                              format; names containing "line" are taken for the dam lines)
      hifld/                  drop-in folder: HIFLD layers from hsdl.org
      services.json           the user's servers and choices (services)
      manifest.json           what "Update data" did last, per data set

Where the folder is (first that is set): the NDSUITE_DATA environment variable, the QGIS setting
`ndsuite/data_folder` (shared by the three plugins, per QGIS profile), else the suite config file
~/.ndsuite/config.json (outside QGIS). The Dams folder works the same way (NDSUITE_DAMS /
`ndsuite/dams_folder`): the three plugins open on the same Dams folder.

Nothing here downloads; `status()` only looks at the files. Pure Python standard library (QGIS's
QgsSettings is used when it can be imported).
"""
import datetime
import json
import os

KEY_DATA = "ndsuite/data_folder"
KEY_DAMS = "ndsuite/dams_folder"
ENV_DATA = "NDSUITE_DATA"
ENV_DAMS = "NDSUITE_DAMS"
CONFIG = os.path.join(os.path.expanduser("~"), ".ndsuite", "config.json")

PACK_NAMES = ("nd_offline_pack.zip", "nd_offline_pack")
TERRAIN, NLCD, DEM, DWR, HIFLD = "terrain", "nlcd", "dem", "dwr", "hifld"
SUBFOLDERS = (TERRAIN, NLCD, DEM, DWR, HIFLD)
MANIFEST = "manifest.json"
RASTER_EXT = (".tif", ".tiff", ".vrt", ".img")
VECTOR_EXT = (".gpkg", ".shp", ".geojson", ".json", ".kml", ".kmz", ".zip", ".gdb", ".csv")
STALE_DAYS = 365          # a pack data set older than this is offered for refresh


# ------------------------------------------------------------------ settings --
def _qsettings():
    try:
        from qgis.core import QgsSettings
        return QgsSettings()
    except Exception:                                            # noqa
        return None


def _read_config():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _get(key, env):
    v = os.environ.get(env)
    if v:
        return v.strip()
    st = _qsettings()
    if st is not None:
        try:
            v = st.value(key, "") or ""
            if v:
                return str(v).strip()
        except Exception:                                        # noqa
            pass
    return str(_read_config().get(key, "") or "").strip()


def _set(key, value):
    value = (value or "").strip()
    st = _qsettings()
    if st is not None:
        try:
            st.setValue(key, value)
            return
        except Exception:                                        # noqa
            pass
    d = _read_config()
    d[key] = value
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    with open(CONFIG + ".tmp", "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)
    os.replace(CONFIG + ".tmp", CONFIG)


def data_folder():
    """The suite data folder ("" when none is set)."""
    return _get(KEY_DATA, ENV_DATA)


def set_data_folder(path, create=True):
    path = (path or "").strip()
    if path and create:
        ensure_layout(path)
    _set(KEY_DATA, path)
    return path


def dams_folder():
    """The suite's Dams folder ("" when none is set)."""
    return _get(KEY_DAMS, ENV_DAMS)


def set_dams_folder(path):
    _set(KEY_DAMS, path)
    return (path or "").strip()


# -------------------------------------------------------------------- layout --
def ensure_layout(folder):
    """Create the folder and its sub-folders (existing files are never touched)."""
    os.makedirs(folder, exist_ok=True)
    for sub in SUBFOLDERS:
        os.makedirs(os.path.join(folder, sub), exist_ok=True)
    return folder


def sub(folder, name):
    return os.path.join(folder, name) if folder else ""


def find_pack(folder=None):
    """The offline pack in the data folder (zip or unpacked folder), or ""."""
    folder = data_folder() if folder is None else folder
    if not folder:
        return ""
    for n in PACK_NAMES:
        p = os.path.join(folder, n)
        if n.endswith(".zip") and os.path.isfile(p):
            return p
        if not n.endswith(".zip") and os.path.isdir(p) and \
                os.path.isfile(os.path.join(p, "manifest.json")):
            return p
    # any other *pack*.zip (a renamed pack)
    try:
        for n in sorted(os.listdir(folder)):
            if n.lower().endswith(".zip") and "pack" in n.lower():
                return os.path.join(folder, n)
    except OSError:
        pass
    return ""


def _files(folder, exts, recurse=True):
    out = []
    if not folder or not os.path.isdir(folder):
        return out
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith((".", "_")) and not
                   d.lower().endswith(".gdb")] if recurse else []
        for d in list(os.listdir(root)):
            if d.lower().endswith(".gdb") and ".gdb" in exts and os.path.isdir(
                    os.path.join(root, d)):
                out.append(os.path.join(root, d))
        out += [os.path.join(root, f) for f in files if f.lower().endswith(exts)
                and not f.startswith(".")]
        if not recurse:
            break
    return sorted(set(out))


def rasters(folder, name):
    """Raster files in a sub-folder (nlcd/, dem/)."""
    return _files(sub(folder, name), RASTER_EXT)


def dropins(folder, name):
    """Vector files in a drop-in folder (dwr/, hifld/)."""
    return _files(sub(folder, name), VECTOR_EXT)


def dwr_layers(folder=None):
    """{"dwr_dams", "dwr_dam_lines", "dwr_unknown", "dwr_dugouts", "dwr_dugout_polygons": path or ""}
    from the dwr/ drop-in folder: a file whose name has "line" in it is the dam lines; 1.7.0: names
    with "unknown" are DWR's unknown structures, "dugout" its dugouts ("poly" in the name: the
    dugout polygons); the first other file is the dams inventory. The unknown-structure and dugout
    files are also looked for in dwr/_other/ (where the offline pack keeps them)."""
    folder = data_folder() if folder is None else folder
    out = {"dwr_dams": "", "dwr_dam_lines": "", "dwr_unknown": "", "dwr_dugouts": "",
           "dwr_dugout_polygons": ""}
    d = sub(folder, DWR)
    files = dropins(folder, DWR) + (_files(os.path.join(d, "_other"), VECTOR_EXT) if d else [])
    for p in files:
        base = os.path.basename(p).lower()
        other = os.sep + "_other" + os.sep in p
        if "dugout" in base:
            key = "dwr_dugout_polygons" if "poly" in base else "dwr_dugouts"
        elif "unknown" in base:
            key = "dwr_unknown"
        elif other:
            continue
        elif "line" in base or "linear" in base or "embank" in base:
            key = "dwr_dam_lines"
        else:
            key = "dwr_dams"
        out[key] = out[key] or p
    return out


def local_dem(folder=None):
    """The user's own DEM in dem/: a .vrt first (a mosaic), else the only raster, else ""."""
    folder = data_folder() if folder is None else folder
    files = rasters(folder, DEM)
    vrts = [p for p in files if p.lower().endswith(".vrt")]
    if vrts:
        return vrts[0]
    return files[0] if len(files) == 1 else ""


def nlcd_raster(folder=None):
    folder = data_folder() if folder is None else folder
    files = rasters(folder, NLCD)
    return files[0] if files else ""


# ------------------------------------------------------------------ manifest --
def read_manifest(folder=None):
    folder = data_folder() if folder is None else folder
    try:
        with open(os.path.join(folder, MANIFEST), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def write_manifest(folder, data):
    p = os.path.join(folder, MANIFEST)
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(p + ".tmp", p)
    return p


def note_update(folder, name, **info):
    """Record in manifest.json that a data set was updated now (info: source, count, note)."""
    d = read_manifest(folder)
    sets = d.setdefault("datasets", {})
    e = dict(info)
    e["when"] = datetime.datetime.now().isoformat(timespec="seconds")
    sets[name] = e
    d["updated"] = e["when"]
    return write_manifest(folder, d)


def age_days(when, now=None):
    """Days since an ISO date / time text (None when unreadable)."""
    try:
        t = datetime.datetime.fromisoformat(str(when).strip()[:19])
    except (TypeError, ValueError):
        try:
            t = datetime.datetime.strptime(str(when).strip()[:10], "%Y-%m-%d")
        except (TypeError, ValueError):
            return None
    now = now or datetime.datetime.now()
    return max(0.0, (now - t).total_seconds() / 86400.0)


def pack_datasets(pack_path):
    """[(name, fetched, count, status)] from a pack's manifest ([] when unreadable)."""
    if not pack_path:
        return []
    try:
        from . import offline_pack as OP
        man = OP.read_manifest(pack_path) or {}
    except Exception:                                            # noqa
        return []
    out = []
    for name, info in sorted((man.get("datasets") or {}).items()):
        if isinstance(info, dict):
            out.append((name, info.get("fetched") or info.get("when") or "",
                        info.get("count"), info.get("status", "")))
    return out


def stale(datasets, max_age_days=STALE_DAYS, now=None):
    """Names of pack data sets older than max_age_days, or not complete."""
    out = []
    for name, fetched, _count, status in datasets:
        a = age_days(fetched, now)
        if a is None or a > max_age_days or (status and status != "complete"):
            out.append(name)
    return out


def _size(path):
    total = 0
    for root, _d, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def status(folder=None, now=None):
    """What the data folder holds: {"folder", "exists", "pack", "pack_sets", "pack_stale",
    "terrain_tiles", "terrain_mb", "nlcd", "dem", "dwr", "hifld", "problems"}."""
    folder = data_folder() if folder is None else folder
    out = {"folder": folder, "exists": bool(folder and os.path.isdir(folder)), "pack": "",
           "pack_sets": [], "pack_stale": [], "terrain_tiles": 0, "terrain_mb": 0.0,
           "nlcd": "", "dem": "", "dwr": {}, "hifld": [], "problems": []}
    if not folder:
        out["problems"].append("No suite data folder is set.")
        return out
    if not out["exists"]:
        out["problems"].append("The suite data folder does not exist: " + folder)
        return out
    out["pack"] = find_pack(folder)
    out["pack_sets"] = pack_datasets(out["pack"])
    out["pack_stale"] = stale(out["pack_sets"], now=now)
    if not out["pack"]:
        out["problems"].append("No offline pack in the folder (Update data builds one).")
    tdir = sub(folder, TERRAIN)
    tiles = [p for p in _files(tdir, (".tif",))]
    out["terrain_tiles"] = len(tiles)
    out["terrain_mb"] = round(_size(tdir) / 1e6, 1) if os.path.isdir(tdir) else 0.0
    out["nlcd"] = nlcd_raster(folder)
    out["dem"] = local_dem(folder)
    out["dwr"] = dwr_layers(folder)
    out["hifld"] = dropins(folder, HIFLD)
    return out


def describe(st):
    """Plain lines for the status box."""
    if not st.get("folder"):
        return ["No suite data folder is set. Choose one: the three plugins read their data from it."]
    lines = ["Suite data folder: " + st["folder"]]
    if not st.get("exists"):
        return lines + ["  (does not exist)"]
    if st["pack"]:
        lines.append("Offline pack: {} ({} data sets{})".format(
            os.path.basename(st["pack"]), len(st["pack_sets"]),
            ", {} to refresh".format(len(st["pack_stale"])) if st["pack_stale"] else ", all current"))
    else:
        lines.append("Offline pack: none yet")
    lines.append("Terrain cache: {} tiles, {} MB".format(st["terrain_tiles"], st["terrain_mb"]))
    lines.append("Your DEM: " + (st["dem"] or "none (dem/ folder)"))
    lines.append("Land cover: " + (st["nlcd"] or "none (nlcd/ folder; Update data can fetch it)"))
    d = st.get("dwr") or {}
    lines.append("DWR dams: " + (d.get("dwr_dams") or "none (put the DWR export in dwr/)"))
    lines.append("DWR dam lines: " + (d.get("dwr_dam_lines") or "none (dwr/, a file named *line*)"))
    lines.append("DWR unknown structures: " + (d.get("dwr_unknown") or "none (dwr/, a file named *unknown*)"))
    lines.append("DWR dugouts: " + (d.get("dwr_dugouts") or "none (dwr/, a file named *dugout*)") +
                 (" + polygons" if d.get("dwr_dugout_polygons") else ""))
    lines.append("HIFLD: {} file(s)".format(len(st.get("hifld") or [])))
    return lines
