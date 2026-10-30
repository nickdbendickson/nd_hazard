# -*- coding: utf-8 -*-
"""2.30.0: "what would change it" - the items nearest a class boundary.

The owner's top pick of the review. The report has carried a single margin sentence since
v2.5 ("the dam would become HIGH if the depth at 1234 Main St rose 0.70 ft",
robustness.analyse); the question engineers actually argue about is the next one: WHICH items
are close, and what exactly would have to change for each of them. On a real dam that was ND
High only through two railroad stretches at 15 trains/day, the depth margins were beside the
point and the train count was everything - so the list has to carry road and railroad
stretches next to the structures, each with its own kind of change.

    rows = candidates(structs, roads, rails, nd, judgment_as_probable=False, one_d=False)
    for r in rows[:5]: print(r["label"], r["change"], "->", r["to_class"])

One row per item: what it is, where it stands now, the smallest change that would flip it, and
the class that would result. Decisions:

  * Items are ranked by how close they are in UNITS OF A PLAINLY NOTICEABLE STEP, because a
    foot of depth and a train a day cannot be compared directly: a depth margin is divided by
    STEP_DEPTH_FT (1 ft - the vertical error of a lidar crest and a coarse grid, the same
    figure classification.py calls "inside the error" when it marks a High provisional), an
    AADT margin by STEP_AADT (a plausible revision of a traffic count) and a train margin by
    STEP_TRAINS. These are screening choices of the engineer, not values from the ND standards.
  * RAISING rows (an item that would push the class UP) come before LOWERING rows. A lowering
    row is marked "would lower the class" and never applied anywhere: this suite only moves
    results in the conservative direction, and a lower class is the engineer's decision.
  * The depth margins are the ones the classification itself used (rec["margin_high"] /
    ["margin_judg"], the ACER depth gap at the structure's maximum velocity), so a row cannot
    disagree with the class it refers to. The velocity alternative is computed from the same
    ACER curves when they are reachable; without them the row carries the depth change only.
  * A structure with no velocity (1D / depth-only run) is measured against the 2-ft rule.
  * Nothing here reads a raster, a layer or a file, and nothing raises: a caller in a UI
    callback can use it directly.
"""

ND_HIGH, ND_MEDIUM, ND_LOW = "High", "Medium", "Low"
ND_1D_DEPTH_FT = 2.0                 # ND 5.2.1.1 (1D model)
AADT_HIGH_ABOVE = 2000               # ND 5.2.1.2
AADT_MEDIUM_FROM = 500               # ND 5.2.1.2
# road classes that are High / Medium whatever the traffic count says (linear.ROAD_*)
ROAD_INTERSTATE = "Interstate"
ROADS_ALWAYS_MEDIUM = ("US Highway", "State Highway")
TRAINS_HIGH_PER_DAY = 10             # ND 5.2.1.3
# one "plainly noticeable step" per kind of change - the ranking's common unit
STEP_DEPTH_FT = 1.0
STEP_AADT = 100.0
STEP_TRAINS = 1.0
# an unknown count (no AADT, no train count) has no margin at all, so it cannot be called
# "near" a boundary: it is listed after everything with a measured margin, as the data gap it is
UNKNOWN_RANK = 90.0
MAX_ROWS = 5
UP, DOWN = "up", "down"
HEAD = ["Item", "Now", "What would change it", "Resulting class", "Direction"]


def _f(v):
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def _label(rec):
    """"structure 12 (1234 Main St)" - the address when the structure has one."""
    addr = ""
    try:
        from .structures import address_of
        addr = (address_of(rec)[0] or "").strip()
    except Exception:                                            # noqa - no QGIS / no sources
        addr = ""
    if not addr:
        # a record that came from a sheet or a CSV carries the address itself
        addr = str(rec.get("address") or "").strip()
    what = str(rec.get("occupancy") or rec.get("fema_category") or "").strip()
    tail = " ({})".format(addr) if addr else (" - {}".format(what[:40]) if what else "")
    return "structure {}{}".format(rec.get("id"), tail)


def _zone_now(rec, one_d):
    out = str(rec.get("outcome") or "").strip()
    if one_d:
        d = _f(rec.get("max_depth"))
        return "{}{}".format(out or "wet", "" if d is None else ", depth {:.2f} ft".format(d))
    d, v = _f(rec.get("max_depth")), _f(rec.get("max_vel"))
    bits = [out or "wet"]
    if d is not None:
        bits.append("depth {:.2f} ft".format(d))
    if v is not None:
        bits.append("velocity {:.2f} ft/s".format(v))
    return ", ".join(bits)


def _velocity_to_flip(rec, judgment_as_probable):
    """The velocity increase (ft/s) that would put this structure in the class above, from the
    same ACER curves the classification used, or None when they are not reachable / no
    increase inside the figure does it."""
    d, v = _f(rec.get("max_depth")), _f(rec.get("max_vel"))
    if d is None or v is None or d <= 0:
        return None
    try:
        from .helpers import classify_scalar
        from .constants import CLASS_HIGH, CLASS_JUDGMENT, CURVE_V_MAX
        from .structures import curve_type
        from .structures import TYPE_MOBILE
    except Exception:                                            # noqa - no QGIS here
        return None
    try:
        kind = "mobile" if curve_type(rec.get("acer_type")) == TYPE_MOBILE else "found"
        want = CLASS_HIGH if judgment_as_probable else CLASS_JUDGMENT
        v_max = CURVE_V_MAX.get(kind)
        hi = float(v_max) if v_max else max(v * 4.0, v + 20.0)
        if classify_scalar(d, hi, kind + "_low", kind + "_high") > want:
            return None                   # nothing inside the figure reaches the class above
        lo = v
        for _i in range(40):              # bisection to 0.01 ft/s
            mid = (lo + hi) / 2.0
            if classify_scalar(d, mid, kind + "_low", kind + "_high") <= want:
                hi = mid
            else:
                lo = mid
            if hi - lo < 0.01:
                break
        step = hi - v
        return step if step > 0.005 else None
    except Exception:                                            # noqa - a curve table problem
        return None


def _structure_rows(structs, judgment_as_probable, one_d):
    """Raising and lowering rows for the habitable structures."""
    out = []
    for rec in structs or ():
        if rec.get("habitable") != "Yes" or rec.get("dwelling_extra"):
            continue
        d = _f(rec.get("max_depth"))
        if d is None or d <= 0:
            continue                      # dry: its row would be "flood it first"
        if one_d:
            margin = ND_1D_DEPTH_FT - d
            boundary = "the 1D 2-ft depth rule (ND 5.2.1.1)"
        else:
            key = "margin_judg" if judgment_as_probable else "margin_high"
            margin = _f(rec.get(key))
            boundary = ("the ACER judgment boundary" if judgment_as_probable
                        else "the ACER high-danger boundary")
        if margin is None:
            continue
        label, now = _label(rec), _zone_now(rec, one_d)
        if margin > 0:                    # below the boundary: raising it makes the dam High
            change = "depth +{:.2f} ft (to {})".format(margin, boundary)
            dv = None if one_d else _velocity_to_flip(rec, judgment_as_probable)
            if dv is not None:
                change += ", or velocity +{:.2f} ft/s at the same depth".format(dv)
            out.append({"kind": "structure", "id": rec.get("id"), "label": label, "now": now,
                        "change": change, "to_class": ND_HIGH, "direction": UP,
                        "margin": margin, "unit": "ft",
                        "rank": margin / STEP_DEPTH_FT,
                        "geom": rec.get("geom")})
        else:                             # at or past it: this item carries the High
            out.append({"kind": "structure", "id": rec.get("id"), "label": label, "now": now,
                        "change": "depth -{:.2f} ft (back below {})".format(-margin, boundary),
                        "to_class": ND_MEDIUM, "direction": DOWN, "margin": -margin,
                        "unit": "ft", "rank": -margin / STEP_DEPTH_FT,
                        "geom": rec.get("geom")})
    return out


def _road_label(r):
    name = str(r.get("name") or "unnamed road").strip()
    length = _f(r.get("length_ft"))
    return "{}{}".format(name, " ({:.0f} ft overtopped)".format(length)
                         if length is not None else "")


def _road_rows(roads):
    out = []
    for r in roads or ():
        aadt = _f(r.get("aadt"))
        cls = str(r.get("nd_class") or "").title()
        label, now = _road_label(r), "{}, AADT {}".format(
            cls or "wet", "{:,.0f}".format(aadt) if aadt is not None else "unknown")
        if aadt is None:
            out.append({"kind": "road", "id": r.get("id"), "label": label, "now": now,
                        "change": "an AADT of {:,} or more (Medium) or over {:,} (High) - the "
                                  "count is unknown; ask the DOT".format(AADT_MEDIUM_FROM,
                                                                         AADT_HIGH_ABOVE),
                        "to_class": ND_HIGH, "direction": UP, "margin": None, "unit": "AADT",
                        "rank": UNKNOWN_RANK, "geom": r.get("geom")})
            continue
        if str(r.get("road_class") or "") == ROAD_INTERSTATE:
            continue                      # an Interstate is High whatever the count
        if aadt <= AADT_HIGH_ABOVE:
            gap = AADT_HIGH_ABOVE + 1 - aadt
            out.append({"kind": "road", "id": r.get("id"), "label": label, "now": now,
                        "change": "AADT +{:,.0f} (over {:,})".format(gap, AADT_HIGH_ABOVE),
                        "to_class": ND_HIGH, "direction": UP, "margin": gap, "unit": "AADT",
                        "rank": gap / STEP_AADT, "geom": r.get("geom")})
        if aadt >= AADT_MEDIUM_FROM and str(r.get("road_class") or "") \
                not in ROADS_ALWAYS_MEDIUM:
            gap = aadt - AADT_MEDIUM_FROM + 1
            out.append({"kind": "road", "id": r.get("id"), "label": label, "now": now,
                        "change": "AADT -{:,.0f} (under {:,})".format(gap, AADT_MEDIUM_FROM),
                        "to_class": ND_LOW if cls == ND_MEDIUM else cls or ND_LOW,
                        "direction": DOWN, "margin": gap, "unit": "AADT",
                        "rank": gap / STEP_AADT, "geom": r.get("geom")})
    return out


def _rail_label(r):
    return "{} {}".format(str(r.get("railroad") or "railroad").strip(),
                          str(r.get("subdivision") or "").strip()).strip()


def _rail_rows(rails):
    out = []
    for r in rails or ():
        trains = _f(r.get("trains"))
        cls = str(r.get("nd_class") or "").title()
        passenger = bool(r.get("passenger"))
        label = _rail_label(r)
        now = "{}, {}".format(cls or "wet", "passenger service" if passenger else
                              ("{:.0f} trains/day".format(trains) if trains is not None
                               else "trains/day unknown"))
        if passenger:
            continue                      # a passenger line is High whatever the count
        if trains is None:
            out.append({"kind": "rail", "id": r.get("id"), "label": label, "now": now,
                        "change": "{} or more trains a day, or any passenger service - the "
                                  "count is unknown; ask the railroad".format(
                                      TRAINS_HIGH_PER_DAY),
                        "to_class": ND_HIGH, "direction": UP, "margin": None,
                        "unit": "trains/day", "rank": UNKNOWN_RANK, "geom": r.get("geom")})
            continue
        if trains < TRAINS_HIGH_PER_DAY:
            gap = TRAINS_HIGH_PER_DAY - trains
            out.append({"kind": "rail", "id": r.get("id"), "label": label, "now": now,
                        "change": "+{:.0f} trains/day (to {})".format(gap,
                                                                      TRAINS_HIGH_PER_DAY),
                        "to_class": ND_HIGH, "direction": UP, "margin": gap,
                        "unit": "trains/day", "rank": gap / STEP_TRAINS,
                        "geom": r.get("geom")})
        else:
            gap = trains - TRAINS_HIGH_PER_DAY + 1
            out.append({"kind": "rail", "id": r.get("id"), "label": label, "now": now,
                        "change": "-{:.0f} trains/day (under {})".format(
                            gap, TRAINS_HIGH_PER_DAY),
                        "to_class": ND_MEDIUM, "direction": DOWN, "margin": gap,
                        "unit": "trains/day", "rank": gap / STEP_TRAINS,
                        "geom": r.get("geom")})
    return out


def candidates(structs=(), roads=(), rails=(), nd=None, judgment_as_probable=False, one_d=False,
               limit=MAX_ROWS, include_lowering=True):
    """The items nearest a class boundary, closest first: raising rows before lowering rows.
    `limit` is the number of RAISING rows kept (the lowering rows get the same allowance), as
    the five nearest items of the review's feature 3."""
    rows = (_structure_rows(structs, judgment_as_probable, one_d) + _road_rows(roads)
            + _rail_rows(rails))
    up = sorted([r for r in rows if r["direction"] == UP], key=lambda r: (r["rank"], r["label"]))
    down = sorted([r for r in rows if r["direction"] == DOWN],
                  key=lambda r: (r["rank"], r["label"]))
    n = max(1, int(limit))
    out = up[:n]
    if include_lowering:
        out += down[:n]
    return out


def table_rows(rows):
    """[[item, now, change, resulting class, direction]] for a table / a CSV."""
    return [[r["label"], r["now"], r["change"], r["to_class"],
             "would raise the class" if r["direction"] == UP else "would lower the class"]
            for r in rows or ()]


def lines(rows, cls=""):
    """The summary / report section."""
    if not rows:
        return ["No item is near a class boundary (or nothing was evaluated)."]
    out = []
    up = [r for r in rows if r["direction"] == UP]
    down = [r for r in rows if r["direction"] == DOWN]
    if up:
        out.append("Nearest to a HIGHER class (the {} closest {}):".format(
            len(up), "item" if len(up) == 1 else "items"))
        for r in up:
            out.append("  {} [{}] - {} -> {}".format(r["label"], r["now"], r["change"],
                                                     r["to_class"]))
    if down:
        out.append("Carrying the class now (what would have to be LOWER - the engineer's "
                   "decision, never the plugin's):")
        for r in down:
            out.append("  {} [{}] - {} -> {}".format(r["label"], r["now"], r["change"],
                                                     r["to_class"]))
    out.append("Ranking: the change needed, in units of 1 ft of depth / {:,.0f} AADT / {:g} "
               "train per day - screening choices of the engineer, not values of the ND "
               "standards. A lower class is never applied by the plugin."
               .format(STEP_AADT, STEP_TRAINS))
    return out


def write_csv(path, rows):
    """The list as a CSV next to the other QA tables. Returns the path."""
    import csv
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(HEAD)
        for row in table_rows(rows):
            w.writerow(row)
    return path
