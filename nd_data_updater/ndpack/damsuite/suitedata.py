"""The suite data folder (1.5.0): one folder per computer that the three plugins read their data
from, set once.

    <suite data folder>/
      ND_Dam_Suite_Pack/      1.10.0: the statewide offline pack, one GeoPackage per layer
                              (gpkgpack; it can also live anywhere else: the pack location)
      nd_offline_pack.zip     the format-1 pack (ND Dam Hazard's build_nd_pack before 1.10.0; an
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

1.10.0: the data source, shared by the three plugins (`ndsuite/data_source`, env NDSUITE_SOURCE):
"pack" (default) - every data set the offline pack holds is read from it, at the pack location
(`ndsuite/pack_path`, env NDSUITE_PACK; empty = the pack in the suite data folder); "download" -
the pack is not read, the data sets come from the servers. A format-2 pack also holds the DWR
files, the HIFLD layers and the land-cover raster, so nothing else has to be dropped into dwr/,
hifld/ or nlcd/ (those folders still work when the pack lacks a layer).

Nothing here downloads; `status()` only looks at the files. Pure Python standard library (QGIS's
QgsSettings is used when it can be imported).
"""
import datetime
import json
import os

KEY_DATA = "ndsuite/data_folder"
KEY_DAMS = "ndsuite/dams_folder"
KEY_PACK = "ndsuite/pack_path"            # 1.10.0
KEY_SOURCE = "ndsuite/data_source"        # 1.10.0: "pack" | "download"
KEY_FILES = "ndsuite/layer_files"         # 1.19.0: folder with the DWR / HIFLD files to build in
ENV_DATA = "NDSUITE_DATA"
ENV_DAMS = "NDSUITE_DAMS"
ENV_PACK = "NDSUITE_PACK"
ENV_SOURCE = "NDSUITE_SOURCE"
ENV_FILES = "NDSUITE_LAYER_FILES"
SOURCE_PACK, SOURCE_DOWNLOAD = "pack", "download"
SOURCE_LABELS = {SOURCE_PACK: "Offline pack", SOURCE_DOWNLOAD: "Download from the servers"}
CONFIG = os.path.join(os.path.expanduser("~"), ".ndsuite", "config.json")

PACK_NAMES = ("ND_Dam_Suite_Pack", "nd_offline_pack.zip", "nd_offline_pack")
# 1.10.0: suite role -> data set of a format-2 pack that the role's file comes from
PACK_FILES = {"dwr_dams": "DWR_dams", "dwr_dam_lines": "DWR_dam_lines",
              "dwr_unknown": "DWR_unknown_structures", "dwr_dugouts": "DWR_dugouts",
              "dwr_dugout_polygons": "DWR_dugout_polygons"}
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


def adopt_folder(path, dams=True, source=True):
    """1.16.0: one folder chosen once - the whole suite set up from a single path.

    Sets the suite data folder (creating its sub-folders), points the pack location at the pack
    in it when there is one (and clears a pack location that no longer exists), switches the data
    source to the pack when a pack is there, and takes <path>/Dams as the Dams folder when none is
    set. Returns {"folder", "pack", "dams", "source", "created"}: what it set, so a window can
    say so.

    This is what the ND Data Updater's share step and every "Suite folder" chooser call, so a
    user who sets the folder in one window never has to set it again in another - the two
    settings drifting apart (the updater's data folder vs the plugins' suite data folder) was
    the commonest first-run problem."""
    path = (path or "").strip()
    out = {"folder": path, "pack": "", "dams": "", "source": data_source(), "created": []}
    if not path:
        return out
    before = [s for s in SUBFOLDERS if not os.path.isdir(os.path.join(path, s))]
    ensure_layout(path)
    out["created"] = before
    _set(KEY_DATA, path)
    pack = find_pack(path)
    loc = pack_location()
    if pack:
        # the pack in this folder wins; a location elsewhere that still exists is left alone
        if not loc or not os.path.exists(loc) or os.path.abspath(loc) == os.path.abspath(path):
            set_pack_location(pack)
            out["pack"] = pack
        else:
            out["pack"] = loc
        if source and data_source() != SOURCE_PACK:
            set_data_source(SOURCE_PACK)
        out["source"] = data_source()
    elif loc and not os.path.exists(loc):
        set_pack_location("")                      # a stale location only confuses the status
    if dams and not dams_folder():
        d = os.path.join(path, "Dams")
        os.makedirs(d, exist_ok=True)
        set_dams_folder(d)
        out["dams"] = d
    else:
        out["dams"] = dams_folder()
    return out


def dams_folder():
    """The suite's Dams folder ("" when none is set)."""
    return _get(KEY_DAMS, ENV_DAMS)


def set_dams_folder(path):
    _set(KEY_DAMS, path)
    return (path or "").strip()


def data_source():
    """1.10.0: "pack" (read the offline pack) or "download" (the servers; the pack is not read)."""
    v = _get(KEY_SOURCE, ENV_SOURCE).lower()
    return SOURCE_DOWNLOAD if v == SOURCE_DOWNLOAD else SOURCE_PACK


def set_data_source(value):
    value = SOURCE_DOWNLOAD if str(value or "").lower() == SOURCE_DOWNLOAD else SOURCE_PACK
    _set(KEY_SOURCE, value)
    return value


def pack_location():
    """1.10.0: the pack chosen in Suite data and servers (a pack folder, a file in it, or a
    format-1 .zip), or "" (the pack in the suite data folder is used)."""
    return _get(KEY_PACK, ENV_PACK)


def set_pack_location(path):
    _set(KEY_PACK, path)
    return (path or "").strip()


def layer_files_folder():
    """1.19.0: an extra folder with the DWR / HIFLD layer files that "Build the offline pack" writes
    into the pack (the suite data folder's dwr/ and hifld/ are searched anyway), or ""."""
    return _get(KEY_FILES, ENV_FILES)


def set_layer_files_folder(path):
    _set(KEY_FILES, path)
    return (path or "").strip()


def active_pack(folder=None):
    """1.10.0: the pack the plugins read: "" with Data source = Download; the pack location when
    it exists; else the pack in the suite data folder (`folder`, default the suite's)."""
    if data_source() == SOURCE_DOWNLOAD:
        return ""
    loc = pack_location()
    if loc and os.path.exists(loc):
        try:
            from . import gpkgpack as GP
            return GP.pack_folder_of(loc) or loc
        except Exception:                                        # noqa
            return loc
    return find_pack(folder)


_PACK_MANIFESTS = {}


def _gpkg_manifest(pack):
    """The manifest of a format-2 pack folder (cached by file time), or {}."""
    if not pack or not os.path.isdir(pack):
        return {}
    p = os.path.join(pack, MANIFEST)
    try:
        stamp = os.path.getmtime(p)
    except OSError:
        return {}
    hit = _PACK_MANIFESTS.get(pack)
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        from . import gpkgpack as GP
        m = GP.read_manifest(pack)
        m = m if GP.is_gpkg_manifest(m) else {}
    except Exception:                                            # noqa
        m = {}
    _PACK_MANIFESTS[pack] = (stamp, m)
    return m


def pack_file(name, pack=None, kind="datasets"):
    """1.10.0: the file of data set (kind "datasets") or raster (kind "rasters") `name` in a
    format-2 pack (the active pack by default), or "" (no such layer, Download mode, a
    format-1 pack)."""
    pack = active_pack() if pack is None else pack
    e = (_gpkg_manifest(pack).get(kind) or {}).get(name) or {}
    if not e.get("file"):
        return ""
    p = os.path.join(pack, *e["file"].split("/"))
    return p if os.path.isfile(p) else ""


def pack_entries(pack=None, kind="datasets"):
    """{name: manifest entry} of a format-2 pack's data sets / rasters ({} otherwise)."""
    pack = active_pack() if pack is None else pack
    return dict(_gpkg_manifest(pack).get(kind) or {})


def hifld_layers(folder=None):
    """1.10.0: [(path, category or None)] of the HIFLD lifeline layers: the active pack's HIFLD_*
    layers (category from its manifest), then the files in hifld/."""
    folder = data_folder() if folder is None else folder
    pack = active_pack(folder)
    out = []
    for name, e in sorted(pack_entries(pack).items()):
        if e.get("group") == "hifld" or name.startswith("HIFLD_"):
            p = pack_file(name, pack)
            if p:
                out.append((p, e.get("category")))
    out += [(p, None) for p in dropins(folder, HIFLD)]
    return out


# 1.16.0: what each sub-folder is for and where its files come from, for the "Open folder"
# rows of the settings windows (one line each, so a user never has to guess a file name)
SUBFOLDER_INFO = (
    (DWR, "DWR exports", "The ND DWR dams inventory, the dam lines, and (optional) the unknown "
     "structures and dugouts. A file name containing \"line\" is taken for the dam lines, "
     "\"unknown\" for the unknown structures, \"dugout\" for the dugouts.",
     "ND State Water Commission / DWR"),
    (HIFLD, "HIFLD lifelines", "Fire and EMS stations, emergency operations centres, law "
     "enforcement and wastewater plants. Any vector format; the file names are matched to the "
     "lifeline kinds.", "hsdl.org (HIFLD open data)"),
    (DEM, "Your own DEM", "A statewide or county elevation mosaic (.tif, or a .vrt pointing at "
     "tiles). With one here no run has to download terrain.", "USGS 3DEP, or ND LiDAR"),
    (NLCD, "Land cover", "One NLCD raster for the state, used for Manning's n. The updater can "
     "fetch this into the pack instead.", "MRLC (NLCD)"),
    (TERRAIN, "Terrain cache", "Written by the plugins as you run dams; nothing to put here. "
     "Delete it to free space.", ""),
)


def subfolder_info(folder=None):
    """[(name, path, label, what it is for, where it comes from, file count)] for the sub-folders,
    for the rows the settings windows draw with an "Open folder" button."""
    folder = data_folder() if folder is None else folder
    out = []
    for name, label, why, where in SUBFOLDER_INFO:
        p = sub(folder, name)
        if name in (NLCD, DEM):
            n = len(rasters(folder, name))
        elif name == TERRAIN:
            n = len(_files(p, (".tif",)))
        else:
            n = len(dropins(folder, name))
        out.append((name, p, label, why, where, n))
    return out


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
    """The offline pack in the data folder (1.10.0: the folder itself when it is a format-2 pack,
    ND_Dam_Suite_Pack/ or any other format-2 pack folder in it; a format-1 zip or unpacked
    folder), or ""."""
    folder = data_folder() if folder is None else folder
    if not folder:
        return ""
    if os.path.isdir(folder) and _gpkg_manifest(folder):
        return folder
    for n in PACK_NAMES:
        p = os.path.join(folder, n)
        if n.endswith(".zip") and os.path.isfile(p):
            return p
        if not n.endswith(".zip") and os.path.isdir(p) and \
                os.path.isfile(os.path.join(p, "manifest.json")):
            return p
    try:
        for n in sorted(os.listdir(folder)):
            p = os.path.join(folder, n)
            if os.path.isdir(p) and not n.startswith((".", "_")) and _gpkg_manifest(p):
                return p
    except OSError:
        pass
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
    # 1.10.0: the active format-2 pack's DWR layers first; dwr/ for what the pack lacks
    pack = active_pack(folder)
    for key, name in PACK_FILES.items():
        out[key] = pack_file(name, pack) if pack else ""
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
    """The land-cover raster: 1.10.0 the active pack's (a raster whose name starts with NLCD),
    else the first raster in nlcd/."""
    folder = data_folder() if folder is None else folder
    pack = active_pack(folder)
    for name in sorted(pack_entries(pack, "rasters")):
        if name.upper().startswith("NLCD"):
            p = pack_file(name, pack, "rasters")
            if p:
                return p
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
           "nlcd": "", "dem": "", "dwr": {}, "hifld": [], "problems": [],
           "source": data_source(), "pack_location": pack_location(), "pack_layout": ""}
    # 1.10.0: the pack (its location may be outside the data folder) before the folder checks
    out["pack"] = active_pack(folder) if out["exists"] or pack_location() else ""
    out["pack_sets"] = pack_datasets(out["pack"])
    out["pack_stale"] = stale(out["pack_sets"], now=now)
    out["pack_layout"] = "gpkg" if _gpkg_manifest(out["pack"]) else ("sqlite" if out["pack"]
                                                                      else "")
    if out["source"] == SOURCE_PACK and pack_location() and not os.path.exists(pack_location()):
        out["problems"].append("The pack location does not exist: " + pack_location())
    if not folder:
        out["problems"].append("No suite data folder is set.")
        return out
    if not out["exists"]:
        out["problems"].append("The suite data folder does not exist: " + folder)
        return out
    if not out["pack"] and out["source"] == SOURCE_PACK:
        out["problems"].append("No offline pack found (choose its location, or Update data "
                               "builds one).")
    tdir = sub(folder, TERRAIN)
    tiles = [p for p in _files(tdir, (".tif",))]
    out["terrain_tiles"] = len(tiles)
    out["terrain_mb"] = round(_size(tdir) / 1e6, 1) if os.path.isdir(tdir) else 0.0
    out["nlcd"] = nlcd_raster(folder)
    out["dem"] = local_dem(folder)
    out["dwr"] = dwr_layers(folder)
    out["hifld"] = [p for p, _c in hifld_layers(folder)]
    return out


def describe(st):
    """Plain lines for the status box."""
    src = st.get("source") or SOURCE_PACK
    lines = ["Data source: {}".format(SOURCE_LABELS.get(src, src)) + (
        " - the offline pack is not read" if src == SOURCE_DOWNLOAD else "")]
    if src == SOURCE_PACK:
        if st.get("pack"):
            lines.append("Offline pack: {} ({} data sets{}{})".format(
                st["pack"], len(st["pack_sets"]),
                ", one GeoPackage per layer" if st.get("pack_layout") == "gpkg" else "",
                ", {} to refresh".format(len(st["pack_stale"])) if st["pack_stale"]
                else ", all current"))
        else:
            lines.append("Offline pack: none found" + (" at " + st["pack_location"]
                                                       if st.get("pack_location") else ""))
    if not st.get("folder"):
        return lines + ["No suite data folder is set. Choose one: the three plugins keep their "
                        "terrain cache and your DEM there."]
    lines.append("Suite data folder: " + st["folder"])
    if not st.get("exists"):
        return lines + ["  (does not exist)"]
    lines.append("Terrain cache: {} tiles, {} MB".format(st["terrain_tiles"], st["terrain_mb"]))
    lines.append("Your DEM: " + (st["dem"] or "none (dem/ folder)"))
    lines.append("Land cover: " + (st["nlcd"] or "none (nlcd/ folder; Update data can fetch it)"))
    d = st.get("dwr") or {}
    lines.append("DWR dams: " + (d.get("dwr_dams") or "none (in the pack, or the DWR export in dwr/)"))
    lines.append("DWR dam lines: " + (d.get("dwr_dam_lines") or "none (in the pack, or dwr/, a file named *line*)"))
    lines.append("DWR unknown structures: " + (d.get("dwr_unknown") or "none (dwr/, a file named *unknown*)"))
    lines.append("DWR dugouts: " + (d.get("dwr_dugouts") or "none (dwr/, a file named *dugout*)") +
                 (" + polygons" if d.get("dwr_dugout_polygons") else ""))
    lines.append("HIFLD: {} file(s)".format(len(st.get("hifld") or [])))
    return lines
