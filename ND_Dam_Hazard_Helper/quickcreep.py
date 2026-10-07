# -*- coding: utf-8 -*-
"""v2.10: statewide quick creep screen - a NEW structures layer against every dam's flood,
without re-running any model or classification.

When a new structure inventory comes out (a new FEMA USA Structures or NSI release, a
county's new building layer), which dams have new buildings in their flood? The creep
watch (watch.py) answers it by re-classifying every dam - hours for a state. This screen
answers it in minutes: for each dam folder of a parent folder (watch.dam_folders) it

  1. finds the depth raster the dam's LAST classification used: outputs/QA/Run_Settings.json
     (the governing breach scenario, else the first), else QA/Last_Run_Snapshot.json
     ("rasters"), else the raster search of the dam folder (autodetect.find_rasters - said
     so in the row: it may not be the raster that was classified);
  2. reads the new layer's features in the raster's extent only (a filter rectangle in the
     layer's CRS, so the data provider's spatial index does the work; reprojection with
     QGIS transforms) and samples the depth: a POINT reads the cell it lies in; a POLYGON
     the largest value of the cells whose centre is inside it (the cell under its
     pointOnSurface when no centre is). Polygons over an all-dry part of the raster are
     skipped with a block-maximum index built in one strip-wise numpy pass. A structure is
     wet when that depth is above wet_ft (default 0: any depth, as the classification
     treats a structure as inundated);
  3. compares the wet structures with the structures of the dam's last run
     (Classification/Habitable_Structures[_<governing>].gpkg - all structures the run kept,
     dry ones too when "keep dry" was on - else the snapshot's flooded structures): same
     structure = within match_ft (default 30 ft, as history.MATCH_FT) or the new point
     inside a last-run outline / a last-run point inside the new outline;
  4. per dam: NEW structures in the flood (wet, not in the last run), their largest depth,
     structures GONE (with a previous layer: wet in the previous release and not in the new
     one; without it: wet in the last run and no structure of the new layer there -
     outline-sourced ones only when the new layer is polygons), and the flag
     "RE-RUN recommended" (at least min_new new structures), "CHECK" (only gone ones),
     "NOT CLASSIFIED" (no last run to compare with) or "-".

This is a SCREEN, not a classification: it does not decide habitability, occupancy, ACER
zones or consequences, and the classification reads a building over its outline plus a
5-ft ring, so a structure at the flood edge can differ. It tells which dams to re-run
(batch classification or the creep watch). Outputs in the parent folder:
Quick_Creep.csv / .html (one row per dam) and Quick_Creep_Structures.csv (every new /
gone structure with its location). Nothing here has been run in a real QGIS.
"""

import csv
import html
import math
import os
import time

import numpy as np

from .constants import OUTPUT_FOLDER_NAME, SUBDIR_QA, SUBDIR_CLASSIFICATION, PLUGIN_NAME, \
    PLUGIN_VERSION

CSV_FILE = "Quick_Creep.csv"
HTML_FILE = "Quick_Creep.html"
STRUCT_CSV = "Quick_Creep_Structures.csv"
DEFAULTS = {"wet_ft": 0.0, "match_ft": 30.0, "min_new": 1}
FLAG_RERUN = "RE-RUN recommended"
FLAG_CHECK = "CHECK"
FLAG_NOT_CLASSIFIED = "NOT CLASSIFIED"
FLAG_NONE = "-"
FLAG_FAILED = "FAILED"
_FLAG_ORDER = [FLAG_FAILED, FLAG_RERUN, FLAG_NOT_CLASSIFIED, FLAG_CHECK, FLAG_NONE, ""]
FIELDS = ["Flag", "Dam", "Status", "New_In_Flood", "Max_Depth_New_ft", "Gone_From_Flood",
          "New_Layer_Wet", "New_Layer_In_Extent", "Previous_Layer_New", "Last_Run_Structures",
          "Last_Run", "Depth_Raster", "Raster_Source", "Structures_Source", "Seconds",
          "Message", "Folder"]
STRUCT_FIELDS = ["Dam", "Change", "Feature_ID", "Depth_ft", "X", "Y", "CRS",
                 "In_Previous_Layer", "Detail"]
STRUCTURES_BASE = "Habitable_Structures"
_OUTLINE_FAMILIES = ("FEMA_STRUCTURES", "USA_STRUCTURES", "OUTLINES", "OSM")
BLOCK = 64                      # cells per side of the dry / wet block index
_DRY_OUTCOME = ("", None, "Not inundated", "NULL")


def _num(v, default=None):
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _params(params):
    p = dict(DEFAULTS)
    p.update({k: v for k, v in (params or {}).items() if k in DEFAULTS and v is not None})
    return p


# ======================================================== the last run ==
def _slug(name):
    import re
    return re.sub(r"[^A-Za-z0-9]+", "_", str(name or "")).strip("_")[:40] or "Scenario"


def last_run_info(dam_folder):
    """{"depth", "raster_source", "governing", "time", "snapshot"} of the dam's last
    classification (depth None when no raster is found)."""
    from . import history as HIS
    qa = os.path.join(dam_folder, OUTPUT_FOLDER_NAME, SUBDIR_QA)
    snap = HIS.read_snapshot(qa)
    gov = (snap or {}).get("governing") or ""
    info = {"depth": None, "raster_source": "", "governing": gov,
            "time": (snap or {}).get("time") or "", "snapshot": snap}
    try:
        from . import reproduce as REP
        rec = REP.read_run_settings(os.path.join(qa, REP.RUN_SETTINGS_FILE))
    except Exception:                       # noqa: BLE001 - missing / older record
        rec = None
    if rec is not None:
        scen = [s for s in rec.get("scenarios") or [] if (s.get("role") or "breach") == "breach"]
        pick = next((s for s in scen if gov and s.get("name") == gov), None) or \
            (scen[0] if scen else None)
        if pick and pick.get("depth") and os.path.isfile(pick["depth"]):
            info.update({"depth": pick["depth"], "raster_source": "QA/Run_Settings.json "
                         "(scenario '{}')".format(pick.get("name") or "")})
            info["governing"] = info["governing"] or pick.get("name") or ""
            info["time"] = info["time"] or str(rec.get("created") or "")
            return info
    rasters = (snap or {}).get("rasters") or {}
    for name in ([gov] if gov else []) + sorted(rasters):
        p = rasters.get(name)
        if p and os.path.isfile(p):
            info.update({"depth": p, "raster_source": "QA/Last_Run_Snapshot.json (scenario "
                                                      "'{}')".format(name)})
            return info
    from .autodetect import find_rasters
    try:
        p = find_rasters(dam_folder).get("depth")
    except Exception:                       # noqa: BLE001
        p = None
    if p:
        info.update({"depth": p, "raster_source": "raster search of the dam folder (no "
                     "record of the last run's rasters - check it is the classified one)"})
    return info


def last_run_structures(dam_folder, governing, raster_crs, ctx):
    """(records, source text) of the structures of the dam's last run, in the raster CRS:
    [{"id", "x", "y", "geom" (QgsGeometry or None), "wet", "depth", "outline"}]. None
    records when the run left no structure table / snapshot."""
    from qgis.core import QgsVectorLayer, QgsCoordinateReferenceSystem
    from .helpers import transform_geometry
    folder = os.path.join(dam_folder, OUTPUT_FOLDER_NAME, SUBDIR_CLASSIFICATION)
    names = ["{}.gpkg".format(STRUCTURES_BASE)]
    if governing:
        names.insert(0, "{}_{}.gpkg".format(STRUCTURES_BASE, _slug(governing)))
    for n in names:
        path = os.path.join(folder, n)
        if not os.path.isfile(path):
            continue
        lyr = QgsVectorLayer(path, n, "ogr")
        if not lyr.isValid():
            continue
        xf = _xform(lyr.crs(), raster_crs, ctx)
        idx = {f.name(): i for i, f in enumerate(lyr.fields())}
        out = []
        for feat in lyr.getFeatures():
            g = feat.geometry()
            if g is None or g.isNull() or g.isEmpty():
                continue
            g = transform_geometry(g, xf) if xf is not None else g
            if g is None:
                continue
            pt = _point_of(g)
            if pt is None:
                continue
            at = feat.attributes()

            def get(name, at=at):
                i = idx.get(name)
                return at[i] if i is not None and i < len(at) else None
            outcome = get("ND_Outcome")
            depth = _num(get("Max_Depth_ft"))
            srcs = str(get("Sources") or "").upper()
            out.append({"id": get("Struct_ID"), "x": pt[0], "y": pt[1], "geom": g,
                        "wet": str(outcome) not in _DRY_OUTCOME and outcome is not None,
                        "depth": depth,
                        "outline": any(f in srcs for f in _OUTLINE_FAMILIES)})
        return out, "{} ({} structures)".format(os.path.join(SUBDIR_CLASSIFICATION, n),
                                                len(out))
    from . import history as HIS
    snap = HIS.read_snapshot(os.path.join(dam_folder, OUTPUT_FOLDER_NAME, SUBDIR_QA))
    if snap and snap.get("structures") is not None:
        crs = QgsCoordinateReferenceSystem(snap.get("crs") or "")
        xf = _xform(crs, raster_crs, ctx) if crs.isValid() else None
        out = []
        from qgis.core import QgsPointXY
        for s in snap.get("structures") or []:
            x, y = _num(s.get("x")), _num(s.get("y"))
            if x is None or y is None:
                continue
            if xf is not None:
                try:
                    q = xf.transform(QgsPointXY(x, y))
                    x, y = q.x(), q.y()
                except Exception:           # noqa: BLE001
                    continue
            out.append({"id": s.get("id"), "x": x, "y": y, "geom": None,
                        "wet": s.get("outcome") not in _DRY_OUTCOME, "depth": _num(
                            s.get("depth")), "outline": True})
        return out, "QA/Last_Run_Snapshot.json (flooded / cut-off habitable structures " \
                    "only: {})".format(len(out))
    return None, "no structure table of a last run"


# ========================================================== sampling ==
def _xform(src, dst, ctx):
    from qgis.core import QgsCoordinateTransform
    try:
        if src is None or dst is None or not src.isValid() or not dst.isValid():
            return None
        t = QgsCoordinateTransform(src, dst, ctx)
        return None if t.isShortCircuited() else t
    except Exception:                       # noqa: BLE001
        return None


def _point_of(g):
    try:
        p = g.pointOnSurface().asPoint()
        return float(p.x()), float(p.y())
    except Exception:                       # noqa: BLE001
        try:
            bb = g.boundingBox()
            return (bb.xMinimum() + bb.xMaximum()) / 2.0, (bb.yMinimum() + bb.yMaximum()) / 2.0
        except Exception:                   # noqa: BLE001
            return None


class WetIndex(object):
    """Largest depth per BLOCK x BLOCK cells, from one strip-wise pass: a polygon over
    blocks that are all dry (<= wet_ft or no data) needs no raster read."""

    def __init__(self, sampler, wet_ft=0.0, block=BLOCK, check=None):
        self.s = sampler
        self.block = int(block)
        self.wet_ft = float(wet_ft)
        nby = int(math.ceil(sampler.ysize / float(self.block)))
        nbx = int(math.ceil(sampler.xsize / float(self.block)))
        self.bmax = np.full((max(1, nby), max(1, nbx)), -np.inf)
        for by in range(nby):
            if check:
                check()
            y0 = by * self.block
            h = min(self.block, sampler.ysize - y0)
            arr = sampler.window_values(0, y0, sampler.xsize, h)
            if arr is None:
                self.bmax[by, :] = np.inf           # unreadable: never skip
                continue
            a = np.where(np.isfinite(arr), arr, -np.inf)
            pad = nbx * self.block - a.shape[1]
            if pad:
                a = np.pad(a, ((0, 0), (0, pad)), constant_values=-np.inf)
            self.bmax[by, :] = a.reshape(h, nbx, self.block).max(axis=(0, 2))
        self.any_wet = bool((self.bmax > self.wet_ft).any())

    def maybe_wet(self, bb):
        win = self.s._window_for_bbox(bb.xMinimum(), bb.yMinimum(), bb.xMaximum(),
                                      bb.yMaximum())
        if win is None:
            return False
        c0, r0, w, h = win
        b = self.block
        sub = self.bmax[r0 // b:(r0 + h - 1) // b + 1, c0 // b:(c0 + w - 1) // b + 1]
        return bool((sub > self.wet_ft).any())


def _raster_rect(sampler):
    from qgis.core import QgsRectangle
    gt = sampler.gt
    xs = [gt[0], gt[0] + sampler.xsize * gt[1] + sampler.ysize * gt[2]]
    ys = [gt[3], gt[3] + sampler.xsize * gt[4] + sampler.ysize * gt[5]]
    return QgsRectangle(min(xs), min(ys), max(xs), max(ys))


def sample_layer(layer, sampler, index, ctx, check=None):
    """Features of `layer` inside the raster, with their depth (raster CRS):
    [{"fid", "x", "y", "depth" (None: no value / dry block), "geom", "point"}]."""
    from qgis.core import QgsFeatureRequest, QgsPointXY
    from .compat import GEOM_POLYGON
    from .helpers import transform_geometry
    rect = _raster_rect(sampler)
    to_r = _xform(layer.crs(), sampler.crs, ctx)
    back = _xform(sampler.crs, layer.crs(), ctx)
    req = QgsFeatureRequest()
    try:
        lrect = back.transformBoundingBox(rect) if back is not None else rect
        req.setFilterRect(lrect)
    except Exception:                       # noqa: BLE001 - no pre-filter then
        pass
    polys = layer.geometryType() == GEOM_POLYGON
    out, pts_i = [], []
    for n, feat in enumerate(layer.getFeatures(req)):
        if check and n % 500 == 0:
            check()
        g = feat.geometry()
        if g is None or g.isNull() or g.isEmpty():
            continue
        if to_r is not None:
            g = transform_geometry(g, to_r)
            if g is None:
                continue
        pt = _point_of(g)
        if pt is None or not rect.contains(QgsPointXY(pt[0], pt[1])) and not (
                polys and rect.intersects(g.boundingBox())):
            continue
        rec = {"fid": feat.id(), "x": pt[0], "y": pt[1], "depth": None, "geom": g,
               "point": not polys}
        out.append(rec)
        if not polys:
            pts_i.append(len(out) - 1)
            continue
        if not index.maybe_wet(g.boundingBox()):
            continue
        _xs, _ys, vals = sampler.cells_in_geometry(g)
        if vals.size:
            rec["depth"] = float(np.max(vals))
        else:
            pts_i.append(len(out) - 1)          # smaller than a cell: the cell under it
    if pts_i:
        xs = np.array([out[i]["x"] for i in pts_i])
        ys = np.array([out[i]["y"] for i in pts_i])
        vals = sampler.values_at(xs, ys)
        for i, v in zip(pts_i, vals):
            if np.isfinite(v):
                out[i]["depth"] = float(v)
    return out


# ========================================================== matching ==
class _Grid(object):
    """Points (x, y) and optional geometries, found by distance or containment."""

    def __init__(self, items, cell):
        self.items = items
        self.cell = max(float(cell), 1e-9)
        self.g = {}
        self.boxes = {}
        for i, it in enumerate(items):
            self.g.setdefault(self._k(it["x"], it["y"]), []).append(i)
            geom = it.get("geom")
            if geom is not None and not it.get("point"):
                try:
                    bb = geom.boundingBox()
                except Exception:           # noqa: BLE001
                    continue
                for kx in range(int(math.floor(bb.xMinimum() / self.cell)),
                                int(math.floor(bb.xMaximum() / self.cell)) + 1):
                    for ky in range(int(math.floor(bb.yMinimum() / self.cell)),
                                    int(math.floor(bb.yMaximum() / self.cell)) + 1):
                        self.boxes.setdefault((kx, ky), set()).add(i)

    def _k(self, x, y):
        return int(math.floor(x / self.cell)), int(math.floor(y / self.cell))

    def near(self, x, y, tol):
        kx, ky = self._k(x, y)
        best, bd = None, None
        r = int(math.ceil(tol / self.cell))
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                for i in self.g.get((kx + dx, ky + dy), ()):
                    d = math.hypot(self.items[i]["x"] - x, self.items[i]["y"] - y)
                    if d <= tol and (bd is None or d < bd):
                        best, bd = i, d
        return best

    def containing(self, x, y):
        from qgis.core import QgsGeometry, QgsPointXY
        cands = self.boxes.get(self._k(x, y), ())
        if not cands:
            return None
        p = QgsGeometry.fromPointXY(QgsPointXY(x, y))
        for i in sorted(cands):
            try:
                if self.items[i]["geom"].intersects(p):
                    return i
            except Exception:               # noqa: BLE001
                continue
        return None

    def inside(self, geom):
        """Index of an item whose POINT lies inside `geom` (a polygon), or None."""
        from qgis.core import QgsGeometry, QgsPointXY
        try:
            bb = geom.boundingBox()
        except Exception:                   # noqa: BLE001
            return None
        for kx in range(int(math.floor(bb.xMinimum() / self.cell)),
                        int(math.floor(bb.xMaximum() / self.cell)) + 1):
            for ky in range(int(math.floor(bb.yMinimum() / self.cell)),
                            int(math.floor(bb.yMaximum() / self.cell)) + 1):
                for i in self.g.get((kx, ky), ()):
                    it = self.items[i]
                    try:
                        if geom.intersects(QgsGeometry.fromPointXY(QgsPointXY(it["x"],
                                                                              it["y"]))):
                            return i
                    except Exception:       # noqa: BLE001
                        continue
        return None


def matched(rec, grid, tol):
    """Is `rec` (a sampled structure) one of the grid's structures?"""
    if grid is None or not grid.items:
        return False
    if grid.near(rec["x"], rec["y"], tol) is not None:
        return True
    if grid.containing(rec["x"], rec["y"]) is not None:
        return True
    if not rec.get("point") and rec.get("geom") is not None:
        return grid.inside(rec["geom"]) is not None
    return False


# ============================================================== a dam ==
def screen_dam(dam_folder, new_layer, prev_layer=None, params=None, ctx=None, check=None):
    """(row, structure rows) of one dam. Never raises for the dam's own problems (the row
    says FAILED / skipped); CancelledByUser passes through."""
    from qgis.core import QgsProject, QgsRasterLayer
    from .helpers import RasterSampler, dam_display_name, ground_distance_in_layer_units, \
        CancelledByUser
    p = _params(params)
    ctx = ctx if ctx is not None else QgsProject.instance().transformContext()
    name = dam_display_name(dam_folder)
    row = dict.fromkeys(FIELDS, "")
    row.update({"Dam": name, "Folder": dam_folder, "Flag": FLAG_NONE, "Status": "done"})
    t0 = time.monotonic()
    srows = []
    smp = None
    try:
        info = last_run_info(dam_folder)
        row.update({"Last_Run": info["time"], "Raster_Source": info["raster_source"]})
        if not info["depth"]:
            row.update({"Status": "skipped", "Flag": "", "Message": "no depth raster found "
                        "(not classified and no rasters in the folder)"})
            return row, srows
        row["Depth_Raster"] = os.path.relpath(info["depth"], dam_folder) \
            if os.path.abspath(info["depth"]).startswith(os.path.abspath(dam_folder)) \
            else info["depth"]
        lyr = QgsRasterLayer(info["depth"], "Depth")
        smp = RasterSampler(lyr) if lyr.isValid() else None
        if smp is None or not smp.ok:
            row.update({"Status": "failed", "Flag": FLAG_FAILED,
                        "Message": "depth raster not readable: {}".format(info["depth"])})
            return row, srows
        wet_ft = float(_num(p["wet_ft"], 0.0))
        index = WetIndex(smp, wet_ft, check=check)
        # 2.18.0: latitude-aware (raster centre) for Web Mercator / geographic
        tol = ground_distance_in_layer_units(float(_num(p["match_ft"], 30.0)), smp.crs,
                                             rect=lyr.extent(), transform_ctx=ctx)
        crs_id = smp.crs.authid() if smp.crs is not None else ""
        new = sample_layer(new_layer, smp, index, ctx, check) if index.any_wet else []
        row["New_Layer_In_Extent"] = len(new) if index.any_wet else "0 (raster all dry)"
        wet = [r for r in new if r["depth"] is not None and r["depth"] > wet_ft]
        row["New_Layer_Wet"] = len(wet)
        last, src = last_run_structures(dam_folder, info["governing"], smp.crs, ctx)
        row["Structures_Source"] = src
        prev_grid = None
        prev = None
        if prev_layer is not None:
            prev = sample_layer(prev_layer, smp, index, ctx, check) if index.any_wet else []
            prev_grid = _Grid(prev, max(tol, 1e-6) * 4)
        if prev_grid is not None:
            fresh = [r for r in wet if not matched(r, prev_grid, tol)]
            row["Previous_Layer_New"] = len(fresh)
            fresh_ids = set(id(r) for r in fresh)
        else:
            fresh_ids = None
        if last is None:
            row.update({"Flag": FLAG_NOT_CLASSIFIED, "Status": "not classified",
                        "Last_Run_Structures": "", "New_In_Flood": len(wet),
                        "Max_Depth_New_ft": _fmt_depth(max((r["depth"] for r in wet),
                                                           default=None)),
                        "Message": "no structure table of a last run to compare with - "
                                   "classify this dam ({} structure(s) of the new layer in "
                                   "its flood)".format(len(wet))})
            for r in wet:
                srows.append(_srow(name, "IN FLOOD (no last run)", r, crs_id, fresh_ids))
            return row, srows
        row["Last_Run_Structures"] = len(last)
        grid = _Grid(last, max(tol, 1e-6) * 4)
        new_in = [r for r in wet if not matched(r, grid, tol)]
        row["New_In_Flood"] = len(new_in)
        row["Max_Depth_New_ft"] = _fmt_depth(max((r["depth"] for r in new_in), default=None))
        for r in new_in:
            srows.append(_srow(name, "NEW IN FLOOD", r, crs_id, fresh_ids))
        # gone: with the previous release, wet there and not in the new one; else wet in
        # the last run and nothing of the new layer at that place
        new_grid = _Grid(new, max(tol, 1e-6) * 4)
        gone = []
        if prev is not None:
            for r in prev:
                if r["depth"] is not None and r["depth"] > wet_ft and \
                        not matched(r, new_grid, tol):
                    gone.append(r)
                    srows.append(_srow(name, "GONE (in the previous release, not in the new "
                                             "one)", r, crs_id, None))
        else:
            polys = any(not r.get("point") for r in new)
            for s in last:
                if not s["wet"] or (polys and not s["outline"]):
                    continue
                probe = {"x": s["x"], "y": s["y"], "geom": s["geom"],
                         "point": s["geom"] is None}
                if not matched(probe, new_grid, tol):
                    gone.append(s)
                    srows.append(_srow(name, "GONE (last-run structure, none of the new "
                                             "layer there)", {"fid": s["id"], "x": s["x"],
                                                              "y": s["y"], "depth": s["depth"]},
                                       crs_id, None))
        row["Gone_From_Flood"] = len(gone)
        msg = []
        if new_in:
            msg.append("{} structure(s) of the new layer in the flood that the last run did "
                       "not have".format(len(new_in)))
        if gone:
            msg.append("{} structure(s) gone".format(len(gone)))
        if len(new_in) >= int(_num(p["min_new"], 1) or 1):
            row["Flag"] = FLAG_RERUN
        elif gone:
            row["Flag"] = FLAG_CHECK
        row["Message"] = "; ".join(msg) or "nothing new in the flood"
        return row, srows
    except CancelledByUser:
        raise
    except Exception as e:                  # noqa: BLE001 - one dam never stops the screen
        row.update({"Status": "failed", "Flag": FLAG_FAILED, "Message": str(e)[:250]})
        return row, srows
    finally:
        if smp is not None:
            smp.close()
        row["Seconds"] = "{:.1f}".format(time.monotonic() - t0)


def _fmt_depth(v):
    return "" if v is None else "{:.2f}".format(v)


def _srow(dam, change, r, crs_id, fresh_ids):
    return {"Dam": dam, "Change": change, "Feature_ID": r.get("fid"),
            "Depth_ft": _fmt_depth(r.get("depth")), "X": "{:.2f}".format(r["x"]),
            "Y": "{:.2f}".format(r["y"]), "CRS": crs_id,
            "In_Previous_Layer": "" if fresh_ids is None else
            ("No" if id(r) in fresh_ids else "Yes"), "Detail": ""}


# ============================================================ the screen ==
def run_quick_creep(parent, new_layer, prev_layer=None, params=None, progress=None,
                    log=None):
    """Screen every dam folder of `parent` (watch.dam_folders). Returns {"rows",
    "structures", "csv", "html", "structures_csv", "summary"}."""
    from .watch import dam_folders
    if not parent or not os.path.isdir(parent):
        raise ValueError("Folder not found: {}".format(parent))
    if new_layer is None or not new_layer.isValid():
        raise ValueError("The new structures layer is not valid")
    p = _params(params)
    folders = dam_folders(parent)
    rows, srows = [], []
    n = max(1, len(folders))
    for i, folder in enumerate(folders):
        if progress:
            progress(100.0 * i / n, "Quick creep: {} ({}/{})".format(
                os.path.basename(folder), i + 1, n))
        row, sr = screen_dam(folder, new_layer, prev_layer, p,
                             check=(lambda: progress(100.0 * i / n, "")) if progress else None)
        rows.append(row)
        srows.extend(sr)
        if log:
            log("{}: {} - {}".format(row["Dam"], row["Flag"] or row["Status"], row["Message"]))
    rows = sort_rows(rows)
    paths = write_reports(parent, rows, srows, p, new_layer, prev_layer)
    text = summary_text(rows)
    if progress:
        progress(100.0, "Quick creep: done")
    return {"rows": rows, "structures": srows, "csv": paths["csv"], "html": paths["html"],
            "structures_csv": paths["structures_csv"], "summary": text}


def sort_rows(rows):
    def key(r):
        f = r.get("Flag") or ""
        try:
            k = _FLAG_ORDER.index(f)
        except ValueError:
            k = len(_FLAG_ORDER)
        return (k, -int(_num(r.get("New_In_Flood"), 0) or 0), str(r.get("Dam")))
    return sorted(rows, key=key)


def summary_text(rows):
    c = {}
    for r in rows:
        c[r.get("Flag") or r.get("Status")] = c.get(r.get("Flag") or r.get("Status"), 0) + 1
    n_new = sum(int(_num(r.get("New_In_Flood"), 0) or 0) for r in rows)
    return ("Quick creep screen of {} dam(s): {} re-run recommended, {} to check, {} not "
            "classified, {} failed, {} skipped; {} new structure(s) in floods in all. A "
            "screen - nothing was re-classified.".format(
                len(rows), c.get(FLAG_RERUN, 0), c.get(FLAG_CHECK, 0),
                c.get(FLAG_NOT_CLASSIFIED, 0), c.get(FLAG_FAILED, 0), c.get("skipped", 0),
                n_new))


def _layer_text(lyr):
    if lyr is None:
        return "-"
    try:
        return "{} ({})".format(lyr.name(), lyr.source().split("|")[0])
    except Exception:                       # noqa: BLE001
        return str(lyr)


def write_reports(parent, rows, srows, params, new_layer=None, prev_layer=None):
    out = {}
    path = os.path.join(parent, CSV_FILE)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    out["csv"] = path
    path = os.path.join(parent, STRUCT_CSV)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=STRUCT_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in srows:
            w.writerow(r)
    out["structures_csv"] = path
    out["html"] = os.path.join(parent, HTML_FILE)
    with open(out["html"], "w", encoding="utf-8") as fh:
        fh.write(render_html(rows, parent, params, new_layer, prev_layer))
    return out


_CSS = ("body{font-family:Segoe UI,Arial,sans-serif;margin:16px;color:#222;background:#fff}"
        "table{border-collapse:collapse;font-size:13px}td,th{border:1px solid #ccc;"
        "padding:3px 6px;vertical-align:top}th{background:#eee}.RE-RUN{background:#fdd}"
        ".CHECK{background:#ffd}.FAILED{background:#fcc}.NOT{background:#eef}"
        ".note{max-width:60em;font-size:13px}")


def render_html(rows, parent, params, new_layer=None, prev_layer=None):
    e = html.escape
    p = _params(params)
    cols = ["Flag", "Dam", "Status", "New_In_Flood", "Max_Depth_New_ft", "Gone_From_Flood",
            "New_Layer_Wet", "Previous_Layer_New", "Last_Run_Structures", "Last_Run",
            "Raster_Source", "Message"]
    out = ["<!DOCTYPE html><html><head><meta charset='utf-8'><title>Quick creep screen"
           "</title><style>{}</style></head><body>".format(_CSS),
           "<h1>Quick creep screen</h1>",
           "<p class='note'>{} - {} v{} - {} dam(s) in {}</p>".format(
               e(time.strftime("%Y-%m-%d %H:%M")), e(PLUGIN_NAME), e(PLUGIN_VERSION),
               len(rows), e(parent)),
           "<p class='note'><b>This screen does not re-classify any dam and runs no model.</b> "
           "It samples the new structures layer on the depth raster each dam's last "
           "classification used and lists the structures in the flood that the last run did "
           "not have. It tells which dams to re-run (batch classification or the creep "
           "watch); only a re-run classifies. A point reads the cell it lies in, a polygon "
           "the largest value of the cells whose centre is inside it - the classification "
           "reads a building over its outline plus a 5-ft ring, so a structure at the flood "
           "edge can differ.</p>",
           "<p class='note'>New layer: {}<br>Previous release: {}<br>Wet: depth &gt; {} ft; "
           "same structure within {} ft (or inside the outline); re-run recommended from {} "
           "new structure(s). These are screening values chosen by the engineer, not "
           "requirements.</p>".format(e(_layer_text(new_layer)), e(_layer_text(prev_layer)),
                                      e(str(p["wet_ft"])), e(str(p["match_ft"])),
                                      e(str(p["min_new"]))),
           "<p class='note'>{}</p>".format(e(summary_text(rows))),
           "<table><tr>{}</tr>".format("".join("<th>{}</th>".format(e(c.replace("_", " ")))
                                               for c in cols))]
    for r in rows:
        cls = (r.get("Flag") or "").split(" ")[0]
        out.append("<tr class='{}'>{}</tr>".format(e(cls, quote=True), "".join(
            "<td>{}</td>".format(e(str(r.get(c, "")))) for c in cols)))
    out.append("</table></body></html>")
    return "\n".join(out)
