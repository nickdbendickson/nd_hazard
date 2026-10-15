# -*- coding: utf-8 -*-
"""v2.10 chart sign-off: ND Dam Hazard's ACER TM 11 curves as the chart file the
other two tools read (ND Dam Breach Runner / ND Dam Breach Screening,
core/nd_hazard.py + acer_tm11_charts.json).

v2.11.0: ND Dam Hazard's zone boundaries for houses on foundations (ND Fig.
5.1 = TM 11 Fig. 2) and mobile homes (ND Fig. 5.2 = TM 11 Fig. 3) are the
table the engineer digitized from REG_05.2026 (damsuite.tm11_curves, read
through constants.HAZARD_COEFFICIENTS as TableCurve objects, linear between
the tabulated velocities); until v2.10.0 they were quartic fits. nd_hazard.py
wants zone POLYGONS in
(velocity ft/s, depth ft) and refuses the chart method until an engineer has
signed them off (one engineer is enough since 2.28.1; a second checker is
optional). This module:

* charts_from_curves() - samples the curves (default every 0.1 ft/s, plus every
  tabulated velocity of the table, so no corner of the table is cut) and
  builds the nd_hazard polygons: high danger zone = everything above the high
  curve up to the top of the chart; judgment zone = between the low and the
  high curve (clipped to depth 0 where the low curve drops below 0).
  Axes as nd_hazard's shipped chart file states them: Fig. 2 velocity 0-25
  ft/s, depth 0-10 ft; Fig. 3 velocity 0-16 ft/s, depth 0-3 ft. The velocity
  ranges are ND Dam Hazard's CURVE_V_MAX (check_axes() says when they are not).
* equivalence() - nd_hazard's DangerChart.zone() on the exported polygons
  against ND Dam Hazard's own zone rule (the table, velocity clipped to the
  figure's range, d >= curve) on a dense (d, v) grid plus points just either
  side of each curve. Disagreements are expected only inside nd_hazard's
  digitizing tolerance band (nd_hazard puts a point that close to a higher
  zone INTO that zone - ND 5.3); the result reports how many and how far.
* write_chart_file() - the JSON nd_hazard.load_charts() reads. Status
  "digitized" when an engineer is named (signed_off_by = the digitizer, else
  the checker; REQUIRE_DIFFERENT_CHECKER, off by default, demands a second
  person as checker); otherwise "not digitized" with the polygons kept for
  review. install_in_plugins() copies the file over core/acer_tm11_charts.json
  of the installed Runner and Screening. The notes say what the
  curves are; the file never says a check happened that the user did not
  record - the tool only records the names it is given.
* overlay_svg() - the engineer's scan of the printed figure (PNG / JPEG,
  embedded base64) with the exported curves, a grid and a legend drawn on top
  through a 2-, 3- or more-point pixel <-> (velocity, depth) calibration, so a
  reviewer can see whether the curves lie on the printed lines. Without an
  image: the curves alone on a plain chart.

Pure Python (standard library); nd_hazard.py (numpy) is only loaded for
equivalence(). Nothing here has been run inside a real QGIS.
"""

import base64
import datetime
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import struct
import sys

from .constants import (HAZARD_COEFFICIENTS, CURVE_V_MAX, PLUGIN_NAME, PLUGIN_VERSION,
                        CURVE_TABLE_SHA256)

FORMAT_NOTE = "nd_hazard chart file (acer_tm11_charts.json), MODULE 1.1.0 format"
CHART_FILE_NAME = "acer_tm11_charts.json"
DEFAULT_STEP_FTS = 0.1          # curve sampling step (ft/s)
DEFAULT_TOLERANCE = 0.01        # nd_hazard digitizing tolerance (share of each axis)
# One engineer's sign-off is enough (2.28.1, as the dam safety engineer decided); a second
# checker may be recorded. True demands a different person as checker - the record says which.
REQUIRE_DIFFERENT_CHECKER = False
# (velocity steps, depth steps) of the equivalence grid of a sign-off, plus the
# points either side of each curve (about 12,000 points a chart: ~7 s each)
EQUIVALENCE_GRID = (101, 81)

HOUSE, MOBILE = "house", "mobile"          # nd_hazard CHART_HOUSE / CHART_MOBILE
ZONE_LOW, ZONE_JUDGMENT, ZONE_HIGH = "low danger", "judgment", "high danger"
_RANK = {ZONE_LOW: 0, ZONE_JUDGMENT: 1, ZONE_HIGH: 2}

# chart key -> ND curve family, titles and the axes of the printed figure.
# Axis maxima: as stated in nd_hazard's shipped acer_tm11_charts.json notes
# ("Fig. 2 velocity 0-25 ft/s, depth 0-10 ft; Fig. 3 velocity 0-16 ft/s,
# depth 0-3 ft"); the velocity maxima must equal CURVE_V_MAX (check_axes).
CHART_SPECS = {
    HOUSE: {"kind": "found", "title": "Houses built on foundations",
            "figure": "ACER TM 11 Fig. 2 (ND Fig. 5.1)", "v_max": 25.0, "d_max": 10.0,
            "grid_v": 1.0, "grid_d": 0.5, "label_v": 5.0, "label_d": 1.0},
    MOBILE: {"kind": "mobile", "title": "Mobile homes",
             "figure": "ACER TM 11 Fig. 3 (ND Fig. 5.2)", "v_max": 16.0, "d_max": 3.0,
             "grid_v": 1.0, "grid_d": 0.25, "label_v": 2.0, "label_d": 0.5},
}

# What the curves are, quoted from the comments next to HAZARD_COEFFICIENTS in
# constants.py (the tests check that these numbers are in that file).
CURVE_ORIGIN = (
    "The zone polygons are ND Dam Hazard's flood danger curves (constants.HAZARD_COEFFICIENTS, "
    "v2.11.0): the table digitized by the engineer from ND REG_05.2026 Figures 5.1 and 5.2 "
    "(damsuite.tm11_curves; stated accuracy +/-0.06 ft), linear between the tabulated "
    "velocities, sampled every {step:g} ft/s and at every tabulated velocity, closed along the "
    "chart axes. Check of the table against screenshots of the two figures (pixel trace, not a "
    "person's check): houses median 0.03 ft, 95 % within 0.05 ft (high line) and 0.09 ft (low "
    "line); mobile homes median 0.008 / 0.004 ft, 95 % within 0.02 ft. Axes: Fig. 5.1 velocity "
    "0-25 ft/s, depth 0-10 ft; Fig. 5.2 velocity 0-16 ft/s, depth 0-3 ft (velocity ranges = ND "
    "Dam Hazard CURVE_V_MAX). Where the house low curve is 0 ft (19 ft/s and above) the judgment "
    "zone reaches depth 0.")


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


# ------------------------------------------------------------------ curves --
def horner(coeffs, x):
    """Same evaluation as helpers.evaluate_polynomial ([x^4 .. const])."""
    r = 0.0
    for c in coeffs:
        r = r * x + c
    return r


def curve_depth(key, v):
    """Depth (ft) of ND curve `key` (e.g. "found_high") at velocity v, with v
    clipped to the figure's range exactly as ND Dam Hazard does (a table curve
    linearly, a quartic by Horner)."""
    kind = key.split("_")[0]
    vmax = CURVE_V_MAX.get(kind)
    vv = max(0.0, float(v))
    if vmax is not None:
        vv = min(vv, vmax)
    c = HAZARD_COEFFICIENTS[key]
    if hasattr(c, "at"):
        return c.at(vv)
    return horner(c, vv)


def nd_zone(chart, depth_ft, vel_fts):
    """ND Dam Hazard's zone for one (depth, velocity) point on chart "house" or
    "mobile" - the rule of helpers.classify_scalar (d >= high curve -> high,
    d >= low curve and d > 0 -> judgment, else low; velocity clipped to the
    figure)."""
    kind = CHART_SPECS[chart]["kind"]
    d = float(depth_ft)
    if d >= curve_depth(kind + "_high", vel_fts):
        return ZONE_HIGH
    if d >= curve_depth(kind + "_low", vel_fts) and d > 0:
        return ZONE_JUDGMENT
    return ZONE_LOW


def check_axes():
    """Problems (list of text) between the chart axes used here and ND Dam
    Hazard's curve ranges; [] when they agree."""
    out = []
    for key, sp in CHART_SPECS.items():
        vm = CURVE_V_MAX.get(sp["kind"])
        if vm is None or abs(float(vm) - sp["v_max"]) > 1e-9:
            out.append("chart '{}': velocity axis 0-{:g} ft/s but ND Dam Hazard's CURVE_V_MAX "
                       "for '{}' is {}".format(key, sp["v_max"], sp["kind"], vm))
        for lvl in ("low", "high"):
            if not HAZARD_COEFFICIENTS.get(sp["kind"] + "_" + lvl):
                out.append("chart '{}': curve {}_{} missing".format(key, sp["kind"], lvl))
    return out


def _velocities(vmax, step):
    n = max(1, int(round(vmax / float(step))))
    return [vmax * i / n for i in range(n + 1)]


def _dedupe(pts):
    out = []
    for p in pts:
        if not out or abs(out[-1][0] - p[0]) > 1e-12 or abs(out[-1][1] - p[1]) > 1e-12:
            out.append(p)
    if len(out) > 1 and out[0] == out[-1]:
        out.pop()
    return out


def charts_from_curves(step=DEFAULT_STEP_FTS, tolerance=DEFAULT_TOLERANCE, digits=6):
    """{"house": chart, "mobile": chart} in nd_hazard's JSON chart format
    ({title, figure, v_max_fts, d_max_ft, tolerance, high, judgment}) from ND
    Dam Hazard's curves. step: curve sampling interval (ft/s)."""
    if not (step > 0):
        raise ValueError("the sampling step must be positive")
    probs = check_axes()
    if probs:
        raise ValueError("; ".join(probs))
    out = {}
    for key, sp in CHART_SPECS.items():
        vmax, dmax = sp["v_max"], sp["d_max"]
        vs = _velocities(vmax, step)
        for k in ("_high", "_low"):                       # the table's own corners
            c = HAZARD_COEFFICIENTS[sp["kind"] + k]
            vs = sorted(set(vs) | {float(v) for v in getattr(c, "v", ()) if 0 <= v <= vmax})
        hi = [min(max(curve_depth(sp["kind"] + "_high", v), 0.0), dmax) for v in vs]
        lo = [min(max(curve_depth(sp["kind"] + "_low", v), 0.0), dmax) for v in vs]
        lo = [min(a, b) for a, b in zip(lo, hi)]         # never above the high curve
        r = lambda x: round(float(x), digits)            # noqa: E731
        upper = [(r(v), r(h)) for v, h in zip(vs, hi)]
        high = _dedupe(upper + [(r(vmax), r(dmax)), (0.0, r(dmax))])
        judgment = _dedupe(upper + [(r(v), r(d)) for v, d in reversed(list(zip(vs, lo)))])
        out[key] = {"title": "{} ({})".format(sp["title"], sp["figure"]),
                    "figure": sp["figure"], "v_max_fts": vmax, "d_max_ft": dmax,
                    "tolerance": float(tolerance),
                    "high": [list(p) for p in high], "judgment": [list(p) for p in judgment]}
    return out


# --------------------------------------------------- nd_hazard.py locating --
ND_HAZARD_PACKAGES = ("nd_breach_runner", "nd_dam_screening")


def _candidate_files(path=None):
    """Where nd_hazard.py may be: an explicit file / folder, $ND_HAZARD_PY, the
    installed runner / screening packages (import by name, not executed), and
    the plugin folders next to this one (QGIS keeps all plugins in one folder)."""
    cands = []

    def add(p):
        if not p:
            return
        p = os.path.abspath(p)
        if os.path.isdir(p):
            for sub in ("nd_hazard.py", os.path.join("core", "nd_hazard.py")):
                q = os.path.join(p, sub)
                if os.path.isfile(q):
                    cands.append(q)
                    return
        elif os.path.isfile(p):
            cands.append(p)
    add(path)
    add(os.environ.get("ND_HAZARD_PY"))
    for name in ND_HAZARD_PACKAGES:
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            spec = None
        if spec is not None and spec.submodule_search_locations:
            for loc in spec.submodule_search_locations:
                add(loc)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ND_HAZARD_PACKAGES:
        add(os.path.join(here, name))
    seen, uniq = set(), []
    for c in cands:
        k = os.path.normcase(os.path.realpath(c))
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return uniq


_LOADED = {}


def _sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def find_nd_hazard(path=None):
    """(module, info) of the other tools' nd_hazard.py, or (None, info) when none
    is found. The file is loaded on its own (it has no package-relative imports)
    under a private module name, so the runner / screening plugin itself is not
    imported. info: {"file", "module_version", "sha256", "candidates",
    "identical_copies" (True / False / None when only one copy), "problem"}."""
    cands = _candidate_files(path)
    info = {"file": "", "module_version": "", "sha256": "", "candidates": cands,
            "identical_copies": None, "problem": ""}
    if not cands:
        info["problem"] = ("nd_hazard.py not found: install ND Dam Breach Runner or ND Dam "
                           "Breach Screening, or give the path of their core/nd_hazard.py")
        return None, info
    shas = []
    for c in cands:
        try:
            shas.append(_sha256_file(c))
        except OSError:
            shas.append(None)
    if len(cands) > 1:
        info["identical_copies"] = len({s for s in shas if s}) == 1
    target = cands[0]
    info["file"], info["sha256"] = target, shas[0] or ""
    key = (os.path.normcase(os.path.realpath(target)), shas[0])
    mod = _LOADED.get(key)
    if mod is None:
        name = "_ndhazard_ext_{}".format((shas[0] or "x")[:12])
        try:
            spec = importlib.util.spec_from_file_location(name, target)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod                  # dataclasses look the module up
            spec.loader.exec_module(mod)
        except Exception as e:
            sys.modules.pop(name, None)
            info["problem"] = "{} could not be loaded: {}".format(target, e)
            return None, info
        _LOADED[key] = mod
    info["module_version"] = getattr(mod, "MODULE_VERSION", "")
    return mod, info


# ------------------------------------------------------------- equivalence --
def _dist_to_poly(v, d, poly, sx, sy):
    best = float("inf")
    n = len(poly)
    px, py = v / sx, d / sy
    for i in range(n):
        ax, ay = poly[i][0] / sx, poly[i][1] / sy
        bx, by = poly[(i + 1) % n][0] / sx, poly[(i + 1) % n][1] / sy
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
        best = min(best, math.hypot(px - ax - t * dx, py - ay - t * dy))
    return best


def equivalence_points(chart, nv=101, nd=81, near=(0.0005, 0.005, 0.02, 0.05, 0.15),
                       near_step=None):
    """(depth, velocity) test points of one chart: an nv x nd grid over the axes
    plus points just above / below each curve every `near_step` ft/s (default:
    the grid spacing / 2) at the depth offsets `near` (ft, scaled for the mobile
    chart by its depth range / 10)."""
    sp = CHART_SPECS[chart]
    vmax, dmax = sp["v_max"], sp["d_max"]
    pts = [(dmax * j / (nd - 1), vmax * i / (nv - 1)) for i in range(nv) for j in range(nd)]
    step = near_step or vmax / (nv - 1) / 2.0
    scale = dmax / 10.0
    for v in _velocities(vmax, step):
        for lvl in ("low", "high"):
            c = curve_depth(sp["kind"] + "_" + lvl, v)
            for off in near:
                for s in (1, -1):
                    d = c + s * off * scale
                    if 0.0 <= d <= dmax:
                        pts.append((d, v))
    return pts


def equivalence(nh=None, charts=None, tolerance=None, nv=101, nd=81, points=None,
                nh_path=None):
    """nd_hazard's DangerChart.zone() on the exported polygons against ND Dam
    Hazard's own zone for the same points. Returns {"ok", "nd_hazard", "charts":
    {key: {...}}, "lines"}; per chart: points, disagreements, nd_hazard_higher,
    nd_hazard_lower, max_gap_ft (the largest depth distance of a disagreeing
    point from the ND curve between the two zones), max_gap_axis (the same
    in nd_hazard's tolerance measure: distance in shares of the axes), the
    tolerance and what it is in ft and ft/s; the same once more with tolerance 0
    ("exact"), which shows the polygon (chord) error alone. ok: no point where
    nd_hazard gives a LOWER zone than ND Dam Hazard, and every disagreement
    within the tolerance band."""
    info = {}
    if nh is None:
        nh, info = find_nd_hazard(nh_path)
        if nh is None:
            return {"ok": None, "nd_hazard": info, "charts": {},
                    "lines": ["Equivalence not checked: " + info.get("problem", "")]}
    charts = charts or charts_from_curves()
    res = {"ok": True, "nd_hazard": info, "charts": {}, "lines": []}
    for key, c in charts.items():
        if key not in CHART_SPECS:
            continue
        sp = CHART_SPECS[key]
        tol = float(c.get("tolerance", DEFAULT_TOLERANCE) if tolerance is None else tolerance)
        pts = points.get(key) if isinstance(points, dict) else None
        pts = pts or equivalence_points(key, nv, nd)
        row = {"points": len(pts), "tolerance": tol,
               "tolerance_ft": tol * sp["d_max"], "tolerance_fts": tol * sp["v_max"]}
        for label, t in (("", tol), ("exact_", 0.0)):
            dc = nh.DangerChart(key=key, title=c["title"], figure=c["figure"],
                                v_max_fts=float(c["v_max_fts"]), d_max_ft=float(c["d_max_ft"]),
                                high=[tuple(p) for p in c["high"]],
                                judgment=[tuple(p) for p in c["judgment"]],
                                tolerance=t).validate()
            n_dis = hi = lo = 0
            gap_ft = gap_ax = 0.0
            worst = None
            for d, v in pts:
                a = nd_zone(key, d, v)
                b, _note = dc.zone(d, v)
                if a == b:
                    continue
                n_dis += 1
                if _RANK[b] > _RANK[a]:
                    hi += 1
                else:
                    lo += 1
                # the ND curve between the two zones (the higher one's lower edge)
                upper = b if _RANK[b] > _RANK[a] else a
                ck = sp["kind"] + ("_high" if upper == ZONE_HIGH else "_low")
                g = abs(d - max(curve_depth(ck, v), 0.0))
                poly = dc.high if upper == ZONE_HIGH else dc.judgment
                ga = _dist_to_poly(v, d, poly, sp["v_max"], sp["d_max"])
                if g > gap_ft:
                    gap_ft, worst = g, (round(d, 4), round(v, 4), a, b)
                gap_ax = max(gap_ax, ga)
            row.update({label + "disagreements": n_dis, label + "nd_hazard_higher": hi,
                        label + "nd_hazard_lower": lo, label + "max_gap_ft": gap_ft,
                        label + "max_gap_axis": gap_ax, label + "worst": worst})
        row["chord_max_ft"] = chord_error(key, c)
        if row["nd_hazard_lower"] or row["max_gap_axis"] > tol + 1e-9:
            res["ok"] = False
        res["charts"][key] = row
        res["lines"].append(
            "{}: {} points; with nd_hazard's tolerance {:g} of the axes ({:.3g} ft / {:.3g} "
            "ft/s) {} disagree ({} nd_hazard higher, {} lower), largest {:.4f} ft from the ND "
            "curve ({:.4f} of the axes); tolerance 0: {} disagree{}; polygon edges within "
            "{:.1e} ft of the curves".format(
                key, row["points"], tol, row["tolerance_ft"], row["tolerance_fts"],
                row["disagreements"], row["nd_hazard_higher"], row["nd_hazard_lower"],
                row["max_gap_ft"], row["max_gap_axis"], row["exact_disagreements"],
                ", largest {:.2e} ft".format(row["exact_max_gap_ft"])
                if row["exact_disagreements"] else "", row["chord_max_ft"]))
    return res


def chord_error(chart, c=None, sub=20):
    """Largest vertical distance (ft) between the exported polygon edges and ND
    Dam Hazard's curves (both curves, clipped to the axes), checked `sub` times
    between consecutive vertices."""
    sp = CHART_SPECS[chart]
    c = c or charts_from_curves()[chart]
    worst = 0.0
    # the upper edge of the judgment polygon is the high curve, its lower edge
    # (reversed) the low curve; the high polygon's curve part is the same line
    ring = [tuple(p) for p in c["judgment"]]
    k = len(ring) // 2
    for pts, lvl in ((ring[:k], "high"), (list(reversed(ring[k:])), "low")):
        key = sp["kind"] + "_" + lvl
        for (v1, d1), (v2, d2) in zip(pts[:-1], pts[1:]):
            for i in range(sub + 1):
                t = i / float(sub)
                v = v1 + t * (v2 - v1)
                cd = min(max(curve_depth(key, v), 0.0), sp["d_max"])
                if lvl == "low":
                    cd = min(cd, min(max(curve_depth(sp["kind"] + "_high", v), 0.0),
                                     sp["d_max"]))
                worst = max(worst, abs(d1 + t * (d2 - d1) - cd))
    return worst


# ---------------------------------------------------------------- the file --
def _norm_name(s):
    return re.sub(r"\s+", " ", str(s or "").strip()).casefold()


def signoff_status(digitized_by, checked_by, require_different=None):
    """(status, reason): "digitized" when an engineer is named - the digitizer, else the
    checker (one engineer is enough). With require_different (REQUIRE_DIFFERENT_CHECKER) a
    second, different person must be named as checker (case and spacing ignored; the tool
    cannot tell initials from a full name, so use the same form for both)."""
    req = REQUIRE_DIFFERENT_CHECKER if require_different is None else bool(require_different)
    a, b = _norm_name(digitized_by), _norm_name(checked_by)
    if not a and not b:
        return "not digitized", "no engineer named: nobody has signed the charts off"
    if req and not a:
        return "not digitized", "'signed off by' is empty (a second checker alone is not a sign-off)"
    if req and not b:
        return "not digitized", ("'checked by' is empty: a second engineer must check (rule "
                                 "REQUIRE_DIFFERENT_CHECKER)")
    if req and a == b:
        return "not digitized", ("the same person is named as digitizer and checker: a person may "
                                 "not check their own work (rule REQUIRE_DIFFERENT_CHECKER)")
    if a and b and a != b:
        return "digitized", "signed off by {}, checked by {} (as entered)".format(
            str(digitized_by).strip(), str(checked_by).strip())
    return "digitized", "signed off by {} (one engineer; no second checker recorded)".format(
        str(digitized_by or checked_by).strip())


def write_chart_file(path, digitized_from="", digitized_by="", checked_by="", digitized_on="",
                     notes="", require_different_checker=None, charts=None,
                     step=DEFAULT_STEP_FTS, tolerance=DEFAULT_TOLERANCE, equivalence_result=None):
    """Write the nd_hazard chart file. `path`: a .json file or a folder (then
    acer_tm11_charts.json in it). Returns {"path", "status", "reason", "sha256",
    "ready_expected"}. The polygons are always written (for review); only the
    status decides whether nd_hazard uses them."""
    if os.path.isdir(path) or not path.lower().endswith(".json"):
        os.makedirs(path, exist_ok=True)
        path = os.path.join(path, CHART_FILE_NAME)
    else:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    charts = charts or charts_from_curves(step, tolerance)
    status, reason = signoff_status(digitized_by, checked_by, require_different_checker)
    parts = [CURVE_ORIGIN.format(step=step)]
    parts.append("Written by {} {} chartsign on {}. Sign-off: {}. The tool records the names "
                 "entered; it cannot confirm that a check was made.".format(
                     PLUGIN_NAME, PLUGIN_VERSION, _now(), reason))
    if equivalence_result and equivalence_result.get("lines"):
        parts.append("Equivalence with ND Dam Hazard's own zones: " +
                     " | ".join(equivalence_result["lines"]))
    if notes and str(notes).strip():
        parts.append("Engineer's notes: " + str(notes).strip())
    d = {"status": status,
         "source": ("Reclamation (1988), ACER Technical Memorandum No. 11, Downstream Hazard "
                    "Classification Guidelines: Fig. 2 houses built on foundations (ND Fig. "
                    "5.1), Fig. 3 mobile homes (ND Fig. 5.2)"),
         "digitized_from": str(digitized_from or "").strip() or
         "ND Dam Hazard's REG_05.2026 Fig. 5.1 / 5.2 table (damsuite.tm11_curves, "
         "table SHA-256 {})".format(CURVE_TABLE_SHA256),
         "digitized_by": str(digitized_by or "").strip(),
         "digitized_on": str(digitized_on or "").strip() or datetime.date.today().isoformat(),
         "checked_by": str(checked_by or "").strip(),
         "signed_off_by": str(digitized_by or checked_by or "").strip() if status == "digitized"
         else "",
         "signed_off_on": (str(digitized_on or "").strip() or datetime.date.today().isoformat())
         if status == "digitized" else "",
         "notes": " ".join(parts),
         "charts": charts,
         # extra keys: ignored by nd_hazard.load_charts, kept for the record
         "generator": {"tool": PLUGIN_NAME, "version": PLUGIN_VERSION, "module": "chartsign",
                       "step_fts": step, "sign_off_rule_different_checker":
                           REQUIRE_DIFFERENT_CHECKER if require_different_checker is None
                           else bool(require_different_checker)}}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=1)
    os.replace(tmp, path)
    return {"path": path, "status": status, "reason": reason, "sha256": _sha256_file(path),
            "ready_expected": status == "digitized"}


# ------------------------------------------------------------ image header --
def image_info(path):
    """(width, height, mime) of a PNG or JPEG file read from its header."""
    with open(path, "rb") as fh:
        head = fh.read(32)
        if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
            w, h = struct.unpack(">II", head[16:24])
            return int(w), int(h), "image/png"
        if head[:2] != b"\xff\xd8":
            raise ValueError("{}: not a PNG or JPEG image".format(path))
        fh.seek(2)
        while True:
            b = fh.read(1)
            while b and b != b"\xff":
                b = fh.read(1)
            while b == b"\xff":
                b = fh.read(1)
            if not b:
                break
            m = b[0]
            if m in (0x01,) or 0xD0 <= m <= 0xD9:
                continue                                   # no length
            ln = fh.read(2)
            if len(ln) < 2:
                break
            seg = struct.unpack(">H", ln)[0]
            if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE,
                     0xCF):
                data = fh.read(5)
                if len(data) < 5:
                    break
                h, w = struct.unpack(">HH", data[1:5])
                return int(w), int(h), "image/jpeg"
            fh.seek(seg - 2, 1)
    raise ValueError("{}: JPEG without a frame header (size unknown)".format(path))


# ------------------------------------------------------------- calibration --
_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"


def parse_calibration(text):
    """Reference points from text: one point per ';' or line, each
    "x_px, y_px = velocity_fts, depth_ft" (any separators; 4 numbers, pixel
    first). Returns [(x_px, y_px, v, d), ...]."""
    pts = []
    for part in re.split(r"[;\n]+", str(text or "")):
        nums = re.findall(_NUM, part)
        if not nums:
            continue
        if len(nums) != 4:
            raise ValueError("calibration point '{}': 4 numbers needed (x_px, y_px = velocity, "
                             "depth)".format(part.strip()))
        pts.append(tuple(float(x) for x in nums))
    return pts


def _solve3(A, b):
    """Gauss-Jordan for a 3x3 system; None when singular."""
    M = [list(A[i]) + [b[i]] for i in range(3)]
    for c in range(3):
        p = max(range(c, 3), key=lambda r: abs(M[r][c]))
        if abs(M[p][c]) < 1e-12:
            return None
        M[c], M[p] = M[p], M[c]
        for r in range(3):
            if r != c:
                f = M[r][c] / M[c][c]
                M[r] = [M[r][k] - f * M[c][k] for k in range(4)]
    return [M[i][3] / M[i][i] for i in range(3)]


def fit_calibration(points):
    """Affine (velocity, depth) -> pixel fit. 2 points: axes parallel to the
    image (scale + offset per axis); 3 points: full affine, exact; 4 or more:
    least squares with the residual. Returns {"n", "method", "fwd": (a, b, c, e,
    f, g) with x = a v + b d + c, y = e v + f d + g, "rms_px", "max_px",
    "residuals" [px per point], "rms_v_fts", "rms_d_ft", "axis_angle_deg",
    "rotation_deg", "text"}."""
    pts = [tuple(map(float, p)) for p in points]
    n = len(pts)
    if n < 2:
        raise ValueError("at least two calibration points are needed")
    if n == 2:
        (x1, y1, v1, d1), (x2, y2, v2, d2) = pts
        if abs(v2 - v1) < 1e-12 or abs(d2 - d1) < 1e-12:
            raise ValueError("two calibration points must differ in both velocity and depth "
                             "(e.g. the chart origin and the top-right corner)")
        a = (x2 - x1) / (v2 - v1)
        f = (y2 - y1) / (d2 - d1)
        fwd = (a, 0.0, x1 - a * v1, 0.0, f, y1 - f * d1)
        method = "2 points: axes taken parallel to the image edges (no rotation / skew)"
    else:
        ata = [[0.0] * 3 for _ in range(3)]
        atx, aty = [0.0] * 3, [0.0] * 3
        for x, y, v, d in pts:
            row = (v, d, 1.0)
            for i in range(3):
                for j in range(3):
                    ata[i][j] += row[i] * row[j]
                atx[i] += row[i] * x
                aty[i] += row[i] * y
        sx, sy = _solve3(ata, atx), _solve3(ata, aty)
        if sx is None or sy is None:
            raise ValueError("calibration points are on one line: no affine fit")
        fwd = (sx[0], sx[1], sx[2], sy[0], sy[1], sy[2])
        method = ("3 points: exact affine fit (no redundancy - add a 4th point for a residual "
                  "check)" if n == 3 else "{} points: least-squares affine fit".format(n))
    a, b, c, e, f, g = fwd
    res = []
    for x, y, v, d in pts:
        res.append(math.hypot(a * v + b * d + c - x, e * v + f * d + g - y))
    rms = math.sqrt(sum(r * r for r in res) / n)
    # the residual in chart units: pixels per ft/s and per ft along each axis
    ppv, ppd = math.hypot(a, e), math.hypot(b, f)
    ang_v = math.degrees(math.atan2(e, a))
    ang_d = math.degrees(math.atan2(f, b))
    between = abs(((ang_d - ang_v) + 180.0) % 360.0 - 180.0)
    out = {"n": n, "method": method, "fwd": fwd, "residuals": res, "rms_px": rms,
           "max_px": max(res), "rms_v_fts": rms / ppv if ppv else None,
           "rms_d_ft": rms / ppd if ppd else None, "axis_angle_deg": between,
           "rotation_deg": ang_v, "px_per_fts": ppv, "px_per_ft": ppd}
    if n == 2:
        chk = "no residual: 2 points define the fit exactly"
    elif n == 3:
        chk = "no residual: 3 points define the affine fit exactly"
    else:
        chk = "residual RMS {:.2f} px (max {:.2f} px) = about {:.3f} ft/s or {:.3f} ft".format(
            rms, max(res), out["rms_v_fts"] or 0.0, out["rms_d_ft"] or 0.0)
    out["text"] = "{}; {}; axes {:.2f} deg apart (90 = square), velocity axis rotated {:.2f} " \
                  "deg; {:.2f} px per ft/s, {:.2f} px per ft".format(
                      method, chk, between, ang_v, ppv, ppd)
    return out


def _to_px(fit, v, d):
    a, b, c, e, f, g = fit["fwd"]
    return a * v + b * d + c, e * v + f * d + g


# --------------------------------------------------------------------- svg --
def _esc(t):
    return (str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _plain_fit(sp, W=860, H=540, ml=70, mr=24, mt=44, mb=52):
    """A calibration that puts the chart axes into a plain W x H frame."""
    x0, x1, y0, y1 = ml, W - mr, H - mb, mt
    return {"fwd": ((x1 - x0) / sp["v_max"], 0.0, x0, 0.0, (y1 - y0) / sp["d_max"], y0),
            "n": 0, "text": "no image: plain chart frame", "rms_px": 0.0, "max_px": 0.0}


def _frange(a, b, s):
    n = int(math.floor((b - a) / s + 1e-9))
    return [a + i * s for i in range(n + 1)]


def overlay_svg(image_path, calibration, out_svg, chart=HOUSE, charts=None,
                step=DEFAULT_STEP_FTS, title=None):
    """SVG of chart "house" or "mobile": the scanned figure `image_path` (PNG /
    JPEG, embedded) with the exported curves drawn through `calibration` (text
    for parse_calibration, a point list, or a fit_calibration result), plus a
    grid, the calibration points and a legend. image_path None: the curves on a
    plain chart (calibration ignored). Returns {"path", "fit", "image", "width",
    "height"}."""
    if chart not in CHART_SPECS:
        raise ValueError("chart must be 'house' or 'mobile'")
    sp = CHART_SPECS[chart]
    charts = charts or charts_from_curves(step)
    c = charts[chart]
    img = None
    if image_path:
        W, H, mime = image_info(image_path)
        with open(image_path, "rb") as fh:
            img = "data:{};base64,{}".format(mime, base64.b64encode(fh.read()).decode("ascii"))
        if isinstance(calibration, dict) and "fwd" in calibration:
            fit = calibration
        else:
            pts = parse_calibration(calibration) if isinstance(calibration, str) else \
                list(calibration or [])
            fit = fit_calibration(pts)
            fit["points"] = pts
    else:
        W, H = 860, 540
        fit = _plain_fit(sp, W, H)
        fit["points"] = []
    lw = max(1.0, W / 700.0)
    fs = max(11.0, W / 70.0)
    P = lambda v, d: "{:.2f},{:.2f}".format(*_to_px(fit, v, d))       # noqa: E731
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
           'width="{0}" height="{1}" viewBox="0 0 {0} {1}" font-family="Helvetica,Arial,'
           'sans-serif" font-size="{2:.1f}">'.format(W, H, fs),
           '<title>{}</title>'.format(_esc(title or "{} - curve overlay".format(c["title"])))]
    out.append('<rect width="100%" height="100%" fill="#ffffff"/>')
    if img:
        out.append('<image x="0" y="0" width="{0}" height="{1}" preserveAspectRatio="none" '
                   'href="{2}" xlink:href="{2}"/>'.format(W, H, img))
    else:
        out.append('<polygon points="{}" fill="#fbe7b5" fill-opacity="0.8" stroke="none"/>'
                   .format(" ".join(P(v, d) for v, d in c["judgment"])))
        out.append('<polygon points="{}" fill="#f4b6b0" fill-opacity="0.8" stroke="none"/>'
                   .format(" ".join(P(v, d) for v, d in c["high"])))
    # grid + frame
    g = ['<g stroke="#2a6fdb" stroke-opacity="0.45" stroke-width="{:.2f}" '
         'stroke-dasharray="{:.1f},{:.1f}" fill="none">'.format(lw * 0.6, lw * 3, lw * 3)]
    for v in _frange(0.0, sp["v_max"], sp["grid_v"]):
        g.append('<polyline points="{} {}"/>'.format(P(v, 0.0), P(v, sp["d_max"])))
    for d in _frange(0.0, sp["d_max"], sp["grid_d"]):
        g.append('<polyline points="{} {}"/>'.format(P(0.0, d), P(sp["v_max"], d)))
    g.append('</g>')
    out += g
    out.append('<polygon points="{} {} {} {}" fill="none" stroke="#2a6fdb" stroke-width="{:.2f}"/>'
               .format(P(0, 0), P(sp["v_max"], 0), P(sp["v_max"], sp["d_max"]),
                       P(0, sp["d_max"]), lw))
    for v in _frange(0.0, sp["v_max"], sp["label_v"]):
        x, y = _to_px(fit, v, 0.0)
        out.append('<text x="{:.1f}" y="{:.1f}" fill="#2a6fdb" text-anchor="middle">{:g}</text>'
                   .format(x, y + fs * 1.2, v))
    for d in _frange(0.0, sp["d_max"], sp["label_d"]):
        x, y = _to_px(fit, 0.0, d)
        out.append('<text x="{:.1f}" y="{:.1f}" fill="#2a6fdb" text-anchor="end">{:g}</text>'
                   .format(x - fs * 0.4, y + fs * 0.35, d))
    if not img:
        x, y = _to_px(fit, sp["v_max"] / 2.0, 0.0)
        out.append('<text x="{:.1f}" y="{:.1f}" text-anchor="middle">Velocity (ft/s)</text>'
                   .format(x, y + fs * 2.5))
        x, y = _to_px(fit, 0.0, sp["d_max"] / 2.0)
        out.append('<text x="{0:.1f}" y="{1:.1f}" transform="rotate(-90 {0:.1f} {1:.1f})" '
                   'text-anchor="middle">Depth (ft)</text>'.format(x - fs * 2.6, y))
    # the curves (sampled as exported)
    vs = _velocities(sp["v_max"], step)
    for lvl, col, dash in (("low", "#e07b00", ""), ("high", "#c1121f", "")):
        key = sp["kind"] + "_" + lvl
        pts = [(v, min(max(curve_depth(key, v), 0.0), sp["d_max"])) for v in vs]
        out.append('<polyline points="{}" fill="none" stroke="{}" stroke-width="{:.2f}" '
                   'stroke-linejoin="round"{}/>'.format(" ".join(P(v, d) for v, d in pts), col,
                                                        lw * 1.6, dash))
    # calibration points
    for i, p in enumerate(fit.get("points") or []):
        x, y, v, d = p
        fx, fy = _to_px(fit, v, d)
        r = lw * 6
        out.append('<g stroke="#006d2c" stroke-width="{:.2f}" fill="none"><circle cx="{:.1f}" '
                   'cy="{:.1f}" r="{:.1f}"/><polyline points="{:.1f},{:.1f} {:.1f},{:.1f}"/>'
                   '<polyline points="{:.1f},{:.1f} {:.1f},{:.1f}"/></g>'.format(
                       lw, x, y, r, x - r * 1.6, y, x + r * 1.6, y, x, y - r * 1.6, x,
                       y + r * 1.6))
        left = x > W * 0.6                            # label inside the image
        out.append('<text x="{:.1f}" y="{:.1f}" fill="#006d2c" text-anchor="{}">P{} ({:g} ft/s, '
                   '{:g} ft) {:.1f} px</text>'.format(
                       x - r * 1.8 if left else x + r * 1.8,
                       y + r * 2.2 if y < H * 0.2 else y - r * 0.8,
                       "end" if left else "start", i + 1, v, d, math.hypot(fx - x, fy - y)))
    # legend: below the figure (never over it)
    lines = [("#c1121f", "high danger boundary ({}_high, REG_05.2026 table)".format(sp["kind"])),
             ("#e07b00", "low / judgment boundary ({}_low, REG_05.2026 table)".format(sp["kind"])),
             ("#2a6fdb", "chart frame and grid ({:g} ft/s, {:g} ft){}".format(
                 sp["grid_v"], sp["grid_d"], " from the calibration" if img else ""))]
    if img:
        lines.append(("#006d2c", "calibration point (label: fit residual)"))
    head = title or "{} - ND Dam Hazard curves{}".format(
        c["figure"], " on the scanned figure" if img else "")
    boxw = W - 20
    small = fs * 0.8
    per = max(30, int((boxw - 16) / (small * 0.55)))
    cal = _wrap("Calibration: " + fit.get("text", ""), per)
    lh = fs * 1.35
    boxh = lh * (1 + len(lines)) + small * 1.3 * len(cal) + lh * 0.8
    bx, by = 10, H + 8
    out.append('<g><rect x="{:.1f}" y="{:.1f}" width="{:.1f}" height="{:.1f}" fill="#ffffff" '
               'stroke="#444" stroke-width="{:.2f}"/>'.format(bx, by, boxw, boxh, lw * 0.6))
    ty = by + lh
    out.append('<text x="{:.1f}" y="{:.1f}" font-weight="bold">{}</text>'.format(
        bx + 8, ty, _esc(head)))
    for ln in cal:
        ty += small * 1.3
        out.append('<text x="{:.1f}" y="{:.1f}" font-size="{:.1f}">{}</text>'.format(
            bx + 8, ty, small, _esc(ln)))
    ty += lh * 0.2
    for col, lab in lines:
        ty += lh
        out.append('<polyline points="{:.1f},{:.1f} {:.1f},{:.1f}" stroke="{}" stroke-width='
                   '"{:.2f}"/><text x="{:.1f}" y="{:.1f}">{}</text>'.format(
                       bx + 8, ty - fs * 0.35, bx + 8 + fs * 2.2, ty - fs * 0.35, col, lw * 2,
                       bx + 14 + fs * 2.2, ty, _esc(lab)))
    Ht = int(math.ceil(H + boxh + 16))
    out[1] = out[1].replace('height="{}" viewBox="0 0 {} {}"'.format(H, W, H),
                            'height="{}" viewBox="0 0 {} {}"'.format(Ht, W, Ht))
    out.append('</g></svg>')
    os.makedirs(os.path.dirname(os.path.abspath(out_svg)), exist_ok=True)
    with open(out_svg, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    return {"path": out_svg, "fit": fit, "image": image_path or "", "width": W, "height": H,
            "canvas_height": Ht}


def _wrap(text, n):
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > n:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return lines


# ------------------------------------------------------------ one sign-off --
def plugin_chart_paths():
    """core/acer_tm11_charts.json of the installed ND Dam Breach Runner and Screening: the
    folder QGIS loaded each from, else python/plugins of the active profile, else the folder
    next to this plugin."""
    out = {}
    for name in ("nd_breach_runner", "nd_dam_screening"):
        folder = ""
        try:
            mod = sys.modules.get(name)
            if mod is not None and getattr(mod, "__file__", None):
                folder = os.path.dirname(os.path.abspath(mod.__file__))
            if not folder:
                import qgis.utils as qu
                if hasattr(qu, "pluginDirectory"):
                    folder = qu.pluginDirectory(name) or ""
        except Exception:
            folder = ""
        if not folder or not os.path.isdir(folder):
            try:
                from qgis.core import QgsApplication
                base = QgsApplication.qgisSettingsDirPath()
            except Exception:
                base = ""
            if base:
                folder = os.path.join(base, "python", "plugins", name)
        if not folder or not os.path.isdir(folder):
            folder = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), name)
        if os.path.isdir(os.path.join(folder, "core")):
            out[name] = os.path.join(folder, "core", CHART_FILE_NAME)
    return out


def install_in_plugins(chart_path, paths=None):
    """Copy the chart file over core/acer_tm11_charts.json of the installed Runner and
    Screening (the copy their chart method reads by default). Returns {"installed": [paths],
    "missing": [plugin names], "lines": [text]}; never raises."""
    res = {"installed": [], "missing": [], "lines": []}
    paths = paths if paths is not None else plugin_chart_paths()
    for name in ("nd_breach_runner", "nd_dam_screening"):
        dst = paths.get(name)
        if not dst:
            res["missing"].append(name)
            res["lines"].append("{}: not installed in this QGIS - copy the chart file over its "
                                "core/{} when it is".format(name, CHART_FILE_NAME))
            continue
        try:
            tmp = dst + ".tmp"
            shutil.copyfile(chart_path, tmp)
            os.replace(tmp, dst)
            res["installed"].append(dst)
            res["lines"].append("{}: {} replaced (SHA-256 {}); its next chart run uses it".format(
                name, dst, _sha256_file(dst)[:16]))
        except OSError as e:
            res["missing"].append(name)
            res["lines"].append("{}: could not write {}: {}".format(name, dst, e))
    return res


def sign_off(out_folder, digitized_by="", checked_by="", digitized_from="", digitized_on="",
             notes="", images=None, calibrations=None, require_different_checker=None,
             step=DEFAULT_STEP_FTS, tolerance=DEFAULT_TOLERANCE, nh_path=None,
             equivalence_grid=None, install=False):
    """Everything the Processing tool writes into `out_folder`: the chart file,
    one overlay SVG per chart (on the scan when an image and a calibration are
    given, else curves only) and Chart_SignOff.txt; with `install`, the signed-off
    file is also copied over the Runner's and the Screening's core copy. images /
    calibrations: {"house": path, "mobile": path} / {"house": text, ...}. Returns a
    dict of the paths, the status and the text lines of the record."""
    os.makedirs(out_folder, exist_ok=True)
    images, calibrations = images or {}, calibrations or {}
    charts = charts_from_curves(step, tolerance)
    grid = equivalence_grid or EQUIVALENCE_GRID
    eq = equivalence(charts=charts, nv=grid[0], nd=grid[1],
                     nh_path=nh_path)
    svgs, fits, warn = {}, {}, []
    for key in (HOUSE, MOBILE):
        img = images.get(key) or None
        cal = calibrations.get(key) or ""
        path = os.path.join(out_folder, "Chart_Overlay_{}.svg".format(
            "Fig2_house" if key == HOUSE else "Fig3_mobile"))
        if img and not str(cal).strip():
            warn.append("{}: image given without a calibration - curves drawn on a plain chart "
                        "instead".format(key))
            img = None
        r = overlay_svg(img, cal, path, key, charts, step)
        svgs[key] = r["path"]
        fits[key] = r["fit"] if img else None
    info = write_chart_file(out_folder, digitized_from, digitized_by, checked_by, digitized_on,
                            notes, require_different_checker, charts, step, tolerance, eq)
    lines = ["ACER TM 11 chart sign-off record", "=" * 32,
             "Written: {} by {} {} (chartsign)".format(_now(), PLUGIN_NAME, PLUGIN_VERSION),
             "Chart file: {} (SHA-256 {})".format(info["path"], info["sha256"]),
             "Status: {} - {}".format(info["status"], info["reason"]),
             "Digitized from: {}".format(digitized_from or "(not given)"),
             "Signed off by: {}".format(digitized_by or checked_by or "(not given)"),
             "Checked by (second engineer): {}".format(checked_by or "(none)"),
             "Date: {}".format(digitized_on or datetime.date.today().isoformat()),
             "Rule: {}".format(
                 "a second, different engineer must check (REQUIRE_DIFFERENT_CHECKER applied)"
                 if (REQUIRE_DIFFERENT_CHECKER if require_different_checker is None
                     else require_different_checker)
                 else "one engineer's sign-off is enough; a second checker is optional"),
             "The tool records the names entered; it cannot confirm that a check was made.",
             "", "What the curves are", "-" * 19, CURVE_ORIGIN.format(step=step), "",
             "Equivalence with ND Dam Hazard's own zone rule", "-" * 46]
    lines += eq.get("lines") or []
    if eq.get("nd_hazard", {}).get("file"):
        lines.append("nd_hazard.py used: {} (module {}, SHA-256 {}){}".format(
            eq["nd_hazard"]["file"], eq["nd_hazard"].get("module_version"),
            (eq["nd_hazard"].get("sha256") or "")[:16],
            "" if eq["nd_hazard"].get("identical_copies") in (None, True)
            else " - WARNING: the installed copies of nd_hazard.py differ"))
    if eq.get("ok") is False:
        lines.append("WARNING: disagreements outside the digitizing tolerance - do not sign off.")
    lines.append("Disagreements inside the tolerance band are nd_hazard's 5.3 borderline rule "
                 "(a point that close to a higher zone takes that zone); ND Dam Hazard's own "
                 "zones have no such band (its near-threshold option is separate).")
    lines += ["", "Overlays (compare the red / orange curves with the printed lines)", "-" * 64]
    for key in (HOUSE, MOBILE):
        f = fits[key]
        lines.append("{}: {}".format(os.path.basename(svgs[key]),
                                     "on {} - {}".format(os.path.basename(images[key]), f["text"])
                                     if f else "curves only (no scanned figure given)"))
    lines += warn
    installed = None
    if install and info["status"] == "digitized":
        installed = install_in_plugins(info["path"])
        lines += ["", "Installed in the plugins", "-" * 24] + installed["lines"]
    elif install:
        lines += ["", "Not installed in the plugins: the file is not signed off."]
    lines += ["", "Use: the Runner and the Screening read their core/acer_tm11_charts.json (the "
              "installed copy) unless a CHART_FILE is given to their hazard tools; keep the two "
              "copies identical. The official classification is made by the ND Department of "
              "Water Resources."]
    rec = os.path.join(out_folder, "Chart_SignOff.txt")
    with open(rec, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return {"chart_file": info["path"], "status": info["status"], "reason": info["reason"],
            "sha256": info["sha256"], "svgs": svgs, "record": rec, "equivalence": eq,
            "fits": fits, "lines": lines, "installed": installed}
