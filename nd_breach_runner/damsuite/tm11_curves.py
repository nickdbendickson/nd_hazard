"""The ND Dam Safety Standards depth-velocity flood danger curves, as one table for the suite.

REG_05.2026 Figure 5.1 (houses built on foundations) and Figure 5.2 (mobile homes), which
reproduce Reclamation (1988) ACER Technical Memorandum No. 11, Figures 2 and 3. The table was
digitized from the REG_05.2026 figures by the engineer (the file flood_danger_classifier.py,
stated accuracy +/-0.06 ft) and is copied here verbatim: velocity ft/s, depth ft, the
low / judgment boundary ("low") and the judgment / high boundary ("high").

Independent check (2026-10-01, not a person's check): the zone colours of screenshots of the two
REG_05.2026 figures were traced pixel by pixel (Fig. 5.1 about 0.025 ft a pixel, Fig. 5.2 about
0.007 ft) and compared with this table at its velocities. Houses: median difference 0.03 ft,
95 % of points within 0.05 ft (high line) and 0.09 ft (low line); the low line's tail differs
most - the table reaches 0 ft at 19 ft/s, the figure shows a sliver about 0.06 ft thick to
about 20.5 ft/s. Mobile homes: median 0.008 ft (high) and 0.004 ft (low), 95 % within 0.02 ft.
Columns crossing the zone labels were left out of that comparison.

The suite's rule (used by all three plugins):
  * between the tabulated velocities the boundary is a straight line (linear interpolation);
  * a velocity above the figure's range (25 / 16 ft/s) is read at the range's last value, a
    negative velocity at 0 (ND Dam Hazard; ND Dam Breach Runner / Screening's nd_hazard.py
    instead count any point beyond the chart's axes as high danger);
  * a depth ON a boundary takes the HIGHER zone (d >= curve), because ND 5.3 gives a borderline
    case the higher class. The uploaded classify() puts a point on the line in the LOWER zone
    (d <= curve); the two differ only for a depth exactly on a line.

Pure Python standard library.
"""
import hashlib
import json

FIGURES = {
    "house": "ND REG_05.2026 Figure 5.1 - houses built on foundations (ACER TM 11 Fig. 2)",
    "mobile": "ND REG_05.2026 Figure 5.2 - mobile homes (ACER TM 11 Fig. 3)",
}
AXES = {"house": (25.0, 10.0), "mobile": (16.0, 3.0)}      # (velocity ft/s, depth ft) maxima
SOURCE = ("ND Dam Safety Standards REG_05.2026 Figures 5.1 and 5.2 (Reclamation 1988, ACER TM 11 "
          "Figures 2 and 3), digitized by the engineer (flood_danger_classifier.py, stated "
          "accuracy +/-0.06 ft)")
STATED_ACCURACY_FT = 0.06
# The engineer's sign-off of the chart file (release 1.18.1, 2026-10-12): one engineer is enough;
# a second checker may be recorded but is not required. The shipped acer_tm11_charts.json of the
# Breach Runner and the Screening is default_chart_file(), so the chart method runs out of the box.
SIGNED_OFF_BY = "Nicholas Bendickson"
SIGNED_OFF_ON = "2026-10-12"
ALIASES = {"houses": "house", "found": "house", "foundation": "house", "house_on_foundation": "house",
           "mobile_home": "mobile", "mobile_homes": "mobile", "mh": "mobile"}
LOW, JUDGMENT, HIGH = "low", "judgment", "high"

CURVES = {
    "house": {
        "v": (0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5,
              5.0, 5.5, 6.0, 6.5, 7.0, 7.5, 8.0, 8.5, 9.0, 9.5,
              10.0, 10.5, 11.0, 11.5, 12.0, 12.5, 13.0, 13.5, 14.0, 14.5,
              15.0, 15.5, 16.0, 16.5, 17.0, 17.5, 18.0, 18.5, 19.0, 19.5,
              20.0, 20.5, 21.0, 21.5, 22.0, 22.5, 23.0, 23.5, 24.0, 24.5,
              25.0),
        "low": (2.94, 2.94, 2.916, 2.866, 2.816, 2.767, 2.711, 2.618, 2.568, 2.469,
                2.417, 2.32, 2.246, 2.146, 2.049, 1.95, 1.873, 1.774, 1.675, 1.576,
                1.476, 1.377, 1.278, 1.179, 1.091, 1.005, 0.906, 0.806, 0.732, 0.633,
                0.558, 0.484, 0.429, 0.355, 0.285, 0.236, 0.136, 0.062, 0.0, 0.0,
                0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                0.0),
        "high": (5.968, 5.968, 5.968, 5.918, 5.893, 5.819, 5.769, 5.695, 5.616, 5.521,
                 5.422, 5.298, 5.174, 5.074, 4.95, 4.804, 4.677, 4.529, 4.38, 4.231,
                 4.082, 3.917, 3.759, 3.61, 3.448, 3.288, 3.128, 2.965, 2.807, 2.659,
                 2.494, 2.345, 2.196, 2.072, 1.923, 1.797, 1.65, 1.55, 1.427, 1.304,
                 1.203, 1.129, 1.03, 0.955, 0.881, 0.839, 0.782, 0.757, 0.732, 0.732,
                 0.732),
    },
    "mobile": {
        "v": (0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.25,
              2.5, 2.75, 3.0, 3.25, 3.5, 3.75, 4.0, 4.25, 4.5, 4.75,
              5.0, 5.25, 5.5, 5.75, 6.0, 6.25, 6.5, 6.75, 7.0, 7.25,
              7.5, 7.75, 8.0, 8.25, 8.5, 8.75, 9.0, 9.25, 9.5, 9.75,
              10.0, 10.25, 10.5, 10.75, 11.0, 11.25, 11.5, 11.75, 12.0, 12.25,
              12.5, 12.75, 13.0, 13.25, 13.5, 13.75, 14.0, 14.25, 14.5, 14.75,
              15.0, 15.25, 15.5, 15.75, 16.0),
        "low": (1.905, 1.905, 1.905, 1.905, 1.905, 1.905, 1.905, 1.905, 1.904, 1.897,
                1.89, 1.882, 1.882, 1.875, 1.868, 1.86, 1.853, 1.838, 1.83, 1.823,
                1.816, 1.801, 1.793, 1.778, 1.771, 1.756, 1.741, 1.734, 1.719, 1.704,
                1.697, 1.682, 1.667, 1.652, 1.645, 1.63, 1.615, 1.6, 1.593, 1.578,
                1.563, 1.548, 1.541, 1.526, 1.519, 1.504, 1.489, 1.481, 1.472, 1.459,
                1.452, 1.439, 1.429, 1.422, 1.415, 1.407, 1.406, 1.392, 1.392, 1.385,
                1.377, 1.377, 1.377, 1.377, 1.37),
        "high": (2.499, 2.499, 2.499, 2.499, 2.499, 2.499, 2.499, 2.499, 2.499, 2.491,
                 2.491, 2.484, 2.476, 2.475, 2.469, 2.462, 2.454, 2.442, 2.432, 2.425,
                 2.417, 2.403, 2.395, 2.387, 2.373, 2.365, 2.35, 2.343, 2.328, 2.313,
                 2.298, 2.291, 2.276, 2.261, 2.249, 2.239, 2.224, 2.209, 2.194, 2.179,
                 2.165, 2.15, 2.135, 2.12, 2.106, 2.098, 2.083, 2.068, 2.053, 2.038,
                 2.024, 2.011, 1.994, 1.986, 1.972, 1.957, 1.949, 1.934, 1.92, 1.912,
                 1.897, 1.89, 1.876, 1.868, 1.86),
    },
}


def kind_of(name):
    k = str(name).strip().lower()
    k = ALIASES.get(k, k)
    if k not in CURVES:
        raise KeyError("no flood danger curve '{}' (house or mobile)".format(name))
    return k


def _interp(x, xs, ys):
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    lo, hi = 0, len(xs) - 1
    while hi - lo > 1:                       # bisection: xs is increasing
        mid = (lo + hi) // 2
        if xs[mid] <= x:
            lo = mid
        else:
            hi = mid
    t = (x - xs[lo]) / (xs[hi] - xs[lo])
    return ys[lo] + t * (ys[hi] - ys[lo])


def depth_at(kind, level, velocity_fts):
    """Depth (ft) of the "low" or "high" boundary of chart `kind` at a velocity (ft/s)."""
    c = CURVES[kind_of(kind)]
    if level not in (LOW, HIGH):
        raise KeyError("level must be 'low' or 'high'")
    v = max(0.0, float(velocity_fts))
    return _interp(v, c["v"], c[level])


def thresholds(kind, velocity_fts):
    """(low boundary ft, high boundary ft) at a velocity."""
    return depth_at(kind, LOW, velocity_fts), depth_at(kind, HIGH, velocity_fts)


def zone(kind, depth_ft, velocity_fts):
    """"low", "judgment" or "high" - a depth on a boundary takes the higher zone (ND 5.3)."""
    lo, hi = thresholds(kind, velocity_fts)
    d = float(depth_ft)
    if d >= hi:
        return HIGH
    if d >= lo and d > 0:
        return JUDGMENT
    return LOW


def polygons(kind):
    """(high zone polygon, judgment zone polygon) as [[velocity ft/s, depth ft], ...] within the
    chart's axes - the form ND Dam Breach Runner / Screening's nd_hazard.py reads."""
    k = kind_of(kind)
    c, (vmax, dmax) = CURVES[k], AXES[k]
    hi = [[v, d] for v, d in zip(c["v"], c["high"])]
    lo = [[v, d] for v, d in zip(c["v"], c["low"])]
    high = hi + [[vmax, dmax], [0.0, dmax]]
    judgment = lo + hi[::-1]
    return high, judgment


def table_sha256():
    """SHA-256 of the table (canonical JSON) - recorded with every classification."""
    text = json.dumps({k: {f: list(v) for f, v in c.items()} for k, c in sorted(CURVES.items())},
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def check():
    """Problems of the table ([] = consistent): increasing velocities ending at the axis
    maximum, depths within the axes, low <= high."""
    out = []
    for k, c in CURVES.items():
        v, lo, hi = c["v"], c["low"], c["high"]
        vmax, dmax = AXES[k]
        if not (len(v) == len(lo) == len(hi)) or len(v) < 2:
            out.append("{}: columns of different length".format(k))
            continue
        if any(b <= a for a, b in zip(v, v[1:])):
            out.append("{}: velocities not increasing".format(k))
        if v[0] != 0.0 or v[-1] != vmax:
            out.append("{}: velocities do not run 0-{:g} ft/s".format(k, vmax))
        if any(not (0.0 <= x <= dmax) for x in list(lo) + list(hi)):
            out.append("{}: a depth outside 0-{:g} ft".format(k, dmax))
        if any(a > b for a, b in zip(lo, hi)):
            out.append("{}: low boundary above the high boundary".format(k))
    return out


def chart_file(digitized_by="", checked_by="", digitized_on="", notes_extra="", signed_off_by="",
               signed_off_on=""):
    """The acer_tm11_charts.json content (nd_hazard.py format) from this table. Status
    "digitized" when an engineer is named (signed_off_by, else digitized_by, else checked_by:
    one engineer is enough since 1.18.1; a second checker is optional); otherwise "not digitized"
    with the polygons kept, so nd_hazard.py's chart method waits for the sign-off."""
    by, chk = str(digitized_by or "").strip(), str(checked_by or "").strip()
    signer = str(signed_off_by or "").strip() or by or chk
    signed = bool(signer)
    charts = {}
    for k, title in (("house", "Houses built on foundations"), ("mobile", "Mobile homes")):
        high, judgment = polygons(k)
        charts[k] = {"title": title, "figure": FIGURES[k], "v_max_fts": AXES[k][0],
                     "d_max_ft": AXES[k][1], "high": high, "judgment": judgment,
                     "tolerance": 0.01}
    notes = ("Zone polygons from damsuite.tm11_curves (table SHA-256 {}): the table digitized from "
             "REG_05.2026 Figs 5.1 / 5.2, linear between the tabulated velocities. Axes (velocity, "
             "depth): ND Fig. 5.1 = TM 11 Fig. 2 velocity 0-25 ft/s, depth 0-10 ft; ND Fig. 5.2 = "
             "TM 11 Fig. 3 velocity 0-16 ft/s, depth 0-3 ft. ".format(table_sha256()))
    if not signed:
        notes += ("Status stays 'not digitized' until the engineer who signs the curves off is "
                  "recorded (signed_off_by, or ND Dam Hazard > Chart sign-off; one engineer is "
                  "enough, a second checker is optional). ")
    else:
        notes += "Signed off by {}{}{}. ".format(
            signer, " on " + str(signed_off_on or digitized_on) if (signed_off_on or digitized_on)
            else "", "; checked by " + chk if chk and chk.lower() != signer.lower() else "")
    return {"status": "digitized" if signed else "not digitized",
            "source": "Reclamation (1988), ACER Technical Memorandum No. 11, Downstream Hazard "
                      "Classification Guidelines: Fig. 2 houses built on foundations (ND Fig. "
                      "5.1), Fig. 3 mobile homes (ND Fig. 5.2)",
            "digitized_from": SOURCE, "digitized_by": by, "digitized_on": digitized_on,
            "checked_by": chk, "signed_off_by": signer, "signed_off_on": str(
                signed_off_on or digitized_on or ""),
            "notes": (notes + notes_extra).strip(), "charts": charts}


def default_chart_file():
    """The chart file shipped with the Breach Runner and the Screening (core/acer_tm11_charts.json):
    this table's zone polygons, signed off by SIGNED_OFF_BY on SIGNED_OFF_ON. Identical in both
    plugins; the Hazard Helper's chart sign-off tool writes the same file for another engineer."""
    return chart_file(digitized_by=SIGNED_OFF_BY, checked_by="", digitized_on=SIGNED_OFF_ON,
                      notes_extra=("Shipped with the suite: the engineer who digitized the table "
                                   "signed it off for the suite's default (release 1.18.1)."),
                      signed_off_by=SIGNED_OFF_BY, signed_off_on=SIGNED_OFF_ON)
