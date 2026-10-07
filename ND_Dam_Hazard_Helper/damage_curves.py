# -*- coding: utf-8 -*-
"""Depth-damage curves for structures the built-in residential curves
(USACE EGM 01-03, consequences.py) do not cover: commercial, industrial,
agricultural, multi-family, public buildings ...

The plugin ships NO curves for them. Which curves are right - FEMA Hazus,
USACE regional curves, an agency's own - is an engineering choice, so the
engineer supplies them in a CSV file (Hazard Classification tab > Consequences
> "Non-residential depth-damage curves"); without the file only the value
exposed is reported for these structures, as before.

CSV columns (header line required, '#' lines are comments):
    occupancy   NSI occupancy code or its start: COM1, COM, RES3, IND2 ...
                (COM1 does not match COM10; COM matches both; the longest
                match wins)
    kind        structure  - percent of the STRUCTURE value (NSI Val_Struct)
                content    - percent of the CONTENTS value (NSI Val_Cont)
    depth_ft    flood depth above the first floor, ft (negative = below it)
    percent     damage, 0 - 100

Between the points the damage is interpolated linearly; above the last point
it stays at the last value, below the first point it is 0. Each occupancy /
kind needs at least two points.
"""

import csv
import os

import numpy as np

KINDS = ("structure", "content")
TEMPLATE_NAME = "Nonresidential_Damage_Curves_TEMPLATE.csv"

TEMPLATE = """# Non-residential depth-damage curves for the ND Dam Hazard Classification Helper.
# The plugin has no curves for these structures: enter the ones you have chosen
# (FEMA Hazus, USACE, ...) and cite them in your submittal.
#
# occupancy : NSI occupancy code or its start (COM1, COM, IND2, RES3, AGR1 ...)
# kind      : structure (percent of the structure value) or content (percent of the contents value)
# depth_ft  : flood depth above the first floor, ft (negative = below the first floor)
# percent   : damage, 0 - 100
#
# THE ROWS BELOW ARE A FORMAT EXAMPLE ONLY - the numbers are NOT real curve values.
# Delete the leading '#' after replacing them with your curve.
#
occupancy,kind,depth_ft,percent
# COM1,structure,-2,0
# COM1,structure,0,<your value>
# COM1,structure,4,<your value>
# COM1,content,0,<your value>
# COM1,content,4,<your value>
"""


class CurveError(Exception):
    pass


def _matches(occ, prefix):
    if not occ.startswith(prefix):
        return False
    if len(occ) == len(prefix):
        return True
    # "COM1" must not match "COM10"
    return not (prefix[-1].isdigit() and occ[len(prefix)].isdigit())


class DamageCurves(object):
    """Curves by occupancy prefix and kind."""

    def __init__(self, source=""):
        self.source = source
        self.curves = {}                 # (prefix, kind) -> (depths, percents)

    def add(self, prefix, kind, points):
        pts = sorted(points)
        self.curves[(prefix.upper(), kind)] = (np.array([p[0] for p in pts], dtype=np.float64),
                                               np.array([p[1] for p in pts], dtype=np.float64))

    def prefixes(self):
        return sorted({p for p, _k in self.curves})

    def match(self, occtype):
        """The longest occupancy prefix with a curve for `occtype`, or None."""
        occ = str(occtype or "").strip().upper()
        if not occ:
            return None
        best = None
        for prefix in self.prefixes():
            if _matches(occ, prefix) and (best is None or len(prefix) > len(best)):
                best = prefix
        return best

    @staticmethod
    def _pct(curve, depth):
        d, p = curve
        if depth < d[0]:
            return 0.0
        return float(np.interp(depth, d, p))

    def damage(self, occtype, first_floor_depth, val_struct, val_cont):
        """(damage in USD, prefix used) for one NSI unit, or (None, None) when
        no curve fits. A curve kind whose value is missing adds nothing."""
        prefix = self.match(occtype)
        if prefix is None or first_floor_depth is None:
            return None, None
        total = 0.0
        for kind, value in (("structure", val_struct), ("content", val_cont)):
            curve = self.curves.get((prefix, kind))
            if curve is not None and value:
                total += float(value) * self._pct(curve, float(first_floor_depth)) / 100.0
        return total, prefix


def load(path):
    """(DamageCurves or None, [problem]) from a CSV file. Rows with a problem
    are skipped and reported; an occupancy / kind with fewer than two good
    points is dropped and reported."""
    problems = []
    if not path:
        return None, problems
    if not os.path.isfile(path):
        return None, ["curve file not found: {}".format(path)]
    try:
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
            rows = [r for r in csv.reader(fh)
                    if r and not (r[0].strip().startswith("#") or not "".join(r).strip())]
    except OSError as e:
        return None, ["curve file not readable: {}".format(e)]
    if not rows:
        return None, ["curve file is empty: {}".format(os.path.basename(path))]
    head = [h.strip().lower() for h in rows[0]]
    try:
        io_, ik, idp, ip = (head.index(k) for k in ("occupancy", "kind", "depth_ft", "percent"))
    except ValueError:
        return None, ["curve file needs the columns occupancy, kind, depth_ft, percent (found: "
                      "{})".format(", ".join(head))]
    pts = {}
    for n, r in enumerate(rows[1:], 2):
        try:
            occ = r[io_].strip().upper()
            kind = r[ik].strip().lower()
            depth = float(r[idp])
            pct = float(r[ip])
        except (IndexError, ValueError):
            problems.append("row {} skipped (not readable): {}".format(n, ",".join(r)[:60]))
            continue
        if not occ or kind not in KINDS or not (0.0 <= pct <= 100.0) \
                or not np.isfinite(depth):
            problems.append("row {} skipped (occupancy, kind structure/content, percent "
                            "0-100): {}".format(n, ",".join(r)[:60]))
            continue
        pts.setdefault((occ, kind), []).append((depth, pct))
    curves = DamageCurves(os.path.basename(path))
    for (occ, kind), p in sorted(pts.items()):
        if len({d for d, _v in p}) < 2:
            problems.append("{} {}: needs at least two different depths - not used".format(
                occ, kind))
            continue
        depths = [d for d, _v in p]
        if len(depths) != len(set(depths)):
            problems.append("{} {}: a depth is given twice - not used".format(occ, kind))
            continue
        ps = [v for _d, v in sorted(p)]
        if any(b < a - 1e-9 for a, b in zip(ps, ps[1:])):
            problems.append("{} {}: damage falls as the water gets deeper - check the "
                            "curve (used as given)".format(occ, kind))
        curves.add(occ, kind, p)
    if not curves.curves:
        return None, problems or ["no usable curve in {}".format(os.path.basename(path))]
    return curves, problems


def write_template(folder):
    """Write the template file into `folder`; returns its path."""
    path = os.path.join(folder, TEMPLATE_NAME)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(TEMPLATE)
    return path
