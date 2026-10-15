"""Merge the DWR dams (tools 1-4) and the ponds (tool 16) into one set of layers for tools 5-10
(0.13, the Simple tab's "Both"). Pure Python: GeoPackages are SQLite files, so the rows of the
pond layers are appended to copies of the dam layers with sqlite3 (a union of the columns; the
geometry blobs are copied as they are - both sets are written in the same CRS by the tools:
dams in EPSG:4326, breach points and arrows in EPSG:5070). The stage-storage curves and terrain
clips are hard-linked (copied where a link is not possible) into one folder each.

    merge(work) -> {"dams": path, "bpts": path, "arrows": path, "curves": folder, "terrain": folder,
                    "n_dams": n, "n_ponds": n, "notes": [...]}

Nothing here needs QGIS or GDAL. Not run in a real QGIS.
"""
import os
import shutil
import sqlite3

from . import workflow as wf
from . import gpkg_sqlite

LAYOUT = {"OUT": "18_dams_all.gpkg", "OUTP": "18_breach_points_all.gpkg", "OUTL": "18_flow_arrows_all.gpkg",
          "CURVES": "18_stage_storage", "TERRAIN": "18_terrain"}


def _feature_table(con):
    row = con.execute("SELECT table_name FROM gpkg_contents WHERE data_type='features'").fetchone()
    if row is None:
        raise ValueError("no feature table")
    return row[0]


def _columns(con, table):
    return [(r[1], r[2] or "TEXT") for r in con.execute('PRAGMA table_info("{}")'.format(table))]


def append_gpkg(dst_path, src_path, notes=None):
    """Append the rows of the (single) feature table of src to the feature table of dst, adding
    the columns dst lacks. Returns the number of rows appended."""
    notes = notes if notes is not None else []
    con = sqlite3.connect(dst_path)
    try:
        dst = _feature_table(con)
        # 0.29.0: a dst GeoPackage that QGIS/GDAL wrote has rtree triggers calling ST_IsEmpty,
        # which plain sqlite3 lacks; drop the index so the INSERT below does not fail. (The merged
        # 18_* layer is an intermediate for steps 5-10; QGIS rebuilds an index if it needs one.)
        gpkg_sqlite.disable_rtree(con, dst)
        con.execute("ATTACH DATABASE ? AS src", (src_path,))
        row = con.execute("SELECT table_name FROM src.gpkg_contents WHERE data_type='features'").fetchone()
        if row is None:
            raise ValueError("{} has no feature table".format(src_path))
        srct = row[0]
        dcols = _columns(con, dst)
        scols = [(r[1], r[2] or "TEXT") for r in con.execute('PRAGMA src.table_info("{}")'.format(srct))]
        dnames = {c for c, _t in dcols}
        geom_d = con.execute("SELECT column_name FROM gpkg_geometry_columns WHERE table_name=?", (dst,)).fetchone()[0]
        geom_s = con.execute("SELECT column_name FROM src.gpkg_geometry_columns WHERE table_name=?", (srct,)).fetchone()[0]
        for c, t in scols:
            if c in ("fid", geom_s) or c in dnames:
                continue
            con.execute('ALTER TABLE "{}" ADD COLUMN "{}" {}'.format(dst, c, t))
            dnames.add(c)
            notes.append("added column {} to {}".format(c, os.path.basename(dst_path)))
        common = [c for c, _t in scols if c not in ("fid", geom_s)]
        cols_d = '"{}"'.format(geom_d) + "".join(', "{}"'.format(c) for c in common)
        cols_s = '"{}"'.format(geom_s) + "".join(', "{}"'.format(c) for c in common)
        n = con.execute('SELECT COUNT(*) FROM src."{}"'.format(srct)).fetchone()[0]
        con.execute('INSERT INTO "{}" ({}) SELECT {} FROM src."{}"'.format(dst, cols_d, cols_s, srct))
        # the recorded extent is stale now; NULL lets QGIS compute it
        con.execute("UPDATE gpkg_contents SET min_x=NULL, min_y=NULL, max_x=NULL, max_y=NULL WHERE table_name=?", (dst,))
        con.commit()
        con.execute("DETACH DATABASE src")
        return int(n)
    finally:
        con.close()


def _link_all(src_dir, dst_dir, exts, notes):
    n = 0
    if not src_dir or not os.path.isdir(src_dir):
        return 0
    os.makedirs(dst_dir, exist_ok=True)
    for fn in sorted(os.listdir(src_dir)):
        if not fn.lower().endswith(exts):
            continue
        s, d = os.path.join(src_dir, fn), os.path.join(dst_dir, fn)
        if os.path.exists(d):
            os.remove(d)
        try:
            os.link(s, d)
        except OSError:
            shutil.copyfile(s, d)
        n += 1
    return n


def merge(work):
    """Build the 18_* outputs from the 3/4 (and 2) outputs plus the 16 outputs. Raises
    FileNotFoundError when a side is missing."""
    notes = []
    dams = wf.path(work, "3", "OUT")
    ponds = wf.path(work, "16", "OUT")
    if not os.path.isfile(dams):
        raise FileNotFoundError("no dams with storage ({}): run steps 1-3 first".format(os.path.basename(dams)))
    if not os.path.isfile(ponds):
        raise FileNotFoundError("no ponds ({}): run step 16 first".format(os.path.basename(ponds)))
    out = {}
    pairs = (("OUT", wf.path(work, "3", "OUT"), ponds),
             ("OUTP", wf.path(work, "4", "OUTP"), wf.path(work, "16", "OUTP")),
             ("OUTL", wf.path(work, "4", "OUTL"), wf.path(work, "16", "OUTL")))
    counts = {}
    for key, a, b in pairs:
        dst = os.path.join(work, LAYOUT[key])
        if not os.path.isfile(a):
            if key == "OUTL":
                notes.append("no {}: arrows not merged".format(os.path.basename(a)))
                continue
            raise FileNotFoundError("no {}: run step 4 first".format(os.path.basename(a)))
        if os.path.exists(dst):
            os.remove(dst)
        shutil.copyfile(a, dst)
        n = append_gpkg(dst, b, notes) if os.path.isfile(b) else 0
        if not os.path.isfile(b):
            notes.append("no {}: nothing appended".format(os.path.basename(b)))
        counts[key] = n
        out[key] = dst
    con = sqlite3.connect(out["OUT"])
    try:
        t = _feature_table(con)
        total = con.execute('SELECT COUNT(*) FROM "{}"'.format(t)).fetchone()[0]
    finally:
        con.close()
    curves = os.path.join(work, LAYOUT["CURVES"])
    terrain = os.path.join(work, LAYOUT["TERRAIN"])
    nc = _link_all(wf.path(work, "3", "CURVES"), curves, (".csv",), notes) + \
        _link_all(wf.path(work, "16", "CURVES"), curves, (".csv",), notes)
    nt = _link_all(wf.path(work, "2", "OUT"), terrain, (".tif", ".tiff"), notes) + \
        _link_all(wf.path(work, "16", "TERRAIN"), terrain, (".tif", ".tiff"), notes)
    return {"dams": out["OUT"], "bpts": out.get("OUTP", ""), "arrows": out.get("OUTL", ""),
            "curves": curves, "terrain": terrain, "n_dams": int(total) - counts.get("OUT", 0),
            "n_ponds": counts.get("OUT", 0), "n_curves": nc, "n_terrain": nt, "notes": notes}
