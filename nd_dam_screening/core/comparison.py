"""Runner vs Screener comparison sheet (Item 8).

Reads the run_manifest.json the breach runner writes to each dam folder and combines it
with the screener's per-dam attribute values to produce a side-by-side HTML table.

Pure Python, no QGIS: can be unit-tested and called from runner_handoff.py.
"""
import json
import math
import os

FT_PER_M = 1.0 / 0.3048
CFS_PER_M3S = 35.3147
AC_PER_HA = 2.47105

MANIFEST_NAME = "run_manifest.json"     # same as nd_breach_runner.core.provenance.MANIFEST_NAME


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _fmt(v, fmt="{:,.0f}", missing="—"):
    """Format a number or return missing placeholder."""
    x = _num(v)
    return fmt.format(x) if x is not None else missing


def read_runner_manifest(dam_folder):
    """Key values from run_manifest.json in dam_folder. Returns {} on missing/invalid file."""
    path = os.path.join(dam_folder, MANIFEST_NAME)
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            m = json.load(fh)
    except (OSError, ValueError):
        return {}
    dam = m.get("dam") or {}
    hy = m.get("hydrograph") or {}
    settings = m.get("settings") or {}
    extra = m.get("extra") or {}
    cell_m = _num(settings.get("cell_size_m") or settings.get("cell_m") or
                  settings.get("cell"))
    return {
        "rn_top_ft": _num(dam.get("top_of_dam_ft")),
        "rn_peak_cfs": _num(hy.get("peak_cfs")),
        "rn_method": str(hy.get("method") or ""),
        "rn_cell_m": cell_m,
        "rn_solver": str(extra.get("lisflood_solver") or settings.get("lisflood_solver") or ""),
        "rn_height_ft": _num(dam.get("dam_height_ft")),
        "rn_storage_acft": _num(dam.get("storage_top_acft")),
    }


def _class_css(cls):
    return {"High": "cls-h", "Medium": "cls-m", "Low": "cls-l"}.get(cls, "cls-x")


def _agree_css(agree):
    return {"agree": "ag-ok", "runner higher": "ag-hi", "runner lower": "ag-lo"}.get(agree, "")


def build_row(dam_id, attrs, res, manifest):
    """One comparison dict from screener attrs, runner result res, and manifest data."""
    # screener
    scr_class = str(attrs.get("hz_class") or "")
    scr_peak_m3s = _num(attrs.get("qp_m3s") or attrs.get("peak_qp_m3s"))
    scr_peak_cfs = (scr_peak_m3s * CFS_PER_M3S) if scr_peak_m3s is not None else None
    scr_crest_m = _num(attrs.get("est_crest_m"))
    scr_crest_ft = (scr_crest_m * FT_PER_M) if scr_crest_m is not None else None
    scr_area_ha = _num(attrs.get("area_ha"))
    scr_area_ac = (scr_area_ha * AC_PER_HA) if scr_area_ha is not None else None
    scr_depth_m = _num(attrs.get("max_depth_m"))
    scr_depth_ft = (scr_depth_m * FT_PER_M) if scr_depth_m is not None else None
    scr_bldg_wet = _num(attrs.get("n_bldg_wet"))
    scr_bldg_let = _num(attrs.get("n_bldg_lethal"))
    scr_road_wet = _num(attrs.get("n_road_wet"))
    scr_grid_m = _num(attrs.get("grid_m"))
    scr_dem_m = _num(attrs.get("dem_m"))
    # runner
    rn_class = str(res.get("rn_class") or "")
    rn_agree = str(res.get("rn_agree") or "")
    rn_status = str(res.get("rn_status") or "")
    rn_border = str(res.get("rn_border") or "")
    rn_gaps = str(res.get("rn_gaps") or "")
    rn_inputs = str(res.get("rn_inputs") or "")
    # manifest
    rn_top_ft = manifest.get("rn_top_ft")
    rn_peak_cfs = manifest.get("rn_peak_cfs")
    rn_method = manifest.get("rn_method") or ""
    rn_cell_m = manifest.get("rn_cell_m")
    rn_solver = manifest.get("rn_solver") or ""
    # differences
    delta_crest = None
    if scr_crest_ft is not None and rn_top_ft is not None:
        delta_crest = rn_top_ft - scr_crest_ft
    peak_ratio = None
    if scr_peak_cfs and rn_peak_cfs:
        peak_ratio = rn_peak_cfs / scr_peak_cfs
    return {
        "dam_id": dam_id,
        "name": str(attrs.get("name") or ""),
        # classes
        "scr_class": scr_class, "rn_class": rn_class, "rn_agree": rn_agree,
        # flow
        "scr_peak_cfs": scr_peak_cfs, "rn_peak_cfs": rn_peak_cfs, "peak_ratio": peak_ratio,
        # top of dam
        "scr_crest_ft": scr_crest_ft, "rn_top_ft": rn_top_ft, "delta_crest_ft": delta_crest,
        # screener flood
        "scr_area_ac": scr_area_ac, "scr_depth_ft": scr_depth_ft,
        "scr_bldg_wet": scr_bldg_wet, "scr_bldg_let": scr_bldg_let,
        "scr_road_wet": scr_road_wet,
        # screener model
        "scr_grid_m": scr_grid_m, "scr_dem_m": scr_dem_m,
        # runner model
        "rn_method": rn_method, "rn_cell_m": rn_cell_m, "rn_solver": rn_solver,
        "rn_border": rn_border, "rn_gaps": rn_gaps, "rn_inputs": rn_inputs,
        "rn_status": rn_status,
    }


def write_comparison_html(path, rows, title="Runner vs Screener comparison"):
    """Write an HTML comparison table from a list of build_row() dicts."""
    if not rows:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    lines = [_HTML_HEAD.format(title=title)]
    lines.append("<h1>{}</h1>".format(title))
    lines.append("<p>{} dam{}</p>".format(len(rows), "s" if len(rows) != 1 else ""))
    lines.append(_TABLE_HEADER)
    for r in rows:
        lines.append(_table_row(r))
    lines.append("</tbody></table>")
    # legend
    lines.append('<div class="legend">'
                 '<span class="cls-h">High</span> '
                 '<span class="cls-m">Medium</span> '
                 '<span class="cls-l">Low</span> '
                 '<span class="ag-hi">runner higher</span> '
                 '<span class="ag-lo">runner lower</span>'
                 '</div>')
    lines.append("</body></html>")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return path


def _td(val, css=""):
    a = ' class="{}"'.format(css) if css else ""
    return "<td{}>{}</td>".format(a, val)


def _table_row(r):
    cells = []
    # ID and name
    cells.append(_td("{}<br><small>{}</small>".format(r["dam_id"], r["name"])))
    # screener class
    cells.append(_td(r["scr_class"] or "—", _class_css(r["scr_class"])))
    # runner class + agreement
    ag = r["rn_agree"]
    if r["rn_class"]:
        rn_lbl = r["rn_class"]
        if ag and ag != "agree":
            rn_lbl += " ({})".format(ag)
    else:
        rn_lbl = r["rn_status"] or "—"
    cells.append(_td(rn_lbl, _class_css(r["rn_class"]) + " " + _agree_css(ag)))
    # peak flow
    cells.append(_td(_fmt(r["scr_peak_cfs"])))
    rn_peak = _fmt(r["rn_peak_cfs"])
    if r["peak_ratio"] is not None:
        rn_peak += " <small>(×{:.2f})</small>".format(r["peak_ratio"])
    cells.append(_td(rn_peak))
    # top of dam
    cells.append(_td(_fmt(r["scr_crest_ft"], "{:,.1f}")))
    rn_top = _fmt(r["rn_top_ft"], "{:,.1f}")
    if r["delta_crest_ft"] is not None:
        sign = "+" if r["delta_crest_ft"] >= 0 else ""
        rn_top += " <small>({}{})</small>".format(sign, _fmt(r["delta_crest_ft"], "{:.1f}"))
    cells.append(_td(rn_top))
    # screener flood results
    cells.append(_td(_fmt(r["scr_area_ac"], "{:,.1f}")))
    cells.append(_td(_fmt(r["scr_depth_ft"], "{:,.1f}")))
    wet = _fmt(r["scr_bldg_wet"], "{:.0f}")
    let = _fmt(r["scr_bldg_let"], "{:.0f}")
    cells.append(_td("{} / {}".format(wet, let)))
    cells.append(_td(_fmt(r["scr_road_wet"], "{:.0f}")))
    # model: screener
    scr_model = ""
    if r["scr_grid_m"] is not None:
        scr_model = "{:.0f} m grid".format(r["scr_grid_m"])
    if r["scr_dem_m"] is not None:
        scr_model += " / {:.0f} m DEM".format(r["scr_dem_m"])
    cells.append(_td(scr_model))
    # model: runner
    rn_model = r["rn_method"]
    if r["rn_cell_m"] is not None:
        rn_model += " / {:.0f} m".format(r["rn_cell_m"])
    if r["rn_solver"]:
        rn_model += " / {}".format(r["rn_solver"])
    cells.append(_td(rn_model))
    # runner details
    cells.append(_td(r["rn_border"] or "—"))
    cells.append(_td("<small>{}</small>".format(r["rn_gaps"]) if r["rn_gaps"] else "—"))
    return "<tr>" + "".join(cells) + "</tr>"


_TABLE_HEADER = """
<table>
<thead>
<tr>
  <th rowspan="2">Dam</th>
  <th colspan="2">Class</th>
  <th colspan="2">Peak breach flow (cfs)</th>
  <th colspan="2">Top of dam (ft)</th>
  <th colspan="4">Screener flood</th>
  <th colspan="2">Model</th>
  <th colspan="2">Runner details</th>
</tr>
<tr>
  <th>Screener</th><th>Runner</th>
  <th>Screener</th><th>Runner</th>
  <th>Screener</th><th>Runner</th>
  <th>Area (ac)</th><th>Depth (ft)</th><th>Bldg wet/lethal</th><th>Roads wet</th>
  <th>Screener</th><th>Runner</th>
  <th>Border</th><th>Gaps</th>
</tr>
</thead>
<tbody>"""

_HTML_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
body{{font-family:system-ui,sans-serif;font-size:13px;margin:1rem 2rem;color:#222}}
h1{{font-size:1.1rem;margin-bottom:.3rem}}
p{{margin:.2rem 0 .8rem;color:#555}}
table{{border-collapse:collapse;width:100%;white-space:nowrap}}
th,td{{border:1px solid #ccc;padding:4px 6px;vertical-align:top}}
thead th{{background:#f0f0f0;font-size:11px;text-align:center}}
th[colspan]{{background:#e4e4e4}}
td small{{color:#666;font-size:11px}}
.cls-h{{background:#f8d7da;color:#6c1c22;font-weight:600}}
.cls-m{{background:#fff3cd;color:#6c4c00;font-weight:600}}
.cls-l{{background:#d1e7dd;color:#155724;font-weight:600}}
.cls-x{{color:#888}}
.ag-hi{{outline:2px solid #dc3545}}
.ag-lo{{outline:2px solid #0d6efd}}
.legend{{margin-top:.8rem;font-size:11px}}
.legend span{{display:inline-block;padding:2px 6px;margin-right:.4rem;border:1px solid #ccc}}
@media(prefers-color-scheme:dark){{
  body{{background:#1a1a1a;color:#ddd}}
  table{{border-color:#444}}
  th,td{{border-color:#444}}
  thead th{{background:#2a2a2a}}
  th[colspan]{{background:#252525}}
  td small{{color:#aaa}}
  .cls-h{{background:#5c1c22;color:#f8a0a8}}
  .cls-m{{background:#4c3c00;color:#ffe080}}
  .cls-l{{background:#1c4c30;color:#80e0a8}}
}}
</style>
</head>
<body>"""
