# -*- coding: utf-8 -*-
"""v2.10 (dam suite item 12): one data pack for the whole suite.

Exports ONE GeoPackage in the standard schema of ND Dam Breach Screening's
"0 - Build offline data package" (EPSG:5070, the layer and field names its
later tools pick up automatically) from what ND Dam Hazard already has:
its offline data pack (offline_pack.py / build_nd_pack.py) or a folder of
its downloads (the GeoJSON files in <output>/_downloads, also _pack copies).

Layers written (all EPSG:5070):
  structures        the chosen structure data set (FEMA USA Structures outlines by
                    default; NSI points or ND GIS Hub address points on request) with
                    habitable 1/0, mobile 1/0, occ_cls, prim_occ, plus ND Dam Hazard's own
                    reading (nd_habitable Yes / Unverified / No, nd_category, nd_type,
                    type_basis). Habitability and the manufactured-home type are
                    structures._interpret - the SAME rules as ND Dam Hazard's
                    classification - run on each feature's own attributes (one source per
                    structure: the classification's merging of several sources at one
                    building, the OSM mobile-home-park test and engineer review
                    decisions are not applied here). habitable = 0 only for "No";
                    "Unverified" (e.g. an untagged outline, an NSI AGR record) is
                    written as 1, the conservative side, and kept in nd_habitable.
  structure_points  the same structures as points (point on surface) - ND Dam Breach
                    Runner's receptor layers are points.
  roads             ND GIS Hub NG9-1-1 centerlines (else the newest HPMS year, else the
                    county roads) with road_class (linear.AttrReader.road_class),
                    aadt / aadt_year / aadt_src (the most recent AADT by ND Dam Hazard's
                    rules: the road's own attribute, HPMS (newest year; else NDDOT
                    counts) and county-road lines that run along the road - within
                    AADT_CORRIDOR_FT for at least AADT_MIN_ALONG of its length, within
                    AADT_MAX_ANGLE_DEG, names not conflicting - count stations within
                    AADT_POINT_FT, the nearest FRA crossing within CROSSING_AADT_SEARCH_FT;
                    linear.best_aadt). Unknown AADT stays EMPTY, never 0. level /
                    level_basis / judgment: linear.nd_road_class (ND 5.2.1.2 as ND Dam
                    Hazard applies it: a local road without AADT is Low "verify traffic").
  railroads         NARN lines (abandoned / removed / trail NET codes left out) with
                    passenger 1/0 (NARN PASSNGR, ND Dam Hazard's reading: any value but
                    blank / N / 0 ... is a passenger line), trains_day from FRA crossings
                    as linear.analyze_rail finds them (same railroad, on the line or the
                    same subdivision first, within RAIL_CROSSING_SEARCH_MI), level
                    (linear.nd_rail_class).
  flowlines         USGS NHD flowlines as the pack holds them (name and type only).
  lifelines         5.2.2 lifeline facilities (ND GIS Hub / EIA / HIFLD layers in the
                    source, OpenStreetMap) as points with category (and kind = category,
                    the field name Screening's tool 8 looks for); care facilities and
                    schools are not lifelines and are left out; the same category within
                    FACILITY_DEDUP_FT is one facility (agency layers first).
  dams              NID dams: nid_id, name, hazard, height_ft, storage_acft, river,
                    eap, state_id.
  suite_export_info attribute table: what was exported from where, counts, gaps.

NOT in the pack and therefore not exported: NHD waterbodies, the DWR dam lines, any
DEM / terrain. Give those to Screening's tool 0 (and tool 2) yourself.

2.17.0 - an export for ONE dam folder (export(..., dam_folder=...), or a package written
in <dam folder>/data/ next to dam.json, as "Get data for this dam" does) also writes
what Screening requires and the dam folder has (dam.json's data section, damsuite.damdata),
else the source's own data set when it has one:
  waterbodies       NHD waterbodies (role waterbodies; source NHD_waterbodies) - the
                    layer name Screening's tools 3 / 16 take from a package
  dam_lines         ND DWR dam lines (role dwr_dam_lines; source DWR_dam_lines) - tool 4
  dwr_dams          ND DWR dams inventory (role dwr_dams; source DWR_dams) - tool 1 (its
                    DWR slot has no package layer: choose this layer there)
  DEM               not copied (a large raster): its path is recorded in suite_export_info
                    (dem, dem_relative = from the package's folder, dem_source)
The features keep their own attributes (plus source). An export not tied to a dam
folder is unchanged.

Area: North Dakota's box (offline_pack.ND_BBOX, the pack's own area) by default, or
a given lon / lat box. Features that touch the box are exported whole (not cut).

Everything here is pure Python on GeoJSON features (the pack stores them that
way) - no QGIS layers - so it runs in the Processing algorithm
(suite_algorithms.SuiteExportAlgorithm) and from tests alike. Nothing has
been run inside a real QGIS or opened with a real GDAL.
"""

import datetime
import json
import math
import os
import re

from . import suitegpkg as G
from . import constants as C
from .offline_pack import open_pack, intersects_rect, ND_BBOX, DB_NAME, MANIFEST_NAME, \
    PackError

FORMAT = "nd_dam_suite/screening_package"
FORMAT_VERSION = 1
INFO_TABLE = "suite_export_info"
LAYERS = ("structures", "structure_points", "roads", "railroads", "flowlines", "lifelines",
          "dams")
NOT_IN_PACK = ("waterbodies (NHD waterbodies: the pack holds flowlines only)",
               "dam_lines (ND DWR dam lines)",
               "DEM / terrain (Screening tool 2: USGS 3DEP or local LiDAR)")
FT = 0.3048
# 2.17.0: what an export for one dam folder adds - (layer, dam.json data role, source data
# set, label); the layer names are those ND Dam Breach Screening reads (workflow.LAYER_SLOTS)
DAM_EXTRAS = (("waterbodies", "waterbodies", "NHD_waterbodies", "NHD waterbodies"),
              ("dam_lines", "dwr_dam_lines", "DWR_dam_lines", "ND DWR dam lines"),
              ("dwr_dams", "dwr_dams", "DWR_dams", "ND DWR dams"))

# structure data sets, in the order "auto" tries them
STRUCTURE_SETS = (("USA_Structures", "FEMA USA Structures outlines", "USA_STRUCTURES"),
                  ("NSI_structures", "USACE National Structure Inventory points", "NSI"),
                  ("NDGISHUB_address_points", "ND GIS Hub site address points", "NDGISHUB"))
ROAD_SETS = ("ND_roads_NG911",)
HPMS_SETS = tuple("HPMS_ND_{}".format("".join(ch for ch in u.split("HPMS_FULL_ND_")[-1][:4]
                                              if ch.isdigit()))
                  for u in C.HPMS_ND_URLS)


class ExportError(Exception):
    pass


# ------------------------------------------------------------------ sources --
class PackSource(object):
    """ND Dam Hazard's offline data pack (.zip, .sqlite or its folder)."""
    kind = "offline data pack"

    def __init__(self, path, log=None):
        self.path = path
        try:
            self.reader = open_pack(path, log=log)
        except PackError as e:
            raise ExportError(str(e))

    def names(self):
        return sorted(self.reader.datasets)

    def has(self, name):
        return self.reader.has(name)

    def features(self, name, extent):
        return self.reader.query(name, extent) if self.reader.has(name) else None

    def note(self, name, extent):
        info = self.reader.info(name) or {}
        partial = not self.reader.covers(extent, name)
        return "fetched {}{}".format(info.get("fetched") or self.reader.created or "?",
                                     "; INCOMPLETE for this area (partial data set or gaps)"
                                     if partial else "")


_DL_RE = re.compile(r"^(?P<name>.+?)_(?:(?P<day>\d{8})|pack-(?P<pday>\d*))_"
                    r"(?P<digest>[0-9a-f]{8}|area)(?:_\d+)?\.geojson$")


class FolderSource(object):
    """A folder of ND Dam Hazard downloads: <name>_<YYYYMMDD>_<digest>.geojson (and the
    _pack copies <name>_pack-<date>_<digest>.geojson), searched up to four folder levels
    down (a dam output folder, its _downloads folder, or a folder of dams). For every data
    set the newest file of each study area is read; features found in several files are
    kept once."""
    kind = "folder of downloads"

    def __init__(self, folder, max_depth=4):
        self.path = folder
        if not os.path.isdir(folder):
            raise ExportError("folder not found: {}".format(folder))
        found = {}
        base = os.path.abspath(folder).rstrip(os.sep).count(os.sep)
        for root, dirs, files in os.walk(folder):
            if root.count(os.sep) - base >= max_depth:
                dirs[:] = []
            for f in files:
                m = _DL_RE.match(f)
                if not m:
                    continue
                day = m.group("day") or m.group("pday") or ""
                key = (m.group("name"), m.group("digest"))
                prev = found.get(key)
                if prev is None or day > prev[0]:
                    found[key] = (day, os.path.join(root, f))
        self.files = {}
        for (name, _digest), (day, path) in found.items():
            self.files.setdefault(name, []).append((day, path))
        if not self.files:
            raise ExportError("no ND Dam Hazard downloads (*.geojson) found in {}".format(folder))

    def names(self):
        return sorted(self.files)

    def has(self, name):
        return name in self.files

    def features(self, name, extent):
        if name not in self.files:
            return None
        out, seen = [], set()
        for _day, path in sorted(self.files[name]):
            try:
                with open(path, encoding="utf-8") as fh:
                    fc = json.load(fh)
            except (OSError, ValueError):
                continue
            for f in fc.get("features") or ():
                if not isinstance(f, dict) or not f.get("geometry"):
                    continue
                if extent is not None and not intersects_rect(f["geometry"], extent):
                    continue
                key = json.dumps([f.get("geometry"), f.get("properties")], sort_keys=True,
                                 default=str)
                if key in seen:
                    continue
                seen.add(key)
                out.append(f)
        return out

    def note(self, name, extent):
        days = sorted({d for d, _p in self.files.get(name, ())})
        return "{} file(s), downloaded {}".format(len(self.files.get(name, ())),
                                                  ", ".join(d for d in days if d) or "?")


def open_source(path, log=None):
    """PackSource for a pack (.zip / .sqlite / pack folder), else FolderSource."""
    if not path:
        raise ExportError("no offline data pack or downloads folder given")
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(path):
        raise ExportError("not found: {}".format(path))
    if os.path.isfile(path) or os.path.isfile(os.path.join(path, DB_NAME)) or \
            os.path.isfile(os.path.join(path, MANIFEST_NAME)):
        return PackSource(path, log)
    return FolderSource(path)


# ------------------------------------------------------------------ shims --
class _Field(object):
    def __init__(self, name):
        self._n = name

    def name(self):
        return self._n


class _Fields(object):
    def __init__(self, names):
        self._f = [_Field(n) for n in names]

    def count(self):
        return len(self._f)

    def at(self, i):
        return self._f[i]

    def __iter__(self):
        return iter(self._f)

    def names(self):
        return [f.name() for f in self._f]


class _Layer(object):
    """Just enough of a vector layer for linear.AttrReader / structures._SourceReader."""

    def __init__(self, names, name=""):
        self._fields = _Fields(names)
        self._name = name

    def fields(self):
        return self._fields

    def name(self):
        return self._name


class _Feat(object):
    def __init__(self, attrs):
        self._a = attrs

    def attributes(self):
        return self._a


def prop_names(features):
    """Property names of GeoJSON features in first-seen order; names that differ only in
    case are one field (the first spelling)."""
    out, low = [], set()
    for f in features:
        for k in (f.get("properties") or {}):
            if k.lower() not in low:
                low.add(k.lower())
                out.append(k)
    return out


def _attrs(f, names):
    p = f.get("properties") or {}
    if len(p) and all(n in p for n in names):
        return [p[n] for n in names]
    low = {str(k).lower(): v for k, v in p.items()}
    return [p[n] if n in p else low.get(n.lower()) for n in names]


def _getp(props, *names):
    """First non-empty property value of `names` (case-insensitive)."""
    low = {str(k).lower(): v for k, v in (props or {}).items()}
    for n in names:
        v = low.get(n.lower())
        if v is not None and str(v).strip() != "":
            return v
    return None


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(str(v).strip()) if isinstance(v, str) else float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def projected(features):
    """[(feature, geometry in EPSG:5070)] - features whose geometry cannot be read are
    left out."""
    out = []
    for f in features or ():
        g = G.project_5070(f.get("geometry"))
        if g is not None and G.points_of(g):
            out.append((f, g))
    return out


def _parts(g):
    return [p for p in G.lines_of(g) if len(p) >= 2]


# ------------------------------------------------------------------ structures --
def _structure_texts(dataset, props):
    """(occ_cls, prim_occ, source id) of one structure feature."""
    if dataset == "NSI_structures":
        from .structures import hazus_label
        occ = _getp(props, "occtype")
        return (str(occ) if occ is not None else None,
                hazus_label(occ) if occ is not None else None,
                _getp(props, "fd_id"))
    if dataset == "NDGISHUB_address_points":
        return (None, _getp(props, "place_type"), _getp(props, "site_nguid", "objectid"))
    return (_getp(props, "occ_cls"), _getp(props, "prim_occ"),
            _getp(props, "build_id", "objectid", "fid"))


STRUCTURE_FIELDS = [("habitable", "int"), ("mobile", "int"), ("occ_cls", "text"),
                    ("prim_occ", "text"), ("nd_habitable", "text"), ("nd_category", "text"),
                    ("nd_type", "text"), ("type_basis", "text"), ("area_ft2", "real"),
                    ("source", "text"), ("src_id", "text")]


def standardize_structures(dataset, features, mobile_rules=None, progress=None):
    """[(geometry 5070, attrs)] with ND Dam Hazard's habitability / manufactured-home
    reading of each feature's own attributes (structures._interpret)."""
    from . import structures as S
    rules = dict(S.MOBILE_RULE_DEFAULTS)
    rules.update(mobile_rules or {})
    family = {d: fam for d, _l, fam in STRUCTURE_SETS}.get(dataset, "USER")
    feats = projected(features)
    names = prop_names(f for f, _g in feats)
    reader = S._SourceReader(family, _Layer(names, dataset))
    out = []
    n = len(feats) or 1
    for i, (f, g) in enumerate(feats):
        if progress and i % 5000 == 0:
            progress(100.0 * i / n, "structures {:,} / {:,}".format(i, len(feats)))
        src = reader.describe(_Feat(_attrs(f, names)))
        poly = G.kind(g) == "polygon"
        area_ft2 = G.polygon_area(g) / (FT * FT) if poly else None
        dims = None
        if poly:
            rings = [pl[0] for pl in G.polygons_of(g) if pl]
            wh = G.min_area_rect([p for r in rings for p in r])
            dims = (wh[0] / FT, wh[1] / FT) if wh else None
        rec = {"sources": [src], "footprint": poly, "area_ft2": area_ft2, "dims_ft": dims,
               "mobile_park": None}
        S._interpret(rec, **rules)
        occ, prim, sid = _structure_texts(dataset, f.get("properties") or {})
        out.append((g, {"habitable": 0 if rec["habitable"] == "No" else 1,
                        "mobile": 1 if rec["acer_type"] == S.TYPE_MOBILE else 0,
                        "occ_cls": occ, "prim_occ": prim, "nd_habitable": rec["habitable"],
                        "nd_category": rec["category"], "nd_type": rec["acer_type"],
                        "type_basis": rec.get("type_basis") or "",
                        "area_ft2": round(area_ft2, 1) if area_ft2 is not None else None,
                        "source": dataset, "src_id": sid}))
    return out


# ------------------------------------------------------------------ roads --
def _densify(pts, step):
    out = [tuple(pts[0])]
    for a, b in zip(pts[:-1], pts[1:]):
        d = math.hypot(b[0] - a[0], b[1] - a[1])
        k = max(1, int(math.ceil(d / step)))
        out += [(a[0] + (b[0] - a[0]) * j / k, a[1] + (b[1] - a[1]) * j / k)
                for j in range(1, k + 1)]
    return out


def _direction_at(parts, x, y):
    best = None
    for pts in parts:
        for i in range(len(pts) - 1):
            d = G.seg_distance(x, y, pts[i], pts[i + 1])
            dx, dy = pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]
            n = math.hypot(dx, dy)
            if n > 0 and (best is None or d < best[0]):
                best = (d, dx / n, dy / n)
    return None if best is None else best[1:]


class _Index(object):
    """Features of one data set in EPSG:5070 with a grid over their segments / points."""

    def __init__(self, label, feats, cell):
        from .linear import AttrReader
        self.label = label
        self.names = prop_names(f for f, _g in feats)
        self.reader = AttrReader(_Layer(self.names, label))
        self.items = []
        self.grid = G.SegmentGrid(cell)
        for f, g in feats:
            k = len(self.items)
            attrs = _attrs(f, self.names)
            if G.kind(g) == "point":
                pts = G.points_of(g)
                self.items.append((attrs, "point", [[pts[0]]]))
                self.grid.add_line(k, [[pts[0], pts[0]]])
            else:
                parts = _parts(g) or [[p] for p in G.points_of(g)[:1]]
                self.items.append((attrs, G.kind(g), parts))
                self.grid.add_line(k, [p if len(p) >= 2 else [p[0], p[0]] for p in parts])

    def within(self, samples, dist, corridor=None):
        """[(distance, item index)] of the items within `dist` of any sample point, nearest
        first. With `corridor`: [(distance, item index, share of the samples within
        `corridor` of the item)] (only segments near each sample are measured)."""
        best, hits = {}, {}
        for (x, y) in samples:
            near = {}
            for k, pi, si in self.grid.near(x, y, dist):
                pts = self.items[k][2][pi]
                a = pts[si]
                b = pts[si + 1] if si + 1 < len(pts) else pts[si]
                d = G.seg_distance(x, y, a, b)
                if d <= dist and (k not in near or d < near[k]):
                    near[k] = d
            for k, d in near.items():
                if k not in best or d < best[k]:
                    best[k] = d
                if corridor is not None and d <= corridor:
                    hits[k] = hits.get(k, 0) + 1
        if corridor is None:
            return sorted((d, k) for k, d in best.items())
        n = float(len(samples) or 1)
        return sorted((d, k, hits.get(k, 0) / n) for k, d in best.items())


def road_rows(road_feats, aadt_indexes=(), crossing_index=None, progress=None):
    """[(geometry, attrs)] of the road lines with ND Dam Hazard's road class and the most
    recent AADT (see the module docstring)."""
    from . import linear as L
    names = prop_names(f for f, _g in road_feats)
    reader = L.AttrReader(_Layer(names, "roads"))
    search = C.AADT_SEARCH_FT * FT
    corridor = L.AADT_CORRIDOR_FT * FT
    point_near = L.AADT_POINT_FT * FT
    xsearch = C.CROSSING_AADT_SEARCH_FT * FT
    cos_max = math.cos(math.radians(L.AADT_MAX_ANGLE_DEG))
    out = []
    n = len(road_feats) or 1
    for i, (f, g) in enumerate(road_feats):
        if progress and i % 2000 == 0:
            progress(100.0 * i / n, "roads {:,} / {:,}".format(i, len(road_feats)))
        parts = _parts(g)
        if not parts:
            continue
        attrs = _attrs(f, names)
        rclass, cbasis = reader.road_class(attrs)
        rname = reader.road_name(attrs)
        rnames = reader.name_text(attrs)
        cands = [c + (2,) for c in reader.aadt_candidates(attrs, "road layer")]
        total = sum(G.line_length(p) for p in parts)
        step = max(10.0, total / 100.0)
        samples = [p for pts in parts for p in _densify(pts, step)]
        longest = max(parts, key=G.line_length)
        mid = G.point_at(longest, G.line_length(longest) / 2.0)
        mdir = _direction_at([longest], mid[0], mid[1])
        for idx in aadt_indexes:
            for dd, k, share in idx.within(samples, search, corridor):
                fa, kind, cparts = idx.items[k]
                if L.names_conflict(rnames, idx.reader.name_text(fa)):
                    continue
                if kind == "point":
                    if dd > point_near:
                        continue
                else:
                    if share < L.AADT_MIN_ALONG:
                        continue
                    cdir = _direction_at(cparts, mid[0], mid[1])
                    if mdir is not None and cdir is not None and \
                            abs(mdir[0] * cdir[0] + mdir[1] * cdir[1]) < cos_max:
                        continue
                cands.extend(c + (1,) for c in idx.reader.aadt_candidates(
                    fa, "'{}'".format(idx.label)))
        if crossing_index is not None:
            for _dd, k in crossing_index.within(samples, xsearch)[:1]:
                fa = crossing_index.items[k][0]
                rd = crossing_index.reader
                xname = " ".join(str(v) for v in (rd.get(fa, "street"), rd.get(fa, "highwayname"))
                                 if v not in (None, "") and not _is_null(v))
                if L.names_conflict(rnames, xname):
                    continue
                cid = rd.get(fa, "crossingid")
                cands.extend(c + (0,) for c in rd.aadt_candidates(
                    fa, "FRA crossing {}".format(cid or "")))
        aadt, year, src = L.best_aadt(cands)
        cls, basis, judge = L.nd_road_class(rclass, aadt)
        out.append((g, {"name": rname, "road_class": rclass, "class_basis": cbasis,
                        "aadt": aadt, "aadt_year": year, "aadt_src": src, "level": cls,
                        "level_basis": basis, "judgment": 1 if judge else 0,
                        "bridge": 1 if reader.is_bridge(attrs) else 0}))
    return out


def _is_null(v):
    try:
        from .helpers import is_null
        return is_null(v)
    except Exception:
        return v is None


ROAD_FIELDS = [("name", "text"), ("road_class", "text"), ("class_basis", "text"),
               ("aadt", "real"), ("aadt_year", "int"), ("aadt_src", "text"), ("level", "text"),
               ("level_basis", "text"), ("judgment", "int"), ("bridge", "int"),
               ("source", "text")]


# ------------------------------------------------------------------ rail --
RAIL_FIELDS = [("railroad", "text"), ("subdivision", "text"), ("passenger", "int"),
               ("passngr", "text"), ("trains_day", "real"), ("trains_src", "text"),
               ("level", "text"), ("level_basis", "text"), ("judgment", "int"), ("net", "text"),
               ("source", "text")]


def rail_rows(rail_feats, crossing_index=None, progress=None):
    """[(geometry, attrs)] of railroad lines with passenger and trains a day
    (linear.analyze_rail's rules on whole lines), and the number of lines left out."""
    from . import linear as L
    names = prop_names(f for f, _g in rail_feats)
    reader = L.AttrReader(_Layer(names, "rail"))
    owner_i = next((reader.low[n] for n in L._OWNER_NAMES if n in reader.low), None)
    pass_i = next((reader.low[n] for n in L._PASS_NAMES if n in reader.low), None)
    sub_i = next((reader.low[n] for n in ("subdiv", "subdivision", "branch", "name")
                  if n in reader.low), None)
    net_i = reader.low.get("net")
    search = C.RAIL_CROSSING_SEARCH_MI * 5280.0 * FT
    on_line = L.RAIL_ON_LINE_FT * FT
    out, skipped = [], 0

    def txt(attrs, i):
        return "" if i is None or _is_null(attrs[i]) else str(attrs[i]).strip()
    for i, (f, g) in enumerate(rail_feats):
        if progress and i % 2000 == 0:
            progress(100.0 * i / max(len(rail_feats), 1), "railroads {:,}".format(i))
        parts = _parts(g)
        if not parts:
            continue
        attrs = _attrs(f, names)
        net = txt(attrs, net_i).upper()
        if net in L.NARN_NO_TRAFFIC:
            skipped += 1
            continue
        owner = txt(attrs, owner_i)
        pval = txt(attrs, pass_i)
        passenger = pval.upper() not in ("", "N", "NO", "0", "FALSE", "NONE")
        sub = txt(attrs, sub_i)
        trains, tsrc = None, "no FRA crossing found within {:g} mi".format(
            C.RAIL_CROSSING_SEARCH_MI)
        unsure = False
        if crossing_index is not None:
            total = sum(G.line_length(p) for p in parts)
            samples = [p for pts in parts for p in _densify(pts, max(50.0, total / 50.0))]
            same, other = [], []
            rd = crossing_index.reader
            for _dd, k in crossing_index.within(samples, search):
                ca, _kind, cparts = crossing_index.items[k]
                code = str(rd.get(ca, "railroadcode") or rd.get(ca, "railroad") or "")
                if owner and code and code.strip().upper() != owner.strip().upper():
                    continue
                t = L.trains_per_day(rd, ca)
                if t is None:
                    continue
                cid = rd.get(ca, "crossingid") or rd.get(ca, "crossing") or k
                xsub = rd.get(ca, "railroadsubdivision")
                px, py = cparts[0][0]
                on_this = (G.dist_to_lines(px, py, parts) or 1e30) <= on_line
                if on_this or (sub and xsub and L._norm_sub(sub) == L._norm_sub(xsub)):
                    same.append((t, cid))
                elif sub and xsub:
                    continue
                else:
                    other.append((t, cid))
            used = same[:5] or other[:5]
            if used:
                trains = max(t for t, _c in used)
                tsrc = "FRA crossings " + ", ".join(str(c) for _t, c in used[:3])
                if not same:
                    unsure = True
                    tsrc += " (same railroad nearby; line / subdivision not confirmed)"
        cls, basis, judge = L.nd_rail_class(passenger, trains)
        if unsure and not judge:
            judge, basis = True, basis + " - crossing on this line not confirmed"
        if net in L.NARN_OUT_OF_SERVICE:
            judge, basis = True, "out-of-service line (NARN NET = X) - " + basis
        out.append((g, {"railroad": owner, "subdivision": sub, "passenger": 1 if passenger else 0,
                        "passngr": pval, "trains_day": trains, "trains_src": tsrc, "level": cls,
                        "level_basis": basis, "judgment": 1 if judge else 0, "net": net}))
    return out, skipped


# ------------------------------------------------------------------ lifelines --
LIFELINE_FIELDS = [("name", "text"), ("category", "text"), ("kind", "text"), ("source", "text"),
                   ("src_id", "text")]


def lifeline_rows(sets):
    """sets: [(dataset, fixed category or None, [(feature, geometry)])] - agency sets first.
    Returns ([(point geometry, attrs)], {"not lifelines": n, "duplicates": n})."""
    from .facilities import canonical_category, _CARE, SCHOOLS
    tol = C.FACILITY_DEDUP_FT * FT
    kept = []
    grid = {}
    stats = {"not lifelines (care / schools / unknown)": 0, "duplicates": 0}
    cell = tol

    def key(x, y):
        return int(math.floor(x / cell)), int(math.floor(y / cell))
    for dataset, fixed, feats in sets:
        for f, g in feats:
            p = f.get("properties") or {}
            cat = fixed or canonical_category(str(_getp(p, "category") or "")) or \
                canonical_category(str(_getp(p, "type", "naics_desc", "fac_type") or ""))
            if not cat or cat not in C.LIFELINE_CATEGORIES or cat in _CARE or cat in SCHOOLS:
                stats["not lifelines (care / schools / unknown)"] += 1
                continue
            xy = G.point_on_surface(g)
            if xy is None:
                continue
            ki, kj = key(*xy)
            dup = False
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    for (ox, oy, ocat) in grid.get((ki + di, kj + dj), ()):
                        if ocat == cat and math.hypot(ox - xy[0], oy - xy[1]) <= tol:
                            dup = True
            if dup:
                stats["duplicates"] += 1
                continue
            grid.setdefault((ki, kj), []).append((xy[0], xy[1], cat))
            name = _getp(p, "name", "facility_name", "fac_name", "plant_name") or "(unnamed)"
            kept.append(({"type": "Point", "coordinates": xy},
                         {"name": str(name), "category": cat, "kind": cat, "source": dataset,
                          "src_id": _getp(p, "osm_id", "objectid", "id", "fid")}))
    return kept, stats


# ------------------------------------------------------------------ dams --
DAM_FIELDS = [("nid_id", "text"), ("name", "text"), ("hazard", "text"), ("height_ft", "real"),
              ("storage_acft", "real"), ("river", "text"), ("eap", "text"), ("state_id", "text"),
              ("county", "text"), ("source", "text")]


def dam_rows(feats):
    out = []
    for f, g in feats:
        p = f.get("properties") or {}
        xy = G.point_on_surface(g)
        if xy is None:
            continue
        out.append(({"type": "Point", "coordinates": xy},
                    {"nid_id": _getp(p, "nidid", "nid_id", "nid", "damid", "dam_id"),
                     "name": _getp(p, "name", "dam_name") or "(unnamed dam)",
                     "hazard": _getp(p, "hazard_potential", "hazard", "hazard_class",
                                     "haz_class", "hazardclass"),
                     "height_ft": _num(_getp(p, "nid_height", "dam_height", "height",
                                             "structural_height")),
                     "storage_acft": _num(_getp(p, "nid_storage", "max_storage", "storage",
                                                "normal_storage")),
                     "river": _getp(p, "river_or_stream", "river", "stream"),
                     "eap": _getp(p, "eap_prepared", "eap"),
                     "state_id": _getp(p, "stateid", "state_id", "other_structure_id"),
                     "county": _getp(p, "county", "countyname"),
                     "source": "NID_dams"}))
    return out


# ------------------------------------------------------------------ passthrough --
def _field_type(values):
    kinds = set()
    for v in values:
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        if isinstance(v, bool):
            kinds.add("int")
        elif isinstance(v, int):
            kinds.add("int")
        elif isinstance(v, float):
            kinds.add("real")
        else:
            kinds.add("text")
    if kinds == {"int"}:
        return "int"
    if kinds and kinds <= {"int", "real"}:
        return "real"
    return "text"


def passthrough(feats, extra=()):
    """(fields, [(geometry, attrs)]) with the features' own properties."""
    names = prop_names(f for f, _g in feats)
    used = {n.lower() for n, _t in extra}
    fields, mapping = [], []
    for n in names:
        out = n if n.lower() not in used | {"fid", "geom"} else n + "_src"
        used.add(out.lower())
        fields.append((out, _field_type((f.get("properties") or {}).get(n) for f, _g in feats)))
        mapping.append((n, out))
    rows = []
    for f, g in feats:
        p = f.get("properties") or {}
        rows.append((g, {out: p.get(n) for n, out in mapping}))
    return fields + list(extra), rows


# ------------------------------------------------------------------ export --
def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def dam_folder_of(out_path):
    """2.17.0: the dam folder of a package written in <dam folder>/data/ (damdata's
    suite_package.gpkg) - the folder holds dam.json - else ""."""
    d = os.path.dirname(os.path.abspath(out_path or ""))
    if os.path.basename(d).lower() != "data":
        return ""
    dam = os.path.dirname(d)
    return dam if os.path.isfile(os.path.join(dam, "dam.json")) else ""


def _same_file(a, b):
    try:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    except Exception:                                            # noqa
        return False


def dam_extras(dam_folder, source, extent, out_path, log=None):
    """2.17.0: {layer: (features lon / lat, from)} of DAM_EXTRAS for a dam folder, the DEM
    entry {"path", "source"} or None, and notes. The dam folder's data first, else the
    source's own data set. Never raises."""
    from .damsuite import damdata as DD
    out, notes, dem = {}, [], None
    try:
        rec = DD.DR.load(dam_folder)
    except Exception as e:                                       # noqa - a broken dam.json
        rec = None
        notes.append("dam.json not read ({})".format(e))
    data = DD.dam_data(dam_folder, rec) if rec is not None else {}
    section = DD.data_section(rec)
    for layer, role, ds, label in DAM_EXTRAS:
        path = data.get(role, "")
        if path and not _same_file(path.split("|")[0], out_path):
            try:
                feats = DD.read_vector_file(path, extent)
                out[layer] = (feats, "dam folder {} ({})".format(
                    DD.rel_path(dam_folder, path),
                    (section.get(role) or {}).get("source") or "dam.json data"))
                continue
            except Exception as e:                               # noqa
                notes.append("{}: {} not read ({})".format(label, path, e))
        try:
            if source is not None and source.has(ds):
                out[layer] = (source.features(ds, extent) or [],
                              "{} ({})".format(ds, source.note(ds, extent)))
        except Exception as e:                                   # noqa
            notes.append("{}: {} not read from the source ({})".format(label, ds, e))
    if data.get("dem"):
        dem = {"path": data["dem"], "source": (section.get("dem") or {}).get("source", "")}
    if log:
        for n in notes:
            log(n)
    return out, dem, notes


def _extra_layer(w, layer, feats, label):
    """Write lon / lat features with their own attributes into a layer of the writer.
    Returns the count."""
    feats = projected(feats)
    kinds = {G.kind(g) for _f, g in feats} - {None}
    gtype = {"polygon": "MULTIPOLYGON", "line": "MULTILINESTRING"}.get(
        next(iter(kinds)) if len(kinds) == 1 else "", "POINT")
    fields, rows = passthrough(feats, extra=[("source", "text")])
    lyr = w.layer(layer, gtype, fields, description=label)
    n = 0
    for g, a in rows:
        if gtype == "POINT" and G.kind(g) != "point":
            xy = G.point_on_surface(g)
            g = {"type": "Point", "coordinates": xy} if xy else None
        elif gtype != "POINT" and G.kind(g) == "point":
            continue
        a["source"] = label
        lyr.add(g, a)
        n += 1
    return n


def export(out_path, source, extent=None, structures="auto", mobile_rules=None, progress=None,
           log=None, dam_folder=None):
    """Write the screening package. source: PackSource / FolderSource (open_source).
    extent: (lon_min, lat_min, lon_max, lat_max) or None for North Dakota's box.
    structures: "auto" or one of the STRUCTURE_SETS names.
    dam_folder (2.17.0): the dam the package is for (adds DAM_EXTRAS and the DEM path);
    None = the dam folder when out_path is in <dam folder>/data/, "" = no dam folder.
    Returns a report dict {"path", "counts", "missing", "notes", "lines"}."""
    log = log or (lambda m: None)
    prog = progress or (lambda p, m="": None)
    extent = tuple(float(v) for v in (extent or ND_BBOX))
    if not (extent[0] < extent[2] and extent[1] < extent[3]):
        raise ExportError("empty area {}".format(extent))
    if not out_path.lower().endswith(".gpkg"):
        out_path += ".gpkg"
    counts, used, missing, notes = {}, {}, [], []
    dam = dam_folder_of(out_path) if dam_folder is None else (dam_folder or "")
    if dam and not os.path.isdir(dam):
        raise ExportError("dam folder not found: {}".format(dam))
    extras, dem, extra_notes = (dam_extras(dam, source, extent, out_path) if dam
                                else ({}, None, []))
    notes.extend(extra_notes)

    def get(name):
        feats = source.features(name, extent)
        if feats is None:
            return None
        used[name] = "{} features ({})".format(len(feats), source.note(name, extent))
        return projected(feats)

    w = G.GpkgWriter(out_path)
    try:
        # structures
        prog(2, "structures")
        order = [s[0] for s in STRUCTURE_SETS]
        if structures and structures != "auto":
            if structures not in order:
                raise ExportError("unknown structure data set {}".format(structures))
            order = [structures]
        ds = next((d for d in order if source.has(d)), None)
        if ds is None:
            missing.append("structures ({} not in the source)".format(" / ".join(order)))
        else:
            feats = get(ds)
            raw = [f for f, _g in feats]
            rows = standardize_structures(ds, raw, mobile_rules,
                                          lambda p, m: prog(2 + 0.3 * p, m))
            kinds = {G.kind(g) for g, _a in rows}
            gtype = "MULTIPOLYGON" if kinds == {"polygon"} else "POINT"
            lyr = w.layer("structures", gtype, STRUCTURE_FIELDS,
                          description="{} with ND Dam Hazard habitability".format(ds))
            pts = w.layer("structure_points", "POINT", STRUCTURE_FIELDS,
                          description="structures as points (receptors for ND Dam Breach Runner)")
            for g, a in rows:
                if gtype == "POINT" and G.kind(g) != "point":
                    xy = G.point_on_surface(g)
                    g2 = {"type": "Point", "coordinates": xy} if xy else None
                    lyr.add(g2, a)
                else:
                    lyr.add(g, a)
                xy = G.point_on_surface(g)
                pts.add({"type": "Point", "coordinates": xy} if xy else None, a)
            counts["structures"] = counts["structure_points"] = len(rows)
            n_no = sum(1 for _g, a in rows if a["habitable"] == 0)
            n_unv = sum(1 for _g, a in rows if a["nd_habitable"] == "Unverified")
            n_mob = sum(1 for _g, a in rows if a["mobile"])
            notes.append("structures from {}: {:,} ({:,} not habitable, {:,} Unverified written as "
                         "habitable = 1, {:,} manufactured / mobile homes)".format(
                             ds, len(rows), n_no, n_unv, n_mob))
        # roads
        prog(35, "roads")
        road_ds = next((d for d in ROAD_SETS + HPMS_SETS + ("ND_county_roads",)
                        if source.has(d)), None)
        if road_ds is None:
            missing.append("roads (ND_roads_NG911 / HPMS / county roads not in the source)")
        else:
            roads = get(road_ds)
            cell = C.AADT_SEARCH_FT * FT * 2
            idxs = []
            hp = next((d for d in HPMS_SETS if source.has(d) and d != road_ds), None)
            if hp:
                idxs.append(_Index(hp, get(hp), cell))
            elif source.has("NDDOT_traffic_counts"):
                idxs.append(_Index("NDDOT_traffic_counts", get("NDDOT_traffic_counts"), cell))
            if source.has("ND_county_roads") and road_ds != "ND_county_roads":
                idxs.append(_Index("ND_county_roads", get("ND_county_roads"), cell))
            if not idxs and road_ds in ROAD_SETS:
                missing.append("AADT layers (HPMS / NDDOT / county roads): only the roads' own "
                               "attributes and FRA crossings give AADT")
            xing = get("FRA_grade_crossings") if source.has("FRA_grade_crossings") else None
            xidx = _Index("FRA_grade_crossings", xing, 2000.0) if xing else None
            rows = road_rows(roads, idxs, xidx, lambda p, m: prog(35 + 0.25 * p, m))
            lyr = w.layer("roads", "MULTILINESTRING", ROAD_FIELDS,
                          description="{} with AADT (ND Dam Hazard rules)".format(road_ds))
            for g, a in rows:
                a["source"] = road_ds
                lyr.add(g, a)
            counts["roads"] = len(rows)
            n_a = sum(1 for _g, a in rows if a["aadt"] is not None)
            notes.append("roads from {}: {:,}, AADT found for {:,}; AADT from {}{} (unknown AADT "
                         "is empty)".format(road_ds, len(rows), n_a,
                                            ", ".join(i.label for i in idxs) or "own attributes",
                                            " and FRA crossings" if xidx else ""))
            # rail (FRA crossings reused)
        prog(62, "railroads")
        if not source.has("NARN_rail_lines"):
            missing.append("railroads (NARN_rail_lines not in the source)")
        else:
            rails = get("NARN_rail_lines")
            if road_ds is None:
                xing = get("FRA_grade_crossings") if source.has("FRA_grade_crossings") else None
            ridx = _Index("FRA_grade_crossings", xing, 2000.0) if xing else None
            if ridx is None:
                missing.append("FRA grade crossings: trains a day unknown for every line")
            rows, skipped = rail_rows(rails, ridx, lambda p, m: prog(62 + 0.1 * p, m))
            lyr = w.layer("railroads", "MULTILINESTRING", RAIL_FIELDS,
                          description="NARN lines with passenger and trains a day")
            for g, a in rows:
                a["source"] = "NARN_rail_lines"
                lyr.add(g, a)
            counts["railroads"] = len(rows)
            notes.append("railroads: {:,} lines, {:,} abandoned / removed / trail lines left out, "
                         "trains a day known for {:,}".format(
                             len(rows), skipped, sum(1 for _g, a in rows
                                                     if a["trains_day"] is not None)))
        # flowlines
        prog(74, "flowlines")
        if not source.has("NHD_flowlines"):
            missing.append("flowlines (NHD_flowlines not in the source)")
        else:
            feats = get("NHD_flowlines")
            fields, rows = passthrough(feats, extra=[("source", "text")])
            lyr = w.layer("flowlines", "MULTILINESTRING", fields,
                          description="USGS NHD flowlines (digitised in flow direction)")
            for g, a in rows:
                a["source"] = "NHD_flowlines"
                lyr.add(g, a)
            counts["flowlines"] = len(rows)
        # lifelines
        prog(82, "lifelines")
        sets = []
        for key, _label, _url, cat in C.LIFELINE_SOURCES:
            ds_name = "lifeline_" + key
            if source.has(ds_name):
                sets.append((ds_name, cat, get(ds_name)))
        if source.has("OSM_lifelines"):
            sets.append(("OSM_lifelines", None, get("OSM_lifelines")))
        absent = [k for k, _l, _u, _c in C.LIFELINE_SOURCES if not source.has("lifeline_" + k)]
        if absent:
            notes.append("lifeline layers not in the source: {} (HIFLD layers have no download "
                         "address since 2025; OpenStreetMap fills gaps)".format(", ".join(absent)))
        if not sets:
            missing.append("lifelines (no lifeline data set in the source)")
        else:
            rows, stats = lifeline_rows(sets)
            lyr = w.layer("lifelines", "POINT", LIFELINE_FIELDS,
                          description="ND 5.2.2 lifeline facilities")
            for g, a in rows:
                lyr.add(g, a)
            counts["lifelines"] = len(rows)
            notes.append("lifelines: {:,}; left out: {}".format(
                len(rows), ", ".join("{} {}".format(v, k) for k, v in stats.items())))
        # dams
        prog(92, "dams")
        if not source.has("NID_dams"):
            missing.append("dams (NID_dams not in the source)")
        else:
            rows = dam_rows(get("NID_dams"))
            lyr = w.layer("dams", "POINT", DAM_FIELDS, description="National Inventory of Dams")
            for g, a in rows:
                lyr.add(g, a)
            counts["dams"] = len(rows)
        # 2.17.0: an export for one dam folder - waterbodies, DWR dam lines / dams, DEM path
        if dam:
            prog(96, "dam folder data")
            for layer, _role, _ds, label in DAM_EXTRAS:
                if layer not in extras:
                    continue
                feats, src = extras[layer]
                counts[layer] = _extra_layer(w, layer, feats, label)
                used[layer] = "{} features ({})".format(len(feats), src)
                notes.append("{} from {}: {:,}".format(label, src, counts[layer]))
            if dem:
                notes.append("DEM (not copied, path recorded): {}".format(dem["path"]))
            gaps = ([m for m, key in zip(NOT_IN_PACK, ("waterbodies", "dam_lines"))
                     if key not in extras] + ([] if dem else [NOT_IN_PACK[2]]))
            missing.extend("{} - not in the dam folder's data or the source".format(m)
                           for m in gaps)
        else:
            missing.extend("{} - not in ND Dam Hazard's pack".format(m) for m in NOT_IN_PACK)
        info = w.table(INFO_TABLE, [("key", "text"), ("value", "text")],
                       description="what this package holds and where it came from")
        meta = [("format", FORMAT), ("format_version", str(FORMAT_VERSION)),
                ("created", _now()),
                ("created_by", "ND Dam Hazard {} (suiteexport.py)".format(C.PLUGIN_VERSION)),
                ("source", "{}: {}".format(source.kind, source.path)),
                ("area_lonlat", json.dumps([round(v, 6) for v in extent])),
                ("crs", "EPSG:5070"),
                ("counts", json.dumps(counts, sort_keys=True)),
                ("missing", json.dumps(missing)),
                ("notes", json.dumps(notes))]
        if dam:
            meta.append(("dam_folder", dam))
        if dem:
            rel = dem["path"]
            try:
                p0, _sep, lay = dem["path"].partition("|")
                rel = os.path.relpath(p0, os.path.dirname(os.path.abspath(out_path))).replace(
                    os.sep, "/") + ("|" + lay if lay else "")
            except ValueError:                                   # another drive (Windows)
                pass
            meta += [("dem", dem["path"]), ("dem_relative", rel),
                     ("dem_source", dem.get("source") or "")]
        meta += [("dataset:" + k, v) for k, v in sorted(used.items())]
        for k, v in meta:
            info.add(None, {"key": k, "value": v})
        w.close()
    except BaseException:
        w.abort()
        raise
    prog(100, "done")
    lines = ["Suite data package: {}".format(out_path),
             "Source: {} {}".format(source.kind, source.path),
             "Area (lon / lat): {:.4f}, {:.4f} to {:.4f}, {:.4f}".format(*extent)]
    lines += ["  {}: {:,}".format(k, counts[k]) for k in LAYERS + tuple(e[0] for e in DAM_EXTRAS)
              if k in counts]
    if dem:
        lines.append("  DEM (path only): {}".format(dem["path"]))
    lines += ["Note: " + n for n in notes]
    lines += ["Missing: " + m for m in missing]
    for ln in lines:
        log(ln)
    return {"path": out_path, "counts": counts, "missing": missing, "notes": notes,
            "datasets": used, "lines": lines}
