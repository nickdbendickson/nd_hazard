"""Vector products, the EAP inundation map, the hydrograph plot and the HTML
report. Pure numpy and text: GeoJSON and SVG need no GIS library; a
GeoPackage is written too when GDAL's Python bindings are there (they are, in
QGIS).

ND REG_05.2026 5.4 asks for inundation mapping, and section 15 for an
inundation map in the EAP. v3.2.0: the EAP map is ND Dam Hazard's map book (a PDF on
aerial imagery, <dam folder>/outputs/EAP/EAP_Map_Book.pdf). This tool's map - flood depth
classes, arrival-time lines, the dam, the receptors that flood, a scale bar and a north
arrow - is a preview, written as `EAP Inundation Map (preview).svg` (up to 3.1.x
`EAP Inundation Map.svg`, still read by the Word report) and by default only when ND Dam
Hazard is not installed (setting "EAP map": EAP_CHOICES).
"""
import html
import json
import math
import os

import numpy as np

from . import conditioning as cond
from .units import FT, MILE, CFS

# v3.2.0: one EAP map - ND Dam Hazard's map book; this tool's SVG is a preview
EAP_PREVIEW_NAME = "EAP Inundation Map (preview).svg"
EAP_OLD_NAME = "EAP Inundation Map.svg"          # written up to 3.1.x; still read
EAP_NAMES = (EAP_PREVIEW_NAME, EAP_OLD_NAME)
EAP_AUTO, EAP_MAPBOOK, EAP_SVG = "auto", "mapbook", "svg"
EAP_CHOICES = (EAP_AUTO, EAP_MAPBOOK, EAP_SVG)
EAP_LABELS = {EAP_AUTO: "Automatic: draw this tool's SVG preview only when ND Dam Hazard is "
                        "not installed",
              EAP_MAPBOOK: "Leave to ND Dam Hazard's map book (recommended)",
              EAP_SVG: "Also draw this tool's SVG preview"}
MAPBOOK_NOTE = ("the EAP map is ND Dam Hazard's map book (outputs/EAP/EAP_Map_Book.pdf, on "
                "aerial imagery); this tool's SVG preview was not drawn (setting 'EAP map')")
_UNSET = object()


def eap_preview_wanted(choice=EAP_AUTO, ndhazard_version=_UNSET):
    """Draw this tool's SVG preview? "svg" yes, "mapbook" no, "auto" only when ND Dam Hazard
    is not installed (damsuite.versions.installed("ndhazard") is None; outside QGIS it is
    always None). `ndhazard_version` replaces the lookup (tests)."""
    c = str(choice or EAP_AUTO).strip().lower()
    if c == EAP_SVG:
        return True
    if c == EAP_MAPBOOK:
        return False
    if ndhazard_version is _UNSET:
        try:
            from ..damsuite import versions
            ndhazard_version = versions.installed("ndhazard")
        except Exception:
            ndhazard_version = None
    return ndhazard_version is None


DEPTH_CLASSES_FT = (0.0, 1.0, 3.0, 6.0, 10.0)      # lower bounds of the map classes
DEPTH_COLORS = ("#bfe3f5", "#7fc4ea", "#3f92d6", "#2a55b5", "#5a1a8c")
DV_LIMIT = 7.0


# --------------------------------------------------------------------------------------
# mask -> polygons
# --------------------------------------------------------------------------------------
def _trace_rings(mask):
    """Boundary rings of the True cells, as integer (row, col) corner lists.
    Edges are directed with the True cell on the right, so outer rings run
    clockwise (in image coordinates) and holes counter-clockwise."""
    m = np.pad(mask, 1, constant_values=False)
    ny, nx = mask.shape
    nxt = {}

    def add(a, b):
        nxt.setdefault(a, []).append(b)

    up = m[1:-1, 1:-1] & ~m[:-2, 1:-1]       # dry above: top edge, runs east
    dn = m[1:-1, 1:-1] & ~m[2:, 1:-1]        # dry below: bottom edge, runs west
    lf = m[1:-1, 1:-1] & ~m[1:-1, :-2]       # dry left: left edge, runs north
    rt = m[1:-1, 1:-1] & ~m[1:-1, 2:]        # dry right: right edge, runs south
    for r, c in zip(*np.nonzero(up)):
        add((r, c), (r, c + 1))
    for r, c in zip(*np.nonzero(rt)):
        add((r, c + 1), (r + 1, c + 1))
    for r, c in zip(*np.nonzero(dn)):
        add((r + 1, c + 1), (r + 1, c))
    for r, c in zip(*np.nonzero(lf)):
        add((r + 1, c), (r, c))
    rings = []
    while nxt:
        start = next(iter(nxt))
        ring = [start]
        cur = start
        prev_dir = None
        while True:
            outs = nxt.get(cur)
            if not outs:
                break
            if len(outs) == 1 or prev_dir is None:
                k = 0
            else:
                # two ways out (cells touching at a corner): turn right-most so the
                # True cells stay separate
                best, k = -9, 0
                for i, o in enumerate(outs):
                    d = (o[0] - cur[0], o[1] - cur[1])
                    cross = prev_dir[1] * d[0] - prev_dir[0] * d[1]   # >0: right turn (rows down)
                    if cross > best:
                        best, k = cross, i
            nx_ = outs.pop(k)
            if not outs:
                del nxt[cur]
            prev_dir = (nx_[0] - cur[0], nx_[1] - cur[1])
            cur = nx_
            if cur == start:
                break
            ring.append(cur)
        if len(ring) >= 4:
            rings.append(ring)
    return rings


def _dp(pts, tol):
    """Douglas-Peucker on an open polyline (N, 2); iterative."""
    n = len(pts)
    if n < 3:
        return pts
    keep = np.zeros(n, bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        seg = pts[b] - pts[a]
        ln = math.hypot(*seg)
        p = pts[a + 1:b] - pts[a]
        if ln == 0:
            d = np.hypot(p[:, 0], p[:, 1])
        else:
            d = np.abs(p[:, 0] * seg[1] - p[:, 1] * seg[0]) / ln
        i = int(np.argmax(d))
        if d[i] > tol:
            k = a + 1 + i
            keep[k] = True
            stack.append((a, k))
            stack.append((k, b))
    return pts[keep]


def _simplify_ring(pts, tol):
    """Simplify a closed ring (first point not repeated)."""
    if len(pts) < 8 or tol <= 0:
        return pts
    far = int(np.argmax(np.hypot(*(pts - pts[0]).T)))
    a = _dp(pts[:far + 1], tol)
    b = _dp(np.vstack([pts[far:], pts[:1]]), tol)
    out = np.vstack([a[:-1], b[:-1]])
    return out if len(out) >= 3 else pts


def _area(ring):
    x, y = ring[:, 0], ring[:, 1]
    return 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def mask_polygons(grid, mask, min_cells=4, simplify=0.7):
    """Polygons around the True cells of `mask` on `grid`: [(outer, [holes])],
    rings as (N, 2) arrays in map coordinates, closed. Blobs and holes smaller
    than `min_cells` are dropped; `simplify` is the tolerance in cells."""
    mask = np.asarray(mask, dtype=bool)
    if not mask.any():
        return []
    rings = _trace_rings(mask)
    outers, holes = [], []
    dx = grid.dx
    for ring in rings:
        rc = np.asarray(ring, dtype=float)
        xy = np.column_stack([grid.x0 + rc[:, 1] * dx, grid.y0 - rc[:, 0] * dx])
        area = _area(xy)                  # map coordinates: outer rings run clockwise (< 0)
        if abs(area) < min_cells * dx * dx:
            continue
        xy = _simplify_ring(xy, simplify * dx)
        area = _area(xy)
        if area < 0:
            outers.append([xy, abs(area), []])
        else:
            holes.append((xy, abs(area)))
    outers.sort(key=lambda o: o[1])
    for h, _a in holes:
        p = h[0]
        for o in outers:                  # smallest outer ring that contains the hole
            if cond.points_in_polygon([p[0]], [p[1]], o[0])[0]:
                o[2].append(h)
                break
    out = []
    for xy, _a, hs in outers:
        out.append((np.vstack([xy, xy[:1]]), [np.vstack([h, h[:1]]) for h in hs]))
    return out


def polygon_area_m2(poly):
    outer, holes = poly
    return abs(_area(outer[:-1])) - sum(abs(_area(h[:-1])) for h in holes)


# --------------------------------------------------------------------------------------
# GeoJSON / GeoPackage
# --------------------------------------------------------------------------------------
def crs_name(crs):
    """'EPSG:26914' for a CRS given as a code, a URN or WKT; "" when it has no code.

    3.20.3: the working CRS reaches here as WKT whenever it is not a plain EPSG code (a custom
    or an ESRI CRS). Before, such a CRS was dropped and every GeoJSON of the run went out with
    no CRS at all, which QGIS and ArcGIS then read as longitude / latitude - metres read as
    degrees, so the layer landed off the coast of Africa."""
    if not crs:
        return ""
    s = str(crs).strip()
    if s.upper().startswith(("EPSG", "URN")):
        return s
    try:
        from . import gdal_io
        return gdal_io.epsg_of(s) or ""
    except Exception:                        # noqa: BLE001 - no GDAL: the .prj sidecar remains
        return ""


def _crs_member(crs):
    name = crs_name(crs)
    if not name:
        return {}
    return {"crs": {"type": "name", "properties": {"name": name}}}


def _is_wkt(crs):
    return str(crs or "").strip().upper().startswith(("PROJCS", "PROJCRS", "GEOGCS", "GEOGCRS",
                                                      "COMPD_CS", "LOCAL_CS"))


def _ring_json(r):
    return [[round(float(x), 2), round(float(y), 2)] for x, y in r]


def geojson_polygons(polys, props):
    """One MultiPolygon feature."""
    return {"type": "Feature", "properties": props,
            "geometry": {"type": "MultiPolygon",
                         "coordinates": [[_ring_json(o)] + [_ring_json(h) for h in hs]
                                         for o, hs in polys]}}


def write_geojson(path, features, crs=None):
    fc = {"type": "FeatureCollection", "features": features}
    member = _crs_member(crs)
    fc.update(member)
    with open(path, "w") as f:
        json.dump(fc, f)
    # 3.20.3: a CRS with no EPSG code goes beside the file as a .prj, which QGIS, ArcGIS and
    # GDAL read for a .geojson, so the layer still lands in the right place
    if not member and _is_wkt(crs):
        try:
            with open(os.path.splitext(path)[0] + ".prj", "w") as f:
                f.write(str(crs).strip())
        except OSError:
            pass
    return path


def write_gpkg(path, layers, crs=None):
    """layers: {name: (geometry type 'polygon'|'line', [(geometry, props)])}.
    Returns the path, or None when OGR is not available."""
    try:
        from osgeo import ogr, osr
    except Exception:
        return None
    if os.path.exists(path):
        os.remove(path)
    drv = ogr.GetDriverByName("GPKG")
    if drv is None:
        raise RuntimeError("this GDAL has no GeoPackage (GPKG) driver")
    ds = drv.CreateDataSource(path)
    if ds is None:                     # 3.4.1: same error in both GDAL exception modes
        raise RuntimeError("cannot create the GeoPackage {}".format(path))
    srs = None
    if crs:
        srs = osr.SpatialReference()
        srs.SetFromUserInput(crs)
    for name, (kind, feats) in layers.items():
        if not feats:
            continue
        gt = ogr.wkbMultiPolygon if kind == "polygon" else ogr.wkbLineString
        lyr = ds.CreateLayer(name, srs, gt)
        if lyr is None:
            raise RuntimeError("cannot create layer {} in {}".format(name, path))
        keys = []
        for k, v in feats[0][1].items():
            keys.append(k)
            if isinstance(v, bool):
                t = ogr.OFTInteger
            elif isinstance(v, int):
                t = ogr.OFTInteger64
            elif isinstance(v, float):
                t = ogr.OFTReal
            else:
                t = ogr.OFTString
            lyr.CreateField(ogr.FieldDefn(k, t))
        for geom, props in feats:
            f = ogr.Feature(lyr.GetLayerDefn())
            for k in keys:
                f.SetField(k, props.get(k))
            if kind == "polygon":
                mp = ogr.Geometry(ogr.wkbMultiPolygon)
                for outer, holes in geom:
                    pg = ogr.Geometry(ogr.wkbPolygon)
                    for ring in [outer] + list(holes):
                        lr = ogr.Geometry(ogr.wkbLinearRing)
                        for x, y in ring:
                            lr.AddPoint_2D(float(x), float(y))
                        pg.AddGeometry(lr)
                    mp.AddGeometry(pg)
                f.SetGeometry(mp)
            else:
                ls = ogr.Geometry(ogr.wkbLineString)
                for x, y in geom:
                    ls.AddPoint_2D(float(x), float(y))
                f.SetGeometry(ls)
            lyr.CreateFeature(f)
    ds = None
    return path


# --------------------------------------------------------------------------------------
# SVG
# --------------------------------------------------------------------------------------
def _nice(v):
    """A round scale-bar length at or below v."""
    if v <= 0:
        return 1.0
    p = 10 ** math.floor(math.log10(v))
    for m in (5, 2, 1):
        if m * p <= v:
            return m * p
    return p


class _Map:
    def __init__(self, bounds, width=1000, pad=40, header=70, footer=70):
        x0, y0, x1, y1 = bounds
        self.w_m, self.h_m = max(x1 - x0, 1.0), max(y1 - y0, 1.0)
        self.scale = (width - 2 * pad) / self.w_m
        self.x0, self.y1 = x0, y1
        self.pad, self.header = pad, header
        self.width = width
        self.height = int(self.h_m * self.scale + header + footer + 2 * pad * 0.0)
        self.parts = []

    def px(self, x, y):
        return (self.pad + (x - self.x0) * self.scale, self.header + (self.y1 - y) * self.scale)

    def path(self, poly):
        outer, holes = poly
        d = []
        for ring in [outer] + list(holes):
            pts = [self.px(x, y) for x, y in ring]
            d.append("M" + "L".join("{:.1f},{:.1f}".format(*p) for p in pts) + "Z")
        return "".join(d)

    def add(self, s):
        self.parts.append(s)

    def svg(self):
        return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" '
                'height="{h}" font-family="Helvetica,Arial,sans-serif">'
                '<rect width="100%" height="100%" fill="#ffffff"/>{body}</svg>'.format(
                    w=self.width, h=self.height, body="".join(self.parts)))


def eap_map_svg(grid, final, dam, dam_xy, receptors=None, arrival_polys=None,
                flooded_receptors=None, title=None, note="", unit_m=1.0):
    """Depth-class map with arrival lines. `final` holds SI arrays."""
    d_ft = final["depth"] / FT
    valid = np.isfinite(d_ft)
    if not valid.any():
        raise ValueError("nothing flooded")
    rows = np.nonzero(valid.any(axis=1))[0]
    cols = np.nonzero(valid.any(axis=0))[0]
    pad_m = 0.08 * max(grid.dx * (cols[-1] - cols[0] + 1), grid.dx * (rows[-1] - rows[0] + 1))
    xmin = grid.x0 + cols[0] * grid.dx - pad_m
    xmax = grid.x0 + (cols[-1] + 1) * grid.dx + pad_m
    ymax = grid.y0 - rows[0] * grid.dx + pad_m
    ymin = grid.y0 - (rows[-1] + 1) * grid.dx - pad_m
    xmin, xmax = min(xmin, dam_xy[0] - pad_m), max(xmax, dam_xy[0] + pad_m)
    ymin, ymax = min(ymin, dam_xy[1] - pad_m), max(ymax, dam_xy[1] + pad_m)
    m = _Map((xmin, ymin, xmax, ymax))
    title = title or "Dam breach inundation: {} ({})".format(dam.name or dam.dam_id, dam.dam_id)
    m.add('<text x="{}" y="30" font-size="20" font-weight="bold">{}</text>'.format(
        m.pad, html.escape(title)))
    m.add('<text x="{}" y="52" font-size="12" fill="#444">{}</text>'.format(
        m.pad, html.escape(note or "Maximum depth of the breach flood, and time after breach "
                                    "initiation the flood first reaches an area.")))
    m.add('<rect x="{:.1f}" y="{:.1f}" width="{:.1f}" height="{:.1f}" fill="#f4f1ea" '
          'stroke="#999"/>'.format(m.pad, m.header, m.w_m * m.scale, m.h_m * m.scale))
    # depth classes: paint shallow first, deeper on top
    for lo, colr in zip(DEPTH_CLASSES_FT, DEPTH_COLORS):
        lo_thr = max(lo, 0.0)
        mask = valid & (d_ft > lo_thr if lo == 0.0 else d_ft >= lo_thr)
        for poly in mask_polygons(grid, mask, min_cells=2, simplify=0.8):
            m.add('<path d="{}" fill="{}" fill-rule="evenodd" stroke="none"/>'.format(
                m.path(poly), colr))
    for hrs, polys in (arrival_polys or []):
        for outer, holes in polys:
            for ring in [outer] + list(holes):
                pts = [m.px(x, y) for x, y in ring]
                m.add('<polyline points="{}" fill="none" stroke="#222" stroke-width="1" '
                      'stroke-dasharray="5,3"/>'.format(" ".join("{:.1f},{:.1f}".format(*p)
                                                                 for p in pts)))
        # label at the polygon's lowest-left vertex of the largest outer ring
        if polys:
            big = max(polys, key=lambda p: abs(_area(p[0][:-1])))[0]
            k = int(np.argmax(big[:, 0]))
            px, py = m.px(*big[k])
            m.add('<text x="{:.1f}" y="{:.1f}" font-size="11" fill="#222" '
                  'stroke="#fff" stroke-width="3" paint-order="stroke">{:g} h</text>'.format(
                      px + 4, py, hrs))
    if receptors is not None and len(receptors):
        rx, ry = receptors.x, receptors.y
        inb = (rx >= xmin) & (rx <= xmax) & (ry >= ymin) & (ry <= ymax)
        wet = flooded_receptors if flooded_receptors is not None else np.zeros(len(rx), bool)
        for i in np.nonzero(inb)[0]:
            px, py = m.px(rx[i], ry[i])
            if receptors.kind[i] == "structure":
                colr = "#d62728" if wet[i] else "#555555"
                m.add('<rect x="{:.1f}" y="{:.1f}" width="5" height="5" fill="{}"/>'.format(
                    px - 2.5, py - 2.5, colr))
            else:
                colr = "#d62728" if wet[i] else "#8a8a8a"
                m.add('<circle cx="{:.1f}" cy="{:.1f}" r="1.6" fill="{}"/>'.format(px, py, colr))
    px, py = m.px(*dam_xy)
    m.add('<polygon points="{:.1f},{:.1f} {:.1f},{:.1f} {:.1f},{:.1f}" fill="#000" '
          'stroke="#fff"/>'.format(px, py - 9, px - 8, py + 6, px + 8, py + 6))
    m.add('<text x="{:.1f}" y="{:.1f}" font-size="12" font-weight="bold" stroke="#fff" '
          'stroke-width="3" paint-order="stroke">Dam</text>'.format(px + 11, py + 4))
    # scale bar and north arrow
    real_w = m.w_m * unit_m
    if real_w * 0.2 >= 0.5 * MILE:
        bar_m = _nice(real_w * 0.2 / MILE) * MILE
        lab = "{:g} mi".format(bar_m / MILE)
    else:
        bar_m = _nice(real_w * 0.2)
        lab = "{:g} m".format(bar_m)
    bx, by = m.pad + 10, m.header + m.h_m * m.scale - 18
    m.add('<rect x="{:.1f}" y="{:.1f}" width="{:.1f}" height="6" fill="#000"/>'.format(
        bx, by, bar_m / unit_m * m.scale))
    m.add('<text x="{:.1f}" y="{:.1f}" font-size="11">{}</text>'.format(bx, by - 4, lab))
    nx_, ny_ = m.pad + m.w_m * m.scale - 24, m.header + 40
    m.add('<polygon points="{0:.1f},{1:.1f} {2:.1f},{3:.1f} {0:.1f},{4:.1f} {5:.1f},{3:.1f}" '
          'fill="#222"/><text x="{0:.1f}" y="{6:.1f}" font-size="12" text-anchor="middle" '
          'font-weight="bold">N</text>'.format(nx_, ny_ - 26, nx_ + 8, ny_, ny_ - 8, nx_ - 8,
                                                ny_ - 30))
    # legend
    ly = m.header + m.h_m * m.scale + 20
    lx = m.pad
    m.add('<text x="{}" y="{:.1f}" font-size="12" font-weight="bold">Depth (ft)</text>'.format(
        lx, ly + 10))
    lx += 80
    bounds_txt = list(DEPTH_CLASSES_FT) + [None]
    for i, colr in enumerate(DEPTH_COLORS):
        lo, hi = bounds_txt[i], bounds_txt[i + 1]
        txt = "> 0 – {:g}".format(hi) if i == 0 else (
            "{:g} – {:g}".format(lo, hi) if hi else "{:g} +".format(lo))
        m.add('<rect x="{}" y="{:.1f}" width="18" height="12" fill="{}"/>'
              '<text x="{}" y="{:.1f}" font-size="11">{}</text>'.format(lx, ly, colr, lx + 22,
                                                                         ly + 10, txt))
        lx += 92
    ly2 = ly + 24
    m.add('<polyline points="{0},{1:.1f} {2},{1:.1f}" stroke="#222" stroke-dasharray="5,3" '
          'fill="none"/><text x="{3}" y="{4:.1f}" font-size="11">Time flood first arrives, hours '
          'after breach initiation</text>'.format(m.pad, ly2 + 6, m.pad + 30, m.pad + 36,
                                                   ly2 + 10))
    if receptors is not None and len(receptors):
        m.add('<rect x="{}" y="{:.1f}" width="6" height="6" fill="#d62728"/>'
              '<text x="{}" y="{:.1f}" font-size="11">Flooded structure or road</text>'
              '<rect x="{}" y="{:.1f}" width="6" height="6" fill="#555"/>'
              '<text x="{}" y="{:.1f}" font-size="11">Not flooded</text>'.format(
                  m.pad + 330, ly2 + 3, m.pad + 342, ly2 + 10, m.pad + 500, ly2 + 3,
                  m.pad + 512, ly2 + 10))
    return m.svg()


def line_plot_svg(series, title, xlabel="Hours", ylabel="Discharge (cfs)", width=760,
                  height=300, marks=None):
    """series: [(label, x array, y array, colour)]; marks: [(x, label)] vertical lines."""
    left, right, top, bot = 70, 20, 40, 45
    xs = np.concatenate([s[1] for s in series])
    ys = np.concatenate([s[2] for s in series])
    xmin, xmax = float(xs.min()), float(xs.max())
    ymax = float(ys.max()) * 1.08 or 1.0
    xmax = xmax if xmax > xmin else xmin + 1.0

    def px(x):
        return left + (x - xmin) / (xmax - xmin) * (width - left - right)

    def py(y):
        return top + (1 - y / ymax) * (height - top - bot)

    out = ['<rect width="100%" height="100%" fill="#fff"/>',
           '<text x="{}" y="22" font-size="15" font-weight="bold">{}</text>'.format(
               left, html.escape(title))]
    step = _nice(ymax / 4.0)
    y = 0.0
    while y <= ymax:
        out.append('<line x1="{}" y1="{:.1f}" x2="{}" y2="{:.1f}" stroke="#ddd"/>'
                   '<text x="{}" y="{:.1f}" font-size="10" text-anchor="end">{:,.0f}</text>'
                   .format(left, py(y), width - right, py(y), left - 5, py(y) + 3, y))
        y += step
    step = _nice((xmax - xmin) / 6.0)
    x = math.ceil(xmin / step) * step
    while x <= xmax + 1e-9:
        out.append('<line x1="{:.1f}" y1="{}" x2="{:.1f}" y2="{}" stroke="#eee"/>'
                   '<text x="{:.1f}" y="{}" font-size="10" text-anchor="middle">{:g}</text>'
                   .format(px(x), top, px(x), height - bot, px(x), height - bot + 14, x))
        x += step
    for label, mx in (marks or []):
        if xmin <= mx <= xmax:
            out.append('<line x1="{0:.1f}" y1="{1}" x2="{0:.1f}" y2="{2}" stroke="#c00" '
                       'stroke-dasharray="4,3"/><text x="{0:.1f}" y="{3}" font-size="10" '
                       'fill="#c00">{4}</text>'.format(px(mx), top, height - bot, top - 4,
                                                       html.escape(label)))
    lx = left + 8
    for label, x, y, colr in series:
        out.append('<polyline fill="none" stroke="{}" stroke-width="2" points="{}"/>'.format(
            colr, " ".join("{:.1f},{:.1f}".format(px(a), py(b)) for a, b in zip(x, y))))
        out.append('<rect x="{}" y="{}" width="14" height="3" fill="{}"/><text x="{}" y="{}" '
                   'font-size="11">{}</text>'.format(lx, top + 8, colr, lx + 18, top + 13,
                                                     html.escape(label)))
        lx += 24 + 6.2 * len(label)
    out.append('<text x="{}" y="{}" font-size="11" text-anchor="middle">{}</text>'.format(
        (left + width - right) / 2, height - 8, html.escape(xlabel)))
    out.append('<text x="14" y="{}" font-size="11" text-anchor="middle" transform="rotate(-90 '
               '14 {})">{}</text>'.format((top + height - bot) / 2, (top + height - bot) / 2,
                                          html.escape(ylabel)))
    out.append('<rect x="{}" y="{}" width="{}" height="{}" fill="none" stroke="#888"/>'.format(
        left, top, width - left - right, height - top - bot))
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {0} {1}" width="{0}" '
            'height="{1}" font-family="Helvetica,Arial,sans-serif">{2}</svg>'.format(
                width, height, "".join(out)))


def hydrograph_plot(hy, hy_nb=None, flood=None):
    ser = [("With breach", hy.t_s / 3600.0, hy.q_cms / CFS, "#d62728")]
    if hy_nb is not None:
        ser.append(("Without breach", hy_nb.t_s / 3600.0, hy_nb.q_cms / CFS, "#1f77b4"))
    if flood is not None and getattr(flood, "inflow_t_s", None) is not None:
        ser.append(("Inflow", flood.inflow_t_s / 3600.0, flood.inflow_q_cms / CFS, "#2ca02c"))
    marks = [("breach starts", hy.t_ref_s / 3600.0)] if hy.t_ref_s > 0 else None
    return line_plot_svg(ser, "Outflow from the dam", marks=marks)


# --------------------------------------------------------------------------------------
# what write_workflow calls
# --------------------------------------------------------------------------------------
def _arrival_polys(grid, arrival, hours):
    out = []
    for h in hours:
        mask = np.isfinite(arrival) & (arrival <= h)
        polys = mask_polygons(grid, mask, min_cells=4, simplify=1.0)
        if polys:
            out.append((h, polys))
    return out


def write_products(res, report, st, dam, dam_xy, dem, fin, incr, hy, hy_nb, path, out_folder,
                   model_dir, arrival_m, crs=None, unit_m=1.0):
    """Flood polygons, arrival zones, the flow path, a GeoPackage, the EAP map
    and the hydrograph plot. Fills res.vectors and res.eap_map."""
    crs = crs or getattr(res, "crs", None)
    d = fin["depth"]
    feats = []
    layers = {}
    depth_feats = []
    for lo, colr in zip(DEPTH_CLASSES_FT, DEPTH_COLORS):
        hi_i = DEPTH_CLASSES_FT.index(lo) + 1
        hi = DEPTH_CLASSES_FT[hi_i] if hi_i < len(DEPTH_CLASSES_FT) else None
        with np.errstate(invalid="ignore"):
            mask = np.isfinite(d) & (d / FT > 0.0 if lo == 0 else d / FT >= lo)
            if hi is not None:
                mask &= (d / FT < hi)
        polys = mask_polygons(dem, mask, min_cells=2, simplify=0.8)
        if not polys:
            continue
        ac = sum(polygon_area_m2(p) for p in polys) * unit_m ** 2 / 4046.8564224
        props = {"class": "{:g}-{}".format(lo, "{:g}".format(hi) if hi else "+") + " ft",
                 "min_depth_ft": float(lo), "max_depth_ft": float(hi) if hi else 999.0,
                 "acres": round(ac, 1)}
        feats.append(geojson_polygons(polys, props))
        depth_feats.append((polys, props))
    if feats:
        p = os.path.join(out_folder, "Flood Polygons (Max Depth).geojson")
        write_geojson(p, feats, crs)
        res.vectors["depth_polygons"] = p
        layers["max_depth_classes"] = ("polygon", depth_feats)
    ext = mask_polygons(dem, np.isfinite(d) & (d >= arrival_m), min_cells=4, simplify=0.8)
    if ext:
        props = {"scenario": st.scenario, "dam_id": dam.dam_id,
                 "acres": round(sum(polygon_area_m2(x) for x in ext) * unit_m ** 2 / 4046.8564224, 1)}
        p = os.path.join(out_folder, "Flood Extent.geojson")
        write_geojson(p, [geojson_polygons(ext, props)], crs)
        res.vectors["extent"] = p
        layers["flood_extent"] = ("polygon", [(ext, props)])
    arr_polys = _arrival_polys(dem, fin["arrival"], st.arrival_contours_hr)
    if arr_polys:
        af, al = [], []
        for h, polys in arr_polys:
            props = {"hours_after_breach": float(h)}
            af.append(geojson_polygons(polys, props))
            al.append((polys, props))
        p = os.path.join(out_folder, "Arrival Zones.geojson")
        write_geojson(p, af, crs)
        res.vectors["arrival_zones"] = p
        layers["arrival_zones"] = ("polygon", al)
    if incr is not None:
        idf = []
        il = []
        for t in st.incr_thresholds_ft:
            with np.errstate(invalid="ignore"):
                mk = np.isfinite(incr["depth"]) & (incr["depth"] >= t * FT)
            polys = mask_polygons(dem, mk, min_cells=2, simplify=0.8)
            if polys:
                props = {"incremental_depth_ge_ft": float(t),
                         "acres": round(sum(polygon_area_m2(x) for x in polys) * unit_m ** 2 / 4046.8564224, 1)}
                idf.append(geojson_polygons(polys, props))
                il.append((polys, props))
        if idf:
            p = os.path.join(out_folder, "Incremental Depth Polygons.geojson")
            write_geojson(p, idf, crs)
            res.vectors["incremental"] = p
            layers["incremental_depth"] = ("polygon", il)
    if path is not None and len(path) > 1:
        layers["flow_path"] = ("line", [(np.asarray(path), {"dam_id": dam.dam_id})])
        # QA layer: where the breach enters the model and the path the domain follows
        plan = res.plan or {}
        qa = [{"type": "Feature", "properties": {"what": "dam"},
               "geometry": {"type": "Point", "coordinates": [float(dam_xy[0]), float(dam_xy[1])]}},
              {"type": "Feature", "properties": {"what": "flow path used to lay out the domain"},
               "geometry": {"type": "LineString", "coordinates": _ring_json(np.asarray(path))}}]
        if plan.get("inflow_xy"):
            qa.append({"type": "Feature", "properties": {"what": "breach inflow point"},
                       "geometry": {"type": "Point", "coordinates": [
                           float(plan["inflow_xy"][0]), float(plan["inflow_xy"][1])]}})
        p = os.path.join(model_dir, "qa_dam_inflow_and_flow_path.geojson")
        write_geojson(p, qa, crs)
        res.vectors["qa_points_and_path"] = p
    if layers:
        g = write_gpkg(os.path.join(out_folder, "Breach Products.gpkg"), layers, crs)
        if g:
            res.vectors["geopackage"] = g

    # EAP map
    wet_rec = None
    if st.receptors is not None and len(st.receptors):
        dd = st.receptors.sample(dem, d)
        wet_rec = np.isfinite(dd) & (dd >= arrival_m)
    if eap_preview_wanted(getattr(st, "eap_map", EAP_AUTO)):
        try:
            svg = eap_map_svg(dem, fin, dam, dam_xy, st.receptors, arr_polys, wet_rec,
                              unit_m=unit_m)
            res.eap_map = os.path.join(out_folder, EAP_PREVIEW_NAME)
            with open(res.eap_map, "w", encoding="utf-8") as f:
                f.write(svg)
        except ValueError:
            pass
    else:
        report["eap_map_note"] = MAPBOOK_NOTE
    # hydrograph plot
    svg = hydrograph_plot(hy, hy_nb, res.flood)
    p = os.path.join(model_dir, "hydrograph.svg")
    with open(p, "w", encoding="utf-8") as f:
        f.write(svg)
    res.vectors["hydrograph_plot"] = p
    if res.gauges:
        ser = [(n, t / 3600.0, q / CFS, c) for (n, (t, q)), c in zip(
            res.gauges.items(), ("#d62728", "#1f77b4", "#2ca02c", "#9467bd", "#8c564b"))]
        p = os.path.join(model_dir, "gauges.svg")
        with open(p, "w", encoding="utf-8") as f:
            f.write(line_plot_svg(ser, "Discharge past the gauges (LISFLOOD-FP)"))
        res.vectors["gauge_plot"] = p
    report.setdefault("outputs", {})["vectors"] = dict(res.vectors)
    if res.eap_map:
        report["outputs"]["eap_map"] = res.eap_map


# --------------------------------------------------------------------------------------
# HTML report (a small markdown-to-HTML for the report the workflow writes)
# --------------------------------------------------------------------------------------
_CSS = ("body{font-family:Helvetica,Arial,sans-serif;max-width:960px;margin:24px auto;"
        "padding:0 16px;color:#222;line-height:1.45}table{border-collapse:collapse;margin:8px 0}"
        "td,th{border:1px solid #ccc;padding:4px 8px;text-align:left;font-size:14px}"
        "th{background:#f0f0f0}code{background:#f4f4f4;padding:1px 4px}h1{font-size:24px}"
        "h2{font-size:19px;margin-top:28px;border-bottom:1px solid #ddd}"
        ".warn{background:#fff6e0;border-left:4px solid #e0a000;padding:6px 12px}"
        "svg{max-width:100%;height:auto;border:1px solid #ddd;margin:8px 0}")


def _inline(s):
    s = html.escape(s)
    out, bold = [], False
    for i, part in enumerate(s.split("**")):
        out.append(("<b>" if i % 2 else "") + part + ("</b>" if i % 2 else ""))
    return "".join(out)


def markdown_to_html(md, title="Report", extra_html=""):
    lines = md.split("\n")
    out = []
    i = 0
    in_list = False
    warn = False
    while i < len(lines):
        ln = lines[i]
        if in_list and not ln.startswith("- "):
            out.append("</ul>")
            in_list = False
        if ln.startswith("# "):
            out.append("<h1>{}</h1>".format(_inline(ln[2:])))
        elif ln.startswith("## "):
            warn = ln[3:].startswith("Check these")
            out.append("<h2>{}</h2>".format(_inline(ln[3:])))
        elif ln.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            i -= 1
            rows = [r for r in rows if not all(c and set(c) <= set("-: ") for c in r)]
            head = rows[0] if rows and any(rows[0]) else None
            out.append("<table>")
            if head:
                out.append("<tr>" + "".join("<th>{}</th>".format(_inline(c)) for c in head)
                           + "</tr>")
            for r in rows[1:]:
                out.append("<tr>" + "".join("<td>{}</td>".format(_inline(c)) for c in r)
                           + "</tr>")
            out.append("</table>")
        elif ln.startswith("- "):
            if not in_list:
                out.append('<ul class="warn">' if warn else "<ul>")
                in_list = True
            out.append("<li>{}</li>".format(_inline(ln[2:])))
        elif ln.strip():
            out.append("<p>{}</p>".format(_inline(ln)))
        i += 1
    if in_list:
        out.append("</ul>")
    return ("<!doctype html><html><head><meta charset='utf-8'><title>{}</title><style>{}</style>"
            "</head><body>{}{}</body></html>".format(html.escape(title), _CSS, "\n".join(out),
                                                     extra_html))


def write_html_report(report, model_dir, res):
    from . import workflow
    md = workflow.render_report(report)
    extra = []
    if res.eap_map and os.path.exists(res.eap_map):
        extra.append("<h2>EAP inundation map (preview; the EAP map is ND Dam Hazard's map "
                     "book)</h2>")
        with open(res.eap_map, encoding="utf-8") as f:
            extra.append(f.read())
    for key, head in (("hydrograph_plot", "Outflow hydrograph"),
                      ("gauge_plot", "Discharge past the gauges")):
        p = res.vectors.get(key)
        if p and os.path.exists(p):
            extra.append("<h2>{}</h2>".format(head))
            with open(p, encoding="utf-8") as f:
                extra.append(f.read())
    out = os.path.join(model_dir, "run_report.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(markdown_to_html(md, "Breach model run: {}".format(res.dam_id), "\n".join(extra)))
    return out


def products_from_rasters(folder, dam_id, dam_xy, arrival_depth_ft=1.0, contours_hr=(0.5, 1, 2, 4, 8),
                          name="", out_folder=None, eap_map=EAP_AUTO):
    """Flood polygons, arrival zones, GeoPackage and EAP map made from the
    'Depth (Max).tif', 'Arrival Time.tif' (and optionally velocity, WSE) in a
    dam folder, for runs made earlier or by another model. Needs GDAL."""
    import types
    from . import gdal_io
    from .workflow import FINAL_NAMES
    paths = {k: os.path.join(folder, v) for k, v in FINAL_NAMES.items()}
    if not os.path.exists(paths["depth"]):
        raise FileNotFoundError("no '{}' in {}".format(FINAL_NAMES["depth"], folder))
    g = gdal_io.read_grid(paths["depth"])
    fin = {"depth": g.data * FT}
    for k in ("arrival", "velocity", "wse"):
        if os.path.exists(paths[k]):
            a = gdal_io.read_like(paths[k], g, "near")
            fin[k] = a if k == "arrival" else a * FT
        else:
            fin[k] = np.full(g.data.shape, np.nan)
    unit = gdal_io.linear_unit_m(g.crs_wkt) or 1.0
    dam = types.SimpleNamespace(dam_id=dam_id, name=name)
    st = types.SimpleNamespace(scenario="from rasters", arrival_contours_hr=contours_hr,
                               receptors=None, incr_thresholds_ft=(1.0, 2.0, 3.0),
                               eap_map=eap_map)
    res = types.SimpleNamespace(vectors={}, eap_map="", flood=None, gauges={})
    out = out_folder or folder
    md = os.path.join(out, "_breach_model")
    os.makedirs(md, exist_ok=True)
    hy = types.SimpleNamespace(t_s=np.array([0.0, 1.0]), q_cms=np.array([0.0, 0.0]),
                               t_ref_s=0.0)
    write_products(res, {}, st, dam, dam_xy, g, fin, None, hy, None, None, out, md,
                   arrival_depth_ft * FT, crs=gdal_io.epsg_of(g.crs_wkt), unit_m=unit)
    return res
