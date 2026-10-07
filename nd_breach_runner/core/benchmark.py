"""Compare this tool's rasters with another model's (a HEC-RAS 2D run of the
same dam, say) on the same grid.

All inputs are US units as the tool writes them: depth ft, velocity ft/s,
arrival hours, WSE ft. Wet means depth at or above the threshold.

The scores are the usual flood-model verification ones:

* fit index F = A∩B / A∪B (Bates & De Roo 2000; 1 = same extent, above about
  0.8 is a close match), critical success index (the same number by another
  name), hit rate and false-alarm ratio, and the bias of the wet area;
* depth: bias, RMSE, mean absolute error and the 90th percentile of the
  absolute error over cells both models flood, and how often both put a
  cell in the same depth class;
* velocity and arrival time the same way.
"""
import csv
import os

import numpy as np


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


def write_outputs(folder, metrics, arrays, writer, grid, title="Benchmark against a reference model"):
    os.makedirs(folder, exist_ok=True)
    paths = {}
    for name, a in arrays.items():
        p = os.path.join(folder, name)
        writer(grid.like(a, np.nan), p)
        paths[name] = p
    flat = []

    def walk(prefix, d):
        for k, v in d.items():
            if isinstance(v, dict):
                walk(prefix + k + ".", v)
            elif not isinstance(v, list):
                flat.append((prefix + k, v))
    walk("", {k: v for k, v in metrics.items() if k != "depth_class_matrix"})
    csv_path = os.path.join(folder, "Benchmark_Summary.csv")
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["metric", "value"])
        w.writerows(flat)
    md = ["# " + title, "", metrics["verdict"] + ".", "", "| metric | value |", "|---|---|"]
    md += ["| {} | {} |".format(k, v) for k, v in flat if k != "verdict"]
    cm = metrics.get("depth_class_matrix")
    if cm:
        cl = cm["classes_ft"]
        md += ["", "Depth classes (ft), cells both models flood: rows = this tool, columns = "
               "reference", "", "| | " + " | ".join(cl) + " |", "|" + "---|" * (len(cl) + 1)]
        for c, row in zip(cl, cm["rows_ours_cols_reference"]):
            md.append("| {} | ".format(c) + " | ".join(str(x) for x in row) + " |")
    md += ["", "Rasters: agreement code 1 = only this tool floods the cell, 2 = only the "
               "reference, 3 = both; the depth difference is this tool minus the reference.", ""]
    md += ["- " + p for p in paths.values()]
    from . import products
    html_path = os.path.join(folder, "Benchmark_Report.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(products.markdown_to_html("\n".join(md), title))
    return {"csv": csv_path, "html": html_path, "rasters": paths}
