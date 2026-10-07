# -*- coding: utf-8 -*-
"""v2.5: EAP travel-time table along the channel.

River mile 0 is at the dam (the NID point of the dam being classified,
else the upstream end of the centerline). Stations every
TRAVEL_STATION_MI along the channel give the earliest arrival, the peak
depth and - when a time-of-peak raster exists (HEC-RAS plan import,
DSS-WISE "time of peak") - the time of the peak, read from the flooded
cells within TRAVEL_SEARCH_FT of the station. Towns (OSM places), bridges /
culverts / crossings, closed roads and other dams are placed at their
river mile with their own first arrival.

Centerline: the engineer's line layer (parts are joined end to end; the
longest chain is used), else USGS NHD flowlines traced downstream from the
dam (or from the first cell the flood reaches) through connected lines,
preferring the same stream name.

The geometry core (functions on lists of (x, y) tuples) needs no QGIS.
"""

import math

# ----------------------------------------------------------------- geometry --


def line_length(pts):
    return sum(math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
               for i in range(len(pts) - 1))


def point_at(pts, dist):
    """(x, y) at `dist` along the polyline (clamped to its ends)."""
    if not pts:
        return None
    if dist <= 0:
        return tuple(pts[0])
    run = 0.0
    for i in range(len(pts) - 1):
        (x0, y0), (x1, y1) = pts[i], pts[i + 1]
        seg = math.hypot(x1 - x0, y1 - y0)
        if seg > 0 and run + seg >= dist:
            t = (dist - run) / seg
            return x0 + t * (x1 - x0), y0 + t * (y1 - y0)
        run += seg
    return tuple(pts[-1])


def project(pts, x, y):
    """(chainage, distance, (px, py)) of the closest point of the polyline."""
    best = (0.0, float("inf"), tuple(pts[0]) if pts else (x, y))
    run = 0.0
    for i in range(len(pts) - 1):
        (x0, y0), (x1, y1) = pts[i], pts[i + 1]
        dx, dy = x1 - x0, y1 - y0
        seg2 = dx * dx + dy * dy
        t = 0.0 if seg2 == 0 else max(0.0, min(1.0, ((x - x0) * dx + (y - y0) * dy) / seg2))
        px, py = x0 + t * dx, y0 + t * dy
        d = math.hypot(x - px, y - py)
        if d < best[1]:
            best = (run + t * math.sqrt(seg2), d, (px, py))
        run += math.sqrt(seg2)
    return best


def cut(pts, start, end=None):
    """Part of the polyline between chainages start and end."""
    total = line_length(pts)
    end = total if end is None else min(end, total)
    start = max(0.0, start)
    if end <= start or len(pts) < 2:
        return []
    out = [point_at(pts, start)]
    run = 0.0
    for i in range(len(pts) - 1):
        seg = math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
        nxt = run + seg
        if start < nxt < end:
            out.append(tuple(pts[i + 1]))
        run = nxt
    out.append(point_at(pts, end))
    return out


def _d(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def join_parts(parts, tol):
    """Join polylines whose ends touch (within tol; parts are reversed as
    needed) and return the longest resulting chain."""
    pool = [list(p) for p in parts if p and len(p) >= 2]
    chains = []
    while pool:
        chain = pool.pop(0)
        grown = True
        while grown:
            grown = False
            for i, p in enumerate(pool):
                if _d(chain[-1], p[0]) <= tol:
                    chain += p[1:]
                elif _d(chain[-1], p[-1]) <= tol:
                    chain += p[::-1][1:]
                elif _d(chain[0], p[-1]) <= tol:
                    chain = p[:-1] + chain
                elif _d(chain[0], p[0]) <= tol:
                    chain = p[::-1][:-1] + chain
                else:
                    continue
                pool.pop(i)
                grown = True
                break
        chains.append(chain)
    return max(chains, key=line_length) if chains else []


_FTYPE_RANK = {460: 0, 558: 0, 334: 1, 336: 2}     # stream/river, artificial path, connector, canal


def _ftype(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        s = str(v or "").lower()
        return 460 if "stream" in s else 558 if "artificial" in s else 334 \
            if "connector" in s else 336 if "canal" in s else 999


def trace(lines, start, tol, max_len, search):
    """Downstream path through flowlines digitised in flow direction (NHD).
    lines: [{"pts": [(x, y)...], "name": str, "ftype": code}]. Starts on the
    line closest to `start` (within `search`), at the point closest to it.
    Returns (pts, [names], note)."""
    best = None
    for k, ln in enumerate(lines):
        if len(ln["pts"]) < 2:
            continue
        ch, dist, _p = project(ln["pts"], start[0], start[1])
        rank = _FTYPE_RANK.get(_ftype(ln.get("ftype")), 3)
        key = (dist > search, rank if dist <= search else 0, dist)
        if best is None or key < best[0]:
            best = (key, k, ch, dist)
    if best is None or best[3] > search:
        return [], [], "no flowline within {:.0f} units of the start point".format(search)
    _key, k, ch, _dist = best
    first = lines[k]
    path = cut(first["pts"], ch)
    names = [first.get("name") or ""]
    visited = {k}
    starts = {}
    for j, ln in enumerate(lines):
        if len(ln["pts"]) >= 2:
            starts.setdefault((round(ln["pts"][0][0] / tol), round(ln["pts"][0][1] / tol)),
                              []).append(j)
    cur = first
    note = ""
    length = line_length(path)                 # running length (no re-summing per step)
    while length < max_len:
        end = path[-1]
        ki, kj = round(end[0] / tol), round(end[1] / tol)
        nxt = []
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for j in starts.get((ki + di, kj + dj), ()):
                    if j not in visited and _d(lines[j]["pts"][0], end) <= tol:
                        nxt.append(j)
        if not nxt:
            break
        cname = (cur.get("name") or "").strip().lower()
        nxt.sort(key=lambda j: (
            0 if cname and (lines[j].get("name") or "").strip().lower() == cname else 1,
            _FTYPE_RANK.get(_ftype(lines[j].get("ftype")), 3),
            -line_length(lines[j]["pts"])))
        j = nxt[0]
        visited.add(j)
        path += [tuple(p) for p in lines[j]["pts"][1:]]
        length += line_length(lines[j]["pts"])
        cur = lines[j]
        if (cur.get("name") or "") and (cur.get("name") or "") != names[-1]:
            names.append(cur["name"])
    if length >= max_len:
        path = cut(path, 0.0, max_len)
        note = "traced channel cut at the maximum length"
    return path, [n for n in names if n], note


def stations(total, spacing):
    """Chainages 0, spacing, ... and the end."""
    if total <= 0 or spacing <= 0:
        return [0.0]
    n = int(math.floor(total / spacing + 1e-9))
    out = [i * spacing for i in range(n + 1)]
    if total - out[-1] > spacing * 0.25:
        out.append(total)
    return out


def interpolate_arrival(rows, chainage):
    """Arrival at a chainage from the station rows (linear between the
    nearest stations that have one)."""
    known = [(r["ch"], r["arrival"]) for r in rows
             if r.get("kind") == "Station" and r.get("arrival") is not None]
    if not known:
        return None
    known.sort()
    if chainage <= known[0][0]:
        return known[0][1]
    for (c0, a0), (c1, a1) in zip(known, known[1:]):
        if c0 <= chainage <= c1:
            return a0 if c1 == c0 else a0 + (a1 - a0) * (chainage - c0) / (c1 - c0)
    return known[-1][1]


def hhmm(hours):
    if hours is None:
        return ""
    m = int(round(float(hours) * 60.0))
    return "{}:{:02d}".format(m // 60, m % 60)


# ------------------------------------------------------------------- QGIS --
def _polylines(geom):
    """[[(x, y)...]] of a line geometry (single or multi part)."""
    try:
        if geom.isMultipart():
            return [[(p.x(), p.y()) for p in part] for part in geom.asMultiPolyline()]
        return [[(p.x(), p.y()) for p in geom.asPolyline()]]
    except Exception:
        return []


def lines_from_layer(layer, analysis_crs, transform_ctx, clip=None):
    from qgis.core import QgsCoordinateTransform, QgsFeatureRequest
    from .helpers import transform_geometry
    if layer is None or not layer.isValid():
        return []
    names = [f.name().lower() for f in layer.fields()]
    i_name = next((names.index(n) for n in ("gnis_name", "name", "stream", "river")
                   if n in names), -1)
    i_ft = names.index("ftype") if "ftype" in names else -1
    xf = QgsCoordinateTransform(layer.crs(), analysis_crs, transform_ctx)
    req = QgsFeatureRequest()
    if clip is not None:
        r = clip.request_rect(layer.crs())
        if r is not None:
            req.setFilterRect(r)
    out = []
    for feat in layer.getFeatures(req):
        g = transform_geometry(feat.geometry(), xf)
        if g is None or g.isNull() or g.isEmpty():
            continue
        a = feat.attributes()
        nm = a[i_name] if i_name >= 0 else ""
        ft = a[i_ft] if i_ft >= 0 else None
        for pts in _polylines(g):
            if len(pts) >= 2:
                out.append({"pts": pts, "name": "" if nm is None or str(nm) == "NULL"
                            else str(nm), "ftype": ft})
    return out


def first_arrival_cell(arr_sampler, depth_sampler=None, rows_per_block=512):
    """(x, y) in the arrival raster CRS of the earliest cell with an arrival
    time > 0 (the first cell the breach flood reaches; a reservoir stored as
    arrival 0 is skipped). None if there is none."""
    import numpy as np
    s = arr_sampler
    if s is None or not s.ok:
        return None
    best = None
    from .helpers import strip_rows
    rows_per_block = strip_rows(s.xsize, s.ysize, rows_per_block)   # v2.12: cell budget
    for y0 in range(0, s.ysize, rows_per_block):
        h = min(rows_per_block, s.ysize - y0)
        arr = s._read(0, y0, s.xsize, h)
        if arr is None:
            continue
        m = s._valid_mask(arr)
        vals = arr * s.scale + s.offset
        with np.errstate(invalid="ignore"):
            m &= vals > 1e-6
        if not m.any():
            continue
        v = np.where(m, vals, np.inf)
        i = int(np.argmin(v))
        r, c = divmod(i, s.xsize)
        if best is None or v.flat[i] < best[0]:
            best = (float(v.flat[i]), y0 + r, c)
    if best is None:
        return None
    xs, ys = s.cell_centres(np.array([best[2]]), np.array([best[1]]))
    return float(xs[0]), float(ys[0])


def _open(path):
    from qgis.core import QgsRasterLayer
    from .helpers import RasterSampler
    if not path:
        return None
    lyr = QgsRasterLayer(path, "peak")
    return RasterSampler(lyr) if lyr.isValid() else None


def _xform(src_crs, sampler, transform_ctx):
    """Transform src_crs -> the sampler's CRS, or None when not needed."""
    from qgis.core import QgsCoordinateTransform
    if sampler is None or not sampler.ok or sampler.crs is None:
        return None
    t = QgsCoordinateTransform(src_crs, sampler.crs, transform_ctx)
    return None if t.isShortCircuited() else t


def build(depth_sampler, arr_sampler, peak_path, analysis_crs, transform_ctx, lat, *,
          centerline_layer=None, flowline_layer=None, dam_point=None, places=None,
          facilities=None, fac_eval=None, roads=None, structs=None, spacing_mi=0.5,
          search_ft=150.0, max_mi=200.0, snap_ft=3.0, clip=None):
    """{"rows": [...], "line": [(x, y)...], "source": str, "notes": [...]} or
    None when no centerline could be made. Rows: kind, name, ch (CRS
    units), rm (river miles), offset_ft, arrival, depth, peak, n_struct, x, y."""
    import numpy as np
    from qgis.core import QgsCoordinateTransform, QgsGeometry, QgsPointXY
    from .structures import _ft_units
    from .helpers import transform_xy
    notes = []
    unit = _ft_units(1.0, analysis_crs, lat)            # CRS units per foot
    if unit <= 0:
        return None
    to_arr = None
    if arr_sampler is not None and arr_sampler.ok and arr_sampler.crs is not None:
        t = QgsCoordinateTransform(analysis_crs, arr_sampler.crs, transform_ctx)
        to_arr = None if t.isShortCircuited() else t
    # ---- centerline ----
    pts, source = [], ""
    if centerline_layer is not None:
        parts = [ln["pts"] for ln in lines_from_layer(centerline_layer, analysis_crs,
                                                       transform_ctx, clip)]
        pts = join_parts(parts, snap_ft * unit * 10)
        source = "centerline layer '{}'".format(centerline_layer.name())
        if len(parts) > 1:
            notes.append("{} centerline parts joined; the longest chain is used".format(
                len(parts)))
    elif flowline_layer is not None:
        lines = lines_from_layer(flowline_layer, analysis_crs, transform_ctx, clip)
        start = dam_point
        start_note = "the dam (NID point)"
        if start is None and arr_sampler is not None:
            c = first_arrival_cell(arr_sampler)
            if c is not None:
                if to_arr is not None:
                    back = QgsCoordinateTransform(arr_sampler.crs, analysis_crs, transform_ctx)
                    p = back.transform(QgsPointXY(c[0], c[1]))
                    c = (p.x(), p.y())
                start, start_note = c, "the first cell the flood reaches"
        if start is None:
            return {"rows": [], "line": [], "source": "", "notes": [
                "no dam point and no arrival raster - the NHD flowlines cannot be traced; "
                "give a centerline layer"]}
        pts, names, note = trace(lines, start, snap_ft * unit, max_mi * 5280.0 * unit,
                                 2640.0 * unit)
        if note:
            notes.append(note)
        source = "USGS NHD flowlines traced downstream from {}{}".format(
            start_note, " ({})".format(" > ".join(names[:4])) if names else "")
    if len(pts) < 2:
        return {"rows": [], "line": [], "source": source,
                "notes": notes + ["no usable centerline"]}
    peak = _open(peak_path)
    to_peak = _xform(analysis_crs, peak, transform_ctx)
    radius = search_ft * unit

    def values(x, y):
        g = QgsGeometry.fromPointXY(QgsPointXY(x, y)).buffer(radius, 8)
        xs, ys, d = depth_sampler.cells_in_geometry(g)
        if d.size == 0:
            d = depth_sampler.values_at([x], [y])
            xs, ys = np.array([x]), np.array([y])
        wet = np.isfinite(d) & (d > 0)
        if not wet.any():
            return None, None, None
        xs, ys, d = xs[wet], ys[wet], d[wet]
        i = int(np.argmax(d))
        arr = pk = None
        if arr_sampler is not None and arr_sampler.ok:
            ax, ay = transform_xy(xs, ys, to_arr)
            a = arr_sampler.values_at(ax, ay)
            a = a[np.isfinite(a) & (a >= 0)]
            arr = float(a.min()) if a.size else None
        if peak is not None and peak.ok:
            px, py = transform_xy([xs[i]], [ys[i]], to_peak)
            pv = peak.values_at(px, py)
            pk = float(pv[0]) if np.isfinite(pv[0]) else None
        return float(d[i]), arr, pk

    # orientation and river mile 0: at the dam when its NID point is near the
    # line, else from the arrival times at the two ends
    total = line_length(pts)
    ch0 = dist0 = None
    if dam_point is not None:
        ch0, dist0, _p = project(pts, dam_point[0], dam_point[1])
    if dist0 is not None and dist0 <= 2640.0 * unit:
        # downstream = the side where the flood arrives later (the
        # reservoir has arrival 0 or none); without arrivals, the longer
        # side of a traced line / the side away from the nearer end
        mile = 5280.0 * unit
        a_up = values(*point_at(pts, max(0.0, ch0 - mile)))[1] if ch0 > 0 else None
        a_dn = values(*point_at(pts, min(total, ch0 + mile)))[1] if ch0 < total else None
        if a_up is not None and a_dn is not None:
            flip = a_up > a_dn
        elif a_up is not None or a_dn is not None:
            flip = a_dn is None
        else:
            flip = ch0 > total / 2.0
        if flip:
            pts = pts[::-1]
            ch0 = total - ch0
        pts = cut(pts, ch0)
        notes.append("river mile 0 at the dam (NID point, {:.0f} ft from the "
                     "line)".format(dist0 / unit))
    elif arr_sampler is not None:
        a0 = values(*pts[0])[1]
        a1 = values(*pts[-1])[1]
        if a0 is not None and a1 is not None and a1 < a0:
            pts = pts[::-1]
            notes.append("line reversed so that river mile 0 is upstream (earlier arrival)")
    total = line_length(pts)
    rows = []
    for ch in stations(total, spacing_mi * 5280.0 * unit):
        x, y = point_at(pts, ch)
        d, a, pk = values(x, y)
        rows.append({"kind": "Station", "name": "RM {:.1f}".format(ch / unit / 5280.0),
                     "ch": ch, "offset_ft": 0.0, "arrival": a, "depth": d, "peak": pk,
                     "n_struct": None, "x": x, "y": y})
    # the channel beyond the flood: keep one dry station after the last wet one
    wet_idx = [i for i, r in enumerate(rows) if r["depth"] is not None]
    if wet_idx and wet_idx[-1] + 2 < len(rows):
        end_ch = rows[wet_idx[-1] + 1]["ch"]
        rows = rows[:wet_idx[-1] + 2]
        pts = cut(pts, 0.0, end_ch)
        notes.append("channel beyond the flooded area left out")

    def place(kind, name, x, y, arrival=None, depth=None, n_struct=None, extra=""):
        ch, dist, _p = project(pts, x, y)
        rows.append({"kind": kind, "name": name, "ch": ch, "offset_ft": dist / unit,
                     "arrival": arrival, "depth": depth, "peak": None, "n_struct": n_struct,
                     "x": x, "y": y, "extra": extra})

    # towns: first flooded structure within the place radius
    from .constants import OSM_PLACE_RADIUS_FT
    wet_structs = []
    for s in structs or []:
        if s.get("outcome") in (None, "Not inundated") or s.get("habitable") == "No":
            continue
        try:
            p = s["geom"].pointOnSurface().asPoint()
            wet_structs.append((p.x(), p.y(), s.get("arrival")))
        except Exception:
            continue
    for pl in places or []:
        r = OSM_PLACE_RADIUS_FT.get(pl.get("place"), 2640.0) * unit
        near = [(ax, ay, a) for ax, ay, a in wet_structs
                if math.hypot(ax - pl["x"], ay - pl["y"]) <= r]
        ch, dist, _p = project(pts, pl["x"], pl["y"])
        if not near and dist > r:
            continue
        arrs = [a for _x, _y, a in near if a is not None]
        place("Town", "{} ({})".format(pl.get("name") or "unnamed", pl.get("place")),
              pl["x"], pl["y"], arrival=min(arrs) if arrs else None, n_struct=len(near),
              extra="first flooded structure within {:.1f} mi".format(r / unit / 5280.0))
    from . import facilities as FAC
    for rec in facilities or []:
        if rec.get("kind") not in (FAC.KIND_CROSSING, FAC.KIND_DAM) or rec.get("subject"):
            continue
        e = (fac_eval or {}).get(rec["id"]) or {}
        if e.get("depth") is None:
            continue
        try:
            p = rec["geom"].asPoint()
        except Exception:
            continue
        kind = "Dam" if rec["kind"] == FAC.KIND_DAM else "Bridge / crossing"
        place(kind, rec.get("name") or rec.get("category") or kind, p.x(), p.y(),
              arrival=e.get("arrival"), depth=e.get("depth"),
              extra=rec.get("category") or "")
    for rd in roads or []:
        g = rd.get("geom")
        if g is None:
            continue
        try:
            p = g.interpolate(g.length() / 2.0).asPoint()
        except Exception:
            continue
        place("Road closure", rd.get("name") or "unnamed road", p.x(), p.y(),
              arrival=rd.get("arrival"), depth=rd.get("max_depth"),
              extra=rd.get("road_class") or "")
    for r in rows:
        r["rm"] = r["ch"] / unit / 5280.0
        if r["kind"] != "Station" and r.get("arrival") is None:
            r["arrival_channel"] = interpolate_arrival(rows, r["ch"])
    rows.sort(key=lambda r: (r["ch"], 0 if r["kind"] == "Station" else 1))
    if peak is not None:
        peak.close()
    return {"rows": rows, "line": pts, "source": source, "notes": notes}


FIELDS = (("Order", "int"), ("River_Mile", "dbl"), ("Item", "str"), ("Name", "str"),
          ("Offset_ft", "dbl"), ("Arrival_hr", "dbl"), ("Arrival_hhmm", "str"),
          ("Peak_Depth_ft", "dbl"), ("Time_Of_Peak_hr", "dbl"), ("Flooded_Structures", "int"),
          ("Notes", "str"))


def to_layers(result, crs, name="EAP_Travel_Time"):
    """(points layer, centerline layer)."""
    from qgis.core import QgsFeature, QgsField, QgsGeometry, QgsPointXY
    from .compat import FT_INT, FT_DOUBLE, FT_STRING, NULL, WKB_POINT, WKB_LINESTRING
    from .helpers import create_memory_layer
    ft = {"int": FT_INT, "dbl": FT_DOUBLE, "str": FT_STRING}
    pl = create_memory_layer(name, WKB_POINT, crs, [QgsField(n, ft[t]) for n, t in FIELDS])

    def nz(v, n=2):
        return NULL if v is None else round(float(v), n)
    feats = []
    for i, r in enumerate(result["rows"]):
        f = QgsFeature(pl.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(r["x"], r["y"])))
        arr = r.get("arrival")
        note = r.get("extra") or ""
        if arr is None and r.get("arrival_channel") is not None:
            note = (note + "; " if note else "") + "channel arrival {} h:min".format(
                hhmm(r["arrival_channel"]))
        f.setAttributes([i + 1, round(r["rm"], 2), r["kind"], r["name"] or NULL,
                         round(r["offset_ft"], 0), nz(arr), hhmm(arr) or NULL,
                         nz(r.get("depth")), nz(r.get("peak")),
                         NULL if r.get("n_struct") is None else int(r["n_struct"]),
                         note or NULL])
        feats.append(f)
    pl.dataProvider().addFeatures(feats)
    pl.updateExtents()
    ll = create_memory_layer("EAP_Channel_Centerline", WKB_LINESTRING, crs,
                             [QgsField("Source", FT_STRING), QgsField("Length_mi", FT_DOUBLE)])
    f = QgsFeature(ll.fields())
    f.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(x, y) for x, y in result["line"]]))
    f.setAttributes([result.get("source") or "", NULL])
    ll.dataProvider().addFeatures([f])
    ll.updateExtents()
    return pl, ll


def summary_lines(result, layer_name="EAP_Travel_Time"):
    rows = result.get("rows") or []
    st = [r for r in rows if r["kind"] == "Station"]
    out = []
    if not st:
        return ["Travel-time table not made: " + "; ".join(result.get("notes") or [
            "no centerline"])]
    wet = [r for r in st if r.get("depth") is not None]
    out.append("{} stations along {:.1f} river miles ({}); flood on the channel to river "
               "mile {:.1f} - EAP/{}.csv / .xlsx".format(
                   len(st), st[-1]["rm"], result.get("source") or "centerline",
                   wet[-1]["rm"] if wet else 0.0, layer_name))
    key = [r for r in rows if r["kind"] in ("Town", "Bridge / crossing", "Dam")]
    key.sort(key=lambda r: r["rm"])
    if key:
        out.append("First arrival: " + "; ".join(
            "{} RM {:.1f} {}".format(r["name"], r["rm"], hhmm(
                r.get("arrival") if r.get("arrival") is not None else
                r.get("arrival_channel")) or "-") for r in key[:8]) +
            ("" if len(key) <= 8 else " and {} more".format(len(key) - 8)))
    for n in result.get("notes") or []:
        out.append("Note: " + n)
    return out
