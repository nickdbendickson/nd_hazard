"""Per-dam HTML reports, a statewide summary page and CSV.  Pure Python + numpy.

Everything is self-contained HTML (maps are embedded PNGs, charts inline SVG),
so the folder can be zipped, emailed or opened offline and printed to PDF.
"""
import base64
import csv
import html
import json
import os
import struct
import zlib

import numpy as np

from .proposed import PHRASE as PROPOSED_PHRASE
from .us_units import FT, ACRE_M2, CFS_M3S, convert

# ---- colours: sequential blue ramp for depth, fixed status colours for classes
DEPTH_RAMP = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
DEPTH_BREAKS_FT = [0.25, 1.0, 2.0, 3.0, 6.0, 10.0, 16.0]      # ft, lower edge of each colour (0.18.0)
DEPTH_BREAKS = [b * FT for b in DEPTH_BREAKS_FT]                # m, the rasters are metric
CLASS_STYLE = {"High": ("#d03b3b", "▲"), "Medium": ("#ec835a", "◆"),
               "Low": ("#0ca30c", "●"), "NoData": ("#898781", "○")}
FEATURE_STYLE = {"lethal": (208, 59, 59), "structure": (236, 131, 90), "road": (250, 178, 25),
                 "railroad": (74, 58, 167)}


def _hex(h):
    h = h.lstrip("#"); return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


# ------------------------------------------------------------------ PNG
def png_bytes(rgba):
    """Minimal PNG encoder (RGBA uint8 array)."""
    h, w, _ = rgba.shape
    raw = b"".join(b"\x00" + rgba[r].tobytes() for r in range(h))

    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def flood_map(depth, hill, features=(), breach_rc=None, max_px=900):
    """RGBA image: hillshade grey, depth in the blue ramp, features as ringed dots."""
    h, w = depth.shape
    k = max(1, int(np.ceil(max(h, w) / max_px)))
    d = depth[::k, ::k]; hs = hill[::k, ::k]
    g = (80 + 160 * hs).astype(np.uint8)
    img = np.stack([g, g, g, np.full(g.shape, 255, np.uint8)], axis=-1)
    for i, lo in enumerate(DEPTH_BREAKS):
        m = d >= lo
        r, gg, b = _hex(DEPTH_RAMP[i])
        img[m] = (r, gg, b, 255)

    def dot(rc, col, rad=4):
        r0, c0 = rc[0] // k, rc[1] // k
        for dr in range(-rad - 1, rad + 2):
            for dc in range(-rad - 1, rad + 2):
                rr, cc = r0 + dr, c0 + dc
                if 0 <= rr < img.shape[0] and 0 <= cc < img.shape[1]:
                    dd = dr * dr + dc * dc
                    if dd <= rad * rad:
                        img[rr, cc] = (*col, 255)
                    elif dd <= (rad + 1.5) ** 2:
                        img[rr, cc] = (252, 252, 251, 255)          # surface ring
    for f in features:
        dot((f[0], f[1]), FEATURE_STYLE.get(f[2], (82, 81, 78)), 3 if f[2] != "lethal" else 4)
    if breach_rc is not None:
        dot(breach_rc, (11, 11, 11), 5)
    return img


# ------------------------------------------------------------------ SVG hydrograph
def hydrograph_svg(t_s, q, title="Breach outflow"):
    """`q` in m3/s (as the steps hold it); the chart is drawn in cfs."""
    t = np.asarray(t_s, float) / 60.0; q = np.asarray(q, float) / CFS_M3S
    if len(t) > 400:
        idx = np.unique(np.concatenate((np.linspace(0, len(t) - 1, 400).astype(int), [int(q.argmax())])))
        t, q = t[idx], q[idx]
    W, H, L, R, T, B = 640, 260, 56, 16, 16, 40
    tx = max(t.max(), 1e-6); qy = _nice(max(q.max(), 1e-6))
    X = lambda v: L + (W - L - R) * v / tx
    Y = lambda v: T + (H - T - B) * (1 - v / qy)
    pts = " ".join(f"{X(a):.1f},{Y(b):.1f}" for a, b in zip(t, q))
    grid = "".join(f'<line x1="{L}" x2="{W - R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" class="grid"/>'
                   f'<text x="{L - 6}" y="{Y(v) + 4:.1f}" class="tick" text-anchor="end">{v:g}</text>'
                   for v in np.linspace(0, qy, 5))
    xt = _nice(tx) / 4
    xticks = "".join(f'<text x="{X(v):.1f}" y="{H - B + 16}" class="tick" text-anchor="middle">{v:g}</text>'
                     for v in np.arange(0, tx + 1e-9, xt) if v <= tx)
    data = json.dumps({"t": [round(float(a), 2) for a in t], "q": [round(float(b), 1) for b in q]})
    return f'''<figure class="chart"><figcaption>{html.escape(title)}</figcaption>
<svg viewBox="0 0 {W} {H}" role="img" aria-label="{html.escape(title)}: peak {q.max():,.0f} cfs" data-l="{L}" data-r="{R}" data-w="{W}">
{grid}<line x1="{L}" x2="{W - R}" y1="{Y(0):.1f}" y2="{Y(0):.1f}" class="axis"/>{xticks}
<text x="{(W + L) / 2}" y="{H - 4}" class="tick" text-anchor="middle">minutes after breach starts</text>
<text x="12" y="{T + (H - T - B) / 2}" class="tick" transform="rotate(-90 12 {T + (H - T - B) / 2})" text-anchor="middle">cfs</text>
<polyline points="{pts}" class="series"/>
<line class="xhair" y1="{T}" y2="{H - B}" x1="-10" x2="-10"/><circle class="xdot" r="4" cx="-10" cy="-10"/>
<rect x="{L}" y="{T}" width="{W - L - R}" height="{H - T - B}" fill="transparent" class="hit"/>
</svg><div class="tip" hidden></div><script type="application/json" class="hdata">{data}</script></figure>'''


def _nice(v):
    e = 10 ** np.floor(np.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if v <= m * e:
            return float(m * e)
    return float(10 * e)


# ------------------------------------------------------------------ review queue
def review_reasons(rec):
    r = []
    if rec.get("status_sim") and rec["status_sim"] not in ("ok", "cached", "triage", "noflood", "excluded"):
        r.append("simulation failed")
    if rec.get("hz_class") == "NoData":
        r.append("no classification")
    v = str(rec.get("hz_vs_existing") or "")
    if v.startswith("higher"): r.append("screened class HIGHER than the DWR class")
    if v.startswith("lower"): r.append("screened class lower than the DWR class")
    if str(rec.get("hz_conf")) in ("review", "low", "none"): r.append(f"confidence {rec.get('hz_conf')}")
    if str(rec.get("edge_flag")).upper() == "YES": r.append("flood truncated at terrain edge")
    if str(rec.get("dir_conf")) == "low": r.append("breach direction uncertain")
    if rec.get("class_stable") == "no": r.append("class changes between sensitivity runs")
    if rec.get("cascade"): r.append("overtops a downstream dam")
    if rec.get("audit") == "mismatch": r.append("triage audit disagreed with the 2D model")
    blob = " ".join(str(rec.get(k) or "") for k in (
        "stor_notes", "status", "brch_notes", "sim_notes", "hz_notes", "source"))
    if "the pool leaves the basin" in blob or "edge of the terrain clip" in blob:
        r.append("proposed dam: the pool leaves the basin below the design crest")
    est = estimated_inputs(rec)
    if est:
        r.append("ESTIMATED " + " and ".join(est) + " (no DWR value)")
    if "spill level" in blob:
        r.append("DEM storage held at the spill level (leak guard)")
    if "leaks at the pool level" in blob:
        r.append("DEM pool leaks at the pool level: storage above the pool is a prism estimate")
    return r


def estimated_inputs(rec):
    """0.19.0: which of height / storage were estimated (h_src / stor_src from tools 1 and 3)."""
    out = []
    if "estimated" in str(rec.get("h_src") or "").lower():
        out.append("height")
    if "estimated" in str(rec.get("stor_src") or "").lower():
        out.append("storage")
    return out


KIND_LABEL = {"dam": "DWR dam", "unknown structure": "DWR unknown structure", "dugout": "DWR dugout",
              "pond": "pond (LiDAR)", "proposed dam": "proposed dam"}


# ------------------------------------------------------------------ pages
CSS = """
:root{--surface:#fcfcfb;--page:#f9f9f7;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;
--grid:#e1e0d9;--axis:#c3c2b7;--series:#2a78d6;--ring:rgba(11,11,11,.10)}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--surface:#1a1a19;--page:#0d0d0d;
--ink:#fff;--ink2:#c3c2b7;--grid:#2c2c2a;--axis:#383835;--series:#3987e5;--ring:rgba(255,255,255,.10)}}
:root[data-theme="dark"]{--surface:#1a1a19;--page:#0d0d0d;--ink:#fff;--ink2:#c3c2b7;--grid:#2c2c2a;
--axis:#383835;--series:#3987e5;--ring:rgba(255,255,255,.10)}
*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--ink);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:24px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px}
.sub{color:var(--ink2);margin:0 0 16px}
.card{background:var(--surface);border:1px solid var(--ring);border-radius:10px;padding:16px;margin:12px 0}
.badge{display:inline-flex;gap:6px;align-items:center;font-weight:600;padding:2px 10px;border-radius:999px;
border:1px solid var(--ring);background:var(--surface)}
.badge i{font-style:normal}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;padding:6px 8px;
border-bottom:1px solid var(--grid);vertical-align:top}th{color:var(--ink2);font-weight:600}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:8px 16px}
.kv div span{display:block;color:var(--muted);font-size:12px}.kv div b{font-weight:600}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.tile{background:var(--surface);border:1px solid var(--ring);border-radius:10px;padding:12px 14px}
.tile span{color:var(--ink2);font-size:13px}.tile b{display:block;font-size:28px;font-weight:600}
img.map{width:100%;height:auto;border-radius:8px;display:block}
.legend{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:13px;color:var(--ink2);margin-top:8px}
.legend i{display:inline-block;width:14px;height:10px;border-radius:2px;vertical-align:middle;margin-right:4px}
.chart{margin:0;position:relative}.chart figcaption{font-weight:600;margin-bottom:4px}
.chart svg{width:100%;height:auto;display:block}
.grid{stroke:var(--grid);stroke-width:1}.axis{stroke:var(--axis);stroke-width:1}
.tick{fill:var(--muted);font-size:11px}.series{fill:none;stroke:var(--series);stroke-width:2;
stroke-linejoin:round}.xhair{stroke:var(--axis);stroke-width:1}.xdot{fill:var(--series);stroke:var(--surface);stroke-width:2}
.tip{position:absolute;pointer-events:none;background:var(--surface);border:1px solid var(--ring);
border-radius:8px;padding:6px 10px;font-size:13px;box-shadow:0 2px 8px rgba(0,0,0,.12)}
.tip b{display:block}
.warn{color:var(--ink2)}a{color:var(--series)}
input[type=search]{font:inherit;padding:6px 10px;border:1px solid var(--ring);border-radius:8px;
background:var(--surface);color:var(--ink);width:100%;max-width:360px}
.scroll{overflow-x:auto}
footer{color:var(--muted);font-size:12px;margin-top:32px}
.st-ok{color:#1a6b2a}.st-edge{color:#8a5a00;font-weight:600}.st-fail{color:#a01010;font-weight:600}
button{font:inherit;padding:4px 12px;border:1px solid var(--ring);border-radius:6px;
background:var(--surface);color:var(--ink);cursor:pointer;margin-bottom:8px}
"""

JS = """
document.querySelectorAll('figure.chart').forEach(function(f){
 var svg=f.querySelector('svg'),hit=f.querySelector('.hit'),tip=f.querySelector('.tip'),
 d=JSON.parse(f.querySelector('.hdata').textContent),xh=f.querySelector('.xhair'),xd=f.querySelector('.xdot'),
 pl=f.querySelector('polyline').getAttribute('points').split(' ').map(function(p){return p.split(',').map(Number)});
 function show(e){var r=svg.getBoundingClientRect(),vx=(e.clientX-r.left)*svg.viewBox.baseVal.width/r.width,
 best=0;for(var i=1;i<pl.length;i++){if(Math.abs(pl[i][0]-vx)<Math.abs(pl[best][0]-vx))best=i}
 xh.setAttribute('x1',pl[best][0]);xh.setAttribute('x2',pl[best][0]);xd.setAttribute('cx',pl[best][0]);
 xd.setAttribute('cy',pl[best][1]);tip.hidden=false;tip.textContent='';var b=document.createElement('b');
 b.textContent=Math.round(d.q[best]).toLocaleString()+' cfs';tip.appendChild(b);tip.appendChild(document.createTextNode(
 d.t[best].toFixed(1)+' min'));var px=pl[best][0]*r.width/svg.viewBox.baseVal.width;
 tip.style.left=Math.min(px+12,r.width-140)+'px';tip.style.top='24px'}
 hit.addEventListener('pointermove',show);hit.addEventListener('pointerleave',function(){tip.hidden=true;
 xh.setAttribute('x1',-10);xh.setAttribute('x2',-10);xd.setAttribute('cx',-10)})});
var q=document.getElementById('filter');if(q){q.addEventListener('input',function(){var v=q.value.toLowerCase();
document.querySelectorAll('tbody tr[data-s]').forEach(function(tr){tr.hidden=v&&tr.dataset.s.indexOf(v)<0})})}
document.querySelectorAll('th[data-k]').forEach(function(th,ci){th.style.cursor='pointer';th.addEventListener('click',function(){
var tb=th.closest('table').querySelector('tbody'),rows=[].slice.call(tb.rows),idx=[].indexOf.call(th.parentNode.children,th),
asc=th.dataset.o!=='a';th.dataset.o=asc?'a':'d';rows.sort(function(a,b){var x=a.cells[idx].dataset.v||a.cells[idx].textContent,
y=b.cells[idx].dataset.v||b.cells[idx].textContent,nx=parseFloat(x),ny=parseFloat(y);
var c=(!isNaN(nx)&&!isNaN(ny))?nx-ny:x.localeCompare(y);return asc?c:-c});rows.forEach(function(r){tb.appendChild(r)})})});
"""


def _conservative_note(rec):
    """Return True when the conservative (no-chart) lethality was used."""
    basis = str(rec.get("hz_basis") or "")
    return "Conservative, no chart" in basis


def class_display(rec):
    """Display label for hz_class; adds a conservative note when TM 11 chart was not signed off."""
    cls = rec.get("hz_class") or "NoData"
    if cls == "High" and _conservative_note(rec):
        return "High (conservative: TM 11 chart not signed off)"
    return cls


def badge(cls, label=None):
    col, icon = CLASS_STYLE.get(cls or "NoData", CLASS_STYLE["NoData"])
    display = label if label is not None else str(cls or "NoData")
    return (f'<span class="badge"><i style="color:{col}" aria-hidden="true">{icon}</i>'
            f'{html.escape(display)}</span>')


def _fmt(v, nd=1):
    if v is None or v == "":
        return "–"
    if isinstance(v, float):
        return f"{v:,.{nd}f}"
    return html.escape(str(v))


def _us(v, factor, nd=1):
    """A metric value shown in US units (blank stays blank). factor = metric units per US unit."""
    x = convert(v, factor)
    if x is None:
        return None
    return int(round(x)) if nd == 0 else round(x, nd)


def _page(title, body):
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html.escape(title)}</title><style>{CSS}</style></head><body><main>{body}'
            f'<footer>Screening-level result from the ND Dam Breach Screening plugin. The official hazard '
            f'classification is made by the ND Department of Water Resources.</footer></main>'
            f'<script>{JS}</script></body></html>')


def dam_page(rec, map_png=None, hydro=None, impacts=(), out_path=None):
    facts = [("Dam ID", rec.get("dam_id")),
             ("Structure kind", KIND_LABEL.get(str(rec.get("kind") or ""), rec.get("kind") or "DWR dam")),
             ("DWR class", rec.get("haz_existing")),
             ("Comparison", rec.get("hz_vs_existing")), ("Confidence", rec.get("hz_conf")),
             ("Height (ft)", _us(rec.get("height_m"), FT, 1)),
             ("Height from", rec.get("h_src") or "inventory"),
             ("Storage used (ac-ft)", rec.get("store_af")),
             ("Storage from", rec.get("stor_src") or rec.get("stor_method")),
             ("Breach peak (cfs)", _us(rec.get("qp_m3s"), CFS_M3S, 0)), ("Formation time (min)", rec.get("tf_min")),
             ("Engine", rec.get("engine")), ("Cell (ft)", _us(rec.get("cell_m"), FT, 0)),
             ("Simulated (h)", rec.get("sim_h")), ("Max depth (ft)", _us(rec.get("max_depth_m"), FT, 1)),
             ("Flooded area (ac)", _us(None if rec.get("area_ha") in (None, "") else rec.get("area_ha"), ACRE_M2 / 1e4, 0)),
             ("Structures wet / lethal",
             f'{rec.get("n_bldg_wet", "–")} / {rec.get("n_bldg_lethal", "–")}'),
             ("Roads / rail overtopped", f'{rec.get("n_road_wet", "–")} / {rec.get("n_rail_wet", "–")}'),
             ("Class stable (sensitivity)", rec.get("class_stable"))]
    kv = "".join(f"<div><span>{html.escape(k)}</span><b>{_fmt(v)}</b></div>" for k, v in facts)
    rev = review_reasons(rec)
    body = (f'<h1>{html.escape(str(rec.get("name") or rec.get("dam_id")))}</h1>'
            f'<p class="sub">{badge(rec.get("hz_class"), class_display(rec))} &nbsp; {html.escape(str(rec.get("hz_reason") or ""))}</p>')
    # item 3: prominent cut-off warning at the top of the report
    if str(rec.get("edge_flag") or "").upper() == "YES":
        body += ('<div class="card" style="border-color:#c88000;background:#fff8e6">'
                 '<b>⚠ Flood cut off at the terrain edge — results are incomplete.</b> '
                 'The flood was still deep when it reached the edge of the DEM clip. '
                 'Peak flow, flooded area and class may be understated. '
                 'Extend the DEM clip (tool 2) and rerun.</div>')
    if rev:
        body += '<div class="card"><b>Needs review:</b> ' + html.escape("; ".join(rev)) + '</div>'
    note_blob = " ".join(str(rec.get(k) or "") for k in (
        "stor_notes", "status", "source", "brch_notes", "sim_notes", "hz_notes"))
    if PROPOSED_PHRASE in note_blob or str(rec.get("source") or "") == "proposed":
        body += ('<div class="card"><b>Proposed dam.</b> ' + html.escape(PROPOSED_PHRASE) + '</div>')
    # item 5: explain the conservative class where the class is shown
    if rec.get("hz_class") == "High" and _conservative_note(rec):
        body += ('<div class="card warn"><b>High (conservative rule).</b> '
                 'The ACER TM 11 depth-velocity charts are not signed off for this QGIS, '
                 'so every inundated habitable structure is counted as lethal. '
                 'To use the chart method, open ND Dam Hazard Helper &rarr; Hazard Classification '
                 '&rarr; Dam Suite &rarr; Chart sign-off.</div>')
    est = estimated_inputs(rec)
    if est:                                  # 0.19.0
        body += ('<div class="card warn"><b>Estimated inputs.</b> DWR has no ' + " or ".join(est) +
                 ' for this structure: the value(s) used were ESTIMATED from the DEM (see the notes). '
                 'Treat the class as a screening estimate until the structure is measured.</div>')
    body += f'<div class="card kv">{kv}</div>'
    if map_png is not None:
        b64 = base64.b64encode(map_png).decode()
        leg = "".join(f'<span><i style="background:{c}"></i>{lo:g}{"–" + format(DEPTH_BREAKS_FT[i + 1], "g") if i + 1 < len(DEPTH_BREAKS_FT) else "+"} ft</span>'
                      for i, (c, lo) in enumerate(zip(DEPTH_RAMP, DEPTH_BREAKS_FT)))
        leg += "".join(f'<span><i style="background:rgb{FEATURE_STYLE[k]};border-radius:50%;width:10px"></i>{lab}</span>'
                       for k, lab in (("lethal", "structure, lethal"), ("structure", "structure, wet"),
                                      ("road", "road overtopped"), ("railroad", "railroad overtopped")))
        leg += '<span><i style="background:#0b0b0b;border-radius:50%;width:10px"></i>breach</span>'
        body += (f'<h2>Maximum flood depth</h2><div class="card"><img class="map" alt="Maximum flood depth map" '
                 f'src="data:image/png;base64,{b64}"><div class="legend">{leg}</div></div>')
    if hydro is not None:
        body += '<h2>Breach hydrograph</h2><div class="card">' + hydrograph_svg(*hydro) + '</div>'
    if impacts:
        rows = "".join(f'<tr><td>{html.escape(str(i.get("kind")))}</td><td class="num">{_fmt(_us(i.get("depth_m"), FT, 1), 1)}</td>'
                       f'<td class="num">{_fmt(_us(i.get("vel_ms"), FT, 1), 1)}</td>'
                       f'<td class="num">{_fmt(_us(i.get("dv_m2s"), FT * FT, 1), 1)}</td>'
                       f'<td>{html.escape(str(i.get("lethal", "")))}</td><td>{html.escape(str(i.get("level", "")))}</td></tr>'
                       for i in impacts)
        body += ('<h2>Impacted features</h2><div class="card scroll"><table><thead><tr><th>Kind</th>'
                 '<th class="num">Depth ft</th><th class="num">Velocity ft/s</th><th class="num">D×V ft²/s</th>'
                 f'<th>Lethal</th><th>Level</th></tr></thead><tbody>{rows}</tbody></table></div>')
    if rec.get("cascade"):
        body += f'<h2>Downstream dams</h2><div class="card">{html.escape(str(rec["cascade"]))}</div>'
    notes = "; ".join(str(rec.get(k)) for k in (
        "hz_notes", "sim_notes", "brch_notes", "stor_notes", "status", "source") if rec.get(k))
    if notes:
        body += f'<h2>Notes</h2><div class="card warn">{html.escape(notes)}</div>'
    page = _page(f"{rec.get('name') or rec.get('dam_id')} – breach screening", body)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(page)
    return page


SUMMARY_COLS = ["dam_id", "name", "hz_class", "haz_existing", "hz_vs_existing", "hz_conf",
                "class_stable", "n_bldg_wet", "n_bldg_lethal", "n_road_wet", "n_rail_wet",
                "store_af", "height_ft", "qp_cfs", "engine", "review",
                # 0.9: read by ND Dam Hazard's portfolio dashboard (Next action)
                "tri_result", "hz_border", "hz_gaps", "proposed",
                # 0.19.0: which DWR inventory, and whether height / storage were estimated
                "kind", "h_src", "stor_src",
                # 0.23.0: run status for the dashboard (item 12)
                "status_sim", "edge_flag"]


def _suite_note(out_dir):
    """0.9: where the whole suite's table is - ND Dam Hazard's portfolio dashboard reads this
    folder's summary.csv when the report folder is in (or directly inside) the folder of dam
    folders. The class here is a screening preview; ND Dam Hazard's class is the one."""
    ov = os.path.join(out_dir, os.pardir, "Dam_Portfolio.html")
    link = ('<a href="../Dam_Portfolio.html">Dam_Portfolio.html</a>' if os.path.exists(ov)
            else "Dam_Portfolio.html")
    return ('<p class="sub">All dams in one table with the breach model and the hazard '
            'classification: ND Dam Hazard\'s portfolio dashboard ({}) reads this folder\'s '
            'summary.csv - keep this report folder in, or directly inside, the folder of dam '
            'folders (e.g. "_screening").</p>'.format(link))


def summary(records, out_dir, title="ND dam breach screening – statewide summary"):
    """Writes index.html and summary.csv; returns their paths."""
    os.makedirs(out_dir, exist_ok=True)
    for r in records:
        r["review"] = "; ".join(review_reasons(r))
        blob = " ".join(str(r.get(k) or "") for k in (
            "stor_notes", "status", "source", "brch_notes", "sim_notes", "hz_notes"))
        r["proposed"] = "yes" if (PROPOSED_PHRASE in blob or str(r.get("source") or "") == "proposed") else ""
        r["height_ft"] = _us(r.get("height_m"), FT, 1)         # 0.18.0: US units only in the CSV
        r["qp_cfs"] = _us(r.get("qp_m3s"), CFS_M3S, 0)
    csvp = os.path.join(out_dir, "summary.csv")
    with open(csvp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(SUMMARY_COLS)
        for r in records:
            w.writerow(["" if r.get(k) is None else r.get(k) for k in SUMMARY_COLS])
    n = len(records)
    cnt = {k: sum(1 for r in records if r.get("hz_class") == k) for k in ("High", "Medium", "Low", "NoData")}
    higher = sum(1 for r in records if str(r.get("hz_vs_existing") or "").startswith("higher"))
    rev = [r for r in records if r["review"]]
    tiles = (f'<div class="tiles"><div class="tile"><span>Dams screened</span><b>{n:,}</b></div>'
             + "".join(f'<div class="tile"><span>{badge(k)}</span><b>{cnt[k]:,}</b></div>' for k in ("High", "Medium", "Low", "NoData"))
             + f'<div class="tile"><span>Higher than DWR class</span><b>{higher:,}</b></div>'
             f'<div class="tile"><span>Needs review</span><b>{len(rev):,}</b></div></div>')
    n_prop = sum(1 for r in records if r.get("proposed") == "yes")
    if n_prop:
        tiles += ('<div class="card"><b>Proposed dam.</b> {} — {} of {}.</div>'.format(
            html.escape(PROPOSED_PHRASE), n_prop, n))

    def _run_status(r):
        """item 12: done / cut off / failed / —."""
        sim = str(r.get("status_sim") or "")
        if sim and sim not in ("ok", "cached", "triage", "noflood", "excluded"):
            return "failed", "st-fail"
        if str(r.get("edge_flag") or "").upper() == "YES":
            return "cut off", "st-edge"
        if sim in ("ok", "cached", "triage", "noflood", "excluded"):
            return "done", "st-ok"
        return "—", ""

    def row(r):
        nm = html.escape(str(r.get("name") or r["dam_id"]))
        page = os.path.join(out_dir, "dams", f'{r["dam_id"]}.html')
        link = f'<a href="dams/{html.escape(str(r["dam_id"]))}.html">{nm}</a>' if os.path.exists(page) else nm
        s = " ".join(str(r.get(k) or "") for k in ("dam_id", "name", "hz_class", "haz_existing", "review")).lower()
        rk = {"High": 2, "Medium": 1, "Low": 0}.get(r.get("hz_class"), -1)
        st_lbl, st_css = _run_status(r)
        qp = _fmt(r.get("qp_cfs"), "{:,.0f}") if r.get("qp_cfs") else "—"
        return (f'<tr data-s="{html.escape(s)}"><td>{html.escape(str(r["dam_id"]))}</td><td>{link}</td>'
                f'<td class="{st_css}">{st_lbl}</td>'
                f'<td data-v="{rk}">{badge(r.get("hz_class"), class_display(r))}</td><td>{_fmt(r.get("haz_existing"))}</td>'
                f'<td>{_fmt(r.get("hz_vs_existing"))}</td><td class="num">{qp}</td>'
                f'<td class="num">{_fmt(r.get("n_bldg_wet"))}</td>'
                f'<td class="num">{_fmt(r.get("n_bldg_lethal"))}</td><td class="num">{_fmt(r.get("store_af"), 0)}</td>'
                f'<td>{html.escape(r["review"])}</td></tr>')
    head = ('<thead><tr><th data-k>ID</th><th data-k>Dam</th><th data-k>Status</th>'
            '<th data-k>Screened</th><th data-k>DWR</th>'
            '<th data-k>Comparison</th><th class="num" data-k>Peak (cfs)</th>'
            '<th class="num" data-k>Bldg wet</th><th class="num" data-k>Lethal</th>'
            '<th class="num" data-k>ac-ft</th><th data-k>Review</th></tr></thead>')
    # item 12: IDs of failed/cut-off dams for "rerun" copy button
    rerun_ids = [r["dam_id"] for r in records if _run_status(r)[0] in ("failed", "cut off")]
    rerun_btn = ""
    if rerun_ids:
        ids_js = ", ".join(f'"{html.escape(str(i))}"' for i in rerun_ids)
        rerun_btn = (f'<button onclick="navigator.clipboard.writeText([{ids_js}].join(\',\'))'
                     f'.then(()=>this.textContent=\'Copied!\').catch(()=>0)">'
                     f'Copy {len(rerun_ids)} dam ID{"s" if len(rerun_ids)!=1 else ""} to rerun</button> ')
    body = (f'<h1>{html.escape(title)}</h1><p class="sub">Screening results for {n:,} dams. '
            f'Click a column to sort; the CSV has the same table (<a href="summary.csv">summary.csv</a>).</p>'
            + _suite_note(out_dir) + f'{tiles}'
            f'<h2>Review queue ({len(rev):,})</h2>{rerun_btn}'
            f'<div class="card scroll"><table>{head}<tbody>'
            + "".join(row(r) for r in sorted(rev, key=lambda r: -{"High": 2, "Medium": 1, "Low": 0}.get(r.get("hz_class"), -1)))
            + f'</tbody></table></div><h2>All dams</h2><input id="filter" type="search" placeholder="Filter by ID, name, class…" aria-label="Filter dams">'
            f'<div class="card scroll"><table>{head}<tbody>' + "".join(row(r) for r in records) + '</tbody></table></div>')
    idx = os.path.join(out_dir, "index.html")
    with open(idx, "w", encoding="utf-8") as f:
        f.write(_page("Dam breach screening summary", body))
    return idx, csvp
