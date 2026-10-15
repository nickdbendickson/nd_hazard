"""What does a finer grid buy? Two runs of the same dam, compared (3.20.0).

Feature 15 of the review. A 10 m run of one dam costs most of an afternoon and a 20 m run
costs a few minutes, and the honest answer to "is it worth it" is a picture and a table, not an
opinion. Given two run folders of the same dam - different cell size, breach method or duration
cap - this writes

  * a difference raster, depth B minus depth A, on the finer of the two grids, so the deeper
    water of the finer run shows as positive feet;
  * a table of the structures whose ACER TM 11 danger zone CHANGES between the two runs, which
    is the only difference that can change a hazard class. A run that is 0.4 ft deeper
    everywhere and moves nobody across a zone boundary bought nothing.

    diff = difference(a_depth, b_depth)                 # grids in, grid + stats out
    rows, counts = zone_changes(points, a_fields, b_fields, charts)
    lines = markdown_lines(info)

Decisions:
  * The coarse run is sampled onto the fine grid by NEAREST cell, not interpolated. A coarse
    cell is one number for the whole cell; smoothing it would invent a gradient the model never
    computed and would hide, not show, the difference between the two grids.
  * Cells wet in neither run are no-data in the difference, not zero: a map of zeros over the
    whole county says nothing, and an engineer reading "0.0" where no water ever came would be
    reading a result that does not exist.
  * The zone table needs the charts to be signed off (core/nd_hazard, the shipped ACER TM 11
    file). Without them the difference raster and the depth statistics are still written and the
    table says why it is empty - the comparison is useful without a class.
  * Nothing here decides which run is right. A finer grid is not automatically the answer (the
    DEM may not carry the terrain the cells imply), and the summary says which run is deeper
    where rather than which is correct.
  * The two folders are not required to be the same dam, but the summary compares the dam IDs
    of the two run reports and says so when they differ, because comparing two dams by accident
    would be a quiet nonsense.

Pure numpy and text; the rasters come in through a reader the caller supplies, as everywhere
else in this plugin.
"""
import json
import os

import numpy as np

from . import workflow
from .grid import Grid

DIFF_NAME = "Depth Difference (B minus A).tif"
ROWS_NAME = "Run Comparison (Structures).csv"
SUMMARY_NAME = "Run Comparison.json"
MARKDOWN_NAME = "Run Comparison.md"
WET_FT = 0.1
COLUMNS = ("id", "x", "y", "a_depth_ft", "b_depth_ft", "depth_change_ft", "a_velocity_fps",
           "b_velocity_fps", "a_zone", "b_zone", "change", "mobile")
# the run-report keys worth putting side by side (what the two runs were)
REPORT_KEYS = (("dam_id", "Dam"), ("scenario", "Scenario"), ("cell_m", "Grid (m)"),
               ("breach_method", "Breach method"), ("sim_time_hr", "Simulated time (hr)"),
               ("duration_max_hr", "Longest simulated time (hr)"),
               ("final_model", "Final model"), ("lisflood_runs", "Domain runs"),
               ("max_depth_ft", "Max depth (ft)"), ("flooded_acres", "Flooded (ac)"),
               ("latest_arrival_hr", "Latest arrival (hr)"))


def report_path(folder):
    return os.path.join(folder or "", workflow.MODEL_FOLDER, "run_report.json")


def run_info(folder):
    """What a run folder says about itself: {key: value} out of its run report, flattened.

    Never raises: a folder with no report gives {"folder": ...} alone, and the comparison then
    says the two runs could not be described."""
    out = {"folder": folder or ""}
    p = report_path(folder)
    if not os.path.isfile(p):
        out["problem"] = "no run report in {}".format(os.path.join(
            os.path.basename(os.path.normpath(folder or "")), workflow.MODEL_FOLDER))
        return out
    try:
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError) as e:
        out["problem"] = "the run report could not be read ({})".format(e)
        return out
    if not isinstance(d, dict):
        out["problem"] = "the run report is not a JSON object"
        return out
    flat = {}
    for k, v in d.items():
        if isinstance(v, dict):
            for k2, v2 in v.items():
                flat.setdefault(k2, v2)
        else:
            flat.setdefault(k, v)
    for key, _label in REPORT_KEYS:
        if key in flat:
            out[key] = flat[key]
    out["report"] = p
    return out


def depth_path(folder):
    return os.path.join(folder or "", workflow.FINAL_NAMES["depth"])


def velocity_path(folder):
    return os.path.join(folder or "", workflow.FINAL_NAMES["velocity"])


def sample_nearest(grid, X, Y):
    """`grid` at the points, nearest cell, NaN outside and on no-data."""
    z = grid.masked()
    c = np.floor((X - grid.x0) / grid.dx).astype(int)
    r = np.floor((grid.y0 - Y) / grid.dx).astype(int)
    ok = (r >= 0) & (r < grid.ny) & (c >= 0) & (c < grid.nx)
    out = np.full(np.shape(X), np.nan, dtype=float)
    if ok.any():
        out[ok] = z[r[ok], c[ok]]
    return out


def difference(a, b, wet_ft=WET_FT):
    """(Grid of B - A on the finer grid, stats).

    The grids need not match: the coarser one is sampled onto the finer by nearest cell. A cell
    dry in both runs is no-data."""
    fine, coarse, flip = (b, a, False) if b.dx <= a.dx else (a, b, True)
    X, Y = np.meshgrid(fine.x_centers(), fine.y_centers())
    other = sample_nearest(coarse, X, Y)
    fine_z = fine.masked()
    f = np.where(np.isfinite(fine_z), fine_z, 0.0)
    o = np.where(np.isfinite(other), other, 0.0)
    a_arr, b_arr = (o, f) if not flip else (f, o)
    diff = b_arr - a_arr
    dry = (a_arr < wet_ft) & (b_arr < wet_ft)
    diff = np.where(dry, np.nan, diff)
    wet_a, wet_b = a_arr >= wet_ft, b_arr >= wet_ft
    cell_ac = (fine.dx ** 2) / 4046.8564224
    stats = {"cell_m": fine.dx, "coarse_cell_m": coarse.dx,
             "cells_compared": int(np.isfinite(diff).sum()),
             "max_increase_ft": _r(np.nanmax(diff)) if np.isfinite(diff).any() else 0.0,
             "max_decrease_ft": _r(np.nanmin(diff)) if np.isfinite(diff).any() else 0.0,
             "mean_change_ft": _r(np.nanmean(diff)) if np.isfinite(diff).any() else 0.0,
             "a_flooded_ac": _r(wet_a.sum() * cell_ac, 1),
             "b_flooded_ac": _r(wet_b.sum() * cell_ac, 1),
             "wet_only_in_a_ac": _r((wet_a & ~wet_b).sum() * cell_ac, 1),
             "wet_only_in_b_ac": _r((wet_b & ~wet_a).sum() * cell_ac, 1)}
    stats["flooded_change_ac"] = _r(stats["b_flooded_ac"] - stats["a_flooded_ac"], 1)
    return Grid(diff, fine.x0, fine.y0, fine.dx, np.nan, fine.crs_wkt), stats


def _r(v, nd=2):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    return round(f, nd) if f == f else ""


# -------------------------------------------------------------- zone changes --
def zone_changes(points, a_fields, b_fields, charts=None, wet_ft=WET_FT):
    """([row], counts): the structures whose danger zone differs between the two runs.

    points     [{"id", "x", "y", "mobile"}] in the rasters' CRS
    a_fields   {"depth": Grid, "velocity": Grid or None} of run A (feet, ft/s)
    charts     core.nd_hazard.ChartSet; None or not signed off = no zones, and the counts say so
    """
    rows, counts = [], {"structures": 0, "changed": 0, "raised": 0, "lowered": 0,
                        "wet_only_in_b": 0, "wet_only_in_a": 0, "zones": bool(charts)}
    chart_problem = ""
    if charts is not None:
        try:
            charts.chart(False)
        except Exception as e:                                   # noqa - not signed off
            chart_problem = str(e)
            charts = None
    for p in points or []:
        x, y = float(p["x"]), float(p["y"])
        got = {}
        for tag, fields in (("a", a_fields), ("b", b_fields)):
            d = _one(fields.get("depth"), x, y)
            v = _one(fields.get("velocity"), x, y)
            got[tag] = (d, v)
        counts["structures"] += 1
        ad, av = got["a"]
        bd, bv = got["b"]
        za = zb = ""
        if charts is not None:
            chart = charts.chart(bool(p.get("mobile")))
            za = chart.zone(ad or 0.0, av or 0.0)[0] if (ad or 0.0) >= wet_ft else "dry"
            zb = chart.zone(bd or 0.0, bv or 0.0)[0] if (bd or 0.0) >= wet_ft else "dry"
        change = ""
        if za != zb:
            change = "{} -> {}".format(za, zb)
        if (ad or 0.0) < wet_ft <= (bd or 0.0):
            counts["wet_only_in_b"] += 1
        if (bd or 0.0) < wet_ft <= (ad or 0.0):
            counts["wet_only_in_a"] += 1
        if change:
            counts["changed"] += 1
            if _rank(zb) > _rank(za):
                counts["raised"] += 1
            elif _rank(zb) < _rank(za):
                counts["lowered"] += 1
        rows.append({"id": p.get("id", ""), "x": round(x, 2), "y": round(y, 2),
                     "a_depth_ft": _r(ad), "b_depth_ft": _r(bd),
                     "depth_change_ft": _r((bd or 0.0) - (ad or 0.0)),
                     "a_velocity_fps": _r(av), "b_velocity_fps": _r(bv),
                     "a_zone": za, "b_zone": zb, "change": change,
                     "mobile": "yes" if p.get("mobile") else "no"})
    if chart_problem:
        counts["chart_problem"] = chart_problem
    return rows, counts


def _one(grid, x, y):
    if grid is None:
        return None
    v = sample_nearest(grid, np.array([x]), np.array([y]))[0]
    return None if not np.isfinite(v) else float(v)


_ZONE_RANK = {"dry": -1, "": -1, "low danger": 0, "judgment": 1, "high danger": 2}


def _rank(z):
    return _ZONE_RANK.get(z, -1)


def changed_only(rows):
    return [r for r in rows or [] if r.get("change")]


# ----------------------------------------------------------------- the report --
def side_by_side(a_info, b_info):
    """[(label, A, B)] of what the two runs were."""
    out = []
    for key, label in REPORT_KEYS:
        va, vb = a_info.get(key, ""), b_info.get(key, "")
        if va == "" and vb == "":
            continue
        out.append((label, va, vb))
    return out


def markdown_lines(info, title="Two runs compared"):
    """The comparison report."""
    a_info, b_info = info.get("a") or {}, info.get("b") or {}
    stats = info.get("stats") or {}
    counts = info.get("counts") or {}
    lines = ["# " + title, "",
             "- A: {}".format(a_info.get("folder") or "?"),
             "- B: {}".format(b_info.get("folder") or "?"), ""]
    for w in info.get("warnings") or []:
        lines.append("- {}".format(w))
    if info.get("warnings"):
        lines.append("")
    rows = side_by_side(a_info, b_info)
    if rows:
        lines += ["| What | A | B |", "| --- | --- | --- |"]
        lines += ["| {} | {} | {} |".format(*r) for r in rows]
        lines.append("")
    lines += ["## Depth difference (B minus A)", "",
              "- Compared on the {} m grid ({} cells); the {} m run was sampled onto it by "
              "nearest cell.".format(stats.get("cell_m"), stats.get("cells_compared"),
                                     stats.get("coarse_cell_m")),
              "- Deepest increase {} ft, deepest decrease {} ft, mean change {} ft.".format(
                  stats.get("max_increase_ft"), stats.get("max_decrease_ft"),
                  stats.get("mean_change_ft")),
              "- Flooded area: A {} ac, B {} ac ({:+g} ac); {} ac wet only in A, {} ac wet "
              "only in B.".format(stats.get("a_flooded_ac"), stats.get("b_flooded_ac"),
                                  stats.get("flooded_change_ac") or 0.0,
                                  stats.get("wet_only_in_a_ac"),
                                  stats.get("wet_only_in_b_ac")),
              "- A cell dry in both runs is no-data in the difference raster, not zero.", ""]
    lines += ["## Structures whose danger zone changes", ""]
    if not counts.get("zones"):
        lines.append("- No zones: {}".format(counts.get("chart_problem")
                                             or "no structures or no signed-off ACER TM 11 "
                                                "chart file was given."))
    else:
        lines.append("- {} structure(s) tested; {} change zone ({} to a higher zone, {} to a "
                     "lower one).".format(counts.get("structures", 0),
                                          counts.get("changed", 0), counts.get("raised", 0),
                                          counts.get("lowered", 0)))
        lines.append("- Wet only in B: {}; wet only in A: {}.".format(
            counts.get("wet_only_in_b", 0), counts.get("wet_only_in_a", 0)))
        changed = changed_only(info.get("rows"))
        if changed:
            lines += ["", "| Structure | A depth (ft) | B depth (ft) | Zone A | Zone B |",
                      "| --- | --- | --- | --- | --- |"]
            for r in changed[:40]:
                lines.append("| {} | {} | {} | {} | {} |".format(
                    r["id"], r["a_depth_ft"], r["b_depth_ft"], r["a_zone"], r["b_zone"]))
            if len(changed) > 40:
                lines.append("| ... {} more in {} | | | | |".format(len(changed) - 40,
                                                                     ROWS_NAME))
    lines += ["", "- Neither run is taken to be the right one: a finer grid is only better "
                  "where the DEM really carries the terrain its cells imply.", ""]
    return lines


def _cell(v):
    s = "" if v is None else str(v)
    return '"{}"'.format(s.replace('"', '""')) if ("," in s or '"' in s) else s


def write_outputs(out_folder, info, diff_grid=None, writer=None):
    """Run Comparison.csv / .json / .md (and the difference raster when a writer is given).

    `writer(grid, path)` is the plugin's GeoTIFF writer. Returns {kind: path}."""
    os.makedirs(out_folder, exist_ok=True)
    out = {}
    p = os.path.join(out_folder, ROWS_NAME)
    with open(p, "w", newline="", encoding="utf-8") as fh:
        fh.write(",".join(COLUMNS) + "\n")
        for r in info.get("rows") or []:
            fh.write(",".join(_cell(r.get(c, "")) for c in COLUMNS) + "\n")
    out["csv"] = p
    p = os.path.join(out_folder, SUMMARY_NAME)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({k: v for k, v in info.items() if k != "rows"}, fh, indent=1, default=str)
    out["json"] = p
    p = os.path.join(out_folder, MARKDOWN_NAME)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("\n".join(markdown_lines(info)))
    out["markdown"] = p
    if diff_grid is not None and writer is not None:
        p = os.path.join(out_folder, DIFF_NAME)
        writer(diff_grid, p)
        out["difference"] = p
    return out


def compare(a_folder, b_folder, read_raster):
    """The whole comparison except the structures: the two runs described, the difference
    raster and its statistics.

    `read_raster(path)` returns a Grid in feet (the dam folder's rasters are in feet).
    Returns (info, difference Grid or None)."""
    a_info, b_info = run_info(a_folder), run_info(b_folder)
    warnings = []
    for tag, inf in (("A", a_info), ("B", b_info)):
        if inf.get("problem"):
            warnings.append("Run {}: {}".format(tag, inf["problem"]))
    if a_info.get("dam_id") and b_info.get("dam_id") and \
            str(a_info["dam_id"]).strip() != str(b_info["dam_id"]).strip():
        warnings.append("The two runs are of DIFFERENT dams ({} and {}): the comparison is "
                        "almost certainly not what you meant.".format(a_info["dam_id"],
                                                                      b_info["dam_id"]))
    diff, stats = None, {}
    pa, pb = depth_path(a_folder), depth_path(b_folder)
    for tag, p in (("A", pa), ("B", pb)):
        if not os.path.isfile(p):
            warnings.append("Run {} has no {}".format(tag, workflow.FINAL_NAMES["depth"]))
    if os.path.isfile(pa) and os.path.isfile(pb):
        diff, stats = difference(read_raster(pa), read_raster(pb))
    info = {"a": a_info, "b": b_info, "stats": stats, "warnings": warnings, "rows": [],
            "counts": {}}
    return info, diff
