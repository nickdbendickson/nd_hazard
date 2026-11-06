"""Merge two NSI versions (2022 and 2026) into one point inventory.

Pure Python with no QGIS imports, so it can be tested outside QGIS.

NSI point ids (fd_id) and footprint ids (ftprntid) are not carried over between
NSI versions, so structures are matched by location:

  * A 2022 point that has any 2026 point within `match_m` metres overlaps the
    2026 inventory, and the 2026 data wins: the 2022 point is folded into the
    2026 point that best matches it. "Best" is the shortest distance plus
    `category_penalty_m` when the occupancy categories differ (RES vs AGR vs
    COM ...), so a 2022 house prefers the 2026 house next to it over a 2026 barn
    that is slightly closer.
  * The 2026 point records what was folded into it: the best 2022 match's id,
    occupancy type, building type and distance, how many 2022 points were
    folded in, and whether any of them was a mobile home (occtype RES2 or
    bldgtype H), so evidence the newer version dropped is not lost.
  * A 2022 point with no 2026 point within `match_m` is kept with its 2022 data
    and nsi_version = 2022. Some of these are demolished or misplaced; keeping
    them is the conservative choice.
  * Every 2026 point is kept.

The two versions place different numbers of points on the same building
(apartments, stacked units), so one-to-one matching would leave thousands of
2022 points "unmatched" next to a 2026 point and count those buildings and
their population twice. Folding avoids that.

Field names follow the 2026 schema; the one renamed 2022 field
(ground_elv_meters) is mapped to its 2026 name (grnd_elv_m).
"""

import math

DEFAULT_MATCH_M = 30.0
CATEGORY_PENALTY_M = 15.0

# 2022 field name -> 2026 field name
RENAMES_2022 = {"ground_elv_meters": "grnd_elv_m"}

# Fields added to every merged point.
MERGE_FIELDS = ("nsi_version", "nsi22_fd_id", "nsi22_occtype", "nsi22_bldgtype",
                "nsi22_match_m", "nsi22_count", "nsi22_mobile")

_M_PER_DEG_LAT = 111_132.0


def occ_category(occtype):
    """'RES' for 'RES1-1SNB', 'AGR' for 'AGR1' ..."""
    return str(occtype or "").strip().upper()[:3]


def _is_mobile(props):
    """NSI mobile-home evidence: occtype RES2 or bldgtype H (manufactured)."""
    return (str(props.get("occtype") or "").upper().startswith("RES2")
            or str(props.get("bldgtype") or "").strip().upper() == "H")


def _points(features):
    """(lon, lat, category) for each feature; None for features without a point."""
    out = []
    for f in features:
        geom = f.get("geometry") or {}
        coords = geom.get("coordinates") if geom.get("type") == "Point" else None
        if not coords or len(coords) < 2:
            out.append(None)
            continue
        props = f.get("properties") or {}
        out.append((float(coords[0]), float(coords[1]), occ_category(props.get("occtype"))))
    return out


def match_points(old_pts, new_pts, match_m=DEFAULT_MATCH_M,
                 category_penalty_m=CATEGORY_PENALTY_M):
    """Best new point for each old point within match_m (several old points can
    share one new point).

    old_pts / new_pts: lists of (lon, lat, category) or None.
    Returns {old_index: (new_index, distance_m, cost)}.
    """
    valid = [p for p in old_pts + new_pts if p is not None]
    if not valid or match_m <= 0:
        return {}
    lat0 = sum(p[1] for p in valid) / len(valid)
    m_per_deg_lon = _M_PER_DEG_LAT * math.cos(math.radians(lat0))
    cell = float(match_m)

    def xy(p):
        return p[0] * m_per_deg_lon, p[1] * _M_PER_DEG_LAT

    grid = {}
    new_xy = [None] * len(new_pts)
    for j, p in enumerate(new_pts):
        if p is None:
            continue
        x, y = xy(p)
        new_xy[j] = (x, y)
        grid.setdefault((int(math.floor(x / cell)), int(math.floor(y / cell))), []).append(j)

    result = {}
    for i, p in enumerate(old_pts):
        if p is None:
            continue
        x, y = xy(p)
        gx, gy = int(math.floor(x / cell)), int(math.floor(y / cell))
        best = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in grid.get((gx + dx, gy + dy), ()):
                    nx, ny = new_xy[j]
                    d = math.hypot(nx - x, ny - y)
                    if d > match_m:
                        continue
                    cost = d + (category_penalty_m if p[2] != new_pts[j][2] else 0.0)
                    if best is None or (cost, d, j) < best:
                        best = (cost, d, j)
        if best is not None:
            result[i] = (best[2], best[1], best[0])
    return result


def _props_2022(props):
    out = {}
    for k, v in props.items():
        out[RENAMES_2022.get(k, k)] = v
    return out


def merge_versions(features_2022, features_2026, match_m=DEFAULT_MATCH_M,
                   category_penalty_m=CATEGORY_PENALTY_M):
    """Merge two lists of NSI GeoJSON point features.

    Returns (merged_features, stats). Merged features carry the MERGE_FIELDS.
    """
    old_pts = _points(features_2022)
    new_pts = _points(features_2026)
    matches = match_points(old_pts, new_pts, match_m, category_penalty_m)
    folded = {}  # new index -> [(cost, distance, old index), ...]
    for i, (j, d, cost) in matches.items():
        folded.setdefault(j, []).append((cost, d, i))

    merged = []
    for j, f in enumerate(features_2026):
        if new_pts[j] is None:
            continue
        props = dict(f.get("properties") or {})
        props["nsi_version"] = "2026"
        group = sorted(folded.get(j, ()))
        if group:
            _cost, d, i = group[0]
            old = features_2022[i].get("properties") or {}
            props["nsi22_fd_id"] = old.get("fd_id")
            props["nsi22_occtype"] = old.get("occtype")
            props["nsi22_bldgtype"] = old.get("bldgtype")
            props["nsi22_match_m"] = round(d, 1)
            props["nsi22_count"] = len(group)
            props["nsi22_mobile"] = int(any(
                _is_mobile(features_2022[k].get("properties") or {}) for _c, _d, k in group))
        else:
            props.update({"nsi22_fd_id": None, "nsi22_occtype": None, "nsi22_bldgtype": None,
                          "nsi22_match_m": None, "nsi22_count": 0, "nsi22_mobile": 0})
        merged.append({"type": "Feature", "geometry": f["geometry"], "properties": props})

    added = 0
    for i, f in enumerate(features_2022):
        if old_pts[i] is None or i in matches:
            continue
        old = f.get("properties") or {}
        props = _props_2022(old)
        props["nsi_version"] = "2022"
        props["nsi22_fd_id"] = old.get("fd_id")
        props["nsi22_occtype"] = old.get("occtype")
        props["nsi22_bldgtype"] = old.get("bldgtype")
        props["nsi22_match_m"] = None
        props["nsi22_count"] = 1
        props["nsi22_mobile"] = int(_is_mobile(old))
        merged.append({"type": "Feature", "geometry": f["geometry"], "properties": props})
        added += 1

    # One schema for every feature, so GDAL writes a single consistent table.
    keys = []
    seen = set()
    for f in merged:
        for k in f["properties"]:
            if k not in seen:
                seen.add(k)
                keys.append(k)
    for f in merged:
        p = f["properties"]
        for k in keys:
            p.setdefault(k, None)

    stats = {
        "points_2022": sum(1 for p in old_pts if p is not None),
        "points_2026": sum(1 for p in new_pts if p is not None),
        "folded_2022": len(matches),
        "kept_2022_only": added,
        "merged_total": len(merged),
        "mobile_2022": sum(1 for f in features_2022 if _is_mobile(f.get("properties") or {})),
        "mobile_2026": sum(1 for f in features_2026 if _is_mobile(f.get("properties") or {})),
        "mobile_2022_folded_into_2026_other": sum(
            1 for i, (j, _d, _c) in matches.items()
            if _is_mobile(features_2022[i].get("properties") or {})
            and not _is_mobile(features_2026[j].get("properties") or {})),
    }
    stats["mobile_merged"] = sum(
        1 for f in merged if _is_mobile(f["properties"]) or f["properties"]["nsi22_mobile"])
    return merged, stats
