"""Preflight check (0.13.0): the things that make a run fail halfway, checked in a few seconds
before "Run all" - and the same list as a Processing tool (17) for headless runs.

Pure Python. Everything that needs QGIS or the network goes through an `Env` the caller gives:
    env.layer_info(ref)   -> {"fields": [...], "count": n, "crs": "EPSG:...", "geometry": "point"|...}
                             or None when the layer cannot be opened (ref: a path, 'path|layername=',
                             or a QGIS layer id)
    env.values(ref, field) -> the distinct values of a field (for the dam-line join), or None
    env.url_ok(url)       -> (True, detail) / (False, why)
    env.plugin_version(key) -> the installed version of a suite plugin, or None
    env.disk_free(path)   -> free bytes, or None
    env.cpu_count()       -> int
The default Env reads GeoPackages with sqlite3 and the network with urllib; the popup and the
tool give one backed by QGIS.

A check is a dict: status ("ok" / "warn" / "fail" / "skip"), title, detail, fix. `summary` turns
the list into one line; the run should not start on a "fail".
"""
import os
import shutil
import sqlite3
import tempfile
import time

from . import workflow as wf
from . import inventory as inv
from . import settings_groups as SG
from ..damsuite import netproxy       # QGIS's proxy setting applies (0.28.2)


def _q(name):
    """A GeoPackage table or column name as a quoted SQL identifier ("" doubles a quote), so a
    layer whose name holds a quote is read instead of breaking the query. 0.28.1."""
    return '"' + str(name).replace('"', '""') + '"'

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
THREEDEP_URL = ("https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer?f=json")
MB = 1024.0 ** 2
GB = 1024.0 ** 3
# a rough need per dam for terrain clips, simulations and reports (screening cell sizes)
PER_DAM_MB = 40.0
MIN_FREE_GB = 2.0
DWR_EXPECTED = ("structure_", "dam_name", "max_dam_he", "normal_vol", "max_pool_v", "hazard_cla", "status")
# 0.26.0: the NSI names (occtype / bldgtype) beside the ND package names, so an NSI-only structures
# layer does not warn; each inner tuple is one group of alternatives (any one of them will do)
STRUCT_FIELDS = (("occ_cls", "OCC_CLS", "occtype", "OCCTYPE"), ("prim_occ", "PRIM_OCC", "bldgtype", "BLDGTYPE"))
LISFLOOD_ENGINES = (0, 1)


def check(status, title, detail="", fix=""):
    return {"status": status, "title": title, "detail": detail, "fix": fix}


# ---------------------------------------------------------------- default env --
class Env(object):
    """Pure-Python environment: GeoPackage files, urllib, shutil."""

    timeout = 6.0

    def layer_info(self, ref):
        path, _, opts = str(ref or "").partition("|")
        if not path or not os.path.isfile(path):
            return None
        lname = opts.split("layername=", 1)[1].split("|")[0] if "layername=" in opts else None
        try:
            con = sqlite3.connect(path)
            try:
                if lname is None:
                    row = con.execute("SELECT table_name FROM gpkg_contents WHERE data_type='features'").fetchone()
                    if row is None:
                        return None
                    lname = row[0]
                g = con.execute("SELECT column_name, geometry_type_name, srs_id FROM gpkg_geometry_columns "
                                "WHERE table_name=?", (lname,)).fetchone()
                gcol, gtype, srs = (g[0], g[1], g[2]) if g else (None, "", None)
                cols = [r[1] for r in con.execute('PRAGMA table_info({})'.format(_q(lname)))]
                n = con.execute('SELECT COUNT(*) FROM {}'.format(_q(lname))).fetchone()[0]
                crs = ""
                if srs is not None:
                    r = con.execute("SELECT organization, organization_coordsys_id FROM gpkg_spatial_ref_sys "
                                    "WHERE srs_id=?", (srs,)).fetchone()
                    if r:
                        crs = "{}:{}".format(r[0], r[1])
            finally:
                con.close()
        except sqlite3.Error:
            return None
        kind = {"POINT": "point", "MULTIPOINT": "point", "LINESTRING": "line", "MULTILINESTRING": "line",
                "POLYGON": "polygon", "MULTIPOLYGON": "polygon"}.get((gtype or "").upper(), "")
        return {"fields": [c for c in cols if c not in ("fid", gcol)], "count": int(n), "crs": crs,
                "geometry": kind, "name": lname}

    def values(self, ref, field):
        path, _, opts = str(ref or "").partition("|")
        if not path or not os.path.isfile(path):
            return None
        lname = opts.split("layername=", 1)[1].split("|")[0] if "layername=" in opts else None
        try:
            con = sqlite3.connect(path)
            try:
                if lname is None:
                    lname = con.execute("SELECT table_name FROM gpkg_contents WHERE data_type='features'").fetchone()[0]
                rows = con.execute('SELECT DISTINCT {} FROM {}'.format(_q(field), _q(lname))).fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            return None
        return {str(r[0]).strip() for r in rows if r[0] is not None}

    def rows(self, ref, fields):
        """[(value, ...)] of the given fields of a GeoPackage layer (missing fields read as None)."""
        path, _, opts = str(ref or "").partition("|")
        if not path or not os.path.isfile(path):
            return None
        lname = opts.split("layername=", 1)[1].split("|")[0] if "layername=" in opts else None
        try:
            con = sqlite3.connect(path)
            try:
                if lname is None:
                    lname = con.execute("SELECT table_name FROM gpkg_contents WHERE data_type='features'").fetchone()[0]
                have = {r[1] for r in con.execute('PRAGMA table_info({})'.format(_q(lname)))}
                sel = ", ".join(_q(f) if f in have else "NULL" for f in fields)
                return [tuple(r) for r in con.execute('SELECT {} FROM {} ORDER BY fid'.format(sel, _q(lname)))]
            finally:
                con.close()
        except sqlite3.Error:
            return None

    def url_ok(self, url):
        import urllib.request
        import urllib.error
        t0 = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "nd-dam-screening-preflight"})
            with netproxy.urlopen(req, timeout=self.timeout) as r:
                code = getattr(r, "status", 200)
            return (code < 400), "answered in {:.1f} s (HTTP {})".format(time.time() - t0, code)
        except urllib.error.HTTPError as e:
            return False, "HTTP {}".format(e.code)
        except Exception as e:                                   # noqa - URLError, socket timeout
            return False, "{}".format(getattr(e, "reason", None) or e)

    def plugin_version(self, key):
        try:
            from ..damsuite import versions as vs
            return vs.installed(key)
        except Exception:                                        # noqa
            return None

    def disk_free(self, path):
        try:
            return shutil.disk_usage(path).free
        except OSError:
            return None

    def cpu_count(self):
        return os.cpu_count() or 1


# ------------------------------------------------------------------- checks --
def _writable(folder):
    try:
        os.makedirs(folder, exist_ok=True)
        fd, p = tempfile.mkstemp(prefix="_preflight", dir=folder)
        os.close(fd)
        os.remove(p)
        return True, ""
    except OSError as e:
        return False, str(e)


def only_ids_count(S):
    """How many dams settings['only_ids'] names (comma separated, as tool
    1's ONLY_IDS reads them; 0 = no filter)."""
    raw = S.get("only_ids") if isinstance(S, dict) else None
    if isinstance(raw, (list, tuple)):
        raw = ",".join(str(x) for x in raw)
    ids = {x.strip().upper() for x in str(raw or "").split(",") if x.strip()}
    return len(ids)


def _slot(S, work, slot):
    try:
        return wf.slot_source(slot, work, S) or ""
    except Exception:                                            # noqa
        return (S.get(slot) or "").strip()


def pending_suite_clip(ref, work):
    """0.28.0: True when `ref` is a <work>/0_suite_data/<slot>.gpkg clip that the DATA STEP will
    write when the run starts, and it is not there yet.

    The four suite-data clips (DWR dams, dam lines, water bodies, structures with the offline
    pack or Download) are cut out of the pack - or downloaded - by the first step that needs
    them, as the run starts. The preflight asks for them before that, so every new work folder
    got four "could not open" warnings for files that were never meant to exist yet. A file that
    is about to be written is not a problem; a file that is there and cannot be read still is."""
    path = str(ref or "").partition("|")[0]
    if not path or not work:
        return False
    try:
        inside = os.path.dirname(os.path.abspath(path)) == os.path.abspath(
            os.path.join(work, wf.SUITE_DIR))
    except (OSError, ValueError):
        return False
    return bool(inside) and not os.path.isfile(path)


def run_checks(work, settings, steps=None, env=None, n_dams=None):
    """The preflight list for a work folder, the window's settings and the steps that would run
    (default: the chain plan). Never raises; a check that cannot be made says so."""
    env = env or Env()
    S = dict(wf.DEFAULT_SETTINGS)
    S.update(wf.migrate_settings(settings))
    out = []
    if steps is None:
        try:
            include = tuple(k for k in wf.CHAIN_OPTIONAL if k in (S.get("chain") or []))
            steps = wf.chain_plan(work, S, include) if work and os.path.isdir(work) else list(wf.KEYS)
        except Exception:                                        # noqa
            steps = list(wf.KEYS)
    steps = list(steps or [])
    plan = ", ".join(SG.plain(k, wf.TITLE.get(k)) for k in steps) or "nothing"

    # -- work folder
    if not work:
        out.append(check(FAIL, "Work folder", "none chosen", "Choose or create a work folder."))
    else:
        ok, why = _writable(work)
        out.append(check(OK if ok else FAIL, "Work folder", work if ok else "{}: {}".format(work, why),
                         "" if ok else "Choose a folder you can write to."))
        free = env.disk_free(work) if ok else None
        if free is not None:
            need_dams = n_dams
            if need_dams is None:
                info = env.layer_info(_slot(S, work, "dwr")) if "1" in steps else None
                need_dams = info["count"] if info else None
                only = only_ids_count(S)
                if only and "1" in steps:
                    # 0.33.1: a one-dam (only_ids / DAM_ID) run needs room for those dams, not the layer
                    need_dams = min(only, need_dams) if need_dams else only
            need_gb = (need_dams or 0) * PER_DAM_MB / 1024.0
            detail = "{:.1f} GB free".format(free / GB)
            if need_dams:
                detail += "; roughly {:.1f} GB for {} dams at screening cell sizes".format(need_gb, need_dams)
            status = OK
            if free / GB < MIN_FREE_GB or (need_dams and free / GB < need_gb):
                status = WARN
            out.append(check(status, "Disk space", detail, "" if status == OK else
                             "Free space on this drive or use a smaller cell size / corridor."))
    out.append(check(OK, "Steps that would run", plan, ""))

    # -- terrain (0.28.0: the step 7 larger-clip source is resolved, not read raw - the window's
    # default is "follow the terrain source")
    clips = wf.larger_clips_index(S)
    needs_terrain = any(k in steps for k in ("2", "16")) or ("7" in steps and clips > 0)
    if needs_terrain:
        local = int(S.get("terrain_source") or 0) == 1 or ("7" in steps and clips == 2
                                                           and not any(k in steps for k in ("2", "16")))
        if local:
            folder = (S.get("local_dem") or "").strip()
            tiles = []
            if folder and os.path.isdir(folder):
                try:
                    from . import terrain
                    tiles = terrain.list_local_tiles(folder)
                except Exception:                                # noqa
                    tiles = [f for f in os.listdir(folder) if f.lower().endswith((".tif", ".tiff", ".img", ".vrt"))]
            if tiles:
                out.append(check(OK, "Local LiDAR DEM", "{} tile(s) in {}".format(len(tiles), folder)))
            else:
                out.append(check(FAIL, "Local LiDAR DEM", "no DEM tiles in {}".format(folder or "(no folder)"),
                                 "Point 'Local DEM folder' at the folder of .tif / .img tiles, or use 3DEP."))
            # 0.28.0: the cached cell follows the SOURCE, and 10 m is the 3DEP source. Your own
            # tiles are usually 1 m, and clipping them at 10 m throws that terrain away, so the
            # one setting that cannot be guessed from here is named before the run.
            if "2" in steps and float(S.get("dem_cell_ft") or 0) > 10.0:
                out.append(check(WARN, "Cached DEM cell size",
                                 "{:.1f} ft ({:.1f} m) from a local DEM folder".format(
                                     float(S["dem_cell_ft"]), float(S["dem_cell_ft"]) * 0.3048),
                                 "Your tiles are probably finer: set 'DEM cell size (ft)' to their "
                                 "cell (3.3 ft for 1 m LiDAR). Step 2 cannot read it back off the "
                                 "tiles, so it uses what the box says."))
        else:
            ok, detail = env.url_ok(THREEDEP_URL)
            out.append(check(OK if ok else FAIL, "USGS 3DEP service", detail,
                             "" if ok else "No internet to the USGS service: connect, or switch the terrain "
                             "source to a local DEM folder."))
    else:
        out.append(check(SKIP, "Terrain source", "no step that fetches terrain is planned"))

    # -- engine
    if any(k in steps for k in ("7", "9")):
        eng = int(S.get("engine") or 0)
        if eng in LISFLOOD_ENGINES:
            exe = (S.get("exe") or "").strip()
            if exe and os.path.isfile(exe):
                out.append(check(OK, "LISFLOOD-FP", exe))
            else:
                out.append(check(FAIL, "LISFLOOD-FP", "executable not found: {}".format(exe or "(none)"),
                                 "Give the LISFLOOD-FP executable, or choose the built-in engine."))
        else:
            out.append(check(OK, "Flood engine", "built-in engine (no install needed)"))
        workers = int(S.get("workers") or 0)
        cpus = env.cpu_count()
        if workers > cpus:
            out.append(check(WARN, "Dams at the same time", "{} asked, {} CPU(s) here".format(workers, cpus),
                             "Lower it to the CPU count or less."))
    # -- 0.28.0: the truncation this release is about. A flood that reaches the edge of the step 2
    # clip and cannot grow stops there, and the result reads like a finished answer.
    if "7" in steps:
        from . import incomplete as ic
        from . import runplan as rpl
        why = ic.settings_reason(S, clips)
        out.append(check(WARN if why else OK, "Flood cut off at the clip edge",
                         why or wf.larger_clips_text(S).split(": ", 1)[-1],
                         "Set 'Step 7 terrain for larger clips' to the terrain source (or leave it "
                         "following it): a flood that reaches the clip edge then loads a larger clip "
                         "instead of stopping." if why else ""))
        out.append(check(OK, "Flood grid cell size", rpl.cell_label(S)))

    # -- layers
    def layer(slot, label, needed, geometry=None, fields=(), any_of=(), empty_ok=False):
        ref = _slot(S, work, slot)
        if not needed:
            return None
        if not ref:
            out.append(check(FAIL, label, "no layer chosen (and none in the data package)",
                             "Pick it on the Inputs tab, or build the data package (step 0)."))
            return None
        info = env.layer_info(ref)
        if info is None:
            if pending_suite_clip(ref, work):                    # 0.28.0: not written yet, by design
                out.append(check(OK, label, "written by the data step when the run starts ({})".format(
                    os.path.basename(str(ref).partition("|")[0]))))
                return None
            out.append(check(WARN, label, "{} - the preflight could not open it here".format(ref),
                             "Check the layer opens in QGIS; a project layer is fine, the check reads files."))
            return None
        if empty_ok and info["count"] == 0:
            # 0.18.0: an empty layer is how you say "no data of this kind is available" (a
            # structure inventory behind a blocked service): said, not refused
            out.append(check(WARN, label, "0 feature(s), {}; empty on purpose?".format(
                info.get("crs") or "CRS unknown"), "Nothing of this kind will be counted in the "
                "impacts, so the class is a minimum. Fine when no data are available; add the "
                "layer when they are."))
            return info
        problems = []
        if geometry and info.get("geometry") and info["geometry"] != geometry:
            problems.append("geometry is {} (needs {})".format(info["geometry"], geometry))
        missing = [f for f in fields if f not in info["fields"]]
        if missing:
            problems.append("fields not found: {}".format(", ".join(missing)))
        if any_of and not any(any(a in info["fields"] for a in alts) for alts in any_of):
            problems.append("none of the fields {} found".format(" / ".join("|".join(a) for a in any_of)))
        if not info.get("crs"):
            problems.append("no CRS recorded")
        detail = "{} feature(s), {}".format(info["count"], info.get("crs") or "CRS unknown")
        if info["count"] == 0:
            problems.append("empty layer")
        if problems:
            out.append(check(WARN if info["count"] else FAIL, label, detail + "; " + "; ".join(problems),
                             "Pick the field names in the tool's dialog if they differ, or fix the layer."))
        else:
            out.append(check(OK, label, detail))
        return info

    dwr = layer("dwr", "ND DWR dams", "1" in steps, "point", fields=(), any_of=(("structure_", "structure_id"),))
    dwr_kind = inv.KIND_DAM
    if dwr is not None:
        # 0.19.0: the DWR GeoPackage / service names and the unknown-structure and dugout exports
        # are read by tool 1 under their own names; warn only for a role no export name covers
        dwr_kind = inv.detect_kind(dwr["fields"], os.path.basename(str(_slot(S, work, "dwr") or "")))
        fld, _n = inv.resolve_fields({}, dwr["fields"], dwr_kind)
        missing = [r for r in ("ID", "NAME", "H", "MS", "STATUS") if not fld.get(r)
                   and not (dwr_kind == inv.KIND_DUGOUT and r == "H")]
        if dwr_kind != inv.KIND_DAM:
            out.append(check(OK, "DWR layer kind", "{}: IDs {}<number>, status filter 'auto' = {}".format(
                dwr_kind, inv.ID_PREFIX[dwr_kind], inv.DEFAULT_KEEP[dwr_kind])))
        if missing:
            out.append(check(WARN, "DWR export fields", "no field found for: {}".format(", ".join(missing)),
                             "Tool 1's field pickers can take other names."))
    wb = layer("waterbodies", "Water bodies", any(k in steps for k in ("3", "16")), "polygon")
    lines = layer("dam_lines", "DWR dam lines", "4" in steps, "line", any_of=(("structure_id", "structure_"),))
    if dwr is not None and lines is not None and dwr_kind != inv.KIND_DAM:
        out.append(check(OK, "Dams without a dam line", "{}s are not joined to DWR dam lines by number (a "
                         "separate number series); tool 4 uses a line within its match distance, else the "
                         "reservoir direction".format(dwr_kind)))
    elif dwr is not None and lines is not None:
        ids = env.values(_slot(S, work, "dwr"), "structure_" if "structure_" in dwr["fields"] else "structure_id")
        lids = env.values(_slot(S, work, "dam_lines"), "structure_id" if "structure_id" in lines["fields"] else "structure_")
        if ids is not None and lids is not None:
            # 0.18.0: only the dams this run is about (ONLY_IDS: DWR structure IDs, "DWR-<id>")
            only = {x.strip().upper() for x in str(S.get("only_ids") or "").split(",") if x.strip()}
            if only:
                ids = {i for i in ids if str(i).strip().upper() in only
                       or "DWR-" + str(i).strip().upper() in only}
            without = len(ids - lids) if ids else 0
            if without:
                out.append(check(WARN, "Dams without a dam line", "{} of {} dams have no DWR dam line".format(
                    without, len(ids)), "Their breach direction falls back to 'away from the reservoir centroid' "
                    "(dir_conf = low): check them on the Review tab."))
            else:
                out.append(check(OK, "Dams without a dam line", "every dam has a dam line"))
    layer("structures", "Structures", any(k in steps for k in ("6", "8")), None, any_of=STRUCT_FIELDS,
          empty_ok=True)
    if "6" in steps or "8" in steps:
        for slot, label in (("roads", "Roads"), ("rail", "Railroads"), ("lifelines", "Lifeline facilities")):
            ref = _slot(S, work, slot)
            if not ref:
                out.append(check(WARN, label, "no layer: impacts on {} will not be counted".format(label.lower()),
                                 "Optional; pick it on the Inputs tab if you have it."))
    # -- hazard criterion / charts
    if "8" in steps or "9" in steps:
        crit = int(S.get("crit") or 0)
        chart = (S.get("chart_file") or "").strip()
        if crit == 0:
            try:
                from . import hazard as hz
                hz.load_ready_charts(chart or None)
                out.append(check(OK, "ACER TM 11 charts", chart or "the plugin's chart file"))
            except Exception as e:                               # noqa
                out.append(check(FAIL, "ACER TM 11 charts", str(e)[:200],
                                 "Sign the charts off in ND Dam Hazard (Dam suite > Chart sign-off, one engineer is "
                                 "enough), or choose another criterion."))
        else:
            out.append(check(OK, "Hazard criterion", "no chart file needed"))
    # -- 0.28.0: the suite's own set-up (damsuite.suitecheck), named here rather than only on the
    # suite home. WARN, not FAIL: a run whose layers are all chosen by hand needs none of it.
    try:
        from ..damsuite import suitecheck as SC
        items = SC.check()
        stop = SC.blocking(items)
        if stop:
            out.append(check(WARN, "Suite set-up", "; ".join(
                "{}: {}".format(i.get("label") or i.get("key"), i.get("text") or "") for i in stop),
                "; ".join(i["fix"] for i in stop if i.get("fix"))))
        else:
            out.append(check(OK, "Suite set-up", SC.summary_text(items)))
    except Exception as e:                                       # noqa - never stops the check list
        out.append(check(WARN, "Suite set-up", "could not be checked: {}".format(e)))
    # -- other plugins
    if "7b" in steps:
        v = env.plugin_version("ndbreach")
        out.append(check(OK if v else FAIL, "ND Dam Breach Runner", "version {}".format(v) if v else "not installed",
                         "" if v else "Install the Runner, or leave the detailed runs out."))
    # -- settings file
    try:
        from ..damsuite import settings as DS
        p = (S.get("settings_file") or "").strip() or DS.current_path()
        if p:
            s = DS.load_current(p)
            if s is None:
                out.append(check(WARN, "Settings file", DS.load_current.problem or p,
                                 "Fix or re-choose the file; the tools use their own defaults meanwhile."))
            else:
                n = len([k for k in s.values if k[0] == SG.PLUGIN and k[1] != DS.WINDOW])
                out.append(check(OK, "Settings file", "{} ({} value(s) set for this plugin)".format(p, n)))
        else:
            out.append(check(OK, "Settings file", "none: the tools' own starting values"))
    except Exception as e:                                       # noqa
        out.append(check(WARN, "Settings file", "could not be checked: {}".format(e)))
    return out


def summary(checks):
    """One line: 'ready', 'ready, N warning(s)' or 'not ready: <first failure>'."""
    fails = [c for c in checks if c["status"] == FAIL]
    warns = [c for c in checks if c["status"] == WARN]
    if fails:
        return "not ready: {} - {}".format(fails[0]["title"], fails[0]["detail"]) + (
            " (+{} more)".format(len(fails) - 1) if len(fails) > 1 else "")
    if warns:
        return "ready, {} warning(s)".format(len(warns))
    return "ready"


def ready(checks):
    return not any(c["status"] == FAIL for c in checks)


def blocking_reasons(checks=None, suite_items=None, work=None, busy=False):
    """0.28.0: why a Run button is grey, named - the text the button's tooltip and the status
    line both show.

    Three sources, in the order a user meets them: the suite is not set up
    (damsuite.suitecheck.blocking - the folder, the pack, the charts), the work folder is not
    chosen, and the preflight found a failure. A grey button with no reason is the complaint this
    answers, so an empty list means the button should be enabled."""
    out = []
    if busy:
        out.append("a run is going on: Stop it first")
    if work is not None and not str(work or "").strip():
        out.append("no work folder chosen")
    elif work is not None and not os.path.isdir(str(work)):
        out.append("the work folder does not exist: {}".format(work))
    items = suite_items
    if items is None:
        try:
            from ..damsuite import suitecheck as SC
            items = SC.check()
        except Exception:                                        # noqa - a check is never a crash
            items = []
    try:
        from ..damsuite import suitecheck as SC
        for i in SC.blocking(items):
            out.append("{}: {}{}".format(i.get("label") or i.get("key"), i.get("text") or "",
                                         " - " + i["fix"] if i.get("fix") else ""))
    except Exception:                                            # noqa
        pass
    for c in checks or []:
        if c.get("status") == FAIL:
            out.append("{}: {}{}".format(c.get("title"), c.get("detail") or "",
                                         " - " + c["fix"] if c.get("fix") else ""))
    seen, uniq = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def blocking_text(reasons, lead="Cannot run: "):
    """The reasons as one line ("" when nothing blocks the run)."""
    return (lead + "; ".join(reasons)) if reasons else ""


MARK = {OK: "OK  ", WARN: "WARN", FAIL: "FAIL", SKIP: "--  "}


def report_lines(checks):
    lines = ["Preflight: " + summary(checks), ""]
    for c in checks:
        lines.append("{} {}: {}".format(MARK.get(c["status"], c["status"]), c["title"], c["detail"]))
        if c.get("fix") and c["status"] in (WARN, FAIL):
            lines.append("       -> " + c["fix"])
    return lines
