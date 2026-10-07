"""Compare a tool-7 flood with a reference model (HEC-RAS 2D, DSS-WISE Lite, the Runner) on
the same grid. Pure numpy; GDAL stays in the QGIS tool.

THE METRIC FUNCTIONS ARE COPIED FROM THE ND DAM BREACH RUNNER (nd_breach_runner/core/benchmark.py:
_wet, _err, compare_fields, verdict, difference_arrays) AND MUST STAY EQUAL TO THEM, so a
Screening-vs-reference comparison reads like a Runner-vs-reference one.
tests/test_tools_benchmark.py runs both modules on the same arrays when the Runner is on the
machine. Edit the Runner's module first, then copy the functions here.

All inputs are US units: depth ft, velocity ft/s, arrival hours. Wet means depth at or above
the threshold (THRESH_FT in the tool: 1.0 ft default, a screening value you choose; the
Runner's default is the same). Screening's tool-7 rasters are metres and m/s; the tool
converts them before calling anything here (to_us).

The scores are the usual flood-model verification ones:

* fit index F = A∩B / A∪B (Bates & De Roo 2000; 1 = same extent, above about
  0.8 is a close match), critical success index (the same number by another
  name), hit rate and false-alarm ratio, and the bias of the wet area;
* depth: bias, RMSE, mean absolute error and the 90th percentile of the
  absolute error over cells both models flood, and how often both put a
  cell in the same depth class;
* velocity and arrival time the same way.

What is this module's own: unit conversion, finding a dam's reference rasters in a folder,
the per-dam CSV / HTML report (plain HTML, no dependencies), and the statewide table.
"""
import csv
import html
import os

import numpy as np

M_PER_FT = 0.3048
DIFF_TIF = "Benchmark Depth Difference (ft).tif"
CODE_TIF = "Benchmark Extent Agreement.tif"
SUMMARY_CSV, REPORT_HTML, ALL_CSV = "Benchmark_Summary.csv", "Benchmark_Report.html", "Benchmark_All.csv"
RASTER_EXTS = (".tif", ".tiff", ".img", ".vrt", ".asc", ".flt")
# words that name a kind of raster in a file name (lower case); the first match wins
KIND_WORDS = {"depth": ("depth", "wd", "maxdepth", "d_max", "dmax"),
              "velocity": ("velocity", "vel", "speed", "maxvel", "v_max", "vmax"),
              "arrival": ("arrival", "arriv", "arrive", "time_to", "timeto", "tarr", "t_arr")}


# ============================================================ copied from the Runner: START
def _wet(depth, thr):
    return np.isfinite(depth) & (depth >= thr)


def _err(a, b):
    ok = np.isfinite(a) & np.isfinite(b)
    if not ok.any():
        return None
    d = (a - b)[ok]
    return {"n": int(ok.sum()), "bias": round(float(d.mean()), 3),
            "rmse": round(float(np.sqrt((d ** 2).mean())), 3),
            "mae": round(float(np.abs(d).mean()), 3),
            "p90_abs": round(float(np.percentile(np.abs(d), 90)), 3),
            "ratio_mean": round(float(np.nanmean(a[ok])) / max(float(np.nanmean(b[ok])), 1e-9), 3)}


def compare_fields(ours, ref, cell_area_ac, thr_ft=1.0, classes_ft=(1.0, 3.0, 6.0)):
    """ours, ref: dicts with "depth" and optionally "velocity", "arrival",
    "wse" (same shape). Returns a metrics dict."""
    a, b = ours["depth"], ref["depth"]
    wa, wb = _wet(a, thr_ft), _wet(b, thr_ft)
    both, either = wa & wb, wa | wb
    m = {"threshold_ft": thr_ft,
         "wet_acres_ours": round(float(wa.sum()) * cell_area_ac, 1),
         "wet_acres_reference": round(float(wb.sum()) * cell_area_ac, 1),
         "wet_acres_both": round(float(both.sum()) * cell_area_ac, 1),
         "fit_index_F": round(float(both.sum()) / max(1, int(either.sum())), 3),
         "hit_rate": round(float(both.sum()) / max(1, int(wb.sum())), 3),
         "false_alarm_ratio": round(float((wa & ~wb).sum()) / max(1, int(wa.sum())), 3),
         "area_bias_pct": round(100.0 * (float(wa.sum()) - float(wb.sum()))
                                / max(1.0, float(wb.sum())), 1)}
    aa = np.where(wa, a, np.nan)
    bb = np.where(wb, b, np.nan)
    m["depth_ft"] = _err(aa, bb)
    if "wse" in ours and "wse" in ref:
        m["wse_ft"] = _err(np.where(wa, ours["wse"], np.nan), np.where(wb, ref["wse"], np.nan))
    for k, unit in (("velocity", "fps"), ("arrival", "hr")):
        if k in ours and k in ref:
            m[k + "_" + unit] = _err(np.where(both, ours[k], np.nan),
                                     np.where(both, ref[k], np.nan))
    edges = [-np.inf] + list(classes_ft) + [np.inf]
    if both.any():
        ca = np.digitize(a[both], edges[1:-1])
        cb = np.digitize(b[both], edges[1:-1])
        m["depth_class_agreement"] = round(float((ca == cb).mean()), 3)
        m["depth_class_within_one"] = round(float((np.abs(ca - cb) <= 1).mean()), 3)
        n = len(edges) - 1
        conf = np.zeros((n, n), int)
        for i in range(n):
            for j in range(n):
                conf[i, j] = int(((ca == i) & (cb == j)).sum())
        m["depth_class_matrix"] = {"classes_ft": ["<{:g}".format(classes_ft[0])] + [
            "{:g}-{:g}".format(classes_ft[k], classes_ft[k + 1])
            for k in range(len(classes_ft) - 1)] + [">={:g}".format(classes_ft[-1])],
            "rows_ours_cols_reference": conf.tolist()}
    m["verdict"] = verdict(m)
    return m


def verdict(m):
    f = m["fit_index_F"]
    if f >= 0.9:
        s = "extents agree closely (F = {:.2f})".format(f)
    elif f >= 0.8:
        s = "extents agree well (F = {:.2f})".format(f)
    elif f >= 0.65:
        s = "extents differ noticeably (F = {:.2f}); check terrain, roughness and inflow " \
            "hydrograph in both models".format(f)
    else:
        s = "extents differ a lot (F = {:.2f}); the two models are not describing the same " \
            "flood: compare the terrain, the breach hydrograph and the boundaries first".format(f)
    d = m.get("depth_ft")
    if d:
        s += "; depths over the shared area differ by {:+.2f} ft on average (90% within {:.2f} " \
             "ft)".format(d["bias"], d["p90_abs"])
    return s


def difference_arrays(ours, ref, thr_ft=1.0):
    """Rasters for the map: depth difference (ours - reference) where either is
    wet, and a code raster 1 = only ours, 2 = only reference, 3 = both."""
    a, b = ours["depth"], ref["depth"]
    wa, wb = _wet(a, thr_ft), _wet(b, thr_ft)
    diff = np.where(wa | wb, np.nan_to_num(a) - np.nan_to_num(b), np.nan)
    code = np.where(wa & wb, 3.0, np.where(wa, 1.0, np.where(wb, 2.0, np.nan)))
    return {"Benchmark Depth Difference (ft).tif": diff, "Benchmark Extent Agreement.tif": code}
# ============================================================ copied from the Runner: END


# ---------------------------------------------------------------- this plugin's own part
def to_us(fields, units, nodata=None):
    """{"depth", "velocity", "arrival"} arrays -> US units (ft, ft/s, h), NaN where no data.
    units: "metric" (m, m/s, h: Screening's own rasters) or "us" (already ft, ft/s, h:
    HEC-RAS GeoTIFFs, DSS-WISE Lite after ND Dam Hazard's import, the Runner). `nodata`
    (a number) and anything below -1000 or not finite become NaN."""
    out = {}
    for k, a in fields.items():
        if a is None:
            continue
        a = np.asarray(a, "float64").copy()
        bad = ~np.isfinite(a) | (a < -1000.0)
        if nodata is not None:
            bad |= a == nodata
        a[bad] = np.nan
        if units == "metric" and k in ("depth", "velocity"):
            a = a / M_PER_FT
        out[k] = a
    return out


def kind_of(filename):
    """'depth' / 'velocity' / 'arrival' from a raster file name, else None."""
    stem = os.path.splitext(os.path.basename(filename))[0].lower()
    stem_t = stem.replace("-", " ").replace("_", " ").replace("(", " ").replace(")", " ")
    toks = set(stem_t.split())
    for kind, words in KIND_WORDS.items():
        if any(w in toks or (len(w) > 3 and w in stem) for w in words):
            return kind
    return None


def _rasters(folder):
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    return [n for n in names if n.lower().endswith(RASTER_EXTS)]


def find_reference(folder, dam_id):
    """Reference rasters of one dam in a folder: {"depth": path, "velocity": path?, "arrival":
    path?} or None when no depth raster is found. Two layouts: `<folder>/<dam_id>/` with the
    rasters named by kind (the Runner's "Depth (Max).tif", HEC-RAS "Depth (Max).tif" exports,
    anything with depth / velocity / arrival in the name), or files in `<folder>` whose name
    contains the dam ID (`DWR-5289_depth.tif`, `depth_DWR-5289.tif`). A sub-folder wins."""
    sub = os.path.join(folder, str(dam_id))
    out = {}
    if os.path.isdir(sub):
        for n in _rasters(sub):
            k = kind_of(n)
            if k and k not in out:
                out[k] = os.path.join(sub, n)
    if "depth" not in out:
        out = {}
        did = str(dam_id).lower()
        for n in _rasters(folder):
            if did in n.lower():
                k = kind_of(n)
                if k and k not in out:
                    out[k] = os.path.join(folder, n)
    return out if "depth" in out else None


def dams_with_reference(folder, dam_ids):
    return [d for d in dam_ids if find_reference(folder, d)]


def flatten_metrics(metrics):
    """[(name, value)] without the class matrix, nested dicts joined with '.'."""
    flat = []

    def walk(prefix, d):
        for k, v in d.items():
            if isinstance(v, dict):
                walk(prefix + k + ".", v)
            elif not isinstance(v, list):
                flat.append((prefix + k, v))
    walk("", {k: v for k, v in metrics.items() if k != "depth_class_matrix"})
    return flat


def write_summary_csv(path, metrics):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["metric", "value"])
        w.writerows(flatten_metrics(metrics))
    return path


def report_html(metrics, title, dam_id, inputs, raster_paths, thr_ft):
    """Plain HTML (no dependencies) for one dam. `inputs` = [(label, path)]."""
    e = html.escape
    rows = "".join("<tr><td>{}</td><td>{}</td></tr>".format(e(str(k)), e(str(v)))
                   for k, v in flatten_metrics(metrics) if k != "verdict")
    cm = metrics.get("depth_class_matrix")
    matrix = ""
    if cm:
        cl = cm["classes_ft"]
        head = "".join("<th>{}</th>".format(e(c)) for c in cl)
        body = "".join("<tr><th>{}</th>{}</tr>".format(e(c), "".join("<td>{}</td>".format(x) for x in row))
                       for c, row in zip(cl, cm["rows_ours_cols_reference"]))
        matrix = ("<h2>Depth classes (ft) over the cells both models flood</h2>"
                  "<p>Rows = this plugin (tool 7), columns = the reference model.</p>"
                  "<table><tr><th></th>{}</tr>{}</table>".format(head, body))
    ins = "".join("<li>{}: <code>{}</code></li>".format(e(a), e(str(b))) for a, b in inputs)
    outs = "".join("<li><code>{}</code></li>".format(e(str(p))) for p in raster_paths)
    return ("<!DOCTYPE html><html><head><meta charset='utf-8'><title>{t}</title>"
            "<style>body{{font-family:sans-serif;max-width:60em;margin:2em auto;padding:0 1em}}"
            "table{{border-collapse:collapse}}td,th{{border:1px solid #999;padding:2px 8px;text-align:left}}"
            ".v{{background:#eef;padding:.6em;border-left:4px solid #44a}}</style></head><body>"
            "<h1>{t}</h1><p>Dam <b>{d}</b>. Wet threshold {thr:g} ft (a screening value you choose). "
            "Both models were compared on the tool-7 grid (the reference resampled to it, nearest "
            "neighbour). The metrics are the ND Dam Breach Runner's (same functions).</p>"
            "<p class='v'>{v}.</p>"
            "<h2>Metrics</h2><table><tr><th>metric</th><th>value</th></tr>{rows}</table>{m}"
            "<h2>Inputs</h2><ul>{ins}</ul>"
            "<h2>Rasters</h2><p>Agreement code 1 = only this plugin floods the cell, 2 = only the "
            "reference, 3 = both; the depth difference is this plugin minus the reference, in feet.</p>"
            "<ul>{outs}</ul>"
            "<p><small>ND Dam Breach Screening - Compare with a reference model. Screening-level: a "
            "difference points at terrain, roughness, breach hydrograph or boundary choices to look "
            "at; it does not say which model is right. Not run in a real QGIS during development."
            "</small></p></body></html>").format(t=e(title), d=e(str(dam_id)), thr=thr_ft,
                                                   v=e(metrics["verdict"]), rows=rows, m=matrix,
                                                   ins=ins, outs=outs)


def all_row(dam_id, metrics, scen, ref_depth):
    """One flat row for Benchmark_All.csv."""
    d = metrics.get("depth_ft") or {}
    v = metrics.get("velocity_fps") or {}
    a = metrics.get("arrival_hr") or {}
    return {"dam_id": dam_id, "scenario": scen, "fit_index_F": metrics["fit_index_F"],
            "hit_rate": metrics["hit_rate"], "false_alarm_ratio": metrics["false_alarm_ratio"],
            "area_bias_pct": metrics["area_bias_pct"], "wet_acres_ours": metrics["wet_acres_ours"],
            "wet_acres_reference": metrics["wet_acres_reference"],
            "depth_bias_ft": d.get("bias"), "depth_rmse_ft": d.get("rmse"), "depth_p90_abs_ft": d.get("p90_abs"),
            "depth_class_agreement": metrics.get("depth_class_agreement"),
            "velocity_bias_fps": v.get("bias"), "velocity_rmse_fps": v.get("rmse"),
            "arrival_bias_hr": a.get("bias"), "arrival_rmse_hr": a.get("rmse"),
            "verdict": metrics["verdict"], "reference_depth": ref_depth}


ALL_COLUMNS = ["dam_id", "scenario", "fit_index_F", "hit_rate", "false_alarm_ratio", "area_bias_pct",
               "wet_acres_ours", "wet_acres_reference", "depth_bias_ft", "depth_rmse_ft",
               "depth_p90_abs_ft", "depth_class_agreement", "velocity_bias_fps", "velocity_rmse_fps",
               "arrival_bias_hr", "arrival_rmse_hr", "verdict", "reference_depth"]


def write_all_csv(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ALL_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in ALL_COLUMNS})
    return path
