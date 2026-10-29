"""Offline dashboard.html next to run_report.html.

Reads a finished Runner folder. Does not rerun the flood and does not
assign an ND 5.2 hazard class. Same file for DamSafety_Runner and
nd_breach_runner.

Looks in ``model_dir`` (the ``_breach_model`` folder) and its parent:

* ``run_report.json`` — ``summary``, ``dam``, ``warnings``, ``hydrograph``
* ``breach_hydrograph.csv`` — header ``time_hr,discharge_cfs``
* ``gauge_<name>.csv`` in the model folder, and ``gauges/*.csv``
* ``receptors_flooded.csv`` — ``id, kind, x, y, depth_ft, velocity_fps, arrival_hr``

Not called from ``workflow.py``. The dock button calls ``write_dashboard``.
To write it at the end of a run, after ``write_html_report``::

    from .dashboard_html import write_dashboard
    write_dashboard(model_dir, report)
"""
import csv
import html
import json
import os


def _read_json(path):
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def _find(model_dir, name):
    parent = os.path.dirname(model_dir)
    for folder in (model_dir, parent):
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            return path
    return None


def _series(path):
    rows = []
    if not path or not os.path.isfile(path):
        return rows
    with open(path, encoding="utf-8", newline="") as f:
        for line in f:
            parts = [p.strip() for p in line.replace(";", ",").split(",")]
            if len(parts) < 2:
                continue
            try:
                rows.append([float(parts[0]), float(parts[1])])
            except ValueError:
                continue
    return rows


def _num(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _receptors(path):
    if not path:
        return []
    with open(path, encoding="utf-8", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(f, dialect=dialect)
        if not reader.fieldnames:
            return []
        fields = {name.lower().strip(): name for name in reader.fieldnames if name}

        def col(*names):
            for n in names:
                if n in fields:
                    return fields[n]
            return None

        name_k = col("id", "name", "structure", "feature", "label")
        depth_k = col("depth_ft", "depth", "max_depth_ft", "depth_max_ft")
        vel_k = col("velocity_fps", "velocity", "vel_fps", "max_velocity_fps")
        arr_k = col("arrival_hr", "arrival", "arrival_h", "arrival_time_hr")
        kind_k = col("kind", "type", "layer")
        out = []
        for row in reader:
            depth = _num(row.get(depth_k)) if depth_k else None
            if depth is None:
                continue
            vel = _num(row.get(vel_k)) if vel_k else None
            out.append({
                "name": (row.get(name_k) or "").strip() if name_k else "",
                "kind": (row.get(kind_k) or "").strip() if kind_k else "",
                "depth_ft": depth,
                "velocity_fps": vel,
                "arrival_hr": _num(row.get(arr_k)) if arr_k else None,
                "dxv": None if vel is None else round(depth * vel, 2),
            })
    return out


def _gauges(model_dir):
    seen = set()
    found = []
    parent = os.path.dirname(model_dir)
    folders = [model_dir]
    if parent and parent != model_dir:
        folders.append(parent)
    for folder in folders:
        if not os.path.isdir(folder):
            continue
        names = []
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for name in names:
            low = name.lower()
            path = os.path.join(folder, name)
            label = None
            if low.startswith("gauge_") and low.endswith(".csv") and os.path.isfile(path):
                label = name[6:-4]
            elif low == "gauges" and os.path.isdir(path):
                for child in sorted(os.listdir(path)):
                    if child.lower().endswith(".csv"):
                        cpath = os.path.join(path, child)
                        key = os.path.abspath(cpath)
                        if key in seen:
                            continue
                        seen.add(key)
                        found.append({"name": child[:-4], "series": _series(cpath)})
                continue
            if label is None:
                continue
            key = os.path.abspath(path)
            if key in seen:
                continue
            seen.add(key)
            found.append({"name": label, "series": _series(path)})
    return found


def _summary(report):
    block = report.get("summary")
    if isinstance(block, dict) and block:
        return block
    if report.get("peak_breach_cfs") is not None or report.get("dam_id"):
        return report
    return {}


def write_dashboard(model_dir, report=None):
    """Write dashboard.html. ``report`` is the dict already in memory, if any."""
    model_dir = os.path.abspath(model_dir)
    if report is None:
        report = _read_json(os.path.join(model_dir, "run_report.json"))
    if not report:
        report = _read_json(_find(model_dir, "run_report.json") or "")
    summary = _summary(report)
    dam = report.get("dam") if isinstance(report.get("dam"), dict) else {}
    hydro_meta = report.get("hydrograph") if isinstance(report.get("hydrograph"), dict) else {}
    parent = os.path.basename(os.path.dirname(model_dir))
    dam_id = summary.get("dam_id") or dam.get("dam_id") or parent or "Breach results"
    hydro = _series(_find(model_dir, "breach_hydrograph.csv"))
    warnings = report.get("warnings") or []
    if not isinstance(warnings, list):
        warnings = [str(warnings)]
    payload = {
        "dam_id": dam_id,
        "name": dam.get("name") or "",
        "scenario": summary.get("scenario") or "",
        "method": summary.get("breach_method") or hydro_meta.get("failure_mode") or "",
        "solver": summary.get("final_model") or report.get("flood_engine_short") or "",
        "peak_cfs": _num(summary.get("peak_breach_cfs")),
        "flooded_acres": _num(summary.get("flooded_acres")),
        "max_depth_ft": _num(summary.get("max_depth_ft")),
        "max_velocity_fps": _num(summary.get("max_velocity_fps")),
        "latest_arrival_hr": _num(summary.get("latest_arrival_hr")),
        "reaches": summary.get("receptors_flooded") or "",
        "warnings": [str(w) for w in warnings[:24]],
        "hydro": hydro,
        "gauges": _gauges(model_dir),
        "receptors": _receptors(_find(model_dir, "receptors_flooded.csv")),
    }
    out = os.path.join(model_dir, "dashboard.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(_page(payload))
    return out


def _page(payload):
    data = json.dumps(payload, ensure_ascii=True)
    data = data.replace("<", "\\u003c")
    title = html.escape(str(payload.get("dam_id") or "Breach results"))
    return _TEMPLATE.replace("__TITLE__", title).replace("__DATA__", data)


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>__TITLE__</title>
<style>
  :root { --paper:#f3efe6; --ink:#1c2420; --muted:#5c665e; --line:#d8d0c2;
          --card:#fffdf8; --water:#0e5f6e; --sand:#e7dfd0; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--paper); color:var(--ink);
         font:16px/1.45 "Source Sans 3", "Segoe UI", sans-serif; }
  main { max-width:980px; margin:0 auto; padding:24px 16px 56px; }
  h1 { font-size:1.7rem; margin:0 0 4px; letter-spacing:-0.02em; }
  h2 { font-size:1.05rem; margin:22px 0 8px; }
  button { cursor:pointer; }
  .kicker { color:var(--muted); font-size:0.82rem; letter-spacing:0.06em;
            text-transform:uppercase; margin:0 0 6px; }
  .note { background:var(--sand); border:1px solid var(--line); padding:8px 12px;
          font-size:0.9rem; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
           gap:1px; background:var(--line); margin:16px 0; }
  .cards div { background:var(--card); padding:10px 12px; }
  .cards span { display:block; color:var(--muted); font-size:0.72rem;
                letter-spacing:0.04em; text-transform:uppercase; }
  .cards b { font-family:ui-monospace, "IBM Plex Mono", monospace;
             font-size:1.15rem; font-weight:500; }
  table { width:100%; border-collapse:collapse; background:var(--card); font-size:0.9rem; }
  th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); }
  th button { background:none; border:0; font:inherit; color:var(--muted);
              text-transform:uppercase; letter-spacing:0.04em; font-size:0.72rem; }
  svg { width:100%; height:240px; background:var(--card); border:1px solid var(--line); display:block; }
  .muted { color:var(--muted); font-size:0.85rem; }
  ul.warn { margin:8px 0 0; padding-left:1.1rem; }
  ul.warn li { margin:4px 0; }
</style>
</head>
<body>
<main>
  <p class="kicker">ND Dam Breach Runner · read from this dam folder</p>
  <h1 id="title"></h1>
  <p class="muted" id="sub"></p>
  <p class="note">This page only displays files the run already wrote. It does not rerun the model. Hazard class stays in the ND 5.2 report.</p>
  <div class="cards" id="cards"></div>
  <h2>Breach hydrograph</h2>
  <svg id="chart" viewBox="0 0 640 220" role="img" aria-label="Breach hydrograph"></svg>
  <p class="muted" id="chart-note"></p>
  <h2>Receptors reached</h2>
  <table id="table"></table>
  <p class="muted" id="reaches"></p>
  <h2 id="warn-h" hidden>Things to check</h2>
  <ul class="warn" id="warns"></ul>
</main>
<script id="payload" type="application/json">__DATA__</script>
<script>
const data = JSON.parse(document.getElementById("payload").textContent);
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>]/g, function (ch) {
    var amp = String.fromCharCode(38);
    if (ch === amp) return amp + "amp;";
    if (ch === "<") return amp + "lt;";
    return amp + "gt;";
  });
}
function fmt(n, digits, suffix) {
  if (n == null || n !== n) return "\u2014";
  var t = Math.abs(n) >= 1000 ? Math.round(n).toLocaleString() : n.toFixed(digits);
  return t + (suffix || "");
}
const title = data.name ? data.name + " (" + data.dam_id + ")" : (data.dam_id || "Breach results");
document.getElementById("title").textContent = title;
document.title = title;
document.getElementById("sub").textContent = [data.scenario, data.method, data.solver].filter(Boolean).join(" \u00b7 ");
const hydro = data.hydro || [];
const hydroPeak = hydro.reduce(function (m, p) { return Math.max(m, p[1]); }, 0);
const peak = data.peak_cfs != null ? data.peak_cfs : (hydroPeak || null);
const cards = [
  ["Peak discharge", fmt(peak, 0, " cfs")],
  ["Flooded area", fmt(data.flooded_acres, 0, " ac")],
  ["Max depth", fmt(data.max_depth_ft, 1, " ft")],
  ["Max velocity", fmt(data.max_velocity_fps, 1, " ft/s")],
  ["Receptors", String((data.receptors || []).length)],
  ["Gauges", String((data.gauges || []).length)]
];
document.getElementById("cards").innerHTML = cards.map(function (c) {
  return "<div><span>" + esc(c[0]) + "</span><b>" + esc(c[1]) + "</b></div>";
}).join("");

function line(pts, maxT, maxQ, color, dash) {
  if (!pts.length || !maxQ) return "";
  var d = pts.map(function (p, i) {
    var x = 40 + (p[0] / maxT) * 580;
    var y = 190 - (p[1] / maxQ) * 160;
    return (i ? "L" : "M") + x.toFixed(1) + "," + y.toFixed(1);
  }).join("");
  return '<path d="' + d + '" fill="none" stroke="' + color + '" stroke-width="2"' +
    (dash ? ' stroke-dasharray="5 3"' : "") + "/>";
}
const gauges = (data.gauges || []).filter(function (g) { return g.series && g.series.length; });
const maxT = Math.max(1, hydro.reduce(function (m, p) { return Math.max(m, p[0]); }, 0),
  gauges.reduce(function (m, g) {
    return Math.max(m, g.series.reduce(function (a, p) { return Math.max(a, p[0]); }, 0));
  }, 0));
const maxQ = Math.max(hydroPeak, gauges.reduce(function (m, g) {
  return Math.max(m, g.series.reduce(function (a, p) { return Math.max(a, p[1]); }, 0));
}, 0), 1);
const colors = ["#1c2420", "#8a5a12", "#3f6b4e"];
document.getElementById("chart").innerHTML =
  '<line x1="40" y1="190" x2="620" y2="190" stroke="#d8d0c2"/>' +
  '<line x1="40" y1="30" x2="40" y2="190" stroke="#d8d0c2"/>' +
  line(hydro, maxT, maxQ, "#0e5f6e", false) +
  gauges.slice(0, 3).map(function (g, i) {
    return line(g.series, maxT, maxQ, colors[i], true);
  }).join("");
var names = gauges.slice(0, 3).map(function (g) { return g.name; });
document.getElementById("chart-note").textContent =
  "Solid line: breach outflow, cfs. " +
  (names.length ? ("Dashed: " + names.join(", ") + ".") : "No gauge CSV in this folder.");

let rows = (data.receptors || []).slice();
let sortKey = "depth_ft";
let dir = -1;
const cols = [
  ["name", "Feature"], ["kind", "Kind"], ["depth_ft", "Depth ft"],
  ["velocity_fps", "Vel ft/s"], ["arrival_hr", "Arrival h"], ["dxv", "D x V"]
];
function drawTable() {
  rows.sort(function (a, b) {
    var av = a[sortKey], bv = b[sortKey];
    if (typeof av === "number" && typeof bv === "number") return (av - bv) * dir;
    return String(av || "").localeCompare(String(bv || "")) * dir;
  });
  var head = "<tr>" + cols.map(function (c) {
    var mark = sortKey === c[0] ? (dir < 0 ? " \u2193" : " \u2191") : "";
    return '<th><button type="button" data-k="' + esc(c[0]) + '">' + esc(c[1] + mark) + "</button></th>";
  }).join("") + "</tr>";
  var body = rows.map(function (r) {
    return "<tr>" + cols.map(function (c) {
      var v = r[c[0]];
      var shown = typeof v === "number" ? v.toFixed(c[0] === "dxv" ? 1 : 2) : (v || "");
      return "<td>" + esc(shown) + "</td>";
    }).join("") + "</tr>";
  }).join("");
  var table = document.getElementById("table");
  table.innerHTML = head + (body || '<tr><td colspan="6">No receptors_flooded.csv rows.</td></tr>');
  table.querySelectorAll("button").forEach(function (btn) {
    btn.onclick = function () {
      var k = btn.getAttribute("data-k");
      if (k === sortKey) dir = -dir; else { sortKey = k; dir = (k === "name" || k === "kind") ? 1 : -1; }
      drawTable();
    };
  });
}
drawTable();
if (data.reaches) document.getElementById("reaches").textContent = "Run summary: " + data.reaches;
var warns = data.warnings || [];
if (warns.length) {
  document.getElementById("warn-h").hidden = false;
  document.getElementById("warns").innerHTML = warns.map(function (w) {
    return "<li>" + esc(w) + "</li>";
  }).join("");
}
</script>
</body>
</html>
"""


def _self_check():
    import shutil
    import tempfile
    folder = tempfile.mkdtemp(prefix="dash_")
    try:
        report = {
            "dam": {"dam_id": "SAMPLE-ABS", "name": "Absaraka Dam"},
            "summary": {
                "dam_id": "SAMPLE-ABS", "scenario": "sunny_day",
                "breach_method": "Froehlich 2008", "final_model": "LISFLOOD-FP",
                "peak_breach_cfs": 8420, "flooded_acres": 120.5,
                "max_depth_ft": 11.4, "max_velocity_fps": 4.2,
                "latest_arrival_hr": 3.5, "receptors_flooded": "2 structures",
            },
            "warnings": ["CHECK THE DAM POINT", "a<b & c"],
        }
        with open(os.path.join(folder, "run_report.json"), "w", encoding="utf-8") as f:
            json.dump(report, f)
        with open(os.path.join(folder, "breach_hydrograph.csv"), "w", encoding="utf-8") as f:
            f.write("time_hr,discharge_cfs\n0,0\n1,8420\n2,100\n")
        with open(os.path.join(folder, "gauge_valley.csv"), "w", encoding="utf-8") as f:
            f.write("time_hr,discharge_cfs\n0,0\n1,2100\n")
        with open(os.path.join(folder, "receptors_flooded.csv"), "w", encoding="utf-8") as f:
            f.write("id,kind,x,y,depth_ft,velocity_fps,arrival_hr\n")
            f.write("House north,structure,1,2,6.5,2.0,1.2\n")
            f.write("a<b & c,road,3,4,1.0,,\n")
        out = write_dashboard(folder)
        text = open(out, encoding="utf-8").read()
        assert "SAMPLE-ABS" in text
        assert "8420" in text
        assert "gauge_valley" not in text or "valley" in text
        assert "\\u003c" in text
        assert "<b &" not in text.split('type="application/json">', 1)[1].split("</script>", 1)[0]
        assert "CHECK THE DAM POINT" not in text.split('type="application/json">', 1)[0]
        print("ok", out, "bytes", len(text))
    finally:
        shutil.rmtree(folder)


if __name__ == "__main__":
    _self_check()
